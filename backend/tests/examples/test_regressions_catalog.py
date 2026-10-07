"""La forma de ``tests/examples/regressions/`` (NFR-NUC-48, NFR-GOB-62, PBT-10).

Cada módulo de la carpeta es un contraejemplo reducido de una propiedad: se llama
``test_pr_nuc_NN_*.py`` (U-02) o ``test_pr_gob_NN_*.py`` (U-03, TASK-229) y su docstring nombra la
propiedad (``PR-NUC-NN`` o ``PR-GOB-NN``) y cómo se reproduce (semilla o ``@reproduce_failure``).
Sin módulos todavía, la comprobación pasa; con uno mal formado, lo nombra.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Final

REGRESSIONS: Final = Path(__file__).resolve().parent / "regressions"
_NAME: Final = re.compile(r"test_pr_(nuc|gob)_(\d{2})_[a-z0-9_]+\.py")
_REPRODUCTION: Final = re.compile(r"semilla|seed|reproduce_failure", re.IGNORECASE)


def problems(directory: Path) -> list[str]:
    """``archivo: motivo`` por cada módulo de ``directory`` que no sigue la convención."""
    found: list[str] = []
    for path in sorted(directory.glob("*.py")):
        if path.name == "__init__.py":
            continue
        match = _NAME.fullmatch(path.name)
        if match is None:
            found.append(f"{path.name}: el nombre no es test_pr_(nuc|gob)_NN_<qué>.py")
            continue
        unit, number = match.groups()
        prop = f"PR-{unit.upper()}-{number}"
        docstring = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
        if prop not in docstring:
            found.append(f"{path.name}: el docstring no nombra {prop}")
        if not _REPRODUCTION.search(docstring):
            found.append(f"{path.name}: el docstring no dice cómo se reproduce (semilla)")
    return found


def test_every_regression_names_its_property_and_its_reproduction() -> None:
    assert REGRESSIONS.is_dir()
    assert (REGRESSIONS / "__init__.py").is_file()
    assert problems(REGRESSIONS) == []


def test_a_malformed_regression_is_named(tmp_path: Path) -> None:
    (tmp_path / "__init__.py").write_text('"""x"""\n', encoding="utf-8")
    (tmp_path / "test_fallo_suelto.py").write_text('"""Sin propiedad."""\n', encoding="utf-8")
    (tmp_path / "test_pr_nuc_13_cadena.py").write_text(
        '"""PR-NUC-31: otra propiedad."""\n', encoding="utf-8"
    )
    (tmp_path / "test_pr_nuc_25_particion.py").write_text(
        '"""PR-NUC-25: partición; semilla 20260929."""\n', encoding="utf-8"
    )
    assert problems(tmp_path) == [
        "test_fallo_suelto.py: el nombre no es test_pr_(nuc|gob)_NN_<qué>.py",
        "test_pr_nuc_13_cadena.py: el docstring no nombra PR-NUC-13",
        "test_pr_nuc_13_cadena.py: el docstring no dice cómo se reproduce (semilla)",
    ]


def test_a_malformed_u03_regression_is_named(tmp_path: Path) -> None:
    (tmp_path / "test_pr_gob_12_aislamiento.py").write_text(
        '"""PR-NUC-12: la propiedad de otra unidad; semilla 20261007."""\n', encoding="utf-8"
    )
    (tmp_path / "test_pr_gob_20_concesion.py").write_text(
        '"""PR-GOB-20: concesión de un solo PUT."""\n', encoding="utf-8"
    )
    (tmp_path / "test_pr_gob_29_limite.py").write_text(
        '"""PR-GOB-29: ráfaga; @reproduce_failure del contraejemplo."""\n', encoding="utf-8"
    )
    (tmp_path / "test_pr_gob_7_corto.py").write_text('"""PR-GOB-07."""\n', encoding="utf-8")
    assert problems(tmp_path) == [
        "test_pr_gob_12_aislamiento.py: el docstring no nombra PR-GOB-12",
        "test_pr_gob_20_concesion.py: el docstring no dice cómo se reproduce (semilla)",
        "test_pr_gob_7_corto.py: el nombre no es test_pr_(nuc|gob)_NN_<qué>.py",
    ]
