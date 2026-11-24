"""Versión 2 congelada de ``catalog_version_published``: el sobre del contrato v1.5.0 (TASK-593).

La versión 2 del tipo (VIG-457) guardaba el sobre firmado tal como lo definía el contrato v1.5.0
y la plataforma la persistió en ``ledger.record_type`` con ese esquema. v1.6.0 (A-129) añade al
catálogo el bloque opcional ``process_context``: el esquema del sobre crece y el modelo vigente ya
no es el de la versión 2. Sin congelarla, una base anterior no arrancaría
(``RegistryStartupError``, como en VIG-457) y el registro rechazaría la descripción de las
variables como texto libre no declarado.

Este módulo es una **instantánea en el código** del ``ZoneCatalog`` y del ``SignedEnvelope`` de
v1.5.0: los mismos campos, ``$defs`` y restricciones, sin ``process_context``. Las piezas anidadas
que v1.6.0 no cambió (cámaras, cobertura, señales, estándares, umbrales, ventana y episodio) se
toman del contrato vigente; ``tests/unit/test_record_types_frozen_v1_5.py`` compara el esquema con
el que persistía la plataforma con v1.5.0 (``tests/fixtures/record_types/``), así que un cambio
futuro de esas piezas no pasa inadvertido. ``CatalogChangedField`` es la copia congelada de la
versión 1 (ocho valores).

Sirve para **presentar** los registros históricos v2 (``registry.get(tipo, schema_version=2)``);
los escritores escriben siempre la última versión (``CatalogVersionPublished``, versión 3). Las
reglas entre campos del contrato no se repiten aquí: esos registros ya se validaron al escribirse
y el contenido del expediente nunca cambia (P4).
"""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import Field, StrictInt, StrictStr, model_validator
from vigia_contracts.models._base import StrictTrue
from vigia_contracts.models.common import UUID, Code, KeyId, Sha256Hex, Timestamp
from vigia_contracts.models.declared_standard import DeclaredStandard
from vigia_contracts.models.zone_catalog import (
    CatalogCamera,
    ClipWindow,
    EpisodeParameters,
    MinimumCoverage,
    SignalDeclaration,
    Thresholds,
)

from vigia_platform.catalog.record_types_frozen import CatalogChangedField
from vigia_platform.ledger.registry import ContentModel

__all__ = ["CatalogVersionPublishedV2"]


class ZoneCatalog(ContentModel):
    """``ZoneCatalog`` de v1.5.0: el de v1.6.0 sin ``process_context``."""

    zone_id: UUID
    zone_code: Code
    plant_id: UUID
    organization_id: UUID
    version: Annotated[StrictInt, Field(ge=1, le=2147483647)]
    issued_at: Timestamp
    cameras: Annotated[list[CatalogCamera], Field(max_length=8, min_length=1)]
    minimum_coverage: MinimumCoverage
    signals: Annotated[list[SignalDeclaration], Field(max_length=32, min_length=0)]
    standards: Annotated[list[DeclaredStandard], Field(max_length=32, min_length=1)]
    thresholds: Thresholds
    clip_window: ClipWindow
    episode: EpisodeParameters
    commissioning_watermark: StrictTrue


type Payload = ZoneCatalog
"""La carga del sobre (el mismo nombre de ``$defs`` que en el contrato)."""


class SignedEnvelope(ContentModel):
    """``SignedEnvelope<ZoneCatalog>`` de v1.5.0."""

    payload: Payload
    payload_canonical_sha256: Sha256Hex
    signature: Annotated[
        StrictStr, Field(max_length=88, min_length=88, pattern="^[A-Za-z0-9+/]{86}==$")
    ]
    key_id: KeyId
    signed_at: Timestamp


_UUID_PATTERN = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


class CatalogVersionPublishedV2(ContentModel):
    """``catalog_version_published`` en su versión 2: el sobre del contrato v1.5.0."""

    source_key: Annotated[
        StrictStr,
        Field(min_length=38, max_length=47, pattern=f"^{_UUID_PATTERN}:[1-9][0-9]{{0,9}}$"),
    ]
    zone_id: UUID
    catalog_version: Annotated[StrictInt, Field(ge=1, le=2**31 - 1)]
    changed_fields: Annotated[tuple[CatalogChangedField, ...], Field(min_length=1, max_length=8)]
    reason_es: Annotated[StrictStr, Field(min_length=10, max_length=500)]
    envelope: SignedEnvelope

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.source_key != f"{self.zone_id}:{self.catalog_version}":
            raise ValueError("source_key debe ser <zone_id>:<catalog_version>")
        if len(set(self.changed_fields)) != len(self.changed_fields):
            raise ValueError("changed_fields no admite valores repetidos")
        payload = self.envelope.payload
        if payload.zone_id != self.zone_id or payload.version != self.catalog_version:
            raise ValueError("el sobre firmado es de otra zona o de otra versión del catálogo")
        return self
