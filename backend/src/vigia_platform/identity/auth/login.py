"""Inicio de sesión en dos pasos (S-PLA-01; ``business-logic-model.md`` §1; BR-NUC-20 a 24, 28).

Pieza del módulo de autenticación (``IdentityProvider``, BR-NUC-28): la autorización nunca lo ve.
Orquesta la contraseña (LC-NUC-01), el segundo factor (LC-NUC-02), el retardo de fallos y las
sesiones (LC-NUC-03). El SQL está en ``identity.adapters.session_store``; los contextos de cada
organización los aporta TASK-125 por el puerto ``LoginContexts``. Módulo crítico aislado
(NFR-NUC-25): no importa FastAPI ni SQLAlchemy. No lee la hora del sistema.

``authenticate(email, password, origin)``:

1. Normaliza el correo y, en una transacción de la organización proveedora, resuelve la
   organización del correo (``identity.login_organization``) y **reserva** el intento en el
   retardo del origen (``reserve_attempt``: retenido, o contado ya como fallo).
2. En una segunda transacción lee la cuenta en su organización (con un correo desconocido, la
   misma consulta en la proveedora, que no la encuentra) y, si el origen no retuvo el intento,
   lo reserva también en el retardo de la cuenta. Las reservas se hacen con la fila bloqueada
   **antes** de verificar: una ráfaga concurrente no verifica más intentos de los que el retardo
   permite (PAT-NUC-ESC-03).
3. Si algún retardo retiene el intento: ``throttled`` con ``retry_after_seconds`` y
   ``login_throttled`` auditado, sin verificar nada (sin revelar si la cuenta existe); si el
   origen lo había reservado, se le devuelve (un intento retenido no cuenta).
4. Verifica **siempre** una contraseña con Argon2id, también si la cuenta no existe o no tiene
   credencial (contra un hash ficticio con los parámetros vigentes). Cuenta inexistente,
   invitada o desactivada, organización suspendida o contraseña incorrecta: el fallo ya está
   contado por la reserva; se audita ``login_failed`` y se responde ``unauthenticated`` con el
   mismo mensaje (``credenciales inválidas``) en todos los casos.
5. Contraseña correcta: sesión nueva y se devuelve la reserva del origen. Si el usuario tiene el
   segundo factor inscrito o requerido, la sesión queda **pendiente** (``second_factor_required``;
   utilizable solo para el segundo factor o, sin inscribir, para inscribirlo) y se devuelve la
   reserva de la cuenta. Si no, queda verificada, se reinicia el contador de la cuenta, se fija
   ``last_login_at`` y se audita ``login_succeeded``. Si el hash se calculó con parámetros de
   otra versión, se recalcula con los vigentes en la misma transacción.

El tiempo no distingue los casos (BR-NUC-23): con un correo desconocido o una cuenta cualquiera
que falla, el camino es el mismo (tres transacciones y una verificación de Argon2id); la cuenta
existente solo añade, dentro de la segunda transacción, la sentencia que bloquea su fila. La
única diferencia visible es la que BR-NUC-24 impone: una cuenta retenida por su retardo responde
``throttled`` desde cualquier origen.

``verify_second_factor(cookie, code)``: con la sesión pendiente, reserva el intento en el retardo
de la cuenta (retenido, o contado ya como fallo) y verifica un TOTP de 6 dígitos dentro de ±1
paso y posterior al último aceptado, o un código de recuperación sin usar (se marca usado). Con
éxito, la sesión queda verificada, se reinicia el contador y se audita ``login_succeeded`` con el
método. ``start_enrollment`` y ``confirm_enrollment`` son la inscripción obligatoria en el inicio
de sesión (BR-NUC-22): la inscripción solo cuenta cuando el usuario demuestra un primer código
válido, y ese código pasa por el mismo retardo.
"""

from __future__ import annotations

import enum
import re
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Protocol

from vigia_platform.identity.auth.passwords import PasswordHash, VerifyResult
from vigia_platform.identity.auth.second_factor import (
    EnrollmentChallenge,
    SecondFactorUser,
    TotpCredential,
)
from vigia_platform.identity.auth.sessions import (
    FailureOutcome,
    NewSession,
    Reservation,
    SessionCookie,
    SessionPurpose,
    SessionStore,
    ThrottleState,
    ThrottleSubject,
    ValidSession,
    client_hint_of,
    new_session_cookie,
    origin_hash,
    retry_after_seconds,
)
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.observability import redaction
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics

__all__ = [
    "INVALID_CREDENTIALS_MESSAGE",
    "Authenticated",
    "LoginAccount",
    "LoginAuditEvent",
    "LoginContexts",
    "LoginOutcome",
    "LoginService",
    "LoginStore",
    "PasswordPort",
    "Rejected",
    "RejectionCode",
    "SecondFactorMethod",
    "SecondFactorPort",
    "SecondFactorRequired",
    "normalize_email",
]

INVALID_CREDENTIALS_MESSAGE: Final = "credenciales inválidas"
"""El único mensaje de fallo: correo desconocido, contraseña o código incorrectos (BR-NUC-23)."""
THROTTLED_MESSAGE: Final = "demasiados intentos; espera antes de volver a intentar"
ENROLLMENT_REQUIRED_MESSAGE: Final = "inscribe el segundo factor para continuar"

_EMAIL: Final = re.compile(r"[^@\s]+@[^@\s]+")
_EMAIL_MAX_CHARS: Final = 254
_TOTP_CODE: Final = re.compile(r"[0-9]{6}", re.ASCII)


class LoginAuditEvent(enum.StrEnum):
    """Operaciones de auditoría del inicio de sesión (valores de ``audit_operation``)."""

    LOGIN_SUCCEEDED = "login_succeeded"
    LOGIN_FAILED = "login_failed"
    LOGIN_THROTTLED = "login_throttled"


class SecondFactorMethod(enum.StrEnum):
    """Cómo se superó el segundo paso (``filters.second_factor`` de ``login_succeeded``)."""

    TOTP = "totp"
    RECOVERY_CODE = "recovery_code"
    ENROLLMENT = "enrollment"


class RejectionCode(enum.StrEnum):
    """``api_error_code`` de un inicio de sesión rechazado."""

    UNAUTHENTICATED = "unauthenticated"
    THROTTLED = "throttled"
    SECOND_FACTOR_REQUIRED = "second_factor_required"


class _Result(enum.StrEnum):
    # Valores de menos de 20 caracteres: la redacción trata una tira más larga como un token.
    SUCCEEDED = "succeeded"
    SECOND_FACTOR_REQUIRED = "second_factor"
    FAILED = "failed"
    THROTTLED = "throttled"


class _FailureReason(enum.StrEnum):
    PASSWORD = "login_password"  # noqa: S105 - motivo, no un secreto
    SECOND_FACTOR = "login_second_factor"


redaction.DEFAULT_POLICY.register("result", [result.value for result in _Result])
redaction.DEFAULT_POLICY.register("reason", [reason.value for reason in _FailureReason])


# --- Resultados -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Authenticated:
    """Los dos pasos superados: la sesión es utilizable."""

    cookie: SessionCookie
    user_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class SecondFactorRequired:
    """Contraseña correcta; la sesión pendiente solo sirve para el segundo factor."""

    cookie: SessionCookie
    enrollment_required: bool
    """El usuario debe inscribir el segundo factor antes de nada (BR-NUC-22)."""


@dataclass(frozen=True, slots=True)
class Rejected:
    """Rechazo con un mensaje que no distingue la causa."""

    code: RejectionCode
    message: str
    retry_after_seconds: int | None = None


type LoginOutcome = Authenticated | SecondFactorRequired | Rejected

_INVALID: Final = Rejected(RejectionCode.UNAUTHENTICATED, INVALID_CREDENTIALS_MESSAGE)
_ENROLL_FIRST: Final = Rejected(RejectionCode.SECOND_FACTOR_REQUIRED, ENROLLMENT_REQUIRED_MESSAGE)


def _throttled(seconds: int) -> Rejected:
    return Rejected(RejectionCode.THROTTLED, THROTTLED_MESSAGE, seconds)


# --- Puertos ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LoginAccount:
    """La cuenta de un correo con lo que decide el inicio de sesión."""

    user_id: uuid.UUID
    organization_id: uuid.UUID
    email: str = field(repr=False)
    display_name: str = field(repr=False)
    user_active: bool
    organization_active: bool
    second_factor_required: bool
    second_factor_enrolled: bool
    password_hash: str | None = field(repr=False)
    throttle: ThrottleState | None
    """El retardo de la cuenta tal como estaba al leerla (antes de la reserva)."""

    @property
    def can_log_in(self) -> bool:
        return self.user_active and self.organization_active and self.password_hash is not None


class PasswordPort(Protocol):
    """Lo que el inicio de sesión usa de ``PasswordService`` (LC-NUC-01)."""

    async def verify(self, password: str, encoded: str) -> VerifyResult: ...

    async def hash(self, password: str) -> PasswordHash: ...


class SecondFactorPort(Protocol):
    """Lo que el inicio de sesión usa de ``SecondFactorService`` (LC-NUC-02)."""

    async def credential(
        self, context: ScopeContext, user_id: uuid.UUID
    ) -> TotpCredential | None: ...

    async def verify_totp(
        self, context: ScopeContext, credential: TotpCredential, code: str, now: datetime
    ) -> bool: ...

    async def consume_recovery_code(
        self, context: ScopeContext, credential: TotpCredential, code: str
    ) -> bool: ...

    async def enroll(
        self, context: ScopeContext, user: SecondFactorUser
    ) -> EnrollmentChallenge: ...

    async def confirm_enrollment(
        self, context: ScopeContext, credential: TotpCredential, code: str, now: datetime
    ) -> bool: ...


class LoginContexts(Protocol):
    """Contextos del inicio de sesión; los construye ``identity.authz.context`` (TASK-125)."""

    def anonymous(self, organization_id: uuid.UUID) -> ScopeContext:
        """Contexto de la organización sin persona identificada (actor del sistema)."""
        ...

    def for_user(
        self,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        display_name: str,
        session_id_hash: str,
    ) -> ScopeContext:
        """Contexto de la persona que acaba de acreditarse con la sesión ``session_id_hash``."""
        ...


class LoginStore(Protocol):
    """Persistencia del inicio de sesión; cada método es una transacción."""

    async def begin_login(
        self, context: ScopeContext, email: str | None, origin: ThrottleSubject, now: datetime
    ) -> tuple[uuid.UUID | None, Reservation]:
        """En la proveedora: la organización del correo (o ``None``; sin correo, sin búsqueda) y
        la reserva del intento en el retardo del origen, en una transacción."""
        ...

    async def load_account(
        self, context: ScopeContext, email: str, now: datetime, *, reserve: bool
    ) -> tuple[LoginAccount | None, Reservation | None]:
        """La cuenta del correo en la organización del contexto y, con ``reserve``, la reserva
        del intento en su retardo, en una transacción (sin cuenta, sin reserva)."""
        ...

    async def reserve(
        self,
        context: ScopeContext,
        subject: ThrottleSubject,
        now: datetime,
        *,
        user_id: uuid.UUID | None,
    ) -> Reservation:
        """Reserva un intento (``INSERT ... ON CONFLICT`` con la fila bloqueada), publica
        ``security_alert`` si toca y, si se retiene, audita ``login_throttled``; todo en una
        transacción."""
        ...

    async def release(self, context: ScopeContext, reservation: Reservation) -> None:
        """Devuelve la reserva de un intento correcto (``release_attempt``), con la fila
        bloqueada."""
        ...

    async def audit(
        self, context: ScopeContext, event: LoginAuditEvent, *, user_id: uuid.UUID | None
    ) -> None:
        """Una entrada de auditoría; sin cuenta, en la cadena de la proveedora (BR-NUC-61)."""
        ...

    async def open_session(
        self,
        context: ScopeContext,
        session: NewSession,
        *,
        rehash: PasswordHash | None,
        release: Reservation | None,
    ) -> None:
        """Inserta la sesión, recalcula el hash si toca y devuelve ``release`` (la reserva de la
        cuenta de una sesión pendiente). Si nace verificada, además reinicia el contador de la
        cuenta, fija ``last_login_at`` y audita ``login_succeeded``."""
        ...

    async def complete_second_factor(
        self,
        context: ScopeContext,
        session_id_hash: str,
        user_id: uuid.UUID,
        now: datetime,
        method: SecondFactorMethod,
    ) -> bool:
        """Marca verificada la sesión pendiente, reinicia el contador de la cuenta, fija
        ``last_login_at`` y audita ``login_succeeded``; ``False`` si la sesión ya no está."""
        ...


def normalize_email(email: object) -> str | None:
    """El correo sin espacios alrededor y en minúsculas, o ``None`` si no tiene forma de correo."""
    if not isinstance(email, str):
        return None
    normalized = email.strip().lower()
    if not 3 <= len(normalized) <= _EMAIL_MAX_CHARS or _EMAIL.fullmatch(normalized) is None:
        return None
    return normalized


# --- Servicio ---------------------------------------------------------------------------------


class LoginService:
    """``authenticate`` y el segundo paso (``business-logic-model.md`` §1)."""

    def __init__(
        self,
        *,
        store: LoginStore,
        sessions: SessionStore,
        passwords: PasswordPort,
        second_factor: SecondFactorPort,
        contexts: LoginContexts,
        clock: Clock,
        provider_organization_id: uuid.UUID,
        origin_key: bytes,
        metrics: PlatformMetrics | None = None,
        random_bytes: Callable[[int], bytes] | None = None,
    ) -> None:
        if type(provider_organization_id) is not uuid.UUID:
            raise TypeError("provider_organization_id debe ser uuid.UUID")
        origin_hash("", origin_key)  # valida la clave al construir, no en el primer intento
        self._store = store
        self._sessions = sessions
        self._passwords = passwords
        self._second_factor = second_factor
        self._contexts = contexts
        self._clock = clock
        self._provider = provider_organization_id
        self._origin_key = origin_key
        self._metrics = metrics if metrics is not None else get_metrics()
        self._random_bytes = random_bytes
        self._dummy_hash: str | None = None

    def __repr__(self) -> str:
        return "LoginService()"

    async def authenticate(
        self, email: str, password: str, origin: str, user_agent: str | None = None
    ) -> LoginOutcome:
        """Primer paso: correo y contraseña desde ``origin`` (dirección de red)."""
        now = self._clock.now()
        normalized = normalize_email(email)
        hashed_origin = origin_hash(origin, self._origin_key)
        origin_subject = ThrottleSubject.origin(self._provider, hashed_origin)
        provider_context = self._contexts.anonymous(self._provider)
        organization_id, by_origin = await self._store.begin_login(
            provider_context, normalized, origin_subject, now
        )
        # Con un correo desconocido, la misma consulta en la proveedora: el mismo trabajo.
        account_context = provider_context
        if organization_id is not None:
            account_context = self._contexts.anonymous(organization_id)
        account, reservation = await self._store.load_account(
            account_context, normalized or "", now, reserve=by_origin.granted
        )
        retry = max(
            by_origin.retry_after_seconds,
            retry_after_seconds(None if account is None else account.throttle, now),
        )
        if retry > 0:
            if by_origin.granted:
                await self._store.release(provider_context, by_origin)
            await self._audit_throttled(provider_context, account)
            return _throttled(retry)
        encoded = account.password_hash if account is not None else None
        verified = await self._passwords.verify(
            password if isinstance(password, str) else "",
            encoded if encoded is not None else await self._dummy(),
        )
        if account is None or not account.can_log_in or not verified.ok:
            await self._password_failure(provider_context, by_origin, account, reservation, now)
            return _INVALID
        rehash = await self._passwords.hash(password) if verified.needs_rehash else None
        cookie = self._new_cookie(account.organization_id)
        pending = account.second_factor_required or account.second_factor_enrolled
        session = NewSession(
            session_id_hash=cookie.session_id_hash,
            organization_id=account.organization_id,
            user_id=account.user_id,
            created_at=now,
            second_factor_verified=not pending,
            client_hint=client_hint_of(user_agent),
            origin_hash=hashed_origin,
        )
        context = self._contexts.for_user(
            account.organization_id, account.user_id, account.display_name, cookie.session_id_hash
        )
        await self._store.open_session(
            context, session, rehash=rehash, release=reservation if pending else None
        )
        await self._store.release(provider_context, by_origin)
        if pending:
            self._metrics.auth_logins_total.add(1, {"result": _Result.SECOND_FACTOR_REQUIRED})
            return SecondFactorRequired(cookie, not account.second_factor_enrolled)
        self._metrics.auth_logins_total.add(1, {"result": _Result.SUCCEEDED})
        return Authenticated(cookie, account.user_id)

    async def verify_second_factor(self, cookie: SessionCookie | None, code: str) -> LoginOutcome:
        """Segundo paso: TOTP o código de recuperación sobre la sesión pendiente."""
        return await self._second_step(cookie, code, enrollment=False)

    async def start_enrollment(self, cookie: SessionCookie | None) -> EnrollmentChallenge | None:
        """Inscripción obligatoria en el inicio de sesión: el reto (QR y códigos) o ``None``.

        Solo con una sesión pendiente de un usuario sin segundo factor inscrito.
        """
        session = await self._pending(cookie)
        if session is None or session.second_factor_enrolled:
            return None
        context = self._user_context(session)
        return await self._second_factor.enroll(
            context, SecondFactorUser(session.user_id, session.organization_id, session.email)
        )

    async def confirm_enrollment(self, cookie: SessionCookie | None, code: str) -> LoginOutcome:
        """Confirma la inscripción con un primer TOTP válido y completa el inicio de sesión."""
        return await self._second_step(cookie, code, enrollment=True)

    async def _second_step(
        self, cookie: SessionCookie | None, code: str, *, enrollment: bool
    ) -> LoginOutcome:
        now = self._clock.now()
        session = await self._pending(cookie)
        if session is None or cookie is None:
            self._metrics.auth_logins_total.add(1, {"result": _Result.FAILED})
            return _INVALID
        if session.second_factor_enrolled is enrollment:
            # Inscrito y pide inscribirse, o sin inscribir y trae un código: nada que verificar.
            return _INVALID if enrollment else _ENROLL_FIRST
        organization_context = self._contexts.anonymous(session.organization_id)
        subject = ThrottleSubject.account(session.organization_id, session.user_id)
        # Reservado antes de verificar: una ráfaga de códigos no esquiva el retardo.
        reservation = await self._store.reserve(
            organization_context, subject, now, user_id=session.user_id
        )
        if reservation.outcome is None:
            self._metrics.auth_logins_total.add(1, {"result": _Result.THROTTLED})
            return _throttled(reservation.retry_after_seconds)
        context = self._user_context(session)
        credential = await self._second_factor.credential(context, session.user_id)
        method = await self._check_code(context, credential, code, now, enrollment=enrollment)
        if method is None:
            await self._store.audit(
                organization_context, LoginAuditEvent.LOGIN_FAILED, user_id=session.user_id
            )
            self._count_failure(_FailureReason.SECOND_FACTOR, reservation.outcome, now)
            return _INVALID
        completed = await self._store.complete_second_factor(
            context, session.session_id_hash, session.user_id, now, method
        )
        if not completed:
            self._metrics.auth_logins_total.add(1, {"result": _Result.FAILED})
            return _INVALID
        self._metrics.auth_logins_total.add(1, {"result": _Result.SUCCEEDED})
        return Authenticated(cookie, session.user_id)

    async def _check_code(
        self,
        context: ScopeContext,
        credential: TotpCredential | None,
        code: str,
        now: datetime,
        *,
        enrollment: bool,
    ) -> SecondFactorMethod | None:
        if credential is None or not isinstance(code, str):
            return None
        if enrollment:
            confirmed = await self._second_factor.confirm_enrollment(context, credential, code, now)
            return SecondFactorMethod.ENROLLMENT if confirmed else None
        if _TOTP_CODE.fullmatch(code) is not None:
            accepted = await self._second_factor.verify_totp(context, credential, code, now)
            return SecondFactorMethod.TOTP if accepted else None
        used = await self._second_factor.consume_recovery_code(context, credential, code)
        return SecondFactorMethod.RECOVERY_CODE if used else None

    async def _pending(self, cookie: SessionCookie | None) -> ValidSession | None:
        if not isinstance(cookie, SessionCookie):
            return None
        return await self._sessions.validate(
            self._contexts.anonymous(cookie.organization_id),
            cookie.session_id_hash,
            self._clock.now(),
            SessionPurpose.SECOND_FACTOR,
        )

    def _user_context(self, session: ValidSession) -> ScopeContext:
        return self._contexts.for_user(
            session.organization_id,
            session.user_id,
            session.display_name,
            session.session_id_hash,
        )

    def _new_cookie(self, organization_id: uuid.UUID) -> SessionCookie:
        if self._random_bytes is None:
            return new_session_cookie(organization_id)
        return new_session_cookie(organization_id, self._random_bytes)

    async def _dummy(self) -> str:
        """Hash ficticio con los parámetros vigentes: la verificación cuesta lo mismo."""
        if self._dummy_hash is None:
            self._dummy_hash = (await self._passwords.hash(secrets.token_urlsafe(24))).encoded
        return self._dummy_hash

    async def _audit_throttled(
        self, provider_context: ScopeContext, account: LoginAccount | None
    ) -> None:
        if account is None:
            await self._store.audit(provider_context, LoginAuditEvent.LOGIN_THROTTLED, user_id=None)
        else:
            await self._store.audit(
                self._contexts.anonymous(account.organization_id),
                LoginAuditEvent.LOGIN_THROTTLED,
                user_id=account.user_id,
            )
        self._metrics.auth_logins_total.add(1, {"result": _Result.THROTTLED})

    async def _password_failure(
        self,
        provider_context: ScopeContext,
        origin: Reservation,
        account: LoginAccount | None,
        reservation: Reservation | None,
        now: datetime,
    ) -> None:
        # Los fallos ya los contaron las reservas; queda auditarlo (sin cuenta, en la proveedora).
        if account is None:
            await self._store.audit(provider_context, LoginAuditEvent.LOGIN_FAILED, user_id=None)
        else:
            await self._store.audit(
                self._contexts.anonymous(account.organization_id),
                LoginAuditEvent.LOGIN_FAILED,
                user_id=account.user_id,
            )
        if origin.outcome is not None:
            self._count_failure(_FailureReason.PASSWORD, origin.outcome, now)
        if reservation is not None and reservation.outcome is not None:
            self._count_failure(None, reservation.outcome, now)
        self._metrics.auth_logins_total.add(1, {"result": _Result.FAILED})

    def _count_failure(
        self, reason: _FailureReason | None, outcome: FailureOutcome, now: datetime
    ) -> None:
        if reason is not None:
            self._metrics.auth_failures_total.add(1, {"reason": reason})
        delay = retry_after_seconds(outcome.state, now)
        if delay > 0:
            self._metrics.auth_throttle_delay_ms.record(delay * 1000)
