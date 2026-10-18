"""Presupuesto de llamadas a U-03 del enriquecimiento de la proyección (TASK-341; NFR-LAZ-03 y 05;
PAT-LAZ-REN-05; SUP-U03-04).

Con el doble instrumentado (``InstrumentedPorts``, ``tests/laz_support.py``), sin base:

- ``single_occupancy_many``: N zonas cuestan ``ceil(N/50)`` llamadas, ninguna de más de 50 zonas,
  y cada zona sale una vez aunque se repita;
- sin respuesta de un lote, **sus** zonas quedan unipersonales con la ventana por defecto de la
  configuración (fallo hacia la privacidad); las de otro lote que sí respondió, no;
- **ninguna llamada dentro de un bucle sobre elementos**: el número de llamadas de un evento por
  zona no cambia entre 1 y 500 filas de la zona, ni el de un hallazgo con 1 o 30 evidencias (una
  sola consulta de marcas);
- cada evento consulta solo lo que cambia: ``catalog_updated`` la ocupación, ``regression_*`` la
  historia (una sentencia), ``gate_state_changed`` y ``user_deactivated`` ningún puerto.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest

from tests.laz_support import (
    FakeMarkers,
    FakeProjectionTransaction,
    FakeSettings,
    InstrumentedPorts,
    RecordingStore,
    enrichment_with,
    synthetic_row,
)
from tests.writer_support import unit_context
from vigia_platform.loop.projection.consumer import LoopProjectionConsumer
from vigia_platform.loop.projection.fold import FindingStateRow
from vigia_platform.shared.context import ActorKind, ActorUnit
from vigia_platform.shared.outbox.publish import OutboxEvent, partition_key

ORGANIZATION: Final = uuid.uuid4()
PLANT: Final = uuid.uuid4()
AT: Final = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)
DEFAULT_WINDOW: Final = 240


def _context() -> Any:
    return unit_context(ORGANIZATION, ActorUnit.U04, kind=ActorKind.SYSTEM)


def _zones(count: int) -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(count)]


def _ports(zones: list[uuid.UUID], **changes: Any) -> InstrumentedPorts:
    return InstrumentedPorts(
        organization_id=ORGANIZATION,
        plant_id=PLANT,
        occupancy={zone: (False, 30) for zone in zones},
        **changes,
    )


# --- single_occupancy_many ---------------------------------------------------------------------


@pytest.mark.parametrize("count", [0, 1, 49, 50, 51, 100, 101, 250])
def test_n_zones_take_ceil_n_over_50_batch_calls(count: int) -> None:
    zones = _zones(count)
    ports = _ports(zones)
    result = asyncio.run(enrichment_with(ports).occupancy(_context(), zones, DEFAULT_WINDOW))
    assert ports.calls["single_occupancy_many"] == math.ceil(count / 50)
    assert set(result) == set(zones)
    assert all(value.answered and not value.single_occupancy for value in result.values())


def test_repeated_zones_are_asked_once() -> None:
    zones = _zones(50)
    ports = _ports(zones)
    asyncio.run(enrichment_with(ports).occupancy(_context(), zones * 3, DEFAULT_WINDOW))
    assert ports.calls["single_occupancy_many"] == 1


def test_an_unanswered_batch_aggregates_its_zones_with_the_default_window() -> None:
    zones = _zones(60)
    ports = _ports(zones, fail_single_occupancy=True)
    result = asyncio.run(enrichment_with(ports).occupancy(_context(), zones, DEFAULT_WINDOW))
    assert ports.calls["single_occupancy_many"] == 2
    assert {
        (value.single_occupancy, value.aggregation_window_minutes) for value in result.values()
    } == {(True, DEFAULT_WINDOW)}
    assert not any(value.answered for value in result.values())


def test_only_the_unanswered_batch_falls_back() -> None:
    zones = _zones(60)
    ports = _ports(zones)
    del ports.occupancy[zones[55]]  # el segundo lote no responde (ResourceNotFound en la real)
    result = asyncio.run(enrichment_with(ports).occupancy(_context(), zones, DEFAULT_WINDOW))
    assert all(result[zone].answered for zone in zones[:50])
    assert all(
        (
            result[zone].single_occupancy,
            result[zone].aggregation_window_minutes,
            result[zone].answered,
        )
        == (True, DEFAULT_WINDOW, False)
        for zone in zones[50:]
    )


# --- hallazgo nuevo ----------------------------------------------------------------------------


@pytest.mark.parametrize("evidences", [1, 30])
def test_a_new_finding_asks_each_port_once_whatever_its_evidences(evidences: int) -> None:
    zone = uuid.uuid4()
    ports = _ports([zone])
    markers = FakeMarkers()
    enrichment = enrichment_with(ports, markers=markers)
    transaction = FakeProjectionTransaction(_context())
    asyncio.run(
        enrichment.finding_facts(
            transaction,
            plant_id=PLANT,
            zone_id=zone,
            occurred_at=AT,
            evidence_ids=[uuid.uuid4() for _ in range(evidences)],
            new=True,
        )
    )
    assert dict(ports.calls) == {"estado_en": 1, "state_at": 1, "single_occupancy_many": 1}
    assert markers.batches == [evidences]
    assert len(transaction.statements) == 1  # la historia de regresión de la zona


def test_a_segment_of_a_known_finding_only_reads_its_markers() -> None:
    zone = uuid.uuid4()
    ports = _ports([zone])
    markers = FakeMarkers()
    transaction = FakeProjectionTransaction(_context())
    asyncio.run(
        enrichment_with(ports, markers=markers).finding_facts(
            transaction,
            plant_id=PLANT,
            zone_id=zone,
            occurred_at=AT,
            evidence_ids=[uuid.uuid4()],
            new=False,
        )
    )
    assert dict(ports.calls) == {}
    assert markers.batches == [1] and transaction.statements == []


# --- eventos por zona: el recuento no crece con las filas -------------------------------------


def _rows(zones: list[uuid.UUID], per_zone: int) -> list[FindingStateRow]:
    return [
        synthetic_row(ORGANIZATION, PLANT, zone, AT + timedelta(minutes=index))
        for zone in zones
        for index in range(per_zone)
    ]


def _event(name: str, payload: dict[str, Any], *, plant: bool = True) -> OutboxEvent:
    plant_id = PLANT if plant else None
    return OutboxEvent(
        event_id=uuid.uuid4(),
        organization_id=ORGANIZATION,
        plant_id=plant_id,
        event_name=name,
        partition_key=partition_key(ORGANIZATION, plant_id),
        ledger_sequence=None,
        payload=payload,
        correlation_id=uuid.uuid4(),
        created_at=AT,
    )


def _deliver(
    event: OutboxEvent, ports: InstrumentedPorts, rows: list[FindingStateRow]
) -> tuple[RecordingStore, FakeProjectionTransaction]:
    store = RecordingStore(rows=list(rows))
    consumer = LoopProjectionConsumer(
        store=store,  # type: ignore[arg-type]
        reader=None,  # type: ignore[arg-type]
        enrichment=enrichment_with(ports, settings=FakeSettings(DEFAULT_WINDOW)),
        writer=None,  # type: ignore[arg-type]
        clock=None,  # type: ignore[arg-type]
    )
    transaction = FakeProjectionTransaction(_context())
    asyncio.run(consumer.apply(event, transaction))  # type: ignore[arg-type]
    return store, transaction


def _node_event(zones: list[uuid.UUID]) -> OutboxEvent:
    return _event(
        "node_revoked",
        {
            "node_id": str(uuid.uuid4()),
            "plant_id": str(PLANT),
            "zone_ids": [str(zone) for zone in zones],
        },
    )


@pytest.mark.parametrize("per_zone", [1, 5])
def test_a_node_event_of_n_zones_costs_the_same_for_one_row_or_many(per_zone: int) -> None:
    zones = _zones(120)
    ports = _ports(zones)
    store, transaction = _deliver(_node_event(zones), ports, _rows(zones, per_zone))
    assert dict(ports.calls) == {"single_occupancy_many": math.ceil(120 / 50)}
    assert len(transaction.statements) == 1  # la historia de las 120 zonas, de una vez
    assert len(store.calls) == 1 and store.calls[0]["zone_ids"] == zones
    assert {row.aggregation_window_minutes for row in store.rows} == {30}


def test_a_zone_with_500_rows_costs_what_a_zone_with_one_costs() -> None:
    zone = _zones(1)
    counts = []
    for per_zone in (1, 500):
        ports = _ports(zone)
        event = _event(
            "catalog_updated",
            {"zone_id": str(zone[0]), "catalog_version": 2, "changed_fields": ["single_occupancy"]},
        )
        store, transaction = _deliver(event, ports, _rows(zone, per_zone))
        counts.append((dict(ports.calls), len(transaction.statements), len(store.calls)))
    assert counts[0] == counts[1] == ({"single_occupancy_many": 1}, 0, 1)


def test_regression_events_read_the_history_once_and_ask_no_port() -> None:
    zone = _zones(1)
    ports = _ports(zone)
    event = _event(
        "regression_marked",
        {"zone_id": str(zone[0]), "cause": "catalog_change", "affected_row_ids": "all"},
    )
    store, transaction = _deliver(event, ports, _rows(zone, 50))
    assert dict(ports.calls) == {} and len(transaction.statements) == 1 and len(store.calls) == 1


def test_gate_and_user_events_ask_no_port() -> None:
    zone = _zones(1)
    ports = _ports(zone)
    gate = _event(
        "gate_state_changed",
        {
            "zone_id": str(zone[0]),
            "gate": "usage",
            "status": "revoked",
            "resulting_mode": "commissioning",
            "record_id": str(uuid.uuid4()),
            "reason_es_present": True,
        },
    )
    user = _event(
        "user_deactivated",
        {"user_id": str(uuid.uuid4()), "deactivated_by": str(uuid.uuid4()), "deactivated_at": "x"},
        plant=False,
    )
    for event in (gate, user):
        _, transaction = _deliver(event, ports, _rows(zone, 10))
        assert transaction.statements == []
    assert dict(ports.calls) == {}
