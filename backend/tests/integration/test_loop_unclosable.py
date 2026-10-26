"""SUP-U05-12 y 13: el estado SIN ALTERNATIVA SEGURA DISPONIBLE (TASK-312; LC-LAZ-09 y LC-LAZ-11;
BR-LAZ-52 a 59; PR-LAZ-12; H-26).

Sobre PostgreSQL 16 real como ``vigia_app`` y la aplicación real (``unclosable_stack``), con la
política de texto libre de producción (los dos validadores reales de U-04), la configuración real
de la planta y la bandeja de salida real:

- **T10** (``open | action_assigned | overdue → unclosable``): los cinco campos de §2.7, la
  política de la planta y la fecha de revisión; el registro ``unclosable_state_declared``, su
  evento, la fila de ``loop.unclosable_declaration`` y la proyección en la misma transacción.
- **La compuerta de la política** (BR-LAZ-53, 54 y 56): sin política firmada el rechazo **nombra la
  compuerta que falta**; con el puerto sin respuesta, ``503`` y **nada escrito** (fallo cerrado);
  con la política revocada no se declaran nuevos y lo ya declarado se conserva (P4); con una
  política a medias (``loaded = true`` sin el hash del documento) el puerto se resuelve cerrado.
- **Los cinco campos y la firma** (BR-LAZ-52 y 59): falta cualquiera, el rechazo es
  ``unclosable_fields_incomplete`` sin registro; sin licencia, ``signer_license_missing``.
- **La fecha de revisión** (BR-LAZ-57): sin fecha, la de por defecto de la planta (6 meses); fuera
  de ``(ahora, ahora + unclosable_review_max_months]``, ``unclosable_review_date_out_of_range``;
  la planta puede mover las dos (``loop.config``).
- **T11** (``renewed``) y **T12** (``converted_to_action``): el registro ``unclosable_review``, su
  evento, su fila, la declaración que se conserva (P4) y —en T12— el ``action_assigned`` por el
  servicio de TASK-311 en la misma transacción. La revisión **no reabre** el lazo por sí sola.
- **La tarea diaria** ``unclosable_review_due`` (BR-LAZ-58): marca ``pending_review`` una sola vez
  —también cuando otra ejecución toma la tarea con el arrendamiento **vencido** a mano, en la
  pasada del día siguiente y con dos ejecuciones **simultáneas** desde dos conexiones— y **no
  cambia** el estado del ciclo de vida.
- **Alcance** (BR-NUC-13): otra organización, otra zona, un rol sin ``findings.close_and_sign`` y
  la concesión de proveedor responden ``not_found`` sin escribir.

Solo datos generados. Un solo reloj: las marcas salen del reloj simulado del mundo (retro 15).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any, Final

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import IngestedSubject, LoopOrganization
from tests.loop_actions_support import production_free_text
from tests.loop_unclosable_support import (
    ALTERNATIVE,
    COMPENSATORY,
    DECISION_DATE,
    LICENSE,
    REASON,
    REVIEW_NOTE,
    UnclosableStack,
    Who,
    unclosable_stack,
)
from tests.properties.laz.strategies.lifecycle import RevokedPolicyState
from vigia_platform.catalog.domain.ports import PlantPolicyState
from vigia_platform.loop.application.unclosable import TASK_NAME, UnclosableService, add_months
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.config.service import LoopConfigurationService
from vigia_platform.loop.domain.time_aggregation import bucket_start
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration

_NOT_SIGNED: Final = (400, "invalid_request", "loop_unclosable_policy_not_signed")
_UNAVAILABLE: Final = (503, "temporarily_unavailable", "loop_unclosable_policy_unavailable")
_REVOKED: Final = (400, "invalid_request", "loop_unclosable_policy_revoked")
_FIELDS: Final = (400, "invalid_request", "loop_unclosable_fields_incomplete")
_OUT_OF_RANGE: Final = (400, "invalid_request", "loop_unclosable_review_date_out_of_range")
_NO_LICENSE: Final = (400, "invalid_request", "loop_signer_license_missing")
_CONFLICT: Final = (409, "conflict", "loop_transition_not_allowed")
_INVALID: Final = (400, "invalid_request", None)
_NOT_FOUND: Final = (404, "not_found", None)
_REASON: Final = "Ajuste sintético de los meses de revisión del incerrable"


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[UnclosableStack]:
    with unclosable_stack(postgres_endpoint, "loop_unclosable") as built:
        yield built


@pytest.fixture(autouse=True)
def _clean_gates(stack: UnclosableStack) -> Iterator[None]:
    stack.gates.down = False
    stack.gates.override = None
    yield
    stack.gates.down = False
    stack.gates.override = None


@pytest.fixture
def coordinator(stack: UnclosableStack) -> Who:
    """Coordinador SST de la organización A, cuya planta tiene política firmada.

    La sesión se abre por prueba: el reloj simulado avanza meses en las pruebas de la tarea
    diaria y una sesión de módulo caducaría."""
    return stack.member(stack.world.a)


@pytest.fixture
def responsible(stack: UnclosableStack) -> uuid.UUID:
    return stack.user(stack.world.a)


def _error(response: Any) -> tuple[int, str | None, str | None]:
    body = response.json()
    return response.status_code, body.get("code"), body.get("detail_code")


def _comparable(response: Any) -> tuple[int, Any]:
    body = response.json()
    return response.status_code, {k: v for k, v in body.items() if k != "correlation_id"}


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _exact_declared_at(stack: UnclosableStack, subject: IngestedSubject) -> datetime:
    """El instante exacto de la declaración (el de su registro; la respuesta lo agrega)."""
    (record,) = stack.records_of(subject, "unclosable_state_declared")
    return _instant(record["content"]["declared_at"])


def _json(row: Any, column: str) -> dict[str, Any]:
    """Una columna ``jsonb`` leída por SQL directo (llega como texto)."""
    document: dict[str, Any] = json.loads(row[column])
    return document


def _signed(stack: UnclosableStack, organization: LoopOrganization) -> uuid.UUID:
    """Una política firmada de la planta, con la versión siguiente a la última."""
    version = len(stack.policy_rows(organization)) + 1
    return stack.signed_policy(organization, version=version)


def _declared(
    stack: UnclosableStack,
    who: Who,
    subject: IngestedSubject,
    responsible: uuid.UUID,
    **changes: Any,
) -> dict[str, Any]:
    response = stack.declare(who, subject, stack.declaration_body(responsible, **changes))
    assert response.status_code == 201, response.text
    return response.json()


def _presented(stack: UnclosableStack, subject: IngestedSubject) -> datetime:
    """La marca que la respuesta presenta: el inicio de la ventana si la zona es unipersonal."""
    projection = stack.world.finding_state(subject)
    assert projection is not None
    assert projection["single_occupancy_zone"] is True
    return bucket_start(
        _exact_declared_at(stack, subject), int(projection["aggregation_window_minutes"])
    )


# --- T10 ------------------------------------------------------------------------------------------


def test_t10_writes_record_event_declaration_row_and_projection_in_one_transaction(
    stack: UnclosableStack, coordinator: Who, responsible: uuid.UUID
) -> None:
    subject = stack.opened()
    policy_id = _signed(stack, subject.organization)
    body = _declared(stack, coordinator, subject, responsible)
    declaration_id = uuid.UUID(body["declaration_id"])
    assert body["lifecycle_state"] == "unclosable"
    assert body["policy_id"] == str(policy_id)
    assert body["policy_version"] == 1
    assert body["policy_document_sha256"] == "b" * 64
    (record,) = stack.records_of(subject, "unclosable_state_declared")
    record_id = uuid.UUID(body["record_id"])
    assert record["record_id"] == record_id
    content = record["content"]
    assert content["declaration_id"] == str(declaration_id)
    assert content["evaluated_alternative"] == ALTERNATIVE
    assert content["rejection_reason"] == REASON
    assert content["compensatory_measure"] == COMPENSATORY
    assert content["decision_date"] == DECISION_DATE
    assert content["responsible_user_id"] == str(responsible)
    assert content["anchor_record_id"] == str(subject.record_id)
    assert content["policy"]["policy_id"] == str(policy_id)
    assert (content["policy"]["version"], content["policy"]["document_sha256"]) == (
        1,
        "b" * 64,
    )
    assert content["signer"]["professional_license"] == LICENSE
    assert content["review_due_at"] == body["review_due_at"]
    # La fecha por defecto: 6 meses después del instante de la declaración (BR-LAZ-57).
    declared_at = _instant(content["declared_at"])
    assert _instant(body["review_due_at"]) == add_months(declared_at, 6)
    assert _instant(body["declared_at"]) == _presented(stack, subject)
    assert stack.events(subject.organization, record_id) == ["finding_unclosable"]
    (row,) = stack.declaration_rows(subject)
    assert row["declaration_id"] == declaration_id
    assert row["record_id"] == record_id
    assert (row["state"], row["policy_version"]) == ("active", "1")
    assert row["policy_id"] == policy_id
    assert row["policy_document_sha256"] == "b" * 64
    assert row["review_due_at"] == _instant(body["review_due_at"])
    assert _json(row, "signer")["professional_license"] == LICENSE
    assert row["decision_date"].isoformat() == DECISION_DATE
    projection = stack.world.finding_state(subject)
    assert projection is not None
    assert projection["lifecycle_state"] == "unclosable"
    assert projection["unclosable_since"] == declared_at
    assert projection["unclosable_review_due_at"] == _instant(body["review_due_at"])
    assert projection["unclosable_pending_review"] is False
    assert projection["last_record_id"] == record_id


@pytest.mark.parametrize("state", ["open", "action_assigned", "overdue"])
def test_t10_declares_from_the_three_active_states(
    stack: UnclosableStack, coordinator: Who, responsible: uuid.UUID, state: str
) -> None:
    subject = stack.subject_in(state)
    _signed(stack, subject.organization)
    body = _declared(stack, coordinator, subject, responsible)
    assert body["lifecycle_state"] == "unclosable"


@pytest.mark.parametrize("state", ["received", "in_review", "closed", "discarded"])
def test_t10_is_rejected_from_any_other_state(
    stack: UnclosableStack, coordinator: Who, responsible: uuid.UUID, state: str
) -> None:
    subject = stack.subject_in(state)
    _signed(stack, subject.organization)
    before = stack.untouched(subject)
    response = stack.declare(coordinator, subject, stack.declaration_body(responsible))
    assert _error(response) == _CONFLICT
    assert stack.untouched(subject) == before


# --- la compuerta de la política ------------------------------------------------------------------


def test_the_policy_gates_the_declaration(stack: UnclosableStack) -> None:
    """Sin política firmada el rechazo **nombra la compuerta que falta** (BR-LAZ-53).

    La planta de B no tiene política en toda la prueba: el caso no depende del orden."""
    organization = stack.world.b
    who = stack.member(organization)
    subject = stack.opened(organization)
    before = stack.untouched(subject)
    response = stack.declare(who, subject, stack.declaration_body(stack.user(organization)))
    assert _error(response) == _NOT_SIGNED
    assert stack.untouched(subject) == before
    assert stack.policy_rows(organization) == []


def test_a_policy_signed_later_has_no_retroactive_effect(stack: UnclosableStack) -> None:
    """BR-LAZ-56: firmada la política, se declara desde ese momento; nada cambia hacia atrás."""
    organization = stack.world.b
    who = stack.member(organization)
    responsible = stack.user(organization)
    subject = stack.opened(organization)
    assert _error(stack.declare(who, subject, stack.declaration_body(responsible))) == _NOT_SIGNED
    policy_id = _signed(stack, organization)
    body = _declared(stack, who, subject, responsible)
    assert body["policy_id"] == str(policy_id)
    assert body["policy_version"] == 1
    (row,) = stack.declaration_rows(subject)
    assert row["declared_at"] > stack.policy_rows(organization)[0]["signed_at"]


def test_a_port_without_answer_fails_closed_and_writes_nothing(
    stack: UnclosableStack, coordinator: Who, responsible: uuid.UUID
) -> None:
    subject = stack.opened()
    _signed(stack, subject.organization)
    before = stack.untouched(subject)
    stack.gates.down = True
    response = stack.declare(coordinator, subject, stack.declaration_body(responsible))
    assert _error(response) == _UNAVAILABLE
    # Fallo cerrado (BR-LAZ-54): la duda se resuelve cerrada aunque el cuerpo esté a medias.
    partial = stack.declare(coordinator, subject, {"evaluated_alternative": ALTERNATIVE})
    assert _error(partial) == _UNAVAILABLE
    assert stack.untouched(subject) == before


def test_a_revoked_policy_blocks_new_declarations_and_keeps_the_declared(
    stack: UnclosableStack, coordinator: Who, responsible: uuid.UUID
) -> None:
    declared = stack.opened()
    _signed(stack, declared.organization)
    body = _declared(stack, coordinator, declared, responsible)
    other = stack.opened()
    before = stack.untouched(declared)
    other_before = stack.untouched(other)
    stack.gates.override = _revoked(stack)
    assert (
        _error(stack.declare(coordinator, other, stack.declaration_body(responsible))) == _REVOKED
    )
    assert stack.untouched(other) == other_before
    # P4: lo declarado se conserva tal cual y su revisión sigue siendo posible.
    assert _error(stack.declare(coordinator, declared, stack.declaration_body(responsible))) == (
        _REVOKED
    )
    assert stack.untouched(declared) == before
    assert stack.review(coordinator, body["declaration_id"], stack.review_body()).status_code == 201


def test_a_policy_loaded_without_its_document_is_a_closed_failure(
    stack: UnclosableStack, coordinator: Who, responsible: uuid.UUID
) -> None:
    """``loaded = true`` sin el hash del documento: no se puede citar la política (el registro la
    exige), así que el puerto se resuelve **cerrado**."""
    subject = stack.opened()
    _signed(stack, subject.organization)
    stack.gates.override = PlantPolicyState(
        loaded=True, policy_id=uuid.uuid4(), version=1, signed_at=stack.now(), document_sha256=None
    )
    before = stack.untouched(subject)
    response = stack.declare(coordinator, subject, stack.declaration_body(responsible))
    assert _error(response) == _UNAVAILABLE
    assert stack.untouched(subject) == before


def _revoked(stack: UnclosableStack) -> PlantPolicyState:
    """Una política revocada: la forma de §1.2 más la marca de revocación (BR-LAZ-56)."""
    return RevokedPolicyState(
        loaded=True,
        policy_id=uuid.uuid4(),
        version=2,
        signed_at=stack.now(),
        document_sha256="c" * 64,
    )


# --- los cinco campos y la firma ------------------------------------------------------------------


@pytest.mark.parametrize(
    "missing",
    [
        "evaluated_alternative",
        "rejection_reason",
        "responsible_user_id",
        "decision_date",
        "compensatory_measure",
    ],
)
def test_every_one_of_the_five_fields_is_required(
    stack: UnclosableStack, coordinator: Who, responsible: uuid.UUID, missing: str
) -> None:
    subject = stack.opened()
    _signed(stack, subject.organization)
    body = stack.declaration_body(responsible)
    body.pop(missing)
    before = stack.untouched(subject)
    assert _error(stack.declare(coordinator, subject, body)) == _FIELDS
    assert stack.untouched(subject) == before


def test_a_field_rejected_by_the_vocabulary_names_the_term_and_writes_nothing(
    stack: UnclosableStack, coordinator: Who, responsible: uuid.UUID
) -> None:
    """SUP-U05-12 (VIG-344): un texto de la declaración que el vocabulario bloqueado rechaza
    responde su ``detail_code`` con ``rejection`` exacto, sin escribir nada."""
    subject = stack.opened()
    _signed(stack, subject.organization)
    body = stack.declaration_body(
        responsible,
        compensatory_measure="Vigilancia permanente que garantiza la zona en el turno de carga",
    )
    before = stack.untouched(subject)
    response = stack.declare(coordinator, subject, body)
    assert _error(response) == (400, "invalid_request", "loop_blocked_vocabulary_term")
    assert response.json()["rejection"] == {
        "code": "blocked_vocabulary_term",
        "term": "garantiza",
        "suggestion": "Cita la cifra medida, con su periodo y su fuente, en lugar de prometerla.",
    }
    assert stack.untouched(subject) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"evaluated_alternative": "corta"},
        {"rejection_reason": "corta"},
        {"compensatory_measure": "corta"},
        {"decision_date": "2026-02-30"},
        {"decision_date": "01/10/2026"},
    ],
)
def test_the_five_fields_out_of_their_schema_are_rejected(
    stack: UnclosableStack, coordinator: Who, responsible: uuid.UUID, changes: dict[str, Any]
) -> None:
    subject = stack.opened()
    _signed(stack, subject.organization)
    body = stack.declaration_body(responsible, **changes)
    before = stack.untouched(subject)
    assert _error(stack.declare(coordinator, subject, body)) == _FIELDS
    assert stack.untouched(subject) == before


def test_the_signature_needs_the_license(
    stack: UnclosableStack, coordinator: Who, responsible: uuid.UUID
) -> None:
    subject = stack.opened()
    _signed(stack, subject.organization)
    before = stack.untouched(subject)
    response = stack.declare(
        coordinator, subject, stack.declaration_body(responsible, professional_license=None)
    )
    assert _error(response) == _NO_LICENSE
    assert stack.untouched(subject) == before


# --- la fecha de revisión -------------------------------------------------------------------------


def test_the_review_date_range(
    stack: UnclosableStack, coordinator: Who, responsible: uuid.UUID
) -> None:
    _signed(stack, stack.world.a)
    now = stack.now()
    for months, expected in (
        (12, None),
        (1, None),
        (13, _OUT_OF_RANGE),
        (-1, _OUT_OF_RANGE),
        (0, _OUT_OF_RANGE),
    ):
        subject = stack.opened()
        body = stack.declaration_body(
            responsible, review_due_at=format_timestamp(add_months(now, months))
        )
        before = stack.untouched(subject)
        response = stack.declare(coordinator, subject, body)
        if expected is None:
            assert response.status_code == 201, (months, response.text)
            assert _instant(response.json()["review_due_at"]) == add_months(now, months)
        else:
            assert _error(response) == expected, (months, response.text)
            assert stack.untouched(subject) == before


def test_the_plant_configuration_moves_the_default_and_the_ceiling(
    stack: UnclosableStack, coordinator: Who, responsible: uuid.UUID
) -> None:
    """La fecha por defecto y el tope salen de la configuración de la planta (BR-LAZ-57)."""
    subject = stack.opened()
    _signed(stack, subject.organization)
    manager = stack.member(subject.organization, Role.PLANT_MANAGER)
    _configure(stack, manager, default=3, maximum=4)
    try:
        body = _declared(stack, coordinator, subject, responsible)
        assert _instant(body["review_due_at"]) == add_months(_exact_declared_at(stack, subject), 3)
        other = stack.opened()
        _signed(stack, other.organization)
        beyond = stack.declare(
            coordinator,
            other,
            stack.declaration_body(
                responsible, review_due_at=format_timestamp(add_months(stack.now(), 5))
            ),
        )
        assert _error(beyond) == _OUT_OF_RANGE
        ceiling = add_months(stack.now(), 4)
        at_ceiling = _declared(
            stack,
            coordinator,
            stack.opened(),
            responsible,
            review_due_at=format_timestamp(ceiling),
        )
        assert _instant(at_ceiling["review_due_at"]) == ceiling
    finally:
        _configure(stack, manager, default=6, maximum=12)


def _configure(stack: UnclosableStack, manager: Who, *, default: int, maximum: int) -> None:
    """Cambia los meses del incerrable de la planta por el servicio real (TASK-304)."""
    service = LoopConfigurationService(
        repository=PostgresLoopConfigurationRepository(stack.database),
        database=stack.database,
        writer=stack.writer,
        authorizer=stack.world.stack.authz.authorizer,
        free_text=production_free_text(),
        clock=stack.world.stack.authz.sessions.clock,
    )
    stack.run(
        service.change(
            stack.context(manager),
            stack.world.a.plant_id,
            integers={
                "unclosable_review_default_months": default,
                "unclosable_review_max_months": maximum,
            },
            business_calendar=None,
            reason=_REASON,
        )
    )


# --- T11 y T12 ------------------------------------------------------------------------------------


def test_t11_renews_the_declaration_without_reopening_the_loop(
    stack: UnclosableStack, coordinator: Who, responsible: uuid.UUID
) -> None:
    subject = stack.opened()
    _signed(stack, subject.organization)
    body = _declared(stack, coordinator, subject, responsible)
    declaration_id = uuid.UUID(body["declaration_id"])
    new_due = format_timestamp(add_months(stack.now(), 4))
    response = stack.review(
        coordinator, declaration_id, stack.review_body(new_review_due_at=new_due)
    )
    assert response.status_code == 201, response.text
    review = response.json()
    assert (review["outcome"], review["new_action_id"]) == ("renewed", None)
    assert review["new_review_due_at"] == new_due
    # El ciclo de vida no cambia: sigue incerrable (BR-LAZ-58 y respuesta 7).
    assert review["lifecycle_state"] == "unclosable"
    (record,) = stack.records_of(subject, "unclosable_review")
    assert record["content"]["review_note"] == REVIEW_NOTE
    assert record["content"]["new_review_due_at"] == new_due
    assert record["content"]["reviewer"]["professional_license"] == LICENSE
    assert "new_action_id" not in record["content"]
    assert stack.events(subject.organization, uuid.UUID(review["record_id"])) == [
        "unclosable_reviewed"
    ]
    (row,) = stack.review_rows(declaration_id)
    assert (row["outcome"], row["new_action_id"]) == ("renewed", None)
    assert row["new_review_due_at"] == _instant(new_due)
    assert row["review_note"] == REVIEW_NOTE
    assert row["record_id"] == uuid.UUID(review["record_id"])
    (declaration,) = stack.declaration_rows(subject)
    assert declaration["state"] == "renewed"
    assert declaration["review_due_at"] == _instant(new_due)
    assert declaration["declared_at"] == _exact_declared_at(stack, subject)
    projection = stack.world.finding_state(subject)
    assert projection is not None
    assert projection["lifecycle_state"] == "unclosable"
    assert projection["unclosable_review_due_at"] == _instant(new_due)
    assert projection["unclosable_pending_review"] is False


def test_t11_with_a_date_out_of_range_writes_nothing(
    stack: UnclosableStack, coordinator: Who, responsible: uuid.UUID
) -> None:
    subject = stack.opened()
    _signed(stack, subject.organization)
    body = _declared(stack, coordinator, subject, responsible)
    declaration_id = uuid.UUID(body["declaration_id"])
    before = stack.untouched(subject)
    for months in (13, -1):
        response = stack.review(
            coordinator,
            declaration_id,
            stack.review_body(new_review_due_at=format_timestamp(add_months(stack.now(), months))),
        )
        assert _error(response) == _OUT_OF_RANGE, months
    assert stack.untouched(subject) == before
    (declaration,) = stack.declaration_rows(subject)
    assert declaration["state"] == "active"


def test_the_review_needs_the_license_and_a_valid_outcome(
    stack: UnclosableStack, coordinator: Who, responsible: uuid.UUID
) -> None:
    subject = stack.opened()
    _signed(stack, subject.organization)
    body = _declared(stack, coordinator, subject, responsible)
    declaration_id = uuid.UUID(body["declaration_id"])
    before = stack.untouched(subject)
    without = stack.review(
        coordinator, declaration_id, stack.review_body(professional_license=None)
    )
    assert _error(without) == _NO_LICENSE
    unknown = stack.review(coordinator, declaration_id, stack.review_body(outcome="reopened"))
    assert _error(unknown) == _INVALID
    # Sin nota, o con una nota fuera de su esquema (menos de 20 caracteres), la revisión está
    # incompleta: el código que la ruta declara (``requires(..., detail_codes=...)``, A-68).
    for note in (None, "Sigue igual"):
        incomplete = stack.review(coordinator, declaration_id, stack.review_body(review_note=note))
        assert _error(incomplete) == _FIELDS
    assert stack.untouched(subject) == before


def test_t12_converts_the_declaration_into_an_action(
    stack: UnclosableStack, coordinator: Who, responsible: uuid.UUID
) -> None:
    subject = stack.opened()
    _signed(stack, subject.organization)
    body = _declared(stack, coordinator, subject, responsible)
    declaration_id = uuid.UUID(body["declaration_id"])
    assignee = stack.user(subject.organization)
    response = stack.review(
        coordinator,
        declaration_id,
        stack.review_body(outcome="converted_to_action", action=stack.action_body(assignee)),
    )
    assert response.status_code == 201, response.text
    review = response.json()
    action_id = uuid.UUID(review["new_action_id"])
    assert review["outcome"] == "converted_to_action"
    assert review["new_review_due_at"] is None
    assert review["lifecycle_state"] == "action_assigned"
    # Los dos registros de la fila T12, cada uno con su evento, en la misma transacción.
    (review_record,) = stack.records_of(subject, "unclosable_review")
    assert review_record["content"]["new_action_id"] == str(action_id)
    assert "new_review_due_at" not in review_record["content"]
    (action_record,) = stack.records_of(subject, "action_assigned")
    assert action_record["content"]["action_id"] == str(action_id)
    assert action_record["content"]["assignee_user_id"] == str(assignee)
    assert stack.events(subject.organization, uuid.UUID(review["record_id"])) == [
        "unclosable_reviewed"
    ]
    assert stack.events(subject.organization, uuid.UUID(str(action_record["record_id"]))) == [
        "action_assigned"
    ]
    (row,) = stack.review_rows(declaration_id)
    assert (row["outcome"], row["new_action_id"]) == ("converted_to_action", action_id)
    (action,) = stack.action_rows(subject)
    assert (action["action_id"], action["state"]) == (action_id, "assigned")
    (declaration,) = stack.declaration_rows(subject)
    assert declaration["state"] == "converted"
    projection = stack.world.finding_state(subject)
    assert projection is not None
    assert projection["lifecycle_state"] == "action_assigned"
    assert projection["current_action_id"] == action_id
    assert projection["unclosable_since"] is None
    assert projection["unclosable_review_due_at"] is None
    assert projection["unclosable_pending_review"] is False
    # La declaración convertida ya no se puede revisar otra vez; su revisión queda íntegra (P4).
    before = stack.untouched(subject)
    assert _error(stack.review(coordinator, declaration_id, stack.review_body())) == _CONFLICT
    assert stack.untouched(subject) == before


def test_t12_without_the_action_is_invalid_and_writes_nothing(
    stack: UnclosableStack, coordinator: Who, responsible: uuid.UUID
) -> None:
    subject = stack.opened()
    _signed(stack, subject.organization)
    body = _declared(stack, coordinator, subject, responsible)
    before = stack.untouched(subject)
    response = stack.review(
        coordinator, body["declaration_id"], stack.review_body(outcome="converted_to_action")
    )
    assert _error(response) == _INVALID
    assert stack.untouched(subject) == before
    (declaration,) = stack.declaration_rows(subject)
    assert declaration["state"] == "active"


# --- la tarea diaria ------------------------------------------------------------------------------


def _soon(stack: UnclosableStack, seconds: int = 2) -> str:
    """Una fecha de revisión a segundos del reloj: la tarea diaria no tiene que avanzar meses
    (y el certificado del nodo, la sesión y el arrendamiento siguen vigentes)."""
    return format_timestamp(stack.now() + timedelta(seconds=seconds))


def _due_of(
    stack: UnclosableStack, organization: LoopOrganization, declaration_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Los eventos ``unclosable_review_due`` de **esa** declaración (la tarea los publica por
    organización y la misma pasada puede vencer otras)."""
    return [
        event
        for event in stack.due_events(organization)
        if event["payload"]["declaration_id"] == str(declaration_id)
    ]


def test_the_daily_task_marks_pending_review_once(
    stack: UnclosableStack, coordinator: Who, responsible: uuid.UUID
) -> None:
    subject = stack.opened()
    _signed(stack, subject.organization)
    body = _declared(stack, coordinator, subject, responsible, review_due_at=_soon(stack))
    declaration_id = uuid.UUID(body["declaration_id"])
    stack.reach_review_date(subject)
    report = stack.run(stack.scheduler("worker-a").run_due(TASK_NAME))
    assert report is not None and not report.lease_lost
    projection = stack.world.finding_state(subject)
    assert projection is not None
    # La marca está; el estado del ciclo de vida **no** cambió (BR-LAZ-58).
    assert projection["unclosable_pending_review"] is True
    assert projection["lifecycle_state"] == "unclosable"
    (declaration,) = stack.declaration_rows(subject)
    assert declaration["state"] == "pending_review"
    due = _due_of(stack, subject.organization, declaration_id)
    assert [event["payload"]["review_due_at"] for event in due] == [body["review_due_at"]]
    # La segunda ejecución —otro proceso, con el arrendamiento de la primera vencido a mano— no
    # marca ni publica otra vez: la marca es única por (declaration_id, review_due_at).
    stack.task_due(owner="worker-a")
    second = stack.run(stack.scheduler("worker-b").run_due(TASK_NAME))
    assert second is not None and not second.lease_lost
    assert len(_due_of(stack, subject.organization, declaration_id)) == 1
    assert stack.world.finding_state(subject) == projection
    assert stack.declaration_rows(subject) == [declaration]


def test_the_daily_task_on_a_later_day_does_not_mark_again(
    stack: UnclosableStack, coordinator: Who, responsible: uuid.UUID
) -> None:
    """La marca es única por ``(declaration_id, review_due_at)``: la pasada del día siguiente, con
    la misma fecha de revisión ya marcada, no marca ni publica otra vez (la condición
    ``unclosable_pending_review = false`` de la lectura y del ``UPDATE`` es la garantía)."""
    subject = stack.opened()
    _signed(stack, subject.organization)
    body = _declared(stack, coordinator, subject, responsible, review_due_at=_soon(stack))
    declaration_id = uuid.UUID(body["declaration_id"])
    stack.reach_review_date(subject)
    assert stack.run(stack.scheduler("worker-a").run_due(TASK_NAME)) is not None
    projection = stack.world.finding_state(subject)
    assert projection is not None and projection["unclosable_pending_review"] is True
    (declaration,) = stack.declaration_rows(subject)
    assert len(_due_of(stack, subject.organization, declaration_id)) == 1
    # El día siguiente, una pasada nueva del planificador (otro proceso) y una ejecución directa
    # en la organización: ninguna marca ni publica.
    stack.tick(24 * 3600)
    stack.task_due()
    report = stack.run(stack.scheduler("worker-b").run_due(TASK_NAME))
    assert report is not None and not report.lease_lost
    assert stack.run(stack.mark_due_async(subject.organization)) == 0
    assert len(_due_of(stack, subject.organization, declaration_id)) == 1
    assert stack.world.finding_state(subject) == projection
    assert stack.declaration_rows(subject) == [declaration]


_LOCK_WAIT_SECONDS: Final = 60.0
"""Tope de las esperas de la prueba concurrente (retro 15: topes de 60 s, nunca de pared corta)."""


class _HoldsFirst(UnclosableService):
    """Con la marca hecha y sus candados tomados (antes de su ``COMMIT``), avisa (``holding``) y
    espera a que la otra ejecución esté esperando un candado; después termina."""

    stack: UnclosableStack
    holding: asyncio.Event

    async def _declaration_of(self, transaction: Any, due: Any) -> uuid.UUID:  # type: ignore[override]
        self.holding.set()
        async with asyncio.timeout(_LOCK_WAIT_SECONDS):
            while await self.stack.waiting_locks() == 0:
                await asyncio.sleep(0.05)
        return await UnclosableService._declaration_of(transaction, due)


@pytest.mark.parametrize("round_", range(3))
def test_two_runs_at_once_mark_and_publish_once(
    stack: UnclosableStack, coordinator: Who, responsible: uuid.UUID, round_: int
) -> None:
    """Dos ejecuciones simultáneas de la tarea en la misma organización, desde dos conexiones (el
    arrendamiento de la primera venció mientras seguía viva): la segunda arranca cuando la primera
    ya marcó y tiene sus candados, espera el control de la planta y, tras el ``COMMIT`` de la
    primera, no encuentra nada que marcar. Una sola marca y un solo ``unclosable_review_due``."""
    subject = stack.opened()
    _signed(stack, subject.organization)
    body = _declared(stack, coordinator, subject, responsible, review_due_at=_soon(stack))
    declaration_id = uuid.UUID(body["declaration_id"])
    stack.reach_review_date(subject)
    holding = asyncio.Event()
    first: Any = stack.service_like(_HoldsFirst)
    first.stack, first.holding = stack, holding

    async def later() -> int:
        await holding.wait()
        return await stack.mark_due_async(subject.organization)

    async def race() -> list[Any]:
        async with asyncio.timeout(4 * _LOCK_WAIT_SECONDS):
            return list(
                await asyncio.gather(
                    stack.mark_due_async(subject.organization, first),
                    later(),
                    return_exceptions=True,
                )
            )

    results = stack.run(race())
    failures = [r for r in results if isinstance(r, BaseException)]
    assert failures == [], failures
    assert results == [1, 0]
    assert len(_due_of(stack, subject.organization, declaration_id)) == 1
    projection = stack.world.finding_state(subject)
    assert projection is not None
    assert projection["unclosable_pending_review"] is True
    assert projection["lifecycle_state"] == "unclosable"
    (declaration,) = stack.declaration_rows(subject)
    assert declaration["state"] == "pending_review"


def test_the_daily_task_waits_for_the_renewed_date(
    stack: UnclosableStack, coordinator: Who, responsible: uuid.UUID
) -> None:
    subject = stack.opened()
    _signed(stack, subject.organization)
    body = _declared(stack, coordinator, subject, responsible)
    renewed = stack.review(
        coordinator, body["declaration_id"], stack.review_body(new_review_due_at=_soon(stack))
    )
    assert renewed.status_code == 201, renewed.text
    before = stack.world.finding_state(subject)
    assert before is not None
    # Antes de la fecha nueva la tarea no marca nada.
    assert stack.run(stack.scheduler("worker-a").run_due(TASK_NAME)) is not None
    projection = stack.world.finding_state(subject)
    assert projection is not None
    assert projection["unclosable_pending_review"] is False
    assert _due_of(stack, subject.organization, uuid.UUID(body["declaration_id"])) == []
    # Al llegar la fecha nueva, marca una vez y no toca el ciclo de vida.
    stack.reach_review_date(subject)
    stack.task_due()
    assert stack.run(stack.scheduler("worker-b").run_due(TASK_NAME)) is not None
    projection = stack.world.finding_state(subject)
    assert projection is not None
    assert projection["unclosable_pending_review"] is True
    assert projection["lifecycle_state"] == "unclosable"
    assert projection["unclosable_review_due_at"] != _instant(body["review_due_at"])
    assert projection["unclosable_review_due_at"] == before["unclosable_review_due_at"]
    assert len(_due_of(stack, subject.organization, uuid.UUID(body["declaration_id"]))) == 1
    (declaration,) = stack.declaration_rows(subject)
    assert declaration["state"] == "pending_review"


def test_the_daily_task_does_not_touch_another_organization(
    stack: UnclosableStack, coordinator: Who, responsible: uuid.UUID
) -> None:
    """La tarea itera por organización: lo de B no cambia cuando marca lo de A."""
    subject = stack.opened()
    _signed(stack, subject.organization)
    _declared(stack, coordinator, subject, responsible, review_due_at=_soon(stack))
    foreign = stack.opened(stack.world.b)
    foreign_before = stack.world.finding_state(foreign)
    events_before = stack.due_events(stack.world.b)
    assert foreign_before is not None
    stack.reach_review_date(subject)
    assert stack.run(stack.scheduler("worker-a").run_due(TASK_NAME)) is not None
    assert stack.world.finding_state(foreign) == foreign_before
    assert stack.due_events(stack.world.b) == events_before


# --- alcance --------------------------------------------------------------------------------------


def test_out_of_scope_subjects_answer_like_missing_and_write_nothing(
    stack: UnclosableStack, coordinator: Who, responsible: uuid.UUID
) -> None:
    world = stack.world
    site = world.a.site
    foreign = stack.opened(world.b)
    foreign_before = stack.untouched(foreign)
    body = stack.declaration_body(responsible)
    known = stack.declare(coordinator, foreign, body)
    missing = stack.declare(coordinator, uuid.uuid4(), body)
    assert _error(known) == _NOT_FOUND
    assert _comparable(known) == _comparable(missing)
    assert stack.untouched(foreign) == foreign_before
    subject = stack.opened()
    _signed(stack, subject.organization)
    before = stack.untouched(subject)
    for who in (
        stack.member(world.a, Role.COORDINATOR_SST, ScopeLevel.ZONE, site.other_zone_id),
        stack.member(world.a, Role.PLANT_MANAGER),
        stack.member(world.a, Role.ADMINISTRATOR),
        stack.member(world.a, Role.LINE_MANAGER),
        stack.installer(world.a),
    ):
        response = stack.declare(who, subject, body)
        assert _error(response) == _NOT_FOUND, response.text
        assert _comparable(response) == _comparable(stack.declare(who, uuid.uuid4(), body))
    assert stack.untouched(subject) == before
    # La revisión de una declaración de otra organización responde como una inexistente.
    declared = _declared(stack, coordinator, subject, responsible)
    foreign_reviewer = stack.member(world.b)
    unknown = stack.review(coordinator, uuid.uuid4(), stack.review_body())
    assert _error(unknown) == _NOT_FOUND
    assert _comparable(
        stack.review(foreign_reviewer, declared["declaration_id"], stack.review_body())
    ) == _comparable(unknown)
    (declaration,) = stack.declaration_rows(subject)
    assert declaration["state"] == "active"


def test_a_responsible_that_is_not_an_active_user_of_the_organization_answers_like_missing(
    stack: UnclosableStack, coordinator: Who
) -> None:
    """El campo 3 de §2.7 es un usuario **activo de la organización** (BR-LAZ-52): el resto de
    casos —inexistente, de otra organización, invitado o dado de baja— responde ``not_found``."""
    world = stack.world
    subject = stack.opened()
    _signed(stack, subject.organization)
    before = stack.untouched(subject)
    known = stack.declare(coordinator, subject, stack.declaration_body(uuid.uuid4()))
    assert _error(known) == _NOT_FOUND
    deactivated = stack.user(world.a)
    stack.deactivate(deactivated)
    invited = world.stack.authz.add_user(world.a.organization_id, status="invited")
    world.stack.authz.assign(world.a.organization_id, invited, Role.COORDINATOR_SST)
    for user in (stack.user(world.b), deactivated, invited):
        response = stack.declare(coordinator, subject, stack.declaration_body(user))
        assert _comparable(response) == _comparable(known), user
    assert stack.untouched(subject) == before
    # Un usuario activo de la organización lo es aunque su alcance sea otra zona de la planta.
    scoped = stack.user(world.a, Role.COORDINATOR_SST, ScopeLevel.ZONE, world.a.site.other_zone_id)
    assert stack.declare(coordinator, subject, stack.declaration_body(scoped)).status_code == 201


def test_an_unknown_subject_answers_not_found(
    stack: UnclosableStack, coordinator: Who, responsible: uuid.UUID
) -> None:
    body = stack.declaration_body(responsible)
    assert _error(stack.declare(coordinator, uuid.uuid4(), body)) == _NOT_FOUND
