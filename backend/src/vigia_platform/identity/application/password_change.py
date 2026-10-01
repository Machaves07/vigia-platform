"""Cambio de contraseña con sesión (``POST /auth/password``; BR-NUC-20, 24, 26; LC-NUC-01).

``PasswordChangeService.change(session, current, new)``: la persona de la sesión cambia **su**
contraseña. Nunca bajo concesión ni desde un contexto que no sea de sesión (``user_state``).

1. **Retardo de la cuenta** (BR-NUC-24): el intento se reserva antes de verificar, igual que en el
   inicio de sesión (``LoginStore.reserve``). Una sesión robada no sirve para probar contraseñas
   sin límite: retenido, ``throttled`` con ``retry_after_seconds``.
2. Verifica la contraseña actual con Argon2id. Si no coincide, el fallo queda contado y se audita
   ``password_changed`` con ``outcome = denied``: ``current_password_invalid``.
3. Con la actual correcta se devuelve la reserva y se aplica la política a la nueva (8 a 128
   caracteres, distinta del correo, fuera de las filtradas): ``password_rejected`` con los motivos.
4. En **una** transacción: guarda el hash nuevo, ``password_updated_at``, revoca todas las demás
   sesiones del usuario con ``password_changed`` (la actual sigue, BR-NUC-26) y audita
   ``password_changed`` (``success``). La contraseña en claro no se registra ni se persiste.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol

from sqlalchemy import text

from vigia_platform.identity.adapters.session_store import (
    end_user_sessions,
    password_algorithm_version,
)
from vigia_platform.identity.application.common import as_uuid
from vigia_platform.identity.auth.passwords import PasswordHash, PolicyResult, VerifyResult
from vigia_platform.identity.auth.sessions import (
    Reservation,
    SessionEndReason,
    ThrottleSubject,
)
from vigia_platform.ledger.application.audit_writer import (
    AuditOperation,
    AuditOutcome,
    AuditWriter,
    ResourceRef,
)
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorKind, ContextOrigin, ScopeContext, repository

__all__ = [
    "PasswordChangeRejected",
    "PasswordChangeRejection",
    "PasswordChangeService",
    "PasswordChanged",
]

_USER: Final = "user"


class PasswordChangeRejection(enum.StrEnum):
    """Por qué no se cambió la contraseña (lista cerrada)."""

    USER_STATE = "user_state"
    """Sin sesión propia (concesión, otro origen) o la cuenta ya no está activa."""
    THROTTLED = "throttled"
    """El retardo de la cuenta retiene el intento (BR-NUC-24)."""
    CURRENT_PASSWORD_INVALID = "current_password_invalid"  # noqa: S105 - código, no un secreto
    """La contraseña actual no coincide."""
    PASSWORD_REJECTED = "password_rejected"  # noqa: S105 - código, no un secreto
    """La nueva no cumple la política (BR-NUC-20)."""


class PasswordChangeRejected(Exception):
    """Rechazo con ``code`` cerrado; nunca repite una contraseña."""

    def __init__(
        self,
        code: PasswordChangeRejection,
        *,
        retry_after_seconds: int | None = None,
        details: tuple[str, ...] = (),
    ) -> None:
        super().__init__(code.value)
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        self.details = details


@dataclass(frozen=True, slots=True)
class PasswordChanged:
    sessions_closed: int
    """Cuántas de las demás sesiones del usuario se revocaron."""


class ChangePasswords(Protocol):
    """``PasswordService``: verificación, política y hash Argon2id."""

    async def verify(self, password: str, encoded: str) -> VerifyResult: ...

    async def check_policy(self, password: str, email: str) -> PolicyResult: ...

    async def hash(self, password: str) -> PasswordHash: ...


class ChangeThrottle(Protocol):
    """La parte de ``LoginStore`` del retardo de fallos (``PostgresSessionStore``)."""

    async def reserve(
        self,
        context: ScopeContext,
        subject: ThrottleSubject,
        now: datetime,
        *,
        user_id: uuid.UUID | None,
    ) -> Reservation: ...

    async def release(self, context: ScopeContext, reservation: Reservation) -> None: ...


class ChangeContexts(Protocol):
    def anonymous(self, organization_id: uuid.UUID) -> ScopeContext: ...


_ACCOUNT: Final = text(
    "SELECT u.email, u.status, p.password_hash FROM identity.user_account AS u"
    " LEFT JOIN identity.password_credential AS p ON p.user_id = u.user_id"
    " WHERE u.user_id = :user_id"
)
_UPDATE_PASSWORD: Final = text(
    "UPDATE identity.password_credential SET password_hash = :password_hash,"
    " algorithm_version = :algorithm_version, updated_at = :now,"
    " breach_checked_at = :breach_checked_at WHERE user_id = :user_id RETURNING user_id"
)
_TOUCH_USER: Final = text(
    "UPDATE identity.user_account SET password_updated_at = :now"
    " WHERE user_id = :user_id AND status = 'active' RETURNING user_id"
)


@repository
class PasswordChangeService:
    """El cambio de la propia contraseña (``POST /auth/password``)."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        audit: AuditWriter,
        passwords: ChangePasswords,
        throttle: ChangeThrottle,
        contexts: ChangeContexts,
        clock: Clock,
    ) -> None:
        self._database = database
        self._audit = audit
        self._passwords = passwords
        self._throttle = throttle
        self._contexts = contexts
        self._clock = clock

    def __repr__(self) -> str:
        return "PasswordChangeService()"

    async def change(self, context: ScopeContext, current: str, new: str) -> PasswordChanged:
        """Cambia la contraseña de la persona de ``context`` (la sesión de la ruta)."""
        if (
            context.origin is not ContextOrigin.SESSION
            or context.actor.kind is not ActorKind.USER
            or context.concession_id is not None
            or context.session_id_hash is None
        ):
            raise PasswordChangeRejected(PasswordChangeRejection.USER_STATE)
        user_id = context.actor.id
        organization_id = context.organization_id
        now = self._clock.now()
        throttle_context = self._contexts.anonymous(organization_id)
        reservation = await self._throttle.reserve(
            throttle_context,
            ThrottleSubject.account(organization_id, user_id),
            now,
            user_id=user_id,
        )
        if not reservation.granted:
            raise PasswordChangeRejected(
                PasswordChangeRejection.THROTTLED,
                retry_after_seconds=reservation.retry_after_seconds,
            )
        rows = await self._database.read(context, _ACCOUNT, {"user_id": user_id})
        account = rows[0] if rows else None
        if account is None or account.status != "active" or account.password_hash is None:
            raise PasswordChangeRejected(PasswordChangeRejection.USER_STATE)
        verified = await self._passwords.verify(current, account.password_hash)
        if not verified.ok:
            # El fallo ya cuenta en el retardo por la reserva; queda auditarlo.
            async with self._database.transaction(context) as transaction:
                await self._audit.append(
                    context,
                    AuditOperation.PASSWORD_CHANGED,
                    outcome=AuditOutcome.DENIED,
                    resource=ResourceRef(_USER, user_id),
                    transaction=transaction,
                )
            raise PasswordChangeRejected(PasswordChangeRejection.CURRENT_PASSWORD_INVALID)
        await self._throttle.release(throttle_context, reservation)
        policy = await self._passwords.check_policy(new, account.email)
        if not policy.ok:
            raise PasswordChangeRejected(
                PasswordChangeRejection.PASSWORD_REJECTED,
                details=tuple(violation.value for violation in policy.violations),
            )
        hashed = await self._passwords.hash(new)
        now = self._clock.now()
        async with self._database.transaction(context) as transaction:
            updated = (
                await transaction.execute(
                    _UPDATE_PASSWORD,
                    {
                        "password_hash": hashed.encoded,
                        "algorithm_version": password_algorithm_version(hashed.algorithm_version),
                        "now": now,
                        "breach_checked_at": None if policy.breach_source is None else now,
                        "user_id": user_id,
                    },
                )
            ).first()
            touched = (
                await transaction.execute(_TOUCH_USER, {"now": now, "user_id": user_id})
            ).first()
            if updated is None or touched is None:
                raise PasswordChangeRejected(PasswordChangeRejection.USER_STATE)
            closed = await end_user_sessions(
                transaction,
                self._audit,
                as_uuid(user_id),
                SessionEndReason.PASSWORD_CHANGED,
                now,
                keep=context.session_id_hash,
            )
            await self._audit.append(
                context,
                AuditOperation.PASSWORD_CHANGED,
                resource=ResourceRef(_USER, user_id),
                result_count=closed,
                transaction=transaction,
            )
        return PasswordChanged(closed)
