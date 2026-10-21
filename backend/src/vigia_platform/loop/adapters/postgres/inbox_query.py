"""La consulta única de la bandeja (TASK-307; LC-LAZ-05; PAT-LAZ-ESC-03; NFR-LAZ-01 a 03).

``InboxQuery.page`` ejecuta **una sola sentencia** (sin contar la apertura del contexto de la
transacción) y devuelve, en una fila, tres listas ``jsonb``:

- ``items``: la página de ``loop.finding_state`` sobre el **índice parcial**
  ``finding_state_inbox`` (los siete estados activos, que la sentencia repite como literal para
  que el planificador pueda usarlo también con un plan genérico), ordenada y cortada por clave
  ``(occurred_at_bucket, finding_state_id)`` con ``limit + 1`` filas para saber si hay otra;
- ``zones``: cada zona consultada con su nodo asignado hoy (``identity.zone_node_assignment``) y
  el ``communication_state`` que U-03 proyecta en ``fleet.node_inventory`` (BR-LAZ-149 y 153);
- ``partitions``: el control de la proyección de cada planta consultada que no está ``healthy``
  (``rebuilding`` o ``diverged``, BR-LAZ-156), o cuyo checkpoint va detrás de la ingesta.

**Cero llamadas a U-03** (NFR-LAZ-03): todo sale de tablas; ningún puerto. La sentencia nunca lee
el expediente (BR-LAZ-09: la bandeja no genera auditoría de consulta).
La secuencia del evento de ingesta se publica en la misma transacción que el registro. Permite
declarar el atraso antes de la primera lectura, también tras restaurar una copia física, sin leer
cargas ni ``outbox_delivery`` ni esperar al verificador diario. El retraso incremental normal se
trata igual (fallo cerrado): no hay una ventana de gracia que pueda ocultar una copia atrasada.

**Alcance** (BR-LAZ-01): la seguridad a nivel de fila acota la organización; las zonas que entran
son las del alcance de la sesión con ``findings.read`` (organización entera, plantas o zonas), y
de ellas las pedidas. Una zona fuera de esa lista no aporta ni filas ni avisos.
**BR-LAZ-12**: solo filas con la compuerta de uso ``approved`` en el instante del hecho.

La sentencia es fija: los filtros ausentes viajan como ``NULL`` y el sentido del orden elige una
de dos sentencias construidas aquí, al importar, con constantes (nunca con datos de la petición).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text

from vigia_platform.loop.domain.enums import FindingLifecycleState
from vigia_platform.loop.domain.time_aggregation import MAX_WINDOW_MINUTES
from vigia_platform.shared.context import repository
from vigia_platform.shared.db import Transaction

__all__ = [
    "INBOX_STATES",
    "InboxCursor",
    "InboxFilters",
    "InboxQuery",
    "InboxRows",
    "SessionScope",
    "inbox_statement",
]

INBOX_STATES: Final = (
    FindingLifecycleState.RECEIVED,
    FindingLifecycleState.IN_REVIEW,
    FindingLifecycleState.OPEN,
    FindingLifecycleState.ACTION_ASSIGNED,
    FindingLifecycleState.OVERDUE,
    FindingLifecycleState.UNCLOSABLE,
    FindingLifecycleState.SUSPENDED_BY_RECLASSIFICATION,
)
"""Los siete estados activos del índice parcial ``finding_state_inbox`` (laz_0029)."""

_STATES_SQL: Final = ", ".join(f"'{state.value}'" for state in INBOX_STATES)
_MARGIN_SQL: Final = f"interval '{MAX_WINDOW_MINUTES} minutes'"
"""Prefiltro por cubo del borde inferior del periodo: el cubo nunca está más de una ventana por
detrás de la marca exacta (``occurred_at_bucket <= occurred_at_exact``, laz_0029), así que la
poda de particiones y el índice siguen sirviendo cuando el periodo se compara con la exacta."""

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
                WHEN EXISTS (
                    SELECT 1 FROM shared.outbox_event e
                    WHERE e.organization_id = :organization_id AND e.plant_id = p.plant_id
                      AND e.event_name IN ('finding_received', 'detection_for_review_received')
                      AND e.ledger_sequence > COALESCE(c.last_applied_ledger_sequence, 0)
                ) THEN 'diverged'
                ELSE 'healthy' END AS state
    FROM (SELECT DISTINCT plant_id FROM zones) p
    LEFT JOIN loop.loop_projection_checkpoint c
      ON c.organization_id = :organization_id AND c.plant_id = p.plant_id
),
page AS (
    SELECT f.finding_state_id, f.plant_id, f.zone_id, f.subject_kind, f.family, f.tier,
           f.standard_id, f.standard_version, f.lifecycle_state,
           f.current_classification_outcome, f.classification_count, f.due_at, f.overdue_days,
           f.observability_state_at_event, f.zone_agreement_revoked, f.zone_agreement_revoked_at,
           f.single_occupancy_zone, f.aggregation_window_minutes,
           f.walk_test_regression_pending, f.walk_test_regression_cause,
           f.occurred_at_exact, f.occurred_at_bucket, f.age_days, f.evidence_flagged,
           f.missing_segments_declared
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
              @CMP@ (CAST(:after_bucket AS timestamptz), CAST(:after_id AS uuid)))
    ORDER BY f.occurred_at_bucket @DIR@, f.finding_state_id @DIR@
    LIMIT :limit
),
zone_notices AS (
    SELECT z.plant_id, z.zone_id, a.node_id, n.code AS node_code,
           CASE WHEN a.node_id IS NULL THEN NULL
                ELSE COALESCE(i.communication_state, 'unknown') END AS communication_state
    FROM zones z
    LEFT JOIN identity.zone_node_assignment a
      ON a.organization_id = z.organization_id AND a.plant_id = z.plant_id
     AND a.zone_id = z.zone_id AND a.unassigned_at IS NULL
    LEFT JOIN identity.node_identity n
      ON n.organization_id = a.organization_id AND n.node_id = a.node_id
    LEFT JOIN fleet.node_inventory i
      ON i.organization_id = a.organization_id AND i.node_id = a.node_id
)
SELECT
    (SELECT COALESCE(jsonb_agg(to_jsonb(p)
                               ORDER BY p.occurred_at_bucket @DIR@, p.finding_state_id @DIR@),
                     '[]'::jsonb)
       FROM page p) AS items,
    (SELECT COALESCE(jsonb_agg(to_jsonb(zn) ORDER BY zn.plant_id, zn.zone_id), '[]'::jsonb)
       FROM zone_notices zn) AS zones,
    (SELECT COALESCE(jsonb_agg(jsonb_build_object('plant_id', h.plant_id, 'state', h.state)
                               ORDER BY h.plant_id), '[]'::jsonb)
       FROM partition_health h WHERE h.state <> 'healthy') AS partitions
"""


def inbox_statement(*, newest_first: bool) -> str:
    """La sentencia de la bandeja en un sentido del orden (solo constantes)."""
    direction, comparison = ("DESC", "<") if newest_first else ("ASC", ">")
    return (
        _TEMPLATE.replace("@STATES@", _STATES_SQL)
        .replace("@MARGIN@", _MARGIN_SQL)
        .replace("@CMP@", comparison)
        .replace("@DIR@", direction)
    )


_NEWEST_FIRST: Final = text(inbox_statement(newest_first=True))
_OLDEST_FIRST: Final = text(inbox_statement(newest_first=False))


@dataclass(frozen=True, slots=True)
class InboxCursor:
    """La clave de la última fila servida."""

    occurred_at_bucket: datetime
    finding_state_id: uuid.UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class InboxFilters:
    """Los **únicos** criterios de la bandeja (BR-LAZ-02): zona, familia, tier, estado y periodo."""

    zone_ids: tuple[uuid.UUID, ...] | None = None
    family: str | None = None
    tier: str | None = None
    lifecycle_states: tuple[FindingLifecycleState, ...] | None = None
    period_from: datetime | None = None
    period_to: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionScope:
    """Lo que el alcance de la sesión con ``findings.read`` deja ver."""

    organization_wide: bool
    plant_ids: tuple[uuid.UUID, ...]
    zone_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True, slots=True)
class InboxRows:
    """La fila única de la sentencia, ya decodificada."""

    items: tuple[dict[str, Any], ...]
    zones: tuple[dict[str, Any], ...]
    partitions: tuple[dict[str, Any], ...]


def _decoded(value: Any) -> tuple[dict[str, Any], ...]:
    loaded = json.loads(value) if isinstance(value, str | bytes) else value
    return tuple(dict(entry) for entry in loaded)


def _array(values: Sequence[uuid.UUID] | None) -> list[uuid.UUID] | None:
    return None if values is None else list(values)


@repository
class InboxQuery:
    """La lectura de la bandeja dentro de la transacción (con contexto) del llamador."""

    async def page(
        self,
        transaction: Transaction,
        *,
        organization_id: uuid.UUID,
        scope: SessionScope,
        filters: InboxFilters,
        after: InboxCursor | None,
        limit: int,
        newest_first: bool = True,
    ) -> InboxRows:
        statement = _NEWEST_FIRST if newest_first else _OLDEST_FIRST
        states = (
            None
            if filters.lifecycle_states is None
            else [state.value for state in filters.lifecycle_states]
        )
        result = await transaction.execute(
            statement,
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
        return InboxRows(_decoded(row.items), _decoded(row.zones), _decoded(row.partitions))
