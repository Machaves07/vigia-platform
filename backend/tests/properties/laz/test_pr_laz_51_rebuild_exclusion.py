"""PR-LAZ-51: exclusión entre dos reconstrucciones de la misma planta (TASK-306; LC-LAZ-03;
PAT-LAZ-RES-03; PAT-NUC-ESC-05; domain-entities.md de U-04 §2.14).

Contra PostgreSQL 16 real como ``vigia_app``, dos ``ProjectionRebuilder.rebuild_plant`` lanzados a
la vez con ``asyncio.gather`` sobre el mismo fondo de conexiones (``Database``), cada uno en su
propia transacción:

- de la **misma** planta, entra una sola (``RebuildResult``); la otra ve
  ``rebuild.AlreadyRebuilding`` y no toca nada;
- de **dos plantas distintas**, ninguna espera a la otra: las dos terminan con ``RebuildResult``.

**Mutación** (bloqueo consultivo sustituido por una clave distinta por llamada, que nunca
comparte candado con nadie): las dos reconstrucciones de la misma planta entran a la vez, sin que
ninguna vea ``AlreadyRebuilding`` — la exclusión depende solo de
``pg_try_advisory_xact_lock(hash(organization_id, plant_id))``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any, Final

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import InstrumentedPorts, LoopWorld, loop_world
from vigia_platform.ledger.application.evidence_read import EvidenceService
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.projection import rebuild as rebuild_module
from vigia_platform.loop.projection.rebuild import AlreadyRebuilding, ProjectionRebuilder

pytestmark = pytest.mark.integration

EXAMPLES: Final = 5


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[LoopWorld]:
    with loop_world(postgres_endpoint, "pr_laz_51") as built:
        yield built


def _rebuilder(world: LoopWorld) -> ProjectionRebuilder:
    database = world.stack.primary.database
    sessions = world.stack.authz.sessions
    ports = InstrumentedPorts(wrapped=world.real_ports(database))
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
    )


async def _race(
    rebuilder: ProjectionRebuilder, context: Any, plant_id: uuid.UUID
) -> tuple[Any, Any]:
    return await asyncio.gather(
        rebuilder.rebuild_plant(context, plant_id),
        rebuilder.rebuild_plant(context, plant_id),
        return_exceptions=True,
    )


def test_pr_laz_51_same_plant_only_one_enters(world: LoopWorld) -> None:
    """``EXAMPLES`` rondas independientes: cada una, su propio hallazgo y su propia carrera."""
    for _ in range(EXAMPLES):
        organization = world.organization()
        subject = world.ingest_finding(organization)
        world.project(subject)
        context = world.context(organization)
        rebuilder = _rebuilder(world)

        results = world.stack.run(_race(rebuilder, context, organization.plant_id))
        successes = [r for r in results if not isinstance(r, BaseException)]
        failures = [r for r in results if isinstance(r, AlreadyRebuilding)]
        assert len(successes) == 1, results
        assert len(failures) == 1, results


def test_pr_laz_51_different_plants_never_block(world: LoopWorld) -> None:
    organization = world.organization()
    other_plant_id = world.add_plant(organization)
    subject_a = world.ingest_finding(organization)
    world.project(subject_a)
    context = world.context(organization)
    rebuilder = _rebuilder(world)

    async def race() -> tuple[Any, Any]:
        return await asyncio.gather(
            rebuilder.rebuild_plant(context, organization.plant_id),
            rebuilder.rebuild_plant(context, other_plant_id),
            return_exceptions=True,
        )

    results = world.stack.run(race())
    assert all(not isinstance(r, BaseException) for r in results), results


def test_pr_laz_51_mutation_without_shared_lock_both_enter(world: LoopWorld) -> None:
    """Mutación del bloqueo (AGENTS.md): una clave distinta por llamada nunca comparte candado,
    así que las dos reconstrucciones de la misma planta entran a la vez."""
    organization = world.organization()
    subject = world.ingest_finding(organization)
    world.project(subject)
    context = world.context(organization)
    rebuilder = _rebuilder(world)

    original_key = rebuild_module.rebuild_lock_key

    def unique_key(organization_id: uuid.UUID, plant_id: uuid.UUID) -> str:
        return f"{original_key(organization_id, plant_id)}|{uuid.uuid4()}"

    rebuild_module.rebuild_lock_key = unique_key
    try:
        results = world.stack.run(_race(rebuilder, context, organization.plant_id))
    finally:
        rebuild_module.rebuild_lock_key = original_key

    failures = [r for r in results if isinstance(r, AlreadyRebuilding)]
    assert not failures, results
