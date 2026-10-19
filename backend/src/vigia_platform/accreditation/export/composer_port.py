"""Puertos de la composición de una exportación (TASK-317; LC-LAZ-21 y 22; PAT-LAZ-RES-01).

El consumidor ``export_worker`` (``export.worker``) orquesta; **no** sabe qué lleva el paquete ni
cómo se escriben sus bytes. Lo recibe por puerto:

- ``PackageComposer.compose(job, sink) -> ExportPackage``: compone el paquete **entero y desde el
  principio** a partir de ``ExportJob`` (el alcance, ``generated_at`` y los puntos de control ya
  fijados en el primer intento, U04-H-08) y avisa al ``ExportSink`` de lo que hace. Es
  determinista: con el mismo ``ExportJob`` devuelve el mismo paquete (PR-LAZ-49, que TASK-345 y
  TASK-318 comprueban sobre bytes reales). Si falta un insumo obligatorio lanza
  ``ExportInputUnavailable`` con su código, y la solicitud pasa a ``failed`` **sin paquete**
  (BR-LAZ-101). El compositor real es de TASK-318; hasta entonces la raíz instala
  ``MissingPackageComposer``, que falla sin publicar nada (nunca un paquete inventado).
- ``ExportSink``: lo que el compositor cuenta mientras trabaja: la subida por partes que abrió
  (``upload_started``, para poder abortarla al retomar), el artefacto en curso y cada artefacto
  terminado (el progreso, publicado al terminar cada uno, NFR-LAZ-12).
- ``UploadPort.abort_previous_upload(location, multipart_upload_id)``: abandona la subida por
  partes de un intento anterior (la guardada, por su identificador, y las demás sin terminar de
  esa exportación) antes de componer de nuevo, para que no queden fragmentos pagándose. Lo
  implementa ``export.stream.ExportStream`` (TASK-345); ``MissingUploadPort`` queda para los
  procesos sin depósito de evidencias.

Ninguno de estos puertos publica un objeto como paquete: el paquete existe solo cuando el
consumidor escribe ``export_package`` y pasa la solicitud a ``ready`` en la misma transacción.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Protocol

from vigia_platform.accreditation.detail_codes import AccreditationDetailCode
from vigia_platform.accreditation.export.volumes import ExportLocation, StoredObjects
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.domain.enums import PackageForm

__all__ = [
    "DECLARED_FAILURE_CODES",
    "ExportInputUnavailable",
    "ExportJob",
    "ExportPackage",
    "ExportSink",
    "MissingPackageComposer",
    "MissingUploadPort",
    "PackageComposer",
    "PackageComposerMissing",
    "UploadPort",
    "UploadPortMissing",
]

DECLARED_FAILURE_CODES: Final = frozenset(
    {
        AccreditationDetailCode.COVERAGE_UNAVAILABLE.value,
        LoopDetailCode.STANDARD_TEXT_UNAVAILABLE.value,
        LoopDetailCode.GATE_STATE_UNAVAILABLE.value,
    }
)
"""Insumos obligatorios cuya falta declara la exportación ``failed`` (BR-LAZ-101; SUP-U05-23)."""


class ExportInputUnavailable(Exception):
    """Falta un insumo obligatorio: la exportación falla declarada, sin paquete (BR-LAZ-101)."""

    def __init__(self, failure_code: str) -> None:
        if failure_code not in DECLARED_FAILURE_CODES:
            raise ValueError(f"código de fallo declarado desconocido: {failure_code!r}")
        self.failure_code = failure_code
        super().__init__(failure_code)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExportJob:
    """Lo que el compositor recibe: el alcance y lo fijado en el primer intento (U04-H-08)."""

    export_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_ids: tuple[uuid.UUID, ...]
    period_from: datetime
    period_to: datetime
    package_form: PackageForm
    requested_by: Mapping[str, Any]
    correlation_id: uuid.UUID
    attempt: int
    """Número de intento (1 en el primero): solo para la traza; el paquete no depende de él."""
    generated_at: datetime
    """``generated_at`` del manifiesto, fijado en el primer intento."""
    checkpoint_record_ids: tuple[uuid.UUID, ...]
    """Puntos de control previos, tomados en el primer intento."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ExportPackage:
    """El resultado de una composición completa (DE §3.1); los puntos de control, del trabajo."""

    document_sha256: str
    archive_sha256: str
    manifest: Mapping[str, Any] = field(default_factory=dict)
    size_bytes: int
    artifact_count: int
    verifier_version: str
    stored: StoredObjects = field(default_factory=StoredObjects)
    """Los volúmenes y el documento escritos (``ArchiveResult.stored``): lo que la descarga sirve.
    Vacío si el compositor no escribió bytes (el sintético de TASK-317)."""


class ExportSink(Protocol):
    """Lo que el compositor cuenta al consumidor mientras compone."""

    async def upload_started(self, multipart_upload_id: str) -> None:
        """Abrió una subida por partes: se guarda para abortarla si hay que retomar."""
        ...

    async def artifact_started(self, artifact: str) -> None:
        """Empieza ``artifact`` (≤ 128 caracteres, sin texto libre): ``current_artifact``."""
        ...

    async def artifact_finished(self, artifact: str, done: int, total: int) -> None:
        """Terminó ``artifact``: ``done`` de ``total``; publica el progreso."""
        ...


class PackageComposer(Protocol):
    """Compone el paquete entero desde el principio (determinista)."""

    async def compose(self, job: ExportJob, sink: ExportSink) -> ExportPackage: ...


class UploadPort(Protocol):
    """Lo que el consumidor pide al flujo de bytes (TASK-345) al retomar."""

    async def abort_previous_upload(
        self, location: ExportLocation, multipart_upload_id: str | None
    ) -> None: ...


class PackageComposerMissing(Exception):
    """La raíz no tiene compositor real todavía (TASK-318): no se compone nada."""

    code: Final = "package_composer_missing"


class UploadPortMissing(Exception):
    """El proceso no tiene el flujo de bytes (sin depósito de evidencias)."""

    code: Final = "upload_port_missing"


class MissingPackageComposer:
    """El compositor de la raíz hasta TASK-318: falla sin publicar nada ni inventar un paquete."""

    async def compose(self, job: ExportJob, sink: ExportSink) -> ExportPackage:
        raise PackageComposerMissing()


class MissingUploadPort:
    """El flujo de un proceso sin depósito de evidencias: falla sin tocar nada."""

    async def abort_previous_upload(
        self, location: ExportLocation, multipart_upload_id: str | None
    ) -> None:
        raise UploadPortMissing()
