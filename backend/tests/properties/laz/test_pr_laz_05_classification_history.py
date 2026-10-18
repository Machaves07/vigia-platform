"""PR-LAZ-05: el historial de clasificaciones es monótono y nada anterior se retira.

business-logic-model.md de U-04 §11, C-PLA-23 (TASK-308; BR-LAZ-20, 21, 23 a 25; P4). Con
``classification_sequences`` sobre un hallazgo nuevo por ejemplo, contra PostgreSQL 16 real como
``vigia_app``, el ``ClassificationService`` real y el escritor con los validadores de U-04:

- **monótono**: tras cada paso, los registros anteriores del sujeto son un **prefijo idéntico**
  (identificador, secuencia de cadena, hashes y contenido) de los nuevos; ninguna operación lo
  reduce ni lo reescribe;
- **un rechazo no escribe nada**: ni registro, ni etiqueta, ni evento, ni historial, ni cambio en
  la proyección; una aceptación añade exactamente un ``classification`` con la secuencia
  siguiente;
- ``classification_count`` **no decrece** y es el número de registros ``classification`` (y de
  filas de ``loop.classification``);
- el código de cada rechazo y el estado de cada aceptación son los del **modelo**: la tabla de
  §4.1 (``lifecycle.transition``) y el orden de BLM §1 (transición, motivo, motivo reforzado,
  validadores);
- **un cierre o una declaración previos siguen íntegros** tras cualquier reclasificación
  (T19, T20): sus registros no cambian y la proyección conserva ``closed_at``,
  ``closure_record_id`` y ``unclosable_since``.

Solo datos generados.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Final

import pytest
from hypothesis import HealthCheck, event, given, settings

from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import IngestedSubject
from tests.loop_classification_support import ClassificationStack, classification_stack
from tests.properties.laz.strategies.classification import (
    ClassifyStep,
    LifecycleStep,
    Step,
    classification_sequences,
    reason_text,
)
from vigia_platform.loop.application.classification import (
    ClassificationRejected,
    ClassificationResult,
)
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.domain.enums import FindingLifecycleState
from vigia_platform.loop.domain.lifecycle import (
    Accepted,
    Classify,
    LifecycleSnapshot,
    Rejected,
    transition,
)
from vigia_platform.shared.context import Role

pytestmark = pytest.mark.integration

EXAMPLES: Final = 30
"""Cada ejemplo ingresa un hallazgo y escribe varias transacciones reales."""
S = FindingLifecycleState


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[ClassificationStack]:
    with classification_stack(postgres_endpoint, "pr_laz_05") as built:
        yield built


def _snapshot(row: dict[str, Any]) -> LifecycleSnapshot:
    previous = row["previous_lifecycle_state"]
    return LifecycleSnapshot(
        S(row["lifecycle_state"]), None if previous is None else S(previous), row["due_at"]
    )


def _expected(stack: ClassificationStack, row: dict[str, Any], step: ClassifyStep) -> str:
    """Oráculo: ``rejected:<detail_code>`` o el estado de destino (BLM §1, en su orden)."""
    snapshot = _snapshot(row)
    at = stack.world.stack.now()
    if isinstance(transition(snapshot, Classify(step.outcome, True, at)), Rejected):
        return "rejected:" + LoopDetailCode.TRANSITION_NOT_ALLOWED.value
    if step.reason == "missing":
        return "rejected:" + LoopDetailCode.REASON_REQUIRED.value
    if step.reason == "short":
        return "rejected:" + LoopDetailCode.REASON_TOO_SHORT.value
    if row["classification_count"] >= 1 and not step.reinforced:
        return "rejected:" + LoopDetailCode.REASON_REQUIRED.value
    if step.reason == "blocked":
        return "rejected:" + LoopDetailCode.BLOCKED_VOCABULARY_TERM.value
    accepted = transition(snapshot, Classify(step.outcome, step.reinforced, at))
    assert isinstance(accepted, Accepted)
    return accepted.state.value


def _lifecycle(stack: ClassificationStack, subject: IngestedSubject, step: LifecycleStep) -> None:
    """La operación si el estado la permite (§4.1); si no, nada."""
    row = stack.world.finding_state(subject)
    assert row is not None
    state = S(row["lifecycle_state"])
    if step.operation == "assign_action" and state is S.OPEN:
        stack.assign_action(subject)
    elif step.operation == "close" and state in (S.ACTION_ASSIGNED, S.OVERDUE):
        stack.close(subject, str(row["current_action_id"]))
    elif step.operation == "declare_unclosable" and state in (
        S.OPEN,
        S.ACTION_ASSIGNED,
        S.OVERDUE,
    ):
        stack.declare_unclosable(subject)


_TERMINAL_FIELDS: Final = (
    "closed_at",
    "closure_record_id",
    "unclosable_since",
    "unclosable_review_due_at",
)


def test_pr_laz_05_history_is_monotone_and_prior_closures_stay_intact(
    stack: ClassificationStack,
) -> None:
    coordinator = stack.member(stack.world.a, Role.COORDINATOR_SST)
    context = stack.context(coordinator)

    @settings(
        max_examples=EXAMPLES,
        deadline=None,
        database=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(classification_sequences())
    def run(steps: list[Step]) -> None:
        subject = stack.received()
        fsid = stack.finding_state_id(subject)
        for step in steps:
            if isinstance(step, LifecycleStep):
                _lifecycle(stack, subject, step)
                continue
            row = stack.world.finding_state(subject)
            assert row is not None
            records = stack.subject_records(subject)
            counts = stack.counts(subject.organization)
            expected = _expected(stack, row, step)
            try:
                result: ClassificationResult | str = stack.run(
                    stack.service.classify(
                        context,
                        fsid,
                        outcome=step.outcome.value,
                        reason_category=step.reason_category,
                        reason_text=reason_text(step),
                        reinforced_reason=step.reinforced,
                    )
                )
            except ClassificationRejected as rejected:
                result = "rejected:" + rejected.detail_code.value
            after_records = stack.subject_records(subject)
            after = stack.world.finding_state(subject)
            assert after is not None
            # Monótono: lo anterior es un prefijo idéntico de lo nuevo.
            assert after_records[: len(records)] == records
            assert after["classification_count"] >= row["classification_count"]
            if isinstance(result, str):
                assert result == expected, step
                event(result)
                assert after_records == records
                assert stack.counts(subject.organization) == counts
                assert after == row
                continue
            assert result.lifecycle_state.value == expected, step
            event(f"aceptada {result.transition_id}")
            (added,) = after_records[len(records) :]
            assert added["record_type"] == "classification"
            assert added["content"]["sequence_in_subject"] == row["classification_count"] + 1
            assert after["classification_count"] == row["classification_count"] + 1
            assert after["lifecycle_state"] == expected
            if S(row["lifecycle_state"]) in (S.CLOSED, S.UNCLOSABLE):
                for name in _TERMINAL_FIELDS:
                    assert after[name] == row[name], name
                if step.outcome.value != "confirmed":
                    assert after["closed_with_later_different_classification"] is True
            if row["closed_with_later_different_classification"]:
                assert after["closed_with_later_different_classification"] is True
        classifications = [
            r for r in stack.subject_records(subject) if r["record_type"] == "classification"
        ]
        final = stack.world.finding_state(subject)
        assert final is not None
        assert final["classification_count"] == len(classifications)
        assert [r["content"]["sequence_in_subject"] for r in classifications] == list(
            range(1, len(classifications) + 1)
        )
        assert len(stack.history(subject)) == len(classifications)

    run()
