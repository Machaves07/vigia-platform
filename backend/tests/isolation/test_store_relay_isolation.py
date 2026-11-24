"""El relevo firmado del almacén es una ruta sin sesión (BR-NUC-91; TASK-607, A-130).

Caso ``PUBLIC`` de la ruta ``/store-relay/object``: no hay organización de contexto porque la URL
es la capacidad, como una de S3. Por eso:

- sin el relevo (cualquier entorno sin ``VIGIA_STORE_RELAY=1``), la ruta **no existe**: responde
  exactamente como una ruta desconocida;
- con el relevo, la cadena nunca lee la cookie de sesión de esa ruta (el puerto de sesiones no
  recibe ninguna llamada, con o sin cookie) y la respuesta es la misma con y sin cookie: solo
  decide el token;
- las dos entradas de la lista cerrada existen, con su método, su ruta y exposición pública.

No toca la base: el relevo no lee ni escribe filas. Solo datos generados.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest

from tests.api_support import World
from tests.store_relay_support import (
    BUCKET,
    START,
    FakeS3,
    attachment_key,
    relay_app,
    relay_storage,
    same_origin,
    send,
)
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME
from vigia_platform.shared.api.declarations import Exposure, UnauthenticatedRoute
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.storage_relay import RELAY_PATH


class _Sessions:
    """``SessionContextPort`` que solo cuenta: el relevo nunca debe llamarlo."""

    def __init__(self) -> None:
        self.calls = 0

    async def context_from_session(self, cookie: Any, **_: Any) -> Any:
        self.calls += 1
        raise AssertionError("el relevo no lee la sesión")


def _strip(response: httpx.Response) -> tuple[int, dict[str, Any]]:
    body = response.json()
    body.pop("correlation_id", None)
    return response.status_code, body


@pytest.mark.parametrize("method", ["GET", "HEAD", "PUT"])
def test_without_the_relay_the_route_answers_like_an_unknown_one(method: str) -> None:
    app = relay_app(World(), None)
    relay_path = asyncio.run(
        send(app, method, f"{RELAY_PATH}?token=a.b", headers=same_origin(), content=b"x")
    )
    unknown = asyncio.run(
        send(app, method, "/store-relay/otra?token=a.b", headers=same_origin(), content=b"x")
    )
    assert relay_path.status_code == unknown.status_code == 404
    if method != "HEAD":
        assert _strip(relay_path) == _strip(unknown)


@pytest.mark.parametrize("cookie", [False, True], ids=["sin-cookie", "con-cookie"])
def test_the_relay_never_reads_the_session_and_only_the_token_decides(cookie: bool) -> None:
    clock = SimulatedClock(START)
    client = FakeS3()
    storage = relay_storage(client, clock)
    sessions = _Sessions()
    app = relay_app(World(clock=clock), storage, sessions=sessions)
    version = client.store(BUCKET, attachment_key(), b"clip sintetico")
    url = asyncio.run(storage.presign_get(attachment_key(), version_id=version)).url
    headers = {"cookie": f"{SESSION_COOKIE_NAME}={uuid.UUID(int=7).hex}"} if cookie else {}
    served = asyncio.run(send(app, "GET", url, headers=headers))
    denied = asyncio.run(send(app, "GET", f"{RELAY_PATH}?token=a.b", headers=headers))
    assert (served.status_code, served.content) == (200, b"clip sintetico")
    assert denied.status_code == 403
    assert sessions.calls == 0


def test_the_closed_list_has_the_two_relay_entries() -> None:
    entries = {
        entry: (entry.method, entry.path, entry.exposure)
        for entry in UnauthenticatedRoute
        if entry.path == RELAY_PATH
    }
    assert entries == {
        UnauthenticatedRoute.STORE_RELAY_READ: ("GET", RELAY_PATH, Exposure.PUBLIC),
        UnauthenticatedRoute.STORE_RELAY_WRITE: ("PUT", RELAY_PATH, Exposure.PUBLIC),
    }
