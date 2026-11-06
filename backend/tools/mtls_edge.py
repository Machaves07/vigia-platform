"""``vigia-mtls-edge``: los dos balanceadores de U-03 para el entorno de **integración** (TASK-542).

``infrastructure-design.md`` §5 de U-03: el balanceador de **nodos** (``nodes.``) termina mTLS en
modo *verify* contra la raíz de ``vigia-node-ca`` y pasa a la aplicación la hoja y sus datos en las
cabeceras ``X-Amzn-Mtls-Clientcert-{Leaf,Serial-Number,Subject,Issuer,Validity}``; el de
**personas** (``app.``) no pide certificado y quita cualquier ``X-Amzn-Mtls-*`` entrante (U-02
§4.2). Sin esas cabeceras la plataforma no reconoce a ningún nodo: la aplicación sigue idéntica a
producción (``node_api/identity.py``, ``AlbCertificateHeaders``) y este proceso hace de balanceador.

**Pieza de integración, no de producto** (A-76 D3). En producción termina TLS el ALB de AWS (A-47);
esta herramienta no entra en la imagen de ``backend/Dockerfile`` y no comprueba la lista de
revocación (la revocación la decide la aplicación por credencial).

- ``MtlsProxy``: una escucha TLS con reparto **por petición** en turno rotatorio entre ``backends``;
  una conexión persistente del cliente no queda atada a un proceso. Con raíz de clientes pide
  certificado (opcional, para que la petición sin certificado llegue a la aplicación y responda
  ``node_not_enrolled``) y lo verifica en el saludo TLS: un certificado de otra autoridad no lo
  completa. **Siempre** descarta las ``X-Amzn-Mtls-*`` del cliente. La usan también las pruebas
  (``tests/conformance/mtls_proxy.py``) en 127.0.0.1 con una autoridad efímera.
- ``MtlsEdge``: las dos escuchas de ``EdgeConfig`` en ``VIGIA_EDGE_BIND`` y la recarga de la raíz
  de nodos cada ``reload_seconds`` (rotación D-6: el paquete de dos raíces se acepta). Una recarga
  fallida conserva la raíz anterior; sin ninguna raíz válida todavía, ``nodes.`` no admite ningún
  saludo (fallo cerrado) y ``app.`` sigue atendiendo, porque el alta y el arranque van por ahí.
- ``init_server_certificate``: el certificado de servidor **persistente**, generado una vez por
  ``vigia-mtls-edge init-cert``, firmado por una autoridad local propia cuya raíz se exporta para
  los clientes. La clave de esa autoridad no se guarda: renovar es volver a repartir una raíz.

Cada intercambio deja en ``exchanges`` y en el registro ruta (sin consulta), proceso, estado y las
cabeceras ``X-Amzn-Mtls-*``; **nunca** un cuerpo ni una cabecera de autorización.

Entorno: ``VIGIA_EDGE_BIND`` (IP o nombre de interfaz, como ``tailscale0``), ``VIGIA_EDGE_BACKENDS``
(``http://api-1:8000,http://api-2:8000``), ``VIGIA_EDGE_SERVER_CERT``, ``VIGIA_EDGE_SERVER_KEY``,
``VIGIA_EDGE_SERVER_CA`` (raíz exportada; por omisión ``edge-ca.pem`` junto al certificado),
``VIGIA_EDGE_CLIENT_CA`` (ruta o URL ``http(s)`` de ``vigia-edge/ca/root.pem``),
``VIGIA_EDGE_APP_PORT`` (8443), ``VIGIA_EDGE_NODES_PORT`` (8444) y ``VIGIA_EDGE_RELOAD_SECONDS``
(60).

Sin dependencias de ``vigia_platform``: ``local/mtls-edge/Dockerfile`` solo instala
``cryptography``, ``h11`` y ``httpx``. Por eso no usa ``shared.clock``: la hora es la de la pared,
inyectable en ``init_server_certificate``, y OpenSSL comprueba las vigencias con el reloj del
sistema.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import fcntl
import ipaddress
import itertools
import json
import os
import re
import signal
import socket
import ssl
import stat
import struct
import sys
import threading
from collections import deque
from collections.abc import Callable, Iterator, Mapping, MutableSequence, Sequence
from dataclasses import dataclass, field
from http.cookiejar import DefaultCookiePolicy
from pathlib import Path
from typing import Final, TextIO
from urllib.parse import quote

import h11
import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

__all__ = [
    "MTLS_HEADER_PREFIX",
    "UPSTREAM_ERROR_HEADER",
    "EdgeConfig",
    "EdgeError",
    "Exchange",
    "InitResult",
    "MtlsEdge",
    "MtlsProxy",
    "ServerTls",
    "alb_headers",
    "certificate_sha256",
    "init_server_certificate",
    "main",
    "mtls_proxy",
    "read_client_roots",
    "resolve_bind",
    "run_edge",
    "validated_roots",
]

MTLS_HEADER_PREFIX: Final = "x-amzn-mtls-"
UPSTREAM_TIMEOUT_SECONDS: Final = 120.0
"""Tope de la petición a la aplicación: nunca decide una prueba (retro 15)."""
UPSTREAM_ERROR_HEADER: Final = "x-vigia-balancer-error"
"""En el ``502`` propio del balanceador: la clase del ``httpx.HTTPError`` hacia el proceso
(``ConnectError``, ``RemoteProtocolError``, ``ReadTimeout``…), para el informe de carga."""
START_TIMEOUT_SECONDS: Final = 30.0
HANDSHAKE_TIMEOUT_SECONDS: Final = 30.0
ROOT_FETCH_TIMEOUT_SECONDS: Final = 10.0
DEFAULT_APP_PORT: Final = 8443
DEFAULT_NODES_PORT: Final = 8444
DEFAULT_RELOAD_SECONDS: Final = 60.0
MAX_ROOTS: Final = 2
"""La vigente y la nueva, durante una sustitución (D-6); ``shared/node_ca.py`` publica como mucho
dos y esta herramienta no importa la plataforma."""
EXCHANGES_KEPT: Final = 1_000
"""Los intercambios en memoria de un proceso de larga vida; el registro los lleva todos."""
SERVER_DAYS: Final = 825
AUTHORITY_DAYS: Final = 3_650
_READ_CHUNK: Final = 65_536
_SIOCGIFADDR: Final = 0x8915
_IFNAME_MAX: Final = 15
_BACKEND: Final = re.compile(r"https?://[A-Za-z0-9._-]+(?::[0-9]{1,5})?")
_DNS_NAME: Final = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")
_FORWARDED: Final = frozenset(
    {"x-forwarded-for", "x-forwarded-proto", "x-forwarded-port", "x-forwarded-host"}
)
_HOP_BY_HOP: Final = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)


class EdgeError(Exception):
    """Configuración o certificado no válidos: fallo cerrado con un código de lista cerrada."""

    def __init__(self, code: str, field_name: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.field_name = field_name


def _wall_now() -> dt.datetime:
    """La hora de la pared: la herramienta no depende de ``shared.clock`` (ver el módulo)."""
    return dt.datetime.now(dt.UTC)  # noqa: TID251


def _stderr_line(document: Mapping[str, object]) -> None:
    print(json.dumps(document, sort_keys=True), file=sys.stderr, flush=True)


def alb_headers(certificate: x509.Certificate) -> dict[str, str]:
    """Las cabeceras ``X-Amzn-Mtls-Clientcert-*`` que el balanceador entrega con ``certificate``."""
    pem = certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")
    stamp = "%Y-%m-%dT%H:%M:%SZ"
    return {
        "X-Amzn-Mtls-Clientcert-Leaf": quote(pem, safe=""),
        "X-Amzn-Mtls-Clientcert-Serial-Number": format(certificate.serial_number, "X"),
        "X-Amzn-Mtls-Clientcert-Subject": certificate.subject.rfc4514_string(),
        "X-Amzn-Mtls-Clientcert-Issuer": certificate.issuer.rfc4514_string(),
        "X-Amzn-Mtls-Clientcert-Validity": (
            f"NotBefore={certificate.not_valid_before_utc.strftime(stamp)};"
            f"NotAfter={certificate.not_valid_after_utc.strftime(stamp)}"
        ),
    }


@dataclass(frozen=True)
class ServerTls:
    """Certificado de servidor del balanceador y la raíz que los clientes deben creer."""

    ca_file: Path
    certificate_file: Path
    key_file: Path


def server_context(tls: ServerTls, *, mutual: bool, roots: bytes | None) -> ssl.SSLContext:
    """El contexto de una escucha. Con ``mutual`` y sin ``roots``, no admite ningún saludo."""
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(tls.certificate_file, tls.key_file)
    if not mutual:
        context.verify_mode = ssl.CERT_NONE
    elif roots is None:
        context.verify_mode = ssl.CERT_REQUIRED
    else:
        context.load_verify_locations(cadata=roots.decode("ascii"))
        context.verify_mode = ssl.CERT_OPTIONAL
    return context


@dataclass(frozen=True)
class Exchange:
    """Un intercambio que pasó por el balanceador (sin cuerpos)."""

    listener: str
    method: str
    path: str
    backend: int
    """Índice en ``backends`` del proceso que lo atendió; ``-1`` si no se reenvió."""
    status: int
    client_certificate: bool
    forwarded_mtls: tuple[tuple[str, str], ...]
    """Las cabeceras ``X-Amzn-Mtls-*`` que llegaron a la aplicación (nombre en minúsculas)."""
    dropped_mtls: tuple[str, ...]
    """Los nombres de las ``X-Amzn-Mtls-*`` que traía el cliente y se descartaron."""
    upstream_error: str | None = None
    """En un ``502`` del balanceador, la clase del ``httpx.HTTPError`` hacia el proceso."""


@dataclass
class MtlsProxy:
    """Proxy inverso TLS con reparto por petición (ver el módulo)."""

    name: str
    backends: Sequence[str]
    tls: ServerTls
    client_ca: Path | None = None
    strip_client_mtls: bool = True
    """Solo las sondas de la prueba del balanceador lo apagan (mutación)."""
    preserve_host: bool = False
    """Reenvía ``Host`` tal cual: lo exige la firma SigV4 del almacén (el ``https`` de S3)."""
    mutual: bool = False
    """Pide certificado aunque aún no haya raíz (``MtlsEdge``): sin raíz nadie entra."""
    set_forwarded: bool = False
    """Como el ALB: ``X-Forwarded-*`` las pone el balanceador, nunca el cliente. Apagado en las
    pruebas de carga, cuyo cliente sintético declara un origen de red por nodo."""
    host: str = "127.0.0.1"
    listen_port: int = 0
    on_exchange: Callable[[Exchange], None] | None = None
    upstream_verify: ssl.SSLContext | bool = True
    """Cómo se verifica un backend ``https``; sin el entorno del proceso (``trust_env=False``)."""
    exchanges: MutableSequence[Exchange] = field(default_factory=list)
    _loop: asyncio.AbstractEventLoop = field(default_factory=asyncio.new_event_loop)
    _thread: threading.Thread | None = None
    _server: asyncio.Server | None = None
    _client: httpx.AsyncClient | None = None
    _turn: Iterator[int] = field(default_factory=itertools.count)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _connections: dict[asyncio.Task[None], asyncio.StreamWriter] = field(default_factory=dict)
    """Las conexiones abiertas: al cerrar se abortan (una persistente ociosa no acaba sola, y el
    cierre ordenado de TLS esperaría al cliente)."""
    _roots: bytes | None = None
    _tls_context: ssl.SSLContext | None = None
    """El contexto de los saludos nuevos. Se sustituye entero: añadir a un ``SSLContext`` no
    retiraría una raíz vieja. Las conexiones abiertas conservan el suyo."""
    port: int = 0

    @property
    def url(self) -> str:
        return f"https://{self.host}:{self.port}"

    @property
    def _verifies_clients(self) -> bool:
        return self.mutual or self.client_ca is not None

    def _context(self) -> ssl.SSLContext:
        roots = self._roots
        if roots is None and self.client_ca is not None:
            roots = self.client_ca.read_bytes()
        return server_context(self.tls, mutual=self._verifies_clients, roots=roots)

    def use_client_roots(self, roots: bytes) -> None:
        """Los saludos siguientes verifican contra ``roots``; si no se puede, no cambia nada."""
        previous = self._roots
        self._roots = roots
        try:
            self._tls_context = self._context()
        except (ssl.SSLError, ValueError):
            self._roots = previous
            raise

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop.run_forever, name=f"balanceador-{self.name}", daemon=True
        )
        self._thread.start()
        future = asyncio.run_coroutine_threadsafe(self._open(), self._loop)
        future.result(timeout=START_TIMEOUT_SECONDS)

    async def _open(self) -> None:
        # Sin el entorno: un ``HTTP_PROXY`` vería pasar las cookies y las cabeceras de identidad.
        self._client = httpx.AsyncClient(
            timeout=UPSTREAM_TIMEOUT_SECONDS, trust_env=False, verify=self.upstream_verify
        )
        # Un solo cliente atiende a todos: un tarro de cookies daría la sesión de uno al siguiente.
        self._client.cookies.jar.set_policy(DefaultCookiePolicy(allowed_domains=[]))
        self._tls_context = self._context()
        self._server = await asyncio.start_server(
            self._serve, host=self.host, port=self.listen_port
        )
        self.port = int(self._server.sockets[0].getsockname()[1])

    def close(self) -> None:
        if self._thread is None:
            return

        async def shut() -> None:
            if self._server is not None:
                self._server.close()
                for task, writer in tuple(self._connections.items()):
                    writer.transport.abort()
                    task.cancel()
                await asyncio.gather(*self._connections, return_exceptions=True)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._server.wait_closed(), timeout=10)
            if self._client is not None:
                await self._client.aclose()

        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(shut(), self._loop).result(timeout=30)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=30)
        self._thread = None
        self._loop.close()

    def served(self, path_prefix: str = "") -> list[Exchange]:
        with self._lock:
            return [item for item in self.exchanges if item.path.startswith(path_prefix)]

    # --- Una conexión -------------------------------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._connections[task] = writer
            task.add_done_callback(lambda done: self._connections.pop(done, None))
        connection = h11.Connection(h11.SERVER)
        request: h11.Request | None = None
        body = bytearray()
        try:
            assert self._tls_context is not None  # noqa: S101 - lo pone ``_open`` antes de escuchar
            await writer.start_tls(
                self._tls_context, ssl_handshake_timeout=HANDSHAKE_TIMEOUT_SECONDS
            )
            ssl_object = writer.get_extra_info("ssl_object")
            der = ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None
            certificate = x509.load_der_x509_certificate(der) if der else None
            peer = writer.get_extra_info("peername")
            address = str(peer[0]) if peer else "desconocida"
            while True:
                event = connection.next_event()
                if event is h11.NEED_DATA:
                    connection.receive_data(await reader.read(_READ_CHUNK))
                    continue
                if isinstance(event, h11.Request):
                    request, body = event, bytearray()
                    if connection.they_are_waiting_for_100_continue:
                        # El cliente S3 espera este acuse antes de enviar el paquete. Sin él,
                        # el proxy espera el cuerpo y boto3 espera al proxy hasta agotar el tope.
                        writer.write(
                            connection.send(h11.InformationalResponse(status_code=100, headers=[]))
                        )
                        await writer.drain()
                elif isinstance(event, h11.Data):
                    body += event.data
                elif isinstance(event, h11.EndOfMessage) and request is not None:
                    await self._answer(
                        connection, writer, request, bytes(body), certificate, address
                    )
                    request = None
                    if connection.our_state is not h11.DONE:
                        break
                    connection.start_next_cycle()
                elif isinstance(event, (h11.ConnectionClosed, h11.PAUSED)):
                    break
        except (h11.RemoteProtocolError, ConnectionError, ssl.SSLError, TimeoutError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    def _record(self, exchange: Exchange) -> None:
        with self._lock:
            self.exchanges.append(exchange)
        if self.on_exchange is not None:
            self.on_exchange(exchange)

    async def _answer(
        self,
        connection: h11.Connection,
        writer: asyncio.StreamWriter,
        request: h11.Request,
        body: bytes,
        certificate: x509.Certificate | None,
        address: str,
    ) -> None:
        method = request.method.decode("ascii")
        target = request.target.decode("latin-1")
        if not target.startswith("/"):
            # Solo la forma de origen: ``base + target`` con «@otro/» o una URL absoluta sacaría
            # la petición hacia otra máquina con las cabeceras del balanceador.
            self._record(Exchange(self.name, method, "", -1, 400, certificate is not None, (), ()))
            await self._respond(connection, writer, request, 400, [], b"")
            return
        headers: list[tuple[str, str]] = []
        dropped: list[str] = []
        for raw_name, raw_value in request.headers:
            name = raw_name.decode("latin-1").lower()
            if name == "host" and self.preserve_host:
                headers.append((name, raw_value.decode("latin-1")))
                continue
            if name in _HOP_BY_HOP:
                continue
            if name in _FORWARDED and self.set_forwarded:
                continue
            if name.startswith(MTLS_HEADER_PREFIX) and self.strip_client_mtls:
                dropped.append(name)
                continue
            headers.append((name, raw_value.decode("latin-1")))
        if self.set_forwarded:
            headers += [("x-forwarded-for", address), ("x-forwarded-proto", "https")]
        forwarded: list[tuple[str, str]] = []
        if certificate is not None and self._verifies_clients:
            forwarded = [(name.lower(), value) for name, value in alb_headers(certificate).items()]
            headers.extend(forwarded)
        forwarded_mtls = tuple(
            (name, value) for name, value in headers if name.startswith(MTLS_HEADER_PREFIX)
        )
        with self._lock:
            index = next(self._turn) % len(self.backends)
        status, response_headers, content = await self._upstream(
            self.backends[index], method, target, headers, body
        )
        upstream_error = next(
            (value for name, value in response_headers if name == UPSTREAM_ERROR_HEADER), None
        )
        self._record(
            Exchange(
                listener=self.name,
                method=method,
                path=target.split("?", 1)[0],
                backend=index,
                status=status,
                client_certificate=certificate is not None,
                forwarded_mtls=forwarded_mtls,
                dropped_mtls=tuple(dropped),
                upstream_error=upstream_error,
            )
        )
        await self._respond(connection, writer, request, status, response_headers, content)

    @staticmethod
    async def _respond(
        connection: h11.Connection,
        writer: asyncio.StreamWriter,
        request: h11.Request,
        status: int,
        headers: list[tuple[str, str]],
        content: bytes,
    ) -> None:
        if request.method != b"HEAD":
            # A un HEAD se le deja el Content-Length de la aplicación: es el tamaño del objeto.
            headers.append(("content-length", str(len(content))))
        writer.write(connection.send(h11.Response(status_code=status, headers=headers)))
        if content:
            writer.write(connection.send(h11.Data(data=content)))
        writer.write(connection.send(h11.EndOfMessage()))
        await writer.drain()

    async def _upstream(
        self, base: str, method: str, target: str, headers: list[tuple[str, str]], body: bytes
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        assert self._client is not None  # noqa: S101 - lo pone ``_open`` antes de escuchar
        try:
            upstream = self._client.build_request(
                method, base + target, headers=headers, content=body
            )
        except UnicodeEncodeError:
            # Una cabecera con bytes no ASCII: un 400, no una traza por cada cliente de ``app.``.
            return 400, [], b""
        try:
            response = await self._client.send(upstream, stream=True)
        except httpx.HTTPError as error:
            # Como el balanceador real (``HTTPCode_ELB_502_Count``), un 502; además, qué error de
            # transporte fue, en la respuesta y en la salida del proceso (sin cuerpos ni consultas).
            kind = type(error).__name__
            print(
                f"{_wall_now().isoformat()} balanceador {self.name}: 502 {method} "
                f"{target.split('?', 1)[0]} ({base}): {kind}",
                file=sys.stderr,
                flush=True,
            )
            return (
                502,
                [("content-type", "text/plain"), (UPSTREAM_ERROR_HEADER, kind)],
                f"balanceador: proceso sin respuesta ({kind})".encode(),
            )
        try:
            # Los bytes tal cual (sin descomprimir): el cliente recibe lo que envió la aplicación.
            content = b"".join([chunk async for chunk in response.aiter_raw()])
        finally:
            await response.aclose()
        kept = [
            (name, value)
            for name, value in response.headers.multi_items()
            if name.lower() not in _HOP_BY_HOP
            or (method == "HEAD" and name.lower() == "content-length")
        ]
        return response.status_code, kept, content


@contextlib.contextmanager
def mtls_proxy(
    name: str,
    backends: Sequence[str],
    tls: ServerTls,
    *,
    client_ca: Path | None = None,
    strip_client_mtls: bool = True,
    preserve_host: bool = False,
) -> Iterator[MtlsProxy]:
    proxy = MtlsProxy(
        name=name,
        backends=list(backends),
        tls=tls,
        client_ca=client_ca,
        strip_client_mtls=strip_client_mtls,
        preserve_host=preserve_host,
    )
    proxy.start()
    try:
        yield proxy
    finally:
        proxy.close()


# --- Certificado de servidor persistente ----------------------------------------------------------


def certificate_sha256(path: Path) -> str:
    """La huella SHA-256 del certificado ``path`` (la que va a ``Evidence``)."""
    certificate = x509.load_pem_x509_certificate(path.read_bytes())
    return certificate.fingerprint(hashes.SHA256()).hex()


@dataclass(frozen=True)
class InitResult:
    """Lo que dejó ``init-cert``: ``created`` es falso si el certificado ya estaba."""

    created: bool
    certificate_sha256: str
    ca_file: Path


def _name(common_name: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Vigia"),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )


def _san_names(
    dns_names: Sequence[str], addresses: Sequence[str]
) -> tuple[tuple[str, ...], tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]]:
    """Los nombres y direcciones del certificado, validados: ``app.``, ``nodes.`` y una IP."""
    names = tuple(name.lower() for name in dns_names)
    if not all(_DNS_NAME.fullmatch(name) for name in names):
        raise EdgeError("edge_san_invalid", "--san")
    if not any(name.startswith("app.") for name in names):
        raise EdgeError("edge_san_missing", "app.")
    if not any(name.startswith("nodes.") for name in names):
        raise EdgeError("edge_san_missing", "nodes.")
    if not addresses:
        raise EdgeError("edge_san_missing", "--ip")
    try:
        return names, tuple(ipaddress.ip_address(address) for address in addresses)
    except ValueError:
        raise EdgeError("edge_san_invalid", "--ip") from None


def _write_atomically(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def init_server_certificate(
    certificate_file: Path,
    key_file: Path,
    ca_file: Path,
    *,
    dns_names: Sequence[str],
    addresses: Sequence[str],
    now: dt.datetime,
) -> InitResult:
    """Genera **una vez** el certificado de servidor; repetir no lo cambia ni lo sobrescribe.

    La clave se crea primero con ``O_EXCL``: entre dos ``init-cert`` a la vez gana uno, y el otro
    termina en ``edge_server_cert_partial`` sin tocar nada. El certificado es lo último que se
    escribe: su presencia dice que el trío está completo.
    """
    names, ips = _san_names(dns_names, addresses)
    if certificate_file.exists() and key_file.exists():
        try:
            existing = x509.load_pem_x509_certificate(certificate_file.read_bytes())
            san = existing.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        except (ValueError, x509.ExtensionNotFound):
            raise EdgeError("edge_server_cert_san_mismatch") from None
        if not (
            set(names) <= set(san.get_values_for_type(x509.DNSName))
            and set(ips) <= set(san.get_values_for_type(x509.IPAddress))
        ):
            raise EdgeError("edge_server_cert_san_mismatch")
        return InitResult(False, certificate_sha256(certificate_file), ca_file)
    if certificate_file.exists() or key_file.exists():
        raise EdgeError("edge_server_cert_partial")

    authority_key = ec.generate_private_key(ec.SECP256R1())
    authority_name = _name("Vigia mtls-edge integracion")
    authority_id = x509.SubjectKeyIdentifier.from_public_key(authority_key.public_key())
    authority = (
        x509.CertificateBuilder()
        .subject_name(authority_name)
        .issuer_name(authority_name)
        .public_key(authority_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=AUTHORITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(authority_id, critical=False)
        .sign(authority_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(_name("Vigia mtls-edge servidor"))
        .issuer_name(authority_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=SERVER_DAYS))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    *(x509.DNSName(name) for name in names),
                    *(x509.IPAddress(ip) for ip in ips),
                ]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(authority_id),
            critical=False,
        )
        .sign(authority_key, hashes.SHA256())
    )
    try:
        descriptor = os.open(key_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise EdgeError("edge_server_cert_partial") from None
    with os.fdopen(descriptor, "wb") as output:
        output.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    _write_atomically(ca_file, authority.public_bytes(serialization.Encoding.PEM))
    _write_atomically(certificate_file, leaf.public_bytes(serialization.Encoding.PEM))
    return InitResult(True, certificate_sha256(certificate_file), ca_file)


# --- Raíz de los nodos ----------------------------------------------------------------------------


def validated_roots(data: bytes) -> bytes:
    """``data`` si es un paquete PEM de 1 a ``MAX_ROOTS`` raíces autofirmadas; si no, ``ValueError``
    (o la excepción de lectura).

    Mismo criterio que ``shared.node_ca.read_bundle``: un archivo a medias nunca sustituye a la
    raíz que había.
    """
    roots = x509.load_pem_x509_certificates(data)
    if not 1 <= len(roots) <= MAX_ROOTS:
        raise ValueError("el paquete no tiene de 1 a 2 raíces")
    for root in roots:
        try:
            root.verify_directly_issued_by(root)
        except (InvalidSignature, TypeError) as error:
            raise ValueError("el paquete tiene un certificado que no es autofirmado") from error
    return data


def read_client_roots(source: str) -> bytes:
    """La raíz de nodos de ``source`` (ruta o URL ``http(s)``), validada."""
    if source.startswith(("http://", "https://")):
        response = httpx.get(
            source, timeout=ROOT_FETCH_TIMEOUT_SECONDS, trust_env=False, follow_redirects=False
        )
        response.raise_for_status()
        return validated_roots(response.content)
    return validated_roots(Path(source).read_bytes())


# --- Configuración --------------------------------------------------------------------------------


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise EdgeError("edge_config_missing", name)
    return value


def _port(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    if not raw.isascii() or not raw.isdigit() or not 1 <= int(raw) <= 65_535:
        raise EdgeError("edge_config_invalid", name)
    return int(raw)


def _reload_seconds(environ: Mapping[str, str], name: str) -> float:
    raw = environ.get(name, "").strip()
    if not raw:
        return DEFAULT_RELOAD_SECONDS
    try:
        value = float(raw)
    except ValueError:
        raise EdgeError("edge_config_invalid", name) from None
    if not 0 < value < float("inf"):
        raise EdgeError("edge_config_invalid", name)
    return value


def _backends(environ: Mapping[str, str], name: str) -> tuple[str, ...]:
    values = tuple(item.strip() for item in _required(environ, name).split(","))
    if not all(_BACKEND.fullmatch(value) for value in values):
        raise EdgeError("edge_config_invalid", name)
    return values


def resolve_bind(value: str) -> str:
    """La dirección de escucha: una IP tal cual, o la IPv4 de una interfaz (``tailscale0``)."""
    with contextlib.suppress(ValueError):
        return str(ipaddress.ip_address(value))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            answer = fcntl.ioctl(
                probe.fileno(), _SIOCGIFADDR, struct.pack("256s", value[:_IFNAME_MAX].encode())
            )
        except (OSError, UnicodeError):
            raise EdgeError("edge_config_invalid", "VIGIA_EDGE_BIND") from None
    return socket.inet_ntoa(answer[20:24])


@dataclass(frozen=True)
class EdgeConfig:
    """La configuración de ``serve``, del entorno. Nada tiene un valor que abra de más: sin
    ``VIGIA_EDGE_BIND``, ``VIGIA_EDGE_BACKENDS`` o la raíz, no arranca."""

    bind: str
    backends: tuple[str, ...]
    server_cert: Path
    server_key: Path
    server_ca: Path
    client_ca: str
    app_port: int = DEFAULT_APP_PORT
    nodes_port: int = DEFAULT_NODES_PORT
    reload_seconds: float = DEFAULT_RELOAD_SECONDS

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> EdgeConfig:
        certificate, key, authority = server_paths(environ)
        return cls(
            bind=resolve_bind(_required(environ, "VIGIA_EDGE_BIND")),
            backends=_backends(environ, "VIGIA_EDGE_BACKENDS"),
            server_cert=certificate,
            server_key=key,
            server_ca=authority,
            client_ca=_required(environ, "VIGIA_EDGE_CLIENT_CA"),
            app_port=_port(environ, "VIGIA_EDGE_APP_PORT", DEFAULT_APP_PORT),
            nodes_port=_port(environ, "VIGIA_EDGE_NODES_PORT", DEFAULT_NODES_PORT),
            reload_seconds=_reload_seconds(environ, "VIGIA_EDGE_RELOAD_SECONDS"),
        )


def server_paths(environ: Mapping[str, str]) -> tuple[Path, Path, Path]:
    certificate = Path(_required(environ, "VIGIA_EDGE_SERVER_CERT"))
    key = Path(_required(environ, "VIGIA_EDGE_SERVER_KEY"))
    authority = environ.get("VIGIA_EDGE_SERVER_CA", "").strip()
    return certificate, key, Path(authority) if authority else certificate.with_name("edge-ca.pem")


def _check_server_files(certificate: Path, key: Path) -> None:
    """El certificado lo crea ``init-cert``, nunca el arranque, y su clave es solo del dueño."""
    if not certificate.is_file() or not key.is_file():
        raise EdgeError("edge_server_cert_missing")
    if stat.S_IMODE(key.stat().st_mode) & 0o077:
        raise EdgeError("edge_server_key_insecure")


def _exchange_line(exchange: Exchange) -> None:
    """El registro de un intercambio: sin cuerpos, sin cabeceras y sin los valores de la hoja."""
    _stderr_line(
        {
            "listener": exchange.listener,
            "method": exchange.method,
            "path": exchange.path,
            "backend": exchange.backend,
            "status": exchange.status,
            "client_certificate": exchange.client_certificate,
            "dropped_mtls": list(exchange.dropped_mtls),
            "upstream_error": exchange.upstream_error,
        }
    )


class MtlsEdge:
    """Las dos escuchas de integración y la recarga de la raíz de nodos."""

    def __init__(
        self,
        config: EdgeConfig,
        *,
        on_exchange: Callable[[Exchange], None] = _exchange_line,
        log: Callable[[Mapping[str, object]], None] = _stderr_line,
    ) -> None:
        _check_server_files(config.server_cert, config.server_key)
        self.config = config
        self._log = log
        self._current: bytes | None = None
        tls = ServerTls(config.server_ca, config.server_cert, config.server_key)
        self.app = MtlsProxy(
            name="app",
            backends=config.backends,
            tls=tls,
            set_forwarded=True,
            host=config.bind,
            listen_port=config.app_port,
            on_exchange=on_exchange,
            exchanges=deque(maxlen=EXCHANGES_KEPT),
        )
        self.nodes = MtlsProxy(
            name="nodes",
            backends=config.backends,
            tls=tls,
            mutual=True,
            set_forwarded=True,
            host=config.bind,
            listen_port=config.nodes_port,
            on_exchange=on_exchange,
            exchanges=deque(maxlen=EXCHANGES_KEPT),
        )

    def reload_client_roots(self) -> bool:
        """Lee la raíz; ``True`` si la cambió. Un fallo conserva la que había (nunca abre)."""
        try:
            roots = read_client_roots(self.config.client_ca)
            if roots == self._current:
                return False
            self.nodes.use_client_roots(roots)
        except (OSError, ValueError, ssl.SSLError, httpx.HTTPError, httpx.InvalidURL) as error:
            self._log({"event": "edge_client_ca_reload_failed", "kind": type(error).__name__})
            return False
        self._current = roots
        self._log({"event": "edge_client_ca_loaded"})
        return True

    def start(self) -> None:
        self.reload_client_roots()
        self.app.start()
        self.nodes.start()

    def close(self) -> None:
        self.nodes.close()
        self.app.close()

    def run(self, stop: threading.Event) -> None:
        """Recarga la raíz cada ``reload_seconds`` hasta ``stop``."""
        while not stop.wait(self.config.reload_seconds):
            self.reload_client_roots()


def run_edge(config: EdgeConfig, stop: threading.Event) -> None:
    edge = MtlsEdge(config)
    edge.start()
    try:
        _stderr_line(
            {
                "event": "edge_started",
                "bind": config.bind,
                "app_port": edge.app.port,
                "nodes_port": edge.nodes.port,
                "server_certificate_sha256": certificate_sha256(config.server_cert),
            }
        )
        edge.run(stop)
    finally:
        edge.close()


# --- Órdenes --------------------------------------------------------------------------------------


def _print_json(document: Mapping[str, object], stream: TextIO) -> None:
    print(json.dumps(document, sort_keys=True), file=stream, flush=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vigia-mtls-edge",
        description="Balanceador local de personas y de nodos para el entorno de integración.",
    )
    orders = parser.add_subparsers(dest="order", required=True)
    orders.add_parser("serve", help="escucha en VIGIA_EDGE_BIND (app. y nodes.)")
    init_cert = orders.add_parser(
        "init-cert", help="genera una vez el certificado de servidor persistente"
    )
    init_cert.add_argument(
        "--san", action="append", default=[], help="app.<dominio>, nodes.<dominio>"
    )
    init_cert.add_argument("--ip", action="append", default=[], help="IP de la LAN o de la tailnet")
    return parser


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    values = os.environ if environ is None else environ
    try:
        if arguments.order == "init-cert":
            certificate, key, authority = server_paths(values)
            result = init_server_certificate(
                certificate,
                key,
                authority,
                dns_names=arguments.san,
                addresses=arguments.ip,
                now=_wall_now(),
            )
            _print_json(
                {
                    "created": result.created,
                    "certificate_sha256": result.certificate_sha256,
                    "ca_file": str(result.ca_file),
                },
                sys.stdout,
            )
            return 0
        config = EdgeConfig.from_env(values)
        stop = threading.Event()
        for number in (signal.SIGTERM, signal.SIGINT):
            signal.signal(number, lambda *_: stop.set())
        run_edge(config, stop)
        return 0
    except EdgeError as error:
        _print_json({"error": error.code, "field": error.field_name}, sys.stderr)
        return 2
    except OSError as error:
        # Un puerto ocupado, una dirección que no es de la máquina o un directorio que no existe.
        _print_json({"error": "edge_os_error", "kind": type(error).__name__}, sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
