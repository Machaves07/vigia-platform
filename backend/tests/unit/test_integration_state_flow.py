"""El recorrido completo de ``localstack/state.py`` con clientes falsos (TASK-545, VIG-393).

``restore`` y ``save`` de punta a punta (lo que corre el gancho ``ready.d`` de LocalStack), la
limpieza de temporales si la publicación de la copia falla, los ajustes obligatorios y la orden.
Complementa a ``test_integration_scripts.py`` (piezas sueltas) y al despliegue real de
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
from botocore.exceptions import ClientError

STATE: Final = (
    Path(__file__).resolve().parents[3] / "local" / "integration" / "localstack" / "state.py"
)


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("vigia_state_flow", STATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["vigia_state_flow"] = module
    # Sin bytecode: un `__pycache__` en `local/integration/` rompería `test_no_minio_anywhere`, que
    # lee como texto todo lo que hay bajo `local/`.
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    (tmp_path / "state").mkdir()
    for name, content in (
        ("kms-secrets.material", base64.b64encode(bytes(range(32))).decode()),
        ("kms-secrets.id", str(uuid.UUID(int=4))),
        ("app.pw", "clave-de-la-app"),
        ("enrollment-source.key", base64.b64encode(b"k" * 32).decode()),
    ):
        (tmp_path / "state" / name).write_text(content + chr(10))
    monkeypatch.setattr(module, "STATE", tmp_path / "state")
    monkeypatch.setattr(module, "SNAPSHOT", tmp_path / "state" / "localstack-snapshot.json")
    monkeypatch.setattr(module, "MARKER", tmp_path / "marcador")
    for name, value in {
        "VIGIA_BUCKETS": "vigia-evidence vigia-edge",
        "VIGIA_EVIDENCE_BUCKET": "vigia-evidence",
        "VIGIA_EDGE_BUCKET": "vigia-edge",
        "VIGIA_KMS_KEY_ALIAS": "alias/vigia-secrets",
        "VIGIA_DB_SECRET_NAME": "vigia/integration/database/app",
        "VIGIA_ENROLLMENT_SECRET_NAME": "vigia/integration/enrollment/source-key",
    }.items():
        monkeypatch.setenv(name, value)
    return module


def error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code}}, "Operacion")


class Cloud:
    """Los tres servicios de LocalStack en memoria (lo justo para ``restore`` y ``save``)."""

    def __init__(self) -> None:
        self.buckets: dict[str, dict[str, bytes]] = {}
        self.keys: dict[str, dict[str, Any]] = {}
        self.aliases: dict[str, str] = {}
        self.secrets: dict[str, dict[str, Any]] = {}

    # S3
    def create_bucket(self, Bucket: str) -> None:
        self.buckets.setdefault(Bucket, {})

    def put_bucket_versioning(self, **_: Any) -> None:
        return None

    def head_object(self, Bucket: str, Key: str) -> None:
        if Key not in self.buckets[Bucket]:
            raise error("404")

    def put_object(self, Bucket: str, Key: str, Body: bytes, **_: Any) -> None:
        self.buckets[Bucket][Key] = Body

    def get_paginator(self, name: str) -> Any:
        cloud = self

        class Pages:
            def paginate(self, **arguments: Any) -> list[dict[str, Any]]:
                if name == "list_secrets":
                    return [{"SecretList": [{"Name": key} for key in cloud.secrets]}]
                bucket = cloud.buckets[arguments["Bucket"]]
                return [{"Contents": [{"Key": k, "Size": len(v)} for k, v in bucket.items()]}]

        return Pages()

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        import io

        return {"ContentType": "text/plain", "Body": io.BytesIO(self.buckets[Bucket][Key])}

    # KMS
    def describe_key(self, KeyId: str) -> None:
        if KeyId not in self.keys:
            raise error("NotFoundException")

    def create_key(self, Tags: list[dict[str, str]], **_: Any) -> None:
        tags = {item["TagKey"]: item["TagValue"] for item in Tags}
        self.keys[tags["_custom_id_"]] = tags

    def create_alias(self, AliasName: str, TargetKeyId: str) -> None:
        if AliasName in self.aliases:
            raise error("AlreadyExistsException")
        self.aliases[AliasName] = TargetKeyId

    # Secrets Manager
    def create_secret(self, Name: str, **value: Any) -> None:
        if Name in self.secrets:
            raise error("ResourceExistsException")
        self.secrets[Name] = {"name": Name, **value}

    def put_secret_value(self, SecretId: str, **value: Any) -> None:
        self.secrets[SecretId].update(value)

    def describe_secret(self, SecretId: str) -> None:
        if SecretId not in self.secrets:
            raise error("ResourceNotFoundException")

    def get_secret_value(self, SecretId: str) -> dict[str, Any]:
        stored = self.secrets[SecretId]
        found: dict[str, Any] = {
            "ARN": f"arn:aws:secretsmanager:::secret:{SecretId}-AbCdEf",
            "VersionId": str(uuid.UUID(int=len(SecretId))),
        }
        for field in ("SecretString", "SecretBinary"):
            if field in stored:
                found[field] = stored[field]
        return found


def attach(state: types.ModuleType, monkeypatch: pytest.MonkeyPatch, cloud: Cloud) -> None:
    monkeypatch.setattr(state, "_client", lambda service: cloud)


def test_restore_on_an_empty_cloud_builds_everything_and_marks_itself_done(
    state: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    cloud = Cloud()
    attach(state, monkeypatch, cloud)
    assert state.restore() == 0
    assert set(cloud.buckets) == {"vigia-evidence", "vigia-edge"}
    assert cloud.buckets["vigia-evidence"]["health/ready-sentinel"] == b"centinela"
    assert cloud.aliases == {"alias/vigia-secrets": str(uuid.UUID(int=4))}
    database = json.loads(cloud.secrets["vigia/integration/database/app"]["SecretString"])
    assert database["password"] == "clave-de-la-app"  # noqa: S105
    assert (database["host"], database["username"]) == ("postgres", "vigia_app")
    source = cloud.secrets["vigia/integration/enrollment/source-key"]
    assert source["SecretBinary"] == b"k" * 32
    assert state.MARKER.exists()


def test_a_secret_from_the_snapshot_wins_over_the_derived_one(
    state: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = {
        "version": 1,
        "secrets": [
            {"name": "vigia/integration/database/app", "string": "de-la-copia", "arn": None},
            {"name": "vigia/integration/signing/catalog-1", "string": "firma", "arn": None},
        ],
        "objects": [{"key": "ca/root.pem", "content_type": "text/plain", "body": "UEVN"}],
    }
    state.SNAPSHOT.write_text(json.dumps(snapshot))
    cloud = Cloud()
    attach(state, monkeypatch, cloud)
    state.restore()
    assert cloud.secrets["vigia/integration/database/app"]["SecretString"] == "de-la-copia"
    assert cloud.secrets["vigia/integration/signing/catalog-1"]["SecretString"] == "firma"
    assert cloud.buckets["vigia-edge"]["ca/root.pem"] == b"PEM"
    assert "vigia/integration/enrollment/source-key" in cloud.secrets


def test_save_after_restore_round_trips_through_a_second_restore(
    state: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = Cloud()
    attach(state, monkeypatch, first)
    state.restore()
    first.buckets["vigia-edge"]["ca/root.pem"] = b"RAIZ"
    first.secrets["vigia/integration/signing/gate-1"] = {"name": "x", "SecretString": "otra"}
    assert state.save() == 0
    saved = json.loads(state.SNAPSHOT.read_text())
    assert sorted(item["name"] for item in saved["secrets"]) == [
        "vigia/integration/database/app",
        "vigia/integration/enrollment/source-key",
        "vigia/integration/signing/gate-1",
    ]
    assert state.SNAPSHOT.stat().st_mode & 0o777 == 0o600
    again = Cloud()
    attach(state, monkeypatch, again)
    state.MARKER.unlink()
    state.restore()
    assert again.secrets["vigia/integration/signing/gate-1"]["SecretString"] == "otra"
    assert again.buckets["vigia-edge"]["ca/root.pem"] == b"RAIZ"


def test_a_failed_publication_leaves_no_temporary_and_keeps_the_old_copy(
    state: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state" / "copia.json"
    state.write_atomically(target, "buena")

    def broken(source: object, destination: object) -> None:
        raise OSError("disco lleno")

    monkeypatch.setattr(state.os, "replace", broken)
    with pytest.raises(OSError, match="disco lleno"):
        state.write_atomically(target, "nueva")
    assert target.read_text() == "buena"
    assert [path.name for path in (tmp_path / "state").iterdir() if path.name.startswith(".")] == []


def test_an_unexpected_sentinel_error_is_not_taken_for_a_missing_object(
    state: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    cloud = Cloud()

    def denied(Bucket: str, Key: str) -> None:
        raise error("AccessDenied")

    monkeypatch.setattr(cloud, "head_object", denied)
    attach(state, monkeypatch, cloud)
    with pytest.raises(ClientError):
        state.restore()
    assert not state.MARKER.exists(), "un restore que falló no deja el marcador"


def test_a_missing_setting_stops_the_script(
    state: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIGIA_PRUEBA", "valor")
    assert state._setting("VIGIA_PRUEBA") == "valor"
    monkeypatch.setenv("VIGIA_PRUEBA", "   ")
    with pytest.raises(SystemExit):
        state._setting("VIGIA_PRUEBA")
    monkeypatch.delenv("VIGIA_PRUEBA")
    with pytest.raises(SystemExit):
        state._setting("VIGIA_PRUEBA")


def test_main_runs_the_order_asked_and_refuses_any_other(
    state: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(state, "restore", lambda: calls.append("restore") or 0)
    monkeypatch.setattr(state, "save", lambda: calls.append("save") or 0)
    assert state.main(["state.py", "restore"]) == 0
    assert state.main(["state.py", "save"]) == 0
    assert calls == ["restore", "save"]
    for arguments in (["state.py"], ["state.py", "borrar"], ["state.py", "save", "extra"]):
        assert state.main(arguments) == 2
    assert calls == ["restore", "save"]


def test_no_snapshot_yet_is_an_empty_one(state: types.ModuleType) -> None:
    assert not state.SNAPSHOT.exists()
    assert state.load_snapshot() == {"version": 1, "secrets": [], "objects": []}
