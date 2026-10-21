"""H-29: el borrador cita evidencia real, sigue siendo borrador y no entra al expediente.

Reubicación (sin duplicar) del caso citado de test_loop_assistant_flow. Proveedor simulado,
consumidor y API reales; comparación de hojas exactas, no de subcadenas de UUID (VIG-194).
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.loop_assistant_support import assistant_stack
from vigia_platform.loop.adapters.assistant.fake import FAKE_MODEL, FAKE_MODEL_VERSION, Script
from vigia_platform.loop.assistant.context import canonical_json, context_digest

pytestmark = [pytest.mark.integration, pytest.mark.laz_examples]


def _leaves(value: Any) -> Iterator[Any]:
    if isinstance(value, dict):
        for item in value.values():
            yield from _leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _leaves(item)
    else:
        yield value


def test_laz_h29_the_draft_cites_evidence_outside_the_ledger(
    postgres_endpoint: PostgresEndpoint,
) -> None:
    with assistant_stack(postgres_endpoint, "laz_h29") as stack:
        coordinator = stack.coordinator("a")
        provider = stack.provider(Script.VALID_WITH_CITATIONS)
        stack.use(provider)
        request_id = stack.requested(coordinator)
        before = stack.fetch("SELECT count(*) AS n FROM ledger.ledger_record")
        stack.deliver(request_id)
        assert provider.call_count == 1
        row = stack.request_row(request_id)
        assert (row["state"], row["unavailable_cause"], row["attempt_count"]) == ("ready", None, 1)
        assert (row["input_tokens"], row["output_tokens"], row["cache_read_tokens"]) == (
            1200,
            300,
            0,
        )
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
        assert (citation["id"], citation["kind"]) == (context.citable[0].alias, "record")
        analysis = stack.base.analysis_row(row["analysis_id"])
        assert citation["reference_id"] == str(stack.anchor_of(analysis["subject_id"]))
        sent = list(_leaves(json.loads(canonical_json(context))))
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
        assert context_digest(context) == row["context_digest"]
        assert stack.fetch("SELECT count(*) AS n FROM ledger.ledger_record") == before
        response = stack.get(coordinator, request_id)
        assert response.status_code == 200
        body = response.json()
        assert body["assistant_state"] == "ready"
        assert body["draft"]["is_draft"] is True
        assert body["draft"]["model"] == FAKE_MODEL
        assert body["draft"]["citations"][0]["reference_id"] == citation["reference_id"]
