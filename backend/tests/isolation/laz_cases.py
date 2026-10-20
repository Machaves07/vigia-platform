"""Casos de aislamiento por ruta de U-04 (TASK-302; PR-LAZ-62 parcial; LC-LAZ-43).

``test_route_isolation.py`` concatena ``LAZ_CASES`` a su ``CASES``, y ``uncovered`` exige un caso
por cada ruta registrada: una ruta de ``/loop/*``, ``/accreditation/*`` o ``/notifications/*``
sin su caso aquí deja la prueba en rojo. **Ninguna tarea de U-04 edita
``test_route_isolation.py``**: cada tarea que registra una ruta añade aquí su caso, con los tipos
de ``tests.isolation.route_cases``::

    (("GET", "/loop/findings/{finding_state_id}"), Case(Kind.RESOURCE, call=...)),

y, si la ruta necesita servicios en ``app.state``, su constructor en ``LAZ_STATE`` (sobre
``LazServices``, la infraestructura real de la prueba) y los eventos que publica en
``LAZ_EVENT_REGISTRARS`` (TASK-317 añadió los dos ganchos; la prueba los consume una vez).

``LAZ_DENIED_UNDER_CONCESSION`` (TASK-304) son las rutas cuya clave está en la columna
``provider_installer`` pero cuyo servicio deniega bajo concesión (``not_found`` tras su
``provider_query``, con ``authorization_denied`` en la auditoría del cliente). ``GET
/loop/plants/{plant_id}/configuration`` es la primera: ``hierarchy.read`` la tienen todos los
roles, también el instalador, y la configuración del lazo no es de la puesta en marcha (VIG-205).
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

from tests.isolation.route_cases import Call, Case, Kind
from vigia_platform.accreditation.adapters.http.monthly_routes import MonthlyPackageReads
from vigia_platform.accreditation.adapters.http.services import (
    ACCREDITATION_STATE_KEY,
    AccreditationHttp,
)
from vigia_platform.accreditation.adoption.reads import AdoptionReads
from vigia_platform.accreditation.adoption.task import ProductiveZones
from vigia_platform.accreditation.events import register_accreditation_event_types
from vigia_platform.accreditation.export.download import ExportDownloads
from vigia_platform.accreditation.export.request import ExportRequests
from vigia_platform.accreditation.exposure.service import ExposureService
from vigia_platform.accreditation.monthly.audit import SampleAuditService
from vigia_platform.accreditation.monthly.commitments import CommitmentService
from vigia_platform.accreditation.monthly.repository import MonthlyPackageRepository
from vigia_platform.catalog.adapters.postgres.query_ports import catalog_query_ports
from vigia_platform.fleet.application.inventory_read import FleetInventory
from vigia_platform.identity.authz.authorize import Authorizer
from vigia_platform.ledger.application.audit_writer import AuditWriter
from vigia_platform.ledger.application.coverage import CoverageService
from vigia_platform.ledger.application.writer import EscritorExpediente
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.loop.adapters.http.services import LOOP_STATE_KEY, LoopHttp
from vigia_platform.loop.adapters.postgres.review_queue_repository import ReviewQueueRepository
from vigia_platform.loop.application.actions import ActionService
from vigia_platform.loop.application.classification import ClassificationService
from vigia_platform.loop.application.closure import ClosureService
from vigia_platform.loop.application.deadlines import OverdueService
from vigia_platform.loop.application.evidence import EvidenceService
from vigia_platform.loop.application.inbox import InboxService
from vigia_platform.loop.application.reopening import ReopeningService
from vigia_platform.loop.application.review_queue import ReviewQueueService
from vigia_platform.loop.application.root_cause import RootCauseService
from vigia_platform.loop.assistant.service import AssistantService
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.config.service import LoopConfigurationService
from vigia_platform.notifications.adapters.http.services import (
    NOTIFICATIONS_STATE_KEY,
    NotificationsHttp,
)
from vigia_platform.notifications.preferences import NotificationPreferences
from vigia_platform.notifications.reader import NotificationReader
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.outbox.publish import OutboxPort
from vigia_platform.shared.outbox.registries import EventTypeRegistry

__all__ = [
    "LAZ_CASES",
    "LAZ_DENIED_UNDER_CONCESSION",
    "LAZ_EVENT_REGISTRARS",
    "LAZ_STATE",
    "LazServices",
    "laz_state",
]

_CONFIGURATION: Final = "/loop/plants/{plant_id}/configuration"
_NO_PROVIDER: Final = uuid.UUID("00000000-0000-4000-8000-000000000315")
"""``FleetQueryPort.nodes_by_zone`` (lo único que usa el análisis) no mira la organización del
proveedor; la de esta prueba no se usa."""
_ANALYSIS_STEP: Final = {
    "statement": "La carretilla cruzó el pasillo peatonal con la estación de carga energizada"
}
_ANALYSIS_SIGNATURE: Final = {
    "professional_license": "LIC-SST-0001",
    "root_cause_category": "engineering",
    "root_cause_statement": "El trazado de la zona mezcla los flujos de carga y de paso",
    "conclusion": "Separar físicamente los flujos con un resguardo fijo en el acceso",
}


class _Database(Protocol):
    def transaction(
        self, context: ScopeContext
    ) -> contextlib.AbstractAsyncContextManager[Transaction]: ...


@dataclass(frozen=True)
class LazServices:
    """La infraestructura real del ``isolation`` con la que se construyen los servicios de U-04."""

    database: _Database
    authorizer: Authorizer
    audit: AuditWriter
    outbox: OutboxPort
    """Bandeja sobre un catálogo con los eventos de ``LAZ_EVENT_REGISTRARS``."""
    clock: Clock
    writer: EscritorExpediente
    """El escritor del expediente de la prueba (la configuración del lazo, TASK-304)."""
    free_text: FreeTextPolicyRegistry


def _loop(services: LazServices) -> Mapping[str, object]:
    """``LoopHttp`` con un servicio por grupo de rutas de ``/loop/*`` (una sola clave de estado)."""
    database: Any = services.database
    actions = ActionService(  # TASK-311
        database=database,
        writer=services.writer,
        authorizer=services.authorizer,
        clock=services.clock,
    )
    return {
        LOOP_STATE_KEY: LoopHttp(
            configuration=LoopConfigurationService(
                repository=PostgresLoopConfigurationRepository(database),
                database=database,
                writer=services.writer,
                authorizer=services.authorizer,
                free_text=services.free_text,
                clock=services.clock,
            ),
            classification=ClassificationService(  # TASK-308
                database=database,
                writer=services.writer,
                authorizer=services.authorizer,
                clock=services.clock,
            ),
            actions=actions,
            evidence=EvidenceService(  # TASK-311
                database=database,
                writer=services.writer,
                authorizer=services.authorizer,
                clock=services.clock,
            ),
            closure=ClosureService(  # VIG-233
                database=database,
                writer=services.writer,
                authorizer=services.authorizer,
                clock=services.clock,
            ),
            reopening=ReopeningService(  # VIG-233
                database=database,
                writer=services.writer,
                authorizer=services.authorizer,
                clock=services.clock,
                actions=actions,
            ),
            root_cause=RootCauseService(  # TASK-315
                database=database,
                writer=services.writer,
                authorizer=services.authorizer,
                free_text=services.free_text,
                catalog=catalog_query_ports(database).catalog,
                fleet=FleetInventory(
                    database=database,
                    authorizer=services.authorizer,
                    audit=services.audit,
                    clock=services.clock,
                    provider_organization_id=_NO_PROVIDER,
                ),
                coverage=CoverageService(
                    database=database, audit=services.audit, clock=services.clock
                ),
                clock=services.clock,
            ),
            assistant=AssistantService(  # TASK-316
                database=database,
                authorizer=services.authorizer,
                outbox=services.outbox,
                clock=services.clock,
            ),
            inbox=InboxService(database=database, authorizer=services.authorizer),  # TASK-307
            review_queue=ReviewQueueService(  # TASK-309
                database=database,
                writer=services.writer,
                authorizer=services.authorizer,
                repository=ReviewQueueRepository(database),
                clock=services.clock,
            ),
            overdue=OverdueService(  # TASK-313
                database=database,
                authorizer=services.authorizer,
                configuration=PostgresLoopConfigurationRepository(database),
                clock=services.clock,
            ),
        ),
    }


class _NoSigner:
    """Las descargas de B nunca llegan al almacén: responden ``not_found`` antes de firmar."""

    async def presign_attachment_get(self, *args: object, **kwargs: object) -> Any:
        raise AssertionError("una descarga fuera de alcance no firma ninguna URL")


def _any(value: object) -> Any:
    """La base de la prueba donde un repositorio pide la de producción (como ``_loop``)."""
    return value


def _accreditation(services: LazServices) -> Mapping[str, object]:
    database: Any = services.database
    return {
        ACCREDITATION_STATE_KEY: AccreditationHttp(
            exports=ExportRequests(
                database=services.database,
                authorizer=services.authorizer,
                audit=services.audit,
                outbox=services.outbox,
                clock=services.clock,
            ),
            downloads=ExportDownloads(  # TASK-345
                database=services.database,
                authorizer=services.authorizer,
                audit=services.audit,
                storage=_NoSigner(),
                clock=services.clock,
            ),
            monthly_packages=MonthlyPackageReads(  # TASK-344
                database=services.database,
                repository=MonthlyPackageRepository(database=services.database),
                authorizer=services.authorizer,
                audit=services.audit,
                storage=_NoSigner(),
            ),
            exposure=_exposure(services),  # TASK-322
            sample_audit=SampleAuditService(  # TASK-347
                database=database,
                writer=services.writer,
                authorizer=services.authorizer,
                clock=services.clock,
            ),
            commitments=CommitmentService(  # TASK-347
                database=database,
                writer=services.writer,
                authorizer=services.authorizer,
                clock=services.clock,
            ),
            adoption=AdoptionReads(  # TASK-323
                database=services.database,
                authorizer=services.authorizer,
                activations=ProductiveZones(),
                configuration=PostgresLoopConfigurationRepository(_any(services.database)),
                clock=services.clock,
            ),
        )
    }


def _exposure(services: LazServices) -> ExposureService:
    database: Any = services.database
    ports = catalog_query_ports(database)
    return ExposureService(
        database=database,
        authorizer=services.authorizer,
        catalog=ports.catalog,
        gates=ports.gates,
        coverage=CoverageService(database=database, audit=services.audit, clock=services.clock),
        settings=PostgresLoopConfigurationRepository(database),
        clock=services.clock,
    )


def _notifications(services: LazServices) -> Mapping[str, object]:
    """``NotificationsHttp`` (TASK-324). ``LazServices`` no lleva la proveedora y
    ``test_route_isolation.py`` no se edita: se toma la del ``Authorizer`` de la prueba."""
    provider: uuid.UUID = services.authorizer._provider_organization_id
    return {
        NOTIFICATIONS_STATE_KEY: NotificationsHttp(
            reader=NotificationReader(
                database=services.database,  # type: ignore[arg-type]
                authorizer=services.authorizer,
                clock=services.clock,
                provider_organization_id=provider,
            ),
            preferences=NotificationPreferences(
                database=services.database,  # type: ignore[arg-type]
                clock=services.clock,
                authorizer=services.authorizer,
                provider_organization_id=provider,
            ),
        )
    }


LAZ_STATE: tuple[Callable[[LazServices], Mapping[str, object]], ...] = (
    _loop,  # TASK-304, TASK-308 y TASK-315
    _accreditation,  # TASK-317
    _notifications,  # TASK-324
)
"""Constructores del ``app.state`` de las rutas de U-04 en la aplicación de la prueba."""

LAZ_EVENT_REGISTRARS: tuple[Callable[[EventTypeRegistry], None], ...] = (
    register_accreditation_event_types,
)
"""Los eventos que publican las rutas de U-04 (``export_requested``, TASK-317)."""


def laz_state(services: LazServices) -> dict[str, object]:
    state: dict[str, object] = {}
    for build in LAZ_STATE:
        state.update(build(services))
    return state


def _export_body(plant: Any, zone: Any) -> dict[str, Any]:
    return {
        "plant_id": str(plant),
        "zone_ids": [str(zone)],
        "period": {"from": "2026-09-01T00:00:00.000Z", "to": "2026-10-01T00:00:00.000Z"},
        "package_form": "without_clips",
    }


LAZ_CASES: list[tuple[tuple[str, str], Case]] = [
    # VIG-205 (TASK-304): configuración del lazo de la planta (SUP-U05-38 y 39).
    (
        ("GET", _CONFIGURATION),
        Case(Kind.RESOURCE, lambda i: Call("GET", f"/loop/plants/{i.plant}/configuration")),
    ),
    (
        ("PUT", _CONFIGURATION),
        Case(
            Kind.RESOURCE,
            lambda i: Call(
                "PUT",
                f"/loop/plants/{i.plant}/configuration",
                json={
                    "review_deadline_business_days": 7,
                    "reason": "Ajuste acordado en el comité de seguridad",
                },
            ),
        ),
    ),
    # VIG-212 (TASK-308): clasificar un hallazgo de B responde igual que uno inexistente
    # (not_found, nunca transition_not_allowed ni un detail_code); nada se escribe en B.
    (
        ("POST", "/loop/findings/{finding_state_id}/classifications"),
        Case(
            Kind.RESOURCE,
            lambda i: Call(
                "POST",
                f"/loop/findings/{i.record}/classifications",
                json={
                    "outcome": "confirmed",
                    "reason_category": "real_coexistence",
                    "reason_text": "Motivo sintético de la clasificación del hallazgo en la zona",
                },
            ),
        ),
    ),
    # VIG-213 (TASK-309): la cola de revisión es ``own`` (sin identificador: nunca muestra ni
    # toca la de B) y resolver una detección de B responde igual que un identificador inexistente
    # (not_found, nunca transition_not_allowed ni un detail_code); nada se escribe en B.
    (
        ("GET", "/loop/review-queue"),
        Case(Kind.OWN, lambda i: Call("GET", "/loop/review-queue")),
    ),
    (
        ("POST", "/loop/review-queue/{detection_id}/resolution"),
        Case(
            Kind.RESOURCE,
            lambda i: Call(
                "POST",
                f"/loop/review-queue/{i.record}/resolution",
                json={
                    "outcome": "review_confirmed",
                    "reason_category": "real_coexistence",
                    "reason_text": "Motivo sintético de la resolución de la detección en la zona",
                },
            ),
        ),
    ),
    # VIG-223 (TASK-311): asignar la acción de un hallazgo de B y añadir evidencia a una acción
    # de B responden igual que con un identificador inexistente (not_found, nunca
    # transition_not_allowed ni un detail_code); nada se escribe en B.
    (
        ("POST", "/loop/findings/{finding_state_id}/actions"),
        Case(
            Kind.RESOURCE,
            lambda i: Call(
                "POST",
                f"/loop/findings/{i.record}/actions",
                json={
                    "action_type": "engineering",
                    "action_description": "Instalar un resguardo fijo en el acceso a la zona",
                    "assignee_user_id": str(i.user),
                },
            ),
        ),
    ),
    (
        ("POST", "/loop/actions/{action_id}/evidence"),
        Case(
            Kind.RESOURCE,
            lambda i: Call(
                "POST",
                f"/loop/actions/{i.record}/evidence",
                json={
                    "description": "Resguardo fijo instalado y anclado al piso en el acceso",
                    "performed_at": "2026-10-01T10:00:00.000Z",
                    "performed_by_role": "Técnico de mantenimiento",
                },
            ),
        ),
    ),
    # VIG-233 (TASK-343): cerrar o reabrir un hallazgo de B responde igual que uno inexistente
    # (not_found, nunca transition_not_allowed ni un detail_code); nada se escribe en B.
    (
        ("POST", "/loop/findings/{finding_state_id}/closure"),
        Case(
            Kind.RESOURCE,
            lambda i: Call(
                "POST",
                f"/loop/findings/{i.record}/closure",
                json={
                    "closure_description": (
                        "Resguardo fijo verificado y anclado en el acceso de la zona"
                    ),
                    "professional_license": "LIC-SST-0001",
                },
            ),
        ),
    ),
    (
        ("POST", "/loop/findings/{finding_state_id}/reopening"),
        Case(
            Kind.RESOURCE,
            lambda i: Call(
                "POST",
                f"/loop/findings/{i.record}/reopening",
                json={
                    "reason_text": "El resguardo se retiró durante el mantenimiento mayor",
                    "action_type": "engineering",
                    "action_description": "Reinstalar el resguardo fijo en el acceso de la zona",
                    "assignee_user_id": str(i.user),
                },
            ),
        ),
    ),
    # VIG-214 (TASK-315): el análisis de causa raíz de un hallazgo o de un análisis de B responde
    # igual que uno inexistente (not_found, nunca un detail_code); nada se escribe en B.
    (
        ("POST", "/loop/findings/{finding_state_id}/root-cause-analyses"),
        Case(
            Kind.RESOURCE,
            lambda i: Call("POST", f"/loop/findings/{i.record}/root-cause-analyses"),
        ),
    ),
    (
        ("PUT", "/loop/root-cause-analyses/{analysis_id}/steps/{step_index}"),
        Case(
            Kind.RESOURCE,
            lambda i: Call(
                "PUT", f"/loop/root-cause-analyses/{i.record}/steps/1", json=_ANALYSIS_STEP
            ),
        ),
    ),
    (
        ("POST", "/loop/root-cause-analyses/{analysis_id}/signature"),
        Case(
            Kind.RESOURCE,
            lambda i: Call(
                "POST",
                f"/loop/root-cause-analyses/{i.record}/signature",
                json=_ANALYSIS_SIGNATURE,
            ),
        ),
    ),
    # VIG-224 (TASK-316): pedir el asistente sobre un análisis de B o leer una solicitud de B
    # responde igual que con un identificador inexistente (not_found, nunca un detail_code ni el
    # 503 del circuito); nada se encola ni se escribe en B.
    (
        ("POST", "/loop/root-cause-analyses/{analysis_id}/assistant-requests"),
        Case(
            Kind.RESOURCE,
            lambda i: Call("POST", f"/loop/root-cause-analyses/{i.record}/assistant-requests"),
        ),
    ),
    (
        ("GET", "/loop/assistant-requests/{request_id}"),
        Case(Kind.RESOURCE, lambda i: Call("GET", f"/loop/assistant-requests/{i.record}")),
    ),
    # VIG-222 (TASK-307): la bandeja con una zona de B en la consulta responde igual que con una
    # inexistente (not_found, nunca forbidden ni una página vacía con avisos de B).
    (
        ("GET", "/loop/findings"),
        Case(
            Kind.RESOURCE, lambda i: Call("GET", "/loop/findings", params={"zone_id": str(i.zone)})
        ),
    ),
    # VIG-229 (TASK-313): el panel de vencidos de una planta de B responde igual que el de una
    # inexistente (not_found, nunca forbidden ni una página vacía con el umbral de B).
    (
        ("GET", "/loop/overdue"),
        Case(
            Kind.RESOURCE, lambda i: Call("GET", "/loop/overdue", params={"plant_id": str(i.plant)})
        ),
    ),
    # TASK-310: adjuntos de evidencia de cierre (SUP-U05-09 y 10).
    (
        ("POST", "/loop/actions/{action_id}/attachments/upload-grant"),
        Case(
            Kind.RESOURCE,
            call=lambda ids: Call(
                "POST",
                f"/loop/actions/{ids.record}/attachments/upload-grant",
                json={"content_type": "image/png", "size_bytes": 1, "sha256": "a" * 64},
            ),
        ),
    ),
    (
        ("POST", "/loop/attachments/{attachment_id}/read-url"),
        Case(
            Kind.RESOURCE,
            call=lambda ids: Call("POST", f"/loop/attachments/{ids.evidence}/read-url"),
        ),
    ),
    # VIG-215 (TASK-317): una zona de B en el cuerpo responde igual que una inexistente
    # (not_found, nunca forbidden ni export_zone_out_of_scope); nada se encola ni se audita en B.
    (
        ("POST", "/accreditation/exports"),
        Case(
            Kind.RESOURCE,
            lambda i: Call("POST", "/accreditation/exports", json=_export_body(i.plant, i.zone)),
        ),
    ),
    # La exportación de otra organización no es visible: igual que una inexistente (la prueba con
    # una exportación real de B está en tests/integration/test_export_request_routes.py).
    (
        ("GET", "/accreditation/exports/{export_id}"),
        Case(Kind.RESOURCE, lambda i: Call("GET", f"/accreditation/exports/{i.event}")),
    ),
    # VIG-226 (TASK-345): las descargas de una exportación de otra organización responden como
    # una inexistente y no firman ni auditan ninguna concesión (las pruebas con una exportación
    # lista de B están en tests/integration/test_export_download_routes.py).
    (
        ("GET", "/accreditation/exports/{export_id}/download"),
        Case(Kind.RESOURCE, lambda i: Call("GET", f"/accreditation/exports/{i.event}/download")),
    ),
    (
        ("POST", "/accreditation/exports/{export_id}/download-url"),
        Case(
            Kind.RESOURCE,
            lambda i: Call("POST", f"/accreditation/exports/{i.event}/download-url"),
        ),
    ),
    # VIG-217 (TASK-324): la lista propia (SUP-U05-33, forma mínima) nunca muestra un aviso de B
    # ni de otro usuario; ``notifications.read`` la tienen los siete roles (lista propia, OWN).
    (("GET", "/notifications"), Case(Kind.OWN, lambda i: Call("GET", "/notifications"))),
    # TASK-348 (SUP-U05-34, PR-LAZ-61): marcar leído un aviso ajeno o inexistente responde
    # ``not_found`` y no deja efecto (lista propia, OWN; sin identificador real de B que probar
    # aquí, el caso de ``not_found`` sobre un aviso real de otro usuario está en
    # ``tests/properties/laz/test_pr_laz_61_preference_ownership.py``).
    (
        ("POST", "/notifications/{notification_id}/read"),
        Case(Kind.OWN, lambda i: Call("POST", f"/notifications/{i.record}/read")),
    ),
    # TASK-348 (SUP-U05-35, PR-LAZ-61): las preferencias son siempre las del propio actor.
    (
        ("GET", "/notifications/preferences"),
        Case(Kind.OWN, lambda i: Call("GET", "/notifications/preferences")),
    ),
    (
        ("PUT", "/notifications/preferences"),
        Case(
            Kind.OWN,
            lambda i: Call(
                "PUT",
                "/notifications/preferences",
                json={
                    "email_enabled_normal": True,
                    "email_enabled_digest": True,
                    "weekly_digest_weekday": 1,
                },
            ),
        ),
    ),
    # TASK-344 (SUP-U05-24, 25 y 41): el paquete mensual, su muestra y la descarga por planta de
    # una zona de B responden igual que un identificador inexistente (not_found, nunca forbidden
    # ni el 409 de "no sellado"); nada se firma ni se audita en B.
    (
        ("GET", "/accreditation/monthly-packages"),
        Case(
            Kind.RESOURCE,
            lambda i: Call(
                "GET",
                "/accreditation/monthly-packages",
                params={"zone_id": str(i.zone), "period": "2026-01"},
            ),
        ),
    ),
    (
        ("GET", "/accreditation/monthly-packages/{package_id}/sample"),
        Case(
            Kind.RESOURCE,
            lambda i: Call("GET", f"/accreditation/monthly-packages/{i.record}/sample"),
        ),
    ),
    (
        ("GET", "/accreditation/monthly-packages/{plant_id}/{period}/download"),
        Case(
            Kind.RESOURCE,
            lambda i: Call("GET", f"/accreditation/monthly-packages/{i.plant}/2026-01/download"),
        ),
    ),
    # TASK-347 (SUP-U05-26 a 29): el resultado de un elemento de la muestra, los compromisos de
    # un paquete, la actualización de un compromiso y la lista de una zona de B responden igual
    # que con un identificador inexistente (not_found, nunca forbidden, sample_already_reviewed
    # ni commitment_out_of_scope); nada se escribe en B. Las pruebas con una muestra y un paquete
    # reales de otra organización y de otra zona están en
    # tests/integration/test_monthly_audit_and_commitments_routes.py.
    (
        ("POST", "/accreditation/sample-items/{sample_item_id}/result"),
        Case(
            Kind.RESOURCE,
            lambda i: Call(
                "POST",
                f"/accreditation/sample-items/{i.record}/result",
                json={"outcome": "consistent_criterion"},
            ),
        ),
    ),
    (
        ("POST", "/accreditation/monthly-packages/{package_id}/commitments"),
        Case(
            Kind.RESOURCE,
            lambda i: Call(
                "POST",
                f"/accreditation/monthly-packages/{i.record}/commitments",
                json={
                    "commitments": [
                        {
                            "text": "Revisar la señalización del pasillo peatonal de la zona",
                            "responsible_user_id": str(i.user),
                            "due_date": "2026-12-01",
                        }
                    ]
                },
            ),
        ),
    ),
    (
        ("PATCH", "/accreditation/commitments/{commitment_id}"),
        Case(
            Kind.RESOURCE,
            lambda i: Call(
                "PATCH", f"/accreditation/commitments/{i.record}", json={"state": "done"}
            ),
        ),
    ),
    (
        ("GET", "/accreditation/commitments"),
        Case(
            Kind.RESOURCE,
            lambda i: Call(
                "GET",
                "/accreditation/commitments",
                params={"zone_id": str(i.zone), "period": "2026-01"},
            ),
        ),
    ),
    # VIG-231 (TASK-322, SUP-U05-31): el indicador de exposición de una zona de B responde igual
    # que una zona inexistente (not_found, nunca forbidden, exposure_not_computable ni el 503 de
    # cobertura); nada se calcula, se audita ni se guarda en B.
    (
        ("GET", "/accreditation/exposure"),
        Case(
            Kind.RESOURCE,
            lambda i: Call(
                "GET",
                "/accreditation/exposure",
                params={"zone_id": str(i.zone), "period": "2026-01"},
            ),
        ),
    ),
    # VIG-216 (TASK-323): la adopción de una planta de B responde igual que la de una inexistente
    # (not_found, nunca forbidden ni la actividad o el umbral de B).
    (
        ("GET", "/accreditation/adoption"),
        Case(
            Kind.RESOURCE,
            lambda i: Call(
                "GET",
                "/accreditation/adoption",
                params={"plant_id": str(i.plant), "from": "2026-01-01", "to": "2026-01-31"},
            ),
        ),
    ),
]
"""``((MÉTODO, /plantilla), Case)`` de cada ruta de U-04."""

LAZ_DENIED_UNDER_CONCESSION: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("GET", _CONFIGURATION),
        # VIG-217 (TASK-324): el proveedor no entra al lazo (BR-LAZ-142) ni tiene avisos en el
        # cliente; ``notifications.read`` la tienen los siete roles, también el instalador.
        ("GET", "/notifications"),
        # TASK-348: mismo cierre bajo concesión (A-69: ninguna fila de notifications visible ni
        # escribible bajo concesión de proveedor).
        ("POST", "/notifications/{notification_id}/read"),
        ("GET", "/notifications/preferences"),
        ("PUT", "/notifications/preferences"),
    }
)
"""Rutas de U-04 con la clave en la columna que el servicio deniega bajo concesión."""
