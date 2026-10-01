"""``GET /me`` y la aceptación del aviso de tratamiento (§10.2; adenda A-15; NFR-NUC-29).

- ``GET /me`` (sesión): identidad (identificador, nombre y correo propios), organización de la
  sesión, asignaciones vigentes, permisos efectivos y, bajo concesión, su identificador. Además, los
  pendientes aditivos de U-05 aceptados en TASK-102 (A-15):

  * nº 5: ``idle_expires_at`` y ``absolute_expires_at`` (BR-NUC-25; el primero ya prolongado por
    esta misma petición) y ``privacy_notice_version``, la versión vigente del aviso;
  * nº 7: ``api_version``, la versión de la release desplegada (la ``app_version`` de
    ``/version.json``), también en la cabecera ``X-Vigia-Api-Version`` (NFR-APP-13).

  Nunca el identificador de sesión, su hash ni el de la contraseña.

- ``POST /privacy-notice/accept`` (sesión): la persona acepta la versión vigente. Es la única ruta
  con sesión que la cadena deja pasar sin el aviso vigente aceptado (``PrivacyNoticeStep``); en la
  siguiente petición la sesión ya sirve. Otra versión: ``invalid_request``; bajo concesión o con la
  cuenta inactiva: ``conflict``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from vigia_platform.identity.adapters.http.services import (
    IdentityHttp,
    api_version,
    identity_error,
    identity_http,
    no_store,
    privacy_notice_version,
)
from vigia_platform.identity.application.common import IdentityRejected
from vigia_platform.shared.api.declarations import SessionRoute, authenticated
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_session
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = ["API_VERSION_HEADER", "MeResponse", "me_router"]

API_VERSION_HEADER: Final = "X-Vigia-Api-Version"
"""Cabecera equivalente a ``api_version`` (pendiente nº 7)."""

Services = Annotated[IdentityHttp, Depends(identity_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UserView(_Strict):
    user_id: uuid.UUID
    display_name: str
    email: str


class OrganizationView(_Strict):
    organization_id: uuid.UUID
    code: str
    name: str
    kind: str


class AssignmentView(_Strict):
    role: Role
    scope_level: ScopeLevel
    scope_id: uuid.UUID


class MeResponse(_Strict):
    user: UserView
    organization: OrganizationView
    assignments: tuple[AssignmentView, ...]
    effective_permissions: tuple[str, ...]
    concession_id: uuid.UUID | None
    idle_expires_at: str
    absolute_expires_at: str
    privacy_notice_version: str
    api_version: str


class NoticeAcceptRequest(_Strict):
    notice_version: StrictStr = Field(min_length=1, max_length=32)


class NoticeAcceptResponse(_Strict):
    notice_version: str
    newly_accepted: bool
    """``False`` si la persona ya la tenía aceptada (no se escribió nada)."""


def me_router() -> APIRouter:
    router = APIRouter(tags=["yo"])

    @router.get(
        "/me",
        dependencies=[authenticated(SessionRoute.ME)],
        summary="La persona de la sesión: identidad, asignaciones, permisos y vencimientos",
    )
    async def me(request: Request, response: Response, services: Services) -> MeResponse:
        no_store(response)
        session = request_session(request)
        if session is None:
            raise ApiError(ApiErrorCode.UNAUTHENTICATED)
        view = await services.me.profile(session.context, session)
        if view is None:
            raise ApiError(ApiErrorCode.UNAUTHENTICATED)
        version = api_version(request)
        response.headers[API_VERSION_HEADER] = version
        organization = view.organization
        return MeResponse(
            user=UserView(user_id=view.user_id, display_name=view.display_name, email=view.email),
            organization=OrganizationView(
                organization_id=organization.organization_id,
                code=organization.code,
                name=organization.name,
                kind=organization.kind,
            ),
            assignments=tuple(
                AssignmentView(role=s.role, scope_level=s.scope_level, scope_id=s.scope_id)
                for s in view.assignments
            ),
            effective_permissions=view.effective_permissions,
            concession_id=view.concession_id,
            idle_expires_at=format_timestamp(view.idle_expires_at),
            absolute_expires_at=format_timestamp(view.absolute_expires_at),
            privacy_notice_version=privacy_notice_version(request),
            api_version=version,
        )

    @router.post(
        "/privacy-notice/accept",
        dependencies=[authenticated(SessionRoute.PRIVACY_NOTICE_ACCEPT)],
        summary="Aceptación de la versión vigente del aviso de tratamiento",
    )
    async def accept_notice(
        body: NoticeAcceptRequest, request: Request, response: Response, services: Services
    ) -> NoticeAcceptResponse:
        no_store(response)
        session = request_session(request)
        if session is None:
            raise ApiError(ApiErrorCode.UNAUTHENTICATED)
        if body.notice_version != privacy_notice_version(request):
            raise ApiError(ApiErrorCode.INVALID_REQUEST)
        try:
            accepted = await services.privacy_notice.accept(session.context, body.notice_version)
        except IdentityRejected as error:
            raise identity_error(error) from None
        return NoticeAcceptResponse(notice_version=body.notice_version, newly_accepted=accepted)

    return router
