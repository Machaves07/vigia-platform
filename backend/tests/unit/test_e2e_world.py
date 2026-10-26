"""Permisos, exclusión y detección de fugas del soporte e2e; solo datos generados."""

from __future__ import annotations

import asyncio
import json
import secrets
import stat
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from tests.conformance.platform_target import PlatformStack
from tests.e2e_u05 import __main__ as cli
from tests.e2e_u05 import world as world_module
from tests.e2e_u05.journeys import browser_store_cors
from tests.e2e_u05.world import (
    assert_no_leaks,
    configure_store_origin,
    deploy_check_invitation,
    private_json,
    published_manifest,
)
from vigia_platform.identity.application.users import InviteRequest
from vigia_platform.shared.context import Role, ScopeLevel


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


@pytest.mark.parametrize("kind", ["ValueError", "Skipped", "Failed"])
def test_world_startup_diagnostic_never_includes_exception_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    value = secrets.token_hex(20)

    def unavailable() -> None:
        if kind == "Skipped":
            pytest.skip(value)
        if kind == "Failed":
            pytest.fail(value)
        raise ValueError(value)

    monkeypatch.setattr(cli, "world", unavailable)
    monkeypatch.setattr(cli.signal, "signal", lambda *_: None)
    assert cli.main() == 1
    output = capsys.readouterr()
    assert output.out.startswith(f"VIGIA_WORLD_FAILURE {kind} (")
    assert "unavailable:" in output.out
    assert value not in output.out
    assert output.err == ""


def test_store_cors_uses_the_single_application_origin_and_production_headers() -> None:
    calls: list[dict[str, Any]] = []
    closed: list[bool] = []
    store = SimpleNamespace(
        put_bucket_cors=lambda **kwargs: calls.append(kwargs), close=lambda: closed.append(True)
    )
    platform = cast(
        PlatformStack,
        SimpleNamespace(
            environ={"VIGIA_EVIDENCE_BUCKET": "evidence-synthetic"},
            localstack=SimpleNamespace(aws_client=lambda _service: store),
        ),
    )
    configure_store_origin(platform, "https://app.example.test")
    assert closed == [True]
    assert len(calls) == 1
    assert calls[0] == {
        "Bucket": "evidence-synthetic",
        "CORSConfiguration": {
            "CORSRules": [
                {
                    "AllowedOrigins": ["https://app.example.test"],
                    "AllowedMethods": ["PUT", "GET", "HEAD"],
                    "AllowedHeaders": ["Content-Type", "x-amz-checksum-sha256", "x-amz-meta-*"],
                    "ExposeHeaders": ["ETag"],
                    "MaxAgeSeconds": 3600,
                }
            ]
        },
    }


def test_browser_store_has_one_closed_origin_and_the_production_headers() -> None:
    assert browser_store_cors("https://127.0.0.1:4443") == {
        "CORSRules": [
            {
                "AllowedOrigins": ["https://127.0.0.1:4443"],
                "AllowedMethods": ["PUT", "GET", "HEAD"],
                "AllowedHeaders": ["Content-Type", "x-amz-checksum-sha256", "x-amz-meta-*"],
                "ExposeHeaders": ["ETag"],
                "MaxAgeSeconds": 3600,
            }
        ]
    }


def _invitation_world(
    monkeypatch: pytest.MonkeyPatch, link: str | None
) -> tuple[Any, list[Any], list[InviteRequest]]:
    """Mundo mínimo: la persona administradora invita por ``UserService`` (doble)."""
    statements: list[Any] = []
    requests: list[InviteRequest] = []

    class FakeProvisioner:
        def __init__(self, *_: Any) -> None:
            pass

        def _session(self, organization: uuid.UUID, user: uuid.UUID) -> tuple[str, str]:
            return f"cookie-{user}", f"sesión de {user} en {organization}"

        async def _context(self, cookie: str, concession: None) -> str:
            return f"contexto de {cookie}"

    async def invite_user(context: str, request: InviteRequest) -> Any:
        assert context.startswith("contexto de cookie-")
        requests.append(request)
        return SimpleNamespace(link=link)

    monkeypatch.setattr(world_module, "Provisioner", FakeProvisioner)
    users = SimpleNamespace(invite_user=invite_user)
    target = SimpleNamespace(
        stack=SimpleNamespace(admin=statements.extend),
        api=SimpleNamespace(runtime=SimpleNamespace(identity=SimpleNamespace(users=users))),
        app=SimpleNamespace(url="https://127.0.0.1:1"),
        tls=SimpleNamespace(ca_file=Path("ca")),
        directory=Path("d"),
    )
    target.api.call = asyncio.run
    return target, statements, requests


def test_deploy_check_invitation_is_a_disclosed_coordinator_invitation_of_the_synthetic_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    link = f"https://127.0.0.1:1/invitations#{secrets.token_urlsafe(16)}"
    target, statements, requests = _invitation_world(monkeypatch, link)
    organization, administrator = uuid.uuid4(), uuid.uuid4()
    fleet = SimpleNamespace(organization_id=organization)
    material = deploy_check_invitation(cast(Any, target), cast(Any, fleet), str(administrator))
    assert material == {"invitation_link": link, "email": "deploy-check@e2e.example.test"}
    assert statements == [f"sesión de {administrator} en {organization}"]
    (request,) = requests
    assert request.disclose_link is True and request.email == material["email"]
    (assignment,) = request.assignments
    assert assignment.role is Role.COORDINATOR_SST
    assert (assignment.scope_level, assignment.scope_id) == (ScopeLevel.ORGANIZATION, organization)


def test_deploy_check_invitation_fails_closed_without_a_disclosed_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target, _, _ = _invitation_world(monkeypatch, None)
    fleet = SimpleNamespace(organization_id=uuid.uuid4())
    with pytest.raises(RuntimeError, match="no divulgó"):
        deploy_check_invitation(cast(Any, target), cast(Any, fleet), str(uuid.uuid4()))
