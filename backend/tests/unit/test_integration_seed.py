"""Idempotencia de ``local/integration/seed.py`` con una conexión falsa (TASK-545, VIG-393).

``bootstrap.sh`` se repite y debe dar la misma salida sin crear nada nuevo: aquí se comprueban las
decisiones de ``seed.py`` (qué existe ya, qué se crea, qué se rechaza) sin base de datos. El
recorrido real contra PostgreSQL, LocalStack y los servicios de aplicación es de
``integration/test_compose_integration.py``.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import types
import uuid
from pathlib import Path
from typing import Any, Final

import pytest

SEED: Final = Path(__file__).resolve().parents[3] / "local" / "integration" / "seed.py"


@pytest.fixture(scope="module")
def seed() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("vigia_integration_seed", SEED)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["vigia_integration_seed"] = module
    spec.loader.exec_module(module)
    return module


class Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: object) -> None:
        return None


class FakeConnection:
    """Responde a ``fetchrow`` y ``fetch`` con lo guionizado y anota cada ``execute``."""

    def __init__(self, *, rows: list[Any] | None = None, fetched: list[Any] | None = None) -> None:
        self.rows = list(rows or [])
        self.fetched = list(fetched or [])
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.queries: list[str] = []

    async def fetchrow(self, sql: str, *arguments: Any) -> Any:
        self.queries.append(sql)
        return self.rows.pop(0) if self.rows else None

    async def fetch(self, sql: str, *arguments: Any) -> Any:
        self.queries.append(sql)
        return self.fetched.pop(0) if self.fetched else []

    async def fetchval(self, sql: str, *arguments: Any) -> Any:
        self.queries.append(sql)
        return 2

    async def execute(self, sql: str, *arguments: Any) -> None:
        self.executed.append((sql, arguments))

    def transaction(self) -> Transaction:
        return Transaction()

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_the_provider_must_exist(seed: types.ModuleType) -> None:
    with pytest.raises(SystemExit):
        await seed.provider_organization(FakeConnection())
    provider = uuid.UUID(int=5)
    found = await seed.provider_organization(FakeConnection(rows=[{"organization_id": provider}]))
    assert found == provider


@pytest.mark.asyncio
async def test_the_client_organization_is_created_once(seed: types.ModuleType) -> None:
    existing = uuid.UUID(int=8)
    connection = FakeConnection(rows=[{"organization_id": existing}])
    assert (
        await seed.ensure_organization(connection, uuid.UUID(int=1), uuid.UUID(int=2)) == existing
    )
    assert connection.executed == []
    empty = FakeConnection()
    created = await seed.ensure_organization(empty, uuid.UUID(int=1), uuid.UUID(int=2))
    assert created == uuid.uuid5(seed.NAMESPACE, "organizacion/INT-CLI")
    assert len(empty.executed) == 1 and "INSERT INTO identity.organization" in empty.executed[0][0]


class FakeCipher:
    async def encrypt(self, secret: bytes, aad: bytes) -> types.SimpleNamespace:
        return types.SimpleNamespace(ciphertext=b"c" + secret, wrapped_key=b"w")


@pytest.mark.asyncio
async def test_an_existing_person_is_left_alone_and_gets_no_new_credentials(
    seed: types.ModuleType,
) -> None:
    connection = FakeConnection(rows=[{"?column?": 1}])
    result = await seed.ensure_user(
        connection,
        FakeCipher(),
        role=seed.Role.ADMINISTRATOR,
        organization=uuid.UUID(int=3),
        operator=uuid.UUID(int=4),
        zone=None,
    )
    assert result is None
    assert connection.executed == []


@pytest.mark.asyncio
async def test_a_new_person_gets_all_four_rows_and_a_totp_secret(seed: types.ModuleType) -> None:
    connection = FakeConnection()
    result = await seed.ensure_user(
        connection,
        FakeCipher(),
        role=seed.Role.ADMINISTRATOR,
        organization=uuid.UUID(int=3),
        operator=uuid.UUID(int=4),
        zone=None,
    )
    assert result is not None
    tables = [sql.split("INTO ")[1].split(" ")[0] for sql, _ in connection.executed]
    assert tables == [
        "identity.user_account",
        "identity.role_assignment",
        "identity.password_credential",
        "identity.totp_credential",
    ]
    assert (
        len(base64.b32decode(result["totp_base32"] + "=" * (-len(result["totp_base32"]) % 8))) == 20
    )
    assert result["email"] == "administrator@integracion.example.test"
    assignment = connection.executed[1][1]
    assert assignment[4:6] == ("organization", uuid.UUID(int=3))


@pytest.mark.asyncio
async def test_the_line_manager_is_scoped_to_a_zone_and_needs_one(seed: types.ModuleType) -> None:
    zone = uuid.UUID(int=9)
    connection = FakeConnection()
    await seed.ensure_user(
        connection,
        FakeCipher(),
        role=seed.Role.LINE_MANAGER,
        organization=uuid.UUID(int=3),
        operator=uuid.UUID(int=4),
        zone=zone,
    )
    assert connection.executed[1][1][4:6] == ("zone", zone)
    with pytest.raises(SystemExit):
        await seed.ensure_user(
            FakeConnection(),
            FakeCipher(),
            role=seed.Role.LINE_MANAGER,
            organization=uuid.UUID(int=3),
            operator=uuid.UUID(int=4),
            zone=None,
        )


@pytest.mark.asyncio
async def test_a_live_concession_is_reused_and_a_missing_one_is_granted(
    seed: types.ModuleType,
) -> None:
    live = uuid.UUID(int=6)
    reused = FakeConnection(rows=[{"concession_id": live}])
    assert (
        await seed.renew_concession(reused, client=uuid.UUID(int=1), provider=uuid.UUID(int=2))
        == live
    )
    assert reused.executed == []
    fresh = FakeConnection()
    granted = await seed.renew_concession(fresh, client=uuid.UUID(int=1), provider=uuid.UUID(int=2))
    assert len(fresh.executed) == 1
    assert "INSERT INTO identity.provider_concession" in fresh.executed[0][0]
    assert fresh.executed[0][1][0] == granted


class FakeRuntime:
    def __init__(self) -> None:
        self.created: list[Any] = []
        hierarchy = types.SimpleNamespace(create_zone=self.create_zone)
        self.app = types.SimpleNamespace(identity=types.SimpleNamespace(hierarchy=hierarchy))

    async def context(self, cookie: object, concession: object = None) -> str:
        return "contexto"

    async def create_zone(self, context: str, plant: uuid.UUID, spec: Any) -> types.SimpleNamespace:
        self.created.append((plant, spec.code))
        return types.SimpleNamespace(zone_id=uuid.UUID(int=77))


@pytest.mark.asyncio
async def test_a_free_zone_is_used_before_a_new_one_is_created(
    seed: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    free = uuid.UUID(int=21)
    runtime = FakeRuntime()
    connection = FakeConnection(fetched=[[{"zone_id": free}]])
    cookie: Any = object()
    zone = await seed.free_zone(
        runtime, connection, uuid.UUID(int=1), administrator=cookie, installer=cookie,
        concession=uuid.UUID(int=2),
    )  # fmt: skip
    assert zone == free and runtime.created == []


@pytest.mark.asyncio
async def test_without_a_free_zone_the_next_one_is_created_with_its_catalog(
    seed: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared: list[Any] = []

    async def ensure_catalog(*arguments: Any, **keywords: Any) -> None:
        prepared.append((arguments[2], keywords["approve_usage"]))

    monkeypatch.setattr(seed, "ensure_catalog_and_gates", ensure_catalog)
    runtime = FakeRuntime()
    plant = uuid.UUID(int=1)
    cookie: Any = object()
    zone = await seed.free_zone(
        runtime, FakeConnection(), plant, administrator=cookie, installer=cookie,
        concession=uuid.UUID(int=2),
    )  # fmt: skip
    assert zone == uuid.UUID(int=77)
    assert runtime.created == [(plant, "INT-ZONA-3")], "la siguiente a las dos que ya hay"
    assert prepared == [([uuid.UUID(int=77)], False)], "catálogo y montaje, sin el uso"


def test_credentials_are_merged_private_and_untouched_when_there_is_nothing_new(
    seed: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "credentials.json"
    monkeypatch.setattr(seed, "CREDENTIALS_FILE", target)
    seed.merge_credentials({})
    assert not target.exists()
    seed.merge_credentials({"administrator": {"email": "a"}})
    seed.merge_credentials({"copasst": {"email": "c"}})
    assert json.loads(target.read_text()) == {
        "administrator": {"email": "a"},
        "copasst": {"email": "c"},
    }
    assert target.stat().st_mode & 0o777 == 0o600
    seed.merge_credentials({})
    assert sorted(json.loads(target.read_text())) == ["administrator", "copasst"]


@pytest.mark.asyncio
async def test_a_session_is_never_opened_for_the_provider_roles(seed: types.ModuleType) -> None:
    for role in ("provider_installer", "platform_operator"):
        with pytest.raises(SystemExit):
            await seed.session(role)


def test_main_dispatches_each_command_and_refuses_the_unknown(
    seed: types.ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []

    async def stub(name: str) -> dict[str, str]:
        calls.append(name)
        return {"command": name}

    monkeypatch.setattr(seed, "provider_id", lambda: stub("provider-id"))
    monkeypatch.setattr(seed, "site", lambda: stub("site"))
    monkeypatch.setattr(seed, "issue_code", lambda: stub("issue-code"))
    monkeypatch.setattr(seed, "session", lambda role: stub(f"session:{role}"))
    for command in (["provider-id"], ["site"], ["issue-code"], ["session", "copasst"], ["session"]):
        assert seed.main(command) == 0
    assert calls == [
        "provider-id",
        "site",
        "issue-code",
        "session:copasst",
        "session:administrator",
    ]
    assert seed.main([]) == 2
    assert seed.main(["borrar-todo"]) == 2
    printed = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    assert printed[0] == {"command": "provider-id"}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["sintetica", "real"])
async def test_provider_id_publishes_the_id_and_reports_the_breach_list_kind(
    seed: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    provider = uuid.UUID(int=12)

    async def connect() -> FakeConnection:
        return FakeConnection(rows=[{"organization_id": provider}])

    monkeypatch.setattr(seed, "connect", connect)
    monkeypatch.setattr(seed, "PROVIDER_FILE", tmp_path / "provider")
    monkeypatch.setattr(seed, "BREACH_KIND_FILE", tmp_path / "kind")
    (tmp_path / "kind").write_text(kind + chr(10))
    result = await seed.provider_id()
    assert result == {
        "command": "provider-id",
        "provider_organization_id": str(provider),
        "breach_list": kind,
    }
    assert (tmp_path / "provider").read_text().strip() == str(provider)


@pytest.mark.asyncio
async def test_provider_id_does_not_hide_a_missing_breach_list_kind(
    seed: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def connect() -> FakeConnection:
        return FakeConnection(rows=[{"organization_id": uuid.UUID(int=12)}])

    monkeypatch.setattr(seed, "connect", connect)
    monkeypatch.setattr(seed, "PROVIDER_FILE", tmp_path / "provider")
    monkeypatch.setattr(seed, "BREACH_KIND_FILE", tmp_path / "no-hay")
    assert (await seed.provider_id())["breach_list"] == "desconocida"
