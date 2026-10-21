"""Contrapruebas acotadas de las inyecciones exclusivas del arnés de U-04."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from tests.resilience.laz_consumers import Outage, consumer_case
from tests.resilience.laz_support import (
    SelectiveProjectionDelivery,
    consecutive_breaches,
    expire_export,
    loop_metrics_export,
    run_worker_case,
)
from vigia_platform.loop.observability.metrics import LoopMetricName


def test_selective_delivery_does_not_discard_other_events() -> None:
    world = Mock()
    world.deliver.return_value = 7
    consumer = Mock()
    chosen = SimpleNamespace(event_name="finding_received")
    other = SimpleNamespace(event_name="catalog_updated")
    delivery = SelectiveProjectionDelivery(world, consumer, frozenset({"finding_received"}))
    assert delivery.deliver(chosen) == 0  # type: ignore[arg-type]
    world.deliver.assert_not_called()
    assert delivery.deliver(other) == 7  # type: ignore[arg-type]
    world.deliver.assert_called_once_with(consumer, other)
    assert delivery.dropped == [chosen]


def test_expiration_uses_only_the_injected_clock() -> None:
    moment = datetime(2030, 1, 1, tzinfo=UTC)
    stack = Mock()
    stack.now.return_value = moment
    expire_export(stack, "synthetic-id")
    sql, at, export_id = stack.execute.call_args.args
    assert "now()" not in sql
    assert at == moment - timedelta(milliseconds=1)
    assert export_id == "synthetic-id"


@pytest.mark.parametrize(
    ("ratios", "expected"),
    [
        ([], -1),
        ([0.06], -1),  # una sola evaluación por encima no alarma
        ([0.06, 0.04, 0.06], -1),  # no consecutivas
        ([0.05, 0.05], -1),  # en el umbral no es «por encima»
        ([0.0, 0.06, 0.07], 2),
        ([0.06, 0.06, 0.06], 1),
    ],
)
def test_consecutive_breaches_needs_two_evaluations_above(
    ratios: list[float], expected: int
) -> None:
    assert consecutive_breaches(ratios, 0.05, 2) == expected


def test_metrics_export_reads_the_in_memory_points() -> None:
    export = loop_metrics_export()
    name = LoopMetricName.ASSISTANT_TOKEN_BUDGET_USED_RATIO
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    assert export.value(name, organization_id=first) is None
    export.metrics.gauge(name).set(0.81, {"organization_id": first})
    export.metrics.gauge(name).set(0.2, {"organization_id": second})
    assert export.value(name, organization_id=first) == pytest.approx(0.81)
    assert export.value(name, organization_id=second) == pytest.approx(0.2)
    histogram = LoopMetricName.ASSISTANT_REQUEST_DURATION_MS
    export.metrics.histogram(histogram).record(1500, {"assistant_state": "ready"})
    export.metrics.histogram(histogram).record(90_000, {"assistant_state": "ready"})
    ((attributes, count, peak),) = export.histogram(histogram)
    assert (attributes, count, peak) == ({"assistant_state": "ready"}, 2, 90_000.0)


def test_outage_fails_without_calling_the_real_handler() -> None:
    calls: list[str] = []

    async def real(event: Any, transaction: Any) -> None:
        calls.append(event)

    outage = Outage(real, down=True)
    with pytest.raises(RuntimeError):
        asyncio.run(outage("evento", None))  # type: ignore[arg-type]
    assert calls == [] and outage.failures == 1
    outage.down = False
    asyncio.run(outage("evento", None))  # type: ignore[arg-type]
    assert calls == ["evento"]


def test_unknown_consumer_case_fails_closed() -> None:
    with pytest.raises(ValueError, match="desconocido"), consumer_case(Mock(), "otro"):
        pass


def test_unknown_worker_configuration_fails_closed() -> None:
    spec: dict[str, Any] = {"case": "not_a_case"}
    with pytest.raises(ValueError, match="no reconocido"):
        run_worker_case(spec)
