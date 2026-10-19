"""Compositor real del paquete exportado (TASK-318; LC-LAZ-21 a 24; business-logic-model §6;
BR-LAZ-88 a 101; PAT-LAZ-REN-04 y 05; NFR-LAZ-03).

``ExportComposer`` es el ``PackageComposer`` de producción. Trabaja en **dos fases**:

1. **Recoger** (``collect``) todo lo que el paquete lleva, **antes de abrir ninguna subida**: si
   falta un insumo obligatorio la exportación falla declarada (``ExportInputUnavailable``) sin que
   exista ni una subida por partes (BR-LAZ-101). Los insumos:

   - **Lugares**: la organización (una sola, BR-LAZ-88), la planta y sus zonas pedidas.
   - **Puntos de control** fijados en el primer intento (``checkpoint_record_ids``) y **claves
     públicas** ``checkpoint`` vigentes y retiradas de U-02. Si falta uno, o no hay clave para la
     firma de uno, fallo cerrado con ``checkpoint_unavailable`` (BR-LAZ-91).
   - **U-03 solo por lote y por rango** (PAT-LAZ-REN-05, NFR-LAZ-03): ``single_occupancy_many``
     (una llamada, hasta 50 zonas), ``gate_history`` y ``assignment_history`` (una por zona, el
     periodo entero: hasta 366 días) y ``standards_at_many`` (hasta 200 referencias por llamada).
     Ninguna llamada dentro de un bucle sobre hallazgos: para 20 zonas, 42 llamadas más una por
     cada 200 estándares citados. Sin compuerta, ``gate_state_unavailable``; sin el texto de un
     estándar citado (o con otra versión que la citada), ``standard_text_unavailable``; sin la
     marca unipersonal, se asume unipersonal con la ventana por defecto (fallo cerrado hacia la
     privacidad, SUP-U03-04); sin la historia de asignación, se declara ausente; un **solape** en
     ella es un defecto de datos: alarma ``export-assignment-overlap`` y se declara tal cual.
   - **Hallazgos** (BR-LAZ-89): por zona, ``LectorExpediente.list`` con los tipos del lazo y la
     ventana ``[period_from, generated_at)``. Un sujeto (episodio o detección) es **del periodo**
     si su primer registro de U-03 tiene ``received_at`` en ``[period_from, period_to)``. Si un
     episodio ya tenía un tramo recibido antes del periodo lo dice el expediente, con **una**
     consulta por lote de los episodios de la ventana (``EpisodeHistory``). No sirve
     ``previous_segment_record_id``: es el identificador del nodo. Un episodio cuyo tramo 0 falta
     o llega tarde sigue siendo del periodo, y su primer tramo recibido lo ancla, como en la
     proyección. Su estado sale de **plegar** sus registros con el
     pliegue puro de la proyección (``loop.projection.fold.step``), así que el paquete lleva
     **todos** los estados, también los descartados y suspendidos. Leer hasta ``generated_at`` y
     no hasta «ahora» hace que una retoma lea lo mismo (PR-LAZ-49).
   - ``evidence_flagged`` (BR-LAZ-94): el resultado de la marca por lote (``resultados_marca``);
     cuenta solo una verificación terminada antes de ``generated_at``.
   - **Cobertura**: ``timeline.zone_timeline`` por tramos de 31 días; sin ella,
     ``coverage_unavailable``.
   - **Acuerdos, regresiones y revocaciones** (BR-LAZ-96 y 97): los registros
     ``use_agreement_signed`` y ``walk_test_regression_*`` de la zona hasta ``generated_at`` y los
     intervalos de ``gate_history``.
   - **Auditoría del periodo** de las zonas pedidas (``LectorExpediente.list_audit`` por zona):
     ``export.create`` se autoriza por zona, así que nada de otras zonas ni de la planta sin zona.

2. **Escribir** con ``ExportStream`` (TASK-345), en orden fijo: un ``findings_json`` y un
   ``coverage`` por zona, la auditoría, los puntos de control, las claves, los clips (solo en la
   forma ``with_clips``: los bytes verificados contra su SHA-256) y los adjuntos de cierre con su
   declaración, el documento portátil (``accreditation.document``, TASK-319); el manifiesto
   (``accreditation.manifest``) al final, en ``vol-01``. El verificador (TASK-346) sigue
   **declarado ausente** con su motivo.

**Zonas unipersonales** (BR-LAZ-99): las marcas de los hallazgos se presentan agregadas a su
ventana y sus registros se listan por sus hashes, sin contenido (el expediente conserva la marca
exacta). El ``ExportPackage`` lleva además los conteos y el hash del manifiesto que el consumidor
escribe en ``export_produced``.

Todo es determinista con el mismo ``ExportJob``: el mismo paquete en cualquier intento.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Protocol

from vigia_platform.accreditation.document import generate_document
from vigia_platform.accreditation.export.composer_port import (
    CHECKPOINT_UNAVAILABLE,
    ExportInputUnavailable,
    ExportJob,
    ExportPackage,
    ExportSink,
)
from vigia_platform.accreditation.export.stream import Artifact, ExportStream
from vigia_platform.accreditation.export.timeline import (
    TimelineCoverage,
    ZoneTimeline,
    timeline_annotations,
    timeline_json,
    zone_timeline,
)
from vigia_platform.accreditation.export.volumes import DOCUMENT_NAME
from vigia_platform.accreditation.manifest.build import (
    ABSENT_REASONS,
    build_manifest,
    count_states,
    manifest_json,
)
from vigia_platform.accreditation.manifest.model import (
    ABSENT_KINDS,
    VERIFIER_VERSION_ABSENT,
    AbsentEntry,
    ActorSnapshot,
    AgreementEntry,
    AgreementSignatoryEntry,
    ArtifactEntry,
    AssignmentEntry,
    CheckpointEntry,
    DeclaredAttachment,
    DeclaredClip,
    FindingSummary,
    GateIntervalEntry,
    ManifestInputs,
    OrganizationRef,
    PlantRef,
    RegressionPeriod,
    RevokedGate,
    StandardCited,
    StateCounts,
    ZoneEntry,
)
from vigia_platform.catalog.domain.enums import GateKind
from vigia_platform.catalog.domain.gates import GateInterval
from vigia_platform.catalog.domain.ports import (
    MAX_SINGLE_OCCUPANCY_ZONES,
    MAX_STANDARD_REFS,
    PortLimitExceeded,
    SingleOccupancy,
    StandardRef,
)
from vigia_platform.catalog.domain.standard import DeclaredStandardVersion
from vigia_platform.fleet.domain.clock_tolerance import clock_tolerance
from vigia_platform.fleet.ports import FleetRangeTooLong, ZoneAssignmentPeriod
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.ledger.application.evidence_read import (
    MAX_MARKER_BATCH,
    MarkerResult,
    MarkerVerification,
)
from vigia_platform.ledger.application.reader import (
    MAX_PAGE_SIZE,
    AuditCursor,
    AuditEntryView,
    AuditFilters,
    AuditPageRequest,
    EvidenceReference,
    LedgerFilters,
    LedgerRecordView,
    Page,
    PageRequest,
    RecordCursor,
)
from vigia_platform.ledger.chain.checkpoints import CheckpointPublicKey, StoredCheckpoint
from vigia_platform.loop.adapters.attachments.rules import attachment_storage_key
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.domain.enums import AttachmentContentType, PackageForm
from vigia_platform.loop.domain.time_aggregation import presented
from vigia_platform.loop.projection.fold import (
    DEFAULT_AGGREGATION_WINDOW_MINUTES,
    U03_RECORD_TYPES,
    U04_RECORD_TYPES,
    EpisodeRow,
    FindingStateRow,
    ProjectionInput,
    step,
    subject_id_of,
)
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "ASSIGNMENT_OVERLAP_ALARM",
    "EMPTY_SHA256",
    "GOVERNANCE_RECORD_TYPES",
    "LOOP_RECORD_TYPES",
    "BlobSource",
    "CheckpointLookup",
    "EpisodeHistory",
    "ExportCatalog",
    "ExportComposer",
    "ExportFleet",
    "ExportGates",
    "ExportMarkers",
    "ExportPorts",
    "ExportedFinding",
    "LedgerReader",
    "PackageContents",
    "PlaceLookup",
    "Places",
    "ZoneContents",
]

EMPTY_SHA256: Final = hashlib.sha256(b"").hexdigest()
"""``document_sha256`` de un paquete que no escribió el documento portátil (compositores
sintéticos de antes de TASK-319, o cualquiera sin ``Artifact(document=True)``): la suma de cero
bytes, que nadie puede confundir con la de un documento real. El compositor real siempre escribe
el documento, así que nunca cae en este valor."""

LOOP_RECORD_TYPES: Final = tuple(sorted(U03_RECORD_TYPES | U04_RECORD_TYPES))
"""Los tipos que se leen de cada zona para sus hallazgos (los 2 de U-03 y los 17 de U-04)."""
GOVERNANCE_RECORD_TYPES: Final = (
    "use_agreement_signed",
    "walk_test_regression_cleared",
    "walk_test_regression_marked",
)
"""Los tipos de U-03 de los acuerdos y las regresiones de la zona (BR-LAZ-96 y 97)."""
ASSIGNMENT_OVERLAP_ALARM: Final = "export-assignment-overlap"

_EVIDENCE_ENTRY: Final = "action_evidence_added"
_NOT_FINDINGS: Final = frozenset(
    {
        "export_produced",
        "monthly_package_produced",
        "sample_audit_result",
        "vocabulary_list_changed",
        "commitments_recorded",
        "loop_configuration_changed",
    }
)
_EPISODE_BATCH: Final = 1000
"""Episodios como mucho por consulta a la historia de la zona."""
_FACT_INSTANTS: Final = 3
"""Rondas de ``standards_at_many`` como mucho: el instante del hecho y los dos extremos."""
_GATE_UNAVAILABLE: Final = LoopDetailCode.GATE_STATE_UNAVAILABLE.value
_STANDARD_UNAVAILABLE: Final = LoopDetailCode.STANDARD_TEXT_UNAVAILABLE.value
_CLIP_EXTENSIONS: Final = {"video/mp4": "mp4", "image/jpeg": "jpg", "image/png": "png"}
_ATTACHMENT_EXTENSIONS: Final = {
    AttachmentContentType.IMAGE_JPEG.value: "jpg",
    AttachmentContentType.IMAGE_PNG.value: "png",
    AttachmentContentType.APPLICATION_PDF.value: "pdf",
}

_log = get_logger("accreditation.export")


# --- Puertos ------------------------------------------------------------------------------------


class LedgerReader(Protocol):
    """``LectorExpediente`` (U-02): las listas paginadas por clave, auditadas."""

    async def list(
        self, context: ScopeContext, filters: LedgerFilters, page: PageRequest
    ) -> Page[LedgerRecordView, RecordCursor]: ...

    async def list_audit(
        self, context: ScopeContext, filters: AuditFilters, page: AuditPageRequest
    ) -> Page[AuditEntryView, AuditCursor]: ...


class ExportCatalog(Protocol):
    """Las formas por lote de ``CatalogQueryPort`` (U-03)."""

    async def single_occupancy_many(
        self, context: ScopeContext, zone_ids: Sequence[uuid.UUID], at: datetime | None = None
    ) -> tuple[SingleOccupancy, ...]: ...

    async def standards_at_many(
        self, context: ScopeContext, refs: Sequence[StandardRef]
    ) -> tuple[DeclaredStandardVersion, ...]: ...


class ExportGates(Protocol):
    """La forma por rango de ``GateQueryPort`` (U-03)."""

    async def gate_history(
        self, context: ScopeContext, zone_id: uuid.UUID, start: datetime, end: datetime
    ) -> tuple[GateInterval, ...]: ...


class ExportFleet(Protocol):
    """La forma por rango de ``FleetQueryPort`` (U-03)."""

    async def assignment_history(
        self, context: ScopeContext, zone_id: uuid.UUID, start: datetime, end: datetime
    ) -> Sequence[ZoneAssignmentPeriod]: ...


class ExportMarkers(Protocol):
    """``EvidencePort.resultados_marca`` (U-02), por lote."""

    async def resultados_marca(
        self, context: ScopeContext, evidence_ids: Iterable[uuid.UUID]
    ) -> Mapping[uuid.UUID, MarkerVerification]: ...


class CheckpointLookup(Protocol):
    """Los puntos de control fijados, por su identificador, y las claves ``checkpoint`` (U-02)."""

    async def checkpoints_by_ids(
        self, context: ScopeContext, entry_ids: Sequence[uuid.UUID]
    ) -> tuple[StoredCheckpoint, ...]: ...

    def checkpoint_public_keys(self) -> tuple[CheckpointPublicKey, ...]: ...


class EpisodeHistory(Protocol):
    """Qué episodios de la ventana ya tenían un tramo (``finding_received``) en el expediente de
    la zona antes de ``before``: una consulta por lote, nunca una por episodio."""

    async def episodes_received_before(
        self,
        context: ScopeContext,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        episode_ids: Sequence[uuid.UUID],
        before: datetime,
    ) -> frozenset[uuid.UUID]: ...


@dataclass(frozen=True, slots=True)
class Places:
    """La organización, la planta y el código y el nombre de cada zona pedida."""

    organization: OrganizationRef
    plant: PlantRef
    zones: Mapping[uuid.UUID, tuple[str, str]]


class PlaceLookup(Protocol):
    async def places(
        self, context: ScopeContext, plant_id: uuid.UUID, zone_ids: Sequence[uuid.UUID]
    ) -> Places: ...


class BlobSource(Protocol):
    """Los bytes de un clip o de un adjunto, ya comprobados contra su SHA-256."""

    async def clip_bytes(self, context: ScopeContext, evidence: EvidenceReference) -> bytes: ...

    async def attachment_bytes(
        self, context: ScopeContext, storage_key: str, sha256: str, size_bytes: int
    ) -> bytes: ...


@dataclass(frozen=True, slots=True)
class ExportPorts:
    reader: LedgerReader
    catalog: ExportCatalog
    gates: ExportGates
    fleet: ExportFleet
    coverage: TimelineCoverage
    markers: ExportMarkers
    checkpoints: CheckpointLookup
    places: PlaceLookup
    blobs: BlobSource
    episodes: EpisodeHistory


# --- Contenido recogido -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExportedFinding:
    """Un hallazgo del paquete: su estado plegado y sus registros."""

    row: FindingStateRow
    records: tuple[LedgerRecordView, ...]
    evidence_flagged: bool


@dataclass(frozen=True, slots=True)
class ZoneContents:
    zone: ZoneEntry
    findings: tuple[ExportedFinding, ...]
    timeline: ZoneTimeline
    regression: tuple[RegressionPeriod, ...]
    revoked: tuple[RevokedGate, ...]
    clips: tuple[tuple[DeclaredClip, EvidenceReference], ...]
    attachments: tuple[tuple[DeclaredAttachment, str], ...]
    """Cada adjunto con su clave de almacén."""


@dataclass(frozen=True, slots=True)
class PackageContents:
    """Todo lo que el paquete lleva, recogido antes de escribir un solo byte."""

    places: Places
    zones: tuple[ZoneContents, ...]
    standards: tuple[StandardCited, ...]
    audit: tuple[AuditEntryView, ...]
    checkpoints: tuple[StoredCheckpoint, ...]
    public_keys: tuple[CheckpointPublicKey, ...]
    counts: StateCounts = field(init=False)

    def __post_init__(self) -> None:
        summaries = [
            FindingSummary(
                subject_id=finding.row.subject_id,
                zone_id=finding.row.zone_id,
                lifecycle_state=finding.row.lifecycle_state,
                evidence_flagged=finding.evidence_flagged,
            )
            for zone in self.zones
            for finding in zone.findings
        ]
        object.__setattr__(self, "counts", count_states(summaries))

    def finding_summaries(self) -> tuple[FindingSummary, ...]:
        return tuple(
            FindingSummary(
                subject_id=finding.row.subject_id,
                zone_id=finding.row.zone_id,
                lifecycle_state=finding.row.lifecycle_state,
                evidence_flagged=finding.evidence_flagged,
            )
            for zone in self.zones
            for finding in zone.findings
        )


# --- utilidades ---------------------------------------------------------------------------------


def _uuid(value: Any) -> uuid.UUID:
    return uuid.UUID(str(value))


def _optional_uuid(value: Any) -> uuid.UUID | None:
    return None if value is None else _uuid(value)


def _instant(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _input(record: LedgerRecordView, document: Mapping[str, Any]) -> ProjectionInput:
    return ProjectionInput(
        record_id=record.record_id,
        organization_id=record.organization_id,
        plant_id=record.plant_id,
        chain_sequence=record.chain_sequence,
        record_type=record.record_type,
        received_at=record.received_at,
        document=document,
    )


def _fact_instants(record: LedgerRecordView) -> tuple[datetime, ...]:
    """Dónde regía la versión del catálogo que cita el registro (la ventana del paso 7 de la
    ingesta, ``FactTime.catalog_window``): con reloj sincronizado, el inicio del hecho y los dos
    extremos de la ventana con su tolerancia; sin él, la recepción."""
    document = record.document()
    node_time = document.get("node_time") or {}
    clock = node_time.get("clock") or {}
    if clock.get("synchronized") is not True or "started_at" not in node_time:
        return (record.received_at,)
    started = _instant(node_time["started_at"])
    ended = _instant(node_time.get("ended_at", node_time["started_at"]))
    offset = clock.get("offset_ms", 0)
    tolerance = clock_tolerance(offset if type(offset) is int else 0)
    return tuple(dict.fromkeys((started, ended + tolerance, started - tolerance)))


def _subject_of(record: LedgerRecordView, document: Mapping[str, Any]) -> uuid.UUID | None:
    """El sujeto del lazo del registro (también la entrada de evidencia de una acción)."""
    if record.record_type in _NOT_FINDINGS:
        return None
    if record.record_type == _EVIDENCE_ENTRY:
        return _optional_uuid(document.get("subject_id"))
    return subject_id_of(_input(record, document))


# --- el compositor ------------------------------------------------------------------------------


@repository
class ExportComposer:
    """El ``PackageComposer`` real (TASK-318)."""

    def __init__(
        self,
        *,
        stream: ExportStream,
        ports: ExportPorts,
        default_window_minutes: int = DEFAULT_AGGREGATION_WINDOW_MINUTES,
        document_renderer: Callable[[ManifestInputs], bytes] = generate_document,
    ) -> None:
        self._stream = stream
        self._ports = ports
        self._default_window = default_window_minutes
        self._document_renderer = document_renderer
        """``accreditation.document.generate_document`` por defecto; las pruebas sintéticas
        inyectan un generador falso y rápido para no pagar un render real de WeasyPrint por cada
        ejemplo (``tests/export_contents_support.py``)."""

    # --- fase 1: recoger ----------------------------------------------------------------------

    async def collect(self, context: ScopeContext, job: ExportJob) -> PackageContents:
        """Todos los insumos del paquete, o ``ExportInputUnavailable`` si falta uno obligatorio."""
        if context.organization_id != job.organization_id:
            raise ValueError("el contexto no es de la organización de la exportación")
        ports = self._ports
        zones = tuple(job.zone_ids)
        places = await ports.places.places(context, job.plant_id, zones)
        if places.organization.organization_id != job.organization_id or set(places.zones) != set(
            zones
        ):
            raise ValueError("los lugares no corresponden al alcance de la exportación")
        checkpoints, public_keys = await self._checkpoints(context, job)
        occupancy = await self._occupancy(context, zones)
        observed_until = min(job.period_to, job.generated_at)
        collected: list[_ZoneDraft] = []
        for zone_id in zones:
            gates = await self._gate_history(context, job, zone_id)
            assignments = await self._assignment_history(context, job, zone_id)
            loop_records = await self._records(
                context, job.plant_id, zone_id, LOOP_RECORD_TYPES, job.period_from, job.generated_at
            )
            governance = await self._records(
                context, job.plant_id, zone_id, GOVERNANCE_RECORD_TYPES, None, job.generated_at
            )
            single, window = occupancy[zone_id]
            by_subject = _by_subject(loop_records)
            started_before = await self._started_before(
                context,
                job,
                zone_id,
                _candidate_episodes(by_subject, job.period_from, job.period_to),
            )
            findings = _fold_findings(by_subject, job.period_from, job.period_to, started_before)
            flagged = await self._flagged(context, findings, job.generated_at)
            timeline = await zone_timeline(
                ports.coverage, context, zone_id, job.period_from, job.period_to, job.generated_at
            )
            code, name = places.zones[zone_id]
            collected.append(
                _ZoneDraft(
                    zone_id=zone_id,
                    code=code,
                    name=name,
                    single=single,
                    window=window,
                    gates=gates,
                    assignments=assignments,
                    governance=governance,
                    findings=findings,
                    flagged=flagged,
                    timeline=timeline,
                )
            )
        standards = await self._standards(context, collected)
        audit = await self._audit(context, job, job.period_from, observed_until)
        return PackageContents(
            places=places,
            zones=tuple(draft.contents(job) for draft in collected),
            standards=standards,
            audit=audit,
            checkpoints=checkpoints,
            public_keys=public_keys,
        )

    async def _checkpoints(
        self, context: ScopeContext, job: ExportJob
    ) -> tuple[tuple[StoredCheckpoint, ...], tuple[CheckpointPublicKey, ...]]:
        """BR-LAZ-91: los puntos de control fijados existen y hay clave para cada firma."""
        ids = tuple(job.checkpoint_record_ids)
        if not ids:
            raise ExportInputUnavailable(CHECKPOINT_UNAVAILABLE)
        found = await self._ports.checkpoints.checkpoints_by_ids(context, ids)
        if sorted((item.entry_id for item in found), key=str) != sorted(set(ids), key=str):
            raise ExportInputUnavailable(CHECKPOINT_UNAVAILABLE)
        keys = self._ports.checkpoints.checkpoint_public_keys()
        published = {key.key_id for key in keys}
        if not keys or any(item.content.key_id not in published for item in found):
            raise ExportInputUnavailable(CHECKPOINT_UNAVAILABLE)
        ordered = tuple(sorted(found, key=lambda item: (item.chain.sort_key(), item.sequence)))
        return ordered, tuple(sorted(keys, key=lambda key: key.key_id))

    async def _occupancy(
        self, context: ScopeContext, zones: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[bool, int]]:
        """Una llamada por cada 50 zonas; sin respuesta, unipersonal con la ventana por defecto."""
        result: dict[uuid.UUID, tuple[bool, int]] = {}
        for start in range(0, len(zones), MAX_SINGLE_OCCUPANCY_ZONES):
            batch = list(zones[start : start + MAX_SINGLE_OCCUPANCY_ZONES])
            try:
                answers = await self._ports.catalog.single_occupancy_many(context, batch)
            except (ResourceNotFound, PortLimitExceeded, LookupError):
                answers = ()
            for answer in answers:
                result[answer.zone_id] = (
                    answer.single_occupancy,
                    answer.aggregation_window_minutes,
                )
        return {zone: result.get(zone, (True, self._default_window)) for zone in zones}

    async def _gate_history(
        self, context: ScopeContext, job: ExportJob, zone_id: uuid.UUID
    ) -> tuple[GateInterval, ...]:
        try:
            intervals = await self._ports.gates.gate_history(
                context, zone_id, job.period_from, job.period_to
            )
        except (ResourceNotFound, PortLimitExceeded, LookupError):
            raise ExportInputUnavailable(_GATE_UNAVAILABLE) from None
        if any(interval.zone_id != zone_id for interval in intervals):
            raise ExportInputUnavailable(_GATE_UNAVAILABLE)
        return tuple(intervals)

    async def _assignment_history(
        self, context: ScopeContext, job: ExportJob, zone_id: uuid.UUID
    ) -> tuple[ZoneAssignmentPeriod, ...] | None:
        try:
            periods = tuple(
                await self._ports.fleet.assignment_history(
                    context, zone_id, job.period_from, job.period_to
                )
            )
        except (ResourceNotFound, FleetRangeTooLong, LookupError):
            return None
        ordered = sorted(periods, key=lambda period: period.assigned_at)
        for before, after in itertools.pairwise(ordered):
            if before.unassigned_at is None or before.unassigned_at > after.assigned_at:
                # La garantía de U-03 es que no se solapan: si ocurre es un defecto de datos.
                _log.error(
                    ASSIGNMENT_OVERLAP_ALARM,
                    organization_id=str(job.organization_id),
                    plant_id=str(job.plant_id),
                    zone_id=str(zone_id),
                    export_request_id=str(job.export_id),
                )
                break
        return tuple(ordered)

    async def _records(
        self,
        context: ScopeContext,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        record_types: Sequence[str],
        received_from: datetime | None,
        received_before: datetime,
    ) -> tuple[LedgerRecordView, ...]:
        """Todos los registros de la zona con esos tipos en la ventana, en orden de cadena."""
        filters = LedgerFilters(
            record_types=tuple(record_types),
            plant_id=plant_id,
            zone_id=zone_id,
            received_from=received_from,
            received_before=received_before,
        )
        found: dict[uuid.UUID, LedgerRecordView] = {}
        cursor: RecordCursor | None = None
        while True:
            page = await self._ports.reader.list(
                context, filters, PageRequest(size=MAX_PAGE_SIZE, after=cursor)
            )
            for record in page.items:
                found[record.record_id] = record
            if page.next_cursor is None or not page.items:
                break
            cursor = page.next_cursor
        return tuple(sorted(found.values(), key=lambda record: record.chain_sequence))

    async def _flagged(
        self, context: ScopeContext, findings: Sequence[_FoldedFinding], generated_at: datetime
    ) -> frozenset[uuid.UUID]:
        """Los sujetos con alguna evidencia de marca rota, verificada antes de ``generated_at``."""
        owners: dict[uuid.UUID, uuid.UUID] = {}
        for finding in findings:
            for record in finding.records:
                for evidence in record.evidences:
                    owners[evidence.evidence_id] = finding.row.subject_id
        ids = sorted(owners, key=str)
        flagged: set[uuid.UUID] = set()
        for start in range(0, len(ids), MAX_MARKER_BATCH):
            batch = ids[start : start + MAX_MARKER_BATCH]
            results = await self._ports.markers.resultados_marca(context, batch)
            for evidence_id, result in results.items():
                if (
                    result.result is MarkerResult.BROKEN
                    and result.marker_verified_at is not None
                    and result.marker_verified_at < generated_at
                    and evidence_id in owners
                ):
                    flagged.add(owners[evidence_id])
        return frozenset(flagged)

    async def _standards(
        self, context: ScopeContext, zones: Sequence[_ZoneDraft]
    ) -> tuple[StandardCited, ...]:
        """BR-LAZ-95: el texto de cada versión citada, por lotes de 200 (``standards_at_many``).

        Cada versión se busca en el instante del hecho de su primer hallazgo (``FactTime.instant``
        de la ingesta). Si allí rige otra (un cambio de catálogo dentro de la tolerancia de reloj
        con que la ingesta la aceptó), se busca en los extremos de esa ventana: a lo sumo tres
        rondas de lotes, cada una solo con lo que quedó sin resolver. Nunca se cita otra versión
        que la evaluada (H-16): lo que no se resuelve es ``standard_text_unavailable``."""
        cited: dict[tuple[uuid.UUID, uuid.UUID, int], tuple[datetime, ...]] = {}
        for draft in zones:
            for finding in draft.findings:
                row = finding.row
                key = (row.zone_id, row.standard_id, row.standard_version)
                cited.setdefault(key, _fact_instants(finding.records[0]))
        pending = sorted(cited, key=lambda key: (str(key[0]), str(key[1]), key[2]))
        found: dict[tuple[uuid.UUID, uuid.UUID, int], DeclaredStandardVersion] = {}
        for round_ in range(_FACT_INSTANTS):
            ask = [key for key in pending if round_ < len(cited[key])]
            for start in range(0, len(ask), MAX_STANDARD_REFS):
                batch = ask[start : start + MAX_STANDARD_REFS]
                refs = [StandardRef(key[0], key[1], cited[key][round_]) for key in batch]
                try:
                    versions = await self._ports.catalog.standards_at_many(context, refs)
                except (ResourceNotFound, PortLimitExceeded, LookupError):
                    raise ExportInputUnavailable(_STANDARD_UNAVAILABLE) from None
                if len(versions) != len(batch):
                    raise ExportInputUnavailable(_STANDARD_UNAVAILABLE)
                for key, declared in zip(batch, versions, strict=True):
                    if declared.zone_id != key[0] or declared.standard_id != key[1]:
                        raise ExportInputUnavailable(_STANDARD_UNAVAILABLE)
                    if declared.version == key[2] and declared.declared_text:
                        found[key] = declared
            pending = [key for key in pending if key not in found]
            if not pending:
                break
        if pending:
            raise ExportInputUnavailable(_STANDARD_UNAVAILABLE)
        return tuple(
            StandardCited(
                zone_id=zone,
                standard_id=standard,
                version=version,
                title_es=declared.title_es,
                declared_text=declared.declared_text,
                declared_text_sha256=_sha256_text(declared.declared_text),
            )
            for (zone, standard, version), declared in sorted(
                found.items(), key=lambda item: (str(item[0][0]), str(item[0][1]), item[0][2])
            )
        )

    async def _started_before(
        self,
        context: ScopeContext,
        job: ExportJob,
        zone_id: uuid.UUID,
        episode_ids: Sequence[uuid.UUID],
    ) -> frozenset[uuid.UUID]:
        """Los episodios con algún tramo recibido antes de ``period_from`` (por lotes de 1000)."""
        found: set[uuid.UUID] = set()
        for start in range(0, len(episode_ids), _EPISODE_BATCH):
            batch = episode_ids[start : start + _EPISODE_BATCH]
            found |= await self._ports.episodes.episodes_received_before(
                context, job.plant_id, zone_id, batch, job.period_from
            )
        return frozenset(found)

    async def _audit(
        self, context: ScopeContext, job: ExportJob, start: datetime, end: datetime
    ) -> tuple[AuditEntryView, ...]:
        """El registro de auditoría del periodo **de las zonas pedidas** en ``[start, end)``.

        ``export.create`` se autoriza por zona: el paquete no lleva entradas de otras zonas de la
        planta ni las de la planta sin zona, que quien pide podría no tener derecho a leer
        (fuera de alcance = no existe)."""
        if end <= start:
            return ()
        found: dict[uuid.UUID, AuditEntryView] = {}
        for zone_id in job.zone_ids:
            filters = AuditFilters(
                plant_id=job.plant_id, zone_id=zone_id, occurred_from=start, occurred_before=end
            )
            cursor: AuditCursor | None = None
            while True:
                page = await self._ports.reader.list_audit(
                    context, filters, AuditPageRequest(size=MAX_PAGE_SIZE, after=cursor)
                )
                for entry in page.items:
                    found[entry.entry_id] = entry
                if page.next_cursor is None or not page.items:
                    break
                cursor = page.next_cursor
        return tuple(sorted(found.values(), key=lambda entry: entry.chain_sequence))

    # --- fase 2: escribir ---------------------------------------------------------------------

    async def compose(self, job: ExportJob, sink: ExportSink) -> ExportPackage:
        """Recoge y escribe el paquete de ``job`` con el contexto de su entrega."""
        context = job.context
        if context is None:
            raise ValueError("el compositor real lee con el contexto de la entrega (job.context)")
        contents = await self.collect(context, job)
        with_clips = job.package_form is PackageForm.WITH_CLIPS
        plan = _plan(contents, with_clips)
        total = len(plan) + 1
        async with self._stream.writer(job, sink) as writer:
            for done, (path, kind, payload) in enumerate(plan, start=1):
                await sink.artifact_started(kind)
                if isinstance(payload, _Blob):
                    data = await payload.read(self._ports.blobs, context)
                    await writer.add_bytes(
                        Artifact(path=path, kind=kind, size_bytes=len(data)), data
                    )
                else:
                    await writer.add_canonical(path, kind, payload)
                await sink.artifact_finished(kind, done, total)
            await sink.artifact_started("document")
            document_inputs = _manifest_inputs(job, contents, writer.artifacts, with_clips)
            pdf_bytes = await asyncio.to_thread(self._document_renderer, document_inputs)
            await writer.add_bytes(
                Artifact(
                    path=DOCUMENT_NAME, kind="document", size_bytes=len(pdf_bytes), document=True
                ),
                pdf_bytes,
            )
            await sink.artifact_finished("document", total, total)
            manifest = build_manifest(_manifest_inputs(job, contents, writer.artifacts, with_clips))
            document = manifest_json(manifest)
            result = await writer.finish(document)
        counts = contents.counts
        return ExportPackage(
            document_sha256=result.document_sha256 or EMPTY_SHA256,
            archive_sha256=result.archive_sha256,
            manifest=document,
            size_bytes=result.size_bytes,
            artifact_count=len(result.artifacts),
            verifier_version=VERIFIER_VERSION_ABSENT,
            stored=result.stored,
            manifest_sha256=result.manifest_sha256,
            finding_counts={
                "finding_count": counts.finding_count,
                "open_finding_count": counts.open_finding_count,
                "closed_finding_count": counts.closed_finding_count,
                "unclosable_count": counts.unclosable_count,
                "discarded_count": counts.discarded_count,
            },
        )


# --- pliegue de los hallazgos -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FoldedFinding:
    row: FindingStateRow
    records: tuple[LedgerRecordView, ...]


type _Subjects = dict[uuid.UUID, list[tuple[LedgerRecordView, Mapping[str, Any]]]]


def _by_subject(records: Sequence[LedgerRecordView]) -> _Subjects:
    """Los registros de la ventana agrupados por sujeto del lazo, en orden de cadena."""
    by_subject: _Subjects = {}
    for record in records:
        document = record.document()
        subject = _subject_of(record, document)
        if subject is not None:
            by_subject.setdefault(subject, []).append((record, document))
    return by_subject


def _first_u03(
    items: Sequence[tuple[LedgerRecordView, Mapping[str, Any]]],
) -> LedgerRecordView | None:
    return next((record for record, _ in items if record.record_type in U03_RECORD_TYPES), None)


def _candidate_episodes(
    by_subject: _Subjects, period_from: datetime, period_to: datetime
) -> tuple[uuid.UUID, ...]:
    """Los episodios cuyo primer tramo **de la ventana** cae en el periodo: de ellos hay que saber
    si ya había un tramo recibido antes de ``period_from`` (empezaron antes)."""
    found: list[uuid.UUID] = []
    for subject, items in by_subject.items():
        first = _first_u03(items)
        if (
            first is not None
            and first.record_type == "finding_received"
            and period_from <= first.received_at < period_to
        ):
            found.append(subject)
    return tuple(sorted(found, key=str))


def _fold_findings(
    by_subject: _Subjects,
    period_from: datetime,
    period_to: datetime,
    started_before: frozenset[uuid.UUID],
) -> tuple[_FoldedFinding, ...]:
    """Los hallazgos del periodo de una zona y su estado, plegando sus registros en orden.

    Un sujeto es del periodo si su **primer** registro de U-03 tiene ``received_at`` en
    ``[period_from, period_to)``: el primero de la ventana, salvo que el episodio ya tuviera un
    tramo recibido antes (``started_before``, consultado al expediente por lote). Un episodio
    cuyo tramo 0 falta o llega tarde sigue siendo del periodo: su primer tramo recibido lo ancla."""
    findings: list[_FoldedFinding] = []
    for subject, items in by_subject.items():
        first = _first_u03(items)
        if first is None:
            continue  # sujeto de antes de la ventana: solo hay registros de U-04
        if not period_from <= first.received_at < period_to or subject in started_before:
            continue
        episode: EpisodeRow | None = None
        row: FindingStateRow | None = None
        applied: list[LedgerRecordView] = []
        for record, document in items:
            if record.chain_sequence < first.chain_sequence:
                continue
            if record.record_type != _EVIDENCE_ENTRY:
                result = step(episode, row, _input(record, document))
                episode, row = result.episode, result.finding
            applied.append(record)
        if row is None:
            continue
        findings.append(_FoldedFinding(row, tuple(applied)))
    return tuple(
        sorted(
            findings, key=lambda item: (item.records[0].chain_sequence, str(item.row.subject_id))
        )
    )


# --- borradores por zona ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ZoneDraft:
    zone_id: uuid.UUID
    code: str
    name: str
    single: bool
    window: int
    gates: tuple[GateInterval, ...]
    assignments: tuple[ZoneAssignmentPeriod, ...] | None
    governance: tuple[LedgerRecordView, ...]
    findings: tuple[_FoldedFinding, ...]
    flagged: frozenset[uuid.UUID]
    timeline: ZoneTimeline

    def contents(self, job: ExportJob) -> ZoneContents:
        regression = _regression_periods(self.zone_id, self.governance, job)
        revoked = tuple(
            RevokedGate(
                zone_id=self.zone_id,
                since=gate.effective_from,
                until=gate.effective_until,
                reason_es=gate.reason_es,
                record_id=gate.record_id,
            )
            for gate in self.gates
            if gate.gate is GateKind.USAGE and gate.status.value == "revoked"
        )
        zone = ZoneEntry(
            zone_id=self.zone_id,
            code=self.code,
            name=self.name,
            gate_intervals=tuple(
                GateIntervalEntry(
                    gate=gate.gate.value,
                    status=gate.status.value,
                    effective_from=gate.effective_from,
                    effective_until=gate.effective_until,
                    decided_by=gate.decided_by,
                    reason_es=gate.reason_es,
                    record_id=gate.record_id,
                )
                for gate in self.gates
            ),
            agreements=_agreements(self.governance, self.gates, job),
            node_assignments=None
            if self.assignments is None
            else tuple(
                AssignmentEntry(
                    node_id=period.node_id,
                    assigned_at=period.assigned_at,
                    unassigned_at=period.unassigned_at,
                    replaces_node_id=period.replaces_node_id,
                )
                for period in self.assignments
            ),
            single_occupancy=self.single,
            aggregation_window_minutes=self.window,
        )
        with_clips = job.package_form is PackageForm.WITH_CLIPS
        findings = tuple(
            ExportedFinding(item.row, item.records, item.row.subject_id in self.flagged)
            for item in self.findings
        )
        clips: list[tuple[DeclaredClip, EvidenceReference]] = []
        attachments: list[tuple[DeclaredAttachment, str]] = []
        for finding in findings:
            for record in finding.records:
                for evidence in record.evidences:
                    extension = _CLIP_EXTENSIONS.get(evidence.content_type, "bin")
                    clips.append(
                        (
                            DeclaredClip(
                                evidence_id=evidence.evidence_id,
                                clip_id=evidence.clip_id,
                                zone_id=self.zone_id,
                                subject_id=finding.row.subject_id,
                                record_id=record.record_id,
                                sha256=evidence.sha256,
                                size_bytes=evidence.size_bytes,
                                duration_ms=evidence.duration_ms,
                                media_kind=evidence.media_kind,
                                path=f"clips/{evidence.evidence_id}.{extension}"
                                if with_clips
                                else None,
                            ),
                            evidence,
                        )
                    )
                if record.record_type == _EVIDENCE_ENTRY:
                    attachments.extend(_attachments(job, self.zone_id, finding, record))
        return ZoneContents(
            zone=zone,
            findings=findings,
            timeline=self.timeline,
            regression=regression,
            revoked=revoked,
            clips=tuple(clips),
            attachments=tuple(attachments),
        )


def _attachments(
    job: ExportJob, zone_id: uuid.UUID, finding: ExportedFinding, record: LedgerRecordView
) -> list[tuple[DeclaredAttachment, str]]:
    found: list[tuple[DeclaredAttachment, str]] = []
    for item in record.document().get("attachments") or ():
        attachment_id = _uuid(item["attachment_id"])
        content_type = str(item["content_type"])
        key = attachment_storage_key(
            organization_id=job.organization_id,
            plant_id=job.plant_id,
            zone_id=zone_id,
            subject_id=finding.row.subject_id,
            attachment_id=attachment_id,
            content_type=AttachmentContentType(content_type),
        )
        found.append(
            (
                DeclaredAttachment(
                    attachment_id=attachment_id,
                    subject_id=finding.row.subject_id,
                    zone_id=zone_id,
                    sha256=str(item["sha256"]),
                    size_bytes=int(item["size_bytes"]),
                    content_type=content_type,
                    path=f"attachments/{attachment_id}.{_ATTACHMENT_EXTENSIONS[content_type]}",
                ),
                key,
            )
        )
    return found


def _regression_periods(
    zone_id: uuid.UUID, governance: Sequence[LedgerRecordView], job: ExportJob
) -> tuple[RegressionPeriod, ...]:
    """Los tramos con acta no vigente que se solapan con el periodo (BR-LAZ-97)."""
    periods: list[RegressionPeriod] = []
    open_since: datetime | None = None
    cause = ""
    for record in governance:
        document = record.document()
        if record.record_type == "walk_test_regression_marked" and open_since is None:
            open_since = _instant(document["marked_at"])
            cause = str(document["cause"])
        elif record.record_type == "walk_test_regression_cleared" and open_since is not None:
            periods.append(
                RegressionPeriod(
                    zone_id=zone_id,
                    starts_at=open_since,
                    ends_at=_instant(document["cleared_at"]),
                    cause=cause,
                )
            )
            open_since = None
    if open_since is not None:
        periods.append(
            RegressionPeriod(zone_id=zone_id, starts_at=open_since, ends_at=None, cause=cause)
        )
    return tuple(
        period
        for period in periods
        if period.starts_at < job.period_to
        and (period.ends_at is None or period.ends_at > job.period_from)
    )


def _agreements(
    governance: Sequence[LedgerRecordView], gates: Sequence[GateInterval], job: ExportJob
) -> tuple[AgreementEntry, ...]:
    """La vigencia de cada acuerdo de uso de la zona en el periodo (BR-LAZ-96)."""
    signed = [record for record in governance if record.record_type == "use_agreement_signed"]
    revocations = sorted(
        (
            gate.effective_from
            for gate in gates
            if gate.gate is GateKind.USAGE and gate.status.value == "revoked"
        ),
    )
    entries: list[AgreementEntry] = []
    for index, record in enumerate(signed):
        document = record.document()
        approved_at = record.occurred_at or record.received_at
        later = signed[index + 1] if index + 1 < len(signed) else None
        if later is not None:
            state, since = "superseded", later.occurred_at or later.received_at
        else:
            revoked = [moment for moment in revocations if approved_at <= moment < job.period_to]
            state, since = ("revoked", revoked[-1]) if revoked else ("current", approved_at)
        if approved_at >= job.period_to or (state == "superseded" and since <= job.period_from):
            continue
        confirmed = {
            str(item["user_id"]): _instant(item["confirmed_at"])
            for item in document.get("confirmations") or ()
        }
        reference = document.get("document_ref")
        entries.append(
            AgreementEntry(
                agreement_id=_uuid(document["agreement_id"]),
                record_id=record.record_id,
                status="approved",
                approved_at=approved_at,
                approved_by=record.actor.id,
                state_in_period=state,
                since=since,
                replaces_agreement_id=_optional_uuid(document.get("replaces_agreement_id")),
                signatories=tuple(
                    AgreementSignatoryEntry(
                        role=str(item["role"]),
                        user_id=_uuid(item["user_id"]),
                        confirmed_at=confirmed.get(str(item["user_id"])),
                    )
                    for item in document.get("signatories") or ()
                ),
                document_sha256=None if reference is None else str(reference["sha256"]),
            )
        )
    return tuple(entries)


# --- plan de artefactos -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Blob:
    """Bytes que se leen del almacén al escribirlos (un clip o un adjunto)."""

    evidence: EvidenceReference | None = None
    storage_key: str | None = None
    sha256: str = ""
    size_bytes: int = 0

    async def read(self, blobs: BlobSource, context: ScopeContext) -> bytes:
        if self.evidence is not None:
            data = await blobs.clip_bytes(context, self.evidence)
            expected, size = self.evidence.sha256, self.evidence.size_bytes
        else:
            if self.storage_key is None:
                raise ValueError("adjunto sin clave")
            data = await blobs.attachment_bytes(
                context, self.storage_key, self.sha256, self.size_bytes
            )
            expected, size = self.sha256, self.size_bytes
        if len(data) != size or hashlib.sha256(data).hexdigest() != expected:
            # Nunca se entrega un byte que no es el que el expediente declara.
            raise ValueError("los bytes no coinciden con su hash declarado")
        return data


def _presented(moment: datetime | None, single: bool, window: int) -> str | None:
    if moment is None:
        return None
    return format_timestamp(presented(moment, single_occupancy=single, window_minutes=window))


def _record_json(record: LedgerRecordView, single: bool, window: int) -> dict[str, Any]:
    document: dict[str, Any] = {
        "record_id": str(record.record_id),
        "record_type": record.record_type,
        "schema_version": record.schema_version,
        "chain_sequence": record.chain_sequence,
        "received_at": _presented(record.received_at, single, window),
        "content_hash": record.content_hash,
        "previous_hash": record.previous_hash,
        "record_hash": record.record_hash,
        "evidence_ids": [str(evidence.evidence_id) for evidence in record.evidences],
    }
    if not single:
        document["content"] = record.document()
    return document


def _finding_json(finding: ExportedFinding, zone: ZoneEntry) -> dict[str, Any]:
    row = finding.row
    single, window = zone.single_occupancy, zone.aggregation_window_minutes
    return {
        "subject_kind": row.subject_kind,
        "subject_id": str(row.subject_id),
        "anchor_record_id": str(row.anchor_record_id),
        "zone_id": str(row.zone_id),
        "family": row.family,
        "tier": row.tier,
        "standard": {"standard_id": str(row.standard_id), "version": row.standard_version},
        "lifecycle_state": row.lifecycle_state,
        "previous_lifecycle_state": row.previous_lifecycle_state,
        "current_classification_outcome": row.current_classification_outcome,
        "current_reason_category": row.current_reason_category,
        "classification_count": row.classification_count,
        "reopening_count": row.reopening_count,
        "has_signed_root_cause": row.has_signed_root_cause,
        "missing_segments_declared": row.missing_segments_declared,
        "observability_state_at_event": row.observability_state_at_event,
        "occurred_at": _presented(row.occurred_at_exact, single, window),
        "closed_at": _presented(row.closed_at, single, window),
        "unclosable_since": _presented(row.unclosable_since, single, window),
        "time_aggregated": single,
        "evidence_flagged": finding.evidence_flagged,
        "records": [_record_json(record, single, window) for record in finding.records],
    }


def _audit_json(entry: AuditEntryView) -> dict[str, Any]:
    actor = entry.actor
    return {
        "entry_id": str(entry.entry_id),
        "chain_sequence": entry.chain_sequence,
        "actor": {
            "kind": actor.kind,
            "id": str(actor.id),
            "display_name_snapshot": actor.display_name_snapshot,
            "role_in_use": actor.role_in_use,
            "concession_id": None if actor.concession_id is None else str(actor.concession_id),
            "unit": actor.unit,
        },
        "operation": entry.operation,
        "scope_plant_id": None if entry.scope_plant_id is None else str(entry.scope_plant_id),
        "scope_zone_id": None if entry.scope_zone_id is None else str(entry.scope_zone_id),
        "resource_kind": entry.resource_kind,
        "resource_id": None if entry.resource_id is None else str(entry.resource_id),
        "filters": entry.filters,
        "result_count": entry.result_count,
        "outcome": entry.outcome,
        "correlation_id": str(entry.correlation_id),
        "occurred_at": format_timestamp(entry.occurred_at),
        "previous_hash": entry.previous_hash,
        "entry_hash": entry.entry_hash,
    }


def _checkpoint_entry(item: StoredCheckpoint) -> CheckpointEntry:
    return CheckpointEntry(
        kind=item.chain.kind.value,
        plant_id=item.chain.plant_id,
        entry_id=item.entry_id,
        sequence=item.sequence,
        entry_hash=item.entry_hash,
        content=item.content.to_json(),
    )


type _Payload = _Blob | dict[str, Any] | list[Any]


def _plan(contents: PackageContents, with_clips: bool) -> list[tuple[str, str, _Payload]]:
    """Los artefactos en su orden fijo: ruta, tipo y lo que llevan."""
    plan: list[tuple[str, str, _Payload]] = []
    for zone in contents.zones:
        plan.append(
            (
                f"findings/zone-{zone.zone.zone_id}.json",
                "findings_json",
                {
                    "zone_id": str(zone.zone.zone_id),
                    "single_occupancy": zone.zone.single_occupancy,
                    "aggregation_window_minutes": zone.zone.aggregation_window_minutes,
                    "findings": [_finding_json(finding, zone.zone) for finding in zone.findings],
                },
            )
        )
    for zone in contents.zones:
        notes = timeline_annotations(
            zone.zone.zone_id,
            zone.timeline.period_from,
            zone.timeline.period_to,
            zone.regression,
            zone.revoked,
        )
        plan.append(
            (
                f"coverage/zone-{zone.zone.zone_id}.json",
                "coverage",
                timeline_json(zone.timeline, notes),
            )
        )
    plan.append(
        ("audit/audit-log.json", "audit", {"entries": [_audit_json(e) for e in contents.audit]})
    )
    plan.append(
        (
            "checkpoints/checkpoints.json",
            "checkpoint",
            {"checkpoints": [_checkpoint_json(item) for item in contents.checkpoints]},
        )
    )
    plan.append(
        (
            "keys/checkpoint-public-keys.json",
            "public_key",
            {"keys": [key.to_json() for key in contents.public_keys]},
        )
    )
    if with_clips:
        clips = sorted(
            (clip for zone in contents.zones for clip in zone.clips),
            key=lambda item: str(item[0].evidence_id),
        )
        for clip, evidence in clips:
            if clip.path is not None:
                plan.append((clip.path, "clip", _Blob(evidence=evidence)))
    attachments = sorted(
        {
            attachment.path: (attachment, key)
            for zone in contents.zones
            for attachment, key in zone.attachments
        }.values(),
        key=lambda item: item[0].path,
    )
    for attachment, key in attachments:
        plan.append(
            (
                attachment.path,
                "attachment",
                _Blob(storage_key=key, sha256=attachment.sha256, size_bytes=attachment.size_bytes),
            )
        )
    return plan


def _checkpoint_json(item: StoredCheckpoint) -> dict[str, Any]:
    entry = _checkpoint_entry(item)
    return {
        "kind": entry.kind,
        "plant_id": None if entry.plant_id is None else str(entry.plant_id),
        "entry_id": str(entry.entry_id),
        "sequence": entry.sequence,
        "entry_hash": entry.entry_hash,
        "content": dict(entry.content),
    }


def _manifest_inputs(
    job: ExportJob,
    contents: PackageContents,
    written: Sequence[Any],
    with_clips: bool,
) -> ManifestInputs:
    requested = job.requested_by
    attachments: dict[str, DeclaredAttachment] = {}
    for zone in contents.zones:
        for declared, _ in zone.attachments:
            attachments[declared.path] = declared
    return ManifestInputs(
        export_id=job.export_id,
        package_form=job.package_form.value,
        organization=contents.places.organization,
        plant=contents.places.plant,
        period_from=job.period_from,
        period_to=job.period_to,
        generated_at=job.generated_at,
        generated_by=ActorSnapshot(
            user_id=str(requested.get("user_id")),
            display_name=requested.get("display_name"),
            role_in_use=requested.get("role_in_use"),
        ),
        zones=tuple(zone.zone for zone in contents.zones),
        findings=contents.finding_summaries(),
        coverage=tuple(zone.timeline.summary for zone in contents.zones),
        regression_periods=tuple(item for zone in contents.zones for item in zone.regression),
        revoked_gates=tuple(item for zone in contents.zones for item in zone.revoked),
        standards_cited=contents.standards,
        clips=tuple(declared for zone in contents.zones for declared, _ in zone.clips),
        attachments=tuple(attachments.values()),
        artifacts=tuple(
            ArtifactEntry(
                path=item.path,
                kind=item.kind,
                sha256=item.sha256,
                size_bytes=item.size_bytes,
                volume=item.volume,
            )
            for item in written
        ),
        absent=tuple(
            AbsentEntry(kind=kind, reason_es=ABSENT_REASONS[kind])
            for kind in ABSENT_KINDS
            if kind not in {item.kind for item in written}
        ),
        checkpoints=tuple(_checkpoint_entry(item) for item in contents.checkpoints),
        checkpoint_public_keys=tuple(key.to_json() for key in contents.public_keys),
    )
