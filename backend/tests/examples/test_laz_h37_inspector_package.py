"""H-37 · el día del inspector, minutos en vez de semanas (TASK-318; RF-PLA-06; BR-LAZ-88 a 99).

Historia: la gerente de planta exporta el expediente de una zona y un mes para el inspector. El
mundo es pequeño y hecho a mano, con todo lo que el inspector debe poder comprobar:

- un hallazgo **cerrado** con su acción, su entrada de evidencia con un **adjunto** declarado sin
  personas identificables y su cierre firmado; una detección **descartada** en revisión; un
  hallazgo de la **otra organización** en el mismo instante;
- la compuerta de uso **revocada** a mitad del mes con su motivo, un acuerdo **sustituido** por
  otro, un tramo con **acta no vigente**, y la zona de ocupación unipersonal;
- dos puntos de control fijados y las claves públicas vigente y retirada.

El paquete ``with_clips`` lleva, leído de vuelta de sus volúmenes ZIP: los hallazgos estructurados
(todos los estados del periodo), los clips con sus bytes (cuya SHA-256 es la del expediente), el
adjunto con su declaración, la línea de tiempo de cobertura, la auditoría, los puntos de control
y las claves; y el manifiesto declara la organización, la planta y el periodo, el hash de **cada**
artefacto (que coincide con sus bytes), los avisos fijos, la revocación, la regresión, el acuerdo
sustituido, el documento portátil (presente desde TASK-319; aquí con el generador falso y rápido
de ``tests/export_contents_support.py``, no con WeasyPrint) y el verificador ``vigia_verify.py``
con su versión (TASK-346). Ningún dato de la otra organización. La forma ``without_clips`` lleva lo
mismo salvo los bytes de los clips, que se listan con su hash, su duración y su zona bajo el
aviso fijo.

Solo datos generados.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest
from vigia_contracts.models.enumerations import GateStatus

from tests.export_contents_support import Composed, compose_world
from tests.properties.laz.strategies.export import ExportWorld, _Builder, world_context
from tests.properties.laz.strategies.projection import _Ids, stamp
from vigia_platform.accreditation.export.composer_port import ExportJob
from vigia_platform.accreditation.export.contents import EMPTY_SHA256
from vigia_platform.accreditation.manifest.model import (
    AGGREGATION_NOTICE_ES,
    ANONYMIZATION_STATEMENT_ES,
    COVERAGE_NOTICE_ES,
    REGRESSION_NOTICE_ES,
    REVOCATION_NOTICE_ES,
    WITH_CLIPS_NOTICE_ES,
    WITHOUT_CLIPS_NOTICE_ES,
)
from vigia_platform.accreditation.verifier_bundle import (
    VERIFIER_PATH,
    VERIFIER_VERSION,
    load_verifier_bundle,
)
from vigia_platform.fleet.ports import ZoneAssignmentPeriod
from vigia_platform.ledger.application.evidence_read import MarkerResult, MarkerVerification
from vigia_platform.ledger.chain.checkpoints import (
    ChainKind,
    CheckpointChain,
    CheckpointContent,
    CheckpointPublicKey,
    StoredCheckpoint,
)
from vigia_platform.ledger.domain.coverage import (
    AssignmentInput,
    CommunicationInput,
    CommunicationState,
    CoverageInputs,
    CoverageState,
    EventPhase,
    GateInput,
    ObservabilityInput,
    Subject,
    ZoneMode,
)
from vigia_platform.loop.adapters.attachments.rules import attachment_storage_key
from vigia_platform.loop.domain.enums import AttachmentContentType, PackageForm
from vigia_platform.loop.projection.fold import ProjectionInput
from vigia_platform.shared.signing.keys import KeyStatus

pytestmark = pytest.mark.laz_examples

ids = _Ids(uuid.UUID(int=0x37))
ORG_A: Final = ids.next()
ORG_B: Final = ids.next()
PLANT_A: Final = ids.next()
PLANT_B: Final = ids.next()
ZONE_A: Final = ids.next()
ZONE_B: Final = ids.next()
EPISODE: Final = ids.next()
DETECTION: Final = ids.next()
FOREIGN: Final = ids.next()
ACTION: Final = ids.next()
ATTACHMENT: Final = ids.next()
MONTH_START: Final = datetime(2026, 1, 1, 5, 0, tzinfo=UTC)  # 00:00 en Bogotá
MONTH_END: Final = datetime(2026, 2, 1, 5, 0, tzinfo=UTC)
ATTACHMENT_BYTES: Final = b"%PDF-1.7\n% adjunto sintetico\n" + b"0" * 200


def _observed(zone: uuid.UUID, plant: uuid.UUID, at: datetime) -> dict[str, Any]:
    return {
        "plant_id": str(plant),
        "zone_id": str(zone),
        "family": "coexistence",
        "standard": {"standard_id": str(uuid.UUID(int=7)), "version": 2},
        "node_time": {"started_at": stamp(at), "ended_at": stamp(at + timedelta(seconds=20))},
        "observability_state": "observable",
        "max_confidence": 0.9,
        "episode_duration_ms": 20_000,
        "condition_duration_ms": 15_000,
        "cameras": [
            {
                "camera_id": str(uuid.UUID(int=11)),
                "clips": [{"clip_id": str(ids.next()), "segment": "full"}],
            }
        ],
    }


def _item(
    record_type: str, at: datetime, document: dict[str, Any], org: uuid.UUID
) -> ProjectionInput:
    return ProjectionInput(
        record_id=ids.next(),
        organization_id=org,
        plant_id=PLANT_A if org == ORG_A else PLANT_B,
        chain_sequence=0,
        record_type=record_type,
        received_at=at,
        document=document,
    )


def _world(form: PackageForm) -> ExportWorld:
    builder = _Builder(ids)
    day = timedelta(days=1)
    t1 = MONTH_START + 3 * day
    subject = {"subject_kind": "finding", "subject_id": str(EPISODE)}
    finding = _observed(ZONE_A, PLANT_A, t1 - timedelta(minutes=5))
    finding["tier"] = "tier_1"
    finding["episode"] = {"episode_id": str(EPISODE), "segment_index": 0, "continues": False}
    detection = _observed(ZONE_A, PLANT_A, t1 + 2 * day)
    detection["detection_id"] = str(DETECTION)
    foreign = _observed(ZONE_B, PLANT_B, t1)
    foreign["tier"] = "tier_1"
    foreign["episode"] = {"episode_id": str(FOREIGN), "segment_index": 0, "continues": False}
    entry = ids.next()
    loop = [
        _item("finding_received", t1, finding, ORG_A),
        _item(
            "classification",
            t1 + day,
            {
                **subject,
                "outcome": "confirmed",
                "reason_category": "real_coexistence",
                "reinforced_reason": False,
                "classified_at": stamp(t1 + day),
            },
            ORG_A,
        ),
        _item(
            "action_assigned",
            t1 + 2 * day,
            {
                **subject,
                "action_id": str(ACTION),
                "action_type": "engineering",
                "assignee_user_id": str(uuid.UUID(int=99)),
                "due_at": stamp(t1 + 20 * day),
            },
            ORG_A,
        ),
        _item("detection_for_review_received", t1 + 2 * day, detection, ORG_A),
        _item(
            "review_resolution",
            t1 + 3 * day,
            {"detection_id": str(DETECTION), "outcome": "review_discarded"},
            ORG_A,
        ),
        _item(
            "action_evidence_added",
            t1 + 5 * day,
            {
                "evidence_entry_id": str(entry),
                "action_id": str(ACTION),
                "subject_id": str(EPISODE),
                "attachments": [
                    {
                        "attachment_id": str(ATTACHMENT),
                        "sha256": hashlib.sha256(ATTACHMENT_BYTES).hexdigest(),
                        "content_type": "application/pdf",
                        "size_bytes": len(ATTACHMENT_BYTES),
                        "filename": "barrera.pdf",
                        "no_identifiable_person_declared": True,
                    }
                ],
            },
            ORG_A,
        ),
        _item(
            "closure",
            t1 + 6 * day,
            {
                **subject,
                "action_id": str(ACTION),
                "evidence_entry_ids": [str(entry)],
                "signer": {"professional_license": "LIC-0001"},
                "signed_at": stamp(t1 + 6 * day),
            },
            ORG_A,
        ),
        _item("finding_received", t1, foreign, ORG_B),
    ]
    first_agreement, second_agreement = ids.next(), ids.next()
    users = [ids.next() for _ in range(3)]
    roles = ("plant_manager", "copasst", "coordinator_sst")

    def agreement(
        agreement_id: uuid.UUID, replaces: uuid.UUID | None, at: datetime
    ) -> dict[str, Any]:
        return {
            "agreement_id": str(agreement_id),
            "zone_id": str(ZONE_A),
            "signatories": [
                {"role": r, "user_id": str(u)} for r, u in zip(roles, users, strict=True)
            ],
            "confirmations": [
                {"user_id": str(u), "role_in_use": r, "confirmed_at": stamp(at), "origin": "in_app"}
                for r, u in zip(roles, users, strict=True)
            ],
            **({"replaces_agreement_id": str(replaces)} if replaces else {}),
        }

    marked, cleared = MONTH_START + 10 * day, MONTH_START + 12 * day
    governance = [
        _item(
            "use_agreement_signed",
            MONTH_START - 40 * day,
            agreement(first_agreement, None, MONTH_START - 40 * day),
            ORG_A,
        ),
        _item(
            "use_agreement_signed",
            MONTH_START + 8 * day,
            agreement(second_agreement, first_agreement, MONTH_START + 8 * day),
            ORG_A,
        ),
        _item(
            "walk_test_regression_marked",
            marked,
            {
                "zone_id": str(ZONE_A),
                "cause": "catalog_change",
                "catalog_version": 3,
                "affected_row_ids": "all",
                "marked_at": stamp(marked),
            },
            ORG_A,
        ),
        _item(
            "walk_test_regression_cleared",
            cleared,
            {
                "zone_id": str(ZONE_A),
                "cleared_by_session_id": str(ids.next()),
                "cleared_at": stamp(cleared),
            },
            ORG_A,
        ),
    ]
    for item in [*loop, *governance]:
        zone = ZONE_A if item.organization_id == ORG_A else ZONE_B
        builder.add(item.organization_id, item.plant_id or PLANT_A, zone, item)
    markers: dict[uuid.UUID, MarkerVerification] = {}
    blobs: dict[uuid.UUID, bytes] = {}
    records = builder.build(markers, blobs)
    for evidence_id in markers:
        markers[evidence_id] = MarkerVerification(MarkerResult.INTACT, t1, t1)
    node = uuid.UUID(int=0xA1)
    zone_subject = Subject("zone")
    coverage = CoverageInputs(
        observability=(
            ObservabilityInput(
                ids.next(),
                ids.next(),
                zone_subject,
                EventPhase.OPENED,
                CoverageState.OBSERVABLE,
                (),
                MONTH_START - day,
            ),
            ObservabilityInput(
                ids.next(),
                ids.next(),
                zone_subject,
                EventPhase.OPENED,
                CoverageState.NOT_OBSERVABLE,
                ("obstruction",),
                MONTH_START + 15 * day,
            ),
            ObservabilityInput(
                ids.next(),
                ids.next(),
                zone_subject,
                EventPhase.OPENED,
                CoverageState.OBSERVABLE,
                (),
                MONTH_START + 16 * day,
            ),
        ),
        communication=(
            CommunicationInput(
                ids.next(), node, CommunicationState.REACHABLE, MONTH_START - 60 * day
            ),
        ),
        gates=(GateInput(ids.next(), MONTH_START - 60 * day, ZoneMode.PRODUCTIVE),),
        assignments=(AssignmentInput(ids.next(), node, MONTH_START - 60 * day),),
    )
    revoked = MONTH_START + 20 * day
    public_keys = (
        _key(
            "checkpoint-2025-12", KeyStatus.RETIRED, MONTH_START - 400 * day, MONTH_START - 30 * day
        ),
        _key(
            "checkpoint-2026-01", KeyStatus.ACTIVE, MONTH_START - 30 * day, MONTH_START + 400 * day
        ),
    )
    checkpoints = {
        ORG_A: [
            _checkpoint(CheckpointChain(ChainKind.LEDGER, PLANT_A), "checkpoint-2026-01"),
            _checkpoint(CheckpointChain(ChainKind.AUDIT, None), "checkpoint-2025-12"),
        ],
        ORG_B: [_checkpoint(CheckpointChain(ChainKind.LEDGER, PLANT_B), "checkpoint-2026-01")],
    }
    standards = {
        (record.scope_zone_id, uuid.UUID(int=7), record.received_at): 2
        for record in records
        if record.record_type in ("finding_received", "detection_for_review_received")
        and record.scope_zone_id is not None
    }
    generated_at = MONTH_END + timedelta(hours=3)
    job = ExportJob(
        export_id=ids.next(),
        organization_id=ORG_A,
        plant_id=PLANT_A,
        zone_ids=(ZONE_A,),
        period_from=MONTH_START,
        period_to=MONTH_END,
        package_form=form,
        requested_by={
            "user_id": str(uuid.UUID(int=0xB0)),
            "display_name": "Gerencia de planta sintética",
            "role_in_use": "plant_manager",
        },
        correlation_id=uuid.UUID(int=0xC2),
        attempt=1,
        generated_at=generated_at,
        checkpoint_record_ids=tuple(item.entry_id for item in checkpoints[ORG_A]),
        context=world_context(ORG_A),
    )
    key = attachment_storage_key(
        organization_id=ORG_A,
        plant_id=PLANT_A,
        zone_id=ZONE_A,
        subject_id=EPISODE,
        attachment_id=ATTACHMENT,
        content_type=AttachmentContentType.APPLICATION_PDF,
    )
    return ExportWorld(
        organizations=(ORG_A, ORG_B),
        plants={ORG_A: PLANT_A, ORG_B: PLANT_B},
        zones={ORG_A: [ZONE_A], ORG_B: [ZONE_B]},
        zone_plant={ZONE_A: (ORG_A, PLANT_A), ZONE_B: (ORG_B, PLANT_B)},
        records=records,
        audit=[],
        coverage={ZONE_A: coverage, ZONE_B: coverage},
        usage_changes={
            ZONE_A: [
                (MONTH_START - 40 * day, GateStatus.APPROVED, None),
                (revoked, GateStatus.REVOKED, "Revocación sintética: el comité retiró el acuerdo"),
            ],
            ZONE_B: [(MONTH_START - 40 * day, GateStatus.APPROVED, None)],
        },
        assignments={
            zone: [
                ZoneAssignmentPeriod(
                    node_id=node,
                    assigned_at=MONTH_START - 60 * day,
                    unassigned_at=None,
                    replaces_node_id=None,
                )
            ]
            for zone in (ZONE_A, ZONE_B)
        },
        occupancy={ZONE_A: (True, 60), ZONE_B: (False, 60)},
        markers=markers,
        blobs=blobs,
        attachment_blobs={key: ATTACHMENT_BYTES},
        checkpoints=checkpoints,
        keys=public_keys,
        regression={ZONE_A: [(marked, cleared, "catalog_change")], ZONE_B: []},
        standards=standards,  # type: ignore[arg-type]
        job=job,
    )


def _key(key_id: str, status: KeyStatus, start: datetime, end: datetime) -> CheckpointPublicKey:
    return CheckpointPublicKey(
        key_id=key_id,
        public_key=base64.b64encode(hashlib.sha256(key_id.encode()).digest()).decode(),
        status=status,
        valid_from=start,
        valid_until=end,
    )


def _checkpoint(chain: CheckpointChain, key_id: str) -> StoredCheckpoint:
    return StoredCheckpoint(
        chain=chain,
        sequence=42,
        entry_id=ids.next(),
        entry_hash=hashlib.sha256(str(chain).encode()).hexdigest(),
        content=CheckpointContent(
            covered_sequence=41,
            covered_hash=hashlib.sha256(key_id.encode()).hexdigest(),
            taken_at=stamp(MONTH_END + timedelta(hours=3)),
            key_id=key_id,
            signature="c2lnbmF0dXJh",
        ),
    )


@pytest.fixture(scope="module", params=[PackageForm.WITH_CLIPS, PackageForm.WITHOUT_CLIPS])
def package(request: pytest.FixtureRequest) -> tuple[ExportWorld, Composed]:
    world = _world(request.param)
    return world, compose_world(world)


def _findings(composed: Composed) -> dict[str, dict[str, Any]]:
    document = composed.json(f"findings/zone-{ZONE_A}.json")
    return {finding["subject_id"]: finding for finding in document["findings"]}


def test_the_manifest_declares_organization_plant_and_period(
    package: tuple[ExportWorld, Composed],
) -> None:
    _, composed = package
    manifest = composed.manifest
    assert manifest["organization"]["organization_id"] == str(ORG_A)
    assert manifest["plant"]["plant_id"] == str(PLANT_A)
    assert manifest["plant"]["timezone"] == "America/Bogota"
    assert manifest["period"]["from"] == "2026-01-01T05:00:00.000Z"
    assert manifest["period"]["to"] == "2026-02-01T05:00:00.000Z"
    assert manifest["period"]["from_local"].startswith("2026-01-01T00:00:00.000-05:00")
    assert manifest["generated_at"] == "2026-02-01T08:00:00.000Z"


def test_every_finding_of_the_month_with_its_state_and_none_of_the_other_organization(
    package: tuple[ExportWorld, Composed],
) -> None:
    _, composed = package
    findings = _findings(composed)
    assert {subject: f["lifecycle_state"] for subject, f in findings.items()} == {
        str(EPISODE): "closed",
        str(DETECTION): "discarded",
    }
    manifest = composed.manifest
    assert (manifest["finding_count"], manifest["closed_finding_count"]) == (2, 1)
    assert manifest["discarded_count"] == 1
    every = b"".join(composed.artifacts.values()) + str(manifest).encode()
    for foreign in (ORG_B, PLANT_B, ZONE_B, FOREIGN):
        assert str(foreign).encode() not in every


def test_each_artifact_hash_in_the_manifest_matches_its_bytes(
    package: tuple[ExportWorld, Composed],
) -> None:
    _, composed = package
    declared = {item["path"]: item for item in composed.manifest["artifacts"]}
    assert set(declared) == set(composed.artifacts)
    for path, data in composed.artifacts.items():
        assert declared[path]["sha256"] == hashlib.sha256(data).hexdigest()
        assert declared[path]["size_bytes"] == len(data)
    kinds = {item["kind"] for item in declared.values()}
    assert {"findings_json", "coverage", "audit", "checkpoint", "public_key", "attachment"} <= kinds


def test_clips_with_their_bytes_or_listed_with_hash_duration_and_zone(
    package: tuple[ExportWorld, Composed],
) -> None:
    world, composed = package
    manifest = composed.manifest
    clips = manifest["clips"]
    assert len(clips) == 2
    for clip in clips:
        assert clip["zone_id"] == str(ZONE_A)
        assert clip["duration_ms"] == 20_000
        expected = world.blobs[uuid.UUID(clip["evidence_id"])]
        assert clip["sha256"] == hashlib.sha256(expected).hexdigest()
        if manifest["package_form"] == "with_clips":
            assert composed.artifacts[clip["path"]] == expected
        else:
            assert clip["path"] is None and clip["included"] is False
    if manifest["package_form"] == "with_clips":
        assert manifest["package_form_notice_es"] == WITH_CLIPS_NOTICE_ES
    else:
        assert manifest["package_form_notice_es"] == WITHOUT_CLIPS_NOTICE_ES
        assert not [path for path in composed.artifacts if path.startswith("clips/")]


def test_the_attachment_goes_with_its_declaration(package: tuple[ExportWorld, Composed]) -> None:
    _, composed = package
    (attachment,) = composed.manifest["attachments"]
    assert attachment["declaration"] == "no_identifiable_person_declared"
    assert composed.artifacts[attachment["path"]] == ATTACHMENT_BYTES
    assert composed.manifest["anonymization_statement_es"] == ANONYMIZATION_STATEMENT_ES


def test_revocation_regression_and_superseded_agreement_are_declared(
    package: tuple[ExportWorld, Composed],
) -> None:
    _, composed = package
    manifest = composed.manifest
    (revoked,) = manifest["revoked_gates"]
    assert revoked["since"] == "2026-01-21T05:00:00.000Z"
    assert revoked["reason_es"] == "Revocación sintética: el comité retiró el acuerdo"
    assert manifest["revocation_notice_es"] == REVOCATION_NOTICE_ES
    (regression,) = manifest["regression_periods"]
    assert (regression["from"], regression["to"], regression["cause"]) == (
        "2026-01-11T05:00:00.000Z",
        "2026-01-13T05:00:00.000Z",
        "catalog_change",
    )
    assert manifest["regression_notice_es"] == REGRESSION_NOTICE_ES
    (zone,) = manifest["zones"]
    states = [agreement["state_in_period"] for agreement in zone["agreements"]]
    assert states == ["superseded", "revoked"]
    assert all(len(agreement["signatories"]) == 3 for agreement in zone["agreements"])
    notes = composed.json(f"coverage/zone-{ZONE_A}.json")["annotations"]
    assert {note["kind"] for note in notes} == {"walk_test_not_current", "usage_gate_revoked"}


def test_coverage_gaps_are_shown_and_never_mean_nothing_happened(
    package: tuple[ExportWorld, Composed],
) -> None:
    _, composed = package
    coverage = composed.json(f"coverage/zone-{ZONE_A}.json")
    gaps = [i for i in coverage["intervals"] if i["state"] != "observable"]
    assert gaps and gaps[0]["causes"] == ["obstruction"]
    assert coverage["summary"]["total_ms"] == 31 * 24 * 3600 * 1000
    assert composed.manifest["coverage_notice_es"] == COVERAGE_NOTICE_ES


def test_single_occupancy_zone_is_aggregated_and_says_why(
    package: tuple[ExportWorld, Composed],
) -> None:
    _, composed = package
    manifest = composed.manifest
    assert manifest["aggregated_zone_ids"] == [str(ZONE_A)]
    assert manifest["aggregation_notice_es"] == AGGREGATION_NOTICE_ES
    finding = _findings(composed)[str(EPISODE)]
    assert finding["time_aggregated"] is True
    assert finding["occurred_at"].endswith(":00:00.000Z")
    assert all("content" not in record for record in finding["records"])


def test_document_and_verifier_are_written_and_nothing_is_declared_absent(
    package: tuple[ExportWorld, Composed],
) -> None:
    """Desde TASK-319 el documento portátil ya no falta, y desde TASK-346 tampoco el verificador:
    los dos viajan, con su hash, y ninguna entrada se declara ausente."""
    _, composed = package
    manifest = composed.manifest
    assert manifest["absent_entries"] == []
    assert {item["kind"] for item in manifest["artifacts"]} >= {"document", "verifier"}
    assert composed.package.document_sha256 != EMPTY_SHA256
    assert composed.package.stored.document is not None
    # TASK-346: el verificador viaja con su hash y su versión declarada (NFR-LAZ-53).
    bundle = load_verifier_bundle()
    assert composed.artifacts[VERIFIER_PATH] == bundle.data
    (entry,) = [item for item in manifest["artifacts"] if item["kind"] == "verifier"]
    assert entry["path"] == VERIFIER_PATH
    assert entry["sha256"] == bundle.sha256
    assert manifest["verifier_version"] == VERIFIER_VERSION == bundle.version
    assert composed.package.verifier_version == VERIFIER_VERSION


def test_checkpoints_and_both_public_keys_travel_inside(
    package: tuple[ExportWorld, Composed],
) -> None:
    world, composed = package
    manifest = composed.manifest
    assert {item["entry_id"] for item in manifest["checkpoints"]} == {
        str(item) for item in world.job.checkpoint_record_ids
    }
    assert [key["status"] for key in manifest["checkpoint_public_keys"]] == ["retired", "active"]
    assert (
        composed.json("keys/checkpoint-public-keys.json")["keys"]
        == manifest["checkpoint_public_keys"]
    )
