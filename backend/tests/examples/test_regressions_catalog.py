"""La forma de ``tests/examples/regressions/`` (NFR-NUC-48, PBT-10).

Cada módulo de la carpeta es un contraejemplo reducido de una propiedad: se llama
``test_pr_nuc_NN_*.py`` y su docstring nombra la propiedad (``PR-NUC-NN``) y cómo se reproduce
(semilla o ``@reproduce_failure``). Sin módulos todavía, la comprobación pasa; con uno mal formado,
lo nombra.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Final

REGRESSIONS: Final = Path(__file__).resolve().parent / "regressions"
_NAME: Final = re.compile(r"test_pr_nuc_(\d{2})_[a-z0-9_]+\.py")
_REPRODUCTION: Final = re.compile(r"semilla|seed|reproduce_failure", re.IGNORECASE)


def problems(directory: Path) -> list[str]:
    """``archivo: motivo`` por cada módulo de ``directory`` que no sigue la convención."""
    found: list[str] = []
    for path in sorted(directory.glob("*.py")):
        if path.name == "__init__.py":
            continue
        match = _NAME.fullmatch(path.name)
        if match is None:
            found.append(f"{path.name}: el nombre no es test_pr_nuc_NN_<qué>.py")
            continue
        docstring = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
        if f"PR-NUC-{match.group(1)}" not in docstring:
            found.append(f"{path.name}: el docstring no nombra PR-NUC-{match.group(1)}")
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
        "test_fallo_suelto.py: el nombre no es test_pr_nuc_NN_<qué>.py",
        "test_pr_nuc_13_cadena.py: el docstring no nombra PR-NUC-13",
        "test_pr_nuc_13_cadena.py: el docstring no dice cómo se reproduce (semilla)",
    ]
