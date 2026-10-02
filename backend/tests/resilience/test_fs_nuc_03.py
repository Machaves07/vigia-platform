"""FS-NUC-03 · Cadena caliente (PR-NUC-13, PAT-NUC-RES-08; NFR-NUC-38).

**Inyección**: **20 escritores concurrentes** sobre la cadena de una sola planta (hallazgos del
kit de U-01 por ``EscritorExpediente``, base con los ajustes de producción de ``vigia-api``:
``lock_timeout`` de 2 s) y, a mitad de la carga (instante elegido con la semilla), una
transacción inyectada que **retiene la fila de ``ChainHead`` 5 s** (``SELECT … FOR UPDATE`` en
otra conexión, como un escritor colgado dentro de su transacción).

**Resultado esperado**: algunos ``chain_locked_timeout`` **transitorios** (cada escritor
reencola y reintenta); **ninguna secuencia perdida ni repetida** (la cadena tiene exactamente un
registro por hallazgo, secuencias contiguas y hashes recalculados, PR-NUC-13); **métrica de
contención** (``chain_locked_timeout_total`` cuenta cada espera agotada y ``chain_lock_wait_ms``
cada escritura que llegó a la cadena); y la condición de alarma ``chain_locked_timeout_rate`` de
NFR-NUC-38 (``chain_locked_timeout_total`` / ``ledger_writes_total``) **supera el 1 %**, así que
la alarma se dispara.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter, deque
from collections.abc import Iterator
from typing import Any, Final

import pytest
from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader

from tests.hibp_service import metric_total, metrics_with_reader
from tests.resilience.harness import WALL, scenario
from tests.resilience.load import SimulatedNode, all_clips, kit_findings
from tests.resilience.stack import LedgerStack, ledger_stack
from tests.writer_support import Place, unit_context, verify_ledger_chains
from vigia_platform.ledger.application.writer import LedgerRejectionCode
from vigia_platform.shared.context import ActorKind, ActorUnit
from vigia_platform.shared.db import RouteClass, route_class_scope
from vigia_platform.shared.observability.metrics import ALARM_CONDITIONS, MetricName

pytestmark = [pytest.mark.integration, pytest.mark.nightly]

WRITERS: Final = 20
FINDINGS_PER_WRITER: Final = 6
HOLD_SECONDS: Final = 5.0
ALARM_THRESHOLD: Final = 0.01
"""Umbral de la alarma ``chain_locked_timeout_rate`` (NFR-NUC-38: más del 1 %)."""


@pytest.fixture(scope="module")
def stack() -> Iterator[LedgerStack]:
    with ledger_stack("fs_nuc_03") as stack:
        yield stack


def _histogram(reader: InMemoryMetricReader, name: MetricName) -> tuple[int, float]:
    """(observaciones, máximo) de un histograma, sumando todos sus atributos."""
    count, maximum = 0, 0.0
    data = reader.get_metrics_data()
    if data is None:
        return count, maximum
    for resource in data.resource_metrics:
        for scope_metrics in resource.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name != name.value:
                    continue
                for point in metric.data.data_points:
                    if isinstance(point, HistogramDataPoint):
                        count += point.count
                        maximum = max(maximum, point.max)
    return count, maximum


async def _hold_chain_head(stack: LedgerStack, place: Place, seconds: float) -> float:
    """Retiene la fila de la cabeza de la cadena de ``place`` ``seconds`` segundos."""
    connection = await stack.migrated.connect()
    try:
        async with connection.transaction():
            started = WALL.monotonic()
            row = await connection.fetchrow(
                "SELECT last_sequence FROM ledger.chain_head WHERE organization_id = $1"
                " AND kind = 'ledger' AND plant_id = $2 FOR UPDATE",
                place.organization_id,
                place.plant_id,
            )
            assert row is not None, "la cadena ya existe cuando se retiene su cabeza"
            await asyncio.sleep(seconds)
            return WALL.monotonic() - started
    finally:
        await connection.close()


def test_fs_nuc_03_hot_chain_with_a_held_chain_head(stack: LedgerStack) -> None:
    with scenario(
        "FS-NUC-03",
        title="Cadena caliente",
        injection=(
            f"{WRITERS} escritores concurrentes sobre una cadena más una transacción inyectada"
            f" que retiene ChainHead {HOLD_SECONDS:.0f} s"
        ),
        expected=(
            "algunos chain_locked_timeout transitorios; ninguna secuencia perdida ni repetida;"
            " métrica de contención; la alarma de NFR-NUC-38 se dispara al superar el 1 %"
        ),
    ) as run:
        env = stack.env
        plant = Place.new()
        metrics, reader = metrics_with_reader()
        # Reloj real, como en producción: ``chain_lock_wait_ms`` mide tiempo de verdad.
        writer = stack.writer(stack.database(), metrics=metrics, clock=WALL)
        context = unit_context(plant.organization_id, ActorUnit.U03, kind=ActorKind.SYSTEM)
        writers: list[SimulatedNode] = []
        for index in range(WRITERS):
            place = Place(plant.organization_id, plant.plant_id, plant.zone_id, uuid.uuid4())
            documents = kit_findings(run.random.getrandbits(32), place, FINDINGS_PER_WRITER)
            for clip in all_clips(documents):
                env.storage.put(clip)
            writers.append(
                SimulatedNode(
                    name=f"escritor-{index:02d}",
                    context=context,
                    queue=deque(documents),
                    write=writer.write,
                    rng=run.child(),
                    backoff=(0.01, 0.1),
                )
            )
        total = WRITERS * FINDINGS_PER_WRITER
        hold_after = int(total * run.random.uniform(0.1, 0.4))
        run.observe(writers=WRITERS, findings=total, hold_after_accepted=hold_after)

        async def drive() -> dict[str, Any]:
            with route_class_scope(RouteClass.NODE):
                tasks = [asyncio.create_task(node.drain()) for node in writers]
                while sum(len(node.report.accepted) for node in writers) < max(1, hold_after):
                    await asyncio.sleep(0.01)
                held = await _hold_chain_head(stack, plant, HOLD_SECONDS)
                async with asyncio.timeout(300):
                    await asyncio.gather(*tasks)
                return {"held_seconds": round(held, 2)}

        timings = stack.run(drive())
        retries: Counter[str] = Counter()
        for node in writers:
            retries.update(node.report.retries)
        rejected = {k: v for node in writers for k, v in node.report.rejected.items()}
        accepted = sum(len(node.report.accepted) for node in writers)
        lengths = stack.run(verify_ledger_chains(stack.migrated, plant.organization_id))
        timeouts = metric_total(reader, MetricName.CHAIN_LOCKED_TIMEOUT_TOTAL)
        writes = metric_total(reader, MetricName.LEDGER_WRITES_TOTAL)
        waits, longest_wait = _histogram(reader, MetricName.CHAIN_LOCK_WAIT_MS)
        rate = timeouts / writes if writes else 0.0
        (condition,) = [c for c in ALARM_CONDITIONS if c.key == "chain_locked_timeout_rate"]
        run.observe(
            **timings,
            retries=dict(retries),
            rejected=rejected,
            chain_length=lengths.get(plant.plant_id),
            metrics={
                "chain_locked_timeout_total": timeouts,
                "ledger_writes_total": writes,
                "chain_lock_wait_ms_count": waits,
                "chain_lock_wait_ms_max": round(longest_wait, 1),
                "chain_locked_timeout_rate": round(rate, 4),
                "alarm_threshold": ALARM_THRESHOLD,
                "alarm_fires": rate > ALARM_THRESHOLD,
            },
        )

        locked = LedgerRejectionCode.CHAIN_LOCKED_TIMEOUT.value
        assert rejected == {}
        assert accepted == total
        assert retries[locked] > 0, "la cabeza retenida produce chain_locked_timeout transitorios"
        # Ninguna secuencia perdida ni repetida: verify_ledger_chains comprueba 1..n contiguas.
        assert lengths == {plant.plant_id: total}
        # La métrica de contención cuenta lo que vieron los escritores.
        assert timeouts == retries[locked]
        assert writes == total + timeouts
        assert waits == writes
        assert longest_wait >= 1_500, "una espera agotada dura lo que lock_timeout (2 s)"
        # La alarma de NFR-NUC-38: el cociente de la condición supera el 1 %.
        assert condition.metric is MetricName.CHAIN_LOCKED_TIMEOUT_TOTAL
        assert condition.denominator is MetricName.LEDGER_WRITES_TOTAL
        assert rate > ALARM_THRESHOLD
