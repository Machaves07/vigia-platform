"""Puerto de **solo lectura** de lo que el alta entrega ya firmado (TASK-219; NFR-GOB-10;
PAT-GOB-REN-02).

Lee, con el ``ScopeContext`` de la organización del nodo, las tablas de ``gob_0017`` que escriben
la publicación del catálogo (VIG-145) y las compuertas (VIG-146), y ``fleet.node_configuration``:

- el sobre vigente ``catalog.zone_catalog_version.envelope`` de cada zona (``superseded_at IS
  NULL``);
- el ``SignedEnvelope<GateState>`` conservado en ``catalog.zone_gate_state.envelope``: desde A-60
  toda zona nace con su sobre inicial ``pending``; la anterior que aún no lo tiene sale sin sobre
  y el alta se lo hace emitir (``GateService.ensure_initial_envelopes``);
- las cámaras de ``catalog.zone_camera`` con su ``stream_reference`` (filas que nunca se borran:
  el cruce con el catálogo vigente lo hace ``node_configuration.initial_configuration``);
- la fila de ``fleet.node_configuration`` del nodo, si existe.

Nada aquí firma ni escribe. Los sobres salen tal como se guardaron (el valor JSON de la columna).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Final

from sqlalchemy import text

from vigia_platform.fleet.domain.node_configuration import (
    BootstrapZone,
    ConfigurationUnavailable,
    NodeConfiguration,
    StoredCamera,
)
from vigia_platform.shared.context import repository
from vigia_platform.shared.db import Transaction

__all__ = ["PostgresBootstrapEnvelopes"]

_CATALOGS: Final = text(
    "SELECT zone_id, envelope FROM catalog.zone_catalog_version"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " AND zone_id = ANY(CAST(:zone_ids AS uuid[])) AND superseded_at IS NULL"
)
_GATES: Final = text(
    "SELECT zone_id, envelope FROM catalog.zone_gate_state"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " AND zone_id = ANY(CAST(:zone_ids AS uuid[]))"
)
_CAMERAS: Final = text(
    "SELECT zone_id, camera_id, stream_reference FROM catalog.zone_camera"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " AND zone_id = ANY(CAST(:zone_ids AS uuid[])) ORDER BY zone_id, camera_id"
)
_CONFIGURATION: Final = text(
    "SELECT time_sources, sent_records_retention_days, token_max_age_seconds,"
    " heartbeat_interval_seconds FROM fleet.node_configuration"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id AND node_id = :node_id"
)


def _uuid(value: object) -> uuid.UUID:
    return uuid.UUID(str(value))


def _json(value: object) -> Any:
    """``jsonb`` llega ya decodificado con asyncpg; como texto, se decodifica aquí."""
    return json.loads(value) if isinstance(value, str | bytes) else value


def _configuration(row: Any) -> NodeConfiguration:
    sources = _json(row.time_sources)
    try:
        return NodeConfiguration(
            time_sources=tuple(sources) if isinstance(sources, list) else (),
            sent_records_retention_days=row.sent_records_retention_days,
            token_max_age_seconds=row.token_max_age_seconds,
            heartbeat_interval_seconds=row.heartbeat_interval_seconds,
        )
    except ValueError:
        # Una fila que no cabe en el contrato nunca sale hacia el nodo: transitorio y registro.
        raise ConfigurationUnavailable("la configuración guardada del nodo no es válida") from None


@repository
class PostgresBootstrapEnvelopes:
    """Lecturas del alta, todas en la transacción de lectura que abre el servicio."""

    async def zones(
        self, transaction: Transaction, plant_id: uuid.UUID, zone_ids: Sequence[uuid.UUID]
    ) -> tuple[BootstrapZone, ...]:
        """Lo guardado de cada zona, en el orden de ``zone_ids`` (``None`` donde falte un sobre)."""
        if not zone_ids:
            return ()
        parameters = {
            "organization_id": transaction.context.organization_id,
            "plant_id": plant_id,
            "zone_ids": [str(zone_id) for zone_id in zone_ids],
        }
        catalogs: dict[uuid.UUID, Mapping[str, Any]] = {
            _uuid(row.zone_id): _json(row.envelope)
            for row in (await transaction.execute(_CATALOGS, parameters)).all()
        }
        gates: dict[uuid.UUID, Mapping[str, Any]] = {
            _uuid(row.zone_id): _json(row.envelope)
            for row in (await transaction.execute(_GATES, parameters)).all()
        }
        cameras: dict[uuid.UUID, list[StoredCamera]] = {}
        for row in (await transaction.execute(_CAMERAS, parameters)).all():
            cameras.setdefault(_uuid(row.zone_id), []).append(
                StoredCamera(_uuid(row.camera_id), str(row.stream_reference))
            )
        return tuple(
            BootstrapZone(
                zone_id=zone_id,
                catalog_envelope=catalogs.get(zone_id),
                gate_envelope=gates.get(zone_id),
                cameras=tuple(cameras.get(zone_id, ())),
            )
            for zone_id in zone_ids
        )

    async def configuration(
        self, transaction: Transaction, plant_id: uuid.UUID, node_id: uuid.UUID
    ) -> NodeConfiguration:
        """La ``NodeConfiguration`` del nodo, o los valores por defecto de D-11 sin fila."""
        result = await transaction.execute(
            _CONFIGURATION,
            {
                "organization_id": transaction.context.organization_id,
                "plant_id": plant_id,
                "node_id": node_id,
            },
        )
        row = result.first()
        return NodeConfiguration() if row is None else _configuration(row)
