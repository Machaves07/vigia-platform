"""El arnés sin contenedores (LC-NUC-34): semilla, informe por escenario y resultado.

- Cada escenario imprime su semilla (la de la sesión, la que repite ``--hypothesis-seed``) y deja
  un informe JSON con la semilla, la inyección, lo esperado, lo observado y el resultado.
- Un escenario que falla deja el informe con ``failed`` y el error, y el fallo sale tal cual.
- El generador del escenario solo depende de la semilla y del identificador.
- Solo admite identificadores ``FS-NUC-nn`` (con letra de variante) o ``NFR-NUC-nn``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.resilience.harness import REPORT_DIR_VARIABLE, scenario, session_seed


def _report(directory: Path, scenario_id: str) -> dict[str, object]:
    (path,) = directory.glob(f"{scenario_id}-*.json")
    loaded: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def test_a_passing_scenario_prints_its_seed_and_leaves_its_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(REPORT_DIR_VARIABLE, str(tmp_path))
    with scenario("FS-NUC-99", title="t", injection="i", expected="e") as run:
        run.observe(valor=1, lista=[1, 2])
    seed = session_seed()
    assert f"FS-NUC-99: semilla {seed} (reproducir: --hypothesis-seed={seed})" in (
        capsys.readouterr().out
    )
    report = _report(tmp_path, "FS-NUC-99")
    assert report["outcome"] == "passed" and report["failure"] is None
    assert report["seed"] == str(seed)
    assert (report["injection"], report["expected"]) == ("i", "e")
    assert report["observed"] == {"valor": 1, "lista": [1, 2]}


def test_a_failing_scenario_reports_failed_and_reraises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REPORT_DIR_VARIABLE, str(tmp_path))
    with (
        pytest.raises(AssertionError, match="no se cumplió"),
        scenario("FS-NUC-98a", title="t", injection="i", expected="e"),
    ):
        raise AssertionError("no se cumplió")
    report = _report(tmp_path, "FS-NUC-98a")
    assert report["outcome"] == "failed"
    assert "no se cumplió" in str(report["failure"])


def test_the_scenario_random_depends_only_on_the_seed_and_the_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REPORT_DIR_VARIABLE, str(tmp_path))
    draws = []
    for scenario_id in ("FS-NUC-97", "FS-NUC-97", "FS-NUC-96"):
        with scenario(scenario_id, title="t", injection="i", expected="e") as run:
            draws.append([run.random.random(), run.child().random()])
    assert draws[0] == draws[1]
    assert draws[0] != draws[2]


@pytest.mark.parametrize("scenario_id", ["FS-01", "fs-nuc-01", "FS-NUC-1", "FS-NUC-01/../x", ""])
def test_only_closed_scenario_ids_are_accepted(scenario_id: str) -> None:
    with (
        pytest.raises(ValueError, match="identificador"),
        scenario(scenario_id, title="t", injection="i", expected="e"),
    ):
        pass
