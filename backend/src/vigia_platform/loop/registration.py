"""Registro de U-04 en la raíz de composición (TASK-302; LC-LAZ-37; NFR-LAZ-14; A-52).

``shared.runtime.units`` declara las tres unidades de U-04 (``loop``, ``accreditation`` y
``notifications``) y las hace delegar en el ``registration.py`` de cada paquete; **ninguna tarea
posterior de U-04 edita ``units.py``**. Cada tarea de servicio **añade una línea** al
``registration.py`` de su módulo: aquí, a ``ROUTERS``, ``LOOP_TASKS`` y ``TASK_REGISTRARS``,
``LOOP_CONSUMERS`` y ``CONSUMER_REGISTRARS``, ``FREE_TEXT_REGISTRARS`` (los dos validadores de
TASK-303 y TASK-340) o ``API_STATE``.

**Comprobación de arranque con fallo cerrado** (``missing_u04_registrations``, NFR-LAZ-14,
PAT-LAZ-RES-07): la composición contrasta lo registrado por **todas** las unidades con los
manifiestos de U-04, escritos **a mano** (no derivados de los registros):

- ``U04_RECORD_TYPES``: los 17 tipos de §5.1 (13 del lazo y 4 de la acreditación);
- ``U04_EVENT_TYPES``: los 25 eventos de §11.3 (nota R14 de §7: 25, no 20; 21 del lazo y 4 de
  la acreditación; ``user_invited`` no es de U-04, U04-H-13) y el trabajo interno
  ``export_requested`` del consumidor ``export_worker`` (TASK-317);
- ``U04_TASKS``: las tareas periódicas con su cadencia e iteración, todas con ``unit = U-04``;
- ``U04_CONSUMERS``: los consumidores con su concurrencia, todos con ``unit = U-04``;
- ``U04_FREE_TEXT_VALIDATORS``: los validadores de texto libre (BLM §9: sin ellos U-04 **no
  arranca**). Los mira ``loop_tasks`` sobre la política ya sellada de ``UnitServices``, porque
  ``required`` solo recibe los tipos y el catálogo (``units.py`` no se edita).

Si falta uno, o una tarea cambió de cadencia, unidad o iteración, ``vigia-api``,
``vigia-worker`` y ``vigia-admin`` no arrancan y la salida nombra lo que falta
(``RegistrationIncomplete``). Las enumeraciones con etiqueta las comprueba el arranque de
``vigia-api`` contra ``labels.loop.es.json`` (``PlatformUnit.labels``, NFR-LAZ-52).

**Transversales** (TASK-349), que las tareas de servicio usan sin construirlos por su cuenta:

- ``loop_consumers`` (y los de ``accreditation`` y ``notifications``) entregan a sus registradores
  ``bulkheaded_registry``: cada consumidor de U-04 sale con su mamparo (LC-LAZ-40), que el
  despachador comprueba antes del bloqueo de la partición;
- ``loop_http_client_factory(services)``: la fábrica única de clientes de salida con los dos
  destinos de la configuración (LC-LAZ-39);
- ``loop_metrics()``: los instrumentos de ``loop.observability.metrics`` (LC-LAZ-38).

**Asistente** (TASK-344): ``assistant_provider(services)`` elige en la primera entrega el adaptador
real ``loop.adapters.anthropic`` **solo si el proceso tiene la clave** del proveedor; sin ella,
``FakeAssistantProvider.unconfigured()``.
"""

from __future__ import annotations

import enum
import importlib
import os
from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import TYPE_CHECKING, Final

from fastapi import APIRouter

from vigia_platform.accreditation.registration import (
    ACCREDITATION_CONSUMERS,
    ACCREDITATION_TASKS,
)
from vigia_platform.catalog.adapters.postgres.query_ports import catalog_query_ports
from vigia_platform.ledger.application.coverage import CoverageService
from vigia_platform.ledger.application.evidence_read import EvidenceService
from vigia_platform.ledger.application.reader import LectorExpediente
from vigia_platform.loop.adapters.attachments.repository import PostgresAttachmentRepository
from vigia_platform.loop.adapters.http.actions_routes import actions_router
from vigia_platform.loop.adapters.http.assistant_routes import assistant_router
from vigia_platform.loop.adapters.http.attachment_routes import (
    ATTACHMENTS_STATE_KEY,
    attachment_router,
)
from vigia_platform.loop.adapters.http.classification_routes import classification_router
from vigia_platform.loop.adapters.http.closure_routes import closure_router
from vigia_platform.loop.adapters.http.configuration_routes import configuration_router
from vigia_platform.loop.adapters.http.findings_routes import findings_router
from vigia_platform.loop.adapters.http.overdue_routes import overdue_router
from vigia_platform.loop.adapters.http.review_routes import review_queue_router
from vigia_platform.loop.adapters.http.root_cause_routes import root_cause_router
from vigia_platform.loop.adapters.http.services import loop_http_state
from vigia_platform.loop.adapters.http.unclosable_routes import unclosable_router
from vigia_platform.loop.adapters.postgres.declared_names import PostgresDeclaredNamesStore
from vigia_platform.loop.adapters.postgres.review_queue_repository import ReviewQueueRepository
from vigia_platform.loop.adapters.postgres.vocabulary import PostgresVocabularyStore
from vigia_platform.loop.application.attachments import build_attachment_service
from vigia_platform.loop.application.deadlines import (
    EVALUATE_DEADLINES_SCHEDULE,
    EVALUATE_DEADLINES_TASK,
    DeadlineEvaluator,
    register_evaluate_deadlines,
)
from vigia_platform.loop.application.review_queue import (
    REVIEW_QUEUE_CONSUMER,
    REVIEW_QUEUE_CONSUMER_CONCURRENCY,
    REVIEW_REMINDERS_SCHEDULE,
    REVIEW_REMINDERS_TASK,
    ReviewQueueConsumer,
    ReviewQueueReminder,
    register_review_queue,
    register_review_reminders,
)
from vigia_platform.loop.application.unclosable import (
    SCHEDULE as UNCLOSABLE_SCHEDULE,
)
from vigia_platform.loop.application.unclosable import (
    TASK_NAME as UNCLOSABLE_REVIEW_DUE,
)
from vigia_platform.loop.application.unclosable import (
    UnclosableService,
    register_unclosable_review_due,
)
from vigia_platform.loop.assistant.consumer import (
    ASSISTANT_WORKER,
    ASSISTANT_WORKER_CONCURRENCY,
    AssistantWorker,
    register_assistant_worker,
)
from vigia_platform.loop.assistant.context import AssistantContext
from vigia_platform.loop.assistant.port import (
    AssistantOutcome,
    AssistantProviderPort,
    provider_credentials_present,
)
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.detail_codes import LOOP_DETAIL_CODE_LABEL_BINDINGS, LoopDetailCode
from vigia_platform.loop.domain.enums import LOOP_LABEL_BINDINGS
from vigia_platform.loop.events import register_loop_event_types
from vigia_platform.loop.http.client_factory import HttpClientFactory
from vigia_platform.loop.observability.metrics import LoopMetrics, get_loop_metrics
from vigia_platform.loop.projection.consumer import (
    LOOP_PROJECTION,
    LOOP_PROJECTION_CONCURRENCY,
    LoopProjectionConsumer,
    register_loop_projection,
)
from vigia_platform.loop.projection.enrichment import ProjectionEnrichment
from vigia_platform.loop.projection.rebuild import ProjectionRebuilder
from vigia_platform.loop.projection.store import LoopProjectionStore
from vigia_platform.loop.projection.verify import (
    VERIFY_LOOP_PROJECTION,
    VERIFY_LOOP_PROJECTION_SCHEDULE,
    ProjectionVerifier,
    register_verify_loop_projection,
    verify_loop_projection_handler,
)
from vigia_platform.loop.record_types import register_loop_record_types
from vigia_platform.loop.validators.attribution import (
    PERSON_ATTRIBUTION,
    PersonAttributionValidator,
    common_words,
)
from vigia_platform.loop.validators.cache import DeclaredNamesCache, VocabularyCache
from vigia_platform.loop.validators.vocabulary import (
    BLOCKED_VOCABULARY,
    BlockedVocabularyValidator,
    initial_vocabulary,
)
from vigia_platform.loop.worker.bulkheads import bulkheaded_registry
from vigia_platform.notifications.registration import (
    NOTIFICATIONS_CONSUMERS,
    NOTIFICATIONS_TASKS,
)
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.outbox.registries import (
    ConsumerRegistry,
    EventTypeRegistry,
    OutboxCatalog,
    PeriodicTaskRegistry,
    Schedule,
    TaskIteration,
)
from vigia_platform.shared.runtime.config import RegistrationIncomplete
from vigia_platform.shared.storage import PRESIGN_GET_MAX_TTL, ObjectHead, PresignedRequest

if TYPE_CHECKING:
    from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
    from vigia_platform.ledger.registry import RecordTypeRegistry
    from vigia_platform.shared.runtime.units import UnitServices

__all__ = [
    "API_STATE",
    "CONSUMER_REGISTRARS",
    "FREE_TEXT_REGISTRARS",
    "LOOP_CONSUMERS",
    "LOOP_DETAIL_CODES",
    "LOOP_LABELS",
    "LOOP_TASKS",
    "ROUTERS",
    "TASK_REGISTRARS",
    "U04_CONSUMERS",
    "U04_EVENT_TYPES",
    "U04_FREE_TEXT_VALIDATORS",
    "U04_RECORD_TYPES",
    "U04_TASKS",
    "assistant_provider",
    "loop_api_state",
    "loop_consumers",
    "loop_event_types",
    "loop_free_text",
    "loop_http_client_factory",
    "loop_metrics",
    "loop_record_types",
    "loop_routers",
    "loop_tasks",
    "missing_free_text_validators",
    "missing_u04_registrations",
    "register_blocked_vocabulary",
    "register_person_attribution",
]

U04_RECORD_TYPES: Final = frozenset(
    {
        # loop (13): los once reservados por U-02 que son del lazo y los seis propuestos
        "classification",
        "review_resolution",
        "action_assigned",
        "action_evidence_added",
        "closure",
        "unclosable_state_declared",
        "root_cause_analysis",
        "vocabulary_list_changed",
        "action_overdue",
        "unclosable_review",
        "reopening",
        "loop_configuration_changed",
        "late_segment_attached",
        # accreditation (4)
        "export_produced",
        "monthly_package_produced",
        "sample_audit_result",
        "commitments_recorded",
    }
)
"""Los 17 tipos de registro de U-04 (domain-entities §5.1)."""

U04_EVENT_TYPES: Final = frozenset(
    {
        # loop (21)
        "finding_confirmed",
        "finding_discarded",
        "finding_reclassified",
        "loop_suspended",
        "loop_resumed",
        "review_confirmed",
        "review_discarded",
        "review_overdue",
        "action_assigned",
        "action_evidence_added",
        "finding_closed",
        "finding_unclosable",
        "unclosable_review_due",
        "unclosable_reviewed",
        "finding_reopened",
        "finding_overdue",
        "finding_escalated",
        "root_cause_analysis_signed",
        "vocabulary_list_changed",
        "late_segment_attached",
        "loop_projection_divergence",
        # accreditation (4)
        "export_available",
        "monthly_package_available",
        "commitments_recorded",
        "adoption_low",
        # interno (TASK-317): el trabajo del consumidor export_worker, fuera de §11.3
        "export_requested",
        # interno (TASK-316): el trabajo del consumidor assistant_worker, fuera de §11.3
        "assistant_requested",
        # interno (TASK-325): el pulso que dispara email_delivery_events, fuera de §11.3
        "email_delivery_events_poll",
    }
)
"""Los 25 eventos de U-04 (domain-entities §11.3 y nota R14 de §7) y los trabajos internos
``export_requested`` (TASK-317), ``assistant_requested`` (TASK-316) y
``email_delivery_events_poll`` (TASK-325)."""

type TaskRegistrar = Callable[[PeriodicTaskRegistry, UnitServices], None]
type ConsumerRegistrar = Callable[[ConsumerRegistry, UnitServices], None]
type FreeTextRegistrar = Callable[[FreeTextPolicyRegistry], None]
type StateBuilder = Callable[[UnitServices], Mapping[str, object]]

ROUTERS: Final[tuple[Callable[[], APIRouter], ...]] = (
    attachment_router,  # SUP-U05-09 y 10 (TASK-310)
    configuration_router,  # SUP-U05-38 y 39 (TASK-304)
    classification_router,  # SUP-U05-03 (TASK-308)
    review_queue_router,  # SUP-U05-05 y 06 (TASK-309)
    actions_router,  # SUP-U05-07 y 08 (TASK-311)
    closure_router,  # SUP-U05-11 y 14 (TASK-343, VIG-233)
    root_cause_router,  # SUP-U05-16 a 18 (TASK-315)
    assistant_router,  # SUP-U05-19 y 20 (TASK-316)
    findings_router,  # SUP-U05-01 (TASK-307), 02 y 04 (TASK-342)
    overdue_router,  # SUP-U05-15 (TASK-313)
    unclosable_router,  # SUP-U05-12 y 13 (TASK-312)
)
"""Fábricas de los enrutadores de ``/loop/*``."""


def _register_review_reminders(registry: PeriodicTaskRegistry, services: UnitServices) -> None:
    """``review_reminders`` (TASK-309): un recordatorio por ventana, sin sacar nada de la cola."""
    register_review_reminders(
        registry,
        ReviewQueueReminder(
            repository=ReviewQueueRepository(services.database),
            configuration=PostgresLoopConfigurationRepository(services.database),
            outbox=services.outbox,
            clock=services.clock,
        ),
    )


def _register_evaluate_deadlines(registry: PeriodicTaskRegistry, services: UnitServices) -> None:
    """``evaluate_deadlines`` (TASK-313): vencimientos por nivel, sin cerrar ni ocultar nada."""
    register_evaluate_deadlines(
        registry,
        DeadlineEvaluator(
            writer=services.writer,
            clock=services.clock,
            configuration=PostgresLoopConfigurationRepository(services.database),
        ),
    )


def _register_unclosable_review_due(registry: PeriodicTaskRegistry, services: UnitServices) -> None:
    """``unclosable_review_due`` (TASK-312) con la bandeja de salida real y el puerto de la
    política de planta de U-03. Declara hechos de plazo: no escribe el expediente."""
    register_unclosable_review_due(
        registry,
        UnclosableService(
            database=services.database,
            writer=services.writer,
            authorizer=services.authorizer,
            clock=services.clock,
            gates=catalog_query_ports(services.database).gates,
            outbox=services.outbox,
        ),
    )


class _NoEvidenceReads:
    """``vigia-worker`` sin depósito de evidencias: ``loop_projection`` solo usa
    ``resultados_marca``, que lee la base; una lectura de clip aquí es un defecto."""

    async def head_object(self, key: str) -> ObjectHead | None:
        raise RuntimeError("loop_projection no lee clips")

    async def presign_get(
        self, key: str, ttl: timedelta = PRESIGN_GET_MAX_TTL, *, version_id: str | None = None
    ) -> PresignedRequest:
        raise RuntimeError("loop_projection no lee clips")


def _register_loop_projection(registry: ConsumerRegistry, services: UnitServices) -> None:
    """``loop_projection`` (TASK-341) con los puertos de U-03 y de U-02 en proceso."""
    ports = catalog_query_ports(services.database)
    reader = LectorExpediente(
        database=services.database,
        audit=services.audit,
        clock=services.clock,
        metrics=services.metrics,
    )
    enrichment = ProjectionEnrichment(
        catalog=ports.catalog,
        gates=ports.gates,
        coverage=CoverageService(
            database=services.database,
            audit=services.audit,
            clock=services.clock,
            metrics=services.metrics,
        ),
        markers=EvidenceService(
            database=services.database,
            audit=services.audit,
            storage=services.evidence if services.evidence is not None else _NoEvidenceReads(),
        ),
        settings=PostgresLoopConfigurationRepository(services.database),
    )
    register_loop_projection(
        registry,
        LoopProjectionConsumer(
            store=LoopProjectionStore(),
            reader=reader,
            enrichment=enrichment,
            writer=services.writer,
            clock=services.clock,
        ),
    )


def _register_verify_loop_projection(
    registry: PeriodicTaskRegistry, services: UnitServices
) -> None:
    """``verify_loop_projection`` (TASK-306, diaria por organización; LC-LAZ-04).

    ``rebuild_loop_projection`` (LC-LAZ-03) corre a demanda, sin cadencia que vigilar (U04-H-14,
    ``infrastructure-design.md``): no entra en ``LOOP_TASKS``. Esta verificación construye su
    propio ``ProjectionRebuilder`` para disparar la reconstrucción de la partición que diverge; la
    orden administrativa de VIG-241 (``vigia-admin rebuild-loop-projection``) construye el suyo por
    su cuenta, con los mismos puertos."""
    ports = catalog_query_ports(services.database)
    rebuilder = ProjectionRebuilder(
        database=services.database,
        catalog=ports.catalog,
        gates=ports.gates,
        coverage=CoverageService(
            database=services.database,
            audit=services.audit,
            clock=services.clock,
            metrics=services.metrics,
        ),
        markers=EvidenceService(
            database=services.database,
            audit=services.audit,
            storage=services.evidence if services.evidence is not None else _NoEvidenceReads(),
        ),
        settings=PostgresLoopConfigurationRepository(services.database),
        clock=services.clock,
    )
    verifier = ProjectionVerifier(
        database=services.database,
        rebuilder=rebuilder,
        outbox=services.outbox,
        clock=services.clock,
    )
    register_verify_loop_projection(registry, verify_loop_projection_handler(verifier))


LOOP_TASKS: Final[Mapping[str, tuple[Schedule, TaskIteration]]] = {
    VERIFY_LOOP_PROJECTION: (VERIFY_LOOP_PROJECTION_SCHEDULE, TaskIteration.PER_ORGANIZATION),
    REVIEW_REMINDERS_TASK: (REVIEW_REMINDERS_SCHEDULE, TaskIteration.PER_ORGANIZATION),  # TASK-309
    EVALUATE_DEADLINES_TASK: (EVALUATE_DEADLINES_SCHEDULE, TaskIteration.PER_ORGANIZATION),  # 313
    UNCLOSABLE_REVIEW_DUE: (UNCLOSABLE_SCHEDULE, TaskIteration.PER_ORGANIZATION),  # TASK-312
}
"""Tareas periódicas del lazo: nombre → (cadencia, iteración), todas con ``unit = U-04``.

``rebuild_loop_projection`` (LC-LAZ-03) no está aquí: corre a demanda, sin cadencia (U04-H-14)."""
TASK_REGISTRARS: Final[tuple[TaskRegistrar, ...]] = (
    _register_verify_loop_projection,
    _register_review_reminders,  # review_reminders (TASK-309)
    _register_evaluate_deadlines,  # evaluate_deadlines (TASK-313)
    _register_unclosable_review_due,  # unclosable_review_due (TASK-312)
)


def _unconfigured_assistant() -> AssistantProviderPort:
    fake = importlib.import_module("vigia_platform.loop.adapters.assistant.fake")
    provider: AssistantProviderPort = fake.FakeAssistantProvider.unconfigured()
    return provider


def assistant_provider(
    services: UnitServices, environ: Mapping[str, str] | None = None
) -> AssistantProviderPort:
    """El adaptador del asistente según la configuración del proceso (TASK-344).

    Con la clave del proveedor en el entorno (``ANTHROPIC_API_KEY`` o ``ANTHROPIC_AUTH_TOKEN``,
    que Secrets Manager inyecta **solo** en ``vigia-worker``, NFR-LAZ-26),
    ``AnthropicAssistantProvider`` con el modelo de ``VIGIA_ASSISTANT_MODEL`` y la fábrica de
    U-04 (LC-LAZ-39); sin clave, ``FakeAssistantProvider.unconfigured()``. Los dos módulos se
    importan aquí, nunca al cargar este archivo.
    """
    environ = os.environ if environ is None else environ
    if not provider_credentials_present(environ):
        return _unconfigured_assistant()
    adapter = importlib.import_module("vigia_platform.loop.adapters.anthropic.client")
    config = services.config
    model = (
        config.assistant_model
        if config is not None and config.assistant_model is not None
        else adapter.DEFAULT_MODEL
    )
    provider: AssistantProviderPort = adapter.AnthropicAssistantProvider(
        loop_http_client_factory(services),
        model=model,
        api_key=environ.get("ANTHROPIC_API_KEY", "").strip() or None,
        auth_token=environ.get("ANTHROPIC_AUTH_TOKEN", "").strip() or None,
    )
    return provider


class AssistantProviderOnFirstUse:
    """El adaptador del asistente, elegido y construido en la **primera entrega**.

    Solo ``vigia-worker`` entrega eventos: ``vigia-api`` compone el consumidor con el resto del
    catálogo pero **nunca importa ni construye un adaptador** del proveedor (NFR-LAZ-26, LC-LAZ-14).
    ``build`` lo elige una sola vez por proceso (``assistant_provider``: el real con clave,
    TASK-344); sin ``build``, ``FakeAssistantProvider.unconfigured()``: el consumidor responde
    ``no_api_key`` sin llamar (BR-LAZ-74).
    """

    def __init__(self, build: Callable[[], AssistantProviderPort] | None = None) -> None:
        self._build = build if build is not None else _unconfigured_assistant
        self._provider: AssistantProviderPort | None = None

    def _resolved(self) -> AssistantProviderPort:
        if self._provider is None:
            self._provider = self._build()
        return self._provider

    @property
    def resolved(self) -> bool:
        """Si ya se eligió el adaptador (``False`` en ``vigia-api``)."""
        return self._provider is not None

    @property
    def configured(self) -> bool:
        return self._resolved().configured

    async def complete(self, context: AssistantContext, *, timeout: timedelta) -> AssistantOutcome:
        return await self._resolved().complete(context, timeout=timeout)


def _register_assistant_worker(registry: ConsumerRegistry, services: UnitServices) -> None:
    """``assistant_worker`` (TASK-316) con los dos validadores de la política sellada."""
    register_assistant_worker(
        registry,
        AssistantWorker(
            database=services.database,
            provider=AssistantProviderOnFirstUse(lambda: assistant_provider(services)),
            free_text=services.free_text,
            clock=services.clock,
        ),
    )


def _register_review_queue(registry: ConsumerRegistry, services: UnitServices) -> None:
    """``review_queue`` (TASK-309): sin dependencia externa, lee del expediente en proceso."""
    register_review_queue(
        registry,
        ReviewQueueConsumer(
            store=LoopProjectionStore(),
            reader=LectorExpediente(
                database=services.database, audit=services.audit, clock=services.clock
            ),
            repository=ReviewQueueRepository(services.database),
            configuration=PostgresLoopConfigurationRepository(services.database),
        ),
    )


LOOP_CONSUMERS: Final[Mapping[str, int]] = {
    LOOP_PROJECTION: LOOP_PROJECTION_CONCURRENCY,  # TASK-341
    ASSISTANT_WORKER: ASSISTANT_WORKER_CONCURRENCY,  # TASK-316
    REVIEW_QUEUE_CONSUMER: REVIEW_QUEUE_CONSUMER_CONCURRENCY,  # TASK-309
}
"""Consumidores del lazo: nombre → concurrencia de su mamparo (LC-LAZ-40)."""
CONSUMER_REGISTRARS: Final[tuple[ConsumerRegistrar, ...]] = (
    _register_loop_projection,  # loop_projection (TASK-341)
    _register_assistant_worker,  # assistant_worker (TASK-316)
    _register_review_queue,  # review_queue (TASK-309)
)


def register_blocked_vocabulary(registry: FreeTextPolicyRegistry) -> None:
    """``blocked_vocabulary`` (TASK-303) con su caché por proceso y su preparador.

    El preparador comprueba la versión de la lista al abrir cada escritura (PAT-LAZ-REN-02); el
    validador solo lee la caché. La lista inicial se compila aquí, al arrancar: un error en la tabla
    de flexiones o en un patrón detiene el arranque, no la primera escritura.
    """
    initial_vocabulary()
    cache = VocabularyCache(PostgresVocabularyStore())
    registry.register(BLOCKED_VOCABULARY, BlockedVocabularyValidator(cache.compiled_for))
    registry.register_preparer(BLOCKED_VOCABULARY, cache.refresh)


def register_person_attribution(registry: FreeTextPolicyRegistry) -> None:
    """``person_attribution`` (TASK-340) con la caché del vocabulario declarado y su preparador.

    El preparador relee los nombres de plantas y zonas de la organización al abrir la escritura
    (fuera de la ruta de validación); el validador solo lee la caché. Las palabras comunes se
    cargan aquí, al arrancar: un archivo roto detiene el arranque, no la primera escritura.
    """
    common_words()
    cache = DeclaredNamesCache(PostgresDeclaredNamesStore())
    registry.register(PERSON_ATTRIBUTION, PersonAttributionValidator(cache.names_for))
    registry.register_preparer(PERSON_ATTRIBUTION, cache.refresh)


FREE_TEXT_REGISTRARS: Final[tuple[FreeTextRegistrar, ...]] = (
    register_blocked_vocabulary,  # TASK-303
    register_person_attribution,  # TASK-340
)
"""Los validadores de texto libre de U-04, en este orden."""

U04_FREE_TEXT_VALIDATORS: Final = frozenset({BLOCKED_VOCABULARY, PERSON_ATTRIBUTION})
"""Los validadores que U-04 exige en la política sellada (BLM §9; NFR-LAZ-14)."""


def _attachments_state(services: UnitServices) -> Mapping[str, object]:
    """``ClosureAttachmentPort`` sobre ``vigia-evidence`` (prefijo ``closure/``, LC-LAZ-10)."""
    return {
        ATTACHMENTS_STATE_KEY: build_attachment_service(
            repository=PostgresAttachmentRepository(services.database),
            storage=services.require_evidence(),
            authorizer=services.authorizer,
            audit=services.audit,
            clock=services.clock,
        )
    }


API_STATE: Final[tuple[StateBuilder, ...]] = (
    _attachments_state,  # ClosureAttachmentPort (TASK-310)
    loop_http_state,  # LoopHttp (TASK-304)
)

U04_TASKS: Final[Mapping[str, tuple[Schedule, TaskIteration]]] = {
    **LOOP_TASKS,
    **ACCREDITATION_TASKS,
    **NOTIFICATIONS_TASKS,
}
"""Todas las tareas periódicas de U-04: ``monthly_package`` (TASK-342 a 344),
``verify_loop_projection`` (TASK-306) y ``unclosable_review_due`` (TASK-312); TASK-326 verifica
las diez."""

U04_CONSUMERS: Final[Mapping[str, int]] = {
    **LOOP_CONSUMERS,
    **ACCREDITATION_CONSUMERS,
    **NOTIFICATIONS_CONSUMERS,
}
"""Todos los consumidores de U-04 con su concurrencia."""

LOOP_LABELS: Final[Mapping[str, type[enum.Enum]]] = {
    **LOOP_LABEL_BINDINGS,
    **LOOP_DETAIL_CODE_LABEL_BINDINGS,
}
"""Enumeraciones con etiqueta de la unidad (``PlatformUnit.labels``)."""

LOOP_DETAIL_CODES: Final[tuple[str, ...]] = tuple(code.value for code in LoopDetailCode)


def missing_u04_registrations(
    record_types: RecordTypeRegistry, catalog: OutboxCatalog
) -> tuple[tuple[str, str], ...]:
    """Lo que falta de U-04 en los registros ya compuestos, como pares (clase, nombre).

    Un tipo o un evento de U-04 registrado por otra unidad cuenta como ausente, igual que una
    tarea con otra cadencia, otra unidad u otra iteración, o un consumidor de otra unidad.
    """
    present_types = {
        compiled.record_type
        for compiled in record_types.latest()
        if compiled.writer_unit is ActorUnit.U04
    }
    missing = [("record_type", name) for name in sorted(U04_RECORD_TYPES - present_types)]
    present_events = {
        compiled.event_name
        for compiled in catalog.event_types.compiled_types()
        if compiled.definition.publisher_unit is ActorUnit.U04
    }
    missing += [("event_type", name) for name in sorted(U04_EVENT_TYPES - present_events)]
    for name, (schedule, iteration) in sorted(U04_TASKS.items()):
        task = catalog.periodic_tasks.get(name)
        if (
            task is None
            or task.unit is not ActorUnit.U04
            or task.schedule != schedule
            or task.iteration is not iteration
        ):
            missing.append(("periodic_task", name))
    for name in sorted(U04_CONSUMERS):
        consumer = catalog.consumers.get(name)
        if consumer is None or consumer.unit is not ActorUnit.U04:
            missing.append(("consumer", name))
    return tuple(missing)


def loop_routers() -> tuple[APIRouter, ...]:
    return tuple(build() for build in ROUTERS)


def loop_record_types(registry: RecordTypeRegistry) -> None:
    register_loop_record_types(registry)


def loop_event_types(registry: EventTypeRegistry) -> None:
    register_loop_event_types(registry)


def loop_free_text(registry: FreeTextPolicyRegistry) -> None:
    for register in FREE_TEXT_REGISTRARS:
        register(registry)


def missing_free_text_validators(services: UnitServices) -> tuple[tuple[str, str], ...]:
    """Los validadores de ``U04_FREE_TEXT_VALIDATORS`` que la política sellada no tiene."""
    present = set(services.free_text.validator_names)
    return tuple(
        ("free_text_validator", name) for name in sorted(U04_FREE_TEXT_VALIDATORS - present)
    )


def loop_tasks(registry: PeriodicTaskRegistry, services: UnitServices) -> None:
    # Arranque cerrado (BLM §9): sin los validadores de U-04 no arranca ningún proceso. Esta es la
    # primera llamada de la composición que ve la política ya sellada.
    missing = missing_free_text_validators(services)
    if missing:
        raise RegistrationIncomplete(missing)
    for register in TASK_REGISTRARS:
        register(registry, services)


def loop_consumers(registry: ConsumerRegistry, services: UnitServices) -> None:
    # Cada consumidor sale con su mamparo (LC-LAZ-40); el registrador no lo conoce.
    bulkheaded = bulkheaded_registry(registry, services)
    for register in CONSUMER_REGISTRARS:
        register(bulkheaded, services)


def loop_http_client_factory(services: UnitServices) -> HttpClientFactory:
    """La fábrica única de clientes de salida de U-04 (LC-LAZ-39), con los destinos de
    ``services.config`` (los del diseño si no hay configuración)."""
    return HttpClientFactory.from_config(services.config)


def loop_metrics() -> LoopMetrics:
    """Los instrumentos de U-04 sobre el medidor de la plataforma (LC-LAZ-38)."""
    return get_loop_metrics()


def loop_api_state(services: UnitServices) -> Mapping[str, object]:
    state: dict[str, object] = {}
    for build in API_STATE:
        for key, value in build(services).items():
            if key in state:
                raise ValueError(f"la clave de app.state «{key}» está dos veces en loop")
            state[key] = value
    return state
