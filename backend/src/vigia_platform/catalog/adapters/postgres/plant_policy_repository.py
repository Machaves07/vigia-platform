"""Política de planta sobre PostgreSQL (DE §2.10; ``catalog.plant_policy`` ⛓).

Solo ``INSERT`` y ``SELECT``: una versión nueva se anexa y la anterior permanece. **Exclusión**
entre cargas de la misma planta: ``lock_plant`` toma ``pg_advisory_xact_lock`` antes de leer la
última versión, así que la segunda carga espera y compara su versión con la que dejó la primera.
La restricción ``plant_policy_version_unique (plant_id, version)`` es el respaldo: dos políticas
con el mismo número nunca se confirman. Toda sentencia nombra la organización del contexto y la
planta (defensa en profundidad sobre la RLS).
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.engine import Row

from vigia_platform.catalog.domain.documents import DocumentRef
from vigia_platform.catalog.domain.plant_policy import PlantPolicy
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.db import Transaction

__all__ = ["PLANT_POLICY_VERSION_UNIQUE", "PostgresPlantPolicyRepository"]

PLANT_POLICY_VERSION_UNIQUE: Final = "plant_policy_version_unique"

_PLANT: Final = text(
    "SELECT plant_id FROM identity.plant"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
)
_LOCK_PLANT: Final = text(
    "SELECT pg_advisory_xact_lock(hashtextextended('plant_policy|' || :plant_id, 0))"
)
_LATEST: Final = text(
    "SELECT policy_id, organization_id, plant_id, version, signed_at, signed_by_display_name,"
    " legal_opinion_reference, criteria_summary_es, document_ref, loaded_by, loaded_at,"
    " ledger_record_id FROM catalog.plant_policy"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " ORDER BY version DESC LIMIT 1"
)
_LOADED: Final = text(
    "SELECT 1 FROM catalog.plant_policy"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id LIMIT 1"
)
_INSERT: Final = text(
    "INSERT INTO catalog.plant_policy (policy_id, organization_id, plant_id, version, signed_at,"
    " signed_by_display_name, legal_opinion_reference, document_ref, criteria_summary_es,"
    " loaded_by, loaded_at, ledger_record_id)"
    " VALUES (:policy_id, :organization_id, :plant_id, :version, :signed_at,"
    " :signed_by_display_name, :legal_opinion_reference, CAST(:document_ref AS jsonb),"
    " :criteria_summary_es, :loaded_by, :loaded_at, :ledger_record_id)"
)


def _uuid(value: object) -> uuid.UUID:
    return uuid.UUID(str(value))


def _json(value: object) -> Any:
    return json.loads(value) if isinstance(value, str | bytes) else value


def _policy(row: Row[Any]) -> PlantPolicy:
    return PlantPolicy(
        policy_id=_uuid(row.policy_id),
        organization_id=_uuid(row.organization_id),
        plant_id=_uuid(row.plant_id),
        version=int(row.version),
        signed_at=row.signed_at,
        signed_by_display_name=row.signed_by_display_name,
        legal_opinion_reference=row.legal_opinion_reference,
        criteria_summary_es=row.criteria_summary_es,
        document_ref=DocumentRef.parse(_json(row.document_ref)),
        loaded_by=_uuid(row.loaded_by),
        loaded_at=row.loaded_at,
        ledger_record_id=_uuid(row.ledger_record_id),
    )


def _plant_key(context: ScopeContext, plant_id: uuid.UUID) -> dict[str, Any]:
    return {"organization_id": context.organization_id, "plant_id": plant_id}


@repository
class PostgresPlantPolicyRepository:
    """Versiones de la política de hallazgos incerrables de una planta."""

    def __init__(self, database: LedgerDatabase) -> None:
        self._database = database

    async def plant_exists(self, context: ScopeContext, plant_id: uuid.UUID) -> bool:
        """¿Existe la planta en la organización del contexto (y la deja ver la RLS)?"""
        return bool(await self._database.read(context, _PLANT, _plant_key(context, plant_id)))

    async def lock_plant(self, transaction: Transaction, plant_id: uuid.UUID) -> None:
        """Exclusión entre cargas de política de la planta hasta el fin de la transacción."""
        await transaction.execute(_LOCK_PLANT, {"plant_id": str(plant_id)})

    async def latest(self, transaction: Transaction, plant_id: uuid.UUID) -> PlantPolicy | None:
        """La última versión cargada de la planta, dentro de la transacción."""
        result = await transaction.execute(_LATEST, _plant_key(transaction.context, plant_id))
        row = result.first()
        return None if row is None else _policy(row)

    async def loaded(self, transaction: Transaction, plant_id: uuid.UUID) -> bool:
        """¿Tiene la planta alguna política cargada? (``plant_policy_loaded_at_signing``)."""
        result = await transaction.execute(_LOADED, _plant_key(transaction.context, plant_id))
        return result.first() is not None

    async def read_latest(self, context: ScopeContext, plant_id: uuid.UUID) -> PlantPolicy | None:
        """La última versión cargada, fuera de una transacción de escritura."""
        rows = await self._database.read(context, _LATEST, _plant_key(context, plant_id))
        return _policy(rows[0]) if rows else None

    async def insert(self, transaction: Transaction, policy: PlantPolicy) -> None:
        """Anexa la versión; una versión repetida sale como ``IntegrityError``."""
        if policy.organization_id != transaction.context.organization_id:
            raise ValueError("la política es de otra organización que la transacción")
        await transaction.execute(
            _INSERT,
            {
                "policy_id": policy.policy_id,
                "organization_id": policy.organization_id,
                "plant_id": policy.plant_id,
                "version": policy.version,
                "signed_at": policy.signed_at,
                "signed_by_display_name": policy.signed_by_display_name,
                "legal_opinion_reference": policy.legal_opinion_reference,
                "document_ref": json.dumps(policy.document_ref.to_json()),
                "criteria_summary_es": policy.criteria_summary_es,
                "loaded_by": policy.loaded_by,
                "loaded_at": policy.loaded_at,
                "ledger_record_id": policy.ledger_record_id,
            },
        )
