"""``GateQueryPort`` sobre PostgreSQL (TASK-213; LC-GOB-23a; interfaces §1.2; PAT-GOB-REN-01, 04).

Una **sola sentencia** ``READ ONLY`` por operación, siempre desde ``identity.zone`` o
``identity.plant`` con el alcance del contexto (como ``catalog_query``): fuera de él, la
sentencia no devuelve la fila de la zona o de la planta y la operación responde
``ResourceNotFound``. Cada sentencia nombra además la organización del contexto (defensa en
profundidad sobre la RLS):

- ``state``: la proyección ``zone_gate_state`` (``pending`` en las dos si la zona nunca cambió);
- ``states_by_plant``: la proyección de cada zona visible de la planta, por ``code``; la planta es
  visible por la organización, por ella misma o por una zona suya (un contexto de zona solo ve su
  zona);
- ``state_at``: **siempre desde la historia** (BR-GOB-20), nunca desde la proyección: el
  intervalo de ``gate_state_history`` cuyo rango ``effective`` contiene ``at``, por el índice GiST
  de alcance (``organization_id, plant_id, zone_id, effective``); ninguno es ``None``
  (``pending``);
- ``gate_history``: los intervalos de las dos compuertas cuyo rango se solapa con ``[from, to]``
  (``effective && tstzrange(from, to, '[]')``, el mismo índice), por compuerta y
  ``effective_from``; el rango es de **366 días** como mucho y superarlo es
  ``PortLimitExceeded``, nunca un resultado truncado;
- ``plant_policy``: la última versión cargada de la política de la planta (``loaded = False`` si
  no hay ninguna);
- ``current_agreement``: el acuerdo ``approved`` de la zona con sus confirmaciones. La sustitución
  supersede el anterior y aprueba el nuevo en la misma transacción (BR-GOB-32), así que una
  lectura ve uno u otro, nunca ninguno; ``None`` si la zona no tiene acuerdo aprobado.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.engine import Row
from vigia_contracts.models.enumerations import GateStatus

from vigia_platform.catalog.adapters.postgres.catalog_query import scope_parameters
from vigia_platform.catalog.adapters.postgres.gate_repository import interval_from_row
from vigia_platform.catalog.domain.agreements import signatory_from_json
from vigia_platform.catalog.domain.enums import AgreementStatus, GateKind
from vigia_platform.catalog.domain.gates import GateDecision, GateInterval, ZoneGateState
from vigia_platform.catalog.domain.ports import (
    MAX_GATE_HISTORY_RANGE,
    AgreementSignatory,
    CurrentAgreement,
    PlantPolicyState,
    PortLimitExceeded,
    PortQueryInvalid,
)
from vigia_platform.catalog.domain.time_windows import containing
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.context import ScopeContext, repository

__all__ = ["PostgresGateQuery"]

# Toda sentencia repite la condición de visibilidad (organización del contexto y
# ``allowed_scopes``): ``text()`` solo admite literales (VIG001).
_STATE: Final = text(
    "SELECT z.organization_id, z.plant_id, z.zone_id, g.mounting, g.usage, g.issued_at"
    " FROM identity.zone AS z"
    " LEFT JOIN catalog.zone_gate_state AS g ON g.organization_id = z.organization_id"
    " AND g.plant_id = z.plant_id AND g.zone_id = z.zone_id"
    " WHERE z.organization_id = :organization_id AND z.zone_id = :zone_id"
    " AND (CAST(:whole_organization AS boolean)"
    " OR z.plant_id = ANY(CAST(:scope_plants AS uuid[]))"
    " OR z.zone_id = ANY(CAST(:scope_zones AS uuid[])))"
)

# Una fila por zona visible de la planta (y una sin zona si la planta visible no tiene ninguna).
_STATES_BY_PLANT: Final = text(
    "SELECT p.organization_id, p.plant_id, z.zone_id, g.mounting, g.usage, g.issued_at"
    " FROM identity.plant AS p"
    " LEFT JOIN identity.zone AS z ON z.organization_id = p.organization_id"
    " AND z.plant_id = p.plant_id"
    " AND (CAST(:whole_organization AS boolean)"
    " OR z.plant_id = ANY(CAST(:scope_plants AS uuid[]))"
    " OR z.zone_id = ANY(CAST(:scope_zones AS uuid[])))"
    " LEFT JOIN catalog.zone_gate_state AS g ON g.organization_id = z.organization_id"
    " AND g.plant_id = z.plant_id AND g.zone_id = z.zone_id"
    " WHERE p.organization_id = :organization_id AND p.plant_id = :plant_id"
    " AND (CAST(:whole_organization AS boolean)"
    " OR p.plant_id = ANY(CAST(:scope_plants AS uuid[]))"
    " OR EXISTS (SELECT 1 FROM identity.zone AS s WHERE s.organization_id = p.organization_id"
    " AND s.plant_id = p.plant_id AND s.zone_id = ANY(CAST(:scope_zones AS uuid[]))))"
    " ORDER BY z.code, z.zone_id"
)

_STATE_AT: Final = text(
    "SELECT z.zone_id AS visible, h.organization_id, h.plant_id, h.zone_id, h.gate, h.status,"
    " h.effective_from, h.effective_until, h.decided_by, h.reason_es, h.record_id,"
    " h.ledger_record_id"
    " FROM identity.zone AS z"
    " LEFT JOIN catalog.gate_state_history AS h ON h.organization_id = z.organization_id"
    " AND h.plant_id = z.plant_id AND h.zone_id = z.zone_id AND h.gate = :gate"
    " AND h.effective @> CAST(:at AS timestamptz)"
    " WHERE z.organization_id = :organization_id AND z.zone_id = :zone_id"
    " AND (CAST(:whole_organization AS boolean)"
    " OR z.plant_id = ANY(CAST(:scope_plants AS uuid[]))"
    " OR z.zone_id = ANY(CAST(:scope_zones AS uuid[])))"
)

_HISTORY: Final = text(
    "SELECT z.zone_id AS visible, h.organization_id, h.plant_id, h.zone_id, h.gate, h.status,"
    " h.effective_from, h.effective_until, h.decided_by, h.reason_es, h.record_id,"
    " h.ledger_record_id"
    " FROM identity.zone AS z"
    " LEFT JOIN catalog.gate_state_history AS h ON h.organization_id = z.organization_id"
    " AND h.plant_id = z.plant_id AND h.zone_id = z.zone_id"
    " AND h.effective && tstzrange(CAST(:start AS timestamptz), CAST(:end AS timestamptz), '[]')"
    " WHERE z.organization_id = :organization_id AND z.zone_id = :zone_id"
    " AND (CAST(:whole_organization AS boolean)"
    " OR z.plant_id = ANY(CAST(:scope_plants AS uuid[]))"
    " OR z.zone_id = ANY(CAST(:scope_zones AS uuid[])))"
    " ORDER BY h.gate, h.effective_from"
)

_PLANT_POLICY: Final = text(
    "SELECT p.plant_id AS visible, l.policy_id, l.version, l.signed_at,"
    " l.signed_by_display_name, l.legal_opinion_reference, l.document_ref,"
    " l.criteria_summary_es"
    " FROM identity.plant AS p"
    " LEFT JOIN LATERAL (SELECT c.policy_id, c.version, c.signed_at, c.signed_by_display_name,"
    " c.legal_opinion_reference, c.document_ref, c.criteria_summary_es"
    " FROM catalog.plant_policy AS c"
    " WHERE c.organization_id = p.organization_id AND c.plant_id = p.plant_id"
    " ORDER BY c.version DESC LIMIT 1) AS l ON true"
    " WHERE p.organization_id = :organization_id AND p.plant_id = :plant_id"
    " AND (CAST(:whole_organization AS boolean)"
    " OR p.plant_id = ANY(CAST(:scope_plants AS uuid[]))"
    " OR EXISTS (SELECT 1 FROM identity.zone AS s WHERE s.organization_id = p.organization_id"
    " AND s.plant_id = p.plant_id AND s.zone_id = ANY(CAST(:scope_zones AS uuid[]))))"
)

_CURRENT_AGREEMENT: Final = text(
    "SELECT z.zone_id AS visible, a.agreement_id, a.status, a.approved_at, a.approved_by,"
    " a.signatories, a.document_ref, a.replaces_agreement_id,"
    " ARRAY(SELECT c.user_id FROM catalog.agreement_confirmation AS c"
    " WHERE c.organization_id = a.organization_id AND c.plant_id = a.plant_id"
    " AND c.agreement_id = a.agreement_id ORDER BY c.user_id) AS confirmed_users,"
    " ARRAY(SELECT c.confirmed_at FROM catalog.agreement_confirmation AS c"
    " WHERE c.organization_id = a.organization_id AND c.plant_id = a.plant_id"
    " AND c.agreement_id = a.agreement_id ORDER BY c.user_id) AS confirmed_ats"
    " FROM identity.zone AS z"
    " LEFT JOIN LATERAL (SELECT u.organization_id, u.plant_id, u.agreement_id, u.status,"
    " u.approved_at, u.approved_by, u.signatories, u.document_ref, u.replaces_agreement_id"
    " FROM catalog.use_agreement AS u"
    " WHERE u.organization_id = z.organization_id AND u.plant_id = z.plant_id"
    " AND u.zone_id = z.zone_id AND u.status = 'approved'"
    " ORDER BY u.approved_at DESC, u.agreement_id DESC LIMIT 1) AS a ON true"
    " WHERE z.organization_id = :organization_id AND z.zone_id = :zone_id"
    " AND (CAST(:whole_organization AS boolean)"
    " OR z.plant_id = ANY(CAST(:scope_plants AS uuid[]))"
    " OR z.zone_id = ANY(CAST(:scope_zones AS uuid[])))"
)


def _uuid(value: object) -> uuid.UUID:
    """asyncpg devuelve su propio tipo de UUID; el dominio exige ``uuid.UUID``."""
    return uuid.UUID(str(value))


def _optional_uuid(value: object) -> uuid.UUID | None:
    return None if value is None else _uuid(value)


def _json(value: object) -> Any:
    """``jsonb`` llega ya decodificado con asyncpg; como texto, se decodifica aquí."""
    return json.loads(value) if isinstance(value, str | bytes) else value


def _identifier(value: object, name: str) -> uuid.UUID:
    if type(value) is not uuid.UUID:
        raise PortQueryInvalid(f"{name} debe ser uuid.UUID")
    return value


def _instant(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise PortQueryInvalid(f"{name} debe ser una marca con zona horaria")
    return value


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
    """La proyección de la zona sin su sobre (U-04 no lo necesita), o ``pending`` en las dos."""
    organization_id, plant_id, zone_id = (
        _uuid(row.organization_id),
        _uuid(row.plant_id),
        _uuid(row.zone_id),
    )
    if row.mounting is None:
        return ZoneGateState.initial(organization_id, plant_id, zone_id)
    return ZoneGateState(
        organization_id=organization_id,
        plant_id=plant_id,
        zone_id=zone_id,
        mounting=_decision(_json(row.mounting)),
        usage=_decision(_json(row.usage)),
        issued_at=row.issued_at,
    )


@repository
class PostgresGateQuery:
    """Las seis operaciones de ``GateQueryPort``: cada una, una lectura ``READ ONLY`` de una
    sentencia."""

    def __init__(self, database: LedgerDatabase) -> None:
        self._database = database

    def __repr__(self) -> str:
        return "PostgresGateQuery()"

    async def state(self, context: ScopeContext, zone_id: uuid.UUID) -> ZoneGateState:
        """``ZoneGateState`` de la zona: montaje, uso, modo resultante y emisión."""
        zone_id = _identifier(zone_id, "zone_id")
        rows = await self._database.read(
            context, _STATE, {**scope_parameters(context), "zone_id": zone_id}
        )
        if not rows:
            raise ResourceNotFound()
        return _state(rows[0])

    async def states_by_plant(
        self, context: ScopeContext, plant_id: uuid.UUID
    ) -> tuple[ZoneGateState, ...]:
        """La proyección de cada zona visible de la planta, por ``code`` de zona."""
        plant_id = _identifier(plant_id, "plant_id")
        rows = await self._database.read(
            context, _STATES_BY_PLANT, {**scope_parameters(context), "plant_id": plant_id}
        )
        if not rows:
            raise ResourceNotFound()
        return tuple(_state(row) for row in rows if row.zone_id is not None)

    async def state_at(
        self, context: ScopeContext, zone_id: uuid.UUID, gate: GateKind, at: datetime
    ) -> GateInterval | None:
        """El intervalo de ``gate`` que contiene ``at``, desde la historia; ``None`` es
        ``pending`` (ningún intervalo lo contiene)."""
        zone_id = _identifier(zone_id, "zone_id")
        if not isinstance(gate, GateKind):
            raise PortQueryInvalid("gate debe ser GateKind")
        moment = _instant(at, "at")
        rows = await self._database.read(
            context,
            _STATE_AT,
            {**scope_parameters(context), "zone_id": zone_id, "gate": gate.value, "at": moment},
        )
        if not rows:
            raise ResourceNotFound()
        found = [interval_from_row(row) for row in rows if row.zone_id is not None]
        # Dos a la vez sería una historia rota: ``ValueError`` en lugar de elegir uno.
        return containing(found, moment, lambda interval: interval.window)

    async def gate_history(
        self, context: ScopeContext, zone_id: uuid.UUID, start: datetime, end: datetime
    ) -> tuple[GateInterval, ...]:
        """Los intervalos de las dos compuertas que se solapan con ``[start, end]`` (≤ 366 días),
        por compuerta y ``effective_from``."""
        zone_id = _identifier(zone_id, "zone_id")
        start = _instant(start, "from")
        end = _instant(end, "to")
        if end < start:
            raise PortQueryInvalid("from no puede ser posterior a to")
        if end - start > MAX_GATE_HISTORY_RANGE:
            # PAT-GOB-REN-04: el tope se rechaza en el borde, nunca se trunca.
            raise PortLimitExceeded("gate_history", "366 días")
        rows = await self._database.read(
            context,
            _HISTORY,
            {**scope_parameters(context), "zone_id": zone_id, "start": start, "end": end},
        )
        if not rows:
            raise ResourceNotFound()
        return tuple(interval_from_row(row) for row in rows if row.zone_id is not None)

    async def plant_policy(self, context: ScopeContext, plant_id: uuid.UUID) -> PlantPolicyState:
        """La última versión cargada de la política de la planta, o ``loaded = False``."""
        plant_id = _identifier(plant_id, "plant_id")
        rows = await self._database.read(
            context, _PLANT_POLICY, {**scope_parameters(context), "plant_id": plant_id}
        )
        if not rows:
            raise ResourceNotFound()
        row = rows[0]
        if row.policy_id is None:
            return PlantPolicyState(loaded=False)
        return PlantPolicyState(
            loaded=True,
            policy_id=_uuid(row.policy_id),
            version=int(row.version),
            signed_at=row.signed_at,
            signed_by_display_name=row.signed_by_display_name,
            legal_opinion_reference=row.legal_opinion_reference,
            document_sha256=_json(row.document_ref).get("sha256"),
            criteria_summary_es=row.criteria_summary_es,
        )

    async def current_agreement(
        self, context: ScopeContext, zone_id: uuid.UUID
    ) -> CurrentAgreement | None:
        """El acuerdo de uso aprobado de la zona con sus firmantes y confirmaciones."""
        zone_id = _identifier(zone_id, "zone_id")
        rows = await self._database.read(
            context, _CURRENT_AGREEMENT, {**scope_parameters(context), "zone_id": zone_id}
        )
        if not rows:
            raise ResourceNotFound()
        row = rows[0]
        if row.agreement_id is None:
            return None
        confirmed = {
            _uuid(user): at for user, at in zip(row.confirmed_users, row.confirmed_ats, strict=True)
        }
        signatories = tuple(
            AgreementSignatory(
                role=signatory.role,
                user_id=signatory.user_id,
                display_name=signatory.display_name,
                confirmed_at=confirmed.get(signatory.user_id),
            )
            for signatory in (signatory_from_json(item) for item in _json(row.signatories))
        )
        document = _json(row.document_ref)
        return CurrentAgreement(
            agreement_id=_uuid(row.agreement_id),
            zone_id=_uuid(row.visible),
            status=AgreementStatus(row.status),
            approved_at=row.approved_at,
            approved_by=_uuid(row.approved_by),
            signatories=signatories,
            document_sha256=None if document is None else document.get("sha256"),
            replaces_agreement_id=_optional_uuid(row.replaces_agreement_id),
        )
