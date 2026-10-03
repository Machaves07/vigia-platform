"""Pila ``vigia-data``: base gestionada y su grupo de parámetros, secretos de base, depósitos,
bóveda y plan de copias (§6, §11). Depende de ``vigia-foundation``. Recursos: TASK-146.

Base ``vigia-<despliegue>-db`` (§6.1 y tabla D-8 de §2.1):

- RDS para PostgreSQL 16 en las subredes de datos, ``sg-db``, sin dirección pública; ``gp3`` de
  100 GB con crecimiento hasta 1 TB, cifrado con ``vigia-db``; grupo de parámetros
  ``vigia-pg16`` (``rds.force_ssl=1``, ``pg_stat_statements``, tiempos de espera); monitoreo
  mejorado cada 60 s; registros de PostgreSQL a ``/aws/rds/instance/<base>/postgresql`` (90 días,
  ``vigia-logs``); suscripción de eventos de conmutación, fallo y mantenimiento a
  ``vigia-alerts``; etiqueta ``backup=monthly`` para el plan de copias.
- ``pilot``: ``db.t4g.medium`` en dos zonas, eliminación protegida, copias de 35 días,
  ``DeletionPolicy: Retain`` y ``UpdateReplacePolicy: Snapshot`` (instantánea final si
  CloudFormation la sustituye). ``staging-<n>``: ``db.t4g.small``, sin protección, copias de 1 día,
  sin instantánea final y sin copias automáticas residuales.
- Usuario maestro ``vigia_owner`` con contraseña gestionada por RDS, cifrada con ``vigia-secrets``.
  RDS elige el nombre del secreto (``rds!db-...``): el nombre ``vigia/<entorno>/db/master`` de
  §7.2 es la hipótesis (b) de R14 (nota U02-H-16 de §13).

Secretos ``vigia/<despliegue>/db/app`` y ``db/migrate`` (§7.2): cifrados con ``vigia-secrets`` y
rotados cada 30 días por la función de un solo usuario para PostgreSQL que provee el servicio,
dentro de la VPC (subredes de aplicación, ``sg-tasks``, que ya llega a ``sg-db`` y a los puntos
privados). La primera rotación espera al calendario: los roles ``vigia_app`` y ``vigia_migrate`` los
crea la migración ``0001`` después de este despliegue (§5.4).

Depósitos (§6.2, notas de 2026-09-23 de §2.1 y §6.2, A-22 y A-33): todos versionados salvo el de
registros, con acceso público bloqueado, propiedad forzada al dueño, solo TLS y registro de
acceso en ``vigia-logs`` bajo ``s3/<uso>/``:

- ``vigia-evidence``: bloqueo en gobernanza de 365 días, SSE-KMS con ``vigia-evidence``, rechazo
  de ``PutObject`` sin ``x-amz-checksum-sha256`` (clave de condición: hipótesis (a) de R14),
  acceso infrecuente a los 90 días sin expiración, subidas por partes incompletas abortadas a
  los 7 días (nº 22) y **una** regla de origen cruzado con **un** origen, el del entorno:
  ``https://app.<dominio>`` en ``pilot``, ``https://staging-<n>.<dominio>`` en ``staging-<n>``.
  Los prefijos ``node/``, ``documents/`` (nº 14) y ``closure/`` los usan las URL prefirmadas; sus
  permisos son de ``vigia-compute`` (TASK-148).
- ``vigia-archive``: bloqueo en cumplimiento de 10 años, ``vigia-archive``, archivo profundo a los
  180 días.
- ``vigia-edge``: ``ca/root.pem`` y ``ca/crl.pem``, cifrado con ``vigia-secrets``.
- ``vigia-drill``: depósito del ensayo de restauración (deployment-architecture §6.1), con la clave
  ``vigia-evidence``, sin bloqueo y ``DESTROY`` también en ``pilot``.

En ``staging-<n>`` (D-8) ningún depósito lleva bloqueo, todos son ``DESTROY`` con vaciado
automático, y el registro de acceso va a ``vigia-logs-staging-<n>-<cuenta>-us-east-1``, que crea
esta pila porque ``vigia-datasets`` no existe en ``staging``. En ``pilot`` compartido el destino es
el ``vigia-logs`` heredado de ``vigia-datasets`` (TASK-150), que concede la entrega.

El dominio (P5) no se fija en el repositorio: el dueño lo registra en el parámetro SSM
:data:`DOMAIN_PARAMETER` antes del primer despliegue, igual que el correo de alertas.

Bóveda ``vigia-backup-vault`` (``vigia-backup``) y plan ``vigia-monthly-snapshots``: día 1 de cada
mes a las 02:00 UTC, retención de 365 días, sobre la base de este despliegue con la etiqueta
``backup=monthly``, con el rol de servicio ``vigia-backup`` (§8).
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from aws_cdk import (
    Annotations,
    ArnFormat,
    CfnDeletionPolicy,
    CfnTag,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
    Tags,
)
from aws_cdk import aws_backup as backup
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_events as events
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from config import EnvironmentConfig, ObjectLock, ObjectLockMode
from stacks.base import VigiaStack
from stacks.foundation import (
    APP_SUBNETS,
    DATA_SUBNETS,
    POSTGRES_PORT,
    FoundationStack,
    KeyName,
    SecurityGroupName,
)

# --- Base de datos (§6.1) -----------------------------------------------------------------

DB_ENGINE = "postgres"
DB_ENGINE_VERSION = "16"  # última versión menor; actualizaciones menores automáticas
DB_PARAMETER_FAMILY = "postgres16"
DB_NAME = "vigia"
DB_MASTER_USER = "vigia_owner"
DB_STORAGE_TYPE = "gp3"
DB_ALLOCATED_STORAGE_GB = 100  # [objetivo propio]
DB_MAX_ALLOCATED_STORAGE_GB = 1000
DB_CA_CERTIFICATE = "rds-ca-rsa2048-g1"
# Copias 05:00 a 06:00 UTC y mantenimiento el domingo 06:00 a 07:00 UTC; RDS rechaza ventanas
# que se tocan, así que la de copias cierra un minuto antes.
DB_BACKUP_WINDOW = "05:00-05:59"
DB_MAINTENANCE_WINDOW = "sun:06:00-sun:07:00"
DB_MONITORING_INTERVAL_SECONDS = 60
DB_LOG_EXPORTS = ("postgresql",)
DB_LOG_RETENTION = logs.RetentionDays.THREE_MONTHS  # 90 días
DB_PARAMETERS: Mapping[str, str] = {
    "rds.force_ssl": "1",
    "shared_preload_libraries": "pg_stat_statements",
    "log_min_duration_statement": "1000",
    "idle_in_transaction_session_timeout": "60000",
    "log_connections": "1",
}
DB_EVENT_CATEGORIES = ("failover", "failure", "maintenance")
BACKUP_TAG = ("backup", "monthly")


class DbUser(StrEnum):
    """Usuarios de la aplicación con secreto propio (§7.2); el valor es el nombre del secreto."""

    APP = "app"
    MIGRATE = "migrate"


DB_ROLES: Mapping[DbUser, str] = {DbUser.APP: "vigia_app", DbUser.MIGRATE: "vigia_migrate"}
SECRET_ROTATION = Duration.days(30)  # [objetivo propio]
SECRET_PASSWORD_LENGTH = 32
# Sin caracteres que rompan una URL de conexión o una cadena de la shell.
CONNECTION_UNSAFE_CHARACTERS = " %+~`#$&*()|[]{}:;<>?!'/@\"\\"

# --- Depósitos (§6.2) ---------------------------------------------------------------------


class BucketUsage(StrEnum):
    """Depósitos de la pila; el valor es el ``<uso>`` del nombre y del prefijo de registros."""

    EVIDENCE = "evidence"
    ARCHIVE = "archive"
    EDGE = "edge"
    DRILL = "drill"


LOGS_USAGE = "logs"
# Etiqueta ``data=`` por clase de dato (infrastructure-design, «Cómo leer este documento»).
DATA_TAGS: Mapping[BucketUsage, str] = {
    BucketUsage.EVIDENCE: "evidence",
    BucketUsage.ARCHIVE: "audit-archive",
    BucketUsage.EDGE: "public-keys",
    BucketUsage.DRILL: "drill",
}
BUCKET_KEYS: Mapping[BucketUsage, KeyName] = {
    BucketUsage.EVIDENCE: KeyName.EVIDENCE,
    BucketUsage.ARCHIVE: KeyName.ARCHIVE,
    BucketUsage.EDGE: KeyName.SECRETS,
    # Nota de 2026-09-23 de deployment-architecture §6.1: la misma clave que vigia-evidence.
    BucketUsage.DRILL: KeyName.EVIDENCE,
}
EVIDENCE_INFREQUENT_ACCESS_AFTER = Duration.days(90)  # [hipótesis pendiente de R5]
ARCHIVE_DEEP_ARCHIVE_AFTER = Duration.days(180)  # [objetivo propio]
ABORT_INCOMPLETE_UPLOADS_AFTER = Duration.days(7)  # nº 22, [objetivo propio]
ACCESS_LOGS_EXPIRATION = Duration.days(365)  # ciclo de vida de vigia-logs (U-01)
# Filtro de las métricas de peticiones de ``vigia-evidence`` (dimensión ``FilterId`` en AWS/S3).
EVIDENCE_REQUEST_METRICS = "EntireBucket"
ACCESS_LOGS_POLICY_WARNING = "@aws-cdk/aws-s3:accessLogsPolicyNotAdded"
# Prefijos de los registros de acceso de los balanceadores de ``vigia-edge`` (§4.2 y §4.3).
LOAD_BALANCER_LOG_PREFIXES = ("alb/app", "alb/nodes")
# Cuenta del servicio de balanceo que entrega los registros en us-east-1 (documentación de
# Elastic Load Balancing); el de red (contingencia de R2) entrega por ``delivery.logs``.
ELB_LOG_DELIVERY_ACCOUNT = "127311923021"

# Cabecera de suma que exige la política de vigia-evidence (§6.2; hipótesis (a) de R14).
CHECKSUM_CONDITION_KEY = "s3:x-amz-checksum-sha256"
# Política de origen cruzado (A-22 y A-33; shared-infrastructure §5.2).
CORS_METHODS = (s3.HttpMethods.PUT, s3.HttpMethods.GET, s3.HttpMethods.HEAD)
CORS_ALLOWED_HEADERS = ("Content-Type", "x-amz-checksum-sha256", "x-amz-meta-*")
CORS_EXPOSED_HEADERS = ("ETag",)
CORS_MAX_AGE_SECONDS = 3600
# Dominio del producto (P5), registrado por el dueño antes del primer despliegue.
DOMAIN_PARAMETER = "/vigia/domain"
PILOT_APP_HOST = "app"

# --- Copias (§11) -------------------------------------------------------------------------

MONTHLY_SNAPSHOTS_SCHEDULE = events.Schedule.cron(minute="0", hour="2", day="1")
MONTHLY_SNAPSHOTS_RETENTION = Duration.days(365)
BACKUP_SERVICE_POLICY = "service-role/AWSBackupServiceRolePolicyForBackup"
RDS_MONITORING_POLICY = "service-role/AmazonRDSEnhancedMonitoringRole"


def db_identifier(config: EnvironmentConfig) -> str:
    """``vigia-pilot-db`` (§6.1); ``vigia-<despliegue>-db`` en los demás."""
    return f"vigia-{config.deployment}-db"


def db_log_group_name(config: EnvironmentConfig) -> str:
    """Grupo al que RDS exporta los registros de PostgreSQL (§9.2)."""
    return f"/aws/rds/instance/{db_identifier(config)}/postgresql"


def db_secret_name(config: EnvironmentConfig, user: DbUser) -> str:
    """``vigia/<despliegue>/db/<usuario>`` (§7.2)."""
    return f"vigia/{config.deployment}/db/{user.value}"


def app_host(config: EnvironmentConfig) -> str:
    """Nombre del origen de la aplicación: ``app`` en ``pilot``, ``staging-<n>`` en staging."""
    return config.environment if config.ephemeral else PILOT_APP_HOST


def lock_retention(lock: ObjectLock) -> s3.ObjectLockRetention:
    days = Duration.days(lock.days)
    if lock.mode is ObjectLockMode.COMPLIANCE:
        return s3.ObjectLockRetention.compliance(days)
    return s3.ObjectLockRetention.governance(days)


def accept_load_balancer_logs(bucket: s3.Bucket) -> None:
    """Entrega de los registros de ``vigia-alb-app`` y ``vigia-alb-nodes`` (o del balanceador
    de red de la contingencia) en sus prefijos de ``vigia-logs``: el propio de esta pila o el
    heredado de ``vigia-datasets``. ``vigia-edge`` importa el depósito por nombre y no toca esta
    política, así que no cambia con ``nodes_tls_mode``."""
    stack = Stack.of(bucket)
    objects = [
        bucket.arn_for_objects(f"{prefix}/AWSLogs/{stack.account}/*")
        for prefix in LOAD_BALANCER_LOG_PREFIXES
    ]
    source_account = {"StringEquals": {"aws:SourceAccount": stack.account}}
    bucket.add_to_resource_policy(
        iam.PolicyStatement(
            sid="LoadBalancerLogDelivery",
            principals=[
                iam.ArnPrincipal(f"arn:{stack.partition}:iam::{ELB_LOG_DELIVERY_ACCOUNT}:root")
            ],
            actions=["s3:PutObject"],
            resources=objects,
        )
    )
    bucket.add_to_resource_policy(
        iam.PolicyStatement(
            sid="NetworkLoadBalancerLogDelivery",
            principals=[iam.ServicePrincipal("delivery.logs.amazonaws.com")],
            actions=["s3:PutObject"],
            resources=objects,
            conditions={
                "StringEquals": {
                    "s3:x-amz-acl": "bucket-owner-full-control",
                    "aws:SourceAccount": stack.account,
                }
            },
        )
    )
    bucket.add_to_resource_policy(
        iam.PolicyStatement(
            sid="NetworkLoadBalancerLogAclCheck",
            principals=[iam.ServicePrincipal("delivery.logs.amazonaws.com")],
            actions=["s3:GetBucketAcl"],
            resources=[bucket.bucket_arn],
            conditions=source_account,
        )
    )


class DataStack(VigiaStack):
    """Pila ``vigia-data`` (infrastructure-design §2.3)."""

    key = "data"
    summary = "base gestionada, depositos, boveda y plan de copias"

    def __init__(
        self, scope: Construct, config: EnvironmentConfig, *, tags: Mapping[str, str]
    ) -> None:
        super().__init__(scope, config, tags=tags)
        foundation = scope.node.find_child(config.stack_name(FoundationStack.key))
        if not isinstance(foundation, FoundationStack):
            raise TypeError("vigia-data necesita vigia-foundation registrada antes")
        self.foundation = foundation
        self._keys: dict[KeyName, kms.IKey] = {}
        self.removal = RemovalPolicy.RETAIN if config.buckets_retained else RemovalPolicy.DESTROY
        self.access_logs_bucket = self._access_logs_bucket()
        self.buckets: dict[BucketUsage, s3.Bucket] = {
            usage: self._bucket(usage) for usage in BucketUsage
        }
        self._require_checksum(self.buckets[BucketUsage.EVIDENCE])
        self.database = self._database()
        self.db_secrets = {user: self._db_secret(user) for user in DbUser}
        self.backup_vault, self.backup_plan = self._monthly_snapshots()

    def _key(self, name: KeyName) -> kms.IKey:
        """Clave de ``vigia-foundation`` importada por su ARN: sus políticas son explícitas
        (§7.1) y ningún constructo de esta pila les añade sentencias."""
        if name not in self._keys:
            self._keys[name] = kms.Key.from_key_arn(
                self, f"Key-{name.value}", self.foundation.keys[name].key_arn
            )
        return self._keys[name]

    # --- Depósitos ------------------------------------------------------------------------

    def _access_logs_bucket(self) -> s3.IBucket:
        """``vigia-logs`` heredado en ``pilot`` compartido; uno propio en los demás."""
        config = self.config
        name = config.bucket_name(LOGS_USAGE, self.account)
        if not config.access_logs_bucket_owned:
            return s3.Bucket.from_bucket_name(self, "AccessLogsBucket", name)
        # Mismo cifrado que el vigia-logs heredado: SSE-S3, excepción declarada (§2.3).
        bucket = s3.Bucket(
            self,
            "AccessLogsBucket",
            bucket_name=name,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            enforce_ssl=True,
            lifecycle_rules=[
                s3.LifecycleRule(id="ExpireAfter365Days", expiration=ACCESS_LOGS_EXPIRATION)
            ],
            removal_policy=self.removal,
            auto_delete_objects=config.buckets_auto_delete_objects,
        )
        accept_load_balancer_logs(bucket)
        return bucket

    def _bucket(self, usage: BucketUsage) -> s3.Bucket:
        config = self.config
        lock = {
            BucketUsage.EVIDENCE: config.evidence_object_lock,
            BucketUsage.ARCHIVE: config.archive_object_lock,
        }.get(usage)
        drill = usage is BucketUsage.DRILL
        bucket = s3.Bucket(
            self,
            f"Bucket-{usage.value}",
            bucket_name=config.bucket_name(usage.value, self.account),
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self._key(BUCKET_KEYS[usage]),
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            enforce_ssl=True,
            versioned=True,
            object_lock_enabled=True if lock else None,
            object_lock_default_retention=lock_retention(lock) if lock else None,
            server_access_logs_bucket=self.access_logs_bucket,
            server_access_logs_prefix=f"s3/{usage.value}/",
            lifecycle_rules=self._lifecycle(usage),
            cors=self._cors() if usage is BucketUsage.EVIDENCE else None,
            # Métricas de peticiones de todo el depósito para la alarma ``quota-evidence-reads``
            # de ``vigia-observability`` (U-03 §8.4, nº 18).
            metrics=(
                [s3.BucketMetrics(id=EVIDENCE_REQUEST_METRICS)]
                if usage is BucketUsage.EVIDENCE
                else None
            ),
            # El depósito de ensayo se vacía en el paso 6 del runbook y se destruye con la pila.
            removal_policy=RemovalPolicy.DESTROY if drill else self.removal,
            auto_delete_objects=config.buckets_auto_delete_objects,
        )
        Tags.of(bucket).add("data", DATA_TAGS[usage])
        if not config.access_logs_bucket_owned:
            # La política de entrega del vigia-logs heredado es de vigia-datasets (TASK-150).
            Annotations.of(bucket).acknowledge_warning(
                ACCESS_LOGS_POLICY_WARNING,
                "vigia-logs es de vigia-datasets: su politica concede la entrega (TASK-150)",
            )
        return bucket

    def _lifecycle(self, usage: BucketUsage) -> list[s3.LifecycleRule] | None:
        if usage is BucketUsage.EVIDENCE:
            # Sin expiración: el registro no se borra (P4).
            return [
                s3.LifecycleRule(
                    id="InfrequentAccessAfter90Days",
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.INFREQUENT_ACCESS,
                            transition_after=EVIDENCE_INFREQUENT_ACCESS_AFTER,
                        )
                    ],
                ),
                s3.LifecycleRule(
                    id="AbortIncompleteMultipartUploads",
                    abort_incomplete_multipart_upload_after=ABORT_INCOMPLETE_UPLOADS_AFTER,
                ),
            ]
        if usage is BucketUsage.ARCHIVE:
            return [
                s3.LifecycleRule(
                    id="DeepArchiveAfter180Days",
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.DEEP_ARCHIVE,
                            transition_after=ARCHIVE_DEEP_ARCHIVE_AFTER,
                        )
                    ],
                )
            ]
        return None

    def _cors(self) -> list[s3.CorsRule]:
        """Una sola regla con un solo origen, el del entorno (A-33)."""
        domain = ssm.StringParameter.value_for_string_parameter(self, DOMAIN_PARAMETER)
        origin = Fn.join("", ["https://", app_host(self.config), ".", domain])
        return [
            s3.CorsRule(
                allowed_methods=list(CORS_METHODS),
                allowed_origins=[origin],
                allowed_headers=list(CORS_ALLOWED_HEADERS),
                exposed_headers=list(CORS_EXPOSED_HEADERS),
                max_age=CORS_MAX_AGE_SECONDS,
            )
        ]

    def _require_checksum(self, bucket: s3.Bucket) -> None:
        """§6.2: toda subida lleva la cabecera de suma SHA-256 firmada."""
        bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyUploadsWithoutChecksum",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["s3:PutObject"],
                resources=[bucket.arn_for_objects("*")],
                conditions={"Null": {CHECKSUM_CONDITION_KEY: "true"}},
            )
        )

    # --- Base de datos --------------------------------------------------------------------

    def _database(self) -> rds.CfnDBInstance:
        config = self.config
        identifier = db_identifier(config)
        vpc = self.foundation.vpc
        subnet_group = rds.CfnDBSubnetGroup(
            self,
            "DbSubnetGroup",
            db_subnet_group_name=f"{identifier}-subnets",
            db_subnet_group_description="Subredes de datos aisladas (infrastructure-design 3)",
            subnet_ids=vpc.select_subnets(subnet_group_name=DATA_SUBNETS).subnet_ids,
        )
        parameter_group = rds.CfnDBParameterGroup(
            self,
            "DbParameterGroup",
            db_parameter_group_name=config.resource_name("pg16"),
            family=DB_PARAMETER_FAMILY,
            description="vigia-pg16: TLS obligatorio, pg_stat_statements y tiempos (6.1)",
            parameters=dict(DB_PARAMETERS),
        )
        log_group = logs.LogGroup(
            self,
            "DbLogGroup",
            log_group_name=db_log_group_name(config),
            retention=DB_LOG_RETENTION,
            encryption_key=self._key(KeyName.LOGS),
            removal_policy=self.removal,
        )
        monitoring_role = iam.Role(
            self,
            "DbMonitoringRole",
            role_name=config.resource_name("rds-monitoring"),
            assumed_by=iam.ServicePrincipal("monitoring.rds.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(RDS_MONITORING_POLICY)
            ],
            description="vigia-rds-monitoring: monitoreo mejorado de la base (6.1)",
        )
        database = rds.CfnDBInstance(
            self,
            "Database",
            db_instance_identifier=identifier,
            engine=DB_ENGINE,
            engine_version=DB_ENGINE_VERSION,
            auto_minor_version_upgrade=True,
            allow_major_version_upgrade=False,
            db_instance_class=config.db_instance_class,
            multi_az=config.db_multi_az,
            storage_type=DB_STORAGE_TYPE,
            allocated_storage=str(DB_ALLOCATED_STORAGE_GB),
            max_allocated_storage=DB_MAX_ALLOCATED_STORAGE_GB,
            storage_encrypted=True,
            kms_key_id=self._key(KeyName.DB).key_arn,
            db_subnet_group_name=subnet_group.ref,
            vpc_security_groups=[
                self.foundation.security_groups[SecurityGroupName.DB].security_group_id
            ],
            publicly_accessible=False,
            db_parameter_group_name=parameter_group.ref,
            ca_certificate_identifier=DB_CA_CERTIFICATE,
            db_name=DB_NAME,
            master_username=DB_MASTER_USER,
            manage_master_user_password=True,
            master_user_secret=rds.CfnDBInstance.MasterUserSecretProperty(
                kms_key_id=self._key(KeyName.SECRETS).key_arn
            ),
            backup_retention_period=config.db_backup_retention_days,
            preferred_backup_window=DB_BACKUP_WINDOW,
            preferred_maintenance_window=DB_MAINTENANCE_WINDOW,
            copy_tags_to_snapshot=True,
            delete_automated_backups=config.ephemeral,
            deletion_protection=config.db_deletion_protection,
            monitoring_interval=DB_MONITORING_INTERVAL_SECONDS,
            monitoring_role_arn=monitoring_role.role_arn,
            enable_cloudwatch_logs_exports=list(DB_LOG_EXPORTS),
            tags=[CfnTag(key=BACKUP_TAG[0], value=BACKUP_TAG[1])],
        )
        # El grupo existe antes que la base: si no, RDS lo crea sin cifrado ni retención.
        database.node.add_dependency(log_group)
        options = database.cfn_options
        if config.buckets_retained:
            options.deletion_policy = CfnDeletionPolicy.RETAIN
        else:
            options.deletion_policy = CfnDeletionPolicy.DELETE
        options.update_replace_policy = (
            CfnDeletionPolicy.SNAPSHOT if config.db_final_snapshot else CfnDeletionPolicy.DELETE
        )
        rds.CfnEventSubscription(
            self,
            "DbEvents",
            subscription_name=config.resource_name("db-events"),
            sns_topic_arn=self.foundation.alerts_topic.topic_arn,
            source_type="db-instance",
            source_ids=[database.ref],
            event_categories=list(DB_EVENT_CATEGORIES),
            enabled=True,
        )
        return database

    def _db_secret(self, user: DbUser) -> secretsmanager.Secret:
        config = self.config
        database = self.database
        template = Stack.of(self).to_json_string(
            {
                "engine": DB_ENGINE,
                "host": database.attr_endpoint_address,
                "port": POSTGRES_PORT,
                "dbname": DB_NAME,
                "username": DB_ROLES[user],
            }
        )
        secret = secretsmanager.Secret(
            self,
            f"DbSecret-{user.value}",
            secret_name=db_secret_name(config, user),
            description=f"Contrasena de {DB_ROLES[user]} (7.2)",
            encryption_key=self._key(KeyName.SECRETS),
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=template,
                generate_string_key="password",
                password_length=SECRET_PASSWORD_LENGTH,
                exclude_characters=CONNECTION_UNSAFE_CHARACTERS,
            ),
            removal_policy=self.removal,
        )
        secret.add_rotation_schedule(
            "Rotation",
            automatically_after=SECRET_ROTATION,
            # El rol aún no existe en la base: la migración 0001 lo crea (§5.4).
            rotate_immediately_on_update=False,
            hosted_rotation=secretsmanager.HostedRotation.postgre_sql_single_user(
                function_name=config.resource_name(f"db-{user.value}-rotation"),
                vpc=self.foundation.vpc,
                vpc_subnets=ec2.SubnetSelection(subnet_group_name=APP_SUBNETS),
                security_groups=[self.foundation.security_groups[SecurityGroupName.TASKS]],
                exclude_characters=CONNECTION_UNSAFE_CHARACTERS,
            ),
        )
        return secret

    # --- Copias ---------------------------------------------------------------------------

    def _monthly_snapshots(self) -> tuple[backup.BackupVault, backup.BackupPlan]:
        config = self.config
        vault = backup.BackupVault(
            self,
            "BackupVault",
            backup_vault_name=config.resource_name("backup-vault"),
            encryption_key=self._key(KeyName.BACKUP),
            removal_policy=self.removal,
        )
        role = iam.Role(
            self,
            "BackupRole",
            role_name=config.resource_name("backup"),
            assumed_by=iam.ServicePrincipal("backup.amazonaws.com"),
            # Política gestionada del servicio: excepción documentada de §8.
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(BACKUP_SERVICE_POLICY)
            ],
            description="vigia-backup: instantaneas mensuales de la base (8, 11)",
        )
        plan = backup.BackupPlan(
            self,
            "MonthlySnapshots",
            backup_plan_name=config.resource_name("monthly-snapshots"),
            backup_vault=vault,
            backup_plan_rules=[
                backup.BackupPlanRule(
                    rule_name="monthly",
                    schedule_expression=MONTHLY_SNAPSHOTS_SCHEDULE,
                    delete_after=MONTHLY_SNAPSHOTS_RETENTION,
                )
            ],
        )
        # La base de este despliegue, y solo si lleva ``backup=monthly``: una selección solo por
        # etiqueta tomaría también la base de otro despliegue de la cuenta.
        database_arn = self.format_arn(
            service="rds",
            resource="db",
            resource_name=db_identifier(config),
            arn_format=ArnFormat.COLON_RESOURCE_NAME,
        )
        selection = backup.CfnBackupSelection(
            self,
            "MonthlySnapshotsSelection",
            backup_plan_id=plan.backup_plan_id,
            backup_selection=backup.CfnBackupSelection.BackupSelectionResourceTypeProperty(
                selection_name="database",
                iam_role_arn=role.role_arn,
                resources=[database_arn],
                # Propiedad sin tipo en CDK: se escribe con las claves de CloudFormation.
                conditions={
                    "StringEquals": [
                        {
                            "ConditionKey": f"aws:ResourceTag/{BACKUP_TAG[0]}",
                            "ConditionValue": BACKUP_TAG[1],
                        }
                    ]
                },
            ),
        )
        selection.node.add_dependency(self.database)
        return vault, plan


__all__ = [
    "DOMAIN_PARAMETER",
    "LOAD_BALANCER_LOG_PREFIXES",
    "LOGS_USAGE",
    "BucketUsage",
    "DataStack",
    "DbUser",
    "accept_load_balancer_logs",
    "app_host",
    "db_identifier",
    "db_log_group_name",
    "db_secret_name",
]
