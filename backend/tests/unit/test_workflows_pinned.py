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
_ALLOWED_WRITE = frozenset({"id-token"})
"""Único permiso de escritura: el token OIDC de la federación con AWS (SECURITY-10)."""
FIRST_PARTY_ACTIONS = frozenset(
    {
        "actions/checkout",
        "astral-sh/setup-uv",
        "actions/upload-artifact",
        "actions/download-artifact",
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
    assert "ssh-add -q -" in run and "> ~/.ssh" not in run and "--build-arg" not in run
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


def test_staging_is_destroyed_always_and_checked_for_residue() -> None:
    release = _workflow("release")
    job = release["jobs"]["destruir-staging"]
    assert str(job["if"]).startswith("always() && ")
    assert "staging" in job["needs"]
    run = _run_text(job)
    assert _position(run, '$CDK destroy --all --force -c environment="$STAGING"') < _position(
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
    assert "--ignore=tests/conformance" in _run_text(jobs["propiedades-integracion"])
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
