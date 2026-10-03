"""Lint de los flujos de GitHub Actions y de la imagen (TASK-143; NFR-NUC-24; SECURITY-10).

Criterio de aceptación: ninguna acción referenciada por etiqueta móvil. Además, lo que la
canalización promete y un cambio descuidado rompería sin que nadie lo viera:

- **Acciones fijadas por hash**: todo ``uses:`` de ``.github/workflows/*.yml`` es una acción local
  (``./…``), ``propietario/repo[/ruta]@<hash de 40>`` con su comentario de versión, o
  ``docker://imagen@sha256:<64>``. Una etiqueta (``@v4``), una rama (``@main``) o un hash corto no
  valen.
- **Permisos y disparadores**: permisos de nivel superior de solo lectura, ningún ``write`` en
  ningún trabajo, sin ``pull_request_target`` ni ``workflow_run``.
- **Descargas verificadas**: toda orden que descarga con ``curl`` comprueba la suma con
  ``sha256sum -c`` en el mismo paso.
- **ci.yml**: conserva los cuatro checks obligatorios y tiene los trabajos de cobertura (que
  ejecuta ``tools/check_coverage.py``), licencias, pip-audit, imagen arm64 con ``--ssh default``,
  auditoría de la imagen y arranque N-1.
- **Imágenes base por digest**: cada ``FROM`` de ``backend/Dockerfile`` es una etapa anterior o una
  imagen ``@sha256:``, nunca ``latest``; la de PostgreSQL de ``tools/run_boot_check.py`` es la
  de las pruebas de integración.
- **CODEOWNERS y Dependabot**: las rutas de A-26 y los ecosistemas de deployment-architecture §3.1.

Cada regla se prueba también en negativo con un flujo sintético que la incumple.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.integration.conftest import POSTGRES_IMAGE
from tools import run_boot_check

REPOSITORY = Path(__file__).resolve().parents[3]
WORKFLOWS = REPOSITORY / ".github" / "workflows"
DOCKERFILE = REPOSITORY / "backend" / "Dockerfile"
CODEOWNERS = REPOSITORY / ".github" / "CODEOWNERS"
DEPENDABOT = REPOSITORY / ".github" / "dependabot.yml"

REQUIRED_CHECKS = (
    "escaneo de secretos",
    "backend (lint, tipos y pruebas sin integración)",
    "backend (pruebas de integración)",
    "infra (pruebas y cdk synth)",
)
"""Los checks obligatorios de la rama (AGENTS.md): renombrarlos deja la rama sin protección."""

_PINNED = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_./-]+)?@[0-9a-f]{40}$")
_DOCKER_PINNED = re.compile(r"^docker://[^@\s]+@sha256:[0-9a-f]{64}$")
_VERSION_COMMENT = re.compile(r"#\s*v\d+(?:\.\d+)*\b")
_WRITE = re.compile(r"\bwrite\b|write-all")


# --- Comprobaciones (también se aplican a flujos sintéticos) -----------------------------------


def _load(text: str) -> dict[Any, Any]:
    document = yaml.safe_load(text)
    assert isinstance(document, dict)
    return document


def _triggers(workflow: dict[Any, Any]) -> set[str]:
    # YAML 1.1 lee la clave ``on`` como ``True``.
    raw = workflow.get("on", workflow.get(True))
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, list):
        return {str(item) for item in raw}
    if isinstance(raw, dict):
        return {str(key) for key in raw}
    return set()


def _steps(workflow: dict[Any, Any]) -> Iterator[tuple[str, dict[Any, Any]]]:
    for job_id, job in (workflow.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            yield str(job_id), step


def unpinned_uses(text: str) -> list[str]:
    """``uses:`` que no están fijados por hash completo (o por digest) con su versión."""
    problems = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = re.match(r"^\s*(?:-\s+)?uses:\s*(?P<ref>[^\s#]+)(?P<rest>.*)$", line)
        if match is None:
            continue
        reference = match["ref"].strip("'\"")
        if reference.startswith("./"):
            continue
        if _DOCKER_PINNED.match(reference):
            continue
        if not _PINNED.match(reference):
            problems.append(f"línea {number}: {reference} no está fijada por hash completo")
        elif not _VERSION_COMMENT.search(match["rest"]):
            problems.append(f"línea {number}: {reference} sin comentario de versión (# vX.Y.Z)")
    return problems


def permission_problems(text: str) -> list[str]:
    workflow = _load(text)
    problems = []
    top = workflow.get("permissions")
    if top != {"contents": "read"} and top not in ("read-all", {}):
        problems.append(f"permisos de nivel superior no son de solo lectura: {top!r}")
    for job_id, job in (workflow.get("jobs") or {}).items():
        permissions = job.get("permissions")
        if permissions is not None and _WRITE.search(str(permissions)):
            problems.append(f"el trabajo {job_id} pide permisos de escritura: {permissions!r}")
    forbidden = _triggers(workflow) & {"pull_request_target", "workflow_run"}
    if forbidden:
        problems.append(f"disparadores no permitidos: {sorted(forbidden)}")
    return problems


def unverified_downloads(text: str) -> list[str]:
    problems = []
    for job_id, step in _steps(_load(text)):
        run = str(step.get("run") or "")
        if "curl " in run and "sha256sum -c" not in run:
            problems.append(f"{job_id}: «{step.get('name')}» descarga sin verificar la suma")
    return problems


def unpinned_images(dockerfile: str) -> list[str]:
    """``FROM`` que no es una etapa anterior ni una imagen fijada por digest."""
    arguments: dict[str, str] = {}
    stages: set[str] = set()
    problems = []
    for line in dockerfile.splitlines():
        stripped = line.strip()
        argument = re.match(r"^ARG\s+(\w+)=(\S+)$", stripped)
        if argument:
            arguments[argument[1]] = argument[2]
            continue
        source = re.match(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?$", stripped, re.I)
        copy = re.search(r"COPY\s+--from=(\S+)", stripped)
        references = [source[1]] if source else ([copy[1]] if copy else [])
        for reference in references:
            resolved = re.sub(r"\$\{(\w+)\}", lambda m: arguments.get(m[1], m[0]), reference)
            if resolved in stages:
                continue
            if not re.search(r"@sha256:[0-9a-f]{64}$", resolved) or ":latest@" in resolved:
                problems.append(f"{reference} ({resolved}) no está fijada por digest")
        if source and source[2]:
            stages.add(source[2])
    return problems


def _workflow_files() -> list[Path]:
    files = sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])
    assert files, "no hay flujos en .github/workflows"
    return files


# --- El repositorio ----------------------------------------------------------------------------


@pytest.mark.parametrize("workflow", _workflow_files(), ids=lambda path: path.name)
def test_every_action_is_pinned_by_full_hash(workflow: Path) -> None:
    text = workflow.read_text(encoding="utf-8")
    assert "uses:" in text
    assert unpinned_uses(text) == []


@pytest.mark.parametrize("workflow", _workflow_files(), ids=lambda path: path.name)
def test_permissions_are_read_only_and_triggers_safe(workflow: Path) -> None:
    assert permission_problems(workflow.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("workflow", _workflow_files(), ids=lambda path: path.name)
def test_downloads_are_verified_by_checksum(workflow: Path) -> None:
    assert unverified_downloads(workflow.read_text(encoding="utf-8")) == []


def _ci() -> dict[Any, Any]:
    return _load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))


def _job_text(job: dict[Any, Any]) -> str:
    return "\n".join(str(step.get("run") or step.get("uses") or "") for step in job["steps"])


def test_ci_keeps_the_required_checks_and_runs_on_pull_requests_and_main() -> None:
    ci = _ci()
    names = [job["name"] for job in ci["jobs"].values()]
    for check in REQUIRED_CHECKS:
        assert names.count(check) == 1, check
    assert _triggers(ci) == {"pull_request", "push"}


def test_ci_has_the_jobs_of_lc_nuc_35() -> None:
    jobs = {job["name"]: _job_text(job) for job in _ci()["jobs"].values()}
    everything = "\n".join(jobs.values())
    for command in (
        "ruff check",
        "ruff format --check",
        "tools/lint_rules.py",
        "tools/lint_migrations.py",
        "mypy --strict src",
        "export_openapi --check",
        "build_verifier.py --check",
        "--hypothesis-profile=ci",
        "tools/check_licenses.py",
        "pip-audit",
        "gitleaks",
        "cdk synth",
    ):
        assert command in everything, command
    coverage = next(text for name, text in jobs.items() if name.startswith("cobertura"))
    assert "tools/check_coverage.py coverage.json" in coverage
    image = next(text for name, text in jobs.items() if name.startswith("imagen arm64"))
    assert "--platform linux/arm64 --ssh default -f backend/Dockerfile" in image
    assert "tools/image_audit.py" in image and "--needle-env CLAVE" in image
    assert "tools/run_boot_check.py" in image
    assert "--read-only" in image and "grype" in image and "--fail-on high" in image
    previous = next(text for name, text in jobs.items() if name.startswith("arranque N-1"))
    assert "--boot-image vigia-platform:n-1" in previous
    assert "--migrate-image vigia-platform:n " in previous


def test_the_coverage_job_needs_both_backend_jobs() -> None:
    ci = _ci()
    job = next(job for job in ci["jobs"].values() if job["name"].startswith("cobertura"))
    assert sorted(job["needs"]) == ["backend", "backend-integracion"]
    for job_id in ("backend", "backend-integracion"):
        steps = ci["jobs"][job_id]["steps"]
        assert any("coverage run -m pytest" in str(step.get("run")) for step in steps)


def test_the_deploy_key_never_reaches_a_build_argument_or_a_file_in_the_image_jobs() -> None:
    for job_id in ("imagen", "arranque-n-1"):
        job = _ci()["jobs"][job_id]
        text = _job_text(job)
        assert "--build-arg" not in text and "--secret" not in text
        assert "ssh-add -q -" in text and "> ~/.ssh" not in text
        assert job["runs-on"] == "ubuntu-24.04-arm"


def test_the_dockerfile_pins_every_image_by_digest() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert unpinned_images(text) == []
    assert re.search(r"^USER\s+10001:10001$", text, re.MULTILINE)
    assert "RUN --mount=type=ssh" in text
    assert "uv sync --frozen --no-dev" in text
    assert re.search(r'^CMD \["vigia-api"\]$', text, re.MULTILINE)


def test_the_boot_check_uses_the_integration_postgres_image() -> None:
    assert run_boot_check.POSTGRES_IMAGE == POSTGRES_IMAGE


def test_codeowners_covers_the_paths_of_a_26() -> None:
    owned = {
        line.split()[0]
        for line in CODEOWNERS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert owned >= {
        "/.github/CODEOWNERS",
        "/infra/",
        "/backend/migrations/",
        "/.github/workflows/",
        "/backend/src/vigia_platform/identity/auth/",
        "/backend/src/vigia_platform/identity/authz/",
        "/backend/src/vigia_platform/ledger/chain/",
        "/backend/src/vigia_platform/shared/signing/",
        "/backend/src/vigia_platform/shared/crypto/",
        "/frontend/src/state/session/",
        "/frontend/src/ports/",
        "/frontend/vite.config.ts",
        "/frontend/package.json",
        "/frontend/src/texts/",
    }
    owners = {
        tuple(line.split()[1:])
        for line in CODEOWNERS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert owners == {("@Machaves07",)}


def test_dependabot_watches_python_npm_actions_and_the_base_image() -> None:
    config = _load(DEPENDABOT.read_text(encoding="utf-8"))
    assert config["version"] == 2
    watched = {(u["package-ecosystem"], u["directory"]) for u in config["updates"]}
    assert watched == {
        ("uv", "/backend"),
        ("uv", "/infra"),
        ("npm", "/frontend"),
        ("github-actions", "/"),
        ("docker", "/backend"),
    }


# --- Negativos: cada regla falla con un flujo que la incumple ----------------------------------

_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"


@pytest.mark.parametrize(
    "line",
    [
        "      - uses: actions/checkout@v4",
        "      - uses: actions/checkout@main",
        "      - uses: actions/checkout@3d3c42e",
        f"      - uses: actions/checkout@{_SHA.upper()}  # v7.0.1",
        "      - uses: actions/checkout",
        "        uses: docker://alpine:3.20",
        "        uses: docker://alpine:latest@sha256:abc",
        f"      - uses: actions/checkout@{_SHA}",
        f"      - uses: 'actions/checkout@{_SHA}x'  # v7.0.1",
    ],
)
def test_a_mobile_or_short_reference_fails(line: str) -> None:
    assert unpinned_uses(f"steps:\n{line}\n")


@pytest.mark.parametrize(
    "line",
    [
        f"      - uses: actions/checkout@{_SHA}  # v7.0.1",
        f"        uses: github/codeql-action/init@{_SHA} # v3.29.0",
        "      - uses: ./.github/actions/local",
        "        uses: docker://alpine@sha256:" + "a" * 64,
    ],
)
def test_a_pinned_reference_passes(line: str) -> None:
    assert unpinned_uses(f"steps:\n{line}\n") == []


@pytest.mark.parametrize(
    "workflow",
    [
        "on: push\npermissions: write-all\njobs: {}\n",
        "on: push\njobs: {}\n",
        "on: push\npermissions: {contents: write}\njobs: {}\n",
        "on: push\npermissions: {contents: read}\njobs: {a: {permissions: {packages: write}}}\n",
        "on: [push, pull_request_target]\npermissions: {contents: read}\njobs: {}\n",
        "on: {workflow_run: {}}\npermissions: {contents: read}\njobs: {}\n",
    ],
)
def test_write_permissions_or_unsafe_triggers_fail(workflow: str) -> None:
    assert permission_problems(workflow)


def test_an_unverified_download_fails() -> None:
    workflow = (
        "on: push\npermissions: {contents: read}\njobs:\n  a:\n    steps:\n"
        "      - name: bajar\n        run: curl -sSfL -o x.tgz https://example.invalid/x.tgz\n"
    )
    assert unverified_downloads(workflow)


@pytest.mark.parametrize(
    "dockerfile",
    [
        "FROM python:3.12-slim\n",
        "FROM python:latest\n",
        "ARG BASE=python:3.12-slim\nFROM ${BASE}\n",
        "FROM python:3.12-slim@sha256:abc\n",
        "FROM python@sha256:" + "a" * 64 + " AS a\nCOPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uv\n",
        "FROM python:latest@sha256:" + "a" * 64 + "\n",
    ],
)
def test_an_image_without_digest_fails(dockerfile: str) -> None:
    assert unpinned_images(dockerfile)


def test_stages_and_pinned_arguments_pass() -> None:
    digest = "@sha256:" + "b" * 64
    dockerfile = (
        f"ARG BASE=python:3.12-slim{digest}\nFROM ${{BASE}} AS build\n"
        "FROM build AS runtime\nCOPY --from=build /app /app\n"
    )
    assert unpinned_images(dockerfile) == []
