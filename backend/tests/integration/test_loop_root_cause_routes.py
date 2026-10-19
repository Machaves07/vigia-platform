"""SUP-U05-16 a 18: las rutas del análisis de causa raíz guiado (TASK-315; LC-LAZ-12).

Sobre PostgreSQL 16 real como ``vigia_app`` y la aplicación real (``root_cause_stack``):

- **Inicio** (BR-LAZ-63): ``201`` con el borrador y el ``ZoneObjectiveContext`` compuesto de
  hechos medidos (estándar con que se evaluó, señales, duraciones, cobertura del periodo, nodo,
  historial de la zona); sin el texto del estándar, ``loop_standard_text_unavailable`` (503) sin
  escribir nada.
- **Pasos** (BR-LAZ-61, 62): el siguiente o uno ya completo; saltarse uno es
  ``loop_analysis_step_out_of_order`` (409); un paso 0 o 6, ``invalid_request``; el enunciado de
  19 y 1001 caracteres, ``invalid_request``, y de 20 y 1000, aceptado; los dos validadores; una
  categoría fuera de las tres, ``loop_cause_category_not_allowed``. Nada escrito en un rechazo.
- **Firma** (BR-LAZ-64, 65): con algo pendiente, ``loop_analysis_information_insufficient`` y la
  fila queda ``pending_information`` con lo que falta (igualdad exacta de la enumeración); sin
  licencia, ``loop_signer_license_missing``; firmada, un registro ``root_cause_analysis`` con el
  texto aceptado **exactamente**, su evento y ``has_signed_root_cause`` en la proyección, todo en
  la misma transacción; otra firma u otro paso, ``loop_transition_not_allowed`` (409).
- **Concurrencia**: dos firmas simultáneas dan un solo registro; dos pasos simultáneos no se
  pierden; un paso durante una firma no toca lo firmado; y la prueba cruzada del orden de
  candados frente a la clasificación del mismo sujeto.
- **Alcance**: otra organización, inexistente, otra zona, un rol sin ``root_cause.analyze`` y la
  concesión de proveedor responden ``not_found`` sin escribir.
- **Enlace de la acción**: ``linked_analysis`` solo resuelve el registro de un análisis firmado
  del mismo sujeto.

Solo datos generados. Las comparaciones son de códigos, valores de listas cerradas o igualdad
exacta de cuerpos (VIG-194): ninguna por subcadena.
"""

from __future__ import annotations

import asyncio
import unicodedata
import uuid
from collections.abc import Iterator
from typing import Any, Final

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.loop_classification_support import ATTRIBUTION_MARKER, BLOCKED_REASON, Who
from tests.loop_root_cause_support import (
    CONCLUSION,
    FACTOR,
    LICENSE,
    ROOT_STATEMENT,
    STATEMENTS,
    RootCauseStack,
    complete_signature,
    root_cause_stack,
)
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.loop.application.root_cause import (
    AnalysisRejected,
    AnalysisView,
    RootCauseService,
    linked_analysis,
)
from vigia_platform.loop.detail_codes import HTTP_STATUS_BY_DETAIL_CODE, LoopDetailCode
from vigia_platform.loop.domain.root_cause import QUESTIONS_ES
from vigia_platform.shared.api.errors import ApiErrorCode
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration

LOCK_WAIT_SECONDS: Final = 60.0
"""Tope para ver una transacción esperando un candado (no decide el resultado)."""
MISSING_ALL: Final = [
    "why_steps",
    "root_cause_category",
    "root_cause_statement",
    "conclusion",
]
ATTRIBUTED: Final = f"El atasco de la prensa ocurrió porque {ATTRIBUTION_MARKER} sin avisar"
"""El doble de ``person_attribution`` lo rechaza (TASK-340 trae el real)."""


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[RootCauseStack]:
    with root_cause_stack(postgres_endpoint, "loop_root_cause_routes") as built:
        yield built


@pytest.fixture(autouse=True)
def _release_instances(stack: RootCauseStack) -> Iterator[None]:
    yield
    stack.world.stack.run(stack.world.stack.release())


@pytest.fixture(scope="module")
def coordinator(stack: RootCauseStack) -> Who:
    return stack.member(stack.world.a, Role.COORDINATOR_SST)


def _error(response: Any, status: int, detail_code: LoopDetailCode | None) -> None:
    assert response.status_code == status, response.text
    body = response.json()
    assert body.get("detail_code") == (None if detail_code is None else detail_code.value)


def _row(stack: RootCauseStack, analysis_id: str | uuid.UUID) -> dict[str, Any]:
    return stack.analysis_row(analysis_id)


# --- inicio ------------------------------------------------------------------------------------


def test_start_creates_a_draft_with_the_objective_zone_context(
    stack: RootCauseStack, coordinator: Who
) -> None:
    subject = stack.received()
    row = stack.world.finding_state(subject)
    assert row is not None
    body = stack.started(coordinator, subject)
    assert body["state"] == "draft"
    assert body["subject_kind"] == "finding"
    assert body["subject_id"] == str(subject.subject_id)
    assert body["why_steps"] == []
    assert (body["next_step_index"], body["next_question_es"]) == (1, QUESTIONS_ES[0])
    assert body["pending_information"] == MISSING_ALL
    assert body["missing_information"] is None
    assert (body["record_id"], body["signer"], body["signed_at"]) == (None, None, None)
    context = body["zone_context"]
    assert context["zone_id"] == str(subject.organization.zone_id)
    assert context["family"] == row["family"]
    assert context["tier"] == row["tier"]
    assert context["standard_id"] == str(row["standard_id"])
    assert context["standard_version"] == row["standard_version"]
    assert context["observability_state_at_event"] == row["observability_state_at_event"]
    assert isinstance(context["standard_text"], str) and context["standard_text"]
    assert context["signals_by_role"]
    assert all(set(signal) == {"role", "value", "valid"} for signal in context["signals_by_role"])
    episode = stack.world.episode(subject)
    assert episode is not None
    assert context["episode_durations"] == {
        "episode_ms": episode["total_episode_duration_ms"],
        "condition_ms": episode["total_condition_duration_ms"],
    }
    assert set(context["coverage_summary_period"]) == {
        "observable_ms",
        "degraded_ms",
        "not_observable_ms",
        "no_communication_ms",
        "never_reported_ms",
        "zone_not_active_ms",
    }
    assert context["node_state"]
    assert all(
        set(node) == {"communication_state", "last_heartbeat_at"} for node in context["node_state"]
    )
    families = {
        item["family"]: item["count"] for item in context["finding_counts_by_family_in_period"]
    }
    assert families.get(row["family"], 0) >= 1
    stored = stack.analysis_row(body["analysis_id"])
    assert stored["state"] == "draft" and stored["zone_context"] == context
    assert stored["organization_id"] == subject.organization.organization_id
    assert stored["zone_id"] == subject.organization.zone_id


def test_start_on_a_detection_has_no_episode_durations(
    stack: RootCauseStack, coordinator: Who
) -> None:
    detection = stack.in_review()
    body = stack.started(coordinator, detection)
    assert body["subject_kind"] == "detection"
    assert body["zone_context"]["episode_durations"] == {"episode_ms": None, "condition_ms": None}


class _NoStandard:
    """``CatalogQueryPort`` que no encuentra la versión del estándar (``ResourceNotFound``)."""

    async def standard_version(self, *args: Any, **kwargs: Any) -> Any:
        raise ResourceNotFound()


def test_without_the_standard_text_nothing_is_created(
    stack: RootCauseStack, coordinator: Who
) -> None:
    subject = stack.received()
    before = stack.analysis_counts(stack.world.a)
    service = stack.root_cause_like(catalog=_NoStandard())
    with pytest.raises(AnalysisRejected) as rejected:
        stack.run(service.start(stack.context(coordinator), stack.finding_state_id(subject)))
    assert rejected.value.detail_code is LoopDetailCode.STANDARD_TEXT_UNAVAILABLE
    assert HTTP_STATUS_BY_DETAIL_CODE[LoopDetailCode.STANDARD_TEXT_UNAVAILABLE] == (
        ApiErrorCode.TEMPORARILY_UNAVAILABLE,
        503,
    )
    assert stack.analysis_counts(stack.world.a) == before


def test_the_context_counts_previous_actions_recurrence_and_signed_causes(
    stack: RootCauseStack, coordinator: Who
) -> None:
    earlier = stack.received()
    assert stack.classify(coordinator, earlier).status_code == 201
    action_id = stack.assign_action(earlier)
    stack.close(earlier, action_id)
    closed = stack.world.finding_state(earlier)
    assert closed is not None and closed["closed_at"] is not None
    signed = stack.with_steps(coordinator, 1)
    assert stack.signature(coordinator, signed, complete_signature()).status_code == 201
    # El hallazgo sintético empieza 10 minutos antes de «ahora»: tras 11, es posterior al cierre.
    stack.tick(11 * 60)
    later = stack.received()
    context = stack.started(coordinator, later)["zone_context"]
    previous = [
        action
        for action in context["previous_actions"]
        if action.get("closed_at") == format_timestamp(closed["closed_at"])
    ]
    assert len(previous) == 1
    (action,) = previous
    assert (action["action_type"], action["outcome"]) == ("engineering", "closed")
    assert action["recurrence_after_days"] >= 0
    # Ningún dato de una persona: ni responsable, ni firmante, ni texto escrito por un usuario.
    assert set(action) == {"action_type", "outcome", "closed_at", "recurrence_after_days"}
    causes = {item["category"]: item["count"] for item in context["previous_cause_categories"]}
    assert causes.get("engineering", 0) >= 1
    assert context["recurrences_in_period"] >= 1


# --- pasos -------------------------------------------------------------------------------------


def test_steps_advance_in_order_and_an_earlier_one_can_be_edited(
    stack: RootCauseStack, coordinator: Who
) -> None:
    analysis_id = str(stack.started(coordinator)["analysis_id"])
    first = stack.step(coordinator, analysis_id, 1, {"statement": STATEMENTS[0]})
    assert first.status_code == 200, first.text
    assert [s["step_index"] for s in first.json()["why_steps"]] == [1]
    assert (first.json()["next_step_index"], first.json()["next_question_es"]) == (
        2,
        QUESTIONS_ES[1],
    )
    before = _row(stack, analysis_id)
    _error(
        stack.step(coordinator, analysis_id, 3, {"statement": STATEMENTS[2]}),
        409,
        LoopDetailCode.ANALYSIS_STEP_OUT_OF_ORDER,
    )
    for index in (0, 6, -1, "dos"):
        _error(stack.step(coordinator, analysis_id, index, {"statement": STATEMENTS[0]}), 400, None)
    assert _row(stack, analysis_id) == before
    assert stack.step(coordinator, analysis_id, 2, {"statement": STATEMENTS[1]}).status_code == 200
    edited = stack.step(coordinator, analysis_id, 1, {"statement": STATEMENTS[4]})
    assert edited.status_code == 200
    assert [
        (s["step_index"], s["statement"], s["question_es"]) for s in edited.json()["why_steps"]
    ] == [(1, STATEMENTS[4], QUESTIONS_ES[0]), (2, STATEMENTS[1], QUESTIONS_ES[1])]


def test_never_more_than_five_whys(stack: RootCauseStack, coordinator: Who) -> None:
    analysis_id = stack.with_steps(coordinator, 5)
    assert [s["step_index"] for s in _row(stack, analysis_id)["why_steps"]] == [1, 2, 3, 4, 5]
    edit = stack.step(coordinator, analysis_id, 5, {"statement": STATEMENTS[0]})
    assert edit.status_code == 200
    assert (edit.json()["next_step_index"], edit.json()["next_question_es"]) == (None, None)
    _error(stack.step(coordinator, analysis_id, 6, {"statement": STATEMENTS[0]}), 400, None)
    assert len(_row(stack, analysis_id)["why_steps"]) == 5


@pytest.mark.parametrize(
    ("length", "accepted"), [(19, False), (20, True), (1000, True), (1001, False)]
)
def test_the_statement_borders(
    stack: RootCauseStack, coordinator: Who, length: int, accepted: bool
) -> None:
    analysis_id = str(stack.started(coordinator)["analysis_id"])
    statement = ("Trazado del pasillo de carga " * 40)[: length - 1] + "."
    assert len(statement) == length
    response = stack.step(coordinator, analysis_id, 1, {"statement": statement})
    if accepted:
        assert response.status_code == 200, response.text
        assert response.json()["why_steps"][0]["statement"] == statement
    else:
        _error(response, 400, None)
        assert _row(stack, analysis_id)["why_steps"] == []


@pytest.mark.parametrize("statement", ["", "   ", None, 42])
def test_a_missing_or_blank_statement_is_rejected(
    stack: RootCauseStack, coordinator: Who, statement: Any
) -> None:
    analysis_id = str(stack.started(coordinator)["analysis_id"])
    body = {} if statement is None else {"statement": statement}
    _error(stack.step(coordinator, analysis_id, 1, body), 400, None)
    assert _row(stack, analysis_id)["why_steps"] == []


@pytest.mark.parametrize(
    "category",
    [
        "person",
        "operator",
        "worker",
        "human_error",
        "behavior",
        "persona",
        "operario",
        "Process",
        "process ",
        "ENGINEERING",
        "",
        "organisation",
    ],
)
def test_a_step_category_outside_the_three_is_not_allowed(
    stack: RootCauseStack, coordinator: Who, category: str
) -> None:
    analysis_id = str(stack.started(coordinator)["analysis_id"])
    before = _row(stack, analysis_id)
    body = {"statement": STATEMENTS[0], "category": category}
    _error(
        stack.step(coordinator, analysis_id, 1, body),
        400,
        LoopDetailCode.CAUSE_CATEGORY_NOT_ALLOWED,
    )
    assert _row(stack, analysis_id) == before


@pytest.mark.parametrize("category", ["process", "engineering", "organization"])
def test_each_of_the_three_categories_is_accepted_on_a_step(
    stack: RootCauseStack, coordinator: Who, category: str
) -> None:
    analysis_id = str(stack.started(coordinator)["analysis_id"])
    body = {"statement": STATEMENTS[0], "category": category}
    response = stack.step(coordinator, analysis_id, 1, body)
    assert response.status_code == 200, response.text
    assert response.json()["why_steps"][0]["category"] == category


@pytest.mark.parametrize(
    ("statement", "code"),
    [
        (BLOCKED_REASON, LoopDetailCode.BLOCKED_VOCABULARY_TERM),
        (ATTRIBUTED, LoopDetailCode.PERSON_ATTRIBUTION_DETECTED),
    ],
)
def test_the_two_validators_reject_a_step_and_nothing_is_written(
    stack: RootCauseStack, coordinator: Who, statement: str, code: LoopDetailCode
) -> None:
    analysis_id = stack.with_steps(coordinator, 1)
    before = _row(stack, analysis_id)
    for index in (1, 2):
        _error(stack.step(coordinator, analysis_id, index, {"statement": statement}), 400, code)
    assert _row(stack, analysis_id) == before


def test_evidence_refs_are_kept_and_their_shape_is_strict(
    stack: RootCauseStack, coordinator: Who
) -> None:
    analysis_id = str(stack.started(coordinator)["analysis_id"])
    refs = [
        {"kind": kind, "id": str(uuid.uuid4())} for kind in ("record", "clip", "metric", "coverage")
    ]
    body = {"statement": STATEMENTS[0], "evidence_refs": refs}
    accepted = stack.step(coordinator, analysis_id, 1, body)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["why_steps"][0]["evidence_refs"] == refs
    before = _row(stack, analysis_id)
    for bad in (
        [{"kind": "person", "id": str(uuid.uuid4())}],
        [{"kind": "record", "id": "no-es-uuid"}],
        [{"kind": "record", "id": str(uuid.uuid4()), "quote": "texto"}],
        [{"kind": "record", "id": str(uuid.uuid4())}] * 33,
    ):
        body = {"statement": STATEMENTS[1], "evidence_refs": bad}
        _error(stack.step(coordinator, analysis_id, 2, body), 400, None)
    extra = {"statement": STATEMENTS[1], "extra": True}
    _error(stack.step(coordinator, analysis_id, 2, extra), 400, None)
    assert _row(stack, analysis_id) == before


# --- firma -------------------------------------------------------------------------------------

ALL_MISSING_ES: Final = (
    "Falta para firmar: al menos el primer porqué completo; la categoría de la causa raíz;"
    " el enunciado de la causa raíz; la conclusión del análisis."
)
LABELS_ES: Final = {
    "why_steps": "al menos el primer porqué completo",
    "root_cause_category": "la categoría de la causa raíz",
    "root_cause_statement": "el enunciado de la causa raíz",
    "conclusion": "la conclusión del análisis",
}


def test_signing_with_missing_information_marks_it_pending_and_enumerates_what_is_missing(
    stack: RootCauseStack, coordinator: Who
) -> None:
    analysis_id = str(stack.started(coordinator)["analysis_id"])
    before = stack.analysis_counts(stack.world.a)
    _error(
        stack.signature(coordinator, analysis_id, {"professional_license": LICENSE}),
        400,
        LoopDetailCode.ANALYSIS_INFORMATION_INSUFFICIENT,
    )
    row = _row(stack, analysis_id)
    assert (row["state"], row["missing_information"]) == ("pending_information", ALL_MISSING_ES)
    assert (row["record_id"], row["signer"], row["signed_at"]) == (None, None, None)
    assert stack.analysis_counts(stack.world.a) == before
    # Un paso no lo saca de pendiente mientras falte algo: vuelve a enumerar lo que falta.
    step = stack.step(coordinator, analysis_id, 1, {"statement": STATEMENTS[0]})
    assert step.status_code == 200
    assert step.json()["state"] == "pending_information"
    assert step.json()["pending_information"] == MISSING_ALL[1:]
    assert step.json()["missing_information"] == (
        "Falta para firmar: la categoría de la causa raíz; el enunciado de la causa raíz;"
        " la conclusión del análisis."
    )
    # Firmar otra vez con algo pendiente sigue sin firmar.
    partial = {"professional_license": LICENSE, "root_cause_category": "process"}
    _error(
        stack.signature(coordinator, analysis_id, partial),
        400,
        LoopDetailCode.ANALYSIS_INFORMATION_INSUFFICIENT,
    )
    row = _row(stack, analysis_id)
    assert (row["state"], row["root_cause_category"], row["missing_information"]) == (
        "pending_information",
        "process",
        "Falta para firmar: el enunciado de la causa raíz; la conclusión del análisis.",
    )
    assert stack.analysis_counts(stack.world.a) == before
    signed = stack.signature(coordinator, analysis_id, complete_signature())
    assert signed.status_code == 201, signed.text
    assert (signed.json()["state"], signed.json()["pending_information"]) == ("signed", [])
    assert signed.json()["missing_information"] is None


@pytest.mark.parametrize("missing", MISSING_ALL)
def test_each_missing_item_alone_blocks_the_signature(
    stack: RootCauseStack, coordinator: Who, missing: str
) -> None:
    analysis_id = stack.with_steps(coordinator, 0 if missing == "why_steps" else 1)
    body = complete_signature(**({} if missing == "why_steps" else {missing: None}))
    _error(
        stack.signature(coordinator, analysis_id, body),
        400,
        LoopDetailCode.ANALYSIS_INFORMATION_INSUFFICIENT,
    )
    row = _row(stack, analysis_id)
    assert (row["state"], row["missing_information"]) == (
        "pending_information",
        f"Falta para firmar: {LABELS_ES[missing]}.",
    )


def test_a_blank_final_text_counts_as_missing(stack: RootCauseStack, coordinator: Who) -> None:
    analysis_id = stack.with_steps(coordinator, 1)
    body = complete_signature(root_cause_statement="   ", conclusion="")
    _error(
        stack.signature(coordinator, analysis_id, body),
        400,
        LoopDetailCode.ANALYSIS_INFORMATION_INSUFFICIENT,
    )
    assert _row(stack, analysis_id)["missing_information"] == (
        "Falta para firmar: el enunciado de la causa raíz; la conclusión del análisis."
    )


def test_a_pending_analysis_returns_to_draft_when_nothing_is_missing(
    stack: RootCauseStack, coordinator: Who
) -> None:
    analysis_id = str(stack.started(coordinator)["analysis_id"])
    _error(
        stack.signature(coordinator, analysis_id, complete_signature()),
        400,
        LoopDetailCode.ANALYSIS_INFORMATION_INSUFFICIENT,
    )
    assert _row(stack, analysis_id)["missing_information"] == (
        "Falta para firmar: al menos el primer porqué completo."
    )
    step = stack.step(coordinator, analysis_id, 1, {"statement": STATEMENTS[0]})
    assert (step.json()["state"], step.json()["pending_information"]) == ("draft", [])
    assert step.json()["missing_information"] is None


@pytest.mark.parametrize("license_", [None, "", "   "])
def test_without_a_license_there_is_no_signature(
    stack: RootCauseStack, coordinator: Who, license_: str | None
) -> None:
    analysis_id = stack.with_steps(coordinator, 2)
    row = _row(stack, analysis_id)
    before = stack.analysis_counts(stack.world.a)
    body = complete_signature(professional_license=license_)
    _error(
        stack.signature(coordinator, analysis_id, body),
        400,
        LoopDetailCode.SIGNER_LICENSE_MISSING,
    )
    assert _row(stack, analysis_id) == row
    assert stack.analysis_counts(stack.world.a) == before


def test_the_license_borders(stack: RootCauseStack, coordinator: Who) -> None:
    analysis_id = stack.with_steps(coordinator, 1)
    for bad in ("L" * 65, "LIC\u0000001", "LIC​001"):
        body = complete_signature(professional_license=bad)
        _error(stack.signature(coordinator, analysis_id, body), 400, None)
    body = complete_signature(professional_license="L" * 64)
    signed = stack.signature(coordinator, analysis_id, body)
    assert signed.status_code == 201, signed.text
    assert signed.json()["signer"]["professional_license"] == "L" * 64


def _record_steps(view_steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Los porqués que vio el coordinador, en la forma del registro (sin ``question_es``)."""
    steps = []
    for step in view_steps:
        shaped = {
            "step_index": step["step_index"],
            "statement": step["statement"],
            "evidence_refs": step["evidence_refs"],
        }
        if step["category"] is not None:
            shaped["category"] = step["category"]
        steps.append(shaped)
    return steps


def _stored_steps(row: dict[str, Any]) -> list[dict[str, Any]]:
    return [{k: v for k, v in step.items() if k != "question_es"} for step in row["why_steps"]]


def test_the_signed_record_is_exactly_the_accepted_text(
    stack: RootCauseStack, coordinator: Who
) -> None:
    subject = stack.received()
    row = stack.world.finding_state(subject)
    assert row is not None
    analysis_id = str(stack.started(coordinator, subject)["analysis_id"])
    evidence = [{"kind": "record", "id": str(subject.record_id)}]
    first = {"statement": STATEMENTS[0], "evidence_refs": evidence, "category": "engineering"}
    assert stack.step(coordinator, analysis_id, 1, first).status_code == 200
    # Un enunciado en NFD: lo que el coordinador ve (y firma) es su forma NFC.
    decomposed = unicodedata.normalize("NFD", "Revisión del trazado sin análisis de los flujos")
    last = stack.step(coordinator, analysis_id, 2, {"statement": decomposed})
    assert last.status_code == 200
    seen = last.json()["why_steps"]
    assert seen[1]["statement"] == unicodedata.normalize("NFC", decomposed) != decomposed
    branches = [
        {
            "branch_category": "engineering",
            "factors": [{"text": FACTOR, "evidence_refs": evidence}],
        },
        {
            "branch_category": "organization",
            "factors": [{"text": "Revisión de cambios sin flujos", "evidence_refs": []}],
        },
    ]
    before = stack.analysis_counts(stack.world.a)
    body = complete_signature(cause_effect_branches=branches)
    response = stack.signature(coordinator, analysis_id, body)
    assert response.status_code == 201, response.text
    signed = response.json()
    assert signed["state"] == "signed"
    assert signed["signer"]["professional_license"] == LICENSE
    assert signed["signer"]["display_name"]
    assert stack.analysis_counts(stack.world.a) == (before[0], before[1] + 1, before[2] + 1)
    (record,) = [
        r
        for r in stack.analysis_records(stack.world.a)
        if str(r["record_id"]) == signed["record_id"]
    ]
    assert record["content"] == {
        "analysis_id": analysis_id,
        "subject_kind": "finding",
        "subject_id": str(subject.subject_id),
        "anchor_record_id": str(row["anchor_record_id"]),
        "why_steps": _record_steps(seen),
        "cause_effect_branches": branches,
        "root_cause_category": "engineering",
        "root_cause_statement": ROOT_STATEMENT,
        "conclusion": CONCLUSION,
        "state": "signed",
        "assistant": {"used": False},
        "signer": signed["signer"],
        "signed_at": signed["signed_at"],
    }
    # La fila firmada guarda lo mismo que el registro.
    stored = _row(stack, analysis_id)
    assert stored["state"] == "signed"
    assert str(stored["record_id"]) == signed["record_id"]
    assert _stored_steps(stored) == record["content"]["why_steps"]
    assert stored["cause_effect_branches"] == branches
    assert stored["signer"] == signed["signer"]
    # El evento y la proyección, en la misma transacción.
    record_id = uuid.UUID(signed["record_id"])
    assert stack.events(stack.world.a, record_id) == ["root_cause_analysis_signed"]
    projected = stack.world.finding_state(subject)
    assert projected is not None and projected["has_signed_root_cause"] is True
    assert projected["last_record_id"] == record_id
    assert projected["lifecycle_state"] == row["lifecycle_state"]


def test_a_signed_analysis_admits_no_step_and_no_second_signature(
    stack: RootCauseStack, coordinator: Who
) -> None:
    analysis_id = stack.with_steps(coordinator, 2)
    assert stack.signature(coordinator, analysis_id, complete_signature()).status_code == 201
    row = _row(stack, analysis_id)
    before = stack.analysis_counts(stack.world.a)
    for response in (
        stack.signature(coordinator, analysis_id, complete_signature()),
        stack.signature(coordinator, analysis_id, {}),
        stack.step(coordinator, analysis_id, 1, {"statement": STATEMENTS[3]}),
        stack.step(coordinator, analysis_id, 3, {"statement": STATEMENTS[3]}),
    ):
        _error(response, 409, LoopDetailCode.TRANSITION_NOT_ALLOWED)
    assert _row(stack, analysis_id) == row
    assert stack.analysis_counts(stack.world.a) == before


def _branch(category: str, text: str) -> list[dict[str, Any]]:
    return [{"branch_category": category, "factors": [{"text": text}]}]


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"root_cause_category": "human_error"}, LoopDetailCode.CAUSE_CATEGORY_NOT_ALLOWED),
        ({"root_cause_category": "operator"}, LoopDetailCode.CAUSE_CATEGORY_NOT_ALLOWED),
        (
            {"cause_effect_branches": _branch("person", FACTOR)},
            LoopDetailCode.CAUSE_CATEGORY_NOT_ALLOWED,
        ),
        (
            {"cause_effect_branches": _branch("process", ATTRIBUTED)},
            LoopDetailCode.PERSON_ATTRIBUTION_DETECTED,
        ),
        ({"conclusion": BLOCKED_REASON}, LoopDetailCode.BLOCKED_VOCABULARY_TERM),
        ({"root_cause_statement": ATTRIBUTED}, LoopDetailCode.PERSON_ATTRIBUTION_DETECTED),
        ({"cause_effect_branches": [{"branch_category": "process", "factors": []}]}, None),
        ({"root_cause_statement": "Muy corto"}, None),
        ({"conclusion": "x" * 2001}, None),
        ({"cause_effect_branches": _branch("process", "abc")}, None),
        ({"cause_effect_branches": _branch("process", FACTOR) * 7}, None),
    ],
)
def test_the_final_text_is_validated_before_anything_is_written(
    stack: RootCauseStack,
    coordinator: Who,
    changes: dict[str, Any],
    code: LoopDetailCode | None,
) -> None:
    analysis_id = stack.with_steps(coordinator, 1)
    row = _row(stack, analysis_id)
    before = stack.analysis_counts(stack.world.a)
    _error(stack.signature(coordinator, analysis_id, complete_signature(**changes)), 400, code)
    assert _row(stack, analysis_id) == row
    assert stack.analysis_counts(stack.world.a) == before


# --- concurrencia ------------------------------------------------------------------------------


async def _waiting_locks(stack: RootCauseStack) -> int:
    """Esperas de candado de las sesiones de esta base: de exclusión (``advisory``) y de fila
    (``transactionid``, sin ``database`` en ``pg_locks``: se filtra por la sesión)."""
    rows = await stack.world.stack.authz.sessions.admin.fetch(
        "SELECT count(*) AS n FROM pg_locks AS l JOIN pg_stat_activity AS a ON a.pid = l.pid"
        " WHERE NOT l.granted AND a.datname = current_database()"
    )
    return int(rows[0]["n"])


class _SignRendezvous(RootCauseService):
    """La primera firma espera, tras leer el análisis bajo sus candados, a que la segunda lo haya
    leído también **o** esté esperando un candado: sin la exclusión, las dos leen ``draft`` de
    forma determinista; con ella, la segunda espera y la primera sigue."""

    stack: RootCauseStack
    arrivals: int = 0

    async def _subject_locked(self, transaction: Transaction, analysis_id: uuid.UUID) -> Any:
        row = await super()._subject_locked(transaction, analysis_id)
        self.arrivals += 1
        if self.arrivals == 1:
            async with asyncio.timeout(LOCK_WAIT_SECONDS):
                while self.arrivals < 2 and await _waiting_locks(self.stack) == 0:
                    await asyncio.sleep(0.05)
        return row


def _sign_arguments() -> dict[str, Any]:
    return complete_signature()


@pytest.mark.parametrize("round_", range(3))
def test_two_simultaneous_signatures_write_one_record(
    stack: RootCauseStack, coordinator: Who, round_: int
) -> None:
    analysis_id = uuid.UUID(stack.with_steps(coordinator, 1))
    context = stack.context(coordinator)
    service = stack.root_cause_like(cls=_SignRendezvous)
    assert isinstance(service, _SignRendezvous)
    service.stack = stack
    before = stack.analysis_counts(stack.world.a)

    async def both() -> list[Any]:
        calls = [service.sign(context, analysis_id, **_sign_arguments()) for _ in range(2)]
        return list(await asyncio.gather(*calls, return_exceptions=True))

    results = stack.run(both())
    signed = [r for r in results if isinstance(r, AnalysisView)]
    rejected = [r for r in results if isinstance(r, AnalysisRejected)]
    assert len(signed) == 1, results
    assert [r.detail_code for r in rejected] == [LoopDetailCode.TRANSITION_NOT_ALLOWED]
    assert stack.analysis_counts(stack.world.a) == (before[0], before[1] + 1, before[2] + 1)
    assert _row(stack, analysis_id)["record_id"] == signed[0].record_id


class _StepRendezvous(RootCauseService):
    """El primer paso espera, tras leer la fila, a que el segundo la haya leído **o** esté
    esperando su candado: sin ``FOR UPDATE`` los dos leen lo mismo y uno se pierde."""

    stack: RootCauseStack
    arrivals: int = 0

    async def _locked(self, transaction: Transaction, analysis_id: uuid.UUID) -> Any:
        row = await super()._locked(transaction, analysis_id)
        self.arrivals += 1
        if self.arrivals == 1:
            async with asyncio.timeout(LOCK_WAIT_SECONDS):
                while self.arrivals < 2 and await _waiting_locks(self.stack) == 0:
                    await asyncio.sleep(0.05)
        return row


@pytest.mark.parametrize("round_", range(3))
def test_two_simultaneous_steps_are_both_kept(
    stack: RootCauseStack, coordinator: Who, round_: int
) -> None:
    analysis_id = uuid.UUID(stack.with_steps(coordinator, 1))
    context = stack.context(coordinator)
    service = stack.root_cause_like(cls=_StepRendezvous)
    assert isinstance(service, _StepRendezvous)
    service.stack = stack

    async def both() -> list[Any]:
        edit = service.put_step(
            context, analysis_id, 1, statement=STATEMENTS[4], evidence_refs=[], category=None
        )
        add = service.put_step(
            context, analysis_id, 2, statement=STATEMENTS[1], evidence_refs=[], category=None
        )
        return list(await asyncio.gather(edit, add, return_exceptions=True))

    results = stack.run(both())
    assert all(isinstance(r, AnalysisView) for r in results), results
    steps = _row(stack, analysis_id)["why_steps"]
    assert [(s["step_index"], s["statement"]) for s in steps] == [
        (1, STATEMENTS[4]),
        (2, STATEMENTS[1]),
    ]


class _SlowStep(RootCauseService):
    """El paso, tras leer la fila, espera a que la firma termine **o** espere su candado: sin
    ``FOR UPDATE`` escribiría después de la firma sobre una fila ya firmada."""

    stack: RootCauseStack
    signed: asyncio.Event
    reads: int = 0

    async def _locked(self, transaction: Transaction, analysis_id: uuid.UUID) -> Any:
        row = await super()._locked(transaction, analysis_id)
        self.reads += 1
        if self.reads == 1:
            async with asyncio.timeout(LOCK_WAIT_SECONDS):
                while not self.signed.is_set() and await _waiting_locks(self.stack) == 0:
                    await asyncio.sleep(0.05)
        return row


@pytest.mark.parametrize("round_", range(3))
def test_a_step_during_a_signature_never_changes_what_was_signed(
    stack: RootCauseStack, coordinator: Who, round_: int
) -> None:
    analysis_id = uuid.UUID(stack.with_steps(coordinator, 1))
    context = stack.context(coordinator)
    stepper = stack.root_cause_like(cls=_SlowStep)
    assert isinstance(stepper, _SlowStep)
    stepper.stack = stack

    async def race() -> list[Any]:
        stepper.signed = asyncio.Event()
        step = asyncio.create_task(
            stepper.put_step(
                context, analysis_id, 1, statement=STATEMENTS[3], evidence_refs=[], category=None
            )
        )
        async with asyncio.timeout(LOCK_WAIT_SECONDS):
            while stepper.reads == 0:
                await asyncio.sleep(0.01)

        async def signature() -> Any:
            try:
                return await stack.root_cause.sign(context, analysis_id, **_sign_arguments())
            finally:
                stepper.signed.set()

        return list(await asyncio.gather(step, signature(), return_exceptions=True))

    results = stack.run(race())
    assert all(isinstance(r, AnalysisView) for r in results), results
    row = _row(stack, analysis_id)
    assert row["state"] == "signed"
    (record,) = [
        r for r in stack.analysis_records(stack.world.a) if r["record_id"] == row["record_id"]
    ]
    assert _stored_steps(row) == record["content"]["why_steps"]
    assert record["content"]["why_steps"][0]["statement"] == STATEMENTS[3]


CROSS_PAIRS: Final = 6


def test_signature_and_classification_share_one_lock_order(
    stack: RootCauseStack, coordinator: Who
) -> None:
    """Prueba cruzada: a la vez, sobre los mismos sujetos, la firma (exclusión del sujeto → fila
    del análisis → cadena → control) y la reclasificación (exclusión del sujeto → cadena →
    control). Ninguna acaba en interbloqueo ni en ``TemporarilyUnavailable``."""
    subjects = [stack.received() for _ in range(CROSS_PAIRS)]
    analyses: dict[uuid.UUID, uuid.UUID] = {}
    for subject in subjects:
        assert stack.classify(coordinator, subject).status_code == 201
        analyses[subject.subject_id] = uuid.UUID(stack.with_steps(coordinator, 1, subject))
    ids = {subject.subject_id: stack.finding_state_id(subject) for subject in subjects}
    context = stack.context(coordinator)
    # La clasificación con su propio pool, como un proceso más: la prueba trata del orden de
    # candados, no del tamaño del pool.
    classify_database = stack.world.stack.database()
    classifier = stack.service_like(database=classify_database)
    start = asyncio.Event()

    async def sign(subject: Any) -> Any:
        await start.wait()
        return await stack.root_cause.sign(
            context, analyses[subject.subject_id], **_sign_arguments()
        )

    async def reclassify(subject: Any) -> Any:
        await start.wait()
        return await classifier.classify(
            context,
            ids[subject.subject_id],
            outcome="false_positive",
            reason_category="no_person_present",
            reason_text="Revisado el clip completo con mantenimiento: la intervención constaba",
            reinforced_reason=True,
        )

    async def race() -> list[Any]:
        tasks = [
            *(asyncio.create_task(sign(s)) for s in subjects),
            *(asyncio.create_task(reclassify(s)) for s in subjects),
        ]
        await asyncio.sleep(0)
        start.set()
        return list(await asyncio.gather(*tasks, return_exceptions=True))

    try:
        results = stack.run(race())
    finally:
        stack.run(classify_database.dispose())
    failures = [r for r in results if isinstance(r, BaseException)]
    assert failures == []
    for subject in subjects:
        row = stack.world.finding_state(subject)
        assert row is not None
        assert row["has_signed_root_cause"] is True
        # T15: la reclasificación de un hallazgo abierto suspende el lazo.
        assert (row["lifecycle_state"], row["classification_count"]) == (
            "suspended_by_reclassification",
            2,
        )


# --- alcance -----------------------------------------------------------------------------------


def _comparable(response: Any) -> tuple[int, dict[str, Any]]:
    body = dict(response.json())
    body.pop("correlation_id", None)
    return response.status_code, body


def test_out_of_scope_answers_like_missing_and_writes_nothing(
    stack: RootCauseStack, coordinator: Who
) -> None:
    world = stack.world
    foreign_coordinator = stack.member(world.b, Role.COORDINATOR_SST)
    foreign_subject = stack.received(world.b)
    foreign = stack.with_steps(foreign_coordinator, 1, foreign_subject)
    subject = stack.received()
    own = stack.with_steps(coordinator, 1, subject)
    counts = (stack.analysis_counts(world.a), stack.analysis_counts(world.b))
    rows = (_row(stack, own), _row(stack, foreign))
    step_body = {"statement": STATEMENTS[1]}
    missing_start = _comparable(stack.start(coordinator, uuid.uuid4()))
    missing_step = _comparable(stack.step(coordinator, uuid.uuid4(), 2, step_body))
    missing_sign = _comparable(stack.signature(coordinator, uuid.uuid4(), complete_signature()))
    assert missing_start[0] == missing_step[0] == missing_sign[0] == 404
    assert missing_start[1]["code"] == "not_found"
    site = world.a.site
    outsiders = (
        stack.member(world.a, Role.COORDINATOR_SST, ScopeLevel.ZONE, site.other_zone_id),
        stack.member(world.a, Role.PLANT_MANAGER),
        stack.member(world.a, Role.ADMINISTRATOR),
        stack.member(world.a, Role.LINE_MANAGER),
        stack.installer(world.a),
    )
    cases = [(coordinator, foreign_subject, foreign)]
    cases += [(outsider, subject, own) for outsider in outsiders]
    for who, finding, analysis in cases:
        assert _comparable(stack.start(who, stack.finding_state_id(finding))) == missing_start
        assert _comparable(stack.step(who, analysis, 2, step_body)) == missing_step
        signature = stack.signature(who, analysis, complete_signature())
        assert _comparable(signature) == missing_sign
    assert (stack.analysis_counts(world.a), stack.analysis_counts(world.b)) == counts
    assert (_row(stack, own), _row(stack, foreign)) == rows
    zone_coordinator = stack.member(world.a, Role.COORDINATOR_SST, ScopeLevel.ZONE, site.zone_id)
    assert stack.step(zone_coordinator, own, 2, step_body).status_code == 200
    plant_coordinator = stack.member(
        world.a, Role.COORDINATOR_SST, ScopeLevel.PLANT, world.a.plant_id
    )
    assert stack.signature(plant_coordinator, own, complete_signature()).status_code == 201


def test_the_service_itself_requires_root_cause_analyze_on_the_zone(
    stack: RootCauseStack, coordinator: Who
) -> None:
    """Sin pasar por la ruta (su ``requires`` ya deniega sin la clave): el servicio autoriza
    ``root_cause.analyze`` sobre la zona del sujeto o del análisis, y si no, ``not_found``."""
    world = stack.world
    subject = stack.received()
    own = uuid.UUID(stack.with_steps(coordinator, 1, subject))
    fsid = stack.finding_state_id(subject)
    rows, counts = _row(stack, own), stack.analysis_counts(world.a)
    site = world.a.site
    service = stack.root_cause
    for who in (
        stack.member(world.a, Role.PLANT_MANAGER),
        stack.member(world.a, Role.LINE_MANAGER),
        stack.member(world.a, Role.ADMINISTRATOR),
        stack.member(world.a, Role.COORDINATOR_SST, ScopeLevel.ZONE, site.other_zone_id),
    ):
        context = stack.context(who)
        for call in (
            service.start(context, fsid),
            service.put_step(
                context, own, 2, statement=STATEMENTS[1], evidence_refs=[], category=None
            ),
            service.sign(context, own, **_sign_arguments()),
        ):
            with pytest.raises(ResourceNotFound):
                stack.run(call)
    assert (_row(stack, own), stack.analysis_counts(world.a)) == (rows, counts)


# --- enlace de la acción -----------------------------------------------------------------------


def test_linked_analysis_resolves_only_a_signed_analysis_of_the_same_subject(
    stack: RootCauseStack, coordinator: Who
) -> None:
    subject = stack.received()
    analysis_id = stack.with_steps(coordinator, 1, subject)
    signed = stack.signature(coordinator, analysis_id, complete_signature())
    record_id = uuid.UUID(signed.json()["record_id"])
    other = stack.received()
    draft = stack.with_steps(coordinator, 1, other)
    site = stack.world.a.site
    context = stack.context(coordinator)
    foreign = stack.context(stack.member(stack.world.b, Role.COORDINATOR_SST))

    async def resolve(ctx: Any, subject_id: uuid.UUID, record: uuid.UUID) -> uuid.UUID | str:
        async with stack.database.transaction(ctx) as transaction:
            try:
                return await linked_analysis(
                    transaction,
                    plant_id=site.plant_id,
                    zone_id=site.zone_id,
                    subject_id=subject_id,
                    record_id=record,
                )
            except ResourceNotFound:
                return "not_found"

    assert stack.run(resolve(context, subject.subject_id, record_id)) == uuid.UUID(analysis_id)
    assert stack.run(resolve(context, other.subject_id, record_id)) == "not_found"
    assert stack.run(resolve(context, subject.subject_id, uuid.uuid4())) == "not_found"
    assert stack.run(resolve(foreign, subject.subject_id, record_id)) == "not_found"
    assert _row(stack, draft)["record_id"] is None
