"""``shared.storage`` sin red: URL firmadas contra el punto regional fijo, límites y errores.

- Las URL van a ``https://<depósito>.s3.us-east-1.amazonaws.com/``, el origen que declara
  ``VIGIA_CSP_STORE_ORIGINS`` (infra: ``evidence_store_origin``).
- ``presign_get`` ≤ 5 min, ``presign_put`` y ``presign_part`` ≤ 15 min; fuera de eso,
  ``ValueError``. La suma, el tipo y los metadatos van en ``X-Amz-SignedHeaders``.
- Solo ``x-amz-meta-*`` en ``required_headers``; suma en hexadecimal de 64 caracteres.
- Conexión rechazada, tiempo de espera, 5xx y limitación → ``StorageUnavailable``; 403 y 404 en
  ``HEAD`` → objeto ausente; el resto de errores no se traga.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from botocore import exceptions as botocore_exceptions  # type: ignore[import-untyped]
from vigia_contracts.clock import SimulatedClock

from tests.integration.conftest import LOCALSTACK_ENVIRONMENT
from vigia_platform.shared.storage import (
    ANONYMIZED_HEADER,
    CHECKSUM_HEADER,
    PRESIGN_GET_MAX_TTL,
    PRESIGN_PUT_MAX_TTL,
    REGIONAL_ENDPOINT,
    ChecksumType,
    CompletedPart,
    ObjectHead,
    S3Storage,
    StorageCredentials,
    StorageSettings,
    StorageUnavailable,
    sha256_hex_to_b64,
)

START = datetime(2026, 9, 29, 12, tzinfo=UTC)
BUCKET = "vigia-evidence-123456789012-us-east-1"
KEY = (
    "org/0b6f1a52-5c1e-4a55-9d0e-2f1d1f4e8a01/plant/0b6f1a52-5c1e-4a55-9d0e-2f1d1f4e8a02"
    "/zone/0b6f1a52-5c1e-4a55-9d0e-2f1d1f4e8a03/node/0b6f1a52-5c1e-4a55-9d0e-2f1d1f4e8a04"
    "/01920000-0000-7000-8000-000000000001.mp4"
)
SHA = hashlib.sha256(b"clip").hexdigest()
COMPOSE_FILE = Path(__file__).resolve().parents[3] / "docker-compose.yml"


def _storage(client: Any | None = None) -> S3Storage:
    settings = StorageSettings(
        bucket=BUCKET, credentials=StorageCredentials("test", "example-secret")
    )
    return S3Storage(settings, SimulatedClock(START), client=client)


def _signed_headers(url: str) -> set[str]:
    query = parse_qs(urlsplit(url).query)
    return set(query["X-Amz-SignedHeaders"][0].split(";"))


def test_urls_are_signed_against_the_fixed_regional_endpoint() -> None:
    assert REGIONAL_ENDPOINT == "https://s3.us-east-1.amazonaws.com"
    grant = asyncio.run(_storage().presign_get(KEY))
    parts = urlsplit(grant.url)
    assert f"{parts.scheme}://{parts.netloc}" == f"https://{BUCKET}.s3.us-east-1.amazonaws.com"
    assert parts.path == "/" + KEY
    assert parse_qs(parts.query)["X-Amz-Expires"] == ["300"]
    assert grant.method == "GET"
    assert grant.expires_at == START + timedelta(minutes=5)


def test_presign_put_signs_checksum_type_and_marker() -> None:
    grant = asyncio.run(_storage().presign_put(KEY, "video/mp4", SHA, {ANONYMIZED_HEADER: "1"}))
    assert grant.method == "PUT"
    assert grant.url.startswith(f"https://{BUCKET}.s3.us-east-1.amazonaws.com/{KEY}?")
    assert {"content-type", CHECKSUM_HEADER, ANONYMIZED_HEADER} <= _signed_headers(grant.url)
    assert grant.headers == {
        "content-type": "video/mp4",
        CHECKSUM_HEADER: base64.b64encode(bytes.fromhex(SHA)).decode("ascii"),
        ANONYMIZED_HEADER: "1",
    }
    assert grant.expires_at == START + PRESIGN_PUT_MAX_TTL
    assert parse_qs(urlsplit(grant.url).query)["X-Amz-Expires"] == ["900"]


def test_presign_part_signs_the_part_checksum() -> None:
    grant = asyncio.run(_storage().presign_part(KEY, "upload-1", 3, SHA, timedelta(minutes=1)))
    query = parse_qs(urlsplit(grant.url).query)
    assert query["partNumber"] == ["3"]
    assert query["uploadId"] == ["upload-1"]
    assert CHECKSUM_HEADER in _signed_headers(grant.url)
    assert grant.headers == {CHECKSUM_HEADER: sha256_hex_to_b64(SHA)}
    assert grant.expires_at == START + timedelta(minutes=1)


@pytest.mark.parametrize(
    "ttl",
    [
        timedelta(0),
        timedelta(seconds=-1),
        PRESIGN_GET_MAX_TTL + timedelta(seconds=1),
        timedelta(milliseconds=1500),
    ],
)
def test_presign_get_ttl_bounds(ttl: timedelta) -> None:
    with pytest.raises(ValueError):
        asyncio.run(_storage().presign_get(KEY, ttl))


@pytest.mark.parametrize("ttl", [timedelta(seconds=1), PRESIGN_GET_MAX_TTL])
def test_presign_get_ttl_edges_are_accepted(ttl: timedelta) -> None:
    assert asyncio.run(_storage().presign_get(KEY, ttl)).expires_at == START + ttl


@pytest.mark.parametrize(
    "ttl", [timedelta(0), PRESIGN_PUT_MAX_TTL + timedelta(seconds=1), timedelta(hours=1)]
)
def test_presign_put_and_part_ttl_bounds(ttl: timedelta) -> None:
    with pytest.raises(ValueError):
        asyncio.run(_storage().presign_put(KEY, "video/mp4", SHA, {}, ttl))
    with pytest.raises(ValueError):
        asyncio.run(_storage().presign_part(KEY, "u", 1, SHA, ttl))


@pytest.mark.parametrize(
    "headers",
    [
        {CHECKSUM_HEADER: "x"},
        {"Content-Type": "video/mp4"},
        {"x-amz-server-side-encryption": "aws:kms"},
        {"authorization": "x"},
        {"x-amz-meta-": "1"},
        {"x-amz-meta-vigia anonymized": "1"},
        {ANONYMIZED_HEADER: ""},
        {ANONYMIZED_HEADER: "1 2"},
        {ANONYMIZED_HEADER: "ñ"},
    ],
)
def test_required_headers_only_admit_user_metadata(headers: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        asyncio.run(_storage().presign_put(KEY, "video/mp4", SHA, headers))


@pytest.mark.parametrize("checksum", ["", SHA.upper(), SHA[:-1], SHA + "0", "g" * 64])
def test_checksum_must_be_lowercase_hex_sha256(checksum: str) -> None:
    with pytest.raises(ValueError):
        asyncio.run(_storage().presign_put(KEY, "video/mp4", checksum, {}))


@pytest.mark.parametrize("part_number", [0, -1, 10_001, True])
def test_part_number_bounds(part_number: int) -> None:
    with pytest.raises(ValueError):
        asyncio.run(_storage().presign_part(KEY, "u", part_number, SHA))


@pytest.mark.parametrize("key", ["", "/org/x", "org/x y", "org/ñ", "a" * 513, "org/x\n"])
def test_invalid_keys_never_reach_the_store(key: str) -> None:
    class Unreachable:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"no debía llamarse {name}")

    storage = _storage(Unreachable())
    with pytest.raises(ValueError):
        asyncio.run(storage.head_object(key))
    with pytest.raises(ValueError):
        asyncio.run(storage.presign_get(key))


def test_no_object_listing_is_offered() -> None:
    assert not [name for name in dir(S3Storage) if "list" in name.lower()]


# --- Errores -----------------------------------------------------------------------------------


def _client_error(status: int, code: str) -> botocore_exceptions.ClientError:
    return botocore_exceptions.ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, "HeadObject"
    )


class RaisingClient:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def head_object(self, **_: Any) -> Any:
        raise self.error


@pytest.mark.parametrize(
    "error",
    [
        botocore_exceptions.EndpointConnectionError(endpoint_url="https://x"),
        botocore_exceptions.ConnectTimeoutError(endpoint_url="https://x"),
        botocore_exceptions.ReadTimeoutError(endpoint_url="https://x"),
        botocore_exceptions.ConnectionClosedError(endpoint_url="https://x"),
        _client_error(500, "InternalError"),
        _client_error(503, "SlowDown"),
        _client_error(400, "RequestTimeout"),
    ],
    ids=lambda error: type(error).__name__ + getattr(error, "operation_name", ""),
)
def test_unreachable_store_is_transient(error: Exception) -> None:
    with pytest.raises(StorageUnavailable) as raised:
        asyncio.run(_storage(RaisingClient(error)).head_object(KEY))
    assert raised.value.code == "storage_unavailable"
    assert raised.value.retry_after_seconds == 5
    assert KEY not in str(raised.value)


@pytest.mark.parametrize("status", [403, 404])
def test_head_of_an_absent_object_is_none(status: int) -> None:
    client = RaisingClient(_client_error(status, str(status)))
    assert asyncio.run(_storage(client).head_object(KEY)) is None


@pytest.mark.parametrize(
    "error", [_client_error(400, "InvalidArgument"), _client_error(301, "PermanentRedirect")]
)
def test_other_errors_are_not_swallowed(error: Exception) -> None:
    with pytest.raises(botocore_exceptions.ClientError):
        asyncio.run(_storage(RaisingClient(error)).head_object(KEY))


def test_hung_call_ends_at_the_call_timeout() -> None:
    release = threading.Event()

    class Hung:
        def head_object(self, **_: Any) -> Any:
            release.wait(5)
            raise AssertionError("no debía esperarse")

    settings = StorageSettings(
        bucket=BUCKET, connect_timeout_seconds=0.05, read_timeout_seconds=0.05
    )
    executor = ThreadPoolExecutor(max_workers=1)
    storage = S3Storage(settings, SimulatedClock(START), client=Hung(), executor=executor)
    try:
        with pytest.raises(StorageUnavailable):
            asyncio.run(asyncio.wait_for(storage.head_object(KEY), timeout=3))
    finally:
        release.set()
        executor.shutdown(wait=True)


def test_default_timeouts_and_no_retries() -> None:
    settings = StorageSettings(bucket=BUCKET)
    config = settings.botocore_config()
    assert (config.connect_timeout, config.read_timeout) == (5.0, 10.0)
    assert config.retries == {"total_max_attempts": 1, "mode": "standard"}
    assert settings.call_timeout_seconds == 16.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"bucket": ""},
        {"bucket": "b", "connect_timeout_seconds": 0},
        {"bucket": "b", "read_timeout_seconds": -1},
        {"bucket": "b", "max_pool_connections": 0},
    ],
)
def test_settings_are_validated(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        StorageSettings(**kwargs)


def test_credentials_are_not_in_repr() -> None:
    assert "example-secret" not in repr(StorageCredentials("id", "example-secret"))


# --- Metadatos ---------------------------------------------------------------------------------


def _head(checksum: str | None, kind: ChecksumType | None) -> ObjectHead:
    return ObjectHead(KEY, 4, checksum, kind, "video/mp4", {}, None)


def test_full_object_sha256_hex() -> None:
    b64 = sha256_hex_to_b64(SHA)
    assert _head(b64, ChecksumType.FULL_OBJECT).full_object_sha256_hex == SHA
    assert _head(b64, None).full_object_sha256_hex == SHA
    assert _head(b64 + "-2", ChecksumType.COMPOSITE).full_object_sha256_hex is None
    assert _head(b64 + "-2", None).full_object_sha256_hex is None
    assert _head(b64, ChecksumType.COMPOSITE).full_object_sha256_hex is None
    assert _head(None, None).full_object_sha256_hex is None
    assert _head("AAAA", ChecksumType.FULL_OBJECT).full_object_sha256_hex is None
    assert _head("no-base64!", ChecksumType.FULL_OBJECT).full_object_sha256_hex is None


def test_complete_multipart_needs_parts() -> None:
    with pytest.raises(ValueError):
        asyncio.run(_storage().complete_multipart(KEY, "u", []))
    with pytest.raises(ValueError):
        asyncio.run(_storage().complete_multipart(KEY, "u", [CompletedPart(1, "e", "x")]))


def test_compose_localstack_has_the_test_environment() -> None:
    """``docker-compose.yml`` y testcontainers levantan LocalStack con el mismo entorno."""
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    for name, value in LOCALSTACK_ENVIRONMENT.items():
        if name == "SERVICES":
            assert f"{name}: {value}" in text
        else:
            assert f'{name}: "{value}"' in text or f"{name}: {value}" in text, name
