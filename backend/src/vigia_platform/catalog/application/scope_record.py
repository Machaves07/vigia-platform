"""Acta de alcance y aprobación de la compuerta de montaje (LC-GOB-03; BL §2.2.1 y nota D-2).

``file_scope_record`` (``POST /zones/{zone_id}/gates/mounting/scope-record``):

1. **Antes de la transacción**, sin escribir nada y en este orden: zona dentro del alcance con
   ``commissioning.run`` (fuera de alcance o inexistente responden igual, ``ResourceNotFound``;
   la clave se exige **antes** de verificar documentos, que no autoriza); declaración del
   difuminado con su captura (``declared: true`` y ``capture_document_ref``; si falta,
   ``blur_not_verified``); textos por la política base de U-02 y el validador mínimo de U-03
   (``free_text_rejected``), guardados en NFC; nodo asignado a la zona por
   ``IdentityQueryPort.assigned_node`` (si no, ``node_not_assigned``); una fila de encuadre por
   cada cámara del catálogo vigente de la zona, ni más ni menos (si no, ``ScopeRecordInvalid``,
   ``invalid_request``); y ``verify_document_refs`` de la captura (``blur_check_capture``) y del
   acta firmada opcional (``scope_record``), que consulta el almacén sin transacción abierta.
2. **En una sola transacción**: candado de la proyección y sobre ``GateState`` firmado
   (``prepare_transition``, antes de tocar la cadena), ``plant_policy_loaded_at_signing`` (la
   política de planta no bloquea, BR-GOB-22), ``mounting_gate_record`` (``source_key =
   record_id``) y su fila, la transición de montaje a ``approved`` (``gate_state_changed`` con su
   evento, intervalo y proyección) y los documentos a ``used``.

Un acta nueva sobre un montaje ya ``approved`` abre un intervalo contiguo con el nuevo
``record_id``, sin hueco. Con la firma caída nada queda escrito (``GateUnavailable``).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Final, Protocol

from vigia_contracts.models.enumerations import GateStatus

from vigia_platform.catalog.adapters.postgres.catalog_repository import PostgresCatalogRepository
from vigia_platform.catalog.adapters.postgres.plant_policy_repository import (
    PostgresPlantPolicyRepository,
)
from vigia_platform.catalog.adapters.postgres.scope_record_repository import (
    PostgresScopeRecordRepository,
)
from vigia_platform.catalog.application.admission import CatalogRejected
from vigia_platform.catalog.application.documents import DocumentService
from vigia_platform.catalog.application.gates import GateService, GateTransition, record_id_of
from vigia_platform.catalog.detail_codes import CatalogDetailCode
from vigia_platform.catalog.domain.documents import DocumentRef
from vigia_platform.catalog.domain.enums import DocumentKind, GateKind
from vigia_platform.catalog.domain.scope_record import (
    MAX_FRAMING_CHARS,
    MAX_SCOPE_TEXT_CHARS,
    MountingGateRecord,
    ScopeRecordInvalid,
    ScopeRecordRequest,
    framings_match,
)
from vigia_platform.catalog.domain.texts import has_content
from vigia_platform.catalog.domain.time_windows import utc_instant
from vigia_platform.identity.application.hierarchy import NodeView
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.identity.authz.context import with_unit
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.writer import EscritorExpediente, RecordScope
from vigia_platform.ledger.free_text import FreeTextField, FreeTextPolicyRegistry, FreeTextRejected
from vigia_platform.shared.context import ActorUnit, Role, ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7

__all__ = [
    "MOUNTING_GATE_RECORD",
    "AssignedNodeLookup",
    "ScopeRecordFiled",
    "ScopeRecordService",
]

MOUNTING_GATE_RECORD: Final = "mounting_gate_record"

_SCOPE_TEXT: Final = FreeTextField(MOUNTING_GATE_RECORD, "/scope_text_es", 1, MAX_SCOPE_TEXT_CHARS)
_FRAMING: Final = FreeTextField(
    MOUNTING_GATE_RECORD, "/cameras[*]/framing_description_es", 1, MAX_FRAMING_CHARS
)


class AssignedNodeLookup(Protocol):
    """``IdentityQueryPort.assigned_node`` de U-02 (LC-NUC-05)."""

    async def assigned_node(self, context: ScopeContext, zone_id: uuid.UUID) -> NodeView | None: ...


@dataclass(frozen=True, slots=True)
class ScopeRecordFiled:
    """El acta registrada y la transición de montaje que aprobó (tras confirmar)."""

    record: MountingGateRecord
    transition: GateTransition


@repository
class ScopeRecordService:
    """``catalog.gates`` (acta de alcance): la compuerta de montaje."""

    def __init__(
        self,
        *,
        gates: GateService,
        records: PostgresScopeRecordRepository,
        catalog: PostgresCatalogRepository,
        policies: PostgresPlantPolicyRepository,
        documents: DocumentService,
        nodes: AssignedNodeLookup,
        writer: EscritorExpediente,
        free_text: FreeTextPolicyRegistry,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._gates = gates
        self._records = records
        self._catalog = catalog
        self._policies = policies
        self._documents = documents
        self._nodes = nodes
        self._writer = writer
        self._free_text = free_text
        self._random_bytes = random_bytes

    def __repr__(self) -> str:
        return "ScopeRecordService()"

    def _text(self, value: str, field: FreeTextField) -> str:
        try:
            text = self._free_text.apply(value, field)
        except FreeTextRejected:
            raise CatalogRejected(CatalogDetailCode.FREE_TEXT_REJECTED) from None
        if not has_content(text):
            raise CatalogRejected(CatalogDetailCode.FREE_TEXT_REJECTED)
        return text

    async def _zone_cameras(self, context: ScopeContext, zone_id: uuid.UUID) -> set[uuid.UUID]:
        """Las cámaras del catálogo vigente de la zona (``zone_camera`` conserva también las que
        salieron del catálogo, que ya no son de la zona)."""
        version = await self._catalog.version(context, zone_id)
        if version is None:
            return set()
        return {uuid.UUID(str(camera["camera_id"])) for camera in version.payload["cameras"]}

    async def file_scope_record(
        self, context: ScopeContext, zone_id: uuid.UUID, request: ScopeRecordRequest
    ) -> ScopeRecordFiled:
        """Registra el acta y aprueba la compuerta de montaje; lo devuelve tras confirmar.

        ``ResourceNotFound``, ``CatalogRejected`` (``blur_not_verified``, ``node_not_assigned``,
        ``free_text_rejected``), ``ScopeRecordInvalid``, ``DocumentRequestInvalid``,
        ``StorageUnavailable`` o ``GateUnavailable``; en todos, nada queda escrito.
        """
        if not isinstance(request, ScopeRecordRequest):
            raise TypeError("request debe ser ScopeRecordRequest")
        zone, authorized = await self._gates.zone(context, zone_id, PermissionKey.COMMISSIONING_RUN)
        role = authorized.actor.role_in_use
        if role is None:  # ``authorize`` siempre lo fija; sin él no se registra autor.
            raise ResourceNotFound()
        if not request.blur_verified_by_declaration:
            raise CatalogRejected(CatalogDetailCode.BLUR_NOT_VERIFIED)
        scope_text = self._text(request.scope_text_es, _SCOPE_TEXT)
        cameras = tuple(
            replace(
                framing,
                framing_description_es=self._text(framing.framing_description_es, _FRAMING),
            )
            for framing in request.cameras
        )
        if await self._nodes.assigned_node(authorized, zone.zone_id) is None:
            raise CatalogRejected(CatalogDetailCode.NODE_NOT_ASSIGNED)
        if not framings_match(cameras, sorted(await self._zone_cameras(authorized, zone.zone_id))):
            raise ScopeRecordInvalid("una fila de encuadre por cada cámara de la zona")
        capture = DocumentRef.parse(request.capture_document_ref)
        signed = None if request.document_ref is None else DocumentRef.parse(request.document_ref)
        verified = [
            await self._documents.verify_document_refs(
                authorized, [capture], {DocumentKind.BLUR_CHECK_CAPTURE}, zone.plant_id
            )
        ]
        if signed is not None:
            verified.append(
                await self._documents.verify_document_refs(
                    authorized, [signed], {DocumentKind.SCOPE_RECORD}, zone.plant_id
                )
            )
        writer_context = with_unit(authorized, ActorUnit.U03)
        actor_id = uuid.UUID(str(authorized.actor.id))
        record_id = uuid7(self._gates.clock, self._random_bytes)

        async def file(transaction: Transaction) -> ScopeRecordFiled:
            prepared = await self._gates.prepare_transition(
                transaction,
                writer_context,
                zone.zone_id,
                GateKind.MOUNTING,
                GateStatus.APPROVED,
                record_id,
            )
            policy_loaded = await self._policies.loaded(transaction, zone.plant_id)
            record = MountingGateRecord(
                record_id=record_id,
                organization_id=zone.organization_id,
                plant_id=zone.plant_id,
                zone_id=zone.zone_id,
                scope_text_es=scope_text,
                cameras=cameras,
                declared_by=actor_id,
                declared_at=utc_instant(prepared.at),
                capture_document_ref=capture,
                document_ref=signed,
                signed_by=actor_id,
                role_in_use=Role(role),
                plant_policy_loaded_at_signing=policy_loaded,
            )
            written = await self._writer.write(
                writer_context,
                MOUNTING_GATE_RECORD,
                record.record_content(),
                scope=RecordScope(plant_id=zone.plant_id, zone_id=zone.zone_id),
                occurred_at=prepared.at,
                transaction=transaction,
            )
            record = replace(record, ledger_record_id=record_id_of(written))
            await self._records.insert(transaction, record)
            transition = await self._gates.commit_transition(transaction, writer_context, prepared)
            for documents in verified:
                await self._documents.mark_used(transaction, documents)
            return ScopeRecordFiled(record, transition)

        filed: ScopeRecordFiled = await self._gates.run(writer_context, file)
        return filed
