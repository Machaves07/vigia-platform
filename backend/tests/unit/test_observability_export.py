"""Instrumentación automática, exportación limpia de métricas y aislamiento de los módulos.

La instrumentación de FastAPI, SQLAlchemy, httpx y botocore recibe un token en la ruta, en la
consulta, en un parámetro SQL y en la clave de S3; ninguno sale en los tramos ni en las métricas
exportados (NFR-NUC-41, 43). Solo quedan identificadores y enumeraciones.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricExportResult,
    MetricsData,
)
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from vigia_platform.shared.observability import redaction, tracing

BACKEND = Path(__file__).resolve().parents[2]
TOKEN = "Zm9vYmFyYmF6cXV4cXV1eDEyMzQ1Njc4OTA"  # noqa: S105 — token falso de la prueba.
ROUTE = "/v1/invitations/{invitation_token}"
POLICY = redaction.AttributePolicy()
"""Política propia de estas pruebas: registrar rutas no toca la del proceso."""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


class _Collecting(MetricExporter):
    def __init__(self) -> None:
        super().__init__()
        self.batches: list[MetricsData] = []

    def export(
        self, metrics_data: MetricsData, timeout_millis: float = 10_000, **kwargs: Any
    ) -> MetricExportResult:
        self.batches.append(metrics_data)
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: Any) -> None:
        return None


@pytest.fixture
def telemetry() -> Iterator[tuple[tracing.Telemetry, InMemorySpanExporter, _Collecting]]:
    spans, metrics = InMemorySpanExporter(), _Collecting()
    built = tracing.configure_telemetry(
        tracing.TelemetrySettings(schedule_delay_seconds=3600, metric_export_interval_seconds=3600),
        span_exporter=spans,
        metric_exporter=metrics,
        policy=POLICY,
    )
    yield built, spans, metrics
    built.shutdown()


def _flush(built: tracing.Telemetry) -> None:
    built.span_processor.force_flush()
    built.meter_provider.force_flush()


def _dump(spans: list[ReadableSpan], metrics: list[MetricsData]) -> str:
    parts: list[object] = []
    for span in spans:
        parts += [span.name, dict(span.attributes or {}), span.status.description]
        parts += [(e.name, dict(e.attributes or {})) for e in span.events]
    for batch in metrics:
        for resource in batch.resource_metrics:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    parts += [dict(p.attributes or {}) for p in metric.data.data_points]
    return json.dumps(parts, default=str)


def test_fastapi_route_template_survives_raw_path_and_query_do_not(
    telemetry: tuple[tracing.Telemetry, InMemorySpanExporter, _Collecting],
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    built, spans, metrics = telemetry
    app = FastAPI()

    @app.get(ROUTE)
    def invitation(invitation_token: str) -> dict[str, str]:
        return {"ok": "si"}

    POLICY.register("route", [ROUTE])
    handle = tracing.enable_auto_instrumentation(built, app=app, httpx=False, botocore=False)
    try:
        with TestClient(app) as client:
            assert client.get(f"/v1/invitations/{TOKEN}?t={TOKEN}").status_code == 200
    finally:
        handle.uninstrument()
    _flush(built)
    dump = _dump(list(spans.get_finished_spans()), metrics.batches)
    assert TOKEN not in dump
    server = [s for s in spans.get_finished_spans() if (s.attributes or {}).get("http.route")]
    assert server and all((s.attributes or {})["http.route"] == ROUTE for s in server)


def test_sqlalchemy_statement_and_parameters_do_not_leave(
    telemetry: tuple[tracing.Telemetry, InMemorySpanExporter, _Collecting],
) -> None:
    from sqlalchemy import create_engine, text

    built, spans, metrics = telemetry
    engine = create_engine("sqlite://")
    handle = tracing.enable_auto_instrumentation(built, engine=engine, httpx=False, botocore=False)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT :token").bindparams(token=TOKEN)).scalar_one()
    finally:
        handle.uninstrument()
    _flush(built)
    finished = list(spans.get_finished_spans())
    assert finished
    assert TOKEN not in _dump(finished, metrics.batches)
    for span in finished:
        assert not {"db.statement", "db.query.text"} & set(span.attributes or {})


def test_httpx_url_and_query_do_not_leave(
    telemetry: tuple[tracing.Telemetry, InMemorySpanExporter, _Collecting],
) -> None:
    import httpx

    built, spans, metrics = telemetry
    handle = tracing.enable_auto_instrumentation(built, botocore=False)
    port = _free_port()
    try:
        with httpx.Client(timeout=1.0) as client, pytest.raises(httpx.ConnectError):
            client.get(f"http://127.0.0.1:{port}/range/{TOKEN}?k={TOKEN}")
    finally:
        handle.uninstrument()
    _flush(built)
    finished = list(spans.get_finished_spans())
    assert finished
    dump = _dump(finished, metrics.batches)
    assert TOKEN not in dump and str(port) not in dump and "127.0.0.1" not in dump
    assert (finished[0].attributes or {}).get("http.request.method", "GET") == "GET"


def test_botocore_object_key_does_not_leave(
    telemetry: tuple[tracing.Telemetry, InMemorySpanExporter, _Collecting],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import boto3  # type: ignore[import-untyped]
    from botocore.config import Config  # type: ignore[import-untyped]
    from botocore.stub import Stubber  # type: ignore[import-untyped]

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "pruebas")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "pruebas")
    built, spans, metrics = telemetry
    handle = tracing.enable_auto_instrumentation(built, httpx=False)
    try:
        client = boto3.client(
            "s3",
            region_name="us-east-1",
            config=Config(connect_timeout=1, read_timeout=1, retries={"max_attempts": 0}),
        )
        with Stubber(client) as stubber:
            stubber.add_response("head_object", {}, {"Bucket": "vigia-evidencias", "Key": TOKEN})
            client.head_object(Bucket="vigia-evidencias", Key=TOKEN)
    finally:
        handle.uninstrument()
    _flush(built)
    finished = list(spans.get_finished_spans())
    assert finished
    dump = _dump(finished, metrics.batches)
    assert TOKEN not in dump and "vigia-evidencias" not in dump
    assert (finished[0].attributes or {}).get("rpc.method") == "HeadObject"


def test_export_merges_series_that_become_equal(
    telemetry: tuple[tracing.Telemetry, InMemorySpanExporter, _Collecting],
) -> None:
    built, _, metrics = telemetry
    raw = built.meter_provider.get_meter("instrumentacion").create_counter("peticiones")
    raw.add(2, {"http.route": "/no/registrada/uno"})
    raw.add(3, {"http.route": "/no/registrada/dos"})
    histogram = built.meter_provider.get_meter("instrumentacion").create_histogram("duracion")
    histogram.record(4, {"http.route": "/a"})
    histogram.record(6, {"http.route": "/b"})
    _flush(built)
    found: dict[str, list[Any]] = {}
    for batch in metrics.batches:
        for resource in batch.resource_metrics:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    found[metric.name] = list(metric.data.data_points)
    (counter,) = found["peticiones"]
    assert dict(counter.attributes) == {"http.route": redaction.OTHER} and counter.value == 5
    (merged,) = found["duracion"]
    assert merged.count == 2 and merged.sum == 10 and merged.min == 4 and merged.max == 6
    # Exponencial (``tracing.metric_aggregation``): las dos cuentas siguen en sus cubos.
    assert sum(merged.positive.bucket_counts) == 2 and list(merged.exemplars) == []


def test_platform_metric_drops_attributes_outside_its_spec(
    telemetry: tuple[tracing.Telemetry, InMemorySpanExporter, _Collecting],
) -> None:
    built, _, metrics = telemetry
    built.metrics.security_alert_total.add(
        1, {"alert_type": "security_alert", "task": "evidence_sample", "correlation_id": "x"}
    )
    _flush(built)
    points = [
        dict(point.attributes or {})
        for batch in metrics.batches
        for resource in batch.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "security_alert_total"
        for point in metric.data.data_points
    ]
    assert points == [{"alert_type": "security_alert"}]


def test_observability_modules_import_without_fastapi_or_sqlalchemy() -> None:
    modules = [
        "vigia_platform.shared.observability.logging",
        "vigia_platform.shared.observability.metrics",
        "vigia_platform.shared.observability.redaction",
        "vigia_platform.shared.observability.tracing",
    ]
    completed = subprocess.run(
        [
            sys.executable,
            str(BACKEND / "tools" / "check_isolated_imports.py"),
            "--modules",
            *modules,
        ],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        check=False,
    )
    result = json.loads(completed.stdout)
    assert result["failures"] == {} and result["leaked"] == [] and completed.returncode == 0
