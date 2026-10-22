"""La suite de U-04 con dos procesos de API y dos workers tras un balanceador local (TASK-353;
PAT-LAZ-ESC-05; NFR-NUC-06 heredado; LC-LAZ-40).

PAT-LAZ-ESC-05 decide que U-04 cabe en los **dos procesos que ya existen**: ``vigia-api`` sirve
sus rutas y ``vigia-worker`` sus consumidores, sus tareas y la composición de paquetes, con el
semáforo de ``export_worker`` (concurrencia 1) y el arrendamiento de la exportación. Montaje, como
``tests/resilience/test_two_processes.py``: **dos ``vigia-api`` reales** (uvicorn, ``create_app``,
PostgreSQL 16 como ``vigia_app``) detrás del balanceador local de turno rotatorio por conexión y
**dos ``vigia-worker`` reales**, todos con **U-04 cargado** (``VIGIA_TEST_U04``,
``tests/laz_process_support.py``): consumidores y tareas de U-04 con sus registradores reales (y
sus mamparos) y el estado de sus rutas. En los workers, ``export_worker`` es el ``ExportWorker``
real con un compositor que tarda y anota cuándo empieza y termina cada paquete (el gancho del
proceso de prueba).

Lo que se comprueba, siempre alternando procesos:

- U-04 cargado en los cuatro procesos: sus consumidores y tareas en el catálogo de la base con la
  unidad ``U-04``, y sus rutas atendidas por las dos API (sin sesión, ``unauthenticated``; nunca un
  404 ni un 500);
- la bandeja de notificaciones con estado compartido: la paginación por clave de
  ``GET /notifications`` alternando procesos no pierde ni repite ningún aviso, y un aviso marcado
  leído en un proceso consta leído en el otro;
- **exportaciones con dos workers**: tres solicitudes de cada una de dos organizaciones, pedidas a
  la vez por las dos API; todas terminan ``ready`` con ``export_package`` en un solo intento,
  **nunca dos de la misma organización se componen a la vez** (ni en el registro de composiciones
  ni en ``started_at``/``completed_at`` de la base), ningún proceso compone dos a la vez (el
  mamparo de ``export_worker``) y los dos workers compusieron.

Solo datos generados. Esperas por condición con topes de 120 s que nunca deciden un resultado.
"""

from __future__ import annotations

import itertools
import json
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import httpx
import pytest

from tests.export_support import export_body
from tests.hierarchy_support import HierarchyEnvironment, hierarchy_environment
from tests.integration.conftest import PostgresEndpoint
from tests.laz_process_support import (
    EXPORT_LOG_VARIABLE,
    EXPORT_SECONDS_VARIABLE,
    U04_VARIABLE,
    u04_units,
)
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
from vigia_platform.accreditation.export.worker import EXPORT_WORKER
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME
from vigia_platform.identity.domain.privacy_notice import CURRENT_PRIVACY_NOTICE_VERSION
from vigia_platform.shared.context import Role

pytestmark = pytest.mark.integration

API_MODULE: Final = "tests.resilience.api_process"
WORKER_MODULE: Final = "tests.resilience.worker_process"
SAME_ORIGIN: Final = {"Sec-Fetch-Site": "same-origin"}
REQUESTS: Final = 6
EXPORT_SECONDS: Final = 2.0
"""Lo que tarda cada composición del gancho: deja ver el solape si lo hubiera."""
EXPORTS_PER_ORGANIZATION: Final = 3
NOTIFICATIONS: Final = 23
PAGE: Final = 5


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
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Una petición por el balanceador en una conexión nueva (la siguiente va al otro)."""
        headers = dict(SAME_ORIGIN)
        if cookie is not None:
            headers["Cookie"] = f"{SESSION_COOKIE_NAME}={cookie}"
        with httpx.Client(base_url=self.url, timeout=30.0) as client:
            return client.request(method, path, headers=headers, json=json, params=params)

    def served(self) -> Counter[str]:
        return Counter(self.balancer.served)


@pytest.fixture(scope="module")
def cluster(postgres_endpoint: PostgresEndpoint, tmp_path_factory: Any) -> Iterator[Cluster]:
    directory = Path(tmp_path_factory.mktemp("laz_two_processes"))
    with (
        hierarchy_environment(postgres_endpoint, "laz_two_processes") as env,
        process_group(directory) as group,
    ):
        migrated = env.authz.sessions.migrated
        common = {
            "VIGIA_TEST_DATABASE_URL": migrated.as_role("vigia_app").sqlalchemy_url,
            "VIGIA_TEST_PROVIDER_ORGANIZATION": env.authz.provider_organization_id,
            U04_VARIABLE: "1",
        }
        worker_ports = {name: free_port() for name in ("worker-a", "worker-b")}
        worker_base = process_environment(
            **common,
            VIGIA_TEST_WORKER_LOG=directory / "worker.jsonl",
            VIGIA_TEST_WORKER_ORG_SECONDS=0.05,
            VIGIA_TEST_LEASE_SECONDS=3.0,
            VIGIA_TEST_RENEW_SECONDS=1.0,
            VIGIA_TEST_EFFECT_LOG=directory / "effects.jsonl",
            VIGIA_TEST_DELIVERY_LOG=directory / "deliveries.jsonl",
            VIGIA_TEST_KILL_MARK=directory / "killed.mark",
            **{
                EXPORT_LOG_VARIABLE: directory / "exports.jsonl",
                EXPORT_SECONDS_VARIABLE: EXPORT_SECONDS,
            },
        )
        # Los workers primero: sincronizan el catálogo (con U-04) que la API también declara.
        for name, port in worker_ports.items():
            group.start(name, WORKER_MODULE, {**worker_base, "VIGIA_WORKER_HEALTH_PORT": str(port)})
        for name, port in worker_ports.items():
            wait_http(f"http://127.0.0.1:{port}/health/live", message=f"{name} no arrancó")
            wait_worker_started(group, name)
        api_ports = {name: free_port() for name in ("api-a", "api-b")}
        for name, port in api_ports.items():
            group.start(name, API_MODULE, process_environment(**common, VIGIA_TEST_PORT=port))
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
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    email: str
    password: str


def _person(cluster: Cluster) -> Person:
    """Una coordinadora SST de toda una organización nueva (una planta con una zona)."""
    authz = cluster.env.authz
    site = authz.add_site(plants=1, zones_per_plant=1)
    user = authz.sessions.add_user(
        site.organization_id, privacy_notice=CURRENT_PRIVACY_NOTICE_VERSION
    )
    authz.assign(site.organization_id, user.user_id, Role.COORDINATOR_SST)
    ((plant, zone),) = site.zones()
    return Person(user.user_id, user.organization_id, plant, zone, user.email, user.password)


def _login(cluster: Cluster, person: Person) -> str:
    response = cluster.request(
        "POST", "/auth/login", json={"email": person.email, "password": person.password}
    )
    assert response.status_code == 200, response.text
    for header in response.headers.get_list("set-cookie"):
        name, _, rest = header.partition("=")
        if name == SESSION_COOKIE_NAME:
            return rest.split(";", 1)[0]
    raise AssertionError("sin cookie de sesión")


def _both_served(cluster: Cluster, before: Counter[str]) -> None:
    after = cluster.served()
    spread = {name: after[name] - before[name] for name in cluster.api_ports}
    assert all(count > 0 for count in spread.values()), f"no atendieron los dos: {spread}"


# --- U-04 cargado ---------------------------------------------------------------------------


def test_u04_is_loaded_in_both_api_processes_and_in_both_workers(cluster: Cluster) -> None:
    consumers = {
        row["consumer_name"]: row["unit"]
        for row in cluster.env.authz.fetch("SELECT consumer_name, unit FROM shared.consumer")
    }
    tasks = {
        row["task_name"]: row["unit"]
        for row in cluster.env.authz.fetch("SELECT task_name, unit FROM shared.periodic_task")
    }
    assert consumers.get(EXPORT_WORKER) == "U-04"
    assert sum(1 for unit in consumers.values() if unit == "U-04") >= 5
    assert sum(1 for unit in tasks.values() if unit == "U-04") >= 10
    assert {unit.name for unit in u04_units()} == {"loop", "accreditation", "notifications"}
    before = cluster.served()
    answers = [cluster.request("GET", "/notifications") for _ in range(REQUESTS)]
    assert [a.status_code for a in answers] == [401] * REQUESTS, answers[-1].text
    assert {a.json()["code"] for a in answers} == {"unauthenticated"}
    _both_served(cluster, before)
    for port in cluster.worker_ports.values():
        assert httpx.get(f"http://127.0.0.1:{port}/health/live", timeout=30).status_code == 200


# --- Notificaciones ---------------------------------------------------------------------------


def _seed_notifications(cluster: Cluster, person: Person, count: int) -> set[uuid.UUID]:
    """``count`` avisos de ``person`` (dos por marca: los empates se resuelven por el
    identificador)."""
    base = WALL.now() - timedelta(hours=1)
    ids = {uuid.uuid4() for _ in range(count)}
    rows = [
        (
            notification_id,
            person.organization_id,
            person.plant_id,
            person.zone_id,
            uuid.uuid4(),
            json.dumps({"kind": "finding", "id": str(uuid.uuid4())}),
            person.user_id,
            base + timedelta(milliseconds=index // 2),
        )
        for index, notification_id in enumerate(sorted(ids))
    ]
    admin = cluster.env.authz.sessions.admin

    async def seed() -> None:
        # Con el alcance de la organización: el disparador reclama la clave de idempotencia.
        async with admin.transaction():
            await admin.execute(
                "SELECT set_config('vigia.organization_id', $1, true)", str(person.organization_id)
            )
            await admin.execute("SELECT set_config('vigia.actor_kind', 'system', true)")
            await admin.executemany(
                "INSERT INTO notifications.notification (notification_id, organization_id,"
                " plant_id, zone_id, event_name, source_event_id, urgency, title_es, body_es,"
                " deep_link_ref, recipient_user_id, created_at) VALUES ($1, $2, $3, $4,"
                " 'finding_received', $5, 'normal', 'Hallazgo nuevo en la zona sintética',"
                " 'Hay un hallazgo por clasificar.', $6::jsonb, $7, $8)",
                rows,
            )

    cluster.env.authz.run(seed())
    return ids


def test_notifications_page_by_key_and_read_across_processes(cluster: Cluster) -> None:
    person = _person(cluster)
    ids = _seed_notifications(cluster, person, NOTIFICATIONS)
    cookie = _login(cluster, person)
    before = cluster.served()
    seen: list[uuid.UUID] = []
    after: str | None = None
    while True:
        params: dict[str, Any] = {"limit": PAGE}
        if after is not None:
            params["after"] = after
        page = cluster.request("GET", "/notifications", cookie=cookie, params=params)
        assert page.status_code == 200, page.text
        body = page.json()
        seen.extend(uuid.UUID(item["notification_id"]) for item in body["items"])
        after = body["next_after"]
        if after is None:
            break
    assert len(seen) == len(set(seen)) == NOTIFICATIONS and set(seen) == ids
    _both_served(cluster, before)
    chosen = seen[0]
    marked = cluster.request("POST", f"/notifications/{chosen}/read", cookie=cookie)
    assert marked.status_code == 204, marked.text
    answers = [
        cluster.request("GET", "/notifications", cookie=cookie, params={"status": "read"})
        for _ in range(2)
    ]
    for answer in answers:
        assert answer.status_code == 200, answer.text
        assert [item["notification_id"] for item in answer.json()["items"]] == [str(chosen)]


# --- Exportaciones con dos workers --------------------------------------------------------------


def _intervals(lines: list[dict[str, Any]]) -> dict[str, tuple[datetime, datetime, str, str]]:
    """Por exportación: (comienzo, final, organización, proceso) del registro del gancho."""
    starts: dict[str, dict[str, Any]] = {}
    found: dict[str, tuple[datetime, datetime, str, str]] = {}
    for line in lines:
        if line["event"] == "start":
            starts[line["export_id"]] = line
        else:
            start = starts[line["export_id"]]
            assert start["pid"] == line["pid"]
            found[line["export_id"]] = (
                datetime.fromisoformat(start["at"]),
                datetime.fromisoformat(line["at"]),
                line["organization_id"],
                line["owner"],
            )
    return found


def _disjoint(spans: list[tuple[datetime, datetime]]) -> bool:
    ordered = sorted(spans)
    return all(earlier[1] <= later[0] for earlier, later in itertools.pairwise(ordered))


def test_two_workers_never_compose_two_exports_of_one_organization_at_once(
    cluster: Cluster,
) -> None:
    people = [_person(cluster), _person(cluster)]
    cookies = {person.organization_id: _login(cluster, person) for person in people}
    before = cluster.served()
    start = WALL.now() - timedelta(days=10)
    requested: dict[uuid.UUID, list[str]] = defaultdict(list)
    # Intercaladas: las dos organizaciones piden a la vez, por las dos API.
    for index in range(EXPORTS_PER_ORGANIZATION):
        for person in people:
            body = export_body(
                person.plant_id, [person.zone_id], start=start + timedelta(hours=index), days=7
            )
            response = cluster.request(
                "POST", "/accreditation/exports", cookie=cookies[person.organization_id], json=body
            )
            assert response.status_code == 202, response.text
            requested[person.organization_id].append(response.json()["export_id"])
    _both_served(cluster, before)
    every = [export_id for ids in requested.values() for export_id in ids]

    def finished() -> bool:
        rows = cluster.env.authz.fetch(
            "SELECT state FROM accreditation.export_request WHERE export_id = ANY($1::uuid[])",
            every,
        )
        return len(rows) == len(every) and all(row["state"] == "ready" for row in rows)

    wait_until(finished, timeout=120, message="las exportaciones no terminaron")
    rows = cluster.env.authz.fetch(
        "SELECT r.export_id, r.organization_id, r.state, r.attempt_count, r.started_at,"
        " r.completed_at, p.export_id IS NOT NULL AS packaged"
        " FROM accreditation.export_request r"
        " LEFT JOIN accreditation.export_package p USING (export_id)"
        " WHERE r.export_id = ANY($1::uuid[])",
        every,
    )
    assert all(row["packaged"] and row["attempt_count"] == 1 for row in rows), rows
    # La base: por organización, los periodos de trabajo no se solapan.
    by_organization: dict[uuid.UUID, list[tuple[datetime, datetime]]] = defaultdict(list)
    for row in rows:
        by_organization[row["organization_id"]].append((row["started_at"], row["completed_at"]))
    assert all(_disjoint(spans) for spans in by_organization.values()), by_organization
    # El registro del gancho: lo mismo por organización y, por proceso, el mamparo (uno a la vez).
    composed = _intervals(read_lines(cluster.directory / "exports.jsonl"))
    assert set(composed) == set(every)
    per_organization: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    per_process: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    for began, ended, organization, owner in composed.values():
        per_organization[organization].append((began, ended))
        per_process[owner].append((began, ended))
    assert all(_disjoint(spans) for spans in per_organization.values()), per_organization
    assert all(_disjoint(spans) for spans in per_process.values()), per_process
    assert len(per_process) == 2, f"no compusieron los dos workers: {per_process}"
    for name in cluster.worker_ports:
        assert cluster.group.spawned[name].process.poll() is None, f"{name} sigue vivo"
