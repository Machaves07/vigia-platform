"""Validación de las solicitudes de firma del nodo (BR-GOB-63; LC-GOB-11; tech-stack §2.2).

``read_csr`` carga una CSR PKCS #10 con ``cryptography.x509.load_pem_x509_csr`` y exige, en este
orden: PEM de 1 a 8 192 caracteres con **un solo** bloque ``CERTIFICATE REQUEST`` y nada más que
espacios alrededor; algoritmo **ECDSA con SHA-256**; clave pública EC **SECP256R1**; autofirma
verificada (``is_signature_valid``); extensiones legibles (una extensión repetida o un nombre
alternativo de un tipo que ``cryptography`` no lee también es una CSR no válida); y un sujeto con
**exactamente un** nombre común que es un UUID canónico (el ``node_id`` que propone el nodo).
Cualquier otra forma, y **cualquier** excepción al leerla, es ``CsrRejected`` con el campo del
cuerpo que la trajo: la ruta responde ``schema_invalid`` con ese ``field`` (422, A-37), nunca un
transitorio.

**Nada de la CSR pasa al certificado** salvo su clave pública (PAT-GOB-SEG-05): sujeto,
extensiones y vigencia los compone ``certificate_profiles`` desde el estado de la plataforma. El
nombre alternativo que anuncia la CSR de servidor solo se **lee** (``announced_host``) para elegir
la dirección de la vista en vivo, y se vuelve a escribir desde cero tras comprobar que es local.

**Dirección de la vista en vivo** (``announced_host``; pendiente nº 29, nota U03-H-07): si el nodo
ya anunció ``live_view_local_url`` (A-35), manda su anfitrión; si no, el **único** nombre
alternativo de la CSR de servidor. En los dos casos tiene que ser una dirección privada (IPv4
privada o IPv6 local única; una IPv4 escrita como IPv6 mapeada se decide como IPv4; nunca bucle
local, enlace local, multidifusión ni sin especificar) o un nombre local (una etiqueta sola, o
terminado en ``.local``, ``.lan``, ``.internal`` o ``.home.arpa``; nunca ``localhost``). Nunca una
dirección pública ni un nombre de Internet: el certificado de servidor no vale fuera de la planta.

Módulo puro: sin FastAPI, sin SQLAlchemy y sin leer la hora del sistema.
"""

from __future__ import annotations

import ipaddress
import re
import uuid
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID, SignatureAlgorithmOID

__all__ = [
    "CLIENT_CSR_FIELD",
    "MAX_CSR_PEM_CHARS",
    "SERVER_CSR_FIELD",
    "CsrRejected",
    "LiveViewHost",
    "NodeCsr",
    "announced_host",
    "read_csr",
]

MAX_CSR_PEM_CHARS: Final = 8_192
"""Tope del PEM de la CSR (BR-GOB-63, ``CertificateSigningRequestPem`` de U-01)."""
CLIENT_CSR_FIELD: Final = "certificate_signing_request"
SERVER_CSR_FIELD: Final = "server_certificate_signing_request"
"""``field`` del ``schema_invalid``: la ruta JSON del campo (el contrato no admite ``/``)."""

_UUID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_LABEL: Final = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
_LOCAL_SUFFIXES: Final = (".local", ".lan", ".internal", ".home.arpa")
_MAX_HOST_CHARS: Final = 253
_LOOPBACK_NAMES: Final = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})
_PEM_BEGIN: Final = "-----BEGIN CERTIFICATE REQUEST-----"
_PEM_END: Final = "-----END CERTIFICATE REQUEST-----"

type LiveViewHost = ipaddress.IPv4Address | ipaddress.IPv6Address | str
"""La dirección privada o el nombre local de la vista en vivo del nodo."""


class CsrRejected(ValueError):
    """La CSR no es válida: ``schema_invalid`` con ``field`` (nunca repite el contenido)."""

    def __init__(self, field: str) -> None:
        super().__init__(f"solicitud de firma no válida en {field}")
        self.field = field


@dataclass(frozen=True, slots=True)
class NodeCsr:
    """Lo único que la plataforma toma de una CSR válida."""

    node_id: uuid.UUID
    """El nombre común propuesto (se contrasta con el nodo declarado; nunca se copia)."""
    public_key: ec.EllipticCurvePublicKey
    announced: tuple[x509.GeneralName, ...]
    """Los nombres alternativos que la CSR anuncia: solo se leen (``announced_host``)."""


def read_csr(pem: object, *, field: str) -> NodeCsr:
    """La CSR de ``pem`` validada; ``CsrRejected(field)`` ante cualquier otra forma."""
    if not isinstance(pem, str) or not 0 < len(pem) <= MAX_CSR_PEM_CHARS:
        raise CsrRejected(field)
    body = pem.strip()
    if (
        not body.startswith(_PEM_BEGIN)
        or not body.endswith(_PEM_END)
        or body.count("-----BEGIN") != 1
        or body.count("-----END") != 1
    ):
        raise CsrRejected(field)  # un solo bloque, sin texto alrededor
    try:
        csr = x509.load_pem_x509_csr(pem.encode("ascii"))
        if csr.signature_algorithm_oid != SignatureAlgorithmOID.ECDSA_WITH_SHA256:
            raise CsrRejected(field)
        public_key = csr.public_key()
        if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
            public_key.curve, ec.SECP256R1
        ):
            raise CsrRejected(field)
        if not csr.is_signature_valid:
            raise CsrRejected(field)
        announced = _announced(csr)
        common_names = csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    except CsrRejected:
        raise
    except Exception:
        # Entrada hostil del nodo, no fallo de la plataforma: además de ValueError, TypeError,
        # UnicodeError y UnsupportedAlgorithm, ``cryptography`` lanza ``DuplicateExtension`` o
        # ``UnsupportedGeneralNameType`` (que no heredan de ValueError) al leer las extensiones.
        # Todo es schema_invalid (422), nunca un transitorio.
        raise CsrRejected(field) from None
    if len(common_names) != 1:
        raise CsrRejected(field)
    value = common_names[0].value
    if not isinstance(value, str) or _UUID.fullmatch(value) is None:
        raise CsrRejected(field)
    return NodeCsr(node_id=uuid.UUID(value), public_key=public_key, announced=announced)


def _announced(csr: x509.CertificateSigningRequest) -> tuple[x509.GeneralName, ...]:
    """Los nombres alternativos de la CSR (leer las extensiones valida que no estén rotas)."""
    try:
        extension = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return ()
    return tuple(extension.value)


def _local_ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        address: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped  # ::ffff:a.b.c.d se decide como a.b.c.d
    if (
        not address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
        or address.is_reserved
    ):
        return None
    return address


def _local_name(text: str) -> str | None:
    name = text.lower()
    if not 0 < len(name) <= _MAX_HOST_CHARS or name != text or name in _LOOPBACK_NAMES:
        return None
    labels = name.split(".")
    if not all(_LABEL.fullmatch(label) for label in labels):
        return None
    if len(labels) == 1 and not name.isdigit():
        return name
    if any(name.endswith(suffix) for suffix in _LOCAL_SUFFIXES):
        return name
    return None


def _host_from_url(url: str) -> LiveViewHost | None:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    return _local_ip(host) or _local_name(host)


def announced_host(
    server: NodeCsr, live_view_local_url: str | None, *, field: str = SERVER_CSR_FIELD
) -> LiveViewHost:
    """La dirección del certificado de servidor: la de ``live_view_local_url`` si se conoce; si
    no, el único nombre alternativo de la CSR. Local o ``CsrRejected(field)``."""
    if live_view_local_url is not None:
        host = _host_from_url(live_view_local_url)
        if host is None:
            raise CsrRejected(field)
        return host
    if len(server.announced) != 1:
        raise CsrRejected(field)
    (name,) = server.announced
    if isinstance(name, x509.IPAddress) and isinstance(
        name.value, ipaddress.IPv4Address | ipaddress.IPv6Address
    ):
        address = _local_ip(str(name.value))
        if address is not None:
            return address
    elif isinstance(name, x509.DNSName):
        local = _local_name(name.value)
        if local is not None:
            return local
    raise CsrRejected(field)
