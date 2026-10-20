"""Registro de ``accreditation`` en la raíz de composición (TASK-302; LC-LAZ-37; A-52).

``shared.runtime.units`` declara la unidad ``accreditation`` con lo que aquí se compone; ninguna
tarea posterior de U-04 vuelve a editar ``units.py``. Cada tarea de servicio del módulo **añade
una línea** a los manifiestos y a las tuplas de este archivo:

- ``ROUTERS``: fábricas de los enrutadores del módulo (``/accreditation/*``, LC-LAZ-41);
- ``ACCREDITATION_TASKS`` (nombre → cadencia e iteración, escrito **a mano**) y
  ``TASK_REGISTRARS`` (la función ``register_*`` que la registra con su manejador);
- ``ACCREDITATION_CONSUMERS`` (nombre → concurrencia del mamparo, LC-LAZ-40) y
  ``CONSUMER_REGISTRARS``;
- ``API_STATE``: los servicios que las rutas del módulo dejan en ``app.state``.

Los tipos de registro y los eventos ya están completos (``accreditation.record_types`` y
``accreditation.events``). La comprobación de arranque de las tres unidades de U-04 es
``loop.registration.missing_u04_registrations``.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Final

from fastapi import APIRouter

from vigia_platform.accreditation.adapters.http.adoption_routes import adoption_router
from vigia_platform.accreditation.adapters.http.export_routes import export_router
from vigia_platform.accreditation.adapters.http.exposure_routes import exposure_router
from vigia_platform.accreditation.adapters.http.monthly_routes import (
    MonthlyPackageReads,
    monthly_router,
)
from vigia_platform.accreditation.adapters.http.services import (
    ACCREDITATION_STATE_KEY,
    AccreditationHttp,
)
from vigia_platform.accreditation.adapters.postgres.export_reads import (
    SqlCheckpointLookup,
    SqlEpisodeHistory,
    SqlExportPlaces,
    StorageBlobs,
)
from vigia_platform.accreditation.adoption.reads import AdoptionReads
from vigia_platform.accreditation.adoption.task import (
    ADOPTION_CHECK,
    ADOPTION_CHECK_SCHEDULE,
    AdoptionCheck,
    ProductiveZones,
    register_adoption_check,
)
from vigia_platform.accreditation.detail_codes import (
    ACCREDITATION_DETAIL_CODE_LABEL_BINDINGS,
    AccreditationDetailCode,
)
from vigia_platform.accreditation.events import register_accreditation_event_types
from vigia_platform.accreditation.export.composer_port import (
    MissingPackageComposer,
    MissingUploadPort,
    PackageComposer,
    UploadPort,
)
from vigia_platform.accreditation.export.contents import ExportComposer, ExportPorts
from vigia_platform.accreditation.export.download import ExportDownloads
from vigia_platform.accreditation.export.lease import SqlExportLeaseStore
from vigia_platform.accreditation.export.request import ExportRequests
from vigia_platform.accreditation.export.stream import ExportStream
from vigia_platform.accreditation.export.worker import (
    EXPORT_WORKER,
    EXPORT_WORKER_CONCURRENCY,
    ExportWorker,
    LoggingExportAlarm,
    register_export_worker,
)
from vigia_platform.accreditation.exposure.service import ExposureService
from vigia_platform.accreditation.monthly.audit import SampleAuditService
from vigia_platform.accreditation.monthly.commitments import CommitmentService
from vigia_platform.accreditation.monthly.package import (
    EvidencePort,
    MissingEvidence,
    MonthlyPackageComposer,
)
from vigia_platform.accreditation.monthly.repository import MonthlyPackageRepository
from vigia_platform.accreditation.monthly.task import (
    MONTHLY_PACKAGE,
    MONTHLY_PACKAGE_SCHEDULE,
    MonthlyPackageTask,
    register_monthly_package,
)
from vigia_platform.accreditation.record_types import register_accreditation_record_types
from vigia_platform.catalog.adapters.postgres.query_ports import catalog_query_ports
from vigia_platform.fleet.application.inventory_read import FleetInventory
from vigia_platform.ledger.adapters.checkpoint_store import SqlCheckpointStore
from vigia_platform.ledger.application.coverage import CoverageService
from vigia_platform.ledger.application.evidence_read import EvidenceService
from vigia_platform.ledger.application.reader import LectorExpediente
from vigia_platform.ledger.chain.checkpoints import CheckpointService
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.domain.enums import ACCREDITATION_LABEL_BINDINGS
from vigia_platform.loop.worker.bulkheads import bulkheaded_registry
from vigia_platform.shared.outbox.registries import (
    ConsumerRegistry,
    EventTypeRegistry,
    PeriodicTaskRegistry,
    Schedule,
    TaskIteration,
)

if TYPE_CHECKING:
    from vigia_platform.ledger.registry import RecordTypeRegistry
    from vigia_platform.shared.runtime.units import UnitServices

__all__ = [
    "ACCREDITATION_CONSUMERS",
    "ACCREDITATION_DETAIL_CODES",
    "ACCREDITATION_LABELS",
    "ACCREDITATION_TASKS",
    "API_STATE",
    "CONSUMER_REGISTRARS",
    "ROUTERS",
    "TASK_REGISTRARS",
    "accreditation_api_state",
    "accreditation_consumers",
    "accreditation_event_types",
    "accreditation_record_types",
    "accreditation_routers",
    "accreditation_tasks",
    "exposure_service",
]

type TaskRegistrar = Callable[[PeriodicTaskRegistry, UnitServices], None]
type ConsumerRegistrar = Callable[[ConsumerRegistry, UnitServices], None]
type StateBuilder = Callable[[UnitServices], Mapping[str, object]]


def _export_composer(services: UnitServices, stream: ExportStream) -> ExportComposer:
    """El compositor real (TASK-318) con los puertos de U-02 y U-03 en proceso."""
    evidence = services.require_evidence()
    database = services.database
    ports = catalog_query_ports(database)
    return ExportComposer(
        stream=stream,
        ports=ExportPorts(
            reader=LectorExpediente(
                database=database,
                audit=services.audit,
                clock=services.clock,
                metrics=services.metrics,
            ),
            catalog=ports.catalog,
            gates=ports.gates,
            fleet=FleetInventory(
                database=database,
                authorizer=services.authorizer,
                audit=services.audit,
                clock=services.clock,
                provider_organization_id=services.provider_organization_id,
            ),
            coverage=CoverageService(
                database=database,
                audit=services.audit,
                clock=services.clock,
                metrics=services.metrics,
            ),
            markers=EvidenceService(database=database, audit=services.audit, storage=evidence),
            checkpoints=SqlCheckpointLookup(database, services.checkpoints),
            places=SqlExportPlaces(database),
            blobs=StorageBlobs(database, evidence),
            episodes=SqlEpisodeHistory(database),
            signer=services.signing,
        ),
    )


def _register_export_worker(registry: ConsumerRegistry, services: UnitServices) -> None:
    """``export_worker`` (TASK-317) con el flujo de bytes sobre ``vigia-evidence`` (TASK-345) y el
    compositor real (TASK-318), que escribe ``export_produced`` al cerrar. Sin depósito de
    evidencias no hay flujo ni compositor: uno que falla sin publicar nada (nunca un paquete
    inventado)."""
    evidence = services.evidence
    uploads: UploadPort
    composer: PackageComposer
    if evidence is None:
        uploads, composer = MissingUploadPort(), MissingPackageComposer()
    else:
        stream = ExportStream(store=evidence)
        uploads, composer = stream, _export_composer(services, stream)
    register_export_worker(
        registry,
        ExportWorker(
            leases=SqlExportLeaseStore(database=services.database, writer=services.writer),
            checkpoints=services.checkpoints,
            uploads=uploads,
            composer=composer,
            alarm=LoggingExportAlarm(),
            clock=services.clock,
        ),
    )


def _register_monthly_package(registry: PeriodicTaskRegistry, services: UnitServices) -> None:
    """``monthly_package`` (TASK-342 a 344): registrada **siempre**, también en procesos sin
    depósito de evidencias (``vigia-api``, que no lo necesita para estas rutas pero sí comparte el
    registro de tareas). Sin él, el compositor degrada con ``MissingEvidence`` y falla recién al
    intentar subir el paquete, nunca al arrancar ni al registrar la tarea (mismo patrón que
    ``_register_export_worker`` con ``MissingUploadPort``/``MissingPackageComposer``)."""
    monthly_repository = MonthlyPackageRepository(database=services.database)
    evidence: EvidencePort = MissingEvidence() if services.evidence is None else services.evidence
    # ``services.checkpoints`` no sirve aquí: su ``writer_context`` por defecto espera ya recibir
    # un contexto de U-02 (``_require_u02``), y el de la tarea periódica es de U-04. El paquete
    # mensual necesita su propio ``CheckpointService``, con el mismo almacén y firmante, pero con
    # ``writer_context=services.contexts.checkpoint_writer``: lo traduce al contexto de sistema de
    # U-02 de la misma organización (checkpoints.py documenta a U-04 como invocador legítimo
    # «antes de cada exportación o paquete mensual»).
    checkpoints = CheckpointService(
        store=SqlCheckpointStore(
            database=services.database,
            writer=services.writer,
            audit=services.audit,
            outbox=services.outbox,
        ),
        signer=services.signing,
        clock=services.clock,
        writer_context=services.contexts.checkpoint_writer,
    )
    composer = MonthlyPackageComposer(
        database=services.database,
        reader=LectorExpediente(
            database=services.database,
            audit=services.audit,
            clock=services.clock,
            metrics=services.metrics,
        ),
        repository=monthly_repository,
        evidence=evidence,
        checkpoints=checkpoints,
        writer=services.writer,
        clock=services.clock,
        signer=services.signing,
    )
    task = MonthlyPackageTask(
        config=PostgresLoopConfigurationRepository(services.database),
        repository=monthly_repository,
        composer=composer,
        clock=services.clock,
    )
    register_monthly_package(registry, task)


def _register_adoption_check(registry: PeriodicTaskRegistry, services: UnitServices) -> None:
    """``adoption_check`` (TASK-323): diaria y por organización; las zonas activas salen de la
    proyección de compuertas de U-03 (``ProductiveZones``) y el umbral, de ``loop.config``."""
    register_adoption_check(
        registry,
        AdoptionCheck(
            activations=ProductiveZones(),
            configuration=PostgresLoopConfigurationRepository(services.database),
            outbox=services.outbox,
            clock=services.clock,
        ),
    )


def exposure_service(services: UnitServices) -> ExposureService:
    """``ExposurePort`` (TASK-322) con los puertos de U-02 y U-03 en proceso; el paquete mensual
    (VIG-239) lo construye igual."""
    database = services.database
    ports = catalog_query_ports(database)
    return ExposureService(
        database=database,
        authorizer=services.authorizer,
        catalog=ports.catalog,
        gates=ports.gates,
        coverage=CoverageService(
            database=database,
            audit=services.audit,
            clock=services.clock,
            metrics=services.metrics,
        ),
        settings=PostgresLoopConfigurationRepository(database),
        clock=services.clock,
    )


def _accreditation_state(services: UnitServices) -> Mapping[str, object]:
    """El único ``AccreditationHttp`` del módulo: las tareas de servicio (TASK-317, 345, 344 y
    347) añaden aquí su campo, nunca una clave nueva en ``API_STATE`` (``accreditation_api_state``
    exige una sola aparición de ``ACCREDITATION_STATE_KEY``)."""
    evidence = services.evidence
    monthly_repository = MonthlyPackageRepository(database=services.database)
    return {
        ACCREDITATION_STATE_KEY: AccreditationHttp(
            exports=ExportRequests(
                database=services.database,
                authorizer=services.authorizer,
                audit=services.audit,
                outbox=services.outbox,
                clock=services.clock,
            ),
            # TASK-345: SUP-U05-40 y 23, firmadas por vigia-api sobre vigia-evidence.
            downloads=(
                None
                if evidence is None
                else ExportDownloads(
                    database=services.database,
                    authorizer=services.authorizer,
                    audit=services.audit,
                    storage=evidence,
                    clock=services.clock,
                )
            ),
            # TASK-344: SUP-U05-24, 25 y 41; la descarga por zona firma sobre vigia-evidence.
            monthly_packages=(
                None
                if evidence is None
                else MonthlyPackageReads(
                    database=services.database,
                    repository=monthly_repository,
                    authorizer=services.authorizer,
                    audit=services.audit,
                    storage=evidence,
                )
            ),
            # TASK-322: SUP-U05-31.
            exposure=exposure_service(services),
            # TASK-347: SUP-U05-26 a 29, escrituras del expediente sin depósito de evidencias.
            sample_audit=SampleAuditService(
                database=services.database,
                writer=services.writer,
                authorizer=services.authorizer,
                clock=services.clock,
            ),
            commitments=CommitmentService(
                database=services.database,
                writer=services.writer,
                authorizer=services.authorizer,
                clock=services.clock,
            ),
            # TASK-323: SUP-U05-32, la adopción por zona.
            adoption=AdoptionReads(
                database=services.database,
                authorizer=services.authorizer,
                activations=ProductiveZones(),
                configuration=PostgresLoopConfigurationRepository(services.database),
                clock=services.clock,
            ),
        )
    }


ROUTERS: Final[tuple[Callable[[], APIRouter], ...]] = (
    export_router,
    monthly_router,
    exposure_router,
    adoption_router,  # TASK-323
)
"""Fábricas de los enrutadores de ``/accreditation/*``."""

ACCREDITATION_TASKS: Final[Mapping[str, tuple[Schedule, TaskIteration]]] = {
    MONTHLY_PACKAGE: (MONTHLY_PACKAGE_SCHEDULE, TaskIteration.PER_ORGANIZATION),
    # TASK-323: el diseño no fija la cadencia; diaria y por organización.
    ADOPTION_CHECK: (ADOPTION_CHECK_SCHEDULE, TaskIteration.PER_ORGANIZATION),
}
"""Tareas periódicas del módulo: nombre → (cadencia, iteración), todas con ``unit = U-04``."""
TASK_REGISTRARS: Final[tuple[TaskRegistrar, ...]] = (
    _register_monthly_package,
    _register_adoption_check,
)

ACCREDITATION_CONSUMERS: Final[Mapping[str, int]] = {
    EXPORT_WORKER: EXPORT_WORKER_CONCURRENCY,
}
"""Consumidores del módulo: nombre → concurrencia de su mamparo (LC-LAZ-40)."""
CONSUMER_REGISTRARS: Final[tuple[ConsumerRegistrar, ...]] = (_register_export_worker,)

API_STATE: Final[tuple[StateBuilder, ...]] = (_accreditation_state,)

ACCREDITATION_LABELS: Final[Mapping[str, type[enum.Enum]]] = {
    **ACCREDITATION_LABEL_BINDINGS,
    **ACCREDITATION_DETAIL_CODE_LABEL_BINDINGS,
}
"""Enumeraciones con etiqueta de la unidad (``PlatformUnit.labels``)."""

ACCREDITATION_DETAIL_CODES: Final[tuple[str, ...]] = tuple(
    code.value for code in AccreditationDetailCode
)


def accreditation_routers() -> tuple[APIRouter, ...]:
    return tuple(build() for build in ROUTERS)


def accreditation_record_types(registry: RecordTypeRegistry) -> None:
    register_accreditation_record_types(registry)


def accreditation_event_types(registry: EventTypeRegistry) -> None:
    register_accreditation_event_types(registry)


def accreditation_tasks(registry: PeriodicTaskRegistry, services: UnitServices) -> None:
    for register in TASK_REGISTRARS:
        register(registry, services)


def accreditation_consumers(registry: ConsumerRegistry, services: UnitServices) -> None:
    # Cada consumidor sale con su mamparo (LC-LAZ-40); el registrador no lo conoce.
    bulkheaded = bulkheaded_registry(registry, services)
    for register in CONSUMER_REGISTRARS:
        register(bulkheaded, services)


def accreditation_api_state(services: UnitServices) -> Mapping[str, object]:
    state: dict[str, object] = {}
    for build in API_STATE:
        for key, value in build(services).items():
            if key in state:
                raise ValueError(f"la clave de app.state «{key}» está dos veces en accreditation")
            state[key] = value
    return state
