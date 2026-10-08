"""``gob_0028``: índice parcial de las transiciones de comunicación por nodo (TASK-233).

La volumetría de U-03 midió ``GET /fleet/nodes`` con un año de la organización mayor: sin este
índice, la búsqueda del último ``node_communication_state_changed`` de cada nodo recorre todos los
registros sin zona de la organización (mediana de 30 s por página). Aquí se comprueba:

- el índice está en la tabla particionada y en **cada** partición, con su predicado;
- la búsqueda del inventario, como ``vigia_app`` con el contexto de la seguridad a nivel de fila y
  sin barridos secuenciales, se planifica sobre él **sin ordenar**
  (el índice da la última transición), y no sobre el índice de zona, que es lo que hacía antes.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Final

import asyncpg  # type: ignore[import-untyped]
import pytest

from tests.identity_db import migrated_database
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.fleet.adapters.postgres.inventory_queries import _INVENTORY

pytestmark = pytest.mark.integration

INDEX: Final = "ledger_record_node_communication"
"""El índice de la tabla particionada; cada partición tiene su copia."""
LOOKUP: Final = (
    "EXPLAIN SELECT r.content_json ->> 'state' FROM ledger.ledger_record AS r"
    " WHERE r.organization_id = $1 AND r.scope_zone_id IS NULL"
    " AND r.record_type = 'node_communication_state_changed' AND r.scope_node_id = $2"
    " ORDER BY r.content_json ->> 'since' DESC, r.record_id DESC LIMIT 1"
)
"""El lateral ``comm`` de ``fleet.adapters.postgres.inventory_queries._INVENTORY``."""


async def _check(dsn: str) -> tuple[bool, list[str], list[str], str]:
    connection = await asyncpg.connect(dsn)
    try:
        parent = bool(
            await connection.fetchval(
                "SELECT 1 FROM pg_indexes WHERE schemaname = 'ledger' AND indexname = $1", INDEX
            )
        )
        partitions = [
            row["relid"]
            for row in await connection.fetch(
                "SELECT relid::regclass::text AS relid FROM pg_partition_tree("
                "'ledger.ledger_record') WHERE isleaf"
            )
        ]
        definitions = [
            row["indexdef"]
            for row in await connection.fetch(
                "SELECT i.indexdef FROM pg_inherits AS h"
                " JOIN pg_class AS c ON c.oid = h.inhrelid"
                " JOIN pg_indexes AS i ON i.tablename = c.relname AND i.schemaname = 'ledger'"
                " WHERE h.inhparent = 'ledger.ledger_record'::regclass"
                " AND i.indexdef LIKE '%(organization_id, scope_node_id, ((content_json ->> %'"
            )
        ]
        # Como la planifica la aplicación: ``vigia_app`` con el contexto de ``Database.read``,
        # así que la seguridad a nivel de fila entra en el plan.
        organization = uuid.uuid4()
        async with connection.transaction():
            await connection.execute("SET LOCAL ROLE vigia_app")
            await connection.execute("SET LOCAL enable_seqscan = off")
            await connection.execute(
                "SELECT set_config('vigia.organization_id', $1, true),"
                " set_config('vigia.actor_kind', 'user', true),"
                " set_config('vigia.concession_id', '', true)",
                str(organization),
            )
            plan = "\n".join(
                row[0] for row in await connection.fetch(LOOKUP, organization, uuid.uuid4())
            )
    finally:
        await connection.close()
    return parent, partitions, definitions, plan


def test_gob_0028_index_on_every_partition_and_used_by_the_inventory_lookup(
    postgres_endpoint: PostgresEndpoint,
) -> None:
    with migrated_database(postgres_endpoint, "gob_0028_index") as migrated:
        parent, partitions, definitions, plan = asyncio.run(_check(migrated.as_role().dsn))

    # La consulta del inventario ordena como el índice (si no, lo usaría sin su orden).
    assert LOOKUP.split(" ORDER BY ")[1] in _INVENTORY.text
    assert parent, INDEX
    assert partitions, "ledger.ledger_record sin particiones"
    assert len(definitions) == len(partitions), (partitions, definitions)
    for definition in definitions:
        assert "WHERE ((record_type = 'node_communication_state_changed'::text)" in definition
        assert "(scope_zone_id IS NULL)" in definition
    # Las particiones nombran su copia del índice por sus columnas.
    scans = [line for line in plan.splitlines() if "Index Scan" in line or "Seq Scan" in line]
    assert scans, plan
    assert all("organization_id_scope_node_id_expr" in line for line in scans), plan
    # El índice da el orden: la última transición sin ordenar la historia del nodo.
    assert "Merge Append" in plan and "Sort  (" not in plan, plan
