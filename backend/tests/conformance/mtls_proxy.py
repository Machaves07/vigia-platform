"""Balanceador local de la suite de conformidad: termina TLS como los dos balanceadores de U-03.

``infrastructure-design.md`` §5 de U-03: el de **nodos** (``nodes.``) termina mTLS en modo
*verify* contra el almacén de confianza de ``vigia-node-ca`` y pasa a la aplicación la hoja y sus
datos en las cabeceras ``X-Amzn-Mtls-Clientcert-{Leaf,Serial-Number,Subject,Issuer,Validity}``;
el de **personas** (``app.``) no pide certificado y quita cualquier ``X-Amzn-Mtls-*`` entrante
(U-02 §4.2). ``MtlsProxy`` hace lo mismo en local, en un hilo con su propio bucle:

- ``client_ca`` dado (``nodes.``): pide certificado de cliente (opcional, para que la petición sin
  certificado llegue a la aplicación y responda ``node_not_enrolled``) y lo verifica contra esa
  raíz en el saludo TLS; un certificado de otra autoridad no completa el saludo, como en el
  balanceador real. Con certificado, añade las cinco cabeceras (``alb_headers``).
- **Siempre** descarta las ``X-Amzn-Mtls-*`` que traiga el cliente, en los dos: la aplicación solo
  ve las que pone el balanceador.
- Reparte **por petición** en turno rotatorio entre ``backends`` (dos procesos de ``vigia-api`` en
  NFR-GOB-15); una conexión persistente del cliente no queda atada a un proceso.

Cada intercambio queda en ``exchanges`` (ruta, proceso, estado y las cabeceras ``X-Amzn-Mtls-*``
enviadas a la aplicación), nunca el cuerpo. ``server_tls`` crea la autoridad efímera del
certificado de servidor (``127.0.0.1``), nunca versionada (NFR-CTR-14).

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import itertools
import ssl
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from ipaddress import IPv4Address
from pathlib import Path
from typing import Final

import h11
import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from tests.node_api_support import alb_headers

__all__ = [
    "MTLS_HEADER_PREFIX",
    "Exchange",
    "MtlsProxy",
    "ServerTls",
    "mtls_proxy",
    "server_tls",
]

MTLS_HEADER_PREFIX: Final = "x-amzn-mtls-"
UPSTREAM_TIMEOUT_SECONDS: Final = 120.0
"""Tope de la petición a la aplicación: nunca decide una prueba (retro 15)."""
START_TIMEOUT_SECONDS: Final = 30.0
_READ_CHUNK: Final = 65_536
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


@dataclass(frozen=True)
class ServerTls:
    """Certificado de servidor del balanceador local y su raíz (rutas en un directorio temporal)."""

    ca_file: Path
    certificate_file: Path
    key_file: Path


def _name(common_name: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Vigia"),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )


def server_tls(directory: Path, now: dt.datetime) -> ServerTls:
    """Raíz efímera ``test-only`` y certificado de servidor para ``127.0.0.1`` y ``localhost``."""
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = _name("Vigia balanceador local test-only")
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(_name("127.0.0.1"))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(IPv4Address("127.0.0.1")), x509.DNSName("localhost")]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    tls = ServerTls(
        directory / "balanceador-ca.crt",
        directory / "balanceador.crt",
        directory / "balanceador.key",
    )
    tls.ca_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    tls.certificate_file.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    tls.key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    tls.key_file.chmod(0o600)
    return tls


@dataclass(frozen=True)
class Exchange:
    """Un intercambio que pasó por el balanceador (sin cuerpos)."""

    listener: str
    method: str
    path: str
    backend: int
    """Índice en ``backends`` del proceso que lo atendió."""
    status: int
    client_certificate: bool
    forwarded_mtls: tuple[tuple[str, str], ...]
    """Las cabeceras ``X-Amzn-Mtls-*`` que llegaron a la aplicación (nombre en minúsculas)."""
    dropped_mtls: tuple[str, ...]
    """Los nombres de las ``X-Amzn-Mtls-*`` que traía el cliente y se descartaron."""


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
    exchanges: list[Exchange] = field(default_factory=list)
    _loop: asyncio.AbstractEventLoop = field(default_factory=asyncio.new_event_loop)
    _thread: threading.Thread | None = None
    _server: asyncio.Server | None = None
    _client: httpx.AsyncClient | None = None
    _turn: Iterator[int] = field(default_factory=itertools.count)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    port: int = 0

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.port}"

    def _context(self) -> ssl.SSLContext:
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(self.tls.certificate_file, self.tls.key_file)
        if self.client_ca is not None:
            context.load_verify_locations(cafile=str(self.client_ca))
            context.verify_mode = ssl.CERT_OPTIONAL
        else:
            context.verify_mode = ssl.CERT_NONE
        return context

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop.run_forever, name=f"balanceador-{self.name}", daemon=True
        )
        self._thread.start()
        future = asyncio.run_coroutine_threadsafe(self._open(), self._loop)
        future.result(timeout=START_TIMEOUT_SECONDS)

    async def _open(self) -> None:
        self._client = httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_SECONDS)
        self._server = await asyncio.start_server(
            self._serve, host="127.0.0.1", port=0, ssl=self._context()
        )
        self.port = int(self._server.sockets[0].getsockname()[1])

    def close(self) -> None:
        if self._thread is None:
            return

        async def shut() -> None:
            if self._server is not None:
                self._server.close()
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
        ssl_object = writer.get_extra_info("ssl_object")
        der = ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None
        certificate = x509.load_der_x509_certificate(der) if der else None
        connection = h11.Connection(h11.SERVER)
        request: h11.Request | None = None
        body = bytearray()
        try:
            while True:
                event = connection.next_event()
                if event is h11.NEED_DATA:
                    connection.receive_data(await reader.read(_READ_CHUNK))
                    continue
                if isinstance(event, h11.Request):
                    request, body = event, bytearray()
                elif isinstance(event, h11.Data):
                    body += event.data
                elif isinstance(event, h11.EndOfMessage) and request is not None:
                    await self._answer(connection, writer, request, bytes(body), certificate)
                    request = None
                    if connection.our_state is not h11.DONE:
                        break
                    connection.start_next_cycle()
                elif isinstance(event, (h11.ConnectionClosed, h11.PAUSED)):
                    break
        except (h11.RemoteProtocolError, ConnectionError, ssl.SSLError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _answer(
        self,
        connection: h11.Connection,
        writer: asyncio.StreamWriter,
        request: h11.Request,
        body: bytes,
        certificate: x509.Certificate | None,
    ) -> None:
        headers: list[tuple[str, str]] = []
        dropped: list[str] = []
        for raw_name, raw_value in request.headers:
            name = raw_name.decode("latin-1").lower()
            if name == "host" and self.preserve_host:
                headers.append((name, raw_value.decode("latin-1")))
                continue
            if name in _HOP_BY_HOP:
                continue
            if name.startswith(MTLS_HEADER_PREFIX) and self.strip_client_mtls:
                dropped.append(name)
                continue
            headers.append((name, raw_value.decode("latin-1")))
        forwarded: list[tuple[str, str]] = []
        if certificate is not None and self.client_ca is not None:
            forwarded = [(name.lower(), value) for name, value in alb_headers(certificate).items()]
            headers.extend(forwarded)
        forwarded_mtls = tuple(
            (name, value) for name, value in headers if name.startswith(MTLS_HEADER_PREFIX)
        )
        with self._lock:
            index = next(self._turn) % len(self.backends)
        target = request.target.decode("latin-1")
        status, response_headers, content = await self._upstream(
            self.backends[index], request.method.decode("ascii"), target, headers, body
        )
        with self._lock:
            self.exchanges.append(
                Exchange(
                    listener=self.name,
                    method=request.method.decode("ascii"),
                    path=target.split("?", 1)[0],
                    backend=index,
                    status=status,
                    client_certificate=certificate is not None,
                    forwarded_mtls=forwarded_mtls,
                    dropped_mtls=tuple(dropped),
                )
            )
        if request.method != b"HEAD":
            # A un HEAD se le deja el Content-Length de la aplicación: es el tamaño del objeto.
            response_headers.append(("content-length", str(len(content))))
        writer.write(connection.send(h11.Response(status_code=status, headers=response_headers)))
        if content:
            writer.write(connection.send(h11.Data(data=content)))
        writer.write(connection.send(h11.EndOfMessage()))
        await writer.drain()

    async def _upstream(
        self, base: str, method: str, target: str, headers: list[tuple[str, str]], body: bytes
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        assert self._client is not None
        upstream = self._client.build_request(method, base + target, headers=headers, content=body)
        try:
            response = await self._client.send(upstream, stream=True)
        except httpx.HTTPError:
            return 502, [("content-type", "text/plain")], b"balanceador: proceso sin respuesta"
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
