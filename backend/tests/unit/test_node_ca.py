"""Raíz de ``vigia-node-ca`` firmada con KMS y paquete de dos raíces (TASK-132, D-6).

``FakeKms`` hace de KMS con claves P-256 reales en memoria: la plataforma nunca recibe la clave
privada, solo la firma (``kms:Sign``) y la pública (``kms:GetPublicKey``). Contra LocalStack lo
repite ``tests/integration/test_admin_bootstrap.py``.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec

from tests.admin_support import (
    FakeKms,
    MemoryStorage,
    issue_node_certificate,
    public_der,
    validates,
)
from vigia_platform.shared.node_ca import (
    ROOT_CERTIFICATE_KEY,
    ROOT_VALIDITY_YEARS,
    NodeCaError,
    NodeCaPublisher,
    build_root_certificate,
    certificate_pem,
    read_bundle,
    two_root_bundle,
)

NOW = datetime(2026, 10, 2, 9, 30, 15, 123456, tzinfo=UTC)


def _root(kms: FakeKms, key_id: str, now: datetime = NOW) -> x509.Certificate:
    return asyncio.run(
        build_root_certificate(kms, key_id, now=now, environment="test", random_bytes=os.urandom)
    )


def test_root_is_self_signed_by_the_kms_key_for_ten_years() -> None:
    kms = FakeKms()
    kms.add("ca")
    root = _root(kms, "ca")
    root.verify_directly_issued_by(root)
    assert public_der(root.public_key()) == asyncio.run(kms.get_public_key("ca"))
    assert root.not_valid_before_utc == NOW.replace(microsecond=0)
    assert root.not_valid_after_utc == NOW.replace(microsecond=0, year=2036)
    assert ROOT_VALIDITY_YEARS == 10
    constraints = root.extensions.get_extension_for_class(x509.BasicConstraints)
    assert constraints.critical and constraints.value.ca and constraints.value.path_length == 0
    usage = root.extensions.get_extension_for_class(x509.KeyUsage)
    assert usage.critical and usage.value.key_cert_sign and usage.value.crl_sign
    assert not usage.value.digital_signature
    assert root.issuer == root.subject
    assert root.serial_number > 0 and root.serial_number.bit_length() <= 159
    assert kms.signed == ["ca"]  # una sola firma: la del TBSCertificate


def test_root_on_february_29_ends_on_february_28() -> None:
    kms = FakeKms()
    kms.add("ca")
    root = _root(kms, "ca", datetime(2028, 2, 29, 12, tzinfo=UTC))
    assert root.not_valid_after_utc == datetime(2038, 2, 28, 12, tzinfo=UTC)


def test_a_kms_signing_with_another_key_publishes_nothing() -> None:
    kms = FakeKms()
    kms.add("ca")
    kms.add("other")
    kms.sign_with["ca"] = "other"
    storage = MemoryStorage()
    publisher = NodeCaPublisher(
        storage=storage, kms=kms, environment="test", random_bytes=os.urandom
    )
    with pytest.raises(NodeCaError):
        asyncio.run(publisher.publish_root("ca", now=NOW))
    assert storage.puts == []


@pytest.mark.parametrize("curve", [ec.SECP384R1(), ec.SECP256K1()])
def test_only_p256_keys_can_be_the_authority(curve: ec.EllipticCurve) -> None:
    kms = FakeKms()
    kms.add("ca", curve)
    with pytest.raises(NodeCaError, match="ECC_NIST_P256"):
        _root(kms, "ca")


def test_publish_root_writes_only_the_root_object() -> None:
    kms = FakeKms()
    kms.add("ca")
    storage = MemoryStorage()
    publisher = NodeCaPublisher(
        storage=storage, kms=kms, environment="test", random_bytes=os.urandom
    )
    published = asyncio.run(publisher.publish_root("ca", now=NOW))
    assert storage.puts == [ROOT_CERTIFICATE_KEY]  # nunca ca/crl.pem (D-7: lo publica el worker)
    (root,) = read_bundle(storage.objects[ROOT_CERTIFICATE_KEY])
    assert root == published.root
    assert published.fingerprints == (published.fingerprints[0],)


def test_rotation_publishes_both_roots_and_old_nodes_keep_validating() -> None:
    kms = FakeKms()
    kms.add("ca")
    kms.add("ca-2")
    kms.add("intruder")
    storage = MemoryStorage()
    publisher = NodeCaPublisher(
        storage=storage, kms=kms, environment="test", random_bytes=os.urandom
    )
    old_root = asyncio.run(publisher.publish_root("ca", now=NOW)).root
    old_node = asyncio.run(issue_node_certificate(kms, "ca", old_root, NOW))
    # Sustitución con la credencial del nodo (365 días) aún vigente.
    later = NOW + timedelta(days=100)
    published = asyncio.run(publisher.rotate_root("ca-2", now=later))
    bundle = read_bundle(storage.objects[ROOT_CERTIFICATE_KEY])
    assert bundle == (old_root, published.root)
    new_node = asyncio.run(issue_node_certificate(kms, "ca-2", published.root, later))
    at = later + timedelta(days=1)
    assert validates(old_node, bundle, at)
    assert validates(new_node, bundle, at)
    # Sondas negativas: solo la raíz nueva no valida al nodo viejo; una raíz ajena, a ninguno.
    assert not validates(old_node, (published.root,), at)
    foreign = _root(kms, "intruder")
    assert not validates(new_node, (foreign,), at)


def test_a_third_root_is_refused_while_a_substitution_is_in_progress() -> None:
    kms = FakeKms()
    for key in ("ca", "ca-2", "ca-3"):
        kms.add(key)
    storage = MemoryStorage()
    publisher = NodeCaPublisher(
        storage=storage, kms=kms, environment="test", random_bytes=os.urandom
    )
    asyncio.run(publisher.publish_root("ca", now=NOW))
    asyncio.run(publisher.rotate_root("ca-2", now=NOW))
    before = storage.objects[ROOT_CERTIFICATE_KEY]
    with pytest.raises(NodeCaError, match="dos raíces"):
        asyncio.run(publisher.rotate_root("ca-3", now=NOW))
    assert storage.objects[ROOT_CERTIFICATE_KEY] == before
    assert len(storage.puts) == 2


def test_the_new_root_must_use_another_key() -> None:
    kms = FakeKms()
    kms.add("ca")
    current = _root(kms, "ca")
    with pytest.raises(NodeCaError, match="distinta"):
        two_root_bundle(certificate_pem(current), _root(kms, "ca"))


@pytest.mark.parametrize(
    "data",
    [b"", b"no es PEM", b"-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"],
)
def test_bundles_that_are_not_pem_certificates_are_rejected(data: bytes) -> None:
    with pytest.raises(NodeCaError):
        read_bundle(data)


def test_a_bundle_with_a_certificate_that_is_not_a_root_is_rejected() -> None:
    kms = FakeKms()
    kms.add("ca")
    root = _root(kms, "ca")
    node = asyncio.run(issue_node_certificate(kms, "ca", root, NOW))
    with pytest.raises(NodeCaError, match="autofirmado"):
        read_bundle(certificate_pem(node))
    with pytest.raises(NodeCaError, match="de 1 a 2"):
        read_bundle(certificate_pem(root) * 3)
