"""Prueba de importación del contrato en ``node_api`` (NFR-GOB-66; D-4; LC-GOB-19; TASK-230).

El equivalente de ``generate --check`` en el consumidor: el esqueleto de rutas generado por U-01
(``vigia_contracts.server_skeleton``, el del paquete fijado en ``uv.lock``) se **implementa sin
modificarse**, y lo que la aplicación de producción publica en cada ruta del contrato es lo que el
esqueleto declara:

- toda operación de ``OPERATIONS`` (salvo la opcional ``get_conformance_profile``, que U-03 no
  implementa en v1 y nunca se monta: A-51) está montada en ``build_openapi_app()`` bajo
  ``/api/nodes`` con su método, su ``endpoint`` es la función del esqueleto (no una copia) y la
  ``NodeApiGate`` de la raíz de producción (``api_state`` del registro de unidades) tiene un
  manejador para ella;
- el **esquema efectivo** de cada ruta coincide con el del esqueleto (``create_app().openapi()``
  del paquete, con las referencias resueltas): el cuerpo (el modelo que lee el lector estricto de
  ``node_api.declarations``, frente al ``requestBody`` del esqueleto), las respuestas (estado y
  modelo; ``default`` es el ``ApiError`` común de la plataforma y no es del contrato), los
  parámetros, ``operationId`` y las extensiones ``x-vigia-*`` (certificado, límite de cuerpo y
  codificaciones);
- los **códigos**: cada rechazo que el esqueleto declara con un estado se responde con ese
  estado (``rejections.status_of``, A-37); ``schema_invalid`` con sus dos.

Las sondas reconstruyen la comparación con una ruta sin montar, un esquema de respuesta, un
modelo del cuerpo y un estado de rechazo distintos, y la operación opcional montada: cada una la
hace fallar nombrando la operación.
"""

from __future__ import annotations

import copy
import inspect
import uuid
from collections.abc import Callable, Mapping
from typing import Any, Final

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from vigia_contracts.models import api
from vigia_contracts.models.enumerations import RejectionCode
from vigia_contracts.server_skeleton import OPERATIONS, create_app

from tests.node_api_support import scope_contexts
from vigia_platform.node_api.declarations import SPECS, NodeOperationSpec
from vigia_platform.node_api.rejections import NodeRejection, status_of
from vigia_platform.node_api.router import NodeApiGate
from vigia_platform.shared.api.app import UnitRegistration, build_openapi_app, platform_units
from vigia_platform.shared.api.declarations import (
    NODE_GATE_STATE_KEY,
    NODE_PREFIX,
    NodeRoute,
    iter_declared_routes,
)
from vigia_platform.shared.clock import SystemClock
from vigia_platform.shared.observability.metrics import get_metrics
from vigia_platform.shared.runtime.units import (
    PUBLISHED_NODE_ROUTES,
    UnitServices,
    api_state,
    registered_units,
)

OPTIONAL_OPERATION: Final = "get_conformance_profile"
"""Descubrimiento de versión: opcional en ``ingest.yaml``; U-03 no lo implementa en v1 (A-51)."""
SKELETON_MODULE: Final = "vigia_contracts.server_skeleton.operations"
PLATFORM_ONLY_RESPONSE: Final = "default"
"""El ``ApiError`` común que la plataforma documenta en toda ruta (no lo responde el contrato)."""
_REF_PREFIX: Final = "#/components/schemas/"
_FASTAPI_VALIDATION: Final = _REF_PREFIX + "HTTPValidationError"

type Gate = Callable[[NodeRoute], object | None]


class _NotUsed:
    """Dependencia de la raíz que construir los manejadores nunca toca."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"construir los manejadores no debía usar «{name}»")


def production_gate() -> NodeApiGate:
    """La ``NodeApiGate`` de la raíz de producción, construida sin red (``api_state``)."""
    unused: Any = _NotUsed()
    clock = SystemClock()
    services = UnitServices(
        clock=clock,
        metrics=get_metrics(),
        provider_organization_id=uuid.uuid4(),
        database=unused,
        contexts=scope_contexts(clock),
        authorizer=unused,
        audit=unused,
        outbox=unused,
        writer=unused,
        free_text=unused,
        signing=unused,
        checkpoints=unused,
        kms=unused,
        evidence=unused,
    )
    gate = api_state(registered_units(), services)[NODE_GATE_STATE_KEY]
    assert isinstance(gate, NodeApiGate)
    return gate


# --- Comparación ---------------------------------------------------------------------------------


def _resolved(document: Mapping[str, Any], node: Any, seen: tuple[str, ...] = ()) -> Any:
    """``node`` con cada ``$ref`` a ``components/schemas`` sustituida por su definición."""
    if isinstance(node, dict):
        reference = node.get("$ref")
        if isinstance(reference, str) and reference.startswith(_REF_PREFIX):
            name = reference.removeprefix(_REF_PREFIX)
            if name in seen:
                return {"$recursive": name}
            target = document["components"]["schemas"][name]
            return _resolved(document, target, (*seen, name))
        return {key: _resolved(document, value, seen) for key, value in node.items()}
    if isinstance(node, list):
        return [_resolved(document, value, seen) for value in node]
    return node


def _ref(name: str) -> dict[str, str]:
    return {"$ref": _REF_PREFIX + name}


def _body_model(spec: NodeOperationSpec | None) -> type[Any] | None:
    """El modelo que lee el lector estricto de la ruta (``api._parser`` lo guarda en su cierre)."""
    if spec is None or spec.parser is None:
        return None
    model: type[Any] = inspect.getclosurevars(spec.parser).nonlocals["model"]
    return model


def _skeleton_body(item: Mapping[str, Any]) -> Any:
    """El esquema del ``requestBody`` del esqueleto, sin resolver: la referencia a su modelo.

    Se compara el modelo y no su esquema resuelto: el OpenAPI del esqueleto fusiona los ``$defs``
    de todos los modelos por nombre (``setdefault``) y una definición homónima de otro modelo
    puede quedar con otra descripción; el modelo generado es la fuente.
    """
    body = item.get("requestBody")
    if body is None:
        return None
    return body["content"]["application/json"]["schema"]


def _app_route(app: FastAPI, method: str, path: str) -> APIRoute | None:
    """La ruta efectiva ``method path`` (también dentro de enrutadores incluidos)."""
    for declared in iter_declared_routes(app.routes):
        route = declared.route
        if isinstance(route, APIRoute) and declared.path == path and method in declared.methods:
            return route
    return None


def _contract_responses(document: Mapping[str, Any], item: Mapping[str, Any]) -> dict[str, Any]:
    """Las respuestas del contrato: sin el ``default`` de la plataforma ni el 422 que FastAPI
    añade por su cuenta a una ruta con parámetros (``HTTPValidationError``, no es de
    ``ingest.yaml``)."""
    responses: dict[str, Any] = {}
    for code, value in item.get("responses", {}).items():
        schema = value.get("content", {}).get("application/json", {}).get("schema", {})
        if code == PLATFORM_ONLY_RESPONSE or schema.get("$ref") == _FASTAPI_VALIDATION:
            continue
        responses[code] = _resolved(document, value)
    return responses


def contract_differences(
    app: FastAPI,
    *,
    specs: Mapping[NodeRoute, NodeOperationSpec] = SPECS,
    gate: Gate,
    status: Callable[[NodeRoute | None, NodeRejection], int] = status_of,
    skeleton: Mapping[str, Any] | None = None,
) -> list[str]:
    """Cada diferencia entre lo que publica ``app`` y el esqueleto, nombrando la operación."""
    reference = skeleton if skeleton is not None else create_app().openapi()
    published = app.openapi()
    by_operation = {route.operation_id: route for route in NodeRoute}
    differences: list[str] = []
    for operation_id, operation in OPERATIONS.items():
        path = NODE_PREFIX + operation.path
        method = operation.method.lower()
        item = published.get("paths", {}).get(path, {}).get(method)
        if operation_id == OPTIONAL_OPERATION:
            if item is not None:
                differences.append(f"{operation_id}: opcional y montada en {path} (A-51)")
            continue
        route = by_operation.get(operation_id)
        if item is None or route is None:
            differences.append(f"{operation_id}: {operation.method} {path} sin implementar")
            continue
        if gate(route) is None:
            differences.append(f"{operation_id}: la raíz no registra su manejador")
        mounted = _app_route(app, operation.method, path)
        if mounted is None or mounted.endpoint.__module__ != SKELETON_MODULE:
            differences.append(f"{operation_id}: la ruta no es la del esqueleto de U-01")
        expected = reference["paths"][operation.path][method]
        for key in ("operationId", "parameters"):
            if item.get(key) != expected.get(key):
                differences.append(f"{operation_id}: `{key}` distinto del esqueleto")
        extensions = {key for key in (*item, *expected) if key.startswith("x-vigia-")}
        for key in sorted(extensions):
            if item.get(key) != expected.get(key):
                differences.append(f"{operation_id}: `{key}` distinto del esqueleto")
        responses = _contract_responses(published, item)
        wanted = _contract_responses(reference, expected)
        for code in sorted(set(responses) | set(wanted)):
            if responses.get(code) != wanted.get(code):
                differences.append(f"{operation_id}: la respuesta {code} es distinta")
        model = _body_model(specs.get(route))
        skeleton_body = _skeleton_body(expected)
        if (model is None) != (skeleton_body is None):
            differences.append(f"{operation_id}: el cuerpo no coincide con el esqueleto")
        elif model is not None and (
            model is not operation.request_model or skeleton_body != _ref(model.__name__)
        ):
            differences.append(f"{operation_id}: el modelo del cuerpo es distinto")
        for response in operation.responses:
            for code in response.rejection_codes or ():
                statuses = {
                    status(route, NodeRejection(RejectionCode(code), body_level=level))
                    for level in (False, True)
                }
                allowed = set(operation.rejection_statuses(code))
                if not statuses <= allowed or response.status_code not in statuses:
                    differences.append(
                        f"{operation_id}: `{code}` responde {sorted(statuses)}, el esqueleto"
                        f" {sorted(allowed)}"
                    )
    return differences


# --- Pruebas --------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app() -> FastAPI:
    return build_openapi_app()


@pytest.fixture(scope="module")
def gate() -> NodeApiGate:
    return production_gate()


def test_every_skeleton_operation_is_implemented_with_the_skeleton_schema(
    app: FastAPI, gate: NodeApiGate
) -> None:
    assert contract_differences(app, gate=gate.operation) == []


def test_the_published_routes_are_the_skeleton_operations_but_the_optional_one() -> None:
    expected = {op for op in OPERATIONS if op != OPTIONAL_OPERATION}
    assert {route.operation_id for route in PUBLISHED_NODE_ROUTES} == expected
    assert {route.operation_id for route in NodeRoute} == expected
    assert set(SPECS) == set(NodeRoute)


def test_the_effective_body_is_the_generated_strict_reader() -> None:
    for route, spec in SPECS.items():
        model = _body_model(spec)
        assert model is OPERATIONS[route.operation_id].request_model, route
        if model is not None:
            assert getattr(api, f"parse_{_snake(model.__name__)}") is spec.parser, route


def _snake(name: str) -> str:
    return "".join(f"_{c.lower()}" if c.isupper() else c for c in name).lstrip("_")


# --- Sondas: cada una hace fallar la comparación ------------------------------------------------


def _app_without(route: NodeRoute) -> FastAPI:
    from vigia_platform.node_api.router import node_router

    units = tuple(
        UnitRegistration(
            unit.name,
            routers=(node_router(r for r in PUBLISHED_NODE_ROUTES if r is not route),)
            if unit.name == "node_api"
            else unit.routers,
            detail_codes=unit.detail_codes,
            labels=unit.labels,
        )
        for unit in platform_units()
    )
    assert any(unit.name == "node_api" for unit in units)
    return build_openapi_app(units)


def test_probe_an_unimplemented_skeleton_operation_fails(gate: NodeApiGate) -> None:
    differences = contract_differences(_app_without(NodeRoute.FINDING), gate=gate.operation)
    assert differences == ["post_finding: POST /api/nodes/findings sin implementar"]


def test_probe_a_missing_handler_fails(app: FastAPI, gate: NodeApiGate) -> None:
    def without_heartbeat(route: NodeRoute) -> object | None:
        return None if route is NodeRoute.HEARTBEAT else gate.operation(route)

    differences = contract_differences(app, gate=without_heartbeat)
    assert differences == ["post_heartbeat: la raíz no registra su manejador"]


def test_probe_a_different_response_schema_fails(app: FastAPI, gate: NodeApiGate) -> None:
    skeleton = copy.deepcopy(create_app().openapi())
    receipt = skeleton["paths"]["/findings"]["post"]["responses"]["200"]["content"]
    receipt["application/json"]["schema"] = {"$ref": "#/components/schemas/RejectionResponse"}
    differences = contract_differences(app, gate=gate.operation, skeleton=skeleton)
    assert differences == ["post_finding: la respuesta 200 es distinta"]


def test_probe_a_different_header_or_limit_fails(app: FastAPI, gate: NodeApiGate) -> None:
    skeleton = copy.deepcopy(create_app().openapi())
    heartbeat = skeleton["paths"]["/heartbeats"]["post"]
    heartbeat["x-vigia-max-bytes"] = heartbeat["x-vigia-max-bytes"] * 2
    heartbeat["parameters"] = heartbeat["parameters"][:1]
    differences = contract_differences(app, gate=gate.operation, skeleton=skeleton)
    assert sorted(differences) == [
        "post_heartbeat: `parameters` distinto del esqueleto",
        "post_heartbeat: `x-vigia-max-bytes` distinto del esqueleto",
    ]


def test_probe_a_different_body_model_fails(app: FastAPI, gate: NodeApiGate) -> None:
    specs = dict(SPECS)
    specs[NodeRoute.FINDING] = NodeOperationSpec(
        NodeRoute.FINDING, SPECS[NodeRoute.FINDING].operation, api.parse_finding
    )
    differences = contract_differences(app, specs=specs, gate=gate.operation)
    assert differences == ["post_finding: el modelo del cuerpo es distinto"]


def test_probe_a_different_rejection_status_fails(app: FastAPI, gate: NodeApiGate) -> None:
    def moved(route: NodeRoute | None, rejection: NodeRejection) -> int:
        if route is NodeRoute.UPDATE_RESULT and rejection.code is RejectionCode.RATE_LIMITED:
            return 503
        return status_of(route, rejection)

    differences = contract_differences(app, gate=gate.operation, status=moved)
    assert differences == ["post_update_result: `rate_limited` responde [503], el esqueleto [429]"]


def test_probe_the_optional_operation_mounted_fails(app: FastAPI, gate: NodeApiGate) -> None:
    skeleton = create_app().openapi()
    published = copy.deepcopy(app.openapi())
    published["paths"]["/api/nodes/conformance-profile"] = skeleton["paths"]["/conformance-profile"]
    mounted = FastAPI()
    mounted.routes.extend(app.routes)
    mounted.openapi = lambda: published  # type: ignore[method-assign]
    differences = contract_differences(mounted, gate=gate.operation)
    assert differences == [
        "get_conformance_profile: opcional y montada en /api/nodes/conformance-profile (A-51)"
    ]
