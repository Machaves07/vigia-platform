"""Generador ``coverage_inputs`` y oráculo de ``compuesto(t)`` para la línea de tiempo (TASK-120).

``coverage_inputs`` produce un periodo ``[a, b)`` y entradas de cobertura de una zona: eventos de
observabilidad de la zona y de otros sujetos (aperturas, pares, cierres huérfanos que restauran o
no, aperturas sin cierre, inicios empatados), cambios de comunicación de dos nodos (con ``mute``
que empieza en su último latido, antes o después de otras declaraciones), cambios de compuerta y
asignaciones de nodo (con huecos y solapes). Las marcas caen en una rejilla gruesa (1 ms, 1 s o
1 min) para que los empates y los bordes del periodo aparezcan a menudo, y hasta 20 pasos fuera
del periodo por cada lado. El periodo cruza a menudo la medianoche y el cambio de mes (``T0``) y
las marcas llegan con desfases distintos de UTC (seguimiento de VIG-64).

``oracle`` es la lectura literal del §7 del modelo lógico, escrita aparte de
``vigia_platform.ledger.domain.coverage``: devuelve el compuesto y las tres lecturas de capa en
un milisegundo.

Solo datos generados.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from hypothesis import strategies as st

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
)

T0 = datetime(2026, 9, 1, tzinfo=UTC)
MS = timedelta(milliseconds=1)

ZONE = Subject("zone")
CAMERA_A = Subject("camera", camera_id=uuid.UUID("3f2b8c9d-4e5f-4a6b-8c7d-9e0f1a2b3c5a"))
CAMERA_B = Subject("camera", camera_id=uuid.UUID("3f2b8c9d-4e5f-4a6b-8c7d-9e0f1a2b3c5b"))
CLOCK = Subject("clock")
SUBJECTS = (ZONE, ZONE, ZONE, CAMERA_A, CAMERA_B, CLOCK)
"""Tres de cada seis eventos son del sujeto ``zone``: el que decide el compuesto."""

NODES = (uuid.UUID(int=0x1), uuid.UUID(int=0x2))
CAUSES = ("focus", "obstruction", "node_restart", "camera_heartbeat_lost")


def at(ms: int) -> datetime:
    return T0 + ms * MS


_at = at


def ms_of(moment: datetime) -> int:
    return (moment - T0) // MS


@dataclass(frozen=True)
class Scenario:
    inputs: CoverageInputs
    period: CoveragePeriod
    unit: int
    """Paso de la rejilla, en milisegundos."""

    @property
    def a(self) -> int:
        return ms_of(self.period.start)

    @property
    def b(self) -> int:
        return ms_of(self.period.end)


def states_and_causes() -> st.SearchStrategy[tuple[CoverageState, tuple[str, ...]]]:
    return st.one_of(
        st.just((CoverageState.OBSERVABLE, ())),
        st.tuples(
            st.sampled_from([CoverageState.DEGRADED, CoverageState.NOT_OBSERVABLE]),
            st.lists(st.sampled_from(CAUSES), min_size=1, max_size=3, unique=True).map(tuple),
        ),
    )


ZONES = (
    UTC,
    timezone(timedelta(hours=-5)),  # America/Bogota
    timezone(timedelta(hours=5, minutes=30)),
    timezone(timedelta(hours=14)),
    timezone(timedelta(hours=-12)),
)
"""Desfases con que llegan las marcas: el mismo instante escrito en otra zona horaria."""


@st.composite
def coverage_inputs(draw: st.DrawFn, *, max_events: int = 12) -> Scenario:
    """Escenario generado. ``T0`` es medianoche UTC del 1 de septiembre: el periodo puede empezar
    hasta ``span`` pasos antes, así que cruza a menudo la medianoche y el cambio de mes, y cada
    marca llega con un desfase de ``ZONES`` (seguimiento de VIG-64)."""
    unit = draw(st.sampled_from([1, 1_000, 60_000, 3_600_000]))
    span = draw(st.integers(min_value=1, max_value=120))
    a = draw(st.integers(min_value=-span, max_value=50)) * unit
    b = a + span * unit
    moment = st.integers(min_value=-20, max_value=span + 20).map(lambda k: a + k * unit)
    ids = iter(draw(st.lists(st.uuids(), min_size=80, max_size=80, unique=True)))

    def at(ms: int) -> datetime:
        return _at(ms).astimezone(draw(st.sampled_from(ZONES)))

    assignments = []
    for _ in range(draw(st.integers(min_value=0, max_value=3))):
        start = draw(moment)
        length = draw(st.one_of(st.none(), st.integers(min_value=1, max_value=60)))
        end = None if length is None else start + length * unit
        assignments.append(
            AssignmentInput(
                next(ids), draw(st.sampled_from(NODES)), at(start), None if end is None else at(end)
            )
        )

    communication = []
    for _ in range(draw(st.integers(min_value=0, max_value=8))):
        since = draw(moment)
        heartbeat = draw(st.one_of(st.none(), moment))
        communication.append(
            CommunicationInput(
                next(ids),
                draw(st.sampled_from(NODES)),
                draw(st.sampled_from(list(CommunicationState))),
                at(since),
                None if heartbeat is None else at(heartbeat),
            )
        )

    gates = [
        GateInput(next(ids), at(draw(moment)), draw(st.sampled_from(list(ZoneMode))))
        for _ in range(draw(st.integers(min_value=0, max_value=5)))
    ]

    observability: list[ObservabilityInput] = []
    for _ in range(draw(st.integers(min_value=0, max_value=max_events))):
        subject = draw(st.sampled_from(SUBJECTS))
        start = draw(moment)
        state, causes = draw(states_and_causes())
        opened = ObservabilityInput(
            next(ids),
            next(ids),
            subject,
            EventPhase.OPENED,
            state,
            causes,
            at(start),
            clock_offset_ms=draw(st.one_of(st.none(), st.integers(-5_000, 5_000))),
        )
        shape = draw(st.sampled_from(["open", "pair", "pair", "orphan", "double_close"]))
        if shape != "orphan":
            observability.append(opened)
        if shape == "open":
            continue
        closes = 2 if shape == "double_close" else 1
        for _ in range(closes):
            end = start + draw(st.integers(min_value=0, max_value=40)) * unit
            close_state, close_causes = draw(states_and_causes())
            observability.append(
                ObservabilityInput(
                    next(ids),
                    next(ids),
                    subject,
                    EventPhase.CLOSED,
                    close_state,
                    close_causes,
                    at(start),
                    ended_at=at(end),
                    opened_event_id=opened.event_id,
                )
            )

    first_report = draw(st.one_of(st.none(), moment))
    return Scenario(
        CoverageInputs(
            observability=tuple(observability),
            communication=tuple(communication),
            gates=tuple(gates),
            assignments=tuple(assignments),
            first_report_at=None if first_report is None else at(first_report),
        ),
        CoveragePeriod(at(a), at(b)),
        unit,
    )


def with_inputs(scenario: Scenario, **changes: Any) -> Scenario:
    return replace(scenario, inputs=replace(scenario.inputs, **changes))


# --- Oráculo: el §7 literal -------------------------------------------------------------------


@dataclass(frozen=True)
class Reading:
    """``compuesto(t)`` y lo que cada capa decía en ``t``."""

    state: str
    causes: tuple[str, ...]
    node_assigned: bool
    communication: str
    mode: str
    node_state: str | None
    """``None`` = desconocido (el nodo nunca reportó hasta ``t``)."""


def oracle(inputs: CoverageInputs, t: int) -> Reading:
    # Nodo asignado: la asignación que contiene t (con solapes, la última asignada).
    containing = [
        r
        for r in inputs.assignments
        if ms_of(r.assigned_at) <= t and (r.unassigned_at is None or t < ms_of(r.unassigned_at))
    ]
    node = max(containing, key=lambda r: (ms_of(r.assigned_at), r.assignment_id.int), default=None)

    # Comunicación: la última declaración (por since) cuyo silencio o alcance ya empezó en t.
    def effective(c: CommunicationInput) -> int:
        if c.state is CommunicationState.MUTE and c.last_heartbeat_at is not None:
            return ms_of(c.last_heartbeat_at)
        return ms_of(c.since)

    declared = [
        c
        for c in inputs.communication
        if node is not None and c.node_id == node.node_id and effective(c) <= t
    ]
    last = max(declared, key=lambda c: (ms_of(c.since), c.record_id.int), default=None)
    communication = "unknown" if last is None else last.state.value

    gate = max(
        (g for g in inputs.gates if ms_of(g.at) <= t),
        key=lambda g: (ms_of(g.at), g.record_id.int),
        default=None,
    )
    mode = "no_capture" if gate is None else gate.resulting_mode.value

    # Estado del nodo para el sujeto zone.
    opens = {e.event_id: e for e in inputs.observability if e.phase is EventPhase.OPENED}
    starts: list[tuple[int, int, str, tuple[str, ...], int | None]] = []
    for event in inputs.observability:
        if event.subject.kind != "zone":
            continue
        if event.phase is EventPhase.OPENED:
            ends = [
                ms_of(c.ended_at)
                for c in inputs.observability
                if c.phase is EventPhase.CLOSED
                and c.opened_event_id == event.event_id
                and c.ended_at is not None
            ]
            starts.append(
                (
                    ms_of(event.started_at),
                    event.event_id.int,
                    event.state.value,
                    event.causes,
                    min(ends, default=None),
                )
            )
        elif event.opened_event_id not in opens and event.ended_at is not None:
            restored = event.state is CoverageState.OBSERVABLE
            starts.append(
                (
                    ms_of(event.started_at),
                    event.event_id.int,
                    "not_observable" if restored else event.state.value,
                    event.causes,
                    ms_of(event.ended_at),
                )
            )
    current = max((s for s in starts if s[0] <= t), default=None)
    reported = any(ms_of(e.started_at) <= t for e in inputs.observability) or (
        inputs.first_report_at is not None and ms_of(inputs.first_report_at) <= t
    )
    node_state: str | None
    node_causes: tuple[str, ...] = ()
    if current is not None and (current[4] is None or t < current[4]):
        node_state, node_causes = current[2], current[3]
    elif reported:
        node_state = "observable"
    else:
        node_state = None

    state: str
    causes: tuple[str, ...]
    if node is None or communication == "unknown":
        state, causes = "not_observable", ("never_reported",)
    elif mode != "productive":
        state, causes = "not_observable", ("zone_not_active",)
    elif communication == "mute":
        state, causes = "not_observable", ("no_communication",)
    elif node_state is None:
        state, causes = "not_observable", ("never_reported",)
    else:
        state, causes = node_state, node_causes
    return Reading(state, causes, node is not None, communication, mode, node_state)
