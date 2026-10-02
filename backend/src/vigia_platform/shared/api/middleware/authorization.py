"""Autorización por ruta (paso 10) y auditoría de la barrera anti-falsificación (paso 8).

``ContextAuthorizer`` es el ``Authorizer`` de ``vigia-api`` (la raíz de composición lo instala en
``AppRuntime.authorizer``): la declaración ``requires(clave)`` de cada ruta lo llama tras los diez
eslabones. Sin contexto de sesión en la petición, ``unauthenticated``. Con contexto, concede si
alguna asignación que cuenta en ese contexto tiene la clave (``identity.authz.route_role``); si no,
audita ``authorization_denied`` sobre la organización del contexto y responde ``not_found``
(BR-NUC-09: igual que una ruta o un recurso inexistente, nunca ``forbidden``).

**La denegación no depende de la base** (seguimiento de VIG-73): si la auditoría de la denegación
falla (base caída, tiempo agotado), la respuesta sigue siendo ``not_found`` y el fallo queda en el
registro. Así, ante una base caída, la respuesta no distingue «sin permiso» de «no existe».

**``provider_query`` bajo concesión** (BR-NUC-38 y 41, PR-NUC-11; seguimiento de VIG-83): cuando
concede una ruta con clave (``requires``) a un contexto bajo concesión, escribe **antes** de que
corra la ruta un ``provider_query`` en la cadena del alcance concedido con la operación (``read``
en los métodos seguros, ``write`` en el resto), el método, la plantilla de la ruta y el momento.
Toda ruta con clave opera sobre datos de la organización del contexto, que bajo concesión es la del
cliente; las rutas de sesión (``authenticated``), las de salud y los estáticos no pasan por aquí y
no escriben ninguno. **Fallo cerrado**: si la escritura falla, la ruta no corre y la respuesta es
el error genérico (``translate``), así ningún acceso del proveedor queda invisible para el cliente
(amenaza N-4). Se escribe antes y no al terminar porque después la respuesta ya salió (o la
escritura de la ruta ya se confirmó) y no habría forma de negarla; una petición que luego la ruta
rechaza (cuerpo inválido, recurso fuera de alcance) queda igualmente registrada como intento.

``AuditCsrfRejections`` implementa ``CsrfAuditPort`` sobre ``AuditWriter``: ``csrf_rejected``
(``outcome = denied``) en la cadena de la organización del contexto o, sin sesión (p. ej. un
``POST /auth/login`` falsificado), en la de la organización **proveedora** (BR-NUC-61). Los
filtros son el motivo, el método, la plantilla de la ruta y el HMAC del origen de red: nunca una
cabecera, una cookie ni una dirección. ``csrf_rejected`` es solo una operación de auditoría, no
un código de error (nota del 2026-09-23 de PAT-NUC-SEG-02).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from pydantic import JsonValue
from starlette.requests import Request

from vigia_platform.identity.authz.authorize import (
    AuthorizationAudit,
    Resource,
    ResourceNotFound,
    route_role,
)
from vigia_platform.identity.authz.context import (
    ProviderQueryLedger,
    SessionScope,
    record_provider_query,
)
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import (
    AuditOperation,
    AuditOutcome,
    AuditWriter,
)
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware.steps import SAFE_METHODS, CsrfRejection
from vigia_platform.shared.api.request_state import request_state
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.observability.logging import get_logger

__all__ = [
    "AuditCsrfRejections",
    "ContextAuthorizer",
    "request_context",
    "request_session",
]

_log = get_logger("shared.api.middleware")


def request_session(request: Request) -> SessionScope | None:
    """La sesión que validó la cadena para esta petición, si la ruta la exige y es válida."""
    return request_state(request.scope).session


def request_context(request: Request) -> ScopeContext:
    """El ``ScopeContext`` de la petición; ``unauthenticated`` si no hay sesión válida."""
    session = request_session(request)
    if session is None:
        raise ApiError(ApiErrorCode.UNAUTHENTICATED)
    return session.context


class ContextAuthorizer:
    """``Authorizer`` por ruta sobre el contexto de la sesión (paso 10 de PAT-NUC-SEG-06)."""

    def __init__(
        self,
        *,
        audit: AuthorizationAudit,
        provider_organization_id: uuid.UUID,
        provider_queries: ProviderQueryLedger,
        clock: Clock,
    ) -> None:
        if type(provider_organization_id) is not uuid.UUID:
            raise TypeError("provider_organization_id debe ser uuid.UUID")
        self._audit = audit
        self._provider_organization_id = provider_organization_id
        self._provider_queries = provider_queries
        self._clock = clock

    async def authorize(self, request: Request, permission: str) -> None:
        context = request_context(request)
        key = PermissionKey(permission)
        role = route_role(context, key, provider_organization_id=self._provider_organization_id)
        if role is not None:
            if context.concession_id is not None:
                await self._record_provider_query(request, context)
            return
        try:
            await self._audit.authorization_denied(
                context, key, Resource.organization(context.organization_id)
            )
        except Exception:
            _log.exception("no se pudo auditar authorization_denied")
        raise ResourceNotFound()

    async def _record_provider_query(self, request: Request, context: ScopeContext) -> None:
        """El ``provider_query`` de la petición; si no se escribe, la ruta no corre."""
        route = request_state(request.scope).route
        if route is None:
            # La declaración ya comprobó que la cadena resolvió esta ruta: no debería ocurrir.
            raise ApiError(ApiErrorCode.INTERNAL_ERROR)
        method = request.method
        try:
            await record_provider_query(
                context,
                self._provider_queries,
                operation="read" if method in SAFE_METHODS else "write",
                # ``HEAD`` lee lo mismo que ``GET``: cuenta como esa lectura.
                method="GET" if method == "HEAD" else method,
                route_template=route.path,
                occurred_at=self._clock.now(),
            )
        except Exception:
            _log.exception("no se pudo escribir provider_query: la petición no se atiende")
            raise


@repository
class AuditCsrfRejections:
    """``CsrfAuditPort`` sobre ``AuditWriter``."""

    def __init__(self, *, audit: AuditWriter, provider_context: Callable[[], ScopeContext]) -> None:
        self._audit = audit
        self._provider_context = provider_context

    async def csrf_rejected(self, context: ScopeContext, rejection: CsrfRejection) -> None:
        """Con sesión: en la cadena de auditoría de la organización del contexto."""
        await self._audit.append(
            context,
            AuditOperation.CSRF_REJECTED,
            outcome=AuditOutcome.DENIED,
            filters=_filters(rejection),
        )

    async def csrf_rejected_without_session(self, rejection: CsrfRejection) -> None:
        """Sin sesión (BR-NUC-61): en la cadena de la organización proveedora."""
        await self._audit.append_without_organization(
            self._provider_context(),
            AuditOperation.CSRF_REJECTED,
            outcome=AuditOutcome.DENIED,
            filters=_filters(rejection),
        )


def _filters(rejection: CsrfRejection) -> dict[str, JsonValue]:
    filters: dict[str, JsonValue] = {
        "reason": rejection.reason.value,
        "method": rejection.method,
        "route": rejection.route,
    }
    if rejection.origin_hash is not None:
        filters["origin_hash"] = rejection.origin_hash
    return filters
