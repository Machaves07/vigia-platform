"""H-25 · cerrar un hallazgo con acción asignada y evidencia (TASK-343, VIG-233; LC-LAZ-09;
RF-PLA-02; BR-LAZ-36, 42 y 43; F02 a F04).

Historia: el coordinador de SST asigna una acción de ingeniería, añade una entrada de evidencia
estructurada y firma el cierre con su nombre y su licencia profesional. El hallazgo pasa a
``closed`` (T09), el registro ``closure`` cita la entrada de evidencia y el signer con licencia,
y el evento ``finding_closed`` se publica una sola vez.

Sobre PostgreSQL 16 real como ``vigia_app`` y la aplicación real (``actions_stack``), con la
política de texto libre de producción.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.loop_actions_support import ActionsStack, Who, actions_stack

pytestmark = [pytest.mark.integration, pytest.mark.laz_examples]


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[ActionsStack]:
    with actions_stack(postgres_endpoint, "laz_h25_closure_with_evidence") as built:
        yield built


def test_h25_closure_with_evidence(stack: ActionsStack) -> None:
    coordinator: Who = stack.member(stack.world.a)
    assignee = stack.user(stack.world.a)
    finding, action_id = stack.with_evidence(coordinator, assignee)

    before = stack.untouched(finding)[0]

    response = stack.close(coordinator, finding, stack.closure_body())
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["lifecycle_state"] == "closed"
    assert body["action_id"] == str(action_id)
    assert len(body["evidence_entry_ids"]) == 1

    record_id = uuid.UUID(body["record_id"])
    (closure_record,) = stack.records_of(finding, "closure")
    assert closure_record["record_id"] == record_id
    content = closure_record["content"]
    assert content["action_id"] == str(action_id)
    assert content["action_type"] == "engineering"
    assert content["evidence_entry_ids"] == body["evidence_entry_ids"]
    assert content["signer"]["professional_license"]
    assert content["signer"]["display_name"]

    assert stack.events(finding.organization, record_id) == ["finding_closed"]

    row = stack.world.finding_state(finding)
    assert row is not None
    assert row["lifecycle_state"] == "closed"
    assert row["closure_record_id"] == record_id
    assert row["closed_at"] is not None

    # Append-only: ningún registro ni fila anteriores se tocan; solo crece el expediente.
    after = stack.untouched(finding)[0]
    assert after[0] == before[0] + 1  # un registro más (closure)
    assert after[1] == before[1] + 1  # un evento más (finding_closed)
    # La fila de ``loop.corrective_action`` de la acción vigente no se reescribe por el cierre.
    (action_row,) = [a for a in stack.action_rows(finding) if a["action_id"] == action_id]
    assert action_row["state"] == "assigned"
