"""Lecturas de la composición de una exportación sobre PostgreSQL y el almacén (TASK-318).

Lo que ``accreditation.export.contents`` necesita y no es un puerto de U-02 ni de U-03:

- ``SqlExportPlaces.places``: la organización, la planta y las zonas pedidas (código y nombre),
  en una sentencia con el contexto de la organización (la seguridad a nivel de fila limita a
  ella). Una zona que no es de la planta no aparece y el compositor no compone.
- ``SqlCheckpointLookup``: los puntos de control **fijados en el primer intento**, por su
  identificador (registro ``checkpoint`` del expediente o entrada ``checkpoint`` de auditoría), y
  las claves públicas ``checkpoint`` de ``CheckpointService`` (vigentes y retiradas, BR-NUC-55).
  Leer por identificador, y no el último punto de control de cada cadena, hace que una retoma
  incruste exactamente los mismos (U04-H-08).
- ``StorageBlobs``: los bytes de un clip (su clave en ``ledger.evidence``) o de un adjunto de
  cierre (la clave de BR-LAZ-44), leídos del depósito de evidencias. El compositor los compara
  con el SHA-256 declarado antes de escribirlos.

Todas leen con el ``ScopeContext`` de la entrega, en transacciones de solo lectura.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Final, Protocol

from sqlalchemy import text

from vigia_platform.accreditation.export.contents import Places
from vigia_platform.accreditation.manifest.model import OrganizationRef, PlantRef
from vigia_platform.ledger.application.reader import EvidenceReference
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.ledger.chain.checkpoints import (
    ChainKind,
    CheckpointChain,
    CheckpointContent,
    CheckpointPublicKey,
    StoredCheckpoint,
)
from vigia_platform.shared.context import ScopeContext, repository

__all__ = ["SqlCheckpointLookup", "SqlExportPlaces", "StorageBlobs"]

_PLACES: Final = text(
    "SELECT o.code AS organization_code, o.name AS organization_name, p.code AS plant_code,"
    " p.name AS plant_name, p.country, p.timezone, z.zone_id, z.code AS zone_code,"
    " z.name AS zone_name"
    " FROM identity.organization AS o"
    " JOIN identity.plant AS p ON p.organization_id = o.organization_id"
    " JOIN identity.zone AS z ON z.organization_id = p.organization_id AND z.plant_id = p.plant_id"
    " WHERE o.organization_id = :organization_id AND p.plant_id = :plant_id"
    " AND z.zone_id = ANY(CAST(:zone_ids AS uuid[]))"
)
_LEDGER_CHECKPOINTS: Final = text(
    "SELECT record_id AS entry_id, plant_id, chain_sequence, record_hash AS entry_hash, content"
    " FROM ledger.ledger_record WHERE organization_id = :organization_id"
    " AND record_type = 'checkpoint' AND record_id = ANY(CAST(:ids AS uuid[]))"
)
_AUDIT_CHECKPOINTS: Final = text(
    "SELECT entry_id, chain_sequence, entry_hash, filters AS content FROM shared.audit_entry"
    " WHERE organization_id = :organization_id AND operation = 'checkpoint'"
    " AND entry_id = ANY(CAST(:ids AS uuid[]))"
)
_EVIDENCE_KEY: Final = text(
    "SELECT storage_key FROM ledger.evidence"
    " WHERE organization_id = :organization_id AND evidence_id = :evidence_id"
)


def _uuid(value: object) -> uuid.UUID:
    return uuid.UUID(str(value))


@repository
class SqlExportPlaces:
    """``PlaceLookup`` sobre ``identity``."""

    def __init__(self, database: LedgerDatabase) -> None:
        self._database = database

    async def places(
        self, context: ScopeContext, plant_id: uuid.UUID, zone_ids: Sequence[uuid.UUID]
    ) -> Places:
        rows = await self._database.read(
            context,
            _PLACES,
            {
                "organization_id": context.organization_id,
                "plant_id": plant_id,
                "zone_ids": [str(zone) for zone in zone_ids],
            },
        )
        if not rows:
            raise LookupError("la planta o sus zonas no están en la organización del contexto")
        first = rows[0]
        return Places(
            organization=OrganizationRef(
                organization_id=context.organization_id,
                code=str(first.organization_code),
                name=str(first.organization_name),
            ),
            plant=PlantRef(
                plant_id=plant_id,
                code=str(first.plant_code),
                name=str(first.plant_name),
                country=str(first.country),
                timezone=str(first.timezone),
            ),
            zones={_uuid(row.zone_id): (str(row.zone_code), str(row.zone_name)) for row in rows},
        )


class PublicKeySource(Protocol):
    """``CheckpointService.checkpoint_public_keys``."""

    def checkpoint_public_keys(self) -> tuple[CheckpointPublicKey, ...]: ...


def _content(value: Any) -> CheckpointContent:
    raw = bytes(value) if isinstance(value, bytes | bytearray | memoryview) else value
    document = json.loads(raw) if isinstance(raw, bytes | str) else raw
    return CheckpointContent.from_json(document)


@repository
class SqlCheckpointLookup:
    """``CheckpointLookup``: los puntos de control por identificador y las claves publicadas."""

    def __init__(self, database: LedgerDatabase, keys: PublicKeySource) -> None:
        self._database = database
        self._keys = keys

    async def checkpoints_by_ids(
        self, context: ScopeContext, entry_ids: Sequence[uuid.UUID]
    ) -> tuple[StoredCheckpoint, ...]:
        parameters: Mapping[str, Any] = {
            "organization_id": context.organization_id,
            "ids": [str(value) for value in entry_ids],
        }
        found: list[StoredCheckpoint] = []
        for row in await self._database.read(context, _LEDGER_CHECKPOINTS, parameters):
            plant = None if row.plant_id is None else _uuid(row.plant_id)
            found.append(
                StoredCheckpoint(
                    chain=CheckpointChain(ChainKind.LEDGER, plant),
                    sequence=int(row.chain_sequence),
                    entry_id=_uuid(row.entry_id),
                    entry_hash=str(row.entry_hash),
                    content=_content(row.content),
                )
            )
        for row in await self._database.read(context, _AUDIT_CHECKPOINTS, parameters):
            found.append(
                StoredCheckpoint(
                    chain=CheckpointChain(ChainKind.AUDIT, None),
                    sequence=int(row.chain_sequence),
                    entry_id=_uuid(row.entry_id),
                    entry_hash=str(row.entry_hash),
                    content=_content(row.content),
                )
            )
        return tuple(found)

    def checkpoint_public_keys(self) -> tuple[CheckpointPublicKey, ...]:
        return self._keys.checkpoint_public_keys()


class ObjectReader(Protocol):
    """``S3Storage.get_object``."""

    async def get_object(self, key: str, *, version_id: str | None = None) -> bytes: ...


@repository
class StorageBlobs:
    """``BlobSource`` sobre ``ledger.evidence`` y el depósito de evidencias."""

    def __init__(self, database: LedgerDatabase, storage: ObjectReader) -> None:
        self._database = database
        self._storage = storage

    async def clip_bytes(self, context: ScopeContext, evidence: EvidenceReference) -> bytes:
        rows = await self._database.read(
            context,
            _EVIDENCE_KEY,
            {"organization_id": context.organization_id, "evidence_id": evidence.evidence_id},
        )
        if not rows:
            raise LookupError("evidencia no encontrada")
        return await self._storage.get_object(str(rows[0].storage_key))

    async def attachment_bytes(
        self, context: ScopeContext, storage_key: str, sha256: str, size_bytes: int
    ) -> bytes:
        return await self._storage.get_object(storage_key)
