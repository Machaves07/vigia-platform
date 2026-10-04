"""PR-GOB-11: admisión si y solo si las tres respuestas son afirmativas (TASK-207; BR-GOB-13).

Invariante sobre ``decide_admission`` con el generador ``admission_answers`` (perfil ``ci``: la
semilla fija y la de la sesión, ``tests/conftest.py``):

- ``admitted`` si y solo si ``standard``, ``remedy`` y ``subject`` valen ``true``;
- con alguna ``false``, ``failed_criterion`` es el **primer** criterio falso en el orden
  ``standard``, ``remedy``, ``subject``, y esa respuesta es negativa;
- el contenido del registro ``standard_admission_test`` que se compone con la decisión pasa el
  modelo estricto del tipo (que vuelve a exigir la coherencia de resultado y criterio).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from hypothesis import given
from hypothesis import strategies as st
from vigia_contracts.models.enumerations import PredicateFamily

from tests.factories import uuid7
from tests.properties.gob.strategies.admission import admission_answers
from vigia_platform.catalog.application.admission import record_content
from vigia_platform.catalog.domain.admission import (
    AdmissionAnswers,
    FamilyAdmission,
    decide_admission,
)
from vigia_platform.catalog.domain.enums import AdmissionCriterion, AdmissionResult
from vigia_platform.catalog.record_types import StandardAdmissionTest
from vigia_platform.shared.context import Role

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
ADMISSION_ID = uuid7()
IDS = {n: uuid.uuid4() for n in range(2, 6)}
"""Identificadores fijos: la generación depende solo de lo dibujado."""


@given(admission_answers())
def test_pr_gob_11_admitted_iff_the_three_answers_are_true(answers: AdmissionAnswers) -> None:
    decision = decide_admission(answers)
    all_true = answers.standard and answers.remedy and answers.subject
    assert (decision.result is AdmissionResult.ADMITTED) == all_true
    assert decision.admitted == all_true
    if all_true:
        assert decision.failed_criterion is None
        return
    negatives = [c for c in AdmissionCriterion if not answers.answer(c)]
    assert decision.result is AdmissionResult.REJECTED
    assert decision.failed_criterion is negatives[0]
    assert not answers.answer(decision.failed_criterion)


@given(admission_answers(), st.sampled_from(PredicateFamily))
def test_pr_gob_11_the_record_content_passes_the_strict_model(
    answers: AdmissionAnswers, family: PredicateFamily
) -> None:
    decision = decide_admission(answers)
    admission = FamilyAdmission(
        admission_id=ADMISSION_ID,
        organization_id=IDS[2],
        plant_id=IDS[3],
        family=family,
        answers=answers,
        justification_es=None,
        result=decision.result,
        failed_criterion=decision.failed_criterion,
        evaluated_by=IDS[4],
        role_in_use=Role.ADMINISTRATOR,
        evaluated_at=T0,
        ledger_record_id=IDS[5],
    )
    model = StandardAdmissionTest.model_validate_json(json.dumps(record_content(admission)))
    assert (model.result, model.failed_criterion) == (decision.result, decision.failed_criterion)
    assert model.answers.model_dump() == answers.as_dict()
