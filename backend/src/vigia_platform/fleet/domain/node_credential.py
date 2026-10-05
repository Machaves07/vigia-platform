"""``NodeCredential`` y su máquina de estados (DE §3.4 y nota de §4; BLM §3.5 y su nota; TASK-219).

Solo metadatos del certificado de cliente que firmó ``vigia-node-ca``: número de serie, sujeto,
vigencia, estado y de qué credencial rotó. La clave privada del nodo nunca llega a la plataforma
(BR-GOB-63, BR-CTR-46).

**Estados** (``credential_status``; transiciones que admite ``gob_0018``, solo hacia adelante):

- ``→ active``: alta aceptada o rotación pedida con la credencial vigente;
- ``active → overlapping``: se emitió la sucesora (rotación). Sigue autenticando **24 h** desde el
  ``issued_at`` de la sucesora (``OVERLAP``, el mismo valor que lee ``context_from_node``), sin
  columna nueva: la consulta de identidad lo deriva de ``rotated_from``;
- ``overlapping → superseded``: fuera de esas 24 h ya no autentica en la consulta de identidad; la
  fila se materializa **de forma perezosa** (``stale_overlapping``) en la siguiente rotación o
  re-alta, o en el barrido de la lista de revocación (TASK-220), sin tarea nueva. La nota de §4
  sustituye así el ``overlapping → revoked`` de BLM §3.5;
- ``active → revoked`` y ``overlapping → revoked``: la revocación y la re-alta (TASK-218).

``expired`` no es un estado: se deriva de ``expires_at`` (BR-GOB-64).

Módulo puro: sin FastAPI, sin SQLAlchemy y sin leer la hora del sistema.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from vigia_platform.fleet.domain.enums import CredentialStatus
from vigia_platform.fleet.domain.node_subject import NodeSubject
from vigia_platform.identity.authz.context import OVERLAP

__all__ = [
    "ALERT_BEFORE_DAYS",
    "KEY_ALGORITHM",
    "OVERLAP",
    "ROTATE_BEFORE_DAYS",
    "VALIDITY",
    "VALIDITY_DAYS",
    "NodeCredential",
    "stale_overlapping",
    "successors",
]

VALIDITY_DAYS: Final = 365
"""Vigencia del certificado de cliente (BR-GOB-64, NFR-CTR-13)."""
VALIDITY: Final = timedelta(days=VALIDITY_DAYS)
ROTATE_BEFORE_DAYS: Final = 30
"""El nodo pide la rotación 30 días antes del vencimiento (``CredentialPolicy``)."""
ALERT_BEFORE_DAYS: Final = 15
"""El inventario alerta si faltan menos de 15 días sin rotar (BR-GOB-65)."""
KEY_ALGORITHM: Final = "ecdsa_p256"
"""La única clave que admite ``node_credential_key_algorithm`` (NFR-CTR-13)."""

_SERIAL: Final = re.compile(r"[0-9a-f]{1,64}")
_LIVE: Final = frozenset({CredentialStatus.ACTIVE, CredentialStatus.OVERLAPPING})


@dataclass(frozen=True, slots=True, kw_only=True)
class NodeCredential:
    """``NodeCredential`` 🔒 (DE §3.4): una fila de ``fleet.node_credential``."""

    credential_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    node_id: uuid.UUID
    certificate_serial: str
    issued_at: datetime
    expires_at: datetime
    status: CredentialStatus = CredentialStatus.ACTIVE
    rotated_from: uuid.UUID | None = None
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("credential_id", "organization_id", "plant_id", "node_id"):
            if type(getattr(self, name)) is not uuid.UUID:
                raise TypeError(f"{name} debe ser uuid.UUID")
        object.__setattr__(self, "status", CredentialStatus(self.status))
        if not isinstance(self.certificate_serial, str) or not _SERIAL.fullmatch(
            self.certificate_serial
        ):
            raise ValueError("certificate_serial son de 1 a 64 hexadecimales en minúsculas")
        for name in ("issued_at", "expires_at"):
            value = getattr(self, name)
            if not isinstance(value, datetime) or value.utcoffset() is None:
                raise ValueError(f"{name} debe llevar zona horaria")
        if not self.expires_at > self.issued_at:
            raise ValueError("expires_at debe ser posterior a issued_at")
        if self.rotated_from == self.credential_id:
            raise ValueError("una credencial no rota desde sí misma")
        if (self.status is CredentialStatus.REVOKED) != (self.revoked_at is not None):
            raise ValueError("solo una credencial revocada lleva revoked_at, y siempre")

    @property
    def subject(self) -> NodeSubject:
        """El sujeto del certificado: sale del estado de la plataforma, nunca de la CSR."""
        return NodeSubject(self.node_id, self.organization_id, self.plant_id)

    def authenticates(self, now: datetime, successor_issued_at: datetime | None) -> bool:
        """¿Autentica en ``now``? La misma regla que ``context_from_node`` (PR-GOB-28).

        ``active`` dentro de su vigencia, u ``overlapping`` dentro de su vigencia y antes de que
        pasen ``OVERLAP`` desde el ``issued_at`` de su sucesora.
        """
        if self.status not in _LIVE or not self.issued_at <= now < self.expires_at:
            return False
        if self.status is CredentialStatus.ACTIVE:
            return True
        return successor_issued_at is not None and now < successor_issued_at + OVERLAP


def successors(credentials: Iterable[NodeCredential]) -> Mapping[uuid.UUID, datetime]:
    """El ``issued_at`` de la sucesora de cada credencial (la primera que rotó desde ella)."""
    found: dict[uuid.UUID, datetime] = {}
    for credential in credentials:
        previous = credential.rotated_from
        if previous is None:
            continue
        current = found.get(previous)
        if current is None or credential.issued_at < current:
            found[previous] = credential.issued_at
    return found


def stale_overlapping(
    credentials: Iterable[NodeCredential], now: datetime
) -> tuple[uuid.UUID, ...]:
    """Las ``overlapping`` que ya no autentican en ``now``: pasan a ``superseded``.

    Una ``overlapping`` dentro de sus 24 h se deja como está (sigue vaciando la cola del nodo).
    """
    listed = tuple(credentials)
    successor = successors(listed)
    return tuple(
        sorted(
            credential.credential_id
            for credential in listed
            if credential.status is CredentialStatus.OVERLAPPING
            and not credential.authenticates(now, successor.get(credential.credential_id))
        )
    )
