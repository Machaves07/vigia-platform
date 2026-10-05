"""Perfiles de los certificados del nodo y su emisión con ``vigia-node-ca`` (TASK-219; BR-GOB-63,
64; NFR-GOB-34; pendiente nº 29; PAT-GOB-SEG-05).

Con ``NodeCaIssuer`` real sobre un doble de KMS con una clave P-256 de prueba y el paquete
``ca/root.pem`` que construye ``shared.node_ca`` con ese doble:

- el certificado de cliente es P-256 con ECDSA-SHA256, 365 días, ``clientAuth``, no CA, sujeto
  ``{node_id, organization_id, plant_id}`` que lee ``node_api.identity`` y verifica contra la raíz
  del paquete (``verify_directly_issued_by``); el de servidor lleva ``serverAuth`` y **solo** la
  dirección anunciada;
- **propiedad** (PAT-GOB-SEG-05): con CSR que traen extensiones arbitrarias (CA, nombres
  alternativos, usos extendidos, políticas, usos de clave, restricciones de nombre) y un sujeto
  con organización y planta ajenas, el certificado emitido tiene exactamente las extensiones del
  perfil y el sujeto de la plataforma: **ninguna** de la CSR aparece;
- con un paquete de dos raíces (sustitución, D-6) emite con la raíz de la clave de KMS y entrega
  las dos; sin raíz de esa clave, con KMS que falla o que firma con otra clave, o sin respuesta en
  el plazo, ``NodeCaUnavailable`` sin certificado.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import os
import uuid
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, ExtensionOID, NameOID, SignatureAlgorithmOID
from hypothesis import given, settings
from hypothesis import strategies as st
from opentelemetry.sdk.metrics.export import HistogramDataPoint

from tests.dispatch_support import metrics_with_reader
from tests.fleet_credentials_support import (
    MemoryKms,
    RootStore,
    csr_pem,
    local_ip,
    new_key,
    root_bundle_for,
)
from vigia_platform.fleet.adapters.ca.certificate_profiles import (
    IssuedCertificates,
    NodeCaIssuer,
    NodeCaUnavailable,
)
from vigia_platform.fleet.adapters.ca.csr import (
    CLIENT_CSR_FIELD,
    SERVER_CSR_FIELD,
    LiveViewHost,
    announced_host,
    read_csr,
)
from vigia_platform.fleet.domain.node_subject import NodeSubject, read_subject, subject_name
from vigia_platform.shared.clock import SimulatedClock

NOW = dt.datetime(2026, 10, 5, 12, 30, 15, 250_000, tzinfo=dt.UTC)
SUBJECT = NodeSubject(
    node_id=uuid.UUID("0192f0c4-2222-7000-8000-000000000001"),
    organization_id=uuid.UUID("0192f0c4-2222-7000-8000-000000000002"),
    plant_id=uuid.UUID("0192f0c4-2222-7000-8000-000000000003"),
)
HOST: LiveViewHost = ipaddress.ip_address("192.168.10.20")
PROFILE_EXTENSIONS = {
    ExtensionOID.BASIC_CONSTRAINTS,
    ExtensionOID.KEY_USAGE,
    ExtensionOID.SUBJECT_KEY_IDENTIFIER,
    ExtensionOID.AUTHORITY_KEY_IDENTIFIER,
    ExtensionOID.EXTENDED_KEY_USAGE,
}


def _issuer(
    kms: MemoryKms, body: bytes | None, *, deadline: float = 60.0, metrics: Any = None
) -> NodeCaIssuer:
    return NodeCaIssuer(
        kms=kms,
        key_id=kms.key_id,
        roots=RootStore(body),
        clock=SimulatedClock(NOW),
        random_bytes=os.urandom,
        metrics=metrics,
        deadline_seconds=deadline,
    )


def _issue(
    issuer: NodeCaIssuer,
    client: ec.EllipticCurvePublicKey | None = None,
    server: ec.EllipticCurvePublicKey | None = None,
    host: LiveViewHost = HOST,
) -> IssuedCertificates:
    return asyncio.run(
        issuer.issue(
            SUBJECT,
            client if client is not None else new_key().public_key(),
            server if server is not None else new_key().public_key(),
            host,
            now=NOW,
        )
    )


@pytest.fixture(scope="module")
def authority() -> tuple[MemoryKms, bytes, x509.Certificate]:
    kms = MemoryKms()
    body, root = asyncio.run(root_bundle_for(kms, NOW))
    return kms, body, root


def test_the_client_certificate_has_the_profile(
    authority: tuple[MemoryKms, bytes, x509.Certificate],
) -> None:
    kms, body, root = authority
    client_key = new_key()
    issued = _issue(_issuer(kms, body), client=client_key.public_key())
    certificate = issued.client
    public = certificate.public_key()
    assert isinstance(public, ec.EllipticCurvePublicKey)
    assert isinstance(public.curve, ec.SECP256R1)
    assert public.public_numbers() == client_key.public_key().public_numbers()
    assert certificate.signature_algorithm_oid == SignatureAlgorithmOID.ECDSA_WITH_SHA256
    start = NOW.replace(microsecond=0)
    assert certificate.not_valid_before_utc == start
    assert certificate.not_valid_after_utc == start + dt.timedelta(days=365)
    assert certificate.subject == subject_name(SUBJECT)
    assert read_subject(certificate.subject) == SUBJECT
    assert certificate.issuer == root.subject
    certificate.verify_directly_issued_by(root)
    constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints)
    assert constraints.critical and constraints.value.ca is False
    usage = certificate.extensions.get_extension_for_class(x509.KeyUsage)
    assert usage.critical and usage.value.digital_signature and not usage.value.key_cert_sign
    eku = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(eku) == [ExtendedKeyUsageOID.CLIENT_AUTH]
    authority_key = certificate.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)
    root_key = root.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
    assert authority_key.value.key_identifier == root_key.value.digest
    assert {extension.oid for extension in certificate.extensions} == PROFILE_EXTENSIONS
    assert 0 < certificate.serial_number < 2**159
    assert issued.ca_chain == body.decode("ascii")


def test_the_server_certificate_has_server_auth_and_only_the_announced_address(
    authority: tuple[MemoryKms, bytes, x509.Certificate],
) -> None:
    kms, body, root = authority
    for host in (HOST, "nodo-prensa.local"):
        issued = _issue(_issuer(kms, body), host=host)
        server = issued.server
        server.verify_directly_issued_by(root)
        eku = server.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        assert list(eku) == [ExtendedKeyUsageOID.SERVER_AUTH]
        names = list(server.extensions.get_extension_for_class(x509.SubjectAlternativeName).value)
        expected = x509.IPAddress(host) if not isinstance(host, str) else x509.DNSName(host)
        assert names == [expected]
        assert server.serial_number != issued.client.serial_number
        constraints = server.extensions.get_extension_for_class(x509.BasicConstraints)
        assert constraints.value.ca is False
        assert {e.oid for e in server.extensions} == PROFILE_EXTENSIONS | {
            ExtensionOID.SUBJECT_ALTERNATIVE_NAME
        }


# --- Ninguna extensión de la CSR pasa al certificado -------------------------------------------

_CSR_EXTENSIONS: list[tuple[x509.ExtensionType, bool]] = [
    (x509.BasicConstraints(ca=True, path_length=None), True),
    (x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING, ExtendedKeyUsageOID.SERVER_AUTH,
                            ExtendedKeyUsageOID.ANY_EXTENDED_KEY_USAGE]), False),
    (x509.CertificatePolicies([x509.PolicyInformation(x509.ObjectIdentifier("2.5.29.32.0"),
                                                      None)]), False),
    (x509.KeyUsage(digital_signature=True, content_commitment=True, key_encipherment=True,
                   data_encipherment=True, key_agreement=True, key_cert_sign=True, crl_sign=True,
                   encipher_only=False, decipher_only=False), True),
    (x509.NameConstraints(permitted_subtrees=[x509.DNSName("example.com")],
                          excluded_subtrees=None), True),
    (x509.OCSPNoCheck(), False),
    (x509.TLSFeature([x509.TLSFeatureType.status_request]), False),
]  # fmt: skip
_FOREIGN_NAMES: list[x509.GeneralName] = [
    x509.DNSName("vigia.example.com"),
    x509.IPAddress(ipaddress.ip_address("8.8.8.8")),
    x509.RFC822Name("persona@example.com"),
    x509.UniformResourceIdentifier("https://evil.example/"),
]


@settings(max_examples=25, deadline=None)
@given(
    extensions=st.lists(st.sampled_from(_CSR_EXTENSIONS), unique_by=lambda e: type(e[0])),
    names=st.lists(st.sampled_from(_FOREIGN_NAMES), unique=True, max_size=3),
    foreign_subject=st.booleans(),
)
def test_no_csr_extension_or_subject_reaches_the_issued_certificates(
    authority: tuple[MemoryKms, bytes, x509.Certificate],
    extensions: list[tuple[x509.ExtensionType, bool]],
    names: list[x509.GeneralName],
    foreign_subject: bool,
) -> None:
    kms, body, _ = authority
    extra = (
        (
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, str(uuid.uuid4())),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, str(uuid.uuid4())),
            x509.NameAttribute(NameOID.EMAIL_ADDRESS, "persona@example.com"),
        )
        if foreign_subject
        else ()
    )
    client_pem = csr_pem(str(SUBJECT.node_id), names=names, extensions=extensions,
                         extra_subject=extra)  # fmt: skip
    server_pem = csr_pem(
        str(SUBJECT.node_id),
        names=[local_ip("192.168.10.20")],
        extensions=extensions,
        extra_subject=extra,
    )
    client = read_csr(client_pem, field=CLIENT_CSR_FIELD)
    server = read_csr(server_pem, field=SERVER_CSR_FIELD)
    issued = _issue(
        _issuer(kms, body),
        client=client.public_key,
        server=server.public_key,
        host=announced_host(server, None),
    )
    for certificate, eku in (
        (issued.client, ExtendedKeyUsageOID.CLIENT_AUTH),
        (issued.server, ExtendedKeyUsageOID.SERVER_AUTH),
    ):
        assert certificate.subject == subject_name(SUBJECT)
        oids = {extension.oid for extension in certificate.extensions}
        assert oids <= PROFILE_EXTENSIONS | {ExtensionOID.SUBJECT_ALTERNATIVE_NAME}
        assert ExtensionOID.CERTIFICATE_POLICIES not in oids
        assert ExtensionOID.NAME_CONSTRAINTS not in oids
        assert (
            certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
        )
        assert list(
            certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        ) == [eku]
        usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
        assert (usage.digital_signature, usage.key_cert_sign, usage.crl_sign) == (
            True,
            False,
            False,
        )
    assert ExtensionOID.SUBJECT_ALTERNATIVE_NAME not in {e.oid for e in issued.client.extensions}
    server_names = issued.server.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    assert list(server_names.value) == [x509.IPAddress(ipaddress.ip_address("192.168.10.20"))]


# --- Paquete de raíces y fallo cerrado ---------------------------------------------------------


def test_with_two_published_roots_the_one_of_the_kms_key_issues() -> None:
    kms = MemoryKms()
    body, root = asyncio.run(root_bundle_for(kms, NOW, second_foreign_root=True))
    issued = _issue(_issuer(kms, body))
    assert issued.root == root
    issued.client.verify_directly_issued_by(root)
    assert len(x509.load_pem_x509_certificates(issued.ca_chain.encode())) == 2


@pytest.mark.parametrize("case", ["foreign_bundle", "no_bundle", "kms_fails", "other_key"])
def test_an_unusable_authority_issues_nothing(case: str) -> None:
    kms = MemoryKms()
    body, _ = asyncio.run(root_bundle_for(kms, NOW))
    if case == "foreign_bundle":
        body, _ = asyncio.run(root_bundle_for(MemoryKms(), NOW))
    elif case == "no_bundle":
        body = None  # type: ignore[assignment]
    elif case == "kms_fails":
        kms.fail = True
    else:
        # KMS firma con otra clave que la publicada: la comprobación de la firma lo detiene.
        issuer = _issuer(kms, body)
        asyncio.run(issuer.issue(SUBJECT, new_key().public_key(), new_key().public_key(), HOST,
                                 now=NOW))  # fmt: skip
        kms.key = new_key()
        with pytest.raises(NodeCaUnavailable):
            _issue(issuer)
        return
    with pytest.raises(NodeCaUnavailable):
        _issue(_issuer(kms, body))


def test_a_hung_kms_ends_in_unavailable_at_the_deadline() -> None:
    kms = MemoryKms()
    body, _ = asyncio.run(root_bundle_for(kms, NOW))
    kms.hang = True
    with pytest.raises(NodeCaUnavailable):
        _issue(_issuer(kms, body, deadline=1.0))


def test_each_kms_sign_is_measured_by_result() -> None:
    metrics, reader = metrics_with_reader()
    kms = MemoryKms()
    body, _ = asyncio.run(root_bundle_for(kms, NOW))
    _issue(_issuer(kms, body, metrics=metrics))
    counts: dict[str, int] = {}
    data = reader.get_metrics_data()
    assert data is not None
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name != "node_ca_sign_duration_ms":
                    continue
                for point in metric.data.data_points:
                    assert isinstance(point, HistogramDataPoint)
                    counts[str(dict(point.attributes or {})["result"])] = point.count
    assert counts == {"ok": 2}
