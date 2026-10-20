"""SUP-U05-02 y 04: el detalle auditado de un hallazgo y su historial de clasificaciones (TASK-342;
LC-LAZ-06; BR-LAZ-01, 06, 08, 09 y 152; NFR-LAZ-03).

Sobre PostgreSQL 16 real como ``vigia_app`` y la aplicación real (``detail_stack``), con los puertos
reales de U-03 y de cobertura contados por ``DetailPorts`` (``InstrumentedPorts``):

- **El detalle** de un hallazgo ingresado por la ruta del nodo: su registro del expediente (con su
  ``content_hash``, sus señales y sus clips con URL), la cita del estándar con su texto y su
  ``sha256``, la compuerta de uso del instante, los nodos, la cobertura y el episodio; y de una
  detección, su registro de anclaje sin episodio.
- **Lectura auditada** (BR-LAZ-09): una entrada ``ledger_detail_read`` por registro leído, con su
  identificador, el actor y la zona; **a lo sumo tres** llamadas a U-03 (``state_at``,
  ``standard_version`` y ``nodes_by_zone``, una vez cada una; NFR-LAZ-03).
- **Dos versiones del catálogo** (BR-LAZ-152): el detalle cita la versión con la que se evaluó, con
  su texto, y la nota de la posterior con su fecha; nada se reevalúa ni se escribe.
- **El historial** es exactamente la lista de registros ``classification`` del sujeto en orden de
  ``chain_sequence`` (oráculo: ``LectorExpediente.list``), sin los de otro sujeto de la zona; el
  detalle lleva el mismo.
- **Guardas de alcance** (BR-LAZ-01): quien tiene ``findings.read`` en la zona hermana, en otra
  planta o en otra organización, quien no tiene la clave y el instalador bajo concesión reciben
  ``not_found`` igual que con un identificador inexistente, sin leer el expediente ni preguntar a
  ningún puerto; con alcance de la zona o de la planta del hallazgo, 200.
- **Agregación** (PR-LAZ-02 sobre el expediente real): en zona unipersonal las marcas del detalle y
  del historial son el inicio de la ventana y el ``content_hash`` del registro no cambia.

Solo datos generados.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any, Final

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import IngestedSubject, LoopOrganization
from tests.loop_classification_support import REASON, REINFORCED_REASON
from tests.loop_detail_support import STANDARD_TEXT, DetailStack, Who, detail_stack
from vigia_platform.ledger.application.reader import LectorExpediente, LedgerFilters, PageRequest
from vigia_platform.loop.domain.time_aggregation import GRID_ORIGIN
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration

_WINDOW: Final = 60
_STRIP: Final = frozenset({"correlation_id"})


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[DetailStack]:
    with detail_stack(postgres_endpoint, "loop_detail_routes") as built:
        built.declare_standard(built.world.a)
        yield built


@pytest.fixture(autouse=True)
def _healthy(stack: DetailStack) -> Iterator[None]:
    stack.ports.reset()
    stack.evidence_store.down = False
    yield
    stack.ports.reset()


@pytest.fixture(scope="module")
def coordinator(stack: DetailStack) -> Who:
    return stack.member(stack.world.a)


def _ok(response: Any) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _without(response: Any) -> tuple[int, dict[str, Any]]:
    return response.status_code, {k: v for k, v in response.json().items() if k not in _STRIP}


def _actor(stack: DetailStack, who: Who) -> uuid.UUID:
    return stack.context(who).actor.id


def _reads(stack: DetailStack, organization: LoopOrganization, record_id: uuid.UUID) -> list[Any]:
    return [r for r in stack.audit_reads(organization) if r["resource_id"] == record_id]


def _record(stack: DetailStack, subject: IngestedSubject) -> dict[str, Any]:
    return stack.record(subject.record_id)


# --- el detalle ----------------------------------------------------------------------------------


def test_the_detail_shows_the_record_signals_clips_standard_gate_nodes_and_coverage(
    stack: DetailStack, coordinator: Who
) -> None:
    finding = stack.received()
    stack.occupancy(stack.finding_state_id(finding), False)
    organization = finding.organization
    body = _ok(stack.detail(coordinator, stack.finding_state_id(finding)))
    site = organization.site
    document = finding.document
    (record,) = body["records"]
    ledger = _record(stack, finding)
    assert record["record_id"] == str(finding.record_id)
    assert record["content_hash"] == ledger["content_hash"]
    assert record["chain_sequence"] == ledger["chain_sequence"]
    assert record["segment_index"] == 0
    assert [(s["role"], s["value"], s["valid"]) for s in record["signals"]] == [
        (s["role"], s["value"], s["valid"]) for s in document["signals"]
    ]
    clips = [clip for camera in document["cameras"] for clip in camera["clips"]]
    assert [(c["clip_id"], c["sha256"], c["size_bytes"]) for c in record["clips"]] == [
        (c["clip_id"], c["sha256"], c["size_bytes"]) for c in clips
    ]
    assert all(c["read_url"] for c in record["clips"])
    assert body["standard"]["standard_id"] == str(site.standard_id)
    assert body["standard"]["version"] == 1
    assert body["standard"]["declared_text"] == STANDARD_TEXT
    assert (
        body["standard"]["declared_text_sha256"]
        == hashlib.sha256(STANDARD_TEXT.encode()).hexdigest()
    )
    assert body["standard"]["superseded_since"] is None
    assert body["gate_at_event"]["status"] == "approved"
    assert [n["node_id"] for n in body["nodes"]] == [str(site.node_id)]
    assert body["coverage_at_event"] is not None
    assert body["episode"]["episode_id"] == str(finding.subject_id)
    assert body["episode"]["missing_segment_indexes"] == []
    assert body["subject_id"] == str(finding.subject_id)
    assert body["zone_id"] == str(site.zone_id)
    assert body["time_aggregated"] is False
    assert body["notices"] == []
    assert body["classifications"] == body["actions"] == body["analyses"] == []


def test_the_detail_reads_the_ledger_audited_and_calls_u03_at_most_three_times(
    stack: DetailStack, coordinator: Who
) -> None:
    finding = stack.received()
    organization = finding.organization
    before = len(_reads(stack, organization, finding.record_id))
    _ok(stack.detail(coordinator, stack.finding_state_id(finding)))
    reads = _reads(stack, organization, finding.record_id)[before:]
    assert len(reads) == 1
    (read,) = reads
    assert read["operation"] == "ledger_detail_read"
    assert read["outcome"] == "success"
    assert read["actor_id"] == _actor(stack, coordinator)
    assert read["resource_kind"] == "ledger_record"
    assert read["result_count"] == 1
    assert read["scope_zone_id"] == organization.zone_id
    assert stack.ports.u03_calls <= 3
    assert dict(stack.ports.calls) == {
        "state_at": 1,
        "standard_version": 1,
        "nodes_by_zone": 1,
        "estado_en": 1,
    }


def test_the_detail_of_a_detection_reads_its_anchor_record_without_episode(
    stack: DetailStack, coordinator: Who
) -> None:
    detection = stack.in_review()
    body = _ok(stack.detail(coordinator, stack.finding_state_id(detection)))
    assert body["subject_kind"] == "detection"
    assert body["episode"] is None
    assert [r["record_id"] for r in body["records"]] == [str(detection.record_id)]
    assert stack.ports.u03_calls <= 3


def test_the_detail_cites_the_evaluated_version_and_notes_the_later_one_never_reevaluating(
    stack: DetailStack,
) -> None:
    organization = stack.world.organization()
    who = stack.member(organization)
    finding = stack.received(organization)
    finding_state_id = stack.finding_state_id(finding)
    stack.occupancy(finding_state_id, False)
    later = stack.later_catalog(organization)
    row_before = stack.world.finding_state(finding)
    records_before = stack.record_count(organization)
    body = _ok(stack.detail(who, finding_state_id))
    assert body["standard"]["version"] == 1
    assert body["standard"]["declared_text"] == STANDARD_TEXT
    assert body["standard"]["superseded_since"] == format_timestamp(later)
    assert body["standard"]["superseded_in_catalog_version"] == 2
    assert body["notices"] == [
        {"code": "later_standard_version", "since": format_timestamp(later), "cause": None}
    ]
    # Nunca reevalúa: ni la proyección ni el expediente cambian.
    assert stack.world.finding_state(finding) == row_before
    assert stack.record_count(organization) == records_before
    assert stack.ports.u03_calls <= 3


def test_an_unknown_query_parameter_is_invalid(stack: DetailStack, coordinator: Who) -> None:
    finding = stack.received()
    path = f"/loop/findings/{stack.finding_state_id(finding)}"
    for suffix in ("", "/classifications"):
        response = stack.get(coordinator, f"{path}{suffix}?zone_id={uuid.uuid4()}")
        assert response.status_code == 400, response.text
        assert response.json()["code"] == "invalid_request"


# --- el historial --------------------------------------------------------------------------------


def _oracle(stack: DetailStack, who: Who, subject: IngestedSubject) -> list[dict[str, Any]]:
    """Los registros ``classification`` del sujeto por ``LectorExpediente``, en orden de cadena."""
    audit = stack.world.stack.authz.sessions.audit
    reader = LectorExpediente(database=stack.database, audit=audit)
    page = stack.run(
        reader.list(
            stack.context(who),
            LedgerFilters(record_types=("classification",), zone_id=subject.organization.zone_id),
            PageRequest(size=200),
        )
    )
    assert page.next_cursor is None
    records = [r for r in page.items if r.document()["subject_id"] == str(subject.subject_id)]
    return [
        {"record_id": str(r.record_id), **r.document()}
        for r in sorted(records, key=lambda record: record.chain_sequence)
    ]


def test_the_history_is_exactly_the_classification_records_of_the_subject_in_chain_order(
    stack: DetailStack, coordinator: Who
) -> None:
    finding = stack.received()
    other = stack.received()
    stack.occupancy(stack.finding_state_id(finding), False)
    assert stack.classify(coordinator, finding).status_code == 201
    assert stack.classify(coordinator, other).status_code == 201
    assert (
        stack.classify(
            coordinator,
            finding,
            "false_positive",
            "reflection_or_glare",
            REINFORCED_REASON,
            reinforced=True,
        ).status_code
        == 201
    )
    assert (
        stack.classify(
            coordinator, finding, "confirmed", "real_coexistence", REASON, reinforced=True
        ).status_code
        == 201
    )
    oracle = _oracle(stack, coordinator, finding)
    assert len(oracle) == 3
    history = _ok(stack.history(coordinator, stack.finding_state_id(finding)))
    assert history["finding_state_id"] == str(stack.finding_state_id(finding))
    assert history["time_aggregated"] is False
    shown = history["classifications"]
    assert [c["record_id"] for c in shown] == [r["record_id"] for r in oracle]
    assert [
        (
            c["sequence_in_subject"],
            c["outcome"],
            c["reason_category"],
            c["reason_text"],
            c["is_reclassification"],
            c["reinforced_reason"],
            c["effect_on_loop"],
            c["classified_at"],
            c["classified_by"],
        )
        for c in shown
    ] == [
        (
            r["sequence_in_subject"],
            r["outcome"],
            r["reason_category"],
            r["reason_text"],
            r["is_reclassification"],
            r["reinforced_reason"],
            r["effect_on_loop"],
            r["classified_at"],
            r["signer"],
        )
        for r in oracle
    ]
    detail = _ok(stack.detail(coordinator, stack.finding_state_id(finding)))
    assert detail["classifications"] == shown
    assert detail["classification_count"] == 3


def test_the_history_of_an_unclassified_finding_is_empty(
    stack: DetailStack, coordinator: Who
) -> None:
    finding = stack.received()
    history = _ok(stack.history(coordinator, stack.finding_state_id(finding)))
    assert history["classifications"] == []
    assert stack.ports.calls == {}  # el historial no pregunta a U-03


# --- guardas de alcance --------------------------------------------------------------------------


def _who(stack: DetailStack, level: ScopeLevel, scope: uuid.UUID, role: Role) -> Who:
    return stack.member(stack.world.a, role, level, scope)


def _denied(stack: DetailStack, who: Who, finding: IngestedSubject) -> None:
    finding_state_id = stack.finding_state_id(finding)
    organization = finding.organization
    reads = len(_reads(stack, organization, finding.record_id))
    stack.ports.reset()
    for request in (stack.detail, stack.history):
        response = request(who, finding_state_id)
        missing = request(who, uuid.uuid4())
        assert _without(response) == _without(missing)
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"
    assert stack.ports.calls == {}
    assert len(_reads(stack, organization, finding.record_id)) == reads


def test_findings_read_on_the_sibling_zone_of_the_same_plant_does_not_see_the_finding(
    stack: DetailStack,
) -> None:
    finding = stack.received()
    site = finding.organization.site
    _denied(stack, _who(stack, ScopeLevel.ZONE, site.other_zone_id, Role.COORDINATOR_SST), finding)


def test_findings_read_on_another_plant_does_not_see_the_finding(stack: DetailStack) -> None:
    finding = stack.received()
    plant = stack.world.add_plant(finding.organization)
    _denied(stack, _who(stack, ScopeLevel.PLANT, plant, Role.PLANT_MANAGER), finding)


def test_another_organization_without_the_key_or_under_concession_does_not_see_it(
    stack: DetailStack,
) -> None:
    finding = stack.received()
    _denied(stack, stack.member(stack.world.b), finding)
    _denied(stack, stack.member(stack.world.a, Role.LINE_MANAGER), finding)
    _denied(stack, stack.member(stack.world.a, Role.ADMINISTRATOR), finding)
    _denied(stack, stack.installer(stack.world.a), finding)


def test_findings_read_on_the_zone_or_the_plant_of_the_finding_sees_it(
    stack: DetailStack,
) -> None:
    finding = stack.received()
    site = finding.organization.site
    for who in (
        _who(stack, ScopeLevel.ZONE, site.zone_id, Role.COORDINATOR_SST),
        _who(stack, ScopeLevel.PLANT, site.plant_id, Role.PLANT_MANAGER),
    ):
        detail = _ok(stack.detail(who, stack.finding_state_id(finding)))
        assert detail["records"][0]["record_id"] == str(finding.record_id)
        _ok(stack.history(who, stack.finding_state_id(finding)))


# --- agregación sobre el expediente real (PR-LAZ-02) ----------------------------------------------


def _on_grid(stamp: str) -> bool:
    moment = datetime.fromisoformat(stamp)
    return (moment - GRID_ORIGIN) % timedelta(minutes=_WINDOW) == timedelta(0)


def test_pr_laz_02_in_a_single_occupancy_zone_every_mark_is_aggregated_and_the_hash_unchanged(
    stack: DetailStack, coordinator: Who
) -> None:
    finding = stack.received()
    assert stack.classify(coordinator, finding).status_code == 201
    finding_state_id = stack.finding_state_id(finding)
    stack.occupancy(finding_state_id, True, _WINDOW)
    ledger = _record(stack, finding)
    body = _ok(stack.detail(coordinator, finding_state_id))
    history = _ok(stack.history(coordinator, finding_state_id))
    assert body["time_aggregated"] is history["time_aggregated"] is True
    assert body["aggregation_window_minutes"] == history["aggregation_window_minutes"] == _WINDOW
    (record,) = body["records"]
    marks = [
        body["occurred_at"],
        body["gate_at_event"]["effective_from"],
        body["standard"]["effective_from"],
        body["episode"]["started_at"],
        body["episode"]["ended_at"],
        record["received_at"],
        *[s["read_at"] for s in record["signals"]],
        *[m for c in record["clips"] for m in (c["starts_at"], c["ends_at"])],
        *[n["since"] for n in body["nodes"]],
        *[c["classified_at"] for c in body["classifications"]],
        *[c["classified_at"] for c in history["classifications"]],
    ]
    assert marks and all(_on_grid(mark) for mark in marks), marks
    assert record["content_hash"] == ledger["content_hash"]
    assert _record(stack, finding) == ledger  # el expediente no cambia
