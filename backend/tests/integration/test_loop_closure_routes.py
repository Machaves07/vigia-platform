"""SUP-U05-11 y 14: el cierre firmado y la reapertura (TASK-343, VIG-233; LC-LAZ-09; BR-LAZ-36,
42, 43 y 50; F02 a F04).

Sobre PostgreSQL 16 real como ``vigia_app`` y la aplicación real (``actions_stack``), con la
política de texto libre de producción:

- **T09** (``action_assigned``/``overdue`` → ``closed``): el registro ``closure``, su evento
  ``finding_closed`` y la proyección en la misma transacción; sin evidencia,
  ``closure_evidence_missing``; sin licencia, ``signer_license_missing``; desde cualquier otro
  estado, ``closure_requires_assigned_action`` o ``transition_not_allowed`` según la tabla de
  §4.1 y §4.2, sin efecto.
- **El cierre no depende del almacén de adjuntos** (FS-LAZ-03, NFR-LAZ-37): con el almacén caído
  se firma igual, sin ninguna llamada al almacén.
- **T13/T14** (``closed``/``unclosable`` → ``action_assigned``): el registro ``reopening``, su
  evento ``finding_reopened`` y el ``action_assigned`` de la acción nueva (con
  ``supersedes_action_id``), en la misma transacción; el registro anterior (``closure`` o
  ``unclosable_state_declared``) queda íntegro, byte a byte; sin motivo,
  ``reopening_reason_required``; desde cualquier otro estado, ``transition_not_allowed``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import IngestedSubject
from tests.loop_actions_support import ActionsStack, Who, actions_stack

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[ActionsStack]:
    with actions_stack(postgres_endpoint, "loop_closure_routes") as built:
        yield built


@pytest.fixture
def coordinator(stack: ActionsStack) -> Who:
    return stack.member(stack.world.a)


@pytest.fixture
def assignee(stack: ActionsStack) -> uuid.UUID:
    return stack.user(stack.world.a)


def _error(response: object) -> tuple[int, str, str | None]:
    body = response.json()  # type: ignore[attr-defined]
    return response.status_code, body.get("code"), body.get("detail_code")  # type: ignore[attr-defined]


# --- T09: el cierre firmado -----------------------------------------------------------------


def test_close_writes_closure_record_event_and_projection(
    stack: ActionsStack, coordinator: Who, assignee: uuid.UUID
) -> None:
    finding, action_id = stack.with_evidence(coordinator, assignee)
    response = stack.close(coordinator, finding, stack.closure_body())
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["lifecycle_state"], body["action_id"]) == ("closed", str(action_id))
    (record,) = stack.records_of(finding, "closure")
    assert stack.events(finding.organization, record["record_id"]) == ["finding_closed"]
    row = stack.world.finding_state(finding)
    assert row is not None
    assert row["closure_record_id"] == record["record_id"]


def test_close_without_evidence_rejects_with_closure_evidence_missing(
    stack: ActionsStack, coordinator: Who, assignee: uuid.UUID
) -> None:
    finding, _action_id = stack.assigned(coordinator, assignee)
    before = stack.untouched(finding)
    response = stack.close(coordinator, finding, stack.closure_body())
    assert _error(response) == (400, "invalid_request", "loop_closure_evidence_missing")
    assert stack.untouched(finding) == before


def test_close_without_license_rejects_with_signer_license_missing(
    stack: ActionsStack, coordinator: Who, assignee: uuid.UUID
) -> None:
    finding, _action_id = stack.with_evidence(coordinator, assignee)
    before = stack.untouched(finding)
    for license_ in (None, "", "   "):
        response = stack.close(
            coordinator, finding, stack.closure_body(professional_license=license_)
        )
        assert _error(response)[1:] == ("invalid_request", "loop_signer_license_missing")
    assert stack.untouched(finding) == before
    # La licencia sí válida cierra, para dejar la fila lista a otras pruebas del módulo.
    response = stack.close(coordinator, finding, stack.closure_body())
    assert response.status_code == 201, response.text


def test_close_with_attachment_store_down_still_succeeds(
    stack: ActionsStack, coordinator: Who, assignee: uuid.UUID
) -> None:
    """FS-LAZ-03, NFR-LAZ-37: el cierre no sube ni verifica ningún adjunto."""
    finding, _action_id = stack.with_evidence(coordinator, assignee)
    stack.store.down = True
    try:
        before_calls = list(stack.store.calls)
        response = stack.close(coordinator, finding, stack.closure_body())
        assert response.status_code == 201, response.text
        assert stack.store.calls == before_calls, "el cierre no debe tocar el almacén de adjuntos"
    finally:
        stack.store.down = False


# --- F02, F03, F04 y transition_not_allowed: la tabla completa -------------------------------


def _discarded(stack: ActionsStack, coordinator: Who) -> IngestedSubject:
    finding = stack.received()
    stack.tick()
    context = stack.context(coordinator)
    stack.run(
        stack.classification.classify(
            context,
            stack.finding_state_id(finding),
            outcome="false_positive",
            reason_category="no_person_present",
            reason_text="No se observó ninguna persona en el cruce de la zona",
            reinforced_reason=False,
        )
    )
    return finding


def _suspended_by_reclassification(
    stack: ActionsStack, coordinator: Who, assignee: uuid.UUID
) -> IngestedSubject:
    finding, _action_id = stack.assigned(coordinator, assignee)
    stack.tick()
    context = stack.context(coordinator)
    stack.run(
        stack.classification.classify(
            context,
            stack.finding_state_id(finding),
            outcome="false_positive",
            reason_category="no_person_present",
            reason_text="Revisión posterior: no había ninguna persona en el cruce",
            reinforced_reason=True,
        )
    )
    return finding


def _in_state(
    stack: ActionsStack, coordinator: Who, assignee: uuid.UUID, state: str
) -> IngestedSubject:
    match state:
        case "received":
            return stack.received()
        case "in_review":
            return stack.in_review()
        case "open":
            return stack.opened()
        case "discarded":
            return _discarded(stack, coordinator)
        case "closed":
            finding, _action_id = stack.closed(coordinator, assignee)
            return finding
        case "unclosable":
            finding = stack.opened()
            stack.write_unclosable(finding)
            return finding
        case "suspended_by_reclassification":
            return _suspended_by_reclassification(stack, coordinator, assignee)
    raise AssertionError(state)  # pragma: no cover - parametrización cerrada


@pytest.mark.parametrize(
    ("state", "detail_code"),
    [
        ("received", "loop_closure_requires_assigned_action"),
        ("in_review", "loop_closure_requires_assigned_action"),
        ("open", "loop_closure_requires_assigned_action"),
        ("discarded", "loop_closure_requires_assigned_action"),
        ("closed", "loop_transition_not_allowed"),
        ("unclosable", "loop_transition_not_allowed"),
        ("suspended_by_reclassification", "loop_transition_not_allowed"),
    ],
)
def test_close_from_every_other_state_is_rejected_without_side_effects(
    stack: ActionsStack, coordinator: Who, assignee: uuid.UUID, state: str, detail_code: str
) -> None:
    finding = _in_state(stack, coordinator, stack.user(stack.world.a), state)
    before = stack.untouched(finding)
    response = stack.close(coordinator, finding, stack.closure_body())
    assert _error(response) == (409, "conflict", detail_code), (state, response.text)
    assert stack.untouched(finding) == before


# --- T13 y T14: la reapertura -----------------------------------------------------------------


def test_reopening_from_closed_keeps_the_closure_record_byte_for_byte(
    stack: ActionsStack, coordinator: Who, assignee: uuid.UUID
) -> None:
    finding, first_action_id = stack.closed(coordinator, assignee)
    (closure_before,) = stack.records_of(finding, "closure")
    other_assignee = stack.user(stack.world.a)

    response = stack.reopen(coordinator, finding, stack.reopening_body(other_assignee))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["from_state"] == "closed"
    assert body["supersedes_action_id"] == str(first_action_id)
    assert body["lifecycle_state"] == "action_assigned"
    new_action_id = uuid.UUID(body["action_id"])
    assert new_action_id != first_action_id

    (closure_after,) = stack.records_of(finding, "closure")
    assert closure_after == closure_before, "el cierre anterior se editó (viola P4 y BR-LAZ-50)"

    (reopening_record,) = stack.records_of(finding, "reopening")
    assert reopening_record["content"]["previous_closure_id"] == str(closure_before["record_id"])
    assert "previous_declaration_id" not in reopening_record["content"]
    assert stack.events(finding.organization, reopening_record["record_id"]) == ["finding_reopened"]
    (new_action_record,) = [
        r
        for r in stack.records_of(finding, "action_assigned")
        if r["content"]["action_id"] == str(new_action_id)
    ]
    assert new_action_record["content"]["supersedes_action_id"] == str(first_action_id)
    assert stack.events(finding.organization, new_action_record["record_id"]) == ["action_assigned"]

    states = {str(a["action_id"]): a["state"] for a in stack.action_rows(finding)}
    assert states == {str(first_action_id): "superseded", str(new_action_id): "assigned"}

    row = stack.world.finding_state(finding)
    assert row is not None
    assert (row["lifecycle_state"], row["current_action_id"]) == (
        "action_assigned",
        new_action_id,
    )
    assert row["closure_record_id"] is None
    assert row["closed_at"] is None


def test_reopening_from_unclosable_cites_the_declaration_and_has_no_previous_action(
    stack: ActionsStack, coordinator: Who, assignee: uuid.UUID
) -> None:
    finding = stack.opened()
    declaration_id = stack.write_unclosable(finding)

    response = stack.reopen(coordinator, finding, stack.reopening_body(assignee))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["from_state"] == "unclosable"
    assert body["supersedes_action_id"] is None
    assert body["previous_declaration_id"] == str(declaration_id)
    assert body["previous_closure_id"] is None

    (reopening_record,) = stack.records_of(finding, "reopening")
    assert reopening_record["content"]["previous_declaration_id"] == str(declaration_id)
    assert "previous_closure_id" not in reopening_record["content"]
    (new_action_record,) = stack.records_of(finding, "action_assigned")
    assert "supersedes_action_id" not in new_action_record["content"]


def test_reopening_without_reason_is_rejected_without_side_effects(
    stack: ActionsStack, coordinator: Who, assignee: uuid.UUID
) -> None:
    finding, _action_id = stack.closed(coordinator, assignee)
    before = stack.untouched(finding)
    for reason in (None, "", "   "):
        response = stack.reopen(
            coordinator, finding, stack.reopening_body(assignee, reason_text=reason)
        )
        assert _error(response) == (
            400,
            "invalid_request",
            "loop_reopening_reason_required",
        ), (reason, response.text)
    # Un motivo presente pero más corto que el mínimo es un defecto de forma, no de negocio.
    response = stack.reopen(
        coordinator, finding, stack.reopening_body(assignee, reason_text="corto")
    )
    assert _error(response)[1:] == ("invalid_request", None), response.text
    assert stack.untouched(finding) == before


@pytest.mark.parametrize(
    "state",
    ["received", "in_review", "open", "discarded", "suspended_by_reclassification"],
)
def test_reopening_from_any_state_other_than_closed_or_unclosable_is_not_allowed(
    stack: ActionsStack, coordinator: Who, assignee: uuid.UUID, state: str
) -> None:
    finding = _in_state(stack, coordinator, stack.user(stack.world.a), state)
    before = stack.untouched(finding)
    response = stack.reopen(coordinator, finding, stack.reopening_body(assignee))
    assert _error(response) == (409, "conflict", "loop_transition_not_allowed"), (
        state,
        response.text,
    )
    assert stack.untouched(finding) == before
