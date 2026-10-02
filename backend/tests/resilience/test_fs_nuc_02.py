"""FS-NUC-02 · Conmutación de la base con la cola del nodo activa (PR-NUC-13, PAT-NUC-RES-01,
NFR-NUC-11).

**Inyección**: el contenedor de PostgreSQL del escenario se **reinicia** (parada rápida y
arranque, como una conmutación) mientras **20 nodos simulados** envían. Cada nodo tiene su cola
local con hallazgos de los **generadores del kit de U-01** (``finding`` sobre un
``zone_catalog``), los envía por ``EscritorExpediente`` sobre una base con los ajustes de
producción de ``vigia-api`` (pool ``node`` de 10 conexiones) y reencola lo transitorio
(``temporarily_unavailable``, también con resultado desconocido tras el ``COMMIT``, o
``chain_locked_timeout``). El momento del reinicio sale de la semilla.

**Resultado esperado**: al drenar las colas, **cero pérdidas** (cada hallazgo está una vez en el
expediente y su recibo final apunta a ese registro) y **cero duplicados** (ninguna
``source_key`` repetida: un reenvío tras un ``COMMIT`` incierto vuelve como
``accepted_duplicate``); las cadenas de todas las plantas íntegras (PR-NUC-13); el pool se
reconecta solo (la misma ``Database`` escribe después del reinicio, sin intervención).

Solo datos generados: los clips son metadatos de bytes que nunca se suben.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter, deque
from collections.abc import Iterator
from typing import Any

import pytest
from vigia_contracts.models.enumerations import AcceptanceStatus

from tests.resilience.harness import WALL, scenario
from tests.resilience.load import SimulatedNode, all_clips, kit_findings
from tests.resilience.stack import LedgerStack, ledger_stack
from tests.writer_support import (
    FINDING_TYPE,
    Place,
    fetch_record,
    unit_context,
    verify_ledger_chains,
)
from vigia_platform.shared.context import ActorKind, ActorUnit
from vigia_platform.shared.db import RouteClass, route_class_scope

pytestmark = [pytest.mark.integration, pytest.mark.nightly]

NODES = 20
FINDINGS_PER_NODE = 8
PLANTS = 4
DRAIN_TIMEOUT_SECONDS = 300.0


@pytest.fixture(scope="module")
def stack() -> Iterator[LedgerStack]:
    with ledger_stack("fs_nuc_02") as stack:
        yield stack


async def _source_keys(stack: LedgerStack, organizations: list[uuid.UUID]) -> Counter[str]:
    connection = await stack.migrated.connect()
    try:
        rows = await connection.fetch(
            "SELECT source_key FROM ledger.ledger_record"
            " WHERE record_type = $1 AND organization_id = ANY($2::uuid[])",
            FINDING_TYPE,
            organizations,
        )
    finally:
        await connection.close()
    return Counter(str(row["source_key"]) for row in rows)


def test_fs_nuc_02_database_failover_with_the_node_queues_active(stack: LedgerStack) -> None:
    with scenario(
        "FS-NUC-02",
        title="Conmutación de la base con la cola del nodo activa",
        injection=f"contenedor de PostgreSQL reiniciado mientras {NODES} nodos simulados envían",
        expected=(
            "cero pérdidas y cero duplicados al drenar; cadenas íntegras; el pool se reconecta"
            " solo; sin intervención"
        ),
    ) as run:
        env = stack.env
        organizations = [uuid.uuid4(), uuid.uuid4()]
        plants = [Place.new(organizations[index % 2]) for index in range(PLANTS)]
        database = stack.database(node_pool_size=10)
        writer = stack.writer(database)
        nodes: list[SimulatedNode] = []
        for index in range(NODES):
            plant = plants[index % PLANTS]
            place = Place(plant.organization_id, plant.plant_id, plant.zone_id, uuid.uuid4())
            documents = kit_findings(run.random.getrandbits(32), place, FINDINGS_PER_NODE)
            for clip in all_clips(documents):
                env.storage.put(clip)
            context = unit_context(place.organization_id, ActorUnit.U03, kind=ActorKind.SYSTEM)
            nodes.append(
                SimulatedNode(
                    name=f"nodo-{index:02d}",
                    context=context,
                    queue=deque(documents),
                    write=writer.write,
                    rng=run.child(),
                )
            )
        total = NODES * FINDINGS_PER_NODE
        restart_after = int(total * run.random.uniform(0.2, 0.5))
        run.observe(nodes=NODES, findings=total, restart_after_accepted=restart_after)

        async def drive() -> dict[str, Any]:
            with route_class_scope(RouteClass.NODE):
                tasks = [asyncio.create_task(node.drain()) for node in nodes]

                def accepted() -> int:
                    return sum(len(node.report.accepted) for node in nodes)

                while accepted() < restart_after:
                    await asyncio.sleep(0.02)
                before_restart = accepted()
                started = WALL.monotonic()
                await asyncio.to_thread(stack.container.restart)
                restart_seconds = WALL.monotonic() - started
                # Los nodos reintentan solos; la prueba solo espera a la base para leer después.
                await asyncio.to_thread(stack.container.wait_ready)
                after_restart_mark = accepted()
                async with asyncio.timeout(DRAIN_TIMEOUT_SECONDS):
                    await asyncio.gather(*tasks)
                return {
                    "accepted_before_restart": before_restart,
                    "accepted_when_restarted": after_restart_mark,
                    "restart_seconds": round(restart_seconds, 2),
                    "drained_seconds": round(WALL.monotonic() - started, 2),
                }

        timings = stack.run(drive())
        reports = [node.report for node in nodes]
        retries: Counter[str] = Counter()
        for report in reports:
            retries.update(report.retries)
        receipts = {key: receipt for report in reports for key, receipt in report.accepted.items()}
        rejected = {key: code for report in reports for key, code in report.rejected.items()}
        duplicates = sum(
            1
            for receipt in receipts.values()
            if receipt.status is AcceptanceStatus.ACCEPTED_DUPLICATE
        )
        stored = stack.run(_source_keys(stack, organizations))
        chains = {
            str(organization): {
                str(plant): length
                for plant, length in stack.run(
                    verify_ledger_chains(stack.migrated, organization)
                ).items()
            }
            for organization in organizations
        }
        run.observe(
            **timings,
            retries=dict(retries),
            accepted_duplicate=duplicates,
            rejected=rejected,
            stored_findings=sum(stored.values()),
            chains=chains,
        )

        assert rejected == {}, "ningún hallazgo se pierde ni se rechaza de forma permanente"
        assert len(receipts) == total
        assert set(stored) == set(receipts), "cero pérdidas: cada hallazgo está en el expediente"
        assert max(stored.values()) == 1, "cero duplicados: ninguna source_key repetida"
        assert sum(retries.values()) > 0, "el reinicio cortó envíos en curso"
        assert timings["accepted_when_restarted"] < total, "el reinicio llegó con colas activas"
        # El pool se reconectó solo: la misma ``Database`` escribió el resto tras el reinicio.
        assert sum(stored.values()) - timings["accepted_when_restarted"] > 0
        # Cada recibo final apunta al registro que quedó (también los accepted_duplicate).
        for key, receipt in run.random.sample(sorted(receipts.items()), k=20):
            row = stack.run(fetch_record(stack.migrated, receipt.record_id))
            assert row is not None and row["source_key"] == key
        # El pool se reconectó solo: la misma base escribió después del reinicio.
        assert sum(len(chain) for chain in chains.values()) == PLANTS
        assert sum(sum(chain.values()) for chain in chains.values()) == total
