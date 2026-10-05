"""Piezas de las pruebas de las versiones de flota (TASK-226) sobre PostgreSQL 16 real.

- ``node_app``: una ``NodeApiGate`` real (identidad del nodo por certificado contra la base, sin
  límite de tasa) con las operaciones que pide la prueba y la aplicación con la cadena fija y solo
  esas rutas. Lo usan el latido (``heartbeat_support``) y la ingesta (``fleet_ingest_support``) para
  montar a su lado ``POST update-results``.
- ``update_result``: un ``UpdateResult`` válido del contrato (lector estricto de U-01) y
  ``send_update``: su presentación con el certificado del nodo, la versión del contrato y la
  ``Idempotency-Key`` igual a ``update_result_id``.
- ``publisher`` e ``installer_context``: el ``TargetVersionService`` real y el contexto de un
  instalador del proveedor con una concesión vigente sobre la organización (``fleet.manage`` solo
  lo tiene ``provider_installer``).
- ``inventory_row``: la fila mínima de ``fleet.node_inventory`` de un nodo, como la deja su primer
  latido aceptado.

Solo datos generados (NFR-CTR-43). Marcas del reloj simulado; topes de la base de 60 s (retro 15).
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import httpx
from cryptography import x509
from vigia_contracts.models import api

from tests.api_support import World
from tests.authz_support import AuthzEnvironment
from tests.heartbeat_support import UnlimitedLimiter
from tests.node_api_support import VERSION, alb_headers, node_unit
from vigia_platform.fleet.application.target_versions import TargetVersionService
from vigia_platform.fleet.application.update_results import UpdateResultService
from vigia_platform.ledger.application.writer import EscritorExpediente, LedgerDatabase
from vigia_platform.node_api.identity import NodeIdentity, PostgresNodeContextStore
from vigia_platform.node_api.limits import NodeRateLimits
from vigia_platform.node_api.observability import NodeResponses
from vigia_platform.node_api.router import NodeApiGate, NodeOperation
from vigia_platform.node_api.routes.update_results import update_result_operation
from vigia_platform.node_api.versioning import VersionPolicy
from vigia_platform.shared.api.declarations import NODE_GATE_STATE_KEY, NodeRoute
from vigia_platform.shared.context import ScopeContext, ScopeLevel
from vigia_platform.shared.db import Database
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "HOUR",
    "NodeApp",
    "installer_context",
    "inventory_row",
    "node_app",
    "publisher",
    "send_update",
    "update_result",
    "window",
]

HOUR: Final = dt.timedelta(hours=1)
DETAIL: Final = {
    "applied": "health_check_passed",
    "reverted": "health_check_failed",
    "failed": "download_hash_mismatch",
}


@dataclass
class NodeApp:
    gate: NodeApiGate
    app: Any
    client: httpx.AsyncClient
    updates: UpdateResultService


def node_app(
    authz: AuthzEnvironment,
    database: Database,
    writer: EscritorExpediente,
    operations: Mapping[NodeRoute, NodeOperation],
) -> NodeApp:
    """La aplicación con las ``operations`` dadas más ``POST update-results`` sobre ``database``."""
    clock = authz.sessions.clock
    updates = UpdateResultService(database=database, writer=writer, clock=clock)
    routes = {**operations, NodeRoute.UPDATE_RESULT: update_result_operation(updates)}
    gate = NodeApiGate(
        identity=NodeIdentity(contexts=authz.contexts, store=PostgresNodeContextStore(database)),
        limits=NodeRateLimits(UnlimitedLimiter(clock)),
        clock=clock,
        responses=NodeResponses(clock),
        operations=routes,
    )
    app = World(clock=clock).app(
        units=(node_unit(tuple(routes)),), runtime={"state": {NODE_GATE_STATE_KEY: gate}}
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=120.0
    )
    return NodeApp(gate, app, client, updates)


def update_result(
    *,
    organization_id: uuid.UUID,
    plant_id: uuid.UUID,
    node_id: uuid.UUID,
    update_result_id: uuid.UUID,
    verified_at: dt.datetime,
    target_version: str = "1.0.3",
    outcome: str = "applied",
    previous_version: str = "1.0.2",
) -> dict[str, Any]:
    """Un ``UpdateResult`` válido del contrato (``queue_preserved`` siempre ``true``, H-48)."""
    document = {
        "update_result_id": str(update_result_id),
        "contract_version": VERSION,
        "organization_id": str(organization_id),
        "plant_id": str(plant_id),
        "node_id": str(node_id),
        "target_version": target_version,
        "previous_version": previous_version,
        "outcome": outcome,
        "verified_at": format_timestamp(verified_at),
        "detail_code": DETAIL[outcome],
        "queue_preserved": True,
    }
    api.parse_update_result(json.dumps(document).encode())
    return document


async def send_update(
    client: httpx.AsyncClient,
    certificate: x509.Certificate,
    document: Mapping[str, Any],
    *,
    key: str | None = None,
    version: str = VERSION,
) -> httpx.Response:
    """``POST update-results`` con el certificado del nodo (``key``: otra ``Idempotency-Key``)."""
    headers = {
        **alb_headers(certificate),
        "X-Vigia-Contract-Version": version,
        "Idempotency-Key": key if key is not None else str(document.get("update_result_id")),
        "Content-Type": "application/json",
    }
    response: httpx.Response = await client.post(
        NodeRoute.UPDATE_RESULT.path, content=json.dumps(document).encode(), headers=headers
    )
    return response


def publisher(
    authz: AuthzEnvironment,
    database: LedgerDatabase,
    writer: EscritorExpediente,
    *,
    policy: VersionPolicy | None = None,
) -> TargetVersionService:
    """El ``TargetVersionService`` real (por omisión, la política de versiones de producción)."""
    return TargetVersionService(
        database=database,
        writer=writer,
        authorizer=authz.authorizer,
        audit=authz.sessions.audit,
        clock=authz.sessions.clock,
        policy=policy if policy is not None else VersionPolicy(),
    )


def installer_context(
    authz: AuthzEnvironment,
    organization_id: uuid.UUID,
    *,
    level: ScopeLevel = ScopeLevel.ORGANIZATION,
    scope: uuid.UUID | None = None,
) -> ScopeContext:
    """El contexto de un instalador del proveedor con concesión vigente sobre la organización."""
    installer = authz.add_provider_user()
    concession = authz.add_concession(
        organization_id, installer, level=level, scope_id=scope, granted_at=authz.now() - HOUR
    )
    cookie = authz.open_session(authz.provider_organization_id, installer)
    scope_context = authz.run(authz.contexts.context_from_session(cookie, concession_id=concession))
    context: ScopeContext = scope_context.context
    return context


def window(start: dt.datetime, hours: int = 2) -> tuple[dt.datetime, dt.datetime]:
    """Una ventana de mantenimiento de ``hours`` horas desde ``start``."""
    return start, start + dt.timedelta(hours=hours)


def inventory_row(
    authz: AuthzEnvironment,
    *,
    organization_id: uuid.UUID,
    plant_id: uuid.UUID,
    node_id: uuid.UUID,
    at: dt.datetime,
) -> None:
    """La fila de ``fleet.node_inventory`` del nodo, como tras su primer latido."""
    authz.execute(
        "INSERT INTO fleet.node_inventory (node_id, organization_id, plant_id, software_version,"
        " contract_version, model_version, contract_notice, last_heartbeat_at,"
        " communication_state, local_queue, clock, signal_reader, uptime_seconds, updated_at)"
        " VALUES ($1, $2, $3, '1.0.2', $4, 'yolov8n-2026.09', '{\"result\": \"accepted\"}',"
        ' $5, \'reachable\', \'{"pending": 0, "dead_letter": [], "retained_sent": 0}\','
        ' \'{"synchronized": true, "offset_ms": 3}\','
        ' \'{"available": true, "adapter": "modbus_rtu"}\', 60, $5)',
        node_id,
        organization_id,
        plant_id,
        VERSION,
        at,
    )
