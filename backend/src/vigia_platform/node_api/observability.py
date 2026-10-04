"""Métricas, tramos y registro de las rutas del contrato (NFR-GOB-54, 57 y 25; TASK-206).

- ``node_requests_total`` por ruta, resultado (``accepted``, ``accepted_duplicate``,
  ``rejected`` o ``not_found``) y ``rejection_code``; ``node_request_body_bytes`` con el tamaño
  del cuerpo recibido; la latencia por ruta es ``http_server_duration_ms`` de la cadena y los
  ``rate_limited`` por causa, ``rate_limited_total`` (``node_api.limits``). Son la comparación
  directa con los contadores del nodo simulado de U-01 (NFR-CTR-42).
- Tramo ``node_api.route`` por petición con su ruta y su ``correlation_id``; la verificación de
  identidad tiene el suyo (``node_api.identity``). Nunca SQL ni contenido (NFR-GOB-57).
- Una línea de registro por petición de nodo con los campos obligatorios de NFR-GOB-25
  (``correlation_id``, ``organization_id``, ``plant_id``, ``node_id``, ``zone_id`` si aplica,
  ``route``, ``rejection_code`` o ``status`` y ``duration_ms``) y sin los prohibidos: ni
  cabeceras, ni certificado, ni cuerpo, ni mensajes de excepción.

``NodeResponses`` es además el ``NodeErrorRenderer`` de la cadena (``shared.api.errors``): toda
excepción de una petición de nodo, venga de la cadena compartida, de FastAPI, de la verificación
previa o de la operación, se responde con ``rejections.render`` y se mide aquí.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import MutableMapping
from typing import Any, Final

from starlette.responses import Response
from vigia_contracts.models.enumerations import RejectionCode

from vigia_platform.identity.authz.context import NodeScope, PresentedNode
from vigia_platform.node_api.rejections import render, route_of
from vigia_platform.node_api.versioning import PLATFORM_CONTRACT_VERSION
from vigia_platform.shared.api.declarations import NodeRoute
from vigia_platform.shared.api.request_state import request_state
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.observability import redaction
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics
from vigia_platform.shared.observability.tracing import span_name

__all__ = ["ROUTE_SPAN", "UNMATCHED_NODE_ROUTE", "NodeResponses", "NodeResult"]

ROUTE_SPAN: Final = span_name("node_api.route")
UNMATCHED_NODE_ROUTE: Final = "unmatched"


class NodeResult(enum.StrEnum):
    """Resultado de una petición de nodo (atributo ``result`` de ``node_requests_total``)."""

    ACCEPTED = "accepted"
    ACCEPTED_DUPLICATE = "accepted_duplicate"
    REJECTED = "rejected"
    NOT_FOUND = "not_found"


redaction.DEFAULT_POLICY.register("result", [result.value for result in NodeResult])

_log = get_logger("node_api")


class NodeResponses:
    """Respuestas de la clase ``node``: rechazos del contrato y medición de cada petición."""

    def __init__(
        self,
        clock: Clock,
        *,
        metrics: PlatformMetrics | None = None,
        contract_version: str = PLATFORM_CONTRACT_VERSION,
    ) -> None:
        self._clock = clock
        self._metrics = metrics
        self._contract_version = contract_version

    @property
    def contract_version(self) -> str:
        return self._contract_version

    @property
    def _instruments(self) -> PlatformMetrics:
        return self._metrics if self._metrics is not None else get_metrics()

    def render(self, error: BaseException, scope: MutableMapping[str, Any]) -> Response:
        """``NodeErrorRenderer``: la respuesta del contrato a ``error`` (y su medición)."""
        route = route_of(scope)
        rendered = render(route, error, self._contract_version)
        if rendered.rejection is None:
            self.record(scope, route, rendered.status, NodeResult.NOT_FOUND, None)
        else:
            self.record(scope, route, rendered.status, NodeResult.REJECTED, rendered.rejection.code)
        return rendered.response

    def record(
        self,
        scope: MutableMapping[str, Any],
        route: NodeRoute | None,
        status: int,
        result: NodeResult,
        code: RejectionCode | None,
        *,
        zone_id: uuid.UUID | None = None,
    ) -> None:
        """Métricas y línea de registro de una petición de nodo terminada."""
        state = request_state(scope)
        route_name = route.path if route is not None else UNMATCHED_NODE_ROUTE
        instruments = self._instruments
        attributes: dict[str, object] = {"route": route_name, "result": result.value}
        if code is not None:
            attributes["rejection_code"] = code.value
        instruments.node_requests_total.add(1, attributes)
        instruments.node_request_body_bytes.record(state.body_bytes_received, {"route": route_name})
        fields: dict[str, object] = {"route": route_name, "status": status}
        if state.correlation_id is not None:
            fields["correlation_id"] = str(state.correlation_id)
        if state.started_monotonic is not None:
            fields["duration_ms"] = max(
                0.0, (self._clock.monotonic() - state.started_monotonic) * 1000
            )
        if code is not None:
            fields["rejection_code"] = code.value
        node = getattr(state.node, "node", None)
        presented = getattr(state.node, "presented", None)
        if isinstance(node, NodeScope):
            fields["organization_id"] = str(node.organization_id)
            fields["plant_id"] = str(node.plant_id)
            fields["node_id"] = str(node.node_id)
        elif isinstance(presented, PresentedNode):
            # Rechazada antes de resolver la identidad: lo que dice su certificado.
            fields["organization_id"] = str(presented.organization_id)
            fields["plant_id"] = str(presented.plant_id)
            fields["node_id"] = str(presented.node_id)
        if zone_id is not None:
            fields["zone_id"] = str(zone_id)
        _log.info("petición de nodo atendida", **fields)
