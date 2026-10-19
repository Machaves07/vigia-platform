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
from vigia_platform.accreditation.adapters.http.services import (
    ACCREDITATION_STATE_KEY,
    AccreditationHttp,
)
from vigia_platform.accreditation.events import register_accreditation_event_types
from vigia_platform.accreditation.export.request import ExportRequests
from vigia_platform.catalog.adapters.postgres.query_ports import catalog_query_ports
from vigia_platform.fleet.application.inventory_read import FleetInventory
from vigia_platform.identity.authz.authorize import Authorizer
from vigia_platform.ledger.application.audit_writer import AuditWriter
from vigia_platform.ledger.application.coverage import CoverageService
from vigia_platform.ledger.application.writer import EscritorExpediente
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.loop.adapters.http.services import LOOP_STATE_KEY, LoopHttp
from vigia_platform.loop.application.classification import ClassificationService
from vigia_platform.loop.application.root_cause import RootCauseService
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.config.service import LoopConfigurationService
from vigia_platform.notifications.adapters.http.services import (
    NOTIFICATIONS_STATE_KEY,
    NotificationsHttp,
)
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
        ),
    }


def _accreditation(services: LazServices) -> Mapping[str, object]:
    return {
        ACCREDITATION_STATE_KEY: AccreditationHttp(
            exports=ExportRequests(
                database=services.database,
                authorizer=services.authorizer,
                audit=services.audit,
                outbox=services.outbox,
                clock=services.clock,
            )
        )
    }


def _notifications(services: LazServices) -> Mapping[str, object]:
    """``NotificationsHttp`` (TASK-324). ``LazServices`` no lleva la proveedora y
    ``test_route_isolation.py`` no se edita: se toma la del ``Authorizer`` de la prueba."""
    provider: uuid.UUID = services.authorizer._provider_organization_id
    return {
        NOTIFICATIONS_STATE_KEY: NotificationsHttp(
            reader=NotificationReader(
                database=services.database,  # type: ignore[arg-type]
                provider_organization_id=provider,
            )
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
    # VIG-217 (TASK-324): la lista propia (SUP-U05-33, forma mínima) nunca muestra un aviso de B
    # ni de otro usuario; ``notifications.read`` la tienen los siete roles (lista propia, OWN).
    (("GET", "/notifications"), Case(Kind.OWN, lambda i: Call("GET", "/notifications"))),
]
"""``((MÉTODO, /plantilla), Case)`` de cada ruta de U-04."""

LAZ_DENIED_UNDER_CONCESSION: Final[frozenset[tuple[str, str]]] = frozenset(
    {("GET", _CONFIGURATION)}
)
"""Rutas de U-04 con la clave en la columna que el servicio deniega bajo concesión."""
