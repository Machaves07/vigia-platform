"""PR-LAZ-18 (con estado, PBT-06): el flujo guiado nunca deja un paso completo con el anterior
incompleto ni más de cinco porqués.

business-logic-model.md de U-04 §11, C-PLA-26 (TASK-315; BR-LAZ-61). Tras cualquier secuencia de
``analysis_step_commands`` (avances, retrocesos, ediciones, pasos fuera de orden o de rango,
enunciados inválidos, categorías de cualquier forma e intentos de firma):

- los porqués guardados son exactamente 1, 2, …, *k* con *k* ≤ 5, cada uno completo (su enunciado
  pasó la política) y cada categoría es de las tres;
- el resultado de cada orden es el del **modelo** (una lista de enunciados): el código de cada
  rechazo, en el orden de la aplicación, y el estado de cada aceptación;
- un análisis firmado ya no cambia: sus porqués son los del registro.

Dos máquinas con las mismas reglas: una sobre el dominio puro (``loop.domain.root_cause``, sin
base) y otra sobre el ``RootCauseService`` real contra PostgreSQL 16 (``integration``), que además
compara cada vez la fila de ``loop.root_cause_analysis`` con el modelo.

Solo datos generados.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Iterator
from typing import Any, ClassVar, Final

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import seed as hypothesis_seed
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule, run_state_machine_as_test

from tests.conftest import _seeds_for_profile
from tests.integration.conftest import PostgresEndpoint
from tests.loop_root_cause_support import (
    CONCLUSION,
    FACTOR,
    LICENSE,
    ROOT_STATEMENT,
    RootCauseStack,
    root_cause_stack,
)
from tests.properties.laz.strategies.root_cause import (
    THREE,
    AnalysisCommand,
    PutStepCommand,
    SignCommand,
    analysis_step_commands,
)
from vigia_platform.loop.application.root_cause import (
    AnalysisRejected,
    AnalysisRequestInvalid,
    AnalysisView,
)
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.domain.enums import RootCauseAnalysisState
from vigia_platform.loop.domain.root_cause import (
    MAX_WHY_STEPS,
    Branch,
    Factor,
    GuidedAnalysis,
    Pending,
    RootCauseRejected,
    WhyStep,
    cause_category,
    put_step,
    sign,
    steps_in_order,
)
from vigia_platform.shared.context import Role

INVALID: Final = "invalid_request"
SERVICE_EXAMPLES: Final = 10
"""Secuencias por semilla contra la base: cada orden es una transacción real."""


@dataclasses.dataclass
class Model:
    """Lo que el flujo guiado debería guardar: los enunciados y si ya se firmó."""

    statements: list[tuple[str, str | None]] = dataclasses.field(default_factory=list)
    signed: bool = False

    def put(self, command: PutStepCommand) -> str:
        """El resultado esperado de escribir un paso, en el orden de la aplicación."""
        if self.signed:
            return LoopDetailCode.TRANSITION_NOT_ALLOWED.value
        if not 1 <= command.step_index <= min(len(self.statements) + 1, MAX_WHY_STEPS):
            return LoopDetailCode.ANALYSIS_STEP_OUT_OF_ORDER.value
        if command.kind == "short":
            return INVALID
        if command.kind == "blocked":
            return LoopDetailCode.BLOCKED_VOCABULARY_TERM.value
        if command.kind == "attributed":
            return LoopDetailCode.PERSON_ATTRIBUTION_DETECTED.value
        if command.category is not None and command.category not in THREE:
            return LoopDetailCode.CAUSE_CATEGORY_NOT_ALLOWED.value
        entry = (command.statement, command.category)
        if command.step_index == len(self.statements) + 1:
            self.statements.append(entry)
        else:
            self.statements[command.step_index - 1] = entry
        return "accepted"

    def sign(self, command: SignCommand) -> str:
        if self.signed:
            return LoopDetailCode.TRANSITION_NOT_ALLOWED.value
        categories = [
            c for c in (command.root_cause_category, *command.branch_categories) if c is not None
        ]
        if any(category not in THREE for category in categories):
            return LoopDetailCode.CAUSE_CATEGORY_NOT_ALLOWED.value
        complete = (
            bool(self.statements)
            and command.root_cause_category is not None
            and command.root_cause_statement == "present"
            and command.conclusion == "present"
        )
        if not complete:
            return LoopDetailCode.ANALYSIS_INFORMATION_INSUFFICIENT.value
        if command.license != "present":
            return LoopDetailCode.SIGNER_LICENSE_MISSING.value
        self.signed = True
        return "signed"


def _final(presence: str, text: str) -> str | None:
    return {"present": text, "absent": None, "blank": "   "}[presence]


# --- dominio puro -------------------------------------------------------------------------------


_TEXT_CODES: Final = {
    "short": INVALID,
    "blocked": LoopDetailCode.BLOCKED_VOCABULARY_TERM.value,
    "attributed": LoopDetailCode.PERSON_ATTRIBUTION_DETECTED.value,
}
"""Lo que la política de texto libre respondería a cada clase de enunciado (no es del dominio)."""


class GuidedFlowDomain(RuleBasedStateMachine):
    """El dominio puro en el orden de la aplicación: ``put_step`` decide el orden (y lo firmado),
    después la política de texto y ``cause_category``; ``sign`` decide la firma."""

    def __init__(self) -> None:
        super().__init__()
        self.model = Model()
        self.analysis = GuidedAnalysis()

    def _put(self, command: PutStepCommand) -> str:
        try:
            # El dominio decide primero el orden; el texto y la categoría van después.
            put_step(self.analysis, WhyStep(command.step_index, command.statement))
            if command.kind != "valid":
                return _TEXT_CODES[command.kind]
            category = None if command.category is None else cause_category(command.category)
            self.analysis = put_step(
                self.analysis, WhyStep(command.step_index, command.statement, (), category)
            )
        except RootCauseRejected as rejected:
            return f"loop_{rejected.code.value}"
        return "accepted"

    def _sign(self, command: SignCommand) -> str:
        try:
            if self.analysis.state is RootCauseAnalysisState.SIGNED:
                sign(
                    self.analysis,
                    branches=(),
                    root_cause_category=None,
                    root_cause_statement=None,
                    conclusion=None,
                    signer_name=None,
                    professional_license=None,
                )
            category = (
                None
                if command.root_cause_category is None
                else cause_category(command.root_cause_category)
            )
            branches = tuple(
                Branch(cause_category(c), (Factor(FACTOR),)) for c in command.branch_categories
            )
            outcome = sign(
                self.analysis,
                branches=branches,
                root_cause_category=category,
                root_cause_statement=ROOT_STATEMENT
                if command.root_cause_statement == "present"
                else None,
                conclusion=CONCLUSION if command.conclusion == "present" else None,
                signer_name="Coordinación SST sintética",
                professional_license=LICENSE if command.license == "present" else None,
            )
        except RootCauseRejected as rejected:
            return f"loop_{rejected.code.value}"
        self.analysis = outcome.analysis
        if isinstance(outcome, Pending):
            return LoopDetailCode.ANALYSIS_INFORMATION_INSUFFICIENT.value
        return "signed"

    @rule(command=analysis_step_commands())
    def order(self, command: AnalysisCommand) -> None:
        if isinstance(command, PutStepCommand):
            expected = self.model.put(command)
            assert self._put(command) == expected, command
        else:
            expected = self.model.sign(command)
            assert self._sign(command) == expected, command

    @invariant()
    def steps_are_in_order_and_at_most_five(self) -> None:
        assert steps_in_order(self.analysis.steps)
        assert len(self.analysis.steps) <= MAX_WHY_STEPS
        assert all(step.statement for step in self.analysis.steps)

    @invariant()
    def the_model_and_the_domain_agree(self) -> None:
        steps = [
            (step.statement, None if step.category is None else step.category.value)
            for step in self.analysis.steps
        ]
        assert steps == self.model.statements
        assert (self.analysis.state is RootCauseAnalysisState.SIGNED) == self.model.signed


def test_pr_laz_18_the_domain_guided_flow() -> None:
    """Con cada semilla del perfil activo (``tests/conftest.py``)."""
    for value in _seeds_for_profile():
        seeded = hypothesis_seed(value)(GuidedFlowDomain)
        run_state_machine_as_test(seeded, settings=settings(stateful_step_count=30))


# --- el servicio real ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[RootCauseStack]:
    with root_cause_stack(postgres_endpoint, "pr_laz_18") as built:
        yield built


class GuidedFlowService(RuleBasedStateMachine):
    """El ``RootCauseService`` real: cada orden contra la base y la fila contra el modelo."""

    stack: ClassVar[RootCauseStack]
    context: ClassVar[Any]
    finding_state_id: ClassVar[uuid.UUID]

    def __init__(self) -> None:
        super().__init__()
        self.model = Model()
        view: AnalysisView = self.stack.run(
            self.stack.root_cause.start(self.context, self.finding_state_id)
        )
        self.analysis_id = view.analysis_id

    def _call(self, awaitable: Any) -> str:
        try:
            self.stack.run(awaitable)
        except AnalysisRejected as rejected:
            return rejected.detail_code.value
        except AnalysisRequestInvalid:
            return INVALID
        return "accepted"

    @rule(command=analysis_step_commands())
    def order(self, command: AnalysisCommand) -> None:
        service = self.stack.root_cause
        if isinstance(command, PutStepCommand):
            expected = self.model.put(command)
            got = self._call(
                service.put_step(
                    self.context,
                    self.analysis_id,
                    command.step_index,
                    statement=command.statement,
                    evidence_refs=[],
                    category=command.category,
                )
            )
            assert got == expected, command
            return
        expected = self.model.sign(command)
        got = self._call(
            service.sign(
                self.context,
                self.analysis_id,
                professional_license=_final(command.license, LICENSE),
                root_cause_category=command.root_cause_category,
                root_cause_statement=_final(command.root_cause_statement, ROOT_STATEMENT),
                conclusion=_final(command.conclusion, CONCLUSION),
                cause_effect_branches=[
                    {"branch_category": c, "factors": [{"text": FACTOR, "evidence_refs": []}]}
                    for c in command.branch_categories
                ],
            )
        )
        assert got == ("accepted" if expected == "signed" else expected), command

    @invariant()
    def the_row_has_the_steps_in_order_and_at_most_five(self) -> None:
        row = self.stack.analysis_row(self.analysis_id)
        steps = row["why_steps"]
        assert [step["step_index"] for step in steps] == list(range(1, len(steps) + 1))
        assert len(steps) <= MAX_WHY_STEPS
        assert [(s["statement"], s.get("category")) for s in steps] == self.model.statements
        assert (row["state"] == "signed") == self.model.signed
        if self.model.signed:
            (record,) = [
                r
                for r in self.stack.analysis_records(self.stack.world.a)
                if r["record_id"] == row["record_id"]
            ]
            assert [{k: v for k, v in s.items() if k != "question_es"} for s in steps] == record[
                "content"
            ]["why_steps"]


@pytest.mark.integration
def test_pr_laz_18_the_real_guided_flow(stack: RootCauseStack) -> None:
    coordinator = stack.member(stack.world.a, Role.COORDINATOR_SST)
    GuidedFlowService.stack = stack
    GuidedFlowService.context = stack.context(coordinator)
    GuidedFlowService.finding_state_id = stack.finding_state_id(stack.received())
    for value in _seeds_for_profile():
        seeded = hypothesis_seed(value)(GuidedFlowService)
        run_state_machine_as_test(
            seeded,
            settings=settings(
                max_examples=SERVICE_EXAMPLES,
                stateful_step_count=12,
                deadline=None,
                database=None,
                suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
            ),
        )
