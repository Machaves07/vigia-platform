"""Consumidor ``loop_projection`` contra PostgreSQL 16 real como ``vigia_app`` (TASK-341; LC-LAZ-02;
BR-LAZ-140, 150, 151, 154 y 155; NFR-LAZ-03 y 05; U04-H-05).

Sobre ``LoopWorld`` (``tests/laz_support.py``): hallazgos ingresados por la ruta del contrato de
U-03, el ``finding_received`` que la ingesta publicó en la bandeja y los puertos **reales** de U-03
y de U-02, contados por ``InstrumentedPorts``.

- **finding_received**: lee el ``Finding`` por ``LectorExpediente.get`` y deja la fila con lo que
  dicen ``CoveragePort.estado_en``, ``GateQueryPort.state_at`` y ``single_occupancy_many`` en el
  instante del hecho; una llamada a cada puerto; la segunda entrega no escribe ni consulta;
- **fallo hacia la privacidad**: sin respuesta de ``single_occupancy_many``, unipersonal con la
  ventana por defecto de la configuración de la planta (y el cubo con esa ventana);
- **marca de evidencias**: ``broken`` deja ``evidence_flagged`` y el hallazgo sigue proyectado;
- **eventos por zona**: ``gate_state_changed`` con ``revoked`` en **todas** las filas de la zona con
  **una** sentencia (y no en otra zona ni en otra organización); ``catalog_updated`` y
  ``regression_*`` desde el catálogo y la historia de regresión del expediente;
- **user_deactivated**: ``assignee_inactive`` en las filas del usuario de todas las plantas, sin
  cambiar su estado;
- **tramo tardío**: el registro ``late_segment_attached`` se escribe antes de aplicar el tramo;
- **concurrencia**: dos entregas simultáneas del mismo evento desde dos conexiones lo aplican una
  vez; entregas de eventos por zona, de usuario y escrituras de U-04 a la vez no se interbloquean.
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, Final

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import (
    IngestedSubject,
    InstrumentedPorts,
    LoopOrganization,
    LoopWorld,
    loop_world,
    register_u04_and_catalog,
)
from tests.writer_support import unit_context
from vigia_platform.catalog.domain.enums import GateKind
from vigia_platform.fleet.domain.ingest_order import IngestKind
from vigia_platform.ledger.application.writer import Receipt
from vigia_platform.loop.projection.fold import aggregation_bucket
from vigia_platform.shared.context import ActorKind, ActorUnit
from vigia_platform.shared.outbox.publish import OutboxEvent

pytestmark = pytest.mark.integration

LOCK_WAIT_SECONDS: Final = 60.0
"""Tope para ver la segunda transacción esperando la exclusión (no decide el resultado)."""
UPDATE_FINDING_STATE: Final = "UPDATE loop.finding_state"


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[LoopWorld]:
    with loop_world(
        postgres_endpoint, "loop_projection_consumer", register=register_u04_and_catalog
    ) as built:
        yield built


@pytest.fixture(autouse=True)
def _release_instances(world: LoopWorld) -> Iterator[None]:
    yield
    world.stack.run(world.stack.release())


class CountingTransaction:
    """La transacción del despachador, contando las sentencias que el consumidor ejecuta."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.statements: list[str] = []

    @property
    def context(self) -> Any:
        return self._inner.context

    async def execute(self, statement: Any, parameters: Any = None) -> Any:
        self.statements.append(" ".join(str(statement).split()))
        return await self._inner.execute(statement, parameters)

    def updates(self) -> int:
        return sum(statement.startswith(UPDATE_FINDING_STATE) for statement in self.statements)


def _counted(world: LoopWorld, event: OutboxEvent, ports: InstrumentedPorts | None = None):
    consumer, _ = world.projection_consumer(ports=ports)
    database = world.stack.primary.database
    context = unit_context(event.organization_id, ActorUnit.U04, kind=ActorKind.SYSTEM)

    async def run() -> tuple[int, CountingTransaction]:
        async with database.transaction(context) as transaction:
            counting = CountingTransaction(transaction)
            return await consumer.apply(event, counting), counting

    result: tuple[int, CountingTransaction] = world.stack.run(run())
    return result


def _projected(world: LoopWorld, organization: LoopOrganization) -> IngestedSubject:
    subject = world.ingest_finding(organization)
    consumer, _ = world.projection_consumer()
    assert world.deliver(consumer, world.outbox_event(subject)) == 1
    return subject


def _row(world: LoopWorld, subject: IngestedSubject) -> dict[str, Any]:
    row = world.finding_state(subject)
    assert row is not None
    return row


# --- finding_received ------------------------------------------------------------------------


def test_finding_received_reads_the_finding_and_enriches_it_from_the_ports(
    world: LoopWorld,
) -> None:
    organization = world.organization()
    subject = world.ingest_finding(organization)
    event = world.outbox_event(subject)
    consumer, ports = world.projection_consumer()
    assert world.deliver(consumer, event) == 1
    row = _row(world, subject)
    episode = world.episode(subject)
    assert episode is not None and episode["segment_count"] == 1
    assert row["lifecycle_state"] == "received"
    assert row["anchor_record_id"] == subject.record_id
    assert row["last_event_id"] == event.event_id
    # El oráculo: los mismos puertos reales, en el instante del hecho.
    real = world.real_ports()
    context = world.event_context(organization)
    occurred = row["occurred_at_exact"]
    coverage = world.stack.run(real.coverage.estado_en(context, organization.zone_id, occurred))
    interval = world.stack.run(
        real.gates.state_at(context, organization.zone_id, GateKind.USAGE, occurred)
    )
    (occupancy,) = world.stack.run(
        real.catalog.single_occupancy_many(context, [organization.zone_id])
    )
    assert row["observability_state_at_event"] == coverage.state.value
    assert interval is not None and row["zone_gate_status_at_event"] == interval.status.value
    assert row["single_occupancy_zone"] is occupancy.single_occupancy
    assert row["aggregation_window_minutes"] == occupancy.aggregation_window_minutes
    assert row["walk_test_regression_pending"] is False and row["evidence_flagged"] is False
    assert dict(ports.calls) == {"estado_en": 1, "state_at": 1, "single_occupancy_many": 1}
    checkpoint = world.checkpoint(organization)
    assert checkpoint is not None and checkpoint["last_applied_event_id"] == event.event_id
    # La segunda entrega (al menos una vez) ni escribe ni consulta los puertos.
    assert world.deliver(consumer, event) == 0
    assert _row(world, subject) == row
    assert dict(ports.calls) == {"estado_en": 1, "state_at": 1, "single_occupancy_many": 1}


def test_without_single_occupancy_the_zone_is_aggregated_with_the_configured_window(
    world: LoopWorld,
) -> None:
    organization = world.organization()
    world.configure_default_window(organization, 240)
    subject = world.ingest_finding(organization)
    failing = InstrumentedPorts(wrapped=world.real_ports(), fail_single_occupancy=True)
    consumer, _ = world.projection_consumer(ports=failing)
    assert world.deliver(consumer, world.outbox_event(subject)) == 1
    row = _row(world, subject)
    assert row["single_occupancy_zone"] is True
    assert row["aggregation_window_minutes"] == 240
    assert row["occurred_at_bucket"] == aggregation_bucket(row["occurred_at_exact"], 240)
    assert failing.calls["single_occupancy_many"] == 1


def test_the_catalog_answer_wins_over_the_default_when_the_port_answers(world: LoopWorld) -> None:
    organization = world.organization()
    world.configure_default_window(organization, 240)
    world.set_single_occupancy(organization, True, 120)
    subject = _projected(world, organization)
    row = _row(world, subject)
    assert (row["single_occupancy_zone"], row["aggregation_window_minutes"]) == (True, 120)
    assert row["occurred_at_bucket"] == aggregation_bucket(row["occurred_at_exact"], 120)


@pytest.mark.parametrize(("marker", "flagged"), [("broken", True), ("intact", False)])
def test_a_broken_marker_flags_the_finding_without_omitting_it(
    world: LoopWorld, marker: str, flagged: bool
) -> None:
    organization = world.organization()
    subject = world.ingest_finding(organization)
    world.set_marker(subject, marker)
    consumer, _ = world.projection_consumer()
    assert world.deliver(consumer, world.outbox_event(subject)) == 1
    row = _row(world, subject)
    assert row["evidence_flagged"] is flagged
    assert row["lifecycle_state"] == "received"


def test_a_late_segment_writes_its_record_before_it_is_projected(world: LoopWorld) -> None:
    organization = world.organization()
    first = _projected(world, organization)
    document = copy.deepcopy(dict(world.stack.finding(organization.site)))
    document["episode"] = {
        **dict(first.document["episode"]),
        "segment_index": 1,
        "continues": False,
        "previous_segment_record_id": str(first.record_id),
    }
    second = world._ingest(organization, IngestKind.FINDING, document)
    event = world.outbox_event(second)
    consumer, _ = world.projection_consumer()
    assert world.deliver(consumer, event) == 1
    (late,) = world.stack.records("late_segment_attached", organization.organization_id)
    assert late["content"]["segment_record_id"] == str(second.record_id)
    assert late["content"]["episode_state_at_arrival"] == "received"
    assert late["content"]["reopened_loop"] is False
    episode = world.episode(first)
    assert episode is not None and episode["segment_count"] == 2
    row = _row(world, first)
    assert row["lifecycle_state"] == "received" and row["last_record_id"] == late["record_id"]
    (published,) = world.stack.events("late_segment_attached", organization.organization_id)
    assert published["payload"]["record_id"] == str(late["record_id"])
    assert world.deliver(consumer, event) == 0
    assert len(world.stack.records("late_segment_attached", organization.organization_id)) == 1


# --- eventos por zona -------------------------------------------------------------------------


def test_a_revoked_usage_gate_marks_every_row_of_the_zone_with_one_statement(
    world: LoopWorld,
) -> None:
    organization = world.organization()
    subjects = [_projected(world, organization) for _ in range(3)]
    other_zone = world.clone_row(subjects[0], zone_id=organization.site.other_zone_id)
    elsewhere = _projected(world, world.organization())
    event = world.gate_event(organization, "revoked")
    written, counting = _counted(world, event)
    assert written == 3
    assert counting.updates() == 1
    for subject in subjects:
        row = _row(world, subject)
        assert row["zone_agreement_revoked"] is True
        assert row["zone_agreement_revoked_at"] == event.created_at
        assert row["lifecycle_state"] == "received" and row["last_event_id"] == event.event_id
    (cloned,) = world.rows(organization, subject_id=other_zone)
    assert cloned["zone_agreement_revoked"] is False
    assert _row(world, elsewhere)["zone_agreement_revoked"] is False
    checkpoint = world.checkpoint(organization)
    assert checkpoint is not None and checkpoint["last_applied_event_id"] == event.event_id
    # La segunda entrega no escribe ninguna fila.
    again, recount = _counted(world, event)
    assert (again, recount.updates()) == (0, 0)
    # Un acuerdo nuevo levanta la marca; la compuerta de montaje no la toca.
    mounting = world.synthetic_event(
        organization, "gate_state_changed", {**event.payload, "gate": "mounting"}
    )
    assert world.deliver(world.projection_consumer()[0], mounting) == 0
    approved = world.gate_event(organization, "approved")
    assert world.deliver(world.projection_consumer()[0], approved) == 3
    for subject in subjects:
        row = _row(world, subject)
        assert (row["zone_agreement_revoked"], row["zone_agreement_revoked_at"]) == (False, None)


def test_catalog_updated_refreshes_the_occupancy_of_the_zone_rows(world: LoopWorld) -> None:
    organization = world.organization()
    subjects = [_projected(world, organization) for _ in range(2)]
    bucket = _row(world, subjects[0])["occurred_at_bucket"]
    world.set_single_occupancy(organization, True, 180)
    event = world.synthetic_event(
        organization,
        "catalog_updated",
        {
            "zone_id": str(organization.zone_id),
            "catalog_version": 1,
            "changed_fields": ["single_occupancy"],
        },
    )
    written, counting = _counted(world, event)
    assert (written, counting.updates()) == (2, 1)
    for subject in subjects:
        row = _row(world, subject)
        assert (row["single_occupancy_zone"], row["aggregation_window_minutes"]) == (True, 180)
    # El cubo es la clave de partición: se fija al nacer la fila.
    assert _row(world, subjects[0])["occurred_at_bucket"] == bucket


def test_regression_events_rebuild_the_annotation_from_the_ledger_history(
    world: LoopWorld,
) -> None:
    organization = world.organization()
    subject = _projected(world, organization)
    occurred = _row(world, subject)["occurred_at_exact"]
    world.regression_record(organization, marked=True, at=occurred - timedelta(hours=1))
    marked = world.synthetic_event(
        organization,
        "regression_marked",
        {
            "zone_id": str(organization.zone_id),
            "cause": "catalog_change",
            "affected_row_ids": "all",
        },
    )
    assert world.deliver(world.projection_consumer()[0], marked) == 1
    row = _row(world, subject)
    assert (row["walk_test_regression_pending"], row["walk_test_regression_cause"]) == (
        True,
        "catalog_change",
    )
    # Levantada **después** del hecho: en el periodo del hecho el acta seguía sin estar vigente.
    world.regression_record(organization, marked=False, at=occurred + timedelta(minutes=5))
    cleared = world.synthetic_event(
        organization,
        "regression_cleared",
        {
            "zone_id": str(organization.zone_id),
            "cleared_by_session_id": str(uuid.uuid4()),
            "cause": "catalog_change",
            "affected_row_ids": "all",
        },
    )
    assert world.deliver(world.projection_consumer()[0], cleared) == 1
    assert _row(world, subject)["walk_test_regression_pending"] is True
    # Un hecho posterior a la reejecución nace con el acta vigente.
    later = world.ingest_finding(organization, started=occurred + timedelta(minutes=6))
    assert world.deliver(world.projection_consumer()[0], world.outbox_event(later)) == 1
    assert _row(world, later)["walk_test_regression_pending"] is False


def test_a_node_event_refreshes_its_zones_with_one_batch_call(world: LoopWorld) -> None:
    organization = world.organization()
    subjects = [_projected(world, organization) for _ in range(2)]
    world.set_single_occupancy(organization, True, 90)
    ports = InstrumentedPorts(wrapped=world.real_ports())
    event = world.synthetic_event(
        organization,
        "node_revoked",
        {
            "node_id": str(organization.site.node_id),
            "plant_id": str(organization.plant_id),
            "zone_ids": [str(organization.zone_id)],
        },
    )
    written, counting = _counted(world, event, ports)
    assert (written, counting.updates()) == (2, 1)
    assert dict(ports.calls) == {"single_occupancy_many": 1}
    for subject in subjects:
        assert _row(world, subject)["aggregation_window_minutes"] == 90


def test_user_deactivated_marks_the_actions_of_the_user_without_changing_their_state(
    world: LoopWorld,
) -> None:
    organization = world.organization()
    user = world.stack.authz.add_user(organization.organization_id)
    other = world.stack.authz.add_user(organization.organization_id)
    base = _projected(world, organization)
    second_plant = world.add_plant(organization)
    second_zone = uuid.uuid4()
    world.stack.authz.add_zone(organization.organization_id, second_plant, second_zone)
    action = {"current_action_type": "engineering", "current_action_id": uuid.uuid4()}
    mine = [
        world.clone_row(base, assignee_user_id=user, **action),
        world.clone_row(
            base, zone_id=second_zone, plant_id=second_plant, assignee_user_id=user, **action
        ),
    ]
    theirs = world.clone_row(base, assignee_user_id=other, **action)
    event = world.synthetic_event(
        organization,
        "user_deactivated",
        {
            "user_id": str(user),
            "deactivated_by": str(world.stack.authz.operator_id),
            "deactivated_at": world.stack.now().isoformat(),
        },
        plant=False,
    )
    written, counting = _counted(world, event)
    assert (written, counting.updates()) == (2, 1)
    for subject_id in mine:
        (row,) = world.rows(organization, subject_id=subject_id)
        assert row["assignee_inactive"] is True
        assert row["lifecycle_state"] == "received" and row["assignee_user_id"] == user
    (row,) = world.rows(organization, subject_id=theirs)
    assert row["assignee_inactive"] is False
    assert _row(world, base)["assignee_inactive"] is False
    assert _counted(world, event)[0] == 0


# --- concurrencia -----------------------------------------------------------------------------


async def _waiting_locks(world: LoopWorld) -> int:
    rows = await world.stack.authz.sessions.admin.fetch(
        "SELECT count(*) AS n FROM pg_locks WHERE NOT granted"
    )
    return int(rows[0]["n"])


def _race(world: LoopWorld, event: OutboxEvent) -> list[int]:
    """Dos entregas de ``event`` desde dos conexiones: la segunda empieza con la primera dentro."""
    consumers = [world.projection_consumer()[0], world.projection_consumer()[0]]
    databases = [world.stack.database(), world.stack.database()]
    context = unit_context(event.organization_id, ActorUnit.U04, kind=ActorKind.SYSTEM)
    applied = asyncio.Event()
    release = asyncio.Event()

    async def first() -> int:
        async with databases[0].transaction(context) as transaction:
            written = await consumers[0].apply(event, transaction)
            applied.set()
            await release.wait()
            return written

    async def second() -> int:
        await applied.wait()
        async with databases[1].transaction(context) as transaction:
            return await consumers[1].apply(event, transaction)

    async def race() -> list[int]:
        tasks = [asyncio.create_task(first()), asyncio.create_task(second())]
        await applied.wait()
        async with asyncio.timeout(LOCK_WAIT_SECONDS):
            while await _waiting_locks(world) == 0:
                await asyncio.sleep(0.05)
        release.set()
        return list(await asyncio.gather(*tasks))

    try:
        results: list[int] = world.stack.run(race())
    finally:
        for database in databases:
            world.stack.run(database.dispose())
    return results


def test_two_simultaneous_deliveries_of_one_zone_event_apply_it_once(world: LoopWorld) -> None:
    organization = world.organization()
    for _ in range(3):
        _projected(world, organization)
    event = world.gate_event(organization, "revoked")
    assert sorted(_race(world, event)) == [0, 3]


def test_two_simultaneous_deliveries_of_one_finding_apply_it_once(world: LoopWorld) -> None:
    organization = world.organization()
    subject = world.ingest_finding(organization)
    consumers = [world.projection_consumer()[0], world.projection_consumer()[0]]
    databases = [world.stack.database(), world.stack.database()]
    event = world.outbox_event(subject)
    context = unit_context(event.organization_id, ActorUnit.U04, kind=ActorKind.SYSTEM)
    start = asyncio.Event()

    async def deliver(index: int) -> int:
        await start.wait()
        async with databases[index].transaction(context) as transaction:
            return await consumers[index].apply(event, transaction)

    async def race() -> list[int]:
        tasks = [asyncio.create_task(deliver(index)) for index in range(2)]
        await asyncio.sleep(0)
        start.set()
        return list(await asyncio.gather(*tasks))

    try:
        results = world.stack.run(race())
    finally:
        for database in databases:
            world.stack.run(database.dispose())
    assert sorted(results) == [0, 1]
    row = _row(world, subject)
    assert row["last_event_id"] == event.event_id


CROSS_ROUNDS: Final = 4
"""Rondas de la prueba cruzada del orden de candados."""


def test_zone_events_user_events_and_u04_writes_share_one_lock_order(world: LoopWorld) -> None:
    """Prueba cruzada (cadena → control → filas): a la vez, sobre la misma planta, clasificaciones
    por ``write_and_apply``, ``gate_state_changed`` (todas las filas de la zona) y
    ``user_deactivated`` (filas de dos plantas). Ninguna acaba en interbloqueo."""
    organization = world.organization()
    user = world.stack.authz.add_user(organization.organization_id)
    subjects = [_projected(world, organization) for _ in range(CROSS_ROUNDS)]
    second_plant = world.add_plant(organization)
    second_zone = uuid.uuid4()
    world.stack.authz.add_zone(organization.organization_id, second_plant, second_zone)
    action = {"current_action_type": "engineering", "current_action_id": uuid.uuid4()}
    for subject in subjects[:2]:
        world.clone_row(subject, assignee_user_id=user, **action)
        world.clone_row(
            subject, zone_id=second_zone, plant_id=second_plant, assignee_user_id=user, **action
        )
    databases = [world.stack.database() for _ in range(3)]
    writer = world.stack.primary.writer
    scope = world.scope(organization)
    context = world.context(organization)
    event_context = world.event_context(organization)
    gates = [
        world.gate_event(organization, "revoked" if index % 2 == 0 else "approved")
        for index in range(CROSS_ROUNDS)
    ]
    users = [
        world.synthetic_event(
            organization,
            "user_deactivated",
            {
                "user_id": str(user),
                "deactivated_by": str(world.stack.authz.operator_id),
                "deactivated_at": world.stack.now().isoformat(),
            },
            plant=False,
        )
        for _ in range(CROSS_ROUNDS)
    ]
    consumer = world.projection_consumer(database=databases[1])[0]
    start = asyncio.Event()

    async def classify(subject: IngestedSubject) -> object:
        await start.wait()
        async with databases[0].transaction(context) as transaction:
            receipt, _ = await world.store.write_and_apply(
                transaction,
                writer,
                context,
                "classification",
                world.classification_content(subject),
                scope=scope,
            )
            return receipt

    async def deliver(event: OutboxEvent, index: int) -> object:
        await start.wait()
        async with databases[index].transaction(event_context) as transaction:
            return await consumer.apply(event, transaction)

    async def race() -> list[object]:
        tasks = [
            *(asyncio.create_task(classify(subject)) for subject in subjects),
            *(asyncio.create_task(deliver(event, 1)) for event in gates),
            *(asyncio.create_task(deliver(event, 2)) for event in users),
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
    assert all(isinstance(result, Receipt) for result in results[:CROSS_ROUNDS])
    for subject in subjects:
        assert _row(world, subject)["classification_count"] == 1
