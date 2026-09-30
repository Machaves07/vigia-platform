"""``LabelPort``: consulta de etiquetas por zona, familia, motivo y periodo (LC-NUC-16 parte 2).

Operación del puerto (business-logic-model §6 "Etiquetas" y §10.1; BR-NUC-67, 68):

- ``consultar(context, period, zone_id?, family?, reason_category?, page?)``: las ``Label`` de la
  organización del contexto cuyo ``labeled_at`` cae en ``period`` (inicio inclusivo, fin
  exclusivo), combinando los filtros con Y, de la más reciente a la más antigua y paginadas
  **por clave** (``labeled_at, label_id`` de la última; página de 1 a 200), como
  ``LectorExpediente``.

**Sin imágenes ni texto libre** (BR-NUC-67, NFR-NUC-35). Cada ``LabelView`` lleva solo
identificadores, códigos de listas cerradas y marcas: ``evidence_ids`` son referencias (nunca
clave de almacén, URL ni bytes; el clip se pide aparte a ``EvidencePort``), y de la instantánea
del firmante (``labeled_by``, que en U-04 incluye ``display_name``) solo salen ``user_id`` y el
rol. Nada del contenido del registro fuente sale de aquí.

**Sin exportación** (BR-NUC-68). El puerto no tiene más operación que ``consultar``: no existe
exportación de etiquetas ni de imágenes para entrenamiento mientras las compuertas 1 y 5 no estén
firmadas; cuando se añada, será un tipo de registro y una clave de permiso nuevos.

**Permiso ``labels.read`` y alcance.** La matriz (``domain-entities.md`` §2.6) lo da solo a
``coordinator_sst`` y ``plant_manager``: solo cuentan las asignaciones de ``allowed_scopes`` con
uno de esos roles (organización, planta o zona), como en ``EvidencePort``. Un contexto sin
ninguna da ``LabelReadDenied`` y queda auditado con resultado ``denied``. Un filtro por una zona
fuera de ese alcance devuelve una página vacía (nunca ``forbidden``). La ruta ``GET /labels``
(TASK-137) aplica además ``authorize``.

**Auditoría** (BR-NUC-59, 62): cada consulta escribe exactamente una entrada ``label_read`` con
los filtros tal cual y ``result_count``, en la misma transacción que la consulta: si la entrada no
puede escribirse, la consulta no devuelve nada. Una entrada inválida lanza ``LabelQueryInvalid``
antes de tocar la base, sin consultar ni auditar; sin contexto, ``ContextAbsent``.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from pydantic import JsonValue
from sqlalchemy import text
from sqlalchemy.engine import Row

from vigia_platform.ledger.application.audit_writer import (
    MAX_FILTERS_BYTES,
    AuditOperation,
    AuditOutcome,
    AuditWriter,
)
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.ledger.canonical import CanonicalFormError, canonical_bytes_sync
from vigia_platform.shared.context import ContextAbsent, Role, ScopeContext, ScopeLevel

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "LABEL_READ_ROLES",
    "MAX_PAGE_SIZE",
    "LabelCursor",
    "LabelPage",
    "LabelPageRequest",
    "LabelPeriod",
    "LabelPort",
    "LabelQueryInvalid",
    "LabelReadDenied",
    "LabelService",
    "LabelView",
]

MAX_PAGE_SIZE: Final = 200
"""Página máxima (PAT-NUC-ESC-07), la misma que ``LectorExpediente``."""

DEFAULT_PAGE_SIZE: Final = 50
"""Página por defecto ``[objetivo propio]``."""

LABEL_READ_ROLES: Final[frozenset[Role]] = frozenset({Role.COORDINATOR_SST, Role.PLANT_MANAGER})
"""Roles con ``labels.read`` en la matriz (§2.6). La matriz en código llega con TASK-125."""

_SNAKE: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class LabelQueryInvalid(ValueError):
    """Filtros, periodo o página inválidos: no se consulta ni se audita nada."""

    code: Final = "query_invalid"

    def __init__(self, detail: str) -> None:
        super().__init__(f"consulta de etiquetas inválida: {detail}")


class LabelReadDenied(PermissionError):
    """El contexto no tiene ninguna asignación con ``labels.read``; queda auditado."""

    code: Final = "forbidden"

    def __init__(self) -> None:
        super().__init__("el contexto no tiene el permiso labels.read")


# --- Valores del puerto -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LabelPeriod:
    """Intervalo de ``labeled_at``: ``start`` inclusivo y ``end`` exclusivo, con zona horaria."""

    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class LabelCursor:
    """Clave de la última etiqueta de una página: la siguiente empieza justo después."""

    labeled_at: datetime
    label_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class LabelPageRequest:
    """Tamaño de página (1 a 200) y clave de la última etiqueta de la página anterior."""

    size: int = DEFAULT_PAGE_SIZE
    after: LabelCursor | None = None


@dataclass(frozen=True, slots=True)
class LabelView:
    """Una etiqueta tal como se consulta: solo identificadores, códigos y marcas.

    ``labeled_by_user_id`` y ``labeled_by_role`` son lo único que sale de la instantánea del
    firmante; ``None`` si la instantánea no los trae con forma de identificador o de rol.
    """

    label_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    source_record_id: uuid.UUID
    subject_record_id: uuid.UUID
    family: str
    outcome: str
    reason_category: str
    evidence_ids: tuple[uuid.UUID, ...]
    labeled_at: datetime
    labeled_by_user_id: uuid.UUID | None
    labeled_by_role: Role | None

    @property
    def cursor(self) -> LabelCursor:
        return LabelCursor(self.labeled_at, self.label_id)


@dataclass(frozen=True, slots=True)
class LabelPage:
    """Una página y la clave para pedir la siguiente (``None`` si no hay más)."""

    items: tuple[LabelView, ...]
    next_cursor: LabelCursor | None


class LabelPort(Protocol):
    """Puerto ``LabelPort`` (business-logic-model §10.1): solo consulta, nunca exportación."""

    async def consultar(
        self,
        context: ScopeContext,
        period: LabelPeriod,
        *,
        zone_id: uuid.UUID | None = None,
        family: str | None = None,
        reason_category: str | None = None,
        page: LabelPageRequest | None = None,
    ) -> LabelPage: ...


# --- Validación de entradas ---------------------------------------------------------------------


def _require_context(context: object) -> ScopeContext:
    if not isinstance(context, ScopeContext):
        raise ContextAbsent()
    return context


def _plain(value: uuid.UUID) -> uuid.UUID:
    """``uuid.UUID`` exacto (asyncpg devuelve una subclase propia; la auditoría exige el tipo)."""
    return uuid.UUID(int=value.int)


def _uuid(value: object, name: str) -> uuid.UUID | None:
    if value is None:
        return None
    if not isinstance(value, uuid.UUID):
        raise LabelQueryInvalid(f"{name} debe ser uuid.UUID")
    return _plain(value)


def _moment(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise LabelQueryInvalid(f"{name} debe ser una marca con zona horaria")
    return value.astimezone(UTC)


def _code(value: object, name: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or _SNAKE.fullmatch(value) is None:
        raise LabelQueryInvalid(f"{name} debe ser un código snake_case de a lo sumo 64")
    return value


def _stamp(moment: datetime) -> str:
    """Marca en UTC con microsegundos y ``Z``: el filtro auditado tal como se pidió."""
    return moment.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="microseconds") + "Z"


@dataclass(frozen=True, slots=True)
class _Query:
    parameters: dict[str, Any]
    audited: dict[str, JsonValue]
    size: int
    zone_id: uuid.UUID | None


def _query(
    period: object,
    zone_id: object,
    family: object,
    reason_category: object,
    page: object,
) -> _Query:
    if not isinstance(period, LabelPeriod):
        raise LabelQueryInvalid("period debe ser LabelPeriod")
    if not isinstance(page, LabelPageRequest):
        raise LabelQueryInvalid("page debe ser LabelPageRequest")
    start = _moment(period.start, "period.start")
    end = _moment(period.end, "period.end")
    if start >= end:
        raise LabelQueryInvalid("period: el inicio debe ser anterior al fin")
    size = page.size
    if type(size) is not int or not 1 <= size <= MAX_PAGE_SIZE:
        raise LabelQueryInvalid(f"la página debe tener de 1 a {MAX_PAGE_SIZE} elementos")
    zone = _uuid(zone_id, "zone_id")
    family_code = _code(family, "family")
    reason_code = _code(reason_category, "reason_category")
    after_labeled_at: datetime | None = None
    after_label_id: uuid.UUID | None = None
    if page.after is not None:
        if not isinstance(page.after, LabelCursor):
            raise LabelQueryInvalid("page.after debe ser LabelCursor")
        after_labeled_at = _moment(page.after.labeled_at, "page.after.labeled_at")
        after_label_id = _uuid(page.after.label_id, "page.after.label_id")
        if after_label_id is None:
            raise LabelQueryInvalid("page.after.label_id es obligatorio")
    audited: dict[str, JsonValue] = {"labeled_from": _stamp(start), "labeled_before": _stamp(end)}
    if zone is not None:
        audited["zone_id"] = str(zone)
    if family_code is not None:
        audited["family"] = family_code
    if reason_code is not None:
        audited["reason_category"] = reason_code
    audited["page_size"] = size
    if after_labeled_at is not None and after_label_id is not None:
        audited["after"] = {"labeled_at": _stamp(after_labeled_at), "label_id": str(after_label_id)}
    try:
        audited_size = len(canonical_bytes_sync(audited))
    except CanonicalFormError:  # pragma: no cover - solo identificadores, códigos y marcas
        raise LabelQueryInvalid("los filtros no son JSON canónico") from None
    if audited_size > MAX_FILTERS_BYTES:  # pragma: no cover - acotado por construcción
        raise LabelQueryInvalid(f"los filtros superan {MAX_FILTERS_BYTES} B")
    return _Query(
        parameters={
            "labeled_from": start,
            "labeled_before": end,
            "zone_id": zone,
            "family": family_code,
            "reason_category": reason_code,
            "after_labeled_at": after_labeled_at,
            "after_label_id": after_label_id,
            "limit": size + 1,
        },
        audited=audited,
        size=size,
        zone_id=zone,
    )


# --- Alcance ------------------------------------------------------------------------------------


def _scope_parameters(context: ScopeContext) -> dict[str, Any] | None:
    """Asignaciones con ``labels.read`` como parámetros; ``None`` si no hay ninguna."""
    scopes = [s for s in context.allowed_scopes if s.role in LABEL_READ_ROLES]
    if not scopes:
        return None
    return {
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


# --- Sentencia y conversión ---------------------------------------------------------------------

_LIST_LABELS: Final = text(
    "SELECT l.label_id, l.plant_id, l.zone_id, l.source_record_id, l.subject_record_id,"
    " l.family, l.outcome, l.reason_category, l.evidence_ids, l.labeled_at,"
    " l.labeled_by ->> 'user_id' AS labeled_by_user_id,"
    " COALESCE(l.labeled_by ->> 'role_in_use', l.labeled_by ->> 'role') AS labeled_by_role"
    " FROM ledger.label AS l"
    " WHERE l.organization_id = :organization_id"
    " AND (CAST(:whole_organization AS boolean)"
    " OR l.plant_id = ANY(CAST(:scope_plants AS uuid[]))"
    " OR l.zone_id = ANY(CAST(:scope_zones AS uuid[])))"
    " AND l.labeled_at >= CAST(:labeled_from AS timestamptz)"
    " AND l.labeled_at < CAST(:labeled_before AS timestamptz)"
    " AND (CAST(:zone_id AS uuid) IS NULL OR l.zone_id = CAST(:zone_id AS uuid))"
    " AND (CAST(:family AS text) IS NULL OR l.family = CAST(:family AS text))"
    " AND (CAST(:reason_category AS text) IS NULL"
    " OR l.reason_category = CAST(:reason_category AS text))"
    " AND (CAST(:after_labeled_at AS timestamptz) IS NULL"
    " OR (l.labeled_at, l.label_id)"
    " < (CAST(:after_labeled_at AS timestamptz), CAST(:after_label_id AS uuid)))"
    " ORDER BY l.labeled_at DESC, l.label_id DESC"
    " LIMIT :limit"
)


def _signer_id(value: object) -> uuid.UUID | None:
    if type(value) is not str:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _signer_role(value: object) -> Role | None:
    try:
        return Role(value) if type(value) is str else None
    except ValueError:
        return None


def _label(row: Row[Any]) -> LabelView:
    return LabelView(
        label_id=_plain(row.label_id),
        plant_id=_plain(row.plant_id),
        zone_id=_plain(row.zone_id),
        source_record_id=_plain(row.source_record_id),
        subject_record_id=_plain(row.subject_record_id),
        family=row.family,
        outcome=row.outcome,
        reason_category=row.reason_category,
        evidence_ids=tuple(_plain(value) for value in row.evidence_ids),
        labeled_at=row.labeled_at,
        labeled_by_user_id=_signer_id(row.labeled_by_user_id),
        labeled_by_role=_signer_role(row.labeled_by_role),
    )


# --- El puerto --------------------------------------------------------------------------------


class LabelService:
    """``LabelPort`` sobre PostgreSQL."""

    def __init__(self, *, database: LedgerDatabase, audit: AuditWriter) -> None:
        self._database = database
        self._audit = audit

    async def consultar(
        self,
        context: ScopeContext,
        period: LabelPeriod,
        *,
        zone_id: uuid.UUID | None = None,
        family: str | None = None,
        reason_category: str | None = None,
        page: LabelPageRequest | None = None,
    ) -> LabelPage:
        """Una página de etiquetas con alcance; escribe una entrada ``label_read``."""
        context = _require_context(context)
        query = _query(period, zone_id, family, reason_category, page or LabelPageRequest())
        scopes = _scope_parameters(context)
        if scopes is None:
            await self._audit.append(
                context,
                AuditOperation.LABEL_READ,
                outcome=AuditOutcome.DENIED,
                zone_id=query.zone_id,
                filters=query.audited,
                result_count=0,
            )
            raise LabelReadDenied()
        parameters: Mapping[str, Any] = {
            **query.parameters,
            **scopes,
            "organization_id": context.organization_id,
        }
        async with self._database.transaction(context) as transaction:
            rows = list((await transaction.execute(_LIST_LABELS, parameters)).all())
            more = len(rows) > query.size
            rows = rows[: query.size]
            await self._audit.append(
                context,
                AuditOperation.LABEL_READ,
                zone_id=query.zone_id,
                filters=query.audited,
                result_count=len(rows),
                transaction=transaction,
            )
        items = tuple(_label(row) for row in rows)
        return LabelPage(items=items, next_cursor=items[-1].cursor if more and items else None)
