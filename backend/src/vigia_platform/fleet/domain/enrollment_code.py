"""Código de alta de un solo uso (domain-entities §3.2 y sus notas del 2026-09-23; BLM §3.4).

El código es el único secreto que protege la única operación del contrato sin certificado
(BR-GOB-58, BR-CTR-45):

- **12 caracteres** del alfabeto de 32 símbolos sin ambiguos ``ALPHABET`` (sin 0, O, 1 ni I;
  60 bits), generados con ``secrets.choice`` (``generate_code``);
- **sal de 16 bytes** por código (``secrets.token_bytes``) y solo se guarda
  ``code_hash = sha256(sal || código)`` (``salted_hash``): el claro se muestra **una vez** en la
  respuesta 201 y nunca se persiste (BR-GOB-59, NFR-GOB-31);
- válido **24 horas**: ``expires_at = issued_at + VALIDITY``.

**Máquina** (BLM §3.4 y su nota): ``→ active`` con el nodo ``declared`` o en re-alta;
``active → superseded`` al emitir otro para el mismo nodo (a lo sumo uno ``active`` por nodo,
BR-GOB-60); ``active → used`` solo por el consumo del alta (VIG-151); ``active → expired`` es
**derivado** de ``now ≥ expires_at`` (``effective_status``): la verificación lo aplica aunque la
tarea ``expire_enrollment_codes`` (VIG-163) aún no lo haya escrito.

**Verificación** (nota U03-H-13 del coordinador): el código presentado se compara en **tiempo
constante** (``hmac.compare_digest`` sobre ``sha256(sal || presentado)``) contra **todos** los
códigos de ese nodo, sin salir al primer acierto (``check_presented``): válido, ``used``,
``expired`` o ``invalid`` (también ``superseded`` y desconocido, para no revelar que existió).

El intento guarda ``presented_code_hash`` (``sha256`` del presentado, sin sal: nunca el claro).
Módulo puro: sin FastAPI, sin SQLAlchemy y sin leer la hora del sistema.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from vigia_platform.fleet.domain.enums import EnrollmentAttemptResult, EnrollmentCodeStatus

__all__ = [
    "ALPHABET",
    "CODE_LENGTH",
    "SALT_BYTES",
    "VALIDITY",
    "CodeCheck",
    "EnrollmentCode",
    "check_presented",
    "generate_code",
    "new_salt",
    "presented_code_hash",
    "salted_hash",
]

ALPHABET: Final = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
"""Los 32 símbolos sin ambiguos (sin 0, O, 1 ni I): 5 bits por carácter."""
CODE_LENGTH: Final = 12
"""12 caracteres: 60 bits (BR-GOB-58)."""
SALT_BYTES: Final = 16
VALIDITY: Final = timedelta(hours=24)
"""Vigencia desde la emisión (BR-CTR-45)."""
CODE_SHAPE: Final = re.compile(r"[A-HJ-NP-Z2-9]{12}")
"""El ``EnrollmentCode`` del contrato (``^[A-HJ-NP-Z2-9]{12}$``)."""
_HEX64: Final = re.compile(r"[0-9a-f]{64}")


def generate_code(choice: Callable[[str], str] = secrets.choice) -> str:
    """Un código nuevo de ``CODE_LENGTH`` símbolos de ``ALPHABET``."""
    code = "".join(choice(ALPHABET) for _ in range(CODE_LENGTH))
    if CODE_SHAPE.fullmatch(code) is None:  # un generador inyectado fuera del alfabeto
        raise ValueError("el generador devolvió un símbolo fuera del alfabeto")
    return code


def new_salt(token_bytes: Callable[[int], bytes] = secrets.token_bytes) -> bytes:
    """La sal de ``SALT_BYTES`` bytes de un código."""
    salt = token_bytes(SALT_BYTES)
    if not isinstance(salt, bytes) or len(salt) != SALT_BYTES:
        raise ValueError("la sal debe tener 16 bytes")
    return salt


def salted_hash(salt: bytes, code: str) -> str:
    """``sha256(sal || código)`` en hexadecimal: lo único que se guarda del código."""
    return hashlib.sha256(salt + code.encode("utf-8")).hexdigest()


def presented_code_hash(presented: str) -> str:
    """``sha256`` del código presentado en un intento (nunca el claro, DE §3.3)."""
    return hashlib.sha256(presented.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class EnrollmentCode:
    """``EnrollmentCode`` 🔒 tal como se guarda: sin el código en claro."""

    code_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    node_id: uuid.UUID
    code_hash: str = field(repr=False)
    code_salt: bytes = field(repr=False)
    issued_at: datetime
    issued_by: uuid.UUID
    expires_at: datetime
    disclosed_at: datetime
    status: EnrollmentCodeStatus
    ledger_record_id: uuid.UUID

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", EnrollmentCodeStatus(self.status))
        if not isinstance(self.code_hash, str) or _HEX64.fullmatch(self.code_hash) is None:
            raise ValueError("code_hash debe ser un SHA-256 en hexadecimal")
        if not isinstance(self.code_salt, bytes) or len(self.code_salt) != SALT_BYTES:
            raise ValueError("code_salt debe tener 16 bytes")
        for moment in (self.issued_at, self.expires_at, self.disclosed_at):
            if not isinstance(moment, datetime) or moment.utcoffset() is None:
                raise ValueError("las marcas del código llevan zona horaria")
        if self.expires_at != self.issued_at + VALIDITY:
            raise ValueError("expires_at es issued_at + 24 h")
        if self.disclosed_at < self.issued_at:
            raise ValueError("disclosed_at no es anterior a issued_at")

    def effective_status(self, now: datetime) -> EnrollmentCodeStatus:
        """El estado en ``now``: un ``active`` vencido es ``expired`` (derivado)."""
        if self.status is EnrollmentCodeStatus.ACTIVE and now >= self.expires_at:
            return EnrollmentCodeStatus.EXPIRED
        return self.status

    def matches(self, presented: str) -> bool:
        """¿Es ``presented`` este código? Comparación en tiempo constante."""
        return hmac.compare_digest(
            salted_hash(self.code_salt, presented).encode("ascii"), self.code_hash.encode("ascii")
        )


@dataclass(frozen=True, slots=True)
class CodeCheck:
    """Resultado de comparar un código presentado con los de su nodo.

    ``result`` es ``accepted`` si el código es el ``active`` vigente (``code`` es entonces ese
    código, el que consume el alta) o el ``rejection_code`` del contrato.
    """

    result: EnrollmentAttemptResult
    code: EnrollmentCode | None = None

    @property
    def valid(self) -> bool:
        return self.result is EnrollmentAttemptResult.ACCEPTED


_REJECTION_OF: Final = {
    EnrollmentCodeStatus.USED: EnrollmentAttemptResult.ENROLLMENT_CODE_USED,
    EnrollmentCodeStatus.EXPIRED: EnrollmentAttemptResult.ENROLLMENT_CODE_EXPIRED,
    EnrollmentCodeStatus.SUPERSEDED: EnrollmentAttemptResult.ENROLLMENT_CODE_INVALID,
}


def check_presented(codes: Iterable[EnrollmentCode], presented: object, now: datetime) -> CodeCheck:
    """Compara ``presented`` con **todos** ``codes`` (los del nodo) sin salir al primer acierto.

    Un ``superseded`` responde como uno desconocido (``enrollment_code_invalid``): nunca se acepta
    un código ``used``, ``expired`` o ``superseded`` (BR-GOB-58, 60).
    """
    text = presented if isinstance(presented, str) else ""
    shaped = CODE_SHAPE.fullmatch(text) is not None
    found: EnrollmentCode | None = None
    for code in codes:
        # Sin cortocircuito: se calcula y compara con cada código del conjunto acotado.
        if code.matches(text) and shaped:
            found = code
    if found is None:
        return CodeCheck(EnrollmentAttemptResult.ENROLLMENT_CODE_INVALID)
    status = found.effective_status(now)
    if status is EnrollmentCodeStatus.ACTIVE:
        return CodeCheck(EnrollmentAttemptResult.ACCEPTED, found)
    return CodeCheck(_REJECTION_OF[status])
