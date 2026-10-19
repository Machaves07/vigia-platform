"""``accreditation.verifier_bundle``: el verificador que viaja en el paquete (TASK-346; LC-LAZ-25;
NFR-LAZ-53).

- ``load_verifier_bundle`` lee el archivo versionado, con su SHA-256 y la versión **embebida** en
  él, que es la que la plataforma declara (igualdad); un archivo de otra versión, sin versión, con
  dos versiones o ilegible no viaja.
- La entrada del paquete es ``vigia_verify.py`` de tipo ``verifier``, comprimida, y ``include``
  la escribe con esos bytes (el paquete real la lleva: ``tests/examples/test_laz_h37_*`` y
  PR-LAZ-28).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from vigia_platform.accreditation.export.stream import ArchiveWriter, Artifact
from vigia_platform.accreditation.export_verifier import EXPORT_VERIFIER_VERSION
from vigia_platform.accreditation.verifier_bundle import (
    MANIFEST_SIGNATURE_DECLARATION,
    VERIFIER_FILE,
    VERIFIER_KIND,
    VERIFIER_PATH,
    VERIFIER_VERSION,
    VerifierBundle,
    VerifierBundleError,
    embedded_version,
    load_verifier_bundle,
    manifest_signature_payload,
    sign_manifest,
)
from vigia_platform.ledger.canonical import canonical_bytes_sync
from vigia_platform.shared.signing.keys import SigningPurpose


def test_the_packaged_file_carries_the_declared_version() -> None:
    bundle = load_verifier_bundle()
    data = VERIFIER_FILE.read_bytes()
    assert bundle.data == data
    assert bundle.sha256 == hashlib.sha256(data).hexdigest()
    assert bundle.version == embedded_version(data) == VERIFIER_VERSION == EXPORT_VERIFIER_VERSION


@pytest.mark.parametrize(
    "source",
    [
        b"x = 1\n",
        b'EXPORT_VERIFIER_VERSION = "a"\nEXPORT_VERIFIER_VERSION = "b"\n',
        b"EXPORT_VERIFIER_VERSION = 1\n",
        b"def (:\n",
    ],
    ids=["none", "two", "not-text", "not-python"],
)
def test_embedded_version_must_be_exactly_one_text(source: bytes) -> None:
    with pytest.raises(VerifierBundleError):
        embedded_version(source)


def test_a_file_of_another_version_or_missing_does_not_travel(tmp_path: Path) -> None:
    other = tmp_path / "otra.py"
    other.write_bytes(
        VERIFIER_FILE.read_bytes().replace(
            f'"{VERIFIER_VERSION}"'.encode(), b'"export-verifier.0"', 1
        )
    )
    assert embedded_version(other.read_bytes()) == "export-verifier.0"
    with pytest.raises(VerifierBundleError):
        load_verifier_bundle(other)
    with pytest.raises(VerifierBundleError):
        load_verifier_bundle(tmp_path / "no-existe.py")


class _Writer:
    def __init__(self) -> None:
        self.added: list[tuple[Artifact, bytes]] = []

    async def add_bytes(self, artifact: Artifact, data: bytes) -> Any:
        self.added.append((artifact, data))
        return artifact


def test_include_writes_the_entry_with_its_bytes() -> None:
    bundle = VerifierBundle.of(b'EXPORT_VERIFIER_VERSION = "export-verifier.1"\n')
    writer = _Writer()
    asyncio.run(bundle.include(cast(ArchiveWriter, writer)))
    ((artifact, data),) = writer.added
    assert data == bundle.data
    assert (artifact.path, artifact.kind, artifact.size_bytes, artifact.compress) == (
        VERIFIER_PATH,
        VERIFIER_KIND,
        len(bundle.data),
        True,
    )


# --- firma del manifiesto (ronda 1, decisión A2) -------------------------------------------------


class _Signer:
    def __init__(self) -> None:
        self.private = Ed25519PrivateKey.from_private_bytes(bytes(range(7, 39)))
        self.purposes: list[SigningPurpose] = []

    def sign(self, purpose: SigningPurpose, payload: Any) -> Any:
        self.purposes.append(purpose)
        signature = self.private.sign(canonical_bytes_sync(payload))
        return SimpleNamespace(key_id="k-1", signature=base64.b64encode(signature).decode())


_DOCUMENT: dict[str, Any] = {
    "export_id": "018f0000-0000-7000-8000-000000000001",
    "organization": {"organization_id": "018f0000-0000-7000-8000-000000000002"},
    "plant": {"plant_id": "018f0000-0000-7000-8000-000000000003"},
    "generated_at": "2026-10-19T00:00:00.000Z",
    "manifest_signature": dict(MANIFEST_SIGNATURE_DECLARATION),
}


def test_sign_manifest_signs_the_manifest_sha256_with_the_checkpoint_key() -> None:
    signer = _Signer()
    data = canonical_bytes_sync(_DOCUMENT)
    document = json.loads(sign_manifest(signer, _DOCUMENT, data))
    assert signer.purposes == [SigningPurpose.CHECKPOINT]
    assert document["key_id"] == "k-1"
    payload = document["payload"]
    assert payload == manifest_signature_payload(_DOCUMENT, data)
    assert payload["manifest_sha256"] == hashlib.sha256(data).hexdigest()
    assert payload["kind"] == "vigia.export.manifest"
    signer.private.public_key().verify(
        base64.b64decode(document["signature"]), canonical_bytes_sync(payload)
    )


def test_sign_manifest_requires_the_declaration_and_the_signature_is_not_an_artifact() -> None:
    undeclared = {key: value for key, value in _DOCUMENT.items() if key != "manifest_signature"}
    with pytest.raises(ValueError):
        sign_manifest(_Signer(), undeclared, canonical_bytes_sync(undeclared))
    with pytest.raises(ValueError):
        Artifact(path="manifest.signature.json", kind="checkpoint", size_bytes=1)
