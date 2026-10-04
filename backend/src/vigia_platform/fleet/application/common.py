"""Piezas comunes de los servicios de identidad del nodo de ``fleet`` (TASK-218, LC-GOB-10).

- ``FleetDependencies``: lo que reciben los servicios (base, escritor del expediente, auditoría,
  autorización, política de texto libre, reloj, ``IdentityCommandPort`` de U-02 y los almacenes).
- ``FleetRejected``: rechazo de negocio con su ``detail_code`` de la lista cerrada ``fleet_``; el
  mensaje nunca repite el valor recibido.
- ``authorized_node``: el nodo de la organización del contexto y el contexto autorizado sobre
  **su planta** (la comprobación de alcance de las rutas ``/nodes/{node_id}/…``): inexistente, de
  otra organización, fuera del alcance o sin la clave responde igual, ``ResourceNotFound``
  (BR-NUC-09, BR-GOB-88).
- ``checked_reason``: ``reason_es`` con la política de texto libre de U-02 más los validadores
  registrados (el mínimo de U-03, A-45), límites 10 a 500. Nunca llega a un registro
  estructurado, una métrica, una traza ni un evento (NFR-GOB-25): solo al expediente.
- ``write``: el registro de U-03 en la transacción de la operación.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from vigia_platform.fleet.adapters.postgres.enrollment_store import PostgresEnrollmentStore
from vigia_platform.fleet.adapters.postgres.node_fleet_store import PostgresNodeFleetStore
from vigia_platform.fleet.adapters.postgres.revocation_mark_store import (
    PostgresRevocationMarkStore,
)
from vigia_platform.fleet.detail_codes import FLEET_API_ERROR_CODES, FleetDetailCode
from vigia_platform.fleet.domain.node_fleet_record import (
    REASON_MAX_CHARS,
    REASON_MIN_CHARS,
    FleetNode,
)
from vigia_platform.identity.application.common import IdentityRejected, IdentityRejection
from vigia_platform.identity.application.hierarchy import IdentityCommandPort
from vigia_platform.identity.authz.authorize import Authorizer, Resource, ResourceNotFound
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import AuditWriter
from vigia_platform.ledger.application.writer import (
    EscritorExpediente,
    LedgerDatabase,
    LedgerRejection,
    LedgerRejectionCode,
    Receipt,
    RecordScope,
)
from vigia_platform.ledger.free_text import FreeTextField, FreeTextPolicyRegistry, FreeTextRejected
from vigia_platform.shared.api.errors import ApiErrorCode
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics
from vigia_platform.shared.outbox.publish import NewEvent

__all__ = [
    "FleetDependencies",
    "FleetRejected",
    "FleetWriteFailed",
    "authorized_node",
    "checked_reason",
    "from_identity",
    "write",
]


class FleetRejected(Exception):
    """Rechazo de negocio con su ``detail_code`` (``fleet_*``) y el ``code`` de U-02."""

    def __init__(self, detail_code: FleetDetailCode) -> None:
        super().__init__(f"rechazo de la flota: {detail_code.value}")
        self.detail_code = FleetDetailCode(detail_code)

    @property
    def api_code(self) -> ApiErrorCode:
        return FLEET_API_ERROR_CODES[self.detail_code]


class FleetWriteFailed(Exception):
    """El expediente rechazó un registro por una causa que no es del cuerpo: la operación se
    revierte entera."""

    def __init__(self, rejection: LedgerRejection) -> None:
        super().__init__(f"registro rechazado: {rejection.code.value}")
        self.rejection = rejection


@dataclass(frozen=True, slots=True, kw_only=True)
class FleetDependencies:
    """Lo que reciben los servicios de identidad del nodo."""

    database: LedgerDatabase
    writer: EscritorExpediente
    audit: AuditWriter
    authorizer: Authorizer
    free_text: FreeTextPolicyRegistry
    clock: Clock
    identity: IdentityCommandPort
    nodes: PostgresNodeFleetStore
    enrollment: PostgresEnrollmentStore = field(default_factory=PostgresEnrollmentStore)
    marks: PostgresRevocationMarkStore = field(default_factory=PostgresRevocationMarkStore)
    metrics: PlatformMetrics | None = None
    random_bytes: Callable[[int], bytes] = os.urandom

    def platform_metrics(self) -> PlatformMetrics:
        return self.metrics if self.metrics is not None else get_metrics()


_IDENTITY_DETAIL: dict[IdentityRejection, FleetDetailCode] = {
    IdentityRejection.ZONE_HAS_NODE: FleetDetailCode.ZONE_ALREADY_SERVED,
    IdentityRejection.NODE_PLANT_MISMATCH: FleetDetailCode.ZONE_IN_OTHER_PLANT,
    IdentityRejection.CODE_TAKEN: FleetDetailCode.CODE_IN_USE,
    IdentityRejection.FREE_TEXT_REJECTED: FleetDetailCode.FREE_TEXT_REJECTED,
}


def from_identity(error: IdentityRejected) -> Exception:
    """El rechazo de U-02 con nombre de la flota (BR-GOB-57), o el mismo si no tiene."""
    detail = _IDENTITY_DETAIL.get(error.code)
    return error if detail is None else FleetRejected(detail)


def checked_reason(free_text: FreeTextPolicyRegistry, value: object, record_type: str) -> str:
    """``reason_es`` en NFC si pasa la política (10 a 500); si no, ``fleet_free_text_rejected``."""
    if not isinstance(value, str):
        raise FleetRejected(FleetDetailCode.FREE_TEXT_REJECTED)
    field_ = FreeTextField(record_type, "/reason_es", REASON_MIN_CHARS, REASON_MAX_CHARS)
    try:
        return free_text.apply(value, field_)
    except FreeTextRejected:
        raise FleetRejected(FleetDetailCode.FREE_TEXT_REJECTED) from None


async def authorized_node(
    deps: FleetDependencies, context: ScopeContext, node_id: uuid.UUID, key: PermissionKey
) -> tuple[ScopeContext, FleetNode]:
    """El contexto autorizado con ``key`` sobre la planta del nodo, y el nodo.

    El nodo se busca en la organización del contexto (y bajo concesión, en su alcance, por la
    RLS); la autorización es sobre **su planta**: otra organización, otra planta o sin la clave
    responden igual que un nodo inexistente.
    """
    if type(node_id) is not uuid.UUID:
        raise ResourceNotFound()
    node = await deps.nodes.node(context, node_id)
    if node is None:
        raise ResourceNotFound()
    authorized = await deps.authorizer.authorize(
        context, key, Resource.plant(context.organization_id, node.plant_id)
    )
    return authorized, node


async def write(
    deps: FleetDependencies,
    context: ScopeContext,
    transaction: Transaction,
    record_type: str,
    content: Mapping[str, Any],
    *,
    plant_id: uuid.UUID,
    occurred_at: datetime,
    events: Sequence[NewEvent] = (),
) -> Receipt:
    """Escribe ``record_type`` en la cadena de ``plant_id``, en la transacción de la operación;
    un rechazo la revierte."""
    written = await deps.writer.write(
        context,
        record_type,
        dict(content),
        scope=RecordScope(plant_id=plant_id),
        events=events,
        occurred_at=occurred_at,
        transaction=transaction,
    )
    if isinstance(written, LedgerRejection):
        if written.code is LedgerRejectionCode.FREE_TEXT_REJECTED:
            raise FleetRejected(FleetDetailCode.FREE_TEXT_REJECTED)
        raise FleetWriteFailed(written)
    return written
