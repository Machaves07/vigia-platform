"""FS-LAZ-11: 429 con cabecera de espera y, después, presupuesto mensual de fichas al 80 y al 100 %.

El ``AnthropicAssistantProvider`` **real** (paquete del proveedor con ``max_retries = 0``) habla
con un doble del transporte (``tests.anthropic_support.Transport``: ninguna conexión sale de la
máquina) y lo atiende el ``AssistantWorker`` real por el despachador de U-02 sobre PostgreSQL 16.
Las esperas son del reloj inyectado. Las alarmas se evalúan sobre el exportador en memoria con los
umbrales de ``infra/stacks/loop.py`` (margen < 20 %, presupuesto > 80 %; NFR-LAZ-35 y 42;
PAT-LAZ-ESC-01).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Final

import httpx2
import pytest

from tests.anthropic_support import SYNTHETIC_KEY, Scripted, Transport, factory, message
from tests.attribution_support import both_policy
from tests.integration.conftest import PostgresEndpoint
from tests.loop_assistant_support import AssistantStack, assistant_stack
from tests.resilience.harness import scenario
from tests.resilience.laz_support import PROFILES, LoopMetricsExport, loop_metrics_export
from vigia_platform.loop.adapters.anthropic.client import AnthropicAssistantProvider
from vigia_platform.loop.adapters.assistant.fake import MISSING_TEXT
from vigia_platform.loop.assistant import consumer as consumer_module
from vigia_platform.loop.assistant.budget import ALARM_RATIO, MONTHLY_OUTPUT_TOKENS
from vigia_platform.loop.assistant.consumer import AssistantWorker
from vigia_platform.loop.assistant.context import build_context
from vigia_platform.loop.observability.metrics import LoopMetricName
from vigia_platform.shared.outbox.dispatcher import Outcome

pytestmark = pytest.mark.integration

HEADROOM_ALARM: Final = 0.2
"""``ASSISTANT_RATE_MARGIN_RATIO`` de ``infra/stacks/loop.py`` (menor que)."""
BUDGET_ALARM: Final = 0.8
"""``ASSISTANT_TOKEN_BUDGET_RATIO`` de ``infra/stacks/loop.py`` (mayor que)."""
RETRY_AFTER: Final = 30
_HEADROOM: Final = LoopMetricName.ASSISTANT_RATE_LIMIT_HEADROOM_RATIO
_BUDGET: Final = LoopMetricName.ASSISTANT_TOKEN_BUDGET_USED_RATIO


def _rate_headers(remaining: int, limit: int = 100) -> dict[str, str]:
    return {
        "anthropic-ratelimit-requests-limit": str(limit),
        "anthropic-ratelimit-requests-remaining": str(remaining),
    }


def _answer(remaining: int) -> Scripted:
    """Un 200 con borrador válido (evidencia insuficiente declarada) y su margen."""
    text = json.dumps(
        {
            "draft_text": MISSING_TEXT,
            "citations": [],
            "insufficient_evidence_declared": True,
            "proposed_cause_category": "process",
        },
        ensure_ascii=False,
    )
    return Scripted(
        status=200,
        body=message(content=[{"type": "text", "text": text}]),
        headers=_rate_headers(remaining),
    )


class SequenceTransport(Transport):
    """``Transport`` que responde cada petición con el siguiente guion (el último se repite)."""

    def __init__(self, scripts: list[Scripted]) -> None:
        super().__init__(scripts[0])
        self.scripts = scripts

    async def send(self, request: httpx2.Request, *args: Any, **kwargs: Any) -> httpx2.Response:
        self.script = self.scripts[min(len(self.requests), len(self.scripts) - 1)]
        return await super().send(request, *args, **kwargs)


@dataclass
class ObservingSleep:
    """El ``sleep`` del worker: anota el margen exportado al empezar cada espera y avanza el
    reloj simulado con el ``AdvancingSleep`` de la pila."""

    stack: AssistantStack
    export: LoopMetricsExport
    headroom: list[float | None] = field(default_factory=list)

    async def __call__(self, seconds: float) -> None:
        self.headroom.append(self.export.value(_HEADROOM))
        await self.stack.sleep(seconds)


def _used(stack: AssistantStack, organization: Any) -> int:
    return sum(int(row["output_tokens"]) for row in stack.requests_of(organization))


@pytest.mark.parametrize("nightly", PROFILES)
def test_rate_limit_then_exhausted_budget(
    postgres_endpoint: PostgresEndpoint, monkeypatch: pytest.MonkeyPatch, nightly: bool
) -> None:
    assert ALARM_RATIO == BUDGET_ALARM
    with (
        scenario(
            "FS-LAZ-11",
            title="Límite de tasa del proveedor y presupuesto agotado",
            injection="429 con retry-after y margen del 10 %; contador mensual al 80 % y 100 %",
            expected="Transitorio con la espera de la cabecera; alarmas 20 % y 80 %; al 100 % "
            "unavailable sin gastar ni construir el contexto",
        ) as run,
        assistant_stack(postgres_endpoint, "fs_laz_11") as stack,
    ):
        export = loop_metrics_export()
        transport = SequenceTransport(
            [
                Scripted(
                    status=429,
                    body={"type": "error", "error": {"type": "rate_limit_error", "message": "x"}},
                    headers={**_rate_headers(10), "retry-after": str(RETRY_AFTER)},
                ),
                _answer(90),
            ]
        ).install(monkeypatch)
        provider = AnthropicAssistantProvider(
            factory(), api_key=SYNTHETIC_KEY, metrics=export.metrics
        )
        sleep = ObservingSleep(stack, export)
        stack.worker = AssistantWorker(
            database=stack.base.database,
            provider=provider,
            free_text=both_policy(),
            clock=stack.clock,
            sleep=sleep,
            metrics=export.metrics,
        )
        who = stack.coordinator()
        organization = stack.organization().organization_id
        attribute = {"organization_id": str(organization)}

        # 1. El 429 es transitorio: se espera lo que dice su cabecera (30 s > 5 s) y se repite.
        first = stack.requested(who)
        assert stack.dispatch().count(Outcome.DELIVERED) == 1
        row = stack.request_row(first)
        assert (row["state"], row["attempt_count"]) == ("ready", 2)
        assert len(transport.requests) == 2  # sin reintentos del cliente del paquete
        assert stack.sleep.waits == [RETRY_AFTER]
        # El margen de la respuesta 429 (10 %) es el que ve la alarma durante la espera.
        (during_wait,) = sleep.headroom
        assert during_wait is not None and during_wait < HEADROOM_ALARM
        after = export.value(_HEADROOM)
        assert after is not None and after >= HEADROOM_ALARM
        below = export.value(_BUDGET, **attribute)
        assert below is not None and below <= BUDGET_ALARM

        # 2. Al 80 % del tope (una ficha por encima) salta la alarma y la solicitud sigue.
        stack.add_tokens(
            organization,
            input_tokens=0,
            output_tokens=int(MONTHLY_OUTPUT_TOKENS * BUDGET_ALARM)
            - _used(stack, organization)
            + 1,
        )
        second = stack.requested(who)
        assert stack.dispatch().count(Outcome.DELIVERED) == 1
        assert stack.request_row(second)["state"] == "ready"
        alarming = export.value(_BUDGET, **attribute)
        assert alarming is not None and BUDGET_ALARM < alarming < 1
        calls = len(transport.requests)

        # 3. Al 100 %: unavailable con budget_exceeded, sin fichas, sin contexto y sin llamada.
        pending = stack.requested(who)  # admitida antes de llegar al tope
        stack.add_tokens(
            organization,
            input_tokens=0,
            output_tokens=MONTHLY_OUTPUT_TOKENS - _used(stack, organization),
        )
        built: list[Any] = []
        real = build_context

        def counting(*args: Any, **kwargs: Any) -> Any:
            built.append(args)
            return real(*args, **kwargs)

        monkeypatch.setattr(consumer_module, "build_context", counting)
        rounds = 3 if nightly else 1
        # ``add_tokens`` anota el consumo previo en la solicitud más reciente (esta): no debe subir.
        spent = {k: stack.request_row(pending)[k] for k in ("input_tokens", "output_tokens")}
        assert stack.dispatch().count(Outcome.DELIVERED) == 1
        row = stack.request_row(pending)
        assert (row["state"], row["unavailable_cause"]) == ("unavailable", "budget_exceeded")
        assert row["attempt_count"] == 0
        assert {k: row[k] for k in spent} == spent
        for _ in range(rounds):
            before = len(stack.requests_of(organization))
            refused = stack.post(who, stack.analysis(who))
            assert refused.status_code == 503, refused.text
            assert refused.json()["detail_code"] == "loop_assistant_unavailable"
            assert len(stack.requests_of(organization)) == before
        assert built == [] and len(transport.requests) == calls
        exhausted = export.value(_BUDGET, **attribute)
        assert exhausted is not None and exhausted >= 1
        assert stack.circuit()[0] == "closed"  # el presupuesto no es una caída del proveedor
        run.observe(
            retry_after_s=RETRY_AFTER,
            waits=list(stack.sleep.waits),
            headroom_during_wait=during_wait,
            headroom_after=after,
            budget_ratio_before=below,
            budget_ratio_alarm=alarming,
            budget_ratio_exhausted=exhausted,
            provider_requests=len(transport.requests),
            contexts_built_at_cap=len(built),
            refused_at_cap=refused.status_code,
            exhausted_state=[row["state"], row["unavailable_cause"]],
        )
