"""Bancos de U-04: datos sintéticos y tramos medidos, nunca presupuestos inventados.

El perfil ci conserva el año y las 20 zonas del panel, pero siembra dos episodios
semanales en tres zonas; nightly usa 77 diarios por zona (RNF-DES-03/05).
La preparación no forma parte de la medida. No se optimiza código de producción.
"""

from __future__ import annotations

import contextlib
import dataclasses
import inspect
import json
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any

from hypothesis import settings
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.benchmarks.conftest import Measure, Result, median, percentile
from tests.integration.conftest import (
    POSTGRES_DATABASE,
    POSTGRES_IMAGE,
    POSTGRES_PASSWORD,
    POSTGRES_PORT,
    POSTGRES_USER,
    LocalStackEndpoint,
    PostgresEndpoint,
    _start,
    versioned_bucket,
)
from tests.laz_support import LoopOrganization, LoopWorld
from tests.loop_actions_support import ActionsStack, actions_stack
from tests.node_api_db import DbNode, issue
from tests.signing_support import bootstrapped_world
from vigia_platform.accreditation.adapters.postgres.export_reads import (
    SqlCheckpointLookup,
    SqlEpisodeHistory,
    SqlExportPlaces,
    StorageBlobs,
)
from vigia_platform.accreditation.export.contents import ExportComposer, ExportPorts
from vigia_platform.accreditation.export.stream import ExportStream
from vigia_platform.accreditation.exposure.service import ExposureService
from vigia_platform.accreditation.metrics.service import ProgramMetricsService
from vigia_platform.accreditation.monthly.package import MonthlyPackageComposer
from vigia_platform.accreditation.monthly.repository import MonthlyPackageRepository
from vigia_platform.catalog.adapters.postgres.query_ports import catalog_query_ports
from vigia_platform.fleet.application.inventory_read import FleetInventory
from vigia_platform.ledger.adapters.checkpoint_store import SqlCheckpointStore
from vigia_platform.ledger.application.coverage import CoverageService
from vigia_platform.ledger.application.evidence_read import EvidenceService
from vigia_platform.ledger.application.reader import LectorExpediente
from vigia_platform.ledger.chain.checkpoints import CheckpointService
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.signing import SigningService
from vigia_platform.shared.storage import S3Storage


class Traces:
    """Exportador aislado: solo duraciones y nombres cerrados, sin texto ni usuarios.

    Cada invocación crea una traza raíz y tramos hijos por fase. Los tiempos son
    inclusivos: no se suman tramos anidados para fingir una composición aditiva.
    """

    def __init__(self) -> None:
        self.exporter = InMemorySpanExporter()
        self.provider = TracerProvider()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.tracer = self.provider.get_tracer("vigia.benchmarks.laz")

    def call(self, step: str, target: Callable[[], Any]) -> Any:
        with self.tracer.start_as_current_span(
            "benchmark.laz.step",
            attributes={"step": step},
            record_exception=False,
            set_status_on_exception=False,
        ):
            return target()

    def breakdown(self) -> dict[str, Any]:
        times: dict[str, list[float]] = defaultdict(list)
        for span in self.exporter.get_finished_spans():
            assert span.start_time is not None and span.end_time is not None
            artifact = (span.attributes or {}).get("artifact")
            name = str((span.attributes or {}).get("step", span.name))
            if artifact is not None:
                name = f"{name}.{artifact}"
            times[name].append((span.end_time - span.start_time) / 1_000_000)
        return {
            name: {
                "median_ms": median(values),
                "p95_ms": percentile(values, 0.95),
                "samples": len(values),
            }
            for name, values in sorted(times.items())
        }

    def close(self) -> None:
        self.provider.shutdown()

    @contextlib.contextmanager
    def instrument(self, owner: Any, attributes: tuple[str, ...]) -> Iterator[None]:
        """Envuelve puertos reales por instancia, sin cambiar sus resultados ni el código."""
        originals = {name: getattr(owner, name) for name in attributes}
        for name, value in originals.items():
            setattr(owner, name, TimedPort(value, self, name.removeprefix("_")))
        try:
            yield
        finally:
            for name, value in originals.items():
                setattr(owner, name, value)


class TimedPort:
    """Un tramo por llamada real al puerto; nunca registra argumentos ni respuestas."""

    def __init__(self, target: Any, trace: Traces, name: str) -> None:
        self.target, self.trace, self.name = target, trace, name

    def __getattr__(self, operation: str) -> Any:
        member = getattr(self.target, operation)
        if not callable(member):
            return member
        name = f"{self.name}.{operation}"
        if inspect.iscoroutinefunction(member):

            async def async_call(*args: Any, **kwargs: Any) -> Any:
                with self.trace.tracer.start_as_current_span(
                    "benchmark.laz.port",
                    attributes={"step": name},
                    record_exception=False,
                    set_status_on_exception=False,
                ):
                    return await member(*args, **kwargs)

            return async_call

        def call(*args: Any, **kwargs: Any) -> Any:
            return self.trace.call(name, lambda: member(*args, **kwargs))

        return call


def measured(
    measure: Measure,
    name: str,
    objective_ms: float,
    operation: Callable[[Traces], Any],
    *,
    setup: Callable[[], Any] | None = None,
    details: dict[str, Any] | None = None,
) -> Result:
    """100 rondas; cada banco comprueba el resultado funcional dentro de su operación."""
    traces = Traces()
    segments: dict[str, Any] = {}
    try:
        result = measure(
            name,
            f"{name} [objetivo propio]",
            lambda: traces.call("total", lambda: operation(traces)),
            setup=setup,
            objective_ms=objective_ms,
            details={
                "profile": settings.get_current_profile_name(),
                **(details or {}),
                "trace_segments": segments,
            },
        )
        return result
    finally:
        # ``measure`` puede fallar por regresión después de guardar Result. El
        # diccionario anidado compartido conserva el diagnóstico también en rojo.
        segments.update(traces.breakdown())
        traces.close()


@dataclasses.dataclass
class LazBench:
    actions: ActionsStack
    storage: S3Storage
    reader: LectorExpediente
    coverage: CoverageService
    catalog: Any
    fleet: FleetInventory
    checkpoints: CheckpointService
    signer: SigningService
    exposure: ExposureService
    metrics: ProgramMetricsService
    organization: LoopOrganization
    zones: tuple[uuid.UUID, ...]
    start: datetime
    end: datetime
    count: int

    @property
    def world(self) -> LoopWorld:
        return self.actions.world

    @property
    def context(self) -> ScopeContext:
        return self.world.event_context(self.organization)

    def run(self, awaitable: Any) -> Any:
        return self.actions.run(awaitable)

    def composer(self) -> ExportComposer:
        database = self.actions.database
        return ExportComposer(
            stream=ExportStream(store=self.storage),
            ports=ExportPorts(
                reader=self.reader,
                catalog=self.catalog.catalog,
                gates=self.catalog.gates,
                fleet=self.fleet,
                coverage=self.coverage,
                markers=EvidenceService(
                    database=database, audit=self.world.stack.authz.audit, storage=self.storage
                ),
                checkpoints=SqlCheckpointLookup(database, self.checkpoints),
                places=SqlExportPlaces(database),
                blobs=StorageBlobs(database, self.storage),
                episodes=SqlEpisodeHistory(database),
                signer=self.signer,
            ),
        )

    def monthly(self) -> MonthlyPackageComposer:
        return MonthlyPackageComposer(
            database=self.actions.database,
            reader=self.reader,
            repository=MonthlyPackageRepository(database=self.actions.database),
            evidence=self.storage,
            checkpoints=self.checkpoints,
            writer=self.actions.writer,
            clock=self.world.stack.authz.sessions.clock,
            signer=self.signer,
            program_metrics=self.metrics,
        )

    def dimensions(self) -> dict[str, Any]:
        return {
            "profile": settings.get_current_profile_name(),
            "zones": len(self.zones),
            "days": 366,
            "findings": self.count,
            "episodes_per_zone_day": 77
            if settings.get_current_profile_name() == "nightly"
            else "2 por semana en 3 zonas (ci acotado)",
        }


def seed_year(
    actions: ActionsStack,
) -> tuple[LoopOrganization, tuple[uuid.UUID, ...], datetime, datetime, int]:
    """Un año por el escritor y el pliegue reales; ninguna copia de filas sustituye al expediente.

    Los sitios comparten organización/planta. El reloj se fija antes de emitir sus
    certificados y los renueva durante el periodo de 366 días del banco.
    """
    world = actions.world
    clock = world.stack.authz.sessions.clock
    start = actions.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    clock.set(start)
    organization = world.organization()
    sites = [organization]
    # La bandeja usa tres zonas; el panel conserva 20 aun en ci.
    for _ in range(19):
        zone_id = uuid.uuid4()
        actions.execute(
            "INSERT INTO identity.zone (zone_id, organization_id, plant_id, code, name,"
            " created_at, created_by) VALUES ($1,$2,$3,$4,'Zona sintética',$5,$6)",
            zone_id,
            organization.organization_id,
            organization.plant_id,
            f"ZN-{zone_id.hex[:12].upper()}",
            start - timedelta(days=400),
            world.stack.authz.operator_id,
        )
        site = dataclasses.replace(organization.site, zone_id=zone_id, standard_id=uuid.uuid4())
        actions.execute(
            "INSERT INTO identity.zone_node_assignment (assignment_id, organization_id,"
            " plant_id, zone_id, node_id, assigned_at, assigned_by) VALUES ($1,$2,$3,$4,$5,$6,$7)",
            uuid.uuid4(),
            organization.organization_id,
            organization.plant_id,
            zone_id,
            site.node_id,
            start - timedelta(days=400),
            world.stack.authz.operator_id,
        )
        world.stack.publish_catalog(site, start - timedelta(days=400))
        world.stack.usage(site, approved=True, at=start - timedelta(days=400))
        sites.append(LoopOrganization(site))
    for site in sites:
        actions.execute(
            "INSERT INTO catalog.declared_standard_version (organization_id, plant_id, zone_id,"
            " standard_id, version, family, title_es, declared_text, declared_by, effective_from,"
            " predicate, catalog_version, reason_es) VALUES ($1,$2,$3,$4,1,'coexistence',"
            " 'Zona de prensa','Perímetro protegido con resguardo fijo.',$5::jsonb,$6,'{}',1,"
            " 'Estándar sintético')",
            site.organization_id,
            site.plant_id,
            site.zone_id,
            site.site.standard_id,
            json.dumps(
                {
                    "user_id": str(world.stack.authz.operator_id),
                    "display_name": "Coordinación sintética",
                    "role": "administrator",
                }
            ),
            start - timedelta(days=400),
        )
    nightly = settings.get_current_profile_name() == "nightly"
    count = 0
    rotated = False
    for day in range(0, 366, 1 if nightly else 7):
        clock.set(start + timedelta(days=day, hours=12))
        actions.execute("UPDATE ledger.bench_clock SET at=$1", actions.now())
        if day >= 350 and not rotated:
            # Certificado nuevo, sin adulterar la vigencia de uno ya emitido.
            node = DbNode(
                organization.site.node_id,
                organization.organization_id,
                organization.plant_id,
                organization.zone_id,
                world.stack.authz.operator_id,
            )
            certificate, _ = actions.run(
                issue(world.stack.authz.sessions.admin, world.stack.authority, node, actions.now())
            )
            sites = [
                LoopOrganization(dataclasses.replace(s.site, certificate=certificate))
                for s in sites
            ]
            rotated = True
        for site in sites if nightly else sites[:3]:
            for _ in range(77 if nightly else 2):
                subject = world.ingest_finding(site)
                assert world.project(subject).applied
                count += 1
    clock.set(start + timedelta(days=367))
    actions.execute("UPDATE ledger.bench_clock SET at=$1", actions.now())
    return (
        sites[0],
        tuple(site.zone_id for site in sites),
        start,
        start + timedelta(days=366),
        count,
    )


@contextlib.contextmanager
def laz_postgres() -> Iterator[PostgresEndpoint]:
    """Clúster propio: migrar otra pila rota los roles globales de PostgreSQL.

    Las pilas de detalle, asistente y correo no deben invalidar las conexiones
    nuevas de este mundo mientras sus pools siguen vivos (no son roles por base).
    """
    from testcontainers.community.postgres import PostgresContainer

    def build() -> PostgresContainer:
        return (
            PostgresContainer(
                image=POSTGRES_IMAGE,
                username=POSTGRES_USER,
                password=POSTGRES_PASSWORD,
                dbname=POSTGRES_DATABASE,
                driver=None,
            )
            .with_env("TZ", "UTC")
            .with_env("PGTZ", "UTC")
        )

    # También con VIGIA_TEST_USE_COMPOSE=1 se necesita un clúster distinto:
    # una segunda base del mismo servidor no aísla sus roles globales.
    container = _start(build)
    try:
        yield PostgresEndpoint(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(POSTGRES_PORT)),
            user=POSTGRES_USER,
            password=POSTGRES_PASSWORD,
            database=POSTGRES_DATABASE,
        )
    finally:
        container.stop()


@contextlib.contextmanager
def laz(
    laz_postgres: PostgresEndpoint, localstack_endpoint: LocalStackEndpoint
) -> Iterator[LazBench]:
    """PostgreSQL 16, S3 versionado y un único reloj inyectado para toda la composición."""
    with actions_stack(laz_postgres, "bench_laz") as actions:
        world = actions.world
        sessions = world.stack.authz.sessions
        database = actions.database
        # Base desechable: recepción y Clock coinciden también en los datos históricos.
        original = actions.fetch(
            "SELECT pg_get_functiondef('ledger.vigia_chain_link()'::regprocedure)"
        )[0][0]
        actions.execute("CREATE TABLE ledger.bench_clock (at timestamptz NOT NULL)")
        actions.execute("INSERT INTO ledger.bench_clock VALUES ($1)", actions.now())
        actions.execute(
            "CREATE FUNCTION ledger.bench_now() RETURNS timestamptz LANGUAGE sql"
            " STABLE SECURITY DEFINER SET search_path=pg_catalog"
            " AS 'SELECT at FROM ledger.bench_clock'"
        )
        actions.execute(original.replace("clock_timestamp()", "ledger.bench_now()"))
        actions.execute(
            "ALTER TABLE ledger.ledger_record ALTER COLUMN received_at"
            " SET DEFAULT date_trunc('milliseconds', ledger.bench_now())"
        )
        actions.execute(
            "ALTER TABLE shared.audit_entry ALTER COLUMN occurred_at"
            " SET DEFAULT date_trunc('milliseconds', ledger.bench_now())"
        )
        org, zones, start, end, count = seed_year(actions)
        signing = actions.run(bootstrapped_world(start=actions.now()))
        signing.service._clock = sessions.clock
        reader = LectorExpediente(database=database, audit=sessions.audit, clock=sessions.clock)
        coverage = CoverageService(database=database, audit=sessions.audit, clock=sessions.clock)
        catalog = catalog_query_ports(database)
        config = PostgresLoopConfigurationRepository(database)
        exposure = ExposureService(
            database=database,
            authorizer=world.stack.authz.authorizer,
            catalog=catalog.catalog,
            gates=catalog.gates,
            coverage=coverage,
            settings=config,
            clock=sessions.clock,
        )
        metrics = ProgramMetricsService(
            database=database,
            authorizer=world.stack.authz.authorizer,
            exposure=exposure,
            clock=sessions.clock,
        )
        checkpoints = CheckpointService(
            store=SqlCheckpointStore(
                database=database,
                writer=actions.writer,
                audit=sessions.audit,
                outbox=world.stack.outbox,
            ),
            signer=signing.service,
            clock=sessions.clock,
            writer_context=world.stack.authz.contexts.checkpoint_writer,
        )
        with versioned_bucket(localstack_endpoint.aws_client("s3"), "vigia-bench-laz") as bucket:
            storage = S3Storage(
                dataclasses.replace(
                    localstack_endpoint.storage_settings(bucket),
                    connect_timeout_seconds=60,
                    read_timeout_seconds=120,
                ),
                sessions.clock,
            )
            fleet = FleetInventory(
                database=database,
                authorizer=world.stack.authz.authorizer,
                audit=sessions.audit,
                clock=sessions.clock,
                provider_organization_id=world.stack.authz.provider_organization_id,
            )
            yield LazBench(
                actions,
                storage,
                reader,
                coverage,
                catalog,
                fleet,
                checkpoints,
                signing.service,
                exposure,
                metrics,
                org,
                zones,
                start,
                end,
                count,
            )
