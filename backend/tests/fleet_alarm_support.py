"""Entorno de las pruebas de las alarmas de flota (TASK-225) sobre PostgreSQL 16 real.

- ``AlarmTasks``: las tres tareas reales (``evaluate_fleet_alarms``, ``detect_mute_nodes`` y
  ``alert_expiring_certificates``) sobre una bandeja con ``fleet_alarm_raised`` y
  ``fleet_alarm_cleared`` registrados, el escritor del expediente y la auditoría de la pila;
- ``run_in`` ejecuta un manejador en **su** transacción con el contexto de iteración periódica de
  la organización (el que da el planificador de U-02), como ``vigia_app``;
- ``AlarmRows`` lee, como superusuario, lo que quedó: filas de ``fleet_alarm``, ranuras, filas de
  histéresis, eventos de la bandeja y entradas de auditoría;
- ``blocked_or_done`` espera a que una tarea lanzada en paralelo esté **esperando un candado** de
  esta base (``pg_stat_activity``) o haya terminado: sincroniza las pruebas concurrentes sin que un
  tope de pared decida nada.

Solo datos generados (NFR-CTR-43). Las marcas salen del reloj simulado de la pila.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from tests.writer_support import unit_context
from vigia_platform.fleet.application.expiring_certificates import ExpiringCertificateAlerter
from vigia_platform.fleet.application.fleet_alarms import (
    BATCH_SIZE,
    AlarmDependencies,
    AlarmReport,
    FleetAlarmEvaluator,
)
from vigia_platform.fleet.application.mute_nodes import MuteDetector
from vigia_platform.ledger.application.audit_writer import AuditWriter
from vigia_platform.ledger.application.writer import EscritorExpediente
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorKind, ActorUnit, ScopeContext
from vigia_platform.shared.db import Database, Transaction
from vigia_platform.shared.outbox.publish import OutboxPort

__all__ = [
    "ARRIVAL_SECONDS",
    "AlarmRows",
    "AlarmTasks",
    "alarm_tasks",
    "blocked_or_done",
    "run_in",
    "system",
]

ARRIVAL_SECONDS: Final = 30.0
"""Tope de la espera de sincronización (no decide el resultado de ninguna prueba)."""
POLL_SECONDS: Final = 0.05


@dataclass(frozen=True)
class AlarmTasks:
    deps: AlarmDependencies
    evaluator: FleetAlarmEvaluator
    detector: MuteDetector
    alerter: ExpiringCertificateAlerter


def alarm_tasks(
    *,
    clock: Clock,
    outbox: OutboxPort,
    writer: EscritorExpediente,
    audit: AuditWriter,
    batch_size: int = BATCH_SIZE,
) -> AlarmTasks:
    deps = AlarmDependencies(clock=clock, outbox=outbox)
    return AlarmTasks(
        deps=deps,
        evaluator=FleetAlarmEvaluator(deps, audit=audit, batch_size=batch_size),
        detector=MuteDetector(deps, writer=writer, batch_size=batch_size),
        alerter=ExpiringCertificateAlerter(deps, batch_size=batch_size),
    )


def system(organization_id: uuid.UUID) -> ScopeContext:
    """El contexto de la iteración periódica de una organización (actor del sistema, U-03)."""
    return unit_context(organization_id, ActorUnit.U03, kind=ActorKind.SYSTEM, role=None)


async def run_in(
    database: Database,
    organization_id: uuid.UUID,
    handler: Callable[[Transaction], Awaitable[AlarmReport]],
) -> AlarmReport:
    """El manejador en su propia transacción de la organización (se confirma al salir)."""
    async with database.transaction(system(organization_id)) as transaction:
        return await handler(transaction)


async def blocked_or_done(admin: Any, task: asyncio.Task[Any], *, waiters: int = 1) -> None:
    """Hasta que ``waiters`` sesiones de esta base esperan un candado, o ``task`` terminó."""
    deadline = asyncio.get_running_loop().time() + ARRIVAL_SECONDS
    while not task.done():
        waiting = await admin.fetchval(
            "SELECT count(*) FROM pg_stat_activity"
            " WHERE datname = current_database() AND wait_event_type = 'Lock'"
        )
        if waiting >= waiters:
            return
        assert asyncio.get_running_loop().time() < deadline, "nadie llegó a esperar el candado"
        await asyncio.sleep(POLL_SECONDS)


@dataclass(frozen=True)
class AlarmRows:
    """Lecturas del superusuario sobre lo que escribieron las tareas."""

    fetch: Callable[..., list[Any]]

    def alarms(self, node: uuid.UUID, kind: str | None = None) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.fetch(
                "SELECT alarm_id, organization_id, plant_id, alarm_kind, node_id, zone_id,"
                " raised_at, cleared_at, raised_event_id, cleared_event_id FROM fleet.fleet_alarm"
                " WHERE node_id = $1 AND ($2::text IS NULL OR alarm_kind = $2)"
                " ORDER BY raised_at, alarm_id",
                node,
                kind,
            )
        ]

    def open_kinds(self, node: uuid.UUID) -> set[str]:
        return {
            str(row["alarm_kind"])
            for row in self.fetch(
                "SELECT alarm_kind FROM fleet.fleet_alarm"
                " WHERE node_id = $1 AND cleared_at IS NULL",
                node,
            )
        }

    def slots(self, node: uuid.UUID) -> set[str]:
        return {
            str(row["alarm_kind"])
            for row in self.fetch(
                "SELECT alarm_kind FROM fleet.open_fleet_alarm"
                " WHERE node_id = $1 AND alarm_id IS NOT NULL",
                node,
            )
        }

    def evaluations(self, node: uuid.UUID) -> dict[str, dict[str, Any]]:
        return {
            str(row["alarm_kind"]): dict(row)
            for row in self.fetch(
                "SELECT alarm_kind, observed, consecutive, observed_since, evaluated_at"
                " FROM fleet.fleet_alarm_evaluation WHERE node_id = $1",
                node,
            )
        }

    def events(self, name: str, node: uuid.UUID | None = None) -> list[dict[str, Any]]:
        rows = self.fetch(
            "SELECT event_id, organization_id, plant_id, payload::text AS payload, created_at"
            " FROM shared.outbox_event WHERE event_name = $1 ORDER BY created_at, event_id",
            name,
        )
        found = [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]
        if node is None:
            return found
        return [event for event in found if event["payload"]["node_id"] == str(node)]

    def security_alerts(self, node: uuid.UUID) -> list[dict[str, Any]]:
        return [
            {**dict(row), "filters": json.loads(row["filters"])}
            for row in self.fetch(
                "SELECT operation, outcome, actor_kind, actor_unit, scope_plant_id, scope_zone_id,"
                " resource_kind, resource_id, correlation_id,"
                " convert_from(filters, 'UTF8') AS filters"
                " FROM shared.audit_entry WHERE operation = 'fleet_security_alert'"
                " AND resource_id = $1 ORDER BY chain_sequence",
                node,
            )
        ]

    def communication(self, node: uuid.UUID) -> list[dict[str, Any]]:
        return [
            json.loads(row["content"])
            for row in self.fetch(
                "SELECT ledger.vigia_bytes_to_jsonb(content)::text AS content"
                " FROM ledger.ledger_record WHERE record_type = 'node_communication_state_changed'"
                " AND scope_node_id = $1 ORDER BY chain_sequence",
                node,
            )
        ]

    def state(self, node: uuid.UUID) -> str | None:
        rows = self.fetch(
            "SELECT communication_state FROM fleet.node_inventory WHERE node_id = $1", node
        )
        return None if not rows else str(rows[0]["communication_state"])


def kinds(alarms: Sequence[dict[str, Any]]) -> list[str]:
    return [str(alarm["alarm_kind"]) for alarm in alarms]
