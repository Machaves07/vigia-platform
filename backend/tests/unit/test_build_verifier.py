"""``tools/build_verifier.py``: el verificador de un archivo está al día y solo usa la biblioteca
estándar (LC-NUC-18, PAT-NUC-MAN-08, NFR-NUC-53).

- ``--check`` termina en 0 sobre el árbol versionado y en 1 si ``vigia_verify.py`` se editó a mano.
- La construcción falla si un módulo importa algo que no es biblioteca estándar, importa un módulo
  posterior, usa alias o importaciones relativas, o define un nombre que ya existe en otro módulo.
- El archivo generado solo importa la biblioteca estándar de Python 3.10 y no importa
  ``vigia_platform``.
- Composición de U-04 (TASK-346): el verificador del paquete exportado
  (``resources/verifier/vigia_verify.py``) es el mismo archivo con ``export_verifier`` detrás;
  está al día, ``--check`` falla si se edita a mano, solo importa la biblioteca estándar y solo
  conserva el ``if __name__ == "__main__"`` del último módulo. ``tools/vigia_verify.py`` no
  cambia por la composición, y las reglas valen también para el módulo de U-04.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tools import build_verifier
from tools.build_verifier import (
    ACCREDITATION_PACKAGE,
    BUNDLE_HEADER,
    BUNDLE_OUTPUT,
    MODULES,
    OUTPUT,
    SOURCE_DIRECTORY,
    BuildError,
    SourceModule,
    build,
    build_bundle,
    main,
)

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


# --- Composición de U-04 (TASK-346) --------------------------------------------------------------


def _imports(text: str) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            modules.add(node.module or "")
    return modules


def _main_guards(text: str) -> list[str]:
    return [
        ast.unparse(node)
        for node in ast.parse(text).body
        if isinstance(node, ast.If) and ast.unparse(node.test) == "__name__ == '__main__'"
    ]


def test_committed_bundle_is_the_generated_one_and_u02_is_unchanged() -> None:
    assert BUNDLE_OUTPUT.read_bytes() == build_bundle().encode("utf-8")
    # La composición no toca el verificador de U-02: sus bytes son los de build() sin extras.
    assert OUTPUT.read_bytes() == build().encode("utf-8")
    assert build_bundle() != build()


def test_bundle_imports_only_the_standard_library_and_keeps_one_entry_point() -> None:
    text = BUNDLE_OUTPUT.read_text(encoding="utf-8")
    modules = _imports(text)
    assert not any(module.startswith("vigia_platform") for module in modules)
    assert {module.split(".")[0] for module in modules} <= set(sys.stdlib_module_names)
    assert _main_guards(text) == ["if __name__ == '__main__':\n    raise SystemExit(export_main())"]
    assert _main_guards(OUTPUT.read_text(encoding="utf-8")) == [
        "if __name__ == '__main__':\n    raise SystemExit(main())"
    ]


def test_check_fails_after_a_hand_edit_of_the_bundle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    edited = tmp_path / "vigia_verify.py"
    edited.write_text(
        BUNDLE_OUTPUT.read_text(encoding="utf-8").replace(
            "MAX_EXPANSION_RATIO: Final = 100", "MAX_EXPANSION_RATIO: Final = 1000"
        ),
        encoding="utf-8",
    )
    assert main(["--check", "--bundle-output", str(edited)]) == 1
    assert "no coincide con lo generado" in capsys.readouterr().err


def _accreditation_copy(tmp_path: Path, line: str) -> SourceModule:
    directory = tmp_path / "accreditation"
    directory.mkdir()
    source = BACKEND / "src" / "vigia_platform" / "accreditation" / "export_verifier.py"
    target = directory / "export_verifier.py"
    target.write_text(source.read_text(encoding="utf-8") + "\n" + line, encoding="utf-8")
    return SourceModule(ACCREDITATION_PACKAGE, directory, "export_verifier")


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("from vigia_platform.loop.domain.enums import PackageForm\n", "que no va antes"),
        ("import pydantic\n", "no es biblioteca estándar"),
        ("def main():\n    return 0\n", "ya está definido"),
        ("MAX_SAFE_INTEGER = 1\n", "ya está definido"),
    ],
)
def test_the_rules_hold_for_the_u04_module(tmp_path: Path, line: str, message: str) -> None:
    with pytest.raises(BuildError, match=message):
        build(extra=[_accreditation_copy(tmp_path, line)], header=BUNDLE_HEADER)
