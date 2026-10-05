"""Ingesta contra PostgreSQL 16 real como ``vigia_app`` (TASK-221; LC-GOB-12; S-PLA-07).

Sobre ``tests/fleet_ingest_support.py`` (escritor, auditoría y bandeja reales, ``NodeApiGate`` real
con
la identidad por certificado contra la base y la aplicación con la cadena fija):

- un hallazgo aceptado: registro ``finding_received`` con el ``Receipt`` dentro
  (``platform_record_id``
  = el del expediente), evidencias, evento con la carga de T-08, concesión ``issued → used`` (y
  ``mark_orphan_clips`` ya no la cuenta); repetido, ``accepted_duplicate`` con el recibo original;
- detección para revisión sin consumidores: se escribe y publica su evento (BR-GOB-95); cierre de
  evento sin apertura: se acepta y queda en ``fleet.observability_orphan_close``; eventos aceptados
  con
  el uso sin aprobar (BR-GOB-92);
- **guardas de alcance** (BR-GOB-88, NFR-GOB-30): zona de otra organización, zona de la organización
  nunca asignada y zona asignada **ahora** pero no en ``node_time.started_at``:
  ``node_zone_mismatch``
  con ``ingest_rejected`` sin contenido en la planta del certificado;
- compuerta, catálogo, antigüedad y clips: sus códigos, su auditoría y ``ingest_rejected`` solo con
  ``zone_gate_not_approved`` y ``node_zone_mismatch`` (BR-GOB-96);
- los bordes de las consultas de PostgreSQL: la ventana ``[t - tol, t + tol]`` frente a un intervalo
  ``[desde, hasta)`` de la compuerta y frente a ``[issued_at, superseded_at)`` del catálogo.

Solo datos generados (NFR-CTR-43). Reloj simulado desde la hora de la base (retro 14).
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Iterator
from typing import Any, Final

import pytest
from vigia_contracts.models import api

from tests.fleet_ingest_support import IngestSite, IngestStack, ingest_stack
from tests.integration.conftest import PostgresEndpoint
from tests.node_api_support import DAY
from tests.writer_support import unit_context, verify_ledger_chains
from vigia_platform.fleet.adapters.postgres.clip_grant_store import PostgresClipGrants
from vigia_platform.fleet.domain.ingest_order import IngestKind
from vigia_platform.shared.context import ActorKind, ActorUnit
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration

MINUTE: Final = dt.timedelta(minutes=1)
TOLERANCE_MS: Final = 120_000
TOLERANCE: Final = dt.timedelta(milliseconds=TOLERANCE_MS)
MS: Final = dt.timedelta(milliseconds=1)
FINDING, DETECTION, EVENT = (
    IngestKind.FINDING,
    IngestKind.DETECTION_FOR_REVIEW,
    IngestKind.OBSERVABILITY_EVENT,
)
INGEST_REJECTED_FIELDS: Final = {
    "node_id",
    "zone_id",
    "record_kind",
    "code",
    "correlation_id",
    "received_at",
}


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[IngestStack]:
    with ingest_stack(postgres_endpoint, "fleet_ingest") as built:
        yield built


@pytest.fixture(autouse=True)
def _release_instances(stack: IngestStack) -> Iterator[None]:
    yield
    stack.run(stack.release())


def _rejection(response: Any) -> Any:
    return api.parse_rejection_response(response.content)


# --- Aceptación, idempotencia, evidencias, evento y concesiones ---------------------------------


def test_an_accepted_finding_is_one_record_with_its_receipt_event_evidence_and_used_grant(
    stack: IngestStack,
) -> None:
    site = stack.site()
    document = stack.finding(site)
    clip = document["cameras"][0]["clips"][0]
    assert stack.grant_status(clip)[0] == "issued"
    stack.tick()
    response = stack.post(site, FINDING, document)
    assert response.status_code == 200, response.text
    receipt = api.parse_receipt(response.content)
    assert receipt.status.value == "accepted"
    (record,) = stack.records("finding_received", site.organization_id)
    assert str(record["record_id"]) == receipt.platform_record_id
    assert record["content"]["receipt"] == receipt.to_json_value()
    assert {key: value for key, value in record["content"].items() if key != "receipt"} == document
    assert record["actor_kind"] == ActorKind.NODE.value
    assert (record["plant_id"], record["scope_zone_id"], record["scope_node_id"]) == (
        site.plant_id,
        site.zone_id,
        site.node_id,
    )
    # La cadena se ordena por recepción; la marca del nodo se conserva aparte (occurred_at).
    assert format_timestamp(record["occurred_at"]) == document["node_time"]["started_at"]
    assert [str(row["clip_id"]) for row in stack.evidence(record["record_id"])] == [clip["clip_id"]]
    (event,) = stack.events("finding_received", site.organization_id)
    assert event["plant_id"] == site.plant_id
    assert event["payload"] == {
        "zone_id": str(site.zone_id),
        "node_id": str(site.node_id),
        "finding_id": document["finding_id"],
        "platform_record_id": receipt.platform_record_id,
        "received_at": receipt.received_at,
        "family": "coexistence",
        "tier": "tier_1",
        "standard": {"standard_id": str(site.standard_id), "version": 1},
    }
    status, used_at = stack.grant_status(clip)
    assert status == "used" and used_at is not None
    # mark_orphan_clips (TASK-222) solo mira las que siguen issued: esta ya no es candidata.
    context = unit_context(site.organization_id, ActorUnit.U03, kind=ActorKind.SYSTEM)

    async def candidates() -> set[uuid.UUID]:
        async with stack.primary.database.transaction(context) as transaction:
            found = await PostgresClipGrants(stack.primary.database).orphan_candidates(
                transaction, issued_before=stack.now() + 2 * DAY, limit=100
            )
        return {grant.clip_id for grant in found}

    assert uuid.UUID(clip["clip_id"]) not in stack.run(candidates())
    assert stack.audit(site.organization_id) == []


def test_a_late_finding_whose_grant_is_already_orphan_is_accepted_and_the_grant_stays(
    stack: IngestStack,
) -> None:
    """La cola del nodo puede vaciarse más de 24 h después de subir el clip: la concesión ya es
    ``orphan`` (TASK-222) y la guarda de la base no admite ``orphan → used``; el registro se acepta
    igual y la marca solo cambia las que siguen ``issued``."""
    site = stack.site()
    document = stack.finding(site)
    clip = document["cameras"][0]["clips"][0]
    for statement in (
        "UPDATE fleet.clip_upload_grant SET status = 'used', used_at = $2 WHERE clip_id = $1",
        "UPDATE fleet.clip_upload_grant SET status = 'orphan', orphaned_at = $2 WHERE clip_id = $1",
    ):
        stack.execute(statement, uuid.UUID(clip["clip_id"]), stack.now())
    response = stack.post(site, FINDING, document)
    assert response.status_code == 200, response.text
    assert stack.grant_status(clip)[0] == "orphan"


def test_a_repeated_finding_is_accepted_duplicate_with_the_original_receipt_and_writes_nothing(
    stack: IngestStack,
) -> None:
    site = stack.site()
    document = stack.finding(site)
    first = api.parse_receipt(stack.post(site, FINDING, document).content)
    stack.tick(30)
    response = stack.post(site, FINDING, document)
    assert response.status_code == 200, response.text
    again = api.parse_receipt(response.content)
    assert again.status.value == "accepted_duplicate"
    assert (again.platform_record_id, again.received_at) == (
        first.platform_record_id,
        first.received_at,
    )
    assert len(stack.records("finding_received", site.organization_id)) == 1
    assert len(stack.events("finding_received", site.organization_id)) == 1


def test_the_same_key_with_other_content_is_idempotency_conflict_and_audited(
    stack: IngestStack,
) -> None:
    site = stack.site()
    document = stack.finding(site)
    assert stack.post(site, FINDING, document).status_code == 200
    changed = {**document, "software_version": "1.4.1"}
    response = stack.post(site, FINDING, changed)
    assert response.status_code == 409, response.text
    assert (_rejection(response).code.value, _rejection(response).field) == (
        "idempotency_conflict",
        "finding_id",
    )
    assert len(stack.records("finding_received", site.organization_id)) == 1
    (entry,) = stack.audit(site.organization_id)
    assert (entry["outcome"], entry["resource_kind"], entry["resource_id"]) == (
        "denied",
        "node",
        site.node_id,
    )
    assert entry["filters"] == {"code": "idempotency_conflict", "record_kind": "finding"}
    assert entry["scope_zone_id"] == site.zone_id
    assert stack.records("ingest_rejected", site.organization_id) == []


def test_a_detection_with_no_consumer_is_written_and_published(stack: IngestStack) -> None:
    site = stack.site()
    document = stack.detection(site)
    response = stack.post(site, DETECTION, document)
    assert response.status_code == 200, response.text
    receipt = api.parse_receipt(response.content)
    (record,) = stack.records("detection_for_review_received", site.organization_id)
    assert str(record["record_id"]) == receipt.platform_record_id
    (event,) = stack.events("detection_for_review_received", site.organization_id)
    assert event["payload"] == {
        "zone_id": str(site.zone_id),
        "node_id": str(site.node_id),
        "detection_id": document["detection_id"],
        "platform_record_id": receipt.platform_record_id,
        "received_at": receipt.received_at,
        "review_reason": "low_confidence",
        "idempotency_key": document["detection_id"],
    }
    # Ningún consumidor registrado: nada que entregar, y aun así está en la bandeja (D-3).
    assert stack.deliveries(site.organization_id) == 0
    assert stack.records("finding_received", site.organization_id) == []


def test_an_orphan_close_is_accepted_and_marked_and_a_paired_one_is_not(
    stack: IngestStack,
) -> None:
    site = stack.site()
    orphan = stack.event(site, opened_event_id=str(stack.uuid7()))
    response = stack.post(site, EVENT, orphan)
    assert response.status_code == 200, response.text
    receipt = api.parse_receipt(response.content)
    (row,) = stack.orphan_closes(site.organization_id)
    assert str(row["event_id"]) == orphan["event_id"]
    assert str(row["opened_event_id"]) == orphan["opened_event_id"]
    assert str(row["ledger_record_id"]) == receipt.platform_record_id
    opened = stack.event(site)
    assert stack.post(site, EVENT, opened).status_code == 200
    closed = stack.event(site, opened_event_id=opened["event_id"])
    assert stack.post(site, EVENT, closed).status_code == 200
    assert len(stack.orphan_closes(site.organization_id)) == 1
    events = stack.events("observability_event_received", site.organization_id)
    assert [event["payload"]["phase"] for event in events] == ["closed", "opened", "closed"]
    assert events[0]["payload"] == {
        "zone_id": str(site.zone_id),
        "node_id": str(site.node_id),
        "event_id_node": orphan["event_id"],
        "platform_record_id": receipt.platform_record_id,
        "subject_kind": "camera",
        "state": "observable",
        "phase": "closed",
    }


def test_observability_events_are_accepted_whatever_the_gate(stack: IngestStack) -> None:
    site = stack.site(usage_approved=False)
    response = stack.post(site, EVENT, stack.event(site))
    assert response.status_code == 200, response.text
    finding = stack.post(site, FINDING, stack.finding(site))
    assert finding.status_code == 403
    assert _rejection(finding).code.value == "zone_gate_not_approved"


# --- Guardas de alcance (BR-GOB-88) ------------------------------------------------------------


def _assert_scope_rejection(
    stack: IngestStack, site: IngestSite, response: Any, zone: uuid.UUID | None
) -> None:
    assert response.status_code == 403, response.text
    assert (_rejection(response).code.value, _rejection(response).field) == (
        "node_zone_mismatch",
        "zone_id",
    )
    assert stack.records("finding_received", site.organization_id) == []
    (written,) = stack.records("ingest_rejected", site.organization_id)
    assert written["plant_id"] == site.plant_id
    assert set(written["content"]) <= INGEST_REJECTED_FIELDS
    assert written["content"]["code"] == "node_zone_mismatch"
    assert written["content"]["record_kind"] == "finding"
    assert written["content"]["node_id"] == str(site.node_id)
    assert written["content"].get("zone_id") == (None if zone is None else str(zone))
    assert written["content"]["correlation_id"] == str(written["correlation_id"])
    (entry,) = stack.audit(site.organization_id)
    assert entry["filters"] == {"code": "node_zone_mismatch", "record_kind": "finding"}
    assert entry["scope_zone_id"] == zone


def test_a_zone_of_another_organization_is_node_zone_mismatch_without_its_zone(
    stack: IngestStack,
) -> None:
    site = stack.site()
    other = stack.site()
    document = stack.finding(site, zone=other.zone_id, grant=False)
    response = stack.post(site, FINDING, document)
    _assert_scope_rejection(stack, site, response, None)
    # Nada llega a la otra organización.
    assert stack.records("ingest_rejected", other.organization_id) == []
    assert stack.audit(other.organization_id) == []


def test_a_zone_of_the_organization_never_assigned_is_node_zone_mismatch(
    stack: IngestStack,
) -> None:
    site = stack.site()
    document = stack.finding(site, zone=site.other_zone_id, grant=False)
    _assert_scope_rejection(stack, site, stack.post(site, FINDING, document), site.other_zone_id)


def test_a_zone_assigned_now_but_not_at_the_fact_instant_is_node_zone_mismatch(
    stack: IngestStack,
) -> None:
    # Asignada hace 5 minutos; el hecho, hace 10: el certificado la cubre hoy, el hecho no.
    site = stack.site(assigned_since=5 * MINUTE)
    document = stack.finding(site, stack.now() - 10 * MINUTE)
    _assert_scope_rejection(stack, site, stack.post(site, FINDING, document), site.zone_id)
    # Un minuto después de asignarla, sí.
    later = stack.finding(site, stack.now() - 4 * MINUTE)
    assert stack.post(site, FINDING, later).status_code == 200


def test_the_scope_check_wins_over_a_schema_violation(stack: IngestStack) -> None:
    site = stack.site()
    document = {**stack.finding(site, zone=site.other_zone_id, grant=False), "campo": 1}
    response = stack.post(site, FINDING, document)
    assert response.status_code == 403
    assert _rejection(response).code.value == "node_zone_mismatch"


# --- Compuerta, catálogo, antigüedad y clips --------------------------------------------------


def test_a_finding_without_approved_use_is_zone_gate_not_approved_with_ingest_rejected(
    stack: IngestStack,
) -> None:
    site = stack.site(usage_approved=False)
    for kind, document in ((FINDING, stack.finding(site)), (DETECTION, stack.detection(site))):
        response = stack.post(site, kind, document)
        assert response.status_code == 403, response.text
        assert (_rejection(response).code.value, _rejection(response).field) == (
            "zone_gate_not_approved",
            "zone_id",
        )
    rejected = stack.records("ingest_rejected", site.organization_id)
    assert [row["content"]["record_kind"] for row in rejected] == [
        "finding",
        "detection_for_review",
    ]
    for row in rejected:
        assert set(row["content"]) == INGEST_REJECTED_FIELDS
        assert row["content"]["zone_id"] == str(site.zone_id)
        assert row["content"]["code"] == "zone_gate_not_approved"
    assert len(stack.audit(site.organization_id)) == 2
    assert stack.records("finding_received", site.organization_id) == []
    assert stack.events("finding_received", site.organization_id) == []


def test_a_standard_not_in_force_is_schema_invalid_on_standard_and_audited(
    stack: IngestStack,
) -> None:
    site = stack.site()
    response = stack.post(site, FINDING, stack.finding(site, standard_version=2))
    assert response.status_code == 422, response.text
    assert (_rejection(response).code.value, _rejection(response).field) == (
        "schema_invalid",
        "standard",
    )
    (entry,) = stack.audit(site.organization_id)
    assert entry["filters"] == {"code": "schema_invalid", "record_kind": "finding"}
    assert stack.records("ingest_rejected", site.organization_id) == []


def test_a_record_older_than_the_retention_is_timestamp_out_of_window(stack: IngestStack) -> None:
    site = stack.site(retention_days=1, assigned_since=10 * DAY)
    old = stack.finding(site, stack.now() - 2 * DAY)
    response = stack.post(site, FINDING, old)
    assert response.status_code == 422, response.text
    rejection = _rejection(response)
    assert (rejection.code.value, rejection.retryable) == ("timestamp_out_of_window", False)
    event = stack.post(site, EVENT, stack.event(site, stack.now() - 2 * DAY))
    assert _rejection(event).code.value == "timestamp_out_of_window"
    recent = stack.finding(site, stack.now() - 23 * dt.timedelta(hours=1))
    assert stack.post(site, FINDING, recent).status_code == 200
    assert [entry["filters"]["code"] for entry in stack.audit(site.organization_id)] == [
        "timestamp_out_of_window",
        "timestamp_out_of_window",
    ]


def test_a_missing_clip_is_clip_missing_nothing_written_and_the_grant_stays_issued(
    stack: IngestStack,
) -> None:
    site = stack.site()
    document = stack.finding(site, store=False)
    clip = document["cameras"][0]["clips"][0]
    response = stack.post(site, FINDING, document)
    assert response.status_code == 422, response.text
    assert _rejection(response).code.value == "clip_missing"
    assert stack.grant_status(clip)[0] == "issued"
    assert stack.records("finding_received", site.organization_id) == []
    (entry,) = stack.audit(site.organization_id)
    assert entry["filters"] == {"code": "clip_missing", "record_kind": "finding"}


def test_every_permanent_rejection_is_audited_and_the_chain_stays_whole(
    stack: IngestStack,
) -> None:
    site = stack.site(usage_approved=False)
    bad_version = stack.finding(site)
    headers = {**site.headers(bad_version, FINDING), "X-Vigia-Contract-Version": "99.0.0"}
    response = stack.run(
        stack.primary.client.post(
            "/api/nodes/findings", content=json.dumps(bad_version), headers=headers
        )
    )
    assert _rejection(response).code.value == "contract_version_unsupported"
    stack.post(site, FINDING, stack.finding(site))  # compuerta
    codes = [entry["filters"]["code"] for entry in stack.audit(site.organization_id)]
    assert codes == ["contract_version_unsupported", "zone_gate_not_approved"]
    stack.run(verify_ledger_chains(stack.authz.sessions.migrated, site.organization_id))


# --- Bordes de las consultas -------------------------------------------------------------------


def test_the_gate_window_edges_against_postgresql(stack: IngestStack) -> None:
    """``[t - tol, t + tol]`` frente al intervalo aprobado ``[desde, hasta)``."""
    site = stack.site(usage_approved=False)
    approved_from = stack.now() - 30 * MINUTE
    approved_until = stack.now() - 20 * MINUTE
    stack.usage(site, approved=True, at=approved_from)
    stack.usage(site, approved=False, at=approved_until)

    def decide(started: dt.datetime) -> int:
        document = stack.finding(site, started, offset_ms=TOLERANCE_MS)
        return stack.post(site, FINDING, document).status_code

    assert decide(approved_from - TOLERANCE) == 200
    assert decide(approved_from - TOLERANCE - MS) == 403
    assert decide(approved_until + TOLERANCE - MS) == 200
    assert decide(approved_until + TOLERANCE) == 403


def test_the_catalog_window_edges_against_postgresql(stack: IngestStack) -> None:
    """``[started - tol, ended + tol]`` frente a ``[issued_at, superseded_at)`` de cada versión."""
    site = stack.site()
    second = stack.now() - 20 * MINUTE
    stack.publish_catalog(site, second, version=2)
    duration = dt.timedelta(seconds=30)

    def decide(started: dt.datetime, version: int) -> tuple[int, str | None]:
        document = stack.finding(
            site, started, duration=duration, offset_ms=TOLERANCE_MS, standard_version=version
        )
        response = stack.post(site, FINDING, document)
        return response.status_code, (
            None if response.status_code == 200 else _rejection(response).field
        )

    # La versión 1 rige hasta ``second`` (excluido): su último instante es second - 1 ms.
    assert decide(second + TOLERANCE - MS, 1) == (200, None)
    assert decide(second + TOLERANCE, 1) == (422, "standard")
    # La versión 2 rige desde ``second``: el final de la ventana tiene que alcanzarlo.
    assert decide(second - duration - TOLERANCE, 2) == (200, None)
    assert decide(second - duration - TOLERANCE - MS, 2) == (422, "standard")
