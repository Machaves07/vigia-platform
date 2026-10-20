"""PR-LAZ-31: ninguna operación de auditoría cambia una clasificación (TASK-347; BR-LAZ-106 y 107;
H-39; L-1).

business-logic-model.md de U-04 §11, ``audit_commands``. Contra PostgreSQL 16 real como
``vigia_app``, por la ruta ``POST /accreditation/sample-items/{sample_item_id}/result`` de la
aplicación real, con el escritor y los validadores de producción. Cada ejemplo siembra un paquete
sellado con una muestra de ``AUDIT_ITEMS`` elementos sobre **clasificaciones reales** (escritas
una vez por el ``ClassificationService``) y ejecuta la secuencia generada:

- **invariante**: el conjunto de registros ``classification`` de la organización es idéntico
  antes y después, **registro a registro y hash a hash**; tampoco cambian la proyección del
  hallazgo (``loop.finding_state``), el historial (``loop.classification``), las etiquetas ni las
  acciones;
- **un intento de modificar** (una salida de clasificación como resultado o un campo de la
  clasificación en el cuerpo) responde ``sample_cannot_modify_classification`` y no escribe nada;
- el código de cada respuesta es el del **modelo**: el primer resultado válido de un elemento es
  ``201``; los siguientes, ``409 sample_already_reviewed``; un comentario con un término bloqueado
  sobre un elemento sin revisar, ``loop_blocked_vocabulary_term``;
- se escribe **exactamente un** ``sample_audit_result`` por resultado aceptado, y la fila de cada
  elemento lleva el resultado del primero.

Solo datos generados.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Final

import pytest
from hypothesis import HealthCheck, event, given, settings

from tests.integration.conftest import PostgresEndpoint
from tests.loop_classification_support import BLOCKED_REASON
from tests.monthly_review_support import MonthlyReviewStack, monthly_review_stack
from tests.properties.laz.strategies.monthly_review import (
    AUDIT_ITEMS,
    AuditCommand,
    audit_commands,
)
from vigia_platform.shared.context import Role, ScopeLevel

pytestmark = pytest.mark.integration

EXAMPLES: Final = 25
"""Cada ejemplo siembra un paquete y su muestra y hace hasta ocho peticiones reales."""
COMMENT: Final = "El criterio aplicado coincide con el procedimiento de la planta"
_COMMENTS: Final = {"none": None, "valid": COMMENT, "blank": "   ", "blocked": BLOCKED_REASON}


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[MonthlyReviewStack]:
    with monthly_review_stack(postgres_endpoint, "pr_laz_31") as built:
        yield built


def _body(command: AuditCommand) -> dict[str, Any]:
    body: dict[str, Any] = {"outcome": command.outcome}
    comment = _COMMENTS[command.comment]
    if comment is not None:
        body["comment"] = comment
    if command.field is not None:
        body[command.field] = "false_positive"
    return body


def _expected(command: AuditCommand, reviewed: set[int]) -> tuple[int, str | None]:
    """El modelo, en el orden del servicio: intento de modificar, revisado, validadores."""
    if command.kind != "result":
        return 400, "accreditation_sample_cannot_modify_classification"
    if command.item in reviewed:
        return 409, "accreditation_sample_already_reviewed"
    if command.comment == "blocked":
        return 400, "loop_blocked_vocabulary_term"
    return 201, None


def test_pr_laz_31_no_audit_operation_changes_a_classification(stack: MonthlyReviewStack) -> None:
    organization = stack.actions.world.a
    organization_id = organization.organization_id
    classified = [stack.classified(organization) for _ in range(AUDIT_ITEMS)]
    coordinator = stack.member(
        organization, Role.COORDINATOR_SST, ScopeLevel.ZONE, organization.zone_id
    )

    @settings(
        max_examples=EXAMPLES,
        deadline=None,
        database=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(audit_commands())
    def run(commands: list[AuditCommand]) -> None:
        seed = stack.seed_sample(organization, classified)
        before = (stack.classification_records(organization_id), stack.loop_rows(organization_id))
        reviewed: dict[int, str] = {}
        for command in commands:
            event(f"comando: {command.kind}")
            expected_status, expected_code = _expected(command, set(reviewed))
            response = stack.sample_result(coordinator, seed.item_ids[command.item], _body(command))
            assert response.status_code == expected_status, (command, response.text)
            if expected_code is not None:
                assert response.json()["detail_code"] == expected_code, (command, response.text)
            else:
                reviewed[command.item] = command.outcome
            # La invariante, tras **cada** operación.
            after = (
                stack.classification_records(organization_id),
                stack.loop_rows(organization_id),
            )
            assert after == before, command

        records = stack.results_of(seed)
        assert sorted(record["content"]["sample_item_id"] for record in records) == sorted(
            str(seed.item_ids[item]) for item in reviewed
        )
        rows = {row["sample_item_id"]: row for row in stack.item_rows(seed.sample_id)}
        for index, item_id in enumerate(seed.item_ids):
            assert rows[item_id]["outcome"] == reviewed.get(index)
        completed = stack.sample_row(seed.sample_id)["review_completed_at"]
        assert (completed is not None) == (len(reviewed) == AUDIT_ITEMS)

    run()
