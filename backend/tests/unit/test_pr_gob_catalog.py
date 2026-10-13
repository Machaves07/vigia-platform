"""Metaprueba del catálogo de propiedades de U-03 (NFR-GOB-60; TASK-234).

Criterio de aceptación: falla si alguna de las propiedades ``PR-GOB-01`` a ``PR-GOB-32`` no tiene
al menos una prueba en ``tests/properties/gob/`` que cite su identificador, o si una de las diez
«con estado» (04, 08, 14, 15, 16, 20, 22, 23, 28 y 32) no es una ``RuleBasedStateMachine``. Además,
``C-PLA-16`` (``LC-GOB-17``, ``fleet.versions``) figura como «sin propiedades» con su justificación,
porque el diseño no le asigna ninguna propiedad y se cubre con ejemplos (H-48).

La prueba lee el árbol real; los negativos usan directorios sintéticos.
"""

from __future__ import annotations

import re
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[3]
GOB = REPOSITORY / "backend" / "tests" / "properties" / "gob"
WITHOUT_PROPERTIES_DECLARATION = (
    REPOSITORY / "backend" / "tests" / "unit" / "fleet" / "test_fleet_versions.py"
)

PR_GOB = tuple(range(1, 33))
"""Las treinta y dos propiedades: las veinte del diseño funcional más las doce de PAT §6."""

STATEFUL = (4, 8, 14, 15, 16, 20, 22, 23, 28, 32)
"""Las diez que se verifican con ``RuleBasedStateMachine`` (tech-stack-decisions §2.5 y §6)."""

WITHOUT_PROPERTIES = "C-PLA-16"
"""``LC-GOB-17`` no tiene propiedades propias: se declara «sin propiedades» con justificación."""


def _python_sources(directory: Path) -> dict[str, str]:
    return {
        str(path): path.read_text(encoding="utf-8")
        for path in sorted(directory.glob("*.py"))
        if path.name != "__init__.py"
    }


def _cites(text: str, number: int) -> bool:
    return re.search(rf"PR-GOB-0*{number}\b", text) is not None


def missing_properties(directory: Path, numbers: tuple[int, ...] = PR_GOB) -> list[int]:
    """Los números de propiedad sin ninguna prueba que cite su identificador."""
    sources = _python_sources(directory)
    return [
        number for number in numbers if not any(_cites(text, number) for text in sources.values())
    ]


def stateful_without_state_machine(
    directory: Path, numbers: tuple[int, ...] = STATEFUL
) -> list[int]:
    """Las propiedades «con estado» cuya prueba que las cita no es una ``RuleBasedStateMachine``."""
    sources = _python_sources(directory)
    result: list[int] = []
    for number in numbers:
        citing = [text for text in sources.values() if _cites(text, number)]
        if not any("RuleBasedStateMachine" in text for text in citing):
            result.append(number)
    return result


def declaration_problems(path: Path) -> list[str]:
    """``C-PLA-16`` debe estar declarado «sin propiedades» con su justificación."""
    if not path.is_file():
        return [f"no existe {path}"]
    text = path.read_text(encoding="utf-8")
    problems = []
    if WITHOUT_PROPERTIES not in text:
        problems.append(f"{path.name}: no menciona {WITHOUT_PROPERTIES}")
    if "sin propiedades" not in text.lower():
        problems.append(f"{path.name}: no declara «sin propiedades»")
    if not re.search(r"PR-GOB-0*7\b", text):
        problems.append(
            f"{path.name}: la justificación no cita la propiedad que ya lo cubre (PR-GOB-07)"
        )
    return problems


def test_every_pr_gob_property_has_a_test_that_cites_it() -> None:
    assert missing_properties(GOB) == []


def test_every_stateful_pr_gob_is_a_rule_based_state_machine() -> None:
    assert stateful_without_state_machine(GOB) == []


def test_c_pla_16_is_declared_without_properties_with_its_justification() -> None:
    assert declaration_problems(WITHOUT_PROPERTIES_DECLARATION) == []


# --- Negativos: cada regla falla con un directorio sintético --------------------------------


def _write(directory: Path, name: str, text: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(text, encoding="utf-8")


def test_a_property_without_a_citing_test_is_missing(tmp_path: Path) -> None:
    _write(tmp_path, "test_one.py", "# PR-GOB-01\n")
    missing = missing_properties(tmp_path, numbers=(1, 2))
    assert missing == [2]


def test_a_stateful_property_without_the_state_machine_is_flagged(tmp_path: Path) -> None:
    _write(tmp_path, "test_one.py", "# PR-GOB-04\n\ndef test_something() -> None:\n    pass\n")
    assert stateful_without_state_machine(tmp_path, numbers=(4,)) == [4]
    _write(
        tmp_path,
        "test_stateful.py",
        "from hypothesis.stateful import RuleBasedStateMachine\n# PR-GOB-04\n",
    )
    assert stateful_without_state_machine(tmp_path, numbers=(4,)) == []


def test_a_stateful_property_in_another_file_does_not_count_for_the_state_machine(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "test_one.py", "# PR-GOB-08\n")
    _write(tmp_path, "test_two.py", "class M(RuleBasedStateMachine): ...\n# PR-GOB-09\n")
    assert stateful_without_state_machine(tmp_path, numbers=(8,)) == [8]


def test_a_missing_or_incomplete_declaration_is_flagged(tmp_path: Path) -> None:
    assert declaration_problems(tmp_path / "nope.py")
    without = tmp_path / "without.py"
    without.write_text("# C-PLA-16\n", encoding="utf-8")
    assert declaration_problems(without)
    complete = tmp_path / "complete.py"
    complete.write_text(
        '"""C-PLA-16 (LC-GOB-17): sin propiedades nuevas; lo cubre PR-GOB-07."""\n',
        encoding="utf-8",
    )
    assert declaration_problems(complete) == []
