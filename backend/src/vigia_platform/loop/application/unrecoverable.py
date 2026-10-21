"""Declarar un adjunto de cierre no recuperable (DR-LAZ-04; runbook 7.3, paso 4; VIG-274).

``vigia-admin declare-unrecoverable-attachment`` es el último paso del runbook 7.3: cuando la
identidad de restauración de U-02 (``vigia-restore``) comprobó que el objeto de ``closure/`` se
perdió **sin versión anterior** (pasos 2 y 3), el operador deja escrito en el expediente que el
adjunto consta como no recuperable, con su hash y la fecha de la comprobación. Nunca se inventa
nada ni se reescribe nada (P4, P5): la entrada de evidencia y el cierre siguen como están, el
estado del hallazgo no cambia y el cierre sigue siendo válido con su descripción estructurada. La
siguiente exportación del mismo alcance se compone sin los bytes del adjunto y lo declara en su
manifiesto (``accreditation.export.contents``).

Como la reconstrucción a demanda (``loop.projection.admin``): el operador autenticado se audita en
la proveedora (``closure_attachment_unrecoverable_declared``, la intención antes de escribir y el
resultado después) y el registro se escribe con el contexto de U-04 de la organización, nunca con
un contexto de operador fabricado en el cliente.

**Guardas** (fallo cerrado, nada se declara por error, porque la declaración es permanente):

- el adjunto existe, es de la organización y de la planta pedidas y fue **aceptado** (está en una
  entrada de evidencia del expediente); si no, ``not_found`` o el rechazo con su motivo;
- ``--sha256`` es el hash que el expediente declara para ese adjunto: el operador confirma qué
  objeto se perdió con lo que listó ``vigia-restore``;
- ``--detected-at`` no es futura según el ``Clock`` inyectado.

**Idempotencia**: ``declaration_id`` es el UUID v5 del adjunto y el contenido es función solo de
los argumentos y del expediente, así que repetir la orden con los mismos datos es un duplicado
aceptado (``already_declared = true``) y con otra fecha es ``idempotency_conflict``: un adjunto se
declara una sola vez. ``vigia-admin`` no tiene el depósito de evidencias (``UnitServices.evidence``
es ``None``): la comprobación de las versiones es de ``vigia-restore``, no de esta orden.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from pydantic import JsonValue
from sqlalchemy import text
from vigia_contracts.models.enumerations import AcceptanceStatus

from vigia_platform.identity.application.admin_cli import AdminError, ExitCode
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.ledger.application.audit_writer import AuditOperation
from vigia_platform.ledger.application.writer import (
    LedgerRejection,
    LedgerRejectionCode,
    RecordScope,
)
from vigia_platform.loop.projection.store import LoopProjectionStore
from vigia_platform.shared.context import (
    ActorKind,
    ActorUnit,
    ContextOrigin,
    ScopeContext,
    repository,
)
from vigia_platform.shared.runtime.units import UnitServices
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

__all__ = [
    "CLOSURE_ATTACHMENT_UNRECOVERABLE",
    "DECLARE_UNRECOVERABLE_ATTACHMENT",
    "UNRECOVERABLE_NAMESPACE",
    "UnrecoverableAttachmentCommand",
    "declaration_id",
]

CLOSURE_ATTACHMENT_UNRECOVERABLE: Final = "closure_attachment_unrecoverable"
DECLARE_UNRECOVERABLE_ATTACHMENT: Final = "declare_unrecoverable_attachment"
UNRECOVERABLE_NAMESPACE: Final = uuid.uuid5(
    uuid.NAMESPACE_URL, "urn:vigia:loop:closure_attachment_unrecoverable"
)

_ATTACHMENT: Final = text(
    "SELECT plant_id, zone_id, subject_id, sha256, size_bytes, content_type, verified_at"
    " FROM loop.closure_attachment"
    " WHERE organization_id = :organization AND attachment_id = :attachment"
)
_EVIDENCE_ENTRY: Final = text(
    "SELECT evidence_entry_id, action_id, record_id FROM loop.action_evidence_entry"
    " WHERE organization_id = :organization AND plant_id = :plant"
    " AND :attachment = ANY(attachment_ids)"
    " ORDER BY added_at, evidence_entry_id LIMIT 1"
)
_SUBJECT_KIND: Final = text(
    "SELECT subject_kind FROM loop.finding_state"
    " WHERE organization_id = :organization AND plant_id = :plant AND subject_id = :subject"
)


def declaration_id(attachment_id: uuid.UUID) -> uuid.UUID:
    """El identificador de la declaración: uno por adjunto (UUID v5)."""
    return uuid.uuid5(UNRECOVERABLE_NAMESPACE, str(attachment_id))


@dataclass(frozen=True)
class DeclareTask:
    """Descriptor a demanda para el constructor de contexto existente; sin horario."""

    task_name: str = DECLARE_UNRECOVERABLE_ATTACHMENT
    unit: ActorUnit = ActorUnit.U04


def _rejected(code: str, message: str) -> AdminError:
    return AdminError(code, message, ExitCode.REJECTED)


@repository
class UnrecoverableAttachmentCommand:
    def __init__(self, services: UnitServices, store: LoopProjectionStore | None = None) -> None:
        self._services = services
        self._store = store if store is not None else LoopProjectionStore()

    async def run(
        self,
        operator: ScopeContext,
        organization_id: uuid.UUID,
        plant_id: uuid.UUID,
        attachment_id: uuid.UUID,
        *,
        sha256: str,
        detected_at: datetime,
        dry_run: bool,
    ) -> Mapping[str, object]:
        services = self._services
        if (
            operator.actor.kind is not ActorKind.OPERATOR
            or operator.origin is not ContextOrigin.ADMIN_COMMAND
            or operator.organization_id != services.provider_organization_id
        ):
            raise ResourceNotFound()
        if detected_at.utcoffset() is None:
            raise _rejected("detected_at_invalid", "--detected-at necesita zona horaria")
        detected = to_millisecond(detected_at)
        if detected > services.clock.now():
            raise _rejected("detected_at_invalid", "--detected-at no puede ser futura")
        context = services.contexts.context_for_organization(DeclareTask(), organization_id)
        async with services.database.transaction(context) as transaction:
            content, zone_id = await self._content(
                transaction, organization_id, plant_id, attachment_id, sha256, detected
            )
            output: dict[str, object] = {
                "organization_id": str(organization_id),
                "plant_id": str(plant_id),
                "attachment_id": str(attachment_id),
                "declaration_id": content["declaration_id"],
                "sha256": content["sha256"],
                "detected_at": content["detected_at"],
            }
            if dry_run:
                return output
        filters: dict[str, JsonValue] = {
            "organization_id": str(organization_id),
            "plant_id": str(plant_id),
            "attachment_id": str(attachment_id),
            "sha256": content["sha256"],
            "detected_at": content["detected_at"],
        }
        # La intención se audita antes de escribir: una caída no deja una declaración muda.
        await services.audit.append(
            operator, AuditOperation.CLOSURE_ATTACHMENT_UNRECOVERABLE_DECLARED, filters=filters
        )
        async with services.database.transaction(context) as transaction:
            content, zone_id = await self._content(
                transaction, organization_id, plant_id, attachment_id, sha256, detected
            )
            result, _ = await self._store.write_and_apply(
                transaction,
                services.writer,
                context,
                CLOSURE_ATTACHMENT_UNRECOVERABLE,
                content,
                scope=RecordScope(plant_id=plant_id, zone_id=zone_id),
                occurred_at=detected,
            )
        if isinstance(result, LedgerRejection):
            if result.code is LedgerRejectionCode.IDEMPOTENCY_CONFLICT:
                raise _rejected(
                    "already_declared", "el adjunto ya consta como no recuperable con otros datos"
                )
            raise _rejected("declaration_rejected", f"el expediente rechazó: {result.code.value}")
        duplicate = result.status is AcceptanceStatus.ACCEPTED_DUPLICATE
        await services.audit.append(
            operator,
            AuditOperation.CLOSURE_ATTACHMENT_UNRECOVERABLE_DECLARED,
            filters={**filters, "completed": True, "record_id": str(result.record_id)},
        )
        return {**output, "record_id": str(result.record_id), "already_declared": duplicate}

    async def _content(
        self,
        transaction: Any,
        organization_id: uuid.UUID,
        plant_id: uuid.UUID,
        attachment_id: uuid.UUID,
        sha256: str,
        detected_at: datetime,
    ) -> tuple[dict[str, Any], uuid.UUID]:
        """El contenido del registro, leído del expediente con las guardas de arriba."""
        keys = {"organization": organization_id, "plant": plant_id, "attachment": attachment_id}
        attachment = (await transaction.execute(_ATTACHMENT, keys)).mappings().first()
        if attachment is None or uuid.UUID(str(attachment["plant_id"])) != plant_id:
            raise ResourceNotFound()
        entry = (await transaction.execute(_EVIDENCE_ENTRY, keys)).mappings().first()
        if attachment["verified_at"] is None or entry is None:
            raise _rejected(
                "attachment_not_in_ledger", "el adjunto no está en ninguna entrada de evidencia"
            )
        if sha256 != attachment["sha256"]:
            raise _rejected("sha256_mismatch", "--sha256 no es el hash que declara el expediente")
        subject_id = uuid.UUID(str(attachment["subject_id"]))
        kind = (
            await transaction.execute(_SUBJECT_KIND, {**keys, "subject": subject_id})
        ).scalar_one_or_none()
        if kind is None:
            raise ResourceNotFound()
        content: dict[str, Any] = {
            "declaration_id": str(declaration_id(attachment_id)),
            "subject_kind": str(kind),
            "subject_id": str(subject_id),
            "action_id": str(entry["action_id"]),
            "evidence_entry_id": str(entry["evidence_entry_id"]),
            "evidence_record_id": str(entry["record_id"]),
            "attachment_id": str(attachment_id),
            "sha256": str(attachment["sha256"]),
            "size_bytes": int(attachment["size_bytes"]),
            "content_type": str(attachment["content_type"]),
            "detected_at": format_timestamp(detected_at),
            "previous_version_available": False,
        }
        return content, uuid.UUID(str(attachment["zone_id"]))
