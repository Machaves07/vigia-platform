"""``catalog.documents``: concesión de subida y verificación de documentos firmados (LC-GOB-05).

**Concesión** (``issue``, ruta ``POST /documents`` con ``commissioning.run`` sobre la planta):

1. límites de dominio (tipo, ``kind``, tamaño de 1 byte a ``VIGIA_DOCUMENTS_MAX_BYTES``, suma);
2. ``authorize`` sobre la planta: fuera de alcance, ``ResourceNotFound`` (``not_found``);
3. la planta existe y está activa en la organización (si no, ``not_found``, auditado como
   ``denied``): una concesión de organización alcanza también plantas que no existen;
4. el almacén: comprobación de la clave nueva y firma de la URL de un solo ``PUT``, **fuera** de
   toda transacción (PAT-NUC-RES-08); con el almacén caído o lento, ``StorageUnavailable`` y nada
   escrito;
5. una sola transacción con la concesión en ``issued`` y su entrada de auditoría
   ``document_upload_granted``: si cualquiera de las dos falla, no queda concesión y la URL no sale
   del servicio.

**Verificación compartida** (TASK-211, 212 y 216), en dos pasos para que la consulta al almacén no
retenga una transacción abierta:

- ``verify_document_refs(context, refs, expected_kinds, plant_id)``, **antes** de abrir la
  transacción del registro: a lo sumo 10 referencias distintas; cada una es exactamente la
  concedida, de esa organización y planta, con un ``kind`` esperado y aún en ``issued``; el objeto
  existe (``head_object`` con ``ChecksumMode=ENABLED``, todos en paralelo, **nunca**
  ``get_object``) con el mismo tamaño, el mismo tipo y la misma SHA-256 de objeto entero. Una
  concesión vencida sin objeto subido está ``expired`` (derivado, no se escribe). Cualquier
  discrepancia es ``DocumentRequestInvalid`` (``invalid_request`` con mensaje genérico, sin
  detalle del objeto) y **no cambia** ninguna concesión;
- ``mark_used(transaction, verified)``, **dentro** de la transacción del registro que cita los
  documentos: ``issued → used`` una sola vez. Si otro registro ya usó alguno, nada cambia y
  ``DocumentRequestInvalid`` revierte la transacción del llamador.

**«Un solo PUT».** La URL vence a los 15 minutos y lleva firmados la suma y el tipo: un ``PUT``
con otros bytes da ``BadDigest`` y uno con otro tipo no pasa la firma. Un ``PUT`` repetido dentro
de la vigencia solo puede escribir **los mismos bytes** (misma suma), en una versión nueva del
depósito versionado y con bloqueo; después de vencer, ninguno. La verificación compara la suma
concedida, así que lo registrado no cambia por un ``PUT`` posterior.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

from vigia_platform.catalog.adapters.postgres.document_repository import PostgresDocumentGrants
from vigia_platform.catalog.adapters.s3.documents import DocumentObjectStore
from vigia_platform.catalog.domain.documents import (
    MAX_DOCUMENT_REFS,
    DocumentRef,
    DocumentRequestInvalid,
    DocumentSettings,
    DocumentUploadGrant,
)
from vigia_platform.catalog.domain.enums import DocumentKind
from vigia_platform.fleet.domain.enums import UploadGrantStatus
from vigia_platform.identity.authz.authorize import Resource, ResourceNotFound
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import (
    AuditOperation,
    AuditOutcome,
    AuditWriter,
    ResourceRef,
)
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ContextAbsent, ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.storage import ObjectHead, PresignedRequest

__all__ = [
    "DOCUMENT_RESOURCE_KIND",
    "DocumentAuthorizer",
    "DocumentService",
    "IssuedDocument",
    "VerifiedDocuments",
]

DOCUMENT_RESOURCE_KIND: Final = "document"
"""``resource_ref.kind`` de la auditoría de una concesión de documento."""

_GENERIC: Final = "las referencias de documento no coinciden con lo concedido"
"""Mensaje interno único de toda discrepancia: nunca dice qué objeto ni qué campo."""


class DocumentAuthorizer(Protocol):
    """``AuthorizationPort.authorize`` (``identity.authz.authorize.Authorizer``)."""

    async def authorize(
        self, context: ScopeContext, key: PermissionKey | str, resource: Resource
    ) -> ScopeContext: ...


@dataclass(frozen=True, slots=True)
class IssuedDocument:
    """La concesión emitida y su URL de subida (secreto de corta vida: no se registra)."""

    grant: DocumentUploadGrant
    upload: PresignedRequest = field(repr=False)


@dataclass(frozen=True, slots=True)
class VerifiedDocuments:
    """Resultado de ``verify_document_refs``: lo que ``mark_used`` pasará a ``used``."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    refs: tuple[DocumentRef, ...]

    @property
    def document_ids(self) -> tuple[uuid.UUID, ...]:
        return tuple(ref.document_id for ref in self.refs)


def _require_context(context: object) -> ScopeContext:
    if not isinstance(context, ScopeContext):
        raise ContextAbsent()
    return context


def _plant(value: object) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise DocumentRequestInvalid("plant_id debe ser un UUID")
    return uuid.UUID(int=value.int)


def _expected(kinds: object) -> frozenset[DocumentKind]:
    if isinstance(kinds, str | bytes) or not isinstance(kinds, Iterable):
        raise DocumentRequestInvalid("expected_kinds debe ser una colección de document_kind")
    expected: set[DocumentKind] = set()
    for kind in kinds:
        if not isinstance(kind, str):
            raise DocumentRequestInvalid("expected_kinds fuera de la lista")
        try:
            expected.add(DocumentKind(kind))
        except ValueError:
            raise DocumentRequestInvalid("expected_kinds fuera de la lista") from None
    if not expected:
        raise DocumentRequestInvalid("expected_kinds no puede estar vacío")
    return frozenset(expected)


def _parsed_refs(refs: object) -> tuple[DocumentRef, ...]:
    if isinstance(refs, str | bytes | dict) or not isinstance(refs, Sequence):
        raise DocumentRequestInvalid("refs debe ser una lista de document_ref")
    if len(refs) > MAX_DOCUMENT_REFS:
        raise DocumentRequestInvalid(f"a lo sumo {MAX_DOCUMENT_REFS} documentos")
    parsed = tuple(DocumentRef.parse(ref) for ref in refs)
    if len({ref.document_id for ref in parsed}) != len(parsed):
        raise DocumentRequestInvalid("un documento citado dos veces")
    return parsed


def _object_matches(grant: DocumentUploadGrant, head: ObjectHead | None) -> bool:
    return head is not None and grant.object_matches(
        size_bytes=head.size_bytes,
        content_type=head.content_type,
        sha256_hex=head.full_object_sha256_hex,
    )


@repository
class DocumentService:
    """``catalog.documents`` sobre PostgreSQL y ``vigia-evidence``."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        audit: AuditWriter,
        authorizer: DocumentAuthorizer,
        store: DocumentObjectStore,
        clock: Clock,
        settings: DocumentSettings | None = None,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._database = database
        self._grants = PostgresDocumentGrants(database)
        self._audit = audit
        self._authorizer = authorizer
        self._store = store
        self._clock = clock
        self._settings = settings if settings is not None else DocumentSettings()
        self._random_bytes = random_bytes

    @property
    def settings(self) -> DocumentSettings:
        return self._settings

    # --- Concesión ---------------------------------------------------------------------------

    async def issue(
        self,
        context: ScopeContext,
        *,
        plant_id: uuid.UUID,
        kind: object,
        content_type: object,
        size_bytes: object,
        sha256: object,
    ) -> IssuedDocument:
        """``POST /documents``: concesión en ``issued`` y URL de un solo ``PUT`` (15 minutos)."""
        context = _require_context(context)
        plant = _plant(plant_id)
        grant = DocumentUploadGrant.issue(
            document_id=uuid7(self._clock, self._random_bytes),
            organization_id=context.organization_id,
            plant_id=plant,
            kind=kind,
            content_type=content_type,
            size_bytes=size_bytes,
            sha256=sha256,
            issued_at=self._clock.now(),
            settings=self._settings,
        )
        authorized = await self._authorizer.authorize(
            context,
            PermissionKey.COMMISSIONING_RUN,
            Resource.plant(context.organization_id, plant),
        )
        resource = ResourceRef(DOCUMENT_RESOURCE_KIND, grant.document_id)
        if not await self._grants.active_plant(authorized, plant):
            await self._audit.append(
                authorized,
                AuditOperation.DOCUMENT_UPLOAD_GRANTED,
                outcome=AuditOutcome.DENIED,
                resource=resource,
                result_count=0,
            )
            raise ResourceNotFound()
        # Una clave UUID v7 recién generada no tiene objeto; si lo tuviera, ``DocumentKeyTaken``
        # (``conflict``): nunca se firma una URL que sobrescriba.
        upload = await self._store.prepare_upload(grant, self._clock.now)
        async with self._database.transaction(authorized) as transaction:
            await self._grants.insert(transaction, grant)
            await self._audit.append(
                authorized,
                AuditOperation.DOCUMENT_UPLOAD_GRANTED,
                plant_id=plant,
                resource=resource,
                result_count=1,
                transaction=transaction,
            )
        return IssuedDocument(grant, upload)

    # --- Verificación ------------------------------------------------------------------------

    async def verify_document_refs(
        self,
        context: ScopeContext,
        refs: Sequence[DocumentRef | object],
        expected_kinds: Collection[DocumentKind | str],
        plant_id: uuid.UUID,
    ) -> VerifiedDocuments:
        """Comprueba ``refs`` contra sus concesiones y los metadatos de sus objetos.

        Sin transacción abierta. No escribe nada: el paso a ``used`` es ``mark_used``.
        """
        context = _require_context(context)
        plant = _plant(plant_id)
        expected = _expected(expected_kinds)
        parsed = _parsed_refs(refs)
        verified = VerifiedDocuments(context.organization_id, plant, parsed)
        if not parsed:
            return verified
        grants = await self._grants.by_ids(context, plant, verified.document_ids)
        for ref in parsed:
            grant = grants.get(ref.document_id)
            if (
                grant is None
                or grant.kind not in expected
                or grant.status is not UploadGrantStatus.ISSUED
                or not grant.matches(ref)
            ):
                raise DocumentRequestInvalid(_GENERIC)
        heads = await self._store.heads([ref.storage_key for ref in parsed])
        now = self._clock.now()
        for ref in parsed:
            grant = grants[ref.document_id]
            head = heads[ref.storage_key]
            status = grant.effective_status(now, uploaded=head is not None)
            if status is not UploadGrantStatus.ISSUED or not _object_matches(grant, head):
                raise DocumentRequestInvalid(_GENERIC)
        return verified

    async def mark_used(self, transaction: Transaction, verified: VerifiedDocuments) -> None:
        """``issued → used`` en la transacción del registro, una sola vez por documento."""
        if not isinstance(transaction, Transaction):
            raise ContextAbsent()
        if not isinstance(verified, VerifiedDocuments):
            raise TypeError("verified debe salir de verify_document_refs")
        if transaction.context.organization_id != verified.organization_id:
            raise DocumentRequestInvalid(_GENERIC)
        if not verified.refs:
            return
        changed = await self._grants.mark_used(
            transaction, verified.plant_id, verified.document_ids
        )
        if changed != frozenset(verified.document_ids):
            raise DocumentRequestInvalid(_GENERIC)
