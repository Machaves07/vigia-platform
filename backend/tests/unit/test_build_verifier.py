"""``tools/build_verifier.py``: el verificador de un archivo está al día y solo usa la biblioteca
estándar (LC-NUC-18, PAT-NUC-MAN-08, NFR-NUC-53).

- ``--check`` termina en 0 sobre el árbol versionado y en 1 si ``vigia_verify.py`` se editó a mano.
- La construcción falla si un módulo importa algo que no es biblioteca estándar, importa un módulo
  posterior, usa alias o importaciones relativas, o define un nombre que ya existe en otro módulo.
- El archivo generado solo importa la biblioteca estándar de Python 3.10 y no importa
  ``vigia_platform``.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tools import build_verifier
from tools.build_verifier import MODULES, OUTPUT, SOURCE_DIRECTORY, BuildError, build, main

BACKEND = Path(__file__).resolve().parents[2]


def test_check_passes_on_the_committed_file() -> None:
    completed = subprocess.run(
        [sys.executable, str(BACKEND / "tools" / "build_verifier.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
        cwd=BACKEND,
    )
    assert completed.returncode == 0, completed.stderr
    assert "al día" in completed.stdout


def test_committed_file_is_the_generated_one() -> None:
    assert OUTPUT.read_bytes() == build().encode("utf-8")


@pytest.mark.parametrize(
    "edit",
    [
        lambda text: text.replace("return False", "return True", 1),
        lambda text: text + "\n# comentario añadido a mano\n",
        lambda text: text.replace("\n", "\r\n"),
        lambda text: text[:-1],
    ],
    ids=["logic", "comment", "crlf", "trailing-newline"],
)
def test_check_fails_after_a_hand_edit(
    tmp_path: Path, edit: object, capsys: pytest.CaptureFixture[str]
) -> None:
    assert callable(edit)
    edited = tmp_path / "vigia_verify.py"
    edited.write_bytes(edit(OUTPUT.read_text(encoding="utf-8")).encode("utf-8"))
    assert main(["--check", "--output", str(edited)]) == 1
    assert "no coincide con lo generado" in capsys.readouterr().err


def test_check_fails_when_the_file_is_missing(tmp_path: Path) -> None:
    assert main(["--check", "--output", str(tmp_path / "vigia_verify.py")]) == 1


def test_build_writes_the_file(tmp_path: Path) -> None:
    target = tmp_path / "vigia_verify.py"
    assert main(["--output", str(target)]) == 0
    assert target.read_bytes() == build().encode("utf-8")
    assert b"\r\n" not in target.read_bytes()


def test_generated_file_imports_only_the_standard_library() -> None:
    tree = ast.parse(OUTPUT.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            modules.add(node.module or "")
    assert modules, "el archivo generado debería importar algo de la biblioteca estándar"
    assert not any(module.startswith("vigia_platform") for module in modules)
    assert {module.split(".")[0] for module in modules} <= set(sys.stdlib_module_names)


def _copy_sources(tmp_path: Path) -> Path:
    directory = tmp_path / "chain"
    shutil.copytree(SOURCE_DIRECTORY, directory)
    return directory


@pytest.mark.parametrize(
    ("module", "line", "message"),
    [
        ("pure_rfc8785", "import pydantic\n", "no es biblioteca estándar"),
        ("pure_rfc8785", "from cryptography import x509\n", "no es biblioteca estándar"),
        (
            "pure_rfc8785",
            "from vigia_platform.ledger.chain.chain_walk import genesis_hash\n",
            "que no va antes",
        ),
        (
            "chain_walk",
            "from vigia_platform.ledger.chain.pure_rfc8785 import canonicalize as c\n",
            "con alias",
        ),
        ("chain_walk", "from vigia_platform.ledger.chain.pure_rfc8785 import *\n", "con alias"),
        ("chain_walk", "from . import pure_rfc8785\n", "relativa"),
        ("chain_walk", "def canonicalize(value):\n    return value\n", "ya está definido"),
        ("package_verifier", "MAX_SAFE_INTEGER = 1\n", "ya está definido"),
    ],
)
def test_build_rejects_modules_that_break_the_rules(
    tmp_path: Path, module: str, line: str, message: str
) -> None:
    directory = _copy_sources(tmp_path)
    path = directory / f"{module}.py"
    path.write_text(path.read_text(encoding="utf-8") + "\n" + line, encoding="utf-8")
    with pytest.raises(BuildError, match=message):
        build(directory)


def test_build_reports_rule_violations_as_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _copy_sources(tmp_path)
    path = directory / "chain_walk.py"
    path.write_text(path.read_text(encoding="utf-8") + "\nimport numpy\n", encoding="utf-8")
    monkeypatch.setattr(build_verifier, "SOURCE_DIRECTORY", directory)
    monkeypatch.setattr(build_verifier.build, "__defaults__", (directory, MODULES))
    assert main(["--check"]) == 1
    assert "numpy" in capsys.readouterr().err
