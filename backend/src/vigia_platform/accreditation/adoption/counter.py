"""El contador de actividad del lazo por zona y día (LC-LAZ-29; DE §3.6; BR-LAZ-134 y 135; H-33).

``record_loop_activity(transaction, plant_id=, zone_id=, day=, kind=, at=)`` suma **una**
actividad de clase ``kind`` a la fila ``(organization_id, zone_id, day)`` de
``accreditation.adoption_counter`` con **una** sentencia ``INSERT … ON CONFLICT DO UPDATE``: el
incremento lo hace la base sobre la fila bloqueada, así que dos escrituras a la vez nunca pierden
una cuenta (prueba concurrente en ``tests/integration/test_adoption_task.py``). ``day`` es la
fecha **local de la planta** (DE §3.6) del instante de la escritura.

**Lo que se guarda** es exactamente lo que H-33 pide («el nivel de uso por zona y no por usuario
individual»): por zona y día, cuántas actividades de cada ``loop_activity_kind``
(``activity_counts``), cuántas en total (``session_count``: el número de actividades del día, sin
identificador de sesión ni de usuario; el paquete mensual cuenta como activo el día con
``session_count > 0``) y la marca de la última (``last_activity_at``). La tabla no tiene ninguna
columna de usuario y este módulo **no lee** la auditoría de U-02 (BR-LAZ-134): la huella por actor
ya existe una sola vez, allí.

**Qué escrituras cuentan** (``LOOP_ACTIVITY_BY_RECORD_TYPE``, BR-LAZ-134: clasificación,
resolución de cola, avance de análisis, gestión de acción y cierre): los registros del lazo que
escribe una persona por ``LoopProjectionStore.write_and_apply``. No cuentan los del nodo (U-03),
los que escribe el sistema (``action_overdue``, ``late_segment_attached``) ni los de la
acreditación, el vocabulario y la configuración (``NOT_LOOP_ACTIVITY``): ninguno es atención de una
zona. Toda clase de registro proyectada está en exactamente uno de los dos conjuntos.

``inbox_opened`` (la apertura de la bandeja) existe en la lista cerrada pero ninguna escritura la
produce: abrir la bandeja es una lectura de una sola sentencia (LC-LAZ-05) y este gancho solo
viaja con las escrituras del lazo.

``write_activity`` es el gancho que ``LoopProjectionStore.apply_in_transaction`` llama con cada
recibo ``accepted`` del escritor, **en la misma transacción** que el registro: si se revierte, no
queda ni el registro ni la cuenta (BR-LAZ-146). Toma el candado de la fila ``(zona, día)`` del
contador **después** de los de la proyección (paso 4 del orden de ``loop.projection.store``) y
ninguna otra operación toma un candado después de él.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import date, datetime
from types import MappingProxyType
from typing import Final

from sqlalchemy import text

from vigia_platform.loop.calendar import local_date
from vigia_platform.loop.domain.enums import LoopActivityKind
from vigia_platform.shared.db import Transaction

__all__ = [
    "LOOP_ACTIVITY_BY_RECORD_TYPE",
    "NOT_LOOP_ACTIVITY",
    "PlantTimezoneMissing",
    "activity_day",
    "plant_timezone",
    "record_loop_activity",
    "write_activity",
]

_K = LoopActivityKind

LOOP_ACTIVITY_BY_RECORD_TYPE: Final[Mapping[str, LoopActivityKind]] = MappingProxyType(
    {
        "classification": _K.FINDING_CLASSIFIED,
        "review_resolution": _K.QUEUE_RESOLVED,
        "root_cause_analysis": _K.ANALYSIS_ADVANCED,
        "action_assigned": _K.ACTION_MANAGED,
        "action_evidence_added": _K.ACTION_MANAGED,
        "reopening": _K.ACTION_MANAGED,
        "closure": _K.FINDING_CLOSED,
        "unclosable_state_declared": _K.FINDING_CLOSED,
        "unclosable_review": _K.FINDING_CLOSED,
    }
)
"""Las escrituras del lazo que son actividad de una zona, con su clase (BR-LAZ-134)."""

NOT_LOOP_ACTIVITY: Final = frozenset(
    {
        # U-03: lo escribe el nodo, no una persona que atiende la zona.
        "finding_received",
        "detection_for_review_received",
        # Lo escribe el sistema (tareas y consumidores).
        "action_overdue",
        "late_segment_attached",
        # Acreditación, vocabulario y configuración: no son atención de una zona.
        "export_produced",
        "monthly_package_produced",
        "sample_audit_result",
        "commitments_recorded",
        "vocabulary_list_changed",
        "loop_configuration_changed",
    }
)
"""Los tipos proyectados que no cuentan como actividad (la partición la comprueba una prueba)."""

_PLANT_TIMEZONE: Final = text(
    "SELECT timezone FROM identity.plant"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
)
_ZONE_OF_SUBJECT: Final = text(
    "SELECT zone_id FROM loop.finding_state WHERE organization_id = :organization_id"
    " AND plant_id = :plant_id AND subject_id = :subject_id LIMIT 1"
)
_RECORD: Final = text(
    "INSERT INTO accreditation.adoption_counter (organization_id, plant_id, zone_id, day,"
    " activity_counts, session_count, last_activity_at)"
    " VALUES (:organization_id, :plant_id, :zone_id, :day,"
    " jsonb_build_object(CAST(:kind AS text), 1), 1, :at)"
    " ON CONFLICT (organization_id, zone_id, day) DO UPDATE SET"
    " activity_counts = jsonb_set(accreditation.adoption_counter.activity_counts,"
    " ARRAY[CAST(:kind AS text)],"
    " to_jsonb(COALESCE((accreditation.adoption_counter.activity_counts"
    " ->> CAST(:kind AS text))::integer, 0) + 1)),"
    " session_count = accreditation.adoption_counter.session_count + 1,"
    " last_activity_at = GREATEST(accreditation.adoption_counter.last_activity_at,"
    " EXCLUDED.last_activity_at)"
)
"""Una actividad más en la fila ``(zona, día)``: el incremento lo hace la base, sobre la fila."""


class PlantTimezoneMissing(LookupError):
    """La planta de la escritura no es visible en la transacción: no hay día local que contar."""


def activity_day(at: datetime, timezone: str) -> date:
    """La fecha local de la planta en la que cae ``at`` (DE §3.6: «fecha en la zona horaria de la
    planta»)."""
    return local_date(at, timezone)


async def plant_timezone(transaction: Transaction, plant_id: uuid.UUID) -> str:
    """``identity.plant.timezone`` de la planta, en la transacción del llamador."""
    row = (
        await transaction.execute(
            _PLANT_TIMEZONE,
            {"organization_id": transaction.context.organization_id, "plant_id": plant_id},
        )
    ).first()
    if row is None:
        raise PlantTimezoneMissing(str(plant_id))
    return str(row.timezone)


async def record_loop_activity(
    transaction: Transaction,
    *,
    plant_id: uuid.UUID,
    zone_id: uuid.UUID,
    day: date,
    kind: LoopActivityKind,
    at: datetime,
) -> None:
    """Suma una actividad ``kind`` a la zona en ``day`` (fecha local), en ``transaction``."""
    await transaction.execute(
        _RECORD,
        {
            "organization_id": transaction.context.organization_id,
            "plant_id": plant_id,
            "zone_id": zone_id,
            "day": day,
            "kind": LoopActivityKind(kind).value,
            "at": at,
        },
    )


async def write_activity(
    transaction: Transaction,
    *,
    record_type: str,
    plant_id: uuid.UUID,
    zone_id: uuid.UUID | None,
    subject_id: uuid.UUID | None,
    at: datetime,
) -> LoopActivityKind | None:
    """El gancho de las escrituras del lazo: cuenta la del registro ``record_type`` escrito en
    ``at`` (la hora del ``Clock`` del escritor; nunca ``received_at``, que es la de la base).

    ``zone_id`` es la zona del hallazgo si el llamador ya la conoce; si no, se busca por
    ``subject_id`` en la proyección (las entradas de evidencia no cambian el hallazgo). Devuelve la
    clase contada, o ``None`` si el tipo no es actividad o el sujeto no tiene zona proyectada."""
    kind = LOOP_ACTIVITY_BY_RECORD_TYPE.get(record_type)
    if kind is None:
        return None
    if zone_id is None and subject_id is not None:
        row = (
            await transaction.execute(
                _ZONE_OF_SUBJECT,
                {
                    "organization_id": transaction.context.organization_id,
                    "plant_id": plant_id,
                    "subject_id": subject_id,
                },
            )
        ).first()
        zone_id = None if row is None else uuid.UUID(str(row.zone_id))
    if zone_id is None:
        return None
    day = activity_day(at, await plant_timezone(transaction, plant_id))
    await record_loop_activity(
        transaction, plant_id=plant_id, zone_id=zone_id, day=day, kind=kind, at=at
    )
    return kind
