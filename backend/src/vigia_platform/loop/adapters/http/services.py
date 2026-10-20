"""Servicios de las rutas de ``loop`` (``app.state``) y su construcción por la raíz.

``LoopHttp`` lo deja la unidad ``loop`` en ``app.state`` (``API_STATE`` de
``loop.registration``, A-52), construido con la infraestructura común (``UnitServices``). Las rutas
lo toman en cada petición: sin él, ``internal_error`` (fallo cerrado, nunca deja pasar). Los
servicios son opcionales para que cada grupo de rutas se pueda montar por separado; cada tarea de
U-04 que añade rutas añade aquí su servicio.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Final

from fastapi import Request

from vigia_platform.catalog.adapters.postgres.query_ports import catalog_query_ports
from vigia_platform.fleet.application.inventory_read import FleetInventory
from vigia_platform.ledger.application.coverage import CoverageService
from vigia_platform.ledger.application.evidence_read import (
    EvidenceService as LedgerEvidenceService,
)
from vigia_platform.ledger.application.reader import LectorExpediente
from vigia_platform.loop.adapters.postgres.review_queue_repository import ReviewQueueRepository
from vigia_platform.loop.application.actions import ActionService
from vigia_platform.loop.application.classification import ClassificationService
from vigia_platform.loop.application.closure import ClosureService
from vigia_platform.loop.application.deadlines import OverdueService
from vigia_platform.loop.application.detail import FindingDetailService
from vigia_platform.loop.application.evidence import EvidenceService
from vigia_platform.loop.application.inbox import InboxService
from vigia_platform.loop.application.reopening import ReopeningService
from vigia_platform.loop.application.review_queue import ReviewQueueService
from vigia_platform.loop.application.root_cause import RootCauseService
from vigia_platform.loop.application.unclosable import UnclosableService
from vigia_platform.loop.assistant.service import AssistantService
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.config.service import LoopConfigurationService
from vigia_platform.loop.vocabulary.repository import PostgresVocabularyRepository
from vigia_platform.loop.vocabulary.service import VocabularyService
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.storage import (
    PRESIGN_GET_MAX_TTL,
    ObjectHead,
    PresignedRequest,
    StorageUnavailable,
)

if TYPE_CHECKING:
    from vigia_platform.shared.runtime.units import UnitServices

__all__ = [
    "LOOP_STATE_KEY",
    "LoopHttp",
    "build_detail",
    "build_loop_http",
    "installed",
    "loop_http",
    "loop_http_state",
]

LOOP_STATE_KEY: Final = "vigia_loop_http"

_log = get_logger("loop.http")


@dataclass(frozen=True, slots=True, kw_only=True)
class LoopHttp:
    """Los servicios que usan las rutas de ``loop``."""

    configuration: LoopConfigurationService | None = None
    """``GET`` y ``PUT /loop/plants/{plant_id}/configuration`` (LC-LAZ-19, VIG-205)."""
    classification: ClassificationService | None = None
    """``POST /loop/findings/{finding_state_id}/classifications`` (LC-LAZ-07, VIG-212)."""
    root_cause: RootCauseService | None = None
    """Las tres rutas del análisis de causa raíz guiado (LC-LAZ-12, VIG-214)."""
    actions: ActionService | None = None
    """``POST /loop/findings/{finding_state_id}/actions`` (LC-LAZ-09, VIG-223)."""
    evidence: EvidenceService | None = None
    """``POST /loop/actions/{action_id}/evidence`` (LC-LAZ-09, VIG-223); los adjuntos los
    verifica ``ClosureAttachmentPort`` de ``app.state`` (TASK-310)."""
    closure: ClosureService | None = None
    """``POST /loop/findings/{finding_state_id}/closure`` (LC-LAZ-09, VIG-233)."""
    reopening: ReopeningService | None = None
    """``POST /loop/findings/{finding_state_id}/reopening`` (LC-LAZ-09, VIG-233)."""
    assistant: AssistantService | None = None
    """Las dos rutas del asistente de causa raíz (LC-LAZ-13, VIG-224); ningún adaptador del
    proveedor: la solicitud la atiende ``assistant_worker`` en ``vigia-worker``."""
    inbox: InboxService | None = None
    """``GET /loop/findings`` (LC-LAZ-05, VIG-222)."""
    detail: FindingDetailService | None = None
    """``GET /loop/findings/{finding_state_id}`` y ``.../classifications`` (LC-LAZ-06, VIG-232)."""
    review_queue: ReviewQueueService | None = None
    """``GET /loop/review-queue`` y ``POST .../resolution`` (LC-LAZ-08, VIG-213)."""
    overdue: OverdueService | None = None
    """``GET /loop/overdue`` (LC-LAZ-11, VIG-229)."""
    vocabulary: VocabularyService | None = None
    """``GET /loop/vocabulary`` y ``POST /loop/vocabulary/changes`` (LC-LAZ-17, VIG-220)."""
    unclosable: UnclosableService | None = None
    """``POST /loop/findings/{finding_state_id}/unclosable`` y
    ``POST /loop/unclosable/{declaration_id}/reviews`` (LC-LAZ-09, VIG-228)."""


def installed[T](service: T | None) -> T:
    """El servicio de la ruta; sin él, ``internal_error`` (nunca deja pasar)."""
    if service is None:
        _log.error("la ruta del lazo no tiene su servicio instalado")
        raise ApiError(ApiErrorCode.INTERNAL_ERROR)
    return service


def loop_http(request: Request) -> LoopHttp:
    """Los servicios de la aplicación; sin ellos, ``internal_error``."""
    services = getattr(request.app.state, LOOP_STATE_KEY, None)
    if not isinstance(services, LoopHttp):
        _log.error("las rutas del lazo no tienen servicios instalados")
        raise ApiError(ApiErrorCode.INTERNAL_ERROR)
    return services


class _NoEvidenceStore:
    """Sin depósito de evidencias configurado: el detalle declara la evidencia no disponible
    (``evidence_url_unavailable``) y muestra la referencia del clip; nunca oculta el hallazgo."""

    async def head_object(self, key: str) -> ObjectHead | None:
        raise StorageUnavailable("head_object")

    async def presign_get(
        self, key: str, ttl: timedelta = PRESIGN_GET_MAX_TTL, *, version_id: str | None = None
    ) -> PresignedRequest:
        raise StorageUnavailable("presign_get")


def build_detail(services: UnitServices) -> FindingDetailService:
    """El detalle con sus puertos en proceso: ``LectorExpediente`` y ``EvidencePort`` de U-02,
    ``CatalogQueryPort``, ``GateQueryPort`` y ``FleetQueryPort`` de U-03 y ``CoveragePort``."""
    ports = catalog_query_ports(services.database)
    return FindingDetailService(
        database=services.database,
        authorizer=services.authorizer,
        reader=LectorExpediente(
            database=services.database,
            audit=services.audit,
            clock=services.clock,
            metrics=services.metrics,
        ),
        evidence=LedgerEvidenceService(
            database=services.database,
            audit=services.audit,
            storage=services.evidence if services.evidence is not None else _NoEvidenceStore(),
        ),
        catalog=ports.catalog,
        gates=ports.gates,
        fleet=FleetInventory(
            database=services.database,
            authorizer=services.authorizer,
            audit=services.audit,
            clock=services.clock,
            provider_organization_id=services.provider_organization_id,
        ),
        coverage=CoverageService(
            database=services.database,
            audit=services.audit,
            clock=services.clock,
            metrics=services.metrics,
        ),
    )


def build_loop_http(services: UnitServices) -> LoopHttp:
    """``LoopHttp`` sobre la infraestructura común de la raíz."""
    actions = ActionService(
        database=services.database,
        writer=services.writer,
        authorizer=services.authorizer,
        clock=services.clock,
    )
    return LoopHttp(
        configuration=LoopConfigurationService(
            repository=PostgresLoopConfigurationRepository(services.database),
            database=services.database,
            writer=services.writer,
            authorizer=services.authorizer,
            free_text=services.free_text,
            clock=services.clock,
        ),
        classification=ClassificationService(
            database=services.database,
            writer=services.writer,
            authorizer=services.authorizer,
            clock=services.clock,
        ),
        actions=actions,
        evidence=EvidenceService(
            database=services.database,
            writer=services.writer,
            authorizer=services.authorizer,
            clock=services.clock,
        ),
        closure=ClosureService(
            database=services.database,
            writer=services.writer,
            authorizer=services.authorizer,
            clock=services.clock,
        ),
        reopening=ReopeningService(
            database=services.database,
            writer=services.writer,
            authorizer=services.authorizer,
            clock=services.clock,
            actions=actions,
        ),
        root_cause=RootCauseService(
            database=services.database,
            writer=services.writer,
            authorizer=services.authorizer,
            free_text=services.free_text,
            catalog=catalog_query_ports(services.database).catalog,
            fleet=FleetInventory(
                database=services.database,
                authorizer=services.authorizer,
                audit=services.audit,
                clock=services.clock,
                provider_organization_id=services.provider_organization_id,
            ),
            coverage=CoverageService(
                database=services.database,
                audit=services.audit,
                clock=services.clock,
                metrics=services.metrics,
            ),
            clock=services.clock,
        ),
        assistant=AssistantService(
            database=services.database,
            authorizer=services.authorizer,
            outbox=services.outbox,
            clock=services.clock,
        ),
        inbox=InboxService(database=services.database, authorizer=services.authorizer),
        detail=build_detail(services),
        review_queue=ReviewQueueService(
            database=services.database,
            writer=services.writer,
            authorizer=services.authorizer,
            repository=ReviewQueueRepository(services.database),
            clock=services.clock,
        ),
        overdue=OverdueService(
            database=services.database,
            authorizer=services.authorizer,
            configuration=PostgresLoopConfigurationRepository(services.database),
            clock=services.clock,
        ),
        vocabulary=VocabularyService(
            repository=PostgresVocabularyRepository(services.database),
            database=services.database,
            writer=services.writer,
            authorizer=services.authorizer,
            clock=services.clock,
        ),
        unclosable=UnclosableService(
            database=services.database,
            writer=services.writer,
            authorizer=services.authorizer,
            clock=services.clock,
            gates=catalog_query_ports(services.database).gates,
            actions=actions,
        ),
    )


def loop_http_state(services: UnitServices) -> Mapping[str, object]:
    """La entrada de ``app.state`` de la unidad (``API_STATE``)."""
    return {LOOP_STATE_KEY: build_loop_http(services)}
