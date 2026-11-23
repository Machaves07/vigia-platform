"""La consulta del histórico de lo cerrado (TASK-586; O-11; LC-LAZ-05; BR-LAZ-01, 02, 09 y 12).

``ClosedFindingsQuery.page`` ejecuta **una sola sentencia** (sin contar la apertura del contexto
de la transacción) sobre la proyección ``loop.finding_state`` y devuelve, en una fila, tres listas
``jsonb``:

- ``items``: la página de los hallazgos ``closed`` y ``discarded`` sobre su **índice parcial**
  propio, ``finding_state_closed`` (``laz_0044``; el de la bandeja, ``finding_state_inbox``, solo
  cubre los siete estados activos). La sentencia repite los dos estados como literal para que el
  planificador pueda usar el índice también con un plan genérico. Orden y corte por clave
  ``(occurred_at_bucket, finding_state_id)``, la más reciente primero, con ``limit + 1`` filas
  para saber si hay otra;
- ``zones``: los identificadores de las zonas consultadas que el alcance deja ver (para responder
  ``not_found`` a una zona pedida fuera de él, igual que a una inexistente);
- ``partitions``: el control de la proyección de cada planta consultada que no está ``healthy``,
  con el mismo cálculo que la bandeja (``rebuilding``, ``diverged`` o la frontera de ingesta
  atrasada, BR-LAZ-156): sus filas no se sirven hasta recuperar la proyección (DR-LAZ-01).

**Sin auditoría de consulta** (BR-LAZ-09): la sentencia nunca lee el expediente; la ficha de vida
que abre cada fila sí se audita. **Alcance** (BR-LAZ-01): la seguridad a nivel de fila acota la
organización; las zonas son las del alcance de la sesión con ``findings.read`` y, de ellas, las
pedidas. **BR-LAZ-12**: solo filas con la compuerta de uso ``approved`` en el instante del hecho.

La sentencia es fija: los filtros ausentes viajan como ``NULL`` y se construye aquí, al importar,
con constantes (nunca con datos de la petición).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import text

from vigia_platform.loop.adapters.postgres.inbox_query import (
    InboxCursor,
    InboxFilters,
    SessionScope,
)
from vigia_platform.loop.domain.enums import FindingLifecycleState
from vigia_platform.loop.domain.time_aggregation import MAX_WINDOW_MINUTES
from vigia_platform.shared.context import repository
from vigia_platform.shared.db import Transaction

__all__ = [
    "CLOSED_STATES",
    "ClosedFindingsQuery",
    "ClosedRows",
    "closed_findings_statement",
]

CLOSED_STATES: Final = (FindingLifecycleState.CLOSED, FindingLifecycleState.DISCARDED)
"""Los dos estados del histórico y del índice parcial ``finding_state_closed`` (laz_0044)."""

_STATES_SQL: Final = ", ".join(f"'{state.value}'" for state in CLOSED_STATES)
_MARGIN_SQL: Final = f"interval '{MAX_WINDOW_MINUTES} minutes'"
"""Prefiltro por cubo del borde inferior del periodo, como la bandeja (``inbox_query``)."""

_TEMPLATE: Final = """
WITH zones AS (
    SELECT z.organization_id, z.plant_id, z.zone_id
    FROM identity.zone z
    WHERE z.organization_id = :organization_id
      AND (CAST(:organization_wide AS boolean)
           OR z.plant_id = ANY(CAST(:scope_plant_ids AS uuid[]))
           OR z.zone_id = ANY(CAST(:scope_zone_ids AS uuid[])))
      AND (CAST(:zone_ids AS uuid[]) IS NULL OR z.zone_id = ANY(CAST(:zone_ids AS uuid[])))
),
partition_health AS MATERIALIZED (
    SELECT p.plant_id,
           CASE WHEN c.state IN ('rebuilding', 'diverged') THEN c.state
                WHEN COALESCE((
                    SELECT e.ledger_sequence FROM shared.outbox_event e
                    WHERE e.organization_id = :organization_id AND e.plant_id = p.plant_id
                      AND e.event_name IN ('finding_received', 'detection_for_review_received')
                      AND e.ledger_sequence IS NOT NULL
                    ORDER BY e.ledger_sequence DESC LIMIT 1
                ), 0) > COALESCE(c.ingestion_frontier, 0) THEN 'diverged'
                ELSE 'healthy' END AS state
    FROM (SELECT DISTINCT plant_id FROM zones) p
    LEFT JOIN loop.loop_projection_checkpoint c
      ON c.organization_id = :organization_id AND c.plant_id = p.plant_id
),
page AS (
    SELECT f.finding_state_id, f.plant_id, f.zone_id, f.family, f.tier, f.lifecycle_state,
           f.current_classification_outcome, f.current_reason_category,
           f.current_action_id IS NOT NULL AS has_action,
           f.has_signed_root_cause, f.single_occupancy_zone, f.aggregation_window_minutes,
           f.occurred_at_exact, f.occurred_at_bucket
    FROM loop.finding_state f
    JOIN zones z
      ON f.organization_id = z.organization_id AND f.plant_id = z.plant_id
     AND f.zone_id = z.zone_id
    WHERE f.organization_id = :organization_id
      AND f.lifecycle_state IN (@STATES@)
      AND f.zone_gate_status_at_event = 'approved'
      AND NOT EXISTS (SELECT 1 FROM partition_health h
                      WHERE h.plant_id = f.plant_id AND h.state <> 'healthy')
      AND (CAST(:lifecycle_states AS text[]) IS NULL
           OR f.lifecycle_state = ANY(CAST(:lifecycle_states AS text[])))
      AND (CAST(:family AS text) IS NULL OR f.family = CAST(:family AS text))
      AND (CAST(:tier AS text) IS NULL OR f.tier = CAST(:tier AS text))
      AND (CAST(:period_from AS timestamptz) IS NULL
           OR (f.occurred_at_bucket >= CAST(:period_from AS timestamptz) - @MARGIN@
               AND CASE WHEN f.single_occupancy_zone THEN f.occurred_at_bucket
                        ELSE f.occurred_at_exact END >= CAST(:period_from AS timestamptz)))
      AND (CAST(:period_to AS timestamptz) IS NULL
           OR (f.occurred_at_bucket < CAST(:period_to AS timestamptz)
               AND CASE WHEN f.single_occupancy_zone THEN f.occurred_at_bucket
                        ELSE f.occurred_at_exact END < CAST(:period_to AS timestamptz)))
      AND (CAST(:after_bucket AS timestamptz) IS NULL
           OR (f.occurred_at_bucket, f.finding_state_id)
              < (CAST(:after_bucket AS timestamptz), CAST(:after_id AS uuid)))
    ORDER BY f.occurred_at_bucket DESC, f.finding_state_id DESC
    LIMIT :limit
)
SELECT
    (SELECT COALESCE(jsonb_agg(to_jsonb(p)
                               ORDER BY p.occurred_at_bucket DESC, p.finding_state_id DESC),
                     '[]'::jsonb)
       FROM page p) AS items,
    (SELECT COALESCE(jsonb_agg(z.zone_id ORDER BY z.zone_id), '[]'::jsonb)
       FROM zones z) AS zones,
    (SELECT COALESCE(jsonb_agg(jsonb_build_object('plant_id', h.plant_id, 'state', h.state)
                               ORDER BY h.plant_id), '[]'::jsonb)
       FROM partition_health h WHERE h.state <> 'healthy') AS partitions
"""


def closed_findings_statement() -> str:
    """La sentencia del histórico (solo constantes)."""
    return _TEMPLATE.replace("@STATES@", _STATES_SQL).replace("@MARGIN@", _MARGIN_SQL)


_STATEMENT: Final = text(closed_findings_statement())


@dataclass(frozen=True, slots=True)
class ClosedRows:
    """La fila única de la sentencia, ya decodificada."""

    items: tuple[dict[str, Any], ...]
    zone_ids: tuple[uuid.UUID, ...]
    partitions: tuple[dict[str, Any], ...]


def _loaded(value: Any) -> list[Any]:
    loaded = json.loads(value) if isinstance(value, str | bytes) else value
    return list(loaded)


def _array(values: Sequence[uuid.UUID] | None) -> list[uuid.UUID] | None:
    return None if values is None else list(values)


@repository
class ClosedFindingsQuery:
    """La lectura del histórico dentro de la transacción (con contexto) del llamador."""

    async def page(
        self,
        transaction: Transaction,
        *,
        organization_id: uuid.UUID,
        scope: SessionScope,
        filters: InboxFilters,
        after: InboxCursor | None,
        limit: int,
    ) -> ClosedRows:
        states = (
            None
            if filters.lifecycle_states is None
            else [state.value for state in filters.lifecycle_states]
        )
        result = await transaction.execute(
            _STATEMENT,
            {
                "organization_id": organization_id,
                "organization_wide": scope.organization_wide,
                "scope_plant_ids": _array(scope.plant_ids),
                "scope_zone_ids": _array(scope.zone_ids),
                "zone_ids": _array(filters.zone_ids),
                "lifecycle_states": states,
                "family": filters.family,
                "tier": filters.tier,
                "period_from": filters.period_from,
                "period_to": filters.period_to,
                "after_bucket": None if after is None else after.occurred_at_bucket,
                "after_id": None if after is None else after.finding_state_id,
                "limit": limit,
            },
        )
        row = result.one()
        return ClosedRows(
            items=tuple(dict(entry) for entry in _loaded(row.items)),
            zone_ids=tuple(uuid.UUID(str(zone)) for zone in _loaded(row.zones)),
            partitions=tuple(dict(entry) for entry in _loaded(row.partitions)),
        )
