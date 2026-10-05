"""Declaración de las rutas del contrato (TASK-206; A-51; BR-NUC-91).

- ``NodeRoute`` son las diez rutas obligatorias de ``OPERATIONS`` del esqueleto de U-01 (método,
  plantilla, certificado y límite de cuerpo; 16 KB la rotación y sin cuerpo la confirmación y el
  catálogo, nota de NFR-GOB-33); ``GET conformance-profile`` no está.
- ``check_routes`` impide arrancar con una ruta bajo ``/api/nodes`` sin ``node_route`` (sin
  declaración o con ``requires``), con un ``node_route`` fuera de su plantilla o de su método
  (también fuera de ``/api/nodes``), con ``body_limit`` en una ruta del contrato o con una entrada
  repetida.
- ``node_router`` monta solo las rutas pedidas, cada una con su ``node_route`` resuelta por
  ``operation_id``, y nunca ``conformance-profile``; la aplicación de producción publica el latido
  y el catálogo por zona (TASK-223).
- Sin ``NodeGate`` instalada la ruta deniega con el transitorio del contrato.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import APIRouter
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from vigia_contracts.server_skeleton import OPERATIONS

from tests.api_support import World
from vigia_platform.node_api.declarations import SPECS, PathParameter
from vigia_platform.node_api.router import node_router
from vigia_platform.shared.api.app import UnitRegistration, build_openapi_app, platform_units
from vigia_platform.shared.api.declarations import (
    NODE_PREFIX,
    NodeRoute,
    body_limit,
    check_routes,
    iter_declared_routes,
    node_route,
    requires,
)
from vigia_platform.shared.api.errors import ApiStartupError, DetailCodeRegistry
from vigia_platform.shared.runtime.units import PUBLISHED_NODE_ROUTES

PERMISSIONS = frozenset({"fleet.read"})

_OVERRIDES = {"post_credential_rotation": 16_384, "post_clip_upload_confirmation": 0}
"""Lo que el diseño de U-03 fija y ``ingest.yaml`` deja sin ``max_bytes`` (nota de NFR-GOB-33)."""


def _problems(*routes: APIRoute) -> list[str]:
    registry = DetailCodeRegistry()
    registry.seal()
    return check_routes(list(routes), PERMISSIONS, registry, docs_enabled=False)


async def _handler() -> dict[str, str]:  # pragma: no cover - no se llama
    return {}


# --- NodeRoute frente al esqueleto de U-01 -------------------------------------------------------


def test_node_route_is_the_ten_mandatory_routes_of_the_skeleton() -> None:
    assert len(NodeRoute) == 10
    assert {route.operation_id for route in NodeRoute} == set(OPERATIONS) - {
        "get_conformance_profile"
    }
    for route in NodeRoute:
        operation = OPERATIONS[route.operation_id]
        assert (route.method, route.relative_path) == (operation.method, operation.path)
        assert route.path == NODE_PREFIX + operation.path
        assert route.mutual_tls is operation.mutual_tls
        expected = operation.max_bytes or _OVERRIDES.get(route.operation_id, 0)
        assert route.max_body_bytes == expected, route


def test_the_body_limits_are_those_of_br_ctr_12_and_the_note_of_nfr_gob_33() -> None:
    assert NodeRoute.FINDING.max_body_bytes == NodeRoute.DETECTION_REVIEW.max_body_bytes == 262_144
    assert NodeRoute.HEARTBEAT.max_body_bytes == 65_536
    assert NodeRoute.OBSERVABILITY_EVENT.max_body_bytes == 65_536
    assert NodeRoute.UPDATE_RESULT.max_body_bytes == 65_536
    assert NodeRoute.CLIP_UPLOAD.max_body_bytes == NodeRoute.ENROLLMENT.max_body_bytes == 16_384
    assert NodeRoute.CREDENTIAL_ROTATION.max_body_bytes == 16_384
    assert NodeRoute.CLIP_CONFIRMATION.max_body_bytes == 0
    assert NodeRoute.ZONE_CATALOG.max_body_bytes == 0


def test_every_route_has_its_strict_reader_of_the_generated_models() -> None:
    assert set(SPECS) == set(NodeRoute)
    for route, spec in SPECS.items():
        model = spec.operation.request_model
        if model is None:
            assert spec.parser is None, route
            continue
        assert spec.parser is not None and model.__name__ in (spec.parser.__doc__ or ""), route
    assert SPECS[NodeRoute.ZONE_CATALOG].path_parameter is PathParameter.ZONE_ID
    assert SPECS[NodeRoute.CLIP_CONFIRMATION].path_parameter is PathParameter.CLIP_ID


# --- check_routes ------------------------------------------------------------------------------


def test_a_route_under_api_nodes_without_node_route_does_not_start() -> None:
    undeclared = APIRoute("/api/nodes/findings", _handler, methods=["POST"])
    with_permission = APIRoute(
        "/api/nodes/findings", _handler, methods=["POST"], dependencies=[requires("fleet.read")]
    )
    assert any("no declara node_route" in p for p in _problems(undeclared))
    assert any("no declara node_route" in p for p in _problems(with_permission))
    # La raíz del prefijo también es de la clase node.
    root = APIRoute("/api/nodes", _handler, methods=["GET"], dependencies=[requires("fleet.read")])
    assert any("no declara node_route" in p for p in _problems(root))


def test_a_node_route_outside_its_entry_or_the_prefix_does_not_start() -> None:
    outside = APIRoute(
        "/findings", _handler, methods=["POST"], dependencies=[node_route(NodeRoute.FINDING)]
    )
    other_path = APIRoute(
        "/api/nodes/finding",
        _handler,
        methods=["POST"],
        dependencies=[node_route(NodeRoute.FINDING)],
    )
    other_method = APIRoute(
        "/api/nodes/findings",
        _handler,
        methods=["PUT"],
        dependencies=[node_route(NodeRoute.FINDING)],
    )
    for route in (outside, other_path, other_method):
        problems = _problems(route)
        assert any("«FINDING» de las rutas del contrato" in p for p in problems), problems


def test_a_correct_node_route_passes_and_a_repeated_entry_does_not() -> None:
    good = APIRoute(
        NodeRoute.FINDING.path,
        _handler,
        methods=["POST"],
        dependencies=[node_route(NodeRoute.FINDING)],
    )
    assert _problems(good) == []
    again = APIRoute(
        NodeRoute.FINDING.path,
        _handler,
        methods=["POST"],
        dependencies=[node_route(NodeRoute.FINDING)],
    )
    assert any("repetida" in p for p in _problems(good, again))


def test_a_node_route_does_not_declare_its_own_body_limit() -> None:
    route = APIRoute(
        NodeRoute.HEARTBEAT.path,
        _handler,
        methods=["POST"],
        dependencies=[node_route(NodeRoute.HEARTBEAT), body_limit(4096)],
    )
    assert any("su límite de cuerpo es el de su NodeRoute" in p for p in _problems(route))


def test_node_route_only_takes_a_node_route() -> None:
    with pytest.raises(TypeError):
        node_route("FINDING")  # type: ignore[arg-type]


def test_an_application_with_an_undeclared_node_route_does_not_start() -> None:
    router = APIRouter()
    router.add_api_route(
        "/api/nodes/findings", _handler, methods=["POST"], dependencies=[requires("fleet.read")]
    )
    with pytest.raises(ApiStartupError, match="no declara node_route"):
        World().app(units=(UnitRegistration("sonda", routers=(router,)),))


# --- node_router -------------------------------------------------------------------------------


def test_node_router_mounts_only_the_requested_routes_with_their_declaration() -> None:
    routes = list(
        iter_declared_routes(node_router((NodeRoute.FINDING, NodeRoute.ZONE_CATALOG)).routes)
    )
    assert {(route.path, *route.methods) for route in routes} == {
        (NodeRoute.FINDING.path, "POST"),
        (NodeRoute.ZONE_CATALOG.path, "GET"),
    }
    for route in routes:
        (declaration,) = route.declarations
        assert declaration.node is not None and declaration.node.path == route.path
        assert declaration.permission is None and declaration.unauthenticated is None


def test_node_router_with_every_route_never_mounts_conformance_profile() -> None:
    router = node_router(tuple(NodeRoute))
    routes = list(iter_declared_routes(router.routes))
    assert len(routes) == 10
    assert all("conformance-profile" not in route.path for route in routes)
    registry = DetailCodeRegistry()
    registry.seal()
    assert check_routes(router.routes, PERMISSIONS, registry, docs_enabled=False) == []


def test_production_publishes_the_heartbeat_and_the_zone_catalog() -> None:
    # TASK-223: las dos primeras rutas del contrato; las demás llegan con sus tareas.
    assert PUBLISHED_NODE_ROUTES == (NodeRoute.HEARTBEAT, NodeRoute.ZONE_CATALOG)
    assert "node_api" in {unit.name for unit in platform_units()}
    paths = {route.path for route in iter_declared_routes(build_openapi_app().routes)}
    assert {path for path in paths if path.startswith(NODE_PREFIX)} == {
        NodeRoute.HEARTBEAT.path,
        NodeRoute.ZONE_CATALOG.path,
    }


# --- Sin NodeGate ------------------------------------------------------------------------------


def test_without_a_node_gate_the_route_denies_with_the_contract_transient() -> None:
    world = World()
    app = world.app(
        units=(UnitRegistration("sonda", routers=(node_router((NodeRoute.HEARTBEAT,)),)),)
    )
    with TestClient(app) as client:
        response = client.post(NodeRoute.HEARTBEAT.path, content=b"{}")
    assert response.status_code == 503
    body: dict[str, Any] = response.json()
    assert body["code"] == "temporarily_unavailable" and body["retryable"] is True
    assert "correlation_id" not in body


def test_node_router_without_routes_mounts_nothing() -> None:
    # La lista de producción vacía no añade ninguna ruta (app.yaml no cambia).
    assert node_router(()).routes == []
