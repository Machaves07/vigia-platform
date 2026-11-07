"""``vigia-mtls-edge`` (TASK-542): los balanceadores del entorno de integración.

Contra una aplicación de eco o la aplicación real de ``node_api`` en proceso, sin contenedores: la
prueba con la plataforma entera (base, LocalStack, autoridad real) es
``tests/conformance/test_mtls_edge_platform.py``.

Solo datos generados: autoridades efímeras de cada prueba.
"""

from __future__ import annotations

import ast
import contextlib
import datetime as dt
import http.server
import json
import os
import re
import signal
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import fields, replace
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from starlette.types import Receive, Scope, Send

from tests.conformance.mtls_proxy import server_tls
from tests.conformance.test_mtls_proxy import FORGED, _authority, _leaf, echo_backend
from tests.node_api_support import DAY, VERSION, NodeWorld, node_world, zone_of
from tests.resilience.harness import WALL, free_port
from tools.mtls_edge import (
    EXCHANGES_KEPT,
    UPSTREAM_ERROR_HEADER,
    EdgeConfig,
    EdgeError,
    Exchange,
    MtlsEdge,
    alb_headers,
    certificate_sha256,
    init_server_certificate,
    main,
    read_client_roots,
    resolve_bind,
    validated_roots,
)
from vigia_platform.node_api.certificate_profile import NodeSubject, subject_name
from vigia_platform.shared.api.declarations import NodeRoute

BACKEND: Final = Path(__file__).resolve().parents[2]
REPOSITORY: Final = BACKEND.parent
DOMAIN: Final = "integracion.test"
DNS_NAMES: Final = (f"app.{DOMAIN}", f"nodes.{DOMAIN}")
LOOPBACK: Final = "127.0.0.1"
FIVE_HEADERS: Final = (
    "x-amzn-mtls-clientcert-leaf",
    "x-amzn-mtls-clientcert-serial-number",
    "x-amzn-mtls-clientcert-subject",
    "x-amzn-mtls-clientcert-issuer",
    "x-amzn-mtls-clientcert-validity",
)
WAIT_SECONDS: Final = 60.0
SILENT_CLIENT_SECONDS: Final = 90.0
"""Plazo de ``recv`` frente al tope de saludo de 30 s: solo se agota si ese tope falta o crece."""
"""Tope de las esperas por sondeo: nunca decide la prueba, que acaba en cuanto se cumple."""
SECRET_MARKER: Final = "marcador-que-no-debe-registrarse"  # noqa: S105
HEARTBEATS: Final = "/api/nodes/heartbeats"


class Pki:
    """Una autoridad de nodos efímera con una hoja de cliente en disco."""

    def __init__(self, directory: Path, name: str) -> None:
        self.authority = _authority(f"Autoridad {name}")
        self.root = self.authority[1].public_bytes(serialization.Encoding.PEM)
        self.certificate, self.cert = _leaf(self.authority, directory, f"nodo-{name}")


def _write_root(path: Path, *pkis: Pki) -> None:
    path.write_bytes(b"".join(pki.root for pki in pkis))


@pytest.fixture
def directory(tmp_path: Path) -> Path:
    init_server_certificate(
        tmp_path / "edge.crt",
        tmp_path / "edge.key",
        tmp_path / "edge-ca.pem",
        dns_names=DNS_NAMES,
        addresses=(LOOPBACK,),
        now=WALL.now(),
    )
    return tmp_path


def _config(directory: Path, backends: tuple[str, ...], **changes: Any) -> EdgeConfig:
    config = EdgeConfig(
        bind=LOOPBACK,
        backends=backends,
        server_cert=directory / "edge.crt",
        server_key=directory / "edge.key",
        server_ca=directory / "edge-ca.pem",
        client_ca=str(directory / "root.pem"),
        app_port=0,
        nodes_port=0,
    )
    return replace(config, **changes)


@contextlib.contextmanager
def running(config: EdgeConfig, **options: Any) -> Iterator[MtlsEdge]:
    edge = MtlsEdge(config, **options)
    edge.start()
    try:
        yield edge
    finally:
        edge.close()


def _client(directory: Path, cert: tuple[str, str] | None = None) -> httpx.Client:
    context = ssl.create_default_context(cafile=str(directory / "edge-ca.pem"))
    if cert is not None:
        context.load_cert_chain(*cert)
    return httpx.Client(verify=context, timeout=60.0)


def _completes(edge: MtlsEdge, directory: Path, cert: tuple[str, str] | None) -> bool:
    """Una conexión nueva: ``True`` si el saludo termina y la aplicación responde."""
    with _client(directory, cert) as client:
        try:
            return client.post(edge.nodes.url + HEARTBEATS).status_code == 200
        except httpx.HTTPError:
            return False


def _names(received: list[list[str]]) -> list[str]:
    return [name for name, _ in received]


# --- Las cabeceras del balanceador ---------------------------------------------------------------


def test_nodes_delivers_each_of_the_five_headers_exactly_once(
    directory: Path,
) -> None:
    pki = Pki(directory, "propia")
    _write_root(directory / "root.pem", pki)
    with (
        echo_backend() as backend,
        running(_config(directory, (backend,))) as edge,
        _client(directory, pki.cert) as client,
    ):
        received = client.post(edge.nodes.url + HEARTBEATS, headers=FORGED).json()
    assert sorted(name for name in _names(received) if name.startswith("x-amzn-mtls-")) == sorted(
        FIVE_HEADERS
    )
    expected = {name.lower(): value for name, value in alb_headers(pki.certificate).items()}
    assert {name: value for name, value in received if name.startswith("x-amzn-mtls-")} == expected


@pytest.mark.parametrize("listener", ["app", "nodes"])
def test_forged_mtls_headers_are_dropped_on_both_listeners(directory: Path, listener: str) -> None:
    pki = Pki(directory, "propia")
    _write_root(directory / "root.pem", pki)
    with (
        echo_backend() as backend,
        running(_config(directory, (backend,))) as edge,
        _client(directory) as client,
    ):
        url = (edge.nodes if listener == "nodes" else edge.app).url
        received = client.post(url + "/api/x", headers=FORGED).json()
    assert [name for name in _names(received) if name.startswith("x-amzn-mtls-")] == []


def test_the_app_listener_never_forwards_a_client_certificate(directory: Path) -> None:
    pki = Pki(directory, "propia")
    _write_root(directory / "root.pem", pki)
    with (
        echo_backend() as backend,
        running(_config(directory, (backend,))) as edge,
        _client(directory, pki.cert) as client,
    ):
        received = client.post(edge.app.url + "/api/x").json()
    assert [name for name in _names(received) if name.startswith("x-amzn-mtls-")] == []


@pytest.mark.parametrize("listener", ["app", "nodes"])
def test_forwarded_headers_come_from_the_edge_not_from_the_client(
    directory: Path, listener: str
) -> None:
    _write_root(directory / "root.pem", Pki(directory, "propia"))
    with (
        echo_backend() as backend,
        running(_config(directory, (backend,))) as edge,
        _client(directory) as client,
    ):
        received = client.get(
            (edge.nodes if listener == "nodes" else edge.app).url + "/api/x",
            headers={
                "X-Forwarded-For": "203.0.113.9",
                "X-Forwarded-Proto": "http",
                "X-Forwarded-Port": "81",
                "X-Forwarded-Host": "otra.invalid",
            },
        ).json()
    forwarded = sorted((name, value) for name, value in received if name.startswith("x-forwarded-"))
    assert forwarded == [("x-forwarded-for", LOOPBACK), ("x-forwarded-proto", "https")]


async def _cookie_app(scope: Scope, receive: Receive, send: Send) -> None:
    """``/login`` da una cookie, con ``Secure`` por ``https`` como la de sesión real; lo demás,
    un eco de las cabeceras recibidas."""
    if scope["type"] != "http":
        return
    headers = [(b"content-type", b"application/json")]
    if scope["path"] == "/login":
        cookie = (
            b"__Host-sesion=sintetica; Secure; HttpOnly; SameSite=Strict; Path=/"
            if scope["scheme"] == "https"
            else b"sesion=sintetica; Path=/"
        )
        headers.append((b"set-cookie", cookie))
    received = [
        [name.decode("latin-1"), value.decode("latin-1")] for name, value in scope["headers"]
    ]
    body = httpx.Response(200, json=received).content
    headers.append((b"content-length", str(len(body)).encode()))
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    await send({"type": "http.response.body", "body": body})


@contextlib.contextmanager
def cookie_backend(
    directory: Path, scheme: str, seen: list[str] | None = None
) -> Iterator[tuple[str, ssl.SSLContext | None]]:
    """La aplicación de cookies por ``scheme``, con su autoridad privada si es ``https``, y el
    contexto que la verifica. ``seen`` recoge las rutas de las peticiones que le llegan."""

    async def counted(scope: Scope, receive: Receive, send: Send) -> None:
        if seen is not None and scope["type"] == "http":
            seen.append(scope["path"])
        await _cookie_app(scope, receive, send)

    options: dict[str, Any] = {}
    verify: ssl.SSLContext | None = None
    if scheme == "https":
        tls = server_tls(directory, WALL.now())
        options = {"ssl_certfile": str(tls.certificate_file), "ssl_keyfile": str(tls.key_file)}
        verify = ssl.create_default_context(cafile=str(tls.ca_file))
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            counted, host=LOOPBACK, port=port, log_level="warning", lifespan="off", **options
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = WALL.monotonic() + WAIT_SECONDS
    while not server.started:
        assert WALL.monotonic() < deadline, "la aplicación de cookies no arrancó"
        time.sleep(0.05)
    try:
        yield f"{scheme}://{LOOPBACK}:{port}", verify
    finally:
        server.should_exit = True
        thread.join(timeout=WAIT_SECONDS)


@pytest.mark.parametrize("listener", ["app", "nodes"])
@pytest.mark.parametrize("scheme", ["http", "https"])
def test_a_cookie_of_one_client_never_reaches_another(
    directory: Path, scheme: str, listener: str
) -> None:
    _write_root(directory / "root.pem", Pki(directory, "propia"))
    with cookie_backend(directory, scheme) as (backend, verify):
        edge = MtlsEdge(_config(directory, (backend,)))
        edge.app.upstream_verify = verify
        edge.nodes.upstream_verify = verify
        edge.start()
        try:
            url = (edge.nodes if listener == "nodes" else edge.app).url
            with _client(directory) as victim, _client(directory) as other:
                login = victim.get(url + "/login")
                assert login.status_code == 200, login.text
                assert "set-cookie" in login.headers  # la respuesta le llega a quien la pidió
                received = other.get(url + "/api/x").json()
                own = other.get(url + "/api/x", headers={"Cookie": "propia=1"}).json()
        finally:
            edge.close()
    assert [value for name, value in received if name == "cookie"] == []
    # El balanceador no guarda cookies, pero sigue reenviando la que trae cada cliente.
    assert [value for name, value in own if name == "cookie"] == ["propia=1"]


@pytest.mark.parametrize("listener", ["app", "nodes"])
def test_a_backend_of_a_private_authority_is_never_reached(directory: Path, listener: str) -> None:
    """Sin tocar ``upstream_verify``: el balanceador verifica el backend, no le manda nada."""
    _write_root(directory / "root.pem", Pki(directory, "propia"))
    seen: list[str] = []
    with (
        cookie_backend(directory, "https", seen) as (backend, _),
        running(_config(directory, (backend,))) as edge,
        _client(directory) as client,
    ):
        response = client.get((edge.nodes if listener == "nodes" else edge.app).url + "/api/x")
    assert response.status_code == 502
    assert response.headers[UPSTREAM_ERROR_HEADER] == "ConnectError"
    assert seen == []


def test_a_client_that_never_greets_is_closed_at_the_handshake_limit(directory: Path) -> None:
    """Mide tiempo de verdad: el tope son 30 s y esta prueba los espera. Si el tope creciera o
    faltara, ``recv`` agotaría su propio plazo. Una conexión muda no ocupa el balanceador."""
    _write_root(directory / "root.pem", Pki(directory, "propia"))
    with (
        echo_backend() as backend,
        running(_config(directory, (backend,))) as edge,
        socket.create_connection((LOOPBACK, edge.app.port), timeout=SILENT_CLIENT_SECONDS) as mute,
        contextlib.suppress(ConnectionResetError),
    ):
        assert mute.recv(1) == b""


def _status_of_raw_request(directory: Path, port: int, request: bytes) -> bytes:
    context = ssl.create_default_context(cafile=str(directory / "edge-ca.pem"))
    with (
        socket.create_connection((LOOPBACK, port), timeout=30) as raw,
        context.wrap_socket(raw, server_hostname=LOOPBACK) as connection,
        connection.makefile("rb") as response,
    ):
        connection.sendall(request)
        status: bytes = response.readline().split()[1]
    return status


def test_a_header_that_is_not_ascii_gets_a_400_not_a_traceback(
    directory: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_root(directory / "root.pem", Pki(directory, "propia"))
    with (
        echo_backend() as first,
        echo_backend() as second,
        running(_config(directory, (first, second))) as edge,
    ):
        capsys.readouterr()
        request = b"GET /api/x HTTP/1.1\r\nHost: x\r\nX-Prueba: \xe9\r\n\r\n"
        status = _status_of_raw_request(directory, edge.app.port, request)
        good = _status_of_raw_request(
            directory, edge.app.port, b"GET /api/x HTTP/1.1\r\nHost: x\r\n\r\n"
        )
    assert (status, good) == (b"400", b"200")
    refused, forwarded = edge.app.exchanges
    # No llegó a ningún proceso ni gastó su turno: el siguiente va al primero.
    assert (refused.backend, refused.status, refused.forwarded_mtls) == (-1, 400, ())
    assert forwarded.backend == 0
    assert "Traceback" not in capsys.readouterr().err


def test_a_leaf_with_a_subject_that_is_not_ascii_is_not_forwarded(directory: Path) -> None:
    """La hoja verifica, pero sus cabeceras no se pueden enviar: nada llega a la aplicación."""
    pki = Pki(directory, "propia")
    _write_root(directory / "root.pem", pki)
    key = ec.generate_private_key(ec.SECP256R1())
    moment = WALL.now()
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Ñandú")]))
        .issuer_name(pki.authority[1].subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(moment - DAY)
        .not_valid_after(moment + DAY)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .sign(pki.authority[0], hashes.SHA256())
    )
    certificate_file, key_file = directory / "ajena.crt", directory / "ajena.key"
    certificate_file.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    cert = (str(certificate_file), str(key_file))
    with (
        echo_backend() as backend,
        running(_config(directory, (backend,))) as edge,
        _client(directory, cert) as client,
    ):
        response = client.get(edge.nodes.url + HEARTBEATS)
    assert response.status_code == 400
    (exchange,) = edge.nodes.exchanges
    assert (exchange.backend, exchange.client_certificate, exchange.forwarded_mtls) == (
        -1,
        True,
        (),
    )


def test_the_502_line_carries_no_query(directory: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_root(directory / "root.pem", Pki(directory, "propia"))
    closed = f"http://{LOOPBACK}:{free_port()}"
    with running(_config(directory, (closed,))) as edge, _client(directory) as client:
        capsys.readouterr()
        response = client.get(edge.app.url + "/api/x?token=" + SECRET_MARKER)
    assert response.status_code == 502
    assert SECRET_MARKER not in capsys.readouterr().err


def test_the_forwarding_ignores_a_proxy_of_the_environment(
    directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un ``HTTP_PROXY`` del proceso vería pasar las cookies y las cabeceras de identidad."""
    _write_root(directory / "root.pem", Pki(directory, "propia"))
    hits: list[str] = []
    with root_server({}, hits=hits) as proxy, echo_backend() as backend:
        _point_the_environment_at(monkeypatch, proxy)
        with running(_config(directory, (backend,))) as edge, _client(directory) as client:
            response = client.get(edge.app.url + "/api/x")
    assert response.status_code == 200
    assert hits == []


def test_requests_alternate_between_the_backends_of_the_edge(directory: Path) -> None:
    pki = Pki(directory, "propia")
    _write_root(directory / "root.pem", pki)
    with (
        echo_backend() as first,
        echo_backend() as second,
        running(_config(directory, (first, second))) as edge,
        _client(directory, pki.cert) as client,
    ):
        for _ in range(4):
            client.get(edge.nodes.url + HEARTBEATS)
    assert [item.backend for item in edge.nodes.exchanges] == [0, 1, 0, 1]


@pytest.mark.parametrize("target", ["@otra.invalid/x", "http://otra.invalid/x", "*"])
def test_only_an_origin_form_target_is_forwarded(directory: Path, target: str) -> None:
    """Una petición con ``@otro/`` como destino no sale hacia otra máquina con las cabeceras."""
    _write_root(directory / "root.pem", Pki(directory, "propia"))
    with echo_backend() as backend, running(_config(directory, (backend,))) as edge:
        context = ssl.create_default_context(cafile=str(directory / "edge-ca.pem"))
        with (
            socket.create_connection((LOOPBACK, edge.app.port), timeout=30) as raw,
            context.wrap_socket(raw, server_hostname=LOOPBACK) as connection,
            connection.makefile("rb") as response,
        ):
            connection.sendall(f"GET {target} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            status = response.readline().split()[1]
    assert status == b"400"
    assert [item.backend for item in edge.app.exchanges] == [-1]


# --- El saludo TLS -------------------------------------------------------------------------------


def test_a_certificate_of_the_environment_completes_the_handshake(directory: Path) -> None:
    """Con ``CERT_OPTIONAL`` sin cargar la raíz, ni el certificado propio completaría el saludo."""
    pki = Pki(directory, "propia")
    _write_root(directory / "root.pem", pki)
    with echo_backend() as backend, running(_config(directory, (backend,))) as edge:
        assert _completes(edge, directory, pki.cert)
    assert edge.nodes.exchanges[0].client_certificate


def test_a_certificate_of_another_authority_does_not_complete_the_handshake(
    directory: Path,
) -> None:
    pki, foreign = Pki(directory, "propia"), Pki(directory, "ajena")
    _write_root(directory / "root.pem", pki)
    with echo_backend() as backend, running(_config(directory, (backend,))) as edge:
        assert not _completes(edge, directory, foreign.cert)
    assert list(edge.nodes.exchanges) == []


def test_a_request_without_a_certificate_reaches_the_application_without_headers(
    directory: Path,
) -> None:
    """Como el balanceador de pruebas: la aplicación responde ``node_not_enrolled``."""
    _write_root(directory / "root.pem", Pki(directory, "propia"))
    with echo_backend() as backend, running(_config(directory, (backend,))) as edge:
        assert _completes(edge, directory, None)
    assert not edge.nodes.exchanges[0].client_certificate
    assert edge.nodes.exchanges[0].forwarded_mtls == ()


def test_without_any_root_nodes_admits_nobody_and_app_keeps_serving(directory: Path) -> None:
    """Antes del primer ``bootstrap``: fallo cerrado en ``nodes.``, sin tumbar ``app.``."""
    pki = Pki(directory, "propia")
    with (
        echo_backend() as backend,
        running(_config(directory, (backend,))) as edge,
        _client(directory) as client,
    ):
        assert client.get(edge.app.url + "/api/x").status_code == 200
        assert not _completes(edge, directory, None)
        assert not _completes(edge, directory, pki.cert)
        _write_root(directory / "root.pem", pki)
        assert edge.reload_client_roots() is True
        assert _completes(edge, directory, pki.cert)


# --- La raíz de los nodos ------------------------------------------------------------------------


def test_the_two_root_bundle_is_accepted_and_the_old_root_retires(directory: Path) -> None:
    """Rotación D-6: raíz sola → paquete de dos → raíz nueva sola."""
    old, new = Pki(directory, "vieja"), Pki(directory, "nueva")
    _write_root(directory / "root.pem", old)
    with echo_backend() as backend, running(_config(directory, (backend,))) as edge:
        assert _completes(edge, directory, old.cert)
        assert not _completes(edge, directory, new.cert)
        _write_root(directory / "root.pem", old, new)
        assert edge.reload_client_roots() is True
        assert _completes(edge, directory, old.cert)
        assert _completes(edge, directory, new.cert)
        _write_root(directory / "root.pem", new)
        assert edge.reload_client_roots() is True
        assert not _completes(edge, directory, old.cert)
        assert _completes(edge, directory, new.cert)


def test_the_root_is_reloaded_periodically(directory: Path) -> None:
    pki, new = Pki(directory, "vieja"), Pki(directory, "nueva")
    _write_root(directory / "root.pem", pki)
    stop = threading.Event()
    with (
        echo_backend() as backend,
        running(_config(directory, (backend,), reload_seconds=0.1)) as edge,
    ):
        worker = threading.Thread(target=edge.run, args=(stop,), daemon=True)
        worker.start()
        try:
            _write_root(directory / "root.pem", new)
            deadline = WALL.monotonic() + WAIT_SECONDS
            while not _completes(edge, directory, new.cert):
                assert WALL.monotonic() < deadline, "la raíz nueva no se recargó"
                time.sleep(0.1)
        finally:
            stop.set()
            worker.join(timeout=WAIT_SECONDS)
    assert not worker.is_alive()


def _garbage(path: Path, directory: Path) -> None:
    path.write_bytes(b"no es un certificado")


def _empty(path: Path, directory: Path) -> None:
    path.write_bytes(b"")


def _missing(path: Path, directory: Path) -> None:
    path.unlink()


def _three_roots(path: Path, directory: Path) -> None:
    """Tres: D-6 admite la vigente y la nueva, nunca más (el número no sale de la constante)."""
    _write_root(path, *(Pki(directory, f"raiz-{index}") for index in range(3)))


def _not_self_signed(path: Path, directory: Path) -> None:
    path.write_bytes(Pki(directory, "hoja").certificate.public_bytes(serialization.Encoding.PEM))


def _forged_self_signed(path: Path, directory: Path) -> None:
    """Se dice autofirmado (emisor = sujeto), pero lo firmó otra clave."""
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Autoridad falsa")])
    moment = WALL.now()
    forged = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(ec.generate_private_key(ec.SECP256R1()).public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(moment - DAY)
        .not_valid_after(moment + DAY)
        .sign(ec.generate_private_key(ec.SECP256R1()), hashes.SHA256())
    )
    path.write_bytes(forged.public_bytes(serialization.Encoding.PEM))


def _not_ascii(path: Path, directory: Path) -> None:
    """Un paquete que ``cryptography`` lee y ``ssl`` no: la raíz anterior sigue entera."""
    path.write_bytes("# raíz\n".encode() + Pki(directory, "nueva").root)


@pytest.mark.parametrize(
    "spoil",
    [_garbage, _empty, _missing, _three_roots, _not_self_signed, _forged_self_signed, _not_ascii],
)
def test_a_failed_reload_keeps_the_previous_root(
    directory: Path, spoil: Callable[[Path, Path], None]
) -> None:
    pki = Pki(directory, "propia")
    _write_root(directory / "root.pem", pki)
    events: list[dict[str, object]] = []
    with (
        echo_backend() as backend,
        running(_config(directory, (backend,)), log=lambda line: events.append(dict(line))) as edge,
    ):
        events.clear()
        spoil(directory / "root.pem", directory)
        assert edge.reload_client_roots() is False
        assert _completes(edge, directory, pki.cert)
    assert [event["event"] for event in events] == ["edge_client_ca_reload_failed"]


def test_an_unusable_first_root_leaves_nodes_closed(directory: Path) -> None:
    """Lo lee ``cryptography`` y no ``ssl``: arranca sin raíz y ``nodes.`` no admite a nadie."""
    pki = Pki(directory, "propia")
    _not_ascii(directory / "root.pem", directory)
    with echo_backend() as backend, running(_config(directory, (backend,))) as edge:
        assert not _completes(edge, directory, None)
        assert not _completes(edge, directory, pki.cert)


def test_an_unchanged_root_is_not_reloaded(directory: Path) -> None:
    _write_root(directory / "root.pem", Pki(directory, "propia"))
    with echo_backend() as backend, running(_config(directory, (backend,))) as edge:
        assert edge.reload_client_roots() is False


@contextlib.contextmanager
def root_server(
    files: dict[str, bytes],
    *,
    statuses: dict[str, int] | None = None,
    locations: dict[str, str] | None = None,
    hits: list[str] | None = None,
) -> Iterator[str]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if hits is not None:
                hits.append(self.path)
            content = files.get(self.path)
            default = 200 if content is not None else 404
            self.send_response((statuses or {}).get(self.path, default))
            location = (locations or {}).get(self.path)
            if location is not None:
                self.send_header("location", location)
            self.send_header("content-length", str(len(content or b"")))
            self.end_headers()
            self.wfile.write(content or b"")

        def log_message(self, *_: object) -> None:
            return

    server = http.server.ThreadingHTTPServer((LOOPBACK, 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{LOOPBACK}:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=WAIT_SECONDS)


def test_the_root_can_come_from_the_local_deposit_by_url(directory: Path) -> None:
    pki = Pki(directory, "propia")
    files = {"/vigia-edge/ca/root.pem": pki.root}
    with root_server(files) as store:
        url = store + "/vigia-edge/ca/root.pem"
        assert read_client_roots(url) == pki.root
        with echo_backend() as backend, running(_config(directory, (backend,), client_ca=url)) as e:
            assert _completes(e, directory, pki.cert)
            # LocalStack reiniciado: ``ca/root.pem`` desaparece hasta el siguiente ``bootstrap``.
            files.clear()
            assert e.reload_client_roots() is False
            assert _completes(e, directory, pki.cert)


ROOT_PATH: Final = "/vigia-edge/ca/root.pem"


def _point_the_environment_at(monkeypatch: pytest.MonkeyPatch, proxy: str) -> None:
    """Solo ``http``: el cliente de la prueba habla ``https`` con el balanceador."""
    for name in ("HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(name, proxy)
    for name in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    "spoil", [_garbage, _empty, _three_roots, _not_self_signed, _forged_self_signed]
)
def test_a_bad_root_served_by_url_keeps_the_previous_root(
    directory: Path, spoil: Callable[[Path, Path], None]
) -> None:
    pki = Pki(directory, "propia")
    files = {ROOT_PATH: pki.root}
    with root_server(files) as store, echo_backend() as backend:
        config = _config(directory, (backend,), client_ca=store + ROOT_PATH)
        with running(config) as edge:
            assert _completes(edge, directory, pki.cert)
            spoil(directory / "servida.pem", directory)
            files[ROOT_PATH] = (directory / "servida.pem").read_bytes()
            assert edge.reload_client_roots() is False
            assert _completes(edge, directory, pki.cert)


def test_the_three_root_bundle_and_the_leaf_are_refused_by_url_on_their_own(
    directory: Path,
) -> None:
    """Sin ``validated_roots`` en la vía URL, ``read_client_roots`` los daría por buenos."""
    files: dict[str, bytes] = {}
    with root_server(files) as store:
        for spoil in (_three_roots, _not_self_signed, _forged_self_signed):
            spoil(directory / "servida.pem", directory)
            files[ROOT_PATH] = (directory / "servida.pem").read_bytes()
            with pytest.raises(ValueError):
                read_client_roots(store + ROOT_PATH)


@pytest.mark.parametrize("status", [500, 404, 403])
def test_an_error_response_with_a_valid_root_is_not_loaded(directory: Path, status: int) -> None:
    old, other = Pki(directory, "vieja"), Pki(directory, "otra")
    files = {ROOT_PATH: old.root}
    statuses: dict[str, int] = {}
    with root_server(files, statuses=statuses) as store, echo_backend() as backend:
        config = _config(directory, (backend,), client_ca=store + ROOT_PATH)
        with running(config) as edge:
            files[ROOT_PATH] = other.root
            statuses[ROOT_PATH] = status
            assert edge.reload_client_roots() is False
            assert _completes(edge, directory, old.cert)
            assert not _completes(edge, directory, other.cert)


def test_a_redirect_is_not_followed_to_another_root(directory: Path) -> None:
    old, other = Pki(directory, "vieja"), Pki(directory, "otra")
    files = {ROOT_PATH: old.root, "/otra.pem": other.root}
    statuses: dict[str, int] = {}
    locations: dict[str, str] = {}
    with (
        root_server(files, statuses=statuses, locations=locations) as store,
        echo_backend() as backend,
    ):
        config = _config(directory, (backend,), client_ca=store + ROOT_PATH)
        with running(config) as edge:
            statuses[ROOT_PATH] = 302
            locations[ROOT_PATH] = "/otra.pem"
            assert edge.reload_client_roots() is False
            assert not _completes(edge, directory, other.cert)


def test_the_root_ignores_a_proxy_of_the_environment(
    directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un ``HTTP_PROXY`` del proceso podría servir otra raíz, en la que se confiaría."""
    good, other = Pki(directory, "buena"), Pki(directory, "otra")
    with root_server({ROOT_PATH: good.root}) as store:
        url = store + ROOT_PATH
        with root_server({url: other.root}) as proxy:
            _point_the_environment_at(monkeypatch, proxy)
            assert read_client_roots(url) == good.root


def test_validated_roots_returns_what_it_read(directory: Path) -> None:
    pki = Pki(directory, "propia")
    assert validated_roots(pki.root) == pki.root


# --- El certificado de servidor ------------------------------------------------------------------


def _init(directory: Path, **changes: Any) -> Any:
    values: dict[str, Any] = {"dns_names": DNS_NAMES, "addresses": (LOOPBACK,), "now": WALL.now()}
    values.update(changes)
    return init_server_certificate(
        directory / "edge.crt", directory / "edge.key", directory / "edge-ca.pem", **values
    )


def test_a_long_first_name_fits_in_the_certificate(tmp_path: Path) -> None:
    """El nombre común es fijo: el primer ``--san`` puede pasar de los 64 caracteres del CN."""
    long_name = "app." + "a" * 60 + "." + DOMAIN
    result = _init(tmp_path, dns_names=(long_name, DNS_NAMES[1]))
    assert result.created


def test_the_server_certificate_persists_and_has_the_expected_names(tmp_path: Path) -> None:
    first = _init(tmp_path)
    second = _init(tmp_path)
    assert (first.created, second.created) == (True, False)
    assert first.certificate_sha256 == second.certificate_sha256
    certificate = x509.load_pem_x509_certificate((tmp_path / "edge.crt").read_bytes())
    san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert sorted(san.get_values_for_type(x509.DNSName)) == sorted(DNS_NAMES)
    assert [str(ip) for ip in san.get_values_for_type(x509.IPAddress)] == [LOOPBACK]
    authority = x509.load_pem_x509_certificate((tmp_path / "edge-ca.pem").read_bytes())
    certificate.verify_directly_issued_by(authority)
    assert stat.S_IMODE((tmp_path / "edge.key").stat().st_mode) == 0o600
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "edge-ca.pem",
        "edge.crt",
        "edge.key",
    ]


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"dns_names": (DNS_NAMES[1],)}, "edge_san_missing"),
        ({"dns_names": (DNS_NAMES[0],)}, "edge_san_missing"),
        ({"addresses": ()}, "edge_san_missing"),
        ({"dns_names": (*DNS_NAMES, "*.integracion.test")}, "edge_san_invalid"),
        ({"addresses": ("no-es-una-ip",)}, "edge_san_invalid"),
    ],
)
def test_init_cert_rejects_incomplete_names_without_writing(
    tmp_path: Path, changes: dict[str, Any], code: str
) -> None:
    with pytest.raises(EdgeError) as caught:
        _init(tmp_path, **changes)
    assert caught.value.code == code
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("keep", ["edge.crt", "edge.key"])
def test_init_cert_never_completes_half_of_the_pair(tmp_path: Path, keep: str) -> None:
    _init(tmp_path)
    (tmp_path / ("edge.key" if keep == "edge.crt" else "edge.crt")).unlink()
    before = (tmp_path / keep).read_bytes()
    with pytest.raises(EdgeError) as caught:
        _init(tmp_path)
    assert caught.value.code == "edge_server_cert_partial"
    assert (tmp_path / keep).read_bytes() == before


@pytest.mark.parametrize(
    "changes",
    [{"dns_names": (*DNS_NAMES, f"otro.{DOMAIN}")}, {"addresses": ("192.0.2.7",)}],
)
def test_init_cert_does_not_overwrite_a_certificate_with_other_names(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    first = _init(tmp_path)
    with pytest.raises(EdgeError) as caught:
        _init(tmp_path, **changes)
    assert caught.value.code == "edge_server_cert_san_mismatch"
    assert certificate_sha256(tmp_path / "edge.crt") == first.certificate_sha256


def test_init_cert_treats_an_unreadable_certificate_as_a_mismatch(tmp_path: Path) -> None:
    _init(tmp_path)
    (tmp_path / "edge.crt").write_bytes(b"no es un certificado")
    with pytest.raises(EdgeError) as caught:
        _init(tmp_path)
    assert caught.value.code == "edge_server_cert_san_mismatch"
    assert (tmp_path / "edge.crt").read_bytes() == b"no es un certificado"


def _attempt_init(
    directory: Path, barrier: threading.Barrier, results: list[bool], errors: list[Exception]
) -> None:
    barrier.wait(timeout=WAIT_SECONDS)
    try:
        results.append(_init(directory).created)
    except EdgeError:
        pass  # el perdedor termina con su código, sin tocar nada
    except Exception as error:
        errors.append(error)


def test_concurrent_init_cert_generates_one_certificate(tmp_path: Path) -> None:
    """Varios ``init-cert`` a la vez: un solo ganador y un par certificado-clave coherente."""
    workers = 8
    winners_by_round: list[int] = []
    for number in range(5):
        directory = tmp_path / f"ronda-{number}"
        directory.mkdir()
        barrier = threading.Barrier(workers)
        results: list[bool] = []
        errors: list[Exception] = []
        threads = [
            threading.Thread(target=_attempt_init, args=(directory, barrier, results, errors))
            for _ in range(workers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=WAIT_SECONDS)
        winners_by_round.append(sum(results))
        assert errors == []
        certificate = x509.load_pem_x509_certificate((directory / "edge.crt").read_bytes())
        key = serialization.load_pem_private_key((directory / "edge.key").read_bytes(), None)
        spki = serialization.PublicFormat.SubjectPublicKeyInfo
        assert certificate.public_key().public_bytes(
            serialization.Encoding.DER, spki
        ) == key.public_key().public_bytes(serialization.Encoding.DER, spki)
    assert winners_by_round == [1] * 5


def test_the_exported_root_validates_the_server_by_ip(directory: Path) -> None:
    _write_root(directory / "root.pem", Pki(directory, "propia"))
    with (
        echo_backend() as backend,
        running(_config(directory, (backend,))) as edge,
        _client(directory) as client,
    ):
        assert client.get(edge.app.url + "/api/x").status_code == 200


def test_the_edge_does_not_start_without_its_certificate(tmp_path: Path) -> None:
    with pytest.raises(EdgeError) as caught:
        MtlsEdge(_config(tmp_path, ("http://api:8000",)))
    assert caught.value.code == "edge_server_cert_missing"


def test_the_edge_does_not_start_with_a_key_others_can_read(directory: Path) -> None:
    (directory / "edge.key").chmod(0o640)
    with pytest.raises(EdgeError) as caught:
        MtlsEdge(_config(directory, ("http://api:8000",)))
    assert caught.value.code == "edge_server_key_insecure"


# --- Configuración -------------------------------------------------------------------------------


def _environ(directory: Path, **changes: str) -> dict[str, str]:
    values = {
        "VIGIA_EDGE_BIND": LOOPBACK,
        "VIGIA_EDGE_BACKENDS": "http://api-1:8000,http://api-2:8000",
        "VIGIA_EDGE_SERVER_CERT": str(directory / "edge.crt"),
        "VIGIA_EDGE_SERVER_KEY": str(directory / "edge.key"),
        "VIGIA_EDGE_CLIENT_CA": str(directory / "root.pem"),
    }
    values.update(changes)
    return values


def test_configuration_defaults(tmp_path: Path) -> None:
    config = EdgeConfig.from_env(_environ(tmp_path))
    assert (config.app_port, config.nodes_port, config.reload_seconds) == (8443, 8444, 60.0)
    assert config.backends == ("http://api-1:8000", "http://api-2:8000")
    assert config.server_ca == tmp_path / "edge-ca.pem"


def test_configuration_reads_every_variable(tmp_path: Path) -> None:
    config = EdgeConfig.from_env(
        _environ(
            tmp_path,
            VIGIA_EDGE_APP_PORT="18443",
            VIGIA_EDGE_NODES_PORT="65535",
            VIGIA_EDGE_RELOAD_SECONDS="5",
            VIGIA_EDGE_SERVER_CA=str(tmp_path / "otra.pem"),
        )
    )
    assert (config.app_port, config.nodes_port, config.reload_seconds) == (18443, 65535, 5.0)
    assert config.server_ca == tmp_path / "otra.pem"


@pytest.mark.parametrize(
    "name",
    [
        "VIGIA_EDGE_BIND",
        "VIGIA_EDGE_BACKENDS",
        "VIGIA_EDGE_SERVER_CERT",
        "VIGIA_EDGE_SERVER_KEY",
        "VIGIA_EDGE_CLIENT_CA",
    ],
)
def test_a_missing_variable_stops_the_edge(tmp_path: Path, name: str) -> None:
    environ = _environ(tmp_path)
    del environ[name]
    with pytest.raises(EdgeError) as caught:
        EdgeConfig.from_env(environ)
    assert (caught.value.code, caught.value.field_name) == ("edge_config_missing", name)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("VIGIA_EDGE_APP_PORT", "0"),
        ("VIGIA_EDGE_APP_PORT", "65536"),
        ("VIGIA_EDGE_NODES_PORT", "ocho"),
        ("VIGIA_EDGE_NODES_PORT", "٨٤٤٤"),
        ("VIGIA_EDGE_RELOAD_SECONDS", "0"),
        ("VIGIA_EDGE_RELOAD_SECONDS", "-1"),
        ("VIGIA_EDGE_RELOAD_SECONDS", "nan"),
        ("VIGIA_EDGE_RELOAD_SECONDS", "inf"),
        ("VIGIA_EDGE_RELOAD_SECONDS", "pronto"),
        ("VIGIA_EDGE_BACKENDS", "ftp://api:8000"),
        ("VIGIA_EDGE_BACKENDS", "http://api:8000/ruta"),
        ("VIGIA_EDGE_BACKENDS", "http://usuario@api:8000"),
        ("VIGIA_EDGE_BACKENDS", "http://api:8000,"),
        ("VIGIA_EDGE_BIND", "no-existe0"),
    ],
)
def test_an_invalid_variable_stops_the_edge(tmp_path: Path, name: str, value: str) -> None:
    with pytest.raises(EdgeError) as caught:
        EdgeConfig.from_env(_environ(tmp_path, **{name: value}))
    assert (caught.value.code, caught.value.field_name) == ("edge_config_invalid", name)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("192.0.2.7", "192.0.2.7"), ("::1", "::1"), ("lo", LOOPBACK)],
)
def test_the_bind_address_is_an_ip_or_an_interface(value: str, expected: str) -> None:
    assert resolve_bind(value) == expected


# --- Registro ------------------------------------------------------------------------------------


def test_no_body_or_authorization_reaches_the_exchange_record(
    directory: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pki = Pki(directory, "propia")
    _write_root(directory / "root.pem", pki)
    with (
        echo_backend() as backend,
        running(_config(directory, (backend,))) as edge,
        _client(directory, pki.cert) as client,
    ):
        capsys.readouterr()
        client.post(
            edge.nodes.url + HEARTBEATS + "?token=" + SECRET_MARKER,
            content=SECRET_MARKER.encode(),
            headers={"Authorization": "Bearer " + SECRET_MARKER},
        )
    output = capsys.readouterr().err
    assert SECRET_MARKER not in output
    (line,) = [json.loads(item) for item in output.splitlines() if '"listener"' in item]
    assert line == {
        "listener": "nodes",
        "method": "POST",
        "path": HEARTBEATS,
        "backend": 0,
        "status": 200,
        "client_certificate": True,
        "dropped_mtls": [],
        "upstream_error": None,
    }
    (exchange,) = edge.nodes.exchanges
    assert SECRET_MARKER not in repr(exchange)


def test_an_exchange_has_no_field_for_a_body_or_general_headers() -> None:
    assert {field.name for field in fields(Exchange)} == {
        "listener",
        "method",
        "path",
        "backend",
        "status",
        "client_certificate",
        "forwarded_mtls",
        "dropped_mtls",
        "upstream_error",
    }


def test_a_long_lived_edge_keeps_a_bounded_number_of_exchanges(directory: Path) -> None:
    edge = MtlsEdge(_config(directory, ("http://api:8000",)))
    for listener in (edge.app, edge.nodes):
        assert getattr(listener.exchanges, "maxlen", None) == EXCHANGES_KEPT


# --- La aplicación real --------------------------------------------------------------------------


@contextlib.contextmanager
def real_api(nodes: NodeWorld) -> Iterator[str]:
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(nodes.app, host=LOOPBACK, port=port, log_level="warning", lifespan="off")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = WALL.monotonic() + WAIT_SECONDS
    while not server.started:
        assert WALL.monotonic() < deadline, "la API no arrancó"
        time.sleep(0.05)
    try:
        yield f"http://{LOOPBACK}:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=WAIT_SECONDS)


def _node_credential(nodes: NodeWorld, directory: Path) -> tuple[bytes, tuple[str, str]]:
    """La raíz de la autoridad de prueba y una hoja del nodo ``a`` con su clave en disco.

    La vigencia cubre los dos relojes porque cada uno lo lee quien debe: OpenSSL, en el saludo, la
    hora del sistema; ``AlbCertificateHeaders``, la simulada de la aplicación. Nada los compara.
    """
    node = nodes.a
    simulated, wall = nodes.now, WALL.now()
    start, end = min(simulated, wall) - DAY, max(simulated, wall) + 300 * DAY
    root = (
        x509.CertificateBuilder()
        .subject_name(nodes.authority.name)
        .issuer_name(nodes.authority.name)
        .public_key(nodes.authority.key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(nodes.authority.key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(subject_name(NodeSubject(node.node_id, node.organization_id, node.plant_id)))
        .issuer_name(nodes.authority.name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .sign(nodes.authority.key, hashes.SHA256())
    )
    node.credentials[format(leaf.serial_number, "x")] = {
        "status": "active",
        "issued_at": simulated - DAY,
        "expires_at": simulated + 300 * DAY,
    }
    certificate_file, key_file = directory / "nodo.crt", directory / "nodo.key"
    certificate_file.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return root.public_bytes(serialization.Encoding.PEM), (str(certificate_file), str(key_file))


def test_the_real_application_recognizes_the_node_through_the_edge(directory: Path) -> None:
    """``AlbCertificateHeaders`` exige cada cabecera una vez y coherente con la hoja: si el
    balanceador dejara pasar una falsa o la repitiera, la respuesta sería ``node_not_enrolled``."""
    nodes = node_world()
    root, cert = _node_credential(nodes, directory)
    (directory / "root.pem").write_bytes(root)
    headers = {"X-Vigia-Contract-Version": VERSION, **FORGED}
    catalog = NodeRoute.ZONE_CATALOG.path.replace("{zone_id}", str(zone_of(nodes.a)))
    with (
        real_api(nodes) as api,
        running(_config(directory, (api,))) as edge,
        _client(directory, cert) as with_certificate,
        _client(directory) as without_certificate,
    ):
        accepted = with_certificate.get(edge.nodes.url + catalog, headers=headers)
        rejected = without_certificate.get(edge.nodes.url + catalog, headers=headers)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["node_id"] == str(nodes.a.node_id)
    assert rejected.status_code == 401


# --- Orden, imagen y dependencias ----------------------------------------------------------------


def _serve(environ: dict[str, str]) -> dict[str, Any]:
    """Arranca ``serve`` como proceso, espera ``edge_started``, lo detiene y devuelve esa línea."""
    process = subprocess.Popen(
        [sys.executable, "-m", "tools.mtls_edge", "serve"],
        cwd=BACKEND,
        env={**os.environ, **environ},
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stderr is not None
    started: dict[str, Any] | None = None
    try:
        for line in process.stderr:
            document = json.loads(line)
            if document.get("event") == "edge_started":
                started = document
                break
    finally:
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=WAIT_SECONDS)
    assert process.returncode == 0
    assert started is not None
    return started


def test_the_server_certificate_is_the_same_across_starts(directory: Path) -> None:
    _write_root(directory / "root.pem", Pki(directory, "propia"))
    environ = _environ(
        directory,
        VIGIA_EDGE_BACKENDS="http://127.0.0.1:9",
        VIGIA_EDGE_APP_PORT=str(free_port()),
        VIGIA_EDGE_NODES_PORT=str(free_port()),
    )
    first, second = _serve(environ), _serve(environ)
    assert first["server_certificate_sha256"] == second["server_certificate_sha256"]
    assert first["server_certificate_sha256"] == certificate_sha256(directory / "edge.crt")


def test_the_init_cert_order_prints_the_fingerprint_and_repeats(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    order = ["init-cert", "--san", DNS_NAMES[0], "--san", DNS_NAMES[1], "--ip", LOOPBACK]
    environ = {
        "VIGIA_EDGE_SERVER_CERT": str(tmp_path / "edge.crt"),
        "VIGIA_EDGE_SERVER_KEY": str(tmp_path / "edge.key"),
    }
    assert main(order, environ) == 0
    first = json.loads(capsys.readouterr().out)
    assert main(order, environ) == 0
    second = json.loads(capsys.readouterr().out)
    assert (first["created"], second["created"]) == (True, False)
    assert first["certificate_sha256"] == second["certificate_sha256"]
    assert first["ca_file"] == str(tmp_path / "edge-ca.pem")


def test_an_order_with_an_invalid_configuration_ends_with_its_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["serve"], {}) == 2
    assert json.loads(capsys.readouterr().err) == {
        "error": "edge_config_missing",
        "field": "VIGIA_EDGE_SERVER_CERT",
    }


def test_an_operating_system_failure_ends_with_its_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "no-existe"
    order = ["init-cert", "--san", DNS_NAMES[0], "--san", DNS_NAMES[1], "--ip", LOOPBACK]
    environ = {
        "VIGIA_EDGE_SERVER_CERT": str(missing / "edge.crt"),
        "VIGIA_EDGE_SERVER_KEY": str(missing / "edge.key"),
    }
    assert main(order, environ) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "edge_os_error"


def test_the_entry_point_runs_with_the_backend_on_the_path() -> None:
    pyproject = tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["scripts"]["vigia-mtls-edge"] == "tools.mtls_edge:main"
    script = Path(sys.executable).with_name("vigia-mtls-edge")
    finished = subprocess.run(
        [str(script), "--help"],
        env={**os.environ, "PYTHONPATH": str(BACKEND)},
        capture_output=True,
        text=True,
        timeout=WAIT_SECONDS,
        check=False,
    )
    assert finished.returncode == 0, finished.stderr


def test_the_tool_depends_only_on_what_its_image_installs() -> None:
    tree = ast.parse((BACKEND / "tools" / "mtls_edge.py").read_text(encoding="utf-8"))
    imported = {
        name.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for name in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert imported - set(sys.stdlib_module_names) == {"cryptography", "h11", "httpx"}


def test_the_image_uses_the_python_of_the_platform_image() -> None:
    dockerfile = (REPOSITORY / "local" / "mtls-edge" / "Dockerfile").read_text(encoding="utf-8")
    platform = (BACKEND / "Dockerfile").read_text(encoding="utf-8")
    base = re.search(r"^ARG PYTHON_IMAGE=(\S+)$", platform, re.MULTILINE)
    assert base is not None
    assert re.findall(r"^FROM (\S+)", dockerfile, re.MULTILINE) == [base[1]]


def test_the_image_requirements_are_the_locked_versions() -> None:
    lock = tomllib.loads((BACKEND / "uv.lock").read_text(encoding="utf-8"))
    locked = {package["name"]: package.get("version") for package in lock["package"]}
    text = (BACKEND / "tools" / "requirements" / "mtls-edge.txt").read_text(encoding="utf-8")
    pins = re.findall(r"^([a-z0-9._-]+)==(\S+)[^\n]*\\$", text, re.MULTILINE)
    assert {"cryptography", "h11", "httpx"} <= {name for name, _ in pins}
    for name, version in pins:
        assert locked[name] == version, name
    assert text.count("--hash=sha256:") >= len(pins)


def test_a_clock_with_a_timezone_is_enough_for_the_certificate(tmp_path: Path) -> None:
    """La vigencia sale del ``now`` inyectado (una herramienta sin ``shared.clock``)."""
    moment = dt.datetime(2031, 3, 4, 5, 6, 7, tzinfo=dt.UTC)
    _init(tmp_path, now=moment)
    certificate = x509.load_pem_x509_certificate((tmp_path / "edge.crt").read_bytes())
    assert certificate.not_valid_before_utc == moment - dt.timedelta(days=1)
