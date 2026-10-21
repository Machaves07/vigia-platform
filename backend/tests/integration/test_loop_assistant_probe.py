"""La sonda del circuito del asistente contra la dependencia real (VIG-272; PAT-LAZ-RES-04;
FS-LAZ-01 de TASK-329; BR-NUC-79).

Sobre PostgreSQL 16 real como ``vigia_app``, el ``Dispatcher`` real de U-02 con su
``ConsumerBreaker`` y ``AssistantWorker`` con ``FakeAssistantProvider`` (``assistant_stack``). Los
tiempos son del reloj simulado (``sleep`` inyectado que lo avanza), nunca de pared.

El fallo declarado deja la solicitud ``unavailable`` y su entrega pendiente; la sonda de 60 s
vuelve a entregar ese evento. Como la solicitud ya está resuelta, el consumidor no la revive: con
el circuito sin cerrar hace **la sonda del puerto** (``AssistantProviderPort.probe``: sin contexto
y sin fichas) y solo si el proveedor responde termina la entrega y el circuito se cierra:

- proveedor aún caído → la sonda falla, el circuito sigue ``open`` (reabierto en la hora de la
  sonda), la entrega sigue pendiente sin intentos y la solicitud resuelta no cambia;
- sondas cada 60 s y cierre solo al recuperarse (``[open, open, open, closed]`` a 59,999, 60,
  120 y 180 s), cero cola muerta, y la siguiente solicitud real responde;
- una sonda que toma una solicitud pendiente y la resuelve **sin llamar** (tope duro vencido)
  también pregunta al proveedor antes de cerrar;
- un proveedor que responde, aunque con un error permanente, cierra (la dependencia respondió);
- con el circuito cerrado, una entrega repetida de una solicitud resuelta no sondea; sin clave
  del proveedor, tampoco (no hay nada que sondear: toda solicitud es ``no_api_key``).

Solo datos generados. Las comparaciones son de códigos, valores de listas cerradas o igualdad
exacta.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any, Final

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.loop_assistant_support import AssistantStack, assistant_stack
from tests.loop_classification_support import Who
from vigia_platform.loop.adapters.assistant.fake import FakeAssistantProvider, Script
from vigia_platform.loop.assistant.consumer import ATTEMPT_TIMEOUT, AssistantProviderDown
from vigia_platform.shared.outbox.dispatcher import Outcome

pytestmark = pytest.mark.integration

PROBE: Final = 60
DEADLINE: Final = 120
DOWN: Final = Script.OVERLOADED
UP: Final = Script.VALID_WITH_CITATIONS


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[AssistantStack]:
    with assistant_stack(postgres_endpoint, "loop_assistant_probe") as built:
        yield built


@pytest.fixture(autouse=True)
def _clean(stack: AssistantStack) -> Iterator[None]:
    stack.settle_deliveries()
    stack.sleep.waits.clear()
    yield
    stack.settle_deliveries()
    stack.run(stack.base.world.stack.release())


@pytest.fixture
def coordinator(stack: AssistantStack) -> Who:
    """Una persona nueva por prueba: el límite de 20 por hora es por usuario."""
    return stack.coordinator("a")


def _dead_letters(stack: AssistantStack) -> int:
    (row,) = stack.fetch(
        "SELECT count(*) AS n FROM shared.outbox_delivery"
        " WHERE consumer_name = 'assistant_worker' AND status = 'dead_letter'"
    )
    return int(row["n"])


def _opened(stack: AssistantStack, provider: FakeAssistantProvider, who: Who) -> tuple[str, Any]:
    """Una solicitud cuyo fallo declarado abre el circuito: ``(request_id, circuit_opened_at)``."""
    stack.use(provider)
    request_id = stack.requested(who)
    assert stack.dispatch().count(Outcome.DEPENDENCY_DOWN) == 1
    state, opened_at = stack.circuit()
    assert state == "open" and opened_at is not None
    return request_id, opened_at


def test_a_probe_against_a_provider_still_down_keeps_the_circuit_open(
    stack: AssistantStack, coordinator: Who
) -> None:
    provider = stack.provider(DOWN)
    request_id, _ = _opened(stack, provider, coordinator)
    resolved = stack.request_row(request_id)
    assert (resolved["state"], resolved["unavailable_cause"]) == ("unavailable", "provider_error")
    calls = provider.call_count
    stack.clock.advance(PROBE)
    probed_at = stack.now()
    report = stack.dispatch()
    assert report.probed and report.count(Outcome.DEPENDENCY_DOWN) == 1
    state, reopened_at = stack.circuit()
    assert state == "open"
    # Reabierto en la hora de la sonda (más la latencia del doble): la siguiente, 60 s después.
    assert reopened_at == probed_at + timedelta(seconds=2)
    # Una sonda externa, sin contexto ni fichas; la solicitud resuelta no se revive.
    assert provider.probe_timeouts == [ATTEMPT_TIMEOUT]
    assert provider.call_count == calls
    assert stack.request_row(request_id) == resolved
    assert stack.delivery(request_id) == {"status": "pending", "attempts": 0}
    assert _dead_letters(stack) == 0


def test_the_probe_runs_every_60_seconds_and_closes_only_on_recovery(
    stack: AssistantStack, coordinator: Who
) -> None:
    # Llamadas y sondas comparten la cadena del doble: tres llamadas y dos sondas caídas; la
    # tercera sonda (a los 180 s de la apertura) encuentra al proveedor recuperado.
    provider = stack.provider([DOWN] * 5 + [UP], latency=timedelta(0))
    request_id, opened_at = _opened(stack, provider, coordinator)
    calls = provider.call_count
    states, probed = [], []
    for seconds in (59.999, 60, 120, 180):
        stack.clock.set(opened_at + timedelta(seconds=seconds))
        report = stack.dispatch()
        states.append(stack.circuit()[0])
        probed.append(report.probed)
    assert states == ["open", "open", "open", "closed"]
    assert probed == [False, True, True, True]
    assert len(provider.probe_timeouts) == 3 and provider.call_count == calls
    assert stack.request_row(request_id)["state"] == "unavailable"
    assert stack.delivery(request_id) == {"status": "delivered", "attempts": 0}
    assert _dead_letters(stack) == 0
    # Cerrado, la siguiente solicitud real llega al proveedor y responde.
    recovered = stack.requested(coordinator)
    assert stack.dispatch().count(Outcome.DELIVERED) == 1
    assert stack.request_row(recovered)["state"] == "ready"
    assert provider.call_count == calls + 1


def test_a_probe_that_resolves_a_pending_request_without_a_call_still_asks_the_provider(
    stack: AssistantStack, coordinator: Who
) -> None:
    provider = stack.provider([DOWN, UP], latency=timedelta(0))
    stack.use(provider)
    request_id = stack.requested(coordinator)
    # Otro proceso abrió el circuito antes de entregar esta solicitud, que sigue pendiente.
    stack.set_circuit("open", stack.now())
    stack.clock.advance(DEADLINE)
    report = stack.dispatch()
    assert report.probed and report.count(Outcome.DEPENDENCY_DOWN) == 1
    row = stack.request_row(request_id)
    assert (row["state"], row["unavailable_cause"], row["attempt_count"]) == (
        "unavailable",
        "timeout",
        0,
    )
    assert provider.call_count == 0 and len(provider.probe_timeouts) == 1
    assert stack.circuit()[0] == "open"
    assert stack.delivery(request_id) == {"status": "pending", "attempts": 0}
    stack.clock.advance(PROBE)
    assert stack.dispatch().count(Outcome.DELIVERED) == 1
    assert stack.circuit() == ("closed", None)
    assert provider.call_count == 0 and len(provider.probe_timeouts) == 2


@pytest.mark.parametrize(
    ("answer", "state"),
    [(DOWN, "open"), (Script.TIMEOUT, "open"), (Script.BAD_REQUEST, "closed"), (UP, "closed")],
)
def test_only_a_provider_that_answers_closes_the_circuit(
    stack: AssistantStack, coordinator: Who, answer: Script, state: str
) -> None:
    provider = stack.provider([DOWN] * 3 + [answer], hang_until=timedelta(hours=1))
    _opened(stack, provider, coordinator)
    stack.clock.advance(PROBE)
    assert stack.dispatch().probed
    assert stack.circuit()[0] == state
    assert len(provider.probe_timeouts) == 1


def test_with_the_circuit_closed_a_repeated_delivery_does_not_probe(
    stack: AssistantStack, coordinator: Who
) -> None:
    provider = stack.provider(DOWN)
    stack.use(provider)
    request_id = stack.requested(coordinator)
    with pytest.raises(AssistantProviderDown):
        stack.deliver(request_id)  # fallo declarado, fuera del despachador: no abre nada
    assert stack.circuit()[0] == "closed"
    calls = provider.call_count
    stack.deliver(request_id)  # entrega repetida: idempotente, sin llamada ni sonda
    assert provider.call_count == calls and provider.probe_timeouts == []


def test_without_a_provider_key_the_probe_does_not_call_and_closes(
    stack: AssistantStack, coordinator: Who
) -> None:
    provider = stack.provider(DOWN)
    request_id, _ = _opened(stack, provider, coordinator)
    unconfigured = FakeAssistantProvider(DOWN, configured=False)
    stack.use(unconfigured)
    stack.clock.advance(PROBE)
    assert stack.dispatch().count(Outcome.DELIVERED) == 1
    assert stack.circuit() == ("closed", None)
    assert unconfigured.probe_timeouts == [] and unconfigured.call_count == 0
    assert stack.delivery(request_id)["status"] == "delivered"
