"""Guardas puras del acuerdo de uso (TASK-212; BR-GOB-25 a 29; ``catalog.domain.agreements``).

- **BR-GOB-29**: para cada una de las 15 combinaciones no vacías de guardas que faltan, la primera
  en el orden montaje, acta, firmas y política de planta determina el error (tabla y propiedad).
- **BR-GOB-25**: política con ``minimum`` en sus bordes (2, 3, 32, 33) y sin ``copasst``.
- **Firmantes** (BR-GOB-26, 31): cada guarda del alta en su orden, con el borde de cada una.
- **Confirmación**: origen por rol y firmas completas solo con el rol con el que se espera.
- **Registro**: ``use_agreement_signed`` pasa el modelo estricto del tipo (DE §5), sin nombres.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import itertools
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Final

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vigia_platform.catalog.domain.agreements import (
    DISPLAY_NAME_MAX,
    MAX_SIGNATORIES,
    AgreementConfirmation,
    AgreementRuleViolated,
    AgreementViolation,
    ApprovalFacts,
    Signatory,
    SignatoryPolicy,
    UseAgreement,
    check_policy,
    check_signatories,
    confirmation_origin,
    first_missing,
    signatures_complete,
    use_agreement_signed_content,
)
from vigia_platform.catalog.domain.enums import AgreementStatus, ConfirmationOrigin
from vigia_platform.catalog.record_types import UseAgreementSigned
from vigia_platform.shared.context import Role

T0: Final = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
GUARDS: Final = (
    ("mounting_approved", AgreementViolation.MOUNTING_GATE_PENDING),
    ("commissioning_record_closed", AgreementViolation.COMMISSIONING_RECORD_MISSING),
    ("signatures_complete", AgreementViolation.SIGNATURES_INCOMPLETE),
    ("plant_policy_loaded", AgreementViolation.PLANT_POLICY_MISSING),
)
MISSING_COMBINATIONS: Final = [
    frozenset(combo)
    for size in range(1, len(GUARDS) + 1)
    for combo in itertools.combinations(range(len(GUARDS)), size)
]


def _uuid7(n: int) -> uuid.UUID:
    # Un UUID v7 sintético (versión 7, variante RFC 4122) con un contador como aleatorio.
    return uuid.UUID(int=(0x0192_0000_0000_7000_8000_0000_0000_0000 | n))


def _facts(missing: frozenset[int]) -> ApprovalFacts:
    return ApprovalFacts(**{name: i not in missing for i, (name, _) in enumerate(GUARDS)})


def _violation(error: pytest.ExceptionInfo[AgreementRuleViolated]) -> AgreementViolation:
    return error.value.violation


# --- BR-GOB-29 ----------------------------------------------------------------------------------


def test_there_are_exactly_15_non_empty_combinations() -> None:
    assert len(MISSING_COMBINATIONS) == 15
    assert len(set(MISSING_COMBINATIONS)) == 15


@pytest.mark.parametrize(
    "missing", MISSING_COMBINATIONS, ids=lambda m: "+".join(GUARDS[i][0] for i in sorted(m))
)
def test_the_first_missing_guard_in_br_gob_29_order_decides(missing: frozenset[int]) -> None:
    expected = GUARDS[min(missing)][1]
    assert first_missing(_facts(missing)) is expected


def test_with_the_four_guards_nothing_is_missing() -> None:
    assert first_missing(_facts(frozenset())) is None


@given(st.tuples(st.booleans(), st.booleans(), st.booleans(), st.booleans()))
def test_first_missing_is_the_first_false_in_order(values: tuple[bool, bool, bool, bool]) -> None:
    facts = ApprovalFacts(*values)
    falses = [violation for (_, violation), ok in zip(GUARDS, values, strict=True) if not ok]
    assert first_missing(facts) == (falses[0] if falses else None)


def test_only_a_true_bool_satisfies_a_guard() -> None:
    # Un valor «verdadero» que no es True (1, "sí") no abre la compuerta: fallo cerrado.
    assert first_missing(ApprovalFacts(1, True, True, True)) is (  # type: ignore[arg-type]
        AgreementViolation.MOUNTING_GATE_PENDING
    )


# --- BR-GOB-25: política de firmantes -------------------------------------------------------------


@pytest.mark.parametrize("minimum", [3, 4, MAX_SIGNATORIES])
def test_a_policy_at_its_edges_is_accepted(minimum: int) -> None:
    roles = check_policy([Role.COORDINATOR_SST, Role.COPASST], minimum)
    assert roles == (Role.COORDINATOR_SST, Role.COPASST)


@pytest.mark.parametrize("minimum", [2, 0, -1])
def test_fewer_than_three_is_rejected(minimum: int) -> None:
    with pytest.raises(AgreementRuleViolated) as error:
        check_policy([Role.COPASST], minimum)
    assert _violation(error) is AgreementViolation.FEWER_THAN_THREE


@pytest.mark.parametrize("roles", [[], [Role.COORDINATOR_SST, Role.PLANT_MANAGER]])
def test_without_copasst_the_policy_is_workers_representation_missing(roles: list[Role]) -> None:
    with pytest.raises(AgreementRuleViolated) as error:
        check_policy(roles, 3)
    assert _violation(error) is AgreementViolation.WORKERS_REPRESENTATION_MISSING


def test_fewer_than_three_comes_before_the_missing_copasst() -> None:
    with pytest.raises(AgreementRuleViolated) as error:
        check_policy([Role.COORDINATOR_SST], 2)
    assert _violation(error) is AgreementViolation.FEWER_THAN_THREE


@pytest.mark.parametrize(
    ("roles", "minimum"),
    [
        ([Role.COPASST, Role.COPASST], 3),  # repetido
        ([Role.COPASST], MAX_SIGNATORIES + 1),  # sobre el tope
        ([Role.COPASST], True),  # bool no es entero
    ],
)
def test_an_incoherent_policy_is_a_value_error(roles: list[Role], minimum: int) -> None:
    with pytest.raises(ValueError):
        check_policy(roles, minimum)


def test_an_unknown_role_is_a_value_error() -> None:
    with pytest.raises(ValueError):
        check_policy(["copasst", "jefe_de_turno"], 3)  # type: ignore[list-item]


# --- Alta del acuerdo: firmantes ------------------------------------------------------------------


def _policy(roles: tuple[Role, ...], minimum: int = 3) -> SignatoryPolicy:
    return SignatoryPolicy(
        organization_id=uuid.uuid4(),
        plant_id=uuid.uuid4(),
        required_roles=roles,
        minimum=minimum,
        updated_by=uuid.uuid4(),
        updated_at=T0,
    )


POLICY: Final = _policy((Role.COORDINATOR_SST, Role.PLANT_MANAGER, Role.COPASST))


def _signers(*roles: Role) -> tuple[Signatory, ...]:
    return tuple(Signatory(role, uuid.uuid4()) for role in roles)


def _holders(signers: tuple[Signatory, ...]) -> set[tuple[uuid.UUID, Role]]:
    return {(s.user_id, s.role) for s in signers}


THREE: Final = (Role.COORDINATOR_SST, Role.PLANT_MANAGER, Role.COPASST)


def test_three_signers_of_the_policy_with_their_roles_are_accepted() -> None:
    signers = _signers(*THREE)
    assert check_signatories(signers, POLICY, _holders(signers)) == signers


def test_without_a_plant_policy_any_role_is_not_in_policy() -> None:
    signers = _signers(*THREE)
    with pytest.raises(AgreementRuleViolated) as error:
        check_signatories(signers, None, _holders(signers))
    assert _violation(error) is AgreementViolation.SIGNATORY_ROLE_NOT_IN_POLICY


def test_a_role_outside_the_policy_is_not_in_policy() -> None:
    signers = _signers(Role.COORDINATOR_SST, Role.LINE_MANAGER, Role.COPASST)
    with pytest.raises(AgreementRuleViolated) as error:
        check_signatories(signers, POLICY, _holders(signers))
    assert _violation(error) is AgreementViolation.SIGNATORY_ROLE_NOT_IN_POLICY


def test_a_user_without_that_role_on_the_zone_is_a_mismatch() -> None:
    signers = _signers(*THREE)
    held = _holders(signers)
    # El primero tiene otro rol (no el declarado) sobre la zona.
    held.discard((signers[0].user_id, signers[0].role))
    held.add((signers[0].user_id, Role.PLANT_MANAGER))
    with pytest.raises(AgreementRuleViolated) as error:
        check_signatories(signers, POLICY, held)
    assert _violation(error) is AgreementViolation.SIGNATORY_USER_ROLE_MISMATCH


def test_without_a_copasst_signer_the_agreement_lacks_workers_representation() -> None:
    signers = _signers(Role.COORDINATOR_SST, Role.PLANT_MANAGER, Role.PLANT_MANAGER)
    with pytest.raises(AgreementRuleViolated) as error:
        check_signatories(signers, POLICY, _holders(signers))
    assert _violation(error) is AgreementViolation.WORKERS_REPRESENTATION_MISSING


@pytest.mark.parametrize(("count", "minimum"), [(2, 3), (3, 4), (0, 3)])
def test_fewer_signers_than_the_minimum_is_fewer_than_three(count: int, minimum: int) -> None:
    roles = (Role.COPASST, *(Role.COORDINATOR_SST for _ in range(max(count - 1, 0))))[:count]
    signers = _signers(*roles)
    with pytest.raises(AgreementRuleViolated) as error:
        check_signatories(signers, _policy(THREE, minimum), _holders(signers))
    expected = (
        AgreementViolation.WORKERS_REPRESENTATION_MISSING
        if count == 0
        else AgreementViolation.FEWER_THAN_THREE
    )
    assert _violation(error) is expected


def test_exactly_the_minimum_is_accepted() -> None:
    signers = _signers(Role.COPASST, Role.COORDINATOR_SST, Role.COORDINATOR_SST, Role.COPASST)
    assert check_signatories(signers, _policy(THREE, 4), _holders(signers)) == signers


def test_the_role_guard_comes_before_the_user_guard() -> None:
    signers = _signers(Role.LINE_MANAGER, Role.COORDINATOR_SST, Role.COPASST)
    with pytest.raises(AgreementRuleViolated) as error:
        check_signatories(signers, POLICY, set())
    assert _violation(error) is AgreementViolation.SIGNATORY_ROLE_NOT_IN_POLICY


def test_a_repeated_user_or_too_many_signers_is_a_value_error() -> None:
    user = uuid.uuid4()
    repeated = (
        Signatory(Role.COPASST, user),
        Signatory(Role.COORDINATOR_SST, user),
        Signatory(Role.PLANT_MANAGER, uuid.uuid4()),
    )
    with pytest.raises(ValueError):
        check_signatories(repeated, POLICY, _holders(repeated))
    many = _signers(*(Role.COPASST for _ in range(MAX_SIGNATORIES + 1)))
    with pytest.raises(ValueError):
        check_signatories(many, POLICY, _holders(many))
    at_the_top = _signers(*(Role.COPASST for _ in range(MAX_SIGNATORIES)))
    assert len(check_signatories(at_the_top, POLICY, _holders(at_the_top))) == MAX_SIGNATORIES


# --- Confirmación ---------------------------------------------------------------------------------


@pytest.mark.parametrize("role", list(Role))
def test_only_copasst_confirms_from_transparency(role: Role) -> None:
    expected = (
        ConfirmationOrigin.TRANSPARENCY if role is Role.COPASST else ConfirmationOrigin.MANAGEMENT
    )
    assert confirmation_origin(role) is expected


def _agreement(signers: tuple[Signatory, ...], **changes: object) -> UseAgreement:
    fields: dict[str, object] = {
        "agreement_id": _uuid7(1),
        "organization_id": uuid.uuid4(),
        "plant_id": uuid.uuid4(),
        "zone_id": uuid.uuid4(),
        "status": AgreementStatus.PENDING_SIGNATURES,
        "signatories": signers,
        "document_ref": None,
        "replaces_agreement_id": None,
        "created_by": uuid.uuid4(),
        "created_at": T0,
    }
    fields.update(changes)
    return UseAgreement(**fields)  # type: ignore[arg-type]


def _confirmations(
    agreement: UseAgreement, signers: tuple[Signatory, ...], role: Role | None = None
) -> tuple[AgreementConfirmation, ...]:
    return tuple(
        AgreementConfirmation(
            agreement_id=agreement.agreement_id,
            user_id=s.user_id,
            organization_id=agreement.organization_id,
            plant_id=agreement.plant_id,
            role_in_use=role or s.role,
            confirmed_at=T0 + timedelta(minutes=i),
            origin=confirmation_origin(role or s.role),
        )
        for i, s in enumerate(signers)
    )


def test_signatures_are_complete_only_with_every_expected_signer_and_role() -> None:
    signers = _signers(*THREE)
    agreement = _agreement(signers)
    confirmations = _confirmations(agreement, signers)
    assert signatures_complete(signers, confirmations)
    for missing in range(len(signers)):
        partial = confirmations[:missing] + confirmations[missing + 1 :]
        assert not signatures_complete(signers, partial)
    # Mismo usuario, otro rol: no es la firma esperada.
    assert not signatures_complete(signers, _confirmations(agreement, signers, Role.ADMINISTRATOR))
    assert not signatures_complete(signers, ())


def test_expected_finds_the_signer_and_nobody_else() -> None:
    signers = _signers(*THREE)
    agreement = _agreement(signers)
    assert agreement.expected(signers[2].user_id) == signers[2]
    assert agreement.expected(uuid.uuid4()) is None


@pytest.mark.parametrize(
    ("length", "kept"), [(1, 1), (119, 119), (120, 120), (121, 120), (500, 120)]
)
def test_the_projection_keeps_at_most_120_characters_of_the_name(length: int, kept: int) -> None:
    # DE §2.8 (VIG-179, menor de la revisión de VIG-149): signatories[].display_name ≤ 120.
    name = "ñ" * length
    signatory = Signatory(Role.COPASST, uuid.uuid4(), name)
    assert signatory.display_name == name[:kept]
    assert DISPLAY_NAME_MAX == 120
    assert signatory.as_json()["display_name"] == name[:kept]
    assert Signatory(Role.COPASST, uuid.uuid4()).display_name is None


# --- Registro use_agreement_signed ----------------------------------------------------------------


def test_the_signed_record_passes_its_strict_model_and_carries_no_names() -> None:
    signers = tuple(Signatory(role, uuid.uuid4(), "Nombre sintético") for role in THREE)
    agreement = _agreement(signers, replaces_agreement_id=_uuid7(2))
    content = use_agreement_signed_content(agreement, _confirmations(agreement, signers))
    # El escritor valida el contenido como JSON (``model_validate_json``, ``ledger.registry``).
    model = UseAgreementSigned.model_validate_json(json.dumps(content))
    assert model.agreement_id == str(agreement.agreement_id)
    assert "Nombre sintético" not in str(content)
    assert [c["origin"] for c in content["confirmations"]] == [
        "management",
        "management",
        "transparency",
    ]
    assert content["replaces_agreement_id"] == str(_uuid7(2))
    assert "document_ref" not in content
