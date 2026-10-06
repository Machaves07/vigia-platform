"""PR-GOB-32: las siete tareas de U-03 con arranques, muertes y solapamientos (TASK-227).

``RuleBasedStateMachine`` (perfil ``ci``, semilla fija y semilla de la sesión registradas por
``tests/conftest.py``) con el **planificador real** (``PeriodicScheduler`` con ``SqlLeaseStore``)
sobre PostgreSQL 16 como ``vigia_app``, **dos procesos de worker** (dos pools y dos dueños de
arrendamiento) y el reloj simulado. El registro es el de la raíz (``fleet_tasks`` y
``catalog_tasks``): los seis manejadores por organización tal cual los compone la raíz (el depósito
de evidencias en memoria responde que cada clip existe) y ``regenerate_revocation_list`` con su
firma y su publicación reales sobre KMS, ``vigia-edge`` y el almacén de confianza en memoria.

Cada ejemplo siembra dos organizaciones nuevas con un trabajo pendiente para **cada** tarea: un
nodo mudo con la cola por encima del umbral y la credencial a punto de vencer
(``detect_mute_nodes``, ``evaluate_fleet_alarms``, ``alert_expiring_certificates``; NFR-GOB-47),
un código de alta vencido, una
concesión de clip de más de 24 h sin registro, una sesión de walk-test sin actividad desde hace 8
días y una marca de la lista de revocación. Las reglas:

- ``run``: un worker ejecuta lo que vence; ``overlap``: los dos a la vez (``asyncio.gather``);
- ``crash``: el worker ejecuta (solo o a la vez que el otro) y **muere** en la n-ésima organización
  de una tarea (después de que el manejador hiciera su trabajo, antes de confirmar): no libera el
  arrendamiento y se sustituye por un proceso nuevo; ``tick``: el reloj avanza (bordes 59/60/61 s
  del arrendamiento, una hora, un día); ``revoke``: una revocación nueva marca la lista.

Invariantes, después de cada paso: ninguna alarma duplicada (a lo sumo una fila por clase y
nodo), ninguna transición repetida (a lo sumo un ``node_communication_state_changed`` por nodo
mudo; el código, la sesión y la concesión solo avanzan) y la lista nunca publica una generación
que no se marcó; **una tarea retomada continúa desde la organización donde quedó**: su
``resumed_after`` es la última organización que la ejecución muerta confirmó y no vuelve a ninguna
anterior. Al final, tras dejar vencer los arrendamientos y correr los ciclos que falten, el efecto
es el de una ejecución por ciclo y organización: cada alarma y cada transición **exactamente una
vez** y ninguna marca perdida (``published_generation == dirty_generation``).

``ci`` acota los ejemplos para su presupuesto de 10 min; la profundidad es del perfil ``nightly``
(``nightly.yml`` en GitHub, VIG-171). Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import hashlib
import json
import os
import secrets
import uuid
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, ClassVar, Final, cast

import pytest
from hypothesis import seed as hypothesis_seed
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
    run_state_machine_as_test,
)

from tests.conftest import CI_PROFILE, _active_profile, _seeds_for_profile
from tests.fleet_credentials_support import MemoryKms, root_bundle_for
from tests.fleet_http_support import FleetStack, fleet_stack
from tests.fleet_inventory_support import InventoryWorld, NodeState
from tests.integration.conftest import PostgresEndpoint
from tests.outbox_support import app_database
from tests.revocation_list_support import TRUST_STORE_ARN, FakeTrustStore, MemoryEdge
from tests.worker_support import SimulatedCrash, synchronize
from vigia_platform.catalog.events import register_catalog_event_types
from vigia_platform.fleet.adapters.ca.crl_signing import NodeCaRevocationListSigner
from vigia_platform.fleet.adapters.ca.trust_store_publisher import TrustStorePublisher
from vigia_platform.fleet.adapters.postgres.credential_store import PostgresCredentialStore
from vigia_platform.fleet.adapters.postgres.revocation_list_state_store import (
    PostgresRevocationListStateStore,
)
from vigia_platform.fleet.adapters.postgres.revocation_mark_store import (
    PostgresRevocationMarkStore,
)
from vigia_platform.fleet.application.revocation_list_task import (
    TASK_NAME as REVOCATION_LIST,
)
from vigia_platform.fleet.application.revocation_list_task import RevocationListService
from vigia_platform.fleet.events import register_fleet_event_types
from vigia_platform.fleet.registration import U03_TASKS, catalog_tasks, fleet_tasks
from vigia_platform.shared.db import Database, Transaction
from vigia_platform.shared.observability.metrics import get_metrics
from vigia_platform.shared.outbox.registries import (
    GlobalTaskScope,
    OutboxCatalog,
    PeriodicTaskRegistry,
    TaskIteration,
)
from vigia_platform.shared.outbox.u02_events import register_u02_event_types
from vigia_platform.shared.runtime.units import UnitServices
from vigia_platform.shared.signing.keys import to_millisecond
from vigia_platform.shared.storage import ObjectHead
from vigia_platform.shared.worker.leases import SqlLeaseStore
from vigia_platform.shared.worker.scheduler import PeriodicScheduler, TaskRunReport

pytestmark = pytest.mark.integration

WORKERS: Final = ("w0", "w1")
TASKS: Final = tuple(sorted(U03_TASKS))
PER_ORGANIZATION: Final = tuple(
    name for name in TASKS if U03_TASKS[name][1] is TaskIteration.PER_ORGANIZATION
)
MUTE_AGE_MS: Final = 3_600_000
LOCK_TIMEOUT_MS: Final = 60_000
PROFILE_BUDGET: Final = {CI_PROFILE: (12, 12)}
"""(ejemplos, pasos) por perfil; cualquier otro (``nightly``), ``NIGHTLY_BUDGET``."""
NIGHTLY_BUDGET: Final = (60, 25)
EXPECTED_ALARMS: Final = ("certificate_expiring", "node_mute", "queue_over_threshold")


# --- Inyección de muertes -------------------------------------------------------------------------

CURRENT_WORKER: contextvars.ContextVar[str] = contextvars.ContextVar("pr_gob_32_worker")


@dataclass
class Injection:
    """La muerte armada y las invocaciones vistas en la ronda en curso de cada worker."""

    armed: dict[str, tuple[str, int]] = field(default_factory=dict)
    """worker → (tarea, posición de la organización en su ejecución)."""
    seen: dict[tuple[str, str], list[uuid.UUID | None]] = field(default_factory=dict)
    crashed: dict[str, tuple[str, list[uuid.UUID | None]]] = field(default_factory=dict)
    """worker → (tarea, organizaciones que la ejecución muerta confirmó, en orden)."""

    def reset(self) -> None:
        self.armed.clear()
        self.seen.clear()
        self.crashed.clear()

    def after(self, task: str, organization: uuid.UUID | None) -> None:
        """Tras el trabajo del manejador, dentro de su transacción: ¿muere aquí el proceso?"""
        worker = CURRENT_WORKER.get()
        seen = self.seen.setdefault((worker, task), [])
        plan = self.armed.get(worker)
        if plan is not None and plan[0] == task and plan[1] == len(seen):
            del self.armed[worker]
            self.crashed[worker] = (task, list(seen))
            raise SimulatedCrash()
        seen.append(organization)


INJECTION: Final = Injection()
STATS: Final[Counter[str]] = Counter()
"""Lo que ejercitaron los ejemplos: muertes, retomas y solapamientos (la propiedad no es vacía)."""


def _per_organization(name: str, handler: Any) -> Any:
    async def wrapped(transaction: Transaction) -> None:
        await handler(transaction)
        INJECTION.after(name, transaction.context.organization_id)

    return wrapped


def _global(name: str, handler: Any) -> Any:
    async def wrapped(scope: GlobalTaskScope) -> None:
        await handler(scope)
        INJECTION.after(name, None)

    return wrapped


# --- Dependencias en memoria ----------------------------------------------------------------------


class PresentClips:
    """``vigia-evidence`` en memoria: cada clip existe (``mark_orphan_clips`` lo deja huérfano)."""

    async def head_object(self, key: str) -> ObjectHead:
        return ObjectHead(
            key=key,
            size_bytes=1000,
            checksum_sha256=None,
            checksum_type=None,
            content_type="video/mp4",
            metadata={},
            version_id=None,
        )


class _Unused:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"la tarea no debía usar «{name}»")


class RecordingScheduler(PeriodicScheduler):
    """El planificador real, anotando cada ejecución en cuanto termina: si el proceso muere en una
    tarea posterior de la misma ronda, lo que ya terminó no se pierde para el modelo."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.reports: list[TaskRunReport] = []

    async def run_due(
        self, task_name: str, stop: asyncio.Event | None = None
    ) -> TaskRunReport | None:
        report = await super().run_due(task_name, stop)
        if report is not None:
            self.reports.append(report)
        return report


@dataclass
class Harness:
    """La pila, el catálogo con las siete tareas y lo que comparten los ejemplos."""

    stack: FleetStack
    catalog: OutboxCatalog
    databases: dict[str, Database]

    @property
    def clock(self) -> Any:
        return self.stack.authz.sessions.clock

    def scheduler(self, worker: str, generation: int) -> RecordingScheduler:
        database = self.databases[worker]
        contexts = self.stack.authz.contexts
        return RecordingScheduler(
            database=database,
            registry=self.catalog.periodic_tasks,
            leases=SqlLeaseStore(database=database, contexts=contexts),
            contexts=contexts,
            clock=self.clock,
            owner=f"{worker}-{generation}-{uuid.uuid4().hex[:6]}",
            poll_seconds=3600.0,
        )


def _harness(stack: FleetStack) -> Harness:
    sessions = stack.authz.sessions
    unused: Any = _Unused()
    services = UnitServices(
        clock=sessions.clock,
        metrics=get_metrics(),
        provider_organization_id=stack.authz.provider_organization_id,
        database=stack.database,
        contexts=stack.authz.contexts,
        authorizer=unused,
        audit=sessions.audit,
        outbox=cast(Any, stack.outbox),
        writer=stack.writer,
        free_text=unused,
        signing=unused,
        checkpoints=unused,
        kms=unused,
        evidence=cast(Any, PresentClips()),
    )
    composed = PeriodicTaskRegistry()
    fleet_tasks(composed, services)
    catalog_tasks(composed, services)
    assert {task.task_name for task in composed.tasks()} == set(U03_TASKS)
    catalog = OutboxCatalog()
    register_u02_event_types(catalog.event_types)
    register_fleet_event_types(catalog.event_types)
    register_catalog_event_types(catalog.event_types)
    revocation_list = _revocation_list(stack)
    for task in composed.tasks():
        schedule, iteration = U03_TASKS[task.task_name]
        assert (task.schedule, task.iteration) == (schedule, iteration)
        if task.task_name == REVOCATION_LIST:
            handler = _global(task.task_name, revocation_list)
        else:
            handler = _per_organization(task.task_name, task.handler)
        catalog.periodic_tasks.register(
            task.task_name, task.schedule, handler, unit=task.unit, iteration=task.iteration
        )
    stack.run(synchronize(stack.database, catalog, sessions.clock))
    databases = {
        "w0": stack.database,
        "w1": app_database(sessions.migrated, worker_pool_size=4, lock_timeout_ms=LOCK_TIMEOUT_MS),
    }
    return Harness(stack, catalog, databases)


def _revocation_list(stack: FleetStack) -> RevocationListService:
    """La lista de revocación de la raíz con KMS, ``vigia-edge`` y el almacén en memoria."""
    kms = MemoryKms()
    edge = MemoryEdge()
    body, _ = stack.run(root_bundle_for(kms, stack.authz.now()))
    edge.put_root(body)
    return RevocationListService(
        states=PostgresRevocationListStateStore(),
        credentials=PostgresCredentialStore(),
        signer=NodeCaRevocationListSigner(kms=kms, key_id=kms.key_id, roots=edge),
        publisher=TrustStorePublisher(
            storage=edge,
            elb=FakeTrustStore(edge.fetch),
            trust_store_arn=TRUST_STORE_ARN,
            bucket=edge.bucket,
        ),
        clock=stack.authz.sessions.clock,
        metrics=get_metrics(),
    )


# --- Siembra ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Organization:
    organization_id: uuid.UUID
    mute_node: uuid.UUID
    code_id: uuid.UUID
    clip_id: uuid.UUID
    session_id: uuid.UUID


def _seed(stack: FleetStack, now: datetime) -> Organization:
    """Una organización con un trabajo pendiente para cada tarea por organización."""
    world = InventoryWorld.build(stack)
    plant = world.plants[0]
    zones = world.zones(plant)
    node = world.add_node(plant, [zones[0]])
    # El estado, escrito un día antes: la credencial revocada de prueba que siembra
    # ``write_state`` (con un número de serie de 160 bits, que la lista no puede firmar) ya
    # venció, y la vigente vence enseguida (``alert_expiring_certificates`` la sigue viendo).
    world.write_state(
        node,
        NodeState(pending=500, certificate_in_ms=1_000, heartbeat_age_ms=MUTE_AGE_MS),
        now - timedelta(days=1),
    )
    organization = world.organization
    operator = stack.authz.operator_id
    # Un código de alta ``active`` que venció hace una hora.
    code_node = world.add_node(plant)
    code_id = uuid.uuid4()
    stack.execute(
        "INSERT INTO fleet.enrollment_code (code_id, organization_id, plant_id, node_id,"
        " code_hash, code_salt, issued_at, issued_by, expires_at, disclosed_at, status,"
        " ledger_record_id) VALUES ($1, $2, $3, $4, $5, $6, $7::timestamptz, $8,"
        " $7::timestamptz + interval '24 hours', $7::timestamptz, 'active', $9)",
        code_id,
        organization,
        plant,
        code_node,
        secrets.token_hex(32),
        os.urandom(16),
        now - timedelta(hours=25),
        operator,
        uuid.uuid4(),
    )
    # Una concesión de clip ``evidence`` de hace 30 h que ningún registro citó.
    clip_id = uuid.uuid4()
    zone = zones[-1]
    prefix = f"org/{organization}/plant/{plant}/zone/{zone}/node/{node}/"
    issued = now - timedelta(hours=30)
    stack.execute(
        "INSERT INTO fleet.clip_upload_grant (clip_id, organization_id, plant_id, zone_id,"
        " node_id, purpose, storage_key, content_type, max_size_bytes, required_headers,"
        " issued_at, expires_at, status) VALUES ($1, $2, $3, $4, $5, 'evidence', $6,"
        " 'video/mp4', 1000, $7::jsonb, $8::timestamptz, $8::timestamptz + interval '10 minutes',"
        " 'issued')",
        clip_id,
        organization,
        plant,
        zone,
        node,
        f"{prefix}{clip_id}.mp4",
        json.dumps(
            {
                "x-amz-checksum-sha256": base64.b64encode(
                    hashlib.sha256(b"clip").digest()
                ).decode(),
                "x-amz-meta-vigia-anonymized": "true",
            }
        ),
        issued,
    )
    # Una sesión de walk-test sin actividad desde hace 8 días.
    walk_zone = zones[1]
    world.add_catalog(walk_zone, [(uuid.uuid4(), "CAM-01")])
    session_id = uuid.uuid4()
    stack.execute(
        "INSERT INTO catalog.walk_test_session (session_id, organization_id, plant_id, zone_id,"
        " node_id, catalog_version, kind, status, passes_per_cell, matrix_rows, started_at,"
        " last_activity_at) VALUES ($1, $2, $3, $4, $5, 1, 'initial', 'in_progress', 3,"
        " '[]'::jsonb, $6::timestamptz, $6::timestamptz)",
        session_id,
        organization,
        plant,
        walk_zone,
        node,
        now - timedelta(days=8),
    )
    return Organization(organization, node, code_id, clip_id, session_id)


async def _mark(stack: FleetStack, now: datetime) -> int:
    async with stack.database.transaction(stack.authz.contexts.provider_audit_context()) as tx:
        return await PostgresRevocationMarkStore().mark_dirty(tx, now)


# --- La máquina ---------------------------------------------------------------------------------


class PeriodicTasks(RuleBasedStateMachine):
    harness: ClassVar[Harness]

    def __init__(self) -> None:
        super().__init__()
        self.organizations: list[Organization] = []
        self.schedulers: dict[str, RecordingScheduler] = {}
        self.generations = dict.fromkeys(WORKERS, 0)
        self.cursor: dict[str, uuid.UUID | None] = dict.fromkeys(PER_ORGANIZATION)
        """El avance que el modelo espera de cada tarea (``None``: ejecución nueva)."""
        self.ready = False

    # --- Utilidades ---------------------------------------------------------------------------

    @property
    def stack(self) -> FleetStack:
        return self.harness.stack

    def _now(self) -> datetime:
        now: datetime = self.harness.clock.now()
        return now

    def _restart(self, worker: str) -> None:
        self.generations[worker] += 1
        self.schedulers[worker] = self.harness.scheduler(worker, self.generations[worker])

    async def _one(self, worker: str) -> list[TaskRunReport]:
        """Lo que terminó en la ronda de ``worker`` (también si murió a mitad)."""
        token = CURRENT_WORKER.set(worker)
        scheduler = self.schedulers[worker]
        scheduler.reports.clear()
        try:
            await scheduler.run_pending()
        except SimulatedCrash:
            pass  # el proceso muere: lo ya terminado está en ``reports``
        finally:
            CURRENT_WORKER.reset(token)
        return list(scheduler.reports)

    def _run(self, workers: Sequence[str]) -> None:
        INJECTION.seen.clear()

        async def everyone() -> list[list[TaskRunReport]]:
            return list(await asyncio.gather(*(self._one(worker) for worker in workers)))

        outcomes = self.stack.run(everyone())
        for worker, reports in zip(workers, outcomes, strict=True):
            for report in reports:
                self._check_resume(worker, report)
        for worker in workers:
            crashed = INJECTION.crashed.pop(worker, None)
            if crashed is None:
                continue
            task, confirmed = crashed
            STATS["crashes"] += 1
            if task in self.cursor and confirmed:
                last = confirmed[-1]
                assert last is not None
                self.cursor[task] = last
            self._restart(worker)  # el proceso muerto se sustituye; su arrendamiento queda
        INJECTION.armed.clear()  # una muerte armada que no llegó a ocurrir no se arrastra

    def _check_resume(self, worker: str, report: TaskRunReport) -> None:
        """La tarea retomada continúa desde la organización donde quedó (NFR-GOB-47)."""
        task = report.task_name
        if task not in self.cursor:
            return
        assert report.resumed_after == self.cursor[task], (task, report.resumed_after)
        if report.resumed_after is not None:
            STATS["resumes"] += 1
        invoked = [o for o in INJECTION.seen.get((worker, task), []) if o is not None]
        if report.resumed_after is not None:
            assert all(o > report.resumed_after for o in invoked), (task, invoked)
        assert invoked == sorted(invoked), (task, invoked)
        if report.outcome is not None and not report.lease_lost:
            self.cursor[task] = None

    # --- Reglas --------------------------------------------------------------------------------

    @initialize()
    def seed(self) -> None:
        stack = self.stack
        (row,) = stack.fetch("SELECT now() AS now")
        self.harness.clock.set(to_millisecond(row["now"]))
        INJECTION.reset()
        provider = stack.authz.provider_organization_id
        stack.execute(
            "UPDATE identity.organization SET status = 'suspended'"
            " WHERE status = 'active' AND organization_id <> $1",
            provider,
        )
        now = self._now()
        self.organizations = [_seed(stack, now) for _ in range(2)]
        stack.execute(
            "UPDATE shared.periodic_task SET next_run_at = $1, lease_owner = NULL,"
            " lease_until = NULL, last_outcome = NULL, progress_run_at = NULL,"
            " progress_organization_id = NULL, progress_failures = 0"
            " WHERE task_name = ANY($2::text[])",
            now,
            list(TASKS),
        )
        stack.run(_mark(stack, now))
        for worker in WORKERS:
            self._restart(worker)
        self.ready = True

    @rule(worker=st.sampled_from(WORKERS))
    def run(self, worker: str) -> None:
        self._run([worker])

    @rule()
    def overlap(self) -> None:
        STATS["overlaps"] += 1
        self._run(WORKERS)

    @rule(
        worker=st.sampled_from(WORKERS),
        task=st.sampled_from(TASKS),
        position=st.integers(0, 2),
        together=st.booleans(),
    )
    def crash(self, worker: str, task: str, position: int, together: bool) -> None:
        """``worker`` muere en la organización ``position`` de ``task`` (si le toca): solo, o
        mientras el otro worker también ejecuta lo que vence."""
        # Por organización, tras confirmar al menos una (queda un avance); la global, en su única
        # invocación.
        position = max(position, 1) if task in self.cursor else 0
        INJECTION.armed[worker] = (task, position)
        self._run(WORKERS if together else [worker])

    @rule(seconds=st.sampled_from([1, 59, 60, 61, 120, 3600, 86_400]))
    def tick(self, seconds: int) -> None:
        self.harness.clock.advance(seconds)

    @rule()
    def revoke(self) -> None:
        self.stack.run(_mark(self.stack, self._now()))

    # --- Invariantes ----------------------------------------------------------------------------

    def _alarm_counts(self) -> dict[tuple[uuid.UUID, str], int]:
        rows = self.stack.fetch(
            "SELECT node_id, alarm_kind, count(*) AS n FROM fleet.fleet_alarm"
            " WHERE node_id = ANY($1::uuid[]) GROUP BY node_id, alarm_kind",
            [o.mute_node for o in self.organizations],
        )
        return {(uuid.UUID(str(r["node_id"])), r["alarm_kind"]): int(r["n"]) for r in rows}

    def _mute_transitions(self, organization: Organization) -> int:
        records = self.stack.records(
            "node_communication_state_changed", organization.organization_id
        )
        return sum(
            1
            for record in records
            if record["content"].get("node_id") == str(organization.mute_node)
            and record["content"].get("state") == "mute"
        )

    def _statuses(self, organization: Organization) -> tuple[str, str, str]:
        (row,) = self.stack.fetch(
            "SELECT (SELECT status FROM fleet.enrollment_code WHERE code_id = $1) AS code,"
            " (SELECT status FROM fleet.clip_upload_grant WHERE clip_id = $2) AS clip,"
            " (SELECT status FROM catalog.walk_test_session WHERE session_id = $3) AS session",
            organization.code_id,
            organization.clip_id,
            organization.session_id,
        )
        return row["code"], row["clip"], row["session"]

    @invariant()
    def never_twice(self) -> None:
        if not self.ready:
            return
        assert all(n == 1 for n in self._alarm_counts().values()), self._alarm_counts()
        for organization in self.organizations:
            assert self._mute_transitions(organization) <= 1
            code, clip, session = self._statuses(organization)
            assert code in {"active", "expired"}
            assert clip in {"issued", "orphan"}
            assert session in {"in_progress", "incomplete"}
        state = self.stack.revocation_state()
        assert state["published_generation"] <= state["dirty_generation"]

    def teardown(self) -> None:
        if not self.ready:
            return
        INJECTION.armed.clear()
        # Se dejan vencer los arrendamientos de los muertos y se corren los ciclos que falten
        # (dos evaluaciones seguidas para la histéresis de la cola).
        for _ in range(3):
            self.harness.clock.advance(61)
            self._run(["w0"])
        assert all(cursor is None for cursor in self.cursor.values()), self.cursor
        counts = self._alarm_counts()
        for organization in self.organizations:
            for kind in EXPECTED_ALARMS:
                assert counts.get((organization.mute_node, kind)) == 1, (kind, counts)
            assert self._mute_transitions(organization) == 1
            assert self._statuses(organization) == ("expired", "orphan", "incomplete")
        state = self.stack.revocation_state()
        assert state["published_generation"] == state["dirty_generation"]


@pytest.fixture(scope="module")
def harness(postgres_endpoint: PostgresEndpoint) -> Iterator[Harness]:
    with fleet_stack(postgres_endpoint, "pr_gob_32", pool=8) as stack:
        built = _harness(stack)
        try:
            yield built
        finally:
            stack.run(built.databases["w1"].dispose())


def test_pr_gob_32_each_cycle_and_organization_takes_effect_once(harness: Harness) -> None:
    examples, steps = PROFILE_BUDGET.get(_active_profile(), NIGHTLY_BUDGET)
    PeriodicTasks.harness = harness
    STATS.clear()
    for value in _seeds_for_profile():
        seeded = hypothesis_seed(value)(PeriodicTasks)
        run_state_machine_as_test(
            seeded, settings=settings(max_examples=examples, stateful_step_count=steps)
        )
    # La propiedad no es vacía: hubo muertes, retomas tras una muerte y solapamientos.
    assert STATS["crashes"] >= 1 and STATS["resumes"] >= 1 and STATS["overlaps"] >= 1, STATS
