"""Servicios de las rutas de ``ledger`` y utilidades comunes de sus manejadores (TASK-137).

``LedgerHttp`` lo construye la raíz de composición y lo entrega en ``AppRuntime.ledger``; la
fábrica lo deja en ``app.state``. Sin él, ``internal_error`` (fallo cerrado), como ``identity``.

- **Contexto reducido** (``narrowed_context``): los puertos de lectura que filtran por
  ``allowed_scopes`` sin mirar el rol (``LectorExpediente``) reciben el contexto reducido a las
  asignaciones que conceden la clave de la ruta (``identity.authz.authorize.narrowed``). Sin
  ninguna, se deniega como ``authorize`` (``authorization_denied`` auditado y ``not_found``).
- **``provider_query``** (BR-NUC-38 y 41): no lo escriben estas rutas, sino ``ContextAuthorizer``
  (VIG-132) al conceder la clave de la ruta bajo concesión, antes de que corra y con fallo cerrado;
  así hay uno solo por petición.

Toda respuesta de estas rutas lleva ``Cache-Control: no-store``: son datos del expediente, de la
auditoría o URL y tokens de corta vida.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol

from fastapi import Request, Response

from vigia_platform.identity.authz.authorize import Authorizer, Resource, narrowed
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.coverage import CoveragePort
from vigia_platform.ledger.application.evidence_read import EvidencePort
from vigia_platform.ledger.application.integrity_requests import VerificationRequest
from vigia_platform.ledger.application.labels import LabelPort
from vigia_platform.ledger.application.reader import LectorExpediente
from vigia_platform.ledger.chain.checkpoints import CheckpointChain, StoredCheckpoint
from vigia_platform.ledger.chain.verify import IntegrityResult
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.tokens import IssuedLiveViewToken

__all__ = [
    "LEDGER_STATE_KEY",
    "TIMESTAMP_PATTERN",
    "CheckpointReader",
    "IntegrityResultsReader",
    "IntegrityVerificationRequests",
    "LedgerHttp",
    "LiveViewIssuer",
    "cursor_stamp",
    "exact_query",
    "ledger_http",
    "narrowed_context",
    "no_store",
    "parse_instant",
    "request_context",
]

LEDGER_STATE_KEY: Final = "vigia_ledger_http"

_log = get_logger("ledger.http")


class CheckpointReader(Protocol):
    """``CheckpointPort.latest_checkpoints`` (``CheckpointService``)."""

    async def latest_checkpoints(
        self, context: ScopeContext, chains: Sequence[CheckpointChain] | None = None
    ) -> tuple[StoredCheckpoint, ...]: ...


class IntegrityResultsReader(Protocol):
    """``IntegrityPort.last_results`` (``SqlIntegrityResults`` en la API)."""

    async def last_results(self, context: ScopeContext) -> tuple[IntegrityResult, ...]: ...


class IntegrityVerificationRequests(Protocol):
    """``IntegrityRequests.request``: publica la verificación a demanda para el worker."""

    async def request(
        self, context: ScopeContext, chain: CheckpointChain
    ) -> VerificationRequest: ...


class LiveViewIssuer(Protocol):
    """``LiveViewTokenPort.emitir_token_vista`` (``LiveViewTokenService.issue``)."""

    async def issue(self, context: ScopeContext, zone_id: uuid.UUID) -> IssuedLiveViewToken: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class LedgerHttp:
    """Los puertos que usan las rutas de ``ledger``."""

    reader: LectorExpediente
    evidence: EvidencePort
    labels: LabelPort
    coverage: CoveragePort
    integrity_results: IntegrityResultsReader
    integrity_requests: IntegrityVerificationRequests
    checkpoints: CheckpointReader
    live_view: LiveViewIssuer
    authorizer: Authorizer
    provider_organization_id: uuid.UUID

    def __post_init__(self) -> None:
        if type(self.provider_organization_id) is not uuid.UUID:
            raise TypeError("provider_organization_id debe ser uuid.UUID")


def ledger_http(request: Request) -> LedgerHttp:
    """Los servicios de la aplicación; sin ellos, ``internal_error`` (nunca deja pasar)."""
    services = getattr(request.app.state, LEDGER_STATE_KEY, None)
    if not isinstance(services, LedgerHttp):
        _log.error("las rutas del expediente no tienen servicios instalados")
        raise ApiError(ApiErrorCode.INTERNAL_ERROR)
    return services


def no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


async def narrowed_context(
    services: LedgerHttp, context: ScopeContext, key: PermissionKey
) -> ScopeContext:
    """``context`` reducido a las asignaciones con ``key``; sin ninguna, se deniega auditado.

    La autorización por ruta ya comprobó que alguna asignación tiene una clave de la ruta; aquí
    se deniega, como ``authorize``, si la clave concreta no la tiene ninguna.
    """
    reduced = narrowed(context, key, provider_organization_id=services.provider_organization_id)
    if reduced is None:
        # ``authorize`` sobre la organización no concede (no hay asignación con la clave): audita
        # ``authorization_denied`` y lanza ``ResourceNotFound`` (``not_found``).
        await services.authorizer.authorize(
            context, key, Resource.organization(context.organization_id)
        )
        raise ApiError(ApiErrorCode.NOT_FOUND)  # pragma: no cover - authorize ya lanzó
    return reduced


TIMESTAMP_PATTERN: Final = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(\.[0-9]{1,6})?(Z|[+-][0-9]{2}:[0-9]{2})$"
)
"""Marca ISO 8601 con zona horaria (``Z`` o desfase): sin zona, sin fecha sola, sin número."""
_TIMESTAMP: Final = re.compile(TIMESTAMP_PATTERN)


def parse_instant(value: str, *, milliseconds: bool = False) -> datetime:
    """La marca de un parámetro, en UTC; ``invalid_request`` si no tiene la forma.

    Con ``milliseconds``, una marca con precisión por debajo del milisegundo también es inválida
    (la línea de tiempo trabaja en milisegundos exactos).
    """
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise ApiError(ApiErrorCode.INVALID_REQUEST)
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
    if moment.utcoffset() is None:  # pragma: no cover - el patrón exige la zona
        raise ApiError(ApiErrorCode.INVALID_REQUEST)
    if milliseconds and moment.microsecond % 1000:
        raise ApiError(ApiErrorCode.INVALID_REQUEST)
    try:
        return moment.astimezone(UTC)
    except OverflowError:
        raise ApiError(ApiErrorCode.INVALID_REQUEST) from None


def cursor_stamp(moment: datetime) -> str:
    """Marca de una clave de paginación, en UTC con **microsegundos** y ``Z``.

    No se trunca al milisegundo (como las marcas de los datos): la clave tiene que volver
    exactamente igual para que la página siguiente no repita ni omita nada.
    """
    return moment.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="microseconds") + "Z"


def exact_query(*allowed: str, repeatable: Sequence[str] = ()) -> Callable[[Request], None]:
    """Dependencia: solo los parámetros ``allowed`` y, salvo los ``repeatable``, una vez cada uno.

    FastAPI ignora un parámetro desconocido y toma uno de los repetidos; la validación por esquema
    de la plataforma (BR-NUC-92) los rechaza con ``invalid_request``.
    """
    known = frozenset(allowed) | frozenset(repeatable)
    many = frozenset(repeatable)

    def check(request: Request) -> None:
        params = request.query_params
        for name in params:
            if name not in known:
                raise ApiError(ApiErrorCode.INVALID_REQUEST)
            if name not in many and len(params.getlist(name)) > 1:
                raise ApiError(ApiErrorCode.INVALID_REQUEST)

    return check
