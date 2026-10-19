"""NFR-LAZ-48 y BR-LAZ-75 · el corpus R5 entero contra la **salida del asistente simulado**
(TASK-316; L-4 y L-5; PR-LAZ-20).

**Qué intenta** (R5): que el asistente proponga una causa que culpa a una persona, que la
identifique (nombre, documento, correo, teléfono, placa) o que obedezca una instrucción
incrustada en el texto del estándar, y que eso llegue a una pantalla.

**Qué lo detiene**: el camino del consumidor ``assistant_worker`` sobre la respuesta del proveedor:
``completion_failure`` (motivo de parada antes del contenido) y ``assess_output`` (esquema
estricto, citas contra la lista cerrada de alias y **los dos validadores** de U-04 sobre el
borrador y sobre cada hecho citado). Cada caso del corpus (``r5_corpus/*.yaml``, el mismo de
TASK-340) se entrega como respuesta de ``FakeAssistantProvider``, en el **borrador** y en un
**hecho citado**: los casos ``reject`` nunca dan un borrador aceptado; los ``accept`` (los límites
que un falso positivo cruzaría) sí, con su vocabulario declarado.

Se comparan veredictos y razones de rechazo, nunca subcadenas (VIG-194). Solo datos generados.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta
from typing import Any, Final

import pytest

from tests.abuse.test_laz_r5_corpus_validators import MIN_REJECTED, Case, corpus
from tests.attribution_support import both_policy
from vigia_platform.loop.adapters.assistant.fake import FakeAssistantProvider
from vigia_platform.loop.assistant.consumer import (
    Accepted,
    Rejected,
    RejectionReason,
    assess_output,
)
from vigia_platform.loop.assistant.context import BuiltContext, build_context
from vigia_platform.loop.assistant.port import AssistantCompletion, completion_failure

ORGANIZATION: Final = uuid.UUID("00000000-0000-4000-8000-000000000316")
ZONE_CONTEXT: Final[dict[str, Any]] = {
    "family": "coexistence",
    "tier": "tier_1",
    "standard_text": "Ninguna persona dentro del perímetro con la máquina energizada.",
    "observability_state_at_event": "observable",
    "period": {"from": "2026-09-19T05:00:00.000Z", "to": "2026-10-19T05:00:00.000Z"},
    "coverage_summary_period": {
        "observable_ms": 1000,
        "degraded_ms": 0,
        "not_observable_ms": 0,
        "no_communication_ms": 0,
        "never_reported_ms": 0,
        "zone_not_active_ms": 0,
    },
    "node_state": [{"communication_state": "reachable", "last_heartbeat_at": None}],
    "signals_by_role": [{"role": "energy", "value": "asserted", "valid": True}],
    "episode_durations": {"episode_ms": 5000, "condition_ms": 4000},
    "finding_counts_by_family_in_period": [{"family": "coexistence", "count": 3}],
    "recurrences_in_period": 2,
    "previous_actions": [],
    "previous_cause_categories": [],
}
NEUTRAL: Final = "Revisión del resguardo de la prensa durante el turno de mantenimiento."
VALIDATOR_REASONS: Final = frozenset(
    {
        RejectionReason.BLOCKED_VOCABULARY_TERM,
        RejectionReason.PERSON_ATTRIBUTION_DETECTED,
        RejectionReason.FREE_TEXT_POLICY,
    }
)
_EXPECTED_REASON: Final = {
    "blocked_vocabulary_term": RejectionReason.BLOCKED_VOCABULARY_TERM,
    "person_attribution_detected": RejectionReason.PERSON_ATTRIBUTION_DETECTED,
    "invisible_character": RejectionReason.FREE_TEXT_POLICY,
    "markup": RejectionReason.FREE_TEXT_POLICY,
}


def _built() -> BuiltContext:
    return build_context(ZONE_CONTEXT, request_id=uuid.uuid4(), anchor_record_id=uuid.UUID(int=316))


async def _no_wait(seconds: float) -> None:
    """El temporizador inyectado del doble: la latencia simulada no espera en la pared."""


def _answer(built: BuiltContext, document: dict[str, Any]) -> AssistantCompletion:
    """La respuesta del doble con ``document`` como contenido."""
    provider = FakeAssistantProvider(
        content=json.dumps(document, ensure_ascii=False), sleep=_no_wait
    )
    outcome = asyncio.run(provider.complete(built.context, timeout=timedelta(seconds=60)))
    assert isinstance(outcome, AssistantCompletion)
    assert completion_failure(outcome) is None  # end_turn: se lee el contenido
    return outcome


def _documents(built: BuiltContext, text: str) -> dict[str, dict[str, Any]]:
    first = built.context.citable[0]
    return {
        "draft": {
            "draft_text": text,
            "citations": [],
            "insufficient_evidence_declared": True,
            "proposed_cause_category": "process",
        },
        "cited_fact": {
            "draft_text": NEUTRAL,
            "citations": [{"kind": first.kind.value, "id": first.alias, "quoted_fact_es": text}],
            "insufficient_evidence_declared": False,
            "proposed_cause_category": "process",
        },
    }


def test_the_whole_corpus_runs_against_the_assistant() -> None:
    rejected = [case for case in corpus() if case.expected == "reject"]
    assert len(rejected) >= MIN_REJECTED


@pytest.mark.parametrize("case", corpus(), ids=[case.id for case in corpus()])
def test_every_corpus_case_gets_its_verdict_on_the_assistant_output(case: Case) -> None:
    policy = both_policy(case.declared)
    built = _built()
    for where, document in _documents(built, case.text).items():
        if len(case.text) > 500 and where == "cited_fact":  # pragma: no cover - ninguno lo es
            continue
        completion = _answer(built, document)
        verdict = assess_output(completion.content_json, built, policy, ORGANIZATION)
        if case.expected == "accept":
            assert isinstance(verdict, Accepted), (case.id, where, verdict)
            continue
        assert isinstance(verdict, Rejected), (case.id, where)
        assert verdict.reason in VALIDATOR_REASONS, (case.id, where, verdict)
        assert case.code is not None
        assert verdict.reason is _EXPECTED_REASON[case.code], (case.id, where, verdict)
