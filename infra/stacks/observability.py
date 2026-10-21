"""Pila ``vigia-observability``: grupos de registro de la aplicación, alarmas, tablero y
comprobaciones de Route 53 (§9 y §10). Depende de ``vigia-foundation``, ``vigia-data`` y
``vigia-edge``; ``vigia-compute`` depende de ella. Recursos: TASK-149.

Grupos de registro (§9.2): ``/vigia/<despliegue>/{api,worker,migrate,admin,otel}`` con 180 días y
``vigia-logs``. ``vigia-compute`` los importa por nombre y ``awslogs`` no arranca una tarea si su
grupo no existe, así que esta pila va **antes** que ``vigia-compute`` (nota de VIG-48: el paso 6
del primer despliegue lanza ``vigia-migrate`` y el paso 9 desplegaba esta pila). Por eso no lee
nada de ``vigia-compute``: el clúster y los servicios entran por su nombre fijo. Los grupos de red
(``vigia-foundation``) y del cortafuegos (``vigia-edge``) ya existen con 90 días y ``vigia-logs``.
Ningún rol de aplicación puede borrar grupos ni flujos (NFR-NUC-20, 28).

Alarmas (§9.4, nota U02-H-14, pendientes nº 17 y 18), todas hacia ``vigia-alerts``:

- de aplicación, sobre las métricas de ``observability.metrics`` (espacio ``Vigia/Platform``,
  dimensiones ``service`` y ``environment`` y, donde la alarma separa por atributo, las
  declaraciones de ``otel/collector.yaml``). ``APPLICATION_ALARM_METRICS`` es la tabla literal de
  cada alarma con sus métricas y atributos; la prueba cruzada del backend
  (``tests/unit/test_alarm_metrics_crosscheck.py``) la lee con ``ast`` y la contrasta con el
  catálogo, y ``infra/tests/test_observability.py`` comprueba que coincide con la síntesis;
- de servicio: balanceadores, ECS, RDS, Route 53, traducción de direcciones, cortafuegos,
  cuotas (``AWS/Usage`` con ``SERVICE_QUOTA``, §10) y la regla de EventBridge de copias fallidas.
  Los eventos de conmutación y fallo de RDS los entrega la suscripción de ``vigia-data`` y los
  presupuestos, sus notificaciones en ``vigia-foundation``.

``integrity-compromised`` lleva asunto propio: el correo de una alarma tiene por asunto
``ALARM: "<nombre>"``, así que el nombre ``vigia-integrity-compromised`` es el asunto.

Comprobaciones de Route 53 (§9.3 con la nota U02-H-07 de §4.3, nº 12): ``app.`` por HTTPS sobre
``/health/live``; ``nodes.`` solo por TCP al 443, porque sin certificado de cliente la
autenticación mutua corta la negociación y no hay código HTTP que esperar; ``vigia-app-index``
busca ``app_version`` en ``/version.json`` y no ``vigia-app`` en ``/``: ``vigia-api`` sirve
``index.html`` solo ante navegación (D-3) y Route 53 no envía ``Sec-Fetch-Mode`` (nota de VIG-71).
La disponibilidad de NFR-NUC-10 es un **sondeo interno**: la salud de ``tg-api`` sobre
``/health/ready`` y la tasa de ``5XX`` del balanceador, en el tablero y en la alarma
``availability-internal-probe``.

Tablero ``vigia-<despliegue>`` (``vigia-pilot``, §9.3) con los paneles de NFR-NUC-44, el de la
aplicación (nº 12), los de flota de U-03 (nº 18, ``FLEET_PANELS`` sobre métricas publicadas) y los
cinco de U-04 (nº 23, ``LOOP_PANELS``, TASK-332), solo sobre las métricas que el código emite
(``LOOP_PANEL_METRICS``, A-64); las alarmas de U-04 viven en ``vigia-loop``. Una métrica que una
alarma vigila y su unidad aún no publica va en ``PENDING_UNIT_METRICS``: la alarma queda en
``INSUFFICIENT_DATA`` hasta que la unidad la emita con ese nombre. U-03 ya publica todas las suyas
(VIG-167).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from aws_cdk import Duration, Fn, RemovalPolicy
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cloudwatch_actions
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_events as events
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_sns as sns
from constructs import Construct

from config import EnvironmentConfig, NodesTlsMode
from stacks.base import VigiaStack
from stacks.compute import (
    API_SERVICE,
    METRICS_NAMESPACE,
    WORKER_SERVICE,
    cluster_name,
    log_group_name,
)
from stacks.data import (
    DB_ALLOCATED_STORAGE_GB,
    EVIDENCE_REQUEST_METRICS,
    DataStack,
    app_host,
    db_identifier,
)
from stacks.edge import LIVE_PATH, EdgeStack, nodes_host
from stacks.foundation import (
    PUBLIC_SUBNETS,
    ZONE_LETTERS,
    ZONES,
    FoundationStack,
    KeyName,
)
from stacks.loop import (
    CONFIGURATION_SET_DIMENSION,
    EXPORT_CONSUMER,
    configuration_set_name,
    email_events_dlq_name,
    email_events_name,
)

# --- Registros (§9.2) -----------------------------------------------------------------------

LOG_PROCESSES: Final = ("api", "worker", "migrate", "admin", "otel")
LOG_RETENTION = logs.RetentionDays.SIX_MONTHS  # 180 días [objetivo propio, ≥ 90]

# --- Tablas literales de las alarmas de aplicación ------------------------------------------
# La prueba cruzada del backend las lee con ``ast.literal_eval``: solo literales.

OPERATION_P95_TARGETS_MS: Final = {
    "ledger_write": 150,
    "ledger_write_with_evidence": 400,
    "ledger_list": 300,
    "ledger_timeline": 1_000,
    "login": 700,
    "live_view_token": 100,
    "outbox_delivery": 5_000,
}
"""Objetivos p95 de NFR-NUC-01; ``latency-<operación>`` salta por encima del doble."""

VERIFICATION_TARGET_RECORDS_PER_SECOND: Final = 5_000
"""Octava fila de NFR-NUC-01: verificación incremental ≥ 5 000 registros por segundo; la
alarma salta por debajo de la mitad (el doble del tiempo por registro)."""

PERIODIC_TASK_MAX_AGE_SECONDS: Final = {
    # Núcleo (§9.4).
    "write_checkpoints": 93_600,  # 26 h
    "verify_chains_incremental": 93_600,
    "evidence_sample": 93_600,
    "create_partitions": 691_200,  # 8 días
    "archive_audit_partitions": 3_024_000,  # 35 días
    # Flota de U-03 (nº 18, LC-GOB-18): tres cadencias.
    "detect_mute_nodes": 180,
    "evaluate_fleet_alarms": 180,
    "expire_enrollment_codes": 180,
    "regenerate_revocation_list": 180,
    "mark_orphan_clips": 10_800,  # 3 h
    "expire_walk_test_sessions": 259_200,  # 72 h
    "alert_expiring_certificates": 259_200,
}
"""``periodic_task_last_success_age_seconds`` por tarea: una alarma ``periodic-task-stale-*``."""

RESTORE_DRILL_OVERDUE_DAYS: Final = 100
"""``restore-drill-overdue`` (nota U02-H-14): más de 100 días sin ensayo correcto."""

NODE_ROUTE_P95_TARGETS_MS: Final = {
    "/api/nodes/findings": 500,
    "/api/nodes/detection-reviews": 500,
    "/api/nodes/observability-events": 200,
    "/api/nodes/heartbeats": 150,
    "/api/nodes/clip-uploads": 100,
    "/api/nodes/zones/{zone_id}/catalog": 100,
    "/api/nodes/enrollment": 2_000,
    # [objetivo propio], provisionales (A-55): NFR-GOB-01 no fija cifra para estas tres.
    "/api/nodes/credential-rotations": 2_000,
    "/api/nodes/clip-uploads/{clip_id}/confirmation": 300,
    "/api/nodes/update-results": 200,
}
"""p95 por ruta obligatoria del contrato: una alarma ``latency-node-*`` por cada una de las diez
(nota T-04 del 2026-09-23 de U-03 §8.1, que completa las seis de A-24). La prueba cruzada del
backend la compara con ``NodeRoute``."""

PROVISIONAL_NODE_ROUTE_TARGETS: Final = (
    "/api/nodes/credential-rotations",
    "/api/nodes/clip-uploads/{clip_id}/confirmation",
    "/api/nodes/update-results",
)
"""Objetivos ``[objetivo propio]`` provisionales de A-55: la descripción de su alarma lo dice."""

CERTIFICATE_NODE_ROUTES: Final = (
    "/api/nodes/findings",
    "/api/nodes/observability-events",
    "/api/nodes/heartbeats",
    "/api/nodes/clip-uploads",
    "/api/nodes/clip-uploads/{clip_id}/confirmation",
    "/api/nodes/zones/{zone_id}/catalog",
    "/api/nodes/detection-reviews",
    "/api/nodes/credential-rotations",
    "/api/nodes/update-results",
)
"""Rutas del contrato con certificado de cliente (la alta entra por ``app.`` con límite propio):
el denominador de ``node-rate-limited-high``."""

INGEST_ROUTES: Final = ("/api/nodes/findings", "/api/nodes/observability-events")
"""Ingesta de hallazgos y eventos: el denominador de ``node-permanent-reject-rate``."""

PENDING_UNIT_METRICS: Final[dict[str, str]] = {}
"""Métricas de ``Vigia/Platform`` que una alarma vigila y que su unidad aún no publica en un
catálogo: la unidad las emite con este nombre y la prueba cruzada exige que no estén en el
catálogo del núcleo (al publicarse, salen de esta tabla). ``revocation_list_seconds_to_expiry``
y ``revocation_list_entries`` salieron al publicarlas U-03 (TASK-220)."""

PENDING_UNIT_TASKS: Final[dict[str, str]] = {}
"""Tareas de ``PERIODIC_TASK_MAX_AGE_SECONDS`` que aún no están en la lista cerrada ``task`` de
la política de atributos del núcleo. Las seis de U-03 salieron al registrarlas U-03 en la raíz
y fijarlas en esa lista (VIG-163, TASK-227)."""

FLEET_PERIODIC_TASKS: Final = (
    "detect_mute_nodes",
    "evaluate_fleet_alarms",
    "expire_enrollment_codes",
    "regenerate_revocation_list",
    "mark_orphan_clips",
    "expire_walk_test_sessions",
    "alert_expiring_certificates",
)
"""Las siete tareas de U-03 (LC-GOB-18, ``U03_TASKS`` del worker), estén o no pendientes: el panel
de duración, resultado y edad del último éxito las recorre todas (nota de VIG-155)."""

FLEET_PER_NODE_METRICS: Final = (
    "fleet_heartbeats_total",
    "fleet_heartbeat_gap_seconds",
    "fleet_node_reachable",
    "fleet_node_queue_pending",
    "fleet_node_clock_offset_ms",
    "clip_grants_issued_total",
    "clip_grants_used_total",
    "clip_grants_orphaned_total",
)
"""Las ocho series por nodo de NFR-GOB-55 (contadores y medidores; NFR-GOB-13: a lo sumo 8 por
nodo y ningún histograma). Son las únicas que el colector publica con ``node_id``."""

FLEET_ALARM_KINDS: Final = (
    "node_mute",
    "queue_over_threshold",
    "clock_drift",
    "version_retiring",
    "simulated_adapter_in_productive",
    "certificate_expiring",
    "camera_below_min_fps",
    "orphan_clips_growing",
)
"""Las ocho clases de alarma de flota (``FleetAlarmKind``). El panel «Alarmas abiertas por clase»
dibuja una serie por clase (VIG-187, NFR-GOB-58) y «Certificados por vencer» la de
``certificate_expiring``."""

FLEET_PANELS: Final = (
    "Inventario por estado de comunicacion",
    "Nodos mudos",
    "Colas por encima de umbral",
    "Alarmas abiertas por clase",
    "Latencias y codigos por ruta del contrato",
    "Aceptados y rechazados por tipo de registro",
    "Lista de revocacion: estado y vigencia",
    "Certificados por vencer",
    "Huerfanos por nodo",
    "Tareas periodicas de U-03: duracion y resultado",
    "Semaforos y pools por clase",
)
"""Los once paneles de U-03 §8.2 (NFR-GOB-58) sobre métricas que el código publica. «Alarmas
abiertas por clase» y «certificados por vencer» leen ``fleet_alarms_open`` (VIG-187): el segundo,
su clase ``certificate_expiring``."""

LOOP_PANELS: Final = (
    "Estado del lazo (U-04, n 23)",
    "Trabajos diferidos (U-04, n 23)",
    "Asistente (U-04, n 23)",
    "Correo (U-04, n 23)",
    "Salud de la proyeccion (U-04, n 23)",
)
"""Los cinco paneles de NFR-LAZ-44 en este tablero (nº 23, A-24): U-04 no crea tablero propio."""

LOOP_STATE_TASKS: Final = ("evaluate_deadlines", "review_reminders", "unclosable_review_due")
"""Las tareas del lazo que calculan vencidos, recordatorios y revisiones sin cierre posible."""

LOOP_EMAIL_TASKS: Final = ("email_delivery_events_poll", "email_grouping", "digest_weekly")

EMAIL_PANEL_METRICS: Final = ("Send", "Delivery", "Bounce", "Complaint", "Reject")
"""Las del destino ``cloudwatch`` del conjunto de configuración (§4.2)."""

LOOP_PANEL_METRICS: Final = {
    "Estado del lazo (U-04, n 23)": (
        ("outbox_oldest_pending_age_seconds", ("consumer",)),
        ("periodic_task_duration_ms", ("result", "task")),
        ("periodic_task_last_success_age_seconds", ("task",)),
    ),
    "Trabajos diferidos (U-04, n 23)": (
        ("export_failed_total", ()),
        ("outbox_oldest_pending_age_seconds", ("consumer",)),
        ("periodic_task_duration_ms", ("result", "task")),
        ("periodic_task_last_success_age_seconds", ("task",)),
    ),
    "Asistente (U-04, n 23)": (
        ("assistant_rate_limit_headroom_ratio", ()),
        ("assistant_request_duration_ms", ("assistant_state",)),
        ("assistant_requests_total", ("assistant_state",)),
        ("assistant_token_budget_used_ratio", ("organization_id",)),
        ("assistant_tokens_total", ("organization_id", "token_direction")),
        ("assistant_unavailable", ()),
    ),
    "Correo (U-04, n 23)": (
        ("outbox_oldest_pending_age_seconds", ("consumer",)),
        ("periodic_task_last_success_age_seconds", ("task",)),
    ),
    "Salud de la proyeccion (U-04, n 23)": (
        ("loop_projection_divergences_total", ()),
        ("periodic_task_duration_ms", ("result", "task")),
        ("periodic_task_last_success_age_seconds", ("task",)),
    ),
}
"""Panel de U-04 → métricas de ``Vigia/Platform`` que dibuja, con sus atributos además de
``service`` y ``environment`` (ordenados). La prueba cruzada del backend
(``tests/unit/test_laz_metrics_cross.py``) exige que el código emita cada una (A-64)."""

APPLICATION_ALARM_METRICS: Final = {
    # NFR-NUC-38 y §9.4.
    "integrity-compromised": (("integrity_compromised_total", ()),),
    "dead-letter-created": (("dead_letter_created_total", ()),),
    "security-alert": (("security_alert_total", ()),),
    "chain-lock-timeout-rate": (
        ("chain_locked_timeout_total", ()),
        ("ledger_writes_total", ()),
    ),
    "db-pool-saturation": (("db_pool_in_use", ()), ("db_pool_size", ())),
    "server-error-rate-application": (
        ("http_server_errors_total", ("status_class",)),
        ("http_server_requests_total", ()),
    ),
    "latency-ledger-write": (("operation_duration_ms", ("operation",)),),
    "latency-ledger-write-with-evidence": (("operation_duration_ms", ("operation",)),),
    "latency-ledger-list": (("operation_duration_ms", ("operation",)),),
    "latency-ledger-timeline": (("operation_duration_ms", ("operation",)),),
    "latency-login": (("operation_duration_ms", ("operation",)),),
    "latency-live-view-token": (("operation_duration_ms", ("operation",)),),
    "latency-outbox-delivery": (("operation_duration_ms", ("operation",)),),
    "latency-verify-chains-incremental": (("chain_verification_records_per_second", ()),),
    "periodic-task-stale-write-checkpoints": (
        ("periodic_task_last_success_age_seconds", ("task",)),
    ),
    "periodic-task-stale-verify-chains-incremental": (
        ("periodic_task_last_success_age_seconds", ("task",)),
    ),
    "periodic-task-stale-evidence-sample": (("periodic_task_last_success_age_seconds", ("task",)),),
    "periodic-task-stale-create-partitions": (
        ("periodic_task_last_success_age_seconds", ("task",)),
    ),
    "periodic-task-stale-archive-audit-partitions": (
        ("periodic_task_last_success_age_seconds", ("task",)),
    ),
    "key-rotation-overdue": (("signing_key_days_to_expiry", ()),),
    "default-partition-rows": (("default_partition_rows", ()),),
    "outbox-oldest-age": (("outbox_oldest_pending_age_seconds", ()),),
    "otel-dropped": (("otel_dropped_total", ()),),
    "restore-drill-overdue": (("restore_drill_age_days", ()),),
    # Flota de U-03 (nº 18).
    "node-rate-limited-high": (
        ("rate_limited_total", ("rate_limit",)),
        ("http_server_requests_total", ("route",)),
    ),
    "node-permanent-reject-rate": (
        ("http_server_errors_total", ("route", "status_class")),
        ("rate_limited_total", ("route",)),
        ("http_server_requests_total", ("route",)),
    ),
    "revocation-list-publish-failed": (("revocation_list_publish_failed", ()),),
    "revocation-list-expiring": (("revocation_list_seconds_to_expiry", ()),),
    "periodic-task-stale-detect-mute-nodes": (
        ("periodic_task_last_success_age_seconds", ("task",)),
    ),
    "periodic-task-stale-evaluate-fleet-alarms": (
        ("periodic_task_last_success_age_seconds", ("task",)),
    ),
    "periodic-task-stale-expire-enrollment-codes": (
        ("periodic_task_last_success_age_seconds", ("task",)),
    ),
    "periodic-task-stale-regenerate-revocation-list": (
        ("periodic_task_last_success_age_seconds", ("task",)),
    ),
    "periodic-task-stale-mark-orphan-clips": (
        ("periodic_task_last_success_age_seconds", ("task",)),
    ),
    "periodic-task-stale-expire-walk-test-sessions": (
        ("periodic_task_last_success_age_seconds", ("task",)),
    ),
    "periodic-task-stale-alert-expiring-certificates": (
        ("periodic_task_last_success_age_seconds", ("task",)),
    ),
    "latency-node-findings": (("http_server_duration_ms", ("route",)),),
    "latency-node-detection-reviews": (("http_server_duration_ms", ("route",)),),
    "latency-node-observability-events": (("http_server_duration_ms", ("route",)),),
    "latency-node-heartbeats": (("http_server_duration_ms", ("route",)),),
    "latency-node-clip-uploads": (("http_server_duration_ms", ("route",)),),
    "latency-node-catalog": (("http_server_duration_ms", ("route",)),),
    "latency-node-enrollment": (("http_server_duration_ms", ("route",)),),
    "latency-node-credential-rotations": (("http_server_duration_ms", ("route",)),),
    "latency-node-confirmation": (("http_server_duration_ms", ("route",)),),
    "latency-node-update-results": (("http_server_duration_ms", ("route",)),),
    "bulkhead-person-saturated": (
        ("bulkhead_in_use", ("pool_class",)),
        ("bulkhead_size", ("pool_class",)),
    ),
    "quota-revocation-list-entries": (("revocation_list_entries", ()),),
}
"""Alarma → métricas de ``Vigia/Platform`` que vigila, con los atributos que usa como dimensión
además de ``service`` y ``environment`` (ordenados)."""

NFR_NUC_38_ALARMS: Final = {
    "integrity_compromised": ("integrity-compromised",),
    "dead_letter_created": ("dead-letter-created",),
    "security_alert": ("security-alert",),
    "chain_locked_timeout_rate": ("chain-lock-timeout-rate",),
    "db_pool_saturation": ("db-pool-saturation",),
    "server_error_rate": ("server-error-rate-application",),
    "operation_latency_p95": (
        "latency-ledger-write",
        "latency-ledger-write-with-evidence",
        "latency-ledger-list",
        "latency-ledger-timeline",
        "latency-login",
        "latency-live-view-token",
        "latency-outbox-delivery",
    ),
    "checkpoint_or_verification_task_stale": (
        "periodic-task-stale-write-checkpoints",
        "periodic-task-stale-verify-chains-incremental",
    ),
    "evidence_sample_stale": ("periodic-task-stale-evidence-sample",),
    "signing_key_rotation_overdue": ("key-rotation-overdue",),
}
"""Cada condición de aplicación de NFR-NUC-38 (``ALARM_CONDITIONS`` del backend) y sus alarmas.
Las de infraestructura (una sola zona, replicación, copias, cuotas) salen de los servicios."""

# --- Umbrales de servicio [objetivos propios del diseño] -------------------------------------

THRESHOLD_PERCENT = 80
ERROR_RATE_PERCENT = 1
NODE_RATE_LIMITED_PERCENT = 5
NODE_PERMANENT_REJECT_PERCENT = 1
OUTBOX_MAX_AGE_SECONDS = 300
REVOCATION_LIST_MIN_SECONDS = 86_400
REVOCATION_LIST_MAX_ENTRIES = 100  # uso esperado (U-03 §8.4); límite del servicio en R14
WAF_BLOCKED_SPIKE = 1_000
DB_FREE_STORAGE_PERCENT = 20
# ``max_connections`` por clase [hipótesis de §6.1 y §10: del orden de 450 en db.t4g.medium].
DB_MAX_CONNECTIONS: Final = {"db.t4g.medium": 450, "db.t4g.small": 225}
# Lecturas por prefijo y segundo de S3 (§10 y U-03 §8.4).
S3_READS_PER_PREFIX_PER_SECOND = 5_500

ONE_MINUTE = Duration.minutes(1)
FIVE_MINUTES = Duration.minutes(5)
FIFTEEN_MINUTES = Duration.minutes(15)
ONE_HOUR = Duration.hours(1)
ONE_DAY = Duration.days(1)

# Route 53 (§9.3): cada 30 s, insana tras 3 fallos.
HEALTH_CHECK_INTERVAL_SECONDS = 30
HEALTH_CHECK_FAILURES = 3
HTTPS_PORT = 443
APP_INDEX_PATH = "/version.json"
APP_INDEX_SEARCH = "app_version"

# Cuotas con métrica de uso (§10 y U-03 §8.4): (servicio, tipo, recurso, clase, estadística).
USAGE_QUOTAS: Final = {
    "quota-fargate-vcpu": ("Fargate", "Resource", "vCPU", "Standard/OnDemand", "Maximum"),
    "quota-kms-symmetric": ("KMS", "API", "CryptographicOperationsSymmetric", "None", "Sum"),
    "quota-kms-ecc": ("KMS", "API", "CryptographicOperationsEcc", "None", "Sum"),
    "quota-secrets-get-secret-value": ("Secrets Manager", "API", "GetSecretValue", "None", "Sum"),
    "quota-cloudwatch-put-metric-data": ("CloudWatch", "API", "PutMetricData", "None", "Sum"),
    "quota-logs-put-log-events": ("Logs", "API", "PutLogEvents", "None", "Sum"),
}

BACKUP_FAILED_STATES: Final = ("FAILED", "EXPIRED", "ABORTED")


def dashboard_name(config: EnvironmentConfig) -> str:
    """``vigia-pilot`` (§9.3); ``vigia-<despliegue>`` en los demás."""
    return f"vigia-{config.deployment}"


def _key_suffix(value: str) -> str:
    """``ledger_write`` → ``ledger-write``; ``/api/nodes/zones/{zone_id}/catalog`` → ``catalog``."""
    last = value.rstrip("/").split("/")[-1]
    return last.replace("_", "-")


@dataclass(frozen=True)
class AlarmSpec:
    """Una alarma: métrica o expresión, umbral y evaluación."""

    key: str
    metric: cloudwatch.IMetric
    threshold: float
    comparison: cloudwatch.ComparisonOperator
    description: str
    evaluation_periods: int = 1
    missing: cloudwatch.TreatMissingData = cloudwatch.TreatMissingData.NOT_BREACHING


_GT = cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD
_GE = cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD
_LT = cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD
_MISSING = cloudwatch.TreatMissingData.MISSING
_BREACHING = cloudwatch.TreatMissingData.BREACHING


class ObservabilityStack(VigiaStack):
    """Pila ``vigia-observability`` (infrastructure-design §2.3)."""

    key = "observability"
    summary = "grupos de registro, alarmas, tablero, comprobaciones de salud"

    def __init__(
        self, scope: Construct, config: EnvironmentConfig, *, tags: Mapping[str, str]
    ) -> None:
        super().__init__(scope, config, tags=tags)
        self.foundation = self._sibling(scope, FoundationStack)
        self.data = self._sibling(scope, DataStack)
        self.edge = self._sibling(scope, EdgeStack)
        self.alerts_topic: sns.ITopic = self.foundation.alerts_topic
        self.alarms: dict[str, cloudwatch.Alarm] = {}

        self.log_groups = self._log_groups()
        self.health_checks = self._health_checks()
        self._application_alarms()
        self._fleet_alarms()
        self._service_alarms()
        self._quota_alarms()
        self.backup_rule = self._backup_rule()
        self.dashboard = self._dashboard()

    @staticmethod
    def _sibling[S: VigiaStack](scope: Construct, kind: type[S]) -> S:
        found = next((c for c in scope.node.children if isinstance(c, kind)), None)
        if found is None:
            raise TypeError(f"vigia-observability necesita {kind.__name__} registrada antes")
        return found

    # --- Registros (§9.2) -----------------------------------------------------------------

    def _log_groups(self) -> dict[str, logs.LogGroup]:
        config = self.config
        # Por ARN, como ``vigia-edge``: la política de ``vigia-logs`` ya concede el servicio de
        # registros sobre ``/vigia/<despliegue>/*`` y esta pila no la toca.
        key = kms.Key.from_key_arn(self, "Key-logs", self.foundation.keys[KeyName.LOGS].key_arn)
        removal = RemovalPolicy.DESTROY if config.ephemeral else RemovalPolicy.RETAIN
        return {
            process: logs.LogGroup(
                self,
                f"LogGroup-{process}",
                log_group_name=log_group_name(config, process),
                retention=LOG_RETENTION,
                encryption_key=key,
                removal_policy=removal,
            )
            for process in LOG_PROCESSES
        }

    # --- Métricas -------------------------------------------------------------------------

    def _app_metric(
        self,
        name: str,
        service: str,
        statistic: str,
        period: Duration,
        dimensions: Mapping[str, str] | None = None,
    ) -> cloudwatch.Metric:
        """Métrica de ``Vigia/Platform`` de un servicio del despliegue."""
        return cloudwatch.Metric(
            namespace=METRICS_NAMESPACE,
            metric_name=name,
            dimensions_map={
                "service": service,
                "environment": self.config.deployment,
                **(dimensions or {}),
            },
            statistic=statistic,
            period=period,
        )

    def _both_services(
        self, name: str, statistic: str, period: Duration, label: str
    ) -> dict[str, cloudwatch.IMetric]:
        """La métrica en ``vigia-api`` y en ``vigia-worker`` (``<label>a`` y ``<label>w``)."""
        return {
            f"{label}a": self._app_metric(name, API_SERVICE, statistic, period),
            f"{label}w": self._app_metric(name, WORKER_SERVICE, statistic, period),
        }

    def _sum_both(self, name: str, period: Duration, label: str) -> cloudwatch.MathExpression:
        """Suma de un contador en los dos servicios."""
        return cloudwatch.MathExpression(
            expression=f"FILL({label}a, 0) + FILL({label}w, 0)",
            using_metrics=self._both_services(name, "Sum", period, label),
            period=period,
            label=name,
        )

    def _alb_metric(
        self,
        name: str,
        statistic: str,
        period: Duration,
        load_balancer: str,
        target_group: str | None = None,
        zone: str | None = None,
    ) -> cloudwatch.Metric:
        dimensions = {"LoadBalancer": load_balancer}
        if target_group is not None:
            dimensions["TargetGroup"] = target_group
        if zone is not None:
            dimensions["AvailabilityZone"] = zone
        return cloudwatch.Metric(
            namespace="AWS/ApplicationELB",
            metric_name=name,
            dimensions_map=dimensions,
            statistic=statistic,
            period=period,
        )

    def _ecs_metric(
        self, namespace: str, name: str, service: str, statistic: str, period: Duration
    ) -> cloudwatch.Metric:
        return cloudwatch.Metric(
            namespace=namespace,
            metric_name=name,
            dimensions_map={"ClusterName": cluster_name(self.config), "ServiceName": service},
            statistic=statistic,
            period=period,
        )

    def _rds_metric(self, name: str, statistic: str, period: Duration) -> cloudwatch.Metric:
        return cloudwatch.Metric(
            namespace="AWS/RDS",
            metric_name=name,
            dimensions_map={"DBInstanceIdentifier": db_identifier(self.config)},
            statistic=statistic,
            period=period,
        )

    # --- Alarmas --------------------------------------------------------------------------

    def _alarm(self, spec: AlarmSpec) -> cloudwatch.Alarm:
        if spec.key in self.alarms:
            raise ValueError(f"Alarma duplicada: {spec.key}")
        alarm = cloudwatch.Alarm(
            self,
            f"Alarm-{spec.key}",
            alarm_name=self.config.resource_name(spec.key),
            alarm_description=spec.description,
            metric=spec.metric,
            threshold=spec.threshold,
            comparison_operator=spec.comparison,
            evaluation_periods=spec.evaluation_periods,
            datapoints_to_alarm=spec.evaluation_periods,
            treat_missing_data=spec.missing,
        )
        alarm.add_alarm_action(cloudwatch_actions.SnsAction(self.alerts_topic))
        self.alarms[spec.key] = alarm
        return alarm

    def _ratio(
        self,
        numerator: Mapping[str, cloudwatch.IMetric],
        denominator: Mapping[str, cloudwatch.IMetric],
        period: Duration,
        label: str,
        *,
        subtract: Mapping[str, cloudwatch.IMetric] | None = None,
    ) -> cloudwatch.MathExpression:
        """``100 · Σ numerador / Σ denominador`` (0 sin denominador); ``subtract`` se resta del
        numerador."""

        def total(metrics: Mapping[str, cloudwatch.IMetric]) -> str:
            return " + ".join(f"FILL({key}, 0)" for key in metrics)

        top = total(numerator)
        if subtract:
            top = f"{top} - ({total(subtract)})"
        bottom = total(denominator)
        return cloudwatch.MathExpression(
            expression=f"IF(({bottom}) > 0, 100 * ({top}) / ({bottom}), 0)",
            using_metrics={**numerator, **(subtract or {}), **denominator},
            period=period,
            label=label,
        )

    def _application_alarms(self) -> None:
        """§9.4 sobre las métricas de ``observability.metrics`` (NFR-NUC-38)."""
        api, worker = API_SERVICE, WORKER_SERVICE
        self._alarm(
            AlarmSpec(
                "integrity-compromised",
                self._sum_both("integrity_compromised_total", ONE_MINUTE, "i"),
                1,
                _GE,
                "MAXIMA SEVERIDAD: una cadena del expediente con integrity_compromised "
                "(NFR-NUC-38). Runbook de integridad.",
            )
        )
        self._alarm(
            AlarmSpec(
                "dead-letter-created",
                self._sum_both("dead_letter_created_total", FIVE_MINUTES, "d"),
                1,
                _GE,
                "Evento de la bandeja enviado a la cola muerta (NFR-NUC-38)",
            )
        )
        self._alarm(
            AlarmSpec(
                "security-alert",
                self._sum_both("security_alert_total", FIVE_MINUTES, "s"),
                1,
                _GE,
                "Alerta de seguridad de NFR-NUC-28 (security_alert_total por tipo)",
            )
        )
        self._alarm(
            AlarmSpec(
                "chain-lock-timeout-rate",
                self._ratio(
                    self._both_services("chain_locked_timeout_total", "Sum", FIVE_MINUTES, "t"),
                    self._both_services("ledger_writes_total", "Sum", FIVE_MINUTES, "w"),
                    FIVE_MINUTES,
                    "chain_locked_timeout %",
                ),
                ERROR_RATE_PERCENT,
                _GT,
                "chain_locked_timeout por encima del 1 % de las escrituras en 5 min (NFR-NUC-38)",
            )
        )
        self._alarm(
            AlarmSpec(
                "db-pool-saturation",
                self._ratio(
                    self._both_services("db_pool_in_use", "Average", ONE_MINUTE, "u"),
                    self._both_services("db_pool_size", "Average", ONE_MINUTE, "z"),
                    ONE_MINUTE,
                    "pool en uso %",
                ),
                THRESHOLD_PERCENT,
                _GT,
                "Conexiones del pool por encima del 80 % durante 5 min (NFR-NUC-38)",
                evaluation_periods=5,
            )
        )
        self._alarm(
            AlarmSpec(
                "server-error-rate-application",
                self._ratio(
                    {
                        "e": self._app_metric(
                            "http_server_errors_total",
                            api,
                            "Sum",
                            FIVE_MINUTES,
                            {"status_class": "5xx"},
                        )
                    },
                    {"r": self._app_metric("http_server_requests_total", api, "Sum", FIVE_MINUTES)},
                    FIVE_MINUTES,
                    "5xx de la aplicacion %",
                ),
                ERROR_RATE_PERCENT,
                _GT,
                "Respuestas 5xx de vigia-api por encima del 1 % en 5 min (NFR-NUC-38)",
            )
        )
        for operation, target in OPERATION_P95_TARGETS_MS.items():
            service = worker if operation == "outbox_delivery" else api
            self._alarm(
                AlarmSpec(
                    f"latency-{_key_suffix(operation)}",
                    self._app_metric(
                        "operation_duration_ms",
                        service,
                        "p95",
                        FIVE_MINUTES,
                        {"operation": operation},
                    ),
                    2 * target,
                    _GT,
                    f"p95 de {operation} por encima de {2 * target} ms (2 x objetivo de "
                    "NFR-NUC-01) durante 15 min",
                    evaluation_periods=3,
                )
            )
        self._alarm(
            AlarmSpec(
                "latency-verify-chains-incremental",
                self._app_metric(
                    "chain_verification_records_per_second", worker, "Average", FIVE_MINUTES
                ),
                VERIFICATION_TARGET_RECORDS_PER_SECOND / 2,
                _LT,
                "Verificacion incremental por debajo de 2 500 registros/s (mitad del objetivo "
                "de NFR-NUC-01) durante 15 min",
                evaluation_periods=3,
            )
        )
        for task, max_age in PERIODIC_TASK_MAX_AGE_SECONDS.items():
            self._periodic_alarm(task, max_age)
        self._alarm(
            AlarmSpec(
                "key-rotation-overdue",
                cloudwatch.MathExpression(
                    expression="MIN([ka, kw])",
                    using_metrics=self._both_services(
                        "signing_key_days_to_expiry", "Minimum", ONE_HOUR, "k"
                    ),
                    period=ONE_HOUR,
                    label="signing_key_days_to_expiry",
                ),
                0,
                _LT,
                "Clave de firma vencida para algun proposito (NFR-NUC-38)",
                missing=_MISSING,
            )
        )
        self._alarm(
            AlarmSpec(
                "default-partition-rows",
                self._app_metric("default_partition_rows", worker, "Maximum", ONE_HOUR),
                1,
                _GE,
                "Filas en la particion por defecto (PAT-NUC-ESC-01)",
                missing=_MISSING,
            )
        )
        self._alarm(
            AlarmSpec(
                "outbox-oldest-age",
                self._app_metric(
                    "outbox_oldest_pending_age_seconds", worker, "Maximum", FIVE_MINUTES
                ),
                OUTBOX_MAX_AGE_SECONDS,
                _GT,
                "Evento pendiente de la bandeja con mas de 300 s durante 10 min (NFR-NUC-01)",
                evaluation_periods=2,
            )
        )
        self._alarm(
            AlarmSpec(
                "otel-dropped",
                self._sum_both("otel_dropped_total", FIVE_MINUTES, "o"),
                0,
                _GT,
                "Tramos o metricas descartados por el colector durante 10 min (PAT-NUC-RES-03)",
                evaluation_periods=2,
            )
        )
        self._alarm(
            AlarmSpec(
                "restore-drill-overdue",
                self._app_metric("restore_drill_age_days", worker, "Maximum", ONE_DAY),
                RESTORE_DRILL_OVERDUE_DAYS,
                _GT,
                "Mas de 100 dias sin ensayo de restauracion correcto (RESILIENCY-12, NFR-NUC-12)",
                missing=_MISSING,
            )
        )

    def _periodic_alarm(self, task: str, max_age: int) -> None:
        # Las tareas de 60 s se evalúan en 3 periodos de 1 min (parada ordenada de 120 s, U-03).
        fast = max_age < ONE_HOUR.to_seconds()
        period = ONE_MINUTE if fast else FIVE_MINUTES
        self._alarm(
            AlarmSpec(
                f"periodic-task-stale-{_key_suffix(task)}",
                self._app_metric(
                    "periodic_task_last_success_age_seconds",
                    WORKER_SERVICE,
                    "Maximum",
                    period,
                    {"task": task},
                ),
                max_age,
                _GT,
                f"{task} sin exito en {max_age} s (NFR-NUC-38 y NFR-GOB-08)",
                evaluation_periods=3 if fast else 1,
                missing=_MISSING,
            )
        )

    def _fleet_alarms(self) -> None:
        """Alarmas de flota de U-03 (nº 17 y 18, U-03 infrastructure-design §8.1)."""
        api, worker = API_SERVICE, WORKER_SERVICE
        requests = {
            f"r{index}": self._app_metric(
                "http_server_requests_total", api, "Sum", FIFTEEN_MINUTES, {"route": route}
            )
            for index, route in enumerate(CERTIFICATE_NODE_ROUTES)
        }
        self._alarm(
            AlarmSpec(
                "node-rate-limited-high",
                self._ratio(
                    {
                        "l": self._app_metric(
                            "rate_limited_total",
                            api,
                            "Sum",
                            FIFTEEN_MINUTES,
                            {"rate_limit": "node"},
                        )
                    },
                    requests,
                    FIFTEEN_MINUTES,
                    "rate_limited de nodos %",
                ),
                NODE_RATE_LIMITED_PERCENT,
                _GT,
                "rate_limited por encima del 5 % de las peticiones de los nodos en 15 min, "
                "agregado de la flota (NFR-GOB-46)",
            )
        )

        def by_route(
            name: str, prefix: str, extra: Mapping[str, str] | None = None
        ) -> dict[str, cloudwatch.IMetric]:
            return {
                f"{prefix}{index}": self._app_metric(
                    name, api, "Sum", FIFTEEN_MINUTES, {"route": route, **(extra or {})}
                )
                for index, route in enumerate(INGEST_ROUTES)
            }

        self._alarm(
            AlarmSpec(
                "node-permanent-reject-rate",
                self._ratio(
                    by_route("http_server_errors_total", "e", {"status_class": "4xx"}),
                    by_route("http_server_requests_total", "r"),
                    FIFTEEN_MINUTES,
                    "rechazos permanentes de la ingesta %",
                    subtract=by_route("rate_limited_total", "l"),
                ),
                NODE_PERMANENT_REJECT_PERCENT,
                _GT,
                "Rechazos 4xx no transitorios por encima del 1 % de la ingesta en 15 min "
                "(NFR-GOB-46)",
            )
        )
        # El contador del ciclo (TASK-220): suma también un ciclo cortado a mitad, que el
        # planificador no siempre registra como ``failed``.
        self._alarm(
            AlarmSpec(
                "revocation-list-publish-failed",
                self._app_metric("revocation_list_publish_failed", worker, "Sum", FIVE_MINUTES),
                1,
                _GE,
                "Fallo de publicacion de la lista de revocacion en 5 min (NFR-GOB-46, 48). "
                "Runbook de la lista de revocacion (U-03 6.2).",
            )
        )
        self._alarm(
            AlarmSpec(
                "revocation-list-expiring",
                self._app_metric(
                    "revocation_list_seconds_to_expiry", worker, "Minimum", FIVE_MINUTES
                ),
                REVOCATION_LIST_MIN_SECONDS,
                _LT,
                "Lista de revocacion a menos de 24 h de vencer (NFR-GOB-48). "
                "Runbook de la lista de revocacion (U-03 6.2).",
                missing=_MISSING,
            )
        )
        for route, target in NODE_ROUTE_P95_TARGETS_MS.items():
            source = (
                "objetivo propio provisional, A-55"
                if route in PROVISIONAL_NODE_ROUTE_TARGETS
                else "NFR-GOB-01"
            )
            self._alarm(
                AlarmSpec(
                    f"latency-node-{_key_suffix(route)}",
                    self._app_metric(
                        "http_server_duration_ms", api, "p95", FIVE_MINUTES, {"route": route}
                    ),
                    2 * target,
                    _GT,
                    f"p95 de {route} por encima de {2 * target} ms (2 x {target} ms, {source}) "
                    "durante 15 min",
                    evaluation_periods=3,
                )
            )
        person = {"pool_class": "person"}
        self._alarm(
            AlarmSpec(
                "bulkhead-person-saturated",
                self._ratio(
                    {"u": self._app_metric("bulkhead_in_use", api, "Maximum", ONE_MINUTE, person)},
                    {"z": self._app_metric("bulkhead_size", api, "Minimum", ONE_MINUTE, person)},
                    ONE_MINUTE,
                    "semaforo person %",
                ),
                100,
                _GE,
                "Semaforo de la clase person al limite durante mas de 2 min (NFR-GOB-19)",
                evaluation_periods=3,
            )
        )
        self._alarm(
            AlarmSpec(
                "api-memory-high",
                self._ecs_metric("AWS/ECS", "MemoryUtilization", api, "Average", FIVE_MINUTES),
                THRESHOLD_PERCENT,
                _GT,
                "Memoria de vigia-api por encima del 80 % durante 15 min (A-21, n 17)",
                evaluation_periods=3,
            )
        )

    def _service_alarms(self) -> None:
        """Balanceadores, ECS, RDS, Route 53, traducción y cortafuegos (§9.4)."""
        edge = self.edge
        app_lb = edge.app_load_balancer.load_balancer_full_name
        tg_api = edge.app_target_group.target_group_full_name
        self._server_error_rate("server-error-rate-app", app_lb, "vigia-alb-app")
        if self.config.nodes_tls_mode is NodesTlsMode.MTLS:
            self._server_error_rate(
                "server-error-rate-nodes",
                edge.nodes_load_balancer.load_balancer_full_name,
                "vigia-alb-nodes",
            )
        zones = {
            f"h{letter}": self._alb_metric(
                "HealthyHostCount", "Minimum", ONE_MINUTE, app_lb, tg_api, zone
            )
            for letter, zone in zip(ZONE_LETTERS, ZONES, strict=True)
        }
        self._alarm(
            AlarmSpec(
                "single-zone-api",
                cloudwatch.MathExpression(
                    expression=f"MIN([{', '.join(zones)}])",
                    using_metrics=zones,
                    period=ONE_MINUTE,
                    label="tg-api sanos en la zona con menos",
                ),
                1,
                _LT,
                "tg-api sin destino sano en alguna zona durante 5 min (RESILIENCY-07)",
                evaluation_periods=5,
                missing=_BREACHING,
            )
        )
        self._alarm(
            AlarmSpec(
                "availability-internal-probe",
                self._alb_metric("HealthyHostCount", "Minimum", ONE_MINUTE, app_lb, tg_api),
                1,
                _LT,
                "Sondeo interno de NFR-NUC-10: ningun destino de tg-api sano en /health/ready "
                "durante 2 min",
                evaluation_periods=2,
                missing=_BREACHING,
            )
        )
        self._alarm(
            AlarmSpec(
                "worker-absent",
                self._ecs_metric(
                    "ECS/ContainerInsights",
                    "RunningTaskCount",
                    WORKER_SERVICE,
                    "Minimum",
                    ONE_MINUTE,
                ),
                1,
                _LT,
                "vigia-worker sin tareas en ejecucion durante 5 min (RESILIENCY-07)",
                evaluation_periods=5,
                missing=_BREACHING,
            )
        )
        self._rds_alarms()
        for name, check in self.health_checks.items():
            self._alarm(
                AlarmSpec(
                    "vigia-app-index-unhealthy"
                    if name == "app-index"
                    else f"health-check-failed-{name}",
                    cloudwatch.Metric(
                        namespace="AWS/Route53",
                        metric_name="HealthCheckStatus",
                        dimensions_map={"HealthCheckId": check.attr_health_check_id},
                        statistic="Minimum",
                        period=ONE_MINUTE,
                    ),
                    1,
                    _LT,
                    f"Comprobacion de Route 53 {name} insana durante 2 min (NFR-NUC-10)",
                    evaluation_periods=2,
                    missing=_BREACHING,
                )
            )
        self._nat_alarms()
        if self.config.waf_enabled:
            self._alarm(
                AlarmSpec(
                    "waf-blocked-spike",
                    cloudwatch.Metric(
                        namespace="AWS/WAFV2",
                        metric_name="BlockedRequests",
                        dimensions_map={
                            "WebACL": self.config.resource_name("app-waf"),
                            "Region": self.region,
                            "Rule": "ALL",
                        },
                        statistic="Sum",
                        period=FIVE_MINUTES,
                    ),
                    WAF_BLOCKED_SPIKE,
                    _GT,
                    "Informativa: mas de 1 000 peticiones bloqueadas en 5 min (SECURITY-14)",
                )
            )

    def _server_error_rate(self, key: str, load_balancer: str, name: str) -> None:
        self._alarm(
            AlarmSpec(
                key,
                self._ratio(
                    {
                        "e": self._alb_metric(
                            "HTTPCode_Target_5XX_Count", "Sum", FIVE_MINUTES, load_balancer
                        )
                    },
                    {"r": self._alb_metric("RequestCount", "Sum", FIVE_MINUTES, load_balancer)},
                    FIVE_MINUTES,
                    f"5XX de {name} %",
                ),
                ERROR_RATE_PERCENT,
                _GT,
                f"5XX de los destinos de {name} por encima del 1 % en 5 min (NFR-NUC-38)",
            )
        )

    def _rds_alarms(self) -> None:
        config = self.config
        max_connections = DB_MAX_CONNECTIONS.get(config.db_instance_class)
        if max_connections is None:
            raise ValueError(
                f"Sin max_connections declarado para la clase {config.db_instance_class!r}"
            )
        allocated_bytes = DB_ALLOCATED_STORAGE_GB * 1024**3
        self._alarm(
            AlarmSpec(
                "rds-storage-low",
                self._rds_metric("FreeStorageSpace", "Minimum", FIVE_MINUTES),
                allocated_bytes * DB_FREE_STORAGE_PERCENT / 100,
                _LT,
                "Almacenamiento libre de la base por debajo del 20 % de lo asignado "
                "(RESILIENCY-07)",
            )
        )
        self._alarm(
            AlarmSpec(
                "rds-connections-high",
                self._rds_metric("DatabaseConnections", "Maximum", FIVE_MINUTES),
                max_connections * THRESHOLD_PERCENT / 100,
                _GT,
                f"Conexiones de la base por encima del 80 % de {max_connections} "
                "(10, hipotesis de max_connections)",
            )
        )
        self._alarm(
            AlarmSpec(
                "rds-cpu-high",
                self._rds_metric("CPUUtilization", "Average", FIVE_MINUTES),
                THRESHOLD_PERCENT,
                _GT,
                "CPU de la base por encima del 80 % durante 15 min",
                evaluation_periods=3,
            )
        )

    def _nat_alarms(self) -> None:
        subnets = self.foundation.vpc.select_subnets(subnet_group_name=PUBLIC_SUBNETS).subnets
        nats = [
            (letter, nat)
            for letter, subnet in zip(ZONE_LETTERS, subnets, strict=True)
            if isinstance(nat := subnet.node.try_find_child("NATGateway"), ec2.CfnNatGateway)
        ]
        for letter, nat in nats:
            key = "nat-single-az-degraded" if len(nats) == 1 else f"nat-{letter}-degraded"
            self._alarm(
                AlarmSpec(
                    key,
                    cloudwatch.Metric(
                        namespace="AWS/NATGateway",
                        metric_name="PacketsDropCount",
                        dimensions_map={"NatGatewayId": nat.ref},
                        statistic="Sum",
                        period=FIVE_MINUTES,
                    ),
                    0,
                    _GT,
                    f"vigia-nat-{letter} descarta paquetes durante 15 min (R13)",
                    evaluation_periods=3,
                )
            )

    def _quota_alarms(self) -> None:
        """Cuotas al 80 % (§10 y U-03 §8.4; RESILIENCY-09)."""
        for key, (service, kind, resource, klass, statistic) in USAGE_QUOTAS.items():
            usage = cloudwatch.Metric(
                namespace="AWS/Usage",
                metric_name="ResourceCount" if kind == "Resource" else "CallCount",
                dimensions_map={
                    "Service": service,
                    "Type": kind,
                    "Resource": resource,
                    "Class": klass,
                },
                statistic=statistic,
                period=FIVE_MINUTES,
            )
            # Las cuotas de llamadas son por segundo: la suma de 5 min se divide por 300.
            rate = "m" if kind == "Resource" else f"(m / {int(FIVE_MINUTES.to_seconds())})"
            self._alarm(
                AlarmSpec(
                    key,
                    cloudwatch.MathExpression(
                        expression=f"100 * {rate} / SERVICE_QUOTA(m)",
                        using_metrics={"m": usage},
                        period=FIVE_MINUTES,
                        label=f"{service} {resource} % de la cuota",
                    ),
                    THRESHOLD_PERCENT,
                    _GT,
                    f"Uso de {service} {resource} por encima del 80 % de la cuota (10)",
                )
            )
        evidence = self.config.bucket_name("evidence", self.account)
        reads = {
            name[0].lower(): cloudwatch.Metric(
                namespace="AWS/S3",
                metric_name=name,
                dimensions_map={"BucketName": evidence, "FilterId": EVIDENCE_REQUEST_METRICS},
                statistic="Sum",
                period=ONE_MINUTE,
            )
            for name in ("GetRequests", "HeadRequests")
        }
        self._alarm(
            AlarmSpec(
                "quota-evidence-reads",
                cloudwatch.MathExpression(
                    expression="100 * (FILL(g, 0) + FILL(h, 0)) / 60 / "
                    f"{S3_READS_PER_PREFIX_PER_SECOND}",
                    using_metrics=reads,
                    period=ONE_MINUTE,
                    label="lecturas de vigia-evidence % de un prefijo",
                ),
                THRESHOLD_PERCENT,
                _GT,
                "Lecturas de vigia-evidence por encima del 80 % de 5 500/s de un prefijo "
                "(U-03 8.4)",
            )
        )
        self._alarm(
            AlarmSpec(
                "quota-revocation-list-entries",
                self._app_metric("revocation_list_entries", WORKER_SERVICE, "Maximum", ONE_HOUR),
                REVOCATION_LIST_MAX_ENTRIES * THRESHOLD_PERCENT / 100,
                _GT,
                "Lista de revocacion por encima del 80 % de las 100 entradas esperadas "
                "(U-03 8.4; limite del servicio en R14)",
                missing=_MISSING,
            )
        )

    def _backup_rule(self) -> events.CfnRule:
        """``backup-job-failed``: trabajo de copia fallido, expirado o abortado (§9.4)."""
        config = self.config
        return events.CfnRule(
            self,
            "BackupJobFailed",
            name=config.resource_name("backup-job-failed"),
            description="Trabajo de copia de vigia-backup-vault fallido o expirado (9.4)",
            event_pattern={
                "source": ["aws.backup"],
                "detail-type": ["Backup Job State Change"],
                "detail": {
                    "state": list(BACKUP_FAILED_STATES),
                    "backupVaultName": [config.resource_name("backup-vault")],
                },
            },
            targets=[
                events.CfnRule.TargetProperty(arn=self.alerts_topic.topic_arn, id="vigia-alerts")
            ],
        )

    # --- Route 53 (§9.3) ------------------------------------------------------------------

    def _health_check(
        self,
        name: str,
        host: str,
        kind: str,
        *,
        path: str | None = None,
        search: str | None = None,
    ) -> route53.CfnHealthCheck:
        http = kind != "TCP"
        return route53.CfnHealthCheck(
            self,
            f"HealthCheck-{name}",
            health_check_config=route53.CfnHealthCheck.HealthCheckConfigProperty(
                type=kind,
                fully_qualified_domain_name=Fn.join(".", [host, self.edge.domain]),
                port=HTTPS_PORT,
                resource_path=path,
                search_string=search,
                enable_sni=True if http else None,
                request_interval=HEALTH_CHECK_INTERVAL_SECONDS,
                failure_threshold=HEALTH_CHECK_FAILURES,
            ),
            health_check_tags=[
                route53.CfnHealthCheck.HealthCheckTagProperty(
                    key="Name", value=self.config.resource_name(name)
                )
            ],
        )

    def _health_checks(self) -> dict[str, route53.CfnHealthCheck]:
        config = self.config
        return {
            "app": self._health_check("app", app_host(config), "HTTPS", path=LIVE_PATH),
            # Solo TCP: sin certificado de cliente no hay respuesta HTTP (nota U02-H-07).
            "nodes": self._health_check("nodes", nodes_host(config), "TCP"),
            "app-index": self._health_check(
                "app-index",
                app_host(config),
                "HTTPS_STR_MATCH",
                path=APP_INDEX_PATH,
                search=APP_INDEX_SEARCH,
            ),
        }

    # --- Tablero (§9.3, NFR-NUC-44, nº 12, 18 y 23) --------------------------------------

    def _dashboard(self) -> cloudwatch.Dashboard:
        dashboard = cloudwatch.Dashboard(
            self,
            "Dashboard",
            dashboard_name=dashboard_name(self.config),
            default_interval=Duration.hours(3),
        )
        for title, widgets in self._panels():
            dashboard.add_widgets(cloudwatch.TextWidget(markdown=f"## {title}", width=24, height=1))
            dashboard.add_widgets(*widgets)
        return dashboard

    def _graph(
        self, title: str, metrics: Sequence[cloudwatch.IMetric], *, width: int = 8
    ) -> cloudwatch.GraphWidget:
        return cloudwatch.GraphWidget(title=title, left=list(metrics), width=width, height=6)

    def _alarm_status(self, title: str, keys: Sequence[str]) -> cloudwatch.AlarmStatusWidget:
        return cloudwatch.AlarmStatusWidget(
            title=title,
            alarms=[self.alarms[key] for key in keys if key in self.alarms],
            width=24,
            height=3,
        )

    def _panels(self) -> list[tuple[str, list[cloudwatch.IWidget]]]:
        api, worker = API_SERVICE, WORKER_SERVICE
        edge = self.edge
        app_lb = edge.app_load_balancer.load_balancer_full_name
        tg_api = edge.app_target_group.target_group_full_name
        m5 = FIVE_MINUTES
        health = [
            cloudwatch.Metric(
                namespace="AWS/Route53",
                metric_name="HealthCheckStatus",
                dimensions_map={"HealthCheckId": check.attr_health_check_id},
                statistic="Minimum",
                period=ONE_MINUTE,
                label=name,
            )
            for name, check in self.health_checks.items()
        ]
        month = Duration.days(30)
        availability = self._ratio(
            {"e": self._alb_metric("HTTPCode_Target_5XX_Count", "Sum", month, app_lb)},
            {"r": self._alb_metric("RequestCount", "Sum", month, app_lb)},
            month,
            "5XX del mes %",
        )
        return [
            (
                "Salud por zona",
                [
                    self._graph(
                        "tg-api sanos por zona",
                        [
                            self._alb_metric(
                                "HealthyHostCount", "Minimum", ONE_MINUTE, app_lb, tg_api, zone
                            )
                            for zone in ZONES
                        ],
                    ),
                    self._graph(
                        "Tareas en ejecucion",
                        [
                            self._ecs_metric(
                                "ECS/ContainerInsights", "RunningTaskCount", name, "Minimum", m5
                            )
                            for name in (api, worker)
                        ],
                    ),
                    self._graph("Comprobaciones de Route 53", health),
                ],
            ),
            (
                "Disponibilidad (NFR-NUC-10, sondeo interno)",
                [
                    cloudwatch.SingleValueWidget(
                        title="Disponibilidad del mes (100 - % de 5XX)",
                        metrics=[
                            cloudwatch.MathExpression(
                                expression="100 - a",
                                using_metrics={"a": availability},
                                period=month,
                                label="disponibilidad %",
                            )
                        ],
                        width=8,
                        height=6,
                    ),
                    self._graph(
                        "tg-api sanos (/health/ready)",
                        [
                            self._alb_metric(
                                "HealthyHostCount", "Minimum", ONE_MINUTE, app_lb, tg_api
                            )
                        ],
                    ),
                    self._alarm_status(
                        "Alarmas de disponibilidad",
                        [
                            "availability-internal-probe",
                            "server-error-rate-app",
                            "health-check-failed-app",
                        ],
                    ),
                ],
            ),
            (
                "Aplicacion (n 12)",
                [
                    self._graph(
                        "health_ready por app_version (version desplegada)",
                        [
                            cloudwatch.MathExpression(
                                expression=(
                                    f"SEARCH('{{{METRICS_NAMESPACE},service,environment,"
                                    f'app_version}} MetricName="health_ready" '
                                    f'service="{api}" environment="{self.config.deployment}"\''
                                    ", 'Minimum', 60)"
                                ),
                                label="",
                                period=ONE_MINUTE,
                            )
                        ],
                    ),
                    self._graph(
                        "5XX y p95 de vigia-alb-app",
                        [
                            self._alb_metric("HTTPCode_Target_5XX_Count", "Sum", m5, app_lb),
                            self._alb_metric("TargetResponseTime", "p95", m5, app_lb),
                        ],
                    ),
                    self._alarm_status("vigia-app-index", ["vigia-app-index-unhealthy"]),
                ],
            ),
            (
                "Latencias p95 (NFR-NUC-01)",
                [
                    self._graph(
                        "Operaciones del nucleo (ms)",
                        [
                            self._app_metric(
                                "operation_duration_ms",
                                worker if operation == "outbox_delivery" else api,
                                "p95",
                                m5,
                                {"operation": operation},
                            )
                            for operation in OPERATION_P95_TARGETS_MS
                        ],
                        width=16,
                    ),
                    self._graph(
                        "Verificacion incremental (registros/s)",
                        [
                            self._app_metric(
                                "chain_verification_records_per_second", worker, "Average", m5
                            )
                        ],
                    ),
                ],
            ),
            (
                "Errores",
                [
                    self._graph(
                        "5XX de los balanceadores",
                        [self._alb_metric("HTTPCode_Target_5XX_Count", "Sum", m5, app_lb)]
                        + (
                            [
                                self._alb_metric(
                                    "HTTPCode_Target_5XX_Count",
                                    "Sum",
                                    m5,
                                    edge.nodes_load_balancer.load_balancer_full_name,
                                )
                            ]
                            if self.config.nodes_tls_mode is NodesTlsMode.MTLS
                            else []
                        ),
                    ),
                    self._graph(
                        "Errores de vigia-api por clase",
                        [
                            self._app_metric(
                                "http_server_errors_total", api, "Sum", m5, {"status_class": c}
                            )
                            for c in ("4xx", "5xx")
                        ],
                    ),
                    self._graph(
                        "Escrituras y chain_locked_timeout",
                        [
                            self._sum_both("ledger_writes_total", m5, "w"),
                            self._sum_both("chain_locked_timeout_total", m5, "t"),
                        ],
                    ),
                ],
            ),
            (
                "Estado de las cadenas",
                [
                    self._graph(
                        "Ultima verificacion y punto de control (s)",
                        [
                            self._app_metric(
                                "periodic_task_last_success_age_seconds",
                                worker,
                                "Maximum",
                                m5,
                                {"task": task},
                            )
                            for task in ("verify_chains_incremental", "write_checkpoints")
                        ],
                    ),
                    self._graph(
                        "integrity_compromised",
                        [self._sum_both("integrity_compromised_total", m5, "i")],
                    ),
                    self._alarm_status(
                        "Alarmas de las cadenas",
                        [
                            "integrity-compromised",
                            "periodic-task-stale-write-checkpoints",
                            "periodic-task-stale-verify-chains-incremental",
                        ],
                    ),
                ],
            ),
            (
                "Bandeja de salida",
                [
                    self._graph(
                        "Pendientes y antiguedad",
                        [
                            self._app_metric("outbox_pending", worker, "Maximum", m5),
                            self._app_metric(
                                "outbox_oldest_pending_age_seconds", worker, "Maximum", m5
                            ),
                        ],
                    ),
                    self._graph(
                        "Cola muerta y circuitos",
                        [
                            self._sum_both("dead_letter_created_total", m5, "d"),
                            self._app_metric("outbox_circuit_open", worker, "Maximum", m5),
                        ],
                    ),
                ],
            ),
            (
                "Seguridad (NFR-NUC-28)",
                [
                    self._graph(
                        "Fallos de autenticacion y alertas",
                        [
                            self._app_metric("auth_failures_total", api, "Sum", m5),
                            self._sum_both("security_alert_total", m5, "s"),
                        ],
                    ),
                    self._graph(
                        "Alertas por tipo",
                        [
                            cloudwatch.MathExpression(
                                expression=(
                                    f"SEARCH('{{{METRICS_NAMESPACE},service,environment,"
                                    f'alert_type}} MetricName="security_alert_total" '
                                    f"environment=\"{self.config.deployment}\"', 'Sum', 300)"
                                ),
                                label="",
                                period=m5,
                            )
                        ],
                    ),
                    self._alarm_status(
                        "Alarmas de seguridad", ["security-alert", "waf-blocked-spike"]
                    ),
                ],
            ),
            (
                "Base de datos",
                [
                    self._graph(
                        "Conexiones y CPU",
                        [
                            self._rds_metric("DatabaseConnections", "Maximum", m5),
                            self._rds_metric("CPUUtilization", "Average", m5),
                        ],
                    ),
                    self._graph(
                        "Almacenamiento libre",
                        [self._rds_metric("FreeStorageSpace", "Minimum", m5)],
                    ),
                    self._graph(
                        "IOPS",
                        [
                            self._rds_metric("ReadIOPS", "Average", m5),
                            self._rds_metric("WriteIOPS", "Average", m5),
                        ],
                    ),
                ],
            ),
            (
                "Cuotas (10)",
                [
                    self._alarm_status(
                        "Cuotas al 80 %",
                        [key for key in self.alarms if key.startswith("quota-")]
                        + ["rds-connections-high"],
                    )
                ],
            ),
            (
                "Costo",
                [
                    self._graph(
                        "Cargo estimado de la cuenta (USD)",
                        [
                            cloudwatch.Metric(
                                namespace="AWS/Billing",
                                metric_name="EstimatedCharges",
                                dimensions_map={"Currency": "USD"},
                                statistic="Maximum",
                                period=Duration.hours(6),
                            )
                        ],
                        width=24,
                    )
                ],
            ),
            *self._fleet_panels(),
            *self._loop_panels(),
        ]

    def _search(
        self,
        name: str,
        service: str,
        statistic: str,
        period: Duration,
        *dimensions: str,
        filters: Mapping[str, str] | None = None,
    ) -> str:
        """``SEARCH`` de una métrica del despliegue: una serie por valor de ``dimensions``.

        ``filters`` añade pares ``dimension="valor"`` a la consulta (por ejemplo
        ``alarm_kind="certificate_expiring"``): el resultado son las series del despliegue que
        cumplen ese valor.
        """
        schema = ",".join((METRICS_NAMESPACE, "service", "environment", *dimensions))
        extra = "".join(f' {key}="{value}"' for key, value in (filters or {}).items())
        return (
            f'SEARCH(\'{{{schema}}} MetricName="{name}" service="{service}" '
            f"environment=\"{self.config.deployment}\"{extra}', '{statistic}', "
            f"{int(period.to_seconds())})"
        )

    def _expression(self, expression: str, period: Duration, label: str = "") -> cloudwatch.IMetric:
        return cloudwatch.MathExpression(expression=expression, label=label, period=period)

    def _open_alarms_by_class(self, kind: str, period: Duration) -> cloudwatch.IMetric:
        """``SUM(SEARCH(...))`` de ``fleet_alarms_open`` de una clase, sumando organizaciones.

        El medidor lleva ``organization_id`` (cada tarea publica su organización); el ``SEARCH``
        devuelve una serie por organización y el ``SUM`` las suma, de modo que una organización no
        pisa a otra (VIG-187).
        """
        search = self._search(
            "fleet_alarms_open",
            WORKER_SERVICE,
            "Maximum",
            period,
            "organization_id",
            "alarm_kind",
            filters={"alarm_kind": kind},
        )
        return self._expression(f"SUM({search})", period, kind)

    def _fleet_panels(self) -> list[tuple[str, list[cloudwatch.IWidget]]]:
        """Paneles de flota de U-03 (nº 18, U-03 §8.2 y NFR-GOB-58) sobre las métricas que el
        código publica. Las series por nodo son solo las de ``FLEET_PER_NODE_METRICS`` (contadores
        y medidores); los histogramas se separan por ruta, clase o tarea, nunca por nodo
        (NFR-GOB-13). «Alarmas abiertas por clase» y «certificados por vencer» leen
        ``fleet_alarms_open`` por ``alarm_kind`` sumando organizaciones (VIG-187)."""
        api, worker = API_SERVICE, WORKER_SERVICE
        m5 = FIVE_MINUTES
        (
            inventory,
            mute,
            queues,
            alarms,
            routes,
            records,
            revocation,
            certificates,
            orphans,
            tasks,
            pools,
        ) = FLEET_PANELS
        by_node = "node_id"
        reachable = self._search("fleet_node_reachable", api, "Maximum", m5, by_node)
        heartbeats = self._search("fleet_heartbeats_total", api, "Sum", m5, by_node)
        latency = [
            self._app_metric("http_server_duration_ms", api, "p95", m5, {"route": route})
            for route in NODE_ROUTE_P95_TARGETS_MS
        ]
        codes = [
            self._app_metric(
                "http_server_errors_total", api, "Sum", m5, {"route": route, "status_class": code}
            )
            for route in NODE_ROUTE_P95_TARGETS_MS
            for code in ("4xx", "5xx")
        ]
        task_duration = [
            self._app_metric(
                "periodic_task_duration_ms",
                worker,
                "p95",
                m5,
                {"task": task, "result": "succeeded"},
            )
            for task in FLEET_PERIODIC_TASKS
        ]
        task_failures = [
            self._app_metric(
                "periodic_task_duration_ms",
                worker,
                "SampleCount",
                m5,
                {"task": task, "result": "failed"},
            )
            for task in FLEET_PERIODIC_TASKS
        ]
        classes = ("node", "person")
        return [
            (
                "Flota (U-03, n 18)",
                [
                    self._graph(
                        inventory,
                        [self._expression(f"SUM({reachable})", m5, "nodos alcanzables")],
                    ),
                    self._graph(
                        mute,
                        [
                            self._expression(f"FILL({heartbeats}, 0)", m5),
                            self._expression(
                                self._search(
                                    "fleet_heartbeat_gap_seconds", api, "Maximum", m5, by_node
                                ),
                                m5,
                            ),
                        ],
                    ),
                    self._graph(
                        queues,
                        [
                            self._expression(
                                self._search(
                                    "fleet_node_queue_pending", api, "Maximum", m5, by_node
                                ),
                                m5,
                            )
                        ],
                    ),
                    self._graph(
                        alarms,
                        [self._open_alarms_by_class(kind, m5) for kind in FLEET_ALARM_KINDS],
                    ),
                    cloudwatch.GraphWidget(
                        title=routes, left=latency, right=codes, width=24, height=6
                    ),
                    self._graph(
                        records,
                        [
                            self._expression(
                                self._search(
                                    "node_requests_total", api, "Sum", m5, "result", "route"
                                ),
                                m5,
                            )
                        ],
                        width=12,
                    ),
                    self._graph(
                        revocation,
                        [
                            self._app_metric(
                                "revocation_list_seconds_to_expiry", worker, "Minimum", m5
                            ),
                            self._app_metric("revocation_list_entries", worker, "Maximum", m5),
                            self._app_metric("revocation_list_publish_failed", worker, "Sum", m5),
                        ],
                        width=12,
                    ),
                    self._graph(
                        certificates,
                        [self._open_alarms_by_class("certificate_expiring", m5)],
                        width=12,
                    ),
                    self._graph(
                        orphans,
                        [
                            self._expression(
                                self._search(
                                    "clip_grants_orphaned_total", worker, "Sum", ONE_HOUR, by_node
                                ),
                                ONE_HOUR,
                            )
                        ],
                    ),
                    cloudwatch.GraphWidget(
                        title=tasks, left=task_duration, right=task_failures, width=8, height=6
                    ),
                    self._graph(
                        "Tareas periodicas de U-03: edad del ultimo exito (s)",
                        [
                            self._app_metric(
                                "periodic_task_last_success_age_seconds",
                                worker,
                                "Maximum",
                                m5,
                                {"task": task},
                            )
                            for task in FLEET_PERIODIC_TASKS
                        ],
                    ),
                    cloudwatch.GraphWidget(
                        title=pools,
                        left=[
                            self._app_metric(name, api, "Maximum", m5, {"pool_class": klass})
                            for name in ("bulkhead_in_use", "bulkhead_size", "db_pool_in_use")
                            for klass in classes
                        ],
                        right=[
                            *(
                                self._app_metric(
                                    "bulkhead_rejected_total", api, "Sum", m5, {"pool_class": k}
                                )
                                for k in classes
                            ),
                            *(
                                self._app_metric(
                                    "bulkhead_wait_ms", api, "p95", m5, {"pool_class": k}
                                )
                                for k in classes
                            ),
                        ],
                        width=16,
                        height=6,
                    ),
                    self._alarm_status(
                        "Alarmas de flota",
                        [
                            key
                            for key in self.alarms
                            if key.startswith(("node-", "revocation-", "latency-node-"))
                            or key in ("bulkhead-person-saturated", "api-memory-high")
                            or (
                                key.startswith("periodic-task-stale-")
                                and key.removeprefix("periodic-task-stale-").replace("-", "_")
                                in FLEET_PERIODIC_TASKS
                            )
                        ],
                    ),
                ],
            )
        ]

    def _task_metrics(
        self, tasks: Sequence[str]
    ) -> tuple[list[cloudwatch.IMetric], list[cloudwatch.IMetric], list[cloudwatch.IMetric]]:
        """Duración p95 de los éxitos, fallos y edad del último éxito de cada tarea."""
        worker, m5 = WORKER_SERVICE, FIVE_MINUTES
        name = "periodic_task_duration_ms"
        return (
            [
                self._app_metric(name, worker, "p95", m5, {"task": t, "result": "succeeded"})
                for t in tasks
            ],
            [
                self._app_metric(name, worker, "SampleCount", m5, {"task": t, "result": "failed"})
                for t in tasks
            ],
            [
                self._app_metric(
                    "periodic_task_last_success_age_seconds", worker, "Maximum", m5, {"task": t}
                )
                for t in tasks
            ],
        )

    def _oldest_delivery(self, consumers: Sequence[str]) -> list[cloudwatch.IMetric]:
        """Entrega pendiente más vieja de cada consumidor de U-04 (``consumer``)."""
        return [
            self._app_metric(
                "outbox_oldest_pending_age_seconds",
                WORKER_SERVICE,
                "Maximum",
                FIVE_MINUTES,
                {"consumer": consumer},
            )
            for consumer in consumers
        ]

    def _email_metric(self, name: str, period: Duration) -> cloudwatch.IMetric:
        return cloudwatch.Metric(
            namespace="AWS/SES",
            metric_name=name,
            dimensions_map={CONFIGURATION_SET_DIMENSION: configuration_set_name(self.config)},
            statistic="Sum",
            period=period,
        )

    def _queue_metric(self, name: str, queue: str) -> cloudwatch.IMetric:
        return cloudwatch.Metric(
            namespace="AWS/SQS",
            metric_name=name,
            dimensions_map={"QueueName": queue},
            statistic="Maximum",
            period=FIVE_MINUTES,
            label=queue,
        )

    def _pending_emitter(self, names: str) -> cloudwatch.TextWidget:
        """Lo que el panel debería mostrar y el código aún no emite (A-64: sin panel vacío)."""
        return cloudwatch.TextWidget(
            markdown=f"Sin emisor en el codigo, fuera del panel (A-64; VIG-270): {names}.",
            width=24,
            height=1,
        )

    def _loop_panels(self) -> list[tuple[str, list[cloudwatch.IWidget]]]:
        """Los cinco paneles de NFR-LAZ-44 (nº 23, U-04 §10.4) sobre las métricas que el código
        emite (``LOOP_PANEL_METRICS``; A-64): las del módulo ``loop.observability.metrics`` que
        ya tienen emisor, las de U-02 por ``task`` y ``consumer`` para las tareas y los
        consumidores de U-04, y las del conjunto de configuración y las colas del correo. Lo que
        aún no tiene emisor queda escrito en el panel, sin widget."""
        api, worker = API_SERVICE, WORKER_SERVICE
        m5, month = FIVE_MINUTES, Duration.days(30)
        state, deferred, assistant, email, projection = LOOP_PANELS
        loop_duration, loop_failures, loop_age = self._task_metrics(LOOP_STATE_TASKS)
        package_duration, package_failures, package_age = self._task_metrics(("monthly_package",))
        _, _, email_age = self._task_metrics(LOOP_EMAIL_TASKS)
        projection_duration, projection_failures, projection_age = self._task_metrics(
            ("verify_loop_projection",)
        )

        def search(
            name: str, service: str, statistic: str, period: Duration, *dims: str
        ) -> cloudwatch.IMetric:
            """Una serie por valor de ``dims`` (estado, organización o sentido)."""
            return self._expression(self._search(name, service, statistic, period, *dims), period)

        by_state = "assistant_state"
        return [
            (
                state,
                [
                    self._graph("Tareas del lazo: edad del ultimo exito (s)", loop_age),
                    cloudwatch.GraphWidget(
                        title="Tareas del lazo: duracion p95 (ms) y fallos",
                        left=loop_duration,
                        right=loop_failures,
                        width=8,
                        height=6,
                    ),
                    self._graph(
                        "Consumidores del lazo: entrega pendiente mas vieja (s)",
                        self._oldest_delivery(("loop_projection", "review_queue")),
                    ),
                    self._pending_emitter(
                        "sin clasificar por planta y antiguedad (loop_unclassified_findings, "
                        "loop_unclassified_oldest_age_seconds) y vencidos por planta "
                        "(loop_overdue_findings)"
                    ),
                ],
            ),
            (
                deferred,
                [
                    cloudwatch.GraphWidget(
                        title="Exportaciones: entrega abierta mas vieja (s) y fallidas",
                        left=self._oldest_delivery((EXPORT_CONSUMER,)),
                        right=[self._app_metric("export_failed_total", worker, "Sum", m5)],
                        width=8,
                        height=6,
                    ),
                    cloudwatch.GraphWidget(
                        title="Paquete mensual: duracion p95 (ms) y fallos",
                        left=package_duration,
                        right=package_failures,
                        width=8,
                        height=6,
                    ),
                    self._graph("Paquete mensual: edad del ultimo exito (s)", package_age),
                    self._pending_emitter(
                        "exportaciones por estado, duracion, tamano e intentos "
                        "(export_requests_total, export_duration_ms, export_package_size_bytes)"
                    ),
                ],
            ),
            (
                assistant,
                [
                    self._graph(
                        "Solicitudes por estado",
                        [
                            search("assistant_requests_total", service, "Sum", m5, by_state)
                            for service in (api, worker)
                        ],
                    ),
                    self._graph(
                        "Latencia p95 por estado (ms)",
                        [search("assistant_request_duration_ms", worker, "p95", m5, by_state)],
                    ),
                    self._graph(
                        "Fichas por organizacion y mes",
                        [
                            search(
                                "assistant_tokens_total",
                                worker,
                                "Sum",
                                month,
                                "organization_id",
                                "token_direction",
                            )
                        ],
                    ),
                    cloudwatch.GraphWidget(
                        title="Presupuesto por organizacion, margen de tasa y no disponible",
                        left=[
                            search(
                                "assistant_token_budget_used_ratio",
                                service,
                                "Maximum",
                                ONE_HOUR,
                                "organization_id",
                            )
                            for service in (api, worker)
                        ],
                        right=[
                            self._app_metric(
                                "assistant_rate_limit_headroom_ratio", worker, "Minimum", m5
                            ),
                            self._app_metric("assistant_unavailable", worker, "Maximum", m5),
                        ],
                        width=24,
                        height=6,
                    ),
                ],
            ),
            (
                email,
                [
                    self._graph(
                        "Enviados, entregados, rebotes, quejas y rechazos",
                        [self._email_metric(name, ONE_HOUR) for name in EMAIL_PANEL_METRICS],
                    ),
                    cloudwatch.GraphWidget(
                        title="Cola de eventos de entrega: profundidad y antiguedad (s)",
                        left=[
                            self._queue_metric("ApproximateNumberOfMessagesVisible", queue)
                            for queue in (
                                email_events_name(self.config),
                                email_events_dlq_name(self.config),
                            )
                        ],
                        right=[
                            self._queue_metric(
                                "ApproximateAgeOfOldestMessage", email_events_name(self.config)
                            )
                        ],
                        width=8,
                        height=6,
                    ),
                    cloudwatch.GraphWidget(
                        title="Correo: tareas (edad del ultimo exito) y consumidores (s)",
                        left=email_age,
                        right=self._oldest_delivery(
                            ("notification_engine", "email_delivery_events")
                        ),
                        width=8,
                        height=6,
                    ),
                    self._pending_emitter(
                        "agrupados y degraded_app_only (email_messages_total por email_state)"
                    ),
                ],
            ),
            (
                projection,
                [
                    self._graph(
                        "Divergencias de proyeccion",
                        [
                            self._app_metric(
                                "loop_projection_divergences_total", worker, "Sum", ONE_HOUR
                            )
                        ],
                    ),
                    self._graph(
                        "verify_loop_projection: edad del ultimo exito (s)", projection_age
                    ),
                    cloudwatch.GraphWidget(
                        title="verify_loop_projection: duracion p95 (ms) y fallos",
                        left=projection_duration,
                        right=projection_failures,
                        width=8,
                        height=6,
                    ),
                    self._pending_emitter(
                        "edad de la ultima verificacion por planta (el catalogo no tiene metrica "
                        "por planta; verify_loop_projection corre por organizacion)"
                    ),
                ],
            ),
        ]


__all__ = [
    "APPLICATION_ALARM_METRICS",
    "LOG_PROCESSES",
    "NFR_NUC_38_ALARMS",
    "ObservabilityStack",
    "dashboard_name",
]
