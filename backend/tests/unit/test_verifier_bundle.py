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
import hashlib
from pathlib import Path
from typing import Any, cast

import pytest

from vigia_platform.accreditation.export.stream import ArchiveWriter, Artifact
from vigia_platform.accreditation.export_verifier import EXPORT_VERIFIER_VERSION
from vigia_platform.accreditation.verifier_bundle import (
    VERIFIER_FILE,
    VERIFIER_KIND,
    VERIFIER_PATH,
    VERIFIER_VERSION,
    VerifierBundle,
    VerifierBundleError,
    embedded_version,
    load_verifier_bundle,
)


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
