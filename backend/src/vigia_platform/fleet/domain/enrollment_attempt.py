"""Todo intento de alta, exitoso o no (domain-entities §3.3 y sus notas; BR-GOB-61, SEG-08).

``EnrollmentAttempt`` ⛓🔒: el resultado (``accepted`` o un ``rejection_code`` del contrato), la
huella de hardware que presentó el equipo, las versiones de la solicitud, el ``sha256`` del código
presentado (nunca el claro), el instante, el origen como **hash** y la correlación de la
plataforma (nunca la del cliente, BR-NUC-96). ``node_id`` y ``plant_id`` van juntos: un intento
con un ``node_id`` que no es de ningún nodo declarado no tiene organización identificable y no
deja fila (lo cuentan la métrica y el registro estructurado; nota del redactor de TASK-218).

``SourceIpHasher`` reduce la dirección de origen a un HMAC-SHA256 con una **clave estable de la
plataforma** (inyectada): el mismo origen da el mismo ``source_ip_hash`` en dos instancias y tras
reiniciar, a diferencia del secreto por proceso de ``shared.ratelimit.origin_key``.

Módulo puro: sin FastAPI, sin SQLAlchemy y sin leer la hora del sistema.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from vigia_platform.fleet.domain.enums import EnrollmentAttemptResult

__all__ = [
    "MIN_SOURCE_KEY_BYTES",
    "EnrollmentAttempt",
    "SourceIpHasher",
]

MIN_SOURCE_KEY_BYTES: Final = 32
_HEX64: Final = re.compile(r"[0-9a-f]{64}")
_SEMVER: Final = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(-(0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)(\.(0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(\+[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?"
)
"""``SemVer`` del contrato (la misma restricción que ``gob_0018``)."""
_MAX_ORIGIN_CHARS: Final = 64


@dataclass(frozen=True, slots=True, kw_only=True)
class EnrollmentAttempt:
    """``EnrollmentAttempt`` (DE §3.3)."""

    attempt_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID | None
    node_id: uuid.UUID | None
    presented_code_hash: str = field(repr=False)
    hardware_fingerprint: str
    software_version: str
    contract_version: str
    result: EnrollmentAttemptResult
    attempted_at: datetime
    source_ip_hash: str = field(repr=False)
    correlation_id: uuid.UUID
    ledger_record_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "result", EnrollmentAttemptResult(self.result))
        for name in ("presented_code_hash", "hardware_fingerprint", "source_ip_hash"):
            value = getattr(self, name)
            if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
                raise ValueError(f"{name} son 64 hexadecimales en minúsculas")
        for name in ("software_version", "contract_version"):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or not 5 <= len(value) <= 64
                or not _SEMVER.fullmatch(value)
            ):
                raise ValueError(f"{name} debe ser SemVer")
        if (self.node_id is None) != (self.plant_id is None):
            raise ValueError("node_id y plant_id van juntos")
        if self.result is EnrollmentAttemptResult.ACCEPTED and self.node_id is None:
            raise ValueError("un intento aceptado es de un nodo")
        if not isinstance(self.attempted_at, datetime) or self.attempted_at.utcoffset() is None:
            raise ValueError("attempted_at lleva zona horaria")

    @property
    def rejected(self) -> bool:
        return self.result is not EnrollmentAttemptResult.ACCEPTED


class SourceIpHasher:
    """``source_ip_hash``: HMAC-SHA256 de la dirección con una clave estable de la plataforma."""

    __slots__ = ("_key",)

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, bytes) or len(key) < MIN_SOURCE_KEY_BYTES:
            raise ValueError("la clave del hash de origen tiene al menos 32 bytes")
        self._key = key

    def __repr__(self) -> str:
        return "SourceIpHasher()"

    def hash(self, address: object) -> str:
        """El hash del origen; una dirección ausente o no válida cuenta como ``unknown``."""
        text = address.strip().lower() if isinstance(address, str) else ""
        if not text or len(text) > _MAX_ORIGIN_CHARS or not text.isascii():
            text = "unknown"
        return hmac.new(self._key, text.encode("ascii"), hashlib.sha256).hexdigest()
