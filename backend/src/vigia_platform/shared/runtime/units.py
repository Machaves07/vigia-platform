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

from vigia_platform.catalog.detail_codes import CATALOG_DETAIL_CODE_LABEL_BINDINGS
from vigia_platform.catalog.domain.enums import CATALOG_LABEL_BINDINGS
from vigia_platform.fleet.detail_codes import FLEET_DETAIL_CODE_LABEL_BINDINGS
from vigia_platform.fleet.domain.enums import FLEET_LABEL_BINDINGS
from vigia_platform.identity.adapters.concession_store import PostgresConcessionStore
from vigia_platform.identity.adapters.http import identity_routers
from vigia_platform.identity.adapters.session_store import register_session_tasks
from vigia_platform.identity.application.concessions import (
    ConcessionService,
    register_expire_concessions,
)
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
from vigia_platform.shared.adapters.http import DEFAULT_VERIFIER_PATH, shared_routers
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
from vigia_platform.shared.runtime.config import RuntimeConfigInvalid
from vigia_platform.shared.secrets import KmsPort
from vigia_platform.shared.signing.keys import KeyStatus, SigningPurpose
from vigia_platform.shared.signing.service import SigningService
from vigia_platform.shared.storage import S3Storage

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
    signing: SigningService
    checkpoints: CheckpointService
    kms: KmsPort
    evidence: S3Storage | None = None
    """Depósito de evidencias (``VIGIA_EVIDENCE_BUCKET``); ``None`` en ``vigia-admin``."""
    archive: S3Storage | None = None
    """Depósito de archivo (``VIGIA_ARCHIVE_BUCKET``); solo en ``vigia-worker``."""
    verifier_path: Path = DEFAULT_VERIFIER_PATH

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
    # U-03 (VIG-139): por ahora solo las etiquetas de sus 21 enumeraciones y de sus detail_code
    # (NFR-GOB-67). Tipos, eventos, detail_code y validador los conecta VIG-163 (TASK-227).
    PlatformUnit(
        name="catalog",
        labels={**CATALOG_LABEL_BINDINGS, **CATALOG_DETAIL_CODE_LABEL_BINDINGS},
    ),
    PlatformUnit(
        name="fleet",
        labels={**FLEET_LABEL_BINDINGS, **FLEET_DETAIL_CODE_LABEL_BINDINGS},
    ),
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
