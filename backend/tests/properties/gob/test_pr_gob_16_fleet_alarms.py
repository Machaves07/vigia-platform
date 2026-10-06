"""PR-GOB-16: alarmas de flota por transición (TASK-225; BR-GOB-81; NFR-GOB-45, 47), contra
PostgreSQL 16 real.

``RuleBasedStateMachine`` (perfil ``ci``, semilla fija y semilla de la sesión registradas por
``tests/conftest.py``) sobre la ruta real ``POST heartbeats`` (VIG-157) y las tres tareas reales:
cada ejemplo es un nodo nuevo dado de alta (intervalo de 60 s). Las reglas generan
``heartbeat_sequences`` con huecos y con valores que **oscilan alrededor de cada umbral** (cola
99/100/101, reloj 4 999/5 000/5 001 y -5 001 ms, cámaras 4,9/5,0/5,1 fps sobre un mínimo de 5),
silencios largos, la credencial vigente por vencer o rotada, y ejecutan ``evaluate_fleet_alarms``
(un ciclo de 60 s después de la anterior), ``detect_mute_nodes`` y
``alert_expiring_certificates`` en cualquier orden.

El modelo es un oráculo escrito aparte (sin usar ``alarm_hysteresis``): dos evaluaciones
consecutivas iguales para entrar y salir de cola, reloj y cámara; ``node_mute`` con la marca de
mudo y baja al volver; ``certificate_expiring`` en la primera pasada diaria y baja tras la
rotación. Invariantes:

- las alarmas abiertas de la base son las del modelo, y cada evaluación levanta y baja
  exactamente lo que el modelo espera (una oscilación de un solo ciclo no produce alarma);
- por clase y nodo, **entre un ``raised`` y su ``cleared`` nunca hay otro ``raised``**: las filas
  no se solapan, a lo sumo una abierta, un evento ``fleet_alarm_raised`` por fila y uno
  ``fleet_alarm_cleared`` por fila cerrada;
- ``node_mute`` y ``certificate_expiring`` se levantan en la primera pasada que ve su condición.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import datetime as dt
import itertools
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, ClassVar, Final

import pytest
from hypothesis import seed as hypothesis_seed
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
    run_state_machine_as_test,
)

from tests.conftest import _seeds_for_profile
from tests.fleet_alarm_support import AlarmRows, AlarmTasks, alarm_tasks, run_in
from tests.heartbeat_support import HeartbeatStack, NodeSite, heartbeat_stack
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.shared.signing.keys import to_millisecond

pytestmark = pytest.mark.integration

STEPS: Final = 15
INTERVAL: Final = 60
CYCLE: Final = 60
CONFIRMED: Final = ("queue_over_threshold", "clock_drift", "camera_below_min_fps")
DAY: Final = dt.timedelta(days=1)


@dataclass
class Readings:
    pending: int
    offset_ms: int
    fps: float

    def conditions(self) -> dict[str, bool]:
        """Las tres condiciones con histéresis con los umbrales por defecto (100, 5 000, 5,0)."""
        return {
            "queue_over_threshold": self.pending > 100,
            "clock_drift": abs(self.offset_ms) > 5_000,
            "camera_below_min_fps": self.fps < 5.0,
        }


@dataclass
class Model:
    readings: Readings | None = None
    streak: dict[str, tuple[bool, int]] = field(default_factory=dict)
    """Por clase con histéresis: el valor observado y cuántas evaluaciones seguidas lo repiten."""
    open: set[str] = field(default_factory=set)
    state: str = "unknown"
    last_heartbeat_at: dt.datetime | None = None
    certificate_expiring: bool = False


class FleetAlarms(RuleBasedStateMachine):
    stack: ClassVar[HeartbeatStack]
    tasks: ClassVar[AlarmTasks]

    def __init__(self) -> None:
        super().__init__()
        self.model = Model()
        self.site: NodeSite | None = None
        self.rows = AlarmRows(self.stack.fetch)

    @initialize()
    def declare(self) -> None:
        self.stack.tick(CYCLE)
        self.site = self.stack.site(interval=INTERVAL)

    def _run(self, handler: Any) -> Any:
        assert self.site is not None
        database = self.stack.primary.database
        return self.stack.run(run_in(database, self.site.organization_id, handler))

    # --- Latidos con valores alrededor de cada umbral ------------------------------------------

    @rule(
        gap=st.integers(min_value=1, max_value=400),
        pending=st.sampled_from([0, 99, 100, 101, 500]),
        offset_ms=st.sampled_from([0, 4_999, 5_000, 5_001, -5_000, -5_001]),
        fps=st.sampled_from([4.9, 5.0, 5.1, 12.0]),
    )
    def heartbeat(self, gap: int, pending: int, offset_ms: int, fps: float) -> None:
        stack, site = self.stack, self.site
        assert site is not None
        stack.tick(gap)
        body = stack.body(site)
        body["local_queue"] = {"pending": pending, "dead_letter": [], "retained_sent": 10}
        body["node_clock"] = {"synchronized": True, "offset_ms": offset_ms, "source": "ntp_local"}
        for camera in body["cameras"]:
            camera["measured_fps"] = fps
        received = to_millisecond(stack.now())
        response = stack.post(site, body)
        assert response.status_code == 200, response.text
        model = self.model
        model.readings = Readings(pending, offset_ms, fps)
        model.state = "reachable"
        model.last_heartbeat_at = received

    @rule(seconds=st.sampled_from([30, 299, 301, 900, 3_600]))
    def silence(self, seconds: int) -> None:
        self.stack.tick(seconds)

    @rule(expiring=st.booleans())
    def certificate(self, expiring: bool) -> None:
        """La credencial vigente vence en 10 días (por vencer) o en un año (rotada)."""
        assert self.site is not None
        expires = self.stack.now() + (10 * DAY if expiring else 365 * DAY)
        self.stack.execute(
            "WITH old AS (DELETE FROM fleet.node_credential WHERE node_id = $1 RETURNING *)"
            " INSERT INTO fleet.node_credential (credential_id, organization_id, plant_id,"
            " node_id, certificate_serial, subject, key_algorithm, issued_at, expires_at, status,"
            " rotated_from, revoked_at) SELECT credential_id, organization_id, plant_id, node_id,"
            " certificate_serial, subject, key_algorithm, issued_at, $2, status, rotated_from,"
            " revoked_at FROM old",
            self.site.node_id,
            expires,
        )
        self.model.certificate_expiring = expiring

    # --- Las tareas reales --------------------------------------------------------------------

    @rule()
    def evaluate_fleet_alarms(self) -> None:
        self.stack.tick(CYCLE)
        report = self._run(self.tasks.evaluator.evaluate)
        model = self.model
        raised: set[str] = set()
        cleared: set[str] = set()
        conditions = (
            model.readings.conditions() if model.readings else dict.fromkeys(CONFIRMED, False)
        )
        for kind in CONFIRMED:
            condition = conditions[kind]
            observed, count = model.streak.get(kind, (not condition, 0))
            count = count + 1 if observed is condition else 1
            model.streak[kind] = (condition, min(count, 2))
            if count >= 2 and condition and kind not in model.open:
                raised.add(kind)
            if count >= 2 and not condition and kind in model.open:
                cleared.add(kind)
        if "node_mute" in model.open and model.state == "reachable":
            cleared.add("node_mute")
        if "certificate_expiring" in model.open and not model.certificate_expiring:
            cleared.add("certificate_expiring")
        assert {a.alarm_kind.value for a in report.raised} == raised
        assert {a.alarm_kind.value for a in report.cleared} == cleared
        model.open = (model.open - cleared) | raised

    @rule()
    def detect_mute_nodes(self) -> None:
        report = self._run(self.tasks.detector.detect)
        model = self.model
        silent = model.last_heartbeat_at is not None and to_millisecond(
            self.stack.now()
        ) - model.last_heartbeat_at > dt.timedelta(seconds=5 * INTERVAL)
        transition = silent and model.state == "reachable"
        assert len(report.transitions) == int(transition)
        if transition:
            model.state = "mute"
        opens = model.state == "mute" and silent and "node_mute" not in model.open
        assert {a.alarm_kind.value for a in report.raised} == ({"node_mute"} if opens else set())
        if opens:
            model.open.add("node_mute")

    @rule()
    def alert_expiring_certificates(self) -> None:
        report = self._run(self.tasks.alerter.alert)
        model = self.model
        opens = model.certificate_expiring and "certificate_expiring" not in model.open
        assert {a.alarm_kind.value for a in report.raised} == (
            {"certificate_expiring"} if opens else set()
        )
        if opens:
            model.open.add("certificate_expiring")

    # --- Invariantes --------------------------------------------------------------------------

    @invariant()
    def the_open_alarms_are_the_model(self) -> None:
        if self.site is None:
            return
        assert self.rows.open_kinds(self.site.node_id) == self.model.open
        assert self.rows.slots(self.site.node_id) == self.model.open

    @invariant()
    def never_a_second_raised_before_its_cleared(self) -> None:
        if self.site is None:
            return
        node = self.site.node_id
        alarms = self.rows.alarms(node)
        for kind, group in itertools.groupby(
            sorted(alarms, key=lambda a: (a["alarm_kind"], a["raised_at"], a["alarm_id"])),
            key=lambda a: a["alarm_kind"],
        ):
            rows = list(group)
            assert sum(row["cleared_at"] is None for row in rows) <= 1, kind
            for first, second in itertools.pairwise(rows):
                assert first["cleared_at"] is not None, kind
                assert first["cleared_at"] <= second["raised_at"], kind
        raised = self.rows.events("fleet_alarm_raised", node)
        cleared = self.rows.events("fleet_alarm_cleared", node)
        assert sorted(e["event_id"] for e in raised) == sorted(a["raised_event_id"] for a in alarms)
        assert sorted(e["event_id"] for e in cleared) == sorted(
            a["cleared_event_id"] for a in alarms if a["cleared_event_id"] is not None
        )


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[HeartbeatStack]:
    with heartbeat_stack(postgres_endpoint, "pr_gob_16") as built:
        built.authz.sessions.clock.set(to_millisecond(built.now()))
        yield built


def test_pr_gob_16_alarms_are_published_by_transition(stack: HeartbeatStack) -> None:
    sessions = stack.authz.sessions
    FleetAlarms.stack = stack
    FleetAlarms.tasks = alarm_tasks(
        clock=sessions.clock, outbox=stack.outbox, writer=stack.writer, audit=sessions.audit
    )
    for value in _seeds_for_profile():
        seeded = hypothesis_seed(value)(FleetAlarms)
        run_state_machine_as_test(seeded, settings=settings(stateful_step_count=STEPS))
