"""Lo que el nodo recibe del catálogo, leído tal como se guardó (TASK-223; PAT-GOB-REN-02).

Lecturas de solo lectura sobre las tablas de ``catalog`` (gob_0017) que escriben la publicación
del catálogo (TASK-208/209) y las compuertas (TASK-211):

- ``zone_snapshot``: por cada zona, la versión vigente del catálogo con su cobertura mínima y sus
  cámaras (para ``coverage_ok``) y el ``SignedEnvelope<GateState>`` guardado **como texto**
  (``envelope::text``) con su ``valid_until``. La respuesta del latido inserta ese texto sin
  volver a serializarlo, canonicalizarlo ni firmarlo;
- ``current_catalog_envelope``: el ``ZoneCatalogVersion.envelope`` vigente **como texto**, que la
  ruta ``GET zones/{zone_id}/catalog`` responde byte a byte.

Toda sentencia va en la transacción del llamador (contexto del nodo) y nombra la organización
del contexto: la RLS y el filtro explícito impiden leer el catálogo de otra organización. Que la
zona sea **del nodo** lo comprueba el llamador con su alcance (``NodeScope.covers_zone``).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text

from vigia_platform.shared.context import repository
from vigia_platform.shared.db import Transaction

__all__ = ["PostgresNodeCatalogStore", "StoredGateEnvelope", "ZoneSnapshot"]

_CATALOGS: Final = text(
    "SELECT zone_id, catalog_version,"
    " jsonb_build_object('cameras', payload -> 'cameras',"
    " 'minimum_coverage', payload -> 'minimum_coverage')::text AS coverage"
    " FROM catalog.zone_catalog_version"
    " WHERE organization_id = :organization_id AND zone_id = ANY(CAST(:zone_ids AS uuid[]))"
    " AND superseded_at IS NULL"
)
_GATES: Final = text(
    "SELECT zone_id, envelope::text AS envelope, valid_until FROM catalog.zone_gate_state"
    " WHERE organization_id = :organization_id AND zone_id = ANY(CAST(:zone_ids AS uuid[]))"
)
_CATALOG_ENVELOPE: Final = text(
    "SELECT envelope::text AS envelope FROM catalog.zone_catalog_version"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id AND superseded_at IS NULL"
)


@dataclass(frozen=True, slots=True)
class StoredGateEnvelope:
    """El ``SignedEnvelope<GateState>`` guardado de una zona, como texto, y su vencimiento."""

    text: str
    valid_until: datetime


@dataclass(frozen=True, slots=True)
class ZoneSnapshot:
    """Catálogos vigentes (versión y cobertura) y sobres de compuertas de las zonas pedidas."""

    catalog_versions: Mapping[uuid.UUID, int]
    coverage: Mapping[uuid.UUID, Mapping[str, Any]]
    gates: Mapping[uuid.UUID, StoredGateEnvelope]


def _uuid(value: object) -> uuid.UUID:
    return uuid.UUID(str(value))


def _zones(zone_ids: Iterable[uuid.UUID]) -> list[str]:
    return sorted({str(zone_id) for zone_id in zone_ids})


@repository
class PostgresNodeCatalogStore:
    """Catálogo vigente y sobre de compuertas guardados, sin firmar ni canonicalizar."""

    async def zone_snapshot(
        self, transaction: Transaction, zone_ids: Iterable[uuid.UUID]
    ) -> ZoneSnapshot:
        zones = _zones(zone_ids)
        if not zones:
            return ZoneSnapshot({}, {}, {})
        key = {"organization_id": transaction.context.organization_id, "zone_ids": zones}
        catalogs = (await transaction.execute(_CATALOGS, key)).all()
        gates = (await transaction.execute(_GATES, key)).all()
        return ZoneSnapshot(
            catalog_versions={_uuid(row.zone_id): int(row.catalog_version) for row in catalogs},
            coverage={_uuid(row.zone_id): json.loads(row.coverage) for row in catalogs},
            gates={
                _uuid(row.zone_id): StoredGateEnvelope(str(row.envelope), row.valid_until)
                for row in gates
            },
        )

    async def gate_envelopes(
        self, transaction: Transaction, zone_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, StoredGateEnvelope]:
        """Los sobres de compuertas guardados de ``zone_ids`` (tras una renovación, A-55)."""
        zones = _zones(zone_ids)
        if not zones:
            return {}
        rows = (
            await transaction.execute(
                _GATES,
                {"organization_id": transaction.context.organization_id, "zone_ids": zones},
            )
        ).all()
        return {
            _uuid(row.zone_id): StoredGateEnvelope(str(row.envelope), row.valid_until)
            for row in rows
        }

    async def current_catalog_envelope(
        self, transaction: Transaction, zone_id: uuid.UUID
    ) -> bytes | None:
        """El sobre del catálogo vigente de la zona tal como se guardó, o ``None`` si no hay."""
        result = await transaction.execute(
            _CATALOG_ENVELOPE,
            {"organization_id": transaction.context.organization_id, "zone_id": zone_id},
        )
        row = result.first()
        return None if row is None else str(row.envelope).encode("utf-8")
