"""Acta de comisionamiento: guardas de cierre, contenido estructurado y reejecución (LC-GOB-08, 09).

**Guardas de cierre**, en el orden de BL §2.2.3 con las de latencia y difuminado al final
(TASK-216); la primera que falla determina el error y no se escribe nada:

1. algún paso sin cerrar: ``steps_still_open`` (BR-GOB-50);
2. alguna fila con menos pases (de cualquier resultado) que su ``required_passes``:
   ``matrix_incomplete``;
3. algún pase ``missed``: ``false_negative_present``. Cero falsos negativos **por zona**, no por
   cámara (BR-GOB-39);
4. alguna cámara del catálogo de la sesión cuya última prueba de oclusión (ya reevaluada) falta,
   quedó ``failed`` o sigue ``pending`` antes de su fecha límite: ``redundancy_not_verified``
   (BR-GOB-43, ``blocking_occlusions``);
5. tasa de falsas alarmas (falsas alarmas sobre pases totales) por encima de
   ``FALSE_ALARM_THRESHOLD`` sin aceptación: ``false_alarm_rate_above_threshold`` (BR-GOB-40);
6. pases más clips de verificación de la zona en la ventana de la sesión por debajo de
   ``MIN_LATENCY_REPETITIONS``: ``latency_not_measured`` (BR-GOB-48 y su nota D-2);
7. ningún ``VerificationClip`` de la zona con el difuminado comprobado por ``head_object``:
   ``blur_not_verified`` (BR-GOB-24 revisada por D-2).

**Comprobación del difuminado** (``blur_check``): solo con lo que dice ``head_object`` (nunca se
descarga el clip, PAT-GOB-REN-05): el objeto existe, su SHA-256 de objeto entero es la del clip y
su metadato ``vigia-anonymized`` vale ``1`` (A-29). La lectura del contenedor sigue en la muestra
diaria de U-02.

**Reejecución** (BR-GOB-55): ``rerun_rows`` abre solo las filas afectadas de la matriz vigente (o
la matriz completa con ``all``); ``clears_regression`` decide si el cierre devuelve la regresión a
``current``: la regresión sigue ``pending`` con la **misma** última marca con la que se abrió la
sesión y las filas de la sesión cubren las afectadas. Las filas se comparan por
``(standard_id, postura)``, como el arrastre de ``regression.carried_forward``: una versión del
estándar que solo cambió textos no marca y cambia el ``row_id``, pero no lo que se midió.

Funciones puras: ningún paso lee la hora del sistema.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from vigia_platform.catalog.domain.enums import (
    OcclusionVerification,
    PassResult,
    Posture,
    RegressionState,
    StepKind,
    WalkTestKind,
)
from vigia_platform.catalog.domain.latency import LatencyReport
from vigia_platform.catalog.domain.matrix import derive_matrix
from vigia_platform.catalog.domain.occlusion import (
    OcclusionTest,
    blocking_occlusions,
    latest_by_camera,
)
from vigia_platform.catalog.domain.regression import ALL_ROWS, AffectedRows, WalkTestRegression
from vigia_platform.catalog.domain.steps import StepHours, WalkTestStep, steps_summary, total_hours
from vigia_platform.catalog.domain.time_windows import utc_instant
from vigia_platform.catalog.domain.walk_test import (
    SessionRow,
    WalkTestPass,
    WalkTestSession,
    pass_counts,
    session_rows,
)
from vigia_platform.fleet.domain.clip_upload_grant import ANONYMIZED_VALUE
from vigia_platform.fleet.domain.verification_clip import ANONYMIZED_METADATA_KEY, ObjectFacts
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "FALSE_ALARM_THRESHOLD",
    "MILLISECONDS_PER_HOUR",
    "MIN_LATENCY_REPETITIONS",
    "BlurCheck",
    "CameraMeasured",
    "CloseFacts",
    "CloseGuard",
    "CommissioningRecord",
    "FalseAlarmAcceptance",
    "RowKey",
    "Signature",
    "blur_check",
    "camera_ids_of",
    "clears_regression",
    "false_alarm_rate",
    "first_failing_guard",
    "matrix_results",
    "rerun_rows",
    "row_keys",
]

FALSE_ALARM_THRESHOLD: Final = 0.0
"""Umbral de la tasa de falsas alarmas del acta (BR-GOB-40), ``[objetivo propio]`` de A-55: toda
falsa alarma exige una aceptación registrada. Se revisa con datos del piloto."""
MIN_LATENCY_REPETITIONS: Final = 100
"""Repeticiones mínimas de la latencia del acta: pases y clips de verificación (BR-GOB-48, D-2)."""
MILLISECONDS_PER_HOUR: Final = 3_600_000

RowKey = tuple[uuid.UUID, Posture]
"""Una fila sin la versión del estándar: ``(standard_id, postura)``."""


class CloseGuard(enum.StrEnum):
    """Las siete guardas del cierre, en su orden (el valor es el nombre del error de dominio)."""

    STEPS_STILL_OPEN = "steps_still_open"
    MATRIX_INCOMPLETE = "matrix_incomplete"
    FALSE_NEGATIVE_PRESENT = "false_negative_present"
    REDUNDANCY_NOT_VERIFIED = "redundancy_not_verified"
    FALSE_ALARM_RATE_ABOVE_THRESHOLD = "false_alarm_rate_above_threshold"
    LATENCY_NOT_MEASURED = "latency_not_measured"
    BLUR_NOT_VERIFIED = "blur_not_verified"


GUARD_ORDER: Final = tuple(CloseGuard)


# --- Guardas -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class CloseFacts:
    """Lo que las guardas miran, leído bajo el candado de la sesión (o antes, para descartar)."""

    steps: Sequence[WalkTestStep] = ()
    rows: Sequence[SessionRow] = ()
    passes: Sequence[WalkTestPass] = ()
    occlusion_tests: Sequence[OcclusionTest] = ()
    cameras: Sequence[uuid.UUID] = ()
    now: datetime | None = None
    false_alarm_accepted: bool = False
    verification_clips_in_window: int = 0
    blur_verified: bool = False


def false_alarm_rate(passes: Iterable[WalkTestPass]) -> float:
    """Falsas alarmas sobre pases totales (cero sin pases)."""
    results = [PassResult(recorded.result) for recorded in passes]
    if not results:
        return 0.0
    return results.count(PassResult.FALSE_ALARM) / len(results)


def _failed(guard: CloseGuard, facts: CloseFacts) -> bool:
    if guard is CloseGuard.STEPS_STILL_OPEN:
        return any(not step.closed for step in facts.steps)
    if guard is CloseGuard.MATRIX_INCOMPLETE:
        counts = pass_counts(facts.rows, facts.passes)
        return any(
            counts[row.row_id].detected + counts[row.row_id].missed + counts[row.row_id].false_alarm
            < row.required_passes
            for row in facts.rows
        )
    if guard is CloseGuard.FALSE_NEGATIVE_PRESENT:
        return any(PassResult(p.result) is PassResult.MISSED for p in facts.passes)
    if guard is CloseGuard.REDUNDANCY_NOT_VERIFIED:
        if facts.now is None:
            raise ValueError("la guarda de oclusión necesita el instante del cierre")
        return bool(blocking_occlusions(facts.occlusion_tests, facts.cameras, facts.now))
    if guard is CloseGuard.FALSE_ALARM_RATE_ABOVE_THRESHOLD:
        above = false_alarm_rate(facts.passes) > FALSE_ALARM_THRESHOLD
        return above and not facts.false_alarm_accepted
    if guard is CloseGuard.LATENCY_NOT_MEASURED:
        repetitions = len(facts.passes) + facts.verification_clips_in_window
        return repetitions < MIN_LATENCY_REPETITIONS
    return not facts.blur_verified


def first_failing_guard(
    facts: CloseFacts, *, last: CloseGuard = CloseGuard.BLUR_NOT_VERIFIED
) -> CloseGuard | None:
    """La primera guarda que falla, evaluando en orden hasta ``last`` incluida; ``None`` si pasan.

    ``last`` permite descartar pronto (las tres primeras antes de reevaluar la oclusión, las seis
    primeras antes de consultar el almacén) sin cambiar el orden.
    """
    for guard in GUARD_ORDER:
        if _failed(guard, facts):
            return guard
        if guard is last:
            return None
    return None


# --- Difuminado ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BlurCheck:
    """Resultado de la comprobación automática del difuminado de un clip (``blur_check_result``)."""

    approved: bool
    checked_at: datetime
    cause: str | None = None
    """Sin aprobar: ``clip_missing``, ``clip_hash_mismatch`` o ``clip_not_anonymized``."""

    def to_json(self) -> dict[str, Any]:
        return {
            "result": "approved" if self.approved else "rejected",
            "method": "head_object_metadata",
            "checked_at": format_timestamp(self.checked_at),
            "cause": self.cause,
        }

    @staticmethod
    def approved_in(result: Mapping[str, Any] | None) -> bool | None:
        """``True``/``False`` según un ``blur_check_result`` guardado; ``None`` si no hay."""
        if result is None:
            return None
        return result.get("result") == "approved"


def blur_check(sha256: str, facts: ObjectFacts | None, at: datetime) -> BlurCheck:
    """El difuminado del clip según ``head_object``: objeto presente, misma SHA-256 de objeto
    entero y metadato ``vigia-anonymized`` igual a ``1`` (A-29)."""
    moment = utc_instant(at)
    if facts is None:
        return BlurCheck(False, moment, "clip_missing")
    if facts.sha256_hex is None or facts.sha256_hex != sha256:
        return BlurCheck(False, moment, "clip_hash_mismatch")
    if facts.metadata.get(ANONYMIZED_METADATA_KEY) != ANONYMIZED_VALUE:
        return BlurCheck(False, moment, "clip_not_anonymized")
    return BlurCheck(True, moment)


# --- Reejecución ---------------------------------------------------------------------------------


def row_keys(rows: Iterable[SessionRow]) -> frozenset[RowKey]:
    return frozenset((row.standard_id, Posture(row.posture)) for row in rows)


def _affected_keys(
    affected: AffectedRows, catalogs: Iterable[Mapping[str, Any]]
) -> frozenset[RowKey] | None:
    """Las filas afectadas como ``(standard_id, postura)``; ``None`` si ``all`` o si alguna no está
    en ninguna de las matrices (entonces cuenta como la matriz completa: nunca se pierde una)."""
    if affected == ALL_ROWS:
        return None
    known: dict[uuid.UUID, RowKey] = {}
    for catalog in catalogs:
        for row in derive_matrix(catalog):
            known[row.row_id] = (row.standard_id, row.posture)
    keys: set[RowKey] = set()
    for row_id in affected:
        key = known.get(row_id)
        if key is None:
            return None
        keys.add(key)
    return frozenset(keys)


def rerun_rows(
    regression: WalkTestRegression, catalog: Mapping[str, Any], passes_per_cell: int
) -> tuple[SessionRow, ...]:
    """La matriz de la reejecución: las filas afectadas de la matriz vigente, o todas (BR-GOB-55).

    Las filas salen de ``derive_matrix`` del catálogo vigente (BR-GOB-52): nunca a mano.
    """
    if regression.state is not RegressionState.PENDING or regression.affected_row_ids is None:
        raise ValueError("solo una regresión pending abre una reejecución")
    rows = session_rows(catalog, passes_per_cell)
    keys = _affected_keys(regression.affected_row_ids, (catalog,))
    if keys is None:
        return rows
    return tuple(row for row in rows if (row.standard_id, Posture(row.posture)) in keys)


def clears_regression(
    session: WalkTestSession,
    regression: WalkTestRegression | None,
    catalogs: Iterable[Mapping[str, Any]],
) -> bool:
    """¿El cierre de ``session`` devuelve la regresión a ``current``? (BR-GOB-55).

    Solo una ``regression_rerun`` sobre la regresión ``pending`` cuya última marca es la que tenía
    al abrirse (ninguna marca después) y cuyas filas cubren las afectadas. ``catalogs`` son el
    catálogo de la sesión y el vigente: las filas afectadas pueden ser de cualquiera de los dos.
    """
    if (
        session.kind is not WalkTestKind.REGRESSION_RERUN
        or regression is None
        or not regression.pending
        or regression.affected_row_ids is None
        or session.regression_basis_record_id is None
        or regression.ledger_record_id != session.regression_basis_record_id
    ):
        return False
    covered = row_keys(session.matrix_rows)
    documents = tuple(catalogs)
    keys = _affected_keys(regression.affected_row_ids, documents)
    if keys is None:
        # «all»: la sesión tiene que cubrir la matriz completa del catálogo vigente.
        current = documents[-1] if documents else None
        if current is None:
            return False
        keys = frozenset((row.standard_id, row.posture) for row in derive_matrix(current))
    return keys <= covered


# --- Acta ----------------------------------------------------------------------------------------


def camera_ids_of(catalog: Mapping[str, Any]) -> tuple[uuid.UUID, ...]:
    """Las cámaras del catálogo, en su orden."""
    return tuple(uuid.UUID(str(camera["camera_id"])) for camera in catalog.get("cameras", ()))


def declared_min_fps_of(catalog: Mapping[str, Any]) -> dict[uuid.UUID, float]:
    """``declared_min_fps`` que declara el catálogo para cada cámara (si lo declara)."""
    declared: dict[uuid.UUID, float] = {}
    for camera in catalog.get("cameras", ()):
        value = camera.get("declared_min_fps")
        if isinstance(value, int | float) and not isinstance(value, bool):
            declared[uuid.UUID(str(camera["camera_id"]))] = float(value)
    return declared


@dataclass(frozen=True, slots=True)
class CameraMeasured:
    """Tasa medida y declarada de una cámara (BR-GOB-49): sin latido, ``measured_fps`` nulo."""

    camera_id: uuid.UUID
    measured_fps: float | None
    declared_min_fps: float | None

    def to_json(self) -> dict[str, Any]:
        return {
            "camera_id": str(self.camera_id),
            "measured_fps": self.measured_fps,
            "declared_min_fps": self.declared_min_fps,
        }


@dataclass(frozen=True, slots=True)
class Signature:
    """``{user_id, role_in_use, signed_at}`` de un firmante del cierre."""

    user_id: uuid.UUID
    role_in_use: str
    signed_at: datetime

    def to_json(self) -> dict[str, Any]:
        return {
            "user_id": str(self.user_id),
            "role_in_use": self.role_in_use,
            "signed_at": format_timestamp(self.signed_at),
        }


@dataclass(frozen=True, slots=True)
class FalseAlarmAcceptance:
    """Aceptación explícita de la tasa de falsas alarmas (BR-GOB-40)."""

    reason_es: str
    accepted_by: uuid.UUID
    accepted_at: datetime

    def to_json(self) -> dict[str, Any]:
        return {
            "reason_es": self.reason_es,
            "accepted_by": str(self.accepted_by),
            "accepted_at": format_timestamp(self.accepted_at),
        }


def matrix_results(
    rows: Sequence[SessionRow],
    passes: Sequence[WalkTestPass],
    verifiable_clips: frozenset[uuid.UUID],
) -> list[dict[str, Any]]:
    """Una entrada por fila: detectados, falsos negativos, falsas alarmas y los ``evidence_ref``
    que no resuelven a un ``VerificationClip`` de la zona («sin clip verificable», sin bloquear)."""
    counts = pass_counts(rows, passes)
    unverifiable: dict[uuid.UUID, list[str]] = {row.row_id: [] for row in rows}
    for recorded in passes:
        ref = recorded.evidence_ref
        listed = unverifiable.get(recorded.row_id)
        if ref is not None and ref not in verifiable_clips and listed is not None:
            listed.append(str(ref))
    results: list[dict[str, Any]] = []
    for row in rows:
        count = counts[row.row_id]
        entry: dict[str, Any] = {
            "row_id": str(row.row_id),
            "detected": count.detected,
            "missed": count.missed,
            "false_alarms": count.false_alarm,
        }
        if unverifiable[row.row_id]:
            entry["unverifiable_evidence_refs"] = sorted(set(unverifiable[row.row_id]))
        results.append(entry)
    return results


@dataclass(frozen=True, slots=True, kw_only=True)
class CommissioningRecord:
    """``CommissioningRecord`` ⛓ (DE §2.15 con sus notas): el acta digital estructurada."""

    commissioning_record_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    session: WalkTestSession
    matrix_results: tuple[Mapping[str, Any], ...]
    false_alarm_rate_observed: float
    false_alarm_threshold: float
    false_alarm_acceptance: FalseAlarmAcceptance | None
    latency: LatencyReport
    latency_repetitions: int
    cameras_measured: tuple[CameraMeasured, ...]
    occlusion_summary: tuple[Mapping[str, Any], ...]
    total_duration_ms: int
    steps_summary: tuple[StepHours, ...]
    installer_measurements: Mapping[str, Any]
    signatures: tuple[Signature, ...]
    closed_at: datetime
    ledger_record_id: uuid.UUID
    regression_cleared: bool = False
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def false_negatives_total(self) -> int:
        return sum(int(entry["missed"]) for entry in self.matrix_results)

    @property
    def total_hours(self) -> Decimal:
        """Horas del acta (columna ``total_hours``): la suma exacta de los pasos en horas."""
        return Decimal(self.total_duration_ms) / Decimal(MILLISECONDS_PER_HOUR)

    def steps_summary_json(self) -> list[dict[str, Any]]:
        """Horas por tipo de paso, **sin responsable** (H-53)."""
        return [
            {"step_kind": StepKind(hours.step_kind).value, "duration_ms": hours.duration_ms}
            for hours in self.steps_summary
        ]

    def latency_json(self) -> dict[str, Any]:
        return {**self.latency.to_json(), "repetitions_counted": self.latency_repetitions}

    def record_content(self) -> dict[str, Any]:
        """Contenido de ``walk_test_result`` (``source_key = commissioning_record_id``)."""
        session = self.session
        content: dict[str, Any] = {
            "commissioning_record_id": str(self.commissioning_record_id),
            "session_id": str(session.session_id),
            "zone_id": str(self.zone_id),
            "catalog_version": session.catalog_version,
            "kind": WalkTestKind(session.kind).value,
            "passes_per_cell": session.passes_per_cell,
            "matrix_results": [dict(entry) for entry in self.matrix_results],
            "false_negatives_total": self.false_negatives_total,
            "false_alarm_rate_observed": self.false_alarm_rate_observed,
            "false_alarm_threshold": self.false_alarm_threshold,
            "latency": self.latency_json(),
            "cameras_measured": [camera.to_json() for camera in self.cameras_measured],
            "occlusion_summary": [dict(entry) for entry in self.occlusion_summary],
            "total_duration_ms": self.total_duration_ms,
            "steps_summary": self.steps_summary_json(),
            "installer_measurements": dict(self.installer_measurements),
            "signatures": [signature.to_json() for signature in self.signatures],
            "closed_at": format_timestamp(self.closed_at),
            "regression_cleared": self.regression_cleared,
        }
        if self.false_alarm_acceptance is not None:
            content["false_alarm_acceptance"] = self.false_alarm_acceptance.to_json()
        return content


def occlusion_summary(
    tests: Iterable[OcclusionTest], cameras: Sequence[uuid.UUID]
) -> tuple[dict[str, Any], ...]:
    """La última prueba de cada cámara del catálogo (las anteriores siguen en el expediente)."""
    latest = latest_by_camera(tests)
    summary: list[dict[str, Any]] = []
    for camera in dict.fromkeys(cameras):
        test = latest.get(camera)
        if test is None:
            raise ValueError("cada cámara del acta tiene su prueba de oclusión (guarda 4)")
        summary.append(
            {
                "camera_id": str(camera),
                "verification": OcclusionVerification(test.verification).value,
                "test_id": str(test.test_id),
            }
        )
    return tuple(summary)


def hours_of(steps: Sequence[WalkTestStep]) -> tuple[int, tuple[StepHours, ...]]:
    """``total_hours`` y ``steps_summary`` de TASK-214, sin responsable (BR-GOB-47)."""
    return total_hours(steps), steps_summary(steps)
