"""``vigia-contracts`` consumido por la etiqueta ``v1.1.0`` con su commit fijado (TASK-153).

NFR-NUC-24 (heredada de NFR-CTR-25) y ADR-004: el backend consume el contrato por ``git+ssh``
con ``#subdirectory=generated/python`` y **por etiqueta** ``vX.Y.Z``, nunca por rama ni por un
hash suelto; el ``uv.lock`` fija el hash del commit de esa etiqueta. Esta prueba corre en el
trabajo «backend (lint, tipos y pruebas sin integración)» de ``ci.yml``, sin red:

- ``pyproject.toml`` pide exactamente ``@v1.1.0`` del repositorio y subdirectorio de ADR-004;
- ``uv.lock`` resuelve esa misma etiqueta (``rev=v1.1.0``) al commit ``TAG_COMMIT`` (40
  hexadecimales), con la versión ``1.1.0`` del paquete, y su ``requires-dist`` coincide;
- el paquete instalado en el entorno es ese commit (``direct_url.json``, PEP 610).

``TAG_COMMIT`` es el commit al que apunta la etiqueta (ligera) en el remoto, leído con
``git ls-remote https://github.com/Machaves07/vigia-contracts refs/tags/v1.1.0*``
el 2026-10-07 (A-63).
Si una etiqueta nueva se adopta, se cambian aquí la etiqueta y su commit a la vez.

Los bordes (hash en lugar de etiqueta, rama, etiqueta móvil, ``rev`` del bloqueo distinto,
commit corto, otro repositorio o subdirectorio) se prueban sobre copias alteradas del texto real.
"""

from __future__ import annotations

import json
import re
import tomllib
from importlib.metadata import distribution
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

import pytest

BACKEND: Final = Path(__file__).resolve().parents[2]
PYPROJECT: Final = BACKEND / "pyproject.toml"
LOCK: Final = BACKEND / "uv.lock"

PACKAGE: Final = "vigia-contracts"
TAG: Final = "v1.1.0"
TAG_COMMIT: Final = "f4b8853a1b7863acee95f4135757afb6463ae565"
VERSION: Final = "1.1.0"
REPOSITORY: Final = "ssh://git@github.com/Machaves07/vigia-contracts.git"
SUBDIRECTORY: Final = "generated/python"

_REQUIREMENT: Final = re.compile(
    r"vigia-contracts @ git\+(?P<repository>ssh://git@github\.com/[A-Za-z0-9_.-]+/"
    r"[A-Za-z0-9_.-]+\.git)@(?P<ref>[^#@\s]+)#subdirectory=(?P<subdirectory>[A-Za-z0-9_./-]+)"
)
_RELEASE_TAG: Final = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_COMMIT: Final = re.compile(r"[0-9a-f]{40}")


class PinError(AssertionError):
    """El consumo del contrato no está fijado por etiqueta y commit."""


def _single(values: list[Any], what: str) -> Any:
    if len(values) != 1:
        raise PinError(f"se esperaba una sola entrada de {what}; hay {len(values)}")
    return values[0]


def requirement_ref(pyproject: dict[str, Any]) -> str:
    """La etiqueta de ``vigia-contracts`` en ``[project].dependencies``."""
    dependencies: list[str] = pyproject["project"]["dependencies"]
    requirement = _single(
        [d for d in dependencies if d.split("@")[0].strip() == PACKAGE], "vigia-contracts"
    )
    match = _REQUIREMENT.fullmatch(requirement)
    if match is None:
        raise PinError(f"forma no admitida (ADR-004): {requirement}")
    if match["repository"] != REPOSITORY or match["subdirectory"] != SUBDIRECTORY:
        raise PinError(f"repositorio o subdirectorio distintos de ADR-004: {requirement}")
    ref = match["ref"]
    if _RELEASE_TAG.fullmatch(ref) is None:
        raise PinError(f"la referencia «{ref}» no es una etiqueta vX.Y.Z (NFR-NUC-24)")
    return ref


def _git_source(url: str) -> tuple[str, str, str, str]:
    """``(repositorio, subdirectorio, rev, commit)`` de una fuente git de ``uv.lock``."""
    parts = urlsplit(url)
    query = {key: _single(values, key) for key, values in parse_qs(parts.query).items()}
    repository = f"{parts.scheme}://{parts.netloc}{parts.path}"
    return repository, query.get("subdirectory", ""), query.get("rev", ""), parts.fragment


def locked_commit(lock: dict[str, Any], ref: str) -> str:
    """El commit que ``uv.lock`` fija para la etiqueta ``ref``; ``PinError`` si no la fija."""
    packages: list[dict[str, Any]] = lock["package"]
    package = _single([p for p in packages if p["name"] == PACKAGE], "paquete vigia-contracts")
    repository, subdirectory, rev, commit = _git_source(package["source"]["git"])
    if repository != REPOSITORY or subdirectory != SUBDIRECTORY:
        raise PinError(f"uv.lock resuelve otro repositorio o subdirectorio: {repository}")
    if rev != ref:
        raise PinError(f"uv.lock resuelve «{rev}», no la etiqueta «{ref}» de pyproject.toml")
    if _COMMIT.fullmatch(commit) is None:
        raise PinError(f"uv.lock no fija un hash de commit completo: «{commit}»")
    platform = _single([p for p in packages if p["name"] == "vigia-platform"], "vigia-platform")
    declared = _single(
        [d for d in platform["metadata"]["requires-dist"] if d["name"] == PACKAGE],
        "requires-dist de vigia-contracts",
    )
    if _git_source(declared["git"])[2] != ref:
        raise PinError("requires-dist de uv.lock no coincide con pyproject.toml")
    if package["version"] != ref.removeprefix("v"):
        raise PinError(f"la versión bloqueada {package['version']} no es la de la etiqueta {ref}")
    return commit


def _texts() -> tuple[str, str]:
    return PYPROJECT.read_text(encoding="utf-8"), LOCK.read_text(encoding="utf-8")


def _check(pyproject_text: str, lock_text: str) -> str:
    ref = requirement_ref(tomllib.loads(pyproject_text))
    return locked_commit(tomllib.loads(lock_text), ref)


def test_pyproject_pins_the_release_tag() -> None:
    pyproject_text, _ = _texts()
    assert requirement_ref(tomllib.loads(pyproject_text)) == TAG


def test_lock_contains_the_tag_commit() -> None:
    pyproject_text, lock_text = _texts()
    assert _check(pyproject_text, lock_text) == TAG_COMMIT


def test_installed_contract_is_the_locked_commit() -> None:
    """El entorno en el que corre la suite tiene instalado el commit bloqueado."""
    installed = distribution(PACKAGE)
    assert installed.version == VERSION
    direct_url = installed.read_text("direct_url.json")
    assert direct_url is not None, "vigia-contracts no se instaló desde git"
    origin = json.loads(direct_url)
    assert origin["vcs_info"]["vcs"] == "git"
    assert origin["vcs_info"]["commit_id"] == TAG_COMMIT
    assert origin["subdirectory"] == SUBDIRECTORY


_REAL_REQUIREMENT: Final = f"git@github.com/Machaves07/vigia-contracts.git@{TAG}#"


@pytest.mark.parametrize(
    ("ref", "problem"),
    [
        (TAG_COMMIT, "no es una etiqueta"),  # el commit de desarrollo de antes de TASK-153
        ("main", "no es una etiqueta"),
        ("v1", "no es una etiqueta"),
        ("v1.0", "no es una etiqueta"),
        ("v1.0.0-rc1", "no es una etiqueta"),
        ("1.0.0", "no es una etiqueta"),
        ("v01.0.0", "no es una etiqueta"),
        ("latest", "no es una etiqueta"),
    ],
)
def test_requirement_rejects_anything_but_a_release_tag(ref: str, problem: str) -> None:
    pyproject_text, lock_text = _texts()
    altered = pyproject_text.replace(_REAL_REQUIREMENT, _REAL_REQUIREMENT.replace(TAG, ref))
    assert altered != pyproject_text
    with pytest.raises(PinError, match=problem):
        _check(altered, lock_text)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("Machaves07/vigia-contracts.git@", "otro/vigia-contracts.git@"),
        ("#subdirectory=generated/python", "#subdirectory=generated"),
        ("vigia-contracts @ git+ssh", "vigia-contracts @ git+https"),
        ("#subdirectory=generated/python", ""),
    ],
)
def test_requirement_rejects_other_sources(old: str, new: str) -> None:
    pyproject_text, lock_text = _texts()
    altered = pyproject_text.replace(old, new, 1)
    assert altered != pyproject_text
    with pytest.raises(PinError):
        _check(altered, lock_text)


@pytest.mark.parametrize(
    ("old", "new", "problem"),
    [
        (f"rev={TAG}#", "rev=v1.0.1#", "no la etiqueta"),
        (f"rev={TAG}#", f"rev={TAG_COMMIT}#", "no la etiqueta"),
        (f"rev={TAG}#", "#", "no la etiqueta"),
        (f'#{TAG_COMMIT}"', f'#{TAG_COMMIT[:12]}"', "hash de commit completo"),
        (f'#{TAG_COMMIT}"', '"', "hash de commit completo"),
        (f'#{TAG_COMMIT}"', f'#{TAG_COMMIT.upper()}"', "hash de commit completo"),
        (f'generated%2Fpython&rev={TAG}" }}', 'generated%2Fpython&rev=v1.0.1" }', "requires-dist"),
        (
            f'name = "vigia-contracts"\nversion = "{VERSION}"',
            'name = "vigia-contracts"\nversion = "1.0.1"',
            "versión",
        ),
        ("Machaves07/vigia-contracts.git?", "otro/vigia-contracts.git?", "otro repositorio"),
    ],
)
def test_lock_rejects_a_source_not_fixed_to_the_tag(old: str, new: str, problem: str) -> None:
    pyproject_text, lock_text = _texts()
    altered = lock_text.replace(old, new)
    assert altered != lock_text
    with pytest.raises(PinError, match=problem):
        _check(pyproject_text, altered)


def test_lock_with_two_contract_packages_is_rejected() -> None:
    pyproject_text, lock_text = _texts()
    duplicate = (
        '\n[[package]]\nname = "vigia-contracts"\nversion = "0.9.0"\nsource = { git = "x" }\n'
    )
    with pytest.raises(PinError, match="una sola entrada"):
        _check(pyproject_text, lock_text + duplicate)
