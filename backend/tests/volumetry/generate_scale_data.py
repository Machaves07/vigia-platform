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

El informe JSON (``--out``, por defecto ``.benchmarks/volumetry-<escala>.json``) es el artefacto de
``nightly``. El script termina con error si la generación o la verificación de la cadena fallan;
un objetivo no cumplido se declara en el informe y no cambia el código de salida.

Solo datos generados. Usa como mucho 3 conexiones de escritura (regla de recursos compartidos).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import random
import sys
import time
from collections.abc import Awaitable, Callable, Generator, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast

BACKEND: Final = Path(__file__).resolve().parents[2]
if str(BACKEND) not in sys.path:  # ejecutado como script: el paquete ``tests`` está en backend/
    sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

from tests.identity_db import MigratedDatabase, migrated_database  # noqa: E402
from tests.integration.conftest import PostgresEndpoint, postgres_endpoint_session  # noqa: E402
from tests.integration.test_coverage_port import COVERAGE_TYPES, scoped  # noqa: E402
from tests.verify_support import CHECKPOINT_KEY, StaticKeys  # noqa: E402
from tests.volumetry.scale_data import (  # noqa: E402
    OrganizationRef,
    PlantRef,
    Rates,
    ScaleWriter,
    create_partitions,
    days_back,
    historical_stamps,
    kit_findings,
    seed_organization,
)
from tests.writer_support import (  # noqa: E402
    ORDER_TYPE,
    Place,
    WriterEnvironment,
    order_document,
    unit_context,
    writer_environment,
)
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
    median_ms: float
    p95_ms: float
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
    """``rounds`` ejecuciones (con 2 de calentamiento) medidas con el reloj de alta resolución."""
    for _ in range(2):
        env.loop.run(operation())
    times: list[float] = []
    for _ in range(rounds):
        started = time.perf_counter()  # noqa: TID251 - la volumetría mide tiempo real.
        env.loop.run(operation())
        times.append((time.perf_counter() - started) * 1000)  # noqa: TID251
    objective = OBJECTIVES_MS.get(name)
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
        await create_partitions(connection, first_day, now)
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


def _measure(env: WriterEnvironment, generated: Generated, scale: Scale) -> list[Timing]:
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
    timings = [
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
    return timings


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
    """Genera, mide y escribe el informe en ``out``; devuelve el informe."""
    workers = max(1, min(workers, MAX_CONNECTIONS))
    with (
        migrated_database(endpoint, f"volumetry_{scale.name}") as migrated,
        writer_environment(migrated, extra_types=COVERAGE_TYPES) as env,
    ):
        generated = env.loop.run(_generate(migrated, scale, seed, workers))
        total = sum(generated.written_total.values())
        _log(f"generados {total} registros y entradas en {generated.seconds:.0f} s")
        _log("midiendo NFR-NUC-01 sobre la organización mayor")
        timings = _measure(env, generated, scale)
        verification = _verify(env, generated)
    report = {
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
        "nfr_nuc_03": _growth(generated, scale),
        "nfr_nuc_01": [asdict(timing) for timing in timings],
        "verification": verification,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


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
        print(
            f"NFR-NUC-01 {timing['name']}: mediana {timing['median_ms']} ms,"
            f" p95 {timing['p95_ms']} ms{verdict}"
        )
    verification = report["verification"]
    print(
        f"NFR-NUC-01 verificación incremental: {verification['records']} registros,"
        f" {verification['records_per_second']} registros/s"
        f" (objetivo {verification['objective_per_second']}:"
        f" {'cumple' if verification['objective_met'] else 'NO cumple'})"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scale", choices=sorted(SCALES), default="target")
    parser.add_argument("--seed", type=int, default=20261002)
    parser.add_argument("--workers", type=int, default=MAX_CONNECTIONS)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    scale = SCALES[args.scale]
    out = args.out or BACKEND / ".benchmarks" / f"volumetry-{scale.name}.json"
    for endpoint in _endpoint():
        report = run(endpoint, scale, seed=args.seed, workers=args.workers, out=out)
        _summary(report)
        print(f"informe: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
