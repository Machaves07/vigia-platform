"""Servicios de las rutas de ``identity`` y utilidades comunes de sus manejadores.

``IdentityHttp`` lo construye la raíz de composición y lo entrega en ``AppRuntime.identity``; la
fábrica lo deja en ``app.state`` junto con la versión vigente del aviso y la versión de la release
(``shared.api.app_state``).

Toda respuesta de estas rutas lleva ``Cache-Control: no-store``: hay cookies de sesión, códigos
de recuperación y datos de la persona.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from fastapi import Request, Response

from vigia_platform.identity.application.common import IdentityRejected
from vigia_platform.identity.application.invitations import InvitationService
from vigia_platform.identity.application.me import MeService
from vigia_platform.identity.application.password_change import PasswordChangeService
from vigia_platform.identity.application.privacy_notice import PrivacyNoticeService
from vigia_platform.identity.auth.login import LoginService
from vigia_platform.identity.auth.sessions import SessionCookie, SessionService
from vigia_platform.shared.api.app_state import API_VERSION_STATE_KEY, PRIVACY_NOTICE_STATE_KEY
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import read_session_cookie
from vigia_platform.shared.observability.logging import get_logger

__all__ = [
    "IDENTITY_STATE_KEY",
    "IdentityHttp",
    "api_version",
    "client_address",
    "identity_error",
    "identity_http",
    "no_store",
    "privacy_notice_version",
    "session_cookie",
]

IDENTITY_STATE_KEY: Final = "vigia_identity_http"

_log = get_logger("identity.http")


@dataclass(frozen=True, slots=True, kw_only=True)
class IdentityHttp:
    """Los servicios que usan las rutas de ``identity``."""

    login: LoginService
    sessions: SessionService
    invitations: InvitationService
    privacy_notice: PrivacyNoticeService
    passwords: PasswordChangeService
    me: MeService
    provider_organization_id: uuid.UUID

    def __post_init__(self) -> None:
        if type(self.provider_organization_id) is not uuid.UUID:
            raise TypeError("provider_organization_id debe ser uuid.UUID")


def identity_http(request: Request) -> IdentityHttp:
    """Los servicios de la aplicación; sin ellos, ``internal_error`` (nunca deja pasar)."""
    services = getattr(request.app.state, IDENTITY_STATE_KEY, None)
    if not isinstance(services, IdentityHttp):
        _log.error("las rutas de identidad no tienen servicios instalados")
        raise ApiError(ApiErrorCode.INTERNAL_ERROR)
    return services


def privacy_notice_version(request: Request) -> str:
    """La versión vigente del aviso que exige la cadena (``AppRuntime``)."""
    version = getattr(request.app.state, PRIVACY_NOTICE_STATE_KEY, None)
    if not isinstance(version, str):
        raise ApiError(ApiErrorCode.INTERNAL_ERROR)
    return version


def api_version(request: Request) -> str:
    """La versión de la release servida: la ``app_version`` de ``version.json`` (nº 7)."""
    version = getattr(request.app.state, API_VERSION_STATE_KEY, None)
    if not isinstance(version, str):
        raise ApiError(ApiErrorCode.INTERNAL_ERROR)
    return version


def session_cookie(request: Request) -> SessionCookie | None:
    """La cookie de sesión si aparece una sola vez y bien formada (el lector de la cadena)."""
    _, cookie = read_session_cookie(request.scope)
    return cookie


def client_address(request: Request) -> str:
    """La dirección de red del cliente (``unknown`` si no la hay); solo va a un HMAC."""
    client = request.client
    return client.host if client is not None and client.host else "unknown"


def no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def identity_error(error: IdentityRejected) -> ApiError:
    """El ``ApiError`` de un rechazo de identidad (código cerrado, mensaje genérico)."""
    return ApiError(ApiErrorCode(error.api_code))
