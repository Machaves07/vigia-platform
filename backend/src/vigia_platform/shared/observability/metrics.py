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
    "Operation",
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
    RATE_LIMITED_TOTAL = "rate_limited_total"
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
    OUTBOX_DELIVERIES_TOTAL = "outbox_deliveries_total"
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
    DB_POOL_RECONNECTS_TOTAL = "db_pool_reconnects_total"
    CPU_POOL_WAIT_MS = "cpu_pool_wait_ms"
    # Mamparos por clase de ruta (U-03, NFR-GOB-19)
    BULKHEAD_IN_USE = "bulkhead_in_use"
    BULKHEAD_SIZE = "bulkhead_size"
    BULKHEAD_WAIT_MS = "bulkhead_wait_ms"
    BULKHEAD_REJECTED_TOTAL = "bulkhead_rejected_total"
    # Rutas del contrato (U-03, NFR-GOB-54)
    NODE_REQUESTS_TOTAL = "node_requests_total"
    NODE_REQUEST_BODY_BYTES = "node_request_body_bytes"
    # Concesiones de clip por nodo (U-03, NFR-GOB-55)
    CLIP_GRANTS_ISSUED_TOTAL = "clip_grants_issued_total"
    CLIP_GRANTS_USED_TOTAL = "clip_grants_used_total"
    CLIP_GRANTS_ORPHANED_TOTAL = "clip_grants_orphaned_total"
    # Flota (U-03, TASK-218): intentos de alta y revocaciones
    ENROLLMENT_ATTEMPTS_TOTAL = "enrollment_attempts_total"
    NODE_REVOCATIONS_TOTAL = "node_revocations_total"
    # Credenciales de nodo (U-03, TASK-219): rotaciones y duración de kms:Sign de vigia-node-ca
    NODE_CREDENTIAL_ROTATIONS_TOTAL = "node_credential_rotations_total"
    NODE_CA_SIGN_DURATION_MS = "node_ca_sign_duration_ms"
    # Lista de revocación global de vigia-node-ca (U-03, TASK-220, NFR-GOB-46 y 48)
    REVOCATION_LIST_PUBLISH_FAILED = "revocation_list_publish_failed"
    REVOCATION_LIST_SECONDS_TO_EXPIRY = "revocation_list_seconds_to_expiry"
    REVOCATION_LIST_ENTRIES = "revocation_list_entries"
    # Flota (U-03, TASK-223, NFR-GOB-55): por nodo, solo contadores y medidores (NFR-GOB-13)
    FLEET_HEARTBEATS_TOTAL = "fleet_heartbeats_total"
    FLEET_HEARTBEAT_GAP_SECONDS = "fleet_heartbeat_gap_seconds"
    FLEET_NODE_REACHABLE = "fleet_node_reachable"
    FLEET_NODE_QUEUE_PENDING = "fleet_node_queue_pending"
    FLEET_NODE_CLOCK_OFFSET_MS = "fleet_node_clock_offset_ms"
    SIGNING_KEY_DAYS_TO_EXPIRY = "signing_key_days_to_expiry"
    RESTORE_DRILL_AGE_DAYS = "restore_drill_age_days"
    SECRETS_REFRESH_FAILED = "secrets_refresh_failed"
    OTEL_DROPPED_TOTAL = "otel_dropped_total"
    # Salud del proceso
    HEALTH_READY = "health_ready"


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
    _spec(_N.RATE_LIMITED_TOTAL, _C, "{request}", "Respuestas rate_limited por ruta y límite.",
          "PAT-NUC-ESC-03", "route", "rate_limit"),
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
    _spec(_N.OUTBOX_DELIVERIES_TOTAL, _C, "{delivery}", "Entregas por consumidor y resultado.",
          "outbox", "consumer", "result"),
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
    _spec(_N.DB_POOL_RECONNECTS_TOTAL, _C, "{connection}",
          "Reconexiones tras releer la credencial rotada de la base.", "runbook 6.6",
          "pool_class"),
    _spec(_N.CPU_POOL_WAIT_MS, _H, "ms", "Espera en cola del pool de hilos de CPU.",
          "PAT-NUC-REN-05"),
    # Mamparos por clase de ruta (LC-GOB-20): por pool_class, nunca por nodo; alimentan la alarma
    # bulkhead-person-saturated (§8.1 de U-03).
    _spec(_N.BULKHEAD_IN_USE, _G, "{request}", "Peticiones en curso dentro del mamparo.",
          "NFR-GOB-19", "pool_class"),
    _spec(_N.BULKHEAD_SIZE, _G, "{request}", "Puestos del mamparo por trabajador.", "NFR-GOB-19",
          "pool_class"),
    _spec(_N.BULKHEAD_WAIT_MS, _H, "ms", "Espera hasta obtener puesto o ser rechazada.",
          "NFR-GOB-19", "pool_class"),
    _spec(_N.BULKHEAD_REJECTED_TOTAL, _C, "{request}",
          "Rechazos temporarily_unavailable del mamparo.", "NFR-GOB-19", "pool_class"),
    # Rutas del contrato (NFR-GOB-54): aceptados, duplicados y rechazos por rejection_code; la
    # latencia por ruta es http_server_duration_ms y la tasa, rate_limited_total con su causa.
    _spec(_N.NODE_REQUESTS_TOTAL, _C, "{request}",
          "Peticiones de nodo por ruta, resultado y rejection_code.", "NFR-GOB-54",
          "route", "result", "rejection_code"),
    _spec(_N.NODE_REQUEST_BODY_BYTES, _H, "By", "Tamaño del cuerpo recibido por ruta del contrato.",
          "NFR-GOB-54", "route"),
    # Concesiones de clip (NFR-GOB-55, LC-GOB-13): emitidas, usadas y huérfanas por nodo. Solo
    # contadores y solo node_id (NFR-GOB-13: sin histogramas por nodo ni etiqueta de zona).
    _spec(_N.CLIP_GRANTS_ISSUED_TOTAL, _C, "{grant}", "Concesiones de clip emitidas por nodo.",
          "NFR-GOB-55", "node_id"),
    _spec(_N.CLIP_GRANTS_USED_TOTAL, _C, "{grant}",
          "Concesiones de clip usadas (clip de verificación confirmado) por nodo.", "NFR-GOB-55",
          "node_id"),
    _spec(_N.CLIP_GRANTS_ORPHANED_TOTAL, _C, "{grant}",
          "Clips sin registro que los cite en 24 h, contados una vez por nodo.", "NFR-GOB-55",
          "node_id"),
    # Flota (TASK-218): todo intento de alta, también el de un nodo desconocido, que no deja fila
    # (reason = node_unknown); y la marca por organización de la lista de revocación (D-7: solo
    # métrica).
    _spec(_N.ENROLLMENT_ATTEMPTS_TOTAL, _C, "{attempt}",
          "Intentos de alta por resultado y si el nodo estaba declarado.", "BR-GOB-61",
          "result", "reason"),
    _spec(_N.NODE_REVOCATIONS_TOTAL, _C, "{revocation}",
          "Revocaciones de nodo (marca de la lista de revocación) por organización.", "D-7",
          "organization_id"),
    # Credenciales (TASK-219): sin el PEM, el código ni la huella (NFR-GOB-25); las altas
    # aceptadas y rechazadas las cuenta enrollment_attempts_total por resultado.
    _spec(_N.NODE_CREDENTIAL_ROTATIONS_TOTAL, _C, "{rotation}",
          "Rotaciones de credencial de nodo confirmadas.", "BR-GOB-64"),
    _spec(_N.NODE_CA_SIGN_DURATION_MS, _H, "ms",
          "Duración de cada kms:Sign de vigia-node-ca por resultado.", "NFR-GOB-43", "result"),
    # Lista de revocación global (TASK-220): sin atributos (ni número de serie, ni ARN, ni PEM;
    # NFR-GOB-13, 25). La alarma revocation-list-publish-failed (VIG-167) vigila el contador:
    # un ciclo fallido, o una lista vigente a menos de 24 h de vencer, suma uno.
    _spec(_N.REVOCATION_LIST_PUBLISH_FAILED, _C, "{cycle}",
          "Ciclos de la lista de revocación fallidos o con la lista a menos de 24 h de vencer.",
          "NFR-GOB-46"),
    _spec(_N.REVOCATION_LIST_SECONDS_TO_EXPIRY, _G, "s",
          "Segundos hasta next_update de la lista de revocación vigente.", "NFR-GOB-48"),
    _spec(_N.REVOCATION_LIST_ENTRIES, _G, "{certificate}",
          "Entradas de la lista de revocación vigente (revocadas y sustituidas no vencidas).",
          "NFR-GOB-14"),
    # Latido (TASK-223, NFR-GOB-55): seis series por nodo (aceptados, ignorados y cuatro
    # medidores), nunca histogramas por nodo ni etiquetas de zona (NFR-GOB-13).
    _spec(_N.FLEET_HEARTBEATS_TOTAL, _C, "{heartbeat}",
          "Latidos por nodo: aceptados o ignorados por heartbeat_id repetido.", "NFR-GOB-55",
          "node_id", "result"),
    _spec(_N.FLEET_HEARTBEAT_GAP_SECONDS, _G, "s",
          "Hueco entre el latido aceptado y el anterior del mismo nodo.", "NFR-GOB-55", "node_id"),
    _spec(_N.FLEET_NODE_REACHABLE, _G, "1",
          "Estado de comunicación del nodo: reachable (1) o no (0).", "NFR-GOB-55", "node_id"),
    _spec(_N.FLEET_NODE_QUEUE_PENDING, _G, "{record}",
          "Registros pendientes en la cola local del nodo.", "NFR-GOB-55", "node_id"),
    _spec(_N.FLEET_NODE_CLOCK_OFFSET_MS, _G, "ms",
          "Desviación del reloj del nodo respecto de su fuente de tiempo.", "NFR-GOB-55",
          "node_id"),
    _spec(_N.SIGNING_KEY_DAYS_TO_EXPIRY, _G, "d", "Días hasta el vencimiento de la clave.",
          "NFR-NUC-38", "purpose"),
    # Alarma restore-drill-overdue (> 100 días; infrastructure-design §9.4, nota U02-H-14).
    _spec(_N.RESTORE_DRILL_AGE_DAYS, _G, "d",
          "Días desde el último ensayo de restauración correcto.", "RESILIENCY-12"),
    _spec(_N.SECRETS_REFRESH_FAILED, _C, "{refresh}",
          "Relecturas fallidas del gestor de secretos o de KMS.", "PAT-NUC-RES-03", "dependency"),
    _spec(_N.OTEL_DROPPED_TOTAL, _C, "{item}", "Tramos y métricas descartados.",
          "PAT-NUC-RES-03", "signal"),
    # Salud de vigia-api con la versión desplegada (LC-NUC-31, pendiente nº 12 de U-05).
    _spec(_N.HEALTH_READY, _G, "1", "Salud profunda: lista (1) o no (0), por versión desplegada.",
          "LC-NUC-31", "app_version"),
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


class Operation(enum.StrEnum):
    """Valores del atributo ``operation`` de ``operation_duration_ms`` (NFR-NUC-01)."""

    LEDGER_WRITE = "ledger_write"
    LEDGER_WRITE_WITH_EVIDENCE = "ledger_write_with_evidence"
    LEDGER_LIST = "ledger_list"
    LEDGER_TIMELINE = "ledger_timeline"
    LOGIN = "login"
    LIVE_VIEW_TOKEN = "live_view_token"  # noqa: S105 - nombre de operación, no un secreto
    OUTBOX_DELIVERY = "outbox_delivery"


OPERATION_P95_TARGET_MS: Final[Mapping[str, int]] = {
    Operation.LEDGER_WRITE.value: 150,
    Operation.LEDGER_WRITE_WITH_EVIDENCE.value: 400,
    Operation.LEDGER_LIST.value: 300,
    Operation.LEDGER_TIMELINE.value: 1_000,
    Operation.LOGIN.value: 700,
    Operation.LIVE_VIEW_TOKEN.value: 100,
    Operation.OUTBOX_DELIVERY.value: 5_000,
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
        self.rate_limited_total = counter(_N.RATE_LIMITED_TOTAL)
        self.ledger_writes_total = counter(_N.LEDGER_WRITES_TOTAL)
        self.ledger_record_size_bytes = histogram(_N.LEDGER_RECORD_SIZE_BYTES)
        self.chain_lock_wait_ms = histogram(_N.CHAIN_LOCK_WAIT_MS)
        self.chain_locked_timeout_total = counter(_N.CHAIN_LOCKED_TIMEOUT_TOTAL)
        self.integrity_compromised_total = counter(_N.INTEGRITY_COMPROMISED_TOTAL)
        self.default_partition_rows = gauge(_N.DEFAULT_PARTITION_ROWS)
        self.audit_entries_total = counter(_N.AUDIT_ENTRIES_TOTAL)
        self.outbox_pending = gauge(_N.OUTBOX_PENDING)
        self.outbox_deliveries_total = counter(_N.OUTBOX_DELIVERIES_TOTAL)
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
        self.db_pool_reconnects_total = counter(_N.DB_POOL_RECONNECTS_TOTAL)
        self.cpu_pool_wait_ms = histogram(_N.CPU_POOL_WAIT_MS)
        self.bulkhead_in_use = gauge(_N.BULKHEAD_IN_USE)
        self.bulkhead_size = gauge(_N.BULKHEAD_SIZE)
        self.bulkhead_wait_ms = histogram(_N.BULKHEAD_WAIT_MS)
        self.bulkhead_rejected_total = counter(_N.BULKHEAD_REJECTED_TOTAL)
        self.node_requests_total = counter(_N.NODE_REQUESTS_TOTAL)
        self.node_request_body_bytes = histogram(_N.NODE_REQUEST_BODY_BYTES)
        self.clip_grants_issued_total = counter(_N.CLIP_GRANTS_ISSUED_TOTAL)
        self.clip_grants_used_total = counter(_N.CLIP_GRANTS_USED_TOTAL)
        self.clip_grants_orphaned_total = counter(_N.CLIP_GRANTS_ORPHANED_TOTAL)
        self.enrollment_attempts_total = counter(_N.ENROLLMENT_ATTEMPTS_TOTAL)
        self.node_revocations_total = counter(_N.NODE_REVOCATIONS_TOTAL)
        self.node_credential_rotations_total = counter(_N.NODE_CREDENTIAL_ROTATIONS_TOTAL)
        self.node_ca_sign_duration_ms = histogram(_N.NODE_CA_SIGN_DURATION_MS)
        self.revocation_list_publish_failed = counter(_N.REVOCATION_LIST_PUBLISH_FAILED)
        self.revocation_list_seconds_to_expiry = gauge(_N.REVOCATION_LIST_SECONDS_TO_EXPIRY)
        self.revocation_list_entries = gauge(_N.REVOCATION_LIST_ENTRIES)
        self.fleet_heartbeats_total = counter(_N.FLEET_HEARTBEATS_TOTAL)
        self.fleet_heartbeat_gap_seconds = gauge(_N.FLEET_HEARTBEAT_GAP_SECONDS)
        self.fleet_node_reachable = gauge(_N.FLEET_NODE_REACHABLE)
        self.fleet_node_queue_pending = gauge(_N.FLEET_NODE_QUEUE_PENDING)
        self.fleet_node_clock_offset_ms = gauge(_N.FLEET_NODE_CLOCK_OFFSET_MS)
        self.signing_key_days_to_expiry = gauge(_N.SIGNING_KEY_DAYS_TO_EXPIRY)
        self.restore_drill_age_days = gauge(_N.RESTORE_DRILL_AGE_DAYS)
        self.secrets_refresh_failed = counter(_N.SECRETS_REFRESH_FAILED)
        self.otel_dropped_total = counter(_N.OTEL_DROPPED_TOTAL)
        self.health_ready = gauge(_N.HEALTH_READY)

    def record_operation(self, operation: Operation, elapsed_seconds: float) -> None:
        """``operation_duration_ms`` de ``operation``: ``elapsed_seconds`` medidos con ``Clock``.

        Lo llama cada operación de NFR-NUC-01 al terminar con resultado; alimenta las alarmas
        ``latency-<operación>`` (p95, NFR-NUC-38). Una excepción no se mide: la cuentan los
        errores. Un intervalo negativo (relojes de dos instancias) cuenta como 0.
        """
        elapsed_ms = max(0.0, elapsed_seconds * 1000)
        self.operation_duration_ms.record(elapsed_ms, {"operation": Operation(operation).value})

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
