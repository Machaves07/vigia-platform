"""``tools/check_links.py``: enlaces relativos de la documentación (NFR-NUC-50, TASK-152).

Bordes: destino ausente, mayúsculas distintas, salida del repositorio, enlace absoluto, ancla
ausente, encabezados repetidos (``-1``), anclas HTML, anclas de línea en archivos que no son
Markdown, enlaces dentro de código (no cuentan), enlaces con esquema (no son relativos),
destinos con ``<…>`` y con ``%20``, definiciones de referencia y códigos de salida. Solo
archivos sintéticos en ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tools.check_links import (
    anchors_of,
    check_files,
    extract_links,
    github_slug,
    main,
    markdown_files,
)


def _write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _reasons(root: Path, text: str, *, name: str = "docs/a.md") -> list[str]:
    source = _write(root, name, text)
    _, broken = check_files([source], root)
    return [item.reason for item in broken]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _write(tmp_path, "README.md", "# Vigía\n\n## Arranque local\n")
    _write(
        tmp_path,
        "docs/runbooks/6.1-restauracion.md",
        "# 6.1 Restauración de prueba\n\n## Pasos\n\n## Validación\n\n## Pasos\n"
        '\n<a id="ancla-propia"></a>\n',
    )
    _write(tmp_path, "backend/tools/check_links.py", "print()\n")
    return tmp_path


@pytest.mark.parametrize(
    ("heading", "slug"),
    [
        ("6.1 Restauración de prueba", "61-restauración-de-prueba"),
        ("Variables de configuración y orden", "variables-de-configuración-y-orden"),
        ("`vigia-admin` y `rotate-node-ca`", "vigia-admin-y-rotate-node-ca"),
        ("Runbook · Traslado de la pila", "runbook--traslado-de-la-pila"),
        ("[Enlace](x.md) en título", "enlace-en-título"),
        ("snake_case <b>y</b> HTML", "snake_case-y-html"),
        ("", ""),
    ],
)  # fmt: skip
def test_github_slug(heading: str, slug: str) -> None:
    assert github_slug(heading) == slug


def test_repeated_headings_get_numbered_suffixes() -> None:
    anchors = anchors_of("# A\n## Pasos\n## Pasos\n## Pasos\n")
    assert {"a", "pasos", "pasos-1", "pasos-2"} <= anchors
    assert "pasos-3" not in anchors


def test_code_spans_in_headings_keep_their_text_in_the_anchor() -> None:
    anchors = anchors_of("## Cómo lanzar `vigia-admin` y `vigia-migrate` en un despliegue\n")
    assert anchors == {"cómo-lanzar-vigia-admin-y-vigia-migrate-en-un-despliegue"}


def test_headings_inside_code_fences_are_not_anchors() -> None:
    assert anchors_of("```text\n# no es encabezado\n```\n# Sí\n") == {"sí"}


@pytest.mark.parametrize(
    "target",
    [
        "runbooks/6.1-restauracion.md",
        "runbooks/6.1-restauracion.md#pasos",
        "runbooks/6.1-restauracion.md#pasos-1",
        "runbooks/6.1-restauracion.md#validación",
        "runbooks/6.1-restauracion.md#ancla-propia",
        "runbooks/6.1-restauracion.md#validaci%C3%B3n",
        "../README.md#arranque-local",
        "runbooks/",
        "runbooks",
        "../backend/tools/check_links.py",
        "../backend/tools/check_links.py#L1",
        "../backend/tools/check_links.py#L1-L2",
        "#título-propio",
    ],
)
def test_resolving_links_pass(repo: Path, target: str) -> None:
    assert _reasons(repo, f"# Título propio\n\nVer [aquí]({target}).\n") == []


@pytest.mark.parametrize(
    ("target", "reason"),
    [
        ("runbooks/6.9-no-existe.md", "no existe"),
        ("runbooks/6.1-Restauracion.md", "las mayúsculas no coinciden"),
        ("Runbooks/6.1-restauracion.md", "las mayúsculas no coinciden"),
        ("runbooks/6.1-restauracion.md#pasos-2", "no existe el ancla #pasos-2"),
        ("runbooks/6.1-restauracion.md#Pasos", "no existe el ancla #Pasos"),
        ("#no-hay", "no existe el ancla #no-hay"),
        ("../../fuera.md", "sale del repositorio"),
        ("/docs/runbooks/6.1-restauracion.md", "enlace absoluto: usa una ruta relativa"),
        ("runbooks/#pasos", "ancla sobre un directorio"),
        ("../backend/tools/check_links.py#main", "ancla en un archivo no Markdown"),
    ],
)
def test_broken_links_are_reported(repo: Path, target: str, reason: str) -> None:
    assert _reasons(repo, f"# T\n\nVer [aquí]({target}).\n") == [reason]


def test_external_links_and_code_are_not_checked(repo: Path) -> None:
    text = (
        "# T\n\n"
        "[web](https://example.com/x.md) [correo](mailto:a@example.com) [p](//host/x)\n"
        "`[en código](no-existe.md)` y ``[doble](tampoco.md)``\n"
        "```markdown\n[cercado](no-existe.md)\n```\n"
        "~~~~\n[tilde](no-existe.md)\n```\n[sigue cercado](no-existe.md)\n~~~~\n"
    )
    assert _reasons(repo, text) == []


def test_indented_list_continuations_are_checked(repo: Path) -> None:
    assert _reasons(repo, "# T\n\n1. Paso\n\n    Ver [roto](no-existe.md).\n") == ["no existe"]


def test_links_wrapped_over_lines_are_checked(repo: Path) -> None:
    # Sonda de la revisión del PR #61: GitHub renderiza el enlace aunque su texto ocupe dos líneas.
    text = (
        "# T\n\nPárrafo que sigue\n"
        "en [texto\nsegunda línea](./inexistente.md) y\n"
        "[otro](runbooks/).\n"
    )
    source = _write(repo, "docs/a.md", text)
    _, broken = check_files([source], repo)
    # Se informa en la línea donde empieza el ``[``, la segunda del párrafo.
    assert [(item.link.line, item.link.target, item.reason) for item in broken] == [
        (4, "./inexistente.md", "no existe")
    ]


def test_paragraphs_do_not_join_across_headings_blank_lines_or_fences(repo: Path) -> None:
    text = "# T [a\ntexto](no-1.md)\n\n[b\n\ntexto](no-2.md)\n[c\n```\n```\ntexto](no-3.md)\n"
    assert _reasons(repo, text) == []


def test_angle_brackets_titles_images_and_references(repo: Path) -> None:
    _write(repo, "docs/con espacio.md", "# X\n")
    text = (
        "# T\n\n"
        '[a](<con espacio.md>) [b](con%20espacio.md "título") ![img](runbooks/no.png)\n'
        "[ref]: runbooks/falta.md\n"
        "[ok]: <con espacio.md#x>\n"
    )
    source = _write(repo, "docs/a.md", text)
    checked, broken = check_files([source], repo)
    assert checked == 5
    assert [(item.link.target, item.reason) for item in broken] == [
        ("runbooks/no.png", "no existe"),
        ("runbooks/falta.md", "no existe"),
    ]


def test_extract_links_reports_line_numbers(tmp_path: Path) -> None:
    links = extract_links(tmp_path / "a.md", "x\n\n[uno](a.md) [dos](b.md)\n")
    assert [(link.line, link.target) for link in links] == [(3, "a.md"), (3, "b.md")]


def test_main_exit_codes(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(repo)
    assert main(["README.md", "docs/"]) == 0
    _write(repo, "docs/roto.md", "[x](nada.md)\n")
    assert main(["README.md", "docs/"]) == 1
    assert "docs/roto.md:1: nada.md (no existe)" in capsys.readouterr().out
    assert main(["no-existe/"]) == 2
    assert main(["backend/tools/check_links.py"]) == 2


REPOSITORY = Path(__file__).resolve().parents[3]


def test_repository_documentation_links_resolve() -> None:
    # El Test Plan de TASK-152 dentro de la suite: un enlace roto en README.md, CHANGELOG.md o
    # docs/ hace fallar el CI, no solo la corrida a mano.
    files = markdown_files(["README.md", "CHANGELOG.md", "docs"], REPOSITORY)
    assert any(path.name == "6.1-quarterly-restore-drill.md" for path in files)
    checked, broken = check_files(files, REPOSITORY)
    assert checked > 0
    assert [item.render(REPOSITORY) for item in broken] == []


@given(st.text(alphabet=st.characters(codec="utf-8"), max_size=300))
def test_arbitrary_markdown_never_crashes(text: str) -> None:
    # Ninguna entrada hace fallar la extracción ni el cálculo de anclas.
    extract_links(Path("a.md"), text)
    anchors_of(text)
