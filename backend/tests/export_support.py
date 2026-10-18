"""La exportación de TASK-317 sobre PostgreSQL 16 real como ``vigia_app`` y por HTTP.

``export_stack`` construye, sobre un ``authz_environment``, la bandeja con los eventos de U-02 y
de la acreditación (``export_requested`` incluido) y el consumidor ``export_worker`` registrado y
sincronizado, ``ExportRequests`` y la aplicación real (``create_app`` con la cadena fija de
middleware y ``ContextAuthorizer``). ``ExportStack`` da las personas, las peticiones, las filas de
la base, el contexto de un evento y ``ExportWorker`` con dobles:

- ``CountingCheckpoints``: cuenta las llamadas a ``write_checkpoints_now`` (U04-H-08);
- ``RecordingUploads``: las subidas por partes abortadas al retomar;
- ``RecordingAlarm``: las alarmas ``export-failed``;
- ``SyntheticComposer``: el compositor **sintético determinista** de esta tarea (TASK-318 entrega
  el real): tres artefactos; los hashes salen solo del ``ExportJob`` sin el número de intento, así
  que la misma solicitud da el mismo paquete en cualquier intento. Se le puede pedir que abra una
  subida por partes, que se pare en un artefacto hasta que la prueba lo suelte, que falle (sin
  declarar) en ciertos intentos o que declare un insumo ausente;
- ``StepSleep``: el ``sleep`` del acompañante del trabajo, que solo avanza cuando la prueba lo
  suelta (un paso cada vez): las decisiones de renovar y publicar salen del ``Clock`` simulado,
  nunca de la pared.

Solo datos generados (NFR-CTR-43). El reloj simulado arranca en la hora de la base (retro 14) y
ninguna marca se compara con la hora de la base. Topes de las esperas de la base: 60 s (retro 15).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import secrets
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

import httpx

from tests.api_support import World
from tests.authz_support import AuthzEnvironment, Site, authz_environment
from tests.factories import uuid7
from tests.integration.conftest import PostgresEndpoint
from tests.ledger_database import set_organization
from tests.outbox_support import app_database
from tests.writer_support import unit_context
from vigia_platform.accreditation.adapters.http.services import (
    ACCREDITATION_STATE_KEY,
    AccreditationHttp,
)
from vigia_platform.accreditation.events import register_accreditation_event_types
from vigia_platform.accreditation.export.composer_port import (
    ExportInputUnavailable,
    ExportJob,
    ExportPackage,
    ExportSink,
)
from vigia_platform.accreditation.export.download import ExportDownloads, ExportDownloadSigner
from vigia_platform.accreditation.export.lease import ExportLeaseSettings, SqlExportLeaseStore
from vigia_platform.accreditation.export.request import ExportRequests
from vigia_platform.accreditation.export.volumes import ExportLocation
from vigia_platform.accreditation.export.worker import (
    ExportWorker,
    register_export_worker,
)
from vigia_platform.identity.adapters.authz_store import LedgerProviderQueryLedger
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME
from vigia_platform.ledger.application.writer import EscritorExpediente
from vigia_platform.ledger.evidence import EvidenceVerifier
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.ledger.record_types.u02 import U02_RECORD_TYPES
from vigia_platform.ledger.registry import RecordTypeRegistry
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.context import ActorKind, ActorUnit, Role, ScopeContext, ScopeLevel
from vigia_platform.shared.db import Database, Transaction
from vigia_platform.shared.outbox.dispatcher import Dispatcher
from vigia_platform.shared.outbox.publish import Outbox, OutboxEvent
from vigia_platform.shared.outbox.registries import OutboxCatalog
from vigia_platform.shared.outbox.store import SqlOutboxCatalogStore
from vigia_platform.shared.outbox.u02_events import register_u02_event_types
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

__all__ = [
    "SAME_ORIGIN",
    "CountingCheckpoints",
    "ExportStack",
    "Person",
    "RecordingAlarm",
    "RecordingUploads",
    "StepSleep",
    "SyntheticComposer",
    "export_body",
    "export_stack",
]

SAME_ORIGIN: Final = {"Sec-Fetch-Site": "same-origin"}
LOCK_TIMEOUT_MS: Final = 60_000
ARTIFACTS: Final = ("document", "findings_json", "coverage")


# --- Dobles ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Stored:
    entry_id: uuid.UUID


@dataclass(frozen=True)
class _Result:
    chain: Any
    checkpoint: _Stored | None


@dataclass
class CountingCheckpoints:
    """``CheckpointPort`` que cuenta las llamadas y devuelve un punto de control por cadena."""

    calls: list[tuple[uuid.UUID, tuple[Any, ...]]] = field(default_factory=list)

    async def write_checkpoints_now(
        self, context: ScopeContext, chains: Any = None
    ) -> tuple[Any, ...]:
        chains = tuple(chains or ())
        self.calls.append((context.organization_id, chains))
        return tuple(_Result(chain, _Stored(uuid.uuid4())) for chain in chains)


@dataclass
class RecordingUploads:
    """``UploadPort`` que registra las subidas abortadas (y cada llamada, con su ubicación)."""

    aborted: list[str] = field(default_factory=list)
    calls: list[tuple[ExportLocation, str | None]] = field(default_factory=list)

    async def abort_previous_upload(
        self, location: ExportLocation, multipart_upload_id: str | None
    ) -> None:
        self.calls.append((location, multipart_upload_id))
        if multipart_upload_id is not None:
            self.aborted.append(multipart_upload_id)


@dataclass
class RecordingAlarm:
    """``ExportAlarm`` que registra cada alarma."""

    raised: list[dict[str, Any]] = field(default_factory=list)

    def export_failed(
        self,
        *,
        organization_id: uuid.UUID,
        plant_id: uuid.UUID,
        export_id: uuid.UUID,
        attempt_count: int,
    ) -> None:
        self.raised.append(
            {
                "organization_id": organization_id,
                "plant_id": plant_id,
                "export_id": export_id,
                "attempt_count": attempt_count,
            }
        )


class Undeclared(RuntimeError):
    """Fallo no declarado del compositor (el despachador reintentará)."""


@dataclass
class SyntheticComposer:
    """Compositor sintético determinista (TASK-317; el real es de TASK-318)."""

    open_upload: bool = True
    fail_attempts: set[int] = field(default_factory=set)
    declared: dict[int, str] = field(default_factory=dict)
    pause_at: tuple[int, int] | None = None
    """``(intento, índice del artefacto)``: se para al empezarlo hasta ``resume``."""
    jobs: list[ExportJob] = field(default_factory=list)
    packages: list[ExportPackage] = field(default_factory=list)
    paused: asyncio.Event = field(default_factory=asyncio.Event)
    resume: asyncio.Event = field(default_factory=asyncio.Event)

    async def compose(self, job: ExportJob, sink: ExportSink) -> ExportPackage:
        self.jobs.append(job)
        if self.open_upload:
            await sink.upload_started(f"upload-{job.export_id}-{job.attempt}")
        for index, artifact in enumerate(ARTIFACTS):
            await sink.artifact_started(artifact)
            if self.pause_at == (job.attempt, index):
                self.paused.set()
                await self.resume.wait()
            if job.attempt in self.declared and index == 1:
                raise ExportInputUnavailable(self.declared[job.attempt])
            if job.attempt in self.fail_attempts and index == 1:
                raise Undeclared("fallo sintético")
            await sink.artifact_finished(artifact, index + 1, len(ARTIFACTS))
        package = synthetic_package(job)
        self.packages.append(package)
        return package


def _job_digest(job: ExportJob, salt: str) -> str:
    """Solo del alcance y de lo fijado en el primer intento; nunca del número de intento."""
    document = {
        "salt": salt,
        "export_id": str(job.export_id),
        "organization_id": str(job.organization_id),
        "plant_id": str(job.plant_id),
        "zone_ids": [str(zone) for zone in job.zone_ids],
        "period": [format_timestamp(job.period_from), format_timestamp(job.period_to)],
        "package_form": job.package_form.value,
        "generated_at": format_timestamp(job.generated_at),
        "checkpoint_record_ids": [str(value) for value in job.checkpoint_record_ids],
    }
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()


def synthetic_package(job: ExportJob) -> ExportPackage:
    return ExportPackage(
        document_sha256=_job_digest(job, "document"),
        archive_sha256=_job_digest(job, "archive"),
        manifest={"manifest_version": 1, "generated_at": format_timestamp(job.generated_at)},
        size_bytes=1024 * len(ARTIFACTS),
        artifact_count=len(ARTIFACTS),
        verifier_version="1.0.0",
    )


@dataclass
class StepSleep:
    """El ``sleep`` del acompañante: cada llamada espera un ``step`` de la prueba."""

    steps: asyncio.Queue[None] = field(default_factory=asyncio.Queue)
    waiting: int = 0
    done: asyncio.Queue[None] = field(default_factory=asyncio.Queue)

    async def __call__(self, seconds: float) -> None:
        # Avisa de que la vuelta anterior del acompañante terminó y espera la siguiente.
        self.done.put_nowait(None)
        await self.steps.get()

    async def step(self) -> None:
        """Una vuelta del acompañante: la suelta y espera a que vuelva a dormir."""
        await asyncio.wait_for(self.done.get(), 60)
        self.steps.put_nowait(None)
        await asyncio.wait_for(self.done.get(), 60)
        self.done.put_nowait(None)


# --- Pila ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Person:
    organization_id: uuid.UUID
    user_id: uuid.UUID


@dataclass(frozen=True)
class _EventRef:
    """Lo que ``context_from_event`` lee de un evento."""

    organization_id: uuid.UUID
    correlation_id: uuid.UUID


def export_body(
    plant: uuid.UUID,
    zones: list[uuid.UUID],
    *,
    start: datetime,
    days: float = 30,
    form: str = "without_clips",
) -> dict[str, Any]:
    return {
        "plant_id": str(plant),
        "zone_ids": [str(zone) for zone in zones],
        "period": {
            "from": format_timestamp(start),
            "to": format_timestamp(start + timedelta(days=days)),
        },
        "package_form": form,
    }


@dataclass
class ExportStack:
    authz: AuthzEnvironment
    database: Database
    catalog: OutboxCatalog
    outbox: Outbox
    exports: ExportRequests
    app: Any
    client: httpx.AsyncClient
    worker: ExportWorker | None = None
    """El manejador que el consumidor registrado invoca (lo fija cada prueba)."""
    extra_databases: list[Database] = field(default_factory=list)

    def run(self, awaitable: Any) -> Any:
        return self.authz.run(awaitable)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.authz.fetch(sql, *args)

    def execute(self, sql: str, *args: Any) -> None:
        self.authz.execute(sql, *args)

    @property
    def clock(self) -> Any:
        return self.authz.sessions.clock

    def now(self) -> datetime:
        return self.authz.now()

    def tick(self, seconds: float = 1.0) -> None:
        self.clock.advance(seconds)

    def new_database(self) -> Database:
        """Otro pool, como el de otro proceso."""
        database = app_database(
            self.authz.sessions.migrated, worker_pool_size=8, lock_timeout_ms=LOCK_TIMEOUT_MS
        )
        self.extra_databases.append(database)
        return database

    # --- Personas -------------------------------------------------------------------------------

    def site(self, plants: int = 2, zones: int = 3) -> Site:
        return self.authz.add_site(plants=plants, zones_per_plant=zones)

    def member(
        self,
        site: Site,
        role: Role = Role.COORDINATOR_SST,
        level: ScopeLevel = ScopeLevel.ORGANIZATION,
        scope: uuid.UUID | None = None,
    ) -> Person:
        user = self.authz.add_user(site.organization_id)
        self.authz.assign(site.organization_id, user, role, level, scope)
        return Person(site.organization_id, user)

    def context(self, person: Person) -> ScopeContext:
        """El contexto de una sesión nueva de ``person``, como lo construye la cadena."""
        cookie = self.authz.open_session(person.organization_id, person.user_id)
        scope = self.run(self.authz.contexts.context_from_session(cookie))
        context: ScopeContext = scope.context
        return context

    def event_context(self, organization_id: uuid.UUID) -> ScopeContext:
        """El contexto que da el despachador a la entrega (organización del evento, U-04)."""
        return self.authz.contexts.context_from_event(
            _EventRef(organization_id, uuid7()), unit=ActorUnit.U04
        )

    # --- Peticiones -----------------------------------------------------------------------------

    def send(self, person: Person, method: str, path: str, body: Any = None) -> httpx.Response:
        # Una sesión nueva por petición: el reloj simulado salta horas y días.
        cookie = self.authz.open_session(person.organization_id, person.user_id)
        headers = {**SAME_ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}"}
        response: httpx.Response = self.run(
            self.client.request(method, path, json=body, headers=headers)
        )
        return response

    def request_export(self, person: Person, body: dict[str, Any]) -> httpx.Response:
        return self.send(person, "POST", "/accreditation/exports", body)

    def status(self, person: Person, export_id: Any) -> httpx.Response:
        return self.send(person, "GET", f"/accreditation/exports/{export_id}")

    # --- Base -----------------------------------------------------------------------------------

    def add_evidence(
        self,
        organization_id: uuid.UUID,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        size_bytes: int,
        at: datetime,
    ) -> None:
        """Un clip ya custodiado de ``size_bytes`` (solo la fila: no hay bytes en ningún sitio)."""
        admin = self.authz.sessions.admin

        async def insert() -> None:
            async with admin.transaction():
                # El disparador que reclama ``evidence_id`` respeta la RLS (ledger_database).
                await set_organization(admin, organization_id)
                await admin.execute(
                    "INSERT INTO ledger.evidence (evidence_id, organization_id, plant_id,"
                    " zone_id, node_id, record_id, clip_id, camera_id, storage_key, sha256,"
                    " size_bytes, content_type, media_kind, duration_ms, segment, starts_at,"
                    " ends_at, verified_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8,"
                    " 'org/x/clip.mp4', $9, $10, 'video/mp4', 'video', 5000, 'full', $11, $11,"
                    " $12)",
                    uuid.uuid4(),
                    organization_id,
                    plant_id,
                    zone_id,
                    uuid.uuid4(),
                    uuid.uuid4(),
                    uuid.uuid4(),
                    uuid.uuid4(),
                    secrets.token_hex(32),
                    size_bytes,
                    at,
                    at + timedelta(minutes=1),
                )

        self.run(insert())

    def export_row(self, export_id: Any) -> Any:
        return self.run(self.export_row_now(export_id))

    async def export_row_now(self, export_id: Any) -> Any:
        """``export_row`` dentro de un escenario que ya corre en el bucle de la base."""
        return await self.authz.sessions.admin.fetchrow(
            "SELECT * FROM accreditation.export_request WHERE export_id = $1",
            uuid.UUID(str(export_id)),
        )

    async def package_row_now(self, export_id: Any) -> Any:
        return await self.authz.sessions.admin.fetchrow(
            "SELECT * FROM accreditation.export_package WHERE export_id = $1",
            uuid.UUID(str(export_id)),
        )

    def requests_of(self, organization_id: uuid.UUID) -> list[Any]:
        return self.fetch(
            "SELECT * FROM accreditation.export_request WHERE organization_id = $1"
            " ORDER BY requested_at, export_id",
            organization_id,
        )

    def package_row(self, export_id: Any) -> Any:
        rows = self.fetch(
            "SELECT * FROM accreditation.export_package WHERE export_id = $1",
            uuid.UUID(str(export_id)),
        )
        return rows[0] if rows else None

    def events(self, organization_id: uuid.UUID) -> list[Any]:
        return self.fetch(
            "SELECT event_id, plant_id, partition_key, payload::text AS payload, correlation_id"
            " FROM shared.outbox_event WHERE event_name = 'export_requested'"
            " AND organization_id = $1 ORDER BY created_at, publish_seq",
            organization_id,
        )

    def audit(self, organization_id: uuid.UUID) -> list[Any]:
        return self.fetch(
            "SELECT operation, outcome, scope_plant_id, resource_kind, resource_id,"
            " correlation_id FROM shared.audit_entry WHERE organization_id = $1"
            " AND operation = 'export_requested' ORDER BY chain_sequence",
            organization_id,
        )

    # --- Consumidor -----------------------------------------------------------------------------

    def leases(
        self, database: Database | None = None, settings: ExportLeaseSettings | None = None
    ) -> SqlExportLeaseStore:
        return SqlExportLeaseStore(database=database or self.database, settings=settings)

    def export_worker(
        self,
        *,
        composer: Any = None,
        checkpoints: CountingCheckpoints | None = None,
        uploads: Any = None,
        alarm: RecordingAlarm | None = None,
        database: Database | None = None,
        sleep: Any = None,
        owner: str | None = None,
    ) -> ExportWorker:
        return ExportWorker(
            leases=self.leases(database),
            checkpoints=checkpoints or CountingCheckpoints(),
            uploads=uploads or RecordingUploads(),
            composer=composer or SyntheticComposer(),
            alarm=alarm or RecordingAlarm(),
            clock=self.clock,
            owner=owner or f"worker-{uuid.uuid4().hex[:8]}",
            sleep=sleep or _forever,
            tick_seconds=1.0,
        )

    def dispatcher(self) -> Dispatcher:
        return Dispatcher(
            database=self.database,
            catalog=self.catalog,
            outbox=self.outbox,
            contexts=self.authz.contexts,
            clock=self.clock,
            jitter=lambda: 0.5,
        )


async def _forever(seconds: float) -> None:
    """Sin ``StepSleep``, el acompañante no da ninguna vuelta: la prueba no depende de la pared."""
    await asyncio.Event().wait()


async def _delegate(stack_ref: list[ExportStack], event: OutboxEvent, tx: Transaction) -> None:
    worker = stack_ref[0].worker
    if worker is None:
        raise RuntimeError("la prueba no fijó el worker")
    await worker(event, tx)


@contextlib.contextmanager
def export_stack(
    endpoint: PostgresEndpoint,
    prefix: str,
    *,
    pool: int = 12,
    storage: Callable[[Any], ExportDownloadSigner] | None = None,
    audit: Callable[[Any], Any] | None = None,
) -> Iterator[ExportStack]:
    """``storage(clock)`` da el firmante de las descargas (TASK-345) con el reloj de la pila;
    ``audit(writer)`` envuelve la auditoría de las descargas (para inyectarle fallos)."""
    with authz_environment(endpoint, prefix) as authz:
        sessions = authz.sessions
        (now,) = authz.fetch("SELECT now() AS now")
        sessions.clock.set(to_millisecond(now["now"]))
        registry = RecordTypeRegistry()
        for definition in U02_RECORD_TYPES:
            registry.register(definition)
        catalog = OutboxCatalog()
        register_u02_event_types(catalog.event_types)
        register_accreditation_event_types(catalog.event_types)
        stack_ref: list[ExportStack] = []

        async def handler(event: OutboxEvent, tx: Transaction) -> None:
            await _delegate(stack_ref, event, tx)

        register_export_worker(catalog.consumers, handler)  # type: ignore[arg-type]

        async def synchronize() -> None:
            system = unit_context(uuid.uuid4(), ActorUnit.U02, kind=ActorKind.SYSTEM)
            async with sessions.database.transaction(system) as transaction:
                await catalog.synchronize(SqlOutboxCatalogStore(transaction), sessions.clock)

        authz.run(synchronize())
        database = app_database(
            sessions.migrated, worker_pool_size=pool, lock_timeout_ms=LOCK_TIMEOUT_MS
        )
        outbox = Outbox(catalog, sessions.clock)
        free_text = FreeTextPolicyRegistry()
        free_text.seal()
        registry.seal()
        writer = EscritorExpediente(
            database=database,
            registry=registry,
            free_text=free_text,
            evidence=EvidenceVerifier(_NoStorage(), sessions.clock),
            outbox=outbox,
            clock=sessions.clock,
        )
        exports = ExportRequests(
            database=database,
            authorizer=authz.authorizer,
            audit=sessions.audit,
            outbox=outbox,
            clock=sessions.clock,
        )
        downloads = (
            None
            if storage is None
            else ExportDownloads(
                database=database,
                authorizer=authz.authorizer,
                audit=sessions.audit if audit is None else audit(sessions.audit),
                storage=storage(sessions.clock),
                clock=sessions.clock,
            )
        )
        app = World(clock=sessions.clock).app(
            runtime={
                "sessions": authz.contexts,
                "authorizer": ContextAuthorizer(
                    audit=authz.audit,
                    provider_organization_id=authz.provider_organization_id,
                    provider_queries=LedgerProviderQueryLedger(writer),
                    clock=sessions.clock,
                ),
                "state": {
                    ACCREDITATION_STATE_KEY: AccreditationHttp(exports=exports, downloads=downloads)
                },
            },
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60.0
        )
        stack = ExportStack(authz, database, catalog, outbox, exports, app, client)
        stack_ref.append(stack)
        try:
            yield stack
        finally:
            authz.run(client.aclose())
            for extra in stack.extra_databases:
                authz.run(extra.dispose())
            authz.run(database.dispose())


class _NoStorage:
    """Ningún camino de estas pruebas lee el almacén: cualquier llamada falla."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"la exportación no toca el almacén ({name})")
