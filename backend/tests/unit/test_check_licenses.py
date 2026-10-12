"""``tools/check_licenses.py``: lista permitida, excepciones y alcance de CI (TASK-143).

Bordes: una alternativa permitida basta (``OR``), todas las de ``AND`` se exigen, los
clasificadores se exigen todos, una licencia desconocida o sin declarar falla, una excepción
vencida falla (el día de la revisión todavía vale) y una excepción de herramienta de CI falla si
el paquete llega al cierre de ejecución de ``uv.lock``. Solo datos sintéticos; la fecha se fija
con ``--today``/``today``.
"""

from __future__ import annotations

import importlib.metadata
from datetime import date, timedelta
from email.message import Message
from pathlib import Path

import pytest

from tools.check_licenses import (
    LOCK_FILE,
    CheckError,
    Dependency,
    LicenseException,
    check,
    declared_license,
    license_leaves,
    load_exceptions,
    main,
    runtime_closure,
)

LOCK = LOCK_FILE

TODAY = date(2026, 10, 2)


def _exception(
    package: str, license_name: str, *, review: date = TODAY, scope: str = ""
) -> LicenseException:
    return LicenseException("EX-01", package, license_name, review, scope)


@pytest.mark.parametrize(
    ("text", "leaves"),
    [
        ("MIT", [["MIT"]]),
        ("Apache-2.0 OR BSD-3-Clause", [["Apache-2.0"], ["BSD-3-Clause"]]),
        ("MIT AND PSF-2.0", [["MIT", "PSF-2.0"]]),
        ("BSD License; Apache Software License", [["BSD", "Apache-2.0"]]),
        ("(MIT OR GPL-3.0) AND Zlib", [["MIT", "Zlib"], ["GPL-3.0", "Zlib"]]),
        ("GPL-2.0 WITH Classpath-exception-2.0", [["GPL-2.0 WITH Classpath-exception-2.0"]]),
        ("(MIT", [["(MIT"]]),
        ("", [[""]]),
    ],
)
def test_license_expressions(text: str, leaves: list[list[str]]) -> None:
    assert license_leaves(text) == leaves


@pytest.mark.parametrize(
    ("license_name", "ok"),
    [
        ("MIT", True),
        ("Apache-2.0 OR GPL-3.0", True),
        ("MIT AND GPL-3.0", False),
        ("LGPL-2.1", False),
        ("BSD License; Other/Proprietary License", False),
        ("", False),
        ("UNKNOWN", False),
    ],
)
def test_allowed_and_rejected_licenses(license_name: str, ok: bool) -> None:
    problems, _ = check([Dependency("pkg", "1.0", license_name)], [], set(), TODAY)
    assert (problems == []) is ok


def test_own_packages_are_not_checked() -> None:
    dependencies = [Dependency("vigia_platform", "0.1", ""), Dependency("Vigia.Contracts", "1", "")]
    assert check(dependencies, [], set(), TODAY) == ([], [])


def test_an_exception_covers_its_package_and_license_until_its_review_date() -> None:
    dependency = Dependency("qrcode", "8.2", "BSD License; Other/Proprietary License")
    exception = _exception("qrcode", "LicenseRef-Proprietary")
    problems, applied = check([dependency], [exception], set(), TODAY)
    assert problems == [] and len(applied) == 1
    problems, _ = check([dependency], [exception], set(), date(2026, 10, 3))
    assert problems and "venció" in problems[0]
    other = Dependency("other", "1", "Other/Proprietary License")
    assert check([other], [exception], set(), TODAY)[0]


def test_a_ci_tool_exception_fails_once_the_package_reaches_the_image() -> None:
    dependency = Dependency("numpy", "2.5.3", "BSD-3-Clause AND 0BSD")
    exception = _exception("numpy", "0BSD", scope="herramienta de CI (grupo dev)")
    assert check([dependency], [exception], set(), TODAY)[0] == []
    problems, _ = check([dependency], [exception], {"numpy"}, TODAY)
    assert problems and "herramienta de CI" in problems[0]


def test_declared_license_prefers_the_expression_then_the_classifiers() -> None:
    message = Message()
    message["License"] = "BSD"
    message["Classifier"] = "License :: OSI Approved :: MIT License"
    assert declared_license(message) == "MIT License"
    message["License-Expression"] = "Apache-2.0"
    assert declared_license(message) == "Apache-2.0"
    full_text = Message()
    full_text["License"] = "Copyright (c) 2020\nPermission is hereby granted"
    assert declared_license(full_text) == ""


_LOCK = """
version = 1
[[package]]
name = "vigia-platform"
dependencies = [{ name = "sqlalchemy", extra = ["asyncio"] }, { name = "fastapi" }]
[package.dev-dependencies]
dev = [{ name = "numpy" }]
[[package]]
name = "sqlalchemy"
dependencies = [{ name = "typing-extensions" }]
[package.optional-dependencies]
asyncio = [{ name = "greenlet" }]
[[package]]
name = "fastapi"
dependencies = [{ name = "starlette" }]
[[package]]
name = "starlette"
[[package]]
name = "greenlet"
[[package]]
name = "typing-extensions"
[[package]]
name = "numpy"
"""


def test_runtime_closure_follows_extras_and_skips_groups() -> None:
    assert runtime_closure(_LOCK) == {
        "vigia-platform",
        "sqlalchemy",
        "greenlet",
        "typing-extensions",
        "fastapi",
        "starlette",
    }
    with pytest.raises(CheckError):
        runtime_closure('[[package]]\nname = "x"\n')
    with pytest.raises(CheckError):
        runtime_closure("[[")


def test_the_exceptions_registry(tmp_path: Path) -> None:
    registry = tmp_path / "LICENSE-EXCEPTIONS.md"
    registry.write_text(
        "| Id | Paquete | Licencia | Revisión | Alcance | Motivo |\n|---|---|---|---|---|---|\n"
        "| EX-07 | Numpy | 0BSD, Zlib | 2027-03-31 | herramienta de CI | motivo |\n"
        "texto libre | sin fila |\n"
    )
    loaded = load_exceptions(registry)
    assert [(e.identifier, e.package, e.license, e.ci_tool_only) for e in loaded] == [
        ("EX-07", "numpy", "0BSD", True),
        ("EX-07", "numpy", "Zlib", True),
    ]
    for bad in (
        "| EX-08 | x | MIT | 31/03/2027 |",
        "| EX-09 | | MIT | 2027-03-31 |",
        "| EX-10 | x |",
    ):
        registry.write_text(bad + "\n")
        with pytest.raises(CheckError):
            load_exceptions(registry)
    with pytest.raises(CheckError):
        load_exceptions(tmp_path / "missing.md")


def test_the_repository_registry_is_well_formed() -> None:
    loaded = load_exceptions(REGISTRY)
    assert {e.package for e in loaded} == {"qrcode", "cffi", "numpy", "pillow", "pyphen"}
    assert {e.package for e in loaded if e.ci_tool_only} == {"numpy"}


# --- Árbol de WeasyPrint (TASK-217; A-53, R-GOB-13) ---------------------------------------------

REGISTRY = Path(__file__).resolve().parents[2] / "LICENSE-EXCEPTIONS.md"
WEASYPRINT_TREE = (
    "weasyprint",
    "pydyf",
    "tinycss2",
    "tinyhtml5",
    "cssselect2",
    "fonttools",
    "brotli",
    "zopfli",
    "webencodings",
    "pillow",
    "pyphen",
    "jinja2",
    "markupsafe",
    "cffi",
)
"""Lo que añade el documento del acta (con Jinja2) y lo que ya había (``cffi``, ``markupsafe``)."""


def _installed(names: tuple[str, ...]) -> list[Dependency]:
    found = []
    for name in names:
        distribution = importlib.metadata.distribution(name)
        found.append(
            Dependency(name, distribution.version, declared_license(distribution.metadata))
        )
    return found


def _registry_without(tmp_path: Path, identifier: str) -> Path:
    lines = REGISTRY.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [line for line in lines if not line.startswith(f"| {identifier} |")]
    assert len(kept) == len(lines) - 1
    trimmed = tmp_path / "LICENSE-EXCEPTIONS.md"
    trimmed.write_text("".join(kept), encoding="utf-8")
    return trimmed


def test_the_weasyprint_tree_reaches_the_image() -> None:
    runtime = runtime_closure(LOCK.read_text(encoding="utf-8"))
    assert set(WEASYPRINT_TREE) <= runtime


def test_pillow_and_pyphen_pass_only_through_their_registry_rows() -> None:
    tree = _installed(WEASYPRINT_TREE)
    runtime = runtime_closure(LOCK.read_text(encoding="utf-8"))
    # Lo que declaran sus metadatos, tal cual (lo que copian EX-04 y EX-05).
    declared = {d.name: d.license for d in tree}
    assert declared["pillow"] == "MIT-CMU"
    assert declared["pyphen"] == (
        "GNU General Public License v2 or later (GPLv2+); "
        "GNU Lesser General Public License v2 or later (LGPLv2+); "
        "Mozilla Public License 1.1 (MPL 1.1)"
    )

    problems, applied = check(tree, load_exceptions(REGISTRY), runtime, TODAY)
    assert problems == []
    assert {line.split(" ")[0] for line in applied} == {"pillow", "pyphen", "cffi"}
    assert all(" EX-04 " in line for line in applied if line.startswith("pillow"))
    assert all(" EX-05 " in line for line in applied if line.startswith("pyphen"))

    # Sin ninguna excepción solo fallan Pillow y pyphen (y cffi, EX-02): el resto del árbol pasa
    # por la lista permitida de NFR-CTR-26.
    problems, _ = check(tree, [], runtime, TODAY)
    assert sorted(line.split(" ")[0] for line in problems) == ["cffi", "pillow", "pyphen"]


@pytest.mark.parametrize(("identifier", "package"), [("EX-04", "pillow"), ("EX-05", "pyphen")])
def test_removing_either_row_fails_the_check(tmp_path: Path, identifier: str, package: str) -> None:
    trimmed = _registry_without(tmp_path, identifier)
    problems, _ = check(
        _installed(WEASYPRINT_TREE),
        load_exceptions(trimmed),
        runtime_closure(LOCK.read_text(encoding="utf-8")),
        TODAY,
    )
    assert [line.split(" ")[0] for line in problems] == [package]
    assert "licencia no permitida" in problems[0]

    # La orden completa también falla (código 1) con ese registro.
    assert main(["--exceptions", str(trimmed), "--lock", str(LOCK)]) == 1


def test_the_command_passes_with_the_repository_registry() -> None:
    assert main(["--exceptions", str(REGISTRY), "--lock", str(LOCK)]) == 0


@pytest.mark.parametrize("package", ["pillow", "pyphen"])
def test_their_exceptions_are_runtime_and_expire(package: str) -> None:
    rows = [e for e in load_exceptions(REGISTRY) if e.package == package]
    assert rows and all(not e.ci_tool_only and e.scope == "ejecución" for e in rows)
    review = rows[0].review
    tree = _installed((package,))
    runtime = runtime_closure(LOCK.read_text(encoding="utf-8"))
    assert check(tree, rows, runtime, review)[0] == []  # el día de la revisión todavía vale
    expired, _ = check(tree, rows, runtime, review + timedelta(days=1))
    assert expired and all("venció" in line for line in expired)
