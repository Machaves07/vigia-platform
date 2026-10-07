"""Mundo de dos organizaciones de U-03 para ``tests/isolation`` (TASK-228; PR-GOB-12; NFR-GOB-30).

El diseño lo llama ``two_organizations``; reutiliza los soportes de U-02 que ya existen
(``tests.authz_support.Site`` para la jerarquía, la ``AuthzEnvironment`` de la pila de cada prueba
y ``tests.node_api_db.issue`` con la autoridad efímera ``TestAuthority`` de NFR-GOB-63), sin
nombres paralelos:

- ``two_organizations`` siembra A y B con **dos plantas** cada una y **dos zonas** por planta; cada
  planta tiene **dos nodos** ``enrolled`` (fila de identidad, ficha de flota con alta y credencial
  ``active`` de la autoridad efímera), el primero asignado a la primera zona y el segundo a la
  segunda (desde hace 30 días). Así un nodo tiene en su misma planta una zona que **no** es suya
  (guarda de zona) y en la otra planta zonas y nodos ajenos (guarda de planta);
- ``seed_zone`` deja en cada zona lo que necesite el módulo: ``publish_zone`` publica el catálogo
  (cámaras con ``code``), las compuertas aprobadas y la proyección ``productive``; las pruebas de
  los puertos pasan el ``populate`` de ``tests.catalog_ports_support``;
- ``unique_node_code`` da códigos ``node_identity.code`` de 6 hexadecimales **únicos en el
  proceso** (la colisión intermitente de U-02 venía de códigos aleatorios que se repetían);
- ``fingerprint`` resume, como superusuario, cada tabla con ``organization_id`` de los esquemas de
  la plataforma (filas de una organización): antes y después de una petición, de una operación o
  de una iteración de tarea, la huella de la otra organización no cambia. La cadena de la planta
  (``ledger.ledger_record``), la auditoría y la bandeja (``shared.outbox_event``) entran en ella.

Solo datos generados (NFR-CTR-43). Las marcas salen del reloj simulado de la pila.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import secrets
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from cryptography import x509

from tests.authz_support import AuthzEnvironment, Site
from tests.node_api_db import DbNode, issue
from tests.node_api_support import DAY, TestAuthority
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "ISOLATION_SCHEMAS",
    "GobNode",
    "GobOrganization",
    "GobZone",
    "changed_tables",
    "fingerprint",
    "organization_tables",
    "publish_zone",
    "two_organizations",
    "unique_node_code",
]

ISOLATION_SCHEMAS: Final = ("identity", "ledger", "shared", "catalog", "fleet")
"""Los esquemas con datos de cliente: toda tabla suya con ``organization_id`` entra en la huella."""
SYNTHETIC_SIGNATURE: Final = "A" * 86 + "=="


# --- Códigos de nodo --------------------------------------------------------------------------


class _NodeCodes:
    """``ND-`` y 6 hexadecimales, sin repetir ninguno en el proceso."""

    def __init__(self) -> None:
        self._issued: set[str] = set()
        self._lock = threading.Lock()

    def next(self) -> str:
        with self._lock:
            while True:
                code = f"ND-{secrets.token_hex(3).upper()}"
                if code not in self._issued:
                    self._issued.add(code)
                    return code


_NODE_CODES: Final = _NodeCodes()


def unique_node_code() -> str:
    """Un ``node_identity.code`` de 6 hexadecimales que ninguna otra siembra del proceso usó."""
    return _NODE_CODES.next()


# --- Huellas ------------------------------------------------------------------------------------

Fetch = Callable[..., list[Any]]


def organization_tables(fetch: Fetch) -> list[str]:
    """Cada tabla (no partición) con ``organization_id`` de ``ISOLATION_SCHEMAS``."""
    rows = fetch(
        "SELECT DISTINCT n.nspname || '.' || c.relname AS name"
        " FROM pg_catalog.pg_attribute AS a"
        " JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid"
        " JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace"
        " WHERE n.nspname = ANY($1::text[])"
        " AND a.attname = 'organization_id' AND NOT a.attisdropped"
        " AND c.relkind IN ('r', 'p') AND NOT c.relispartition ORDER BY 1",
        list(ISOLATION_SCHEMAS),
    )
    return [str(row["name"]) for row in rows]


def fingerprint(
    fetch: Fetch, organization_id: uuid.UUID, tables: Sequence[str] | None = None
) -> dict[str, str]:
    """Huella (cuenta y ``md5`` de las filas en orden) de cada tabla, filas de la organización.

    Se calcula con la conexión de **superusuario** (sin RLS): ve lo que de verdad hay.
    """
    prints: dict[str, str] = {}
    for name in tables if tables is not None else organization_tables(fetch):
        (row,) = fetch(
            "SELECT count(*)::text || ':' || coalesce(md5(string_agg(to_jsonb(t)::text,"  # noqa: S608
            f" '|' ORDER BY to_jsonb(t)::text)), '') AS d FROM {name} AS t"
            " WHERE organization_id = $1",
            organization_id,
        )
        prints[name] = str(row["d"])
    return prints


def changed_tables(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    """Las tablas cuya huella cambió entre ``before`` y ``after``."""
    return sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))


# --- El mundo ---------------------------------------------------------------------------------


class Stack(Protocol):
    """Lo que el mundo necesita de una pila de prueba (``FleetStack``, ``HeartbeatStack``…)."""

    @property
    def authz(self) -> AuthzEnvironment: ...

    def run(self, awaitable: Any) -> Any: ...

    def execute(self, sql: str, *args: Any) -> None: ...


@dataclass(frozen=True)
class GobZone:
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    cameras: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class GobNode:
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    node_id: uuid.UUID
    code: str
    zone_id: uuid.UUID
    """La zona asignada al nodo desde ``assigned_at``."""
    assigned_at: dt.datetime
    certificate: x509.Certificate
    credential_id: uuid.UUID


@dataclass(frozen=True)
class GobOrganization:
    site: Site
    zones: tuple[tuple[GobZone, GobZone], ...]
    """Por planta, en el orden de ``site.plants``: (zona del primer nodo, zona del segundo)."""
    nodes: tuple[tuple[GobNode, GobNode], ...]
    """Por planta: (nodo de la primera zona, nodo de la segunda)."""

    @property
    def organization_id(self) -> uuid.UUID:
        return self.site.organization_id

    @property
    def plants(self) -> tuple[uuid.UUID, ...]:
        return tuple(self.site.plants)

    def zone(self, plant: int = 0, index: int = 0) -> GobZone:
        return self.zones[plant][index]

    def node(self, plant: int = 0, index: int = 0) -> GobNode:
        return self.nodes[plant][index]


ZoneSeed = Callable[[GobZone], None]


def _node(stack: Stack, authority: TestAuthority, zone: GobZone, now: dt.datetime) -> GobNode:
    """Nodo ``enrolled`` de la planta de ``zone``, asignado a ella, con su credencial vigente."""
    authz = stack.authz
    node_id = uuid.uuid4()
    code = unique_node_code()
    assigned_at = now - 30 * DAY
    stack.execute(
        "INSERT INTO identity.node_identity (node_id, organization_id, plant_id, code, status,"
        " created_at) VALUES ($1, $2, $3, $4, 'enrolled', $5)",
        node_id,
        zone.organization_id,
        zone.plant_id,
        code,
        now - 31 * DAY,
    )
    stack.execute(
        "INSERT INTO identity.zone_node_assignment (assignment_id, organization_id, plant_id,"
        " zone_id, node_id, assigned_at, assigned_by) VALUES ($1, $2, $3, $4, $5, $6, $7)",
        uuid.uuid4(),
        zone.organization_id,
        zone.plant_id,
        zone.zone_id,
        node_id,
        assigned_at,
        authz.operator_id,
    )
    stack.execute(
        "INSERT INTO fleet.node_fleet_record (node_id, organization_id, plant_id, declared_at,"
        " declared_by, enrolled_at, hardware_fingerprint) VALUES ($1, $2, $3, $4, $5, $6, $7)",
        node_id,
        zone.organization_id,
        zone.plant_id,
        now - 31 * DAY,
        authz.operator_id,
        assigned_at,
        secrets.token_hex(32),
    )
    db_node = DbNode(node_id, zone.organization_id, zone.plant_id, zone.zone_id, authz.operator_id)
    certificate, credential_id = stack.run(
        issue(authz.sessions.admin, authority, db_node, now - DAY)
    )
    return GobNode(
        zone.organization_id,
        zone.plant_id,
        node_id,
        code,
        zone.zone_id,
        assigned_at,
        certificate,
        credential_id,
    )


def build_organization(
    stack: Stack, authority: TestAuthority, now: dt.datetime, seed_zone: ZoneSeed | None = None
) -> GobOrganization:
    """Una organización con dos plantas, dos zonas por planta y un nodo por zona."""
    site = stack.authz.add_site(plants=2, zones_per_plant=2)
    zones: list[tuple[GobZone, GobZone]] = []
    nodes: list[tuple[GobNode, GobNode]] = []
    for plant_id, zone_ids in site.plants.items():
        first, second = (
            GobZone(site.organization_id, plant_id, zone_id, (uuid.uuid4(), uuid.uuid4()))
            for zone_id in zone_ids[:2]
        )
        for zone in (first, second):
            if seed_zone is not None:
                seed_zone(zone)
        zones.append((first, second))
        nodes.append((_node(stack, authority, first, now), _node(stack, authority, second, now)))
    return GobOrganization(site, tuple(zones), tuple(nodes))


def two_organizations(
    stack: Stack, authority: TestAuthority, now: dt.datetime, seed_zone: ZoneSeed | None = None
) -> tuple[GobOrganization, GobOrganization]:
    """Las organizaciones A y B del aislamiento de U-03."""
    return (
        build_organization(stack, authority, now, seed_zone),
        build_organization(stack, authority, now, seed_zone),
    )


def publish_zone(stack: Stack, zone: GobZone, now: dt.datetime) -> None:
    """Catálogo 1 vigente (cámaras con ``code``), compuertas aprobadas y proyección ``productive``.

    Filas sintéticas como superusuario (la publicación real está en
    ``tests/integration/test_catalog_routes.py``): el sobre del catálogo lleva una firma
    sintética que la ruta del nodo sirve sin mirar.
    """
    operator = stack.authz.operator_id
    payload = {
        "zone_id": str(zone.zone_id),
        "catalog_version": 1,
        "cameras": [
            {
                "camera_id": str(camera),
                "code": f"CAM-{index}",
                "role_in_zone": "primary",
                "declared_min_fps": 5.0,
            }
            for index, camera in enumerate(zone.cameras, start=1)
        ],
        "minimum_coverage": {
            "required_count": len(zone.cameras),
            "required_camera_ids": [str(zone.cameras[0])],
        },
    }
    envelope = {
        "payload": payload,
        "payload_canonical_sha256": hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest(),
        "signature": SYNTHETIC_SIGNATURE,
        "key_id": "catalog-sintetica",
        "signed_at": format_timestamp(now),
    }
    stack.execute(
        "INSERT INTO catalog.zone_catalog_version (organization_id, plant_id, zone_id,"
        " catalog_version, issued_at, issued_by, role_in_use, reason_es, changed_fields,"
        " payload, envelope, single_occupancy, ledger_record_id)"
        " VALUES ($1, $2, $3, 1, $4, $5, 'administrator', 'Catálogo sintético del aislamiento',"
        " ARRAY['cameras'], $6::jsonb, $7::jsonb, false, $8)",
        zone.organization_id,
        zone.plant_id,
        zone.zone_id,
        now - 30 * DAY,
        operator,
        json.dumps(payload),
        json.dumps(envelope),
        uuid.uuid4(),
    )
    for gate in ("mounting", "usage"):
        stack.execute(
            "INSERT INTO catalog.gate_state_history (organization_id, plant_id, zone_id, gate,"
            " status, effective_from, decided_by, ledger_record_id, record_id)"
            " VALUES ($1, $2, $3, $4, 'approved', $5, $6, $7, $8)",
            zone.organization_id,
            zone.plant_id,
            zone.zone_id,
            gate,
            now - 30 * DAY,
            operator,
            uuid.uuid4(),
            uuid.uuid4(),
        )
    decided = {"status": "approved", "decided_at": (now - 30 * DAY).isoformat()}
    stack.execute(
        "INSERT INTO catalog.zone_gate_state (zone_id, organization_id, plant_id, mounting,"
        " usage, resulting_mode, issued_at, envelope, valid_until)"
        " VALUES ($1, $2, $3, $4, $5, 'productive', $6, '{}', $6::timestamptz + interval '7 days')",
        zone.zone_id,
        zone.organization_id,
        zone.plant_id,
        json.dumps(decided),
        json.dumps(decided),
        now,
    )
