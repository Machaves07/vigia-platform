"""Política de firmantes, acuerdos de uso y confirmaciones sobre PostgreSQL (LC-GOB-04).

- ``plant_signatory_policy`` 🔒: una fila por planta que se inserta o se actualiza (``vigia_app``
  no tiene ``DELETE``).
- ``use_agreement`` ⛓: solo ``INSERT`` y los cierres de nulo a valor que admite la lista blanca de
  ``gob_0017`` (``approved_at``, ``approved_by``, ``ledger_record_id``, ``superseded_at``,
  ``revoked_at``) con su estado solo hacia adelante. Cada cierre es **condicional** al estado que
  se leyó: un estado viejo nunca escribe (``AgreementWriteConflict``).
- ``agreement_confirmation`` ⛓: la clave ``(agreement_id, user_id)`` deja una sola confirmación
  por firmante; ``INSERT … ON CONFLICT DO NOTHING`` hace que la repetida no escriba nada.

Las lecturas de la aprobación (acta de comisionamiento cerrada, confirmaciones, acuerdo vigente)
van dentro de su transacción, bajo la exclusión de la proyección de la zona que toma
``catalog.gates``. Toda sentencia recibe una ``Transaction`` abierta con el ``ScopeContext`` (RLS
por organización y concesión) y además nombra la organización del contexto y la zona o el acuerdo
pedidos (defensa en profundidad), así que un acuerdo nunca responde por el de otra zona.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.engine import Row

from vigia_platform.catalog.domain.agreements import (
    AgreementConfirmation,
    SignatoryPolicy,
    UseAgreement,
    signatory_from_json,
)
from vigia_platform.catalog.domain.documents import DocumentRef
from vigia_platform.catalog.domain.enums import AgreementStatus, ConfirmationOrigin
from vigia_platform.shared.context import Role, ScopeContext, repository
from vigia_platform.shared.db import Transaction

__all__ = ["AgreementWriteConflict", "PostgresAgreementRepository", "ScopeRecordView"]

_POLICY: Final = text(
    "SELECT organization_id, plant_id, required_roles, minimum, updated_by, updated_at"
    " FROM catalog.plant_signatory_policy"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
)
_UPSERT_POLICY: Final = text(
    "INSERT INTO catalog.plant_signatory_policy (plant_id, organization_id, required_roles,"
    " minimum, updated_by, updated_at)"
    " VALUES (:plant_id, :organization_id, CAST(:required_roles AS text[]), :minimum,"
    " :updated_by, :updated_at)"
    " ON CONFLICT (plant_id) DO UPDATE SET required_roles = EXCLUDED.required_roles,"
    " minimum = EXCLUDED.minimum, updated_by = EXCLUDED.updated_by,"
    " updated_at = EXCLUDED.updated_at"
    " WHERE catalog.plant_signatory_policy.organization_id = EXCLUDED.organization_id"
)
# Las lecturas repiten la lista de columnas literal: ``text()`` no admite concatenar (VIG001).
_AGREEMENT: Final = text(
    "SELECT agreement_id, organization_id, plant_id, zone_id, status, signatories, document_ref,"
    " replaces_agreement_id, created_by, created_at, approved_at, approved_by, ledger_record_id,"
    " superseded_at, revoked_at FROM catalog.use_agreement"
    " WHERE organization_id = :organization_id AND agreement_id = :agreement_id"
)
_CURRENT: Final = text(
    "SELECT agreement_id, organization_id, plant_id, zone_id, status, signatories, document_ref,"
    " replaces_agreement_id, created_by, created_at, approved_at, approved_by, ledger_record_id,"
    " superseded_at, revoked_at FROM catalog.use_agreement"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id AND status = 'approved'"
    " ORDER BY approved_at DESC, agreement_id DESC"
)
_PENDING_FOR: Final = text(
    "SELECT a.agreement_id FROM catalog.use_agreement AS a"
    " WHERE a.organization_id = :organization_id AND a.zone_id = :zone_id"
    " AND a.status = 'pending_signatures'"
    " AND a.signatories @> CAST(:signatory AS jsonb)"
    " AND NOT EXISTS (SELECT 1 FROM catalog.agreement_confirmation AS c"
    " WHERE c.organization_id = a.organization_id AND c.agreement_id = a.agreement_id"
    " AND c.user_id = :user_id)"
    " ORDER BY a.created_at DESC, a.agreement_id DESC LIMIT 1"
)
_INSERT_AGREEMENT: Final = text(
    "INSERT INTO catalog.use_agreement (agreement_id, organization_id, plant_id, zone_id, status,"
    " signatories, document_ref, replaces_agreement_id, created_by, created_at)"
    " VALUES (:agreement_id, :organization_id, :plant_id, :zone_id, 'pending_signatures',"
    " CAST(:signatories AS jsonb), CAST(:document_ref AS jsonb), :replaces_agreement_id,"
    " :created_by, :created_at)"
)
_APPROVE: Final = text(
    "UPDATE catalog.use_agreement SET status = 'approved', approved_at = :approved_at,"
    " approved_by = :approved_by, ledger_record_id = :ledger_record_id"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id"
    " AND agreement_id = :agreement_id AND status = 'pending_signatures'"
)
_SUPERSEDE: Final = text(
    "UPDATE catalog.use_agreement SET status = 'superseded', superseded_at = :at"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id"
    " AND agreement_id = :agreement_id AND status = 'approved'"
)
_REVOKE_CURRENT: Final = text(
    "UPDATE catalog.use_agreement SET status = 'revoked', revoked_at = :at"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id AND status = 'approved'"
)
_CONFIRM: Final = text(
    "INSERT INTO catalog.agreement_confirmation (agreement_id, user_id, organization_id,"
    " plant_id, role_in_use, confirmed_at, origin)"
    " VALUES (:agreement_id, :user_id, :organization_id, :plant_id, :role_in_use,"
    " :confirmed_at, :origin)"
    " ON CONFLICT DO NOTHING"
)
_CONFIRMATIONS: Final = text(
    "SELECT agreement_id, user_id, organization_id, plant_id, role_in_use, confirmed_at, origin"
    " FROM catalog.agreement_confirmation"
    " WHERE organization_id = :organization_id AND agreement_id = :agreement_id"
    " ORDER BY confirmed_at, user_id"
)
_CLOSED_RECORD: Final = text(
    "SELECT commissioning_record_id FROM catalog.commissioning_record"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id"
    " ORDER BY closed_at DESC, commissioning_record_id DESC LIMIT 1"
)
_SCOPE_RECORD: Final = text(
    "SELECT scope_text_es, cameras FROM catalog.mounting_gate_record"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id AND record_id = :record_id"
)


class AgreementWriteConflict(Exception):
    """El acuerdo ya no está en el estado que se leyó: otra operación llegó antes."""

    def __init__(self) -> None:
        super().__init__("el acuerdo ya no está en el estado que se leyó")


def _uuid(value: object) -> uuid.UUID:
    """asyncpg devuelve su propio tipo de UUID; el dominio exige ``uuid.UUID``."""
    return uuid.UUID(str(value))


def _optional_uuid(value: object) -> uuid.UUID | None:
    return None if value is None else _uuid(value)


def _json(value: object) -> Any:
    """``jsonb`` llega ya decodificado con asyncpg; como texto, se decodifica aquí."""
    return json.loads(value) if isinstance(value, str | bytes) else value


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _rowcount(result: Any) -> int:
    count: int = result.rowcount
    return count


def _policy(row: Row[Any]) -> SignatoryPolicy:
    return SignatoryPolicy(
        organization_id=_uuid(row.organization_id),
        plant_id=_uuid(row.plant_id),
        required_roles=tuple(Role(role) for role in row.required_roles),
        minimum=int(row.minimum),
        updated_by=_uuid(row.updated_by),
        updated_at=row.updated_at,
    )


def _agreement(row: Row[Any]) -> UseAgreement:
    document = _json(row.document_ref)
    return UseAgreement(
        agreement_id=_uuid(row.agreement_id),
        organization_id=_uuid(row.organization_id),
        plant_id=_uuid(row.plant_id),
        zone_id=_uuid(row.zone_id),
        status=AgreementStatus(row.status),
        signatories=tuple(signatory_from_json(s) for s in _json(row.signatories)),
        document_ref=None if document is None else DocumentRef.parse(document),
        replaces_agreement_id=_optional_uuid(row.replaces_agreement_id),
        created_by=_uuid(row.created_by),
        created_at=row.created_at,
        approved_at=row.approved_at,
        approved_by=_optional_uuid(row.approved_by),
        ledger_record_id=_optional_uuid(row.ledger_record_id),
        superseded_at=row.superseded_at,
        revoked_at=row.revoked_at,
    )


def _confirmation(row: Row[Any]) -> AgreementConfirmation:
    return AgreementConfirmation(
        agreement_id=_uuid(row.agreement_id),
        user_id=_uuid(row.user_id),
        organization_id=_uuid(row.organization_id),
        plant_id=_uuid(row.plant_id),
        role_in_use=Role(row.role_in_use),
        confirmed_at=row.confirmed_at,
        origin=ConfirmationOrigin(row.origin),
    )


class ScopeRecordView:
    """Lo que la transparencia muestra del acta de alcance: texto y encuadres declarados."""

    __slots__ = ("cameras", "scope_text_es")

    def __init__(self, scope_text_es: str, cameras: tuple[Mapping[str, Any], ...]) -> None:
        self.scope_text_es = scope_text_es
        self.cameras = cameras


@repository
class PostgresAgreementRepository:
    """Política de firmantes, acuerdos y confirmaciones; lecturas de la aprobación."""

    # --- Política de firmantes -------------------------------------------------------------------

    async def policy(self, transaction: Transaction, plant_id: uuid.UUID) -> SignatoryPolicy | None:
        result = await transaction.execute(_POLICY, _plant_key(transaction.context, plant_id))
        row = result.first()
        return None if row is None else _policy(row)

    async def save_policy(self, transaction: Transaction, policy: SignatoryPolicy) -> None:
        """Inserta o actualiza la política de la planta."""
        if policy.organization_id != transaction.context.organization_id:
            raise ValueError("la política es de otra organización que la transacción")
        result = await transaction.execute(
            _UPSERT_POLICY,
            {
                "plant_id": policy.plant_id,
                "organization_id": policy.organization_id,
                "required_roles": [role.value for role in policy.required_roles],
                "minimum": policy.minimum,
                "updated_by": policy.updated_by,
                "updated_at": policy.updated_at,
            },
        )
        if _rowcount(result) != 1:
            raise AgreementWriteConflict

    # --- Acuerdos --------------------------------------------------------------------------------

    async def agreement(
        self, transaction: Transaction, agreement_id: uuid.UUID
    ) -> UseAgreement | None:
        """El acuerdo en la organización de la transacción (si la RLS lo deja ver), o ``None``."""
        result = await transaction.execute(
            _AGREEMENT,
            {"organization_id": transaction.context.organization_id, "agreement_id": agreement_id},
        )
        row = result.first()
        return None if row is None else _agreement(row)

    async def current(self, transaction: Transaction, zone_id: uuid.UUID) -> UseAgreement | None:
        """El acuerdo ``approved`` de la zona (el vigente), o ``None``."""
        result = await transaction.execute(_CURRENT, _zone_key(transaction.context, zone_id))
        rows = result.all()
        if len(rows) > 1:  # la exclusión de la proyección lo impide: una historia rota
            raise ValueError("la zona tiene más de un acuerdo vigente")
        return _agreement(rows[0]) if rows else None

    async def pending_for(
        self, transaction: Transaction, zone_id: uuid.UUID, user_id: uuid.UUID
    ) -> uuid.UUID | None:
        """El acuerdo más reciente de la zona que espera la confirmación de ``user_id``."""
        result = await transaction.execute(
            _PENDING_FOR,
            {
                **_zone_key(transaction.context, zone_id),
                "signatory": _dumps([{"user_id": str(user_id)}]),
                "user_id": user_id,
            },
        )
        row = result.first()
        return None if row is None else _uuid(row.agreement_id)

    async def insert(self, transaction: Transaction, agreement: UseAgreement) -> None:
        """Anexa el acuerdo en ``pending_signatures``."""
        if agreement.organization_id != transaction.context.organization_id:
            raise ValueError("el acuerdo es de otra organización que la transacción")
        if agreement.status is not AgreementStatus.PENDING_SIGNATURES:
            raise ValueError("un acuerdo nace pendiente de firmas")
        document = agreement.document_ref
        await transaction.execute(
            _INSERT_AGREEMENT,
            {
                "agreement_id": agreement.agreement_id,
                "organization_id": agreement.organization_id,
                "plant_id": agreement.plant_id,
                "zone_id": agreement.zone_id,
                "signatories": _dumps([s.as_json() for s in agreement.signatories]),
                "document_ref": None if document is None else _dumps(document.to_json()),
                "replaces_agreement_id": agreement.replaces_agreement_id,
                "created_by": agreement.created_by,
                "created_at": agreement.created_at,
            },
        )

    async def approve(
        self,
        transaction: Transaction,
        agreement: UseAgreement,
        *,
        approved_at: datetime,
        approved_by: uuid.UUID,
        ledger_record_id: uuid.UUID,
    ) -> None:
        """``pending_signatures → approved``; si ya no lo estaba, ``AgreementWriteConflict``."""
        result = await transaction.execute(
            _APPROVE,
            {
                **_zone_key(transaction.context, agreement.zone_id),
                "agreement_id": agreement.agreement_id,
                "approved_at": approved_at,
                "approved_by": approved_by,
                "ledger_record_id": ledger_record_id,
            },
        )
        if _rowcount(result) != 1:
            raise AgreementWriteConflict

    async def supersede(
        self, transaction: Transaction, zone_id: uuid.UUID, agreement_id: uuid.UUID, at: datetime
    ) -> None:
        """``approved → superseded``; si ya no estaba aprobado, ``AgreementWriteConflict``."""
        result = await transaction.execute(
            _SUPERSEDE,
            {**_zone_key(transaction.context, zone_id), "agreement_id": agreement_id, "at": at},
        )
        if _rowcount(result) != 1:
            raise AgreementWriteConflict

    async def revoke_current(
        self, transaction: Transaction, zone_id: uuid.UUID, at: datetime
    ) -> int:
        """``approved → revoked`` del acuerdo vigente de la zona; devuelve cuántos (0 o 1)."""
        result = await transaction.execute(
            _REVOKE_CURRENT, {**_zone_key(transaction.context, zone_id), "at": at}
        )
        return _rowcount(result)

    # --- Confirmaciones --------------------------------------------------------------------------

    async def confirm(self, transaction: Transaction, confirmation: AgreementConfirmation) -> bool:
        """Anexa la confirmación; ``False`` si el firmante ya había confirmado (nada escrito)."""
        if confirmation.organization_id != transaction.context.organization_id:
            raise ValueError("la confirmación es de otra organización que la transacción")
        result = await transaction.execute(
            _CONFIRM,
            {
                "agreement_id": confirmation.agreement_id,
                "user_id": confirmation.user_id,
                "organization_id": confirmation.organization_id,
                "plant_id": confirmation.plant_id,
                "role_in_use": Role(confirmation.role_in_use).value,
                "confirmed_at": confirmation.confirmed_at,
                "origin": ConfirmationOrigin(confirmation.origin).value,
            },
        )
        return _rowcount(result) == 1

    async def confirmations(
        self, transaction: Transaction, agreement_id: uuid.UUID
    ) -> tuple[AgreementConfirmation, ...]:
        result = await transaction.execute(
            _CONFIRMATIONS,
            {"organization_id": transaction.context.organization_id, "agreement_id": agreement_id},
        )
        return tuple(_confirmation(row) for row in result.all())

    # --- Lecturas de la aprobación y la transparencia --------------------------------------------

    async def closed_commissioning_record(
        self, transaction: Transaction, zone_id: uuid.UUID
    ) -> uuid.UUID | None:
        """El acta de comisionamiento cerrada más reciente de la zona, o ``None``."""
        result = await transaction.execute(_CLOSED_RECORD, _zone_key(transaction.context, zone_id))
        row = result.first()
        return None if row is None else _uuid(row.commissioning_record_id)

    async def scope_record(
        self, transaction: Transaction, zone_id: uuid.UUID, record_id: uuid.UUID
    ) -> ScopeRecordView | None:
        """El acta de alcance ``record_id`` de la zona, o ``None``."""
        result = await transaction.execute(
            _SCOPE_RECORD, {**_zone_key(transaction.context, zone_id), "record_id": record_id}
        )
        row = result.first()
        if row is None:
            return None
        return ScopeRecordView(row.scope_text_es, tuple(_json(row.cameras)))


def _plant_key(context: ScopeContext, plant_id: uuid.UUID) -> dict[str, Any]:
    return {"organization_id": context.organization_id, "plant_id": plant_id}


def _zone_key(context: ScopeContext, zone_id: uuid.UUID) -> dict[str, Any]:
    return {"organization_id": context.organization_id, "zone_id": zone_id}
