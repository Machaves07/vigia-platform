"""Configuración de la organización (``GET/PATCH /organization/settings``; TASK-136).

``business-logic-model.md`` §10.2 y domain-entities §2.1 y §2.6 (``organization.settings``: «topes
de concesión y datos de la organización»; ``plant_manager`` y ``administrator``).

- ``settings(context)``: ``organization.settings`` sobre la organización; su código, nombre, tipo,
  estado y los topes de concesión ``concession_max_days`` (1 a 90) y ``concession_default_days``
  (1 a ``concession_max_days``).
- ``update(context, change)``: cambia el nombre (política de texto libre, ≤ 120) y los topes. Los
  topes valen para las concesiones **nuevas** (BR-NUC-35); una concesión vigente no cambia. Con la
  fila bloqueada, comprueba que el resultado cumple ``1 <= default <= max <= 90`` (si no,
  ``invalid_value`` sin tocar nada) y audita ``organization_settings_changed`` con los **nombres**
  de los campos cambiados y los topes resultantes, nunca el nombre. Sin cambios no escribe nada.

El código, el tipo y el estado no se cambian aquí: el alta y la suspensión son órdenes
administrativas (BR-NUC-06).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Final

from pydantic import JsonValue
from sqlalchemy import text

from vigia_platform.identity.application.common import (
    IdentityDependencies,
    IdentityRejected,
    IdentityRejection,
    as_uuid,
    checked_free_text,
)
from vigia_platform.identity.authz.authorize import Resource, ResourceNotFound
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import AuditOperation, ResourceRef
from vigia_platform.shared.context import ScopeContext, repository

__all__ = [
    "MAX_CONCESSION_DAYS",
    "OrganizationSettings",
    "OrganizationSettingsService",
    "SettingsChange",
]

MAX_CONCESSION_DAYS: Final = 90
"""Tope de ``Organization.concession_max_days`` (domain-entities §2.1)."""
NAME_MAX: Final = 120

_SETTINGS: Final = text(
    "SELECT organization_id, code, name, kind, status, concession_max_days,"
    " concession_default_days FROM identity.organization"
    " WHERE organization_id = :organization_id"
)
_LOCKED: Final = text(
    "SELECT organization_id, code, name, kind, status, concession_max_days,"
    " concession_default_days FROM identity.organization"
    " WHERE organization_id = :organization_id FOR NO KEY UPDATE"
)
_UPDATE: Final = text(
    "UPDATE identity.organization SET name = :name, concession_max_days = :max_days,"
    " concession_default_days = :default_days WHERE organization_id = :organization_id"
)


@dataclass(frozen=True, slots=True)
class OrganizationSettings:
    organization_id: uuid.UUID
    code: str
    name: str
    kind: str
    status: str
    concession_max_days: int
    concession_default_days: int


@dataclass(frozen=True, slots=True)
class SettingsChange:
    """``PATCH /organization/settings``: ``None`` deja el campo como está."""

    name: str | None = None
    concession_max_days: int | None = None
    concession_default_days: int | None = None


def _days(value: object, path: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= MAX_CONCESSION_DAYS:
        raise IdentityRejected(IdentityRejection.INVALID_VALUE, field=path)
    return value


@repository
class OrganizationSettingsService:
    """``organization.settings`` sobre la organización del contexto."""

    def __init__(self, deps: IdentityDependencies) -> None:
        self._deps = deps

    def __repr__(self) -> str:
        return "OrganizationSettingsService()"

    async def _authorized(self, context: ScopeContext) -> ScopeContext:
        return await self._deps.authorizer.authorize(
            context,
            PermissionKey.ORGANIZATION_SETTINGS,
            Resource.organization(context.organization_id),
        )

    async def settings(self, context: ScopeContext) -> OrganizationSettings:
        authorized = await self._authorized(context)
        rows = await self._deps.database.read(
            authorized, _SETTINGS, {"organization_id": authorized.organization_id}
        )
        if not rows:
            raise ResourceNotFound()
        return _settings(rows[0])

    async def update(self, context: ScopeContext, change: SettingsChange) -> OrganizationSettings:
        deps = self._deps
        if not isinstance(change, SettingsChange):
            raise TypeError("change debe ser SettingsChange")
        name = (
            None
            if change.name is None
            else checked_free_text(
                deps, change.name, entity="organization", path="/name", max_length=NAME_MAX
            )
        )
        max_days = _days(change.concession_max_days, "/concession_max_days")
        default_days = _days(change.concession_default_days, "/concession_default_days")
        authorized = await self._authorized(context)
        async with deps.database.transaction(authorized) as transaction:
            row = (
                await transaction.execute(
                    _LOCKED,
                    {"organization_id": authorized.organization_id},
                )
            ).one_or_none()
            if row is None:
                raise ResourceNotFound()
            current = _settings(row)
            updated = OrganizationSettings(
                organization_id=current.organization_id,
                code=current.code,
                name=current.name if name is None else name,
                kind=current.kind,
                status=current.status,
                concession_max_days=(current.concession_max_days if max_days is None else max_days),
                concession_default_days=(
                    current.concession_default_days if default_days is None else default_days
                ),
            )
            if updated.concession_default_days > updated.concession_max_days:
                raise IdentityRejected(
                    IdentityRejection.INVALID_VALUE, field="/concession_default_days"
                )
            changed = sorted(
                field_name
                for field_name, before, after in (
                    ("name", current.name, updated.name),
                    (
                        "concession_max_days",
                        current.concession_max_days,
                        updated.concession_max_days,
                    ),
                    (
                        "concession_default_days",
                        current.concession_default_days,
                        updated.concession_default_days,
                    ),
                )
                if before != after
            )
            if not changed:
                return current
            await transaction.execute(
                _UPDATE,
                {
                    "organization_id": authorized.organization_id,
                    "name": updated.name,
                    "max_days": updated.concession_max_days,
                    "default_days": updated.concession_default_days,
                },
            )
            await deps.audit.append(
                authorized,
                AuditOperation.ORGANIZATION_SETTINGS_CHANGED,
                resource=ResourceRef("organization", authorized.organization_id),
                filters={
                    "fields": list[JsonValue](changed),
                    "concession_max_days": updated.concession_max_days,
                    "concession_default_days": updated.concession_default_days,
                },
                transaction=transaction,
            )
        return updated


def _settings(row: Any) -> OrganizationSettings:
    return OrganizationSettings(
        organization_id=as_uuid(row.organization_id),
        code=row.code,
        name=row.name,
        kind=row.kind,
        status=row.status,
        concession_max_days=int(row.concession_max_days),
        concession_default_days=int(row.concession_default_days),
    )
