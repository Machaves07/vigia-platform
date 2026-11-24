"""Las versiones del catálogo de una zona para nombrar lo que trae un registro (TASK-595; A-129).

``loop`` no lee SQL de ``catalog`` ni de ``fleet``: el detalle y la ficha de vida piden por este
puerto las versiones guardadas del catálogo de la zona vigentes en una ventana (``versions``, una
sentencia con su contexto) y eligen, con el dominio puro (``fleet.domain.record_declarations``), la
del paso 7 de cada registro. La lectura de la foto del episodio (SCR-06) usa ``versions_in`` en su
propia transacción.

Es la misma consulta que el paso 7 de la ingesta (``PostgresIngestStore.catalog_versions``): las
filas de ``catalog.zone_catalog_version`` de la organización y la zona con ``[issued_at,
superseded_at)`` cruzándose con la ventana, la más nueva primero. **Sin auditoría propia**: la
lectura que la contiene (el detalle, la ficha o el listado) ya deja la suya y nada más. Una versión
que no tiene la forma de ``ZoneCatalog`` se salta (sus nombres quedan «sin declaración»), nunca
rompe la lectura. SQL sin valores en trazas ni registros (NFR-GOB-57).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import Any, Final, Protocol

from sqlalchemy import text

from vigia_platform.fleet.domain.clock_tolerance import Window
from vigia_platform.fleet.domain.ingest_order import CatalogUnreadable
from vigia_platform.fleet.domain.record_declarations import DeclaredCatalogVersion
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.observability.logging import get_logger

__all__ = ["RecordCatalogPort", "RecordCatalogs", "declared_versions"]

_log = get_logger("fleet.record_declarations")

_VERSIONS: Final = text(
    "SELECT v.catalog_version, v.issued_at, v.superseded_at, v.payload"
    " FROM catalog.zone_catalog_version AS v"
    " WHERE v.organization_id = :organization_id AND v.zone_id = :zone_id"
    " AND v.issued_at <= :window_end"
    " AND (v.superseded_at IS NULL OR v.superseded_at > :window_start)"
    " ORDER BY v.catalog_version DESC"
)


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str | bytes) else value


def declared_versions(rows: Sequence[Any]) -> tuple[DeclaredCatalogVersion, ...]:
    """Las filas de ``_VERSIONS`` con sus nombres; una ilegible se salta (y se registra)."""
    found: list[DeclaredCatalogVersion] = []
    for row in rows:
        try:
            found.append(
                DeclaredCatalogVersion.from_payload(
                    int(row.catalog_version), row.issued_at, row.superseded_at, _json(row.payload)
                )
            )
        except (CatalogUnreadable, ValueError, TypeError):
            _log.warning("versión del catálogo ilegible al nombrar un registro: se salta")
    return tuple(found)


def _parameters(organization_id: uuid.UUID, zone_id: uuid.UUID, window: Window) -> dict[str, Any]:
    return {
        "organization_id": organization_id,
        "zone_id": zone_id,
        "window_start": window.start,
        "window_end": window.end,
    }


class RecordCatalogPort(Protocol):
    """Lo que ``loop`` usa para nombrar las señales y las variables de un registro."""

    async def versions(
        self, context: ScopeContext, *, zone_id: uuid.UUID, window: Window
    ) -> tuple[DeclaredCatalogVersion, ...]: ...


@repository
class RecordCatalogs:
    """``RecordCatalogPort`` sobre PostgreSQL."""

    def __init__(self, *, database: LedgerDatabase) -> None:
        self._database = database

    def __repr__(self) -> str:
        return "RecordCatalogs()"

    async def versions(
        self, context: ScopeContext, *, zone_id: uuid.UUID, window: Window
    ) -> tuple[DeclaredCatalogVersion, ...]:
        """Las versiones de la zona vigentes en algún instante de ``window``, la más nueva
        primero, con el contexto dado (la RLS limita a su organización)."""
        rows = await self._database.read(
            context, _VERSIONS, _parameters(context.organization_id, zone_id, window)
        )
        return declared_versions(rows)

    async def versions_in(
        self, transaction: Transaction, *, zone_id: uuid.UUID, window: Window
    ) -> tuple[DeclaredCatalogVersion, ...]:
        """Lo mismo dentro de ``transaction``."""
        result = await transaction.execute(
            _VERSIONS, _parameters(transaction.context.organization_id, zone_id, window)
        )
        return declared_versions(result.all())
