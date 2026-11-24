"""La ruta del relevo firmado sobre la cadena real y un S3 en memoria (TASK-607, A-130).

La misma aplicación de ``create_app`` (los once eslabones), con ``FakeS3`` en lugar de LocalStack:
cada garantía de la ruta con su prueba aislada y rápida. El recorrido contra LocalStack es
``tests/integration/test_store_relay_routes.py``. Solo datos generados.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from typing import Any

import httpx
import pytest

from tests.api_support import World
from tests.store_relay_support import (
    BUCKET,
    EDGE_BUCKET,
    ORIGIN,
    START,
    FakeS3,
    attachment_key,
    relay_app,
    relay_storage,
    same_origin,
    send,
)
from vigia_platform.loop.record_types import MAX_ATTACHMENT_BYTES
from vigia_platform.shared.api.middleware.headers import content_security_policy
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.storage_relay import (
    RELAY_PATH,
    RelayGrant,
    RelayMethod,
    StoreRelayStorage,
)

CLIP = bytes(range(256)) * 16  # 4 096 bytes
KEY = attachment_key(extension="pdf")


class Relay:
    """Un relevo con su almacén, su reloj, sus métricas y su aplicación."""

    def __init__(self) -> None:
        self.client = FakeS3()
        self.clock = SimulatedClock(START)
        self.storage = relay_storage(self.client, self.clock)
        self.app = relay_app(World(clock=self.clock), self.storage)

    def outcomes(self) -> list[tuple[object, object]]:
        added = self.storage._metrics.store_relay_requests_total.added  # type: ignore[union-attr]
        return [(entry["method"], entry["outcome"]) for entry in added]

    def get_url(self, version_id: str | None, **changes: Any) -> str:
        if changes:
            _, expires_at_ms = self.storage.tokens.expires_at_ms(300)
            values: dict[str, Any] = {
                "bucket": BUCKET,
                "key": KEY,
                "version_id": version_id,
                "method": RelayMethod.GET,
                "expires_at_ms": expires_at_ms,
            }
            values.update(changes)
            token = self.storage.tokens.issue(RelayGrant(**values))
            return f"{RELAY_PATH}?token={token}"
        return asyncio.run(self.storage.presign_get(KEY, version_id=version_id)).url

    def request(self, method: str, url: str, **options: Any) -> httpx.Response:
        return asyncio.run(send(self.app, method, url, **options))


@pytest.fixture
def relay() -> Relay:
    return Relay()


# --- Lectura -------------------------------------------------------------------------------------


def test_a_read_serves_the_whole_object_with_its_headers(relay: Relay) -> None:
    version = relay.client.store(BUCKET, KEY, CLIP, "video/mp4")
    response = relay.request("GET", relay.get_url(version))
    assert response.status_code == 200
    assert response.content == CLIP
    headers = response.headers
    assert (headers["content-type"], headers["accept-ranges"]) == ("video/mp4", "bytes")
    assert headers["cache-control"] == "private, no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert int(headers["content-length"]) == len(CLIP)
    assert "content-disposition" not in headers
    assert relay.outcomes() == [("GET", "served")]


def test_a_range_serves_exactly_those_bytes_with_206(relay: Relay) -> None:
    version = relay.client.store(BUCKET, KEY, CLIP)
    response = relay.request("GET", relay.get_url(version), headers={"range": "bytes=0-1023"})
    assert response.status_code == 206
    assert response.content == CLIP[:1024]
    assert response.headers["content-range"] == f"bytes 0-1023/{len(CLIP)}"
    assert relay.outcomes() == [("GET", "partial")]


@pytest.mark.parametrize(
    ("header", "first", "last"),
    [("bytes=4000-", 4000, 4095), ("bytes=-100", 3996, 4095), ("bytes=10-99999", 10, 4095)],
    ids=["abierto", "sufijo", "recortado"],
)
def test_open_suffix_and_clipped_ranges(relay: Relay, header: str, first: int, last: int) -> None:
    version = relay.client.store(BUCKET, KEY, CLIP)
    response = relay.request("GET", relay.get_url(version), headers={"range": header})
    assert response.status_code == 206
    assert response.content == CLIP[first : last + 1]
    assert response.headers["content-range"] == f"bytes {first}-{last}/{len(CLIP)}"


@pytest.mark.parametrize("header", ["bytes=4096-", "bytes=-0"], ids=["fuera", "sufijo-vacio"])
def test_a_range_outside_the_object_is_416(relay: Relay, header: str) -> None:
    version = relay.client.store(BUCKET, KEY, CLIP)
    response = relay.request("GET", relay.get_url(version), headers={"range": header})
    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{len(CLIP)}"
    assert response.content == b""
    assert relay.client.calls == ["head_object"]


@pytest.mark.parametrize("header", ["bytes=0-1,5-9", "items=0-1", "bytes=9-1"])
def test_a_malformed_or_multiple_range_is_ignored(relay: Relay, header: str) -> None:
    version = relay.client.store(BUCKET, KEY, CLIP)
    response = relay.request("GET", relay.get_url(version), headers={"range": header})
    assert (response.status_code, response.content) == (200, CLIP)


def test_a_read_serves_the_version_of_the_token_not_the_latest(relay: Relay) -> None:
    first = relay.client.store(BUCKET, KEY, CLIP)
    relay.client.store(BUCKET, KEY, b"otra version, mas reciente")
    response = relay.request("GET", relay.get_url(first))
    assert (response.status_code, response.content) == (200, CLIP)
    ranged = relay.request("GET", relay.get_url(first), headers={"range": "bytes=0-9"})
    assert ranged.content == CLIP[:10]


def test_head_answers_the_headers_without_reading_the_body(relay: Relay) -> None:
    version = relay.client.store(BUCKET, KEY, CLIP)
    response = relay.request("HEAD", relay.get_url(version))
    assert response.status_code == 200
    assert int(response.headers["content-length"]) == len(CLIP)
    assert response.content == b""
    assert relay.client.calls == ["head_object"]
    assert relay.outcomes() == [("HEAD", "served")]


def test_an_attachment_read_is_a_download(relay: Relay) -> None:
    version = relay.client.store(BUCKET, KEY, b"%PDF-1.7 sintetico", "application/pdf")
    url = asyncio.run(relay.storage.presign_attachment_get(KEY, version_id=version)).url
    response = relay.request("GET", url)
    assert response.status_code == 200
    assert response.headers["content-disposition"] == "attachment"
    assert response.headers["content-type"] == "application/octet-stream"


def test_a_missing_version_is_403_like_s3(relay: Relay) -> None:
    relay.client.store(BUCKET, KEY, CLIP)
    response = relay.request("GET", relay.get_url("v9"))
    assert response.status_code == 403
    assert relay.outcomes() == [("GET", "denied")]


# --- Tokens rechazados: 403 y ninguna llamada al almacén ----------------------------------------


@pytest.mark.parametrize(
    "changes",
    [{"bucket": EDGE_BUCKET}, {"key": "ca/root.pem"}, {"method": RelayMethod.PUT}],
    ids=["vigia-edge", "ca-root", "otro-metodo"],
)
def test_an_authentic_token_outside_the_relay_is_403_and_never_reaches_the_store(
    relay: Relay, changes: dict[str, Any]
) -> None:
    relay.client.store(EDGE_BUCKET, KEY, CLIP)
    relay.client.store(BUCKET, "ca/root.pem", b"raiz")
    response = relay.request("GET", relay.get_url(None, **changes))
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"
    assert relay.client.calls == []
    assert relay.outcomes() == [("GET", "denied")]


def test_an_altered_missing_or_repeated_token_is_403(relay: Relay) -> None:
    version = relay.client.store(BUCKET, KEY, CLIP)
    url = relay.get_url(version)
    token = httpx.URL(url).params["token"]
    flipped = token[:-1] + ("A" if token[-1] != "A" else "B")
    for target in (
        f"{RELAY_PATH}?token={flipped}",
        RELAY_PATH,
        f"{RELAY_PATH}?token={token}&token={token}",
    ):
        assert relay.request("GET", target).status_code == 403
    assert relay.client.calls == []


def test_an_expired_token_is_403_and_counted_as_expired(relay: Relay) -> None:
    version = relay.client.store(BUCKET, KEY, CLIP)
    url = relay.get_url(version)
    relay.clock.advance(300)
    response = relay.request("GET", url)
    assert response.status_code == 403
    assert relay.outcomes() == [("GET", "expired")]
    assert relay.client.calls == []


# --- Subida --------------------------------------------------------------------------------------

BODY = b"\x89PNG sintetico del cierre"
SHA = hashlib.sha256(BODY).hexdigest()


def _upload(relay: Relay) -> Any:
    return asyncio.run(relay.storage.presign_put(attachment_key(), "image/png", SHA, {}))


def test_an_upload_with_the_signed_headers_stores_the_object(relay: Relay) -> None:
    signed = _upload(relay)
    response = relay.request(
        "PUT", signed.url, content=BODY, headers={**dict(signed.headers), **same_origin()}
    )
    assert response.status_code == 200
    assert response.headers["etag"] == '"v1"'
    (stored,) = relay.client.objects[(BUCKET, attachment_key())]
    assert (stored.body, stored.content_type) == (BODY, "image/png")
    assert relay.outcomes() == [("PUT", "served")]


@pytest.mark.parametrize(
    "change",
    [
        {"content-type": "application/pdf"},
        {"x-amz-checksum-sha256": base64.b64encode(bytes(32)).decode()},
        {"x-amz-checksum-sha256": None},
        {"content-type": None},
    ],
    ids=["otro-tipo", "otra-suma", "sin-suma", "sin-tipo"],
)
def test_an_upload_without_the_signed_headers_is_403_and_stores_nothing(
    relay: Relay, change: dict[str, str | None]
) -> None:
    signed = _upload(relay)
    headers = {**dict(signed.headers), **same_origin()}
    for name, value in change.items():
        if value is None:
            del headers[name]
        else:
            headers[name] = value
    response = relay.request("PUT", signed.url, content=BODY, headers=headers)
    assert response.status_code == 403
    assert relay.client.calls == []
    assert relay.outcomes() == [("PUT", "denied")]


def test_bytes_that_do_not_match_the_signed_sum_are_400_and_store_nothing(relay: Relay) -> None:
    signed = _upload(relay)
    response = relay.request(
        "PUT", signed.url, content=BODY + b"!", headers={**dict(signed.headers), **same_origin()}
    )
    assert response.status_code == 400
    assert (BUCKET, attachment_key()) not in relay.client.objects
    assert relay.outcomes() == [("PUT", "bad_digest")]


@pytest.mark.parametrize(
    "csrf",
    [
        {"sec-fetch-site": "cross-site"},
        {"sec-fetch-site": "same-origin", "origin": "https://x.test"},
    ],
    ids=["otro-sitio", "otro-origen"],
)
def test_an_upload_from_another_origin_never_reaches_the_relay(
    relay: Relay, csrf: dict[str, str]
) -> None:
    signed = _upload(relay)
    response = relay.request(
        "PUT", signed.url, content=BODY, headers={**dict(signed.headers), **csrf}
    )
    assert response.status_code == 403
    assert relay.client.calls == []
    assert relay.outcomes() == []


def test_an_upload_over_the_attachment_limit_is_413(relay: Relay) -> None:
    signed = _upload(relay)
    response = relay.request(
        "PUT",
        signed.url,
        content=b"\0" * (MAX_ATTACHMENT_BYTES + 1),
        headers={**dict(signed.headers), **same_origin()},
    )
    assert response.status_code == 413
    assert relay.client.calls == []


def test_a_read_token_does_not_upload(relay: Relay) -> None:
    url = relay.get_url(None, key=attachment_key(), method=RelayMethod.GET)
    response = relay.request(
        "PUT", url, content=BODY, headers={"content-type": "image/png", **same_origin()}
    )
    assert response.status_code == 403
    assert relay.client.calls == []


# --- Política de contenido ---------------------------------------------------------------------


def test_the_served_policy_is_the_same_with_and_without_the_relay(relay: Relay) -> None:
    version = relay.client.store(BUCKET, KEY, CLIP)
    with_relay = relay.request("GET", relay.get_url(version))
    without = asyncio.run(send(relay_app(World(), None), "GET", "/health/live"))
    expected = content_security_policy(())
    assert with_relay.headers["content-security-policy"] == expected
    assert without.headers["content-security-policy"] == expected
    directives = dict(item.split(" ", 1) for item in expected.split("; "))
    assert directives["img-src"] == "'self'"
    assert directives["media-src"] == directives["connect-src"] == "'self'"


def test_the_relay_route_is_out_of_the_openapi_schema(relay: Relay) -> None:
    assert RELAY_PATH not in relay.app.openapi()["paths"]
    assert isinstance(relay.storage, StoreRelayStorage)
    assert ORIGIN.startswith("https://")
