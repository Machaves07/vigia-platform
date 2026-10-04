"""PR-GOB-31, base: ningún valor sensible de U-03 sale en registros, métricas, trazas ni eventos.

Metapropiedad de PAT-GOB-SEG-08 (NFR-GOB-25, NFR-GOB-37; extiende PR-GOB-19 y hereda PR-NUC-54).
Los generadores del arnés (``telemetry_harness``) producen lo que NFR-GOB-25 prohíbe fuera del
expediente: código de alta en claro, huella de hardware en claro, PEM de la solicitud de firma,
URL prefirmada con su firma, texto declarado de estándar, texto de acta y ``reason_es``. La
propiedad los inyecta por cada canal por el que el código de U-03 podría sacarlos y busca cada
valor en todo lo emitido, también en los bytes OTLP:

- **registros**: campo permitido y desconocido, contexto, argumento ``%`` de un registrador ajeno,
  ``extra`` de terceros, mensaje de una excepción registrada y mensaje construido en ejecución;
- **métricas**: atributo de una métrica de la plataforma y de un instrumento crudo;
- **trazas**: atributo al crear y después, atributo de evento, nombre de tramo y de evento,
  excepción registrada y descripción del estado;
- **eventos**: el valor en cualquier campo de cadena de una carga de los 15 eventos de U-03, o en
  un campo nuevo. La validación de la bandeja lo rechaza y nada se guarda.

Con las políticas de redacción de U-02 tal como están no hace falta ampliarlas: ninguna fuga.
``test_without_the_barriers_the_property_finds_leaks`` las quita (y la validación de la bandeja)
y exige que Hypothesis encuentre una fuga en cada salida: el arnés detecta lo que promete.

Las llamadas que inyectan un valor como mensaje o nombre llevan ``# noqa: VIG004``: la regla de
lint las prohíbe en el código; aquí se hacen a propósito para probar la barrera en ejecución.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import pytest
from hypothesis import HealthCheck, Phase, assume, find, given, settings
from hypothesis import strategies as st
from opentelemetry.trace import Status, StatusCode

from tests.properties.gob import telemetry_harness
from tests.properties.gob.telemetry_harness import TelemetryCapture, sensitive_values
from tests.properties.gob.u03_records import event_payload, string_paths, u03_event_registry
from vigia_platform.ledger.schema_rules import field_nodes
from vigia_platform.shared.observability import logging as obs_logging
from vigia_platform.shared.observability import redaction, tracing
from vigia_platform.shared.observability.metrics import CATALOG, MetricKind
from vigia_platform.shared.outbox.publish import OutboxRejected

EVENTS = u03_event_registry()
EVENT_TYPES = EVENTS.compiled_types()

SPAN_OPERATION: Final = "u03 operacion"
SPAN_EVENT: Final = "u03 evento"

LOG_CHANNELS = ("field", "context", "argument", "stdlib_extra", "exception", "message")
METRIC_CHANNELS = ("platform_metric", "raw_instrument")
TRACE_CHANNELS = (
    "span_attribute",
    "start_attribute",
    "event_attribute",
    "span_name",
    "event_name",
    "record_exception",
    "status",
)
EVENT_CHANNELS = ("event_field", "event_extra_field")
CHANNELS = (*LOG_CHANNELS, *METRIC_CHANNELS, *TRACE_CHANNELS, *EVENT_CHANNELS)

_ALLOWED_KEYS = sorted(redaction.DEFAULT_POLICY.keys)
_unknown_keys = st.sampled_from(
    [
        "enrollment_code",
        "hardware_fingerprint",
        "csr_pem",
        "upload_url",
        "declared_text",
        "scope_text_es",
        "reason_es",
        "presigned_url",
        "certificate_signing_request",
    ]
)
_SPECS_WITH_ATTRIBUTES = [spec for spec in CATALOG if spec.attributes]


@dataclass(frozen=True)
class Injection:
    kind: str
    values: tuple[str, ...]
    channel: str
    key: str
    event: str
    path: str


HEX_DIGEST_KINDS: Final = frozenset({"hardware_fingerprint", "presigned_url"})
"""Valores sensibles que son 64 hexadecimales (la huella y la firma de la URL)."""


def has_field_shape(event_name: str, path: str, value: str) -> bool:
    """``value`` cumple ya la forma cerrada del campo (su patrón o su lista de valores).

    Es el límite de toda regla de esquema: un identificador técnico (``TechnicalId`` del
    contrato, ``^[a-z][a-z0-9_.-]{0,63}$``) admite 64 hexadecimales que empiecen por letra, y
    nada distingue esa huella de un ``model_version`` legítimo. Ahí la barrera es que ninguna ruta
    ponga una huella en ese campo (PR-GOB-31 sobre el código de cada ruta), no la carga.
    """
    compiled = EVENTS.get(event_name)
    assert compiled is not None
    nodes, _ = field_nodes(compiled.payload_schema)
    for node in nodes:
        if node.path != path or node.schema.get("type") != "string":
            continue
        pattern = node.schema.get("pattern")
        if isinstance(pattern, str) and re.fullmatch(pattern, value):
            return True
        if value in node.schema.get("enum", ()):
            return True
    return False


@st.composite
def _injection(draw: st.DrawFn, channels: Sequence[str]) -> Injection:
    kind, values = draw(sensitive_values())
    channel = draw(st.sampled_from(channels))
    key = draw(st.one_of(st.sampled_from(_ALLOWED_KEYS), _unknown_keys))
    compiled = draw(st.sampled_from(EVENT_TYPES))
    path = draw(st.sampled_from(string_paths(compiled.payload_schema)))
    if channel == "event_field":
        # Un valor con la forma legítima del campo no es una fuga que una carga pueda impedir.
        assume(not any(has_field_shape(compiled.event_name, path, value) for value in values))
    return Injection(kind, values, channel, key, compiled.event_name, path)


def injections(channels: Sequence[str] = CHANNELS) -> st.SearchStrategy[list[Injection]]:
    return st.lists(_injection(channels), min_size=1, max_size=6)


# --- inyección por canal --------------------------------------------------------------------


def _set_path(document: Any, path: str, value: str) -> bool:
    """Pone ``value`` en la ruta de contenido ``path`` (``/a[*]/b``); ``False`` si no existe."""
    parts = [part for part in path.replace("[*]", "/[*]").split("/") if part]
    targets: list[Any] = [document]
    for part in parts[:-1]:
        following: list[Any] = []
        for target in targets:
            if part == "[*]":
                following.extend(target if isinstance(target, list) else [])
            elif isinstance(target, dict) and part in target:
                following.append(target[part])
        targets = following
    last = parts[-1]
    placed = False
    for target in targets:
        if last == "[*]" and isinstance(target, list) and target:
            target[0] = value
            placed = True
        elif isinstance(target, dict) and last in target:
            target[last] = value
            placed = True
    return placed


def _record_metric(capture: TelemetryCapture, index: int, key: str, value: str) -> None:
    spec = _SPECS_WITH_ATTRIBUTES[index % len(_SPECS_WITH_ATTRIBUTES)]
    instrument: Any = capture.telemetry.metrics.instrument(spec.name)
    for attribute in {key, sorted(spec.attributes)[0]}:
        if spec.kind is MetricKind.COUNTER:
            instrument.add(1, {attribute: value})
        elif spec.kind is MetricKind.HISTOGRAM:
            instrument.record(5, {attribute: value})
        else:
            instrument.set(3, {attribute: value})


def inject(capture: TelemetryCapture, injection: Injection, payload: Mapping[str, Any]) -> None:
    """Intenta sacar el valor por el canal de ``injection`` dentro de ``capture``."""
    log = obs_logging.get_logger("catalog.application")
    third_party = logging.getLogger("tercero.biblioteca")
    tracer = capture.tracer
    for index, value in enumerate(injection.values):
        channel, key = injection.channel, injection.key
        built = "texto previo " + value + " texto posterior"
        if channel == "field":
            log.info("evento de prueba", **{key: value})
        elif channel == "context":
            with obs_logging.log_context(**{key: value}):
                log.info("evento con contexto")
        elif channel == "argument":
            third_party.warning("valor recibido %s", value)
        elif channel == "stdlib_extra":
            third_party.info("evento de biblioteca", extra={key: value})
        elif channel == "exception":
            try:
                raise ValueError(value)
            except ValueError:
                log.exception("fallo de prueba")
        elif channel == "message":
            log.warning(built)  # noqa: VIG004 — inyección hostil a propósito.
        elif channel == "platform_metric":
            _record_metric(capture, index, key, value)
        elif channel == "raw_instrument":
            raw = capture.telemetry.meter_provider.get_meter("instrumentacion").create_counter(
                "instrumentacion_u03_prueba"
            )
            raw.add(1, {key: value, "http.route": value})
        elif channel == "span_name":
            with tracer.start_as_current_span(built):  # noqa: VIG004 — inyección hostil.
                pass
        elif channel == "start_attribute":
            with tracer.start_as_current_span(SPAN_OPERATION, attributes={key: value}):
                pass
        elif channel in {"span_attribute", "event_attribute", "event_name", "record_exception"}:
            with tracer.start_as_current_span(SPAN_OPERATION) as span:
                if channel == "span_attribute":
                    span.set_attribute(key, value)
                elif channel == "event_attribute":
                    span.add_event(SPAN_EVENT, {key: value})
                elif channel == "event_name":
                    span.add_event(built)  # noqa: VIG004 — inyección hostil.
                else:
                    span.record_exception(ValueError(value))
        elif channel == "status":
            with tracer.start_as_current_span(SPAN_OPERATION) as span:
                span.set_status(Status(StatusCode.ERROR, value))
        else:
            document = json.loads(json.dumps(payload))
            if channel == "event_field":
                _set_path(document, injection.path, value)
            else:
                document[key if key not in document else f"{key}_extra"] = value
            with contextlib.suppress(OutboxRejected):
                capture.publish(injection.event, document)


def emit(injections: Sequence[Injection], payloads: Sequence[Mapping[str, Any]]) -> list[str]:
    """Inyecta todo en una captura y devuelve las fugas (clase y canal)."""
    with TelemetryCapture() as capture:
        for injection, payload in zip(injections, payloads, strict=True):
            inject(capture, injection, payload)
    found: list[str] = []
    text = "\n".join(capture.emitted())
    blob = b"\n".join(capture.wire())
    for injection in injections:
        for value in injection.values:
            if value in text or value.encode("utf-8") in blob:
                found.append(f"{injection.kind} por {injection.channel}")
    return found


@st.composite
def cases(
    draw: st.DrawFn, channels: Sequence[str] = CHANNELS
) -> tuple[list[Injection], list[Mapping[str, Any]]]:
    chosen = draw(injections(channels))
    payloads = [draw(event_payload(EVENTS.get(i.event))) for i in chosen]  # type: ignore[arg-type]
    return chosen, payloads


_SETTINGS = settings(suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much])


@_SETTINGS
@given(cases())
def test_no_generated_sensitive_value_reaches_any_output(
    case: tuple[list[Injection], list[Mapping[str, Any]]],
) -> None:
    assert emit(*case) == []


@_SETTINGS
@given(data=st.data(), compiled=st.sampled_from(EVENT_TYPES))
def test_every_string_field_of_every_event_refuses_every_sensitive_value(
    data: st.DataObject, compiled: Any
) -> None:
    """La carga de un evento nunca admite un valor sensible, en ningún campo de cadena.

    Salvo cuando el valor ya tiene la forma cerrada del campo (``has_field_shape``): eso solo le
    pasa a un hexadecimal de 64 en un ``TechnicalId``, y la prueba lo comprueba.
    """
    kind, values = data.draw(sensitive_values())
    path = data.draw(st.sampled_from(string_paths(compiled.payload_schema)))
    payload = data.draw(event_payload(compiled))
    refused: list[str] = []
    with TelemetryCapture() as capture:
        capture.publish(compiled.event_name, payload)
        for value in values:
            if has_field_shape(compiled.event_name, path, value):
                assert kind in HEX_DIGEST_KINDS, (kind, path)
                continue
            document = json.loads(json.dumps(payload))
            if _set_path(document, path, value):
                with pytest.raises(OutboxRejected):
                    capture.publish(compiled.event_name, document)
            refused.append(value)
    assert capture.leaks(refused) == []


def test_a_hex_digest_has_the_shape_of_a_technical_id() -> None:
    """El límite de la regla anterior, a la vista: regresión de la semilla 2613962578 del CI."""
    fingerprint = "a" + "0" * 63
    assert has_field_shape("regression_cleared", "/model_version", fingerprint)
    assert not has_field_shape("regression_cleared", "/model_version", "0" * 64)
    assert not has_field_shape("regression_cleared", "/zone_id", fingerprint)
    assert not has_field_shape("node_enrolled", "/node_id", fingerprint)


def test_the_harness_sees_what_is_legitimately_emitted() -> None:
    """Lo que sí sale (identificadores y enumeraciones) aparece: el arnés captura las 4 salidas."""
    zone = "0190a8a0-0000-7000-8000-00000000000a"
    compiled = EVENTS.get("catalog_updated")
    assert compiled is not None
    with TelemetryCapture() as capture:
        obs_logging.get_logger("catalog.application").info("publicado", zone_id=zone)
        with capture.tracer.start_as_current_span(SPAN_OPERATION, attributes={"zone_id": zone}):
            pass
        _record_metric(capture, 0, "zone_id", zone)
        capture.publish(
            "catalog_updated",
            {"zone_id": zone, "catalog_version": 7, "changed_fields": ["standards"]},
        )
    emitted = "\n".join(capture.emitted())
    assert emitted.count(zone) >= 3
    assert '"changed_fields": ["standards"]' in emitted


# --- el arnés detecta la fuga cuando faltan las barreras -------------------------------------


def _without_barriers(monkeypatch: pytest.MonkeyPatch) -> None:
    def clean(
        self: redaction.AttributePolicy, attributes: Mapping[str, object] | None, **kwargs: object
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
    monkeypatch.setattr(tracing, "sanitize_span", lambda span, policy: span)
    monkeypatch.setattr(
        telemetry_harness, "_validated_payload", lambda compiled, payload: dict(payload)
    )


@pytest.mark.parametrize(
    "channels",
    [LOG_CHANNELS, METRIC_CHANNELS, TRACE_CHANNELS, EVENT_CHANNELS],
    ids=["registros", "metricas", "trazas", "eventos"],
)
def test_without_the_barriers_the_property_finds_leaks(
    monkeypatch: pytest.MonkeyPatch, channels: Sequence[str]
) -> None:
    _without_barriers(monkeypatch)
    example = find(
        cases(channels),
        lambda case: bool(emit(*case)),
        settings=settings(
            database=None,
            max_examples=200,
            phases=(Phase.generate,),  # basta con encontrar una fuga: sin reducirla
            suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
        ),
    )
    assert emit(*example) != []
