"""Métricas con nombres fijos (NFR-NUC-42) y una métrica por condición de alarma (NFR-NUC-38).

Infrastructure Design define el tablero y las alarmas sobre estos nombres (PAT-NUC-MAN-02,
``infrastructure-design.md`` §9.3 y §9.4): un nombre publicado no se renombra.

- ``CATALOG``: cada métrica con su tipo, unidad, grupo de NFR-NUC-42 y los **únicos** atributos
  que admite. ``PlatformMetrics`` expone un instrumento por entrada (``metrics.<nombre>``).
- ``ALARM_CONDITIONS``: cada condición de alarma de aplicación de NFR-NUC-38 con la métrica que
  la alimenta; las de infraestructura (una sola zona, replicación, copias, cuotas) salen de los
  servicios de AWS y no tienen métrica de aplicación.
- Los instrumentos limpian sus atributos con ``redaction.AttributePolicy`` antes de registrar:
  solo identificadores y enumeraciones de la lista de la métrica (NFR-NUC-41); nada de texto.

Uso: ``metrics = get_metrics()`` y ``metrics.ledger_writes_total.add(1, {"result": ...})``.
Sin proveedor global instalado, los instrumentos son los de la API de OpenTelemetry y no hacen
nada; ``tracing.configure_telemetry`` crea el proveedor con exportación OTLP.
"""

from __future__ import annotations

import enum
import functools
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from opentelemetry import metrics as otel_metrics

from vigia_platform.shared.observability import redaction

__all__ = [
    "ALARM_CONDITIONS",
    "CATALOG",
    "METER_NAME",
    "OPERATION_P95_TARGET_MS",
    "AlarmCondition",
    "Counter",
    "Gauge",
    "Histogram",
    "MetricKind",
    "MetricName",
    "MetricSpec",
    "PlatformMetrics",
    "get_metrics",
]

METER_NAME: Final = "vigia_platform"


class MetricKind(enum.StrEnum):
    COUNTER = "counter"
    HISTOGRAM = "histogram"
    GAUGE = "gauge"


class MetricName(enum.StrEnum):
    """Nombres fijos publicados hacia el colector (espacio ``Vigia/Platform``)."""

    # Por ruta
    HTTP_SERVER_REQUESTS_TOTAL = "http_server_requests_total"
    HTTP_SERVER_ERRORS_TOTAL = "http_server_errors_total"
    HTTP_SERVER_DURATION_MS = "http_server_duration_ms"
    OPERATION_DURATION_MS = "operation_duration_ms"
    # Expediente
    LEDGER_WRITES_TOTAL = "ledger_writes_total"
    LEDGER_RECORD_SIZE_BYTES = "ledger_record_size_bytes"
    CHAIN_LOCK_WAIT_MS = "chain_lock_wait_ms"
    CHAIN_LOCKED_TIMEOUT_TOTAL = "chain_locked_timeout_total"
    INTEGRITY_COMPROMISED_TOTAL = "integrity_compromised_total"
    DEFAULT_PARTITION_ROWS = "default_partition_rows"
    # Auditoría
    AUDIT_ENTRIES_TOTAL = "audit_entries_total"
    # Bandeja de salida
    OUTBOX_PENDING = "outbox_pending"
    OUTBOX_RETRIES_TOTAL = "outbox_retries_total"
    DEAD_LETTER_CREATED_TOTAL = "dead_letter_created_total"
    OUTBOX_CIRCUIT_OPEN = "outbox_circuit_open"
    OUTBOX_OLDEST_PENDING_AGE_SECONDS = "outbox_oldest_pending_age_seconds"
    # Identidad
    AUTH_LOGINS_TOTAL = "auth_logins_total"
    AUTH_FAILURES_TOTAL = "auth_failures_total"
    AUTH_THROTTLE_DELAY_MS = "auth_throttle_delay_ms"
    SESSIONS_ACTIVE = "sessions_active"
    LIVE_VIEW_TOKENS_ISSUED_TOTAL = "live_view_tokens_issued_total"
    HIBP_FALLBACK_USED = "hibp_fallback_used"
    SECURITY_ALERT_TOTAL = "security_alert_total"
    # Evidencias
    EVIDENCE_VERIFICATIONS_TOTAL = "evidence_verifications_total"
    EVIDENCE_SAMPLE_CHECKED_TOTAL = "evidence_sample_checked_total"
    # Verificación de cadenas
    CHAIN_VERIFICATION_RECORDS_PER_SECOND = "chain_verification_records_per_second"
    CHAIN_VERIFICATION_RESULTS_TOTAL = "chain_verification_results_total"
    # Tareas periódicas
    PERIODIC_TASK_DURATION_MS = "periodic_task_duration_ms"
    PERIODIC_TASK_LAST_SUCCESS_AGE_SECONDS = "periodic_task_last_success_age_seconds"
    # Recursos del proceso
    DB_POOL_IN_USE = "db_pool_in_use"
    DB_POOL_SIZE = "db_pool_size"
    CPU_POOL_WAIT_MS = "cpu_pool_wait_ms"
    SIGNING_KEY_DAYS_TO_EXPIRY = "signing_key_days_to_expiry"
    SECRETS_REFRESH_FAILED = "secrets_refresh_failed"
    OTEL_DROPPED_TOTAL = "otel_dropped_total"


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """Definición publicada de una métrica."""

    name: MetricName
    kind: MetricKind
    unit: str
    description: str
    group: str
    """Grupo de NFR-NUC-42 o requisito que la origina."""
    attributes: frozenset[str] = frozenset()
    """Únicos atributos admitidos (identificadores y enumeraciones de baja cardinalidad)."""


def _spec(
    name: MetricName,
    kind: MetricKind,
    unit: str,
    description: str,
    group: str,
    *attributes: str,
) -> MetricSpec:
    return MetricSpec(name, kind, unit, description, group, frozenset(attributes))


_C, _H, _G = MetricKind.COUNTER, MetricKind.HISTOGRAM, MetricKind.GAUGE
_N = MetricName

CATALOG: Final[tuple[MetricSpec, ...]] = (
    # NFR-NUC-42 · por ruta: latencia, errores por código y caudal.
    _spec(_N.HTTP_SERVER_REQUESTS_TOTAL, _C, "{request}", "Peticiones atendidas.", "route",
          "route", "method", "status_class"),
    _spec(_N.HTTP_SERVER_ERRORS_TOTAL, _C, "{request}", "Respuestas de error por código.", "route",
          "route", "status_class", "code"),
    _spec(_N.HTTP_SERVER_DURATION_MS, _H, "ms", "Latencia por ruta.", "route", "route", "method"),
    _spec(_N.OPERATION_DURATION_MS, _H, "ms", "Latencia de las operaciones de NFR-NUC-01.",
          "NFR-NUC-01", "operation"),
    # NFR-NUC-42 · expediente.
    _spec(_N.LEDGER_WRITES_TOTAL, _C, "{record}", "Escrituras por tipo y resultado.", "ledger",
          "record_type", "result", "chain_kind"),
    _spec(_N.LEDGER_RECORD_SIZE_BYTES, _H, "By", "Tamaño canónico del registro.", "ledger",
          "record_type"),
    _spec(_N.CHAIN_LOCK_WAIT_MS, _H, "ms", "Tiempo de exclusión de cadena.", "ledger",
          "chain_kind"),
    _spec(_N.CHAIN_LOCKED_TIMEOUT_TOTAL, _C, "{write}", "Escrituras con chain_locked_timeout.",
          "ledger", "chain_kind"),
    _spec(_N.INTEGRITY_COMPROMISED_TOTAL, _C, "{event}", "Cadenas con integrity_compromised.",
          "ledger", "chain_kind", "organization_id"),
    _spec(_N.DEFAULT_PARTITION_ROWS, _G, "{row}", "Filas en la partición por defecto.",
          "PAT-NUC-ESC-01", "table"),
    # NFR-NUC-42 · auditoría.
    _spec(_N.AUDIT_ENTRIES_TOTAL, _C, "{entry}", "Entradas de auditoría por operación.", "audit",
          "audit_operation"),
    # NFR-NUC-42 · bandeja de salida.
    _spec(_N.OUTBOX_PENDING, _G, "{event}", "Pendientes por consumidor y partición.", "outbox",
          "consumer", "partition"),
    _spec(_N.OUTBOX_RETRIES_TOTAL, _C, "{attempt}", "Reintentos de entrega.", "outbox",
          "consumer"),
    _spec(_N.DEAD_LETTER_CREATED_TOTAL, _C, "{event}", "Eventos enviados a la cola muerta.",
          "outbox", "consumer", "event_type"),
    _spec(_N.OUTBOX_CIRCUIT_OPEN, _G, "1", "Circuito del consumidor abierto (1) o no (0).",
          "outbox", "consumer"),
    _spec(_N.OUTBOX_OLDEST_PENDING_AGE_SECONDS, _G, "s", "Antigüedad del evento más viejo.",
          "outbox", "consumer"),
    # NFR-NUC-42 · identidad.
    _spec(_N.AUTH_LOGINS_TOTAL, _C, "{login}", "Inicios de sesión por resultado.", "identity",
          "result"),
    _spec(_N.AUTH_FAILURES_TOTAL, _C, "{failure}", "Fallos de autenticación por motivo.",
          "identity", "reason"),
    _spec(_N.AUTH_THROTTLE_DELAY_MS, _H, "ms", "Retardo aplicado por fallos.", "identity"),
    _spec(_N.SESSIONS_ACTIVE, _G, "{session}", "Sesiones activas.", "identity"),
    _spec(_N.LIVE_VIEW_TOKENS_ISSUED_TOTAL, _C, "{token}", "Tokens de vista en vivo emitidos.",
          "identity"),
    _spec(_N.HIBP_FALLBACK_USED, _C, "{check}", "Consultas resueltas con el respaldo local.",
          "PAT-NUC-RES-03"),
    _spec(_N.SECURITY_ALERT_TOTAL, _C, "{alert}", "Alertas de seguridad por tipo.",
          "NFR-NUC-28", "alert_type", "organization_id"),
    # NFR-NUC-42 · evidencias.
    _spec(_N.EVIDENCE_VERIFICATIONS_TOTAL, _C, "{verification}", "Verificaciones por resultado.",
          "evidence", "result"),
    _spec(_N.EVIDENCE_SAMPLE_CHECKED_TOTAL, _C, "{clip}", "Clips de la muestra diaria.",
          "evidence", "result"),
    # NFR-NUC-42 · verificación.
    _spec(_N.CHAIN_VERIFICATION_RECORDS_PER_SECOND, _G, "{record}/s", "Registros por segundo.",
          "verification", "chain_kind"),
    _spec(_N.CHAIN_VERIFICATION_RESULTS_TOTAL, _C, "{verification}", "Resultado por cadena.",
          "verification", "chain_kind", "result"),
    # NFR-NUC-42 · tareas periódicas.
    _spec(_N.PERIODIC_TASK_DURATION_MS, _H, "ms", "Duración y resultado por organización.",
          "periodic", "task", "result", "organization_id"),
    _spec(_N.PERIODIC_TASK_LAST_SUCCESS_AGE_SECONDS, _G, "s", "Tiempo desde el último éxito.",
          "periodic", "task"),
    # Recursos y dependencias.
    _spec(_N.DB_POOL_IN_USE, _G, "{connection}", "Conexiones del pool en uso.", "NFR-NUC-38",
          "pool_class"),
    _spec(_N.DB_POOL_SIZE, _G, "{connection}", "Tamaño del pool.", "NFR-NUC-38", "pool_class"),
    _spec(_N.CPU_POOL_WAIT_MS, _H, "ms", "Espera en cola del pool de hilos de CPU.",
          "PAT-NUC-REN-05"),
    _spec(_N.SIGNING_KEY_DAYS_TO_EXPIRY, _G, "d", "Días hasta el vencimiento de la clave.",
          "NFR-NUC-38", "purpose"),
    _spec(_N.SECRETS_REFRESH_FAILED, _C, "{refresh}",
          "Relecturas fallidas del gestor de secretos o de KMS.", "PAT-NUC-RES-03", "dependency"),
    _spec(_N.OTEL_DROPPED_TOTAL, _C, "{item}", "Tramos y métricas descartados.",
          "PAT-NUC-RES-03", "signal"),
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class AlarmCondition:
    """Condición de alarma de NFR-NUC-38 y la métrica que la alimenta."""

    key: str
    metric: MetricName
    selector: Mapping[str, frozenset[str]] = field(default_factory=dict)
    """Atributo → valores que la condición vigila; vacío si vigila toda la métrica."""
    denominator: MetricName | None = None
    """Métrica del denominador cuando la condición es una tasa."""


ALARM_CONDITIONS: Final[tuple[AlarmCondition, ...]] = (
    AlarmCondition("integrity_compromised", _N.INTEGRITY_COMPROMISED_TOTAL),
    AlarmCondition("dead_letter_created", _N.DEAD_LETTER_CREATED_TOTAL),
    AlarmCondition("security_alert", _N.SECURITY_ALERT_TOTAL),
    AlarmCondition(
        "chain_locked_timeout_rate",
        _N.CHAIN_LOCKED_TIMEOUT_TOTAL,
        denominator=_N.LEDGER_WRITES_TOTAL,
    ),
    AlarmCondition("db_pool_saturation", _N.DB_POOL_IN_USE, denominator=_N.DB_POOL_SIZE),
    AlarmCondition(
        "server_error_rate",
        _N.HTTP_SERVER_ERRORS_TOTAL,
        selector={"status_class": frozenset({"5xx"})},
        denominator=_N.HTTP_SERVER_REQUESTS_TOTAL,
    ),
    AlarmCondition("operation_latency_p95", _N.OPERATION_DURATION_MS),
    AlarmCondition(
        "checkpoint_or_verification_task_stale",
        _N.PERIODIC_TASK_LAST_SUCCESS_AGE_SECONDS,
        selector={"task": frozenset({"write_checkpoints", "verify_chains_incremental"})},
    ),
    AlarmCondition(
        "evidence_sample_stale",
        _N.PERIODIC_TASK_LAST_SUCCESS_AGE_SECONDS,
        selector={"task": frozenset({"evidence_sample"})},
    ),
    AlarmCondition("signing_key_rotation_overdue", _N.SIGNING_KEY_DAYS_TO_EXPIRY),
)
"""Condiciones de aplicación de NFR-NUC-38, en el orden del requisito."""

OPERATION_P95_TARGET_MS: Final[Mapping[str, int]] = {
    "ledger_write": 150,
    "ledger_write_with_evidence": 400,
    "ledger_list": 300,
    "ledger_timeline": 1_000,
    "login": 700,
    "live_view_token": 100,
    "outbox_delivery": 5_000,
}
"""Objetivos p95 de NFR-NUC-01 por operación; la alarma salta por encima del doble."""


class _Instrument:
    def __init__(self, spec: MetricSpec, policy: redaction.AttributePolicy | None) -> None:
        self.spec = spec
        self._policy = policy

    def _attributes(
        self, attributes: Mapping[str, object] | None
    ) -> dict[str, redaction.AttributeValue]:
        policy = self._policy if self._policy is not None else redaction.DEFAULT_POLICY
        return policy.clean(attributes, allowed=self.spec.attributes)


class Counter(_Instrument):
    def __init__(
        self, spec: MetricSpec, meter: otel_metrics.Meter, policy: redaction.AttributePolicy | None
    ) -> None:
        super().__init__(spec, policy)
        self._otel = meter.create_counter(spec.name, unit=spec.unit, description=spec.description)

    def add(self, amount: int | float = 1, attributes: Mapping[str, object] | None = None) -> None:
        self._otel.add(amount, self._attributes(attributes))


class Histogram(_Instrument):
    def __init__(
        self, spec: MetricSpec, meter: otel_metrics.Meter, policy: redaction.AttributePolicy | None
    ) -> None:
        super().__init__(spec, policy)
        self._otel = meter.create_histogram(spec.name, unit=spec.unit, description=spec.description)

    def record(self, value: int | float, attributes: Mapping[str, object] | None = None) -> None:
        self._otel.record(value, self._attributes(attributes))


class Gauge(_Instrument):
    def __init__(
        self, spec: MetricSpec, meter: otel_metrics.Meter, policy: redaction.AttributePolicy | None
    ) -> None:
        super().__init__(spec, policy)
        self._otel = meter.create_gauge(spec.name, unit=spec.unit, description=spec.description)

    def set(self, value: int | float, attributes: Mapping[str, object] | None = None) -> None:
        self._otel.set(value, self._attributes(attributes))


_SPECS: Final = {spec.name: spec for spec in CATALOG}


class PlatformMetrics:
    """Un instrumento por métrica de ``CATALOG``; el atributo lleva el nombre publicado."""

    def __init__(
        self, meter: otel_metrics.Meter, policy: redaction.AttributePolicy | None = None
    ) -> None:
        def counter(name: MetricName) -> Counter:
            return Counter(_SPECS[name], meter, policy)

        def histogram(name: MetricName) -> Histogram:
            return Histogram(_SPECS[name], meter, policy)

        def gauge(name: MetricName) -> Gauge:
            return Gauge(_SPECS[name], meter, policy)

        self.http_server_requests_total = counter(_N.HTTP_SERVER_REQUESTS_TOTAL)
        self.http_server_errors_total = counter(_N.HTTP_SERVER_ERRORS_TOTAL)
        self.http_server_duration_ms = histogram(_N.HTTP_SERVER_DURATION_MS)
        self.operation_duration_ms = histogram(_N.OPERATION_DURATION_MS)
        self.ledger_writes_total = counter(_N.LEDGER_WRITES_TOTAL)
        self.ledger_record_size_bytes = histogram(_N.LEDGER_RECORD_SIZE_BYTES)
        self.chain_lock_wait_ms = histogram(_N.CHAIN_LOCK_WAIT_MS)
        self.chain_locked_timeout_total = counter(_N.CHAIN_LOCKED_TIMEOUT_TOTAL)
        self.integrity_compromised_total = counter(_N.INTEGRITY_COMPROMISED_TOTAL)
        self.default_partition_rows = gauge(_N.DEFAULT_PARTITION_ROWS)
        self.audit_entries_total = counter(_N.AUDIT_ENTRIES_TOTAL)
        self.outbox_pending = gauge(_N.OUTBOX_PENDING)
        self.outbox_retries_total = counter(_N.OUTBOX_RETRIES_TOTAL)
        self.dead_letter_created_total = counter(_N.DEAD_LETTER_CREATED_TOTAL)
        self.outbox_circuit_open = gauge(_N.OUTBOX_CIRCUIT_OPEN)
        self.outbox_oldest_pending_age_seconds = gauge(_N.OUTBOX_OLDEST_PENDING_AGE_SECONDS)
        self.auth_logins_total = counter(_N.AUTH_LOGINS_TOTAL)
        self.auth_failures_total = counter(_N.AUTH_FAILURES_TOTAL)
        self.auth_throttle_delay_ms = histogram(_N.AUTH_THROTTLE_DELAY_MS)
        self.sessions_active = gauge(_N.SESSIONS_ACTIVE)
        self.live_view_tokens_issued_total = counter(_N.LIVE_VIEW_TOKENS_ISSUED_TOTAL)
        self.hibp_fallback_used = counter(_N.HIBP_FALLBACK_USED)
        self.security_alert_total = counter(_N.SECURITY_ALERT_TOTAL)
        self.evidence_verifications_total = counter(_N.EVIDENCE_VERIFICATIONS_TOTAL)
        self.evidence_sample_checked_total = counter(_N.EVIDENCE_SAMPLE_CHECKED_TOTAL)
        self.chain_verification_records_per_second = gauge(_N.CHAIN_VERIFICATION_RECORDS_PER_SECOND)
        self.chain_verification_results_total = counter(_N.CHAIN_VERIFICATION_RESULTS_TOTAL)
        self.periodic_task_duration_ms = histogram(_N.PERIODIC_TASK_DURATION_MS)
        self.periodic_task_last_success_age_seconds = gauge(
            _N.PERIODIC_TASK_LAST_SUCCESS_AGE_SECONDS
        )
        self.db_pool_in_use = gauge(_N.DB_POOL_IN_USE)
        self.db_pool_size = gauge(_N.DB_POOL_SIZE)
        self.cpu_pool_wait_ms = histogram(_N.CPU_POOL_WAIT_MS)
        self.signing_key_days_to_expiry = gauge(_N.SIGNING_KEY_DAYS_TO_EXPIRY)
        self.secrets_refresh_failed = counter(_N.SECRETS_REFRESH_FAILED)
        self.otel_dropped_total = counter(_N.OTEL_DROPPED_TOTAL)

    def instrument(self, name: MetricName) -> Counter | Histogram | Gauge:
        """Instrumento de ``name`` (el atributo con el nombre publicado)."""
        instrument = getattr(self, name.value, None)
        if not isinstance(instrument, Counter | Histogram | Gauge):
            raise LookupError(f"{name.value} no tiene instrumento")
        return instrument


@functools.cache
def get_metrics() -> PlatformMetrics:
    """Métricas del proveedor global de OpenTelemetry (se activan al instalarlo)."""
    return PlatformMetrics(otel_metrics.get_meter(METER_NAME))
