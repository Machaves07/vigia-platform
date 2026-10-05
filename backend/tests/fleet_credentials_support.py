"""Dobles y constructores de las pruebas de credenciales del nodo (TASK-219).

- ``csr_pem``: una CSR PKCS #10 en PEM generada aquí (clave EC P-256 nueva por omisión) con el
  nombre común, la curva, el algoritmo, los nombres alternativos y las extensiones que la prueba
  pida; ``tampered`` rompe la autofirma.
- ``MemoryKms``: ``NodeCaSigner`` en memoria con una clave ECDSA P-256 **de prueba** (nunca la de
  KMS), que cuenta sus firmas y puede **colgarse** (``hang``: espera para siempre, como un KMS sin
  respuesta) o fallar (``fail``).
- ``root_bundle_for``: el paquete ``ca/root.pem`` con la raíz de esa clave (la construye
  ``shared.node_ca.build_root_certificate`` sobre el doble), opcionalmente con una segunda raíz
  ajena (sustitución en curso, D-6).

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

from tests.fleet_support import root_bundle
from vigia_platform.shared.node_ca import build_root_certificate, certificate_pem

__all__ = [
    "KEY_ID",
    "MemoryKms",
    "RootStore",
    "csr_pem",
    "new_key",
    "root_bundle_for",
]

KEY_ID = "alias/vigia-node-ca-test"
"""Identificador de la clave del doble (nunca un ARN real, A-47)."""


def new_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


_ECDSA_WITH_SHA1: bytes = bytes.fromhex("300906072a8648ce3d0401")
"""``AlgorithmIdentifier`` de ``ecdsa-with-SHA1`` (1.2.840.10045.4.1), sin parámetros."""


def _tlv(data: bytes, offset: int) -> tuple[int, int]:
    """Inicio del valor y fin del elemento DER que empieza en ``offset``."""
    length = data[offset + 1]
    start = offset + 2
    if length & 0x80:
        size = length & 0x7F
        length = int.from_bytes(data[start : start + size], "big")
        start += size
    return start, start + length


def _der(tag: int, value: bytes) -> bytes:
    size = len(value)
    if size < 0x80:
        return bytes([tag, size]) + value
    raw = size.to_bytes((size.bit_length() + 7) // 8, "big")
    return bytes([tag, 0x80 | len(raw)]) + raw + value


def _sha1_csr(der: bytes, key: ec.EllipticCurvePrivateKey) -> bytes:
    """La misma solicitud firmada con ECDSA-SHA1 (``cryptography`` ya no la construye)."""
    outer_start, _ = _tlv(der, 0)
    _, info_end = _tlv(der, outer_start)
    info = der[outer_start:info_end]
    signature = key.sign(info, ec.ECDSA(hashes.SHA1()))  # noqa: S303 - la forma que se rechaza
    body = info + _ECDSA_WITH_SHA1 + _der(0x03, b"\x00" + signature)
    return _der(0x30, body)


def csr_pem(
    common_name: str | None,
    *,
    key: Any = None,
    algorithm: hashes.HashAlgorithm | None = None,
    names: Sequence[x509.GeneralName] = (),
    extensions: Sequence[tuple[x509.ExtensionType, bool]] = (),
    extra_subject: Sequence[x509.NameAttribute] = (),
    tampered: bool = False,
) -> str:
    """Una CSR en PEM; ``common_name=None`` la deja sin nombre común."""
    private = key if key is not None else new_key()
    if isinstance(algorithm, hashes.SHA1):
        signed = csr_pem(common_name, key=private, names=names, extensions=extensions,
                         extra_subject=extra_subject)  # fmt: skip
        der = x509.load_pem_x509_csr(signed.encode()).public_bytes(serialization.Encoding.DER)
        return (
            x509.load_der_x509_csr(_sha1_csr(der, private))
            .public_bytes(serialization.Encoding.PEM)
            .decode("ascii")
        )
    attributes = list(extra_subject)
    if common_name is not None:
        attributes.append(x509.NameAttribute(NameOID.COMMON_NAME, common_name))
    builder = x509.CertificateSigningRequestBuilder().subject_name(x509.Name(attributes))
    if names:
        builder = builder.add_extension(x509.SubjectAlternativeName(list(names)), critical=False)
    for extension, critical in extensions:
        builder = builder.add_extension(extension, critical=critical)
    csr = builder.sign(private, algorithm if algorithm is not None else hashes.SHA256())
    der = bytearray(csr.public_bytes(serialization.Encoding.DER))
    if tampered:
        der[-3] ^= 0x01  # un bit de la firma: la autofirma deja de verificar
        return x509.load_der_x509_csr(bytes(der)).public_bytes(serialization.Encoding.PEM).decode()
    return csr.public_bytes(serialization.Encoding.PEM).decode("ascii")


def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65_537, key_size=2048)


def local_ip(text: str = "192.168.10.20") -> x509.GeneralName:
    return x509.IPAddress(ipaddress.ip_address(text))


@dataclass
class MemoryKms:
    """``NodeCaSigner`` en memoria con una clave P-256 de prueba."""

    key: ec.EllipticCurvePrivateKey = field(default_factory=new_key)
    key_id: str = KEY_ID
    signs: int = 0
    public_reads: int = 0
    hang: bool = False
    fail: bool = False

    async def get_public_key(self, key_id: str) -> bytes:
        self.public_reads += 1
        if self.hang:
            await asyncio.Event().wait()
        if self.fail or key_id != self.key_id:
            raise OSError("KMS no responde")
        return self.key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    async def sign(self, key_id: str, message: bytes) -> bytes:
        self.signs += 1
        if self.hang:
            await asyncio.Event().wait()
        if self.fail or key_id != self.key_id:
            raise OSError("KMS no responde")
        return self.key.sign(message, ec.ECDSA(hashes.SHA256()))


async def root_bundle_for(
    kms: MemoryKms, now: dt.datetime, *, second_foreign_root: bool = False
) -> tuple[bytes, x509.Certificate]:
    """``ca/root.pem`` con la raíz de ``kms`` (y otra ajena delante, si se pide) y esa raíz."""
    root = await build_root_certificate(
        kms, kms.key_id, now=now - dt.timedelta(days=1), environment="test", random_bytes=os.urandom
    )
    body = certificate_pem(root)
    if second_foreign_root:
        body = root_bundle(1) + body
    kms.signs = 0
    kms.public_reads = 0
    return body, root


@dataclass
class RootStore:
    """``vigia-edge`` en memoria: solo ``ca/root.pem``."""

    body: bytes | None
    reads: int = 0

    async def get_object(self, key: str, *, version_id: str | None = None) -> bytes:
        self.reads += 1
        if self.body is None or key != "ca/root.pem":
            raise OSError("el depósito no responde")
        return self.body


def node_name(node_id: uuid.UUID) -> str:
    return str(node_id)
