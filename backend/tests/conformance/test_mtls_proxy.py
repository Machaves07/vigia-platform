"""El balanceador local de la suite (``mtls_proxy``) se comporta como los de U-03 (§5).

Contra una aplicación de eco (devuelve las cabeceras que recibe), sin contenedores:

- **Descarta siempre** las ``X-Amzn-Mtls-*`` del cliente: con certificado, la aplicación ve solo
  las cinco del balanceador, calculadas de la hoja verificada; sin certificado (``nodes.``) o en
  ``app.``, ninguna. La sonda con el descarte apagado demuestra que la prueba detecta el reenvío.
- Un certificado de otra autoridad no completa el saludo TLS (modo *verify*).
- Reparto alterno **por petición**, también dentro de una conexión persistente.

Solo datos generados.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import ssl
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import httpx
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from starlette.types import Receive, Scope, Send

from tests.conformance.mtls_proxy import ServerTls, mtls_proxy, server_tls
from tests.node_api_support import alb_headers
from tests.resilience.harness import WALL, free_port
from vigia_platform.node_api.certificate_profile import NodeSubject, subject_name

pytestmark = pytest.mark.integration

FORGED: Final = {
    "X-Amzn-Mtls-Clientcert-Leaf": "hoja-falsa",
    "X-Amzn-Mtls-Clientcert-Serial-Number": "DEADBEEF",
    "X-Amzn-Mtls-Clientcert-Subject": "CN=otro-nodo",
    "X-Amzn-Mtls-Clientcert-Issuer": "CN=otra-autoridad",
    "X-Amzn-Mtls-Clientcert-Validity": (
        "NotBefore=2020-01-01T00:00:00Z;NotAfter=2099-01-01T00:00:00Z"
    ),
    "x-amzn-mtls-otra": "cualquiera",
}
START_SECONDS: Final = 30.0
OBJECT_SIZE: Final = 123_456
"""Lo que la aplicación de eco anuncia en un ``HEAD`` (como S3: el tamaño del objeto)."""


async def _echo(scope: Scope, receive: Receive, send: Send) -> None:
    """Aplicación de eco: el cuerpo de la respuesta son las cabeceras recibidas."""
    if scope["type"] != "http":
        return
    headers = [
        [name.decode("latin-1"), value.decode("latin-1")] for name, value in scope["headers"]
    ]
    body = httpx.Response(200, json=headers).content
    size = OBJECT_SIZE if scope["method"] == "HEAD" else len(body)
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(size).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": b"" if scope["method"] == "HEAD" else body})


@contextlib.contextmanager
def echo_backend() -> Iterator[str]:
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(_echo, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = WALL.monotonic() + START_SECONDS
    while not server.started:
        assert WALL.monotonic() < deadline, "la aplicación de eco no arrancó"
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=START_SECONDS)


def _authority(common_name: str) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = WALL.now()
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, certificate


def _leaf(
    authority: tuple[ec.EllipticCurvePrivateKey, x509.Certificate], directory: Path, name: str
) -> tuple[x509.Certificate, tuple[str, str]]:
    ca_key, ca = authority
    key = ec.generate_private_key(ec.SECP256R1())
    now = WALL.now()
    subject = NodeSubject(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject_name(subject))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    certificate_file, key_file = directory / f"{name}.crt", directory / f"{name}.key"
    certificate_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return certificate, (str(certificate_file), str(key_file))


class World:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.tls: ServerTls = server_tls(directory, WALL.now())
        self.authority = _authority("Vigia Node CA test-only")
        self.node_ca = directory / "node-ca.crt"
        self.node_ca.write_bytes(self.authority[1].public_bytes(serialization.Encoding.PEM))
        self.certificate, self.cert = _leaf(self.authority, directory, "nodo")

    def client(self, cert: tuple[str, str] | None = None) -> httpx.Client:
        context = ssl.create_default_context(cafile=str(self.tls.ca_file))
        if cert is not None:
            context.load_cert_chain(*cert)
        return httpx.Client(verify=context, timeout=60.0)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def _mtls(received: list[list[str]]) -> dict[str, str]:
    return {name: value for name, value in received if name.startswith("x-amzn-mtls-")}


def _expected(certificate: x509.Certificate) -> dict[str, str]:
    return {name.lower(): value for name, value in alb_headers(certificate).items()}


def test_with_a_certificate_only_the_balancer_headers_reach_the_application(world: World) -> None:
    with (
        echo_backend() as backend,
        mtls_proxy("nodes", [backend], world.tls, client_ca=world.node_ca) as proxy,
        world.client(world.cert) as client,
    ):
        received = client.post(proxy.url + "/api/nodes/findings", headers=FORGED).json()
    assert _mtls(received) == _expected(world.certificate)
    (exchange,) = proxy.exchanges
    assert exchange.client_certificate
    assert sorted(exchange.dropped_mtls) == sorted(name.lower() for name in FORGED)


@pytest.mark.parametrize("listener", ["nodes", "app"])
def test_without_a_certificate_no_mtls_header_reaches_the_application(
    world: World, listener: str
) -> None:
    client_ca = world.node_ca if listener == "nodes" else None
    with (
        echo_backend() as backend,
        mtls_proxy(listener, [backend], world.tls, client_ca=client_ca) as proxy,
        world.client() as client,
    ):
        received = client.post(proxy.url + "/api/nodes/enrollment", headers=FORGED).json()
    assert _mtls(received) == {}


def test_the_app_listener_never_forwards_a_certificate(world: World) -> None:
    with (
        echo_backend() as backend,
        mtls_proxy("app", [backend], world.tls) as proxy,
        world.client(world.cert) as client,
    ):
        received = client.post(proxy.url + "/api/nodes/enrollment", headers=FORGED).json()
    assert _mtls(received) == {}


def test_probe_forwarding_the_client_headers_is_detected(world: World) -> None:
    """Sonda: con el descarte apagado, las cabeceras del cliente llegan y la comprobación de
    ``test_without_a_certificate_no_mtls_header_reaches_the_application`` fallaría."""
    with (
        echo_backend() as backend,
        mtls_proxy(
            "nodes", [backend], world.tls, client_ca=world.node_ca, strip_client_mtls=False
        ) as proxy,
        world.client() as client,
    ):
        received = client.post(proxy.url + "/api/nodes/findings", headers=FORGED).json()
    assert _mtls(received) == {name.lower(): value for name, value in FORGED.items()}


def test_a_certificate_of_another_authority_does_not_complete_the_handshake(
    world: World,
) -> None:
    _, foreign = _leaf(_authority("Otra autoridad"), world.directory, "ajeno")
    with (
        echo_backend() as backend,
        mtls_proxy("nodes", [backend], world.tls, client_ca=world.node_ca) as proxy,
        world.client(foreign) as client,
        pytest.raises(httpx.HTTPError),
    ):
        client.post(proxy.url + "/api/nodes/findings")
    assert proxy.exchanges == []


def test_requests_alternate_between_processes_on_one_connection(world: World) -> None:
    with (
        echo_backend() as first,
        echo_backend() as second,
        mtls_proxy("nodes", [first, second], world.tls, client_ca=world.node_ca) as proxy,
        world.client(world.cert) as client,
    ):
        statuses = [client.get(proxy.url + "/api/nodes/heartbeats").status_code for _ in range(4)]
    assert statuses == [200] * 4
    assert [exchange.backend for exchange in proxy.exchanges] == [0, 1, 0, 1]


def test_a_head_keeps_the_content_length_of_the_application(world: World) -> None:
    """S3 anuncia el tamaño del objeto en el ``Content-Length`` de un ``HEAD`` sin cuerpo: el
    balanceador no lo reescribe (la verificación de un clip lee ese tamaño)."""
    with (
        echo_backend() as backend,
        mtls_proxy("aws", [backend], world.tls, preserve_host=True) as proxy,
        world.client() as client,
    ):
        response = client.head(proxy.url + "/vigia-evidence/objeto")
    assert response.status_code == 200
    assert response.headers["content-length"] == str(OBJECT_SIZE)
    assert response.content == b""


def test_the_storage_listener_keeps_the_host_of_the_signature(world: World) -> None:
    with (
        echo_backend() as backend,
        mtls_proxy("aws", [backend], world.tls, preserve_host=True) as proxy,
        world.client() as client,
    ):
        received = dict(client.get(proxy.url + "/vigia-evidence/objeto").json())
    assert received["host"] == proxy.url.removeprefix("https://")
