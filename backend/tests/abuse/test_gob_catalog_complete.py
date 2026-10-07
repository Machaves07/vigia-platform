"""Los quince escenarios de abuso de U-03 están todos en ``tests/abuse/`` (PAT-GOB-SEG-07).

``business-rules.md`` §12 enumera G-1 a G-15; PAT-GOB-SEG-07 los convierte en quince casos
bloqueantes y pide que «la canalización falle si falta uno». ``SCENARIOS`` es ese catálogo: por
escenario, su archivo ``test_gNN_<qué>.py`` y las reglas que su docstring debe nombrar (las de la
columna «Reglas» de §12, más las que TASK-229 añade).

- ``missing`` nombra cada escenario sin archivo; ``undocumented`` cada archivo cuyo docstring no
  nombra sus reglas o que no tiene pruebas;
- las sondas copian el catálogo a un directorio temporal, quitan un archivo (o vacían un
  docstring) y comprueban que la comprobación **nombra** el escenario.

Corre sin base: la canalización la ejecuta también en el trabajo sin integración.
"""

from __future__ import annotations

import ast
import shutil
from pathlib import Path
from typing import Final

ABUSE: Final = Path(__file__).resolve().parent
SCENARIOS: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    "G-1": ("test_g01_revoked_node.py", ("BR-GOB-66", "BR-GOB-88", "BR-GOB-96")),
    "G-2": (
        "test_g02_reused_enrollment_code.py",
        ("BR-GOB-58", "BR-GOB-59", "BR-GOB-60", "BR-GOB-61"),
    ),
    "G-3": (
        "test_g03_tampered_catalog_or_gate.py",
        ("BR-GOB-06", "BR-GOB-07", "BR-GOB-08", "BR-GOB-92"),
    ),
    "G-4": ("test_g04_finding_without_agreement.py", ("BR-GOB-30", "BR-GOB-92", "BR-GOB-96")),
    "G-5": ("test_g05_ingest_flood.py", ("BR-GOB-62", "BR-GOB-86", "BR-GOB-96")),
    "G-6": ("test_g06_productivity_family.py", ("BR-GOB-13", "BR-GOB-17", "BR-GOB-18")),
    "G-7": ("test_g07_false_negative_record.py", ("BR-GOB-38", "BR-GOB-39", "BR-GOB-43")),
    "G-8": ("test_g08_agreement_from_other_zone.py", ("BR-GOB-29", "BR-GOB-31")),
    "G-9": ("test_g09_provider_under_concession.py", ("BR-GOB-27", "BR-NUC-37", "BR-NUC-09")),
    "G-10": (
        "test_g10_threshold_hiding_regression.py",
        ("BR-GOB-03", "BR-GOB-51", "BR-GOB-56"),
    ),
    "G-11": ("test_g11_simulated_adapter_productive.py", ("BR-GOB-78",)),
    "G-12": ("test_g12_foreign_node_revocation.py", ("BR-GOB-57", "BR-GOB-66", "PR-GOB-12")),
    "G-13": ("test_g13_orphan_clip_storage.py", ("BR-GOB-93", "BR-GOB-94")),
    "G-14": ("test_g14_unexpected_signatory.py", ("BR-GOB-25", "BR-GOB-26", "BR-GOB-27")),
    "G-15": (
        "test_g15_stopwatch_correction.py",
        ("BR-GOB-44", "BR-GOB-45", "BR-GOB-46", "BR-GOB-47"),
    ),
}
"""Escenario → (archivo, reglas que su docstring nombra)."""


def missing(directory: Path) -> list[str]:
    """``G-n (archivo)`` de cada escenario sin su archivo en ``directory``."""
    return [
        f"{scenario} ({name})"
        for scenario, (name, _) in SCENARIOS.items()
        if not (directory / name).is_file()
    ]


def undocumented(directory: Path) -> list[str]:
    """``G-n: motivo`` de cada archivo presente que no nombra sus reglas o no tiene pruebas."""
    found: list[str] = []
    for scenario, (name, rules) in SCENARIOS.items():
        path = directory / name
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstring = ast.get_docstring(tree) or ""
        if not docstring.startswith(f"{scenario} "):
            found.append(f"{scenario}: el docstring no empieza por «{scenario} »")
        absent = [rule for rule in rules if rule not in docstring]
        if absent:
            found.append(f"{scenario}: el docstring no nombra {', '.join(absent)}")
        tests = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
        ]
        if not tests:
            found.append(f"{scenario}: sin pruebas")
    return found


def test_every_scenario_g1_to_g15_has_its_file_and_names_its_rules() -> None:
    assert list(SCENARIOS) == [f"G-{n}" for n in range(1, 16)]
    absent = missing(ABUSE)
    assert not absent, f"escenarios de abuso sin archivo: {absent}"
    problems = undocumented(ABUSE)
    assert not problems, f"escenarios de abuso sin sus reglas: {problems}"


def _copy(target: Path) -> Path:
    for name, _ in SCENARIOS.values():
        shutil.copy(ABUSE / name, target / name)
    return target


def test_a_missing_scenario_file_is_named(tmp_path: Path) -> None:
    directory = _copy(tmp_path)
    (directory / SCENARIOS["G-7"][0]).unlink()
    (directory / SCENARIOS["G-13"][0]).unlink()
    assert missing(directory) == [
        "G-7 (test_g07_false_negative_record.py)",
        "G-13 (test_g13_orphan_clip_storage.py)",
    ]


def test_a_scenario_that_does_not_name_its_rules_is_named(tmp_path: Path) -> None:
    directory = _copy(tmp_path)
    (directory / SCENARIOS["G-5"][0]).write_text(
        '"""G-5 · Inundación, sin reglas."""\n\n\ndef test_algo() -> None:\n    pass\n',
        encoding="utf-8",
    )
    (directory / SCENARIOS["G-11"][0]).write_text('"""G-11 · BR-GOB-78."""\n', encoding="utf-8")
    assert undocumented(directory) == [
        "G-5: el docstring no nombra BR-GOB-62, BR-GOB-86, BR-GOB-96",
        "G-11: sin pruebas",
    ]
