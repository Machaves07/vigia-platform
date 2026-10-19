"""PR-LAZ-21 · con el cortacircuito abierto o sin clave del proveedor, ``unavailable`` sin llamada
externa ni espera, y el flujo guiado completa y firma (TASK-316; BR-LAZ-74; PAT-LAZ-RES-04).

Contra PostgreSQL 16 real como ``vigia_app``, la aplicación real y el consumidor real con un
``FakeAssistantProvider`` que **cuenta** sus llamadas y un temporizador que anota cada espera
(``assistant_stack``). Para cualquier estado generado (``circuit_states``: ``open``,
``half_open`` o sin clave) y cualquier flujo guiado (de 1 a 5 porqués y la firma):

- con el circuito abierto o en sonda, ``POST`` responde ``503 loop_assistant_unavailable`` de
  inmediato: ninguna fila, ningún evento, ninguna llamada, ninguna espera;
- sin clave, la solicitud se admite y el consumidor la deja ``unavailable`` con ``no_api_key``:
  cero llamadas, cero esperas, ``attempt_count = 0``;
- en los dos casos el flujo guiado de TASK-315 escribe sus porqués y **firma**: un registro
  ``root_cause_analysis`` con el texto aceptado.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.integration.conftest import PostgresEndpoint
from tests.loop_assistant_support import AssistantStack, assistant_stack
from tests.loop_root_cause_support import STATEMENTS, complete_signature
from tests.properties.laz.strategies.assistant import CircuitState, circuit_states
from vigia_platform.loop.adapters.assistant.fake import FakeAssistantProvider

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[AssistantStack]:
    with assistant_stack(postgres_endpoint, "pr_laz_21") as built:
        yield built


def _events(stack: AssistantStack) -> int:
    (row,) = stack.fetch(
        "SELECT count(*) AS n FROM shared.outbox_event WHERE event_name = 'assistant_requested'"
    )
    return int(row["n"])


@settings(max_examples=6, deadline=None)
@given(state=circuit_states(), steps=st.integers(1, len(STATEMENTS)))
def test_pr_laz_21_the_assistant_degrades_declared_and_the_guided_flow_signs(
    stack: AssistantStack, state: CircuitState, steps: int
) -> None:
    stack.settle_deliveries()
    stack.sleep.waits.clear()
    who = stack.coordinator("a")
    organization = stack.organization("a").organization_id
    provider = FakeAssistantProvider(configured=state != "no_key", clock=stack.clock)
    stack.use(provider)
    analysis_id = stack.analysis(who)
    before = (len(stack.requests_of(organization)), _events(stack))
    if state == "no_key":
        request_id = stack.requested(who, analysis_id)
        stack.deliver(request_id)
        row = stack.request_row(request_id)
        assert (row["state"], row["unavailable_cause"], row["attempt_count"]) == (
            "unavailable",
            "no_api_key",
            0,
        )
        status: Any = stack.get(who, request_id).json()
        assert (status["assistant_state"], status["unavailable_cause"]) == (
            "unavailable",
            "no_api_key",
        )
    else:
        stack.set_circuit(state, stack.now())
        response = stack.post(who, analysis_id)
        assert response.status_code == 503, response.text
        assert response.json()["detail_code"] == "loop_assistant_unavailable"
        assert (len(stack.requests_of(organization)), _events(stack)) == before
        stack.set_circuit("closed")
    assert provider.call_count == 0
    assert stack.sleep.waits == []
    for index in range(1, steps + 1):
        step = stack.base.step(who, analysis_id, index, {"statement": STATEMENTS[index - 1]})
        assert step.status_code == 200, step.text
    signed = stack.base.signature(who, analysis_id, complete_signature())
    assert signed.status_code == 201, signed.text
    body = signed.json()
    assert body["state"] == "signed" and body["record_id"] is not None
    stack.run(stack.base.world.stack.release())
