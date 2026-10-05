"""El nodo desde el lado de la flota (domain-entities §3.1; BR-GOB-57, 60, 66 a 68 y sus notas).

``NodeFleetRecord`` 🔒 es el espejo de ``NodeIdentity`` de U-02, que sigue siendo la identidad
autoritativa: U-03 declara, asigna, reemplaza y cambia el estado **solo** por
``IdentityCommandPort`` y aquí guarda lo propio de la flota (reemplazo, huella de hardware del
alta, revocación con su motivo, baja y URL local de la vista en vivo). La baja se modela con
``decommissioned_at`` sobre un nodo ya revocado (D-14): nada se borra.

**Emisión de un código de alta** (``code_eligibility``; nota U03-H-04 de BR §6, BLM §3.4,
«Versión 1.5» de interfaces y su precisión (e)):

- nodo ``declared`` o ``re_enrollment_pending``: **reemisión** libre, sin cambiar el nodo;
- **re-alta** del mismo ``node_id``: nodo ``revoked`` por revocación deliberada, o ``enrolled`` sin
  credencial viva (la vigente está vencida, derivado de ``expires_at``, revocada o
  ``superseded``): el nodo pasa a ``re_enrollment_pending`` y la credencial que siga
  ``active``/``overlapping`` se revoca;
- cualquier otro caso (con baja, o ``enrolled`` con una credencial viva: eso es la rotación) →
  ``node_not_declared``.

Módulo puro: sin FastAPI, sin SQLAlchemy y sin leer la hora del sistema.
"""

from __future__ import annotations

import enum
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from vigia_platform.fleet.domain.enums import CredentialStatus

__all__ = [
    "REASON_MAX_CHARS",
    "REASON_MIN_CHARS",
    "CodeEligibility",
    "CredentialState",
    "FleetNode",
    "NodeFleetRecord",
    "code_eligibility",
]

REASON_MIN_CHARS: Final = 10
REASON_MAX_CHARS: Final = 500
"""``revocation_reason_es`` y ``reason_es``: 10 a 500 `[estimación propia]`."""
_HEX64: Final = re.compile(r"[0-9a-f]{64}")
_LIVE: Final = frozenset({CredentialStatus.ACTIVE, CredentialStatus.OVERLAPPING})


@dataclass(frozen=True, slots=True, kw_only=True)
class NodeFleetRecord:
    """``NodeFleetRecord`` 🔒 (DE §3.1)."""

    node_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    replaces_node_id: uuid.UUID | None
    hardware_fingerprint: str | None
    declared_at: datetime
    declared_by: uuid.UUID
    enrolled_at: datetime | None = None
    revoked_at: datetime | None = None
    revocation_reason_es: str | None = None
    decommissioned_at: datetime | None = None
    live_view_local_url: str | None = None

    def __post_init__(self) -> None:
        if self.replaces_node_id == self.node_id:
            raise ValueError("un nodo no se reemplaza a sí mismo")
        if self.hardware_fingerprint is not None and (
            not isinstance(self.hardware_fingerprint, str)
            or _HEX64.fullmatch(self.hardware_fingerprint) is None
        ):
            raise ValueError("hardware_fingerprint son 64 hexadecimales en minúsculas")
        if (self.revoked_at is None) != (self.revocation_reason_es is None):
            raise ValueError("la revocación lleva su fecha y su motivo")
        if self.decommissioned_at is not None and (
            self.revoked_at is None or self.decommissioned_at < self.revoked_at
        ):
            raise ValueError("la baja exige el nodo revocado antes (D-14)")

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def decommissioned(self) -> bool:
        return self.decommissioned_at is not None


@dataclass(frozen=True, slots=True)
class CredentialState:
    """Lo que la emisión de un código necesita de cada ``NodeCredential`` del nodo."""

    credential_id: uuid.UUID
    status: CredentialStatus
    expires_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", CredentialStatus(self.status))

    def live(self, now: datetime) -> bool:
        """¿Autentica todavía? ``active``/``overlapping`` y no vencida (``expired`` derivado)."""
        return self.status in _LIVE and now < self.expires_at

    @property
    def revocable(self) -> bool:
        """La re-alta y la revocación revocan las que siguen ``active``/``overlapping``."""
        return self.status in _LIVE


@dataclass(frozen=True, slots=True)
class FleetNode:
    """Un nodo de la organización del contexto: su identidad de U-02 y su ficha de flota."""

    node_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    code: str
    status: str
    """``NodeIdentity.status`` de U-02 (``declared``, ``enrolled``, ``revoked``,
    ``re_enrollment_pending``)."""
    live_view_local_url: str | None
    record: NodeFleetRecord


class CodeEligibility(enum.Enum):
    """Qué hace la emisión de un código de alta sobre el nodo."""

    REISSUE = "reissue"
    RE_ENROLLMENT = "re_enrollment"
    REJECTED = "rejected"


def code_eligibility(
    node_status: str,
    record: NodeFleetRecord,
    credentials: Iterable[CredentialState],
    now: datetime,
) -> CodeEligibility:
    """La regla de emisión de BR-GOB-60 con su nota de re-alta (ver el módulo)."""
    if record.decommissioned:
        return CodeEligibility.REJECTED
    if node_status in ("declared", "re_enrollment_pending"):
        return CodeEligibility.REISSUE
    if node_status == "revoked":
        return CodeEligibility.RE_ENROLLMENT
    if node_status == "enrolled" and not any(state.live(now) for state in credentials):
        return CodeEligibility.RE_ENROLLMENT
    return CodeEligibility.REJECTED
