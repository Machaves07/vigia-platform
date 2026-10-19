"""Generadores de la exportación real (TASK-318; business-logic-model.md de U-04, §11 y §12).

- ``long_periods``: entradas de cobertura de una zona y un periodo de **más de 31 días** (hasta
  366), con eventos de observabilidad, cambios de comunicación, de compuerta y de asignación
  repartidos por todo el periodo (también justo en los cortes de 31 días): el insumo de PR-LAZ-27
  y PR-LAZ-29.
- ``exported_packages``: un ``ExportWorld`` con **dos organizaciones**, cada una con su planta y
  de una a tres zonas. Cada zona tiene su expediente del lazo (hallazgos con sus tramos,
  detecciones y los registros de U-04 que la máquina de §4.1 admite, generados con
  ``loop_projection_commands`` de ``strategies.projection``), su historia de compuertas (con
  revocaciones y su motivo), sus acuerdos de uso (con sustituciones), sus marcas de acta no
  vigente, su historia de asignación, su marca unipersonal, sus clips (con la marca de
  anonimización intacta, pendiente o rota, verificada antes o después de ``generated_at``), su
  cobertura y la auditoría de la planta. El ``ExportJob`` pide un periodo y un subconjunto de
  zonas de la organización ``a``.
- ``WorldPorts``: los puertos de ``ExportComposer`` sobre el mundo, con la semántica de los reales
  que importa aquí: aislamiento por la organización del contexto (lo de otra organización no
  existe: ``ResourceNotFound`` o una lista vacía), paginación por clave del ``LectorExpediente``,
  cobertura de U-02 con el tope de 31 días (``compose_timeline`` real) y **recuento de llamadas**
  por operación (NFR-LAZ-03). Los insumos pueden fallar a voluntad (``fail``) para el fallo
  cerrado.

Solo datos generados; ninguna marca sale de la hora real.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from hypothesis import strategies as st
from vigia_contracts.models.enumerations import GateStatus, PredicateFamily

from tests.properties.laz.strategies.projection import (
    _CHOICES,
    _detection,
    _Ids,
    _late_record,
    _no_subject,
    _Partition,
    _pick_subject,
    _segment,
    _u04_step,
)
from tests.writer_support import unit_context
from vigia_platform.accreditation.export.composer_port import ExportJob
from vigia_platform.accreditation.export.contents import ExportPorts, Places
from vigia_platform.accreditation.manifest.model import OrganizationRef, PlantRef
from vigia_platform.catalog.domain.enums import GateKind
from vigia_platform.catalog.domain.gates import GateInterval
from vigia_platform.catalog.domain.ports import (
    MAX_SINGLE_OCCUPANCY_ZONES,
    MAX_STANDARD_REFS,
    PortLimitExceeded,
    SingleOccupancy,
    StandardRef,
)
from vigia_platform.catalog.domain.standard import DeclaredBy, DeclaredStandardVersion
from vigia_platform.fleet.ports import ZoneAssignmentPeriod
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.ledger.application.coverage import CoverageTimeline, CoverageZoneNotFound
from vigia_platform.ledger.application.evidence_read import MarkerResult, MarkerVerification
from vigia_platform.ledger.application.reader import (
    AuditCursor,
    AuditEntryView,
    AuditFilters,
    AuditPageRequest,
    EvidenceReference,
    LedgerFilters,
    LedgerRecordView,
    Page,
    PageRequest,
    RecordActor,
    RecordCursor,
)
from vigia_platform.ledger.chain.checkpoints import (
    ChainKind,
    CheckpointChain,
    CheckpointContent,
    CheckpointPublicKey,
    StoredCheckpoint,
)
from vigia_platform.ledger.domain.coverage import (
    AssignmentInput,
    CommunicationInput,
    CommunicationState,
    CoverageInputs,
    CoveragePeriod,
    CoverageState,
    EventPhase,
    GateInput,
    ObservabilityInput,
    Subject,
    ZoneMode,
    compose_timeline,
)
from vigia_platform.loop.domain.enums import PackageForm
from vigia_platform.loop.projection.fold import ProjectionInput
from vigia_platform.shared.context import ActorKind, ActorUnit, ScopeContext
from vigia_platform.shared.signing.keys import KeyStatus, format_timestamp

__all__ = [
    "BASE",
    "ExportWorld",
    "LongPeriod",
    "WorldPorts",
    "exported_packages",
    "job_world",
    "long_periods",
    "world_context",
]

BASE: Final = datetime(2026, 1, 5, tzinfo=UTC)
"""Instante de partida del mundo generado (nunca la hora real)."""
STEP: Final = timedelta(hours=7)
"""Paso entre registros de una zona: un expediente de 40 pasos dura unos 12 días."""
MS: Final = timedelta(milliseconds=1)
DAY: Final = timedelta(days=1)
STANDARD_ID: Final = uuid.UUID(int=7)
"""El estándar de ``strategies.projection``."""
_ZONE: Final = Subject("zone")
_NODE: Final = uuid.UUID(int=0xA1)


def stamp(moment: datetime) -> str:
    return format_timestamp(moment)


def world_context(organization_id: uuid.UUID) -> ScopeContext:
    """El contexto de sistema de U-04 de la organización (el de la entrega del evento)."""
    return unit_context(organization_id, ActorUnit.U04, kind=ActorKind.SYSTEM)


# --- long_periods (PR-LAZ-27, 29) -----------------------------------------------------------------


@dataclass(frozen=True)
class LongPeriod:
    inputs: CoverageInputs
    period: CoveragePeriod


@st.composite
def long_periods(draw: st.DrawFn, *, min_days: int = 32, max_days: int = 366) -> LongPeriod:
    """Un periodo de ``min_days`` a ``max_days`` días con milisegundos y cambios por todas partes.

    Las marcas caen en una rejilla gruesa (1 h) más, a veces, justo en un corte de 31 días o a un
    milisegundo de él, para que la fusión en los bordes de los tramos se ejercite."""
    days = draw(st.integers(min_days, max_days))
    offset_ms = draw(st.integers(0, 24 * 3600 * 1000 - 1))
    start = BASE + offset_ms * MS
    end = start + days * DAY - draw(st.integers(0, 3_600_000)) * MS
    if end - start <= timedelta(days=31):
        end = start + timedelta(days=31) + MS
    span_hours = int((end - start) / timedelta(hours=1))
    cuts = [start + timedelta(days=31) * k for k in range(1, days // 31 + 1)]
    ids = _Ids(draw(st.uuids()))

    def moment() -> datetime:
        if cuts and draw(st.integers(0, 3)) == 0:
            cut = draw(st.sampled_from(cuts))
            return cut + draw(st.sampled_from((-1, 0, 1))) * MS
        return start + timedelta(hours=draw(st.integers(-48, span_hours + 48)))

    states = (
        (CoverageState.OBSERVABLE, ()),
        (CoverageState.DEGRADED, ("focus",)),
        (CoverageState.NOT_OBSERVABLE, ("obstruction",)),
    )
    observability: list[ObservabilityInput] = []
    for _ in range(draw(st.integers(0, 14))):
        opened_at = moment()
        state, causes = draw(st.sampled_from(states))
        opened = ObservabilityInput(
            ids.next(), ids.next(), _ZONE, EventPhase.OPENED, state, causes, opened_at
        )
        observability.append(opened)
        if draw(st.booleans()):
            closed_state, closed_causes = draw(st.sampled_from(states))
            ended = opened_at + timedelta(hours=draw(st.integers(0, 24 * 40)))
            observability.append(
                ObservabilityInput(
                    ids.next(),
                    ids.next(),
                    _ZONE,
                    EventPhase.CLOSED,
                    closed_state,
                    closed_causes,
                    opened_at,
                    ended_at=ended,
                    opened_event_id=opened.event_id,
                )
            )
    communication = [
        CommunicationInput(ids.next(), _NODE, CommunicationState.REACHABLE, start - 60 * DAY)
    ]
    for _ in range(draw(st.integers(0, 6))):
        communication.append(
            CommunicationInput(
                ids.next(), _NODE, draw(st.sampled_from(list(CommunicationState))), moment()
            )
        )
    gates = [GateInput(ids.next(), start - 60 * DAY, ZoneMode.PRODUCTIVE)]
    gates.extend(
        GateInput(ids.next(), moment(), draw(st.sampled_from(list(ZoneMode))))
        for _ in range(draw(st.integers(0, 4)))
    )
    assignments = [AssignmentInput(ids.next(), _NODE, start - 60 * DAY)]
    if draw(st.booleans()):
        gap = moment()
        assignments = [
            AssignmentInput(ids.next(), _NODE, start - 60 * DAY, gap),
            AssignmentInput(ids.next(), _NODE, gap + timedelta(hours=draw(st.integers(1, 200)))),
        ]
    return LongPeriod(
        CoverageInputs(
            observability=tuple(observability),
            communication=tuple(communication),
            gates=tuple(gates),
            assignments=tuple(assignments),
        ),
        CoveragePeriod(start, end),
    )


# --- el mundo ---------------------------------------------------------------------------------


@dataclass
class ExportWorld:
    """Dos organizaciones con todo lo que una exportación lee (generado)."""

    organizations: tuple[uuid.UUID, uuid.UUID]
    plants: dict[uuid.UUID, uuid.UUID]
    zones: dict[uuid.UUID, list[uuid.UUID]]
    zone_plant: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID]]
    """Zona → (organización, planta)."""
    records: list[LedgerRecordView]
    audit: list[AuditEntryView]
    coverage: dict[uuid.UUID, CoverageInputs]
    usage_changes: dict[uuid.UUID, list[tuple[datetime, GateStatus, str | None]]]
    assignments: dict[uuid.UUID, list[ZoneAssignmentPeriod]]
    occupancy: dict[uuid.UUID, tuple[bool, int]]
    markers: dict[uuid.UUID, MarkerVerification]
    blobs: dict[uuid.UUID, bytes]
    attachment_blobs: dict[str, bytes]
    checkpoints: dict[uuid.UUID, list[StoredCheckpoint]]
    keys: tuple[CheckpointPublicKey, ...]
    regression: dict[uuid.UUID, list[tuple[datetime, datetime | None, str]]]
    standards: dict[tuple[uuid.UUID, uuid.UUID, datetime], int]
    """(zona, estándar, instante) → versión vigente (la que citan los hallazgos)."""
    job: ExportJob

    @property
    def a(self) -> uuid.UUID:
        return self.organizations[0]

    @property
    def b(self) -> uuid.UUID:
        return self.organizations[1]

    def items(self) -> list[ProjectionInput]:
        """Todos los registros como los lee el pliegue (el oráculo por fuerza bruta)."""
        return [
            ProjectionInput(
                record_id=record.record_id,
                organization_id=record.organization_id,
                plant_id=record.plant_id,
                chain_sequence=record.chain_sequence,
                record_type=record.record_type,
                received_at=record.received_at,
                document=record.document(),
            )
            for record in self.records
        ]

    def context(self, organization_id: uuid.UUID | None = None) -> ScopeContext:
        return world_context(organization_id or self.a)

    def job_with(self, **changes: Any) -> ExportJob:
        return dataclasses.replace(self.job, **changes)


def _actor(kind: str = "system") -> RecordActor:
    return RecordActor(
        kind=kind,
        id=uuid.UUID(int=0xACE),
        display_name_snapshot="Sistema sintético",
        role_in_use=None,
        concession_id=None,
        unit="U-03",
    )


def _clip_bytes(evidence_id: uuid.UUID) -> bytes:
    return b"CLIP" + hashlib.sha256(evidence_id.bytes).digest() * 4


class _Builder:
    """Ensambla las cadenas por planta (``chain_sequence`` creciente) y las vistas del lector."""

    def __init__(self, ids: _Ids) -> None:
        self.ids = ids
        self.pending: list[
            tuple[datetime, int, uuid.UUID, uuid.UUID, uuid.UUID, ProjectionInput]
        ] = []
        self.order = 0

    def add(
        self,
        organization_id: uuid.UUID,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        item: ProjectionInput,
    ) -> None:
        self.order += 1
        self.pending.append(
            (item.received_at, self.order, organization_id, plant_id, zone_id, item)
        )

    def build(
        self, markers: dict[uuid.UUID, MarkerVerification], blobs: dict[uuid.UUID, bytes]
    ) -> list[LedgerRecordView]:
        records: list[LedgerRecordView] = []
        sequence: Counter[uuid.UUID] = Counter()
        previous: dict[uuid.UUID, str] = {}
        for received_at, _, organization_id, plant_id, zone_id, item in sorted(
            self.pending, key=lambda entry: (entry[0], entry[1])
        ):
            sequence[plant_id] += 1
            content = json.dumps(dict(item.document), sort_keys=True).encode()
            content_hash = hashlib.sha256(content).hexdigest()
            before = previous.get(plant_id, "0" * 64)
            record_hash = hashlib.sha256((before + content_hash).encode()).hexdigest()
            previous[plant_id] = record_hash
            evidences = _evidences(item, received_at, markers, blobs)
            records.append(
                LedgerRecordView(
                    record_id=item.record_id,
                    organization_id=organization_id,
                    plant_id=plant_id,
                    chain_sequence=sequence[plant_id],
                    record_type=item.record_type,
                    schema_version=1,
                    actor=_actor(),
                    scope_plant_id=plant_id,
                    scope_zone_id=zone_id,
                    scope_node_id=None,
                    correlation_id=uuid.UUID(int=0xC0),
                    received_at=received_at,
                    occurred_at=received_at,
                    source_key=None,
                    content=content,
                    content_hash=content_hash,
                    previous_hash=before,
                    record_hash=record_hash,
                    evidences=evidences,
                )
            )
        return records


def _evidences(
    item: ProjectionInput,
    received_at: datetime,
    markers: dict[uuid.UUID, MarkerVerification],
    blobs: dict[uuid.UUID, bytes],
) -> tuple[EvidenceReference, ...]:
    if item.record_type not in ("finding_received", "detection_for_review_received"):
        return ()
    found: list[EvidenceReference] = []
    for camera in item.document.get("cameras", ()):
        for clip in camera.get("clips", ()):
            clip_id = uuid.UUID(clip["clip_id"])
            evidence_id = uuid.uuid5(clip_id, "evidence")
            data = _clip_bytes(evidence_id)
            blobs[evidence_id] = data
            found.append(
                EvidenceReference(
                    evidence_id=evidence_id,
                    clip_id=clip_id,
                    camera_id=uuid.UUID(camera["camera_id"]),
                    sha256=hashlib.sha256(data).hexdigest(),
                    size_bytes=len(data),
                    content_type="video/mp4",
                    media_kind="video",
                    duration_ms=int(item.document.get("episode_duration_ms", 0)),
                    segment="full",
                    starts_at=received_at - timedelta(minutes=30),
                    ends_at=received_at - timedelta(minutes=29),
                    verified_at=received_at,
                )
            )
            markers.setdefault(evidence_id, MarkerVerification(MarkerResult.PENDING, None, None))
    return tuple(found)


class _ZonePartition(_Partition):
    """Una zona del mundo: un registro cada ``STEP`` desde ``BASE``."""

    def now(self) -> datetime:
        return BASE + STEP * self.sequence


def _zone_records(
    draw: st.DrawFn,
    ids: _Ids,
    organization_id: uuid.UUID,
    plant_id: uuid.UUID,
    zone_id: uuid.UUID,
    steps: int,
) -> list[ProjectionInput]:
    """El expediente del lazo de una zona (la misma máquina que ``loop_projection_commands``)."""
    partition = _ZonePartition(ids, organization_id, plant_id, zone_id)
    for _ in range(steps):
        subjects = sorted(partition.projection.findings.values(), key=lambda r: str(r.subject_id))
        choice = draw(st.sampled_from(_CHOICES))
        if partition.pending_late and draw(st.booleans()):
            _late_record(partition)
        elif choice == "finding" or (choice in ("segment", "u04") and not subjects):
            _segment(draw, partition, None)
        elif choice == "segment" and partition.episodes:
            _segment(draw, partition, draw(st.sampled_from(sorted(partition.episodes, key=str))))
        elif choice == "detection":
            _detection(draw, partition)
        elif choice == "u04" and subjects:
            _u04_step(draw, partition, _pick_subject(draw, subjects))
        else:
            _no_subject(draw, partition)
    return _contractual(partition.items)


def _contractual(items: Sequence[ProjectionInput]) -> list[ProjectionInput]:
    """Lo que el contrato garantiza y el generador del pliegue no escribe: cada tramo cita el
    tramo anterior **ya recibido** del episodio (``previous_segment_record_id``) y todos los tramos
    de un episodio citan la misma versión del estándar que el primero."""
    last: dict[str, uuid.UUID] = {}
    version: dict[str, int] = {}
    result: list[ProjectionInput] = []
    for item in items:
        if item.record_type != "finding_received":
            result.append(item)
            continue
        document = json.loads(json.dumps(item.document))
        episode = document["episode"]
        key = episode["episode_id"]
        if key in last:
            episode["previous_segment_record_id"] = str(last[key])
        document["standard"]["version"] = version.setdefault(key, document["standard"]["version"])
        last[key] = item.record_id
        result.append(dataclasses.replace(item, document=document))
    return result


def _governance(
    draw: st.DrawFn, ids: _Ids, zone_id: uuid.UUID, end: datetime
) -> tuple[list[tuple[datetime, str, dict[str, Any]]], list[tuple[datetime, datetime | None, str]]]:
    """Acuerdos de uso (el primero antes del mundo, quizá otro que lo sustituye) y marcas de acta
    no vigente con su levantamiento (o sin él)."""
    records: list[tuple[datetime, str, dict[str, Any]]] = []
    first = ids.next()

    def agreement(
        agreement_id: uuid.UUID, replaces: uuid.UUID | None, at: datetime
    ) -> dict[str, Any]:
        users = [ids.next() for _ in range(3)]
        roles = ("plant_manager", "copasst", "coordinator_sst")
        return {
            "agreement_id": str(agreement_id),
            "zone_id": str(zone_id),
            "signatories": [
                {"role": r, "user_id": str(u)} for r, u in zip(roles, users, strict=True)
            ],
            "confirmations": [
                {
                    "user_id": str(u),
                    "role_in_use": r,
                    "confirmed_at": stamp(at - timedelta(hours=1)),
                    "origin": "in_app",
                }
                for r, u in zip(roles, users, strict=True)
            ],
            **({"replaces_agreement_id": str(replaces)} if replaces else {}),
        }

    start = BASE - 30 * DAY
    records.append((start, "use_agreement_signed", agreement(first, None, start)))
    if draw(st.booleans()):
        at = BASE + draw(st.integers(0, int((end - BASE) / timedelta(hours=1)))) * timedelta(
            hours=1
        )
        records.append((at, "use_agreement_signed", agreement(ids.next(), first, at)))
    marks: list[tuple[datetime, datetime | None, str]] = []
    cursor = BASE - 2 * DAY
    for _ in range(draw(st.integers(0, 2))):
        marked = cursor + timedelta(hours=draw(st.integers(1, 200)))
        if marked >= end:
            break  # nada se recibe después de generated_at (que es posterior a end)
        cause = draw(st.sampled_from(("catalog_change", "model_version_change")))
        document: dict[str, Any] = {
            "zone_id": str(zone_id),
            "cause": cause,
            "affected_row_ids": "all",
            "marked_at": stamp(marked),
        }
        if cause == "catalog_change":
            document["catalog_version"] = 2
        else:
            document["model_version"] = "model-2"
        records.append((marked, "walk_test_regression_marked", document))
        if draw(st.booleans()):
            cleared = min(marked + timedelta(hours=draw(st.integers(1, 200))), end - MS)
            records.append(
                (
                    cleared,
                    "walk_test_regression_cleared",
                    {
                        "zone_id": str(zone_id),
                        "cleared_by_session_id": str(ids.next()),
                        "cleared_at": stamp(cleared),
                    },
                )
            )
            marks.append((marked, cleared, cause))
            cursor = cleared
        else:
            marks.append((marked, None, cause))
            break
    return records, marks


def _zone_coverage(draw: st.DrawFn, ids: _Ids, end: datetime) -> CoverageInputs:
    span = int((end - BASE) / timedelta(hours=1)) + 48
    states = (
        (CoverageState.OBSERVABLE, ()),
        (CoverageState.DEGRADED, ("focus",)),
        (CoverageState.NOT_OBSERVABLE, ("obstruction",)),
    )
    observability = []
    for _ in range(draw(st.integers(0, 4))):
        state, causes = draw(st.sampled_from(states))
        observability.append(
            ObservabilityInput(
                ids.next(),
                ids.next(),
                _ZONE,
                EventPhase.OPENED,
                state,
                causes,
                BASE + timedelta(hours=draw(st.integers(-24, span))),
            )
        )
    return CoverageInputs(
        observability=tuple(observability),
        communication=(
            CommunicationInput(ids.next(), _NODE, CommunicationState.REACHABLE, BASE - 60 * DAY),
        ),
        gates=(GateInput(ids.next(), BASE - 60 * DAY, ZoneMode.PRODUCTIVE),),
        assignments=(AssignmentInput(ids.next(), _NODE, BASE - 60 * DAY),),
    )


def _usage_changes(draw: st.DrawFn, end: datetime) -> list[tuple[datetime, GateStatus, str | None]]:
    changes: list[tuple[datetime, GateStatus, str | None]] = [
        (BASE - 30 * DAY, GateStatus.APPROVED, None)
    ]
    if draw(st.booleans()):
        span = int((end - BASE) / timedelta(hours=1))
        revoked = BASE + timedelta(hours=draw(st.integers(0, max(span, 1))))
        changes.append((revoked, GateStatus.REVOKED, "Revocación sintética del acuerdo de la zona"))
        if draw(st.booleans()):
            changes.append(
                (revoked + timedelta(hours=draw(st.integers(1, 100))), GateStatus.APPROVED, None)
            )
    return changes


@st.composite
def exported_packages(
    draw: st.DrawFn,
    *,
    max_zones: int = 3,
    max_steps: int = 40,
    package_form: PackageForm | None = None,
) -> ExportWorld:
    """Un mundo de dos organizaciones y la exportación de un periodo de la organización ``a``."""
    ids = _Ids(draw(st.uuids()))
    organizations = (ids.next(), ids.next())
    plants = {organization: ids.next() for organization in organizations}
    zones = {
        organization: [ids.next() for _ in range(draw(st.integers(1, max_zones)))]
        for organization in organizations
    }
    builder = _Builder(ids)
    markers: dict[uuid.UUID, MarkerVerification] = {}
    blobs: dict[uuid.UUID, bytes] = {}
    coverage: dict[uuid.UUID, CoverageInputs] = {}
    usage: dict[uuid.UUID, list[tuple[datetime, GateStatus, str | None]]] = {}
    assignments: dict[uuid.UUID, list[ZoneAssignmentPeriod]] = {}
    occupancy: dict[uuid.UUID, tuple[bool, int]] = {}
    regression: dict[uuid.UUID, list[tuple[datetime, datetime | None, str]]] = {}
    standards: dict[tuple[uuid.UUID, uuid.UUID, datetime], int] = {}
    zone_plant: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID]] = {}
    end = BASE + STEP * (max_steps + 2)
    for organization in organizations:
        plant = plants[organization]
        for zone in zones[organization]:
            zone_plant[zone] = (organization, plant)
            items = _zone_records(
                draw, ids, organization, plant, zone, draw(st.integers(max_steps // 3, max_steps))
            )
            for item in items:
                builder.add(organization, plant, zone, item)
                if item.record_type in ("finding_received", "detection_for_review_received"):
                    standards[(zone, STANDARD_ID, item.received_at)] = int(
                        item.document["standard"]["version"]
                    )
            governance, marks = _governance(draw, ids, zone, end)
            for at, record_type, document in governance:
                builder.add(
                    organization,
                    plant,
                    zone,
                    ProjectionInput(
                        record_id=ids.next(),
                        organization_id=organization,
                        plant_id=plant,
                        chain_sequence=0,
                        record_type=record_type,
                        received_at=at,
                        document=document,
                    ),
                )
            regression[zone] = marks
            coverage[zone] = _zone_coverage(draw, ids, end)
            usage[zone] = _usage_changes(draw, end)
            assignments[zone] = [
                ZoneAssignmentPeriod(
                    node_id=_NODE,
                    assigned_at=BASE - 60 * DAY,
                    unassigned_at=None,
                    replaces_node_id=None,
                )
            ]
            occupancy[zone] = (
                draw(st.booleans()),
                draw(st.sampled_from((15, 60, 480))),
            )
    records = builder.build(markers, blobs)
    generated_at = end + timedelta(hours=draw(st.integers(0, 48)))
    for evidence_id in sorted(markers, key=str):
        result = draw(st.sampled_from(list(MarkerResult)))
        verified = (
            None
            if result is MarkerResult.PENDING
            else generated_at + timedelta(hours=draw(st.integers(-200, 10)))
        )
        markers[evidence_id] = MarkerVerification(result, verified, verified)
    # El periodo y las zonas pedidas: un tramo cualquiera del mundo (o más largo).
    first = BASE + timedelta(hours=draw(st.integers(-24, 24 * 5)))
    length = timedelta(hours=draw(st.integers(1, 24 * 20)))
    period_from = first.replace(minute=0, second=0, microsecond=0)
    period_to = period_from + length
    requested = draw(st.lists(st.sampled_from(zones[organizations[0]]), min_size=1, unique=True))
    checkpoints: dict[uuid.UUID, list[StoredCheckpoint]] = {}
    keys = (
        CheckpointPublicKey(
            key_id="checkpoint-2026-01",
            public_key=base64.b64encode(b"k" * 32).decode(),
            status=KeyStatus.RETIRED,
            valid_from=BASE - 400 * DAY,
            valid_until=BASE - 30 * DAY,
        ),
        CheckpointPublicKey(
            key_id="checkpoint-2026-02",
            public_key=base64.b64encode(b"j" * 32).decode(),
            status=KeyStatus.ACTIVE,
            valid_from=BASE - 30 * DAY,
            valid_until=BASE + 400 * DAY,
        ),
    )
    for organization in organizations:
        chains = (
            CheckpointChain(ChainKind.LEDGER, plants[organization]),
            CheckpointChain(ChainKind.LEDGER, None),
            CheckpointChain(ChainKind.AUDIT, None),
        )
        checkpoints[organization] = [
            StoredCheckpoint(
                chain=chain,
                sequence=index + 10,
                entry_id=ids.next(),
                entry_hash=hashlib.sha256(f"{organization}{index}".encode()).hexdigest(),
                content=CheckpointContent(
                    covered_sequence=index + 9,
                    covered_hash=hashlib.sha256(f"c{organization}{index}".encode()).hexdigest(),
                    taken_at=stamp(generated_at),
                    key_id=keys[index % 2].key_id,
                    signature=base64.b64encode(b"s" * 64).decode(),
                ),
            )
            for index, chain in enumerate(chains)
        ]
    audit = [
        AuditEntryView(
            entry_id=ids.next(),
            chain_sequence=index + 1,
            actor=_actor("user"),
            operation="ledger_read",
            scope_plant_id=plants[organization],
            scope_zone_id=None,
            resource_kind=None,
            resource_id=None,
            filters={"zone_id": str(zones[organization][0])},
            result_count=index,
            outcome="success",
            correlation_id=uuid.UUID(int=0xC1),
            occurred_at=BASE + timedelta(hours=draw(st.integers(-48, 24 * 20))),
            previous_hash="0" * 64,
            entry_hash=hashlib.sha256(f"a{index}".encode()).hexdigest(),
        )
        for organization in organizations
        for index in range(draw(st.integers(0, 4)))
    ]
    organization = organizations[0]
    job = ExportJob(
        export_id=ids.next(),
        organization_id=organization,
        plant_id=plants[organization],
        zone_ids=tuple(requested),
        period_from=period_from,
        period_to=period_to,
        package_form=package_form
        if package_form is not None
        else draw(st.sampled_from(list(PackageForm))),
        requested_by={
            "user_id": str(uuid.UUID(int=0xB0)),
            "display_name": "Coordinación SST sintética",
            "role_in_use": "coordinator_sst",
        },
        correlation_id=uuid.UUID(int=0xC2),
        attempt=1,
        generated_at=generated_at,
        checkpoint_record_ids=tuple(item.entry_id for item in checkpoints[organization]),
        context=world_context(organization),
    )
    return ExportWorld(
        organizations=organizations,
        plants=plants,
        zones=zones,
        zone_plant=zone_plant,
        records=records,
        audit=audit,
        coverage=coverage,
        usage_changes=usage,
        assignments=assignments,
        occupancy=occupancy,
        markers=markers,
        blobs=blobs,
        attachment_blobs={},
        checkpoints=checkpoints,
        keys=keys,
        regression=regression,
        standards=standards,
        job=job,
    )


# --- los puertos sobre el mundo ---------------------------------------------------------------


@dataclass
class WorldPorts:
    """Los nueve puertos del compositor sobre ``world``, con recuento y fallos a voluntad.

    ``fail`` admite ``coverage``, ``standards``, ``gates``, ``checkpoints``, ``keys``,
    ``occupancy`` y ``assignments``."""

    world: ExportWorld
    fail: frozenset[str] = frozenset()
    calls: Counter[str] = field(default_factory=Counter)

    def ports(self) -> ExportPorts:
        return ExportPorts(
            reader=self,
            catalog=self,
            gates=self,
            fleet=self,
            coverage=self,
            markers=self,
            checkpoints=self,
            places=self,
            blobs=self,
        )

    def _zone(self, context: ScopeContext, zone_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
        owner = self.world.zone_plant.get(zone_id)
        if owner is None or owner[0] != context.organization_id:
            raise ResourceNotFound()
        return owner

    # LectorExpediente --------------------------------------------------------------------------

    async def list(
        self, context: ScopeContext, filters: LedgerFilters, page: PageRequest
    ) -> Page[LedgerRecordView, RecordCursor]:
        self.calls["list"] += 1
        selected = [
            record
            for record in self.world.records
            if record.organization_id == context.organization_id
            and (not filters.record_types or record.record_type in filters.record_types)
            and (filters.plant_id is None or record.plant_id == filters.plant_id)
            and (filters.zone_id is None or record.scope_zone_id == filters.zone_id)
            and (filters.received_from is None or record.received_at >= filters.received_from)
            and (filters.received_before is None or record.received_at < filters.received_before)
        ]
        selected.sort(key=lambda r: (r.received_at, r.record_id.int), reverse=True)
        if page.after is not None:
            after = (page.after.received_at, page.after.record_id.int)
            selected = [r for r in selected if (r.received_at, r.record_id.int) < after]
        items = tuple(selected[: page.size])
        more = len(selected) > page.size
        return Page(items, items[-1].cursor if more and items else None)

    async def list_audit(
        self, context: ScopeContext, filters: AuditFilters, page: AuditPageRequest
    ) -> Page[AuditEntryView, AuditCursor]:
        self.calls["list_audit"] += 1
        plant = self.world.plants.get(context.organization_id)
        selected = [
            entry
            for entry in self.world.audit
            if entry.scope_plant_id == plant
            and (filters.plant_id is None or entry.scope_plant_id == filters.plant_id)
            and (filters.occurred_from is None or entry.occurred_at >= filters.occurred_from)
            and (filters.occurred_before is None or entry.occurred_at < filters.occurred_before)
        ]
        selected.sort(key=lambda e: (e.occurred_at, e.entry_id.int), reverse=True)
        if page.after is not None:
            after = (page.after.occurred_at, page.after.entry_id.int)
            selected = [e for e in selected if (e.occurred_at, e.entry_id.int) < after]
        items = tuple(selected[: page.size])
        more = len(selected) > page.size
        return Page(items, items[-1].cursor if more and items else None)

    # U-03 --------------------------------------------------------------------------------------

    async def single_occupancy_many(
        self, context: ScopeContext, zone_ids: Sequence[uuid.UUID], at: datetime | None = None
    ) -> tuple[SingleOccupancy, ...]:
        self.calls["single_occupancy_many"] += 1
        if len(zone_ids) > MAX_SINGLE_OCCUPANCY_ZONES:
            raise PortLimitExceeded("single_occupancy_many", "50 zonas")
        if "occupancy" in self.fail:
            raise ResourceNotFound()
        for zone in zone_ids:
            self._zone(context, zone)
        return tuple(
            SingleOccupancy(
                zone_id=zone,
                single_occupancy=self.world.occupancy[zone][0],
                aggregation_window_minutes=self.world.occupancy[zone][1],
                catalog_version=1,
                issued_at=BASE - 60 * DAY,
            )
            for zone in zone_ids
        )

    async def standards_at_many(
        self, context: ScopeContext, refs: Sequence[StandardRef]
    ) -> tuple[DeclaredStandardVersion, ...]:
        self.calls["standards_at_many"] += 1
        if len(refs) > MAX_STANDARD_REFS:
            raise PortLimitExceeded("standards_at_many", "200 referencias")
        if "standards" in self.fail:
            raise ResourceNotFound()
        result = []
        for ref in refs:
            organization, plant = self._zone(context, ref.zone_id)
            version = self.world.standards.get((ref.zone_id, ref.standard_id, ref.at))
            if version is None:
                raise ResourceNotFound()
            result.append(
                DeclaredStandardVersion(
                    organization_id=organization,
                    plant_id=plant,
                    zone_id=ref.zone_id,
                    standard_id=ref.standard_id,
                    version=version,
                    family=PredicateFamily.COEXISTENCE,
                    title_es=f"Estándar sintético {version}",
                    declared_text=f"Texto declarado sintético de la versión {version}.",
                    declared_by=_declared_by(),
                    effective_from=BASE - 60 * DAY,
                    predicate={},
                    catalog_version=version,
                    reason_es="Versión sintética",
                )
            )
        return tuple(result)

    async def gate_history(
        self, context: ScopeContext, zone_id: uuid.UUID, start: datetime, end: datetime
    ) -> tuple[GateInterval, ...]:
        self.calls["gate_history"] += 1
        organization, plant = self._zone(context, zone_id)
        if "gates" in self.fail:
            raise ResourceNotFound()
        intervals = [
            GateInterval(
                organization_id=organization,
                plant_id=plant,
                zone_id=zone_id,
                gate=GateKind.MOUNTING,
                status=GateStatus.APPROVED,
                effective_from=BASE - 90 * DAY,
                effective_until=None,
                decided_by=uuid.UUID(int=0xD0),
                reason_es=None,
                record_id=uuid.uuid5(zone_id, "mounting"),
                ledger_record_id=uuid.uuid5(zone_id, "mounting-ledger"),
            )
        ]
        changes = self.world.usage_changes[zone_id]
        for index, (at, status, reason) in enumerate(changes):
            until = changes[index + 1][0] if index + 1 < len(changes) else None
            intervals.append(
                GateInterval(
                    organization_id=organization,
                    plant_id=plant,
                    zone_id=zone_id,
                    gate=GateKind.USAGE,
                    status=status,
                    effective_from=at,
                    effective_until=until,
                    decided_by=uuid.UUID(int=0xD1),
                    reason_es=reason,
                    record_id=uuid.uuid5(zone_id, f"usage{index}"),
                    ledger_record_id=uuid.uuid5(zone_id, f"usage-ledger{index}"),
                )
            )
        return tuple(
            interval
            for interval in intervals
            if interval.effective_from <= end
            and (interval.effective_until is None or interval.effective_until > start)
        )

    async def assignment_history(
        self, context: ScopeContext, zone_id: uuid.UUID, start: datetime, end: datetime
    ) -> Sequence[ZoneAssignmentPeriod]:
        self.calls["assignment_history"] += 1
        self._zone(context, zone_id)
        if "assignments" in self.fail:
            raise ResourceNotFound()
        return tuple(self.world.assignments[zone_id])

    # U-02 --------------------------------------------------------------------------------------

    async def linea_de_tiempo(
        self, context: ScopeContext, zone_id: uuid.UUID, period: CoveragePeriod
    ) -> CoverageTimeline:
        self.calls["linea_de_tiempo"] += 1
        owner = self.world.zone_plant.get(zone_id)
        if "coverage" in self.fail or owner is None or owner[0] != context.organization_id:
            raise CoverageZoneNotFound()
        composition = compose_timeline(self.world.coverage[zone_id], period)
        return CoverageTimeline(
            organization_id=owner[0],
            plant_id=owner[1],
            zone_id=zone_id,
            period=composition.period,
            intervals=composition.intervals,
            node_intervals=composition.node_intervals,
            summary=composition.summary,
        )

    async def resultados_marca(
        self, context: ScopeContext, evidence_ids: Iterable[uuid.UUID]
    ) -> Mapping[uuid.UUID, MarkerVerification]:
        self.calls["resultados_marca"] += 1
        return {
            evidence_id: self.world.markers[evidence_id]
            for evidence_id in evidence_ids
            if evidence_id in self.world.markers
        }

    async def checkpoints_by_ids(
        self, context: ScopeContext, entry_ids: Sequence[uuid.UUID]
    ) -> tuple[StoredCheckpoint, ...]:
        self.calls["checkpoints_by_ids"] += 1
        if "checkpoints" in self.fail:
            return ()
        wanted = set(entry_ids)
        return tuple(
            item
            for item in self.world.checkpoints.get(context.organization_id, ())
            if item.entry_id in wanted
        )

    def checkpoint_public_keys(self) -> tuple[CheckpointPublicKey, ...]:
        self.calls["checkpoint_public_keys"] += 1
        return () if "keys" in self.fail else self.world.keys

    async def places(
        self, context: ScopeContext, plant_id: uuid.UUID, zone_ids: Sequence[uuid.UUID]
    ) -> Places:
        self.calls["places"] += 1
        organization = context.organization_id
        if self.world.plants.get(organization) != plant_id:
            raise LookupError("planta fuera de la organización")
        return Places(
            organization=OrganizationRef(
                organization_id=organization,
                code=f"ORG-{str(organization)[:4].upper()}",
                name="Organización sintética",
            ),
            plant=PlantRef(
                plant_id=plant_id,
                code="PL-1",
                name="Planta sintética",
                country="CO",
                timezone="America/Bogota",
            ),
            zones={
                zone: (f"Z-{index + 1}", f"Zona sintética {index + 1}")
                for index, zone in enumerate(self.world.zones[organization])
                if zone in set(zone_ids)
            },
        )

    async def clip_bytes(self, context: ScopeContext, evidence: EvidenceReference) -> bytes:
        self.calls["clip_bytes"] += 1
        return self.world.blobs[evidence.evidence_id]

    async def attachment_bytes(
        self, context: ScopeContext, storage_key: str, sha256: str, size_bytes: int
    ) -> bytes:
        self.calls["attachment_bytes"] += 1
        return self.world.attachment_blobs[storage_key]


def _declared_by() -> DeclaredBy:
    return DeclaredBy(
        user_id=uuid.UUID(int=0xE0), display_name="Gobernanza sintética", role="plant_manager"
    )


# --- un mundo hecho para un ExportJob dado ----------------------------------------------------


def job_world(
    job: ExportJob, *, findings_per_zone: int = 1, standards_per_zone: int = 1
) -> ExportWorld:
    """Un mundo determinista con los identificadores de ``job`` (su organización, su planta, sus
    zonas y sus puntos de control): ``findings_per_zone`` hallazgos por zona en el periodo, que
    citan ``standards_per_zone`` estándares distintos, más una organización ajena. Lo usan las
    pruebas con la solicitud y el consumidor reales (los identificadores salen de la base) y la
    del presupuesto de llamadas."""
    ids = _Ids(job.export_id)
    other, other_plant, other_zone = ids.next(), ids.next(), ids.next()
    organization, plant = job.organization_id, job.plant_id
    builder = _Builder(ids)
    standards: dict[tuple[uuid.UUID, uuid.UUID, datetime], int] = {}
    step = (job.period_to - job.period_from) / (findings_per_zone + 1)
    for zone in job.zone_ids:
        for index in range(findings_per_zone):
            at = (job.period_from + step * (index + 1)).replace(microsecond=0)
            standard = uuid.uuid5(zone, f"standard{index % standards_per_zone}")
            document: dict[str, Any] = {
                "plant_id": str(plant),
                "zone_id": str(zone),
                "family": "coexistence",
                "tier": "tier_1",
                "standard": {"standard_id": str(standard), "version": 1},
                "node_time": {"started_at": stamp(at - MS * 30_000), "ended_at": stamp(at)},
                "observability_state": "observable",
                "max_confidence": 0.8,
                "episode_duration_ms": 30_000,
                "condition_duration_ms": 20_000,
                "cameras": [
                    {
                        "camera_id": str(uuid.UUID(int=11)),
                        "clips": [{"clip_id": str(ids.next()), "segment": "full"}],
                    }
                ],
                "episode": {"episode_id": str(ids.next()), "segment_index": 0, "continues": False},
            }
            item = ProjectionInput(
                record_id=ids.next(),
                organization_id=organization,
                plant_id=plant,
                chain_sequence=0,
                record_type="finding_received",
                received_at=at,
                document=document,
            )
            builder.add(organization, plant, zone, item)
            standards[(zone, standard, at)] = 1
    markers: dict[uuid.UUID, MarkerVerification] = {}
    blobs: dict[uuid.UUID, bytes] = {}
    records = builder.build(markers, blobs)
    for evidence_id in markers:
        markers[evidence_id] = MarkerVerification(MarkerResult.INTACT, job.period_from, None)
    zones = list(job.zone_ids)
    coverage = CoverageInputs(
        communication=(
            CommunicationInput(ids.next(), _NODE, CommunicationState.REACHABLE, BASE - 900 * DAY),
        ),
        gates=(GateInput(ids.next(), BASE - 900 * DAY, ZoneMode.PRODUCTIVE),),
        assignments=(AssignmentInput(ids.next(), _NODE, BASE - 900 * DAY),),
    )
    keys = (
        CheckpointPublicKey(
            key_id="checkpoint-2026-02",
            public_key=base64.b64encode(b"j" * 32).decode(),
            status=KeyStatus.ACTIVE,
            valid_from=BASE - 900 * DAY,
            valid_until=BASE + 900 * DAY,
        ),
    )
    checkpoints = [
        StoredCheckpoint(
            chain=CheckpointChain(ChainKind.LEDGER, plant),
            sequence=index + 1,
            entry_id=entry_id,
            entry_hash=hashlib.sha256(entry_id.bytes).hexdigest(),
            content=CheckpointContent(
                covered_sequence=index,
                covered_hash=hashlib.sha256(entry_id.bytes + b"c").hexdigest(),
                taken_at=stamp(job.generated_at),
                key_id="checkpoint-2026-02",
                signature=base64.b64encode(b"s" * 64).decode(),
            ),
        )
        for index, entry_id in enumerate(job.checkpoint_record_ids)
    ]
    return ExportWorld(
        organizations=(organization, other),
        plants={organization: plant, other: other_plant},
        zones={organization: zones, other: [other_zone]},
        zone_plant={
            **{zone: (organization, plant) for zone in zones},
            other_zone: (other, other_plant),
        },
        records=records,
        audit=[],
        coverage={zone: coverage for zone in [*zones, other_zone]},
        usage_changes={
            zone: [(BASE - 900 * DAY, GateStatus.APPROVED, None)] for zone in [*zones, other_zone]
        },
        assignments={
            zone: [
                ZoneAssignmentPeriod(
                    node_id=_NODE,
                    assigned_at=BASE - 900 * DAY,
                    unassigned_at=None,
                    replaces_node_id=None,
                )
            ]
            for zone in [*zones, other_zone]
        },
        occupancy={zone: (False, 60) for zone in [*zones, other_zone]},
        markers=markers,
        blobs=blobs,
        attachment_blobs={},
        checkpoints={organization: checkpoints, other: []},
        keys=keys,
        regression={zone: [] for zone in zones},
        standards=standards,
        job=job,
    )
