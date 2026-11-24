"""La imagen de la foto del hallazgo (TASK-589; A-128 §6; A-112 «Precisiones del 13-11»; A-88).

``FindingPhotoReader.image(context, finding_state_id, photo_id)``, en este orden, con **una**
entrada ``finding_photo_read`` por petición (en la cadena de la organización del contexto):

1. **El hallazgo** (``FindingDetailService.visible_subject``, los pasos del detalle):
   ``findings.read`` sobre su zona y compuerta de uso aprobada en el instante del hecho.
   Invisible → ``denied`` sin zona y ``ResourceNotFound``; sin compuerta legible → ``error`` y
   ``FindingGateUnavailable`` (503, fallo cerrado).
2. **La imagen**: ``evidence.read`` sobre la zona del hallazgo, como ``POST
   /evidence/{evidence_id}/read-url``. Sin ella → ``denied`` y ``ResourceNotFound`` (nunca
   ``forbidden``, BR-NUC-09).
3. **La foto** (una transacción con el contexto autorizado): los registros del hallazgo (los
   tramos del episodio o el anclaje) y, por ``FindingPhotoPort.finding_image``, la foto solo si es
   ``productive``, está viva, es de la planta y la zona del hallazgo y es de uno de sus registros
   (mismo ``record_id`` del nodo, episodio y tramo). Una foto ajena, de otra zona,
   ``commissioning``, vencida o sin imagen → ``denied`` y ``ResourceNotFound``.
4. **Integridad**: con los bytes, la ``sha256`` y el tamaño se recalculan **antes** de servir; si
   no coinciden → ``error`` y ``FindingPhotoIntegrityFailed`` (nada se sirve).
5. **Auditoría en la misma transacción** que la lectura de los bytes: se confirma con ella antes de
   devolverlos; si la entrada falla, la transacción se revierte y no sale ningún byte.

Las denegaciones de los pasos 1 y 2 van en su propia transacción y no dependen de la base (como
``authorization_denied``): si su entrada no se escribe, la respuesta sigue siendo ``not_found``.
La petición que la cadena deniega antes de la ruta (sin ``findings.read``) deja su
``finding_photo_read`` ``denied`` por ``gate_denied``. Nunca bytes, suma ni tamaño en registros,
métricas o trazas (NFR-GOB-25). Ningún paso lee la hora del sistema: el reloj es el del puerto.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from vigia_platform.fleet.application.episode_photo_read import image_outcome
from vigia_platform.fleet.application.finding_photos import FindingPhotoPort
from vigia_platform.identity.authz.authorize import Resource, ResourceNotFound
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import (
    AuditOperation,
    AuditOutcome,
    AuditWriter,
    ResourceRef,
)
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.loop.application.detail import FindingDetailService, FindingGateUnavailable
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.observability.logging import get_logger, log_context

__all__ = [
    "RESOURCE_KIND",
    "FindingPhotoIntegrityFailed",
    "FindingPhotoReader",
    "PhotoAuthorizer",
    "ServedFindingPhoto",
]

RESOURCE_KIND: Final = "finding_photo"
"""``resource_ref.kind`` de la auditoría y de la autorización."""

_EVIDENCE: Final = PermissionKey.EVIDENCE_READ

_log = get_logger("loop.finding_photo")


class FindingPhotoIntegrityFailed(Exception):
    """Los bytes guardados no tienen la suma o el tamaño de la fila: no se sirve nada."""

    def __init__(self) -> None:
        super().__init__("la foto del hallazgo no coincide con su suma")


class PhotoAuthorizer(Protocol):
    """``AuthorizationPort.authorize`` (``identity.authz.authorize.Authorizer``)."""

    async def authorize(
        self, context: ScopeContext, key: PermissionKey | str, resource: Resource
    ) -> ScopeContext: ...


@dataclass(frozen=True, slots=True)
class ServedFindingPhoto:
    """Los bytes que se sirven, ya verificados contra la fila."""

    data: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class _Place:
    plant_id: uuid.UUID
    zone_id: uuid.UUID


@repository
class FindingPhotoReader:
    """``GET /loop/findings/{finding_state_id}/photos/{photo_id}/image``."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        subjects: FindingDetailService,
        authorizer: PhotoAuthorizer,
        audit: AuditWriter,
        photos: FindingPhotoPort,
    ) -> None:
        self._database = database
        self._subjects = subjects
        self._authorizer = authorizer
        self._audit = audit
        self._photos = photos

    def __repr__(self) -> str:
        return "FindingPhotoReader()"

    async def image(
        self, context: ScopeContext, finding_state_id: uuid.UUID, photo_id: uuid.UUID
    ) -> ServedFindingPhoto:
        """Los bytes de la foto, con su entrada ``finding_photo_read`` ya confirmada."""
        if type(finding_state_id) is not uuid.UUID or type(photo_id) is not uuid.UUID:
            raise ResourceNotFound()
        try:
            _seen, subject = await self._subjects.visible_subject(context, finding_state_id)
        except ResourceNotFound:
            await self._rejected(context, photo_id, None, AuditOutcome.DENIED)
            raise
        except FindingGateUnavailable:
            await self._rejected(context, photo_id, None, AuditOutcome.ERROR)
            raise
        place = _Place(_uuid(subject["plant_id"]), _uuid(subject["zone_id"]))
        resource = Resource(
            _uuid(subject["organization_id"]),
            RESOURCE_KIND,
            photo_id,
            plant_id=place.plant_id,
            zone_id=place.zone_id,
        )
        try:
            authorized = await self._authorizer.authorize(context, _EVIDENCE, resource)
        except ResourceNotFound:
            await self._rejected(context, photo_id, place, AuditOutcome.DENIED)
            raise
        async with self._database.transaction(authorized) as transaction:
            record_ids = await self._subjects.segment_record_ids(transaction, subject)
            stored = await self._photos.finding_image(
                transaction,
                photo_id,
                plant_id=place.plant_id,
                zone_id=place.zone_id,
                record_ids=record_ids,
            )
            outcome = image_outcome(stored)
            # La entrada se escribe en la transacción de la lectura y se confirma con ella: si
            # falla, la transacción se revierte y la excepción sale antes de servir un byte.
            await self._audit.append(
                authorized,
                AuditOperation.FINDING_PHOTO_READ,
                outcome=outcome,
                plant_id=place.plant_id,
                zone_id=place.zone_id,
                resource=ResourceRef(RESOURCE_KIND, photo_id),
                transaction=transaction,
            )
        if outcome is AuditOutcome.DENIED or stored is None or stored.data is None:
            raise ResourceNotFound()
        if outcome is not AuditOutcome.SUCCESS:
            with log_context(zone_id=place.zone_id):
                _log.error("la foto del hallazgo no coincide con su suma: no se sirve")
            raise FindingPhotoIntegrityFailed()
        return ServedFindingPhoto(data=stored.data)

    async def gate_denied(self, context: ScopeContext, photo_id: uuid.UUID) -> None:
        """La entrada ``denied`` de una petición que la cadena denegó antes de la ruta (sin
        ``findings.read``). Sin zona: la petición no llegó a ver el hallazgo."""
        await self._rejected(context, photo_id, None, AuditOutcome.DENIED)

    async def _rejected(
        self,
        context: ScopeContext,
        photo_id: uuid.UUID,
        place: _Place | None,
        outcome: AuditOutcome,
    ) -> None:
        """``finding_photo_read`` sin servir, en su propia transacción; sin base, no cambia nada."""
        try:
            await self._audit.append(
                context,
                AuditOperation.FINDING_PHOTO_READ,
                outcome=outcome,
                plant_id=None if place is None else place.plant_id,
                zone_id=None if place is None else place.zone_id,
                resource=ResourceRef(RESOURCE_KIND, photo_id),
            )
        except Exception:
            # Como ``authorization_denied``: la denegación no depende de la base.
            _log.exception("no se pudo auditar finding_photo_read sin servir")


def _uuid(value: Any) -> uuid.UUID:
    return value if type(value) is uuid.UUID else uuid.UUID(str(value))
