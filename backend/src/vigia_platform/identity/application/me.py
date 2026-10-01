"""Lo que la aplicación sabe de la persona de la sesión (``GET /me``; §10.2; adenda A-15).

``MeService.profile(context, scope)`` lee en **una** sentencia, bajo la seguridad a nivel de fila de
la organización de la sesión, la sesión (sus dos vencimientos, recién prolongada por la cadena), la
cuenta y la organización. Las asignaciones son las del contexto de la petición y los permisos
efectivos los calcula ``identity.authz.route_role`` con las mismas reglas que la autorización por
ruta (claves ``platform.*`` solo en la proveedora y sin concesión; bajo concesión, solo la columna
de ``provider_installer``).

Nunca devuelve el identificador de sesión ni su hash, ni el de la contraseña.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Protocol

from sqlalchemy import text

from vigia_platform.identity.application.common import as_uuid
from vigia_platform.identity.authz.authorize import route_role
from vigia_platform.identity.authz.context import SessionScope
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.context import AllowedScope, ScopeContext, repository

__all__ = ["MeService", "MeView", "OrganizationSummary", "effective_keys"]

_PROFILE: Final = text(
    "SELECT s.idle_expires_at, s.absolute_expires_at, u.email, u.display_name,"
    " o.organization_id, o.code, o.name, o.kind"
    " FROM identity.session AS s"
    " JOIN identity.user_account AS u ON u.user_id = s.user_id"
    " JOIN identity.organization AS o ON o.organization_id = s.organization_id"
    " WHERE s.session_id_hash = :session_id_hash AND s.user_id = :user_id"
    " AND s.status = 'active' AND u.status = 'active' AND o.status = 'active'"
)


@dataclass(frozen=True, slots=True)
class OrganizationSummary:
    organization_id: uuid.UUID
    code: str
    name: str
    kind: str


@dataclass(frozen=True, slots=True)
class MeView:
    user_id: uuid.UUID
    display_name: str = field(repr=False)
    email: str = field(repr=False)
    organization: OrganizationSummary
    assignments: tuple[AllowedScope, ...]
    effective_permissions: tuple[str, ...]
    concession_id: uuid.UUID | None
    idle_expires_at: datetime
    absolute_expires_at: datetime


def effective_keys(context: ScopeContext, provider_organization_id: uuid.UUID) -> tuple[str, ...]:
    """Las claves que ``context`` puede usar en alguna ruta, ordenadas (BR-NUC-13)."""
    return tuple(
        sorted(
            key.value
            for key in PermissionKey
            if route_role(context, key, provider_organization_id=provider_organization_id)
            is not None
        )
    )


class MeContexts(Protocol):
    def anonymous(self, organization_id: uuid.UUID) -> ScopeContext: ...


@repository
class MeService:
    """``GET /me``."""

    def __init__(
        self,
        database: LedgerDatabase,
        *,
        contexts: MeContexts,
        provider_organization_id: uuid.UUID,
    ) -> None:
        if type(provider_organization_id) is not uuid.UUID:
            raise TypeError("provider_organization_id debe ser uuid.UUID")
        self._database = database
        self._contexts = contexts
        self._provider_organization_id = provider_organization_id

    def __repr__(self) -> str:
        return "MeService()"

    async def profile(self, context: ScopeContext, scope: SessionScope) -> MeView | None:
        """La persona de ``scope`` (``context`` es ``scope.context``); ``None`` si la sesión
        dejó de estar activa.

        La fila de la sesión se lee con un contexto de la organización **de la sesión** (bajo
        concesión, la proveedora).
        """
        session_id_hash = context.session_id_hash
        if context is not scope.context or session_id_hash is None:
            return None
        lookup = self._contexts.anonymous(scope.session_organization_id)
        rows = await self._database.read(
            lookup, _PROFILE, {"session_id_hash": session_id_hash, "user_id": scope.user_id}
        )
        if not rows:
            return None
        row = rows[0]
        return MeView(
            user_id=scope.user_id,
            display_name=row.display_name,
            email=row.email,
            organization=OrganizationSummary(
                organization_id=as_uuid(row.organization_id),
                code=row.code,
                name=row.name,
                kind=row.kind,
            ),
            assignments=context.allowed_scopes,
            effective_permissions=effective_keys(context, self._provider_organization_id),
            concession_id=context.concession_id,
            idle_expires_at=row.idle_expires_at,
            absolute_expires_at=row.absolute_expires_at,
        )
