"""SUP-U05-07 y 08: la acción correctiva y su evidencia (TASK-311; LC-LAZ-09; BR-LAZ-36 a 39, 42,
49, 51 y 154).

Sobre PostgreSQL 16 real como ``vigia_app`` y la aplicación real (``actions_stack``), con la
política de texto libre de producción (los dos validadores reales de U-04) y el
``ClosureAttachmentPort`` real de TASK-310 sobre un almacén en memoria:

- **T07** (``open → action_assigned``): el registro ``action_assigned``, su evento, la fila de
  ``loop.corrective_action`` y la proyección en la misma transacción; el plazo en días hábiles del
  calendario de la planta. Desde ``received``, ``in_review``, ``closed`` o ``discarded`` (y desde
  cualquier estado sin fila T07 en la tabla de TASK-305), ``transition_not_allowed`` sin efecto.
- **Tipo, textos y responsable**: fuera de la lista, ``action_type_not_allowed``; una acción sobre
  una persona, ``action_on_person_forbidden``; los validadores, su código; un responsable sin
  alcance sobre la zona, desactivado o de otra organización responde igual que uno inexistente
  (``not_found``, BR-NUC-09). **Nada escrito** en ningún rechazo.
- **Evidencia**: sin descripción estructurada, ``invalid_request``; un adjunto cuyo hash no
  coincide, ``attachment_hash_mismatch`` y nada escrito; sin declaración, su código; con el almacén
  caído, la entrada sin adjuntos se escribe y la que lleva adjuntos responde
  ``storage_unavailable``; los topes de 5 por entrada, 20 por hallazgo y 20 entradas por acción.
- **Responsable desactivado** (BR-LAZ-49 y 154): la acción sigue viva y sigue recibiendo
  evidencia; reasignar escribe un ``action_assigned`` nuevo con ``supersedes_action_id``, la
  anterior queda ``superseded`` (igualdad de registros) y el estado no cambia, tampoco desde
  ``overdue``.
- **Concurrencia**: dos asignaciones simultáneas del mismo hallazgo abierto dan un ganador y un
  ``transition_not_allowed``; dos entradas simultáneas nunca pasan los topes.
- **Alcance**: otra organización, otra zona, un rol sin ``actions.manage`` y la concesión de
  proveedor responden ``not_found`` sin escribir.

La prueba cruzada del orden de candados está en ``test_loop_locks_order.py``. Solo datos
generados.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any, Final

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import IngestedSubject
from tests.loop_actions_support import DESCRIPTION, ActionsStack, Who, actions_stack
from vigia_platform.identity.authz.authorize import Resource
from vigia_platform.identity.authz.context import with_unit
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.loop.adapters.attachments.rules import AttachmentRejected
from vigia_platform.loop.adapters.attachments.verify import AttachmentClaim
from vigia_platform.loop.application.actions import (
    ActionRejected,
    ActionRequestInvalid,
    ActionService,
    AssignmentRequest,
    AssignmentResult,
    action_rows,
    read_finding,
)
from vigia_platform.loop.application.evidence import EvidenceResult, EvidenceService
from vigia_platform.loop.application.locks import lock_actions, lock_finding
from vigia_platform.loop.calendar import BusinessCalendar, business_deadline
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.domain.enums import FindingLifecycleState
from vigia_platform.loop.domain.lifecycle import (
    TRANSITIONS,
    AssignAction,
    LifecycleSnapshot,
    Rejected,
    transition,
)
from vigia_platform.shared.context import ActorUnit, Role, ScopeLevel
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration

LOCK_WAIT_SECONDS: Final = 60.0
"""Tope para ver una transacción esperando un candado (no decide el resultado)."""
S = FindingLifecycleState

_CONFLICT: Final = (409, "conflict", "loop_transition_not_allowed")
_INVALID: Final = (400, "invalid_request", None)
_NOT_FOUND: Final = (404, "not_found", None)
_ZWSP: Final = chr(0x200B)
"""Espacio de ancho cero: la política base de U-02 lo rechaza (``invisible_character``)."""


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[ActionsStack]:
    with actions_stack(postgres_endpoint, "loop_actions_routes") as built:
        yield built


@pytest.fixture(autouse=True)
def _release(stack: ActionsStack) -> Iterator[None]:
    stack.store.down = False
    yield
    stack.store.down = False
    stack.world.stack.run(stack.world.stack.release())


@pytest.fixture(scope="module")
def coordinator(stack: ActionsStack) -> Who:
    return stack.member(stack.world.a)


@pytest.fixture(scope="module")
def assignee(stack: ActionsStack) -> uuid.UUID:
    return stack.user(stack.world.a, Role.COORDINATOR_SST, ScopeLevel.ZONE, stack.world.a.zone_id)


def _error(response: Any) -> tuple[int, str | None, str | None]:
    body = response.json()
    return response.status_code, body.get("code"), body.get("detail_code")


def _comparable(response: Any) -> tuple[int, Any]:
    body = response.json()
    return response.status_code, {k: v for k, v in body.items() if k != "correlation_id"}


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _assigned(stack: ActionsStack, who: Who, subject: IngestedSubject, assignee: uuid.UUID) -> Any:
    response = stack.assign(who, subject, stack.action_body(assignee))
    assert response.status_code == 201, response.text
    return response.json()


def _timezone(stack: ActionsStack) -> str:
    (row,) = stack.fetch(
        "SELECT timezone FROM identity.plant WHERE plant_id = $1", stack.world.a.plant_id
    )
    return str(row["timezone"])


# --- T07 ------------------------------------------------------------------------------------------


def test_t07_writes_record_event_row_and_projection_in_one_transaction(
    stack: ActionsStack, coordinator: Who, assignee: uuid.UUID
) -> None:
    finding = stack.opened()
    response = stack.assign(coordinator, finding, stack.action_body(assignee))
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["action_type"], body["lifecycle_state"]) == ("engineering", "action_assigned")
    assert body["assignee_user_id"] == str(assignee)
    assert body["supersedes_action_id"] is None
    assert body["deadline_days"] == 30  # action_deadline_default_days por defecto
    (record,) = stack.records_of(finding, "action_assigned")
    record_id = uuid.UUID(body["record_id"])
    assert record["record_id"] == record_id
    content = record["content"]
    assert content["action_id"] == body["action_id"]
    assert content["action_description"] == DESCRIPTION
    assert content["assignee_display_name"]
    assert content["assigned_by"]["role_in_use"] == "coordinator_sst"
    assert content["anchor_record_id"] == str(finding.record_id)
    assert "supersedes_action_id" not in content
    # El plazo: fin del día hábil 30 posterior a la fecha local de la asignación.
    assigned_at = _instant(content["assigned_at"])
    due_at = business_deadline(assigned_at, 30, BusinessCalendar.default(), _timezone(stack))
    assert content["due_at"] == body["due_at"] == format_timestamp(due_at)
    assert stack.events(finding.organization, record_id) == ["action_assigned"]
    (row,) = stack.action_rows(finding)
    assert (row["action_id"], row["record_id"], row["state"]) == (
        uuid.UUID(body["action_id"]),
        record_id,
        "assigned",
    )
    assert (row["due_at"], row["assignee_user_id"], row["action_type"]) == (
        due_at,
        assignee,
        "engineering",
    )
    projection = stack.world.finding_state(finding)
    assert projection is not None
    assert projection["lifecycle_state"] == "action_assigned"
    assert projection["current_action_id"] == uuid.UUID(body["action_id"])
    assert (projection["due_at"], projection["assignee_user_id"]) == (due_at, assignee)
    assert projection["last_record_id"] == record_id


def _closed(stack: ActionsStack, coordinator: Who, assignee: uuid.UUID) -> IngestedSubject:
    finding = stack.opened()
    stack.write_closure(finding, _assigned(stack, coordinator, finding, assignee)["action_id"])
    return finding


def _discarded(stack: ActionsStack) -> IngestedSubject:
    finding = stack.received()
    stack.classify(finding, "false_positive", "no_person_present")
    return finding


@pytest.mark.parametrize("state", ["received", "in_review", "closed", "discarded"])
def test_assign_outside_open_is_transition_not_allowed_without_effect(
    stack: ActionsStack, coordinator: Who, assignee: uuid.UUID, state: str
) -> None:
    # La tabla de TASK-305 no tiene T07 desde estos estados: la ruta da el mismo rechazo.
    assert isinstance(transition(LifecycleSnapshot(S(state)), AssignAction()), Rejected)
    if state == "received":
        subject = stack.received()
    elif state == "in_review":
        subject = stack.in_review()
    elif state == "closed":
        subject = _closed(stack, coordinator, assignee)
    else:
        subject = _discarded(stack)
    row = stack.world.finding_state(subject)
    assert row is not None and row["lifecycle_state"] == state
    before = stack.untouched(subject)
    response = stack.assign(coordinator, subject, stack.action_body(assignee))
    assert _error(response) == _CONFLICT, response.text
    assert stack.untouched(subject) == before


def test_only_open_has_a_t07_row_in_the_table() -> None:
    (t07,) = [row for row in TRANSITIONS.values() if "action_assigned" in row.record_types[:1]]
    assert t07.transition_id == "T07" and t07.origins == frozenset({S.OPEN})
    for state in S:
        result = transition(LifecycleSnapshot(state), AssignAction())
        assert isinstance(result, Rejected) == (state is not S.OPEN), state


def test_assign_on_an_assigned_finding_without_supersedes_is_a_conflict(
    stack: ActionsStack, coordinator: Who, assignee: uuid.UUID
) -> None:
    finding = stack.opened()
    _assigned(stack, coordinator, finding, assignee)
    before = stack.untouched(finding)
    response = stack.assign(coordinator, finding, stack.action_body(assignee))
    assert _error(response) == _CONFLICT
    assert stack.untouched(finding) == before


# --- tipo, textos y responsable ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"action_type": "training"}, (400, "invalid_request", "loop_action_type_not_allowed")),
        ({"action_type": "Engineering"}, (400, "invalid_request", "loop_action_type_not_allowed")),
        ({"action_type": ""}, (400, "invalid_request", "loop_action_type_not_allowed")),
        ({"action_type": "sanction"}, (400, "invalid_request", "loop_action_type_not_allowed")),
        (
            {"action_description": "Capacitar al operario en el uso del resguardo"},
            (400, "invalid_request", "loop_action_on_person_forbidden"),
        ),
        (
            {"expected_outcome": "Que el personal sea amonestado si cruza la zona"},
            (400, "invalid_request", "loop_action_on_person_forbidden"),
        ),
        (
            {"action_description": "Instalar un resguardo porque el operario omitió el bloqueo"},
            (400, "invalid_request", "loop_person_attribution_detected"),
        ),
        (
            {"expected_outcome": "Corregir la falta de atención en el cruce del pasillo"},
            (400, "invalid_request", "loop_person_attribution_detected"),
        ),
        (
            {"action_description": "Instalar un resguardo que garantice cero riesgo en la zona"},
            (400, "invalid_request", "loop_blocked_vocabulary_term"),
        ),
        ({"action_description": "x" * 19}, _INVALID),
        ({"action_description": "Instalar resguardo " + "x" * 1982}, _INVALID),
        ({"expected_outcome": ""}, _INVALID),
        ({"deadline_days": "30"}, _INVALID),
        ({"deadline_days": 30.0}, _INVALID),
        ({"assignee_user_id": "no-es-un-uuid"}, _INVALID),
        ({"person": "x"}, _INVALID),
        ({"action_description": f"Instalar un resguardo{_ZWSP} fijo en la zona"}, _INVALID),
    ],
)
def test_type_texts_and_shape_are_rejected_without_writing(
    stack: ActionsStack,
    coordinator: Who,
    assignee: uuid.UUID,
    changes: dict[str, Any],
    expected: tuple[int, str, str | None],
) -> None:
    finding = stack.opened()
    before = stack.untouched(finding)
    response = stack.assign(coordinator, finding, {**stack.action_body(assignee), **changes})
    assert _error(response) == expected, response.text
    assert stack.untouched(finding) == before


_ROLE_ACTION_SUGGESTION: Final = (
    "Describa la condición de la zona, del equipo o del procedimiento, no lo que hizo una persona."
)
"""La alternativa del patrón ``role_action`` del validador real (BLM §14.5)."""
_HUMAN_ERROR_SUGGESTION: Final = (
    "Describa qué condición del proceso o de la ingeniería permitió el evento."
)
"""La del patrón ``human_error``."""
_MEASURED_SUGGESTION: Final = (
    "Cita la cifra medida, con su periodo y su fuente, en lugar de prometerla."
)
"""La de ``garantizar`` en la lista inicial (§14.2)."""


@pytest.mark.parametrize(
    ("changes", "rejection"),
    [
        (
            {"action_description": "Instalar un resguardo porque el operario omitió el bloqueo"},
            {"code": "person_attribution_detected", "suggestion": _ROLE_ACTION_SUGGESTION},
        ),
        (
            {"expected_outcome": "Corregir la falta de atención en el cruce del pasillo"},
            {"code": "person_attribution_detected", "suggestion": _HUMAN_ERROR_SUGGESTION},
        ),
        (
            {"action_description": "Instalar un resguardo que garantice cero riesgo en la zona"},
            {
                "code": "blocked_vocabulary_term",
                "term": "garantice",
                "suggestion": _MEASURED_SUGGESTION,
            },
        ),
    ],
)
def test_a_validator_rejection_names_the_term_and_the_suggestion(
    stack: ActionsStack,
    coordinator: Who,
    assignee: uuid.UUID,
    changes: dict[str, Any],
    rejection: dict[str, str],
) -> None:
    """SUP-U05-07 (VIG-344): ``rejection`` exacto con los validadores reales; la atribución
    nunca repite lo que coincidió (P3)."""
    finding = stack.opened()
    response = stack.assign(coordinator, finding, {**stack.action_body(assignee), **changes})
    assert response.status_code == 400, response.text
    assert response.json()["rejection"] == rejection


@pytest.mark.parametrize(
    ("description", "expected_outcome"),
    [("Instalar resguardo x", "y"), ("Instalar resguardo " + "x" * 1981, "y" * 1000)],
)
def test_text_borders_are_accepted(
    stack: ActionsStack,
    coordinator: Who,
    assignee: uuid.UUID,
    description: str,
    expected_outcome: str,
) -> None:
    assert len(description) in (20, 2000)
    finding = stack.opened()
    body = stack.action_body(
        assignee, action_description=description, expected_outcome=expected_outcome
    )
    response = stack.assign(coordinator, finding, body)
    assert response.status_code == 201, response.text
    (record,) = stack.records_of(finding, "action_assigned")
    assert record["content"]["action_description"] == description
    assert record["content"]["expected_outcome"] == expected_outcome


def test_an_assignee_without_scope_answers_like_a_missing_one(
    stack: ActionsStack, coordinator: Who
) -> None:
    world = stack.world
    site = world.a.site
    finding = stack.opened()
    before = stack.untouched(finding)
    deactivated = stack.user(world.a)
    stack.deactivate(deactivated)
    invited = stack.world.stack.authz.add_user(world.a.organization_id, status="invited")
    stack.world.stack.authz.assign(world.a.organization_id, invited, Role.COORDINATOR_SST)
    other_plant = world.add_plant(world.a)
    missing = stack.assign(coordinator, finding, stack.action_body(uuid.uuid4()))
    assert _error(missing) == _NOT_FOUND
    for user in (
        stack.user(world.a, Role.COORDINATOR_SST, ScopeLevel.ZONE, site.other_zone_id),
        stack.user(world.a, Role.COORDINATOR_SST, ScopeLevel.PLANT, other_plant),
        stack.user(world.b),
        deactivated,
        invited,
        stack.world.stack.authz.operator_id,
    ):
        response = stack.assign(coordinator, finding, stack.action_body(user))
        assert _comparable(response) == _comparable(missing), user
    assert stack.untouched(finding) == before
    # La organización, la planta de la zona y la propia zona sí cubren la zona.
    for user in (
        stack.user(world.a, Role.LINE_MANAGER),
        stack.user(world.a, Role.PLANT_MANAGER, ScopeLevel.PLANT, world.a.plant_id),
        stack.user(world.a, Role.COORDINATOR_SST, ScopeLevel.ZONE, site.zone_id),
    ):
        accepted = stack.assign(coordinator, stack.opened(), stack.action_body(user))
        assert accepted.status_code == 201, accepted.text


def test_out_of_scope_findings_answer_like_missing_and_write_nothing(
    stack: ActionsStack, coordinator: Who, assignee: uuid.UUID
) -> None:
    world = stack.world
    site = world.a.site
    foreign = stack.opened(world.b)
    foreign_before = stack.untouched(foreign)
    body = stack.action_body(assignee)
    known = stack.assign(coordinator, foreign, body)
    missing = stack.assign(coordinator, uuid.uuid4(), body)
    assert _error(known) == _NOT_FOUND
    assert _comparable(known) == _comparable(missing)
    assert stack.untouched(foreign) == foreign_before
    finding = stack.opened()
    before = stack.untouched(finding)
    for who in (
        stack.member(world.a, Role.COORDINATOR_SST, ScopeLevel.ZONE, site.other_zone_id),
        stack.member(world.a, Role.PLANT_MANAGER),
        stack.member(world.a, Role.ADMINISTRATOR),
        stack.member(world.a, Role.LINE_MANAGER),
        stack.installer(world.a),
    ):
        response = stack.assign(who, finding, body)
        assert _error(response) == _NOT_FOUND, response.text
        assert _comparable(response) == _comparable(stack.assign(who, uuid.uuid4(), body))
    assert stack.untouched(finding) == before
    zone_coordinator = stack.member(world.a, Role.COORDINATOR_SST, ScopeLevel.ZONE, site.zone_id)
    assert stack.assign(zone_coordinator, finding, body).status_code == 201


# --- evidencia ------------------------------------------------------------------------------------


def test_an_entry_without_attachments_writes_record_event_and_row_without_moving_the_state(
    stack: ActionsStack, coordinator: Who, assignee: uuid.UUID
) -> None:
    finding = stack.opened()
    action_id = _assigned(stack, coordinator, finding, assignee)["action_id"]
    before = stack.world.finding_state(finding)
    checkpoint = stack.world.checkpoint(finding.organization)
    assert before is not None and checkpoint is not None
    response = stack.add_evidence(coordinator, action_id, stack.evidence_body())
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["action_id"], body["lifecycle_state"], body["attachment_ids"]) == (
        action_id,
        "action_assigned",
        [],
    )
    (record,) = stack.records_of(finding, "action_evidence_added")
    record_id = uuid.UUID(body["record_id"])
    assert record["record_id"] == record_id
    content = record["content"]
    assert (content["evidence_entry_id"], content["action_id"]) == (
        body["evidence_entry_id"],
        action_id,
    )
    assert content["performed_by_role"] == "Técnico de mantenimiento"
    assert content["attachments"] == []
    assert content["added_by"]["role_in_use"] == "coordinator_sst"
    assert stack.events(finding.organization, record_id) == ["action_evidence_added"]
    (entry,) = stack.entry_rows(action_id)
    assert (entry["record_id"], entry["attachment_ids"]) == (record_id, [])
    # La entrada no es una transición: la fila del hallazgo no cambia; el control avanza.
    assert stack.world.finding_state(finding) == before
    after = stack.world.checkpoint(finding.organization)
    assert after is not None
    assert after["last_applied_ledger_sequence"] == record["chain_sequence"]


@pytest.mark.parametrize(
    "changes",
    [
        {"description": None},
        {"performed_at": None},
        {"performed_by_role": None},
        {"description": "Corto"},
        {"description": "x" * 2001},
        {"performed_by_role": "x" * 65},
        {"performed_by_role": ""},
        {"verification_note": ""},
        {"performed_at": "2026-10-18T10:00:00Z"},
        {"performed_at": "2026-10-18 10:00:00.000Z"},
        {"performed_at": "2026-02-30T10:00:00.000Z"},
        {"description": f"Resguardo{_ZWSP} instalado y anclado al piso en el acceso"},
        {"extra": "x"},
    ],
)
def test_without_the_structured_description_nothing_is_written(
    stack: ActionsStack, coordinator: Who, assignee: uuid.UUID, changes: dict[str, Any]
) -> None:
    finding = stack.opened()
    action_id = _assigned(stack, coordinator, finding, assignee)["action_id"]
    before = stack.untouched(finding)
    body = {**stack.evidence_body(), **changes}
    body = {key: value for key, value in body.items() if value is not None}
    response = stack.add_evidence(coordinator, action_id, body)
    assert _error(response) == _INVALID, response.text
    assert stack.untouched(finding) == before


def test_performed_at_after_the_entry_is_rejected(
    stack: ActionsStack, coordinator: Who, assignee: uuid.UUID
) -> None:
    finding = stack.opened()
    action_id = _assigned(stack, coordinator, finding, assignee)["action_id"]
    before = stack.untouched(finding)
    future = format_timestamp(stack.now() + timedelta(hours=1))
    response = stack.add_evidence(coordinator, action_id, stack.evidence_body(performed_at=future))
    assert _error(response) == _INVALID
    assert stack.untouched(finding) == before


@pytest.mark.parametrize(
    ("text", "code", "rejection"),
    [
        (
            "Resguardo instalado porque el operario omitió el bloqueo",
            "person_attribution",
            {"code": "person_attribution_detected", "suggestion": _ROLE_ACTION_SUGGESTION},
        ),
        (
            "Resguardo instalado que garantiza cero riesgo en la zona",
            "blocked_vocabulary",
            {
                "code": "blocked_vocabulary_term",
                "term": "garantiza",
                "suggestion": _MEASURED_SUGGESTION,
            },
        ),
    ],
)
def test_the_validators_reject_the_entry_text_without_writing(
    stack: ActionsStack,
    coordinator: Who,
    assignee: uuid.UUID,
    text: str,
    code: str,
    rejection: dict[str, str],
) -> None:
    finding = stack.opened()
    action_id = _assigned(stack, coordinator, finding, assignee)["action_id"]
    before = stack.untouched(finding)
    for field in ("description", "verification_note"):
        response = stack.add_evidence(coordinator, action_id, stack.evidence_body(**{field: text}))
        expected = (
            "loop_person_attribution_detected"
            if code == "person_attribution"
            else "loop_blocked_vocabulary_term"
        )
        assert _error(response) == (400, "invalid_request", expected), (field, response.text)
        assert response.json()["rejection"] == rejection, field  # VIG-344 (SUP-U05-08)
    assert stack.untouched(finding) == before


def test_verified_attachments_are_recorded_with_their_digest_and_declaration(
    stack: ActionsStack, coordinator: Who, assignee: uuid.UUID
) -> None:
    finding = stack.opened()
    action_id = _assigned(stack, coordinator, finding, assignee)["action_id"]
    claims = [stack.uploaded(finding), stack.uploaded(finding)]
    response = stack.add_evidence(coordinator, action_id, stack.evidence_body(attachments=claims))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["attachment_ids"] == [claim["attachment_id"] for claim in claims]
    (record,) = stack.records_of(finding, "action_evidence_added")
    assert [item["sha256"] for item in record["content"]["attachments"]] == [
        claim["sha256"] for claim in claims
    ]
    assert all(item["no_identifiable_person_declared"] for item in record["content"]["attachments"])
    rows = stack.attachment_rows(finding)
    assert {str(row["attachment_id"]) for row in rows} == set(body["attachment_ids"])
    for row in rows:
        assert row["no_identifiable_person_declared"] is True
        assert row["verified_at"] == row["uploaded_at"]
        key = f"/closure/{finding.subject_id}/{row['attachment_id']}.png"
        assert str(row["storage_key"]).endswith(key)
    (entry,) = stack.entry_rows(action_id)
    assert [str(item) for item in entry["attachment_ids"]] == body["attachment_ids"]
    assert stack.events(finding.organization, uuid.UUID(body["record_id"])) == [
        "action_evidence_added"
    ]


@pytest.mark.parametrize(
    ("claim_changes", "code"),
    [
        ({"sha256": "0" * 64}, "loop_attachment_hash_mismatch"),
        ({"size_bytes": 7}, "loop_attachment_hash_mismatch"),
        ({"no_identifiable_person_declared": False}, "loop_attachment_declaration_missing"),
        ({"content_type": "image/gif"}, "loop_attachment_type_not_allowed"),
    ],
)
def test_a_rejected_attachment_writes_nothing(
    stack: ActionsStack,
    coordinator: Who,
    assignee: uuid.UUID,
    claim_changes: dict[str, Any],
    code: str,
) -> None:
    finding = stack.opened()
    action_id = _assigned(stack, coordinator, finding, assignee)["action_id"]
    before = stack.untouched(finding)
    claims = [stack.uploaded(finding), stack.uploaded(finding, **claim_changes)]
    response = stack.add_evidence(coordinator, action_id, stack.evidence_body(attachments=claims))
    assert _error(response) == (400, "invalid_request", code), response.text
    # Ni registro, ni evento, ni proyección, ni filas de entrada o de adjunto.
    assert stack.untouched(finding) == before
    assert stack.attachment_rows(finding) == []


def test_an_attachment_without_declaration_field_is_declaration_missing(
    stack: ActionsStack, coordinator: Who, assignee: uuid.UUID
) -> None:
    finding = stack.opened()
    action_id = _assigned(stack, coordinator, finding, assignee)["action_id"]
    claim = stack.uploaded(finding)
    del claim["no_identifiable_person_declared"]
    response = stack.add_evidence(coordinator, action_id, stack.evidence_body(attachments=[claim]))
    assert _error(response) == (400, "invalid_request", "loop_attachment_declaration_missing")


def test_an_attachment_id_that_no_grant_issued_is_invalid(
    stack: ActionsStack, coordinator: Who, assignee: uuid.UUID
) -> None:
    finding = stack.opened()
    action_id = _assigned(stack, coordinator, finding, assignee)["action_id"]
    before = stack.untouched(finding)
    claim = stack.uploaded(finding, attachment_id=str(uuid.uuid4()))  # v4: ninguna concesión
    response = stack.add_evidence(coordinator, action_id, stack.evidence_body(attachments=[claim]))
    assert _error(response) == _INVALID, response.text
    assert stack.untouched(finding) == before


def test_with_the_store_down_an_entry_without_attachments_is_still_written(
    stack: ActionsStack, coordinator: Who, assignee: uuid.UUID
) -> None:
    finding = stack.opened()
    action_id = _assigned(stack, coordinator, finding, assignee)["action_id"]
    claim = stack.uploaded(finding)
    stack.store.down = True
    calls = len(stack.store.calls)
    before = stack.untouched(finding)
    refused = stack.add_evidence(coordinator, action_id, stack.evidence_body(attachments=[claim]))
    assert _error(refused)[:2] == (503, "storage_unavailable"), refused.text
    assert stack.untouched(finding) == before
    accepted = stack.add_evidence(coordinator, action_id, stack.evidence_body())
    assert accepted.status_code == 201, accepted.text
    # FS-LAZ-03: la entrada sin adjuntos no toca el almacén.
    assert stack.store.calls[calls:] == ["head_object"]
    assert len(stack.records_of(finding, "action_evidence_added")) == 1
    # El circuito quedó abierto: pasado el intervalo de sonda, el almacén vuelve a responder.
    stack.store.down = False
    stack.tick(61)


def test_attachment_limits_per_entry_and_per_finding(
    stack: ActionsStack, coordinator: Who, assignee: uuid.UUID
) -> None:
    finding = stack.opened()
    action_id = _assigned(stack, coordinator, finding, assignee)["action_id"]
    six = [stack.uploaded(finding) for _ in range(6)]
    response = stack.add_evidence(coordinator, action_id, stack.evidence_body(attachments=six))
    assert _error(response) == (400, "invalid_request", "loop_attachment_too_large")
    for _ in range(4):
        five = [stack.uploaded(finding) for _ in range(5)]
        accepted = stack.add_evidence(coordinator, action_id, stack.evidence_body(attachments=five))
        assert accepted.status_code == 201, accepted.text
    assert len(stack.attachment_rows(finding)) == 20
    before = stack.untouched(finding)
    one_more = stack.add_evidence(
        coordinator, action_id, stack.evidence_body(attachments=[stack.uploaded(finding)])
    )
    assert _error(one_more) == (400, "invalid_request", "loop_attachment_too_large")
    assert stack.untouched(finding) == before
    # Sin adjuntos, la entrada sigue cabiendo (el tope es de adjuntos).
    assert stack.add_evidence(coordinator, action_id, stack.evidence_body()).status_code == 201


def test_an_attachment_cannot_be_referenced_by_two_entries(
    stack: ActionsStack, coordinator: Who, assignee: uuid.UUID
) -> None:
    finding = stack.opened()
    action_id = _assigned(stack, coordinator, finding, assignee)["action_id"]
    claim = stack.uploaded(finding)
    first = stack.add_evidence(coordinator, action_id, stack.evidence_body(attachments=[claim]))
    assert first.status_code == 201, first.text
    before = stack.untouched(finding)
    again = stack.add_evidence(coordinator, action_id, stack.evidence_body(attachments=[claim]))
    assert _error(again) == _INVALID
    assert stack.untouched(finding) == before


def test_twenty_entries_per_action(
    stack: ActionsStack, coordinator: Who, assignee: uuid.UUID
) -> None:
    finding = stack.opened()
    action_id = _assigned(stack, coordinator, finding, assignee)["action_id"]
    for _ in range(20):
        response = stack.add_evidence(coordinator, action_id, stack.evidence_body())
        assert response.status_code == 201, response.text
    before = stack.untouched(finding)
    assert _error(stack.add_evidence(coordinator, action_id, stack.evidence_body())) == _INVALID
    assert stack.untouched(finding) == before


def test_evidence_only_for_the_live_action_of_an_active_loop(
    stack: ActionsStack, coordinator: Who, assignee: uuid.UUID
) -> None:
    world = stack.world
    body = stack.evidence_body()
    missing = stack.add_evidence(coordinator, uuid.uuid4(), body)
    assert _error(missing) == _NOT_FOUND
    foreign = stack.opened(world.b)
    foreign_action = _assigned(stack, stack.member(world.b), foreign, stack.user(world.b))
    known = stack.add_evidence(coordinator, foreign_action["action_id"], body)
    assert _comparable(known) == _comparable(missing)
    # Suspendido por reclasificación (T15): el lazo no recibe evidencia mientras tanto.
    suspended = stack.opened()
    action_id = _assigned(stack, coordinator, suspended, assignee)["action_id"]
    stack.classify(suspended, "false_positive", "no_person_present", reinforced=True)
    row = stack.world.finding_state(suspended)
    assert row is not None and row["lifecycle_state"] == "suspended_by_reclassification"
    before = stack.untouched(suspended)
    assert _error(stack.add_evidence(coordinator, action_id, body)) == _CONFLICT
    assert stack.untouched(suspended) == before
    # Cerrado: tampoco.
    closed = stack.opened()
    closed_action = _assigned(stack, coordinator, closed, assignee)["action_id"]
    stack.write_closure(closed, closed_action)
    assert _error(stack.add_evidence(coordinator, closed_action, body)) == _CONFLICT
    # Alcance de la persona: otra zona, otro rol, el instalador bajo concesión.
    live = stack.opened()
    live_action = _assigned(stack, coordinator, live, assignee)["action_id"]
    before = stack.untouched(live)
    for who in (
        stack.member(world.a, Role.COORDINATOR_SST, ScopeLevel.ZONE, world.a.site.other_zone_id),
        stack.member(world.a, Role.PLANT_MANAGER),
        stack.installer(world.a),
    ):
        response = stack.add_evidence(who, live_action, body)
        assert _comparable(response) == _comparable(stack.add_evidence(who, uuid.uuid4(), body))
    assert stack.untouched(live) == before


# --- responsable desactivado y reasignación ------------------------------------------------------


def _kept(stack: ActionsStack, subject: IngestedSubject) -> list[Any]:
    return [
        (r["record_id"], r["chain_sequence"], r["content_hash"], r["record_hash"], r["content"])
        for r in stack.subject_records(subject)
    ]


def test_a_deactivated_assignee_keeps_the_action_alive_and_reassigning_supersedes_it(
    stack: ActionsStack, coordinator: Who
) -> None:
    world = stack.world
    responsible = stack.user(world.a)
    finding = stack.opened()
    first = _assigned(stack, coordinator, finding, responsible)
    stack.deactivate(responsible)
    # El consumidor real de loop_projection (TASK-341) aplica user_deactivated de U-02.
    consumer, _ports = world.projection_consumer()
    event = world.synthetic_event(
        world.a,
        "user_deactivated",
        {
            "user_id": str(responsible),
            "deactivated_by": str(world.stack.authz.operator_id),
            "deactivated_at": world.stack.now().isoformat(),
        },
        plant=False,
    )
    assert world.deliver(consumer, event) == 1
    row = stack.world.finding_state(finding)
    assert row is not None
    assert (row["lifecycle_state"], row["assignee_inactive"]) == ("action_assigned", True)
    assert [a["state"] for a in stack.action_rows(finding)] == ["assigned"]
    # La acción sigue viva: recibe evidencia; nadie la cierra ni la borra.
    alive = stack.add_evidence(coordinator, first["action_id"], stack.evidence_body())
    assert alive.status_code == 201, alive.text
    # Al desactivado no se le asigna nada nuevo.
    to_inactive = stack.assign(
        coordinator,
        finding,
        stack.action_body(responsible, supersedes_action_id=first["action_id"]),
    )
    assert _error(to_inactive) == _NOT_FOUND
    before_records = _kept(stack, finding)
    replacement = stack.user(world.a)
    response = stack.assign(
        coordinator,
        finding,
        stack.action_body(replacement, supersedes_action_id=first["action_id"], deadline_days=10),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["supersedes_action_id"], body["lifecycle_state"]) == (
        first["action_id"],
        "action_assigned",
    )
    # Igualdad de registros: lo anterior es un prefijo idéntico; solo se añade la reasignación.
    after_records = _kept(stack, finding)
    assert after_records[: len(before_records)] == before_records
    (added,) = after_records[len(before_records) :]
    assert added[4]["supersedes_action_id"] == first["action_id"]
    assert added[4]["deadline_days"] == 10
    states = {str(a["action_id"]): a["state"] for a in stack.action_rows(finding)}
    assert states == {first["action_id"]: "superseded", body["action_id"]: "assigned"}
    projection = stack.world.finding_state(finding)
    assert projection is not None
    assert projection["lifecycle_state"] == "action_assigned"
    assert projection["current_action_id"] == uuid.UUID(body["action_id"])
    assert (projection["assignee_user_id"], projection["assignee_inactive"]) == (replacement, False)
    # La sustituida ya no recibe evidencia ni se vuelve a sustituir.
    old = stack.add_evidence(coordinator, first["action_id"], stack.evidence_body())
    assert _error(old) == _CONFLICT
    body = stack.action_body(replacement, supersedes_action_id=first["action_id"])
    assert _error(stack.assign(coordinator, finding, body)) == _CONFLICT


def test_reassigning_keeps_the_type_and_a_declared_overdue(
    stack: ActionsStack, coordinator: Who, assignee: uuid.UUID
) -> None:
    finding = stack.opened()
    first = _assigned(stack, coordinator, finding, assignee)
    other_type = stack.assign(
        coordinator,
        finding,
        stack.action_body(assignee, supersedes_action_id=first["action_id"], action_type="process"),
    )
    assert _error(other_type) == (400, "invalid_request", "loop_action_type_not_allowed")
    (action,) = stack.action_rows(finding)
    stack.write_overdue(finding, action)
    overdue = stack.world.finding_state(finding)
    assert overdue is not None and overdue["lifecycle_state"] == "overdue"
    response = stack.assign(
        coordinator,
        finding,
        stack.action_body(stack.user(stack.world.a), supersedes_action_id=first["action_id"]),
    )
    assert response.status_code == 201, response.text
    assert response.json()["lifecycle_state"] == "overdue"
    after = stack.world.finding_state(finding)
    assert after is not None
    assert (after["overdue_since"], after["overdue_days"]) == (
        overdue["overdue_since"],
        overdue["overdue_days"],
    )
    assert after["current_action_id"] == uuid.UUID(response.json()["action_id"])
    # Una acción que no es la vigente no se sustituye.
    unknown = stack.assign(
        coordinator, finding, stack.action_body(assignee, supersedes_action_id=str(uuid.uuid4()))
    )
    assert _error(unknown) == _CONFLICT


def test_reopening_completes_its_pending_action_through_assign_in_transaction(
    stack: ActionsStack, coordinator: Who, assignee: uuid.UUID
) -> None:
    """La segunda mitad de T13 (TASK-343 la invocará): la reapertura ya dejó la acción vigente."""
    finding = stack.opened()
    first = _assigned(stack, coordinator, finding, assignee)
    stack.write_closure(finding, first["action_id"])
    new_action = stack.world.stack.uuid7()
    stack.write_record(
        finding,
        "reopening",
        {
            "reopening_id": str(stack.world.stack.uuid7()),
            "subject_kind": finding.subject_kind,
            "subject_id": str(finding.subject_id),
            "from_state": "closed",
            "reason_text": "El resguardo se retiró durante el mantenimiento mayor",
            "new_action_id": str(new_action),
            "reopened_by": {
                "user_id": str(stack.world.stack.authz.operator_id),
                "display_name": "Coordinación SST sintética",
                "role_in_use": "coordinator_sst",
            },
            "reopened_at": format_timestamp(stack.now()),
        },
    )
    context = stack.context(coordinator)
    service = stack.actions
    fsid = stack.finding_state_id(finding)

    async def complete() -> AssignmentResult:
        # Quien lo invoca (TASK-343) ya autorizó su operación sobre la zona.
        resource = Resource(
            finding.organization.organization_id,
            "finding",
            fsid,
            plant_id=finding.organization.plant_id,
            zone_id=finding.organization.zone_id,
        )
        authorizer = stack.world.stack.authz.authorizer
        authorized = await authorizer.authorize(context, PermissionKey.ACTIONS_MANAGE, resource)
        writer_context = with_unit(authorized, ActorUnit.U04)
        async with stack.database.transaction(writer_context) as transaction:
            organization = finding.organization
            await lock_finding(transaction, organization.organization_id, finding.subject_id)
            rows = action_rows(
                await lock_actions(
                    transaction,
                    organization.organization_id,
                    organization.plant_id,
                    finding.subject_id,
                )
            )
            current = await read_finding(transaction, fsid)
            assert current is not None
            request = AssignmentRequest(
                "engineering",
                "Reinstalar el resguardo fijo y bloquear su retiro sin permiso",
                None,
                assignee,
                None,
                uuid.UUID(first["action_id"]),
            )
            return await service.assign_in_transaction(
                transaction, writer_context, current, rows, request, pending_action_id=new_action
            )

    stack.tick()
    result = stack.run(complete())
    assert (result.action_id, result.transition_id) == (new_action, None)
    assert result.lifecycle_state is S.ACTION_ASSIGNED
    row = stack.world.finding_state(finding)
    assert row is not None
    assert (row["current_action_id"], row["due_at"]) == (new_action, result.due_at)
    states = {str(a["action_id"]): a["state"] for a in stack.action_rows(finding)}
    assert states == {first["action_id"]: "superseded", str(new_action): "assigned"}


# --- concurrencia ---------------------------------------------------------------------------------


async def _waiting_advisory_locks(stack: ActionsStack) -> int:
    rows = await stack.world.stack.authz.sessions.admin.fetch(
        "SELECT count(*) AS n FROM pg_locks WHERE NOT granted AND locktype = 'advisory'"
        " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
    )
    return int(rows[0]["n"])


class _Rendezvous:
    """Mezcla: la primera operación espera, con los candados tomados, a que la segunda haya
    llegado al mismo punto **o** esté esperando la exclusión del hallazgo. Sin la exclusión, las
    dos leen el mismo estado de forma determinista; con ella, la segunda espera."""

    stack: ActionsStack
    arrivals: int = 0

    async def _checked(self, transaction: Transaction) -> None:
        self.arrivals += 1
        if self.arrivals == 1:
            async with asyncio.timeout(LOCK_WAIT_SECONDS):
                while self.arrivals < 2 and await _waiting_advisory_locks(self.stack) == 0:
                    await asyncio.sleep(0.05)


class _RendezvousActions(_Rendezvous, ActionService):
    pass


class _RendezvousEvidence(_Rendezvous, EvidenceService):
    pass


@pytest.mark.parametrize("round_", range(3))
def test_two_simultaneous_assignments_have_one_winner_and_one_conflict(
    stack: ActionsStack, coordinator: Who, assignee: uuid.UUID, round_: int
) -> None:
    finding = stack.opened()
    fsid = stack.finding_state_id(finding)
    context = stack.context(coordinator)
    service = stack.service_like(_RendezvousActions)
    service.stack = stack

    async def both() -> list[Any]:
        calls = [
            service.assign(
                context,
                fsid,
                action_type="engineering",
                action_description=DESCRIPTION,
                assignee_user_id=assignee,
                deadline_days=days,
            )
            for days in (10, 20)
        ]
        return list(await asyncio.gather(*calls, return_exceptions=True))

    results = stack.run(both())
    winners = [r for r in results if isinstance(r, AssignmentResult)]
    losers = [r for r in results if not isinstance(r, AssignmentResult)]
    assert len(winners) == 1, results
    assert len(losers) == 1 and isinstance(losers[0], ActionRejected), results
    assert losers[0].detail_code is LoopDetailCode.TRANSITION_NOT_ALLOWED
    (row,) = stack.action_rows(finding)
    assert row["action_id"] == winners[0].action_id
    assert len(stack.records_of(finding, "action_assigned")) == 1
    projection = stack.world.finding_state(finding)
    assert projection is not None and projection["current_action_id"] == winners[0].action_id


@pytest.mark.parametrize("round_", range(3))
def test_two_simultaneous_entries_never_pass_the_attachment_limit(
    stack: ActionsStack, coordinator: Who, assignee: uuid.UUID, round_: int
) -> None:
    finding = stack.opened()
    action_id = uuid.UUID(_assigned(stack, coordinator, finding, assignee)["action_id"])
    for _ in range(3):
        five = [stack.uploaded(finding) for _ in range(5)]
        accepted = stack.add_evidence(coordinator, action_id, stack.evidence_body(attachments=five))
        assert accepted.status_code == 201, accepted.text
    # 15 aceptados: cada entrada trae 3; juntas serían 21.
    context = stack.context(coordinator)
    service = stack.service_like(_RendezvousEvidence)
    service.stack = stack
    batches = [
        [_claim(stack.uploaded(finding)) for _ in range(3)],
        [_claim(stack.uploaded(finding)) for _ in range(3)],
    ]
    performed = format_timestamp(stack.now() - timedelta(hours=1))

    async def both() -> list[Any]:
        calls = [
            service.add(
                context,
                action_id,
                description="Resguardo fijo instalado y anclado al piso en el acceso",
                performed_at=performed,
                performed_by_role="Técnico de mantenimiento",
                attachments=batch,
                verifier=stack.attachments,
            )
            for batch in batches
        ]
        return list(await asyncio.gather(*calls, return_exceptions=True))

    results = stack.run(both())
    winners = [r for r in results if isinstance(r, EvidenceResult)]
    losers = [r for r in results if not isinstance(r, EvidenceResult)]
    assert len(winners) == 1, results
    assert len(losers) == 1 and isinstance(losers[0], AttachmentRejected), results
    assert losers[0].detail is LoopDetailCode.ATTACHMENT_TOO_LARGE
    assert len(stack.attachment_rows(finding)) == 18


@pytest.mark.parametrize("round_", range(3))
def test_two_simultaneous_entries_never_pass_twenty_per_action(
    stack: ActionsStack, coordinator: Who, assignee: uuid.UUID, round_: int
) -> None:
    finding = stack.opened()
    action_id = uuid.UUID(_assigned(stack, coordinator, finding, assignee)["action_id"])
    for _ in range(19):
        assert stack.add_evidence(coordinator, action_id, stack.evidence_body()).status_code == 201
    context = stack.context(coordinator)
    service = stack.service_like(_RendezvousEvidence)
    service.stack = stack
    performed = format_timestamp(stack.now() - timedelta(hours=1))

    async def both() -> list[Any]:
        calls = [
            service.add(
                context,
                action_id,
                description="Resguardo fijo instalado y anclado al piso en el acceso",
                performed_at=performed,
                performed_by_role="Técnico de mantenimiento",
            )
            for _ in range(2)
        ]
        return list(await asyncio.gather(*calls, return_exceptions=True))

    results = stack.run(both())
    assert sum(isinstance(r, EvidenceResult) for r in results) == 1, results
    assert sum(isinstance(r, ActionRequestInvalid) for r in results) == 1, results
    assert len(stack.entry_rows(action_id)) == 20


def _claim(document: dict[str, Any]) -> AttachmentClaim:
    return AttachmentClaim(
        attachment_id=uuid.UUID(document["attachment_id"]),
        content_type=document["content_type"],
        size_bytes=document["size_bytes"],
        sha256=document["sha256"],
        filename=document["filename"],
        no_identifiable_person_declared=document["no_identifiable_person_declared"],
    )
