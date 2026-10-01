"""``authorize(context, permission_key, resource)``: denegación por defecto (LC-NUC-04; BR-NUC-09,
12 a 17, 37; ``business-logic-model.md`` §1).

Se concede si **alguna** asignación de ``allowed_scopes`` tiene la clave en la matriz y su alcance
contiene al recurso (organización ⊇ planta ⊇ zona; un recurso de organización, solo con alcance
de organización). Si varias lo conceden, ``role_in_use`` es el rol de la de alcance más
específico (zona > planta > organización) y, a igual alcance, el primero en el orden de la
enumeración ``role`` (BR-NUC-16): el resultado no depende del orden de las asignaciones.

Reglas que no caben en la tabla de §2.6:

- ``platform.*`` solo con contexto de la organización **proveedora** sin concesión;
- ``platform_operator`` y ``provider_installer`` solo conceden en la proveedora; bajo concesión
  (contexto del **cliente** con ``concession_id``) solo cuenta ``provider_installer`` sobre el
  alcance concedido (BR-NUC-04, 37);
- un contexto que no sale de una sesión o de una orden administrativa (evento de la bandeja,
  iteración periódica) no tiene asignaciones: nunca se le concede una clave;
- con ``owner_id`` el recurso es de una persona (p. ej. sus preferencias de notificación): solo
  su dueño puede usarlo, además de tener la clave (verificación de propiedad a nivel de objeto).

``Authorizer.authorize`` devuelve el contexto con ``actor.role_in_use`` fijado (lo que registran
el expediente y la auditoría, BR-NUC-16) o, si no concede, audita ``authorization_denied`` y lanza
``ResourceNotFound``: hacia el llamador es **exactamente** lo mismo que un recurso inexistente
(``not_found``, nunca ``forbidden``; BR-NUC-09).

Módulo puro con un puerto de auditoría: no importa FastAPI ni SQLAlchemy (NFR-NUC-25).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Protocol

from vigia_platform.identity.authz.context import with_role_in_use
from vigia_platform.identity.authz.matrix import (
    MATRIX,
    PermissionKey,
    is_platform_key,
    permission_key,
)
from vigia_platform.shared.context import (
    AllowedScope,
    ContextAbsent,
    Role,
    ScopeContext,
    ScopeLevel,
    repository,
)

__all__ = [
    "AuthorizationAudit",
    "Authorizer",
    "Decision",
    "Resource",
    "ResourceNotFound",
    "decide",
    "role_in_use",
    "route_role",
]

_SPECIFICITY: Final = {ScopeLevel.ZONE: 0, ScopeLevel.PLANT: 1, ScopeLevel.ORGANIZATION: 2}
_ROLE_ORDER: Final = {role: index for index, role in enumerate(Role)}
_PROVIDER_ONLY_ROLES: Final = frozenset({Role.PLATFORM_OPERATOR, Role.PROVIDER_INSTALLER})
_KIND: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")


class ResourceNotFound(Exception):
    """El recurso no existe **o** no está al alcance: el llamador no puede distinguirlos.

    ``shared.api.errors`` lo traduce a ``not_found``. No lleva el motivo ni el recurso.
    """

    code: Final = "not_found"

    def __init__(self) -> None:
        super().__init__("recurso no encontrado")


@dataclass(frozen=True, slots=True)
class Resource:
    """El recurso que una operación toca, con su lugar **real** en la jerarquía.

    ``plant_id`` y ``zone_id`` los aporta quien cargó el recurso: una zona siempre con su planta.
    ``kind`` e ``id`` identifican el recurso en la auditoría (``resource_ref``); ``owner_id``
    restringe el recurso a la persona dueña.
    """

    organization_id: uuid.UUID
    kind: str
    id: uuid.UUID
    plant_id: uuid.UUID | None = None
    zone_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        for name in ("organization_id", "id"):
            if type(getattr(self, name)) is not uuid.UUID:
                raise TypeError(f"{name} debe ser uuid.UUID")
        for name in ("plant_id", "zone_id", "owner_id"):
            value = getattr(self, name)
            if value is not None and type(value) is not uuid.UUID:
                raise TypeError(f"{name} debe ser uuid.UUID")
        if not isinstance(self.kind, str) or _KIND.fullmatch(self.kind) is None:
            raise ValueError("kind debe ser snake_case de 1 a 64 caracteres")
        if self.zone_id is not None and self.plant_id is None:
            raise ValueError("una zona se autoriza con su planta")

    @classmethod
    def organization(cls, organization_id: uuid.UUID) -> Resource:
        """La organización misma (ajustes, usuarios, concesiones, auditoría)."""
        return cls(organization_id, "organization", organization_id)

    @classmethod
    def plant(cls, organization_id: uuid.UUID, plant_id: uuid.UUID) -> Resource:
        return cls(organization_id, "plant", plant_id, plant_id=plant_id)

    @classmethod
    def zone(cls, organization_id: uuid.UUID, plant_id: uuid.UUID, zone_id: uuid.UUID) -> Resource:
        return cls(organization_id, "zone", zone_id, plant_id=plant_id, zone_id=zone_id)


@dataclass(frozen=True, slots=True)
class Decision:
    """Resultado puro de ``decide``: ``role_in_use`` es ``None`` si y solo si se deniega."""

    role_in_use: Role | None

    @property
    def granted(self) -> bool:
        return self.role_in_use is not None


def role_in_use(candidates: Iterable[AllowedScope]) -> Role | None:
    """El rol determinista entre las asignaciones que conceden (BR-NUC-16); ``None`` si ninguna."""
    best: tuple[int, int] | None = None
    chosen: Role | None = None
    for scope in candidates:
        rank = (_SPECIFICITY[scope.scope_level], _ROLE_ORDER[scope.role])
        if best is None or rank < best:
            best, chosen = rank, scope.role
    return chosen


def _counts(
    scope: AllowedScope, context: ScopeContext, provider_organization_id: uuid.UUID
) -> bool:
    """¿Puede esta asignación conceder algo en este contexto?"""
    in_provider = context.organization_id == provider_organization_id
    if context.concession_id is not None:
        # Bajo concesión: exactamente la columna del instalador, sobre el alcance concedido.
        return scope.role is Role.PROVIDER_INSTALLER and not in_provider
    if scope.role in _PROVIDER_ONLY_ROLES:
        return in_provider
    return not in_provider


def decide(
    context: ScopeContext,
    key: PermissionKey | str,
    resource: Resource,
    *,
    provider_organization_id: uuid.UUID,
) -> Decision:
    """La decisión pura de ``authorize`` (sin auditar). ``ValueError`` si la clave no existe."""
    if not isinstance(context, ScopeContext):
        raise ContextAbsent("authorize")
    key = permission_key(key)
    if not isinstance(resource, Resource):
        raise TypeError("resource debe ser Resource")
    denied = Decision(None)
    if resource.organization_id != context.organization_id:
        return denied
    if resource.owner_id is not None and resource.owner_id != context.actor.id:
        return denied
    if is_platform_key(key) and (
        context.organization_id != provider_organization_id or context.concession_id is not None
    ):
        return denied
    candidates = [
        scope
        for scope in context.allowed_scopes
        if key in MATRIX[scope.role]
        and _counts(scope, context, provider_organization_id)
        and scope.covers(resource.organization_id, resource.plant_id, resource.zone_id)
    ]
    return Decision(role_in_use(candidates))


def route_role(
    context: ScopeContext, key: PermissionKey | str, *, provider_organization_id: uuid.UUID
) -> Role | None:
    """El rol con el que ``context`` puede usar una ruta que exige ``key``, o ``None``.

    Es la autorización **por ruta** de la cadena de middleware (paso 10 de PAT-NUC-SEG-06): aún
    no hay recurso, así que basta con que alguna asignación que cuenta en este contexto tenga la
    clave en la matriz, con las mismas reglas de proveedor y de concesión que ``decide``. El
    servicio vuelve a autorizar sobre el recurso real (``Authorizer.authorize``) antes de tocarlo.
    """
    if not isinstance(context, ScopeContext):
        raise ContextAbsent("route_role")
    key = permission_key(key)
    if is_platform_key(key) and (
        context.organization_id != provider_organization_id or context.concession_id is not None
    ):
        return None
    return role_in_use(
        scope
        for scope in context.allowed_scopes
        if key in MATRIX[scope.role] and _counts(scope, context, provider_organization_id)
    )


class AuthorizationAudit(Protocol):
    """Puerto de auditoría de la autorización (adaptador en ``identity.adapters.authz_store``)."""

    async def authorization_denied(
        self, context: ScopeContext, key: PermissionKey, resource: Resource
    ) -> None:
        """Anexa ``authorization_denied`` (``outcome = denied``) a la cadena del contexto."""
        ...


@repository
class Authorizer:
    """``AuthorizationPort.authorize`` (``business-logic-model.md`` §10.1)."""

    def __init__(self, *, audit: AuthorizationAudit, provider_organization_id: uuid.UUID) -> None:
        if type(provider_organization_id) is not uuid.UUID:
            raise TypeError("provider_organization_id debe ser uuid.UUID")
        self._audit = audit
        self._provider_organization_id = provider_organization_id

    async def authorize(
        self, context: ScopeContext, key: PermissionKey | str, resource: Resource
    ) -> ScopeContext:
        """El contexto con ``role_in_use`` si concede; si no, audita y ``ResourceNotFound``."""
        permission = permission_key(key)
        decision = decide(
            context,
            permission,
            resource,
            provider_organization_id=self._provider_organization_id,
        )
        if decision.role_in_use is None:
            await self._audit.authorization_denied(context, permission, resource)
            raise ResourceNotFound()
        return with_role_in_use(context, decision.role_in_use)
