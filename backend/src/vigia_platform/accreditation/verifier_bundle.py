"""El verificador dentro de cada paquete exportado (TASK-346; LC-LAZ-25; PAT-LAZ-SEG-02; BR-LAZ-90;
NFR-LAZ-23 y 53).

- **Paso de construcción**: ``tools/build_verifier.py`` compone el verificador de U-02 (LC-NUC-18)
  con el endurecimiento de U-04 (``accreditation.export_verifier``) en un solo archivo Python 3.10
  sin dependencias, ``backend/resources/verifier/vigia_verify.py``, versionado y comprobado con
  ``--check`` como ``tools/vigia_verify.py``. Llega a la imagen con ``backend/resources``.
- ``load_verifier_bundle()``: lee ese archivo, saca la versión **embebida** en él
  (``EXPORT_VERIFIER_VERSION``, leída de su árbol sintáctico, sin ejecutarlo) y exige que sea la
  de esta versión de la plataforma: un archivo sin regenerar no viaja. Es lo que el manifiesto
  declara en ``verifier_version`` (igualdad, NFR-LAZ-53).
- ``VerifierBundle.include(writer)``: escribe la entrada ``vigia_verify.py`` (tipo ``verifier``)
  en el paquete; el manifiesto la lista con su hash como cualquier otro artefacto, y la entrada
  ausente «verifier» que TASK-318 dejaba declarada deja de existir.
"""

from __future__ import annotations

import ast
import functools
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from vigia_platform.accreditation.export.stream import ArchiveWriter, Artifact, WrittenArtifact
from vigia_platform.accreditation.export_verifier import (
    EXPORT_VERIFIER_NAME,
    EXPORT_VERIFIER_VERSION,
)

__all__ = [
    "VERIFIER_FILE",
    "VERIFIER_KIND",
    "VERIFIER_PATH",
    "VERIFIER_VERSION",
    "VerifierBundle",
    "VerifierBundleError",
    "embedded_version",
    "load_verifier_bundle",
]

VERIFIER_PATH: Final = EXPORT_VERIFIER_NAME
"""Ruta de la entrada dentro del paquete (en la raíz de ``vol-NN.zip``)."""
VERIFIER_KIND: Final = "verifier"
VERIFIER_VERSION: Final = EXPORT_VERIFIER_VERSION
"""La versión que declara el manifiesto: la del módulo que se compone en el archivo."""
BACKEND: Final = Path(__file__).resolve().parents[3]
VERIFIER_FILE: Final = BACKEND / "resources" / "verifier" / VERIFIER_PATH
"""``backend/resources/verifier/vigia_verify.py``: lo genera ``tools/build_verifier.py``."""

_VERSION_NAME: Final = "EXPORT_VERIFIER_VERSION"


class VerifierBundleError(RuntimeError):
    """El verificador empaquetado falta o no es el de esta versión de la plataforma."""


def embedded_version(source: bytes) -> str:
    """La versión que lleva el archivo (``EXPORT_VERIFIER_VERSION = "..."`` de primer nivel)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as error:
        raise VerifierBundleError(f"el verificador no es Python válido: {error}") from None
    found: list[str] = []
    for node in tree.body:
        target: ast.expr | None = node.target if isinstance(node, ast.AnnAssign) else None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        value = node.value if isinstance(node, ast.Assign | ast.AnnAssign) else None
        if (
            isinstance(target, ast.Name)
            and target.id == _VERSION_NAME
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            found.append(value.value)
    if len(found) != 1:
        raise VerifierBundleError("el verificador no declara una sola versión")
    return found[0]


@dataclass(frozen=True, slots=True)
class VerifierBundle:
    """Los bytes de ``vigia_verify.py``, su versión embebida y su SHA-256."""

    data: bytes
    version: str
    sha256: str

    @classmethod
    def of(cls, data: bytes) -> VerifierBundle:
        digest = hashlib.sha256(data).hexdigest()
        return cls(data=data, version=embedded_version(data), sha256=digest)

    def artifact(self) -> Artifact:
        return Artifact(
            path=VERIFIER_PATH, kind=VERIFIER_KIND, size_bytes=len(self.data), compress=True
        )

    async def include(self, writer: ArchiveWriter) -> WrittenArtifact:
        """Escribe ``vigia_verify.py`` en el paquete (``incluir(paquete)`` de LC-LAZ-25)."""
        return await writer.add_bytes(self.artifact(), self.data)


@functools.cache
def _load(path: Path) -> VerifierBundle:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise VerifierBundleError(f"no se puede leer el verificador {path}: {error}") from None
    return VerifierBundle.of(data)


def load_verifier_bundle(path: Path = VERIFIER_FILE) -> VerifierBundle:
    """El verificador empaquetado; falla si su versión no es ``VERIFIER_VERSION``."""
    bundle = _load(path)
    if bundle.version != VERIFIER_VERSION:
        raise VerifierBundleError(
            f"el verificador empaquetado es {bundle.version!r} y la plataforma declara "
            f"{VERIFIER_VERSION!r}: ejecuta `uv run python tools/build_verifier.py`"
        )
    return bundle
