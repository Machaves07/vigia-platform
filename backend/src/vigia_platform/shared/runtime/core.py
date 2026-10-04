"""Infraestructura común de los tres constructores (A-52; LC-NUC-19, 25, 28, 29 y 30).

``build_core`` arma lo que ``vigia-api``, ``vigia-worker`` y ``vigia-admin`` comparten, sobre la
base que cada uno abre a su manera:

- clientes de AWS con tiempos de espera explícitos (``AwsSettings``, ``StorageSettings``: 5 s
  por llamada, un solo intento; PAT-NUC-RES-03) para Secrets Manager, KMS y S3;
- ``ScopeContexts`` con el actor del sistema de la plataforma, ``AuditWriter``, ``Outbox`` y el
  ``EscritorExpediente`` con los tipos y los validadores del registro por unidad;
- el ``Authorizer`` de identidad sobre ``PostgresAuthorizationAudit``;
- ``SigningService`` sobre ``SqlSigningKeyStore`` **con** ``LedgerRotationRecorder``: cada
  rotación escribe sus registros y su auditoría ``key_rotated`` en la transacción que la confirma
  (nota de VIG-88, revisión de VIG-93). Sin él, ``vigia-admin`` se niega a rotar y el
  recordatorio de rotación fallaría abierto; aquí nunca falta;
- ``CheckpointService`` y el catálogo de la bandeja (``units.outbox_catalog``) con lo que
  registra cada unidad;
- los dos sincronizadores del arranque: tipos de registro (``ledger.record_type``) y catálogo
  (``shared.event_type``, ``consumer`` y ``periodic_task``), cada uno en una transacción con el
  contexto de auditoría de la proveedora. Los sella al terminar (PAT-NUC-RES-02).

También abre la base de ``vigia_app`` con la credencial de ``VIGIA_DB_APP_SECRET``
(``open_database``): un secreto inexistente o sin la forma de RDS detiene la construcción con
``RuntimeConfigInvalid`` sobre esa variable, sin el contenido del secreto.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from vigia_platform.identity.adapters.authz_store import (
    PostgresAuthorizationAudit,
    PostgresContextStore,
)
from vigia_platform.identity.authz.authorize import Authorizer
from vigia_platform.identity.authz.context import ScopeContexts
from vigia_platform.ledger.adapters.checkpoint_store import SqlCheckpointStore
from vigia_platform.ledger.adapters.record_type_store import SqlRecordTypeStore
from vigia_platform.ledger.application.audit_writer import AuditWriter
from vigia_platform.ledger.application.writer import EscritorExpediente
from vigia_platform.ledger.chain.checkpoints import CheckpointService
from vigia_platform.ledger.evidence import EvidenceVerifier
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.ledger.registry import RecordTypeRegistry
from vigia_platform.shared.adapters.http import DEFAULT_VERIFIER_PATH
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.cpu_pool import CpuPool
from vigia_platform.shared.db import Database, DatabaseSettings, ProcessKind
from vigia_platform.shared.key_rotation import LedgerKeyEventWriter, LedgerRotationRecorder
from vigia_platform.shared.observability.metrics import PlatformMetrics
from vigia_platform.shared.outbox.publish import Outbox
from vigia_platform.shared.outbox.registries import OutboxCatalog
from vigia_platform.shared.outbox.store import SqlOutboxCatalogStore
from vigia_platform.shared.runtime.config import RuntimeConfig, RuntimeConfigInvalid
from vigia_platform.shared.runtime.db_credentials import (
    AwsSecretStringReader,
    DatabaseCredentials,
    DatabaseSecretInvalid,
    SecretStringReader,
    database_url,
)
from vigia_platform.shared.runtime.units import (
    PlatformUnit,
    RuntimeDatabase,
    UnitServices,
    free_text_registry,
    outbox_catalog,
    record_type_registry,
)
from vigia_platform.shared.secrets import (
    AwsSettings,
    KmsAdapter,
    SecretNotFound,
    SecretsManagerAdapter,
)
from vigia_platform.shared.signing.service import SigningService
from vigia_platform.shared.signing_store import SqlSigningKeyStore
from vigia_platform.shared.storage import AddressingStyle, ObjectHead, S3Storage, StorageSettings

__all__ = [
    "DB_SECRET_VARIABLE",
    "SYSTEM_ACTOR_ID",
    "Core",
    "aws_settings",
    "build_core",
    "load_credentials",
    "open_database",
    "s3_storage",
]

SYSTEM_ACTOR_ID: Final = uuid.UUID("00000000-0000-4000-8000-000000000001")
"""El actor ``system`` de los contextos de la plataforma (eventos, tareas, arranque): el mismo en
los tres procesos, para que el rastro de auditoría muestre un solo actor del sistema. No es una
persona (P3) ni una organización: es un identificador fijo de la plataforma."""

DB_SECRET_VARIABLE: Final = "VIGIA_DB_APP_SECRET"  # noqa: S105 - nombre de la variable

type Synchronizer = Callable[[], Awaitable[None]]


def aws_settings(config: RuntimeConfig) -> AwsSettings:
    """Secrets Manager y KMS de la región, con los tiempos de espera de PAT-NUC-RES-03.

    Sin ``VIGIA_AWS_ENDPOINT_URL``, el punto privado de la VPC por DNS y las credenciales del rol
    de la tarea; con él (solo ``local`` y ``test``), LocalStack.
    """
    return AwsSettings(region=config.aws_region, endpoint_url=config.aws_endpoint_url)


def s3_storage(config: RuntimeConfig, bucket: str, clock: Clock) -> S3Storage:
    """El depósito ``bucket`` con el punto regional de S3 (o LocalStack, por ruta)."""
    endpoint = config.aws_endpoint_url
    settings = StorageSettings(
        bucket=bucket,
        endpoint_url=endpoint
        if endpoint is not None
        else f"https://s3.{config.aws_region}.amazonaws.com",
        region=config.aws_region,
        addressing_style=AddressingStyle.VIRTUAL if endpoint is None else AddressingStyle.PATH,
    )
    return S3Storage(settings, clock)


async def load_credentials(
    config: RuntimeConfig, reader: SecretStringReader | None = None
) -> DatabaseCredentials:
    """La credencial de ``vigia_app`` del secreto de ``VIGIA_DB_APP_SECRET``.

    Un secreto inexistente o sin la forma de RDS es un error de configuración que nombra la
    variable; un servicio que no responde sale como ``SecretsUnavailable`` (transitorio).
    """
    source = reader if reader is not None else AwsSecretStringReader(aws_settings(config))
    try:
        return await DatabaseCredentials.load(source, config.db_app_secret)
    except SecretNotFound:
        raise RuntimeConfigInvalid(DB_SECRET_VARIABLE, "nombra un secreto que no existe") from None
    except DatabaseSecretInvalid:
        raise RuntimeConfigInvalid(
            DB_SECRET_VARIABLE,
            "nombra un secreto sin la forma de RDS (host, port, dbname, username, password)",
        ) from None


def open_database(
    config: RuntimeConfig,
    process: ProcessKind,
    credentials: DatabaseCredentials,
    metrics: PlatformMetrics,
    *,
    worker_pool_size: int | None = None,
) -> Database:
    """``shared.db.Database`` de ``vigia_app`` con la credencial rotable (runbook 6.6).

    En ``vigia-api``, los pools ``node`` y ``person`` de ``VIGIA_DB_POOL_NODE`` y
    ``VIGIA_DB_POOL_PERSON`` sin desbordamiento; en ``vigia-worker`` (y ``vigia-admin``), un
    pool; ``statement_timeout`` de ``VIGIA_DB_STATEMENT_TIMEOUT_MS`` o el del proceso.
    """
    secret = credentials.secret
    settings = DatabaseSettings(
        url=database_url(secret),
        process=process,
        sslmode=config.db_sslmode,
        ssl_root_cert=config.db_ssl_root_cert,
        statement_timeout_ms=config.db_statement_timeout_ms,
        pool_timeout_seconds=float(config.db_pool_timeout_seconds),
        node_pool_size=config.db_pool_node,
        person_pool_size=config.db_pool_person,
        **({} if worker_pool_size is None else {"worker_pool_size": worker_pool_size}),
    )
    return Database.create(settings, metrics=metrics, credentials=credentials)


class _NoEvidence:
    """``ObjectHeadReader`` de un proceso sin depósito de evidencias (``vigia-admin``): los tipos
    que escribe no llevan evidencias, y si alguno la llevara la escritura falla cerrada."""

    async def head_object(self, key: str) -> ObjectHead | None:
        raise RuntimeConfigInvalid("VIGIA_EVIDENCE_BUCKET", "ausente: este proceso la exige")


@dataclass(frozen=True, slots=True, kw_only=True)
class Core:
    """La infraestructura común y los registros ya compuestos de un proceso."""

    services: UnitServices
    record_types: RecordTypeRegistry
    free_text: FreeTextPolicyRegistry
    catalog: OutboxCatalog
    secrets: SecretsManagerAdapter
    kms: KmsAdapter
    authorization_audit: PostgresAuthorizationAudit
    synchronizers: tuple[Synchronizer, ...]


def build_core(
    config: RuntimeConfig,
    *,
    database: RuntimeDatabase,
    clock: Clock,
    metrics: PlatformMetrics,
    provider_organization_id: uuid.UUID,
    units: Sequence[PlatformUnit],
    evidence: S3Storage | None = None,
    archive: S3Storage | None = None,
    cpu_pool: CpuPool | None = None,
    verifier_path: Path = DEFAULT_VERIFIER_PATH,
) -> Core:
    """Compone la infraestructura común sin abrir conexiones (solo construye clientes)."""
    provider = provider_organization_id
    aws = aws_settings(config)
    kms = KmsAdapter(aws)
    secrets = SecretsManagerAdapter(aws, clock, kms_key_id=config.secrets_key_arn, metrics=metrics)
    contexts = ScopeContexts(
        store=PostgresContextStore(database),
        clock=clock,
        provider_organization_id=provider,
        system_actor_id=SYSTEM_ACTOR_ID,
    )
    audit = AuditWriter(database=database, clock=clock, provider_organization_id=provider)
    catalog = OutboxCatalog()
    outbox = Outbox(catalog, clock)
    record_types = record_type_registry(units)
    free_text = free_text_registry(units)
    writer = EscritorExpediente(
        database=database,
        registry=record_types,
        free_text=free_text,
        evidence=EvidenceVerifier(evidence if evidence is not None else _NoEvidence(), clock),
        outbox=outbox,
        clock=clock,
        cpu_pool=cpu_pool,
        metrics=metrics,
    )
    authorization_audit = PostgresAuthorizationAudit(
        database=database, audit=audit, outbox=outbox, clock=clock
    )
    authorizer = Authorizer(audit=authorization_audit, provider_organization_id=provider)
    signing = SigningService(
        provider_organization_id=provider,
        store=SqlSigningKeyStore(
            database=database,
            context=contexts.provider_audit_context,
            recorder=LedgerRotationRecorder(database=database, writer=writer, audit=audit),
        ),
        secrets=secrets,
        events=LedgerKeyEventWriter(writer),
        clock=clock,
        environment=config.signing_environment,
        metrics=metrics,
    )
    checkpoints = CheckpointService(
        store=SqlCheckpointStore(database=database, writer=writer, audit=audit, outbox=outbox),
        signer=signing,
        clock=clock,
    )
    services = UnitServices(
        clock=clock,
        metrics=metrics,
        provider_organization_id=provider,
        database=database,
        contexts=contexts,
        authorizer=authorizer,
        audit=audit,
        outbox=outbox,
        writer=writer,
        signing=signing,
        checkpoints=checkpoints,
        kms=kms,
        evidence=evidence,
        archive=archive,
        verifier_path=verifier_path,
        config=config,
    )
    outbox_catalog(units, services, into=catalog)

    async def synchronize_record_types() -> None:
        async with database.transaction(contexts.provider_audit_context()) as transaction:
            await record_types.synchronize(SqlRecordTypeStore(transaction))

    async def synchronize_catalog() -> None:
        async with database.transaction(contexts.provider_audit_context()) as transaction:
            await catalog.synchronize(SqlOutboxCatalogStore(transaction), clock)

    return Core(
        services=services,
        record_types=record_types,
        free_text=free_text,
        catalog=catalog,
        secrets=secrets,
        kms=kms,
        authorization_audit=authorization_audit,
        synchronizers=(synchronize_record_types, synchronize_catalog),
    )
