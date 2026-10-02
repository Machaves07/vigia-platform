"""Pila ``vigia-datasets``: depósito del conjunto sellado y depósito de registros ``vigia-logs``
(U-01 infrastructure-design §4.2). Única en la cuenta, sin sufijo de entorno y solo en ``pilot``
de la instancia compartida (D-8). Sin dependencias. Traslado: TASK-150.

Trasladada de ``vigia-contracts/infra/stacks/datasets.py`` (deployment-architecture §8,
ADR-002) con el mismo nombre de pila y los mismos identificadores lógicos de los depósitos y
de sus políticas: cambiarlos haría que CloudFormation intentara recrear depósitos con bloqueo
de objetos. ``tests/test_datasets_logical_ids.py`` los compara con la síntesis de
``vigia-contracts`` guardada en ``tests/fixtures/``.

- ``vigia-datasets-<cuenta>-us-east-1``: versionado, SSE-S3 (excepción declarada por nombre
  en ``tests/template_rules.py``), acceso público bloqueado, solo TLS, bloqueo de objetos en
  gobernanza con 365 días por defecto, propiedad forzada al dueño, registro de acceso en
  ``vigia-logs`` bajo ``s3/datasets/``, ``RETAIN`` y sin expiración.
- ``vigia-logs-<cuenta>-us-east-1``: igual salvo el bloqueo de objetos (S3 no entrega registros
  de acceso a un depósito con retención por defecto) y con expiración a los 365 días. Es el
  destino de los registros de acceso de ``pilot`` compartido, así que su política concede,
  además del prefijo ``s3/datasets/``:

  - al servicio de registro de S3, los prefijos ``s3/<uso>/`` de los depósitos de
    ``vigia-data``, cada uno limitado a su depósito por ``aws:SourceArn`` (pendiente de VIG-34);
  - a Elastic Load Balancing, ``alb/app/`` y ``alb/nodes/`` de ``vigia-edge``, también para el
    balanceador de red de la contingencia de R2 (pendiente de VIG-41).

  Añadir sentencias a ``LogsBucketPolicy`` la actualiza sin reemplazos.

Los presupuestos ``vigia-monthly`` y ``vigia-staging`` de la pila de U-01 no se trasladan: son
de ``vigia-foundation`` (TASK-145) con los mismos nombres, y un nombre de presupuesto es único
en la cuenta. El procedimiento del traslado, con ``cdk diff vigia-datasets``, está en
``docs/runbooks/traslado-vigia-datasets.md``.
"""

from __future__ import annotations

from collections.abc import Mapping

from aws_cdk import Duration, RemovalPolicy, Tags
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from constructs import Construct

from config import EnvironmentConfig
from stacks.base import VigiaStack
from stacks.data import LOGS_USAGE, BucketUsage, accept_load_balancer_logs

# Identificadores lógicos fijados por U-01 (deployment-architecture §8).
DATASETS_BUCKET_ID = "DatasetsBucket"
LOGS_BUCKET_ID = "LogsBucket"

DATASETS_USAGE = "datasets"
# [objetivo propio] U-01 infrastructure-design §4.2.
DATASETS_RETENTION_DAYS = 365
LOGS_EXPIRATION_DAYS = 365
DATASETS_ACCESS_LOG_PREFIX = f"s3/{DATASETS_USAGE}/"
# Etiqueta ``data=`` de U-01: el conjunto sellado está anonimizado (BR-CTR-58).
DATA_TAG = ("data", "anonymized")


class DatasetsStack(VigiaStack):
    """Pila ``vigia-datasets`` (infrastructure-design §2.3)."""

    key = "datasets"
    summary = "deposito del conjunto sellado y de registros (U-01)"

    def __init__(
        self, scope: Construct, config: EnvironmentConfig, *, tags: Mapping[str, str]
    ) -> None:
        super().__init__(scope, config, tags=tags)
        # Con la propiedad forzada al dueño no hay ACL: el registro de acceso se concede por
        # política de depósito aunque cdk.json dejara de fijar este indicador.
        self.node.set_context("@aws-cdk/aws-s3:serverAccessLogsUseBucketPolicy", True)

        self.logs_bucket = s3.Bucket(
            self,
            LOGS_BUCKET_ID,
            bucket_name=config.bucket_name(LOGS_USAGE, self.account),
            versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-logs-365d",
                    expiration=Duration.days(LOGS_EXPIRATION_DAYS),
                    noncurrent_version_expiration=Duration.days(LOGS_EXPIRATION_DAYS),
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                )
            ],
            removal_policy=RemovalPolicy.RETAIN,
        )
        _pin_logical_id(self.logs_bucket, LOGS_BUCKET_ID)

        self.datasets_bucket = s3.Bucket(
            self,
            DATASETS_BUCKET_ID,
            bucket_name=config.bucket_name(DATASETS_USAGE, self.account),
            versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            object_lock_enabled=True,
            object_lock_default_retention=s3.ObjectLockRetention.governance(
                Duration.days(DATASETS_RETENTION_DAYS)
            ),
            server_access_logs_bucket=self.logs_bucket,
            server_access_logs_prefix=DATASETS_ACCESS_LOG_PREFIX,
            removal_policy=RemovalPolicy.RETAIN,
        )
        _pin_logical_id(self.datasets_bucket, DATASETS_BUCKET_ID)

        self._accept_platform_access_logs()
        accept_load_balancer_logs(self.logs_bucket)
        for bucket in (self.logs_bucket, self.datasets_bucket):
            Tags.of(bucket).add(*DATA_TAG)

    def _accept_platform_access_logs(self) -> None:
        """Registro de acceso de los depósitos de ``vigia-data`` en ``pilot`` compartido, con la
        misma forma que CDK genera para un destino propio (``vigia-data`` en ``staging``)."""
        for usage in BucketUsage:
            source = self.format_arn(
                service="s3",
                region="",
                account="",
                resource=self.config.bucket_name(usage.value, self.account),
            )
            self.logs_bucket.add_to_resource_policy(
                iam.PolicyStatement(
                    sid=f"S3ServerAccessLogs{usage.value.capitalize()}",
                    principals=[iam.ServicePrincipal("logging.s3.amazonaws.com")],
                    actions=["s3:PutObject"],
                    resources=[self.logs_bucket.arn_for_objects(f"s3/{usage.value}/*")],
                    conditions={
                        "ArnLike": {"aws:SourceArn": source},
                        "StringEquals": {"aws:SourceAccount": self.account},
                    },
                )
            )


def _pin_logical_id(bucket: s3.Bucket, logical_id: str) -> None:
    """Fija el identificador lógico del depósito y de su política (``<id>Policy``),
    independientes de la ruta del constructo."""
    cfn = bucket.node.default_child
    if not isinstance(cfn, s3.CfnBucket):
        raise TypeError(f"{bucket.node.path}: se esperaba un AWS::S3::Bucket")
    cfn.override_logical_id(logical_id)
    policy = bucket.policy.node.default_child if bucket.policy else None
    if not isinstance(policy, s3.CfnBucketPolicy):
        raise TypeError(f"{bucket.node.path}: se esperaba la política de solo TLS")
    policy.override_logical_id(f"{logical_id}Policy")
