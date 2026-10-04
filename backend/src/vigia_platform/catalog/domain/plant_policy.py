"""Política de hallazgos incerrables de la planta (DE §2.10; BR-GOB-21, 22; H-26).

Es **por planta** y la firma el cliente (``signed_by_display_name`` no es un usuario de la
plataforma). La versión es monótona por planta y la fija el servidor como la anterior más uno: el
cuerpo trae la que espera y, si no coincide, ``conflict`` sin escribir nada. Una versión nueva no
borra la anterior; la vigente es la última cargada. Solo bloquea la compuerta de **uso**
(``plant_policy_missing`` al aprobar el acuerdo, TASK-212); el acta de alcance solo deja
constancia (``plant_policy_loaded_at_signing``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from vigia_platform.catalog.domain.documents import DocumentRef
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "MAX_CRITERIA_SUMMARY_CHARS",
    "MAX_DISPLAY_NAME_CHARS",
    "MAX_LEGAL_REFERENCE_CHARS",
    "MAX_POLICY_VERSION",
    "PlantPolicy",
    "PlantPolicyRequest",
    "next_version",
]

MAX_DISPLAY_NAME_CHARS: Final = 120
MAX_LEGAL_REFERENCE_CHARS: Final = 120
MAX_CRITERIA_SUMMARY_CHARS: Final = 2000
MAX_POLICY_VERSION: Final = 2**31 - 1
"""``integer`` de la base y del esquema de ``plant_policy_signed``."""


def next_version(latest: int | None) -> int:
    """La versión que toca: 1 en una planta sin política, si no la última más uno."""
    version = 1 if latest is None else latest + 1
    if version > MAX_POLICY_VERSION:
        raise ValueError("la planta agotó las versiones de su política")
    return version


@dataclass(frozen=True, slots=True, kw_only=True)
class PlantPolicyRequest:
    """El cuerpo de ``POST /plants/{plant_id}/policy`` (textos aún sin la política)."""

    version: int
    signed_at: datetime
    signed_by_display_name: str
    legal_opinion_reference: str
    criteria_summary_es: str
    document_ref: Any


@dataclass(frozen=True, slots=True, kw_only=True)
class PlantPolicy:
    """§2.10 ``PlantPolicy`` ⛓."""

    policy_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    version: int
    signed_at: datetime
    signed_by_display_name: str
    legal_opinion_reference: str
    criteria_summary_es: str
    document_ref: DocumentRef
    loaded_by: uuid.UUID
    loaded_at: datetime
    ledger_record_id: uuid.UUID

    def record_content(self) -> dict[str, Any]:
        """Contenido de ``plant_policy_signed`` (``record_types.PlantPolicySigned``)."""
        return {
            "policy_id": str(self.policy_id),
            "plant_id": str(self.plant_id),
            "version": self.version,
            "signed_at": format_timestamp(self.signed_at),
            "signed_by_display_name": self.signed_by_display_name,
            "legal_opinion_reference": self.legal_opinion_reference,
            "criteria_summary_es": self.criteria_summary_es,
            "document_ref": self.document_ref.to_json(),
        }
