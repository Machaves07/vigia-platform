"""Permisos, exclusión y detección de fugas del soporte e2e; solo datos generados."""

from __future__ import annotations

import json
import secrets
import stat
import zipfile
from pathlib import Path

import pytest

from tests.e2e_u05 import __main__ as cli
from tests.e2e_u05.world import assert_no_leaks, private_json, published_manifest


def test_private_file_is_0600_from_creation_and_never_overwrites(tmp_path: Path) -> None:
    path = tmp_path / "private.json"
    private_json(path, {"value": secrets.token_hex(20)})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        private_json(path, {})
    assert "value" in json.loads(path.read_text())


@pytest.mark.parametrize("tree", ["frontend", "backend"])
@pytest.mark.parametrize("archive", [False, True], ids=["archivo", "traza-comprimida"])
def test_leak_scan_fails_without_disclosing_the_value(
    tmp_path: Path, tree: str, archive: bool
) -> None:
    root = tmp_path / tree
    root.mkdir()
    value = secrets.token_hex(20)
    path = root / ("trace.zip" if archive else "output.log")
    if archive:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as content:
            content.writestr("test.trace", value)
    else:
        path.write_text(value)
    with pytest.raises(AssertionError) as caught:
        assert_no_leaks((root,), (value,))
    assert value not in str(caught.value)


def test_only_explicit_private_file_is_allowed(tmp_path: Path) -> None:
    value = secrets.token_hex(20)
    allowed = tmp_path / "private.json"
    private_json(allowed, {"value": value})
    assert_no_leaks((tmp_path,), (value,), allowed=(allowed,))
    (tmp_path / "other.json").write_text(value)
    with pytest.raises(AssertionError):
        assert_no_leaks((tmp_path,), (value,), allowed=(allowed,))


def test_manifest_is_removed_on_normal_exit_and_test_failure(tmp_path: Path) -> None:
    path = tmp_path / ".world.json"
    with published_manifest(path, {"base_url": "https://example.test"}):
        assert path.is_file()
    assert not path.exists()
    with pytest.raises(RuntimeError), published_manifest(path, {}):
        raise RuntimeError("Fallo sintético de la prueba")
    assert not path.exists()


def test_partial_manifest_is_removed_when_publication_fails(tmp_path: Path) -> None:
    path = tmp_path / ".world.json"
    with pytest.raises(TypeError), published_manifest(path, {"unsupported": object()}):
        pytest.fail("No debe llegar a publicar un documento no serializable")
    assert not path.exists()


def test_manifest_collision_never_deletes_another_run(tmp_path: Path) -> None:
    path = tmp_path / ".world.json"
    path.write_text("otra ejecución")
    with pytest.raises(FileExistsError), published_manifest(path, {}):
        pytest.fail("No debe entrar en el mundo de otra ejecución")
    assert path.read_text() == "otra ejecución"


def test_world_startup_diagnostic_never_includes_exception_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    value = secrets.token_hex(20)

    def unavailable() -> None:
        raise ValueError(value)

    monkeypatch.setattr(cli, "world", unavailable)
    monkeypatch.setattr(cli.signal, "signal", lambda *_: None)
    assert cli.main() == 1
    output = capsys.readouterr()
    assert output.out.startswith("VIGIA_WORLD_FAILURE ValueError (")
    assert "unavailable:" in output.out
    assert value not in output.out
    assert output.err == ""
