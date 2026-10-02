"""N-12 · Clip sustituido o sin difuminar (H-04, H-34; business-rules §14).

**Qué intenta**: que un hallazgo referencie un objeto distinto del que el nodo firmó (otro clip,
de otro tamaño), uno sin la marca de anonimización (identificable), uno que no existe, o uno de
otra zona u otra organización.

**Qué lo detiene** (BR-NUC-64, BR-NUC-65):

- BR-NUC-64: dentro de la escritura, por cada referencia: el objeto existe, su tamaño y su suma
  SHA-256 de objeto entero coinciden, y lleva la marca de anonimización; cualquier fallo rechaza
  la escritura **completa** (``evidence_missing``, ``evidence_hash_mismatch``,
  ``evidence_not_anonymized``): un hallazgo sin su evidencia no nace;
- BR-NUC-65: la clave del objeto está ligada a organización, planta, zona y nodo del registro; una
  clave de otro alcance ni se consulta al almacén.
"""

from __future__ import annotations

import base64
import uuid
from typing import Any

import pytest

from tests.platform_support import EVIDENCE_TYPE, Platform
from tests.writer_support import Place, order_document
from vigia_platform.ledger.application.writer import LedgerRejection, LedgerRejectionCode, Receipt
from vigia_platform.shared.storage import ANONYMIZED_METADATA_KEY, ChecksumType, ObjectHead

pytestmark = pytest.mark.integration


def _place(platform: Platform) -> tuple[Place, Place]:
    """Dos zonas con nodo de la misma planta."""
    site = platform.site(plants=1, zones_per_plant=2)
    (plant_id, first), (_, second) = site.zones()
    return (
        Place(site.organization_id, plant_id, first, platform.node(site, plant_id, first)),
        Place(site.organization_id, plant_id, second, platform.node(site, plant_id, second)),
    )


def _store(platform: Platform, clip: dict[str, Any], **changes: Any) -> None:
    """Deja en el almacén el objeto que ``clip`` describe, o uno alterado según ``changes``."""
    sha = bytes.fromhex(str(changes.get("sha256", clip["sha256"])))
    marker = changes.get("marker", "1")
    platform.storage.objects[str(clip["storage_key"])] = ObjectHead(
        key=str(clip["storage_key"]),
        size_bytes=int(changes.get("size_bytes", clip["size_bytes"])),
        checksum_sha256=base64.b64encode(sha).decode("ascii"),
        checksum_type=ChecksumType.FULL_OBJECT,
        content_type=str(clip["content_type"]),
        metadata={} if marker is None else {ANONYMIZED_METADATA_KEY: marker},
        version_id="v1",
    )


def _write(platform: Platform, place: Place, document: dict[str, Any]) -> Any:
    return platform.write(place.organization_id, EVIDENCE_TYPE, document)


def _counts(platform: Platform, organization_id: uuid.UUID) -> tuple[int, int]:
    records = platform.fetch(
        "SELECT count(*) AS n FROM ledger.ledger_record WHERE organization_id = $1",
        organization_id,
    )[0]["n"]
    evidence = platform.fetch(
        "SELECT count(*) AS n FROM ledger.evidence WHERE organization_id = $1", organization_id
    )[0]["n"]
    return int(records), int(evidence)


def test_n12_the_signed_and_anonymized_clip_is_registered(platform: Platform) -> None:
    place, _ = _place(platform)
    document = order_document(place, clips=2)
    for clip in document["clips"]:
        _store(platform, clip)
    receipt = _write(platform, place, document)
    assert isinstance(receipt, Receipt), receipt
    rows = platform.fetch(
        "SELECT sha256, storage_key FROM ledger.evidence WHERE record_id = $1", receipt.record_id
    )
    assert sorted((r["sha256"], r["storage_key"]) for r in rows) == sorted(
        (c["sha256"], c["storage_key"]) for c in document["clips"]
    )


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"sha256": "ab" * 32}, LedgerRejectionCode.EVIDENCE_HASH_MISMATCH),
        ({"size_bytes_delta": 1}, LedgerRejectionCode.EVIDENCE_HASH_MISMATCH),
        ({"marker": None}, LedgerRejectionCode.EVIDENCE_NOT_ANONYMIZED),
        ({"marker": "0"}, LedgerRejectionCode.EVIDENCE_NOT_ANONYMIZED),
        ({"marker": "true"}, LedgerRejectionCode.EVIDENCE_NOT_ANONYMIZED),
        ({"missing": True}, LedgerRejectionCode.EVIDENCE_MISSING),
    ],
    ids=["otro-clip", "otro-tamano", "sin-marca", "marca-0", "marca-true", "inexistente"],
)
def test_n12_one_bad_clip_rejects_the_whole_record(
    platform: Platform, changes: dict[str, Any], code: LedgerRejectionCode
) -> None:
    place, _ = _place(platform)
    document = order_document(place, clips=2)
    good, bad = document["clips"]
    _store(platform, good)
    if changes.get("missing"):
        platform.storage.objects[str(bad["storage_key"])] = None
    elif "size_bytes_delta" in changes:
        _store(platform, bad, size_bytes=int(bad["size_bytes"]) + changes["size_bytes_delta"])
    else:
        _store(platform, bad, **changes)
    before = _counts(platform, place.organization_id)
    rejection = _write(platform, place, document)
    assert isinstance(rejection, LedgerRejection), rejection
    assert rejection.code is code
    # Ni el registro ni la evidencia buena: nada.
    assert _counts(platform, place.organization_id) == before


def test_n12_a_key_of_another_zone_or_organization_is_never_looked_up(
    platform: Platform,
) -> None:
    place, other_zone = _place(platform)
    stranger, _ = _place(platform)
    for owner in (other_zone, stranger):
        document = order_document(place, clips=1)
        (clip,) = document["clips"]
        # El objeto existe, íntegro y anonimizado… pero bajo la clave de otro alcance.
        clip["storage_key"] = owner.storage_key(clip["clip_id"])
        _store(platform, clip)
        looked_up: list[str] = []
        original = platform.storage.head_object

        async def spy(key: str, original: Any = original, looked_up: list[str] = looked_up) -> Any:
            looked_up.append(key)
            return await original(key)

        platform.storage.head_object = spy  # type: ignore[method-assign]
        try:
            before = _counts(platform, place.organization_id)
            rejection = _write(platform, place, document)
        finally:
            del platform.storage.head_object
        assert isinstance(rejection, LedgerRejection), rejection
        assert rejection.code is LedgerRejectionCode.EVIDENCE_MISSING
        assert looked_up == []
        assert _counts(platform, place.organization_id) == before
