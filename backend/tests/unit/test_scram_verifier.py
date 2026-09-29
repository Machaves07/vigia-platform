"""Verificador SCRAM-SHA-256 de las contraseñas de rol (TASK-106, NFR-NUC-20, PAT-NUC-SEG-05).

El verificador que envía ``nuc_0001`` debe ser exactamente el que PostgreSQL guardaría para esa
contraseña: se comprueba contra el intercambio de ejemplo de RFC 7677 §3 (el servidor valida la
prueba del cliente con ``StoredKey`` y firma con ``ServerKey``). La prueba de extremo a extremo
(iniciar sesión como ``vigia_app``) está en ``tests/integration/test_roles.py``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vigia_platform.shared.role_passwords import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    RolePasswordError,
    role_password_verifier,
    scram_sha256_verifier,
    validate_role_password,
)

VERIFIER = re.compile(
    r"^SCRAM-SHA-256\$(?P<iterations>[0-9]+):(?P<salt>[A-Za-z0-9+/=]+)"
    r"\$(?P<stored>[A-Za-z0-9+/=]+):(?P<server>[A-Za-z0-9+/=]+)$"
)

# RFC 7677 §3: usuario "user", contraseña "pencil".
RFC_SALT = base64.b64decode("W22ZaJ0SNY7soEsUEjb6gQ==")
RFC_AUTH_MESSAGE = (
    b"n=user,r=rOprNGfwEbeRWgbNEkqO,"
    b"r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0,s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096,"
    b"c=biws,r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0"
)
RFC_CLIENT_PROOF = base64.b64decode("dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ=")
RFC_SERVER_SIGNATURE = base64.b64decode("6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4=")


def _parts(verifier: str) -> tuple[int, bytes, bytes, bytes]:
    match = VERIFIER.fullmatch(verifier)
    assert match is not None, verifier
    return (
        int(match["iterations"]),
        base64.b64decode(match["salt"]),
        base64.b64decode(match["stored"]),
        base64.b64decode(match["server"]),
    )


def test_verifier_matches_rfc_7677_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
    # "pencil" no cumple la longitud mínima de la política: se mide solo la criptografía.
    monkeypatch.setattr("vigia_platform.shared.role_passwords.validate_role_password", len)
    iterations, salt, stored_key, server_key = _parts(
        scram_sha256_verifier("pencil", salt=RFC_SALT)
    )
    assert (iterations, salt) == (4096, RFC_SALT)
    # El servidor recupera ClientKey de la prueba y comprueba H(ClientKey) == StoredKey.
    client_signature = hmac.digest(stored_key, RFC_AUTH_MESSAGE, "sha256")
    client_key = bytes(a ^ b for a, b in zip(RFC_CLIENT_PROOF, client_signature, strict=True))
    assert hashlib.sha256(client_key).digest() == stored_key
    assert hmac.digest(server_key, RFC_AUTH_MESSAGE, "sha256") == RFC_SERVER_SIGNATURE


@given(
    password=st.text(
        alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E), min_size=16, max_size=64
    )
)
def test_verifier_never_contains_the_password(password: str) -> None:
    verifier = scram_sha256_verifier(password, salt=b"s" * 16)
    iterations, salt, stored, server = _parts(verifier)
    assert (iterations, salt, len(stored), len(server)) == (4096, b"s" * 16, 32, 32)
    assert password not in verifier


APP = "vigia_app"


def test_random_salt_differs_per_call() -> None:
    passwords = {APP: "x" * 32}
    first = role_password_verifier(APP, passwords)
    second = role_password_verifier(APP, passwords)
    assert _parts(first)[1] != _parts(second)[1]


@pytest.mark.parametrize(
    "password",
    [
        "a" * MIN_PASSWORD_LENGTH,
        "~" * MAX_PASSWORD_LENGTH,
        " !\"#$%&'()*+,-./0123456789:;<=>?@AZaz[\\]^_`{|}~",
    ],
)
def test_policy_accepts_printable_ascii_within_bounds(password: str) -> None:
    validate_role_password(password)


@pytest.mark.parametrize(
    "password",
    [
        "",
        "a" * (MIN_PASSWORD_LENGTH - 1),
        "a" * (MAX_PASSWORD_LENGTH + 1),
        "contraseña-larga-con-eñe",
        "tab\there-is-long-enough",
        "newline\nis-long-enough-x",
        "nul\x00byte-long-enough-xx",
        "emoji-🙂-long-enough-xxx",
    ],
)
def test_policy_rejects_and_never_echoes_the_value(password: str) -> None:
    with pytest.raises(RolePasswordError) as caught:
        role_password_verifier(APP, {APP: password})
    assert APP in str(caught.value)
    if password:
        assert password not in str(caught.value)


def test_missing_role_is_rejected() -> None:
    with pytest.raises(RolePasswordError, match="falta la contraseña de vigia_app"):
        role_password_verifier(APP, {"vigia_migrate": "x" * 32})


@pytest.mark.parametrize(("salt", "iterations"), [(b"s" * 15, 4096), (b"s" * 16, 4095)])
def test_weak_salt_or_iterations_are_rejected(salt: bytes, iterations: int) -> None:
    with pytest.raises(ValueError, match="sal de al menos 16 bytes"):
        scram_sha256_verifier("x" * 32, salt=salt, iterations=iterations)
