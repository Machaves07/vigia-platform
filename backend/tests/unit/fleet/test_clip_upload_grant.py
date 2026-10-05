"""``ClipUploadGrant``, ``VerificationClip`` y el adaptador del almacén de clips (TASK-222).

Sin base ni almacén: el dominio puro (clave, vigencia, cabeceras, repetición, huérfanos y
verificación por metadatos) en sus bordes, ``ClipObjectStore`` sobre dobles del ``StoragePort``
(tope de la firma, consultas en paralelo con tope, cabeceras exactas, nunca ``get_object``) y las
piezas de las rutas del contrato que no necesitan base (alcance antes que esquema, respuesta con el
modelo estricto de U-01, la URL fuera de ``repr``).

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from botocore import exceptions as botocore_exceptions  # type: ignore[import-untyped]
from vigia_contracts.models.enumerations import ClipUploadPurpose, MediaKind, RejectionCode

from vigia_platform.fleet.adapters.s3.clip_storage import ClipObjectStore
from vigia_platform.fleet.application.clip_grants import IssuedClipGrant
from vigia_platform.fleet.domain.clip_upload_grant import (
    CLIP_GRANT_TTL,
    CLIP_MAX_BYTES,
    ORPHAN_AFTER,
    ClipContentType,
    ClipGrantRequestInvalid,
    ClipUploadGrant,
    OrphanOutcome,
    RepeatOutcome,
    clip_storage_key,
    orphan_outcome,
    repeat_outcome,
    sha256_from_headers,
    to_millisecond,
)
from vigia_platform.fleet.domain.enums import UploadGrantStatus
from vigia_platform.fleet.domain.verification_clip import (
    ClipCheckFailure,
    ObjectFacts,
    check_object,
)
from vigia_platform.identity.authz.context import NodeScope
from vigia_platform.node_api.rejections import NodeRejection
from vigia_platform.node_api.routes.clip_uploads import grant_document, zone_before_schema
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.storage import ObjectHead, PresignedRequest, StorageUnavailable

T0 = datetime(2026, 10, 4, 12, 0, 0, 123_456, tzinfo=UTC)
ORG = uuid.UUID("0192f0c4-1111-7000-8000-000000000001")
PLANT = uuid.UUID("0192f0c4-2222-7000-8000-000000000002")
ZONE = uuid.UUID("0192f0c4-3333-7000-8000-000000000003")
NODE = uuid.UUID("0192f0c4-4444-7000-8000-000000000004")
CLIP = uuid.UUID("0192f0c4-5555-7000-8000-000000000005")
DATA = b"clip sintetico difuminado" * 40
SHA = hashlib.sha256(DATA).hexdigest()


def _grant(**changes: Any) -> ClipUploadGrant:
    values: dict[str, Any] = {
        "clip_id": CLIP,
        "organization_id": ORG,
        "plant_id": PLANT,
        "zone_id": ZONE,
        "node_id": NODE,
        "purpose": ClipUploadPurpose.EVIDENCE,
        "media_kind": MediaKind.VIDEO,
        "content_type": "video/mp4",
        "sha256": SHA,
        "size_bytes": len(DATA),
        "issued_at": T0,
    }
    values.update(changes)
    return ClipUploadGrant.issue(**values)


def _facts(**changes: Any) -> ObjectFacts:
    values: dict[str, Any] = {
        "size_bytes": len(DATA),
        "sha256_hex": SHA,
        "content_type": "video/mp4",
        "metadata": {"vigia-anonymized": "1"},
    }
    values.update(changes)
    return ObjectFacts(**values)


# --- Clave, vigencia y cabeceras -----------------------------------------------------------------


def test_the_key_names_organization_plant_zone_and_node_and_the_extension_of_the_type() -> None:
    grant = _grant()
    assert grant.storage_key == (f"org/{ORG}/plant/{PLANT}/zone/{ZONE}/node/{NODE}/{CLIP}.mp4")
    assert len(grant.storage_key) <= 512
    image = _grant(media_kind=MediaKind.IMAGE, content_type="image/jpeg")
    assert image.storage_key.endswith(f"/{CLIP}.jpg")
    assert clip_storage_key(ORG, PLANT, ZONE, NODE, CLIP, ClipContentType.JPEG) == (
        image.storage_key
    )


def test_expires_exactly_fifteen_minutes_after_the_millisecond_issue() -> None:
    grant = _grant()
    assert grant.issued_at == datetime(2026, 10, 4, 12, 0, 0, 123_000, tzinfo=UTC)
    assert grant.expires_at - grant.issued_at == CLIP_GRANT_TTL == timedelta(minutes=15)
    assert grant.status is UploadGrantStatus.ISSUED
    with pytest.raises(ValueError, match="zona horaria"):
        to_millisecond(T0.replace(tzinfo=None))


def test_required_headers_are_exactly_type_checksum_and_anonymized_mark() -> None:
    grant = _grant()
    checksum = base64.b64encode(hashlib.sha256(DATA).digest()).decode()
    assert grant.required_headers == {
        "content-type": "video/mp4",
        "x-amz-checksum-sha256": checksum,
        "x-amz-meta-vigia-anonymized": "1",
    }
    assert len(grant.required_headers) <= 8
    assert grant.metadata_headers == {"x-amz-meta-vigia-anonymized": "1"}
    assert sha256_from_headers(grant.required_headers) == SHA


@pytest.mark.parametrize(
    "headers",
    [{}, {"x-amz-checksum-sha256": "no-base64"}, {"x-amz-checksum-sha256": "AAAA"}],
)
def test_a_stored_grant_without_a_valid_checksum_is_rejected(headers: Mapping[str, str]) -> None:
    with pytest.raises(ValueError, match="suma"):
        sha256_from_headers(headers)


def test_purpose_defaults_to_evidence_and_keeps_verification() -> None:
    assert _grant(purpose=None).purpose is ClipUploadPurpose.EVIDENCE
    assert _grant(purpose=ClipUploadPurpose.VERIFICATION).purpose is (
        ClipUploadPurpose.VERIFICATION
    )


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"media_kind": MediaKind.IMAGE}, "content_type"),
        ({"media_kind": MediaKind.VIDEO, "content_type": "image/jpeg"}, "content_type"),
        ({"content_type": "image/png"}, "content_type"),
        ({"sha256": "AB" * 32}, "sha256"),
        ({"sha256": "ab" * 31}, "sha256"),
        ({"size_bytes": 0}, "size_bytes"),
        ({"size_bytes": CLIP_MAX_BYTES + 1}, "size_bytes"),
        ({"size_bytes": True}, "size_bytes"),
    ],
)
def test_requests_outside_the_limits_name_their_field(changes: dict[str, Any], field: str) -> None:
    with pytest.raises(ClipGrantRequestInvalid) as raised:
        _grant(**changes)
    assert raised.value.field == field


@pytest.mark.parametrize("size", [1, CLIP_MAX_BYTES])
def test_sizes_at_the_bounds_are_admitted(size: int) -> None:
    assert _grant(size_bytes=size).max_size_bytes == size


# --- Repetición de la petición (PR-GOB-20) -------------------------------------------------------


def test_the_same_live_request_without_object_renews_the_url() -> None:
    grant = _grant()
    now = grant.expires_at - timedelta(milliseconds=1)
    assert repeat_outcome(grant, _grant(), now, uploaded=False) is RepeatOutcome.RENEW_URL


def test_the_same_expired_request_without_object_is_reissued_from_the_expiry_on() -> None:
    grant = _grant()
    assert (
        repeat_outcome(grant, _grant(), grant.expires_at, uploaded=False) is RepeatOutcome.REISSUE
    )
    reissued = grant.reissued(grant.expires_at + timedelta(seconds=3))
    assert reissued.issued_at == grant.expires_at + timedelta(seconds=3)
    assert reissued.expires_at - reissued.issued_at == CLIP_GRANT_TTL
    assert reissued.same_request(grant)
    with pytest.raises(ValueError, match="vencida"):
        grant.reissued(grant.expires_at - timedelta(milliseconds=1))
    with pytest.raises(ValueError, match="emitida"):
        replace(grant, status=UploadGrantStatus.USED).reissued(grant.expires_at)


@pytest.mark.parametrize("now_offset", [timedelta(0), timedelta(minutes=20)])
def test_an_uploaded_object_is_a_conflict_live_or_expired(now_offset: timedelta) -> None:
    grant = _grant()
    now = grant.issued_at + now_offset
    assert repeat_outcome(grant, _grant(), now, uploaded=True) is RepeatOutcome.CONFLICT


@pytest.mark.parametrize(
    "other",
    [
        {"sha256": "cd" * 32},
        {"size_bytes": len(DATA) + 1},
        {"purpose": ClipUploadPurpose.VERIFICATION},
        {"media_kind": MediaKind.IMAGE, "content_type": "image/jpeg"},
        {"zone_id": uuid.UUID(int=ZONE.int + 1)},
        {"node_id": uuid.UUID(int=NODE.int + 1)},
        {"plant_id": uuid.UUID(int=PLANT.int + 1)},
        {"organization_id": uuid.UUID(int=ORG.int + 1)},
    ],
)
def test_other_parameters_are_a_conflict(other: dict[str, Any]) -> None:
    grant = _grant()
    assert repeat_outcome(grant, _grant(**other), T0, uploaded=False) is RepeatOutcome.CONFLICT


@pytest.mark.parametrize(
    "status", [UploadGrantStatus.USED, UploadGrantStatus.EXPIRED, UploadGrantStatus.ORPHAN]
)
def test_a_closed_grant_is_a_conflict(status: UploadGrantStatus) -> None:
    grant = replace(_grant(), status=status)
    assert repeat_outcome(grant, _grant(), T0, uploaded=False) is RepeatOutcome.CONFLICT


# --- Huérfanos (BR-GOB-94) -----------------------------------------------------------------------


def test_an_uploaded_evidence_clip_becomes_orphan_exactly_from_24_hours() -> None:
    grant = _grant()
    before = grant.issued_at + ORPHAN_AFTER - timedelta(milliseconds=1)
    at = grant.issued_at + ORPHAN_AFTER
    assert orphan_outcome(grant, before, uploaded=True) is OrphanOutcome.KEEP
    assert orphan_outcome(grant, at, uploaded=True) is OrphanOutcome.ORPHAN
    assert orphan_outcome(grant, at, uploaded=False) is OrphanOutcome.EXPIRE


@pytest.mark.parametrize("uploaded", [True, False])
def test_a_verification_clip_never_becomes_orphan_nor_expires(uploaded: bool) -> None:
    grant = _grant(purpose=ClipUploadPurpose.VERIFICATION)
    later = grant.issued_at + 400 * ORPHAN_AFTER
    assert orphan_outcome(grant, later, uploaded=uploaded) is OrphanOutcome.KEEP


@pytest.mark.parametrize(
    "status", [UploadGrantStatus.USED, UploadGrantStatus.EXPIRED, UploadGrantStatus.ORPHAN]
)
def test_only_issued_grants_are_swept(status: UploadGrantStatus) -> None:
    grant = replace(_grant(), status=status)
    later = grant.issued_at + 2 * ORPHAN_AFTER
    assert orphan_outcome(grant, later, uploaded=True) is OrphanOutcome.KEEP


# --- Verificación por metadatos -------------------------------------------------------------------


def test_the_granted_object_passes() -> None:
    assert check_object(_grant(), _facts()) is None


@pytest.mark.parametrize(
    ("facts", "failure"),
    [
        (None, ClipCheckFailure.CLIP_MISSING),
        ({"size_bytes": len(DATA) + 1}, ClipCheckFailure.CLIP_TOO_LARGE),
        ({"size_bytes": CLIP_MAX_BYTES + 1, "sha256_hex": None}, ClipCheckFailure.CLIP_TOO_LARGE),
        ({"size_bytes": len(DATA) - 1}, ClipCheckFailure.CLIP_HASH_MISMATCH),
        ({"sha256_hex": "cd" * 32}, ClipCheckFailure.CLIP_HASH_MISMATCH),
        ({"sha256_hex": None}, ClipCheckFailure.CLIP_HASH_MISMATCH),
        ({"content_type": "image/jpeg"}, ClipCheckFailure.CLIP_HASH_MISMATCH),
        ({"content_type": None}, ClipCheckFailure.CLIP_HASH_MISMATCH),
        ({"metadata": {}}, ClipCheckFailure.CLIP_NOT_ANONYMIZED),
        ({"metadata": {"vigia-anonymized": "true"}}, ClipCheckFailure.CLIP_NOT_ANONYMIZED),
        ({"metadata": {"vigia-anonymized": "1 "}}, ClipCheckFailure.CLIP_NOT_ANONYMIZED),
        ({"metadata": {"vigia-anonymized": "0"}}, ClipCheckFailure.CLIP_NOT_ANONYMIZED),
        ({"metadata": {"vigia_anonymized": "1"}}, ClipCheckFailure.CLIP_NOT_ANONYMIZED),
    ],
)
def test_each_cause_is_told_apart_with_metadata_only(
    facts: dict[str, Any] | None, failure: ClipCheckFailure
) -> None:
    observed = None if facts is None else _facts(**facts)
    assert check_object(_grant(), observed) is failure
    # Los valores son exactamente los rejection_code del contrato.
    assert RejectionCode(failure.value).value == failure.value


# --- ClipObjectStore sobre dobles del StoragePort ------------------------------------------------


class FakeStorage:
    """Doble del puerto: ``head_object`` y ``presign_put``; cualquier otra operación falla."""

    def __init__(self, *, heads: Mapping[str, ObjectHead | None] | None = None) -> None:
        self.heads = dict(heads or {})
        self.signed: list[tuple[str, str, str, dict[str, str], timedelta]] = []
        self.headers_override: dict[str, str] | None = None
        self.in_flight = 0
        self.max_in_flight = 0
        self.fail_on: str | None = None
        self.hang = False

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        async def forbidden(*_: Any, **__: Any) -> Any:
            raise AssertionError(f"operación prohibida en clips: {name}")

        return forbidden

    async def head_object(self, key: str) -> ObjectHead | None:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.hang:
                await asyncio.Event().wait()
            await asyncio.sleep(0)
            if key == self.fail_on:
                raise StorageUnavailable("head_object")
            return self.heads.get(key)
        finally:
            self.in_flight -= 1

    async def presign_put(
        self,
        key: str,
        content_type: str,
        checksum_sha256: str,
        required_headers: Mapping[str, str],
        ttl: timedelta = CLIP_GRANT_TTL,
    ) -> PresignedRequest:
        if self.hang:
            await asyncio.Event().wait()
        if self.fail_on == "presign":
            raise botocore_exceptions.NoCredentialsError()
        self.signed.append((key, content_type, checksum_sha256, dict(required_headers), ttl))
        headers = {
            "content-type": content_type,
            "x-amz-checksum-sha256": base64.b64encode(bytes.fromhex(checksum_sha256)).decode(),
            **dict(required_headers),
        }
        if self.headers_override is not None:
            headers = self.headers_override
        return PresignedRequest(
            "PUT",
            f"https://bucket.s3.us-east-1.amazonaws.com/{key}?X-Amz-Signature=s",
            headers,
            T0 + ttl,
        )


def _head(key: str, data: bytes = DATA) -> ObjectHead:
    return ObjectHead(
        key=key,
        size_bytes=len(data),
        checksum_sha256=base64.b64encode(hashlib.sha256(data).digest()).decode(),
        checksum_type=None,
        content_type="video/mp4",
        metadata={"vigia-anonymized": "1"},
        version_id=None,
    )


def test_sign_uses_the_remaining_whole_seconds_and_exactly_the_granted_headers() -> None:
    grant = _grant()
    storage = FakeStorage()
    store = ClipObjectStore(storage)
    now = grant.issued_at + timedelta(minutes=10, milliseconds=400)
    upload = asyncio.run(store.sign(grant, lambda: now))
    ((key, content_type, checksum, metadata, ttl),) = storage.signed
    assert (key, content_type, checksum) == (grant.storage_key, "video/mp4", SHA)
    assert metadata == {"x-amz-meta-vigia-anonymized": "1"}
    assert ttl == timedelta(seconds=299)  # vence antes que la concesión, nunca después
    assert dict(upload.headers) == grant.required_headers


def test_sign_without_a_second_left_is_transient_and_signs_nothing() -> None:
    grant = _grant()
    storage = FakeStorage()
    with pytest.raises(StorageUnavailable):
        asyncio.run(
            ClipObjectStore(storage).sign(grant, lambda: grant.expires_at - timedelta(seconds=0.5))
        )
    assert storage.signed == []


def test_a_url_that_does_not_demand_exactly_the_granted_headers_is_never_returned() -> None:
    grant = _grant()
    storage = FakeStorage()
    storage.headers_override = {**grant.required_headers, "x-amz-meta-extra": "1"}
    with pytest.raises(RuntimeError, match="cabeceras"):
        asyncio.run(ClipObjectStore(storage).sign(grant, lambda: grant.issued_at))


def test_a_botocore_signing_failure_is_storage_unavailable() -> None:
    grant = _grant()
    storage = FakeStorage()
    storage.fail_on = "presign"
    with pytest.raises(StorageUnavailable):
        asyncio.run(ClipObjectStore(storage).sign(grant, lambda: grant.issued_at))


def test_the_signing_deadline_turns_a_hung_store_into_storage_unavailable() -> None:
    # El tope es el sujeto de la prueba: 1 s (nunca menos, retro 14), el almacén nunca responde.
    storage = FakeStorage()
    storage.hang = True
    store = ClipObjectStore(storage, presign_timeout_seconds=1.0)
    grant = _grant()

    async def grant_attempt() -> None:
        async with store.signing_deadline():
            await store.head(grant.storage_key)

    with pytest.raises(StorageUnavailable):
        asyncio.run(grant_attempt())


def test_heads_run_in_parallel_up_to_the_cap_and_never_download() -> None:
    keys = [f"org/o/plant/p/zone/z/node/n/{index}.mp4" for index in range(40)]
    storage = FakeStorage(heads={key: _head(key) for key in keys[::2]})
    store = ClipObjectStore(storage, max_parallel_heads=4)
    facts = asyncio.run(store.heads(keys))
    assert set(facts) == set(keys)
    assert all((facts[key] is not None) == (index % 2 == 0) for index, key in enumerate(keys))
    assert 1 < storage.max_in_flight <= 4


def test_one_failed_head_fails_the_whole_batch() -> None:
    keys = [f"k/{index}.mp4" for index in range(5)]
    storage = FakeStorage()
    storage.fail_on = keys[3]
    with pytest.raises(StorageUnavailable):
        asyncio.run(ClipObjectStore(storage).heads(keys))


def test_head_reads_the_full_object_checksum_and_the_user_metadata() -> None:
    key = "k/clip.mp4"
    storage = FakeStorage(heads={key: _head(key)})
    facts = asyncio.run(ClipObjectStore(storage).head(key))
    assert facts == ObjectFacts(len(DATA), SHA, "video/mp4", {"vigia-anonymized": "1"})


@pytest.mark.parametrize(
    "arguments",
    [
        {"presign_timeout_seconds": 0},
        {"head_timeout_seconds": -1},
        {"max_parallel_heads": 0},
    ],
)
def test_store_settings_are_checked(arguments: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ClipObjectStore(FakeStorage(), **arguments)


# --- Rutas del contrato: alcance antes que esquema y respuesta estricta --------------------------


def _node(zones: frozenset[uuid.UUID]) -> NodeScope:
    context = object.__new__(ScopeContext)
    return NodeScope(
        context=context,
        node_id=NODE,
        plant_id=PLANT,
        zone_ids=zones,
        certificate_serial="ab",
        credential_status="active",
    )


def test_a_zone_not_assigned_to_the_node_wins_over_the_schema() -> None:
    node = _node(frozenset({ZONE}))
    foreign = uuid.UUID(int=ZONE.int + 7)
    with pytest.raises(NodeRejection) as raised:
        zone_before_schema(node, {"zone_id": str(foreign), "size_bytes": "no es un entero"})
    assert raised.value.code is RejectionCode.NODE_ZONE_MISMATCH
    assert raised.value.field == "zone_id"
    zone_before_schema(node, {"zone_id": str(ZONE), "size_bytes": "no es un entero"})


@pytest.mark.parametrize(
    "body",
    [None, [], "x", {}, {"zone_id": 7}, {"zone_id": "no-uuid"}, {"zone_id": str(ZONE).upper()}],
)
def test_bodies_without_a_canonical_zone_are_left_to_the_schema(body: object) -> None:
    zone_before_schema(_node(frozenset()), body)


def test_the_response_is_the_strict_contract_grant_and_the_url_stays_out_of_repr() -> None:
    grant = _grant()
    upload = asyncio.run(ClipObjectStore(FakeStorage()).sign(grant, lambda: grant.issued_at))
    issued = IssuedClipGrant(grant, upload)
    document = grant_document(issued)
    value = document.to_json_value()
    assert value["required_headers"] == grant.required_headers
    assert value["expires_at"] == "2026-10-04T12:15:00.123Z"
    assert value["storage_key"] == grant.storage_key
    assert value["max_size_bytes"] == len(DATA)
    assert value["method"] == "PUT" and value["purpose"] == "evidence"
    assert "X-Amz-Signature" not in repr(issued)


def test_a_grant_the_contract_does_not_admit_never_leaves() -> None:
    grant = _grant()
    upload = PresignedRequest("PUT", "http://127.0.0.1:4566/x", grant.required_headers, T0)
    with pytest.raises(RuntimeError, match="contrato"):
        grant_document(IssuedClipGrant(grant, upload))
