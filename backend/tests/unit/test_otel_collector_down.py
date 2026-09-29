"""Colector de OpenTelemetry apagado: la aplicación no espera y lo descartado se cuenta.

PAT-NUC-RES-03 (fila «Colector de OpenTelemetry»): exportador con cola acotada; si el colector
cae se descartan tramos y métricas con contador local (``otel_dropped_total``) y la aplicación
nunca bloquea por telemetría. La prueba usa el exportador OTLP gRPC real contra un puerto sin
nadie escuchando: ``localhost:4317``, el del colector lateral (``infrastructure-design.md``
§9.1), y un puerto libre que el sistema acaba de dar y que nadie puede estar usando.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Iterator, Mapping

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from vigia_platform.shared.observability import tracing
from vigia_platform.shared.observability.metrics import MetricName

SPANS = 500
QUEUE = 16
EXPORT_TIMEOUT_SECONDS = 1.0
MAX_EMIT_SECONDS = 0.5
FLUSH_TIMEOUT_MILLIS = 200
"""500 tramos y 500 medidas; con un colector que bloqueara, un solo lote tardaría 1 s."""


def _listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def _dropped(reader: InMemoryMetricReader, signal: str) -> int:
    data = reader.get_metrics_data()
    if data is None:
        return 0
    total = 0
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name != MetricName.OTEL_DROPPED_TOTAL.value:
                    continue
                for point in metric.data.data_points:
                    attributes: Mapping[str, object] = point.attributes or {}
                    if attributes.get("signal") == signal:
                        total += int(getattr(point, "value", 0))
    return total


def _elapsed(start: float) -> float:
    return time.perf_counter() - start  # noqa: TID251 — la prueba mide tiempo real a propósito.


def _now() -> float:
    return time.perf_counter()  # noqa: TID251 — la prueba mide tiempo real a propósito.


@pytest.fixture(params=["localhost:4317", "puerto-libre"])
def closed_endpoint(request: pytest.FixtureRequest) -> Iterator[str]:
    if request.param == "localhost:4317":
        if _listening(4317):
            pytest.skip("hay un colector escuchando en localhost:4317; se prueba el puerto libre")
        yield "http://localhost:4317"
    else:
        port = _free_port()
        assert not _listening(port)
        yield f"http://127.0.0.1:{port}"


def test_collector_down_never_blocks_and_counts_every_drop(closed_endpoint: str) -> None:
    reader = InMemoryMetricReader()
    telemetry = tracing.configure_telemetry(
        tracing.TelemetrySettings(
            otlp_endpoint=closed_endpoint,
            max_queue_size=QUEUE,
            max_export_batch_size=QUEUE // 2,
            schedule_delay_seconds=0.05,
            export_timeout_seconds=EXPORT_TIMEOUT_SECONDS,
            metric_export_interval_seconds=3600,
        ),
        extra_metric_readers=[reader],
    )
    tracer = telemetry.tracer()
    try:
        start = _now()
        for _ in range(SPANS):
            with tracer.start_as_current_span("ledger.write"):
                telemetry.metrics.ledger_writes_total.add(1)
        emit_seconds = _elapsed(start)

        # La cola está llena y el hilo exportador espera al colector: se descarta al instante.
        dropped_while_emitting = _dropped(reader, "traces")

        # force_flush respeta su plazo aunque el hilo exportador siga esperando al colector.
        start = _now()
        flushed = telemetry.tracer_provider.force_flush(FLUSH_TIMEOUT_MILLIS)
        flush_seconds = _elapsed(start)

        # Métricas: el lector periódico exporta al colector caído y cuenta los puntos perdidos.
        start = _now()
        telemetry.meter_provider.force_flush()
        metric_flush_seconds = _elapsed(start)
        dropped_metrics = _dropped(reader, "metrics")

        start = _now()
        telemetry.tracer_provider.shutdown()
        shutdown_seconds = _elapsed(start)
        dropped_traces = _dropped(reader, "traces")
    finally:
        telemetry.meter_provider.shutdown()

    print(
        f"\ncolector={closed_endpoint} tramos={SPANS} emisión={emit_seconds * 1000:.1f} ms "
        f"descartados_al_emitir={dropped_while_emitting} "
        f"otel_dropped_total{{traces}}={dropped_traces} "
        f"otel_dropped_total{{metrics}}={dropped_metrics} "
        f"force_flush({FLUSH_TIMEOUT_MILLIS} ms)={flushed} en {flush_seconds * 1000:.0f} ms "
        f"flush_métricas={metric_flush_seconds:.2f} s cierre={shutdown_seconds:.2f} s"
    )
    assert emit_seconds < MAX_EMIT_SECONDS
    assert dropped_while_emitting > 0
    assert flush_seconds < FLUSH_TIMEOUT_MILLIS / 1000 + 0.3
    assert dropped_traces == SPANS  # Ningún tramo llegó: cada uno quedó contado.
    assert dropped_metrics > 0
    assert shutdown_seconds < EXPORT_TIMEOUT_SECONDS + 2


def test_queue_never_grows_past_its_bound() -> None:
    telemetry = tracing.configure_telemetry(
        tracing.TelemetrySettings(
            otlp_endpoint=f"http://127.0.0.1:{_free_port()}",
            max_queue_size=QUEUE,
            max_export_batch_size=QUEUE,
            schedule_delay_seconds=3600,
            export_timeout_seconds=EXPORT_TIMEOUT_SECONDS,
            metric_export_interval_seconds=3600,
        )
    )
    tracer = telemetry.tracer()
    try:
        for _ in range(SPANS):
            with tracer.start_as_current_span("ledger.write"):
                pass
            assert telemetry.span_processor.pending() <= QUEUE
    finally:
        telemetry.shutdown()


def test_collector_down_logs_one_line_per_signal_not_per_batch() -> None:
    import io
    import json
    import logging

    from vigia_platform.shared.observability.logging import configure_logging

    root = logging.getLogger()
    saved = list(root.handlers), root.level
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    telemetry = tracing.configure_telemetry(
        tracing.TelemetrySettings(
            otlp_endpoint=f"http://127.0.0.1:{_free_port()}",
            max_queue_size=QUEUE,
            max_export_batch_size=2,
            schedule_delay_seconds=0.01,
            export_timeout_seconds=0.3,
            metric_export_interval_seconds=3600,
        )
    )
    tracer = telemetry.tracer()
    try:
        for _ in range(3):
            for _ in range(QUEUE):
                with tracer.start_as_current_span("ledger.write"):
                    telemetry.metrics.ledger_writes_total.add(1)
            telemetry.tracer_provider.force_flush(2000)
            telemetry.meter_provider.force_flush()
    finally:
        telemetry.shutdown()
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    print("\n" + "\n".join(json.dumps(line, ensure_ascii=False) for line in lines))
    outage = [(line["level"], line.get("signal")) for line in lines]
    assert sorted(outage) == [("WARNING", "metrics"), ("WARNING", "traces")]
    assert all(line["component"] == "shared.observability" for line in lines)
