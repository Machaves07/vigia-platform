"""Listas cerradas de la flota (domain-entities.md de U-03, §4 y sus notas).

Seis de las veintiuna enumeraciones que U-03 aporta a ``labels.platform.es.json``; las quince
del catálogo están en ``catalog.domain.enums``. Dentro de una versión mayor solo se **añaden**
valores. Cada valor tiene su etiqueta en español y el arranque falla si falta una
(``FLEET_LABEL_BINDINGS``, NFR-GOB-67).

``enrollment_attempt_result`` repite como valores de rechazo los ``rejection_code`` del contrato
(``enrollment_code_used``, ``enrollment_code_expired``, ``enrollment_code_invalid``,
``rate_limited``): es el resultado de un intento, no un código nuevo de rechazo.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping, MutableMapping
from typing import Final

__all__ = [
    "FLEET_LABEL_BINDINGS",
    "CredentialStatus",
    "EnrollmentAttemptResult",
    "EnrollmentCodeStatus",
    "FleetAlarmKind",
    "UpdateResult",
    "UploadGrantStatus",
    "register_fleet_label_bindings",
]


class EnrollmentCodeStatus(enum.StrEnum):
    """Código de alta de un solo uso; cada reemisión deja el anterior ``superseded``."""

    ACTIVE = "active"
    USED = "used"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class EnrollmentAttemptResult(enum.StrEnum):
    ACCEPTED = "accepted"
    ENROLLMENT_CODE_USED = "enrollment_code_used"
    ENROLLMENT_CODE_EXPIRED = "enrollment_code_expired"
    ENROLLMENT_CODE_INVALID = "enrollment_code_invalid"
    RATE_LIMITED = "rate_limited"


class CredentialStatus(enum.StrEnum):
    """Credencial del nodo; ``superseded`` por la nota de §4 (rotación o re-alta)."""

    ACTIVE = "active"
    OVERLAPPING = "overlapping"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"


class FleetAlarmKind(enum.StrEnum):
    """Las ocho clases de alarma de flota (manda sobre las listas de cinco y de siete)."""

    NODE_MUTE = "node_mute"
    QUEUE_OVER_THRESHOLD = "queue_over_threshold"
    CLOCK_DRIFT = "clock_drift"
    VERSION_RETIRING = "version_retiring"
    SIMULATED_ADAPTER_IN_PRODUCTIVE = "simulated_adapter_in_productive"
    CERTIFICATE_EXPIRING = "certificate_expiring"
    CAMERA_BELOW_MIN_FPS = "camera_below_min_fps"
    ORPHAN_CLIPS_GROWING = "orphan_clips_growing"


class UpdateResult(enum.StrEnum):
    """Proyección de ``update_outcome`` del contrato; ``failed`` por la nota de §3.12."""

    APPLIED = "applied"
    REVERTED = "reverted"
    FAILED = "failed"


class UploadGrantStatus(enum.StrEnum):
    """Concesión de subida de un clip o de un documento (D-13)."""

    ISSUED = "issued"
    USED = "used"
    EXPIRED = "expired"
    ORPHAN = "orphan"


FLEET_LABEL_BINDINGS: Final[Mapping[str, type[enum.Enum]]] = {
    "enrollment_code_status": EnrollmentCodeStatus,
    "enrollment_attempt_result": EnrollmentAttemptResult,
    "credential_status": CredentialStatus,
    "fleet_alarm_kind": FleetAlarmKind,
    "update_result": UpdateResult,
    "upload_grant_status": UploadGrantStatus,
}
"""Enumeración de ``labels.platform.es.json`` → lista cerrada del código (NFR-GOB-67)."""


def register_fleet_label_bindings(bindings: MutableMapping[str, type[enum.Enum]]) -> None:
    """Añade las enumeraciones de la flota a la comprobación de etiquetas del arranque."""
    clashes = sorted(
        name for name, kind in FLEET_LABEL_BINDINGS.items() if bindings.get(name, kind) is not kind
    )
    if clashes:
        raise ValueError(f"enumeraciones ya ligadas a otra lista: {', '.join(clashes)}")
    bindings.update(FLEET_LABEL_BINDINGS)
