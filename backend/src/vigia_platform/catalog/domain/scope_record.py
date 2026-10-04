"""Acta de alcance de la compuerta de montaje (DE §2.6 y su nota D-2; BR-GOB-23, 24).

El instalador declara el alcance que se comunica a los trabajadores (``scope_text_es`` ≤ 4 000),
el encuadre de **cada** cámara de la zona (``framing_description_es`` ≤ 500 y si tiene marcador de
referencia de escena) y la verificación **declarada** del difuminado con su captura adjunta
(``capture_document_ref``). Desde la v1.5 (D-2) el acta no exige clip: la comprobación automática
del difuminado es la guarda de cierre del acta de comisionamiento (TASK-216).

Este módulo es puro: forma de la petición, guardas que no necesitan la base y el contenido del
registro ``mounting_gate_record``. Las guardas con la base (nodo asignado, cámaras de la zona,
documentos) las hace ``catalog.application.scope_record``.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from vigia_platform.catalog.domain.documents import DocumentRef
from vigia_platform.shared.context import Role
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "MAX_CAMERAS",
    "MAX_FRAMING_CHARS",
    "MAX_SCOPE_TEXT_CHARS",
    "CameraFraming",
    "MountingGateRecord",
    "ScopeRecordInvalid",
    "ScopeRecordRequest",
    "framings_match",
]

MAX_SCOPE_TEXT_CHARS: Final = 4000
MAX_FRAMING_CHARS: Final = 500
MAX_CAMERAS: Final = 8
"""Cámaras por zona (``ZoneCatalog``: 1 a 8; ``mounting_gate_record_cameras_shape``)."""


class ScopeRecordInvalid(Exception):
    """El acta incumple su forma (cámaras que no son las de la zona, tipos): ``invalid_request``."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"acta de alcance no válida: {reason}")


@dataclass(frozen=True, slots=True)
class CameraFraming:
    """Una fila del acta: el encuadre de una cámara de la zona."""

    camera_id: uuid.UUID
    framing_description_es: str
    reference_marker: bool

    def __post_init__(self) -> None:
        if type(self.camera_id) is not uuid.UUID:
            raise ScopeRecordInvalid("camera_id debe ser un UUID")
        if not isinstance(self.framing_description_es, str):
            raise ScopeRecordInvalid("framing_description_es debe ser texto")
        if type(self.reference_marker) is not bool:
            raise ScopeRecordInvalid("reference_marker debe ser booleano")

    def as_json(self) -> dict[str, Any]:
        return {
            "camera_id": str(self.camera_id),
            "framing_description_es": self.framing_description_es,
            "reference_marker": self.reference_marker,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ScopeRecordRequest:
    """El cuerpo de ``POST /zones/{zone_id}/gates/mounting/scope-record``.

    ``blur_declared`` y ``capture_document_ref`` pueden faltar: la guarda de la declaración los
    rechaza con ``blur_not_verified`` (no con ``invalid_request``).
    """

    scope_text_es: str
    cameras: tuple[CameraFraming, ...]
    blur_declared: bool | None = None
    capture_document_ref: Mapping[str, Any] | DocumentRef | None = None
    document_ref: Mapping[str, Any] | DocumentRef | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope_text_es, str):
            raise ScopeRecordInvalid("scope_text_es debe ser texto")
        received: object = self.cameras
        if isinstance(received, str | bytes) or not isinstance(received, Sequence):
            raise ScopeRecordInvalid("cameras debe ser una lista")
        cameras = tuple(received)
        if not all(isinstance(camera, CameraFraming) for camera in cameras):
            raise ScopeRecordInvalid("cameras debe ser una lista de encuadres")
        object.__setattr__(self, "cameras", cameras)
        if self.blur_declared is not None and type(self.blur_declared) is not bool:
            raise ScopeRecordInvalid("blur_verification.declared debe ser booleano")

    @property
    def blur_verified_by_declaration(self) -> bool:
        """BR-GOB-24 con D-2: la declaración (``declared: true``) **y** la captura adjunta."""
        return self.blur_declared is True and self.capture_document_ref is not None


def framings_match(framings: Sequence[CameraFraming], zone_cameras: Sequence[uuid.UUID]) -> bool:
    """Una fila por cada cámara de la zona, ni más ni menos, sin repetir (de 1 a 8)."""
    declared = [framing.camera_id for framing in framings]
    expected = set(zone_cameras)
    return (
        1 <= len(declared) <= MAX_CAMERAS
        and len(set(declared)) == len(declared)
        and set(declared) == expected
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class MountingGateRecord:
    """§2.6 ``MountingGateRecord`` ⛓: el acta tal como se firmó."""

    record_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    scope_text_es: str
    cameras: tuple[CameraFraming, ...]
    declared_by: uuid.UUID
    declared_at: datetime
    capture_document_ref: DocumentRef
    document_ref: DocumentRef | None
    signed_by: uuid.UUID
    role_in_use: Role
    plant_policy_loaded_at_signing: bool
    ledger_record_id: uuid.UUID | None = None

    def blur_verification(self) -> dict[str, Any]:
        """La parte declarada (D-2): quién, cuándo y la captura."""
        return {
            "declared_by": str(self.declared_by),
            "declared_at": format_timestamp(self.declared_at),
            "capture_document_ref": self.capture_document_ref.to_json(),
        }

    def record_content(self) -> dict[str, Any]:
        """Contenido de ``mounting_gate_record`` (``record_types.MountingGateRecord``)."""
        content: dict[str, Any] = {
            "record_id": str(self.record_id),
            "zone_id": str(self.zone_id),
            "scope_text_es": self.scope_text_es,
            "cameras": [camera.as_json() for camera in self.cameras],
            "blur_verification": self.blur_verification(),
            "signed_by": str(self.signed_by),
            "role_in_use": Role(self.role_in_use).value,
            "plant_policy_loaded_at_signing": self.plant_policy_loaded_at_signing,
        }
        if self.document_ref is not None:
            content["document_ref"] = self.document_ref.to_json()
        return content
