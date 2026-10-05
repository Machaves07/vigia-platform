"""PR-GOB-17: tolerancia de reloj acotada y antigüedad máxima (BR-GOB-90, 91; nota T-12; TASK-221).

Con ``clock_offsets`` (reloj declarado con los bordes de la cota de 5 minutos) y ``gate_sequences``
(historia de la compuerta de uso), por la aplicación real y el ``IngestService`` real:

- **compuerta**: un hallazgo o una detección se acepta si y solo si el uso estuvo aprobado en algún
  instante de ``[started_at - tol, started_at + tol]`` con ``tol = min(|offset_ms|, 300 000 ms)``, o
  en el instante de recepción si ``synchronized = false`` (oráculo ``UsageHistory``, independiente
  del dominio). Los ``started_at`` caen en los cambios de la compuerta y a su alrededor, en los
  bordes de la tolerancia;
- **antigüedad**: con ``sent_records_retention_days`` (sin configuración, 1, 30, 90 y fuera de
  rango,
  que se acota a 1..90), un registro con ``ended_at + tol < received_at - retención`` es siempre
  ``timestamp_out_of_window`` (422, permanente) y nunca se acepta; uno dentro, sí. Vale para los
  tres
  tipos (los eventos también tienen antigüedad máxima).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from vigia_contracts.conformance.generators import (
    detection_for_review,
    finding,
    observability_event,
    zone_catalog,
)
from vigia_contracts.models.api import parse_rejection_response

from tests.ingest_support import IngestWorld, ingest_world, place
from tests.properties.gob.strategies.clock import (
    LIMIT_MS,
    ClockDeclaration,
    UsageHistory,
    clock_offsets,
    expected_window,
    fact_offsets,
)
from tests.properties.gob.strategies.gates import Approve, GateScenario, gate_sequences
from vigia_platform.catalog.domain.enums import GateKind
from vigia_platform.fleet.domain.ingest_order import AssignmentSpan, IngestKind

YEAR = dt.timedelta(days=365)
DAY = dt.timedelta(days=1)
MILLISECOND = dt.timedelta(milliseconds=1)
DURATION = dt.timedelta(seconds=30)


def _strategy(kind: IngestKind, catalog: dict[str, Any], node_id: str) -> st.SearchStrategy[Any]:
    if kind is IngestKind.FINDING:
        return finding(catalog, node_id=node_id)
    if kind is IngestKind.DETECTION_FOR_REVIEW:
        return detection_for_review(catalog, node_id=node_id)
    return observability_event(catalog, node_id=node_id)


def _world(catalog: dict[str, Any]) -> tuple[IngestWorld, dict[str, Any]]:
    world = ingest_world()
    world.store.assignment_rows = [
        (world.a.node_id, AssignmentSpan(world.zone, world.now - 2 * YEAR, None))
    ]
    scoped = world.scoped(catalog)
    world.publish_catalog(scoped, world.now - 2 * YEAR)
    return world, scoped


def _apply(world: IngestWorld, scenario: GateScenario, start: dt.datetime) -> UsageHistory:
    """La historia de uso de ``scenario`` desde ``start`` (los cambios de montaje no cuentan).

    Empieza siempre con una aprobación del uso en ``start``: sin ella, la mitad de las secuencias
    no tendría ningún cambio de uso y no habría bordes de la tolerancia que mirar.
    """
    history = UsageHistory()
    history.change(start, True)
    world.set_usage(True, start)
    moment = start
    for step in scenario.steps:
        moment += step.advance
        for command in step.commands:
            if command.gate is not GateKind.USAGE:
                continue
            approve = isinstance(command, Approve)
            if not approve and not history.approved_now:
                continue
            if history.changes and moment <= history.changes[-1][0]:
                moment = history.changes[-1][0] + MILLISECOND
            history.change(moment, approve)
            world.set_usage(approve, moment)
    return history


@settings(suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(
    data=st.data(),
    catalog=zone_catalog(),
    scenario=gate_sequences(max_steps=6, concurrent=False),
    clock=clock_offsets(),
    offset=fact_offsets(),
    kind=st.sampled_from([IngestKind.FINDING, IngestKind.DETECTION_FOR_REVIEW]),
)
def test_pr_gob_17_the_gate_decision_uses_the_bounded_tolerance_or_the_reception(
    data: st.DataObject,
    catalog: dict[str, Any],
    scenario: GateScenario,
    clock: ClockDeclaration,
    offset: dt.timedelta,
    kind: IngestKind,
) -> None:
    world, scoped = _world(catalog)
    history = _apply(world, scenario, world.now - 8 * dt.timedelta(hours=1))
    anchors = [at for at, _ in history.changes] or [world.now]
    anchor = data.draw(st.sampled_from(anchors), label="anchor")
    started = anchor - offset
    document = place(
        data.draw(_strategy(kind, scoped, str(world.a.node_id)), label="document"),
        started,
        duration=DURATION,
        synchronized=clock.synchronized,
        offset_ms=clock.offset_ms,
    )
    received_at = world.now
    response = world.post(kind, document)
    if history.approved_during(*expected_window(started, clock, received_at)):
        assert response.status_code == 200, (clock, offset, history, response.text)
    else:
        assert response.status_code == 403, (clock, offset, history, response.text)
        code = parse_rejection_response(response.content).code.value
        assert code == "zone_gate_not_approved"


RETENTIONS = (None, 1, 30, 90, 91, 400)


def _bounded(days: int | None) -> dt.timedelta:
    return dt.timedelta(days=30 if days is None else min(max(days, 1), 90))


@settings(suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(
    data=st.data(),
    catalog=zone_catalog(),
    clock=clock_offsets(),
    retention=st.sampled_from(RETENTIONS),
    margin_ms=st.one_of(
        st.sampled_from([-1, 0, 1, -LIMIT_MS, LIMIT_MS, -(LIMIT_MS + 1), LIMIT_MS + 1]),
        st.integers(-2 * LIMIT_MS, 2 * LIMIT_MS),
    ),
    kind=st.sampled_from(tuple(IngestKind)),
)
def test_pr_gob_17_never_accepts_beyond_the_maximum_age(
    data: st.DataObject,
    catalog: dict[str, Any],
    clock: ClockDeclaration,
    retention: int | None,
    margin_ms: int,
    kind: IngestKind,
) -> None:
    world, scoped = _world(catalog)
    world.set_usage(True, world.now - 2 * YEAR)
    if retention is not None:
        world.store.retention[world.a.node_id] = retention
    received_at = world.now
    oldest = received_at - _bounded(retention)
    # ``ended_at`` en el borde de la antigüedad, desplazado ``margin_ms``.
    ended = oldest + dt.timedelta(milliseconds=margin_ms)
    document = place(
        data.draw(_strategy(kind, scoped, str(world.a.node_id)), label="document"),
        ended - DURATION,
        duration=DURATION,
        synchronized=clock.synchronized,
        offset_ms=clock.offset_ms,
    )
    response = world.post(kind, document)
    tolerance = dt.timedelta(milliseconds=min(abs(clock.offset_ms), LIMIT_MS))
    if ended + tolerance < oldest:
        assert response.status_code == 422, response.text
        rejection = parse_rejection_response(response.content)
        assert (rejection.code.value, rejection.retryable) == ("timestamp_out_of_window", False)
        assert world.accepted(kind) == []
    else:
        assert response.status_code == 200, (retention, margin_ms, clock, response.text)
