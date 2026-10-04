"""``VerificationClip``: el clip de verificación del difuminado de una zona (nº 32; DE §3.13).

Nota del 2026-09-23 de ``domain-entities.md`` §3.13 y texto canónico del nº 32 (§2.6): el nodo
sube en ``commissioning`` un clip difuminado de 10 s sin hallazgo con ``purpose = verification`` y
lo confirma con ``POST clip-uploads/{clip_id}/confirmation``; U-03 lo guarda como
``VerificationClip {clip_id, zone_id, node_id, received_at, sha256, blur_check_result}`` con la
misma retención que la evidencia (⛓: nada se borra) y responde ``VerificationClipReceipt
{clip_id, zone_id, node_id, received_at, sha256}``.

**Verificación por metadatos** (NFR-GOB-09, PAT-GOB-REN-05; ``check_object``): solo con lo que
devuelve ``head_object`` con ``ChecksumMode=ENABLED``; el clip **nunca** se descarga. Orden de
las causas: sin objeto → ``clip_missing``; más grande que lo concedido → ``clip_too_large``; otro
tamaño, otro tipo u otra SHA-256 de objeto entero → ``clip_hash_mismatch``; metadato
``vigia-anonymized`` distinto de ``1`` → ``clip_not_anonymized``. Son rechazos permanentes.

``blur_check_result`` nace **nulo**: sin descargar el clip solo se comprueban la suma y el
metadato (la marca del contenedor MP4 la muestrea U-02 a diario) y la comprobación automática del
difuminado corre en la guarda de cierre del acta (TASK-216), que lo cierra de nulo a valor.
``first_served_at`` (``gob_0021``) es la primera vez que la consola obtuvo el clip (tramo 3b de
NFR-GOB-70); tampoco cambia después.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from vigia_platform.fleet.domain.clip_upload_grant import ANONYMIZED_VALUE, ClipUploadGrant

__all__ = [
    "ANONYMIZED_METADATA_KEY",
    "ClipCheckFailure",
    "ObjectFacts",
    "VerificationClip",
    "check_object",
]

ANONYMIZED_METADATA_KEY = "vigia-anonymized"
"""Nombre del metadato de usuario tal como lo devuelve ``head_object`` (sin ``x-amz-meta-``)."""


class ClipCheckFailure(enum.StrEnum):
    """Causa permanente de rechazo de un clip; los valores son ``rejection_code`` del contrato."""

    CLIP_MISSING = "clip_missing"
    CLIP_HASH_MISMATCH = "clip_hash_mismatch"
    CLIP_TOO_LARGE = "clip_too_large"
    CLIP_NOT_ANONYMIZED = "clip_not_anonymized"


@dataclass(frozen=True, slots=True)
class ObjectFacts:
    """Lo que ``head_object`` dice de un objeto (sin descargarlo)."""

    size_bytes: int
    sha256_hex: str | None
    """SHA-256 del objeto **entero** que guardó el almacén; ``None`` sin suma o compuesta."""
    content_type: str | None
    metadata: Mapping[str, str]


def check_object(grant: ClipUploadGrant, facts: ObjectFacts | None) -> ClipCheckFailure | None:
    """La causa por la que el objeto no es el concedido, o ``None`` si lo es."""
    if facts is None:
        return ClipCheckFailure.CLIP_MISSING
    if facts.size_bytes > grant.max_size_bytes:
        return ClipCheckFailure.CLIP_TOO_LARGE
    if (
        facts.size_bytes != grant.max_size_bytes
        or facts.content_type != grant.content_type.value
        or facts.sha256_hex is None
        or facts.sha256_hex != grant.sha256
    ):
        return ClipCheckFailure.CLIP_HASH_MISMATCH
    if facts.metadata.get(ANONYMIZED_METADATA_KEY) != ANONYMIZED_VALUE:
        return ClipCheckFailure.CLIP_NOT_ANONYMIZED
    return None


@dataclass(frozen=True, slots=True)
class VerificationClip:
    """Nota de §3.13 ``VerificationClip`` ⛓ (tabla ``fleet.verification_clip``)."""

    clip_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    node_id: uuid.UUID
    received_at: datetime
    sha256: str
    blur_check_result: Mapping[str, Any] | None = None
    first_served_at: datetime | None = None

    @classmethod
    def confirmed(cls, grant: ClipUploadGrant, received_at: datetime) -> VerificationClip:
        """El clip de una concesión ``verification`` cuyo objeto pasó ``check_object``."""
        return cls(
            clip_id=grant.clip_id,
            organization_id=grant.organization_id,
            plant_id=grant.plant_id,
            zone_id=grant.zone_id,
            node_id=grant.node_id,
            received_at=received_at,
            sha256=grant.sha256,
        )
