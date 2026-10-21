"""Catálogo bloqueante de SEG-09 y NFR-LAZ-48/50: suites completas en cada cambio.

El reparto real de la colección se verifica además con tools/check_test_split.py --collect.
No se altera ci.yml: ya ejecuta toda la colección, con y sin integración.
"""

import ast
import shlex
from pathlib import Path

import yaml

BACKEND = Path(__file__).resolve().parents[2]
EXAMPLES = (
    "h25_closure_with_evidence",
    "h26_unclosable_with_policy",
    "h27_escalation",
    "h29_draft_cites_evidence",
    "h30_text_rejected",
    "h37_inspector_package",
    "h39_sample_does_not_modify",
    "h41_exposure_not_computable",
    "hanging_sheet_false_positive",
)


def test_laz_the_twelve_scenarios_and_nine_marked_examples_exist() -> None:
    abuse = BACKEND / "tests" / "abuse"
    assert sorted(path.name.split("_")[2] for path in abuse.glob("test_laz_l[01][0-9]_*.py")) == [
        f"l{number:02d}" for number in range(1, 13)
    ]
    for example in EXAMPLES:
        path = BACKEND / "tests" / "examples" / f"test_laz_{example}.py"
        tree = ast.parse(path.read_text("utf-8"))
        marks = [
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "pytestmark"
                for target in node.targets
            )
        ]
        assert len(marks) == 1, path
        assert any(
            isinstance(node, ast.Attribute) and node.attr == "laz_examples"
            for node in ast.walk(marks[0].value)
        ), path
        assert any(
            isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
            for node in tree.body
        ), path


def test_laz_ci_collects_the_corpora_without_path_or_name_exclusions() -> None:
    workflow = yaml.safe_load(
        (BACKEND.parent / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    )
    for job, selection in (
        ("backend-pruebas", "not integration"),
        ("backend-integracion-grupos", "integration"),
    ):
        commands = [
            shlex.split(step["run"])
            for step in workflow["jobs"][job]["steps"]
            if "run" in step and "-m pytest" in step["run"]
        ]
        assert len(commands) == 1
        command = commands[0]
        assert "--hypothesis-profile=$PERFIL" in command
        assert [command[i + 1] for i, word in enumerate(command) if word == "-m"] == [
            "pytest",
            selection,
        ]
        assert "-k" not in command
        assert {word for word in command if word.startswith("--ignore")} <= {"--ignore=tests/load"}
        assert not [word for word in command if word.startswith("tests/")]
    assert all(
        (BACKEND / "tests" / "abuse" / name).is_file()
        for name in (
            "test_laz_r5_corpus_validators.py",
            "test_laz_r5_corpus_assistant.py",
            "test_laz_hostile_packages.py",
        )
    )
