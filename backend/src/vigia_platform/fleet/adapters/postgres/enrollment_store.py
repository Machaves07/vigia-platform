"""Códigos e intentos de alta sobre PostgreSQL (TASK-218; tablas de gob_0018).

- ``fleet.enrollment_code`` 🔒: solo ``code_hash`` y su sal; ``status`` solo hacia adelante
  (``catalog.guard_update``). **A lo sumo un ``active`` por nodo** lo garantiza el índice único
  parcial ``enrollment_code_one_active_per_node`` (``ONE_ACTIVE_PER_NODE``): ``supersede_active``
  y el ``INSERT`` del código nuevo no se serializan de otro modo, así que dos emisiones
  simultáneas chocan en el índice y el servicio reintenta la perdedora.
- **Consumo** (``consume``): ``UPDATE … SET status = 'used' WHERE … AND status = 'active' AND
  expires_at > now``; el éxito es **una** fila afectada. Dos consumos simultáneos del mismo código:
  el segundo espera el candado de la fila, vuelve a evaluar la condición y no afecta ninguna.
- ``fleet.enrollment_attempt`` ⛓ (particionada por mes de ``attempted_at``): solo ``INSERT`` y
  ``SELECT``, por páginas de la planta y el nodo.

Cada sentencia nombra la organización del contexto de su ``Transaction`` (además de la RLS).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.engine import Row

from vigia_platform.fleet.domain.enrollment_attempt import EnrollmentAttempt
from vigia_platform.fleet.domain.enrollment_code import EnrollmentCode
from vigia_platform.fleet.domain.enums import EnrollmentAttemptResult, EnrollmentCodeStatus
from vigia_platform.shared.context import repository
from vigia_platform.shared.db import Transaction

__all__ = [
    "MAX_CODES_COMPARED",
    "ONE_ACTIVE_PER_NODE",
    "AttemptCursor",
    "PostgresEnrollmentStore",
]

ONE_ACTIVE_PER_NODE: Final = "enrollment_code_one_active_per_node"
"""Índice único parcial ``(node_id) WHERE status = 'active'`` (gob_0018)."""
MAX_CODES_COMPARED: Final = 64
"""Conjunto acotado de la verificación: los últimos códigos emitidos para el nodo
`[objetivo propio]`. Un código más viejo que estos 64 hace mucho que dejó de ser ``active``."""

_CODES: Final = text(
    "SELECT code_id, organization_id, plant_id, node_id, code_hash, code_salt, issued_at,"
    " issued_by, expires_at, disclosed_at, status, ledger_record_id FROM fleet.enrollment_code"
    " WHERE organization_id = :organization_id AND node_id = :node_id"
    " ORDER BY issued_at DESC, code_id DESC LIMIT :limit"
)
_SUPERSEDE: Final = text(
    "UPDATE fleet.enrollment_code SET status = 'superseded'"
    " WHERE organization_id = :organization_id AND node_id = :node_id AND status = 'active'"
    " RETURNING code_id"
)
_INSERT_CODE: Final = text(
    "INSERT INTO fleet.enrollment_code (code_id, organization_id, plant_id, node_id, code_hash,"
    " code_salt, issued_at, issued_by, expires_at, disclosed_at, status, ledger_record_id)"
    " VALUES (:code_id, :organization_id, :plant_id, :node_id, :code_hash, :code_salt,"
    " :issued_at, :issued_by, :expires_at, :disclosed_at, :status, :ledger_record_id)"
)
_CONSUME: Final = text(
    "UPDATE fleet.enrollment_code SET status = 'used'"
    " WHERE organization_id = :organization_id AND code_id = :code_id"
    " AND status = 'active' AND expires_at > :now RETURNING code_id"
)
_INSERT_ATTEMPT: Final = text(
    "INSERT INTO fleet.enrollment_attempt (attempt_id, organization_id, plant_id, node_id,"
    " presented_code_hash, hardware_fingerprint, software_version, contract_version, result,"
    " attempted_at, source_ip_hash, correlation_id, ledger_record_id)"
    " VALUES (:attempt_id, :organization_id, :plant_id, :node_id, :presented_code_hash,"
    " :hardware_fingerprint, :software_version, :contract_version, :result, :attempted_at,"
    " :source_ip_hash, :correlation_id, :ledger_record_id)"
)
_ATTEMPTS: Final = text(
    "SELECT attempt_id, organization_id, plant_id, node_id, presented_code_hash,"
    " hardware_fingerprint, software_version, contract_version, result, attempted_at,"
    " source_ip_hash, correlation_id, ledger_record_id FROM fleet.enrollment_attempt"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id AND node_id = :node_id"
    " ORDER BY attempted_at DESC, attempt_id DESC LIMIT :limit"
)
_ATTEMPTS_AFTER: Final = text(
    "SELECT attempt_id, organization_id, plant_id, node_id, presented_code_hash,"
    " hardware_fingerprint, software_version, contract_version, result, attempted_at,"
    " source_ip_hash, correlation_id, ledger_record_id FROM fleet.enrollment_attempt"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id AND node_id = :node_id"
    " AND (attempted_at, attempt_id) < (:after_at, :after_id)"
    " ORDER BY attempted_at DESC, attempt_id DESC LIMIT :limit"
)


def _uuid(value: object) -> uuid.UUID:
    """asyncpg devuelve su propio tipo de UUID; el dominio exige ``uuid.UUID``."""
    return uuid.UUID(str(value))


def _optional_uuid(value: object) -> uuid.UUID | None:
    return None if value is None else _uuid(value)


@dataclass(frozen=True, slots=True)
class AttemptCursor:
    """Clave del último intento de una página (el más reciente primero)."""

    attempted_at: datetime
    attempt_id: uuid.UUID

    def __post_init__(self) -> None:
        if not isinstance(self.attempted_at, datetime) or self.attempted_at.utcoffset() is None:
            raise ValueError("attempted_at del cursor debe llevar zona horaria")
        if type(self.attempt_id) is not uuid.UUID:
            raise TypeError("attempt_id del cursor debe ser uuid.UUID")


def _code(row: Row[Any]) -> EnrollmentCode:
    return EnrollmentCode(
        code_id=_uuid(row.code_id),
        organization_id=_uuid(row.organization_id),
        plant_id=_uuid(row.plant_id),
        node_id=_uuid(row.node_id),
        code_hash=row.code_hash,
        code_salt=bytes(row.code_salt),
        issued_at=row.issued_at,
        issued_by=_uuid(row.issued_by),
        expires_at=row.expires_at,
        disclosed_at=row.disclosed_at,
        status=EnrollmentCodeStatus(row.status),
        ledger_record_id=_uuid(row.ledger_record_id),
    )


def _attempt(row: Row[Any]) -> EnrollmentAttempt:
    return EnrollmentAttempt(
        attempt_id=_uuid(row.attempt_id),
        organization_id=_uuid(row.organization_id),
        plant_id=_optional_uuid(row.plant_id),
        node_id=_optional_uuid(row.node_id),
        presented_code_hash=row.presented_code_hash,
        hardware_fingerprint=row.hardware_fingerprint,
        software_version=row.software_version,
        contract_version=row.contract_version,
        result=EnrollmentAttemptResult(row.result),
        attempted_at=row.attempted_at,
        source_ip_hash=row.source_ip_hash,
        correlation_id=_uuid(row.correlation_id),
        ledger_record_id=_optional_uuid(row.ledger_record_id),
    )


def _same_organization(transaction: Transaction, organization_id: uuid.UUID) -> None:
    if organization_id != transaction.context.organization_id:
        raise ValueError("la fila es de otra organización que la transacción")


@repository
class PostgresEnrollmentStore:
    """``fleet.enrollment_code`` y ``fleet.enrollment_attempt``."""

    async def codes(
        self, transaction: Transaction, node_id: uuid.UUID, *, limit: int = MAX_CODES_COMPARED
    ) -> tuple[EnrollmentCode, ...]:
        """Los últimos ``limit`` códigos del nodo, el más reciente primero."""
        result = await transaction.execute(
            _CODES,
            {
                "organization_id": transaction.context.organization_id,
                "node_id": node_id,
                "limit": limit,
            },
        )
        return tuple(_code(row) for row in result.all())

    async def supersede_active(
        self, transaction: Transaction, node_id: uuid.UUID
    ) -> tuple[uuid.UUID, ...]:
        """El ``active`` del nodo (si lo hay) pasa a ``superseded``."""
        result = await transaction.execute(
            _SUPERSEDE,
            {"organization_id": transaction.context.organization_id, "node_id": node_id},
        )
        return tuple(_uuid(row.code_id) for row in result.all())

    async def insert_code(self, transaction: Transaction, code: EnrollmentCode) -> None:
        """Anexa el código; un segundo ``active`` del nodo sale como ``IntegrityError``."""
        _same_organization(transaction, code.organization_id)
        await transaction.execute(
            _INSERT_CODE,
            {
                "code_id": code.code_id,
                "organization_id": code.organization_id,
                "plant_id": code.plant_id,
                "node_id": code.node_id,
                "code_hash": code.code_hash,
                "code_salt": code.code_salt,
                "issued_at": code.issued_at,
                "issued_by": code.issued_by,
                "expires_at": code.expires_at,
                "disclosed_at": code.disclosed_at,
                "status": code.status.value,
                "ledger_record_id": code.ledger_record_id,
            },
        )

    async def consume(self, transaction: Transaction, code_id: uuid.UUID, now: datetime) -> bool:
        """``active → used`` si sigue ``active`` y vigente en ``now``; ``True`` solo si cambió."""
        result = await transaction.execute(
            _CONSUME,
            {
                "organization_id": transaction.context.organization_id,
                "code_id": code_id,
                "now": now,
            },
        )
        return len(result.all()) == 1

    async def insert_attempt(self, transaction: Transaction, attempt: EnrollmentAttempt) -> None:
        _same_organization(transaction, attempt.organization_id)
        await transaction.execute(
            _INSERT_ATTEMPT,
            {
                "attempt_id": attempt.attempt_id,
                "organization_id": attempt.organization_id,
                "plant_id": attempt.plant_id,
                "node_id": attempt.node_id,
                "presented_code_hash": attempt.presented_code_hash,
                "hardware_fingerprint": attempt.hardware_fingerprint,
                "software_version": attempt.software_version,
                "contract_version": attempt.contract_version,
                "result": attempt.result.value,
                "attempted_at": attempt.attempted_at,
                "source_ip_hash": attempt.source_ip_hash,
                "correlation_id": attempt.correlation_id,
                "ledger_record_id": attempt.ledger_record_id,
            },
        )

    async def attempts(
        self,
        transaction: Transaction,
        *,
        plant_id: uuid.UUID,
        node_id: uuid.UUID,
        after: AttemptCursor | None,
        limit: int,
    ) -> Sequence[EnrollmentAttempt]:
        """Hasta ``limit`` intentos del nodo **de esa planta**, el más reciente primero."""
        parameters: dict[str, Any] = {
            "organization_id": transaction.context.organization_id,
            "plant_id": plant_id,
            "node_id": node_id,
            "limit": limit,
        }
        statement = _ATTEMPTS
        if after is not None:
            statement = _ATTEMPTS_AFTER
            parameters.update(after_at=after.attempted_at, after_id=after.attempt_id)
        result = await transaction.execute(statement, parameters)
        return tuple(_attempt(row) for row in result.all())
