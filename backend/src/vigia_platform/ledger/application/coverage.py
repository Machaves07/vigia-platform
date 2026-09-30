"""``CoveragePort``: línea de tiempo de cobertura de una zona y su estado en un instante.

LC-NUC-17 ``ledger.coverage``.

Operaciones del puerto (business-logic-model §7 y §10.1; PAT-NUC-REN-04):

- ``linea_de_tiempo(context, zone_id, period)``: la partición exacta de ``[from, to)`` en tramos
  de estado compuesto, la capa ``node_report`` sin componer y el resumen. El periodo tiene como
  mucho **31 días** (``PeriodTooLong``, ``period_too_long``): U-04 y U-05 piden por tramos y
  concatenan (PR-NUC-51).
- ``estado_en(context, zone_id, instant)``: el compuesto en un instante (lo que U-04 adjunta a
  cada hallazgo por su ``node_time.started_at``), con lecturas puntuales por índice: la
  asignación vigente, la última compuerta, la comunicación del nodo (``CommunicationState`` o, si
  el instante es anterior a ella, la última declaración que ya regía) y el último tramo ``zone``
  del nodo.

La composición es la función pura de ``ledger.domain.coverage``; aquí solo se cargan sus entradas
de ``ledger.ledger_record`` (``observability_event_received``, ``gate_state_changed`` y
``node_communication_state_changed``, con los campos del contrato y de U-03), de
``identity.zone_node_assignment`` y de ``ledger.communication_state``. De la historia se lee solo
lo que puede influir en el periodo: por sujeto, el último inicio de tramo anterior a ``from`` y
los del periodo con sus cierres; por capa de plataforma, la última declaración anterior y las del
periodo. Sin caché: un cierre huérfano tardío puede cambiar un periodo ya consultado y la verdad
es siempre el expediente.

**Alcance.** La seguridad a nivel de fila limita a la organización del contexto; además la zona
tiene que estar en ``allowed_scopes`` (la organización entera, su planta o ella misma). Una zona
que no existe o está fuera del alcance lanza ``CoverageZoneNotFound`` (``not_found``, nunca
``forbidden``). Qué **rol** puede consultar la cobertura lo decide ``authorize`` en la ruta
(TASK-137).

**Auditoría** (BR-NUC-59): cada consulta válida escribe exactamente una entrada
``coverage_read`` en la misma transacción que las lecturas, con la zona y el periodo o el
instante pedidos; también la de una zona no encontrada (``result_count`` 0, sin planta ni zona en
el alcance de la entrada). Una consulta inválida (``CoverageInputInvalid``) o demasiado larga
(``PeriodTooLong``) no consulta ni audita nada.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from pydantic import JsonValue
from sqlalchemy import text
from sqlalchemy.engine import Row

from vigia_platform.ledger.application.audit_writer import AuditOperation, AuditWriter
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.ledger.domain.coverage import (
    AssignmentInput,
    CommunicationInput,
    CommunicationState,
    CoverageInputInvalid,
    CoverageInputs,
    CoverageInterval,
    CoveragePeriod,
    CoverageStatus,
    CoverageSummary,
    EventPhase,
    GateInput,
    ObservabilityInput,
    PeriodTooLong,
    Subject,
    ZoneMode,
    check_period,
    compose_timeline,
    state_at,
)
from vigia_platform.shared.context import ContextAbsent, ScopeContext, ScopeLevel, repository
from vigia_platform.shared.db import Transaction

__all__ = [
    "CoverageInputInvalid",
    "CoveragePeriod",
    "CoveragePort",
    "CoverageService",
    "CoverageTimeline",
    "CoverageZoneNotFound",
    "PeriodTooLong",
]

OBSERVABILITY_TYPE: Final = "observability_event_received"
GATE_TYPE: Final = "gate_state_changed"
COMMUNICATION_TYPE: Final = "node_communication_state_changed"


class CoverageZoneNotFound(LookupError):
    """La zona no existe o está fuera del alcance: la interfaz responde ``not_found``."""

    code: Final = "not_found"

    def __init__(self) -> None:
        super().__init__("zona no encontrada")


@dataclass(frozen=True, slots=True)
class CoverageTimeline:
    """``CoverageTimeline`` (domain-entities §3.8)."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    period: CoveragePeriod
    intervals: tuple[CoverageInterval, ...]
    node_intervals: tuple[CoverageInterval, ...]
    summary: CoverageSummary


class CoveragePort(Protocol):
    """El puerto que consumen U-04 y U-05 (business-logic-model §10.1)."""

    async def linea_de_tiempo(
        self, context: ScopeContext, zone_id: uuid.UUID, period: CoveragePeriod
    ) -> CoverageTimeline: ...

    async def estado_en(
        self, context: ScopeContext, zone_id: uuid.UUID, instant: datetime
    ) -> CoverageStatus: ...


# --- Sentencias -------------------------------------------------------------------------------
#
# Las marcas se truncan al milisegundo en la base igual que en la composición, para que los
# límites y los desempates por ``(marca, identificador)`` coincidan.

_ZONE: Final = text(
    "SELECT z.plant_id FROM identity.zone AS z"
    " WHERE z.organization_id = :organization_id AND z.zone_id = :zone_id"
)

_ASSIGNMENTS_IN_PERIOD: Final = text(
    "SELECT a.assignment_id, a.node_id, a.assigned_at, a.unassigned_at"
    " FROM identity.zone_node_assignment AS a"
    " WHERE a.organization_id = :organization_id AND a.zone_id = :zone_id"
    " AND date_trunc('milliseconds', a.assigned_at) < CAST(:period_end AS timestamptz)"
    " AND (a.unassigned_at IS NULL"
    " OR date_trunc('milliseconds', a.unassigned_at) > CAST(:period_start AS timestamptz))"
)

_ASSIGNMENT_AT: Final = text(
    "SELECT a.assignment_id, a.node_id, a.assigned_at, a.unassigned_at"
    " FROM identity.zone_node_assignment AS a"
    " WHERE a.organization_id = :organization_id AND a.zone_id = :zone_id"
    " AND date_trunc('milliseconds', a.assigned_at) <= CAST(:instant AS timestamptz)"
    " AND (a.unassigned_at IS NULL"
    " OR date_trunc('milliseconds', a.unassigned_at) > CAST(:instant AS timestamptz))"
    " ORDER BY date_trunc('milliseconds', a.assigned_at) DESC, a.assignment_id DESC"
    " LIMIT 1"
)

_GATES_IN_PERIOD: Final = text(
    "WITH g AS ("
    " SELECT r.record_id, r.content_json ->> 'resulting_mode' AS resulting_mode,"
    " date_trunc('milliseconds', coalesce(r.occurred_at, r.received_at)) AS changed_at"
    " FROM ledger.ledger_record AS r"
    " WHERE r.organization_id = :organization_id AND r.scope_zone_id = :zone_id"
    " AND r.record_type = 'gate_state_changed')"
    " (SELECT record_id, resulting_mode, changed_at FROM g"
    " WHERE changed_at <= CAST(:period_start AS timestamptz)"
    " ORDER BY changed_at DESC, record_id DESC LIMIT 1)"
    " UNION ALL"
    " (SELECT record_id, resulting_mode, changed_at FROM g"
    " WHERE changed_at > CAST(:period_start AS timestamptz)"
    " AND changed_at < CAST(:period_end AS timestamptz))"
)

_GATE_AT: Final = text(
    "SELECT r.record_id, r.content_json ->> 'resulting_mode' AS resulting_mode,"
    " date_trunc('milliseconds', coalesce(r.occurred_at, r.received_at)) AS changed_at"
    " FROM ledger.ledger_record AS r"
    " WHERE r.organization_id = :organization_id AND r.scope_zone_id = :zone_id"
    " AND r.record_type = 'gate_state_changed'"
    " AND date_trunc('milliseconds', coalesce(r.occurred_at, r.received_at))"
    " <= CAST(:instant AS timestamptz)"
    " ORDER BY changed_at DESC, r.record_id DESC LIMIT 1"
)

_COMMUNICATION_IN_PERIOD: Final = text(
    "WITH c AS ("
    " SELECT r.record_id, r.scope_node_id AS node_id, r.content_json ->> 'state' AS state,"
    " date_trunc('milliseconds', CAST(r.content_json ->> 'since' AS timestamptz)) AS since,"
    " date_trunc('milliseconds', CAST(r.content_json ->> 'last_heartbeat_at' AS timestamptz))"
    " AS last_heartbeat_at"
    " FROM ledger.ledger_record AS r"
    " WHERE r.organization_id = :organization_id AND r.scope_zone_id IS NULL"
    " AND r.record_type = 'node_communication_state_changed'"
    " AND r.scope_node_id = ANY(CAST(:node_ids AS uuid[]))),"
    " e AS (SELECT c.*, CASE WHEN c.state = 'mute' AND c.last_heartbeat_at IS NOT NULL"
    " THEN c.last_heartbeat_at ELSE c.since END AS effective FROM c)"
    " (SELECT DISTINCT ON (node_id) record_id, node_id, state, since, last_heartbeat_at FROM e"
    " WHERE effective <= CAST(:period_start AS timestamptz)"
    " ORDER BY node_id, since DESC, record_id DESC)"
    " UNION ALL"
    " (SELECT record_id, node_id, state, since, last_heartbeat_at FROM e"
    " WHERE effective > CAST(:period_start AS timestamptz)"
    " AND effective < CAST(:period_end AS timestamptz))"
)

_COMMUNICATION_STATE: Final = text(
    "SELECT s.source_record_id AS record_id, s.node_id, s.state, s.since, s.last_heartbeat_at"
    " FROM ledger.communication_state AS s"
    " WHERE s.organization_id = :organization_id AND s.node_id = :node_id"
)

_COMMUNICATION_AT: Final = text(
    "WITH c AS ("
    " SELECT r.record_id, r.scope_node_id AS node_id, r.content_json ->> 'state' AS state,"
    " date_trunc('milliseconds', CAST(r.content_json ->> 'since' AS timestamptz)) AS since,"
    " date_trunc('milliseconds', CAST(r.content_json ->> 'last_heartbeat_at' AS timestamptz))"
    " AS last_heartbeat_at"
    " FROM ledger.ledger_record AS r"
    " WHERE r.organization_id = :organization_id AND r.scope_zone_id IS NULL"
    " AND r.record_type = 'node_communication_state_changed'"
    " AND r.scope_node_id = :node_id)"
    " SELECT record_id, node_id, state, since, last_heartbeat_at FROM c"
    " WHERE CASE WHEN state = 'mute' AND last_heartbeat_at IS NOT NULL"
    " THEN last_heartbeat_at ELSE since END <= CAST(:instant AS timestamptz)"
    " ORDER BY since DESC, record_id DESC LIMIT 1"
)

# Eventos de la zona con los campos del contrato; ``starter`` marca las aperturas y los cierres
# huérfanos (sin apertura de la zona con ese ``event_id``), que son los que inician un tramo.
_OBSERVABILITY_IN_PERIOD: Final = text(
    "WITH ev AS ("
    " SELECT r.record_id,"
    " CAST(r.content_json ->> 'event_id' AS uuid) AS event_id,"
    " r.content_json -> 'subject' ->> 'kind' AS subject_kind,"
    " CAST(r.content_json -> 'subject' ->> 'camera_id' AS uuid) AS camera_id,"
    " CAST(r.content_json -> 'subject' ->> 'signal_id' AS uuid) AS signal_id,"
    " r.content_json ->> 'phase' AS phase, r.content_json ->> 'state' AS state,"
    " ARRAY(SELECT jsonb_array_elements_text(r.content_json -> 'causes')) AS causes,"
    " CAST(r.content_json ->> 'started_at' AS timestamptz) AS started_at,"
    " CAST(r.content_json ->> 'ended_at' AS timestamptz) AS ended_at,"
    " CAST(r.content_json ->> 'opened_event_id' AS uuid) AS opened_event_id,"
    " CAST(r.content_json -> 'node_time' -> 'clock' ->> 'offset_ms' AS bigint)"
    " AS clock_offset_ms"
    " FROM ledger.ledger_record AS r"
    " WHERE r.organization_id = :organization_id AND r.scope_zone_id = :zone_id"
    " AND r.record_type = 'observability_event_received'),"
    " marked AS (SELECT ev.*, (ev.phase = 'opened' OR NOT EXISTS ("
    " SELECT 1 FROM ev AS o WHERE o.phase = 'opened' AND o.event_id = ev.opened_event_id))"
    " AS starter FROM ev),"
    " chosen AS ("
    " (SELECT * FROM marked WHERE starter"
    " AND started_at >= CAST(:period_start AS timestamptz)"
    " AND started_at < CAST(:period_end AS timestamptz))"
    " UNION ALL"
    " (SELECT DISTINCT ON (subject_kind, camera_id, signal_id) * FROM marked"
    " WHERE starter AND started_at < CAST(:period_start AS timestamptz)"
    " ORDER BY subject_kind, camera_id, signal_id, started_at DESC, event_id DESC))"
    " SELECT record_id, event_id, subject_kind, camera_id, signal_id, phase, state, causes,"
    " started_at, ended_at, opened_event_id, clock_offset_ms FROM chosen"
    " UNION ALL"
    " SELECT record_id, event_id, subject_kind, camera_id, signal_id, phase, state, causes,"
    " started_at, ended_at, opened_event_id, clock_offset_ms FROM marked"
    " WHERE NOT starter AND opened_event_id IN ("
    " SELECT event_id FROM chosen WHERE phase = 'opened')"
)

_ZONE_STARTER_AT: Final = text(
    "WITH ev AS ("
    " SELECT r.record_id,"
    " CAST(r.content_json ->> 'event_id' AS uuid) AS event_id,"
    " r.content_json -> 'subject' ->> 'kind' AS subject_kind,"
    " CAST(r.content_json -> 'subject' ->> 'camera_id' AS uuid) AS camera_id,"
    " CAST(r.content_json -> 'subject' ->> 'signal_id' AS uuid) AS signal_id,"
    " r.content_json ->> 'phase' AS phase, r.content_json ->> 'state' AS state,"
    " ARRAY(SELECT jsonb_array_elements_text(r.content_json -> 'causes')) AS causes,"
    " CAST(r.content_json ->> 'started_at' AS timestamptz) AS started_at,"
    " CAST(r.content_json ->> 'ended_at' AS timestamptz) AS ended_at,"
    " CAST(r.content_json ->> 'opened_event_id' AS uuid) AS opened_event_id,"
    " CAST(r.content_json -> 'node_time' -> 'clock' ->> 'offset_ms' AS bigint)"
    " AS clock_offset_ms"
    " FROM ledger.ledger_record AS r"
    " WHERE r.organization_id = :organization_id AND r.scope_zone_id = :zone_id"
    " AND r.record_type = 'observability_event_received'),"
    " chosen AS (SELECT * FROM ev WHERE subject_kind = 'zone'"
    " AND date_trunc('milliseconds', started_at) <= CAST(:instant AS timestamptz)"
    " AND (phase = 'opened' OR NOT EXISTS (SELECT 1 FROM ev AS o"
    " WHERE o.phase = 'opened' AND o.event_id = ev.opened_event_id))"
    " ORDER BY date_trunc('milliseconds', started_at) DESC, event_id DESC LIMIT 1)"
    " SELECT * FROM chosen"
    " UNION ALL"
    " SELECT * FROM ev WHERE phase = 'closed' AND opened_event_id IN ("
    " SELECT event_id FROM chosen WHERE phase = 'opened')"
)

_FIRST_REPORT: Final = text(
    "SELECT min(CAST(r.content_json ->> 'started_at' AS timestamptz)) AS first_report_at"
    " FROM ledger.ledger_record AS r"
    " WHERE r.organization_id = :organization_id AND r.scope_zone_id = :zone_id"
    " AND r.record_type = 'observability_event_received'"
)


# --- Conversión de filas ------------------------------------------------------------------------


def _id(value: uuid.UUID) -> uuid.UUID:
    """``uuid.UUID`` exacto (asyncpg devuelve una subclase propia)."""
    return uuid.UUID(int=value.int)


def _optional_id(value: uuid.UUID | None) -> uuid.UUID | None:
    return None if value is None else _id(value)


def _observability(row: Row[Any]) -> ObservabilityInput:
    return ObservabilityInput(
        record_id=_id(row.record_id),
        event_id=_id(row.event_id),
        subject=Subject(
            kind=row.subject_kind,
            camera_id=_optional_id(row.camera_id),
            signal_id=_optional_id(row.signal_id),
        ),
        phase=EventPhase(row.phase),
        state=row.state,
        causes=tuple(row.causes),
        started_at=row.started_at,
        ended_at=row.ended_at,
        opened_event_id=_optional_id(row.opened_event_id),
        clock_offset_ms=None if row.clock_offset_ms is None else int(row.clock_offset_ms),
    )


def _communication(row: Row[Any]) -> CommunicationInput:
    return CommunicationInput(
        record_id=_id(row.record_id),
        node_id=_id(row.node_id),
        state=CommunicationState(row.state),
        since=row.since,
        last_heartbeat_at=row.last_heartbeat_at,
    )


def _gate(row: Row[Any]) -> GateInput:
    return GateInput(
        record_id=_id(row.record_id), at=row.changed_at, resulting_mode=ZoneMode(row.resulting_mode)
    )


def _assignment(row: Row[Any]) -> AssignmentInput:
    return AssignmentInput(
        assignment_id=_id(row.assignment_id),
        node_id=_id(row.node_id),
        assigned_at=row.assigned_at,
        unassigned_at=row.unassigned_at,
    )


def _effective(change: CommunicationInput) -> datetime:
    if change.state is CommunicationState.MUTE and change.last_heartbeat_at is not None:
        return change.last_heartbeat_at
    return change.since


def _stamp(moment: datetime) -> str:
    """Marca en UTC con milisegundos y ``Z`` (la forma del contrato), para la auditoría."""
    return moment.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="milliseconds") + "Z"


def _require_context(context: object) -> ScopeContext:
    if not isinstance(context, ScopeContext):
        raise ContextAbsent()
    return context


def _zone_id(value: object) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise CoverageInputInvalid("zone_id debe ser uuid.UUID")
    return _id(value)


def _instant(value: object) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise CoverageInputInvalid("instant debe ser una marca con zona horaria")
    # Al milisegundo, como la composición.
    return value.replace(microsecond=value.microsecond - value.microsecond % 1000)


def _visible(context: ScopeContext, plant_id: uuid.UUID, zone_id: uuid.UUID) -> bool:
    for scope in context.allowed_scopes:
        if scope.scope_level is ScopeLevel.ORGANIZATION:
            if scope.scope_id == context.organization_id:
                return True
        elif scope.scope_level is ScopeLevel.PLANT:
            if scope.scope_id == plant_id:
                return True
        elif scope.scope_id == zone_id:
            return True
    return False


# --- El puerto ------------------------------------------------------------------------------------


@repository
class CoverageService:
    """``CoveragePort`` sobre PostgreSQL."""

    def __init__(self, *, database: LedgerDatabase, audit: AuditWriter) -> None:
        self._database = database
        self._audit = audit

    async def linea_de_tiempo(
        self, context: ScopeContext, zone_id: uuid.UUID, period: CoveragePeriod
    ) -> CoverageTimeline:
        """La línea de tiempo de la zona en ``period``; escribe una entrada ``coverage_read``."""
        context = _require_context(context)
        zone = _zone_id(zone_id)
        check_period(period)  # CoverageInputInvalid o PeriodTooLong antes de tocar la base
        filters: dict[str, JsonValue] = {
            "zone_id": str(zone),
            "period_from": _stamp(period.start),
            "period_to": _stamp(period.end),
        }
        parameters = {
            "organization_id": context.organization_id,
            "zone_id": zone,
            "period_start": period.start,
            "period_end": period.end,
        }
        async with self._database.transaction(context) as transaction:
            plant_id = await self._visible_zone(transaction, context, zone)
            if plant_id is None:
                await self._audited(transaction, context, filters, None, None, 0)
                composition = None
            else:
                inputs = await self._period_inputs(transaction, parameters)
                composition = compose_timeline(inputs, period)
                await self._audited(
                    transaction, context, filters, plant_id, zone, len(composition.intervals)
                )
        if composition is None or plant_id is None:
            raise CoverageZoneNotFound()
        return CoverageTimeline(
            organization_id=context.organization_id,
            plant_id=plant_id,
            zone_id=zone,
            period=composition.period,
            intervals=composition.intervals,
            node_intervals=composition.node_intervals,
            summary=composition.summary,
        )

    async def estado_en(
        self, context: ScopeContext, zone_id: uuid.UUID, instant: datetime
    ) -> CoverageStatus:
        """El compuesto de la zona en ``instant``; escribe una entrada ``coverage_read``."""
        context = _require_context(context)
        zone = _zone_id(zone_id)
        moment = _instant(instant)
        filters: dict[str, JsonValue] = {"zone_id": str(zone), "instant": _stamp(moment)}
        parameters = {
            "organization_id": context.organization_id,
            "zone_id": zone,
            "instant": moment,
        }
        async with self._database.transaction(context) as transaction:
            plant_id = await self._visible_zone(transaction, context, zone)
            if plant_id is None:
                await self._audited(transaction, context, filters, None, None, 0)
                status = None
            else:
                inputs = await self._instant_inputs(transaction, parameters, moment)
                status = state_at(inputs, moment)
                await self._audited(transaction, context, filters, plant_id, zone, 1)
        if status is None:
            raise CoverageZoneNotFound()
        return status

    # --- Lecturas -----------------------------------------------------------------------------

    @staticmethod
    async def _visible_zone(
        transaction: Transaction, context: ScopeContext, zone_id: uuid.UUID
    ) -> uuid.UUID | None:
        row = (
            await transaction.execute(
                _ZONE, {"organization_id": context.organization_id, "zone_id": zone_id}
            )
        ).first()
        if row is None:
            return None
        plant_id = _id(row.plant_id)
        return plant_id if _visible(context, plant_id, zone_id) else None

    @staticmethod
    async def _rows(
        transaction: Transaction, statement: Any, parameters: dict[str, Any]
    ) -> Sequence[Row[Any]]:
        return (await transaction.execute(statement, parameters)).all()

    async def _period_inputs(
        self, transaction: Transaction, parameters: dict[str, Any]
    ) -> CoverageInputs:
        assignments = tuple(
            _assignment(row)
            for row in await self._rows(transaction, _ASSIGNMENTS_IN_PERIOD, parameters)
        )
        node_ids = sorted({assignment.node_id for assignment in assignments})
        communication: tuple[CommunicationInput, ...] = ()
        if node_ids:
            communication = tuple(
                _communication(row)
                for row in await self._rows(
                    transaction, _COMMUNICATION_IN_PERIOD, {**parameters, "node_ids": node_ids}
                )
            )
        gates = tuple(
            _gate(row) for row in await self._rows(transaction, _GATES_IN_PERIOD, parameters)
        )
        observability = tuple(
            _observability(row)
            for row in await self._rows(transaction, _OBSERVABILITY_IN_PERIOD, parameters)
        )
        first = (await self._rows(transaction, _FIRST_REPORT, parameters))[0]
        return CoverageInputs(
            observability=observability,
            communication=communication,
            gates=gates,
            assignments=assignments,
            first_report_at=first.first_report_at,
        )

    async def _instant_inputs(
        self, transaction: Transaction, parameters: dict[str, Any], moment: datetime
    ) -> CoverageInputs:
        assignments = tuple(
            _assignment(row) for row in await self._rows(transaction, _ASSIGNMENT_AT, parameters)
        )
        communication: tuple[CommunicationInput, ...] = ()
        if assignments:
            node = {**parameters, "node_id": assignments[0].node_id}
            current = await self._rows(transaction, _COMMUNICATION_STATE, node)
            # ``CommunicationState`` es la última declaración del nodo: si ya regía en el
            # instante, es la respuesta; si no, la última declaración que regía entonces.
            projected = tuple(_communication(row) for row in current)
            if projected and _effective(projected[0]) <= moment:
                communication = projected
            else:
                communication = tuple(
                    _communication(row)
                    for row in await self._rows(transaction, _COMMUNICATION_AT, node)
                )
        gates = tuple(_gate(row) for row in await self._rows(transaction, _GATE_AT, parameters))
        observability = tuple(
            _observability(row)
            for row in await self._rows(transaction, _ZONE_STARTER_AT, parameters)
        )
        first = (await self._rows(transaction, _FIRST_REPORT, parameters))[0]
        return CoverageInputs(
            observability=observability,
            communication=communication,
            gates=gates,
            assignments=assignments,
            first_report_at=first.first_report_at,
        )

    async def _audited(
        self,
        transaction: Transaction,
        context: ScopeContext,
        filters: dict[str, JsonValue],
        plant_id: uuid.UUID | None,
        zone_id: uuid.UUID | None,
        result_count: int,
    ) -> None:
        await self._audit.append(
            context,
            AuditOperation.COVERAGE_READ,
            plant_id=plant_id,
            zone_id=zone_id,
            filters=filters,
            result_count=result_count,
            transaction=transaction,
        )
