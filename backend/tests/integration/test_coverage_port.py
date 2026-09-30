"""``CoveragePort`` sobre PostgreSQL 16: cargas por índice, alcance y auditoría (TASK-120).

Como ``vigia_app``, con los registros de U-03 escritos por ``EscritorExpediente`` (tipos de
prueba con los nombres y campos de U-03 y del contrato):

- **Las cargas no cambian el resultado**: para escenarios generados con ``coverage_inputs``
  (eventos de la zona y de otros sujetos, pares, huérfanos, aperturas sin cierre, dos nodos,
  compuertas y asignaciones), ``linea_de_tiempo`` es igual a ``compose_timeline`` sobre **todas**
  las entradas del escenario, y ``estado_en`` en cada borde y en instantes generados es igual a
  ``state_at`` sobre todas ellas (PR-NUC-26 a través de la base). La base solo lee lo que puede
  influir en el periodo; si filtrara de más o de menos, la igualdad fallaría.
- ``CommunicationState``: con el instante posterior al inicio de la última declaración se usa la
  proyección; con uno anterior, la historia.
- **Tope**: 32 días responde ``PeriodTooLong`` (``period_too_long``) sin consultar ni auditar.
- **Alcance**: una zona de otra zona, de otra planta, de otra organización o inexistente
  responde ``CoverageZoneNotFound`` (``not_found``); una de la propia planta u organización se ve.
- **Auditoría**: cada consulta escribe exactamente una ``coverage_read`` con la zona y el periodo
  o el instante; en la misma transacción (si el ``COMMIT`` falla, ni resultado ni entrada).

Solo datos generados.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from vigia_contracts.models.common import UUID, Timestamp
from vigia_contracts.models.observability_event import ObservabilityEventRecord

from tests.factories import uuid7
from tests.identity_db import MigratedDatabase, migrated_database, seed_identity
from tests.integration.conftest import PostgresEndpoint
from tests.properties.coverage_strategies import NODES, Scenario, at, coverage_inputs, ms_of
from tests.writer_support import Fault, WriterEnvironment, unit_context, writer_environment
from vigia_platform.ledger.application.coverage import (
    CoverageInputInvalid,
    CoveragePeriod,
    CoverageService,
    CoverageZoneNotFound,
    PeriodTooLong,
)
from vigia_platform.ledger.application.writer import Receipt
from vigia_platform.ledger.domain.coverage import (
    AssignmentInput,
    CommunicationInput,
    CommunicationState,
    CoverageInputs,
    CoverageState,
    EventPhase,
    ObservabilityInput,
    Subject,
    compose_timeline,
    state_at,
)
from vigia_platform.ledger.registry import ChainLevel, ContentModel, RecordType
from vigia_platform.shared.context import (
    Actor,
    ActorKind,
    ActorUnit,
    AllowedScope,
    ContextAbsent,
    ContextOrigin,
    Role,
    ScopeContext,
    ScopeLevel,
    _seal_scope_context,
)
from vigia_platform.shared.db import TemporarilyUnavailable

pytestmark = pytest.mark.integration

MS = timedelta(milliseconds=1)

# --- Tipos de U-03 que lee la cobertura (forma de prueba) -----------------------------------------


class GateStateChanged(ContentModel):
    """``{zone_id, gate, status, resulting_mode}`` de U-03 (sin texto libre)."""

    zone_id: UUID
    plant_id: UUID
    gate: Literal["mounting", "use"]
    status: Literal["pending", "approved", "revoked"]
    resulting_mode: Literal["no_capture", "commissioning", "productive"]


class NodeCommunicationStateChanged(ContentModel):
    """``{node_id, state, since, last_heartbeat_at}`` de U-03."""

    node_id: UUID
    plant_id: UUID
    state: Literal["unknown", "reachable", "mute"]
    since: Timestamp
    last_heartbeat_at: Timestamp | None = None


COVERAGE_TYPES = (
    RecordType(
        record_type="observability_event_received",
        writer_unit=ActorUnit.U03,
        chain_level=ChainLevel.PLANT,
        schema_version=1,
        content_model=ObservabilityEventRecord,
        source_key_path="/event_id",
        free_text_paths=(
            "/contract_version",
            "/software_version",
            "/node_time/clock/source",
            "/evidence[*]/storage_key",
        ),
        evidence_paths=("/evidence[*]",),
    ),
    RecordType(
        record_type="gate_state_changed",
        writer_unit=ActorUnit.U03,
        chain_level=ChainLevel.PLANT,
        schema_version=1,
        content_model=GateStateChanged,
    ),
    RecordType(
        record_type="node_communication_state_changed",
        writer_unit=ActorUnit.U03,
        chain_level=ChainLevel.PLANT,
        schema_version=1,
        content_model=NodeCommunicationStateChanged,
    ),
)


# --- Entorno ----------------------------------------------------------------------------------


@dataclass
class Environment:
    env: WriterEnvironment
    coverage: CoverageService
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    user_id: uuid.UUID
    other_organization_id: uuid.UUID
    other_plant_id: uuid.UUID
    """Otra planta de la misma organización."""

    def run(self, awaitable: Any) -> Any:
        return self.env.loop.run(awaitable)

    @property
    def migrated(self) -> MigratedDatabase:
        return self.env.migrated


@pytest.fixture(scope="module")
def environment(postgres_endpoint: PostgresEndpoint) -> Iterator[Environment]:
    with (
        migrated_database(postgres_endpoint, "coverage_port") as migrated,
        writer_environment(migrated, extra_types=COVERAGE_TYPES) as env,
    ):

        async def seed() -> Any:
            connection = await migrated.connect()
            try:
                return await seed_identity(connection)
            finally:
                await connection.close()

        identity = env.loop.run(seed())
        tenant = identity.a
        yield Environment(
            env=env,
            coverage=CoverageService(database=env.database, audit=env.audit),
            organization_id=tenant.organization_id,
            plant_id=tenant.plants[0].plant_id,
            user_id=tenant.user_id,
            other_organization_id=identity.b.organization_id,
            other_plant_id=tenant.plants[1].plant_id,
        )


def scoped(organization_id: uuid.UUID, scopes: Sequence[AllowedScope]) -> ScopeContext:
    actor = Actor(
        kind=ActorKind.USER,
        id=uuid.uuid4(),
        display_name_snapshot="Coordinación SST sintética",
        unit=ActorUnit.U02,
        role_in_use=Role.COORDINATOR_SST,
    )
    return _seal_scope_context(
        organization_id=organization_id,
        actor=actor,
        origin=ContextOrigin.SESSION,
        allowed_scopes=list(scopes),
        correlation_id=uuid7(),
        session_id_hash=hashlib.sha256(os.urandom(8)).hexdigest(),
    )


def whole(environment: Environment) -> ScopeContext:
    return scoped(
        environment.organization_id,
        [AllowedScope(ScopeLevel.ORGANIZATION, environment.organization_id, Role.ADMINISTRATOR)],
    )


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="milliseconds") + "Z"


# --- Escritura de un escenario ----------------------------------------------------------------


@dataclass(frozen=True)
class Zone:
    zone_id: uuid.UUID
    nodes: tuple[uuid.UUID, uuid.UUID]


async def _insert_zone(environment: Environment, plant_id: uuid.UUID | None = None) -> Zone:
    plant = plant_id or environment.plant_id
    zone = Zone(uuid.uuid4(), (uuid.uuid4(), uuid.uuid4()))
    connection = await environment.migrated.connect()
    try:
        await connection.execute(
            "INSERT INTO identity.zone"
            " (zone_id, organization_id, plant_id, code, name, created_at, created_by)"
            " VALUES ($1, $2, $3, $4, 'Zona de cobertura sintética', $5, $6)",
            zone.zone_id,
            environment.organization_id,
            plant,
            f"ZC-{zone.zone_id.hex[:8].upper()}",
            datetime(2026, 1, 1, tzinfo=UTC),
            environment.user_id,
        )
        for node_id in zone.nodes:
            await connection.execute(
                "INSERT INTO identity.node_identity"
                " (node_id, organization_id, plant_id, code, status, created_at)"
                " VALUES ($1, $2, $3, $4, 'enrolled', $5)",
                node_id,
                environment.organization_id,
                plant,
                f"NC-{node_id.hex[:8].upper()}",
                datetime(2026, 1, 1, tzinfo=UTC),
            )
    finally:
        await connection.close()
    return zone


async def _insert_assignments(
    environment: Environment, zone: Zone, rows: Sequence[AssignmentInput]
) -> None:
    connection = await environment.migrated.connect()
    try:
        for row in rows:
            await connection.execute(
                "INSERT INTO identity.zone_node_assignment (assignment_id, organization_id,"
                " plant_id, zone_id, node_id, assigned_at, unassigned_at, assigned_by)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                row.assignment_id,
                environment.organization_id,
                environment.plant_id,
                zone.zone_id,
                row.node_id,
                row.assigned_at,
                row.unassigned_at,
                environment.user_id,
            )
    finally:
        await connection.close()


async def _project_communication(
    environment: Environment, changes: Sequence[CommunicationInput]
) -> None:
    """``CommunicationState``: la última declaración de cada nodo, por ``(since, record_id)``."""
    latest: dict[uuid.UUID, CommunicationInput] = {}
    for change in changes:
        current = latest.get(change.node_id)
        if current is None or (ms_of(change.since), change.record_id.int) > (
            ms_of(current.since),
            current.record_id.int,
        ):
            latest[change.node_id] = change
    connection = await environment.migrated.connect()
    try:
        for change in latest.values():
            await connection.execute(
                "INSERT INTO ledger.communication_state (node_id, organization_id, plant_id,"
                " state, since, last_heartbeat_at, source_record_id)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7)",
                change.node_id,
                environment.organization_id,
                environment.plant_id,
                change.state.value,
                change.since,
                change.last_heartbeat_at,
                change.record_id,
            )
    finally:
        await connection.close()


def _write(
    environment: Environment,
    record_type: str,
    document: dict[str, Any],
    occurred_at: datetime | None = None,
) -> uuid.UUID:
    context = unit_context(environment.organization_id, ActorUnit.U03, kind=ActorKind.SYSTEM)
    receipt = environment.run(
        environment.env.writer.write(context, record_type, document, occurred_at=occurred_at)
    )
    assert isinstance(receipt, Receipt), receipt
    return receipt.record_id


def _event_document(
    environment: Environment, zone: Zone, event: ObservabilityInput
) -> dict[str, Any]:
    subject: dict[str, Any] = {"kind": event.subject.kind}
    if event.subject.camera_id is not None:
        subject["camera_id"] = str(event.subject.camera_id)
    ended = event.ended_at or event.started_at
    document: dict[str, Any] = {
        "event_id": str(event.event_id),
        "contract_version": "1.0.0",
        "organization_id": str(environment.organization_id),
        "plant_id": str(environment.plant_id),
        "zone_id": str(zone.zone_id),
        "node_id": str(zone.nodes[0]),
        "subject": subject,
        "phase": event.phase.value,
        "state": event.state.value,
        "causes": list(event.causes),
        "started_at": _stamp(event.started_at),
        "node_time": {
            "started_at": _stamp(event.started_at),
            "ended_at": _stamp(ended),
            "clock": {
                "synchronized": True,
                "offset_ms": event.clock_offset_ms or 0,
                "source": "ntp.local",
            },
        },
        "evidence": [],
        "software_version": "1.0.0",
    }
    if event.phase is EventPhase.CLOSED:
        document["ended_at"] = _stamp(ended)
        document["opened_event_id"] = str(event.opened_event_id)
    return document


def _non_overlapping(rows: Sequence[AssignmentInput], unit: int) -> list[AssignmentInput]:
    """La base admite un solo nodo por zona y periodo: se quedan las que no se solapan."""
    kept: list[AssignmentInput] = []
    for row in sorted(rows, key=lambda r: r.assigned_at):
        end = row.unassigned_at or row.assigned_at + 60 * unit * MS
        if kept and row.assigned_at < (kept[-1].unassigned_at or row.assigned_at):
            continue
        kept.append(replace(row, unassigned_at=end))
    return kept


def store(environment: Environment, scenario: Scenario) -> tuple[Zone, CoverageInputs]:
    """Escribe el escenario en una zona nueva y devuelve las entradas tal como quedaron."""
    zone: Zone = environment.run(_insert_zone(environment))
    nodes = dict(zip(NODES, zone.nodes, strict=True))
    inputs = scenario.inputs

    # Identificadores nuevos: Hypothesis repite los del generador entre ejemplos.
    assignments = [
        replace(row, assignment_id=uuid.uuid4(), node_id=nodes[row.node_id])
        for row in _non_overlapping(inputs.assignments, scenario.unit)
    ]
    environment.run(_insert_assignments(environment, zone, assignments))

    communication = []
    for change in inputs.communication:
        node_id = nodes[change.node_id]
        document: dict[str, Any] = {
            "node_id": str(node_id),
            "plant_id": str(environment.plant_id),
            "state": change.state.value,
            "since": _stamp(change.since),
        }
        if change.last_heartbeat_at is not None:
            document["last_heartbeat_at"] = _stamp(change.last_heartbeat_at)
        record_id = _write(environment, "node_communication_state_changed", document)
        communication.append(replace(change, record_id=record_id, node_id=node_id))
    environment.run(_project_communication(environment, communication))

    gates = []
    for gate in inputs.gates:
        document = {
            "zone_id": str(zone.zone_id),
            "plant_id": str(environment.plant_id),
            "gate": "use",
            "status": "approved",
            "resulting_mode": gate.resulting_mode.value,
        }
        record_id = _write(environment, "gate_state_changed", document, occurred_at=gate.at)
        gates.append(replace(gate, record_id=record_id))

    # Identificadores de evento UUID v7 (el contrato los exige), con los cierres apuntando igual.
    event_ids = {event.event_id: uuid7() for event in inputs.observability}
    observability = []
    for event in inputs.observability:
        opener = event.opened_event_id
        stored = replace(
            event,
            event_id=event_ids[event.event_id],
            opened_event_id=None if opener is None else event_ids.get(opener, uuid7()),
            clock_offset_ms=event.clock_offset_ms or 0,
        )
        record_id = _write(
            environment, "observability_event_received", _event_document(environment, zone, stored)
        )
        observability.append(replace(stored, record_id=record_id))

    return zone, CoverageInputs(
        observability=tuple(observability),
        communication=tuple(communication),
        gates=tuple(gates),
        assignments=tuple(assignments),
    )


# --- Las cargas por índice dan lo mismo que todas las entradas ----------------------------------


@settings(max_examples=20)
@given(
    scenario=coverage_inputs(max_events=10),
    extra=st.lists(st.integers(min_value=0), max_size=4),
)
def test_port_equals_the_pure_composition_over_all_inputs(
    environment: Environment, scenario: Scenario, extra: list[int]
) -> None:
    zone, inputs = store(environment, scenario)
    context = whole(environment)
    timeline = environment.run(
        environment.coverage.linea_de_tiempo(context, zone.zone_id, scenario.period)
    )
    expected = compose_timeline(inputs, scenario.period)
    assert timeline.intervals == expected.intervals
    assert timeline.node_intervals == expected.node_intervals
    assert timeline.summary == expected.summary
    assert timeline.summary.total_ms == scenario.b - scenario.a
    assert (timeline.organization_id, timeline.plant_id, timeline.zone_id) == (
        environment.organization_id,
        environment.plant_id,
        zone.zone_id,
    )

    instants = {scenario.a, scenario.b - 1, scenario.a - 20 * scenario.unit}
    instants.update(ms_of(i.starts_at) for i in expected.intervals)
    instants.update(scenario.a + offset % (scenario.b - scenario.a) for offset in extra)
    for t in sorted(instants):
        status = environment.run(environment.coverage.estado_en(context, zone.zone_id, at(t)))
        assert status == state_at(inputs, at(t)), t


def test_estado_en_uses_the_projection_or_the_history(environment: Environment) -> None:
    zone: Zone = environment.run(_insert_zone(environment))
    node = zone.nodes[0]
    environment.run(
        _insert_assignments(environment, zone, [AssignmentInput(uuid.uuid4(), node, at(0))])
    )
    _write(
        environment,
        "gate_state_changed",
        {
            "zone_id": str(zone.zone_id),
            "plant_id": str(environment.plant_id),
            "gate": "use",
            "status": "approved",
            "resulting_mode": "productive",
        },
        occurred_at=at(0),
    )
    changes = []
    for state, since, heartbeat in (
        ("reachable", 0, None),
        ("mute", 5_000, 3_000),
        ("reachable", 9_000, None),
    ):
        document: dict[str, Any] = {
            "node_id": str(node),
            "plant_id": str(environment.plant_id),
            "state": state,
            "since": _stamp(at(since)),
        }
        if heartbeat is not None:
            document["last_heartbeat_at"] = _stamp(at(heartbeat))
        record_id = _write(environment, "node_communication_state_changed", document)
        changes.append(
            CommunicationInput(
                record_id,
                node,
                CommunicationState(state),
                at(since),
                None if heartbeat is None else at(heartbeat),
            )
        )
    environment.run(_project_communication(environment, changes))
    opened = ObservabilityInput(
        uuid.uuid4(),
        uuid7(),
        _zone_subject(),
        EventPhase.OPENED,
        CoverageState.OBSERVABLE,
        (),
        at(0),
        clock_offset_ms=0,
    )
    _write(environment, "observability_event_received", _event_document(environment, zone, opened))
    context = whole(environment)
    expected = {
        1_000: ("observable", (), None),
        3_000: ("not_observable", ("no_communication",), changes[1].record_id),
        8_999: ("not_observable", ("no_communication",), changes[1].record_id),
        9_000: ("observable", (), None),  # la proyección
        20_000: ("observable", (), None),
    }
    for t, (state, causes, source) in expected.items():
        status = environment.run(environment.coverage.estado_en(context, zone.zone_id, at(t)))
        assert (status.state.value, status.causes) == (state, causes), t
        if source is not None:
            assert source in status.source_record_ids


def _zone_subject() -> Subject:
    return Subject("zone")


# --- Tope, alcance y auditoría ------------------------------------------------------------------


async def _coverage_entries(migrated: MigratedDatabase, organization_id: uuid.UUID) -> list[Any]:
    connection = await migrated.connect()
    try:
        rows = await connection.fetch(
            "SELECT operation, filters_json, result_count, scope_plant_id, scope_zone_id,"
            " actor_id, correlation_id FROM shared.audit_entry"
            " WHERE organization_id = $1 AND operation = 'coverage_read' ORDER BY chain_sequence",
            organization_id,
        )
    finally:
        await connection.close()
    return list(rows)


def entries(environment: Environment, organization_id: uuid.UUID | None = None) -> list[Any]:
    rows: list[Any] = environment.run(
        _coverage_entries(environment.migrated, organization_id or environment.organization_id)
    )
    return rows


PERIOD = CoveragePeriod(at(0), at(60_000))


def test_a_32_day_period_is_period_too_long_without_reading_or_auditing(
    environment: Environment,
) -> None:
    zone: Zone = environment.run(_insert_zone(environment))
    context = whole(environment)
    before = len(entries(environment))
    start = datetime(2026, 9, 1, tzinfo=UTC)
    for end in (start + timedelta(days=32), start + timedelta(days=31, milliseconds=1)):
        with pytest.raises(PeriodTooLong) as raised:
            environment.run(
                environment.coverage.linea_de_tiempo(
                    context, zone.zone_id, CoveragePeriod(start, end)
                )
            )
        assert raised.value.code == "period_too_long"
    timeline = environment.run(
        environment.coverage.linea_de_tiempo(
            context, zone.zone_id, CoveragePeriod(start, start + timedelta(days=31))
        )
    )
    assert timeline.summary.total_ms == 31 * 24 * 3600 * 1000
    assert timeline.summary.never_reported_ms == timeline.summary.total_ms
    assert len(entries(environment)) == before + 1


def test_invalid_queries_neither_read_nor_audit(environment: Environment) -> None:
    zone: Zone = environment.run(_insert_zone(environment))
    context = whole(environment)
    before = len(entries(environment))
    naive = datetime(2026, 9, 1)  # noqa: DTZ001 - entrada hostil sin zona a propósito
    for call in (
        environment.coverage.linea_de_tiempo(context, zone.zone_id, CoveragePeriod(at(5), at(5))),
        environment.coverage.linea_de_tiempo(
            context, zone.zone_id, CoveragePeriod(at(0), at(0) + timedelta(microseconds=10))
        ),
        environment.coverage.linea_de_tiempo(context, str(zone.zone_id), PERIOD),  # type: ignore[arg-type]
        environment.coverage.linea_de_tiempo(context, zone.zone_id, (at(0), at(1))),  # type: ignore[arg-type]
        environment.coverage.estado_en(context, zone.zone_id, naive),
        environment.coverage.estado_en(context, None, at(0)),  # type: ignore[arg-type]
    ):
        with pytest.raises(CoverageInputInvalid):
            environment.run(call)
    for call in (
        environment.coverage.linea_de_tiempo(None, zone.zone_id, PERIOD),  # type: ignore[arg-type]
        environment.coverage.estado_en(None, zone.zone_id, at(0)),  # type: ignore[arg-type]
    ):
        with pytest.raises(ContextAbsent):
            environment.run(call)
    assert len(entries(environment)) == before


def test_zone_out_of_scope_is_not_found_and_each_query_is_audited_once(
    environment: Environment,
) -> None:
    zone: Zone = environment.run(_insert_zone(environment))
    sibling: Zone = environment.run(_insert_zone(environment))
    elsewhere: Zone = environment.run(_insert_zone(environment, environment.other_plant_id))
    organization = environment.organization_id
    zone_reader = scoped(organization, [AllowedScope(ScopeLevel.ZONE, zone.zone_id, Role.COPASST)])
    plant_reader = scoped(
        organization, [AllowedScope(ScopeLevel.PLANT, environment.plant_id, Role.PLANT_MANAGER)]
    )
    foreign = scoped(
        environment.other_organization_id,
        [AllowedScope(ScopeLevel.ORGANIZATION, organization, Role.ADMINISTRATOR)],
    )
    before = len(entries(environment))

    visible = [
        (zone_reader, zone.zone_id),
        (plant_reader, zone.zone_id),
        (plant_reader, sibling.zone_id),
        (whole(environment), elsewhere.zone_id),
    ]
    hidden = [
        (zone_reader, sibling.zone_id),
        (zone_reader, elsewhere.zone_id),
        (plant_reader, elsewhere.zone_id),
        (whole(environment), uuid.uuid4()),
        (scoped(organization, []), zone.zone_id),
    ]
    for context, zone_id in visible:
        timeline = environment.run(environment.coverage.linea_de_tiempo(context, zone_id, PERIOD))
        assert timeline.zone_id == zone_id
        environment.run(environment.coverage.estado_en(context, zone_id, at(1)))
    for context, zone_id in hidden:
        with pytest.raises(CoverageZoneNotFound) as raised:
            environment.run(environment.coverage.linea_de_tiempo(context, zone_id, PERIOD))
        assert raised.value.code == "not_found"
        with pytest.raises(CoverageZoneNotFound):
            environment.run(environment.coverage.estado_en(context, zone_id, at(1)))
    foreign_before = len(entries(environment, environment.other_organization_id))
    with pytest.raises(CoverageZoneNotFound):
        environment.run(environment.coverage.linea_de_tiempo(foreign, zone.zone_id, PERIOD))

    new = entries(environment)[before:]
    assert len(new) == 2 * (len(visible) + len(hidden))
    first = new[0]
    assert json.loads(first["filters_json"]) == {
        "zone_id": str(zone.zone_id),
        "period_from": _stamp(PERIOD.start),
        "period_to": _stamp(PERIOD.end),
    }
    assert first["result_count"] == 1
    assert (first["scope_plant_id"], first["scope_zone_id"]) == (environment.plant_id, zone.zone_id)
    assert first["actor_id"] == zone_reader.actor.id
    assert first["correlation_id"] == zone_reader.correlation_id
    second = new[1]
    assert json.loads(second["filters_json"]) == {
        "zone_id": str(zone.zone_id),
        "instant": _stamp(at(1)),
    }
    assert second["result_count"] == 1
    # Fuera de alcance: una entrada con 0 resultados que no revela dónde está la zona.
    for entry in new[2 * len(visible) :]:
        assert entry["result_count"] == 0
        assert (entry["scope_plant_id"], entry["scope_zone_id"]) == (None, None)
    # El contexto de otra organización audita en su propia cadena, nunca en la de la zona.
    assert len(entries(environment, environment.other_organization_id)) == foreign_before + 1


def test_the_audit_entry_is_in_the_same_transaction_as_the_reads(
    environment: Environment,
) -> None:
    zone: Zone = environment.run(_insert_zone(environment))
    context = whole(environment)
    before = len(entries(environment))
    environment.env.database.next_fault = Fault(commit=True)
    with pytest.raises(TemporarilyUnavailable):
        environment.run(environment.coverage.linea_de_tiempo(context, zone.zone_id, PERIOD))
    environment.env.database.next_fault = Fault(commit=True)
    with pytest.raises(TemporarilyUnavailable):
        environment.run(environment.coverage.estado_en(context, zone.zone_id, at(0)))
    assert len(entries(environment)) == before
    # Y sin fallo: exactamente una entrada por consulta.
    environment.run(environment.coverage.linea_de_tiempo(context, zone.zone_id, PERIOD))
    assert len(entries(environment)) == before + 1


def test_gate_modes_and_assignment_gaps_through_the_database(environment: Environment) -> None:
    zone: Zone = environment.run(_insert_zone(environment))
    first, second = zone.nodes
    environment.run(
        _insert_assignments(
            environment,
            zone,
            [
                AssignmentInput(uuid.uuid4(), first, at(1_000), at(4_000)),
                AssignmentInput(uuid.uuid4(), second, at(5_000)),
            ],
        )
    )
    for node in (first, second):
        _write(
            environment,
            "node_communication_state_changed",
            {
                "node_id": str(node),
                "plant_id": str(environment.plant_id),
                "state": "reachable",
                "since": _stamp(at(0)),
            },
        )
    for mode, moment in (("commissioning", 0), ("productive", 2_000)):
        _write(
            environment,
            "gate_state_changed",
            {
                "zone_id": str(zone.zone_id),
                "plant_id": str(environment.plant_id),
                "gate": "use",
                "status": "approved",
                "resulting_mode": mode,
            },
            occurred_at=at(moment),
        )
    event = ObservabilityInput(
        uuid.uuid4(),
        uuid7(),
        _zone_subject(),
        EventPhase.OPENED,
        CoverageState.DEGRADED,
        ("focus",),
        at(0),
        clock_offset_ms=0,
    )
    _write(environment, "observability_event_received", _event_document(environment, zone, event))
    timeline = environment.run(
        environment.coverage.linea_de_tiempo(
            whole(environment), zone.zone_id, CoveragePeriod(at(0), at(8_000))
        )
    )
    assert [
        (ms_of(i.starts_at), ms_of(i.ends_at), i.state.value, i.causes) for i in timeline.intervals
    ] == [
        (0, 1_000, "not_observable", ("never_reported",)),
        (1_000, 2_000, "not_observable", ("zone_not_active",)),
        (2_000, 4_000, "degraded", ("focus",)),
        (4_000, 5_000, "not_observable", ("never_reported",)),
        (5_000, 8_000, "degraded", ("focus",)),
    ]
    assert timeline.summary.degraded_ms == 5_000
    assert timeline.summary.total_ms == 8_000
