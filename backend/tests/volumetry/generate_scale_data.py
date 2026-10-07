"""Volumetría a escala objetivo y comprobación de NFR-NUC-01 sobre ella (NFR-NUC-05; TASK-142).

Uso (desde ``backend/``)::

    uv run python tests/volumetry/generate_scale_data.py --scale target
    uv run python tests/volumetry/generate_scale_data.py --scale smoke --out informe.json

Levanta PostgreSQL 16 en un contenedor (o usa el de ``docker compose`` con
``VIGIA_TEST_USE_COMPOSE=1``), migra una base desechable y genera, con
``tests/volumetry/scale_data.py`` y el disparador de encadenado real:

- **Escala objetivo** (``target``, NFR-NUC-05): 50 organizaciones, 100 plantas y 300 zonas; la
  organización mayor con 20 plantas y 60 zonas y **un año** de expediente (unos 154 registros por
  zona y día, NFR-NUC-03; una zona con el volumen máximo de eventos de observabilidad, 646 al día)
  y de auditoría (4 000 entradas al día, del orden de 1,5 millones al año); las otras 49, con 3
  zonas por planta, una semana. ``smoke`` es lo mismo en miniatura (lo usa la prueba de
  integración del script).
- **Métricas de NFR-NUC-03**, medidas sobre el año de la organización mayor:
  ``ledger_bytes_per_day`` (lo que crecen ``ledger.ledger_record``, sus particiones, índices y
  TOAST, ``record_identity`` y ``record_source_key`` por día), ``audit_entries_per_day`` (y sus
  bytes), por zona y extrapoladas a 300 zonas.
- **NFR-NUC-01 sobre un año de la organización mayor**, como ``vigia_app`` con la seguridad a nivel
  de fila: lista de 200 con alcance de planta (3 zonas) en la primera página y a mitad del año, y
  con alcance de organización; línea de tiempo de 31 días y ``estado_en`` en la zona caliente y en
  una normal; escritura sin evidencias; verificación incremental de una cadena de planta de un año
  desde la génesis. Mediana, p95 y el objetivo de cada una, con ``cumple`` o ``NO cumple``: el
  informe declara la medición, no la oculta.

**U-03** (TASK-233; NFR-GOB-11 y 16, sección ``u03`` del informe; ``--part gob`` solo ella, ``all``
las dos una tras otra: la base de U-02 se borra antes, así que el disco no se suma). Sobre
``GobPlatform`` (la aplicación completa de U-03, con PostgreSQL y LocalStack):

- **núcleo por las rutas reales** en la organización mayor: el acta de la matriz máxima (32
  estándares, 8 cámaras) cerrada por ``POST /walk-tests/{id}/close``, un walk-test abierto y una
  zona normal cuyo latido es la plantilla del inventario generado;
- **historia en masa** (``scale_data``, ``generate_series`` en el servidor): 19 plantas más con 3
  zonas (60 zonas), cada una con su nodo dado de alta, entre 1 y 32 estándares y hasta 8 cámaras,
  24 meses de catálogo (una versión por semana), compuertas, acuerdos, intentos de alta y alarmas,
  un año de expediente con las transiciones de comunicación de cada nodo; 100 nodos activos con
  90 días de latidos (uno por minuto: unos 13 millones de filas) y 30 días de concesiones de
  subida (77 por zona y día); las otras 49 organizaciones con su identidad;
- **mediciones**: las rutas de NFR-GOB-03 por HTTP como la administración de la organización
  mayor, los puertos de NFR-GOB-04 sobre los 24 meses (con el suelo de una lectura vacía),
  ``gate_history`` de 366 días desde el primer mes (intervalos completos y contiguos, o el script
  falla) y las métricas de presupuesto de NFR-GOB-07 (``fleet_heartbeat_rows_per_day``,
  ``fleet_upload_grants_per_day`` y ``catalog_versions_per_day``, por día y a escala objetivo).

El informe JSON (``--out``, por defecto ``.benchmarks/volumetry-<escala>.json``) es el artefacto de
``nightly``. El script termina con error si la generación o la verificación de la cadena fallan;
un objetivo no cumplido se declara en el informe y no cambia el código de salida.

Solo datos generados. Usa como mucho 3 conexiones de escritura (regla de recursos compartidos).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import itertools
import json
import math
import os
import platform
import random
import sys
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Generator, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast

BACKEND: Final = Path(__file__).resolve().parents[2]
if str(BACKEND) not in sys.path:  # ejecutado como script: el paquete ``tests`` está en backend/
    sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

from tests.benchmarks.gob_support import (  # noqa: E402
    MAX_CAMERAS,
    MAX_STANDARDS,
    catalog_zone,
    closed_record,
    open_walk_test,
)
from tests.catalog_ports_support import PortsWorld  # noqa: E402
from tests.gob_platform_support import GobPlatform, GobZone, gob_platform, ok  # noqa: E402
from tests.identity_db import MigratedDatabase, migrated_database  # noqa: E402
from tests.integration.conftest import (  # noqa: E402
    LocalStackEndpoint,
    PostgresEndpoint,
    localstack_endpoint_session,
    postgres_endpoint_session,
)
from tests.integration.test_coverage_port import COVERAGE_TYPES, scoped  # noqa: E402
from tests.verify_support import CHECKPOINT_KEY, StaticKeys  # noqa: E402
from tests.volumetry.scale_data import (  # noqa: E402
    CatalogZone,
    FleetNode,
    GobRates,
    OrganizationRef,
    PlantRef,
    Rates,
    ScaleWriter,
    ZoneRef,
    catalog_history,
    copy_inventory,
    create_fleet_partitions,
    create_partitions,
    days_back,
    enroll_fleet,
    enrollment_attempts,
    fleet_alarms,
    heartbeat_history,
    historical_stamps,
    kit_findings,
    lean_findings,
    load_catalog_history,
    seed_organization,
    upload_grants,
)
from tests.writer_support import (  # noqa: E402
    ORDER_TYPE,
    Place,
    WriterEnvironment,
    order_document,
    unit_context,
    writer_environment,
)
from vigia_platform.catalog.adapters.postgres.query_ports import (  # noqa: E402
    catalog_query_ports,
)
from vigia_platform.catalog.domain.enums import GateKind  # noqa: E402
from vigia_platform.catalog.domain.ports import StandardRef  # noqa: E402
from vigia_platform.ledger.adapters.integrity_store import SqlIntegrityStore  # noqa: E402
from vigia_platform.ledger.application.coverage import CoverageService  # noqa: E402
from vigia_platform.ledger.application.reader import (  # noqa: E402
    LectorExpediente,
    LedgerFilters,
    PageRequest,
)
from vigia_platform.ledger.application.writer import Receipt  # noqa: E402
from vigia_platform.ledger.chain.checkpoints import CheckpointChain  # noqa: E402
from vigia_platform.ledger.chain.verify import (  # noqa: E402
    IntegrityService,
    IntegrityStatus,
    VerificationMode,
)
from vigia_platform.ledger.domain.coverage import CoveragePeriod  # noqa: E402
from vigia_platform.shared.context import (  # noqa: E402
    ActorKind,
    ActorUnit,
    AllowedScope,
    Role,
    ScopeLevel,
)
from vigia_platform.shared.db import TemporarilyUnavailable  # noqa: E402

MAX_CONNECTIONS: Final = 3
CONTAINER_ATTEMPTS: Final = 3


@dataclass(frozen=True)
class Scale:
    """Tamaño de la generación (NFR-NUC-05)."""

    name: str
    largest_plants: int
    largest_zones_per_plant: int
    other_plants: tuple[int, ...]
    """Plantas de cada una de las otras organizaciones (3 zonas cada una)."""
    other_zones_per_plant: int
    days: int
    other_days: int
    audit_per_day: int
    rounds: int
    timeline_rounds: int
    findings_pool: int
    rates: Rates = field(default_factory=Rates)

    @property
    def organizations(self) -> int:
        return 1 + len(self.other_plants)

    @property
    def plants(self) -> int:
        return self.largest_plants + sum(self.other_plants)

    @property
    def zones(self) -> int:
        return (
            self.largest_plants * self.largest_zones_per_plant
            + sum(self.other_plants) * self.other_zones_per_plant
        )


SCALES: Final = {
    "target": Scale(
        name="target",
        largest_plants=20,
        largest_zones_per_plant=3,
        other_plants=(2,) * 31 + (1,) * 18,
        other_zones_per_plant=3,
        days=365,
        other_days=7,
        audit_per_day=4_000,
        rounds=100,
        timeline_rounds=30,
        findings_pool=64,
    ),
    "smoke": Scale(
        name="smoke",
        largest_plants=2,
        largest_zones_per_plant=2,
        other_plants=(1,),
        other_zones_per_plant=2,
        days=3,
        other_days=1,
        audit_per_day=50,
        rounds=5,
        timeline_rounds=3,
        findings_pool=8,
        rates=Rates(findings=6, observability_pairs=3, classifications=3, hot_zone_pairs=20),
    ),
}

OBJECTIVES_MS: Final = {
    "ledger_list_200_plant_scope": 300.0,
    "ledger_list_200_plant_scope_mid_year": 300.0,
    "ledger_list_200_organization_scope": 300.0,
    "coverage_timeline_31d_hot_zone": 1_000.0,
    "coverage_timeline_31d_zone": 1_000.0,
    "coverage_state_at_hot_zone": 50.0,
    "coverage_state_at_zone": 50.0,
    "ledger_write": 150.0,
}
"""``[objetivos propios]`` de NFR-NUC-01 y NFR-NUC-04."""

VERIFY_TARGET_PER_SECOND: Final = 5_000

_LEDGER_SIZE: Final = (
    "SELECT coalesce(sum(pg_total_relation_size(relid)), 0)"
    " FROM pg_partition_tree('ledger.ledger_record')"
)
_LEDGER_EXTRA_SIZE: Final = (
    "SELECT pg_total_relation_size('ledger.record_identity')"
    " + pg_total_relation_size('ledger.record_source_key')"
)
_AUDIT_SIZE: Final = (
    "SELECT coalesce(sum(pg_total_relation_size(relid)), 0)"
    " FROM pg_partition_tree('shared.audit_entry')"
    " UNION ALL SELECT pg_total_relation_size('shared.audit_entry_identity')"
)


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# --- Medidas ----------------------------------------------------------------------------------


@dataclass
class Timing:
    name: str
    label: str
    rounds: int
    median_ms: float | None
    p95_ms: float | None
    """``None`` si la operación agotó el ``statement_timeout`` (``details.timed_out``)."""
    objective_ms: float | None
    objective_met: bool | None
    details: dict[str, Any]


def _percentile(values: Sequence[float], fraction: float) -> float:
    """Percentil por rango más cercano, como los bancos (``tests/benchmarks/conftest.py``)."""
    ordered = sorted(values)
    return ordered[max(1, math.ceil(fraction * len(ordered))) - 1]


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def _time(
    env: WriterEnvironment,
    name: str,
    label: str,
    operation: Callable[[], Awaitable[object]],
    rounds: int,
    details: dict[str, Any] | None = None,
) -> Timing:
    """``rounds`` ejecuciones (con 2 de calentamiento) medidas con el reloj de alta resolución.

    Si una ejecución agota el ``statement_timeout`` del pool (30 s en el proceso de trabajo), la
    operación se declara así en el informe (``timed_out``, objetivo no cumplido) y la medición
    sigue con la siguiente: no se repiten 100 rondas de 30 s.
    """
    objective = OBJECTIVES_MS.get(name)
    times: list[float] = []
    try:
        for _ in range(2):
            env.loop.run(operation())
        for _ in range(rounds):
            started = time.perf_counter()  # noqa: TID251 - la volumetría mide tiempo real.
            env.loop.run(operation())
            times.append((time.perf_counter() - started) * 1000)  # noqa: TID251
    except TemporarilyUnavailable as error:
        # ``TemporarilyUnavailable`` cubre el ``statement_timeout`` y la base caída: se distingue
        # por la causa del controlador para no declarar lento lo que fue una conexión perdida.
        causes: list[str] = []
        cause: BaseException | None = error
        while cause is not None:
            causes.append(type(cause).__name__)
            cause = cause.__cause__ or cause.__context__
        timed_out = "QueryCanceledError" in causes
        reason = "agotó el statement_timeout de 30 s" if timed_out else "base no disponible"
        _log(f"  {name}: {reason} ({len(times)} rondas completas) NO cumple")
        return Timing(
            name=name,
            label=label,
            rounds=len(times),
            median_ms=None,
            p95_ms=None,
            objective_ms=objective,
            objective_met=False if objective is not None and timed_out else None,
            details={
                **(details or {}),
                "timed_out": timed_out,
                "unavailable": not timed_out,
                "statement_timeout_ms": 30_000,
                "causes": causes,
            },
        )
    p95 = _percentile(times, 0.95)
    timing = Timing(
        name=name,
        label=label,
        rounds=rounds,
        median_ms=round(_median(times), 2),
        p95_ms=round(p95, 2),
        objective_ms=objective,
        objective_met=None if objective is None else p95 <= objective,
        details=dict(details or {}),
    )
    verdict = "" if objective is None else (" cumple" if timing.objective_met else " NO cumple")
    _log(f"  {name}: mediana {timing.median_ms} ms, p95 {timing.p95_ms} ms{verdict}")
    return timing


# --- Generación -------------------------------------------------------------------------------


async def _sizes(connection: Any) -> dict[str, int]:
    ledger = int(await connection.fetchval(_LEDGER_SIZE)) + int(
        await connection.fetchval(_LEDGER_EXTRA_SIZE)
    )
    audit = sum(int(row[0]) for row in await connection.fetch(_AUDIT_SIZE))
    return {"ledger_bytes": ledger, "audit_bytes": audit}


async def _parallel(
    migrated: MigratedDatabase,
    workers: int,
    jobs: Sequence[Callable[[Any], Awaitable[None]]],
) -> None:
    """Reparte ``jobs`` entre ``workers`` conexiones de superusuario.

    La marca histórica la pone el llamador **una vez** para toda la generación: si cada conexión
    la pusiera y la quitara, la primera en terminar devolvería la marca real a las demás.
    """
    queue: asyncio.Queue[Callable[[Any], Awaitable[None]]] = asyncio.Queue()
    for job in jobs:
        queue.put_nowait(job)
    done = 0

    async def worker() -> None:
        nonlocal done
        connection = await migrated.connect()
        try:
            while not queue.empty():
                job = queue.get_nowait()
                await job(connection)
                done += 1
                if done % 5 == 0 or done == len(jobs):
                    _log(f"  {done}/{len(jobs)} trabajos")
        finally:
            await connection.close()

    await asyncio.gather(*(worker() for _ in range(workers)))


@dataclass
class Generated:
    largest: OrganizationRef
    others: list[OrganizationRef]
    now: datetime
    first_day: datetime
    written_largest: dict[str, int]
    written_total: dict[str, int]
    sizes_before: dict[str, int]
    sizes_after_largest: dict[str, int]
    seconds: float


async def _generate(migrated: MigratedDatabase, scale: Scale, seed: int, workers: int) -> Generated:
    started = time.perf_counter()  # noqa: TID251 - duración de la generación.
    rng = random.Random(seed)  # noqa: S311 - datos sintéticos deterministas
    connection = await migrated.connect()
    try:
        now: datetime = await connection.fetchval("SELECT date_trunc('milliseconds', now())")
        first_day = days_back(now, scale.days)
        # También el mes siguiente: una corrida larga que cruce el fin de mes escribe (marcas en
        # vivo de las mediciones) en esa partición, no en la de por defecto (seguimiento de VIG-91).
        await create_partitions(connection, first_day, now + timedelta(days=31))
        since = first_day - timedelta(days=1)
        largest = await seed_organization(
            connection,
            rng,
            plants=scale.largest_plants,
            zones_per_plant=scale.largest_zones_per_plant,
            since=since,
        )
        others = [
            await seed_organization(
                connection,
                rng,
                plants=plants,
                zones_per_plant=scale.other_zones_per_plant,
                since=since,
            )
            for plants in scale.other_plants
        ]
        sizes_before = await _sizes(connection)
    finally:
        await connection.close()

    findings = kit_findings(scale.findings_pool, seed)
    writer = ScaleWriter(findings, seed=seed, rates=scale.rates)
    hot_zone = largest.plants[0].zones[0].zone_id

    def plant_job(
        organization: OrganizationRef, plant: PlantRef, first: datetime, days: int
    ) -> Callable[[Any], Awaitable[None]]:
        async def job(conn: Any) -> None:
            await writer.bootstrap_plant(conn, organization, plant, first - timedelta(hours=1))
            await writer.plant_history(conn, organization, plant, first, days, hot_zone=hot_zone)

        return job

    async def audit_job(conn: Any) -> None:
        await writer.audit_history(conn, largest, first_day, scale.days, scale.audit_per_day)

    jobs: list[Callable[[Any], Awaitable[None]]] = [audit_job]
    jobs += [plant_job(largest, plant, first_day, scale.days) for plant in largest.plants]
    other_first = days_back(now, scale.other_days)
    other_jobs = [
        plant_job(organization, plant, other_first, scale.other_days)
        for organization in others
        for plant in organization.plants
    ]
    control = await migrated.connect()
    try:
        async with historical_stamps(control):
            _log(f"organización mayor: {len(largest.plants)} plantas, {scale.days} días")
            await _parallel(migrated, workers, jobs)
            written_largest = dict(writer.written)
            sizes_after_largest = await _sizes(control)
            _log(f"otras {len(others)} organizaciones: {scale.other_days} días")
            await _parallel(migrated, workers, other_jobs)
        _log("ANALYZE")
        await control.execute("ANALYZE")
        oldest = await control.fetchval(
            "SELECT min(received_at) FROM ledger.ledger_record WHERE organization_id = $1",
            largest.organization_id,
        )
    finally:
        await control.close()
    if oldest is None or oldest >= first_day + timedelta(days=1):
        raise RuntimeError(f"la historia no quedó en su fecha: el registro más antiguo es {oldest}")
    return Generated(
        largest=largest,
        others=others,
        now=now,
        first_day=first_day,
        written_largest=written_largest,
        written_total=dict(writer.written),
        sizes_before=sizes_before,
        sizes_after_largest=sizes_after_largest,
        seconds=time.perf_counter() - started,  # noqa: TID251
    )


# --- Medición de NFR-NUC-01 ---------------------------------------------------------------------


def _measure(
    env: WriterEnvironment, generated: Generated, scale: Scale, timings: list[Timing]
) -> None:
    """Mide NFR-NUC-01 y deja cada medida en ``timings`` (lo medido sobrevive a un fallo)."""
    largest = generated.largest
    organization_id = largest.organization_id
    reader = LectorExpediente(database=env.database, audit=env.audit)
    coverage = CoverageService(database=env.database, audit=env.audit)
    hot_plant = largest.plants[0]
    plant = largest.plants[1] if len(largest.plants) > 1 else hot_plant
    plant_context = scoped(
        organization_id, [AllowedScope(ScopeLevel.PLANT, plant.plant_id, Role.PLANT_MANAGER)]
    )
    hot_context = scoped(
        organization_id, [AllowedScope(ScopeLevel.PLANT, hot_plant.plant_id, Role.PLANT_MANAGER)]
    )
    organization_context = scoped(
        organization_id,
        [AllowedScope(ScopeLevel.ORGANIZATION, organization_id, Role.ADMINISTRATOR)],
    )
    year = LedgerFilters(
        received_from=generated.first_day, received_before=generated.now + timedelta(days=1)
    )
    mid_year = LedgerFilters(
        received_from=generated.first_day,
        received_before=generated.first_day + timedelta(days=scale.days // 2),
    )
    end = days_back(generated.now, 0)
    period = CoveragePeriod(end - timedelta(days=min(31, scale.days)), end)
    rng = random.Random(7)  # noqa: S311 - instantes de prueba

    def instant() -> datetime:
        return end - timedelta(milliseconds=rng.randrange(int(period_ms())))

    def period_ms() -> float:
        return (period.end - period.start).total_seconds() * 1000

    rounds = scale.rounds
    timings.extend(
        [
            _time(
                env,
                "ledger_list_200_plant_scope",
                "Lista de 200 con alcance de planta (3 zonas) sobre 1 año, primera página",
                lambda: reader.list(plant_context, year, PageRequest(size=200)),
                rounds,
            ),
            _time(
                env,
                "ledger_list_200_plant_scope_mid_year",
                "Lista de 200 con alcance de planta, página a mitad del año",
                lambda: reader.list(plant_context, mid_year, PageRequest(size=200)),
                rounds,
            ),
            _time(
                env,
                "ledger_list_200_organization_scope",
                "Lista de 200 con alcance de organización (60 zonas) sobre 1 año",
                lambda: reader.list(organization_context, year, PageRequest(size=200)),
                rounds,
            ),
            _time(
                env,
                "coverage_timeline_31d_hot_zone",
                "Línea de tiempo de 31 días, zona con el máximo de eventos y un año de historia",
                lambda: coverage.linea_de_tiempo(hot_context, hot_plant.zones[0].zone_id, period),
                scale.timeline_rounds,
            ),
            _time(
                env,
                "coverage_timeline_31d_zone",
                "Línea de tiempo de 31 días, zona normal con un año de historia",
                lambda: coverage.linea_de_tiempo(plant_context, plant.zones[0].zone_id, period),
                scale.timeline_rounds,
            ),
            _time(
                env,
                "coverage_state_at_hot_zone",
                "estado_en de la zona con el volumen máximo de eventos",
                lambda: coverage.estado_en(hot_context, hot_plant.zones[0].zone_id, instant()),
                rounds,
            ),
            _time(
                env,
                "coverage_state_at_zone",
                "estado_en de una zona normal con un año de historia",
                lambda: coverage.estado_en(plant_context, plant.zones[0].zone_id, instant()),
                rounds,
            ),
        ]
    )
    zone = plant.zones[1] if len(plant.zones) > 1 else plant.zones[0]
    place = Place(organization_id, plant.plant_id, zone.zone_id, zone.node_id)
    node = unit_context(organization_id, ActorUnit.U03, kind=ActorKind.SYSTEM)

    async def write() -> None:
        receipt = await env.writer.write(node, ORDER_TYPE, order_document(place, clips=0))
        if not isinstance(receipt, Receipt):
            raise RuntimeError(f"escritura rechazada: {receipt}")

    timings.append(
        _time(env, "ledger_write", "Escritura sin evidencias con un año cargado", write, rounds)
    )


def _verify(env: WriterEnvironment, generated: Generated) -> dict[str, Any]:
    """Verificación incremental desde la génesis de una cadena de planta de un año."""
    assert env.outbox is not None
    plant = generated.largest.plants[1 if len(generated.largest.plants) > 1 else 0]
    service = IntegrityService(
        store=SqlIntegrityStore(database=env.database, audit=env.audit, outbox=env.outbox),
        keys=StaticKeys(CHECKPOINT_KEY),
        clock=env.clock,
    )
    context = unit_context(generated.largest.organization_id, ActorUnit.U02, kind=ActorKind.SYSTEM)
    started = time.perf_counter()  # noqa: TID251 - la volumetría mide tiempo real.
    result = env.loop.run(
        service.verify(context, CheckpointChain.plant(plant.plant_id), VerificationMode.INCREMENTAL)
    )
    seconds = time.perf_counter() - started  # noqa: TID251
    if result.status is not IntegrityStatus.INTACT:
        raise RuntimeError(f"la cadena generada no está íntegra: {result}")
    rate = result.to_sequence / seconds
    _log(f"  verificación: {result.to_sequence} registros en {seconds:.1f} s ({rate:,.0f}/s)")
    return {
        "records": result.to_sequence,
        "seconds": round(seconds, 2),
        "records_per_second": round(rate),
        "objective_per_second": VERIFY_TARGET_PER_SECOND,
        "objective_met": rate >= VERIFY_TARGET_PER_SECOND,
    }


# --- Informe ----------------------------------------------------------------------------------


def _growth(generated: Generated, scale: Scale) -> dict[str, Any]:
    zones = len(generated.largest.zones)
    days = scale.days
    ledger_bytes = (
        generated.sizes_after_largest["ledger_bytes"] - generated.sizes_before["ledger_bytes"]
    )
    audit_bytes = (
        generated.sizes_after_largest["audit_bytes"] - generated.sizes_before["audit_bytes"]
    )
    audit_entries = generated.written_largest.get("audit_entry", 0)
    ledger_records = sum(
        count for name, count in generated.written_largest.items() if name != "audit_entry"
    )
    per_day = ledger_bytes / days
    return {
        "organization_zones": zones,
        "days": days,
        "ledger_records": ledger_records,
        "ledger_records_per_day": round(ledger_records / days),
        "ledger_bytes_per_day": round(per_day),
        "ledger_bytes_per_record": round(ledger_bytes / max(ledger_records, 1)),
        "ledger_bytes_per_zone_day": round(per_day / zones),
        "ledger_bytes_per_day_at_300_zones": round(per_day / zones * 300),
        "audit_entries_per_day": round(audit_entries / days),
        "audit_bytes_per_day": round(audit_bytes / days),
        "audit_bytes_per_entry": round(audit_bytes / max(audit_entries, 1)),
        "records_by_type": generated.written_largest,
        "note": (
            "Tamaño en disco de PostgreSQL 16 (tablas, particiones, índices y TOAST) que añade el "
            "año de la organización mayor; la zona con el volumen máximo de eventos lo sube. El "
            "contenido de los hallazgos es el del generador del kit de U-01 (mediana ~3 KB), no "
            "la estimación de 10-20 KB de NFR-NUC-03; el tamaño con el nodo simulado lo mide U-03."
        ),
    }


def run(
    endpoint: PostgresEndpoint, scale: Scale, *, seed: int, workers: int, out: Path
) -> dict[str, Any]:
    """Genera, mide y escribe el informe en ``out``; devuelve el informe.

    Si una fase posterior a la generación falla (p. ej. la base desaparece a mitad de la medida),
    el informe se escribe igual con lo medido hasta entonces y el error, y la excepción sigue.
    """
    workers = max(1, min(workers, MAX_CONNECTIONS))
    timings: list[Timing] = []
    with (
        migrated_database(endpoint, f"volumetry_{scale.name}") as migrated,
        writer_environment(migrated, extra_types=COVERAGE_TYPES) as env,
    ):
        generated = env.loop.run(_generate(migrated, scale, seed, workers))
        total = sum(generated.written_total.values())
        _log(f"generados {total} registros y entradas en {generated.seconds:.0f} s")
        growth = _growth(generated, scale)
        _log(f"NFR-NUC-03: {json.dumps(growth, ensure_ascii=False)}")
        _log("midiendo NFR-NUC-01 sobre la organización mayor")
        try:
            _measure(env, generated, scale, timings)
            verification = _verify(env, generated)
        except Exception as error:
            partial = _report(scale, seed, workers, generated, growth, timings, None)
            partial["error"] = f"{type(error).__name__}: {error}"
            _write(out, partial)
            raise
    report = _report(scale, seed, workers, generated, growth, timings, verification)
    _write(out, report)
    return report


def _write(out: Path, report: dict[str, Any]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _report(
    scale: Scale,
    seed: int,
    workers: int,
    generated: Generated,
    growth: dict[str, Any],
    timings: Sequence[Timing],
    verification: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),  # noqa: TID251
        "scale": {
            "name": scale.name,
            "organizations": scale.organizations,
            "plants": scale.plants,
            "zones": scale.zones,
            "largest_organization": {
                "plants": scale.largest_plants,
                "zones": scale.largest_plants * scale.largest_zones_per_plant,
                "days": scale.days,
            },
            "other_organizations_days": scale.other_days,
            "rates_per_zone_day": asdict(scale.rates),
            "audit_entries_per_day_largest": scale.audit_per_day,
        },
        "seed": seed,
        "generation_seconds": round(generated.seconds),
        "written": generated.written_total,
        "environment": {
            "python": platform.python_version(),
            "machine": platform.machine(),
            "cpus": os.cpu_count(),
            "write_connections": workers,
        },
        "nfr_nuc_03": growth,
        "nfr_nuc_01": [asdict(timing) for timing in timings],
        "verification": verification,
    }


# --- U-03: gobernanza y flota (TASK-233; NFR-GOB-03, 04, 07, 11 y 16) ----------------------------


@dataclass(frozen=True)
class GobScale:
    """Lo que la volumetría de U-03 añade (``GobPlatform``: la aplicación completa de U-03)."""

    extra_plants: int
    """Plantas de la organización mayor generadas en masa (más la de las zonas por las rutas)."""
    zones_per_plant: int
    other_active_nodes: int
    """Nodos activos (con ficha, latidos y concesiones) de las otras organizaciones."""
    ledger_days: int
    core_standards: int
    core_cameras: int
    rates: GobRates
    rounds: int
    ledger_rates: Rates


GOB_SCALES: Final = {
    "target": GobScale(
        extra_plants=19,
        zones_per_plant=3,
        other_active_nodes=40,
        ledger_days=365,
        core_standards=32,
        core_cameras=8,
        rates=GobRates(),
        rounds=100,
        # Las 37 clasificaciones de U-04 por zona y día van como hallazgos: su tipo no está
        # registrado en la plataforma de U-03 y lo que cuenta aquí es el número de registros.
        ledger_rates=Rates(findings=77 + 37, classifications=0, communication_pairs=2),
    ),
    "smoke": GobScale(
        extra_plants=1,
        zones_per_plant=2,
        other_active_nodes=1,
        ledger_days=2,
        core_standards=2,
        core_cameras=2,
        rates=GobRates(
            heartbeat_seconds=900,
            heartbeat_days=2,
            grants_per_zone_day=10,
            grant_days=1,
            history_months=3,
            catalog_versions_per_month=2,
            attempts_per_node_month=2,
            alarms_per_node_month=2,
        ),
        rounds=3,
        ledger_rates=Rates(
            findings=9,
            observability_pairs=3,
            classifications=0,
            hot_zone_pairs=3,
            communication_pairs=2,
        ),
    ),
}

GOB_OBJECTIVES_MS: Final = {
    "gob_console_fleet_nodes": 500.0,
    "gob_console_fleet_node_detail": 300.0,
    "gob_console_walk_test_current": 200.0,
    "gob_console_zone_catalog": 200.0,
    "gob_console_catalog_versions": 200.0,
    "gob_console_zone_gates": 200.0,
    "gob_console_zone_transparency": 200.0,
    "gob_console_commissioning_record": 300.0,
    "gob_console_documents": 300.0,
}
"""``[objetivos propios]`` de NFR-GOB-03; los de los puertos (NFR-GOB-04) van con cada uno."""
POINT_MS: Final = 5.0
BATCH_MS: Final = 50.0
PERSON_SPACING_SECONDS: Final = 0.2
UNAVAILABLE: Final = 503
"""``temporarily_unavailable``: el ``statement_timeout`` de la base, entre otros."""
MONTH_DAYS: Final = 30
PASSES_PER_CELL: Final = 3
"""El mínimo por celda; ``open_walk_test`` añade pases hasta las 100 repeticiones de la latencia."""


@dataclass
class GobGenerated:
    organization_id: uuid.UUID
    admin: Any
    installer_zone: GobZone
    record_id: str
    walk_zone_id: uuid.UUID
    catalog_zone: CatalogZone
    zones: list[CatalogZone]
    plants: list[uuid.UUID]
    detail_node: uuid.UUID
    now: datetime
    history_from: datetime
    written: Counter[str]
    heartbeat_bytes: int
    grant_bytes: int
    seconds: float


def _timed(
    run: Callable[[Awaitable[object]], object],
    name: str,
    label: str,
    operation: Callable[[], Awaitable[object]],
    rounds: int,
    objective: float | None,
    details: dict[str, Any] | None = None,
    *,
    before: Callable[[], None] | None = None,
    timed_out: Callable[[], bool] | None = None,
) -> Timing:
    """Como ``_time``, sobre cualquier bucle (``run``) y con la preparación ``before`` de cada
    ronda fuera de la medida (el reloj simulado de la plataforma).

    Si ``timed_out`` dice que la operación agotó el tope de la base (una ruta que respondió
    ``temporarily_unavailable``), la medición se declara así y no se repiten las rondas que
    quedan, como en ``_time``.
    """
    times: list[float] = []
    for index in range(rounds + 2):
        if before is not None:
            before()
        started = time.perf_counter()  # noqa: TID251 - la volumetría mide tiempo real.
        run(operation())
        elapsed = (time.perf_counter() - started) * 1000  # noqa: TID251
        if timed_out is not None and timed_out():
            _log(f"  {name}: agotó el tope ({elapsed:.0f} ms, {len(times)} rondas completas)")
            return Timing(
                name=name,
                label=label,
                rounds=len(times),
                median_ms=None,
                p95_ms=None,
                objective_ms=objective,
                objective_met=None if objective is None else False,
                details={**(details or {}), "timed_out": True, "elapsed_ms": round(elapsed)},
            )
        if index >= 2:
            times.append(elapsed)
    p95 = _percentile(times, 0.95)
    timing = Timing(
        name=name,
        label=label,
        rounds=rounds,
        median_ms=round(_median(times), 2),
        p95_ms=round(p95, 2),
        objective_ms=objective,
        objective_met=None if objective is None else p95 <= objective,
        details={**(details or {}), "p99_ms": round(_percentile(times, 0.99), 2)},
    )
    verdict = "" if objective is None else (" cumple" if timing.objective_met else " NO cumple")
    _log(f"  {name}: mediana {timing.median_ms} ms, p95 {timing.p95_ms} ms{verdict}")
    return timing


def _gob_core(gob: GobPlatform, gob_scale: GobScale) -> tuple[Any, ...]:
    """Las zonas que nacen por las **rutas reales** en la organización mayor: el acta cerrada de
    la matriz máxima, un walk-test abierto y una zona normal cuyo latido es la plantilla del
    inventario generado."""
    flow, record_zone = catalog_zone(
        gob, standards=gob_scale.core_standards, cameras=gob_scale.core_cameras
    )
    flow.mount(record_zone)
    passes = PASSES_PER_CELL
    record_id = closed_record(flow, record_zone, passes)
    _, walk_zone = catalog_zone(
        gob,
        standards=gob_scale.core_standards,
        cameras=gob_scale.core_cameras,
        within=record_zone,
    )
    flow.mount(walk_zone)
    open_walk_test(flow, walk_zone, passes)
    _, template = catalog_zone(gob, within=record_zone)
    for zone in (record_zone, walk_zone, template):
        ok(flow.post_heartbeat(zone))
    return flow, record_zone, record_id, walk_zone, template


async def _gob_bulk(
    gob: GobPlatform,
    scale: Scale,
    gob_scale: GobScale,
    seed: int,
    workers: int,
    core: tuple[Any, ...],
    signers: Sequence[tuple[str, uuid.UUID]],
) -> GobGenerated:
    started = time.perf_counter()  # noqa: TID251 - duración de la generación.
    rates = gob_scale.rates
    _, record_zone, record_id, walk_zone, template = core
    organization = record_zone.organization_id
    user = record_zone.admin_id
    migrated = gob.authz.sessions.migrated
    rng = random.Random(seed)  # noqa: S311 - datos sintéticos deterministas
    now = gob.now()
    history_from = days_back(now, rates.history_months * MONTH_DAYS)
    written: Counter[str] = Counter()
    connection = await migrated.connect()
    try:
        await create_partitions(connection, days_back(now, gob_scale.ledger_days + 31), now)
        await create_fleet_partitions(connection, history_from, now + timedelta(days=31))
        # La organización mayor: plantas y zonas generadas, cada zona con su nodo dado de alta.
        plants: list[PlantRef] = []
        nodes: list[FleetNode] = []
        zones: list[CatalogZone] = []
        async with connection.transaction():
            for plant_index in range(gob_scale.extra_plants):
                plant_id = uuid.UUID(int=rng.getrandbits(128), version=4)
                await connection.execute(
                    "INSERT INTO identity.plant (plant_id, organization_id, code, name, country,"
                    " data_region, timezone, created_at, created_by) VALUES ($1, $2, $3,"
                    " 'Planta sintética', 'CO', 'us-east-1', 'UTC', $4, $5)",
                    plant_id,
                    organization,
                    f"PL-GOB-{plant_index:04d}-{plant_id.hex[:8].upper()}",
                    history_from,
                    user,
                )
                refs: list[ZoneRef] = []
                for zone_index in range(gob_scale.zones_per_plant):
                    index = plant_index * gob_scale.zones_per_plant + zone_index
                    zone_id, node_id = (uuid.UUID(int=rng.getrandbits(128), version=4)
                                        for _ in range(2))  # fmt: skip
                    cameras = tuple(
                        uuid.UUID(int=rng.getrandbits(128), version=4)
                        for _ in range(1 + index % MAX_CAMERAS)
                    )
                    standards = tuple(
                        uuid.UUID(int=rng.getrandbits(128), version=4)
                        for _ in range(1 + (index * 7) % MAX_STANDARDS)
                    )
                    await connection.execute(
                        "INSERT INTO identity.zone (zone_id, organization_id, plant_id, code,"
                        " name, created_at, created_by) VALUES ($1, $2, $3, $4,"
                        " 'Zona sintética', $5, $6)",
                        zone_id,
                        organization,
                        plant_id,
                        f"ZN-GOB-{index:04d}",
                        history_from,
                        user,
                    )
                    await connection.execute(
                        "INSERT INTO identity.node_identity (node_id, organization_id, plant_id,"
                        " code, status, created_at) VALUES ($1, $2, $3, $4, 'enrolled', $5)",
                        node_id,
                        organization,
                        plant_id,
                        f"ND-GOB-{index:04d}",
                        history_from,
                    )
                    await connection.execute(
                        "INSERT INTO identity.zone_node_assignment (assignment_id, organization_id,"
                        " plant_id, zone_id, node_id, assigned_at, assigned_by)"
                        " VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6)",
                        organization,
                        plant_id,
                        zone_id,
                        node_id,
                        history_from,
                        user,
                    )
                    refs.append(ZoneRef(zone_id, node_id, cameras[0]))
                    nodes.append(FleetNode(organization, plant_id, zone_id, node_id, cameras))
                    zones.append(CatalogZone(plant_id, zone_id, standards, cameras))
                plants.append(PlantRef(plant_id, tuple(refs)))
            await enroll_fleet(connection, nodes, user, history_from)
            await copy_inventory(connection, template.node, nodes, now - timedelta(minutes=1))
        largest = OrganizationRef(organization, user, tuple(plants))
        # Las otras organizaciones (identidad de NFR-NUC-05) con ``other_active_nodes`` activos.
        others = [
            await seed_organization(
                connection,
                rng,
                plants=count,
                zones_per_plant=scale.other_zones_per_plant,
                since=history_from,
            )
            for count in scale.other_plants
        ]
        active: list[FleetNode] = []
        for other in others:
            for plant in other.plants:
                zone = plant.zones[0]
                if len(active) < gob_scale.other_active_nodes:
                    node = FleetNode(other.organization_id, plant.plant_id, zone.zone_id,
                                     zone.node_id, (zone.camera_id,))  # fmt: skip
                    await enroll_fleet(connection, [node], other.user_id, history_from)
                    await copy_inventory(connection, template.node, [node], now)
                    active.append(node)
        # Catálogo y compuertas de 24 meses de las zonas generadas de la organización mayor.
        versions = rates.history_months * rates.catalog_versions_per_month
        history = catalog_history(
            organization,
            user,
            signers,
            zones,
            [plant.plant_id for plant in plants],
            origin=history_from,
            now=now,
            rng=rng,
            versions=versions,
            version_spacing=timedelta(days=rates.history_months * MONTH_DAYS) / versions,
            gate_intervals=rates.history_months,
            agreements=rates.history_months,
            policies=3,
        )
        await load_catalog_history(connection, history)
        written["catalog_versions"] = len(history.versions)
        written["gate_intervals"] = len(history.intervals)
    finally:
        await connection.close()

    route_nodes = [
        FleetNode(organization, z.plant_id, z.zone_id, z.node, z.cameras)
        for z in (record_zone, walk_zone, template)
    ]
    fleet = route_nodes + nodes + active
    heartbeat_from = now - timedelta(days=rates.heartbeat_days)
    grants_from = days_back(now, rates.grant_days)
    summary = await _template_summary(migrated, template.node)

    def per_node(node: FleetNode) -> Callable[[Any], Awaitable[None]]:
        async def job(conn: Any) -> None:
            # Cada conteo se suma después de su ``await``: los trabajos corren a la vez.
            counts = {
                "heartbeat_history": await heartbeat_history(
                    conn, node, heartbeat_from, now - timedelta(minutes=1), summary,
                    every=rates.heartbeat_seconds,
                ),
                "clip_upload_grant": await upload_grants(
                    conn, node, grants_from, rates.grant_days, rates.grants_per_zone_day
                ),
                "enrollment_attempt": await enrollment_attempts(
                    conn, node, history_from, rates.history_months, rates.attempts_per_node_month
                ),
                "fleet_alarm": await fleet_alarms(
                    conn, node, history_from, rates.history_months, rates.alarms_per_node_month
                ),
            }  # fmt: skip
            written.update(counts)

        return job

    _log(f"flota: {len(fleet)} nodos, {rates.heartbeat_days} días de latidos")
    await _parallel(migrated, workers, [per_node(node) for node in fleet])
    # Un año de expediente de las plantas generadas, con las transiciones de comunicación de cada
    # nodo (lo que recorre ``GET /fleet/nodes``).
    writer = ScaleWriter(lean_findings(16), seed=seed, rates=gob_scale.ledger_rates)
    ledger_from = days_back(now, gob_scale.ledger_days)

    def plant_job(plant: PlantRef) -> Callable[[Any], Awaitable[None]]:
        async def job(conn: Any) -> None:
            await writer.bootstrap_plant(conn, largest, plant, ledger_from - timedelta(hours=1))
            await writer.plant_history(conn, largest, plant, ledger_from, gob_scale.ledger_days)

        return job

    control = await migrated.connect()
    try:
        async with historical_stamps(control):
            _log(f"expediente: {len(plants)} plantas, {gob_scale.ledger_days} días")
            await _parallel(migrated, workers, [plant_job(plant) for plant in plants])
        written.update(writer.written)
        _log("ANALYZE")
        await control.execute("ANALYZE")
        heartbeat_bytes = int(
            await control.fetchval(
                "SELECT coalesce(sum(pg_total_relation_size(relid)), 0)"
                " FROM pg_partition_tree('fleet.heartbeat_history')"
            )
        )
        grant_bytes = int(await control.fetchval(
            "SELECT pg_total_relation_size('fleet.clip_upload_grant')"
        ))  # fmt: skip
    finally:
        await control.close()
    detail = max(nodes, key=lambda node: len(node.cameras))
    widest = max(zones, key=lambda zone: len(zone.standards))
    return GobGenerated(
        organization_id=organization,
        admin=record_zone.admin,
        installer_zone=record_zone,
        record_id=record_id,
        walk_zone_id=walk_zone.zone_id,
        catalog_zone=widest,
        zones=zones,
        plants=[plant.plant_id for plant in plants],
        detail_node=detail.node_id,
        now=now,
        history_from=history_from,
        written=written,
        heartbeat_bytes=heartbeat_bytes,
        grant_bytes=grant_bytes,
        seconds=time.perf_counter() - started,  # noqa: TID251
    )


async def _template_summary(migrated: MigratedDatabase, node_id: uuid.UUID) -> dict[str, Any]:
    """El ``payload_summary`` que dejó el latido real del nodo plantilla."""
    connection = await migrated.connect()
    try:
        raw = await connection.fetchval(
            "SELECT payload_summary::text FROM fleet.heartbeat_history WHERE node_id = $1"
            " ORDER BY received_at DESC LIMIT 1",
            node_id,
        )
    finally:
        await connection.close()
    if raw is None:
        raise RuntimeError("el nodo plantilla no tiene latido en la historia")
    summary: dict[str, Any] = json.loads(raw)
    return summary


def _console(gob: GobPlatform, generated: GobGenerated, rounds: int) -> list[Timing]:
    """NFR-GOB-03 por HTTP sobre la organización mayor, como su administración (y el instalador
    del proveedor con su concesión en ``POST /documents``)."""
    zone = generated.catalog_zone.zone_id
    admin = {"cookie": generated.admin}
    installer = generated.installer_zone

    def document() -> dict[str, Any]:
        data = uuid.uuid4().bytes * 256
        return {
            "cookie": installer.installer,
            "concession": installer.concession,
            "json_body": {
                "plant_id": str(installer.plant_id),
                "kind": "plant_policy",
                "content_type": "application/pdf",
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            },
        }

    routes: list[tuple[str, str, str, str, Callable[[], dict[str, Any]]]] = [
        ("fleet_nodes", "GET /fleet/nodes de la organización mayor (60 nodos)", "GET",
         "/fleet/nodes", lambda: admin),
        ("fleet_node_detail", "GET /fleet/nodes/{node_id} con 90 días de latidos", "GET",
         f"/fleet/nodes/{generated.detail_node}", lambda: admin),
        ("walk_test_current", "GET /zones/{zone_id}/walk-tests/current de la matriz del núcleo",
         "GET", f"/zones/{generated.walk_zone_id}/walk-tests/current", lambda: admin),
        ("zone_catalog", "GET /zones/{zone_id}/catalog con 24 meses de versiones", "GET",
         f"/zones/{zone}/catalog", lambda: admin),
        ("catalog_versions", "GET /zones/{zone_id}/catalog/versions con 24 meses", "GET",
         f"/zones/{zone}/catalog/versions", lambda: admin),
        ("zone_gates", "GET /zones/{zone_id}/gates con 24 meses de historia", "GET",
         f"/zones/{zone}/gates", lambda: admin),
        ("zone_transparency", "GET /zones/{zone_id}/transparency con 24 meses", "GET",
         f"/zones/{zone}/transparency", lambda: admin),
        ("commissioning_record", "GET /commissioning-records/{record_id} (acta del núcleo)",
         "GET", f"/commissioning-records/{generated.record_id}", lambda: admin),
        ("documents", "POST /documents (concesión de un documento firmado)", "POST",
         "/documents", document),
    ]  # fmt: skip
    timings: list[Timing] = []
    for key, label, method, path, options in routes:
        statuses: Counter[int] = Counter()

        async def call(
            method: str = method,
            path: str = path,
            options: Callable[[], dict[str, Any]] = options,
            statuses: Counter[int] = statuses,
        ) -> None:
            response = await gob.send(method, path, **options())
            statuses[response.status_code] += 1

        name = f"gob_console_{key}"
        timing = _timed(
            gob.run,
            name,
            label,
            call,
            rounds,
            GOB_OBJECTIVES_MS[name],
            before=lambda: gob.advance(PERSON_SPACING_SECONDS),
            timed_out=lambda statuses=statuses: UNAVAILABLE in statuses,
        )
        timing.details["statuses"] = {str(code): count for code, count in statuses.items()}
        timings.append(timing)
        # 503 es el tope de la base agotado: se declara (objetivo no cumplido). Cualquier otro
        # estado fuera de 2xx es un fallo de la generación o de la prueba.
        if any(code >= 300 and code != UNAVAILABLE for code in statuses):
            raise RuntimeError(f"{name} respondió {dict(statuses)}")
    return timings


def _ports(gob: GobPlatform, generated: GobGenerated, rounds: int) -> list[Timing]:
    """NFR-GOB-04 sobre los 24 meses de la organización mayor, como ``vigia_app``."""
    ports = catalog_query_ports(gob.services.database)
    context = PortsWorld.context(generated.organization_id)
    rng = random.Random(11)  # noqa: S311 - instantes de prueba
    zones = generated.zones
    span = (generated.now - generated.history_from).total_seconds()

    def instant() -> datetime:
        return generated.history_from + timedelta(seconds=rng.uniform(3 * 86_400, span - 60))

    def zone() -> CatalogZone:
        return rng.choice(zones)

    def ref() -> StandardRef:
        chosen = zone()
        return StandardRef(chosen.zone_id, rng.choice(chosen.standards), instant())

    year = timedelta(days=366)

    def standard_at() -> Awaitable[object]:
        chosen = ref()
        return ports.catalog.standard_at(context, chosen.zone_id, chosen.standard_id, chosen.at)

    operations: list[tuple[str, str, float | None, Callable[[], Awaitable[object]]]] = [
        # El suelo: una lectura vacía por el mismo camino (contexto, SET LOCAL y READ ONLY). Lo
        # que una operación puntual tarde por encima es su consulta; sin objetivo.
        ("floor_select_1", "Lectura vacía (SELECT 1) con el contexto de la sesión", None,
         lambda: gob.services.database.read(context, text("SELECT 1"))),
        ("current_catalog", "Catálogo vigente de una zona", POINT_MS,
         lambda: ports.catalog.current_catalog(context, zone().zone_id)),
        ("catalog_at", "Catálogo vigente en un instante de los 24 meses", POINT_MS,
         lambda: ports.catalog.catalog_at(context, zone().zone_id, instant())),
        ("standard_at", "Versión de un estándar vigente en un instante", POINT_MS,
         standard_at),
        ("catalog_history", "Historia del catálogo (una página)", BATCH_MS,
         lambda: ports.catalog.catalog_history(context, zone().zone_id)),
        ("single_occupancy", "Marca unipersonal de una zona en un instante", POINT_MS,
         lambda: ports.catalog.single_occupancy(context, zone().zone_id, instant())),
        ("single_occupancy_many", "Marca unipersonal de hasta 50 zonas en un instante", BATCH_MS,
         lambda: ports.catalog.single_occupancy_many(
             context, [z.zone_id for z in rng.sample(zones, min(50, len(zones)))], instant())),
        ("standards_at_many", "200 referencias de estándar en los 24 meses", BATCH_MS,
         lambda: ports.catalog.standards_at_many(context, [ref() for _ in range(200)])),
        ("state", "Estado de compuertas de una zona", POINT_MS,
         lambda: ports.gates.state(context, zone().zone_id)),
        ("states_by_plant", "Compuertas de las zonas de una planta", BATCH_MS,
         lambda: ports.gates.states_by_plant(context, rng.choice(generated.plants))),
        ("state_at", "Compuerta de uso en un instante, desde la historia", POINT_MS,
         lambda: ports.gates.state_at(context, zone().zone_id, GateKind.USAGE, instant())),
        ("gate_history", "Historia de las dos compuertas en 366 días de los 24 meses", BATCH_MS,
         lambda: ports.gates.gate_history(
             context, zone().zone_id, (start := instant() - year / 2), start + year)),
        ("plant_policy", "Política vigente de una planta", POINT_MS,
         lambda: ports.gates.plant_policy(context, rng.choice(generated.plants))),
        ("current_agreement", "Acuerdo vigente de una zona con sus firmantes", POINT_MS,
         lambda: ports.gates.current_agreement(context, zone().zone_id)),
    ]  # fmt: skip
    return [
        _timed(gob.run, f"gob_ports_{key}", f"{label} (NFR-GOB-04)", call, rounds, objective)
        for key, label, objective, call in operations
    ]


def _gate_history_24_months(gob: GobPlatform, generated: GobGenerated) -> dict[str, Any]:
    """``gate_history`` de 366 días que empieza en el primer mes de la historia (hace 24 meses en
    la escala objetivo): devuelve intervalos completos y contiguos de las dos compuertas."""
    ports = catalog_query_ports(gob.services.database)
    context = PortsWorld.context(generated.organization_id)
    zone = generated.catalog_zone.zone_id
    start = generated.history_from + timedelta(days=2)
    end = start + timedelta(days=366)
    started = time.perf_counter()  # noqa: TID251 - la volumetría mide tiempo real.
    intervals = gob.run(ports.gates.gate_history(context, zone, start, end))
    elapsed = (time.perf_counter() - started) * 1000  # noqa: TID251
    by_gate: dict[str, list[Any]] = {}
    for interval in intervals:
        by_gate.setdefault(str(interval.gate), []).append(interval)
    complete = len(by_gate) == 2
    for chain in by_gate.values():
        chain.sort(key=lambda item: item.effective_from)
        complete &= chain[0].effective_from <= start
        complete &= chain[-1].effective_until is None or chain[-1].effective_until >= end
        complete &= all(a.effective_until == b.effective_from for a, b in itertools.pairwise(chain))
    if not complete:
        raise RuntimeError(f"gate_history de 366 días incompleto: {intervals}")
    return {
        "zone_id": str(zone),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "months_ago": round((generated.now - start).days / MONTH_DAYS, 1),
        "intervals": {gate: len(chain) for gate, chain in sorted(by_gate.items())},
        "complete": complete,
        "elapsed_ms": round(elapsed, 2),
        "objective_ms": BATCH_MS,
    }


def _budget(generated: GobGenerated, gob_scale: GobScale) -> dict[str, Any]:
    """Métricas de presupuesto de NFR-GOB-07 medidas sobre lo generado (``[estimación propia]``),
    por día y extrapoladas a la escala objetivo (100 nodos, 300 zonas)."""
    rates = gob_scale.rates
    written = generated.written
    heartbeat_rows = written["heartbeat_history"]
    nodes = heartbeat_rows / (rates.heartbeat_days * 86_400 / rates.heartbeat_seconds)
    grant_rows = written["clip_upload_grant"]
    grant_zones = grant_rows / (rates.grant_days * rates.grants_per_zone_day)
    catalog_days = rates.history_months * MONTH_DAYS
    catalog_zones = len(generated.zones)
    return {
        "fleet_heartbeat_rows_per_day": round(heartbeat_rows / rates.heartbeat_days),
        "fleet_heartbeat_rows_per_day_at_100_nodes": round(
            heartbeat_rows / rates.heartbeat_days / nodes * 100
        ),
        "fleet_heartbeat_rows_in_window": heartbeat_rows,
        "fleet_heartbeat_bytes_per_row": round(generated.heartbeat_bytes / max(heartbeat_rows, 1)),
        "fleet_heartbeat_bytes_at_100_nodes_90_days": round(
            generated.heartbeat_bytes / nodes * 100 * 90 / rates.heartbeat_days
        ),
        "fleet_upload_grants_per_day": round(grant_rows / rates.grant_days),
        "fleet_upload_grants_per_day_at_300_zones": round(
            grant_rows / rates.grant_days / grant_zones * 300
        ),
        "fleet_upload_grant_bytes_per_row": round(generated.grant_bytes / max(grant_rows, 1)),
        "catalog_versions_per_day": round(written["catalog_versions"] / catalog_days, 2),
        "catalog_versions_per_day_at_300_zones": round(
            written["catalog_versions"] / catalog_days / catalog_zones * 300, 2
        ),
        "enrollment_attempts_per_year": round(
            written["enrollment_attempt"] / rates.history_months * 12
        ),
        "fleet_alarms_per_year": round(written["fleet_alarm"] / rates.history_months * 12),
        "note": (
            "Volumen de las tablas de U-03 a los ritmos de GobRates [estimación propia]: un latido "
            "por minuto y nodo, una concesión por episodio (77 por zona y día) y una versión del "
            "catálogo por semana y zona. El volumen real en bytes de los clips es "
            "TBD-por-medición (R5 de U-01)."
        ),
    }


def run_gob(
    postgres: PostgresEndpoint,
    localstack: LocalStackEndpoint,
    scale: Scale,
    *,
    seed: int,
    workers: int,
) -> dict[str, Any]:
    """La volumetría de U-03: genera sobre ``GobPlatform`` y mide; devuelve su sección del
    informe (``u03``). Lo medido sobrevive a un fallo posterior en ``partial``."""
    gob_scale = GOB_SCALES[scale.name]
    workers = max(1, min(workers, MAX_CONNECTIONS))
    with gob_platform(postgres, localstack, f"volumetry_gob_{scale.name}") as gob:
        _log("U-03: núcleo por las rutas reales (acta, walk-test, plantilla del inventario)")
        core = _gob_core(gob, gob_scale)
        organization = core[1].organization_id
        signers = [(role, gob.authz.add_user(organization))
                   for role in ("coordinator_sst", "plant_manager", "copasst")]  # fmt: skip
        generated = gob.run(_gob_bulk(gob, scale, gob_scale, seed, workers, core, signers))
        _log(f"U-03 generado en {generated.seconds:.0f} s: {dict(generated.written)}")
        section: dict[str, Any] = {
            "scale": {
                "largest_organization": {
                    "plants": 1 + gob_scale.extra_plants,
                    "zones": 3 + gob_scale.extra_plants * gob_scale.zones_per_plant,
                    "ledger_days": gob_scale.ledger_days,
                },
                "active_nodes": 3 + len(generated.zones) + gob_scale.other_active_nodes,
                "rates": asdict(gob_scale.rates),
                "ledger_rates_per_zone_day": asdict(gob_scale.ledger_rates),
            },
            "generation_seconds": round(generated.seconds),
            "written": dict(generated.written),
            "nfr_gob_07": _budget(generated, gob_scale),
        }
        _log("U-03: NFR-GOB-03 (rutas de consola) sobre la organización mayor")
        section["nfr_gob_03"] = [asdict(t) for t in _console(gob, generated, gob_scale.rounds)]
        _log("U-03: NFR-GOB-04 (puertos) sobre 24 meses")
        section["nfr_gob_04"] = [asdict(t) for t in _ports(gob, generated, gob_scale.rounds)]
        section["gate_history_366_days"] = _gate_history_24_months(gob, generated)
    return section


def _endpoint() -> Iterator[PostgresEndpoint]:
    """PostgreSQL de la sesión, con reintentos si el contenedor tarda en publicar su puerto."""
    for attempt in range(1, CONTAINER_ATTEMPTS + 1):
        generator = cast(Generator[PostgresEndpoint], postgres_endpoint_session())
        try:
            endpoint = next(generator)
        except pytest.fail.Exception:
            if attempt == CONTAINER_ATTEMPTS:
                raise
            _log(f"el contenedor no arrancó (intento {attempt}); reintento")
            continue
        try:
            yield endpoint
        finally:
            generator.close()
        return


def _summary(report: dict[str, Any]) -> None:
    growth = report["nfr_nuc_03"]
    print(f"escala {report['scale']['name']}: {report['scale']}")
    print(f"generación: {report['generation_seconds']} s; escritos: {report['written']}")
    print(
        "NFR-NUC-03: ledger_bytes_per_day={ledger_bytes_per_day} ({ledger_records_per_day}"
        " registros/día, {ledger_bytes_per_record} B/registro; a 300 zonas"
        " {ledger_bytes_per_day_at_300_zones} B/día), audit_entries_per_day="
        "{audit_entries_per_day} ({audit_bytes_per_entry} B/entrada)".format(**growth)
    )
    for timing in report["nfr_nuc_01"]:
        verdict = ""
        if timing["objective_ms"] is not None:
            verdict = " cumple" if timing["objective_met"] else " NO cumple"
            verdict = f" (objetivo p95 {timing['objective_ms']:.0f} ms:{verdict})"
        if timing["details"].get("timed_out"):
            measured = "agotó el statement_timeout de 30 s"
        elif timing["details"].get("unavailable"):
            measured = "sin medida: la base dejó de estar disponible"
        else:
            measured = f"mediana {timing['median_ms']} ms, p95 {timing['p95_ms']} ms"
        print(f"NFR-NUC-01 {timing['name']}: {measured}{verdict}")
    verification = report["verification"]
    print(
        f"NFR-NUC-01 verificación incremental: {verification['records']} registros,"
        f" {verification['records_per_second']} registros/s"
        f" (objetivo {verification['objective_per_second']}:"
        f" {'cumple' if verification['objective_met'] else 'NO cumple'})"
    )


def _localstack() -> Iterator[LocalStackEndpoint]:
    """LocalStack de la sesión (solo la parte de U-03 lo necesita)."""
    generator = cast(Generator[LocalStackEndpoint], localstack_endpoint_session())
    try:
        yield next(generator)
    finally:
        generator.close()


def _gob_summary(section: dict[str, Any]) -> None:
    print(f"U-03: generación {section['generation_seconds']} s; escritos: {section['written']}")
    print(f"NFR-GOB-07: {json.dumps(section['nfr_gob_07'], ensure_ascii=False)}")
    for group in ("nfr_gob_03", "nfr_gob_04"):
        for timing in section[group]:
            verdict = ""
            if timing["objective_ms"] is not None:
                verdict = " cumple" if timing["objective_met"] else " NO cumple"
                verdict = f" (objetivo p95 {timing['objective_ms']:.0f} ms:{verdict})"
            measured = f"mediana {timing['median_ms']} ms, p95 {timing['p95_ms']} ms"
            if timing["details"].get("timed_out"):
                measured = "agotó el tope de la base (temporarily_unavailable)"
            print(f"{group.upper().replace('_', '-')} {timing['name']}: {measured}{verdict}")
    history = section["gate_history_366_days"]
    print(
        f"NFR-GOB-16 gate_history de 366 días desde hace {history['months_ago']} meses:"
        f" intervalos {history['intervals']}, completo={history['complete']},"
        f" {history['elapsed_ms']} ms"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scale", choices=sorted(SCALES), default="target")
    parser.add_argument("--seed", type=int, default=20261002)
    parser.add_argument("--workers", type=int, default=MAX_CONNECTIONS)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--part",
        choices=("all", "nuc", "gob"),
        default="all",
        help="nuc: U-02 (NFR-NUC-05); gob: U-03 (NFR-GOB-11); all: las dos, una tras otra",
    )
    args = parser.parse_args(argv)
    scale = SCALES[args.scale]
    out = args.out or BACKEND / ".benchmarks" / f"volumetry-{scale.name}.json"
    for endpoint in _endpoint():
        if args.part in {"all", "nuc"}:
            report = run(endpoint, scale, seed=args.seed, workers=args.workers, out=out)
            _summary(report)
        else:
            report = {
                "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),  # noqa: TID251
                "scale": {"name": scale.name},
                "seed": args.seed,
            }
        if args.part in {"all", "gob"}:
            # La base de U-02 ya se borró: la de U-03 usa el mismo disco, no el doble.
            for localstack in _localstack():
                try:
                    report["u03"] = run_gob(
                        endpoint, localstack, scale, seed=args.seed, workers=args.workers
                    )
                except Exception as error:
                    report["u03"] = {"error": f"{type(error).__name__}: {error}"}
                    _write(out, report)
                    raise
            _write(out, report)
            _gob_summary(report["u03"])
        print(f"informe: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
