"""H-27 · lo vencido se ve y escala (TASK-313; LC-LAZ-11; RF-PLA-02 y 12; BR-LAZ-46 a 48; P5).

Historia: el coordinador asigna una acción de ingeniería con plazo; el plazo pasa sin evidencia.
La tarea ``evaluate_deadlines`` declara el vencimiento (el hallazgo pasa a ``overdue``, T08) y lo
escala al **coordinador**; el hallazgo **sigue en la bandeja** y en el **panel de vencidos** del
gerente, por zona. Al cumplir ``manager_highlight_overdue_days`` días (30, PRD §10), la tarea lo
escala al **resumen semanal del gerente** y el panel lo **destaca**. Nada lo cierra ni lo oculta:
el coordinador lo sigue gestionando y lo cierra con evidencia y firma.

**Secuencia exacta** de registros, eventos y niveles del sujeto:

1. ``action_assigned`` → ``action_assigned``;
2. día del plazo: ``action_overdue`` (``coordinator``, ``days_overdue`` 0) →
   ``finding_overdue`` y ``finding_escalated``, los dos con nivel ``coordinator``;
3. 30 días después: ``action_overdue`` (``manager_weekly``, ``days_overdue`` 30) →
   ``finding_escalated`` con nivel ``manager_weekly``;
4. ejecuciones posteriores: nada;
5. ``action_evidence_added`` y ``closure`` → ``finding_closed``.

Ningún evento ni el panel nombran a una persona observada: la zona, el hallazgo, la acción y el
responsable de la acción (un usuario de la plataforma). Sobre PostgreSQL 16 real como
``vigia_app``, la tarea real y la aplicación real (``deadlines_stack``). Solo datos generados.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Final

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import IngestedSubject
from tests.loop_deadlines_support import DeadlinesStack, deadlines_stack
from vigia_platform.loop.adapters.postgres.inbox_query import InboxFilters
from vigia_platform.loop.domain.enums import FindingLifecycleState

pytestmark = [pytest.mark.integration, pytest.mark.laz_examples]

DAY: Final = timedelta(days=1)
HIGHLIGHT_DAYS: Final = 30
"""``manager_highlight_overdue_days`` por defecto (fuente: PRD §10 y H-27)."""
_PAYLOAD_KEYS: Final = {
    "zone_id",
    "subject_kind",
    "subject_id",
    "action_id",
    "overdue_id",
    "record_id",
    "due_at",
    "days_overdue",
    "escalation_level",
    "assignee_user_id",
}


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[DeadlinesStack]:
    with deadlines_stack(postgres_endpoint, "laz_h27_escalation") as built:
        yield built


def _sequence(
    stack: DeadlinesStack, finding: IngestedSubject
) -> list[tuple[str, list[tuple[str, str]]]]:
    """``(tipo de registro, [(evento, nivel)])`` del sujeto, en el orden de la cadena."""
    out = []
    for record in stack.actions.subject_records(finding):
        events = stack.record_events(finding.organization, record["record_id"])
        out.append(
            (
                record["record_type"],
                [(name, str(payload.get("escalation_level", "-"))) for name, payload in events],
            )
        )
    return out


def test_h27_the_overdue_finding_is_seen_and_escalates(stack: DeadlinesStack) -> None:
    organization = stack.actions.world.organization()
    coordinator, assignee = stack.actions.member(organization), stack.assignee(organization)
    finding, action_id, due_at = stack.with_action(organization, coordinator, assignee, 5)
    fsid = stack.actions.finding_state_id(finding)
    records_before = [r["record_type"] for r in stack.actions.subject_records(finding)]

    # El día del plazo: vencido, escalado al coordinador, visible en la bandeja y en el panel.
    stack.tick_to(due_at)
    (first,) = stack.evaluate(organization)
    assert (first.escalation_level.value, first.days_overdue, first.transition_id) == (
        "coordinator",
        0,
        "T08",
    )
    assert stack.state(finding) == "overdue"
    page = stack.run(
        stack.inbox.page(
            stack.actions.context(stack.actions.member(organization)),
            filters=InboxFilters(lifecycle_states=(FindingLifecycleState.OVERDUE,)),
        )
    )
    assert fsid in {item.finding_state_id for item in page.items}
    manager = stack.manager(organization)
    panel = stack.get_overdue(manager, {"plant_id": str(organization.plant_id)}).json()
    (item,) = panel["items"]
    assert (item["finding_state_id"], item["zone_id"], item["highlighted"]) == (
        str(fsid),
        str(organization.zone_id),
        False,
    )
    assert panel["highlight_threshold_days"] == HIGHLIGHT_DAYS
    assert panel["zones"] == [
        {"zone_id": str(organization.zone_id), "overdue_count": 1, "highlighted_count": 0}
    ]

    # Un día antes del umbral: nada; al cumplirlo, al resumen semanal del gerente y destacado.
    stack.tick_to(due_at + (HIGHLIGHT_DAYS - 1) * DAY)
    assert stack.evaluate(organization) == ()
    stack.tick_to(due_at + HIGHLIGHT_DAYS * DAY)
    (second,) = stack.evaluate(organization)
    assert (second.escalation_level.value, second.days_overdue, second.transition_id) == (
        "manager_weekly",
        HIGHLIGHT_DAYS,
        None,
    )
    panel = stack.get_overdue(stack.manager(organization), {"plant_id": str(organization.plant_id)})
    (item,) = panel.json()["items"]
    assert (item["overdue_days"], item["highlighted"], item["declared"]) == (
        HIGHLIGHT_DAYS,
        True,
        True,
    )
    stack.tick(7 * DAY.total_seconds())
    assert stack.evaluate(organization) == ()

    # Sigue gestionable: evidencia y cierre firmado desde overdue (T09).
    coordinator = stack.actions.member(organization)
    response = stack.actions.add_evidence(coordinator, action_id, stack.actions.evidence_body())
    assert response.status_code == 201, response.text
    response = stack.actions.close(coordinator, finding, stack.actions.closure_body())
    assert response.status_code == 201, response.text
    assert stack.evaluate(organization) == ()

    sequence = _sequence(stack, finding)[len(records_before) - 1 :]
    assert sequence == [
        ("action_assigned", [("action_assigned", "-")]),
        (
            "action_overdue",
            [("finding_escalated", "coordinator"), ("finding_overdue", "coordinator")],
        ),
        ("action_overdue", [("finding_escalated", "manager_weekly")]),
        ("action_evidence_added", [("action_evidence_added", "-")]),
        ("closure", [("finding_closed", "-")]),
    ]
    for record in stack.overdue_records(finding):
        for _, payload in stack.record_events(organization, record["record_id"]):
            assert set(payload) == _PAYLOAD_KEYS
            assert payload["assignee_user_id"] == str(assignee)
