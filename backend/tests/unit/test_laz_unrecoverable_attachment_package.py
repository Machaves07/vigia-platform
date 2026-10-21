"""DR-LAZ-04 sin versión anterior en el paquete (VIG-274; runbook 7.3, paso 4).

Sobre el mundo determinista de una exportación (``job_world``) con criptografía real
(``seal_world``): un hallazgo con una entrada de evidencia de dos adjuntos, uno de ellos perdido
del depósito y declarado no recuperable en el expediente (``closure_attachment_unrecoverable``).

- **Composición**: el paquete se compone sin pedir los bytes del perdido (el almacén del mundo
  no los tiene: pedirlos fallaría) y el manifiesto lo declara ``available = false``, sin ruta, con
  la declaración **igual** campo a campo al registro del hallazgo (hash, fecha, identificadores) y
  el texto fijo; el otro adjunto viaja con sus bytes. Mismo estado declarado, mismo
  ``archive_sha256``.
- **Sin declaración**, el mismo adjunto perdido hace fallar la composición: nunca silencio.
- **Verificador** (``verify_export``, el que viaja en el paquete): ``intact`` y anclado con la
  declaración, también en una zona unipersonal (registros sin contenido). Rechaza con
  ``manifest_mismatch`` un manifiesto **firmado con la clave activa** que omite el adjunto, lo
  declara disponible sin sus bytes, lo declara no recuperable sin registro o con otra fecha, o
  presenta como disponible uno que el expediente declara no recuperable.

Se compara por campos estructurados y códigos, nunca por subcadenas (VIG-194). Solo datos
generados.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import io
import json
import tempfile
import uuid
import zipfile
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from tests.export_contents_support import (
    SMALL_VOLUMES,
    Composed,
    compose_world,
    fake_document_renderer,
)
from tests.export_stream_support import MemoryExportStore, RecordingSink
from tests.properties.laz.strategies.export import (
    BASE,
    DAY,
    ExportWorld,
    WorldPorts,
    _Builder,
    job_world,
    world_context,
)
from tests.properties.laz.strategies.projection import _Ids, stamp
from tests.verifier_bundle_support import (
    ACTIVE_KEY,
    compose_to_directory,
    private_key,
    rewrite_volume,
    seal_world,
    volume_paths,
)
from vigia_platform.accreditation.export.composer_port import ExportJob
from vigia_platform.accreditation.export.contents import ExportComposer
from vigia_platform.accreditation.export.stream import ExportStream
from vigia_platform.accreditation.export.volumes import MANIFEST_NAME, MANIFEST_SIGNATURE_NAME
from vigia_platform.accreditation.export_verifier import verify_export
from vigia_platform.accreditation.manifest.build import ManifestInvalid, build_manifest
from vigia_platform.accreditation.manifest.model import (
    ATTACHMENT_UNRECOVERABLE_ES,
    FIXED_TEXTS_ES,
    MANIFEST_VERSION,
    ManifestInputs,
)
from vigia_platform.accreditation.verifier_bundle import manifest_signature_payload
from vigia_platform.ledger.canonical import canonical_bytes_sync
from vigia_platform.loop.adapters.attachments.rules import attachment_storage_key
from vigia_platform.loop.application.unrecoverable import declaration_id
from vigia_platform.loop.domain.enums import AttachmentContentType, PackageForm
from vigia_platform.loop.projection.fold import ProjectionInput
from vigia_platform.loop.record_types import ClosureAttachmentUnrecoverable

ids = _Ids(uuid.UUID(int=0x274))
ORGANIZATION: Final = ids.next()
PLANT: Final = ids.next()
ZONE: Final = ids.next()
ACTION: Final = uuid.UUID("018f0000-0000-7000-8000-000000000a01")
ENTRY: Final = uuid.UUID("018f0000-0000-7000-8000-000000000e01")
LOST: Final = uuid.UUID("018f0000-0000-7000-8000-000000000f01")
KEPT: Final = uuid.UUID("018f0000-0000-7000-8000-000000000f02")
LOST_BYTES: Final = b"%PDF-1.7\n% adjunto sintetico perdido\n" + b"1" * 300
KEPT_BYTES: Final = b"%PDF-1.7\n% adjunto sintetico conservado\n" + b"2" * 300
DETECTED: Final = BASE + 6 * DAY


def _job() -> ExportJob:
    return ExportJob(
        export_id=ids.next(),
        organization_id=ORGANIZATION,
        plant_id=PLANT,
        zone_ids=(ZONE,),
        period_from=BASE,
        period_to=BASE + 5 * DAY,
        package_form=PackageForm.WITHOUT_CLIPS,
        requested_by={
            "user_id": str(uuid.UUID(int=0xB0)),
            "display_name": "P",
            "role_in_use": None,
        },
        correlation_id=uuid.UUID(int=0xC2),
        attempt=1,
        generated_at=BASE + 8 * DAY,
        checkpoint_record_ids=(ids.next(),),
        context=world_context(ORGANIZATION),
    )


JOB: Final = _job()


def _attachment(attachment_id: uuid.UUID, data: bytes) -> dict[str, Any]:
    return {
        "attachment_id": str(attachment_id),
        "sha256": hashlib.sha256(data).hexdigest(),
        "content_type": "application/pdf",
        "size_bytes": len(data),
        "filename": "resguardo.pdf",
        "no_identifiable_person_declared": True,
    }


def _key(attachment_id: uuid.UUID, subject: uuid.UUID) -> str:
    return attachment_storage_key(
        organization_id=ORGANIZATION,
        plant_id=PLANT,
        zone_id=ZONE,
        subject_id=subject,
        attachment_id=attachment_id,
        content_type=AttachmentContentType.APPLICATION_PDF,
    )


def declaration_content(subject: uuid.UUID, evidence_record: uuid.UUID) -> dict[str, Any]:
    """El contenido que escribe ``declare-unrecoverable-attachment`` para ``LOST``."""
    content: dict[str, Any] = {
        "declaration_id": str(declaration_id(LOST)),
        "subject_kind": "finding",
        "subject_id": str(subject),
        "action_id": str(ACTION),
        "evidence_entry_id": str(ENTRY),
        "evidence_record_id": str(evidence_record),
        "attachment_id": str(LOST),
        "sha256": hashlib.sha256(LOST_BYTES).hexdigest(),
        "size_bytes": len(LOST_BYTES),
        "content_type": "application/pdf",
        "detected_at": stamp(DETECTED),
        "previous_version_available": False,
    }
    # El modelo estricto del tipo, como lo valida el escritor (desde JSON).
    ClosureAttachmentUnrecoverable.model_validate_json(json.dumps(content))
    return content


def world(*, declared: bool, lost: bool = True, single: bool = False) -> ExportWorld:
    """El mundo de ``JOB`` con la entrada de evidencia y, si ``declared``, la declaración."""
    base = job_world(JOB)
    (finding,) = [r for r in base.records if r.record_type == "finding_received"]
    subject = uuid.UUID(finding.document()["episode"]["episode_id"])
    evidence_record = ids.next()
    extra = [
        ProjectionInput(
            record_id=evidence_record,
            organization_id=ORGANIZATION,
            plant_id=PLANT,
            chain_sequence=0,
            record_type="action_evidence_added",
            received_at=finding.received_at + timedelta(hours=1),
            document={
                "evidence_entry_id": str(ENTRY),
                "action_id": str(ACTION),
                "subject_id": str(subject),
                "attachments": [_attachment(LOST, LOST_BYTES), _attachment(KEPT, KEPT_BYTES)],
            },
        )
    ]
    if declared:
        extra.append(
            ProjectionInput(
                record_id=ids.next(),
                organization_id=ORGANIZATION,
                plant_id=PLANT,
                chain_sequence=0,
                record_type="closure_attachment_unrecoverable",
                received_at=DETECTED,
                document=declaration_content(subject, evidence_record),
            )
        )
    builder = _Builder(ids)
    for record in base.records:
        builder.add(
            record.organization_id,
            record.plant_id,  # type: ignore[arg-type]
            record.scope_zone_id,  # type: ignore[arg-type]
            ProjectionInput(
                record_id=record.record_id,
                organization_id=record.organization_id,
                plant_id=record.plant_id,
                chain_sequence=0,
                record_type=record.record_type,
                received_at=record.received_at,
                document=record.document(),
            ),
        )
    for item in extra:
        builder.add(ORGANIZATION, PLANT, ZONE, item)
    records = builder.build(dict(base.markers), dict(base.blobs))
    blobs = {_key(KEPT, subject): KEPT_BYTES}
    if not lost:
        blobs[_key(LOST, subject)] = LOST_BYTES
    occupancy = {**base.occupancy, ZONE: (single, 60)}
    return dataclasses.replace(base, records=records, attachment_blobs=blobs, occupancy=occupancy)


def _records(composed: Composed, record_type: str) -> list[dict[str, Any]]:
    (finding,) = composed.json(f"findings/zone-{ZONE}.json")["findings"]
    return [r for r in finding["records"] if r["record_type"] == record_type]


def _attachments(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["attachment_id"]: item for item in manifest["attachments"]}


@pytest.fixture(scope="module")
def sealed() -> ExportWorld:
    return seal_world(world(declared=True))


@pytest.fixture(scope="module")
def package(sealed: ExportWorld) -> Iterator[tuple[Path, Composed]]:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary) / "paquete"
        yield directory, compose_to_directory(sealed, directory)


# --- composición ----------------------------------------------------------------------------


def test_the_declaration_is_the_same_structured_field_in_the_finding_and_the_manifest(
    package: tuple[Path, Composed],
) -> None:
    _, composed = package
    manifest = composed.manifest
    assert manifest["manifest_version"] == MANIFEST_VERSION == 2
    (record,) = _records(composed, "closure_attachment_unrecoverable")
    content = record["content"]
    lost = _attachments(manifest)[str(LOST)]
    assert lost["available"] is False and lost["path"] is None
    assert lost["unrecoverable"] == {
        "record_id": record["record_id"],
        "declaration_id": content["declaration_id"],
        "sha256": content["sha256"],
        "detected_at": content["detected_at"],
        "reason_es": ATTACHMENT_UNRECOVERABLE_ES,
    }
    assert (lost["sha256"], lost["size_bytes"]) == (content["sha256"], content["size_bytes"])
    assert content["detected_at"] == stamp(DETECTED)
    assert ATTACHMENT_UNRECOVERABLE_ES in FIXED_TEXTS_ES
    # El perdido no viaja ni se pidió; el otro va con sus bytes y su hash.
    assert f"attachments/{LOST}.pdf" not in composed.artifacts
    kept = _attachments(manifest)[str(KEPT)]
    assert (kept["available"], kept["unrecoverable"]) == (True, None)
    assert composed.artifacts[kept["path"]] == KEPT_BYTES


def test_the_finding_and_its_closure_state_are_not_altered(
    package: tuple[Path, Composed],
) -> None:
    _, composed = package
    plain = compose_world(world(declared=False, lost=False))
    (with_declaration,) = composed.json(f"findings/zone-{ZONE}.json")["findings"]
    (without,) = plain.json(f"findings/zone-{ZONE}.json")["findings"]
    states = ("lifecycle_state", "closed_at", "classification_count", "reopening_count")
    assert {k: with_declaration[k] for k in states} == {k: without[k] for k in states}
    assert manifest_counts(composed.manifest) == manifest_counts(plain.manifest)


def manifest_counts(manifest: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in manifest.items() if key.endswith("_count")}


def test_the_same_declared_state_composes_the_same_archive(sealed: ExportWorld) -> None:
    first, second = compose_world(sealed), compose_world(sealed)
    assert first.package.archive_sha256 == second.package.archive_sha256
    assert first.package.manifest_sha256 == second.package.manifest_sha256


def test_without_the_declaration_the_lost_attachment_still_fails_the_export() -> None:
    with pytest.raises(KeyError):
        compose_world(world(declared=False))


def test_the_manifest_rejects_an_attachment_without_bytes_and_without_declaration(
    sealed: ExportWorld,
) -> None:
    captured: list[ManifestInputs] = []

    def capture(inputs: ManifestInputs) -> bytes:
        captured.append(inputs)
        return fake_document_renderer(inputs)

    composer = ExportComposer(
        stream=ExportStream(store=MemoryExportStore(), settings=SMALL_VOLUMES),
        ports=WorldPorts(sealed).ports(),
        document_renderer=capture,
    )
    asyncio.run(composer.compose(JOB, RecordingSink()))
    inputs = captured[0]
    lost = next(a for a in inputs.attachments if a.attachment_id == LOST)
    kept = next(a for a in inputs.attachments if a.attachment_id == KEPT)
    for broken in (
        dataclasses.replace(lost, unrecoverable=None),  # sin bytes y sin declaración
        dataclasses.replace(lost, path=f"attachments/{LOST}.pdf"),  # bytes y declaración
    ):
        with pytest.raises(ManifestInvalid):
            build_manifest(dataclasses.replace(inputs, attachments=(broken, kept)))
    with pytest.raises(ManifestInvalid):
        build_manifest(dataclasses.replace(inputs, attachments=(lost, kept, kept)))


# --- verificador ----------------------------------------------------------------------------


@pytest.mark.parametrize("single", [False, True], ids=["con_contenido", "unipersonal"])
def test_the_verifier_accepts_the_declaration(single: bool) -> None:
    sealed = seal_world(world(declared=True, single=single))
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary) / "paquete"
        composed = compose_to_directory(sealed, directory)
        (record,) = _records(composed, "closure_attachment_unrecoverable")
        assert ("content" in record) is not single
        report = verify_export(directory)
        assert report.failure is None, report.failure
        assert (report.status, report.anchored_by) == ("intact", ["manifest_signature"])


def _forge(
    composed: Composed,
    directory: Path,
    change: Callable[[dict[str, Any]], None],
    drop: frozenset[str] = frozenset(),
) -> None:
    """Reescribe el manifiesto con ``change`` y lo **firma con la clave activa** (quien firma
    es la plataforma: solo las comprobaciones de contenido pueden delatarlo)."""
    manifest = json.loads(json.dumps(composed.manifest))
    change(manifest)
    manifest["artifacts"] = [a for a in manifest["artifacts"] if a["path"] not in drop]
    data = canonical_bytes_sync(manifest)
    payload = manifest_signature_payload(manifest, data)
    signature = base64.b64encode(private_key(ACTIVE_KEY).sign(canonical_bytes_sync(payload)))
    envelope = canonical_bytes_sync(
        {"payload": payload, "key_id": ACTIVE_KEY, "signature": signature.decode("ascii")}
    )
    changes: dict[str, bytes | None] = {
        MANIFEST_NAME: data,
        MANIFEST_SIGNATURE_NAME: envelope,
        **dict.fromkeys(drop),
    }
    for volume in volume_paths(directory):
        volume.write_bytes(rewrite_volume(volume.read_bytes(), changes))


def _lost(manifest: dict[str, Any]) -> dict[str, Any]:
    return next(a for a in manifest["attachments"] if a["attachment_id"] == str(LOST))


def _kept(manifest: dict[str, Any]) -> dict[str, Any]:
    return next(a for a in manifest["attachments"] if a["attachment_id"] == str(KEPT))


def _omit_lost(manifest: dict[str, Any]) -> None:
    manifest["attachments"].remove(_lost(manifest))


def _lost_available_without_bytes(manifest: dict[str, Any]) -> None:
    _lost(manifest).update(available=True, path=None, unrecoverable=None)


def _kept_unrecoverable_without_record(manifest: dict[str, Any]) -> None:
    kept = _kept(manifest)
    kept.update(
        available=False,
        path=None,
        unrecoverable={**_lost(manifest)["unrecoverable"], "sha256": kept["sha256"]},
    )


def _kept_unrecoverable_pointing_elsewhere(manifest: dict[str, Any]) -> None:
    kept = _kept(manifest)
    kept.update(
        available=False,
        path=None,
        unrecoverable={
            **_lost(manifest)["unrecoverable"],
            "record_id": str(uuid.UUID(int=0xDEAD)),
            "sha256": kept["sha256"],
        },
    )


def _lost_with_another_date(manifest: dict[str, Any]) -> None:
    _lost(manifest)["unrecoverable"]["detected_at"] = stamp(DETECTED + DAY)


def _no_available_flag(manifest: dict[str, Any]) -> None:
    del _lost(manifest)["available"]


FORGERIES: Final = [
    # (nombre, cambio del manifiesto, ¿quita también los bytes del adjunto conservado?)
    ("omite_el_adjunto", _omit_lost, False),
    ("disponible_sin_bytes", _lost_available_without_bytes, False),
    ("no_recuperable_sin_su_registro", _kept_unrecoverable_without_record, True),
    ("no_recuperable_con_otro_registro", _kept_unrecoverable_pointing_elsewhere, True),
    ("otra_fecha", _lost_with_another_date, False),
    ("sin_available", _no_available_flag, False),
]


@pytest.mark.parametrize(
    ("change", "drop_kept"),
    [(change, drop) for _, change, drop in FORGERIES],
    ids=[name for name, _, _ in FORGERIES],
)
def test_the_verifier_rejects_an_absent_attachment_that_is_not_declared(
    package: tuple[Path, Composed],
    change: Callable[[dict[str, Any]], None],
    drop_kept: bool,
    tmp_path: Path,
) -> None:
    original, composed = package
    directory = tmp_path / "alterado"
    directory.mkdir()
    for volume in volume_paths(original):
        (directory / volume.name).write_bytes(volume.read_bytes())
    removed = frozenset({_kept(composed.manifest)["path"]}) if drop_kept else frozenset()
    _forge(composed, directory, change, removed)
    report = verify_export(directory)
    assert report.failure is not None
    position = report.failure.position
    assert (report.failure.reason, position.artifact) == ("manifest_mismatch", MANIFEST_NAME)
    assert position.pointer.startswith("/attachments")


def test_the_verifier_rejects_a_lost_attachment_presented_as_available() -> None:
    """Con los bytes en el paquete, pero el expediente lo declara no recuperable."""
    sealed = seal_world(world(declared=True))
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary) / "paquete"
        composed = compose_to_directory(sealed, directory)
        honest = seal_world(world(declared=False, lost=False))
        other = compose_world(honest)
        artifact = next(
            a for a in other.manifest["artifacts"] if a["path"] == f"attachments/{LOST}.pdf"
        )

        def present(manifest: dict[str, Any]) -> None:
            _lost(manifest).update(available=True, path=artifact["path"], unrecoverable=None)
            manifest["artifacts"].append({**artifact, "volume": 1})

        _forge(composed, directory, present)
        volume = directory / "vol-01.zip"
        volume.write_bytes(rewrite_volume_adding(volume.read_bytes(), artifact["path"], LOST_BYTES))
        report = verify_export(directory)
        assert report.failure is not None
        assert report.failure.reason == "manifest_mismatch"


def rewrite_volume_adding(data: bytes, name: str, content: bytes) -> bytes:
    """El volumen con una entrada más al final."""
    output = io.BytesIO(data)
    with zipfile.ZipFile(output, mode="a", allowZip64=False) as archive:
        archive.writestr(zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0)), content)
    return output.getvalue()
