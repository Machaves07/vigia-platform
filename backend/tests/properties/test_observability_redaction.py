"""PR-NUC-54: ningún secreto, token, PEM, correo, enlace ni texto libre sale en la telemetría.

Metapropiedad de LC-NUC-31 (NFR-NUC-17, NFR-NUC-41, PAT-NUC-SEG-09). Los generadores producen
valores sensibles (secretos largos y cortos, tokens, JWT de segmentos cortos, credenciales
``Bearer``, PEM, correos, enlaces y texto libre) y la propiedad los inyecta por **cada canal**
por el que un dato puede llegar a la telemetría. Después busca cada valor en todo lo emitido y,
para trazas y métricas, también en los bytes OTLP serializados que saldrían hacia el colector.

- **Registros**: campos permitidos y desconocidos; contexto de ``log_context``; argumentos ``%`` y
  ``extra`` de terceros; mensaje de una excepción registrada; el **mensaje** de ``get_logger``
  construido en ejecución; el **componente** de ``get_logger`` y el nombre de un registrador
  ajeno; un registrador ajeno con un **f-string** sin argumentos; la **excepción no recuperada
  de una tarea de asyncio**; una plantilla ``%`` en una variable que **no encaja** con sus
  argumentos; y, solo para las formas reconocibles (token, PEM, correo, enlace),
  la plantilla de un registrador ajeno con argumentos.
- **Métricas**: atributos permitidos y desconocidos en las métricas de la plataforma y en un
  instrumento crudo como los de la instrumentación automática, medidas dentro de un tramo (el SDK
  guarda entonces ejemplares con los atributos filtrados).
- **Trazas**: atributos al crear el tramo y después, eventos, enlaces, excepciones registradas o
  escapadas, descripción del estado y los **nombres**: ``start_as_current_span``,
  ``start_span``, ``update_name`` y ``add_event`` con el valor generado.

Las llamadas que inyectan un valor como mensaje o como nombre llevan ``# noqa: VIG004``: la regla
de lint las prohíbe en el código; aquí se hacen a propósito para probar la barrera en ejecución.

Cada valor generado lleva algo que una salida legítima no contiene nunca (``@``, ``://``,
``-----BEGIN``, ``eyJ`` seguido de puntos, un símbolo de ``SECRET_SYMBOLS``, una mayúscula entre
minúsculas seguida de dígitos, 20 o más caracteres de token seguidos, o varias palabras con una
mayúscula). Así la búsqueda por subcadena no confunde un valor generado con un nombre de campo,
una marca de tiempo o un mensaje: si aparece, es una fuga.

``test_without_the_filter_the_property_finds_leaks`` quita los filtros y exige que Hypothesis
encuentre una fuga en cada señal: la propiedad detecta lo que promete.
"""

from __future__ import annotations

import asyncio
import base64
import gc
import io
import json
import logging
import string
import textwrap
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import pytest
from hypothesis import HealthCheck, find, given, settings
from hypothesis import strategies as st
from opentelemetry.exporter.otlp.proto.common.metrics_encoder import encode_metrics
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricExportResult,
    MetricsData,
)
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Link, Status, StatusCode

from vigia_platform.shared.observability import logging as obs_logging
from vigia_platform.shared.observability import redaction, tracing
from vigia_platform.shared.observability.metrics import CATALOG, MetricKind, MetricSpec

# --- Generadores de valores sensibles -------------------------------------------------------

SECRET_SYMBOLS = "!#$%&*?^~"  # noqa: S105 — símbolos de los secretos falsos generados.
"""Ningún registro, métrica ni traza legítimos contienen estos símbolos."""

_SECRET_ALPHABET = string.ascii_letters + string.digits + string.punctuation
_URL_ALPHABET = string.ascii_letters + string.digits + "-._~%"
_WORD_ALPHABET = string.ascii_lowercase + "áéíóúñü"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


secrets_ = st.builds(
    lambda core, digit, symbol: f"{core}{digit}{symbol}",
    st.text(alphabet=_SECRET_ALPHABET, min_size=8, max_size=40),
    st.integers(min_value=0, max_value=9),
    st.sampled_from(SECRET_SYMBOLS),
)
"""Contraseñas, códigos de recuperación y secretos arbitrarios."""

short_secrets = st.builds(
    lambda head, upper, tail, digits: f"{head}{upper}{tail}{digits}",
    st.from_regex(r"[a-z]{2,6}", fullmatch=True),
    st.sampled_from(string.ascii_uppercase),
    st.from_regex(r"[a-z]{1,4}", fullmatch=True),
    st.from_regex(r"[0-9]{1,3}", fullmatch=True),
)
"""Contraseñas cortas sin forma reconocible, como ``hunter2`` con una mayúscula."""

_segment = st.from_regex(r"[A-Za-z0-9_-]{3,6}", fullmatch=True)
short_jwts = st.builds(
    lambda header, payload, signature: f"eyJ{header}.{payload}.{signature}",
    st.binary(min_size=1, max_size=6).map(_b64url),
    st.binary(min_size=1, max_size=6).map(_b64url),
    st.binary(max_size=6).map(_b64url),
)
"""JWT con segmentos cortos."""

dotted_bearers = st.builds(
    lambda scheme, parts: scheme + ".".join(parts),
    st.sampled_from(["", "Bearer ", "bearer "]),
    st.lists(_segment, min_size=3, max_size=4).filter(
        lambda parts: (
            any(c.isdigit() for p in parts for c in p)
            and any(c.isupper() for p in parts for c in p)
        )
    ),
)
"""Tokens de portador con puntos (``mF_9.B5f-4.1JqM``), con o sin ``Bearer``."""

tokens = st.one_of(
    st.binary(min_size=16, max_size=48).map(_b64url),
    st.binary(min_size=16, max_size=48).map(lambda data: base64.b64encode(data).decode("ascii")),
    st.binary(min_size=16, max_size=32).map(bytes.hex),
    st.tuples(
        st.from_regex(r"[a-z]{2,5}_", fullmatch=True), st.binary(min_size=16, max_size=32)
    ).map(lambda parts: parts[0] + _b64url(parts[1])),
    short_jwts,
    dotted_bearers,
)
"""Identificadores de sesión, tokens de invitación o de vista en vivo, claves de API, JWT."""

pem_blocks = st.builds(
    lambda label, body: (
        f"-----BEGIN {label}-----\n"
        + "\n".join(textwrap.wrap(base64.b64encode(body).decode("ascii"), 64))
        + f"\n-----END {label}-----"
    ),
    st.from_regex(r"[A-Z]{3,10}( [A-Z]{3,10}){0,2}", fullmatch=True),
    st.binary(min_size=32, max_size=192),
)
"""Claves y certificados en PEM."""

emails = st.emails()

links = st.one_of(
    st.builds(
        lambda scheme, host, path, query: f"{scheme}://{host}/{path}?t={query}",
        st.sampled_from(["http", "https"]),
        st.from_regex(r"[a-z0-9]{1,12}(\.[a-z0-9]{1,12}){1,3}", fullmatch=True),
        st.text(alphabet=_URL_ALPHABET, max_size=30),
        st.text(alphabet=_URL_ALPHABET, min_size=1, max_size=40),
    ),
    st.builds(
        lambda host, path: f"www.{host}/{path}",
        st.from_regex(r"[a-z0-9]{1,12}\.[a-z]{2,6}", fullmatch=True),
        st.text(alphabet=_URL_ALPHABET, max_size=30),
    ),
)
"""Enlaces de invitación, de restablecimiento o de evidencias."""

free_texts = st.builds(
    lambda words: " ".join([*words[:1], words[1].capitalize(), *words[2:]]),
    st.lists(st.text(alphabet=_WORD_ALPHABET, min_size=4, max_size=12), min_size=3, max_size=10),
)
"""Motivos, descripciones y nombres de personas: varias palabras, una con mayúscula."""

PATTERN_KINDS = ("token", "pem", "email", "link")
"""Formas que ``redact_text`` reconoce dentro de un texto."""

KINDS: Mapping[str, st.SearchStrategy[str]] = {
    "secret": secrets_,
    "short_secret": short_secrets,
    "token": tokens,
    "pem": pem_blocks,
    "email": emails,
    "link": links,
    "free_text": free_texts,
}


@dataclass(frozen=True)
class Injection:
    """Un valor sensible y el canal por el que se intenta sacar."""

    kind: str
    value: str
    channel: str
    key: str
    separator: str


def _tagged(kind: str) -> st.SearchStrategy[tuple[str, str]]:
    def tag(value: str) -> tuple[str, str]:
        return kind, value

    return KINDS[kind].map(tag)


def _sensitive(kinds: Sequence[str]) -> st.SearchStrategy[tuple[str, str]]:
    return st.one_of(*(_tagged(kind) for kind in kinds))


_SEPARATORS = st.sampled_from(["", " ", "=", ": ", "("])
_STANDARD_RECORD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys() | {"message", "asctime"}
)
_ALLOWED_KEYS = sorted(redaction.DEFAULT_POLICY.keys)
_unknown_keys = st.from_regex(r"[a-z][a-z0-9_]{2,20}", fullmatch=True).filter(
    lambda key: (
        key not in redaction.DEFAULT_POLICY.keys
        and key not in _STANDARD_RECORD_ATTRS
        and key != "duration_ms"
    )
)


@st.composite
def _injections(
    draw: st.DrawFn, channels: Sequence[str], pattern_channels: Sequence[str]
) -> list[Injection]:
    found: list[Injection] = []
    for _ in range(draw(st.integers(min_value=1, max_value=6))):
        channel = draw(st.sampled_from([*channels, *pattern_channels]))
        kinds = PATTERN_KINDS if channel in pattern_channels else tuple(KINDS)
        kind, value = draw(_sensitive(kinds))
        known = draw(st.booleans())
        key = draw(st.sampled_from(_ALLOWED_KEYS) if known else _unknown_keys)
        found.append(Injection(kind, value, channel, key, draw(_SEPARATORS)))
    return found


LOG_CHANNELS = (
    "field",
    "context",
    "argument",
    "stdlib_extra",
    "exception",
    "stdlib_exception",
    "message",
    "component",
    "stdlib_logger_name",
    "stdlib_fstring",
    "stdlib_mismatch",
    "asyncio_task",
)
log_cases = _injections(LOG_CHANNELS, ("stdlib_template",))

METRIC_CHANNELS = ("platform", "raw_instrument")
metric_cases = _injections(METRIC_CHANNELS, ())

TRACE_CHANNELS = (
    "attribute",
    "start_attribute",
    "event_attribute",
    "link_attribute",
    "record_exception",
    "escaped_exception",
    "status",
    "span_name",
    "start_span_name",
    "update_name",
    "event_name",
)
trace_cases = _injections(TRACE_CHANNELS, ())


# --- Búsqueda en la salida -----------------------------------------------------------------


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)


def _leaks(
    injections: Sequence[Injection], emitted: Sequence[str], wire: Sequence[bytes] = ()
) -> list[str]:
    """Inyecciones cuyo valor aparece en lo emitido o en los bytes OTLP serializados."""
    text = "\n".join(emitted)
    blob = b"\n".join(wire)
    found: list[str] = []
    for i in injections:
        if i.value in text:
            found.append(f"{i.kind} por {i.channel}")
        elif i.value.encode("utf-8") in blob:
            found.append(f"{i.kind} por {i.channel} (OTLP)")
    return found


# --- Registros -----------------------------------------------------------------------------


async def _fail(value: str) -> None:
    raise ValueError(value)


def _unretrieved_task_exception(value: str) -> None:
    """Deja que asyncio registre «Task exception was never retrieved» con ``value`` dentro."""
    loop = asyncio.new_event_loop()
    try:
        task = loop.create_task(_fail(value))
        loop.run_until_complete(asyncio.wait([task]))
        del task
        gc.collect()
    finally:
        loop.close()


def emit_logs(injections: Sequence[Injection]) -> list[str]:
    """Inyecta por los canales de registro y devuelve las fugas."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(obs_logging.JsonFormatter())
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    log = obs_logging.get_logger("pruebas.redaccion")
    third_party = logging.getLogger("tercero.biblioteca")
    # Plantillas que no encajan: sin propagar a la raíz, donde el manejador de captura de pytest
    # relanzaría el error de formato; solo las ve el formateador de la plataforma.
    mismatched = logging.getLogger("tercero.plantilla")
    mismatched.propagate = False
    mismatched.addHandler(handler)
    try:
        for i in injections:
            built = "texto previo" + i.separator + i.value + " texto posterior"
            if i.channel == "field":
                log.info("evento de prueba", **{i.key: i.value})
            elif i.channel == "context":
                with obs_logging.log_context(**{i.key: i.value}):
                    log.info("evento con contexto")
            elif i.channel == "argument":
                third_party.warning("valor recibido %s", i.value)
            elif i.channel == "stdlib_extra":
                third_party.info("evento de biblioteca", extra={i.key: i.value})
            elif i.channel == "exception":
                try:
                    raise ValueError(i.value)
                except ValueError:
                    log.exception("fallo de prueba")
            elif i.channel == "stdlib_exception":
                try:
                    raise RuntimeError(i.value)
                except RuntimeError:
                    third_party.exception("fallo de biblioteca")
            elif i.channel == "message":
                log.warning(built)  # noqa: VIG004 — inyección hostil a propósito.
            elif i.channel == "component":
                obs_logging.get_logger(i.value).info(  # noqa: VIG004 — inyección hostil.
                    "evento de componente"
                )
            elif i.channel == "stdlib_logger_name":
                logging.getLogger("tercero." + i.value).warning("valor %s", 1)
            elif i.channel == "stdlib_fstring":
                third_party.warning(built)
            elif i.channel == "stdlib_mismatch":
                # Plantilla en una variable que no encaja con su argumento (sobra uno).
                mismatched.warning(built.replace("%", "%%"), 7)
            elif i.channel == "asyncio_task":
                _unretrieved_task_exception(i.value)
            else:
                # La plantilla de una biblioteca es válida: ``%`` del dato va escapado y, al
                # formatearla, el texto vuelve a contener el valor tal cual.
                template = built.replace("%", "%%") + " %s"
                third_party.warning(template, 1)
    finally:
        root.removeHandler(handler)
        mismatched.removeHandler(handler)
        mismatched.propagate = True  # Si quedara en False, pytest le engancharía su captura.
        root.setLevel(previous_level)
    emitted = [
        text for line in stream.getvalue().splitlines() for text in _strings(json.loads(line))
    ]
    return _leaks(injections, emitted)


@given(log_cases)
def test_logs_never_contain_generated_values(injections: list[Injection]) -> None:
    assert emit_logs(injections) == []


# --- Métricas y trazas ---------------------------------------------------------------------


class CollectingMetricExporter(MetricExporter):
    """Exportador de prueba que guarda lo que el SDK exportaría al colector."""

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


SPAN_ORIGIN: Final = "origen"
SPAN_OPERATION: Final = "operacion"
SPAN_LINKED: Final = "enlazado"
SPAN_MEASURE: Final = "medicion"
SPAN_FAILED: Final = "operacion fallida"
EVENT: Final = "evento"

POLICY = redaction.AttributePolicy()
"""Política propia de la propiedad, con sus nombres de tramo y de evento registrados."""
POLICY.register_span_names([SPAN_ORIGIN, SPAN_OPERATION, SPAN_LINKED, SPAN_MEASURE, SPAN_FAILED])
POLICY.register_event_names([EVENT])

_SETTINGS = tracing.TelemetrySettings(
    schedule_delay_seconds=3600, metric_export_interval_seconds=3600
)
_SPECS_WITH_ATTRIBUTES = [spec for spec in CATALOG if spec.attributes]


def _telemetry(spans: InMemorySpanExporter, metrics: CollectingMetricExporter) -> tracing.Telemetry:
    return tracing.configure_telemetry(
        _SETTINGS, span_exporter=spans, metric_exporter=metrics, policy=POLICY
    )


def _record(spec: MetricSpec, telemetry: tracing.Telemetry, attributes: dict[str, str]) -> None:
    instrument: Any = telemetry.metrics.instrument(spec.name)
    if spec.kind is MetricKind.COUNTER:
        instrument.add(1, attributes)
    elif spec.kind is MetricKind.HISTOGRAM:
        instrument.record(5, attributes)
    else:
        instrument.set(3, attributes)


def emit_metrics(injections: Sequence[Injection]) -> list[str]:
    """Inyecta por los atributos de métricas y devuelve las fugas de lo exportado."""
    exporter = CollectingMetricExporter()
    telemetry = _telemetry(InMemorySpanExporter(), exporter)
    tracer = telemetry.tracer()
    raw = telemetry.meter_provider.get_meter("instrumentacion.automatica").create_counter(
        "instrumentacion_automatica_prueba"
    )
    try:
        for index, i in enumerate(injections):
            with tracer.start_as_current_span(SPAN_MEASURE):
                if i.channel == "platform":
                    spec = _SPECS_WITH_ATTRIBUTES[index % len(_SPECS_WITH_ATTRIBUTES)]
                    key = sorted(spec.attributes)[0] if i.key in spec.attributes else i.key
                    _record(spec, telemetry, {key: i.value})
                    _record(spec, telemetry, {sorted(spec.attributes)[0]: i.value})
                else:
                    raw.add(1, {i.key: i.value, "http.route": i.value})
        telemetry.meter_provider.force_flush()
    finally:
        telemetry.shutdown()
    emitted: list[str] = []
    for batch in exporter.batches:
        for resource in batch.resource_metrics:
            emitted.extend(_strings(dict(resource.resource.attributes)))
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    emitted.append(metric.name)
                    for point in metric.data.data_points:
                        emitted.extend(_strings(dict(point.attributes or {})))
                        for exemplar in point.exemplars or ():
                            emitted.extend(_strings(dict(exemplar.filtered_attributes or {})))
    wire = [encode_metrics(batch).SerializeToString() for batch in exporter.batches]
    return _leaks(injections, emitted, wire)


@given(metric_cases)
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_metric_attributes_never_contain_generated_values(injections: list[Injection]) -> None:
    assert emit_metrics(injections) == []


def _span_strings(span: ReadableSpan) -> Iterator[str]:
    yield span.name
    yield from _strings(dict(span.attributes or {}))
    for event in span.events:
        yield event.name
        yield from _strings(dict(event.attributes or {}))
    for link in span.links:
        yield from _strings(dict(link.attributes or {}))
    if span.status.description:
        yield span.status.description
    yield from _strings(dict(span.resource.attributes))


def emit_traces(injections: Sequence[Injection]) -> list[str]:
    """Inyecta por los canales de trazas y devuelve las fugas de lo exportado."""
    exporter = InMemorySpanExporter()
    telemetry = _telemetry(exporter, CollectingMetricExporter())
    tracer = telemetry.tracer()
    try:
        with tracer.start_as_current_span(SPAN_ORIGIN) as origin:
            origin_context = origin.get_span_context()
        for i in injections:
            built = SPAN_OPERATION + i.separator + i.value
            if i.channel == "span_name":
                with tracer.start_as_current_span(built):  # noqa: VIG004 — inyección hostil.
                    pass
            elif i.channel == "start_span_name":
                tracer.start_span(i.value).end()  # noqa: VIG004 — inyección hostil.
            elif i.channel == "start_attribute":
                with tracer.start_as_current_span(SPAN_OPERATION, attributes={i.key: i.value}):
                    pass
            elif i.channel == "link_attribute":
                tracer.start_span(SPAN_LINKED, links=[Link(origin_context, {i.key: i.value})]).end()
            elif i.channel == "escaped_exception":
                with (
                    pytest.raises(RuntimeError),
                    tracer.start_as_current_span(SPAN_FAILED),
                ):
                    raise RuntimeError(i.value)
            else:
                with tracer.start_as_current_span(SPAN_OPERATION) as span:
                    if i.channel == "attribute":
                        span.set_attribute(i.key, i.value)
                    elif i.channel == "event_attribute":
                        span.add_event(EVENT, {i.key: i.value})
                    elif i.channel == "event_name":
                        span.add_event(built)  # noqa: VIG004 — inyección hostil.
                    elif i.channel == "update_name":
                        span.update_name(i.value)  # noqa: VIG004 — inyección hostil.
                    elif i.channel == "record_exception":
                        span.record_exception(ValueError(i.value))
                    else:
                        span.set_status(Status(StatusCode.ERROR, i.value))
        telemetry.span_processor.force_flush()
    finally:
        telemetry.shutdown()
    finished = exporter.get_finished_spans()
    emitted = [text for span in finished for text in _span_strings(span)]
    wire = [encode_spans(finished).SerializeToString()] if finished else []
    return _leaks(injections, emitted, wire)


@given(trace_cases)
def test_trace_attributes_never_contain_generated_values(injections: list[Injection]) -> None:
    assert emit_traces(injections) == []


# --- La propiedad detecta la fuga cuando falta el filtro -----------------------------------


def _identity_span(span: ReadableSpan, policy: redaction.AttributePolicy) -> ReadableSpan:
    return span


def _without_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Quita la redacción: textos, nombres, atributos y argumentos salen tal cual."""

    def clean(
        self: redaction.AttributePolicy,
        attributes: Mapping[str, object] | None,
        **kwargs: object,
    ) -> dict[str, object]:
        return {str(key): str(value) for key, value in (attributes or {}).items()}

    monkeypatch.setattr(redaction, "redact_text", lambda text: text)
    monkeypatch.setattr(redaction.AttributePolicy, "clean", clean)
    monkeypatch.setattr(
        redaction.AttributePolicy, "clean_value", lambda self, key, value, replacement: value
    )
    monkeypatch.setattr(obs_logging, "_safe_argument", lambda value: value)
    monkeypatch.setattr(obs_logging, "_is_code_constant", lambda text, frame: True)
    monkeypatch.setattr(obs_logging, "_message", lambda record: record.getMessage())
    monkeypatch.setattr(tracing, "sanitize_span", _identity_span)


@pytest.mark.parametrize(
    ("cases", "emit"),
    [(log_cases, emit_logs), (metric_cases, emit_metrics), (trace_cases, emit_traces)],
    ids=["logs", "metrics", "traces"],
)
def test_without_the_filter_the_property_finds_leaks(
    monkeypatch: pytest.MonkeyPatch, cases: Any, emit: Any
) -> None:
    _without_filters(monkeypatch)
    example = find(
        cases,
        lambda injections: bool(emit(injections)),
        settings=settings(database=None, max_examples=200),
    )
    assert emit(example) != []
