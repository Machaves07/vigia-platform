"""``catalog.family_admission`` sobre PostgreSQL (LC-GOB-02; tabla de ``gob_0017``).

Toda operación recibe un ``ScopeContext`` o una ``Transaction`` abierta con él: la seguridad a
nivel de fila limita a la organización del contexto (``organization_isolation``) y, bajo
concesión, al alcance concedido. La **planta** no la filtra la RLS: cada sentencia la nombra, y
también la organización del contexto (defensa en profundidad), de modo que una admisión de la
planta A nunca satisface la consulta de la planta B (BR-GOB-16).

La tabla es de solo anexar: aquí solo hay ``INSERT`` y ``SELECT``. La unicidad de la admitida
por (organización, planta, familia) la garantiza el índice único parcial
``family_admission_admitted_once`` (``FAMILY_ADMITTED_ONCE``); el servicio traduce su violación
a ``family_already_admitted``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.engine import Row
from vigia_contracts.models.enumerations import PredicateFamily

from vigia_platform.catalog.domain.admission import AdmissionAnswers, FamilyAdmission
from vigia_platform.catalog.domain.enums import AdmissionCriterion, AdmissionResult
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.context import Role, ScopeContext, repository
from vigia_platform.shared.db import Transaction

__all__ = ["FAMILY_ADMITTED_ONCE", "AdmissionCursor", "PostgresAdmissionRepository"]

FAMILY_ADMITTED_ONCE: Final = "family_admission_admitted_once"
"""Índice único parcial ``(organization_id, plant_id, family) WHERE result = 'admitted'``."""

_PLANT: Final = text(
    "SELECT plant_id FROM identity.plant"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
)
# Las tres lecturas repiten la lista de columnas literal: ``text()`` no admite concatenar
# (VIG001, ``tools/lint_rules.py``).
_ADMITTED: Final = text(
    "SELECT admission_id, organization_id, plant_id, family,"
    " (answers ->> 'standard')::boolean AS standard,"
    " (answers ->> 'remedy')::boolean AS remedy,"
    " (answers ->> 'subject')::boolean AS subject,"
    " justification_es, result, failed_criterion, evaluated_by, role_in_use, evaluated_at,"
    " ledger_record_id FROM catalog.family_admission"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " AND family = :family AND result = 'admitted'"
)
_INSERT: Final = text(
    "INSERT INTO catalog.family_admission (admission_id, organization_id, plant_id, family,"
    " answers, justification_es, result, failed_criterion, evaluated_by, role_in_use,"
    " evaluated_at, ledger_record_id)"
    " VALUES (:admission_id, :organization_id, :plant_id, :family,"
    " jsonb_build_object('standard', CAST(:standard AS boolean),"
    " 'remedy', CAST(:remedy AS boolean), 'subject', CAST(:subject AS boolean)),"
    " :justification_es, :result, :failed_criterion, :evaluated_by, :role_in_use,"
    " :evaluated_at, :ledger_record_id)"
)
_PAGE: Final = text(
    "SELECT admission_id, organization_id, plant_id, family,"
    " (answers ->> 'standard')::boolean AS standard,"
    " (answers ->> 'remedy')::boolean AS remedy,"
    " (answers ->> 'subject')::boolean AS subject,"
    " justification_es, result, failed_criterion, evaluated_by, role_in_use, evaluated_at,"
    " ledger_record_id FROM catalog.family_admission"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " ORDER BY evaluated_at DESC, admission_id DESC LIMIT :limit"
)
_PAGE_AFTER: Final = text(
    "SELECT admission_id, organization_id, plant_id, family,"
    " (answers ->> 'standard')::boolean AS standard,"
    " (answers ->> 'remedy')::boolean AS remedy,"
    " (answers ->> 'subject')::boolean AS subject,"
    " justification_es, result, failed_criterion, evaluated_by, role_in_use, evaluated_at,"
    " ledger_record_id FROM catalog.family_admission"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " AND (evaluated_at, admission_id) < (:after_at, :after_id)"
    " ORDER BY evaluated_at DESC, admission_id DESC LIMIT :limit"
)


@dataclass(frozen=True, slots=True)
class AdmissionCursor:
    """Clave de la última admisión de una página (la más reciente primero)."""

    evaluated_at: datetime
    admission_id: uuid.UUID

    def __post_init__(self) -> None:
        if not isinstance(self.evaluated_at, datetime) or self.evaluated_at.utcoffset() is None:
            raise ValueError("evaluated_at del cursor debe llevar zona horaria")
        if type(self.admission_id) is not uuid.UUID:
            raise TypeError("admission_id del cursor debe ser uuid.UUID")


def _uuid(value: object) -> uuid.UUID:
    """asyncpg devuelve su propio tipo de UUID; el dominio exige ``uuid.UUID``."""
    return uuid.UUID(str(value))


def _admission(row: Row[Any]) -> FamilyAdmission:
    return FamilyAdmission(
        admission_id=_uuid(row.admission_id),
        organization_id=_uuid(row.organization_id),
        plant_id=_uuid(row.plant_id),
        family=PredicateFamily(row.family),
        answers=AdmissionAnswers(
            standard=bool(row.standard), remedy=bool(row.remedy), subject=bool(row.subject)
        ),
        justification_es=row.justification_es,
        result=AdmissionResult(row.result),
        failed_criterion=None
        if row.failed_criterion is None
        else AdmissionCriterion(row.failed_criterion),
        evaluated_by=_uuid(row.evaluated_by),
        role_in_use=Role(row.role_in_use),
        evaluated_at=row.evaluated_at,
        ledger_record_id=_uuid(row.ledger_record_id),
    )


@repository
class PostgresAdmissionRepository:
    """Lecturas y altas de ``catalog.family_admission``."""

    def __init__(self, database: LedgerDatabase) -> None:
        self._database = database

    async def plant_exists(self, context: ScopeContext, plant_id: uuid.UUID) -> bool:
        """¿Existe la planta en la organización del contexto (y la deja ver la RLS)?"""
        rows = await self._database.read(
            context, _PLANT, {"organization_id": context.organization_id, "plant_id": plant_id}
        )
        return bool(rows)

    async def admitted(
        self, context: ScopeContext, plant_id: uuid.UUID, family: PredicateFamily
    ) -> FamilyAdmission | None:
        """La admisión ``admitted`` de (planta, familia), o ``None``."""
        rows = await self._database.read(context, _ADMITTED, _key(context, plant_id, family))
        return _admission(rows[0]) if rows else None

    async def admitted_in(
        self, transaction: Transaction, plant_id: uuid.UUID, family: PredicateFamily
    ) -> bool:
        """¿Hay ya una ``admitted`` de (planta, familia)? Dentro de la transacción de la alta."""
        result = await transaction.execute(_ADMITTED, _key(transaction.context, plant_id, family))
        return result.first() is not None

    async def insert(self, transaction: Transaction, admission: FamilyAdmission) -> None:
        """Anexa la evaluación; la violación del índice único sale como ``IntegrityError``."""
        if admission.organization_id != transaction.context.organization_id:
            raise ValueError("la admisión es de otra organización que la transacción")
        await transaction.execute(
            _INSERT,
            {
                "admission_id": admission.admission_id,
                "organization_id": admission.organization_id,
                "plant_id": admission.plant_id,
                "family": admission.family.value,
                **admission.answers.as_dict(),
                "justification_es": admission.justification_es,
                "result": admission.result.value,
                "failed_criterion": None
                if admission.failed_criterion is None
                else admission.failed_criterion.value,
                "evaluated_by": admission.evaluated_by,
                "role_in_use": admission.role_in_use.value,
                "evaluated_at": admission.evaluated_at,
                "ledger_record_id": admission.ledger_record_id,
            },
        )

    async def page(
        self,
        transaction: Transaction,
        plant_id: uuid.UUID,
        *,
        after: AdmissionCursor | None,
        limit: int,
    ) -> Sequence[FamilyAdmission]:
        """Hasta ``limit`` evaluaciones de la planta, la más reciente primero, tras ``after``.

        En la transacción del llamador: la lectura bajo concesión se audita en la misma.
        """
        parameters: dict[str, Any] = {
            "organization_id": transaction.context.organization_id,
            "plant_id": plant_id,
            "limit": limit,
        }
        statement = _PAGE
        if after is not None:
            statement = _PAGE_AFTER
            parameters.update(after_at=after.evaluated_at, after_id=after.admission_id)
        result = await transaction.execute(statement, parameters)
        return tuple(_admission(row) for row in result.all())


def _key(context: ScopeContext, plant_id: uuid.UUID, family: PredicateFamily) -> dict[str, Any]:
    return {
        "organization_id": context.organization_id,
        "plant_id": plant_id,
        "family": PredicateFamily(family).value,
    }
