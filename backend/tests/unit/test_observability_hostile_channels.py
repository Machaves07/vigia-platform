"""Canales hostiles de la revisión de VIG-22 (ronda 1): regresión con las sondas del revisor.

Mensajes de terceros sin argumentos, la excepción no recuperada de una tarea de asyncio, mensajes
y nombres de tramo o evento construidos en ejecución, JWT de segmentos cortos y credenciales
``Bearer`` con puntos. Ninguna sonda llega al registro ni a los bytes OTLP serializados.
"""

from __future__ import annotations

import asyncio
import gc
import io
import json
import logging
from collections.abc import Iterator
from typing import Final

import pytest
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from vigia_platform.shared.observability import redaction, tracing
from vigia_platform.shared.observability.logging import JsonFormatter, get_logger

PROBES = (
    "Juan Pérez no llevaba casco en la zona 3",
    "hunter2",
    "eyJhbGc.eyJz.abc",
    "Bearer mF_9.B5f-4.1JqM",
    "mF_9.B5f-4.1JqM",
)
"""Las sondas de la revisión: texto libre, contraseña corta, JWT corto y portador con puntos."""

CONSTANT_MESSAGE: Final = "mensaje constante del módulo"


@pytest.fixture
def captured() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield stream
    finally:
        root.removeHandler(handler)
        root.setLevel(level)


def _lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


async def _fail(value: str) -> None:
    raise ValueError(value)


def _unretrieved_task_exception(value: str) -> None:
    loop = asyncio.new_event_loop()
    try:
        task = loop.create_task(_fail(value))
        loop.run_until_complete(asyncio.wait([task]))
        del task
        gc.collect()
    finally:
        loop.close()


@pytest.mark.parametrize("probe", PROBES)
def test_probe_never_reaches_the_log(captured: io.StringIO, probe: str) -> None:
    get_logger("sondas").info(probe)  # noqa: VIG004 — sonda hostil de la revisión.
    get_logger("sondas").warning("previo " + probe)  # noqa: VIG004 — sonda hostil.
    logging.getLogger("tercero").warning(f"usuario {probe}")  # noqa: G004 — sonda de terceros.
    get_logger(probe).info("evento")  # noqa: VIG004 — sonda hostil de la revisión.
    _unretrieved_task_exception(probe)
    text = captured.getvalue()
    assert probe not in text
    produced = [line for line in _lines(captured) if line["level"] != "DEBUG"]
    assert len(produced) == 5  # Las cinco sondas llegaron al formateador, redactadas.


@pytest.mark.parametrize("probe", PROBES)
def test_probe_never_reaches_serialized_otlp(probe: str) -> None:
    exporter = InMemorySpanExporter()
    telemetry = tracing.configure_telemetry(
        tracing.TelemetrySettings(schedule_delay_seconds=3600, metric_export_interval_seconds=3600),
        span_exporter=exporter,
    )
    tracer = telemetry.tracer()
    with tracer.start_as_current_span(probe) as span:  # noqa: VIG004 — sonda hostil.
        span.add_event(probe)  # noqa: VIG004 — sonda hostil.
    with tracer.start_as_current_span("operacion") as span:
        span.update_name(probe)  # noqa: VIG004 — sonda hostil.
    tracer.start_span(probe).end()  # noqa: VIG004 — sonda hostil.
    telemetry.span_processor.force_flush()
    telemetry.shutdown()
    finished = exporter.get_finished_spans()
    assert len(finished) == 3
    assert probe.encode("utf-8") not in encode_spans(finished).SerializeToString()
    assert {span.name for span in finished} == {redaction.OTHER}


def test_asyncio_unretrieved_exception_keeps_logger_and_level(captured: io.StringIO) -> None:
    _unretrieved_task_exception("Ana Gómez sin arnés")
    (line,) = [line for line in _lines(captured) if line["level"] == "ERROR"]
    assert line["component"] == "asyncio"
    assert line["level"] == "ERROR"
    assert line["message"] == redaction.REDACTED
    assert line["exception_type"] == "ValueError"


def test_third_party_message_with_arguments_keeps_its_template(captured: io.StringIO) -> None:
    logging.getLogger("asyncio").warning("conexiones abiertas: %s", 3)
    logging.getLogger("tercero.biblioteca").warning("sin argumentos")
    first, second = _lines(captured)
    assert first["message"] == "conexiones abiertas: 3"
    assert second["message"] == redaction.REDACTED
    assert second["component"] == redaction.OTHER  # «tercero» no es un módulo cargado.


@pytest.mark.parametrize(
    ("name", "component"),
    [("asyncio", "asyncio"), ("asyncio.hunter2", "asyncio"), ("json.decoder", "json.decoder")],
)
def test_third_party_component_is_the_loaded_module_prefix(
    captured: io.StringIO, name: str, component: str
) -> None:
    logging.getLogger(name).warning("valor %s", 1)
    assert _lines(captured)[0]["component"] == component


def test_get_logger_keeps_literals_and_final_constants_only(captured: io.StringIO) -> None:
    log = get_logger("pruebas.constantes")
    built = " ".join(["mensaje", "construido"])  # En ejecución: no es una constante.
    log.info("literal de la función")
    log.info(CONSTANT_MESSAGE)
    log.info(built)  # noqa: VIG004 — texto construido en ejecución a propósito.
    messages = [line["message"] for line in _lines(captured)]
    assert messages == ["literal de la función", CONSTANT_MESSAGE, redaction.REDACTED]
    assert {line["component"] for line in _lines(captured)} == {"pruebas.constantes"}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("GET /v1/x/{id}", "GET /v1/x/{id}"),
        ("GET /v1/x/{id} http send", "GET /v1/x/{id} http send"),
        ("GET /v1/x/hunter2", "GET"),
        ("HTTP POST", "POST"),
        ("SELECT vigia", "SELECT"),
        ("connect", "connect"),
        ("S3.HeadObject", "S3.HeadObject"),
        ("S3.Evil", redaction.OTHER),
        ("ledger.write", "ledger.write"),
        ("hunter2", redaction.OTHER),
        ("Juan Pérez", redaction.OTHER),
    ],
)
def test_span_names_come_from_closed_lists(name: str, expected: str) -> None:
    policy = redaction.AttributePolicy()
    policy.register("route", ["/v1/x/{id}"])
    policy.register_span_names(["ledger.write"])
    assert policy.clean_span_name(name) == expected


def test_span_and_event_name_registration_rejects_data() -> None:
    policy = redaction.AttributePolicy()
    for bad in ["hunter2 eyJhbGc.eyJz.abc", "a@b.c", "https://x", ""]:
        with pytest.raises(ValueError):
            policy.register_span_names([bad])
    assert policy.clean_event_name("reintento") == redaction.OTHER
    policy.register_event_names(["reintento"])
    assert policy.clean_event_name("reintento") == "reintento"


@pytest.mark.parametrize(
    "text",
    [
        "token eyJhbGc.eyJz.abc",
        "Authorization: Bearer mF_9.B5f-4.1JqM",
        "clave mF_9.B5f-4.1JqM usada",
        "Basic dXNlcjpwYXNz",
    ],
)
def test_redact_text_catches_short_jwts_and_dotted_bearers(text: str) -> None:
    redacted = redaction.redact_text(text)
    assert redaction.REDACTED in redacted
    assert "mF_9" not in redacted and "eyJ" not in redacted and "dXNlcjpwYXNz" not in redacted


def test_export_failures_log_one_line_per_outage(captured: io.StringIO) -> None:
    health = tracing._ExportHealth("traces")
    for succeeded in [True, False, False, False, True, True, False]:
        health(succeeded)
    lines = _lines(captured)
    assert [(line["level"], line["signal"]) for line in lines] == [
        ("WARNING", "traces"),
        ("INFO", "traces"),
        ("WARNING", "traces"),
    ]
    assert all(line["component"] == "shared.observability" for line in lines)
