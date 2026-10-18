"""Estado de una exportación y sus topes (TASK-317; DE §3.1 con U04-H-02, 03 y 08; BR-LAZ-87).

- ``export_state`` es el enumerado de §1 (``requested``, ``in_progress``, ``ready``, ``failed``,
  ``expired``), **sin** ``composing`` (U04-H-02). ``expired`` no se escribe: una solicitud
  ``ready`` cuya descarga venció (``expires_at`` ≤ ahora, con el ``Clock`` inyectado) **se lee**
  como ``expired`` (``effective_state``). El paquete no se borra nunca: lo que caduca es la
  descarga.
- Los topes de la solicitud (BR-LAZ-87 y nota de NFR Requirements sobre ``export_scope_too_large``,
  ``[estimaciones propias]``): de 1 a 20 zonas, a lo sumo 366 días y, en la forma con clips, a lo
  sumo 20 GB estimados sumando los ``size_bytes`` de las evidencias ya en el expediente.
- ``ExportStatus`` es la fila de ``accreditation.export_request`` (y, cuando está lista, el
  resumen de su ``export_package``) tal como la leen las rutas.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from vigia_platform.loop.domain.enums import ExportState, PackageForm

__all__ = [
    "DOWNLOAD_VALIDITY",
    "MAX_ESTIMATED_BYTES",
    "MAX_PERIOD",
    "MAX_ZONES",
    "TERMINAL_STATES",
    "ExportStatus",
    "PackageSummary",
    "effective_state",
]

MAX_ZONES: Final = 20
"""Zonas por exportación (BR-LAZ-87, ``[estimación propia]``)."""
MAX_PERIOD: Final = timedelta(days=366)
"""Periodo máximo (BR-LAZ-87, ``[estimación propia]``); la restricción de laz_0031 es la misma."""
MAX_ESTIMATED_BYTES: Final = 20 * 1000**3
"""20 GB estimados para la forma con clips (NFR-LAZ-10, ``[estimación propia]``)."""
DOWNLOAD_VALIDITY: Final = timedelta(days=30)
"""La descarga caduca a los 30 días; el paquete no se borra (LC-LAZ-20, ``[estimación propia]``)."""

TERMINAL_STATES: Final = frozenset({ExportState.READY, ExportState.FAILED, ExportState.EXPIRED})
"""Una solicitud en uno de estos estados ya no se compone."""


def effective_state(state: ExportState, expires_at: datetime, now: datetime) -> ExportState:
    """El estado que se muestra: ``ready`` con la descarga vencida se lee ``expired``."""
    if state is ExportState.READY and expires_at <= now:
        return ExportState.EXPIRED
    return state


@dataclass(frozen=True, slots=True, kw_only=True)
class PackageSummary:
    """Resumen del manifiesto de un paquete listo (``export_package``)."""

    document_sha256: str
    archive_sha256: str
    size_bytes: int
    artifact_count: int
    checkpoint_count: int
    verifier_version: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ExportStatus:
    """Una solicitud de exportación tal como la leen las rutas (``SUP-U05-21`` y ``22``)."""

    export_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_ids: tuple[uuid.UUID, ...]
    period_from: datetime
    period_to: datetime
    package_form: PackageForm
    state: ExportState
    """El estado guardado; ``effective_state`` da el que se muestra."""
    progress_percent: int
    current_artifact: str | None
    attempt_count: int
    failure_code: str | None
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    expires_at: datetime
    correlation_id: uuid.UUID
    package: PackageSummary | None = None

    def shown_state(self, now: datetime) -> ExportState:
        return effective_state(self.state, self.expires_at, now)

    @classmethod
    def from_row(cls, row: Mapping[str, Any], package: PackageSummary | None) -> ExportStatus:
        return cls(
            export_id=_uuid(row["export_id"]),
            organization_id=_uuid(row["organization_id"]),
            plant_id=_uuid(row["plant_id"]),
            zone_ids=tuple(_uuid(zone) for zone in row["zone_ids"]),
            period_from=row["period_from"],
            period_to=row["period_to"],
            package_form=PackageForm(row["package_form"]),
            state=ExportState(row["state"]),
            progress_percent=int(row["progress_percent"]),
            current_artifact=row["current_artifact"],
            attempt_count=int(row["attempt_count"]),
            failure_code=row["failure_code"],
            requested_at=row["requested_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            expires_at=row["expires_at"],
            correlation_id=_uuid(row["correlation_id"]),
            package=package,
        )


def _uuid(value: object) -> uuid.UUID:
    """asyncpg devuelve su propio tipo de UUID; el dominio usa ``uuid.UUID``."""
    return uuid.UUID(str(value))
