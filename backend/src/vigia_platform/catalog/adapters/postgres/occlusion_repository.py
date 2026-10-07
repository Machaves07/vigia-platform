"""Pruebas de oclusión sobre PostgreSQL (LC-GOB-07; ``catalog.occlusion_test`` de gob_0017).

``occlusion_test`` ⛓: solo ``INSERT`` de la prueba ``pending`` y **una** resolución, que la guarda
de gob_0017 admite como transición ``pending → verified | failed | declared`` con los cierres de
nulo a valor de ``correlated_event_ids``, ``declared_reason_es`` y ``ledger_record_id`` (sin
migración nueva). La resolución es condicional a ``verification = 'pending'`` y va bajo el
candado de la fila (``lock``), en la transacción que escribe ``occlusion_test_result``.

**Candados** (orden único de ``catalog.occlusion``): la fila de la sesión de walk-test (solo el
alta y la declaración), después la fila de la prueba y por último la cadena de la planta.

Toda sentencia recibe una ``Transaction`` abierta con el ``ScopeContext`` (RLS por organización y
concesión) y además nombra la organización del contexto (defensa en profundidad).
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.engine import Row

from vigia_platform.catalog.domain.enums import OcclusionVerification
from vigia_platform.catalog.domain.occlusion import OcclusionTest
from vigia_platform.shared.context import repository
from vigia_platform.shared.db import Transaction

__all__ = ["OcclusionWriteConflict", "PostgresOcclusionRepository"]

# Las lecturas repiten la lista de columnas literal: ``text()`` no admite concatenar (VIG001).
_TESTS: Final = text(
    "SELECT test_id, organization_id, plant_id, session_id, camera_id, started_at, ended_at,"
    " deadline, verification, correlated_event_ids, declared_reason_es, recorded_by,"
    " ledger_record_id FROM catalog.occlusion_test"
    " WHERE organization_id = :organization_id AND session_id = :session_id ORDER BY test_id"
)
_PENDING: Final = text(
    "SELECT test_id, organization_id, plant_id, session_id, camera_id, started_at, ended_at,"
    " deadline, verification, correlated_event_ids, declared_reason_es, recorded_by,"
    " ledger_record_id FROM catalog.occlusion_test"
    " WHERE organization_id = :organization_id AND session_id = :session_id"
    " AND verification = 'pending' ORDER BY test_id"
)
_LATEST: Final = text(
    "SELECT test_id, organization_id, plant_id, session_id, camera_id, started_at, ended_at,"
    " deadline, verification, correlated_event_ids, declared_reason_es, recorded_by,"
    " ledger_record_id FROM catalog.occlusion_test"
    " WHERE organization_id = :organization_id AND session_id = :session_id"
    " AND camera_id = :camera_id ORDER BY test_id DESC LIMIT 1"
)
_LOCK: Final = text(
    "SELECT test_id, organization_id, plant_id, session_id, camera_id, started_at, ended_at,"
    " deadline, verification, correlated_event_ids, declared_reason_es, recorded_by,"
    " ledger_record_id FROM catalog.occlusion_test"
    " WHERE organization_id = :organization_id AND test_id = :test_id FOR UPDATE"
)
_INSERT: Final = text(
    "INSERT INTO catalog.occlusion_test (test_id, organization_id, plant_id, session_id,"
    " camera_id, started_at, ended_at, deadline, verification, recorded_by)"
    " VALUES (:test_id, :organization_id, :plant_id, :session_id, :camera_id, :started_at,"
    " :ended_at, :deadline, 'pending', :recorded_by)"
)
_RESOLVE: Final = text(
    "UPDATE catalog.occlusion_test SET verification = :verification,"
    " correlated_event_ids = CAST(:correlated_event_ids AS uuid[]),"
    " declared_reason_es = :declared_reason_es, ledger_record_id = :ledger_record_id"
    " WHERE organization_id = :organization_id AND test_id = :test_id"
    " AND verification = 'pending'"
)


class OcclusionWriteConflict(Exception):
    """La prueba ya no está ``pending``: otra resolución llegó antes."""

    def __init__(self) -> None:
        super().__init__("la prueba de oclusión ya estaba resuelta")


def _uuid(value: object) -> uuid.UUID:
    """asyncpg devuelve su propio tipo de UUID; el dominio exige ``uuid.UUID``."""
    return uuid.UUID(str(value))


def _optional_uuid(value: object) -> uuid.UUID | None:
    return None if value is None else _uuid(value)


def _rowcount(result: Any) -> int:
    count: int = result.rowcount
    return count


def _test(row: Row[Any]) -> OcclusionTest:
    correlated = row.correlated_event_ids
    return OcclusionTest(
        test_id=_uuid(row.test_id),
        organization_id=_uuid(row.organization_id),
        plant_id=_uuid(row.plant_id),
        session_id=_uuid(row.session_id),
        camera_id=_uuid(row.camera_id),
        started_at=row.started_at,
        ended_at=row.ended_at,
        deadline=row.deadline,
        verification=OcclusionVerification(row.verification),
        correlated_event_ids=None if correlated is None else tuple(_uuid(e) for e in correlated),
        declared_reason_es=row.declared_reason_es,
        recorded_by=_uuid(row.recorded_by),
        ledger_record_id=_optional_uuid(row.ledger_record_id),
    )


def _session_key(transaction: Transaction, session_id: uuid.UUID) -> dict[str, Any]:
    return {"organization_id": transaction.context.organization_id, "session_id": session_id}


@repository
class PostgresOcclusionRepository:
    """Pruebas de oclusión de una sesión: alta, lecturas, candado y resolución única."""

    async def tests(
        self, transaction: Transaction, session_id: uuid.UUID
    ) -> tuple[OcclusionTest, ...]:
        """Las pruebas de la sesión en orden de registro."""
        result = await transaction.execute(_TESTS, _session_key(transaction, session_id))
        return tuple(_test(row) for row in result.all())

    async def pending(
        self, transaction: Transaction, session_id: uuid.UUID
    ) -> tuple[OcclusionTest, ...]:
        """Las pruebas ``pending`` de la sesión en orden de registro."""
        result = await transaction.execute(_PENDING, _session_key(transaction, session_id))
        return tuple(_test(row) for row in result.all())

    async def latest(
        self, transaction: Transaction, session_id: uuid.UUID, camera_id: uuid.UUID
    ) -> OcclusionTest | None:
        """La última prueba de la cámara en la sesión, o ``None``."""
        result = await transaction.execute(
            _LATEST, {**_session_key(transaction, session_id), "camera_id": camera_id}
        )
        row = result.first()
        return None if row is None else _test(row)

    async def lock(self, transaction: Transaction, test_id: uuid.UUID) -> OcclusionTest | None:
        """La prueba con su fila bloqueada hasta el fin de la transacción, o ``None``."""
        result = await transaction.execute(
            _LOCK, {"organization_id": transaction.context.organization_id, "test_id": test_id}
        )
        row = result.first()
        return None if row is None else _test(row)

    async def insert(self, transaction: Transaction, test: OcclusionTest) -> None:
        """Anexa la prueba ``pending``."""
        if test.organization_id != transaction.context.organization_id:
            raise ValueError("la prueba es de otra organización que la transacción")
        if test.resolved:
            raise ValueError("una prueba de oclusión nace pending")
        await transaction.execute(
            _INSERT,
            {
                "test_id": test.test_id,
                "organization_id": test.organization_id,
                "plant_id": test.plant_id,
                "session_id": test.session_id,
                "camera_id": test.camera_id,
                "started_at": test.started_at,
                "ended_at": test.ended_at,
                "deadline": test.deadline,
                "recorded_by": test.recorded_by,
            },
        )

    async def resolve(self, transaction: Transaction, test: OcclusionTest) -> None:
        """Guarda la resolución **una sola vez**; si ya no estaba ``pending``,
        ``OcclusionWriteConflict``."""
        if not test.resolved or test.ledger_record_id is None:
            raise ValueError("la resolución fija la verificación y su registro")
        result = await transaction.execute(
            _RESOLVE,
            {
                "organization_id": transaction.context.organization_id,
                "test_id": test.test_id,
                "verification": OcclusionVerification(test.verification).value,
                "correlated_event_ids": list(test.correlated_event_ids or ()),
                "declared_reason_es": test.declared_reason_es,
                "ledger_record_id": test.ledger_record_id,
            },
        )
        if _rowcount(result) != 1:
            raise OcclusionWriteConflict
