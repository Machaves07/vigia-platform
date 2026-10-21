"""Contrapruebas acotadas de las inyecciones exclusivas del arnés de U-04."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from tests.resilience.laz_support import (
    SelectiveProjectionDelivery,
    expire_export,
    run_worker_case,
)


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


def test_unknown_worker_configuration_fails_closed() -> None:
    spec: dict[str, Any] = {"case": "not_a_case"}
    with pytest.raises(ValueError, match="no reconocido"):
        run_worker_case(spec)
