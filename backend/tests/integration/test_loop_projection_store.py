"""``LoopProjectionStore.apply_in_transaction`` contra PostgreSQL 16 real como ``vigia_app``
(TASK-305; LC-LAZ-02; BR-LAZ-146 y 151; PR-LAZ-43).

Sobre ``LoopWorld`` (``tests/laz_support.py``): hallazgos y detecciones ingresados por las rutas
del contrato de U-03 y registros de U-04 escritos por el ``EscritorExpediente`` real.

- **misma transacción**: la clasificación y su proyección se confirman juntas; si la transacción
  se revierte **después** de aplicar la proyección, no queda ni el registro ni la fila
  proyectada, y el control de la partición no avanza;
- **lo escrito es el pliegue**: las filas y el resumen de la partición en la base son los que
  ``fold`` calcula desde los registros de la cadena de la planta;
- **idempotencia**: el evento repetido y el ``accepted_duplicate`` del escritor no cambian nada;
- **alcance**: otra organización no ve el registro (``ProjectionRecordMissing``) y un registro de
  otra planta no alcanza al sujeto de esta;
- **concurrencia**: dos aplicaciones simultáneas del mismo registro desde dos conexiones dejan una
  sola aplicación (la segunda espera la exclusión del control y ve ``last_record_id``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from collections.abc import Iterator
from typing import Any, Final

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import IngestedSubject, LoopOrganization, LoopWorld, loop_world
from vigia_platform.ledger.application.writer import Receipt
from vigia_platform.loop.projection.fold import (
    EpisodeRow,
    FindingStateRow,
    ProjectionInconsistent,
    Step,
    fold,
)
from vigia_platform.loop.projection.store import ProjectionRecordMissing

pytestmark = pytest.mark.integration

LOCK_WAIT_SECONDS: Final = 60.0
"""Tope para ver la segunda transacción esperando la exclusión (no decide el resultado)."""


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[LoopWorld]:
    with loop_world(postgres_endpoint, "loop_projection_store") as built:
        yield built


@pytest.fixture(autouse=True)
def _release_instances(world: LoopWorld) -> Iterator[None]:
    yield
    world.stack.run(world.stack.release())


def _received(world: LoopWorld, organization: LoopOrganization | None = None) -> IngestedSubject:
    finding = world.ingest_finding(organization or world.a)
    step = world.project(finding)
    assert step.applied and step.transition_id == "T01"
    return finding


def _classify(world: LoopWorld, subject: IngestedSubject, **changes: Any) -> tuple[Receipt, Step]:
    receipt, step = world.write(
        subject.organization, "classification", world.classification_content(subject, **changes)
    )
    assert isinstance(receipt, Receipt), receipt
    assert step is not None
    return receipt, step


# --- misma transacción ---------------------------------------------------------------------


def test_a_classification_and_its_projection_commit_together(world: LoopWorld) -> None:
    finding = _received(world)
    receipt, step = _classify(world, finding)
    assert (step.applied, step.transition_id) == (True, "T03")
    row = world.finding_state(finding)
    assert row is not None
    assert (row["lifecycle_state"], row["classification_count"]) == ("open", 1)
    assert row["last_record_id"] == receipt.record_id
    assert world.record_exists(receipt.record_id)


def test_a_rollback_after_applying_the_projection_leaves_neither_record_nor_projection(
    world: LoopWorld,
) -> None:
    finding = _received(world)
    before_row = world.finding_state(finding)
    before_checkpoint = world.checkpoint(world.a)
    content = world.classification_content(finding)
    receipt, step = world.write(world.a, "classification", content, rollback=True)
    assert isinstance(receipt, Receipt)
    assert step is not None and step.applied  # la proyección se aplicó dentro de la transacción
    assert not world.record_exists(receipt.record_id)
    assert world.finding_state(finding) == before_row
    assert world.checkpoint(world.a) == before_checkpoint
    # Y nada quedó a medias: la misma clasificación se escribe y se proyecta después.
    receipt, step = world.write(world.a, "classification", content)
    assert isinstance(receipt, Receipt) and step is not None and step.transition_id == "T03"


def test_a_new_subject_rolled_back_leaves_no_row(world: LoopWorld) -> None:
    detection = world.ingest_detection(world.a)
    context = world.event_context(world.a)
    database = world.stack.primary.database

    class Abort(Exception):
        pass

    async def apply_then_abort() -> Step:
        async with database.transaction(context) as transaction:
            item = await world.store.record_input(transaction, detection.record_id)
            step = await world.store.apply_in_transaction(transaction, item)
            assert step.applied and step.transition_id == "T02"
            raise Abort

    with pytest.raises(Abort):
        world.stack.run(apply_then_abort())
    assert world.finding_state(detection) is None
    assert world.project(detection).transition_id == "T02"
    row = world.finding_state(detection)
    assert row is not None and (row["subject_kind"], row["lifecycle_state"]) == (
        "detection",
        "in_review",
    )


# --- lo escrito es el pliegue ----------------------------------------------------------------


def _rows(
    world: LoopWorld, organization: LoopOrganization
) -> tuple[dict[uuid.UUID, Any], dict[uuid.UUID, Any]]:
    episodes = world.stack.fetch(
        "SELECT * FROM loop.episode WHERE organization_id = $1 AND plant_id = $2",
        organization.organization_id,
        organization.plant_id,
    )
    findings = world.stack.fetch(
        "SELECT * FROM loop.finding_state WHERE organization_id = $1 AND plant_id = $2",
        organization.organization_id,
        organization.plant_id,
    )
    # La conexión de superusuario (asyncpg sin códec) devuelve ``jsonb`` como texto.
    return (
        {
            uuid.UUID(str(row["episode_id"])): {
                **dict(row),
                "clip_references": json.loads(row["clip_references"]),
            }
            for row in episodes
        },
        {uuid.UUID(str(row["subject_id"])): dict(row) for row in findings},
    )


def _as_db(row: EpisodeRow | FindingStateRow) -> dict[str, Any]:
    values = dataclasses.asdict(row)
    for name in ("segment_record_ids", "missing_segment_indexes"):
        if name in values:
            values[name] = list(values[name])
    if "clip_references" in values:
        values["clip_references"] = [dict(clip) for clip in values["clip_references"]]
    return values


def test_the_database_holds_exactly_what_the_fold_computes_from_the_chain(
    world: LoopWorld,
) -> None:
    # Una organización propia: el mundo es del módulo y otras pruebas dejan hallazgos sin proyectar.
    organization = world.organization()
    finding = _received(world, organization)
    second = _received(world, organization)
    detection = world.ingest_detection(organization)
    world.project(detection)
    _classify(
        world,
        finding,
        outcome="false_positive",
        reason_category="reflection_or_glare",
        effect_on_loop="none",
    )
    _classify(world, second)
    episodes, findings = _rows(world, organization)
    (projection,) = fold(world.ledger_records(organization)).values()
    assert len(projection.findings) == 3
    assert set(findings) == set(projection.findings)
    for subject, row in projection.findings.items():
        stored = findings[subject]
        expected = _as_db(row)
        # La contabilidad depende de la vía (evento o reconstrucción), no del hecho.
        for name in ("last_event_id", "projection_updated_at"):
            expected.pop(name)
            stored.pop(name)
        assert {key: stored[key] for key in expected} == expected
    for subject, episode_row in projection.episodes.items():
        expected = _as_db(episode_row)
        stored = episodes[subject]
        for name in ("last_event_id", "projection_updated_at"):
            expected.pop(name)
            stored.pop(name)
        assert {key: stored[key] for key in expected} == expected
    checkpoint = world.checkpoint(organization)
    assert checkpoint is not None
    assert checkpoint["projection_digest"] == projection.projection_digest
    assert checkpoint["last_applied_ledger_sequence"] == projection.last_applied_ledger_sequence


# --- idempotencia ----------------------------------------------------------------------------


def test_a_repeated_event_and_a_writer_duplicate_change_nothing(world: LoopWorld) -> None:
    finding = world.ingest_finding(world.a)
    event_id = uuid.uuid4()
    assert world.project(finding, event_id=event_id).applied
    row = world.finding_state(finding)
    checkpoint = world.checkpoint(world.a)
    assert checkpoint is not None and checkpoint["last_applied_event_id"] == event_id
    assert world.project(finding, event_id=event_id).applied is False
    assert world.project(finding).applied is False
    assert world.finding_state(finding) == row
    content = world.classification_content(finding)
    first, step = world.write(world.a, "classification", content)
    assert isinstance(first, Receipt) and step is not None and step.applied
    after = world.finding_state(finding)
    again, replay = world.write(world.a, "classification", content)
    assert isinstance(again, Receipt) and again.status.value == "accepted_duplicate"
    assert replay is not None and replay.applied is False
    assert world.finding_state(finding) == after


# --- alcance -----------------------------------------------------------------------------------


def test_another_organization_cannot_see_nor_project_the_record(world: LoopWorld) -> None:
    finding = world.ingest_finding(world.a)
    context = world.event_context(world.b)
    database = world.stack.primary.database

    async def load() -> None:
        async with database.transaction(context) as transaction:
            await world.store.record_input(transaction, finding.record_id)

    with pytest.raises(ProjectionRecordMissing):
        world.stack.run(load())
    assert world.finding_state(finding) is None


def test_a_record_of_another_plant_does_not_reach_this_plant_subject(world: LoopWorld) -> None:
    finding = _received(world)
    receipt, _ = _classify(world, finding)
    other_plant = world.add_plant(world.a)
    context = world.event_context(world.a)
    database = world.stack.primary.database

    async def load() -> Any:
        async with database.transaction(context) as transaction:
            return await world.store.record_input(transaction, receipt.record_id)

    item = world.stack.run(load())
    moved = dataclasses.replace(
        item,
        plant_id=other_plant,
        record_id=uuid.uuid4(),
        document={
            **item.document,
            "outcome": "false_positive",
            "reason_category": "reflection_or_glare",
            "reinforced_reason": True,
        },
    )
    before = world.finding_state(finding)
    before_episode = world.episode(finding)
    with pytest.raises(ProjectionInconsistent, match="no existe"):
        world.apply(world.a, moved)
    assert world.finding_state(finding) == before

    async def load_segment() -> Any:
        async with database.transaction(context) as transaction:
            return await world.store.record_input(transaction, finding.record_id)

    segment = world.stack.run(load_segment())
    episode = dict(segment.document["episode"], segment_index=1, continues=False)
    other_segment = dataclasses.replace(
        segment,
        plant_id=other_plant,
        record_id=uuid.uuid4(),
        document={**segment.document, "plant_id": str(other_plant), "episode": episode},
    )
    with pytest.raises(ProjectionInconsistent, match="no concuerdan"):
        world.apply(world.a, other_segment)
    assert world.episode(finding) == before_episode
    assert world.finding_state(finding) == before


# --- concurrencia ------------------------------------------------------------------------------


async def _waiting_locks(world: LoopWorld) -> int:
    rows = await world.stack.authz.sessions.admin.fetch(
        "SELECT count(*) AS n FROM pg_locks WHERE NOT granted"
    )
    return int(rows[0]["n"])


def test_two_simultaneous_applications_of_one_record_apply_it_once(world: LoopWorld) -> None:
    finding = _received(world)
    _classify(
        world,
        finding,
        outcome="false_positive",
        reason_category="reflection_or_glare",
        effect_on_loop="none",
    )
    content = world.classification_content(
        finding,
        outcome="authorized_operation",
        reason_category="scheduled_maintenance",
        sequence=2,
        previous_outcome="false_positive",
        effect_on_loop="none",
    )
    # T18 (descartado → descartado) es aplicable otra vez: sin la guarda contaría dos veces.
    receipt, step = world.write(world.a, "classification", content, apply=False)
    assert isinstance(receipt, Receipt) and step is None
    first_database = world.stack.database()
    second_database = world.stack.database()
    context = world.event_context(world.a)
    applied = asyncio.Event()
    release = asyncio.Event()

    async def first() -> Step:
        async with first_database.transaction(context) as transaction:
            result = await world.store.apply_in_transaction(transaction, receipt)
            applied.set()
            await release.wait()
            return result

    async def second() -> Step:
        await applied.wait()
        async with second_database.transaction(context) as transaction:
            return await world.store.apply_in_transaction(transaction, receipt)

    async def race() -> list[Step]:
        tasks = [asyncio.create_task(first()), asyncio.create_task(second())]
        await applied.wait()
        async with asyncio.timeout(LOCK_WAIT_SECONDS):
            while await _waiting_locks(world) == 0:
                await asyncio.sleep(0.05)
        release.set()
        return list(await asyncio.gather(*tasks))

    try:
        results = world.stack.run(race())
    finally:
        world.stack.run(first_database.dispose())
        world.stack.run(second_database.dispose())
    assert sorted(result.applied for result in results) == [False, True]
    row = world.finding_state(finding)
    assert row is not None
    assert (row["lifecycle_state"], row["classification_count"]) == ("discarded", 2)
    assert row["current_classification_outcome"] == "authorized_operation"


CROSS_PAIRS: Final = 6
"""Parejas de escrituras concurrentes sobre la misma planta en la prueba cruzada."""


def test_concurrent_writes_and_event_applications_share_one_lock_order(world: LoopWorld) -> None:
    """Prueba cruzada del orden de candados (cadena de la planta → control → filas).

    A la vez, sobre la misma planta: ``write_and_apply`` (la forma de las operaciones de U-04),
    la secuencia directa escritor → ``apply_in_transaction`` que use una operación sin el
    ayudante, y la vía de eventos (solo el control). Ninguna debe acabar en interbloqueo ni en
    ``TemporarilyUnavailable``, y cada sujeto queda con su única clasificación."""
    organization = world.organization()
    helper = [_received(world, organization) for _ in range(CROSS_PAIRS)]
    direct = [_received(world, organization) for _ in range(CROSS_PAIRS)]
    events = [world.ingest_detection(organization) for _ in range(CROSS_PAIRS)]
    writer = world.stack.primary.writer
    scope = world.scope(organization)
    databases = [world.stack.database(), world.stack.database(), world.stack.database()]
    context = world.context(organization)
    event_context = world.event_context(organization)
    start = asyncio.Event()

    async def with_helper(subject: IngestedSubject) -> Step:
        await start.wait()
        async with databases[0].transaction(context) as transaction:
            _, step = await world.store.write_and_apply(
                transaction,
                writer,
                context,
                "classification",
                world.classification_content(subject),
                scope=scope,
            )
            return step

    async def direct_order(subject: IngestedSubject) -> Step:
        await start.wait()
        async with databases[1].transaction(context) as transaction:
            receipt = await writer.write(
                context,
                "classification",
                world.classification_content(subject),
                scope=scope,
                transaction=transaction,
            )
            assert isinstance(receipt, Receipt)
            return await world.store.apply_in_transaction(transaction, receipt)

    async def by_event(subject: IngestedSubject) -> Step:
        await start.wait()
        async with databases[2].transaction(event_context) as transaction:
            item = await world.store.record_input(
                transaction, subject.record_id, event_id=uuid.uuid4()
            )
            return await world.store.apply_in_transaction(transaction, item)

    async def race() -> list[Step | BaseException]:
        tasks = [
            *(asyncio.create_task(with_helper(subject)) for subject in helper),
            *(asyncio.create_task(direct_order(subject)) for subject in direct),
            *(asyncio.create_task(by_event(subject)) for subject in events),
        ]
        await asyncio.sleep(0)
        start.set()
        return list(await asyncio.gather(*tasks, return_exceptions=True))

    try:
        results = world.stack.run(race())
    finally:
        for database in databases:
            world.stack.run(database.dispose())
    failures = [result for result in results if isinstance(result, BaseException)]
    assert failures == []
    assert all(isinstance(result, Step) and result.applied for result in results)
    for subject in (*helper, *direct):
        row = world.finding_state(subject)
        assert row is not None
        assert (row["lifecycle_state"], row["classification_count"]) == ("open", 1)
    for subject in events:
        row = world.finding_state(subject)
        assert row is not None and row["lifecycle_state"] == "in_review"
    (projection,) = fold(world.ledger_records(organization)).values()
    checkpoint = world.checkpoint(organization)
    assert checkpoint is not None
    assert checkpoint["projection_digest"] == projection.projection_digest
