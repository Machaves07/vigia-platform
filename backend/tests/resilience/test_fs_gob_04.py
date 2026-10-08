"""FS-GOB-04 · Cadena de planta retenida durante una ingesta (NFR-GOB-02, 42; PR-GOB-01;
PAT-NUC-RES-08; PAT-GOB-RES-03, LC-GOB-12).

Sobre la aplicación completa (``gob_platform``) con el ``lock_timeout`` de producción de la ruta
del nodo (2 s, NFR-NUC-36): **20 nodos** dados de alta, cada uno con su zona productiva en **la
misma planta** (una sola cadena), envían sus hallazgos a la vez por ``POST /api/nodes/findings``
con su certificado. Cada nodo simulado tiene su cola local: ante un rechazo transitorio
**reencola** y reintenta con la misma ``Idempotency-Key`` (como el nodo de U-01, H-13).

**Inyección**: a mitad de la carga (el instante sale de la semilla), una transacción inyectada en
otra conexión **retiene la fila de ``ChainHead`` de la planta 5 s** (``SELECT … FOR UPDATE``, como
un escritor colgado dentro de su transacción).

**Resultado esperado**:

- las escrituras que esperan la cabeza más allá del ``lock_timeout`` terminan en
  ``chain_locked_timeout``, que el nodo recibe como ``temporarily_unavailable`` (503,
  ``retry_after_seconds`` entre 1 y 60): **transitorio**;
- **U-03 no reintenta por su cuenta**: cada petición llega al escritor exactamente una vez, y cada
  503 corresponde a un ``chain_locked_timeout`` del escritor;
- el reintento del nodo con la misma clave produce ``accepted`` al liberarse la cabeza;
- **ninguna secuencia perdida ni repetida**: cada hallazgo está una sola vez y la cadena de la
  planta es íntegra y contigua (PR-NUC-13, ``verify_ledger_chains``).

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import random
from collections import Counter, deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Final

import pytest

from tests.gob_platform_support import GobPlatform, GobZone, Onboarding, gob_platform
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint
from tests.resilience.gob_support import productive_zones
from tests.resilience.harness import WALL, scenario
from tests.writer_support import verify_ledger_chains
from vigia_platform.ledger.application.writer import LedgerRejection, LedgerRejectionCode
from vigia_platform.shared.api.declarations import NodeRoute

pytestmark = [pytest.mark.integration, pytest.mark.nightly]

NODES: Final = 20
FINDINGS_PER_NODE: Final = 3
HOLD_SECONDS: Final = 5.0
LOCK_TIMEOUT_MS: Final = 2_000
"""``lock_timeout`` de producción de ``vigia-api`` (NFR-NUC-36): el asunto del escenario."""
DRAIN_SECONDS: Final = 300.0
"""Tope real del vaciado (nunca decide un resultado: lo que no entra es pérdida y falla)."""


@pytest.fixture(scope="module")
def gob(
    postgres_endpoint: PostgresEndpoint, localstack_endpoint: LocalStackEndpoint
) -> Iterator[GobPlatform]:
    with gob_platform(
        postgres_endpoint,
        localstack_endpoint,
        "fs_gob_04",
        database_changes={"lock_timeout_ms": LOCK_TIMEOUT_MS, "worker_pool_size": NODES + 8},
    ) as platform:
        yield platform


@dataclass
class Node:
    """Un nodo simulado con su cola local (reencola lo transitorio, como el de U-01)."""

    zone: GobZone
    queue: deque[dict[str, Any]]
    rng: random.Random
    sent: int = 0
    accepted: dict[str, str] = field(default_factory=dict)
    transient: list[dict[str, Any]] = field(default_factory=list)
    permanent: dict[str, Any] = field(default_factory=dict)


async def _drain(flow: Onboarding, node: Node) -> None:
    while node.queue:
        document = node.queue.popleft()
        node.sent += 1
        flow.gob.advance(0.001)
        response = await flow.submit(node.zone, NodeRoute.FINDING, document, "finding_id")
        body = response.json()
        if response.status_code == 200:
            node.accepted[document["finding_id"]] = body["status"]
            continue
        if response.status_code == 503 and body.get("retryable") is True:
            node.transient.append(
                {"code": body["code"], "retry_after_seconds": body["retry_after_seconds"]}
            )
            node.queue.append(document)
            await asyncio.sleep(node.rng.uniform(0.05, 0.5))
            continue
        node.permanent[document["finding_id"]] = body


async def _hold_chain_head(gob: GobPlatform, zone: GobZone, seconds: float) -> float:
    connection = await gob.env.authz.sessions.migrated.connect()
    try:
        async with connection.transaction():
            started = WALL.monotonic()
            row = await connection.fetchrow(
                "SELECT last_sequence FROM ledger.chain_head WHERE organization_id = $1"
                " AND kind = 'ledger' AND plant_id = $2 FOR UPDATE",
                zone.organization_id,
                zone.plant_id,
            )
            assert row is not None, "la cadena de la planta ya existe"
            await asyncio.sleep(seconds)
            return WALL.monotonic() - started
    finally:
        await connection.close()


def test_fs_gob_04_plant_chain_held_during_an_ingest_of_twenty_nodes(gob: GobPlatform) -> None:
    with scenario(
        "FS-GOB-04",
        title="Cadena de planta retenida durante una ingesta",
        injection=(
            f"transacción que retiene ChainHead de la planta {HOLD_SECONDS:.0f} s mientras"
            f" {NODES} nodos simulados envían"
        ),
        expected=(
            "chain_locked_timeout transitorio sin reintento propio de U-03; el reintento del nodo"
            " con la misma clave da accepted; ninguna secuencia perdida ni repetida"
        ),
    ) as run:
        flow = Onboarding(gob)
        zones = productive_zones(flow, NODES)
        first = zones[0]
        nodes = [
            Node(zone, deque(flow.finding(zone) for _ in range(FINDINGS_PER_NODE)), run.child())
            for zone in zones
        ]
        total = NODES * FINDINGS_PER_NODE
        hold_after = int(total * run.random.uniform(0.1, 0.4))
        run.observe(nodes=NODES, findings=total, hold_after_accepted=hold_after)

        writes: list[Any] = []
        writer = gob.writer
        original = writer.write

        async def counted(*arguments: Any, **options: Any) -> Any:
            outcome = await original(*arguments, **options)
            writes.append(outcome)
            return outcome

        writer.write = counted  # type: ignore[method-assign]

        async def drive() -> float:
            tasks = [asyncio.create_task(_drain(flow, node)) for node in nodes]
            while sum(len(node.accepted) for node in nodes) < max(1, hold_after):
                await asyncio.sleep(0.01)
            held = await _hold_chain_head(gob, first, HOLD_SECONDS)
            async with asyncio.timeout(DRAIN_SECONDS):
                await asyncio.gather(*tasks)
            return held

        try:
            held = gob.run(drive())
        finally:
            writer.write = original  # type: ignore[method-assign]

        sent = sum(node.sent for node in nodes)
        transient = [item for node in nodes for item in node.transient]
        locked = [
            outcome
            for outcome in writes
            if isinstance(outcome, LedgerRejection)
            and outcome.code is LedgerRejectionCode.CHAIN_LOCKED_TIMEOUT
        ]
        statuses = Counter(status for node in nodes for status in node.accepted.values())
        rows = gob.records(first.organization_id, "finding_received")
        keys = Counter(str(row["source_key"]) for row in rows)
        lengths = gob.run(
            verify_ledger_chains(gob.env.authz.sessions.migrated, first.organization_id)
        )
        (chain,) = gob.fetch(
            "SELECT count(*) AS records, max(chain_sequence) AS last FROM ledger.ledger_record"
            " WHERE organization_id = $1 AND plant_id = $2",
            first.organization_id,
            first.plant_id,
        )
        run.observe(
            held_seconds=round(held, 2),
            requests=sent,
            writer_calls=len(writes),
            transient={
                str(code): count
                for code, count in Counter(str(item["code"]) for item in transient).items()
            },
            chain_locked_timeout=len(locked),
            retry_after_seconds=sorted({item["retry_after_seconds"] for item in transient}),
            statuses=dict(statuses),
            permanent=len([1 for node in nodes for _ in node.permanent]),
            stored_findings=len(rows),
            chain_length=lengths.get(first.plant_id),
            chain_records=chain["records"],
        )

        assert all(not node.permanent for node in nodes), "ningún rechazo permanente"
        assert sum(len(node.accepted) for node in nodes) == total
        assert locked, "la cabeza retenida produce chain_locked_timeout"
        # Transitorio hacia el nodo, y sin reintento propio de U-03: una escritura por petición.
        assert {item["code"] for item in transient} == {"temporarily_unavailable"}
        assert all(1 <= item["retry_after_seconds"] <= 60 for item in transient)
        assert len(writes) == sent
        assert len(locked) == len(transient)
        # El reintento del nodo con la misma clave entra: accepted, una vez cada hallazgo.
        assert statuses["accepted"] + statuses["accepted_duplicate"] == total
        assert len(keys) == total and max(keys.values()) == 1
        # Ninguna secuencia perdida ni repetida: la cadena de la planta, íntegra y contigua.
        assert lengths[first.plant_id] == chain["records"] == chain["last"]
