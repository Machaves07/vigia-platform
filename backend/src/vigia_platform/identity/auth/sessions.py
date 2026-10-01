"""Sesiones del lado del servidor y retardo de fallos (LC-NUC-03; BR-NUC-24 a 27; PR-NUC-06, 07).

Pieza del módulo de autenticación (``IdentityProvider``, BR-NUC-28). Aquí está la lógica pura y
los puertos; el SQL vive en ``identity.adapters.session_store``. Módulo crítico aislado
(NFR-NUC-25): no importa FastAPI ni SQLAlchemy y no lee la hora del sistema (recibe un ``Clock``).

**Sesión** (PAT-NUC-SEG-03):

- Identificador aleatorio de 256 bits en base64url; en la base solo su SHA-256 en hexadecimal
  (``session_id_hash``). El identificador en claro solo existe en la cookie: ningún ``repr``,
  excepción, registro ni entrada de auditoría de este módulo lo contiene.
- Cookie ``__Host-vigia_session=<organization_id>.<identificador>; Secure; HttpOnly;
  SameSite=Strict; Path=/``. La organización viaja con el identificador para validar la sesión
  en **una** transacción con la seguridad a nivel de fila de esa organización (PAT-NUC-REN-03):
  una organización cambiada en la cookie no encuentra la fila. No es secreta (es la del propio
  usuario); el secreto son los 256 bits.
- Vence a los 30 minutos sin actividad (``idle_expires_at = last_seen_at + 30 min``) y a las
  12 horas en todo caso (``absolute_expires_at = created_at + 12 h``); en el instante exacto del
  vencimiento ya no es utilizable. Se valida en cada petición: estado, vencimientos, usuario
  activo, organización activa y segundo factor verificado (BR-NUC-25). Una sesión **pendiente**
  (contraseña correcta, segundo factor sin verificar) solo sirve para el segundo factor o su
  inscripción (``SessionPurpose.SECOND_FACTOR``, BR-NUC-22).
- Fin de sesión: ``closed`` (``logout``, ``closed_by_user``), ``expired`` (``idle_timeout``,
  ``absolute_timeout``, por la tarea ``expire_sessions``) o ``revoked`` (cambio de contraseña,
  restablecimiento del segundo factor, desactivación, suspensión). Todo fin se audita con su
  motivo (BR-NUC-27).

**Retardo de fallos** (``AuthThrottle``, BR-NUC-24, PAT-NUC-ESC-03): exacto, en PostgreSQL.

- Un contador por cuenta (``user_id``, en la organización de la cuenta) y otro por origen (hash
  del origen de red, en la organización **proveedora**: un solo contador por origen en toda la
  plataforma). Los intentos sobre cuentas inexistentes cuentan solo por origen.
- Tras el enésimo fallo consecutivo el retardo es 0 hasta el cuarto y ``30 s x 2^(n-5)`` desde el
  quinto, con tope de 15 minutos (``throttle_delay``). Un intento retenido no cuenta como fallo.
- **Reserva pesimista** (PAT-NUC-ESC-03, actualización atómica): cada intento se reserva
  **antes** de verificar, en la transacción que bloquea la fila (``reserve_attempt``). Si hay
  retardo vigente, el intento se retiene y la fila no cambia; si no, cuenta ya como fallo. Así
  una ráfaga concurrente verifica como mucho los intentos que el retardo permite: el sexto
  intento simultáneo ve los cinco anteriores. Si el intento resulta correcto, la reserva se
  devuelve (``release_attempt``): sin otros intentos por medio, la fila vuelve a su estado
  anterior; con intentos concurrentes, solo se resta el fallo y el retardo no se acorta. Un
  intento que falla a medias (error o caída) queda contado: el fallo es cerrado.
- La ventana de 15 minutos corre desde el último instante permitido (``next_allowed_at``): si no
  hay ningún fallo en los 15 minutos siguientes a poder reintentar, el contador vuelve a empezar.
  Así el retardo no decrece mientras siga el ataque (PR-NUC-06) y el tope de 15 min se alcanza.
- Un inicio correcto reinicia el contador de la cuenta; al del origen solo le devuelve su propia
  reserva (un atacante no limpia su origen entrando en una cuenta suya).
- ``security_alert`` una vez por ventana al llegar a 10 fallos por cuenta o 50 por origen. La
  alerta sale con la reserva que alcanza el umbral, aunque ese intento acabe siendo correcto; la
  devolución conserva ``alerted_at`` para no repetirla en la ventana.

El origen de red nunca se guarda en claro: ``origin_hash`` es un HMAC-SHA256 con una clave de la
plataforma. Un SHA-256 simple de una dirección IPv4 se invierte recorriendo las 2^32 direcciones.
"""

from __future__ import annotations

import base64
import enum
import hashlib
import hmac
import math
import os
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Protocol

from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ContextAbsent, ScopeContext, repository

__all__ = [
    "ABSOLUTE_TIMEOUT",
    "ACCOUNT_ALERT_THRESHOLD",
    "CLIENT_HINT_MAX_CHARS",
    "IDLE_TIMEOUT",
    "ORIGIN_ALERT_THRESHOLD",
    "ORIGIN_KEY_MIN_BYTES",
    "SESSION_COOKIE_ATTRIBUTES",
    "SESSION_COOKIE_NAME",
    "SESSION_TOKEN_BYTES",
    "THROTTLE_BASE_DELAY",
    "THROTTLE_FREE_FAILURES",
    "THROTTLE_MAX_DELAY",
    "THROTTLE_WINDOW",
    "FailureOutcome",
    "NewSession",
    "Reservation",
    "SessionContexts",
    "SessionCookie",
    "SessionEndReason",
    "SessionPurpose",
    "SessionService",
    "SessionStatus",
    "SessionStore",
    "SessionSummary",
    "ThrottleState",
    "ThrottleSubject",
    "ThrottleSubjectKind",
    "ValidSession",
    "absolute_expiry",
    "after_failure",
    "after_success",
    "alert_threshold",
    "clearing_cookie",
    "client_hint_of",
    "idle_expiry",
    "new_session_cookie",
    "origin_hash",
    "release_attempt",
    "reserve_attempt",
    "retry_after_seconds",
    "session_id_hash",
    "throttle_delay",
    "window_expired",
]

SESSION_TOKEN_BYTES: Final = 32
"""256 bits (LC-NUC-03; BR-NUC-25 pide al menos 128)."""
IDLE_TIMEOUT: Final = timedelta(minutes=30)
ABSOLUTE_TIMEOUT: Final = timedelta(hours=12)
SESSION_COOKIE_NAME: Final = "__Host-vigia_session"
"""Con el prefijo ``__Host-`` el navegador exige ``Secure``, ``Path=/`` y ningún ``Domain``."""
SESSION_COOKIE_ATTRIBUTES: Final = "Secure; HttpOnly; SameSite=Strict; Path=/"
CLIENT_HINT_MAX_CHARS: Final = 256

THROTTLE_FREE_FAILURES: Final = 4
"""Fallos sin retardo: el retardo empieza en el quinto (BR-NUC-24)."""
THROTTLE_BASE_DELAY: Final = timedelta(seconds=30)
THROTTLE_MAX_DELAY: Final = timedelta(minutes=15)
THROTTLE_WINDOW: Final = timedelta(minutes=15)
ACCOUNT_ALERT_THRESHOLD: Final = 10
ORIGIN_ALERT_THRESHOLD: Final = 50
ORIGIN_KEY_MIN_BYTES: Final = 32
_ORIGIN_MAX_CHARS: Final = 256

_TOKEN: Final = re.compile(r"[A-Za-z0-9_-]{43}")
"""32 bytes en base64url sin relleno."""
_UUID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SESSION_ID_HASH: Final = re.compile(r"[0-9a-f]{64}")
_MAX_EXPONENT: Final = math.ceil(math.log2(THROTTLE_MAX_DELAY / THROTTLE_BASE_DELAY))
"""A partir de este exponente, ``30 s x 2^k`` ya supera el tope: evita potencias enormes."""


# --- Identificador y cookie ---------------------------------------------------------------------


def session_id_hash(token: str) -> str:
    """SHA-256 en hexadecimal del identificador en claro (lo único que se guarda)."""
    if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
        raise ValueError("identificador de sesión mal formado")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class SessionCookie:
    """La cookie de sesión: la organización y el identificador en claro (nunca en ``repr``)."""

    organization_id: uuid.UUID
    token: str = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.organization_id) is not uuid.UUID:
            raise TypeError("organization_id debe ser uuid.UUID")
        session_id_hash(self.token)

    @property
    def session_id_hash(self) -> str:
        return session_id_hash(self.token)

    @property
    def value(self) -> str:
        """Valor de la cookie: ``<organization_id>.<identificador>``."""
        return f"{self.organization_id}.{self.token}"

    def header_value(self) -> str:
        """Cabecera ``Set-Cookie`` sin ``Max-Age``: el vencimiento lo decide el servidor."""
        return f"{SESSION_COOKIE_NAME}={self.value}; {SESSION_COOKIE_ATTRIBUTES}"

    @classmethod
    def parse(cls, value: object) -> SessionCookie | None:
        """La cookie de ``value`` o ``None`` si no tiene exactamente la forma esperada."""
        if not isinstance(value, str) or len(value) != 36 + 1 + 43:
            return None
        organization, separator, token = value.partition(".")
        if separator != "." or _UUID.fullmatch(organization) is None:
            return None
        if _TOKEN.fullmatch(token) is None:
            return None
        return cls(uuid.UUID(organization), token)


def new_session_cookie(
    organization_id: uuid.UUID, random_bytes: Callable[[int], bytes] = os.urandom
) -> SessionCookie:
    """Cookie nueva con un identificador de 256 bits."""
    raw = random_bytes(SESSION_TOKEN_BYTES)
    if not isinstance(raw, bytes) or len(raw) != SESSION_TOKEN_BYTES:
        raise ValueError("el generador no devolvió 32 bytes")
    token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return SessionCookie(organization_id, token)


def clearing_cookie() -> str:
    """``Set-Cookie`` que borra la cookie en el navegador al cerrar la sesión."""
    return f"{SESSION_COOKIE_NAME}=; Max-Age=0; {SESSION_COOKIE_ATTRIBUTES}"


def origin_hash(origin: str, key: bytes) -> str:
    """HMAC-SHA256 en hexadecimal del origen de red (nunca la dirección en claro).

    ``origin`` es la dirección tal como la entrega la interfaz (sin espacios alrededor, en
    minúsculas); vacío o ilegible cuenta como el origen ``unknown``.

    Raises:
        ValueError: la clave tiene menos de 32 bytes.
    """
    if not isinstance(key, bytes) or len(key) < ORIGIN_KEY_MIN_BYTES:
        raise ValueError("la clave del hash de origen debe tener al menos 32 bytes")
    text = origin.strip().lower() if isinstance(origin, str) else ""
    if not text or len(text) > _ORIGIN_MAX_CHARS or not text.isascii():
        text = "unknown"
    return hmac.new(key, text.encode("ascii"), hashlib.sha256).hexdigest()


# --- Vigencia de la sesión ----------------------------------------------------------------------


class SessionStatus(enum.StrEnum):
    """``session_status`` (domain-entities §1)."""

    ACTIVE = "active"
    EXPIRED = "expired"
    CLOSED = "closed"
    REVOKED = "revoked"


class SessionEndReason(enum.StrEnum):
    """``session_end_reason`` (domain-entities §1)."""

    LOGOUT = "logout"
    IDLE_TIMEOUT = "idle_timeout"
    ABSOLUTE_TIMEOUT = "absolute_timeout"
    CLOSED_BY_USER = "closed_by_user"
    PASSWORD_CHANGED = "password_changed"  # noqa: S105 - motivo, no un secreto
    SECOND_FACTOR_RESET = "second_factor_reset"
    USER_DEACTIVATED = "user_deactivated"
    ORGANIZATION_SUSPENDED = "organization_suspended"

    @property
    def status(self) -> SessionStatus:
        """El estado final que corresponde al motivo."""
        if self in (SessionEndReason.LOGOUT, SessionEndReason.CLOSED_BY_USER):
            return SessionStatus.CLOSED
        if self in (SessionEndReason.IDLE_TIMEOUT, SessionEndReason.ABSOLUTE_TIMEOUT):
            return SessionStatus.EXPIRED
        return SessionStatus.REVOKED


class SessionPurpose(enum.StrEnum):
    """Para qué se valida la sesión."""

    FULL = "full"
    """Cualquier ruta autenticada: exige el segundo factor verificado."""
    SECOND_FACTOR = "second_factor"
    """Solo la ruta del segundo factor o su inscripción: exige la sesión **pendiente**."""


def idle_expiry(last_seen_at: datetime) -> datetime:
    return last_seen_at + IDLE_TIMEOUT


def absolute_expiry(created_at: datetime) -> datetime:
    return created_at + ABSOLUTE_TIMEOUT


@dataclass(frozen=True, slots=True)
class NewSession:
    """La fila de una sesión nueva (el identificador en claro ya no está aquí)."""

    session_id_hash: str
    organization_id: uuid.UUID
    user_id: uuid.UUID
    created_at: datetime
    second_factor_verified: bool
    client_hint: str | None
    origin_hash: str

    def __post_init__(self) -> None:
        for name in ("session_id_hash", "origin_hash"):
            value = getattr(self, name)
            if not isinstance(value, str) or _SESSION_ID_HASH.fullmatch(value) is None:
                raise ValueError(f"{name} debe ser un SHA-256 en hexadecimal")
        if self.client_hint is not None and (
            not isinstance(self.client_hint, str) or len(self.client_hint) > CLIENT_HINT_MAX_CHARS
        ):
            raise ValueError("client_hint admite a lo sumo 256 caracteres")

    @property
    def last_seen_at(self) -> datetime:
        return self.created_at

    @property
    def idle_expires_at(self) -> datetime:
        return idle_expiry(self.created_at)

    @property
    def absolute_expires_at(self) -> datetime:
        return absolute_expiry(self.created_at)


def client_hint_of(user_agent: object) -> str | None:
    """El agente de usuario recortado a 256 caracteres, para que el usuario reconozca la sesión."""
    if not isinstance(user_agent, str):
        return None
    text = "".join(char for char in user_agent if char.isprintable()).strip()
    return text[:CLIENT_HINT_MAX_CHARS] or None


@dataclass(frozen=True, slots=True)
class ValidSession:
    """Una sesión aceptada por ``SessionStore.validate`` (ya prolongada)."""

    session_id_hash: str
    organization_id: uuid.UUID
    user_id: uuid.UUID
    email: str = field(repr=False)
    display_name: str = field(repr=False)
    second_factor_verified: bool
    second_factor_required: bool
    second_factor_enrolled: bool
    created_at: datetime
    last_seen_at: datetime

    @property
    def idle_expires_at(self) -> datetime:
        return idle_expiry(self.last_seen_at)

    @property
    def absolute_expires_at(self) -> datetime:
        return absolute_expiry(self.created_at)


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """Una sesión del usuario para la lista de BR-NUC-26 (sin el identificador en claro)."""

    session_id_hash: str
    status: SessionStatus
    end_reason: SessionEndReason | None
    created_at: datetime
    last_seen_at: datetime
    second_factor_verified: bool
    client_hint: str | None
    usable: bool
    """Utilizable en el instante de la consulta (como la validación completa)."""


class SessionStore(Protocol):
    """Persistencia de las sesiones bajo el ``ScopeContext`` (seguridad a nivel de fila).

    Cada método es una transacción y audita en ella lo que cierra. Una sesión de otra
    organización no existe para el almacén.
    """

    async def validate(
        self,
        context: ScopeContext,
        session_id_hash: str,
        now: datetime,
        purpose: SessionPurpose,
    ) -> ValidSession | None:
        """La sesión si es utilizable en ``now`` para ``purpose`` (ya prolongada) o ``None``."""
        ...

    async def close(
        self, context: ScopeContext, session_id_hash: str, reason: SessionEndReason, now: datetime
    ) -> bool:
        """Cierra la sesión si está activa y audita ``session_closed``; ``True`` si la cerró."""
        ...

    async def end_user_sessions(
        self,
        context: ScopeContext,
        user_id: uuid.UUID,
        reason: SessionEndReason,
        now: datetime,
        *,
        keep: str | None = None,
    ) -> int:
        """Termina las sesiones activas del usuario salvo ``keep`` y lo audita; cuántas cerró."""
        ...

    async def end_organization_sessions(
        self, context: ScopeContext, reason: SessionEndReason, now: datetime
    ) -> int:
        """Termina todas las sesiones activas de la organización del contexto y lo audita."""
        ...

    async def list_sessions(
        self, context: ScopeContext, user_id: uuid.UUID, now: datetime
    ) -> Sequence[SessionSummary]:
        """Todas las sesiones del usuario, de la más reciente a la más antigua."""
        ...

    async def expire(self, context: ScopeContext, now: datetime) -> int:
        """Marca vencidas las sesiones activas ya vencidas en ``now`` y audita cada una."""
        ...


class SessionContexts(Protocol):
    """Contexto de una organización antes de conocer a la persona (lo aporta TASK-125)."""

    def anonymous(self, organization_id: uuid.UUID) -> ScopeContext:
        """Contexto de la organización con un actor del sistema, sin persona identificada."""
        ...


@repository
class SessionService:
    """Validación, cierre, ``close_others`` e invalidación de sesiones (LC-NUC-03)."""

    def __init__(self, store: SessionStore, contexts: SessionContexts, clock: Clock) -> None:
        self._store = store
        self._contexts = contexts
        self._clock = clock

    def __repr__(self) -> str:
        return "SessionService()"

    async def validate(
        self, cookie: SessionCookie | None, purpose: SessionPurpose = SessionPurpose.FULL
    ) -> ValidSession | None:
        """La sesión de ``cookie`` si es utilizable ahora (y la prolonga); si no, ``None``."""
        if not isinstance(cookie, SessionCookie):
            return None
        context = self._contexts.anonymous(cookie.organization_id)
        return await self._store.validate(
            context, cookie.session_id_hash, self._clock.now(), purpose
        )

    async def close(self, cookie: SessionCookie | None) -> bool:
        """Cierre de sesión (``logout``): invalida en el servidor aunque ya hubiera vencido."""
        if not isinstance(cookie, SessionCookie):
            return False
        context = self._contexts.anonymous(cookie.organization_id)
        return await self._store.close(
            context, cookie.session_id_hash, SessionEndReason.LOGOUT, self._clock.now()
        )

    async def close_others(self, cookie: SessionCookie | None) -> int | None:
        """Cierra las demás sesiones del usuario (BR-NUC-26); ``None`` si la sesión no es válida."""
        session = await self.validate(cookie)
        if session is None:
            return None
        context = self._contexts.anonymous(session.organization_id)
        return await self._store.end_user_sessions(
            context,
            session.user_id,
            SessionEndReason.CLOSED_BY_USER,
            self._clock.now(),
            keep=session.session_id_hash,
        )

    async def list_sessions(self, cookie: SessionCookie | None) -> Sequence[SessionSummary] | None:
        """Las sesiones del usuario de ``cookie`` (BR-NUC-26); ``None`` si no es válida."""
        session = await self.validate(cookie)
        if session is None:
            return None
        context = self._contexts.anonymous(session.organization_id)
        return await self._store.list_sessions(context, session.user_id, self._clock.now())

    async def on_password_changed(
        self, context: ScopeContext, user_id: uuid.UUID, current: str | None
    ) -> int:
        """Cambio de contraseña: revoca todas las sesiones del usuario salvo la actual."""
        return await self._end_user(context, user_id, SessionEndReason.PASSWORD_CHANGED, current)

    async def on_user_deactivated(self, context: ScopeContext, user_id: uuid.UUID) -> int:
        """Desactivación: revoca todas las sesiones del usuario."""
        return await self._end_user(context, user_id, SessionEndReason.USER_DEACTIVATED, None)

    async def on_organization_suspended(self, context: ScopeContext) -> int:
        """Suspensión: revoca todas las sesiones de la organización del contexto."""
        _require_context(context)
        return await self._store.end_organization_sessions(
            context, SessionEndReason.ORGANIZATION_SUSPENDED, self._clock.now()
        )

    async def _end_user(
        self,
        context: ScopeContext,
        user_id: uuid.UUID,
        reason: SessionEndReason,
        keep: str | None,
    ) -> int:
        _require_context(context)
        if type(user_id) is not uuid.UUID:
            raise TypeError("user_id debe ser uuid.UUID")
        return await self._store.end_user_sessions(
            context, user_id, reason, self._clock.now(), keep=keep
        )


# --- Retardo de fallos (funciones puras) --------------------------------------------------------


class ThrottleSubjectKind(enum.StrEnum):
    """``throttle_subject_kind`` (domain-entities §1)."""

    ACCOUNT = "account"
    ORIGIN = "origin"


@dataclass(frozen=True, slots=True)
class ThrottleSubject:
    """Una fila de ``AuthThrottle``: la cuenta en su organización o el origen en la proveedora."""

    organization_id: uuid.UUID
    kind: ThrottleSubjectKind
    key: str

    @classmethod
    def account(cls, organization_id: uuid.UUID, user_id: uuid.UUID) -> ThrottleSubject:
        return cls(organization_id, ThrottleSubjectKind.ACCOUNT, str(user_id))

    @classmethod
    def origin(cls, provider_organization_id: uuid.UUID, hashed_origin: str) -> ThrottleSubject:
        if _SESSION_ID_HASH.fullmatch(hashed_origin) is None:
            raise ValueError("el origen debe llegar como hash")
        return cls(provider_organization_id, ThrottleSubjectKind.ORIGIN, hashed_origin)


@dataclass(frozen=True, slots=True)
class ThrottleState:
    """El estado de una fila de ``AuthThrottle`` (domain-entities §2.10)."""

    consecutive_failures: int
    window_started_at: datetime
    next_allowed_at: datetime
    alerted_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class FailureOutcome:
    """El estado tras un fallo y si ese fallo dispara la alerta de la ventana."""

    state: ThrottleState
    alert: bool


def throttle_delay(failures: int) -> timedelta:
    """Retardo tras ``failures`` fallos consecutivos: 0 hasta 4; ``30 s x 2^(n-5)``, tope 15 min."""
    if isinstance(failures, bool) or not isinstance(failures, int) or failures < 0:
        raise ValueError("failures debe ser un entero no negativo")
    if failures <= THROTTLE_FREE_FAILURES:
        return timedelta(0)
    exponent = failures - THROTTLE_FREE_FAILURES - 1
    if exponent >= _MAX_EXPONENT:
        return THROTTLE_MAX_DELAY
    delay: timedelta = THROTTLE_BASE_DELAY * (1 << exponent)
    return min(delay, THROTTLE_MAX_DELAY)


def alert_threshold(kind: ThrottleSubjectKind) -> int:
    """10 fallos por cuenta, 50 por origen (BR-NUC-24)."""
    if kind is ThrottleSubjectKind.ACCOUNT:
        return ACCOUNT_ALERT_THRESHOLD
    return ORIGIN_ALERT_THRESHOLD


def window_expired(state: ThrottleState, now: datetime) -> bool:
    """¿Pasaron 15 minutos sin fallos desde que se pudo volver a intentar?"""
    return now >= state.next_allowed_at + THROTTLE_WINDOW


def retry_after_seconds(state: ThrottleState | None, now: datetime) -> int:
    """Segundos que faltan para poder intentar (redondeo hacia arriba); 0 si ya se puede."""
    if state is None or state.next_allowed_at <= now:
        return 0
    return math.ceil((state.next_allowed_at - now).total_seconds())


def after_failure(state: ThrottleState | None, now: datetime, *, threshold: int) -> FailureOutcome:
    """El estado tras un fallo en ``now`` (``state`` es el guardado, o ``None`` si no hay fila)."""
    if state is None or state.consecutive_failures == 0 or window_expired(state, now):
        failures, window_started_at, alerted_at = 1, now, None
        floor = now
    else:
        failures = state.consecutive_failures + 1
        window_started_at, alerted_at = state.window_started_at, state.alerted_at
        # Dos fallos concurrentes nunca acortan un retardo ya concedido.
        floor = max(now, state.next_allowed_at)
    alert = failures >= threshold and alerted_at is None
    new_state = ThrottleState(
        consecutive_failures=failures,
        window_started_at=window_started_at,
        next_allowed_at=max(floor, now + throttle_delay(failures)),
        alerted_at=now if alert else alerted_at,
    )
    return FailureOutcome(new_state, alert)


def after_success(now: datetime) -> ThrottleState:
    """El contador de la cuenta tras un inicio correcto (o al limpiar una ventana vencida)."""
    return ThrottleState(0, now, now, None)


@dataclass(frozen=True, slots=True)
class Reservation:
    """Un intento reservado antes de verificarlo (``reserve_attempt``)."""

    subject: ThrottleSubject
    before: ThrottleState
    """La fila tal como estaba al bloquearla."""
    retry_after_seconds: int
    """Mayor que 0 si el intento se retiene (y entonces no cuenta)."""
    outcome: FailureOutcome | None
    """El fallo anotado de forma pesimista; ``None`` si el intento se retiene."""

    @property
    def granted(self) -> bool:
        return self.outcome is not None


def reserve_attempt(subject: ThrottleSubject, before: ThrottleState, now: datetime) -> Reservation:
    """Reserva un intento sobre la fila bloqueada ``before``: retenido o contado ya como fallo."""
    retry = retry_after_seconds(before, now)
    if retry > 0:
        return Reservation(subject, before, retry, None)
    outcome = after_failure(before, now, threshold=alert_threshold(subject.kind))
    return Reservation(subject, before, 0, outcome)


def release_attempt(current: ThrottleState, reservation: Reservation) -> ThrottleState:
    """La fila tras devolver la reserva de un intento correcto (``current`` es la bloqueada).

    Sin intentos por medio (``current`` es lo que dejó la reserva), la fila vuelve a ``before``.
    Con intentos concurrentes, solo se resta el fallo: el retardo que fijaron no se acorta. En los
    dos casos se conserva ``alerted_at``: la alerta de la ventana no se repite.
    """
    if reservation.outcome is None:
        raise ValueError("un intento retenido no tiene reserva que devolver")
    if current == reservation.outcome.state:
        return ThrottleState(
            reservation.before.consecutive_failures,
            reservation.before.window_started_at,
            reservation.before.next_allowed_at,
            current.alerted_at,
        )
    return ThrottleState(
        max(0, current.consecutive_failures - 1),
        current.window_started_at,
        current.next_allowed_at,
        current.alerted_at,
    )


def _require_context(context: object) -> None:
    if not isinstance(context, ScopeContext):
        raise ContextAbsent()
