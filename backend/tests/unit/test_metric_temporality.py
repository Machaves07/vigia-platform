"""Temporalidad de la configuración OTLP del proceso, sin AWS (A-47, VIG-258)."""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.metrics.v1.metrics_pb2 import (
    AGGREGATION_TEMPORALITY_CUMULATIVE,
    AGGREGATION_TEMPORALITY_DELTA,
)
from opentelemetry.sdk.metrics.export import MetricExportResult, MetricsData
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from vigia_platform.shared.observability import tracing


@pytest.mark.parametrize("preference", [None, "CUMULATIVE", "DELTA", "LOWMEMORY"])
@pytest.mark.parametrize(
    ("name", "attributes"),
    [
        ("operation_duration_ms", {"operation": "ledger_list"}),
        ("http_server_duration_ms", {"http.request.method": "GET"}),
        ("automatic_duration_ms", {"operation": "ledger_list"}),
    ],
)
def test_process_exports_only_current_histogram_interval_and_preserves_counters(
    monkeypatch: pytest.MonkeyPatch,
    preference: str | None,
    name: str,
    attributes: dict[str, str],
) -> None:
    """La segunda distribución no arrastra muestras rápidas; ni la tercera repite el pico."""
    variable = "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE"
    if preference is None:
        monkeypatch.delenv(variable, raising=False)
    else:
        monkeypatch.setenv(variable, preference)
    batches: list[ExportMetricsServiceRequest] = []

    def capture(self: OTLPMetricExporter, data: MetricsData, **kwargs: Any) -> MetricExportResult:
        # Conserva construcción, preferencias y serialización reales; solo sustituye la red.
        batches.append(self._translate_data(data))
        return MetricExportResult.SUCCESS

    monkeypatch.setattr(OTLPMetricExporter, "_export", capture)
    built = tracing.configure_telemetry(
        tracing.TelemetrySettings(metric_export_interval_seconds=3600),
        span_exporter=InMemorySpanExporter(),
    )
    meter = built.meter_provider.get_meter("prueba-temporalidad")
    histogram = meter.create_histogram(name, unit="ms")
    counter = built.metrics.ledger_writes_total
    try:
        for value, count, increment in [(1, 1000, 2), (1000, 20, 3), (0, 1, 4)]:
            for _ in range(count):
                histogram.record(value, attributes)
            counter.add(increment)
            assert built.meter_provider.force_flush()
        assert len(batches) == 3
        for index, (value, count) in enumerate([(1, 1000), (1000, 20), (0, 1)]):
            metrics = {
                metric.name: metric
                for resource in batches[index].resource_metrics
                for scope in resource.scope_metrics
                for metric in scope.metrics
            }
            exported = metrics[name]
            assert exported.WhichOneof("data") == "exponential_histogram"
            histogram_data = exported.exponential_histogram
            assert histogram_data.aggregation_temporality == AGGREGATION_TEMPORALITY_DELTA
            (point,) = histogram_data.data_points
            assert point.count == count
            assert point.sum == value * count
            assert point.min == point.max == value
            assert point.zero_count + sum(point.positive.bucket_counts) == count
            assert {a.key: a.value.string_value for a in point.attributes} == attributes
            assert not point.exemplars
            if index:
                previous = next(
                    m.exponential_histogram.data_points[0]
                    for r in batches[index - 1].resource_metrics
                    for s in r.scope_metrics
                    for m in s.metrics
                    if m.name == name
                )
                assert point.start_time_unix_nano == previous.time_unix_nano
            summed = metrics["ledger_writes_total"].sum
            # La preferencia del exportador sigue mandando SOLO para los contadores.
            delta_counter = preference in {"DELTA", "LOWMEMORY"}
            assert summed.aggregation_temporality == (
                AGGREGATION_TEMPORALITY_DELTA
                if delta_counter
                else AGGREGATION_TEMPORALITY_CUMULATIVE
            )
            (counter_point,) = summed.data_points
            assert counter_point.as_int == ([2, 3, 4] if delta_counter else [2, 5, 9])[index]
            assert not counter_point.attributes
    finally:
        built.shutdown()
