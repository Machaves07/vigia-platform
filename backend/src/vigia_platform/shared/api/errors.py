"""Tabla cerrada de errores y fallo cerrado por petición (LC-NUC-21; BR-NUC-93; NFR-NUC-30, 51).

Toda respuesta de error de la plataforma es un ``ApiError`` genérico (domain-entities §4.4 con el
pendiente nº 33): ``{code, detail_code?, message_es, correlation_id, retry_after_seconds?}``.

- ``code`` es de ``api_error_code`` (``ApiErrorCode``): las once de §1 más las cuatro del
  pendiente nº 33 (``privacy_notice_required``, ``period_too_long``, ``zone_without_node`` y
  ``storage_unavailable``). ``csrf_rejected`` **no** es un código: es solo una operación de
  auditoría y la respuesta es ``forbidden`` (nota del 2026-09-23 de §1).
- ``detail_code`` es opcional: ``snake_case`` de una lista cerrada que registra cada unidad con su
  prefijo (``catalog_``, ``fleet_``, ``loop_``, ``accreditation_``, ``notifications_``, ``app_``)
  en ``DetailCodeRegistry``. Nunca sustituye a ``code``; un valor sin prefijo registrado impide
  arrancar (``ApiStartupError``) y un valor sin registrar en una respuesta se convierte en
  ``internal_error`` (fallo cerrado).
- ``message_es`` sale de ``labels.platform.es.json`` (``api_error_code``): genérico y accionable,
  nunca un texto del llamador, una traza, una ruta interna, una versión ni un detalle de la base.
- ``retry_after_seconds`` solo en los códigos transitorios (``TRANSIENT_CODES``); va también en
  la cabecera ``Retry-After``.

``translate`` convierte cualquier excepción en un ``ApiError``: ``ApiError`` tal cual;
``ContextAbsent`` y todo lo desconocido en ``internal_error`` (una excepción en autorización,
contexto o validación **deniega**, BR-NUC-93); ``ExternalDependencyDown``, los fallos
transitorios de la base, del gestor de secretos, el mamparo saturado (``BulkheadSaturated``, con
su ``retry_after_seconds``) y los tiempos de espera en ``temporarily_unavailable``; el almacén
caído en ``storage_unavailable``; la validación de FastAPI en ``invalid_request``;
``ResourceNotFound`` de ``authorize`` (sin la clave o fuera de alcance) en ``not_found``, nunca
``forbidden`` (BR-NUC-09); ``ContextUnavailable`` en ``unauthenticated`` (sin sesión utilizable)
o ``not_found`` (concesión no vigente).
``from_ledger_rejection`` traduce un ``LedgerRejection``.

``ErrorBoundary`` es el manejador global (paso 2 de la cadena de PAT-NUC-SEG-06, que ordena
TASK-134): envuelve la aplicación ASGI y responde un ``ApiError`` ante cualquier excepción que
no se haya respondido. La transacción ya se revirtió y la conexión ya se liberó al llegar aquí:
``shared.db.Database.transaction`` lo hace al propagarse la excepción por su ``async with``
(reversión y liberación garantizadas), así que el manejador nunca toca la base.
"""

from __future__ import annotations

import enum
import os
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, MutableMapping
from typing import Any, Final

from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.identity.authz.context import ContextUnavailable, ContextUnavailableReason
from vigia_platform.ledger.application.writer import LedgerRejection, LedgerRejectionCode
from vigia_platform.shared.api.labels import PlatformLabels
from vigia_platform.shared.bulkheads import BulkheadSaturated
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ContextAbsent
from vigia_platform.shared.db import TransientDatabaseError
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.secrets import SecretsUnavailable
from vigia_platform.shared.storage import StorageUnavailable

__all__ = [
    "DEFAULT_RETRY_AFTER_SECONDS",
    "DETAIL_CODE_PREFIXES",
    "HTTP_STATUS",
    "MAX_DETAIL_CODE_CHARS",
    "MAX_MESSAGE_CHARS",
    "MAX_RETRY_AFTER_SECONDS",
    "TRANSIENT_CODES",
    "ApiError",
    "ApiErrorBody",
    "ApiErrorCode",
    "ApiStartupError",
    "DetailCodeRegistry",
    "ErrorBoundary",
    "ErrorCatalog",
    "ExternalDependencyDown",
    "correlation_id_of",
    "from_ledger_rejection",
    "install_error_handlers",
    "translate",
]

MAX_MESSAGE_CHARS: Final = 256
"""Tope de ``message_es`` (domain-entities §4.4)."""
MAX_DETAIL_CODE_CHARS: Final = 64
DEFAULT_RETRY_AFTER_SECONDS: Final = 5
"""``retry_after_seconds`` de un transitorio sin valor propio ``[objetivo propio]``."""
MAX_RETRY_AFTER_SECONDS: Final = 3_600

DETAIL_CODE_PREFIXES: Final = (
    "catalog_",
    "fleet_",
    "loop_",
    "accreditation_",
    "notifications_",
    "app_",
)
"""Prefijos de ``detail_code`` por unidad (pendiente nº 33): U-03, U-04 y U-05."""

_DETAIL_CODE: Final = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")
"""``snake_case`` ASCII de al menos dos palabras (el prefijo y el resto), sin espacios."""

_CORRELATION_STATE_KEY: Final = "correlation_id"

_log = get_logger("shared.api.errors")


class ApiErrorCode(enum.StrEnum):
    """``api_error_code`` (domain-entities §1 con el pendiente nº 33): lista cerrada de U-02."""

    INVALID_REQUEST = "invalid_request"
    NOT_FOUND = "not_found"
    UNAUTHENTICATED = "unauthenticated"
    SECOND_FACTOR_REQUIRED = "second_factor_required"
    FORBIDDEN = "forbidden"
    THROTTLED = "throttled"
    RATE_LIMITED = "rate_limited"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    CONFLICT = "conflict"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    INTERNAL_ERROR = "internal_error"
    PRIVACY_NOTICE_REQUIRED = "privacy_notice_required"
    PERIOD_TOO_LONG = "period_too_long"
    ZONE_WITHOUT_NODE = "zone_without_node"
    STORAGE_UNAVAILABLE = "storage_unavailable"


HTTP_STATUS: Final[Mapping[ApiErrorCode, int]] = {
    ApiErrorCode.INVALID_REQUEST: 400,
    ApiErrorCode.NOT_FOUND: 404,
    ApiErrorCode.UNAUTHENTICATED: 401,
    ApiErrorCode.SECOND_FACTOR_REQUIRED: 401,
    ApiErrorCode.FORBIDDEN: 403,
    ApiErrorCode.THROTTLED: 429,
    ApiErrorCode.RATE_LIMITED: 429,
    ApiErrorCode.PAYLOAD_TOO_LARGE: 413,
    ApiErrorCode.CONFLICT: 409,
    ApiErrorCode.TEMPORARILY_UNAVAILABLE: 503,
    ApiErrorCode.INTERNAL_ERROR: 500,
    ApiErrorCode.PRIVACY_NOTICE_REQUIRED: 403,
    ApiErrorCode.PERIOD_TOO_LONG: 400,
    ApiErrorCode.ZONE_WITHOUT_NODE: 409,
    ApiErrorCode.STORAGE_UNAVAILABLE: 503,
}
"""Estado HTTP de cada código. Un recurso fuera de alcance es ``not_found``, nunca ``forbidden``."""

TRANSIENT_CODES: Final = frozenset(
    {
        ApiErrorCode.THROTTLED,
        ApiErrorCode.RATE_LIMITED,
        ApiErrorCode.TEMPORARILY_UNAVAILABLE,
        ApiErrorCode.STORAGE_UNAVAILABLE,
    }
)
"""Códigos que llevan ``retry_after_seconds`` (y ``Retry-After``); ningún otro lo lleva."""


# --- Errores de arranque y registro de detail_code ------------------------------------------


class ApiStartupError(Exception):
    """Una comprobación estructural de la aplicación falló: el proceso no arranca.

    ``problems`` son frases en español que nombran la ruta, el código o la etiqueta; nunca un
    secreto ni un dato de cliente.
    """

    def __init__(self, problems: Iterable[str]) -> None:
        self.problems = tuple(problems)
        super().__init__(
            "la aplicación no puede arrancar: " + "; ".join(self.problems)
            if self.problems
            else "la aplicación no puede arrancar"
        )


def _detail_code_problem(value: object) -> str | None:
    """Motivo en español por el que ``value`` no puede ser un ``detail_code``, o ``None``."""
    if not isinstance(value, str):
        return "un detail_code debe ser una cadena"
    shown = value if len(value) <= MAX_DETAIL_CODE_CHARS else value[:MAX_DETAIL_CODE_CHARS] + "…"
    if len(value) > MAX_DETAIL_CODE_CHARS:
        return f"el detail_code «{shown}» supera {MAX_DETAIL_CODE_CHARS} caracteres"
    if not value.isascii() or _DETAIL_CODE.fullmatch(value) is None:
        return f"el detail_code «{shown!r}» no es snake_case ASCII"
    if not value.startswith(DETAIL_CODE_PREFIXES):
        return (
            f"el detail_code «{value}» no empieza por un prefijo registrado ("
            + ", ".join(DETAIL_CODE_PREFIXES)
            + ")"
        )
    return None


class DetailCodeRegistry:
    """Lista cerrada de ``detail_code`` que registra cada unidad al arrancar (pendiente nº 33).

    ``register`` falla con ``ApiStartupError`` ante un valor sin prefijo registrado o que no es
    ``snake_case``; tras ``seal`` no admite más registros.
    """

    def __init__(self) -> None:
        self._codes: set[str] = set()
        self._sealed = False

    def register(self, codes: Iterable[str]) -> None:
        if self._sealed:
            raise ApiStartupError(["el registro de detail_code ya está sellado"])
        accepted = list(codes)
        problems = [p for p in map(_detail_code_problem, accepted) if p is not None]
        if problems:
            raise ApiStartupError(problems)
        self._codes.update(accepted)

    def seal(self) -> None:
        self._sealed = True

    @property
    def sealed(self) -> bool:
        return self._sealed

    def __contains__(self, value: object) -> bool:
        return isinstance(value, str) and value in self._codes

    def codes(self) -> frozenset[str]:
        return frozenset(self._codes)


# --- ApiError y excepciones que se traducen ----------------------------------------------------


def _checked_retry(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_RETRY_AFTER_SECONDS:
        raise ValueError(f"retry_after_seconds debe ser un entero de 1 a {MAX_RETRY_AFTER_SECONDS}")
    return value


class ApiError(Exception):
    """Error de la interfaz: ``raise ApiError(ApiErrorCode.NOT_FOUND)``.

    El mensaje en español no se pasa: sale de la etiqueta del código al responder, así que nunca
    puede llevar un dato de la petición. ``retry_after_seconds`` solo en los transitorios (por
    defecto ``DEFAULT_RETRY_AFTER_SECONDS``); en los demás es un error de programación.
    """

    def __init__(
        self,
        code: ApiErrorCode,
        *,
        detail_code: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        if not isinstance(code, ApiErrorCode):
            raise TypeError("code debe ser ApiErrorCode")
        if detail_code is not None:
            problem = _detail_code_problem(detail_code)
            if problem is not None:
                raise ValueError(problem)
        if code in TRANSIENT_CODES:
            retry = (
                DEFAULT_RETRY_AFTER_SECONDS if retry_after_seconds is None else retry_after_seconds
            )
            retry_after_seconds = _checked_retry(retry)
        elif retry_after_seconds is not None:
            raise ValueError(f"{code.value} no es transitorio: no lleva retry_after_seconds")
        super().__init__(code.value)
        self.code = code
        self.detail_code = detail_code
        self.retry_after_seconds = retry_after_seconds

    @property
    def status_code(self) -> int:
        return HTTP_STATUS[self.code]

    def __repr__(self) -> str:
        return f"ApiError({self.code.value!r}, detail_code={self.detail_code!r})"


class ExternalDependencyDown(Exception):
    """Una dependencia externa (correo, asistente…) no responde: transitorio.

    La lanzan los consumidores de la bandeja y las rutas que dependen de un servicio externo; hacia
    la interfaz es ``temporarily_unavailable``. ``dependency`` es un nombre del código, nunca una
    dirección ni un mensaje del servicio.
    """

    def __init__(
        self, dependency: str, *, retry_after_seconds: int = DEFAULT_RETRY_AFTER_SECONDS
    ) -> None:
        super().__init__(f"dependencia externa no disponible: {dependency}")
        self.dependency = dependency
        self.retry_after_seconds = _checked_retry(retry_after_seconds)


_REJECTION_CODES: Final[Mapping[LedgerRejectionCode, ApiErrorCode]] = {
    # Escribir sin contexto o con un tipo no registrado es un error del código que escribe.
    LedgerRejectionCode.CONTEXT_ABSENT: ApiErrorCode.INTERNAL_ERROR,
    LedgerRejectionCode.RECORD_TYPE_UNKNOWN: ApiErrorCode.INTERNAL_ERROR,
    LedgerRejectionCode.CONTENT_INVALID: ApiErrorCode.INVALID_REQUEST,
    LedgerRejectionCode.FREE_TEXT_REJECTED: ApiErrorCode.INVALID_REQUEST,
    LedgerRejectionCode.IDEMPOTENCY_CONFLICT: ApiErrorCode.CONFLICT,
    LedgerRejectionCode.EVIDENCE_MISSING: ApiErrorCode.INVALID_REQUEST,
    LedgerRejectionCode.EVIDENCE_HASH_MISMATCH: ApiErrorCode.INVALID_REQUEST,
    LedgerRejectionCode.EVIDENCE_NOT_ANONYMIZED: ApiErrorCode.INVALID_REQUEST,
    LedgerRejectionCode.CHAIN_LOCKED_TIMEOUT: ApiErrorCode.TEMPORARILY_UNAVAILABLE,
}
"""Traducción de ``ledger_rejection_code`` a la interfaz de personas (U-03 usa
``to_contract_rejection`` del escritor para los nodos)."""


def from_ledger_rejection(rejection: LedgerRejection) -> ApiError:
    """``ApiError`` de un rechazo del expediente; el puntero del campo no sale en la respuesta."""
    if not isinstance(rejection, LedgerRejection):
        raise TypeError("rejection debe ser LedgerRejection")
    return ApiError(_REJECTION_CODES[rejection.code])


_HTTP_EXCEPTION_CODES: Final[Mapping[int, ApiErrorCode]] = {
    400: ApiErrorCode.INVALID_REQUEST,
    401: ApiErrorCode.UNAUTHENTICATED,
    403: ApiErrorCode.FORBIDDEN,
    404: ApiErrorCode.NOT_FOUND,
    # Un método no admitido no revela que la ruta existe (BR-NUC-93).
    405: ApiErrorCode.NOT_FOUND,
    409: ApiErrorCode.CONFLICT,
    413: ApiErrorCode.PAYLOAD_TOO_LARGE,
    422: ApiErrorCode.INVALID_REQUEST,
    429: ApiErrorCode.RATE_LIMITED,
    503: ApiErrorCode.TEMPORARILY_UNAVAILABLE,
}


def translate(error: BaseException) -> ApiError:
    """El ``ApiError`` que responde a ``error``; lo no previsto es ``internal_error``."""
    if isinstance(error, ApiError):
        return error
    if isinstance(error, RequestValidationError):
        return ApiError(ApiErrorCode.INVALID_REQUEST)
    if isinstance(error, StarletteHTTPException):
        return ApiError(_HTTP_EXCEPTION_CODES.get(error.status_code, ApiErrorCode.INTERNAL_ERROR))
    if isinstance(error, StorageUnavailable):
        return ApiError(ApiErrorCode.STORAGE_UNAVAILABLE)
    if isinstance(
        error,
        ExternalDependencyDown | SecretsUnavailable | TransientDatabaseError | BulkheadSaturated,
    ):
        retry = getattr(error, "retry_after_seconds", DEFAULT_RETRY_AFTER_SECONDS)
        valid = type(retry) is int and 1 <= retry <= MAX_RETRY_AFTER_SECONDS
        return ApiError(
            ApiErrorCode.TEMPORARILY_UNAVAILABLE,
            retry_after_seconds=retry if valid else DEFAULT_RETRY_AFTER_SECONDS,
        )
    if isinstance(error, TimeoutError):
        return ApiError(ApiErrorCode.TEMPORARILY_UNAVAILABLE)
    if isinstance(error, ResourceNotFound):
        # Fuera de alcance o sin la clave: igual que inexistente, nunca ``forbidden`` (BR-NUC-09).
        return ApiError(ApiErrorCode.NOT_FOUND)
    if isinstance(error, ContextUnavailable):
        if error.reason is ContextUnavailableReason.CONCESSION_INVALID:
            # Concesión inexistente, ajena, vencida o revocada: como una organización inexistente.
            return ApiError(ApiErrorCode.NOT_FOUND)
        return ApiError(ApiErrorCode.UNAUTHENTICATED)
    if isinstance(error, ContextAbsent):
        # Operación de datos sin contexto: se deniega sin decir por qué; el intento ya lo auditó
        # identity.authz (context_absent_attempt y security_alert).
        _log.error("operación de datos sin contexto denegada")
    return ApiError(ApiErrorCode.INTERNAL_ERROR)


# --- Respuesta ---------------------------------------------------------------------------------


class ApiErrorBody(BaseModel):
    """Cuerpo JSON de toda respuesta de error (domain-entities §4.4, pendiente nº 33)."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    code: ApiErrorCode
    detail_code: str | None = Field(
        default=None, max_length=MAX_DETAIL_CODE_CHARS, pattern=_DETAIL_CODE.pattern
    )
    message_es: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    correlation_id: uuid.UUID
    retry_after_seconds: int | None = Field(default=None, ge=1, le=MAX_RETRY_AFTER_SECONDS)


class ErrorCatalog:
    """Mensajes de ``api_error_code`` y lista de ``detail_code`` de una aplicación."""

    def __init__(self, labels: PlatformLabels, detail_codes: DetailCodeRegistry) -> None:
        missing = [c.value for c in ApiErrorCode if not labels.has("api_error_code", c.value)]
        too_long = [
            c.value
            for c in ApiErrorCode
            if c.value not in missing
            and len(labels.label("api_error_code", c.value)) > MAX_MESSAGE_CHARS
        ]
        problems = [f"el código de error «{code}» no tiene mensaje en español" for code in missing]
        problems += [
            f"el mensaje del código «{code}» supera {MAX_MESSAGE_CHARS} caracteres"
            for code in too_long
        ]
        if problems:
            raise ApiStartupError(problems)
        self._labels = labels
        self._detail_codes = detail_codes

    @property
    def detail_codes(self) -> DetailCodeRegistry:
        return self._detail_codes

    def body(self, error: ApiError, correlation_id: uuid.UUID) -> ApiErrorBody:
        """Cuerpo de ``error``; un ``detail_code`` sin registrar pasa a ``internal_error``."""
        if error.detail_code is not None and error.detail_code not in self._detail_codes:
            _log.error("detail_code sin registrar: se responde internal_error")
            error = ApiError(ApiErrorCode.INTERNAL_ERROR)
        return ApiErrorBody(
            code=error.code,
            detail_code=error.detail_code,
            message_es=self._labels.label("api_error_code", error.code.value),
            correlation_id=correlation_id,
            retry_after_seconds=error.retry_after_seconds,
        )

    def response(self, error: ApiError, correlation_id: uuid.UUID) -> Response:
        body = self.body(error, correlation_id)
        headers = {"Cache-Control": "no-store"}
        if body.retry_after_seconds is not None:
            headers["Retry-After"] = str(body.retry_after_seconds)
        return JSONResponse(
            body.model_dump(mode="json", exclude_none=True),
            status_code=HTTP_STATUS[body.code],
            headers=headers,
        )


# --- correlation_id y manejador global ---------------------------------------------------------


def correlation_id_of(
    scope: MutableMapping[str, Any], clock: Clock, random_bytes: Callable[[int], bytes] = os.urandom
) -> uuid.UUID:
    """El ``correlation_id`` de la petición; lo genera la plataforma si aún no existe.

    Nunca se acepta del cliente (BR-NUC-96): se guarda en el estado de la petición, no se lee de
    una cabecera. La cadena de middleware (TASK-134) lo genera como primer paso.
    """
    state = scope.setdefault("state", {})
    current = state.get(_CORRELATION_STATE_KEY)
    if isinstance(current, uuid.UUID) and current.version == 7:
        return current
    generated = uuid7(clock, random_bytes)
    state[_CORRELATION_STATE_KEY] = generated
    return generated


type _Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
type _Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
type _Asgi = Callable[[MutableMapping[str, Any], _Receive, _Send], Awaitable[None]]


class ErrorBoundary:
    """Middleware ASGI del manejador global: cualquier excepción sin responder es un ``ApiError``.

    Si la respuesta ya empezó, no se puede cambiar: la conexión se corta (la excepción sube a
    uvicorn) y queda en el registro. Nunca sale una traza ni el mensaje de la excepción.
    """

    def __init__(self, app: _Asgi, *, catalog: ErrorCatalog, clock: Clock) -> None:
        self._app = app
        self._catalog = catalog
        self._clock = clock

    async def __call__(
        self, scope: MutableMapping[str, Any], receive: _Receive, send: _Send
    ) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        correlation_id = correlation_id_of(scope, self._clock)
        started = False

        async def tracked_send(message: MutableMapping[str, Any]) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self._app(scope, receive, tracked_send)
        except Exception as error:
            api_error = translate(error)
            if api_error.code is ApiErrorCode.INTERNAL_ERROR:
                _log.exception(
                    "excepción no controlada en una petición", correlation_id=correlation_id
                )
            if started:
                raise
            response = self._catalog.response(api_error, correlation_id)
            await response(scope, receive, send)


def install_error_handlers(app: Any, catalog: ErrorCatalog, clock: Clock) -> None:
    """Respuestas ``ApiError`` para lo que FastAPI y Starlette responderían por su cuenta.

    ``HTTPException`` (404, 405…), la validación de FastAPI y ``ApiError`` se responden aquí;
    todo lo demás llega a ``ErrorBoundary``.
    """

    async def handle(request: Request, error: Exception) -> Response:
        api_error = translate(error)
        return catalog.response(api_error, correlation_id_of(request.scope, clock))

    app.add_exception_handler(ApiError, handle)
    app.add_exception_handler(StarletteHTTPException, handle)
    app.add_exception_handler(RequestValidationError, handle)
