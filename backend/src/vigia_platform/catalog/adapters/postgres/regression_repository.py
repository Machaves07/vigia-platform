"""``catalog.walk_test_regression`` sobre PostgreSQL (LC-GOB-09; tabla de ``gob_0017``).

Una fila por zona, proyección 🔒: se inserta en la primera marca y después se actualiza (las
columnas de ``_MUTABLE_COLUMNS`` de la migración); ``vigia_app`` no tiene ``DELETE``. Toda
sentencia va en una ``Transaction`` abierta con un ``ScopeContext``: la RLS limita a la
organización y, bajo concesión, a la planta; cada sentencia nombra además la organización del
contexto y la zona (defensa en profundidad).

**Candado de la fila** (``lock``): ``pg_advisory_xact_lock`` sobre la regresión de la zona. Toda
marca lo toma antes de leer la fila y lo suelta al confirmar: dos marcas simultáneas de la misma
zona (una publicación y un cambio de ``model_version``, o dos publicaciones) se ordenan y la
segunda lee lo que dejó la primera, así que la unión de filas nunca pierde una marca. El candado
existe aunque la fila todavía no exista (la primera marca de la zona).
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.engine import Row

from vigia_platform.catalog.domain.catalog_version import ZoneRef
from vigia_platform.catalog.domain.enums import RegressionCause, RegressionState
from vigia_platform.catalog.domain.regression import ALL_ROWS, AffectedRows, WalkTestRegression
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.db import Transaction

__all__ = ["PostgresRegressionRepository"]

_LOCK: Final = text(
    "SELECT pg_advisory_xact_lock(hashtextextended('walk_test_regression|' || :zone_id, 0))"
)
_ZONE: Final = text(
    "SELECT organization_id, plant_id, zone_id, code FROM identity.zone"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id"
)
_SELECT: Final = text(
    "SELECT organization_id, plant_id, zone_id, state, marked_at, cause, catalog_version,"
    " model_version, affected_row_ids, cleared_at, cleared_by_session_id, ledger_record_id"
    " FROM catalog.walk_test_regression"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id"
)
_UPSERT: Final = text(
    "INSERT INTO catalog.walk_test_regression (zone_id, organization_id, plant_id, state,"
    " marked_at, cause, catalog_version, model_version, affected_row_ids, cleared_at,"
    " cleared_by_session_id, ledger_record_id)"
    " VALUES (:zone_id, :organization_id, :plant_id, :state, :marked_at, :cause,"
    " :catalog_version, :model_version, CAST(:affected_row_ids AS jsonb), :cleared_at,"
    " :cleared_by_session_id, :ledger_record_id)"
    " ON CONFLICT (zone_id) DO UPDATE SET state = EXCLUDED.state,"
    " marked_at = EXCLUDED.marked_at, cause = EXCLUDED.cause,"
    " catalog_version = EXCLUDED.catalog_version, model_version = EXCLUDED.model_version,"
    " affected_row_ids = EXCLUDED.affected_row_ids, cleared_at = EXCLUDED.cleared_at,"
    " cleared_by_session_id = EXCLUDED.cleared_by_session_id,"
    " ledger_record_id = EXCLUDED.ledger_record_id"
    " WHERE catalog.walk_test_regression.organization_id = EXCLUDED.organization_id"
)


def _uuid(value: object) -> uuid.UUID:
    """asyncpg devuelve su propio tipo de UUID; el dominio exige ``uuid.UUID``."""
    return uuid.UUID(str(value))


def _affected(value: object) -> AffectedRows | None:
    # asyncpg entrega el ``jsonb`` ya decodificado («all» llega como la cadena ``all``); como
    # texto sin decodificar, llega con sus comillas o sus corchetes.
    document: Any = value.decode() if isinstance(value, bytes) else value
    if document is None:
        return None
    if document == ALL_ROWS:
        return ALL_ROWS
    if isinstance(document, str):
        document = json.loads(document)
        if document == ALL_ROWS:
            return ALL_ROWS
    return tuple(uuid.UUID(str(row)) for row in document)


def _stored(rows: AffectedRows | None) -> str | None:
    if rows is None:
        return None
    return json.dumps(ALL_ROWS if rows == ALL_ROWS else [str(row) for row in rows])


def _regression(row: Row[Any]) -> WalkTestRegression:
    return WalkTestRegression(
        organization_id=_uuid(row.organization_id),
        plant_id=_uuid(row.plant_id),
        zone_id=_uuid(row.zone_id),
        state=RegressionState(row.state),
        marked_at=row.marked_at,
        cause=None if row.cause is None else RegressionCause(row.cause),
        catalog_version=None if row.catalog_version is None else int(row.catalog_version),
        model_version=row.model_version,
        affected_row_ids=_affected(row.affected_row_ids),
        cleared_at=row.cleared_at,
        cleared_by_session_id=(
            None if row.cleared_by_session_id is None else _uuid(row.cleared_by_session_id)
        ),
        ledger_record_id=_uuid(row.ledger_record_id),
    )


def _key(context: ScopeContext, zone_id: uuid.UUID) -> dict[str, Any]:
    return {"organization_id": context.organization_id, "zone_id": zone_id}


@repository
class PostgresRegressionRepository:
    """Lectura, candado y escritura de la regresión de una zona."""

    def __init__(self, database: LedgerDatabase) -> None:
        self._database = database

    async def zone(self, transaction: Transaction, zone_id: uuid.UUID) -> ZoneRef | None:
        """La zona en la organización de la transacción (si la RLS la deja ver), o ``None``."""
        result = await transaction.execute(_ZONE, _key(transaction.context, zone_id))
        row = result.first()
        if row is None:
            return None
        return ZoneRef(
            organization_id=_uuid(row.organization_id),
            plant_id=_uuid(row.plant_id),
            zone_id=_uuid(row.zone_id),
            zone_code=row.code,
        )

    async def lock(self, transaction: Transaction, zone_id: uuid.UUID) -> None:
        """Exclusión entre marcas de la zona hasta el fin de la transacción."""
        await transaction.execute(_LOCK, {"zone_id": str(zone_id)})

    async def get(self, transaction: Transaction, zone_id: uuid.UUID) -> WalkTestRegression | None:
        """La fila de la zona, o ``None`` si nunca se marcó."""
        result = await transaction.execute(_SELECT, _key(transaction.context, zone_id))
        row = result.first()
        return None if row is None else _regression(row)

    async def save(self, transaction: Transaction, regression: WalkTestRegression) -> None:
        """Inserta o actualiza la fila; siempre con el registro que la respalda."""
        if regression.organization_id != transaction.context.organization_id:
            raise ValueError("la regresión es de otra organización que la transacción")
        if regression.ledger_record_id is None:
            raise ValueError("la fila de la regresión siempre nombra su último registro")
        await transaction.execute(
            _UPSERT,
            {
                "zone_id": regression.zone_id,
                "organization_id": regression.organization_id,
                "plant_id": regression.plant_id,
                "state": regression.state.value,
                "marked_at": regression.marked_at,
                "cause": None if regression.cause is None else regression.cause.value,
                "catalog_version": regression.catalog_version,
                "model_version": regression.model_version,
                "affected_row_ids": _stored(regression.affected_row_ids),
                "cleared_at": regression.cleared_at,
                "cleared_by_session_id": regression.cleared_by_session_id,
                "ledger_record_id": regression.ledger_record_id,
            },
        )
