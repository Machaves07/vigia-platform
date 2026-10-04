"""Propiedades de las compuertas (C-PLA-09; TASK-211): PR-GOB-03 y PR-GOB-21.

- **PR-GOB-03** (invariante y monotonía, dominio puro): para toda secuencia generada
  (``gate_sequences``) de aprobaciones y revocaciones, ``resulting_mode`` es función total de
  (montaje, uso) y coincide con la tabla de BL §3.1; nunca vale ``productive`` sin las dos
  ``approved``; ninguna revocación amplía el modo; revocar lo que no está ``approved`` se rechaza
  sin cambiar nada; y la carga ``GateState`` de cada estado es válida para el modelo del contrato
  de U-01 (que exige la derivación y los 7 días).
- **PR-GOB-21** (verificada por la base, servicio real): la misma clase de secuencias, con pasos de
  dos órdenes **concurrentes** (dos conexiones a la vez) e instantes repetidos (avance 0), sobre
  ``GateService.transition_gate`` y PostgreSQL 16 como ``vigia_app``. Después de cada ejemplo:
  la base rechaza con ``exclusion_violation`` todo intervalo que se solape con uno de la misma
  zona y compuerta; para cada instante de muestra, ``state_at`` (repositorio y servicio) devuelve
  a lo sumo un intervalo, el del oráculo; ``gate_history(from, to)`` devuelve exactamente los que
  se solapan según el oráculo de ``time_windows`` sobre **todas** las filas de la zona (leídas como
  superusuario); la historia de cada compuerta es contigua y la proyección coincide con su último
  intervalo. Las concurrentes solo terminan en transición o en ``GateConflict``: nunca en carrera
  perdida (lo garantiza el candado de la proyección).

Perfil ``ci`` con semilla registrada (``tests/conftest.py``). Solo datos generados.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given
from sqlalchemy import exc as sa_exc
from sqlalchemy import text
from vigia_contracts.models.enumerations import GateStatus, ZoneMode
from vigia_contracts.models.gate_state import GateState

from tests.gates_support import REASON, GatesWorld, gates_world, sqlstate
from tests.integration.conftest import PostgresEndpoint
from tests.properties.gob.strategies.gates import Approve, Command, GateScenario, gate_sequences
from vigia_platform.catalog.application.gates import GateConflict, GateService, GateTransition
from vigia_platform.catalog.domain.enums import GateKind
from vigia_platform.catalog.domain.gates import (
    GateRuleViolated,
    GateViolation,
    ZoneGateState,
    gate_state_payload,
    mode_rank,
    plan_transition,
    resulting_mode,
)
from vigia_platform.catalog.domain.time_windows import (
    MILLISECOND,
    HalfOpenInterval,
    containing,
    overlapping,
)
from vigia_platform.identity.authz.context import with_unit
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import ActorUnit, Role, ScopeContext

T0 = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
ACTOR = uuid.UUID(int=7, version=4)
EXCLUSION_VIOLATION = "23P01"

_TABLE = {
    (GateStatus.PENDING, GateStatus.PENDING): ZoneMode.NO_CAPTURE,
    (GateStatus.PENDING, GateStatus.APPROVED): ZoneMode.NO_CAPTURE,
    (GateStatus.PENDING, GateStatus.REVOKED): ZoneMode.NO_CAPTURE,
    (GateStatus.REVOKED, GateStatus.PENDING): ZoneMode.NO_CAPTURE,
    (GateStatus.REVOKED, GateStatus.APPROVED): ZoneMode.NO_CAPTURE,
    (GateStatus.REVOKED, GateStatus.REVOKED): ZoneMode.NO_CAPTURE,
    (GateStatus.APPROVED, GateStatus.PENDING): ZoneMode.COMMISSIONING,
    (GateStatus.APPROVED, GateStatus.REVOKED): ZoneMode.COMMISSIONING,
    (GateStatus.APPROVED, GateStatus.APPROVED): ZoneMode.PRODUCTIVE,
}
"""BL §3.1, escrita a mano: el oráculo de PR-GOB-03."""


def _status(command: Command) -> GateStatus:
    return GateStatus.APPROVED if isinstance(command, Approve) else GateStatus.REVOKED


# --- PR-GOB-03 (dominio puro) -------------------------------------------------------------------


def test_pr_gob_03_resulting_mode_is_the_table_for_every_pair() -> None:
    assert len(_TABLE) == len(GateStatus) ** 2
    for (mounting, usage), mode in _TABLE.items():
        assert resulting_mode(mounting, usage) is mode
        assert resulting_mode(mounting.value, usage.value) is mode  # type: ignore[arg-type]


@given(scenario=gate_sequences())
def test_pr_gob_03_mode_is_total_never_productive_without_both_and_revoking_never_widens(
    scenario: GateScenario,
) -> None:
    zone = uuid.uuid4()
    state = ZoneGateState.initial(uuid.uuid4(), uuid.uuid4(), zone)
    now = T0
    for step in scenario.steps:
        now += step.advance
        for command in step.commands:
            gate = command.gate
            before = state
            try:
                plan = plan_transition(
                    state,
                    gate,
                    _status(command),
                    at=now,
                    decided_by=ACTOR,
                    record_id=uuid.uuid4() if isinstance(command, Approve) else None,
                    reason_es=None if isinstance(command, Approve) else REASON,
                )
            except GateRuleViolated as rejected:
                # Solo se rechaza revocar lo que no está aprobado; el estado no cambia.
                assert rejected.violation is GateViolation.NOT_APPROVED
                assert not isinstance(command, Approve)
                assert before.decision(gate).status is not GateStatus.APPROVED
                continue
            state = plan.state
            mounting, usage = state.mounting.status, state.usage.status
            assert state.resulting_mode is _TABLE[(mounting, usage)]
            productive = state.resulting_mode is ZoneMode.PRODUCTIVE
            assert productive == (mounting is usage is GateStatus.APPROVED)
            if not isinstance(command, Approve):
                assert mode_rank(state.resulting_mode) <= mode_rank(before.resulting_mode)
                assert state.decision(gate).status is GateStatus.REVOKED
                assert state.decision(gate).record_id == before.decision(gate).record_id
            # La otra compuerta no se toca nunca (las dos no se funden).
            other = GateKind.USAGE if gate is GateKind.MOUNTING else GateKind.MOUNTING
            assert state.decision(other) == before.decision(other)
            payload = gate_state_payload(state, now)
            contract = GateState.model_validate_json(json.dumps(payload))
            assert contract.resulting_mode.value == state.resulting_mode.value


def test_pr_gob_03_revoking_a_pending_or_revoked_gate_is_rejected() -> None:
    state = ZoneGateState.initial(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    for gate in GateKind:
        with pytest.raises(GateRuleViolated) as raised:
            plan_transition(
                state,
                gate,
                GateStatus.REVOKED,
                at=T0,
                decided_by=ACTOR,
                record_id=None,
                reason_es=REASON,
            )
        assert raised.value.violation is GateViolation.NOT_APPROVED
    approved = plan_transition(
        state,
        GateKind.MOUNTING,
        GateStatus.APPROVED,
        at=T0,
        decided_by=ACTOR,
        record_id=uuid.uuid4(),
        reason_es=None,
    ).state
    revoked = plan_transition(
        approved,
        GateKind.MOUNTING,
        GateStatus.REVOKED,
        at=T0,
        decided_by=ACTOR,
        record_id=None,
        reason_es=REASON,
    ).state
    with pytest.raises(GateRuleViolated):
        plan_transition(
            revoked,
            GateKind.MOUNTING,
            GateStatus.REVOKED,
            at=T0,
            decided_by=ACTOR,
            record_id=None,
            reason_es=REASON,
        )


@pytest.mark.parametrize(
    ("status", "record_id", "reason"),
    [
        (GateStatus.PENDING, uuid.UUID(int=1, version=4), None),  # nada vuelve a pending
        (GateStatus.APPROVED, None, None),  # aprobar exige respaldo
        (GateStatus.APPROVED, uuid.UUID(int=1, version=4), REASON),  # y ningún motivo
        (GateStatus.REVOKED, None, None),  # revocar exige motivo
        (GateStatus.REVOKED, None, "x" * 9),
        (GateStatus.REVOKED, None, "x" * 501),
    ],
)
def test_incoherent_transitions_are_rejected(
    status: GateStatus, record_id: uuid.UUID | None, reason: str | None
) -> None:
    state = ZoneGateState.initial(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    state = plan_transition(
        state,
        GateKind.USAGE,
        GateStatus.APPROVED,
        at=T0,
        decided_by=ACTOR,
        record_id=uuid.uuid4(),
        reason_es=None,
    ).state
    with pytest.raises(GateRuleViolated) as raised:
        plan_transition(
            state,
            GateKind.USAGE,
            status,
            at=T0,
            decided_by=ACTOR,
            record_id=record_id,
            reason_es=reason,
        )
    assert raised.value.violation is GateViolation.REQUEST_INVALID


def test_revoke_reason_edges_10_and_500_are_accepted() -> None:
    state = ZoneGateState.initial(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    state = plan_transition(
        state,
        GateKind.USAGE,
        GateStatus.APPROVED,
        at=T0,
        decided_by=ACTOR,
        record_id=uuid.uuid4(),
        reason_es=None,
    ).state
    for reason in ("x" * 10, "x" * 500):
        plan = plan_transition(
            state,
            GateKind.USAGE,
            GateStatus.REVOKED,
            at=T0,
            decided_by=ACTOR,
            record_id=None,
            reason_es=reason,
        )
        assert plan.state.usage.status is GateStatus.REVOKED


def test_the_payload_lasts_seven_days_and_hides_the_author() -> None:
    state = ZoneGateState.initial(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    state = plan_transition(
        state,
        GateKind.MOUNTING,
        GateStatus.APPROVED,
        at=T0,
        decided_by=ACTOR,
        record_id=uuid.uuid4(),
        reason_es=None,
    ).state
    payload = gate_state_payload(state, T0 + timedelta(microseconds=1500))
    assert payload["issued_at"] == "2026-10-01T08:00:00.001Z"
    assert payload["valid_until"] == "2026-10-08T08:00:00.001Z"
    assert "decided_by" not in str(payload) and str(ACTOR) not in str(payload)
    GateState.model_validate_json(json.dumps(payload))


# --- PR-GOB-21 (servicio real sobre PostgreSQL) -----------------------------------------------


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[GatesWorld]:
    with gates_world(postgres_endpoint, "pr_gob_gates") as world:
        yield world


async def _execute(
    gates: GateService, writer: ScopeContext, zone: uuid.UUID, command: Command
) -> GateTransition:
    approve = isinstance(command, Approve)

    async def body(transaction: Any) -> GateTransition:
        return await gates.transition_gate(
            transaction,
            writer,
            zone,
            command.gate,
            _status(command),
            uuid.uuid4() if approve else None,
            None if approve else REASON,
        )

    return await gates.run(writer, body)


def _window(row: Any) -> HalfOpenInterval:
    return HalfOpenInterval(row["effective_from"], row["effective_until"])


def _key(row: Any) -> tuple[str, datetime]:
    return (str(row["gate"]), row["effective_from"])


def _interval_key(interval: Any) -> tuple[str, datetime]:
    return (interval.gate.value, interval.effective_from)


@pytest.mark.integration
@given(scenario=gate_sequences(max_steps=6))
def test_pr_gob_21_the_database_rejects_overlaps_and_queries_match_the_oracle(
    world: GatesWorld, scenario: GateScenario
) -> None:
    site = world.site()
    ((_, zone),) = site.zones()
    member = world.member(site, Role.ADMINISTRATOR)
    writer = with_unit(member, ActorUnit.U03)
    clock = SimulatedClock(world.authz.sessions.clock.now())
    gates = world.build_gates(clock=clock)
    committed = 0
    for step in scenario.steps:
        clock.advance(step.advance.total_seconds())

        async def run_step(commands: tuple[Command, ...]) -> list[Any]:
            return list(
                await asyncio.gather(
                    *(_execute(gates, writer, zone, c) for c in commands), return_exceptions=True
                )
            )

        for outcome in world.run(run_step(step.commands)):
            # El candado ordena las concurrentes: transición o rechazo de negocio, nunca carrera.
            assert isinstance(outcome, GateTransition | GateConflict), outcome
            committed += isinstance(outcome, GateTransition)

    rows = world.history(zone)
    assert len(rows) == committed
    by_gate: dict[str, list[Any]] = {}
    for row in rows:
        by_gate.setdefault(row["gate"], []).append(row)
    # Historia contigua por compuerta: cada cierre es el inicio del siguiente; el último, abierto.
    for gate_rows in by_gate.values():
        for earlier, later in itertools.pairwise(gate_rows):
            assert earlier["effective_until"] == later["effective_from"]
        assert [r["effective_until"] is None for r in gate_rows].count(True) == 1
        assert gate_rows[-1]["effective_until"] is None
    projection = world.projection(zone)
    if rows:
        statuses = {
            gate: (by_gate[gate.value][-1]["status"] if gate.value in by_gate else "pending")
            for gate in GateKind
        }
        assert projection is not None
        assert projection["mounting"]["status"] == statuses[GateKind.MOUNTING]
        assert projection["usage"]["status"] == statuses[GateKind.USAGE]
        assert projection["resulting_mode"] == resulting_mode(
            GateStatus(statuses[GateKind.MOUNTING]), GateStatus(statuses[GateKind.USAGE])
        )
    else:
        assert projection is None

    # La base rechaza todo solapamiento de la misma zona y compuerta.
    for row in rows:
        start = row["effective_from"]
        for candidate in ((start, start + MILLISECOND), (start + MILLISECOND, None)):
            if row["effective_until"] is not None and candidate[1] is None:
                candidate = (row["effective_until"] - MILLISECOND, row["effective_until"])
            error = _insert_interval(world, member, site, zone, row["gate"], *candidate)
            assert error == EXCLUSION_VIOLATION, (row, candidate, error)

    # state_at: a lo sumo uno, el del oráculo, en cada instante de muestra.
    instants = sorted(
        {row["effective_from"] + d for row in rows for d in (-MILLISECOND, timedelta(0))}
        | {clock.now() + timedelta(days=1)}
    )
    repository = gates._repository  # la consulta de la base
    for gate in GateKind:
        gate_rows = by_gate.get(gate.value, [])
        for instant in instants:
            found = world.run(repository.state_at(member, zone, gate, instant))
            assert len(found) <= 1
            expected = containing(gate_rows, instant, _window)
            assert [_interval_key(i) for i in found] == (
                [] if expected is None else [_key(expected)]
            )
            served = world.run(gates.state_at(member, zone, gate, instant))
            assert (served is None) == (expected is None)
            if served is not None and expected is not None:
                assert _interval_key(served) == _key(expected)
                assert served.status.value == expected["status"]
                assert served.record_id == expected["record_id"]

    # gate_history(from, to): exactamente los que se solapan (consulta de la base y servicio).
    bounds = sorted({instants[0], *instants, instants[-1] + timedelta(hours=1)})
    for start, end in itertools.pairwise(bounds):
        _check_history(world, gates, member, zone, rows, start, end)
    _check_history(world, gates, member, zone, rows, bounds[0], bounds[-1])


def _check_history(
    world: GatesWorld,
    gates: GateService,
    member: ScopeContext,
    zone: uuid.UUID,
    rows: list[Any],
    start: datetime,
    end: datetime,
) -> None:
    query = HalfOpenInterval(start, end)
    expected = sorted(_key(row) for row in overlapping(rows, query, _window))
    raw = world.run(gates._repository.history(member, zone, start, end))
    assert sorted(_interval_key(i) for i in raw) == expected
    served = world.run(gates.gate_history(member, zone, start, end))
    assert sorted(_interval_key(i) for i in served) == expected


def _insert_interval(
    world: GatesWorld,
    context: ScopeContext,
    site: Any,
    zone: uuid.UUID,
    gate: str,
    start: datetime,
    end: datetime | None,
) -> str | None:
    """Intenta escribir un intervalo como ``vigia_app``; devuelve el SQLSTATE del rechazo."""
    plant = next(iter(site.plants))

    async def insert() -> None:
        async with world.database.transaction(context) as transaction:
            await transaction.execute(
                text(
                    "INSERT INTO catalog.gate_state_history (organization_id, plant_id, zone_id,"
                    " gate, status, effective_from, effective_until, decided_by,"
                    " ledger_record_id, record_id) VALUES (:org, :plant, :zone, :gate,"
                    " 'approved', :start, :end, :actor, :ledger, :record)"
                ),
                {
                    "org": context.organization_id,
                    "plant": plant,
                    "zone": zone,
                    "gate": gate,
                    "start": start,
                    "end": end,
                    "actor": context.actor.id,
                    "ledger": uuid.uuid4(),
                    "record": uuid.uuid4(),
                },
            )

    try:
        world.run(insert())
    except sa_exc.IntegrityError as error:
        return sqlstate(error)
    return None
