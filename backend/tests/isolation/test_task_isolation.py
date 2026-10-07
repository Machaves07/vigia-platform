"""PR-GOB-12 sobre las siete tareas periódicas de U-03 (TASK-228; LC-GOB-18; D-7, nota T-07).

**Catálogo de casos** (``TASK_CASES``): una entrada por tarea del ``PeriodicTaskRegistry`` que
compone la raíz con **todas** las unidades registradas (``registered_units``), con su clase:

- ``PER_ORGANIZATION``: una invocación por organización, en su transacción y con
  ``context_for_organization`` (el actor del sistema, BR-NUC-03); se prueba aquí;
- ``GLOBAL``: la única excepción, ``regenerate_revocation_list`` (D-7): un ciclo por ejecución con
  lecturas de solo lectura por organización y la escritura de la marca global;
- ``INHERITED``: tareas de U-02, por organización, fuera del alcance de U-03; el caso dice dónde
  está su prueba.

``uncovered_tasks`` nombra cada tarea registrada sin caso; ``test_a_task_without_case_is_named``
lo demuestra con una tarea sonda. Corren sin base (la composición con servicios inertes).

Con la base (``fleet_stack``, como ``vigia_app``), cada prueba siembra **dos organizaciones nuevas**
con trabajo pendiente para cada tarea (el de ``tests/properties/gob/test_pr_gob_32``: un nodo mudo
con la cola sobre el umbral y la credencial a punto de vencer, un código de alta vencido, una
concesión de clip de 30 h sin registro y una sesión de walk-test sin actividad desde hace 8 días):

- cada tarea por organización, invocada como la invoca el planificador (una transacción con el
  contexto de la organización), cambia filas de **su** organización (la prueba no pasa por una
  tarea que no hace nada) y **ninguna** de la otra: huella de cada tabla con ``organization_id``,
  con la cadena del expediente, la auditoría y la bandeja de eventos;
- una tarea sonda que, iterando A, escribe en B se detecta (la comprobación no es vacía);
- ``regenerate_revocation_list`` lee las dos organizaciones y publica una sola lista con los
  revocados de A y de B; no cambia ninguna fila de ninguna organización: solo la fila global
  ``fleet.revocation_list_state`` (la marca única, sin ``organization_id``).

Solo datos generados (NFR-CTR-43). Reloj simulado desde la hora de la base, al milisegundo.
"""

from __future__ import annotations

import enum
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, cast
from unittest.mock import Mock

import pytest

from tests.fleet_credentials_support import MemoryKms, root_bundle_for
from tests.fleet_http_support import FleetStack, fleet_stack
from tests.integration.conftest import PostgresEndpoint
from tests.isolation.gob_world import changed_tables, fingerprint, organization_tables
from tests.properties.gob.test_pr_gob_32_periodic_tasks import PresentClips
from tests.properties.gob.test_pr_gob_32_periodic_tasks import _seed as seed_pending_work
from tests.revocation_list_support import TRUST_STORE_ARN, FakeTrustStore, MemoryEdge, crl_of
from vigia_platform.fleet.adapters.ca.crl_signing import NodeCaRevocationListSigner
from vigia_platform.fleet.adapters.ca.trust_store_publisher import (
    CRL_OBJECT_KEY,
    TrustStorePublisher,
)
from vigia_platform.fleet.adapters.postgres.credential_store import PostgresCredentialStore
from vigia_platform.fleet.adapters.postgres.revocation_list_state_store import (
    PostgresRevocationListStateStore,
)
from vigia_platform.fleet.adapters.postgres.revocation_mark_store import (
    PostgresRevocationMarkStore,
)
from vigia_platform.fleet.application.revocation_list_task import RevocationListService
from vigia_platform.fleet.registration import U03_TASKS, catalog_tasks, fleet_tasks
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.observability.metrics import get_metrics
from vigia_platform.shared.outbox.registries import (
    PeriodicHandler,
    PeriodicTask,
    PeriodicTaskRegistry,
    Schedule,
    TaskIteration,
)
from vigia_platform.shared.runtime.units import UnitServices, registered_units
from vigia_platform.shared.signing.keys import to_millisecond
from vigia_platform.shared.worker.scheduler import OrganizationReads

DAY: Final = timedelta(days=1)
REVOCATION_LIST: Final = "regenerate_revocation_list"


# --- Catálogo de casos ----------------------------------------------------------------------------


class TaskKind(enum.Enum):
    PER_ORGANIZATION = "per_organization"
    GLOBAL = "global"
    INHERITED = "inherited"


@dataclass(frozen=True)
class TaskCase:
    kind: TaskKind
    why: str = ""


_U02_WHY = (
    "tarea de U-02 por organización con context_for_organization; su aislamiento es de U-02"
    " (tests/integration/test_worker_iteration.py y la RLS de cada tabla)"
)

TASK_CASES: Final[dict[str, TaskCase]] = {
    # --- U-03 (LC-GOB-18): las seis por organización y la global (D-7) ---
    "detect_mute_nodes": TaskCase(TaskKind.PER_ORGANIZATION),
    "evaluate_fleet_alarms": TaskCase(TaskKind.PER_ORGANIZATION),
    "expire_enrollment_codes": TaskCase(TaskKind.PER_ORGANIZATION),
    "mark_orphan_clips": TaskCase(TaskKind.PER_ORGANIZATION),
    "expire_walk_test_sessions": TaskCase(TaskKind.PER_ORGANIZATION),
    "alert_expiring_certificates": TaskCase(TaskKind.PER_ORGANIZATION),
    REVOCATION_LIST: TaskCase(
        TaskKind.GLOBAL, "lista global de vigia-node-ca: excepción D-7 (nota T-07)"
    ),
    # --- U-02 ---
    **{
        name: TaskCase(TaskKind.INHERITED, _U02_WHY)
        for name in (
            "archive_audit_partitions",
            "create_partitions",
            "evidence_sample",
            "expire_concessions",
            "expire_sessions",
            "key_rotation_reminder",
            "restore_drill_age",
            "throttle_window_cleanup",
            "verify_chains_full",
            "verify_chains_incremental",
            "write_checkpoints",
        )
    },
}
"""Un caso por tarea registrada en la raíz (``uncovered_tasks`` lo exige)."""

PER_ORGANIZATION_TASKS: Final = tuple(
    sorted(name for name, case in TASK_CASES.items() if case.kind is TaskKind.PER_ORGANIZATION)
)


def composed_tasks(services: UnitServices | None = None) -> PeriodicTaskRegistry:
    """El registro de tareas que compone la raíz con todas las unidades registradas.

    Sin ``services``, con servicios inertes: las tareas se construyen pero ninguna se ejecuta.
    """
    if services is None:
        inert: Any = Mock()
        services = UnitServices(
            clock=inert,
            metrics=get_metrics(),
            provider_organization_id=uuid.uuid4(),
            database=inert,
            contexts=inert,
            authorizer=inert,
            audit=inert,
            outbox=inert,
            writer=inert,
            free_text=inert,
            signing=inert,
            checkpoints=inert,
            kms=inert,
        )
    registry = PeriodicTaskRegistry()
    for unit in registered_units():
        unit.periodic_tasks(registry, services)
    return registry


def uncovered_tasks(registry: PeriodicTaskRegistry) -> list[str]:
    """Las tareas de ``registry`` sin caso de aislamiento en ``TASK_CASES``."""
    return sorted({task.task_name for task in registry.tasks()} - set(TASK_CASES))


# --- Cobertura del catálogo (sin base) -----------------------------------------------------------


def test_every_registered_task_has_its_isolation_case() -> None:
    registry = composed_tasks()
    missing = uncovered_tasks(registry)
    assert not missing, f"tareas periódicas sin caso de aislamiento: {missing}"
    names = {task.task_name for task in registry.tasks()}
    assert set(TASK_CASES) == names, sorted(set(TASK_CASES) - names)
    for task in registry.tasks():
        case = TASK_CASES[task.task_name]
        if task.unit is ActorUnit.U03:
            assert task.task_name in U03_TASKS
            expected = (
                TaskKind.GLOBAL
                if task.iteration is TaskIteration.GLOBAL
                else TaskKind.PER_ORGANIZATION
            )
            assert case.kind is expected, task.task_name
        else:
            assert case.kind is TaskKind.INHERITED and case.why, task.task_name
            assert task.iteration is TaskIteration.PER_ORGANIZATION, task.task_name
    assert len(PER_ORGANIZATION_TASKS) == 6
    assert [n for n, c in TASK_CASES.items() if c.kind is TaskKind.GLOBAL] == [REVOCATION_LIST]


def test_a_task_without_case_is_named() -> None:
    registry = composed_tasks()

    async def probe(transaction: Transaction) -> None:  # pragma: no cover - no se ejecuta
        return None

    registry.register("sonda_sin_caso", Schedule.every(60), probe, unit=ActorUnit.U03)
    assert uncovered_tasks(registry) == ["sonda_sin_caso"]
    with pytest.raises(AssertionError, match="sonda_sin_caso"):
        missing = uncovered_tasks(registry)
        assert not missing, f"tareas periódicas sin caso de aislamiento: {missing}"


# --- El mundo ------------------------------------------------------------------------------------


@dataclass
class TaskWorld:
    stack: FleetStack
    tasks: dict[str, PeriodicTask]
    tables: list[str]

    @property
    def now(self) -> datetime:
        now: datetime = self.stack.authz.sessions.clock.now()
        return now

    def fingerprint(self, organization_id: uuid.UUID) -> dict[str, str]:
        return fingerprint(self.stack.fetch, organization_id, self.tables)

    async def iterate(
        self, task: PeriodicTask, organization_id: uuid.UUID, handler: PeriodicHandler
    ) -> None:
        """Una iteración del planificador: transacción con el contexto de la organización."""
        context = self.stack.authz.contexts.context_for_organization(task, organization_id)
        async with self.stack.database.transaction(context) as transaction:
            await handler(transaction)


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[TaskWorld]:
    with fleet_stack(postgres_endpoint, "task_isolation") as stack:
        clock = stack.authz.sessions.clock
        (row,) = stack.fetch("SELECT now() AS now")
        clock.set(to_millisecond(row["now"]))
        sessions = stack.authz.sessions
        inert: Any = Mock()
        services = UnitServices(
            clock=sessions.clock,
            metrics=get_metrics(),
            provider_organization_id=stack.authz.provider_organization_id,
            database=stack.database,
            contexts=stack.authz.contexts,
            authorizer=inert,
            audit=sessions.audit,
            outbox=cast(Any, stack.outbox),
            writer=stack.writer,
            free_text=inert,
            signing=inert,
            checkpoints=inert,
            kms=inert,
            evidence=cast(Any, PresentClips()),
        )
        registry = PeriodicTaskRegistry()
        fleet_tasks(registry, services)
        catalog_tasks(registry, services)
        tasks = {task.task_name: task for task in registry.tasks()}
        assert set(tasks) == set(U03_TASKS)
        yield TaskWorld(stack, tasks, organization_tables(stack.fetch))


def _iteration_leaks(
    world: TaskWorld,
    task: PeriodicTask,
    mine: uuid.UUID,
    other: uuid.UUID,
    handler: PeriodicHandler | None = None,
) -> tuple[list[str], list[str]]:
    """(tablas de ``other`` que cambió la iteración de ``mine``, tablas de ``mine`` que cambió)."""
    run = cast(PeriodicHandler, handler if handler is not None else task.handler)
    own_before, other_before = world.fingerprint(mine), world.fingerprint(other)
    world.stack.run(world.iterate(task, mine, run))
    return (
        changed_tables(other_before, world.fingerprint(other)),
        changed_tables(own_before, world.fingerprint(mine)),
    )


# --- Las seis por organización -------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("name", PER_ORGANIZATION_TASKS)
def test_each_iteration_touches_only_its_own_organization(world: TaskWorld, name: str) -> None:
    task = world.tasks[name]
    a = seed_pending_work(world.stack, world.now).organization_id
    b = seed_pending_work(world.stack, world.now).organization_id
    for mine, other in ((a, b), (b, a)):
        leaked, own = _iteration_leaks(world, task, mine, other)
        assert leaked == [], f"{name} iterando {mine} cambió {leaked} de la otra organización"
        # La prueba no pasa por una tarea que no hizo nada: su propia organización cambió.
        assert own, f"{name} no hizo nada en {mine}"


@pytest.mark.integration
def test_a_task_that_writes_into_another_organization_is_detected(world: TaskWorld) -> None:
    # Sonda: el manejador real de los códigos de alta, ejecutado con el contexto de B mientras el
    # planificador itera A. La comprobación de arriba lo ve.
    task = world.tasks["expire_enrollment_codes"]
    a = seed_pending_work(world.stack, world.now).organization_id
    b = seed_pending_work(world.stack, world.now).organization_id
    real = cast(PeriodicHandler, task.handler)

    async def misplaced(transaction: Transaction) -> None:
        await world.iterate(task, b, real)

    leaked, _ = _iteration_leaks(world, task, a, b, misplaced)
    assert leaked == ["fleet.enrollment_code"], leaked


# --- La global (D-7) -----------------------------------------------------------------------------


def _revocation_list(world: TaskWorld) -> tuple[RevocationListService, MemoryEdge]:
    """La lista de revocación con KMS, ``vigia-edge`` y el almacén de confianza en memoria."""
    stack = world.stack
    kms = MemoryKms()
    edge = MemoryEdge()
    body, _ = stack.run(root_bundle_for(kms, stack.authz.now()))
    edge.put_root(body)
    service = RevocationListService(
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
    return service, edge


def _revoked(world: TaskWorld, organization_id: uuid.UUID) -> int:
    """Una credencial revocada y no vencida de un nodo de la organización (19 octetos)."""
    (node,) = world.stack.fetch(
        "SELECT node_id, plant_id FROM identity.node_identity WHERE organization_id = $1"
        " ORDER BY created_at, node_id LIMIT 1",
        organization_id,
    )
    serial = secrets.token_hex(19)
    now = world.now
    world.stack.execute(
        "INSERT INTO fleet.node_credential (credential_id, organization_id, plant_id, node_id,"
        " certificate_serial, subject, issued_at, expires_at, status, revoked_at)"
        " VALUES ($1, $2, $3, $4, $5, jsonb_build_object('node_id', $4::uuid::text,"
        " 'organization_id', $2::uuid::text, 'plant_id', $3::uuid::text), $6, $7, 'revoked', $8)",
        uuid.uuid4(),
        organization_id,
        node["plant_id"],
        node["node_id"],
        serial,
        now - 10 * DAY,
        now + 300 * DAY,
        now - DAY,
    )
    return int(serial, 16)


@pytest.mark.integration
def test_the_revocation_list_is_the_global_exception_and_writes_no_organization(
    world: TaskWorld,
) -> None:
    stack = world.stack
    a = seed_pending_work(stack, world.now).organization_id
    b = seed_pending_work(stack, world.now).organization_id
    serials = {_revoked(world, a), _revoked(world, b)}

    async def mark() -> None:
        async with stack.database.transaction(stack.authz.contexts.provider_audit_context()) as tx:
            await PostgresRevocationMarkStore().mark_dirty(tx, world.now)

    stack.run(mark())
    (state,) = stack.fetch("SELECT dirty_generation, published_generation FROM"
                           " fleet.revocation_list_state")  # fmt: skip
    assert state["dirty_generation"] > state["published_generation"]
    task = world.tasks[REVOCATION_LIST]
    assert task.iteration is TaskIteration.GLOBAL
    service, edge = _revocation_list(world)
    prints = {organization: world.fingerprint(organization) for organization in (a, b)}
    reads = OrganizationReads(database=stack.database, contexts=stack.authz.contexts, task=task)
    stack.run(service(reads))
    # Una sola lista con los revocados de A y de B (D-7).
    listed = {
        entry.serial_number for entry in crl_of(edge.fetch(edge.bucket, CRL_OBJECT_KEY, None))
    }
    assert serials <= listed
    # Ninguna fila de ninguna organización; solo la marca global, sin organization_id.
    for organization, before in prints.items():
        assert changed_tables(before, world.fingerprint(organization)) == [], organization
    (after,) = stack.fetch("SELECT dirty_generation, published_generation FROM"
                           " fleet.revocation_list_state")  # fmt: skip
    assert after["published_generation"] == state["dirty_generation"]
    assert not stack.fetch(
        "SELECT 1 FROM pg_catalog.pg_attribute WHERE attrelid = 'fleet.revocation_list_state'"
        "::regclass AND attname = 'organization_id' AND NOT attisdropped"
    )
