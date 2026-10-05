"""Validación de la CSR del nodo (TASK-219; BR-GOB-63; LC-GOB-11; tech-stack §2.2).

``read_csr`` acepta solo PKCS #10 de 1 a 8 192 caracteres, ECDSA con SHA-256 sobre una clave EC
P-256, con la autofirma válida y un único nombre común que es un UUID canónico. Cada otra forma es
``CsrRejected`` con el campo que la trajo (la ruta responde ``schema_invalid`` con ese ``field``).
``announced_host`` elige la dirección del certificado de servidor: la de ``live_view_local_url`` si
se conoce, si no el único nombre alternativo local de la CSR; nunca una dirección pública.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import ipaddress
import uuid

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from hypothesis import given
from hypothesis import strategies as st

from tests.fleet_credentials_support import csr_pem, local_ip, rsa_key
from vigia_platform.fleet.adapters.ca.csr import (
    CLIENT_CSR_FIELD,
    MAX_CSR_PEM_CHARS,
    SERVER_CSR_FIELD,
    CsrRejected,
    announced_host,
    read_csr,
)

NODE = uuid.UUID("0192f0c4-1111-7000-8000-000000000001")


def _rejected(pem: object, field: str = CLIENT_CSR_FIELD) -> CsrRejected:
    with pytest.raises(CsrRejected) as raised:
        read_csr(pem, field=field)
    assert raised.value.field == field
    return raised.value


def test_a_p256_sha256_csr_with_the_node_id_is_accepted() -> None:
    read = read_csr(csr_pem(str(NODE)), field=CLIENT_CSR_FIELD)
    assert read.node_id == NODE
    assert isinstance(read.public_key.curve, ec.SECP256R1)


def test_an_rsa_key_is_rejected() -> None:
    _rejected(csr_pem(str(NODE), key=rsa_key()))


@pytest.mark.parametrize("curve", [ec.SECP384R1(), ec.SECP521R1(), ec.SECP256K1()])
def test_a_curve_other_than_p256_is_rejected(curve: ec.EllipticCurve) -> None:
    _rejected(csr_pem(str(NODE), key=ec.generate_private_key(curve)))


@pytest.mark.parametrize(
    "algorithm",
    [hashes.SHA1(), hashes.SHA384(), hashes.SHA512()],  # noqa: S303 - las formas que se rechazan
)
def test_a_hash_other_than_sha256_is_rejected(algorithm: hashes.HashAlgorithm) -> None:
    _rejected(csr_pem(str(NODE), algorithm=algorithm))


def test_an_invalid_self_signature_is_rejected() -> None:
    _rejected(csr_pem(str(NODE), tampered=True))


def test_the_pem_limit_is_8192_characters() -> None:
    pem = csr_pem(str(NODE))
    # Relleno de saltos de línea al final: sigue siendo la misma CSR válida.
    at_limit = pem + "\n" * (MAX_CSR_PEM_CHARS - len(pem))
    assert len(at_limit) == MAX_CSR_PEM_CHARS
    assert read_csr(at_limit, field=CLIENT_CSR_FIELD).node_id == NODE
    _rejected(at_limit + "\n")  # 8 193 caracteres


@pytest.mark.parametrize(
    "common_name",
    [
        None,
        "nodo-de-prueba",
        str(NODE).upper(),
        "{" + str(NODE) + "}",
        str(NODE) + " ",
        "0192f0c4111170008000000000000001",
    ],
)
def test_the_common_name_must_be_one_canonical_uuid(common_name: str | None) -> None:
    _rejected(csr_pem(common_name))


def test_two_common_names_are_rejected() -> None:
    extra = (x509.NameAttribute(NameOID.COMMON_NAME, str(uuid.uuid4())),)
    _rejected(csr_pem(str(NODE), extra_subject=extra))


@pytest.mark.parametrize(
    "value",
    [
        None,
        b"",
        "",
        "-----BEGIN CERTIFICATE REQUEST-----\nAAAA\n-----END CERTIFICATE REQUEST-----\n",
        "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n",
        "ñ" * 10,
        12345,
    ],
)
def test_anything_that_is_not_a_csr_is_rejected(value: object) -> None:
    _rejected(value, SERVER_CSR_FIELD)


@given(st.binary(max_size=600))
def test_arbitrary_pem_bodies_never_escape_as_other_errors(body: bytes) -> None:
    import base64

    pem = (
        "-----BEGIN CERTIFICATE REQUEST-----\n"
        + base64.encodebytes(body).decode("ascii")
        + "-----END CERTIFICATE REQUEST-----\n"
    )
    with pytest.raises(CsrRejected):
        read_csr(pem, field=CLIENT_CSR_FIELD)


# --- Dirección de la vista en vivo --------------------------------------------------------------


def _server(*names: x509.GeneralName) -> object:
    return read_csr(csr_pem(str(NODE), names=names), field=SERVER_CSR_FIELD)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (local_ip("192.168.10.20"), ipaddress.ip_address("192.168.10.20")),
        (local_ip("10.1.2.3"), ipaddress.ip_address("10.1.2.3")),
        (local_ip("fd00::1"), ipaddress.ip_address("fd00::1")),
        (x509.DNSName("nodo-prensa.local"), "nodo-prensa.local"),
        (x509.DNSName("nodo1.planta.internal"), "nodo1.planta.internal"),
        (x509.DNSName("nodo1"), "nodo1"),
    ],
)
def test_a_single_local_announced_name_is_the_host(
    name: x509.GeneralName, expected: object
) -> None:
    assert announced_host(_server(name), None) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "names",
    [
        (),
        (local_ip("8.8.8.8"),),
        (local_ip("127.0.0.1"),),
        (local_ip("0.0.0.0"),),  # noqa: S104 - es el valor que se rechaza
        (x509.DNSName("vigia.example.com"),),
        (x509.DNSName("NODO.local"),),
        (x509.DNSName("1234"),),
        (local_ip("192.168.10.20"), x509.DNSName("nodo.local")),
        (x509.RFC822Name("persona@example.com"),),
        (x509.UniformResourceIdentifier("https://192.168.1.2:8443/"),),
    ],
)
def test_public_missing_or_several_names_are_rejected(names: tuple[x509.GeneralName, ...]) -> None:
    with pytest.raises(CsrRejected) as raised:
        announced_host(_server(*names), None)  # type: ignore[arg-type]
    assert raised.value.field == SERVER_CSR_FIELD


def test_a_known_live_view_url_wins_over_the_csr() -> None:
    server = _server(local_ip("192.168.10.20"))
    host = announced_host(server, "https://10.20.30.40:8443/")  # type: ignore[arg-type]
    assert host == ipaddress.ip_address("10.20.30.40")
    assert announced_host(server, "https://camara-norte.local:8443/") == "camara-norte.local"  # type: ignore[arg-type]
    with pytest.raises(CsrRejected):
        announced_host(server, "https://8.8.4.4:8443/")  # type: ignore[arg-type]
