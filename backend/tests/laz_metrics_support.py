"""Captura aislada de instrumentos de U-04, sin cambiar el proveedor global de OTel."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from sqlalchemy.dialects.postgresql.asyncpg import dialect

from vigia_platform.loop.observability.metrics import LoopMetrics


@dataclass
class MetricCapture:
    metrics: LoopMetrics
    reader: InMemoryMetricReader

    def points(self, name: str) -> list[Any]:
        data = self.reader.get_metrics_data()
        if data is None:
            return []
        return [
            point
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
            if metric.name == name
            for point in metric.data.data_points
        ]


async def unscoped_rows(connection: Any, statement: Any, **parameters: Any) -> list[Any]:
    """Ejecuta con el administrador sintético: RLS no oculta la retirada de un filtro."""
    bindings = statement.compile().params
    compiled = statement.bindparams(
        **{k: v for k, v in parameters.items() if k in bindings}
    ).compile(dialect=dialect())
    return list(
        await connection.fetch(
            str(compiled), *(compiled.params[name] for name in compiled.positiontup)
        )
    )


@pytest.fixture(name="capture")
def metric_capture(monkeypatch: pytest.MonkeyPatch) -> Iterator[MetricCapture]:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    metrics = LoopMetrics(provider.get_meter("prueba-u04"))
    for module in (
        "vigia_platform.loop.observability.worker",
        "vigia_platform.loop.validators.vocabulary",
        "vigia_platform.loop.validators.attribution",
        "vigia_platform.accreditation.export.worker",
        "vigia_platform.accreditation.export.request",
        "vigia_platform.notifications.metrics",
    ):
        monkeypatch.setattr(f"{module}.get_loop_metrics", lambda: metrics)
    yield MetricCapture(metrics, reader)
    provider.shutdown()
