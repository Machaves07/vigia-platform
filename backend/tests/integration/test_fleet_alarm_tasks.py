"""Alarmas de flota por transición: las tres tareas y ``GET /fleet/alarms`` (TASK-225).

Contra PostgreSQL 16 real como ``vigia_app`` (``fleet_stack`` con la bandeja de las alarmas y la
aplicación real), con el estado de cada nodo escrito por el superusuario (``InventoryWorld``) y el
reloj simulado avanzando un ciclo (60 s) entre evaluaciones. Criterios de TASK-225:

- histéresis de dos evaluaciones para entrar y salir de ``queue_over_threshold``, ``clock_drift`` y
  ``camera_below_min_fps`` (bordes 100/101, 5 000/5 001 ms, 4,9/5,0 fps); una oscilación de un
  ciclo no alarma (NFR-GOB-45);
- ``version_retiring`` solo con ``retires_at``; ``simulated_adapter_in_productive`` con
  ``simulated`` o ``file`` y una zona ``productive`` deja su alerta en la auditoría (BR-GOB-78,
  80; NFR-GOB-36);
- ``orphan_clips_growing``: 51 huérfanos al momento, el 5 % sostenido 24 h, salida tras 24 h; los
  de verificación no cuentan (BR-GOB-94);
- un nodo revocado o dado de baja no levanta nada y sus alarmas abiertas se cierran una vez
  (BR-GOB-76);
- ``detect_mute_nodes``: **más de** cinco intervalos, el silencio empieza en ``last_heartbeat_at``;
  un nodo que nunca latió sigue ``unknown`` (BR-GOB-74);
- ``alert_expiring_certificates`` a 15 días; la rotación la baja (BR-GOB-65);
- eventos con solo identificadores, la clase y marcas, en la misma transacción que la fila;
- guardas de alcance: cada tarea toca solo su organización, y sus sentencias filtran por
  organización también sin RLS (superusuario); ``GET /fleet/alarms`` de otra organización o planta
  responde ``not_found`` (PR-GOB-12, NFR-GOB-30);
- número de sentencias acotado: no crece con los nodos.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import pytest

from tests.fleet_alarm_support import AlarmRows, AlarmTasks, alarm_tasks, kinds, run_in, system
from tests.fleet_http_support import FleetStack, fleet_stack
from tests.fleet_inventory_support import InventoryWorld, NodeState, as_superuser, counting
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.fleet.adapters.postgres import fleet_alarm_store
from vigia_platform.fleet.application.fleet_alarms import AlarmReport
from vigia_platform.fleet.domain.communication_state import MUTE_FACTOR
from vigia_platform.shared.context import ScopeLevel
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

pytestmark = pytest.mark.integration

CYCLE: Final = 60
DAY: Final = timedelta(days=1)
RETIRES_AT: Final = "2027-01-31T00:00:00.000Z"
STAMP: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
QUIET: Final = NodeState()


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[FleetStack]:
    with fleet_stack(postgres_endpoint, "fleet_alarm_tasks", alarm_events=True) as built:
        yield built


@dataclass
class Alarms:
    stack: FleetStack
    world: InventoryWorld
    tasks: AlarmTasks
    rows: AlarmRows

    def now(self) -> datetime:
        return self.world.now()

    def cycle(self, seconds: float = CYCLE) -> None:
        self.stack.tick(seconds)

    def node(self, plant: int = 0, zones: int = 0) -> uuid.UUID:
        plant_id = self.world.plants[plant]
        return self.world.add_node(plant_id, self.world.zones(plant_id)[:zones])

    def state(self, node: uuid.UUID, state: NodeState) -> None:
        self.world.write_state(node, state, self.now())

    def evaluate(self, world: InventoryWorld | None = None) -> AlarmReport:
        organization = (world or self.world).organization
        report: AlarmReport = self.stack.run(
            run_in(self.stack.database, organization, self.tasks.evaluator.evaluate)
        )
        return report

    def detect(self, world: InventoryWorld | None = None) -> AlarmReport:
        organization = (world or self.world).organization
        report: AlarmReport = self.stack.run(
            run_in(self.stack.database, organization, self.tasks.detector.detect)
        )
        return report

    def alert(self, world: InventoryWorld | None = None) -> AlarmReport:
        organization = (world or self.world).organization
        report: AlarmReport = self.stack.run(
            run_in(self.stack.database, organization, self.tasks.alerter.alert)
        )
        return report

    def other(self) -> InventoryWorld:
        return InventoryWorld.build(self.stack)


@pytest.fixture
def alarms(stack: FleetStack) -> Alarms:
    assert stack.outbox is not None
    sessions = stack.authz.sessions
    # Cada prueba arranca en la hora de la base (las de 24 h adelantan el reloj del módulo y la RLS
    # de las concesiones compara con ella; retro 14), al milisegundo como las marcas de las tareas.
    (row,) = stack.fetch("SELECT now() AS now")
    sessions.clock.set(to_millisecond(row["now"]))
    tasks = alarm_tasks(
        clock=sessions.clock, outbox=stack.outbox, writer=stack.writer, audit=sessions.audit
    )
    return Alarms(stack, InventoryWorld.build(stack), tasks, AlarmRows(stack.fetch))


def _stamp(moment: datetime) -> str:
    return format_timestamp(moment)


# --- Histéresis de dos evaluaciones ---------------------------------------------------------------

CONFIRMED: Final = {
    "queue_over_threshold": (NodeState(pending=101), NodeState(pending=100)),
    "queue_over_threshold_by_age": (
        NodeState(pending=1, oldest_pending_age_ms=30 * 60_000 + 1),
        NodeState(pending=1, oldest_pending_age_ms=30 * 60_000),
    ),
    "clock_drift": (NodeState(offset_ms=5_001), NodeState(offset_ms=5_000)),
    "clock_drift_negative": (NodeState(offset_ms=-5_001), NodeState(offset_ms=-5_000)),
    "camera_below_min_fps": (
        NodeState(cameras=((4.9, 5.0), (12.0, 5.0))),
        NodeState(cameras=((5.0, 5.0), (12.0, 5.0))),
    ),
}


def _kind(case: str) -> str:
    return case.removesuffix("_by_age").removesuffix("_negative")


@pytest.mark.parametrize("case", sorted(CONFIRMED))
def test_confirmed_kinds_raise_on_the_second_evaluation_and_clear_on_the_second_without(
    alarms: Alarms, case: str
) -> None:
    kind = _kind(case)
    bad, good = CONFIRMED[case]
    node = alarms.node()
    alarms.state(node, bad)
    first = alarms.now()
    assert alarms.evaluate().raised == []
    assert alarms.rows.evaluations(node)[kind]["consecutive"] == 1
    alarms.cycle()
    alarms.state(node, bad)
    report = alarms.evaluate()
    assert [alarm.alarm_kind.value for alarm in report.raised] == [kind]
    (row,) = alarms.rows.alarms(node, kind)
    assert row["cleared_at"] is None and row["raised_at"] == alarms.now()
    (raised,) = alarms.rows.events("fleet_alarm_raised", node)
    assert raised["event_id"] == row["raised_event_id"]
    assert raised["payload"] == {
        "alarm_id": str(row["alarm_id"]),
        "alarm_kind": kind,
        "node_id": str(node),
        "zone_id": None,
        "since": _stamp(first),
    }
    # Sostenida: ningún raise más.
    for _ in range(3):
        alarms.cycle()
        alarms.state(node, bad)
        assert alarms.evaluate().raised == []
    # Un ciclo bien no la baja; dos, sí, con since en el primero.
    alarms.cycle()
    alarms.state(node, good)
    first_good = alarms.now()
    assert alarms.evaluate().cleared == []
    alarms.cycle()
    alarms.state(node, good)
    assert [alarm.alarm_kind.value for alarm in alarms.evaluate().cleared] == [kind]
    (row,) = alarms.rows.alarms(node, kind)
    assert row["cleared_at"] == alarms.now()
    (cleared,) = alarms.rows.events("fleet_alarm_cleared", node)
    assert cleared["event_id"] == row["cleared_event_id"]
    assert cleared["payload"] == {
        "alarm_id": str(row["alarm_id"]),
        "alarm_kind": kind,
        "node_id": str(node),
        "zone_id": None,
        "since": _stamp(first_good),
        "cleared_at": _stamp(alarms.now()),
    }
    assert alarms.rows.slots(node) == set()


@pytest.mark.parametrize("case", sorted(CONFIRMED))
def test_a_one_cycle_oscillation_raises_nothing(alarms: Alarms, case: str) -> None:
    bad, good = CONFIRMED[case]
    node = alarms.node()
    for state in (bad, good, bad, good, bad, good, bad):
        alarms.state(node, state)
        assert alarms.evaluate().raised == []
        alarms.cycle()
    assert alarms.rows.alarms(node) == []
    assert alarms.rows.events("fleet_alarm_raised", node) == []


def test_an_evaluation_inside_the_same_cycle_does_not_count_twice(alarms: Alarms) -> None:
    node = alarms.node()
    alarms.state(node, NodeState(offset_ms=9_000))
    alarms.evaluate()
    alarms.cycle(29)  # menos de medio ciclo: es el mismo ciclo
    alarms.evaluate()
    assert alarms.rows.evaluations(node)["clock_drift"]["consecutive"] == 1
    assert alarms.rows.alarms(node) == []
    alarms.cycle(1)  # medio ciclo: la siguiente evaluación
    assert [a.alarm_kind.value for a in alarms.evaluate().raised] == ["clock_drift"]
    assert kinds(alarms.rows.alarms(node)) == ["clock_drift"]


# --- Clases sin histéresis ------------------------------------------------------------------------


def test_version_retiring_needs_retires_at(alarms: Alarms) -> None:
    node = alarms.node()
    alarms.state(node, QUIET)
    assert alarms.evaluate().raised == []
    alarms.cycle()
    alarms.state(node, NodeState(retires_at=RETIRES_AT))
    assert [a.alarm_kind.value for a in alarms.evaluate().raised] == ["version_retiring"]
    alarms.cycle()
    alarms.state(node, NodeState(retires_at=RETIRES_AT))
    assert alarms.evaluate().raised == []
    alarms.cycle()
    alarms.state(node, QUIET)
    assert [a.alarm_kind.value for a in alarms.evaluate().cleared] == ["version_retiring"]


@pytest.mark.parametrize("adapter", ["simulated", "file"])
def test_a_simulated_adapter_with_a_productive_zone_raises_and_is_audited(
    alarms: Alarms, adapter: str
) -> None:
    world = alarms.world
    plant = world.plants[0]
    zones = world.zones(plant)
    node = world.add_node(plant, zones[:2])
    for zone in zones[:2]:
        world.set_gate(zone, mounting="approved", usage="approved")
    alarms.state(node, NodeState(adapter=adapter))
    (alarm,) = alarms.evaluate().raised
    productive = min(zones[:2], key=str)
    assert alarm.alarm_kind.value == "simulated_adapter_in_productive"
    assert alarm.zone_id == productive
    (event,) = alarms.rows.events("fleet_alarm_raised", node)
    assert event["payload"]["zone_id"] == str(productive)
    (entry,) = alarms.rows.security_alerts(node)
    assert entry["operation"] == "fleet_security_alert"
    assert entry["outcome"] == "success" and entry["actor_kind"] == "system"
    assert (entry["scope_plant_id"], entry["scope_zone_id"]) == (plant, productive)
    assert entry["resource_kind"] == "node"
    assert entry["filters"] == {
        "alert_kind": "simulated_adapter_in_productive",
        "alarm_id": str(alarm.alarm_id),
    }
    # Sostenida: ni otra alarma ni otra alerta.
    alarms.cycle()
    alarms.state(node, NodeState(adapter=adapter))
    assert alarms.evaluate().raised == []
    assert len(alarms.rows.security_alerts(node)) == 1


@pytest.mark.parametrize(
    ("adapter", "usage"), [("simulated", "pending"), ("modbus_rtu", "approved")]
)
def test_no_simulated_alarm_without_both_conditions(
    alarms: Alarms, adapter: str, usage: str
) -> None:
    world = alarms.world
    plant = world.plants[0]
    zone = world.zones(plant)[0]
    node = world.add_node(plant, [zone])
    world.set_gate(zone, mounting="approved", usage=usage)
    alarms.state(node, NodeState(adapter=adapter))
    assert alarms.evaluate().raised == []
    assert alarms.rows.security_alerts(node) == []


# --- Huérfanos ------------------------------------------------------------------------------------


def test_fifty_one_orphans_raise_at_once_and_fifty_do_not(alarms: Alarms) -> None:
    fifty, fifty_one = alarms.node(), alarms.node()
    alarms.state(fifty, NodeState(orphan_clips=50, day_clips=2_000))
    alarms.state(fifty_one, NodeState(orphan_clips=51, day_clips=2_000))
    raised = alarms.evaluate().raised
    assert [(a.node_id, a.alarm_kind.value) for a in raised] == [
        (fifty_one, "orphan_clips_growing")
    ]


def test_five_percent_must_hold_24_hours_to_raise_and_24_below_to_clear(alarms: Alarms) -> None:
    node = alarms.node()
    above = NodeState(orphan_clips=2, day_clips=20)  # 10 %: por encima del 5 %
    alarms.state(node, above)
    first = alarms.now()
    assert alarms.evaluate().raised == []
    alarms.cycle(12 * 3600)
    alarms.state(node, above)
    assert alarms.evaluate().raised == []
    alarms.cycle(12 * 3600 - CYCLE)
    alarms.state(node, above)
    assert alarms.evaluate().raised == []  # un ciclo antes de las 24 h
    alarms.cycle()
    alarms.state(node, above)
    (alarm,) = alarms.evaluate().raised
    (event,) = alarms.rows.events("fleet_alarm_raised", node)
    assert event["payload"]["since"] == _stamp(first)
    below = NodeState(orphan_clips=1, day_clips=20)  # 5 %: no es más del 5 %
    alarms.cycle()
    alarms.state(node, below)
    first_below = alarms.now()
    assert alarms.evaluate().cleared == []
    alarms.cycle(24 * 3600 - CYCLE)
    alarms.state(node, below)
    assert alarms.evaluate().cleared == []
    alarms.cycle()
    alarms.state(node, below)
    assert [a.alarm_id for a in alarms.evaluate().cleared] == [alarm.alarm_id]
    (cleared,) = alarms.rows.events("fleet_alarm_cleared", node)
    assert cleared["payload"]["since"] == _stamp(first_below)


def test_verification_clips_and_orphans_outside_the_window_never_count(alarms: Alarms) -> None:
    node = alarms.node()
    alarms.state(node, NodeState(verification_orphans=80, orphan_clips_outside=80))
    assert alarms.evaluate().raised == []
    alarms.cycle(24 * 3600)
    alarms.state(node, NodeState(verification_orphans=80, orphan_clips_outside=80))
    assert alarms.evaluate().raised == []


# --- Nodos retirados ------------------------------------------------------------------------------


@pytest.mark.parametrize("retirement", ["revoked", "decommissioned"])
def test_a_retired_node_raises_nothing_and_its_open_alarms_close_once(
    alarms: Alarms, retirement: str
) -> None:
    node = alarms.node()
    bad = NodeState(offset_ms=9_000, retires_at=RETIRES_AT, certificate_in_ms=1_000)
    alarms.state(node, bad)
    alarms.evaluate()
    alarms.cycle()
    alarms.state(node, bad)
    alarms.evaluate()
    alarms.alert()
    assert alarms.rows.open_kinds(node) == {
        "clock_drift",
        "version_retiring",
        "certificate_expiring",
    }
    retired = NodeState(
        status="revoked",
        decommissioned=retirement == "decommissioned",
        offset_ms=9_000,
        retires_at=RETIRES_AT,
        certificate_in_ms=1_000,
        heartbeat_age_ms=3_600_000,
    )
    alarms.cycle()
    alarms.state(node, retired)
    report = alarms.evaluate()
    assert report.raised == []
    assert sorted(a.alarm_kind.value for a in report.cleared) == [
        "certificate_expiring",
        "clock_drift",
        "version_retiring",
    ]
    assert alarms.rows.open_kinds(node) == set() == alarms.rows.slots(node)
    assert len(alarms.rows.events("fleet_alarm_cleared", node)) == 3
    for _ in range(2):
        alarms.cycle()
        alarms.state(node, retired)
        assert alarms.evaluate() == AlarmReport()
        assert alarms.detect() == AlarmReport()
        assert alarms.alert() == AlarmReport()
    assert len(alarms.rows.events("fleet_alarm_cleared", node)) == 3
    assert alarms.rows.communication(node) == []


# --- Nodos mudos ----------------------------------------------------------------------------------


@pytest.mark.parametrize("interval", [15, 60, 600])
def test_mute_is_more_than_five_intervals_and_starts_at_the_last_heartbeat(
    alarms: Alarms, interval: int
) -> None:
    node = alarms.node()
    threshold_ms = MUTE_FACTOR * interval * 1_000
    alarms.state(node, NodeState(interval_seconds=interval, heartbeat_age_ms=threshold_ms))
    assert alarms.detect() == AlarmReport()  # exactamente cinco intervalos: todavía no
    assert alarms.rows.state(node) == "reachable"
    alarms.state(node, NodeState(interval_seconds=interval, heartbeat_age_ms=threshold_ms + 1))
    last = alarms.now() - timedelta(milliseconds=threshold_ms + 1)
    report = alarms.detect()
    assert report.transitions == [node]
    assert alarms.rows.state(node) == "mute"
    assert alarms.rows.communication(node) == [
        {
            "node_id": str(node),
            "state": "mute",
            "since": _stamp(last),
            "last_heartbeat_at": _stamp(last),
        }
    ]
    (alarm,) = report.raised
    assert alarm.alarm_kind.value == "node_mute" and alarm.raised_at == alarms.now()
    (event,) = alarms.rows.events("fleet_alarm_raised", node)
    assert event["payload"]["since"] == _stamp(last)
    # Otra pasada: ni otra transición ni otra alarma.
    alarms.cycle()
    assert alarms.detect() == AlarmReport()
    assert len(alarms.rows.communication(node)) == 1
    assert len(alarms.rows.alarms(node)) == 1


def test_a_node_that_never_beat_or_is_not_enrolled_is_never_mute(alarms: Alarms) -> None:
    never = alarms.node()
    alarms.state(never, NodeState(heartbeat_age_ms=None))
    declared = alarms.node()
    alarms.state(declared, NodeState(status="declared", heartbeat_age_ms=86_400_000))
    assert alarms.detect() == AlarmReport()
    assert alarms.rows.alarms(never) == alarms.rows.alarms(declared) == []
    assert alarms.rows.communication(never) == []


def test_node_mute_clears_when_the_node_is_back(alarms: Alarms) -> None:
    node = alarms.node()
    alarms.state(node, NodeState(heartbeat_age_ms=600_000))
    alarms.detect()
    # Sin latido, la evaluación no la baja.
    alarms.cycle()
    assert alarms.evaluate().cleared == []
    # Vuelve (la ruta del latido escribe reachable): la siguiente evaluación la baja.
    alarms.cycle()
    alarms.state(node, NodeState(heartbeat_age_ms=2_000))
    back = alarms.now() - timedelta(milliseconds=2_000)
    (cleared,) = alarms.evaluate().cleared
    assert cleared.alarm_kind.value == "node_mute"
    (event,) = alarms.rows.events("fleet_alarm_cleared", node)
    assert event["payload"]["since"] == _stamp(back)


def test_a_mute_node_without_its_alarm_gets_it_without_another_transition(alarms: Alarms) -> None:
    node = alarms.node()
    alarms.state(node, NodeState(heartbeat_age_ms=600_000))
    alarms.stack.execute(
        "UPDATE fleet.node_inventory SET communication_state = 'mute' WHERE node_id = $1", node
    )
    report = alarms.detect()
    assert report.transitions == []
    assert [a.alarm_kind.value for a in report.raised] == ["node_mute"]
    assert alarms.rows.communication(node) == []


# --- Certificados ---------------------------------------------------------------------------------


def test_certificate_expiring_at_15_days_and_cleared_after_rotation(alarms: Alarms) -> None:
    at, after = alarms.node(), alarms.node()
    fifteen_days = 15 * 86_400_000
    alarms.state(at, NodeState(certificate_in_ms=fifteen_days))
    alarms.state(after, NodeState(certificate_in_ms=fifteen_days + 1))
    (alarm,) = alarms.alert().raised
    assert alarm.node_id == at and alarm.alarm_kind.value == "certificate_expiring"
    (event,) = alarms.rows.events("fleet_alarm_raised", at)
    assert event["payload"]["since"] == _stamp(alarms.now())
    assert alarms.alert() == AlarmReport()
    # La evaluación no la baja mientras siga por vencer.
    alarms.cycle()
    alarms.state(at, NodeState(certificate_in_ms=fifteen_days - 60_000))
    assert alarms.evaluate().cleared == []
    # Rotada: la credencial vigente vence en un año.
    alarms.cycle()
    alarms.state(at, NodeState(certificate_in_ms=365 * 86_400_000))
    (cleared,) = alarms.evaluate().cleared
    assert cleared.alarm_id == alarm.alarm_id


# --- Eventos en la misma transacción --------------------------------------------------------------


class Rollback(Exception):
    pass


def test_alarm_rows_events_and_evaluations_live_and_die_with_the_transaction(
    alarms: Alarms,
) -> None:
    node = alarms.node()
    alarms.state(node, NodeState(retires_at=RETIRES_AT, offset_ms=9_000))

    async def failing() -> None:
        async with alarms.stack.database.transaction(system(alarms.world.organization)) as tx:
            report = await alarms.tasks.evaluator.evaluate(tx)
            assert report.raised
            raise Rollback()

    with pytest.raises(Rollback):
        alarms.stack.run(failing())
    assert alarms.rows.alarms(node) == []
    assert alarms.rows.events("fleet_alarm_raised", node) == []
    assert alarms.rows.evaluations(node) == {}
    assert len(alarms.evaluate().raised) == 1
    (row,) = alarms.rows.alarms(node)
    (event,) = alarms.rows.events("fleet_alarm_raised", node)
    assert row["raised_event_id"] == event["event_id"] and event["plant_id"] == row["plant_id"]
    for value in event["payload"].values():
        assert _is_identifier_kind_or_stamp(value), value


def _is_identifier_kind_or_stamp(value: Any) -> bool:
    if value is None:
        return True  # zone_id ausente
    if not isinstance(value, str):
        return False
    if STAMP.fullmatch(value):
        return True
    try:
        uuid.UUID(value)
    except ValueError:
        return re.fullmatch(r"[a-z_]+", value) is not None
    return True


# --- Alcance --------------------------------------------------------------------------------------


def _fingerprint(alarms: Alarms, organization: uuid.UUID) -> dict[str, str]:
    prints: dict[str, str] = {}
    for table in (
        "fleet.fleet_alarm",
        "fleet.fleet_alarm_evaluation",
        "fleet.node_inventory",
        "ledger.ledger_record",
        "shared.audit_entry",
    ):
        (row,) = alarms.stack.fetch(
            f"SELECT md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) AS h"  # noqa: S608
            f" FROM {table} AS t WHERE t.organization_id = $1",
            organization,
        )
        prints[table] = str(row["h"])
    return prints


def _everything_bad(alarms: Alarms, world: InventoryWorld) -> uuid.UUID:
    plant = world.plants[0]
    zone = world.zones(plant)[0]
    node = world.add_node(plant, [zone])
    world.set_gate(zone, mounting="approved", usage="approved")
    world.write_state(
        node,
        NodeState(
            pending=500,
            offset_ms=9_000,
            retires_at=RETIRES_AT,
            adapter="simulated",
            certificate_in_ms=1_000,
            orphan_clips=60,
            heartbeat_age_ms=3_600_000,
        ),
        alarms.now(),
    )
    return node


def test_each_task_only_touches_the_organization_of_its_context(alarms: Alarms) -> None:
    other = alarms.other()
    mine = _everything_bad(alarms, alarms.world)
    theirs = _everything_bad(alarms, other)
    before = _fingerprint(alarms, other.organization)
    for _ in range(2):
        alarms.evaluate()
        alarms.detect()
        alarms.alert()
        alarms.cycle()
    assert alarms.rows.open_kinds(mine) == {
        "node_mute",
        "queue_over_threshold",
        "clock_drift",
        "version_retiring",
        "simulated_adapter_in_productive",
        "certificate_expiring",
        "orphan_clips_growing",
    }
    assert alarms.rows.alarms(theirs) == []
    assert _fingerprint(alarms, other.organization) == before


def test_the_task_statements_filter_by_organization_even_without_rls(alarms: Alarms) -> None:
    """Como superusuario (sin RLS): con los parámetros de A, ninguna sentencia devuelve filas de B
    aunque B las tenga que cumplirían todo lo demás (la prueba falla sin el filtro)."""
    other = alarms.other()
    mine = _everything_bad(alarms, alarms.world)
    theirs = _everything_bad(alarms, other)
    alarms.evaluate(other)
    alarms.detect(other)
    alarms.alert(other)
    assert alarms.rows.open_kinds(theirs)
    now = alarms.now()
    organization = alarms.world.organization
    everyone = [str(mine), str(theirs)]
    statements: dict[str, tuple[Any, dict[str, Any]]] = {
        "facts": (
            fleet_alarm_store._FACTS,
            {"now": now, "clips_since": now - DAY, "after": None, "limit": 1000},
        ),
        "evaluations": (fleet_alarm_store._LOCK_EVALUATIONS, {"node_ids": everyone}),
        "open": (fleet_alarm_store._OPEN_ALARMS, {"node_ids": everyone}),
        "mute": (
            fleet_alarm_store._MUTE_CANDIDATES,
            {
                "eligible_status": "enrolled",
                "now": now,
                "mute_factor": MUTE_FACTOR,
                "default_interval": 60,
                "after_plant": None,
                "after_node": None,
                "limit": 1000,
            },
        ),
        "expiring": (
            fleet_alarm_store._EXPIRING,
            {"until": now + 30 * DAY, "after": None, "limit": 1000},
        ),
        "page": (
            fleet_alarm_store._ALARM_PAGE,
            {
                "whole_organization": True,
                "scope_plants": [],
                "scope_zones": [],
                "plant_id": None,
                "node_id": None,
                "alarm_kind": None,
                "status": None,
                "after_raised_at": None,
                "after_alarm_id": None,
                "limit": 1000,
            },
        ),
    }
    for name, (statement, parameters) in statements.items():
        rows = as_superuser(
            alarms.stack, statement, {"organization_id": organization, **parameters}
        )
        assert str(theirs) not in {str(row["node_id"]) for row in rows}, name


# --- Sentencias acotadas --------------------------------------------------------------------------


def _count(alarms: Alarms, run: Any) -> int:
    with counting(alarms.stack) as log:
        run()
    return len(log.statements)


def test_the_statements_per_organization_do_not_grow_with_the_nodes(alarms: Alarms) -> None:
    small, large = alarms.world, alarms.other()
    for world, nodes in ((small, 2), (large, 8)):
        for _ in range(nodes):
            node = world.add_node(world.plants[0])
            world.write_state(node, QUIET, alarms.now())
    for task in (alarms.evaluate, alarms.detect, alarms.alert):
        task(small)
        task(large)  # la primera evaluación inserta las filas de histéresis
        alarms.cycle()
        assert _count(alarms, lambda task=task: task(small)) == _count(
            alarms, lambda task=task: task(large)
        )


# --- GET /fleet/alarms ----------------------------------------------------------------------------


def _raise_some(alarms: Alarms) -> tuple[uuid.UUID, uuid.UUID]:
    """Un nodo con dos alarmas en la planta 0 y uno con una en la planta 1 (una ya cerrada)."""
    first, second = alarms.node(0), alarms.node(1)
    alarms.state(first, NodeState(retires_at=RETIRES_AT, certificate_in_ms=1_000))
    alarms.state(second, NodeState(retires_at=RETIRES_AT))
    alarms.evaluate()
    alarms.alert()
    alarms.cycle()
    alarms.state(second, QUIET)
    alarms.evaluate()
    return first, second


def _listed(alarms: Alarms, who: Any, params: dict[str, str] | None = None) -> list[Any]:
    found: list[Any] = []
    query = dict(params or {})
    while True:
        response = alarms.world.get(who, "/fleet/alarms", query)
        assert response.status_code == 200, response.text
        assert response.headers["Cache-Control"] == "no-store"
        body = response.json()
        found += body["alarms"]
        if body["next_after"] is None:
            return found
        query["after"] = body["next_after"]


def test_the_console_lists_active_and_recent_alarms_with_filters_and_cursor(
    alarms: Alarms,
) -> None:
    first, second = _raise_some(alarms)
    admin = alarms.world.admin
    listed = _listed(alarms, admin)
    assert len(listed) == 3
    assert [item["raised_at"] for item in listed] == sorted(
        (item["raised_at"] for item in listed), reverse=True
    )
    assert set(listed[0]) == {
        "alarm_id",
        "alarm_kind",
        "plant_id",
        "node_id",
        "zone_id",
        "raised_at",
        "cleared_at",
    }
    assert _listed(alarms, admin, {"limit": "1"}) == listed
    active = _listed(alarms, admin, {"status": "active"})
    assert sorted(item["alarm_kind"] for item in active) == [
        "certificate_expiring",
        "version_retiring",
    ]
    assert all(item["cleared_at"] is None for item in active)
    (cleared,) = _listed(alarms, admin, {"status": "cleared"})
    assert cleared["node_id"] == str(second) and cleared["cleared_at"] is not None
    assert {item["node_id"] for item in _listed(alarms, admin, {"node_id": str(first)})} == {
        str(first)
    }
    assert {
        item["alarm_kind"] for item in _listed(alarms, admin, {"alarm_kind": "version_retiring"})
    } == {"version_retiring"}
    plant = str(alarms.world.plants[1])
    assert {item["plant_id"] for item in _listed(alarms, admin, {"plant_id": plant})} == {plant}
    for bad in ({"after": "no*vale"}, {"status": "open"}, {"desconocido": "1"}, {"limit": "101"}):
        response = alarms.world.get(admin, "/fleet/alarms", bad)
        assert response.status_code == 400, (bad, response.text)


def test_another_organization_or_plant_answers_not_found(alarms: Alarms) -> None:
    first, second = _raise_some(alarms)
    other = alarms.other()
    their_node = _everything_bad(alarms, other)
    outsider = other.admin
    for params in (
        {"plant_id": str(alarms.world.plants[0])},
        {"node_id": str(first)},
        {"plant_id": str(uuid.uuid4())},
        {"node_id": str(uuid.uuid4())},
    ):
        response = alarms.world.get(outsider, "/fleet/alarms", params)
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "not_found"
    assert str(first) not in alarms.world.get(outsider, "/fleet/alarms").text
    # Un administrador de la planta 0 no ve la planta 1, ni pidiéndola.
    plant0, plant1 = alarms.world.plants
    member = alarms.world.member(level=ScopeLevel.PLANT, scope=plant0)
    assert {item["node_id"] for item in _listed(alarms, member)} == {str(first)}
    for params in ({"plant_id": str(plant1)}, {"node_id": str(second)}):
        response = alarms.world.get(member, "/fleet/alarms", params)
        assert response.status_code == 404, response.text
    response = alarms.world.get(
        member, "/fleet/alarms", {"plant_id": str(plant0), "node_id": str(second)}
    )
    assert response.status_code == 404
    assert their_node


def test_the_provider_reads_under_concession_and_it_is_audited(alarms: Alarms) -> None:
    _raise_some(alarms)
    organization = alarms.world.organization
    before = len(alarms.stack.audit(organization, "fleet_read"))
    listed = _listed(alarms, alarms.world.installer)
    assert len(listed) == 3
    entries = alarms.stack.audit(organization, "fleet_read")
    assert len(entries) == before + 1
    assert entries[-1]["actor_concession_id"] == alarms.world.installer[1]
