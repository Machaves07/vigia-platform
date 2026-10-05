"""Filas sintéticas del esquema ``fleet`` para las pruebas (TASK-203).

``ROW_BUILDERS`` da, por tabla, la sentencia y los parámetros de una fila nueva y válida de una
planta (``FleetScope``): ``seed_fleet`` siembra con ellos filas de cada tabla en cada planta de
los dos clientes de ``tests/identity_db.py`` (y la marca por organización y un intento de alta de
un nodo desconocido, sin planta), y las pruebas de aislamiento los reutilizan para intentar
escribir desde un contexto de proveedor. Cada constructor escribe **solo** en su tabla: así, en
la sonda negativa, la política que decide es la suya.

Solo datos generados (NFR-CTR-43). Las marcas salen de ``BASE_TIME``, nunca de la hora real; las
filas de las tablas particionadas caen en la partición por defecto salvo que la prueba pase otra
marca.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from tests.identity_db import BASE_TIME, IdentitySeed, Tenant, set_scope

FLEET_TABLES = (
    "node_fleet_record",
    "revocation_list_dirty",
    "enrollment_code",
    "enrollment_attempt",
    "node_credential",
    "node_inventory",
    "camera_inventory",
    "zone_node_state",
    "heartbeat_history",
    "fleet_alarm",
    "open_fleet_alarm",
    "plant_fleet_thresholds",
    "target_version_publication",
    "update_result",
    "clip_upload_grant",
    "verification_clip",
    "node_configuration",
)
"""Las 17 tablas de ``fleet`` con organización (``gob_0018``)."""

GLOBAL_TABLE = "revocation_list_publication"

REVOCATION_STATE_TABLE = "revocation_list_state"
"""``gob_0021`` (TASK-218): marca única de la lista de revocación, una fila global **sin** RLS ni
datos de cliente (excepción documentada: la sube la revocación de un instalador bajo concesión de
planta, que la política ``operator_only`` no dejaría escribir)."""
REVOCATION_STATE_COLUMNS = frozenset(
    {
        "dirty_generation",
        "dirty_since",
        "published_generation",
        "published_at",
        "object_version_id",
        "next_update",
        "entries",
    }
)

ORPHAN_CLOSE_TABLE = "observability_orphan_close"
"""``gob_0024`` (TASK-221): marca ⛓ del cierre huérfano de un evento de observabilidad, con RLS y
las dos políticas de las tablas de ``fleet``, ``SELECT`` e ``INSERT`` para ``vigia_app``."""

APPEND_ONLY_TABLES = (
    "enrollment_attempt",
    "heartbeat_history",
    "fleet_alarm",
    "target_version_publication",
    "update_result",
    "verification_clip",
)
"""Tablas ⛓ de ``fleet``."""

PARTITIONED_TABLES = {
    "heartbeat_history": "received_at",
    "enrollment_attempt": "attempted_at",
    "fleet_alarm": "raised_at",
}

ORGANIZATION_TABLES = frozenset({"revocation_list_dirty"})
"""Sin planta: solo una concesión de toda la organización la alcanza."""

SINGLETONS = frozenset(
    {
        "node_fleet_record",
        "revocation_list_dirty",
        "node_inventory",
        "zone_node_state",
        "plant_fleet_thresholds",
        "node_configuration",
    }
)
"""Una fila por nodo, por (nodo, zona), por planta o por organización: una segunda choca por
clave."""

READ_ONLY_FOR_APP = frozenset({"open_fleet_alarm"})
"""``vigia_app`` solo la lee: la escriben los disparadores de ``fleet_alarm``."""

SEMVER = "1.4.0"
FINGERPRINT = "ab" * 32


@dataclass(frozen=True)
class FleetScope:
    """Planta de un cliente con su zona, su nodo, su usuario y una concesión de clip de
    verificación sin clip confirmado (para escribir solo en ``verification_clip``)."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    node_id: uuid.UUID
    user_id: uuid.UUID
    clip_id: uuid.UUID = dataclasses.field(default_factory=uuid.uuid4)


Builder = Callable[[FleetScope], tuple[str, list[Any]]]


def _json(value: Any) -> str:
    return json.dumps(value)


def exact(text: str | bytes) -> Any:
    """Un documento JSON leído sin pérdida (enteros exactos, decimales como ``Decimal``), con
    ``json`` de la biblioteca estándar: independiente de ``table_archive.parse_row``."""
    return json.loads(text, parse_float=Decimal)


def json_order(document: Any) -> str:
    """Clave de orden estable de un documento leído con ``exact``."""
    return json.dumps(document, sort_keys=True, default=str)


def _hex() -> str:
    return secrets.token_hex(32)


def node_fleet_record(scope: FleetScope) -> tuple[str, list[Any]]:
    return (
        "INSERT INTO fleet.node_fleet_record (node_id, organization_id, plant_id, declared_at,"
        " declared_by) VALUES ($1, $2, $3, $4, $5)",
        [scope.node_id, scope.organization_id, scope.plant_id, BASE_TIME, scope.user_id],
    )


def revocation_list_dirty(scope: FleetScope) -> tuple[str, list[Any]]:
    return (
        "INSERT INTO fleet.revocation_list_dirty (organization_id, dirty, updated_at)"
        " VALUES ($1, false, $2)",
        [scope.organization_id, BASE_TIME],
    )


def enrollment_code(
    scope: FleetScope, code_id: uuid.UUID | None = None, *, status: str = "superseded"
) -> tuple[str, list[Any]]:
    """Código de alta; por omisión ``superseded`` (se puede repetir; el activo es único)."""
    return (
        "INSERT INTO fleet.enrollment_code (code_id, organization_id, plant_id, node_id,"
        " code_hash, code_salt, issued_at, issued_by, expires_at, disclosed_at, status,"
        " ledger_record_id) VALUES ($1, $2, $3, $4, $5, $6, $7::timestamptz, $8,"
        " $7::timestamptz + interval '24 hours', $7::timestamptz, $9, $10)",
        [
            code_id or uuid.uuid4(),
            scope.organization_id,
            scope.plant_id,
            scope.node_id,
            _hex(),
            secrets.token_bytes(16),
            BASE_TIME,
            scope.user_id,
            status,
            uuid.uuid4(),
        ],
    )


def enrollment_attempt(
    scope: FleetScope,
    attempted_at: dt.datetime = BASE_TIME,
    *,
    known_node: bool = True,
    attempt_id: uuid.UUID | None = None,
) -> tuple[str, list[Any]]:
    """Intento de alta; con ``known_node=False``, sin nodo ni planta (código desconocido)."""
    return (
        "INSERT INTO fleet.enrollment_attempt (attempt_id, organization_id, plant_id, node_id,"
        " presented_code_hash, hardware_fingerprint, software_version, contract_version, result,"
        " attempted_at, source_ip_hash, correlation_id)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, $7, $8, $9, $10, $11)",
        [
            attempt_id or uuid.uuid4(),
            scope.organization_id,
            scope.plant_id if known_node else None,
            scope.node_id if known_node else None,
            _hex(),
            FINGERPRINT,
            SEMVER,
            "enrollment_code_invalid" if not known_node else "enrollment_code_expired",
            attempted_at,
            _hex(),
            uuid.uuid4(),
        ],
    )


def node_credential(
    scope: FleetScope,
    credential_id: uuid.UUID | None = None,
    *,
    status: str = "active",
    serial: str | None = None,
) -> tuple[str, list[Any]]:
    revoked = status == "revoked"
    return (
        "INSERT INTO fleet.node_credential (credential_id, organization_id, plant_id, node_id,"
        " certificate_serial, subject, issued_at, expires_at, status, revoked_at)"
        " VALUES ($1, $2, $3, $4, $5, jsonb_build_object('node_id', $4::uuid::text,"
        " 'organization_id', $2::uuid::text, 'plant_id', $3::uuid::text), $6::timestamptz,"
        " $6::timestamptz + interval '365 days', $7,"
        " CASE WHEN $8::boolean THEN $6::timestamptz END)",
        [
            credential_id or uuid.uuid4(),
            scope.organization_id,
            scope.plant_id,
            scope.node_id,
            secrets.token_hex(20) if serial is None else serial,
            BASE_TIME,
            status,
            revoked,
        ],
    )


def node_inventory(scope: FleetScope) -> tuple[str, list[Any]]:
    return (
        "INSERT INTO fleet.node_inventory (node_id, organization_id, plant_id, software_version,"
        " contract_version, model_version, contract_notice, communication_state, local_queue,"
        " clock, signal_reader, uptime_seconds, updated_at)"
        " VALUES ($1, $2, $3, $4, $4, 'model-1', '{\"result\": \"accepted\"}', 'reachable',"
        ' \'{"pending": 0}\', \'{"synchronized": true, "offset_ms": 3}\','
        ' \'{"available": true, "adapter": "modbus"}\', 120, $5)',
        [scope.node_id, scope.organization_id, scope.plant_id, SEMVER, BASE_TIME],
    )


def camera_inventory(
    scope: FleetScope, camera_id: uuid.UUID | None = None
) -> tuple[str, list[Any]]:
    return (
        "INSERT INTO fleet.camera_inventory (organization_id, plant_id, node_id, camera_id,"
        " connected, measured_fps, declared_min_fps, observability_state, updated_at)"
        " VALUES ($1, $2, $3, $4, true, 12.5, 5, 'observable', $5)",
        [
            scope.organization_id,
            scope.plant_id,
            scope.node_id,
            camera_id or uuid.uuid4(),
            BASE_TIME,
        ],
    )


def zone_node_state(scope: FleetScope) -> tuple[str, list[Any]]:
    return (
        "INSERT INTO fleet.zone_node_state (organization_id, plant_id, node_id, zone_id, mode,"
        " observability_state, catalog_version_in_node, gate_state_valid_until, open_episodes,"
        " coverage_ok, updated_at)"
        " VALUES ($1, $2, $3, $4, 'commissioning', 'observable', 1, $5::timestamptz"
        " + interval '7 days', 0, true, $5)",
        [scope.organization_id, scope.plant_id, scope.node_id, scope.zone_id, BASE_TIME],
    )


def heartbeat(
    scope: FleetScope,
    received_at: dt.datetime = BASE_TIME,
    *,
    heartbeat_id: uuid.UUID | None = None,
    summary: dict[str, Any] | None = None,
) -> tuple[str, list[Any]]:
    return (
        "INSERT INTO fleet.heartbeat_history (heartbeat_id, organization_id, plant_id, node_id,"
        " received_at, sent_at, payload_summary)"
        " VALUES ($1, $2, $3, $4, $5::timestamptz, $5::timestamptz - interval '1 second', $6)",
        [
            heartbeat_id or uuid.uuid4(),
            scope.organization_id,
            scope.plant_id,
            scope.node_id,
            received_at,
            _json(summary if summary is not None else {"pending": 0, "cameras": 1}),
        ],
    )


def fleet_alarm(
    scope: FleetScope,
    raised_at: dt.datetime = BASE_TIME,
    *,
    kind: str = "clock_drift",
    cleared: bool = True,
    alarm_id: uuid.UUID | None = None,
) -> tuple[str, list[Any]]:
    """Alarma; cerrada por omisión (no ocupa la ranura de la abierta y se puede repetir)."""
    return (
        "INSERT INTO fleet.fleet_alarm (alarm_id, organization_id, plant_id, alarm_kind, node_id,"
        " raised_at, cleared_at, raised_event_id, cleared_event_id)"
        " VALUES ($1, $2, $3, $4, $5, $6::timestamptz,"
        " CASE WHEN $7::boolean THEN $6::timestamptz + interval '1 minute' END, $8,"
        " CASE WHEN $7::boolean THEN gen_random_uuid() END)",
        [
            alarm_id or uuid.uuid4(),
            scope.organization_id,
            scope.plant_id,
            kind,
            scope.node_id,
            raised_at,
            cleared,
            uuid.uuid4(),
        ],
    )


def open_fleet_alarm(scope: FleetScope) -> tuple[str, list[Any]]:
    """Alta directa en la proyección: ``vigia_app`` no tiene ``INSERT`` (solo los disparadores)."""
    return (
        "INSERT INTO fleet.open_fleet_alarm (organization_id, plant_id, alarm_kind, node_id)"
        " VALUES ($1, $2, 'node_mute', $3)",
        [scope.organization_id, scope.plant_id, scope.node_id],
    )


def plant_fleet_thresholds(scope: FleetScope) -> tuple[str, list[Any]]:
    return (
        "INSERT INTO fleet.plant_fleet_thresholds (plant_id, organization_id, updated_by,"
        " updated_at) VALUES ($1, $2, $3, $4)",
        [scope.plant_id, scope.organization_id, scope.user_id, BASE_TIME],
    )


def target_version_publication(scope: FleetScope) -> tuple[str, list[Any]]:
    return (
        "INSERT INTO fleet.target_version_publication (publication_id, organization_id,"
        " plant_id, target_version, node_ids, maintenance_window_from, maintenance_window_to,"
        " published_by, published_at, ledger_record_id)"
        " VALUES ($1, $2, $3, '1.5.0', ARRAY[$4::uuid], $5::timestamptz,"
        " $5::timestamptz + interval '2 hours', $6, $5::timestamptz, $7)",
        [
            uuid.uuid4(),
            scope.organization_id,
            scope.plant_id,
            scope.node_id,
            BASE_TIME,
            scope.user_id,
            uuid.uuid4(),
        ],
    )


def update_result(scope: FleetScope) -> tuple[str, list[Any]]:
    return (
        "INSERT INTO fleet.update_result (update_result_id, organization_id, plant_id, node_id,"
        " target_version, result, reported_at, ledger_record_id)"
        " VALUES ($1, $2, $3, $4, '1.5.0', 'failed', $5, $6)",
        [
            uuid.uuid4(),
            scope.organization_id,
            scope.plant_id,
            scope.node_id,
            BASE_TIME,
            uuid.uuid4(),
        ],
    )


def storage_key(scope: FleetScope, clip_id: uuid.UUID, ext: str = "mp4") -> str:
    return (
        f"org/{scope.organization_id}/plant/{scope.plant_id}/zone/{scope.zone_id}"
        f"/node/{scope.node_id}/{clip_id}.{ext}"
    )


HEADERS = {"x-amz-checksum-sha256": "c2hh", "x-amz-meta-vigia-anonymized": "true"}


def clip_upload_grant(
    scope: FleetScope,
    clip_id: uuid.UUID | None = None,
    *,
    purpose: str = "evidence",
    status: str = "issued",
) -> tuple[str, list[Any]]:
    clip_id = clip_id or uuid.uuid4()
    used = status in ("used", "orphan")
    return (
        "INSERT INTO fleet.clip_upload_grant (clip_id, organization_id, plant_id, zone_id,"
        " node_id, purpose, storage_key, content_type, max_size_bytes, required_headers,"
        " issued_at, expires_at, status, used_at, orphaned_at)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, 'video/mp4', 52428800, $8, $9::timestamptz,"
        " $9::timestamptz + interval '15 minutes', $10,"
        " CASE WHEN $11::boolean THEN $9::timestamptz + interval '1 minute' END,"
        " CASE WHEN $10 = 'orphan' THEN $9::timestamptz + interval '1 day' END)",
        [
            clip_id,
            scope.organization_id,
            scope.plant_id,
            scope.zone_id,
            scope.node_id,
            purpose,
            storage_key(scope, clip_id),
            _json(HEADERS),
            BASE_TIME,
            status,
            used,
        ],
    )


def verification_clip(scope: FleetScope, clip_id: uuid.UUID | None = None) -> tuple[str, list[Any]]:
    """El clip de verificación de la concesión ``clip_id`` (por omisión, la del ``FleetScope``)."""
    return (
        "INSERT INTO fleet.verification_clip (clip_id, organization_id, plant_id, zone_id,"
        " node_id, received_at, sha256) VALUES ($1, $2, $3, $4, $5, $6, $7)",
        [
            clip_id or scope.clip_id,
            scope.organization_id,
            scope.plant_id,
            scope.zone_id,
            scope.node_id,
            BASE_TIME,
            _hex(),
        ],
    )


def _verification_clip_with_grant(scope: FleetScope) -> tuple[str, list[Any]]:
    # Para sembrar: un clip por concesión, así que confirma una concesión propia, creada en la
    # misma sentencia.
    clip_id = uuid.uuid4()
    grant_sql, grant_args = clip_upload_grant(scope, clip_id, purpose="verification", status="used")
    return (
        f"WITH grant_row AS ({grant_sql} RETURNING clip_id)"  # noqa: S608
        " INSERT INTO fleet.verification_clip (clip_id, organization_id, plant_id, zone_id,"
        " node_id, received_at, sha256)"
        " SELECT clip_id, $2, $3, $4, $5, $9, $12 FROM grant_row",
        [*grant_args, _hex()],
    )


def node_configuration(scope: FleetScope) -> tuple[str, list[Any]]:
    return (
        "INSERT INTO fleet.node_configuration (node_id, organization_id, plant_id, time_sources,"
        ' updated_at) VALUES ($1, $2, $3, \'[{"kind": "ntp", "host": "ntp.local"}]\', $4)',
        [scope.node_id, scope.organization_id, scope.plant_id, BASE_TIME],
    )


ROW_BUILDERS: dict[str, Builder] = {
    "node_fleet_record": node_fleet_record,
    "revocation_list_dirty": revocation_list_dirty,
    "enrollment_code": enrollment_code,
    "enrollment_attempt": enrollment_attempt,
    "node_credential": lambda scope: node_credential(scope, status="superseded"),
    "node_inventory": node_inventory,
    "camera_inventory": camera_inventory,
    "zone_node_state": zone_node_state,
    "heartbeat_history": heartbeat,
    "fleet_alarm": fleet_alarm,
    "open_fleet_alarm": open_fleet_alarm,
    "plant_fleet_thresholds": plant_fleet_thresholds,
    "target_version_publication": target_version_publication,
    "update_result": update_result,
    "clip_upload_grant": clip_upload_grant,
    "verification_clip": verification_clip,
    "node_configuration": node_configuration,
}
"""Una fila nueva por tabla, que solo escribe en esa tabla."""

assert tuple(ROW_BUILDERS) == FLEET_TABLES


def fleet_scopes(tenant: Tenant) -> tuple[FleetScope, ...]:
    return tuple(
        FleetScope(
            tenant.organization_id, plant.plant_id, plant.zone_id, plant.node_id, tenant.user_id
        )
        for plant in tenant.plants
    )


async def seed_plant(connection: Any, scope: FleetScope) -> None:
    """Una fila de cada tabla de planta, una alarma abierta (su ranura) y la concesión de clip de
    verificación del ``FleetScope`` sin clip (como superusuario, dentro de una transacción).

    Con el contexto de la organización: el disparador de la alarma abierta es ``SECURITY
    DEFINER`` de ``vigia_migrate`` y la RLS forzada de la ranura le sigue aplicando."""
    await set_scope(connection, scope.organization_id)
    for sql, args in (
        node_fleet_record(scope),
        enrollment_code(scope),
        enrollment_attempt(scope),
        node_credential(scope),
        node_inventory(scope),
        camera_inventory(scope),
        zone_node_state(scope),
        heartbeat(scope),
        fleet_alarm(scope),
        fleet_alarm(scope, kind="node_mute", cleared=False),
        plant_fleet_thresholds(scope),
        target_version_publication(scope),
        update_result(scope),
        clip_upload_grant(scope),
        clip_upload_grant(scope, scope.clip_id, purpose="verification", status="used"),
        _verification_clip_with_grant(scope),
        node_configuration(scope),
    ):
        await connection.execute(sql, *args)


@dataclass(frozen=True)
class FleetSeed:
    identity: IdentitySeed
    scopes: dict[uuid.UUID, tuple[FleetScope, ...]]
    """Plantas sembradas de cada organización cliente."""

    def plant(self, organization_id: uuid.UUID, index: int = 0) -> FleetScope:
        return self.scopes[organization_id][index]


async def seed_fleet(connection: Any, seed: IdentitySeed) -> FleetSeed:
    """Filas de cada tabla de ``fleet`` en cada planta de A y de B, la marca de cada organización
    y un intento de alta sin nodo por organización (superusuario)."""
    scopes: dict[uuid.UUID, tuple[FleetScope, ...]] = {}
    async with connection.transaction():
        for tenant in (seed.a, seed.b):
            scopes[tenant.organization_id] = fleet_scopes(tenant)
            first = scopes[tenant.organization_id][0]
            for sql, args in (
                revocation_list_dirty(first),
                enrollment_attempt(first, known_node=False),
            ):
                await connection.execute(sql, *args)
            for scope in scopes[tenant.organization_id]:
                await seed_plant(connection, scope)
    return FleetSeed(seed, scopes)
