"""PR-NUC-57: las implementaciones puras del verificador frente a ``cryptography`` y ``rfc8785``.

- **Ed25519** (``pure_ed25519.ed25519_verify``): para claves, mensajes y firmas generados, con y
  sin un bit alterado en la firma, el mensaje o la clave, y para firmas y claves arbitrarias de
  32 y 64 bytes, la verificación pura coincide con la de ``cryptography`` (OpenSSL). Bordes
  explícitos: ``S`` igual al orden y mayor, ``R`` no canónica, clave neutra con y sin bit de
  signo, ``y`` sin reducir, puntos que no están en la curva, longitudes erróneas y los vectores
  de RFC 8032 §7.1.
- **RFC 8785** (``pure_rfc8785.canonicalize``): para documentos JSON generados, incluidos dobles
  arbitrarios, enteros fuera del rango seguro, ``NaN``, infinitos, sustitutos sueltos, claves
  fuera del plano básico y tipos que no son JSON, la canonicalización pura coincide byte a byte
  con ``rfc8785`` o ambas rechazan el documento; la pura rechaza siempre con
  ``CanonicalizationError``. La lectura del verificador (``parse_json``) devuelve un documento con
  la misma forma canónica (ida y vuelta).

Solo datos generados.
"""

from __future__ import annotations

import math
import struct
from typing import Any

import pytest
import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from hypothesis import example, given
from hypothesis import strategies as st

from tests.properties.envelope_strategies import texts
from vigia_platform.ledger.chain import pure_ed25519, pure_rfc8785
from vigia_platform.ledger.chain.package_verifier import parse_json
from vigia_platform.ledger.chain.pure_ed25519 import ed25519_verify
from vigia_platform.ledger.chain.pure_rfc8785 import CanonicalizationError, canonicalize

P = 2**255 - 19
L = 2**252 + 27742317777372353535851937790883648493

# --- Ed25519 --------------------------------------------------------------------------------------


def library_verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """La verificación de la plataforma (``cryptography``); una clave ilegible no verifica."""
    try:
        key = Ed25519PublicKey.from_public_bytes(public_key)
        key.verify(signature, message)
    except (InvalidSignature, ValueError):
        return False
    return True


def flip(data: bytes, bit: int) -> bytes:
    """``data`` con el bit ``bit`` (módulo su longitud en bits) invertido."""
    position = bit % (8 * len(data))
    altered = bytearray(data)
    altered[position // 8] ^= 1 << position % 8
    return bytes(altered)


@given(
    seed=st.binary(min_size=32, max_size=32),
    message=st.binary(max_size=256),
    target=st.sampled_from(["none", "signature", "message", "public_key"]),
    bit=st.integers(min_value=0, max_value=511),
)
def test_ed25519_matches_cryptography(seed: bytes, message: bytes, target: str, bit: int) -> None:
    private = Ed25519PrivateKey.from_private_bytes(seed)
    public_key = private.public_key().public_bytes_raw()
    signature = private.sign(message)
    if target == "signature":
        signature = flip(signature, bit)
    elif target == "public_key":
        public_key = flip(public_key, bit)
    elif target == "message":
        message = flip(message, bit) if message else b"\x00"
    expected = library_verify(public_key, message, signature)
    assert ed25519_verify(public_key, message, signature) is expected
    if target == "none":
        assert expected is True
    else:
        assert expected is False


@given(
    public_key=st.binary(min_size=32, max_size=32),
    message=st.binary(max_size=64),
    signature=st.binary(min_size=64, max_size=64),
)
def test_ed25519_arbitrary_bytes_match(public_key: bytes, message: bytes, signature: bytes) -> None:
    assert ed25519_verify(public_key, message, signature) is library_verify(
        public_key, message, signature
    )


def _encode_point(scalar: int) -> bytes:
    """Codificación de ``[scalar]B`` con la aritmética del propio módulo (solo para los bordes)."""
    base = pure_ed25519._BASE
    assert base is not None
    return pure_ed25519._encode(pure_ed25519._double_scalar_mult(scalar, base, 0, base))


_IDENTITY = (1).to_bytes(32, "little")
_IDENTITY_SIGNED = flip(_IDENTITY, 255)
_IDENTITY_UNREDUCED = (P + 1).to_bytes(32, "little")
_MINUS_ONE = (P - 1).to_bytes(32, "little")  # y = -1: punto de orden 2
_S = 123456789


@pytest.mark.parametrize(
    ("public_key", "signature"),
    [
        # Con la clave neutra, [S]B - [k]A = [S]B para todo mensaje: la firma vale en OpenSSL.
        (_IDENTITY, _encode_point(_S) + _S.to_bytes(32, "little")),
        (_IDENTITY_SIGNED, _encode_point(_S) + _S.to_bytes(32, "little")),
        (_IDENTITY_UNREDUCED, _encode_point(_S) + _S.to_bytes(32, "little")),
        # S igual al orden y mayor: se rechaza aunque [S]B coincida.
        (_IDENTITY, _encode_point(_S) + (_S + L).to_bytes(32, "little")),
        (_IDENTITY, _encode_point(0) + L.to_bytes(32, "little")),
        (_IDENTITY, _encode_point(L - 1) + (L - 1).to_bytes(32, "little")),
        # R no canónica (y = p + 1 para el neutro) y R neutra con S = 0.
        (_IDENTITY, _IDENTITY_UNREDUCED + bytes(32)),
        (_IDENTITY, _IDENTITY + bytes(32)),
        (_MINUS_ONE, _IDENTITY + bytes(32)),
        # y = 2 no está en la curva.
        ((2).to_bytes(32, "little"), _encode_point(_S) + _S.to_bytes(32, "little")),
        (bytes(32), bytes(64)),
        (b"\xff" * 32, b"\xff" * 64),
    ],
)
@pytest.mark.parametrize("message", [b"", b"m", b"vigia" * 40])
def test_ed25519_edges_match(public_key: bytes, signature: bytes, message: bytes) -> None:
    assert ed25519_verify(public_key, message, signature) is library_verify(
        public_key, message, signature
    )


@pytest.mark.parametrize(
    ("key_size", "signature_size"), [(31, 64), (33, 64), (32, 63), (32, 65), (0, 0)]
)
def test_ed25519_wrong_lengths_do_not_verify(key_size: int, signature_size: int) -> None:
    private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    public_key = private.public_key().public_bytes_raw()
    signature = private.sign(b"m")
    key = (public_key * 2)[:key_size]
    assert ed25519_verify(key, b"m", (signature * 2)[:signature_size]) is False


# RFC 8032 §7.1, TEST 1 a 3: clave secreta, mensaje y firma esperada.
RFC8032_VECTORS = [
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
        "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
        "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
    (
        "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "af82",
        "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
        "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
    ),
]


@pytest.mark.parametrize(("secret", "message", "signature"), RFC8032_VECTORS)
def test_ed25519_rfc8032_vectors(secret: str, message: str, signature: str) -> None:
    private = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(secret))
    public_key = private.public_key().public_bytes_raw()
    assert private.sign(bytes.fromhex(message)).hex() == signature  # el vector es el de la RFC
    assert ed25519_verify(public_key, bytes.fromhex(message), bytes.fromhex(signature)) is True
    assert (
        ed25519_verify(public_key, bytes.fromhex(message) + b"x", bytes.fromhex(signature)) is False
    )


# --- RFC 8785 -------------------------------------------------------------------------------------

_any_text = st.text(alphabet=st.characters(), max_size=24)
"""Texto con cualquier punto de código, sustitutos sueltos incluidos."""

_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**53) - 3, max_value=2**53 + 3),
    st.integers(),
    st.floats(allow_nan=True, allow_infinity=True),
    texts(max_size=24),
    _any_text,
)
json_documents = st.recursive(
    _scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=5),
        st.lists(children, max_size=3).map(tuple),
        st.dictionaries(st.one_of(texts(max_size=10), _any_text), children, max_size=5),
    ),
    max_leaves=25,
)


def library_canonical(document: Any) -> bytes | None:
    """``rfc8785.dumps`` o ``None`` si lo rechaza (con cualquier excepción)."""
    try:
        return rfc8785.dumps(document)
    except Exception:  # el oráculo rechaza con varios tipos; aquí basta el rechazo
        return None


def pure_canonical(document: Any) -> bytes | None:
    try:
        return canonicalize(document)
    except CanonicalizationError:
        return None


@given(json_documents)
@example({"\U0001f600": 1, "\ufb01": 2, "é": 3, "a": 4})  # orden UTF-16, no por punto de código
@example(['\u2028\u2029\x7f\x00\x1f"\\/'])
@example([1e21, 1e20, 1e-7, 1e-6, 5e-324, -0.0, 2**53 - 1, -(2**53 - 1)])
@example({"x": "\udc00"})
@example({"\ud800": 1})
def test_rfc8785_matches_library(document: Any) -> None:
    assert pure_canonical(document) == library_canonical(document)


@given(st.floats(allow_nan=False, allow_infinity=False))
@example(1e21)
@example(9.999999999999999e20)
@example(1e-6)
@example(9.999999999999999e-7)
@example(1.7976931348623157e308)
@example(2.2250738585072014e-308)
def test_rfc8785_numbers_match_library(value: float) -> None:
    assert canonicalize(value) == rfc8785.dumps(value)
    assert pure_rfc8785.format_number(value) == rfc8785.dumps(value).decode()


@given(st.binary(min_size=8, max_size=8))
def test_rfc8785_any_double_bits_match_library(data: bytes) -> None:
    (value,) = struct.unpack("<d", data)
    if not math.isfinite(value):
        with pytest.raises(CanonicalizationError):
            canonicalize(value)
        return
    assert canonicalize(value) == rfc8785.dumps(value)


@pytest.mark.parametrize(
    "document",
    [
        float("nan"),
        float("inf"),
        -float("inf"),
        2**53,
        -(2**53),
        10**400,
        {1: "a"},
        {("a",): 1},
        {"a": {b"x"}},
        b"bytes",
        object(),
        "\ud800",
        ["\udfff"],
    ],
    ids=repr,
)
def test_rfc8785_rejections_are_contract_errors(document: Any) -> None:
    assert library_canonical(document) is None
    with pytest.raises(CanonicalizationError):
        canonicalize(document)


def test_rfc8785_deep_nesting_is_a_contract_error() -> None:
    document: Any = 1
    for _ in range(100_000):
        document = [document]
    with pytest.raises(CanonicalizationError):
        canonicalize(document)


@given(json_documents)
def test_parse_json_round_trip(document: Any) -> None:
    data = pure_canonical(document)
    if data is None:
        return
    assert canonicalize(parse_json(data)) == data


@pytest.mark.parametrize(
    "data",
    [
        b"NaN",
        b"[Infinity]",
        b"-Infinity",
        b"1e400",
        b'{"a":1,"a":2}',
        b"\xff",
        b"[" * 100_000,
        b"1" * 5000,
    ],
    ids=[
        "nan",
        "infinity",
        "minus-infinity",
        "overflow",
        "duplicate-key",
        "utf8",
        "deep",
        "digits",
    ],
)
def test_parse_json_rejects_what_rfc8785_cannot_write(data: bytes) -> None:
    with pytest.raises(ValueError):
        parse_json(data)


def test_parse_json_reads_large_integers_as_doubles() -> None:
    data = canonicalize(1.2345678901234567e19)
    assert data == b"12345678901234567000"
    assert canonicalize(parse_json(data)) == data
