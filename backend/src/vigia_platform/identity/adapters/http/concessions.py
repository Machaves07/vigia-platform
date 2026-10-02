"""Rutas de concesiones del lado cliente y del lado proveedor (§10.2; BR-NUC-35 a 42; H-56).

Lado cliente (``administrator`` y ``plant_manager``):

- ``GET /concessions`` (``concessions.read``): todas las concesiones de la organización
  (vigentes, vencidas y revocadas) con su motivo; ``plant_id`` acota a las que alcanzan esa
  planta. El estado es el **efectivo**: una concesión cuyo ``expires_at`` ya pasó se muestra
  ``expired`` aunque la tarea ``expire_concessions`` aún no haya corrido (P5). La lectura queda
  auditada (BR-NUC-41).
- ``GET /concessions/{concession_id}/queries`` (``concessions.read``): cada ``provider_query`` de
  la concesión con momento, operación y el motivo, en orden de llegada y en páginas
  (``after`` opaco, ``limit`` ≤ 200). Ningún acceso del proveedor es invisible para el cliente.
- ``POST /concessions/{concession_id}/revoke`` (``concessions.revoke``): revocación inmediata
  (BR-NUC-39). Desde la petición siguiente el proveedor ya no obtiene contexto sobre el cliente:
  con ``X-Vigia-Concession`` de esa concesión, toda ruta responde ``not_found``. Un operador de la
  proveedora también revoca por esta ruta desde su organización. Revocar una concesión ya cerrada
  responde ``conflict``.

Lado proveedor (``provider_installer`` y ``platform_operator`` en la proveedora, sin concesión):

- ``POST /provider/concessions`` (``concessions.grant``): autoservicio sobre una organización
  **cliente** activa con alcance de organización o de planta, motivo de 10 a 500 caracteres y
  duración en horas entre 1 y ``concession_max_days`` del cliente (sin duración, la de omisión
  del cliente). Entra en vigor de inmediato (``201``). Un cliente inexistente, suspendido o la
  propia proveedora responden ``not_found``.
- ``GET /provider/concessions`` (``concessions.grant``): las concesiones que la persona se
  concedió, la más reciente primero, **sin el motivo** (vive en el expediente del cliente).

Bajo concesión (``X-Vigia-Concession``) ninguna de estas rutas responde nada útil: el proveedor
no ve ni revoca concesiones desde el contexto del cliente (``not_found``).
"""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from vigia_platform.identity.adapters.http.services import (
    IdentityHttp,
    identity_http,
    installed,
    no_store,
)
from vigia_platform.identity.application.concessions import (
    MAX_PAGE_SIZE,
    MAX_REASON_CHARS,
    MIN_REASON_CHARS,
    Concession,
    ConcessionRejected,
    ConcessionRejectionCode,
    ConcessionStatus,
    ProviderQueryCursor,
    ProviderQueryView,
    RevokedBySide,
)
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode, from_ledger_rejection
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.context import ScopeLevel
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = ["MAX_DURATION_HOURS", "concessions_router", "decode_cursor", "encode_cursor"]

MAX_DURATION_HOURS: Final = 90 * 24
"""Tope del esquema; el del cliente (``concession_max_days``) lo aplica el servicio."""
MAX_CURSOR_CHARS: Final = 128
_CURSOR: Final = re.compile(r"[A-Za-z0-9_-]{1,128}")

Services = Annotated[IdentityHttp, Depends(identity_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GrantBody(_Strict):
    client_organization_id: uuid.UUID
    scope_level: Literal["organization", "plant"]
    scope_id: uuid.UUID
    reason: StrictStr = Field(min_length=MIN_REASON_CHARS, max_length=MAX_REASON_CHARS)
    duration_hours: StrictInt | None = Field(default=None, ge=1, le=MAX_DURATION_HOURS)


class ConcessionView(_Strict):
    """Una concesión vista por el cliente (con su motivo)."""

    concession_id: uuid.UUID
    provider_organization_id: uuid.UUID
    provider_user_id: uuid.UUID
    scope_level: ScopeLevel
    scope_id: uuid.UUID
    reason: str | None
    granted_at: str
    expires_at: str
    status: ConcessionStatus
    revoked_at: str | None
    revoked_by_side: RevokedBySide | None


class ConcessionsView(_Strict):
    concessions: tuple[ConcessionView, ...]


class ProviderConcessionView(_Strict):
    """Una concesión vista por quien se la concedió (sin el motivo)."""

    concession_id: uuid.UUID
    client_organization_id: uuid.UUID
    scope_level: ScopeLevel
    scope_id: uuid.UUID
    granted_at: str
    expires_at: str
    status: ConcessionStatus
    revoked_at: str | None
    revoked_by_side: RevokedBySide | None


class ProviderConcessionsView(_Strict):
    concessions: tuple[ProviderConcessionView, ...]


class ProviderQueryItem(_Strict):
    record_id: uuid.UUID
    received_at: str
    plant_id: uuid.UUID | None
    provider_user_id: uuid.UUID
    operation: str
    method: str
    resource: str
    occurred_at: str
    reason: str | None


class ProviderQueriesPage(_Strict):
    queries: tuple[ProviderQueryItem, ...]
    next_after: str | None
    """Pásalo como ``after`` para la página siguiente; ``null`` si no hay más."""


def encode_cursor(cursor: ProviderQueryCursor) -> str:
    """Cursor opaco: la marca de llegada con microsegundos y el identificador del registro."""
    raw = f"{cursor.received_at.astimezone(UTC).isoformat()}|{cursor.record_id}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(value: str) -> ProviderQueryCursor:
    """El cursor de ``encode_cursor``; cualquier otra cosa, ``invalid_request``."""
    if _CURSOR.fullmatch(value) is None:
        raise ApiError(ApiErrorCode.INVALID_REQUEST)
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("ascii")
        moment, _, record = raw.partition("|")
        received_at = datetime.fromisoformat(moment)
        record_id = uuid.UUID(record)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
    if received_at.tzinfo is None:
        raise ApiError(ApiErrorCode.INVALID_REQUEST)
    return ProviderQueryCursor(received_at, record_id)


def _rejected(error: ConcessionRejected) -> ApiError:
    if error.code is ConcessionRejectionCode.CONCESSION_CLOSED:
        return ApiError(ApiErrorCode.CONFLICT)
    if error.code is ConcessionRejectionCode.LEDGER_REJECTED:
        if error.rejection is None:
            return ApiError(ApiErrorCode.INTERNAL_ERROR)
        return from_ledger_rejection(error.rejection)
    return ApiError(ApiErrorCode.INVALID_REQUEST)


def _timestamp(moment: datetime | None) -> str | None:
    return None if moment is None else format_timestamp(moment)


def _client_view(concession: Concession, now: datetime) -> ConcessionView:
    return ConcessionView(
        concession_id=concession.concession_id,
        provider_organization_id=concession.provider_organization_id,
        provider_user_id=concession.provider_user_id,
        scope_level=concession.scope_level,
        scope_id=concession.scope_id,
        reason=concession.reason,
        granted_at=format_timestamp(concession.granted_at),
        expires_at=format_timestamp(concession.expires_at),
        status=concession.effective_status(now),
        revoked_at=_timestamp(concession.revoked_at),
        revoked_by_side=concession.revoked_by_side,
    )


def _provider_view(concession: Concession, now: datetime) -> ProviderConcessionView:
    return ProviderConcessionView(
        concession_id=concession.concession_id,
        client_organization_id=concession.organization_id,
        scope_level=concession.scope_level,
        scope_id=concession.scope_id,
        granted_at=format_timestamp(concession.granted_at),
        expires_at=format_timestamp(concession.expires_at),
        status=concession.effective_status(now),
        revoked_at=_timestamp(concession.revoked_at),
        revoked_by_side=concession.revoked_by_side,
    )


def _query(item: ProviderQueryView) -> ProviderQueryItem:
    return ProviderQueryItem(
        record_id=item.record_id,
        received_at=format_timestamp(item.received_at),
        plant_id=item.plant_id,
        provider_user_id=item.provider_user_id,
        operation=item.operation,
        method=item.method,
        resource=item.resource,
        occurred_at=item.occurred_at,
        reason=item.reason,
    )


_READ: Final = PermissionKey.CONCESSIONS_READ.value
_REVOKE: Final = PermissionKey.CONCESSIONS_REVOKE.value
_GRANT: Final = PermissionKey.CONCESSIONS_GRANT.value


def concessions_router() -> APIRouter:
    router = APIRouter(tags=["concesiones"])

    @router.get(
        "/concessions",
        dependencies=[requires(_READ)],
        summary="Concesiones del proveedor sobre la organización (cualquier estado)",
    )
    async def list_concessions(
        request: Request,
        response: Response,
        services: Services,
        plant_id: Annotated[uuid.UUID | None, Query()] = None,
    ) -> ConcessionsView:
        no_store(response)
        service = installed(services.concessions)
        listed = await service.list_concessions(request_context(request), plant_id=plant_id)
        now = service.now()
        return ConcessionsView(concessions=tuple(_client_view(item, now) for item in listed))

    @router.get(
        "/concessions/{concession_id}/queries",
        dependencies=[requires(_READ)],
        summary="Consultas del proveedor bajo una concesión, en orden de llegada",
    )
    async def list_queries(
        concession_id: uuid.UUID,
        request: Request,
        response: Response,
        services: Services,
        after: Annotated[str | None, Query(max_length=MAX_CURSOR_CHARS)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = MAX_PAGE_SIZE,
    ) -> ProviderQueriesPage:
        no_store(response)
        cursor = None if after is None else decode_cursor(after)
        page = await installed(services.concessions).list_provider_queries(
            request_context(request), concession_id, after=cursor, limit=limit
        )
        return ProviderQueriesPage(
            queries=tuple(_query(item) for item in page.items),
            next_after=None if page.next_cursor is None else encode_cursor(page.next_cursor),
        )

    @router.post(
        "/concessions/{concession_id}/revoke",
        dependencies=[requires(_REVOKE)],
        summary="Revocar una concesión de inmediato",
    )
    async def revoke(
        concession_id: uuid.UUID, request: Request, response: Response, services: Services
    ) -> ConcessionView:
        no_store(response)
        service = installed(services.concessions)
        try:
            revoked = await service.revoke(request_context(request), concession_id)
        except ConcessionRejected as error:
            raise _rejected(error) from None
        return _client_view(revoked, service.now())

    @router.post(
        "/provider/concessions",
        status_code=201,
        dependencies=[requires(_GRANT)],
        summary="Concederse acceso temporal a una organización cliente",
    )
    async def grant(
        body: GrantBody, request: Request, response: Response, services: Services
    ) -> ProviderConcessionView:
        no_store(response)
        service = installed(services.concessions)
        try:
            granted = await service.grant(
                request_context(request),
                client_organization_id=body.client_organization_id,
                scope_level=body.scope_level,
                scope_id=body.scope_id,
                reason=body.reason,
                duration=None
                if body.duration_hours is None
                else timedelta(hours=body.duration_hours),
            )
        except ConcessionRejected as error:
            raise _rejected(error) from None
        return _provider_view(granted, service.now())

    @router.get(
        "/provider/concessions",
        dependencies=[requires(_GRANT)],
        summary="Concesiones que la persona del proveedor se concedió",
    )
    async def own_concessions(
        request: Request, response: Response, services: Services
    ) -> ProviderConcessionsView:
        no_store(response)
        service = installed(services.concessions)
        listed = await service.list_own_concessions(request_context(request))
        now = service.now()
        return ProviderConcessionsView(
            concessions=tuple(_provider_view(item, now) for item in listed)
        )

    return router
