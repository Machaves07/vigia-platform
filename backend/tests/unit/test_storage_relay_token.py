"""El relevo firmado del almacén sin red (TASK-607, A-130): token, adaptador y configuración.

- **Token**: cambiar un solo byte de la carga o de la firma lo invalida (propiedad); el
  vencimiento se mide con el reloj inyectado (en el milisegundo exacto deja de valer); la firma se
  compara con ``hmac.compare_digest``; un token auténtico de otro depósito, de una clave ``ca/`` o
  de otro método no vale.
- **Adaptador**: las URL son del mismo origen, con el token en la consulta; ``presign_put`` solo
  firma adjuntos de cierre (un clip del nodo sigue siendo ``storage_unavailable``, A-108) y
  ``presign_part`` nunca; no se construye para ``vigia-edge`` ni ``vigia-archive``.
- **Configuración**: ``VIGIA_STORE_RELAY=1`` solo arranca en ``local`` e ``integration`` y con su
  secreto; sin relevo, ``s3_storage`` da el ``S3Storage`` de siempre.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.store_relay_support import (
    BUCKET,
    EDGE_BUCKET,
    KEY,
    ORIGIN,
    START,
    FakeS3,
    attachment_key,
    clip_key,
    relay_storage,
)
from vigia_platform.fleet.adapters.s3.clip_storage import ClipObjectStore
from vigia_platform.fleet.domain.clip_upload_grant import ClipContentType, ClipUploadGrant
from vigia_platform.shared import storage_relay
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.runtime.config import (
    RuntimeConfig,
    RuntimeConfigInvalid,
    StoreRelayForbidden,
)
from vigia_platform.shared.runtime.core import s3_storage, store_relay_settings
from vigia_platform.shared.secrets import SecretNotFound
from vigia_platform.shared.storage import (
    PRESIGN_ATTACHMENT_GET_MAX_TTL,
    PRESIGN_GET_MAX_TTL,
    S3Storage,
    StorageSettings,
    StorageUnavailable,
)
from vigia_platform.shared.storage_relay import (
    RELAY_PATH,
    RelayGrant,
    RelayMethod,
    RelayOutcome,
    RelayTokenRejected,
    RelayTokens,
    StoreRelaySettings,
    StoreRelayStorage,
)

SHA256 = hashlib.sha256(b"adjunto").hexdigest()


def clock() -> SimulatedClock:
    return SimulatedClock(START)


def tokens(at: SimulatedClock | None = None, bucket: str = BUCKET) -> RelayTokens:
    return RelayTokens(KEY, at if at is not None else clock(), bucket)


def grant(**changes: Any) -> RelayGrant:
    values: dict[str, Any] = {
        "bucket": BUCKET,
        "key": attachment_key(),
        "version_id": "v1",
        "method": RelayMethod.GET,
        "expires_at_ms": 1_768_478_700_000,  # START + 5 min
        "disposition": "attachment",
        "response_content_type": "application/octet-stream",
    }
    values.update(changes)
    return RelayGrant(**values)


def token_of(url: str) -> str:
    (value,) = httpx.URL(url).params.get_list("token")
    return value


def rejected(issuer: RelayTokens, token: object, method: RelayMethod) -> RelayOutcome:
    with pytest.raises(RelayTokenRejected) as raised:
        issuer.verify(token, method)
    return raised.value.outcome


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# --- Token ---------------------------------------------------------------------------------------


def test_a_token_round_trips_to_the_same_grant() -> None:
    issuer = tokens()
    original = grant(metadata={"vigia-anonymized": "1"}, method=RelayMethod.PUT)
    assert issuer.verify(issuer.issue(original), RelayMethod.PUT) == original


segments = st.from_regex(r"[a-z0-9][a-z0-9-]{0,11}", fullmatch=True)
grants = st.builds(
    lambda parts, version, method, expires: grant(
        key="/".join(parts), version_id=version, method=method, expires_at_ms=expires
    ),
    st.lists(segments, min_size=1, max_size=6),
    st.one_of(st.none(), st.text(alphabet="abcdefXYZ0123456789", min_size=1, max_size=32)),
    st.sampled_from(list(RelayMethod)),
    st.integers(min_value=1_768_478_400_001, max_value=1_900_000_000_000),
)


@given(grants, st.data())
def test_changing_any_byte_of_the_payload_or_the_signature_is_refused(
    original: RelayGrant, data: st.DataObject
) -> None:
    issuer = tokens()
    body, signature = issuer.issue(original).split(".")
    assert issuer.verify(f"{body}.{signature}", original.method) == original
    part = data.draw(st.sampled_from(["payload", "signature"]))
    raw = bytearray(_unb64(body if part == "payload" else signature))
    index = data.draw(st.integers(min_value=0, max_value=len(raw) - 1))
    raw[index] ^= data.draw(st.integers(min_value=1, max_value=255))
    altered = f"{_b64(bytes(raw))}.{signature}" if part == "payload" else f"{body}.{_b64(raw)}"
    assert rejected(issuer, altered, original.method) is RelayOutcome.DENIED


@given(grants, st.data())
def test_changing_any_character_of_the_token_is_refused(
    original: RelayGrant, data: st.DataObject
) -> None:
    issuer = tokens()
    token = issuer.issue(original)
    index = data.draw(st.integers(min_value=0, max_value=len(token) - 1))
    other = data.draw(
        st.sampled_from("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.=")
    )
    if other == token[index]:
        return
    altered = token[:index] + other + token[index + 1 :]
    assert rejected(issuer, altered, original.method) is RelayOutcome.DENIED


def test_a_signature_with_other_trailing_bits_is_not_canonical_and_is_refused() -> None:
    issuer = tokens()
    body, signature = issuer.issue(grant()).split(".")
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    last = alphabet.index(signature[-1])
    twin = signature[:-1] + alphabet[last ^ 1]  # 43 caracteres: los 2 bits bajos sobran
    assert _unb64(twin) == _unb64(signature)
    assert rejected(issuer, f"{body}.{twin}", RelayMethod.GET) is RelayOutcome.DENIED


@pytest.mark.parametrize(
    "token",
    [None, 7, "", "solo-una-parte", "a.b.c", "x" * 5000, "%%%.%%%", "ñ.ñ"],
    ids=["nada", "numero", "vacio", "una", "tres", "largo", "simbolos", "no-ascii"],
)
def test_a_token_without_the_shape_is_refused(token: object) -> None:
    assert rejected(tokens(), token, RelayMethod.GET) is RelayOutcome.DENIED


def test_a_token_signed_with_another_key_is_refused() -> None:
    other = RelayTokens(bytes(32), clock(), BUCKET).issue(grant())
    assert rejected(tokens(), other, RelayMethod.GET) is RelayOutcome.DENIED


def test_an_authentic_payload_without_its_exact_shape_is_refused() -> None:
    issuer = tokens()
    body = _b64(json.dumps({"v": 1, "b": BUCKET}).encode())
    forged = f"{body}.{_b64(issuer._mac(body))}"
    assert rejected(issuer, forged, RelayMethod.GET) is RelayOutcome.DENIED


def test_the_signature_is_compared_with_compare_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bytes, bytes]] = []

    def spy(left: bytes, right: bytes) -> bool:
        calls.append((left, right))
        return left == right

    monkeypatch.setattr(storage_relay, "compare_digest", spy)
    issuer = tokens()
    token = issuer.issue(grant())
    issuer.verify(token, RelayMethod.GET)
    assert len(calls) == 1
    assert calls[0][1] == _unb64(token.split(".")[1])


def test_the_token_expires_at_its_millisecond_by_the_injected_clock() -> None:
    at = clock()
    issuer = tokens(at)
    _, expires_at_ms = issuer.expires_at_ms(300)
    token = issuer.issue(grant(expires_at_ms=expires_at_ms))
    at.advance(299.999)
    assert issuer.verify(token, RelayMethod.GET).expires_at_ms == expires_at_ms
    at.advance(0.001)
    assert rejected(issuer, token, RelayMethod.GET) is RelayOutcome.EXPIRED


def test_an_authentic_token_for_another_bucket_is_refused() -> None:
    issuer = tokens()
    assert rejected(issuer, issuer.issue(grant(bucket=EDGE_BUCKET)), RelayMethod.GET) is (
        RelayOutcome.DENIED
    )


def test_an_authentic_token_for_a_ca_key_is_refused() -> None:
    issuer = tokens()
    token = issuer.issue(grant(key="ca/root.pem"))
    assert rejected(issuer, token, RelayMethod.GET) is RelayOutcome.DENIED


def test_a_token_for_one_method_does_not_serve_the_other() -> None:
    issuer = tokens()
    assert rejected(issuer, issuer.issue(grant()), RelayMethod.PUT) is RelayOutcome.DENIED
    put = issuer.issue(grant(method=RelayMethod.PUT))
    assert rejected(issuer, put, RelayMethod.GET) is RelayOutcome.DENIED


def test_a_short_key_is_refused_and_the_key_never_shows() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        RelayTokens(bytes(31), clock(), BUCKET)
    issuer = tokens()
    assert KEY.hex() not in repr(issuer) and repr(issuer) == "RelayTokens()"
    assert "org/" not in repr(grant())


# --- Adaptador ----------------------------------------------------------------------------------


def test_read_urls_are_same_origin_with_the_token_in_the_query() -> None:
    at = clock()
    relay = relay_storage(FakeS3(), at)
    signed = asyncio.run(relay.presign_get(attachment_key(), version_id="v3"))
    url = httpx.URL(signed.url)
    assert f"{url.scheme}://{url.host}:{url.port}" == ORIGIN
    assert url.path == RELAY_PATH
    assert list(url.params.keys()) == ["token"]
    assert (signed.method, dict(signed.headers)) == ("GET", {})
    assert signed.expires_at == START + PRESIGN_GET_MAX_TTL
    read = relay.tokens.verify(token_of(signed.url), RelayMethod.GET)
    assert (read.key, read.version_id, read.disposition) == (attachment_key(), "v3", None)


def test_an_attachment_read_keeps_the_disposition_the_type_and_fifteen_minutes() -> None:
    relay = relay_storage(FakeS3(), clock())
    signed = asyncio.run(relay.presign_attachment_get(attachment_key(), version_id="v1"))
    assert signed.expires_at == START + PRESIGN_ATTACHMENT_GET_MAX_TTL
    read = relay.tokens.verify(token_of(signed.url), RelayMethod.GET)
    assert (read.disposition, read.response_content_type) == (
        "attachment",
        "application/octet-stream",
    )
    with pytest.raises(ValueError, match="vigencia"):
        asyncio.run(relay.presign_get(attachment_key(), timedelta(minutes=6)))


def test_an_upload_url_signs_the_type_the_sum_and_the_metadata() -> None:
    relay = relay_storage(FakeS3(), clock())
    signed = asyncio.run(
        relay.presign_put(attachment_key(), "image/png", SHA256, {"x-amz-meta-origen": "cierre"})
    )
    checksum = base64.b64encode(bytes.fromhex(SHA256)).decode()
    assert signed.method == "PUT"
    assert dict(signed.headers) == {
        "content-type": "image/png",
        "x-amz-checksum-sha256": checksum,
        "x-amz-meta-origen": "cierre",
    }
    upload = relay.tokens.verify(token_of(signed.url), RelayMethod.PUT)
    assert upload.signed_headers() == dict(signed.headers)


def test_a_node_clip_is_never_signed_for_upload_and_the_grant_stays_unavailable() -> None:
    client = FakeS3()
    relay = relay_storage(client, clock())
    with pytest.raises(StorageUnavailable) as raised:
        asyncio.run(relay.presign_put(clip_key(), "video/mp4", SHA256, {}))
    assert raised.value.code == "storage_unavailable"
    assert client.calls == []


def test_the_node_clip_grant_answers_storage_unavailable_through_the_relay() -> None:
    client = FakeS3()
    content = next(iter(ClipContentType))
    clip_grant = ClipUploadGrant.issue(
        clip_id=uuid.UUID(int=5),
        organization_id=uuid.UUID(int=1),
        plant_id=uuid.UUID(int=2),
        zone_id=uuid.UUID(int=3),
        node_id=uuid.UUID(int=4),
        purpose=None,
        media_kind=content.media_kind,
        content_type=content.value,
        sha256=SHA256,
        size_bytes=1,
        issued_at=START,
    )
    with pytest.raises(StorageUnavailable) as raised:
        asyncio.run(ClipObjectStore(relay_storage(client, clock())).sign(clip_grant, lambda: START))
    assert raised.value.code == "storage_unavailable"
    assert client.calls == []


def test_multipart_uploads_are_never_signed() -> None:
    with pytest.raises(StorageUnavailable):
        asyncio.run(relay_storage(FakeS3(), clock()).presign_part(attachment_key(), "u", 1, SHA256))


@pytest.mark.parametrize("bucket", [EDGE_BUCKET, "vigia-archive"])
def test_the_relay_is_never_built_for_the_edge_or_the_archive(bucket: str) -> None:
    with pytest.raises(ValueError, match="evidencias"):
        relay_storage(FakeS3(), clock(), bucket=bucket)


def test_the_relay_never_shows_its_key() -> None:
    relay = relay_storage(FakeS3(), clock())
    settings = StoreRelaySettings(key=KEY, public_origin=ORIGIN)
    for text in (repr(relay), repr(settings), repr(relay.tokens)):
        assert KEY.hex() not in text and str(KEY) not in text


# --- Configuración y composición ---------------------------------------------------------------


def environ(environment: str = "integration", **changes: str) -> dict[str, str]:
    values = {
        "VIGIA_ENVIRONMENT": environment,
        "AWS_REGION": "us-east-1",
        "VIGIA_DB_APP_SECRET": "vigia/integration/database/app",
        "VIGIA_SIGNING_SECRET_PREFIX": "vigia/integration/signing/",
        "VIGIA_SECRETS_KEY_ARN": "alias/vigia-secrets",
        "VIGIA_EVIDENCE_BUCKET": BUCKET,
        "VIGIA_ARCHIVE_BUCKET": "vigia-archive",
        "VIGIA_EDGE_BUCKET": EDGE_BUCKET,
        "VIGIA_STORE_RELAY": "1",
        "VIGIA_STORE_RELAY_SECRET": "vigia/integration/store-relay/key",
    }
    values.update(changes)
    return values


@pytest.mark.parametrize("environment", ["local", "integration"])
def test_the_relay_is_accepted_in_local_and_integration(environment: str) -> None:
    config = RuntimeConfig.from_environ(environ(environment))
    assert (config.store_relay, config.store_relay_secret) == (
        True,
        "vigia/integration/store-relay/key",
    )


@pytest.mark.parametrize("environment", ["pilot", "staging-1", "test"])
def test_the_relay_outside_local_and_integration_does_not_start(environment: str) -> None:
    with pytest.raises(StoreRelayForbidden) as raised:
        RuntimeConfig.from_environ(environ(environment))
    assert raised.value.variable == "VIGIA_STORE_RELAY"
    assert raised.value.code == "store_relay_forbidden"


def test_the_relay_without_its_secret_does_not_start() -> None:
    without = environ()
    del without["VIGIA_STORE_RELAY_SECRET"]
    with pytest.raises(RuntimeConfigInvalid) as raised:
        RuntimeConfig.from_environ(without)
    assert raised.value.variable == "VIGIA_STORE_RELAY_SECRET"


@pytest.mark.parametrize("value", ["true", "yes", "2", " 1"])
def test_the_relay_flag_is_only_zero_or_one(value: str) -> None:
    with pytest.raises(RuntimeConfigInvalid) as raised:
        RuntimeConfig.from_environ(environ(VIGIA_STORE_RELAY=value))
    assert raised.value.variable == "VIGIA_STORE_RELAY"


def test_without_the_relay_the_storage_is_the_plain_s3_one() -> None:
    plain = environ("pilot", VIGIA_STORE_RELAY="0")
    config = RuntimeConfig.from_environ(plain)
    assert config.store_relay is False
    relay = StoreRelaySettings(key=KEY, public_origin=ORIGIN)
    storage = s3_storage(config, BUCKET, clock(), relay=relay)
    assert type(storage) is S3Storage
    assert storage._settings == StorageSettings(
        bucket=BUCKET, endpoint_url="https://s3.us-east-1.amazonaws.com", region="us-east-1"
    )


def test_with_the_relay_only_the_evidence_bucket_is_relayed() -> None:
    config = RuntimeConfig.from_environ(environ())
    relay = StoreRelaySettings(key=KEY, public_origin=ORIGIN)
    assert type(s3_storage(config, BUCKET, clock(), relay=relay)) is StoreRelayStorage
    assert type(s3_storage(config, BUCKET, clock())) is S3Storage
    for other in (EDGE_BUCKET, "vigia-archive"):
        assert type(s3_storage(config, other, clock(), relay=relay)) is S3Storage


class _Secrets:
    def __init__(self, value: bytes | Exception) -> None:
        self.value = value
        self.asked: list[str] = []

    async def get(self, arn: str) -> bytes:
        self.asked.append(arn)
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def test_the_relay_key_is_read_from_its_secret() -> None:
    config = RuntimeConfig.from_environ(environ())
    secrets = _Secrets(KEY)
    settings = asyncio.run(store_relay_settings(config, secrets, ORIGIN))
    assert settings is not None and settings.key == KEY and settings.public_origin == ORIGIN
    assert secrets.asked == ["vigia/integration/store-relay/key"]
    off = RuntimeConfig.from_environ(environ(VIGIA_STORE_RELAY="0"))
    assert asyncio.run(store_relay_settings(off, _Secrets(KEY), ORIGIN)) is None


@pytest.mark.parametrize(
    "value",
    [bytes(range(31)), SecretNotFound(), ValueError("sin valor binario")],
    ids=["corta", "inexistente", "texto"],
)
def test_a_bad_relay_secret_stops_the_start_and_names_only_the_variable(
    value: bytes | Exception,
) -> None:
    config = RuntimeConfig.from_environ(environ())
    with pytest.raises(RuntimeConfigInvalid) as raised:
        asyncio.run(store_relay_settings(config, _Secrets(value), ORIGIN))
    assert raised.value.variable == "VIGIA_STORE_RELAY_SECRET"
    if isinstance(value, bytes):
        assert value.hex() not in str(raised.value) and str(value) not in str(raised.value)


def test_the_relay_variables_are_known_to_the_error_line() -> None:
    names = {variable.name for variable in storage_relay_variables()}
    assert names == {"VIGIA_STORE_RELAY", "VIGIA_STORE_RELAY_SECRET"}


def storage_relay_variables() -> list[Any]:
    from vigia_platform.shared.runtime.config import KNOWN_VARIABLES, VARIABLES

    found = [v for v in VARIABLES if v.name.startswith("VIGIA_STORE_RELAY")]
    assert {v.name for v in found} <= KNOWN_VARIABLES
    return found
