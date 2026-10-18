"""Filas sintéticas del esquema ``accreditation`` para las pruebas (TASK-338).

``ROW_BUILDERS`` da, por tabla, la sentencia y los parámetros de una fila nueva y válida de una
planta (``AccreditationScope``): ``seed_accreditation`` siembra con ellos al menos una fila de
cada una de las nueve tablas en cada planta de los dos clientes de ``tests/identity_db.py`` (el
par de organizaciones de U-02), y las pruebas de aislamiento los reutilizan para intentar escribir
desde un contexto de proveedor. La muestra, sus elementos y los compromisos cuelgan del paquete
mensual y de la muestra del ``AccreditationScope``, que ``seed_plant`` crea primero. El paquete de
exportación necesita una solicitud sin paquete: su constructor inserta las dos en una sentencia.
Cada fila de las tablas con clave por zona y periodo (o día) toma un mes (o día) nuevo.

Solo datos generados (NFR-CTR-43). Las marcas salen de ``BASE_TIME`` o del instante que se pase,
nunca de la hora real.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import itertools
import json
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from tests.identity_db import BASE_TIME, IdentitySeed, Tenant

ACCREDITATION_TABLES: Final = (
    "export_request",
    "export_package",
    "monthly_package",
    "audit_sample",
    "audit_sample_item",
    "commitment",
    "program_metric_snapshot",
    "exposure_indicator",
    "adoption_counter",
)
"""Las nueve tablas de ``accreditation`` (``laz_0031``), todas con planta."""

PERIOD_KEY_TABLES: Final = ("monthly_package", "program_metric_snapshot", "exposure_indicator")
"""Las de clave ``(organization_id, zone_id, period)``."""

SINGLE_ROW_TABLES: Final = frozenset[str]()
"""Ninguna: cada constructor toma un periodo, un día o una solicitud nuevos."""

_MONTHS: Final = itertools.count()
_DAYS: Final = itertools.count()


def next_month() -> dt.date:
    """Un primer día de mes que ninguna otra fila de la prueba usó."""
    index = next(_MONTHS)
    return dt.date(2001 + index // 12, 1 + index % 12, 1)


def next_day() -> dt.date:
    return dt.date(2001, 1, 1) + dt.timedelta(days=next(_DAYS))


def _hex64() -> str:
    return secrets.token_hex(32)


@dataclass(frozen=True)
class AccreditationScope:
    """Planta de un cliente con las filas de las que dependen las demás."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    user_id: uuid.UUID
    package_id: uuid.UUID = dataclasses.field(default_factory=uuid.uuid4)
    sample_id: uuid.UUID = dataclasses.field(default_factory=uuid.uuid4)


Statement = tuple[str, list[Any]]
Builder = Callable[[AccreditationScope], Statement]


def insert(table: str, values: dict[str, Any]) -> Statement:
    """``INSERT INTO accreditation.<table>`` con los valores en orden."""
    columns = ", ".join(values)
    placeholders = ", ".join(f"${index}" for index in range(1, len(values) + 1))
    sql = f"INSERT INTO accreditation.{table} ({columns}) VALUES ({placeholders})"  # noqa: S608
    return sql, list(values.values())


def export_request_values(
    scope: AccreditationScope,
    at: dt.datetime = BASE_TIME,
    *,
    export_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    return {
        "export_id": export_id or uuid.uuid4(),
        "organization_id": scope.organization_id,
        "plant_id": scope.plant_id,
        "zone_ids": [scope.zone_id],
        "period_from": at - dt.timedelta(days=30),
        "period_to": at,
        "requested_by": json.dumps({"user_id": str(scope.user_id), "role": "coordinator_sst"}),
        "user_id": user_id or scope.user_id,
        "requested_at": at,
        "package_form": "without_clips",
        "expires_at": at + dt.timedelta(days=7),
        "correlation_id": uuid.uuid4(),
    }


def export_request(
    scope: AccreditationScope, at: dt.datetime = BASE_TIME, **changes: Any
) -> Statement:
    return insert("export_request", export_request_values(scope, at) | changes)


def export_package(scope: AccreditationScope, at: dt.datetime = BASE_TIME) -> Statement:
    """Una solicitud nueva y su paquete, en una sola sentencia (la clave del paquete es la de la
    solicitud)."""
    request = export_request_values(scope, at)
    columns = ", ".join(request)
    placeholders = ", ".join(f"${index}" for index in range(1, len(request) + 1))
    package = [_hex64(), _hex64(), json.dumps({"manifest_version": 1}), 1024, 3, [uuid.uuid4()]]
    first = len(request) + 1
    sql = (
        f"WITH request AS (INSERT INTO accreditation.export_request ({columns})"  # noqa: S608
        f" VALUES ({placeholders}) RETURNING export_id, organization_id, plant_id)"
        " INSERT INTO accreditation.export_package (export_id, organization_id, plant_id,"
        " document_sha256, archive_sha256, manifest, size_bytes, artifact_count,"
        " checkpoint_record_ids, verifier_version)"
        f" SELECT export_id, organization_id, plant_id, ${first}, ${first + 1}, ${first + 2},"
        f" ${first + 3}, ${first + 4}, ${first + 5}, 'verifier-1.0.0' FROM request"
    )
    return sql, [*request.values(), *package]


def monthly_package(
    scope: AccreditationScope,
    at: dt.datetime = BASE_TIME,
    *,
    package_id: uuid.UUID | None = None,
    period: dt.date | None = None,
) -> Statement:
    return insert(
        "monthly_package",
        {
            "package_id": package_id or uuid.uuid4(),
            "organization_id": scope.organization_id,
            "plant_id": scope.plant_id,
            "zone_id": scope.zone_id,
            "period": period or next_month(),
            "state": "sealed",
            "generated_at": at,
            "findings_summary": json.dumps({"received": 0, "classified": 0}),
            "recurrence_summary": "[]",
            "coverage_summary": json.dumps({"observable_percent": 100}),
            "commitments_from_previous_period": "[]",
        },
    )


def audit_sample(
    scope: AccreditationScope, at: dt.datetime = BASE_TIME, *, sample_id: uuid.UUID | None = None
) -> Statement:
    return insert(
        "audit_sample",
        {
            "sample_id": sample_id or uuid.uuid4(),
            "organization_id": scope.organization_id,
            "plant_id": scope.plant_id,
            "zone_id": scope.zone_id,
            "package_id": scope.package_id,
            "population_size": 10,
            "sample_size": 5,
            "seed": secrets.randbits(63),
            "strata": "[]",
            "selected_at": at,
            "reviewer_user_id": scope.user_id,
        },
    )


def audit_sample_item(
    scope: AccreditationScope,
    at: dt.datetime = BASE_TIME,
    *,
    sample_item_id: uuid.UUID | None = None,
) -> Statement:
    return insert(
        "audit_sample_item",
        {
            "sample_item_id": sample_item_id or uuid.uuid4(),
            "organization_id": scope.organization_id,
            "plant_id": scope.plant_id,
            "zone_id": scope.zone_id,
            "sample_id": scope.sample_id,
            "classification_id": uuid.uuid4(),
            "subject_id": uuid.uuid4(),
        },
    )


def commitment(scope: AccreditationScope, at: dt.datetime = BASE_TIME) -> Statement:
    return insert(
        "commitment",
        {
            "commitment_id": uuid.uuid4(),
            "organization_id": scope.organization_id,
            "plant_id": scope.plant_id,
            "zone_id": scope.zone_id,
            "package_id": scope.package_id,
            "text": "Revisar el resguardo de la zona sintética antes del próximo mes.",
            "responsible_user_id": scope.user_id,
            "due_date": at.date() + dt.timedelta(days=30),
            "state": "open",
            "recorded_by": json.dumps({"user_id": str(scope.user_id), "role": "line_manager"}),
            "recorded_at": at,
            "record_id": uuid.uuid4(),
        },
    )


def program_metric_snapshot(
    scope: AccreditationScope, at: dt.datetime = BASE_TIME, *, period: dt.date | None = None
) -> Statement:
    return insert(
        "program_metric_snapshot",
        {
            "snapshot_id": uuid.uuid4(),
            "organization_id": scope.organization_id,
            "plant_id": scope.plant_id,
            "zone_id": scope.zone_id,
            "period": period or next_month(),
            "granularity": "month",
            "values": "[]",
            "materialized_at": at,
            "inputs_digest": _hex64(),
        },
    )


def exposure_indicator(
    scope: AccreditationScope, at: dt.datetime = BASE_TIME, *, period: dt.date | None = None
) -> Statement:
    return insert(
        "exposure_indicator",
        {
            "exposure_id": uuid.uuid4(),
            "organization_id": scope.organization_id,
            "plant_id": scope.plant_id,
            "zone_id": scope.zone_id,
            "period": period or next_month(),
            "status": "not_computable",
            "confirmed_minutes": 0.0,
            "pending_classification_minutes": 0.0,
            "observable_coverage_percent": 0.0,
            "coverage_timeline_ref": json.dumps({"zone_id": str(scope.zone_id)}),
            "hourly_distribution": "[]",
            "not_computable_reason_es": "La zona no declara señal dura.",
            "usage_notice_es": "Hecho medido; insumo interno de priorización, nunca meta.",
            "materialized_at": at,
            "inputs_digest": _hex64(),
        },
    )


def adoption_counter(
    scope: AccreditationScope,
    at: dt.datetime = BASE_TIME,
    *,
    day: dt.date | None = None,
    activity_counts: dict[str, Any] | None = None,
) -> Statement:
    return insert(
        "adoption_counter",
        {
            "organization_id": scope.organization_id,
            "plant_id": scope.plant_id,
            "zone_id": scope.zone_id,
            "day": day or next_day(),
            "activity_counts": json.dumps(
                {"inbox_opened": 2} if activity_counts is None else activity_counts
            ),
            "session_count": 1,
            "last_activity_at": at,
        },
    )


ROW_BUILDERS: Final[dict[str, Builder]] = {
    "export_request": export_request,
    "export_package": export_package,
    "monthly_package": monthly_package,
    "audit_sample": audit_sample,
    "audit_sample_item": audit_sample_item,
    "commitment": commitment,
    "program_metric_snapshot": program_metric_snapshot,
    "exposure_indicator": exposure_indicator,
    "adoption_counter": adoption_counter,
}
"""Una fila nueva por tabla (el paquete de exportación, además, su solicitud)."""

assert tuple(ROW_BUILDERS) == ACCREDITATION_TABLES


def plant_scopes(tenant: Tenant) -> tuple[AccreditationScope, ...]:
    return tuple(
        AccreditationScope(tenant.organization_id, plant.plant_id, plant.zone_id, tenant.user_id)
        for plant in tenant.plants
    )


async def seed_plant(connection: Any, scope: AccreditationScope) -> None:
    """Las filas de las que dependen las demás y una fila de cada tabla (como superusuario)."""
    for sql, args in (
        monthly_package(scope, package_id=scope.package_id),
        audit_sample(scope, sample_id=scope.sample_id),
    ):
        await connection.execute(sql, *args)
    for table in ACCREDITATION_TABLES:
        sql, args = ROW_BUILDERS[table](scope)
        await connection.execute(sql, *args)


@dataclass(frozen=True)
class AccreditationSeed:
    identity: IdentitySeed
    scopes: dict[uuid.UUID, tuple[AccreditationScope, ...]]
    """Plantas sembradas de cada organización cliente."""

    def plant(self, organization_id: uuid.UUID, index: int = 0) -> AccreditationScope:
        return self.scopes[organization_id][index]


async def seed_accreditation(connection: Any, seed: IdentitySeed) -> AccreditationSeed:
    """Filas de cada tabla de ``accreditation`` en cada planta de A y de B (superusuario)."""
    scopes: dict[uuid.UUID, tuple[AccreditationScope, ...]] = {}
    async with connection.transaction():
        for tenant in (seed.a, seed.b):
            scopes[tenant.organization_id] = plant_scopes(tenant)
            for scope in scopes[tenant.organization_id]:
                await seed_plant(connection, scope)
    return AccreditationSeed(seed, scopes)
