"""Filas sintéticas del esquema ``loop`` para las pruebas (TASK-301).

``ROW_BUILDERS`` da, por tabla, la sentencia y los parámetros de una fila nueva y válida de una
planta (``LoopScope``): ``seed_loop`` siembra con ellos al menos una fila de cada una de las 22
tablas (las 19 de ``laz_0029``, TASK-301, más las 3 de ``laz_0037``, TASK-306) en cada planta de
los dos clientes de ``tests/identity_db.py`` (el par de organizaciones de U-02), y las pruebas de
aislamiento los reutilizan para intentar escribir desde un contexto de
proveedor. Las filas que dependen de otra (evidencia y cierre → acción; revisión → declaración;
solicitud → análisis; borrador → solicitud; término → lista) apuntan a las del ``LoopScope``, que
``seed_plant`` crea primero. La lista de vocabulario es una por organización: la siembra la
primera planta.

Solo datos generados (NFR-CTR-43). Las marcas salen de ``BASE_TIME`` o del instante que se pase,
nunca de la hora real.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from tests.identity_db import BASE_TIME, IdentitySeed, Tenant

LOOP_TABLES: Final = (
    "episode",
    "finding_state",
    "classification",
    "review_queue_item",
    "review_resolution",
    "corrective_action",
    "action_evidence_entry",
    "closure_attachment",
    "closure_record",
    "unclosable_declaration",
    "unclosable_review",
    "reopening",
    "root_cause_analysis",
    "assistant_request",
    "assistant_draft",
    "blocked_vocabulary_list",
    "blocked_term",
    "plant_loop_configuration",
    "loop_projection_checkpoint",
    "episode_rebuild_shadow",
    "finding_state_rebuild_shadow",
    "loop_projection_sample_run",
)
"""Las 19 tablas de ``loop`` de ``laz_0029`` (TASK-301) más las tres de ``laz_0037`` (TASK-306):
la sombra de la reconstrucción y la semilla de la muestra diaria. ``tests/integration/
test_laz_loop_schema.py`` sigue comprobando solo las 19 originales contra ``MIGRATION.TABLES``."""

ORGANIZATION_ONLY_TABLES: Final = (
    "assistant_request",
    "assistant_draft",
    "blocked_vocabulary_list",
    "blocked_term",
    "loop_projection_checkpoint",
    "episode_rebuild_shadow",
    "finding_state_rebuild_shadow",
    "loop_projection_sample_run",
)
"""Las de solo organización (A-69): ninguna fila bajo una concesión de proveedor. Las tres de
``laz_0037`` también lo son (control interno de la proyección, no un dato de cliente)."""

PLANTLESS_TABLES: Final = ("assistant_draft", "blocked_vocabulary_list", "blocked_term")
"""Las que no llevan ``plant_id``."""

PLANT_TABLES: Final = tuple(table for table in LOOP_TABLES if table not in PLANTLESS_TABLES)

SINGLE_ROW_TABLES: Final = frozenset(
    {
        "blocked_vocabulary_list",
        "plant_loop_configuration",
        "loop_projection_checkpoint",
        "loop_projection_sample_run",
    }
)
"""Tablas cuya fila nueva choca por clave con la sembrada (una por organización o por planta):
la política decide antes que la clave."""

REASON: Final = "Motivo sintético con la longitud mínima"
DIGEST: Final = "0" * 64


@dataclass(frozen=True)
class LoopScope:
    """Planta de un cliente con las filas de las que dependen las demás."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    node_id: uuid.UUID
    user_id: uuid.UUID
    list_id: uuid.UUID
    """Lista de vocabulario de la organización (una por organización)."""
    action_id: uuid.UUID = dataclasses.field(default_factory=uuid.uuid4)
    declaration_id: uuid.UUID = dataclasses.field(default_factory=uuid.uuid4)
    analysis_id: uuid.UUID = dataclasses.field(default_factory=uuid.uuid4)
    request_id: uuid.UUID = dataclasses.field(default_factory=uuid.uuid4)


Statement = tuple[str, list[Any]]
Builder = Callable[[LoopScope], Statement]


def insert(table: str, values: dict[str, Any]) -> Statement:
    """``INSERT INTO loop.<table>`` con los valores en orden."""
    columns = ", ".join(values)
    placeholders = ", ".join(f"${index}" for index in range(1, len(values) + 1))
    sql = f"INSERT INTO loop.{table} ({columns}) VALUES ({placeholders})"  # noqa: S608
    return sql, list(values.values())


def _json(value: Any) -> str:
    return json.dumps(value)


def _actor(scope: LoopScope) -> str:
    return _json({"user_id": str(scope.user_id), "display_name": "Coordinación sintética"})


def _signer(scope: LoopScope) -> str:
    return _json(
        {
            "user_id": str(scope.user_id),
            "display_name": "Firma sintética",
            "professional_license": "LIC-0001",
        }
    )


def _zone(scope: LoopScope) -> dict[str, Any]:
    return {
        "organization_id": scope.organization_id,
        "plant_id": scope.plant_id,
        "zone_id": scope.zone_id,
    }


def episode(scope: LoopScope, at: dt.datetime = BASE_TIME) -> Statement:
    return insert(
        "episode",
        {
            "episode_id": uuid.uuid4(),
            **_zone(scope),
            "family": "coexistence",
            "tier": "tier_1",
            "standard_id": uuid.uuid4(),
            "standard_version": 1,
            "segment_record_ids": [uuid.uuid4()],
            "segment_count": 1,
            "expected_segment_count": 1,
            "missing_segment_indexes": [],
            "started_at": at,
            "ended_at": at + dt.timedelta(minutes=1),
            "total_episode_duration_ms": 60_000,
            "total_condition_duration_ms": 30_000,
            "max_confidence": 0.9,
            "clip_references": "[]",
            "observability_state_at_start": "observable",
            "continues_open": False,
            "last_segment_received_at": at,
            "last_event_id": uuid.uuid4(),
            "projection_updated_at": at,
        },
    )


def finding_state(
    scope: LoopScope,
    at: dt.datetime = BASE_TIME,
    *,
    state: str = "received",
    due_at: dt.datetime | None = None,
    finding_state_id: uuid.UUID | None = None,
) -> Statement:
    """Una fila de la proyección en ``state``; ``at`` es el instante del hecho (y su cubo)."""
    classified = state not in ("received", "in_review")
    return insert(
        "finding_state",
        {
            "finding_state_id": finding_state_id or uuid.uuid4(),
            **_zone(scope),
            "subject_kind": "finding",
            "subject_id": uuid.uuid4(),
            "anchor_record_id": uuid.uuid4(),
            "family": "coexistence",
            "tier": "tier_1",
            "standard_id": uuid.uuid4(),
            "standard_version": 1,
            "lifecycle_state": state,
            "previous_lifecycle_state": (
                "open" if state == "suspended_by_reclassification" else None
            ),
            "current_classification_outcome": (
                ("false_positive" if state == "discarded" else "confirmed") if classified else None
            ),
            "classification_count": 1 if classified else 0,
            "due_at": due_at,
            "observability_state_at_event": "observable",
            "zone_gate_status_at_event": "approved",
            "single_occupancy_zone": False,
            "occurred_at_exact": at,
            "occurred_at_bucket": at,
            "last_event_id": uuid.uuid4(),
            "last_record_id": uuid.uuid4(),
            "projection_updated_at": at,
            "projection_digest": DIGEST,
        },
    )


def classification(scope: LoopScope, at: dt.datetime = BASE_TIME) -> Statement:
    return insert(
        "classification",
        {
            "classification_id": uuid.uuid4(),
            **_zone(scope),
            "subject_kind": "finding",
            "subject_id": uuid.uuid4(),
            "record_id": uuid.uuid4(),
            "sequence_in_subject": 1,
            "outcome": "confirmed",
            "reason_category": "real_coexistence",
            "reason_text": REASON,
            "is_reclassification": False,
            "reinforced_reason": False,
            "effect_on_loop": "loop_opened",
            "classified_by": _actor(scope),
            "classified_at": at,
            "label_id": uuid.uuid4(),
            "correlation_id": uuid.uuid4(),
        },
    )


def review_queue_item(scope: LoopScope, at: dt.datetime = BASE_TIME) -> Statement:
    return insert(
        "review_queue_item",
        {
            "detection_id": uuid.uuid4(),
            **_zone(scope),
            "node_id": scope.node_id,
            "anchor_record_id": uuid.uuid4(),
            "family": "coexistence",
            "standard_id": uuid.uuid4(),
            "standard_version": 1,
            "max_confidence": 0.5,
            "review_threshold": 0.4,
            "publication_threshold": 0.7,
            "review_reason": "low_confidence",
            "observability_state_at_event": "observable",
            "occurred_at_exact": at,
            "occurred_at_bucket": at,
            "clip_references": "[]",
            "signals_summary": "{}",
            "received_at": at,
            "review_due_at": at + dt.timedelta(days=5),
            "source_event_id": uuid.uuid4(),
        },
    )


def review_resolution(scope: LoopScope, at: dt.datetime = BASE_TIME) -> Statement:
    return insert(
        "review_resolution",
        {
            "resolution_id": uuid.uuid4(),
            **_zone(scope),
            "detection_id": uuid.uuid4(),
            "record_id": uuid.uuid4(),
            "outcome": "review_confirmed",
            "reason_category": "real_coexistence",
            "reason_text": REASON,
            "resolved_by": _actor(scope),
            "resolved_at": at,
            "label_id": uuid.uuid4(),
            "correlation_id": uuid.uuid4(),
        },
    )


def corrective_action(
    scope: LoopScope, at: dt.datetime = BASE_TIME, *, action_id: uuid.UUID | None = None
) -> Statement:
    return insert(
        "corrective_action",
        {
            "action_id": action_id or uuid.uuid4(),
            **_zone(scope),
            "subject_kind": "finding",
            "subject_id": uuid.uuid4(),
            "record_id": uuid.uuid4(),
            "action_type": "engineering",
            "action_description": "Instalar un resguardo fijo en la zona sintética",
            "assignee_user_id": scope.user_id,
            "assigned_by": _actor(scope),
            "assigned_at": at,
            "due_at": at + dt.timedelta(days=30),
        },
    )


def action_evidence_entry(scope: LoopScope, at: dt.datetime = BASE_TIME) -> Statement:
    return insert(
        "action_evidence_entry",
        {
            "evidence_entry_id": uuid.uuid4(),
            **_zone(scope),
            "action_id": scope.action_id,
            "record_id": uuid.uuid4(),
            "description": "Resguardo instalado y verificado en la zona",
            "performed_at": at,
            "performed_by_role": "Supervisión de mantenimiento",
            "attachment_ids": [],
            "added_by": _actor(scope),
            "added_at": at,
        },
    )


def closure_attachment(
    scope: LoopScope, at: dt.datetime = BASE_TIME, *, attachment_id: uuid.UUID | None = None
) -> Statement:
    attachment = attachment_id or uuid.uuid4()
    subject = uuid.uuid4()
    return insert(
        "closure_attachment",
        {
            "attachment_id": attachment,
            **_zone(scope),
            "subject_id": subject,
            "content_type": "image/png",
            "size_bytes": 1024,
            "sha256": secrets.token_hex(32),
            "filename": "resguardo.png",
            "storage_key": (
                f"org/{scope.organization_id}/plant/{scope.plant_id}/zone/{scope.zone_id}"
                f"/closure/{subject}/{attachment}.png"
            ),
            "no_identifiable_person_declared": True,
            "declared_by": _actor(scope),
            "uploaded_at": at,
        },
    )


def closure_record(scope: LoopScope, at: dt.datetime = BASE_TIME) -> Statement:
    return insert(
        "closure_record",
        {
            "closure_id": uuid.uuid4(),
            **_zone(scope),
            "subject_kind": "finding",
            "subject_id": uuid.uuid4(),
            "action_id": scope.action_id,
            "record_id": uuid.uuid4(),
            "closure_description": "Cierre con el resguardo instalado en la zona",
            "evidence_entry_ids": [uuid.uuid4()],
            "attachment_digests": [secrets.token_hex(32)],
            "signer": _signer(scope),
            "signed_at": at,
            "correlation_id": uuid.uuid4(),
        },
    )


def unclosable_declaration(
    scope: LoopScope, at: dt.datetime = BASE_TIME, *, declaration_id: uuid.UUID | None = None
) -> Statement:
    return insert(
        "unclosable_declaration",
        {
            "declaration_id": declaration_id or uuid.uuid4(),
            **_zone(scope),
            "subject_kind": "finding",
            "subject_id": uuid.uuid4(),
            "record_id": uuid.uuid4(),
            "evaluated_alternative": "Encerramiento completo de la máquina sintética",
            "rejection_reason": "No cabe en el espacio disponible de la zona",
            "responsible_user_id": scope.user_id,
            "decision_date": at.date(),
            "compensatory_measure": "Señalización y bloqueo durante el mantenimiento",
            "review_due_at": at + dt.timedelta(days=180),
            "policy_id": uuid.uuid4(),
            "policy_version": "1",
            "policy_document_sha256": DIGEST,
            "signer": _signer(scope),
            "declared_at": at,
        },
    )


def unclosable_review(scope: LoopScope, at: dt.datetime = BASE_TIME) -> Statement:
    return insert(
        "unclosable_review",
        {
            "review_id": uuid.uuid4(),
            **_zone(scope),
            "declaration_id": scope.declaration_id,
            "record_id": uuid.uuid4(),
            "outcome": "renewed",
            "review_note": "La alternativa sigue sin ser viable en la zona",
            "new_review_due_at": at + dt.timedelta(days=365),
            "reviewed_by": _signer(scope),
            "reviewed_at": at,
        },
    )


def reopening(scope: LoopScope, at: dt.datetime = BASE_TIME) -> Statement:
    return insert(
        "reopening",
        {
            "reopening_id": uuid.uuid4(),
            **_zone(scope),
            "subject_kind": "finding",
            "subject_id": uuid.uuid4(),
            "record_id": uuid.uuid4(),
            "from_state": "closed",
            "previous_closure_id": uuid.uuid4(),
            "reason_text": REASON,
            "new_action_id": uuid.uuid4(),
            "reopened_by": _actor(scope),
            "reopened_at": at,
        },
    )


def root_cause_analysis(
    scope: LoopScope, at: dt.datetime = BASE_TIME, *, analysis_id: uuid.UUID | None = None
) -> Statement:
    return insert(
        "root_cause_analysis",
        {
            "analysis_id": analysis_id or uuid.uuid4(),
            **_zone(scope),
            "subject_kind": "finding",
            "subject_id": uuid.uuid4(),
            "zone_context": "{}",
            "why_steps": "[]",
            "cause_effect_branches": "[]",
            "created_at": at,
            "updated_at": at,
        },
    )


def assistant_request(
    scope: LoopScope,
    at: dt.datetime = BASE_TIME,
    *,
    request_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> Statement:
    return insert(
        "assistant_request",
        {
            "request_id": request_id or uuid.uuid4(),
            **_zone(scope),
            "analysis_id": scope.analysis_id,
            "user_id": user_id or scope.user_id,
            "requested_by": _actor(scope),
            "requested_at": at,
            "outbox_event_id": uuid.uuid4(),
            "context_digest": DIGEST,
        },
    )


def assistant_draft(scope: LoopScope, at: dt.datetime = BASE_TIME) -> Statement:
    return insert(
        "assistant_draft",
        {
            "draft_id": uuid.uuid4(),
            "organization_id": scope.organization_id,
            "request_id": scope.request_id,
            "draft_text": "Borrador sintético del análisis",
            "citations": _json([{"kind": "record", "id": str(uuid.uuid4())}]),
            "insufficient_evidence_declared": False,
            "model_name": "modelo-sintetico",
            "model_version": "1",
            "provider": "anthropic",
            "validation_results": "[]",
            "created_at": at,
        },
    )


def blocked_vocabulary_list(
    scope: LoopScope, at: dt.datetime = BASE_TIME, *, list_id: uuid.UUID | None = None
) -> Statement:
    return insert(
        "blocked_vocabulary_list",
        {
            "list_id": list_id or uuid.uuid4(),
            "organization_id": scope.organization_id,
            "version": 1,
            "effective_from": at,
            "created_by": _actor(scope),
            "record_id": uuid.uuid4(),
        },
    )


def blocked_term(scope: LoopScope, at: dt.datetime = BASE_TIME) -> Statement:
    return insert(
        "blocked_term",
        {
            "term_id": uuid.uuid4(),
            "list_id": scope.list_id,
            "organization_id": scope.organization_id,
            "category": "absence_claim",
            "form": "lemma",
            "value": f"termino{secrets.token_hex(4)}",
            "suggestion_es": "Alternativa sintética",
            "origin": "customer_added",
            "added_in_version": 1,
            "added_reason": "Motivo sintético del término",
        },
    )


def plant_loop_configuration(scope: LoopScope, at: dt.datetime = BASE_TIME) -> Statement:
    return insert(
        "plant_loop_configuration",
        {
            "plant_id": scope.plant_id,
            "organization_id": scope.organization_id,
            "business_calendar": _json(
                {"working_weekdays": [1, 2, 3, 4, 5], "non_working_dates": []}
            ),
            "timezone": "America/Bogota",
            "version": 1,
            "changed_at": at,
            "changed_by": _actor(scope),
            "change_reason": "Configuración inicial sintética",
        },
    )


def loop_projection_checkpoint(scope: LoopScope, at: dt.datetime = BASE_TIME) -> Statement:
    return insert(
        "loop_projection_checkpoint",
        {"organization_id": scope.organization_id, "plant_id": scope.plant_id},
    )


def episode_rebuild_shadow(scope: LoopScope, at: dt.datetime = BASE_TIME) -> Statement:
    """Una fila de la tabla sombra (TASK-306, ``laz_0037``): sin valores por defecto en la base
    (la aplicación siempre escribe todas las columnas), así que el constructor las da todas."""
    return insert(
        "episode_rebuild_shadow",
        {
            "episode_id": uuid.uuid4(),
            **_zone(scope),
            "family": "coexistence",
            "tier": "tier_1",
            "standard_id": uuid.uuid4(),
            "standard_version": 1,
            "segment_record_ids": [uuid.uuid4()],
            "segment_count": 1,
            "expected_segment_count": 1,
            "missing_segment_indexes": [],
            "started_at": at,
            "ended_at": at + dt.timedelta(minutes=1),
            "total_episode_duration_ms": 60_000,
            "total_condition_duration_ms": 30_000,
            "max_confidence": 0.9,
            "clip_references": "[]",
            "observability_state_at_start": "observable",
            "continues_open": False,
            "last_segment_received_at": at,
            "last_event_id": uuid.uuid4(),
            "projection_updated_at": at,
        },
    )


def finding_state_rebuild_shadow(scope: LoopScope, at: dt.datetime = BASE_TIME) -> Statement:
    """Una fila de la tabla sombra (TASK-306, ``laz_0037``): todas las columnas, por la misma
    razón que ``episode_rebuild_shadow``."""
    return insert(
        "finding_state_rebuild_shadow",
        {
            "finding_state_id": uuid.uuid4(),
            **_zone(scope),
            "subject_kind": "finding",
            "subject_id": uuid.uuid4(),
            "anchor_record_id": uuid.uuid4(),
            "family": "coexistence",
            "tier": "tier_1",
            "standard_id": uuid.uuid4(),
            "standard_version": 1,
            "lifecycle_state": "received",
            "previous_lifecycle_state": None,
            "current_classification_outcome": None,
            "current_reason_category": None,
            "classification_count": 0,
            "first_classified_at": None,
            "last_classified_at": None,
            "classified_within_deadline": None,
            "current_action_id": None,
            "current_action_type": None,
            "assignee_user_id": None,
            "assignee_inactive": False,
            "due_at": None,
            "overdue_since": None,
            "overdue_days": 0,
            "closed_at": None,
            "closure_record_id": None,
            "unclosable_since": None,
            "unclosable_review_due_at": None,
            "unclosable_pending_review": False,
            "closed_with_later_different_classification": False,
            "reopening_count": 0,
            "has_signed_root_cause": False,
            "observability_state_at_event": "observable",
            "zone_gate_status_at_event": "approved",
            "zone_agreement_revoked": False,
            "zone_agreement_revoked_at": None,
            "single_occupancy_zone": False,
            "aggregation_window_minutes": 60,
            "walk_test_regression_pending": False,
            "walk_test_regression_cause": None,
            "occurred_at_exact": at,
            "occurred_at_bucket": at,
            "age_days": 0,
            "evidence_flagged": False,
            "missing_segments_declared": False,
            "last_event_id": uuid.uuid4(),
            "last_record_id": uuid.uuid4(),
            "projection_updated_at": at,
            "projection_digest": DIGEST,
        },
    )


def loop_projection_sample_run(scope: LoopScope, at: dt.datetime = BASE_TIME) -> Statement:
    return insert(
        "loop_projection_sample_run",
        {
            "organization_id": scope.organization_id,
            "plant_id": scope.plant_id,
            "sample_day": at.date(),
            "seed": secrets.token_hex(32),
            "population": 100,
            "sample_size": 10,
            "started_at": at,
        },
    )


ROW_BUILDERS: Final[dict[str, Builder]] = {
    "episode": episode,
    "finding_state": finding_state,
    "classification": classification,
    "review_queue_item": review_queue_item,
    "review_resolution": review_resolution,
    "corrective_action": corrective_action,
    "action_evidence_entry": action_evidence_entry,
    "closure_attachment": closure_attachment,
    "closure_record": closure_record,
    "unclosable_declaration": unclosable_declaration,
    "unclosable_review": unclosable_review,
    "reopening": reopening,
    "root_cause_analysis": root_cause_analysis,
    "assistant_request": assistant_request,
    "assistant_draft": assistant_draft,
    "blocked_vocabulary_list": blocked_vocabulary_list,
    "blocked_term": blocked_term,
    "plant_loop_configuration": plant_loop_configuration,
    "loop_projection_checkpoint": loop_projection_checkpoint,
    "episode_rebuild_shadow": episode_rebuild_shadow,
    "finding_state_rebuild_shadow": finding_state_rebuild_shadow,
    "loop_projection_sample_run": loop_projection_sample_run,
}
"""Una fila nueva por tabla, que solo escribe en esa tabla. Las de ``SINGLE_ROW_TABLES`` chocan
por clave con la sembrada."""

assert tuple(ROW_BUILDERS) == LOOP_TABLES


def plant_scopes(tenant: Tenant) -> tuple[LoopScope, ...]:
    list_id = uuid.uuid4()
    return tuple(
        LoopScope(
            tenant.organization_id,
            plant.plant_id,
            plant.zone_id,
            plant.node_id,
            tenant.user_id,
            list_id,
        )
        for plant in tenant.plants
    )


async def seed_plant(connection: Any, scope: LoopScope, *, first: bool) -> None:
    """Las filas de las que dependen las demás y una fila de cada tabla (como superusuario).

    ``first``: la primera planta de la organización, que siembra la lista de vocabulario.
    """
    dependencies: list[Statement] = [
        corrective_action(scope, action_id=scope.action_id),
        unclosable_declaration(scope, declaration_id=scope.declaration_id),
        root_cause_analysis(scope, analysis_id=scope.analysis_id),
        assistant_request(scope, request_id=scope.request_id),
    ]
    if first:
        dependencies.append(blocked_vocabulary_list(scope, list_id=scope.list_id))
    for sql, args in dependencies:
        await connection.execute(sql, *args)
    for table in LOOP_TABLES:
        if table == "blocked_vocabulary_list":  # una por organización, ya sembrada
            continue
        sql, args = ROW_BUILDERS[table](scope)
        await connection.execute(sql, *args)


@dataclass(frozen=True)
class LoopSeed:
    identity: IdentitySeed
    scopes: dict[uuid.UUID, tuple[LoopScope, ...]]
    """Plantas sembradas de cada organización cliente."""

    def plant(self, organization_id: uuid.UUID, index: int = 0) -> LoopScope:
        return self.scopes[organization_id][index]


async def seed_loop(connection: Any, seed: IdentitySeed) -> LoopSeed:
    """Filas de cada tabla de ``loop`` en cada planta de A y de B (superusuario)."""
    scopes: dict[uuid.UUID, tuple[LoopScope, ...]] = {}
    async with connection.transaction():
        for tenant in (seed.a, seed.b):
            scopes[tenant.organization_id] = plant_scopes(tenant)
            for index, scope in enumerate(scopes[tenant.organization_id]):
                await seed_plant(connection, scope, first=index == 0)
    return LoopSeed(seed, scopes)
