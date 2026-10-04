"""``ZoneCamera``: la cámara de una zona tal como la declara U-03 (nota de DE §3.14, U03-H-03).

``{camera_id, role_in_zone, declared_min_fps, stream_reference}`` más el ``code`` legible que el
contrato exige en ``ZoneCatalog.cameras`` (U-02 no tiene tabla de cámaras: el código vive en el
``payload`` firmado de cada versión). ``role_in_zone`` y ``declared_min_fps`` alimentan
``ZoneCatalog.cameras``; ``stream_reference`` alimenta la configuración inicial del nodo y **nunca**
entra en el catálogo firmado.

``declared_min_fps`` es lo que la planta declaró, de 1 a 60: U-03 nunca lo completa ni lo ajusta
con la tasa medida, que vive en el inventario y en el acta (BR-GOB-12, RNF-DES-06).
"""

from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass
from typing import Final

from vigia_contracts.models.enumerations import CameraRoleInZone

__all__ = [
    "MAX_CAMERAS",
    "MAX_FPS",
    "MIN_FPS",
    "STREAM_REFERENCE_PATTERN",
    "ZoneCamera",
]

MAX_CAMERAS: Final = 8
"""Cámaras por zona (``ZoneCatalog.cameras``: 1 a 8; BR-GOB-12)."""
MIN_FPS: Final = 1.0
MAX_FPS: Final = 60.0
"""``declared_min_fps`` entre 1 y 60 cuadros por segundo (contrato, BR-GOB-12)."""
STREAM_REFERENCE_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
"""Identificador técnico de la fuente de video (la misma restricción que ``zone_camera``)."""
_CODE: Final = re.compile(r"^[A-Z0-9-]{2,32}$")
"""``Code`` del contrato."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ZoneCamera:
    """Una cámara declarada en la zona."""

    camera_id: uuid.UUID
    code: str
    role_in_zone: CameraRoleInZone
    declared_min_fps: float
    stream_reference: str

    def __post_init__(self) -> None:
        if type(self.camera_id) is not uuid.UUID:
            raise TypeError("camera_id debe ser uuid.UUID")
        if not isinstance(self.code, str) or not _CODE.fullmatch(self.code):
            raise ValueError("code debe ser un código legible del contrato")
        object.__setattr__(self, "role_in_zone", CameraRoleInZone(self.role_in_zone))
        fps = self.declared_min_fps
        if type(fps) not in (int, float) or not math.isfinite(fps):
            raise ValueError("declared_min_fps debe ser un número finito")
        if not MIN_FPS <= fps <= MAX_FPS:
            raise ValueError("declared_min_fps debe estar entre 1 y 60")
        object.__setattr__(self, "declared_min_fps", float(fps))
        if not isinstance(self.stream_reference, str) or not STREAM_REFERENCE_PATTERN.fullmatch(
            self.stream_reference
        ):
            raise ValueError("stream_reference debe ser un identificador técnico")

    def catalog_camera(self) -> dict[str, object]:
        """La cámara como la lleva ``ZoneCatalog.cameras``: sin ``stream_reference``."""
        return {
            "camera_id": str(self.camera_id),
            "code": self.code,
            "role_in_zone": self.role_in_zone.value,
            "declared_min_fps": self.declared_min_fps,
        }
