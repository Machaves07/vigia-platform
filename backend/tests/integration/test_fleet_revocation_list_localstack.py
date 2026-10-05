"""``regenerate_revocation_list`` sobre PostgreSQL 16 y LocalStack (TASK-220; D-7, PR-GOB-23).

Base migrada con ``seed_identity`` (proveedora con su ``platform_operator`` y dos clientes con
plantas y nodos), como ``vigia_app``; ``vigia-edge`` es un depósito **versionado** de LocalStack;
el almacén ``vigia-node-trust`` es el doble ``FakeTrustStore`` con la interfaz de ``elbv2``
(LocalStack comunitario no implementa los almacenes de confianza; nunca una cuenta real, A-47),
que cuenta las entradas leyendo de LocalStack la versión exacta del objeto. La firma usa una clave
P-256 **de prueba** (``MemoryKms``) con la técnica de ``kms:Sign``.

- **De extremo a extremo por el planificador**: ``ca/crl.pem`` queda versionada, el almacén recibe
  esa versión, la anterior se retira, ``DescribeTrustStoreRevocations`` cuenta las mismas entradas
  que la base, la lista verifica con la clave pública de la raíz, ``next_update = last_update +
  7 días`` y no contiene certificados vencidos (criterio 3).
- **Lista global** (D-7, criterio 4): revocaciones en dos organizaciones → **una** lista con las de
  ambas, leída con un contexto de iteración por organización en transacciones de solo lectura; la
  lectura del repositorio con la RLS sorteada (superusuario) devuelve solo las de su organización:
  falla si se quita el filtro de organización de la sentencia.
- **Concurrencia** (criterio 2): una revocación confirmada mientras el ciclo publica deja la marca
  puesta y el ciclo siguiente la publica; dos workers que toman el ciclo a la vez publican una sola
  vez (arrendamiento); el ciclo forzado de ``vigia-admin`` y el del worker a la vez publican uno
  solo (candado de publicación).
- **Fallo** (criterio 5): el almacén colgado termina el ciclo en ≤ 5 s con la marca intacta y la
  ejecución ``partial_failure`` (``periodic_task_duration_ms`` con ``failed``).
- **Orden administrativa** (criterio 7): ``vigia-admin regenerate-revocation-list`` publica desde
  la base sin marca y deja ``revocation_list_regenerated`` en la auditoría de la proveedora.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import io
import json
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from cryptography import x509
from sqlalchemy import text

from tests.admin_support import OffsetClock
from tests.authz_support import SYSTEM_ACTOR_ID
from tests.dispatch_support import metric_points, metrics_with_reader
from tests.fleet_credentials_support import MemoryKms, root_bundle_for
from tests.identity_db import IdentitySeed, MigratedDatabase, seeded_identity
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint, versioned_bucket
from tests.ledger_database import DatabaseLoop
from tests.outbox_support import app_database
from tests.revocation_list_support import (
    TRUST_STORE_ARN,
    FakeTrustStore,
    crl_of,
    histogram_points,
)
from tests.worker_support import synchronize
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
    TASK_NAME,
    CycleOutcome,
    RevocationListCommand,
    RevocationListCycle,
    RevocationListService,
    register_regenerate_revocation_list,
)
from vigia_platform.fleet.domain.revocation_list import (
    PublishedRevocationList,
    SignedRevocationList,
)
from vigia_platform.identity.adapters.authz_store import PostgresContextStore
from vigia_platform.identity.application.admin_cli import (
    AdminConfig,
    AdminRuntime,
    Streams,
    build_parser,
    execute,
)
from vigia_platform.identity.authz.context import ScopeContexts
from vigia_platform.ledger.application.audit_writer import AuditWriter
from vigia_platform.shared.clock import SystemClock
from vigia_platform.shared.db import Database, DatabaseSettings, ProcessKind, SslMode
from vigia_platform.shared.observability.alerts_consumer import register_alerts_consumer
from vigia_platform.shared.observability.metrics import MetricName
from vigia_platform.shared.outbox.registries import OutboxCatalog, PeriodicTask
from vigia_platform.shared.outbox.u02_events import register_u02_event_types
from vigia_platform.shared.storage import S3Storage
from vigia_platform.shared.worker.leases import SqlLeaseStore, TaskOutcome
from vigia_platform.shared.worker.scheduler import OrganizationReads, PeriodicScheduler

pytestmark = pytest.mark.integration

DAY = dt.timedelta(days=1)
STEP_TIMEOUT = 5.0
WALL = SystemClock()
"""Reloj de pared, solo para medir el tope de producción."""
WALL_MARGIN = 3.0
"""Margen de pared sobre el tope de 5 s de un paso (segundos, retro 14)."""
WAIT_SECONDS = 30.0
"""Espera máxima de un suceso en las pruebas concurrentes (no es el asunto de la prueba)."""


@dataclass
class CrlWorld:
    loop: DatabaseLoop
    migrated: MigratedDatabase
    seed: IdentitySeed
    database: Database
    clock: OffsetClock
    contexts: ScopeContexts
    s3: Any
    bucket: str
    edge: S3Storage
    kms: MemoryKms
    root: x509.Certificate
    metrics: Any
    reader: Any
    store: FakeTrustStore = field(init=False)
    extra: list[Database] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.store = FakeTrustStore(self.fetch)

    def run(self, awaitable: Any) -> Any:
        return self.loop.run(awaitable)

    def fetch(self, bucket: str, key: str, version: str) -> bytes:
        body: bytes = self.s3.get_object(Bucket=bucket, Key=key, VersionId=version)["Body"].read()
        return body

    def publisher(self) -> TrustStorePublisher:
        return TrustStorePublisher(
            storage=self.edge, elb=self.store, trust_store_arn=TRUST_STORE_ARN, bucket=self.bucket
        )

    def service(self, publisher: Any = None) -> RevocationListService:
        return RevocationListService(
            states=PostgresRevocationListStateStore(),
            credentials=PostgresCredentialStore(),
            signer=NodeCaRevocationListSigner(
                kms=self.kms, key_id=self.kms.key_id, roots=self.edge
            ),
            publisher=publisher if publisher is not None else self.publisher(),
            clock=self.clock,
            metrics=self.metrics,
        )

    def catalog(self, service: RevocationListService) -> tuple[OutboxCatalog, PeriodicTask]:
        catalog = OutboxCatalog()
        register_u02_event_types(catalog.event_types)
        register_alerts_consumer(catalog.consumers)
        task = register_regenerate_revocation_list(catalog.periodic_tasks, service)
        return catalog, task

    def reads(self, task: PeriodicTask, database: Database | None = None) -> OrganizationReads:
        return OrganizationReads(
            database=database or self.database, contexts=self.contexts, task=task
        )

    def scheduler(
        self, catalog: OutboxCatalog, owner: str, database: Database | None = None
    ) -> PeriodicScheduler:
        database = database or self.database
        return PeriodicScheduler(
            database=database,
            registry=catalog.periodic_tasks,
            leases=SqlLeaseStore(database=database, contexts=self.contexts),
            contexts=self.contexts,
            clock=self.clock,
            owner=owner,
            metrics=self.metrics,
        )

    def new_database(self) -> Database:
        database = app_database(self.migrated, worker_pool_size=4)
        self.extra.append(database)
        return database

    # --- siembra y lectura como superusuario ----------------------------------------------------

    async def execute_async(self, sql: str, *args: Any) -> None:
        connection = await self.migrated.connect()
        try:
            await connection.execute(sql, *args)
        finally:
            await connection.close()

    def execute(self, sql: str, *args: Any) -> None:
        self.run(self.execute_async(sql, *args))

    def fetchall(self, sql: str, *args: Any) -> list[Any]:
        async def go() -> list[Any]:
            connection = await self.migrated.connect()
            try:
                return list(await connection.fetch(sql, *args))
            finally:
                await connection.close()

        rows: list[Any] = self.run(go())
        return rows

    def credential(self, tenant: str, status: str, **changes: Any) -> int:
        serial: int = self.run(self.credential_async(tenant, status, **changes))
        return serial

    async def credential_async(
        self,
        tenant: str,
        status: str,
        *,
        expires_at: dt.datetime | None = None,
        successor_issued_at: dt.datetime | None = None,
    ) -> int:
        """Una credencial de ``status`` en la primera planta de ``tenant`` (``a`` o ``b``)."""
        organization = getattr(self.seed, tenant)
        plant = organization.plants[0]
        now = self.clock.now()
        expires = expires_at or now + 300 * DAY
        issued = min(expires - DAY, now - 10 * DAY)
        serial = secrets.token_hex(19)
        credential_id = uuid.uuid4()
        await self.execute_async(
            "INSERT INTO fleet.node_credential (credential_id, organization_id, plant_id, node_id,"
            " certificate_serial, subject, issued_at, expires_at, status, revoked_at)"
            " VALUES ($1, $2, $3, $4, $5, jsonb_build_object('node_id', $4::uuid::text,"
            " 'organization_id', $2::uuid::text, 'plant_id', $3::uuid::text), $6, $7, $8, $9)",
            credential_id,
            organization.organization_id,
            plant.plant_id,
            plant.node_id,
            serial,
            issued,
            expires,
            status,
            now - DAY if status == "revoked" else None,
        )
        if successor_issued_at is not None:
            await self.execute_async(
                "INSERT INTO fleet.node_credential (credential_id, organization_id, plant_id,"
                " node_id, certificate_serial, subject, issued_at, expires_at, status,"
                " rotated_from) VALUES ($1, $2, $3, $4, $5, jsonb_build_object('node_id',"
                " $4::uuid::text, 'organization_id', $2::uuid::text, 'plant_id',"
                " $3::uuid::text), $6, $7, 'active', $8)",
                uuid.uuid4(),
                organization.organization_id,
                plant.plant_id,
                plant.node_id,
                secrets.token_hex(19),
                successor_issued_at,
                successor_issued_at + 365 * DAY,
                credential_id,
            )
        return int(serial, 16)

    def mark_dirty(self) -> int:
        """La marca que deja una revocación en su transacción (``PostgresRevocationMarkStore``)."""

        async def go() -> int:
            async with self.database.transaction(self.contexts.provider_audit_context()) as tx:
                return await PostgresRevocationMarkStore().mark_dirty(tx, self.clock.now())

        generation: int = self.run(go())
        return generation

    def state(self) -> Any:
        (row,) = self.fetchall("SELECT * FROM fleet.revocation_list_state")
        return row

    def expected_serials(self) -> set[int]:
        """La verdad, como superusuario: revocadas y sustituidas no vencidas de todas."""
        rows = self.fetchall(
            "SELECT certificate_serial FROM fleet.node_credential"
            " WHERE status IN ('revoked', 'superseded') AND expires_at > $1",
            self.clock.now(),
        )
        return {int(row["certificate_serial"], 16) for row in rows}

    def published(self) -> x509.CertificateRevocationList:
        (current,) = self.store.current()
        return crl_of(self.fetch(current.bucket, current.key, current.version))

    def versions(self) -> list[str]:
        listing = self.s3.list_object_versions(Bucket=self.bucket, Prefix="ca/crl.pem")
        return [item["VersionId"] for item in listing.get("Versions", [])]

    def reset(self) -> None:
        """Estado global limpio, ``ca/crl.pem`` sin versiones y almacén vacío entre pruebas."""
        listing = self.s3.list_object_versions(Bucket=self.bucket, Prefix="ca/crl.pem")
        for item in listing.get("Versions", []) + listing.get("DeleteMarkers", []):
            self.s3.delete_object(Bucket=self.bucket, Key=item["Key"], VersionId=item["VersionId"])
        self.execute(
            "UPDATE fleet.revocation_list_state SET dirty_generation = 0, dirty_since = NULL,"
            " published_generation = 0, published_at = NULL, object_version_id = NULL,"
            " next_update = NULL, entries = 0"
        )
        self.execute("DELETE FROM shared.periodic_task WHERE task_name = $1", TASK_NAME)
        self.store = FakeTrustStore(self.fetch)
        self.metrics, self.reader = metrics_with_reader()


@pytest.fixture(scope="module")
def world(
    postgres_endpoint: PostgresEndpoint, localstack_endpoint: LocalStackEndpoint
) -> Iterator[CrlWorld]:
    loop = DatabaseLoop()
    s3 = localstack_endpoint.aws_client("s3")
    with (
        seeded_identity(postgres_endpoint, "fleet_crl") as (migrated, seed),
        versioned_bucket(s3, "vigia-edge") as bucket,
    ):
        database = app_database(migrated, worker_pool_size=6)
        clock = OffsetClock()
        contexts = ScopeContexts(
            store=PostgresContextStore(database),
            clock=clock,
            provider_organization_id=seed.provider_organization_id,
            system_actor_id=SYSTEM_ACTOR_ID,
        )
        edge = S3Storage(localstack_endpoint.storage_settings(bucket), clock)
        kms = MemoryKms()
        body, root = loop.run(root_bundle_for(kms, clock.now()))
        loop.run(edge.put_object("ca/root.pem", body, "application/x-pem-file"))
        metrics, reader = metrics_with_reader()
        crl_world = CrlWorld(
            loop, migrated, seed, database, clock, contexts, s3, bucket, edge, kms, root, metrics,
            reader,
        )  # fmt: skip
        # Las organizaciones de otras pruebas no existen: esta base es solo del módulo.
        try:
            yield crl_world
        finally:
            for extra in crl_world.extra:
                loop.run(extra.dispose())
            loop.run(database.dispose())
            loop.close()


@pytest.fixture
def crl(world: CrlWorld) -> CrlWorld:
    world.reset()
    return world


def _due(world: CrlWorld, catalog: OutboxCatalog) -> None:
    world.run(synchronize(world.database, catalog, world.clock))
    world.execute(
        "UPDATE shared.periodic_task SET next_run_at = $2, lease_owner = NULL, lease_until = NULL"
        " WHERE task_name = $1",
        TASK_NAME,
        world.clock.now() - dt.timedelta(seconds=1),
    )


# --- Criterio 3: de extremo a extremo -------------------------------------------------------------


def test_the_scheduler_publishes_a_versioned_list_that_matches_the_database(crl: CrlWorld) -> None:
    now = crl.clock.now()
    revoked_a = crl.credential("a", "revoked")
    revoked_b = crl.credential("b", "revoked")
    superseded = crl.credential("b", "superseded", successor_issued_at=now - 3 * DAY)
    expired = crl.credential("a", "revoked", expires_at=now - DAY)
    active = crl.credential("a", "active")
    generation = crl.mark_dirty()
    service = crl.service()
    catalog, _ = crl.catalog(service)
    _due(crl, catalog)

    (report,) = crl.run(crl.scheduler(catalog, "worker-a").run_pending())
    assert (report.task_name, report.outcome, report.global_error_code) == (
        TASK_NAME,
        TaskOutcome.SUCCEEDED,
        None,
    )
    crl_list = crl.published()
    listed = {entry.serial_number for entry in crl_list}
    assert {revoked_a, revoked_b, superseded} <= listed
    assert expired not in listed and active not in listed
    assert listed == crl.expected_serials()
    assert crl_list.is_signature_valid(crl.root.public_key())  # type: ignore[arg-type]
    assert crl_list.issuer == crl.root.subject
    assert crl_list.next_update_utc - crl_list.last_update_utc == 7 * DAY
    (stored,) = crl.store.current()
    assert crl.versions() == [stored.version]
    assert stored.entries == len(crl.expected_serials())
    state = crl.state()
    assert (state["published_generation"], state["dirty_since"]) == (generation, None)
    assert state["object_version_id"] == stored.version
    assert state["entries"] == len(listed)
    assert state["next_update"] == crl_list.next_update_utc
    first_number = crl_list.extensions.get_extension_for_class(x509.CRLNumber).value.crl_number
    assert state["crl_number"] == first_number

    # Otra revocación: versión nueva, la anterior retirada del almacén y la cuenta al día.
    later = crl.credential("b", "revoked")
    crl.mark_dirty()
    crl.clock.offset += dt.timedelta(minutes=1)
    _due(crl, catalog)
    crl.run(crl.scheduler(catalog, "worker-a").run_pending())
    (newest,) = crl.store.current()
    assert crl.store.removed == [stored.revocation_id]
    assert crl.versions()[0] == newest.version and len(crl.versions()) == 2
    assert later in {entry.serial_number for entry in crl.published()}
    assert newest.entries == len(crl.expected_serials())
    number = crl.published().extensions.get_extension_for_class(x509.CRLNumber).value.crl_number
    assert number > first_number


# --- Criterio 4: lista global con lecturas por organización ------------------------------------


class RecordingCredentials(PostgresCredentialStore):
    def __init__(self) -> None:
        self.reads: list[tuple[uuid.UUID, str, str, set[int]]] = []

    async def revocation_facts(self, transaction: Any, now: dt.datetime) -> Any:
        facts = await super().revocation_facts(transaction, now)
        context = transaction.context
        self.reads.append(
            (
                context.organization_id,
                context.actor.kind.value,
                context.origin.value,
                {int(item.certificate_serial, 16) for item in facts},
            )
        )
        return facts


def test_one_global_list_is_read_with_one_context_per_organization(crl: CrlWorld) -> None:
    a = crl.credential("a", "revoked")
    b = crl.credential("b", "revoked")
    crl.mark_dirty()
    service = crl.service()
    recording = RecordingCredentials()
    service._credentials = recording
    _, task = crl.catalog(service)
    cycle: RevocationListCycle = crl.run(service.run_cycle(crl.reads(task)))
    assert cycle.outcome is CycleOutcome.PUBLISHED
    assert len(crl.store.added) == 1  # una sola lista para las dos organizaciones
    assert {a, b} <= {entry.serial_number for entry in crl.published()}
    by_organization = {organization: serials for organization, _, _, serials in recording.reads}
    # Las tres organizaciones activas (la proveedora, sin credenciales de nodo), una vez cada una.
    assert len(recording.reads) == len(by_organization) == 3
    assert by_organization[crl.seed.provider_organization_id] == set()
    assert a in by_organization[crl.seed.a.organization_id]
    assert b not in by_organization[crl.seed.a.organization_id]
    assert b in by_organization[crl.seed.b.organization_id]
    assert {(kind, origin) for _, kind, origin, _ in recording.reads} == {
        ("system", "periodic_iteration")
    }


def test_the_repository_read_filters_by_organization_even_without_row_security(
    crl: CrlWorld,
) -> None:
    """Como superusuario la RLS no aplica: solo el filtro de la sentencia separa a A de B."""
    a = crl.credential("a", "revoked")
    b = crl.credential("b", "revoked")
    owner = Database.create(
        DatabaseSettings(
            url=crl.migrated.as_role(None).sqlalchemy_url,
            process=ProcessKind.WORKER,
            sslmode=SslMode.DISABLE,
            worker_pool_size=1,
        )
    )
    crl.extra.append(owner)
    _, task = crl.catalog(crl.service())
    context = crl.contexts.context_for_organization(task, crl.seed.a.organization_id)

    async def read() -> set[int]:
        async with owner.transaction(context) as transaction:
            facts = await PostgresCredentialStore().revocation_facts(transaction, crl.clock.now())
        return {int(item.certificate_serial, 16) for item in facts}

    serials: set[int] = crl.run(read())
    assert a in serials and b not in serials


def test_the_organization_reads_are_read_only(crl: CrlWorld) -> None:
    _, task = crl.catalog(crl.service())
    reads = crl.reads(task)

    async def write_inside_read() -> str:
        try:
            async with reads.read(crl.seed.a.organization_id) as transaction:
                await transaction.execute(
                    text("UPDATE fleet.revocation_list_state SET entries = entries WHERE singleton")
                )
        except Exception as error:
            return str(getattr(error, "orig", error))
        return "escribió"

    assert "read-only transaction" in crl.run(write_inside_read())


# --- Criterio 2: concurrencia -------------------------------------------------------------------


class RevokingPublisher:
    """Publicador real que, antes de publicar, deja confirmada otra revocación (otra conexión)."""

    def __init__(self, world: CrlWorld, inner: TrustStorePublisher) -> None:
        self.world = world
        self.inner = inner
        self.serial: int | None = None

    async def publish(self, revocation_list: SignedRevocationList) -> PublishedRevocationList:
        if self.serial is None:
            self.serial = await self.world.credential_async("b", "revoked")

            async def mark() -> None:
                context = self.world.contexts.provider_audit_context()
                async with self.world.database.transaction(context) as tx:
                    await PostgresRevocationMarkStore().mark_dirty(tx, self.world.clock.now())

            # La revocación confirma mientras el ciclo tiene su candado: nunca espera por él.
            await asyncio.wait_for(mark(), timeout=WAIT_SECONDS)
        return await self.inner.publish(revocation_list)


def test_a_revocation_during_publication_keeps_the_mark_for_the_next_cycle(crl: CrlWorld) -> None:
    crl.credential("a", "revoked")
    generation = crl.mark_dirty()
    publisher = RevokingPublisher(crl, crl.publisher())
    service = crl.service(publisher)
    _, task = crl.catalog(service)
    first: RevocationListCycle = crl.run(service.run_cycle(crl.reads(task)))
    assert first.outcome is CycleOutcome.PUBLISHED and not first.mark_cleared
    state = crl.state()
    assert state["published_generation"] == generation
    assert state["dirty_generation"] == generation + 1
    assert state["dirty_since"] is not None
    assert publisher.serial not in {entry.serial_number for entry in crl.published()}
    second: RevocationListCycle = crl.run(service.run_cycle(crl.reads(task)))
    assert second.outcome is CycleOutcome.PUBLISHED and second.mark_cleared
    assert publisher.serial in {entry.serial_number for entry in crl.published()}
    state = crl.state()
    assert state["published_generation"] == state["dirty_generation"]
    assert state["dirty_since"] is None


class GatedPublisher:
    """Publicador real que espera a que la prueba lo suelte (para solapar dos ciclos)."""

    def __init__(self, inner: TrustStorePublisher) -> None:
        self.inner = inner
        self.entered = asyncio.Event()
        self.go = asyncio.Event()
        self.calls = 0

    async def publish(self, revocation_list: SignedRevocationList) -> PublishedRevocationList:
        self.calls += 1
        self.entered.set()
        await asyncio.wait_for(self.go.wait(), timeout=WAIT_SECONDS)
        return await self.inner.publish(revocation_list)


def test_two_workers_taking_the_cycle_at_once_publish_once(crl: CrlWorld) -> None:
    crl.credential("a", "revoked")
    crl.mark_dirty()
    gate = GatedPublisher(crl.publisher())
    service = crl.service(gate)
    catalog, _ = crl.catalog(service)
    _due(crl, catalog)
    first = crl.scheduler(catalog, "worker-a")
    second = crl.scheduler(catalog, "worker-b", crl.new_database())

    async def both() -> tuple[Any, Any]:
        running = asyncio.create_task(first.run_due(TASK_NAME))
        await asyncio.wait_for(gate.entered.wait(), timeout=WAIT_SECONDS)
        other = await asyncio.wait_for(second.run_due(TASK_NAME), timeout=WAIT_SECONDS)
        gate.go.set()
        return await running, other

    report, other = crl.run(both())
    assert other is None  # el arrendamiento es del primero: el segundo no toma el ciclo
    assert report.outcome is TaskOutcome.SUCCEEDED
    assert gate.calls == 1 and len(crl.store.added) == 1


def test_the_admin_cycle_and_the_worker_cycle_at_once_publish_once(crl: CrlWorld) -> None:
    crl.credential("a", "revoked")
    crl.mark_dirty()
    gate = GatedPublisher(crl.publisher())
    service = crl.service(gate)
    _, task = crl.catalog(service)
    command = RevocationListCommand(service, crl.reads(task, crl.new_database()))

    async def both() -> tuple[RevocationListCycle, RevocationListCycle]:
        worker = asyncio.create_task(service.run_cycle(crl.reads(task)))
        await asyncio.wait_for(gate.entered.wait(), timeout=WAIT_SECONDS)
        admin = await asyncio.wait_for(command.regenerate(dry_run=False), timeout=WAIT_SECONDS)
        gate.go.set()
        return await worker, admin

    worker, admin = crl.run(both())
    assert (worker.outcome, admin.outcome) == (CycleOutcome.PUBLISHED, CycleOutcome.BUSY)
    assert gate.calls == 1 and len(crl.store.added) == 1


# --- Criterio 5: fallo con tope -----------------------------------------------------------------


def test_a_hanging_trust_store_ends_the_cycle_within_five_seconds_with_the_mark_intact(
    crl: CrlWorld,
) -> None:
    crl.credential("a", "revoked")
    generation = crl.mark_dirty()
    service = crl.service()
    catalog, _ = crl.catalog(service)
    _due(crl, catalog)
    crl.store.switch.hang.add("add")
    started = WALL.monotonic()
    try:
        (report,) = crl.run(crl.scheduler(catalog, "worker-a").run_pending())
    finally:
        crl.store.switch.release()
    assert WALL.monotonic() - started <= STEP_TIMEOUT + WALL_MARGIN + 2.0  # firma y S3 incluidas
    assert report.outcome is TaskOutcome.PARTIAL_FAILURE
    assert report.global_error_code == "crl_publish_failed"
    state = crl.state()
    assert (state["dirty_generation"], state["published_generation"]) == (generation, 0)
    failed = [
        attributes
        for attributes in histogram_points(crl.reader, MetricName.PERIODIC_TASK_DURATION_MS)
        if attributes.get("task") == TASK_NAME
    ]
    # La alarma revocation-list-publish-failed vigila exactamente esta serie (infra §8.1).
    assert failed == [{"task": TASK_NAME, "result": "failed"}]
    counted = metric_points(crl.reader, MetricName.REVOCATION_LIST_PUBLISH_FAILED)
    assert [value for _, value in counted] == [1]
    row = crl.fetchall(
        "SELECT last_outcome, last_success_at FROM shared.periodic_task WHERE task_name = $1",
        TASK_NAME,
    )[0]
    assert (row["last_outcome"], row["last_success_at"]) == ("partial_failure", None)


# --- Criterio 7: orden administrativa ----------------------------------------------------------


class _Unused:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"la orden no debe usar {name}")


def test_vigia_admin_regenerates_without_mark_and_audits(crl: CrlWorld) -> None:
    revoked = crl.credential("a", "revoked")
    # Ni marca ni regeneración diaria pendiente: la lista vigente es reciente.
    crl.execute(
        "UPDATE fleet.revocation_list_state SET published_at = $1, object_version_id = 'v0',"
        " next_update = $2",
        crl.clock.now(),
        crl.clock.now() + 7 * DAY,
    )
    service = crl.service()
    _, task = crl.catalog(service)
    command = RevocationListCommand(service, crl.reads(task))
    audit = AuditWriter(
        database=crl.database,
        clock=crl.clock,
        provider_organization_id=crl.seed.provider_organization_id,
    )

    async def builder(config: Any, provider_id: uuid.UUID) -> AdminRuntime:
        assert provider_id == crl.seed.provider_organization_id
        unused: Any = _Unused()
        return AdminRuntime(
            clock=crl.clock,
            database=_KeepOpen(crl.database),
            contexts=crl.contexts,
            authorizer=unused,
            genesis=unused,
            signing=unused,
            replay=unused,
            partitions=unused,
            drills=unused,
            secrets=unused,
            audit=audit,
            revocation_list=command,
        )

    environ = {
        "VIGIA_ENVIRONMENT": "test",
        "VIGIA_PROVIDER_ORGANIZATION_ID": str(crl.seed.provider_organization_id),
    }
    out, err = io.StringIO(), io.StringIO()
    # ``run`` abriría otro bucle con asyncio.run: la base del módulo vive en el suyo.
    args = build_parser().parse_args(
        ["regenerate-revocation-list", "--operator", str(crl.seed.operator_id)]
    )
    streams = Streams(io.StringIO(), out, err)
    code = crl.run(execute(args, AdminConfig.from_environ(environ), builder, streams))
    assert code == 0, err.getvalue()
    document = json.loads(out.getvalue())
    assert document["outcome"] == "published" and document["entries"] >= 1
    assert revoked in {entry.serial_number for entry in crl.published()}
    entries = crl.fetchall(
        "SELECT actor_kind, actor_id, operation, outcome, filters, organization_id"
        " FROM shared.audit_entry WHERE operation = 'revocation_list_regenerated'"
    )
    assert len(entries) == 1
    (entry,) = entries
    assert (entry["actor_kind"], entry["actor_id"], entry["outcome"]) == (
        "operator",
        crl.seed.operator_id,
        "success",
    )
    assert entry["organization_id"] == crl.seed.provider_organization_id
    raw = entry["filters"]
    filters = json.loads(raw if isinstance(raw, str) else bytes(raw).decode("utf-8"))
    assert filters["crl_number"] == document["crl_number"]
    assert "BEGIN" not in json.dumps(filters)


class _KeepOpen:
    """La base del módulo: ``vigia-admin`` la cierra al terminar y aquí se sigue usando."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def transaction(self, context: Any) -> contextlib.AbstractAsyncContextManager[Any]:
        return self._database.transaction(context)

    async def dispose(self) -> None:
        return None
