"""Sesión de walk-test, pasos y pases sobre PostgreSQL (LC-GOB-06; gob_0017 y gob_0022).

- ``walk_test_session`` 🔒: la apertura es ``INSERT … ON CONFLICT (zone_id) WHERE status IN
  ('in_progress', 'reopened') DO NOTHING`` sobre el índice único parcial
  ``walk_test_session_one_open_per_zone``: dos aperturas simultáneas dejan una sola sesión y la
  otra no escribe nada (``insert_open`` devuelve ``False``). Las demás escrituras son
  **condicionales** al estado que se leyó (``WalkTestWriteConflict`` si ya no lo está).
- ``walk_test_step`` ⛓: solo ``INSERT`` y el cierre de nulo a valor de ``ended_at`` y
  ``correction`` (lista blanca de gob_0017), condicional a ``ended_at IS NULL``: un paso se cierra
  una sola vez.
- ``walk_test_pass`` ⛓: solo ``INSERT``.

**Candado**: ``lock_session`` (``SELECT … FOR UPDATE`` de la fila de la sesión) serializa las
operaciones de una misma sesión. Es el primero que toma cualquier operación de la sesión; después
solo pueden venir la cadena de la planta (``EscritorExpediente``) y la de auditoría de la
organización, en ese orden.

Ninguna consulta agrega, agrupa ni ordena por ``responsible_user_id`` (H-53, BR-GOB-46): los pasos
se leen por sesión, en orden de apertura. Toda sentencia recibe una ``Transaction`` abierta con el
``ScopeContext`` (RLS por organización y concesión) y además nombra la organización del contexto
(defensa en profundidad).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.engine import Row

from vigia_platform.catalog.domain.enums import PassResult, StepKind, WalkTestKind, WalkTestStatus
from vigia_platform.catalog.domain.steps import StepCorrection, WalkTestStep
from vigia_platform.catalog.domain.time_windows import utc_instant
from vigia_platform.catalog.domain.walk_test import SessionRow, WalkTestPass, WalkTestSession
from vigia_platform.shared.context import repository
from vigia_platform.shared.db import Transaction

__all__ = ["PostgresWalkTestRepository", "WalkTestWriteConflict"]

# Las lecturas repiten la lista de columnas literal: ``text()`` no admite concatenar (VIG001).
_SESSION: Final = text(
    "SELECT session_id, organization_id, plant_id, zone_id, node_id, catalog_version, kind,"
    " status, passes_per_cell, matrix_rows, started_at, last_activity_at, closed_at,"
    " commissioning_record_id, reopened_at, reopened_by, reopen_reason_es"
    " FROM catalog.walk_test_session"
    " WHERE organization_id = :organization_id AND session_id = :session_id"
)
_LOCK_SESSION: Final = text(
    "SELECT session_id, organization_id, plant_id, zone_id, node_id, catalog_version, kind,"
    " status, passes_per_cell, matrix_rows, started_at, last_activity_at, closed_at,"
    " commissioning_record_id, reopened_at, reopened_by, reopen_reason_es"
    " FROM catalog.walk_test_session"
    " WHERE organization_id = :organization_id AND session_id = :session_id FOR UPDATE"
)
_OPEN_FOR_ZONE: Final = text(
    "SELECT session_id, organization_id, plant_id, zone_id, node_id, catalog_version, kind,"
    " status, passes_per_cell, matrix_rows, started_at, last_activity_at, closed_at,"
    " commissioning_record_id, reopened_at, reopened_by, reopen_reason_es"
    " FROM catalog.walk_test_session"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id"
    " AND status IN ('in_progress', 'reopened')"
)
_CURRENT_FOR_ZONE: Final = text(
    "SELECT session_id, organization_id, plant_id, zone_id, node_id, catalog_version, kind,"
    " status, passes_per_cell, matrix_rows, started_at, last_activity_at, closed_at,"
    " commissioning_record_id, reopened_at, reopened_by, reopen_reason_es"
    " FROM catalog.walk_test_session"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id"
    " AND status IN ('in_progress', 'reopened', 'incomplete')"
    " ORDER BY (status <> 'incomplete') DESC, started_at DESC, session_id DESC LIMIT 1"
)
_INSERT_SESSION: Final = text(
    "INSERT INTO catalog.walk_test_session (session_id, organization_id, plant_id, zone_id,"
    " node_id, catalog_version, kind, status, passes_per_cell, matrix_rows, started_at,"
    " last_activity_at)"
    " VALUES (:session_id, :organization_id, :plant_id, :zone_id, :node_id, :catalog_version,"
    " :kind, 'in_progress', :passes_per_cell, CAST(:matrix_rows AS jsonb), :started_at,"
    " :started_at)"
    " ON CONFLICT (zone_id) WHERE status IN ('in_progress', 'reopened') DO NOTHING"
)
_TOUCH: Final = text(
    "UPDATE catalog.walk_test_session SET last_activity_at = GREATEST(last_activity_at, :at)"
    " WHERE organization_id = :organization_id AND session_id = :session_id"
    " AND status IN ('in_progress', 'reopened')"
)
_MARK_INCOMPLETE: Final = text(
    "UPDATE catalog.walk_test_session SET status = 'incomplete'"
    " WHERE organization_id = :organization_id AND session_id = :session_id"
    " AND status IN ('in_progress', 'reopened') AND last_activity_at = :seen"
)
_REOPEN: Final = text(
    "UPDATE catalog.walk_test_session SET status = 'reopened', last_activity_at = :at,"
    " reopened_at = :at, reopened_by = :by, reopen_reason_es = :reason"
    " WHERE organization_id = :organization_id AND session_id = :session_id"
    " AND status = :seen_status AND last_activity_at = :seen_activity"
)
_STEPS: Final = text(
    "SELECT step_id, organization_id, plant_id, session_id, step_kind, responsible_user_id,"
    " started_at, ended_at, correction FROM catalog.walk_test_step"
    " WHERE organization_id = :organization_id AND session_id = :session_id"
    " ORDER BY started_at, step_id"
)
_STEP: Final = text(
    "SELECT step_id, organization_id, plant_id, session_id, step_kind, responsible_user_id,"
    " started_at, ended_at, correction FROM catalog.walk_test_step"
    " WHERE organization_id = :organization_id AND session_id = :session_id"
    " AND step_id = :step_id"
)
_INSERT_STEP: Final = text(
    "INSERT INTO catalog.walk_test_step (step_id, organization_id, plant_id, session_id,"
    " step_kind, responsible_user_id, started_at)"
    " VALUES (:step_id, :organization_id, :plant_id, :session_id, :step_kind,"
    " :responsible_user_id, :started_at)"
)
_CLOSE_STEP: Final = text(
    "UPDATE catalog.walk_test_step SET ended_at = :ended_at,"
    " correction = CAST(:correction AS jsonb)"
    " WHERE organization_id = :organization_id AND session_id = :session_id"
    " AND step_id = :step_id AND ended_at IS NULL"
)
_PASSES: Final = text(
    "SELECT pass_id, organization_id, plant_id, session_id, row_id, result, evidence_ref,"
    " recorded_by, recorded_at FROM catalog.walk_test_pass"
    " WHERE organization_id = :organization_id AND session_id = :session_id"
    " ORDER BY recorded_at, pass_id"
)
_INSERT_PASS: Final = text(
    "INSERT INTO catalog.walk_test_pass (pass_id, organization_id, plant_id, session_id, row_id,"
    " result, evidence_ref, recorded_by, recorded_at)"
    " VALUES (:pass_id, :organization_id, :plant_id, :session_id, :row_id, :result,"
    " :evidence_ref, :recorded_by, :recorded_at)"
)


class WalkTestWriteConflict(Exception):
    """La sesión o el paso ya no están en el estado que se leyó: otra operación llegó antes."""

    def __init__(self) -> None:
        super().__init__("la sesión o el paso ya no están en el estado que se leyó")


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


def _instant(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("una marca de la corrección es texto ISO 8601")
    return utc_instant(datetime.fromisoformat(value))


def _session(row: Row[Any]) -> WalkTestSession:
    return WalkTestSession(
        session_id=_uuid(row.session_id),
        organization_id=_uuid(row.organization_id),
        plant_id=_uuid(row.plant_id),
        zone_id=_uuid(row.zone_id),
        node_id=_uuid(row.node_id),
        catalog_version=int(row.catalog_version),
        kind=WalkTestKind(row.kind),
        status=WalkTestStatus(row.status),
        passes_per_cell=int(row.passes_per_cell),
        matrix_rows=tuple(SessionRow.from_json(r) for r in _json(row.matrix_rows)),
        started_at=row.started_at,
        last_activity_at=row.last_activity_at,
        closed_at=row.closed_at,
        commissioning_record_id=_optional_uuid(row.commissioning_record_id),
        reopened_at=row.reopened_at,
        reopened_by=_optional_uuid(row.reopened_by),
        reopen_reason_es=row.reopen_reason_es,
    )


def _correction(value: object) -> StepCorrection | None:
    data = _json(value)
    if data is None:
        return None
    return StepCorrection(
        reason_es=str(data["reason_es"]),
        corrected_by=_uuid(data["corrected_by"]),
        corrected_at=utc_instant(datetime.fromisoformat(data["corrected_at"])),
        started_at=_instant(data.get("started_at")),
        ended_at=_instant(data.get("ended_at")),
    )


def _step(row: Row[Any]) -> WalkTestStep:
    return WalkTestStep(
        step_id=_uuid(row.step_id),
        organization_id=_uuid(row.organization_id),
        plant_id=_uuid(row.plant_id),
        session_id=_uuid(row.session_id),
        step_kind=StepKind(row.step_kind),
        responsible_user_id=_uuid(row.responsible_user_id),
        started_at=row.started_at,
        ended_at=row.ended_at,
        correction=_correction(row.correction),
    )


def _pass(row: Row[Any]) -> WalkTestPass:
    return WalkTestPass(
        pass_id=_uuid(row.pass_id),
        organization_id=_uuid(row.organization_id),
        plant_id=_uuid(row.plant_id),
        session_id=_uuid(row.session_id),
        row_id=_uuid(row.row_id),
        result=PassResult(row.result),
        evidence_ref=_optional_uuid(row.evidence_ref),
        recorded_by=_uuid(row.recorded_by),
        recorded_at=row.recorded_at,
    )


def _session_key(transaction: Transaction, session_id: uuid.UUID) -> dict[str, Any]:
    return {"organization_id": transaction.context.organization_id, "session_id": session_id}


def _zone_key(transaction: Transaction, zone_id: uuid.UUID) -> dict[str, Any]:
    return {"organization_id": transaction.context.organization_id, "zone_id": zone_id}


def _same_organization(transaction: Transaction, organization_id: uuid.UUID) -> None:
    if organization_id != transaction.context.organization_id:
        raise ValueError("la fila es de otra organización que la transacción")


@repository
class PostgresWalkTestRepository:
    """Sesiones, pasos y pases del walk-test; lecturas por sesión y por zona."""

    # --- Sesión ----------------------------------------------------------------------------------

    async def session(
        self, transaction: Transaction, session_id: uuid.UUID
    ) -> WalkTestSession | None:
        """La sesión en la organización de la transacción (si la RLS la deja ver), o ``None``."""
        result = await transaction.execute(_SESSION, _session_key(transaction, session_id))
        row = result.first()
        return None if row is None else _session(row)

    async def lock_session(
        self, transaction: Transaction, session_id: uuid.UUID
    ) -> WalkTestSession | None:
        """Como ``session``, con la fila bloqueada hasta el fin de la transacción."""
        result = await transaction.execute(_LOCK_SESSION, _session_key(transaction, session_id))
        row = result.first()
        return None if row is None else _session(row)

    async def open_for_zone(
        self, transaction: Transaction, zone_id: uuid.UUID
    ) -> WalkTestSession | None:
        """La sesión ``in_progress`` o ``reopened`` de la zona (a lo sumo una), o ``None``."""
        result = await transaction.execute(_OPEN_FOR_ZONE, _zone_key(transaction, zone_id))
        row = result.first()
        return None if row is None else _session(row)

    async def current_for_zone(
        self, transaction: Transaction, zone_id: uuid.UUID
    ) -> WalkTestSession | None:
        """La sesión abierta de la zona o, sin ella, la ``incomplete`` más reciente."""
        result = await transaction.execute(_CURRENT_FOR_ZONE, _zone_key(transaction, zone_id))
        row = result.first()
        return None if row is None else _session(row)

    async def insert_open(self, transaction: Transaction, session: WalkTestSession) -> bool:
        """Anexa la sesión ``in_progress``; ``False`` si la zona ya tenía una abierta."""
        _same_organization(transaction, session.organization_id)
        if session.status is not WalkTestStatus.IN_PROGRESS:
            raise ValueError("una sesión nace en curso")
        result = await transaction.execute(
            _INSERT_SESSION,
            {
                "session_id": session.session_id,
                "organization_id": session.organization_id,
                "plant_id": session.plant_id,
                "zone_id": session.zone_id,
                "node_id": session.node_id,
                "catalog_version": session.catalog_version,
                "kind": WalkTestKind(session.kind).value,
                "passes_per_cell": session.passes_per_cell,
                "matrix_rows": _dumps([row.to_json() for row in session.matrix_rows]),
                "started_at": session.started_at,
            },
        )
        return _rowcount(result) == 1

    async def touch(self, transaction: Transaction, session_id: uuid.UUID, at: datetime) -> None:
        """``last_activity_at`` llevado a ``at`` (nunca hacia atrás) de una sesión abierta; si no
        lo está, conflicto."""
        result = await transaction.execute(
            _TOUCH, {**_session_key(transaction, session_id), "at": at}
        )
        if _rowcount(result) != 1:
            raise WalkTestWriteConflict

    async def mark_incomplete(self, transaction: Transaction, session: WalkTestSession) -> bool:
        """``in_progress | reopened → incomplete`` si sigue sin actividad desde la que se leyó."""
        result = await transaction.execute(
            _MARK_INCOMPLETE,
            {**_session_key(transaction, session.session_id), "seen": session.last_activity_at},
        )
        return _rowcount(result) == 1

    async def reopen(
        self, transaction: Transaction, seen: WalkTestSession, reopened: WalkTestSession
    ) -> None:
        """Guarda ``reopened`` sobre la fila ``seen`` (estado y actividad leídos)."""
        if reopened.status is not WalkTestStatus.REOPENED or reopened.reopened_at is None:
            raise ValueError("la reapertura deja la sesión reopened con su marca")
        result = await transaction.execute(
            _REOPEN,
            {
                **_session_key(transaction, seen.session_id),
                "at": reopened.reopened_at,
                "by": reopened.reopened_by,
                "reason": reopened.reopen_reason_es,
                "seen_status": WalkTestStatus(seen.status).value,
                "seen_activity": seen.last_activity_at,
            },
        )
        if _rowcount(result) != 1:
            raise WalkTestWriteConflict

    # --- Pasos -----------------------------------------------------------------------------------

    async def steps(
        self, transaction: Transaction, session_id: uuid.UUID
    ) -> tuple[WalkTestStep, ...]:
        """Los pasos de la sesión en orden de apertura."""
        result = await transaction.execute(_STEPS, _session_key(transaction, session_id))
        return tuple(_step(row) for row in result.all())

    async def step(
        self, transaction: Transaction, session_id: uuid.UUID, step_id: uuid.UUID
    ) -> WalkTestStep | None:
        result = await transaction.execute(
            _STEP, {**_session_key(transaction, session_id), "step_id": step_id}
        )
        row = result.first()
        return None if row is None else _step(row)

    async def insert_step(self, transaction: Transaction, step: WalkTestStep) -> None:
        """Anexa el paso abierto (``ended_at`` y ``correction`` nulos)."""
        _same_organization(transaction, step.organization_id)
        if step.closed or step.correction is not None:
            raise ValueError("un paso nace abierto y sin corrección")
        await transaction.execute(
            _INSERT_STEP,
            {
                "step_id": step.step_id,
                "organization_id": step.organization_id,
                "plant_id": step.plant_id,
                "session_id": step.session_id,
                "step_kind": StepKind(step.step_kind).value,
                "responsible_user_id": step.responsible_user_id,
                "started_at": step.started_at,
            },
        )

    async def close_step(self, transaction: Transaction, step: WalkTestStep) -> None:
        """Cierra el paso **una sola vez**; si ya tenía ``ended_at``, ``WalkTestWriteConflict``."""
        if step.ended_at is None:
            raise ValueError("el cierre fija ended_at")
        correction = step.correction
        result = await transaction.execute(
            _CLOSE_STEP,
            {
                **_session_key(transaction, step.session_id),
                "step_id": step.step_id,
                "ended_at": step.ended_at,
                "correction": None if correction is None else _dumps(correction.to_json()),
            },
        )
        if _rowcount(result) != 1:
            raise WalkTestWriteConflict

    # --- Pases -----------------------------------------------------------------------------------

    async def passes(
        self, transaction: Transaction, session_id: uuid.UUID
    ) -> tuple[WalkTestPass, ...]:
        """Los pases de la sesión en orden de registro."""
        result = await transaction.execute(_PASSES, _session_key(transaction, session_id))
        return tuple(_pass(row) for row in result.all())

    async def insert_pass(self, transaction: Transaction, recorded: WalkTestPass) -> None:
        _same_organization(transaction, recorded.organization_id)
        await transaction.execute(
            _INSERT_PASS,
            {
                "pass_id": recorded.pass_id,
                "organization_id": recorded.organization_id,
                "plant_id": recorded.plant_id,
                "session_id": recorded.session_id,
                "row_id": recorded.row_id,
                "result": PassResult(recorded.result).value,
                "evidence_ref": recorded.evidence_ref,
                "recorded_by": recorded.recorded_by,
                "recorded_at": recorded.recorded_at,
            },
        )
