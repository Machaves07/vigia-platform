"""``provider_query`` de una petición HTTP bajo concesión, contra PostgreSQL 16 real (VIG-132).

BR-NUC-38 y 41, PR-NUC-11: la aplicación real (``create_app`` con la cadena fija de middleware,
``ScopeContexts`` y ``ContextAuthorizer``) sobre la base migrada como ``vigia_app``, con la
concesión concedida por ``ConcessionService`` y el ``provider_query`` escrito por
``LedgerProviderQueryLedger`` (``EscritorExpediente``). La ruta de datos es una ruta de prueba
``GET /hierarchy`` con la clave ``hierarchy.read``, como la de TASK-136; el panel del cliente es
``ConcessionService.list_provider_queries``, lo que responde ``GET /concessions/{id}/queries``.

- Una petición bajo concesión deja exactamente un ``provider_query`` en la cadena del alcance
  concedido (planta u organización) con operación, método, plantilla de ruta y momento, y el
  cliente lo ve en su panel.
- Una petición sin concesión y la salud, aun con la cabecera de concesión, no escriben ninguno.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any, Final

import httpx
import pytest
from fastapi import APIRouter, Request

from tests.api_support import World
from tests.authz_support import Site
from tests.concession_support import REASON, ConcessionEnvironment, concession_environment
from tests.integration.conftest import PostgresEndpoint
from tests.middleware_support import chain_units
from vigia_platform.identity.application.concessions import Concession
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME, SessionCookie
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.shared.api.app import UnitRegistration
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.middleware import ContextAuthorizer, request_context
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration

DATA_ROUTE: Final = "/hierarchy"


def _data_unit() -> UnitRegistration:
    router = APIRouter()

    @router.get(DATA_ROUTE, dependencies=[requires(PermissionKey.HIERARCHY_READ.value)])
    async def hierarchy(request: Request) -> dict[str, str]:
        return {"organization_id": str(request_context(request).organization_id)}

    return UnitRegistration("provider_query_data", (router,))


class Api:
    def __init__(self, env: ConcessionEnvironment, client: httpx.AsyncClient) -> None:
        self.env = env
        self.client = client

    def get(
        self, path: str, cookie: SessionCookie, concession: uuid.UUID | None = None
    ) -> httpx.Response:
        headers = {"Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}"}
        if concession is not None:
            headers["X-Vigia-Concession"] = str(concession)
        response: httpx.Response = self.env.run(self.client.get(path, headers=headers))
        return response


@pytest.fixture(scope="module")
def api(postgres_endpoint: PostgresEndpoint) -> Iterator[Api]:
    with concession_environment(postgres_endpoint, "provider_query_http") as env:
        authz = env.authz
        clock = authz.sessions.clock
        app = World(clock=clock).app(
            units=(*chain_units(), _data_unit()),
            permissions=frozenset(key.value for key in PermissionKey),
            runtime={
                "sessions": authz.contexts,
                "authorizer": ContextAuthorizer(
                    audit=authz.audit,
                    provider_organization_id=authz.provider_organization_id,
                    provider_queries=env.provider_queries,
                    clock=clock,
                ),
            },
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=10.0
        )
        try:
            yield Api(env, client)
        finally:
            env.run(client.aclose())


def _grant(
    env: ConcessionEnvironment, installer: uuid.UUID, site: Site, level: ScopeLevel
) -> Concession:
    plant = next(iter(site.plants))
    concession: Concession = env.run(
        env.service.grant(
            env.provider_context(installer),
            client_organization_id=site.organization_id,
            scope_level=level,
            scope_id=site.organization_id if level is ScopeLevel.ORGANIZATION else plant,
            reason=REASON,
            duration=None,
        )
    )
    return concession


def _provider_queries(env: ConcessionEnvironment, organization_id: uuid.UUID) -> list[Any]:
    return env.fetch(
        "SELECT plant_id, actor_concession_id, ledger.vigia_bytes_to_jsonb(content) AS content"
        " FROM ledger.ledger_record WHERE record_type = 'provider_query'"
        " AND organization_id = $1 ORDER BY received_at, record_id",
        organization_id,
    )


@pytest.mark.parametrize("level", [ScopeLevel.ORGANIZATION, ScopeLevel.PLANT])
def test_a_request_under_concession_leaves_one_provider_query_the_client_sees(
    api: Api, level: ScopeLevel
) -> None:
    env = api.env
    site = env.authz.add_site(plants=1, zones_per_plant=1)
    installer = env.provider_user()
    concession = _grant(env, installer, site, level)
    cookie = env.authz.open_session(env.provider_organization_id, installer)
    moment = format_timestamp(env.authz.sessions.clock.now())

    response = api.get(DATA_ROUTE, cookie, concession.concession_id)

    assert response.status_code == 200, response.text
    assert response.json() == {"organization_id": str(site.organization_id)}
    (row,) = _provider_queries(env, site.organization_id)
    plant = next(iter(site.plants))
    assert row["plant_id"] == (plant if level is ScopeLevel.PLANT else None)
    assert row["actor_concession_id"] == concession.concession_id
    content = json.loads(row["content"])
    assert (content["operation"], content["method"], content["resource"]) == (
        "read",
        "GET",
        DATA_ROUTE,
    )
    assert content["occurred_at"] == moment
    # El panel del cliente (``GET /concessions/{id}/queries``) lo muestra con su motivo.
    administrator = env.session_context(site.organization_id, env.client_user(site))
    page = env.run(env.service.list_provider_queries(administrator, concession.concession_id))
    (item,) = page.items
    assert (item.operation, item.method, item.resource, item.reason) == (
        "read",
        "GET",
        DATA_ROUTE,
        REASON,
    )
    assert item.provider_user_id == installer and item.occurred_at == moment


def test_without_concession_and_on_health_nothing_is_written(api: Api) -> None:
    env = api.env
    site = env.authz.add_site(plants=1, zones_per_plant=1)
    installer = env.provider_user()
    concession = _grant(env, installer, site, ScopeLevel.ORGANIZATION)
    provider_cookie = env.authz.open_session(env.provider_organization_id, installer)
    administrator = env.client_user(site, Role.ADMINISTRATOR)
    client_cookie = env.authz.open_session(site.organization_id, administrator)

    own = api.get(DATA_ROUTE, client_cookie)
    live = api.get("/health/live", provider_cookie, concession.concession_id)

    assert own.status_code == 200 and live.status_code == 200
    assert _provider_queries(env, site.organization_id) == []
