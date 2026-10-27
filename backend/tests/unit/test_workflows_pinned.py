"""Lint de los flujos de GitHub Actions y de la imagen (TASK-143; NFR-NUC-24; SECURITY-10).

Criterio de aceptación: ninguna acción referenciada por etiqueta móvil. Además, lo que la
canalización promete y un cambio descuidado rompería sin que nadie lo viera:

- **Acciones fijadas por hash**: todo ``uses:`` de ``.github/workflows/*.yml`` (leído del YAML,
  también en un mapeo en línea ``- {uses: …}``) es una acción local (``./…``),
  ``propietario/repo[/ruta]@<hash de 40>`` con su comentario de versión, o
  ``docker://imagen@sha256:<64>``. Una etiqueta (``@v4``), una rama (``@main``) o un hash corto no
  valen. Solo se usan las cuatro acciones de primera parte que ya fija ``ci.yml``.
- **Permisos y disparadores**: permisos de nivel superior de solo lectura; el único ``write`` es
  ``id-token: write`` (federación con AWS sin claves); sin ``pull_request_target`` ni
  ``workflow_run``.
- **AWS (A-47, TASK-151)**: todo trabajo que toca AWS lleva
  ``if: vars.VIGIA_AWS_ENABLED == 'true'`` como conjunción (sin ``||``), corre en el entorno
  ``staging`` o ``pilot`` (la confianza del rol ``vigia-deploy``) y pide ``id-token: write``;
  ningún flujo nombra claves de AWS de larga duración; lo que despliega en ``pilot`` corre en el
  entorno ``pilot`` (revisor obligatorio).
- **Los cinco flujos de TASK-151**: ``nightly``, ``release`` (orden corregido del primer
  despliegue en ``staging-<n>``, destrucción con ``always()`` y residuos, ``pilot`` sin ``dry-run``
  y etiqueta al final), ``rollback``, ``trust-store`` y ``staging-sweeper``.
- **Herramientas de CI fijadas por suma**: ``coverage`` y ``pip-audit`` con ``--require-hashes``.
- **Descargas verificadas**: toda orden que descarga con ``curl`` comprueba la suma con
  ``sha256sum -c`` en el mismo paso.
- **ci.yml**: conserva los nombres de sus trabajos, incluidos los checks obligatorios,
  y tiene los trabajos de cobertura (que
  ejecuta ``tools/check_coverage.py``), licencias, pip-audit, imagen arm64 con ``--ssh default``,
  auditoría de la imagen y arranque N-1.
- **frontend (VIG-294)**: Node 22 fijado, ``npm ci --ignore-scripts`` con la clave de despliegue en
  un archivo que se borra, y cada verificación de TASK-412 en su orden (``frontend_job_problems``).
- **Imágenes base por digest**: cada ``FROM`` de ``backend/Dockerfile`` es una etapa anterior o una
  imagen ``@sha256:``, nunca ``latest``; la de PostgreSQL de ``tools/run_boot_check.py`` es la
  de las pruebas de integración.
- **CODEOWNERS y Dependabot**: las rutas de A-26 y los ecosistemas de deployment-architecture §3.1.

Cada regla se prueba también en negativo con un flujo sintético que la incumple.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.integration.conftest import POSTGRES_IMAGE
from tests.unit.test_ci_runs_u03_suites import _pytest_commands, coverage_problems
from tools import run_boot_check
from tools.check_test_split import SPECS, read_job, static_problems

REPOSITORY = Path(__file__).resolve().parents[3]
WORKFLOWS = REPOSITORY / ".github" / "workflows"
DOCKERFILE = REPOSITORY / "backend" / "Dockerfile"
CODEOWNERS = REPOSITORY / ".github" / "CODEOWNERS"
DEPENDABOT = REPOSITORY / ".github" / "dependabot.yml"

REQUIRED_CHECKS = (
    # Retro 29 (dueño, 2026-10-24): decide si un PR solo de frontend/ o docs/ salta los
    # trabajos de backend.
    "rutas del cambio (frontend o backend)",
    "escaneo de secretos",
    "backend (lint y tipos)",
    "backend (pruebas sin integración, grupo ${{ matrix.grupo }} de 4)",
    "backend (lint, tipos y pruebas sin integración)",
    "backend (pruebas de integración, grupo ${{ matrix.grupo }} de 4)",
    "backend (pruebas de integración)",
    "backend (pruebas de integración, segunda mitad)",
    "backend (carga ci)",
    "cobertura (≥ 90 % identity y ledger, ≥ 80 % global)",
    "dependencias (licencias y pip-audit)",
    "imagen arm64 (construcción, auditoría, arranque y escaneo)",
    "arranque N-1 contra el esquema N",
    "flujos (actionlint)",
    "infra (pruebas y cdk synth)",
    # VIG-294 (TASK-412): al fusionarlo, la sesión de control lo añade a los checks de procesar.py.
    "frontend (verificaciones, pruebas y construcción)",
)
"""Los checks obligatorios de la rama (AGENTS.md): renombrarlos deja la rama sin protección."""

_PINNED = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_./-]+)?@[0-9a-f]{40}$")
_DOCKER_PINNED = re.compile(r"^docker://[^@\s]+@sha256:[0-9a-f]{64}$")
_VERSION_COMMENT = re.compile(r"#\s*v\d+(?:\.\d+)*\b")
_WRITE = re.compile(r"\bwrite\b|write-all")
_ALLOWED_WRITE = frozenset({"id-token"})
"""Único permiso de escritura: el token OIDC de la federación con AWS (SECURITY-10)."""
FIRST_PARTY_ACTIONS = frozenset(
    {
        "actions/checkout",
        "astral-sh/setup-uv",
        "actions/upload-artifact",
        "actions/download-artifact",
        "actions/setup-node",  # VIG-294: Node 22 fijado del trabajo frontend de ci.yml.
    }
)
"""Las acciones que ya fija ``ci.yml``. La federación y el token de ``vigia-release`` van por
``.github/scripts/`` con la biblioteca estándar: ninguna acción de terceros toca credenciales."""
TASK_151_WORKFLOWS = ("nightly", "release", "rollback", "trust-store", "staging-sweeper")
AWS_GATE = "vars.VIGIA_AWS_ENABLED == 'true'"
"""A-47: sin esta variable de repositorio, ningún trabajo toca AWS."""
_AWS_MARKERS = re.compile(
    r"aws_federation\.py"
    r"|\baws (?:ecr|ecs|elbv2|sts|cloudformation|s3|s3api|kms|ssm|secretsmanager)\b"
    r"|\bcdk (?:deploy|destroy|diff)\b|\$CDK (?:deploy|destroy|diff)\b"
    r"|staging\.py (?:run-task|residue|orphans|output)|deploy_checks\.py"
)
_LONG_LIVED_KEYS = re.compile(
    r"AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|aws-access-key-id|aws-secret-access-key|secrets\.AWS_",
    re.IGNORECASE,
)
_PILOT_ACTIONS = re.compile(
    r"\bcdk (?:deploy|destroy)\b|\$CDK (?:deploy|destroy)\b|staging\.py (?:run-task|output)"
    r"|deploy_checks\.py"
)
DEPLOY_ENVIRONMENTS = frozenset({"staging", "pilot"})
"""Entornos en los que confía ``vigia-deploy`` (A-40: ``repo:Machaves07/vigia-platform``)."""


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


def _uses_values(node: object) -> Iterator[str]:
    """Todos los ``uses`` del documento, en pasos y en trabajos, con cualquier sintaxis YAML."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "uses":
                yield str(value)
            else:
                yield from _uses_values(value)
    elif isinstance(node, list):
        for item in node:
            yield from _uses_values(item)


def unpinned_uses(text: str) -> list[str]:
    """``uses`` que no están fijados por hash completo (o por digest) con su versión.

    Se leen del YAML, no por línea: un mapeo en línea (``- {uses: owner/action@v5}``) también cuenta
    (seguimiento de VIG-94). El comentario de versión se busca en la línea del ``uses``.
    """
    problems = []
    lines = text.splitlines()
    for reference in _uses_values(yaml.safe_load(text)):
        if reference.startswith("./") or _DOCKER_PINNED.match(reference):
            continue
        if not _PINNED.match(reference):
            problems.append(f"{reference} no está fijada por hash completo")
            continue
        on_its_line = re.compile(
            rf"uses:\s*['\"]?{re.escape(reference)}['\"]?\s*\}}?\s*(?P<rest>.*)$"
        )
        rests = [m["rest"] for line in lines if (m := on_its_line.search(line))]
        if not any(_VERSION_COMMENT.search(rest) for rest in rests):
            problems.append(f"{reference} sin comentario de versión (# vX.Y.Z) en su línea")
    return problems


def permission_problems(text: str) -> list[str]:
    workflow = _load(text)
    problems = []
    top = workflow.get("permissions")
    if top != {"contents": "read"} and top not in ("read-all", {}):
        problems.append(f"permisos de nivel superior no son de solo lectura: {top!r}")
    for job_id, job in (workflow.get("jobs") or {}).items():
        permissions = job.get("permissions")
        if isinstance(permissions, dict):
            granted = {
                str(scope): str(level)
                for scope, level in permissions.items()
                if _WRITE.search(str(level)) and scope not in _ALLOWED_WRITE
            }
            if granted:
                problems.append(f"el trabajo {job_id} pide permisos de escritura: {granted!r}")
        elif permissions is not None and _WRITE.search(str(permissions)):
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


def _environment_name(job: dict[Any, Any]) -> str | None:
    environment = job.get("environment")
    if isinstance(environment, dict):
        environment = environment.get("name")
    return None if environment is None else str(environment)


def _run_text(job: dict[Any, Any]) -> str:
    return "\n".join(str(step.get("run") or "") for step in job.get("steps") or [])


def aws_job_problems(text: str) -> list[str]:
    """A-47 y SECURITY-10: los trabajos que tocan AWS, tras la variable, en un entorno del rol y
    con el token OIDC; un ``id-token: write`` fuera de ellos tampoco vale."""
    problems = []
    for job_id, job in (_load(text).get("jobs") or {}).items():
        permissions = job.get("permissions") or {}
        oidc = isinstance(permissions, dict) and permissions.get("id-token") == "write"
        if not (_AWS_MARKERS.search(_run_text(job)) or oidc):
            continue
        condition = str(job.get("if") or "")
        if AWS_GATE not in condition or "||" in condition:
            problems.append(f"{job_id}: toca AWS sin la conjunción «{AWS_GATE}» en su if")
        if _environment_name(job) not in DEPLOY_ENVIRONMENTS:
            problems.append(f"{job_id}: toca AWS fuera de los entornos staging y pilot")
        if not oidc:
            problems.append(f"{job_id}: toca AWS sin id-token: write (federación sin claves)")
    return problems


def long_lived_key_problems(text: str) -> list[str]:
    """Ninguna clave de acceso de AWS en variables, entradas ni secretos del flujo."""
    return [
        f"línea {number}: {match[0]}"
        for number, line in enumerate(text.splitlines(), start=1)
        if (match := _LONG_LIVED_KEYS.search(line))
    ]


def pilot_environment_problems(text: str) -> list[str]:
    """Lo que despliega o revierte ``pilot`` corre en el entorno ``pilot`` (revisor obligatorio)."""
    problems = []
    for job_id, job in (_load(text).get("jobs") or {}).items():
        run = _run_text(job)
        # ``cdk diff`` de pilot solo lee (lo adjunta el trabajo de publicación, en staging).
        if re.search(r"environment=pilot|--environment pilot", run) and _PILOT_ACTIONS.search(run):
            touches_pilot = True
        else:
            touches_pilot = bool(re.search(r"elbv2 (?:add|remove)-trust-store-revocations", run))
        if touches_pilot and _environment_name(job) != "pilot":
            problems.append(f"{job_id}: toca pilot fuera del entorno pilot")
    return problems


def requirement_problems(text: str) -> list[str]:
    """Cada requisito fijado con ``==`` y con al menos una suma ``--hash=sha256:``."""
    problems = []
    blocks = re.split(r"\n(?=[A-Za-z0-9])", "\n" + text)
    for block in blocks:
        head = block.strip().splitlines()[0] if block.strip() else ""
        if not head or head.startswith("#"):
            continue
        name = head.split()[0]
        if "==" not in name:
            problems.append(f"{name}: sin versión exacta")
        if not re.search(r"--hash=sha256:[0-9a-f]{64}", block):
            problems.append(f"{name}: sin suma")
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


@pytest.mark.parametrize("workflow", _workflow_files(), ids=lambda path: path.name)
def test_aws_jobs_are_gated_in_a_deploy_environment_with_oidc(workflow: Path) -> None:
    assert aws_job_problems(workflow.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("workflow", _workflow_files(), ids=lambda path: path.name)
def test_no_workflow_names_long_lived_aws_keys(workflow: Path) -> None:
    assert long_lived_key_problems(workflow.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("workflow", _workflow_files(), ids=lambda path: path.name)
def test_whatever_changes_pilot_runs_in_the_pilot_environment(workflow: Path) -> None:
    assert pilot_environment_problems(workflow.read_text(encoding="utf-8")) == []


def test_only_the_first_party_actions_are_used() -> None:
    used = {
        reference.split("@", 1)[0]
        for workflow in _workflow_files()
        for reference in _uses_values(_load(workflow.read_text(encoding="utf-8")))
    }
    assert used == FIRST_PARTY_ACTIONS


def _workflow(name: str) -> dict[Any, Any]:
    return _load((WORKFLOWS / f"{name}.yml").read_text(encoding="utf-8"))


def _position(text: str, needle: str) -> int:
    assert needle in text, needle
    return text.index(needle)


def test_the_five_workflows_of_task_151_exist() -> None:
    present = {path.stem for path in _workflow_files()}
    assert present >= set(TASK_151_WORKFLOWS)


def test_every_aws_job_of_the_five_workflows_is_gated() -> None:
    """Con la variable ausente, se omiten justo los trabajos que tocan AWS (A-47)."""
    expected = {
        "release": {"publicar", "staging", "destruir-staging", "pilot"},
        "rollback": {"revertir"},
        "trust-store": {"publicar"},
        "staging-sweeper": {"barrer"},
        "nightly": set(),
    }
    for name, jobs in expected.items():
        gated = {
            job_id
            for job_id, job in _workflow(name)["jobs"].items()
            if AWS_GATE in str(job.get("if") or "")
        }
        assert gated == jobs, name


def test_release_is_manual_with_version_and_dry_run_by_default() -> None:
    release = _workflow("release")
    assert _triggers(release) == {"workflow_dispatch"}
    inputs = release[True]["workflow_dispatch"]["inputs"]
    assert inputs["version"]["required"] is True
    assert inputs["dry-run"] == {**inputs["dry-run"], "type": "boolean", "default": True}
    assert inputs["soak"]["default"] is False
    preparation = _run_text(release["jobs"]["preparar"])
    assert "gh run list" in preparation and "--workflow nightly.yml" in preparation
    assert '--commit "$GITHUB_SHA"' in preparation and "--status success" in preparation
    assert release["jobs"]["preparar"]["permissions"] == {"contents": "read", "actions": "read"}


def test_release_builds_scans_and_documents_the_arm64_image() -> None:
    job = _workflow("release")["jobs"]["imagen"]
    assert job["runs-on"] == "ubuntu-24.04-arm"
    run = _run_text(job)
    assert "--platform linux/arm64 --ssh default -f backend/Dockerfile" in run
    assert "ssh-add -q -" in run and "> ~/.ssh" not in run
    # VIG-337: el único argumento de construcción es la versión ya validada (nunca la clave).
    assert release_build_argument_problems(run) == []
    assert "tools/image_audit.py" in run and "--needle-env CLAVE" in run
    assert "--fail-on high" in run and "cyclonedx-json=" in run
    assert "uv export --frozen --no-dev --format cyclonedx1.5" in run
    assert "tools/build_verifier.py --check" in run and "sha256sum tools/vigia_verify.py" in run


def test_release_synthesizes_staging_and_pilot_without_aws() -> None:
    job = _workflow("release")["jobs"]["sintesis"]
    assert "if" not in job and "environment" not in job
    run = _run_text(job)
    assert '-c environment="$STAGING" -c first_deploy=true' in run
    assert "-c environment=pilot" in run


def test_staging_follows_the_corrected_first_deployment_order() -> None:
    """Nota U02-H-01: pilas con first_deploy=true, vigia-migrate, vigia-admin bootstrap, pilas con
    first_deploy=false (almacén de confianza y servicios escalados) y después las comprobaciones."""
    job = _workflow("release")["jobs"]["staging"]
    assert _environment_name(job) == "staging"
    run = _run_text(job)
    steps = [
        "-c first_deploy=true",
        "--task migrate",
        "vigia-admin bootstrap",
        "-c first_deploy=false",
        "aws ecs wait services-stable",
        'deploy_checks.py --environment "$STAGING"',
    ]
    positions = [_position(run, step) for step in steps]
    assert positions == sorted(positions)
    assert '-c environment="$STAGING"' in run


def soak_artifact_problems(text: str) -> list[str]:
    """PR-GOB-31: el artefacto del soak es solo el informe, nunca los secretos del entorno.

    La flota, la sesión de consola y las credenciales viven en otro directorio, se enmascaran y se
    borran antes de subir el artefacto.
    """
    steps = _load(text)["jobs"]["staging"]["steps"]
    uploads = [
        step
        for step in steps
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
        and step["with"].get("name") == "informe-soak"
    ]
    if not uploads:
        return ["el trabajo staging no sube un artefacto «informe-soak»"]
    problems = []
    path = str(uploads[0]["with"]["path"])
    for forbidden in ("secretos", "fleet", "console-session", "files"):
        if forbidden in path:
            problems.append(f"el artefacto informe-soak sube {forbidden!r}: {path}")
    runs = [str(step.get("run") or "") for step in steps]
    cleanup = [index for index, run in enumerate(runs) if "rm -rf" in run and "secretos" in run]
    if not cleanup:
        problems.append("no hay un paso que borre los secretos del soak")
    elif cleanup[0] > steps.index(uploads[0]):
        problems.append("el artefacto del soak se sube antes de borrar los secretos")
    if not any("::add-mask::" in run for run in runs):
        problems.append("la cookie de la sesión de consola no se enmascara con ::add-mask::")
    return problems


def test_the_soak_artifact_carries_only_the_report_never_the_secrets() -> None:
    release = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert soak_artifact_problems(release) == []
    # Negativos: el artefacto que sube el directorio de secretos y el que se sube antes de
    # borrarlos fallan.
    assert soak_artifact_problems(
        release.replace("path: ${{ runner.temp }}/soak/", "path: ${{ runner.temp }}/soak-secretos/")
    )
    without_cleanup = release.replace('rm -rf "${{ runner.temp }}/soak-secretos"', "true")
    assert any(
        "borre los secretos" in problem for problem in soak_artifact_problems(without_cleanup)
    )


def test_staging_is_destroyed_always_and_checked_for_residue() -> None:
    release = _workflow("release")
    job = release["jobs"]["destruir-staging"]
    assert str(job["if"]).startswith("always() && ")
    assert "staging" in job["needs"]
    run = _run_text(job)
    assert _position(run, '$CDK destroy --force -c environment="$STAGING"') < _position(
        run, 'staging.py residue --environment "$STAGING"'
    )
    assert release["env"]["STAGING"] == "staging-${{ github.run_number }}"


def test_pilot_needs_the_destroyed_staging_and_is_not_a_dry_run() -> None:
    job = _workflow("release")["jobs"]["pilot"]
    assert _environment_name(job) == "pilot"
    assert {"staging", "destruir-staging", "publicar"} <= set(job["needs"])
    condition = str(job["if"])
    assert "!inputs.dry-run" in condition and "!inputs.soak" in condition
    assert "github.ref == 'refs/heads/main'" in condition
    run = _run_text(job)
    assert "-c environment=pilot" in run and "vigia-datasets" not in run
    assert "--environment pilot" in run and "deploy_checks.py" in run


def test_the_tag_is_created_last_by_vigia_release() -> None:
    job = _workflow("release")["jobs"]["etiqueta"]
    assert "pilot" in job["needs"] and "if" not in job
    steps = job["steps"]
    assert 'gh release create "v$VERSION"' in str(steps[-1]["run"])
    token = next(step for step in steps if step.get("id") == "app")
    assert token["run"] == "python3 .github/scripts/github_app_token.py"
    assert token["env"] == {
        "VIGIA_RELEASE_APP_ID": "${{ secrets.VIGIA_RELEASE_APP_ID }}",
        "VIGIA_RELEASE_PRIVATE_KEY": "${{ secrets.VIGIA_RELEASE_PRIVATE_KEY }}",
    }
    assert "${{ steps.app.outputs.token }}" in str(steps)


def test_the_release_summary_says_what_was_skipped_without_aws() -> None:
    job = _workflow("release")["jobs"]["resumen"]
    assert job["if"] == "always()"
    assert "Sin AWS (A-47)" in _run_text(job)


def test_rollback_redeploys_compute_with_the_previous_digest_in_pilot() -> None:
    rollback = _workflow("rollback")
    assert _triggers(rollback) == {"workflow_dispatch"}
    assert rollback[True]["workflow_dispatch"]["inputs"]["digest"]["required"] is True
    job = rollback["jobs"]["revertir"]
    assert _environment_name(job) == "pilot"
    run = _run_text(job)
    assert "^sha256:[0-9a-f]{64}$" in run
    assert _position(run, "aws ecr describe-images") < _position(run, "vigia-compute")
    assert _position(run, '-c image_digest="$DIGEST" vigia-compute') < _position(
        run, "deploy_checks.py --environment pilot"
    )


def test_trust_store_is_a_manual_backup_that_never_leaves_the_store_empty() -> None:
    workflow = _workflow("trust-store")
    assert _triggers(workflow) == {"workflow_dispatch"}
    job = workflow["jobs"]["publicar"]
    assert _environment_name(job) == "pilot"
    run = _run_text(job)
    assert _position(run, "add-trust-store-revocations") < _position(
        run, 'describe-trust-store-revocations --trust-store-arn "$STORE" --revocation-ids'
    )
    assert _position(run, '--revocation-ids "$NUEVA"') < _position(
        run, "remove-trust-store-revocations"
    )


def test_the_sweeper_destroys_staging_older_than_six_hours_every_night() -> None:
    workflow = _workflow("staging-sweeper")
    assert _triggers(workflow) == {"schedule", "workflow_dispatch"}
    job = workflow["jobs"]["barrer"]
    assert _environment_name(job) == "staging"
    run = _run_text(job)
    assert "staging.py orphans" in run and "--hours 6" in run
    assert _position(run, '$CDK destroy --all --force -c environment="$ENTORNO"') < _position(
        run, 'staging.py residue --environment "$ENTORNO"'
    )


@pytest.mark.parametrize(
    ("job_id", "name"),
    [
        (
            "propiedades-suite",
            "propiedades nightly (tests/properties, grupo ${{ matrix.grupo }} de 5)",
        ),
        (
            "propiedades-integracion",
            "propiedades nightly (integración, grupo ${{ matrix.grupo }} de 5)",
        ),
    ],
)
def test_nightly_splits_the_properties_by_measured_times_without_hand_lists(
    job_id: str, name: str
) -> None:
    """VIG-199: los dos trabajos de propiedades del nightly son matrices de pytest-split por los
    tiempos de ``.test_durations_nightly`` (perfil nightly; el de ``ci.yml`` es otro), sin listas
    de archivos a mano; la forma la exige ``tools/check_test_split.py`` (estática aquí, con
    ``--collect`` en «backend (lint y tipos)»). Cada grupo deja sus tiempos medidos como artefacto
    y el tope de cada uno es de 90 min (la meta es ≤ 60)."""
    spec = {spec.job_id: spec for spec in SPECS}[job_id]
    nightly_text = (WORKFLOWS / "nightly.yml").read_text(encoding="utf-8")
    assert static_problems(read_job(nightly_text, job_id), spec) == []
    job = _workflow("nightly")["jobs"][job_id]
    assert job["name"] == name
    assert job["strategy"] == {"fail-fast": False, "matrix": {"grupo": [1, 2, 3, 4, 5]}}
    assert job["timeout-minutes"] <= 90
    run = _run_text(job)
    assert "--hypothesis-profile=nightly" in run and "include" not in str(job["strategy"])
    assert "--store-durations --clean-durations" in run
    (artifact,) = [
        step
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
    ]
    assert artifact["if"] == "always()"
    assert artifact["with"]["path"] == "backend/.test_durations_nightly"
    assert artifact["with"]["include-hidden-files"] is True
    assert str(artifact["with"]["name"]).startswith("tiempos-")
    durations = json.loads((REPOSITORY / "backend" / spec.durations).read_text(encoding="utf-8"))
    assert isinstance(durations, dict) and durations
    assert all(key.startswith("tests/properties/") for key in durations)


def test_nightly_runs_the_suites_of_lc_nuc_35_and_keeps_the_report_90_days() -> None:
    nightly = _workflow("nightly")
    assert _triggers(nightly) == {"schedule", "workflow_dispatch"}
    jobs = nightly["jobs"]
    everything = "\n".join(_run_text(job) for job in jobs.values())
    for command in (
        '--hypothesis-profile=nightly -m "not integration"',
        "--hypothesis-profile=nightly -m integration",
        "--hypothesis-profile=nightly tests/benchmarks",
        "generate_scale_data.py --scale target",
        "--hypothesis-profile=nightly tests/resilience",
    ):
        assert command in everything, command
    assert "VIGIA_BENCHMARK_UPDATE_BASELINE" in str(jobs["bancos"])
    # Conformidad de U-01 contra la plataforma (TASK-230): la suite completa del kit, una sola vez
    # (las propiedades con integración la excluyen), con su informe de 90 días.
    conformance = jobs["conformance"]
    assert conformance["name"] == "conformidad de U-01 contra la plataforma"
    run = _run_text(conformance)
    assert "--hypothesis-profile=nightly -m integration tests/conformance" in run
    assert "Conformidad omitida" not in run and "exit 1" not in run
    assert _position(run, "uv sync --frozen") < _position(run, "rm -f ~/.ssh/vigia_contracts")
    # Reparto del 2026-10-13 (VIG-173 y VIG-199): la integración y las pruebas sin base que no son
    # propiedades excluyen tests/properties, y tests/properties entero (sin y con base) lo corren
    # los grupos de pytest-split, cuya unión comprueba tools/check_test_split.py.
    integration = _run_text(jobs["integracion"])
    assert "--hypothesis-profile=nightly -m integration" in integration
    assert "--ignore=tests/conformance" in integration
    assert "--ignore=tests/properties" in integration
    assert "--ignore=tests/properties" in _run_text(jobs["propiedades"])
    assert {spec.job_id for spec in SPECS if spec.workflow.name == "nightly.yml"} == {
        "propiedades-suite",
        "propiedades-integracion",
    }
    assert {(spec.marker, spec.paths) for spec in SPECS if spec.workflow.name == "nightly.yml"} == {
        ("not integration", ("tests/properties",)),
        ("integration", ("tests/properties",)),
    }
    (artifact,) = [
        step["with"]
        for step in conformance["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
    ]
    assert artifact["name"] == "informe-conformidad"
    assert set(jobs["informe"]["needs"]) == set(jobs) - {"informe"}
    retention = [
        step["with"]["retention-days"]
        for job in jobs.values()
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
    ]
    assert retention and set(retention) == {90}


def _ci() -> dict[Any, Any]:
    return _load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))


def _job_text(job: dict[Any, Any]) -> str:
    return "\n".join(str(step.get("run") or step.get("uses") or "") for step in job["steps"])


def test_ci_keeps_the_required_checks_and_runs_on_pull_requests_and_main() -> None:
    ci = _ci()
    names = [job["name"] for job in ci["jobs"].values()]
    for check in REQUIRED_CHECKS:
        assert names.count(check) == 1, check
    assert len(names) == len(REQUIRED_CHECKS)
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
    flows = next(text for name, text in jobs.items() if name.startswith("flujos"))
    assert "actionlint" in flows


def test_the_coverage_job_keeps_its_exact_name() -> None:
    """NFR-GOB-59: el nombre del trabajo de cobertura no cambia al añadir `catalog` y `fleet`."""
    names = [job["name"] for job in _ci()["jobs"].values()]
    assert names.count("cobertura (≥ 90 % identity y ledger, ≥ 80 % global)") == 1


def test_the_previous_image_job_fails_when_the_base_cannot_boot() -> None:
    """Seguimiento de VIG-94: sin Dockerfile o image_boot.py en la base, «arranque N-1» falla."""
    job = _ci()["jobs"]["arranque-n-1"]
    run = _job_text(job)
    missing = run.index("n-1/backend/tools/image_boot.py")
    assert "exit 1" in run[missing : missing + 400]
    assert "exit 0" not in run
    assert all("if" not in step for step in job["steps"][3:])


def test_ci_tools_are_installed_by_hash() -> None:
    """Seguimiento de VIG-94: coverage y pip-audit con --require-hashes, no ``uvx --from``."""
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    assert "uvx" not in text
    for name in ("coverage", "pip-audit"):
        assert f"--require-hashes -r tools/requirements/{name}.txt" in text
        requirements = (
            REPOSITORY / "backend" / "tools" / "requirements" / f"{name}.txt"
        ).read_text(encoding="utf-8")
        assert requirement_problems(requirements) == [], name
    lock = (REPOSITORY / "backend" / "uv.lock").read_text(encoding="utf-8")
    locked = re.search(r'name = "coverage"\nversion = "([^"]+)"', lock)
    assert locked is not None
    coverage = (REPOSITORY / "backend" / "tools" / "requirements" / "coverage.txt").read_text(
        encoding="utf-8"
    )
    assert f"coverage=={locked[1]} " in coverage


def test_the_coverage_job_needs_every_backend_job_with_coverage() -> None:
    """Los 4 grupos sin integración (VIG-198) y los 4 de la integración (2026-10-19): ninguna
    cifra baja. Cada trabajo con cobertura sube su artefacto ``cobertura-*`` (en la matriz,
    uno por grupo) y «cobertura» los combina todos."""
    ci = _ci()
    job = next(job for job in ci["jobs"].values() if job["name"].startswith("cobertura"))
    backend_jobs = ["backend-integracion-grupos", "backend-pruebas"]
    assert sorted(job["needs"]) == sorted([*backend_jobs, "rutas"])
    artifacts = []
    for job_id in backend_jobs:
        steps = ci["jobs"][job_id]["steps"]
        assert any("coverage run -m pytest" in str(step.get("run")) for step in steps)
        artifacts += [
            str(step["with"]["name"])
            for step in steps
            if str(step.get("uses", "")).startswith("actions/upload-artifact")
            and str(step["with"]["name"]).startswith("cobertura-")
        ]
    assert artifacts == [
        "cobertura-integracion-${{ matrix.grupo }}",
        "cobertura-sin-integracion-${{ matrix.grupo }}",
    ]
    (download,) = [
        step["with"]
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/download-artifact")
    ]
    assert download["pattern"] == "cobertura-*" and download["merge-multiple"] is True
    # Ningún otro trabajo mide cobertura sin que «cobertura» lo espere.
    measured = {
        job_id for job_id, other in ci["jobs"].items() if "coverage run" in _job_text(other)
    }
    assert measured == set(backend_jobs)


def test_the_required_backend_check_aggregates_lint_and_the_four_groups() -> None:
    """VIG-198: el check obligatorio «backend (lint, tipos y pruebas sin integración)» conserva su
    nombre, espera al lint y a la matriz, corre aunque fallen (un check omitido cuenta como verde)
    y solo da verde si los dos terminaron en ``success``."""
    jobs = _ci()["jobs"]
    job = jobs["backend"]
    assert job["name"] == "backend (lint, tipos y pruebas sin integración)"
    assert sorted(job["needs"]) == ["backend-lint", "backend-pruebas", "rutas"]
    assert str(job["if"]).startswith("always() && ")
    assert "github.event.pull_request.draft == false" in job["if"]
    (step,) = job["steps"]
    assert step["env"] == {
        "LINT": "${{ needs.backend-lint.result }}",
        "PRUEBAS": "${{ needs.backend-pruebas.result }}",
        "BACKEND": "${{ needs.rutas.outputs.backend }}",
    }
    assert 'test "$LINT" = success && test "$PRUEBAS" = success' in step["run"]
    # Retro 29: solo salta con un «false» explícito de «rutas» (falla cerrado).
    assert 'if [ "$BACKEND" = false ]; then' in step["run"]
    lint = _job_text(jobs["backend-lint"])
    for command in (
        "ruff check",
        "tools/lint_rules.py",
        "tools/lint_migrations.py",
        "mypy --strict src",
        "export_openapi --check",
        "build_verifier.py --check",
        "tools/check_test_split.py --collect",
    ):
        assert command in lint, command
    assert "pytest" not in lint
    matrix = jobs["backend-pruebas"]
    assert matrix["strategy"] == {"fail-fast": False, "matrix": {"grupo": [1, 2, 3, 4]}}
    assert matrix["name"] == "backend (pruebas sin integración, grupo ${{ matrix.grupo }} de 4)"
    assert matrix["timeout-minutes"] <= 30
    assert (REPOSITORY / "backend" / ".test_durations").is_file()


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
        # Seguimiento de VIG-94 y TASK-151.
        "/.github/scripts/",
        "/.github/dependabot.yml",
        "/backend/tools/",
        "/backend/tests/unit/test_workflows_pinned.py",
        "/AGENTS.md",
        "/.gitleaksignore",
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
    # PAT-APP-SEG-09 (VIG-294): npm de frontend/ semanal y agrupado.
    (npm,) = [u for u in config["updates"] if u["package-ecosystem"] == "npm"]
    assert npm["schedule"]["interval"] == "weekly"
    assert npm["groups"] and all(group.get("update-types") for group in npm["groups"].values())


FRONTEND_JOB = "frontend"
FRONTEND_COMMANDS = (
    "npm ci --ignore-scripts",
    "rm -f ~/.ssh/vigia_contracts",
    "npm run generate:check",
    "npm run lint",
    "npm run typecheck",
    "npm run knip",
    "npm run check",
    "npm test -- --coverage",
    "npm run audit:check",
    "npm run build",
    "npm run size",
)
"""Pasos de TASK-412, en este orden: la construcción y su tamaño, al final."""
_NODE_22 = re.compile(r"^22\.\d+\.\d+$")


def frontend_job_problems(text: str) -> list[str]:
    """VIG-294 (TASK-412): el trabajo ``frontend`` de ``ci.yml`` corre cada verificación de
    ``frontend/`` en su orden, con Node 22 fijado y la clave de despliegue solo en un archivo que
    se borra pase lo que pase; ningún paso se salta por condición ni deja seguir tras un fallo."""
    jobs = _load(text).get("jobs") or {}
    job = jobs.get(FRONTEND_JOB)
    if not isinstance(job, dict):
        return ["no hay trabajo frontend"]
    problems = []
    if job.get("name") != "frontend (verificaciones, pruebas y construcción)":
        problems.append(f"nombre del trabajo: {job.get('name')!r}")
    if job.get("runs-on") != "ubuntu-24.04":
        problems.append(f"runs-on: {job.get('runs-on')!r}")
    if job.get("defaults", {}).get("run", {}).get("working-directory") != "frontend":
        problems.append("los pasos no corren en frontend/")
    if "needs" in job or job.get("continue-on-error"):
        problems.append("el trabajo depende de otro o no bloquea")
    steps = job.get("steps") or []
    setup = [step for step in steps if str(step.get("uses", "")).startswith("actions/setup-node")]
    version = str(setup[0].get("with", {}).get("node-version", "")) if setup else ""
    if not _NODE_22.match(version):
        problems.append(f"Node 22 sin fijar: {version!r}")
    runs = [str(step.get("run") or "") for step in steps]
    positions = []
    for command in FRONTEND_COMMANDS:
        found = [index for index, run in enumerate(runs) if command in run]
        if not found:
            problems.append(f"falta el paso «{command}»")
        else:
            positions.append(found[0])
    if positions != sorted(positions):
        problems.append("los pasos no siguen el orden de TASK-412")
    for step in steps:
        run = str(step.get("run") or "")
        if run.startswith("npm ") and (step.get("if") or step.get("continue-on-error")):
            problems.append(f"«{run}» tiene condición o no bloquea")
        if "npm install" in run or ("npm ci" in run and "--ignore-scripts" not in run):
            problems.append(f"«{run}» instala sin bloqueo o con guiones de instalación")
    cleanup = [step for step in steps if "rm -f ~/.ssh/vigia_contracts" in str(step.get("run"))]
    if not cleanup or cleanup[0].get("if") != "always()":
        problems.append("la clave de despliegue no se borra con if: always()")
    secrets = set(re.findall(r"secrets\.(\w+)", yaml.safe_dump(job)))
    if secrets - {"VIGIA_CONTRACTS_DEPLOY_KEY"}:
        problems.append(f"secretos no permitidos: {sorted(secrets)}")
    return problems


def test_the_frontend_job_runs_every_check_of_task_412() -> None:
    assert frontend_job_problems((WORKFLOWS / "ci.yml").read_text(encoding="utf-8")) == []
    # Los guiones que corre existen y los umbrales de NFR-APP-46 están en la configuración.
    frontend = REPOSITORY / "frontend"
    scripts = json.loads((frontend / "package.json").read_text(encoding="utf-8"))["scripts"]
    assert scripts["size"] == "node scripts/checks/size.mjs"
    assert scripts["audit:check"] == "node scripts/audit.mjs"
    assert (frontend / "scripts" / "checks" / "licenses.mjs").is_file()
    vitest = (frontend / "vitest.config.ts").read_text(encoding="utf-8")
    for area in ("src/state/**", "src/ports/**", "src/ports/validation/**", "src/ports/errors/**"):
        assert f"'{area}': {{ lines: 80 }}" in vitest, area
    assert "'src/state/coverage/**': { lines: 80 }" in vitest


def _ci_with_frontend(change: Any) -> str:
    ci = _ci()
    change(ci["jobs"][FRONTEND_JOB])
    return yaml.safe_dump(ci, allow_unicode=True)


def _drop_step(command: str) -> Any:
    def change(job: dict[Any, Any]) -> None:
        job["steps"] = [step for step in job["steps"] if command not in str(step.get("run"))]

    return change


def _edit_step(command: str, **fields: Any) -> Any:
    def change(job: dict[Any, Any]) -> None:
        for step in job["steps"]:
            if command in str(step.get("run")):
                step.update(fields)

    return change


@pytest.mark.parametrize("command", FRONTEND_COMMANDS)
def test_dropping_a_frontend_step_is_detected(command: str) -> None:
    assert frontend_job_problems(_ci_with_frontend(_drop_step(command)))


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(_edit_step("npm run size", run="npm run build"), id="tamaño-antes"),
        pytest.param(_edit_step("npm run audit:check", **{"if": "false"}), id="audit-con-if"),
        pytest.param(
            _edit_step("npm test -- --coverage", **{"continue-on-error": True}),
            id="test-no-bloquea",
        ),
        pytest.param(_edit_step("npm ci --ignore-scripts", run="npm ci"), id="con-guiones"),
        pytest.param(_edit_step("rm -f ~/.ssh/vigia_contracts", **{"if": None}), id="clave-queda"),
        pytest.param(
            lambda job: job["steps"][1]["with"].update({"node-version": 22}), id="node-sin-fijar"
        ),
        pytest.param(
            lambda job: job["steps"][1]["with"].update({"node-version": "20.19.0"}), id="node-20"
        ),
        pytest.param(lambda job: job.update({"needs": ["rutas"]}), id="depende-de-rutas"),
        pytest.param(lambda job: job.update({"name": "frontend"}), id="nombre"),
        pytest.param(
            lambda job: job["steps"].append({"run": "echo ${{ secrets.OTRO }}"}), id="secreto"
        ),
    ],
)
def test_each_frontend_job_rule_fails_on_its_own(change: Any) -> None:
    assert frontend_job_problems(_ci_with_frontend(change))


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


def test_an_inline_mapping_is_read_as_yaml() -> None:
    """Seguimiento de VIG-94: ``- {uses: owner/action@v5}`` no se escapa del lint."""
    assert unpinned_uses("steps:\n  - {uses: owner/action@v5}\n")
    assert unpinned_uses("jobs: {a: {steps: [{uses: owner/action@main}]}}\n")
    assert unpinned_uses(f"steps:\n  - {{uses: owner/action@{_SHA}}}\n")  # sin comentario
    assert unpinned_uses(f"steps:\n  - {{uses: owner/action@{_SHA}}}  # v5.0.0\n") == []


def test_a_reusable_workflow_reference_is_checked() -> None:
    assert unpinned_uses("jobs:\n  a:\n    uses: owner/repo/.github/workflows/x.yml@main\n")


def test_id_token_is_the_only_write_permission_allowed() -> None:
    allowed = (
        "on: push\npermissions: {contents: read}\njobs: {a: {permissions: {id-token: write}}}\n"
    )
    assert permission_problems(allowed) == []
    for scope in ("contents", "actions", "packages", "deployments"):
        workflow = (
            "on: push\npermissions: {contents: read}\n"
            f"jobs: {{a: {{permissions: {{id-token: write, {scope}: write}}}}}}\n"
        )
        assert permission_problems(workflow), scope


_AWS_JOB = """\
on: workflow_dispatch
permissions: {{contents: read}}
jobs:
  a:
{extra}    runs-on: ubuntu-24.04
    steps:
      - run: {run}
"""
_GOOD_EXTRA = (
    "    if: vars.VIGIA_AWS_ENABLED == 'true'\n"
    "    environment: staging\n"
    "    permissions: {contents: read, id-token: write}\n"
)


@pytest.mark.parametrize(
    "run",
    [
        "python3 .github/scripts/aws_federation.py --role-arn x",
        "aws ecs wait services-stable",
        "$CDK deploy --all",
        "npx aws-cdk destroy --all",
        "$CDK diff -c environment=pilot",
        "python3 .github/scripts/staging.py residue --environment staging-1",
        "uv run python backend/tools/deploy_checks.py --environment pilot",
    ],
)
def test_an_ungated_aws_job_fails(run: str) -> None:
    assert aws_job_problems(_AWS_JOB.format(extra=_GOOD_EXTRA, run=run)) == []
    assert aws_job_problems(_AWS_JOB.format(extra="", run=run))


@pytest.mark.parametrize(
    "extra",
    [
        # Sin la variable.
        "    environment: staging\n    permissions: {contents: read, id-token: write}\n",
        # La variable en una disyunción: con la variable ausente, el trabajo corre igual.
        "    if: vars.VIGIA_AWS_ENABLED == 'true' || github.event_name == 'schedule'\n"
        "    environment: staging\n    permissions: {contents: read, id-token: write}\n",
        # Fuera de los entornos del rol.
        "    if: vars.VIGIA_AWS_ENABLED == 'true'\n"
        "    permissions: {contents: read, id-token: write}\n",
        "    if: vars.VIGIA_AWS_ENABLED == 'true'\n    environment: produccion\n"
        "    permissions: {contents: read, id-token: write}\n",
        # Sin el token OIDC.
        "    if: vars.VIGIA_AWS_ENABLED == 'true'\n    environment: staging\n",
    ],
)
def test_each_aws_rule_fails_on_its_own(extra: str) -> None:
    assert aws_job_problems(_AWS_JOB.format(extra=extra, run="aws sts get-caller-identity"))


def test_an_oidc_token_outside_an_aws_job_fails() -> None:
    workflow = _AWS_JOB.format(extra="    permissions: {id-token: write}\n", run="echo hola")
    assert aws_job_problems(workflow)


@pytest.mark.parametrize(
    "line",
    [
        "        env: {AWS_ACCESS_KEY_ID: AKIAEXAMPLE}",
        "          AWS_SECRET_ACCESS_KEY: ${{ secrets.CLAVE }}",
        "          aws-access-key-id: ${{ secrets.ID }}",
        "        run: echo ${{ secrets.AWS_DEPLOY }}",
    ],
)
def test_long_lived_aws_keys_fail(line: str) -> None:
    assert long_lived_key_problems(f"jobs:\n  a:\n    steps:\n{line}\n")


@pytest.mark.parametrize(
    "run",
    [
        "$CDK deploy -c environment=pilot vigia-compute",
        "python3 .github/scripts/staging.py run-task --environment pilot --task migrate",
        "uv run python backend/tools/deploy_checks.py --environment pilot",
        "aws elbv2 remove-trust-store-revocations --trust-store-arn x",
    ],
)
def test_changing_pilot_outside_the_pilot_environment_fails(run: str) -> None:
    staging = _AWS_JOB.format(extra=_GOOD_EXTRA, run=run)
    assert pilot_environment_problems(staging)
    in_pilot = staging.replace("environment: staging", "environment: pilot")
    assert pilot_environment_problems(in_pilot) == []


def test_a_pilot_diff_is_only_a_read() -> None:
    workflow = _AWS_JOB.format(extra=_GOOD_EXTRA, run="$CDK diff -c environment=pilot")
    assert pilot_environment_problems(workflow) == []


@pytest.mark.parametrize(
    "requirements",
    [
        "coverage>=7\n    --hash=sha256:" + "a" * 64 + "\n",
        "coverage==7.16.2\n",
        "coverage==7.16.2 \\\n    --hash=sha256:abc\n",
        "a==1 \\\n    --hash=sha256:" + "a" * 64 + "\nb==2\n",
    ],
)
def test_a_requirement_without_exact_version_or_hash_fails(requirements: str) -> None:
    assert requirement_problems(requirements)


def test_hashed_requirements_pass() -> None:
    text = "# comentario\na==1 \\\n    --hash=sha256:" + "a" * 64 + "\n    # via b\n"
    assert requirement_problems(text) == []


# U-04 (TASK-333): las listas de abajo son guardas de pertenencia, no un segundo reparto.
# Los comandos siguen seleccionando directorios completos y pytest-split conserva sus tiempos.
_LAZ_NIGHTLY = (
    *[
        ("resiliencia", f"tests/resilience/test_fs_laz_{number:02d}.py", "")
        for number in range(1, 13)
    ],
    ("resiliencia", "tests/resilience/test_dr_laz_01_02.py", ""),
    ("resiliencia", "tests/resilience/test_dr_laz_03_04.py", ""),
    ("bancos", "tests/benchmarks/test_nfr_laz_01_operations.py", ""),
    ("bancos", "tests/benchmarks/test_laz_validators_bench.py", ""),
    ("bancos", "tests/benchmarks/test_laz_evaluate_deadlines_bench.py", ""),
    ("carga", "tests/load/test_export_load.py", "integration and nightly"),
    ("volumetria", "tests/volumetry/test_laz_volumetry.py", "integration and nightly"),
)


def _suite_is_selected(job: dict[Any, Any], path: str, marker: str, profile: str) -> bool:
    return any(
        call.selects(path)
        and not call.excludes(path)
        and not call.options.get("-k")
        and not call.options.get("--ignore-glob")
        and call.marker == marker
        and call.profile == profile
        for step in job["steps"]
        if not step.get("if")
        for call in _pytest_commands(str(step.get("run") or ""))
    )


def test_ci_includes_laz_suites_and_loop_stack_in_existing_checks() -> None:
    assert coverage_problems((WORKFLOWS / "ci.yml").read_text("utf-8")) == []
    job = _ci()["jobs"]["infra"]
    assert job["defaults"]["run"]["working-directory"] == "infra"
    assert _suite_is_selected(job, "tests/test_loop_stack.py", "", "")


@pytest.mark.parametrize("suite", ["properties/laz", "isolation", "abuse", "examples"])
@pytest.mark.parametrize("job_id", ["backend-pruebas", "backend-integracion-grupos"])
def test_excluding_a_u04_suite_from_either_ci_marker_class_is_detected(
    suite: str, job_id: str
) -> None:
    ci = _ci()
    for step in ci["jobs"][job_id]["steps"]:
        if "coverage run -m pytest" in str(step.get("run")):
            step["run"] += f" --ignore=tests/{suite}"
    assert coverage_problems(yaml.safe_dump(ci))


def test_excluding_the_loop_stack_template_tests_is_detected() -> None:
    job = _ci()["jobs"]["infra"]
    for step in job["steps"]:
        if "pytest" in str(step.get("run")):
            step["run"] += " --ignore=tests/test_loop_stack.py"
    assert not _suite_is_selected(job, "tests/test_loop_stack.py", "", "")


@pytest.mark.parametrize("option", ["--ignore-glob=tests/*stack*", "--ignore-glob tests/*stack*"])
def test_excluding_the_loop_template_with_a_glob_is_also_detected(option: str) -> None:
    job = _ci()["jobs"]["infra"]
    for step in job["steps"]:
        if "pytest" in str(step.get("run")):
            step["run"] += f" {option}"
    assert not _suite_is_selected(job, "tests/test_loop_stack.py", "", "")


@pytest.mark.parametrize(("job_id", "path", "marker"), _LAZ_NIGHTLY)
def test_u04_nightly_suites_run_in_their_own_job_and_exclusion_is_detected(
    job_id: str, path: str, marker: str
) -> None:
    job = _workflow("nightly")["jobs"][job_id]
    assert (REPOSITORY / "backend" / path).is_file()
    assert _suite_is_selected(job, path, marker, "nightly")
    for step in job["steps"]:
        if "pytest" in str(step.get("run")):
            step["run"] = "\n".join(
                f"{line} --deselect={path}" if "pytest" in line else line
                for line in step["run"].splitlines()
            )
    assert not _suite_is_selected(job, path, marker, "nightly")


def test_long_nightly_suites_are_not_duplicated_in_the_general_jobs() -> None:
    jobs = _workflow("nightly")["jobs"]
    for job_id in ("propiedades", "integracion"):
        calls = _pytest_commands(_run_text(jobs[job_id]))
        assert calls
        for directory in ("benchmarks", "resilience", "volumetry", "load", "properties"):
            assert all(call.excludes(f"tests/{directory}") for call in calls)
    volume = _run_text(jobs["volumetria"])
    assert "--part all" not in volume and "--part laz" not in volume
    assert "--part nuc" in volume and "--part gob" in volume
    # Los auxiliares sin base de carga no desaparecen al moverlos desde «propiedades».
    # Volumetría solo tiene pruebas de integración: «not integration» recolectaba cero y fallaba.
    assert _suite_is_selected(jobs["carga"], "tests/load", "not integration", "nightly")


def test_each_nightly_volumetry_pytest_command_collects_tests() -> None:
    steps = _workflow("nightly")["jobs"]["volumetria"]["steps"]
    commands = [step["run"] for step in steps if "pytest" in str(step.get("run"))]
    assert commands
    for command in commands:
        tokens = shlex.split(command)
        arguments = tokens[tokens.index("pytest") + 1 :]
        answer = subprocess.run(
            [sys.executable, "-m", "pytest", *arguments, "--collect-only"],
            cwd=REPOSITORY / "backend",
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert answer.returncode == 0, answer.stdout + answer.stderr


def test_dependency_job_covers_the_new_u04_packages_without_a_narrow_inventory() -> None:
    run = _run_text(_ci()["jobs"]["dependencias"])
    assert "uv sync --frozen" in run
    assert "uv run --frozen python tools/check_licenses.py" in run
    lock = (REPOSITORY / "backend" / "uv.lock").read_text("utf-8")
    # simplemma era una hipótesis del diseño: no está instalada en este árbol. No se añade
    # una dependencia solo para satisfacer el inventario previsto de la planificación.
    for package in ("holidays", "tzdata", "anthropic"):
        assert f'name = "{package}"' in lock


@pytest.mark.parametrize("job_id", ["staging", "pilot"])
def test_release_deploys_loop_last_after_a_successful_isolated_diff(job_id: str) -> None:
    job = _workflow("release")["jobs"][job_id]
    assert AWS_GATE in job["if"]
    deploys = [step["run"] for step in job["steps"] if "$CDK deploy " in str(step.get("run"))]
    assert len(deploys) == (2 if job_id == "staging" else 1)
    for run in deploys:
        assert (
            run.index("$CDK diff --exclusively")
            < run.index("loop_diff.py")
            < run.index("$CDK deploy")
        )
        assert "--no-change-set --no-color" in run
        assert "|| true" not in run
        command = run[run.index("$CDK deploy") :].split("aws ecs", 1)[0].strip()
        assert command.endswith('"vigia-loop-$STAGING"' if job_id == "staging" else "vigia-loop")
        assert "vigia-datasets" not in command


def test_release_destroy_includes_loop_and_pilot_checks_keep_the_whole_alarm_window() -> None:
    jobs = _workflow("release")["jobs"]
    run = _run_text(jobs["destruir-staging"])
    assert '"vigia-loop-$STAGING"' in run
    assert "--watch-minutes 15" in _run_text(jobs["pilot"])
    run = _run_text(jobs["publicar"])
    assert "diff --exclusively vigia-loop" in run and "loop_diff.py --stack vigia-loop" in run
    assert "|| true" not in run


def test_u04_benchmarks_keep_the_fifty_percent_policy_without_changing_other_units() -> None:
    from tests.benchmarks.conftest import unit_regression_factor

    assert unit_regression_factor("laz_operation") == 1.5
    assert unit_regression_factor("gob_operation") == 1.2
    assert unit_regression_factor("nuc_operation") == 1.5
    baseline = json.loads(
        (REPOSITORY / "backend/tests/benchmarks/baseline.json").read_text("utf-8")
    )
    laz = {name: entry for name, entry in baseline["benchmarks"].items() if name.startswith("laz_")}
    assert len(laz) >= 17
    assert all(entry["regression_factor"] == 1.5 for entry in laz.values())
    assert _workflow("nightly")["jobs"]["bancos"]["name"] == (
        "bancos (regresión del 20 % en U-03 y del 50 % en U-02 y U-04)"
    )


@pytest.mark.parametrize("job_id", ["staging", "pilot"])
def test_real_deployments_fail_closed_until_u04_checks_have_an_owner_contract(
    tmp_path: Path, job_id: str
) -> None:
    job = _workflow("release")["jobs"][job_id]
    assert AWS_GATE in job["if"]
    assert not job.get("continue-on-error")
    guard = job["steps"][0]
    assert guard.get("id") == "contrato-u04"
    assert not guard.get("if") and not guard.get("continue-on-error")
    summary = tmp_path / "summary.md"
    answer = subprocess.run(
        ["/bin/bash", "-e", "-o", "pipefail", "-c", guard["run"]],
        env={"PATH": os.defpath, "GITHUB_STEP_SUMMARY": str(summary)},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert answer.returncode == 1, answer.stdout + answer.stderr
    message = "comprobaciones nº 14 y 15 pendientes de contrato del dueño: VIG-252"
    assert message in answer.stderr
    assert summary.read_text("utf-8").strip() == message


@pytest.mark.parametrize("synthesis", ["success", "failure"])
def test_the_real_release_summary_runs_without_aws_and_reports_failures_honestly(
    tmp_path: Path, synthesis: str
) -> None:
    # Solo el intérprete de las dos consultas jq es un doble: se ejecuta el run real del YAML.
    # No impone instalar jq (herramienta del runner de GitHub) para correr las pruebas Python.
    jq = tmp_path / "jq"
    jq.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "data = json.load(sys.stdin)\n"
        "if sys.argv[1:] == ['-r', r'to_entries[] | \"| \\(.key) | \\(.value.result) |\"']:\n"
        "    for key, value in data.items():\n"
        "        print(f'| {key} | {value[\"result\"]} |')\n"
        "elif sys.argv[1:] == ['-e', "
        '\'all(.[]; .result == "success" or .result == "skipped")\']:\n'
        "    ok = all(v['result'] in ('success', 'skipped') for v in data.values())\n"
        "    sys.exit(0 if ok else 1)\n"
        "else:\n"
        "    sys.exit(2)\n",
        encoding="utf-8",
    )
    jq.chmod(0o700)
    job = _workflow("release")["jobs"]["resumen"]
    report = tmp_path / "summary.md"
    results = {
        name: {"result": "skipped"}
        for name in ("publicar", "staging", "destruir-staging", "pilot", "etiqueta")
    }
    results.update({name: {"result": "success"} for name in ("preparar", "imagen")})
    results["sintesis"] = {"result": synthesis}
    answer = subprocess.run(
        ["/bin/bash", "-e", "-o", "pipefail", "-c", _run_text(job)],
        env={
            "PATH": f"{tmp_path}{os.pathsep}{os.defpath}",
            "GITHUB_STEP_SUMMARY": str(report),
            "GITHUB_SHA": "a" * 40,
            "VERSION": "0.4.0",
            "STAGING": "staging-7",
            "AWS_ENABLED": "false",
            "DRY_RUN": "true",
            "SOAK": "false",
            "RESULTADOS": json.dumps(results),
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert answer.returncode == (0 if synthesis == "success" else 1), answer.stderr
    text = report.read_text("utf-8")
    assert "Sin AWS (A-47)" in text and "vigia-loop" in text
    assert f"| sintesis | {synthesis} |" in text
    assert "Se omitieron ECR" in text
    assert "comprobaciones nº 14 y 15 pendientes de contrato del dueño: VIG-252" in text
    assert "No se ejecutaron las comprobaciones nº 14" in text
    assert "no son comprobaciones superadas" in text


def test_the_route_filter_fails_closed_and_never_gates_the_security_jobs() -> None:
    """Retro 29, propuesta 1 (dueño, 2026-10-24): un PR solo de frontend/ o docs/ salta los
    trabajos de backend. Solo un ``backend=false`` explícito salta algo: si «rutas» falla (salida
    vacía), todo corre o el check obligatorio queda en rojo. En un push a main corre todo.
    Secretos, dependencias, imagen y flujos no dependen del filtro."""
    jobs = _ci()["jobs"]
    rutas = jobs["rutas"]
    assert rutas["name"] == "rutas del cambio (frontend o backend)"
    assert rutas["outputs"] == {"backend": "${{ steps.rutas.outputs.backend }}"}
    run = _job_text(rutas)
    assert 'if [ "$EVENTO" != pull_request ]; then' in run and "backend=true" in run
    assert r"grep -qvE '^(frontend/|docs/|README\.md$|CHANGELOG\.md$)'" in run
    assert rutas["steps"][0]["with"]["fetch-depth"] == 0
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    condiciones = re.findall(r"needs\.rutas\.outputs\.backend\s*(==|!=)\s*'(\w+)'", text)
    assert condiciones and all(valor == "false" for _, valor in condiciones)
    for job_id in ("secretos", "dependencias", "imagen", "flujos", "frontend"):
        assert "needs" not in jobs[job_id], job_id
        assert "rutas" not in str(jobs[job_id]), job_id
    pesados = (
        "backend-lint",
        "backend-pruebas",
        "backend-integracion-grupos",
        "arranque-n-1",
        "infra",
    )
    for job_id in pesados:
        assert jobs[job_id]["needs"] == ["rutas"], job_id
        condicion = str(jobs[job_id]["if"])
        assert condicion.endswith("&& needs.rutas.outputs.backend != 'false'"), job_id


# --- U-05: frontend-nightly y artefactos de la aplicación en el release (VIG-337, TASK-456) -----

FRONTEND_NIGHTLY_JOB = "frontend-nightly"
FRONTEND_NIGHTLY_NAME = (
    "frontend nightly (propiedades, resiliencia, abuso, recorridos y rendimiento)"
)
FRONTEND_NIGHTLY_COMMANDS = (
    "uv sync --frozen",
    "npm ci --ignore-scripts",
    "rm -f ~/.ssh/vigia_contracts",
    "npx playwright install --with-deps chromium firefox",
    "node scripts/install_webkit.mjs",
    "npm run test:nightly -- --exclude",
    "npm run test:nightly -- tests/resilience",
    "npm run test:nightly -- tests/abuse",
    "npm run build",
    "xvfb-run -a npm run e2e",
    "xvfb-run -a npm run lighthouse",
)
"""Pasos de TASK-456, en este orden: instalación, vitest nightly, construcción, Playwright y
Lighthouse (tech-stack-decisions §5 de U-05)."""
FRONTEND_NIGHTLY_ARTIFACTS = {
    "informe-frontend-fs-app": "frontend/test-results/fs-app-*.json",
    "frontend-playwright": "frontend/test-results/",
    "informe-frontend-lighthouse": "frontend/.lighthouseci/",
}
"""Artefactos de PAT-APP-RES-08 (90 días: la retención la comprueba la prueba del nightly)."""
_VITEST_NIGHTLY_EXCLUSIONS = frozenset({"tests/resilience/**", "tests/abuse/**"})
"""Lo único que el vitest nightly general deja fuera: resiliencia y abuso corren en su paso."""


def _upload(step: dict[Any, Any]) -> str | None:
    if not str(step.get("uses", "")).startswith("actions/upload-artifact"):
        return None
    return str((step.get("with") or {}).get("name"))


def frontend_nightly_job_problems(text: str) -> list[str]:
    """VIG-337: el trabajo ``frontend-nightly`` de ``nightly.yml`` corre vitest con el perfil
    nightly (sin dejar fuera más que resiliencia y abuso, que corren aparte), **todos** los
    ``tests/e2e/*.spec.ts`` en los seis proyectos (sin filtros de archivo, proyecto ni título),
    y Lighthouse; sube el informe FS-APP antes de que Playwright vacíe ``test-results/``; la
    clave de despliegue solo existe mientras se instala; y ``informe`` lo espera."""
    jobs = _load(text).get("jobs") or {}
    job = jobs.get(FRONTEND_NIGHTLY_JOB)
    if not isinstance(job, dict):
        return ["no hay trabajo frontend-nightly"]
    problems = []
    if job.get("name") != FRONTEND_NIGHTLY_NAME:
        problems.append(f"nombre del trabajo: {job.get('name')!r}")
    if job.get("defaults", {}).get("run", {}).get("working-directory") != "frontend":
        problems.append("los pasos no corren en frontend/")
    if job.get("continue-on-error") or "if" in job:
        problems.append("el trabajo tiene condición o no bloquea")
    if FRONTEND_NIGHTLY_JOB not in ((jobs.get("informe") or {}).get("needs") or []):
        problems.append("el trabajo informe no espera a frontend-nightly")
    steps = job.get("steps") or []
    setup = [step for step in steps if str(step.get("uses", "")).startswith("actions/setup-node")]
    version = str(setup[0].get("with", {}).get("node-version", "")) if setup else ""
    if not _NODE_22.match(version):
        problems.append(f"Node 22 sin fijar: {version!r}")
    runs = [str(step.get("run") or "") for step in steps]
    positions = []
    for command in FRONTEND_NIGHTLY_COMMANDS:
        found = [index for index, run in enumerate(runs) if command in run]
        if not found:
            problems.append(f"falta el paso «{command}»")
        else:
            positions.append(found[0])
    if positions != sorted(positions):
        problems.append("los pasos no siguen el orden de TASK-456")
    for step in steps:
        run = str(step.get("run") or "")
        if step.get("continue-on-error"):
            problems.append(f"«{run or step.get('uses')}» no bloquea")
        if "npm install" in run or ("npm ci" in run and "--ignore-scripts" not in run):
            problems.append(f"«{run}» instala sin bloqueo o con guiones de instalación")
        for line in (line.strip() for line in run.splitlines()):
            if line.startswith("npm run test:nightly -- --exclude"):
                tokens = shlex.split(line)[4:]
                pairs = list(zip(tokens[::2], tokens[1::2], strict=False))
                excluded = {value for flag, value in pairs if flag == "--exclude"}
                if (
                    len(tokens) % 2
                    or any(flag != "--exclude" for flag, _ in pairs)
                    or (excluded != _VITEST_NIGHTLY_EXCLUSIONS)
                ):
                    problems.append(f"vitest nightly deja fuera algo más: «{line}»")
            if "npm run e2e" in line and line != "xvfb-run -a npm run e2e":
                problems.append(f"Playwright filtra especificaciones o proyectos: «{line}»")
    cleanup = [step for step in steps if "rm -f ~/.ssh/vigia_contracts" in str(step.get("run"))]
    if not cleanup or cleanup[0].get("if") != "always()":
        problems.append("la clave de despliegue no se borra con if: always()")
    uploads = {name: index for index, step in enumerate(steps) if (name := _upload(step))}
    for name, path in FRONTEND_NIGHTLY_ARTIFACTS.items():
        if name not in uploads:
            problems.append(f"no sube el artefacto {name}")
            continue
        if str(steps[uploads[name]]["with"].get("path")) != path:
            problems.append(f"el artefacto {name} no sube {path}")
        if "!cancelled()" not in str(steps[uploads[name]].get("if")):
            problems.append(f"el artefacto {name} no se sube cuando falla una suite")
    e2e = [index for index, run in enumerate(runs) if "npm run e2e" in run]
    if "informe-frontend-fs-app" in uploads and e2e and uploads["informe-frontend-fs-app"] > e2e[0]:
        problems.append("el informe FS-APP se sube después de que Playwright vacíe test-results/")
    secrets = set(re.findall(r"secrets\.(\w+)", yaml.safe_dump(job)))
    if secrets - {"VIGIA_CONTRACTS_DEPLOY_KEY"}:
        problems.append(f"secretos no permitidos: {sorted(secrets)}")
    return problems


def test_the_frontend_nightly_job_runs_every_suite_of_task_456() -> None:
    nightly = (WORKFLOWS / "nightly.yml").read_text(encoding="utf-8")
    assert frontend_nightly_job_problems(nightly) == []
    # Los guiones que corre existen con lo que el diseño pide: seis proyectos sobre tests/e2e.
    frontend = REPOSITORY / "frontend"
    scripts = json.loads((frontend / "package.json").read_text(encoding="utf-8"))["scripts"]
    assert scripts["test:nightly"] == "vitest run --mode nightly"
    assert scripts["e2e"] == "playwright test"
    assert scripts["lighthouse"] == "lhci autorun --config=lighthouserc.json"
    playwright = (frontend / "playwright.config.ts").read_text(encoding="utf-8")
    assert "testDir: './tests/e2e'" in playwright
    assert "['chromium', 'firefox', 'webkit']" in playwright
    assert "name: 'desktop'" in playwright and "name: 'tablet'" in playwright


def _nightly_with_frontend(change: Any) -> str:
    nightly = _workflow("nightly")
    change(nightly["jobs"])
    return str(yaml.safe_dump(nightly, allow_unicode=True))


def _frontend_step(command: str, **fields: Any) -> Any:
    def change(jobs: dict[Any, Any]) -> None:
        for step in jobs[FRONTEND_NIGHTLY_JOB]["steps"]:
            if command in str(step.get("run")) or command == _upload(step):
                step.update(fields)

    return change


def _drop_frontend_step(command: str) -> Any:
    def change(jobs: dict[Any, Any]) -> None:
        job = jobs[FRONTEND_NIGHTLY_JOB]
        job["steps"] = [
            step
            for step in job["steps"]
            if command not in str(step.get("run")) and command != _upload(step)
        ]

    return change


def _move_fs_app_report_after_playwright(jobs: dict[Any, Any]) -> None:
    steps = jobs[FRONTEND_NIGHTLY_JOB]["steps"]
    (report,) = [step for step in steps if _upload(step) == "informe-frontend-fs-app"]
    steps.remove(report)
    e2e = next(i for i, step in enumerate(steps) if "npm run e2e" in str(step.get("run")))
    steps.insert(e2e + 1, report)


@pytest.mark.parametrize("command", [*FRONTEND_NIGHTLY_COMMANDS, *FRONTEND_NIGHTLY_ARTIFACTS])
def test_dropping_a_frontend_nightly_step_is_detected(command: str) -> None:
    assert frontend_nightly_job_problems(_nightly_with_frontend(_drop_frontend_step(command)))


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(_move_fs_app_report_after_playwright, id="fs-app-tras-playwright"),
        pytest.param(
            _frontend_step("npm run e2e", run="xvfb-run -a npm run e2e -- tests/e2e/smoke.spec.ts"),
            id="e2e-un-archivo",
        ),
        pytest.param(
            _frontend_step("npm run e2e", run="xvfb-run -a npm run e2e -- --project=webkit-tablet"),
            id="e2e-un-proyecto",
        ),
        pytest.param(
            _frontend_step(
                "npm run test:nightly -- --exclude",
                run="npm run test:nightly -- --exclude 'tests/resilience/**' "
                "--exclude 'tests/abuse/**' --exclude 'tests/properties/**'",
            ),
            id="vitest-sin-propiedades",
        ),
        pytest.param(
            _frontend_step(
                "npm run test:nightly -- --exclude",
                run="npm run test:nightly -- --exclude 'tests/resilience/**' "
                "--exclude 'tests/abuse/**' tests/unit",
            ),
            id="vitest-solo-una-carpeta",
        ),
        pytest.param(
            _frontend_step("npm run lighthouse", **{"continue-on-error": True}),
            id="lighthouse-no-bloquea",
        ),
        pytest.param(
            _frontend_step(
                "informe-frontend-fs-app",
                **{"with": {"name": "informe-frontend-fs-app", "path": "frontend/test-results/"}},
            ),
            id="fs-app-otra-ruta",
        ),
        pytest.param(
            _frontend_step("frontend-playwright", **{"if": "success()"}), id="trazas-solo-en-verde"
        ),
        pytest.param(_frontend_step("npm ci --ignore-scripts", run="npm ci"), id="con-guiones"),
        pytest.param(
            _frontend_step("rm -f ~/.ssh/vigia_contracts", **{"if": None}), id="clave-queda"
        ),
        pytest.param(
            lambda jobs: jobs[FRONTEND_NIGHTLY_JOB]["steps"][1]["with"].update(
                {"node-version": "20.19.0"}
            ),
            id="node-20",
        ),
        pytest.param(
            lambda jobs: jobs[FRONTEND_NIGHTLY_JOB].update({"name": "frontend nightly"}),
            id="nombre",
        ),
        pytest.param(
            lambda jobs: jobs[FRONTEND_NIGHTLY_JOB].update({"if": "false"}), id="trabajo-con-if"
        ),
        pytest.param(
            lambda jobs: jobs["informe"]["needs"].remove(FRONTEND_NIGHTLY_JOB),
            id="informe-no-lo-espera",
        ),
        pytest.param(
            lambda jobs: jobs[FRONTEND_NIGHTLY_JOB]["steps"].append(
                {"run": "echo ${{ secrets.OTRO }}"}
            ),
            id="secreto",
        ),
    ],
)
def test_each_frontend_nightly_rule_fails_on_its_own(change: Any) -> None:
    assert frontend_nightly_job_problems(_nightly_with_frontend(change))


_BUILD_ARGUMENT = re.compile(r"--build-arg\s+(\S+)")
RELEASE_BUILD_ARGUMENTS = frozenset({'APP_VERSION="$VERSION"'})
"""Único argumento de construcción del release: la versión X.Y.Z validada en «preparar»."""


def release_build_argument_problems(run: str) -> list[str]:
    """La clave de despliegue nunca viaja como argumento: solo ``APP_VERSION`` de la release, y en
    cada ``docker buildx build`` (la imagen y su etapa frontend-build salen de la misma versión)."""
    problems = [
        f"argumento de construcción no permitido: {argument}"
        for argument in _BUILD_ARGUMENT.findall(run)
        if argument not in RELEASE_BUILD_ARGUMENTS
    ]
    for line in run.splitlines():
        if "docker buildx build" in line and '--build-arg APP_VERSION="$VERSION"' not in line:
            problems.append(f"construcción sin APP_VERSION de la release: «{line.strip()}»")
    return problems


RELEASE_APP_COMMANDS = (
    "--target frontend-build -t vigia-frontend-build:release",
    'diff -r "$APP/dist" "$APP/static"',
    'test -z "$(find "$APP/static" -name \'*.map\')"',
    '"$PUBLICADA" != "$VERSION"',
    'cp "$APP/dist/version.json" "$RUNNER_TEMP/release/version.json"',
    'xargs -0 sha256sum) > "$RUNNER_TEMP/release/frontend-dist.sha256"',
    '"$RUNNER_TEMP/release/frontend-sourcemaps.tar.gz"',
    "docker run --rm --network none vigia-frontend-build:release npm run --silent sbom",
    "docker run --rm --network none vigia-frontend-build:release npm run --silent size",
)
"""infrastructure-design §4 de U-05: mapas de fuente, CycloneDX de npm, SHA-256 de ``dist/``,
``version.json`` e informe de tamaño, sacados de la etapa y comprobados contra la imagen."""


def release_frontend_problems(text: str) -> list[str]:
    """VIG-337: el trabajo ``imagen`` deja los artefactos de la aplicación en ``release/`` antes de
    subirlo (los adjunta ``etiqueta`` al release, no a la imagen); ``staging`` instala Node,
    ``npm ci`` y Chromium antes de las comprobaciones (la nº 8 los necesita) y las corre con Xvfb;
    el resumen lo dice sin AWS."""
    jobs = _load(text).get("jobs") or {}
    problems = []
    image = jobs.get("imagen") or {}
    steps = image.get("steps") or []
    run = _run_text(image)
    problems += release_build_argument_problems(run)
    for command in RELEASE_APP_COMMANDS:
        if command not in run:
            problems.append(f"imagen: falta «{command}»")
    app = [index for index, step in enumerate(steps) if step.get("id") == "aplicacion"]
    upload = [index for index, step in enumerate(steps) if _upload(step) == "release"]
    if not app or not upload or app[0] > upload[0]:
        problems.append("imagen: los artefactos de la aplicación no van en el artefacto release")
    elif steps[app[0]].get("if") or steps[app[0]].get("continue-on-error"):
        problems.append("imagen: los artefactos de la aplicación tienen condición o no bloquean")
    outputs = image.get("outputs") or {}
    if outputs.get("dist-sha256") != "${{ steps.aplicacion.outputs.dist-sha256 }}":
        problems.append("imagen: no publica el SHA-256 de dist/")
    tag = _run_text(jobs.get("etiqueta") or {})
    if '"$RUNNER_TEMP"/release/*' not in tag or "$DIST" not in tag:
        problems.append("etiqueta: el release no adjunta los artefactos de la aplicación")
    staging_steps = (jobs.get("staging") or {}).get("steps") or []
    staging_runs = [str(step.get("run") or "") for step in staging_steps]
    setup = [
        index
        for index, step in enumerate(staging_steps)
        if str(step.get("uses", "")).startswith("actions/setup-node")
        and _NODE_22.match(str(step.get("with", {}).get("node-version", "")))
    ]

    def first(command: str) -> int:
        found = [index for index, run in enumerate(staging_runs) if command in run]
        return found[0] if found else -1

    order = [
        setup[0] if setup else -1,
        first("npm ci --ignore-scripts"),
        first("rm -f ~/.ssh/vigia_contracts"),
        first("npx playwright install --with-deps chromium"),
        first("aws_federation.py"),
        first("xvfb-run -a uv run --no-project --python 3.12 python backend/tools/deploy_checks"),
    ]
    if -1 in order or order != sorted(order):
        problems.append(
            "staging: Node, npm ci, borrado de la clave, Chromium, federación y comprobaciones "
            f"no van en ese orden: {order}"
        )
    for step in staging_steps:
        command = str(step.get("run") or "")
        installs = "npm ci" in command or "playwright install" in command
        if (installs or "> ~/.ssh/vigia_contracts" in command) and (
            step.get("if") or step.get("continue-on-error")
        ):
            problems.append(f"staging: «{command.splitlines()[0]}» tiene condición o no bloquea")
        if "npm ci" in command and "--ignore-scripts" not in command:
            problems.append(f"staging: «{command}» instala con guiones de instalación")
    cleanup = [s for s in staging_steps if "rm -f ~/.ssh/vigia_contracts" in str(s.get("run"))]
    if not cleanup or cleanup[0].get("if") != "always()":
        problems.append("staging: la clave de despliegue no se borra con if: always()")
    if "artefactos de la aplicación" not in _run_text(jobs.get("resumen") or {}):
        problems.append("resumen: no dice que se produjeron los artefactos de la aplicación")
    return problems


def test_the_release_produces_the_app_artifacts_and_installs_playwright_for_check_8() -> None:
    release = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert release_frontend_problems(release) == []
    package = json.loads((REPOSITORY / "frontend" / "package.json").read_text(encoding="utf-8"))
    assert package["scripts"]["sbom"] == "cyclonedx-npm --omit dev --output-reproducible --validate"
    assert "@cyclonedx/cyclonedx-npm" in package["devDependencies"]


def _release_with(change: Any) -> str:
    release = _workflow("release")
    change(release["jobs"])
    return str(yaml.safe_dump(release, allow_unicode=True))


def _edit_release_run(job_id: str, old: str, new: str) -> Any:
    def change(jobs: dict[Any, Any]) -> None:
        for step in jobs[job_id]["steps"]:
            if old in str(step.get("run")):
                step["run"] = str(step["run"]).replace(old, new)

    return change


def _release_step(job_id: str, command: str, **fields: Any) -> Any:
    def change(jobs: dict[Any, Any]) -> None:
        for step in jobs[job_id]["steps"]:
            if command in str(step.get("run")) or command == step.get("id"):
                step.update(fields)

    return change


def _move_app_step_after_upload(jobs: dict[Any, Any]) -> None:
    steps = jobs["imagen"]["steps"]
    (app,) = [step for step in steps if step.get("id") == "aplicacion"]
    steps.remove(app)
    steps.append(app)


def _install_chromium_after_checks(jobs: dict[Any, Any]) -> None:
    steps = jobs["staging"]["steps"]
    (chromium,) = [step for step in steps if "playwright install" in str(step.get("run"))]
    steps.remove(chromium)
    checks = next(i for i, step in enumerate(steps) if "deploy_checks.py" in str(step.get("run")))
    steps.insert(checks + 1, chromium)


def _drop_setup_node(jobs: dict[Any, Any]) -> None:
    jobs["staging"]["steps"] = [
        step
        for step in jobs["staging"]["steps"]
        if not str(step.get("uses", "")).startswith("actions/setup-node")
    ]


@pytest.mark.parametrize("command", RELEASE_APP_COMMANDS)
def test_dropping_a_release_app_command_is_detected(command: str) -> None:
    assert release_frontend_problems(_release_with(_edit_release_run("imagen", command, "true")))


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(
            _edit_release_run(
                "imagen", '--build-arg APP_VERSION="$VERSION"', '--build-arg CLAVE="$CLAVE"'
            ),
            id="clave-como-argumento",
        ),
        pytest.param(
            _edit_release_run(
                "imagen",
                '--build-arg APP_VERSION="$VERSION" --target frontend-build',
                "--target frontend-build",
            ),
            id="etapa-sin-version",
        ),
        pytest.param(_move_app_step_after_upload, id="aplicacion-tras-subir"),
        pytest.param(
            _release_step("imagen", "aplicacion", **{"continue-on-error": True}),
            id="aplicacion-no-bloquea",
        ),
        pytest.param(
            lambda jobs: jobs["imagen"]["outputs"].pop("dist-sha256"), id="sin-hash-de-dist"
        ),
        pytest.param(
            _edit_release_run(
                "etiqueta", '"$RUNNER_TEMP"/release/*', '"$RUNNER_TEMP"/release/sbom-*'
            ),
            id="etiqueta-sin-artefactos",
        ),
        pytest.param(_drop_setup_node, id="staging-sin-node"),
        pytest.param(
            _release_step("staging", "npm ci --ignore-scripts", **{"if": "inputs.soak"}),
            id="npm-solo-con-soak",
        ),
        pytest.param(
            _release_step("staging", "> ~/.ssh/vigia_contracts", **{"if": "inputs.soak"}),
            id="clave-solo-con-soak",
        ),
        pytest.param(
            _edit_release_run("staging", "npm ci --ignore-scripts", "npm ci"), id="con-guiones"
        ),
        pytest.param(_install_chromium_after_checks, id="chromium-tras-comprobaciones"),
        pytest.param(
            _edit_release_run("staging", "xvfb-run -a uv run", "uv run"), id="n8-sin-pantalla"
        ),
        pytest.param(
            _release_step("staging", "rm -f ~/.ssh/vigia_contracts", **{"if": None}),
            id="clave-queda",
        ),
        pytest.param(
            _edit_release_run("resumen", "artefactos de la aplicación", "otros"),
            id="resumen-calla",
        ),
    ],
)
def test_each_release_app_rule_fails_on_its_own(change: Any) -> None:
    assert release_frontend_problems(_release_with(change))
