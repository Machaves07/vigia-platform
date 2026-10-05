"""Traducción **única** de cualquier fallo a ``rejection_code`` y su estado HTTP (TASK-206; A-37).

Una petición de nodo nunca recibe un ``ApiError``, un nombre de dominio de U-03 ni el mensaje de
una excepción: todo fallo pasa por ``translate`` y sale como ``RejectionResponse`` del contrato
(``code``, ``retryable`` según la clase del código, ``message_es`` genérico de ``MESSAGES`` y, si
procede, ``field``, ``retry_after_seconds`` y ``compatibility_result``), o como ``404`` sin cuerpo
cuando no hay ruta del contrato (``NotFound``; como ``GET conformance-profile`` en
``ingest.yaml``: «si trae cuerpo, es ``RejectionResponse``, y el cliente no lo interpreta»).

**Qué se traduce** (``translate``):

- ``NodeRejection``: el rechazo de la verificación previa o de una ruta de negocio, tal cual.
- ``NodeContextRejected`` (``identity.authz``): ``node_not_enrolled``, ``node_revoked`` o
  ``node_zone_mismatch``.
- ``ContractValidationError`` (U-01): ``schema_invalid`` con ``field`` = ruta JSON.
- ``LedgerRejected`` (un ``LedgerRejection`` del escritor): por ``to_contract_rejection`` del
  escritor (``idempotency_conflict``, ``clip_*``, ``schema_invalid``; ``chain_locked_timeout`` →
  ``temporarily_unavailable``). ``context_absent`` es un error interno.
- La cadena compartida: ``ApiError`` (``rate_limited``, ``payload_too_large``,
  ``invalid_request`` → ``schema_invalid`` 400, ``temporarily_unavailable``,
  ``storage_unavailable``, ``not_found``), ``HTTPException`` de Starlette (404 y 405 → sin ruta),
  el mamparo (``BulkheadSaturated``) y las dependencias caídas (``StorageUnavailable`` →
  ``storage_unavailable``; base, secretos, servicios externos y tiempos de espera →
  ``temporarily_unavailable``).
- Las excepciones de dominio de U-03 que una ruta de negocio deje escapar: ``DOMAIN_REJECTIONS``
  (vacía hasta que TASK-219 y siguientes registren las suyas); cualquier otra es un error interno
  y sale como ``temporarily_unavailable`` (fallo cerrado y reintentable, nunca aceptación).

**Estado** (``status_of``): el que ``Operation.rejection_statuses`` de U-01 da para la operación
(A-37, restringido a lo que su regla permite); ``schema_invalid`` es 400 si el cuerpo no se
interpreta o una cabecera es inválida (``body_level``) y 422 si incumple el esquema. Un código que
la operación no declara: ``payload_too_large`` en una operación sin cuerpo (confirmación y
catálogo, TASK-206) sale con el 413 de A-37; cualquier otro es un defecto y se responde
``temporarily_unavailable`` (que toda operación declara), con registro de error.

``rejected_newer`` nunca es un ``code`` (es ``contract_version_unsupported`` con
``compatibility_result = rejected_newer``; nota T-05 de BLM §4.2) y ``timestamp_out_of_window`` es
permanente: ``RejectionResponse`` lo impone con su esquema y ``body_for`` lo valida.
``retry_after_seconds`` de un transitorio va de 1 a 60 (``contract_retry_after``, NFR-GOB-33).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, Response
from vigia_contracts.models.api import ContractValidationError
from vigia_contracts.models.enumerations import RejectionCode, RejectionCompatibilityResult
from vigia_contracts.models.rejection_response import RejectionResponse
from vigia_contracts.versioning import CONTRACT_VERSION_HEADER

from vigia_platform.catalog.application.regression import RegressionWriteFailed
from vigia_platform.fleet.application.common import FleetWriteFailed
from vigia_platform.fleet.application.heartbeat import HeartbeatUnavailable, NodeScopeMismatch
from vigia_platform.fleet.application.zone_catalog_for_node import CatalogNotPublished
from vigia_platform.identity.authz.context import NodeContextRejected
from vigia_platform.ledger.application.writer import LedgerRejection, to_contract_rejection
from vigia_platform.node_api.declarations import spec_of
from vigia_platform.shared.api.declarations import NodeRoute
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode, ExternalDependencyDown
from vigia_platform.shared.api.request_state import request_state
from vigia_platform.shared.bulkheads import BulkheadSaturated
from vigia_platform.shared.db import TransientDatabaseError
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.ratelimit import contract_retry_after
from vigia_platform.shared.secrets import SecretsUnavailable
from vigia_platform.shared.storage import StorageUnavailable

__all__ = [
    "A37_STATUS",
    "DEFAULT_RETRY_AFTER_SECONDS",
    "DOMAIN_REJECTIONS",
    "MESSAGES",
    "TRANSIENT_CODES",
    "LedgerRejected",
    "NodeRejection",
    "NotFound",
    "Rendered",
    "body_for",
    "render",
    "route_of",
    "status_of",
    "translate",
]

DEFAULT_RETRY_AFTER_SECONDS: Final = 5
"""``retry_after_seconds`` de un transitorio sin valor propio ``[objetivo propio]`` (el de U-02)."""

TRANSIENT_CODES: Final = frozenset(
    {
        RejectionCode.TEMPORARILY_UNAVAILABLE,
        RejectionCode.RATE_LIMITED,
        RejectionCode.STORAGE_UNAVAILABLE,
    }
)
"""Los transitorios de BR-CTR-30 (``retryable = true``); todos los demás son permanentes."""

A37_STATUS: Final[Mapping[RejectionCode, int]] = MappingProxyType(
    {
        RejectionCode.CONTRACT_VERSION_UNSUPPORTED: 400,
        RejectionCode.CONTRACT_VERSION_RETIRED: 400,
        RejectionCode.SCHEMA_INVALID: 422,
        RejectionCode.NODE_NOT_ENROLLED: 401,
        RejectionCode.NODE_REVOKED: 401,
        RejectionCode.ENROLLMENT_CODE_INVALID: 401,
        RejectionCode.ENROLLMENT_CODE_EXPIRED: 401,
        RejectionCode.ENROLLMENT_CODE_USED: 401,
        RejectionCode.NODE_ZONE_MISMATCH: 403,
        RejectionCode.ZONE_GATE_NOT_APPROVED: 403,
        RejectionCode.IDEMPOTENCY_CONFLICT: 409,
        RejectionCode.PAYLOAD_TOO_LARGE: 413,
        RejectionCode.TIMESTAMP_OUT_OF_WINDOW: 422,
        RejectionCode.CLIP_MISSING: 422,
        RejectionCode.CLIP_HASH_MISMATCH: 422,
        RejectionCode.CLIP_TOO_LARGE: 422,
        RejectionCode.CLIP_NOT_ANONYMIZED: 422,
        RejectionCode.RATE_LIMITED: 429,
        RejectionCode.TEMPORARILY_UNAVAILABLE: 503,
        RejectionCode.STORAGE_UNAVAILABLE: 503,
    }
)
"""La tabla de la adenda A-37 (``schema_invalid`` de cabecera o cuerpo ilegible: 400). Solo se usa
sin operación (petición sin ruta) o para el ``payload_too_large`` de una operación sin cuerpo.
``signature_invalid`` no está: la plataforma nunca lo devuelve."""

MESSAGES: Final[Mapping[RejectionCode, str]] = MappingProxyType(
    {
        RejectionCode.SCHEMA_INVALID: "La petición no cumple el esquema del contrato.",
        RejectionCode.PAYLOAD_TOO_LARGE: "El cuerpo supera el tamaño permitido para la operación.",
        RejectionCode.CONTRACT_VERSION_UNSUPPORTED: (
            "La versión del contrato de la petición no se admite."
        ),
        RejectionCode.CONTRACT_VERSION_RETIRED: "La versión del contrato de la petición se retiró.",
        RejectionCode.NODE_NOT_ENROLLED: "El certificado no corresponde a un nodo dado de alta.",
        RejectionCode.NODE_REVOKED: "La credencial del nodo no está vigente.",
        RejectionCode.NODE_ZONE_MISMATCH: "La petición está fuera del alcance del nodo.",
        RejectionCode.ZONE_GATE_NOT_APPROVED: "La zona no tiene aprobado el uso en ese instante.",
        RejectionCode.CLIP_MISSING: "Un clip referenciado no está en el almacén.",
        RejectionCode.CLIP_HASH_MISMATCH: "El tamaño o el SHA-256 de un clip no coinciden.",
        RejectionCode.CLIP_TOO_LARGE: "Un clip supera el tamaño máximo.",
        RejectionCode.CLIP_NOT_ANONYMIZED: "Un clip no lleva la marca de anonimización.",
        RejectionCode.ENROLLMENT_CODE_INVALID: "El código de alta no es válido.",
        RejectionCode.ENROLLMENT_CODE_EXPIRED: "El código de alta venció.",
        RejectionCode.ENROLLMENT_CODE_USED: "El código de alta ya se usó.",
        RejectionCode.SIGNATURE_INVALID: "La firma no se pudo verificar.",
        RejectionCode.IDEMPOTENCY_CONFLICT: (
            "Ya existe un registro con el mismo identificador y distinto contenido."
        ),
        RejectionCode.TIMESTAMP_OUT_OF_WINDOW: (
            "El registro es más antiguo que la antigüedad máxima aceptada."
        ),
        RejectionCode.TEMPORARILY_UNAVAILABLE: (
            "La plataforma no está disponible temporalmente; reintente más tarde."
        ),
        RejectionCode.RATE_LIMITED: "Se superó el límite de peticiones; reintente más tarde.",
        RejectionCode.STORAGE_UNAVAILABLE: (
            "El almacenamiento no está disponible; nada se aceptó y puede reintentar."
        ),
    }
)
"""``message_es`` genérico por código: nombra la regla, nunca contenido, rutas ni trazas."""

DOMAIN_REJECTIONS: Final[Mapping[type[BaseException], RejectionCode]] = MappingProxyType(
    {
        # TASK-223 (latido y catálogo por zona): organización, planta, nodo o zona fuera del
        # alcance del certificado; respuesta que aún no se puede componer (sin catálogo ni sobre
        # de compuertas, sin claves); registro del expediente rechazado (se revierte entera).
        NodeScopeMismatch: RejectionCode.NODE_ZONE_MISMATCH,
        HeartbeatUnavailable: RejectionCode.TEMPORARILY_UNAVAILABLE,
        CatalogNotPublished: RejectionCode.TEMPORARILY_UNAVAILABLE,
        FleetWriteFailed: RejectionCode.TEMPORARILY_UNAVAILABLE,
        RegressionWriteFailed: RejectionCode.TEMPORARILY_UNAVAILABLE,
    }
)
"""Excepciones de dominio de U-03 que una ruta de negocio puede dejar escapar, con su código.

La amplían las tareas de las rutas (TASK-219, 221, 222, 223, 226) **aquí**, en el único lugar de
la traducción; el nombre de la excepción nunca llega al nodo."""

_FIELD: Final = re.compile(r"[A-Za-z0-9_.\[\]-]{1,256}")
_SEGMENT: Final = re.compile(r"[A-Za-z0-9_-]+|\[[0-9]+\]")

_log = get_logger("node_api.rejections")


class NodeRejection(Exception):
    """Un rechazo del contrato: el primer fallo de la verificación o de la operación.

    ``body_level`` distingue el ``schema_invalid`` de un cuerpo no interpretable o de una cabecera
    inválida (``400``) del de un cuerpo que incumple el esquema (``422``).
    """

    def __init__(
        self,
        code: RejectionCode,
        *,
        field: str | None = None,
        body_level: bool = False,
        message_es: str | None = None,
        compatibility_result: RejectionCompatibilityResult | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        code = RejectionCode(code)
        super().__init__(code.value)
        self.code = code
        self.field = field
        self.body_level = body_level
        self.message_es = message_es
        self.compatibility_result = compatibility_result
        self.retry_after_seconds = retry_after_seconds

    @property
    def retryable(self) -> bool:
        return self.code in TRANSIENT_CODES

    def __repr__(self) -> str:
        return f"NodeRejection({self.code.value!r}, field={self.field!r})"


class NotFound(Exception):
    """Ninguna ruta del contrato atiende la petición: ``404`` sin cuerpo."""


class LedgerRejected(Exception):
    """Un ``LedgerRejection`` del escritor que la ruta de negocio no supo resolver."""

    def __init__(self, rejection: LedgerRejection) -> None:
        if not isinstance(rejection, LedgerRejection):
            raise TypeError("rejection debe ser LedgerRejection")
        super().__init__(rejection.code.value)
        self.rejection = rejection


def _contract_field(field: str | None) -> str | None:
    """``field`` con el patrón del contrato: la ruta, o su prefijo válido más largo."""
    if field is None or _FIELD.fullmatch(field):
        return field
    kept = ""
    for part in re.split(r"(?=\[)|\.", field):
        if not _SEGMENT.fullmatch(part):
            break
        candidate = kept + part if part.startswith("[") or not kept else f"{kept}.{part}"
        if len(candidate) > 256:
            break
        kept = candidate
    return kept or None


def _transient(code: RejectionCode, retry_after: object = None) -> NodeRejection:
    seconds = retry_after if type(retry_after) is int else DEFAULT_RETRY_AFTER_SECONDS
    return NodeRejection(code, retry_after_seconds=contract_retry_after(seconds))


def _from_ledger(rejection: LedgerRejection) -> NodeRejection:
    try:
        document = to_contract_rejection(rejection)
    except ValueError:
        _log.error("rechazo del expediente sin código del contrato: error interno")
        return _transient(RejectionCode.TEMPORARILY_UNAVAILABLE)
    code = RejectionCode(document.code)
    if code in TRANSIENT_CODES:
        return _transient(code, document.retry_after_seconds)
    return NodeRejection(code, field=document.field)


_API_ERRORS: Final[Mapping[ApiErrorCode, RejectionCode]] = MappingProxyType(
    {
        ApiErrorCode.RATE_LIMITED: RejectionCode.RATE_LIMITED,
        ApiErrorCode.PAYLOAD_TOO_LARGE: RejectionCode.PAYLOAD_TOO_LARGE,
        ApiErrorCode.INVALID_REQUEST: RejectionCode.SCHEMA_INVALID,
        ApiErrorCode.TEMPORARILY_UNAVAILABLE: RejectionCode.TEMPORARILY_UNAVAILABLE,
        ApiErrorCode.STORAGE_UNAVAILABLE: RejectionCode.STORAGE_UNAVAILABLE,
    }
)
"""Los ``ApiError`` de la cadena compartida con traducción propia; los demás son internos."""


def _from_api_error(error: ApiError) -> NodeRejection | None:
    if error.code is ApiErrorCode.NOT_FOUND:
        return None
    code = _API_ERRORS.get(error.code)
    if code is None:
        if error.code is ApiErrorCode.INTERNAL_ERROR:
            _log.error("error interno en una petición de nodo")
        return _transient(RejectionCode.TEMPORARILY_UNAVAILABLE)
    if code in TRANSIENT_CODES:
        return _transient(code, error.retry_after_seconds)
    return NodeRejection(code, body_level=code is RejectionCode.SCHEMA_INVALID)


def translate(error: BaseException) -> NodeRejection | None:
    """El rechazo del contrato de ``error``; ``None`` si la respuesta es ``404`` sin cuerpo."""
    if isinstance(error, NodeRejection):
        return error
    if isinstance(error, NotFound):
        return None
    if isinstance(error, NodeContextRejected):
        return NodeRejection(RejectionCode(error.reason.value))
    if isinstance(error, ContractValidationError):
        # Sin ``field`` el fallo es del cuerpo entero (JSON inválido, raíz de otro tipo): 400.
        return NodeRejection(
            RejectionCode.SCHEMA_INVALID,
            field=_contract_field(error.field),
            body_level=error.field is None,
        )
    if isinstance(error, LedgerRejected):
        return _from_ledger(error.rejection)
    if isinstance(error, ApiError):
        return _from_api_error(error)
    if isinstance(error, StarletteHTTPException):
        if error.status_code in (404, 405):
            return None
        if error.status_code == 413:
            return NodeRejection(RejectionCode.PAYLOAD_TOO_LARGE)
        return _transient(RejectionCode.TEMPORARILY_UNAVAILABLE)
    if isinstance(error, RequestValidationError):
        return NodeRejection(RejectionCode.SCHEMA_INVALID, body_level=True)
    if isinstance(error, StorageUnavailable):
        return _transient(RejectionCode.STORAGE_UNAVAILABLE)
    if isinstance(error, BulkheadSaturated):
        return _transient(RejectionCode.TEMPORARILY_UNAVAILABLE, error.retry_after_seconds)
    if isinstance(
        error, TransientDatabaseError | SecretsUnavailable | ExternalDependencyDown | TimeoutError
    ):
        return _transient(
            RejectionCode.TEMPORARILY_UNAVAILABLE, getattr(error, "retry_after_seconds", None)
        )
    for kind, code in DOMAIN_REJECTIONS.items():
        if isinstance(error, kind):
            return _transient(code) if code in TRANSIENT_CODES else NodeRejection(code)
    _log.exception("excepción no controlada en una petición de nodo")
    return _transient(RejectionCode.TEMPORARILY_UNAVAILABLE)


@dataclass(frozen=True, slots=True)
class _Status:
    status: int
    rejection: NodeRejection


def _resolve(route: NodeRoute | None, rejection: NodeRejection) -> _Status:
    code = rejection.code
    if route is None:
        status = A37_STATUS.get(code)
        if status is None:
            return _resolve(None, _transient(RejectionCode.TEMPORARILY_UNAVAILABLE))
        if code is RejectionCode.SCHEMA_INVALID and rejection.body_level:
            status = 400
        return _Status(status, rejection)
    operation = spec_of(route).operation
    try:
        statuses = operation.rejection_statuses(code.value)
    except ValueError:
        if code is RejectionCode.PAYLOAD_TOO_LARGE and route.max_body_bytes == 0:
            return _Status(A37_STATUS[code], rejection)
        _log.error("la operación no admite el código de rechazo: se responde transitorio")
        return _resolve(route, _transient(RejectionCode.TEMPORARILY_UNAVAILABLE))
    if code is RejectionCode.SCHEMA_INVALID and len(statuses) > 1:
        return _Status(400 if rejection.body_level else 422, rejection)
    return _Status(statuses[0], rejection)


def status_of(route: NodeRoute | None, rejection: NodeRejection) -> int:
    """El estado HTTP de ``rejection`` en la operación de ``route`` (A-37)."""
    return _resolve(route, rejection).status


def body_for(rejection: NodeRejection) -> RejectionResponse:
    """El ``RejectionResponse`` de ``rejection``, validado con el modelo estricto de U-01."""
    optional: dict[str, Any] = {}
    field = _contract_field(rejection.field)
    if field is not None:
        optional["field"] = field
    if rejection.retryable:
        optional["retry_after_seconds"] = contract_retry_after(
            rejection.retry_after_seconds or DEFAULT_RETRY_AFTER_SECONDS
        )
    if rejection.compatibility_result is not None:
        optional["compatibility_result"] = RejectionCompatibilityResult(
            rejection.compatibility_result
        )
    message = rejection.message_es or MESSAGES[rejection.code]
    return RejectionResponse(
        code=rejection.code, retryable=rejection.retryable, message_es=message, **optional
    )


@dataclass(frozen=True, slots=True)
class Rendered:
    """La respuesta a un nodo y lo que la observabilidad necesita de ella."""

    response: Response
    status: int
    rejection: NodeRejection | None
    """``None``: ``404`` sin cuerpo (no hay ruta del contrato)."""


def render(route: NodeRoute | None, error: BaseException, contract_version: str) -> Rendered:
    """La respuesta del contrato a ``error`` en la operación de ``route``."""
    rejection = translate(error)
    headers = {"Cache-Control": "no-store", CONTRACT_VERSION_HEADER: contract_version}
    if rejection is None:
        return Rendered(Response(status_code=404, headers=headers), 404, None)
    resolved = _resolve(route, rejection)
    body = body_for(resolved.rejection)
    if body.retry_after_seconds is not None:
        headers["Retry-After"] = str(body.retry_after_seconds)
    response = JSONResponse(body.to_json_value(), status_code=resolved.status, headers=headers)
    return Rendered(response, resolved.status, resolved.rejection)


def route_of(scope: MutableMapping[str, Any]) -> NodeRoute | None:
    """La ``NodeRoute`` que la cadena resolvió para la petición, si alguna."""
    route = request_state(scope).route
    if route is None:
        return None
    for declaration in route.declarations:
        if declaration.node is not None:
            return declaration.node
    return None
