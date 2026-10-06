"""Alarmas de flota sobre PostgreSQL (TASK-225; LC-GOB-16, LC-GOB-18; gob_0018 y gob_0026).

Lecturas y escrituras de las tres tareas y de ``GET /fleet/alarms``. Toda sentencia nombra la
organización de la transacción (defensa en profundidad sobre la RLS: las pruebas la ejecutan como
superusuario, sin RLS) y trabaja por lotes de nodos con ``unnest``: un número acotado de
sentencias por lote, nunca una por nodo.

- ``evaluation_facts``: por nodo, lo que necesitan las condiciones de ``fleet_warnings.evaluate``
  (inventario del último latido, umbrales de la planta, intervalo efectivo, credencial vigente,
  cámaras bajo su mínimo, zona productiva y clips ``evidence`` de las últimas 24 h; los de
  verificación nunca cuentan, nota de BR-GOB-94), el estado del nodo y su baja;
- ``lock_evaluations`` / ``insert_evaluations`` / ``update_evaluations``: la fila de histéresis de
  ``fleet.fleet_alarm_evaluation`` de cada (nodo, clase), **bloqueada** (``FOR UPDATE``) antes de
  decidir; las que faltan se insertan con ``ON CONFLICT DO NOTHING`` y solo cuentan las que este
  ciclo insertó. Así dos ciclos solapados se serializan en la fila y el segundo la ve ya evaluada
  (NFR-GOB-47);
- ``open_alarms``: las alarmas abiertas de los nodos, por la ranura ``open_fleet_alarm``;
- ``lock_open`` / ``clear``: el cierre bloquea las filas abiertas (las que otro ciclo ya cerró no
  vuelven) y escribe ``cleared_at`` y ``cleared_event_id`` una sola vez;
- ``raise_alarms``: el ``INSERT`` de las alarmas nuevas; el disparador ``open_alarm_slot`` ocupa la
  ranura y, si otra transacción la ocupó, ``AlarmAlreadyOpen`` (la transacción se deshace entera:
  ese ciclo no tiene efecto);
- ``lock_mute_candidates`` / ``mark_mute``: los nodos silenciosos con su fila de
  ``node_inventory`` bloqueada (``FOR UPDATE``) y la ficha de flota compartida (``FOR SHARE``): la
  condición se vuelve a comprobar sobre la fila bloqueada, así que un latido concurrente
  (``reachable``) o una revocación concurrente la dejan fuera;
- ``expiring_certificates``: los nodos vigentes cuya credencial vence en 15 días o menos sin
  ``certificate_expiring`` abierta;
- ``alarm_page``: una página de ``GET /fleet/alarms`` con el alcance del contexto.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.engine import Row

from vigia_platform.fleet.adapters.postgres.inventory_queries import scope_parameters
from vigia_platform.fleet.domain.alarm_hysteresis import Evaluation
from vigia_platform.fleet.domain.communication_state import (
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    MUTE_FACTOR,
)
from vigia_platform.fleet.domain.enums import FleetAlarmKind
from vigia_platform.fleet.domain.fleet_alarm import AlarmStatus, FleetAlarm, is_retired
from vigia_platform.fleet.domain.fleet_thresholds import FleetThresholds
from vigia_platform.fleet.domain.fleet_warnings import (
    CameraReading,
    WarningInputs,
)
from vigia_platform.fleet.domain.mute_detection import MUTE_ELIGIBLE_STATUS, MuteCandidate
from vigia_platform.ledger.domain.coverage import CommunicationState
from vigia_platform.shared.context import repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.outbox.registries import DiscardedCycle

__all__ = [
    "AlarmAlreadyOpen",
    "AlarmCursor",
    "AlarmFilters",
    "EvaluationFacts",
    "ExpiringCertificate",
    "PostgresFleetAlarmStore",
]

UNIQUE_VIOLATION: Final = "23505"
SLOT_CONSTRAINT: Final = "open_fleet_alarm_pkey"

_FACTS: Final = text(
    "SELECT n.node_id, n.plant_id, n.status, f.revoked_at, f.decommissioned_at,"
    " v.communication_state, v.last_heartbeat_at,"
    " CAST(v.local_queue ->> 'pending' AS bigint) AS pending,"
    " CAST(v.local_queue ->> 'oldest_pending_at' AS timestamptz) AS oldest_pending_at,"
    " CAST(v.clock ->> 'offset_ms' AS bigint) AS offset_ms,"
    " v.contract_notice ->> 'retires_at' AS retires_at,"
    " v.signal_reader ->> 'adapter' AS adapter,"
    " gate.productive_zone_id, cred.expires_at AS certificate_expires_at,"
    " COALESCE(cam.below, false) AS camera_below,"
    " COALESCE(clips.orphan_clips, 0) AS orphan_clips, COALESCE(clips.day_clips, 0) AS day_clips,"
    " cfg.heartbeat_interval_seconds, t.queue_pending_threshold, t.queue_age_threshold_minutes,"
    " t.clock_drift_threshold_ms"
    " FROM identity.node_identity AS n"
    " LEFT JOIN fleet.node_fleet_record AS f"
    " ON f.organization_id = n.organization_id AND f.node_id = n.node_id"
    " LEFT JOIN fleet.node_inventory AS v"
    " ON v.organization_id = n.organization_id AND v.node_id = n.node_id"
    " LEFT JOIN fleet.node_configuration AS cfg"
    " ON cfg.organization_id = n.organization_id AND cfg.node_id = n.node_id"
    " LEFT JOIN fleet.plant_fleet_thresholds AS t"
    " ON t.organization_id = n.organization_id AND t.plant_id = n.plant_id"
    " LEFT JOIN LATERAL (SELECT max(c.expires_at) AS expires_at FROM fleet.node_credential AS c"
    " WHERE c.organization_id = n.organization_id AND c.plant_id = n.plant_id"
    " AND c.node_id = n.node_id AND c.status IN ('active', 'overlapping')) AS cred ON true"
    # Solo las cámaras del último latido aceptado (``updated_at`` igual al del inventario).
    " LEFT JOIN LATERAL (SELECT bool_or(ci.measured_fps < ci.declared_min_fps) AS below"
    " FROM fleet.camera_inventory AS ci WHERE ci.organization_id = n.organization_id"
    " AND ci.node_id = n.node_id AND ci.updated_at = v.updated_at) AS cam ON true"
    # La zona productiva (por ZoneGateState de la plataforma, no por lo que cree el nodo).
    " LEFT JOIN LATERAL (SELECT (array_agg(a.zone_id ORDER BY a.zone_id)"
    " FILTER (WHERE gs.resulting_mode = 'productive'))[1] AS productive_zone_id"
    " FROM identity.zone_node_assignment AS a JOIN catalog.zone_gate_state AS gs"
    " ON gs.organization_id = a.organization_id AND gs.zone_id = a.zone_id"
    " WHERE a.organization_id = n.organization_id AND a.node_id = n.node_id"
    " AND a.unassigned_at IS NULL) AS gate ON true"
    " LEFT JOIN LATERAL (SELECT"
    " count(*) FILTER (WHERE g.status = 'orphan' AND g.orphaned_at >= CAST(:clips_since AS"
    " timestamptz) AND g.orphaned_at < CAST(:now AS timestamptz)) AS orphan_clips,"
    " count(*) FILTER (WHERE g.issued_at >= CAST(:clips_since AS timestamptz)"
    " AND g.issued_at < CAST(:now AS timestamptz)) AS day_clips"
    " FROM fleet.clip_upload_grant AS g"
    " WHERE g.organization_id = n.organization_id AND g.plant_id = n.plant_id"
    " AND g.node_id = n.node_id AND g.purpose = 'evidence'"
    " AND ((g.status = 'orphan' AND g.orphaned_at >= CAST(:clips_since AS timestamptz)"
    " AND g.orphaned_at < CAST(:now AS timestamptz))"
    " OR (g.issued_at >= CAST(:clips_since AS timestamptz)"
    " AND g.issued_at < CAST(:now AS timestamptz)))) AS clips ON true"
    " WHERE n.organization_id = :organization_id"
    " AND (CAST(:after AS uuid) IS NULL OR n.node_id > CAST(:after AS uuid))"
    " ORDER BY n.node_id LIMIT :limit"
)

_LOCK_EVALUATIONS: Final = text(
    "SELECT node_id, alarm_kind, observed, consecutive, observed_since, evaluated_at"
    " FROM fleet.fleet_alarm_evaluation"
    " WHERE organization_id = :organization_id AND node_id = ANY(CAST(:node_ids AS uuid[]))"
    " ORDER BY node_id, alarm_kind FOR UPDATE"
)
_INSERT_EVALUATIONS: Final = text(
    "INSERT INTO fleet.fleet_alarm_evaluation (organization_id, plant_id, node_id, alarm_kind,"
    " observed, consecutive, observed_since, evaluated_at)"
    " SELECT :organization_id, e.plant_id, e.node_id, e.alarm_kind, e.observed, 1, :now, :now"
    " FROM unnest(CAST(:plant_ids AS uuid[]), CAST(:node_ids AS uuid[]),"
    " CAST(:kinds AS text[]), CAST(:observed AS boolean[]))"
    " AS e(plant_id, node_id, alarm_kind, observed)"
    " ORDER BY e.node_id, e.alarm_kind"
    " ON CONFLICT (organization_id, node_id, alarm_kind) DO NOTHING"
    " RETURNING node_id, alarm_kind"
)
_UPDATE_EVALUATIONS: Final = text(
    "UPDATE fleet.fleet_alarm_evaluation AS s SET observed = e.observed,"
    " consecutive = e.consecutive, observed_since = e.observed_since, evaluated_at = :now"
    " FROM unnest(CAST(:node_ids AS uuid[]), CAST(:kinds AS text[]),"
    " CAST(:observed AS boolean[]), CAST(:consecutive AS integer[]),"
    " CAST(:since AS timestamptz[])) AS e(node_id, alarm_kind, observed, consecutive,"
    " observed_since)"
    " WHERE s.organization_id = :organization_id AND s.node_id = e.node_id"
    " AND s.alarm_kind = e.alarm_kind"
)

_OPEN_ALARMS: Final = text(
    "SELECT a.alarm_id, a.plant_id, a.alarm_kind, a.node_id, a.zone_id, a.raised_at,"
    " a.cleared_at, a.raised_event_id, a.cleared_event_id"
    " FROM fleet.open_fleet_alarm AS s JOIN fleet.fleet_alarm AS a"
    " ON a.organization_id = s.organization_id AND a.alarm_id = s.alarm_id"
    " AND a.raised_at = s.raised_at"
    " WHERE s.organization_id = :organization_id AND s.alarm_id IS NOT NULL"
    " AND s.node_id = ANY(CAST(:node_ids AS uuid[]))"
    " ORDER BY a.node_id, a.alarm_kind"
)
_LOCK_OPEN: Final = text(
    "SELECT a.alarm_id, a.plant_id, a.alarm_kind, a.node_id, a.zone_id, a.raised_at,"
    " a.cleared_at, a.raised_event_id, a.cleared_event_id"
    " FROM fleet.fleet_alarm AS a"
    " JOIN unnest(CAST(:alarm_ids AS uuid[]), CAST(:raised_ats AS timestamptz[]))"
    " AS t(alarm_id, raised_at) ON a.alarm_id = t.alarm_id AND a.raised_at = t.raised_at"
    " WHERE a.organization_id = :organization_id AND a.cleared_at IS NULL"
    " ORDER BY a.alarm_id FOR UPDATE OF a"
)
_CLEAR: Final = text(
    "UPDATE fleet.fleet_alarm AS a SET cleared_at = :cleared_at, cleared_event_id = t.event_id"
    " FROM unnest(CAST(:alarm_ids AS uuid[]), CAST(:raised_ats AS timestamptz[]),"
    " CAST(:event_ids AS uuid[])) AS t(alarm_id, raised_at, event_id)"
    " WHERE a.organization_id = :organization_id AND a.alarm_id = t.alarm_id"
    " AND a.raised_at = t.raised_at AND a.cleared_at IS NULL"
)
_RAISE: Final = text(
    "INSERT INTO fleet.fleet_alarm (alarm_id, organization_id, plant_id, alarm_kind, node_id,"
    " zone_id, raised_at, raised_event_id)"
    " SELECT r.alarm_id, :organization_id, r.plant_id, r.alarm_kind, r.node_id, r.zone_id,"
    " :raised_at, r.event_id"
    " FROM unnest(CAST(:alarm_ids AS uuid[]), CAST(:plant_ids AS uuid[]),"
    " CAST(:kinds AS text[]), CAST(:node_ids AS uuid[]), CAST(:zone_ids AS uuid[]),"
    " CAST(:event_ids AS uuid[])) AS r(alarm_id, plant_id, alarm_kind, node_id, zone_id, event_id)"
    " ORDER BY r.node_id, r.alarm_kind"
)

# Los nodos silenciosos (o ya ``mute`` sin su alarma), con la fila del inventario bloqueada: la
# condición se vuelve a evaluar sobre la versión bloqueada (un latido concurrente la deja fuera).
_MUTE_CANDIDATES: Final = text(
    "SELECT v.node_id, v.plant_id, v.last_heartbeat_at, v.communication_state"
    " FROM fleet.node_inventory AS v"
    " JOIN identity.node_identity AS n"
    " ON n.organization_id = v.organization_id AND n.node_id = v.node_id"
    " JOIN fleet.node_fleet_record AS f"
    " ON f.organization_id = v.organization_id AND f.node_id = v.node_id"
    " LEFT JOIN fleet.node_configuration AS cfg"
    " ON cfg.organization_id = v.organization_id AND cfg.node_id = v.node_id"
    " WHERE v.organization_id = :organization_id AND n.status = :eligible_status"
    " AND f.revoked_at IS NULL AND f.decommissioned_at IS NULL"
    " AND v.last_heartbeat_at IS NOT NULL"
    " AND CAST(:now AS timestamptz) - v.last_heartbeat_at > interval '1 second'"
    " * (CAST(:mute_factor AS integer)"
    " * COALESCE(cfg.heartbeat_interval_seconds, CAST(:default_interval AS integer)))"
    " AND (v.communication_state <> 'mute' OR NOT EXISTS (SELECT 1"
    " FROM fleet.open_fleet_alarm AS s WHERE s.organization_id = v.organization_id"
    " AND s.node_id = v.node_id AND s.alarm_kind = 'node_mute' AND s.alarm_id IS NOT NULL))"
    " AND (CAST(:after_plant AS uuid) IS NULL"
    " OR (v.plant_id, v.node_id) > (CAST(:after_plant AS uuid), CAST(:after_node AS uuid)))"
    " ORDER BY v.plant_id, v.node_id LIMIT :limit"
    " FOR UPDATE OF v FOR SHARE OF f"
)
_MARK_MUTE: Final = text(
    "UPDATE fleet.node_inventory SET communication_state = 'mute'"
    " WHERE organization_id = :organization_id AND node_id = ANY(CAST(:node_ids AS uuid[]))"
    " AND communication_state <> 'mute'"
)

_EXPIRING: Final = text(
    "SELECT n.node_id, n.plant_id, cred.expires_at"
    " FROM identity.node_identity AS n"
    " JOIN fleet.node_fleet_record AS f"
    " ON f.organization_id = n.organization_id AND f.node_id = n.node_id"
    " JOIN LATERAL (SELECT max(c.expires_at) AS expires_at FROM fleet.node_credential AS c"
    " WHERE c.organization_id = n.organization_id AND c.plant_id = n.plant_id"
    " AND c.node_id = n.node_id AND c.status IN ('active', 'overlapping')) AS cred ON true"
    " WHERE n.organization_id = :organization_id AND n.status <> 'revoked'"
    " AND f.revoked_at IS NULL AND f.decommissioned_at IS NULL"
    " AND cred.expires_at <= CAST(:until AS timestamptz)"
    " AND NOT EXISTS (SELECT 1 FROM fleet.open_fleet_alarm AS s"
    " WHERE s.organization_id = n.organization_id AND s.node_id = n.node_id"
    " AND s.alarm_kind = 'certificate_expiring' AND s.alarm_id IS NOT NULL)"
    " AND (CAST(:after AS uuid) IS NULL OR n.node_id > CAST(:after AS uuid))"
    " ORDER BY n.node_id LIMIT :limit"
)

_ALARM_PAGE: Final = text(
    "SELECT a.alarm_id, a.plant_id, a.alarm_kind, a.node_id, a.zone_id, a.raised_at,"
    " a.cleared_at, a.raised_event_id, a.cleared_event_id"
    " FROM fleet.fleet_alarm AS a"
    " WHERE a.organization_id = :organization_id"
    " AND (CAST(:whole_organization AS boolean)"
    " OR a.plant_id = ANY(CAST(:scope_plants AS uuid[]))"
    " OR EXISTS (SELECT 1 FROM identity.zone_node_assignment AS sa"
    " WHERE sa.organization_id = a.organization_id AND sa.node_id = a.node_id"
    " AND sa.unassigned_at IS NULL AND sa.zone_id = ANY(CAST(:scope_zones AS uuid[]))))"
    " AND (CAST(:plant_id AS uuid) IS NULL OR a.plant_id = CAST(:plant_id AS uuid))"
    " AND (CAST(:node_id AS uuid) IS NULL OR a.node_id = CAST(:node_id AS uuid))"
    " AND (CAST(:alarm_kind AS text) IS NULL OR a.alarm_kind = CAST(:alarm_kind AS text))"
    " AND (CAST(:status AS text) IS NULL"
    " OR (CAST(:status AS text) = 'active') = (a.cleared_at IS NULL))"
    " AND (CAST(:after_raised_at AS timestamptz) IS NULL"
    " OR (a.raised_at, a.alarm_id)"
    " < (CAST(:after_raised_at AS timestamptz), CAST(:after_alarm_id AS uuid)))"
    " ORDER BY a.raised_at DESC, a.alarm_id DESC LIMIT :limit"
)


class AlarmAlreadyOpen(DiscardedCycle):
    """Otra transacción abrió antes la alarma de (clase, nodo): la ranura está ocupada. La
    transacción ya falló y se deshace entera (el ciclo solapado no tiene efecto). En una tarea
    periódica, el planificador lo cuenta como ciclo descartado, no como fallo (TASK-227)."""

    def __init__(self) -> None:
        super().__init__("la alarma de esa clase y ese nodo ya está abierta")


@dataclass(frozen=True, slots=True)
class EvaluationFacts:
    """Lo que ``evaluate_fleet_alarms`` sabe de un nodo en ``now``."""

    node_id: uuid.UUID
    plant_id: uuid.UUID
    retired: bool
    communication_state: CommunicationState | None
    inputs: WarningInputs
    thresholds: FleetThresholds
    productive_zone_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class ExpiringCertificate:
    node_id: uuid.UUID
    plant_id: uuid.UUID
    expires_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class AlarmFilters:
    """Filtros de ``GET /fleet/alarms`` (``None``: sin filtro)."""

    plant_id: uuid.UUID | None = None
    node_id: uuid.UUID | None = None
    alarm_kind: FleetAlarmKind | None = None
    status: AlarmStatus | None = None


@dataclass(frozen=True, slots=True)
class AlarmCursor:
    """Posición en el listado: el último ``(raised_at, alarm_id)`` devuelto."""

    raised_at: datetime
    alarm_id: uuid.UUID


def _uuid(value: object) -> uuid.UUID:
    """``uuid.UUID`` exacto (asyncpg devuelve una subclase propia)."""
    return uuid.UUID(str(value))


def _optional_uuid(value: object) -> uuid.UUID | None:
    return None if value is None else _uuid(value)


def _alarm(organization_id: uuid.UUID, row: Row[Any]) -> FleetAlarm:
    return FleetAlarm(
        alarm_id=_uuid(row.alarm_id),
        organization_id=organization_id,
        plant_id=_uuid(row.plant_id),
        alarm_kind=FleetAlarmKind(row.alarm_kind),
        node_id=_uuid(row.node_id),
        zone_id=_optional_uuid(row.zone_id),
        raised_at=row.raised_at,
        cleared_at=row.cleared_at,
        raised_event_id=_uuid(row.raised_event_id),
        cleared_event_id=_optional_uuid(row.cleared_event_id),
    )


def _facts(row: Row[Any]) -> EvaluationFacts:
    plant_id = _uuid(row.plant_id)
    retired = is_retired(
        str(row.status), revoked_at=row.revoked_at, decommissioned_at=row.decommissioned_at
    )
    heard = row.last_heartbeat_at is not None
    inputs = WarningInputs(
        status=str(row.status),
        decommissioned_at=row.decommissioned_at,
        last_heartbeat_at=row.last_heartbeat_at,
        heartbeat_interval_seconds=(
            DEFAULT_HEARTBEAT_INTERVAL_SECONDS
            if row.heartbeat_interval_seconds is None
            else int(row.heartbeat_interval_seconds)
        ),
        pending=None if row.pending is None else int(row.pending),
        oldest_pending_at=row.oldest_pending_at,
        offset_ms=None if row.offset_ms is None else int(row.offset_ms),
        retires_at=row.retires_at,
        adapter=row.adapter,
        productive_zone=row.productive_zone_id is not None,
        certificate_expires_at=row.certificate_expires_at,
        # Basta una cámara bajo su mínimo (la consulta ya lo resolvió con las del último latido).
        cameras=(CameraReading(0.0, 1.0),) if heard and row.camera_below else (),
        orphan_clips=int(row.orphan_clips),
        day_clips=int(row.day_clips),
    )
    thresholds = (
        FleetThresholds(plant_id=plant_id)
        if row.queue_pending_threshold is None
        else FleetThresholds(
            plant_id=plant_id,
            queue_pending_threshold=int(row.queue_pending_threshold),
            queue_age_threshold_minutes=int(row.queue_age_threshold_minutes),
            clock_drift_threshold_ms=int(row.clock_drift_threshold_ms),
        )
    )
    return EvaluationFacts(
        node_id=_uuid(row.node_id),
        plant_id=plant_id,
        retired=retired,
        communication_state=(
            None if row.communication_state is None else CommunicationState(row.communication_state)
        ),
        inputs=inputs,
        thresholds=thresholds,
        productive_zone_id=_optional_uuid(row.productive_zone_id),
    )


def _slot_taken(error: BaseException) -> bool:
    """¿La ranura de ``open_fleet_alarm`` estaba ocupada? (``unique_violation`` del disparador)."""
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if getattr(current, "sqlstate", None) == UNIQUE_VIOLATION and getattr(
            current, "constraint_name", SLOT_CONSTRAINT
        ) in (None, SLOT_CONSTRAINT):
            return True
        for linked in (current.__cause__, getattr(current, "orig", None)):
            if isinstance(linked, BaseException):
                pending.append(linked)
    return False


@repository
class PostgresFleetAlarmStore:
    """Las sentencias de ``fleet.alarms`` sobre ``fleet``: cada una nombra la organización."""

    # --- Evaluación ----------------------------------------------------------------------------

    async def evaluation_facts(
        self,
        transaction: Transaction,
        *,
        now: datetime,
        clips_since: datetime,
        after: uuid.UUID | None,
        limit: int,
    ) -> tuple[EvaluationFacts, ...]:
        """Un lote de nodos de la organización, en orden de ``node_id`` (una sentencia)."""
        result = await transaction.execute(
            _FACTS,
            {
                "organization_id": transaction.context.organization_id,
                "now": now,
                "clips_since": clips_since,
                "after": after,
                "limit": limit,
            },
        )
        return tuple(_facts(row) for row in result)

    async def lock_evaluations(
        self, transaction: Transaction, node_ids: Sequence[uuid.UUID]
    ) -> dict[tuple[uuid.UUID, FleetAlarmKind], tuple[Evaluation, datetime]]:
        """Las filas de histéresis de los nodos, bloqueadas: ``(evaluación, evaluated_at)``."""
        if not node_ids:
            return {}
        result = await transaction.execute(
            _LOCK_EVALUATIONS,
            {
                "organization_id": transaction.context.organization_id,
                "node_ids": [str(node) for node in node_ids],
            },
        )
        return {
            (_uuid(row.node_id), FleetAlarmKind(row.alarm_kind)): (
                Evaluation(bool(row.observed), int(row.consecutive), row.observed_since),
                row.evaluated_at,
            )
            for row in result
        }

    async def insert_evaluations(
        self,
        transaction: Transaction,
        rows: Sequence[tuple[uuid.UUID, uuid.UUID, FleetAlarmKind, bool]],
        now: datetime,
    ) -> set[tuple[uuid.UUID, FleetAlarmKind]]:
        """Inserta las primeras evaluaciones ``(planta, nodo, clase, observado)``; devuelve las que
        insertó este ciclo (las que otro ciclo insertó antes no son suyas)."""
        if not rows:
            return set()
        result = await transaction.execute(
            _INSERT_EVALUATIONS,
            {
                "organization_id": transaction.context.organization_id,
                "now": now,
                "plant_ids": [str(plant) for plant, _, _, _ in rows],
                "node_ids": [str(node) for _, node, _, _ in rows],
                "kinds": [kind.value for _, _, kind, _ in rows],
                "observed": [observed for _, _, _, observed in rows],
            },
        )
        return {(_uuid(row.node_id), FleetAlarmKind(row.alarm_kind)) for row in result}

    async def update_evaluations(
        self,
        transaction: Transaction,
        rows: Mapping[tuple[uuid.UUID, FleetAlarmKind], Evaluation],
        now: datetime,
    ) -> None:
        """Escribe la evaluación de ``now`` en las filas ya bloqueadas (una sentencia)."""
        if not rows:
            return
        keys = sorted(rows, key=lambda key: (str(key[0]), key[1].value))
        await transaction.execute(
            _UPDATE_EVALUATIONS,
            {
                "organization_id": transaction.context.organization_id,
                "now": now,
                "node_ids": [str(node) for node, _ in keys],
                "kinds": [kind.value for _, kind in keys],
                "observed": [rows[key].observed for key in keys],
                "consecutive": [rows[key].consecutive for key in keys],
                "since": [rows[key].since for key in keys],
            },
        )

    # --- Alarmas -------------------------------------------------------------------------------

    async def open_alarms(
        self, transaction: Transaction, node_ids: Sequence[uuid.UUID]
    ) -> dict[tuple[uuid.UUID, FleetAlarmKind], FleetAlarm]:
        """Las alarmas abiertas de los nodos por (nodo, clase)."""
        if not node_ids:
            return {}
        organization_id = transaction.context.organization_id
        result = await transaction.execute(
            _OPEN_ALARMS,
            {"organization_id": organization_id, "node_ids": [str(node) for node in node_ids]},
        )
        alarms = (_alarm(organization_id, row) for row in result)
        return {(alarm.node_id, alarm.alarm_kind): alarm for alarm in alarms}

    async def lock_open(
        self, transaction: Transaction, alarms: Sequence[FleetAlarm]
    ) -> tuple[FleetAlarm, ...]:
        """Bloquea las alarmas que siguen abiertas; las que otro ciclo cerró no vuelven."""
        if not alarms:
            return ()
        organization_id = transaction.context.organization_id
        result = await transaction.execute(
            _LOCK_OPEN,
            {
                "organization_id": organization_id,
                "alarm_ids": [str(alarm.alarm_id) for alarm in alarms],
                "raised_ats": [alarm.raised_at for alarm in alarms],
            },
        )
        return tuple(_alarm(organization_id, row) for row in result)

    async def clear(
        self,
        transaction: Transaction,
        cleared: Sequence[tuple[FleetAlarm, uuid.UUID]],
        cleared_at: datetime,
    ) -> None:
        """``cleared_at`` y ``cleared_event_id`` de las alarmas ya bloqueadas (una sentencia)."""
        if not cleared:
            return
        await transaction.execute(
            _CLEAR,
            {
                "organization_id": transaction.context.organization_id,
                "cleared_at": cleared_at,
                "alarm_ids": [str(alarm.alarm_id) for alarm, _ in cleared],
                "raised_ats": [alarm.raised_at for alarm, _ in cleared],
                "event_ids": [str(event_id) for _, event_id in cleared],
            },
        )

    async def raise_alarms(self, transaction: Transaction, raised: Sequence[FleetAlarm]) -> None:
        """Inserta las alarmas nuevas (una sentencia); ranura ocupada → ``AlarmAlreadyOpen``."""
        if not raised:
            return
        organization_id = transaction.context.organization_id
        moments = {alarm.raised_at for alarm in raised}
        if len(moments) != 1 or any(a.organization_id != organization_id for a in raised):
            raise ValueError("las alarmas de un lote son de la organización y del mismo instante")
        try:
            await transaction.execute(
                _RAISE,
                {
                    "organization_id": organization_id,
                    "raised_at": moments.pop(),
                    "alarm_ids": [str(alarm.alarm_id) for alarm in raised],
                    "plant_ids": [str(alarm.plant_id) for alarm in raised],
                    "kinds": [alarm.alarm_kind.value for alarm in raised],
                    "node_ids": [str(alarm.node_id) for alarm in raised],
                    "zone_ids": [_text_or_none(alarm.zone_id) for alarm in raised],
                    "event_ids": [str(alarm.raised_event_id) for alarm in raised],
                },
            )
        except Exception as error:
            if _slot_taken(error):
                raise AlarmAlreadyOpen() from error
            raise

    # --- Mudos ---------------------------------------------------------------------------------

    async def lock_mute_candidates(
        self,
        transaction: Transaction,
        *,
        now: datetime,
        after: tuple[uuid.UUID, uuid.UUID] | None,
        limit: int,
    ) -> tuple[MuteCandidate, ...]:
        """Un lote de nodos silenciosos con su fila del inventario bloqueada, por (planta, nodo)."""
        result = await transaction.execute(
            _MUTE_CANDIDATES,
            {
                "organization_id": transaction.context.organization_id,
                "eligible_status": MUTE_ELIGIBLE_STATUS,
                "now": now,
                "mute_factor": MUTE_FACTOR,
                "default_interval": DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
                "after_plant": None if after is None else after[0],
                "after_node": None if after is None else after[1],
                "limit": limit,
            },
        )
        return tuple(
            MuteCandidate(
                node_id=_uuid(row.node_id),
                plant_id=_uuid(row.plant_id),
                last_heartbeat_at=row.last_heartbeat_at,
                communication_state=CommunicationState(row.communication_state),
            )
            for row in result
        )

    async def mark_mute(self, transaction: Transaction, node_ids: Sequence[uuid.UUID]) -> None:
        """``communication_state = mute`` en las filas ya bloqueadas (una sentencia)."""
        if not node_ids:
            return
        await transaction.execute(
            _MARK_MUTE,
            {
                "organization_id": transaction.context.organization_id,
                "node_ids": [str(node) for node in node_ids],
            },
        )

    # --- Certificados --------------------------------------------------------------------------

    async def expiring_certificates(
        self,
        transaction: Transaction,
        *,
        until: datetime,
        after: uuid.UUID | None,
        limit: int,
    ) -> tuple[ExpiringCertificate, ...]:
        """Un lote de nodos vigentes con la credencial a ``until`` o antes y sin alarma abierta."""
        result = await transaction.execute(
            _EXPIRING,
            {
                "organization_id": transaction.context.organization_id,
                "until": until,
                "after": after,
                "limit": limit,
            },
        )
        return tuple(
            ExpiringCertificate(_uuid(row.node_id), _uuid(row.plant_id), row.expires_at)
            for row in result
        )

    # --- Consola -------------------------------------------------------------------------------

    async def alarm_page(
        self,
        transaction: Transaction,
        *,
        filters: AlarmFilters,
        after: AlarmCursor | None,
        limit: int,
    ) -> tuple[FleetAlarm, ...]:
        """Una página de alarmas del alcance del contexto, la más reciente primero."""
        context = transaction.context
        organization_id = context.organization_id
        result = await transaction.execute(
            _ALARM_PAGE,
            {
                "organization_id": organization_id,
                **scope_parameters(context),
                "plant_id": filters.plant_id,
                "node_id": filters.node_id,
                "alarm_kind": None if filters.alarm_kind is None else filters.alarm_kind.value,
                "status": None if filters.status is None else filters.status.value,
                "after_raised_at": None if after is None else after.raised_at,
                "after_alarm_id": None if after is None else after.alarm_id,
                "limit": limit,
            },
        )
        return tuple(_alarm(organization_id, row) for row in result)


def _text_or_none(value: uuid.UUID | None) -> str | None:
    return None if value is None else str(value)
