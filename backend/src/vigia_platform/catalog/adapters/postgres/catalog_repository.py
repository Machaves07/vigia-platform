"""Catálogo versionado sobre PostgreSQL (LC-GOB-01; tablas de ``gob_0017``).

``catalog.zone_catalog_version`` y ``catalog.declared_standard_version`` son de solo anexar: aquí
solo hay ``INSERT``, ``SELECT`` y los dos cierres que la guarda de la base admite, una sola vez y
de nulo a su valor (``superseded_at`` y ``retired_in_catalog_version``). ``catalog.zone_camera`` es
una proyección: se inserta o se actualiza, nunca se borra (``vigia_app`` no tiene ``DELETE``).

Toda sentencia recibe un ``ScopeContext`` o una ``Transaction`` abierta con él: la seguridad a
nivel de fila limita a la organización y, bajo concesión, a la planta concedida. La **zona** no la
filtra la RLS: cada sentencia nombra la organización del contexto y la zona pedida (defensa en
profundidad), así que el catálogo de una zona nunca responde por otra.

**Exclusión entre publicaciones de la misma zona**: ``lock_zone`` toma
``pg_advisory_xact_lock`` sobre la zona al empezar la transacción de publicación; la segunda
publicación espera (hasta ``lock_timeout``, transitorio) y lee la versión que dejó la primera. La
clave primaria ``(zone_id, catalog_version)`` y la ``source_key`` del registro son el respaldo:
dos versiones con el mismo número nunca se confirman.

``envelope`` devuelve el sobre **tal como se guardó**: ninguna lectura canonicaliza ni firma
(PAT-GOB-REN-02).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.engine import Row
from vigia_contracts.models.enumerations import CameraRoleInZone, PredicateFamily

from vigia_platform.catalog.domain.catalog_version import ZoneCatalogVersion, ZoneRef
from vigia_platform.catalog.domain.enums import CatalogChangedField
from vigia_platform.catalog.domain.standard import DeclaredBy, DeclaredStandardVersion
from vigia_platform.catalog.domain.zone_camera import ZoneCamera
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.context import Role, ScopeContext, repository
from vigia_platform.shared.db import Transaction

__all__ = [
    "CATALOG_VERSION_PRIMARY_KEY",
    "CatalogWriteConflict",
    "PostgresCatalogRepository",
    "StoredZoneCamera",
]

CATALOG_VERSION_PRIMARY_KEY: Final = "zone_catalog_version_pkey"
"""``(zone_id, catalog_version)``: dos versiones con el mismo número nunca se confirman."""

_ZONE: Final = text(
    "SELECT organization_id, plant_id, zone_id, code FROM identity.zone"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id"
)
_LOCK_ZONE: Final = text(
    "SELECT pg_advisory_xact_lock(hashtextextended('catalog_version|' || :zone_id, 0))"
)
# Las lecturas repiten la lista de columnas literal: ``text()`` no admite concatenar (VIG001).
_CURRENT: Final = text(
    "SELECT organization_id, plant_id, zone_id, catalog_version, issued_at, issued_by,"
    " role_in_use, reason_es, changed_fields, payload, envelope, single_occupancy,"
    " aggregation_window_minutes, ledger_record_id, superseded_at"
    " FROM catalog.zone_catalog_version"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id"
    " AND superseded_at IS NULL"
)
_VERSION: Final = text(
    "SELECT organization_id, plant_id, zone_id, catalog_version, issued_at, issued_by,"
    " role_in_use, reason_es, changed_fields, payload, envelope, single_occupancy,"
    " aggregation_window_minutes, ledger_record_id, superseded_at"
    " FROM catalog.zone_catalog_version"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id"
    " AND catalog_version = :catalog_version"
)
_CURRENT_ENVELOPE: Final = text(
    "SELECT envelope FROM catalog.zone_catalog_version"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id"
    " AND superseded_at IS NULL"
)
_VERSION_ENVELOPE: Final = text(
    "SELECT envelope FROM catalog.zone_catalog_version"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id"
    " AND catalog_version = :catalog_version"
)
_INSERT_VERSION: Final = text(
    "INSERT INTO catalog.zone_catalog_version (organization_id, plant_id, zone_id,"
    " catalog_version, issued_at, issued_by, role_in_use, reason_es, changed_fields, payload,"
    " envelope, single_occupancy, aggregation_window_minutes, ledger_record_id)"
    " VALUES (:organization_id, :plant_id, :zone_id, :catalog_version, :issued_at, :issued_by,"
    " :role_in_use, :reason_es, CAST(:changed_fields AS text[]), CAST(:payload AS jsonb),"
    " CAST(:envelope AS jsonb), :single_occupancy, :aggregation_window_minutes,"
    " :ledger_record_id)"
)
_SUPERSEDE: Final = text(
    "UPDATE catalog.zone_catalog_version SET superseded_at = :superseded_at"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id"
    " AND catalog_version = :catalog_version AND superseded_at IS NULL"
)
_INSERT_STANDARD: Final = text(
    "INSERT INTO catalog.declared_standard_version (organization_id, plant_id, zone_id,"
    " standard_id, version, family, title_es, declared_text, declared_by, effective_from,"
    " predicate, catalog_version, reason_es)"
    " VALUES (:organization_id, :plant_id, :zone_id, :standard_id, :version, :family, :title_es,"
    " :declared_text, CAST(:declared_by AS jsonb), :effective_from, CAST(:predicate AS jsonb),"
    " :catalog_version, :reason_es)"
)
_CLOSE_STANDARD: Final = text(
    "UPDATE catalog.declared_standard_version"
    " SET retired_in_catalog_version = :retired_in_catalog_version"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id"
    " AND standard_id = :standard_id AND version = :version"
    " AND retired_in_catalog_version IS NULL"
)
_STANDARD_HISTORY: Final = text(
    "SELECT d.organization_id, d.plant_id, d.zone_id, d.standard_id, d.version, d.family,"
    " d.title_es, d.declared_text, d.declared_by, d.effective_from, d.predicate,"
    " d.catalog_version, d.reason_es, d.retired_in_catalog_version,"
    " z.issued_at AS effective_until"
    " FROM catalog.declared_standard_version AS d"
    " LEFT JOIN catalog.zone_catalog_version AS z"
    " ON z.organization_id = d.organization_id AND z.zone_id = d.zone_id"
    " AND z.catalog_version = d.retired_in_catalog_version"
    " WHERE d.organization_id = :organization_id AND d.zone_id = :zone_id"
    " ORDER BY d.standard_id, d.version"
)
_UPSERT_CAMERA: Final = text(
    "INSERT INTO catalog.zone_camera (organization_id, plant_id, zone_id, camera_id,"
    " role_in_zone, declared_min_fps, stream_reference, updated_at)"
    " VALUES (:organization_id, :plant_id, :zone_id, :camera_id, :role_in_zone,"
    " :declared_min_fps, :stream_reference, :updated_at)"
    " ON CONFLICT (zone_id, camera_id) DO UPDATE SET role_in_zone = EXCLUDED.role_in_zone,"
    " declared_min_fps = EXCLUDED.declared_min_fps,"
    " stream_reference = EXCLUDED.stream_reference, updated_at = EXCLUDED.updated_at"
)
_CAMERAS: Final = text(
    "SELECT camera_id, role_in_zone, declared_min_fps, stream_reference"
    " FROM catalog.zone_camera"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id"
    " ORDER BY camera_id"
)


class CatalogWriteConflict(Exception):
    """Un cierre no encontró la fila vigente que esperaba: otra publicación llegó antes."""

    def __init__(self) -> None:
        super().__init__("la versión vigente ya no es la que se leyó")


@dataclass(frozen=True, slots=True)
class StoredZoneCamera:
    """Una fila de ``zone_camera``: lo que no viaja en el catálogo (``stream_reference``)."""

    camera_id: uuid.UUID
    role_in_zone: CameraRoleInZone
    declared_min_fps: float
    stream_reference: str


def _uuid(value: object) -> uuid.UUID:
    """asyncpg devuelve su propio tipo de UUID; el dominio exige ``uuid.UUID``."""
    return uuid.UUID(str(value))


def _json(value: object) -> Any:
    """``jsonb`` llega ya decodificado con asyncpg; como texto, se decodifica aquí."""
    return json.loads(value) if isinstance(value, str | bytes) else value


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _version(row: Row[Any]) -> ZoneCatalogVersion:
    return ZoneCatalogVersion(
        organization_id=_uuid(row.organization_id),
        plant_id=_uuid(row.plant_id),
        zone_id=_uuid(row.zone_id),
        catalog_version=int(row.catalog_version),
        issued_at=row.issued_at,
        issued_by=_uuid(row.issued_by),
        role_in_use=Role(row.role_in_use),
        reason_es=row.reason_es,
        changed_fields=tuple(CatalogChangedField(f) for f in row.changed_fields),
        payload=_json(row.payload),
        envelope=_json(row.envelope),
        single_occupancy=bool(row.single_occupancy),
        aggregation_window_minutes=int(row.aggregation_window_minutes),
        ledger_record_id=_uuid(row.ledger_record_id),
        superseded_at=row.superseded_at,
    )


def _standard(row: Row[Any]) -> DeclaredStandardVersion:
    declared_by = _json(row.declared_by)
    return DeclaredStandardVersion(
        organization_id=_uuid(row.organization_id),
        plant_id=_uuid(row.plant_id),
        zone_id=_uuid(row.zone_id),
        standard_id=_uuid(row.standard_id),
        version=int(row.version),
        family=PredicateFamily(row.family),
        title_es=row.title_es,
        declared_text=row.declared_text,
        declared_by=DeclaredBy(
            uuid.UUID(declared_by["user_id"]), declared_by["display_name"], declared_by["role"]
        ),
        effective_from=row.effective_from,
        predicate=_json(row.predicate),
        catalog_version=int(row.catalog_version),
        reason_es=row.reason_es,
        retired_in_catalog_version=(
            None if row.retired_in_catalog_version is None else int(row.retired_in_catalog_version)
        ),
        effective_until=row.effective_until,
    )


@repository
class PostgresCatalogRepository:
    """Lecturas, altas y cierres del catálogo versionado de una zona."""

    def __init__(self, database: LedgerDatabase) -> None:
        self._database = database

    # --- Zona --------------------------------------------------------------------------------

    async def zone(self, context: ScopeContext, zone_id: uuid.UUID) -> ZoneRef | None:
        """La zona en la organización del contexto (si la RLS la deja ver), o ``None``."""
        rows = await self._database.read(
            context, _ZONE, {"organization_id": context.organization_id, "zone_id": zone_id}
        )
        if not rows:
            return None
        row = rows[0]
        return ZoneRef(
            organization_id=_uuid(row.organization_id),
            plant_id=_uuid(row.plant_id),
            zone_id=_uuid(row.zone_id),
            zone_code=row.code,
        )

    async def lock_zone(self, transaction: Transaction, zone_id: uuid.UUID) -> None:
        """Exclusión entre publicaciones de la zona hasta el fin de la transacción."""
        await transaction.execute(_LOCK_ZONE, {"zone_id": str(zone_id)})

    # --- Versiones ---------------------------------------------------------------------------

    async def current(
        self, transaction: Transaction, zone_id: uuid.UUID
    ) -> ZoneCatalogVersion | None:
        """La versión vigente de la zona (``superseded_at`` nulo), dentro de la transacción."""
        result = await transaction.execute(_CURRENT, _zone_key(transaction.context, zone_id))
        row = result.first()
        return None if row is None else _version(row)

    async def version(
        self, context: ScopeContext, zone_id: uuid.UUID, catalog_version: int | None = None
    ) -> ZoneCatalogVersion | None:
        """La versión ``catalog_version`` de la zona, o la vigente si es ``None``."""
        if catalog_version is None:
            rows = await self._database.read(context, _CURRENT, _zone_key(context, zone_id))
        else:
            rows = await self._database.read(
                context,
                _VERSION,
                {**_zone_key(context, zone_id), "catalog_version": catalog_version},
            )
        return _version(rows[0]) if rows else None

    async def envelope(
        self, transaction: Transaction, zone_id: uuid.UUID, catalog_version: int | None = None
    ) -> Mapping[str, Any] | None:
        """El sobre firmado tal como se guardó (la vigente si ``catalog_version`` es ``None``)."""
        if catalog_version is None:
            result = await transaction.execute(
                _CURRENT_ENVELOPE, _zone_key(transaction.context, zone_id)
            )
        else:
            result = await transaction.execute(
                _VERSION_ENVELOPE,
                {**_zone_key(transaction.context, zone_id), "catalog_version": catalog_version},
            )
        row = result.first()
        if row is None:
            return None
        envelope: Mapping[str, Any] = _json(row.envelope)
        return envelope

    async def insert_version(self, transaction: Transaction, version: ZoneCatalogVersion) -> None:
        """Anexa la versión; la violación de la clave primaria sale como ``IntegrityError``."""
        if version.organization_id != transaction.context.organization_id:
            raise ValueError("la versión es de otra organización que la transacción")
        await transaction.execute(
            _INSERT_VERSION,
            {
                "organization_id": version.organization_id,
                "plant_id": version.plant_id,
                "zone_id": version.zone_id,
                "catalog_version": version.catalog_version,
                "issued_at": version.issued_at,
                "issued_by": version.issued_by,
                "role_in_use": version.role_in_use.value,
                "reason_es": version.reason_es,
                "changed_fields": [f.value for f in version.changed_fields],
                "payload": _dumps(version.payload),
                "envelope": _dumps(version.envelope),
                "single_occupancy": version.single_occupancy,
                "aggregation_window_minutes": version.aggregation_window_minutes,
                "ledger_record_id": version.ledger_record_id,
            },
        )

    async def supersede(
        self,
        transaction: Transaction,
        zone_id: uuid.UUID,
        catalog_version: int,
        superseded_at: Any,
    ) -> None:
        """Cierra la versión ``catalog_version`` (de nulo a valor); si no estaba vigente,
        ``CatalogWriteConflict``."""
        result = await transaction.execute(
            _SUPERSEDE,
            {
                **_zone_key(transaction.context, zone_id),
                "catalog_version": catalog_version,
                "superseded_at": superseded_at,
            },
        )
        if _rowcount(result) != 1:
            raise CatalogWriteConflict

    # --- Estándares --------------------------------------------------------------------------

    async def insert_standard(
        self, transaction: Transaction, standard: DeclaredStandardVersion
    ) -> None:
        """Anexa una versión de estándar (su versión de catálogo ya tiene que existir)."""
        if standard.organization_id != transaction.context.organization_id:
            raise ValueError("el estándar es de otra organización que la transacción")
        await transaction.execute(
            _INSERT_STANDARD,
            {
                "organization_id": standard.organization_id,
                "plant_id": standard.plant_id,
                "zone_id": standard.zone_id,
                "standard_id": standard.standard_id,
                "version": standard.version,
                "family": standard.family.value,
                "title_es": standard.title_es,
                "declared_text": standard.declared_text,
                "declared_by": _dumps(standard.declared_by.as_json()),
                "effective_from": standard.effective_from,
                "predicate": _dumps(dict(standard.predicate)),
                "catalog_version": standard.catalog_version,
                "reason_es": standard.reason_es,
            },
        )

    async def close_standard(
        self,
        transaction: Transaction,
        zone_id: uuid.UUID,
        standard_id: uuid.UUID,
        version: int,
        retired_in_catalog_version: int,
    ) -> None:
        """Cierra la versión vigente del estándar; ya cerrada, ``CatalogWriteConflict``."""
        result = await transaction.execute(
            _CLOSE_STANDARD,
            {
                **_zone_key(transaction.context, zone_id),
                "standard_id": standard_id,
                "version": version,
                "retired_in_catalog_version": retired_in_catalog_version,
            },
        )
        if _rowcount(result) != 1:
            raise CatalogWriteConflict

    async def standard_history(
        self, context: ScopeContext, zone_id: uuid.UUID
    ) -> tuple[DeclaredStandardVersion, ...]:
        """Todas las versiones de los estándares de la zona con su vigencia (PR-GOB-06)."""
        rows = await self._database.read(context, _STANDARD_HISTORY, _zone_key(context, zone_id))
        return tuple(_standard(row) for row in rows)

    # --- Cámaras -----------------------------------------------------------------------------

    async def upsert_cameras(
        self,
        transaction: Transaction,
        zone: ZoneRef,
        cameras: Sequence[ZoneCamera],
        updated_at: Any,
    ) -> None:
        """Proyecta las cámaras declaradas en ``zone_camera`` (sin borrar las que salen)."""
        if zone.organization_id != transaction.context.organization_id:
            raise ValueError("la zona es de otra organización que la transacción")
        for camera in cameras:
            await transaction.execute(
                _UPSERT_CAMERA,
                {
                    "organization_id": zone.organization_id,
                    "plant_id": zone.plant_id,
                    "zone_id": zone.zone_id,
                    "camera_id": camera.camera_id,
                    "role_in_zone": camera.role_in_zone.value,
                    "declared_min_fps": camera.declared_min_fps,
                    "stream_reference": camera.stream_reference,
                    "updated_at": updated_at,
                },
            )

    async def cameras(
        self, transaction: Transaction, zone_id: uuid.UUID
    ) -> tuple[StoredZoneCamera, ...]:
        """Las filas de ``zone_camera`` de la zona (también las de cámaras ya retiradas)."""
        result = await transaction.execute(_CAMERAS, _zone_key(transaction.context, zone_id))
        return tuple(
            StoredZoneCamera(
                camera_id=_uuid(row.camera_id),
                role_in_zone=CameraRoleInZone(row.role_in_zone),
                declared_min_fps=float(row.declared_min_fps),
                stream_reference=row.stream_reference,
            )
            for row in result.all()
        )


def _zone_key(context: ScopeContext, zone_id: uuid.UUID) -> dict[str, Any]:
    return {"organization_id": context.organization_id, "zone_id": zone_id}


def _rowcount(result: Any) -> int:
    count: int = result.rowcount
    return count
