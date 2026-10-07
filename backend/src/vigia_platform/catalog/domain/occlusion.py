"""Prueba de redundancia por oclusión (DE §2.14 y su nota; BR-GOB-41 a 43, 97 a 100; LC-GOB-07).

Se ocluye cada cámara de la zona por turno y la plataforma correlaciona la ventana de la prueba
con los ``observability_event_received`` que el nodo ya envió al expediente. La prueba nace
``pending`` con ``deadline = ended_at + 5 min`` y se resuelve **una sola vez** (PAT-GOB-REN-07).

**Eventos contados** (nota del 2026-09-20 y pendiente nº 40): los de la zona cuyo ``started_at``
(el instante del nodo) cae en ``[started_at - 30 s, ended_at + 30 s]`` y que la plataforma
**recibió** como mucho en ``deadline``. No cuentan los de alcance de nodo: sujeto ``clock``,
``signal_reader`` o ``local_queue``, causas solo de nodo (``clock_unsynchronized``,
``signal_reader_unavailable``, ``local_queue_over_threshold``, ``node_restart``), las copias que
comparten ``node_id``, ``causes`` y ``started_at`` con uno de ellos y los cierres que apuntan a
uno de ellos. Contar solo lo recibido hasta ``deadline`` hace que el resultado no dependa del
momento en que alguien mira (decisión del redactor de TASK-215).

**Criterio de ``verified``** (BR-GOB-41 leído con BR-GOB-99): al menos un evento contado de
sujeto ``camera`` de la cámara ocluida **y** el comportamiento de la zona esperado según
``coverage_state`` con las cámaras de la zona sin la ocluida: si la zona conserva la cobertura,
ningún evento de zona ``not_observable``; si la pierde (cámara requerida o por debajo de
``required_count``), ese evento tiene que aparecer.

**Orden de resolución** (``resolve``), determinista e idempotente:

1. ``verified`` si se cumple el criterio. Si la zona debe caer, basta verlo (un evento más no lo
   deshace); si debe conservarse, el silencio de la zona solo se afirma pasada ``deadline``
   (P2: nunca se afirma una ausencia antes de tiempo);
2. ``declared`` si se pidió con motivo antes de ``deadline`` y no hay eventos contados;
3. ``failed`` solo si ``now > deadline``: ``no_observability_events_in_window`` sin eventos
   contados, ``redundancy_not_verified`` si los hubo pero no cumplieron el criterio;
4. si no, sigue ``pending``.

``blocking_occlusions`` es la guarda del cierre del acta (BR-GOB-43, TASK-216): la última prueba
de cada cámara del catálogo; bloquea la que no tiene prueba, la ``failed`` y la ``pending`` con la
fecha límite sin vencer.

Funciones puras: ningún paso lee la hora del sistema.
"""

from __future__ import annotations

import dataclasses
import enum
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from vigia_contracts.models.enumerations import (
    DegradationCause,
    ObservabilityState,
    ObservabilitySubjectKind,
)

from vigia_platform.catalog.domain.coverage import MinimumCoverage, coverage_state
from vigia_platform.catalog.domain.enums import OcclusionVerification
from vigia_platform.catalog.domain.time_windows import utc_instant
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "MAX_CORRELATED_EVENTS",
    "MAX_WINDOW",
    "NODE_SCOPE_CAUSES",
    "NODE_SCOPE_SUBJECTS",
    "VERIFICATION_WAIT",
    "WINDOW_TOLERANCE",
    "FailureReason",
    "ObservedEvent",
    "OcclusionRuleViolated",
    "OcclusionTest",
    "OcclusionViolation",
    "Resolution",
    "blocking_occlusions",
    "check_window",
    "counted_events",
    "coverage_of_catalog",
    "latest_by_camera",
    "new_test",
    "resolve",
    "verified_criterion",
    "zone_drop_expected",
]

VERIFICATION_WAIT: Final = timedelta(minutes=5)
"""``deadline = ended_at + 5 min`` (NFR-GOB-44, `[objetivo propio]`)."""
WINDOW_TOLERANCE: Final = timedelta(seconds=30)
"""Tolerancia de ±30 s en los bordes de la ventana: absorbe el desfase de reloj del nodo."""
MAX_WINDOW: Final = timedelta(hours=1)
"""Duración máxima de la ventana de oclusión `[estimación propia]`: tapar una cámara dura segundos
o minutos; una ventana de horas correlacionaría cualquier evento del turno."""
MAX_CORRELATED_EVENTS: Final = 256
"""Tope de ``correlated_event_ids`` (el del registro ``occlusion_test_result``)."""

NODE_SCOPE_CAUSES: Final = frozenset(
    {
        DegradationCause.CLOCK_UNSYNCHRONIZED,
        DegradationCause.SIGNAL_READER_UNAVAILABLE,
        DegradationCause.LOCAL_QUEUE_OVER_THRESHOLD,
        DegradationCause.NODE_RESTART,
    }
)
"""Causas de alcance de nodo (pendiente nº 40): sus eventos no cuentan en la prueba."""
NODE_SCOPE_SUBJECTS: Final = frozenset(
    {
        ObservabilitySubjectKind.CLOCK,
        ObservabilitySubjectKind.SIGNAL_READER,
        ObservabilitySubjectKind.LOCAL_QUEUE,
    }
)
"""Sujetos de alcance de nodo: reloj, lector de señales y cola local."""


class OcclusionViolation(enum.StrEnum):
    """Por qué el dominio rechaza una petición de prueba de oclusión."""

    WINDOW_INVALID = "window_invalid"
    """``started_at < ended_at ≤ ahora`` o la duración máxima no se cumplen: ``invalid_request``."""
    CAMERA_NOT_IN_CATALOG = "camera_not_in_catalog"
    """La cámara no es del catálogo de la sesión: ``invalid_request``."""
    TEST_NOT_REPEATABLE = "test_not_repeatable"
    """La última prueba de la cámara no quedó ``failed``: ``conflict``."""
    DECLARATION_NOT_ADMITTED = "declaration_not_admitted"
    """Declaración con eventos contados, vencida la fecha límite o sobre una prueba resuelta."""


class OcclusionRuleViolated(Exception):
    """El dominio rechaza la petición; ``violation`` dice por qué."""

    def __init__(self, violation: OcclusionViolation) -> None:
        super().__init__(violation.value)
        self.violation = violation


class FailureReason(enum.StrEnum):
    """Por qué una prueba quedó ``failed`` (``detail_code`` de BLM §4.1, sin el prefijo)."""

    NO_OBSERVABILITY_EVENTS_IN_WINDOW = "no_observability_events_in_window"
    REDUNDANCY_NOT_VERIFIED = "redundancy_not_verified"


# --- Eventos -------------------------------------------------------------------------------------


def _instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("una marca del evento es texto ISO 8601")
    return utc_instant(datetime.fromisoformat(value))


def _optional_uuid(value: object) -> uuid.UUID | None:
    return None if value is None else uuid.UUID(str(value))


@dataclass(frozen=True, slots=True)
class ObservedEvent:
    """Lo que la prueba lee de un ``observability_event_received`` del expediente."""

    event_id: uuid.UUID
    node_id: uuid.UUID
    subject_kind: ObservabilitySubjectKind
    camera_id: uuid.UUID | None
    state: ObservabilityState
    causes: frozenset[DegradationCause]
    started_at: datetime
    received_at: datetime
    opened_event_id: uuid.UUID | None = None

    @classmethod
    def of_content(cls, content: Mapping[str, Any], received_at: datetime) -> ObservedEvent:
        """El evento desde el contenido del registro (``ObservabilityEvent`` con recibo).

        ``received_at`` es la marca del registro en el expediente. Un contenido sin la forma del
        contrato (que el escritor ya validó) es ``ValueError``: nunca se adivina.
        """
        try:
            subject = content["subject"]
            return cls(
                event_id=uuid.UUID(str(content["event_id"])),
                node_id=uuid.UUID(str(content["node_id"])),
                subject_kind=ObservabilitySubjectKind(subject["kind"]),
                camera_id=_optional_uuid(subject.get("camera_id")),
                state=ObservabilityState(content["state"]),
                causes=frozenset(DegradationCause(cause) for cause in content["causes"]),
                started_at=_instant(content["started_at"]),
                received_at=utc_instant(received_at),
                opened_event_id=_optional_uuid(content.get("opened_event_id")),
            )
        except (KeyError, TypeError, AttributeError) as error:
            raise ValueError("el contenido no es un evento de observabilidad") from error

    @property
    def copy_key(self) -> tuple[uuid.UUID, frozenset[DegradationCause], datetime]:
        """Las copias de un evento de nodo comparten ``node_id``, ``causes`` y ``started_at``."""
        return (self.node_id, self.causes, self.started_at)

    @property
    def node_scope(self) -> bool:
        """¿Es de alcance de nodo por su sujeto o porque todas sus causas lo son?"""
        if self.subject_kind in NODE_SCOPE_SUBJECTS:
            return True
        return bool(self.causes) and self.causes <= NODE_SCOPE_CAUSES

    def of_camera(self, camera_id: uuid.UUID) -> bool:
        return self.subject_kind is ObservabilitySubjectKind.CAMERA and self.camera_id == camera_id

    @property
    def zone_dropped(self) -> bool:
        """Evento de zona ``not_observable``."""
        return (
            self.subject_kind is ObservabilitySubjectKind.ZONE
            and self.state is ObservabilityState.NOT_OBSERVABLE
        )


# --- La prueba -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Resolution:
    """La resolución de una prueba ``pending``: se escribe una sola vez."""

    verification: OcclusionVerification
    correlated_event_ids: tuple[uuid.UUID, ...]
    declared_reason_es: str | None = None

    def __post_init__(self) -> None:
        if self.verification is OcclusionVerification.PENDING:
            raise ValueError("una resolución nunca es pending")
        if (self.verification is OcclusionVerification.DECLARED) != (
            self.declared_reason_es is not None
        ):
            raise ValueError("declared_reason_es solo y siempre en declared")
        if self.verification is OcclusionVerification.VERIFIED and not self.correlated_event_ids:
            raise ValueError("verified exige eventos correlacionados")
        if len(self.correlated_event_ids) > MAX_CORRELATED_EVENTS:
            raise ValueError("demasiados eventos correlacionados")


@dataclass(frozen=True, slots=True, kw_only=True)
class OcclusionTest:
    """``OcclusionTest`` ⛓ (DE §2.14 con su nota del 2026-09-20)."""

    test_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    session_id: uuid.UUID
    camera_id: uuid.UUID
    started_at: datetime
    ended_at: datetime
    deadline: datetime
    verification: OcclusionVerification
    correlated_event_ids: tuple[uuid.UUID, ...] | None
    declared_reason_es: str | None
    recorded_by: uuid.UUID
    ledger_record_id: uuid.UUID | None = None

    @property
    def resolved(self) -> bool:
        return self.verification is not OcclusionVerification.PENDING

    @property
    def resolution(self) -> Resolution | None:
        if not self.resolved:
            return None
        return Resolution(
            verification=self.verification,
            correlated_event_ids=self.correlated_event_ids or (),
            declared_reason_es=self.declared_reason_es,
        )

    @property
    def failure_reason(self) -> FailureReason | None:
        if self.verification is not OcclusionVerification.FAILED:
            return None
        if self.correlated_event_ids:
            return FailureReason.REDUNDANCY_NOT_VERIFIED
        return FailureReason.NO_OBSERVABILITY_EVENTS_IN_WINDOW

    def resolved_with(self, resolution: Resolution, ledger_record_id: uuid.UUID) -> OcclusionTest:
        """La prueba con su resolución y el registro que la respalda; solo desde ``pending``."""
        if self.resolved:
            raise ValueError("la prueba ya está resuelta")
        return dataclasses.replace(
            self,
            verification=resolution.verification,
            correlated_event_ids=resolution.correlated_event_ids,
            declared_reason_es=resolution.declared_reason_es,
            ledger_record_id=ledger_record_id,
        )

    def record_content(self) -> dict[str, Any]:
        """Contenido de ``occlusion_test_result`` (``source_key = test_id``)."""
        content: dict[str, Any] = {
            "test_id": str(self.test_id),
            "session_id": str(self.session_id),
            "camera_id": str(self.camera_id),
            "started_at": format_timestamp(self.started_at),
            "ended_at": format_timestamp(self.ended_at),
            "deadline": format_timestamp(self.deadline),
            "verification": OcclusionVerification(self.verification).value,
            "correlated_event_ids": [str(e) for e in self.correlated_event_ids or ()],
        }
        if self.declared_reason_es is not None:
            content["declared_reason_es"] = self.declared_reason_es
        return content


def check_window(started_at: object, ended_at: object, now: datetime) -> tuple[datetime, datetime]:
    """``started_at < ended_at ≤ now`` y a lo sumo ``MAX_WINDOW``, en UTC al milisegundo; si no,
    ``window_invalid``."""
    if not isinstance(started_at, datetime) or not isinstance(ended_at, datetime):
        raise OcclusionRuleViolated(OcclusionViolation.WINDOW_INVALID)
    try:
        start, end = utc_instant(started_at), utc_instant(ended_at)
    except (ValueError, OverflowError):
        raise OcclusionRuleViolated(OcclusionViolation.WINDOW_INVALID) from None
    if not start < end <= utc_instant(now) or end - start > MAX_WINDOW:
        raise OcclusionRuleViolated(OcclusionViolation.WINDOW_INVALID)
    return start, end


def new_test(
    *,
    test_id: uuid.UUID,
    organization_id: uuid.UUID,
    plant_id: uuid.UUID,
    session_id: uuid.UUID,
    camera_id: uuid.UUID,
    started_at: datetime,
    ended_at: datetime,
    recorded_by: uuid.UUID,
) -> OcclusionTest:
    """La prueba recién registrada: ``pending`` con ``deadline = ended_at + 5 min``."""
    return OcclusionTest(
        test_id=test_id,
        organization_id=organization_id,
        plant_id=plant_id,
        session_id=session_id,
        camera_id=camera_id,
        started_at=utc_instant(started_at),
        ended_at=utc_instant(ended_at),
        deadline=utc_instant(ended_at) + VERIFICATION_WAIT,
        verification=OcclusionVerification.PENDING,
        correlated_event_ids=None,
        declared_reason_es=None,
        recorded_by=recorded_by,
    )


# --- Correlación ---------------------------------------------------------------------------------


def counted_events(
    test: OcclusionTest, events: Iterable[ObservedEvent]
) -> tuple[ObservedEvent, ...]:
    """Los eventos que cuentan en la prueba, sin repetidos y en orden (``started_at``, id)."""
    candidates = {event.event_id: event for event in events}.values()
    node_scope = [event for event in candidates if event.node_scope]
    node_keys = {event.copy_key for event in node_scope}
    node_ids = {event.event_id for event in node_scope}
    lower = test.started_at - WINDOW_TOLERANCE
    upper = test.ended_at + WINDOW_TOLERANCE
    counted = [
        event
        for event in candidates
        if not event.node_scope
        and event.copy_key not in node_keys
        and event.opened_event_id not in node_ids
        and lower <= event.started_at <= upper
        and event.received_at <= test.deadline
    ]
    return tuple(sorted(counted, key=lambda event: (event.started_at, event.event_id)))


def zone_drop_expected(coverage: MinimumCoverage, camera_id: uuid.UUID) -> bool:
    """¿Ocluir ``camera_id`` deja la zona ``not_observable`` (BR-GOB-97 y 99)?"""
    remaining = [camera for camera in coverage.camera_ids if camera != camera_id]
    return coverage_state(coverage, remaining) is ObservabilityState.NOT_OBSERVABLE


def verified_criterion(
    camera_id: uuid.UUID, counted: Iterable[ObservedEvent], coverage: MinimumCoverage
) -> bool:
    """BR-GOB-41 con BR-GOB-99: evento de la cámara y la zona como manda la cobertura."""
    events = tuple(counted)
    camera_seen = any(event.of_camera(camera_id) for event in events)
    zone_dropped = any(event.zone_dropped for event in events)
    return camera_seen and zone_dropped == zone_drop_expected(coverage, camera_id)


def _correlated(camera_id: uuid.UUID, counted: tuple[ObservedEvent, ...]) -> tuple[uuid.UUID, ...]:
    """Los eventos contados: primero los de la cámara, después los de zona ``not_observable`` y
    después los demás, cada grupo en su orden; como mucho ``MAX_CORRELATED_EVENTS``."""

    def group(event: ObservedEvent) -> int:
        if event.of_camera(camera_id):
            return 0
        return 1 if event.zone_dropped else 2

    ordered = sorted(counted, key=lambda e: (group(e), e.started_at, e.event_id))
    return tuple(event.event_id for event in ordered[:MAX_CORRELATED_EVENTS])


def resolve(
    test: OcclusionTest,
    events: Iterable[ObservedEvent],
    coverage: MinimumCoverage | None,
    now: datetime,
    declared_reason_es: str | None = None,
) -> Resolution | None:
    """La resolución de la prueba con estos eventos en ``now``, o ``None`` si sigue ``pending``.

    Una prueba ya resuelta devuelve su resolución (monotonía). Sin cobertura legible el criterio no
    se cumple nunca (falla cerrado: ``verified`` no sale de un catálogo que no se entiende).
    """
    if test.resolved:
        return test.resolution
    moment = utc_instant(now)
    expired = moment > test.deadline
    counted = counted_events(test, events)
    correlated = _correlated(test.camera_id, counted)
    # Con la zona que debe caer, verlo basta; con la que debe conservarse, su silencio solo se
    # afirma vencida la fecha límite.
    if (
        coverage is not None
        and verified_criterion(test.camera_id, counted, coverage)
        and (expired or zone_drop_expected(coverage, test.camera_id))
    ):
        return Resolution(OcclusionVerification.VERIFIED, correlated)
    if declared_reason_es is not None and not expired and not counted:
        return Resolution(OcclusionVerification.DECLARED, (), declared_reason_es)
    if expired:
        return Resolution(OcclusionVerification.FAILED, correlated)
    return None


def coverage_of_catalog(catalog: Mapping[str, Any]) -> MinimumCoverage | None:
    """La cobertura mínima del catálogo, o ``None`` si su forma no se entiende."""
    try:
        return MinimumCoverage.of_catalog(catalog)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


# --- Guarda del acta -----------------------------------------------------------------------------


def latest_by_camera(tests: Iterable[OcclusionTest]) -> dict[uuid.UUID, OcclusionTest]:
    """La última prueba de cada cámara (``test_id`` es UUID v7: orden de registro)."""
    latest: dict[uuid.UUID, OcclusionTest] = {}
    for test in tests:
        current = latest.get(test.camera_id)
        if current is None or test.test_id.int > current.test_id.int:
            latest[test.camera_id] = test
    return latest


def blocking_occlusions(
    tests: Iterable[OcclusionTest], cameras: Iterable[uuid.UUID], now: datetime
) -> tuple[uuid.UUID, ...]:
    """Las cámaras que impiden cerrar el acta (``redundancy_not_verified``, BR-GOB-43).

    ``tests`` son las pruebas de la sesión ya reevaluadas; ``cameras``, las del catálogo de la
    zona. Bloquea la cámara sin prueba, con su última prueba ``failed`` o ``pending`` sin la fecha
    límite vencida; una ``failed`` anterior seguida de otra resuelta no bloquea (la última manda).
    """
    moment = utc_instant(now)
    latest = latest_by_camera(tests)
    blocking: list[uuid.UUID] = []
    for camera in dict.fromkeys(cameras):
        test = latest.get(camera)
        if (
            test is None
            or test.verification is OcclusionVerification.FAILED
            or (test.verification is OcclusionVerification.PENDING and moment <= test.deadline)
        ):
            blocking.append(camera)
    return tuple(blocking)
