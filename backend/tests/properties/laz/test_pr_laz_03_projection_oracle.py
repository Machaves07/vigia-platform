"""PR-LAZ-03: lo enriquecido de cada elemento proyectado es lo que dicen sus fuentes (TASK-341;
LC-LAZ-02; domain-entities.md de U-04 §2.2 y §9 con la nota U04-H-05; BR-LAZ-149 y 152).

Propiedad pura (sin base), sobre una historia generada de la zona —cambios de la compuerta de uso
y de la cobertura (intervalos que **no se solapan**, como los de la implementación real) y
registros ``walk_test_regression_marked``/``_cleared`` del expediente— y hallazgos en instantes
generados (con sesgo a los bordes de cada cambio):

- ``observability_state_at_event`` es ``CoveragePort.estado_en(zona, instante)``;
- ``zone_gate_status_at_event`` es ``GateQueryPort.state_at(zona, uso, instante)`` (``pending`` sin
  intervalo, A-60);
- la anotación «acta no vigente» (``walk_test_regression_pending`` y su causa) es la del oráculo
  que **reproduce la historia** de regresión del expediente en orden hasta el instante, tanto al
  nacer la fila (``finding_received``) como al refrescar todas las filas de la zona con un evento
  ``regression_*`` (el consumidor sobre ``RecordingStore``);
- la marca unipersonal y la ventana son las de ``single_occupancy_many`` o, si no responde, las de
  la configuración con la zona unipersonal; el cubo es el instante truncado a esa ventana.

Perfil ``ci`` con la semilla fija y la de la sesión de ``tests/conftest.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from hypothesis import given
from hypothesis import strategies as st
from vigia_contracts.models.enumerations import GateStatus

from tests.laz_support import (
    FakeProjectionTransaction,
    FakeSettings,
    InstrumentedPorts,
    RecordingStore,
    enrichment_with,
    regression_row,
    synthetic_row,
)
from tests.writer_support import unit_context
from vigia_platform.catalog.domain.enums import GateKind
from vigia_platform.ledger.domain.coverage import CoverageState
from vigia_platform.loop.projection.consumer import LoopProjectionConsumer
from vigia_platform.loop.projection.fold import FindingStateRow, aggregation_bucket
from vigia_platform.shared.context import ActorKind, ActorUnit
from vigia_platform.shared.outbox.publish import OutboxEvent, partition_key

ORGANIZATION: Final = uuid.uuid4()
PLANT: Final = uuid.uuid4()
ZONE: Final = uuid.uuid4()
BASE: Final = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)
SPAN_MINUTES: Final = 3 * 24 * 60
CAUSES: Final = ("catalog_change", "model_version_change", "framing_recaptured")

offsets = st.integers(min_value=-SPAN_MINUTES, max_value=SPAN_MINUTES)
"""Minutos desde ``BASE``."""


def _moment(minutes: int) -> datetime:
    return BASE + timedelta(minutes=minutes)


@st.composite
def zone_history(draw: st.DrawFn) -> dict[str, Any]:
    gates = draw(
        st.lists(
            st.tuples(offsets, st.sampled_from(list(GateStatus))),
            max_size=6,
            unique_by=lambda change: change[0],
        )
    )
    coverage = draw(
        st.lists(
            st.tuples(offsets, st.sampled_from(list(CoverageState))),
            max_size=6,
            unique_by=lambda change: change[0],
        )
    )
    regression = draw(
        st.lists(
            st.tuples(offsets, st.booleans(), st.sampled_from(CAUSES)),
            max_size=8,
        )
    )
    borders = [change[0] for change in (*gates, *coverage)] + [mark[0] for mark in regression]
    near = st.sampled_from(borders).flatmap(
        lambda border: st.sampled_from([border - 1, border, border + 1])
    )
    instants = draw(
        st.lists(st.one_of(offsets, near) if borders else offsets, min_size=1, max_size=6)
    )
    occupancy = draw(st.none() | st.tuples(st.booleans(), st.integers(min_value=15, max_value=480)))
    default_window = draw(st.integers(min_value=15, max_value=480))
    return {
        "gates": gates,
        "coverage": coverage,
        "regression": regression,
        "instants": instants,
        "occupancy": occupancy,
        "default_window": default_window,
    }


def _ports(history: dict[str, Any]) -> InstrumentedPorts:
    occupancy = history["occupancy"]
    return InstrumentedPorts(
        organization_id=ORGANIZATION,
        plant_id=PLANT,
        occupancy={} if occupancy is None else {ZONE: occupancy},
        gate_changes={ZONE: [(_moment(at), status) for at, status in history["gates"]]},
        coverage_changes={ZONE: [(_moment(at), state) for at, state in history["coverage"]]},
        fail_single_occupancy=occupancy is None,
    )


def _ledger_rows(history: dict[str, Any]) -> list[Any]:
    """La historia de regresión en orden de cadena (el orden en que se generó)."""
    return [
        regression_row(ZONE, marked=marked, at=_moment(at), sequence=index + 1, cause=cause)
        for index, (at, marked, cause) in enumerate(history["regression"])
    ]


def _replayed(history: dict[str, Any], instant: datetime) -> tuple[bool, str | None]:
    """Oráculo: reproduce los registros en orden de su marca (en empate, de la cadena) hasta el
    instante; cada marca deja el acta no vigente con su causa y cada levantamiento, vigente."""
    state: tuple[bool, str | None] = (False, None)
    ordered = sorted(enumerate(history["regression"]), key=lambda item: (item[1][0], item[0]))
    for _, (at, marked, cause) in ordered:
        if _moment(at) > instant:
            break
        state = (True, cause) if marked else (False, None)
    return state


def _context() -> Any:
    return unit_context(ORGANIZATION, ActorUnit.U04, kind=ActorKind.SYSTEM)


async def _project(history: dict[str, Any], instant: datetime) -> tuple[FindingStateRow, Any]:
    ports = _ports(history)
    enrichment = enrichment_with(ports, settings=FakeSettings(history["default_window"]))
    transaction = FakeProjectionTransaction(_context(), regression=_ledger_rows(history))
    facts = await enrichment.finding_facts(
        transaction,
        plant_id=PLANT,
        zone_id=ZONE,
        occurred_at=instant,
        evidence_ids=[],
        new=True,
    )
    row = facts.enrich(None, synthetic_row(ORGANIZATION, PLANT, ZONE, instant))
    coverage = await ports.estado_en(_context(), ZONE, instant)
    interval = await ports.state_at(_context(), ZONE, GateKind.USAGE, instant)
    return row, (coverage, interval)


@given(history=zone_history())
def test_pr_laz_03_each_projected_finding_matches_its_sources(history: dict[str, Any]) -> None:
    for minutes in history["instants"]:
        instant = _moment(minutes)
        row, (coverage, interval) = asyncio.run(_project(history, instant))
        assert row.observability_state_at_event == coverage.state.value
        expected_gate = GateStatus.PENDING if interval is None else interval.status
        assert row.zone_gate_status_at_event == expected_gate.value
        pending, cause = _replayed(history, instant)
        assert (row.walk_test_regression_pending, row.walk_test_regression_cause) == (
            pending,
            cause,
        )
        occupancy = history["occupancy"]
        expected = (True, history["default_window"]) if occupancy is None else occupancy
        assert (row.single_occupancy_zone, row.aggregation_window_minutes) == expected
        assert row.occurred_at_bucket == aggregation_bucket(instant, expected[1])
        assert row.occurred_at_bucket <= row.occurred_at_exact


@given(history=zone_history())
def test_pr_laz_03_a_regression_event_refreshes_every_row_from_the_history(
    history: dict[str, Any],
) -> None:
    rows = [synthetic_row(ORGANIZATION, PLANT, ZONE, _moment(m)) for m in history["instants"]]
    store = RecordingStore(rows=list(rows))
    consumer = LoopProjectionConsumer(
        store=store,  # type: ignore[arg-type]
        reader=None,  # type: ignore[arg-type]
        enrichment=enrichment_with(_ports(history)),
        writer=None,  # type: ignore[arg-type]
        clock=None,  # type: ignore[arg-type]
    )
    event = OutboxEvent(
        event_id=uuid.uuid4(),
        organization_id=ORGANIZATION,
        plant_id=PLANT,
        event_name="regression_marked",
        partition_key=partition_key(ORGANIZATION, PLANT),
        ledger_sequence=None,
        payload={"zone_id": str(ZONE), "cause": "catalog_change", "affected_row_ids": "all"},
        correlation_id=uuid.uuid4(),
        created_at=BASE,
    )
    transaction = FakeProjectionTransaction(_context(), regression=_ledger_rows(history))
    written = asyncio.run(consumer.apply(event, transaction))  # type: ignore[arg-type]
    assert written == len(rows)
    for before, after in zip(rows, store.rows, strict=True):
        assert (after.walk_test_regression_pending, after.walk_test_regression_cause) == (
            _replayed(history, before.occurred_at_exact)
        )
        # El evento de regresión no toca nada más de la fila.
        assert after.single_occupancy_zone == before.single_occupancy_zone
        assert after.aggregation_window_minutes == before.aggregation_window_minutes
