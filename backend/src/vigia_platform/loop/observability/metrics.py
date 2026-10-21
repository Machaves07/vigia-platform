"""Módulo único de nombres de métrica y atributos de traza de U-04 (LC-LAZ-38; PAT-LAZ-MAN-02, 03).

Infrastructure Design de U-04 define alarmas y paneles **sobre estos nombres** (NFR-LAZ-41, 42 y
44; TASK-332 los contrasta con las alarmas): un nombre publicado no se renombra. Las tareas
posteriores de U-04 **añaden** aquí sus constantes; ninguna construye una métrica por su cuenta.

- ``LOOP_CATALOG``: cada métrica con su tipo, unidad, origen y los **únicos** atributos que admite,
  en el espacio ``Vigia/Platform`` de U-02 (mismo medidor ``vigia_platform``). Dimensión de
  **organización y planta, nunca de usuario ni de nodo** (NFR-LAZ-41, A-66): las cifras «por
  zona» de NFR-LAZ-41 se publican por planta y la zona se mira en la bandeja, que es por zona;
  una etiqueta por zona multiplicaría las series sin cota (NFR-GOB-13).
- ``NFR_LAZ_41``: cada partida del requisito con las métricas que la cubren.
- ``LOOP_ALARM_CONDITIONS``: las **ocho** alarmas de NFR-LAZ-42, cada una con la métrica que la
  dispara. ``EGRESS_BLOCKED_ALARM`` es la alerta de la fábrica de clientes (LC-LAZ-39), fuera de
  las ocho, y ``EXPORT_FAILED_ALARM`` el contador de ``export_worker`` para ``export-failed``.
- ``LONG_JOB_ATTRIBUTE_KEYS`` y ``long_job_attributes``: los atributos de traza del trabajo largo
  (NFR-LAZ-43): ``correlation_id``, ``export_request_id`` o ``assistant_request_id``, artefacto o
  paso, modelo, esfuerzo, fichas y motivo de parada. **Sin texto libre, sin contenido del contexto
  y sin identificador de usuario**: un valor fuera de su lista sale como ``other``.

Los atributos pasan por ``redaction.AttributePolicy`` (la misma de U-02, ampliada en
``shared.observability.redaction`` con las listas cerradas de U-04 y con ``term_id``): del término
que rechazó un texto sale su ``term_id``, **nunca su valor** (NFR-LAZ-17).
"""

from __future__ import annotations

import enum
import functools
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final

from opentelemetry import metrics as otel_metrics

from vigia_platform.shared.observability import redaction
from vigia_platform.shared.observability.metrics import METER_NAME, MetricKind

__all__ = [
    "EGRESS_BLOCKED_ALARM",
    "EXPORT_FAILED_ALARM",
    "LONG_JOB_ATTRIBUTE_KEYS",
    "LOOP_ALARM_CONDITIONS",
    "LOOP_CATALOG",
    "NFR_LAZ_41",
    "TRANSVERSAL_METRICS",
    "LoopAlarmCondition",
    "LoopCounter",
    "LoopGauge",
    "LoopHistogram",
    "LoopMetricName",
    "LoopMetricSpec",
    "LoopMetrics",
    "get_loop_metrics",
    "long_job_attributes",
    "register_long_job_values",
]


class LoopMetricName(enum.StrEnum):
    """Nombres fijos de U-04 publicados hacia el colector (espacio ``Vigia/Platform``)."""

    # Consumidores del lazo (NFR-LAZ-41)
    LOOP_CONSUMER_PENDING = "loop_consumer_pending"
    LOOP_CONSUMER_OLDEST_PENDING_AGE_SECONDS = "loop_consumer_oldest_pending_age_seconds"
    LOOP_DEAD_LETTER_ENTRIES_TOTAL = "loop_dead_letter_entries_total"
    # Tareas periódicas de U-04
    LOOP_PERIODIC_TASK_DURATION_MS = "loop_periodic_task_duration_ms"
    LOOP_PERIODIC_TASK_LAST_SUCCESS_AGE_SECONDS = "loop_periodic_task_last_success_age_seconds"
    # Asistente
    ASSISTANT_REQUESTS_TOTAL = "assistant_requests_total"
    ASSISTANT_REQUEST_DURATION_MS = "assistant_request_duration_ms"
    ASSISTANT_TOKENS_TOTAL = "assistant_tokens_total"
    ASSISTANT_UNAVAILABLE = "assistant_unavailable"
    ASSISTANT_TOKEN_BUDGET_USED_RATIO = "assistant_token_budget_used_ratio"  # noqa: S105 - nombre
    ASSISTANT_RATE_LIMIT_HEADROOM_RATIO = "assistant_rate_limit_headroom_ratio"
    # Correo
    EMAIL_MESSAGES_TOTAL = "email_messages_total"
    # Exportaciones
    EXPORT_REQUESTS_TOTAL = "export_requests_total"
    EXPORT_DURATION_MS = "export_duration_ms"
    EXPORT_PACKAGE_SIZE_BYTES = "export_package_size_bytes"
    EXPORT_STUCK_OR_FAILED = "export_stuck_or_failed"
    EXPORT_FAILED_TOTAL = "export_failed_total"
    # Proyección y estado del lazo
    LOOP_PROJECTION_DIVERGENCES_TOTAL = "loop_projection_divergences_total"
    LOOP_OVERDUE_FINDINGS = "loop_overdue_findings"
    LOOP_UNCLASSIFIED_FINDINGS = "loop_unclassified_findings"
    LOOP_UNCLASSIFIED_OLDEST_AGE_SECONDS = "loop_unclassified_oldest_age_seconds"
    # Validadores de texto
    LOOP_TEXT_VALIDATIONS_TOTAL = "loop_text_validations_total"
    # Transversales (LC-LAZ-39 y 40)
    LOOP_BULKHEAD_IN_USE = "loop_bulkhead_in_use"
    LOOP_BULKHEAD_SIZE = "loop_bulkhead_size"
    LOOP_BULKHEAD_SATURATED_TOTAL = "loop_bulkhead_saturated_total"
    LOOP_EGRESS_BLOCKED_TOTAL = "loop_egress_blocked_total"


@dataclass(frozen=True, slots=True)
class LoopMetricSpec:
    """Definición publicada de una métrica de U-04 (la forma de ``MetricSpec`` de U-02)."""

    name: LoopMetricName
    kind: MetricKind
    unit: str
    description: str
    group: str
    """Requisito o componente que la origina."""
    attributes: frozenset[str] = frozenset()
    """Únicos atributos admitidos: organización, planta y enumeraciones cerradas."""


def _spec(
    name: LoopMetricName,
    kind: MetricKind,
    unit: str,
    description: str,
    group: str,
    *attributes: str,
) -> LoopMetricSpec:
    return LoopMetricSpec(name, kind, unit, description, group, frozenset(attributes))


_C, _H, _G = MetricKind.COUNTER, MetricKind.HISTOGRAM, MetricKind.GAUGE
_N = LoopMetricName
_ORG, _PLANT = "organization_id", "plant_id"

LOOP_CATALOG: Final[tuple[LoopMetricSpec, ...]] = (
    # NFR-LAZ-41 · consumidores del lazo: pendientes, antigüedad y cola muerta.
    _spec(_N.LOOP_CONSUMER_PENDING, _G, "{event}", "Entregas pendientes por consumidor del lazo.",
          "NFR-LAZ-41", "consumer"),
    _spec(_N.LOOP_CONSUMER_OLDEST_PENDING_AGE_SECONDS, _G, "s",
          "Antigüedad del evento pendiente más viejo por consumidor del lazo.", "NFR-LAZ-41",
          "consumer"),
    _spec(_N.LOOP_DEAD_LETTER_ENTRIES_TOTAL, _C, "{event}",
          "Entradas en cola muerta de los consumidores de U-04.", "NFR-LAZ-41", "consumer"),
    # NFR-LAZ-41 · las diez tareas periódicas: duración y resultado; la antigüedad del último
    # éxito alimenta la alarma de tarea atrasada.
    _spec(_N.LOOP_PERIODIC_TASK_DURATION_MS, _H, "ms",
          "Duración de cada tarea periódica de U-04 por resultado.", "NFR-LAZ-41",
          "task", "result"),
    _spec(_N.LOOP_PERIODIC_TASK_LAST_SUCCESS_AGE_SECONDS, _G, "s",
          "Tiempo desde el último éxito de cada tarea periódica de U-04.", "NFR-LAZ-42", "task"),
    # NFR-LAZ-41 · asistente: solicitudes por estado y su latencia; fichas por organización (el
    # mes lo da la agregación del panel sobre el contador).
    _spec(_N.ASSISTANT_REQUESTS_TOTAL, _C, "{request}",
          "Solicitudes al asistente por estado.", "NFR-LAZ-41", "assistant_state", _ORG),
    _spec(_N.ASSISTANT_REQUEST_DURATION_MS, _H, "ms",
          "Latencia de las solicitudes al asistente por estado.", "NFR-LAZ-41", "assistant_state"),
    _spec(_N.ASSISTANT_TOKENS_TOTAL, _C, "{token}",
          "Fichas de entrada y de salida consumidas por organización.", "NFR-LAZ-41",
          "token_direction", _ORG),
    # NFR-LAZ-42 · una métrica por condición de alarma del asistente.
    _spec(_N.ASSISTANT_UNAVAILABLE, _G, "1",
          "Asistente no disponible (1: circuito abierto o última solicitud unavailable) o no (0).",
          "NFR-LAZ-42"),
    _spec(_N.ASSISTANT_TOKEN_BUDGET_USED_RATIO, _G, "1",
          "Fracción del tope mensual de fichas consumida por organización (NFR-LAZ-35).",
          "NFR-LAZ-42", _ORG),
    _spec(_N.ASSISTANT_RATE_LIMIT_HEADROOM_RATIO, _G, "1",
          "Margen de límite de tasa informado por las cabeceras del proveedor.", "NFR-LAZ-42"),
    # NFR-LAZ-41 · correo por estado (sent, bounced, complained, digested, degraded_app_only).
    _spec(_N.EMAIL_MESSAGES_TOTAL, _C, "{message}", "Correos por estado.", "NFR-LAZ-41",
          "email_state", _ORG),
    # NFR-LAZ-41 · exportaciones por estado, duración y tamaño del paquete.
    _spec(_N.EXPORT_REQUESTS_TOTAL, _C, "{request}", "Exportaciones por estado.", "NFR-LAZ-41",
          "export_state", _ORG),
    _spec(_N.EXPORT_DURATION_MS, _H, "ms", "Duración de la composición de una exportación.",
          "NFR-LAZ-41", "export_state"),
    _spec(_N.EXPORT_PACKAGE_SIZE_BYTES, _H, "By", "Tamaño del paquete exportado.", "NFR-LAZ-41"),
    _spec(_N.EXPORT_STUCK_OR_FAILED, _G, "1",
          "Exportación abierta de la organización por encima de 60 min o con dos fallos seguidos "
          "(1) o no (0).", "NFR-LAZ-42", _ORG),
    # NFR-LAZ-42 · el contador que ``export_worker`` (TASK-317) suma a los dos fallos seguidos de
    # una solicitud: la alarma ``export-failed`` (TASK-332).
    _spec(_N.EXPORT_FAILED_TOTAL, _C, "{request}",
          "Exportaciones con dos fallos seguidos de la misma solicitud.", "NFR-LAZ-42",
          _ORG, _PLANT),
    # NFR-LAZ-41 · proyección y estado del lazo, por organización y planta.
    _spec(_N.LOOP_PROJECTION_DIVERGENCES_TOTAL, _C, "{divergence}",
          "Divergencias entre la proyección y el expediente.", "NFR-LAZ-41", _ORG),
    _spec(_N.LOOP_OVERDUE_FINDINGS, _G, "{finding}", "Hallazgos vencidos por planta.",
          "NFR-LAZ-41", _ORG, _PLANT),
    _spec(_N.LOOP_UNCLASSIFIED_FINDINGS, _G, "{finding}", "Hallazgos sin clasificar por planta.",
          "NFR-LAZ-41", _ORG, _PLANT),
    _spec(_N.LOOP_UNCLASSIFIED_OLDEST_AGE_SECONDS, _G, "s",
          "Antigüedad del hallazgo sin clasificar más viejo por planta.", "NFR-LAZ-41",
          _ORG, _PLANT),
    # NFR-LAZ-41 · validaciones de texto por veredicto (el término sale como term_id en los
    # registros, nunca en una métrica).
    _spec(_N.LOOP_TEXT_VALIDATIONS_TOTAL, _C, "{validation}",
          "Validaciones de texto por validador y veredicto.", "NFR-LAZ-41",
          "text_validator", "verdict"),
    # LC-LAZ-40 · mamparos por consumidor: ocupación, tamaño y saturación.
    _spec(_N.LOOP_BULKHEAD_IN_USE, _G, "{delivery}",
          "Entregas en curso dentro del mamparo del consumidor.", "LC-LAZ-40", "consumer"),
    _spec(_N.LOOP_BULKHEAD_SIZE, _G, "{delivery}", "Concurrencia declarada del consumidor.",
          "LC-LAZ-40", "consumer"),
    _spec(_N.LOOP_BULKHEAD_SATURATED_TOTAL, _C, "{round}",
          "Rondas que dejaron la partición porque el consumidor estaba en su tope.", "LC-LAZ-40",
          "consumer"),
    # LC-LAZ-39 · salidas bloqueadas por la lista cerrada de destinos.
    _spec(_N.LOOP_EGRESS_BLOCKED_TOTAL, _C, "{request}",
          "Peticiones salientes bloqueadas por destino fuera de la lista, por componente.",
          "LC-LAZ-39", "egress_component"),
)  # fmt: skip

NFR_LAZ_41: Final[Mapping[str, tuple[LoopMetricName, ...]]] = {
    "consumer_pending_and_oldest_age": (
        _N.LOOP_CONSUMER_PENDING,
        _N.LOOP_CONSUMER_OLDEST_PENDING_AGE_SECONDS,
    ),
    "dead_letter_entries": (_N.LOOP_DEAD_LETTER_ENTRIES_TOTAL,),
    "periodic_task_duration_and_result": (_N.LOOP_PERIODIC_TASK_DURATION_MS,),
    "assistant_requests_by_state_and_latency": (
        _N.ASSISTANT_REQUESTS_TOTAL,
        _N.ASSISTANT_REQUEST_DURATION_MS,
    ),
    "assistant_tokens_by_organization_and_month": (_N.ASSISTANT_TOKENS_TOTAL,),
    "emails_by_state": (_N.EMAIL_MESSAGES_TOTAL,),
    "exports_by_state_duration_and_size": (
        _N.EXPORT_REQUESTS_TOTAL,
        _N.EXPORT_DURATION_MS,
        _N.EXPORT_PACKAGE_SIZE_BYTES,
    ),
    "projection_divergences": (_N.LOOP_PROJECTION_DIVERGENCES_TOTAL,),
    "overdue_by_zone": (_N.LOOP_OVERDUE_FINDINGS,),
    "unclassified_by_zone_and_age": (
        _N.LOOP_UNCLASSIFIED_FINDINGS,
        _N.LOOP_UNCLASSIFIED_OLDEST_AGE_SECONDS,
    ),
    "text_validations_by_verdict": (_N.LOOP_TEXT_VALIDATIONS_TOTAL,),
}
"""Cada partida de NFR-LAZ-41, en el orden del requisito, con las métricas que la cubren."""


@dataclass(frozen=True, slots=True)
class LoopAlarmCondition:
    """Condición de alarma de NFR-LAZ-42 y la métrica que la dispara (como ``AlarmCondition``)."""

    key: str
    metric: LoopMetricName
    selector: Mapping[str, frozenset[str]] = field(default_factory=dict)
    """Atributo → valores que la condición vigila; vacío si vigila toda la métrica."""
    denominator: LoopMetricName | None = None
    """Métrica del denominador cuando la condición es una tasa."""


LOOP_ALARM_CONDITIONS: Final[tuple[LoopAlarmCondition, ...]] = (
    # Una sola entrada en cola muerta de cualquier consumidor de U-04.
    LoopAlarmCondition("loop_dead_letter", _N.LOOP_DEAD_LETTER_ENTRIES_TOTAL),
    # Circuito abierto o solicitudes unavailable de forma continua más de 30 minutos.
    LoopAlarmCondition("assistant_unavailable", _N.ASSISTANT_UNAVAILABLE),
    # Una exportación por encima de 60 minutos o dos fallos seguidos de la misma solicitud.
    LoopAlarmCondition("export_stuck_or_failed", _N.EXPORT_STUCK_OR_FAILED),
    # Tasa de rebotes por encima del 5 % en 24 horas.
    LoopAlarmCondition(
        "email_bounce_rate",
        _N.EMAIL_MESSAGES_TOTAL,
        selector={"email_state": frozenset({"bounced"})},
        denominator=_N.EMAIL_MESSAGES_TOTAL,
    ),
    # Una sola divergencia de proyección.
    LoopAlarmCondition("loop_projection_divergence", _N.LOOP_PROJECTION_DIVERGENCES_TOTAL),
    # Cualquiera de las diez tareas sin ejecutar en 26 horas (las mensuales, con su ventana).
    LoopAlarmCondition("loop_periodic_task_stale", _N.LOOP_PERIODIC_TASK_LAST_SUCCESS_AGE_SECONDS),
    # Consumo mensual de una organización por encima del 80 % del tope de NFR-LAZ-35.
    LoopAlarmCondition("assistant_token_budget", _N.ASSISTANT_TOKEN_BUDGET_USED_RATIO),
    # Margen de límite de tasa del proveedor por debajo del 20 %.
    LoopAlarmCondition("assistant_rate_limit_headroom", _N.ASSISTANT_RATE_LIMIT_HEADROOM_RATIO),
)
"""Las ocho alarmas de NFR-LAZ-42, en el orden de su tabla (todas hacia ``vigia-alerts``)."""

EGRESS_BLOCKED_ALARM: Final = LoopAlarmCondition(
    "loop_egress_blocked", _N.LOOP_EGRESS_BLOCKED_TOTAL
)
"""La alerta de la fábrica de clientes (LC-LAZ-39): una sola salida bloqueada."""

EXPORT_FAILED_ALARM: Final = LoopAlarmCondition("export_failed", _N.EXPORT_FAILED_TOTAL)
"""La mitad «dos fallos seguidos» de ``export_stuck_or_failed``: el contador que ``export_worker``
emite al ocurrir (hecho único de PAT-LAZ-MAN-01), fuera de las ocho condiciones."""

TRANSVERSAL_METRICS: Final[frozenset[LoopMetricName]] = frozenset(
    {
        _N.LOOP_BULKHEAD_IN_USE,
        _N.LOOP_BULKHEAD_SIZE,
        _N.LOOP_BULKHEAD_SATURATED_TOTAL,
        _N.LOOP_EGRESS_BLOCKED_TOTAL,
    }
)
"""Las métricas de los mamparos y de la fábrica de clientes, fuera de NFR-LAZ-41 y 42."""


# --- Atributos de traza del trabajo largo (NFR-LAZ-43) -------------------------------------------

LONG_JOB_ATTRIBUTE_KEYS: Final = frozenset(
    {
        "correlation_id",
        "export_request_id",
        "assistant_request_id",
        "artifact",
        "step",
        "model",
        "effort",
        "input_tokens",
        "output_tokens",
        "stop_reason",
    }
)
"""Los únicos atributos de los tramos del asistente y de la exportación: identificadores (UUID),
enumeraciones cerradas y cifras acotadas. Ni texto libre, ni contexto, ni usuario."""


def long_job_attributes(
    attributes: Mapping[str, object],
    *,
    policy: redaction.AttributePolicy | None = None,
) -> dict[str, redaction.AttributeValue]:
    """Los atributos de ``attributes`` que un tramo del trabajo largo puede llevar, ya limpios.

    Un nombre fuera de ``LONG_JOB_ATTRIBUTE_KEYS`` se descarta; un valor que no es un UUID, un
    valor registrado de su lista o una cifra en su rango sale como ``other`` (o se descarta, si es
    una cifra): un texto libre, un correo, un enlace o un identificador de usuario nunca sale.
    """
    chosen = policy if policy is not None else redaction.DEFAULT_POLICY
    return chosen.clean(attributes, allowed=LONG_JOB_ATTRIBUTE_KEYS)


def register_long_job_values(
    *,
    models: Iterable[str] = (),
    artifacts: Iterable[str] = (),
    steps: Iterable[str] = (),
    policy: redaction.AttributePolicy | None = None,
) -> None:
    """Amplía al arrancar las listas cerradas de modelo, artefacto y paso (constantes del código
    o de la configuración, nunca datos de una petición)."""
    chosen = policy if policy is not None else redaction.DEFAULT_POLICY
    chosen.register("model", models)
    chosen.register("artifact", artifacts)
    chosen.register("step", steps)


# --- Instrumentos --------------------------------------------------------------------------------


class _LoopInstrument:
    def __init__(self, spec: LoopMetricSpec, policy: redaction.AttributePolicy | None) -> None:
        self.spec = spec
        self._policy = policy

    def _attributes(
        self, attributes: Mapping[str, object] | None
    ) -> dict[str, redaction.AttributeValue]:
        policy = self._policy if self._policy is not None else redaction.DEFAULT_POLICY
        return policy.clean(attributes, allowed=self.spec.attributes)


class LoopCounter(_LoopInstrument):
    def __init__(
        self,
        spec: LoopMetricSpec,
        meter: otel_metrics.Meter,
        policy: redaction.AttributePolicy | None,
    ) -> None:
        super().__init__(spec, policy)
        self._otel = meter.create_counter(spec.name, unit=spec.unit, description=spec.description)

    def add(self, amount: int | float = 1, attributes: Mapping[str, object] | None = None) -> None:
        self._otel.add(amount, self._attributes(attributes))


class LoopHistogram(_LoopInstrument):
    def __init__(
        self,
        spec: LoopMetricSpec,
        meter: otel_metrics.Meter,
        policy: redaction.AttributePolicy | None,
    ) -> None:
        super().__init__(spec, policy)
        self._otel = meter.create_histogram(spec.name, unit=spec.unit, description=spec.description)

    def record(self, value: int | float, attributes: Mapping[str, object] | None = None) -> None:
        self._otel.record(value, self._attributes(attributes))


class LoopGauge(_LoopInstrument):
    def __init__(
        self,
        spec: LoopMetricSpec,
        meter: otel_metrics.Meter,
        policy: redaction.AttributePolicy | None,
    ) -> None:
        super().__init__(spec, policy)
        self._otel = meter.create_gauge(spec.name, unit=spec.unit, description=spec.description)

    def set(self, value: int | float, attributes: Mapping[str, object] | None = None) -> None:
        self._otel.set(value, self._attributes(attributes))


_KIND_OF: Final[Mapping[MetricKind, type[LoopCounter | LoopHistogram | LoopGauge]]] = {
    MetricKind.COUNTER: LoopCounter,
    MetricKind.HISTOGRAM: LoopHistogram,
    MetricKind.GAUGE: LoopGauge,
}


class LoopMetrics:
    """Un instrumento por métrica de ``LOOP_CATALOG`` (``metrics.instrument(nombre)``).

    Los más usados tienen además su atributo con el nombre publicado.
    """

    def __init__(
        self, meter: otel_metrics.Meter, policy: redaction.AttributePolicy | None = None
    ) -> None:
        self._instruments: dict[LoopMetricName, LoopCounter | LoopHistogram | LoopGauge] = {}
        for spec in LOOP_CATALOG:
            kind = _KIND_OF[spec.kind]
            self._instruments[spec.name] = kind(spec, meter, policy)
        self.loop_bulkhead_in_use = self.gauge(_N.LOOP_BULKHEAD_IN_USE)
        self.loop_bulkhead_size = self.gauge(_N.LOOP_BULKHEAD_SIZE)
        self.loop_bulkhead_saturated_total = self.counter(_N.LOOP_BULKHEAD_SATURATED_TOTAL)
        self.loop_egress_blocked_total = self.counter(_N.LOOP_EGRESS_BLOCKED_TOTAL)

    def instrument(self, name: LoopMetricName) -> LoopCounter | LoopHistogram | LoopGauge:
        """El instrumento de ``name``."""
        return self._instruments[LoopMetricName(name)]

    def counter(self, name: LoopMetricName) -> LoopCounter:
        instrument = self.instrument(name)
        if not isinstance(instrument, LoopCounter):
            raise LookupError(f"{name} no es un contador")
        return instrument

    def histogram(self, name: LoopMetricName) -> LoopHistogram:
        instrument = self.instrument(name)
        if not isinstance(instrument, LoopHistogram):
            raise LookupError(f"{name} no es un histograma")
        return instrument

    def gauge(self, name: LoopMetricName) -> LoopGauge:
        instrument = self.instrument(name)
        if not isinstance(instrument, LoopGauge):
            raise LookupError(f"{name} no es un medidor")
        return instrument


@functools.cache
def get_loop_metrics() -> LoopMetrics:
    """Métricas de U-04 sobre el proveedor global de OpenTelemetry (el medidor de U-02)."""
    return LoopMetrics(otel_metrics.get_meter(METER_NAME))
