"""FS-LAZ-07: proveedor lento, corte a 60 s, intentos propios a 5 y 20 s y tope duro de 120 s.

El despachador real de U-02 entrega ``assistant_requested`` al ``AssistantWorker`` real sobre
PostgreSQL 16 (``assistant_stack``); el ``FakeAssistantProvider`` responde a los 90 s y a los 150 s
desde la primera llamada. Todos los tiempos son del reloj inyectado (``AdvancingSleep``), nunca de
pared, y las cifras se leen del exportador en memoria de las métricas de U-04 (NFR-LAZ-35;
``tech-stack-decisions.md`` §2.4).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Final

import pytest

from tests.anthropic_support import SYNTHETIC_KEY, Built, factory
from tests.attribution_support import both_policy
from tests.integration.conftest import PostgresEndpoint
from tests.loop_assistant_support import AssistantStack, assistant_stack
from tests.resilience.harness import scenario
from tests.resilience.laz_support import PROFILES, LoopMetricsExport, loop_metrics_export
from vigia_platform.loop.adapters.anthropic.client import AnthropicAssistantProvider
from vigia_platform.loop.adapters.assistant.fake import FakeAssistantProvider, Script
from vigia_platform.loop.assistant.consumer import (
    ATTEMPT_TIMEOUT,
    HARD_DEADLINE,
    RETRY_BACKOFFS,
    AssistantWorker,
)
from vigia_platform.loop.observability.metrics import LoopMetricName
from vigia_platform.shared.outbox.dispatcher import Outcome

pytestmark = pytest.mark.integration

ATTEMPT: Final = 60
BACKOFFS: Final = (5, 20)
DEADLINE: Final = 120


def _worker(
    stack: AssistantStack, provider: FakeAssistantProvider, export: LoopMetricsExport
) -> None:
    stack.worker = AssistantWorker(
        database=stack.base.database,
        provider=provider,
        free_text=both_policy(),
        clock=stack.clock,
        sleep=stack.sleep,
        metrics=export.metrics,
    )


def _case(
    stack: AssistantStack, export: LoopMetricsExport, script: Script, **changes: Any
) -> tuple[dict[str, Any], FakeAssistantProvider, list[float], Any]:
    """Una solicitud nueva atendida por una ronda del despachador real."""
    stack.settle_deliveries()
    stack.sleep.waits.clear()
    provider = stack.provider(script, **changes)
    _worker(stack, provider, export)
    request_id = stack.requested(stack.coordinator())
    report = stack.dispatch()
    row = stack.request_row(request_id)
    return row, provider, list(stack.sleep.waits), (report, stack.delivery(request_id))


def _elapsed(row: dict[str, Any]) -> timedelta:
    elapsed: timedelta = row["resolved_at"] - row["requested_at"]
    return elapsed


@pytest.mark.parametrize("nightly", PROFILES)
def test_slow_provider_is_cut_and_never_passes_the_hard_deadline(
    postgres_endpoint: PostgresEndpoint, nightly: bool
) -> None:
    assert timedelta(seconds=ATTEMPT) == ATTEMPT_TIMEOUT
    assert tuple(b.total_seconds() for b in RETRY_BACKOFFS) == BACKOFFS
    assert timedelta(seconds=DEADLINE) == HARD_DEADLINE
    with (
        scenario(
            "FS-LAZ-07",
            title="Proveedor lento",
            injection="FakeAssistantProvider que responde a los 90 s y a los 150 s",
            expected="Corte a 60 s sin reintento del cliente; 5 y 20 s; timeout al pasar 120 s",
        ) as run,
        assistant_stack(postgres_endpoint, "fs_laz_07") as stack,
    ):
        # El adaptador real nunca reintenta por su cuenta: los reintentos son los propios.
        built = Built()
        AnthropicAssistantProvider(factory(), api_key=SYNTHETIC_KEY, constructor=built)
        (options,) = built.options
        assert options["max_retries"] == 0
        assert options["timeout"] == ATTEMPT
        export = loop_metrics_export()
        rounds = 3 if nightly else 1
        observed: dict[str, Any] = {"client_max_retries": 0, "client_timeout_s": ATTEMPT}
        for _ in range(rounds):
            # A los 90 s: el primer intento se corta a los 60 s; el segundo, tras 5 s, responde.
            row, provider, waits, (report, delivery) = _case(
                stack, export, Script.TIMEOUT, hang_until=timedelta(seconds=90)
            )
            assert report.count(Outcome.DELIVERED) == 1 and delivery["status"] == "delivered"
            assert (row["state"], row["attempt_count"]) == ("ready", 2)
            assert provider.calls[0].timeout == timedelta(seconds=ATTEMPT)
            assert waits[:2] == [ATTEMPT, BACKOFFS[0]] and abs(waits[2] - 25) < 0.001
            assert _elapsed(row) <= timedelta(seconds=DEADLINE)
            observed["slow_90"] = {
                "state": row["state"],
                "calls": provider.call_count,
                "waits": waits,
                "elapsed_ms": _elapsed(row) // timedelta(milliseconds=1),
            }
            # A los 150 s: dos intentos cortados; el retroceso de 20 s ya pasaría de los 120 s.
            row, provider, waits, (report, delivery) = _case(
                stack, export, Script.TIMEOUT, hang_until=timedelta(seconds=150)
            )
            assert report.count(Outcome.DEPENDENCY_DOWN) == 1
            # El fallo declarado abre el circuito sin consumir intentos ni ir a la cola muerta.
            assert delivery == {"status": "pending", "attempts": 0}
            assert (row["state"], row["unavailable_cause"], row["attempt_count"]) == (
                "unavailable",
                "timeout",
                2,
            )
            assert provider.call_count == row["attempt_count"]
            assert waits[:2] == [ATTEMPT, BACKOFFS[0]] and BACKOFFS[1] not in waits
            assert sum(waits) <= DEADLINE and _elapsed(row) <= timedelta(seconds=DEADLINE)
            assert stack.drafts(row["request_id"]) == []
            observed["slow_150"] = {
                "state": row["state"],
                "cause": row["unavailable_cause"],
                "calls": provider.call_count,
                "waits": waits,
                "elapsed_ms": _elapsed(row) // timedelta(milliseconds=1),
            }
            # Fallos rápidos: se ven los dos intentos propios completos, a 5 s y a 20 s.
            row, provider, waits, (report, _) = _case(stack, export, Script.OVERLOADED)
            assert report.count(Outcome.DEPENDENCY_DOWN) == 1
            assert provider.call_count == row["attempt_count"] == 3
            assert waits == [2, BACKOFFS[0], 2, BACKOFFS[1], 2]
            assert (row["state"], row["unavailable_cause"]) == ("unavailable", "provider_error")
            observed["fast_failures"] = {"calls": provider.call_count, "waits": waits}
        stack.settle_deliveries()
        states = {
            attributes["assistant_state"]: value
            for attributes, value in export.points(LoopMetricName.ASSISTANT_REQUESTS_TOTAL)
        }
        assert states == {"ready": rounds, "unavailable": 2 * rounds}
        durations = export.histogram(LoopMetricName.ASSISTANT_REQUEST_DURATION_MS)
        assert durations and all(peak <= DEADLINE * 1000 for _, _, peak in durations)
        observed["requests_total"] = states
        observed["max_duration_ms"] = max(peak for _, _, peak in durations)
        run.observe(**observed)
