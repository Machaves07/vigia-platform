"""``tools/check_coverage.py``: los umbrales de NFR-NUC-45 fallan la canalización (TASK-143).

Criterio de aceptación: la cobertura por debajo del umbral en ``identity`` o ``ledger`` falla la
canalización. El trabajo «cobertura» de ``ci.yml`` ejecuta esta herramienta sobre el informe
combinado (``tests/unit/test_workflows_pinned.py`` lo comprueba); aquí se fijan sus bordes con
informes sintéticos: justo en el umbral cumple, una línea por debajo falla, los adaptadores no
cuentan en el área, un área sin medir falla y un informe mal formado es un error.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tools.check_coverage import AREAS, GLOBAL_MINIMUM, CoverageError, evaluate, main


def _report(files: dict[str, tuple[int, int]]) -> dict[str, Any]:
    return {
        "meta": {"format": 3},
        "files": {
            path: {"summary": {"covered_lines": covered, "num_statements": statements}}
            for path, (covered, statements) in files.items()
        },
    }


def _healthy(**overrides: tuple[int, int]) -> dict[str, tuple[int, int]]:
    files = {
        "src/vigia_platform/identity/domain/roles.py": (90, 100),
        "src/vigia_platform/identity/auth/sessions.py": (900, 1000),
        "src/vigia_platform/identity/adapters/session_store.py": (0, 500),
        "src/vigia_platform/ledger/application/writer.py": (450, 500),
        "src/vigia_platform/ledger/chain/verify.py": (450, 500),
        "src/vigia_platform/ledger/adapters/http/routes.py": (0, 100),
        "src/vigia_platform/shared/db.py": (4000, 4000),
    }
    files.update(overrides)
    return files


def _run(tmp_path: Path, files: dict[str, tuple[int, int]]) -> int:
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(_report(files)))
    return main([str(path)])


def test_the_areas_are_identity_and_ledger_at_ninety_and_the_package_at_eighty() -> None:
    assert [(a.name.split()[0], a.minimum) for a in AREAS] == [
        ("identity", Decimal(90)),
        ("ledger", Decimal(90)),
    ]
    assert GLOBAL_MINIMUM.minimum == Decimal(80)
    assert all(area.exclude for area in AREAS)  # los adaptadores quedan fuera


def test_exactly_at_the_thresholds_passes(tmp_path: Path) -> None:
    results = evaluate(_report(_healthy()))
    assert [str(r.percent) for r in results[:2]] == ["90.00", "90.00"]
    assert all(r.passed for r in results)
    assert _run(tmp_path, _healthy()) == 0


@pytest.mark.parametrize(
    "override",
    [
        {"src/vigia_platform/identity/domain/roles.py": (89, 100)},
        {"src/vigia_platform/identity/auth/sessions.py": (899, 1000)},
        {"src/vigia_platform/ledger/application/writer.py": (449, 500)},
        {"src/vigia_platform/ledger/chain/verify.py": (449, 500)},
    ],
)
def test_one_line_below_ninety_in_identity_or_ledger_fails(
    tmp_path: Path, override: dict[str, tuple[int, int]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(tmp_path, _healthy(**override)) == 1
    assert "NO CUMPLE" in capsys.readouterr().out


def test_the_percentage_is_truncated_not_rounded_up(tmp_path: Path) -> None:
    # 89,999 % no se redondea a 90 %.
    # 89 099/99 000 + 900/1 000 = 89 999/100 000.
    files = _healthy(**{"src/vigia_platform/identity/domain/roles.py": (89_099, 99_000)})
    files["src/vigia_platform/identity/auth/sessions.py"] = (900, 1000)
    results = evaluate(_report(files))
    assert str(results[0].percent) == "89.99"
    assert not results[0].passed
    assert _run(tmp_path, files) == 1


def test_global_below_eighty_fails_even_with_healthy_areas(tmp_path: Path) -> None:
    files = _healthy(**{"src/vigia_platform/shared/db.py": (0, 4000)})
    assert all(r.passed for r in evaluate(_report(files))[:2])
    assert _run(tmp_path, files) == 1


def test_adapters_do_not_count_in_the_area() -> None:
    files = _healthy(**{"src/vigia_platform/identity/adapters/session_store.py": (500, 500)})
    assert evaluate(_report(files))[0].statements == 1100


def test_an_area_without_measured_files_fails(tmp_path: Path) -> None:
    files = {
        path: value
        for path, value in _healthy().items()
        if "/ledger/" not in path or "adapters" in path
    }
    results = evaluate(_report(files))
    assert results[1].statements == 0 and not results[1].passed
    assert _run(tmp_path, files) == 1


def test_a_lookalike_module_is_not_the_area() -> None:
    files = _healthy(**{"src/vigia_platform/identity_extra/x.py": (0, 1000)})
    assert evaluate(_report(files))[0].statements == 1100


@pytest.mark.parametrize(
    "report",
    [
        [],
        {},
        {"files": []},
        {"files": {"a.py": {}}},
        {"files": {"a.py": {"summary": {"covered_lines": 2, "num_statements": 1}}}},
        {"files": {"a.py": {"summary": {"covered_lines": -1, "num_statements": 1}}}},
        {"files": {"a.py": {"summary": {"covered_lines": True, "num_statements": 1}}}},
        {"files": {"a.py": {"summary": {"covered_lines": 1.5, "num_statements": 2}}}},
    ],
)
def test_a_malformed_report_is_an_error(tmp_path: Path, report: Any) -> None:
    with pytest.raises(CoverageError):
        evaluate(report)
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(report))
    assert main([str(path)]) == 2


def test_an_unreadable_report_is_an_error(tmp_path: Path) -> None:
    assert main([str(tmp_path / "missing.json")]) == 2
    broken = tmp_path / "broken.json"
    broken.write_text("{")
    assert main([str(broken)]) == 2
