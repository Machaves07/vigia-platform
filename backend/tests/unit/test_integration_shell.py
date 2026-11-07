"""Los guiones de ``local/integration/`` con un ``docker`` de mentira (TASK-545, VIG-393).

Corren en el CI sin Docker: el ``docker`` falso anota cada llamada y falla o responde según el
entorno. Lo que se comprueba es la lógica de los guiones (qué rechazan, en qué orden hacen las
cosas y qué no tocan nunca antes de comprobar), siempre por **código de error cerrado**
(``error[<código>]`` en la salida de errores) o por las llamadas anotadas, nunca por el texto. El
despliegue real lo recorre ``integration/test_compose_integration.py``.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Final

import pytest

from vigia_platform.shared.clock import SystemClock

INTEGRATION: Final = Path(__file__).resolve().parents[3] / "local" / "integration"
FAKE_DOCKER: Final = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
if [ ! -t 0 ]; then cat >/dev/null 2>&1 || true; fi
if [ -n "${FAKE_FAIL:-}" ] && [[ "$*" == *"$FAKE_FAIL"* ]]; then exit 1; fi
case "$*" in
  *pg_dump*) [ -n "${FAKE_EMPTY_DUMP:-}" ] || printf 'DUMP' ;;
  *"FROM identity.organization"*) printf '%s' "${FAKE_PROVIDER:-}" ;;
  *" run "*) echo "$FAKE_RUN_JSON" ;;
esac
exit 0
"""
RUN_JSON: Final = (
    '{"provider_organization_id": "p-1", "root_sha256": "r-1", "breach_list": "sintetica"}'
)
CODE: Final = re.compile(r"error\[([a-z_]+)\]")


class Harness:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp_path
        self.log = tmp_path / "docker.log"
        self.log.write_text("")
        binary = tmp_path / "bin"
        binary.mkdir()
        docker = binary / "docker"
        docker.write_text(FAKE_DOCKER)
        docker.chmod(0o755)
        self.environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("VIGIA_", "FAKE_"))
        }
        self.environment.update(
            PATH=f"{binary}:{os.environ['PATH']}",
            HOME=str(tmp_path),
            FAKE_LOG=str(self.log),
            FAKE_RUN_JSON=RUN_JSON,
            VIGIA_EDGE_BIND="127.0.0.1",
            VIGIA_INTEGRATION_ENV=str(tmp_path / "sin-env"),
            VIGIA_BACKUP_DIR=str(tmp_path / "copias"),
            VIGIA_INTEGRATION_CREDENTIALS=str(tmp_path / "credenciales" / "c.json"),
        )

    def run(
        self, script: str, *arguments: str, **environment: str
    ) -> subprocess.CompletedProcess[str]:
        env = {**self.environment, **environment}
        for key in [key for key, value in env.items() if value is None]:
            del env[key]
        return subprocess.run(
            [str(INTEGRATION / script), *arguments],
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )

    def calls(self) -> list[str]:
        return [line for line in self.log.read_text().splitlines() if line]

    def called(self, fragment: str) -> bool:
        return any(fragment in line for line in self.calls())

    @property
    def copies(self) -> Path:
        return self.tmp / "copias"


def code(result: subprocess.CompletedProcess[str]) -> str | None:
    found = CODE.findall(result.stderr)
    return found[-1] if found else None


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(tmp_path, monkeypatch)


# --- lib.sh: la IP de publicación ---------------------------------------------------------------


@pytest.mark.parametrize("script", ["backup.sh", "restore.sh", "admin.sh", "bootstrap.sh"])
def test_every_script_needs_the_edge_address(harness: Harness, script: str) -> None:
    arguments = () if script in {"bootstrap.sh", "admin.sh"} else ("--yes", "x")
    result = harness.run(script, *arguments, VIGIA_EDGE_BIND="")
    assert result.returncode != 0 and code(result) == "edge_bind_missing"
    assert harness.calls() == []


@pytest.mark.parametrize(
    "address",
    [
        "0.0.0.0",  # noqa: S104
        "::",
        "::ffff:0.0.0.0",
        "192.168.20.69",
        "8.8.8.8",
        "tailscale0",
    ],
)
def test_no_script_accepts_an_address_outside_loopback_and_the_tailnet(
    harness: Harness, address: str
) -> None:
    for script, arguments in (
        ("backup.sh", ()),
        ("restore.sh", ("--yes", "x")),
        ("bootstrap.sh", ()),
    ):
        result = harness.run(script, *arguments, VIGIA_EDGE_BIND=address)
        assert result.returncode != 0 and code(result) == "edge_bind_not_allowed", script
    assert harness.calls() == []


@pytest.mark.parametrize("address", ["127.0.0.1", "100.64.0.1", "fd7a:115c:a1e0::5"])
def test_loopback_and_tailnet_addresses_pass(harness: Harness, address: str) -> None:
    assert harness.run("backup.sh", VIGIA_EDGE_BIND=address).returncode == 0


def test_the_address_can_come_from_the_environment_file(harness: Harness) -> None:
    env_file = harness.tmp / "integration.env"
    env_file.write_text("VIGIA_EDGE_BIND=100.64.9.9" + chr(10))
    ok = harness.run("backup.sh", VIGIA_EDGE_BIND="", VIGIA_INTEGRATION_ENV=str(env_file))
    assert ok.returncode == 0
    assert harness.called("--env-file " + str(env_file))
    env_file.write_text("VIGIA_EDGE_BIND=0.0.0.0" + chr(10))
    bad = harness.run("backup.sh", VIGIA_EDGE_BIND="", VIGIA_INTEGRATION_ENV=str(env_file))
    assert code(bad) == "edge_bind_not_allowed"


# --- backup.sh ----------------------------------------------------------------------------------


def test_backup_publishes_one_private_validated_copy(harness: Harness) -> None:
    result = harness.run("backup.sh")
    assert result.returncode == 0, result.stderr
    path = Path(result.stdout.strip())
    assert path.parent == harness.copies and path.read_bytes() == b"DUMP"
    assert path.stat().st_mode & 0o777 == 0o600
    assert harness.copies.stat().st_mode & 0o777 == 0o700
    assert [p.name for p in harness.copies.iterdir() if p.name.startswith(".")] == []
    assert harness.called("pg_dump --format=custom --create")
    assert harness.called("pg_restore --file=/dev/null"), "la lee entera, no solo el índice"
    assert not harness.called("pg_restore --list")


def test_backup_fails_and_leaves_nothing_if_pg_dump_fails(harness: Harness) -> None:
    result = harness.run("backup.sh", FAKE_FAIL="pg_dump")
    assert code(result) == "dump_failed" and result.returncode != 0
    assert list(harness.copies.glob("vigia-*.dump")) == []
    assert [p.name for p in harness.copies.iterdir()] == []


def test_backup_refuses_an_empty_copy(harness: Harness) -> None:
    result = harness.run("backup.sh", FAKE_EMPTY_DUMP="1")
    assert code(result) == "dump_empty"
    assert list(harness.copies.glob("vigia-*.dump")) == []


def test_backup_refuses_a_copy_pg_restore_cannot_read_whole(harness: Harness) -> None:
    result = harness.run("backup.sh", FAKE_FAIL="--file=/dev/null")
    assert code(result) == "dump_unreadable"
    assert list(harness.copies.glob("vigia-*.dump")) == []
    assert [p.name for p in harness.copies.iterdir()] == []


def test_backup_deletes_only_the_copies_older_than_the_retention(harness: Harness) -> None:
    harness.copies.mkdir()
    old, recent, other = (
        harness.copies / name for name in ("vigia-viejo.dump", "vigia-reciente.dump", "otra.txt")
    )
    for path in (old, recent, other):
        path.write_text("x")
    long_ago = SystemClock().now().timestamp() - 20 * 86400
    os.utime(old, (long_ago, long_ago))
    os.utime(other, (long_ago, long_ago))
    assert harness.run("backup.sh").returncode == 0
    assert not old.exists() and recent.exists() and other.exists()
    keep_one_day = harness.run("backup.sh", VIGIA_BACKUP_KEEP_DAYS="30")
    assert keep_one_day.returncode == 0


# --- restore.sh ---------------------------------------------------------------------------------


@pytest.fixture
def dump(harness: Harness) -> Path:
    path = harness.tmp / "vigia-prueba.dump"
    path.write_bytes(b"DUMP")
    return path


def test_restore_needs_yes_in_the_first_position(harness: Harness, dump: Path) -> None:
    result = harness.run("restore.sh", str(dump), "--yes")
    assert code(result) == "restore_needs_yes"
    assert harness.calls() == []


def test_restore_needs_yes_even_with_a_good_copy(harness: Harness, dump: Path) -> None:
    result = harness.run("restore.sh", "--no", str(dump))
    assert code(result) == "restore_needs_yes"
    assert harness.calls() == []


def test_restore_needs_a_copy_that_exists(harness: Harness, dump: Path) -> None:
    assert code(harness.run("restore.sh", "--yes")) == "restore_missing_file"
    assert code(harness.run("restore.sh", "--yes", str(dump) + ".no")) == "restore_missing_file"
    assert code(harness.run("restore.sh", "--yes", str(harness.tmp))) == "restore_missing_file"
    assert harness.calls() == []


def stops_or_drops(harness: Harness) -> bool:
    return any(
        harness.called(fragment)
        for fragment in (
            " stop ",
            "--create --clean",
            "pg_terminate_backend",
            "DROP DATABASE IF EXISTS vigia ",
        )
    )


def test_a_copy_that_cannot_be_read_whole_touches_nothing(harness: Harness, dump: Path) -> None:
    result = harness.run("restore.sh", "--yes", str(dump), FAKE_FAIL="--file=/dev/null")
    assert code(result) == "dump_unreadable"
    assert not stops_or_drops(harness) and not harness.called("pg_dump")


def test_a_copy_that_does_not_restore_in_a_scratch_database_touches_nothing(
    harness: Harness, dump: Path
) -> None:
    result = harness.run("restore.sh", "--yes", str(dump), FAKE_FAIL="--dbname=vigia_restore_check")
    assert code(result) == "restore_check_failed"
    assert not stops_or_drops(harness) and not harness.called("pg_dump")
    assert harness.called("DROP DATABASE IF EXISTS vigia_restore_check"), "la temporal se borra"


def test_the_live_database_is_saved_before_anything_is_stopped(
    harness: Harness, dump: Path
) -> None:
    result = harness.run("restore.sh", "--yes", str(dump), FAKE_FAIL="pg_dump")
    assert code(result) == "dump_failed" and not stops_or_drops(harness)
    assert [p.name for p in harness.copies.iterdir()] == []


def test_restore_runs_its_steps_in_order_and_keeps_the_previous_database(
    harness: Harness, dump: Path
) -> None:
    result = harness.run("restore.sh", "--yes", str(dump))
    assert result.returncode == 0, result.stderr
    calls = harness.calls()

    def at(fragment: str) -> int:
        return next(index for index, line in enumerate(calls) if fragment in line)

    order = [
        at("pg_restore --file=/dev/null"),
        at("--dbname=vigia_restore_check"),
        at("pg_dump --format=custom --create"),
        at(" stop "),
        at("pg_terminate_backend"),
        at("pg_restore --create --clean --if-exists --exit-on-error"),
    ]
    assert order == sorted(order) and len(set(order)) == len(order)
    previous = list(harness.copies.glob("previa-*.dump"))
    assert len(previous) == 1 and previous[0].read_bytes() == b"DUMP"
    assert previous[0].stat().st_mode & 0o777 == 0o600


def test_a_failed_restore_brings_back_the_previous_database_and_the_apps(
    harness: Harness, dump: Path
) -> None:
    result = harness.run("restore.sh", "--yes", str(dump), FAKE_FAIL="--create --clean")
    assert code(result) == "restore_failed"
    restores = [line for line in harness.calls() if "--create --clean" in line]
    assert len(restores) == 2, "el segundo intento es con la copia previa"
    assert harness.calls()[-1].endswith("up -d --wait") or "up -d --wait" in harness.calls()[-1]


# --- bootstrap.sh -------------------------------------------------------------------------------


def admin_calls(harness: Harness) -> list[str]:
    return [line for line in harness.calls() if " run " in line]


def test_bootstrap_refuses_an_unknown_argument_before_anything(harness: Harness) -> None:
    result = harness.run("bootstrap.sh", "--first-install", "--borrar")
    assert code(result) == "unknown_argument" and harness.calls() == []


def test_the_first_install_mark_is_passed_only_when_asked_and_there_is_no_provider(
    harness: Harness,
) -> None:
    with_mark = harness.run("bootstrap.sh", "--first-install", FAKE_PROVIDER="")
    assert with_mark.returncode == 0, with_mark.stderr
    creating = [line for line in admin_calls(harness) if "vigia-admin bootstrap" in line]
    assert len(creating) == 1 and "--first-install" in creating[0] and "--resume" not in creating[0]
    harness.log.write_text("")
    without = harness.run("bootstrap.sh", FAKE_PROVIDER="")
    creating = [line for line in admin_calls(harness) if "vigia-admin bootstrap" in line]
    assert without.returncode == 0 and "--first-install" not in creating[0]


@pytest.mark.parametrize("arguments", [[], ["--first-install"]])
def test_with_a_provider_the_recipe_is_resume_and_never_creates_the_authority(
    harness: Harness, arguments: list[str]
) -> None:
    result = harness.run(
        "bootstrap.sh", *arguments, FAKE_PROVIDER="0a0a0a0a-0000-4000-8000-000000000000"
    )
    assert result.returncode == 0, result.stderr
    bootstrap = [line for line in admin_calls(harness) if "vigia-admin bootstrap" in line]
    assert len(bootstrap) == 1
    assert "--resume --publish-root --yes" in bootstrap[0] and "--first-install" not in bootstrap[0]


def test_bootstrap_prints_a_fixed_summary_with_the_breach_list_kind(harness: Harness) -> None:
    result = harness.run("bootstrap.sh", "--first-install")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert [line.split(":")[0] for line in lines] == [
        "proveedora",
        "raiz_de_nodos",
        "lista_contrasenas",
        "organizacion",
        "planta",
        "credenciales_de_prueba",
        "BOOTSTRAP_OK",
    ]
    assert "lista_contrasenas: sintetica" in lines
    again = harness.run("bootstrap.sh", "--first-install")
    assert again.stdout == result.stdout


def test_bootstrap_stops_if_the_authority_command_fails(harness: Harness) -> None:
    result = harness.run("bootstrap.sh", "--first-install", FAKE_FAIL="vigia-admin bootstrap")
    assert result.returncode != 0 and "BOOTSTRAP_OK" not in result.stdout.splitlines()
    assert not harness.called("seed.py"), "no siembra ni publica nada si bootstrap falló"


# --- admin.sh -----------------------------------------------------------------------------------


def test_admin_forces_a_localstack_copy_and_returns_the_command_status(harness: Harness) -> None:
    ok = harness.run("admin.sh", "rotate-key", "catalog")
    assert ok.returncode == 0
    assert harness.called("vigia-admin rotate-key catalog")
    assert harness.calls()[-1].endswith("state.py save")
    harness.log.write_text("")
    failed = harness.run("admin.sh", "rotate-key", "catalog", FAKE_FAIL="vigia-admin rotate-key")
    assert failed.returncode != 0
    assert harness.calls()[-1].endswith("state.py save"), "la copia se fuerza también si falla"


def test_backup_makes_an_existing_directory_private(harness: Harness) -> None:
    harness.copies.mkdir()
    harness.copies.chmod(0o755)
    assert harness.run("backup.sh").returncode == 0
    assert harness.copies.stat().st_mode & 0o777 == 0o700


def test_bootstrap_forces_a_copy_before_starting_the_app(harness: Harness) -> None:
    assert harness.run("bootstrap.sh", "--first-install").returncode == 0
    calls = harness.calls()
    saved = next(i for i, line in enumerate(calls) if line.endswith("/opt/vigia/state.py save"))
    started = next(i for i, line in enumerate(calls) if "--profile app up -d --wait" in line)
    migrated = next(i for i, line in enumerate(calls) if "vigia-migrate" in line)
    assert migrated < saved < started
