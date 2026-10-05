"""Perfiles de los certificados del nodo y su emisión con ``vigia-node-ca`` (BR-GOB-63, 64;
NFR-GOB-34, 43; pendiente nº 29; PAT-GOB-SEG-05, PAT-GOB-RES-03).

**Cliente** (``client_builder``): sujeto ``{node_id, organization_id, plant_id}`` con la función
única de ``fleet.domain.node_subject`` (la misma que lee ``node_api.identity``), número de serie
aleatorio de 159 bits, **365 días** desde el segundo de la emisión, ``BasicConstraints(ca=False)``
crítica, ``KeyUsage(digital_signature)`` crítica, ``ExtendedKeyUsage(clientAuth)`` e
identificadores de clave de sujeto y de autoridad.

**Servidor de la vista en vivo** (``server_builder``): el mismo sujeto y vigencia, ``serverAuth``
y ``SubjectAlternativeName`` con **una sola** entrada, la dirección privada o el nombre local que
eligió ``csr.announced_host``. ``NameConstraints`` no se pone: RFC 5280 §4.2.1.10 la reserva a los
certificados de autoridad; la limitación a la dirección local es esa única entrada.

De la CSR solo se usa la clave pública: ninguna extensión, sujeto ni vigencia propuestos.

**Emisión** (``NodeCaIssuer.issue``): lee el paquete publicado ``ca/root.pem`` (1 o 2 raíces,
D-6), toma la clave pública de ``vigia-node-ca`` de KMS (una vez por proceso: es pública), elige
la raíz del paquete con **esa** clave como emisora (nombre e identificador de autoridad) y firma
los dos certificados con ``shared.node_ca.sign_certificate`` (``kms:Sign``, ECDSA P-256 con
SHA-256), que comprueba cada firma con la clave pública. Después, cada certificado verifica contra
la raíz (``verify_directly_issued_by``) antes de entregar nada.

**Fallo cerrado** (FS-GOB-05 base): todo el tramo tiene un plazo de ``NODE_CA_DEADLINE_SECONDS``
(4 s ``[objetivo propio]``, por debajo de los 5 s de NFR-GOB-43, para que el alta y la rotación
respondan en ≤ 5 s con KMS sin respuesta). KMS que no responde, que falla, una raíz que no casa
con la clave o un paquete ilegible terminan en ``NodeCaUnavailable`` (transitorio: la ruta
responde ``temporarily_unavailable`` con ``retry_after_seconds``) sin haber escrito nada.
``node_ca_sign_duration_ms`` mide cada ``kms:Sign`` por resultado; nunca lleva el PEM.
"""

from __future__ import annotations

import asyncio
import enum
import ipaddress
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID

from vigia_platform.fleet.adapters.ca.csr import LiveViewHost
from vigia_platform.fleet.domain.node_credential import VALIDITY
from vigia_platform.fleet.domain.node_subject import NodeSubject, subject_name
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.node_ca import (
    ROOT_CERTIFICATE_KEY,
    NodeCaError,
    NodeCaSigner,
    certificate_pem,
    kms_public_key,
    read_bundle,
    sign_certificate,
)
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics

__all__ = [
    "NODE_CA_DEADLINE_SECONDS",
    "SERIAL_BYTES",
    "IssuedCertificates",
    "NodeCaIssuer",
    "NodeCaUnavailable",
    "SignResult",
    "client_builder",
    "random_serial",
    "server_builder",
]

NODE_CA_DEADLINE_SECONDS: Final = 4.0
"""Plazo de todo el tramo de la autoridad (paquete, clave pública y las dos firmas)."""
SERIAL_BYTES: Final = 20

_log = get_logger("fleet.credentials")


class NodeCaUnavailable(Exception):
    """La autoridad no emitió a tiempo o no se puede usar ahora: transitorio, nada escrito."""


class SignResult(enum.StrEnum):
    """``result`` de ``node_ca_sign_duration_ms``."""

    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"


def random_serial(random_bytes: Callable[[int], bytes]) -> int:
    """Número de serie positivo y menor que 2^159 (RFC 5280: 20 octetos como mucho)."""
    raw = random_bytes(SERIAL_BYTES)
    if not isinstance(raw, bytes) or len(raw) != SERIAL_BYTES:
        raise ValueError("el generador no devolvió 20 bytes para el número de serie")
    return (int.from_bytes(raw, "big") >> 1) | 1


def _base(
    public_key: ec.EllipticCurvePublicKey,
    subject: NodeSubject,
    issuer: x509.Certificate,
    *,
    serial: int,
    not_before: datetime,
) -> x509.CertificateBuilder:
    if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
        public_key.curve, ec.SECP256R1
    ):
        raise ValueError("la clave del nodo debe ser EC P-256")
    start = not_before.astimezone(UTC).replace(microsecond=0)
    issuer_key = issuer.public_key()
    if not isinstance(issuer_key, ec.EllipticCurvePublicKey):
        raise NodeCaError("la raíz de la autoridad no tiene clave EC")
    return (
        x509.CertificateBuilder()
        .subject_name(subject_name(subject))
        .issuer_name(issuer.subject)
        .public_key(public_key)
        .serial_number(serial)
        .not_valid_before(start)
        .not_valid_after(start + VALIDITY)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key), critical=False
        )
    )


def client_builder(
    public_key: ec.EllipticCurvePublicKey,
    subject: NodeSubject,
    issuer: x509.Certificate,
    *,
    serial: int,
    not_before: datetime,
) -> x509.CertificateBuilder:
    """El certificado de cliente del nodo (``clientAuth``), sin nada de la CSR salvo la clave."""
    return _base(public_key, subject, issuer, serial=serial, not_before=not_before).add_extension(
        x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False
    )


def _general_name(host: LiveViewHost) -> x509.GeneralName:
    if isinstance(host, ipaddress.IPv4Address | ipaddress.IPv6Address):
        return x509.IPAddress(host)
    return x509.DNSName(host)


def server_builder(
    public_key: ec.EllipticCurvePublicKey,
    subject: NodeSubject,
    issuer: x509.Certificate,
    host: LiveViewHost,
    *,
    serial: int,
    not_before: datetime,
) -> x509.CertificateBuilder:
    """El certificado de servidor de la vista en vivo (``serverAuth``, una sola dirección)."""
    return (
        _base(public_key, subject, issuer, serial=serial, not_before=not_before)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName([_general_name(host)]), critical=False)
    )


@dataclass(frozen=True, slots=True)
class IssuedCertificates:
    """Los dos certificados firmados y la cadena publicada que los valida."""

    client: x509.Certificate
    server: x509.Certificate
    root: x509.Certificate
    ca_chain: str
    """El paquete ``ca/root.pem`` publicado (1 o 2 raíces), en PEM."""


class RootObjects(Protocol):
    """``vigia-edge``: solo la lectura de ``ca/root.pem``."""

    async def get_object(self, key: str, *, version_id: str | None = None) -> bytes: ...


class _TimedSigner:
    """``NodeCaSigner`` que mide cada ``kms:Sign`` (``node_ca_sign_duration_ms``)."""

    def __init__(self, target: NodeCaSigner, clock: Clock, metrics: PlatformMetrics) -> None:
        self._target = target
        self._clock = clock
        self._metrics = metrics

    async def get_public_key(self, key_id: str) -> bytes:
        return await self._target.get_public_key(key_id)

    async def sign(self, key_id: str, message: bytes) -> bytes:
        start = self._clock.monotonic()
        result = SignResult.ERROR
        try:
            signature = await self._target.sign(key_id, message)
            result = SignResult.OK
            return signature
        except asyncio.CancelledError:
            result = SignResult.TIMEOUT
            raise
        finally:
            elapsed = max(0.0, self._clock.monotonic() - start) * 1000
            self._metrics.node_ca_sign_duration_ms.record(elapsed, {"result": result})


def _same_key(certificate: x509.Certificate, public: ec.EllipticCurvePublicKey) -> bool:
    spki = serialization.PublicFormat.SubjectPublicKeyInfo
    der = serialization.Encoding.DER
    mine = certificate.public_key()
    return isinstance(mine, ec.EllipticCurvePublicKey) and mine.public_bytes(
        der, spki
    ) == public.public_bytes(der, spki)


class NodeCaIssuer:
    """Firma los certificados del nodo con ``vigia-node-ca`` (KMS) contra el paquete publicado."""

    def __init__(
        self,
        *,
        kms: NodeCaSigner,
        key_id: str | None,
        roots: RootObjects | None,
        clock: Clock,
        random_bytes: Callable[[int], bytes],
        metrics: PlatformMetrics | None = None,
        deadline_seconds: float = NODE_CA_DEADLINE_SECONDS,
        root_key: str = ROOT_CERTIFICATE_KEY,
    ) -> None:
        self._kms = kms
        self._key_id = key_id
        self._roots = roots
        self._clock = clock
        self._random_bytes = random_bytes
        self._metrics = metrics
        self._deadline = deadline_seconds
        self._root_key = root_key
        self._public: ec.EllipticCurvePublicKey | None = None

    def __repr__(self) -> str:
        return "NodeCaIssuer()"

    async def issue(
        self,
        subject: NodeSubject,
        client_key: ec.EllipticCurvePublicKey,
        server_key: ec.EllipticCurvePublicKey,
        host: LiveViewHost,
        *,
        now: datetime,
    ) -> IssuedCertificates:
        """Los dos certificados de ``subject`` firmados y verificados, o ``NodeCaUnavailable``."""
        try:
            async with asyncio.timeout(self._deadline):
                return await self._issue(subject, client_key, server_key, host, now)
        except NodeCaUnavailable:
            raise
        except TimeoutError:
            _log.warning("la autoridad de nodos no respondió en el plazo")
            raise NodeCaUnavailable("vigia-node-ca no respondió a tiempo") from None
        except Exception:
            # KMS caído o que niega, paquete ilegible, firma que no verifica: fallo cerrado. Ni el
            # mensaje ni el PEM llegan al registro.
            _log.error("la autoridad de nodos no pudo emitir")
            raise NodeCaUnavailable("vigia-node-ca no pudo emitir") from None

    async def _issuer(self) -> tuple[str, ec.EllipticCurvePublicKey, x509.Certificate, str]:
        key_id, roots = self._key_id, self._roots
        if key_id is None or roots is None:
            raise NodeCaUnavailable("este proceso no tiene la autoridad configurada")
        bundle_bytes = await roots.get_object(self._root_key)
        bundle = read_bundle(bundle_bytes)
        public = self._public
        if public is None:
            public = await kms_public_key(self._kms, key_id)
            self._public = public
        matching = [root for root in bundle if _same_key(root, public)]
        if len(matching) != 1:
            raise NodeCaError("ninguna raíz publicada es de la clave de la autoridad")
        chain = "".join(certificate_pem(root).decode("ascii") for root in bundle)
        return key_id, public, matching[0], chain

    async def _issue(
        self,
        subject: NodeSubject,
        client_key: ec.EllipticCurvePublicKey,
        server_key: ec.EllipticCurvePublicKey,
        host: LiveViewHost,
        now: datetime,
    ) -> IssuedCertificates:
        key_id, public, root, chain = await self._issuer()
        metrics = self._metrics if self._metrics is not None else get_metrics()
        signer = _TimedSigner(self._kms, self._clock, metrics)
        client = client_builder(
            client_key, subject, root, serial=random_serial(self._random_bytes), not_before=now
        )
        server = server_builder(
            server_key,
            subject,
            root,
            host,
            serial=random_serial(self._random_bytes),
            not_before=now,
        )
        signed = await asyncio.gather(
            sign_certificate(client, signer, key_id, issuer_public_key=public),
            sign_certificate(server, signer, key_id, issuer_public_key=public),
        )
        for certificate in signed:
            certificate.verify_directly_issued_by(root)
        return IssuedCertificates(client=signed[0], server=signed[1], root=root, ca_chain=chain)
