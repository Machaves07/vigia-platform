"""Componer un paquete real sobre un mundo generado y leerlo de vuelta (TASK-318).

- ``compose_world(world, ports)``: ``ExportComposer`` con ``ExportStream`` sobre el almacén en
  memoria de TASK-345 (volúmenes pequeños, para que el paquete ocupe más de uno) y los puertos de
  ``WorldPorts``; devuelve el ``ExportPackage``, el manifiesto y **cada artefacto leído de los
  volúmenes ZIP** escritos, para comprobar el contenido real y no lo que el compositor cree haber
  escrito.
- ``oracle_findings(world, job)``: el oráculo por fuerza bruta de PR-LAZ-26: pliega **todos** los
  registros del mundo (las dos organizaciones) con ``fold`` y se queda con los hallazgos de la
  organización y las zonas pedidas cuyo primer registro de U-03 cae en el periodo.
- ``prose(document)``: cada texto de un documento JSON con su ruta, para comparar los avisos con
  la lista cerrada de textos fijos por **igualdad**.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Final

from tests.export_stream_support import MemoryExportStore, RecordingSink, read_volume
from tests.properties.laz.strategies.export import ExportWorld, WorldPorts
from vigia_platform.accreditation.export.composer_port import ExportJob, ExportPackage
from vigia_platform.accreditation.export.contents import ExportComposer
from vigia_platform.accreditation.export.stream import ExportStream, ExportStreamSettings
from vigia_platform.accreditation.export.volumes import MANIFEST_NAME, ExportLocation
from vigia_platform.accreditation.manifest.model import ManifestInputs
from vigia_platform.loop.projection.fold import U03_RECORD_TYPES, FindingStateRow, fold

__all__ = [
    "SMALL_VOLUMES",
    "Composed",
    "compose_world",
    "fake_document_renderer",
    "oracle_findings",
    "prose",
]

KIB: Final = 1024
SMALL_VOLUMES: Final = ExportStreamSettings(
    part_size=5 * 1024 * KIB, volume_max_bytes=48 * KIB, manifest_reserve_bytes=40 * KIB
)
"""Volúmenes pequeños: un paquete generado ocupa a menudo más de uno."""


def fake_document_renderer(inputs: ManifestInputs) -> bytes:
    """Un documento falso, determinista y rápido: ``accreditation.document.generate_document``
    de verdad llama a WeasyPrint (cientos de ms); con cientos de ejemplos por propiedad
    (``test_pr_laz_26_export_completeness.py`` y similares), eso convierte una prueba de
    segundos en una de minutos sin probar nada que ``test_export_document_determinism.py`` y
    ``test_export_document_no_active_content.py`` no prueben ya con el generador real. Mismo
    ``ManifestInputs`` (mismo ``export_id``), mismos bytes."""
    seed = str(inputs.export_id).encode("utf-8")
    return b"%PDF-1.7\n% documento falso de pruebas\n" + hashlib.sha256(seed).digest()


@dataclass(frozen=True)
class Composed:
    package: ExportPackage
    manifest: dict[str, Any]
    artifacts: dict[str, bytes]
    store: MemoryExportStore
    sink: RecordingSink

    def json(self, path: str) -> Any:
        return json.loads(self.artifacts[path])


def compose_world(
    world: ExportWorld,
    ports: WorldPorts | None = None,
    *,
    job: ExportJob | None = None,
    store: MemoryExportStore | None = None,
) -> Composed:
    ports = ports if ports is not None else WorldPorts(world)
    store = store if store is not None else MemoryExportStore()
    job = job if job is not None else world.job
    composer = ExportComposer(
        stream=ExportStream(store=store, settings=SMALL_VOLUMES),
        ports=ports.ports(),
        document_renderer=fake_document_renderer,
    )
    sink = RecordingSink()
    package = asyncio.run(composer.compose(job, sink))
    location = ExportLocation(job.organization_id, job.plant_id, job.export_id)
    artifacts: dict[str, bytes] = {}
    for volume in package.stored.volumes:
        archive = read_volume(store.latest(location.volume_key(volume.number)))
        for name in archive.namelist():
            assert name not in artifacts, f"entrada repetida entre volúmenes: {name}"
            artifacts[name] = archive.read(name)
    manifest = json.loads(artifacts.pop(MANIFEST_NAME))
    return Composed(package, manifest, artifacts, store, sink)


def oracle_findings(world: ExportWorld, job: ExportJob) -> dict[uuid.UUID, FindingStateRow]:
    """Los hallazgos que el paquete debe llevar, por fuerza bruta sobre todo el expediente."""
    items = world.items()
    first_u03: dict[uuid.UUID, Any] = {}
    for item in sorted(items, key=lambda entry: (str(entry.plant_id), entry.chain_sequence)):
        if item.record_type not in U03_RECORD_TYPES:
            continue
        subject = (
            uuid.UUID(item.document["episode"]["episode_id"])
            if item.record_type == "finding_received"
            else uuid.UUID(item.document["detection_id"])
        )
        first_u03.setdefault(subject, item)
    visible = [item for item in items if item.received_at < job.generated_at]
    projections = fold(visible)
    expected: dict[uuid.UUID, FindingStateRow] = {}
    for (organization_id, _), projection in projections.items():
        if organization_id != job.organization_id:
            continue
        for subject, row in projection.findings.items():
            anchor = first_u03[subject]
            if (
                row.zone_id in set(job.zone_ids)
                and job.period_from <= anchor.received_at < job.period_to
            ):
                expected[subject] = row
    return expected


def prose(document: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """Cada cadena del documento con su ruta (``/zones/0/name``…)."""
    if isinstance(document, dict):
        for key, value in document.items():
            yield from prose(value, f"{path}/{key}")
    elif isinstance(document, list):
        for index, value in enumerate(document):
            yield from prose(value, f"{path}/{index}")
    elif isinstance(document, str):
        yield path, document
