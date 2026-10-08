"""Prueba de arranque de una imagen contra el esquema de otra (TASK-143; NFR-NUC-14, RESILIENCY-04).

La reversión es volver a desplegar la imagen anterior, que arranca sobre el esquema ya migrado.
Esta orden lo comprueba con Docker, igual en local que en el trabajo «arranque N-1» de ``ci.yml``:

1. PostgreSQL 16 (la imagen de ``docker-compose.yml``, por digest) en una red propia.
2. ``alembic upgrade head`` con ``--migrate-image`` (N): la misma orden que la tarea
   ``vigia-migrate``, con las variables locales de ``shared.migration_credentials``.
3. ``vigia-api`` de ``--boot-image`` (N-1) con ``tools/image_boot.py`` de su misma versión,
   montado de solo lectura: raíz de solo lectura, ``/tmp`` aparte, sin capacidades y con el
   usuario de la imagen. Debe llegar a ``/health/ready`` 200 dentro de ``--timeout`` y terminar
   con 0 tras ``SIGTERM``.

Con ``--migrate-image`` igual a ``--boot-image`` es la prueba de arranque de la propia imagen, y
añade un paso:

4. ``tools/image_render.py`` de ese árbol, montado de solo lectura, genera dentro de la imagen el
   documento de un acta sintética con la raíz de solo lectura, ``/tmp`` aparte, sin
   capacidades y **sin red** (``--network none``): fuentes empaquetadas presentes, ninguna
   fuente del sistema, PDF completo y solo las fuentes pedidas (TASK-217, NFR-GOB-32).
   ``--render-script`` lo fuerza con otro guion y ``--no-render`` lo omite. La imagen anterior
   (N-1) no lo corre: su código puede no tener el documento.

Las contraseñas de la base son aleatorias en cada ejecución. Los contenedores y la red se borran
al terminar, también si algo falla.

Uso::

    uv run python tools/run_boot_check.py --migrate-image IMG_N --boot-image IMG_N1 \\
        [--boot-script RUTA] [--render-script RUTA | --no-render] [--platform linux/arm64] \\
        [--timeout 240]

Termina en 0 si la imagen arranca, en 1 si no (migración fallida, nunca ``ready``, sale antes o
no para con 0) y en 2 si Docker o PostgreSQL no están disponibles.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

__all__ = ["BootPlan", "main"]

POSTGRES_IMAGE: Final = (
    "postgres:16@sha256:1a6ab3f5345eb6dbe04a1349529caabdb0ab09293a09590fad07b2246bfa4b54"
)
"""La de ``docker-compose.yml`` (``tests/unit/test_local_environment.py`` las compara)."""
DEFAULT_BOOT_SCRIPT: Final = Path(__file__).resolve().parent / "image_boot.py"
BOOT_SCRIPT_MOUNT: Final = "/boot/image_boot.py"
DEFAULT_RENDER_SCRIPT: Final = Path(__file__).resolve().parent / "image_render.py"
RENDER_SCRIPT_MOUNT: Final = "/boot/image_render.py"
API_PORT: Final = 8000
TMP_MOUNT: Final = "/tmp:rw,nosuid,nodev"  # noqa: S108 - punto de montaje, no un archivo
CONTAINER_HARDENING: Final = ("--read-only", "--tmpfs", TMP_MOUNT, "--cap-drop", "ALL")
"""Lo que hará la tarea en Fargate: raíz de solo lectura, ``/tmp`` aparte, sin capacidades."""


class BootCheckError(Exception):
    """Docker o PostgreSQL no están disponibles (código 2)."""


@dataclass(frozen=True, slots=True)
class BootPlan:
    """Nombres y órdenes de una ejecución (sin efectos: se pueden comprobar en pruebas)."""

    migrate_image: str
    boot_image: str
    boot_script: Path
    platform: str | None = None
    render_script: Path | None = None
    """``tools/image_render.py`` para el render sin red; ``None``, sin ese paso."""
    suffix: str = field(default_factory=lambda: secrets.token_hex(4))
    owner_password: str = field(default_factory=lambda: secrets.token_urlsafe(18), repr=False)
    app_password: str = field(default_factory=lambda: secrets.token_urlsafe(18), repr=False)
    migrate_password: str = field(default_factory=lambda: secrets.token_urlsafe(18), repr=False)

    @property
    def network(self) -> str:
        return f"vigia-boot-{self.suffix}"

    @property
    def database(self) -> str:
        return f"vigia-boot-db-{self.suffix}"

    @property
    def api(self) -> str:
        return f"vigia-boot-api-{self.suffix}"

    def _platform(self) -> list[str]:
        return ["--platform", self.platform] if self.platform else []

    def postgres_command(self) -> list[str]:
        return [
            "docker", "run", "--detach", "--name", self.database, "--network", self.network,
            "--env", "POSTGRES_USER=vigia", "--env", f"POSTGRES_PASSWORD={self.owner_password}",
            "--env", "POSTGRES_DB=vigia", "--env", "TZ=UTC", POSTGRES_IMAGE,
        ]  # fmt: skip

    def migrate_command(self) -> list[str]:
        return [
            "docker", "run", "--rm", *self._platform(), *CONTAINER_HARDENING,
            "--network", self.network,
            "--env", f"PGHOST={self.database}", "--env", "PGPORT=5432",
            "--env", "PGUSER=vigia", "--env", f"PGPASSWORD={self.owner_password}",
            "--env", "PGDATABASE=vigia", "--env", "PGSSLMODE=disable",
            "--env", f"VIGIA_DB_APP_PASSWORD={self.app_password}",
            "--env", f"VIGIA_DB_MIGRATE_PASSWORD={self.migrate_password}",
            self.migrate_image, "alembic", "upgrade", "head",
        ]  # fmt: skip

    def boot_command(self) -> list[str]:
        url = f"postgresql+asyncpg://vigia_app:{self.app_password}@{self.database}:5432/vigia"
        return [
            "docker", "run", "--detach", "--name", self.api, *self._platform(),
            *CONTAINER_HARDENING, "--network", self.network,
            "--publish", f"127.0.0.1::{API_PORT}",
            "--volume", f"{self.boot_script.resolve()}:{BOOT_SCRIPT_MOUNT}:ro",
            "--env", f"VIGIA_BOOT_DATABASE_URL={url}",
            self.boot_image, "python", BOOT_SCRIPT_MOUNT,
        ]  # fmt: skip

    def render_command(self) -> list[str]:
        """El documento del acta dentro de ``boot_image``, sin red (``--network none``)."""
        if self.render_script is None:
            raise ValueError("el plan no tiene guion de render")
        return [
            "docker", "run", "--rm", *self._platform(), *CONTAINER_HARDENING,
            "--network", "none",
            "--volume", f"{self.render_script.resolve()}:{RENDER_SCRIPT_MOUNT}:ro",
            self.boot_image, "python", RENDER_SCRIPT_MOUNT,
        ]  # fmt: skip


def _execute(command: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """``command`` empieza por ``docker``; se ejecuta con la ruta absoluta de la orden."""
    docker = shutil.which("docker")
    if docker is None:
        raise BootCheckError("no se encontró la orden docker")
    try:
        return subprocess.run(
            [docker, *command[1:]], capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        raise BootCheckError(f"«{' '.join(command[:3])}…» superó {timeout:.0f} s") from None


def _docker(command: Sequence[str], *, timeout: float) -> str:
    completed = _execute(command, timeout=timeout)
    if completed.returncode != 0:
        raise BootCheckError(
            f"«{' '.join(command[:3])}…» terminó con {completed.returncode}: "
            f"{completed.stderr.strip()[-2000:]}"
        )
    return completed.stdout


def _wait_postgres(plan: BootPlan, deadline_seconds: float) -> None:
    # Por TCP: el servidor temporal de la inicialización solo escucha en el socket local.
    probe = ["docker", "exec", plan.database, "pg_isready", "--host=127.0.0.1", "--username=vigia"]
    limit = time.monotonic() + deadline_seconds  # noqa: TID251 - herramienta de CI
    while time.monotonic() < limit:  # noqa: TID251 - herramienta de CI
        if _execute(probe, timeout=30).returncode == 0:
            return
        time.sleep(1)
    raise BootCheckError(f"PostgreSQL no quedó listo en {deadline_seconds:.0f} s")


def _status(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - 127.0.0.1
            status: int = response.status
            return status
    except urllib.error.HTTPError as error:
        return error.code
    except (urllib.error.URLError, OSError):
        return 0


def _container_state(name: str) -> dict[str, object]:
    raw = _docker(["docker", "inspect", "--format", "{{json .State}}", name], timeout=30)
    state: dict[str, object] = json.loads(raw)
    return state


def _boot(plan: BootPlan, timeout: float) -> tuple[bool, str]:
    _docker(plan.boot_command(), timeout=120)
    mapping = _docker(["docker", "port", plan.api, f"{API_PORT}/tcp"], timeout=30)
    base = f"http://{mapping.splitlines()[0].strip()}"
    limit = time.monotonic() + timeout  # noqa: TID251 - herramienta de CI
    ready = 0
    while time.monotonic() < limit:  # noqa: TID251 - herramienta de CI
        state = _container_state(plan.api)
        if not state.get("Running"):
            return (
                False,
                f"el proceso terminó antes de quedar listo (código {state.get('ExitCode')})",
            )
        ready = _status(f"{base}/health/ready")
        if ready == 200:
            break
        time.sleep(2)
    else:
        return False, f"/health/ready nunca respondió 200 en {timeout:.0f} s (último: {ready})"
    live = _status(f"{base}/health/live")
    if live != 200:
        return False, f"/health/live respondió {live}"
    _docker(["docker", "stop", "--time", "30", plan.api], timeout=60)
    code = _container_state(plan.api).get("ExitCode")
    if code != 0:
        return False, f"tras SIGTERM el proceso salió con {code}, no con 0"
    return True, "/health/live y /health/ready 200; parada ordenada con 0"


def run(plan: BootPlan, *, timeout: float) -> int:
    print(f"run_boot_check: migraciones de {plan.migrate_image}")
    print(f"run_boot_check: arranque de {plan.boot_image} con {plan.boot_script.name}")
    try:
        _docker(["docker", "network", "create", plan.network], timeout=60)
        _docker(plan.postgres_command(), timeout=600)
        _wait_postgres(plan, 120)
        migrate = _execute(plan.migrate_command(), timeout=900)
        if migrate.returncode != 0:
            print(migrate.stderr[-4000:], file=sys.stderr)
            print("run_boot_check: FALLA: las migraciones no se aplicaron", file=sys.stderr)
            return 1
        print("run_boot_check: migraciones aplicadas (alembic upgrade head)")
        passed, detail = _boot(plan, timeout)
        if not passed:
            logs = _execute(["docker", "logs", "--tail", "120", plan.api], timeout=60)
            print(logs.stdout[-8000:] + logs.stderr[-8000:], file=sys.stderr)
            print(f"run_boot_check: FALLA: {detail}", file=sys.stderr)
            return 1
        print(f"run_boot_check: la imagen arranca contra el esquema: {detail}")
        if plan.render_script is not None:
            render = _execute(plan.render_command(), timeout=900)
            print(render.stdout[-4000:], end="")
            if render.returncode != 0:
                print(render.stderr[-8000:], file=sys.stderr)
                print("run_boot_check: FALLA: el acta no se renderiza sin red", file=sys.stderr)
                return 1
            print("run_boot_check: el acta se renderiza dentro de la imagen sin red")
        return 0
    except BootCheckError as error:
        print(f"run_boot_check: {error}", file=sys.stderr)
        return 2
    finally:
        for cleanup in (
            ["docker", "rm", "--force", plan.api, plan.database],
            ["docker", "network", "rm", plan.network],
        ):
            with contextlib.suppress(BootCheckError):
                _execute(cleanup, timeout=120)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Arranca vigia-api de una imagen contra el esquema migrado por otra."
    )
    parser.add_argument("--migrate-image", required=True, help="imagen N: aplica las migraciones")
    parser.add_argument("--boot-image", required=True, help="imagen N-1: debe arrancar")
    parser.add_argument("--boot-script", type=Path, default=DEFAULT_BOOT_SCRIPT)
    render = parser.add_mutually_exclusive_group()
    render.add_argument(
        "--render-script",
        type=Path,
        help="render del acta sin red (por defecto, tools/image_render.py si N y N-1 coinciden)",
    )
    render.add_argument("--no-render", action="store_true", help="sin el render del acta")
    parser.add_argument("--platform", help="p. ej. linux/arm64 (por defecto, la de la imagen)")
    parser.add_argument("--timeout", type=float, default=240.0, help="segundos hasta ready")
    args = parser.parse_args(argv)
    render_script: Path | None = args.render_script
    if render_script is None and not args.no_render and args.migrate_image == args.boot_image:
        render_script = DEFAULT_RENDER_SCRIPT
    for script in (args.boot_script, render_script):
        if script is not None and not script.is_file():
            print(f"run_boot_check: no existe {script}", file=sys.stderr)
            return 2
    plan = BootPlan(
        migrate_image=args.migrate_image,
        boot_image=args.boot_image,
        boot_script=args.boot_script,
        platform=args.platform,
        render_script=render_script,
    )
    return run(plan, timeout=args.timeout)


if __name__ == "__main__":
    sys.exit(main())
