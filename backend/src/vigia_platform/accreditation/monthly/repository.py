"""Acceso a datos del paquete mensual (TASK-342 a 344; LC-LAZ-24; C-PLA-30; BR-LAZ-102).

``MonthlyPackageRepository`` reúne las lecturas y escrituras de ``accreditation.monthly_package``,
``accreditation.audit_sample`` y ``accreditation.audit_sample_item``. Sus métodos reciben una
``Transaction`` **ya abierta**, como ``PostgresLoopConfigurationRepository.current``/``lock_plant``:
no abren transacción propia, porque el orquestador (``monthly.package.MonthlyPackageComposer``) es
quien decide dónde empieza y termina cada una (lecturas en la transacción barata de la tarea
periódica; ``claim`` y ``seal`` en sus propias transacciones cortas).

``claim`` es la valla de «una sola vez por zona y periodo»: una fila ``ON CONFLICT (organization_id,
zone_id, period)`` que solo se mueve a ``generating`` si no estaba ya ``sealed``. ``seal`` es la
valla simétrica: solo sella si ``package_id`` sigue siendo el de esta composición (si otra
composición ganó la carrera entre medias, ``UPDATE ... WHERE package_id = :package_id`` no afecta
ninguna fila y ``seal`` devuelve ``False``; el llamador descarta su intento, nunca escribe el
expediente de un paquete que perdió).

``acquire_lease``/``lease_still_valid``/``release_lease`` (rework 1, VIG-239: B2/B3 de la
revisión) serializan la composición sobre ``accreditation.monthly_package_lease`` (``laz_0035``):
solo quien tiene el arrendamiento vigente sube el zip a ``vigia-evidence``, para que un perdedor
nunca sobrescriba el objeto de un paquete ya sellado por otro.
"""

from __future__ import annotations

import contextlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Final, Protocol

from sqlalchemy import text

from vigia_platform.shared.context import ScopeContext, ScopeLevel, repository
from vigia_platform.shared.db import Transaction

__all__ = [
    "AuditSampleItemRow",
    "MonthlyPackageLease",
    "MonthlyPackageRepository",
    "MonthlyPackageRow",
    "SampleItemRow",
    "ZoneRef",
    "date_to_period",
    "object_key",
    "period_to_date",
]

_PERIOD: Final = re.compile(r"^([0-9]{4})-(0[1-9]|1[0-2])$")


def period_to_date(period: str) -> date:
    """``"AAAA-MM"`` → el primer día de ese mes."""
    match = _PERIOD.fullmatch(period)
    if match is None:
        raise ValueError("period debe tener la forma AAAA-MM")
    return date(int(match.group(1)), int(match.group(2)), 1)


def date_to_period(value: date) -> str:
    """El primer día de un mes (columna ``period``) → ``"AAAA-MM"``."""
    return f"{value.year:04d}-{value.month:02d}"


def object_key(
    *, organization_id: uuid.UUID, plant_id: uuid.UUID, zone_id: uuid.UUID, period: str
) -> str:
    """La clave del paquete en ``vigia-evidence``: única por ``(zone_id, period)``."""
    return f"org/{organization_id}/plant/{plant_id}/zone/{zone_id}/monthly/{period}/package.zip"


def _json(value: object) -> Any:
    return json.loads(value) if isinstance(value, str | bytes) else value


class TransactionSource(Protocol):
    """``shared.db.Database``: el único camino a la base (como en ``export.lease``)."""

    def transaction(
        self, context: ScopeContext
    ) -> contextlib.AbstractAsyncContextManager[Transaction]: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class ZoneRef:
    """Una zona de la organización, con su planta."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class MonthlyPackageRow:
    """Una fila de ``accreditation.monthly_package``, tal como la leen las rutas de lectura."""

    package_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    period: str
    state: str
    generated_at: datetime
    findings_summary: Mapping[str, Any]
    recurrence_summary: Sequence[Mapping[str, Any]]
    coverage_summary: Mapping[str, Any]
    commitments_from_previous_period: Sequence[Mapping[str, Any]]
    audit_sample_id: uuid.UUID | None
    sealed_checkpoint_record_id: uuid.UUID | None
    record_id: uuid.UUID | None


@dataclass(frozen=True, slots=True, kw_only=True)
class SampleItemRow:
    """Una fila de ``accreditation.audit_sample_item`` con su clasificación, para ``GET
    .../sample`` (TASK-320: «con su clasificación, motivo, autor, fecha»).

    ``outcome``/``comment``/``reviewed_by``/``reviewed_at`` son la revisión por muestra
    (``sample_audit_outcome``, TASK-347, nulos hasta entonces); ``classification_outcome``,
    ``reason_category``, ``classified_by_role`` y ``classified_at`` son los de la clasificación
    original, leídos de ``loop.classification`` (nunca el nombre de la persona, P3: solo el rol
    en uso, ``classified_by_role``)."""

    sample_item_id: uuid.UUID
    classification_id: uuid.UUID
    subject_id: uuid.UUID
    outcome: str | None
    comment: str | None
    reviewed_by: Mapping[str, Any] | None
    reviewed_at: datetime | None
    classification_outcome: str
    reason_category: str
    classified_by_role: str | None
    classified_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditSampleItemRow:
    """Un elemento seleccionado de la muestra, listo para ``insert_audit_sample_items``."""

    sample_item_id: uuid.UUID
    sample_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    classification_id: uuid.UUID
    subject_id: uuid.UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class MonthlyPackageLease:
    """Un arrendamiento vigente de ``accreditation.monthly_package_lease`` (rework 1, VIG-239).

    Serializa la composición de un ``(zone_id, period)``: solo quien lo tiene vigente sube el
    zip y sella (P4/P5, BR-LAZ-108) — sin esto, dos composiciones del mismo periodo reclaman la
    misma fila de ``monthly_package`` (``claim`` solo excluye un periodo ya ``sealed``, no uno en
    curso) y ambas podrían subir a la misma clave de ``vigia-evidence`` sin condición."""

    organization_id: uuid.UUID
    zone_id: uuid.UUID
    period: str
    owner: str
    until: datetime
    attempt_count: int


# --- sentencias ----------------------------------------------------------------------------

_ZONES_OF_ORGANIZATION: Final = text(
    "SELECT organization_id, plant_id, zone_id FROM identity.zone"
    " WHERE organization_id = :organization_id ORDER BY plant_id, zone_id"
)

_SEALED_PERIODS: Final = text(
    "SELECT period FROM accreditation.monthly_package"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id AND state = 'sealed'"
)

_CLAIM: Final = text(
    "INSERT INTO accreditation.monthly_package (package_id, organization_id, plant_id, zone_id,"
    " period, state, generated_at, findings_summary, recurrence_summary, coverage_summary,"
    " commitments_from_previous_period)"
    " VALUES (:package_id, :organization_id, :plant_id, :zone_id, CAST(:period AS date),"
    " 'generating', :generated_at, '{}'::jsonb, '[]'::jsonb, '{}'::jsonb, '[]'::jsonb)"
    " ON CONFLICT (organization_id, zone_id, period) DO UPDATE SET"
    " state = 'generating', generated_at = EXCLUDED.generated_at"
    " WHERE accreditation.monthly_package.state != 'sealed'"
    " RETURNING package_id"
)
"""La valla de claim: solo toma la fila si no estaba ``sealed`` (BR-LAZ-102: un mes sellado no se
rehace). **Nunca** toca ``package_id`` ni ``plant_id`` en el ``DO UPDATE``: la migración
(``laz_0031``, ``_MUTABLE_COLUMNS``) no concede ``UPDATE`` sobre esas columnas a ``vigia_app``, y
Postgres comprueba el privilegio de toda la cláusula ``SET`` aunque el conflicto no llegue a
aplicarse. Por eso ``claim`` devuelve el ``package_id`` **de la fila** (el recién insertado, o el
de un intento anterior sin sellar que se retoma con su propia identidad) en vez del candidato que
recibió: el llamador adopta ese identificador para todo lo que sigue. ``coverage_summary`` queda
``{}`` de reserva (objeto vacío, como exige ``monthly_package_coverage``); ``seal`` la rellena."""

_SEAL_PACKAGE: Final = text(
    "UPDATE accreditation.monthly_package SET state = 'sealed',"
    " findings_summary = CAST(:findings_summary AS jsonb),"
    " recurrence_summary = CAST(:recurrence_summary AS jsonb),"
    " metric_snapshot_id = :metric_snapshot_id,"
    " exposure_indicator_id = :exposure_indicator_id,"
    " coverage_summary = CAST(:coverage_summary AS jsonb),"
    " commitments_from_previous_period = CAST(:commitments_from_previous_period AS jsonb),"
    " audit_sample_id = :audit_sample_id,"
    " sealed_checkpoint_record_id = :sealed_checkpoint_record_id,"
    " record_id = :record_id"
    " WHERE package_id = :package_id AND organization_id = :organization_id"
    " AND state != 'sealed'"
    " RETURNING package_id"
)
"""La valla de sellado: ``package_id`` ya identifica la fila sin ambigüedad (no cambia tras
``claim``), pero dos composiciones pueden resolver el **mismo** ``package_id`` (una tomó la fila
ya existente de la otra) y componer en paralelo sin colisionar entre sí (cada una inserta su
propia ``audit_sample``); ``AND state != 'sealed'`` es la valla real: solo la primera ``UPDATE``
que llega la sella, la segunda no afecta ninguna fila y ``seal`` devuelve ``False``."""

_ACQUIRE_LEASE: Final = text(
    "INSERT INTO accreditation.monthly_package_lease"
    " (organization_id, plant_id, zone_id, period, lease_owner, lease_until, attempt_count)"
    " VALUES (:organization_id, :plant_id, :zone_id, CAST(:period AS date), :owner, :until, 1)"
    " ON CONFLICT (organization_id, zone_id, period) DO UPDATE SET"
    " lease_owner = :owner, lease_until = :until,"
    " attempt_count = accreditation.monthly_package_lease.attempt_count + 1"
    " WHERE accreditation.monthly_package_lease.lease_owner IS NULL"
    " OR accreditation.monthly_package_lease.lease_until < :now"
    " RETURNING attempt_count"
)
"""La valla del arrendamiento (PAT-LAZ-RES-05, símil de ``export.lease``): una sola sentencia que
gana como mucho un proceso, igual si la fila no existía o si el arrendamiento anterior venció
(``lease_until < :now``, con el ``Clock`` inyectado, nunca la hora de la base)."""

_LEASE_STILL_VALID: Final = text(
    "SELECT 1 FROM accreditation.monthly_package_lease"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id"
    " AND period = CAST(:period AS date) AND lease_owner = :owner AND lease_until >= :now"
)
"""La comprobación antes de subir el zip (B3 del rework): si el arrendamiento ya no es de este
proceso (vencido o tomado por otro), no se sube nada: el objeto de ``vigia-evidence`` nunca
queda sin dueño vigente que responda por él."""

_RELEASE_LEASE: Final = text(
    "UPDATE accreditation.monthly_package_lease SET lease_owner = NULL, lease_until = NULL"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id"
    " AND period = CAST(:period AS date) AND lease_owner = :owner"
    " RETURNING organization_id"
)

_INSERT_AUDIT_SAMPLE: Final = text(
    "INSERT INTO accreditation.audit_sample (sample_id, organization_id, plant_id, zone_id,"
    " package_id, population_size, sample_size, seed, strata, selected_at, reviewer_user_id)"
    " VALUES (:sample_id, :organization_id, :plant_id, :zone_id, :package_id, :population_size,"
    " :sample_size, :seed, CAST(:strata AS jsonb), :selected_at, :reviewer_user_id)"
)
"""``reviewer_user_id`` es ``NOT NULL`` en el esquema (quien revisará, DE §3.3): el coordinador
SST de la zona, resuelto antes de insertar. ``review_completed_at`` queda sin fijar (``NULL``):
la revisión en sí es de TASK-347, que aún no existe en ``main``."""

_COORDINATOR_OF_ZONE: Final = text(
    "SELECT user_id FROM identity.role_assignment"
    " WHERE organization_id = :organization_id AND role = 'coordinator_sst'"
    " AND removed_at IS NULL"
    " AND ((scope_level = 'zone' AND scope_id = :zone_id)"
    " OR (scope_level = 'plant' AND scope_id = :plant_id)"
    " OR (scope_level = 'organization' AND scope_id = :organization_id))"
    " ORDER BY (scope_level = 'zone') DESC, (scope_level = 'plant') DESC, user_id LIMIT 1"
)
"""El coordinador más concreto (zona, luego planta, luego organización) con una asignación
vigente que cubre la zona (``identity.role_assignment``, sin pasar por ``IdentityQueryPort``
para no acoplar la composición mensual a la maquinaria de destinatarios de notificaciones)."""

_INSERT_AUDIT_SAMPLE_ITEM: Final = text(
    "INSERT INTO accreditation.audit_sample_item (sample_item_id, organization_id, plant_id,"
    " zone_id, sample_id, classification_id, subject_id)"
    " VALUES (:sample_item_id, :organization_id, :plant_id, :zone_id, :sample_id,"
    " :classification_id, :subject_id)"
)

_ADOPTION_DAYS: Final = text(
    "SELECT count(*) AS active_days FROM accreditation.adoption_counter"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id"
    " AND day >= :period_start AND day < :period_end AND session_count > 0"
)

_ZONE_PLANT: Final = text(
    "SELECT plant_id FROM identity.zone WHERE organization_id = :organization_id"
    " AND zone_id = :zone_id"
)

_PLANT_OF_ORGANIZATION: Final = text(
    "SELECT 1 FROM identity.plant p WHERE p.organization_id = :organization_id"
    " AND p.plant_id = :plant_id AND (:organization_wide"
    " OR p.plant_id = ANY(CAST(:plant_ids AS uuid[]))"
    " OR EXISTS (SELECT 1 FROM identity.zone z WHERE z.organization_id = p.organization_id"
    " AND z.plant_id = p.plant_id AND z.zone_id = ANY(CAST(:zone_ids AS uuid[]))))"
)

_FIND_PACKAGE: Final = text(
    "SELECT package_id, organization_id, plant_id, zone_id, period, state, generated_at,"
    " findings_summary, recurrence_summary, coverage_summary, commitments_from_previous_period,"
    " audit_sample_id, sealed_checkpoint_record_id, record_id"
    " FROM accreditation.monthly_package"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id"
    " AND period = CAST(:period AS date)"
)

_PACKAGE_BY_ID: Final = text(
    "SELECT package_id, organization_id, plant_id, zone_id, period, state, generated_at,"
    " findings_summary, recurrence_summary, coverage_summary, commitments_from_previous_period,"
    " audit_sample_id, sealed_checkpoint_record_id, record_id"
    " FROM accreditation.monthly_package"
    " WHERE organization_id = :organization_id AND package_id = :package_id"
)

_SEALED_PACKAGES_OF_PLANT: Final = text(
    "SELECT package_id, organization_id, plant_id, zone_id, period, state, generated_at,"
    " findings_summary, recurrence_summary, coverage_summary, commitments_from_previous_period,"
    " audit_sample_id, sealed_checkpoint_record_id, record_id"
    " FROM accreditation.monthly_package"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " AND period = CAST(:period AS date) AND state = 'sealed' ORDER BY zone_id"
)

_SAMPLE_ITEMS: Final = text(
    "SELECT ai.sample_item_id, ai.classification_id, ai.subject_id, ai.outcome, ai.comment,"
    " ai.reviewed_by, ai.reviewed_at, c.outcome AS classification_outcome, c.reason_category,"
    " c.classified_by, c.classified_at"
    " FROM accreditation.audit_sample_item AS ai"
    " JOIN loop.classification AS c"
    " ON c.organization_id = ai.organization_id AND c.classification_id = ai.classification_id"
    " WHERE ai.organization_id = :organization_id AND ai.sample_id = :sample_id"
    " ORDER BY ai.sample_item_id"
)
"""Cada elemento con su clasificación (TASK-320: «con su clasificación, motivo, autor, fecha»):
``c.outcome``/``c.reason_category``/``c.classified_at`` son columnas directas de
``loop.classification``; el autor se lee de ``c.classified_by`` (``role_in_use``, nunca el
nombre, P3)."""

_PREVIOUS_COMMITMENTS: Final = text(
    "SELECT c.commitment_id, c.state FROM accreditation.commitment AS c"
    " JOIN accreditation.monthly_package AS p ON p.package_id = c.package_id"
    " WHERE c.organization_id = :organization_id AND c.zone_id = :zone_id"
    " AND p.organization_id = :organization_id AND p.zone_id = :zone_id"
    " AND p.period = CAST(:previous_period AS date) AND p.state = 'sealed'"
    " ORDER BY c.commitment_id"
)


def _package_row(row: Any) -> MonthlyPackageRow:
    return MonthlyPackageRow(
        package_id=uuid.UUID(str(row.package_id)),
        organization_id=uuid.UUID(str(row.organization_id)),
        plant_id=uuid.UUID(str(row.plant_id)),
        zone_id=uuid.UUID(str(row.zone_id)),
        period=date_to_period(row.period),
        state=str(row.state),
        generated_at=row.generated_at,
        findings_summary=_json(row.findings_summary),
        recurrence_summary=_json(row.recurrence_summary),
        coverage_summary=_json(row.coverage_summary),
        commitments_from_previous_period=_json(row.commitments_from_previous_period),
        audit_sample_id=(
            None if row.audit_sample_id is None else uuid.UUID(str(row.audit_sample_id))
        ),
        sealed_checkpoint_record_id=(
            None
            if row.sealed_checkpoint_record_id is None
            else uuid.UUID(str(row.sealed_checkpoint_record_id))
        ),
        record_id=None if row.record_id is None else uuid.UUID(str(row.record_id)),
    )


def _sample_item_row(row: Any) -> SampleItemRow:
    classified_by = _json(row.classified_by)
    role = classified_by.get("role_in_use") if isinstance(classified_by, Mapping) else None
    return SampleItemRow(
        sample_item_id=uuid.UUID(str(row.sample_item_id)),
        classification_id=uuid.UUID(str(row.classification_id)),
        subject_id=uuid.UUID(str(row.subject_id)),
        outcome=row.outcome,
        comment=row.comment,
        reviewed_by=None if row.reviewed_by is None else _json(row.reviewed_by),
        reviewed_at=row.reviewed_at,
        classification_outcome=str(row.classification_outcome),
        reason_category=str(row.reason_category),
        classified_by_role=None if role is None else str(role),
        classified_at=row.classified_at,
    )


@repository
class MonthlyPackageRepository:
    """Lecturas y escrituras del paquete mensual, en la transacción que aporta el llamador."""

    def __init__(self, *, database: TransactionSource) -> None:
        self._database = database

    async def zones_of_organization(self, transaction: Transaction) -> tuple[ZoneRef, ...]:
        """Las zonas de la organización del contexto, en orden de planta y zona."""
        rows = (
            await transaction.execute(
                _ZONES_OF_ORGANIZATION,
                {"organization_id": transaction.context.organization_id},
            )
        ).all()
        return tuple(
            ZoneRef(
                organization_id=uuid.UUID(str(row.organization_id)),
                plant_id=uuid.UUID(str(row.plant_id)),
                zone_id=uuid.UUID(str(row.zone_id)),
            )
            for row in rows
        )

    async def sealed_periods(self, transaction: Transaction, zone_id: uuid.UUID) -> frozenset[str]:
        """Los periodos (``"AAAA-MM"``) ya sellados de ``zone_id``."""
        rows = (
            await transaction.execute(
                _SEALED_PERIODS,
                {"organization_id": transaction.context.organization_id, "zone_id": zone_id},
            )
        ).all()
        return frozenset(date_to_period(row.period) for row in rows)

    async def claim(
        self,
        transaction: Transaction,
        *,
        package_id: uuid.UUID,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        period: str,
        generated_at: datetime,
    ) -> uuid.UUID | None:
        """Reclama ``(zone_id, period)``; ``None`` si ya estaba sellado.

        Devuelve el ``package_id`` **de la fila** (puede no ser ``package_id``: una fila sin
        sellar de un intento anterior conserva su propia identidad, nunca se reescribe)."""
        row = (
            await transaction.execute(
                _CLAIM,
                {
                    "package_id": package_id,
                    "organization_id": transaction.context.organization_id,
                    "plant_id": plant_id,
                    "zone_id": zone_id,
                    "period": period_to_date(period),
                    "generated_at": generated_at,
                },
            )
        ).first()
        return None if row is None else uuid.UUID(str(row.package_id))

    async def seal(
        self,
        transaction: Transaction,
        *,
        package_id: uuid.UUID,
        findings_summary: Mapping[str, Any],
        recurrence_summary: Sequence[Mapping[str, Any]],
        metric_snapshot_id: uuid.UUID | None,
        exposure_indicator_id: uuid.UUID | None,
        coverage_summary: Mapping[str, Any],
        commitments_from_previous_period: Sequence[Mapping[str, Any]],
        audit_sample_id: uuid.UUID | None,
        sealed_checkpoint_record_id: uuid.UUID | None,
        record_id: uuid.UUID | None,
    ) -> bool:
        """Sella ``package_id``; ``False`` si otra composición ya se lo llevó entre medias."""
        row = (
            await transaction.execute(
                _SEAL_PACKAGE,
                {
                    "package_id": package_id,
                    "organization_id": transaction.context.organization_id,
                    "findings_summary": json.dumps(dict(findings_summary), ensure_ascii=False),
                    "recurrence_summary": json.dumps(
                        [dict(entry) for entry in recurrence_summary], ensure_ascii=False
                    ),
                    "metric_snapshot_id": metric_snapshot_id,
                    "exposure_indicator_id": exposure_indicator_id,
                    "coverage_summary": json.dumps(dict(coverage_summary), ensure_ascii=False),
                    "commitments_from_previous_period": json.dumps(
                        [dict(entry) for entry in commitments_from_previous_period],
                        ensure_ascii=False,
                    ),
                    "audit_sample_id": audit_sample_id,
                    "sealed_checkpoint_record_id": sealed_checkpoint_record_id,
                    "record_id": record_id,
                },
            )
        ).first()
        return row is not None

    async def acquire_lease(
        self,
        transaction: Transaction,
        *,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        period: str,
        owner: str,
        now: datetime,
        duration: timedelta,
    ) -> MonthlyPackageLease | None:
        """Reclama el arrendamiento de ``(zone_id, period)``; ``None`` si ya lo tiene otro
        proceso con un arrendamiento vigente (rework 1, VIG-239: B2/B3)."""
        if not isinstance(owner, str) or not 1 <= len(owner) <= 128:
            raise ValueError("owner debe tener de 1 a 128 caracteres")
        until = now + duration
        row = (
            await transaction.execute(
                _ACQUIRE_LEASE,
                {
                    "organization_id": transaction.context.organization_id,
                    "plant_id": plant_id,
                    "zone_id": zone_id,
                    "period": period_to_date(period),
                    "owner": owner,
                    "until": until,
                    "now": now,
                },
            )
        ).first()
        if row is None:
            return None
        return MonthlyPackageLease(
            organization_id=transaction.context.organization_id,
            zone_id=zone_id,
            period=period,
            owner=owner,
            until=until,
            attempt_count=int(row.attempt_count),
        )

    async def lease_still_valid(
        self,
        transaction: Transaction,
        *,
        zone_id: uuid.UUID,
        period: str,
        owner: str,
        now: datetime,
    ) -> bool:
        """Si ``owner`` sigue teniendo el arrendamiento de ``(zone_id, period)`` en ``now``
        (comprobación justo antes de subir el zip, B3: nunca se sube sin dueño vigente)."""
        row = (
            await transaction.execute(
                _LEASE_STILL_VALID,
                {
                    "organization_id": transaction.context.organization_id,
                    "zone_id": zone_id,
                    "period": period_to_date(period),
                    "owner": owner,
                    "now": now,
                },
            )
        ).first()
        return row is not None

    async def release_lease(
        self, transaction: Transaction, *, zone_id: uuid.UUID, period: str, owner: str
    ) -> bool:
        """Libera el arrendamiento si sigue siendo de ``owner`` (fallo no declarado: la fila de
        ``monthly_package`` sigue en ``generating``, se retoma al día siguiente)."""
        row = (
            await transaction.execute(
                _RELEASE_LEASE,
                {
                    "organization_id": transaction.context.organization_id,
                    "zone_id": zone_id,
                    "period": period_to_date(period),
                    "owner": owner,
                },
            )
        ).first()
        return row is not None

    async def insert_audit_sample(
        self,
        transaction: Transaction,
        *,
        sample_id: uuid.UUID,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        package_id: uuid.UUID,
        population_size: int,
        sample_size: int,
        seed: int,
        strata: Sequence[Mapping[str, Any]],
        selected_at: datetime,
        reviewer_user_id: uuid.UUID,
    ) -> None:
        """La fila de ``audit_sample``; ``reviewer_user_id`` es obligatorio en el esquema (quien
        revisará, no quien ya revisó): el coordinador SST de la zona. La revisión en sí
        (``review_completed_at``, TASK-347) la completa después."""
        await transaction.execute(
            _INSERT_AUDIT_SAMPLE,
            {
                "sample_id": sample_id,
                "organization_id": transaction.context.organization_id,
                "plant_id": plant_id,
                "zone_id": zone_id,
                "package_id": package_id,
                "population_size": population_size,
                "sample_size": sample_size,
                "seed": seed,
                "strata": json.dumps([dict(entry) for entry in strata], ensure_ascii=False),
                "selected_at": selected_at,
                "reviewer_user_id": reviewer_user_id,
            },
        )

    async def coordinator_user_id(
        self,
        transaction: Transaction,
        *,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
    ) -> uuid.UUID | None:
        """Un coordinador SST vigente que cubre ``zone_id`` (por zona, planta u organización, el
        más concreto primero); ``None`` si la zona todavía no tiene ninguno asignado (la
        composición lo declara y se retoma al día siguiente, nunca inventa un revisor)."""
        row = (
            await transaction.execute(
                _COORDINATOR_OF_ZONE,
                {
                    "organization_id": transaction.context.organization_id,
                    "plant_id": plant_id,
                    "zone_id": zone_id,
                },
            )
        ).first()
        return None if row is None else uuid.UUID(str(row.user_id))

    async def insert_audit_sample_items(
        self, transaction: Transaction, *, items: Sequence[AuditSampleItemRow]
    ) -> None:
        """Las filas de ``audit_sample_item``, una por elemento seleccionado (a lo sumo 30)."""
        organization_id = transaction.context.organization_id
        for item in items:
            await transaction.execute(
                _INSERT_AUDIT_SAMPLE_ITEM,
                {
                    "sample_item_id": item.sample_item_id,
                    "organization_id": organization_id,
                    "plant_id": item.plant_id,
                    "zone_id": item.zone_id,
                    "sample_id": item.sample_id,
                    "classification_id": item.classification_id,
                    "subject_id": item.subject_id,
                },
            )

    async def adoption_days(
        self,
        transaction: Transaction,
        *,
        zone_id: uuid.UUID,
        period_start: date,
        period_end_exclusive: date,
    ) -> int:
        """Días del periodo con alguna actividad del lazo (``session_count > 0``).

        ``accreditation.adoption_counter`` lo alimentan las escrituras del lazo
        (``accreditation.adoption.counter``, TASK-323): una fila por zona y día local con alguna
        actividad, y ``session_count`` es el número de actividades de ese día.
        """
        row = (
            await transaction.execute(
                _ADOPTION_DAYS,
                {
                    "organization_id": transaction.context.organization_id,
                    "zone_id": zone_id,
                    "period_start": period_start,
                    "period_end": period_end_exclusive,
                },
            )
        ).first()
        return 0 if row is None else int(row.active_days)

    async def previous_commitments(
        self, transaction: Transaction, *, zone_id: uuid.UUID, previous_period_start: date
    ) -> tuple[tuple[uuid.UUID, str], ...]:
        """Los compromisos del paquete sellado anterior de la zona, con su estado actual.

        ``accreditation.commitment`` ya existe en el esquema, pero ningún escritor de este
        repositorio la llena todavía: registrar un compromiso es de TASK-347/``notifications``.
        Mientras no exista ese escritor, la consulta es honesta: una zona sin compromisos
        registrados da la tupla vacía, nunca un valor inventado (P4, P6).
        """
        rows = (
            await transaction.execute(
                _PREVIOUS_COMMITMENTS,
                {
                    "organization_id": transaction.context.organization_id,
                    "zone_id": zone_id,
                    "previous_period": previous_period_start,
                },
            )
        ).all()
        return tuple((uuid.UUID(str(row.commitment_id)), str(row.state)) for row in rows)

    # --- lecturas de las rutas HTTP (SUP-U05-24, 25 y 41) ------------------------------------

    async def zone_plant(self, transaction: Transaction, zone_id: uuid.UUID) -> uuid.UUID | None:
        """La planta de ``zone_id`` en la organización del contexto, o ``None`` si no existe."""
        row = (
            await transaction.execute(
                _ZONE_PLANT,
                {"organization_id": transaction.context.organization_id, "zone_id": zone_id},
            )
        ).first()
        return None if row is None else uuid.UUID(str(row.plant_id))

    async def plant_in_scope(self, transaction: Transaction, plant_id: uuid.UUID) -> bool:
        """¿La planta existe y el contexto reducido tiene alcance sobre ella o alguna zona?

        Sin esta comprobación, la planta de otra organización (o una inexistente) no sale como
        ``not_found``: la lista de descargas (vacía por el filtro de ``organization_id``) echa
        igual el ``plant_id`` pedido en la respuesta, lo que la deja distinguible de un
        identificador realmente inexistente (PR-NUC-01).
        """
        scopes = transaction.context.allowed_scopes
        row = (
            await transaction.execute(
                _PLANT_OF_ORGANIZATION,
                {
                    "organization_id": transaction.context.organization_id,
                    "plant_id": plant_id,
                    "organization_wide": any(
                        scope.scope_level is ScopeLevel.ORGANIZATION
                        and scope.scope_id == transaction.context.organization_id
                        for scope in scopes
                    ),
                    "plant_ids": [
                        scope.scope_id for scope in scopes if scope.scope_level is ScopeLevel.PLANT
                    ],
                    "zone_ids": [
                        scope.scope_id for scope in scopes if scope.scope_level is ScopeLevel.ZONE
                    ],
                },
            )
        ).first()
        return row is not None

    async def find_package(
        self, transaction: Transaction, *, zone_id: uuid.UUID, period: str
    ) -> MonthlyPackageRow | None:
        """La fila ``(zone_id, period)`` en cualquier estado (la ruta decide si está disponible)."""
        row = (
            await transaction.execute(
                _FIND_PACKAGE,
                {
                    "organization_id": transaction.context.organization_id,
                    "zone_id": zone_id,
                    "period": period_to_date(period),
                },
            )
        ).first()
        return None if row is None else _package_row(row)

    async def package_by_id(
        self, transaction: Transaction, package_id: uuid.UUID
    ) -> MonthlyPackageRow | None:
        row = (
            await transaction.execute(
                _PACKAGE_BY_ID,
                {"organization_id": transaction.context.organization_id, "package_id": package_id},
            )
        ).first()
        return None if row is None else _package_row(row)

    async def sealed_packages_of_plant(
        self, transaction: Transaction, *, plant_id: uuid.UUID, period: str
    ) -> tuple[MonthlyPackageRow, ...]:
        """Los paquetes sellados de ``plant_id`` en ``period``, ordenados por zona."""
        rows = (
            await transaction.execute(
                _SEALED_PACKAGES_OF_PLANT,
                {
                    "organization_id": transaction.context.organization_id,
                    "plant_id": plant_id,
                    "period": period_to_date(period),
                },
            )
        ).all()
        return tuple(_package_row(row) for row in rows)

    async def sample_items(
        self, transaction: Transaction, *, sample_id: uuid.UUID
    ) -> tuple[SampleItemRow, ...]:
        rows = (
            await transaction.execute(
                _SAMPLE_ITEMS,
                {"organization_id": transaction.context.organization_id, "sample_id": sample_id},
            )
        ).all()
        return tuple(_sample_item_row(row) for row in rows)
