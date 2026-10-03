"""Raíz de la autoridad de nodos ``vigia-node-ca`` (LC-NUC-07; ``infrastructure-design.md`` §7.1).

La raíz es un certificado X.509 **autofirmado** cuyo campo de clave pública es la de la clave
asimétrica de KMS (ECC P-256, ``SIGN_VERIFY``). La plataforma construye el ``TBSCertificate`` con
``cryptography`` y obtiene la firma con ``kms:Sign`` (ECDSA P-256 con SHA-256, el perfil de
NFR-CTR-13): **la clave privada nunca existe fuera de KMS**, ni siquiera una efímera. Para eso
``sign_certificate`` entrega a ``CertificateBuilder.sign`` una «clave» que solo conoce la parte
pública: en una primera pasada recoge los bytes del ``TBSCertificate`` (que no dependen de la
firma), los firma en KMS y en la segunda pasada devuelve esa firma. Después comprueba la firma
con la clave pública: un KMS que firmara con otra clave no deja un certificado publicado.

- ``build_root_certificate``: la raíz de ``ROOT_VALIDITY_YEARS`` años con ``BasicConstraints``
  (CA, longitud de cadena 0) y ``KeyUsage`` (``keyCertSign`` y ``cRLSign``), críticos.
- ``NodeCaPublisher.publish_root``: ``vigia-admin bootstrap`` publica **solo**
  ``vigia-edge/ca/root.pem``; la lista de revocación ``ca/crl.pem`` la publica el worker (D-7).
- ``NodeCaPublisher.rotate_root``: ``vigia-admin rotate-node-ca`` (``ca_rotation=true``) firma
  la raíz nueva con la clave nueva y publica ``ca/root.pem`` como **paquete de dos raíces**, la
  vigente y la nueva (D-6, ``deployment-architecture.md`` §6.4): un nodo con credencial de la
  raíz anterior sigue validando y pasa a la nueva en su rotación normal. Con dos raíces ya
  publicadas (una sustitución en curso) no se añade una tercera.
- ``prepare_root`` y ``prepare_rotation`` firman y arman el paquete **sin escribir nada**;
  ``publish`` es la única escritura. Así ``vigia-admin`` audita la intención (con la huella de la
  raíz nueva) antes de publicar y el resultado después: el depósito y la base no comparten
  transacción, y nunca queda una raíz publicada sin su entrada (revisión de VIG-93).

No lee la hora del sistema: ``now`` llega del ``Clock`` del llamador.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.storage import ObjectHead

__all__ = [
    "MAX_BUNDLE_ROOTS",
    "ROOT_CERTIFICATE_KEY",
    "ROOT_CONTENT_TYPE",
    "ROOT_VALIDITY_YEARS",
    "NodeCaError",
    "NodeCaPublisher",
    "NodeCaSigner",
    "PreparedRoot",
    "PublishedRoot",
    "RootStorage",
    "build_root_certificate",
    "certificate_pem",
    "fingerprint",
    "read_bundle",
    "sign_certificate",
    "two_root_bundle",
]

ROOT_VALIDITY_YEARS: Final = 10
"""Vigencia de la raíz ``[objetivo propio]`` (§7.1)."""
ROOT_CERTIFICATE_KEY: Final = "ca/root.pem"
"""Objeto de la raíz en ``vigia-edge`` (``infra/stacks/edge.py``)."""
ROOT_CONTENT_TYPE: Final = "application/x-pem-file"
MAX_BUNDLE_ROOTS: Final = 2
"""La vigente y la nueva, durante una sustitución (D-6)."""
_SERIAL_BYTES: Final = 20
_COMMON_NAME: Final = "Vigia Node CA"
_ORGANIZATION: Final = "Vigia"

_log = get_logger("shared.node_ca")


class NodeCaError(Exception):
    """La raíz no se puede emitir o publicar (clave que no es P-256, paquete no válido…).

    El mensaje, en español, nunca lleva un ARN ni material de clave.
    """


class RootStorage(Protocol):
    """La parte de ``StoragePort`` que usa la publicación (depósito ``vigia-edge``)."""

    async def get_object(self, key: str, *, version_id: str | None = None) -> bytes: ...

    async def put_object(
        self,
        key: str,
        body: bytes,
        content_type: str,
        *,
        kms_key_id: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectHead: ...


class NodeCaSigner(Protocol):
    """La parte de ``KmsPort`` que usa la autoridad (``shared.secrets.KmsAdapter``)."""

    async def sign(self, key_id: str, message: bytes) -> bytes: ...

    async def get_public_key(self, key_id: str) -> bytes: ...


# --- Firma con KMS ------------------------------------------------------------------------------


class _TbsCaptured(Exception):
    """Fin de la primera pasada: ya se tienen los bytes del ``TBSCertificate``."""

    def __init__(self, tbs: bytes) -> None:
        super().__init__("TBSCertificate recogido")
        self.tbs = tbs


class _KmsBackedKey(ec.EllipticCurvePrivateKey):
    """Lo que ``CertificateBuilder.sign`` necesita de una clave, sin material privado.

    Sin ``signature`` recoge el ``TBSCertificate`` y corta la firma; con ella, la devuelve solo
    para esos mismos bytes. Ninguna operación privada (intercambio, exportación) existe.
    """

    def __init__(
        self,
        public: ec.EllipticCurvePublicKey,
        *,
        tbs: bytes | None = None,
        signature: bytes | None = None,
    ) -> None:
        self._public = public
        self._tbs = tbs
        self._signature = signature

    def sign(
        self,
        data: bytes | bytearray | memoryview,
        signature_algorithm: ec.EllipticCurveSignatureAlgorithm,
    ) -> bytes:
        if not isinstance(signature_algorithm, ec.ECDSA) or not isinstance(
            signature_algorithm.algorithm, hashes.SHA256
        ):
            raise NodeCaError("la autoridad solo firma con ECDSA y SHA-256")
        if self._signature is None:
            raise _TbsCaptured(bytes(data))
        if bytes(data) != self._tbs:
            raise NodeCaError("el TBSCertificate cambió entre las dos pasadas")
        return self._signature

    def public_key(self) -> ec.EllipticCurvePublicKey:
        return self._public

    @property
    def curve(self) -> ec.EllipticCurve:
        return self._public.curve

    @property
    def key_size(self) -> int:
        return self._public.key_size

    def exchange(
        self, algorithm: ec.ECDH, peer_public_key: ec.EllipticCurvePublicKey
    ) -> bytes:  # pragma: no cover - la autoridad no intercambia claves
        raise NodeCaError("la clave de la autoridad vive en KMS")

    def private_numbers(self) -> ec.EllipticCurvePrivateNumbers:  # pragma: no cover
        raise NodeCaError("la clave de la autoridad vive en KMS")

    def private_bytes(
        self,
        encoding: serialization.Encoding,
        format: serialization.PrivateFormat,
        encryption_algorithm: serialization.KeySerializationEncryption,
    ) -> bytes:  # pragma: no cover
        raise NodeCaError("la clave de la autoridad vive en KMS")

    def __copy__(self) -> _KmsBackedKey:  # pragma: no cover - lo exige la clase base
        return self

    def __deepcopy__(self, memo: object) -> _KmsBackedKey:  # pragma: no cover
        return self


async def kms_public_key(kms: NodeCaSigner, key_id: str) -> ec.EllipticCurvePublicKey:
    """La clave pública P-256 de ``key_id``; ``NodeCaError`` si la clave no es ECC P-256."""
    der = await kms.get_public_key(key_id)
    try:
        public = serialization.load_der_public_key(der)
    except ValueError:
        raise NodeCaError("KMS devolvió una clave pública que no se puede leer") from None
    if not isinstance(public, ec.EllipticCurvePublicKey) or not isinstance(
        public.curve, ec.SECP256R1
    ):
        raise NodeCaError("la clave de la autoridad debe ser ECC_NIST_P256 (SIGN_VERIFY)")
    return public


async def sign_certificate(
    builder: x509.CertificateBuilder,
    kms: NodeCaSigner,
    key_id: str,
    *,
    issuer_public_key: ec.EllipticCurvePublicKey | None = None,
) -> x509.Certificate:
    """Firma ``builder`` con ``kms:Sign`` sobre ``key_id`` (ECDSA P-256 con SHA-256).

    Comprueba la firma con la clave pública de ``key_id`` antes de devolver el certificado.
    """
    public = (
        issuer_public_key if issuer_public_key is not None else await kms_public_key(kms, key_id)
    )
    try:
        builder.sign(_KmsBackedKey(public), hashes.SHA256())
    except _TbsCaptured as captured:
        tbs = captured.tbs
    else:  # pragma: no cover - la primera pasada siempre se corta
        raise NodeCaError("no se pudo recoger el TBSCertificate")
    signature = await kms.sign(key_id, tbs)
    certificate = builder.sign(_KmsBackedKey(public, tbs=tbs, signature=signature), hashes.SHA256())
    try:
        public.verify(certificate.signature, tbs, ec.ECDSA(hashes.SHA256()))
    except Exception:
        raise NodeCaError(
            "la firma de KMS no verifica con la clave pública de la autoridad"
        ) from None
    return certificate


# --- Raíz ---------------------------------------------------------------------------------------


def _years_later(moment: datetime, years: int) -> datetime:
    try:
        return moment.replace(year=moment.year + years)
    except ValueError:  # 29 de febrero
        return moment.replace(year=moment.year + years, day=28)


def fingerprint(certificate: x509.Certificate) -> str:
    """SHA-256 en hexadecimal del certificado DER (identificador que se puede mostrar)."""
    return certificate.fingerprint(hashes.SHA256()).hex()


def certificate_pem(certificate: x509.Certificate) -> bytes:
    return certificate.public_bytes(serialization.Encoding.PEM)


async def build_root_certificate(
    kms: NodeCaSigner,
    key_id: str,
    *,
    now: datetime,
    environment: str,
    random_bytes: Callable[[int], bytes],
) -> x509.Certificate:
    """Raíz autofirmada de ``ROOT_VALIDITY_YEARS`` años con la clave de KMS ``key_id``."""
    if now.tzinfo is None:
        raise ValueError("now debe llevar zona horaria")
    public = await kms_public_key(kms, key_id)
    key_identifier = x509.SubjectKeyIdentifier.from_public_key(public)
    # El nombre lleva el identificador de la clave: dos raíces (D-6) nunca comparten nombre.
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, _ORGANIZATION),
            x509.NameAttribute(
                NameOID.COMMON_NAME,
                f"{_COMMON_NAME} {environment} {key_identifier.digest.hex()[:16]}",
            ),
        ]
    )
    raw_serial = random_bytes(_SERIAL_BYTES)
    if not isinstance(raw_serial, bytes) or len(raw_serial) != _SERIAL_BYTES:
        raise ValueError("el generador no devolvió 20 bytes para el número de serie")
    serial = (int.from_bytes(raw_serial, "big") >> 1) | 1  # positivo y < 2^159 (RFC 5280)
    not_before = now.astimezone(UTC).replace(microsecond=0)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(public)
        .serial_number(serial)
        .not_valid_before(not_before)
        .not_valid_after(_years_later(not_before, ROOT_VALIDITY_YEARS))
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
        .add_extension(key_identifier, critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(key_identifier),
            critical=False,
        )
    )
    return await sign_certificate(builder, kms, key_id, issuer_public_key=public)


# --- Paquete de raíces --------------------------------------------------------------------------


def read_bundle(data: bytes) -> tuple[x509.Certificate, ...]:
    """Las raíces de un ``root.pem`` publicado: de 1 a ``MAX_BUNDLE_ROOTS``, autofirmadas."""
    try:
        roots = tuple(x509.load_pem_x509_certificates(data))
    except ValueError:
        raise NodeCaError("ca/root.pem no es un paquete PEM de certificados") from None
    if not 1 <= len(roots) <= MAX_BUNDLE_ROOTS:
        raise NodeCaError(f"ca/root.pem debe tener de 1 a {MAX_BUNDLE_ROOTS} raíces")
    for root in roots:
        try:
            root.verify_directly_issued_by(root)
        except Exception:
            raise NodeCaError("ca/root.pem contiene un certificado que no es autofirmado") from None
    return roots


def two_root_bundle(current: bytes, new_root: x509.Certificate) -> bytes:
    """``root.pem`` con la raíz vigente y la nueva, en ese orden (D-6)."""
    roots = read_bundle(current)
    if len(roots) != 1:
        raise NodeCaError(
            "ya hay dos raíces publicadas: retira la antigua antes de otra sustitución"
        )
    (vigente,) = roots
    same_key = vigente.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    ) == new_root.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    if same_key:
        raise NodeCaError("la raíz nueva debe usar una clave KMS distinta de la vigente")
    return certificate_pem(vigente) + certificate_pem(new_root)


# --- Publicación --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreparedRoot:
    """La raíz nueva ya firmada y el paquete que se publicaría (nada escrito todavía): quien
    publica audita su intención con estos identificadores antes de ``publish``."""

    root: x509.Certificate
    bundle: tuple[x509.Certificate, ...]
    object_key: str

    @property
    def fingerprints(self) -> tuple[str, ...]:
        return tuple(fingerprint(root) for root in self.bundle)


@dataclass(frozen=True, slots=True)
class PublishedRoot:
    """Lo publicado: la raíz nueva, el paquete completo y la versión del objeto."""

    root: x509.Certificate
    bundle: tuple[x509.Certificate, ...]
    object_key: str
    version_id: str | None

    @property
    def fingerprints(self) -> tuple[str, ...]:
        return tuple(fingerprint(root) for root in self.bundle)

    @property
    def bundle_sha256(self) -> str:
        return hashlib.sha256(b"".join(certificate_pem(root) for root in self.bundle)).hexdigest()


class NodeCaPublisher:
    """Emite y publica ``ca/root.pem`` en el depósito ``vigia-edge``."""

    def __init__(
        self,
        *,
        storage: RootStorage,
        kms: NodeCaSigner,
        environment: str,
        random_bytes: Callable[[int], bytes],
        object_key: str = ROOT_CERTIFICATE_KEY,
    ) -> None:
        self._storage = storage
        self._kms = kms
        self._environment = environment
        self._random_bytes = random_bytes
        self._object_key = object_key

    def __repr__(self) -> str:
        return "NodeCaPublisher()"

    async def check_key(self, key_id: str) -> None:
        """Solo lectura (``--dry-run``): la clave existe y es ECC P-256."""
        await kms_public_key(self._kms, key_id)

    async def prepare_root(self, key_id: str, *, now: datetime) -> PreparedRoot:
        """``bootstrap``: la primera raíz, sola, firmada y sin publicar todavía."""
        root = await self._build(key_id, now)
        return PreparedRoot(root, (root,), self._object_key)

    async def prepare_rotation(self, new_key_id: str, *, now: datetime) -> PreparedRoot:
        """``rotate-node-ca``: la vigente (leída del depósito) y la nueva, sin publicar."""
        current = await self._storage.get_object(self._object_key)
        read_bundle(current)  # antes de firmar nada: un paquete inválido no se amplía
        root = await self._build(new_key_id, now)
        bundle = two_root_bundle(current, root)
        return PreparedRoot(root, read_bundle(bundle), self._object_key)

    async def publish(self, prepared: PreparedRoot) -> PublishedRoot:
        """Escribe ``ca/root.pem`` con el paquete preparado (la única escritura)."""
        if prepared.object_key != self._object_key:
            raise NodeCaError("el paquete se preparó para otro objeto")
        return await self._put(prepared.bundle, prepared.root)

    async def publish_root(self, key_id: str, *, now: datetime) -> PublishedRoot:
        return await self.publish(await self.prepare_root(key_id, now=now))

    async def rotate_root(self, new_key_id: str, *, now: datetime) -> PublishedRoot:
        return await self.publish(await self.prepare_rotation(new_key_id, now=now))

    async def _build(self, key_id: str, now: datetime) -> x509.Certificate:
        return await build_root_certificate(
            self._kms,
            key_id,
            now=now,
            environment=self._environment,
            random_bytes=self._random_bytes,
        )

    async def _put(
        self, bundle: Sequence[x509.Certificate], root: x509.Certificate
    ) -> PublishedRoot:
        body = b"".join(certificate_pem(certificate) for certificate in bundle)
        head = await self._storage.put_object(self._object_key, body, ROOT_CONTENT_TYPE)
        _log.info("raíz de la autoridad de nodos publicada", roots=len(bundle))
        return PublishedRoot(root, tuple(bundle), self._object_key, head.version_id)
