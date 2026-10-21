"""FS-LAZ-02: correo caído y degradación visible sin perder avisos."""

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
def test_email_down_for_five_minutes(
    postgres_endpoint: PostgresEndpoint, tmp_path: Path, nightly: bool
) -> None:
    with scenario(
        "FS-LAZ-02",
        title="Correo caído",
        injection="FakeEmailSender caído durante 300 s",
        expected="degraded_app_only visible, integridad encolada y recuperación",
    ) as run:
        result = tmp_path / "email.json"
        with process_group(tmp_path) as group:
            child = group.start_laz_case(
                "email",
                {
                    "case": "email",
                    "endpoint": asdict(postgres_endpoint),
                    "result": str(result),
                },
            )
            assert child.process.wait(120) == 0, child.tail(100)
        observed = json.loads(result.read_text())
        run.observe(**observed)
        assert observed["notifications"] == 1
        assert observed["email_status"] == "degraded_app_only"
        assert observed["integrity_queued"] > 0
        assert observed["circuit_after_recovery"] == "closed"
