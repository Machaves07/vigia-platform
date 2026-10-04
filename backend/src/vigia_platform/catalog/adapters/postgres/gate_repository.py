"""Compuertas sobre PostgreSQL (LC-GOB-03; ``zone_gate_state`` y ``gate_state_history``).

- ``zone_gate_state`` 🔒 es la proyección: una fila por zona, que se inserta o se actualiza
  (``vigia_app`` no tiene ``DELETE``); guarda el sobre ``SignedEnvelope<GateState>`` ya emitido,
  que las lecturas devuelven **tal cual**, sin canonicalizar ni firmar (PAT-GOB-REN-02).
- ``gate_state_history`` ⛓ es la verdad: solo ``INSERT`` y el cierre ``effective_until`` de nulo a
  su valor, una vez (lista blanca de ``gob_0017``). El cierre del intervalo abierto y la apertura
  del siguiente van en la **misma transacción**: dos rangos no acotados se solaparían y la
  restricción ``gate_state_no_overlap`` lo rechazaría.

**Exclusión** entre transiciones de la misma zona: ``lock_projection`` toma
``pg_advisory_xact_lock`` sobre la proyección de la zona antes de leerla; la segunda transición
espera (hasta ``lock_timeout``, transitorio) y decide sobre lo que dejó la primera. El cierre
condicional (``effective_until IS NULL`` y el ``effective_from`` que se leyó) y la restricción de
exclusión son el respaldo: un estado leído viejo nunca escribe.

Toda sentencia recibe un ``ScopeContext`` o una ``Transaction`` abierta con él (RLS por
organización y concesión) y además nombra la organización del contexto y la zona pedida (defensa
en profundidad), así que la historia de una zona nunca responde por otra.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.engine import Row
from vigia_contracts.models.enumerations import GateStatus

from vigia_platform.catalog.domain.catalog_version import ZoneRef
from vigia_platform.catalog.domain.enums import GateKind
from vigia_platform.catalog.domain.gates import GateDecision, GateInterval, ZoneGateState
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.db import Transaction

__all__ = ["GateWriteConflict", "PostgresGateRepository"]

_ZONE: Final = text(
    "SELECT organization_id, plant_id, zone_id, code FROM identity.zone"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id"
)
_LOCK_PROJECTION: Final = text(
    "SELECT pg_advisory_xact_lock(hashtextextended('zone_gate_state|' || :zone_id, 0))"
)
_STATE: Final = text(
    "SELECT organization_id, plant_id, zone_id, mounting, usage, issued_at, envelope"
    " FROM catalog.zone_gate_state"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id"
)
_UPSERT_STATE: Final = text(
    "INSERT INTO catalog.zone_gate_state (zone_id, organization_id, plant_id, mounting, usage,"
    " resulting_mode, issued_at, envelope, valid_until)"
    " VALUES (:zone_id, :organization_id, :plant_id, CAST(:mounting AS jsonb),"
    " CAST(:usage AS jsonb), :resulting_mode, :issued_at, CAST(:envelope AS jsonb),"
    " :valid_until)"
    " ON CONFLICT (zone_id) DO UPDATE SET mounting = EXCLUDED.mounting,"
    " usage = EXCLUDED.usage, resulting_mode = EXCLUDED.resulting_mode,"
    " issued_at = EXCLUDED.issued_at, envelope = EXCLUDED.envelope,"
    " valid_until = EXCLUDED.valid_until"
    " WHERE catalog.zone_gate_state.organization_id = EXCLUDED.organization_id"
)
_ENVELOPES: Final = text(
    "SELECT zone_id, envelope FROM catalog.zone_gate_state"
    " WHERE organization_id = :organization_id AND zone_id = ANY(CAST(:zone_ids AS uuid[]))"
)
_CLOSE_OPEN: Final = text(
    "UPDATE catalog.gate_state_history SET effective_until = :effective_until"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id AND gate = :gate"
    " AND effective_from = :effective_from AND effective_until IS NULL"
)
_INSERT_INTERVAL: Final = text(
    "INSERT INTO catalog.gate_state_history (organization_id, plant_id, zone_id, gate, status,"
    " effective_from, decided_by, reason_es, ledger_record_id, record_id)"
    " VALUES (:organization_id, :plant_id, :zone_id, :gate, :status, :effective_from,"
    " :decided_by, :reason_es, :ledger_record_id, :record_id)"
)
# Las lecturas repiten la lista de columnas literal: ``text()`` no admite concatenar (VIG001).
_STATE_AT: Final = text(
    "SELECT organization_id, plant_id, zone_id, gate, status, effective_from, effective_until,"
    " decided_by, reason_es, record_id, ledger_record_id FROM catalog.gate_state_history"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id AND gate = :gate"
    " AND effective @> CAST(:at AS timestamptz)"
)
_HISTORY: Final = text(
    "SELECT organization_id, plant_id, zone_id, gate, status, effective_from, effective_until,"
    " decided_by, reason_es, record_id, ledger_record_id FROM catalog.gate_state_history"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id"
    " AND effective && tstzrange(CAST(:from_ AS timestamptz), CAST(:to_ AS timestamptz), '[)')"
    " ORDER BY gate, effective_from"
)


class GateWriteConflict(Exception):
    """El intervalo abierto ya no es el que se leyó: otra transición llegó antes (transitorio)."""

    def __init__(self) -> None:
        super().__init__("el intervalo abierto de la compuerta ya no es el que se leyó")


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


def _decision(value: Mapping[str, Any]) -> GateDecision:
    decided_at = value.get("decided_at")
    record = value.get("record_id", value.get("agreement_id"))
    return GateDecision(
        GateStatus(value["status"]),
        None if decided_at is None else datetime.fromisoformat(decided_at),
        _optional_uuid(record),
        _optional_uuid(value.get("decided_by")),
    )


def _state(row: Row[Any]) -> ZoneGateState:
    return ZoneGateState(
        organization_id=_uuid(row.organization_id),
        plant_id=_uuid(row.plant_id),
        zone_id=_uuid(row.zone_id),
        mounting=_decision(_json(row.mounting)),
        usage=_decision(_json(row.usage)),
        issued_at=row.issued_at,
        envelope=_json(row.envelope),
    )


def _interval(row: Row[Any]) -> GateInterval:
    return GateInterval(
        organization_id=_uuid(row.organization_id),
        plant_id=_uuid(row.plant_id),
        zone_id=_uuid(row.zone_id),
        gate=GateKind(row.gate),
        status=GateStatus(row.status),
        effective_from=row.effective_from,
        effective_until=row.effective_until,
        decided_by=_uuid(row.decided_by),
        reason_es=row.reason_es,
        record_id=_optional_uuid(row.record_id),
        ledger_record_id=_uuid(row.ledger_record_id),
    )


def _rowcount(result: Any) -> int:
    count: int = result.rowcount
    return count


@repository
class PostgresGateRepository:
    """Proyección, historia y sobre de las compuertas de una zona."""

    def __init__(self, database: LedgerDatabase) -> None:
        self._database = database

    # --- Zona ---------------------------------------------------------------------------------

    async def zone(self, transaction: Transaction, zone_id: uuid.UUID) -> ZoneRef | None:
        """La zona en la organización de la transacción (si la RLS la deja ver), o ``None``."""
        result = await transaction.execute(_ZONE, _zone_key(transaction.context, zone_id))
        row = result.first()
        if row is None:
            return None
        return ZoneRef(
            organization_id=_uuid(row.organization_id),
            plant_id=_uuid(row.plant_id),
            zone_id=_uuid(row.zone_id),
            zone_code=row.code,
        )

    # --- Proyección ----------------------------------------------------------------------------

    async def lock_projection(self, transaction: Transaction, zone_id: uuid.UUID) -> None:
        """Exclusión entre transiciones de la zona hasta el fin de la transacción."""
        await transaction.execute(_LOCK_PROJECTION, {"zone_id": str(zone_id)})

    async def state(self, transaction: Transaction, zone_id: uuid.UUID) -> ZoneGateState | None:
        """La proyección de la zona (``None`` si nunca cambió), dentro de la transacción."""
        result = await transaction.execute(_STATE, _zone_key(transaction.context, zone_id))
        row = result.first()
        return None if row is None else _state(row)

    async def read_state(self, context: ScopeContext, zone_id: uuid.UUID) -> ZoneGateState | None:
        """La proyección, fuera de una transacción de escritura."""
        rows = await self._database.read(context, _STATE, _zone_key(context, zone_id))
        return _state(rows[0]) if rows else None

    async def save_state(
        self, transaction: Transaction, state: ZoneGateState, envelope: Mapping[str, Any]
    ) -> None:
        """Inserta o actualiza la proyección con su sobre ya firmado."""
        if state.organization_id != transaction.context.organization_id:
            raise ValueError("la proyección es de otra organización que la transacción")
        if state.issued_at is None or state.valid_until is None:
            raise ValueError("una proyección guardada lleva su emisión")
        result = await transaction.execute(
            _UPSERT_STATE,
            {
                "zone_id": state.zone_id,
                "organization_id": state.organization_id,
                "plant_id": state.plant_id,
                "mounting": _dumps(state.mounting.as_json(GateKind.MOUNTING, with_author=True)),
                "usage": _dumps(state.usage.as_json(GateKind.USAGE, with_author=True)),
                "resulting_mode": state.resulting_mode.value,
                "issued_at": state.issued_at,
                "envelope": _dumps(dict(envelope)),
                "valid_until": state.valid_until,
            },
        )
        if _rowcount(result) != 1:
            raise GateWriteConflict

    async def envelopes(
        self, context: ScopeContext, zone_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, Mapping[str, Any]]:
        """Los sobres guardados de ``zone_ids`` (las zonas sin proyección no aparecen)."""
        zones = sorted({str(zone_id) for zone_id in zone_ids})
        if not zones:
            return {}
        rows = await self._database.read(
            context,
            _ENVELOPES,
            {"organization_id": context.organization_id, "zone_ids": zones},
        )
        return {_uuid(row.zone_id): _json(row.envelope) for row in rows}

    # --- Historia ------------------------------------------------------------------------------

    async def close_open_interval(
        self,
        transaction: Transaction,
        zone_id: uuid.UUID,
        gate: GateKind,
        effective_from: datetime,
        effective_until: datetime,
    ) -> None:
        """Cierra el intervalo abierto que empezó en ``effective_from``; si ya no es el abierto,
        ``GateWriteConflict``."""
        result = await transaction.execute(
            _CLOSE_OPEN,
            {
                **_zone_key(transaction.context, zone_id),
                "gate": GateKind(gate).value,
                "effective_from": effective_from,
                "effective_until": effective_until,
            },
        )
        if _rowcount(result) != 1:
            raise GateWriteConflict

    async def open_interval(self, transaction: Transaction, interval: GateInterval) -> None:
        """Abre ``[effective_from, ∞)``; un solapamiento sale como ``IntegrityError``."""
        if interval.organization_id != transaction.context.organization_id:
            raise ValueError("el intervalo es de otra organización que la transacción")
        if interval.effective_until is not None:
            raise ValueError("solo se abre un intervalo no acotado")
        await transaction.execute(
            _INSERT_INTERVAL,
            {
                "organization_id": interval.organization_id,
                "plant_id": interval.plant_id,
                "zone_id": interval.zone_id,
                "gate": interval.gate.value,
                "status": interval.status.value,
                "effective_from": interval.effective_from,
                "decided_by": interval.decided_by,
                "reason_es": interval.reason_es,
                "ledger_record_id": interval.ledger_record_id,
                "record_id": interval.record_id,
            },
        )

    async def state_at(
        self, context: ScopeContext, zone_id: uuid.UUID, gate: GateKind, at: datetime
    ) -> tuple[GateInterval, ...]:
        """Los intervalos de ``gate`` que contienen ``at`` (``effective @> at``): a lo sumo uno."""
        rows = await self._database.read(
            context,
            _STATE_AT,
            {**_zone_key(context, zone_id), "gate": GateKind(gate).value, "at": at},
        )
        return tuple(_interval(row) for row in rows)

    async def history(
        self, context: ScopeContext, zone_id: uuid.UUID, from_: datetime, to_: datetime
    ) -> tuple[GateInterval, ...]:
        """Los intervalos de las dos compuertas que se solapan con ``[from_, to_)``."""
        rows = await self._database.read(
            context, _HISTORY, {**_zone_key(context, zone_id), "from_": from_, "to_": to_}
        )
        return tuple(_interval(row) for row in rows)


def _zone_key(context: ScopeContext, zone_id: uuid.UUID) -> dict[str, Any]:
    return {"organization_id": context.organization_id, "zone_id": zone_id}
