"""Compuertas, acta de alcance y revocación contra PostgreSQL 16 real (TASK-211, LC-GOB-03).

Servicios reales de ``catalog.gates`` sobre la base migrada como ``vigia_app`` (``gates_support``):

- **Acta de alcance** (BL §2.2.1 con D-2): en una transacción, ``mounting_gate_record`` y su fila,
  ``gate_state_changed`` con un solo evento, el relevo de intervalos, la proyección con su sobre
  ``SignedEnvelope<GateState>`` (verifica con el verificador de U-01, ``expected_purpose = gate``,
  y dura 7 días) y los documentos a ``used``. Guardas: ``blur_not_verified``,
  ``node_not_assigned``, encuadres que no son las cámaras de la zona (``ScopeRecordInvalid``),
  documentos que no coinciden y textos; ninguna escribe nada ni firma. Sin política de planta el
  acta se aprueba con ``plant_policy_loaded_at_signing = false``.
- **Revocación** (BR-GOB-33, 34): modo resultante correcto, ``state_at`` anterior sigue
  ``approved``, ningún registro anterior cambia; revocar lo no aprobado es ``GateConflict``.
- **Concurrencia**: dos revocaciones o dos actas simultáneas de la misma compuerta se ordenan por
  el candado de la proyección (la segunda espera: se ve en ``pg_locks``, sin topes de pared). Sin
  el candado la perdedora termina en carrera (``GateUnavailable``) en lugar de en el resultado de
  negocio, y las pruebas fallan.
- **Fallo cerrado** (FS-GOB-02): con la firma caída o retenida más allá del tope, cero historia,
  actas, registros y eventos, la proyección intacta y los documentos aún ``issued``.
- **NFR-GOB-10**: cero firmas en N lecturas de ``stored_gate_envelopes``; ``renew_gate_envelope``
  emite otro sobre con el mismo estado, sin historia, registro ni evento.
- **Guardas de alcance**: otra organización, otra planta, zona fuera del alcance o inexistente →
  ``ResourceNotFound``; las sentencias filtran la zona y la organización (también sin RLS).

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from vigia_contracts.models.enumerations import GateStatus, ZoneMode
from vigia_contracts.signing import verify

from tests.gates_support import (
    FRAMING,
    POLL_SECONDS,
    REASON,
    WAIT_SECONDS,
    GatesWorld,
    gates_world,
)
from tests.identity_db import BASE_TIME
from tests.integration.conftest import PostgresEndpoint
from tests.outbox_support import app_database
from tests.writer_support import unit_context
from vigia_platform.catalog.adapters.postgres.gate_repository import PostgresGateRepository
from vigia_platform.catalog.application.admission import CatalogRejected
from vigia_platform.catalog.application.gates import (
    GateConflict,
    GateRequestInvalid,
    GateTransition,
    GateUnavailable,
)
from vigia_platform.catalog.application.scope_record import ScopeRecordFiled
from vigia_platform.catalog.detail_codes import CatalogDetailCode
from vigia_platform.catalog.domain.documents import DocumentRequestInvalid
from vigia_platform.catalog.domain.enums import DocumentKind, GateKind
from vigia_platform.catalog.domain.scope_record import (
    CameraFraming,
    ScopeRecordInvalid,
    ScopeRecordRequest,
)
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.shared.context import ActorUnit, Role, ScopeContext, ScopeLevel

pytestmark = pytest.mark.integration

SEVEN_DAYS = timedelta(days=7)


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[GatesWorld]:
    with gates_world(postgres_endpoint, "catalog_gates") as world:
        yield world


@pytest.fixture
def faults(world: GatesWorld) -> Iterator[GatesWorld]:
    """Para las pruebas que tumban la firma o el almacén: lo deja sano al terminar."""
    try:
        yield world
    finally:
        gate = world.signer.gate
        if gate is not None:
            gate.set()
        world.signer.gate = None
        world.signer.down = False
        world.storage.down = False


class Zone:
    """Una zona equipada (catálogo con cámaras y nodo) y su instalador."""

    def __init__(self, world: GatesWorld, cameras: int = 2, *, node: bool = True) -> None:
        self.site = world.site()
        ((self.plant, self.zone),) = self.site.zones()
        self.cameras = world.equip(self.site, self.plant, self.zone, cameras, node=node)
        self.installer = world.installer(self.site)


def _request(world: GatesWorld, z: Zone, **changes: Any) -> ScopeRecordRequest:
    return world.scope_request(z.installer, z.plant, z.cameras, **changes)


def _approved(world: GatesWorld, z: Zone) -> ScopeRecordFiled:
    return world.file(z.installer, z.zone, _request(world, z))


def _stamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


# --- Acta de alcance: una transacción -------------------------------------------------------------


def test_the_scope_record_writes_record_history_projection_envelope_and_one_event(
    world: GatesWorld,
) -> None:
    z = Zone(world)
    signed = world.document(z.installer, z.plant, DocumentKind.SCOPE_RECORD)
    request = _request(world, z, document_ref=signed)
    calls = world.signer.calls

    filed = world.file(z.installer, z.zone, request)

    assert world.signer.calls == calls + 1  # una sola firma, en la escritura
    record = filed.record
    assert record.plant_policy_loaded_at_signing is False  # BR-GOB-22: no bloquea
    (acta,) = world.acta_rows(z.zone)
    assert acta["record_id"] == record.record_id
    assert acta["role_in_use"] == "provider_installer"
    assert acta["plant_policy_loaded_at_signing"] is False
    installer_id = z.installer.actor.id
    assert acta["signed_by"] == installer_id
    blur = json.loads(acta["blur_verification"])
    assert blur["declared_by"] == str(installer_id)
    assert blur["capture_document_ref"] == request.capture_document_ref
    assert json.loads(acta["document_ref"]) == signed
    assert {c["camera_id"] for c in json.loads(acta["cameras"])} == {str(c) for c in z.cameras}
    records = world.records_of(z.zone)
    assert [r["record_type"] for r in records] == ["mounting_gate_record", "gate_state_changed"]
    mounting_record, changed = records
    assert mounting_record["record_id"] == acta["ledger_record_id"]
    assert mounting_record["source_key"] == str(record.record_id)
    assert mounting_record["actor_concession_id"] is not None
    assert json.loads(changed["content"]) == {
        "zone_id": str(z.zone),
        "gate": "mounting",
        "status": "approved",
        "resulting_mode": "commissioning",
        "record_id": str(record.record_id),
    }
    (event,) = world.events(z.plant, z.zone)
    assert event["event_name"] == "gate_state_changed"
    assert json.loads(event["payload"]) == {
        "zone_id": str(z.zone),
        "gate": "mounting",
        "status": "approved",
        "resulting_mode": "commissioning",
        "record_id": str(record.record_id),
        "reason_es_present": False,
    }
    (interval,) = world.history(z.zone)
    assert (interval["gate"], interval["status"]) == ("mounting", "approved")
    assert interval["effective_until"] is None and interval["reason_es"] is None
    assert interval["record_id"] == record.record_id
    assert interval["ledger_record_id"] == changed["record_id"]
    assert interval["decided_by"] == installer_id
    projection = world.projection(z.zone)
    assert projection is not None
    assert projection["resulting_mode"] == "commissioning"
    assert projection["mounting"] == {
        "status": "approved",
        "decided_at": projection["mounting"]["decided_at"],
        "record_id": str(record.record_id),
        "decided_by": str(installer_id),
    }
    assert _stamp(projection["mounting"]["decided_at"]) == interval["effective_from"]
    assert projection["usage"] == {"status": "pending"}
    assert projection["valid_until"] - projection["issued_at"] == SEVEN_DAYS
    # El sobre verifica con el verificador de U-01 (propósito gate) y dura 7 días.
    envelope = projection["envelope"]
    assert envelope == dict(filed.transition.envelope)
    payload = verify(envelope, world.signer.keyset(), "gate", world.signer.world.clock)
    assert payload["mounting_gate"] == {
        "status": "approved",
        "decided_at": projection["mounting"]["decided_at"],
        "record_id": str(record.record_id),
    }
    assert payload["usage_gate"] == {"status": "pending"}
    assert payload["resulting_mode"] == "commissioning"
    assert payload["zone_id"] == str(z.zone) and payload["plant_id"] == str(z.plant)
    assert _stamp(payload["valid_until"]) - _stamp(payload["issued_at"]) == SEVEN_DAYS
    assert _stamp(payload["issued_at"]) == projection["issued_at"]
    assert str(installer_id) not in str(payload)  # decided_by no viaja al nodo
    with pytest.raises(Exception):  # noqa: B017 - otro propósito no verifica
        verify(envelope, world.signer.keyset(), "catalog", world.signer.world.clock)
    for ref in (request.capture_document_ref, signed):
        assert world.grant_status(ref["document_id"]) == "used"  # type: ignore[index]


def test_with_a_plant_policy_the_record_says_it_was_loaded(world: GatesWorld) -> None:
    z = Zone(world)
    world.execute(
        "INSERT INTO catalog.plant_policy (policy_id, organization_id, plant_id, version,"
        " signed_at, signed_by_display_name, legal_opinion_reference, document_ref,"
        " criteria_summary_es, loaded_by, loaded_at, ledger_record_id)"
        " VALUES ($1, $2, $3, 1, $4, 'Firmante sintético', 'REF-1', '{}', 'Resumen sintético',"
        " $5, $4, $6)",
        uuid.uuid4(),
        z.site.organization_id,
        z.plant,
        BASE_TIME,
        world.authz.operator_id,
        uuid.uuid4(),
    )

    filed = _approved(world, z)

    assert filed.record.plant_policy_loaded_at_signing is True
    assert world.acta_rows(z.zone)[0]["plant_policy_loaded_at_signing"] is True


def test_a_new_record_on_an_approved_mounting_opens_a_contiguous_interval(
    world: GatesWorld,
) -> None:
    z = Zone(world)
    first = _approved(world, z)
    second = _approved(world, z)

    rows = world.history(z.zone)
    assert [r["record_id"] for r in rows] == [first.record.record_id, second.record.record_id]
    assert rows[0]["effective_until"] == rows[1]["effective_from"]  # sin hueco
    assert rows[1]["effective_until"] is None
    assert [r["status"] for r in rows] == ["approved", "approved"]
    projection = world.projection(z.zone)
    assert projection is not None
    assert projection["mounting"]["record_id"] == str(second.record.record_id)
    assert len(world.acta_rows(z.zone)) == 2
    assert len(world.events(z.plant, z.zone)) == 2


def test_texts_at_their_edges_are_accepted_and_stored_in_nfc(world: GatesWorld) -> None:
    z = Zone(world, cameras=1)
    request = _request(
        world,
        z,
        scope_text_es="a" * 4000,
        cameras=(CameraFraming(z.cameras[0], "Café " + "b" * 494, False),),
    )

    filed = world.file(z.installer, z.zone, request)

    (framing,) = filed.record.cameras
    assert framing.framing_description_es == "Café " + "b" * 494  # NFC
    assert len(filed.record.scope_text_es) == 4000


# --- Guardas del acta: ninguna escribe ni firma ---------------------------------------------------


def _rejected_without_writing(world: GatesWorld, z: Zone, request: ScopeRecordRequest) -> Any:
    calls = world.signer.calls
    with pytest.raises(Exception) as raised:
        world.file(z.installer, z.zone, request)
    assert world.written(z.plant, z.zone) == (0, 0, 0, 0, None)
    assert world.signer.calls == calls
    for ref in (request.capture_document_ref, request.document_ref):
        if isinstance(ref, dict) and world.fetch(
            "SELECT 1 FROM catalog.document_upload_grant WHERE document_id = $1",
            uuid.UUID(ref["document_id"]),
        ):
            assert world.grant_status(ref["document_id"]) == "issued"
    return raised.value


@pytest.mark.parametrize(
    "changes",
    [
        {"blur_declared": None},
        {"blur_declared": False},
        {"capture_document_ref": None},
        {"blur_declared": None, "capture_document_ref": None},
    ],
    ids=["sin-declared", "declared-false", "sin-captura", "sin-nada"],
)
def test_without_the_declaration_and_its_capture_the_record_is_blur_not_verified(
    world: GatesWorld, changes: dict[str, Any]
) -> None:
    z = Zone(world)
    error = _rejected_without_writing(world, z, _request(world, z, **changes))
    assert isinstance(error, CatalogRejected)
    assert error.detail_code is CatalogDetailCode.BLUR_NOT_VERIFIED


def test_a_zone_without_an_assigned_node_is_node_not_assigned(world: GatesWorld) -> None:
    z = Zone(world, node=False)
    error = _rejected_without_writing(world, z, _request(world, z))
    assert isinstance(error, CatalogRejected)
    assert error.detail_code is CatalogDetailCode.NODE_NOT_ASSIGNED
    # Con el nodo asignado, la misma acta se aprueba.
    world.assign_node(z.site, z.plant, z.zone)
    assert _approved(world, z).transition.state.mounting.status is GateStatus.APPROVED


@pytest.mark.parametrize(
    "shape", ["falta-una", "sobra-una", "repetida", "ajena", "zona-sin-catalogo"]
)
def test_framings_that_are_not_the_zone_cameras_are_invalid(world: GatesWorld, shape: str) -> None:
    z = Zone(world, cameras=2)
    first, second = z.cameras
    cameras = {
        "falta-una": (first,),
        "sobra-una": (first, second, uuid.uuid4()),
        "repetida": (first, first, second),
        "ajena": (first, uuid.uuid4()),
        "zona-sin-catalogo": (first, second),
    }[shape]
    if shape == "zona-sin-catalogo":
        z = Zone(world, cameras=0)
    request = _request(world, z, cameras=tuple(CameraFraming(c, FRAMING, False) for c in cameras))
    error = _rejected_without_writing(world, z, request)
    assert isinstance(error, ScopeRecordInvalid)


@pytest.mark.parametrize(
    "case", ["captura-de-otro-tipo", "no-subida", "ya-usada", "de-otra-planta", "acta-de-otro-tipo"]
)
def test_documents_that_do_not_match_their_grant_are_invalid(world: GatesWorld, case: str) -> None:
    z = Zone(world)
    changes: dict[str, Any]
    if case == "captura-de-otro-tipo":
        changes = {
            "capture_document_ref": world.document(z.installer, z.plant, DocumentKind.SCOPE_RECORD)
        }
    elif case == "no-subida":
        changes = {
            "capture_document_ref": world.document(
                z.installer, z.plant, DocumentKind.BLUR_CHECK_CAPTURE, uploaded=False
            )
        }
    elif case == "ya-usada":
        used = _approved(world, z)
        before = world.written(z.plant, z.zone)
        with pytest.raises(DocumentRequestInvalid):
            world.file(
                z.installer,
                z.zone,
                _request(
                    world,
                    z,
                    capture_document_ref=used.record.capture_document_ref.to_json(),
                ),
            )
        assert world.written(z.plant, z.zone) == before
        return
    elif case == "de-otra-planta":
        other = world.site()
        (other_plant, _), *_ = other.zones()
        foreign = world.installer(other)
        changes = {
            "capture_document_ref": world.document(
                foreign, other_plant, DocumentKind.BLUR_CHECK_CAPTURE
            )
        }
    else:
        changes = {"document_ref": world.document(z.installer, z.plant, DocumentKind.PLANT_POLICY)}
    error = _rejected_without_writing(world, z, _request(world, z, **changes))
    assert isinstance(error, DocumentRequestInvalid)


@pytest.mark.parametrize(
    "changes",
    [
        {"scope_text_es": "a" * 4001},
        {"scope_text_es": "Alcance <b>con</b> marcado"},
        {"scope_text_es": ""},
        {"framing": "f" * 501},
        {"framing": "Encuadre de la zona donde hubo sabotaje"},
    ],
    ids=["scope-4001", "scope-markup", "scope-empty", "framing-501", "framing-intent"],
)
def test_texts_outside_the_policy_are_free_text_rejected(
    world: GatesWorld, changes: dict[str, Any]
) -> None:
    z = Zone(world, cameras=1)
    framing = changes.pop("framing", FRAMING)
    request = _request(world, z, cameras=(CameraFraming(z.cameras[0], framing, True),), **changes)
    error = _rejected_without_writing(world, z, request)
    assert isinstance(error, CatalogRejected)
    assert error.detail_code is CatalogDetailCode.FREE_TEXT_REJECTED


def test_with_the_document_store_down_nothing_is_written(faults: GatesWorld) -> None:
    world = faults
    z = Zone(world)
    request = _request(world, z)
    world.storage.down = True
    error = _rejected_without_writing(world, z, request)
    assert type(error).__name__ == "StorageUnavailable"


# --- Revocación -----------------------------------------------------------------------------------


def test_revoking_mounting_leaves_no_capture_and_the_past_stays_approved(
    world: GatesWorld,
) -> None:
    z = Zone(world)
    filed = _approved(world, z)
    records_before = [(r["record_id"], r["record_hash"]) for r in world.records_of(z.zone)]
    acta_before = world.acta_rows(z.zone)
    approved_from = world.history(z.zone)[0]["effective_from"]

    transition = world.revoke(z.installer, z.zone, GateKind.MOUNTING)

    assert transition.state.resulting_mode is ZoneMode.NO_CAPTURE
    approved, revoked = world.history(z.zone)
    assert approved["effective_until"] == revoked["effective_from"]
    assert (revoked["status"], revoked["reason_es"]) == ("revoked", REASON)
    assert revoked["record_id"] == filed.record.record_id  # la aprobación que se revocó
    assert revoked["effective_until"] is None
    # BR-GOB-34: lo escrito permanece; ningún registro ni acta anterior cambia.
    assert [(r["record_id"], r["record_hash"]) for r in world.records_of(z.zone)][:2] == (
        records_before
    )
    assert world.acta_rows(z.zone) == acta_before
    changed = world.records_of(z.zone)[-1]
    assert changed["record_type"] == "gate_state_changed"
    assert json.loads(changed["content"])["reason_es"] == REASON
    event = json.loads(world.events(z.plant, z.zone)[-1]["payload"])
    assert event == {
        "zone_id": str(z.zone),
        "gate": "mounting",
        "status": "revoked",
        "resulting_mode": "no_capture",
        "record_id": str(filed.record.record_id),
        "reason_es_present": True,
    }
    assert REASON not in world.events(z.plant, z.zone)[-1]["payload"]  # nunca el motivo
    # state_at responde de la historia: antes de revocar, aprobada.
    reader = world.member(z.site)
    before = world.run(
        world.gates.state_at(
            reader, z.zone, GateKind.MOUNTING, revoked["effective_from"] - timedelta(milliseconds=1)
        )
    )
    assert before is not None and before.status is GateStatus.APPROVED
    assert before.effective_from == approved_from
    after = world.run(
        world.gates.state_at(reader, z.zone, GateKind.MOUNTING, revoked["effective_from"])
    )
    assert after is not None and after.status is GateStatus.REVOKED
    assert (
        world.run(world.gates.state_at(reader, z.zone, GateKind.USAGE, revoked["effective_from"]))
        is None
    )  # pending: ningún intervalo
    projection = world.projection(z.zone)
    assert projection is not None and projection["resulting_mode"] == "no_capture"
    payload = verify(
        projection["envelope"], world.signer.keyset(), "gate", world.signer.world.clock
    )
    assert payload["mounting_gate"]["status"] == "revoked"
    assert payload["resulting_mode"] == "no_capture"


def test_revoking_usage_returns_to_commissioning_and_a_new_agreement_reapproves(
    world: GatesWorld,
) -> None:
    z = Zone(world)
    _approved(world, z)
    agreement = uuid.uuid4()
    approved = world.approve_usage(z.installer, z.zone, agreement)
    assert approved.state.resulting_mode is ZoneMode.PRODUCTIVE
    assert approved.state.usage.record_id == agreement

    revoked = world.revoke(z.installer, z.zone, GateKind.USAGE)

    assert revoked.state.resulting_mode is ZoneMode.COMMISSIONING
    assert revoked.state.mounting.status is GateStatus.APPROVED  # montaje intacto
    usage = [r for r in world.history(z.zone) if r["gate"] == "usage"]
    assert [r["status"] for r in usage] == ["approved", "revoked"]
    assert usage[1]["record_id"] == agreement
    changed = json.loads(world.records_of(z.zone)[-1]["content"])
    assert changed["agreement_id"] == str(agreement) and "record_id" not in changed
    # De revoked se vuelve a approved con un acuerdo nuevo.
    again = world.approve_usage(z.installer, z.zone)
    assert again.state.resulting_mode is ZoneMode.PRODUCTIVE
    assert len([r for r in world.history(z.zone) if r["gate"] == "usage"]) == 3


def test_revoking_mounting_does_not_touch_usage_and_a_new_record_restores_productive(
    world: GatesWorld,
) -> None:
    # Nota de la tarea: BL §3.1 trata las compuertas por separado.
    z = Zone(world)
    _approved(world, z)
    world.approve_usage(z.installer, z.zone)

    revoked = world.revoke(z.installer, z.zone, GateKind.MOUNTING)

    assert revoked.state.resulting_mode is ZoneMode.NO_CAPTURE
    assert revoked.state.usage.status is GateStatus.APPROVED
    assert _approved(world, z).transition.state.resulting_mode is ZoneMode.PRODUCTIVE


@pytest.mark.parametrize("gate", list(GateKind))
def test_revoking_a_gate_that_is_not_approved_is_a_conflict_without_writing(
    world: GatesWorld, gate: GateKind
) -> None:
    z = Zone(world)
    calls = world.signer.calls
    with pytest.raises(GateConflict):  # pending
        world.revoke(z.installer, z.zone, gate)
    assert world.written(z.plant, z.zone) == (0, 0, 0, 0, None)
    assert world.signer.calls == calls
    _approved(world, z)
    if gate is GateKind.USAGE:
        world.approve_usage(z.installer, z.zone)
    world.revoke(z.installer, z.zone, gate)
    before = world.written(z.plant, z.zone)
    with pytest.raises(GateConflict):  # ya revocada
        world.revoke(z.installer, z.zone, gate)
    assert world.written(z.plant, z.zone) == before


def test_reason_edges(world: GatesWorld) -> None:
    z = Zone(world)
    _approved(world, z)
    for reason in ("r" * 9, "r" * 501, "Motivo <i>con</i> marcado", " " * 12):
        with pytest.raises(CatalogRejected) as raised:
            world.revoke(z.installer, z.zone, GateKind.MOUNTING, reason)
        assert raised.value.detail_code is CatalogDetailCode.FREE_TEXT_REJECTED
    assert world.revoke(z.installer, z.zone, GateKind.MOUNTING, "r" * 10).state.mounting.status is (
        GateStatus.REVOKED
    )
    _approved(world, z)
    world.revoke(z.installer, z.zone, GateKind.MOUNTING, "r" * 500)
    assert [r["reason_es"] for r in world.history(z.zone) if r["status"] == "revoked"] == [
        "r" * 10,
        "r" * 500,
    ]


def test_a_reader_cannot_revoke_or_file_and_reads_pending_on_a_fresh_zone(
    world: GatesWorld,
) -> None:
    z = Zone(world)
    reader = world.member(z.site, Role.ADMINISTRATOR)
    state = world.run(world.gates.gate_state(reader, z.zone))
    assert (state.mounting.status, state.usage.status) == (GateStatus.PENDING,) * 2
    assert state.resulting_mode is ZoneMode.NO_CAPTURE and state.issued_at is None
    with pytest.raises(ResourceNotFound):  # commissioning.run es solo del instalador
        world.revoke(reader, z.zone, GateKind.MOUNTING)
    with pytest.raises(ResourceNotFound):
        world.file(reader, z.zone, _request(world, z))
    assert world.written(z.plant, z.zone) == (0, 0, 0, 0, None)


def test_under_concession_the_read_is_audited(world: GatesWorld) -> None:
    z = Zone(world)
    _approved(world, z)
    state = world.run(world.gates.gate_state(z.installer, z.zone))
    assert state.mounting.status is GateStatus.APPROVED
    audited = world.fetch(
        "SELECT scope_plant_id, scope_zone_id, result_count FROM shared.audit_entry"
        " WHERE organization_id = $1 AND actor_concession_id = $2 AND operation = 'catalog_read'",
        z.site.organization_id,
        z.installer.concession_id,
    )
    assert [(r["scope_plant_id"], r["scope_zone_id"], r["result_count"]) for r in audited] == [
        (z.plant, z.zone, 1)
    ]


# --- Concurrencia ---------------------------------------------------------------------------------


async def _advisory_waiters(admin: Any) -> int:
    value: int = await admin.fetchval(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
    )
    return value


def _race(world: GatesWorld, calls: list[Any]) -> list[Any]:
    """Lanza ``calls`` a la vez con la firma retenida; la suelta cuando la segunda espera el
    candado (con el candado) o llega también a la firma (sin él)."""
    gate = threading.Event()
    world.signer.gate = gate
    before = world.signer.calls
    admin = world.authz.sessions.admin

    async def race() -> list[Any]:
        tasks = [asyncio.create_task(call()) for call in calls]
        async with asyncio.timeout(WAIT_SECONDS):
            while not (await _advisory_waiters(admin) >= 1 or world.signer.calls - before >= 2):
                await asyncio.sleep(POLL_SECONDS)
        gate.set()
        return list(await asyncio.gather(*tasks, return_exceptions=True))

    try:
        results: list[Any] = world.run(race())
    finally:
        world.signer.gate = None
    return results


@pytest.mark.parametrize("attempt", [1, 2, 3])
def test_two_simultaneous_revocations_leave_one_transition_and_reject_the_other(
    faults: GatesWorld, attempt: int
) -> None:
    world = faults
    z = Zone(world)
    _approved(world, z)
    world.advance()
    calls = world.signer.calls

    results = _race(
        world,
        [
            lambda: world.gates.revoke(z.installer, z.zone, GateKind.MOUNTING, REASON),
            lambda: world.gates.revoke(z.installer, z.zone, GateKind.MOUNTING, REASON),
        ],
    )

    transitions = [r for r in results if isinstance(r, GateTransition)]
    conflicts = [r for r in results if isinstance(r, GateConflict)]
    assert (len(transitions), len(conflicts)) == (1, 1), results
    assert world.signer.calls == calls + 1  # la rechazada no firma
    assert [r["status"] for r in world.history(z.zone)] == ["approved", "revoked"]
    records = [r["record_type"] for r in world.records_of(z.zone)]
    assert records.count("gate_state_changed") == 2  # aprobación y una sola revocación
    assert len(world.events(z.plant, z.zone)) == 2


@pytest.mark.parametrize("attempt", [1, 2, 3])
def test_two_simultaneous_scope_records_are_serialized_into_contiguous_intervals(
    faults: GatesWorld, attempt: int
) -> None:
    # Un acta nueva sobre un montaje aprobado es válida (intervalo contiguo): con el candado la
    # segunda espera, ve la primera y releva su intervalo. Sin él, las dos parten de «pending»,
    # abren dos intervalos no acotados y la base rechaza una: la prueba falla.
    world = faults
    z = Zone(world)
    first, second = _request(world, z), _request(world, z)
    world.advance()

    results = _race(
        world,
        [
            lambda: world.records.file_scope_record(z.installer, z.zone, first),
            lambda: world.records.file_scope_record(z.installer, z.zone, second),
        ],
    )

    assert all(isinstance(r, ScopeRecordFiled) for r in results), results
    rows = world.history(z.zone)
    assert len(rows) == 2 and rows[0]["effective_until"] == rows[1]["effective_from"]
    assert rows[1]["effective_until"] is None
    assert {r["record_id"] for r in rows} == {r.record.record_id for r in results}
    projection = world.projection(z.zone)
    assert projection is not None
    assert projection["mounting"]["record_id"] == str(rows[1]["record_id"])
    assert len(world.acta_rows(z.zone)) == 2
    assert len(world.events(z.plant, z.zone)) == 2


# --- Fallo cerrado (FS-GOB-02) --------------------------------------------------------------------


def test_with_signing_down_the_scope_record_writes_nothing(faults: GatesWorld) -> None:
    world = faults
    z = Zone(world)
    request = _request(world, z)
    world.signer.down = True

    with pytest.raises(GateUnavailable):
        world.file(z.installer, z.zone, request)

    assert world.written(z.plant, z.zone) == (0, 0, 0, 0, None)
    assert world.grant_status(request.capture_document_ref["document_id"]) == "issued"  # type: ignore[index]
    world.signer.down = False
    assert world.file(z.installer, z.zone, request).transition.state.mounting.status is (
        GateStatus.APPROVED
    )


@pytest.mark.parametrize("operation", ["revocation", "usage-approval", "new-record"])
def test_with_signing_down_a_transition_leaves_the_projection_intact(
    faults: GatesWorld, operation: str
) -> None:
    world = faults
    z = Zone(world)
    _approved(world, z)
    if operation == "revocation":
        world.approve_usage(z.installer, z.zone)
    before = world.written(z.plant, z.zone)
    request = _request(world, z)
    world.signer.down = True

    with pytest.raises(GateUnavailable):
        if operation == "revocation":
            world.revoke(z.installer, z.zone, GateKind.USAGE)
        elif operation == "usage-approval":
            world.approve_usage(z.installer, z.zone)
        else:
            world.file(z.installer, z.zone, request)

    assert world.written(z.plant, z.zone) == before


def test_a_signature_beyond_its_timeout_writes_nothing(faults: GatesWorld) -> None:
    world = faults
    z = Zone(world)
    _approved(world, z)
    before = world.written(z.plant, z.zone)
    gate = threading.Event()
    world.signer.gate = gate
    # El tope es lo que se prueba aquí: 2 s con la firma retenida hasta que la prueba la suelta.
    short = world.build_gates(sign_timeout_seconds=2.0)

    with pytest.raises(GateUnavailable):
        world.revoke(z.installer, z.zone, GateKind.MOUNTING, service=short)

    gate.set()
    world.signer.gate = None
    assert world.written(z.plant, z.zone) == before


# --- Sobre conservado (NFR-GOB-10) y renovación ---------------------------------------------------


def test_reading_stored_envelopes_never_signs(faults: GatesWorld) -> None:
    world = faults
    z1, z2 = Zone(world), Zone(world)
    _approved(world, z1)
    _approved(world, z2)
    fresh = Zone(world)
    calls = world.signer.calls
    world.signer.down = True  # ni siquiera hace falta la firma para leer
    expected = world.projection(z1.zone)
    assert expected is not None

    for _ in range(50):
        stored = world.run(
            world.gates.stored_gate_envelopes(z1.installer, [z1.zone, z2.zone, fresh.zone])
        )
        # z2 es de otra organización (otra concesión) y fresh no tiene transiciones.
        assert stored == {z1.zone: expected["envelope"]}

    assert world.signer.calls == calls
    assert world.run(world.gates.stored_gate_envelopes(z1.installer, [])) == {}


def test_renewing_the_envelope_keeps_the_state_and_writes_no_history(faults: GatesWorld) -> None:
    world = faults
    z = Zone(world)
    _approved(world, z)
    world.approve_usage(z.installer, z.zone)
    before = world.projection(z.zone)
    assert before is not None
    counts = world.written(z.plant, z.zone)[:4]
    world.advance(60)  # menos de 1 h: las concesiones nuevas siguen vigentes en la base

    renewed = world.run(world.gates.renew_gate_envelope(z.installer, z.zone))

    after = world.projection(z.zone)
    assert after is not None and renewed == after["envelope"]
    assert after["issued_at"] > before["issued_at"]
    assert after["valid_until"] - after["issued_at"] == SEVEN_DAYS
    assert (after["mounting"], after["usage"], after["resulting_mode"]) == (
        before["mounting"],
        before["usage"],
        before["resulting_mode"],
    )
    payload = verify(renewed, world.signer.keyset(), "gate", world.signer.world.clock)
    old = before["envelope"]["payload"]
    assert {k: v for k, v in payload.items() if k not in ("issued_at", "valid_until")} == {
        k: v for k, v in old.items() if k not in ("issued_at", "valid_until")
    }
    assert world.written(z.plant, z.zone)[:4] == counts  # sin historia, registro ni evento
    # Con la firma caída, el sobre anterior queda intacto.
    world.signer.down = True
    with pytest.raises(GateUnavailable):
        world.run(world.gates.renew_gate_envelope(z.installer, z.zone))
    assert world.projection(z.zone) == after
    world.signer.down = False
    assert world.run(world.gates.renew_gate_envelope(z.installer, Zone(world).zone)) is None


# --- Consultas de la historia ---------------------------------------------------------------------


def test_history_queries_validate_their_instants(world: GatesWorld) -> None:
    z = Zone(world)
    _approved(world, z)
    reader = world.member(z.site)
    now = world.authz.now()
    for start, end in (
        (now, now),
        (now, now - timedelta(milliseconds=1)),
        (now - timedelta(days=366, milliseconds=1), now),
    ):
        with pytest.raises(GateRequestInvalid):
            world.run(world.gates.gate_history(reader, z.zone, start, end))
    with pytest.raises(GateRequestInvalid):
        world.run(world.gates.state_at(reader, z.zone, GateKind.MOUNTING, now.replace(tzinfo=None)))
    full = world.run(
        world.gates.gate_history(reader, z.zone, now - timedelta(days=365), now + timedelta(days=1))
    )
    assert [i.status for i in full] == [GateStatus.APPROVED]


# --- Guardas de alcance ---------------------------------------------------------------------------


def test_another_organization_another_plant_or_out_of_scope_answer_not_found(
    world: GatesWorld,
) -> None:
    site = world.site(plants=2, zones=2)
    (plant_a, zone_a1), (_, zone_a2), (plant_b, zone_b1), _ = site.zones()
    cameras_a1 = world.equip(site, plant_a, zone_a1)
    cameras_b1 = world.equip(site, plant_b, zone_b1)
    organization = world.installer(site)
    plant_installer = world.installer(site, ScopeLevel.PLANT, plant_a)
    foreign = world.installer(world.site())
    zone_reader = world.member(site, Role.COORDINATOR_SST, ScopeLevel.ZONE, zone_a2)
    world.file(organization, zone_a1, world.scope_request(organization, plant_a, cameras_a1))
    before = {z: world.written(p, z) for p, z in site.zones()}
    now = world.authz.now()

    attempts: list[tuple[ScopeContext, uuid.UUID]] = [
        (foreign, zone_a1),  # otra organización
        (plant_installer, zone_b1),  # otra planta, fuera de la concesión de planta
        (organization, uuid.uuid4()),  # inexistente
    ]
    for context, zone in attempts:
        with pytest.raises(ResourceNotFound):
            world.revoke(context, zone, GateKind.MOUNTING)
        with pytest.raises(ResourceNotFound):
            world.run(world.gates.gate_state(context, zone))
        with pytest.raises(ResourceNotFound):
            world.run(world.gates.state_at(context, zone, GateKind.MOUNTING, now))
        with pytest.raises(ResourceNotFound):
            world.run(world.gates.gate_history(context, zone, now - timedelta(days=1), now))
    with pytest.raises(ResourceNotFound):  # la planta B no está en la concesión de planta
        world.file(
            plant_installer,
            zone_b1,
            ScopeRecordRequest(
                scope_text_es="Alcance sintético de la planta B",
                cameras=tuple(CameraFraming(c, FRAMING, False) for c in cameras_b1),
                blur_declared=True,
                capture_document_ref=world.document(
                    organization, plant_b, DocumentKind.BLUR_CHECK_CAPTURE
                ),
            ),
        )
    with pytest.raises(ResourceNotFound):  # zona fuera del alcance de la sesión
        world.run(world.gates.gate_state(zone_reader, zone_a1))
    assert world.run(world.gates.gate_state(zone_reader, zone_a2)).issued_at is None
    assert {z: world.written(p, z) for p, z in site.zones()} == before
    # El instalador de la planta A sí actúa sobre su planta.
    revoked = world.revoke(plant_installer, zone_a1, GateKind.MOUNTING)
    assert revoked.state.mounting.status is GateStatus.REVOKED


def test_the_statements_filter_the_zone_and_the_organization(world: GatesWorld) -> None:
    # Dos zonas de la misma planta con historia: la de B se escribe antes. Sin el filtro de zona,
    # leer la de A devolvería filas de B.
    site = world.site(plants=1, zones=2)
    (plant, zone_a), (_, zone_b) = site.zones()
    installer = world.installer(site)
    for zone in (zone_b, zone_a):
        cameras = world.equip(site, plant, zone)
        world.file(installer, zone, world.scope_request(installer, plant, cameras))
    world.revoke(installer, zone_b, GateKind.MOUNTING)
    other = world.site()
    repository = PostgresGateRepository(world.database)
    own = unit_context(site.organization_id, ActorUnit.U03)
    foreign = unit_context(other.organization_id, ActorUnit.U03)
    now = world.authz.now()

    async def read(context: ScopeContext, zone: uuid.UUID) -> Any:
        async with world.database.transaction(context) as transaction:
            state = await repository.state(transaction, zone)
        history = await repository.history(
            context, zone, now - timedelta(days=1), now + timedelta(days=1)
        )
        at = await repository.state_at(context, zone, GateKind.MOUNTING, now + timedelta(hours=1))
        envelopes = await repository.envelopes(context, [zone])
        return state, history, at, envelopes

    state, history, at, envelopes = world.run(read(own, zone_a))
    assert state.zone_id == zone_a and state.mounting.status is GateStatus.APPROVED
    assert {i.zone_id for i in history} == {zone_a} and len(history) == 1
    assert [i.status for i in at] == [GateStatus.APPROVED]
    assert set(envelopes) == {zone_a}
    assert envelopes[zone_a]["payload"]["zone_id"] == str(zone_a)
    assert world.run(read(foreign, zone_a)) == (None, (), (), {})


def test_the_organization_filter_holds_even_without_rls(world: GatesWorld) -> None:
    # Como superusuario la RLS no aplica: lo que separa las organizaciones es el filtro de cada
    # sentencia (defensa en profundidad; sin él, esta prueba falla).
    z = Zone(world)
    _approved(world, z)
    other = world.site()
    migrated = world.authz.sessions.migrated
    database = app_database(migrated, url=migrated.as_role().sqlalchemy_url, worker_pool_size=1)
    repository = PostgresGateRepository(database)
    foreign = unit_context(other.organization_id, ActorUnit.U03)
    now = world.authz.now()

    async def read() -> Any:
        async with database.transaction(foreign) as transaction:
            state = await repository.state(transaction, z.zone)
            zone = await repository.zone(transaction, z.zone)
        history = await repository.history(
            foreign, z.zone, now - timedelta(days=1), now + timedelta(days=1)
        )
        at = await repository.state_at(foreign, z.zone, GateKind.MOUNTING, now + timedelta(hours=1))
        envelopes = await repository.envelopes(foreign, [z.zone])
        return state, zone, history, at, envelopes

    try:
        assert world.run(read()) == (None, None, (), (), {})
    finally:
        world.run(database.dispose())
