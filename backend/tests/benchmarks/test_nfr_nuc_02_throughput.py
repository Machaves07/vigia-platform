"""Caudal sostenido por el puerto de escritura (NFR-NUC-02; TASK-142, VIG-91).

Generador de carga **por el puerto** ``EscritorExpediente.write`` (la carga por HTTP con el nodo
simulado la añade U-03), sobre PostgreSQL 16 en contenedor como ``vigia_app``, con el pool de
nodos de una instancia de la API (10 conexiones) y el ``lock_timeout`` de producción (2 s). Cada
escritura es un ``order_probe`` sin evidencias que recorre los siete pasos y publica un evento.

- **Agregado**: 40 escritores concurrentes reparten sus escrituras entre 20 cadenas de planta
  durante 20 s: ≥ 200 escrituras por segundo.
- **Una cadena**: 10 escritores concurrentes sobre la misma planta durante 20 s (todas compiten
  por la cabeza de la cadena): ≥ 20 escrituras por segundo.
- En los dos, ``chain_locked_timeout`` en a lo sumo el 1 % de las escrituras, y ningún otro fallo.
  Al final, las cadenas de la organización son íntegras (secuencias contiguas, hashes recalculados
  y cabezas) con el oráculo de ``tests/writer_support.py``.

Las tasas van al informe de los bancos y se comparan con la línea base (una caída de más del 50 %
falla). Solo datos generados. Solo corre con ``--hypothesis-profile=nightly``.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

import pytest

from tests.benchmarks.conftest import Report, Result
from tests.identity_db import migrated_database
from tests.integration.conftest import PostgresEndpoint
from tests.writer_support import (
    ORDER_TYPE,
    Place,
    WriterEnvironment,
    order_document,
    unit_context,
    verify_ledger_chains,
    writer_environment,
)
from vigia_platform.ledger.application.writer import Receipt
from vigia_platform.shared.context import ActorKind, ActorUnit
from vigia_platform.shared.db import ChainLockedTimeout

pytestmark = [pytest.mark.nightly, pytest.mark.integration]

POOL_SIZE = 10
"""``node_pool_size`` de ``DatabaseSettings``: el pool de los nodos de una instancia."""
LOCK_TIMEOUT_MS = 2_000
SECONDS = 20.0
AGGREGATE_TARGET = 200.0
CHAIN_TARGET = 20.0
TIMEOUT_CEILING = 0.01


@dataclass(frozen=True)
class Load:
    accepted: int
    timeouts: int
    failures: Counter[str]
    seconds: float

    @property
    def rate(self) -> float:
        return self.accepted / self.seconds

    @property
    def timeout_ratio(self) -> float:
        attempts = self.accepted + self.timeouts + sum(self.failures.values())
        return self.timeouts / attempts if attempts else 0.0


@pytest.fixture(scope="module")
def environment(postgres_endpoint: PostgresEndpoint) -> Iterator[WriterEnvironment]:
    with (
        migrated_database(postgres_endpoint, "bench_throughput") as migrated,
        writer_environment(
            migrated, pool_size=POOL_SIZE, lock_timeout_ms=LOCK_TIMEOUT_MS
        ) as environment,
    ):
        yield environment


async def _load(
    environment: WriterEnvironment, places: Sequence[Place], writers: int, seconds: float
) -> Load:
    context = unit_context(places[0].organization_id, ActorUnit.U03, kind=ActorKind.SYSTEM)
    accepted = timeouts = 0
    failures: Counter[str] = Counter()
    started = time.perf_counter()  # noqa: TID251 - el banco mide tiempo real a propósito.
    deadline = started + seconds

    async def writer(index: int) -> None:
        nonlocal accepted, timeouts
        turn = index
        while time.perf_counter() < deadline:  # noqa: TID251 - tiempo real del banco.
            place = places[turn % len(places)]
            turn += writers
            try:
                receipt = await environment.writer.write(
                    context, ORDER_TYPE, order_document(place, clips=0)
                )
            except ChainLockedTimeout:
                timeouts += 1
                continue
            except Exception as error:  # el banco cuenta todo fallo y la prueba lo exige en cero
                failures[type(error).__name__] += 1
                continue
            if isinstance(receipt, Receipt):
                accepted += 1
            else:
                failures[str(receipt.code)] += 1

    await asyncio.gather(*(writer(index) for index in range(writers)))
    elapsed = time.perf_counter() - started  # noqa: TID251 - tiempo real del banco.
    return Load(accepted, timeouts, failures, elapsed)


def _report(report: Report, name: str, label: str, load: Load, target: float) -> Result:
    result = report.add(
        Result(
            name=name,
            label=label,
            kind="rate",
            unit="escrituras/s",
            median=load.rate,
            p95=None,
            samples=load.accepted,
            objective=target,
            objective_met=load.rate >= target and load.timeout_ratio <= TIMEOUT_CEILING,
            details={
                "accepted": load.accepted,
                "chain_locked_timeout": load.timeouts,
                "chain_locked_timeout_ratio": round(load.timeout_ratio, 5),
                "failures": dict(load.failures),
                "seconds": round(load.seconds, 2),
                "pool_size": POOL_SIZE,
                "lock_timeout_ms": LOCK_TIMEOUT_MS,
            },
        )
    )
    return result


def test_aggregate_and_single_chain_throughput(
    environment: WriterEnvironment,
    benchmark_report: Report,
    capsys: pytest.CaptureFixture[str],
) -> None:
    organization_id = uuid.uuid4()
    places = [Place.new(organization_id) for _ in range(20)]
    environment.loop.run(_load(environment, places, writers=8, seconds=2.0))  # calentamiento

    aggregate = environment.loop.run(_load(environment, places, writers=40, seconds=SECONDS))
    single = environment.loop.run(_load(environment, places[:1], writers=10, seconds=SECONDS))
    results = [
        _report(
            benchmark_report,
            "ledger_write_throughput",
            "Escrituras por segundo agregadas (20 cadenas, 40 escritores)",
            aggregate,
            AGGREGATE_TARGET,
        ),
        _report(
            benchmark_report,
            "ledger_write_throughput_one_chain",
            "Escrituras por segundo sobre una misma cadena (10 escritores)",
            single,
            CHAIN_TARGET,
        ),
    ]
    with capsys.disabled():
        for name, load in (("agregado", aggregate), ("una cadena", single)):
            print(
                f"\ncaudal {name}: {load.rate:,.1f} escrituras/s ({load.accepted} en"
                f" {load.seconds:.1f} s); chain_locked_timeout {load.timeouts}"
                f" ({load.timeout_ratio:.2%}); otros fallos {dict(load.failures)}"
            )
    lengths = environment.loop.run(verify_ledger_chains(environment.migrated, organization_id))
    assert sum(lengths.values()) >= aggregate.accepted + single.accepted

    for load in (aggregate, single):
        assert not load.failures, load.failures
        assert load.timeout_ratio <= TIMEOUT_CEILING, load
    assert aggregate.rate >= AGGREGATE_TARGET, aggregate
    assert single.rate >= CHAIN_TARGET, single
    for result in results:
        benchmark_report.check(result)
