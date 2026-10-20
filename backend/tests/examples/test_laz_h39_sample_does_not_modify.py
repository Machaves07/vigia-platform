"""H-39 · las reclasificaciones se auditan por muestra, y la muestra no modifica (TASK-347;
SUP-U05-26; BR-LAZ-106 y 107; L-1; P4).

Historia: un hallazgo confirmado se **reclasifica** como falso positivo con motivo reforzado (L-1:
el abuso de suavizar el registro). Entra en la muestra del paquete mensual sellado. La
coordinadora de SST la revisa y concluye ``criterion_error`` con un comentario: el resultado queda
en el expediente como registro ``sample_audit_result``, que cita la clasificación auditada, y la
muestra queda completa. **La reclasificación sigue intacta**: el hallazgo conserva la salida
reclasificada, los dos registros ``classification`` no cambian ni un byte y nada en la ruta de la
auditoría permite revertirla (``sample_cannot_modify_classification``).

Sobre PostgreSQL 16 real como ``vigia_app`` y la aplicación real (``monthly_review_stack``), con
la política de texto libre de producción.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.monthly_review_support import MonthlyReviewStack, monthly_review_stack
from vigia_platform.shared.context import Role, ScopeLevel

pytestmark = pytest.mark.integration

COMMENT = "La reclasificación no cita evidencia que contradiga la coexistencia del clip"


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[MonthlyReviewStack]:
    with monthly_review_stack(postgres_endpoint, "laz_h39_sample_does_not_modify") as built:
        yield built


def test_h39_the_sample_review_is_recorded_and_the_reclassification_stays(
    stack: MonthlyReviewStack,
) -> None:
    organization = stack.actions.world.a
    organization_id = organization.organization_id
    subject = stack.actions.opened(organization)
    stack.actions.classify(subject, "false_positive", "no_person_present", reinforced=True)
    (row,) = stack.fetch(
        "SELECT classification_id FROM loop.classification WHERE organization_id = $1"
        " AND subject_id = $2 AND sequence_in_subject = 2",
        organization_id,
        subject.subject_id,
    )
    reclassification = uuid.UUID(str(row["classification_id"]))
    seed = stack.seed_sample(organization, [(subject, reclassification)])
    coordinator = stack.member(
        organization, Role.COORDINATOR_SST, ScopeLevel.ZONE, organization.zone_id
    )
    finding_before = stack.actions.world.finding_state(subject)
    assert finding_before is not None
    assert finding_before["current_classification_outcome"] == "false_positive"
    records_before = stack.classification_records(organization_id)
    assert len(records_before) >= 2

    # Un intento de revertir la reclasificación desde la muestra no escribe nada.
    revert = stack.sample_result(coordinator, seed.item_ids[0], {"outcome": "confirmed"})
    assert revert.status_code == 400, revert.text
    assert revert.json()["detail_code"] == "accreditation_sample_cannot_modify_classification"

    # La revisión: observa y deja constancia.
    response = stack.sample_result(
        coordinator, seed.item_ids[0], {"outcome": "criterion_error", "comment": COMMENT}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["outcome"] == "criterion_error"
    assert body["classification_id"] == str(reclassification)
    assert body["review_completed"] is True

    (record,) = stack.results_of(seed)
    assert str(record["record_id"]) == body["record_id"]
    assert record["content"]["outcome"] == "criterion_error"
    assert record["content"]["comment"] == COMMENT
    (audited,) = [
        classification
        for classification in stack.records_of(organization_id, "classification")
        if classification["content"]["classification_id"] == str(reclassification)
    ]
    assert record["content"]["classification_record_id"] == str(audited["record_id"])
    assert audited["content"]["is_reclassification"] is True
    assert stack.sample_row(seed.sample_id)["review_completed_at"] is not None

    # La reclasificación sigue intacta: los registros, byte a byte, y el hallazgo.
    assert stack.classification_records(organization_id) == records_before
    assert stack.actions.world.finding_state(subject) == finding_before

    # Y un segundo resultado no cabe: la constancia es única.
    again = stack.sample_result(coordinator, seed.item_ids[0], {"outcome": "consistent_criterion"})
    assert again.status_code == 409, again.text
    assert again.json()["detail_code"] == "accreditation_sample_already_reviewed"
