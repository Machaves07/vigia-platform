"""PR-LAZ-16: toda categoría de causa aceptada es de las tres; ninguna rama admite una persona.

business-logic-model.md de U-04 §11, C-PLA-26 (TASK-315; BR-LAZ-62, H-28, P7). Generadores
``cause_categories`` (las tres, sus variantes de forma, categorías que nombran a una persona o una
conducta y cualquier texto) y ``ATTRIBUTED_TEXT`` (el texto que el doble de ``person_attribution``
rechaza):

- **categoría** (sin base): ``checked_category`` acepta un valor **si y solo si** es exactamente
  ``process``, ``engineering`` u ``organization``, y devuelve ese mismo valor; cualquier otro es
  ``loop_cause_category_not_allowed``;
- **ramas del diagrama** (sin base): ``checked_branches`` sobre ramas generadas responde el código
  del **primer** fallo en el orden de la lista (categoría de la rama y después sus factores) o
  acepta; lo aceptado solo tiene categorías de las tres y ningún factor atribuye la causa a una
  persona;
- **el flujo real** (``integration``): con el ``RootCauseService`` real sobre PostgreSQL 16, un paso
  con categoría generada se acepta si y solo si es de las tres, y la fila nunca guarda otra.

Solo datos generados.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any, Final, Literal

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from tests.integration.conftest import PostgresEndpoint
from tests.loop_classification_support import classification_free_text
from tests.loop_root_cause_support import RootCauseStack, root_cause_stack
from tests.properties.laz.strategies.classification import BLOCKED_TEXT
from tests.properties.laz.strategies.root_cause import (
    ATTRIBUTED_TEXT,
    PERSON_LIKE_CATEGORIES,
    THREE,
    VALID_STATEMENTS,
    cause_categories,
)
from vigia_platform.loop.application.root_cause import (
    AnalysisRejected,
    AnalysisView,
    checked_branches,
    checked_category,
)
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.shared.context import Role

FREE_TEXT: Final = classification_free_text()
"""La política de producción de U-04 con el doble de ``person_attribution`` (sin base: la lista
inicial de vocabulario)."""
ORGANIZATION: Final = uuid.UUID("00000000-0000-4000-8000-000000000016")
FACTORS: Final = {
    "valid": "Trazado del pasillo sin separación física",
    "attributed": ATTRIBUTED_TEXT,
    "blocked": BLOCKED_TEXT,
}
type FactorKind = Literal["valid", "attributed", "blocked"]
CODE_OF: Final = {
    "attributed": LoopDetailCode.PERSON_ATTRIBUTION_DETECTED,
    "blocked": LoopDetailCode.BLOCKED_VOCABULARY_TERM,
}


@given(cause_categories())
def test_pr_laz_16_only_the_three_exact_categories_are_accepted(value: str) -> None:
    try:
        accepted = checked_category(value)
    except AnalysisRejected as rejected:
        assert rejected.detail_code is LoopDetailCode.CAUSE_CATEGORY_NOT_ALLOWED
        assert value not in THREE
        event("rechazada")
        return
    assert value in THREE
    assert accepted.value == value
    event(f"aceptada {value}")


@pytest.mark.parametrize("value", [*PERSON_LIKE_CATEGORIES, None, 1, ["process"], {"v": 1}])
def test_pr_laz_16_no_person_behaviour_or_non_text_category_is_accepted(value: Any) -> None:
    with pytest.raises(AnalysisRejected) as rejected:
        checked_category(value)
    assert rejected.value.detail_code is LoopDetailCode.CAUSE_CATEGORY_NOT_ALLOWED


_BRANCHES = st.lists(
    st.tuples(
        st.one_of(st.sampled_from(THREE), cause_categories()),
        st.lists(
            st.sampled_from(("valid", "valid", "attributed", "blocked")), min_size=1, max_size=3
        ),
    ),
    max_size=6,
)


def _expected(branches: list[tuple[str, list[FactorKind]]]) -> LoopDetailCode | None:
    """El primer fallo en el orden de la lista: la categoría de la rama, después sus factores."""
    for category, factors in branches:
        if category not in THREE:
            return LoopDetailCode.CAUSE_CATEGORY_NOT_ALLOWED
        for kind in factors:
            if kind != "valid":
                return CODE_OF[kind]
    return None


@given(_BRANCHES)
def test_pr_laz_16_no_branch_admits_a_person(
    branches: list[tuple[str, list[FactorKind]]],
) -> None:
    values = [
        {
            "branch_category": category,
            "factors": [{"text": FACTORS[kind], "evidence_refs": []} for kind in factors],
        }
        for category, factors in branches
    ]
    expected = _expected(branches)
    try:
        accepted = checked_branches(FREE_TEXT, values, ORGANIZATION)
    except AnalysisRejected as rejected:
        assert rejected.detail_code is expected
        event(f"rechazada {expected}")
        return
    assert expected is None
    assert [branch.category.value for branch in accepted] == [c for c, _ in branches]
    assert all(branch.category.value in THREE for branch in accepted)
    assert all(factor.text == FACTORS["valid"] for branch in accepted for factor in branch.factors)
    event("aceptadas")


# --- el flujo real ------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[RootCauseStack]:
    with root_cause_stack(postgres_endpoint, "pr_laz_16") as built:
        yield built


@pytest.mark.integration
def test_pr_laz_16_the_real_flow_keeps_only_the_three(stack: RootCauseStack) -> None:
    coordinator = stack.member(stack.world.a, Role.COORDINATOR_SST)
    context = stack.context(coordinator)
    analysis_id = uuid.UUID(stack.with_steps(coordinator, 1))

    @settings(
        max_examples=40,
        deadline=None,
        database=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(cause_categories())
    def run(value: str) -> None:
        before = stack.analysis_row(analysis_id)["why_steps"]
        try:
            view: AnalysisView | LoopDetailCode = stack.run(
                stack.root_cause.put_step(
                    context,
                    analysis_id,
                    1,
                    statement=VALID_STATEMENTS[0],
                    evidence_refs=[],
                    category=value,
                )
            )
        except AnalysisRejected as rejected:
            view = rejected.detail_code
        after = stack.analysis_row(analysis_id)["why_steps"]
        if value in THREE:
            assert isinstance(view, AnalysisView)
            assert after[0]["category"] == value
        else:
            assert view is LoopDetailCode.CAUSE_CATEGORY_NOT_ALLOWED
            assert after == before
        assert all(step.get("category") in (None, *THREE) for step in after)

    run()
