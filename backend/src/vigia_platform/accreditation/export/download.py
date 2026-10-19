"""Descarga de una exportación lista (TASK-345; LC-LAZ-20; SUP-U05-23 y 40; PAT-LAZ-SEG-04;
NFR-LAZ-16 y 18; PR-LAZ-60; infraestructura §9.2 y nota U04-H-09).

- ``volumes(context, export_id)`` (SUP-U05-40): la lista de volúmenes ``vol-{nn}.zip`` del
  paquete, con **una URL prefirmada de 15 minutos por volumen**.
- ``document(context, export_id)`` (SUP-U05-23): la URL de 15 minutos del documento portátil
  (``document_sha256``), que el flujo escribió también como objeto propio.

Las dos exigen ``export.create`` sobre **cada** zona de la exportación (como SUP-U05-22): una
exportación de otra organización, inexistente o fuera del alcance responde ``not_found``, nunca
``forbidden``. Solo se emiten con el paquete ``ready``: en cualquier otro estado, incluido
``expired`` (``ready`` con la descarga vencida, leído con el ``Clock`` inyectado),
``ExportNotDownloadable`` con el estado que se muestra (409).

La URL la firma ``vigia-api`` con el ``s3:GetObject`` sobre ``org/*`` que U-02 ya concede
(ningún permiso nuevo), fijada a la **versión** escrita por el flujo (``stored_objects``), con
``Content-Disposition: attachment`` y tipo opaco (``presign_attachment_get``): el navegador la
descarga y no interpreta nada (``nosniff`` no es una cabecera que S3 deje sustituir; con un tipo
opaco no hay nada que adivinar). Vigencia: 15 minutos del mismo reloj; se emite por clic y **no
se promete uso único** (U04-H-09).

**Auditoría de la emisión** (PAT-LAZ-SEG-04): cada URL escribe **una** entrada
``export_download_granted`` **en la misma transacción** en la que se emite (y en la que se lee el
paquete): el artefacto (``vol-01.zip``… o ``document.pdf``) y su SHA-256, el actor, el instante de
la entrada y la vigencia (``expires_at``), con ``records = access_grant_not_download``: registra
la concesión de acceso, no la descarga efectiva. Si una entrada no se escribe, la transacción se
deshace y **ninguna** URL sale de aquí. No existe ninguna operación que liste el prefijo
``export/``: las claves salen de la organización, la planta y la exportación.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Protocol

from sqlalchemy import text

from vigia_platform.accreditation.export.request import read_status
from vigia_platform.accreditation.export.state import ExportStatus
from vigia_platform.accreditation.export.volumes import (
    DOCUMENT_NAME,
    ExportLocation,
    StoredObjects,
)
from vigia_platform.identity.authz.authorize import Authorizer, Resource, ResourceNotFound
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import (
    AuditOperation,
    AuditWriter,
    ResourceRef,
)
from vigia_platform.loop.application.attachments import ACCESS_GRANT_DECLARATION
from vigia_platform.loop.domain.enums import ExportState
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.signing.keys import format_timestamp
from vigia_platform.shared.storage import PresignedRequest

__all__ = [
    "EXPORT_RESOURCE_KIND",
    "EXPORT_URL_TTL",
    "ExportDocumentGrant",
    "ExportDownloadSigner",
    "ExportDownloads",
    "ExportNotDownloadable",
    "ExportPackageUnreadable",
    "ExportVolumesGrant",
    "VolumeUrl",
]

EXPORT_URL_TTL: Final = timedelta(minutes=15)
"""Vigencia de cada URL de descarga (``[objetivo propio]``, U04-H-09)."""
EXPORT_RESOURCE_KIND: Final = "export"
"""``resource_ref.kind`` de las entradas de auditoría de una exportación."""

_EXPORT_CREATE: Final = PermissionKey.EXPORT_CREATE
_PACKAGE: Final = text(
    "SELECT archive_sha256, document_sha256, stored_objects"
    " FROM accreditation.export_package WHERE export_id = :export_id"
)


class ExportNotDownloadable(Exception):
    """La exportación no está ``ready`` (o su descarga venció): 409 con el estado mostrado."""

    def __init__(self, state: ExportState) -> None:
        self.state = state
        super().__init__(state.value)


class ExportPackageUnreadable(Exception):
    """El paquete está ``ready`` pero no consta lo que escribió el flujo: no se emite nada."""


class ExportDownloadSigner(Protocol):
    """Lo único que la descarga usa del almacén: firmar la lectura de **una** clave y versión."""

    async def presign_attachment_get(
        self, key: str, ttl: timedelta = ..., *, version_id: str | None = None
    ) -> PresignedRequest: ...


class TransactionSource(Protocol):
    def transaction(
        self, context: ScopeContext
    ) -> contextlib.AbstractAsyncContextManager[Transaction]: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class VolumeUrl:
    number: int
    name: str
    size_bytes: int
    sha256: str
    url: str
    expires_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class ExportVolumesGrant:
    export_id: uuid.UUID
    archive_sha256: str
    volumes: tuple[VolumeUrl, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ExportDocumentGrant:
    export_id: uuid.UUID
    document_sha256: str
    size_bytes: int
    url: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class _Package:
    archive_sha256: str
    document_sha256: str
    stored: StoredObjects


@repository
class ExportDownloads:
    """SUP-U05-40 y SUP-U05-23: URL de descarga de 15 minutos, auditadas por emisión."""

    def __init__(
        self,
        *,
        database: TransactionSource,
        authorizer: Authorizer,
        audit: AuditWriter,
        storage: ExportDownloadSigner,
        clock: Clock,
    ) -> None:
        self._database = database
        self._authorizer = authorizer
        self._audit = audit
        self._storage = storage
        self._clock = clock

    async def volumes(self, context: ScopeContext, export_id: uuid.UUID) -> ExportVolumesGrant:
        authorized, status = await self._ready(context, export_id)
        location = ExportLocation(status.organization_id, status.plant_id, status.export_id)
        urls: list[VolumeUrl] = []
        async with self._database.transaction(authorized) as transaction:
            package = await _package(transaction, export_id)
            volumes = package.stored.volumes
            if not volumes or any(volume.version_id is None for volume in volumes):
                raise ExportPackageUnreadable()
            for volume in volumes:
                presigned = await self._storage.presign_attachment_get(
                    location.volume_key(volume.number), EXPORT_URL_TTL, version_id=volume.version_id
                )
                await self._grant(
                    authorized, transaction, status, volume.name, volume.sha256, presigned
                )
                urls.append(
                    VolumeUrl(
                        number=volume.number,
                        name=volume.name,
                        size_bytes=volume.size_bytes,
                        sha256=volume.sha256,
                        url=presigned.url,
                        expires_at=presigned.expires_at,
                    )
                )
        return ExportVolumesGrant(
            export_id=status.export_id, archive_sha256=package.archive_sha256, volumes=tuple(urls)
        )

    async def document(self, context: ScopeContext, export_id: uuid.UUID) -> ExportDocumentGrant:
        authorized, status = await self._ready(context, export_id)
        location = ExportLocation(status.organization_id, status.plant_id, status.export_id)
        async with self._database.transaction(authorized) as transaction:
            package = await _package(transaction, export_id)
            document = package.stored.document
            if (
                document is None
                or document.version_id is None
                or document.sha256 != package.document_sha256
            ):
                raise ExportPackageUnreadable()
            presigned = await self._storage.presign_attachment_get(
                location.document_key, EXPORT_URL_TTL, version_id=document.version_id
            )
            await self._grant(
                authorized, transaction, status, DOCUMENT_NAME, document.sha256, presigned
            )
        return ExportDocumentGrant(
            export_id=status.export_id,
            document_sha256=document.sha256,
            size_bytes=document.size_bytes,
            url=presigned.url,
            expires_at=presigned.expires_at,
        )

    async def _ready(
        self, context: ScopeContext, export_id: uuid.UUID
    ) -> tuple[ScopeContext, ExportStatus]:
        """La exportación visible y autorizada sobre todas sus zonas, y solo si está ``ready``."""
        if not isinstance(context, ScopeContext) or type(export_id) is not uuid.UUID:
            raise ResourceNotFound()
        async with self._database.transaction(context) as transaction:
            status = await read_status(transaction, export_id)
        if status is None:
            raise ResourceNotFound()
        granted = [
            await self._authorizer.authorize(
                context,
                _EXPORT_CREATE,
                Resource.zone(context.organization_id, status.plant_id, zone),
            )
            for zone in status.zone_ids
        ]
        shown = status.shown_state(self._clock.now())
        if shown is not ExportState.READY:
            raise ExportNotDownloadable(shown)
        return granted[0], status

    async def _grant(
        self,
        context: ScopeContext,
        transaction: Transaction,
        status: ExportStatus,
        artifact: str,
        sha256: str,
        presigned: PresignedRequest,
    ) -> None:
        """La entrada de la emisión, en la transacción de la emisión (PAT-LAZ-SEG-04)."""
        await self._audit.append(
            context,
            AuditOperation.EXPORT_DOWNLOAD_GRANTED,
            plant_id=status.plant_id,
            resource=ResourceRef(EXPORT_RESOURCE_KIND, status.export_id),
            filters={
                "artifact": artifact,
                "sha256": sha256,
                "records": ACCESS_GRANT_DECLARATION,
                "expires_at": format_timestamp(presigned.expires_at),
            },
            result_count=1,
            transaction=transaction,
        )


async def _package(transaction: Transaction, export_id: uuid.UUID) -> _Package:
    row = (await transaction.execute(_PACKAGE, {"export_id": export_id})).mappings().first()
    if row is None:
        raise ExportPackageUnreadable()
    try:
        value = row["stored_objects"]
        stored = StoredObjects.from_json(json.loads(value) if isinstance(value, str) else value)
    except (KeyError, TypeError, ValueError):
        raise ExportPackageUnreadable() from None
    return _Package(row["archive_sha256"], row["document_sha256"], stored)
