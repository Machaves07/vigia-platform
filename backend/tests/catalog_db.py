"""Filas sintéticas del esquema ``catalog`` para las pruebas (TASK-202).

``ROW_BUILDERS`` da, por tabla, la sentencia y los parámetros de una fila nueva y válida de una
planta (``PlantScope``): ``seed_catalog`` siembra con ellos una fila de cada una de las 18 tablas
en cada planta de los dos clientes de ``tests/identity_db.py``, y las pruebas de aislamiento los
reutilizan para intentar escribir desde un contexto de proveedor. Las filas que dependen de otra
(estándar → versión de catálogo; confirmación → acuerdo; paso, pase, prueba de oclusión y acta →
sesión) apuntan a las del ``PlantScope``, que ``seed_catalog`` crea primero.

Solo datos generados (NFR-CTR-43). Las marcas salen de ``BASE_TIME``, nunca de la hora real.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from tests.identity_db import BASE_TIME, IdentitySeed, Tenant

CATALOG_TABLES = (
    "zone_catalog_version",
    "declared_standard_version",
    "zone_camera",
    "family_admission",
    "zone_gate_state",
    "gate_state_history",
    "mounting_gate_record",
    "plant_signatory_policy",
    "use_agreement",
    "agreement_confirmation",
    "plant_policy",
    "document_upload_grant",
    "walk_test_session",
    "walk_test_step",
    "walk_test_pass",
    "occlusion_test",
    "commissioning_record",
    "walk_test_regression",
    "exposure_sample",
)
"""Las 18 tablas de ``catalog`` de ``gob_0017`` y ``exposure_sample`` de ``gob_0027``."""

APPEND_ONLY_TABLES = (
    "zone_catalog_version",
    "declared_standard_version",
    "family_admission",
    "gate_state_history",
    "mounting_gate_record",
    "use_agreement",
    "agreement_confirmation",
    "plant_policy",
    "walk_test_step",
    "walk_test_pass",
    "occlusion_test",
    "commissioning_record",
    "exposure_sample",
)
"""Tablas ⛓ de ``catalog``."""

SINGLE_ROW_TABLES = frozenset(
    {"zone_gate_state", "walk_test_regression", "plant_signatory_policy", "exposure_sample"}
)
"""Tablas cuya fila nueva choca por clave con la sembrada (una por zona, por planta o por pase):
la política decide antes que la clave."""

REASON = "Motivo sintético del cambio"


@dataclass(frozen=True)
class PlantScope:
    """Planta de un cliente con las filas de las que dependen las demás."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    node_id: uuid.UUID
    user_id: uuid.UUID
    camera_id: uuid.UUID = dataclasses.field(default_factory=uuid.uuid4)
    session_id: uuid.UUID = dataclasses.field(default_factory=uuid.uuid4)
    agreement_id: uuid.UUID = dataclasses.field(default_factory=uuid.uuid4)
    pass_id: uuid.UUID = dataclasses.field(default_factory=uuid.uuid4)
    """Pase de la sesión con la muestra de exposición sembrada (una por pase, gob_0027)."""


Builder = Callable[[PlantScope], tuple[str, list[Any]]]


def _json(value: Any) -> str:
    return json.dumps(value)


def _zone_catalog_version(scope: PlantScope, version: int = 1) -> tuple[str, list[Any]]:
    return (
        "INSERT INTO catalog.zone_catalog_version (organization_id, plant_id, zone_id,"
        " catalog_version, issued_at, issued_by, role_in_use, reason_es, changed_fields, payload,"
        " envelope, single_occupancy, ledger_record_id)"
        " VALUES ($1, $2, $3, $4, $5, $6, 'administrator', $7, ARRAY['standards'], '{}', '{}',"
        " false, $8)",
        [
            scope.organization_id,
            scope.plant_id,
            scope.zone_id,
            version,
            BASE_TIME,
            scope.user_id,
            REASON,
            uuid.uuid4(),
        ],
    )


def _declared_standard_version(scope: PlantScope) -> tuple[str, list[Any]]:
    return (
        "INSERT INTO catalog.declared_standard_version (organization_id, plant_id, zone_id,"
        " standard_id, version, family, title_es, declared_text, declared_by, effective_from,"
        " predicate, catalog_version, reason_es)"
        " VALUES ($1, $2, $3, $4, 1, 'coexistence', 'Estándar sintético', 'Texto declarado"
        " sintético', $5, $6, '{}', 1, $7)",
        [
            scope.organization_id,
            scope.plant_id,
            scope.zone_id,
            uuid.uuid4(),
            _json({"user_id": str(scope.user_id), "display_name": "Firmante", "role": "admin"}),
            BASE_TIME,
            REASON,
        ],
    )


def _zone_camera(scope: PlantScope, camera_id: uuid.UUID | None = None) -> tuple[str, list[Any]]:
    return (
        "INSERT INTO catalog.zone_camera (organization_id, plant_id, zone_id, camera_id,"
        " role_in_zone, declared_min_fps, stream_reference, updated_at)"
        " VALUES ($1, $2, $3, $4, 'primary', 5, 'cam-1', $5)",
        [
            scope.organization_id,
            scope.plant_id,
            scope.zone_id,
            camera_id or uuid.uuid4(),
            BASE_TIME,
        ],
    )


def _family_admission(scope: PlantScope) -> tuple[str, list[Any]]:
    # Rechazada: se puede repetir; la admitida es única por planta y familia.
    return (
        "INSERT INTO catalog.family_admission (admission_id, organization_id, plant_id, family,"
        " answers, result, failed_criterion, evaluated_by, role_in_use, evaluated_at,"
        " ledger_record_id)"
        ' VALUES ($1, $2, $3, \'dwell\', \'{"standard": true, "remedy": false,'
        " \"subject\": true}', 'rejected', 'remedy', $4, 'administrator', $5, $6)",
        [
            uuid.uuid4(),
            scope.organization_id,
            scope.plant_id,
            scope.user_id,
            BASE_TIME,
            uuid.uuid4(),
        ],
    )


def _zone_gate_state(scope: PlantScope) -> tuple[str, list[Any]]:
    return (
        "INSERT INTO catalog.zone_gate_state (zone_id, organization_id, plant_id, mounting, usage,"
        " resulting_mode, issued_at, envelope, valid_until)"
        ' VALUES ($1, $2, $3, \'{"status": "approved"}\', \'{"status": "pending"}\','
        " 'commissioning', $4::timestamptz, '{}', $4::timestamptz + interval '7 days')",
        [scope.zone_id, scope.organization_id, scope.plant_id, BASE_TIME],
    )


def gate_interval(
    scope: PlantScope,
    effective_from: dt.datetime,
    effective_until: dt.datetime | None = None,
    *,
    zone_id: uuid.UUID | None = None,
    gate: str = "mounting",
    status: str = "approved",
) -> tuple[str, list[Any]]:
    """Intervalo de ``gate_state_history`` (revocado lleva motivo; decidido, ``record_id``)."""
    return (
        "INSERT INTO catalog.gate_state_history (organization_id, plant_id, zone_id, gate, status,"
        " effective_from, effective_until, decided_by, reason_es, ledger_record_id, record_id)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
        [
            scope.organization_id,
            scope.plant_id,
            zone_id or scope.zone_id,
            gate,
            status,
            effective_from,
            effective_until,
            scope.user_id,
            REASON if status == "revoked" else None,
            uuid.uuid4(),
            None if status == "pending" else uuid.uuid4(),
        ],
    )


def _gate_state_history(scope: PlantScope) -> tuple[str, list[Any]]:
    # Un instante propio por fila: dos intervalos no acotados de la misma compuerta chocarían.
    start = BASE_TIME + dt.timedelta(microseconds=secrets.randbelow(10**12))
    return gate_interval(scope, start, start + dt.timedelta(microseconds=1), gate="usage")


def _mounting_gate_record(scope: PlantScope) -> tuple[str, list[Any]]:
    return (
        "INSERT INTO catalog.mounting_gate_record (record_id, organization_id, plant_id, zone_id,"
        " scope_text_es, cameras, blur_verification, signed_by, role_in_use,"
        " plant_policy_loaded_at_signing, ledger_record_id)"
        " VALUES ($1, $2, $3, $4, 'Alcance declarado sintético', $5, '{}', $6,"
        " 'provider_installer', false, $7)",
        [
            uuid.uuid4(),
            scope.organization_id,
            scope.plant_id,
            scope.zone_id,
            _json([{"camera_id": str(scope.camera_id)}]),
            scope.user_id,
            uuid.uuid4(),
        ],
    )


def _plant_signatory_policy(scope: PlantScope) -> tuple[str, list[Any]]:
    return (
        "INSERT INTO catalog.plant_signatory_policy (plant_id, organization_id, required_roles,"
        " minimum, updated_by, updated_at)"
        " VALUES ($1, $2, ARRAY['copasst', 'plant_manager', 'administrator'], 3, $3, $4)",
        [scope.plant_id, scope.organization_id, scope.user_id, BASE_TIME],
    )


def use_agreement(
    scope: PlantScope, agreement_id: uuid.UUID | None = None
) -> tuple[str, list[Any]]:
    """Acuerdo ``pending_signatures`` con tres firmantes esperados."""
    signatories = [{"role": role} for role in ("copasst", "plant_manager", "administrator")]
    return (
        "INSERT INTO catalog.use_agreement (agreement_id, organization_id, plant_id, zone_id,"
        " signatories, created_by, created_at) VALUES ($1, $2, $3, $4, $5, $6, $7)",
        [
            agreement_id or uuid.uuid4(),
            scope.organization_id,
            scope.plant_id,
            scope.zone_id,
            _json(signatories),
            scope.user_id,
            BASE_TIME,
        ],
    )


def _use_agreement(scope: PlantScope) -> tuple[str, list[Any]]:
    return use_agreement(scope)


def _agreement_confirmation(scope: PlantScope) -> tuple[str, list[Any]]:
    # La firma del usuario de la planta en el acuerdo del PlantScope (que siembra sin firmas):
    # solo escribe en agreement_confirmation, así la política que decide es la suya.
    return (
        "INSERT INTO catalog.agreement_confirmation (agreement_id, user_id, organization_id,"
        " plant_id, role_in_use, confirmed_at, origin)"
        " VALUES ($1, $2, $3, $4, 'administrator', $5, 'management')",
        [scope.agreement_id, scope.user_id, scope.organization_id, scope.plant_id, BASE_TIME],
    )


def _agreement_confirmation_with_agreement(scope: PlantScope) -> tuple[str, list[Any]]:
    # Para sembrar: un firmante confirma una vez por acuerdo, así que confirma un acuerdo propio,
    # creado en la misma sentencia.
    agreement_sql, agreement_args = use_agreement(scope)
    return (
        f"WITH agreement AS ({agreement_sql} RETURNING agreement_id)"  # noqa: S608
        " INSERT INTO catalog.agreement_confirmation (agreement_id, user_id, organization_id,"
        " plant_id, role_in_use, confirmed_at, origin)"
        " SELECT agreement_id, $6, $2, $3, 'administrator', $7, 'management' FROM agreement",
        agreement_args,
    )


def _plant_policy(scope: PlantScope) -> tuple[str, list[Any]]:
    return (
        "INSERT INTO catalog.plant_policy (policy_id, organization_id, plant_id, version,"
        " signed_at, signed_by_display_name, legal_opinion_reference, document_ref,"
        " criteria_summary_es, loaded_by, loaded_at, ledger_record_id)"
        " VALUES ($1, $2, $3, $4, $5, 'Firmante sintético', 'REF-SINTETICA', '{}',"
        " 'Resumen sintético de criterios', $6, $5, $7)",
        [
            uuid.uuid4(),
            scope.organization_id,
            scope.plant_id,
            1 + secrets.randbelow(2**30),
            BASE_TIME,
            scope.user_id,
            uuid.uuid4(),
        ],
    )


def document_upload_grant(
    scope: PlantScope, document_id: uuid.UUID | None = None, *, status: str = "issued"
) -> tuple[str, list[Any]]:
    document_id = document_id or uuid.uuid4()
    key = f"org/{scope.organization_id}/plant/{scope.plant_id}/documents/{document_id}.pdf"
    return (
        "INSERT INTO catalog.document_upload_grant (document_id, organization_id, plant_id, kind,"
        " content_type, storage_key, sha256, size_bytes, issued_at, expires_at, status)"
        " VALUES ($1, $2, $3, 'plant_policy', 'application/pdf', $4, $5, 1000, $6::timestamptz,"
        " $6::timestamptz + interval '15 minutes', $7)",
        [
            document_id,
            scope.organization_id,
            scope.plant_id,
            key,
            secrets.token_hex(32),
            BASE_TIME,
            status,
        ],
    )


def _document_upload_grant(scope: PlantScope) -> tuple[str, list[Any]]:
    return document_upload_grant(scope)


def walk_test_session(
    scope: PlantScope, session_id: uuid.UUID | None = None, *, status: str = "in_progress"
) -> tuple[str, list[Any]]:
    closed = status == "closed"
    return (
        "INSERT INTO catalog.walk_test_session (session_id, organization_id, plant_id, zone_id,"
        " node_id, catalog_version, kind, status, passes_per_cell, matrix_rows, started_at,"
        " last_activity_at, closed_at)"
        " VALUES ($1, $2, $3, $4, $5, 1, 'initial', $6, 3, '[]', $7, $7,"
        " CASE WHEN $8::boolean THEN $7::timestamptz END)",
        [
            session_id or uuid.uuid4(),
            scope.organization_id,
            scope.plant_id,
            scope.zone_id,
            scope.node_id,
            status,
            BASE_TIME,
            closed,
        ],
    )


def _walk_test_session(scope: PlantScope) -> tuple[str, list[Any]]:
    # Cerrada: se puede repetir; la abierta es única por zona.
    return walk_test_session(scope, status="closed")


def walk_test_step(scope: PlantScope, step_id: uuid.UUID | None = None) -> tuple[str, list[Any]]:
    return (
        "INSERT INTO catalog.walk_test_step (step_id, organization_id, plant_id, session_id,"
        " step_kind, responsible_user_id, started_at) VALUES ($1, $2, $3, $4, 'framing', $5, $6)",
        [
            step_id or uuid.uuid4(),
            scope.organization_id,
            scope.plant_id,
            scope.session_id,
            scope.user_id,
            BASE_TIME,
        ],
    )


def _walk_test_step(scope: PlantScope) -> tuple[str, list[Any]]:
    return walk_test_step(scope)


def _walk_test_pass(scope: PlantScope, pass_id: uuid.UUID | None = None) -> tuple[str, list[Any]]:
    return (
        "INSERT INTO catalog.walk_test_pass (pass_id, organization_id, plant_id, session_id,"
        " row_id, result, recorded_by, recorded_at)"
        " VALUES ($1, $2, $3, $4, $5, 'detected', $6, $7)",
        [
            pass_id or uuid.uuid4(),
            scope.organization_id,
            scope.plant_id,
            scope.session_id,
            uuid.uuid4(),
            scope.user_id,
            BASE_TIME,
        ],
    )


def occlusion_test(scope: PlantScope, test_id: uuid.UUID | None = None) -> tuple[str, list[Any]]:
    """Prueba de oclusión ``pending`` con ``deadline = ended_at + 5 min``."""
    return (
        "INSERT INTO catalog.occlusion_test (test_id, organization_id, plant_id, session_id,"
        " camera_id, started_at, ended_at, deadline, recorded_by)"
        " VALUES ($1, $2, $3, $4, $5, $6::timestamptz, $6::timestamptz + interval '1 minute',"
        " $6::timestamptz + interval '6 minutes', $7)",
        [
            test_id or uuid.uuid4(),
            scope.organization_id,
            scope.plant_id,
            scope.session_id,
            scope.camera_id,
            BASE_TIME,
            scope.user_id,
        ],
    )


def _occlusion_test(scope: PlantScope) -> tuple[str, list[Any]]:
    return occlusion_test(scope)


_RECORD_COLUMNS = (
    "commissioning_record_id, organization_id, plant_id, zone_id, session_id, catalog_version,"
    " matrix_results, false_negatives_total, false_alarm_rate_observed, false_alarm_threshold,"
    " latency, installer_measurements, cameras_measured, occlusion_summary, total_hours,"
    " steps_summary, signatures, closed_at, ledger_record_id"
)


def _commissioning_record(scope: PlantScope) -> tuple[str, list[Any]]:
    # El acta de la sesión del PlantScope (que siembra sin acta): solo escribe en
    # commissioning_record, así la política que decide es la suya.
    return (
        f"INSERT INTO catalog.commissioning_record ({_RECORD_COLUMNS})"  # noqa: S608
        " VALUES ($1, $2, $3, $4, $5, 1, '[]', 0, 0, 0, '{}', '{}', '[]', '[]', 0, '[]', '[]',"
        " $6, $7)",
        [
            uuid.uuid4(),
            scope.organization_id,
            scope.plant_id,
            scope.zone_id,
            scope.session_id,
            BASE_TIME,
            uuid.uuid4(),
        ],
    )


def _commissioning_record_with_session(scope: PlantScope) -> tuple[str, list[Any]]:
    # Para sembrar: un acta por sesión, así que cierra una sesión propia, creada en la misma
    # sentencia.
    session_id = uuid.uuid4()
    session_sql, session_args = walk_test_session(scope, session_id, status="closed")
    return (
        f"WITH session AS ({session_sql} RETURNING session_id)"  # noqa: S608
        f" INSERT INTO catalog.commissioning_record ({_RECORD_COLUMNS})"
        " SELECT $9, $2, $3, $4, session_id, 1, '[]', 0, 0, 0, '{}', '{}', '[]', '[]', 0, '[]',"
        " '[]', $7, $10 FROM session",
        [*session_args, uuid.uuid4(), uuid.uuid4()],
    )


def _exposure_sample(scope: PlantScope) -> tuple[str, list[Any]]:
    # Del pase sembrado de la sesión del PlantScope: solo escribe en exposure_sample (la segunda
    # del mismo pase choca por clave, después de la política).
    return (
        "INSERT INTO catalog.exposure_sample (sample_id, organization_id, plant_id, session_id,"
        " pass_id, fetched_at, displayed_at, recorded_by, recorded_at)"
        " VALUES ($1, $2, $3, $4, $5, $6, $6::timestamptz + interval '80 milliseconds', $7, $6)",
        [
            uuid.uuid4(),
            scope.organization_id,
            scope.plant_id,
            scope.session_id,
            scope.pass_id,
            BASE_TIME,
            scope.user_id,
        ],
    )


def _walk_test_regression(scope: PlantScope) -> tuple[str, list[Any]]:
    return (
        "INSERT INTO catalog.walk_test_regression (zone_id, organization_id, plant_id, state,"
        " ledger_record_id) VALUES ($1, $2, $3, 'current', $4)",
        [scope.zone_id, scope.organization_id, scope.plant_id, uuid.uuid4()],
    )


ROW_BUILDERS: dict[str, Builder] = {
    "zone_catalog_version": lambda scope: _zone_catalog_version(
        scope, 2 + secrets.randbelow(2**30)
    ),
    "declared_standard_version": _declared_standard_version,
    "zone_camera": _zone_camera,
    "family_admission": _family_admission,
    "zone_gate_state": _zone_gate_state,
    "gate_state_history": _gate_state_history,
    "mounting_gate_record": _mounting_gate_record,
    "plant_signatory_policy": _plant_signatory_policy,
    "use_agreement": _use_agreement,
    "agreement_confirmation": _agreement_confirmation,
    "plant_policy": _plant_policy,
    "document_upload_grant": _document_upload_grant,
    "walk_test_session": _walk_test_session,
    "walk_test_step": _walk_test_step,
    "walk_test_pass": _walk_test_pass,
    "occlusion_test": _occlusion_test,
    "commissioning_record": _commissioning_record,
    "walk_test_regression": _walk_test_regression,
    "exposure_sample": _exposure_sample,
}
"""Una fila nueva por tabla, que solo escribe en esa tabla. Las de una fila por zona o por planta
(``zone_gate_state``, ``walk_test_regression``, ``plant_signatory_policy``) chocan por clave si
ya hay una; la firma y el acta, con la que ``seed_plant`` deja en el acuerdo y la sesión del
``PlantScope``, que siembra sin ellas."""

_SEED_BUILDERS: dict[str, Builder] = {
    **ROW_BUILDERS,
    "agreement_confirmation": _agreement_confirmation_with_agreement,
    "commissioning_record": _commissioning_record_with_session,
}

assert tuple(ROW_BUILDERS) == CATALOG_TABLES


def plant_scopes(tenant: Tenant) -> tuple[PlantScope, ...]:
    return tuple(
        PlantScope(
            tenant.organization_id, plant.plant_id, plant.zone_id, plant.node_id, tenant.user_id
        )
        for plant in tenant.plants
    )


async def seed_plant(connection: Any, scope: PlantScope) -> None:
    """Las filas de las que dependen las demás y una fila de cada tabla (como superusuario)."""
    for sql, args in (
        _zone_catalog_version(scope),
        _zone_camera(scope, scope.camera_id),
        walk_test_session(scope, scope.session_id),
        use_agreement(scope, scope.agreement_id),
        _walk_test_pass(scope, scope.pass_id),
    ):
        await connection.execute(sql, *args)
    for table in CATALOG_TABLES:
        if table == "use_agreement":
            continue
        sql, args = _SEED_BUILDERS[table](scope)
        await connection.execute(sql, *args)


@dataclass(frozen=True)
class CatalogSeed:
    identity: IdentitySeed
    scopes: dict[uuid.UUID, tuple[PlantScope, ...]]
    """Plantas sembradas de cada organización cliente."""

    def plant(self, organization_id: uuid.UUID, index: int = 0) -> PlantScope:
        return self.scopes[organization_id][index]


async def seed_catalog(connection: Any, seed: IdentitySeed) -> CatalogSeed:
    """Una fila de cada tabla de ``catalog`` en cada planta de A y de B (superusuario)."""
    scopes: dict[uuid.UUID, tuple[PlantScope, ...]] = {}
    async with connection.transaction():
        for tenant in (seed.a, seed.b):
            scopes[tenant.organization_id] = plant_scopes(tenant)
            for scope in scopes[tenant.organization_id]:
                await seed_plant(connection, scope)
    return CatalogSeed(seed, scopes)
