"""Preferencias de notificación por correo del propio usuario (TASK-348; SUP-U05-35; BR-LAZ-126;
PAT-LAZ-SEG-03; PR-LAZ-61).

``UserNotificationPreference`` no tiene ningún campo para silenciar ``immediate`` (BR-LAZ-126,
imposibilidad estructural: el esquema de ``tables.py`` no declara esa columna). ``email_enabled_
normal`` y ``email_enabled_digest`` solo gobiernan el correo agrupado y el resumen semanal; el
aviso en la aplicación nunca se silencia.

**Propiedad del objeto** (PAT-LAZ-SEG-03, PR-LAZ-61): toda lectura y escritura opera sobre
``context.actor.id``, nunca sobre un ``user_id`` que llegue de otra parte; no existe ruta para
nombrar a otro usuario. ``ensure_owned`` es la comprobación defensiva que usa cualquier llamador
interno que sí reciba un ``user_id`` explícito (por ejemplo, una prueba o una futura ruta de
administración): ``NotificationPreferenceNotOwned`` si no coincide con el actor.

**Primera lectura** (como ``loop.config.service``): un usuario sin fila la recibe con sus valores
por defecto, creados en la misma transacción (``ON CONFLICT DO NOTHING`` por si una escritura
concurrente ya la creó).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from vigia_platform.identity.authz.authorize import (
    Authorizer,
    Resource,
    ResourceNotFound,
    narrowed,
)
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.notifications.adapters.postgres.tables import USER_NOTIFICATION_PREFERENCE
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.db import Transaction

__all__ = [
    "DEFAULT_WEEKLY_DIGEST_WEEKDAY",
    "MAX_WEEKLY_DIGEST_WEEKDAY",
    "MIN_WEEKLY_DIGEST_WEEKDAY",
    "NotificationPreference",
    "NotificationPreferenceNotOwned",
    "NotificationPreferenceUpdate",
    "NotificationPreferences",
    "ensure_owned",
    "preference_for",
]

DEFAULT_WEEKLY_DIGEST_WEEKDAY: Final = 1
"""Lunes (ISO 8601: 1 = lunes … 7 = domingo), como la columna de la base."""
MIN_WEEKLY_DIGEST_WEEKDAY: Final = 1
MAX_WEEKLY_DIGEST_WEEKDAY: Final = 7


class NotificationPreferenceNotOwned(Exception):
    """El llamador intenta leer o cambiar las preferencias de otro usuario (PR-LAZ-61)."""

    code: Final = "notifications_notification_preference_not_owned"

    def __init__(self) -> None:
        super().__init__("las preferencias de notificación son del propio usuario")


def ensure_owned(context: ScopeContext, user_id: uuid.UUID) -> None:
    """``NotificationPreferenceNotOwned`` si ``user_id`` no es el del actor del contexto."""
    if user_id != context.actor.id:
        raise NotificationPreferenceNotOwned()


@dataclass(frozen=True, slots=True)
class NotificationPreference:
    user_id: uuid.UUID
    email_enabled_normal: bool
    email_enabled_digest: bool
    weekly_digest_weekday: int
    updated_at: datetime
    email_suppressed_hard_bounce: bool
    email_suppressed_complaint: bool
    email_suppressed_at: datetime | None

    @property
    def email_suppressed(self) -> bool:
        """Rebote duro o queja (los llena el consumidor de TASK-325): ningún correo más."""
        return self.email_suppressed_hard_bounce or self.email_suppressed_complaint


@dataclass(frozen=True, slots=True)
class NotificationPreferenceUpdate:
    email_enabled_normal: bool
    email_enabled_digest: bool
    weekly_digest_weekday: int

    def __post_init__(self) -> None:
        if not MIN_WEEKLY_DIGEST_WEEKDAY <= self.weekly_digest_weekday <= MAX_WEEKLY_DIGEST_WEEKDAY:
            raise ValueError("weekly_digest_weekday debe estar entre 1 y 7")


def _preference(row: Any) -> NotificationPreference:
    return NotificationPreference(
        user_id=uuid.UUID(str(row.user_id)),
        email_enabled_normal=row.email_enabled_normal,
        email_enabled_digest=row.email_enabled_digest,
        weekly_digest_weekday=row.weekly_digest_weekday,
        updated_at=row.updated_at,
        email_suppressed_hard_bounce=row.email_suppressed_hard_bounce,
        email_suppressed_complaint=row.email_suppressed_complaint,
        email_suppressed_at=row.email_suppressed_at,
    )


async def preference_for(
    transaction: Transaction, user_id: uuid.UUID, clock: Clock
) -> NotificationPreference:
    """La fila vigente de ``user_id``; sin fila, sus valores por defecto (los crea)."""
    statement = select(USER_NOTIFICATION_PREFERENCE).where(
        USER_NOTIFICATION_PREFERENCE.c.user_id == user_id
    )
    row = (await transaction.execute(statement)).first()
    if row is not None:
        return _preference(row)
    await transaction.execute(
        pg_insert(USER_NOTIFICATION_PREFERENCE)
        .values(
            user_id=user_id,
            organization_id=transaction.context.organization_id,
            email_enabled_normal=True,
            email_enabled_digest=True,
            weekly_digest_weekday=DEFAULT_WEEKLY_DIGEST_WEEKDAY,
            updated_at=clock.now(),
            email_suppressed_hard_bounce=False,
            email_suppressed_complaint=False,
        )
        .on_conflict_do_nothing(index_elements=["user_id"])
    )
    row = (await transaction.execute(statement)).first()
    if row is None:  # pragma: no cover - imposible tras el INSERT ... DO NOTHING
        raise RuntimeError("la preferencia no se pudo crear ni leer")
    return _preference(row)


class _Database(Protocol):
    def transaction(self, context: ScopeContext) -> Any: ...


@repository
class NotificationPreferences:
    """``GET``/``PUT /notifications/preferences`` (SUP-U05-35): siempre las propias."""

    def __init__(
        self,
        *,
        database: _Database,
        clock: Clock,
        authorizer: Authorizer,
        provider_organization_id: uuid.UUID,
    ) -> None:
        self._database = database
        self._clock = clock
        self._authorizer = authorizer
        self._provider_organization_id = provider_organization_id

    def __repr__(self) -> str:
        return "NotificationPreferences()"

    async def _own_scope(self, context: ScopeContext) -> None:
        """``ResourceNotFound`` bajo concesión (A-69: ninguna fila de preferencias bajo
        concesión) o sin la clave del módulo con alcance."""
        key = PermissionKey.NOTIFICATIONS_READ
        if context.concession_id is not None:
            await self._authorizer.deny(
                context, key, Resource.organization(context.organization_id)
            )
        if narrowed(context, key, provider_organization_id=self._provider_organization_id) is None:
            raise ResourceNotFound()

    async def read_own(self, context: ScopeContext) -> NotificationPreference:
        await self._own_scope(context)
        async with self._database.transaction(context) as transaction:
            return await preference_for(transaction, context.actor.id, self._clock)

    async def update_own(
        self, context: ScopeContext, update_values: NotificationPreferenceUpdate
    ) -> NotificationPreference:
        """Actualiza (o crea) la fila del actor; nunca la de otro (PR-LAZ-61)."""
        await self._own_scope(context)
        user_id = context.actor.id
        async with self._database.transaction(context) as transaction:
            await preference_for(transaction, user_id, self._clock)
            now = self._clock.now()
            await transaction.execute(
                update(USER_NOTIFICATION_PREFERENCE)
                .where(USER_NOTIFICATION_PREFERENCE.c.user_id == user_id)
                .values(
                    email_enabled_normal=update_values.email_enabled_normal,
                    email_enabled_digest=update_values.email_enabled_digest,
                    weekly_digest_weekday=update_values.weekly_digest_weekday,
                    updated_at=now,
                )
            )
            return await preference_for(transaction, user_id, self._clock)
