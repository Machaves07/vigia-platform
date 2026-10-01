"""Los diez eslabones ASGI de la cadena fija (PAT-NUC-SEG-06, LC-NUC-20; pendiente nº 37).

Cada clase es un middleware ASGI puro (sin ``BaseHTTPMiddleware``: no copia el cuerpo ni rompe
las variables de contexto) con su ``step``. Al pasar, deja su nombre en la traza de la petición
(``request_state``). Un eslabón que rechaza lanza ``ApiError``: el manejador global (paso 2) lo
convierte en la respuesta genérica, con las cabeceras de seguridad y el ``correlation_id``.

1. ``CorrelationStep``: genera el ``correlation_id`` (UUID v7) y un estado de petición nuevo;
   nunca lee uno del cliente (cabecera, cookie, consulta o cuerpo). Lo pone en el contexto de los
   registros y, al terminar, publica la latencia y el resultado por ruta y la línea de registro de
   la petición (salvo ``/assets/*``, que solo queda en el registro del balanceador).
2. ``ErrorStep``: manejador global; toda excepción sin responder es un ``ApiError`` genérico.
3. ``SecurityHeadersStep``: cabeceras de seguridad en toda respuesta (también en las de error).
   (nº 37) ``RouteClassStep``: resuelve la ruta y la clase ``node``/``person`` y fija el pool.
4. ``BodyLimitStep``: 1 MB (o el ``body_limit`` de la ruta); lee el cuerpo entero antes de
   seguir, así que un cuerpo excedido es siempre ``payload_too_large``, con o sin
   ``Content-Length``.
5. ``OriginRateLimitStep``: 1 200 por minuto por origen en rutas con permiso; 60 en las públicas
   y en lo que no es ninguna ruta; fuera, los estáticos y las rutas de nodos.
6. ``SessionStep``: en las rutas con permiso, el ``ScopeContext`` de la cookie de sesión con el
   ``correlation_id`` de la petición (``context_from_session``). Cookie mal formada o repetida:
   ``unauthenticated``.
7. ``SessionRateLimitStep``: 600 por minuto por sesión.
8. ``CsrfStep``: métodos que cambian estado exigen ``Sec-Fetch-Site: same-origin`` y, si viene,
   ``Origin`` igual al configurado; si no, ``forbidden`` y ``csrf_rejected`` en la auditoría.
9. ``PrivacyNoticeStep``: con sesión y sin la versión vigente del aviso aceptada, solo responde la
   ruta de aceptación; el resto, ``privacy_notice_required``.
"""

from __future__ import annotations

import enum
import re
import uuid
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass
from typing import Any, ClassVar, Final, Protocol

from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME, SessionCookie, origin_hash
from vigia_platform.identity.authz.context import SessionScope
from vigia_platform.shared.api.errors import (
    ApiError,
    ApiErrorCode,
    ErrorCatalog,
    translate,
)
from vigia_platform.shared.api.middleware.headers import SecurityHeaders
from vigia_platform.shared.api.middleware.routing import (
    RATE_EXEMPT_ROUTES,
    RouteTable,
    route_class_of,
)
from vigia_platform.shared.api.request_state import (
    STATE_KEY,
    ChainStep,
    RequestState,
    request_state,
)
from vigia_platform.shared.api.static import is_request_logged
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.db import RouteClass, route_class_scope
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.observability.logging import get_logger, log_context
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics
from vigia_platform.shared.ratelimit import (
    AUTHENTICATED_ORIGIN_BUDGET,
    PUBLIC_ORIGIN_BUDGET,
    SESSION_BUDGET,
    Budget,
    Limited,
    RateLimiter,
    origin_key,
    public_key,
    session_key,
)

__all__ = [
    "DEFAULT_BODY_LIMIT_BYTES",
    "PRIVACY_NOTICE_ACCEPT",
    "SAFE_METHODS",
    "STEP_CLASSES",
    "UNMATCHED_ROUTE",
    "BodyLimitStep",
    "ChainSettings",
    "CorrelationStep",
    "CsrfAuditPort",
    "CsrfReason",
    "CsrfRejection",
    "CsrfStep",
    "ErrorStep",
    "OriginRateLimitStep",
    "PrivacyNoticeStep",
    "RouteClassStep",
    "SecurityHeadersStep",
    "SessionContextPort",
    "SessionRateLimitStep",
    "SessionStep",
]

DEFAULT_BODY_LIMIT_BYTES: Final = 1024 * 1024
"""1 MB (BR-NUC-92; las rutas del contrato de U-03 declaran el suyo con ``body_limit``)."""
SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
"""Métodos que no cambian estado (PAT-NUC-SEG-02); todos los demás pasan la barrera."""
PRIVACY_NOTICE_ACCEPT: Final = ("POST", "/privacy-notice/accept")
"""La única ruta que responde a una sesión sin el aviso vigente aceptado (TASK-135 la crea)."""
CONCESSION_HEADER: Final = b"x-vigia-concession"
_UUID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_CONTENT_LENGTH: Final = re.compile(r"[0-9]{1,19}")
UNMATCHED_ROUTE: Final = "unmatched"
"""Valor del atributo ``route`` de una petición que no coincide con ninguna ruta."""

_log = get_logger("shared.api.middleware")

type _Message = MutableMapping[str, Any]
type _Receive = Callable[[], Awaitable[_Message]]
type _Send = Callable[[_Message], Awaitable[None]]
type _Asgi = Callable[[MutableMapping[str, Any], _Receive, _Send], Awaitable[None]]


# --- Puertos y ajustes ---------------------------------------------------------------------------


class SessionContextPort(Protocol):
    """``ScopeContexts.context_from_session`` (``identity.authz.context``)."""

    async def context_from_session(
        self,
        cookie: SessionCookie | None,
        *,
        concession_id: uuid.UUID | None = None,
        correlation_id: uuid.UUID | None = None,
    ) -> SessionScope: ...


class CsrfReason(enum.StrEnum):
    """Por qué la barrera rechazó la petición (va en ``filters`` de ``csrf_rejected``)."""

    FETCH_SITE_MISSING = "fetch_site_missing"
    FETCH_SITE_NOT_SAME_ORIGIN = "fetch_site_not_same_origin"
    ORIGIN_MISMATCH = "origin_mismatch"


@dataclass(frozen=True, slots=True)
class CsrfRejection:
    """Lo que se audita de un rechazo: nunca la cabecera, la cookie ni el origen en claro."""

    reason: CsrfReason
    method: str
    route: str
    """Plantilla de la ruta resuelta o ``unmatched``."""
    origin_hash: str | None
    """HMAC del origen de red (``identity.auth.sessions.origin_hash``), si hay clave."""


class CsrfAuditPort(Protocol):
    """Escribe ``csrf_rejected`` (``outcome = denied``) en la auditoría."""

    async def csrf_rejected(self, context: ScopeContext, rejection: CsrfRejection) -> None:
        """Con sesión: en la cadena de la organización de ``context``."""
        ...

    async def csrf_rejected_without_session(self, rejection: CsrfRejection) -> None:
        """Sin sesión: en la cadena de la organización proveedora (BR-NUC-61)."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class ChainSettings:
    """Lo que necesitan los eslabones; uno por aplicación."""

    clock: Clock
    catalog: ErrorCatalog
    headers: SecurityHeaders
    routes: RouteTable
    limiter: RateLimiter
    public_origin: str | None = None
    """Origen de la aplicación (``https://app.<dominio>``); sin él, un ``Origin`` se rechaza."""
    sessions: SessionContextPort | None = None
    csrf_audit: CsrfAuditPort | None = None
    origin_secret: bytes | None = None
    """Clave del HMAC del origen de red en ``csrf_rejected`` (la de ``AuthThrottle``)."""
    privacy_notice_version: str | None = None
    """Versión vigente del aviso; sin ella ninguna sesión la tiene aceptada (fallo cerrado)."""
    metrics: PlatformMetrics | None = None
    body_limit_bytes: int = DEFAULT_BODY_LIMIT_BYTES

    @property
    def instruments(self) -> PlatformMetrics:
        return self.metrics if self.metrics is not None else get_metrics()


# --- Utilidades ----------------------------------------------------------------------------------


def _header_values(scope: MutableMapping[str, Any], name: bytes) -> list[bytes]:
    return [bytes(value) for key, value in scope.get("headers", ()) if bytes(key).lower() == name]


def _route_name(state: RequestState) -> str:
    return state.route.path if state.route is not None else UNMATCHED_ROUTE


def _client_address(scope: MutableMapping[str, Any]) -> str:
    client = scope.get("client")
    if isinstance(client, tuple | list) and client and isinstance(client[0], str):
        return client[0]
    return "unknown"


def _status_class(status: int) -> str:
    return f"{min(max(status // 100, 1), 5)}xx"


class _Step:
    """Base: deja el nombre del eslabón en la traza y delega en ``handle``."""

    step: ClassVar[ChainStep]

    def __init__(self, app: _Asgi, *, chain: ChainSettings) -> None:
        self._app = app
        self._chain = chain

    async def __call__(
        self, scope: MutableMapping[str, Any], receive: _Receive, send: _Send
    ) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        state = request_state(scope)
        state.trace.append(self.step)
        await self.handle(scope, receive, send, state)

    async def handle(
        self, scope: MutableMapping[str, Any], receive: _Receive, send: _Send, state: RequestState
    ) -> None:
        raise NotImplementedError

    def _limit(self, key: str, budget: Budget, state: RequestState, kind: str) -> None:
        verdict = self._chain.limiter.check(key, budget)
        if isinstance(verdict, Limited):
            self._chain.instruments.rate_limited_total.add(
                1, {"route": _route_name(state), "rate_limit": kind}
            )
            raise ApiError(
                ApiErrorCode.RATE_LIMITED, retry_after_seconds=verdict.retry_after_seconds
            )


# --- (1) correlation_id -------------------------------------------------------------------------


class CorrelationStep(_Step):
    step = ChainStep.CORRELATION

    async def __call__(
        self, scope: MutableMapping[str, Any], receive: _Receive, send: _Send
    ) -> None:
        if scope["type"] == "websocket":
            # La interfaz no tiene WebSocket: nada se salta la cadena por esa vía.
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] == "http":
            # Estado nuevo en cada petición: nada que venga de antes (ni del cliente) cuenta.
            scope.setdefault("state", {})[STATE_KEY] = RequestState()
        await super().__call__(scope, receive, send)

    async def handle(
        self, scope: MutableMapping[str, Any], receive: _Receive, send: _Send, state: RequestState
    ) -> None:
        clock = self._chain.clock
        correlation_id = uuid7(clock)
        state.correlation_id = correlation_id
        scope["state"]["correlation_id"] = correlation_id
        started = clock.monotonic()
        status = 500

        async def tracked(message: _Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        with log_context(correlation_id=correlation_id):
            try:
                await self._app(scope, receive, tracked)
            finally:
                self._record(scope, state, status, (clock.monotonic() - started) * 1000)

    def _record(
        self,
        scope: MutableMapping[str, Any],
        state: RequestState,
        status: int,
        duration_ms: float,
    ) -> None:
        route = _route_name(state)
        method = str(scope.get("method", ""))
        status_class = _status_class(status)
        instruments = self._chain.instruments
        instruments.http_server_requests_total.add(
            1, {"route": route, "method": method, "status_class": status_class}
        )
        instruments.http_server_duration_ms.record(duration_ms, {"route": route, "method": method})
        if status >= 400:
            instruments.http_server_errors_total.add(
                1, {"route": route, "status_class": status_class}
            )
        if is_request_logged(str(scope.get("path", ""))):
            _log.info(
                "petición atendida",
                route=route,
                method=method,
                status=status,
                duration_ms=round(duration_ms, 3),
            )


# --- (2) manejador global de errores ------------------------------------------------------------


class ErrorStep(_Step):
    """Toda excepción sin responder es un ``ApiError`` genérico con las cabeceras de seguridad.

    Si la respuesta ya empezó no se puede cambiar: la excepción sube y la conexión se corta. La
    transacción ya se revirtió y la conexión ya se liberó al llegar aquí (``shared.db``).
    """

    step = ChainStep.ERRORS

    async def handle(
        self, scope: MutableMapping[str, Any], receive: _Receive, send: _Send, state: RequestState
    ) -> None:
        started = False

        async def tracked(message: _Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self._app(scope, receive, tracked)
        except Exception as error:
            api_error = translate(error)
            if api_error.code is ApiErrorCode.INTERNAL_ERROR:
                _log.exception("excepción no controlada en una petición")
            if started:
                raise
            correlation_id = state.correlation_id or uuid7(self._chain.clock)
            response = self._chain.catalog.response(api_error, correlation_id)
            headers = self._chain.headers

            async def secured(message: _Message) -> None:
                headers.apply(message)
                await send(message)

            await response(scope, receive, secured)


# --- (3) cabeceras de seguridad -----------------------------------------------------------------


class SecurityHeadersStep(_Step):
    step = ChainStep.SECURITY_HEADERS

    async def handle(
        self, scope: MutableMapping[str, Any], receive: _Receive, send: _Send, state: RequestState
    ) -> None:
        headers = self._chain.headers

        async def secured(message: _Message) -> None:
            headers.apply(message)
            await send(message)

        await self._app(scope, receive, secured)


# --- (nº 37) clase de ruta ----------------------------------------------------------------------


class RouteClassStep(_Step):
    step = ChainStep.ROUTE_CLASS

    async def handle(
        self, scope: MutableMapping[str, Any], receive: _Receive, send: _Send, state: RequestState
    ) -> None:
        state.route = self._chain.routes.resolve(scope)
        state.route_class = route_class_of(str(scope.get("path", "")))
        state.resolved = True
        with route_class_scope(state.route_class):
            await self._app(scope, receive, send)


# --- (4) límite de cuerpo ------------------------------------------------------------------------


class BodyLimitStep(_Step):
    step = ChainStep.BODY_LIMIT

    def _limit_for(self, state: RequestState) -> int:
        route = state.route
        if route is not None and len(route.body_limits) == 1:
            return route.body_limits[0]
        return self._chain.body_limit_bytes

    async def handle(
        self, scope: MutableMapping[str, Any], receive: _Receive, send: _Send, state: RequestState
    ) -> None:
        limit = self._limit_for(state)
        declared = _header_values(scope, b"content-length")
        if declared:
            text = {value.strip().decode("latin-1") for value in declared}
            if len(text) != 1 or _CONTENT_LENGTH.fullmatch(next(iter(text))) is None:
                raise ApiError(ApiErrorCode.INVALID_REQUEST)
            if int(next(iter(text))) > limit:
                raise ApiError(ApiErrorCode.PAYLOAD_TOO_LARGE)
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                # Desconexión antes de terminar el cuerpo: se entrega tal cual y no se sigue.
                pending: list[_Message] = [message]
                break
            body = bytes(message.get("body", b""))
            size += len(body)
            if size > limit:
                raise ApiError(ApiErrorCode.PAYLOAD_TOO_LARGE)
            chunks.append(body)
            if not message.get("more_body", False):
                pending = [{"type": "http.request", "body": b"".join(chunks), "more_body": False}]
                break

        async def replay() -> _Message:
            if pending:
                return pending.pop(0)
            return await receive()

        await self._app(scope, replay, send)


# --- (5) y (7) límites de tasa ------------------------------------------------------------------


class OriginRateLimitStep(_Step):
    step = ChainStep.ORIGIN_RATE_LIMIT

    async def handle(
        self, scope: MutableMapping[str, Any], receive: _Receive, send: _Send, state: RequestState
    ) -> None:
        route = state.route
        exempt = route is not None and any(
            declaration.unauthenticated in RATE_EXEMPT_ROUTES for declaration in route.declarations
        )
        if state.route_class is RouteClass.PERSON and not exempt:
            address = _client_address(scope)
            if _requires_permission(state):
                self._limit(origin_key(address), AUTHENTICATED_ORIGIN_BUDGET, state, "origin")
            else:
                self._limit(public_key(address), PUBLIC_ORIGIN_BUDGET, state, "public")
        await self._app(scope, receive, send)


class SessionRateLimitStep(_Step):
    step = ChainStep.SESSION_RATE_LIMIT

    async def handle(
        self, scope: MutableMapping[str, Any], receive: _Receive, send: _Send, state: RequestState
    ) -> None:
        session = state.session
        if session is not None and session.context.session_id_hash is not None:
            self._limit(
                session_key(session.context.session_id_hash), SESSION_BUDGET, state, "session"
            )
        await self._app(scope, receive, send)


def _requires_permission(state: RequestState) -> bool:
    route = state.route
    return route is not None and any(d.permission is not None for d in route.declarations)


# --- (6) sesión y contexto ----------------------------------------------------------------------


def _session_cookie(scope: MutableMapping[str, Any]) -> tuple[bool, SessionCookie | None]:
    """``(presente, cookie)``: la cookie de sesión si aparece exactamente una vez y bien formada."""
    values: list[str] = []
    prefix = SESSION_COOKIE_NAME + "="
    for header in _header_values(scope, b"cookie"):
        for item in header.decode("latin-1").split(";"):
            item = item.strip()
            if item.startswith(prefix):
                values.append(item[len(prefix) :])
    if not values:
        return False, None
    if len(values) != 1:
        return True, None
    return True, SessionCookie.parse(values[0])


def _concession(scope: MutableMapping[str, Any]) -> uuid.UUID | None:
    values = _header_values(scope, CONCESSION_HEADER)
    if not values:
        return None
    text = values[0].strip().decode("latin-1")
    if len(values) != 1 or _UUID.fullmatch(text) is None:
        # Concesión ilegible: igual que una inexistente (BR-NUC-04), nunca otra organización.
        raise ApiError(ApiErrorCode.NOT_FOUND)
    return uuid.UUID(text)


class SessionStep(_Step):
    step = ChainStep.SESSION

    async def handle(
        self, scope: MutableMapping[str, Any], receive: _Receive, send: _Send, state: RequestState
    ) -> None:
        sessions = self._chain.sessions
        if (
            state.route_class is RouteClass.NODE
            or sessions is None
            or not _requires_permission(state)
        ):
            await self._app(scope, receive, send)
            return
        present, cookie = _session_cookie(scope)
        if not present:
            # Sin cookie no hay contexto: la autorización de la ruta responde ``unauthenticated``.
            await self._app(scope, receive, send)
            return
        if cookie is None:
            raise ApiError(ApiErrorCode.UNAUTHENTICATED)
        session = await sessions.context_from_session(
            cookie, concession_id=_concession(scope), correlation_id=state.correlation_id
        )
        if not isinstance(session, SessionScope):
            raise ApiError(ApiErrorCode.INTERNAL_ERROR)
        if session.context.correlation_id != state.correlation_id:
            # PR-NUC-55: el contexto lleva el correlation_id de la plataforma o no hay contexto.
            raise ApiError(ApiErrorCode.INTERNAL_ERROR)
        state.session = session
        context = session.context
        with log_context(organization_id=context.organization_id, actor_id=context.actor.id):
            await self._app(scope, receive, send)


# --- (8) barrera anti-falsificación -------------------------------------------------------------


class CsrfStep(_Step):
    step = ChainStep.CSRF

    def _reason(self, scope: MutableMapping[str, Any]) -> CsrfReason | None:
        fetch_site = _header_values(scope, b"sec-fetch-site")
        if not fetch_site:
            return CsrfReason.FETCH_SITE_MISSING
        if fetch_site != [b"same-origin"]:
            return CsrfReason.FETCH_SITE_NOT_SAME_ORIGIN
        origins = _header_values(scope, b"origin")
        if origins:
            expected = self._chain.public_origin
            if expected is None or origins != [expected.encode("latin-1")]:
                return CsrfReason.ORIGIN_MISMATCH
        return None

    async def handle(
        self, scope: MutableMapping[str, Any], receive: _Receive, send: _Send, state: RequestState
    ) -> None:
        method = str(scope.get("method", ""))
        if state.route_class is RouteClass.NODE or method in SAFE_METHODS:
            await self._app(scope, receive, send)
            return
        reason = self._reason(scope)
        if reason is not None:
            await self._audit(scope, state, reason, method)
            raise ApiError(ApiErrorCode.FORBIDDEN)
        await self._app(scope, receive, send)

    async def _audit(
        self,
        scope: MutableMapping[str, Any],
        state: RequestState,
        reason: CsrfReason,
        method: str,
    ) -> None:
        audit = self._chain.csrf_audit
        if audit is None:
            _log.warning("petición falsificada rechazada sin auditoría instalada")
            return
        secret = self._chain.origin_secret
        rejection = CsrfRejection(
            reason=reason,
            method=method if method.isascii() and method.isalpha() and len(method) <= 16 else "_",
            route=_route_name(state),
            origin_hash=None if secret is None else origin_hash(_client_address(scope), secret),
        )
        try:
            if state.session is not None:
                await audit.csrf_rejected(state.session.context, rejection)
            else:
                await audit.csrf_rejected_without_session(rejection)
        except Exception:
            # La respuesta sigue siendo ``forbidden``; el fallo de la auditoría va al registro.
            _log.exception("no se pudo auditar csrf_rejected")


# --- (9) aviso de tratamiento -------------------------------------------------------------------


class PrivacyNoticeStep(_Step):
    step = ChainStep.PRIVACY_NOTICE

    async def handle(
        self, scope: MutableMapping[str, Any], receive: _Receive, send: _Send, state: RequestState
    ) -> None:
        session = state.session
        if session is not None:
            route = state.route
            accepting = (
                route is not None
                and (str(scope.get("method", "")), route.path) == PRIVACY_NOTICE_ACCEPT
            )
            current = self._chain.privacy_notice_version
            if not accepting and (
                current is None or session.privacy_notice_version_accepted != current
            ):
                raise ApiError(ApiErrorCode.PRIVACY_NOTICE_REQUIRED)
        await self._app(scope, receive, send)


STEP_CLASSES: Final[dict[ChainStep, type[_Step]]] = {
    ChainStep.CORRELATION: CorrelationStep,
    ChainStep.ERRORS: ErrorStep,
    ChainStep.SECURITY_HEADERS: SecurityHeadersStep,
    ChainStep.ROUTE_CLASS: RouteClassStep,
    ChainStep.BODY_LIMIT: BodyLimitStep,
    ChainStep.ORIGIN_RATE_LIMIT: OriginRateLimitStep,
    ChainStep.SESSION: SessionStep,
    ChainStep.SESSION_RATE_LIMIT: SessionRateLimitStep,
    ChainStep.CSRF: CsrfStep,
    ChainStep.PRIVACY_NOTICE: PrivacyNoticeStep,
}
"""El middleware ASGI de cada eslabón (la autorización es la dependencia de cada ruta)."""
