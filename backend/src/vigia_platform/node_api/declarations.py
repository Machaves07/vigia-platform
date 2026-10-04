"""Lo que fija cada ruta del contrato (TASK-206): su operación, su lector y su parámetro de ruta.

``OPERATIONS`` del esqueleto de U-01 es la fuente del método, la ruta, el límite de cuerpo
(``max_bytes``), las codificaciones admitidas, ``mutual_tls`` y los estados de rechazo de cada
operación. Esta tabla los liga a la lista cerrada ``NodeRoute`` (``shared.api.declarations``, A-51)
y añade el lector **estricto** del cuerpo con los modelos generados de U-01 (``parse_*``: sin
coerción, ``extra = "forbid"``, ``field`` = ruta JSON del primer campo que falla) y el parámetro
de ruta que nombra un recurso del alcance (``zone_id``) o un identificador (``clip_id``).

``tests/unit/test_node_route_declarations.py`` comprueba que ``NodeRoute`` y esta tabla coinciden
con ``OPERATIONS`` salvo en lo que el diseño de U-03 fija aparte (16 KB la rotación, sin cuerpo la
confirmación; nota de NFR-GOB-33).
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from vigia_contracts.models import api
from vigia_contracts.models._base import ContractModel
from vigia_contracts.server_skeleton import OPERATIONS, Operation

from vigia_platform.shared.api.declarations import NodeRoute

__all__ = ["SPECS", "NodeOperationSpec", "PathParameter", "spec_of"]

type Parser = Callable[[bytes], ContractModel]


class PathParameter(enum.StrEnum):
    """Parámetros de ruta de las operaciones del contrato."""

    ZONE_ID = "zone_id"
    """Una zona: tiene que estar entre las asignadas al nodo (paso 2, ``node_zone_mismatch``)."""
    CLIP_ID = "clip_id"
    """Un clip: UUID v7 del nodo (paso 4, ``schema_invalid``)."""


@dataclass(frozen=True, slots=True)
class NodeOperationSpec:
    """Una ruta del contrato con lo que su verificación previa necesita."""

    route: NodeRoute
    operation: Operation
    parser: Parser | None
    """Lector estricto del cuerpo; ``None`` si la operación no lleva cuerpo."""
    path_parameter: PathParameter | None = None

    @property
    def max_body_bytes(self) -> int:
        return self.route.max_body_bytes

    @property
    def content_encodings(self) -> tuple[str, ...]:
        """Las codificaciones del cuerpo que la operación admite (``ingest.yaml``)."""
        return tuple(self.operation.content_encodings)


def _spec(
    route: NodeRoute, parser: Parser | None, parameter: PathParameter | None = None
) -> NodeOperationSpec:
    return NodeOperationSpec(route, OPERATIONS[route.operation_id], parser, parameter)


SPECS: Final[Mapping[NodeRoute, NodeOperationSpec]] = MappingProxyType(
    {
        NodeRoute.ENROLLMENT: _spec(NodeRoute.ENROLLMENT, api.parse_node_enrollment_request),
        NodeRoute.CREDENTIAL_ROTATION: _spec(
            NodeRoute.CREDENTIAL_ROTATION, api.parse_credential_rotation_request
        ),
        NodeRoute.HEARTBEAT: _spec(NodeRoute.HEARTBEAT, api.parse_heartbeat),
        NodeRoute.FINDING: _spec(NodeRoute.FINDING, api.parse_finding_submission),
        NodeRoute.DETECTION_REVIEW: _spec(
            NodeRoute.DETECTION_REVIEW, api.parse_detection_for_review_submission
        ),
        NodeRoute.OBSERVABILITY_EVENT: _spec(
            NodeRoute.OBSERVABILITY_EVENT, api.parse_observability_event_submission
        ),
        NodeRoute.CLIP_UPLOAD: _spec(NodeRoute.CLIP_UPLOAD, api.parse_clip_upload_request),
        NodeRoute.CLIP_CONFIRMATION: _spec(
            NodeRoute.CLIP_CONFIRMATION, None, PathParameter.CLIP_ID
        ),
        NodeRoute.ZONE_CATALOG: _spec(NodeRoute.ZONE_CATALOG, None, PathParameter.ZONE_ID),
        NodeRoute.UPDATE_RESULT: _spec(NodeRoute.UPDATE_RESULT, api.parse_update_result),
    }
)
"""Una entrada por ``NodeRoute``; ``GET conformance-profile`` no está (A-51: no se implementa)."""


def spec_of(route: NodeRoute) -> NodeOperationSpec:
    """La entrada de ``route`` (``KeyError`` si no es una ruta del contrato)."""
    return SPECS[NodeRoute(route)]
