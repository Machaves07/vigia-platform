"""FS-GOB-05 base: ``vigia-node-ca`` sin respuesta (TASK-219; PAT-GOB-RES-03; NFR-GOB-43).

Contra PostgreSQL 16 real como ``vigia_app`` (``tests/fleet_enrollment_support.py``) con el doble
de KMS **colgado** (``MemoryKms.hang``: ``GetPublicKey`` y ``Sign`` esperan para siempre, como un
KMS que no responde) y el **plazo de producción** de la autoridad (``NODE_CA_DEADLINE_SECONDS``,
4 s, por debajo de los 5 s de NFR-GOB-43):

- el alta responde ``temporarily_unavailable`` (503) con ``retry_after_seconds`` en ≤ 5 s; el código
  sigue ``active`` y no se escribe nada (ni credencial, ni intento, ni registro, ni evento);
- la rotación igual: la credencial presentada sigue ``active`` y no hay ``node_credential_rotated``;
- mientras el alta espera a KMS, la ruta de prueba interna de TASK-206 (petición de un nodo dado
  de alta) sigue respondiendo: ninguna otra ruta del contrato se ve afectada.

Mide tiempo real a propósito (es el tope de producción lo que se prueba): el margen sobre el plazo
de la autoridad es de 1 s, y la prueba de que la ruta de prueba responde no decide por tiempo
(espera a que acabe, con el alta aún colgada).

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Final

import httpx
import pytest
from vigia_contracts.models.api import parse_rejection_response

from tests.fleet_credentials_support import MemoryKms
from tests.fleet_enrollment_support import (
    ENROLLMENT_PATH,
    ROTATION_PATH,
    EnrollmentWorld,
    enrollment_world,
)
from tests.integration.conftest import PostgresEndpoint
from tests.node_api_support import VERSION, alb_headers
from vigia_platform.fleet.adapters.ca.certificate_profiles import NODE_CA_DEADLINE_SECONDS
from vigia_platform.shared.clock import SystemClock

pytestmark = pytest.mark.integration

WALL_CLOCK: Final = SystemClock()
"""Tiempo real: lo que se mide es el tope de producción (no el reloj simulado de la prueba)."""
LIMIT_SECONDS: Final = 5.0
"""El tope del criterio: alta y rotación responden en ≤ 5 s con KMS sin respuesta."""
GUARD_SECONDS: Final = 30.0
"""Corta la prueba si la ruta se queda colgada (sin el plazo de la autoridad, nunca responde):
falla en lugar de esperar para siempre. No decide el criterio, que es ``LIMIT_SECONDS``."""


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[EnrollmentWorld]:
    with enrollment_world(postgres_endpoint, "fleet_node_ca_down") as built:
        yield built


def _hung_app(world: EnrollmentWorld) -> object:
    """La aplicación con la autoridad colgada y el plazo de producción."""
    hung = MemoryKms(key=world.kms.key, hang=True)
    issuer = world.issuer(deadline=NODE_CA_DEADLINE_SECONDS, kms=hung)
    enrollment, rotation = world.services(issuer=issuer)
    _, app = world.app(enrollment, rotation)
    return app


def _client(app: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="https://nodes.vigia.test",
        timeout=60,
    )


def _unavailable(response: httpx.Response, elapsed: float) -> None:
    rejection = parse_rejection_response(response.content)
    assert (response.status_code, rejection.code.value) == (503, "temporarily_unavailable")
    assert rejection.retryable is True and rejection.retry_after_seconds == 5
    assert response.headers["retry-after"] == "5"
    assert elapsed <= LIMIT_SECONDS, elapsed


def test_enrollment_with_the_node_ca_down_is_transient_and_writes_nothing(
    world: EnrollmentWorld,
) -> None:
    setup = world.declared()
    code = world.code(setup)
    body = world.body(setup, code)
    app = _hung_app(world)

    async def post() -> tuple[httpx.Response, float]:
        async with _client(app) as client, asyncio.timeout(GUARD_SECONDS):
            start = WALL_CLOCK.monotonic()
            response = await client.post(
                ENROLLMENT_PATH, json=body, headers={"X-Vigia-Contract-Version": VERSION}
            )
            return response, WALL_CLOCK.monotonic() - start

    response, elapsed = world.run(post())

    _unavailable(response, elapsed)
    assert world.code_statuses(setup.node_id) == ["active"]
    assert world.credentials(setup.node_id) == []
    assert world.attempts(setup.node_id) == []
    assert world.fleet_record(setup.node_id)["status"] == "declared"
    assert world.fleet.records("node_enrolled", setup.organization_id) == []
    assert world.fleet.events("node_enrolled", setup.organization_id) == []
    # Con la autoridad de vuelta, el mismo código da de alta.
    assert world.post(ENROLLMENT_PATH, body).status_code == 200


def test_rotation_with_the_node_ca_down_is_transient_and_writes_nothing(
    world: EnrollmentWorld,
) -> None:
    setup = world.declared()
    enrolled = world.enroll(setup)
    world.advance(1)
    body = world.rotation_body(setup.node_id)
    app = _hung_app(world)

    async def post() -> tuple[httpx.Response, float]:
        async with _client(app) as client, asyncio.timeout(GUARD_SECONDS):
            start = WALL_CLOCK.monotonic()
            response = await client.post(ROTATION_PATH, json=body, headers=enrolled.headers)
            return response, WALL_CLOCK.monotonic() - start

    response, elapsed = world.run(post())

    _unavailable(response, elapsed)
    assert [row["status"] for row in world.credentials(setup.node_id)] == ["active"]
    assert world.fleet.records("node_credential_rotated", setup.organization_id) == []
    assert world.probe_get(enrolled.certificate, setup.zones[0]).status_code == 200


def test_other_contract_routes_keep_answering_while_the_enrollment_waits(
    world: EnrollmentWorld,
) -> None:
    other = world.declared()
    other_enrolled = world.enroll(other)
    setup = world.declared()
    body = world.body(setup, world.code(setup))
    app = _hung_app(world)
    probe_headers = {
        **alb_headers(other_enrolled.certificate),
        "X-Vigia-Contract-Version": VERSION,
    }

    async def scenario() -> tuple[httpx.Response, httpx.Response, bool]:
        async with _client(app) as client:
            enrollment = asyncio.ensure_future(
                client.post(
                    ENROLLMENT_PATH, json=body, headers={"X-Vigia-Contract-Version": VERSION}
                )
            )
            await asyncio.sleep(0)  # el alta ya está dentro, esperando a KMS
            probe = await client.get(
                f"/api/nodes/zones/{other.zones[0]}/catalog", headers=probe_headers
            )
            still_waiting = not enrollment.done()
            return probe, await enrollment, still_waiting

    probe, enrollment, still_waiting = world.run(scenario())

    assert probe.status_code == 200, probe.text
    assert probe.json()["node_id"] == str(other.node_id)
    assert still_waiting  # la ruta de prueba respondió con el alta todavía colgada
    assert enrollment.status_code == 503
