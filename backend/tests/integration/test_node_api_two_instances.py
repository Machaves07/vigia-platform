"""Dos procesos de ``vigia-api`` tras un balanceador local aplican la misma identidad (NFR-GOB-15).

Montaje como el de ``tests/resilience/test_two_processes.py`` (NFR-NUC-06): **dos procesos reales**
(``tests/node_api_process.py``: uvicorn, ``create_app`` con la cadena fija, la ruta de prueba
interna y la ``NodeApiGate`` real sobre PostgreSQL como ``vigia_app``) detrás del ``Balancer`` de
turno rotatorio por conexión. Cada comprobación envía la misma petición seis veces, en conexiones
nuevas: tres las atiende cada proceso, y las seis respuestas son **iguales** (estado y cuerpo).

- un nodo dado de alta con su zona: ``200`` con el mismo nodo y las mismas zonas en los dos;
- una zona de otra organización: ``node_zone_mismatch`` en los dos;
- una revocación confirmada en la base: la petición siguiente en **cualquiera** de los dos
  procesos responde ``node_revoked`` (sin caché en ninguno).

Solo datos generados.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
from cryptography import x509

from tests.integration.conftest import PostgresEndpoint
from tests.node_api_db import DbNode, insert_node, issue, revoke_credential
from tests.node_api_support import DAY, VERSION, TestAuthority, alb_headers
from tests.resilience.harness import free_port, wait_until
from tests.resilience.processes import (
    Balancer,
    balancer,
    process_environment,
    process_group,
    wait_http,
)
from tests.session_support import SessionEnvironment, session_environment
from vigia_platform.shared.clock import SystemClock

pytestmark = pytest.mark.integration

MODULE: Final = "tests.node_api_process"
REQUESTS: Final = 6
"""Peticiones por comprobación: con turno rotatorio, tres a cada proceso."""
CATALOG: Final = "/api/nodes/zones/{zone}/catalog"


@dataclass
class Pair:
    env: SessionEnvironment
    balancer: Balancer
    authority: TestAuthority

    def now(self) -> dt.datetime:
        # Los procesos usan el reloj del sistema: la siembra también, con márgenes de días.
        return SystemClock().now()

    def run(self, awaitable: Any) -> Any:
        return self.env.run(awaitable)

    def node(self, tenant: str = "a") -> DbNode:
        owner = getattr(self.env.seed, tenant)
        return self.run(
            insert_node(
                self.env.admin,
                owner.organization_id,
                owner.plants[0].plant_id,
                owner.user_id,
                self.now(),
            )
        )

    def same_answer(self, zone: uuid.UUID, certificate: x509.Certificate) -> tuple[int, Any]:
        """Envía ``REQUESTS`` veces la misma petición; las respuestas tienen que ser iguales."""
        headers = {**alb_headers(certificate), "X-Vigia-Contract-Version": VERSION}
        before = Counter(self.balancer.served)
        answers: list[tuple[int, Any]] = []
        for _ in range(REQUESTS):
            with httpx.Client(base_url=self.balancer.url, timeout=30.0) as client:
                response = client.get(CATALOG.format(zone=zone), headers=headers)
            assert response.headers["x-vigia-contract-version"] == VERSION
            answers.append((response.status_code, response.json()))
        served = Counter(self.balancer.served) - before
        assert set(served) == {"api-a", "api-b"}, served
        assert all(answer == answers[0] for answer in answers), answers
        return answers[0]


@pytest.fixture(scope="module")
def pair(postgres_endpoint: PostgresEndpoint, tmp_path_factory: Any) -> Iterator[Pair]:
    directory = Path(tmp_path_factory.mktemp("node_api_two_instances"))
    with (
        session_environment(postgres_endpoint, "node_api_two") as env,
        process_group(directory) as group,
    ):
        ports = {name: free_port() for name in ("api-a", "api-b")}
        for name, port in ports.items():
            group.start(
                name,
                MODULE,
                process_environment(
                    VIGIA_TEST_DATABASE_URL=env.migrated.as_role("vigia_app").sqlalchemy_url,
                    VIGIA_TEST_PROVIDER_ORGANIZATION=env.seed.provider_organization_id,
                    VIGIA_TEST_PORT=port,
                ),
            )
        for name, port in ports.items():
            wait_http(
                f"http://127.0.0.1:{port}/health/ready",
                timeout=120,
                message=f"{name} no quedó listo: {group.spawned[name].tail()}",
            )
        with balancer(ports) as running:
            wait_until(
                lambda: running.healthy == set(ports),
                timeout=30,
                message="el balanceador no vio los dos procesos listos",
            )
            yield Pair(env, running, TestAuthority())


def test_both_processes_apply_the_same_identity_and_answer_the_same(pair: Pair) -> None:
    node = pair.node()
    certificate, _ = pair.run(issue(pair.env.admin, pair.authority, node, pair.now() - DAY))
    status, body = pair.same_answer(node.zone_id, certificate)
    assert status == 200
    assert body["node_id"] == str(node.node_id) and body["zones"] == [str(node.zone_id)]


def test_a_foreign_zone_is_node_zone_mismatch_in_both(pair: Pair) -> None:
    node, other = pair.node("a"), pair.node("b")
    certificate, _ = pair.run(issue(pair.env.admin, pair.authority, node, pair.now() - DAY))
    status, body = pair.same_answer(other.zone_id, certificate)
    assert (status, body["code"]) == (403, "node_zone_mismatch")


def test_a_revocation_is_seen_by_the_next_request_in_either_process(pair: Pair) -> None:
    node = pair.node()
    certificate, credential = pair.run(
        issue(pair.env.admin, pair.authority, node, pair.now() - DAY)
    )
    assert pair.same_answer(node.zone_id, certificate)[0] == 200
    pair.run(revoke_credential(pair.env.admin, credential, pair.now()))
    status, body = pair.same_answer(node.zone_id, certificate)
    assert (status, body["code"], body["retryable"]) == (401, "node_revoked", False)
