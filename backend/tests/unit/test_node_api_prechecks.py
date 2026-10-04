"""Verificación previa común de las rutas del contrato sobre la ruta de prueba interna (TASK-206).

La aplicación real (``create_app`` con la cadena fija) con los manejadores de prueba de
``tests/node_api_support.py`` y un almacén de identidad en memoria (las pruebas con PostgreSQL
están en ``tests/integration/test_node_api_identity.py``):

- rutas no canónicas (mayúsculas, ``%2F``, otros caracteres codificados, barra final) y rutas
  inexistentes: ``404`` sin cuerpo, con ``nosniff``, ``X-Vigia-Contract-Version`` y sin CORS;
- toda respuesta de nodo lleva ``nosniff`` y ``X-Vigia-Contract-Version`` y ninguna cabecera CORS;
- límites de cuerpo: 257 KB en hallazgos, 65 KB en latido, 17 KB en rotación y alta, cualquier
  cuerpo en la confirmación y un ``gzip`` que se infla responden ``payload_too_large`` sin leer más
  allá del límite más un byte (cuerpo entregado byte a byte);
- versión, certificado (cabeceras del balanceador coherentes con la hoja) y alcance (zona de la
  ruta entre las asignadas al nodo **ahora**; otra organización → ``node_zone_mismatch``);
- una consulta de identidad por petición y sin caché.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import MutableMapping
from typing import Any

import pytest
from cryptography import x509
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from vigia_contracts.models.api import parse_rejection_response

from tests.node_api_support import (
    DAY,
    VERSION,
    NodeWorld,
    alb_headers,
    gzip_bomb,
    node_world,
    zone_of,
)
from vigia_platform.identity.authz.context import NodeAssignment
from vigia_platform.node_api.certificate_profile import NodeSubject
from vigia_platform.shared.api.declarations import NodeRoute

CATALOG = "/api/nodes/zones/{zone}/catalog"
CONFIRMATION = "/api/nodes/clip-uploads/{clip}/confirmation"
CLIP_ID = "0192f0c4-0000-7000-8000-0000000000c1"


@pytest.fixture
def nodes() -> NodeWorld:
    return node_world()


def _client(nodes: NodeWorld) -> TestClient:
    return TestClient(nodes.app)


def _rejection(response: Any) -> Any:
    return parse_rejection_response(response.content)


def _node_headers_ok(response: Any) -> None:
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-vigia-contract-version"] == VERSION
    assert not any(name.lower().startswith("access-control-") for name in response.headers)


# --- Rutas y cabeceras ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/nodes/Findings",
        "/api/nodes/FINDINGS",
        "/api/nodes/zones/a%2Fb/catalog",
        "/api/nodes/zones/a%2fb/catalog",
        "/api/nodes/finding%73",
        "/api/nodes/findings/",
        "/api/nodes//findings",
        "/api/nodes",
        "/api/nodes/conformance-profile",
        "/api/nodes/no-existe",
        # La plantilla coincide, pero el identificador no está en forma canónica.
        "/api/nodes/zones/0192F0C4-0000-7000-8000-0000000000AA/catalog",
        "/api/nodes/clip-uploads/0192F0C4-0000-7000-8000-0000000000C1/confirmation",
    ],
)
def test_non_canonical_or_unknown_node_paths_are_not_found_without_body(
    nodes: NodeWorld, path: str
) -> None:
    with _client(nodes) as client:
        for method in ("GET", "POST"):
            response = client.request(method, path, headers=nodes.headers(nodes.a), content=b"{}")
            assert response.status_code == 404, (method, path)
            assert response.content == b""
            _node_headers_ok(response)
    assert nodes.probe.seen == []


def test_a_percent_encoded_slash_never_reaches_a_route(nodes: NodeWorld) -> None:
    zone = zone_of(nodes.a)
    scope_path = f"/api/nodes/zones/{zone}/catalog"
    raw = f"/api/nodes/zones/{zone}%2F/catalog".encode()
    status = _asgi(nodes, "GET", scope_path, raw_path=raw, headers=nodes.headers(nodes.a))
    assert status["status"] == 404 and nodes.probe.seen == []


def test_every_node_response_carries_nosniff_the_version_and_no_cors(nodes: NodeWorld) -> None:
    zone = zone_of(nodes.a)
    cors = {"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"}
    with _client(nodes) as client:
        accepted = client.get(CATALOG.format(zone=zone), headers={**nodes.headers(nodes.a), **cors})
        rejected = client.get(CATALOG.format(zone=zone), headers=nodes.headers(None))
        preflight = client.options(CATALOG.format(zone=zone), headers=cors)
    assert accepted.status_code == 200
    assert rejected.status_code == 401
    for response in (accepted, rejected, preflight):
        _node_headers_ok(response)


# --- Límites de cuerpo ---------------------------------------------------------------------------


def _asgi(
    nodes: NodeWorld,
    method: str,
    path: str,
    *,
    chunks: list[bytes] | None = None,
    headers: dict[str, str] | None = None,
    raw_path: bytes | None = None,
) -> dict[str, Any]:
    """Una petición ASGI cuyo cuerpo llega en ``chunks``; cuenta los fragmentos leídos."""
    pending = list(chunks or [])
    consumed: list[int] = []
    result: dict[str, Any] = {"body": b""}

    async def receive() -> MutableMapping[str, Any]:
        if pending:
            chunk = pending.pop(0)
            consumed.append(len(chunk))
            return {"type": "http.request", "body": chunk, "more_body": bool(pending)}
        await asyncio.sleep(3600)  # pragma: no cover - nadie debe esperar más cuerpo
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.start":
            result["status"] = message["status"]
            result["headers"] = {
                bytes(k).decode().lower(): bytes(v).decode() for k, v in message["headers"]
            }
        elif message["type"] == "http.response.body":
            result["body"] += message.get("body", b"")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": raw_path if raw_path is not None else path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", b"nodes.vigia.test"),
            *((k.lower().encode(), v.encode()) for k, v in (headers or {}).items()),
        ],
        "client": ("192.0.2.10", 50000),
        "server": ("nodes.vigia.test", 443),
        "state": {},
    }

    # Sin arranque supervisado (lifespan): la verificación previa no depende de él.
    asyncio.run(nodes.app(scope, receive, send))
    result["consumed"] = sum(consumed)
    return result


@pytest.mark.parametrize(
    ("route", "size"),
    [
        (NodeRoute.FINDING, 257 * 1024),
        (NodeRoute.HEARTBEAT, 65 * 1024),
        (NodeRoute.CREDENTIAL_ROTATION, 17 * 1024),
        (NodeRoute.ENROLLMENT, 17 * 1024),
        (NodeRoute.CLIP_CONFIRMATION, 1),
    ],
)
def test_an_oversized_body_is_payload_too_large_reading_at_most_limit_plus_one(
    nodes: NodeWorld, route: NodeRoute, size: int
) -> None:
    path = route.path.replace("{clip_id}", CLIP_ID)
    node = None if route is NodeRoute.ENROLLMENT else nodes.a
    # Sin Content-Length y byte a byte: la cadena deja de leer en el primero que sobra.
    response = _asgi(nodes, route.method, path, chunks=[b" "] * size, headers=nodes.headers(node))
    assert response["status"] == 413, response
    assert parse_rejection_response(response["body"]).code.value == "payload_too_large"
    assert response["consumed"] <= route.max_body_bytes + 1
    assert nodes.probe.seen == []


@pytest.mark.parametrize("route", [NodeRoute.FINDING, NodeRoute.HEARTBEAT, NodeRoute.ENROLLMENT])
def test_a_declared_content_length_over_the_limit_is_not_read_at_all(
    nodes: NodeWorld, route: NodeRoute
) -> None:
    node = None if route is NodeRoute.ENROLLMENT else nodes.a
    headers = {**nodes.headers(node), "Content-Length": str(route.max_body_bytes + 1)}
    response = _asgi(
        nodes, route.method, route.path, chunks=[b" "] * (route.max_body_bytes + 1), headers=headers
    )
    assert response["status"] == 413 and response["consumed"] == 0


@pytest.mark.parametrize("route", [NodeRoute.FINDING, NodeRoute.HEARTBEAT, NodeRoute.ENROLLMENT])
def test_a_gzip_that_inflates_over_the_limit_is_payload_too_large(
    nodes: NodeWorld, route: NodeRoute
) -> None:
    node = None if route is NodeRoute.ENROLLMENT else nodes.a
    bomb = gzip_bomb(route.max_body_bytes)
    assert len(bomb) < route.max_body_bytes
    with _client(nodes) as client:
        response = client.request(
            route.method,
            route.path,
            headers={**nodes.headers(node), "Content-Encoding": "gzip"},
            content=bomb,
        )
    assert response.status_code == 413
    assert _rejection(response).code.value == "payload_too_large"


def test_a_body_at_the_limit_passes_the_size_step(nodes: NodeWorld) -> None:
    route = NodeRoute.HEARTBEAT
    with _client(nodes) as client:
        response = client.post(
            route.path, headers=nodes.headers(nodes.a), content=b" " * route.max_body_bytes
        )
    # Pasa el tamaño y lo rechaza el esquema (un cuerpo en blanco no es JSON).
    assert response.status_code == 400 and _rejection(response).code.value == "schema_invalid"


@pytest.mark.parametrize(
    ("route", "encoding"),
    [
        (NodeRoute.FINDING, "br"),
        (NodeRoute.FINDING, "gzip, gzip"),
        (NodeRoute.ZONE_CATALOG, "gzip"),
    ],
)
def test_an_encoding_the_operation_does_not_admit_is_schema_invalid(
    nodes: NodeWorld, route: NodeRoute, encoding: str
) -> None:
    path = route.path.replace("{zone_id}", str(zone_of(nodes.a)))
    with _client(nodes) as client:
        response = client.request(
            route.method,
            path,
            headers={**nodes.headers(nodes.a), "Content-Encoding": encoding},
            content=b"",
        )
    rejection = _rejection(response)
    assert (response.status_code, rejection.code.value, rejection.field) == (
        400,
        "schema_invalid",
        "Content-Encoding",
    )


# --- Versión -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "status", "code", "result"),
    [
        (None, 400, "contract_version_unsupported", None),
        ("uno", 400, "schema_invalid", None),
        ("99.0.0", 400, "contract_version_unsupported", "rejected_major"),
    ],
)
def test_the_version_step(
    nodes: NodeWorld, version: str | None, status: int, code: str, result: str | None
) -> None:
    with _client(nodes) as client:
        response = client.get(
            CATALOG.format(zone=zone_of(nodes.a)), headers=nodes.headers(nodes.a, version=version)
        )
    rejection = _rejection(response)
    assert (response.status_code, rejection.code.value) == (status, code)
    assert rejection.field == "X-Vigia-Contract-Version"
    assert (
        None if rejection.compatibility_result is None else rejection.compatibility_result.value
    ) == result


def test_a_newer_minor_is_unsupported_with_rejected_newer_never_as_a_code(nodes: NodeWorld) -> None:
    major, minor, _ = (int(part) for part in VERSION.split("-")[0].split("+")[0].split("."))
    with _client(nodes) as client:
        response = client.get(
            CATALOG.format(zone=zone_of(nodes.a)),
            headers=nodes.headers(nodes.a, version=f"{major}.{minor + 1}.0"),
        )
    rejection = _rejection(response)
    assert rejection.code.value == "contract_version_unsupported"
    assert rejection.compatibility_result.value == "rejected_newer"
    assert rejection.retryable is False
    assert response.json()["code"] != "rejected_newer"


# --- Certificado y alcance ---------------------------------------------------------------------


def test_an_enrolled_node_with_its_zone_passes_with_one_identity_lookup(nodes: NodeWorld) -> None:
    zone = zone_of(nodes.a)
    with _client(nodes) as client:
        first = client.get(CATALOG.format(zone=zone), headers=nodes.headers(nodes.a))
        second = client.get(CATALOG.format(zone=zone), headers=nodes.headers(nodes.a))
    assert first.status_code == second.status_code == 200
    assert first.json()["node_id"] == str(nodes.a.node_id)
    assert first.json()["zones"] == [str(zone)]
    # Una consulta por petición y nada guardado entre peticiones.
    assert len(nodes.store.lookups) == 2


def test_a_revocation_is_seen_by_the_next_request(nodes: NodeWorld) -> None:
    zone = zone_of(nodes.a)
    with _client(nodes) as client:
        assert (
            client.get(CATALOG.format(zone=zone), headers=nodes.headers(nodes.a)).status_code == 200
        )
        nodes.a.node_status = "revoked"
        nodes.a.revoked_at = nodes.now
        response = client.get(CATALOG.format(zone=zone), headers=nodes.headers(nodes.a))
    assert (response.status_code, _rejection(response).code.value) == (401, "node_revoked")


def test_a_zone_of_another_organization_is_node_zone_mismatch(nodes: NodeWorld) -> None:
    with _client(nodes) as client:
        response = client.get(CATALOG.format(zone=zone_of(nodes.b)), headers=nodes.headers(nodes.a))
    rejection = _rejection(response)
    assert (response.status_code, rejection.code.value, rejection.field) == (
        403,
        "node_zone_mismatch",
        "zone_id",
    )
    assert nodes.probe.seen == []


def test_only_the_zones_assigned_now_are_in_scope(nodes: NodeWorld) -> None:
    now = nodes.now
    past, future = uuid.uuid4(), uuid.uuid4()
    nodes.a.assignments.append(NodeAssignment(past, now - 10 * DAY, now - DAY))
    nodes.a.assignments.append(NodeAssignment(future, now + DAY, None))
    with _client(nodes) as client:
        for zone in (past, future, uuid.uuid4()):
            response = client.get(CATALOG.format(zone=zone), headers=nodes.headers(nodes.a))
            assert _rejection(response).code.value == "node_zone_mismatch", zone
        own = client.get(CATALOG.format(zone=zone_of(nodes.a)), headers=nodes.headers(nodes.a))
    assert own.json()["zones"] == [str(zone_of(nodes.a))]


def test_a_malformed_zone_identifier_is_out_of_scope(nodes: NodeWorld) -> None:
    with _client(nodes) as client:
        response = client.get(CATALOG.format(zone="no-es-uuid"), headers=nodes.headers(nodes.a))
    assert _rejection(response).code.value == "node_zone_mismatch"


def test_a_plant_other_than_the_certificate_is_node_zone_mismatch(nodes: NodeWorld) -> None:
    nodes.a.plant_id = uuid.uuid4()  # el nodo cambió de planta: el certificado dice otra
    with _client(nodes) as client:
        response = client.get(CATALOG.format(zone=zone_of(nodes.a)), headers=nodes.headers(nodes.a))
    assert (response.status_code, _rejection(response).code.value) == (403, "node_zone_mismatch")


@pytest.mark.parametrize(
    "tamper",
    [
        "missing_leaf",
        "repeated_serial",
        "other_serial",
        "other_subject",
        "other_issuer",
        "other_validity",
        "garbled_leaf",
        "subject_profile",
        "expired_leaf",
    ],
)
def test_incoherent_balancer_headers_are_node_not_enrolled(nodes: NodeWorld, tamper: str) -> None:
    headers = nodes.headers(nodes.a)
    raw = list(headers.items())
    if tamper == "missing_leaf":
        del headers["X-Amzn-Mtls-Clientcert-Leaf"]
        raw = list(headers.items())
    elif tamper == "repeated_serial":
        raw.append(("X-Amzn-Mtls-Clientcert-Serial-Number", "01"))
    elif tamper == "other_serial":
        headers["X-Amzn-Mtls-Clientcert-Serial-Number"] = "0A"
        raw = list(headers.items())
    elif tamper == "other_subject":
        headers["X-Amzn-Mtls-Clientcert-Subject"] = f"CN={uuid.uuid4()},OU={nodes.a.plant_id}"
        raw = list(headers.items())
    elif tamper == "other_issuer":
        headers["X-Amzn-Mtls-Clientcert-Issuer"] = "CN=Otra CA,O=Otra"
        raw = list(headers.items())
    elif tamper == "other_validity":
        headers["X-Amzn-Mtls-Clientcert-Validity"] = (
            "NotBefore=2020-01-01T00:00:00Z;NotAfter=2030-01-01T00:00:00Z"
        )
        raw = list(headers.items())
    elif tamper == "garbled_leaf":
        headers["X-Amzn-Mtls-Clientcert-Leaf"] = "-----BEGIN%20CERTIFICATE-----%0Axx"
        raw = list(headers.items())
    elif tamper in ("subject_profile", "expired_leaf"):
        now = nodes.now
        subject = NodeSubject(nodes.a.node_id, nodes.a.organization_id, nodes.a.plant_id)
        name = (
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, str(nodes.a.node_id))])
            if tamper == "subject_profile"
            else None
        )
        window = (
            (now - 400 * DAY, now - DAY) if tamper == "expired_leaf" else (now - DAY, now + DAY)
        )
        certificate = nodes.authority.leaf(
            subject, not_before=window[0], not_after=window[1], name=name
        )
        nodes.a.credentials[format(certificate.serial_number, "x")] = {
            "status": "active",
            "issued_at": now - DAY,
            "expires_at": now + DAY,
        }
        raw = list({**alb_headers(certificate), "X-Vigia-Contract-Version": VERSION}.items())
    zone = zone_of(nodes.a)
    with _client(nodes) as client:
        response = client.get(CATALOG.format(zone=zone), headers=raw)
    assert (response.status_code, _rejection(response).code.value) == (401, "node_not_enrolled")
    assert nodes.probe.seen == []


def test_enrollment_ignores_balancer_headers_and_has_no_node(nodes: NodeWorld) -> None:
    with _client(nodes) as client:
        response = client.post(
            NodeRoute.ENROLLMENT.path,
            headers={**nodes.headers(nodes.a), "X-Amzn-Mtls-Clientcert-Leaf": "basura"},
            content=b"{}",
        )
    # Sin certificado: pasa versión y tamaño y lo rechaza el esquema (el cuerpo no es un alta).
    rejection = _rejection(response)
    assert (response.status_code, rejection.code.value) == (422, "schema_invalid")


def test_a_confirmation_requires_a_uuid7_clip(nodes: NodeWorld) -> None:
    with _client(nodes) as client:
        bad = client.post(CONFIRMATION.format(clip="no-uuid"), headers=nodes.headers(nodes.a))
        good = client.post(CONFIRMATION.format(clip=CLIP_ID), headers=nodes.headers(nodes.a))
    assert (bad.status_code, _rejection(bad).code.value, _rejection(bad).field) == (
        422,
        "schema_invalid",
        "clip_id",
    )
    assert good.status_code == 200


def test_the_clock_decides_the_leaf_validity(nodes: NodeWorld) -> None:
    nodes.world.clock.advance(dt.timedelta(days=400).total_seconds())
    with _client(nodes) as client:
        response = client.get(CATALOG.format(zone=zone_of(nodes.a)), headers=nodes.headers(nodes.a))
    assert _rejection(response).code.value == "node_not_enrolled"
