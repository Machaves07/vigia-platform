"""OpenTelemetry del proceso: trazas y métricas con exportación OTLP acotada (NFR-NUC-41, 43).

- ``configure_telemetry`` crea el ``TracerProvider`` y el ``MeterProvider`` con exportación OTLP
  gRPC hacia el colector lateral (``localhost:4317``, ``infrastructure-design.md`` §9.1) y
  muestreo del 100 % en el piloto.
- **Cola acotada** (PAT-NUC-RES-03): los tramos entran en una cola de tamaño fijo sin bloquear
  nunca al hilo que los termina y un hilo propio los exporta por lotes. Si la cola está llena, o
  si una exportación falla porque el colector no responde, los tramos se descartan y
  ``otel_dropped_total{signal="traces"}`` los cuenta. Una exportación de métricas fallida suma
  sus puntos a ``otel_dropped_total{signal="metrics"}``. La aplicación nunca espera a la
  telemetría.
- **Lista blanca de atributos** (NFR-NUC-41): cada tramo pasa por ``SanitizingSpanExporter``
  antes de salir. Solo quedan identificadores y enumeraciones; los nombres pasan por
  ``redact_text``, la descripción del estado se retira y un evento ``exception`` conserva solo el
  tipo (nunca el mensaje ni la traza). Las métricas se limpian al registrar
  (``metrics.PlatformMetrics``); una vista global quita los nombres de atributo no permitidos y
  ``SanitizingMetricExporter`` limpia los valores al exportar, retira los ejemplares (llevan los
  atributos filtrados) y fusiona las series que queden iguales. Así también quedan cubiertas las
  métricas de la instrumentación automática.
- ``enable_auto_instrumentation`` activa FastAPI, SQLAlchemy, httpx y botocore desde la fábrica
  de la aplicación; importa esas bibliotecas solo al llamarla.
"""

from __future__ import annotations

import contextlib
import dataclasses
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from opentelemetry import metrics as otel_metrics
from opentelemetry import trace as otel_trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import AlwaysOffExemplarFilter, MeterProvider
from opentelemetry.sdk.metrics.export import (
    HistogramDataPoint,
    MetricExporter,
    MetricExportResult,
    MetricReader,
    MetricsData,
    NumberDataPoint,
    PeriodicExportingMetricReader,
    Sum,
)
from opentelemetry.sdk.metrics.view import View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import Link, Status

from vigia_platform.shared.observability import redaction
from vigia_platform.shared.observability.metrics import METER_NAME, PlatformMetrics

__all__ = [
    "AutoInstrumentation",
    "BoundedSpanProcessor",
    "CountingMetricExporter",
    "SanitizingMetricExporter",
    "SanitizingSpanExporter",
    "Telemetry",
    "TelemetrySettings",
    "configure_telemetry",
    "enable_auto_instrumentation",
    "metric_views",
    "sanitize_metrics",
    "sanitize_span",
]

TRACER_NAME: Final = "vigia_platform"
MAX_SPAN_NAME_CHARS: Final = 128
_EXCEPTION_EVENT: Final = "exception"
_INTERRUPTED_EXPORT_GRACE_SECONDS: Final = 2.0


@dataclass(frozen=True, slots=True)
class TelemetrySettings:
    """Configuración del exportador; la fábrica de la aplicación la lee de su configuración."""

    service_name: str = "vigia-api"
    environment: str = "local"
    service_version: str = "0.0.0"
    otlp_endpoint: str = "http://localhost:4317"
    max_queue_size: int = 2048
    max_export_batch_size: int = 512
    schedule_delay_seconds: float = 5.0
    export_timeout_seconds: float = 10.0
    metric_export_interval_seconds: float = 60.0


def _span_name(name: str) -> str:
    cleaned = redaction.redact_text(name)[:MAX_SPAN_NAME_CHARS]
    return cleaned or "span"


def sanitize_span(span: ReadableSpan, policy: redaction.AttributePolicy) -> ReadableSpan:
    """Copia de ``span`` con solo identificadores y enumeraciones (NFR-NUC-41, 43)."""
    events: list[Event] = []
    for event in span.events:
        if event.name == _EXCEPTION_EVENT:
            original: Mapping[str, object] = event.attributes or {}
            kept: dict[str, redaction.AttributeValue] = {}
            exception_type = original.get("exception.type")
            if isinstance(exception_type, str) and redaction.known_exception_type(exception_type):
                kept["exception.type"] = exception_type
            escaped = original.get("exception.escaped")
            if isinstance(escaped, bool):
                kept["exception.escaped"] = escaped
            events.append(Event(_EXCEPTION_EVENT, kept, event.timestamp))
        else:
            events.append(
                Event(_span_name(event.name), policy.clean(event.attributes), event.timestamp)
            )
    links = [Link(link.context, policy.clean(link.attributes)) for link in span.links]
    return ReadableSpan(
        name=_span_name(span.name),
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=policy.clean(span.attributes),
        events=events,
        links=links,
        kind=span.kind,
        status=Status(span.status.status_code),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class SanitizingSpanExporter(SpanExporter):
    """Exportador que limpia cada tramo antes de entregarlo al exportador real."""

    def __init__(
        self, inner: SpanExporter, policy: redaction.AttributePolicy | None = None
    ) -> None:
        self._inner = inner
        self._policy = policy

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        policy = self._policy if self._policy is not None else redaction.DEFAULT_POLICY
        return self._inner.export([sanitize_span(span, policy) for span in spans])

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self._inner.force_flush(timeout_millis)


class BoundedSpanProcessor(SpanProcessor):
    """Cola de tramos de tamaño fijo con un hilo exportador; nunca bloquea a quien la usa.

    ``on_drop(n)`` recibe cada descarte: cola llena, exportación fallida o cierre.
    """

    def __init__(
        self,
        exporter: SpanExporter,
        *,
        on_drop: Callable[[int], None],
        max_queue_size: int = 2048,
        max_export_batch_size: int = 512,
        schedule_delay_seconds: float = 5.0,
        shutdown_timeout_seconds: float = 10.0,
    ) -> None:
        if max_queue_size < 1 or max_export_batch_size < 1:
            raise ValueError("la cola y el lote deben admitir al menos un tramo")
        self._exporter = exporter
        self._on_drop = on_drop
        self._max_queue_size = max_queue_size
        self._batch_size = min(max_export_batch_size, max_queue_size)
        self._delay = schedule_delay_seconds
        self._shutdown_timeout = shutdown_timeout_seconds
        self._queue: deque[ReadableSpan] = deque()
        self._lock = threading.Lock()
        self._export_lock = threading.Lock()
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._worker = threading.Thread(target=self._run, name="vigia-otel-spans", daemon=True)
        self._worker.start()

    def _drop(self, count: int) -> None:
        if count > 0:
            with contextlib.suppress(Exception):  # Contar un descarte nunca rompe la aplicación.
                self._on_drop(count)

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        return None

    def on_end(self, span: ReadableSpan) -> None:
        if self._stopped.is_set():
            self._drop(1)
            return
        with self._lock:
            full = len(self._queue) >= self._max_queue_size
            if not full:
                self._queue.append(span)
            ready = len(self._queue) >= self._batch_size
        if full:
            self._drop(1)
        elif ready:
            self._wake.set()

    def _next_batch(self) -> list[ReadableSpan]:
        with self._lock:
            count = min(self._batch_size, len(self._queue))
            return [self._queue.popleft() for _ in range(count)]

    def _export(self, batch: list[ReadableSpan]) -> bool:
        with self._export_lock:
            try:
                result = self._exporter.export(batch)
            except Exception:
                result = SpanExportResult.FAILURE
        if result is not SpanExportResult.SUCCESS:
            self._drop(len(batch))
            return False
        return True

    def _drain(self) -> bool:
        succeeded = True
        while batch := self._next_batch():
            succeeded = self._export(batch) and succeeded
        return succeeded

    def _run(self) -> None:
        while not self._stopped.is_set():
            self._wake.wait(self._delay)
            self._wake.clear()
            self._drain()
        # Cierre: un intento; si falla, lo que queda se cuenta sin esperar a más exportaciones.
        batch = self._next_batch()
        if batch and self._export(batch):
            self._drain()
        else:
            self._discard_pending()

    def _discard_pending(self) -> None:
        with self._lock:
            pending = len(self._queue)
            self._queue.clear()
        self._drop(pending)

    def pending(self) -> int:
        """Tramos en cola."""
        with self._lock:
            return len(self._queue)

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self._drain()

    def shutdown(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        self._wake.set()
        self._worker.join(self._shutdown_timeout)
        if self._worker.is_alive():
            # Atascado en un colector que no responde: cerrar el exportador corta sus reintentos
            # y el hilo cuenta el lote en vuelo como descartado antes de terminar.
            self._exporter.shutdown()
            self._worker.join(_INTERRUPTED_EXPORT_GRACE_SECONDS)
            self._discard_pending()
            return
        self._discard_pending()
        self._exporter.shutdown()


class _DropCounter:
    """Destino de los descartes; se enlaza a ``otel_dropped_total`` al crear las métricas."""

    def __init__(self) -> None:
        self._metrics: PlatformMetrics | None = None

    def bind(self, metrics: PlatformMetrics) -> None:
        self._metrics = metrics

    def __call__(self, count: int, signal: str) -> None:
        if self._metrics is not None and count > 0:
            self._metrics.otel_dropped_total.add(count, {"signal": signal})


class CountingMetricExporter(MetricExporter):
    """Exportador de métricas que cuenta en ``otel_dropped_total`` los puntos no entregados."""

    def __init__(self, inner: MetricExporter, on_drop: Callable[[int], None]) -> None:
        super().__init__(
            preferred_temporality=getattr(inner, "_preferred_temporality", None),
            preferred_aggregation=getattr(inner, "_preferred_aggregation", None),
        )
        self._inner = inner
        self._on_drop = on_drop

    @staticmethod
    def _points(metrics_data: MetricsData) -> int:
        return sum(
            len(metric.data.data_points)
            for resource in metrics_data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
        )

    def export(
        self, metrics_data: MetricsData, timeout_millis: float = 10_000, **kwargs: Any
    ) -> MetricExportResult:
        try:
            result = self._inner.export(metrics_data, timeout_millis=timeout_millis, **kwargs)
        except Exception:
            result = MetricExportResult.FAILURE
        if result is not MetricExportResult.SUCCESS:
            with contextlib.suppress(Exception):  # Contar un descarte nunca rompe la aplicación.
                self._on_drop(self._points(metrics_data))
        return result

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return self._inner.force_flush(timeout_millis)

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: Any) -> None:
        self._inner.shutdown(timeout_millis=timeout_millis, **kwargs)


def _merge_points(first: Any, second: Any, data: Any) -> Any:
    """Une dos puntos que quedaron con los mismos atributos tras limpiarlos."""
    start = min(first.start_time_unix_nano, second.start_time_unix_nano)
    end = max(first.time_unix_nano, second.time_unix_nano)
    if (
        isinstance(first, HistogramDataPoint)
        and isinstance(second, HistogramDataPoint)
        and first.explicit_bounds == second.explicit_bounds
    ):
        return dataclasses.replace(
            first,
            start_time_unix_nano=start,
            time_unix_nano=end,
            count=first.count + second.count,
            sum=first.sum + second.sum,
            bucket_counts=[
                a + b for a, b in zip(first.bucket_counts, second.bucket_counts, strict=True)
            ],
            min=min(first.min, second.min),
            max=max(first.max, second.max),
        )
    if isinstance(data, Sum) and isinstance(first, NumberDataPoint):
        return dataclasses.replace(
            first, start_time_unix_nano=start, time_unix_nano=end, value=first.value + second.value
        )
    return second if second.time_unix_nano >= first.time_unix_nano else first


def _sanitize_data(data: Any, policy: redaction.AttributePolicy) -> Any:
    points: dict[frozenset[tuple[str, redaction.AttributeValue]], Any] = {}
    for point in data.data_points:
        attributes = policy.clean(point.attributes)
        cleaned = dataclasses.replace(point, attributes=attributes, exemplars=[])
        key = frozenset(attributes.items())
        points[key] = _merge_points(points[key], cleaned, data) if key in points else cleaned
    return dataclasses.replace(data, data_points=list(points.values()))


def sanitize_metrics(data: MetricsData, policy: redaction.AttributePolicy) -> MetricsData:
    """Copia de ``data`` con atributos permitidos y sin ejemplares (NFR-NUC-41)."""
    return MetricsData(
        resource_metrics=[
            dataclasses.replace(
                resource,
                scope_metrics=[
                    dataclasses.replace(
                        scope,
                        metrics=[
                            dataclasses.replace(metric, data=_sanitize_data(metric.data, policy))
                            for metric in scope.metrics
                        ],
                    )
                    for scope in resource.scope_metrics
                ],
            )
            for resource in data.resource_metrics
        ]
    )


class SanitizingMetricExporter(MetricExporter):
    """Exportador que limpia atributos y retira ejemplares antes del exportador real."""

    def __init__(
        self, inner: MetricExporter, policy: redaction.AttributePolicy | None = None
    ) -> None:
        super().__init__(
            preferred_temporality=getattr(inner, "_preferred_temporality", None),
            preferred_aggregation=getattr(inner, "_preferred_aggregation", None),
        )
        self._inner = inner
        self._policy = policy

    def export(
        self, metrics_data: MetricsData, timeout_millis: float = 10_000, **kwargs: Any
    ) -> MetricExportResult:
        policy = self._policy if self._policy is not None else redaction.DEFAULT_POLICY
        return self._inner.export(
            sanitize_metrics(metrics_data, policy), timeout_millis=timeout_millis, **kwargs
        )

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return self._inner.force_flush(timeout_millis)

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: Any) -> None:
        self._inner.shutdown(timeout_millis=timeout_millis, **kwargs)


def metric_views(policy: redaction.AttributePolicy | None = None) -> list[View]:
    """Vista global: solo los nombres de atributo de la lista blanca, en toda métrica."""
    chosen = policy if policy is not None else redaction.DEFAULT_POLICY
    return [View(instrument_name="*", attribute_keys=set(chosen.keys))]


@dataclass(slots=True)
class Telemetry:
    """Proveedores de trazas y métricas del proceso y sus métricas con nombre fijo."""

    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    metrics: PlatformMetrics
    span_processor: BoundedSpanProcessor
    _installed: bool = field(default=False, init=False)

    def tracer(self, name: str = TRACER_NAME) -> otel_trace.Tracer:
        return self.tracer_provider.get_tracer(name)

    def install_global(self) -> None:
        """Instala ambos proveedores como globales (una vez por proceso; lo hace la fábrica)."""
        if not self._installed:
            otel_trace.set_tracer_provider(self.tracer_provider)
            otel_metrics.set_meter_provider(self.meter_provider)
            self._installed = True

    def shutdown(self) -> None:
        self.tracer_provider.shutdown()
        self.meter_provider.shutdown()


def configure_telemetry(
    settings: TelemetrySettings | None = None,
    *,
    span_exporter: SpanExporter | None = None,
    metric_exporter: MetricExporter | None = None,
    extra_metric_readers: Sequence[MetricReader] = (),
    policy: redaction.AttributePolicy | None = None,
) -> Telemetry:
    """Crea los proveedores con exportación OTLP acotada (o con los exportadores dados)."""
    config = settings if settings is not None else TelemetrySettings()
    resource = Resource.create(
        {
            "service.name": config.service_name,
            "service.version": config.service_version,
            "deployment.environment": config.environment,
        }
    )
    drops = _DropCounter()
    metric_reader = PeriodicExportingMetricReader(
        SanitizingMetricExporter(
            CountingMetricExporter(
                metric_exporter
                if metric_exporter is not None
                else OTLPMetricExporter(
                    endpoint=config.otlp_endpoint, timeout=config.export_timeout_seconds
                ),
                on_drop=lambda count: drops(count, "metrics"),
            ),
            policy,
        ),
        export_interval_millis=config.metric_export_interval_seconds * 1000,
        export_timeout_millis=config.export_timeout_seconds * 1000,
    )
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[metric_reader, *extra_metric_readers],
        views=metric_views(policy),
        exemplar_filter=AlwaysOffExemplarFilter(),
    )
    metrics = PlatformMetrics(meter_provider.get_meter(METER_NAME), policy)
    drops.bind(metrics)
    processor = BoundedSpanProcessor(
        SanitizingSpanExporter(
            span_exporter
            if span_exporter is not None
            else OTLPSpanExporter(
                endpoint=config.otlp_endpoint, timeout=config.export_timeout_seconds
            ),
            policy,
        ),
        on_drop=lambda count: drops(count, "traces"),
        max_queue_size=config.max_queue_size,
        max_export_batch_size=config.max_export_batch_size,
        schedule_delay_seconds=config.schedule_delay_seconds,
        shutdown_timeout_seconds=config.export_timeout_seconds,
    )
    tracer_provider = TracerProvider(resource=resource, sampler=ALWAYS_ON)
    tracer_provider.add_span_processor(processor)
    return Telemetry(tracer_provider, meter_provider, metrics, processor)


@dataclass(slots=True)
class AutoInstrumentation:
    """Instrumentaciones activadas; ``uninstrument`` las retira (pruebas y cierre)."""

    _undo: list[Callable[[], None]] = field(default_factory=list)

    def uninstrument(self) -> None:
        while self._undo:
            self._undo.pop()()


def enable_auto_instrumentation(
    telemetry: Telemetry,
    *,
    app: Any | None = None,
    engine: Any | None = None,
    httpx: bool = True,
    botocore: bool = True,
) -> AutoInstrumentation:
    """Activa la instrumentación automática de FastAPI, SQLAlchemy, httpx y botocore.

    ``app`` es la aplicación FastAPI y ``engine`` el motor de SQLAlchemy (síncrono o
    asíncrono); se omite lo que no se pasa. Las bibliotecas se importan aquí, no al importar el
    módulo, para que los módulos críticos que registran no dependan de ellas (NFR-NUC-25).
    """
    providers: dict[str, Any] = {
        "tracer_provider": telemetry.tracer_provider,
        "meter_provider": telemetry.meter_provider,
    }
    handle = AutoInstrumentation()
    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, **providers)
        handle._undo.append(lambda: FastAPIInstrumentor.uninstrument_app(app))
    if engine is not None:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        sqlalchemy_instrumentor = SQLAlchemyInstrumentor()
        sqlalchemy_instrumentor.instrument(
            engine=getattr(engine, "sync_engine", engine), enable_commenter=False, **providers
        )
        handle._undo.append(sqlalchemy_instrumentor.uninstrument)
    if httpx:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        httpx_instrumentor = HTTPXClientInstrumentor()
        httpx_instrumentor.instrument(**providers)
        handle._undo.append(httpx_instrumentor.uninstrument)
    if botocore:
        from opentelemetry.instrumentation.botocore import BotocoreInstrumentor

        botocore_class: Any = BotocoreInstrumentor  # Sin anotaciones de tipos en la biblioteca.
        botocore_instrumentor = botocore_class()
        botocore_instrumentor.instrument(**providers)
        handle._undo.append(botocore_instrumentor.uninstrument)
    return handle
