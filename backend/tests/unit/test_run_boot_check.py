"""``tools/run_boot_check.py``: las órdenes de la prueba de arranque N-1 (TASK-143; NFR-NUC-14).

Sin Docker: se comprueba el plan. La migración la aplica la imagen N con la orden de
``vigia-migrate``; la imagen N-1 arranca con su propio ``tools/image_boot.py`` montado de solo
lectura, con la raíz de solo lectura, ``/tmp`` aparte, sin capacidades, sin ``--privileged`` ni
``--user`` (el de la imagen) y sin contraseñas fijas. La ejecución real está en el trabajo
«arranque N-1» de ``ci.yml`` y en la evidencia del PR.
"""

from __future__ import annotations

from pathlib import Path

from tools.run_boot_check import BOOT_SCRIPT_MOUNT, POSTGRES_IMAGE, TMP_MOUNT, BootPlan, main


def _plan(tmp_path: Path) -> BootPlan:
    script = tmp_path / "image_boot.py"
    script.write_text("")
    return BootPlan(migrate_image="img:n", boot_image="img:n-1", boot_script=script)


def test_both_images_run_hardened(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    for command in (plan.migrate_command(), plan.boot_command()):
        assert command[:2] == ["docker", "run"]
        for flag in ("--read-only", "--cap-drop"):
            assert flag in command
        assert command[command.index("--cap-drop") + 1] == "ALL"
        assert command[command.index("--tmpfs") + 1] == TMP_MOUNT
        assert "--privileged" not in command and "--user" not in command
        assert command[command.index("--network") + 1] == plan.network


def test_n_migrates_and_n_minus_one_boots_with_its_own_script(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    migrate, boot = plan.migrate_command(), plan.boot_command()
    assert migrate[-4:] == ["img:n", "alembic", "upgrade", "head"]
    assert boot[-3:] == ["img:n-1", "python", BOOT_SCRIPT_MOUNT]
    volume = boot[boot.index("--volume") + 1]
    assert volume == f"{plan.boot_script.resolve()}:{BOOT_SCRIPT_MOUNT}:ro"
    assert boot[boot.index("--publish") + 1].startswith("127.0.0.1::")
    assert any(
        e.startswith("VIGIA_BOOT_DATABASE_URL=postgresql+asyncpg://vigia_app:") for e in boot
    )
    assert plan.postgres_command()[-1] == POSTGRES_IMAGE


def test_passwords_are_random_and_never_shown(tmp_path: Path) -> None:
    first, second = _plan(tmp_path), _plan(tmp_path)
    assert first.app_password != second.app_password
    assert first.network != second.network
    shown = repr(first)
    for password in (first.owner_password, first.app_password, first.migrate_password):
        assert len(password) >= 20
        assert password not in shown


def test_a_missing_boot_script_is_an_environment_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing.py"
    assert main(["--migrate-image", "a", "--boot-image", "b", "--boot-script", str(missing)]) == 2
