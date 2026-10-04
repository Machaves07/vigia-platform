"""Rutas de identidad del nodo de SCR-07 (TASK-218; interfaces §3.4, §4 y «Versión 1.5»).

- ``POST /plants/{plant_id}/nodes`` (``commissioning.run``, planta) ``{code, zone_ids[],
  replaces_node_id?}`` → 201 nodo ``declared`` con sus asignaciones; errores
  ``fleet_zone_already_served``, ``fleet_zone_in_other_plant``, ``fleet_code_in_use`` y
  ``fleet_replaced_node_not_found``.
- ``POST /nodes/{node_id}/zones`` (``commissioning.run``) ``{zone_id}`` → 201 asignación.
- ``POST /nodes/{node_id}/zones/{zone_id}/unassignment`` (``commissioning.run``) ``{reason_es}`` →
  200 asignación retirada con fecha.
- ``POST /nodes/{node_id}/enrollment-codes`` (``commissioning.run``) → 201 ``{code, code_id,
  expires_at, disclosed_at, node_ca_root_sha256[]}``; el código se muestra **una sola vez**: no
  existe ruta que lo vuelva a mostrar. ``fleet_node_not_declared`` si el nodo no admite código.
- ``GET /nodes/{node_id}/enrollment-attempts`` (``fleet.read``, cursor) → intentos con resultado,
  huella de hardware y momento.
- ``POST /nodes/{node_id}/revocation`` (``fleet.manage``) ``{reason_es}`` → 200.
- ``POST /nodes/{node_id}/decommission`` (``fleet.manage``) ``{reason_es}`` → 200; exige el nodo
  revocado (``fleet_node_not_revoked``).

Un recurso de la ruta inexistente, de otra organización o fuera del alcance responde
``not_found``, nunca ``forbidden``. Toda respuesta lleva ``Cache-Control: no-store``. Cada ruta
declara su clave (``requires``) y un límite de cuerpo propio (``body_limit``).
"""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from vigia_platform.fleet.adapters.http.services import FleetHttp, fleet_http, installed
from vigia_platform.fleet.adapters.postgres.enrollment_store import AttemptCursor
from vigia_platform.fleet.application.common import FleetRejected, FleetWriteFailed
from vigia_platform.fleet.application.enrollment_codes import (
    MAX_ATTEMPTS_PAGE,
    ConcurrentIssue,
    RootsUnavailable,
)
from vigia_platform.fleet.application.node_declaration import DeclaredNode, ZoneAssignment
from vigia_platform.fleet.detail_codes import FleetDetailCode
from vigia_platform.fleet.domain.enums import EnrollmentAttemptResult
from vigia_platform.fleet.record_types import MAX_NODE_ZONES
from vigia_platform.identity.adapters.http.services import identity_error
from vigia_platform.identity.application.common import IdentityRejected
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.shared.api.declarations import body_limit, requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode, from_ledger_rejection
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = ["BODY_LIMIT_BYTES", "decode_cursor", "encode_cursor", "nodes_router"]

BODY_LIMIT_BYTES: Final = 16_384
"""Cuerpo máximo de las rutas de esta tarea `[objetivo propio]`: el mayor, la declaración con 16
zonas, ocupa menos de 1 KB."""
MAX_CURSOR_CHARS: Final = 128
_CURSOR: Final = re.compile(r"[A-Za-z0-9_-]{1,128}")
_RUN: Final = PermissionKey.COMMISSIONING_RUN.value
_READ: Final = PermissionKey.FLEET_READ.value
_MANAGE: Final = PermissionKey.FLEET_MANAGE.value
_DECLARE_CODES: Final = tuple(
    code.value
    for code in (
        FleetDetailCode.ZONE_ALREADY_SERVED,
        FleetDetailCode.ZONE_IN_OTHER_PLANT,
        FleetDetailCode.CODE_IN_USE,
        FleetDetailCode.REPLACED_NODE_NOT_FOUND,
    )
)
_ZONE_CODES: Final = (
    FleetDetailCode.ZONE_ALREADY_SERVED.value,
    FleetDetailCode.ZONE_IN_OTHER_PLANT.value,
    FleetDetailCode.NODE_NOT_DECLARED.value,
)
_REASON_CODES: Final = (FleetDetailCode.FREE_TEXT_REJECTED.value,)
_CODE_CODES: Final = (FleetDetailCode.NODE_NOT_DECLARED.value,)
_DECOMMISSION_CODES: Final = (
    FleetDetailCode.NODE_NOT_REVOKED.value,
    FleetDetailCode.FREE_TEXT_REJECTED.value,
)

Services = Annotated[FleetHttp, Depends(fleet_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NodeDeclarationBody(_Strict):
    code: StrictStr
    zone_ids: tuple[uuid.UUID, ...] = Field(max_length=MAX_NODE_ZONES)
    replaces_node_id: uuid.UUID | None = None


class NodeZoneBody(_Strict):
    zone_id: uuid.UUID


class NodeReasonBody(_Strict):
    reason_es: StrictStr


class NodeAssignmentView(_Strict):
    assignment_id: uuid.UUID
    node_id: uuid.UUID
    zone_id: uuid.UUID
    assigned_at: str
    unassigned_at: str | None


class DeclaredNodeView(_Strict):
    node_id: uuid.UUID
    plant_id: uuid.UUID
    code: str
    status: str
    declared_at: str
    replaces_node_id: uuid.UUID | None
    assignments: tuple[NodeAssignmentView, ...]


class EnrollmentCodeView(_Strict):
    """El código de alta, mostrado **una sola vez**."""

    code: str
    code_id: uuid.UUID
    expires_at: str
    disclosed_at: str
    node_ca_root_sha256: tuple[str, ...]


class EnrollmentAttemptView(_Strict):
    attempt_id: uuid.UUID
    node_id: uuid.UUID | None
    result: EnrollmentAttemptResult
    hardware_fingerprint: str
    software_version: str
    contract_version: str
    attempted_at: str


class EnrollmentAttemptsPage(_Strict):
    attempts: tuple[EnrollmentAttemptView, ...]
    next_after: str | None
    """Pásalo como ``after`` para la página siguiente; ``null`` si no hay más."""


class NodeRevocationView(_Strict):
    node_id: uuid.UUID
    status: str
    revoked_at: str


class NodeDecommissionView(_Strict):
    node_id: uuid.UUID
    revoked_at: str
    decommissioned_at: str


def encode_cursor(cursor: AttemptCursor) -> str:
    """Cursor opaco: la marca del intento con microsegundos y su identificador."""
    raw = f"{cursor.attempted_at.astimezone(UTC).isoformat()}|{cursor.attempt_id}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(value: str) -> AttemptCursor:
    """El cursor de ``encode_cursor``; cualquier otra cosa, ``invalid_request``."""
    if _CURSOR.fullmatch(value) is None:
        raise ApiError(ApiErrorCode.INVALID_REQUEST)
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("ascii")
        moment, _, identifier = raw.partition("|")
        return AttemptCursor(datetime.fromisoformat(moment), uuid.UUID(identifier))
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        raise ApiError(ApiErrorCode.INVALID_REQUEST) from None


def _assignment(item: ZoneAssignment) -> NodeAssignmentView:
    return NodeAssignmentView(
        assignment_id=item.assignment_id,
        node_id=item.node_id,
        zone_id=item.zone_id,
        assigned_at=format_timestamp(item.assigned_at),
        unassigned_at=None if item.unassigned_at is None else format_timestamp(item.unassigned_at),
    )


def _declared(node: DeclaredNode) -> DeclaredNodeView:
    return DeclaredNodeView(
        node_id=node.node_id,
        plant_id=node.plant_id,
        code=node.code,
        status=node.status,
        declared_at=format_timestamp(node.declared_at),
        replaces_node_id=node.replaces_node_id,
        assignments=tuple(_assignment(item) for item in node.assignments),
    )


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _api_error(error: Exception) -> ApiError:
    """El ``ApiError`` de un rechazo de los servicios de la flota (código cerrado)."""
    if isinstance(error, FleetRejected):
        return ApiError(error.api_code, detail_code=error.detail_code.value)
    if isinstance(error, FleetWriteFailed):
        return from_ledger_rejection(error.rejection)
    if isinstance(error, IdentityRejected):
        return identity_error(error)
    if isinstance(error, RootsUnavailable):
        return ApiError(ApiErrorCode.STORAGE_UNAVAILABLE)
    if isinstance(error, ConcurrentIssue):
        return ApiError(ApiErrorCode.CONFLICT)
    raise error  # pragma: no cover - ``_HANDLED`` solo lleva las cuatro de arriba


_HANDLED: Final = (
    FleetRejected,
    FleetWriteFailed,
    IdentityRejected,
    RootsUnavailable,
    ConcurrentIssue,
)


def nodes_router() -> APIRouter:
    router = APIRouter(tags=["flota"])
    limit = body_limit(BODY_LIMIT_BYTES)

    @router.post(
        "/plants/{plant_id}/nodes",
        status_code=201,
        dependencies=[requires(_RUN, detail_codes=_DECLARE_CODES), limit],
        summary="Declara un nodo de la planta con sus zonas (y, si procede, reemplaza a otro)",
    )
    async def declare_node(
        plant_id: uuid.UUID,
        body: NodeDeclarationBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> DeclaredNodeView:
        _no_store(response)
        service = installed(services.declarations)
        if len(set(body.zone_ids)) != len(body.zone_ids):
            raise ApiError(ApiErrorCode.INVALID_REQUEST)
        try:
            node = await service.declare(
                request_context(request),
                plant_id,
                code=body.code,
                zone_ids=body.zone_ids,
                replaces_node_id=body.replaces_node_id,
            )
        except _HANDLED as error:
            raise _api_error(error) from None
        return _declared(node)

    @router.post(
        "/nodes/{node_id}/zones",
        status_code=201,
        dependencies=[requires(_RUN, detail_codes=_ZONE_CODES), limit],
        summary="Asigna una zona de su planta a un nodo en operación",
    )
    async def assign_zone(
        node_id: uuid.UUID,
        body: NodeZoneBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> NodeAssignmentView:
        _no_store(response)
        service = installed(services.declarations)
        try:
            item = await service.assign_zone(request_context(request), node_id, body.zone_id)
        except _HANDLED as error:
            raise _api_error(error) from None
        return _assignment(item)

    @router.post(
        "/nodes/{node_id}/zones/{zone_id}/unassignment",
        dependencies=[requires(_RUN, detail_codes=_REASON_CODES), limit],
        summary="Retira con fecha y motivo la asignación de una zona al nodo",
    )
    async def unassign_zone(
        node_id: uuid.UUID,
        zone_id: uuid.UUID,
        body: NodeReasonBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> NodeAssignmentView:
        _no_store(response)
        service = installed(services.declarations)
        try:
            item = await service.unassign_zone(
                request_context(request), node_id, zone_id, body.reason_es
            )
        except _HANDLED as error:
            raise _api_error(error) from None
        return _assignment(item)

    @router.post(
        "/nodes/{node_id}/enrollment-codes",
        status_code=201,
        dependencies=[requires(_RUN, detail_codes=_CODE_CODES), limit],
        summary="Emite el código de alta del nodo (se muestra una sola vez)",
    )
    async def issue_enrollment_code(
        node_id: uuid.UUID,
        request: Request,
        response: Response,
        services: Services,
    ) -> EnrollmentCodeView:
        _no_store(response)
        service = installed(services.enrollment_codes)
        try:
            issued = await service.issue(request_context(request), node_id)
        except _HANDLED as error:
            raise _api_error(error) from None
        return EnrollmentCodeView(
            code=issued.code,
            code_id=issued.code_id,
            expires_at=format_timestamp(issued.expires_at),
            disclosed_at=format_timestamp(issued.disclosed_at),
            node_ca_root_sha256=issued.node_ca_root_sha256,
        )

    @router.get(
        "/nodes/{node_id}/enrollment-attempts",
        dependencies=[requires(_READ)],
        summary="Intentos de alta del nodo, el más reciente primero",
    )
    async def list_enrollment_attempts(
        node_id: uuid.UUID,
        request: Request,
        response: Response,
        services: Services,
        after: Annotated[str | None, Query(max_length=MAX_CURSOR_CHARS)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_ATTEMPTS_PAGE)] = MAX_ATTEMPTS_PAGE,
    ) -> EnrollmentAttemptsPage:
        _no_store(response)
        cursor = None if after is None else decode_cursor(after)
        page = await installed(services.enrollment_codes).attempts(
            request_context(request), node_id, after=cursor, limit=limit
        )
        return EnrollmentAttemptsPage(
            attempts=tuple(
                EnrollmentAttemptView(
                    attempt_id=item.attempt_id,
                    node_id=item.node_id,
                    result=item.result,
                    hardware_fingerprint=item.hardware_fingerprint,
                    software_version=item.software_version,
                    contract_version=item.contract_version,
                    attempted_at=format_timestamp(item.attempted_at),
                )
                for item in page.items
            ),
            next_after=None if page.next_cursor is None else encode_cursor(page.next_cursor),
        )

    @router.post(
        "/nodes/{node_id}/revocation",
        dependencies=[requires(_MANAGE, detail_codes=_REASON_CODES), limit],
        summary="Revoca el nodo: efecto inmediato en las rutas del contrato",
    )
    async def revoke_node(
        node_id: uuid.UUID,
        body: NodeReasonBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> NodeRevocationView:
        _no_store(response)
        service = installed(services.revocations)
        try:
            outcome = await service.revoke(request_context(request), node_id, body.reason_es)
        except _HANDLED as error:
            raise _api_error(error) from None
        return NodeRevocationView(
            node_id=outcome.node_id,
            status="revoked",
            revoked_at=format_timestamp(outcome.revoked_at),
        )

    @router.post(
        "/nodes/{node_id}/decommission",
        dependencies=[requires(_MANAGE, detail_codes=_DECOMMISSION_CODES), limit],
        summary="Da de baja un nodo revocado; conserva todo lo que envió",
    )
    async def decommission_node(
        node_id: uuid.UUID,
        body: NodeReasonBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> NodeDecommissionView:
        _no_store(response)
        service = installed(services.revocations)
        try:
            outcome = await service.decommission(request_context(request), node_id, body.reason_es)
        except _HANDLED as error:
            raise _api_error(error) from None
        return NodeDecommissionView(
            node_id=outcome.node_id,
            revoked_at=format_timestamp(outcome.revoked_at),
            decommissioned_at=format_timestamp(outcome.decommissioned_at),
        )

    return router
