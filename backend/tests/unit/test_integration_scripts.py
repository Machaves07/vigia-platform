"""Los guiones Python de ``local/integration/`` con dobles de boto3 y del sistema (TASK-545).

``edge_guard.main`` (no arranca con una IP ancha ni sin dominio, y no sirve sin certificado) y
``localstack/state.py`` (depósitos, clave KMS con su identificador y su material, errores que no se
confunden con «ya existe», qué entra en la copia). Sin Docker: ``test_compose_integration_file.py``
cubre el archivo de composición y ``integration/test_compose_integration.py`` el despliegue real.
"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import sys
import types
import uuid
from pathlib import Path
from typing import Any, Final

import pytest
from botocore.exceptions import ClientError

INTEGRATION: Final = Path(__file__).resolve().parents[3] / "local" / "integration"
ALIAS: Final = "alias/vigia-secrets"


def load_module(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    # Sin bytecode: un `__pycache__` en `local/integration/` rompería `test_no_minio_anywhere`, que
    # lee como texto todo lo que hay bajo `local/`.
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


@pytest.fixture(scope="module")
def guard() -> types.ModuleType:
    return load_module("vigia_edge_guard_main", INTEGRATION / "edge_guard.py")


@pytest.fixture(scope="module")
def state_module() -> types.ModuleType:
    return load_module("vigia_localstack_state_fakes", INTEGRATION / "localstack" / "state.py")


# --- edge_guard.main ----------------------------------------------------------------------------


class Launch:
    """Sustituye ``subprocess.run`` y ``os.execvp`` del guarda: anota qué habría lanzado."""

    def __init__(self, init_cert_status: int = 0) -> None:
        self.runs: list[list[str]] = []
        self.execs: list[list[str]] = []
        self.status = init_cert_status

    def run(self, command: list[str], **_: Any) -> types.SimpleNamespace:
        self.runs.append(command)
        return types.SimpleNamespace(returncode=self.status)

    def execvp(self, program: str, argv: list[str]) -> None:
        self.execs.append([program, *argv])


def run_guard(
    guard: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    environ: dict[str, str],
    launch: Launch,
) -> int:
    for name in ("VIGIA_EDGE_PUBLISH_IP", "VIGIA_DOMAIN"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(guard.subprocess, "run", launch.run)
    monkeypatch.setattr(guard.os, "execvp", launch.execvp)
    return int(guard.main())


def test_the_guard_starts_the_edge_after_creating_the_certificate(
    guard: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    launch = Launch()
    environ = {"VIGIA_EDGE_PUBLISH_IP": "100.64.1.2", "VIGIA_DOMAIN": "planta.test"}
    run_guard(guard, monkeypatch, environ, launch)
    assert launch.runs == [
        [
            "vigia-mtls-edge",
            *("init-cert", "--san", "app.planta.test", "--san", "nodes.planta.test"),
            *("--ip", "100.64.1.2"),
        ]
    ]
    assert launch.execs == [["vigia-mtls-edge", "vigia-mtls-edge", "serve"]]


@pytest.mark.parametrize("address", ["", "0.0.0.0", "::"])  # noqa: S104
def test_the_guard_refuses_a_wide_address_before_doing_anything(
    guard: types.ModuleType, monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    launch = Launch()
    environ = {"VIGIA_EDGE_PUBLISH_IP": address, "VIGIA_DOMAIN": "planta.test"}
    assert run_guard(guard, monkeypatch, environ, launch) == 2
    assert launch.runs == [] and launch.execs == []


def test_the_guard_refuses_to_start_without_a_domain(
    guard: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    launch = Launch()
    assert run_guard(guard, monkeypatch, {"VIGIA_EDGE_PUBLISH_IP": "127.0.0.1"}, launch) == 2
    assert launch.runs == [] and launch.execs == []


def test_the_guard_does_not_serve_if_the_certificate_cannot_be_created(
    guard: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    launch = Launch(init_cert_status=7)
    environ = {"VIGIA_EDGE_PUBLISH_IP": "127.0.0.1", "VIGIA_DOMAIN": "planta.test"}
    assert run_guard(guard, monkeypatch, environ, launch) == 7
    assert launch.execs == []


# --- state.py con clientes falsos ---------------------------------------------------------------


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code}}, "Operacion")


class FakeS3:
    def __init__(self, *, fail_with: str | None = None) -> None:
        self.buckets: list[str] = []
        self.versioned: list[str] = []
        self.objects: dict[tuple[str, str], bytes] = {}
        self.fail_with = fail_with

    def create_bucket(self, Bucket: str) -> None:
        if self.fail_with:
            raise client_error(self.fail_with)
        self.buckets.append(Bucket)

    def put_bucket_versioning(self, Bucket: str, **_: Any) -> None:
        self.versioned.append(Bucket)

    def head_object(self, Bucket: str, Key: str) -> None:
        if (Bucket, Key) not in self.objects:
            raise client_error("404")

    def put_object(self, Bucket: str, Key: str, Body: bytes, **_: Any) -> None:
        self.objects[(Bucket, Key)] = Body


def test_buckets_are_created_versioned_and_the_sentinel_written_once(
    state_module: types.ModuleType,
) -> None:
    s3 = FakeS3()
    state_module.ensure_buckets(s3, ["vigia-evidence", "vigia-edge"], "vigia-evidence")
    assert s3.buckets == s3.versioned == ["vigia-evidence", "vigia-edge"]
    assert s3.objects == {("vigia-evidence", "health/ready-sentinel"): b"centinela"}
    again = FakeS3(fail_with="BucketAlreadyOwnedByYou")
    again.objects = dict(s3.objects)
    state_module.ensure_buckets(again, ["vigia-evidence"], "vigia-evidence")
    assert again.versioned == ["vigia-evidence"]


def test_an_unexpected_bucket_error_is_not_swallowed(state_module: types.ModuleType) -> None:
    with pytest.raises(ClientError):
        state_module.ensure_buckets(FakeS3(fail_with="AccessDenied"), ["b"], "b")


class FakeKms:
    def __init__(
        self, *, present: bool, error: str = "NotFoundException", alias_error: str | None = None
    ) -> None:
        self.present = present
        self.error = error
        self.alias_error = alias_error
        self.created: list[dict[str, Any]] = []
        self.aliases: list[tuple[str, str]] = []

    def create_alias(self, AliasName: str, TargetKeyId: str) -> None:
        if self.alias_error:
            raise client_error(self.alias_error)
        self.aliases.append((AliasName, TargetKeyId))

    def describe_key(self, KeyId: str) -> None:
        if not self.present:
            raise client_error(self.error)

    def create_key(self, **arguments: Any) -> None:
        self.created.append(arguments)
        self.present = True


def test_the_secrets_key_keeps_its_id_and_material(
    state_module: types.ModuleType, tmp_path: Path
) -> None:
    material = base64.b64encode(bytes(range(32))).decode()
    file = tmp_path / "material"
    file.write_text(material + chr(10))
    kms = FakeKms(present=False)
    key_id = str(uuid.UUID(int=9))
    state_module.ensure_key(kms, key_id, file, ALIAS)
    (call,) = kms.created
    assert call["Tags"] == [
        {"TagKey": "_custom_id_", "TagValue": key_id},
        {"TagKey": "_custom_key_material_", "TagValue": material},
    ]
    assert kms.aliases == [(ALIAS, key_id)]
    state_module.ensure_key(kms, key_id, file, ALIAS)
    assert len(kms.created) == 1, "una clave que ya existe no se recrea"
    assert kms.aliases == [(ALIAS, key_id)] * 2, "el alias se asegura también con la clave viva"


def test_a_short_key_material_stops_the_restore(
    state_module: types.ModuleType, tmp_path: Path
) -> None:
    file = tmp_path / "material"
    file.write_text(base64.b64encode(b"corto").decode())
    with pytest.raises(SystemExit):
        state_module.ensure_key(FakeKms(present=False), "id", file, ALIAS)


def test_an_unexpected_kms_error_is_not_taken_for_a_missing_key(
    state_module: types.ModuleType, tmp_path: Path
) -> None:
    with pytest.raises(ClientError):
        state_module.ensure_key(FakeKms(present=False, error="AccessDenied"), "id", tmp_path, ALIAS)


def test_an_existing_alias_is_fine_and_any_other_alias_error_is_not(
    state_module: types.ModuleType,
) -> None:
    state_module.ensure_alias(
        FakeKms(present=True, alias_error="AlreadyExistsException"), ALIAS, "k"
    )
    with pytest.raises(ClientError):
        state_module.ensure_alias(FakeKms(present=True, alias_error="AccessDenied"), ALIAS, "k")


class DenyingManager:
    def create_secret(self, **_: Any) -> None:
        raise client_error("AccessDeniedException")


def test_an_unrelated_secrets_error_is_not_taken_for_an_existing_secret(
    state_module: types.ModuleType,
) -> None:
    with pytest.raises(ClientError):
        state_module._put_secret(DenyingManager(), {"name": "x", "string": "y"})


class MissingManager:
    def describe_secret(self, SecretId: str) -> None:
        raise client_error("ResourceNotFoundException")


class BrokenManager:
    def describe_secret(self, SecretId: str) -> None:
        raise client_error("InternalServiceError")


def test_a_missing_secret_is_told_apart_from_a_broken_service(
    state_module: types.ModuleType,
) -> None:
    assert state_module._secret_exists(MissingManager(), "x") is False
    with pytest.raises(ClientError):
        state_module._secret_exists(BrokenManager(), "x")


class FakePaginated:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages

    def paginate(self, **_: Any) -> list[dict[str, Any]]:
        return self.pages


class FakeSecretsRead:
    def __init__(self) -> None:
        self.values = {
            "vivo": {"ARN": "arn:x-AbCdEf", "VersionId": "v1", "SecretString": "uno"},
            "binario": {"ARN": "arn:y-ZyXwVu", "VersionId": "v2", "SecretBinary": b"abc"},
        }

    def get_paginator(self, name: str) -> FakePaginated:
        assert name == "list_secrets"
        listed = [
            {"Name": "vivo"},
            {"Name": "binario"},
            {"Name": "borrado", "DeletedDate": 1},
            {"Name": "desaparecido"},
        ]
        return FakePaginated([{"SecretList": listed}])

    def get_secret_value(self, SecretId: str) -> dict[str, Any]:
        if SecretId == "desaparecido":
            raise client_error("ResourceNotFoundException")
        return self.values[SecretId]


class FakeObjectsRead:
    def get_paginator(self, name: str) -> FakePaginated:
        assert name == "list_objects_v2"
        listing = [
            {"Key": "ca/root.pem", "Size": 3},
            {"Key": "ca/enorme.bin", "Size": 2 * 1024 * 1024},
        ]
        return FakePaginated([{"Contents": listing}, {}])

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        assert Key == "ca/root.pem", "no se descarga lo que no se guarda"
        return {"ContentType": "application/x-pem-file", "Body": io.BytesIO(b"PEM")}


def test_the_snapshot_keeps_arn_version_and_small_objects_only(
    state_module: types.ModuleType,
) -> None:
    snapshot = state_module.collect(FakeSecretsRead(), FakeObjectsRead(), "vigia-edge")
    names = [entry["name"] for entry in snapshot["secrets"]]
    assert names == ["binario", "vivo"], "los borrados no se copian y el orden es estable"
    vivo = next(entry for entry in snapshot["secrets"] if entry["name"] == "vivo")
    assert (vivo["arn"], vivo["version_id"], vivo["string"]) == ("arn:x-AbCdEf", "v1", "uno")
    binary = next(entry for entry in snapshot["secrets"] if entry["name"] == "binario")
    assert base64.b64decode(binary["binary"]) == b"abc"
    assert [item["key"] for item in snapshot["objects"]] == ["ca/root.pem"]


def test_an_unknown_snapshot_format_stops_the_restore(
    state_module: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = tmp_path / "copia.json"
    snapshot.write_text(json.dumps({"version": 99}))
    monkeypatch.setattr(state_module, "SNAPSHOT", snapshot)
    with pytest.raises(SystemExit):
        state_module.load_snapshot()
    monkeypatch.setattr(state_module, "SNAPSHOT", tmp_path / "no-existe.json")
    assert state_module.load_snapshot()["secrets"] == []
