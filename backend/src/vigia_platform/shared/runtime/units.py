"""Registro por unidad de la raíz de composición (A-52).

**El único lugar** donde cada unidad declara lo que aporta a los procesos. ``REGISTERED_UNITS``
lleva una entrada ``PlatformUnit`` por unidad; ``shared.api.app.platform_units`` y los tres
constructores (``shared.runtime.api``, ``worker`` y ``admin``) leen de aquí:

- ``routers`` y ``detail_codes``: los enrutadores y los ``detail_code`` de ``vigia-api``
  (``UnitRegistration``). ``routers`` no recibe dependencias: la especificación se genera sin red
  (``build_openapi_app``, NFR-NUC-52); los servicios de las rutas llegan a ``app.state``;
- ``labels``: enumeraciones del código cuyos miembros deben tener etiqueta (``LABEL_BINDINGS``,
  NFR-NUC-51);
- ``record_types`` (``RecordTypeRegistry``), ``event_types`` (``EventTypeRegistry``) y
  ``free_text`` (``FreeTextPolicyRegistry``): registros sin dependencias;
- ``consumers`` (``ConsumerRegistry``) y ``periodic_tasks`` (``PeriodicTaskRegistry``): reciben
  ``UnitServices`` (la infraestructura común que construye la raíz) y crean con ella sus
  servicios y manejadores;
- ``api_state``: servicios que la unidad deja en ``app.state`` para sus rutas, construidos con
  ``UnitServices`` (claves propias; ninguna puede pisar a otra).

**Los tres procesos componen el mismo catálogo de la bandeja.** ``vigia-api`` crea las entregas de
cada evento desde su registro de consumidores (``Outbox.publish``) y ``OutboxCatalog.synchronize``
no arranca si la base tiene un consumidor o una tarea que el proceso no registra: por eso la API y
``vigia-admin`` registran también los consumidores y las tareas, aunque nunca los ejecuten. Los
manejadores solo guardan sus dependencias al registrarse; lo que necesita un depósito que el
proceso no tiene (``VIGIA_EVIDENCE_BUCKET``, ``VIGIA_ARCHIVE_BUCKET``) se construye al ejecutarse,
y solo el worker los ejecuta.

U-02 queda registrada con tres entradas (``shared``, ``identity`` y ``ledger``). U-03 (VIG-139,
VIG-144, VIG-163) y U-04 añaden la suya a ``REGISTERED_UNITS`` sin tocar los constructores.
"""

from __future__ import annotations

import contextlib
import enum
import re
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Protocol

from fastapi import APIRouter
from sqlalchemy.engine import Row
from sqlalchemy.sql import Executable
from vigia_contracts.versioning import Version

from vigia_platform.catalog.adapters.http import CATALOG_STATE_KEY, CatalogHttp, catalog_routers
from vigia_platform.catalog.adapters.postgres.admission_repository import (
    PostgresAdmissionRepository,
)
from vigia_platform.catalog.adapters.postgres.agreement_repository import (
    PostgresAgreementRepository,
)
from vigia_platform.catalog.adapters.postgres.catalog_repository import PostgresCatalogRepository
from vigia_platform.catalog.adapters.postgres.gate_repository import PostgresGateRepository
from vigia_platform.catalog.adapters.postgres.plant_policy_repository import (
    PostgresPlantPolicyRepository,
)
from vigia_platform.catalog.adapters.postgres.regression_repository import (
    PostgresRegressionRepository,
)
from vigia_platform.catalog.adapters.postgres.scope_record_repository import (
    PostgresScopeRecordRepository,
)
from vigia_platform.catalog.adapters.postgres.walk_test_repository import (
    PostgresWalkTestRepository,
)
from vigia_platform.catalog.adapters.s3.documents import DocumentObjectStore
from vigia_platform.catalog.application.admission import ADMISSION_RECORD_TYPE, AdmissionService
from vigia_platform.catalog.application.agreements import USE_AGREEMENT_SIGNED, AgreementService
from vigia_platform.catalog.application.documents import DocumentService
from vigia_platform.catalog.application.free_text_validator import (
    register_u03_free_text_validator,
)
from vigia_platform.catalog.application.gates import GATE_STATE_CHANGED, GateService
from vigia_platform.catalog.application.plant_policy import (
    PLANT_POLICY_SIGNED,
    PlantPolicyService,
)
from vigia_platform.catalog.application.publication import (
    PUBLISHED_RECORD_TYPE,
    RETIRED_RECORD_TYPE,
    SINGLE_OCCUPANCY_RECORD_TYPE,
    CatalogPublicationService,
)
from vigia_platform.catalog.application.regression import MARKED_RECORD_TYPE, RegressionService
from vigia_platform.catalog.application.scope_record import (
    MOUNTING_GATE_RECORD,
    ScopeRecordService,
)
from vigia_platform.catalog.application.signatory_policy import SignatoryPolicyService
from vigia_platform.catalog.application.transparency import TransparencyService
from vigia_platform.catalog.application.walk_test import COMMISSIONING_STEP, WalkTestService
from vigia_platform.catalog.detail_codes import (
    CATALOG_DETAIL_CODE_LABEL_BINDINGS,
    CatalogDetailCode,
)
from vigia_platform.catalog.domain.documents import DocumentSettings
from vigia_platform.catalog.domain.enums import CATALOG_LABEL_BINDINGS
from vigia_platform.catalog.record_types import CATALOG_RECORD_TYPES
from vigia_platform.fleet.adapters.http import FLEET_STATE_KEY, FleetHttp, fleet_routers
from vigia_platform.fleet.adapters.postgres.ingest_queries import PostgresIngestStore
from vigia_platform.fleet.adapters.postgres.node_fleet_store import PostgresNodeFleetStore
from vigia_platform.fleet.adapters.s3.clip_storage import ClipObjectStore
from vigia_platform.fleet.application.clip_confirmation import (
    ClipConfirmationService,
    CommissioningClips,
)
from vigia_platform.fleet.application.clip_grants import ClipGrantService
from vigia_platform.fleet.application.common import FleetDependencies
from vigia_platform.fleet.application.enrollment_codes import (
    ATTEMPT_RECORD_TYPE,
    ISSUED_RECORD_TYPE,
    BundleRoots,
    EnrollmentCodeService,
    NodeCaRoots,
    RootsUnavailable,
)
from vigia_platform.fleet.application.heartbeat import HeartbeatDependencies, HeartbeatService
from vigia_platform.fleet.application.ingest import IngestDependencies, IngestService
from vigia_platform.fleet.application.node_declaration import (
    COMMUNICATION_RECORD_TYPE,
    NodeDeclarationService,
)
from vigia_platform.fleet.application.node_revocation import (
    DECOMMISSIONED_RECORD_TYPE,
    REVOKED_RECORD_TYPE,
    NodeRevocationService,
)
from vigia_platform.fleet.application.zone_catalog_for_node import ZoneCatalogForNode
from vigia_platform.fleet.detail_codes import FLEET_DETAIL_CODE_LABEL_BINDINGS, FleetDetailCode
from vigia_platform.fleet.domain.enums import FLEET_LABEL_BINDINGS
from vigia_platform.fleet.events import FLEET_EVENT_TYPES
from vigia_platform.fleet.record_types import FLEET_RECORD_TYPES
from vigia_platform.identity.adapters.concession_store import PostgresConcessionStore
from vigia_platform.identity.adapters.http import identity_routers
from vigia_platform.identity.adapters.session_store import register_session_tasks
from vigia_platform.identity.application.common import IdentityDependencies
from vigia_platform.identity.application.concessions import (
    ConcessionService,
    register_expire_concessions,
)
from vigia_platform.identity.application.hierarchy import HierarchyService
from vigia_platform.identity.authz.authorize import Authorizer
from vigia_platform.identity.authz.context import ScopeContexts
from vigia_platform.ledger.adapters.http import ledger_routers
from vigia_platform.ledger.adapters.integrity_store import SqlIntegrityStore
from vigia_platform.ledger.application.audit_writer import AuditOutcome, AuditWriter
from vigia_platform.ledger.application.checkpoint_task import (
    register_write_checkpoints,
    write_checkpoints_handler,
)
from vigia_platform.ledger.application.evidence_sample import (
    EvidenceSampler,
    evidence_sample_handler,
    register_evidence_sample,
)
from vigia_platform.ledger.application.integrity_requests import register_integrity_on_demand
from vigia_platform.ledger.application.verify_tasks import register_verify_chains
from vigia_platform.ledger.application.writer import EscritorExpediente
from vigia_platform.ledger.chain.checkpoints import CheckpointChain, CheckpointService
from vigia_platform.ledger.chain.verify import IntegrityResult, IntegrityService, VerificationMode
from vigia_platform.ledger.domain.coverage import (
    CommunicationState,
    CoverageLayer,
    CoverageState,
    PlatformCause,
)
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.ledger.record_types import register_u02_record_types
from vigia_platform.ledger.registry import ChainLevel, RecordTypeRegistry
from vigia_platform.node_api.identity import NodeIdentity, PostgresNodeContextStore
from vigia_platform.node_api.limits import AuditBrakeSource, EmergencyBrake, NodeRateLimits
from vigia_platform.node_api.observability import NodeResponses
from vigia_platform.node_api.router import NodeApiGate, NodeOperation, node_router
from vigia_platform.node_api.routes.clip_confirmations import clip_confirmation_operation
from vigia_platform.node_api.routes.clip_uploads import clip_upload_operation
from vigia_platform.node_api.routes.detection_reviews import detection_review_operation
from vigia_platform.node_api.routes.findings import finding_operation
from vigia_platform.node_api.routes.heartbeats import heartbeat_operation
from vigia_platform.node_api.routes.observability_events import observability_event_operation
from vigia_platform.node_api.routes.zone_catalogs import zone_catalog_operation
from vigia_platform.node_api.versioning import VersionPolicy
from vigia_platform.shared.adapters.http import DEFAULT_VERIFIER_PATH, shared_routers
from vigia_platform.shared.api.declarations import NODE_GATE_STATE_KEY, NodeRoute
from vigia_platform.shared.api.errors import ApiErrorCode
from vigia_platform.shared.api.health import health_router
from vigia_platform.shared.archive.audit_archive import (
    AuditArchiver,
    archive_audit_partitions_handler,
    register_archive_audit_partitions,
)
from vigia_platform.shared.archive.partitions import (
    PartitionMaintenance,
    create_partitions_handler,
    register_create_partitions,
)
from vigia_platform.shared.archive.restore_drill import (
    RestoreDrills,
    register_restore_drill_age,
    restore_drill_age_handler,
)
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorKind, ContextOrigin, Role, ScopeContext, ScopeLevel
from vigia_platform.shared.db import DatabaseHealth, Transaction
from vigia_platform.shared.key_rotation import (
    key_rotation_reminder_handler,
    register_key_rotation_reminder,
)
from vigia_platform.shared.observability.alerts_consumer import register_alerts_consumer
from vigia_platform.shared.observability.metrics import PlatformMetrics
from vigia_platform.shared.outbox.publish import Outbox
from vigia_platform.shared.outbox.registries import (
    ConsumerRegistry,
    EventTypeRegistry,
    OutboxCatalog,
    PeriodicHandler,
    PeriodicTaskRegistry,
)
from vigia_platform.shared.outbox.u02_events import register_u02_event_types
from vigia_platform.shared.ratelimit import RateLimiter
from vigia_platform.shared.runtime.config import RuntimeConfig, RuntimeConfigInvalid
from vigia_platform.shared.secrets import KmsPort
from vigia_platform.shared.signing.keys import KeyStatus, SigningPurpose
from vigia_platform.shared.signing.service import SigningService
from vigia_platform.shared.storage import S3Storage
from vigia_platform.shared.tokens import LiveViewTokenService

__all__ = [
    "REGISTERED_UNITS",
    "PlatformUnit",
    "RuntimeDatabase",
    "UnitServices",
    "api_state",
    "free_text_registry",
    "label_bindings",
    "outbox_catalog",
    "record_type_registry",
    "registered_units",
]

_UNIT_NAME: Final = re.compile(r"[a-z][a-z0-9_]{0,31}")


class RuntimeDatabase(Protocol):
    """``shared.db.Database`` o la ``LazyDatabase`` de ``vigia-admin``."""

    def transaction(self, context: Any) -> contextlib.AbstractAsyncContextManager[Transaction]: ...

    async def read(
        self,
        context: Any,
        statement: Executable,
        parameters: Mapping[str, Any] | None = None,
    ) -> Sequence[Row[Any]]: ...

    async def health(self, *, timeout_seconds: float) -> DatabaseHealth: ...

    async def dispose(self) -> None: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class UnitServices:
    """La infraestructura común que la raíz construye y entrega a cada unidad."""

    clock: Clock
    metrics: PlatformMetrics
    provider_organization_id: uuid.UUID
    database: RuntimeDatabase
    contexts: ScopeContexts
    authorizer: Authorizer
    audit: AuditWriter
    outbox: Outbox
    writer: EscritorExpediente
    free_text: FreeTextPolicyRegistry
    """La política de texto libre del escritor, ya sellada (A-45): la usan los servicios que
    validan un texto antes de guardarlo fuera del expediente (p. ej. ``justification_es``)."""
    signing: SigningService
    checkpoints: CheckpointService
    kms: KmsPort
    evidence: S3Storage | None = None
    """Depósito de evidencias (``VIGIA_EVIDENCE_BUCKET``); ``None`` en ``vigia-admin``."""
    archive: S3Storage | None = None
    """Depósito de archivo (``VIGIA_ARCHIVE_BUCKET``); solo en ``vigia-worker``."""
    verifier_path: Path = DEFAULT_VERIFIER_PATH
    config: RuntimeConfig | None = None
    """La configuración leída del entorno (tamaños y prefijos de cada unidad); ``None`` en las
    pruebas que no la necesitan: cada unidad usa entonces los valores del diseño."""

    def require_evidence(self) -> S3Storage:
        if self.evidence is None:
            raise RuntimeConfigInvalid("VIGIA_EVIDENCE_BUCKET", "ausente: este proceso la exige")
        return self.evidence

    def require_archive(self) -> S3Storage:
        if self.archive is None:
            raise RuntimeConfigInvalid("VIGIA_ARCHIVE_BUCKET", "ausente: este proceso la exige")
        return self.archive


def _no_routers() -> tuple[APIRouter, ...]:
    return ()


def _nothing(_: object) -> None:
    return None


def _nothing_with(_: object, __: UnitServices) -> None:
    return None


def _no_state(_: UnitServices) -> Mapping[str, object]:
    return {}


@dataclass(frozen=True, slots=True, kw_only=True)
class PlatformUnit:
    """Lo que una unidad aporta a ``vigia-api``, ``vigia-worker`` y ``vigia-admin``."""

    name: str
    routers: Callable[[], tuple[APIRouter, ...]] = _no_routers
    detail_codes: tuple[str, ...] = ()
    labels: Mapping[str, type[enum.Enum]] = field(default_factory=dict)
    record_types: Callable[[RecordTypeRegistry], None] = _nothing
    event_types: Callable[[EventTypeRegistry], None] = _nothing
    free_text: Callable[[FreeTextPolicyRegistry], None] = _nothing
    consumers: Callable[[ConsumerRegistry, UnitServices], None] = _nothing_with
    periodic_tasks: Callable[[PeriodicTaskRegistry, UnitServices], None] = _nothing_with
    api_state: Callable[[UnitServices], Mapping[str, object]] = _no_state

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _UNIT_NAME.fullmatch(self.name) is None:
            raise ValueError("el nombre de la unidad no es un identificador válido")
        object.__setattr__(self, "labels", MappingProxyType(dict(self.labels)))


# --- U-02 ----------------------------------------------------------------------------------------


def _lazy_handler(build: Callable[[], PeriodicHandler]) -> PeriodicHandler:
    """Manejador que construye su servicio en la primera ejecución (solo en el worker)."""
    built: list[PeriodicHandler] = []

    async def handler(transaction: Transaction) -> None:
        if not built:
            built.append(build())
        await built[0](transaction)

    return handler


def _shared_tasks(registry: PeriodicTaskRegistry, services: UnitServices) -> None:
    provider = services.provider_organization_id
    register_key_rotation_reminder(
        registry,
        key_rotation_reminder_handler(
            services.signing, services.outbox, services.clock, provider_organization_id=provider
        ),
    )
    register_create_partitions(
        registry,
        create_partitions_handler(
            PartitionMaintenance(clock=services.clock, metrics=services.metrics), provider
        ),
    )

    def archiver() -> PeriodicHandler:
        return archive_audit_partitions_handler(
            AuditArchiver(
                database=services.database,
                storage=services.require_archive(),
                writer=services.writer,
                audit=services.audit,
                outbox=services.outbox,
                checkpoint_keys=services.checkpoints.checkpoint_public_keys,
                verifier=services.verifier_path.read_bytes(),
                clock=services.clock,
            ),
            provider,
        )

    register_archive_audit_partitions(registry, _lazy_handler(archiver))
    register_restore_drill_age(
        registry,
        restore_drill_age_handler(
            RestoreDrills(
                database=services.database,
                audit=services.audit,
                clock=services.clock,
                metrics=services.metrics,
            ),
            provider,
        ),
    )


def _shared_consumers(registry: ConsumerRegistry, services: UnitServices) -> None:
    register_alerts_consumer(registry, services.metrics)


def _identity_tasks(registry: PeriodicTaskRegistry, services: UnitServices) -> None:
    register_expire_concessions(
        registry,
        ConcessionService(
            store=PostgresConcessionStore(database=services.database, audit=services.audit),
            writer=services.writer,
            authorizer=services.authorizer,
            contexts=services.contexts,
            clock=services.clock,
        ),
    )
    register_session_tasks(registry, audit=services.audit, clock=services.clock)


class _WorkerIntegrity:
    """``IntegrityService`` construido en la primera verificación.

    ``SqlIntegrityStore`` solo acepta la base del worker (``statement_timeout`` de 30 s): en
    ``vigia-api`` y ``vigia-admin`` el consumidor y las tareas se registran, pero nunca corren.
    """

    def __init__(self, services: UnitServices) -> None:
        self._services = services
        self._service: IntegrityService | None = None

    def _get(self) -> IntegrityService:
        if self._service is None:
            services = self._services
            self._service = IntegrityService(
                store=SqlIntegrityStore(
                    database=services.database, audit=services.audit, outbox=services.outbox
                ),
                keys=services.checkpoints,
                clock=services.clock,
                metrics=services.metrics,
            )
        return self._service

    async def verify(
        self, context: ScopeContext, chain: CheckpointChain, mode: VerificationMode
    ) -> IntegrityResult:
        return await self._get().verify(context, chain, mode)

    async def verify_all(
        self, context: ScopeContext, mode: VerificationMode
    ) -> tuple[IntegrityResult, ...]:
        return await self._get().verify_all(context, mode)


def _ledger_consumers(registry: ConsumerRegistry, services: UnitServices) -> None:
    register_integrity_on_demand(registry, _WorkerIntegrity(services))


def _ledger_tasks(registry: PeriodicTaskRegistry, services: UnitServices) -> None:
    register_write_checkpoints(registry, write_checkpoints_handler(services.checkpoints))
    register_verify_chains(registry, _WorkerIntegrity(services))

    def sampler() -> PeriodicHandler:
        return evidence_sample_handler(
            EvidenceSampler(
                database=services.database,
                audit=services.audit,
                outbox=services.outbox,
                storage=services.require_evidence(),
                clock=services.clock,
            )
        )

    register_evidence_sample(registry, _lazy_handler(sampler))


def _shared_routers() -> tuple[APIRouter, ...]:
    return (health_router(), *shared_routers())


# --- U-03 ----------------------------------------------------------------------------------------


_CATALOG_WRITTEN_TYPES: Final = frozenset(
    {
        ADMISSION_RECORD_TYPE,
        COMMISSIONING_STEP,
        GATE_STATE_CHANGED,
        MOUNTING_GATE_RECORD,
        PLANT_POLICY_SIGNED,
        PUBLISHED_RECORD_TYPE,
        RETIRED_RECORD_TYPE,
        SINGLE_OCCUPANCY_RECORD_TYPE,
        MARKED_RECORD_TYPE,
        USE_AGREEMENT_SIGNED,
    }
)
"""Tipos de ``catalog.record_types`` que ya escribe una ruta registrada."""


def _catalog_record_types(registry: RecordTypeRegistry) -> None:
    for definition in CATALOG_RECORD_TYPES:
        if definition.record_type in _CATALOG_WRITTEN_TYPES:
            registry.register(definition)


def _document_settings(config: RuntimeConfig | None) -> DocumentSettings:
    """``VIGIA_DOCUMENTS_PREFIX`` y ``VIGIA_DOCUMENTS_MAX_BYTES``; sin configuración, el diseño."""
    if config is None:
        return DocumentSettings()
    return DocumentSettings(prefix=config.documents_prefix, max_bytes=config.documents_max_bytes)


def _catalog_state(services: UnitServices) -> Mapping[str, object]:
    # LC-GOB-05 (VIG-143): POST /documents sobre vigia-evidence; las actas y la política de
    # planta verifican sus documentos con el mismo servicio.
    documents = DocumentService(
        database=services.database,
        audit=services.audit,
        authorizer=services.authorizer,
        store=DocumentObjectStore(services.require_evidence()),
        clock=services.clock,
        settings=_document_settings(services.config),
    )
    catalog = PostgresCatalogRepository(services.database)
    policies = PostgresPlantPolicyRepository(services.database)
    agreements = PostgresAgreementRepository()
    # LC-GOB-03 (VIG-146): compuertas con su sobre GateState firmado por SigningPort; revocar el
    # uso revoca el acuerdo vigente (VIG-149).
    gates = GateService(
        repository=PostgresGateRepository(services.database),
        catalog=catalog,
        agreements=agreements,
        database=services.database,
        writer=services.writer,
        authorizer=services.authorizer,
        audit=services.audit,
        free_text=services.free_text,
        signer=services.signing,
        clock=services.clock,
    )
    admissions = AdmissionService(
        repository=PostgresAdmissionRepository(services.database),
        database=services.database,
        writer=services.writer,
        authorizer=services.authorizer,
        audit=services.audit,
        free_text=services.free_text,
        clock=services.clock,
    )
    # LC-GOB-09 (VIG-148): la marca de regresión, dentro de la transacción de la publicación.
    regression = RegressionService(
        repository=PostgresRegressionRepository(services.database),
        catalog=catalog,
        database=services.database,
        writer=services.writer,
        authorizer=services.authorizer,
        audit=services.audit,
        free_text=services.free_text,
        clock=services.clock,
    )
    # LC-GOB-01 (VIG-145, VIG-148): catálogo firmado por SigningPort, con la admisión de LC-GOB-02.
    publication = CatalogPublicationService(
        repository=catalog,
        database=services.database,
        writer=services.writer,
        authorizer=services.authorizer,
        audit=services.audit,
        free_text=services.free_text,
        admissions=admissions,
        signer=services.signing,
        clock=services.clock,
        regression_marker=regression,
    )
    hierarchy = HierarchyService(
        IdentityDependencies(
            database=services.database,
            writer=services.writer,
            audit=services.audit,
            outbox=services.outbox,
            authorizer=services.authorizer,
            free_text=services.free_text,
            clock=services.clock,
            provider_organization_id=services.provider_organization_id,
        )
    )
    return {
        CATALOG_STATE_KEY: CatalogHttp(
            admissions=admissions,
            documents=documents,
            gates=gates,
            scope_records=ScopeRecordService(
                gates=gates,
                records=PostgresScopeRecordRepository(services.database),
                catalog=catalog,
                policies=policies,
                documents=documents,
                nodes=hierarchy,
                writer=services.writer,
                free_text=services.free_text,
            ),
            plant_policies=PlantPolicyService(
                repository=policies,
                documents=documents,
                database=services.database,
                writer=services.writer,
                authorizer=services.authorizer,
                audit=services.audit,
                free_text=services.free_text,
                clock=services.clock,
            ),
            catalog=publication,
            regression=regression,
            # LC-GOB-04 (VIG-149): firmantes, acuerdo de uso y transparencia.
            signatory_policies=SignatoryPolicyService(
                repository=agreements,
                plants=policies,
                database=services.database,
                authorizer=services.authorizer,
                audit=services.audit,
                clock=services.clock,
            ),
            agreements=AgreementService(
                repository=agreements,
                gates=gates,
                policies=policies,
                documents=documents,
                identity=hierarchy,
                database=services.database,
                writer=services.writer,
                authorizer=services.authorizer,
                clock=services.clock,
            ),
            transparency=TransparencyService(
                repository=agreements,
                gates=gates,
                catalog=catalog,
                database=services.database,
                audit=services.audit,
            ),
            # LC-GOB-06 (VIG-150): sesión de walk-test; las pruebas de oclusión llegan con
            # VIG-154 (hasta entonces, ninguna).
            walk_tests=WalkTestService(
                repository=PostgresWalkTestRepository(),
                catalog=catalog,
                gates=gates,
                nodes=hierarchy,
                identity=hierarchy,
                database=services.database,
                writer=services.writer,
                audit=services.audit,
                free_text=services.free_text,
                clock=services.clock,
            ),
        )
    }


_FLEET_WRITTEN_TYPES: Final = frozenset(
    {
        COMMUNICATION_RECORD_TYPE,
        REVOKED_RECORD_TYPE,
        DECOMMISSIONED_RECORD_TYPE,
        ISSUED_RECORD_TYPE,
        ATTEMPT_RECORD_TYPE,
    }
)
"""Tipos de ``fleet.record_types`` que ya escribe una ruta o un servicio registrado (TASK-218)."""
_FLEET_PUBLISHED_EVENTS: Final = frozenset({"node_revoked", "node_decommissioned"})
"""Eventos de ``fleet.events`` que ya publica un servicio registrado (TASK-218)."""


def _fleet_record_types(registry: RecordTypeRegistry) -> None:
    for definition in FLEET_RECORD_TYPES:
        if definition.record_type in _FLEET_WRITTEN_TYPES:
            registry.register(definition)


def _fleet_event_types(registry: EventTypeRegistry) -> None:
    for event_type in FLEET_EVENT_TYPES:
        if event_type.event_name in _FLEET_PUBLISHED_EVENTS:
            registry.register(event_type)


class _UnpublishedRoots:
    """Sin ``VIGIA_EDGE_BUCKET``: no hay raíz publicada que mostrar, así que no se emite código."""

    async def fingerprints(self) -> tuple[str, ...]:
        raise RootsUnavailable("este proceso no tiene el depósito vigia-edge")


def _node_ca_roots(services: UnitServices) -> NodeCaRoots:
    """``ca/root.pem`` de ``vigia-edge`` (``VIGIA_EDGE_BUCKET``), leído en cada emisión."""
    config = services.config
    if config is None or config.edge_bucket is None:
        return _UnpublishedRoots()
    # ``runtime.core`` importa este módulo: se resuelve al construir, nunca al importar.
    from vigia_platform.shared.runtime.core import s3_storage

    return BundleRoots(s3_storage(config, config.edge_bucket, services.clock))


def _fleet_state(services: UnitServices) -> Mapping[str, object]:
    identity = HierarchyService(
        IdentityDependencies(
            database=services.database,
            writer=services.writer,
            audit=services.audit,
            outbox=services.outbox,
            authorizer=services.authorizer,
            free_text=services.free_text,
            clock=services.clock,
            provider_organization_id=services.provider_organization_id,
        )
    )
    deps = FleetDependencies(
        database=services.database,
        writer=services.writer,
        audit=services.audit,
        authorizer=services.authorizer,
        free_text=services.free_text,
        clock=services.clock,
        identity=identity,
        nodes=PostgresNodeFleetStore(services.database),
        metrics=services.metrics,
    )
    return {
        FLEET_STATE_KEY: FleetHttp(
            declarations=NodeDeclarationService(deps),
            # La clave estable del hash de origen la cablea la ruta del alta (VIG-151).
            enrollment_codes=EnrollmentCodeService(deps, roots=_node_ca_roots(services)),
            revocations=NodeRevocationService(deps),
            # LC-GOB-13 (VIG-152): clips de verificación de la zona para el selector de U-05.
            commissioning_clips=CommissioningClips(
                database=services.database,
                authorizer=services.authorizer,
                audit=services.audit,
                clock=services.clock,
            ),
        )
    }


PUBLISHED_NODE_ROUTES: Final[tuple[NodeRoute, ...]] = (
    NodeRoute.HEARTBEAT,
    NodeRoute.CLIP_UPLOAD,
    NodeRoute.CLIP_CONFIRMATION,
    NodeRoute.ZONE_CATALOG,
    NodeRoute.FINDING,
    NodeRoute.DETECTION_REVIEW,
    NodeRoute.OBSERVABILITY_EVENT,
)
"""Rutas del contrato que ``vigia-api`` publica (TASK-206): cada tarea de negocio (TASK-219, 221,
222, 223, 226) añade aquí la suya y su manejador en ``_node_operations``. VIG-152 (TASK-222)
publica la concesión de clip y la confirmación del clip de verificación; TASK-223, el latido y el
catálogo por zona; VIG-156 (TASK-221), las tres rutas de la ingesta. Desde entonces ``app.yaml``
tiene rutas ``/api/nodes/`` y el trabajo de conformidad de ``nightly.yml`` falla, a propósito,
hasta que TASK-230 escriba su ejecución."""


def _node_routers() -> tuple[APIRouter, ...]:
    return (node_router(PUBLISHED_NODE_ROUTES),)


def _heartbeat_service(services: UnitServices, policy: VersionPolicy) -> HeartbeatService:
    """``fleet.heartbeat`` (TASK-223) con los puertos que usa: caché de claves, renovación del
    sobre de compuertas (A-55), marca de regresión por ``model_version``, ``update_node`` de U-02
    y la incorporación de los accesos locales a la vista en vivo."""
    database = services.database
    catalog = PostgresCatalogRepository(database)
    gates = GateService(
        repository=PostgresGateRepository(database),
        catalog=catalog,
        agreements=PostgresAgreementRepository(),
        database=database,
        writer=services.writer,
        authorizer=services.authorizer,
        audit=services.audit,
        free_text=services.free_text,
        signer=services.signing,
        clock=services.clock,
    )
    regression = RegressionService(
        repository=PostgresRegressionRepository(database),
        catalog=catalog,
        database=database,
        writer=services.writer,
        authorizer=services.authorizer,
        audit=services.audit,
        free_text=services.free_text,
        clock=services.clock,
    )
    identity = HierarchyService(
        IdentityDependencies(
            database=database,
            writer=services.writer,
            audit=services.audit,
            outbox=services.outbox,
            authorizer=services.authorizer,
            free_text=services.free_text,
            clock=services.clock,
            provider_organization_id=services.provider_organization_id,
        )
    )
    live_view = LiveViewTokenService(
        database=database,
        authorizer=services.authorizer,
        audit=services.audit,
        outbox=services.outbox,
        signer=services.signing,
        clock=services.clock,
        metrics=services.metrics,
    )

    def retires_at(version: str) -> str | None:
        return policy.retires_at(Version.parse(version))

    return HeartbeatService(
        HeartbeatDependencies(
            database=database,
            writer=services.writer,
            clock=services.clock,
            key_sets=services.signing,
            gates=gates,
            regression=regression,
            identity=identity,
            live_view=live_view,
            retires_at=retires_at,
            nodes=PostgresNodeFleetStore(database),
            metrics=services.metrics,
        )
    )


def _node_operations(
    services: UnitServices, policy: VersionPolicy
) -> Mapping[NodeRoute, NodeOperation]:
    """Los manejadores de negocio de las rutas publicadas."""
    # LC-GOB-13 (VIG-152): concesiones de clip y clip de verificación sobre vigia-evidence.
    store = ClipObjectStore(services.require_evidence())
    # LC-GOB-12 (VIG-156): la ingesta; los clips los verifica el escritor (EvidenceVerifier).
    ingest = IngestService(
        IngestDependencies(
            database=services.database,
            writer=services.writer,
            audit=services.audit,
            clock=services.clock,
            store=PostgresIngestStore(services.database),
        )
    )
    return {
        NodeRoute.FINDING: finding_operation(ingest),
        NodeRoute.DETECTION_REVIEW: detection_review_operation(ingest),
        NodeRoute.OBSERVABILITY_EVENT: observability_event_operation(ingest),
        # TASK-223: latido y catálogo por zona.
        NodeRoute.HEARTBEAT: heartbeat_operation(_heartbeat_service(services, policy)),
        NodeRoute.CLIP_UPLOAD: clip_upload_operation(
            ClipGrantService(
                database=services.database,
                store=store,
                clock=services.clock,
                metrics=services.metrics,
            )
        ),
        NodeRoute.CLIP_CONFIRMATION: clip_confirmation_operation(
            ClipConfirmationService(
                database=services.database,
                store=store,
                clock=services.clock,
                metrics=services.metrics,
            )
        ),
        NodeRoute.ZONE_CATALOG: zone_catalog_operation(
            ZoneCatalogForNode(database=services.database)
        ),
    }


def _node_api_state(services: UnitServices) -> Mapping[str, object]:
    """La verificación previa común de las rutas del contrato (``NodeApiGate``, TASK-206)."""
    database = services.database
    contexts = services.contexts
    clock = services.clock
    policy = VersionPolicy()
    brake = EmergencyBrake(
        AuditBrakeSource(database=database, provider_context=contexts.provider_audit_context),
        clock,
    )
    return {
        NODE_GATE_STATE_KEY: NodeApiGate(
            identity=NodeIdentity(contexts=contexts, store=PostgresNodeContextStore(database)),
            limits=NodeRateLimits(RateLimiter(clock), brake=brake, metrics=services.metrics),
            clock=clock,
            responses=NodeResponses(clock, metrics=services.metrics),
            policy=policy,
            operations=_node_operations(services, policy),
        )
    }


REGISTERED_UNITS: Final[tuple[PlatformUnit, ...]] = (
    PlatformUnit(
        name="shared",
        routers=_shared_routers,
        labels={
            "api_error_code": ApiErrorCode,
            "role": Role,
            "scope_level": ScopeLevel,
            "actor_kind": ActorKind,
            "context_origin": ContextOrigin,
            "signing_purpose": SigningPurpose,
            "key_status": KeyStatus,
        },
        event_types=register_u02_event_types,
        consumers=_shared_consumers,
        periodic_tasks=_shared_tasks,
    ),
    PlatformUnit(
        name="identity",
        routers=identity_routers,
        periodic_tasks=_identity_tasks,
    ),
    PlatformUnit(
        name="ledger",
        routers=ledger_routers,
        labels={
            "chain_level": ChainLevel,
            "audit_outcome": AuditOutcome,
            "coverage_state": CoverageState,
            "coverage_layer": CoverageLayer,
            "platform_cause": PlatformCause,
            "communication_state": CommunicationState,
        },
        record_types=register_u02_record_types,
        consumers=_ledger_consumers,
        periodic_tasks=_ledger_tasks,
    ),
    # U-03 (VIG-139): las etiquetas de sus 21 enumeraciones y de sus detail_code (NFR-GOB-67).
    # VIG-142: rutas de la admisión, detail_code del catálogo, el tipo que escriben
    # (``standard_admission_test``) y el validador mínimo de texto libre de U-03 (A-45, D-7). El
    # resto de tipos y los eventos los conecta VIG-163 (TASK-227). VIG-143: POST /documents.
    PlatformUnit(
        name="catalog",
        routers=catalog_routers,
        detail_codes=tuple(code.value for code in CatalogDetailCode),
        labels={**CATALOG_LABEL_BINDINGS, **CATALOG_DETAIL_CODE_LABEL_BINDINGS},
        record_types=_catalog_record_types,
        free_text=register_u03_free_text_validator,
        api_state=_catalog_state,
    ),
    # VIG-147 (TASK-218): rutas de identidad del nodo de SCR-07, sus detail_code, los tipos que
    # escriben y los eventos node_revoked y node_decommissioned. VIG-152: GET
    # /zones/{zone_id}/commissioning-clips; ``mark_orphan_clips`` lo registra VIG-163 (TASK-227)
    # con ``register_mark_orphan_clips``.
    PlatformUnit(
        name="fleet",
        routers=fleet_routers,
        detail_codes=tuple(code.value for code in FleetDetailCode),
        labels={**FLEET_LABEL_BINDINGS, **FLEET_DETAIL_CODE_LABEL_BINDINGS},
        record_types=_fleet_record_types,
        event_types=_fleet_event_types,
        api_state=_fleet_state,
    ),
    # VIG-144 (TASK-206): el adaptador único de las rutas del contrato (LC-GOB-19, A-51).
    PlatformUnit(name="node_api", routers=_node_routers, api_state=_node_api_state),
)
"""Las unidades registradas, en orden de registro. U-03 y U-04 añaden aquí su entrada."""


def registered_units() -> tuple[PlatformUnit, ...]:
    """``REGISTERED_UNITS``, leído en cada llamada; nombres únicos."""
    units = REGISTERED_UNITS
    names = [unit.name for unit in units]
    if len(set(names)) != len(names):
        raise ValueError("hay dos unidades con el mismo nombre en el registro")
    return units


# --- Composición ---------------------------------------------------------------------------------


def label_bindings(units: Iterable[PlatformUnit]) -> dict[str, type[enum.Enum]]:
    """Las enumeraciones con etiqueta de todas las unidades; un nombre repetido es un error."""
    bindings: dict[str, type[enum.Enum]] = {}
    for unit in units:
        for name, enumeration in unit.labels.items():
            if name in bindings and bindings[name] is not enumeration:
                raise ValueError(f"la enumeración con etiqueta «{name}» está en dos unidades")
            bindings[name] = enumeration
    return bindings


def record_type_registry(units: Iterable[PlatformUnit]) -> RecordTypeRegistry:
    """Los tipos de registro de todas las unidades, sin sellar (los sella ``synchronize``)."""
    registry = RecordTypeRegistry()
    for unit in units:
        unit.record_types(registry)
    return registry


def free_text_registry(units: Iterable[PlatformUnit]) -> FreeTextPolicyRegistry:
    """Los validadores de texto libre de todas las unidades, ya sellado (A-45)."""
    registry = FreeTextPolicyRegistry()
    for unit in units:
        unit.free_text(registry)
    registry.seal()
    return registry


def outbox_catalog(
    units: Iterable[PlatformUnit],
    services: UnitServices,
    *,
    into: OutboxCatalog | None = None,
) -> OutboxCatalog:
    """Eventos, consumidores y tareas de todas las unidades, sin sellar.

    Primero los eventos de todas (un consumidor se suscribe a eventos de otra unidad), luego
    consumidores y tareas; ``check`` detiene la composición si algo no casa. ``into`` es el
    catálogo vacío que ya usa la ``Outbox`` de ``services`` (se llena en el sitio).
    """
    selected = tuple(units)
    catalog = OutboxCatalog() if into is None else into
    for unit in selected:
        unit.event_types(catalog.event_types)
    for unit in selected:
        unit.consumers(catalog.consumers, services)
        unit.periodic_tasks(catalog.periodic_tasks, services)
    catalog.check()
    return catalog


def api_state(units: Iterable[PlatformUnit], services: UnitServices) -> dict[str, object]:
    """Los servicios que cada unidad deja en ``app.state``; una clave repetida es un error."""
    state: dict[str, object] = {}
    for unit in units:
        for key, value in unit.api_state(services).items():
            if key in state:
                raise ValueError(f"la clave de app.state «{key}» está en dos unidades")
            state[key] = value
    return state
