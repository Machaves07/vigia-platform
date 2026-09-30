"""PR-NUC-08: política de contraseñas de BR-NUC-20 (LC-NUC-01, TASK-122).

"Toda contraseña de menos de 8 caracteres, igual al correo o presente en la muestra de filtradas
se rechaza; toda contraseña de 8 a 128 caracteres fuera de la muestra se acepta."

La muestra de filtradas es el respaldo local (``LocalBreachList``) más lo que responde el
servicio por rango (``FakeRangeService``, sin red). Cada propiedad corre con el servicio
disponible, caído (conexión rechazada), con respuesta ilegible y con el circuito abierto: lo que
está en el respaldo local se rechaza siempre, y lo que no está en ninguna parte se acepta
siempre, responda o no el servicio (PAT-NUC-RES-03). Además, por la red solo salen los 5
primeros caracteres del SHA-1 (anonimato k).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

from hypothesis import assume, given
from hypothesis import strategies as st

from tests.hibp_service import (
    RANGE_URL,
    FakeRangeService,
    Mode,
    checker,
    local_list,
)
from vigia_platform.identity.adapters.hibp import HibpBreachChecker, sha1_hex
from vigia_platform.identity.auth.passwords import (
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    BreachSource,
    PasswordService,
    PolicyResult,
    PolicyViolation,
    comparable_form,
)
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.cpu_pool import CpuPool

START = datetime(2026, 9, 29, tzinfo=UTC)
# Solo ``check_policy``: el pool no llega a usarse, pero el servicio lo exige.
_POOL = CpuPool(SimulatedClock(START), max_workers=1)

# Letras, dígitos, símbolos, espacios y no ASCII; sin sustitutos sueltos (tienen su prueba).
_CHARS = st.characters(codec="utf-8")
valid_passwords = st.text(_CHARS, min_size=PASSWORD_MIN_LENGTH, max_size=PASSWORD_MAX_LENGTH)
short_passwords = st.text(_CHARS, max_size=PASSWORD_MIN_LENGTH - 1)
long_passwords = st.text(_CHARS, min_size=PASSWORD_MAX_LENGTH + 1, max_size=PASSWORD_MAX_LENGTH * 3)
_EMAIL_PART = st.text("abcdefghijklmnopqrstuvwxyz0123456789._-", min_size=1, max_size=20)
emails = st.builds(lambda local, domain: f"{local}@{domain}.test", _EMAIL_PART, _EMAIL_PART)
# ``slow`` (latencia real de más de 3 s) se prueba en ``tests/unit/test_hibp_fallback.py``.
modes = st.sampled_from(["up", "down", "bad", "circuit_open"])


def _prepare(service: FakeRangeService, breach_checker: HibpBreachChecker, mode: str) -> None:
    if mode == "circuit_open":
        service.mode = "up"
        for _ in range(3):
            breach_checker.breaker.record_failure()
    else:
        service.mode = cast(Mode, mode)


def _check(
    password: str,
    email: str,
    *,
    local: list[str],
    remote: Sequence[str] = (),
    mode: str = "up",
) -> tuple[PolicyResult, FakeRangeService]:
    service = FakeRangeService()
    service.add(remote)
    breach_checker = checker(service, local_list(local))
    _prepare(service, breach_checker, mode)
    service_under_test = PasswordService(breach_checker, _POOL)

    async def run() -> PolicyResult:
        try:
            return await service_under_test.check_policy(password, email)
        finally:
            await breach_checker.aclose()

    return asyncio.run(run()), service


_DECOY = ["contraseña-señuelo-0001"]
"""El respaldo local nunca está vacío: una entrada ajena a la prueba."""


@given(password=short_passwords, email=emails, mode=modes)
def test_short_passwords_are_rejected_without_querying(
    password: str, email: str, mode: str
) -> None:
    result, service = _check(password, email, local=_DECOY, mode=mode)
    assert not result.ok
    assert PolicyViolation.TOO_SHORT in result.violations
    assert result.breach_source is None
    assert service.requests == []


@given(password=long_passwords, email=emails)
def test_passwords_over_128_are_rejected(password: str, email: str) -> None:
    result, service = _check(password, email, local=_DECOY)
    assert not result.ok
    assert PolicyViolation.TOO_LONG in result.violations
    assert service.requests == []


_ZERO_WIDTH = st.sampled_from([chr(c) for c in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD)])
_SPACES = st.sampled_from([chr(c) for c in (0x20, 0x09, 0xA0, 0x2003, 0x3000)])


@st.composite
def email_variants(draw: st.DrawFn) -> tuple[str, str]:
    """Un correo de al menos 8 caracteres y una variante suya: mayúsculas, espacios alrededor,
    invisibles intercalados o formas de ancho completo (NFKC)."""
    email = draw(emails.filter(lambda text: len(text) >= PASSWORD_MIN_LENGTH))
    chars = []
    for char in email:
        variant = char
        if draw(st.booleans()):
            variant = variant.upper()
        if draw(st.booleans()) and "!" <= char <= "~":
            variant = chr(ord(variant) - 0x21 + 0xFF01)  # ancho completo
        chars.append(variant)
        if draw(st.integers(0, 4)) == 0:
            chars.append(draw(_ZERO_WIDTH))
    variant = draw(_SPACES) * draw(st.integers(0, 2)) + "".join(chars)
    variant += draw(_SPACES) * draw(st.integers(0, 2))
    assume(len(variant) <= PASSWORD_MAX_LENGTH)
    return email, variant


@given(pair=email_variants(), mode=modes)
def test_password_equal_to_email_is_rejected(pair: tuple[str, str], mode: str) -> None:
    email, variant = pair
    for password in (email, variant):
        result, service = _check(password, email, local=_DECOY, mode=mode)
        assert not result.ok
        assert result.violations == (PolicyViolation.EQUALS_EMAIL,)
        assert service.requests == []


@given(
    sample=st.lists(valid_passwords, min_size=1, max_size=5, unique=True),
    data=st.data(),
    email=emails,
    mode=modes,
)
def test_password_in_local_sample_is_always_rejected(
    sample: list[str], data: st.DataObject, email: str, mode: str
) -> None:
    password = data.draw(st.sampled_from(sample))
    assume(comparable_form(password) != comparable_form(email))
    result, service = _check(password, email, local=sample, mode=mode)
    assert result.violations == (PolicyViolation.BREACHED,)
    assert result.breach_source is BreachSource.LOCAL_LIST
    assert service.requests == []  # decide la lista local, sin salir a la red


@given(password=valid_passwords, email=emails)
def test_password_breached_in_service_is_rejected_when_it_answers(
    password: str, email: str
) -> None:
    result, _ = _check(password, email, local=_DECOY, remote=[password], mode="up")
    assume(PolicyViolation.EQUALS_EMAIL not in result.violations)
    assert result.violations == (PolicyViolation.BREACHED,)
    assert result.breach_source is BreachSource.REMOTE


@given(
    password=valid_passwords,
    others=st.lists(valid_passwords, min_size=1, max_size=5),
    email=emails,
    mode=modes,
)
def test_valid_password_outside_the_sample_is_always_accepted(
    password: str, others: list[str], email: str, mode: str
) -> None:
    assume(password not in others)
    result, service = _check(password, email, local=others, remote=others, mode=mode)
    assume(PolicyViolation.EQUALS_EMAIL not in result.violations)
    assert result.ok
    assert result.violations == ()
    expected = BreachSource.REMOTE if mode == "up" else BreachSource.LOCAL_FALLBACK
    assert result.breach_source is expected
    assert len(service.requests) == (0 if mode == "circuit_open" else 1)


@given(password=valid_passwords, email=emails, remote=st.lists(valid_passwords, max_size=5))
def test_only_the_five_character_prefix_leaves(
    password: str, email: str, remote: list[str]
) -> None:
    """Anonimato k: la URL lleva los 5 primeros caracteres del SHA-1 y nada más de la persona."""
    result, service = _check(password, email, local=_DECOY, remote=remote)
    assume(result.breach_source is not None)
    [request] = service.requests
    digest = sha1_hex(password)
    assert str(request.url) == RANGE_URL + digest[:5]
    assert request.method == "GET"
    assert request.content == b""
    # Cabeceras fijas: ninguna lleva nada de la contraseña, del hash ni del correo.
    assert request.headers["Add-Padding"] == "true"
    assert request.headers["Accept-Encoding"] == "identity"
    assert request.headers["User-Agent"] == "vigia-platform"
    assert set(request.headers) <= {
        "host",
        "accept",
        "accept-encoding",
        "connection",
        "user-agent",
        "add-padding",
    }


def test_boundaries_of_the_length_rule() -> None:
    email = "persona@planta.test"
    assert _check("a" * 7, email, local=_DECOY)[0].violations == (PolicyViolation.TOO_SHORT,)
    assert _check("a" * 8, email, local=_DECOY)[0].ok
    assert _check("a" * 128, email, local=_DECOY)[0].ok
    assert _check("a" * 129, email, local=_DECOY)[0].violations == (PolicyViolation.TOO_LONG,)
    assert _check("", email, local=_DECOY)[0].violations == (PolicyViolation.TOO_SHORT,)
    # Puntos de código, no bytes: 8 letras con tilde (16 bytes) valen; 7 emojis no.
    assert _check("ñ" * 8, email, local=_DECOY)[0].ok
    assert not _check("🙂" * 7, email, local=_DECOY)[0].ok


def test_empty_email_never_matches() -> None:
    assert _check("contraseña-larga", "", local=_DECOY)[0].ok
    assert _check("        ", " ", local=_DECOY)[0].ok


def test_lone_surrogate_does_not_crash() -> None:
    result, service = _check("clave\ud800segura", "persona@planta.test", local=_DECOY)
    assert result.ok
    assert len(service.requests) == 1
