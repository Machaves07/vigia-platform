"""PR-NUC-09: TOTP con último paso aceptado y códigos de recuperación de un solo uso (TASK-123).

"Un código TOTP se acepta solo dentro de ±1 paso del reloj y un paso ya aceptado nunca se acepta
de nuevo; un código de recuperación se acepta exactamente una vez" (``totp_clock_offsets``).

- **TOTP**: el código se calcula con ``pyotp.TOTP`` (camino independiente del de la plataforma)
  para un paso generado, y se verifica con desfases de reloj generados de ±5 pasos: se acepta si
  y solo si el desfase cae dentro de ±1 paso y el paso es posterior al último aceptado.
- **Secuencias**: intentos generados sobre una misma credencial, con la credencial recargada o
  con la instantánea vieja (la de la inscripción), contra un modelo; ningún paso se acepta dos
  veces y el último aceptado solo crece. Lo garantiza también el almacén (``advance_step``).
- **Recuperación**: intentos generados (códigos propios con otros formatos, repetidos, basura);
  cada código se acepta exactamente una vez. Los de una inscripción anterior ya no valen.
- **Cifrado de sobre** (``shared.crypto``): ida y vuelta; un byte alterado, otro dato asociado u
  otra clave envuelta terminan en ``DecryptionFailed``; caché de 5 minutos.
- **Volcado**: ni ``TotpCredential`` ni nada de lo guardado o registrado contiene el secreto.
- **FS-NUC-05 (b)** a nivel de módulo: sin KMS la verificación sigue con la clave de datos en
  memoria y una inscripción nueva responde ``temporarily_unavailable``; nunca se omite el factor.

Solo datos generados (NFR-CTR-43). Los códigos de recuperación de las propiedades se guardan con
un Argon2id barato (los hashes llevan sus parámetros): el coste real se prueba en los ejemplos.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import logging
import uuid
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import pyotp
import pytest
from argon2 import PasswordHasher, Type
from hypothesis import assume, given
from hypothesis import strategies as st

from tests.factories import make_context
from tests.second_factor_support import FakeKms, InMemorySecondFactorStore
from vigia_platform.identity.auth.second_factor import (
    RECOVERY_CODE_ALPHABET,
    RECOVERY_CODE_COUNT,
    RECOVERY_CODE_HASH,
    TOTP_SECRET_BYTES,
    TOTP_STEP_SECONDS,
    AlreadyEnrolled,
    EnrollmentChallenge,
    RecoveryCodeRecord,
    SecondFactorNotFound,
    SecondFactorService,
    SecondFactorUser,
    TotpCredential,
    credential_aad,
    generate_recovery_codes,
    match_totp,
    normalize_recovery_code,
    totp_step,
    verify_recovery_code,
)
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import ActorKind, ScopeContext
from vigia_platform.shared.cpu_pool import CpuPool
from vigia_platform.shared.crypto import (
    DATA_KEY_CACHE_TTL_SECONDS,
    MAX_AAD_BYTES,
    MAX_PLAINTEXT_BYTES,
    DecryptionFailed,
    EnvelopeCipher,
)
from vigia_platform.shared.observability.metrics import PlatformMetrics
from vigia_platform.shared.secrets import SecretsUnavailable

START = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
KEY_ID = "alias/vigia-secrets"
_POOL = CpuPool(SimulatedClock(START), max_workers=2)
_CHEAP_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1, type=Type.ID)
"""Solo para sembrar códigos en las propiedades; ``verify_recovery_code`` lee los parámetros del
hash, así que verifica igual que con los reales."""

secrets_ = st.binary(min_size=TOTP_SECRET_BYTES, max_size=TOTP_SECRET_BYTES)
# Pasos de 2001 a 2100 aproximadamente (fechas reales, lejos de la época).
steps = st.integers(min_value=1_000_000_000 // 30, max_value=4_100_000_000 // 30)


def run[T](awaitable: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(awaitable)


def at_step(step: int, offset_seconds: int = 0) -> datetime:
    return EPOCH + timedelta(seconds=step * TOTP_STEP_SECONDS + offset_seconds)


def independent_code(secret: bytes, step: int) -> str:
    """Código de ``pyotp.TOTP`` (RFC 6238) en el instante de inicio del paso."""
    return pyotp.TOTP(base64.b32encode(secret).decode()).at(step * TOTP_STEP_SECONDS)


class CountingMetrics:
    def __init__(self) -> None:
        self.secrets_refresh_failed = self
        self.added: list[tuple[int, dict[str, str]]] = []

    def add(self, amount: int, attributes: dict[str, str]) -> None:
        self.added.append((amount, attributes))


@dataclasses.dataclass
class Harness:
    clock: SimulatedClock
    kms: FakeKms
    cipher: EnvelopeCipher
    store: InMemorySecondFactorStore
    service: SecondFactorService
    context: ScopeContext
    user: SecondFactorUser
    metrics: CountingMetrics

    def fresh_process(self) -> Harness:
        """Otro proceso: mismo KMS y misma base, sin claves de datos en memoria."""
        metrics = CountingMetrics()
        cipher = EnvelopeCipher(
            self.kms, KEY_ID, self.clock, metrics=cast(PlatformMetrics, metrics)
        )
        service = SecondFactorService(self.store, cipher, _POOL, self.clock)
        return dataclasses.replace(self, cipher=cipher, service=service, metrics=metrics)

    def add_user(self) -> SecondFactorUser:
        user = SecondFactorUser(uuid.uuid4(), self.store.organization_id, "persona@example.test")
        self.store.users.add(user.user_id)
        return user


def harness(now: datetime = START) -> Harness:
    clock = SimulatedClock(now)
    kms = FakeKms()
    metrics = CountingMetrics()
    cipher = EnvelopeCipher(kms, KEY_ID, clock, metrics=cast(PlatformMetrics, metrics))
    organization_id = uuid.uuid4()
    store = InMemorySecondFactorStore(organization_id)
    context = make_context(kind=ActorKind.USER, organization_id=organization_id)
    user = SecondFactorUser(uuid.uuid4(), organization_id, "persona@example.test")
    store.users.add(user.user_id)
    service = SecondFactorService(store, cipher, _POOL, clock)
    return Harness(clock, kms, cipher, store, service, context, user, metrics)


def secret_of(challenge: EnrollmentChallenge) -> bytes:
    query = parse_qs(urlparse(challenge.provisioning_uri).query)
    encoded = query["secret"][0]
    return base64.b32decode(encoded + "=" * (-len(encoded) % 8))


def seeded_credential(h: Harness, secret: bytes, codes: tuple[str, ...] = ()) -> TotpCredential:
    """Credencial activa cifrada con el cifrado real y códigos con hash barato."""
    sealed = run(h.cipher.encrypt(secret, credential_aad(h.user.organization_id, h.user.user_id)))
    credential = TotpCredential(
        user_id=h.user.user_id,
        organization_id=h.user.organization_id,
        secret_encrypted=sealed.ciphertext,
        data_key_wrapped=sealed.wrapped_key,
        enrolled_at=h.clock.now(),
    )
    records = [
        RecoveryCodeRecord(
            recovery_code_id=uuid.uuid4(),
            user_id=h.user.user_id,
            organization_id=h.user.organization_id,
            code_hash=_CHEAP_HASHER.hash(cast(str, normalize_recovery_code(code))),
            generated_at=credential.enrolled_at,
        )
        for code in codes
    ]
    run(h.store.save_enrollment(h.context, credential, records))
    return credential


# --- PR-NUC-09: ventana de ±1 paso -------------------------------------------------------------


@given(
    secret=secrets_,
    code_step=steps,
    clock_offset=st.integers(min_value=-5 * TOTP_STEP_SECONDS, max_value=5 * TOTP_STEP_SECONDS),
    last=st.one_of(st.none(), st.integers(min_value=-3, max_value=3)),
)
def test_code_accepted_only_within_one_step_and_after_last(
    secret: bytes, code_step: int, clock_offset: int, last: int | None
) -> None:
    now = at_step(code_step, clock_offset)
    current = totp_step(now)
    window = range(current - 1, current + 2)
    code = independent_code(secret, code_step)
    # Sin colisiones de código dentro de la ventana (probabilidad ~3e-6): el oráculo es exacto.
    assume(all(independent_code(secret, s) != code for s in window if s != code_step))
    last_accepted = None if last is None else code_step + last
    expected = (
        code_step
        if code_step in window and (last_accepted is None or code_step > last_accepted)
        else None
    )
    assert match_totp(secret, code, now, last_accepted) == expected


@given(
    secret=secrets_,
    base=steps,
    attempts=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=8),
            st.integers(min_value=0, max_value=8),
            st.integers(min_value=0, max_value=TOTP_STEP_SECONDS - 1),
        ),
        min_size=1,
        max_size=25,
    ),
    reload=st.booleans(),
)
def test_a_step_is_never_accepted_twice(
    secret: bytes, base: int, attempts: list[tuple[int, int, int]], reload: bool
) -> None:
    codes = [independent_code(secret, base + i) for i in range(9)]
    assume(len(set(codes)) == len(codes))
    h = harness(at_step(base))
    snapshot = seeded_credential(h, secret)
    model_last: int | None = None
    accepted: list[int] = []
    for code_index, clock_index, second in attempts:
        now = at_step(base + clock_index, second)
        credential = run(h.store.get_credential(h.context, h.user.user_id)) if reload else snapshot
        assert credential is not None
        result = run(h.service.verify_totp(h.context, credential, codes[code_index], now))
        expected = abs(code_index - clock_index) <= 1 and (
            model_last is None or code_index > model_last
        )
        assert result is expected
        if result:
            model_last = code_index
            accepted.append(code_index)
    assert accepted == sorted(set(accepted))
    stored = run(h.store.get_credential(h.context, h.user.user_id))
    assert stored is not None
    assert stored.last_accepted_step == (None if model_last is None else base + model_last)


@given(secret=secrets_, step=steps)
def test_same_code_twice_in_the_same_step_is_rejected(secret: bytes, step: int) -> None:
    h = harness(at_step(step))
    credential = seeded_credential(h, secret)
    code = independent_code(secret, step)
    assert run(h.service.verify_totp(h.context, credential, code, at_step(step, 5))) is True
    for offset in (5, 29, 30, -30):
        assert (
            run(h.service.verify_totp(h.context, credential, code, at_step(step, offset))) is False
        )


# --- PR-NUC-09: códigos de recuperación --------------------------------------------------------


def _variants(code: str) -> st.SearchStrategy[str]:
    raw = code.replace("-", "")
    return st.sampled_from([code, code.lower(), raw, raw.lower(), f" {raw[:5]} {raw[5:]} "])


garbage = st.one_of(
    st.text(max_size=20),
    st.text(RECOVERY_CODE_ALPHABET + "ILOU-", min_size=9, max_size=12),
)


@given(data=st.data(), attempts=st.integers(min_value=1, max_value=30))
def test_each_recovery_code_accepted_exactly_once(data: st.DataObject, attempts: int) -> None:
    codes = generate_recovery_codes()
    h = harness()
    credential = seeded_credential(h, b"s" * TOTP_SECRET_BYTES, codes)
    canonical = {normalize_recovery_code(code) for code in codes}
    used: set[int] = set()
    for _ in range(attempts):
        index = data.draw(st.one_of(st.none(), st.integers(0, RECOVERY_CODE_COUNT - 1)))
        if index is None:
            attempt = data.draw(garbage)
            assume(normalize_recovery_code(attempt) not in canonical)
            expected = False
        else:
            attempt = data.draw(_variants(codes[index]))
            expected = index not in used
            used.add(index)
        assert run(h.service.consume_recovery_code(h.context, credential, attempt)) is expected
    marked = [r for r in h.store.codes.values() if r.used_at is not None]
    assert len(marked) == len(used)


def test_recovery_codes_are_ten_distinct_crockford_codes() -> None:
    codes = generate_recovery_codes()
    assert len(codes) == len(set(codes)) == RECOVERY_CODE_COUNT
    for code in codes:
        assert len(code) == 11 and code[5] == "-"
        assert set(code.replace("-", "")) <= set(RECOVERY_CODE_ALPHABET)


@pytest.mark.parametrize(
    "attempt",
    [
        "",
        "ABCDE-FGHJ",  # 9 símbolos
        "ABCDE-FGHJKM",  # 11 símbolos
        "ABCDE-FGHIK",  # I no es del alfabeto
        "ABCDE-FGHLK",
        "ABCDE-FGHOK",
        "ABCDE-FGHUK",
        "ABCDE_FGHJK",
        "ABCDE-FGHJ\u212a",  # KELVIN SIGN: no ASCII
        "ABCDE\u00a0FGHJK",  # NBSP
        "ABCDE-FGHJK\u200b",
        "\uff21BCDE-FGHJK",  # ancho completo
        "A" * 65,
    ],
)
def test_malformed_recovery_codes_never_normalize(attempt: str) -> None:
    assert normalize_recovery_code(attempt) is None


def test_recovery_code_normalization_accepts_copy_formats() -> None:
    assert normalize_recovery_code("abcde-fghjk") == "ABCDEFGHJK"
    assert normalize_recovery_code(" ABCDE FGHJK ") == "ABCDEFGHJK"
    assert normalize_recovery_code(123) is None


def test_real_enrollment_hashes_codes_with_argon2id_and_consumes_once() -> None:
    h = harness()
    challenge = run(h.service.enroll(h.context, h.user))
    stored = [r for r in h.store.codes.values() if r.user_id == h.user.user_id]
    assert len(stored) == RECOVERY_CODE_COUNT
    params = RECOVERY_CODE_HASH
    for record in stored:
        assert record.code_hash.startswith(
            f"$argon2id$v=19$m={params.memory_kib},t={params.iterations},p={params.parallelism}$"
        )
        assert all(code not in record.code_hash for code in challenge.recovery_codes)
    code = challenge.recovery_codes[3]
    credential = challenge.credential
    assert run(h.service.consume_recovery_code(h.context, credential, code)) is True
    assert run(h.service.consume_recovery_code(h.context, credential, code)) is False
    assert run(h.service.consume_recovery_code(h.context, credential, code.lower())) is False
    assert verify_recovery_code("ABCDEFGHJK", "no-es-un-hash") is False


def test_old_enrollment_codes_and_secret_stop_working_after_reset() -> None:
    h = harness()
    first = run(h.service.enroll(h.context, h.user))
    old_secret = secret_of(first)
    with pytest.raises(AlreadyEnrolled):
        run(h.service.enroll(h.context, h.user))
    h.store.sessions[h.user.user_id] = 2
    assert run(h.service.reset(h.context, h.user.user_id)) == 2
    assert h.store.enrolled_at[h.user.user_id] is None
    disabled = run(h.store.get_credential(h.context, h.user.user_id))
    assert disabled is not None and not disabled.active
    now = h.clock.now()
    code = independent_code(old_secret, totp_step(now))
    assert run(h.service.verify_totp(h.context, disabled, code, now)) is False
    h.clock.advance(1)
    second = run(h.service.enroll(h.context, h.user))
    assert secret_of(second) != old_secret
    current = second.credential
    assert run(h.service.verify_totp(h.context, current, code, now)) is False
    for old_code in first.recovery_codes[:3]:
        assert run(h.service.consume_recovery_code(h.context, current, old_code)) is False
    assert run(h.service.consume_recovery_code(h.context, current, second.recovery_codes[0]))
    assert h.store.audit == [
        ("second_factor_enrolled", h.user.user_id),
        ("second_factor_reset", h.user.user_id),
        ("second_factor_enrolled", h.user.user_id),
    ]


# --- Formato del código y del instante ---------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "",
        "12345",
        "1234567",
        " 123456",
        "123456\n",
        "\u0661\u0662\u0663\u0664\u0665\u0666",  # dígitos arábigos
        "12345\uff16",  # dígito de ancho completo
        "\U0001d7cf\U0001d7d0\U0001d7d1\U0001d7d2\U0001d7d3\U0001d7d4",  # dígitos matemáticos
        "12 456",
        "-12345",
    ],
)
def test_malformed_totp_codes_are_rejected_without_touching_kms(code: str) -> None:
    h = harness()
    credential = seeded_credential(h, b"k" * TOTP_SECRET_BYTES)
    other = h.fresh_process()
    assert run(other.service.verify_totp(h.context, credential, code, h.clock.now())) is False
    assert h.kms.decrypt_calls == 0


def test_naive_or_pre_epoch_instants_are_rejected() -> None:
    with pytest.raises(ValueError):
        totp_step(datetime(2026, 9, 30, 8, 0))  # noqa: DTZ001 - sin zona a propósito
    with pytest.raises(ValueError):
        totp_step(datetime(1969, 12, 31, 23, 59, tzinfo=UTC))
    assert totp_step(EPOCH) == 0
    assert totp_step(EPOCH + timedelta(seconds=29, microseconds=999_999)) == 0
    assert totp_step(EPOCH + timedelta(seconds=30)) == 1
    assert match_totp(b"k" * 20, independent_code(b"k" * 20, 0), EPOCH, None) == 0


def test_code_of_another_credential_row_does_not_decrypt() -> None:
    h = harness()
    credential = seeded_credential(h, b"a" * TOTP_SECRET_BYTES)
    moved = dataclasses.replace(credential, user_id=uuid.uuid4())
    code = independent_code(b"a" * TOTP_SECRET_BYTES, totp_step(h.clock.now()))
    with pytest.raises(DecryptionFailed):
        run(h.service.verify_totp(h.context, moved, code, h.clock.now()))


# --- Cifrado de sobre --------------------------------------------------------------------------

plaintexts = st.binary(min_size=1, max_size=MAX_PLAINTEXT_BYTES)
aads = st.binary(min_size=1, max_size=MAX_AAD_BYTES)


@given(plaintext=plaintexts, aad=aads)
def test_envelope_round_trip_in_another_process(plaintext: bytes, aad: bytes) -> None:
    h = harness()
    sealed = run(h.cipher.encrypt(plaintext, aad))
    assert plaintext not in sealed.ciphertext or len(plaintext) < 4
    assert run(h.fresh_process().cipher.decrypt(sealed.ciphertext, sealed.wrapped_key, aad)) == (
        plaintext
    )


@given(plaintext=plaintexts, aad=aads, data=st.data())
def test_any_tampering_fails_closed(plaintext: bytes, aad: bytes, data: st.DataObject) -> None:
    h = harness()
    sealed = run(h.cipher.encrypt(plaintext, aad))
    other = run(h.cipher.encrypt(plaintext, aad))
    position = data.draw(st.integers(0, len(sealed.ciphertext) - 1))
    bit = data.draw(st.integers(0, 7))
    flipped = bytearray(sealed.ciphertext)
    flipped[position] ^= 1 << bit
    other_aad = data.draw(aads.filter(lambda value: value != aad))
    for ciphertext, wrapped, associated in (
        (bytes(flipped), sealed.wrapped_key, aad),
        (sealed.ciphertext, sealed.wrapped_key, other_aad),
        (sealed.ciphertext, other.wrapped_key, aad),
        (sealed.ciphertext[:-1], sealed.wrapped_key, aad),
        (sealed.ciphertext + b"\x00", sealed.wrapped_key, aad),
    ):
        with pytest.raises(DecryptionFailed):
            run(h.cipher.decrypt(ciphertext, wrapped, associated))


def test_envelope_rejects_bad_inputs() -> None:
    h = harness()
    for plaintext, aad in (
        (b"", b"a"),
        (b"x" * (MAX_PLAINTEXT_BYTES + 1), b"a"),
        (b"x", b""),
        (b"x", b"a" * (MAX_AAD_BYTES + 1)),
        (cast(bytes, "texto"), b"a"),
    ):
        with pytest.raises(ValueError):
            run(h.cipher.encrypt(plaintext, aad))
    sealed = run(h.cipher.encrypt(b"x", b"a"))
    for ciphertext, wrapped, aad in (
        (b"\x02" + sealed.ciphertext[1:], sealed.wrapped_key, b"a"),
        (b"", sealed.wrapped_key, b"a"),
        (sealed.ciphertext, b"", b"a"),
        (sealed.ciphertext, sealed.wrapped_key, b""),
        (sealed.ciphertext, b"w" * 1025, b"a"),
    ):
        with pytest.raises(DecryptionFailed):
            run(h.cipher.decrypt(ciphertext, wrapped, aad))
    assert h.kms.generate_calls == 1
    assert "texto" not in repr(sealed)


def test_data_key_cache_lasts_five_minutes() -> None:
    h = harness()
    sealed = run(h.cipher.encrypt(b"secreto", b"aad"))
    other = h.fresh_process()
    for _ in range(3):
        run(other.cipher.decrypt(sealed.ciphertext, sealed.wrapped_key, b"aad"))
    assert h.kms.decrypt_calls == 1
    h.clock.advance(DATA_KEY_CACHE_TTL_SECONDS - 0.001)
    run(other.cipher.decrypt(sealed.ciphertext, sealed.wrapped_key, b"aad"))
    assert h.kms.decrypt_calls == 1
    h.clock.advance(0.001)
    run(other.cipher.decrypt(sealed.ciphertext, sealed.wrapped_key, b"aad"))
    assert h.kms.decrypt_calls == 2


def test_cache_is_bounded() -> None:
    clock = SimulatedClock(START)
    kms = FakeKms()
    cipher = EnvelopeCipher(kms, KEY_ID, clock, max_cached_keys=2)
    sealed = [run(cipher.encrypt(b"x", b"a")) for _ in range(3)]
    assert repr(cipher) == "EnvelopeCipher(cached=2)"
    run(cipher.decrypt(sealed[0].ciphertext, sealed[0].wrapped_key, b"a"))
    assert kms.decrypt_calls == 1  # la más antigua se descartó
    run(cipher.decrypt(sealed[2].ciphertext, sealed[2].wrapped_key, b"a"))
    assert kms.decrypt_calls == 1


# --- FS-NUC-05 (b): KMS inaccesible en operación -----------------------------------------------


def test_without_kms_verification_continues_and_enrollment_is_unavailable() -> None:
    h = harness()
    challenge = run(h.service.enroll(h.context, h.user))
    secret = secret_of(challenge)
    # Otro proceso ya había verificado antes de la caída: tiene la clave de datos en memoria.
    api = h.fresh_process()
    now = h.clock.now()
    assert run(
        api.service.verify_totp(
            h.context, challenge.credential, independent_code(secret, totp_step(now)), now
        )
    )
    h.kms.down = True
    h.clock.advance(10 * 60)
    now = h.clock.now()
    credential = run(h.store.get_credential(h.context, h.user.user_id))
    assert credential is not None
    code = independent_code(secret, totp_step(now))
    assert run(api.service.verify_totp(h.context, credential, code, now)) is True
    assert api.metrics.added == [(1, {"dependency": "kms"})]
    # Inscripción nueva: temporarily_unavailable, nada guardado, nada auditado.
    newcomer = h.add_user()
    before = (dict(h.store.credentials), dict(h.store.codes), list(h.store.audit))
    with pytest.raises(SecretsUnavailable) as raised:
        run(api.service.enroll(h.context, newcomer))
    assert raised.value.code == "temporarily_unavailable"
    assert (h.store.credentials, h.store.codes, h.store.audit) == before
    assert run(h.store.get_credential(h.context, newcomer.user_id)) is None
    # Un proceso sin la clave en memoria falla cerrado: nunca acepta sin verificar.
    cold = h.fresh_process()
    with pytest.raises(SecretsUnavailable):
        run(cold.service.verify_totp(h.context, credential, code, now))


def test_enrollment_without_kms_from_the_start_saves_nothing() -> None:
    h = harness()
    h.kms.down = True
    with pytest.raises(SecretsUnavailable):
        run(h.service.enroll(h.context, h.user))
    assert h.store.credentials == {} and h.store.codes == {} and h.store.audit == []


# --- Volcado: el secreto no aparece en claro ---------------------------------------------------


def _forms(secret: bytes) -> list[bytes]:
    b32 = base64.b32encode(secret).rstrip(b"=")
    return [secret, secret.hex().encode(), b32, b32.lower(), base64.b64encode(secret)]


def test_credential_dump_contains_no_plain_secret(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    h = harness()
    challenge = run(h.service.enroll(h.context, h.user))
    secret = secret_of(challenge)
    credential = run(h.store.get_credential(h.context, h.user.user_id))
    assert credential is not None
    dumps = [
        repr(credential).encode(),
        repr(dataclasses.asdict(credential)).encode(),
        credential.secret_encrypted,
        credential.data_key_wrapped,
        repr(challenge).encode(),
        repr(h.store).encode(),
        repr(h.cipher).encode(),
        repr(h.service).encode(),
        caplog.text.encode(),
    ]
    for form in _forms(secret):
        for dump in dumps:
            assert form not in dump
    for code in challenge.recovery_codes:
        assert code.encode() not in repr(h.store).encode()
        assert code.replace("-", "").encode() not in repr(h.store).encode()
    assert "secret=" not in repr(challenge)


# --- Restablecimiento (BR-NUC-29) --------------------------------------------------------------


def test_reset_is_limited_to_people_of_the_same_organization() -> None:
    h = harness()
    run(h.service.enroll(h.context, h.user))
    stranger = make_context(kind=ActorKind.USER, organization_id=uuid.uuid4())
    with pytest.raises(SecondFactorNotFound):
        run(h.service.reset(stranger, h.user.user_id))
    node = make_context(kind=ActorKind.NODE, organization_id=h.store.organization_id)
    with pytest.raises(PermissionError):
        run(h.service.reset(node, h.user.user_id))
    with pytest.raises(SecondFactorNotFound):
        run(h.service.enroll(stranger, h.user))
    credential = run(h.store.get_credential(h.context, h.user.user_id))
    assert credential is not None and credential.active
