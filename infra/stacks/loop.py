"""Pila ``vigia-loop`` de U-04 (infrastructure-design de U-04 §2.2; TASK-331, TASK-351 y TASK-332).

U-04 no despliega servicios: sus componentes corren en ``vigia-api`` y ``vigia-worker``. Esta pila
guarda los recursos propios de U-04 y se despliega la última, después de las de U-02. Lee lo que
necesita de U-02 por el contrato de ``stacks/outputs.py`` (pendiente nº 38), sin editar ninguna
pila ajena y sin ``Fn::ImportValue``:

- de ``vigia-foundation``: la red, las subredes ``vigia-app-a`` y ``-b``, ``sg-worker`` (solo como
  origen de la regla del grupo nuevo) y las claves ``vigia-evidence`` y ``vigia-secrets``;
- de ``vigia-data``: el depósito ``vigia-evidence``, por su nombre derivado del despliegue;
- de ``vigia-compute``: el rol ``vigia-worker-task`` por su ARN, inmutable.

La zona ``vigia-zone`` (``import_hosted_zone``) y el tema ``vigia-alerts`` (``import_alerts_topic``,
productor nuevo en ``vigia-foundation``) se importan donde se usan: la zona solo con los registros
DNS de ``pilot`` (``staging-<n>`` lee solo su nombre) y el tema por las alarmas (TASK-332) y la
suscripción de reputación: una importación sin uso deja en la plantilla un parámetro sin
referencia (W2001 de la validación).

Contiene (parte 1, TASK-331):

- Mensajería (§5): clave ``vigia-queue`` con rotación anual; tema ``vigia-email-events-<entorno>``
  cifrado con ella, con publicación solo del servicio de correo de la cuenta y desde el conjunto
  ``vigia-email-<entorno>`` (parte 2); cola estándar
  ``vigia-email-events-<entorno>`` (14 días, visibilidad 60 s, espera 20 s) con redirección a
  ``vigia-email-events-dlq-<entorno>`` tras 5 recepciones. Tema y colas deniegan las peticiones sin
  TLS. La cola admite ``SendMessage`` solo del tema y el consumo solo del rol del worker.
- Red (§3.2): punto privado ``vigia-vpce-sqs-<entorno>`` en las dos subredes de aplicación, con DNS
  privado y política acotada a las dos colas y al rol del worker; grupo ``sg-queue-endpoint`` con
  entrada 443 solo desde ``sg-worker`` y sin salida.
- Secreto (§8.2) ``vigia/<entorno>/assistant/api-key``, creado vacío, cifrado con ``vigia-secrets``
  y legible solo por el rol del worker. No existe en ``staging-<n>``.
- Política gestionada ``vigia-loop-worker`` (§9.1) adjunta al rol importado, con las sentencias de
  la tabla.

Contiene (parte 2, TASK-351):

- Correo (§4.1): identidad ``mail.<dominio>`` con DKIM gestionado por el servicio y remitente de
  rebotes ``bounce.mail.<dominio>`` que rechaza el envío si su MX falla. ``<dominio>`` es el nombre
  de ``vigia-zone`` que publica ``vigia-edge`` (``/vigia/<despliegue>/zone-name``, que a su vez
  sale de ``/vigia/domain``, P5): la pila no fija ningún dominio. Los siete registros sobre la
  zona importada: tres CNAME de DKIM (H-LAZ-3), TXT de SPF en ``mail.``, MX y TXT de SPF en
  ``bounce.mail.`` y TXT ``_dmarc.<dominio>`` con ``p=none`` e informes agregados a la dirección
  del parámetro de cuenta :data:`DMARC_REPORTS_PARAMETER`, que el dueño registra con el buzón de
  TASK-336. Subir a ``p=quarantine`` es un cambio posterior de :data:`DMARC_POLICY`, registrado con
  fecha en ``shared-infrastructure.md`` §6 (deployment-architecture de U-04 §4).
- Conjunto de configuración ``vigia-email-<entorno>`` (§4.2): reputación, TLS obligatorio en la
  entrega (``TlsPolicy: REQUIRE``; ``[hipótesis]`` sobre el nombre de la opción) y asociado a la
  identidad; destino ``cloudwatch`` (métricas con la dimensión del conjunto, base de las alarmas
  de TASK-332) y destino ``topic`` hacia ``vigia-email-events-<entorno>`` con entrega, rebote,
  queja y rechazo. Ningún destino recibe apertura ni clic (P3).
- Sentencia ``ses:SendEmail`` de ``vigia-loop-worker`` (§9.1) sobre la identidad y el conjunto,
  con ``ses:FromAddress = no-responder@mail.<dominio>``.
- ``staging-<n>`` (§2.1): identidad ``mail-<entorno>.<dominio>`` sin verificar, sin remitente de
  rebotes y sin registros en la zona. Lleva su propio nombre porque una identidad es única por
  cuenta y región: la de ``pilot`` ya existe. Desaparece con ``cdk destroy``.

Contiene (parte 3, TASK-332): las quince alarmas de §10.3 en 24 recursos, todas hacia
``vigia-alerts`` y solo esa acción (NFR-LAZ-28), con la histéresis de PAT-LAZ-MAN-01 (dos
evaluaciones en tasas, márgenes y duraciones; una en los hechos únicos) y el runbook que las
atiende en la descripción. ``periodic-task-stale-<tarea>`` se instancia por cada una de las diez
periódicas de ``U04_TASKS`` (las nueve de LC-LAZ-37 y ``email_delivery_events_poll`` de
TASK-325; ``rebuild_loop_projection`` es a demanda, U04-H-14). Toda alarma de aplicación vigila
una métrica que el código emite: donde la del módulo de nombres de U-04 aún no tiene emisor, la de
U-02 que el despachador o el planificador ya emiten por ``consumer`` o ``task``
(:data:`LOOP_METRIC_SUBSTITUTES`). Las tablas literales las contrasta la prueba cruzada del
backend (``tests/unit/test_laz_metrics_cross.py``).

Interruptores de las hipótesis (§14.2), todos apagados en ``config/pilot.py``:

- H-LAZ-9 (§5): ``email_topic_service_managed_key`` apagado deja el tema con ``vigia-queue``;
  encendido, el tema pasa a la clave del servicio y la cola conserva la del cliente.
- H-LAZ-10 (§9.1): ``ses_configuration_set_condition`` añade a ``ses:SendEmail`` la condición
  :data:`CONFIGURATION_SET_CONDITION_KEY` sobre ``vigia-email-<entorno>``. Si la clave no existe,
  IAM evalúa la condición como falsa y el envío se deniega: por eso queda apagado y el arranque
  del adaptador acota el conjunto mientras tanto.
- H-LAZ-11 (§4.2): ``ses_reputation_subscription`` crea la regla ``vigia-ses-reputation`` que lleva
  a ``vigia-alerts`` los eventos de AWS Health del servicio de correo (pausas y revisiones por
  reputación). Encenderla exige además que la política de ``vigia-alerts`` (``vigia-foundation``)
  admita esa regla, igual que ya admite ``backup-job-failed``: es un cambio aditivo de U-02.

``<entorno>`` es ``EnvironmentConfig.deployment``, el mismo valor que las tareas reciben en
``VIGIA_ENVIRONMENT`` y con el que ``loop.bootstrap`` compone los nombres (§6.3). En ``pilot`` el
tema, las colas y el secreto llevan ``RETAIN`` (deployment-architecture §4); en ``staging-<n>`` todo
se destruye con el entorno y ``vigia-queue`` con el borrado programado mínimo del servicio (§3).
La identidad, el conjunto y los registros siguen la misma regla: en ``pilot`` repetir la
verificación por DNS y la salida del arenero no es una opción (deployment-architecture §4).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from aws_cdk import Annotations, Duration, RemovalPolicy, Tags
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cloudwatch_actions
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_ses as ses
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from config import BOOTSTRAP_QUALIFIER, EnvironmentConfig
from stacks.base import VigiaStack
from stacks.compute import API_SERVICE, METRICS_NAMESPACE, WORKER_SERVICE
from stacks.data import BucketUsage
from stacks.foundation import HTTPS_PORT, KEY_ADMIN_ACTIONS, KEY_ROTATION_ADMIN_ACTIONS
from stacks.outputs import (
    Output,
    import_alerts_topic,
    import_app_network,
    import_hosted_zone,
    import_key,
    import_value,
    import_worker_security_group,
    import_worker_task_role_from_arn,
)

UNIT = "U-04"
ROUTE_TABLE_WARNING = "@aws-cdk/aws-ec2:noSubnetRouteTableId"

# --- Mensajería (§5) ----------------------------------------------------------------------

QUEUE_KEY = "queue"
QUEUE_KEY_ROTATION = Duration.days(365)
QUEUE_RETENTION = Duration.days(14)  # [objetivo propio]
QUEUE_VISIBILITY = Duration.seconds(60)
QUEUE_WAIT = Duration.seconds(20)
MAX_RECEIVE_COUNT = 5
# Servicios que cifran con ``vigia-queue``: el de correo publica en el tema y el de notificaciones
# entrega en la cola. ``GenerateDataKey`` y no ``GenerateDataKey*``: sin comodín de acción.
SERVICE_KEY_ACTIONS = ("kms:GenerateDataKey", "kms:Decrypt")
EMAIL_SERVICE = "ses.amazonaws.com"
NOTIFICATION_SERVICE = "sns.amazonaws.com"
# Clave que el servicio de notificaciones gestiona por la cuenta (contingencia de H-LAZ-9).
SNS_MANAGED_KEY_ALIAS = "alias/aws/sns"

# Acciones de consumo de las colas (§9.1) y las que admite el punto privado (§3.2).
CONSUME_ACTIONS = (
    "sqs:ReceiveMessage",
    "sqs:DeleteMessage",
    "sqs:ChangeMessageVisibility",
    "sqs:GetQueueAttributes",
    "sqs:GetQueueUrl",
)
ENDPOINT_ACTIONS = (*CONSUME_ACTIONS, "sqs:SendMessage")

# --- Red (§3.2) ---------------------------------------------------------------------------

QUEUE_ENDPOINT_GROUP = "sg-queue-endpoint"

# --- Política gestionada (§9.1) -----------------------------------------------------------

PACKAGE_PREFIXES = ("org/*/export/*", "org/*/monthly/*")
PACKAGE_WRITE_ACTIONS = ("s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts")
MULTIPART_LIST_PREFIX = "org/*"

# --- Correo (§4) --------------------------------------------------------------------------

MAIL_SUBDOMAIN = "mail"
BOUNCE_SUBDOMAIN = "bounce"
SENDER_LOCAL_PART = "no-responder"
# Informes agregados de DMARC: parámetro SSM de la cuenta que el dueño registra con su buzón
# (TASK-336), igual que ``/vigia/alerts-email``. El repositorio no guarda ninguna dirección.
DMARC_REPORTS_PARAMETER = "/vigia/dmarc-reports-email"
# ``p=none`` durante el piloto; sube a ``p=quarantine`` con dos semanas de alineación estable,
# como cambio registrado (§4.1; deployment-architecture §4).
DMARC_POLICY = "none"
EMAIL_SERVICE_SPF = "v=spf1 include:amazonses.com ~all"
MAIL_FROM_MX_PRIORITY = 10
# Tipos de evento del destino ``topic`` (§4.2 y §5): ni apertura ni clic (P3).
TOPIC_EVENTS = (
    ses.EmailSendingEvent.DELIVERY,
    ses.EmailSendingEvent.BOUNCE,
    ses.EmailSendingEvent.COMPLAINT,
    ses.EmailSendingEvent.REJECT,
)
# El destino ``cloudwatch`` añade los envíos: sin ellos no hay tasa de rebotes ni de quejas.
CLOUDWATCH_EVENTS = (ses.EmailSendingEvent.SEND, *TOPIC_EVENTS)
# Etiqueta que el servicio pone a todo mensaje enviado con un conjunto de configuración.
CONFIGURATION_SET_DIMENSION = "ses:configuration-set"
# H-LAZ-10: nombre supuesto de la clave de condición del conjunto de configuración.
CONFIGURATION_SET_CONDITION_KEY = "ses:ConfigurationSetName"
# H-LAZ-11: eventos de AWS Health del servicio de correo.
REPUTATION_EVENT_PATTERN_SERVICE = "SES"
REPUTATION_RULE = "ses-reputation"

# --- Alarmas (§10.3; NFR-LAZ-28 y 42; PAT-LAZ-MAN-01) -------------------------------------
# Tablas literales: la prueba cruzada del backend (``tests/unit/test_laz_metrics_cross.py``) las
# lee con ``ast.literal_eval`` y ``tests/test_loop_alarms.py`` las contrasta con la síntesis.

LOOP_PERIODIC_TASK_MAX_AGE_SECONDS: Final = {
    "verify_loop_projection": 93_600,  # 26 h
    "review_reminders": 93_600,
    "evaluate_deadlines": 93_600,
    "unclosable_review_due": 93_600,
    "metrics_materialize": 3_024_000,  # 35 días [objetivo propio]
    "monthly_package": 3_024_000,
    "adoption_check": 93_600,
    "email_delivery_events_poll": 93_600,
    "email_grouping": 93_600,
    "digest_weekly": 777_600,  # 9 días [objetivo propio]
}
"""Las diez periódicas de ``U04_TASKS`` con su ventana: ``periodic-task-stale-<tarea>`` por cada
una. ``rebuild_loop_projection`` corre a demanda y no tiene alarma (U04-H-14)."""

LOOP_CONSUMERS: Final = (
    "loop_projection",
    "review_queue",
    "notification_engine",
    "assistant_worker",
    "export_worker",
    "email_delivery_events",
)
"""Los seis consumidores de ``U04_CONSUMERS``: ``loop-dead-letter`` suma su cola muerta."""

EXPORT_CONSUMER: Final = "export_worker"
"""La entrega de ``export_requested`` queda abierta mientras dura la exportación (TASK-317)."""

LOOP_APPLICATION_ALARM_METRICS: Final = {
    "loop-dead-letter": (("dead_letter_created_total", ("consumer",)),),
    "assistant-unavailable": (("assistant_unavailable", ()),),
    "export-stuck": (("outbox_oldest_pending_age_seconds", ("consumer",)),),
    "export-failed": (("export_failed_total", ()),),
    "loop-projection-divergence": (("loop_projection_divergences_total", ()),),
    "periodic-task-stale-verify-loop-projection": (
        ("periodic_task_last_success_age_seconds", ("task",)),
    ),
    "periodic-task-stale-review-reminders": (
        ("periodic_task_last_success_age_seconds", ("task",)),
    ),
    "periodic-task-stale-evaluate-deadlines": (
        ("periodic_task_last_success_age_seconds", ("task",)),
    ),
    "periodic-task-stale-unclosable-review-due": (
        ("periodic_task_last_success_age_seconds", ("task",)),
    ),
    "periodic-task-stale-metrics-materialize": (
        ("periodic_task_last_success_age_seconds", ("task",)),
    ),
    "periodic-task-stale-monthly-package": (("periodic_task_last_success_age_seconds", ("task",)),),
    "periodic-task-stale-adoption-check": (("periodic_task_last_success_age_seconds", ("task",)),),
    "periodic-task-stale-email-delivery-events-poll": (
        ("periodic_task_last_success_age_seconds", ("task",)),
    ),
    "periodic-task-stale-email-grouping": (("periodic_task_last_success_age_seconds", ("task",)),),
    "periodic-task-stale-digest-weekly": (("periodic_task_last_success_age_seconds", ("task",)),),
    "assistant-token-budget": (("assistant_token_budget_used_ratio", ()),),
    "assistant-rate-margin": (("assistant_rate_limit_headroom_ratio", ()),),
}
"""Alarma → métricas de ``Vigia/Platform`` que vigila, con los atributos que usa como dimensión
además de ``service`` y ``environment``. Solo métricas que el código emite (A-64)."""

LOOP_SERVICE_ALARM_METRICS: Final = {
    "email-bounce-rate": (("AWS/SES", "Bounce"), ("AWS/SES", "Send")),
    "email-events-dlq": (("AWS/SQS", "ApproximateNumberOfMessagesVisible"),),
    "email-events-queue-age": (("AWS/SQS", "ApproximateAgeOfOldestMessage"),),
    "email-send-quota-high": (("AWS/SES", "Send"),),
    "email-bounce-rate-warning": (("AWS/SES", "Bounce"), ("AWS/SES", "Send")),
    "email-complaint-rate-warning": (("AWS/SES", "Complaint"), ("AWS/SES", "Send")),
    "email-complaint-rate": (("AWS/SES", "Complaint"), ("AWS/SES", "Send")),
}
"""Alarma → métricas de servicio (espacio, nombre): las del conjunto de configuración, la cuenta
del servicio de correo y las dos colas."""

LOOP_CONDITION_ALARMS: Final = {
    "loop_dead_letter": ("loop-dead-letter",),
    "assistant_unavailable": ("assistant-unavailable",),
    "export_stuck_or_failed": ("export-stuck", "export-failed"),
    "email_bounce_rate": ("email-bounce-rate",),
    "loop_projection_divergence": ("loop-projection-divergence",),
    "loop_periodic_task_stale": (
        "periodic-task-stale-verify-loop-projection",
        "periodic-task-stale-review-reminders",
        "periodic-task-stale-evaluate-deadlines",
        "periodic-task-stale-unclosable-review-due",
        "periodic-task-stale-metrics-materialize",
        "periodic-task-stale-monthly-package",
        "periodic-task-stale-adoption-check",
        "periodic-task-stale-email-delivery-events-poll",
        "periodic-task-stale-email-grouping",
        "periodic-task-stale-digest-weekly",
    ),
    "assistant_token_budget": ("assistant-token-budget",),
    "assistant_rate_limit_headroom": ("assistant-rate-margin",),
}
"""Cada condición de NFR-LAZ-42 (``LOOP_ALARM_CONDITIONS`` del backend) y sus alarmas."""

LOOP_METRIC_SUBSTITUTES: Final = {
    "loop_dead_letter_entries_total": ("dead_letter_created_total",),
    "export_stuck_or_failed": ("outbox_oldest_pending_age_seconds", "export_failed_total"),
    "email_messages_total": ("AWS/SES:Bounce", "AWS/SES:Send"),
    "loop_periodic_task_last_success_age_seconds": ("periodic_task_last_success_age_seconds",),
}
"""Métrica de una condición de NFR-LAZ-42 que la alarma no vigila → lo que vigila en su lugar:

- la cola muerta y la edad del último éxito, sin emisor en U-04: las de U-02 que el despachador y
  el planificador ya emiten para los consumidores y las tareas de U-04 (por ``consumer`` y
  ``task``);
- la exportación: más de 60 min con su entrega abierta y el contador de dos fallos seguidos;
- la tasa de rebotes: las métricas del conjunto de configuración (origen de §10.3)."""

EXPORT_STUCK_SECONDS: Final = 3_600
ASSISTANT_UNAVAILABLE_MINUTES: Final = 30
ASSISTANT_TOKEN_BUDGET_RATIO: Final = 0.8
ASSISTANT_RATE_MARGIN_RATIO: Final = 0.2
EMAIL_BOUNCE_RATE_PERCENT: Final = 5.0  # H-LAZ-5
EMAIL_COMPLAINT_RATE_PERCENT: Final = 0.1  # H-LAZ-5
# Informativas al 60 % de cada umbral, escritas sin redondeo de coma flotante.
EMAIL_BOUNCE_WARNING_PERCENT: Final = 3.0
EMAIL_COMPLAINT_WARNING_PERCENT: Final = 0.06
EMAIL_EVENTS_MAX_AGE_SECONDS: Final = 900  # 15 min [objetivo propio]
EMAIL_DAILY_SEND_QUOTA: Final = 50_000
"""Cuota diaria que se pide al salir del arenero (§4.3 y §11) ``[hipótesis]``: el runbook 7.5
(parte A) la sustituye por la concedida."""
EMAIL_SEND_QUOTA_FRACTION: Final = 0.8

RUNBOOK_DEAD_LETTER = "Runbook 7.2 de U-04, camino A (reproceso de cola muerta del lazo)"
RUNBOOK_EMAIL_DLQ = "Runbook 7.2 de U-04, camino B (cola muerta gestionada del correo)"
RUNBOOK_PROJECTION = "Runbook 7.1 de U-04 (reconstruccion de la proyeccion por planta)"
RUNBOOK_ASSISTANT = "Runbook 7.4 de U-04 (proveedor del asistente)"
RUNBOOK_EMAIL = "Runbook 7.5 de U-04 (reputacion y cuotas del correo)"
RUNBOOK_EXPORT = "Runbook 7.6 de U-04 (exportacion atascada o fallida)"
TASK_RUNBOOKS: Final = {
    "verify_loop_projection": RUNBOOK_PROJECTION,
    "monthly_package": RUNBOOK_EXPORT,
    "email_delivery_events_poll": RUNBOOK_EMAIL_DLQ,
}
"""Tareas con runbook propio; las demás remiten a los registros de ``vigia-worker``."""


def email_events_name(config: EnvironmentConfig) -> str:
    """``vigia-email-events-<entorno>``: nombre del tema y de la cola (§5 y §6.3)."""
    return f"vigia-email-events-{config.deployment}"


def email_events_dlq_name(config: EnvironmentConfig) -> str:
    """``vigia-email-events-dlq-<entorno>`` (§5)."""
    return f"vigia-email-events-dlq-{config.deployment}"


def queue_endpoint_name(config: EnvironmentConfig) -> str:
    """``vigia-vpce-sqs-<entorno>`` (§3.2)."""
    return f"vigia-vpce-sqs-{config.deployment}"


def assistant_secret_name(config: EnvironmentConfig) -> str:
    """``vigia/<entorno>/assistant/api-key`` (§8.2 y §6.3)."""
    return f"vigia/{config.deployment}/assistant/api-key"


def loop_worker_policy_name(config: EnvironmentConfig) -> str:
    """``vigia-loop-worker`` con el sufijo del despliegue, como los roles de §8."""
    return config.resource_name("loop-worker")


def configuration_set_name(config: EnvironmentConfig) -> str:
    """``vigia-email-<entorno>`` (§4.2 y §6.3)."""
    return f"vigia-email-{config.deployment}"


def reputation_rule_name(config: EnvironmentConfig) -> str:
    """``vigia-ses-reputation`` con el sufijo del despliegue (H-LAZ-11)."""
    return config.resource_name(REPUTATION_RULE)


def stale_alarm_key(task: str) -> str:
    """``email_grouping`` → ``periodic-task-stale-email-grouping`` (como las de U-02 y U-03)."""
    return f"periodic-task-stale-{task.replace('_', '-')}"


@dataclass(frozen=True)
class LoopAlarm:
    """Una alarma de U-04: métrica o expresión, umbral y evaluaciones (PAT-LAZ-MAN-01)."""

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
FIVE_MINUTES = Duration.minutes(5)
ONE_HOUR = Duration.hours(1)
ONE_DAY = Duration.days(1)


class LoopStack(VigiaStack):
    """Pila ``vigia-loop`` (infrastructure-design de U-04 §2.2)."""

    key = "loop"
    summary = "correo, cola de eventos de entrega, secreto del asistente y permisos de U-04"
    unit = UNIT

    def __init__(
        self, scope: Construct, config: EnvironmentConfig, *, tags: Mapping[str, str]
    ) -> None:
        super().__init__(scope, config, tags=tags)
        # La etiqueta de unidad de la aplicación es ``U-02``; esta pila es de U-04.
        Tags.of(self).add("unit", UNIT)
        self.removal = RemovalPolicy.RETAIN if config.loop.retained else RemovalPolicy.DESTROY

        # Importaciones (pendiente nº 38).
        self.worker_role = import_worker_task_role_from_arn(self, "WorkerTaskRole", config)
        self.vpc = import_app_network(self, "AppNetwork", config)
        self.worker_group = import_worker_security_group(self, "SgWorker", config)
        self.evidence_key = import_key(self, "EvidenceKey", config, Output.EVIDENCE_KEY_ARN)
        # Solo cifra el secreto del asistente, que ``staging-<n>`` no tiene.
        self.secrets_key = (
            import_key(self, "SecretsKey", config, Output.SECRETS_KEY_ARN)
            if config.loop.assistant_secret
            else None
        )
        # El interfaz de punto privado no usa las tablas de rutas de las subredes importadas.
        for subnet in self.vpc.private_subnets:
            Annotations.of(subnet).acknowledge_warning(
                ROUTE_TABLE_WARNING, "el punto privado de interfaz no usa la tabla de rutas"
            )
        evidence_bucket = config.bucket_name(BucketUsage.EVIDENCE.value, self.account)
        self.evidence_bucket_arn = f"arn:{self.partition}:s3:::{evidence_bucket}"

        # Mensajería, red, secreto e identidad.
        self.queue_key = self._queue_key()
        self.email_events_topic = self._topic()
        self.email_events_dlq = self._queue(email_events_dlq_name(config), "DeadLetterQueue")
        self.email_events_queue = self._queue(
            email_events_name(config), "Queue", dead_letter_queue=self.email_events_dlq
        )
        self._allow_topic_delivery()
        self._subscription()
        self.queue_endpoint_group = self._queue_endpoint_group()
        self.queue_endpoint = self._queue_endpoint()
        self.assistant_secret = self._assistant_secret() if config.loop.assistant_secret else None
        self.zone, self.domain = self._zone_and_domain()
        # ``mail-<entorno>.<dominio>`` en ``staging-<n>``: la identidad de ``pilot`` ya existe.
        mail = MAIL_SUBDOMAIN if self.zone is not None else f"{MAIL_SUBDOMAIN}-{config.deployment}"
        self.mail_domain = f"{mail}.{self.domain}"
        self.sender = f"{SENDER_LOCAL_PART}@{self.mail_domain}"
        self.configuration_set = self._configuration_set()
        self.email_identity = self._email_identity()
        if self.zone is not None:
            self._email_records(self.zone)
        self.alerts_topic = import_alerts_topic(self, "AlertsTopic", config)
        if config.loop.ses_reputation_subscription:
            self._reputation_subscription()
        self.loop_worker_policy = self._loop_worker_policy()
        self.alarms: dict[str, cloudwatch.Alarm] = {}
        self._application_alarms()
        self._service_alarms()

    # --- Principales ----------------------------------------------------------------------

    @property
    def _cfn_execution_role_arn(self) -> str:
        """Rol de ejecución de CloudFormation del arranque ``vigia``: administra la clave y es la
        identidad administrativa que puede borrar el secreto (mismo criterio que §7.1)."""
        name = f"cdk-{BOOTSTRAP_QUALIFIER}-cfn-exec-role-{self.account}-{self.region}"
        return self.format_arn(
            service="iam", region="", account=self.account, resource="role", resource_name=name
        )

    def _service(self, service: str) -> iam.PolicyStatement:
        """Uso de ``vigia-queue`` por un servicio, solo en nombre de esta cuenta."""
        sid = "EmailServiceUsesTheKey" if service == EMAIL_SERVICE else "NotificationsUseTheKey"
        return iam.PolicyStatement(
            sid=sid,
            principals=[iam.ServicePrincipal(service)],
            actions=list(SERVICE_KEY_ACTIONS),
            resources=["*"],
            conditions={"StringEquals": {"aws:SourceAccount": self.account}},
        )

    # --- Mensajería -----------------------------------------------------------------------

    def _queue_key(self) -> kms.Key:
        """``vigia-queue`` (§8.1): política explícita, sin la sentencia por defecto de CDK."""
        config = self.config
        statements = [
            iam.PolicyStatement(
                sid="KeyAdministration",
                principals=[iam.AccountRootPrincipal()],
                actions=[*KEY_ADMIN_ACTIONS, *KEY_ROTATION_ADMIN_ACTIONS],
                resources=["*"],
                conditions={"ArnEquals": {"aws:PrincipalArn": [self._cfn_execution_role_arn]}},
            ),
            # Como en ``vigia-foundation``: el rol se nombra por ``aws:PrincipalArn``.
            iam.PolicyStatement(
                sid="WorkerDecryptsThroughSqs",
                principals=[iam.AccountRootPrincipal()],
                actions=["kms:Decrypt"],
                resources=["*"],
                conditions={
                    "ArnEquals": {"aws:PrincipalArn": [self.worker_role.role_arn]},
                    "StringEquals": {"kms:ViaService": f"sqs.{self.region}.amazonaws.com"},
                },
            ),
            self._service(NOTIFICATION_SERVICE),
        ]
        if not config.loop.email_topic_service_managed_key:
            statements.append(self._service(EMAIL_SERVICE))
        return kms.Key(
            self,
            "QueueKey",
            alias=f"alias/{config.resource_name(QUEUE_KEY)}",
            description="vigia-queue: tema y colas de eventos de entrega del correo (U-04 8.1)",
            policy=iam.PolicyDocument(statements=statements),
            enable_key_rotation=True,
            rotation_period=QUEUE_KEY_ROTATION,
            removal_policy=self.removal,
            pending_window=Duration.days(config.loop.queue_key_pending_window_days),
        )

    def _topic(self) -> sns.Topic:
        config = self.config
        master_key: kms.IKey = self.queue_key
        if config.loop.email_topic_service_managed_key:
            master_key = kms.Alias.from_alias_name(self, "SnsManagedKey", SNS_MANAGED_KEY_ALIAS)
        topic = sns.Topic(
            self,
            "EmailEventsTopic",
            topic_name=email_events_name(config),
            display_name="Vigia eventos de entrega del correo",
            master_key=master_key,
            enforce_ssl=True,
        )
        topic.apply_removal_policy(self.removal)
        return topic

    def _queue(
        self, name: str, construct_id: str, *, dead_letter_queue: sqs.IQueue | None = None
    ) -> sqs.Queue:
        queue = sqs.Queue(
            self,
            construct_id,
            queue_name=name,
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=self.queue_key,
            retention_period=QUEUE_RETENTION,
            visibility_timeout=QUEUE_VISIBILITY,
            receive_message_wait_time=QUEUE_WAIT,
            dead_letter_queue=(
                sqs.DeadLetterQueue(max_receive_count=MAX_RECEIVE_COUNT, queue=dead_letter_queue)
                if dead_letter_queue is not None
                else None
            ),
            enforce_ssl=True,
            removal_policy=self.removal,
        )
        queue.add_to_resource_policy(
            iam.PolicyStatement(
                sid="WorkerConsumes",
                principals=[iam.ArnPrincipal(self.worker_role.role_arn)],
                actions=list(CONSUME_ACTIONS),
                resources=[queue.queue_arn],
            )
        )
        return queue

    def _allow_topic_delivery(self) -> None:
        queue = self.email_events_queue
        queue.add_to_resource_policy(
            iam.PolicyStatement(
                sid="TopicDelivers",
                principals=[iam.ServicePrincipal(NOTIFICATION_SERVICE)],
                actions=["sqs:SendMessage"],
                resources=[queue.queue_arn],
                conditions={"ArnEquals": {"aws:SourceArn": self.email_events_topic.topic_arn}},
            )
        )

    def _subscription(self) -> sns.Subscription:
        """Una sola suscripción, la de la cola, con entrega en bruto (§5). ``sns.Subscription`` y
        no ``SqsSubscription``: las políticas de la cola y de la clave las escribe esta pila."""
        return sns.Subscription(
            self,
            "EmailEventsSubscription",
            topic=self.email_events_topic,
            protocol=sns.SubscriptionProtocol.SQS,
            endpoint=self.email_events_queue.queue_arn,
            raw_message_delivery=True,
        )

    # --- Red ------------------------------------------------------------------------------

    def _queue_endpoint_group(self) -> ec2.SecurityGroup:
        group = ec2.SecurityGroup(
            self,
            "SgQueueEndpoint",
            vpc=self.vpc,
            security_group_name=QUEUE_ENDPOINT_GROUP,
            description=f"{QUEUE_ENDPOINT_GROUP} (U-04 infrastructure-design 3.2)",
            allow_all_outbound=False,
        )
        # La regla vive en este grupo: ``sg-worker`` (U-02) no cambia.
        group.add_ingress_rule(self.worker_group, ec2.Port.tcp(HTTPS_PORT), "443 desde sg-worker")
        return group

    def _queue_endpoint(self) -> ec2.InterfaceVpcEndpoint:
        endpoint = ec2.InterfaceVpcEndpoint(
            self,
            "QueueEndpoint",
            vpc=self.vpc,
            service=ec2.InterfaceVpcEndpointAwsService.SQS,
            subnets=ec2.SubnetSelection(subnets=self.vpc.private_subnets),
            security_groups=[self.queue_endpoint_group],
            private_dns_enabled=True,
            # Sin la regla 443 desde toda la red que CDK añade por defecto.
            open=False,
        )
        endpoint.add_to_policy(
            iam.PolicyStatement(
                sid="WorkerUsesTheEmailEventsQueues",
                principals=[iam.ArnPrincipal(self.worker_role.role_arn)],
                actions=list(ENDPOINT_ACTIONS),
                resources=[self.email_events_queue.queue_arn, self.email_events_dlq.queue_arn],
            )
        )
        Tags.of(endpoint).add("Name", queue_endpoint_name(self.config), priority=200)
        return endpoint

    # --- Secreto --------------------------------------------------------------------------

    def _assistant_secret(self) -> secretsmanager.Secret:
        """Creado vacío: el dueño escribe la clave con el runbook 7.4."""
        secret = secretsmanager.Secret(
            self,
            "AssistantApiKey",
            secret_name=assistant_secret_name(self.config),
            description="Clave del proveedor del asistente; vacia hasta el runbook 7.4 (U-04 8.2)",
            encryption_key=self.secrets_key,
            removal_policy=self.removal,
        )
        # Sin valor generado: CDK genera uno por defecto y el secreto nace vacío.
        resource = secret.node.default_child
        if not isinstance(resource, secretsmanager.CfnSecret):
            raise TypeError("El secreto del asistente no expone su recurso de CloudFormation")
        resource.add_property_deletion_override("GenerateSecretString")
        worker = self.worker_role.role_arn
        secret.add_to_resource_policy(
            iam.PolicyStatement(
                sid="WorkerReads",
                principals=[iam.ArnPrincipal(worker)],
                actions=["secretsmanager:GetSecretValue"],
                resources=["*"],
            )
        )
        secret.add_to_resource_policy(
            iam.PolicyStatement(
                sid="NobodyElseReads",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["secretsmanager:GetSecretValue"],
                resources=["*"],
                conditions={"ArnNotEquals": {"aws:PrincipalArn": worker}},
            )
        )
        secret.add_to_resource_policy(
            iam.PolicyStatement(
                sid="OnlyAdministrationDeletes",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["secretsmanager:DeleteSecret"],
                resources=["*"],
                conditions={"ArnNotEquals": {"aws:PrincipalArn": self._cfn_execution_role_arn}},
            )
        )
        return secret

    # --- Correo (§4) ----------------------------------------------------------------------

    def _configuration_set_arn(self, configuration_set: ses.ConfigurationSet) -> str:
        return self.format_arn(
            service="ses",
            resource="configuration-set",
            resource_name=configuration_set.configuration_set_name,
        )

    def _zone_and_domain(self) -> tuple[route53.IHostedZone | None, str]:
        """``vigia-zone`` y su nombre. Con registros, la zona entera (identificador y nombre); en
        ``staging-<n>``, solo el nombre que publica el despliegue que la aloja."""
        config = self.config
        if config.loop.email_dns:
            zone = import_hosted_zone(self, "Zone", config)
            return zone, zone.zone_name
        source = config.deployment if config.hosted_zone_owned else config.hosted_zone_source
        return None, import_value(self, config, Output.HOSTED_ZONE_NAME, deployment=source)

    def _configuration_set(self) -> ses.ConfigurationSet:
        config = self.config
        configuration_set = ses.ConfigurationSet(
            self,
            "EmailConfigurationSet",
            configuration_set_name=configuration_set_name(config),
            reputation_metrics=True,
            sending_enabled=True,
            # [hipótesis] RequireTls de §4.2 es ``DeliveryOptions.TlsPolicy: REQUIRE``.
            tls_policy=ses.ConfigurationSetTlsPolicy.REQUIRE,
        )
        configuration_set.apply_removal_policy(self.removal)
        ses.ConfigurationSetEventDestination(
            self,
            "EmailEventsToCloudWatch",
            configuration_set=configuration_set,
            configuration_set_event_destination_name="cloudwatch",
            destination=ses.EventDestination.cloud_watch_dimensions(
                [
                    ses.CloudWatchDimension(
                        name=CONFIGURATION_SET_DIMENSION,
                        source=ses.CloudWatchDimensionSource.MESSAGE_TAG,
                        default_value=configuration_set_name(config),
                    )
                ]
            ),
            events=list(CLOUDWATCH_EVENTS),
        )
        self._topic_destination(configuration_set)
        return configuration_set

    def _topic_destination(self, configuration_set: ses.ConfigurationSet) -> None:
        """Destino ``topic``. En L1: el de L2 añade al tema su propia sentencia de publicación y
        la política del tema la escribe esta pila, con una sola sentencia para el servicio."""
        topic = self.email_events_topic
        topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="EmailServicePublishes",
                principals=[iam.ServicePrincipal(EMAIL_SERVICE)],
                actions=["sns:Publish"],
                resources=[topic.topic_arn],
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnEquals": {"aws:SourceArn": self._configuration_set_arn(configuration_set)},
                },
            )
        )
        destination = ses.CfnConfigurationSetEventDestination(
            self,
            "EmailEventsToTopic",
            configuration_set_name=configuration_set.configuration_set_name,
            event_destination=ses.CfnConfigurationSetEventDestination.EventDestinationProperty(
                name="topic",
                enabled=True,
                matching_event_types=[_event_type(event) for event in TOPIC_EVENTS],
                sns_destination=ses.CfnConfigurationSetEventDestination.SnsDestinationProperty(
                    topic_arn=topic.topic_arn
                ),
            ),
        )
        # El servicio comprueba al crear el destino que puede publicar en el tema.
        policy = topic.node.try_find_child("Policy")
        if policy is not None:
            destination.node.add_dependency(policy)

    def _email_identity(self) -> ses.EmailIdentity:
        """``mail.<dominio>`` con DKIM gestionado; en ``staging-<n>``, sin remitente de rebotes:
        sin su MX, el comportamiento «rechazar» bloquearía cualquier envío."""
        with_dns = self.config.loop.email_dns
        identity = ses.EmailIdentity(
            self,
            "EmailIdentity",
            identity=ses.Identity.domain(self.mail_domain),
            configuration_set=self.configuration_set,
            dkim_identity=ses.DkimIdentity.easy_dkim(),
            dkim_signing=True,
            mail_from_domain=f"{BOUNCE_SUBDOMAIN}.{self.mail_domain}" if with_dns else None,
            mail_from_behavior_on_mx_failure=(
                ses.MailFromBehaviorOnMxFailure.REJECT_MESSAGE if with_dns else None
            ),
        )
        identity.apply_removal_policy(self.removal)
        return identity

    def _email_records(self, zone: route53.IHostedZone) -> None:
        """Los siete registros de §4.1 sobre la zona importada; ``vigia-edge`` no cambia."""
        identity = self.email_identity
        records: list[route53.RecordSet] = [
            # El servicio devuelve nombres completos: el punto final evita el sufijo de la zona.
            route53.CnameRecord(
                self,
                f"DkimRecord{index}",
                zone=zone,
                record_name=f"{name}.",
                domain_name=value,
            )
            for index, (name, value) in enumerate(
                (
                    (identity.dkim_dns_token_name1, identity.dkim_dns_token_value1),
                    (identity.dkim_dns_token_name2, identity.dkim_dns_token_value2),
                    (identity.dkim_dns_token_name3, identity.dkim_dns_token_value3),
                ),
                start=1,
            )
        ]
        bounce = f"{BOUNCE_SUBDOMAIN}.{MAIL_SUBDOMAIN}"
        records += [
            route53.TxtRecord(
                self,
                "MailSpfRecord",
                zone=zone,
                record_name=MAIL_SUBDOMAIN,
                values=[EMAIL_SERVICE_SPF],
            ),
            route53.MxRecord(
                self,
                "BounceMxRecord",
                zone=zone,
                record_name=bounce,
                values=[
                    route53.MxRecordValue(
                        host_name=f"feedback-smtp.{self.region}.amazonses.com",
                        priority=MAIL_FROM_MX_PRIORITY,
                    )
                ],
            ),
            route53.TxtRecord(
                self,
                "BounceSpfRecord",
                zone=zone,
                record_name=bounce,
                values=[EMAIL_SERVICE_SPF],
            ),
            route53.TxtRecord(
                self,
                "DmarcRecord",
                zone=zone,
                record_name="_dmarc",
                values=[f"v=DMARC1; p={DMARC_POLICY}; rua=mailto:{self._dmarc_reports()}"],
            ),
        ]
        for record in records:
            record.apply_removal_policy(self.removal)

    def _dmarc_reports(self) -> str:
        return ssm.StringParameter.value_for_string_parameter(self, DMARC_REPORTS_PARAMETER)

    def _reputation_subscription(self) -> events.Rule:
        """H-LAZ-11: eventos de AWS Health del servicio de correo hacia ``vigia-alerts``."""
        rule = events.Rule(
            self,
            "EmailReputationRule",
            rule_name=reputation_rule_name(self.config),
            description="Eventos de reputacion del correo hacia vigia-alerts (U-04 4.2, H-LAZ-11)",
            event_pattern=events.EventPattern(
                source=["aws.health"],
                detail_type=["AWS Health Event"],
                detail={"service": [REPUTATION_EVENT_PATTERN_SERVICE]},
            ),
        )
        rule.add_target(targets.SnsTopic(self.alerts_topic))
        return rule

    # --- Identidad (§9.1) -----------------------------------------------------------------

    def _send_email_statement(self) -> iam.PolicyStatement:
        """``ses:SendEmail`` sobre la identidad y el conjunto: con un conjunto, la versión 2 del
        servicio autoriza el envío también sobre el ARN del conjunto."""
        conditions: dict[str, dict[str, object]] = {
            "StringEquals": {"ses:FromAddress": self.sender}
        }
        if self.config.loop.ses_configuration_set_condition:
            conditions["StringEquals"][CONFIGURATION_SET_CONDITION_KEY] = configuration_set_name(
                self.config
            )
        return _statement(
            "SendEmail",
            ["ses:SendEmail"],
            [
                self.email_identity.email_identity_arn,
                self._configuration_set_arn(self.configuration_set),
            ],
            conditions,
        )

    def _package_objects(self) -> list[str]:
        return [f"{self.evidence_bucket_arn}/{prefix}" for prefix in PACKAGE_PREFIXES]

    def loop_worker_statements(self) -> list[iam.PolicyStatement]:
        """Sentencias de ``vigia-loop-worker`` (tabla de §9.1)."""
        queues = [self.email_events_queue.queue_arn, self.email_events_dlq.queue_arn]
        statements = [
            _statement(
                "WritePackages",
                PACKAGE_WRITE_ACTIONS,
                self._package_objects(),
                {
                    "StringEquals": {
                        "s3:x-amz-server-side-encryption": "aws:kms",
                        "s3:x-amz-server-side-encryption-aws-kms-key-id": (
                            self.evidence_key.key_arn
                        ),
                    }
                },
            ),
            _statement(
                "ListMultipartUploads",
                ["s3:ListBucketMultipartUploads"],
                [self.evidence_bucket_arn],
                {"StringLike": {"s3:prefix": MULTIPART_LIST_PREFIX}},
            ),
            _statement("ReadOwnPackages", ["s3:GetObject"], self._package_objects()),
            self._send_email_statement(),
            _statement("ConsumeEmailEvents", CONSUME_ACTIONS, queues),
            _statement(
                "RedriveFromDeadLetterQueue",
                ["sqs:SendMessage"],
                [self.email_events_queue.queue_arn],
            ),
            _statement(
                "QueueKeyThroughSqs",
                ["kms:Decrypt"],
                [self.queue_key.key_arn],
                {"StringEquals": {"kms:ViaService": f"sqs.{self.region}.amazonaws.com"}},
            ),
        ]
        if self.assistant_secret is not None:
            # El ARN exacto del secreto de esta pila: sin el comodín del sufijo aleatorio.
            statements.append(
                _statement(
                    "ReadAssistantSecret",
                    ["secretsmanager:GetSecretValue"],
                    [self.assistant_secret.secret_arn],
                )
            )
        return statements

    def _loop_worker_policy(self) -> iam.ManagedPolicy:
        return iam.ManagedPolicy(
            self,
            "LoopWorkerPolicy",
            managed_policy_name=loop_worker_policy_name(self.config),
            description="vigia-loop-worker: permisos de U-04 de vigia-worker-task (U-04 9.1)",
            statements=self.loop_worker_statements(),
            roles=[self.worker_role],
        )

    # --- Alarmas (§10.3) ------------------------------------------------------------------

    def _alarm(self, spec: LoopAlarm) -> cloudwatch.Alarm:
        """Una alarma hacia ``vigia-alerts`` (NFR-LAZ-28), sin otra acción."""
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

    def _app_metric(
        self,
        name: str,
        service: str,
        statistic: str,
        period: Duration,
        dimensions: Mapping[str, str] | None = None,
    ) -> cloudwatch.Metric:
        """Métrica de ``Vigia/Platform`` de un servicio del despliegue (§10.1)."""
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

    def _email_metric(self, name: str, period: Duration) -> cloudwatch.Metric:
        """Métrica del destino ``cloudwatch`` del conjunto de configuración (§4.2)."""
        return cloudwatch.Metric(
            namespace="AWS/SES",
            metric_name=name,
            dimensions_map={CONFIGURATION_SET_DIMENSION: configuration_set_name(self.config)},
            statistic="Sum",
            period=period,
        )

    def _email_rate(self, name: str) -> cloudwatch.MathExpression:
        """``100 · <name> / Send`` del conjunto en 24 h (0 sin envíos)."""
        return cloudwatch.MathExpression(
            expression="IF(FILL(s, 0) > 0, 100 * FILL(n, 0) / FILL(s, 0), 0)",
            using_metrics={
                "n": self._email_metric(name, ONE_DAY),
                "s": self._email_metric("Send", ONE_DAY),
            },
            period=ONE_DAY,
            label=f"{name} % en 24 h",
        )

    def _application_alarms(self) -> None:
        """Las ocho condiciones de NFR-LAZ-42 en sus recursos (§10.3, PAT-LAZ-MAN-01)."""
        worker = WORKER_SERVICE
        dead_letters = {
            f"d{index}": self._app_metric(
                "dead_letter_created_total", worker, "Sum", FIVE_MINUTES, {"consumer": consumer}
            )
            for index, consumer in enumerate(LOOP_CONSUMERS)
        }
        self._alarm(
            LoopAlarm(
                "loop-dead-letter",
                cloudwatch.MathExpression(
                    expression=" + ".join(f"FILL({key}, 0)" for key in dead_letters),
                    using_metrics=dead_letters,
                    period=FIVE_MINUTES,
                    label="cola muerta de U-04",
                ),
                1,
                _GE,
                "Una entrada en la cola muerta de un consumidor de U-04 en 5 min (NFR-LAZ-39, "
                f"42). {RUNBOOK_DEAD_LETTER}.",
            )
        )
        self._alarm(
            LoopAlarm(
                "assistant-unavailable",
                self._app_metric(
                    "assistant_unavailable",
                    worker,
                    "Minimum",
                    Duration.minutes(ASSISTANT_UNAVAILABLE_MINUTES),
                ),
                1,
                _GE,
                "Asistente no disponible (circuito abierto o solicitudes unavailable) de forma "
                f"continua durante 30 min (NFR-LAZ-42). {RUNBOOK_ASSISTANT}.",
            )
        )
        self._alarm(
            LoopAlarm(
                "export-stuck",
                self._app_metric(
                    "outbox_oldest_pending_age_seconds",
                    worker,
                    "Maximum",
                    FIVE_MINUTES,
                    {"consumer": EXPORT_CONSUMER},
                ),
                EXPORT_STUCK_SECONDS,
                _GT,
                "Una exportacion con su entrega abierta mas de 60 min, en dos evaluaciones "
                f"(NFR-LAZ-42). {RUNBOOK_EXPORT}.",
                evaluation_periods=2,
            )
        )
        self._alarm(
            LoopAlarm(
                "export-failed",
                self._app_metric("export_failed_total", worker, "Sum", FIVE_MINUTES),
                1,
                _GE,
                f"Dos fallos seguidos de la misma exportacion (NFR-LAZ-42). {RUNBOOK_EXPORT}.",
            )
        )
        self._alarm(
            LoopAlarm(
                "loop-projection-divergence",
                self._app_metric("loop_projection_divergences_total", worker, "Sum", FIVE_MINUTES),
                1,
                _GE,
                "Una divergencia entre la proyeccion del lazo y el expediente (NFR-LAZ-39, 42). "
                f"{RUNBOOK_PROJECTION}.",
            )
        )
        for task, max_age in LOOP_PERIODIC_TASK_MAX_AGE_SECONDS.items():
            runbook = TASK_RUNBOOKS.get(task, "Sin runbook propio: registros de vigia-worker")
            self._alarm(
                LoopAlarm(
                    stale_alarm_key(task),
                    self._app_metric(
                        "periodic_task_last_success_age_seconds",
                        worker,
                        "Maximum",
                        FIVE_MINUTES,
                        {"task": task},
                    ),
                    max_age,
                    _GT,
                    f"{task} sin exito en {max_age} s (NFR-LAZ-42). {runbook}.",
                    missing=cloudwatch.TreatMissingData.MISSING,
                )
            )
        # La admisión (vigia-api) y el consumidor (vigia-worker) informan el presupuesto.
        self._alarm(
            LoopAlarm(
                "assistant-token-budget",
                cloudwatch.MathExpression(
                    expression="MAX([ba, bw])",
                    using_metrics={
                        "ba": self._app_metric(
                            "assistant_token_budget_used_ratio", API_SERVICE, "Maximum", ONE_HOUR
                        ),
                        "bw": self._app_metric(
                            "assistant_token_budget_used_ratio", worker, "Maximum", ONE_HOUR
                        ),
                    },
                    period=ONE_HOUR,
                    label="assistant_token_budget_used_ratio",
                ),
                ASSISTANT_TOKEN_BUDGET_RATIO,
                _GT,
                "Consumo mensual de fichas de una organizacion por encima del 80 % del tope, en "
                f"dos evaluaciones (NFR-LAZ-35, 42). {RUNBOOK_ASSISTANT}.",
                evaluation_periods=2,
            )
        )
        self._alarm(
            LoopAlarm(
                "assistant-rate-margin",
                self._app_metric(
                    "assistant_rate_limit_headroom_ratio", worker, "Minimum", FIVE_MINUTES
                ),
                ASSISTANT_RATE_MARGIN_RATIO,
                _LT,
                "Margen de limite de tasa del proveedor por debajo del 20 %, en dos evaluaciones "
                f"(NFR-LAZ-42). {RUNBOOK_ASSISTANT}.",
                evaluation_periods=2,
            )
        )

    def _service_alarms(self) -> None:
        """Las cinco propias de los recursos nuevos y ``email-complaint-rate`` (§10.3, I-2)."""
        self._alarm(
            LoopAlarm(
                "email-bounce-rate",
                self._email_rate("Bounce"),
                EMAIL_BOUNCE_RATE_PERCENT,
                _GT,
                "Tasa de rebotes por encima del 5 % en 24 h, en dos evaluaciones (NFR-LAZ-42). "
                f"{RUNBOOK_EMAIL}.",
                evaluation_periods=2,
            )
        )
        self._alarm(
            LoopAlarm(
                "email-events-dlq",
                self.email_events_dlq.metric_approximate_number_of_messages_visible(
                    period=FIVE_MINUTES, statistic="Maximum"
                ),
                1,
                _GE,
                f"Primera entrada en {email_events_dlq_name(self.config)} (NFR-LAZ-39). "
                f"{RUNBOOK_EMAIL_DLQ}.",
            )
        )
        self._alarm(
            LoopAlarm(
                "email-events-queue-age",
                self.email_events_queue.metric_approximate_age_of_oldest_message(
                    period=FIVE_MINUTES, statistic="Maximum"
                ),
                EMAIL_EVENTS_MAX_AGE_SECONDS,
                _GT,
                "Evento de entrega del correo con mas de 15 min en la cola, en dos evaluaciones. "
                f"{RUNBOOK_EMAIL_DLQ}.",
                evaluation_periods=2,
            )
        )
        quota = EMAIL_SEND_QUOTA_FRACTION * EMAIL_DAILY_SEND_QUOTA
        self._alarm(
            LoopAlarm(
                "email-send-quota-high",
                cloudwatch.Metric(
                    namespace="AWS/SES", metric_name="Send", statistic="Sum", period=ONE_DAY
                ),
                quota,
                _GT,
                f"Envios de la cuenta en 24 h por encima del 80 % de la cuota diaria "
                f"({EMAIL_DAILY_SEND_QUOTA}, hipotesis hasta el runbook 7.5 A), en dos "
                f"evaluaciones. {RUNBOOK_EMAIL}.",
                evaluation_periods=2,
            )
        )
        self._alarm(
            LoopAlarm(
                "email-bounce-rate-warning",
                self._email_rate("Bounce"),
                EMAIL_BOUNCE_WARNING_PERCENT,
                _GT,
                "Informativa: tasa de rebotes por encima del 60 % del umbral (3 % en 24 h). "
                f"{RUNBOOK_EMAIL}.",
            )
        )
        self._alarm(
            LoopAlarm(
                "email-complaint-rate-warning",
                self._email_rate("Complaint"),
                EMAIL_COMPLAINT_WARNING_PERCENT,
                _GT,
                "Informativa: tasa de quejas por encima del 60 % del umbral (0,06 % en 24 h). "
                f"{RUNBOOK_EMAIL}.",
            )
        )
        self._alarm(
            LoopAlarm(
                "email-complaint-rate",
                self._email_rate("Complaint"),
                EMAIL_COMPLAINT_RATE_PERCENT,
                _GT,
                "Tasa de quejas por encima del 0,1 % en 24 h, en dos evaluaciones (respuesta 2, "
                f"I-2). {RUNBOOK_EMAIL}.",
                evaluation_periods=2,
            )
        )


def _event_type(event: ses.EmailSendingEvent) -> str:
    """Tipo de evento como lo escribe CloudFormation (``delivery``, ``bounce``…)."""
    return str(event.value).lower()


def _statement(
    sid: str,
    actions: Sequence[str],
    resources: Sequence[str],
    conditions: Mapping[str, Mapping[str, object]] | None = None,
) -> iam.PolicyStatement:
    return iam.PolicyStatement(
        sid=sid,
        actions=list(actions),
        resources=list(resources),
        conditions=dict(conditions) if conditions else None,
    )


__all__ = [
    "CLOUDWATCH_EVENTS",
    "CONFIGURATION_SET_CONDITION_KEY",
    "CONFIGURATION_SET_DIMENSION",
    "CONSUME_ACTIONS",
    "DMARC_REPORTS_PARAMETER",
    "ENDPOINT_ACTIONS",
    "EXPORT_CONSUMER",
    "LOOP_APPLICATION_ALARM_METRICS",
    "LOOP_CONDITION_ALARMS",
    "LOOP_CONSUMERS",
    "LOOP_METRIC_SUBSTITUTES",
    "LOOP_PERIODIC_TASK_MAX_AGE_SECONDS",
    "LOOP_SERVICE_ALARM_METRICS",
    "MAX_RECEIVE_COUNT",
    "TOPIC_EVENTS",
    "LoopStack",
    "assistant_secret_name",
    "configuration_set_name",
    "email_events_dlq_name",
    "email_events_name",
    "loop_worker_policy_name",
    "queue_endpoint_name",
    "reputation_rule_name",
    "stale_alarm_key",
]
