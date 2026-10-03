"""Resumen de los escenarios de resiliencia (LC-NUC-34): una línea por escenario con su semilla.

Cada escenario imprime además su semilla al empezar (``harness.scenario``) y deja su informe JSON
en ``VIGIA_RESILIENCE_REPORT_DIR``; esta línea lo hace visible también con ``-q`` y sin ``-s``.
"""

from __future__ import annotations

import pytest

from tests.resilience.harness import finished_scenarios


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    records = finished_scenarios()
    if not records:
        return
    terminalreporter.section("escenarios de resiliencia (PAT-NUC-RES-06)")
    for record in records:
        terminalreporter.write_line(
            f"{record.scenario_id} {record.outcome} semilla={record.seed} "
            f"{record.duration_seconds:.1f} s informe={record.report_path}"
        )
