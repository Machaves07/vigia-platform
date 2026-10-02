"""Tipos de registro propios de U-02 (domain-entities §3.3, registro inicial).

Catorce tipos, todos escritos solo por U-02 y en su versión 1. El contenido solo lleva
identificadores, códigos, listas cerradas, marcas y hashes; el único texto libre son los
nombres de organización, planta y zona, la zona horaria de la planta y el motivo de una
concesión, declarados en ``free_text_paths`` (BR-NUC-51). Ninguno tiene clave de
idempotencia ni rutas de evidencia ni regla de etiqueta. Los tipos de U-03 y U-04 los
registran esas unidades.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import Field, StrictInt, StrictStr
from vigia_contracts.models.common import UUID, Code, Sha256Hex, Timestamp

from vigia_platform.ledger.registry import ChainLevel, ContentModel, RecordType
from vigia_platform.shared.context import ActorUnit

__all__ = ["U02_RECORD_TYPES"]

Name = Annotated[StrictStr, Field(min_length=1, max_length=120)]
"""Nombre de organización, planta o zona: texto libre ≤ 120 (domain-entities §2)."""

ConcessionDays = Annotated[StrictInt, Field(ge=1, le=90)]
Sequence64 = Annotated[StrictInt, Field(ge=1, le=2**63 - 1)]
Count64 = Annotated[StrictInt, Field(ge=0, le=2**63 - 1)]

CountryCode = Annotated[StrictStr, Field(min_length=2, max_length=2, pattern=r"^[A-Z]{2}$")]
"""ISO 3166-1 alfa-2."""

DataRegion = Annotated[
    StrictStr, Field(min_length=9, max_length=32, pattern=r"^[a-z]{2}(-[a-z]+)+-[0-9]$")
]
"""Valor de la lista ``DataRegion`` (inicial ``us-east-1``)."""

TimeZone = Annotated[
    StrictStr,
    Field(min_length=3, max_length=64, pattern=r"^(UTC|[A-Z][A-Za-z_]+(/[A-Za-z0-9_+-]+){1,2})$"),
]
"""Zona IANA (``America/Bogota``, ``America/Argentina/Buenos_Aires``, ``UTC``). Mezcla
mayúsculas y minúsculas, así que cuenta como texto libre y se declara en ``free_text_paths``:
pasa además la política de texto libre."""

Ed25519Signature = Annotated[
    StrictStr, Field(min_length=88, max_length=88, pattern=r"^[A-Za-z0-9+/]{86}==$")
]
"""Firma Ed25519 (64 bytes) en base64 estándar."""

Ed25519PublicKey = Annotated[
    StrictStr, Field(min_length=44, max_length=44, pattern=r"^[A-Za-z0-9+/]{43}=$")
]
"""Clave pública Ed25519 (32 bytes) en base64 estándar."""

RouteTemplate = Annotated[
    StrictStr, Field(min_length=1, max_length=256, pattern=r"^/[a-z0-9_{}/.-]{0,255}$")
]
"""Plantilla de ruta de la API (``/api/v1/zones/{zone_id}``), nunca la URL concreta; las rutas
de la plataforma van en minúsculas."""

PlatformKeyId = Annotated[
    StrictStr, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_.:-]{0,63}$")
]
"""Identificador de una clave de la plataforma: el ``KeyId`` del contrato restringido a
minúsculas. La plataforma genera sus identificadores; un ``KeyId`` con mayúsculas y minúsculas
admitiría un nombre pegado y contaría como texto libre (``schema_rules.is_free_text``)."""

AuditPartition = Annotated[
    StrictStr,
    Field(
        min_length=26,
        max_length=26,
        pattern=r"^shared\.audit_entry_[0-9]{4}_(0[1-9]|1[0-2])$",
    ),
]
"""``shared.audit_entry_AAAA_MM``: 26 caracteres justos (el patrón es de ancho fijo). Hasta
TASK-131 decía 25 y ningún registro ``audit_partition_archived`` podía escribirse."""
MonthPeriod = Annotated[
    StrictStr, Field(min_length=7, max_length=7, pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")
]
ObjectKey = Annotated[
    StrictStr, Field(min_length=1, max_length=512, pattern=r"^[a-z0-9][a-z0-9/_.-]{0,511}$")
]

ConcessionReason = Annotated[StrictStr, Field(min_length=10, max_length=500)]
"""Motivo de la concesión: texto libre de 10 a 500 (domain-entities §2.11)."""

SigningPurpose = Literal["catalog", "gate", "live_view_token", "key_set", "checkpoint"]


# --- identidad y tenencia ----------------------------------------------------------------


class OrganizationCreated(ContentModel):
    """Génesis de la cadena de organización (respuesta 7)."""

    organization_id: UUID
    code: Code
    name: Name
    kind: Literal["client", "provider"]
    concession_max_days: ConcessionDays
    concession_default_days: ConcessionDays
    created_by: UUID


class PlantCreated(ContentModel):
    """Génesis de la cadena de planta."""

    plant_id: UUID
    code: Code
    name: Name
    country: CountryCode
    data_region: DataRegion
    timezone: TimeZone
    created_by: UUID


class ZoneCreated(ContentModel):
    zone_id: UUID
    plant_id: UUID
    code: Code
    name: Name
    created_by: UUID


class NodeDeclared(ContentModel):
    node_id: UUID
    plant_id: UUID
    code: Code
    declared_by: UUID


class NodeZoneAssigned(ContentModel):
    assignment_id: UUID
    zone_id: UUID
    node_id: UUID
    assigned_at: Timestamp
    assigned_by: UUID


class NodeZoneUnassigned(ContentModel):
    assignment_id: UUID
    zone_id: UUID
    node_id: UUID
    unassigned_at: Timestamp
    unassigned_by: UUID


# --- concesiones del proveedor (respuesta 6) ---------------------------------------------


class ProviderConcessionGranted(ContentModel):
    concession_id: UUID
    provider_organization_id: UUID
    provider_user_id: UUID
    scope_level: Literal["organization", "plant"]
    scope_id: UUID
    reason: ConcessionReason
    granted_at: Timestamp
    expires_at: Timestamp


class ProviderConcessionRevoked(ContentModel):
    concession_id: UUID
    revoked_at: Timestamp
    revoked_by: UUID
    revoked_by_side: Literal["client", "provider"]


class ProviderConcessionExpired(ContentModel):
    concession_id: UUID
    expires_at: Timestamp
    recorded_at: Timestamp


class ProviderQuery(ContentModel):
    """Una petición del proveedor que leyó o escribió datos del cliente (BR-NUC-38)."""

    concession_id: UUID
    operation: Literal["read", "write"]
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    resource: RouteTemplate
    occurred_at: Timestamp


# --- integridad y claves ------------------------------------------------------------------


class Checkpoint(ContentModel):
    """Punto de control diario firmado (domain-entities §3.4, BR-NUC-53)."""

    covered_sequence: Sequence64
    covered_hash: Sha256Hex
    taken_at: Timestamp
    key_id: PlatformKeyId
    signature: Ed25519Signature


class KeyRotated(ContentModel):
    """Rotación de una clave de firma de la organización proveedora (respuesta 11)."""

    key_id: PlatformKeyId
    purpose: SigningPurpose
    public_key: Ed25519PublicKey
    previous_key_id: PlatformKeyId | None = None
    valid_from: Timestamp
    rotated_by: UUID


class KeySetPublished(ContentModel):
    """Publicación del conjunto de claves para nodos (BR-NUC-86)."""

    publication_id: UUID
    issued_at: Timestamp
    signed_by_key_id: PlatformKeyId
    key_ids: Annotated[tuple[PlatformKeyId, ...], Field(min_length=1, max_length=16)]


class AuditPartitionArchived(ContentModel):
    """Partición de auditoría archivada y verificada antes de desprenderla (PAT-NUC-RES)."""

    partition_name: AuditPartition
    period: MonthPeriod
    archive_object_key: ObjectKey
    archive_sha256: Sha256Hex
    entry_count: Count64
    archived_at: Timestamp


def _u02(
    record_type: str,
    chain_level: ChainLevel,
    model: type[ContentModel],
    *,
    free_text_paths: tuple[str, ...] = (),
    outbox_events: tuple[str, ...] = (),
    chain_follows_scope: bool = False,
) -> RecordType:
    return RecordType(
        record_type=record_type,
        writer_unit=ActorUnit.U02,
        chain_level=chain_level,
        schema_version=1,
        content_model=model,
        free_text_paths=free_text_paths,
        outbox_events=outbox_events,
        chain_follows_scope=chain_follows_scope,
    )


_ORG = ChainLevel.ORGANIZATION
_PLANT = ChainLevel.PLANT

U02_RECORD_TYPES: Final[tuple[RecordType, ...]] = (
    _u02("organization_created", _ORG, OrganizationCreated, free_text_paths=("/name",)),
    _u02("plant_created", _PLANT, PlantCreated, free_text_paths=("/name", "/timezone")),
    _u02("zone_created", _PLANT, ZoneCreated, free_text_paths=("/name",)),
    _u02("node_declared", _PLANT, NodeDeclared),
    _u02("node_zone_assigned", _PLANT, NodeZoneAssigned),
    _u02("node_zone_unassigned", _PLANT, NodeZoneUnassigned),
    _u02(
        "provider_concession_granted",
        _ORG,
        ProviderConcessionGranted,
        free_text_paths=("/reason",),
        outbox_events=("concession_granted",),
        chain_follows_scope=True,
    ),
    _u02(
        "provider_concession_revoked",
        _ORG,
        ProviderConcessionRevoked,
        outbox_events=("concession_revoked",),
        chain_follows_scope=True,
    ),
    _u02(
        "provider_concession_expired",
        _ORG,
        ProviderConcessionExpired,
        outbox_events=("concession_expired",),
        chain_follows_scope=True,
    ),
    _u02("provider_query", _ORG, ProviderQuery, chain_follows_scope=True),
    _u02(
        "checkpoint",
        _ORG,
        Checkpoint,
        outbox_events=("checkpoint_written",),
        chain_follows_scope=True,
    ),
    _u02("key_rotated", _ORG, KeyRotated),
    _u02("key_set_published", _ORG, KeySetPublished, outbox_events=("key_set_published",)),
    _u02("audit_partition_archived", _ORG, AuditPartitionArchived),
)
"""Los catorce tipos de U-02, en versión 1."""
