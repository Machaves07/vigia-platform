"""Entorno de las pruebas del inventario de flota y de ``FleetQueryPort`` (TASK-224).

Sobre ``fleet_stack`` (PostgreSQL 16 real como ``vigia_app``, la aplicación con la cadena fija):

- ``InventoryWorld`` siembra un cliente con dos plantas y sus zonas, un administrador de toda la
  organización, administradores de una planta y de una zona, y un instalador bajo concesión de
  toda la organización (``fleet.manage``); los nodos se declaran por la ruta real (la declaración
  escribe su ``node_communication_state_changed`` con ``unknown``);
- ``NodeState`` es el estado del inventario que una prueba quiere ver y ``write_state`` lo deja
  en las tablas con el **superusuario** (proyecciones del latido, cámaras, credencial vigente,
  clips huérfanos y del día, compuerta de las zonas): las pruebas de oráculo generan el estado,
  lo escriben y comparan la respuesta de ``GET /fleet/nodes`` con ``fleet_warnings.evaluate``;
- ``counting`` cuenta las sentencias que llegan al servidor desde la ``Database`` de la aplicación
  (``before_cursor_execute``), sin el ``set_config`` de la apertura de cada transacción;
- ``fingerprint`` resume las tablas que el inventario no debe escribir.

Solo datos generados (NFR-CTR-43). Las marcas salen del reloj simulado, que arranca en la hora de
la base (retro 14).
"""

from __future__ import annotations

import contextlib
import json
import secrets
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

import httpx
from sqlalchemy import event
from sqlalchemy.dialects.postgresql.asyncpg import dialect as asyncpg_dialect

from tests.authz_support import Site, StatementLog
from tests.fleet_http_support import FleetStack, new_code
from tests.writer_support import unit_context
from vigia_platform.fleet.domain.fleet_thresholds import FleetThresholds
from vigia_platform.fleet.domain.fleet_warnings import CameraReading, WarningInputs
from vigia_platform.identity.auth.sessions import SessionCookie
from vigia_platform.ledger.application.writer import LedgerRejection, RecordScope
from vigia_platform.shared.context import ActorKind, ActorUnit, Role, ScopeLevel
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "INVENTORY_WRITTEN_TABLES",
    "InventoryWorld",
    "NodeState",
    "as_superuser",
    "counting",
    "fingerprint",
]

Who = tuple[SessionCookie, uuid.UUID | None]

INVENTORY_WRITTEN_TABLES: Final = (
    "fleet.node_inventory",
    "fleet.camera_inventory",
    "fleet.zone_node_state",
    "fleet.heartbeat_history",
    "fleet.node_fleet_record",
    "fleet.node_credential",
    "fleet.clip_upload_grant",
    "fleet.fleet_alarm",
    "fleet.plant_fleet_thresholds",
    "identity.node_identity",
    "identity.zone_node_assignment",
    "ledger.ledger_record",
    "shared.audit_entry",
)
"""Las tablas cuya huella no cambia con una lectura del inventario sin concesión."""


@dataclass(frozen=True, slots=True, kw_only=True)
class NodeState:
    """Lo que una prueba fija del inventario de un nodo (desplazamientos respecto de ``now``)."""

    status: str = "enrolled"
    decommissioned: bool = False
    heartbeat_age_ms: int | None = 1_000
    """``now - last_heartbeat_at``; ``None``: el nodo nunca envió un latido (sin inventario)."""
    interval_seconds: int | None = None
    """``NodeConfiguration.heartbeat_interval_seconds``; ``None``: sin configuración (60 s)."""
    pending: int = 0
    oldest_pending_age_ms: int | None = None
    offset_ms: int = 0
    retires_at: str | None = None
    adapter: str = "modbus_rtu"
    certificate_in_ms: int | None = None
    """``expires_at - now`` de la credencial vigente; ``None``: sin credencial vigente."""
    cameras: tuple[tuple[float, float], ...] = ((25.0, 10.0),)
    """``(measured_fps, declared_min_fps)`` de cada cámara del último latido."""
    stale_cameras: tuple[tuple[float, float], ...] = ()
    """Cámaras de un latido anterior (no deben contar)."""
    orphan_clips: int = 0
    orphan_clips_outside: int = 0
    """Huérfanos fuera de la ventana de 24 h (no deben contar)."""
    day_clips: int = 0
    verification_orphans: int = 0
    """Clips de verificación emitidos en la ventana (nunca cuentan, nota de BR-GOB-94)."""
    last_update_result: str | None = None
    target_version: str | None = None
    live_view_local_url: str | None = None

    def inputs(self, now: datetime, productive_zone: bool) -> WarningInputs:
        """Lo que el evaluador de referencia recibe de este estado en ``now``."""
        return WarningInputs(
            status=self.status,
            decommissioned_at=now if self.decommissioned else None,
            last_heartbeat_at=(
                None if self.heartbeat_age_ms is None else now - _delta(self.heartbeat_age_ms)
            ),
            heartbeat_interval_seconds=self.interval_seconds or 60,
            pending=self.pending if self.heartbeat_age_ms is not None else None,
            oldest_pending_at=(
                None
                if self.oldest_pending_age_ms is None or self.heartbeat_age_ms is None
                else _ms(now - _delta(self.oldest_pending_age_ms))
            ),
            offset_ms=self.offset_ms if self.heartbeat_age_ms is not None else None,
            retires_at=self.retires_at if self.heartbeat_age_ms is not None else None,
            adapter=self.adapter if self.heartbeat_age_ms is not None else None,
            productive_zone=productive_zone,
            certificate_expires_at=(
                None if self.certificate_in_ms is None else now + _delta(self.certificate_in_ms)
            ),
            cameras=(
                tuple(CameraReading(m, d) for m, d in self.cameras)
                if self.heartbeat_age_ms is not None
                else ()
            ),
            orphan_clips=self.orphan_clips,
            day_clips=self.day_clips,
        )


def _delta(ms: int) -> timedelta:
    return timedelta(milliseconds=ms)


def _ms(moment: datetime) -> datetime:
    """Al milisegundo (como las marcas del contrato dentro de ``local_queue``)."""
    return moment.replace(microsecond=moment.microsecond - moment.microsecond % 1000)


@dataclass
class InventoryWorld:
    """Un cliente con dos plantas, sus personas y sus nodos sobre ``FleetStack``."""

    stack: FleetStack
    site: Site
    admin: Who
    installer: Who
    nodes: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    """``node_id → plant_id`` de los nodos declarados."""

    @classmethod
    def build(cls, stack: FleetStack, *, zones: int = 3) -> InventoryWorld:
        site = stack.site(plants=2, zones=zones)
        return cls(
            stack=stack,
            site=site,
            admin=stack.member(site),
            installer=stack.installer(site),
        )

    @property
    def plants(self) -> list[uuid.UUID]:
        return list(self.site.plants)

    def zones(self, plant: uuid.UUID) -> tuple[uuid.UUID, ...]:
        return self.site.plants[plant]

    @property
    def organization(self) -> uuid.UUID:
        return self.site.organization_id

    def member(
        self, level: ScopeLevel = ScopeLevel.ORGANIZATION, scope: uuid.UUID | None = None
    ) -> Who:
        return self.stack.member(self.site, Role.ADMINISTRATOR, level, scope)

    def now(self) -> datetime:
        return self.stack.authz.now()

    # --- Nodos ----------------------------------------------------------------------------------

    def declare(self, plant: uuid.UUID, zones: Sequence[uuid.UUID] = ()) -> uuid.UUID:
        response = self.stack.declare(self.installer, plant, list(zones))
        assert response.status_code == 201, response.text
        node_id = uuid.UUID(response.json()["node_id"])
        self.nodes[node_id] = plant
        return node_id

    def add_node(self, plant: uuid.UUID, zones: Sequence[uuid.UUID] = ()) -> uuid.UUID:
        """Un nodo sembrado como superusuario (sin pasar por la declaración: sin registro de
        comunicación), con sus asignaciones vigentes. Para sembrar muchos deprisa."""
        node = uuid.uuid4()
        created = self.now() - timedelta(days=2)
        operator = self.stack.authz.operator_id
        self.stack.execute(
            "INSERT INTO identity.node_identity (node_id, organization_id, plant_id, code, status,"
            " created_at) VALUES ($1, $2, $3, $4, 'enrolled', $5)",
            node,
            self.organization,
            plant,
            new_code(),
            created,
        )
        self.stack.execute(
            "INSERT INTO fleet.node_fleet_record (node_id, organization_id, plant_id, declared_at,"
            " declared_by) VALUES ($1, $2, $3, $4, $5)",
            node,
            self.organization,
            plant,
            created,
            operator,
        )
        for zone in zones:
            self.stack.execute(
                "INSERT INTO identity.zone_node_assignment (assignment_id, organization_id,"
                " plant_id, zone_id, node_id, assigned_at, assigned_by)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7)",
                uuid.uuid4(),
                self.organization,
                plant,
                zone,
                node,
                created,
                operator,
            )
        self.nodes[node] = plant
        return node

    def add_catalog(self, zone: uuid.UUID, cameras: Sequence[tuple[uuid.UUID, str]]) -> None:
        """Versión vigente del catálogo de la zona con sus cámaras (``camera_id``, ``code``)."""
        plant = next(p for p, zones in self.site.plants.items() if zone in zones)
        self.stack.execute(
            "INSERT INTO catalog.zone_catalog_version (organization_id, plant_id, zone_id,"
            " catalog_version, issued_at, issued_by, role_in_use, reason_es, changed_fields,"
            " payload, envelope, single_occupancy, ledger_record_id)"
            " VALUES ($1, $2, $3, 1, $4, $5, 'administrator', 'Catálogo sintético de prueba',"
            " ARRAY['cameras'], $6, '{}', false, $7)",
            self.organization,
            plant,
            zone,
            self.now(),
            self.stack.authz.operator_id,
            json.dumps({"cameras": [{"camera_id": str(c), "code": code} for c, code in cameras]}),
            uuid.uuid4(),
        )

    def communication(
        self, node: uuid.UUID, state: str, since: datetime, last_heartbeat_at: datetime | None
    ) -> None:
        """Un ``node_communication_state_changed`` por el escritor real (como la tarea de mudos)."""
        content: dict[str, Any] = {
            "node_id": str(node),
            "state": state,
            "since": format_timestamp(since),
        }
        if last_heartbeat_at is not None:
            content["last_heartbeat_at"] = format_timestamp(last_heartbeat_at)
        context = unit_context(self.organization, ActorUnit.U03, kind=ActorKind.SYSTEM)
        written = self.stack.run(
            self.stack.writer.write(
                context,
                "node_communication_state_changed",
                content,
                scope=RecordScope(plant_id=self.nodes[node]),
                occurred_at=since,
            )
        )
        assert not isinstance(written, LedgerRejection), written

    def fetch_cameras(self, node: uuid.UUID) -> list[uuid.UUID]:
        """Las cámaras del nodo en ``camera_inventory`` (de todos sus latidos), la más nueva
        primero."""
        rows = self.stack.fetch(
            "SELECT camera_id FROM fleet.camera_inventory WHERE node_id = $1"
            " ORDER BY updated_at DESC, camera_id",
            node,
        )
        return [uuid.UUID(str(row["camera_id"])) for row in rows]

    def next_now(self) -> datetime:
        """El instante de la próxima petición (``FleetStack.send`` avanza un segundo antes)."""
        return self.now() + timedelta(seconds=1)

    def write_state(self, node: uuid.UUID, state: NodeState, now: datetime) -> None:
        """Deja ``state`` en las tablas del nodo (superusuario), relativo a ``now``."""
        self.write_states({node: state}, now)

    def write_states(self, states: Mapping[uuid.UUID, NodeState], now: datetime) -> None:
        """``write_state`` de varios nodos en una sola transacción del superusuario."""
        statements: list[tuple[str, tuple[Any, ...]]] = []
        for node, state in states.items():
            self._state_statements(node, state, now, statements)
        admin = self.stack.authz.sessions.admin

        async def apply() -> None:
            async with admin.transaction():
                for sql, args in statements:
                    await admin.execute(sql, *args)

        self.stack.run(apply())

    def _state_statements(
        self,
        node: uuid.UUID,
        state: NodeState,
        now: datetime,
        statements: list[tuple[str, tuple[Any, ...]]],
    ) -> None:
        plant = self.nodes[node]
        org = self.organization

        def execute(sql: str, *args: Any) -> None:
            statements.append((sql, args))

        execute(
            "UPDATE identity.node_identity SET status = $2 WHERE node_id = $1", node, state.status
        )
        # La baja exige la revocación (D-14): revocada hace 2 h, de baja hace 1 h.
        execute(
            "UPDATE fleet.node_fleet_record SET revoked_at = $2, revocation_reason_es = $3,"
            " decommissioned_at = $4, live_view_local_url = $5 WHERE node_id = $1",
            node,
            now - timedelta(hours=2) if state.decommissioned else None,
            "Equipo retirado en la prueba" if state.decommissioned else None,
            now - timedelta(hours=1) if state.decommissioned else None,
            state.live_view_local_url,
        )
        execute("DELETE FROM fleet.node_configuration WHERE node_id = $1", node)
        if state.interval_seconds is not None:
            execute(
                "INSERT INTO fleet.node_configuration (node_id, organization_id, plant_id,"
                " time_sources, heartbeat_interval_seconds, mute_after_seconds, updated_at)"
                " VALUES ($1, $2, $3, '[\"pool.ntp.org\"]', $4, $5, $6)",
                node,
                org,
                plant,
                state.interval_seconds,
                5 * state.interval_seconds,
                now,
            )
        execute("DELETE FROM fleet.camera_inventory WHERE node_id = $1", node)
        execute("DELETE FROM fleet.zone_node_state WHERE node_id = $1", node)
        execute("DELETE FROM fleet.node_inventory WHERE node_id = $1", node)
        if state.heartbeat_age_ms is not None:
            heard = now - _delta(state.heartbeat_age_ms)
            queue: dict[str, Any] = {
                "pending": state.pending,
                "dead_letter": [{"code": "schema_invalid", "count": 2}],
                "retained_sent": 0,
            }
            if state.oldest_pending_age_ms is not None:
                queue["oldest_pending_at"] = format_timestamp(
                    now - _delta(state.oldest_pending_age_ms)
                )
            notice: dict[str, Any] = {"result": "accepted"}
            if state.retires_at is not None:
                notice = {"result": "accepted_with_notice", "retires_at": state.retires_at}
            execute(
                "INSERT INTO fleet.node_inventory (node_id, organization_id, plant_id,"
                " software_version, contract_version, model_version, contract_notice,"
                " last_heartbeat_at, communication_state, local_queue, clock, signal_reader,"
                " uptime_seconds, target_version, last_update_result, updated_at)"
                " VALUES ($1, $2, $3, '1.4.0', '1.0.0', 'modelo-1', $4, $5, 'reachable', $6, $7,"
                " $8, 120, $9, $10, $5)",
                node,
                org,
                plant,
                json.dumps(notice),
                heard,
                json.dumps(queue),
                json.dumps({"synchronized": True, "offset_ms": state.offset_ms}),
                json.dumps({"available": True, "adapter": state.adapter}),
                state.target_version,
                state.last_update_result,
            )
            rows = [(m, d, heard) for m, d in state.cameras] + [
                (m, d, heard - timedelta(minutes=5)) for m, d in state.stale_cameras
            ]
            execute(
                "INSERT INTO fleet.camera_inventory (organization_id, plant_id, node_id,"
                " camera_id, connected, measured_fps, declared_min_fps, observability_state,"
                " updated_at)"
                " SELECT $1, $2, $3, gen_random_uuid(), true, c.measured, c.declared,"
                " 'observable', c.at FROM unnest($4::double precision[],"
                " $5::double precision[], $6::timestamptz[]) AS c(measured, declared, at)",
                org,
                plant,
                node,
                [row[0] for row in rows],
                [row[1] for row in rows],
                [row[2] for row in rows],
            )
        execute("DELETE FROM fleet.node_credential WHERE node_id = $1", node)
        if state.certificate_in_ms is not None:
            expires = now + _delta(state.certificate_in_ms)
            execute(
                "INSERT INTO fleet.node_credential (credential_id, organization_id, plant_id,"
                " node_id, certificate_serial, subject, issued_at, expires_at, status)"
                " VALUES ($1, $2, $3, $4, $5, jsonb_build_object('node_id', $4::uuid::text,"
                " 'organization_id', $2::uuid::text, 'plant_id', $3::uuid::text),"
                " $6::timestamptz - interval '365 days', $6, 'active')",
                uuid.uuid4(),
                org,
                plant,
                node,
                secrets.token_hex(20),
                expires,
            )
        # Una credencial revocada que vence enseguida nunca cuenta (no es vigente).
        execute(
            "INSERT INTO fleet.node_credential (credential_id, organization_id, plant_id,"
            " node_id, certificate_serial, subject, issued_at, expires_at, status, revoked_at)"
            " VALUES ($1, $2, $3, $4, $5, jsonb_build_object('node_id', $4::uuid::text,"
            " 'organization_id', $2::uuid::text, 'plant_id', $3::uuid::text),"
            " $6::timestamptz - interval '365 days', $6, 'revoked', $6::timestamptz"
            " - interval '1 day')",
            uuid.uuid4(),
            org,
            plant,
            node,
            secrets.token_hex(20),
            now + timedelta(hours=1),
        )
        self._clips(node, plant, state, now, execute)

    def _clips(
        self,
        node: uuid.UUID,
        plant: uuid.UUID,
        state: NodeState,
        now: datetime,
        execute: Callable[..., None],
    ) -> None:
        execute("DELETE FROM fleet.clip_upload_grant WHERE node_id = $1", node)
        zone = self.zones(plant)[-1]
        prefix = f"org/{self.organization}/plant/{plant}/zone/{zone}/node/{node}/"
        headers = json.dumps({"x-amz-checksum-sha256": "x", "x-amz-meta-vigia-anonymized": "true"})
        # (cuántas, purpose, status, emitida hace, huérfana hace)
        batches = (
            (state.orphan_clips, "evidence", "orphan", timedelta(hours=30), timedelta(hours=2)),
            (
                state.orphan_clips_outside,
                "evidence",
                "orphan",
                timedelta(hours=60),
                timedelta(hours=30),
            ),
            (state.day_clips, "evidence", "issued", timedelta(hours=1), None),
            (state.verification_orphans, "verification", "used", timedelta(hours=1), None),
        )
        for count, purpose, status, issued_ago, orphaned_ago in batches:
            if count == 0:
                continue
            issued = now - issued_ago
            execute(
                "INSERT INTO fleet.clip_upload_grant (clip_id, organization_id, plant_id, zone_id,"
                " node_id, purpose, storage_key, content_type, max_size_bytes, required_headers,"
                " issued_at, expires_at, status, used_at, orphaned_at)"
                " SELECT c.id, $1, $2, $3, $4, $5, $6 || c.id::text || '.mp4', 'video/mp4', 1000,"
                " $7::jsonb, $8::timestamptz, $8::timestamptz + interval '10 minutes', $9,"
                " CASE WHEN $9 IN ('used', 'orphan') THEN $8::timestamptz + interval '1 minute'"
                " END, $10::timestamptz"
                " FROM (SELECT gen_random_uuid() AS id FROM generate_series(1, $11)) AS c",
                self.organization,
                plant,
                zone,
                node,
                purpose,
                prefix,
                headers,
                issued,
                status,
                None if orphaned_ago is None else now - orphaned_ago,
                count,
            )

    # --- Compuertas y umbrales -------------------------------------------------------------------

    def set_gate(self, zone: uuid.UUID, *, mounting: str, usage: str) -> None:
        """La fila de ``catalog.zone_gate_state`` de la zona (superusuario)."""
        plant = next(p for p, zones in self.site.plants.items() if zone in zones)
        mode = (
            "no_capture"
            if mounting != "approved"
            else ("commissioning" if usage != "approved" else "productive")
        )
        now = self.now()
        self.stack.execute("DELETE FROM catalog.zone_gate_state WHERE zone_id = $1", zone)
        self.stack.execute(
            "INSERT INTO catalog.zone_gate_state (zone_id, organization_id, plant_id, mounting,"
            " usage, resulting_mode, issued_at, envelope, valid_until)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, '{}', $7::timestamptz + interval '7 days')",
            zone,
            self.organization,
            plant,
            json.dumps({"status": mounting}),
            json.dumps({"status": usage}),
            mode,
            now,
        )

    def write_thresholds(self, plant: uuid.UUID, thresholds: FleetThresholds | None) -> None:
        """Fija (o borra) la fila de umbrales de la planta como superusuario."""
        self.stack.execute("DELETE FROM fleet.plant_fleet_thresholds WHERE plant_id = $1", plant)
        if thresholds is None:
            return
        self.stack.execute(
            "INSERT INTO fleet.plant_fleet_thresholds (plant_id, organization_id,"
            " queue_pending_threshold, queue_age_threshold_minutes, clock_drift_threshold_ms,"
            " updated_by, updated_at) VALUES ($1, $2, $3, $4, $5, $6, $7)",
            plant,
            self.organization,
            thresholds.queue_pending_threshold,
            thresholds.queue_age_threshold_minutes,
            thresholds.clock_drift_threshold_ms,
            self.stack.authz.operator_id,
            self.now(),
        )

    # --- Peticiones -----------------------------------------------------------------------------

    def get(self, who: Who, path: str, params: dict[str, str] | None = None) -> httpx.Response:
        return self.stack.send(who, "GET", path, params=params)

    def list_all(self, who: Who, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
        """Todas las páginas de ``GET /fleet/nodes``."""
        found: list[dict[str, Any]] = []
        query = dict(params or {})
        while True:
            response = self.get(who, "/fleet/nodes", query)
            assert response.status_code == 200, response.text
            body = response.json()
            found += body["nodes"]
            if body["next_after"] is None:
                return found
            query["after"] = body["next_after"]


@contextlib.contextmanager
def counting(stack: FleetStack) -> Iterator[StatementLog]:
    """Las sentencias que la aplicación envía al servidor dentro del bloque."""
    log = StatementLog()
    engines = [
        pool.engine.sync_engine
        for pool in stack.database._pools.values()
        if getattr(pool, "engine", None) is not None
    ]

    def before(*arguments: Any) -> None:
        log.statements.append(str(arguments[2]))

    for engine in engines:
        event.listen(engine, "before_cursor_execute", before)
    try:
        yield log
    finally:
        for engine in engines:
            event.remove(engine, "before_cursor_execute", before)


def as_superuser(stack: FleetStack, statement: Any, parameters: Mapping[str, Any]) -> list[Any]:
    """Ejecuta una sentencia ``text()`` de la aplicación como **superusuario** (sin RLS).

    Comprueba los filtros explícitos de organización, planta y zona de la sentencia (defensa en
    profundidad sobre la RLS): sin ellos, el superusuario vería las filas de otra organización.
    """
    compiled = statement.compile(dialect=asyncpg_dialect())
    order = compiled.positiontup or []
    rows: list[Any] = stack.fetch(str(compiled), *(parameters[name] for name in order))
    return rows


def fingerprint(stack: FleetStack, organization_id: uuid.UUID) -> dict[str, str]:
    """Huella (``md5`` de las filas en orden) de cada tabla de ``INVENTORY_WRITTEN_TABLES``."""
    prints: dict[str, str] = {}
    for table in INVENTORY_WRITTEN_TABLES:
        (row,) = stack.fetch(
            f"SELECT md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) AS h"  # noqa: S608
            f" FROM {table} AS t WHERE t.organization_id = $1",
            organization_id,
        )
        prints[table] = str(row["h"])
    return prints
