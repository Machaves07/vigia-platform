"""Código de alta, elegibilidad de emisión e intentos, sin base (TASK-218; BR-GOB-58 a 61).

- El código: 12 símbolos del alfabeto de 32 sin ambiguos (sin 0, O, 1 ni I), generado con
  ``secrets.choice``; sal de 16 bytes por código con ``secrets.token_bytes``; solo se guarda
  ``sha256(sal || código)``; ``expires_at = issued_at + 24 h``; el ``repr`` nunca lo muestra.
- La comparación recorre **todos** los códigos del nodo (sin salir al primer acierto) y nunca
  acepta un ``used``, ``expired`` (también derivado) o ``superseded``.
- La elegibilidad de la emisión: reemisión, re-alta o ``node_not_declared`` (nota U03-H-04).
- ``SourceIpHasher``: el mismo origen da el mismo hash en dos instancias con la misma clave, y
  nunca la dirección en claro.

Solo datos generados: ningún código literal en el árbol (BR-CTR-50).
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from vigia_platform.fleet.domain import enrollment_code as module
from vigia_platform.fleet.domain.enrollment_attempt import EnrollmentAttempt, SourceIpHasher
from vigia_platform.fleet.domain.enrollment_code import (
    ALPHABET,
    CODE_LENGTH,
    SALT_BYTES,
    VALIDITY,
    EnrollmentCode,
    check_presented,
    generate_code,
    new_salt,
    presented_code_hash,
    salted_hash,
)
from vigia_platform.fleet.domain.enums import (
    CredentialStatus,
    EnrollmentAttemptResult,
    EnrollmentCodeStatus,
)
from vigia_platform.fleet.domain.node_fleet_record import (
    CodeEligibility,
    CredentialState,
    NodeFleetRecord,
    code_eligibility,
)

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def _code(
    plain: str, *, status: EnrollmentCodeStatus = EnrollmentCodeStatus.ACTIVE, at: datetime = T0
) -> EnrollmentCode:
    salt = new_salt()
    return EnrollmentCode(
        code_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        plant_id=uuid.uuid4(),
        node_id=uuid.uuid4(),
        code_hash=salted_hash(salt, plain),
        code_salt=salt,
        issued_at=at,
        issued_by=uuid.uuid4(),
        expires_at=at + VALIDITY,
        disclosed_at=at,
        status=status,
        ledger_record_id=uuid.uuid4(),
    )


# --- Forma del código ---------------------------------------------------------------------------


def test_alphabet_has_32_unambiguous_symbols() -> None:
    assert len(ALPHABET) == 32 == len(set(ALPHABET))
    assert not set("0O1I") & set(ALPHABET)
    assert set(ALPHABET) == set("ABCDEFGHJKLMNPQRSTUVWXYZ23456789")


def test_generated_codes_have_twelve_symbols_of_the_alphabet() -> None:
    codes = {generate_code() for _ in range(500)}
    assert all(len(code) == CODE_LENGTH == 12 for code in codes)
    assert all(set(code) <= set(ALPHABET) for code in codes)
    assert len(codes) == 500  # 60 bits: ninguna repetición en 500


def test_generation_uses_secrets_choice_and_salt_uses_token_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Los valores por defecto son los de ``secrets``: nunca ``random``.
    assert generate_code.__defaults__ == (secrets.choice,)
    assert new_salt.__defaults__ == (secrets.token_bytes,)
    calls: list[str] = []

    def choice(alphabet: str) -> str:
        calls.append(alphabet)
        return alphabet[len(calls) % len(alphabet)]

    generate_code(choice)
    assert calls == [ALPHABET] * CODE_LENGTH


def test_a_generator_outside_the_alphabet_is_refused() -> None:
    with pytest.raises(ValueError, match="alfabeto"):
        generate_code(lambda _: "0")


def test_salt_has_16_random_bytes() -> None:
    salts = {new_salt() for _ in range(64)}
    assert SALT_BYTES == 16
    assert all(len(salt) == 16 for salt in salts) and len(salts) == 64
    with pytest.raises(ValueError):
        new_salt(lambda n: b"x" * (n - 1))


def test_only_the_salted_hash_is_kept_and_repr_hides_it() -> None:
    plain = generate_code()
    code = _code(plain)
    assert code.code_hash == hashlib.sha256(code.code_salt + plain.encode()).hexdigest()
    assert plain not in repr(code) and code.code_hash not in repr(code)
    assert not hasattr(code, "code")
    # La misma sal no se reutiliza: el mismo código da otro hash con otra sal.
    assert _code(plain).code_hash != code.code_hash


def test_expiry_is_issue_plus_24_hours() -> None:
    assert timedelta(hours=24) == VALIDITY
    code = _code(generate_code())
    assert code.expires_at - code.issued_at == timedelta(hours=24)
    with pytest.raises(ValueError, match="24 h"):
        EnrollmentCode(
            **{
                **{f: getattr(code, f) for f in code.__dataclass_fields__},
                "expires_at": code.issued_at + VALIDITY + timedelta(milliseconds=1),
            }
        )


def test_presented_code_hash_is_sha256_without_salt() -> None:
    plain = generate_code()
    assert presented_code_hash(plain) == hashlib.sha256(plain.encode()).hexdigest()


# --- Verificación -------------------------------------------------------------------------------


def test_the_active_code_is_accepted_until_its_expiry() -> None:
    plain = generate_code()
    code = _code(plain)
    check = check_presented([code], plain, T0 + VALIDITY - timedelta(milliseconds=1))
    assert check.valid and check.code == code
    # ``now ≥ expires_at``: vencido, aunque la tarea aún no lo haya escrito (derivado).
    at_expiry = check_presented([code], plain, T0 + VALIDITY)
    assert at_expiry.result is EnrollmentAttemptResult.ENROLLMENT_CODE_EXPIRED
    assert at_expiry.code is None


@pytest.mark.parametrize(
    ("status", "result"),
    [
        (EnrollmentCodeStatus.USED, EnrollmentAttemptResult.ENROLLMENT_CODE_USED),
        (EnrollmentCodeStatus.EXPIRED, EnrollmentAttemptResult.ENROLLMENT_CODE_EXPIRED),
        (EnrollmentCodeStatus.SUPERSEDED, EnrollmentAttemptResult.ENROLLMENT_CODE_INVALID),
    ],
)
def test_used_expired_and_superseded_codes_are_never_accepted(
    status: EnrollmentCodeStatus, result: EnrollmentAttemptResult
) -> None:
    plain = generate_code()
    check = check_presented([_code(plain, status=status)], plain, T0)
    assert check.result is result and not check.valid and check.code is None


@pytest.mark.parametrize(
    "presented",
    ["", "abc", None, 12, "x" * 13, "aaaaaaaaaaaa", "€" * 12],
)
def test_unknown_or_malformed_codes_are_invalid(presented: object) -> None:
    plain = generate_code()
    check = check_presented([_code(plain)], presented, T0)
    assert check.result is EnrollmentAttemptResult.ENROLLMENT_CODE_INVALID


def test_a_code_of_another_shape_never_matches_even_with_its_hash() -> None:
    # Un código fuera de la forma del contrato no se acepta aunque su hash coincida.
    plain = "a" * 12
    assert check_presented([_code(plain)], plain, T0).valid is False


def test_comparison_visits_every_code_of_the_node(monkeypatch: pytest.MonkeyPatch) -> None:
    plain = generate_code()
    codes = [_code(generate_code()) for _ in range(5)]
    codes.insert(1, _code(plain))
    seen: list[int] = []
    real = module.hmac.compare_digest

    def counting(a: bytes, b: bytes) -> bool:
        seen.append(1)
        return real(a, b)

    monkeypatch.setattr(module.hmac, "compare_digest", counting)
    assert check_presented(codes, plain, T0).valid
    assert len(seen) == len(codes)  # sin salir al primer acierto


# --- Elegibilidad de la emisión ------------------------------------------------------------------


def _record(**changes: object) -> NodeFleetRecord:
    values: dict[str, object] = {
        "node_id": uuid.uuid4(),
        "organization_id": uuid.uuid4(),
        "plant_id": uuid.uuid4(),
        "replaces_node_id": None,
        "hardware_fingerprint": None,
        "declared_at": T0 - timedelta(days=400),
        "declared_by": uuid.uuid4(),
    }
    values.update(changes)
    return NodeFleetRecord(**values)  # type: ignore[arg-type]


def _credential(status: CredentialStatus, expires_at: datetime) -> CredentialState:
    return CredentialState(uuid.uuid4(), status, expires_at)


REVOKED = {"revoked_at": T0 - timedelta(days=1), "revocation_reason_es": "Equipo robado en planta"}


@pytest.mark.parametrize(
    ("status", "record", "credentials", "expected"),
    [
        ("declared", {}, [], CodeEligibility.REISSUE),
        ("re_enrollment_pending", {}, [], CodeEligibility.REISSUE),
        ("revoked", REVOKED, [], CodeEligibility.RE_ENROLLMENT),
        (
            "enrolled",
            {},
            [_credential(CredentialStatus.ACTIVE, T0)],  # vencida justo ahora (derivado)
            CodeEligibility.RE_ENROLLMENT,
        ),
        (
            "enrolled",
            {},
            [_credential(CredentialStatus.REVOKED, T0 + timedelta(days=9))],
            CodeEligibility.RE_ENROLLMENT,
        ),
        (
            "enrolled",
            {},
            [_credential(CredentialStatus.SUPERSEDED, T0 + timedelta(days=9))],
            CodeEligibility.RE_ENROLLMENT,
        ),
        (
            "enrolled",
            {},
            [_credential(CredentialStatus.ACTIVE, T0 + timedelta(milliseconds=1))],
            CodeEligibility.REJECTED,
        ),
        (
            "enrolled",
            {},
            [
                _credential(CredentialStatus.SUPERSEDED, T0 + timedelta(days=9)),
                _credential(CredentialStatus.OVERLAPPING, T0 + timedelta(days=9)),
            ],
            CodeEligibility.REJECTED,
        ),
        (
            "revoked",
            {**REVOKED, "decommissioned_at": T0},
            [],
            CodeEligibility.REJECTED,
        ),
        ("unexpected", {}, [], CodeEligibility.REJECTED),
    ],
)
def test_code_eligibility(
    status: str,
    record: dict[str, object],
    credentials: list[CredentialState],
    expected: CodeEligibility,
) -> None:
    assert code_eligibility(status, _record(**record), credentials, T0) is expected


def test_decommission_requires_a_previous_revocation() -> None:
    with pytest.raises(ValueError, match="D-14"):
        _record(decommissioned_at=T0)
    with pytest.raises(ValueError, match="motivo"):
        _record(revoked_at=T0)


# --- Intentos y hash de origen -------------------------------------------------------------------


def test_source_ip_hash_is_stable_between_instances_and_never_the_address() -> None:
    key = secrets.token_bytes(32)
    first, second = SourceIpHasher(key), SourceIpHasher(key)
    address = "203.0.113.7"
    assert first.hash(address) == second.hash(address) == first.hash(" 203.0.113.7 ")
    assert address not in first.hash(address) and len(first.hash(address)) == 64
    assert SourceIpHasher(secrets.token_bytes(32)).hash(address) != first.hash(address)
    assert first.hash(None) == first.hash("") == first.hash("x" * 65) == first.hash("unknown")
    assert repr(first) == "SourceIpHasher()"
    with pytest.raises(ValueError):
        SourceIpHasher(b"short")


def test_an_attempt_never_keeps_the_code_and_is_coherent() -> None:
    plain = generate_code()
    attempt = EnrollmentAttempt(
        attempt_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        plant_id=uuid.uuid4(),
        node_id=uuid.uuid4(),
        presented_code_hash=presented_code_hash(plain),
        hardware_fingerprint="ab" * 32,
        software_version="1.4.0",
        contract_version="1.0.0",
        result=EnrollmentAttemptResult.ENROLLMENT_CODE_USED,
        attempted_at=T0,
        source_ip_hash="cd" * 32,
        correlation_id=uuid.uuid4(),
    )
    assert attempt.rejected and plain not in repr(attempt)
    with pytest.raises(ValueError, match="juntos"):
        EnrollmentAttempt(**{**vars_of(attempt), "plant_id": None})
    with pytest.raises(ValueError, match="aceptado"):
        EnrollmentAttempt(
            **{
                **vars_of(attempt),
                "plant_id": None,
                "node_id": None,
                "result": EnrollmentAttemptResult.ACCEPTED,
            }
        )
    with pytest.raises(ValueError, match="hexadecimales"):
        EnrollmentAttempt(**{**vars_of(attempt), "presented_code_hash": plain})


def vars_of(attempt: EnrollmentAttempt) -> dict[str, object]:
    return {name: getattr(attempt, name) for name in attempt.__dataclass_fields__}
