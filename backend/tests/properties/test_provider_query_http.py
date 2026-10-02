"""``provider_query`` en cada petición HTTP bajo concesión (BR-NUC-38 y 41, PR-NUC-11; VIG-132).

La aplicación real (cadena fija de middleware y ``ContextAuthorizer``) con dobles en los puertos
(``tests.middleware_support``): el ``ProviderQueryLedger`` recuerda lo que se escribe y puede
fallar.

- Una petición bajo concesión a una ruta con clave (``requires``) deja **exactamente un**
  ``provider_query`` con la operación (``read``/``write``), el método, la plantilla de la ruta y el
  momento, en la cadena del alcance concedido (planta u organización).
- Sin concesión, en las rutas de salud, en las de sesión (``authenticated``) y en una ruta
  denegada no se escribe ninguno.
- Fallo cerrado: si la escritura falla, la ruta no corre y la respuesta es el error genérico.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, Final

import pytest
from fastapi import APIRouter, Request
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ConfigDict, StrictStr

from tests.middleware_support import (
    CLIENT_ORG,
    PROVIDER_ORG,
    SAME_ORIGIN,
    Harness,
    cookie_header,
)
from vigia_platform.identity.authz.context import ConcessionRow
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.shared.api.app import UnitRegistration
from vigia_platform.shared.api.declarations import SessionRoute, authenticated, requires
from vigia_platform.shared.api.errors import ApiErrorCode
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.signing.keys import format_timestamp

CONCESSION: Final = uuid.UUID("0192f0c4-0000-7000-8000-00000000c132")
PLANT: Final = uuid.UUID("0192f0c4-0000-7000-8000-0000000a0132")
WRITE_ROUTE: Final = "/plants/{plant_id}/probe"


class Probe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: StrictStr


def _probe_unit(handled: list[str]) -> UnitRegistration:
    """Una escritura con clave del instalador y una ruta de sesión, propias de este archivo."""
    router = APIRouter()

    @router.post(WRITE_ROUTE, dependencies=[requires(PermissionKey.FLEET_MANAGE.value)])
    async def probe(plant_id: uuid.UUID, body: Probe) -> dict[str, str]:
        handled.append("probe")
        return {"plant_id": str(plant_id)}

    @router.api_route(
        "/plants/{plant_id}/summary",
        methods=["GET", "HEAD"],
        dependencies=[requires(PermissionKey.FLEET_READ.value)],
    )
    async def summary(plant_id: uuid.UUID) -> dict[str, str]:
        handled.append("summary")
        return {"plant_id": str(plant_id)}

    @router.get("/auth/sessions", dependencies=[authenticated(SessionRoute.AUTH_SESSIONS)])
    async def own_sessions(request: Request) -> dict[str, str]:
        handled.append("own_sessions")
        return {"ok": "true"}

    return UnitRegistration("provider_query_probe", (router,))


def _harness() -> tuple[Harness, list[str]]:
    handled: list[str] = []
    return Harness(extra_units=(_probe_unit(handled),)), handled


def _installer(harness: Harness, level: ScopeLevel = ScopeLevel.ORGANIZATION) -> dict[str, str]:
    """Cabeceras de un instalador del proveedor bajo la concesión ``CONCESSION``."""
    cookie = harness.session(
        f"instalador-{level.value}",
        role=Role.PROVIDER_INSTALLER,
        organization_id=PROVIDER_ORG,
        concession=ConcessionRow(
            concession_id=CONCESSION,
            organization_id=CLIENT_ORG,
            scope_level=level,
            scope_id=PLANT if level is ScopeLevel.PLANT else CLIENT_ORG,
            expires_at=harness.world.clock.now() + timedelta(days=1),
        ),
    )
    return {**cookie_header(cookie), "X-Vigia-Concession": str(CONCESSION)}


def _code(response: Any) -> str:
    code: str = response.json()["code"]
    return code


@pytest.mark.parametrize("level", [ScopeLevel.ORGANIZATION, ScopeLevel.PLANT])
def test_a_read_under_concession_writes_exactly_one_provider_query(level: ScopeLevel) -> None:
    harness, _ = _harness()
    headers = _installer(harness, level)
    with TestClient(harness.app()) as client:
        moment = format_timestamp(harness.world.clock.now())
        response = client.get("/me", headers=headers)
    assert response.status_code == 200
    ((context, content, plant_id),) = harness.provider_queries.written
    assert content == {
        "concession_id": str(CONCESSION),
        "operation": "read",
        "method": "GET",
        "resource": "/me",
        "occurred_at": moment,
    }
    # En la cadena del alcance concedido: la planta o, sin planta, la organización cliente.
    assert plant_id == (PLANT if level is ScopeLevel.PLANT else None)
    assert context.organization_id == CLIENT_ORG and context.concession_id == CONCESSION
    assert [context.correlation_id] == harness.observed.correlation_ids


def test_a_write_under_concession_is_recorded_with_the_route_template() -> None:
    harness, handled = _harness()
    headers = {**_installer(harness), **SAME_ORIGIN}
    with TestClient(harness.app()) as client:
        response = client.post(f"/plants/{PLANT}/probe", headers=headers, json={"note": "x"})
    assert response.status_code == 200 and handled == ["probe"]
    ((_, content, _),) = harness.provider_queries.written
    assert (content["operation"], content["method"], content["resource"]) == (
        "write",
        "POST",
        WRITE_ROUTE,
    )


def test_a_head_request_is_recorded_as_the_get_it_reads() -> None:
    harness, handled = _harness()
    with TestClient(harness.app()) as client:
        response = client.head(f"/plants/{PLANT}/summary", headers=_installer(harness))
    assert response.status_code == 200 and handled == ["summary"]
    ((_, content, _),) = harness.provider_queries.written
    assert (content["operation"], content["method"], content["resource"]) == (
        "read",
        "GET",
        "/plants/{plant_id}/summary",
    )


@given(count=st.integers(0, 6))
def test_pr_nuc_11_one_provider_query_per_request(count: int) -> None:
    harness, _ = _harness()
    headers = _installer(harness)
    with TestClient(harness.app()) as client:
        for _ in range(count):
            assert client.get("/me", headers=headers).status_code == 200
            assert client.get("/health/live", headers=headers).status_code == 200
    assert len(harness.provider_queries.written) == count


def test_without_concession_nothing_is_written() -> None:
    harness, _ = _harness()
    cookie = harness.session("administracion", role=Role.ADMINISTRATOR)
    with TestClient(harness.app()) as client:
        response = client.get("/me", headers=cookie_header(cookie))
    assert response.status_code == 200
    assert harness.provider_queries.written == []


def test_health_and_session_routes_write_nothing_under_concession() -> None:
    harness, handled = _harness()
    headers = _installer(harness)
    with TestClient(harness.app()) as client:
        live = client.get("/health/live", headers=headers)
        sessions = client.get("/auth/sessions", headers=headers)
    assert live.status_code == 200
    assert sessions.status_code == 200 and handled == ["own_sessions"]
    assert harness.provider_queries.written == []


def test_a_denied_route_under_concession_writes_nothing() -> None:
    # El instalador no tiene ``users.manage``: ``not_found`` y ningún acceso que registrar.
    harness, _ = _harness()
    headers = {**_installer(harness), **SAME_ORIGIN}
    with TestClient(harness.app()) as client:
        response = client.post(
            "/users",
            headers=headers,
            json={"email": "a@b.co", "role": "copasst", "plant_id": str(uuid.uuid4())},
        )
    assert response.status_code == 404 and _code(response) == ApiErrorCode.NOT_FOUND
    assert harness.provider_queries.written == []
    assert harness.observed.handled == []


def test_if_the_provider_query_cannot_be_written_the_route_does_not_run() -> None:
    # Fallo cerrado (N-4): sin rastro en el expediente del cliente no hay acceso.
    harness, handled = _harness()
    harness.provider_queries.fail = True
    headers = _installer(harness)
    with TestClient(harness.app(), raise_server_exceptions=False) as client:
        read = client.get("/me", headers=headers)
        write = client.post(
            f"/plants/{PLANT}/probe", headers={**headers, **SAME_ORIGIN}, json={"note": "x"}
        )
    for response in (read, write):
        assert response.status_code == 500 and _code(response) == ApiErrorCode.INTERNAL_ERROR
        assert "organization_id" not in response.text
    assert harness.observed.handled == [] and handled == []


def test_a_request_the_route_rejects_is_still_recorded_as_an_attempt() -> None:
    # Se escribe antes de la ruta: un cuerpo inválido no deja el intento sin rastro.
    harness, handled = _harness()
    headers = {**_installer(harness), **SAME_ORIGIN}
    with TestClient(harness.app()) as client:
        response = client.post(f"/plants/{PLANT}/probe", headers=headers, json={"other": 1})
    assert response.status_code == 400 and handled == []
    ((_, content, _),) = harness.provider_queries.written
    assert content["operation"] == "write" and content["resource"] == WRITE_ROUTE
