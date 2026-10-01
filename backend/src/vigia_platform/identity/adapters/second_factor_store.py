"""``SecondFactorStore`` sobre PostgreSQL (LC-NUC-02; BR-NUC-21, 29; PAT-NUC-SEG-04).

El puerto de ``identity.auth.second_factor`` con SQL explícito y parámetros sobre ``shared.db``:
cada método abre **una** transacción con el ``ScopeContext`` (seguridad a nivel de fila forzada),
así que un usuario de otra organización no existe para el almacén (``not_found``).

- La fila de ``identity.totp_credential`` solo guarda ``secret_encrypted`` y
  ``data_key_wrapped`` (cifrado de sobre); el secreto en claro nunca llega aquí.
- Una credencial está **confirmada** cuando ``user_account.second_factor_enrolled_at`` es su
  ``enrolled_at``: el usuario demostró un primer código (``confirm_enrollment``, BR-NUC-22).
- ``save_enrollment`` sustituye una credencial **desactivada o sin confirmar** (``ON CONFLICT ...
  WHERE``) y nunca una activa y confirmada: con una así, ``AlreadyEnrolled``. No marca la
  inscripción ni la audita: eso lo hace ``confirm_enrollment`` en su transacción.
- ``advance_step`` fija ``last_accepted_step`` solo si la credencial está activa y confirmada y
  el paso es mayor que el guardado, en la misma sentencia: dos verificaciones concurrentes del
  mismo paso no se aceptan las dos.
- Los códigos de recuperación válidos son los sin usar **de la inscripción vigente y
  confirmada**: ``generated_at = enrolled_at`` de una credencial activa y confirmada. Los de
  inscripciones anteriores no se borran (``vigia_app`` no tiene ``DELETE``) y dejan de valer.
- ``reset`` desactiva la credencial, borra ``second_factor_enrolled_at``, revoca las sesiones
  activas con ``second_factor_reset`` y audita ``second_factor_reset`` y el cierre de sesiones en
  la misma transacción.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Final

from sqlalchemy import text

from vigia_platform.identity.adapters.session_store import end_user_sessions
from vigia_platform.identity.auth.second_factor import (
    AlreadyEnrolled,
    RecoveryCodeRecord,
    SecondFactorNotFound,
    TotpCredential,
)
from vigia_platform.identity.auth.sessions import SessionEndReason
from vigia_platform.ledger.application.audit_writer import AuditOperation, AuditWriter, ResourceRef
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.context import ScopeContext, repository

__all__ = ["PostgresSecondFactorStore"]

_USER_RESOURCE: Final = "user"

# Una credencial ``c`` está confirmada cuando la inscripción del usuario es la suya:
# EXISTS (SELECT FROM identity.user_account u WHERE u.user_id = c.user_id
#         AND u.second_factor_enrolled_at = c.enrolled_at). Va escrita en cada sentencia.
_SELECT_CREDENTIAL: Final = text(
    "SELECT c.user_id, c.organization_id, c.secret_encrypted, c.data_key_wrapped, c.enrolled_at,"
    " c.last_accepted_step, c.disabled_at,"
    " EXISTS (SELECT FROM identity.user_account u WHERE u.user_id = c.user_id"
    " AND u.second_factor_enrolled_at = c.enrolled_at) AS confirmed"
    " FROM identity.totp_credential c WHERE c.user_id = :user_id"
)
_UPSERT_CREDENTIAL: Final = text(
    "INSERT INTO identity.totp_credential (user_id, organization_id, secret_encrypted,"
    " data_key_wrapped, enrolled_at, last_accepted_step, disabled_at)"
    " VALUES (:user_id, :organization_id, :secret_encrypted, :data_key_wrapped, :enrolled_at,"
    " NULL, NULL)"
    " ON CONFLICT (user_id) DO UPDATE SET secret_encrypted = EXCLUDED.secret_encrypted,"
    " data_key_wrapped = EXCLUDED.data_key_wrapped, enrolled_at = EXCLUDED.enrolled_at,"
    " last_accepted_step = NULL, disabled_at = NULL"
    " WHERE identity.totp_credential.disabled_at IS NOT NULL"
    " OR NOT EXISTS (SELECT FROM identity.user_account u"
    " WHERE u.user_id = identity.totp_credential.user_id"
    " AND u.second_factor_enrolled_at = identity.totp_credential.enrolled_at)"
    " RETURNING user_id"
)
_INSERT_RECOVERY_CODE: Final = text(
    "INSERT INTO identity.recovery_code (recovery_code_id, user_id, organization_id, code_hash,"
    " generated_at) VALUES (:recovery_code_id, :user_id, :organization_id, :code_hash,"
    " :generated_at)"
)
_USER_EXISTS: Final = text("SELECT 1 FROM identity.user_account WHERE user_id = :user_id")
_CONFIRM_STEP: Final = text(
    "UPDATE identity.totp_credential c SET last_accepted_step = :step"
    " WHERE c.user_id = :user_id AND c.disabled_at IS NULL AND c.enrolled_at = :enrolled_at"
    " AND NOT EXISTS (SELECT FROM identity.user_account u WHERE u.user_id = c.user_id"
    " AND u.second_factor_enrolled_at = c.enrolled_at)"
    " AND (c.last_accepted_step IS NULL OR c.last_accepted_step < :step)"
    " RETURNING c.user_id"
)
_MARK_ENROLLED: Final = text(
    "UPDATE identity.user_account SET second_factor_enrolled_at = :enrolled_at"
    " WHERE user_id = :user_id AND second_factor_enrolled_at IS NULL RETURNING user_id"
)
_ADVANCE_STEP: Final = text(
    "UPDATE identity.totp_credential c SET last_accepted_step = :step"
    " WHERE c.user_id = :user_id AND c.disabled_at IS NULL"
    " AND EXISTS (SELECT FROM identity.user_account u WHERE u.user_id = c.user_id"
    " AND u.second_factor_enrolled_at = c.enrolled_at)"
    " AND (c.last_accepted_step IS NULL OR c.last_accepted_step < :step)"
    " RETURNING c.user_id"
)
_UNUSED_RECOVERY_CODES: Final = text(
    "SELECT r.recovery_code_id, r.user_id, r.organization_id, r.code_hash, r.generated_at,"
    " r.used_at FROM identity.recovery_code r"
    " JOIN identity.totp_credential c ON c.user_id = r.user_id"
    " JOIN identity.user_account u ON u.user_id = c.user_id"
    " AND u.second_factor_enrolled_at = c.enrolled_at"
    " WHERE r.user_id = :user_id AND r.used_at IS NULL AND c.disabled_at IS NULL"
    " AND r.generated_at = c.enrolled_at"
    " ORDER BY r.recovery_code_id"
)
_MARK_RECOVERY_CODE_USED: Final = text(
    "UPDATE identity.recovery_code r SET used_at = :used_at"
    " FROM identity.totp_credential c"
    " WHERE r.recovery_code_id = :recovery_code_id AND r.used_at IS NULL"
    " AND c.user_id = r.user_id AND c.disabled_at IS NULL AND r.generated_at = c.enrolled_at"
    " AND EXISTS (SELECT FROM identity.user_account u WHERE u.user_id = c.user_id"
    " AND u.second_factor_enrolled_at = c.enrolled_at)"
    " RETURNING r.recovery_code_id"
)
_CLEAR_ENROLLED: Final = text(
    "UPDATE identity.user_account SET second_factor_enrolled_at = NULL"
    " WHERE user_id = :user_id RETURNING user_id"
)
_DISABLE_CREDENTIAL: Final = text(
    "UPDATE identity.totp_credential SET disabled_at = :now"
    " WHERE user_id = :user_id AND disabled_at IS NULL"
)


def _uuid(value: uuid.UUID) -> uuid.UUID:
    """asyncpg devuelve su propia subclase de ``UUID``: el dominio recibe ``uuid.UUID``."""
    return uuid.UUID(bytes=value.bytes)


@repository
class PostgresSecondFactorStore:
    """``SecondFactorStore`` con ``shared.db`` y la auditoría en la misma transacción."""

    def __init__(self, database: LedgerDatabase, audit: AuditWriter) -> None:
        self._database = database
        self._audit = audit

    async def get_credential(
        self, context: ScopeContext, user_id: uuid.UUID
    ) -> TotpCredential | None:
        async with self._database.transaction(context) as transaction:
            row = (await transaction.execute(_SELECT_CREDENTIAL, {"user_id": user_id})).first()
        if row is None:
            return None
        return TotpCredential(
            user_id=_uuid(row.user_id),
            organization_id=_uuid(row.organization_id),
            secret_encrypted=bytes(row.secret_encrypted),
            data_key_wrapped=bytes(row.data_key_wrapped),
            enrolled_at=row.enrolled_at,
            last_accepted_step=row.last_accepted_step,
            disabled_at=row.disabled_at,
            confirmed=bool(row.confirmed),
        )

    async def save_enrollment(
        self,
        context: ScopeContext,
        credential: TotpCredential,
        recovery_codes: Sequence[RecoveryCodeRecord],
    ) -> None:
        if credential.organization_id != context.organization_id:
            raise SecondFactorNotFound()
        async with self._database.transaction(context) as transaction:
            if (
                await transaction.execute(_USER_EXISTS, {"user_id": credential.user_id})
            ).first() is None:
                raise SecondFactorNotFound()
            saved = await transaction.execute(
                _UPSERT_CREDENTIAL,
                {
                    "user_id": credential.user_id,
                    "organization_id": credential.organization_id,
                    "secret_encrypted": credential.secret_encrypted,
                    "data_key_wrapped": credential.data_key_wrapped,
                    "enrolled_at": credential.enrolled_at,
                },
            )
            if saved.first() is None:
                raise AlreadyEnrolled()
            for record in recovery_codes:
                await transaction.execute(
                    _INSERT_RECOVERY_CODE,
                    {
                        "recovery_code_id": record.recovery_code_id,
                        "user_id": record.user_id,
                        "organization_id": record.organization_id,
                        "code_hash": record.code_hash,
                        "generated_at": record.generated_at,
                    },
                )

    async def confirm_enrollment(
        self, context: ScopeContext, user_id: uuid.UUID, enrolled_at: datetime, step: int
    ) -> bool:
        async with self._database.transaction(context) as transaction:
            accepted = await transaction.execute(
                _CONFIRM_STEP, {"user_id": user_id, "enrolled_at": enrolled_at, "step": step}
            )
            if accepted.first() is None:
                return False
            marked = await transaction.execute(
                _MARK_ENROLLED, {"user_id": user_id, "enrolled_at": enrolled_at}
            )
            if marked.first() is None:
                raise _Conflict()
            await self._audit.append(
                context,
                AuditOperation.SECOND_FACTOR_ENROLLED,
                resource=ResourceRef(_USER_RESOURCE, user_id),
                transaction=transaction,
            )
        return True

    async def advance_step(self, context: ScopeContext, user_id: uuid.UUID, step: int) -> bool:
        async with self._database.transaction(context) as transaction:
            result = await transaction.execute(_ADVANCE_STEP, {"user_id": user_id, "step": step})
            return result.first() is not None

    async def unused_recovery_codes(
        self, context: ScopeContext, user_id: uuid.UUID
    ) -> Sequence[RecoveryCodeRecord]:
        async with self._database.transaction(context) as transaction:
            rows = (await transaction.execute(_UNUSED_RECOVERY_CODES, {"user_id": user_id})).all()
        return tuple(
            RecoveryCodeRecord(
                recovery_code_id=_uuid(row.recovery_code_id),
                user_id=_uuid(row.user_id),
                organization_id=_uuid(row.organization_id),
                code_hash=row.code_hash,
                generated_at=row.generated_at,
                used_at=row.used_at,
            )
            for row in rows
        )

    async def mark_recovery_code_used(
        self, context: ScopeContext, recovery_code_id: uuid.UUID, used_at: datetime
    ) -> bool:
        async with self._database.transaction(context) as transaction:
            result = await transaction.execute(
                _MARK_RECOVERY_CODE_USED,
                {"recovery_code_id": recovery_code_id, "used_at": used_at},
            )
            return result.first() is not None

    async def reset(self, context: ScopeContext, user_id: uuid.UUID, now: datetime) -> int:
        async with self._database.transaction(context) as transaction:
            if (await transaction.execute(_CLEAR_ENROLLED, {"user_id": user_id})).first() is None:
                raise SecondFactorNotFound()
            await transaction.execute(_DISABLE_CREDENTIAL, {"user_id": user_id, "now": now})
            closed = await end_user_sessions(
                transaction, self._audit, user_id, SessionEndReason.SECOND_FACTOR_RESET, now
            )
            await self._audit.append(
                context,
                AuditOperation.SECOND_FACTOR_RESET,
                resource=ResourceRef(_USER_RESOURCE, user_id),
                result_count=closed,
                transaction=transaction,
            )
        return closed


class _Conflict(Exception):
    """El paso se aceptó pero la marca de inscripción ya estaba: se revierte todo."""

    def __init__(self) -> None:
        super().__init__("la inscripción cambió durante la confirmación")
