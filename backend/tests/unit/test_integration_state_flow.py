"""El recorrido completo de ``localstack/state.py`` con clientes falsos (TASK-545, VIG-393).

``restore`` y ``save`` de punta a punta (lo que corre el gancho ``ready.d`` de LocalStack), la
limpieza de temporales si la publicación de la copia falla, los ajustes obligatorios y la orden.
Desde TASK-608, el espejo de ``vigia-evidence`` en ``/almacen``: ida y vuelta con las lecturas de la
plataforma, copia incremental, bajas solo tras restaurar, integridad, tope, alcance, permisos,
registros sin claves y coherencia del índice con subidas concurrentes.
Complementa a ``test_integration_scripts.py`` (piezas sueltas) y al despliegue real de
``integration/test_compose_integration.py``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import io
import itertools
import json
import re
import sys
import threading
import types
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast

import pytest
from botocore.exceptions import ClientError, FlexibleChecksumError

from vigia_platform.ledger.application.evidence_read import _verified_bytes
from vigia_platform.loop.adapters.attachments.read import (
    AttachmentUnreadable,
    ClosureAttachmentReader,
)
from vigia_platform.loop.adapters.attachments.store import ClosureObjectStorage, StoreCircuit
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.storage import ObjectHead, PresignedRequest, _object_head

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
        ("store-relay.key", base64.b64encode(b"r" * 32).decode()),
    ):
        (tmp_path / "state" / name).write_text(content + chr(10))
    monkeypatch.setattr(module, "STATE", tmp_path / "state")
    monkeypatch.setattr(module, "SNAPSHOT", tmp_path / "state" / "localstack-snapshot.json")
    monkeypatch.setattr(module, "MARKER", tmp_path / "marcador")
    monkeypatch.setattr(module, "ALMACEN", tmp_path / "almacen")
    monkeypatch.setattr(module, "EVIDENCE_MARKER", tmp_path / "marcador-evidencias")
    monkeypatch.delenv("VIGIA_ALMACEN_MAX_BYTES", raising=False)
    monkeypatch.delenv("VIGIA_EVENT_LOG", raising=False)
    for name, value in {
        "VIGIA_BUCKETS": "vigia-evidence vigia-edge",
        "VIGIA_EVIDENCE_BUCKET": "vigia-evidence",
        "VIGIA_EDGE_BUCKET": "vigia-edge",
        "VIGIA_KMS_KEY_ALIAS": "alias/vigia-secrets",
        "VIGIA_DB_SECRET_NAME": "vigia/integration/database/app",
        "VIGIA_ENROLLMENT_SECRET_NAME": "vigia/integration/enrollment/source-key",
        "VIGIA_STORE_RELAY_SECRET_NAME": "vigia/integration/store-relay/key",
    }.items():
        monkeypatch.setenv(name, value)
    return module


def error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code}}, "Operacion")


PLAIN: Final[dict[str, Any]] = {
    "content_type": "text/plain",
    "metadata": {},
    "checksum": None,
    "version_id": "v0",
    "etag": '"0"',
}
"""Lo que tiene un objeto escrito a mano en ``buckets`` (sin ``put_object``)."""


def sha256_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


class Cloud:
    """Los tres servicios de LocalStack en memoria (lo justo para ``restore`` y ``save``).

    S3 como LocalStack con versiones: cada ``put_object`` da otra ``VersionId`` y otro ``ETag``,
    guarda la suma SHA-256 solo si llega ``ChecksumSHA256`` (y la rechaza con ``BadDigest`` si no
    es la de los bytes) y la devuelve con ``ChecksumMode=ENABLED``. Cuenta los ``get_object`` por
    depósito; ``before_get`` simula lo que hace la plataforma entre el listado y la descarga.
    """

    def __init__(self) -> None:
        self.buckets: dict[str, dict[str, bytes]] = {}
        self.details: dict[tuple[str, str], dict[str, Any]] = {}
        self.gets: Counter[str] = Counter()
        self.before_get: Callable[[str, str], None] | None = None
        self.versions = itertools.count(1)
        self.lock = threading.Lock()
        self.keys: dict[str, dict[str, Any]] = {}
        self.aliases: dict[str, str] = {}
        self.secrets: dict[str, dict[str, Any]] = {}

    # S3
    def create_bucket(self, Bucket: str) -> None:
        self.buckets.setdefault(Bucket, {})

    def put_bucket_versioning(self, **_: Any) -> None:
        return None

    def _describe(self, Bucket: str, Key: str, ChecksumMode: str | None) -> dict[str, Any]:
        if Key not in self.buckets[Bucket]:
            raise error("NoSuchKey")
        stored = self.details.get((Bucket, Key), PLAIN)
        response: dict[str, Any] = {
            "ContentLength": len(self.buckets[Bucket][Key]),
            "ContentType": stored["content_type"],
            "Metadata": dict(stored["metadata"]),
            "VersionId": stored["version_id"],
            "ETag": stored["etag"],
        }
        if ChecksumMode == "ENABLED" and stored["checksum"] is not None:
            response.update(ChecksumSHA256=stored["checksum"], ChecksumType="FULL_OBJECT")
        return response

    def head_object(self, Bucket: str, Key: str, ChecksumMode: str | None = None) -> dict[str, Any]:
        with self.lock:
            if Key not in self.buckets[Bucket]:
                raise error("404")
            return self._describe(Bucket, Key, ChecksumMode)

    def put_object(
        self,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str = "binary/octet-stream",
        Metadata: dict[str, str] | None = None,
        ChecksumSHA256: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        if ChecksumSHA256 is not None and ChecksumSHA256 != sha256_b64(Body):
            raise error("BadDigest")
        with self.lock:
            self.buckets[Bucket][Key] = Body
            self.details[(Bucket, Key)] = {
                "content_type": ContentType,
                "metadata": dict(Metadata or {}),
                "checksum": ChecksumSHA256,
                "version_id": f"v{next(self.versions)}",
                "etag": '"' + hashlib.sha256(Body).hexdigest()[:32] + '"',
            }
        return {}

    def delete_object(self, Bucket: str, Key: str) -> None:
        with self.lock:
            del self.buckets[Bucket][Key]

    def get_paginator(self, name: str) -> Any:
        cloud = self

        class Pages:
            def paginate(self, **arguments: Any) -> list[dict[str, Any]]:
                if name == "list_secrets":
                    return [{"SecretList": [{"Name": key} for key in cloud.secrets]}]
                bucket = arguments["Bucket"]
                with cloud.lock:
                    listed = [
                        {
                            "Key": k,
                            "Size": len(v),
                            "ETag": cloud.details.get((bucket, k), PLAIN)["etag"],
                        }
                        for k, v in cloud.buckets[bucket].items()
                    ]
                return [{"Contents": listed}]

        return Pages()

    def get_object(self, Bucket: str, Key: str, ChecksumMode: str | None = None) -> dict[str, Any]:
        self.gets[Bucket] += 1
        if self.before_get is not None:
            self.before_get(Bucket, Key)
        with self.lock:
            response = self._describe(Bucket, Key, ChecksumMode)
            response["Body"] = io.BytesIO(self.buckets[Bucket][Key])
        return response

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
    relay = cloud.secrets["vigia/integration/store-relay/key"]
    assert relay["SecretBinary"] == b"r" * 32
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
    assert "vigia/integration/store-relay/key" in cloud.secrets


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
        "vigia/integration/store-relay/key",
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


# --- Espejo de vigia-evidence (TASK-608) --------------------------------------------------------

EVIDENCE: Final = "vigia-evidence"
CLIP_KEY: Final = "clips/org-sintetica/zona-1/clip-0001.mp4"
CLIP: Final = b"clip sintetico difuminado " * 64
CLIP_METADATA: Final = {"vigia-anonymized": "true", "vigia-episode": "ep-sintetico-1"}
ATTACHMENT_KEY: Final = "closure/org-sintetica/cierre-1/adjunto-1.pdf"
ATTACHMENT: Final = b"%PDF-1.7 adjunto sintetico del cierre " * 32
ATTACHMENT_METADATA: Final = {"vigia-closure": "cierre-sintetico-1"}
COUNTS_LINE: Final = re.compile(r"[a-z_ ]+: (?:[a-z_]+=\d+)(?: [a-z_]+=\d+)*")


def put(cloud: Cloud, key: str, body: bytes, content_type: str, metadata: dict[str, str]) -> None:
    cloud.put_object(
        Bucket=EVIDENCE,
        Key=key,
        Body=body,
        ContentType=content_type,
        Metadata=metadata,
        ChecksumSHA256=sha256_b64(body),
    )


def started(state: types.ModuleType, monkeypatch: pytest.MonkeyPatch) -> Cloud:
    """Un LocalStack recién restaurado con un clip y un adjunto subidos por la plataforma."""
    cloud = Cloud()
    attach(state, monkeypatch, cloud)
    monkeypatch.setenv("VIGIA_BUCKETS", "vigia-evidence vigia-edge vigia-archive")
    assert state.restore() == 0
    put(cloud, CLIP_KEY, CLIP, "video/mp4", CLIP_METADATA)
    put(cloud, ATTACHMENT_KEY, ATTACHMENT, "application/pdf", ATTACHMENT_METADATA)
    return cloud


def reboot(state: types.ModuleType, monkeypatch: pytest.MonkeyPatch) -> Cloud:
    """Un reinicio de LocalStack: memoria vacía y las dos marcas borradas (``boot.d``)."""
    cloud = Cloud()
    attach(state, monkeypatch, cloud)
    state.MARKER.unlink()
    state.EVIDENCE_MARKER.unlink()
    assert state.restore() == 0
    return cloud


def index(state: types.ModuleType) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((state.ALMACEN / "indice.json").read_text())
    return cast("dict[str, Any]", document["objetos"])


def blobs(state: types.ModuleType) -> set[str]:
    return {path.name for path in (state.ALMACEN / "objetos").iterdir()}


def platform_head(cloud: Cloud, key: str) -> ObjectHead:
    """Lo que ve la plataforma (``S3Storage.head_object``: ``HEAD`` con ``ChecksumMode``)."""
    return _object_head(key, cloud.head_object(Bucket=EVIDENCE, Key=key, ChecksumMode="ENABLED"))


class PlatformStorage:
    """``ClosureObjectStorage`` sobre la nube falsa: lo que usa ``ClosureAttachmentReader``."""

    def __init__(self, cloud: Cloud) -> None:
        self.cloud = cloud

    async def head_object(self, key: str) -> ObjectHead | None:
        try:
            return platform_head(self.cloud, key)
        except ClientError:
            return None

    async def presign_attachment_get(
        self, key: str, ttl: timedelta = timedelta(minutes=15), *, version_id: str | None = None
    ) -> PresignedRequest:
        expires = datetime(2026, 11, 23, 12, 15, tzinfo=UTC)
        return PresignedRequest("GET", f"https://relevo.test/{version_id}", {}, expires)


def read_attachment(cloud: Cloud) -> PresignedRequest:
    clock = SimulatedClock(datetime(2026, 11, 23, 12, tzinfo=UTC))
    reader = ClosureAttachmentReader(
        storage=cast(ClosureObjectStorage, PlatformStorage(cloud)), circuit=StoreCircuit(clock)
    )
    digest = hashlib.sha256(ATTACHMENT).hexdigest()
    return asyncio.run(reader.read_url(ATTACHMENT_KEY, sha256=digest, size_bytes=len(ATTACHMENT)))


def clip_is_readable(cloud: Cloud) -> bool:
    """La condición de ``EvidenceService.url_lectura`` antes de firmar (``_verified_bytes``)."""
    head = platform_head(cloud, CLIP_KEY)
    row = types.SimpleNamespace(size_bytes=len(CLIP), sha256=hashlib.sha256(CLIP).hexdigest())
    return head.version_id is not None and _verified_bytes(cast(Any, row), head)


def test_a_clip_and_an_attachment_survive_a_localstack_restart(
    state: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = started(state, monkeypatch)
    assert state.save() == 0
    heads = {key: platform_head(before, key) for key in (CLIP_KEY, ATTACHMENT_KEY)}
    after = reboot(state, monkeypatch)
    assert after.buckets[EVIDENCE][CLIP_KEY] == CLIP
    assert after.buckets[EVIDENCE][ATTACHMENT_KEY] == ATTACHMENT
    for key, old in heads.items():
        new = platform_head(after, key)
        assert new.full_object_sha256_hex == old.full_object_sha256_hex is not None, key
        assert (new.content_type, dict(new.metadata)) == (old.content_type, dict(old.metadata))
        assert new.size_bytes == old.size_bytes
    assert dict(platform_head(after, CLIP_KEY).metadata) == CLIP_METADATA
    assert dict(platform_head(after, ATTACHMENT_KEY).metadata) == ATTACHMENT_METADATA
    assert clip_is_readable(after), "la lectura de un clip no firmaría"
    assert read_attachment(after).method == "GET"


def test_without_the_restore_the_platform_reads_fail_closed(
    state: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La otra cara de la prueba anterior: sin suma, la plataforma no firma (``m_sin_suma``)."""
    cloud = started(state, monkeypatch)
    for key, body in ((CLIP_KEY, CLIP), (ATTACHMENT_KEY, ATTACHMENT)):
        cloud.put_object(Bucket=EVIDENCE, Key=key, Body=body, ContentType="video/mp4")
    assert not clip_is_readable(cloud)
    with pytest.raises(AttachmentUnreadable):
        read_attachment(cloud)


def test_a_second_save_without_changes_downloads_nothing(
    state: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    cloud = started(state, monkeypatch)
    assert state.save() == 0
    assert cloud.gets[EVIDENCE] == 2
    copied = (state.ALMACEN / "indice.json").read_text()
    assert state.save() == 0
    assert cloud.gets[EVIDENCE] == 2, "un save sin cambios no descarga nada"
    assert (state.ALMACEN / "indice.json").read_text() == copied
    put(cloud, CLIP_KEY, CLIP + b"otra version", "video/mp4", CLIP_METADATA)
    assert state.save() == 0
    assert cloud.gets[EVIDENCE] == 3, "solo la clave cambiada se descarga"
    assert index(state)[CLIP_KEY]["size"] == len(CLIP) + len(b"otra version")


def test_nothing_leaves_the_index_until_this_boot_restored_it(
    state: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    cloud = started(state, monkeypatch)
    put(cloud, "closure/org-sintetica/cierre-2/copia.pdf", ATTACHMENT, "application/pdf", {})
    assert state.save() == 0
    shared = hashlib.sha256(ATTACHMENT).hexdigest()
    state.EVIDENCE_MARKER.unlink()  # la restauración de este arranque no ha terminado
    cloud.delete_object(Bucket=EVIDENCE, Key=CLIP_KEY)
    cloud.delete_object(Bucket=EVIDENCE, Key=ATTACHMENT_KEY)
    assert state.save() == 0
    assert set(index(state)) == {
        CLIP_KEY,
        ATTACHMENT_KEY,
        "closure/org-sintetica/cierre-2/copia.pdf",
    }
    assert hashlib.sha256(CLIP).hexdigest() in blobs(state)
    state.EVIDENCE_MARKER.write_text("restaurado\n")
    assert state.save() == 0
    assert set(index(state)) == {"closure/org-sintetica/cierre-2/copia.pdf"}
    assert blobs(state) == {shared}, "el blob huérfano se borra; el que otra clave usa, no"


def test_a_failed_evidence_restore_leaves_neither_mark(
    state: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    started(state, monkeypatch)
    assert state.save() == 0
    broken = Cloud()

    def refused(**_: Any) -> None:
        raise error("InternalError")

    def missing(**_: Any) -> None:
        raise error("404")

    monkeypatch.setattr(broken, "put_object", refused)
    monkeypatch.setattr(broken, "head_object", missing)
    monkeypatch.setattr(state, "ensure_buckets", lambda *_: None)
    attach(state, monkeypatch, broken)
    state.MARKER.unlink()
    state.EVIDENCE_MARKER.unlink()
    with pytest.raises(ClientError):
        state.restore()
    assert not state.MARKER.exists() and not state.EVIDENCE_MARKER.exists()


def test_an_altered_blob_is_not_restored_and_is_counted(
    state: types.ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started(state, monkeypatch)
    assert state.save() == 0
    blob = state.ALMACEN / "objetos" / hashlib.sha256(CLIP).hexdigest()
    blob.write_bytes(CLIP[:-1] + b"X")
    capsys.readouterr()
    after = reboot(state, monkeypatch)
    assert CLIP_KEY not in after.buckets[EVIDENCE]
    assert after.buckets[EVIDENCE][ATTACHMENT_KEY] == ATTACHMENT
    lines = capsys.readouterr()
    restored = next(line for line in lines.out.splitlines() if line.startswith("restore almacen:"))
    counts = dict(item.split("=") for item in restored.split(": ", 1)[1].split())
    assert counts == {"restaurados": "1", "presentes": "0", "objeto_corrupto": "1"}
    assert lines.err.splitlines() == ["objeto_corrupto: objetos=1"]


def test_a_download_whose_sum_is_not_the_stores_is_not_saved(
    state: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    cloud = started(state, monkeypatch)
    cloud.details[(EVIDENCE, CLIP_KEY)]["checksum"] = sha256_b64(b"otros bytes")
    counts = state.mirror_evidence(cloud, EVIDENCE, restored=True, limit=state.max_bytes())
    assert counts["suma_distinta"] == 1 and counts["nuevos"] == 1
    assert set(index(state)) == {ATTACHMENT_KEY}
    assert blobs(state) == {hashlib.sha256(ATTACHMENT).hexdigest()}
    again = state.mirror_evidence(cloud, EVIDENCE, restored=True, limit=state.max_bytes())
    assert cloud.gets[EVIDENCE] == 2, "un objeto rechazado y sin cambios no se vuelve a bajar"
    assert again["suma_distinta"] == 0


def test_a_checksum_failure_while_reading_is_not_saved(
    state: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    cloud = started(state, monkeypatch)
    original = cloud.get_object

    def failing(**arguments: Any) -> dict[str, Any]:
        response = original(**arguments)

        class Body:
            def read(self) -> bytes:
                raise FlexibleChecksumError(error_msg="suma distinta")

        response["Body"] = Body()
        return response

    monkeypatch.setattr(cloud, "get_object", failing)
    counts = state.mirror_evidence(cloud, EVIDENCE, restored=True, limit=state.max_bytes())
    assert counts["suma_distinta"] == 2 and index(state) == {} and blobs(state) == set()


def test_an_object_without_a_full_object_sum_is_not_mirrored_nor_downloaded_again(
    state: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    cloud = started(state, monkeypatch)
    cloud.put_object(Bucket=EVIDENCE, Key="otros/sin-suma.bin", Body=b"sin suma")
    counts = state.mirror_evidence(cloud, EVIDENCE, restored=True, limit=state.max_bytes())
    assert counts["sin_suma"] == 1 and "otros/sin-suma.bin" not in index(state)
    state.mirror_evidence(cloud, EVIDENCE, restored=True, limit=state.max_bytes())
    assert cloud.gets[EVIDENCE] == 3
    assert (
        state.full_object_sha256({"ChecksumSHA256": sha256_b64(b"x"), "ChecksumType": "COMPOSITE"})
        is None
    )
    assert state.full_object_sha256({"ChecksumSHA256": "no es base64"}) is None
    assert state.full_object_sha256({"ChecksumSHA256": base64.b64encode(b"corta").decode()}) is None


def test_a_full_store_copies_nothing_new_and_keeps_what_it_has(
    state: types.ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cloud = started(state, monkeypatch)
    assert state.save() == 0
    kept = (state.ALMACEN / "indice.json").read_text()
    stored = {name: (state.ALMACEN / "objetos" / name).read_bytes() for name in blobs(state)}
    monkeypatch.setenv("VIGIA_ALMACEN_MAX_BYTES", str(len(CLIP) + len(ATTACHMENT) + 10))
    put(cloud, "clips/org-sintetica/zona-1/clip-0002.mp4", b"n" * 64, "video/mp4", {})
    capsys.readouterr()
    assert state.save() == 0
    assert set(index(state)) == {CLIP_KEY, ATTACHMENT_KEY}
    assert {
        name: (state.ALMACEN / "objetos" / name).read_bytes() for name in blobs(state)
    } == stored
    assert (
        json.loads((state.ALMACEN / "indice.json").read_text())["objetos"]
        == json.loads(kept)["objetos"]
    )
    (line,) = capsys.readouterr().err.splitlines()
    code, counts = line.split(": ", 1)
    assert code == "almacen_lleno"
    assert dict(item.split("=") for item in counts.split()) == {
        "omitidos": "1",
        "guardados": "2",
        "bytes": str(len(CLIP) + len(ATTACHMENT)),
        "tope": str(len(CLIP) + len(ATTACHMENT) + 10),
    }
    assert state.save() == 0
    assert capsys.readouterr().err == "", "el aviso no se repite en cada vuelta"


def test_the_store_limit_setting(state: types.ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    assert state.max_bytes() == 2 * 1024**3
    monkeypatch.setenv("VIGIA_ALMACEN_MAX_BYTES", "1024")
    assert state.max_bytes() == 1024
    for wrong in ("dos gigas", "-1"):
        monkeypatch.setenv("VIGIA_ALMACEN_MAX_BYTES", wrong)
        with pytest.raises(SystemExit):
            state.max_bytes()


def test_only_evidence_without_the_sentinel_enters_the_store(
    state: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    cloud = started(state, monkeypatch)
    for bucket, key in (("vigia-edge", "ca/root.pem"), ("vigia-archive", "paquete.zip")):
        cloud.put_object(Bucket=bucket, Key=key, Body=b"fuera", ChecksumSHA256=sha256_b64(b"fuera"))
    assert state.save() == 0
    assert set(index(state)) == {CLIP_KEY, ATTACHMENT_KEY}
    assert blobs(state) == {hashlib.sha256(body).hexdigest() for body in (CLIP, ATTACHMENT)}
    assert cloud.gets["vigia-archive"] == 0
    assert cloud.gets[EVIDENCE] == 2, "el centinela no se descarga"


def test_blobs_and_index_are_private_and_the_lines_carry_only_counts(
    state: types.ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cloud = started(state, monkeypatch)
    log = state.ALMACEN.parent / "registro.log"
    monkeypatch.setenv("VIGIA_EVENT_LOG", str(log))
    monkeypatch.setenv("VIGIA_ALMACEN_MAX_BYTES", str(len(CLIP) + len(ATTACHMENT)))
    cloud.details[(EVIDENCE, ATTACHMENT_KEY)]["checksum"] = sha256_b64(b"otros")
    assert state.save() == 0
    put(
        cloud,
        "clips/org-sintetica/zona-1/clip-0003.mp4",
        b"m" * len(ATTACHMENT) * 2,
        "video/mp4",
        {},
    )
    assert state.save() == 0
    (state.ALMACEN / "objetos" / hashlib.sha256(CLIP).hexdigest()).write_bytes(b"alterado")
    reboot(state, monkeypatch)
    assert state.ALMACEN.joinpath("objetos").stat().st_mode & 0o777 == 0o700
    for path in [state.ALMACEN / "indice.json", *(state.ALMACEN / "objetos").iterdir()]:
        assert path.stat().st_mode & 0o777 == 0o600, path.name
    output = capsys.readouterr()
    lines = [*output.out.splitlines(), *output.err.splitlines(), *log.read_text().splitlines()]
    almacen = [line for line in lines if not line.startswith(("save:", "restore:"))]
    assert {line.split(":")[0] for line in almacen} == {
        "save almacen",
        "restore almacen",
        "objeto_no_guardado",
        "almacen_lleno",
        "objeto_corrupto",
    }
    for line in almacen:
        assert COUNTS_LINE.fullmatch(line), line
    forbidden = {CLIP_KEY, ATTACHMENT_KEY, "clips/org-sintetica/zona-1/clip-0003.mp4"}
    forbidden |= {hashlib.sha256(body).hexdigest() for body in (CLIP, ATTACHMENT)}
    for line in lines:
        assert set(re.split(r"[\s=:,()]+", line)).isdisjoint(forbidden), line


def test_an_upload_between_the_listing_and_the_download_keeps_the_index_coherent(
    state: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    cloud = started(state, monkeypatch)
    newer = ATTACHMENT + b" segunda version"

    def platform_writes(bucket: str, key: str) -> None:
        if key == ATTACHMENT_KEY:
            put(cloud, ATTACHMENT_KEY, newer, "application/pdf", ATTACHMENT_METADATA)
        if key == CLIP_KEY:
            cloud.delete_object(Bucket=bucket, Key=key)

    cloud.before_get = platform_writes
    state.mirror_evidence(cloud, EVIDENCE, restored=True, limit=state.max_bytes())
    cloud.before_get = None
    entries = index(state)
    assert set(entries) == {ATTACHMENT_KEY}, "una clave borrada a medias no entra"
    assert entries[ATTACHMENT_KEY]["sha256"] == hashlib.sha256(newer).hexdigest()
    assert entries[ATTACHMENT_KEY]["etag"] == cloud.details[(EVIDENCE, ATTACHMENT_KEY)]["etag"]
    gets = cloud.gets[EVIDENCE]
    state.mirror_evidence(cloud, EVIDENCE, restored=True, limit=state.max_bytes())
    assert cloud.gets[EVIDENCE] == gets, "lo guardado es la versión vigente: no se vuelve a bajar"


def coherent(state: types.ModuleType, uploaded: dict[str, set[str]]) -> None:
    for key, entry in index(state).items():
        data = (state.ALMACEN / "objetos" / entry["sha256"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"] and len(data) == entry["size"]
        assert entry["sha256"] in uploaded[key], "el índice nombra bytes que nadie subió"


def test_saves_running_while_the_platform_uploads_keep_the_index_coherent(
    state: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    cloud = started(state, monkeypatch)
    keys = [f"closure/org-sintetica/cierre-{n}/adjunto.pdf" for n in range(4)]
    uploaded: dict[str, set[str]] = {key: set() for key in keys}
    uploaded[CLIP_KEY] = {hashlib.sha256(CLIP).hexdigest()}
    uploaded[ATTACHMENT_KEY] = {hashlib.sha256(ATTACHMENT).hexdigest()}
    stop = threading.Event()

    def platform() -> None:
        for round_ in itertools.count():
            if stop.is_set():
                return
            key = keys[round_ % len(keys)]
            body = f"adjunto sintetico {round_}".encode() * 50
            uploaded[key].add(hashlib.sha256(body).hexdigest())
            put(cloud, key, body, "application/pdf", {})
            if round_ % 3 == 0:
                cloud.delete_object(Bucket=EVIDENCE, Key=key)
            stop.wait(0.001)

    def saver(errors: list[BaseException]) -> None:
        try:
            for _ in range(15):
                state.mirror_evidence(cloud, EVIDENCE, restored=True, limit=state.max_bytes())
        except BaseException as failure:  # se comprueba abajo, en el hilo principal
            errors.append(failure)

    errors: list[BaseException] = []
    writer = threading.Thread(target=platform)
    savers = [threading.Thread(target=saver, args=(errors,)) for _ in range(2)]
    writer.start()
    for thread in savers:
        thread.start()
    for thread in savers:
        thread.join(timeout=60)
    stop.set()
    writer.join(timeout=10)
    assert errors == [] and not any(thread.is_alive() for thread in savers)
    coherent(state, uploaded)
    state.mirror_evidence(cloud, EVIDENCE, restored=True, limit=state.max_bytes())
    coherent(state, uploaded)
    referenced = {entry["sha256"] for entry in index(state).values()}
    assert blobs(state) == referenced, "ni blobs huérfanos ni índices que nombran blobs borrados"
    listed = set(cloud.buckets[EVIDENCE]) - {"health/ready-sentinel"}
    assert set(index(state)) == listed


def test_an_unknown_index_format_stops_save_and_restore(
    state: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    started(state, monkeypatch)
    (state.ALMACEN / "indice.json").write_text(json.dumps({"version": 99}))
    with pytest.raises(SystemExit):
        state.save()
    with pytest.raises(SystemExit):
        reboot(state, monkeypatch)
    assert not state.MARKER.exists(), "sin índice legible el contenedor no da sano"


def test_restore_leaves_what_localstack_already_has_alone(
    state: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    cloud = started(state, monkeypatch)
    assert state.save() == 0
    versions = {
        key: cloud.details[(EVIDENCE, key)]["version_id"] for key in (CLIP_KEY, ATTACHMENT_KEY)
    }
    counts = state.restore_evidence(cloud, EVIDENCE)
    assert counts == {"restaurados": 0, "presentes": 2, "objeto_corrupto": 0}
    assert {key: cloud.details[(EVIDENCE, key)]["version_id"] for key in versions} == versions


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param({"sha256": "../../state/app.pw"}, id="suma-que-no-es-hex"),
        pytest.param({"sha256": "f" * 64}, id="blob-que-no-existe"),
        pytest.param({"size": len(CLIP) + 1}, id="tamano-distinto"),
    ],
)
def test_an_index_entry_that_does_not_match_its_blob_is_not_restored(
    state: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tamper: dict[str, Any]
) -> None:
    started(state, monkeypatch)
    assert state.save() == 0
    document = json.loads((state.ALMACEN / "indice.json").read_text())
    document["objetos"][CLIP_KEY].update(tamper)
    (state.ALMACEN / "indice.json").write_text(json.dumps(document))
    after = reboot(state, monkeypatch)
    assert CLIP_KEY not in after.buckets[EVIDENCE]
    assert after.buckets[EVIDENCE][ATTACHMENT_KEY] == ATTACHMENT


def test_a_storage_error_is_not_taken_for_a_missing_object(
    state: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    cloud = started(state, monkeypatch)
    assert state.save() == 0
    kept = (state.ALMACEN / "indice.json").read_text()

    def denied(**_: Any) -> None:
        raise error("AccessDenied")

    put(cloud, "clips/org-sintetica/zona-1/clip-0004.mp4", b"z" * 10, "video/mp4", {})
    monkeypatch.setattr(cloud, "get_object", denied)
    with pytest.raises(ClientError):
        state.mirror_evidence(cloud, EVIDENCE, restored=True, limit=state.max_bytes())
    assert (state.ALMACEN / "indice.json").read_text() == kept
    monkeypatch.setattr(cloud, "head_object", denied)
    with pytest.raises(ClientError):
        state.restore_evidence(cloud, EVIDENCE)


def test_a_damaged_blob_is_repaired_when_the_same_bytes_arrive_again(
    state: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    cloud = started(state, monkeypatch)
    assert state.save() == 0
    blob = state.ALMACEN / "objetos" / hashlib.sha256(CLIP).hexdigest()
    blob.write_bytes(b"danado")
    put(cloud, "clips/org-sintetica/zona-2/clip-copia.mp4", CLIP, "video/mp4", CLIP_METADATA)
    assert state.save() == 0
    assert blob.read_bytes() == CLIP


def test_room_again_copies_what_the_full_store_left_out_without_a_warning(
    state: types.ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cloud = started(state, monkeypatch)
    monkeypatch.setenv("VIGIA_ALMACEN_MAX_BYTES", str(len(CLIP)))
    assert state.save() == 0
    assert set(index(state)) == {CLIP_KEY}
    capsys.readouterr()
    monkeypatch.setenv("VIGIA_ALMACEN_MAX_BYTES", str(len(CLIP) + len(ATTACHMENT)))
    assert state.save() == 0
    assert set(index(state)) == {CLIP_KEY, ATTACHMENT_KEY}
    assert capsys.readouterr().err == ""
    assert cloud.gets[EVIDENCE] == 2
