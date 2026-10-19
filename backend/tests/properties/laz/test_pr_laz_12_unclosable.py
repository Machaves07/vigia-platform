"""PR-LAZ-12: el estado incerrable, con los cinco campos, la política y la fecha (TASK-312).

Dos mitades, como el resto de las propiedades del lazo:

- **Pura** (sin base, corre con el perfil ``ci`` en el grupo de unitarias): sobre
  ``plant_policies`` y las marcas del reloj, ``policy_status`` traduce la política de la planta
  (cargada, sin firmar, revocada) y ``review_date`` acepta exactamente ``(ahora, ahora + tope]``
  con la fecha de por defecto de la planta. Ninguna marca sale del sistema: las dos funciones son
  puras y reciben el instante.
- **Con base** (marca ``integration``, ``propiedades-integracion`` del CI): sobre PostgreSQL 16
  real y la aplicación real (``unclosable_stack``), con el puerto de la política mandado por la
  prueba, **ningún** hallazgo queda ``unclosable`` sin los cinco campos, sin política ``loaded =
  true`` en el instante de la declaración y sin fecha de revisión en rango: cada intento generado
  responde el código del **oráculo** —el orden de los pasos de BLM §8.2: política, campos, estado
  del ciclo de vida, fecha y licencia— y un rechazo no escribe **nada** (ni registro, ni evento,
  ni fila, ni proyección). La revisión tiene su propio oráculo: la salida válida, la fecha nueva
  solo al renovar y la licencia.

Solo datos generados. El reloj es el simulado del mundo (retro 15).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import IngestedSubject
from tests.loop_unclosable_support import (
    UnclosableStack,
    Who,
    unclosable_stack,
)
from tests.properties.laz.strategies.lifecycle import (
    DeclarationAttempt,
    PlantPolicy,
    ReviewAttempt,
    declaration_attempts,
    plant_policies,
    review_attempts,
)
from vigia_platform.loop.application.unclosable import add_months, policy_status, review_date
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.domain.enums import FindingLifecycleState
from vigia_platform.loop.domain.lifecycle import PolicyStatus
from vigia_platform.shared.signing.keys import format_timestamp

EXAMPLES: Final = 12
"""Cada ejemplo ingresa y clasifica un hallazgo y escribe varias transacciones reales; el perfil
``nightly`` no sube este número (como en ``test_laz_action_deadline_frozen``)."""
DECLARED_STATES: Final = ("open", "action_assigned", "overdue")
OTHER_STATES: Final = ("received", "in_review", "discarded", "closed")
REVIEW_MONTHS: Final = 12
"""``unclosable_review_max_months`` por defecto de la planta (DE §2.13)."""


# --- la mitad pura: la política y la fecha -------------------------------------------------------


@given(policy=plant_policies())
def test_policy_status_translates_the_port(policy: PlantPolicy) -> None:
    """La política se traduce sin interpretar sus criterios (BR-LAZ-53, 55 y 56)."""
    status = policy_status(policy.state())
    expected = {
        "loaded": PolicyStatus.LOADED,
        "not_signed": PolicyStatus.NOT_SIGNED,
        "revoked": PolicyStatus.REVOKED,
        "partial": PolicyStatus.LOADED,
    }[policy.kind]
    assert status is expected


@given(
    at=st.datetimes(
        timezones=st.just(UTC),
        min_value=datetime(2026, 1, 1, tzinfo=UTC),
        max_value=datetime(2030, 1, 1, tzinfo=UTC),
    ).map(lambda moment: moment.replace(microsecond=moment.microsecond // 1000 * 1000)),
    months=st.integers(-24, 36),
    ceiling=st.sampled_from((1, 6, 12)),
    default=st.sampled_from((1, 6, 12)),
)
def test_review_date_is_in_range_only_inside_the_window(
    at: datetime, months: int, ceiling: int, default: int
) -> None:
    """``review_date`` acepta ``(at, at + tope]``; sin fecha, la de por defecto de la planta."""
    if default > ceiling:
        return
    moment = add_months(at, months)
    accepted, in_range = review_date(
        format_timestamp(moment), at, default_months=default, max_months=ceiling
    )
    assert in_range == (at < moment <= add_months(at, ceiling))
    assert accepted == moment
    defaulted, always = review_date(None, at, default_months=default, max_months=ceiling)
    assert always and defaulted == add_months(at, default)


# --- la mitad con base: la declaración y la revisión ----------------------------------------------


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[UnclosableStack]:
    with unclosable_stack(postgres_endpoint, "pr_laz_12") as built:
        yield built


@pytest.fixture(scope="module")
def coordinator(stack: UnclosableStack) -> Who:
    """El reloj de esta propiedad avanza segundos (las fechas se generan en meses), así que la
    sesión del coordinador vale para todo el módulo."""
    return stack.member(stack.world.a)


@pytest.fixture(scope="module")
def responsible(stack: UnclosableStack) -> uuid.UUID:
    return stack.user(stack.world.a)


def _error(response: Any) -> tuple[int, str | None]:
    body = response.json()
    return response.status_code, body.get("detail_code")


def _declaration_code(attempt: DeclarationAttempt, state: str) -> LoopDetailCode | None:
    """El oráculo: el código del primer paso que falla, en el orden de BLM §8.2."""
    if attempt.policy.kind == "revoked":
        return LoopDetailCode.UNCLOSABLE_POLICY_REVOKED
    if attempt.policy.kind == "partial":
        # ``loaded = true`` sin el hash del documento: el registro no puede citar la política.
        return LoopDetailCode.UNCLOSABLE_POLICY_UNAVAILABLE
    if attempt.policy.kind == "not_signed":
        return LoopDetailCode.UNCLOSABLE_POLICY_NOT_SIGNED
    if attempt.fields < 5:
        return LoopDetailCode.UNCLOSABLE_FIELDS_INCOMPLETE
    if state not in DECLARED_STATES:
        return LoopDetailCode.TRANSITION_NOT_ALLOWED
    if attempt.review_months is not None and not 0 < attempt.review_months <= REVIEW_MONTHS:
        return LoopDetailCode.UNCLOSABLE_REVIEW_DATE_OUT_OF_RANGE
    if not attempt.license:
        return LoopDetailCode.SIGNER_LICENSE_MISSING
    return None


def _body(
    stack: UnclosableStack, responsible: uuid.UUID, attempt: DeclarationAttempt
) -> dict[str, Any]:
    """Los campos que van: los ``fields`` primeros de los cinco, y la fecha si toca."""
    body = stack.declaration_body(responsible)
    order = (
        "evaluated_alternative",
        "rejection_reason",
        "responsible_user_id",
        "decision_date",
        "compensatory_measure",
    )
    for name in order[attempt.fields :]:
        body.pop(name)
    if attempt.review_months is not None:
        body["review_due_at"] = format_timestamp(add_months(stack.now(), attempt.review_months))
    if not attempt.license:
        body["professional_license"] = None
    return {key: value for key, value in body.items() if value is not None}


@settings(max_examples=EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(attempt=declaration_attempts(), state=st.sampled_from((*DECLARED_STATES, *OTHER_STATES)))
@pytest.mark.integration
def test_pr_laz_12_nothing_is_unclosable_without_the_five_fields_policy_and_date(
    stack: UnclosableStack, coordinator: Who, responsible: uuid.UUID, attempt: Any, state: str
) -> None:
    subject: IngestedSubject = stack.subject_in(state)
    stack.gates.override = attempt.policy.state()
    before = stack.untouched(subject)
    response = stack.declare(coordinator, subject, _body(stack, responsible, attempt))
    expected = _declaration_code(attempt, state)
    if expected is None:
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["lifecycle_state"] == FindingLifecycleState.UNCLOSABLE.value
        assert body["policy_id"] == str(attempt.policy.policy_id)
        assert body["policy_version"] == attempt.policy.version
        assert body["policy_document_sha256"] == attempt.policy.document_sha256
        (record,) = stack.records_of(subject, "unclosable_state_declared")
        content = record["content"]
        assert content["policy"]["policy_id"] == str(attempt.policy.policy_id)
        assert content["policy"]["version"] == attempt.policy.version
        assert content["policy"]["document_sha256"] == attempt.policy.document_sha256
        assert content["signer"]["professional_license"]
        assert stack.events(subject.organization, uuid.UUID(body["record_id"])) == [
            "finding_unclosable"
        ]
        projection = stack.world.finding_state(subject)
        assert projection is not None
        assert projection["lifecycle_state"] == FindingLifecycleState.UNCLOSABLE.value
        assert projection["unclosable_since"] is not None
        assert projection["unclosable_review_due_at"] is not None
        assert projection["unclosable_pending_review"] is False
    else:
        # El estado HTTP lo fija ``HTTP_STATUS_BY_DETAIL_CODE`` (400 salvo el 503 del puerto caído).
        assert response.status_code in (400, 409, 503), response.text
        assert _error(response)[1] == expected.value, response.text
        # Un rechazo no deja nada: ni registro, ni evento, ni fila, ni proyección.
        assert stack.untouched(subject) == before
        assert stack.declaration_rows(subject) == []


def _review_code(attempt: ReviewAttempt) -> LoopDetailCode | None:
    """El oráculo de la revisión de una declaración vigente, en el orden de ``revisar_incerrable``.

    La declaración ya dejó el hallazgo ``unclosable``, así que la única puerta de estado que
    queda es la del propio registro (que la revisión no cambie de estado)."""
    if attempt.outcome == "converted_to_action" and attempt.review_months is not None:
        return None  # ``invalid_request`` sin ``detail_code``: se comprueba aparte
    if (
        attempt.outcome == "renewed"
        and attempt.review_months is not None
        and not 0 < attempt.review_months <= REVIEW_MONTHS
    ):
        return LoopDetailCode.UNCLOSABLE_REVIEW_DATE_OUT_OF_RANGE
    if not attempt.license:
        return LoopDetailCode.SIGNER_LICENSE_MISSING
    return None


@settings(max_examples=EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(attempt=review_attempts(), state=st.sampled_from((*DECLARED_STATES, *OTHER_STATES)))
@pytest.mark.integration
def test_pr_laz_12_the_review_only_renews_or_converts_with_a_valid_date_and_license(
    stack: UnclosableStack, coordinator: Who, responsible: uuid.UUID, attempt: Any, state: str
) -> None:
    """La revisión no reabre el lazo: renueva (T11) o convierte en acción (T12), con fecha nueva
    solo al renovar y con la licencia del firmante."""
    subject = stack.subject_in(state)
    stack.gates.override = PlantPolicy(
        kind="loaded",
        policy_id=uuid.uuid4(),
        version=1,
        signed_at=stack.now(),
        document_sha256="d" * 64,
    ).state()
    declared = stack.declare(coordinator, subject, stack.declaration_body(responsible))
    if state not in DECLARED_STATES:
        # Sin declaración vigente no hay revisión que probar: la declaración ya se rechazó.
        assert declared.status_code in (400, 409), declared.text
        return
    assert declared.status_code == 201, declared.text
    declaration_id = declared.json()["declaration_id"]
    body = stack.review_body()
    if attempt.review_months is not None:
        body["new_review_due_at"] = format_timestamp(add_months(stack.now(), attempt.review_months))
    if not attempt.license:
        body["professional_license"] = None
    body = {key: value for key, value in body.items() if value is not None}
    body["outcome"] = attempt.outcome
    if attempt.outcome == "converted_to_action":
        body["action"] = stack.action_body(stack.user(subject.organization))
    before = stack.untouched(subject)
    response = stack.review(coordinator, declaration_id, body)
    expected = _review_code(attempt)
    if (
        expected is None
        and attempt.outcome == "converted_to_action"
        and (attempt.review_months is not None)
    ):
        assert response.status_code == 400, response.text
        assert _error(response)[1] is None
        assert stack.untouched(subject) == before
        return
    if expected is None:
        assert response.status_code == 201, response.text
        reviewed = response.json()
        assert reviewed["outcome"] == attempt.outcome
        assert reviewed["lifecycle_state"] == (
            FindingLifecycleState.UNCLOSABLE.value
            if attempt.outcome == "renewed"
            else FindingLifecycleState.ACTION_ASSIGNED.value
        )
        (record,) = stack.records_of(subject, "unclosable_review")
        assert record["content"]["outcome"] == attempt.outcome
        assert stack.events(subject.organization, uuid.UUID(reviewed["record_id"])) == [
            "unclosable_reviewed"
        ]
        (row,) = stack.review_rows(declaration_id)
        assert row["outcome"] == attempt.outcome
    else:
        assert response.status_code in (400, 409), response.text
        assert _error(response)[1] == expected.value, response.text
        assert stack.untouched(subject) == before
        assert stack.review_rows(declaration_id) == []
