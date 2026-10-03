"""Bancos del expediente de NFR-NUC-01 y NFR-NUC-04 (TASK-142, VIG-91).

Sobre PostgreSQL 16 y LocalStack en contenedores, como ``vigia_app`` (con la seguridad a nivel de
fila), con **un año** de una planta de tres zonas generado con ``tests/volumetry/scale_data.py``
(unos 154 registros por zona y día, ~170 000 registros, y la auditoría de sus lecturas), y la
**zona caliente** con el volumen máximo de eventos de observabilidad del diseño (646 al día, unos
20 000 en 31 días) en sus últimos 62 días (seguimiento de VIG-64):

==============================  =============================================  ==========
banco                           operación                                      objetivo
==============================  =============================================  ==========
``ledger_write``                ``EscritorExpediente.write`` sin evidencias    p95 150 ms
``ledger_write_2_evidences``    con 2 evidencias verificadas en S3 (HEAD)      p95 400 ms
``ledger_list_200_scoped``      ``LectorExpediente.list`` de 200, planta, año  p95 300 ms
``coverage_timeline_31d``       ``linea_de_tiempo`` de 31 días, zona caliente  p95 1 s
``coverage_state_at``           ``estado_en`` en la zona caliente              p95 50 ms
==============================  =============================================  ==========

Cada escritura pasa por los siete pasos del orden fijo (``order_probe``: organización, clave,
texto libre, evidencias y un evento en la bandeja). Las evidencias son objetos sintéticos de
64 KB subidos a LocalStack en la preparación de cada ronda, fuera de la medida: la escritura
verifica sus metadatos (tamaño, ``ChecksumSHA256`` completo y marca de anonimización). Solo datos
generados. Solo corre con ``--hypothesis-profile=nightly``.
"""

from __future__ import annotations

import base64
import hashlib
import os
import random
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest

from tests.benchmarks.conftest import Measure
from tests.identity_db import migrated_database
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint, versioned_bucket
from tests.integration.test_coverage_port import COVERAGE_TYPES, scoped
from tests.volumetry.scale_data import (
    OrganizationRef,
    ScaleWriter,
    create_partitions,
    days_back,
    historical_stamps,
    kit_findings,
    seed_organization,
)
from tests.writer_support import (
    ORDER_TYPE,
    Place,
    WriterEnvironment,
    order_document,
    unit_context,
    writer_environment,
)
from vigia_platform.ledger.application.coverage import CoverageService
from vigia_platform.ledger.application.reader import LectorExpediente, LedgerFilters, PageRequest
from vigia_platform.ledger.application.writer import EscritorExpediente, Receipt
from vigia_platform.ledger.domain.coverage import CoveragePeriod
from vigia_platform.ledger.evidence import EvidenceVerifier
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.shared.context import (
    ActorKind,
    ActorUnit,
    AllowedScope,
    Role,
    ScopeLevel,
)
from vigia_platform.shared.storage import ANONYMIZED_METADATA_KEY, S3Storage

pytestmark = [pytest.mark.nightly, pytest.mark.integration]

SEED = 20261002
YEAR_DAYS = 365
HOT_DAYS = 62
CLIP_BYTES = 64 * 1024


@dataclass
class LedgerBench:
    env: WriterEnvironment
    organization: OrganizationRef
    now: datetime
    first_day: datetime
    s3: Any
    bucket: str
    s3_writer: EscritorExpediente
    reader: LectorExpediente
    coverage: CoverageService
    written: dict[str, int] | None = None

    def run(self, awaitable: Any) -> Any:
        return self.env.loop.run(awaitable)

    @property
    def plant_id(self) -> uuid.UUID:
        return self.organization.plants[0].plant_id

    def place(self, index: int) -> Place:
        zone = self.organization.plants[0].zones[index]
        return Place(self.organization.organization_id, self.plant_id, zone.zone_id, zone.node_id)


async def _generate(bench: LedgerBench) -> None:
    connection = await bench.env.migrated.connect()
    try:
        writer = ScaleWriter(kit_findings(32, SEED), seed=SEED)
        plant = bench.organization.plants[0]
        async with historical_stamps(connection):
            await writer.bootstrap_plant(
                connection, bench.organization, plant, bench.first_day - timedelta(hours=1)
            )
            await writer.plant_history(
                connection,
                bench.organization,
                plant,
                bench.first_day,
                YEAR_DAYS,
                hot_zone=plant.zones[0].zone_id,
                hot_days=HOT_DAYS,
            )
            await writer.audit_history(
                connection, bench.organization, bench.first_day, YEAR_DAYS, per_day=300
            )
        await connection.execute("ANALYZE")
        bench.written = dict(writer.written)
    finally:
        await connection.close()


@pytest.fixture(scope="module")
def bench(
    postgres_endpoint: PostgresEndpoint, localstack_endpoint: LocalStackEndpoint
) -> Iterator[LedgerBench]:
    s3 = localstack_endpoint.aws_client("s3")
    with (
        migrated_database(postgres_endpoint, "bench_ledger") as migrated,
        writer_environment(migrated, extra_types=COVERAGE_TYPES) as env,
        versioned_bucket(s3, "vigia-bench-evidence") as bucket,
    ):

        async def prepare() -> tuple[OrganizationRef, datetime, datetime]:
            connection = await migrated.connect()
            try:
                now: datetime = await connection.fetchval(
                    "SELECT date_trunc('milliseconds', now())"
                )
                first_day = days_back(now, YEAR_DAYS)
                await create_partitions(connection, first_day, now)
                organization = await seed_organization(
                    connection,
                    random.Random(SEED),  # noqa: S311 - datos sintéticos deterministas
                    plants=1,
                    zones_per_plant=3,
                    since=first_day - timedelta(days=1),
                )
            finally:
                await connection.close()
            return organization, now, first_day

        organization, now, first_day = env.loop.run(prepare())
        storage = S3Storage(localstack_endpoint.storage_settings(bucket), env.clock)
        assert env.outbox is not None
        bench = LedgerBench(
            env=env,
            organization=organization,
            now=now,
            first_day=first_day,
            s3=s3,
            bucket=bucket,
            s3_writer=EscritorExpediente(
                database=env.database,
                registry=env.registry,
                free_text=FreeTextPolicyRegistry(),
                evidence=EvidenceVerifier(storage, env.clock),
                outbox=env.outbox,
                clock=env.clock,
            ),
            reader=LectorExpediente(database=env.database, audit=env.audit),
            coverage=CoverageService(database=env.database, audit=env.audit),
        )
        env.loop.run(_generate(bench))
        yield bench


def _node_context(bench: LedgerBench) -> Any:
    return unit_context(bench.organization.organization_id, ActorUnit.U03, kind=ActorKind.SYSTEM)


def test_ledger_write_without_evidence(bench: LedgerBench, measure: Measure) -> None:
    place = bench.place(1)
    context = _node_context(bench)
    documents: list[dict[str, Any]] = []

    def setup() -> None:
        documents.append(order_document(place, clips=0))

    def target() -> None:
        receipt = bench.run(bench.env.writer.write(context, ORDER_TYPE, documents[-1]))
        assert isinstance(receipt, Receipt), receipt

    measure(
        "ledger_write",
        "Escritura en el expediente sin evidencias",
        target,
        setup=setup,
        objective_ms=150,
        details={"history": bench.written},
    )


def test_ledger_write_with_two_verified_evidences(bench: LedgerBench, measure: Measure) -> None:
    place = bench.place(1)
    context = _node_context(bench)
    documents: list[dict[str, Any]] = []

    def setup() -> None:
        document = order_document(place, clips=2)
        for clip in document["clips"]:
            body = os.urandom(CLIP_BYTES)
            digest = hashlib.sha256(body)
            clip["sha256"] = digest.hexdigest()
            clip["size_bytes"] = len(body)
            bench.s3.put_object(
                Bucket=bench.bucket,
                Key=clip["storage_key"],
                Body=body,
                ContentType=clip["content_type"],
                ChecksumSHA256=base64.b64encode(digest.digest()).decode(),
                Metadata={ANONYMIZED_METADATA_KEY: "1"},
            )
        documents.append(document)

    def target() -> None:
        receipt = bench.run(bench.s3_writer.write(context, ORDER_TYPE, documents[-1]))
        assert isinstance(receipt, Receipt), receipt

    measure(
        "ledger_write_2_evidences",
        "Escritura con 2 evidencias verificadas por metadatos (S3 en LocalStack)",
        target,
        setup=setup,
        objective_ms=400,
        details={"clip_bytes": CLIP_BYTES},
    )


def test_ledger_list_200_scoped_over_a_year(bench: LedgerBench, measure: Measure) -> None:
    context = scoped(
        bench.organization.organization_id,
        [AllowedScope(ScopeLevel.PLANT, bench.plant_id, Role.PLANT_MANAGER)],
    )
    filters = LedgerFilters(received_from=bench.first_day, received_before=bench.now + timedelta(1))
    sizes: list[int] = []

    def target() -> None:
        page = bench.run(bench.reader.list(context, filters, PageRequest(size=200)))
        sizes.append(len(page.items))

    measure(
        "ledger_list_200_scoped",
        "Lista de 200 registros con alcance de planta (3 zonas) sobre 1 año",
        target,
        objective_ms=300,
        details={"days": YEAR_DAYS, "zones": 3},
    )
    assert set(sizes) == {200}


def test_coverage_timeline_31_days_at_maximum_volume(bench: LedgerBench, measure: Measure) -> None:
    hot = bench.organization.plants[0].zones[0].zone_id
    end = days_back(bench.now, 0)
    period = CoveragePeriod(end - timedelta(days=31), end)
    context = scoped(
        bench.organization.organization_id,
        [AllowedScope(ScopeLevel.PLANT, bench.plant_id, Role.PLANT_MANAGER)],
    )
    intervals: list[int] = []

    def target() -> None:
        timeline = bench.run(bench.coverage.linea_de_tiempo(context, hot, period))
        intervals.append(len(timeline.node_intervals))

    measure(
        "coverage_timeline_31d",
        "Línea de tiempo de 31 días con el volumen máximo de eventos por zona",
        target,
        objective_ms=1000,
        details={"events_per_day": 646, "history_days": YEAR_DAYS},
    )
    assert min(intervals) >= 9_000  # unos 10 000 tramos de nodo: el volumen se generó


def test_coverage_state_at(bench: LedgerBench, measure: Measure) -> None:
    hot = bench.organization.plants[0].zones[0].zone_id
    end = days_back(bench.now, 0)
    context = scoped(
        bench.organization.organization_id,
        [AllowedScope(ScopeLevel.PLANT, bench.plant_id, Role.PLANT_MANAGER)],
    )
    rng = random.Random(SEED)  # noqa: S311 - instantes de prueba
    instants: list[datetime] = []

    def setup() -> None:
        offset = rng.randrange(31 * 86_400_000)
        instants.append(end - timedelta(milliseconds=offset))

    def target() -> None:
        bench.run(bench.coverage.estado_en(context, hot, instants[-1]))

    measure(
        "coverage_state_at",
        "estado_en de la zona caliente en un instante de los últimos 31 días",
        target,
        setup=setup,
        objective_ms=50,
    )
