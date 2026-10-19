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
- ``sign_manifest(signer, document, data)``: el **ancla** del paquete (decisión del dueño A2,
  2026-10-19). Firma con la clave ``checkpoint`` de ``SigningService`` (la misma y el mismo
  conjunto de claves que los puntos de control) el mensaje
  ``{kind: "vigia.export.manifest", export_id, organization_id, plant_id, manifest_sha256,
  generated_at}``, con el SHA-256 de los bytes canónicos del manifiesto, y devuelve el contenido de
  ``manifest.signature.json`` (``{payload, key_id, signature}``). El manifiesto lo declara en
  ``manifest_signature``. Como el manifiesto lista el hash de cada artefacto, la firma ata todo el
  paquete. El mensaje no lleva la hora de la firma sino ``generated_at``: Ed25519 es
  determinista y una retoma con la misma clave produce los mismos bytes (PR-LAZ-49).
"""

from __future__ import annotations

import ast
import functools
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from vigia_platform.accreditation.export.stream import ArchiveWriter, Artifact, WrittenArtifact
from vigia_platform.accreditation.export_verifier import (
    EXPORT_VERIFIER_NAME,
    EXPORT_VERIFIER_VERSION,
    MANIFEST_SIGNATURE_DECLARATION,
    MANIFEST_SIGNATURE_KIND,
)
from vigia_platform.ledger.canonical import canonical_bytes_sync
from vigia_platform.shared.signing.keys import SigningPurpose

__all__ = [
    "MANIFEST_SIGNATURE_DECLARATION",
    "VERIFIER_FILE",
    "VERIFIER_KIND",
    "VERIFIER_PATH",
    "VERIFIER_VERSION",
    "ManifestSigner",
    "VerifierBundle",
    "VerifierBundleError",
    "embedded_version",
    "load_verifier_bundle",
    "manifest_signature_payload",
    "sign_manifest",
]


class ManifestSigner(Protocol):
    """``SigningService.sign`` (la clave ``checkpoint``): devuelve un sobre con ``key_id`` y
    ``signature`` (Ed25519 en base64 del canónico RFC 8785 de ``payload``)."""

    def sign(self, purpose: SigningPurpose, payload: Any) -> Any: ...


def manifest_signature_payload(document: Mapping[str, Any], data: bytes) -> dict[str, Any]:
    """El mensaje firmado del manifiesto ``document`` cuyos bytes canónicos son ``data``."""
    return {
        "kind": MANIFEST_SIGNATURE_KIND,
        "export_id": document["export_id"],
        "organization_id": document["organization"]["organization_id"],
        "plant_id": document["plant"]["plant_id"],
        "manifest_sha256": hashlib.sha256(data).hexdigest(),
        "generated_at": document["generated_at"],
    }


def sign_manifest(signer: ManifestSigner, document: Mapping[str, Any], data: bytes) -> bytes:
    """El contenido de ``manifest.signature.json`` (decisión A2): ``{payload, key_id, signature}``.

    Falla cerrado: sin clave ``checkpoint`` activa, ``sign`` lanza y el paquete no se publica.
    """
    if document.get("manifest_signature") != MANIFEST_SIGNATURE_DECLARATION:
        raise ValueError("el manifiesto debe declarar la firma que lo acompaña")
    payload = manifest_signature_payload(document, data)
    envelope = signer.sign(SigningPurpose.CHECKPOINT, payload)
    return canonical_bytes_sync(
        {"payload": payload, "key_id": envelope.key_id, "signature": envelope.signature}
    )


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
