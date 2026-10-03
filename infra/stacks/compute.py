"""Pila ``vigia-compute``: registro de imágenes, clúster, definiciones de tarea, servicios,
escalado y roles de tarea (§5, §8, §9.1). Depende de ``vigia-foundation``, ``vigia-data`` y
``vigia-edge``. Recursos: TASK-148 (pendientes nº 10, 16, 17, 20 y 38).

Registro y clúster (§5.1):

- Registro ``vigia-platform``: etiquetas inmutables, escaneo al subir, AES-256 del servicio
  (excepción declarada de §2.3) y ciclo de vida de 20 imágenes. Es uno por cuenta, donde
  ``release.yml`` publica la imagen: lo crea ``pilot`` compartido y los demás despliegues lo
  importan por nombre, igual que la zona DNS (D-8).
- Clúster ``vigia-<despliegue>`` con Container Insights; Fargate ``ARM64``, versión ``LATEST``.
- La imagen se referencia **por digest**: el flujo de release pasa ``--context
  image_digest=sha256:<64 hex>``. Sin ese contexto (``cdk synth`` local, primer despliegue antes
  de publicar la imagen, paso 4) la plantilla lleva un digest de ceros y avisa si hay tareas.

Servicios (§5.2, §5.3 con su nota U02-H-10, nº 17 y A-21):

- ``vigia-api``: 0,5 vCPU y 1 GB, de 2 a 6 tareas en las dos subredes de aplicación (una por
  zona), CPU al 60 % y 300 peticiones por destino y minuto con 120 s de enfriamiento; en
  ``tg-api`` y, cuando ``vigia-edge`` asocia ``tg-api-nodes`` a un balanceador, también en ese.
  Con ``first_deploy=true`` la escucha de nodos aún no existe (VIG-41) y solo va a ``tg-api``.
- ``vigia-worker``: 1 vCPU y 2 GB, de 1 a 3 tareas; +1 si ``outbox_oldest_pending_age_seconds``
  supera 30 durante 2 minutos, CPU al 60 % y reducción tras 15 minutos por debajo; parada
  ordenada de 120 s; ``statement_timeout`` de 30 s solo aquí (NFR-NUC-36).
- Con ``first_deploy=true`` los dos arrancan con 0 tareas y sin escalado (paso 4).
- Comunes: raíz de solo lectura con ``/tmp`` montado desde el volumen efímero de la tarea
  (``ephemeralStorage`` de 21 GiB; presupuesto de 4 GiB para el ``/tmp`` del worker), usuario
  ``vigia`` sin capacidades (se quitan todas), sin exec, despliegue continuo al 100-200 % con
  cortacircuito y reversión, ``awslogs`` sin bloqueo con búfer de 25 MB hacia los grupos
  ``/vigia/<despliegue>/*`` que crea ``vigia-observability`` (TASK-149), y colector ADOT lateral
  (``otel/collector.yaml``). Ningún secreto en variables: la aplicación lee Secrets Manager con su
  rol y las variables solo llevan nombres y ARN (§5.3).

Tareas puntuales (§5.4 y nota U02-H-01): ``vigia-migrate`` (``alembic upgrade head``) y
``vigia-admin`` (``vigia-admin <orden>``), con red ``sg-tasks`` al lanzarlas.

Roles (§8, notas nº 16, nº 20, U02-H-01, U02-H-04 y U02-H-14; A-23 y A-34), con ``roleName`` fijo
y una política gestionada por el cliente cada uno, sin comodines fuera de los declarados en
``tests/template_rules.py``. Los permisos del arranque van en políticas aparte que solo existen
con ``first_deploy=true`` (``vigia-migrate-task``: ``db/master`` y ``db/app``) o con
``first_deploy=true`` o ``ca_rotation=true`` (``vigia-admin-task``: ``kms:Sign`` y
``GetPublicKey`` sobre ``vigia-node-ca`` y ``s3:PutObject`` sobre ``vigia-edge/ca/root.pem``). El
rol del worker se publica en ``/vigia/<despliegue>/worker-task-role-arn`` (nº 38).
``vigia-deploy`` es de la cuenta: lo crea el despliegue permanente y ``staging-<n>`` lo usa.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from aws_cdk import (
    Annotations,
    ArnFormat,
    CfnOutput,
    Duration,
    Fn,
    IResolvable,
    RemovalPolicy,
    Size,
)
from aws_cdk import aws_applicationautoscaling as appscaling
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cloudwatch_actions
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from constructs import Construct

from config import ContextError, EnvironmentConfig, NodesTlsMode
from config.environment import PILOT, SHARED_INSTANCE
from config.pilot import OTEL_COLLECTOR_IMAGE
from stacks.base import VigiaStack
from stacks.data import BucketUsage, DataStack, DbUser, app_host, db_identifier, db_secret_name
from stacks.edge import API_PORT, PASSTHROUGH_PORT, ROOT_CERTIFICATE_KEY, EdgeStack, nodes_host
from stacks.foundation import (
    APP_SUBNETS,
    SIGNING_ALGORITHM,
    FoundationStack,
    KeyName,
    SecurityGroupName,
    deploy_role_name,
)
from stacks.outputs import Output, TaskRole, publish, role_name

# --- Registro e imagen (§5.1) -------------------------------------------------------------

REGISTRY_NAME = "vigia-platform"
REGISTRY_MAX_IMAGES = 20
IMAGE_DIGEST_CONTEXT = "image_digest"
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
# Sin imagen publicada todavía (paso 4 del primer despliegue): solo sirve con 0 tareas.
NO_IMAGE_DIGEST = "sha256:" + "0" * 64

# --- Tareas y contenedores (§5.2, §5.3) ----------------------------------------------------


@dataclass(frozen=True)
class TaskSize:
    """CPU en unidades de Fargate (1024 = 1 vCPU) y memoria en MiB."""

    cpu: int
    memory_mib: int


API_SIZE = TaskSize(512, 1024)  # [objetivo propio], A-21
WORKER_SIZE = TaskSize(1024, 2048)  # [objetivo propio]
ONE_OFF_SIZE = TaskSize(512, 1024)  # §10: una vCPU entre las dos tareas puntuales
# Nota U02-H-10 de §5.3: /tmp desde el volumen efímero de la tarea, de tamaño fijado.
EPHEMERAL_STORAGE_GIB = 21
WORKER_TMP_BUDGET_BYTES = 4 * 1024**3  # [objetivo propio]
TMP_VOLUME = "tmp"
TMP_PATH = "/tmp"  # noqa: S108 - punto de montaje del volumen efímero, no un archivo temporal
CONTAINER_USER = "vigia"
WORKER_HEALTH_PORT = 8001
WORKER_STOP_TIMEOUT = Duration.seconds(120)  # máximo de Fargate
HEALTH_GRACE_PERIOD = Duration.seconds(60)
LOG_BUFFER = Size.mebibytes(25)
# §9.1: reserva del colector en cada tarea.
OTEL_MEMORY_RESERVATION_MIB = {"api": 128, "worker": 256}
OTEL_RESTART_PERIOD = Duration.seconds(60)
OTEL_ENDPOINT = "http://localhost:4317"
COLLECTOR_CONFIG = Path(__file__).resolve().parents[1] / "otel" / "collector.yaml"
METRICS_NAMESPACE = "Vigia/Platform"

API_SERVICE = "vigia-api"
WORKER_SERVICE = "vigia-worker"
MIGRATE_COMMAND = ("alembic", "upgrade", "head")
ADMIN_COMMAND = ("vigia-admin", "--help")  # ``make admin ARGS=...`` lo sustituye al lanzar
WORKER_COMMAND = ("vigia-worker",)
# El contenedor ``api`` usa la orden por defecto de la imagen (uvicorn, §5.2).


def _health_probe(port: int) -> list[str]:
    """Comprobación de salud del contenedor sobre ``/health/live`` sin depender de ``curl``."""
    probe = (
        "import urllib.request;"
        f"urllib.request.urlopen('http://localhost:{port}/health/live', timeout=3)"
    )
    return ["CMD", "python", "-c", probe]


# Nº 17 (U-03 §6) y A-21: tamaños internos de vigia-api.
API_TUNING: Mapping[str, str] = {
    "VIGIA_UVICORN_WORKERS": "2",
    "VIGIA_BULKHEAD_NODE": "35",
    "VIGIA_BULKHEAD_PERSON": "15",
    "VIGIA_DB_POOL_NODE": "10",
    "VIGIA_DB_POOL_PERSON": "5",
    "VIGIA_DB_MAX_OVERFLOW": "0",
    "VIGIA_DB_POOL_TIMEOUT_SECONDS": "5",
    "VIGIA_THREADPOOL_SIZE": "4",
    "VIGIA_DOCUMENTS_PREFIX": "documents/",
    "VIGIA_DOCUMENTS_MAX_BYTES": str(20 * 1024 * 1024),
}
WORKER_STATEMENT_TIMEOUT_MS = "30000"  # NFR-NUC-36, solo en el worker
CRL_KEY = "ca/crl.pem"

# --- Escalado (§5.2) -----------------------------------------------------------------------

CPU_TARGET_PERCENT = 60
API_REQUESTS_PER_TARGET = 300  # por minuto, [objetivo propio]
API_COOLDOWN = Duration.seconds(120)
OUTBOX_AGE_METRIC = "outbox_oldest_pending_age_seconds"
OUTBOX_AGE_THRESHOLD_SECONDS = 30
OUTBOX_SCALE_OUT_MINUTES = 2
WORKER_SCALE_IN_MINUTES = 15
AUTOSCALING_SERVICE_LINKED_ROLE = (
    "aws-service-role/ecs.application-autoscaling.amazonaws.com/"
    "AWSServiceRoleForApplicationAutoScaling_ECSService"
)

# --- Evidencias (A-34) ---------------------------------------------------------------------

# Prefijos de las URL prefirmadas de subida de vigia-api (nº 16 de U-03 y nº 20 de U-04).
EVIDENCE_UPLOAD_PREFIXES = (
    "org/*/plant/*/zone/*/node/*",
    "org/*/plant/*/documents/*",
    "org/*/closure/*",
)
EVIDENCE_READ_PREFIX = "org/*"
CHECKSUM_CONDITION = {"Null": {"s3:x-amz-checksum-sha256": "false"}}

# --- Canalización (§8 y deployment-architecture §3.1) --------------------------------------

# Propietario provisional hasta TASK-035 (AGENTS.md: ``vigia-project/…`` se lee
# ``Machaves07/…`` mientras la organización no exista).
GITHUB_REPOSITORY = "Machaves07/vigia-platform"
GITHUB_ENVIRONMENTS = ("pilot", "staging")
GITHUB_OIDC_HOST = "token.actions.githubusercontent.com"
# Roles del arranque ``cdk bootstrap --qualifier vigia`` que asume ``cdk deploy``.
CDK_BOOTSTRAP_ROLES = ("deploy", "file-publishing", "image-publishing", "lookup")
DEPLOY_ECR_ACTIONS = (
    "ecr:BatchCheckLayerAvailability",
    "ecr:InitiateLayerUpload",
    "ecr:UploadLayerPart",
    "ecr:CompleteLayerUpload",
    "ecr:PutImage",
    "ecr:BatchGetImage",
    "ecr:DescribeImages",
    # NFR-NUC-23: los hallazgos críticos del escaneo bloquean el release.
    "ecr:DescribeImageScanFindings",
)
# Lectura de la canalización sin permisos a nivel de recurso (TASK-151): comprobaciones nº 2
# (``HealthyHostCount``) y 7 (alarmas), y barrido y residuos de ``staging-<n>``.
PIPELINE_READ_ACTIONS = (
    "cloudformation:ListStacks",
    "cloudwatch:DescribeAlarms",
    "cloudwatch:GetMetricStatistics",
    "elasticloadbalancing:DescribeTargetGroups",
    "tag:GetResources",
)
# ``vigia-node-trust``: paquete de raíces (§6.4) y respaldo manual de la lista de revocación
# (``trust-store.yml``, pendiente nº 19; U-03 deployment-architecture §4 y runbook 6.2).
TRUST_STORE_ACTIONS = (
    "elasticloadbalancing:ModifyTrustStore",
    "elasticloadbalancing:AddTrustStoreRevocations",
    "elasticloadbalancing:RemoveTrustStoreRevocations",
    "elasticloadbalancing:DescribeTrustStoreRevocations",
)

# --- Restauración (§8 y deployment-architecture §6.1) --------------------------------------

RESTORE_DB_ACTIONS = ("rds:RestoreDBInstanceToPointInTime", "rds:RestoreDBInstanceFromDBSnapshot")
DB_OPTION_GROUP = "default:postgres-16"
BACKUP_SNAPSHOTS = "awsbackup:job-*"


class ServiceName(StrEnum):
    """Contenedor principal de cada definición de tarea."""

    API = "api"
    WORKER = "worker"
    MIGRATE = "migrate"
    ADMIN = "admin"


def registry_owned(config: EnvironmentConfig) -> bool:
    """El registro es de la cuenta: solo lo crea ``pilot`` compartido."""
    return config.environment == PILOT and config.instance == SHARED_INSTANCE


def deploy_role_owned(config: EnvironmentConfig) -> bool:
    """``vigia-deploy`` es de la cuenta: lo crea el despliegue permanente, no ``staging-<n>``."""
    return not config.ephemeral


def cluster_name(config: EnvironmentConfig) -> str:
    """``vigia-pilot`` (§5.1); ``vigia-<despliegue>`` en los demás."""
    return f"vigia-{config.deployment}"


def log_group_name(config: EnvironmentConfig, process: str) -> str:
    """``/vigia/<despliegue>/<proceso>`` (§9.2); los crea ``vigia-observability``."""
    return f"/vigia/{config.deployment}/{process}"


def read_image_digest(scope: Construct) -> str | None:
    """Digest de la imagen publicada por el flujo de release, o ``None`` si no se pasó."""
    value = scope.node.try_get_context(IMAGE_DIGEST_CONTEXT)
    if value is None:
        return None
    if not isinstance(value, str) or not _IMAGE_DIGEST.match(value):
        raise ContextError(
            f"El contexto '{IMAGE_DIGEST_CONTEXT}' debe ser 'sha256:' seguido de 64 cifras "
            f"hexadecimales en minúscula; se recibió {value!r}."
        )
    return value


_ENV_REFERENCE = re.compile(r"\$\{env:([A-Z0-9_]+)\}")


def render_collector_config(values: Mapping[str, str]) -> str:
    """``otel/collector.yaml`` con sus ``${env:NOMBRE}`` ya resueltos.

    Se resuelven al sintetizar porque CloudFormation lee ``${...}`` como parámetro de
    ``Fn::Sub`` fuera de su sitio; todos los valores se conocen en la plantilla. Un nombre sin
    valor detiene la síntesis.
    """

    def value(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise KeyError(f"otel/collector.yaml usa ${{env:{name}}} sin valor en la tarea")
        return values[name]

    return _ENV_REFERENCE.sub(value, COLLECTOR_CONFIG.read_text(encoding="utf-8"))


def evidence_store_origin(bucket: str, region: str) -> str:
    """Origen del depósito de evidencias en estilo de host virtual (nº 10, U-05 §5.1)."""
    return f"https://{bucket}.s3.{region}.amazonaws.com"


class ComputeStack(VigiaStack):
    """Pila ``vigia-compute`` (infrastructure-design §2.3)."""

    key = "compute"
    summary = "registro de imagenes, cluster, servicios, tareas puntuales, roles"

    def __init__(
        self, scope: Construct, config: EnvironmentConfig, *, tags: Mapping[str, str]
    ) -> None:
        super().__init__(scope, config, tags=tags)
        self.foundation = self._sibling(scope, FoundationStack)
        self.data = self._sibling(scope, DataStack)
        self.edge = self._sibling(scope, EdgeStack)
        self.image_digest = read_image_digest(self)

        self.registry = self._registry()
        self.cluster = self._cluster()
        self.managed_policies: dict[str, iam.ManagedPolicy] = {}
        self.roles: dict[TaskRole, iam.Role] = self._task_roles()
        self.restore_role = self._restore_role()

        self.task_definitions: dict[ServiceName, ecs.FargateTaskDefinition] = {}
        self._log_groups: dict[str, logs.ILogGroup] = {}
        self.api_service = self._api_service()
        self.worker_service = self._worker_service()
        self._one_off(ServiceName.MIGRATE, MIGRATE_COMMAND, self._migrate_environment())
        self._one_off(ServiceName.ADMIN, ADMIN_COMMAND, self._admin_environment())
        self.scaling: list[appscaling.ScalableTarget] = []
        if not config.first_deploy:
            autoscaling_role = self._autoscaling_role()
            self.scaling = [
                self._api_scaling(autoscaling_role),
                self._worker_scaling(autoscaling_role),
            ]

        self.deploy_role = self._deploy_role() if deploy_role_owned(config) else None
        self.staging_deploy_policy = self._staging_deploy_policy() if config.ephemeral else None
        publish(
            self, config, Output.WORKER_TASK_ROLE_ARN, self.roles[TaskRole.WORKER_TASK].role_arn
        )
        self._outputs()

    @staticmethod
    def _sibling[S: VigiaStack](scope: Construct, kind: type[S]) -> S:
        found = next((c for c in scope.node.children if isinstance(c, kind)), None)
        if found is None:
            raise TypeError(f"vigia-compute necesita {kind.__name__} registrada antes")
        return found

    # --- ARN ------------------------------------------------------------------------------

    def _bucket(self, usage: BucketUsage | str) -> str:
        return self.config.bucket_name(str(usage), self.account)

    def _objects(self, usage: BucketUsage, key: str) -> str:
        return f"arn:{self.partition}:s3:::{self._bucket(usage)}/{key}"

    def _secret_arn(self, name: str) -> str:
        return self.format_arn(
            service="secretsmanager",
            resource="secret",
            resource_name=name,
            arn_format=ArnFormat.COLON_RESOURCE_NAME,
        )

    def _log_groups_arn(self, name: str) -> str:
        return self.format_arn(
            service="logs",
            resource="log-group",
            resource_name=name,
            arn_format=ArnFormat.COLON_RESOURCE_NAME,
        )

    def _key_arn(self, name: KeyName) -> str:
        return self.foundation.keys[name].key_arn

    def _signing_secrets(self) -> str:
        return self._secret_arn(f"vigia/{self.config.deployment}/signing/*")

    def _via_s3(self) -> dict[str, dict[str, str]]:
        return {"StringEquals": {"kms:ViaService": f"s3.{self.region}.amazonaws.com"}}

    # --- Registro y clúster ---------------------------------------------------------------

    def _registry(self) -> ecr.IRepository:
        if not registry_owned(self.config):
            return ecr.Repository.from_repository_name(self, "Registry", REGISTRY_NAME)
        return ecr.Repository(
            self,
            "Registry",
            repository_name=REGISTRY_NAME,
            image_tag_mutability=ecr.TagMutability.IMMUTABLE,
            image_scan_on_push=True,
            # Excepción declarada de §2.3: AES-256 del servicio.
            encryption=ecr.RepositoryEncryption.AES_256,
            lifecycle_rules=[
                ecr.LifecycleRule(
                    description="Conserva las ultimas 20 imagenes (5.1)",
                    max_image_count=REGISTRY_MAX_IMAGES,
                )
            ],
            removal_policy=RemovalPolicy.RETAIN,
        )

    def _cluster(self) -> ecs.Cluster:
        return ecs.Cluster(
            self,
            "Cluster",
            cluster_name=cluster_name(self.config),
            vpc=self.foundation.vpc,
            container_insights_v2=ecs.ContainerInsights.ENABLED,
        )

    def _image(self) -> ecs.ContainerImage:
        digest = self.image_digest
        if digest is None:
            digest = NO_IMAGE_DIGEST
            if not self.config.first_deploy:
                Annotations.of(self).add_warning_v2(
                    "vigia:no-image-digest",
                    "Sin --context image_digest=sha256:<digest>: la plantilla referencia un "
                    "digest de ceros y los servicios no arrancarian. El flujo de release lo pasa.",
                )
        return ecs.ContainerImage.from_ecr_repository(self.registry, tag=digest)

    # --- Roles (§8) -----------------------------------------------------------------------

    def _policy(
        self, name: str, description: str, statements: Sequence[iam.PolicyStatement]
    ) -> iam.ManagedPolicy:
        """Política gestionada por el cliente (§8: «no en línea»)."""
        policy = iam.ManagedPolicy(
            self,
            f"Policy-{name}",
            managed_policy_name=self.config.resource_name(name),
            description=description,
            statements=list(statements),
        )
        self.managed_policies[name] = policy
        return policy

    def _ecs_role(self, role: TaskRole, description: str) -> iam.Role:
        return iam.Role(
            self,
            f"Role-{role.value}",
            role_name=role_name(self.config, role),
            assumed_by=iam.ServicePrincipal(
                "ecs-tasks.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
            description=description,
        )

    def _task_roles(self) -> dict[TaskRole, iam.Role]:
        roles = {
            TaskRole.TASK_EXECUTION: self._ecs_role(
                TaskRole.TASK_EXECUTION,
                "vigia-task-execution: descarga la imagen y escribe registros (8)",
            ),
            TaskRole.API_TASK: self._ecs_role(
                TaskRole.API_TASK, "vigia-api-task: proceso vigia-api (8)"
            ),
            TaskRole.WORKER_TASK: self._ecs_role(
                TaskRole.WORKER_TASK, "vigia-worker-task: proceso vigia-worker (8)"
            ),
            TaskRole.MIGRATE_TASK: self._ecs_role(
                TaskRole.MIGRATE_TASK, "vigia-migrate-task: migraciones alembic (5.4, 8)"
            ),
            TaskRole.ADMIN_TASK: self._ecs_role(
                TaskRole.ADMIN_TASK, "vigia-admin-task: ordenes del duenio (5.4, 8)"
            ),
        }
        policies: dict[TaskRole, list[iam.ManagedPolicy]] = {
            TaskRole.TASK_EXECUTION: [
                self._policy(
                    "task-execution",
                    "Imagen y registros de las tareas (8)",
                    self._execution_statements(),
                )
            ],
            TaskRole.API_TASK: [
                self._policy("api-task", "vigia-api (8, A-34)", self._api_statements())
            ],
            TaskRole.WORKER_TASK: [
                self._policy("worker-task", "vigia-worker (8, nº 16)", self._worker_statements())
            ],
            TaskRole.MIGRATE_TASK: [
                self._policy("migrate-task", "vigia-migrate (5.4, 8)", self._migrate_statements())
            ],
            TaskRole.ADMIN_TASK: [
                self._policy("admin-task", "vigia-admin (5.4, 8)", self._admin_statements())
            ],
        }
        if self.config.first_deploy:
            policies[TaskRole.MIGRATE_TASK].append(
                self._policy(
                    "migrate-task-bootstrap",
                    "Solo con first_deploy: db/master y db/app para la migracion 0001 (5.4)",
                    self._migrate_bootstrap_statements(),
                )
            )
        if self.config.elevated_bootstrap:
            policies[TaskRole.ADMIN_TASK].append(
                self._policy(
                    "admin-task-bootstrap",
                    "Solo con first_deploy o ca_rotation: firma y publica la raiz (5.4, 6.4)",
                    self._admin_bootstrap_statements(),
                )
            )
        for name, role in roles.items():
            for policy in policies[name]:
                role.add_managed_policy(policy)
        return roles

    def _immutable(self, role: TaskRole) -> iam.IRole:
        """El rol sin las concesiones automáticas de CDK: sus permisos son solo los de §8."""
        return self.roles[role].without_policy_updates()

    def _execution_statements(self) -> list[iam.PolicyStatement]:
        return [
            # La API no admite recurso (excepción de §8).
            iam.PolicyStatement(
                sid="RegistryToken", actions=["ecr:GetAuthorizationToken"], resources=["*"]
            ),
            iam.PolicyStatement(
                sid="PullImage",
                actions=["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
                resources=[self.registry.repository_arn],
            ),
            iam.PolicyStatement(
                sid="WriteTaskLogs",
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[self._log_groups_arn(f"/vigia/{self.config.deployment}/*")],
            ),
        ]

    def _telemetry_statements(self) -> list[iam.PolicyStatement]:
        """Colector lateral (§9.1): trazas a X-Ray, métricas del espacio ``Vigia/Platform`` y
        el EMF de ``awsemf``, que se escribe como registro en ``/vigia/<despliegue>/otel``."""
        return [
            iam.PolicyStatement(
                sid="Traces",
                actions=["xray:PutTraceSegments", "xray:PutTelemetryRecords"],
                resources=["*"],
            ),
            iam.PolicyStatement(
                sid="Metrics",
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={"StringEquals": {"cloudwatch:namespace": METRICS_NAMESPACE}},
            ),
            iam.PolicyStatement(
                sid="EmbeddedMetrics",
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[self._log_groups_arn(f"{log_group_name(self.config, 'otel')}:*")],
            ),
        ]

    def _shared_process_statements(self) -> list[iam.PolicyStatement]:
        """Lo común de ``vigia-api-task`` y ``vigia-worker-task`` (§8, §7.1 y §7.2)."""
        return [
            iam.PolicyStatement(
                sid="ReadAppAndSigningSecrets",
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    self.data.db_secrets[DbUser.APP].secret_arn,
                    self._signing_secrets(),
                ],
            ),
            # Rotación de las claves de firma (BR-NUC-85).
            iam.PolicyStatement(
                sid="RotateSigningSecrets",
                actions=["secretsmanager:CreateSecret", "secretsmanager:PutSecretValue"],
                resources=[self._signing_secrets()],
            ),
            iam.PolicyStatement(
                sid="SecretsKey",
                actions=["kms:Decrypt", "kms:GenerateDataKey"],
                resources=[self._key_arn(KeyName.SECRETS)],
            ),
            # URL prefirmadas de lectura y la muestra diaria del worker (LC-NUC-15).
            iam.PolicyStatement(
                sid="ReadEvidence",
                actions=["s3:GetObject"],
                resources=[self._objects(BucketUsage.EVIDENCE, EVIDENCE_READ_PREFIX)],
            ),
            # §7.1: la clave vigia-evidence solo se usa a través de S3.
            iam.PolicyStatement(
                sid="EvidenceKeyThroughS3",
                actions=["kms:Decrypt", "kms:GenerateDataKey"],
                resources=[self._key_arn(KeyName.EVIDENCE)],
                conditions=self._via_s3(),
            ),
            *self._telemetry_statements(),
        ]

    def _api_statements(self) -> list[iam.PolicyStatement]:
        uploads = [
            self._objects(BucketUsage.EVIDENCE, prefix) for prefix in EVIDENCE_UPLOAD_PREFIXES
        ]
        return [
            *self._shared_process_statements(),
            iam.PolicyStatement(
                sid="SignNodeCertificates",
                actions=["kms:Sign"],
                resources=[self._key_arn(KeyName.NODE_CA)],
                conditions={"StringEquals": {"kms:SigningAlgorithm": SIGNING_ALGORITHM}},
            ),
            iam.PolicyStatement(
                sid="NodeCaPublicKey",
                actions=["kms:GetPublicKey"],
                resources=[self._key_arn(KeyName.NODE_CA)],
            ),
            # A-34: una sola sentencia de escritura, con la suma obligatoria y sin condición de
            # cifrado (el depósito cifra por defecto con vigia-evidence). El proceso solo firma
            # URL: la subida la hace el nodo o el navegador con la URL.
            iam.PolicyStatement(
                sid="PresignedUploads",
                actions=["s3:PutObject"],
                resources=uploads,
                conditions=CHECKSUM_CONDITION,
            ),
            iam.PolicyStatement(
                sid="DenyOtherWrites",
                effect=iam.Effect.DENY,
                actions=["s3:PutObject"],
                not_resources=uploads,
            ),
        ]

    def _worker_statements(self) -> list[iam.PolicyStatement]:
        statements = [
            *self._shared_process_statements(),
            # §7.1: firma de la lista de revocación.
            iam.PolicyStatement(
                sid="SignRevocationList",
                actions=["kms:Sign"],
                resources=[self._key_arn(KeyName.NODE_CA)],
                conditions={"StringEquals": {"kms:SigningAlgorithm": SIGNING_ALGORITHM}},
            ),
            # §6.2 y LC-NUC-33: particiones de auditoría archivadas.
            iam.PolicyStatement(
                sid="Archive",
                actions=["s3:PutObject", "s3:GetObject"],
                resources=[self._objects(BucketUsage.ARCHIVE, "*")],
            ),
            iam.PolicyStatement(
                sid="ArchiveKeyThroughS3",
                actions=["kms:Encrypt", "kms:GenerateDataKey"],
                resources=[self._key_arn(KeyName.ARCHIVE)],
                conditions=self._via_s3(),
            ),
            # Nº 16 y D-7: la lista de revocación (``ca/crl.pem``) y la lectura de la raíz.
            iam.PolicyStatement(
                sid="PublishRevocationList",
                actions=["s3:PutObject"],
                resources=[self._objects(BucketUsage.EDGE, "ca/*")],
            ),
            iam.PolicyStatement(
                sid="ReadEdgeCa",
                actions=["s3:GetObject", "s3:GetObjectVersion"],
                resources=[self._objects(BucketUsage.EDGE, "ca/*")],
            ),
        ]
        trust_store = self.edge.trust_store
        if trust_store is not None:
            # Nº 16 y A-19: el worker añade la lista al almacén en el mismo barrido.
            statements.append(
                iam.PolicyStatement(
                    sid="TrustStoreRevocations",
                    actions=[
                        "elasticloadbalancing:AddTrustStoreRevocations",
                        "elasticloadbalancing:RemoveTrustStoreRevocations",
                        "elasticloadbalancing:DescribeTrustStoreRevocations",
                        "elasticloadbalancing:ModifyTrustStore",
                    ],
                    resources=[trust_store.trust_store_arn],
                )
            )
        return statements

    def _migrate_statements(self) -> list[iam.PolicyStatement]:
        return [
            iam.PolicyStatement(
                sid="ReadMigrateSecret",
                actions=["secretsmanager:GetSecretValue"],
                resources=[self.data.db_secrets[DbUser.MIGRATE].secret_arn],
            ),
            iam.PolicyStatement(
                sid="SecretsKey",
                actions=["kms:Decrypt"],
                resources=[self._key_arn(KeyName.SECRETS)],
            ),
        ]

    def _migrate_bootstrap_statements(self) -> list[iam.PolicyStatement]:
        """Nota U02-H-01 de §5.4: la migración ``0001`` crea ``vigia_app`` y ``vigia_migrate`` con
        el secreto maestro que gestiona RDS."""
        return [
            iam.PolicyStatement(
                sid="ReadMasterAndAppSecrets",
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    self.data.database.attr_master_user_secret_secret_arn,
                    self.data.db_secrets[DbUser.APP].secret_arn,
                ],
            )
        ]

    def _invitation_secret(self) -> str:
        # Secrets Manager añade seis caracteres aleatorios al nombre.
        return self._secret_arn(f"vigia/{self.config.deployment}/bootstrap/invitation-??????")

    def _admin_statements(self) -> list[iam.PolicyStatement]:
        return [
            iam.PolicyStatement(
                sid="ReadAppSecret",
                actions=["secretsmanager:GetSecretValue"],
                resources=[self.data.db_secrets[DbUser.APP].secret_arn],
            ),
            iam.PolicyStatement(
                sid="WriteInvitationAndSigningSecrets",
                actions=["secretsmanager:CreateSecret", "secretsmanager:PutSecretValue"],
                resources=[self._invitation_secret(), self._signing_secrets()],
            ),
            iam.PolicyStatement(
                sid="SecretsKey",
                actions=["kms:GenerateDataKey", "kms:Decrypt"],
                resources=[self._key_arn(KeyName.SECRETS)],
            ),
        ]

    def _admin_bootstrap_statements(self) -> list[iam.PolicyStatement]:
        """Nota U02-H-01 de §5.4 y nota D-8 de deployment-architecture §6.4 (``rotate-node-ca``)."""
        return [
            iam.PolicyStatement(
                sid="SignRoot",
                actions=["kms:Sign"],
                resources=[self._key_arn(KeyName.NODE_CA)],
                conditions={"StringEquals": {"kms:SigningAlgorithm": SIGNING_ALGORITHM}},
            ),
            iam.PolicyStatement(
                sid="RootPublicKey",
                actions=["kms:GetPublicKey"],
                resources=[self._key_arn(KeyName.NODE_CA)],
            ),
            iam.PolicyStatement(
                sid="PublishRoot",
                actions=["s3:PutObject"],
                resources=[self._objects(BucketUsage.EDGE, ROOT_CERTIFICATE_KEY)],
            ),
        ]

    def _restore_role(self) -> iam.Role:
        """``vigia-restore`` (solo runbooks): la asume la identidad administrativa del dueño con
        segundo factor (§8 y notas U02-H-14 de deployment-architecture §6.1)."""
        config = self.config
        policy = self._policy(
            "restore",
            "vigia-restore: ensayo de restauracion y restauracion de auditoria (8, 6.1)",
            self._restore_statements(),
        )
        return iam.Role(
            self,
            "Role-restore",
            role_name=config.resource_name("restore"),
            assumed_by=iam.AccountRootPrincipal().with_conditions(
                {"Bool": {"aws:MultiFactorAuthPresent": "true"}}
            ),
            managed_policies=[policy],
            max_session_duration=Duration.hours(4),  # RTO del ensayo (§6.1)
            description="vigia-restore: runbooks de restauracion con segundo factor (8)",
        )

    def _rds_arn(self, resource: str, name: str) -> str:
        return self.format_arn(
            service="rds",
            resource=resource,
            resource_name=name,
            arn_format=ArnFormat.COLON_RESOURCE_NAME,
        )

    def _restore_statements(self) -> list[iam.PolicyStatement]:
        config = self.config
        source = db_identifier(config)
        return [
            iam.PolicyStatement(
                sid="RestoreDatabase",
                actions=list(RESTORE_DB_ACTIONS),
                resources=[
                    self._rds_arn("db", source),
                    self._rds_arn("db", config.resource_name("drill-db")),
                    self._rds_arn("subgrp", f"{source}-subnets"),
                    self._rds_arn("pg", config.resource_name("pg16")),
                    self._rds_arn("og", DB_OPTION_GROUP),
                    self._rds_arn("snapshot", BACKUP_SNAPSHOTS),
                ],
            ),
            # §8: ``s3:GetObject*`` sobre ``vigia-archive/*``, con las acciones explícitas.
            iam.PolicyStatement(
                sid="ReadArchive",
                actions=["s3:GetObject", "s3:GetObjectVersion"],
                resources=[self._objects(BucketUsage.ARCHIVE, "*")],
            ),
            iam.PolicyStatement(
                sid="ArchiveAndBackupKeys",
                actions=["kms:Decrypt"],
                resources=[self._key_arn(KeyName.ARCHIVE), self._key_arn(KeyName.BACKUP)],
            ),
            # Nota U02-H-14: una versión anterior de una evidencia se copia al depósito de
            # ensayo, que comparte la clave vigia-evidence.
            iam.PolicyStatement(
                sid="ReadEvidenceVersions",
                actions=["s3:GetObject", "s3:GetObjectVersion"],
                resources=[self._objects(BucketUsage.EVIDENCE, EVIDENCE_READ_PREFIX)],
            ),
            iam.PolicyStatement(
                sid="EvidenceKeyThroughS3",
                actions=["kms:Decrypt", "kms:GenerateDataKey"],
                resources=[self._key_arn(KeyName.EVIDENCE)],
                conditions=self._via_s3(),
            ),
            iam.PolicyStatement(
                sid="WriteDrill",
                actions=["s3:PutObject"],
                resources=[self._objects(BucketUsage.DRILL, "*")],
            ),
        ]

    def _deploy_role(self) -> iam.Role:
        """``vigia-deploy`` por federación de identidad de GitHub (§8, deployment-architecture
        §3.1 y nota U02-H-01). No escribe en ``vigia-sites``: esa copia la hace el dueño."""
        provider = self.format_arn(
            service="iam",
            region="",
            resource="oidc-provider",
            resource_name=GITHUB_OIDC_HOST,
        )
        subjects = [
            f"repo:{GITHUB_REPOSITORY}:environment:{environment}"
            for environment in GITHUB_ENVIRONMENTS
        ]
        principal = iam.WebIdentityPrincipal(
            provider,
            conditions={
                "StringEquals": {
                    f"{GITHUB_OIDC_HOST}:aud": "sts.amazonaws.com",
                    f"{GITHUB_OIDC_HOST}:sub": subjects,
                }
            },
        )
        policy = self._policy(
            "deploy",
            "vigia-deploy: flujo de release de vigia-platform (8)",
            self._deploy_statements(),
        )
        return iam.Role(
            self,
            "Role-deploy",
            role_name=deploy_role_name(self.config),
            assumed_by=principal,
            managed_policies=[policy],
            description="vigia-deploy: GitHub Actions de vigia-platform, entornos pilot y staging",
        )

    def _staging_deploy_policy(self) -> iam.ManagedPolicy:
        """``vigia-deploy-staging-<n>`` (seguimiento de VIG-48 en VIG-95): ``staging-<n>`` nace
        vacío y con autoridad propia, así que el flujo de release lanza en él ``vigia-migrate`` y
        ``vigia-admin bootstrap`` (orden corregido del primer despliegue, nota U02-H-01) y lee la
        invitación sintética del arranque para la comprobación nº 5. Los recursos son los de este
        ``staging``; la política se adjunta al ``vigia-deploy`` de la cuenta, importado por nombre,
        y se destruye con la pila."""
        deploy_role = iam.Role.from_role_name(
            self, "ImportedDeployRole", deploy_role_name(self.config)
        )
        invitation = self._invitation_secret()
        statements = [
            *self._release_statements(with_admin=True),
            iam.PolicyStatement(
                sid="ReadBootstrapInvitation",
                actions=["secretsmanager:GetSecretValue"],
                resources=[invitation],
            ),
            iam.PolicyStatement(
                sid="BootstrapInvitationKey",
                actions=["kms:Decrypt"],
                resources=[self._key_arn(KeyName.SECRETS)],
                conditions={
                    "StringEquals": {
                        "kms:ViaService": f"secretsmanager.{self.region}.amazonaws.com"
                    }
                },
            ),
        ]
        policy = self._policy(
            "deploy",
            f"vigia-deploy en {self.config.deployment}: migrate, bootstrap y comprobaciones",
            statements,
        )
        policy.attach_to_role(deploy_role)
        return policy

    def _release_statements(self, *, with_admin: bool) -> list[iam.PolicyStatement]:
        """Tareas puntuales y servicios del despliegue. ``vigia-admin`` solo en ``staging-<n>``:
        en ``pilot`` las órdenes administrativas las lanza el dueño con su identidad (§5)."""
        on_cluster = {"ArnEquals": {"ecs:cluster": self.cluster.cluster_arn}}
        one_off = [ServiceName.MIGRATE, *([ServiceName.ADMIN] if with_admin else [])]
        passable = [
            TaskRole.TASK_EXECUTION,
            TaskRole.API_TASK,
            TaskRole.WORKER_TASK,
            TaskRole.MIGRATE_TASK,
            *([TaskRole.ADMIN_TASK] if with_admin else []),
        ]
        return [
            iam.PolicyStatement(
                sid="RunMigration",
                actions=["ecs:RunTask"],
                resources=[self.task_definitions[name].task_definition_arn for name in one_off],
                conditions=on_cluster,
            ),
            iam.PolicyStatement(
                sid="WatchMigration",
                actions=["ecs:DescribeTasks"],
                resources=[
                    self.format_arn(
                        service="ecs",
                        resource="task",
                        resource_name=f"{cluster_name(self.config)}/*",
                    )
                ],
                conditions=on_cluster,
            ),
            iam.PolicyStatement(
                sid="UpdateServices",
                actions=["ecs:UpdateService", "ecs:DescribeServices"],
                resources=[self.api_service.service_arn, self.worker_service.service_arn],
            ),
            iam.PolicyStatement(
                sid="PassTaskRoles",
                actions=["iam:PassRole"],
                resources=[self.roles[role].role_arn for role in passable],
                conditions={"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}},
            ),
        ]

    def _deploy_statements(self) -> list[iam.PolicyStatement]:
        bootstrap_roles = [
            self.format_arn(
                service="iam",
                region="",
                resource="role",
                resource_name=f"cdk-vigia-{name}-role-{self.account}-{self.region}",
            )
            for name in CDK_BOOTSTRAP_ROLES
        ]
        statements = [
            iam.PolicyStatement(
                sid="AssumeCdkBootstrapRoles", actions=["sts:AssumeRole"], resources=bootstrap_roles
            ),
            iam.PolicyStatement(
                sid="RegistryToken", actions=["ecr:GetAuthorizationToken"], resources=["*"]
            ),
            iam.PolicyStatement(
                sid="PushImage",
                actions=list(DEPLOY_ECR_ACTIONS),
                resources=[self.registry.repository_arn],
            ),
            *self._release_statements(with_admin=False),
            # Flujos de vigia-platform (TASK-151): salidas de las pilas para lanzar las tareas
            # puntuales, y el barrido y la comprobación de residuos de ``staging-<n>``, que
            # corren cuando la política de ese ``staging`` ya se destruyó con él.
            iam.PolicyStatement(
                sid="ReadStacks",
                actions=["cloudformation:DescribeStacks"],
                resources=[
                    self.format_arn(
                        service="cloudformation", resource="stack", resource_name="vigia-*"
                    )
                ],
            ),
            # Comprobaciones nº 2 y 7 (§3.2) y residuos de ``staging-<n>``: solo lectura, sin
            # permisos a nivel de recurso en estos servicios.
            iam.PolicyStatement(
                sid="ReadDeploymentState",
                actions=list(PIPELINE_READ_ACTIONS),
                resources=["*"],
            ),
            # Residuos: las claves de un ``staging`` destruido quedan con borrado programado.
            iam.PolicyStatement(
                sid="InspectStagingKeys",
                actions=["kms:DescribeKey"],
                resources=[self.format_arn(service="kms", resource="key", resource_name="*")],
                conditions={"StringLike": {"aws:ResourceTag/environment": "staging-*"}},
            ),
            # Nota U02-H-01: ``ModifyTrustStore`` lee el paquete con las credenciales del llamador;
            # ``AddTrustStoreRevocations`` (``trust-store.yml``, nº 19) lee una versión concreta.
            iam.PolicyStatement(
                sid="ReadEdgeCa",
                actions=["s3:GetObject", "s3:GetObjectVersion"],
                resources=[self._objects(BucketUsage.EDGE, "ca/*")],
            ),
            iam.PolicyStatement(
                sid="EdgeCaKey",
                actions=["kms:Decrypt"],
                resources=[self._key_arn(KeyName.SECRETS)],
                conditions={
                    **self._via_s3(),
                    "StringLike": {
                        "kms:EncryptionContext:aws:s3:arn": self._objects(BucketUsage.EDGE, "ca/*")
                    },
                },
            ),
        ]
        trust_store = self.edge.trust_store
        if trust_store is not None:
            statements.append(
                iam.PolicyStatement(
                    sid="UpdateTrustStore",
                    actions=list(TRUST_STORE_ACTIONS),
                    resources=[trust_store.trust_store_arn],
                )
            )
        return statements

    # --- Definiciones de tarea ------------------------------------------------------------

    def _logging(self, process: str) -> ecs.LogDriver:
        if process not in self._log_groups:
            self._log_groups[process] = logs.LogGroup.from_log_group_name(
                self, f"LogGroup-{process}", log_group_name(self.config, process)
            )
        group = self._log_groups[process]
        return ecs.LogDrivers.aws_logs(
            stream_prefix=process,
            log_group=group,
            mode=ecs.AwsLogDriverMode.NON_BLOCKING,
            max_buffer_size=LOG_BUFFER,
        )

    def _linux(self, name: str) -> ecs.LinuxParameters:
        """Sin capacidades: se quitan todas y no se añade ninguna (nota U02-H-10)."""
        parameters = ecs.LinuxParameters(self, f"Linux-{name}", init_process_enabled=True)
        parameters.drop_capabilities(ecs.Capability.ALL)
        return parameters

    def _task_definition(
        self, name: ServiceName, size: TaskSize, task_role: TaskRole
    ) -> ecs.FargateTaskDefinition:
        task_definition = ecs.FargateTaskDefinition(
            self,
            f"TaskDefinition-{name.value}",
            family=self.config.resource_name(name.value),
            cpu=size.cpu,
            memory_limit_mib=size.memory_mib,
            ephemeral_storage_gib=EPHEMERAL_STORAGE_GIB,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
            execution_role=self._immutable(TaskRole.TASK_EXECUTION),
            task_role=self._immutable(task_role),
            # Volumen de la tarea sin origen: vive en el almacenamiento efímero (nota U02-H-10).
            volumes=[ecs.Volume(name=TMP_VOLUME)],
        )
        self.task_definitions[name] = task_definition
        return task_definition

    def _main_container(
        self,
        task_definition: ecs.FargateTaskDefinition,
        name: ServiceName,
        environment: Mapping[str, str],
        *,
        command: Sequence[str] | None = None,
        ports: Sequence[int] = (),
        health_port: int | None = None,
        stop_timeout: Duration | None = None,
    ) -> ecs.ContainerDefinition:
        container = task_definition.add_container(
            name.value,
            container_name=name.value,
            image=self._image(),
            command=list(command) if command else None,
            essential=True,
            readonly_root_filesystem=True,
            user=CONTAINER_USER,
            linux_parameters=self._linux(name.value),
            environment=dict(environment),
            logging=self._logging(name.value),
            port_mappings=[ecs.PortMapping(container_port=port) for port in ports] or None,
            health_check=(
                ecs.HealthCheck(
                    command=_health_probe(health_port),
                    interval=Duration.seconds(30),
                    timeout=Duration.seconds(5),
                    retries=3,
                    start_period=Duration.seconds(30),
                )
                if health_port
                else None
            ),
            stop_timeout=stop_timeout,
        )
        container.add_mount_points(
            ecs.MountPoint(container_path=TMP_PATH, source_volume=TMP_VOLUME, read_only=False)
        )
        return container

    def _collector(
        self,
        task_definition: ecs.FargateTaskDefinition,
        main: ecs.ContainerDefinition,
        name: ServiceName,
        service: str,
    ) -> ecs.ContainerDefinition:
        """Colector ADOT lateral (§9.1): no esencial; ECS lo reinicia si cae."""
        values = {
            "AWS_REGION": self.region,
            "VIGIA_SERVICE": service,
            "VIGIA_ENVIRONMENT": self.config.deployment,
            "VIGIA_OTEL_LOG_GROUP": log_group_name(self.config, "otel"),
        }
        collector = task_definition.add_container(
            "otel",
            container_name="otel",
            image=ecs.ContainerImage.from_registry(OTEL_COLLECTOR_IMAGE),
            essential=False,
            enable_restart_policy=True,
            restart_attempt_period=OTEL_RESTART_PERIOD,
            memory_reservation_mib=OTEL_MEMORY_RESERVATION_MIB[name.value],
            readonly_root_filesystem=True,
            linux_parameters=self._linux(f"otel-{name.value}"),
            environment={"AOT_CONFIG_CONTENT": render_collector_config(values), **values},
            logging=self._logging("otel"),
        )
        main.add_container_dependencies(
            ecs.ContainerDependency(
                container=collector, condition=ecs.ContainerDependencyCondition.START
            )
        )
        return collector

    # --- Variables (§5.3; ninguna es un secreto) ------------------------------------------

    def _common_environment(self, service: str) -> dict[str, str]:
        config = self.config
        return {
            "VIGIA_ENVIRONMENT": config.deployment,
            "VIGIA_SERVICE": service,
            "AWS_REGION": self.region,
            "OTEL_EXPORTER_OTLP_ENDPOINT": OTEL_ENDPOINT,
            "VIGIA_METRICS_NAMESPACE": METRICS_NAMESPACE,
            "VIGIA_EVIDENCE_BUCKET": self._bucket(BucketUsage.EVIDENCE),
            "VIGIA_DB_APP_SECRET": db_secret_name(config, DbUser.APP),
            "VIGIA_SIGNING_SECRET_PREFIX": f"vigia/{config.deployment}/signing/",
            "VIGIA_SECRETS_KEY_ARN": self._key_arn(KeyName.SECRETS),
            "VIGIA_NODE_CA_KEY_ARN": self._key_arn(KeyName.NODE_CA),
        }

    def _api_environment(self) -> dict[str, str]:
        config = self.config
        domain = self.edge.domain
        environment = {
            **self._common_environment(API_SERVICE),
            **API_TUNING,
            # Nº 10: un solo origen, el depósito de evidencias del entorno.
            "VIGIA_CSP_STORE_ORIGINS": evidence_store_origin(
                self._bucket(BucketUsage.EVIDENCE), self.region
            ),
            "VIGIA_ENROLLMENT_PUBLIC_URL": Fn.join(
                "", ["https://", app_host(config), ".", domain, "/api/nodes/enrollment"]
            ),
            "VIGIA_NODES_BASE_URL": Fn.join(
                "", ["https://", nodes_host(config), ".", domain, "/api/nodes"]
            ),
            "VIGIA_NODES_TLS_MODE": config.nodes_tls_mode.value,
        }
        if config.nodes_tls_mode is NodesTlsMode.PASSTHROUGH:
            environment["VIGIA_NODES_TLS_PORT"] = str(PASSTHROUGH_PORT)
        return environment

    def _worker_environment(self) -> dict[str, str]:
        environment = {
            **self._common_environment(WORKER_SERVICE),
            "VIGIA_DB_STATEMENT_TIMEOUT_MS": WORKER_STATEMENT_TIMEOUT_MS,
            "VIGIA_ARCHIVE_BUCKET": self._bucket(BucketUsage.ARCHIVE),
            "VIGIA_EDGE_BUCKET": self._bucket(BucketUsage.EDGE),
            "VIGIA_CRL_KEY": CRL_KEY,
            "VIGIA_TMP_BUDGET_BYTES": str(WORKER_TMP_BUDGET_BYTES),
        }
        if self.edge.trust_store is not None:
            environment["VIGIA_NODE_TRUST_STORE_ARN"] = self.edge.trust_store.trust_store_arn
        return environment

    def _migrate_environment(self) -> dict[str, str]:
        config = self.config
        environment = {
            "VIGIA_ENVIRONMENT": config.deployment,
            "AWS_REGION": self.region,
            "VIGIA_DB_MIGRATE_SECRET": db_secret_name(config, DbUser.MIGRATE),
        }
        if config.first_deploy:
            # RDS elige el nombre del secreto maestro (``rds!db-...``); va su ARN, no su valor.
            environment["VIGIA_DB_MASTER_SECRET_ARN"] = (
                self.data.database.attr_master_user_secret_secret_arn
            )
            environment["VIGIA_DB_APP_SECRET"] = db_secret_name(config, DbUser.APP)
        return environment

    def _admin_environment(self) -> dict[str, str]:
        config = self.config
        environment = {
            "VIGIA_ENVIRONMENT": config.deployment,
            "AWS_REGION": self.region,
            "VIGIA_DB_APP_SECRET": db_secret_name(config, DbUser.APP),
            "VIGIA_BOOTSTRAP_INVITATION_SECRET": f"vigia/{config.deployment}/bootstrap/invitation",
            "VIGIA_SIGNING_SECRET_PREFIX": f"vigia/{config.deployment}/signing/",
            "VIGIA_SECRETS_KEY_ARN": self._key_arn(KeyName.SECRETS),
        }
        if config.elevated_bootstrap:
            environment |= {
                "VIGIA_NODE_CA_KEY_ARN": self._key_arn(KeyName.NODE_CA),
                "VIGIA_EDGE_BUCKET": self._bucket(BucketUsage.EDGE),
                "VIGIA_ROOT_CERTIFICATE_KEY": ROOT_CERTIFICATE_KEY,
            }
        return environment

    # --- Servicios ------------------------------------------------------------------------

    def _service(
        self,
        name: str,
        task_definition: ecs.FargateTaskDefinition,
        group: SecurityGroupName,
        desired: int,
    ) -> ecs.FargateService:
        return ecs.FargateService(
            self,
            f"Service-{name}",
            service_name=name,
            cluster=self.cluster,
            task_definition=task_definition,
            desired_count=desired,
            platform_version=ecs.FargatePlatformVersion.LATEST,
            # Una subred de aplicación por zona: Fargate reparte las tareas entre las dos (§5.2).
            vpc_subnets=ec2.SubnetSelection(subnet_group_name=APP_SUBNETS),
            security_groups=[self.foundation.security_groups[group]],
            assign_public_ip=False,
            min_healthy_percent=100,
            max_healthy_percent=200,
            circuit_breaker=ecs.DeploymentCircuitBreaker(enable=True, rollback=True),
            enable_execute_command=False,
            availability_zone_rebalancing=ecs.AvailabilityZoneRebalancing.ENABLED,
        )

    def _api_service(self) -> ecs.FargateService:
        config = self.config
        task_definition = self._task_definition(ServiceName.API, API_SIZE, TaskRole.API_TASK)
        passthrough = config.nodes_tls_mode is NodesTlsMode.PASSTHROUGH
        ports = (API_PORT, PASSTHROUGH_PORT) if passthrough else (API_PORT,)
        api = self._main_container(
            task_definition,
            ServiceName.API,
            self._api_environment(),
            ports=ports,
            health_port=API_PORT,
        )
        self._collector(task_definition, api, ServiceName.API, API_SERVICE)
        service = self._service(
            API_SERVICE, task_definition, SecurityGroupName.API, config.api_desired_tasks
        )
        # Registro en los grupos de ``vigia-edge`` por el recurso de nivel 1: el constructo de
        # nivel 2 añadiría reglas y dependencias en la pila del balanceador.
        targets: list[IResolvable | ecs.CfnService.LoadBalancerProperty] = [
            ecs.CfnService.LoadBalancerProperty(
                container_name=ServiceName.API.value,
                container_port=API_PORT,
                target_group_arn=self.edge.app_target_group.target_group_arn,
            )
        ]
        if self.edge.nodes_listener is not None:
            targets.append(
                ecs.CfnService.LoadBalancerProperty(
                    container_name=ServiceName.API.value,
                    container_port=PASSTHROUGH_PORT if passthrough else API_PORT,
                    target_group_arn=self.edge.nodes_target_group.target_group_arn,
                )
            )
        cfn_service = service.node.default_child
        if not isinstance(cfn_service, ecs.CfnService):
            raise TypeError("el servicio vigia-api no tiene su recurso de nivel 1")
        cfn_service.load_balancers = targets
        cfn_service.health_check_grace_period_seconds = int(HEALTH_GRACE_PERIOD.to_seconds())
        return service

    def _worker_service(self) -> ecs.FargateService:
        config = self.config
        task_definition = self._task_definition(
            ServiceName.WORKER, WORKER_SIZE, TaskRole.WORKER_TASK
        )
        worker = self._main_container(
            task_definition,
            ServiceName.WORKER,
            self._worker_environment(),
            command=WORKER_COMMAND,
            health_port=WORKER_HEALTH_PORT,
            stop_timeout=WORKER_STOP_TIMEOUT,
        )
        self._collector(task_definition, worker, ServiceName.WORKER, WORKER_SERVICE)
        return self._service(
            WORKER_SERVICE, task_definition, SecurityGroupName.WORKER, config.worker_desired_tasks
        )

    def _one_off(
        self, name: ServiceName, command: Sequence[str], environment: Mapping[str, str]
    ) -> ecs.FargateTaskDefinition:
        role = TaskRole.MIGRATE_TASK if name is ServiceName.MIGRATE else TaskRole.ADMIN_TASK
        task_definition = self._task_definition(name, ONE_OFF_SIZE, role)
        self._main_container(task_definition, name, environment, command=command)
        return task_definition

    # --- Escalado -------------------------------------------------------------------------

    def _autoscaling_role(self) -> iam.IRole:
        """El rol vinculado al servicio, como el escalado de ECS de CDK: sin rol propio."""
        return iam.Role.from_role_arn(
            self,
            "AutoScalingRole",
            self.format_arn(
                service="iam",
                region="",
                resource="role",
                resource_name=AUTOSCALING_SERVICE_LINKED_ROLE,
            ),
        )

    def _scalable_target(
        self,
        construct_id: str,
        service: ecs.FargateService,
        minimum: int,
        maximum: int,
        role: iam.IRole,
    ) -> appscaling.ScalableTarget:
        target = appscaling.ScalableTarget(
            self,
            construct_id,
            service_namespace=appscaling.ServiceNamespace.ECS,
            scalable_dimension="ecs:service:DesiredCount",
            resource_id=f"service/{self.cluster.cluster_name}/{service.service_name}",
            min_capacity=minimum,
            max_capacity=maximum,
            role=role,
        )
        target.node.add_dependency(service)
        return target

    def _api_scaling(self, role: iam.IRole) -> appscaling.ScalableTarget:
        config = self.config
        target = self._scalable_target(
            "ApiScaling", self.api_service, config.api_min_tasks, config.api_max_tasks, role
        )
        target.scale_to_track_metric(
            "Cpu",
            target_value=CPU_TARGET_PERCENT,
            predefined_metric=appscaling.PredefinedMetric.ECS_SERVICE_AVERAGE_CPU_UTILIZATION,
            scale_in_cooldown=API_COOLDOWN,
            scale_out_cooldown=API_COOLDOWN,
        )
        target_group = self.edge.app_target_group
        target.scale_to_track_metric(
            "Requests",
            target_value=API_REQUESTS_PER_TARGET,
            predefined_metric=appscaling.PredefinedMetric.ALB_REQUEST_COUNT_PER_TARGET,
            resource_label=Fn.join(
                "/",
                [target_group.first_load_balancer_full_name, target_group.target_group_full_name],
            ),
            scale_in_cooldown=API_COOLDOWN,
            scale_out_cooldown=API_COOLDOWN,
        )
        return target

    def _worker_scaling(self, role: iam.IRole) -> appscaling.ScalableTarget:
        config = self.config
        target = self._scalable_target(
            "WorkerScaling",
            self.worker_service,
            config.worker_min_tasks,
            config.worker_max_tasks,
            role,
        )
        # La reducción la decide la antigüedad de la bandeja (15 minutos por debajo).
        target.scale_to_track_metric(
            "Cpu",
            target_value=CPU_TARGET_PERCENT,
            predefined_metric=appscaling.PredefinedMetric.ECS_SERVICE_AVERAGE_CPU_UTILIZATION,
            disable_scale_in=True,
        )
        age = cloudwatch.Metric(
            namespace=METRICS_NAMESPACE,
            metric_name=OUTBOX_AGE_METRIC,
            dimensions_map={"service": WORKER_SERVICE, "environment": config.deployment},
            statistic="Maximum",
            period=Duration.minutes(1),
        )
        self._step(
            "WorkerOutboxScaleOut",
            target,
            age,
            adjustment=1,
            minutes=OUTBOX_SCALE_OUT_MINUTES,
            comparison=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        )
        self._step(
            "WorkerOutboxScaleIn",
            target,
            age,
            adjustment=-1,
            minutes=WORKER_SCALE_IN_MINUTES,
            comparison=cloudwatch.ComparisonOperator.LESS_THAN_OR_EQUAL_TO_THRESHOLD,
        )
        return target

    def _step(
        self,
        construct_id: str,
        target: appscaling.ScalableTarget,
        metric: cloudwatch.Metric,
        *,
        adjustment: int,
        minutes: int,
        comparison: cloudwatch.ComparisonOperator,
    ) -> None:
        action = appscaling.StepScalingAction(
            self,
            construct_id,
            scaling_target=target,
            adjustment_type=appscaling.AdjustmentType.CHANGE_IN_CAPACITY,
            metric_aggregation_type=appscaling.MetricAggregationType.MAXIMUM,
            cooldown=Duration.minutes(minutes),
        )
        if adjustment > 0:
            action.add_adjustment(adjustment=adjustment, lower_bound=0)
        else:
            action.add_adjustment(adjustment=adjustment, upper_bound=0)
        alarm = cloudwatch.Alarm(
            self,
            f"{construct_id}Alarm",
            metric=metric,
            threshold=OUTBOX_AGE_THRESHOLD_SECONDS,
            evaluation_periods=minutes,
            comparison_operator=comparison,
            # Sin datos (bandeja vacía y sin emisión) no se escala hacia arriba; hacia abajo sí.
            treat_missing_data=(
                cloudwatch.TreatMissingData.NOT_BREACHING
                if adjustment > 0
                else cloudwatch.TreatMissingData.BREACHING
            ),
            alarm_description=(
                f"Escalado de vigia-worker por {OUTBOX_AGE_METRIC} "
                f"({'+' if adjustment > 0 else ''}{adjustment} tras {minutes} min, 5.2)"
            ),
        )
        alarm.add_alarm_action(cloudwatch_actions.ApplicationScalingAction(action))

    # --- Salidas para la canalización y ``make admin`` (sin exportar) ---------------------

    def _outputs(self) -> None:
        values = {
            "ClusterName": self.cluster.cluster_name,
            "MigrateTaskDefinition": self.task_definitions[ServiceName.MIGRATE].task_definition_arn,
            "AdminTaskDefinition": self.task_definitions[ServiceName.ADMIN].task_definition_arn,
            "OneOffSecurityGroup": self.foundation.security_groups[
                SecurityGroupName.TASKS
            ].security_group_id,
            "AppSubnets": Fn.join(
                ",",
                self.foundation.vpc.select_subnets(subnet_group_name=APP_SUBNETS).subnet_ids,
            ),
        }
        if self.edge.trust_store is not None:
            # ``trust-store.yml`` (respaldo manual de la lista de revocación, nº 19).
            values["NodeTrustStoreArn"] = self.edge.trust_store.trust_store_arn
        for name, value in values.items():
            CfnOutput(self, name, value=value)


__all__ = [
    "API_TUNING",
    "EVIDENCE_UPLOAD_PREFIXES",
    "GITHUB_REPOSITORY",
    "IMAGE_DIGEST_CONTEXT",
    "PIPELINE_READ_ACTIONS",
    "TRUST_STORE_ACTIONS",
    "ComputeStack",
    "ServiceName",
    "cluster_name",
    "deploy_role_owned",
    "evidence_store_origin",
    "log_group_name",
    "read_image_digest",
    "registry_owned",
]
