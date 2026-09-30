"""Sesiones, retardo de fallos e inicio de sesión sobre PostgreSQL (LC-NUC-03; PAT-NUC-SEG-03).

Implementa ``SessionStore`` (``identity.auth.sessions``) y ``LoginStore`` (``identity.auth.login``)
con SQL explícito y parámetros sobre ``shared.db``: cada método abre **una** transacción con el
``ScopeContext`` (seguridad a nivel de fila forzada), así que una sesión, una cuenta o un
contador de otra organización no existen para el almacén.

- **Validación** (BR-NUC-25): una sola sentencia ``UPDATE ... RETURNING`` que comprueba estado,
  vencimientos, usuario activo, organización activa y segundo factor, y prolonga
  ``idle_expires_at = last_seen_at + 30 min`` en el mismo viaje (PAT-NUC-REN-03). En la base
  solo está ``session_id_hash``; el identificador en claro nunca llega aquí.
- **Retardo** (PAT-NUC-ESC-02, ESC-03): ``INSERT ... ON CONFLICT DO UPDATE ... RETURNING`` crea
  la fila o la bloquea y la devuelve; el estado siguiente lo calcula ``after_failure`` (la misma
  función pura que prueba PR-NUC-06) y se guarda en la misma transacción. Dos procesos que fallan
  a la vez se serializan en la fila: ningún fallo se pierde y el retardo es el mismo desde
  cualquier instancia. La alerta ``security_alert`` se publica en la bandeja dentro de esa
  transacción, una vez por ventana.
- **Correo → organización**: ``identity.login_organization`` (``nuc_0005``), la única búsqueda
  previa al contexto de la organización.
- **Auditoría**: todo evento de autenticación y todo fin de sesión, con su motivo en
  ``filters.end_reason``; sin cuenta, en la cadena de la proveedora (BR-NUC-61). Ninguna entrada
  lleva el identificador de sesión ni su hash.
- **Tareas periódicas** (una transacción por organización, TASK-130 las ejecuta):
  ``expire_sessions`` cada 5 minutos marca vencidas las sesiones activas ya vencidas y audita
  cada una; ``throttle_window_cleanup`` cada 15 minutos reinicia los contadores cuya ventana
  venció (``vigia_app`` no tiene ``DELETE``: la fila queda a cero).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text

from vigia_platform.identity.auth.login import (
    LoginAccount,
    LoginAuditEvent,
    SecondFactorMethod,
)
from vigia_platform.identity.auth.passwords import PasswordHash
from vigia_platform.identity.auth.sessions import (
    FailureOutcome,
    NewSession,
    SessionEndReason,
    SessionPurpose,
    SessionStatus,
    SessionSummary,
    ThrottleState,
    ThrottleSubject,
    ThrottleSubjectKind,
    ValidSession,
    after_failure,
    after_success,
    alert_threshold,
)
from vigia_platform.ledger.application.audit_writer import (
    AuditOperation,
    AuditOutcome,
    AuditWriter,
    ResourceRef,
)
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics
from vigia_platform.shared.outbox.publish import NewEvent, OutboxPort
from vigia_platform.shared.outbox.registries import (
    PeriodicHandler,
    PeriodicTask,
    PeriodicTaskRegistry,
    Schedule,
)
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "EXPIRE_SESSIONS",
    "EXPIRE_SESSIONS_SCHEDULE",
    "THROTTLE_WINDOW_CLEANUP",
    "THROTTLE_WINDOW_CLEANUP_SCHEDULE",
    "PostgresSessionStore",
    "cleanup_throttle_windows",
    "end_user_sessions",
    "expire_sessions",
    "expire_sessions_handler",
    "password_algorithm_version",
    "register_session_tasks",
    "throttle_window_cleanup_handler",
]

EXPIRE_SESSIONS: Final = "expire_sessions"
EXPIRE_SESSIONS_SCHEDULE: Final = Schedule.every(5 * 60)
THROTTLE_WINDOW_CLEANUP: Final = "throttle_window_cleanup"
THROTTLE_WINDOW_CLEANUP_SCHEDULE: Final = Schedule.every(15 * 60)

_USER: Final = "user"
_ORGANIZATION: Final = "organization"
_ALERT_KINDS: Final = {
    ThrottleSubjectKind.ACCOUNT: "login_failures_account",
    ThrottleSubjectKind.ORIGIN: "login_failures_origin",
}
_AUDIT_EVENTS: Final = {
    LoginAuditEvent.LOGIN_SUCCEEDED: (AuditOperation.LOGIN_SUCCEEDED, AuditOutcome.SUCCESS),
    LoginAuditEvent.LOGIN_FAILED: (AuditOperation.LOGIN_FAILED, AuditOutcome.DENIED),
    LoginAuditEvent.LOGIN_THROTTLED: (AuditOperation.LOGIN_THROTTLED, AuditOutcome.DENIED),
}

# --- Sesiones -----------------------------------------------------------------------------------

_VALIDATE: Final = text(
    "UPDATE identity.session s SET last_seen_at = GREATEST(s.last_seen_at, :now),"
    " idle_expires_at = GREATEST(s.last_seen_at, :now) + interval '30 minutes'"
    " FROM identity.user_account u, identity.organization o"
    " WHERE s.session_id_hash = :session_id_hash"
    " AND u.user_id = s.user_id AND o.organization_id = s.organization_id"
    " AND s.status = 'active' AND s.idle_expires_at > :now AND s.absolute_expires_at > :now"
    " AND s.second_factor_verified = :verified AND u.status = 'active' AND o.status = 'active'"
    " RETURNING s.session_id_hash, s.organization_id, s.user_id, u.email, u.display_name,"
    " s.second_factor_verified, u.second_factor_required,"
    " u.second_factor_enrolled_at IS NOT NULL AS second_factor_enrolled,"
    " s.created_at, s.last_seen_at"
)
_CLOSE: Final = text(
    "UPDATE identity.session SET status = :status, end_reason = :end_reason, ended_at = :now"
    " WHERE session_id_hash = :session_id_hash AND status = 'active' RETURNING user_id"
)
_END_USER_SESSIONS: Final = text(
    "UPDATE identity.session SET status = :status, end_reason = :end_reason, ended_at = :now"
    " WHERE user_id = :user_id AND status = 'active'"
    " AND session_id_hash IS DISTINCT FROM CAST(:keep AS text) RETURNING user_id"
)
_END_ORGANIZATION_SESSIONS: Final = text(
    "UPDATE identity.session SET status = :status, end_reason = :end_reason, ended_at = :now"
    " WHERE status = 'active' RETURNING user_id"
)
_EXPIRE: Final = text(
    "UPDATE identity.session SET status = 'expired', ended_at = :now,"
    " end_reason = CASE WHEN absolute_expires_at <= :now THEN 'absolute_timeout'"
    " ELSE 'idle_timeout' END"
    " WHERE status = 'active' AND (idle_expires_at <= :now OR absolute_expires_at <= :now)"
    " RETURNING user_id, end_reason"
)
_LIST_SESSIONS: Final = text(
    "SELECT s.session_id_hash, s.status, s.end_reason, s.created_at, s.last_seen_at,"
    " s.second_factor_verified, s.client_hint,"
    " (s.status = 'active' AND s.idle_expires_at > :now AND s.absolute_expires_at > :now"
    " AND s.second_factor_verified AND u.status = 'active' AND o.status = 'active') AS usable"
    " FROM identity.session s"
    " JOIN identity.user_account u ON u.user_id = s.user_id"
    " JOIN identity.organization o ON o.organization_id = s.organization_id"
    " WHERE s.user_id = :user_id ORDER BY s.created_at DESC, s.session_id_hash"
)
_INSERT_SESSION: Final = text(
    "INSERT INTO identity.session (session_id_hash, user_id, organization_id, created_at,"
    " last_seen_at, idle_expires_at, absolute_expires_at, second_factor_verified, client_hint,"
    " origin_hash) VALUES (:session_id_hash, :user_id, :organization_id, :created_at,"
    " :last_seen_at, :idle_expires_at, :absolute_expires_at, :second_factor_verified,"
    " :client_hint, :origin_hash)"
)
_COMPLETE_SECOND_FACTOR: Final = text(
    "UPDATE identity.session SET second_factor_verified = true,"
    " last_seen_at = GREATEST(last_seen_at, :now),"
    " idle_expires_at = GREATEST(last_seen_at, :now) + interval '30 minutes'"
    " WHERE session_id_hash = :session_id_hash AND user_id = :user_id AND status = 'active'"
    " AND NOT second_factor_verified AND idle_expires_at > :now AND absolute_expires_at > :now"
    " RETURNING session_id_hash"
)

# --- Inicio de sesión ---------------------------------------------------------------------------

_LOGIN_ORGANIZATION: Final = text(
    "SELECT identity.login_organization(CAST(:email AS text)) AS organization_id"
)
_LOAD_ACCOUNT: Final = text(
    "SELECT u.user_id, u.organization_id, u.email, u.display_name, u.status,"
    " o.status AS organization_status, u.second_factor_required,"
    " u.second_factor_enrolled_at IS NOT NULL AS second_factor_enrolled, p.password_hash,"
    " t.consecutive_failures, t.window_started_at, t.next_allowed_at, t.alerted_at"
    " FROM identity.user_account u"
    " JOIN identity.organization o ON o.organization_id = u.organization_id"
    " LEFT JOIN identity.password_credential p ON p.user_id = u.user_id"
    " LEFT JOIN identity.auth_throttle t ON t.organization_id = u.organization_id"
    " AND t.subject_kind = 'account' AND t.subject_key = CAST(u.user_id AS text)"
    " WHERE u.email = :email"
)
_THROTTLE: Final = text(
    "SELECT consecutive_failures, window_started_at, next_allowed_at, alerted_at"
    " FROM identity.auth_throttle WHERE organization_id = :organization_id"
    " AND subject_kind = :subject_kind AND subject_key = :subject_key"
)
_LOCK_THROTTLE: Final = text(
    "INSERT INTO identity.auth_throttle AS t (organization_id, subject_kind, subject_key,"
    " consecutive_failures, window_started_at, next_allowed_at)"
    " VALUES (:organization_id, :subject_kind, :subject_key, 0, :now, :now)"
    " ON CONFLICT (organization_id, subject_kind, subject_key)"
    " DO UPDATE SET consecutive_failures = t.consecutive_failures"
    " RETURNING t.consecutive_failures, t.window_started_at, t.next_allowed_at, t.alerted_at"
)
_SAVE_THROTTLE: Final = text(
    "UPDATE identity.auth_throttle SET consecutive_failures = :consecutive_failures,"
    " window_started_at = :window_started_at, next_allowed_at = :next_allowed_at,"
    " alerted_at = :alerted_at WHERE organization_id = :organization_id"
    " AND subject_kind = :subject_kind AND subject_key = :subject_key"
)
_CLEANUP_THROTTLE: Final = text(
    "UPDATE identity.auth_throttle SET consecutive_failures = 0, window_started_at = :now,"
    " next_allowed_at = :now, alerted_at = NULL"
    " WHERE consecutive_failures > 0 AND next_allowed_at + interval '15 minutes' <= :now"
)
_REHASH: Final = text(
    "UPDATE identity.password_credential SET password_hash = :password_hash,"
    " algorithm_version = :algorithm_version, updated_at = :now WHERE user_id = :user_id"
)
_LAST_LOGIN: Final = text(
    "UPDATE identity.user_account SET last_login_at = :now WHERE user_id = :user_id"
)


def password_algorithm_version(version: int) -> str:
    """``PasswordCredential.algorithm_version`` de una versión de ``HASH_VERSIONS``."""
    return f"argon2id-v{version}"


def _uuid(value: Any) -> uuid.UUID:
    """asyncpg devuelve su propia subclase de ``UUID``: el dominio recibe ``uuid.UUID``."""
    return uuid.UUID(bytes=value.bytes)


def _state(row: Any) -> ThrottleState | None:
    if row is None or row.consecutive_failures is None:
        return None
    return ThrottleState(
        consecutive_failures=int(row.consecutive_failures),
        window_started_at=row.window_started_at,
        next_allowed_at=row.next_allowed_at,
        alerted_at=row.alerted_at,
    )


def _subject_parameters(subject: ThrottleSubject) -> dict[str, Any]:
    return {
        "organization_id": subject.organization_id,
        "subject_kind": subject.kind.value,
        "subject_key": subject.key,
    }


async def _save_state(
    transaction: Transaction, subject: ThrottleSubject, state: ThrottleState
) -> None:
    """Guarda ``state`` en la fila de ``subject`` si existe (sin fila, nada que reiniciar)."""
    await transaction.execute(
        _SAVE_THROTTLE,
        {
            **_subject_parameters(subject),
            "consecutive_failures": state.consecutive_failures,
            "window_started_at": state.window_started_at,
            "next_allowed_at": state.next_allowed_at,
            "alerted_at": state.alerted_at,
        },
    )


async def end_user_sessions(
    transaction: Transaction,
    audit: AuditWriter,
    user_id: uuid.UUID,
    reason: SessionEndReason,
    now: datetime,
    *,
    keep: str | None = None,
) -> int:
    """Termina las sesiones activas de ``user_id`` salvo ``keep`` en ``transaction`` y lo audita.

    La usan el cierre de las demás (``sessions_closed_others``), el cambio de contraseña, el
    restablecimiento del segundo factor y la desactivación (``session_closed`` con el motivo),
    dentro de la transacción de cada operación.
    """
    rows = (
        await transaction.execute(
            _END_USER_SESSIONS,
            {
                "status": reason.status.value,
                "end_reason": reason.value,
                "now": now,
                "user_id": user_id,
                "keep": keep,
            },
        )
    ).all()
    closed = len(rows)
    operation = (
        AuditOperation.SESSIONS_CLOSED_OTHERS
        if reason is SessionEndReason.CLOSED_BY_USER
        else AuditOperation.SESSION_CLOSED
    )
    await audit.append(
        transaction.context,
        operation,
        resource=ResourceRef(_USER, user_id),
        filters={"end_reason": reason.value},
        result_count=closed,
        transaction=transaction,
    )
    return closed


async def expire_sessions(transaction: Transaction, audit: AuditWriter, now: datetime) -> int:
    """Marca vencidas las sesiones activas ya vencidas en ``now`` y audita cada una."""
    rows = (await transaction.execute(_EXPIRE, {"now": now})).all()
    for row in rows:
        await audit.append(
            transaction.context,
            AuditOperation.SESSION_CLOSED,
            resource=ResourceRef(_USER, _uuid(row.user_id)),
            filters={"end_reason": row.end_reason},
            result_count=1,
            transaction=transaction,
        )
    return len(rows)


async def cleanup_throttle_windows(transaction: Transaction, now: datetime) -> int:
    """Reinicia los contadores cuya ventana de 15 minutos venció; cuántos reinició."""
    result = await transaction.execute(_CLEANUP_THROTTLE, {"now": now})
    return int(getattr(result, "rowcount", 0) or 0)


def expire_sessions_handler(audit: AuditWriter, clock: Clock) -> PeriodicHandler:
    """Manejador de ``expire_sessions`` para ``PeriodicTaskRegistry``."""

    async def handler(transaction: Transaction) -> None:
        await expire_sessions(transaction, audit, clock.now())

    return handler


def throttle_window_cleanup_handler(clock: Clock) -> PeriodicHandler:
    """Manejador de ``throttle_window_cleanup`` para ``PeriodicTaskRegistry``."""

    async def handler(transaction: Transaction) -> None:
        await cleanup_throttle_windows(transaction, clock.now())

    return handler


def register_session_tasks(
    registry: PeriodicTaskRegistry, *, audit: AuditWriter, clock: Clock
) -> tuple[PeriodicTask, PeriodicTask]:
    """Registra ``expire_sessions`` (5 min) y ``throttle_window_cleanup`` (15 min) de U-02."""
    return (
        registry.register(
            EXPIRE_SESSIONS,
            EXPIRE_SESSIONS_SCHEDULE,
            expire_sessions_handler(audit, clock),
            unit=ActorUnit.U02,
        ),
        registry.register(
            THROTTLE_WINDOW_CLEANUP,
            THROTTLE_WINDOW_CLEANUP_SCHEDULE,
            throttle_window_cleanup_handler(clock),
            unit=ActorUnit.U02,
        ),
    )


class PostgresSessionStore:
    """``SessionStore`` y ``LoginStore`` con ``shared.db``, la auditoría y la bandeja."""

    def __init__(
        self,
        database: LedgerDatabase,
        audit: AuditWriter,
        outbox: OutboxPort,
        *,
        metrics: PlatformMetrics | None = None,
    ) -> None:
        self._database = database
        self._audit = audit
        self._outbox = outbox
        self._metrics = metrics if metrics is not None else get_metrics()

    def __repr__(self) -> str:
        return "PostgresSessionStore()"

    # --- SessionStore -------------------------------------------------------------------------

    async def validate(
        self,
        context: ScopeContext,
        session_id_hash: str,
        now: datetime,
        purpose: SessionPurpose,
    ) -> ValidSession | None:
        parameters = {
            "session_id_hash": session_id_hash,
            "now": now,
            "verified": purpose is SessionPurpose.FULL,
        }
        async with self._database.transaction(context) as transaction:
            row = (await transaction.execute(_VALIDATE, parameters)).first()
        if row is None:
            return None
        return ValidSession(
            session_id_hash=row.session_id_hash,
            organization_id=_uuid(row.organization_id),
            user_id=_uuid(row.user_id),
            email=row.email,
            display_name=row.display_name,
            second_factor_verified=bool(row.second_factor_verified),
            second_factor_required=bool(row.second_factor_required),
            second_factor_enrolled=bool(row.second_factor_enrolled),
            created_at=row.created_at,
            last_seen_at=row.last_seen_at,
        )

    async def close(
        self, context: ScopeContext, session_id_hash: str, reason: SessionEndReason, now: datetime
    ) -> bool:
        async with self._database.transaction(context) as transaction:
            row = (
                await transaction.execute(
                    _CLOSE,
                    {
                        "status": reason.status.value,
                        "end_reason": reason.value,
                        "now": now,
                        "session_id_hash": session_id_hash,
                    },
                )
            ).first()
            if row is None:
                return False
            await self._audit.append(
                context,
                AuditOperation.SESSION_CLOSED,
                resource=ResourceRef(_USER, _uuid(row.user_id)),
                filters={"end_reason": reason.value},
                result_count=1,
                transaction=transaction,
            )
        return True

    async def end_user_sessions(
        self,
        context: ScopeContext,
        user_id: uuid.UUID,
        reason: SessionEndReason,
        now: datetime,
        *,
        keep: str | None = None,
    ) -> int:
        async with self._database.transaction(context) as transaction:
            return await end_user_sessions(
                transaction, self._audit, user_id, reason, now, keep=keep
            )

    async def end_organization_sessions(
        self, context: ScopeContext, reason: SessionEndReason, now: datetime
    ) -> int:
        async with self._database.transaction(context) as transaction:
            closed = len(
                (
                    await transaction.execute(
                        _END_ORGANIZATION_SESSIONS,
                        {"status": reason.status.value, "end_reason": reason.value, "now": now},
                    )
                ).all()
            )
            await self._audit.append(
                context,
                AuditOperation.SESSION_CLOSED,
                resource=ResourceRef(_ORGANIZATION, context.organization_id),
                filters={"end_reason": reason.value},
                result_count=closed,
                transaction=transaction,
            )
        return closed

    async def list_sessions(
        self, context: ScopeContext, user_id: uuid.UUID, now: datetime
    ) -> Sequence[SessionSummary]:
        async with self._database.transaction(context) as transaction:
            rows = (
                await transaction.execute(_LIST_SESSIONS, {"user_id": user_id, "now": now})
            ).all()
        return tuple(
            SessionSummary(
                session_id_hash=row.session_id_hash,
                status=SessionStatus(row.status),
                end_reason=None if row.end_reason is None else SessionEndReason(row.end_reason),
                created_at=row.created_at,
                last_seen_at=row.last_seen_at,
                second_factor_verified=bool(row.second_factor_verified),
                client_hint=row.client_hint,
                usable=bool(row.usable),
            )
            for row in rows
        )

    async def expire(self, context: ScopeContext, now: datetime) -> int:
        async with self._database.transaction(context) as transaction:
            return await expire_sessions(transaction, self._audit, now)

    # --- LoginStore ---------------------------------------------------------------------------

    async def resolve_login(
        self, context: ScopeContext, email: str, origin: ThrottleSubject
    ) -> tuple[uuid.UUID | None, ThrottleState | None]:
        async with self._database.transaction(context) as transaction:
            found = (await transaction.execute(_LOGIN_ORGANIZATION, {"email": email})).one()
            state = _state(
                (await transaction.execute(_THROTTLE, _subject_parameters(origin))).first()
            )
        organization_id = None if found.organization_id is None else _uuid(found.organization_id)
        return organization_id, state

    async def load_account(
        self, context: ScopeContext, email: str, now: datetime
    ) -> LoginAccount | None:
        async with self._database.transaction(context) as transaction:
            row = (await transaction.execute(_LOAD_ACCOUNT, {"email": email})).first()
        if row is None:
            return None
        return LoginAccount(
            user_id=_uuid(row.user_id),
            organization_id=_uuid(row.organization_id),
            email=row.email,
            display_name=row.display_name,
            user_active=row.status == "active",
            organization_active=row.organization_status == "active",
            second_factor_required=bool(row.second_factor_required),
            second_factor_enrolled=bool(row.second_factor_enrolled),
            password_hash=row.password_hash,
            throttle=_state(row),
        )

    async def throttle_state(
        self, context: ScopeContext, subject: ThrottleSubject
    ) -> ThrottleState | None:
        async with self._database.transaction(context) as transaction:
            row = (await transaction.execute(_THROTTLE, _subject_parameters(subject))).first()
        return _state(row)

    async def record_failure(
        self,
        context: ScopeContext,
        subject: ThrottleSubject,
        now: datetime,
        *,
        audit: LoginAuditEvent | None,
        user_id: uuid.UUID | None,
    ) -> FailureOutcome:
        key = _subject_parameters(subject)
        async with self._database.transaction(context) as transaction:
            locked = (await transaction.execute(_LOCK_THROTTLE, {**key, "now": now})).one()
            outcome = after_failure(_state(locked), now, threshold=alert_threshold(subject.kind))
            await _save_state(transaction, subject, outcome.state)
            if outcome.alert:
                await self._publish_alert(transaction, subject, now)
            if audit is not None:
                await self._append(transaction, audit, user_id)
        return outcome

    async def reset_throttle(
        self, context: ScopeContext, subject: ThrottleSubject, now: datetime
    ) -> None:
        """Reinicia el contador de ``subject`` (lo que hace un inicio correcto con la cuenta)."""
        async with self._database.transaction(context) as transaction:
            await _save_state(transaction, subject, after_success(now))

    async def audit(
        self, context: ScopeContext, event: LoginAuditEvent, *, user_id: uuid.UUID | None
    ) -> None:
        async with self._database.transaction(context) as transaction:
            await self._append(transaction, event, user_id)

    async def open_session(
        self,
        context: ScopeContext,
        session: NewSession,
        *,
        rehash: PasswordHash | None,
    ) -> None:
        async with self._database.transaction(context) as transaction:
            await transaction.execute(
                _INSERT_SESSION,
                {
                    "session_id_hash": session.session_id_hash,
                    "user_id": session.user_id,
                    "organization_id": session.organization_id,
                    "created_at": session.created_at,
                    "last_seen_at": session.last_seen_at,
                    "idle_expires_at": session.idle_expires_at,
                    "absolute_expires_at": session.absolute_expires_at,
                    "second_factor_verified": session.second_factor_verified,
                    "client_hint": session.client_hint,
                    "origin_hash": session.origin_hash,
                },
            )
            if rehash is not None:
                await transaction.execute(
                    _REHASH,
                    {
                        "password_hash": rehash.encoded,
                        "algorithm_version": password_algorithm_version(rehash.algorithm_version),
                        "now": session.created_at,
                        "user_id": session.user_id,
                    },
                )
            if session.second_factor_verified:
                await self._succeed(
                    transaction, session.organization_id, session.user_id, session.created_at
                )

    async def complete_second_factor(
        self,
        context: ScopeContext,
        session_id_hash: str,
        user_id: uuid.UUID,
        now: datetime,
        method: SecondFactorMethod,
    ) -> bool:
        async with self._database.transaction(context) as transaction:
            row = (
                await transaction.execute(
                    _COMPLETE_SECOND_FACTOR,
                    {"session_id_hash": session_id_hash, "user_id": user_id, "now": now},
                )
            ).first()
            if row is None:
                return False
            await self._succeed(transaction, context.organization_id, user_id, now, method=method)
        return True

    # --- Internos -----------------------------------------------------------------------------

    async def _succeed(
        self,
        transaction: Transaction,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        now: datetime,
        *,
        method: SecondFactorMethod | None = None,
    ) -> None:
        """Inicio correcto: reinicia el contador de la cuenta, ``last_login_at`` y auditoría."""
        await _save_state(
            transaction, ThrottleSubject.account(organization_id, user_id), after_success(now)
        )
        await transaction.execute(_LAST_LOGIN, {"now": now, "user_id": user_id})
        await self._append(
            transaction,
            LoginAuditEvent.LOGIN_SUCCEEDED,
            user_id,
            filters=None if method is None else {"second_factor": method.value},
        )

    async def _append(
        self,
        transaction: Transaction,
        event: LoginAuditEvent,
        user_id: uuid.UUID | None,
        *,
        filters: dict[str, str] | None = None,
    ) -> None:
        operation, outcome = _AUDIT_EVENTS[event]
        if user_id is None:
            # Cuenta desconocida: el evento no tiene organización (BR-NUC-61).
            await self._audit.append_without_organization(
                transaction.context, operation, outcome=outcome, transaction=transaction
            )
            return
        await self._audit.append(
            transaction.context,
            operation,
            outcome=outcome,
            resource=ResourceRef(_USER, user_id),
            filters=filters,
            transaction=transaction,
        )

    async def _publish_alert(
        self, transaction: Transaction, subject: ThrottleSubject, now: datetime
    ) -> None:
        payload: dict[str, Any] = {
            "alert_kind": _ALERT_KINDS[subject.kind],
            "occurred_at": format_timestamp(now),
        }
        if subject.kind is ThrottleSubjectKind.ACCOUNT:
            payload["resource_kind"] = _USER
            payload["resource_id"] = subject.key
        await self._outbox.publish(
            transaction, NewEvent(event_name="security_alert", payload=payload)
        )
        self._metrics.security_alert_total.add(
            1,
            {"alert_type": "security_alert", "organization_id": str(subject.organization_id)},
        )
