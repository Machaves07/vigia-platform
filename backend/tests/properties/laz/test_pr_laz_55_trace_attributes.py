"""PR-LAZ-55: atributos de traza y de métrica de U-04 sin texto libre ni usuario (TASK-349).

**Metapropiedad sobre una ejecución generada** que atraviesa dobles del asistente
(``FakeAssistantProvider``), del correo (``FakeEmailSender``) y de una exportación con varios
artefactos, emitiendo como lo harán sus adaptadores: tramos con ``long_job_attributes`` e
instrumentos de ``LoopMetrics``, sobre un ``TracerProvider`` con exportador en memoria (sin la
limpieza del exportador de producción: la garantía es del módulo de U-04) y un
``InMemoryMetricReader``. Los dobles son **descuidados a propósito**: además de los atributos
legítimos (identificadores del trabajo, modelo, esfuerzo, fichas, motivo de parada, artefacto,
estado, organización y planta) intentan sacar el contexto enviado, el borrador, el destinatario,
el asunto, el enlace, el valor del término rechazado y el identificador del usuario, por nombres
no admitidos y **dentro** de los admitidos (un texto libre como modelo, motivo de parada o
estado). Sobre todo lo exportado:

- ningún valor de atributo de tramo, evento ni serie **coincide con ni contiene** ninguno de los
  textos libres, correos, enlaces ni identificadores de usuario generados;
- ningún atributo se llama como un dato de usuario (``user_id``, ``actor_id``…) ni de nodo;
- la metapropiedad de cardinalidad de U-03 (``violations`` de
  ``tests/properties/gob/test_metrics_cardinality.py``) sigue verde con las series de U-04;
- los registros estructurados de la misma ejecución tampoco llevan esos valores, y el término
  rechazado sale como ``term_id`` (NFR-LAZ-17).

Y no es vacía: los atributos legítimos salen (el ``correlation_id`` de la petición en cada tramo,
el modelo registrado, las fichas, un tramo por artefacto).

Solo datos generados.
"""

from __future__ import annotations

import io
import json
import logging
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Final

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.properties.gob.test_metrics_cardinality import exported, violations
from vigia_platform.loop.observability.metrics import (
    LONG_JOB_ATTRIBUTE_KEYS,
    LoopMetricName,
    LoopMetrics,
    long_job_attributes,
    register_long_job_values,
)
from vigia_platform.shared.observability import redaction
from vigia_platform.shared.observability.logging import JsonFormatter, get_logger
from vigia_platform.shared.observability.metrics import METER_NAME

MODEL: Final = "claude-opus-5-5"
ARTIFACTS: Final = ("manifest", "document", "clips", "verifier")
STEPS: Final = ("context", "provider_call", "validation")
register_long_job_values(models=[MODEL], artifacts=ARTIFACTS, steps=STEPS)

_log = get_logger("loop.probe")
N: Final = LoopMetricName
FORBIDDEN_KEYS: Final = frozenset(
    {"user_id", "actor_id", "node_id", "recipient", "subject", "prompt", "context", "draft"}
)

# --- Vocabulario legítimo y valores sensibles generados ------------------------------------------

VOCABULARY: Final = frozenset(
    {
        redaction.OTHER,
        redaction.REDACTED,
        *(
            value
            for key in redaction.DEFAULT_POLICY.keys
            for value in redaction.DEFAULT_POLICY.values(key)
        ),
        *(name.value for name in LoopMetricName),
    }
)


def _not_in_vocabulary(text: str) -> bool:
    # Un texto libre que fuera un trozo de un valor legítimo (``ready``, ``end_turn``) o de un
    # identificador (solo hexadecimales y guiones) no distingue una fuga de un valor permitido.
    return (
        not any(text in value for value in VOCABULARY)
        and any(char not in "0123456789abcdef-" for char in text)
        and text.strip() == text
    )


FREE_TEXT: Final = st.text(
    alphabet=st.characters(codec="utf-8", exclude_categories=("Cs", "Cc")), min_size=6, max_size=40
).filter(_not_in_vocabulary)
EMAILS: Final = st.emails().filter(lambda email: len(email) >= 6)
LINKS: Final = st.builds(
    lambda host, path: f"https://{host}/{path}",
    st.from_regex(r"[a-z]{3,10}\.(com|co|io)", fullmatch=True),
    st.from_regex(r"[a-z0-9]{1,12}", fullmatch=True),
)


@dataclass(frozen=True)
class Sensitive:
    """Lo que nunca puede salir: textos del cliente, correos, enlaces y el usuario."""

    texts: tuple[str, ...]
    email: str
    link: str
    user_id: uuid.UUID

    def values(self) -> tuple[str, ...]:
        return (*self.texts, self.email, self.link, str(self.user_id))


@dataclass(frozen=True)
class Run:
    correlation_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    assistant_request_id: uuid.UUID
    export_request_id: uuid.UUID
    term_id: uuid.UUID
    model: str
    stop_reason: str
    assistant_state: str
    email_state: str
    input_tokens: int
    output_tokens: int
    artifacts: tuple[str, ...]
    sensitive: Sensitive


def runs() -> st.SearchStrategy[Run]:
    texts = st.lists(FREE_TEXT, min_size=4, max_size=4).map(tuple)
    sensitive = st.builds(Sensitive, texts, EMAILS, LINKS, st.uuids())
    legit_or_hostile = lambda legit: st.one_of(st.sampled_from(legit), FREE_TEXT)  # noqa: E731
    return st.builds(
        Run,
        correlation_id=st.uuids(),
        organization_id=st.uuids(),
        plant_id=st.uuids(),
        assistant_request_id=st.uuids(),
        export_request_id=st.uuids(),
        term_id=st.uuids(),
        model=legit_or_hostile([MODEL]),
        stop_reason=legit_or_hostile(["end_turn", "max_tokens", "refusal"]),
        assistant_state=legit_or_hostile(["ready", "unavailable", "rejected_by_validators"]),
        email_state=legit_or_hostile(["sent", "bounced", "digested", "degraded_app_only"]),
        input_tokens=st.integers(min_value=0, max_value=1_000_000),
        output_tokens=st.integers(min_value=0, max_value=200_000),
        artifacts=st.lists(
            st.one_of(st.sampled_from(ARTIFACTS), FREE_TEXT), min_size=1, max_size=5
        ).map(tuple),
        sensitive=sensitive,
    )


# --- Dobles descuidados --------------------------------------------------------------------------


class Telemetry:
    def __init__(self) -> None:
        self.spans = InMemorySpanExporter()
        self.tracer_provider = TracerProvider()
        self.tracer_provider.add_span_processor(SimpleSpanProcessor(self.spans))
        self.tracer = self.tracer_provider.get_tracer("pruebas")
        self.reader = InMemoryMetricReader()
        self.meter_provider = MeterProvider(metric_readers=[self.reader])
        self.metrics = LoopMetrics(self.meter_provider.get_meter(METER_NAME))
        self.logs = io.StringIO()
        self._handler = logging.StreamHandler(self.logs)
        self._handler.setFormatter(JsonFormatter())
        self._logger = logging.getLogger("vigia.loop.probe")
        self._logger.addHandler(self._handler)
        self._logger.setLevel(logging.INFO)

    def reset(self) -> None:
        self.spans.clear()
        self.logs.seek(0)
        self.logs.truncate()

    def close(self) -> None:
        self._logger.removeHandler(self._handler)
        self.tracer_provider.shutdown()
        self.meter_provider.shutdown()


def _hostile(run: Run) -> dict[str, object]:
    """Lo que un adaptador descuidado intentaría añadir a un tramo, una serie o un registro."""
    s = run.sensitive
    return {
        "user_id": s.user_id,
        "actor_id": str(s.user_id),
        "prompt": s.texts[0],
        "context": s.texts[1],
        "draft": s.texts[2],
        "term_value": s.texts[3],
        "recipient": s.email,
        "subject": s.texts[0],
        "link": s.link,
        "model_note": s.link,
    }


def _for_log(run: Run) -> dict[str, object]:
    """Lo mismo para un registro, sin ``actor_id``: en los registros, el actor de una operación
    lo admite la política heredada (NFR-NUC-17); PR-LAZ-55 trata de trazas y métricas."""
    return {key: value for key, value in _hostile(run).items() if key != "actor_id"}


class FakeAssistantProvider:
    """Doble del asistente (NFR-LAZ-47): un tramo por paso y las métricas de la solicitud."""

    def __init__(self, telemetry: Telemetry) -> None:
        self.t = telemetry

    def request(self, run: Run) -> None:
        metrics = self.t.metrics
        for step in STEPS:
            attributes = long_job_attributes(
                {
                    "correlation_id": run.correlation_id,
                    "assistant_request_id": run.assistant_request_id,
                    "step": step,
                    "model": run.model,
                    "effort": "high",
                    "input_tokens": run.input_tokens,
                    "output_tokens": run.output_tokens,
                    "stop_reason": run.stop_reason,
                    **_hostile(run),
                }
            )
            with self.t.tracer.start_as_current_span("assistant.step", attributes=attributes):
                pass
        organization = {"organization_id": run.organization_id, **_hostile(run)}
        state = {"assistant_state": run.assistant_state}
        metrics.counter(N.ASSISTANT_REQUESTS_TOTAL).add(1, {**state, **organization})
        metrics.histogram(N.ASSISTANT_REQUEST_DURATION_MS).record(1200, {**state, **_hostile(run)})
        for direction, tokens in (("input", run.input_tokens), ("output", run.output_tokens)):
            metrics.counter(N.ASSISTANT_TOKENS_TOTAL).add(
                tokens, {"token_direction": direction, **organization}
            )
        metrics.gauge(N.ASSISTANT_TOKEN_BUDGET_USED_RATIO).set(0.42, organization)
        metrics.counter(N.LOOP_TEXT_VALIDATIONS_TOTAL).add(
            1,
            {
                "text_validator": "blocked_vocabulary",
                "verdict": "rejected_blocked_vocabulary",
                "term_id": run.term_id,
                **_hostile(run),
            },
        )
        _log.warning(
            "borrador rechazado por el validador",
            term_id=run.term_id,
            assistant_request_id=run.assistant_request_id,
            correlation_id=run.correlation_id,
            **_for_log(run),
        )


class FakeEmailSender:
    """Doble del correo (NFR-LAZ-47): un tramo por envío y el correo por estado."""

    def __init__(self, telemetry: Telemetry) -> None:
        self.t = telemetry

    def send(self, run: Run) -> None:
        attributes = long_job_attributes({"correlation_id": run.correlation_id, **_hostile(run)})
        with self.t.tracer.start_as_current_span("email.send", attributes=attributes) as span:
            span.add_event("email.sent", attributes=long_job_attributes(_hostile(run)))
        self.t.metrics.counter(N.EMAIL_MESSAGES_TOTAL).add(
            1,
            {
                "email_state": run.email_state,
                "organization_id": run.organization_id,
                **_hostile(run),
            },
        )
        _log.info("correo enviado", correlation_id=run.correlation_id, **_for_log(run))


def export(telemetry: Telemetry, run: Run) -> None:
    """Doble de la exportación: un tramo por artefacto y las métricas del paquete."""
    for artifact in run.artifacts:
        attributes = long_job_attributes(
            {
                "correlation_id": run.correlation_id,
                "export_request_id": run.export_request_id,
                "artifact": artifact,
                **_hostile(run),
            }
        )
        with telemetry.tracer.start_as_current_span("export.artifact", attributes=attributes):
            pass
    metrics = telemetry.metrics
    place = {"organization_id": run.organization_id, "plant_id": run.plant_id, **_hostile(run)}
    metrics.counter(N.EXPORT_REQUESTS_TOTAL).add(1, {"export_state": "ready", **place})
    metrics.histogram(N.EXPORT_DURATION_MS).record(90_000, {"export_state": "ready", **place})
    metrics.histogram(N.EXPORT_PACKAGE_SIZE_BYTES).record(1 << 20, place)
    metrics.gauge(N.LOOP_OVERDUE_FINDINGS).set(3, place)
    metrics.gauge(N.LOOP_UNCLASSIFIED_FINDINGS).set(2, place)


# --- La metapropiedad ----------------------------------------------------------------------------


@pytest.fixture
def telemetry() -> Iterator[Telemetry]:
    environment = Telemetry()
    yield environment
    environment.close()


def _span_attributes(telemetry: Telemetry) -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    for span in telemetry.spans.get_finished_spans():
        found.append((span.name, dict(span.attributes or {})))
        found.extend((event.name, dict(event.attributes or {})) for event in span.events)
    return found


def _metric_attributes(telemetry: Telemetry) -> list[tuple[str, dict[str, Any]]]:
    return [(point.metric, dict(point.attributes)) for point in exported(telemetry.reader)]


def _leaks(attributes: dict[str, Any], sensitive: Sensitive) -> list[str]:
    leaks = [f"clave {key}" for key in attributes if key in FORBIDDEN_KEYS]
    for key, value in attributes.items():
        text = str(value)
        leaks.extend(f"{key}={text!r}" for secret in sensitive.values() if secret in text)
    return leaks


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(run=runs())
def test_pr_laz_55_no_attribute_carries_free_text_or_user(telemetry: Telemetry, run: Run) -> None:
    identifiers = {
        run.correlation_id,
        run.organization_id,
        run.plant_id,
        run.assistant_request_id,
        run.export_request_id,
        run.term_id,
    }
    assume(run.sensitive.user_id not in identifiers and len(identifiers) == 6)
    telemetry.reset()
    FakeAssistantProvider(telemetry).request(run)
    FakeEmailSender(telemetry).send(run)
    export(telemetry, run)

    spans = _span_attributes(telemetry)
    series = _metric_attributes(telemetry)
    for name, attributes in [*spans, *series]:
        assert _leaks(attributes, run.sensitive) == [], name
    for name, attributes in spans:
        assert set(attributes) <= LONG_JOB_ATTRIBUTE_KEYS, name
    assert violations(exported(telemetry.reader)) == []

    lines = telemetry.logs.getvalue()
    for secret in run.sensitive.values():
        assert secret not in lines
    for line in lines.splitlines():
        assert not set(json.loads(line)) & FORBIDDEN_KEYS

    # No es vacía: lo legítimo sale.
    finished = telemetry.spans.get_finished_spans()
    assert all(
        (span.attributes or {}).get("correlation_id") == str(run.correlation_id)
        for span in finished
    )
    artifact_spans = [span for span in finished if span.name == "export.artifact"]
    assert len(artifact_spans) == len(run.artifacts)
    assert [(span.attributes or {})["artifact"] for span in artifact_spans] == [
        artifact if artifact in ARTIFACTS else redaction.OTHER for artifact in run.artifacts
    ]
    call = next(span for span in finished if (span.attributes or {}).get("step") == "provider_call")
    assert (call.attributes or {})["model"] == (run.model if run.model == MODEL else "other")
    assert (call.attributes or {})["input_tokens"] == run.input_tokens
    assert f'"term_id": "{run.term_id}"' in lines


def test_the_long_job_attributes_are_only_identifiers_enumerations_and_counts() -> None:
    policy = redaction.DEFAULT_POLICY
    for key in LONG_JOB_ATTRIBUTE_KEYS:
        assert (
            key in redaction.IDENTIFIER_KEYS
            or key in redaction.INTEGER_KEYS
            or key in redaction.DEFAULT_ENUMERATIONS
        ), key
    assert "user_id" not in policy.keys
    assert "actor_id" not in LONG_JOB_ATTRIBUTE_KEYS
    assert long_job_attributes({"input_tokens": 10_000_001, "output_tokens": -1}) == {}
    assert long_job_attributes({"input_tokens": True}) == {}
    assert long_job_attributes({"effort": "ultra", "stop_reason": "a" * 300}) == {
        "effort": "other",
        "stop_reason": "other",
    }
