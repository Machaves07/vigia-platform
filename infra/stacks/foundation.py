"""Pila ``vigia-foundation``: red y puntos privados (§3), claves KMS (§7.1), tema ``vigia-alerts``,
presupuestos (§2.2) y grupo de registro de flujos. Sin dependencias. Recursos: TASK-145.

Red (infrastructure-design §3):

- VPC ``vigia-vpc-<despliegue>`` ``10.40.0.0/16`` en ``us-east-1a`` y ``us-east-1b``: subredes
  públicas ``/24``, de aplicación ``/22`` (ruta por defecto a la traducción) y de datos ``/24``
  aisladas, **sin ruta por defecto**. Registros de flujo de todo el tráfico a
  ``/vigia/<despliegue>/vpc-flow`` (90 días, cifrado con ``vigia-logs``).
- Una traducción de direcciones ``vigia-nat-a`` en ``us-east-1a`` (excepción R13, §13);
  ``nat_per_az=true`` añade ``vigia-nat-b`` y cada subred de aplicación sale por la de su zona.
- Punto de puerta de enlace de S3 en las subredes de aplicación y de datos, con política
  limitada a los depósitos ``vigia-*`` de la cuenta; puntos de interfaz de Secrets Manager, KMS
  y CloudWatch Logs en las dos zonas, con DNS privado y el grupo ``sg-endpoints``.
- Grupos ``sg-api``, ``sg-worker``, ``sg-tasks``, ``sg-db`` y ``sg-endpoints`` de la tabla de §3,
  sin salida abierta por defecto. ``sg-alb-app`` y ``sg-alb-nodes`` (los únicos con entrada
  pública) y la regla 8000 de ``sg-api`` desde ellos son de ``vigia-edge`` (TASK-147).
  ``sg-worker`` se publica en ``/vigia/<despliegue>/sg-worker-id`` (contrato de salidas nº 38).

Claves (§7.1 y su nota de 2026-09-23, U02-H-01): seis simétricas con rotación anual y
``vigia-node-ca`` (ECC P-256, ``SIGN_VERIFY``, sin rotación). Cada política de clave es
explícita, sin la sentencia por defecto de CDK (``kms:*`` a la cuenta), y sin comodines en
principales ni acciones:

- Los roles de ``vigia-compute`` (§8) aún no existen al desplegar esta pila y KMS rechaza una
  política cuyo principal no existe; por eso cada rol se nombra por su ARN en la condición
  ``aws:PrincipalArn`` de una sentencia cuyo principal es la cuenta. El rol necesita además su
  política de identidad (§8): la clave no concede nada por sí sola.
- La administración de la clave (sin uso de datos) es del rol de ejecución de CloudFormation del
  arranque ``vigia`` de CDK, que es por donde la cambia el dueño.
- ``vigia-node-ca`` concede ``Sign`` y ``GetPublicKey`` a ``vigia-admin-task`` **solo** con
  ``first_deploy=true`` o ``ca_rotation=true``; ``vigia-secrets`` concede a ``vigia-deploy``
  ``Decrypt`` acotado por ``kms:ViaService`` de S3 a la lectura de ``vigia-edge/ca/*``.
- Para ``vigia-data`` (TASK-146): ``vigia-secrets`` admite, solo por Secrets Manager, los secretos
  de la base (``vigia/<despliegue>/db/*`` y el maestro ``rds!db-*`` que gestiona RDS), cuyo rol de
  rotación genera el servicio; ``vigia-logs`` cifra también el grupo de registros de PostgreSQL de
  la base del despliegue, y el tema ``vigia-alerts`` admite la publicación de eventos de RDS.

En ``staging-<n>`` (D-8) todo se destruye con el entorno: claves con borrado programado de 7 días
(``vigia-node-ca`` es la de la ejecución) y grupo de registro con ``DESTROY``. Los presupuestos
``vigia-monthly`` y ``vigia-staging`` son de la cuenta y solo se sintetizan con
``environment=pilot``: un ``staging-<n>`` los duplicaría.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from aws_cdk import ArnFormat, Duration, RemovalPolicy, Tags
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from config import BOOTSTRAP_QUALIFIER, EnvironmentConfig
from config.environment import PILOT, SHARED_INSTANCE
from stacks.base import VigiaStack
from stacks.outputs import Output, TaskRole, publish, role_name

# --- Red (§3) -----------------------------------------------------------------------------

VPC_CIDR = "10.40.0.0/16"  # [objetivo propio]
ZONES = ("us-east-1a", "us-east-1b")
ZONE_LETTERS = ("a", "b")
PUBLIC_SUBNETS = "public"
APP_SUBNETS = "app"
DATA_SUBNETS = "data"
PUBLIC_MASK = 24
APP_MASK = 22
DATA_MASK = 24
FLOW_LOG_RETENTION = logs.RetentionDays.THREE_MONTHS  # 90 días
POSTGRES_PORT = 5432
HTTPS_PORT = 443
ANYWHERE_IPV4 = "0.0.0.0/0"

# Acciones de S3 que admite el punto de puerta de enlace: lectura, escritura (también por
# partes, U-04) y listado de los depósitos ``vigia-*``. Ninguna de borrado (NFR-NUC-20, 28).
S3_ENDPOINT_ACTIONS = (
    "s3:GetObject",
    "s3:GetObjectVersion",
    "s3:PutObject",
    "s3:AbortMultipartUpload",
    "s3:ListMultipartUploadParts",
    "s3:ListBucket",
    "s3:GetBucketLocation",
)
# Fargate descarga las capas de las imágenes del registro desde este depósito del servicio, y
# con el punto de puerta de enlace esa descarga pasa por él: sin esta sentencia no arranca
# ninguna tarea (documentación de ECR sobre puntos privados de S3).
ECR_LAYER_BUCKET = "prod-us-east-1-starport-layer-bucket"


class SecurityGroupName(StrEnum):
    """Grupos de seguridad de §3 que crea esta pila."""

    API = "sg-api"
    WORKER = "sg-worker"
    TASKS = "sg-tasks"
    DB = "sg-db"
    ENDPOINTS = "sg-endpoints"


# Grupos de las tareas: salida a la base, a los puntos privados y 443 por la traducción.
TASK_GROUPS = (SecurityGroupName.API, SecurityGroupName.WORKER, SecurityGroupName.TASKS)

INTERFACE_ENDPOINTS: Mapping[str, ec2.InterfaceVpcEndpointAwsService] = {
    "SecretsManager": ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
    "Kms": ec2.InterfaceVpcEndpointAwsService.KMS,
    "Logs": ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
}

# --- Claves (§7.1) ------------------------------------------------------------------------


class KeyName(StrEnum):
    """Claves del cliente por clase de dato; el alias es ``alias/vigia-<clave>[-<sufijo>]``."""

    DB = "db"
    EVIDENCE = "evidence"
    SECRETS = "secrets"
    BACKUP = "backup"
    ARCHIVE = "archive"
    LOGS = "logs"
    NODE_CA = "node-ca"


SYMMETRIC_KEYS = (
    KeyName.DB,
    KeyName.EVIDENCE,
    KeyName.SECRETS,
    KeyName.BACKUP,
    KeyName.ARCHIVE,
    KeyName.LOGS,
)
ROTATION_PERIOD = Duration.days(365)

_DESCRIPTIONS: Mapping[KeyName, str] = {
    KeyName.DB: "Base de datos gestionada e instantaneas (7.1)",
    KeyName.EVIDENCE: "Deposito de evidencias (7.1)",
    KeyName.SECRETS: "Secretos, cifrado de sobre y deposito vigia-edge (7.1)",
    KeyName.BACKUP: "Boveda de copias (7.1)",
    KeyName.ARCHIVE: "Deposito de archivos de auditoria (7.1)",
    KeyName.LOGS: "Grupos de registro de CloudWatch (7.1)",
    KeyName.NODE_CA: "Autoridad de certificados de los nodos, ECDSA P-256 (7.1)",
}

# Administración sin uso de datos, para el rol de ejecución de CloudFormation.
KEY_ADMIN_ACTIONS = (
    "kms:DescribeKey",
    "kms:GetKeyPolicy",
    "kms:PutKeyPolicy",
    "kms:ListResourceTags",
    "kms:TagResource",
    "kms:UntagResource",
    "kms:UpdateKeyDescription",
    "kms:EnableKey",
    "kms:DisableKey",
    "kms:ScheduleKeyDeletion",
    "kms:CancelKeyDeletion",
    "kms:CreateAlias",
    "kms:DeleteAlias",
    "kms:UpdateAlias",
)
KEY_ROTATION_ADMIN_ACTIONS = (
    "kms:GetKeyRotationStatus",
    "kms:EnableKeyRotation",
    "kms:DisableKeyRotation",
)
# Uso de una clave simétrica por un servicio en nombre de la cuenta (RDS, AWS Backup).
SERVICE_USE_ACTIONS = (
    "kms:Encrypt",
    "kms:Decrypt",
    "kms:ReEncryptFrom",
    "kms:ReEncryptTo",
    "kms:GenerateDataKey",
    "kms:GenerateDataKeyWithoutPlaintext",
    "kms:DescribeKey",
)
LOGS_SERVICE_ACTIONS = SERVICE_USE_ACTIONS
SIGNING_ALGORITHM = "ECDSA_SHA_256"

# Presupuestos (§2.2), [objetivo propio].
MONTHLY_BUDGET_USD = 400
MONTHLY_ALERT_PERCENTAGES = (80, 100)
STAGING_BUDGET_USD = 40
STAGING_ALERT_PERCENTAGES = (100,)
MONTHLY_BUDGET = "monthly"
STAGING_BUDGET = "staging"

# Correo del dueño para la suscripción de ``vigia-alerts``: parámetro SSM de la cuenta que el
# dueño crea antes del primer despliegue (registro de la cuenta, P1). Nunca en el repositorio.
ALERTS_EMAIL_PARAMETER = "/vigia/alerts-email"

# Grupo de registro del cortafuegos de ``vigia-edge``; su clave es ``vigia-logs``.
WAF_LOG_GROUP_PREFIX = "aws-waf-logs-"


@dataclass(frozen=True)
class Principals:
    """ARN de los roles que nombran las políticas de clave (§7.1 y §8)."""

    cfn_execution: str
    api_task: str
    worker_task: str
    migrate_task: str
    admin_task: str
    deploy: str
    backup: str
    restore: str


def deploy_role_name(config: EnvironmentConfig) -> str:
    """``vigia-deploy`` de la cuenta compartida (entornos de GitHub ``pilot`` y ``staging``);
    ``vigia-deploy-<cliente>`` en una instancia dedicada (deployment-architecture §7)."""
    if config.instance == SHARED_INSTANCE:
        return "vigia-deploy"
    return f"vigia-deploy-{config.instance}"


def waf_log_group_name(config: EnvironmentConfig) -> str:
    """``aws-waf-logs-vigia-app`` (§9.2); el servicio exige el prefijo ``aws-waf-logs-``."""
    return f"{WAF_LOG_GROUP_PREFIX}{config.resource_name('app')}"


def budgets_enabled(config: EnvironmentConfig) -> bool:
    """Los presupuestos son de la cuenta: solo con ``environment=pilot``."""
    return config.environment == PILOT


class FoundationStack(VigiaStack):
    """Pila ``vigia-foundation`` (infrastructure-design §2.3)."""

    key = "foundation"
    summary = "red, puntos privados, claves KMS, tema de alertas, presupuestos"

    def __init__(
        self, scope: Construct, config: EnvironmentConfig, *, tags: Mapping[str, str]
    ) -> None:
        super().__init__(scope, config, tags=tags)
        self.principals = self._principals()
        self.keys: dict[KeyName, kms.Key] = self._keys()
        self.vpc = self._vpc()
        self.flow_log_group = self._flow_logs()
        self.security_groups = self._security_groups()
        self.s3_endpoint = self._s3_endpoint()
        self.interface_endpoints = self._interface_endpoints()
        self.alerts_topic = self._alerts_topic()
        self.budgets = self._budgets() if budgets_enabled(config) else []
        publish(
            self,
            config,
            Output.WORKER_SECURITY_GROUP_ID,
            self.security_groups[SecurityGroupName.WORKER].security_group_id,
        )

    # --- Principales ----------------------------------------------------------------------

    def _role_arn(self, name: str) -> str:
        return self.format_arn(
            service="iam", region="", account=self.account, resource="role", resource_name=name
        )

    def _principals(self) -> Principals:
        config = self.config
        return Principals(
            # Rol del arranque ``cdk bootstrap --qualifier vigia`` (DefaultStackSynthesizer).
            cfn_execution=self._role_arn(
                f"cdk-{BOOTSTRAP_QUALIFIER}-cfn-exec-role-{self.account}-{self.region}"
            ),
            api_task=self._role_arn(role_name(config, TaskRole.API_TASK)),
            worker_task=self._role_arn(role_name(config, TaskRole.WORKER_TASK)),
            migrate_task=self._role_arn(role_name(config, TaskRole.MIGRATE_TASK)),
            admin_task=self._role_arn(role_name(config, TaskRole.ADMIN_TASK)),
            deploy=self._role_arn(deploy_role_name(config)),
            backup=self._role_arn(config.resource_name("backup")),
            restore=self._role_arn(config.resource_name("restore")),
        )

    # --- Claves ---------------------------------------------------------------------------

    def _for_roles(
        self,
        sid: str,
        roles: Sequence[str],
        actions: Sequence[str],
        conditions: Mapping[str, Mapping[str, object]] | None = None,
    ) -> iam.PolicyStatement:
        """Sentencia para roles concretos por ``aws:PrincipalArn`` (sin exigir que existan)."""
        return iam.PolicyStatement(
            sid=sid,
            principals=[iam.AccountRootPrincipal()],
            actions=list(actions),
            resources=["*"],
            conditions={
                "ArnEquals": {"aws:PrincipalArn": list(roles)},
                **(conditions or {}),
            },
        )

    def _via_service(self, service: str) -> dict[str, dict[str, object]]:
        return {"StringEquals": {"kms:ViaService": f"{service}.{self.region}.amazonaws.com"}}

    def _via_account_service(
        self, sid: str, service: str, actions: Sequence[str]
    ) -> iam.PolicyStatement:
        """Uso de la clave por un servicio en nombre de cualquier identidad de la cuenta."""
        return iam.PolicyStatement(
            sid=sid,
            principals=[iam.AccountRootPrincipal()],
            actions=list(actions),
            resources=["*"],
            conditions={
                "StringEquals": {
                    "kms:ViaService": f"{service}.{self.region}.amazonaws.com",
                    "kms:CallerAccount": self.account,
                }
            },
        )

    def _grant_for_aws_resource(self, sid: str, roles: Sequence[str]) -> iam.PolicyStatement:
        return self._for_roles(
            sid,
            roles,
            ["kms:CreateGrant"],
            {"Bool": {"kms:GrantIsForAWSResource": "true"}},
        )

    def _admin(self, name: KeyName) -> iam.PolicyStatement:
        actions: tuple[str, ...] = KEY_ADMIN_ACTIONS
        if name in SYMMETRIC_KEYS:
            actions += KEY_ROTATION_ADMIN_ACTIONS
        return self._for_roles("KeyAdministration", [self.principals.cfn_execution], actions)

    def _usage(self, name: KeyName) -> list[iam.PolicyStatement]:
        """Sentencias de uso de cada clave: tabla de §7.1 y nota U02-H-01."""
        p = self.principals
        s3 = self._via_service("s3")
        if name is KeyName.DB:
            return [
                self._via_account_service("RdsForTheAccount", "rds", SERVICE_USE_ACTIONS),
                self._grant_for_aws_resource_via("RdsGrants", "rds"),
                self._for_roles(
                    "BackupRole",
                    [p.backup],
                    ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
                ),
                self._grant_for_aws_resource("BackupRoleGrants", [p.backup]),
            ]
        if name is KeyName.EVIDENCE:
            return [
                self._for_roles(
                    "ApiAndWorkerThroughS3",
                    [p.api_task, p.worker_task],
                    ["kms:Decrypt", "kms:GenerateDataKey"],
                    s3,
                ),
                # Notas U02-H-14 de deployment-architecture §6.1 (TASK-148): el ensayo lee una
                # versión anterior de una evidencia y la escribe en vigia-drill, con esta clave.
                self._for_roles(
                    "RestoreDrillThroughS3",
                    [p.restore],
                    ["kms:Decrypt", "kms:GenerateDataKey"],
                    s3,
                ),
            ]
        if name is KeyName.SECRETS:
            statements = [
                self._for_roles(
                    "TaskRoles",
                    [p.api_task, p.worker_task, p.admin_task],
                    ["kms:GenerateDataKey", "kms:Decrypt"],
                ),
                self._for_roles("MigrateTask", [p.migrate_task], ["kms:Decrypt"]),
                # Nota U02-H-01 (2): la actualización del almacén de confianza lee el paquete
                # de ``vigia-edge/ca/*`` con las credenciales de ``vigia-deploy``.
                self._for_roles(
                    "DeployReadsEdgeCa",
                    [p.deploy],
                    ["kms:Decrypt"],
                    {
                        **s3,
                        "StringLike": {"kms:EncryptionContext:aws:s3:arn": self._edge_ca_arn()},
                    },
                ),
                # ``vigia-edge`` (paso 8 del primer despliegue): CloudFormation crea el almacén de
                # confianza ``vigia-node-trust`` y el servicio de balanceo lee ``ca/root.pem``
                # con las credenciales de quien llama (hipótesis (b) de R14 en U-03).
                self._for_roles(
                    "CloudFormationReadsEdgeCa",
                    [p.cfn_execution],
                    ["kms:Decrypt"],
                    {
                        **s3,
                        "StringLike": {"kms:EncryptionContext:aws:s3:arn": self._edge_ca_arn()},
                    },
                ),
                # CloudFormation crea los secretos de ``vigia-data`` cifrados con esta clave.
                self._for_roles(
                    "CloudFormationCreatesSecrets",
                    [p.cfn_execution],
                    ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
                    self._via_service("secretsmanager"),
                ),
                self._db_secrets_rotation(),
            ]
            return statements
        if name is KeyName.BACKUP:
            return [
                self._via_account_service("BackupForTheAccount", "backup", SERVICE_USE_ACTIONS),
                self._grant_for_aws_resource_via("BackupGrants", "backup"),
                self._for_roles(
                    "BackupRole",
                    [p.backup],
                    ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
                ),
                self._grant_for_aws_resource("BackupRoleGrants", [p.backup]),
            ]
        if name is KeyName.ARCHIVE:
            return [
                self._for_roles(
                    "WorkerWritesThroughS3",
                    [p.worker_task],
                    ["kms:Encrypt", "kms:GenerateDataKey"],
                    s3,
                ),
                # Lectura solo por el rol de restauración (runbooks).
                self._for_roles("RestoreReads", [p.restore], ["kms:Decrypt"], s3),
            ]
        if name is KeyName.LOGS:
            return [self._logs_service()]
        # vigia-node-ca
        sign = {"StringEquals": {"kms:SigningAlgorithm": SIGNING_ALGORITHM}}
        statements = [
            self._for_roles("ApiSigns", [p.api_task], ["kms:Sign"], sign),
            self._for_roles("ApiReadsPublicKey", [p.api_task], ["kms:GetPublicKey"]),
            self._for_roles("WorkerSignsRevocationList", [p.worker_task], ["kms:Sign"], sign),
        ]
        if self.config.elevated_bootstrap:
            # Nota U02-H-01 (1) y deployment-architecture §6.4: solo con ``first_deploy=true`` o
            # ``ca_rotation=true``, para firmar la raíz.
            statements += [
                self._for_roles("BootstrapAdminSigns", [p.admin_task], ["kms:Sign"], sign),
                self._for_roles(
                    "BootstrapAdminReadsPublicKey", [p.admin_task], ["kms:GetPublicKey"]
                ),
            ]
        return statements

    def _grant_for_aws_resource_via(self, sid: str, service: str) -> iam.PolicyStatement:
        return iam.PolicyStatement(
            sid=sid,
            principals=[iam.AccountRootPrincipal()],
            actions=["kms:CreateGrant"],
            resources=["*"],
            conditions={
                "StringEquals": {
                    "kms:ViaService": f"{service}.{self.region}.amazonaws.com",
                    "kms:CallerAccount": self.account,
                },
                "Bool": {"kms:GrantIsForAWSResource": "true"},
            },
        )

    def _db_secrets_rotation(self) -> iam.PolicyStatement:
        """Secretos de la base (§7.2) por Secrets Manager: la función de rotación de un solo
        usuario, cuyo rol genera el servicio al desplegar ``vigia-data``, y el secreto maestro que
        gestiona RDS (``rds!db-...``). Solo esos secretos y solo por el servicio."""
        secrets = [
            self.format_arn(
                service="secretsmanager",
                resource="secret",
                resource_name=name,
                arn_format=ArnFormat.COLON_RESOURCE_NAME,
            )
            for name in (f"vigia/{self.config.deployment}/db/*", "rds!db-*")
        ]
        return iam.PolicyStatement(
            sid="DatabaseSecretsThroughSecretsManager",
            principals=[iam.AccountRootPrincipal()],
            actions=["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
            resources=["*"],
            conditions={
                "StringEquals": {
                    "kms:ViaService": f"secretsmanager.{self.region}.amazonaws.com",
                    "kms:CallerAccount": self.account,
                },
                "StringLike": {"kms:EncryptionContext:SecretARN": secrets},
            },
        )

    def _logs_service(self) -> iam.PolicyStatement:
        """El servicio de registros cifra los grupos ``/vigia/<despliegue>/*`` de la cuenta, el de
        los registros de PostgreSQL de la base del despliegue y, con cortafuegos, el de
        ``vigia-app-waf`` (§9.2)."""
        names = [
            f"/vigia/{self.config.deployment}/*",
            f"/aws/rds/instance/vigia-{self.config.deployment}-db/*",
        ]
        if self.config.waf_enabled:
            names.append(waf_log_group_name(self.config))
        log_groups = [
            self.format_arn(
                service="logs",
                resource="log-group",
                resource_name=name,
                arn_format=ArnFormat.COLON_RESOURCE_NAME,
            )
            for name in names
        ]
        return iam.PolicyStatement(
            sid="LogsForVigiaLogGroups",
            principals=[iam.ServicePrincipal(f"logs.{self.region}.amazonaws.com")],
            actions=list(LOGS_SERVICE_ACTIONS),
            resources=["*"],
            conditions={"ArnLike": {"kms:EncryptionContext:aws:logs:arn": log_groups}},
        )

    def _edge_ca_arn(self) -> str:
        bucket = self.config.bucket_name("edge", self.account)
        return f"arn:{self.partition}:s3:::{bucket}/ca/*"

    def _keys(self) -> dict[KeyName, kms.Key]:
        config = self.config
        removal = RemovalPolicy.DESTROY if config.ephemeral else RemovalPolicy.RETAIN
        keys: dict[KeyName, kms.Key] = {}
        for name in KeyName:
            policy = iam.PolicyDocument(statements=[self._admin(name), *self._usage(name)])
            symmetric = name in SYMMETRIC_KEYS
            keys[name] = kms.Key(
                self,
                f"Key-{name.value}",
                alias=f"alias/{config.resource_name(name.value)}",
                description=f"vigia-{name.value}: {_DESCRIPTIONS[name]}",
                policy=policy,
                key_spec=kms.KeySpec.SYMMETRIC_DEFAULT if symmetric else kms.KeySpec.ECC_NIST_P256,
                key_usage=(kms.KeyUsage.ENCRYPT_DECRYPT if symmetric else kms.KeyUsage.SIGN_VERIFY),
                enable_key_rotation=symmetric,
                rotation_period=ROTATION_PERIOD if symmetric else None,
                removal_policy=removal,
                pending_window=Duration.days(config.node_ca_pending_window_days),
            )
        return keys

    # --- Red ------------------------------------------------------------------------------

    def _vpc(self) -> ec2.Vpc:
        config = self.config
        vpc = ec2.Vpc(
            self,
            "Vpc",
            vpc_name=f"vigia-vpc-{config.deployment}",
            ip_addresses=ec2.IpAddresses.cidr(VPC_CIDR),
            availability_zones=list(ZONES),
            enable_dns_hostnames=True,
            enable_dns_support=True,
            nat_gateways=2 if config.nat_per_az else 1,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name=PUBLIC_SUBNETS, subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=PUBLIC_MASK
                ),
                ec2.SubnetConfiguration(
                    name=APP_SUBNETS,
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=APP_MASK,
                ),
                ec2.SubnetConfiguration(
                    name=DATA_SUBNETS,
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=DATA_MASK,
                ),
            ],
            restrict_default_security_group=True,
        )
        for group in (PUBLIC_SUBNETS, APP_SUBNETS, DATA_SUBNETS):
            subnets = vpc.select_subnets(subnet_group_name=group).subnets
            for letter, subnet in zip(ZONE_LETTERS, subnets, strict=True):
                Tags.of(subnet).add("Name", config.resource_name(f"{group}-{letter}"))
                nat = subnet.node.try_find_child("NATGateway")
                if nat is not None:
                    Tags.of(nat).add("Name", config.resource_name(f"nat-{letter}"), priority=200)
        return vpc

    def _flow_logs(self) -> logs.LogGroup:
        config = self.config
        removal = RemovalPolicy.DESTROY if config.ephemeral else RemovalPolicy.RETAIN
        group = logs.LogGroup(
            self,
            "VpcFlowLogs",
            log_group_name=f"/vigia/{config.deployment}/vpc-flow",
            retention=FLOW_LOG_RETENTION,
            encryption_key=self.keys[KeyName.LOGS],
            removal_policy=removal,
        )
        role = iam.Role(
            self,
            "FlowLogsRole",
            role_name=config.resource_name("flow-logs"),
            assumed_by=iam.ServicePrincipal(
                "vpc-flow-logs.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
            description="vigia-flow-logs: escribe los registros de flujo (8)",
        )
        flow_log = self.vpc.add_flow_log(
            "FlowLog",
            destination=ec2.FlowLogDestination.to_cloud_watch_logs(group, role),
            traffic_type=ec2.FlowLogTrafficType.ALL,
        )
        Tags.of(flow_log).add("Name", config.resource_name("vpc-flow"), priority=200)
        return group

    def _security_groups(self) -> dict[SecurityGroupName, ec2.SecurityGroup]:
        groups = {
            name: ec2.SecurityGroup(
                self,
                f"SecurityGroup-{name.value}",
                vpc=self.vpc,
                security_group_name=name.value,
                description=f"{name.value} (infrastructure-design 3)",
                allow_all_outbound=False,
            )
            for name in SecurityGroupName
        }
        db = groups[SecurityGroupName.DB]
        endpoints = groups[SecurityGroupName.ENDPOINTS]
        for name in TASK_GROUPS:
            group = groups[name]
            db.add_ingress_rule(group, ec2.Port.tcp(POSTGRES_PORT), f"5432 desde {name.value}")
            endpoints.add_ingress_rule(group, ec2.Port.tcp(HTTPS_PORT), f"443 desde {name.value}")
            group.add_egress_rule(db, ec2.Port.tcp(POSTGRES_PORT), "5432 hacia sg-db")
            group.add_egress_rule(endpoints, ec2.Port.tcp(HTTPS_PORT), "443 hacia sg-endpoints")
            # Servicio de filtradas, registro de imágenes y X-Ray por la traducción (§3).
            group.add_egress_rule(
                ec2.Peer.ipv4(ANYWHERE_IPV4),
                ec2.Port.tcp(HTTPS_PORT),
                "443 por la traduccion de direcciones",
            )
        return groups

    def _s3_endpoint(self) -> ec2.GatewayVpcEndpoint:
        endpoint = self.vpc.add_gateway_endpoint(
            "S3Endpoint",
            service=ec2.GatewayVpcEndpointAwsService.S3,
            subnets=[
                ec2.SubnetSelection(subnet_group_name=APP_SUBNETS),
                ec2.SubnetSelection(subnet_group_name=DATA_SUBNETS),
            ],
        )
        endpoint.add_to_policy(
            iam.PolicyStatement(
                sid="VigiaBucketsOfTheAccount",
                principals=[iam.AnyPrincipal()],
                actions=list(S3_ENDPOINT_ACTIONS),
                resources=[
                    f"arn:{self.partition}:s3:::vigia-*",
                    f"arn:{self.partition}:s3:::vigia-*/*",
                ],
                conditions={"StringEquals": {"s3:ResourceAccount": self.account}},
            )
        )
        endpoint.add_to_policy(
            iam.PolicyStatement(
                sid="EcrImageLayers",
                principals=[iam.AnyPrincipal()],
                actions=["s3:GetObject"],
                resources=[f"arn:{self.partition}:s3:::{ECR_LAYER_BUCKET}/*"],
            )
        )
        return endpoint

    def _interface_endpoints(self) -> dict[str, ec2.InterfaceVpcEndpoint]:
        endpoints_group = self.security_groups[SecurityGroupName.ENDPOINTS]
        return {
            name: ec2.InterfaceVpcEndpoint(
                self,
                f"{name}Endpoint",
                vpc=self.vpc,
                service=service,
                subnets=ec2.SubnetSelection(subnet_group_name=APP_SUBNETS),
                security_groups=[endpoints_group],
                private_dns_enabled=True,
                # Sin la regla 443 desde toda la VPC que CDK añade por defecto: solo los
                # grupos de las tareas (§3).
                open=False,
            )
            for name, service in INTERFACE_ENDPOINTS.items()
        }

    # --- Alertas y presupuestos -----------------------------------------------------------

    def _alerts_topic(self) -> sns.Topic:
        topic = sns.Topic(
            self,
            "AlertsTopic",
            topic_name=self.config.resource_name("alerts"),
            display_name="Vigia alertas",
        )
        sns.Subscription(
            self,
            "AlertsOwnerEmail",
            topic=topic,
            protocol=sns.SubscriptionProtocol.EMAIL,
            endpoint=ssm.StringParameter.value_for_string_parameter(self, ALERTS_EMAIL_PARAMETER),
        )
        # Suscripción de eventos de la base de ``vigia-data`` (§6.1): con una política propia en
        # el tema, RDS necesita su permiso explícito.
        topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="RdsEventsPublish",
                principals=[iam.ServicePrincipal("events.rds.amazonaws.com")],
                actions=["sns:Publish"],
                resources=[topic.topic_arn],
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            )
        )
        # Alarmas de CloudWatch de la cuenta: aviso de bloqueos por tasa de ``vigia-edge`` (nº 11)
        # y las de ``vigia-observability`` (§9.4).
        topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="CloudWatchAlarmsPublish",
                principals=[iam.ServicePrincipal("cloudwatch.amazonaws.com")],
                actions=["sns:Publish"],
                resources=[topic.topic_arn],
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            )
        )
        return topic

    def _budgets(self) -> list[budgets.CfnBudget]:
        topic = self.alerts_topic
        topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="BudgetsPublish",
                principals=[iam.ServicePrincipal("budgets.amazonaws.com")],
                actions=["sns:Publish"],
                resources=[topic.topic_arn],
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            )
        )
        project = budgets.CfnBudget.ExpressionProperty(
            tags=budgets.CfnBudget.TagValuesProperty(
                key="project", values=["vigia"], match_options=["EQUALS"]
            )
        )
        # ``environment=staging-*``: todo recurso de la aplicación lleva ``environment`` y sus
        # únicos valores son ``pilot`` y ``staging-<n>``, así que se filtra por lo que no es
        # ``pilot`` (las etiquetas de costos no admiten prefijos).
        not_pilot = budgets.CfnBudget.ExpressionProperty(
            not_=budgets.CfnBudget.ExpressionProperty(
                tags=budgets.CfnBudget.TagValuesProperty(
                    key="environment", values=[PILOT], match_options=["EQUALS"]
                )
            )
        )
        return [
            self._budget(
                "MonthlyBudget",
                MONTHLY_BUDGET,
                MONTHLY_BUDGET_USD,
                MONTHLY_ALERT_PERCENTAGES,
                project,
            ),
            self._budget(
                "StagingBudget",
                STAGING_BUDGET,
                STAGING_BUDGET_USD,
                STAGING_ALERT_PERCENTAGES,
                budgets.CfnBudget.ExpressionProperty(and_=[project, not_pilot]),
            ),
        ]

    def _budget(
        self,
        construct_id: str,
        name: str,
        amount: int,
        percentages: Sequence[int],
        expression: budgets.CfnBudget.ExpressionProperty,
    ) -> budgets.CfnBudget:
        subscriber = budgets.CfnBudget.SubscriberProperty(
            subscription_type="SNS", address=self.alerts_topic.topic_arn
        )
        return budgets.CfnBudget(
            self,
            construct_id,
            budget=budgets.CfnBudget.BudgetDataProperty(
                # ``vigia-monthly`` y ``vigia-staging`` en ``pilot`` compartido; una instancia
                # dedicada en la misma cuenta lleva su sufijo.
                budget_name=self.config.resource_name(name),
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(amount=amount, unit="USD"),
                filter_expression=expression,
            ),
            notifications_with_subscribers=[
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        comparison_operator="GREATER_THAN",
                        notification_type="ACTUAL",
                        threshold=percentage,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=[subscriber],
                )
                for percentage in percentages
            ],
        )


__all__ = [
    "ALERTS_EMAIL_PARAMETER",
    "FoundationStack",
    "KeyName",
    "SecurityGroupName",
    "budgets_enabled",
    "deploy_role_name",
    "waf_log_group_name",
]
