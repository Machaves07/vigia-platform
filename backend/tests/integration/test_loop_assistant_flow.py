"""SUP-U05-19 y 20 y ``assistant_worker``: el asistente de causa raíz de punta a punta (TASK-316;
LC-LAZ-13 y 15; BR-LAZ-67 a 76; NFR-LAZ-31 y 35).

Sobre PostgreSQL 16 real como ``vigia_app``, la aplicación real y el consumidor real con
``FakeAssistantProvider`` (``assistant_stack``); los tiempos son del reloj simulado (``sleep``
inyectado que lo avanza), nunca de pared:

- **Admisión**: ``202`` con la solicitud ``pending``, su ``context_digest`` (el del objeto de la
  frontera recompuesto) y el evento ``assistant_requested`` con **solo identificadores**; nada en
  el expediente.
- **Borrador**: con citas → ``ready``, borrador **fuera del expediente** con el modelo **de la
  respuesta**, las citas resueltas (el alias y su registro real) y el uso de la solicitud (fichas,
  región, motivo de parada, duración); con evidencia insuficiente → ``ready`` sin citas.
- **Sin borrador**: esquema inválido, cita fuera de la lista → ``rejected_by_validators`` y ``GET``
  ``loop_assistant_output_rejected``; ``refusal`` (200), tope de fichas y 400 → ``unavailable`` con
  ``provider_error``. Ninguno deja fila en ``assistant_draft``.
- **Degradación** (PR-LAZ-21): sin clave, ``no_api_key`` con cero llamadas y cero esperas; con el
  circuito abierto, ``503 loop_assistant_unavailable`` con ``Retry-After`` de la sonda, sin fila,
  sin evento y sin llamada.
- **Tiempos** (NFR-LAZ-35): el guion de 90 s se corta a 60 s y el segundo intento propio (tras 5 s)
  responde; el de 150 s acaba ``timeout`` dentro de los 120 s; un 429 espera lo que dice su
  cabecera; tres 529 dan ``provider_error`` tras 5 s y 20 s; vencido el tope antes de la entrega,
  ``timeout`` sin llamada. El fallo declarado abre el ``ConsumerBreaker`` de U-02 (despachador
  real), la entrega no consume intentos, la API responde ``circuit_open`` y la sonda de 60 s lo
  cierra sin llamar.
- **Presupuesto**: al 100 % del tope de fichas, la admisión responde ``503`` sin fila y el
  consumidor deja ``budget_exceeded`` **sin construir el contexto** (cero llamadas al
  constructor y al proveedor); justo por debajo, se admite.
- **Una sola vez**: dos entregas simultáneas del mismo evento, desde dos conexiones, hacen **una**
  llamada al proveedor (mutación sin el candado de la solicitud en rojo, Evidence del PR).
- **Alcance**: el análisis o la solicitud de otra organización responden ``not_found``.

Solo datos generados. Las comparaciones son de códigos, valores de listas cerradas o igualdad
exacta (VIG-194).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, Final

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.loop_assistant_support import AssistantStack, assistant_stack
from tests.loop_classification_support import Who
from tests.loop_root_cause_support import STATEMENTS, complete_signature
from vigia_platform.loop.adapters.assistant.fake import (
    FAKE_MODEL,
    FAKE_MODEL_VERSION,
    FakeAssistantProvider,
    Script,
)
from vigia_platform.loop.assistant import consumer as consumer_module
from vigia_platform.loop.assistant.budget import MONTHLY_INPUT_TOKENS, MONTHLY_OUTPUT_TOKENS
from vigia_platform.loop.assistant.consumer import AssistantProviderDown
from vigia_platform.loop.assistant.context import build_context, canonical_json, context_digest
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.outbox.dispatcher import Outcome
from vigia_platform.shared.outbox.publish import OutboxEvent

pytestmark = pytest.mark.integration

ATTEMPT: Final = 60
DEADLINE: Final = 120
LOCK_WAIT_SECONDS: Final = 120.0
"""Tope de pared de la prueba concurrente: solo evita colgarse, no decide el resultado."""


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[AssistantStack]:
    with assistant_stack(postgres_endpoint, "loop_assistant_flow") as built:
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


def _status(stack: AssistantStack, who: Who, request_id: str) -> dict[str, Any]:
    response = stack.get(who, request_id)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _ledger_records(stack: AssistantStack) -> int:
    (row,) = stack.fetch("SELECT count(*) AS n FROM ledger.ledger_record")
    return int(row["n"])


def _elapsed(row: dict[str, Any]) -> timedelta:
    elapsed: timedelta = row["resolved_at"] - row["requested_at"]
    return elapsed


# --- admisión ------------------------------------------------------------------------------------


def test_a_request_is_admitted_pending_with_its_digest_and_an_ids_only_event(
    stack: AssistantStack, coordinator: Who
) -> None:
    analysis_id = stack.analysis(coordinator)
    records = _ledger_records(stack)
    response = stack.post(coordinator, analysis_id)
    assert response.status_code == 202, response.text
    body = response.json()
    assert (body["assistant_state"], body["unavailable_cause"], body["draft"]) == (
        "pending",
        None,
        None,
    )
    assert response.headers["cache-control"] == "no-store"
    row = stack.request_row(body["request_id"])
    assert (row["state"], row["attempt_count"], row["resolved_at"]) == ("pending", 0, None)
    analysis = stack.base.analysis_row(analysis_id)
    built = build_context(
        analysis["zone_context"],
        request_id=uuid.UUID(str(row["request_id"])),
        anchor_record_id=stack.anchor_of(analysis["subject_id"]),
    )
    assert row["context_digest"] == built.digest
    event = stack.event_of(body["request_id"])
    assert event.payload == {
        "request_id": body["request_id"],
        "analysis_id": analysis_id,
        "zone_id": str(analysis["zone_id"]),
    }
    assert row["outbox_event_id"] == event.event_id
    assert _ledger_records(stack) == records  # nada en el expediente


# --- borrador -----------------------------------------------------------------------------------


def test_a_valid_draft_with_citations_is_ready_outside_the_ledger(
    stack: AssistantStack, coordinator: Who
) -> None:
    provider = stack.provider(Script.VALID_WITH_CITATIONS)
    stack.use(provider)
    request_id = stack.requested(coordinator)
    records = _ledger_records(stack)
    stack.deliver(request_id)
    assert provider.call_count == 1
    row = stack.request_row(request_id)
    assert (row["state"], row["unavailable_cause"], row["attempt_count"]) == ("ready", None, 1)
    # Uso por solicitud (NFR-LAZ-31): fichas, región, motivo de parada y modelo de la respuesta.
    assert (row["input_tokens"], row["output_tokens"], row["cache_read_tokens"]) == (1200, 300, 0)
    assert (row["inference_geo"], row["stop_reason"], row["response_model"]) == (
        "us",
        "end_turn",
        FAKE_MODEL,
    )
    assert row["provider_duration_ms"] == 2000
    (draft,) = stack.drafts(request_id)
    assert (draft["model_name"], draft["model_version"], draft["provider"]) == (
        FAKE_MODEL,
        FAKE_MODEL_VERSION,
        "anthropic",
    )
    assert (draft["accepted_by_human"], draft["edited_by_human"]) == (False, False)
    assert draft["proposed_cause_category"] == "process"
    (citation,) = draft["citations"]
    context = provider.contexts[0]
    assert citation["id"] == context.citable[0].alias
    assert citation["kind"] == "record"
    analysis = stack.base.analysis_row(row["analysis_id"])
    assert citation["reference_id"] == str(stack.anchor_of(analysis["subject_id"]))
    # PR-LAZ-19 sobre lo que recibió el doble: ningún identificador real ni el título del estándar.
    sent = canonical_json(context)
    for identifier in (
        analysis["organization_id"],
        analysis["plant_id"],
        analysis["zone_id"],
        analysis["subject_id"],
        stack.anchor_of(analysis["subject_id"]),
        row["request_id"],
        analysis["zone_context"]["standard_id"],
    ):
        assert str(identifier) not in sent
    assert analysis["zone_context"]["standard_title_es"] not in sent
    assert canonical_json(context) and context_digest(context) == row["context_digest"]
    assert _ledger_records(stack) == records  # el borrador no entra al expediente
    body = _status(stack, coordinator, request_id)
    assert body["assistant_state"] == "ready"
    assert body["draft"]["is_draft"] is True
    assert body["draft"]["model"] == FAKE_MODEL
    assert body["draft"]["citations"][0]["reference_id"] == citation["reference_id"]


def test_a_draft_that_declares_insufficient_evidence_is_ready_without_citations(
    stack: AssistantStack, coordinator: Who
) -> None:
    stack.use(stack.provider(Script.VALID_INSUFFICIENT_EVIDENCE))
    request_id = stack.requested(coordinator)
    stack.deliver(request_id)
    (draft,) = stack.drafts(request_id)
    assert (draft["citations"], draft["insufficient_evidence_declared"]) == ([], True)
    assert _status(stack, coordinator, request_id)["draft"]["citations"] == []


@pytest.mark.parametrize(
    ("script", "state", "cause"),
    [
        (Script.SCHEMA_INVALID, "rejected_by_validators", None),
        (Script.CITATION_OUTSIDE_LIST, "rejected_by_validators", None),
        (Script.REFUSAL, "unavailable", "provider_error"),
        (Script.MAX_TOKENS, "unavailable", "provider_error"),
        (Script.BAD_REQUEST, "unavailable", "provider_error"),
    ],
)
def test_no_draft_is_ever_kept_for_a_rejected_or_failed_answer(
    stack: AssistantStack, coordinator: Who, script: Script, state: str, cause: str | None
) -> None:
    provider = stack.provider(script)
    stack.use(provider)
    request_id = stack.requested(coordinator)
    stack.deliver(request_id)
    assert provider.call_count == 1  # ninguno se reintenta
    row = stack.request_row(request_id)
    assert (row["state"], row["unavailable_cause"]) == (state, cause)
    assert stack.drafts(request_id) == []
    assert stack.sleep.waits == [2.0]  # la latencia del doble: ningún retroceso
    response = stack.get(coordinator, request_id)
    if state == "rejected_by_validators":
        assert response.status_code == 400, response.text
        assert response.json()["detail_code"] == "loop_assistant_output_rejected"
    else:
        assert response.status_code == 200, response.text
        assert response.json()["draft"] is None
        assert response.json()["unavailable_cause"] == cause
    if script is Script.REFUSAL:
        assert row["stop_reason"] == "refusal"


def test_an_attributing_answer_is_rejected_and_never_stored(
    stack: AssistantStack, coordinator: Who
) -> None:
    request_id = stack.requested(coordinator)
    blamed = FakeAssistantProvider(
        content=json.dumps(
            {
                "draft_text": "El operario del turno noche tuvo la culpa del atasco de la prensa.",
                "citations": [],
                "insufficient_evidence_declared": True,
                "proposed_cause_category": "organization",
            }
        ),
        clock=stack.clock,
        sleep=stack.sleep,
    )
    stack.use(blamed)
    stack.deliver(request_id)
    assert stack.request_row(request_id)["state"] == "rejected_by_validators"
    assert stack.drafts(request_id) == []


# --- degradación (PR-LAZ-21) --------------------------------------------------------------------


def test_without_a_key_the_request_is_unavailable_without_a_call_or_a_wait(
    stack: AssistantStack, coordinator: Who
) -> None:
    provider = FakeAssistantProvider(configured=False)
    stack.use(provider)
    request_id = stack.requested(coordinator)
    stack.deliver(request_id)
    row = stack.request_row(request_id)
    assert (row["state"], row["unavailable_cause"], row["attempt_count"]) == (
        "unavailable",
        "no_api_key",
        0,
    )
    assert provider.call_count == 0 and stack.sleep.waits == []
    assert _status(stack, coordinator, request_id)["unavailable_cause"] == "no_api_key"


def test_with_the_circuit_open_the_api_answers_at_once_and_creates_nothing(
    stack: AssistantStack, coordinator: Who
) -> None:
    analysis_id = stack.analysis(coordinator)
    organization = stack.base.world.a.organization_id
    before = len(stack.requests_of(organization))
    events = stack.fetch(
        "SELECT count(*) AS n FROM shared.outbox_event WHERE event_name = 'assistant_requested'"
    )[0]["n"]
    for state in ("open", "half_open"):
        stack.set_circuit(state, stack.now())
        response = stack.post(coordinator, analysis_id)
        assert response.status_code == 503, response.text
        assert response.json()["detail_code"] == "loop_assistant_unavailable"
        assert response.headers["retry-after"] == "60"
    assert len(stack.requests_of(organization)) == before
    assert (
        stack.fetch(
            "SELECT count(*) AS n FROM shared.outbox_event WHERE event_name = 'assistant_requested'"
        )[0]["n"]
        == events
    )
    # El flujo guiado sigue y firma (BR-LAZ-74).
    for index in range(1, 3):
        step = stack.base.step(
            coordinator, analysis_id, index, {"statement": STATEMENTS[index - 1]}
        )
        assert step.status_code == 200, step.text
    signed = stack.base.signature(coordinator, analysis_id, complete_signature())
    assert signed.status_code == 201, signed.text
    assert signed.json()["state"] == "signed"


# --- tiempos (NFR-LAZ-35) -----------------------------------------------------------------------


def test_a_90_second_provider_is_cut_at_60_and_the_second_own_attempt_answers(
    stack: AssistantStack, coordinator: Who
) -> None:
    provider = stack.provider(Script.TIMEOUT, hang_until=timedelta(seconds=90))
    stack.use(provider)
    request_id = stack.requested(coordinator)
    stack.deliver(request_id)
    first, second = (call.timeout for call in provider.calls)
    assert first == timedelta(seconds=ATTEMPT)
    # Lo que queda de los 120 s (``requested_at`` va en milisegundos; el reloj, en microsegundos).
    assert abs(second - timedelta(seconds=DEADLINE - ATTEMPT - 5)) < timedelta(milliseconds=1)
    waits = stack.sleep.waits
    assert waits[:2] == [ATTEMPT, 5]  # corte a los 60 s y retroceso propio de 5 s
    assert abs(waits[2] - 25) < 0.001  # el doble responde a los 90 s
    row = stack.request_row(request_id)
    assert (row["state"], row["attempt_count"]) == ("ready", 2)
    assert _elapsed(row) <= timedelta(seconds=DEADLINE)


def test_a_150_second_provider_ends_in_timeout_within_120_seconds(
    stack: AssistantStack, coordinator: Who
) -> None:
    provider = stack.provider(Script.TIMEOUT, hang_until=timedelta(seconds=150))
    stack.use(provider)
    request_id = stack.requested(coordinator)
    with pytest.raises(AssistantProviderDown):
        stack.deliver(request_id)
    row = stack.request_row(request_id)
    assert (row["state"], row["unavailable_cause"], row["attempt_count"]) == (
        "unavailable",
        "timeout",
        2,
    )
    assert _elapsed(row) <= timedelta(seconds=DEADLINE)
    assert sum(stack.sleep.waits) <= DEADLINE
    assert stack.drafts(request_id) == []


def test_a_429_waits_what_its_header_says_and_retries(
    stack: AssistantStack, coordinator: Who
) -> None:
    provider = stack.provider(
        [Script.RATE_LIMITED, Script.VALID_WITH_CITATIONS], retry_after=timedelta(seconds=30)
    )
    stack.use(provider)
    request_id = stack.requested(coordinator)
    stack.deliver(request_id)
    assert stack.sleep.waits == [2, 30, 2]  # latencia, cabecera (no los 5 s) y latencia
    assert stack.request_row(request_id)["state"] == "ready"
    assert provider.call_count == 2


def test_three_overloads_end_in_provider_error_after_5_and_20_seconds(
    stack: AssistantStack, coordinator: Who
) -> None:
    provider = stack.provider(Script.OVERLOADED)
    stack.use(provider)
    request_id = stack.requested(coordinator)
    with pytest.raises(AssistantProviderDown):
        stack.deliver(request_id)
    assert provider.call_count == 3
    assert stack.sleep.waits == [2, 5, 2, 20, 2]
    row = stack.request_row(request_id)
    assert (row["state"], row["unavailable_cause"], row["attempt_count"]) == (
        "unavailable",
        "provider_error",
        3,
    )


def test_past_the_hard_deadline_the_request_times_out_without_a_call(
    stack: AssistantStack, coordinator: Who
) -> None:
    provider = stack.provider()
    stack.use(provider)
    request_id = stack.requested(coordinator)
    stack.clock.advance(DEADLINE)
    stack.deliver(request_id)
    row = stack.request_row(request_id)
    assert (row["state"], row["unavailable_cause"]) == ("unavailable", "timeout")
    assert provider.call_count == 0


def test_the_declared_failure_opens_the_u02_breaker_and_the_probe_closes_it(
    stack: AssistantStack, coordinator: Who
) -> None:
    provider = stack.provider(Script.OVERLOADED)
    stack.use(provider)
    request_id = stack.requested(coordinator)
    report = stack.dispatch()
    assert report.count(Outcome.DEPENDENCY_DOWN) == 1
    state, opened_at = stack.circuit()
    assert state == "open" and opened_at is not None
    assert stack.delivery(request_id) == {"status": "pending", "attempts": 0}
    row = stack.request_row(request_id)
    assert (row["state"], row["unavailable_cause"]) == ("unavailable", "provider_error")
    calls = provider.call_count
    # Con el circuito abierto: la API responde de inmediato y el despachador no entrega.
    response = stack.post(coordinator, stack.analysis(coordinator))
    assert response.status_code == 503, response.text
    assert stack.dispatch().count(Outcome.PAUSED) == 1
    # La sonda, a los 60 s: la solicitud ya está resuelta, termina sin llamar y cierra.
    stack.clock.advance(60)
    probe = stack.dispatch()
    assert probe.probed and probe.count(Outcome.DELIVERED) == 1
    assert stack.circuit() == ("closed", None)
    assert provider.call_count == calls
    assert stack.delivery(request_id)["status"] == "delivered"


# --- presupuesto --------------------------------------------------------------------------------


def test_at_the_monthly_token_cap_nothing_is_admitted_nor_sent(
    stack: AssistantStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    who = stack.coordinator("b")
    organization = stack.base.world.b.organization_id
    provider = stack.provider()
    stack.use(provider)
    pending = stack.requested(who, which="b")  # admitida antes de llegar al tope
    used = sum(int(row["output_tokens"]) for row in stack.requests_of(organization))
    stack.add_tokens(organization, input_tokens=0, output_tokens=MONTHLY_OUTPUT_TOKENS - used - 1)
    admitted = stack.requested(who, which="b")  # una ficha por debajo: cabe
    stack.add_tokens(organization, input_tokens=0, output_tokens=1)
    before = len(stack.requests_of(organization))
    refused = stack.post(who, stack.analysis(who, "b"))
    assert refused.status_code == 503, refused.text
    assert refused.json()["detail_code"] == "loop_assistant_unavailable"
    assert refused.headers["retry-after"] == "3600"  # el tope no se libera hasta el otro mes
    assert len(stack.requests_of(organization)) == before
    built: list[Any] = []
    real = consumer_module.build_context

    def counting(*args: Any, **kwargs: Any) -> Any:
        built.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(consumer_module, "build_context", counting)
    for request_id in (pending, admitted):
        stack.deliver(request_id)
        row = stack.request_row(request_id)
        assert (row["state"], row["unavailable_cause"]) == ("unavailable", "budget_exceeded")
    assert built == [] and provider.call_count == 0  # sin construir el contexto ni gastar
    assert MONTHLY_INPUT_TOKENS == 1_000_000 and MONTHLY_OUTPUT_TOKENS == 200_000


# --- una sola vez -------------------------------------------------------------------------------


async def _two_deliveries(
    stack: AssistantStack,
    event: OutboxEvent,
    other: Any,
    provider: FakeAssistantProvider,
    gate: asyncio.Event,
) -> list[Any]:
    """Las dos entregas del mismo evento a la vez, cada una por su conexión."""
    async with asyncio.timeout(LOCK_WAIT_SECONDS):
        first = asyncio.ensure_future(stack.deliver_now(event))
        second = asyncio.ensure_future(stack.deliver_now(event, database=other))
        # El proveedor retiene a quien llama hasta que una de las dos entregas termina (la que no
        # tiene el candado) o hasta que las dos han llamado (la mutación).
        while not (first.done() or second.done()) and provider.call_count < 2:
            await asyncio.sleep(0.01)
        gate.set()
        return await asyncio.gather(first, second, return_exceptions=True)


def test_two_simultaneous_deliveries_make_a_single_provider_call(
    stack: AssistantStack, coordinator: Who
) -> None:
    for _ in range(3):
        gate = asyncio.Event()
        provider = stack.provider(gate=gate)
        stack.use(provider)
        request_id = stack.requested(coordinator)
        event = stack.event_of(request_id)
        other = stack.database()
        results = stack.run(_two_deliveries(stack, event, other, provider, gate))
        stack.run(other.dispose())
        assert [r for r in results if isinstance(r, BaseException)] == [], results
        assert provider.call_count == 1
        row = stack.request_row(request_id)
        assert (row["state"], row["attempt_count"]) == ("ready", 1)
        assert len(stack.drafts(request_id)) == 1


IDLE_LIMIT_MS: Final = 2_000
"""El tope de inactividad de la base en la prueba (en RDS, 60 s): el proveedor espera más."""
WALL_CALL: Final = timedelta(seconds=6)
"""Lo que tarda de verdad la llamada del doble (pared), con márgenes de segundos sobre el tope."""


async def _idle_limit_scenario(
    stack: AssistantStack, event: OutboxEvent, other: Any, provider: FakeAssistantProvider
) -> list[Any]:
    """La primera entrega llama y espera en la pared más que el tope de inactividad; pasado el
    tope, llega una segunda entrega del mismo evento por otra conexión."""
    async with asyncio.timeout(LOCK_WAIT_SECONDS):
        first = asyncio.ensure_future(stack.deliver_now(event, idle_limit_ms=IDLE_LIMIT_MS))
        while provider.call_count == 0 and not first.done():
            await asyncio.sleep(0.05)
        # Márgenes de segundos: sin el tope propio de la entrega, la base ya la habría cerrado.
        await asyncio.sleep(IDLE_LIMIT_MS / 1000 + 2)
        second = asyncio.ensure_future(
            stack.deliver_now(event, database=other, idle_limit_ms=IDLE_LIMIT_MS)
        )
        return await asyncio.gather(first, second, return_exceptions=True)


def test_a_call_longer_than_the_database_idle_limit_still_ends_ready_once(
    stack: AssistantStack, coordinator: Who
) -> None:
    """Hallazgo de la revisión (ronda 1): con ``idle_in_transaction_session_timeout`` de RDS, la
    transacción de la entrega moría a mitad de la llamada, soltaba el candado y una segunda
    entrega volvía a llamar; con los preparadores reales la respuesta se perdía."""
    provider = FakeAssistantProvider(clock=stack.clock, sleep=asyncio.sleep, latency=WALL_CALL)
    stack.use(provider, free_text=stack.production_policy())
    request_id = stack.requested(coordinator)
    event = stack.event_of(request_id)
    other = stack.database()
    results = stack.run(_idle_limit_scenario(stack, event, other, provider))
    stack.run(other.dispose())
    assert [r for r in results if isinstance(r, BaseException)] == [], results
    assert provider.call_count == 1
    row = stack.request_row(request_id)
    assert (row["state"], row["attempt_count"]) == ("ready", 1)
    assert len(stack.drafts(request_id)) == 1


def test_a_repeated_delivery_after_resolution_does_nothing(
    stack: AssistantStack, coordinator: Who
) -> None:
    provider = stack.provider()
    stack.use(provider)
    request_id = stack.requested(coordinator)
    stack.deliver(request_id)
    stack.deliver(request_id)
    assert provider.call_count == 1 and len(stack.drafts(request_id)) == 1


# --- alcance ------------------------------------------------------------------------------------


def test_another_organization_sees_nothing(stack: AssistantStack, coordinator: Who) -> None:
    other = stack.coordinator("b")
    analysis_id = stack.analysis(coordinator)
    request_id = stack.requested(coordinator, analysis_id)
    before = len(stack.requests_of(stack.base.world.a.organization_id))
    for response in (
        stack.post(other, analysis_id),
        stack.post(other, uuid.uuid4()),
        stack.get(other, request_id),
        stack.get(other, uuid.uuid4()),
    ):
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "not_found"
        assert response.json().get("detail_code") is None
    assert len(stack.requests_of(stack.base.world.a.organization_id)) == before


def test_a_coordinator_of_another_zone_sees_nothing(
    stack: AssistantStack, coordinator: Who
) -> None:
    """Guarda de alcance por zona: un coordinador SST con alcance en **otra zona** de la misma
    organización responde como con un identificador inexistente y no escribe nada."""
    site = stack.organization("a").site
    outsiders = (
        stack.base.member(
            stack.organization("a"), Role.COORDINATOR_SST, ScopeLevel.ZONE, site.other_zone_id
        ),
        stack.base.member(stack.organization("a"), Role.LINE_MANAGER),
    )
    analysis_id = stack.analysis(coordinator)
    request_id = stack.requested(coordinator, analysis_id)
    organization = stack.organization("a").organization_id
    before = (len(stack.requests_of(organization)), _events(stack))
    missing = (stack.post(coordinator, uuid.uuid4()), stack.get(coordinator, uuid.uuid4()))
    for who in outsiders:
        for response, reference in zip(
            (stack.post(who, analysis_id), stack.get(who, request_id)), missing, strict=True
        ):
            assert response.status_code == reference.status_code == 404, response.text
            assert response.json()["code"] == reference.json()["code"] == "not_found"
            assert response.json().get("detail_code") is None
    assert (len(stack.requests_of(organization)), _events(stack)) == before


def _events(stack: AssistantStack) -> int:
    (row,) = stack.fetch(
        "SELECT count(*) AS n FROM shared.outbox_event WHERE event_name = 'assistant_requested'"
    )
    return int(row["n"])
