"""PR-LAZ-17: firmar exige nombre y licencia, lo pendiente se enumera y lo firmado es lo aceptado.

business-logic-model.md de U-04 §11, C-PLA-26 (TASK-315; BR-LAZ-64, 65; P8). Dos niveles:

- **dominio** (sin base, ``loop.domain.root_cause.sign``): para cualquier combinación de porqués,
  categoría, enunciado, conclusión, nombre y licencia, el resultado es ``Pending`` **si y solo si**
  falta algo de la lista cerrada, y entonces ``missing`` es exactamente lo que falta, en el orden de
  la lista; sin pendientes, sin nombre o sin licencia es ``signer_license_missing``; firmado,
  ``record_content`` lleva cada texto **idéntico** al del análisis (igualdad exacta);
- **flujo real** (``integration``, ``analysis_flows``): con el ``RootCauseService`` real sobre
  PostgreSQL 16 y el escritor con los validadores de U-04, cada firma generada responde lo que dice
  el modelo; si falta algo, la fila queda ``pending_information`` con ``missing_information`` igual
  a la enumeración esperada y ningún registro; si falta la licencia, nada cambia; firmada, el
  registro ``root_cause_analysis`` lleva **exactamente** los textos que el coordinador vio (la forma
  NFC que devolvió cada paso) y los de su firma, con nombre y licencia.

Solo datos generados.
"""

from __future__ import annotations

import unicodedata
import uuid
from collections.abc import Iterator
from typing import Any, Final

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from tests.integration.conftest import PostgresEndpoint
from tests.loop_root_cause_support import FACTOR, LICENSE, RootCauseStack, root_cause_stack
from tests.properties.laz.strategies.root_cause import (
    THREE,
    VALID_STATEMENTS,
    AnalysisFlow,
    FinalPresence,
    analysis_flows,
)
from vigia_platform.loop.application.root_cause import AnalysisRejected, AnalysisView
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.domain.enums import AnalysisMissingItem, CauseCategory
from vigia_platform.loop.domain.root_cause import (
    Branch,
    Factor,
    GuidedAnalysis,
    Pending,
    RootCauseCode,
    RootCauseRejected,
    Signed,
    WhyStep,
    missing_information_es,
    record_content,
    sign,
)
from vigia_platform.shared.api.labels import PlatformLabels
from vigia_platform.shared.context import Role

LABELS: Final = PlatformLabels.load()
M = AnalysisMissingItem


def _label(item: AnalysisMissingItem) -> str:
    return LABELS.label("analysis_missing_item", item)


# --- dominio ------------------------------------------------------------------------------------

_NAMES = st.sampled_from(("Coordinación SST sintética", "", "   ", None))
_LICENSES = st.sampled_from((LICENSE, "", " ", None))
_TEXTS = st.one_of(st.none(), st.sampled_from(VALID_STATEMENTS), st.text(min_size=1, max_size=60))


@given(
    steps=st.integers(min_value=0, max_value=5),
    category=st.one_of(st.none(), st.sampled_from(THREE)),
    statement=_TEXTS,
    conclusion=_TEXTS,
    name=_NAMES,
    license_=_LICENSES,
)
def test_pr_laz_17_the_domain_signs_only_complete_and_licensed_analyses(
    steps: int,
    category: str | None,
    statement: str | None,
    conclusion: str | None,
    name: str | None,
    license_: str | None,
) -> None:
    analysis = GuidedAnalysis(
        steps=tuple(WhyStep(i + 1, VALID_STATEMENTS[i]) for i in range(steps))
    )
    branches = (Branch(CauseCategory.PROCESS, (Factor(FACTOR),)),)
    chosen = None if category is None else CauseCategory(category)
    expected_missing = tuple(
        item
        for item, absent in (
            (M.WHY_STEPS, steps == 0),
            (M.ROOT_CAUSE_CATEGORY, category is None),
            (M.ROOT_CAUSE_STATEMENT, statement is None),
            (M.CONCLUSION, conclusion is None),
        )
        if absent
    )
    try:
        outcome = sign(
            analysis,
            branches=branches,
            root_cause_category=chosen,
            root_cause_statement=statement,
            conclusion=conclusion,
            signer_name=name,
            professional_license=license_,
        )
    except RootCauseRejected as rejected:
        assert rejected.code is RootCauseCode.SIGNER_LICENSE_MISSING
        assert expected_missing == ()
        assert not (name or "").strip() or not (license_ or "").strip()
        event("sin nombre o sin licencia")
        return
    if isinstance(outcome, Pending):
        assert outcome.missing == expected_missing != ()
        assert outcome.analysis.state.value == "pending_information"
        text = missing_information_es(outcome.missing, _label)
        assert text == "Falta para firmar: " + "; ".join(map(_label, expected_missing)) + "."
        event(f"pendiente de {len(outcome.missing)}")
        return
    assert isinstance(outcome, Signed)
    assert expected_missing == ()
    assert (name or "").strip() and (license_ or "").strip()
    signer = {"user_id": str(uuid.uuid4()), "display_name": str(name), "license": str(license_)}
    content = record_content(
        outcome.analysis,
        analysis_id=uuid.uuid4(),
        subject_kind="finding",
        subject_id=uuid.uuid4(),
        anchor_record_id=uuid.uuid4(),
        signer=signer,
        signed_at="2026-10-18T00:00:00.000Z",
    )
    assert [step["statement"] for step in content["why_steps"]] == list(VALID_STATEMENTS[:steps])
    assert content["root_cause_statement"] == statement
    assert content["conclusion"] == conclusion
    assert content["root_cause_category"] == category
    assert content["cause_effect_branches"] == [
        {"branch_category": "process", "factors": [{"text": FACTOR, "evidence_refs": []}]}
    ]
    assert content["signer"] == signer
    event("firmado")


# --- el flujo real ------------------------------------------------------------------------------

EXAMPLES: Final = 25
"""Cada ejemplo inicia un análisis, escribe sus porqués y firma: varias transacciones reales."""


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[RootCauseStack]:
    with root_cause_stack(postgres_endpoint, "pr_laz_17") as built:
        yield built


def _final(presence: FinalPresence, text: str) -> str | None:
    return {"present": text, "absent": None, "blank": "   "}[presence]


def _expected(flow: AnalysisFlow) -> tuple[str, tuple[AnalysisMissingItem, ...]]:
    """El modelo de la firma, en el orden de la aplicación (BLM §4, paso 4)."""
    sign_ = flow.sign
    categories = [c for c in (sign_.root_cause_category, *sign_.branch_categories) if c is not None]
    if any(category not in THREE for category in categories):
        return LoopDetailCode.CAUSE_CATEGORY_NOT_ALLOWED.value, ()
    missing = tuple(
        item
        for item, absent in (
            (M.WHY_STEPS, not flow.statements),
            (M.ROOT_CAUSE_CATEGORY, sign_.root_cause_category is None),
            (M.ROOT_CAUSE_STATEMENT, sign_.root_cause_statement != "present"),
            (M.CONCLUSION, sign_.conclusion != "present"),
        )
        if absent
    )
    if missing:
        return LoopDetailCode.ANALYSIS_INFORMATION_INSUFFICIENT.value, missing
    if sign_.license != "present":
        return LoopDetailCode.SIGNER_LICENSE_MISSING.value, ()
    return "signed", ()


@pytest.mark.integration
def test_pr_laz_17_the_real_signature_is_exactly_what_the_coordinator_accepted(
    stack: RootCauseStack,
) -> None:
    coordinator = stack.member(stack.world.a, Role.COORDINATOR_SST)
    context = stack.context(coordinator)
    subject = stack.received()
    fsid = stack.finding_state_id(subject)

    @settings(
        max_examples=EXAMPLES,
        deadline=None,
        database=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(analysis_flows())
    def run(flow: AnalysisFlow) -> None:
        view: AnalysisView = stack.run(stack.root_cause.start(context, fsid))
        analysis_id = view.analysis_id
        for index, statement in enumerate(flow.statements, start=1):
            view = stack.run(
                stack.root_cause.put_step(
                    context,
                    analysis_id,
                    index,
                    statement=statement,
                    evidence_refs=[],
                    category=None,
                )
            )
            # Lo que el coordinador ve es la forma NFC de lo que escribió.
            assert view.analysis.steps[-1].statement == unicodedata.normalize("NFC", statement)
        seen = [step.statement for step in view.analysis.steps]
        sign_ = flow.sign
        statement = _final(sign_.root_cause_statement, flow.root_cause_statement)
        conclusion = _final(sign_.conclusion, flow.conclusion)
        branches: list[dict[str, Any]] = [
            {"branch_category": c, "factors": [{"text": FACTOR, "evidence_refs": []}]}
            for c in sign_.branch_categories
        ]
        before = stack.analysis_row(analysis_id)
        counts = stack.analysis_counts(stack.world.a)
        expected, missing = _expected(flow)
        try:
            signed: AnalysisView | str = stack.run(
                stack.root_cause.sign(
                    context,
                    analysis_id,
                    professional_license=_final(sign_.license, LICENSE),
                    root_cause_category=sign_.root_cause_category,
                    root_cause_statement=statement,
                    conclusion=conclusion,
                    cause_effect_branches=branches,
                )
            )
        except AnalysisRejected as rejected:
            signed = rejected.detail_code.value
        event(expected)
        row = stack.analysis_row(analysis_id)
        if isinstance(signed, str):
            assert signed == expected, flow
            assert stack.analysis_counts(stack.world.a) == counts
            if expected == LoopDetailCode.ANALYSIS_INFORMATION_INSUFFICIENT.value:
                assert row["state"] == "pending_information"
                assert row["missing_information"] == missing_information_es(missing, _label)
                assert row["record_id"] is None
            else:
                assert row == before
            return
        assert expected == "signed", flow
        assert stack.analysis_counts(stack.world.a) == (counts[0], counts[1] + 1, counts[2] + 1)
        (record,) = [
            r for r in stack.analysis_records(stack.world.a) if r["record_id"] == signed.record_id
        ]
        content = record["content"]
        assert [step["statement"] for step in content["why_steps"]] == seen
        assert content["root_cause_statement"] == unicodedata.normalize("NFC", str(statement))
        assert content["conclusion"] == unicodedata.normalize("NFC", str(conclusion))
        assert content["root_cause_category"] == sign_.root_cause_category
        assert content["cause_effect_branches"] == branches
        assert content["signer"]["professional_license"] == LICENSE
        assert content["signer"]["display_name"]
        assert content["signer"] == row["signer"]
        assert row["state"] == "signed" and row["missing_information"] is None

    run()
