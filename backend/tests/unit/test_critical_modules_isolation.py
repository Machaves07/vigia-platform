"""Los módulos críticos se importan sin FastAPI ni SQLAlchemy (NFR-NUC-25).

Lanza ``tools/check_isolated_imports.py`` en un subproceso (intérprete nuevo, sin módulos
cargados) y comprueba también que el bloqueo detecta una importación prohibida, directa o
transitiva: sin esa contraprueba, la prueba podría pasar sin demostrar nada.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

BACKEND = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND / "tools" / "check_isolated_imports.py"
CRITICAL_MODULES = (
    "vigia_platform.identity.auth",
    "vigia_platform.identity.authz",
    "vigia_platform.ledger.chain",
    "vigia_platform.shared.signing",
    "vigia_platform.shared.crypto",
)


def _run(*args: str, cwd: Path = BACKEND) -> tuple[int, dict[str, Any]]:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode, json.loads(completed.stdout)


def test_critical_modules_import_without_fastapi_or_sqlalchemy() -> None:
    code, result = _run()
    assert result["modules"] == list(CRITICAL_MODULES)
    # Los submódulos también se importan bajo el bloqueo (LC-NUC-01, TASK-122).
    assert "vigia_platform.identity.auth.passwords" in result["imported"]
    assert result["block_works"] is True
    assert result["failures"] == {}
    assert result["leaked"] == []
    assert code == 0


@pytest.mark.parametrize("package", ["fastapi", "sqlalchemy"])
def test_blocked_package_is_installed_but_unimportable_under_the_block(package: str) -> None:
    """El entorno sí tiene el paquete: el bloqueo, no su ausencia, es lo que se prueba."""
    assert (
        subprocess.run(
            [sys.executable, "-c", f"import {package}"], cwd=BACKEND, check=False
        ).returncode
        == 0
    )
    code, result = _run("--modules", package)
    assert code == 1
    assert package in result["failures"]
    assert "bloqueado" in result["failures"][package]


def test_transitive_import_of_sqlalchemy_is_detected(tmp_path: Path) -> None:
    """Un módulo que importa SQLAlchemy dentro de una función ejecutada al cargar falla."""
    (tmp_path / "leaky_module.py").write_text(
        "def _load():\n    import sqlalchemy  # noqa: F401\n\n\n_load()\n", encoding="utf-8"
    )
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--modules", "leaky_module"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    result = json.loads(completed.stdout)
    assert completed.returncode == 1
    assert "leaky_module" in result["failures"]
    assert "sqlalchemy" in result["failures"]["leaky_module"]


def test_submodule_of_a_critical_package_is_imported_and_checked(tmp_path: Path) -> None:
    """Importar el paquete no basta: un submódulo que importa FastAPI también se detecta."""
    package = tmp_path / "critical_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "clean.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package / "leaky.py").write_text("import fastapi  # noqa: F401\n", encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--modules", "critical_pkg"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    result = json.loads(completed.stdout)
    assert completed.returncode == 1
    assert result["imported"] == ["critical_pkg", "critical_pkg.clean"]
    assert list(result["failures"]) == ["critical_pkg.leaky"]
    assert "fastapi" in result["failures"]["critical_pkg.leaky"]
