"""H-26 · el estado incerrable con la política firmada (TASK-312; BR-LAZ-52 a 59; escenario L-6).

Historia: la planta tiene su política de incerrables firmada por el abogado (la compuerta 21, que
U-03 publica como ``PlantPolicy``) y un hallazgo confirmado no tiene alternativa segura. El
coordinador SST declara SIN ALTERNATIVA SEGURA DISPONIBLE con los **cinco campos** de §2.7, fija la
fecha de revisión y firma con su licencia; U-04 **no interpreta** los criterios de la política
(BR-LAZ-55): exige ``loaded = true`` y copia ``policy_id``, ``version``, ``signed_at`` y
``document_sha256`` al registro, de modo que un tercero pueda comprobar bajo qué política se
declaró. Sobre PostgreSQL 16 real y la aplicación real (``unclosable_stack``), por HTTP:

- la declaración se escribe con su evento, su fila y su proyección en la misma transacción, y el
  hallazgo queda ``unclosable``;
- al llegar la fecha de revisión, la tarea diaria marca ``pending_review`` y **no reabre** el lazo:
  el estado del ciclo de vida no cambia (BR-LAZ-58);
- la revisión renueva la declaración (T11) o la convierte en una acción de ingeniería o de proceso
  (T12), y la declaración y su revisión anteriores **permanecen íntegras** (P4);
- sin la política firmada no hay declaración, y el rechazo **nombra la compuerta que falta**
  (BR-LAZ-53), aunque el cuerpo esté completo.

TASK-328 reúne esta historia con las demás de U-04. Solo datos generados.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, Final

import pytest

from tests.integration.conftest import PostgresEndpoint
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
from tests.properties.laz.strategies.lifecycle import PlantPolicy
from vigia_platform.loop.application.unclosable import TASK_NAME, add_months
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration

_NOT_SIGNED: Final = (400, "loop_unclosable_policy_not_signed")
_REASON: Final = "Ajuste sintético de los meses de revisión del incerrable"


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[UnclosableStack]:
    with unclosable_stack(postgres_endpoint, "laz_h26") as built:
        yield built


@pytest.fixture(scope="module")
def coordinator(stack: UnclosableStack) -> Who:
    return stack.member(stack.world.a)


def _error(response: Any) -> tuple[int, str | None]:
    body = response.json()
    return response.status_code, body.get("detail_code")


def test_h26_the_declaration_cites_the_policy_and_the_five_fields(
    stack: UnclosableStack, coordinator: Who
) -> None:
    """La historia completa: declarar, revisar al vencer, renovar y convertir en acción."""
    organization = stack.world.a
    who = coordinator
    responsible = stack.user(organization)
    policy = stack.ensure_signed_policy(organization, version=1)
    policy_id, signed_at = policy["policy_id"], policy["signed_at"]
    subject = stack.opened()

    # 1. La declaración: los cinco campos, la política de la planta y la firma con licencia.
    response = stack.declare(
        who,
        subject,
        stack.declaration_body(
            responsible, review_due_at=format_timestamp(stack.now() + timedelta(seconds=2))
        ),
    )
    assert response.status_code == 201, response.text
    declaration = response.json()
    assert declaration["lifecycle_state"] == "unclosable"
    assert declaration["policy_id"] == str(policy_id)
    assert declaration["policy_version"] == 1
    (record,) = stack.records_of(subject, "unclosable_state_declared")
    content = record["content"]
    assert content["evaluated_alternative"] == ALTERNATIVE
    assert content["rejection_reason"] == REASON
    assert content["compensatory_measure"] == COMPENSATORY
    assert content["decision_date"] == DECISION_DATE
    assert content["responsible_user_id"] == str(responsible)
    assert content["signer"]["professional_license"] == LICENSE
    assert content["policy"]["policy_id"] == str(policy_id)
    assert content["policy"]["version"] == 1
    assert content["policy"]["document_sha256"] == "b" * 64
    assert content["policy"]["signed_at"] == format_timestamp(signed_at)
    assert stack.events(organization, uuid.UUID(declaration["record_id"])) == ["finding_unclosable"]
    (row,) = stack.declaration_rows(subject)
    assert (row["state"], row["declaration_id"]) == (
        "active",
        uuid.UUID(declaration["declaration_id"]),
    )

    # 2. Al llegar la fecha, la tarea diaria marca pendiente de revisión y no reabre el lazo.
    stack.reach_review_date(subject)
    stack.task_due()
    assert stack.run(stack.scheduler("worker-a").run_due(TASK_NAME)) is not None
    projection = stack.world.finding_state(subject)
    assert projection is not None
    assert projection["unclosable_pending_review"] is True
    assert projection["lifecycle_state"] == "unclosable"
    assert [event["payload"]["declaration_id"] for event in stack.due_events(organization)] == [
        declaration["declaration_id"]
    ]

    # 3. La revisión renueva la declaración (T11): registro nuevo, fecha nueva, sin reabrir.
    renewed = stack.review(
        who,
        declaration["declaration_id"],
        stack.review_body(
            review_note=REVIEW_NOTE,
            new_review_due_at=format_timestamp(add_months(stack.now(), 6)),
        ),
    )
    assert renewed.status_code == 201, renewed.text
    assert renewed.json()["lifecycle_state"] == "unclosable"
    assert renewed.json()["new_action_id"] is None
    (review_row,) = stack.review_rows(declaration["declaration_id"])
    assert (review_row["outcome"], review_row["review_note"]) == ("renewed", REVIEW_NOTE)
    assert json.loads(review_row["reviewed_by"])["professional_license"] == LICENSE

    # 4. La revisión siguiente la convierte en una acción de ingeniería (T12).
    assignee = stack.user(organization)
    converted = stack.review(
        who,
        declaration["declaration_id"],
        stack.review_body(
            outcome="converted_to_action",
            review_note="Se ejecuta el resguardo fijo que la política admite como medida",
            action=stack.action_body(assignee),
        ),
    )
    assert converted.status_code == 201, converted.text
    assert converted.json()["lifecycle_state"] == "action_assigned"
    projection = stack.world.finding_state(subject)
    assert projection is not None
    assert projection["current_action_id"] == uuid.UUID(converted.json()["new_action_id"])
    # P4: la declaración y la revisión anteriores siguen ahí, con su texto y su firma.
    assert len(stack.records_of(subject, "unclosable_state_declared")) == 1
    assert len(stack.records_of(subject, "unclosable_review")) == 2
    assert len(stack.review_rows(declaration["declaration_id"])) == 2
    (declaration_row,) = stack.declaration_rows(subject)
    assert declaration_row["state"] == "converted"
    assert declaration_row["evaluated_alternative"] == ALTERNATIVE
    assert declaration_row["declared_at"] is not None


def test_h26_without_the_signed_policy_there_is_no_declaration(stack: UnclosableStack) -> None:
    """La compuerta que falta se nombra: la planta de B no tiene política en toda la prueba."""
    organization = stack.world.b
    who = stack.member(organization)
    responsible = stack.user(organization)
    subject = stack.opened(organization)
    before = stack.untouched(subject)
    response = stack.declare(who, subject, stack.declaration_body(responsible))
    assert _error(response) == _NOT_SIGNED
    assert stack.untouched(subject) == before
    assert stack.declaration_rows(subject) == []


def test_h26_a_revoked_policy_keeps_what_was_declared(stack: UnclosableStack) -> None:
    """BR-LAZ-56: lo declarado se conserva aunque la política se revoque después (P4)."""
    organization = stack.world.a
    stack.ensure_signed_policy(organization, version=1)
    who = stack.member(organization)
    responsible = stack.user(organization)
    subject = stack.opened()
    declared = stack.declare(who, subject, stack.declaration_body(responsible))
    assert declared.status_code == 201, declared.text
    stack.gates.override = PlantPolicy(
        kind="revoked",
        policy_id=uuid.uuid4(),
        version=2,
        signed_at=stack.now(),
        document_sha256="c" * 64,
    ).state()
    try:
        assert _error(stack.declare(who, stack.opened(), stack.declaration_body(responsible))) == (
            400,
            "loop_unclosable_policy_revoked",
        )
        (row,) = stack.declaration_rows(subject)
        assert (row["state"], row["policy_version"]) == ("active", "1")
        assert row["evaluated_alternative"] == ALTERNATIVE
        assert len(stack.records_of(subject, "unclosable_state_declared")) == 1
    finally:
        stack.gates.override = None


def test_h26_the_review_note_passes_the_two_validators(stack: UnclosableStack) -> None:
    """La nota de la revisión es texto libre de §5.2: la atribución a una persona la rechaza."""
    organization = stack.world.a
    stack.ensure_signed_policy(organization, version=1)
    who = stack.member(organization)
    responsible = stack.user(organization)
    subject = stack.opened()
    declared = stack.declare(who, subject, stack.declaration_body(responsible))
    assert declared.status_code == 201, declared.text
    before = stack.untouched(subject)
    response = stack.review(
        who,
        declared.json()["declaration_id"],
        stack.review_body(
            review_note="El operario omitió el bloqueo de energía antes de intervenir la prensa"
        ),
    )
    assert _error(response) == (400, "loop_person_attribution_detected")
    assert stack.untouched(subject) == before
    assert stack.review_rows(declared.json()["declaration_id"]) == []
    # Y el mismo texto sin la persona se acepta.
    assert (
        stack.review(
            who,
            declared.json()["declaration_id"],
            stack.review_body(
                review_note="El bloqueo de energía no estaba exigido antes de intervenir la prensa"
            ),
        ).status_code
        == 201
    )


def test_h26_the_review_needs_the_license_and_the_right_role(stack: UnclosableStack) -> None:
    """Sin licencia no hay firma (BR-LAZ-59); sin ``findings.close_and_sign`` no hay revisión."""
    organization = stack.world.a
    stack.ensure_signed_policy(organization, version=1)
    who = stack.member(organization)
    responsible = stack.user(organization)
    subject = stack.opened()
    declared = stack.declare(who, subject, stack.declaration_body(responsible))
    assert declared.status_code == 201, declared.text
    declaration_id = declared.json()["declaration_id"]
    before = stack.untouched(subject)
    assert _error(
        stack.review(who, declaration_id, stack.review_body(professional_license=None))
    ) == (400, "loop_signer_license_missing")
    assert _error(
        stack.review(
            stack.member(organization, Role.LINE_MANAGER), declaration_id, stack.review_body()
        )
    ) == (404, None)
    assert stack.untouched(subject) == before
    assert stack.review_rows(declaration_id) == []
    zone = stack.member(organization, Role.COORDINATOR_SST, ScopeLevel.ZONE, organization.zone_id)
    assert stack.review(zone, declaration_id, stack.review_body()).status_code == 201
