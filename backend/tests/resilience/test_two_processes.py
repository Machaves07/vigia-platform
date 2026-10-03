"""La suite con dos procesos de API y dos workers tras un balanceador local (NFR-NUC-06; TASK-141).

NFR-NUC-06: **cualquier instancia atiende cualquier petición**; el único estado en memoria es la
caché de claves y secretos y los cubos del límite de tasa (aproximados por instancia, documentado);
sesiones, retardo de fallos, aviso de tratamiento, bandeja y tareas, en PostgreSQL.

Montaje: **dos ``vigia-api`` reales** (``tests/resilience/api_process.py``: uvicorn, ``create_app``
con la cadena de middleware y las rutas de ``platform_units()``, PostgreSQL 16 como ``vigia_app``
con los ajustes de producción) detrás de un **balanceador local** de turno rotatorio por conexión
que solo enruta a los procesos con ``/health/ready`` en 200, y **dos ``vigia-worker`` reales**
(``tests/resilience/worker_process.py``). El cliente abre una conexión nueva por petición: dos
peticiones seguidas las atienden procesos distintos, y cada prueba comprueba que los dos sirvieron.

La suite recorre lo que U-02 expone por HTTP con estado compartido, siempre alternando procesos:

- salud: los dos listos tras el balanceador;
- sesión abierta en un proceso y usada en el otro (``/me``, ``/auth/sessions``); cierre de sesión
  en uno, que invalida en los dos; «cerrar las demás» desde un proceso, que invalida la otra sesión
  en los dos;
- retardo de fallos de BR-NUC-24 contado entre procesos: el intento que retiene es el mismo que
  con uno solo;
- aviso de tratamiento aceptado en un proceso y vigente en el otro;
- dos workers con una sola bandeja y una sola tarea periódica: cada efecto una vez;
- un proceso de API se para (``SIGTERM``): el balanceador deja de enrutarle y el otro atiende la
  misma sesión.

Solo datos generados.
"""

from __future__ import annotations

import signal
import uuid
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

import httpx
import pytest

from tests.hierarchy_support import HierarchyEnvironment, hierarchy_environment
from tests.integration.conftest import PostgresEndpoint
from tests.resilience.harness import WALL, free_port, wait_until
from tests.resilience.processes import (
    Balancer,
    ProcessGroup,
    balancer,
    process_environment,
    process_group,
    read_lines,
    wait_http,
    wait_worker_started,
)
from tests.worker_support import EFFECT_EVENT, PROBE_TASK
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME, THROTTLE_FREE_FAILURES
from vigia_platform.identity.domain.privacy_notice import CURRENT_PRIVACY_NOTICE_VERSION
from vigia_platform.shared.context import Role

pytestmark = pytest.mark.integration

API_MODULE: Final = "tests.resilience.api_process"
WORKER_MODULE: Final = "tests.resilience.worker_process"
SAME_ORIGIN: Final = {"Sec-Fetch-Site": "same-origin"}
REQUESTS: Final = 6
"""Peticiones por comprobación: con turno rotatorio, tres a cada proceso."""


@dataclass
class Cluster:
    env: HierarchyEnvironment
    group: ProcessGroup
    balancer: Balancer
    directory: Path
    api_ports: dict[str, int]
    worker_ports: dict[str, int]

    @property
    def url(self) -> str:
        return self.balancer.url

    def request(
        self,
        method: str,
        path: str,
        *,
        cookie: str | None = None,
        json: Any = None,
        source: str = "127.0.0.1",
    ) -> httpx.Response:
        """Una petición por el balanceador en una conexión nueva (la siguiente va al otro).

        ``source`` es la dirección del cliente (de ``127.0.0.0/8``): el balanceador la conserva,
        así que el retardo de fallos por origen distingue a los clientes.
        """
        headers = dict(SAME_ORIGIN)
        if cookie is not None:
            headers["Cookie"] = f"{SESSION_COOKIE_NAME}={cookie}"
        transport = httpx.HTTPTransport(local_address=source)
        with httpx.Client(base_url=self.url, timeout=30.0, transport=transport) as client:
            return client.request(method, path, headers=headers, json=json)

    def served(self) -> Counter[str]:
        return Counter(self.balancer.served)


def _api_environment(env: HierarchyEnvironment, port: int) -> dict[str, str]:
    migrated = env.authz.sessions.migrated
    return process_environment(
        VIGIA_TEST_DATABASE_URL=migrated.as_role("vigia_app").sqlalchemy_url,
        VIGIA_TEST_PROVIDER_ORGANIZATION=env.authz.provider_organization_id,
        VIGIA_TEST_PORT=port,
    )


@pytest.fixture(scope="module")
def cluster(postgres_endpoint: PostgresEndpoint, tmp_path_factory: Any) -> Iterator[Cluster]:
    directory = Path(tmp_path_factory.mktemp("two_processes"))
    with (
        hierarchy_environment(postgres_endpoint, "two_processes") as env,
        process_group(directory) as group,
    ):
        migrated = env.authz.sessions.migrated
        worker_ports = {name: free_port() for name in ("worker-a", "worker-b")}
        worker_base = process_environment(
            VIGIA_TEST_DATABASE_URL=migrated.as_role("vigia_app").sqlalchemy_url,
            VIGIA_TEST_PROVIDER_ORGANIZATION=env.authz.provider_organization_id,
            VIGIA_TEST_WORKER_LOG=directory / "worker.jsonl",
            VIGIA_TEST_WORKER_ORG_SECONDS=0.05,
            VIGIA_TEST_LEASE_SECONDS=3.0,
            VIGIA_TEST_RENEW_SECONDS=1.0,
            VIGIA_TEST_EFFECT_LOG=directory / "effects.jsonl",
            VIGIA_TEST_DELIVERY_LOG=directory / "deliveries.jsonl",
            VIGIA_TEST_KILL_MARK=directory / "killed.mark",
        )
        # Los workers primero: sincronizan el catálogo del arnés que la API también declara.
        for name, port in worker_ports.items():
            group.start(name, WORKER_MODULE, {**worker_base, "VIGIA_WORKER_HEALTH_PORT": str(port)})
        for name, port in worker_ports.items():
            wait_http(f"http://127.0.0.1:{port}/health/live", message=f"{name} no arrancó")
            # Fin del arranque: catálogo sincronizado antes de que la prueba toque las tareas.
            wait_worker_started(group, name)
        api_ports = {name: free_port() for name in ("api-a", "api-b")}
        for name, port in api_ports.items():
            group.start(name, API_MODULE, _api_environment(env, port))
        for name, port in api_ports.items():
            wait_http(
                f"http://127.0.0.1:{port}/health/ready",
                timeout=120,
                message=f"{name} no quedó listo: {group.spawned[name].tail()}",
            )
        with balancer(api_ports) as running:
            wait_until(
                lambda: running.healthy == set(api_ports),
                timeout=30,
                message="el balanceador no vio los dos procesos listos",
            )
            yield Cluster(env, group, running, directory, api_ports, worker_ports)


@dataclass(frozen=True)
class Person:
    user_id: uuid.UUID
    organization_id: uuid.UUID
    email: str
    password: str


def _person(cluster: Cluster, *, notice: str | None = CURRENT_PRIVACY_NOTICE_VERSION) -> Person:
    """Una persona activa, coordinadora SST de toda su organización (contraseña ``fake$``)."""
    authz = cluster.env.authz
    site = authz.add_site(plants=1, zones_per_plant=1)
    user = authz.sessions.add_user(site.organization_id, privacy_notice=notice)
    authz.assign(site.organization_id, user.user_id, Role.COORDINATOR_SST)
    return Person(user.user_id, user.organization_id, user.email, user.password)


def _cookie(response: httpx.Response) -> str | None:
    for header in response.headers.get_list("set-cookie"):
        name, _, rest = header.partition("=")
        if name == SESSION_COOKIE_NAME:
            return rest.split(";", 1)[0]
    return None


def _login(cluster: Cluster, person: Person) -> str:
    response = cluster.request(
        "POST", "/auth/login", json={"email": person.email, "password": person.password}
    )
    assert response.status_code == 200, response.text
    cookie = _cookie(response)
    assert cookie is not None
    return cookie


def _spread(cluster: Cluster, before: Counter[str]) -> Counter[str]:
    """Cuántas conexiones llevó el balanceador a cada proceso desde ``before``."""
    after = cluster.served()
    return Counter({name: after[name] - before[name] for name in cluster.api_ports})


def _both_served(spread: Counter[str]) -> None:
    assert all(spread[name] > 0 for name in spread), f"no atendieron los dos procesos: {spread}"


# --- Salud --------------------------------------------------------------------------------------


def test_both_api_processes_are_ready_behind_the_balancer(cluster: Cluster) -> None:
    before = cluster.served()
    statuses = [cluster.request("GET", "/health/ready").status_code for _ in range(REQUESTS)]
    assert statuses == [200] * REQUESTS
    _both_served(_spread(cluster, before))
    for port in cluster.worker_ports.values():
        assert httpx.get(f"http://127.0.0.1:{port}/health/live", timeout=5).status_code == 200


# --- Sesiones -------------------------------------------------------------------------------------


def test_a_session_opened_in_one_process_works_in_the_other(cluster: Cluster) -> None:
    person = _person(cluster)
    cookie = _login(cluster, person)
    before = cluster.served()
    answers = [cluster.request("GET", "/me", cookie=cookie) for _ in range(REQUESTS)]
    assert [a.status_code for a in answers] == [200] * REQUESTS, answers[-1].text
    assert {a.json()["user"]["user_id"] for a in answers} == {str(person.user_id)}
    listed = cluster.request("GET", "/auth/sessions", cookie=cookie)
    assert listed.status_code == 200, listed.text
    _both_served(_spread(cluster, before))


def test_logout_in_one_process_invalidates_the_session_in_both(cluster: Cluster) -> None:
    person = _person(cluster)
    cookie = _login(cluster, person)
    assert cluster.request("POST", "/auth/logout", cookie=cookie).status_code in (200, 204)
    before = cluster.served()
    answers = [cluster.request("GET", "/me", cookie=cookie) for _ in range(REQUESTS)]
    assert [a.status_code for a in answers] == [401] * REQUESTS
    assert {a.json()["code"] for a in answers} == {"unauthenticated"}
    _both_served(_spread(cluster, before))


def test_close_others_from_one_process_closes_them_in_both(cluster: Cluster) -> None:
    person = _person(cluster)
    kept, other = _login(cluster, person), _login(cluster, person)
    closed = cluster.request("POST", "/auth/sessions/close-others", cookie=kept)
    assert closed.status_code in (200, 204), closed.text
    before = cluster.served()
    for _ in range(REQUESTS // 2):
        assert cluster.request("GET", "/me", cookie=other).status_code == 401
        assert cluster.request("GET", "/me", cookie=kept).status_code == 200
    _both_served(_spread(cluster, before))


# --- Retardo de fallos ----------------------------------------------------------------------------


def test_the_failure_delay_is_counted_across_processes(cluster: Cluster) -> None:
    """BR-NUC-24: con los fallos repartidos entre procesos, retiene el mismo intento que con uno.

    Desde un origen propio (``127.0.0.24``): el retardo del origen no alcanza a las demás pruebas.
    """
    person = _person(cluster)
    origin = "127.0.0.24"
    wrong = {"email": person.email, "password": "no-es-la-clave-sintetica"}
    before = cluster.served()
    codes = [
        cluster.request("POST", "/auth/login", json=wrong, source=origin).json()["code"]
        for _ in range(THROTTLE_FREE_FAILURES + 1)
    ]
    held = cluster.request("POST", "/auth/login", json=wrong, source=origin)
    good = {"email": person.email, "password": person.password}
    # La cuenta queda retenida desde cualquier proceso y cualquier origen (BR-NUC-24), también
    # con la contraseña buena.
    from_elsewhere = cluster.request("POST", "/auth/login", json=good, source="127.0.0.25")
    assert codes == ["unauthenticated"] * (THROTTLE_FREE_FAILURES + 1)
    assert held.status_code == 429 and held.json()["code"] == "throttled"
    assert from_elsewhere.status_code == 429 and from_elsewhere.json()["code"] == "throttled"
    _both_served(_spread(cluster, before))


# --- Aviso de tratamiento -------------------------------------------------------------------------


def test_the_privacy_notice_accepted_in_one_process_holds_in_the_other(cluster: Cluster) -> None:
    person = _person(cluster, notice=None)
    cookie = _login(cluster, person)
    required = [cluster.request("GET", "/me", cookie=cookie) for _ in range(2)]
    assert {r.json()["code"] for r in required} == {"privacy_notice_required"}
    accepted = cluster.request(
        "POST",
        "/privacy-notice/accept",
        cookie=cookie,
        json={"notice_version": CURRENT_PRIVACY_NOTICE_VERSION},
    )
    assert accepted.status_code in (200, 204), accepted.text
    before = cluster.served()
    answers = [cluster.request("GET", "/me", cookie=cookie) for _ in range(REQUESTS)]
    assert [a.status_code for a in answers] == [200] * REQUESTS
    _both_served(_spread(cluster, before))


# --- Dos workers ----------------------------------------------------------------------------------


def test_two_workers_share_the_outbox_and_the_periodic_task(cluster: Cluster) -> None:
    env = cluster.env
    started = WALL.now()
    env.authz.execute(
        "INSERT INTO shared.periodic_task (task_name, unit, schedule, next_run_at)"
        " VALUES ($1, 'U-02', 'every:3600s', $2)"
        " ON CONFLICT (task_name) DO UPDATE SET next_run_at = EXCLUDED.next_run_at,"
        " lease_owner = NULL, lease_until = NULL, last_outcome = NULL",
        PROBE_TASK,
        started - timedelta(seconds=1),
    )

    def finished() -> bool:
        (row,) = env.fetch(
            "SELECT last_outcome, lease_owner FROM shared.periodic_task WHERE task_name = $1",
            PROBE_TASK,
        )
        return row["last_outcome"] == "succeeded" and row["lease_owner"] is None

    wait_until(finished, timeout=120, message="la tarea periódica no terminó")
    active = {
        row["organization_id"]
        for row in env.fetch(
            "SELECT organization_id FROM identity.organization WHERE status = 'active'"
        )
    }
    effects = Counter(
        uuid.UUID(row["organization"])
        for row in env.fetch(
            "SELECT payload->>'organization_id' AS organization FROM shared.outbox_event"
            " WHERE event_name = $1 AND created_at >= $2",
            EFFECT_EVENT,
            started - timedelta(seconds=5),
        )
    )
    # La tarea pasó una vez por cada organización activa, entre los dos workers.
    assert set(effects) == active and max(effects.values()) == 1
    published = {
        str(row["event_id"])
        for row in env.fetch(
            "SELECT event_id FROM shared.outbox_event WHERE event_name = $1 AND created_at >= $2",
            EFFECT_EVENT,
            started - timedelta(seconds=5),
        )
    }

    def delivered() -> bool:
        done = {entry["event_id"] for entry in read_lines(cluster.directory / "effects.jsonl")}
        return published <= done

    wait_until(delivered, timeout=120, message="la bandeja no entregó los efectos")
    effect_lines = Counter(e["event_id"] for e in read_lines(cluster.directory / "effects.jsonl"))
    assert all(effect_lines[event] == 1 for event in published), "cada efecto, una sola vez"
    statuses = {
        row["status"]
        for row in env.fetch(
            "SELECT d.status FROM shared.outbox_delivery d JOIN shared.outbox_event e"
            " ON e.event_id = d.event_id WHERE e.event_name = $1 AND e.created_at >= $2"
            " AND d.consumer_name = 'resilience_effect'",
            EFFECT_EVENT,
            started - timedelta(seconds=5),
        )
    }
    assert statuses == {"delivered"}
    for name in cluster.worker_ports:
        assert cluster.group.spawned[name].process.poll() is None, f"{name} sigue vivo"


# --- Un proceso de API se va ----------------------------------------------------------------------


def test_with_one_api_process_stopped_the_other_serves_the_same_session(cluster: Cluster) -> None:
    person = _person(cluster)
    cookie = _login(cluster, person)
    code = cluster.group.stop("api-a")
    assert code == 0 or code == -signal.SIGTERM, cluster.group.spawned["api-a"].tail()
    wait_until(
        lambda: cluster.balancer.healthy == {"api-b"},
        timeout=30,
        message="el balanceador siguió enrutando al proceso parado",
    )
    answers = [cluster.request("GET", "/me", cookie=cookie) for _ in range(REQUESTS)]
    assert [a.status_code for a in answers] == [200] * REQUESTS
