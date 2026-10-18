"""Casos de aislamiento por ruta de U-04 (TASK-302; PR-LAZ-62 parcial; LC-LAZ-43).

``test_route_isolation.py`` concatena ``LAZ_CASES`` a su ``CASES``, y ``uncovered`` exige un caso
por cada ruta registrada: una ruta de ``/loop/*``, ``/accreditation/*`` o ``/notifications/*``
sin su caso aquí deja la prueba en rojo. **Ninguna tarea de U-04 edita
``test_route_isolation.py``**: cada tarea que registra una ruta añade aquí su caso, con los tipos
de ``tests.isolation.route_cases``::

    (("GET", "/loop/findings/{finding_state_id}"), Case(Kind.RESOURCE, call=...)),

Desde TASK-304 este módulo da también lo que la aplicación de la prueba necesita de U-04:

- ``laz_state``: las entradas de ``app.state`` de las unidades de U-04 (``LoopHttp``…), con la
  infraestructura de la prueba; cada tarea añade aquí su servicio;
- ``LAZ_DENIED_UNDER_CONCESSION``: las rutas cuya clave está en la columna ``provider_installer``
  pero cuyo servicio deniega bajo concesión (``not_found`` tras su ``provider_query``, con
  ``authorization_denied`` en la auditoría del cliente). ``GET /loop/plants/{plant_id}/
  configuration`` es la primera: ``hierarchy.read`` la tienen todos los roles, también el
  instalador, y la configuración del lazo no es de la puesta en marcha (VIG-205).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from tests.isolation.route_cases import Call, Case, Kind
from vigia_platform.identity.authz.authorize import Authorizer
from vigia_platform.ledger.application.writer import EscritorExpediente, LedgerDatabase
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.loop.adapters.http.services import LOOP_STATE_KEY, LoopHttp
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.config.service import LoopConfigurationService
from vigia_platform.shared.clock import Clock

__all__ = ["LAZ_CASES", "LAZ_DENIED_UNDER_CONCESSION", "laz_state"]

_CONFIGURATION: Final = "/loop/plants/{plant_id}/configuration"

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
]
"""``((MÉTODO, /plantilla), Case)`` de cada ruta de U-04."""

LAZ_DENIED_UNDER_CONCESSION: Final[frozenset[tuple[str, str]]] = frozenset(
    {("GET", _CONFIGURATION)}
)
"""Rutas de U-04 con la clave en la columna que el servicio deniega bajo concesión."""


def laz_state(
    *,
    database: LedgerDatabase,
    writer: EscritorExpediente,
    authorizer: Authorizer,
    free_text: FreeTextPolicyRegistry,
    clock: Clock,
) -> Mapping[str, object]:
    """Las entradas de ``app.state`` de U-04 sobre la infraestructura de la prueba."""
    return {
        LOOP_STATE_KEY: LoopHttp(
            configuration=LoopConfigurationService(
                repository=PostgresLoopConfigurationRepository(database),
                database=database,
                writer=writer,
                authorizer=authorizer,
                free_text=free_text,
                clock=clock,
            ),
        ),
    }
