"""Lo que U-03 añade al arnés de resiliencia (LC-GOB-22 sobre LC-NUC-34; PAT-GOB-RES-06; TASK-232).

No es un arnés nuevo: los escenarios ``FS-GOB-01`` a ``FS-GOB-10`` usan ``harness.scenario``
(semilla e informe JSON), ``harness.Container`` (pausa, parada y arranque de los contenedores
propios del escenario), ``processes`` (procesos de verdad y su balanceador) y
``api_process``/``worker_process``. Este módulo añade:

- **la plataforma con todas las unidades** sobre los contenedores propios del escenario
  (``gob_stack``): ``tests.gob_platform_support.gob_platform`` (``create_app`` con las rutas de
  personas y las diez del contrato, PostgreSQL 16 como ``vigia_app`` y S3 de LocalStack) sobre
  ``harness.dedicated_postgres`` y ``harness.dedicated_localstack``, con métricas en memoria;
- la **autoridad de nodos efímera**: la clave P-256 de ``vigia-node-ca`` es ``MemoryKms`` y su raíz
  se publica en ``ca/root.pem`` del ``vigia-edge`` del escenario (``GobPlatform.kms``,
  ``GobPlatform.root``); su ``hang`` la bloquea (FS-GOB-05);
- **dobles bloqueables**: ``BlockableSigning`` (``SigningService`` cuyo ``sign`` no responde,
  como un punto de KMS de firma bloqueado, FS-GOB-02), ``BlockablePublisher``
  (``TrustStorePublisherPort`` con el destino bloqueado, FS-GOB-09) y ``PausingDatabase`` (la base
  de la aplicación que, armada, pausa el contenedor **a mitad de la operación**: dentro de la
  transacción ya abierta o antes de la lectura, FS-GOB-03);
- **U-03 cargado en los procesos del arnés** (``add_u03`` y ``process_units``): los tipos de
  registro de todas las unidades, los eventos y las siete tareas de U-03 y el estado de sus rutas,
  para ``api_process``, ``worker_process`` y el catálogo que sincroniza la prueba
  (``processes.resilience_catalog``): un evento o una tarea persistidos sin registrar impiden
  arrancar, así que todos registran lo mismo.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import contextlib
import statistics
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader

from tests.dispatch_support import metrics_with_reader
from tests.gob_platform_support import (
    APPROVAL_MARGIN_SECONDS,
    LONG_SECONDS,
    GobPlatform,
    GobZone,
    Onboarding,
    gob_platform,
    ok,
)
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint
from tests.resilience.harness import (
    WALL,
    Container,
    dedicated_localstack,
    dedicated_postgres,
    free_port,
)
from vigia_platform.fleet.domain.revocation_list import (
    PublishedRevocationList,
    PublishStep,
    RevocationListPublishFailed,
    SignedRevocationList,
)
from vigia_platform.fleet.registration import U03_TASKS
from vigia_platform.identity.adapters.authz_store import PostgresAuthorizationAudit
from vigia_platform.identity.authz.authorize import Authorizer
from vigia_platform.identity.authz.context import ScopeContexts
from vigia_platform.ledger.adapters.checkpoint_store import SqlCheckpointStore
from vigia_platform.ledger.adapters.record_type_store import SqlRecordTypeStore
from vigia_platform.ledger.application.audit_writer import AuditWriter
from vigia_platform.ledger.application.writer import EscritorExpediente
from vigia_platform.ledger.chain.checkpoints import CheckpointService
from vigia_platform.ledger.evidence import EvidenceVerifier
from vigia_platform.ledger.registry import RecordTypeRegistry
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext
from vigia_platform.shared.db import Database, Transaction
from vigia_platform.shared.observability.metrics import MetricName, PlatformMetrics, get_metrics
from vigia_platform.shared.outbox.publish import Outbox
from vigia_platform.shared.outbox.registries import OutboxCatalog
from vigia_platform.shared.runtime.units import (
    PlatformUnit,
    UnitServices,
    api_state,
    free_text_registry,
    record_type_registry,
    registered_units,
)
from vigia_platform.shared.signing.service import SigningKeyUnavailable

__all__ = [
    "PRODUCTION_API_DATABASE",
    "U03_UNIT_NAMES",
    "BlockablePublisher",
    "BlockableSigning",
    "GobStack",
    "PausingDatabase",
    "ProcessUnits",
    "RestartablePlatform",
    "TaskWrapper",
    "add_u03",
    "gob_stack",
    "histogram_values",
    "metric_sum",
    "p95",
    "process_units",
    "productive_zones",
    "restartable_platform",
    "u03_units",
]

U03_UNIT_NAMES: Final = frozenset({"catalog", "fleet", "node_api"})
"""Las unidades de U-03 en ``REGISTERED_UNITS`` (``shared.runtime.units``)."""

PRODUCTION_API_DATABASE: Final[Mapping[str, Any]] = {
    "statement_timeout_ms": 10_000,
    "lock_timeout_ms": 2_000,
    "connect_timeout_seconds": 5.0,
    "pool_timeout_seconds": 5.0,
}
"""Los topes de ``vigia-api`` (NFR-NUC-36, A-21) para los escenarios que tratan de ellos: el
``statement_timeout`` de 10 s y el ``lock_timeout`` de 2 s de la ruta del nodo."""

BLOCK_CAP_SECONDS: Final = 600.0
"""Lo más que un doble bloqueado retiene un hilo si la prueba no lo libera (nunca decide nada)."""


# --- U-03 en los procesos del arnés ------------------------------------------------------------


def u03_units() -> tuple[PlatformUnit, ...]:
    return tuple(unit for unit in registered_units() if unit.name in U03_UNIT_NAMES)


def _never_runs(name: str) -> Callable[[Any], Any]:
    async def handler(_: Any) -> None:
        raise AssertionError(f"la tarea {name} solo se sincroniza en este catálogo")

    return handler


TaskWrapper = Callable[[str, Any], Any]
"""``(nombre, manejador) -> manejador``: envuelve el manejador de una tarea al registrarla."""


class _WrappingTasks:
    """``PeriodicTaskRegistry`` que pasa cada manejador por ``wrap`` antes de registrarlo."""

    def __init__(self, target: Any, wrap: TaskWrapper) -> None:
        self._target = target
        self._wrap = wrap

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)

    def register(self, task_name: str, schedule: Any, handler: Any, **options: Any) -> Any:
        return self._target.register(task_name, schedule, self._wrap(task_name, handler), **options)


def add_u03(
    catalog: OutboxCatalog,
    services: UnitServices | None = None,
    *,
    wrap: TaskWrapper | None = None,
) -> OutboxCatalog:
    """Los eventos y las siete tareas de U-03 en ``catalog`` (sin sellar).

    Con ``services``, las tareas llevan sus manejadores reales (``fleet_tasks`` y
    ``catalog_tasks``, como ``vigia-worker``), pasados por ``wrap`` si se da (el gancho de prueba
    del worker); sin ellos, los mismos nombres, cadencias, unidad e iteración con un manejador
    que no se ejecuta nunca: lo que sincroniza un proceso que no corre tareas (la API o la propia
    prueba).
    """
    units = u03_units()
    for unit in units:
        unit.event_types(catalog.event_types)
    if services is not None:
        tasks: Any = catalog.periodic_tasks
        if wrap is not None:
            tasks = _WrappingTasks(tasks, wrap)
        for unit in units:
            unit.consumers(catalog.consumers, services)
            unit.periodic_tasks(tasks, services)
        return catalog
    for name, (schedule, iteration) in sorted(U03_TASKS.items()):
        catalog.periodic_tasks.register(
            name, schedule, _never_runs(name), unit=ActorUnit.U03, iteration=iteration
        )
    return catalog


@dataclass(frozen=True)
class ProcessUnits:
    """Los servicios de las unidades en un proceso del arnés y sus registros."""

    services: UnitServices
    record_types: RecordTypeRegistry

    def state(self) -> dict[str, object]:
        """El estado de las rutas de U-03 (catálogo, flota y ``NodeApiGate``) para ``app.state``."""
        return api_state(u03_units(), self.services)

    async def synchronize_record_types(self, context: ScopeContext) -> None:
        """Los tipos de todas las unidades en ``ledger.record_type`` (solo añade lo que falta)."""
        database = self.services.database
        async with database.transaction(context) as transaction:
            await self.record_types.synchronize(SqlRecordTypeStore(transaction))


def process_units(
    *,
    database: Database,
    clock: Clock,
    provider_organization_id: uuid.UUID,
    contexts: ScopeContexts,
    outbox: Outbox,
    signing: Any,
    kms: Any,
    storage: Any,
    metrics: PlatformMetrics | None = None,
) -> ProcessUnits:
    """``UnitServices`` de un proceso del arnés sobre su base, como la raíz de composición
    (``runtime.core.build_core``), con los dobles de firma, KMS y almacén del proceso."""
    record_types = record_type_registry(registered_units())
    free_text = free_text_registry(registered_units())
    audit = AuditWriter(
        database=database, clock=clock, provider_organization_id=provider_organization_id
    )
    writer = EscritorExpediente(
        database=database,
        registry=record_types,
        free_text=free_text,
        evidence=EvidenceVerifier(storage, clock),
        outbox=outbox,
        clock=clock,
    )
    authorizer = Authorizer(
        audit=PostgresAuthorizationAudit(
            database=database, audit=audit, outbox=outbox, clock=clock
        ),
        provider_organization_id=provider_organization_id,
    )
    services = UnitServices(
        clock=clock,
        metrics=metrics if metrics is not None else get_metrics(),
        provider_organization_id=provider_organization_id,
        database=database,
        contexts=contexts,
        authorizer=authorizer,
        audit=audit,
        outbox=outbox,
        writer=writer,
        free_text=free_text,
        signing=signing,
        checkpoints=CheckpointService(
            store=SqlCheckpointStore(database=database, writer=writer, audit=audit, outbox=outbox),
            signer=signing,
            clock=clock,
        ),
        kms=kms,
        evidence=storage,
    )
    return ProcessUnits(services=services, record_types=record_types)


# --- Dobles bloqueables ------------------------------------------------------------------------


class BlockableSigning:
    """``SigningService`` cuyo ``sign`` deja de responder mientras está bloqueado.

    Es el punto de KMS de la firma de sobres bloqueado: la llamada se queda esperando (la
    publicación y las compuertas la hacen en un hilo con su tope de 5 s, NFR-GOB-43) hasta que la
    prueba lo libera; entonces termina y su resultado ya no lo espera nadie. Todo lo demás va al
    servicio real.
    """

    def __init__(self, target: Any) -> None:
        self.target = target
        self._open = threading.Event()
        self._open.set()
        self.calls = 0
        self.blocked_calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.target, name)

    @property
    def blocked(self) -> bool:
        return not self._open.is_set()

    def block(self) -> None:
        self._open.clear()

    def release(self) -> None:
        self._open.set()

    def sign(self, purpose: Any, payload: Any) -> Any:
        self.calls += 1
        if self.blocked:
            self.blocked_calls += 1
            if not self._open.wait(BLOCK_CAP_SECONDS):
                raise SigningKeyUnavailable(purpose)
        return self.target.sign(purpose, payload)


@dataclass
class BlockablePublisher:
    """``TrustStorePublisherPort`` del almacén de confianza del balanceador de nodos.

    Bloqueado, el destino rechaza la escritura de la lista (``crl_put_object``), como el adaptador
    real cuando su paso falla o vence su tope; libre, guarda lo publicado.
    """

    blocked: bool = False
    attempts: int = 0
    published: list[SignedRevocationList] = field(default_factory=list)

    async def publish(self, revocation_list: SignedRevocationList) -> PublishedRevocationList:
        self.attempts += 1
        if self.blocked:
            raise RevocationListPublishFailed(PublishStep.PUT_OBJECT)
        self.published.append(revocation_list)
        return PublishedRevocationList(
            object_version_id=f"version-{len(self.published)}",
            revocation_id=len(self.published),
        )


class PausingDatabase:
    """La base de la aplicación que, armada, ejecuta la inyección a mitad de la operación.

    ``arm(action)``: la siguiente transacción la ejecuta **después de abrirse** (con BEGIN y el
    contexto ya fijados, antes de su primera sentencia) y la siguiente lectura, antes de enviarse;
    lo que llegue primero. Todo lo demás va a la ``Database`` real.
    """

    def __init__(self, target: Database) -> None:
        self.target = target
        self._action: Callable[[], Any] | None = None
        self.fired = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.target, name)

    def arm(self, action: Callable[[], Any]) -> None:
        self._action = action

    async def _fire(self) -> None:
        action, self._action = self._action, None
        if action is not None:
            self.fired += 1
            await asyncio.to_thread(action)

    def transaction(self, context: ScopeContext) -> contextlib.AbstractAsyncContextManager[Any]:
        return self._transaction(context)

    @contextlib.asynccontextmanager
    async def _transaction(self, context: ScopeContext) -> AsyncIterator[Transaction]:
        async with self.target.transaction(context) as transaction:
            await self._fire()
            yield transaction

    async def read(self, *arguments: Any, **options: Any) -> Any:
        await self._fire()
        return await self.target.read(*arguments, **options)


# --- Zonas de una misma planta -----------------------------------------------------------------


def productive_zones(flow: Onboarding, count: int) -> list[GobZone]:
    """``count`` zonas productivas de **una misma planta** (una sola cadena), cada una con su nodo
    dado de alta: la primera de punta a punta (``Onboarding.productive_zone``) y las demás con su
    montaje, su acta cerrada y su acuerdo de uso con los firmantes de la planta (la política de la
    planta y la de firmantes ya existen)."""
    first = flow.productive_zone()
    zones = [first]
    for _ in range(count - 1):
        zone = flow.zone(within=first)
        flow.mount(zone)
        flow.closed_record(zone)
        created = ok(flow.agreement(zone, first.signatories), 201)
        zone.agreement_id = created["agreement_id"]
        for signatory in first.signatories:
            assert flow.confirm(zone.agreement_id, signatory).status_code == 201
        ok(flow.approve(zone, zone.agreement_id))
        # Los hechos por defecto (5 minutos antes de la hora) caen después de la aprobación.
        flow.gob.advance(APPROVAL_MARGIN_SECONDS)
        zones.append(zone)
    return zones


# --- La plataforma con todas las unidades sobre contenedores propios --------------------------


@dataclass
class GobStack:
    """La aplicación completa de U-03 sobre el PostgreSQL y el LocalStack propios del escenario."""

    gob: GobPlatform
    flow: Onboarding
    postgres: Container
    localstack: Container
    postgres_endpoint: PostgresEndpoint
    localstack_endpoint: LocalStackEndpoint
    signing: BlockableSigning
    database: PausingDatabase
    reader: InMemoryMetricReader

    def restore(self) -> None:
        """Los dos contenedores en marcha (al salir, también si la prueba falla)."""
        self.signing.release()
        for container in (self.postgres, self.localstack):
            status = container.status()
            if status == "paused":
                container.unpause()
            elif status != "running":
                container.start()
            container.wait_ready()

    def recreate_buckets(self) -> None:
        """Tras arrancar de nuevo LocalStack (sin persistencia), los depósitos del escenario
        vuelven a existir con versionado; ``ca/root.pem`` se vuelve a publicar en ``vigia-edge``.
        S3 de verdad conserva sus objetos: lo que se pierde aquí es del contenedor de prueba."""
        s3 = self.gob.s3
        existing = {bucket["Name"] for bucket in s3.list_buckets().get("Buckets", [])}
        for name in (self.gob.evidence_bucket, self.gob.edge_bucket):
            if name not in existing:
                s3.create_bucket(Bucket=name)
                s3.put_bucket_versioning(Bucket=name, VersioningConfiguration={"Status": "Enabled"})
        from cryptography.hazmat.primitives.serialization import Encoding

        s3.put_object(
            Bucket=self.gob.edge_bucket,
            Key="ca/root.pem",
            Body=self.gob.root.public_bytes(Encoding.PEM),
        )


@contextlib.contextmanager
def gob_stack(
    prefix: str,
    *,
    database_changes: Mapping[str, Any] | None = None,
    node_ca_deadline_seconds: float = LONG_SECONDS,
    health: bool = False,
) -> Iterator[GobStack]:
    """``gob_platform`` sobre ``dedicated_postgres`` y ``dedicated_localstack`` con los dobles
    bloqueables y métricas en memoria. Al salir, los contenedores vuelven a estar en marcha antes
    de borrar la base y los depósitos, y después se eliminan."""
    metrics, reader = metrics_with_reader()
    holders: dict[str, Any] = {}

    def signing(target: Any) -> BlockableSigning:
        holders["signing"] = BlockableSigning(target)
        return holders["signing"]  # type: ignore[no-any-return]

    def database(target: Database) -> PausingDatabase:
        holders["database"] = PausingDatabase(target)
        return holders["database"]  # type: ignore[no-any-return]

    with (
        dedicated_postgres() as (postgres_endpoint, postgres),
        dedicated_localstack() as (localstack_endpoint, localstack),
        gob_platform(
            postgres_endpoint,
            localstack_endpoint,
            prefix,
            database_changes=database_changes,
            wrap_database=database,
            wrap_signing=signing,
            node_ca_deadline_seconds=node_ca_deadline_seconds,
            metrics=metrics,
            health=health,
        ) as gob,
    ):
        stack = GobStack(
            gob=gob,
            flow=Onboarding(gob),
            postgres=postgres,
            localstack=localstack,
            postgres_endpoint=postgres_endpoint,
            localstack_endpoint=localstack_endpoint,
            signing=holders["signing"],
            database=holders["database"],
            reader=reader,
        )
        try:
            yield stack
        finally:
            stack.restore()
            stack.recreate_buckets()


# --- Plataforma de producción con dos procesos reiniciables ------------------------------------


API_PROCESSES: Final = ("api-a", "api-b")
"""Los dos procesos ``vigia-api`` de la tarea (LC-GOB-20)."""
RESTART_GRACE_SECONDS: Final = 1.0
"""Tras retirar un proceso del balanceador y antes de pararlo (lo que tarda el desregistro)."""


@dataclass
class RestartablePlatform:
    """La plataforma de producción de los perfiles de carga (TASK-230 y 231) con sus **dos
    procesos ``vigia-api`` de verdad** (la orden de la imagen) tras un balanceador que solo enruta
    a los que responden ``/health/ready`` (``processes.Balancer``) y, delante, el balanceador
    ``nodes.`` con mTLS. ``LoadApi`` (en el proceso de la prueba) solo da de alta la flota por
    ``app.``: los nodos nunca lo ven, así que los dos que atienden se pueden reiniciar."""

    stack: Any
    api: Any
    group: Any
    ports: dict[str, int]
    router: Any
    app: Any
    nodes: Any
    tls: Any
    directory: Any
    environ: Mapping[str, str]
    restarts: list[dict[str, Any]] = field(default_factory=list)

    def provision(self, profile: Any) -> Any:
        from tests.load.provision import FleetProvisioner

        provisioner = FleetProvisioner(
            self.stack,
            self.api,
            self.app.url,
            self.tls.ca_file,
            self.directory,
            nodes_url=self.nodes.url,
        )
        return provisioner.fleet(profile)

    def start(self, name: str) -> None:
        from tests.conformance.conftest import API_MODULE
        from tests.conformance.platform_target import api_process_environment, wait_ready
        from tests.load.provision import LOCAL_BALANCER
        from vigia_platform.shared.api.main import FORWARDED_VARIABLE

        port = self.ports[name]
        environ = {
            **api_process_environment(self.stack, port),
            FORWARDED_VARIABLE: LOCAL_BALANCER[0],
        }
        spawned = self.group.start(name, API_MODULE, environ)
        wait_ready(f"http://127.0.0.1:{port}", alive=lambda: spawned.process.poll() is None)

    def restart(self, name: str) -> dict[str, Any]:
        """Reinicio de un proceso como en un despliegue: se retira del balanceador, se para con
        ``SIGTERM`` (parada ordenada), arranca de nuevo y vuelve cuando está listo. Su cubo de
        fichas en memoria vuelve **frío** (R10)."""
        started = WALL.monotonic()
        self.router.drain(name)
        time.sleep(RESTART_GRACE_SECONDS)
        code = self.group.stop(name)
        stopped = WALL.monotonic()
        self.start(name)
        self.router.undrain(name)
        record = {
            "process": name,
            "exit_code": code,
            "stopped_after_seconds": round(stopped - started, 2),
            "ready_after_seconds": round(WALL.monotonic() - started, 2),
        }
        self.restarts.append(record)
        return record


@contextlib.contextmanager
def restartable_platform(
    postgres_endpoint: PostgresEndpoint, localstack_endpoint: LocalStackEndpoint, directory: Any
) -> Iterator[RestartablePlatform]:
    """``RestartablePlatform`` sobre los contenedores dados (``directory`` fuera del árbol)."""
    # Se importan al construirla: arrastran el conjunto sintético de los perfiles de carga, que
    # los procesos del arnés que importan este módulo no necesitan.
    from tests.conformance.mtls_proxy import server_tls
    from tests.conformance.platform_target import platform_stack
    from tests.load.balancer import balancer_process
    from tests.load.provision import load_api
    from tests.resilience.processes import balancer, process_group

    tls = server_tls(directory, WALL.now())
    with (
        balancer_process(
            "aws", [localstack_endpoint.url], tls, directory, preserve_host=True
        ) as aws,
        platform_stack(
            postgres_endpoint,
            localstack_endpoint,
            directory,
            aws_url=aws.url,
            ca_bundle=tls.ca_file,
        ) as stack,
        load_api(stack.environ, free_port(), log_file=directory / "api-alta.log") as api,
        process_group(directory) as group,
    ):
        ports = {name: free_port() for name in API_PROCESSES}
        platform = RestartablePlatform(
            stack=stack,
            api=api,
            group=group,
            ports=ports,
            router=None,
            app=None,
            nodes=None,
            tls=tls,
            directory=directory,
            environ=stack.environ,
        )
        for name in API_PROCESSES:
            platform.start(name)
        node_ca = directory / "vigia-node-ca.crt"
        node_ca.write_bytes(stack.node_ca_root())
        with (
            balancer(ports) as router,
            balancer_process("app", [api.url], tls, directory) as app,
            balancer_process("nodes", [router.url], tls, directory, client_ca=node_ca) as nodes,
        ):
            platform.router, platform.app, platform.nodes = router, app, nodes
            yield platform


# --- Lecturas de métricas y latencias -----------------------------------------------------------


def metric_sum(reader: InMemoryMetricReader, name: MetricName, **attributes: Any) -> float:
    """La suma de un contador (o la cuenta de un histograma) con esos atributos."""
    data = reader.get_metrics_data()
    total = 0.0
    if data is None:
        return total
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name != name.value:
                    continue
                for point in metric.data.data_points:
                    labels = dict(point.attributes or {})
                    if any(labels.get(key) != value for key, value in attributes.items()):
                        continue
                    if isinstance(point, HistogramDataPoint):
                        total += point.count
                    else:
                        total += float(getattr(point, "value", 0.0))
    return total


def histogram_values(
    reader: InMemoryMetricReader, name: MetricName
) -> list[tuple[dict[str, Any], int]]:
    """``(atributos, observaciones)`` de cada punto de un histograma."""
    data = reader.get_metrics_data()
    values: list[tuple[dict[str, Any], int]] = []
    if data is None:
        return values
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == name.value:
                    values.extend(
                        (dict(point.attributes or {}), point.count)
                        for point in metric.data.data_points
                        if isinstance(point, HistogramDataPoint)
                    )
    return values


def p95(samples: Sequence[float]) -> float:
    """El percentil 95 (método inclusivo) de ``samples`` en milisegundos."""
    if len(samples) < 2:
        return max(samples, default=0.0)
    return statistics.quantiles(samples, n=20, method="inclusive")[18]
