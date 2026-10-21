"""Recursos U-04 existentes para el catálogo HTTP, sin otra pila ni otra base.

El hallazgo pasa por el escritor y la proyección reales; clasificación, acción y análisis
se crean con los servicios reales. Solo paquetes sellados y dependencias auxiliares se
siembran por SQL, como en las pruebas de lectura mensual. Todos los datos son sintéticos.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from typing import Any

from tests.examples.test_ledger_routes import CLIP
from tests.factories import uuid7
from tests.isolation.route_cases import Ids
from tests.ledger_database import set_organization
from tests.writer_support import unit_context
from vigia_platform.ledger.application.writer import Receipt, RecordScope
from vigia_platform.loop.adapters.http.services import LOOP_STATE_KEY
from vigia_platform.loop.projection.store import LoopProjectionStore
from vigia_platform.shared.context import ActorKind, ActorUnit
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond


def seed_laz_resources(world: Any, ids: Ids) -> Ids:
    """Un grafo independiente por zona; los identificadores no se reciclan entre tipos."""
    clock = world.env.clock
    at = to_millisecond(clock.now())
    ids = replace(ids, period=at.strftime("%Y-%m"))
    stamp = format_timestamp
    camera, clip, subject = uuid7(), uuid7(), uuid7()
    document = {
        "finding_id": str(uuid7()),
        "contract_version": "1.0.0",
        "organization_id": str(ids.organization),
        "plant_id": str(ids.plant),
        "zone_id": str(ids.zone),
        "node_id": str(ids.node),
        "episode": {
            "episode_id": str(subject),
            "segment_index": 0,
            "continues": False,
            "max_segment_ms": 60000,
        },
        "family": "coexistence",
        "standard": {"standard_id": str(uuid7()), "version": 1},
        "tier": "tier_1",
        "node_time": {
            "started_at": stamp(at),
            "ended_at": stamp(at + timedelta(seconds=30)),
            "clock": {"synchronized": True, "offset_ms": 0, "source": "ntp.local"},
        },
        "signals": [
            {
                "signal_id": str(uuid7()),
                "role": "energy",
                "value": "asserted",
                "valid": True,
                "read_at": stamp(at),
            }
        ],
        "observability_state": "observable",
        "cameras": [
            {
                "camera_id": str(camera),
                "originating": True,
                "clips": [
                    {
                        "clip_id": str(clip),
                        "camera_id": str(camera),
                        "media_kind": "video",
                        "content_type": "video/mp4",
                        "sha256": hashlib.sha256(CLIP).hexdigest(),
                        "size_bytes": len(CLIP),
                        "duration_ms": 30000,
                        "segment": "full",
                        "starts_at": stamp(at),
                        "ends_at": stamp(at + timedelta(seconds=30)),
                        "anonymized": True,
                        "storage_key": (
                            f"org/{ids.organization}/plant/{ids.plant}/zone/{ids.zone}"
                            f"/node/{ids.node}/{clip}.mp4"
                        ),
                    }
                ],
            }
        ],
        "max_confidence": 0.9,
        "episode_duration_ms": 30000,
        "condition_duration_ms": 30000,
        "automatic_classification": {
            "trigger": "publication_threshold",
            "corroborating_camera_ids": [],
        },
        "model_version": "modelo-1",
        "software_version": "1.0.0",
        "receipt": {
            "platform_record_id": str(ids.finding),
            "received_at": stamp(at),
            "status": "accepted",
        },
    }
    database = world.authz.sessions.database
    system = unit_context(ids.organization, ActorUnit.U03, kind=ActorKind.SYSTEM)
    store = LoopProjectionStore()

    async def project() -> None:
        async with database.transaction(system) as transaction:
            receipt = await world.writer.write(
                system,
                "finding_received",
                document,
                record_id=ids.finding,
                scope=RecordScope(plant_id=ids.plant, zone_id=ids.zone),
                transaction=transaction,
            )
            assert isinstance(receipt, Receipt), receipt
            item = await store.record_input(transaction, receipt.record_id)
            step = await store.apply_in_transaction(transaction, item)
            assert step.applied and step.transition_id == "T01", step

            detection = copy.deepcopy(document)
            detection.pop("finding_id")
            detection.pop("automatic_classification")
            detection.pop("tier")
            detection["detection_id"] = str(ids.detection)
            detection["episode"]["episode_id"] = str(uuid7())
            detection["review_threshold"] = 0.4
            detection["publication_threshold"] = 0.7
            detection["max_confidence"] = 0.5
            detection["receipt"]["platform_record_id"] = str(detection_record)
            receipt = await world.writer.write(
                system,
                "detection_for_review_received",
                detection,
                record_id=detection_record,
                scope=RecordScope(plant_id=ids.plant, zone_id=ids.zone),
                transaction=transaction,
            )
            assert isinstance(receipt, Receipt), receipt
            step = await store.apply_in_transaction(transaction, receipt)
            assert step.applied, step

    detection_record = uuid7()
    world.run(project())
    world.env.execute(
        "INSERT INTO catalog.gate_state_history (organization_id, plant_id, zone_id, gate,"
        " status, effective_from, decided_by, ledger_record_id, record_id)"
        " VALUES ($1,$2,$3,'usage','approved',$4,$5,$6,$7)",
        ids.organization,
        ids.plant,
        ids.zone,
        at - timedelta(days=1),
        ids.user,
        uuid7(),
        uuid7(),
    )
    cookie = world.authz.open_session(ids.organization, ids.user)
    context = world.run(world.authz.contexts.context_from_session(cookie)).context
    loop = getattr(world.app.state, LOOP_STATE_KEY)
    classified = world.run(
        loop.classification.classify(
            context,
            ids.finding,
            outcome="confirmed",
            reason_category="real_coexistence",
            reason_text="Motivo sintético de la clasificación del hallazgo en la zona",
            reinforced_reason=False,
        )
    )
    action = world.run(
        loop.actions.assign(
            context,
            ids.finding,
            action_type="engineering",
            action_description="Instalar un resguardo fijo en el acceso a la zona",
            assignee_user_id=ids.user,
        )
    )
    world.env.execute(
        "INSERT INTO catalog.declared_standard_version (organization_id, plant_id, zone_id,"
        " standard_id, version, family, title_es, declared_text, declared_by, effective_from,"
        " predicate, catalog_version, reason_es)"
        " VALUES ($1,$2,$3,$4,1,'coexistence','Zona de carga',"
        " 'Ninguna persona dentro del perímetro con la máquina energizada.',"
        " $5::jsonb,$6,'{}',1,'Catálogo sintético')",
        ids.organization,
        ids.plant,
        ids.zone,
        document["standard"]["standard_id"],
        json.dumps(
            {
                "user_id": str(ids.user),
                "display_name": "Coordinación sintética",
                "role": "coordinator_sst",
            }
        ),
        at,
    )
    analysis = world.run(loop.root_cause.start(context, ids.finding))
    ids = replace(ids, action=action.action_id, analysis=analysis.analysis_id)

    def insert(schema: str, table: str, values: dict[str, Any]) -> None:
        columns = ", ".join(values)
        placeholders = ", ".join(f"${n}" for n in range(1, len(values) + 1))

        async def run() -> None:
            admin = world.authz.sessions.admin
            async with admin.transaction():
                await set_organization(admin, ids.organization)
                await admin.execute(
                    f"INSERT INTO {schema}.{table} ({columns}) VALUES ({placeholders})",  # noqa: S608
                    *values.values(),
                )

        world.run(run())

    place = {"organization_id": ids.organization, "plant_id": ids.plant, "zone_id": ids.zone}
    actor = json.dumps(
        {
            "user_id": str(ids.user),
            "display_name": "Coordinación sintética",
            "role_in_use": "coordinator_sst",
        }
    )
    insert(
        "loop",
        "assistant_request",
        {
            **place,
            "request_id": ids.assistant_request,
            "analysis_id": ids.analysis,
            "user_id": ids.user,
            "requested_by": actor,
            "requested_at": at,
            "outbox_event_id": uuid7(),
            "context_digest": "a" * 64,
        },
    )
    insert(
        "loop",
        "review_queue_item",
        {
            **place,
            "detection_id": ids.detection,
            "node_id": ids.node,
            "anchor_record_id": detection_record,
            "family": "coexistence",
            "standard_id": document["standard"]["standard_id"],
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
            "review_due_at": at + timedelta(days=5),
            "source_event_id": uuid7(),
        },
    )
    insert(
        "loop",
        "unclosable_declaration",
        {
            **place,
            "declaration_id": ids.declaration,
            "subject_kind": "finding",
            "subject_id": subject,
            "record_id": uuid7(),
            "evaluated_alternative": "Encerramiento completo de la máquina sintética",
            "rejection_reason": "No cabe en el espacio disponible de la zona",
            "responsible_user_id": ids.user,
            "decision_date": at.date(),
            "compensatory_measure": "Señalización durante el mantenimiento",
            "review_due_at": at + timedelta(days=180),
            "policy_id": uuid7(),
            "policy_version": "1",
            "policy_document_sha256": "a" * 64,
            "signer": json.dumps(
                {
                    "user_id": str(ids.user),
                    "display_name": "Firma sintética",
                    "professional_license": "LIC-0001",
                }
            ),
            "declared_at": at,
        },
    )
    insert(
        "loop",
        "closure_attachment",
        {
            **place,
            "attachment_id": ids.attachment,
            "subject_id": subject,
            "content_type": "image/png",
            "size_bytes": 1,
            "sha256": "a" * 64,
            "filename": "resguardo.png",
            "storage_key": (
                f"org/{ids.organization}/plant/{ids.plant}/zone/{ids.zone}"
                f"/closure/{subject}/{ids.attachment}.png"
            ),
            "no_identifiable_person_declared": True,
            "declared_by": actor,
            "uploaded_at": at,
        },
    )
    insert(
        "accreditation",
        "export_request",
        {
            "export_id": ids.export,
            "organization_id": ids.organization,
            "plant_id": ids.plant,
            "zone_ids": [ids.zone],
            "period_from": at - timedelta(days=30),
            "period_to": at,
            "requested_by": json.dumps({"user_id": str(ids.user), "role": "coordinator_sst"}),
            "user_id": ids.user,
            "requested_at": at,
            "package_form": "without_clips",
            "expires_at": at + timedelta(days=7),
            "correlation_id": uuid7(),
        },
    )
    sample = uuid7()
    insert(
        "accreditation",
        "monthly_package",
        {
            **place,
            "package_id": ids.package,
            "period": at.date().replace(day=1),
            "state": "sealed",
            "generated_at": at,
            "findings_summary": json.dumps(
                dict.fromkeys(
                    (
                        "received",
                        "classified",
                        "confirmed",
                        "discarded",
                        "closed",
                        "overdue",
                        "unclosable",
                        "reopened",
                    ),
                    0,
                )
            ),
            "recurrence_summary": "[]",
            "coverage_summary": "{}",
            "commitments_from_previous_period": "[]",
            "sealed_checkpoint_record_id": uuid7(),
            "record_id": uuid7(),
        },
    )
    insert(
        "accreditation",
        "audit_sample",
        {
            **place,
            "sample_id": sample,
            "package_id": ids.package,
            "population_size": 1,
            "sample_size": 1,
            "seed": 12345,
            "strata": "[]",
            "selected_at": at,
            "reviewer_user_id": ids.user,
        },
    )
    world.env.execute(
        "UPDATE accreditation.monthly_package SET audit_sample_id=$1 WHERE package_id=$2",
        sample,
        ids.package,
    )
    insert(
        "accreditation",
        "audit_sample_item",
        {
            **place,
            "sample_item_id": ids.sample_item,
            "sample_id": sample,
            "classification_id": classified.classification_id,
            "subject_id": subject,
        },
    )
    insert(
        "accreditation",
        "commitment",
        {
            **place,
            "commitment_id": ids.commitment,
            "package_id": ids.package,
            "text": "Revisar el resguardo de la zona antes del próximo mes",
            "responsible_user_id": ids.user,
            "due_date": at.date() + timedelta(days=30),
            "state": "open",
            "recorded_by": actor,
            "recorded_at": at,
            "record_id": uuid7(),
        },
    )
    insert(
        "notifications",
        "notification",
        {
            **place,
            "notification_id": ids.notification,
            "event_name": "finding_received",
            "source_event_id": uuid7(),
            "urgency": "normal",
            "title_es": "Hallazgo nuevo",
            "body_es": "Hay un hallazgo nuevo por clasificar en la zona sintética.",
            "deep_link_ref": json.dumps({"kind": "finding", "id": str(ids.finding)}),
            "recipient_user_id": ids.user,
            "created_at": at,
        },
    )
    return ids
