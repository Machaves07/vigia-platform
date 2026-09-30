"""Segundo factor: TOTP y códigos de recuperación (LC-NUC-02; BR-NUC-21, 29; NFR-NUC-26; PR-NUC-09).

Pieza del módulo de autenticación (``IdentityProvider``) que usa el inicio de sesión en dos pasos
(LC-NUC-03, TASK-124). Obligatorio para ``administrator`` y ``platform_operator``; disponible
para todos (BR-NUC-21). Lo que decide si se exige es ``user_account.second_factor_required``.

- ``enroll(context, user) -> EnrollmentChallenge``: secreto aleatorio de 160 bits cifrado por
  sobre (``shared.crypto``, con el identificador de la credencial como dato asociado), código QR
  generado en el servidor con ``qrcode`` (SVG) y 10 códigos de recuperación que se muestran
  **una sola vez** y se guardan como Argon2id. Sin KMS no hay inscripción:
  ``SecretsUnavailable`` (``temporarily_unavailable``) y nada se guarda; el factor nunca se
  omite (FS-NUC-05 b). Un usuario con una credencial activa no se reinscribe sin ``reset``.
- ``verify_totp(context, credential, code, now) -> bool``: RFC 6238 con paso de 30 s, 6 dígitos
  ASCII y tolerancia de ±1 paso. Solo se acepta un paso **mayor** que ``last_accepted_step``
  (``business-logic-model.md`` §1), y el almacén lo avanza con una condición en la misma
  sentencia: dos verificaciones concurrentes del mismo código no se aceptan las dos. Sin KMS se
  verifica con la clave de datos que hay en memoria (``shared.crypto``).
- ``consume_recovery_code(context, credential, code) -> bool``: cada código de la inscripción
  vigente se acepta exactamente una vez; el usado se marca (``used_at``), no se borra.
- ``reset(admin_context, user_id)``: restablecimiento por un administrador de la organización
  (BR-NUC-29): desactiva la credencial, borra ``second_factor_enrolled_at`` (el siguiente inicio
  de sesión obliga a inscribirse), cierra las sesiones activas del usuario con
  ``second_factor_reset`` y audita ``second_factor_reset``, todo en una transacción del almacén.
  El permiso de la ruta lo comprueba la autorización (TASK-125); aquí se exige un actor persona
  u operador de la misma organización.

La base y la auditoría llegan por el puerto ``SecondFactorStore``
(``identity.adapters.second_factor_store`` sobre PostgreSQL). Módulo crítico aislado
(NFR-NUC-25): no importa FastAPI ni SQLAlchemy. No lee la hora del sistema: recibe un ``Clock``.

El secreto en claro solo existe en memoria durante ``enroll`` y ``verify_totp``: no se registra,
no se persiste y ningún ``repr`` ni excepción de este módulo lo contiene. Tampoco los códigos de
recuperación en claro ni la URI de aprovisionamiento.
"""

from __future__ import annotations

import base64
import hmac
import os
import re
import secrets
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Protocol
from urllib.parse import quote

import pyotp
import qrcode  # type: ignore[import-untyped]
import qrcode.image.svg  # type: ignore[import-untyped]
from argon2 import exceptions as argon2_exceptions

from vigia_platform.identity.auth.passwords import Argon2Parameters
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorKind, ContextAbsent, ScopeContext
from vigia_platform.shared.cpu_pool import CpuPool
from vigia_platform.shared.crypto import EnvelopeCrypto

__all__ = [
    "ISSUER",
    "RECOVERY_CODE_ALPHABET",
    "RECOVERY_CODE_COUNT",
    "RECOVERY_CODE_HASH",
    "RECOVERY_CODE_LENGTH",
    "TOTP_DIGITS",
    "TOTP_SECRET_BYTES",
    "TOTP_STEP_SECONDS",
    "TOTP_TOLERANCE_STEPS",
    "AlreadyEnrolled",
    "EnrollmentChallenge",
    "RecoveryCodeRecord",
    "SecondFactorError",
    "SecondFactorNotFound",
    "SecondFactorService",
    "SecondFactorStore",
    "SecondFactorUser",
    "TotpCredential",
    "credential_aad",
    "generate_recovery_codes",
    "hash_recovery_code",
    "match_totp",
    "normalize_recovery_code",
    "provisioning_uri",
    "qr_svg",
    "totp_code",
    "totp_step",
    "verify_recovery_code",
]

TOTP_STEP_SECONDS: Final = 30
TOTP_DIGITS: Final = 6
TOTP_TOLERANCE_STEPS: Final = 1
"""±1 paso respecto del reloj de la plataforma (BR-NUC-21)."""
TOTP_SECRET_BYTES: Final = 20
"""160 bits, la longitud que recomienda RFC 4226 §4 para HMAC-SHA-1."""
ISSUER: Final = "Vigía"
"""Emisor que muestra la aplicación de autenticación."""

RECOVERY_CODE_COUNT: Final = 10
RECOVERY_CODE_LENGTH: Final = 10
RECOVERY_CODE_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
"""Base32 de Crockford: sin ``I``, ``L``, ``O`` ni ``U``; 10 símbolos son 50 bits."""
RECOVERY_CODE_HASH: Final = Argon2Parameters(memory_kib=19 * 1024, iterations=2, parallelism=1)
"""Argon2id de los códigos de recuperación ``[objetivo propio]``: el mínimo de OWASP. Con 50 bits
aleatorios por código no hace falta el coste de las contraseñas (64 MB), y la verificación
recorre hasta 10 hashes."""

_TOTP_CODE: Final = re.compile(r"[0-9]{6}", re.ASCII)
_RECOVERY_SEPARATORS: Final = re.compile(r"[\s-]", re.ASCII)
_RECOVERY_CODE: Final = re.compile(f"[{RECOVERY_CODE_ALPHABET}]{{{RECOVERY_CODE_LENGTH}}}")
_RECOVERY_INPUT_MAX_CHARS: Final = 64
_ACCOUNT_LABEL_MAX_CHARS: Final = 254
_AAD_PREFIX: Final = b"vigia/identity/totp_credential/v1/"
_RECOVERY_HASHER: Final = RECOVERY_CODE_HASH.hasher()
_ARGON2ID_PREFIX: Final = "$argon2id$"
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_RESET_ACTORS: Final = frozenset({ActorKind.USER, ActorKind.OPERATOR})
"""Actores que restablecen el segundo factor de otro (BR-NUC-29): una persona o el operador."""


# --- Errores -----------------------------------------------------------------------------------


class SecondFactorError(Exception):
    """Error del segundo factor con ``code`` de ``api_error_code``."""

    code: str = "invalid_request"


class AlreadyEnrolled(SecondFactorError):
    """El usuario ya tiene una credencial activa: se reinscribe solo tras ``reset``."""

    code = "conflict"

    def __init__(self) -> None:
        super().__init__("el usuario ya tiene el segundo factor inscrito")


class SecondFactorNotFound(SecondFactorError):
    """El usuario no existe o es de otra organización: ``not_found``, nunca ``forbidden``."""

    code = "not_found"

    def __init__(self) -> None:
        super().__init__("usuario no encontrado")


# --- Valores -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SecondFactorUser:
    """El usuario que se inscribe; ``account_label`` es lo que muestra su aplicación (su correo)."""

    user_id: uuid.UUID
    organization_id: uuid.UUID
    account_label: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class TotpCredential:
    """``TotpCredential`` (domain-entities §2.7): el secreto solo cifrado por sobre."""

    user_id: uuid.UUID
    organization_id: uuid.UUID
    secret_encrypted: bytes = field(repr=False)
    data_key_wrapped: bytes = field(repr=False)
    enrolled_at: datetime
    last_accepted_step: int | None = None
    disabled_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.disabled_at is None


@dataclass(frozen=True, slots=True)
class RecoveryCodeRecord:
    """``RecoveryCode``: el hash Argon2id de un código; el usado se marca, no se borra."""

    recovery_code_id: uuid.UUID
    user_id: uuid.UUID
    organization_id: uuid.UUID
    code_hash: str = field(repr=False)
    generated_at: datetime
    used_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class EnrollmentChallenge:
    """Lo que se muestra **una vez** al inscribirse; nada de esto se guarda en claro."""

    credential: TotpCredential
    provisioning_uri: str = field(repr=False)
    qr_svg: str = field(repr=False)
    recovery_codes: tuple[str, ...] = field(repr=False)


class SecondFactorStore(Protocol):
    """Persistencia del segundo factor bajo el ``ScopeContext`` (seguridad a nivel de fila).

    Cada método es una transacción. Un usuario de otra organización no existe para el almacén.
    """

    async def get_credential(
        self, context: ScopeContext, user_id: uuid.UUID
    ) -> TotpCredential | None:
        """La credencial del usuario (activa o desactivada) o ``None``."""
        ...

    async def save_enrollment(
        self,
        context: ScopeContext,
        credential: TotpCredential,
        recovery_codes: Sequence[RecoveryCodeRecord],
    ) -> None:
        """Guarda la credencial nueva (o sustituye una desactivada), los códigos, marca
        ``second_factor_enrolled_at`` y audita ``second_factor_enrolled``, todo o nada.

        Raises:
            AlreadyEnrolled: el usuario ya tiene una credencial activa.
            SecondFactorNotFound: el usuario no existe en la organización del contexto.
        """
        ...

    async def advance_step(self, context: ScopeContext, user_id: uuid.UUID, step: int) -> bool:
        """Fija ``last_accepted_step = step`` solo si la credencial está activa y ``step`` es
        mayor que el guardado; ``True`` si cambió una fila."""
        ...

    async def unused_recovery_codes(
        self, context: ScopeContext, user_id: uuid.UUID
    ) -> Sequence[RecoveryCodeRecord]:
        """Los códigos sin usar de la inscripción vigente (credencial activa)."""
        ...

    async def mark_recovery_code_used(
        self, context: ScopeContext, recovery_code_id: uuid.UUID, used_at: datetime
    ) -> bool:
        """Marca el código usado solo si no lo estaba; ``True`` si cambió una fila."""
        ...

    async def reset(self, context: ScopeContext, user_id: uuid.UUID, now: datetime) -> int:
        """BR-NUC-29 en una transacción; devuelve cuántas sesiones cerró.

        Raises:
            SecondFactorNotFound: el usuario no existe en la organización del contexto.
        """
        ...


# --- TOTP (funciones puras) --------------------------------------------------------------------


def totp_step(now: datetime) -> int:
    """Paso TOTP de ``now``: segundos desde la época Unix divididos por 30, hacia abajo.

    Raises:
        ValueError: ``now`` sin zona horaria o anterior a la época.
    """
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now debe ser un datetime con zona horaria")
    delta = now - _EPOCH
    if delta.days < 0:
        raise ValueError("now es anterior a la época Unix")
    return (delta.days * 86_400 + delta.seconds) // TOTP_STEP_SECONDS


def totp_code(secret: bytes, step: int) -> str:
    """Código de 6 dígitos del paso ``step`` (HOTP de RFC 4226 con el paso como contador)."""
    if not isinstance(secret, bytes) or not secret:
        raise ValueError("el secreto debe ser bytes no vacíos")
    if isinstance(step, bool) or not isinstance(step, int) or step < 0:
        raise ValueError("el paso debe ser un entero no negativo")
    return pyotp.HOTP(_base32(secret), digits=TOTP_DIGITS).at(step)


def match_totp(
    secret: bytes, code: str, now: datetime, last_accepted_step: int | None
) -> int | None:
    """El paso que ``code`` acredita dentro de ±1 paso de ``now`` y posterior al último
    aceptado, o ``None``.

    ``code`` son exactamente 6 dígitos ASCII (sin espacios ni dígitos de otras escrituras). Se
    comparan en tiempo constante todos los pasos candidatos; si coinciden varios, el mayor.
    """
    if not isinstance(code, str) or _TOTP_CODE.fullmatch(code) is None:
        return None
    current = totp_step(now)
    floor = -1 if last_accepted_step is None else last_accepted_step
    matched: int | None = None
    for step in range(current - TOTP_TOLERANCE_STEPS, current + TOTP_TOLERANCE_STEPS + 1):
        if step <= floor or step < 0:
            continue
        if hmac.compare_digest(totp_code(secret, step), code):
            matched = step
    return matched


def provisioning_uri(secret: bytes, account_label: str) -> str:
    """URI ``otpauth://totp/`` (formato de Google Authenticator) para el código QR."""
    if not isinstance(account_label, str) or not 0 < len(account_label) <= _ACCOUNT_LABEL_MAX_CHARS:
        raise ValueError("la etiqueta de la cuenta debe tener de 1 a 254 caracteres")
    label = quote(f"{ISSUER}:{account_label}", safe="@:")
    return (
        f"otpauth://totp/{label}?secret={_base32(secret)}&issuer={quote(ISSUER, safe='')}"
        f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP_SECONDS}"
    )


def qr_svg(uri: str) -> str:
    """Código QR de ``uri`` como SVG, generado en el servidor (sin servicios externos)."""
    image = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage)
    svg = image.to_string(encoding="unicode")
    if not isinstance(svg, str):
        raise TypeError("qrcode no devolvió texto")
    return svg


def credential_aad(organization_id: uuid.UUID, user_id: uuid.UUID) -> bytes:
    """Dato asociado del cifrado: identifica la credencial (organización y usuario).

    Un texto cifrado copiado a la fila de otro usuario u otra organización no se descifra.
    """
    if not isinstance(organization_id, uuid.UUID) or not isinstance(user_id, uuid.UUID):
        raise TypeError("organization_id y user_id deben ser uuid.UUID")
    return _AAD_PREFIX + organization_id.bytes + user_id.bytes


def _base32(secret: bytes) -> str:
    return base64.b32encode(secret).decode("ascii").rstrip("=")


# --- Códigos de recuperación (funciones puras) -------------------------------------------------


def generate_recovery_codes(
    count: int = RECOVERY_CODE_COUNT, choice: Callable[[str], str] = secrets.choice
) -> tuple[str, ...]:
    """``count`` códigos distintos de 10 símbolos, mostrados como ``XXXXX-XXXXX``."""
    codes: dict[str, None] = {}
    while len(codes) < count:
        raw = "".join(choice(RECOVERY_CODE_ALPHABET) for _ in range(RECOVERY_CODE_LENGTH))
        codes[f"{raw[:5]}-{raw[5:]}"] = None
    return tuple(codes)


def normalize_recovery_code(code: object) -> str | None:
    """Forma canónica (10 símbolos en mayúsculas, sin guiones ni espacios ASCII) o ``None``.

    Acepta minúsculas y los separadores con que se copió; cualquier otro carácter (incluidos
    ``I``, ``L``, ``O``, ``U`` y los que no son ASCII) invalida el código sin calcular hashes.
    """
    if not isinstance(code, str) or len(code) > _RECOVERY_INPUT_MAX_CHARS or not code.isascii():
        return None
    canonical = _RECOVERY_SEPARATORS.sub("", code).upper()
    if _RECOVERY_CODE.fullmatch(canonical) is None:
        return None
    return canonical


def hash_recovery_code(code: str) -> str:
    """Argon2id (PHC) de la forma canónica de ``code`` (síncrono: en el pool de CPU)."""
    canonical = normalize_recovery_code(code)
    if canonical is None:
        raise ValueError("código de recuperación mal formado")
    return _RECOVERY_HASHER.hash(canonical)


def verify_recovery_code(canonical: str, encoded: str) -> bool:
    """``True`` si ``canonical`` corresponde al hash; un hash ilegible nunca verifica."""
    if not isinstance(encoded, str) or not encoded.startswith(_ARGON2ID_PREFIX):
        return False
    try:
        return _RECOVERY_HASHER.verify(encoded, canonical)
    except (argon2_exceptions.VerificationError, argon2_exceptions.InvalidHashError, ValueError):
        return False


# --- Servicio ----------------------------------------------------------------------------------


class SecondFactorService:
    """Inscripción, verificación y restablecimiento del segundo factor (LC-NUC-02)."""

    def __init__(
        self,
        store: SecondFactorStore,
        crypto: EnvelopeCrypto,
        cpu_pool: CpuPool,
        clock: Clock,
        *,
        random_bytes: Callable[[int], bytes] = os.urandom,
        new_id: Callable[[], uuid.UUID] | None = None,
    ) -> None:
        self._store = store
        self._crypto = crypto
        self._cpu_pool = cpu_pool
        self._clock = clock
        self._random_bytes = random_bytes
        self._new_id = new_id if new_id is not None else uuid.uuid4

    def __repr__(self) -> str:
        return "SecondFactorService()"

    async def enroll(self, context: ScopeContext, user: SecondFactorUser) -> EnrollmentChallenge:
        """Inscribe el segundo factor de ``user`` y devuelve lo que se le muestra una vez.

        Raises:
            SecretsUnavailable: KMS no responde (``temporarily_unavailable``); nada se guardó.
            AlreadyEnrolled: ya tiene una credencial activa.
            SecondFactorNotFound: ``user`` no es de la organización del contexto.
        """
        _require_context(context)
        if context.organization_id != user.organization_id:
            raise SecondFactorNotFound()
        existing = await self._store.get_credential(context, user.user_id)
        if existing is not None and existing.active:
            raise AlreadyEnrolled()
        secret = self._random_bytes(TOTP_SECRET_BYTES)
        if not isinstance(secret, bytes) or len(secret) != TOTP_SECRET_BYTES:
            raise ValueError("el generador no devolvió 20 bytes")
        uri = provisioning_uri(secret, user.account_label)
        sealed = await self._crypto.encrypt(
            secret, credential_aad(user.organization_id, user.user_id)
        )
        codes = generate_recovery_codes()
        hashes = await self._cpu_pool.run(_hash_all, codes)
        now = self._clock.now()
        credential = TotpCredential(
            user_id=user.user_id,
            organization_id=user.organization_id,
            secret_encrypted=sealed.ciphertext,
            data_key_wrapped=sealed.wrapped_key,
            enrolled_at=now,
        )
        records = tuple(
            RecoveryCodeRecord(
                recovery_code_id=self._new_id(),
                user_id=user.user_id,
                organization_id=user.organization_id,
                code_hash=code_hash,
                generated_at=now,
            )
            for code_hash in hashes
        )
        await self._store.save_enrollment(context, credential, records)
        svg = await self._cpu_pool.run(qr_svg, uri)
        return EnrollmentChallenge(credential, uri, svg, codes)

    async def verify_totp(
        self, context: ScopeContext, credential: TotpCredential, code: str, now: datetime
    ) -> bool:
        """``True`` si ``code`` acredita un paso nuevo dentro de ±1 paso y el almacén lo fijó.

        Un código mal formado o una credencial desactivada no llegan a descifrar nada.

        Raises:
            SecretsUnavailable: KMS no responde y la clave de datos no está en memoria.
            DecryptionFailed: la fila no se autentica (otra credencial o bytes alterados).
        """
        _require_context(context)
        if not credential.active or not isinstance(code, str) or not _TOTP_CODE.fullmatch(code):
            return False
        secret = await self._crypto.decrypt(
            credential.secret_encrypted,
            credential.data_key_wrapped,
            credential_aad(credential.organization_id, credential.user_id),
        )
        step = match_totp(secret, code, now, credential.last_accepted_step)
        if step is None:
            return False
        return await self._store.advance_step(context, credential.user_id, step)

    async def consume_recovery_code(
        self, context: ScopeContext, credential: TotpCredential, code: str
    ) -> bool:
        """``True`` si ``code`` es un código sin usar de la inscripción vigente; queda usado."""
        _require_context(context)
        canonical = normalize_recovery_code(code)
        if canonical is None or not credential.active:
            return False
        unused = await self._store.unused_recovery_codes(context, credential.user_id)
        for record in unused:
            if await self._cpu_pool.run(verify_recovery_code, canonical, record.code_hash):
                return await self._store.mark_recovery_code_used(
                    context, record.recovery_code_id, self._clock.now()
                )
        return False

    async def reset(self, admin_context: ScopeContext, user_id: uuid.UUID) -> int:
        """Restablece el segundo factor de ``user_id`` (BR-NUC-29); devuelve las sesiones cerradas.

        Raises:
            SecondFactorNotFound: el usuario no es de la organización del contexto.
            PermissionError: el actor no es una persona ni un operador.
        """
        _require_context(admin_context)
        if admin_context.actor.kind not in _RESET_ACTORS:
            raise PermissionError("solo una persona u operador restablece el segundo factor")
        if type(user_id) is not uuid.UUID:
            raise TypeError("user_id debe ser uuid.UUID")
        return await self._store.reset(admin_context, user_id, self._clock.now())


def _require_context(context: object) -> None:
    if not isinstance(context, ScopeContext):
        raise ContextAbsent()


def _hash_all(codes: Sequence[str]) -> tuple[str, ...]:
    return tuple(hash_recovery_code(code) for code in codes)
