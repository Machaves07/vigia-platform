"""FS-LAZ-01: proveedor caído en un proceso con API y consumidor reales."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.resilience.harness import scenario
from tests.resilience.laz_support import PROFILES
from tests.resilience.processes import process_group

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("nightly", PROFILES)
def test_provider_down_for_three_minutes(
    postgres_endpoint: PostgresEndpoint, tmp_path: Path, nightly: bool
) -> None:
    with scenario(
        "FS-LAZ-01",
        title="Proveedor caído",
        injection="Rechazo durante 180 s",
        expected="Circuito abierto, firma guiada, cero cola muerta; sonda cada 60 s",
    ) as run:
        result = tmp_path / "assistant.json"
        with process_group(tmp_path) as group:
            child = group.start_laz_case(
                "assistant",
                {
                    "case": "assistant",
                    "endpoint": asdict(postgres_endpoint),
                    "result": str(result),
                },
            )
            assert child.process.wait(120) == 0, child.tail(100)
        observed = json.loads(result.read_text())
        run.observe(**observed)
        assert observed["dead_letters"] == 0
        assert observed["signature_status"] == 201
        assert observed["circuit_states"] == ["open", "open", "open", "closed"]
        assert observed["probes"] == [False, True, True, True]
        assert observed["probe_calls"] == 3
        assert observed["request_after_recovery"] == "ready"
