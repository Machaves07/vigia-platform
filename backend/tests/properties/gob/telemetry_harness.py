"""Arnés de telemetría de U-03 para PR-GOB-31 (PAT-GOB-SEG-08; NFR-GOB-25, 37).

``TelemetryCapture`` instala **exportadores en memoria** de las cuatro salidas por las que un dato
puede escapar del expediente: registros (el ``JsonFormatter`` de la plataforma sobre un búfer),
métricas y trazas (``configure_telemetry`` con exportadores de prueba, más los bytes OTLP que
saldrían hacia el colector) y eventos de la bandeja (las cargas que la validación de
``shared.outbox.publish`` dejaría guardar). Al salir, ``leaks(valores)`` dice qué valores
aparecen en algo de lo emitido.

Los generadores producen los valores sensibles de U-03 que NFR-GOB-25 prohíbe fuera del
expediente: código de alta de 12 caracteres, huella de hardware en claro, bloque PEM de la
solicitud de firma, URL prefirmada con su firma, texto declarado de estándar, texto de acta y
``reason_es``. Cada uno lleva algo que ninguna salida legítima contiene (12 símbolos del alfabeto
del código seguidos, 64 hexadecimales, ``-----BEGIN``, ``://`` y ``X-Amz-Signature``, varias
palabras con una mayúscula), así que encontrarlo por subcadena es una fuga.

Las tareas de rutas y de tareas periódicas de U-03 lo reutilizan sobre su propio código::

    with TelemetryCapture() as capture:
        await ruta(...)  # con capture.tracer, capture.metrics y capture.publish
    assert capture.leaks(valores_generados) == []

Solo datos generados.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import textwrap
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final, Self

from hypothesis import strategies as st
from opentelemetry.exporter.otlp.proto.common.metrics_encoder import encode_metrics
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult, MetricsData
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.properties.gob.u03_records import u03_event_registry
from vigia_platform.shared.observability import logging as obs_logging
from vigia_platform.shared.observability import redaction, tracing
from vigia_platform.shared.outbox.publish import _validated_payload
from vigia_platform.shared.outbox.registries import EventTypeRegistry

__all__ = [
    "ENROLLMENT_CODE_ALPHABET",
    "SENSITIVE_KINDS",
    "TelemetryCapture",
    "acta_texts",
    "declared_texts",
    "enrollment_codes",
    "hardware_fingerprints",
    "pem_csrs",
    "presigned_urls",
    "reasons",
    "sensitive_values",
]

ENROLLMENT_CODE_ALPHABET: Final = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
"""Alfabeto del código de alta: 12 caracteres sin ambiguos (BR-CTR-45)."""

_WORDS: Final = (
    "zona",
    "prensa",
    "bloqueo",
    "guarda",
    "acceso",
    "turno",
    "cámara",
    "encuadre",
    "puerta",
    "señal",
    "operación",
    "arranque",
    "mantenimiento",
    "celda",
    "línea",
)


def _phrase(words: list[str], capital: int) -> str:
    chosen = list(words)
    chosen[capital % len(chosen)] = chosen[capital % len(chosen)].capitalize()
    return " ".join(chosen)


def _texts(min_words: int, max_words: int) -> st.SearchStrategy[str]:
    return st.builds(
        _phrase,
        st.lists(st.sampled_from(_WORDS), min_size=min_words, max_size=max_words),
        st.integers(min_value=0, max_value=64),
    )


enrollment_codes: Final = st.text(alphabet=ENROLLMENT_CODE_ALPHABET, min_size=12, max_size=12)
"""Código de alta en claro: nunca se persiste ni se registra (respuesta 14)."""

hardware_fingerprints: Final = st.binary(min_size=32, max_size=32).map(bytes.hex)
"""Huella de hardware en claro (64 hexadecimales): fuera del expediente, solo su hash."""

pem_csrs: Final = st.binary(min_size=48, max_size=256).map(
    lambda body: (
        "-----BEGIN CERTIFICATE REQUEST-----\n"
        + "\n".join(textwrap.wrap(base64.b64encode(body).decode("ascii"), 64))
        + "\n-----END CERTIFICATE REQUEST-----"
    )
)
"""PEM de la solicitud de firma del nodo."""


@dataclass(frozen=True)
class PresignedUrl:
    url: str
    signature: str


_UUID_TEXT: Final = st.uuids(version=4).map(str)

presigned_urls: Final = st.builds(
    lambda account, org, plant, zone, node, clip, signature: PresignedUrl(
        url=(
            f"https://vigia-evidence-{account}-us-east-1.s3.us-east-1.amazonaws.com/"
            f"org/{org}/plant/{plant}/zone/{zone}/node/{node}/{clip}.mp4"
            "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Expires=900"
            f"&X-Amz-Signature={signature}"
        ),
        signature=signature,
    ),
    st.from_regex(r"[0-9]{12}", fullmatch=True),
    _UUID_TEXT,
    _UUID_TEXT,
    _UUID_TEXT,
    _UUID_TEXT,
    _UUID_TEXT,
    st.binary(min_size=32, max_size=32).map(bytes.hex),
)
"""URL prefirmada de un clip con su firma; se buscan la URL completa y la firma sola."""

declared_texts: Final = _texts(6, 20)
"""Texto declarado de un estándar (``declared_text``, ``title_es``)."""

acta_texts: Final = _texts(4, 30)
"""Texto de acta (``scope_text_es``, ``framing_description_es``, ``criteria_summary_es``)."""

reasons: Final = _texts(3, 12)
"""Motivos (``reason_es``, ``declared_reason_es``, ``justification_es``)."""


def _values(kind: str) -> st.SearchStrategy[tuple[str, tuple[str, ...]]]:
    if kind == "presigned_url":
        return presigned_urls.map(lambda value: (kind, (value.url, value.signature)))
    return SENSITIVE_KINDS[kind].map(lambda value: (kind, (value,)))


SENSITIVE_KINDS: Final[Mapping[str, st.SearchStrategy[Any]]] = {
    "enrollment_code": enrollment_codes,
    "hardware_fingerprint": hardware_fingerprints,
    "pem_csr": pem_csrs,
    "presigned_url": presigned_urls,
    "declared_text": declared_texts,
    "acta_text": acta_texts,
    "reason_es": reasons,
}


def sensitive_values() -> st.SearchStrategy[tuple[str, tuple[str, ...]]]:
    """Un valor sensible de U-03: su clase y las cadenas que no pueden aparecer en la salida."""
    return st.one_of(*(_values(kind) for kind in SENSITIVE_KINDS))


# --- captura en memoria -------------------------------------------------------------------------


class CollectingMetricExporter(MetricExporter):
    """Guarda lo que el SDK exportaría al colector."""

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


_SETTINGS: Final = tracing.TelemetrySettings(
    schedule_delay_seconds=3600, metric_export_interval_seconds=3600
)


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


class TelemetryCapture:
    """Registros, métricas, trazas y eventos de la bandeja de lo que corre dentro, en memoria."""

    def __init__(
        self,
        *,
        policy: redaction.AttributePolicy | None = None,
        events: EventTypeRegistry | None = None,
    ) -> None:
        self.policy = policy if policy is not None else redaction.AttributePolicy()
        self.events = events if events is not None else u03_event_registry()
        self._stream = io.StringIO()
        self._handler = logging.StreamHandler(self._stream)
        self._handler.setFormatter(obs_logging.JsonFormatter(self.policy))
        self._spans = InMemorySpanExporter()
        self._metrics = CollectingMetricExporter()
        self.telemetry = tracing.configure_telemetry(
            _SETTINGS, span_exporter=self._spans, metric_exporter=self._metrics, policy=self.policy
        )
        self.published: list[str] = []
        self._previous_level = logging.NOTSET
        self._closed = False

    def __enter__(self) -> Self:
        root = logging.getLogger()
        self._previous_level = root.level
        root.addHandler(self._handler)
        root.setLevel(logging.DEBUG)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        root = logging.getLogger()
        root.removeHandler(self._handler)
        root.setLevel(self._previous_level)
        self.telemetry.span_processor.force_flush()
        self.telemetry.meter_provider.force_flush()
        self.telemetry.shutdown()

    @property
    def tracer(self) -> Any:
        return self.telemetry.tracer()

    def publish(self, event_name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Valida la carga como ``Outbox.prepare`` y la guarda; ``OutboxRejected`` si no pasa."""
        compiled = self.events.get(event_name)
        if compiled is None:
            raise KeyError(event_name)
        stored = _validated_payload(compiled, payload)
        self.published.append(json.dumps(stored, ensure_ascii=False))
        return stored

    # --- lo emitido -------------------------------------------------------------------------

    def emitted(self) -> list[str]:
        """Todo el texto de las cuatro salidas (cerrar antes de leer)."""
        self.close()
        texts = [
            text
            for line in self._stream.getvalue().splitlines()
            for text in _strings(json.loads(line))
        ]
        texts.extend(
            text for span in self._spans.get_finished_spans() for text in _span_strings(span)
        )
        for batch in self._metrics.batches:
            for resource in batch.resource_metrics:
                texts.extend(_strings(dict(resource.resource.attributes)))
                for scope in resource.scope_metrics:
                    for metric in scope.metrics:
                        texts.append(metric.name)
                        for point in metric.data.data_points:
                            texts.extend(_strings(dict(point.attributes or {})))
        texts.extend(self.published)
        return texts

    def wire(self) -> list[bytes]:
        """Los bytes OTLP de trazas y métricas que saldrían hacia el colector."""
        self.close()
        spans = self._spans.get_finished_spans()
        blobs = [encode_spans(spans).SerializeToString()] if spans else []
        blobs.extend(encode_metrics(batch).SerializeToString() for batch in self._metrics.batches)
        return blobs

    def leaks(self, values: Sequence[str]) -> list[str]:
        """Valores que aparecen en lo emitido o en los bytes OTLP."""
        text = "\n".join(self.emitted())
        blob = b"\n".join(self.wire())
        return [value for value in values if value in text or value.encode("utf-8") in blob]
