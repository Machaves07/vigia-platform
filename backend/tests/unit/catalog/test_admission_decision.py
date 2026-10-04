"""Decisión pura de la prueba de admisión y sus bordes (TASK-207; BR-GOB-13, 17, 18; H-43).

Sin base ni hora del sistema (PAT-GOB-MAN): las ocho combinaciones de respuestas con su
resultado y su criterio fallido (el primero falso en el orden ``standard``, ``remedy``,
``subject``), los tipos que no son ``bool``, la coherencia de la entidad y del contenido del
registro ``standard_admission_test``, el cuerpo de la ruta (lista cerrada de familias, sin
parámetros de más: nada admite sin las tres respuestas) y el cursor del listado.
"""

from __future__ import annotations

import itertools
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError
from vigia_contracts.models.enumerations import PredicateFamily

from tests.factories import uuid7
from vigia_platform.catalog.adapters.http.admissions import (
    AdmissionBody,
    decode_cursor,
    encode_cursor,
)
from vigia_platform.catalog.adapters.postgres.admission_repository import AdmissionCursor
from vigia_platform.catalog.application.admission import (
    AdmissionRequest,
    CatalogRejected,
    record_content,
)
from vigia_platform.catalog.detail_codes import CatalogDetailCode
from vigia_platform.catalog.domain.admission import (
    CRITERIA_ORDER,
    AdmissionAnswers,
    AdmissionDecision,
    FamilyAdmission,
    decide_admission,
)
from vigia_platform.catalog.domain.enums import AdmissionCriterion, AdmissionResult
from vigia_platform.catalog.record_types import StandardAdmissionTest
from vigia_platform.identity.authz.matrix import MATRIX, PermissionKey
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.context import Role

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
YES = {"standard": True, "remedy": True, "subject": True}

_S, _R, _U = AdmissionCriterion.STANDARD, AdmissionCriterion.REMEDY, AdmissionCriterion.SUBJECT
EXPECTED = {
    (True, True, True): None,
    (False, True, True): _S,
    (True, False, True): _R,
    (True, True, False): _U,
    (False, False, True): _S,
    (False, True, False): _S,
    (True, False, False): _R,
    (False, False, False): _S,
}
"""Las ocho combinaciones: ``None`` es admitida; si no, el criterio fallido esperado."""


@pytest.mark.parametrize("answers", list(itertools.product((True, False), repeat=3)))
def test_each_combination_decides_its_result_and_first_failed_criterion(
    answers: tuple[bool, bool, bool],
) -> None:
    decision = decide_admission(AdmissionAnswers(*answers))
    expected = EXPECTED[answers]
    if expected is None:
        assert decision == AdmissionDecision(AdmissionResult.ADMITTED)
        assert decision.admitted
    else:
        assert decision == AdmissionDecision(AdmissionResult.REJECTED, expected)
        assert not decision.admitted


def test_the_order_of_criteria_is_standard_remedy_subject() -> None:
    assert CRITERIA_ORDER == (_S, _R, _U)
    assert [c.value for c in AdmissionCriterion] == ["standard", "remedy", "subject"]


@pytest.mark.parametrize("value", [1, 0, "true", "false", None, 1.0])
@pytest.mark.parametrize("field", ["standard", "remedy", "subject"])
def test_answers_must_be_real_booleans(field: str, value: Any) -> None:
    with pytest.raises(TypeError, match=field):
        AdmissionAnswers(**{**YES, field: value})


def test_decide_rejects_anything_but_answers() -> None:
    with pytest.raises(TypeError):
        decide_admission(YES)  # type: ignore[arg-type]


def test_a_decision_carries_a_criterion_iff_rejected() -> None:
    with pytest.raises(ValueError):
        AdmissionDecision(AdmissionResult.ADMITTED, _S)
    with pytest.raises(ValueError):
        AdmissionDecision(AdmissionResult.REJECTED)


def _entity(**changes: Any) -> FamilyAdmission:
    values: dict[str, Any] = {
        "admission_id": uuid7(),
        "organization_id": uuid.uuid4(),
        "plant_id": uuid.uuid4(),
        "family": PredicateFamily.COEXISTENCE,
        "answers": AdmissionAnswers(True, True, True),
        "justification_es": None,
        "result": AdmissionResult.ADMITTED,
        "failed_criterion": None,
        "evaluated_by": uuid.uuid4(),
        "role_in_use": Role.ADMINISTRATOR,
        "evaluated_at": T0,
        "ledger_record_id": uuid.uuid4(),
    }
    values.update(changes)
    return FamilyAdmission(**values)


def test_the_entity_rejects_incoherent_results() -> None:
    assert _entity().admitted
    no = AdmissionAnswers(True, False, True)
    with pytest.raises(ValueError):
        _entity(answers=no)  # admitted con una negativa
    with pytest.raises(ValueError):
        _entity(result=AdmissionResult.REJECTED, failed_criterion=_R)  # rechazo con tres sí
    with pytest.raises(ValueError):
        _entity(answers=no, result=AdmissionResult.REJECTED, failed_criterion=_S)  # «sí»
    with pytest.raises(ValueError):
        _entity(answers=no, result=AdmissionResult.REJECTED)  # rechazo sin criterio
    with pytest.raises(ValueError):
        _entity(evaluated_at=T0.replace(tzinfo=None))
    rejected = _entity(answers=no, result=AdmissionResult.REJECTED, failed_criterion=_R)
    assert not rejected.admitted


@pytest.mark.parametrize("answers", list(itertools.product((True, False), repeat=3)))
def test_the_record_content_of_each_decision_is_valid(answers: tuple[bool, bool, bool]) -> None:
    decision = decide_admission(AdmissionAnswers(*answers))
    entity = _entity(
        answers=AdmissionAnswers(*answers),
        result=decision.result,
        failed_criterion=decision.failed_criterion,
        justification_es="Justificación sintética",
    )
    model = StandardAdmissionTest.model_validate_json(json.dumps(record_content(entity)))
    assert model.result is decision.result
    assert model.failed_criterion is decision.failed_criterion


# --- El cuerpo de la ruta: nada admite sin las tres respuestas (BR-GOB-17, 18) ---------------


def test_the_body_accepts_exactly_the_contract_families() -> None:
    for family in PredicateFamily:
        body = AdmissionBody.model_validate({"family": family.value, "answers": YES})
        assert body.family is family
    assert {f.value for f in PredicateFamily} == {
        "coexistence",
        "guard_bypass",
        "dwell",
        "startup_transition",
    }


@pytest.mark.parametrize(
    "body",
    [
        {"family": "productivity", "answers": YES},
        {"family": "cycle_time", "answers": YES},
        {"family": "", "answers": YES},
        {"family": "coexistence"},
        {"family": "coexistence", "answers": {"standard": True, "remedy": True}},
        {"family": "coexistence", "answers": {**YES, "standard": "yes"}},
        {"family": "coexistence", "answers": {**YES, "subject": 1}},
        {"family": "coexistence", "answers": YES, "force": True},
        {"family": "coexistence", "answers": YES, "result": "admitted"},
        {"family": "coexistence", "answers": {**YES, "bypass": True}},
        {"family": "coexistence", "answers": YES, "justification_es": 7},
    ],
)
def test_the_body_rejects_unknown_families_extra_parameters_and_non_booleans(
    body: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        AdmissionBody.model_validate(body)


def test_no_permission_key_admits_outside_the_catalog_keys() -> None:
    # Ninguna clave «salta» la prueba (BR-GOB-18): admitir exige catalog.manage y solo el
    # administrador la tiene (matriz de U-02).
    names = {key.value for key in PermissionKey}
    assert not {n for n in names if "admission" in n or "admit" in n}
    holders = {role for role, keys in MATRIX.items() if PermissionKey.CATALOG_MANAGE in keys}
    assert holders == {Role.ADMINISTRATOR}


def test_the_request_keeps_a_closed_family_and_real_answers() -> None:
    request = AdmissionRequest(family="dwell", answers=AdmissionAnswers(True, True, True))  # type: ignore[arg-type]
    assert request.family is PredicateFamily.DWELL
    with pytest.raises(ValueError):
        AdmissionRequest(family="productivity", answers=AdmissionAnswers(True, True, True))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        AdmissionRequest(family=PredicateFamily.DWELL, answers=YES)  # type: ignore[arg-type]


def test_catalog_rejections_travel_under_their_u02_code() -> None:
    assert CatalogRejected(CatalogDetailCode.ADMISSION_REJECTED).api_code is (
        ApiErrorCode.INVALID_REQUEST
    )
    assert CatalogRejected(CatalogDetailCode.FAMILY_ALREADY_ADMITTED).api_code is (
        ApiErrorCode.CONFLICT
    )
    assert CatalogRejected(CatalogDetailCode.FREE_TEXT_REJECTED).api_code is (
        ApiErrorCode.INVALID_REQUEST
    )


# --- Cursor del listado --------------------------------------------------------------------------


def test_the_cursor_round_trips() -> None:
    cursor = AdmissionCursor(T0.replace(microsecond=123456), uuid.uuid4())
    assert decode_cursor(encode_cursor(cursor)) == cursor


@pytest.mark.parametrize(
    "value",
    [
        "",
        "no-es-un-cursor!",
        "A" * 129,
        "bm9uZQ",  # «none»: sin separador
        "MjAyNi0xMC0wM1QxMjowMDowMHxub3QtdXVpZA",  # marca válida, identificador no
        "MjAyNi0xMC0wM1QxMjowMDowMHw3ZjJjNmY1Yy0wMDAwLTcwMDAtODAwMC0wMDAwMDAwMDAwMDA",  # sin zona
    ],
)
def test_a_bad_cursor_is_invalid_request(value: str) -> None:
    with pytest.raises(ApiError) as caught:
        decode_cursor(value)
    assert caught.value.code is ApiErrorCode.INVALID_REQUEST
