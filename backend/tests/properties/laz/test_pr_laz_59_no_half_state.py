"""PR-LAZ-59: ninguna lectura de la bandeja ve una mezcla de filas viejas y nuevas durante la
reconstrucción (TASK-306; LC-LAZ-03; PAT-LAZ-RES-03; domain-entities.md de U-04 §2.14 y §9).

Contra PostgreSQL 16 real como ``vigia_app``, dos hallazgos de la misma planta, cada uno con su
``projection_digest`` mutado (como un defecto de la caché) para que la reconstrucción cambie su
valor de forma observable. Un lector en su propia conexión muestrea ``loop.finding_state`` en un
bucle apretado, a la vez que ``ProjectionRebuilder.rebuild_plant`` corre (con un lote de tamaño 1,
para ensanchar la ventana con más viajes a la base por planta): en **cada** muestra, las dos filas
están en el mismo lado (las dos con el resumen mutado, o las dos con el corregido), nunca una de
cada.

**Mutación** (la tabla sombra se omite y la reconstrucción escribe fila a fila directamente en
``loop.finding_state``): el muestreador ve una mezcla al menos una vez.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Final

import pytest
from sqlalchemy import text

from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import (
    IngestedSubject,
    InstrumentedPorts,
    LoopOrganization,
    LoopWorld,
    loop_world,
)
from vigia_platform.ledger.application.evidence_read import EvidenceService
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.projection.fold import FindingStateRow
from vigia_platform.loop.projection.rebuild import ProjectionRebuilder

_DIGESTS: Final = text(
    "SELECT projection_digest FROM loop.finding_state"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " ORDER BY finding_state_id"
)

pytestmark = pytest.mark.integration

ROUNDS: Final = 3
"""Rondas independientes de reconstrucción + muestreo concurrente."""


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[LoopWorld]:
    with loop_world(postgres_endpoint, "pr_laz_59") as built:
        yield built


def _rebuilder(
    world: LoopWorld, ports: InstrumentedPorts, *, batch_size: int = 1
) -> ProjectionRebuilder:
    database = world.stack.primary.database
    sessions = world.stack.authz.sessions
    return ProjectionRebuilder(
        database=database,
        catalog=ports,
        gates=ports,
        coverage=ports,
        markers=EvidenceService(
            database=database, audit=sessions.audit, storage=world.stack.storage
        ),
        settings=PostgresLoopConfigurationRepository(database),
        clock=sessions.clock,
        batch_size=batch_size,
    )


def _mutate(world: LoopWorld, subject: IngestedSubject) -> tuple[str, str]:
    """Invierte ``evidence_flagged`` y recalcula un ``projection_digest`` consistente con el
    valor mutado (como un defecto de la caché, no una operación de U-04); devuelve
    ``(digest_mutado, digest_correcto)``."""
    organization = subject.organization
    row = world.finding_state(subject)
    assert row is not None
    correct_digest = row["projection_digest"]
    world.stack.execute(
        "UPDATE loop.finding_state SET evidence_flagged = NOT evidence_flagged"
        " WHERE organization_id = $1 AND finding_state_id = $2",
        organization.organization_id,
        row["finding_state_id"],
    )
    mutated_row = world.finding_state(subject)
    assert mutated_row is not None
    mutated_digest = FindingStateRow(**mutated_row).computed_digest()
    world.stack.execute(
        "UPDATE loop.finding_state SET projection_digest = $1"
        " WHERE organization_id = $2 AND finding_state_id = $3",
        mutated_digest,
        organization.organization_id,
        row["finding_state_id"],
    )
    return mutated_digest, correct_digest


async def _sample_digests(world: LoopWorld, organization: LoopOrganization) -> list[str]:
    rows = await world.stack.primary.database.read(
        world.event_context(organization),
        _DIGESTS,
        {"organization_id": organization.organization_id, "plant_id": organization.plant_id},
    )
    return [str(row.projection_digest) for row in rows]


async def _poll_while(
    world: LoopWorld,
    organization: LoopOrganization,
    stop: asyncio.Event,
    samples: list[list[str]],
) -> None:
    while not stop.is_set():
        samples.append(await _sample_digests(world, organization))
        await asyncio.sleep(0)


async def _run_round(
    world: LoopWorld, rebuilder: ProjectionRebuilder, subjects: list[IngestedSubject]
) -> list[list[str]]:
    organization = subjects[0].organization
    context = world.context(organization)
    stop = asyncio.Event()
    samples: list[list[str]] = []
    poller = asyncio.ensure_future(_poll_while(world, organization, stop, samples))
    try:
        await rebuilder.rebuild_plant(context, organization.plant_id)
    finally:
        stop.set()
        await poller
    samples.append(await _sample_digests(world, organization))
    return samples


def test_pr_laz_59_no_reader_ever_sees_a_mix(world: LoopWorld) -> None:
    for _ in range(ROUNDS):
        organization = world.organization()
        consumer, ports = world.projection_consumer()
        subjects = [world.ingest_finding(organization) for _ in range(2)]
        for subject in subjects:
            # Con el consumidor real, como en la vía incremental: la reconstrucción enriquece
            # igual, así que el resumen "correcto" coincide con el que ella produce (PR-LAZ-44).
            written = world.deliver(consumer, world.outbox_event(subject))
            assert written == 1
        mutated_and_correct = [_mutate(world, subject) for subject in subjects]
        mutated = {digest for digest, _ in mutated_and_correct}
        correct = {digest for _, digest in mutated_and_correct}

        rebuilder = _rebuilder(world, ports)
        samples = world.stack.run(_run_round(world, rebuilder, subjects))

        for sample in samples:
            assert len(sample) == 2, sample
            assert set(sample) <= mutated or set(sample) <= correct, (sample, mutated, correct)
        # Al menos la última muestra, tras terminar, debe ser la corregida.
        assert set(samples[-1]) <= correct
