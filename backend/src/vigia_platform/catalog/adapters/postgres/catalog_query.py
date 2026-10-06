"""``CatalogQueryPort`` sobre PostgreSQL (TASK-213; LC-GOB-23a; interfaces §1.1; PAT-GOB-REN-04).

Una **sola sentencia** ``READ ONLY`` por operación, siempre desde ``identity.zone``: la zona tiene
que existir en la organización del contexto y estar en su alcance (``allowed_scopes``: la
organización entera, su planta o ella misma). Si no, la sentencia no devuelve la fila de la zona y
la operación responde ``ResourceNotFound``. Cada sentencia nombra además la organización del
contexto (defensa en profundidad sobre la RLS) y entra en las tablas del catálogo por
``(organization_id, plant_id, zone_id)``, el prefijo de sus índices de alcance (PAT-GOB-ESC):

- ``current_catalog`` y ``catalog_at``: la versión con ``superseded_at`` nulo, o la que rige en
  ``at`` (``issued_at <= at < superseded_at``; la de número mayor si una versión duró cero);
- ``standard_version``: la versión pedida del estándar, con su vigencia (``effective_until`` es el
  ``issued_at`` de la versión del catálogo que la retira);
- ``standard_at`` y ``standards_at_many``: **la misma sentencia**, con las referencias como tres
  listas paralelas (``unnest ... WITH ORDINALITY``) y la clave primaria ``(standard_id, version)``
  en cada una; la versión que rige la decide ``standard_valid_at`` (TASK-208), la misma regla de la
  ingesta;
- ``single_occupancy`` y ``single_occupancy_many``: **la misma sentencia**, con las zonas como
  lista ordenada y la versión vigente (o la de ``at``) de cada una;
- ``catalog_history``: las versiones de la más reciente a la más antigua, por clave
  (``catalog_version`` exclusivo) y con una de más para saber si hay página siguiente;
- ``regression_state``: la fila de ``walk_test_regression`` (``current`` si nunca se marcó).

Sin caché y sin paginar salvo ``catalog_history``. Las formas por lote validan su tope antes de
consultar (``PortLimitExceeded``) y responden ``ResourceNotFound`` entera si falta un elemento.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.engine import Row

from vigia_platform.catalog.adapters.postgres.catalog_repository import (
    standard_from_row,
    version_from_row,
)
from vigia_platform.catalog.adapters.postgres.regression_repository import regression_from_row
from vigia_platform.catalog.domain.catalog_version import ZoneCatalogVersion
from vigia_platform.catalog.domain.enums import CatalogChangedField
from vigia_platform.catalog.domain.ports import (
    MAX_CATALOG_HISTORY_PAGE,
    MAX_SINGLE_OCCUPANCY_ZONES,
    MAX_STANDARD_REFS,
    CatalogHistoryEntry,
    CatalogHistoryPage,
    PortLimitExceeded,
    PortQueryInvalid,
    SingleOccupancy,
    StandardRef,
)
from vigia_platform.catalog.domain.regression import WalkTestRegression
from vigia_platform.catalog.domain.standard import DeclaredStandardVersion, standard_valid_at
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.context import ScopeContext, ScopeLevel, repository

__all__ = ["PostgresCatalogQuery", "scope_parameters"]

_MAX_CATALOG_VERSION: Final = 2_147_483_647
"""``catalog_version`` es ``integer``: un cursor mayor no es una versión."""

# Toda sentencia repite la condición de zona visible (organización del contexto y
# ``allowed_scopes``): ``text()`` solo admite literales (VIG001).
_CURRENT: Final = text(
    "SELECT v.organization_id, v.plant_id, v.zone_id, v.catalog_version, v.issued_at,"
    " v.issued_by, v.role_in_use, v.reason_es, v.changed_fields, v.payload, v.envelope,"
    " v.single_occupancy, v.aggregation_window_minutes, v.ledger_record_id, v.superseded_at"
    " FROM identity.zone AS z"
    " JOIN catalog.zone_catalog_version AS v ON v.organization_id = z.organization_id"
    " AND v.plant_id = z.plant_id AND v.zone_id = z.zone_id AND v.superseded_at IS NULL"
    " WHERE z.organization_id = :organization_id AND z.zone_id = :zone_id"
    " AND (CAST(:whole_organization AS boolean)"
    " OR z.plant_id = ANY(CAST(:scope_plants AS uuid[]))"
    " OR z.zone_id = ANY(CAST(:scope_zones AS uuid[])))"
)

_AT: Final = text(
    "SELECT v.organization_id, v.plant_id, v.zone_id, v.catalog_version, v.issued_at,"
    " v.issued_by, v.role_in_use, v.reason_es, v.changed_fields, v.payload, v.envelope,"
    " v.single_occupancy, v.aggregation_window_minutes, v.ledger_record_id, v.superseded_at"
    " FROM identity.zone AS z"
    " JOIN catalog.zone_catalog_version AS v ON v.organization_id = z.organization_id"
    " AND v.plant_id = z.plant_id AND v.zone_id = z.zone_id"
    " AND v.issued_at <= CAST(:at AS timestamptz)"
    " AND (v.superseded_at IS NULL OR v.superseded_at > CAST(:at AS timestamptz))"
    " WHERE z.organization_id = :organization_id AND z.zone_id = :zone_id"
    " AND (CAST(:whole_organization AS boolean)"
    " OR z.plant_id = ANY(CAST(:scope_plants AS uuid[]))"
    " OR z.zone_id = ANY(CAST(:scope_zones AS uuid[])))"
    " ORDER BY v.catalog_version DESC LIMIT 1"
)

_STANDARD_VERSION: Final = text(
    "SELECT d.organization_id, d.plant_id, d.zone_id, d.standard_id, d.version, d.family,"
    " d.title_es, d.declared_text, d.declared_by, d.effective_from, d.predicate,"
    " d.catalog_version, d.reason_es, d.retired_in_catalog_version,"
    " r.issued_at AS effective_until"
    " FROM identity.zone AS z"
    " JOIN catalog.declared_standard_version AS d ON d.standard_id = :standard_id"
    " AND d.version = :version AND d.organization_id = z.organization_id"
    " AND d.plant_id = z.plant_id AND d.zone_id = z.zone_id"
    " LEFT JOIN catalog.zone_catalog_version AS r ON r.organization_id = d.organization_id"
    " AND r.plant_id = d.plant_id AND r.zone_id = d.zone_id"
    " AND r.catalog_version = d.retired_in_catalog_version"
    " WHERE z.organization_id = :organization_id AND z.zone_id = :zone_id"
    " AND (CAST(:whole_organization AS boolean)"
    " OR z.plant_id = ANY(CAST(:scope_plants AS uuid[]))"
    " OR z.zone_id = ANY(CAST(:scope_zones AS uuid[])))"
)

# Una fila por referencia (``ref``) aunque no haya versión (``LEFT JOIN``): ``visible`` dice si la
# zona existe y está en el alcance. Las candidatas ya vienen filtradas por la vigencia en ``at``;
# ``standard_valid_at`` lo confirma y rechaza una historia solapada.
_STANDARDS_AT: Final = text(
    "SELECT q.ref, z.zone_id AS visible, s.organization_id, s.plant_id, s.zone_id,"
    " s.standard_id, s.version, s.family, s.title_es, s.declared_text, s.declared_by,"
    " s.effective_from, s.predicate, s.catalog_version, s.reason_es,"
    " s.retired_in_catalog_version, s.effective_until"
    " FROM unnest(CAST(:zone_ids AS uuid[]), CAST(:standard_ids AS uuid[]),"
    " CAST(:ats AS timestamptz[])) WITH ORDINALITY AS q(zone_id, standard_id, at, ref)"
    " LEFT JOIN identity.zone AS z ON z.organization_id = :organization_id"
    " AND z.zone_id = q.zone_id"
    " AND (CAST(:whole_organization AS boolean)"
    " OR z.plant_id = ANY(CAST(:scope_plants AS uuid[]))"
    " OR z.zone_id = ANY(CAST(:scope_zones AS uuid[])))"
    " LEFT JOIN LATERAL (SELECT d.organization_id, d.plant_id, d.zone_id, d.standard_id,"
    " d.version, d.family, d.title_es, d.declared_text, d.declared_by, d.effective_from,"
    " d.predicate, d.catalog_version, d.reason_es, d.retired_in_catalog_version,"
    " r.issued_at AS effective_until"
    " FROM catalog.declared_standard_version AS d"
    " LEFT JOIN catalog.zone_catalog_version AS r ON r.organization_id = d.organization_id"
    " AND r.plant_id = d.plant_id AND r.zone_id = d.zone_id"
    " AND r.catalog_version = d.retired_in_catalog_version"
    " WHERE d.standard_id = q.standard_id AND d.organization_id = z.organization_id"
    " AND d.plant_id = z.plant_id AND d.zone_id = z.zone_id"
    " AND d.effective_from <= q.at AND (r.issued_at IS NULL OR r.issued_at > q.at)) AS s"
    " ON true"
    " ORDER BY q.ref, s.version"
)

_SINGLE_OCCUPANCY: Final = text(
    "SELECT q.position, z.zone_id, v.single_occupancy, v.aggregation_window_minutes,"
    " v.catalog_version, v.issued_at"
    " FROM unnest(CAST(:zone_ids AS uuid[])) WITH ORDINALITY AS q(zone_id, position)"
    " LEFT JOIN identity.zone AS z ON z.organization_id = :organization_id"
    " AND z.zone_id = q.zone_id"
    " AND (CAST(:whole_organization AS boolean)"
    " OR z.plant_id = ANY(CAST(:scope_plants AS uuid[]))"
    " OR z.zone_id = ANY(CAST(:scope_zones AS uuid[])))"
    " LEFT JOIN LATERAL (SELECT c.single_occupancy, c.aggregation_window_minutes,"
    " c.catalog_version, c.issued_at FROM catalog.zone_catalog_version AS c"
    " WHERE c.organization_id = z.organization_id AND c.plant_id = z.plant_id"
    " AND c.zone_id = z.zone_id"
    " AND CASE WHEN CAST(:at AS timestamptz) IS NULL THEN c.superseded_at IS NULL"
    " ELSE c.issued_at <= CAST(:at AS timestamptz)"
    " AND (c.superseded_at IS NULL OR c.superseded_at > CAST(:at AS timestamptz)) END"
    " ORDER BY c.catalog_version DESC LIMIT 1) AS v ON true"
    " ORDER BY q.position"
)

_HISTORY: Final = text(
    "SELECT z.zone_id, v.catalog_version, v.issued_at, v.issued_by, v.reason_es,"
    " v.changed_fields"
    " FROM identity.zone AS z"
    " LEFT JOIN catalog.zone_catalog_version AS v ON v.organization_id = z.organization_id"
    " AND v.plant_id = z.plant_id AND v.zone_id = z.zone_id"
    " AND (CAST(:cursor AS integer) IS NULL OR v.catalog_version < CAST(:cursor AS integer))"
    " WHERE z.organization_id = :organization_id AND z.zone_id = :zone_id"
    " AND (CAST(:whole_organization AS boolean)"
    " OR z.plant_id = ANY(CAST(:scope_plants AS uuid[]))"
    " OR z.zone_id = ANY(CAST(:scope_zones AS uuid[])))"
    " ORDER BY v.catalog_version DESC LIMIT :limit"
)

_REGRESSION: Final = text(
    "SELECT z.organization_id AS zone_organization_id, z.plant_id AS zone_plant_id,"
    " z.zone_id AS visible, r.organization_id, r.plant_id, r.zone_id, r.state, r.marked_at,"
    " r.cause, r.catalog_version, r.model_version, r.affected_row_ids, r.cleared_at,"
    " r.cleared_by_session_id, r.ledger_record_id"
    " FROM identity.zone AS z"
    " LEFT JOIN catalog.walk_test_regression AS r ON r.organization_id = z.organization_id"
    " AND r.plant_id = z.plant_id AND r.zone_id = z.zone_id"
    " WHERE z.organization_id = :organization_id AND z.zone_id = :zone_id"
    " AND (CAST(:whole_organization AS boolean)"
    " OR z.plant_id = ANY(CAST(:scope_plants AS uuid[]))"
    " OR z.zone_id = ANY(CAST(:scope_zones AS uuid[])))"
)


def scope_parameters(context: ScopeContext) -> dict[str, Any]:
    """La organización y ``allowed_scopes`` del contexto como parámetros de las sentencias."""
    scopes = context.allowed_scopes
    return {
        "organization_id": context.organization_id,
        "whole_organization": any(
            s.scope_level is ScopeLevel.ORGANIZATION and s.scope_id == context.organization_id
            for s in scopes
        ),
        "scope_plants": list(
            dict.fromkeys(s.scope_id for s in scopes if s.scope_level is ScopeLevel.PLANT)
        ),
        "scope_zones": list(
            dict.fromkeys(s.scope_id for s in scopes if s.scope_level is ScopeLevel.ZONE)
        ),
    }


def _uuid(value: object) -> uuid.UUID:
    """asyncpg devuelve su propio tipo de UUID; el dominio exige ``uuid.UUID``."""
    return uuid.UUID(str(value))


def _identifier(value: object, name: str) -> uuid.UUID:
    if type(value) is not uuid.UUID:
        raise PortQueryInvalid(f"{name} debe ser uuid.UUID")
    return value


def _instant(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise PortQueryInvalid(f"{name} debe ser una marca con zona horaria")
    return value


def _sequence(value: object, name: str) -> Sequence[Any]:
    # Una lista o una tupla: el orden de la salida es el declarado (un conjunto no lo tiene).
    if not isinstance(value, list | tuple):
        raise PortQueryInvalid(f"{name} debe ser una lista")
    return value


@repository
class PostgresCatalogQuery:
    """Las nueve operaciones de ``CatalogQueryPort``: cada una, una lectura ``READ ONLY`` de una
    sentencia."""

    def __init__(self, database: LedgerDatabase) -> None:
        self._database = database

    def __repr__(self) -> str:
        return "PostgresCatalogQuery()"

    async def current_catalog(
        self, context: ScopeContext, zone_id: uuid.UUID
    ) -> ZoneCatalogVersion:
        """La versión vigente del catálogo de la zona (estándares, cámaras, cobertura, señales,
        umbrales, ventanas y la marca unipersonal)."""
        zone_id = _identifier(zone_id, "zone_id")
        rows = await self._database.read(
            context, _CURRENT, {**scope_parameters(context), "zone_id": zone_id}
        )
        if not rows:
            raise ResourceNotFound()
        return version_from_row(rows[0])

    async def catalog_at(
        self, context: ScopeContext, zone_id: uuid.UUID, at: datetime
    ) -> ZoneCatalogVersion:
        """La versión del catálogo que regía en ``at`` (lectura histórica, misma forma)."""
        zone_id = _identifier(zone_id, "zone_id")
        moment = _instant(at, "at")
        rows = await self._database.read(
            context, _AT, {**scope_parameters(context), "zone_id": zone_id, "at": moment}
        )
        if not rows:
            raise ResourceNotFound()
        return version_from_row(rows[0])

    async def standard_version(
        self, context: ScopeContext, zone_id: uuid.UUID, standard_id: uuid.UUID, version: int
    ) -> DeclaredStandardVersion:
        """La versión ``version`` del estándar de la zona, con su vigencia, para citarla."""
        zone_id = _identifier(zone_id, "zone_id")
        standard_id = _identifier(standard_id, "standard_id")
        if type(version) is not int or not 1 <= version <= _MAX_CATALOG_VERSION:
            raise PortQueryInvalid("version debe ser un entero de 1 a 2 147 483 647")
        rows = await self._database.read(
            context,
            _STANDARD_VERSION,
            {
                **scope_parameters(context),
                "zone_id": zone_id,
                "standard_id": standard_id,
                "version": version,
            },
        )
        if not rows:
            raise ResourceNotFound()
        return standard_from_row(rows[0])

    async def standard_at(
        self, context: ScopeContext, zone_id: uuid.UUID, standard_id: uuid.UUID, at: datetime
    ) -> DeclaredStandardVersion:
        """La versión del estándar que regía en ``at`` (``standard_valid_at``)."""
        ref = StandardRef(
            _identifier(zone_id, "zone_id"),
            _identifier(standard_id, "standard_id"),
            _instant(at, "at"),
        )
        (found,) = await self._standards_at(context, (ref,))
        return found

    async def standards_at_many(
        self, context: ScopeContext, refs: Sequence[StandardRef]
    ) -> tuple[DeclaredStandardVersion, ...]:
        """Hasta 200 referencias, en su orden; una sola que falte: ``ResourceNotFound``."""
        refs = _sequence(refs, "refs")
        if len(refs) > MAX_STANDARD_REFS:
            raise PortLimitExceeded("standards_at_many", "200 referencias")
        for ref in refs:
            if not isinstance(ref, StandardRef):
                raise PortQueryInvalid("cada referencia debe ser StandardRef")
            _identifier(ref.zone_id, "zone_id")
            _identifier(ref.standard_id, "standard_id")
            _instant(ref.at, "at")
        if not refs:
            return ()
        return await self._standards_at(context, refs)

    async def _standards_at(
        self, context: ScopeContext, refs: Sequence[StandardRef]
    ) -> tuple[DeclaredStandardVersion, ...]:
        rows = await self._database.read(
            context,
            _STANDARDS_AT,
            {
                **scope_parameters(context),
                "zone_ids": [ref.zone_id for ref in refs],
                "standard_ids": [ref.standard_id for ref in refs],
                "ats": [ref.at for ref in refs],
            },
        )
        candidates: list[list[DeclaredStandardVersion]] = [[] for _ in refs]
        for row in rows:
            if row.visible is None:
                raise ResourceNotFound()
            if row.standard_id is not None:
                candidates[int(row.ref) - 1].append(standard_from_row(row))
        found: list[DeclaredStandardVersion] = []
        for ref, history in zip(refs, candidates, strict=True):
            valid = standard_valid_at(history, ref.standard_id, ref.at)
            if valid is None:
                raise ResourceNotFound()
            found.append(valid)
        return tuple(found)

    async def catalog_history(
        self,
        context: ScopeContext,
        zone_id: uuid.UUID,
        cursor: int | None = None,
        limit: int = MAX_CATALOG_HISTORY_PAGE,
    ) -> CatalogHistoryPage:
        """Una página de versiones, de la más reciente a la más antigua (hasta 200).

        ``cursor`` es el ``catalog_version`` exclusivo que devolvió la página anterior. Una zona
        visible sin catálogo da una página vacía.
        """
        zone_id = _identifier(zone_id, "zone_id")
        if type(limit) is not int or not 1 <= limit <= MAX_CATALOG_HISTORY_PAGE:
            raise PortQueryInvalid("limit debe estar entre 1 y 200")
        if cursor is not None and (
            type(cursor) is not int or not 1 <= cursor <= _MAX_CATALOG_VERSION
        ):
            raise PortQueryInvalid("cursor debe ser un catalog_version")
        rows = await self._database.read(
            context,
            _HISTORY,
            {
                **scope_parameters(context),
                "zone_id": zone_id,
                "cursor": cursor,
                # Una de más: dice si hay página siguiente sin otra consulta.
                "limit": limit + 1,
            },
        )
        if not rows:
            raise ResourceNotFound()
        entries = tuple(
            CatalogHistoryEntry(
                catalog_version=int(row.catalog_version),
                issued_at=row.issued_at,
                issued_by=_uuid(row.issued_by),
                reason_es=row.reason_es,
                changed_fields=tuple(CatalogChangedField(f) for f in row.changed_fields),
            )
            for row in rows
            if row.catalog_version is not None
        )
        items = entries[:limit]
        following = items[-1].catalog_version if len(entries) > limit else None
        return CatalogHistoryPage(items=items, next_cursor=following)

    async def single_occupancy(
        self, context: ScopeContext, zone_id: uuid.UUID, at: datetime | None = None
    ) -> SingleOccupancy:
        """La marca unipersonal de la versión vigente (o de la que regía en ``at``)."""
        zone_id = _identifier(zone_id, "zone_id")
        moment = None if at is None else _instant(at, "at")
        (found,) = await self._single_occupancy(context, (zone_id,), moment)
        return found

    async def single_occupancy_many(
        self, context: ScopeContext, zone_ids: Sequence[uuid.UUID], at: datetime | None = None
    ) -> tuple[SingleOccupancy, ...]:
        """Hasta 50 zonas, en su orden; una sola que falte: ``ResourceNotFound``."""
        zone_ids = _sequence(zone_ids, "zone_ids")
        if len(zone_ids) > MAX_SINGLE_OCCUPANCY_ZONES:
            raise PortLimitExceeded("single_occupancy_many", "50 zonas")
        for zone_id in zone_ids:
            _identifier(zone_id, "zone_id")
        moment = None if at is None else _instant(at, "at")
        if not zone_ids:
            return ()
        return await self._single_occupancy(context, zone_ids, moment)

    async def _single_occupancy(
        self, context: ScopeContext, zone_ids: Sequence[uuid.UUID], at: datetime | None
    ) -> tuple[SingleOccupancy, ...]:
        rows = await self._database.read(
            context,
            _SINGLE_OCCUPANCY,
            {**scope_parameters(context), "zone_ids": list(zone_ids), "at": at},
        )
        if len(rows) != len(zone_ids) or any(row.catalog_version is None for row in rows):
            raise ResourceNotFound()
        return tuple(_single_occupancy(row) for row in rows)

    async def regression_state(
        self, context: ScopeContext, zone_id: uuid.UUID
    ) -> WalkTestRegression:
        """La marca de regresión de la zona; ``current`` sin causa si nunca se marcó."""
        zone_id = _identifier(zone_id, "zone_id")
        rows = await self._database.read(
            context, _REGRESSION, {**scope_parameters(context), "zone_id": zone_id}
        )
        if not rows:
            raise ResourceNotFound()
        row = rows[0]
        if row.zone_id is None:
            return WalkTestRegression.initial(
                _uuid(row.zone_organization_id), _uuid(row.zone_plant_id), _uuid(row.visible)
            )
        return regression_from_row(row)


def _single_occupancy(row: Row[Any]) -> SingleOccupancy:
    return SingleOccupancy(
        zone_id=_uuid(row.zone_id),
        single_occupancy=bool(row.single_occupancy),
        aggregation_window_minutes=int(row.aggregation_window_minutes),
        catalog_version=int(row.catalog_version),
        issued_at=row.issued_at,
    )
