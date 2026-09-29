"""Verificación de evidencias por metadatos del objeto (LC-NUC-15 parte 1; BR-NUC-64, 65).

Antes de abrir la transacción de escritura (PAT-NUC-REN-06, PAT-NUC-RES-08), el escritor
(TASK-113) llama a ``EvidenceVerifier.verify_references(owner, refs)``. Por cada
``ClipReference`` se comprueba, **sin descargar el clip**:

1. que ``storage_key`` sigue ``org/{o}/plant/{p}/zone/{z}/node/{n}/{clip_id}.{ext}`` y es
   coherente con el registro: organización, planta, zona y nodo del registro, ``clip_id`` de la
   referencia y extensión del ``content_type`` (``mp4`` o ``jpg``). Una clave incoherente no se
   consulta al almacén (no se sondean claves de otro alcance) y cuenta como ``evidence_missing``:
   para este registro no existe objeto en esa clave;
2. que el objeto existe (``HEAD`` con ``ChecksumMode=ENABLED``);
3. que el tamaño y la suma SHA-256 **de objeto entero** que calculó el almacén coinciden con
   ``size_bytes`` y ``sha256``; un objeto sin suma SHA-256 o con suma compuesta no se puede
   verificar y cuenta como ``evidence_hash_mismatch``;
4. que el metadato de usuario ``x-amz-meta-vigia-anonymized`` vale exactamente ``1``.

El primer fallo en ese orden fija el código de la referencia (integridad antes que la marca).
Las consultas de un mismo registro corren **en paralelo**. Si el almacén no está accesible, la
verificación completa termina en ``StorageUnavailable`` (transitorio, ``storage_unavailable``
hacia el nodo): nunca se confunde un almacén caído con una evidencia ausente.

La traducción al contrato cuando el escritor es U-03 (``domain-entities.md`` §3.6) está en
``CONTRACT_REJECTION_CODE``. La inserción de ``Evidence`` es de TASK-113.
"""

from __future__ import annotations

import asyncio
import enum
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol

from vigia_contracts.models.clip_reference import ClipReference, ContentType
from vigia_contracts.models.enumerations import RejectionCode

from vigia_platform.shared.clock import Clock
from vigia_platform.shared.storage import (
    ANONYMIZED_METADATA_KEY,
    ObjectHead,
    StorageUnavailable,
)

__all__ = [
    "ANONYMIZED_VALUE",
    "CODE_BY_FAILURE",
    "CONTRACT_REJECTION_CODE",
    "EXTENSION_BY_CONTENT_TYPE",
    "EvidenceCheck",
    "EvidenceCode",
    "EvidenceFailure",
    "EvidenceOwner",
    "EvidenceVerifier",
    "ObjectHeadReader",
    "StorageKey",
    "StorageKeyInvalid",
    "evaluate_object",
    "first_failure",
    "format_storage_key",
    "parse_storage_key",
    "storage_key_failure",
]

ANONYMIZED_VALUE: Final = "1"
"""Único valor válido de ``x-amz-meta-vigia-anonymized`` (BR-CTR-14, NFR-NUC-33)."""

EXTENSION_BY_CONTENT_TYPE: Final[Mapping[ContentType, str]] = {
    ContentType.VIDEO_MP4: "mp4",
    ContentType.IMAGE_JPEG: "jpg",
}

_UUID: Final = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_STORAGE_KEY: Final = re.compile(
    rf"org/(?P<organization_id>{_UUID})/plant/(?P<plant_id>{_UUID})"
    rf"/zone/(?P<zone_id>{_UUID})/node/(?P<node_id>{_UUID})"
    rf"/(?P<clip_id>{_UUID})\.(?P<ext>mp4|jpg)"
)
"""Patrón de BR-NUC-65, anclado de principio a fin, con UUID canónicos en minúsculas."""


class EvidenceCode(enum.StrEnum):
    """Códigos de ``ledger_rejection_code`` que produce la verificación (BR-NUC-64)."""

    EVIDENCE_MISSING = "evidence_missing"
    EVIDENCE_HASH_MISMATCH = "evidence_hash_mismatch"
    EVIDENCE_NOT_ANONYMIZED = "evidence_not_anonymized"


class EvidenceFailure(enum.StrEnum):
    """Motivo fino del fallo, para diagnóstico y pruebas; nunca se muestra a una persona."""

    STORAGE_KEY_INVALID = "storage_key_invalid"
    STORAGE_KEY_MISMATCH = "storage_key_mismatch"
    OBJECT_ABSENT = "object_absent"
    SIZE_MISMATCH = "size_mismatch"
    CHECKSUM_ABSENT = "checksum_absent"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    MARKER_ABSENT = "marker_absent"
    MARKER_INVALID = "marker_invalid"


CODE_BY_FAILURE: Final[Mapping[EvidenceFailure, EvidenceCode]] = {
    EvidenceFailure.STORAGE_KEY_INVALID: EvidenceCode.EVIDENCE_MISSING,
    EvidenceFailure.STORAGE_KEY_MISMATCH: EvidenceCode.EVIDENCE_MISSING,
    EvidenceFailure.OBJECT_ABSENT: EvidenceCode.EVIDENCE_MISSING,
    EvidenceFailure.SIZE_MISMATCH: EvidenceCode.EVIDENCE_HASH_MISMATCH,
    EvidenceFailure.CHECKSUM_ABSENT: EvidenceCode.EVIDENCE_HASH_MISMATCH,
    EvidenceFailure.CHECKSUM_MISMATCH: EvidenceCode.EVIDENCE_HASH_MISMATCH,
    EvidenceFailure.MARKER_ABSENT: EvidenceCode.EVIDENCE_NOT_ANONYMIZED,
    EvidenceFailure.MARKER_INVALID: EvidenceCode.EVIDENCE_NOT_ANONYMIZED,
}

CONTRACT_REJECTION_CODE: Final[Mapping[EvidenceCode, RejectionCode]] = {
    EvidenceCode.EVIDENCE_MISSING: RejectionCode.CLIP_MISSING,
    EvidenceCode.EVIDENCE_HASH_MISMATCH: RejectionCode.CLIP_HASH_MISMATCH,
    EvidenceCode.EVIDENCE_NOT_ANONYMIZED: RejectionCode.CLIP_NOT_ANONYMIZED,
}
"""Traducción al ``RejectionResponse`` del contrato cuando escribe U-03 (§3.6)."""


# --- Clave de almacenamiento (BR-NUC-65, PR-NUC-23) -------------------------------------------


class StorageKeyInvalid(ValueError):
    """La clave no sigue el patrón de BR-NUC-65."""

    def __init__(self) -> None:
        super().__init__("storage_key no sigue org/{o}/plant/{p}/zone/{z}/node/{n}/{clip}.{ext}")


@dataclass(frozen=True, slots=True)
class EvidenceOwner:
    """Organización, planta, zona y nodo del registro que referencia las evidencias."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    node_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class StorageKey:
    """Una ``storage_key`` descompuesta."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    node_id: uuid.UUID
    clip_id: uuid.UUID
    ext: str

    @property
    def owner(self) -> EvidenceOwner:
        return EvidenceOwner(self.organization_id, self.plant_id, self.zone_id, self.node_id)


def parse_storage_key(key: str) -> StorageKey:
    """``storage_key`` → ``StorageKey``; ``StorageKeyInvalid`` si no sigue el patrón."""
    match = _STORAGE_KEY.fullmatch(key) if isinstance(key, str) else None
    if match is None:
        raise StorageKeyInvalid()
    return StorageKey(
        organization_id=uuid.UUID(match["organization_id"]),
        plant_id=uuid.UUID(match["plant_id"]),
        zone_id=uuid.UUID(match["zone_id"]),
        node_id=uuid.UUID(match["node_id"]),
        clip_id=uuid.UUID(match["clip_id"]),
        ext=match["ext"],
    )


def format_storage_key(key: StorageKey) -> str:
    """``StorageKey`` → texto; ``format(parse(k)) == k`` para toda clave válida (PR-NUC-23)."""
    if key.ext not in EXTENSION_BY_CONTENT_TYPE.values():
        raise StorageKeyInvalid()
    return (
        f"org/{key.organization_id}/plant/{key.plant_id}/zone/{key.zone_id}"
        f"/node/{key.node_id}/{key.clip_id}.{key.ext}"
    )


def storage_key_failure(owner: EvidenceOwner, reference: ClipReference) -> EvidenceFailure | None:
    """``None`` si ``storage_key`` sigue el patrón y es la del registro, el clip y su tipo."""
    try:
        parsed = parse_storage_key(reference.storage_key)
    except StorageKeyInvalid:
        return EvidenceFailure.STORAGE_KEY_INVALID
    coherent = (
        parsed.owner == owner
        and str(parsed.clip_id) == reference.clip_id
        and parsed.ext == EXTENSION_BY_CONTENT_TYPE[ContentType(reference.content_type)]
    )
    return None if coherent else EvidenceFailure.STORAGE_KEY_MISMATCH


# --- Comparación con el objeto (BR-NUC-64, PR-NUC-22) ------------------------------------------


def evaluate_object(reference: ClipReference, head: ObjectHead | None) -> EvidenceFailure | None:
    """Primer fallo del objeto frente a la referencia, o ``None`` si coincide todo."""
    if head is None:
        return EvidenceFailure.OBJECT_ABSENT
    if head.size_bytes != reference.size_bytes:
        return EvidenceFailure.SIZE_MISMATCH
    stored = head.full_object_sha256_hex
    if stored is None:
        return EvidenceFailure.CHECKSUM_ABSENT
    if stored != reference.sha256:
        return EvidenceFailure.CHECKSUM_MISMATCH
    marker = head.metadata.get(ANONYMIZED_METADATA_KEY)
    if marker is None:
        return EvidenceFailure.MARKER_ABSENT
    if marker != ANONYMIZED_VALUE:
        return EvidenceFailure.MARKER_INVALID
    return None


@dataclass(frozen=True, slots=True)
class EvidenceCheck:
    """Resultado de verificar una referencia; ``code is None`` si pasó."""

    reference: ClipReference
    failure: EvidenceFailure | None
    head: ObjectHead | None
    verified_at: datetime

    @property
    def ok(self) -> bool:
        return self.failure is None

    @property
    def code(self) -> EvidenceCode | None:
        return None if self.failure is None else CODE_BY_FAILURE[self.failure]


def first_failure(checks: Iterable[EvidenceCheck]) -> EvidenceCheck | None:
    """La primera comprobación fallida en el orden de las referencias, o ``None``."""
    return next((check for check in checks if not check.ok), None)


class ObjectHeadReader(Protocol):
    """La parte de ``StoragePort`` que usa la verificación: solo ``head_object``."""

    async def head_object(self, key: str) -> ObjectHead | None: ...


class EvidenceVerifier:
    """``verify_references`` para el escritor (LC-NUC-10), sobre el depósito de evidencias."""

    def __init__(self, storage: ObjectHeadReader, clock: Clock) -> None:
        self._storage = storage
        self._clock = clock

    async def verify_references(
        self, owner: EvidenceOwner, refs: Sequence[ClipReference]
    ) -> list[EvidenceCheck]:
        """Una comprobación por referencia, en el mismo orden; los ``HEAD`` van en paralelo.

        ``StorageUnavailable`` si alguna consulta no pudo hacerse: la verificación entera es
        transitoria y el escritor no abre la transacción.
        """
        key_failures = [storage_key_failure(owner, reference) for reference in refs]
        pending = [
            self._storage.head_object(reference.storage_key)
            for reference, failure in zip(refs, key_failures, strict=True)
            if failure is None
        ]
        results = await asyncio.gather(*pending, return_exceptions=True)
        for result in results:
            if isinstance(result, StorageUnavailable):
                raise result
        for result in results:
            if isinstance(result, BaseException):
                raise result
        heads = iter(results)
        verified_at = self._clock.now()
        checks: list[EvidenceCheck] = []
        for reference, key_failure in zip(refs, key_failures, strict=True):
            if key_failure is not None:
                checks.append(EvidenceCheck(reference, key_failure, None, verified_at))
                continue
            head = next(heads)
            if isinstance(head, BaseException):  # ya relanzado arriba; aquí solo estrecha el tipo
                raise head
            checks.append(
                EvidenceCheck(reference, evaluate_object(reference, head), head, verified_at)
            )
        return checks
