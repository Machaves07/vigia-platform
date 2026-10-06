"""``fleet.node_credential`` y el alta en ``node_fleet_record`` sobre PostgreSQL (TASK-219).

Toda operación recibe una ``Transaction`` abierta con el ``ScopeContext`` de la organización del
nodo: la seguridad a nivel de fila limita a esa organización y cada sentencia la nombra además
(defensa en profundidad). Solo metadatos del certificado: nunca material de clave (BR-CTR-46).

- ``credentials``: todas las credenciales del nodo con su ``rotated_from``;
- ``locked``: la credencial ``(node_id, certificate_serial)`` bloqueada (``FOR UPDATE``): la de la
  rotación, ya autenticada por ``node_api``;
- ``insert``: la credencial nueva ``active`` (el sujeto, con ``subject_document``);
- ``transition``: ``UPDATE … WHERE status = :expected``, cuyo éxito es **una** fila (las
  transiciones que admite ``gob_0018``: ``active → overlapping``, ``overlapping → superseded``);
- ``mark_enrolled``: ``enrolled_at`` y la huella de hardware en ``node_fleet_record``. La huella
  solo se fija si estaba vacía o ya era esa (la re-alta exige la misma, G-2): con otra, ninguna
  fila cambia y el alta se revierte;
- ``revocation_facts``: las credenciales ``revoked``, ``superseded`` y ``overlapping`` no vencidas
  de la organización, con el ``issued_at`` de su sucesora: lo que lee el barrido de la lista de
  revocación (TASK-220) en la transacción de solo lectura de cada organización.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.engine import Row

from vigia_platform.fleet.domain.enums import CredentialStatus
from vigia_platform.fleet.domain.node_credential import KEY_ALGORITHM, NodeCredential
from vigia_platform.fleet.domain.node_subject import subject_document
from vigia_platform.fleet.domain.revocation_list import CredentialRevocationFacts
from vigia_platform.shared.context import repository
from vigia_platform.shared.db import Transaction

__all__ = ["PostgresCredentialStore"]

_CREDENTIALS: Final = text(
    "SELECT credential_id, organization_id, plant_id, node_id, certificate_serial, issued_at,"
    " expires_at, status, rotated_from, revoked_at FROM fleet.node_credential"
    " WHERE organization_id = :organization_id AND node_id = :node_id"
    " ORDER BY issued_at, credential_id"
)
_LOCKED: Final = text(
    "SELECT credential_id, organization_id, plant_id, node_id, certificate_serial, issued_at,"
    " expires_at, status, rotated_from, revoked_at FROM fleet.node_credential"
    " WHERE organization_id = :organization_id AND node_id = :node_id"
    " AND certificate_serial = :certificate_serial FOR UPDATE"
)
_INSERT: Final = text(
    "INSERT INTO fleet.node_credential (credential_id, organization_id, plant_id, node_id,"
    " certificate_serial, subject, key_algorithm, issued_at, expires_at, status, rotated_from)"
    " VALUES (:credential_id, :organization_id, :plant_id, :node_id, :certificate_serial,"
    " CAST(:subject AS jsonb), :key_algorithm, :issued_at, :expires_at, 'active', :rotated_from)"
)
_TRANSITION: Final = text(
    "UPDATE fleet.node_credential SET status = :status"
    " WHERE organization_id = :organization_id AND credential_id = :credential_id"
    " AND status = :expected RETURNING credential_id"
)
_REVOCATION_FACTS: Final = text(
    "SELECT c.certificate_serial, c.status, c.issued_at, c.expires_at, c.revoked_at,"
    " (SELECT min(s.issued_at) FROM fleet.node_credential s"
    " WHERE s.organization_id = c.organization_id AND s.rotated_from = c.credential_id)"
    " AS successor_issued_at"
    " FROM fleet.node_credential c"
    " WHERE c.organization_id = :organization_id"
    " AND c.status IN ('revoked', 'superseded', 'overlapping') AND c.expires_at > :now"
    " ORDER BY c.certificate_serial"
)
_MARK_ENROLLED: Final = text(
    "UPDATE fleet.node_fleet_record SET enrolled_at = :enrolled_at,"
    " hardware_fingerprint = :hardware_fingerprint"
    " WHERE organization_id = :organization_id AND node_id = :node_id"
    " AND revoked_at IS NULL AND decommissioned_at IS NULL"
    " AND (hardware_fingerprint IS NULL OR hardware_fingerprint = :hardware_fingerprint)"
    " RETURNING node_id"
)


def _uuid(value: object) -> uuid.UUID:
    """asyncpg devuelve su propio tipo de UUID; el dominio exige ``uuid.UUID``."""
    return uuid.UUID(str(value))


def _credential(row: Row[Any]) -> NodeCredential:
    return NodeCredential(
        credential_id=_uuid(row.credential_id),
        organization_id=_uuid(row.organization_id),
        plant_id=_uuid(row.plant_id),
        node_id=_uuid(row.node_id),
        certificate_serial=str(row.certificate_serial),
        issued_at=row.issued_at,
        expires_at=row.expires_at,
        status=CredentialStatus(row.status),
        rotated_from=None if row.rotated_from is None else _uuid(row.rotated_from),
        revoked_at=row.revoked_at,
    )


@repository
class PostgresCredentialStore:
    """Lecturas y escrituras de ``NodeCredential`` dentro de la transacción de la operación."""

    async def credentials(
        self, transaction: Transaction, node_id: uuid.UUID
    ) -> tuple[NodeCredential, ...]:
        result = await transaction.execute(
            _CREDENTIALS,
            {"organization_id": transaction.context.organization_id, "node_id": node_id},
        )
        return tuple(_credential(row) for row in result.all())

    async def locked(
        self, transaction: Transaction, node_id: uuid.UUID, certificate_serial: str
    ) -> NodeCredential | None:
        """La credencial del número de serie, bloqueada hasta el final de la transacción."""
        result = await transaction.execute(
            _LOCKED,
            {
                "organization_id": transaction.context.organization_id,
                "node_id": node_id,
                "certificate_serial": certificate_serial,
            },
        )
        row = result.first()
        return None if row is None else _credential(row)

    async def insert(self, transaction: Transaction, credential: NodeCredential) -> None:
        if credential.organization_id != transaction.context.organization_id:
            raise ValueError("la credencial es de otra organización que la transacción")
        if credential.status is not CredentialStatus.ACTIVE:
            raise ValueError("una credencial nace active")
        await transaction.execute(
            _INSERT,
            {
                "credential_id": credential.credential_id,
                "organization_id": credential.organization_id,
                "plant_id": credential.plant_id,
                "node_id": credential.node_id,
                "certificate_serial": credential.certificate_serial,
                "subject": json.dumps(subject_document(credential.subject)),
                "key_algorithm": KEY_ALGORITHM,
                "issued_at": credential.issued_at,
                "expires_at": credential.expires_at,
                "rotated_from": credential.rotated_from,
            },
        )

    async def transition(
        self,
        transaction: Transaction,
        credential_id: uuid.UUID,
        expected: CredentialStatus,
        status: CredentialStatus,
    ) -> bool:
        """``expected → status`` si la fila sigue en ``expected``; ``True`` con una fila."""
        result = await transaction.execute(
            _TRANSITION,
            {
                "organization_id": transaction.context.organization_id,
                "credential_id": credential_id,
                "expected": CredentialStatus(expected).value,
                "status": CredentialStatus(status).value,
            },
        )
        return result.first() is not None

    async def revocation_facts(
        self, transaction: Transaction, now: datetime
    ) -> tuple[CredentialRevocationFacts, ...]:
        """Las credenciales no vencidas que pueden ir a la lista de revocación (TASK-220).

        Solo las de la organización del contexto: la RLS y el filtro de la sentencia.
        """
        result = await transaction.execute(
            _REVOCATION_FACTS,
            {"organization_id": transaction.context.organization_id, "now": now},
        )
        return tuple(
            CredentialRevocationFacts(
                certificate_serial=str(row.certificate_serial),
                status=CredentialStatus(row.status),
                issued_at=row.issued_at,
                expires_at=row.expires_at,
                revoked_at=row.revoked_at,
                successor_issued_at=row.successor_issued_at,
            )
            for row in result.all()
        )

    async def mark_enrolled(
        self,
        transaction: Transaction,
        node_id: uuid.UUID,
        enrolled_at: datetime,
        hardware_fingerprint: str,
    ) -> bool:
        """``enrolled_at`` y la huella; ``False`` si la ficha tiene otra huella o está revocada."""
        result = await transaction.execute(
            _MARK_ENROLLED,
            {
                "organization_id": transaction.context.organization_id,
                "node_id": node_id,
                "enrolled_at": enrolled_at,
                "hardware_fingerprint": hardware_fingerprint,
            },
        )
        return result.first() is not None
