"""Generadores de la proyección del lazo (TASK-305; business-logic-model.md de U-04, §12).

- ``episodes_with_segments``: un episodio con tramos completos, faltantes (huérfanos), tardíos y
  en desorden de llegada, construido sobre ``finding`` del kit de U-01 (``Finding`` aceptado):
  cada tramo es una copia del hallazgo del kit con su índice, su tier, su estándar, sus
  duraciones y sus clips propios. Devuelve un ``EpisodeScenario`` con los ``ProjectionInput`` en
  orden de llegada (``chain_sequence``) y lo que el pliegue debe deducir de ellos.
- ``loop_projection_commands``: registros de una a tres particiones ``(organización, planta)``
  — hallazgos con sus tramos, detecciones y los registros de U-04 que la máquina de §4.1 admite
  en cada momento (clasificar, reclasificar, resolver la cola, asignar, vencer, cerrar, declarar
  incerrable, revisarlo, reabrir, firmar la causa raíz, tramo tardío) más tipos sin efecto sobre
  el sujeto —, con ``chain_sequence`` creciente por partición. Para saber qué admite cada sujeto
  sigue la propia proyección (``PlantProjection``): solo genera secuencias que un expediente
  real podría contener.

Los contenidos de U-04 llevan los campos que el pliegue lee, no el registro completo (el esquema
estricto de cada tipo lo prueban ``test_laz_record_schemas.py`` y TASK-302). Solo datos
generados; ninguna marca sale de la hora real.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from hypothesis import strategies as st
from vigia_contracts.conformance.generators import finding, zone_catalog

from vigia_platform.loop.domain.enums import (
    ClassificationOutcome,
    FindingLifecycleState,
    ReasonCategoryAuthorized,
    ReasonCategoryConfirmed,
    ReasonCategoryFalsePositive,
)
from vigia_platform.loop.projection.fold import (
    FindingStateRow,
    LateSegmentNotice,
    PlantProjection,
    ProjectionInput,
    Step,
)

__all__ = [
    "BASE_TIME",
    "EpisodeScenario",
    "SegmentSpec",
    "episodes_with_segments",
    "loop_projection_commands",
    "stamp",
]

S = FindingLifecycleState
BASE_TIME: Final = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)
"""Instante de partida de los registros generados (nunca la hora real)."""
MAX_SEGMENT_MS: Final = 60_000

_REASONS: Final[Mapping[ClassificationOutcome, tuple[str, ...]]] = {
    ClassificationOutcome.CONFIRMED: tuple(ReasonCategoryConfirmed),
    ClassificationOutcome.AUTHORIZED_OPERATION: tuple(ReasonCategoryAuthorized),
    ClassificationOutcome.FALSE_POSITIVE: tuple(ReasonCategoryFalsePositive),
}
_DISCARDS: Final = (
    ClassificationOutcome.AUTHORIZED_OPERATION,
    ClassificationOutcome.FALSE_POSITIVE,
)


def stamp(moment: datetime) -> str:
    """Marca del contrato: UTC con milisegundos y ``Z``."""
    return moment.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="milliseconds") + "Z"


# --- episodes_with_segments ----------------------------------------------------------------


@dataclass(frozen=True)
class SegmentSpec:
    """Un tramo presente del episodio y lo que declara."""

    segment_index: int
    tier: str
    standard_version: int
    episode_duration_ms: int
    condition_duration_ms: int
    record_id: uuid.UUID


@dataclass(frozen=True)
class EpisodeScenario:
    """Un episodio generado: sus tramos en orden de llegada y lo esperado."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    episode_id: uuid.UUID
    segments: tuple[SegmentSpec, ...]
    """En orden de llegada."""
    inputs: tuple[ProjectionInput, ...]
    """Los ``finding_received`` en orden de llegada (``chain_sequence`` 1, 2…)."""
    expected_count: int
    """``segment_index`` máximo + 1."""

    @property
    def missing(self) -> tuple[int, ...]:
        present = {segment.segment_index for segment in self.segments}
        return tuple(i for i in range(self.expected_count) if i not in present)


def _segment_document(
    base: Mapping[str, Any],
    spec: SegmentSpec,
    *,
    continues: bool,
    previous: uuid.UUID | None,
    started: datetime,
    clip_ids: Sequence[uuid.UUID],
) -> dict[str, Any]:
    document = copy.deepcopy(dict(base))
    document["finding_id"] = str(spec.record_id)
    document["episode"]["segment_index"] = spec.segment_index
    document["episode"]["continues"] = continues
    document["episode"].pop("previous_segment_record_id", None)
    if previous is not None:
        document["episode"]["previous_segment_record_id"] = str(previous)
    document["tier"] = spec.tier
    document["standard"]["version"] = spec.standard_version
    ended = started + timedelta(milliseconds=spec.episode_duration_ms)
    document["node_time"]["started_at"] = stamp(started)
    document["node_time"]["ended_at"] = stamp(ended)
    document["episode_duration_ms"] = spec.episode_duration_ms
    document["condition_duration_ms"] = spec.condition_duration_ms
    clips = iter(clip_ids)
    for camera in document["cameras"]:
        for clip in camera["clips"]:
            clip["clip_id"] = str(next(clips))
    document["receipt"]["platform_record_id"] = str(spec.record_id)
    return document


@st.composite
def episodes_with_segments(draw: st.DrawFn, max_segments: int = 6) -> EpisodeScenario:
    """Un episodio de 1 a ``max_segments`` índices, con huecos y llegada en cualquier orden."""
    catalog = draw(zone_catalog())
    base = draw(finding(catalog, accepted=True, secondary_families=False))
    expected = draw(st.integers(min_value=1, max_value=max_segments))
    present = draw(
        st.sets(st.integers(0, expected - 1), min_size=1, max_size=expected).filter(
            lambda indexes: expected - 1 in indexes
        )
    )
    order = draw(st.permutations(sorted(present)))
    record_ids = draw(st.lists(st.uuids(), min_size=len(order), max_size=len(order), unique=True))
    clip_count = sum(len(camera["clips"]) for camera in base["cameras"])
    specs: list[SegmentSpec] = []
    for index, record_id in zip(order, record_ids, strict=True):
        duration = draw(st.integers(0, MAX_SEGMENT_MS))
        specs.append(
            SegmentSpec(
                segment_index=index,
                tier=draw(st.sampled_from(("tier_1", "tier_2"))),
                standard_version=draw(st.integers(1, 5)),
                episode_duration_ms=duration,
                condition_duration_ms=draw(st.integers(0, duration)),
                record_id=record_id,
            )
        )
    by_index = {spec.segment_index: spec for spec in specs}
    episode_start = BASE_TIME + timedelta(minutes=draw(st.integers(0, 60 * 24 * 30)))
    inputs: list[ProjectionInput] = []
    for sequence, spec in enumerate(specs, start=1):
        previous = by_index.get(spec.segment_index - 1)
        clip_ids = draw(st.lists(st.uuids(), min_size=clip_count, max_size=clip_count, unique=True))
        started = episode_start + timedelta(milliseconds=MAX_SEGMENT_MS * spec.segment_index)
        document = _segment_document(
            base,
            spec,
            continues=spec.segment_index < expected - 1,
            previous=None if spec.segment_index == 0 or previous is None else previous.record_id,
            started=started,
            clip_ids=clip_ids,
        )
        inputs.append(
            ProjectionInput(
                record_id=spec.record_id,
                organization_id=uuid.UUID(base["organization_id"]),
                plant_id=uuid.UUID(base["plant_id"]),
                chain_sequence=sequence,
                record_type="finding_received",
                received_at=started + timedelta(minutes=5 + sequence),
                document=document,
            )
        )
    return EpisodeScenario(
        organization_id=uuid.UUID(base["organization_id"]),
        plant_id=uuid.UUID(base["plant_id"]),
        episode_id=uuid.UUID(base["episode"]["episode_id"]),
        segments=tuple(specs),
        inputs=tuple(inputs),
        expected_count=expected,
    )


# --- loop_projection_commands --------------------------------------------------------------


class _Ids:
    """Identificadores únicos de una generación (Hypothesis puede repetir ``st.uuids()``)."""

    def __init__(self, seed: uuid.UUID) -> None:
        self._seed = seed
        self._count = 0

    def next(self) -> uuid.UUID:
        self._count += 1
        return uuid.uuid5(self._seed, str(self._count))


class _Partition:
    """Una planta en generación: su secuencia, su reloj y la proyección que la sigue."""

    def __init__(
        self, ids: _Ids, organization_id: uuid.UUID, plant_id: uuid.UUID, zone_id: uuid.UUID
    ) -> None:
        self.ids = ids
        self.organization_id = organization_id
        self.plant_id = plant_id
        self.zone_id = zone_id
        self.sequence = 0
        self.projection = PlantProjection(organization_id, plant_id)
        self.items: list[ProjectionInput] = []
        self.episodes: dict[uuid.UUID, set[int]] = {}
        """Por episodio: los ``segment_index`` ya emitidos."""
        self.pending_late: list[LateSegmentNotice] = []

    def now(self) -> datetime:
        return BASE_TIME + timedelta(minutes=10 * self.sequence)

    def emit(self, record_type: str, document: dict[str, Any], *, event: bool) -> Step:
        self.sequence += 1
        record_id = self.ids.next()
        item = ProjectionInput(
            record_id=record_id,
            organization_id=self.organization_id,
            plant_id=self.plant_id,
            chain_sequence=self.sequence,
            record_type=record_type,
            received_at=self.now(),
            document=document,
            event_id=self.ids.next() if event else None,
        )
        self.items.append(item)
        return self.projection.apply(item)


def _observed(draw: st.DrawFn, partition: _Partition, started: datetime) -> dict[str, Any]:
    duration = draw(st.integers(0, MAX_SEGMENT_MS))
    return {
        "plant_id": str(partition.plant_id),
        "zone_id": str(partition.zone_id),
        "family": "coexistence",
        "standard": {"standard_id": str(uuid.UUID(int=7)), "version": draw(st.integers(1, 3))},
        "node_time": {
            "started_at": stamp(started),
            "ended_at": stamp(started + timedelta(milliseconds=duration)),
        },
        "observability_state": draw(st.sampled_from(("observable", "degraded"))),
        "max_confidence": draw(st.floats(0.0, 1.0, allow_nan=False)),
        "episode_duration_ms": duration,
        "condition_duration_ms": draw(st.integers(0, duration)),
        "cameras": [
            {
                "camera_id": str(uuid.UUID(int=11)),
                "clips": [{"clip_id": str(partition.ids.next()), "segment": "full"}],
            }
        ],
    }


def _segment(draw: st.DrawFn, partition: _Partition, episode_id: uuid.UUID | None) -> None:
    """Un tramo: de un episodio nuevo o, si se da, uno no emitido del episodio (o huérfano)."""
    if episode_id is None:
        episode_id = partition.ids.next()
        partition.episodes[episode_id] = set()
    emitted = partition.episodes[episode_id]
    top = max(emitted, default=-1)
    free = [index for index in range(top + 3) if index not in emitted]
    index = draw(st.sampled_from(free))
    emitted.add(index)
    document = _observed(draw, partition, partition.now() - timedelta(minutes=30))
    document["tier"] = draw(st.sampled_from(("tier_1", "tier_2")))
    document["episode"] = {
        "episode_id": str(episode_id),
        "segment_index": index,
        "continues": draw(st.booleans()),
    }
    result = partition.emit("finding_received", document, event=True)
    if result.late_segment is not None:
        partition.pending_late.append(result.late_segment)


def _late_record(partition: _Partition) -> None:
    notice = partition.pending_late.pop(0)
    document = {
        "segment_record_id": str(notice.segment_record_id),
        "episode_id": str(notice.episode_id),
        "segment_index": notice.segment_index,
        "received_at": stamp(notice.received_at),
        "episode_state_at_arrival": notice.episode_state_at_arrival.value,
        "reopened_loop": False,
        "delay_ms": notice.delay_ms,
    }
    partition.emit("late_segment_attached", document, event=False)


def _subject(row: FindingStateRow) -> dict[str, str]:
    return {"subject_kind": row.subject_kind, "subject_id": str(row.subject_id)}


def _classification(
    draw: st.DrawFn, row: FindingStateRow, outcome: ClassificationOutcome, at: datetime
) -> dict[str, Any]:
    return {
        **_subject(row),
        "outcome": outcome.value,
        "reason_category": draw(st.sampled_from(_REASONS[outcome])),
        "reinforced_reason": S(row.lifecycle_state) is not S.RECEIVED,
        "classified_at": stamp(at),
    }


def _assigned(
    draw: st.DrawFn, row: FindingStateRow, action_id: uuid.UUID, at: datetime
) -> dict[str, Any]:
    return {
        **_subject(row),
        "action_id": str(action_id),
        "action_type": draw(st.sampled_from(("engineering", "process"))),
        "assignee_user_id": str(uuid.UUID(int=99)),
        "due_at": stamp(at + timedelta(hours=draw(st.integers(1, 24 * 20)))),
    }


_SIGNER: Final = {"professional_license": "LIC-0001"}


def _options(row: FindingStateRow) -> list[str]:
    """Lo que la máquina de §4.1 admite sobre el sujeto (más lo que no cambia su estado)."""
    state = S(row.lifecycle_state)
    if state is S.ACTION_ASSIGNED and row.due_at is None:
        return ["complete_action"]
    options = {
        S.RECEIVED: ["classify"],
        S.IN_REVIEW: ["review"],
        S.OPEN: ["assign", "suspend", "unclosable"],
        S.ACTION_ASSIGNED: ["overdue", "close", "unclosable", "suspend"],
        S.OVERDUE: ["escalate", "close", "unclosable", "suspend"],
        S.CLOSED: ["reopen", "reclassify"],
        S.UNCLOSABLE: ["renew", "convert", "reopen", "reclassify"],
        S.SUSPENDED_BY_RECLASSIFICATION: ["resume"],
        S.DISCARDED: ["reclassify"],
    }[state]
    return [*options, "root_cause"]


def _u04_document(
    draw: st.DrawFn, partition: _Partition, row: FindingStateRow, option: str
) -> tuple[str, dict[str, Any]]:
    at = partition.now()
    subject = _subject(row)
    outcomes = tuple(ClassificationOutcome)
    match option:
        case "classify" | "reclassify":
            return "classification", _classification(draw, row, draw(st.sampled_from(outcomes)), at)
        case "suspend":
            return "classification", _classification(
                draw, row, draw(st.sampled_from(_DISCARDS)), at
            )
        case "resume":
            return "classification", _classification(draw, row, ClassificationOutcome.CONFIRMED, at)
        case "review":
            outcome = draw(st.sampled_from(("review_confirmed", "review_discarded")))
            return "review_resolution", {"detection_id": str(row.subject_id), "outcome": outcome}
        case "assign":
            return "action_assigned", _assigned(draw, row, partition.ids.next(), at)
        case "complete_action":
            assert row.current_action_id is not None
            return "action_assigned", _assigned(draw, row, row.current_action_id, at)
        case "overdue" | "escalate":
            level = "coordinator" if option == "overdue" else "manager_weekly"
            return "action_overdue", {
                **subject,
                "action_id": str(row.current_action_id),
                "days_overdue": draw(st.integers(0, 60)),
                "declared_at": stamp(at),
                "escalation_level": level,
            }
        case "close":
            entries = [str(partition.ids.next()) for _ in range(draw(st.integers(1, 3)))]
            return "closure", {
                **subject,
                "action_id": str(row.current_action_id),
                "evidence_entry_ids": entries,
                "signer": dict(_SIGNER),
                "signed_at": stamp(at),
            }
        case "unclosable":
            return "unclosable_state_declared", {
                **subject,
                "declared_at": stamp(at),
                "review_due_at": stamp(at + timedelta(days=180)),
                "signer": dict(_SIGNER),
            }
        case "renew":
            due = stamp(at + timedelta(days=draw(st.integers(1, 365))))
            return "unclosable_review", {
                "subject_id": str(row.subject_id),
                "outcome": "renewed",
                "new_review_due_at": due,
            }
        case "convert":
            return "unclosable_review", {
                "subject_id": str(row.subject_id),
                "outcome": "converted_to_action",
                "new_action_id": str(partition.ids.next()),
            }
        case "reopen":
            return "reopening", {
                **subject,
                "reason_text": "Motivo sintético de reapertura del lazo",
                "new_action_id": str(partition.ids.next()),
            }
    state = draw(st.sampled_from(("signed", "pending_information")))
    return "root_cause_analysis", {**subject, "state": state}


def _u04_step(draw: st.DrawFn, partition: _Partition, row: FindingStateRow) -> None:
    record_type, document = _u04_document(
        draw, partition, row, draw(st.sampled_from(_options(row)))
    )
    partition.emit(record_type, document, event=False)


def _no_subject(draw: st.DrawFn, partition: _Partition) -> None:
    record_type = draw(st.sampled_from(("loop_configuration_changed", "action_evidence_added")))
    document = {"plant_id": str(partition.plant_id), "subject_id": str(partition.ids.next())}
    partition.emit(record_type, document, event=False)


def _detection(draw: st.DrawFn, partition: _Partition) -> None:
    document = _observed(draw, partition, partition.now() - timedelta(minutes=20))
    document["detection_id"] = str(partition.ids.next())
    partition.emit("detection_for_review_received", document, event=True)


_CHOICES: Final = ("finding", "segment", "detection", "other", *("u04",) * 8)
"""Pesos: sobre todo comandos de U-04, para recorrer el lazo entero y no solo su entrada."""


def _pick_subject(draw: st.DrawFn, subjects: Sequence[FindingStateRow]) -> FindingStateRow:
    """Un sujeto, con preferencia por los que ya salieron de la entrada (lazo más profundo)."""
    advanced = [row for row in subjects if row.lifecycle_state not in (S.RECEIVED, S.IN_REVIEW)]
    pool = advanced if advanced and draw(st.integers(0, 3)) else subjects
    return draw(st.sampled_from(pool))


@st.composite
def loop_projection_commands(
    draw: st.DrawFn, max_partitions: int = 3, max_steps: int = 40
) -> list[ProjectionInput]:
    """Registros de una a ``max_partitions`` plantas, en orden de ``chain_sequence`` por planta."""
    ids = _Ids(draw(st.uuids()))
    count = draw(st.integers(1, max_partitions))
    organizations = [ids.next() for _ in range(draw(st.integers(1, 2)))]
    partitions = [
        _Partition(ids, draw(st.sampled_from(organizations)), ids.next(), ids.next())
        for _ in range(count)
    ]
    for _ in range(draw(st.integers(0, max_steps))):
        partition = draw(st.sampled_from(partitions))
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
    return [item for partition in partitions for item in partition.items]
