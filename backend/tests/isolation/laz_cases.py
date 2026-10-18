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
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from tests.isolation.route_cases import Call, Case, Kind
from vigia_platform.accreditation.adapters.http.services import (
    ACCREDITATION_STATE_KEY,
    AccreditationHttp,
)
from vigia_platform.accreditation.events import register_accreditation_event_types
from vigia_platform.accreditation.export.request import ExportRequests
from vigia_platform.identity.authz.authorize import Authorizer
from vigia_platform.ledger.application.audit_writer import AuditWriter
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.outbox.publish import OutboxPort
from vigia_platform.shared.outbox.registries import EventTypeRegistry

__all__ = ["LAZ_CASES", "LAZ_EVENT_REGISTRARS", "LAZ_STATE", "LazServices", "laz_state"]


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


LAZ_STATE: tuple[Callable[[LazServices], Mapping[str, object]], ...] = (_accreditation,)
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
]
"""``((MÉTODO, /plantilla), Case)`` de cada ruta de U-04."""
