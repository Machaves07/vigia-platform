"""Plantillas de ruta en la lista cerrada ``route``/``http.route`` (NFR-GOB-54, 25; VIG-184).

``redact_text`` toma por un token cualquier tira de 20 o más caracteres del alfabeto de token, y
``/api/nodes/heartbeats`` lo es; si la fábrica de la aplicación solo registra lo que
``register`` admite, ocho de las diez rutas del contrato salen como ``other`` en
``node_requests_total`` y como ``[redactado]`` en la línea de registro de la petición.
``register_routes`` valida las plantillas declaradas tramo a tramo:

- las diez ``NodeRoute.path`` salen con su ruta en la métrica y en el registro;
- toda ruta declarada de la aplicación (también las de personas) queda en la lista cerrada;
- un tramo con forma de token (dígitos, mayúsculas, ``_``, ``+``, ``=``), un enlace o un tramo
  vacío no es una plantilla; la redacción del texto libre no cambia.
"""

from __future__ import annotations

import io
import json
import logging
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from tests.api_support import World
from tests.dispatch_support import metric_points
from tests.node_api_support import node_world, zone_of
from vigia_platform.node_api.observability import NodeResult
from vigia_platform.shared.api.declarations import NodeRoute, iter_declared_routes
from vigia_platform.shared.observability.logging import JsonFormatter
from vigia_platform.shared.observability.metrics import MetricName, PlatformMetrics
from vigia_platform.shared.observability.redaction import (
    OTHER,
    REDACTED,
    AttributePolicy,
    is_route_template,
    redact_text,
)

CLIP_ID = "0192f0c4-0000-7000-8000-0000000000c1"


@pytest.fixture
def node_log() -> Iterator[io.StringIO]:
    """Las líneas JSON del registro ``vigia.node_api`` (el formateador de la plataforma)."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("vigia.node_api")
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield stream
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def _concrete(route: NodeRoute, zone: uuid.UUID) -> str:
    return route.path.replace("{zone_id}", str(zone)).replace("{clip_id}", CLIP_ID)


def test_every_node_route_keeps_its_template_in_the_metric_and_the_log(
    node_log: io.StringIO,
) -> None:
    policy = AttributePolicy()
    # Lo que ``node_api.observability`` registra al importarse en la política del proceso.
    policy.register("result", [result.value for result in NodeResult])
    reader = InMemoryMetricReader()
    metrics = PlatformMetrics(MeterProvider(metric_readers=[reader]).get_meter("pruebas"), policy)
    # Las mismas métricas en la cadena (rechazos previos) y en la verificación previa.
    nodes = node_world(runtime={"attribute_policy": policy, "metrics": metrics}, metrics=metrics)
    handler = logging.getLogger("vigia.node_api").handlers[-1]
    handler.setFormatter(JsonFormatter(policy))
    zone = zone_of(nodes.a)
    with TestClient(nodes.app) as client:
        for route in NodeRoute:
            client.request(route.method, _concrete(route, zone), headers=nodes.headers(nodes.a))

    expected = {route.path for route in NodeRoute}
    points = metric_points(reader, MetricName.NODE_REQUESTS_TOTAL)
    assert sum(value for _, value in points) == len(NodeRoute)
    assert all(OTHER not in attributes.values() for attributes, _ in points)
    assert {str(attributes["route"]) for attributes, _ in points} == expected

    logged = [json.loads(line) for line in node_log.getvalue().splitlines()]
    assert REDACTED not in {line.get("route") for line in logged}
    assert {line["route"] for line in logged} == expected


def test_every_declared_route_of_the_platform_is_in_the_closed_list() -> None:
    policy = AttributePolicy()
    app = World().app(runtime={"attribute_policy": policy})
    declared = {route.path for route in iter_declared_routes(app.routes) if route.is_api_route}
    assert declared  # la aplicación real, con las rutas de personas y de nodo
    missing = declared - policy.values("route")
    assert not missing
    assert declared <= policy.values("http.route")
    assert {route.path for route in NodeRoute} <= policy.values("route")


@pytest.mark.parametrize("route", list(NodeRoute), ids=lambda route: route.name)
def test_each_node_route_is_a_template(route: NodeRoute) -> None:
    assert is_route_template(route.path)
    policy = AttributePolicy()
    policy.register_routes([route.path])
    assert policy.clean({"route": route.path}) == {"route": route.path}
    assert policy.clean({"http.route": route.path}) == {"http.route": route.path}


@pytest.mark.parametrize(
    "value",
    [
        "/api/nodes/AbCdEfGhIjKlMnOpQrStUv",  # mayúsculas
        "/api/nodes/abcdefghij0123456789",  # dígitos
        "/api/nodes/clip-0123456789abcdef",  # dígitos tras el guion
        "/api/nodes/clip-AbCdEfGhIj",  # mayúsculas tras el guion
        "/api/nodes/{zone_id}-{clip_id}",
        "/api/nodes/abc_defghijklmnopqrstu",  # «_»
        "/api/nodes/abc+def",
        "/api/nodes/abc=def",
        "/api/nodes//heartbeats",  # tramo vacío
        "/api/nodes/heartbeats/",
        "/api/nodes/-heartbeats",
        "/api/nodes/heartbeats-",
        "/api/nodes/{ZONE}",
        "/api/nodes/{zone_id",
        "/api/nodes/{zone id}",
        "api/nodes/heartbeats",  # sin barra inicial
        "https://example.com/api",
        "/api/nodes/heart beats",
        "/api/nodes/h\u0435artbeats",  # e cirílica (U+0435)
        "/api/nodes/heartbeats\u200b",
        "/" + "/".join(["abc"] * 40),  # más de 128 caracteres
        "/" + "a" * 33,  # una palabra más larga que la de cualquier ruta
        "",
    ],
)
def test_a_token_link_or_free_text_is_not_a_route_template(value: str) -> None:
    assert not is_route_template(value)
    policy = AttributePolicy()
    with pytest.raises(ValueError, match="plantilla de ruta no admitida"):
        policy.register_routes([value])
    assert policy.values("route") == frozenset()


def test_registering_routes_does_not_weaken_free_text_redaction() -> None:
    policy = AttributePolicy()
    policy.register_routes([route.path for route in NodeRoute])
    assert redact_text("/api/nodes/heartbeats") == REDACTED
    assert redact_text("credential-rotations") == REDACTED
    # Un valor no registrado, aunque tenga forma de plantilla, sigue saliendo como ``other``.
    assert policy.clean({"route": "/api/nodes/unregistered-route"}) == {"route": OTHER}
