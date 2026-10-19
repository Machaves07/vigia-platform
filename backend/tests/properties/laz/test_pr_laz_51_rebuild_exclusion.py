"""PR-LAZ-51: exclusión entre dos reconstrucciones de la misma planta (TASK-306; LC-LAZ-03;
PAT-LAZ-RES-03; PAT-NUC-RES-05; domain-entities.md de U-04 §2.14).

Contra PostgreSQL 16 real como ``vigia_app``, dos ``ProjectionRebuilder.rebuild_plant`` lanzados a
la vez con ``asyncio.gather`` sobre el mismo fondo de conexiones (``Database``):

- de la **misma** planta, entra una sola (``RebuildResult``); la otra ve
  ``rebuild.AlreadyRebuilding`` y no toca nada — también cuando la primera ya está **computando**
  (fase 2, potencialmente larga), no solo durante su reclamo: la ronda 1 de revisión encontró que
  ``pg_try_advisory_xact_lock`` solo cubría la transacción corta del reclamo, así que una segunda
  reconstrucción entraba en cuanto la primera confirmaba la suya. El arrendamiento
  (``rebuild_owner``, ``rebuild_lease_until``) cubre la reconstrucción entera;
- de **dos plantas distintas**, ninguna espera a la otra: las dos terminan con ``RebuildResult``.

**Mutación** (AGENTS.md): se retira la condición del reclamo (``_CLAIM_LEASE`` sin el ``WHERE``
de la vigencia, siempre reclama); con ella, dos reconstrucciones de la misma planta —incluida la
pausada en fase 2— entran a la vez. Ejecutada manualmente contra el archivo (no queda como prueba
permanente: mutar el módulo de producción en una prueba de regresión no es el patrón de este
repositorio); ver la nota «Evidence» del PR con la salida real en rojo y la restauración.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, Final

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import InstrumentedPorts, LoopWorld, loop_world
from vigia_platform.ledger.application.evidence_read import EvidenceService
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.projection.rebuild import AlreadyRebuilding, ProjectionRebuilder

pytestmark = pytest.mark.integration

EXAMPLES: Final = 5


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[LoopWorld]:
    with loop_world(postgres_endpoint, "pr_laz_51") as built:
        yield built


def _rebuilder(
    world: LoopWorld, *, on_compute_started: Callable[[], Awaitable[None]] | None = None
) -> ProjectionRebuilder:
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
        on_compute_started=on_compute_started,
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


def test_pr_laz_51_second_rebuild_waits_out_the_whole_compute_phase(world: LoopWorld) -> None:
    """La exclusión cubre la fase 2 (cómputo), no solo el reclamo: A se pausa justo al entrar a
    computar; B (misma planta) se lanza mientras A sigue ahí dentro."""
    organization = world.organization()
    subject = world.ingest_finding(organization)
    world.project(subject)
    context = world.context(organization)

    reached = asyncio.Event()
    resume = asyncio.Event()

    async def pause() -> None:
        reached.set()
        await resume.wait()

    rebuilder_a = _rebuilder(world, on_compute_started=pause)
    rebuilder_b = _rebuilder(world)

    async def race() -> tuple[Any, Any]:
        task_a = asyncio.ensure_future(rebuilder_a.rebuild_plant(context, organization.plant_id))
        await reached.wait()
        task_b = asyncio.ensure_future(rebuilder_b.rebuild_plant(context, organization.plant_id))
        try:
            # Tope generoso (30 s, no un «tope corto»: retro 14 de AGENTS.md), no para el
            # comportamiento normal (B ve ``AlreadyRebuilding`` casi de inmediato por `_claim`),
            # sino para que la mutación manual de la tabla de garantías (quitar la condición del
            # `WHERE` de `_CLAIM_LEASE`) termine en un rojo legible en vez de colgar el proceso:
            # sin la exclusión, B reclama el arrendamiento de A y las dos quedan escribiendo la
            # misma sombra, lo que puede bloquearse en un candado de fila de PostgreSQL en lugar
            # de fallar rápido (hallazgo menor de la ronda 2 de revisión).
            result_b: Any = await asyncio.wait_for(task_b, timeout=30.0)
        except AlreadyRebuilding as error:
            result_b = error
        except TimeoutError as error:
            result_b = error
            with contextlib.suppress(BaseException):
                await task_b
        resume.set()
        result_a = await task_a
        return result_a, result_b

    result_a, result_b = world.stack.run(race())
    assert not isinstance(result_a, BaseException), result_a
    assert isinstance(result_b, AlreadyRebuilding), result_b


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
