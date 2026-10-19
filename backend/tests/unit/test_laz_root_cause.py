"""Los textos fijos del flujo guiado de causa raíz (TASK-315; BR-LAZ-77, 81).

- Las cinco preguntas (``QUESTIONS_ES``) y la enumeración de lo que falta
  (``missing_information_es`` con las etiquetas de ``analysis_missing_item``) pasan la política de
  texto libre **de producción** (la de las unidades registradas, con los dos validadores de U-04):
  el sistema nunca escribe un término bloqueado ni una atribución a persona.
- ``missing_information_es`` no enumera una lista vacía y ``record_content`` no existe para un
  análisis sin firmar o incompleto.

Sin base ni red.
"""

from __future__ import annotations

import itertools
import uuid
from typing import Final

import pytest

from vigia_platform.ledger.free_text import FreeTextField
from vigia_platform.loop.application.root_cause import (
    CONCLUSION_FIELD,
    STATEMENT_FIELD,
)
from vigia_platform.loop.domain.enums import AnalysisMissingItem, RootCauseAnalysisState
from vigia_platform.loop.domain.root_cause import (
    MAX_WHY_STEPS,
    QUESTIONS_ES,
    GuidedAnalysis,
    WhyStep,
    missing_information_es,
    record_content,
)
from vigia_platform.shared.api.labels import PlatformLabels
from vigia_platform.shared.runtime.units import free_text_registry, registered_units

POLICY: Final = free_text_registry(registered_units())
LABELS: Final = PlatformLabels.load()
MISSING_FIELD: Final = FreeTextField("root_cause_analysis", "/missing_information", 0, 1000)


def test_there_is_one_fixed_question_per_why() -> None:
    assert len(QUESTIONS_ES) == MAX_WHY_STEPS == 5
    assert len(set(QUESTIONS_ES)) == 5


@pytest.mark.parametrize("question", QUESTIONS_ES)
def test_the_fixed_questions_pass_the_production_policy(question: str) -> None:
    assert POLICY.apply(question, STATEMENT_FIELD) == question


def _subsets() -> list[tuple[AnalysisMissingItem, ...]]:
    items = tuple(AnalysisMissingItem)
    return [
        combo for size in range(1, len(items) + 1) for combo in itertools.combinations(items, size)
    ]


@pytest.mark.parametrize("missing", _subsets())
def test_every_missing_information_enumeration_passes_the_production_policy(
    missing: tuple[AnalysisMissingItem, ...],
) -> None:
    text = missing_information_es(missing, lambda item: LABELS.label("analysis_missing_item", item))
    assert POLICY.apply(text, MISSING_FIELD) == text
    assert POLICY.apply(text, CONCLUSION_FIELD) == text


def test_an_empty_enumeration_and_an_unsigned_record_do_not_exist() -> None:
    with pytest.raises(ValueError):
        missing_information_es((), str)
    draft = GuidedAnalysis(steps=(WhyStep(1, QUESTIONS_ES[0]),))
    signed_but_incomplete = GuidedAnalysis(state=RootCauseAnalysisState.SIGNED)
    for analysis in (draft, signed_but_incomplete):
        with pytest.raises(ValueError):
            record_content(
                analysis,
                analysis_id=uuid.uuid4(),
                subject_kind="finding",
                subject_id=uuid.uuid4(),
                anchor_record_id=uuid.uuid4(),
                signer={},
                signed_at="2026-10-18T00:00:00.000Z",
            )
