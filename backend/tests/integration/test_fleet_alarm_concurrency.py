"""Garantías concurrentes de las alarmas de flota contra PostgreSQL 16 real (TASK-225; NFR-GOB-47).

Cada escenario lanza **a la vez** dos operaciones desde conexiones distintas: la primera hace su
trabajo dentro de su transacción y la retiene; la segunda arranca entonces y la prueba espera a que
esté **esperando un candado** de la base (``pg_stat_activity``) o haya terminado; después se
confirma la primera. Ningún tope de pared decide el resultado. Cada prueba corre tres veces.

- **Un ciclo solapado de ``evaluate_fleet_alarms`` no cuenta otra evaluación**: con la condición
  de ``clock_drift`` en un solo ciclo, dos ejecuciones solapadas dejan ``consecutive = 1`` y
  ninguna alarma (la fila de histéresis bloqueada, ``FOR UPDATE``, y vuelta a comprobar).
- **Una sola alarma por condición**: dos primeras evaluaciones solapadas con ``version_retiring``,
  y dos ``alert_expiring_certificates`` solapadas, dejan una alarma abierta y un evento: la segunda
  choca con la ranura de ``open_fleet_alarm`` y se deshace entera (``AlarmAlreadyOpen``).
- **Una sola transición por nodo mudo**: dos ``detect_mute_nodes`` solapadas escriben un
  ``node_communication_state_changed`` y una ``node_mute`` (la fila del inventario bloqueada).
- **Un latido que llega mientras ``detect_mute_nodes`` evalúa el nodo no queda pisado**: el latido
  bloquea la fila del inventario y se retiene; la tarea espera esa fila y, al volver a comprobar
  la condición sobre la versión confirmada, deja fuera al nodo (sigue ``reachable``, sin ``mute``
  ni alarma). Es además la prueba cruzada del orden de candados (inventario → ficha → cadena): con
  el orden invertido, la tarea y el latido se interbloquean.
- **En el otro orden** (la tarea primero), el latido espera y escribe después su ``reachable``.
- **Entre lotes**: ``detect_mute_nodes`` con un lote por nodo y la revocación de un nodo del lote
  siguiente en la misma planta terminan las dos (la tarea toma los candados de todos los lotes
  antes de escribir en la cadena).
- **Un cierre por alarma**: dos evaluaciones solapadas sobre un nodo recién revocado escriben un
  solo ``fleet_alarm_cleared`` (la fila abierta bloqueada y vuelta a comprobar).

Sondas negativas del PR: quitar el ``FOR UPDATE`` de las filas de histéresis, quitar el ``FOR
UPDATE`` del inventario, invertir el orden inventario/ficha, quitar el disparador
``open_alarm_slot``, escribir la cadena lote a lote y quitar el ``FOR UPDATE`` y el ``cleared_at IS
NULL`` del bloqueo de las alarmas abiertas; cada una deja en rojo al menos una de estas pruebas.

Solo datos generados (NFR-CTR-43). Topes de la base de 60 s (retro 15).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterator
from datetime import datetime, timedelta
from typing import Any, Final

import httpx
import pytest

from tests.fleet_alarm_support import (
    ARRIVAL_SECONDS,
    AlarmRows,
    AlarmTasks,
    alarm_tasks,
    blocked_or_done,
    run_in,
    system,
)
from tests.fleet_http_support import REASON, FleetStack, fleet_stack
from tests.fleet_inventory_support import InventoryWorld, NodeState
from tests.heartbeat_support import HeartbeatStack, heartbeat_stack
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.fleet.adapters.postgres.fleet_alarm_store import (
    AlarmAlreadyOpen,
    PostgresFleetAlarmStore,
)
from vigia_platform.fleet.adapters.postgres.heartbeat_history_store import (
    PostgresHeartbeatHistoryStore,
)
from vigia_platform.fleet.application.fleet_alarms import (
    AlarmDependencies,
    AlarmReport,
    FleetAlarmEvaluator,
)
from vigia_platform.fleet.application.mute_nodes import MuteDetector
from vigia_platform.fleet.application.node_revocation import (
    NodeRevocationService,
    RevocationOutcome,
)
from vigia_platform.shared.db import Database, Transaction
from vigia_platform.shared.signing.keys import to_millisecond

pytestmark = pytest.mark.integration

ATTEMPTS: Final = range(3)
RETIRES_AT: Final = "2027-01-31T00:00:00.000Z"

Handler = Callable[[Transaction], Awaitable[AlarmReport]]


@pytest.fixture(scope="module")
def fleet(postgres_endpoint: PostgresEndpoint) -> Iterator[FleetStack]:
    with fleet_stack(postgres_endpoint, "fleet_alarm_concurrency") as built:
        yield built


@pytest.fixture(scope="module")
def beats(postgres_endpoint: PostgresEndpoint) -> Iterator[HeartbeatStack]:
    with heartbeat_stack(postgres_endpoint, "fleet_alarm_beats") as built:
        yield built


def _tasks(stack: FleetStack | HeartbeatStack) -> AlarmTasks:
    sessions = stack.authz.sessions
    (row,) = stack.fetch("SELECT now() AS now")
    sessions.clock.set(to_millisecond(row["now"]))
    outbox = stack.outbox
    assert outbox is not None
    return alarm_tasks(
        clock=sessions.clock, outbox=outbox, writer=stack.writer, audit=sessions.audit
    )


async def _overlapping(
    admin: Any,
    database: Database,
    organization: uuid.UUID,
    first: Handler,
    second: Handler,
) -> tuple[AlarmReport, AlarmReport | BaseException]:
    """``first`` en su transacción, retenida hasta que ``second`` espera un candado (o terminó)."""
    async with database.transaction(system(organization)) as transaction:
        report = await first(transaction)
        late = asyncio.create_task(run_in(database, organization, second))
        await blocked_or_done(admin, late)
    try:
        return report, await late
    except Exception as error:
        return report, error


# --- evaluate_fleet_alarms ---------------------------------------------------------------------


@pytest.mark.parametrize("attempt", ATTEMPTS)
def test_an_overlapping_evaluation_does_not_count_the_cycle_twice(
    fleet: FleetStack, attempt: int
) -> None:
    tasks = _tasks(fleet)
    world = InventoryWorld.build(fleet)
    rows = AlarmRows(fleet.fetch)
    node = world.add_node(world.plants[0])
    world.write_state(node, NodeState(offset_ms=9_000), world.now())
    evaluate = tasks.evaluator.evaluate
    admin = fleet.authz.sessions.admin
    # Primera evaluación (inserta la fila), sola.
    fleet.run(run_in(fleet.database, world.organization, evaluate))
    fleet.tick(60)
    # Un solo ciclo más con la condición, con dos ejecuciones solapadas.
    _, second = fleet.run(
        _overlapping(admin, fleet.database, world.organization, evaluate, evaluate)
    )
    assert second == AlarmReport(), second  # el ciclo solapado no tiene efecto
    assert rows.evaluations(node)["clock_drift"]["consecutive"] == 2
    (alarm,) = rows.alarms(node)  # el segundo ciclo levanta una sola
    fleet.tick(60)
    world.write_state(node, NodeState(offset_ms=0), world.now())
    # Un ciclo sin la condición, solapado: cuenta uno, no baja.
    _, second = fleet.run(
        _overlapping(admin, fleet.database, world.organization, evaluate, evaluate)
    )
    assert second == AlarmReport(), second  # el ciclo solapado no tiene efecto
    assert rows.evaluations(node)["clock_drift"]["consecutive"] == 1
    assert rows.open_kinds(node) == {"clock_drift"}
    fleet.tick(60)
    world.write_state(node, NodeState(offset_ms=9_000), world.now())
    _, second = fleet.run(
        _overlapping(admin, fleet.database, world.organization, evaluate, evaluate)
    )
    assert second == AlarmReport(), second  # el ciclo solapado no tiene efecto
    assert rows.evaluations(node)["clock_drift"]["consecutive"] == 1
    assert len(rows.alarms(node)) == 1 and rows.open_kinds(node) == {"clock_drift"}
    assert alarm["cleared_at"] is None
    assert len(rows.events("fleet_alarm_raised", node)) == 1
    assert rows.events("fleet_alarm_cleared", node) == []


@pytest.mark.parametrize("attempt", ATTEMPTS)
def test_a_one_cycle_condition_under_overlap_raises_nothing(
    fleet: FleetStack, attempt: int
) -> None:
    tasks = _tasks(fleet)
    world = InventoryWorld.build(fleet)
    rows = AlarmRows(fleet.fetch)
    node = world.add_node(world.plants[0])
    world.write_state(node, NodeState(offset_ms=0), world.now())
    evaluate = tasks.evaluator.evaluate
    admin = fleet.authz.sessions.admin
    fleet.run(run_in(fleet.database, world.organization, evaluate))
    fleet.tick(60)
    world.write_state(node, NodeState(offset_ms=9_000), world.now())
    _, second = fleet.run(
        _overlapping(admin, fleet.database, world.organization, evaluate, evaluate)
    )
    assert second == AlarmReport(), second  # el ciclo solapado no tiene efecto
    fleet.tick(60)
    world.write_state(node, NodeState(offset_ms=0), world.now())
    fleet.run(run_in(fleet.database, world.organization, evaluate))
    assert rows.alarms(node) == []
    assert rows.events("fleet_alarm_raised", node) == []


@pytest.mark.parametrize("attempt", ATTEMPTS)
def test_two_overlapping_first_evaluations_raise_one_alarm(fleet: FleetStack, attempt: int) -> None:
    tasks = _tasks(fleet)
    world = InventoryWorld.build(fleet)
    rows = AlarmRows(fleet.fetch)
    node = world.add_node(world.plants[0])
    world.write_state(node, NodeState(retires_at=RETIRES_AT), world.now())
    evaluate = tasks.evaluator.evaluate
    first, second = fleet.run(
        _overlapping(
            fleet.authz.sessions.admin, fleet.database, world.organization, evaluate, evaluate
        )
    )
    assert [a.alarm_kind.value for a in first.raised] == ["version_retiring"]
    # La segunda: o no vio nada que abrir, o chocó con la ranura y se deshizo entera.
    assert isinstance(second, AlarmAlreadyOpen) or second.raised == []
    (alarm,) = rows.alarms(node)
    assert alarm["cleared_at"] is None
    assert len(rows.events("fleet_alarm_raised", node)) == 1
    assert rows.slots(node) == {"version_retiring"}


# --- alert_expiring_certificates ---------------------------------------------------------------


@pytest.mark.parametrize("attempt", ATTEMPTS)
def test_two_overlapping_certificate_alerts_raise_one_alarm(
    fleet: FleetStack, attempt: int
) -> None:
    tasks = _tasks(fleet)
    world = InventoryWorld.build(fleet)
    rows = AlarmRows(fleet.fetch)
    node = world.add_node(world.plants[0])
    world.write_state(node, NodeState(certificate_in_ms=86_400_000), world.now())
    alert = tasks.alerter.alert
    first, second = fleet.run(
        _overlapping(fleet.authz.sessions.admin, fleet.database, world.organization, alert, alert)
    )
    assert [a.alarm_kind.value for a in first.raised] == ["certificate_expiring"]
    assert isinstance(second, AlarmAlreadyOpen) or second.raised == []
    assert len(rows.alarms(node)) == 1
    assert len(rows.events("fleet_alarm_raised", node)) == 1


# --- detect_mute_nodes -------------------------------------------------------------------------


@pytest.mark.parametrize("attempt", ATTEMPTS)
def test_two_overlapping_detections_write_one_transition_and_one_alarm(
    fleet: FleetStack, attempt: int
) -> None:
    tasks = _tasks(fleet)
    world = InventoryWorld.build(fleet)
    rows = AlarmRows(fleet.fetch)
    node = world.add_node(world.plants[0])
    world.write_state(node, NodeState(heartbeat_age_ms=3_600_000), world.now())
    detect = tasks.detector.detect
    first, second = fleet.run(
        _overlapping(fleet.authz.sessions.admin, fleet.database, world.organization, detect, detect)
    )
    assert first.transitions == [node]
    assert not isinstance(second, BaseException), second
    assert second == AlarmReport()  # ni otra transición ni otra alarma
    assert [record["state"] for record in rows.communication(node)] == ["mute"]
    assert len(rows.alarms(node, "node_mute")) == 1
    assert rows.state(node) == "mute"


class GatedFacts(PostgresFleetAlarmStore):
    """El almacén real de alarmas; tras leer los hechos (sin candado), ``evaluate_fleet_alarms`` se
    retiene hasta ``release``."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def evaluation_facts(self, transaction: Transaction, **arguments: Any) -> Any:
        facts = await super().evaluation_facts(transaction, **arguments)
        self.entered.set()
        await asyncio.wait_for(self.release.wait(), ARRIVAL_SECONDS)
        return facts


@pytest.mark.parametrize("attempt", ATTEMPTS)
def test_an_evaluation_with_a_stale_reachable_snapshot_keeps_a_fresh_node_mute(
    fleet: FleetStack, attempt: int
) -> None:
    """Contraejemplo de PR-GOB-32 (TASK-227), reducido: ``evaluate_fleet_alarms`` lee los hechos
    (el nodo aún ``reachable``), ``detect_mute_nodes`` marca el nodo y levanta ``node_mute`` y la
    evaluación sigue y lee la alarma ya abierta. Su ``reachable`` es de antes del silencio (el
    último latido es anterior a la alarma): no la baja, y la siguiente detección no la duplica."""
    tasks = _tasks(fleet)
    world = InventoryWorld.build(fleet)
    rows = AlarmRows(fleet.fetch)
    node = world.add_node(world.plants[0])
    world.write_state(node, NodeState(heartbeat_age_ms=3_600_000), world.now())
    gated = GatedFacts()
    evaluator = FleetAlarmEvaluator(
        AlarmDependencies(clock=fleet.authz.sessions.clock, outbox=tasks.deps.outbox, store=gated),
        audit=fleet.authz.sessions.audit,
    )

    async def scenario() -> tuple[AlarmReport, AlarmReport]:
        evaluate = asyncio.create_task(
            run_in(fleet.database, world.organization, evaluator.evaluate)
        )
        await asyncio.wait_for(gated.entered.wait(), ARRIVAL_SECONDS)
        detected = await run_in(fleet.database, world.organization, tasks.detector.detect)
        gated.release.set()
        return detected, await evaluate

    detected, evaluated = fleet.run(scenario())
    assert [alarm.alarm_kind.value for alarm in detected.raised] == ["node_mute"]
    assert evaluated.cleared == []
    fleet.tick(60)
    fleet.run(run_in(fleet.database, world.organization, tasks.detector.detect))
    assert len(rows.alarms(node, "node_mute")) == 1
    assert rows.open_kinds(node) == {"node_mute"}


class GatedHistory(PostgresHeartbeatHistoryStore):
    """La historia real; ``seen`` (con la fila del inventario ya bloqueada y antes de la ficha) se
    retiene hasta ``release``."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def seen(self, transaction: Transaction, **arguments: Any) -> datetime | None:
        found = await super().seen(transaction, **arguments)
        self.entered.set()
        await asyncio.wait_for(self.release.wait(), ARRIVAL_SECONDS)
        return found


@pytest.mark.parametrize("attempt", ATTEMPTS)
def test_a_heartbeat_arriving_while_detect_evaluates_is_not_overwritten_by_mute(
    beats: HeartbeatStack, attempt: int
) -> None:
    tasks = _tasks(beats)
    rows = AlarmRows(beats.fetch)
    site = beats.site(interval=15)
    beats.tick()
    assert beats.post(site).status_code == 200
    beats.tick(600)  # cinco intervalos y de sobra: el nodo está en silencio
    history = GatedHistory()
    instance = beats.instance(history=history)
    database = beats.primary.database

    async def scenario() -> tuple[httpx.Response, AlarmReport]:
        heartbeat = asyncio.create_task(beats.send(site, beats.body(site), instance))
        await asyncio.wait_for(history.entered.wait(), ARRIVAL_SECONDS)
        detect = asyncio.create_task(run_in(database, site.organization_id, tasks.detector.detect))
        await blocked_or_done(beats.authz.sessions.admin, detect)
        history.release.set()
        return await heartbeat, await detect

    response, report = beats.run(scenario())
    assert response.status_code == 200, response.text
    assert report == AlarmReport()
    assert rows.state(site.node_id) == "reachable"
    assert [record["state"] for record in rows.communication(site.node_id)] == ["reachable"]
    assert rows.alarms(site.node_id) == []


class GatedWriter:
    """El escritor del expediente; retiene la transacción de la tarea tras su primer registro."""

    def __init__(self, writer: Any) -> None:
        self._writer = writer
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def write(self, *arguments: Any, **keywords: Any) -> Any:
        written = await self._writer.write(*arguments, **keywords)
        self.entered.set()
        await asyncio.wait_for(self.release.wait(), ARRIVAL_SECONDS)
        return written


@pytest.mark.parametrize("attempt", ATTEMPTS)
def test_a_heartbeat_after_the_mute_mark_waits_and_writes_reachable(
    beats: HeartbeatStack, attempt: int
) -> None:
    tasks = _tasks(beats)
    rows = AlarmRows(beats.fetch)
    site = beats.site(interval=15)
    beats.tick()
    assert beats.post(site).status_code == 200
    last = beats.inventory(site.node_id)
    assert last is not None
    beats.tick(600)
    gated = GatedWriter(beats.writer)
    detector = MuteDetector(tasks.deps, writer=gated)  # type: ignore[arg-type]
    database = beats.primary.database

    async def scenario() -> tuple[AlarmReport, httpx.Response]:
        detect = asyncio.create_task(run_in(database, site.organization_id, detector.detect))
        await asyncio.wait_for(gated.entered.wait(), ARRIVAL_SECONDS)
        heartbeat = asyncio.create_task(beats.send(site, beats.body(site)))
        await blocked_or_done(beats.authz.sessions.admin, heartbeat)
        gated.release.set()
        return await detect, await heartbeat

    report, response = beats.run(scenario())
    assert response.status_code == 200, response.text
    assert report.transitions == [site.node_id]
    records = rows.communication(site.node_id)
    assert [record["state"] for record in records] == ["reachable", "mute", "reachable"]
    assert records[1]["since"] == records[1]["last_heartbeat_at"]
    assert rows.state(site.node_id) == "reachable"
    # La alarma queda abierta hasta la evaluación siguiente, que la baja.
    assert rows.open_kinds(site.node_id) == {"node_mute"}
    beats.tick(60)
    (cleared,) = beats.run(run_in(database, site.organization_id, tasks.evaluator.evaluate)).cleared
    assert cleared.alarm_kind.value == "node_mute"
    assert last["last_heartbeat_at"] + timedelta(seconds=75) < beats.now()


@pytest.mark.parametrize("attempt", ATTEMPTS)
def test_detect_by_batches_and_a_revocation_in_the_next_batch_both_finish(
    fleet: FleetStack, attempt: int
) -> None:
    # Dos nodos mudos de la misma planta, un lote por nodo: la tarea escribe el registro del
    # primero en la cadena de la planta y se retiene; entonces se revoca el segundo (ficha →
    # cadena). Con los candados de todos los lotes tomados antes de la cadena, la revocación espera
    # la ficha que la tarea ya compartió y las dos terminan; con la cadena escrita lote a lote, la
    # tarea pediría la ficha del segundo teniendo la cadena (bloqueo mutuo).
    tasks = _tasks(fleet)
    world = InventoryWorld.build(fleet)
    rows = AlarmRows(fleet.fetch)
    plant = world.plants[0]
    first, second = sorted((world.add_node(plant), world.add_node(plant)))
    silent = NodeState(heartbeat_age_ms=3_600_000)
    world.write_states({first: silent, second: silent}, world.now())
    gated = GatedWriter(fleet.writer)
    detector = MuteDetector(tasks.deps, writer=gated, batch_size=1)  # type: ignore[arg-type]
    revocations = NodeRevocationService(fleet.deps)
    context = fleet.context(world.installer)

    async def scenario() -> list[Any]:
        detect = asyncio.create_task(run_in(fleet.database, world.organization, detector.detect))
        await asyncio.wait_for(gated.entered.wait(), ARRIVAL_SECONDS)
        revoke = asyncio.create_task(revocations.revoke(context, second, REASON))
        await blocked_or_done(fleet.authz.sessions.admin, revoke)
        gated.release.set()
        return list(await asyncio.gather(detect, revoke, return_exceptions=True))

    report, outcome = fleet.run(scenario())
    assert isinstance(report, AlarmReport), report
    assert isinstance(outcome, RevocationOutcome), outcome
    assert report.transitions == [first, second]
    assert outcome.dirty_generation is not None
    assert fleet.node_row(second)["revoked_at"] is not None
    for node in (first, second):
        assert [record["state"] for record in rows.communication(node)] == ["mute"]


@pytest.mark.parametrize("attempt", ATTEMPTS)
def test_two_overlapping_evaluations_close_a_retired_node_alarm_once(
    fleet: FleetStack, attempt: int
) -> None:
    # Un nodo revocado no tiene filas de histéresis que bloquear: las dos evaluaciones solapadas
    # solo se serializan en la fila de la alarma abierta (``FOR UPDATE`` con ``cleared_at IS
    # NULL``, vuelta a comprobar tras la espera). La segunda no vuelve a cerrarla.
    tasks = _tasks(fleet)
    world = InventoryWorld.build(fleet)
    rows = AlarmRows(fleet.fetch)
    node = world.add_node(world.plants[0])
    world.write_state(node, NodeState(retires_at=RETIRES_AT), world.now())
    evaluate = tasks.evaluator.evaluate
    (alarm,) = fleet.run(run_in(fleet.database, world.organization, evaluate)).raised
    fleet.tick(60)
    world.write_state(node, NodeState(status="revoked", retires_at=RETIRES_AT), world.now())
    first, second = fleet.run(
        _overlapping(
            fleet.authz.sessions.admin, fleet.database, world.organization, evaluate, evaluate
        )
    )
    assert [cleared.alarm_id for cleared in first.cleared] == [alarm.alarm_id]
    assert second == AlarmReport(), second
    (event,) = rows.events("fleet_alarm_cleared", node)
    (row,) = rows.alarms(node)
    assert row["cleared_at"] is not None
    assert str(row["cleared_event_id"]) == str(event["event_id"])
    assert rows.slots(node) == set()
