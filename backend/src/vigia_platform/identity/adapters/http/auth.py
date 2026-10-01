"""Rutas de sesión (``business-logic-model.md`` §1 y §10.2; BR-NUC-22 a 27; H-58).

Públicas (lista cerrada ``UnauthenticatedRoute``; actúan solo sobre la cookie que traen):

- ``POST /auth/login``: correo y contraseña. Con los dos pasos superados (o sin segundo factor),
  ``authenticated``; si el usuario tiene el segundo factor inscrito o requerido, la sesión queda
  **pendiente**: ``second_factor_required`` o, si aún no lo inscribió,
  ``second_factor_enrollment_required``. En los tres casos la respuesta fija la cookie
  ``__Host-vigia_session`` con ``Secure; HttpOnly; SameSite=Strict; Path=/`` y sin ``Max-Age``
  (BR-NUC-25): el vencimiento lo decide el servidor.
- ``POST /auth/second-factor``: TOTP o código de recuperación con la sesión pendiente.
- ``POST /auth/second-factor/enroll``: inscripción obligatoria con la sesión pendiente (H-58,
  BR-NUC-22). Sin ``code`` empieza (QR, URI y códigos de recuperación, que se muestran **una
  vez**); con ``code`` la confirma con un primer TOTP y completa el inicio de sesión. Hasta
  entonces, un administrador sin segundo factor no obtiene nada más: la sesión pendiente no sirve
  para ninguna ruta con sesión (``unauthenticated``) y el segundo paso responde
  ``second_factor_required``.
- ``POST /auth/logout``: cierra en el servidor la sesión de la cookie aunque haya vencido o falte el
  aviso, y la borra del navegador; responde igual exista o no (BR-NUC-27).

Con sesión (lista cerrada ``SessionRoute``):

- ``GET /auth/sessions``: las sesiones activas de la persona con ``client_hint``, fechas y cuál es
  la actual; nunca su identificador ni su hash (BR-NUC-26).
- ``POST /auth/sessions/close-others``: cierra las demás.
- ``POST /auth/password``: cambio de contraseña (``identity.application.password_change``).

**Mensajes uniformes** (BR-NUC-23): correo desconocido, contraseña incorrecta, cuenta inactiva,
código incorrecto, sesión pendiente inexistente o vencida responden todos ``unauthenticated`` con
el mismo mensaje del catálogo; el retardo, ``throttled`` con ``Retry-After``. El cuerpo de error
nunca repite lo recibido.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from vigia_platform.identity.adapters.http.services import (
    IdentityHttp,
    client_address,
    identity_http,
    no_store,
    session_cookie,
)
from vigia_platform.identity.application.password_change import (
    PasswordChangeRejected,
    PasswordChangeRejection,
)
from vigia_platform.identity.auth.login import (
    Authenticated,
    LoginOutcome,
    Rejected,
    RejectionCode,
)
from vigia_platform.identity.auth.second_factor import AlreadyEnrolled, SecondFactorNotFound
from vigia_platform.identity.auth.sessions import SessionCookie, SessionStatus, clearing_cookie
from vigia_platform.shared.api.declarations import (
    SessionRoute,
    UnauthenticatedRoute,
    authenticated,
    unauthenticated,
)
from vigia_platform.shared.api.errors import MAX_RETRY_AFTER_SECONDS, ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "MAX_LISTED_SESSIONS",
    "EnrollmentView",
    "LoginRequest",
    "LoginResponse",
    "auth_router",
]

MAX_LISTED_SESSIONS: Final = 50
"""Tope de sesiones activas que lista ``GET /auth/sessions`` (las más recientes)."""

_PASSWORD_MAX_CHARS: Final = 128
"""Ninguna contraseña válida supera 128 caracteres (BR-NUC-20)."""
_NEW_PASSWORD_MAX_CHARS: Final = 1024
"""La nueva contraseña la juzga la política (con su motivo), no el esquema."""
_CODE_MAX_CHARS: Final = 64

Services = Annotated[IdentityHttp, Depends(identity_http)]


# --- Modelos --------------------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LoginRequest(_Strict):
    email: StrictStr = Field(min_length=1, max_length=254)
    password: StrictStr = Field(min_length=1, max_length=_PASSWORD_MAX_CHARS)


class SecondFactorRequest(_Strict):
    code: StrictStr = Field(min_length=1, max_length=_CODE_MAX_CHARS)
    """TOTP de 6 dígitos o código de recuperación."""


class EnrollRequest(_Strict):
    code: StrictStr | None = Field(default=None, min_length=1, max_length=_CODE_MAX_CHARS)
    """Sin él, empieza la inscripción; con él, la confirma con el primer TOTP."""


class PasswordChangeRequest(_Strict):
    current_password: StrictStr = Field(min_length=1, max_length=_PASSWORD_MAX_CHARS)
    new_password: StrictStr = Field(min_length=1, max_length=_NEW_PASSWORD_MAX_CHARS)


LoginStatus = Literal[
    "authenticated", "second_factor_required", "second_factor_enrollment_required"
]


class LoginResponse(_Strict):
    status: LoginStatus


class EnrollmentView(_Strict):
    """Lo que se muestra **una sola vez** al inscribirse; nada de esto se guarda en claro."""

    provisioning_uri: str
    qr_svg: str
    recovery_codes: tuple[str, ...]


class EnrollResponse(_Strict):
    status: Literal["enrollment_started", "authenticated"]
    enrollment: EnrollmentView | None = None


class SessionView(_Strict):
    """Una sesión activa de la persona (sin identificador ni hash)."""

    created_at: str
    last_seen_at: str
    client_hint: str | None
    current: bool
    usable: bool


class SessionsResponse(_Strict):
    sessions: tuple[SessionView, ...]


class SessionsClosedResponse(_Strict):
    sessions_closed: int


# --- Utilidades -----------------------------------------------------------------------------------


def _set_cookie(response: Response, cookie: SessionCookie) -> None:
    response.headers.append("Set-Cookie", cookie.header_value())


def _rejected(outcome: Rejected) -> ApiError:
    if outcome.code is RejectionCode.THROTTLED:
        seconds = outcome.retry_after_seconds or 1
        return ApiError(
            ApiErrorCode.THROTTLED,
            retry_after_seconds=min(max(seconds, 1), MAX_RETRY_AFTER_SECONDS),
        )
    if outcome.code is RejectionCode.SECOND_FACTOR_REQUIRED:
        return ApiError(ApiErrorCode.SECOND_FACTOR_REQUIRED)
    return ApiError(ApiErrorCode.UNAUTHENTICATED)


def _second_step(outcome: LoginOutcome) -> None:
    """El segundo paso solo termina en ``Authenticated``; todo lo demás es un rechazo."""
    if isinstance(outcome, Rejected):
        raise _rejected(outcome)
    if not isinstance(outcome, Authenticated):
        raise ApiError(ApiErrorCode.UNAUTHENTICATED)


def _timestamp(moment: datetime) -> str:
    return format_timestamp(moment)


_PASSWORD_ERRORS: Final = {
    PasswordChangeRejection.USER_STATE: ApiErrorCode.CONFLICT,
    PasswordChangeRejection.CURRENT_PASSWORD_INVALID: ApiErrorCode.INVALID_REQUEST,
    PasswordChangeRejection.PASSWORD_REJECTED: ApiErrorCode.INVALID_REQUEST,
}


def _password_error(error: PasswordChangeRejected) -> ApiError:
    if error.code is PasswordChangeRejection.THROTTLED:
        seconds = error.retry_after_seconds or 1
        return ApiError(
            ApiErrorCode.THROTTLED,
            retry_after_seconds=min(max(seconds, 1), MAX_RETRY_AFTER_SECONDS),
        )
    return ApiError(_PASSWORD_ERRORS[error.code])


# --- Rutas ----------------------------------------------------------------------------------------


def auth_router() -> APIRouter:
    router = APIRouter(tags=["sesión"])

    @router.post(
        "/auth/login",
        dependencies=[unauthenticated(UnauthenticatedRoute.AUTH_LOGIN)],
        summary="Inicio de sesión: primer paso (correo y contraseña)",
    )
    async def login(
        body: LoginRequest, request: Request, response: Response, services: Services
    ) -> LoginResponse:
        no_store(response)
        outcome = await services.login.authenticate(
            body.email,
            body.password,
            client_address(request),
            request.headers.get("user-agent"),
        )
        if isinstance(outcome, Rejected):
            raise _rejected(outcome)
        _set_cookie(response, outcome.cookie)
        if isinstance(outcome, Authenticated):
            return LoginResponse(status="authenticated")
        if outcome.enrollment_required:
            return LoginResponse(status="second_factor_enrollment_required")
        return LoginResponse(status="second_factor_required")

    @router.post(
        "/auth/second-factor",
        dependencies=[unauthenticated(UnauthenticatedRoute.AUTH_SECOND_FACTOR)],
        summary="Inicio de sesión: segundo paso (TOTP o código de recuperación)",
    )
    async def second_factor(
        body: SecondFactorRequest, request: Request, response: Response, services: Services
    ) -> LoginResponse:
        no_store(response)
        outcome = await services.login.verify_second_factor(session_cookie(request), body.code)
        _second_step(outcome)
        return LoginResponse(status="authenticated")

    @router.post(
        "/auth/second-factor/enroll",
        dependencies=[unauthenticated(UnauthenticatedRoute.AUTH_SECOND_FACTOR_ENROLL)],
        summary="Inscripción obligatoria del segundo factor con la sesión pendiente",
        response_model_exclude_none=True,
    )
    async def enroll(
        body: EnrollRequest, request: Request, response: Response, services: Services
    ) -> EnrollResponse:
        no_store(response)
        cookie = session_cookie(request)
        if body.code is not None:
            _second_step(await services.login.confirm_enrollment(cookie, body.code))
            return EnrollResponse(status="authenticated")
        try:
            challenge = await services.login.start_enrollment(cookie)
        except (AlreadyEnrolled, SecondFactorNotFound):
            challenge = None
        if challenge is None:
            raise ApiError(ApiErrorCode.UNAUTHENTICATED)
        return EnrollResponse(
            status="enrollment_started",
            enrollment=EnrollmentView(
                provisioning_uri=challenge.provisioning_uri,
                qr_svg=challenge.qr_svg,
                recovery_codes=challenge.recovery_codes,
            ),
        )

    @router.post(
        "/auth/logout",
        status_code=204,
        dependencies=[unauthenticated(UnauthenticatedRoute.AUTH_LOGOUT)],
        summary="Cierre de sesión",
    )
    async def logout(request: Request, services: Services) -> Response:
        cookie = session_cookie(request)
        if cookie is not None:
            await services.sessions.close(cookie)
        response = Response(status_code=204)
        no_store(response)
        response.headers.append("Set-Cookie", clearing_cookie())
        return response

    @router.get(
        "/auth/sessions",
        dependencies=[authenticated(SessionRoute.AUTH_SESSIONS)],
        summary="Sesiones activas de la persona",
    )
    async def sessions(
        request: Request, response: Response, services: Services
    ) -> SessionsResponse:
        no_store(response)
        context = request_context(request)
        listed = await services.sessions.list_sessions(session_cookie(request))
        if listed is None:
            raise ApiError(ApiErrorCode.UNAUTHENTICATED)
        active = [s for s in listed if s.status is SessionStatus.ACTIVE][:MAX_LISTED_SESSIONS]
        return SessionsResponse(
            sessions=tuple(
                SessionView(
                    created_at=_timestamp(summary.created_at),
                    last_seen_at=_timestamp(summary.last_seen_at),
                    client_hint=summary.client_hint,
                    current=summary.session_id_hash == context.session_id_hash,
                    usable=summary.usable,
                )
                for summary in active
            )
        )

    @router.post(
        "/auth/sessions/close-others",
        dependencies=[authenticated(SessionRoute.AUTH_SESSIONS_CLOSE_OTHERS)],
        summary="Cerrar las demás sesiones",
    )
    async def close_others(
        request: Request, response: Response, services: Services
    ) -> SessionsClosedResponse:
        no_store(response)
        closed = await services.sessions.close_others(session_cookie(request))
        if closed is None:
            raise ApiError(ApiErrorCode.UNAUTHENTICATED)
        return SessionsClosedResponse(sessions_closed=closed)

    @router.post(
        "/auth/password",
        dependencies=[authenticated(SessionRoute.AUTH_PASSWORD)],
        summary="Cambio de la contraseña propia",
    )
    async def change_password(
        body: PasswordChangeRequest, request: Request, response: Response, services: Services
    ) -> SessionsClosedResponse:
        no_store(response)
        try:
            changed = await services.passwords.change(
                request_context(request), body.current_password, body.new_password
            )
        except PasswordChangeRejected as error:
            raise _password_error(error) from None
        return SessionsClosedResponse(sessions_closed=changed.sessions_closed)

    return router
