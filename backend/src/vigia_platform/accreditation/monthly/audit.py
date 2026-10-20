"""Resultado de la auditoría muestral (TASK-347; SUP-U05-26; BR-LAZ-106 y 107; H-39; L-1).

``SampleAuditService.record_result(context, sample_item_id, ...)`` es ``registrar_resultado_de_
muestra`` de business-logic-model.md §10 (S-PLA-15). **La auditoría observa y deja constancia: no
modifica ni revierte ninguna clasificación** (BR-LAZ-107): escribe un registro **nuevo**
``sample_audit_result`` y cierra, una sola vez, el resultado de la fila ⛓ de
``accreditation.audit_sample_item``; no lee ni escribe ``loop.finding_state``, ``loop.
classification`` ni ningún registro ``classification``. Pasos, en este orden:

1. **Alcance**: el elemento de la organización, con su muestra y su paquete, y
   ``findings.classify`` sobre **su zona** (la clave reutilizada por la nota de BLM §10 del
   2026-09-21: solo el coordinador SST la tiene). Inexistente, de otra organización, fuera de
   alcance o bajo concesión de proveedor: ``ResourceNotFound`` (``not_found``), sin escribir nada.
2. **Ningún intento de modificar** (BR-LAZ-107, «por una ruta indirecta»): un cuerpo que nombra un
   campo de la clasificación auditada (``reason_category``, ``classification_outcome``,
   ``reclassify``…, comparados sin mayúsculas, guiones ni subrayados) o que pide como resultado
   una salida de clasificación (``confirmed``, ``false_positive``…) responde
   ``sample_cannot_modify_classification`` sin escribir nada. Cualquier otro campo desconocido es
   ``invalid_request``.
3. **Forma**: ``outcome`` de ``sample_audit_outcome`` (``consistent_criterion``,
   ``doubtful_criterion``, ``criterion_error``) y ``comment`` opcional de hasta 1000 caracteres en
   NFC por la política base (vacío o en blanco, sin comentario). Si no, ``invalid_request``.
4. El paquete del elemento está **sellado** y su muestra es la del paquete; si no (un paquete en
   composición, o la muestra de una composición que perdió la carrera de sellado),
   ``monthly_package_not_available`` (409).
5. **En una sola transacción**: la fila del elemento con ``SELECT … FOR UPDATE`` y, si ya tiene
   resultado, ``sample_already_reviewed`` (409) sin escribir nada. Después,
   ``LoopProjectionStore.write_and_apply`` escribe ``sample_audit_result`` (los dos validadores de
   U-04 sobre ``/comment``: ``blocked_vocabulary_term`` o ``person_attribution_detected``) y la
   proyección del lazo solo avanza su control (el tipo no tiene efecto sobre ningún sujeto); la
   fila del elemento recibe su resultado (de nulo a valor, una sola vez: lo vigila además el
   disparador ⛓ de ``laz_0031``) y, si con él ya no queda ningún elemento sin revisar,
   ``audit_sample.review_completed_at``.

**Una sola vez** (AGENTS.md, garantías): la exclusión es el ``FOR UPDATE`` de la fila del elemento
con la comprobación de ``outcome IS NULL``. La clave de idempotencia del tipo
(``/sample_item_id``) es el respaldo de la base: si alguna vez se llegara a escribir con el
elemento ya revisado, el escritor devolvería ``idempotency_conflict`` (o un duplicado) y la
operación falla cerrada (``RuntimeError``, nada queda escrito), nunca un segundo registro.

**La revisión completa** se decide **después** de escribir el registro: el ``INSERT`` del
expediente toma la cadena de la planta hasta el ``COMMIT``, así que dos resultados simultáneos de
la misma muestra se ordenan ahí y el segundo ve confirmado el del primero.

**Orden de candados** (AGENTS.md): 1) la fila de ``audit_sample_item`` (``FOR UPDATE``); 2) la
cadena de la planta (el disparador del expediente); 3) el control de la proyección de la planta
(``LoopProjectionStore``); 4) la fila de ``audit_sample`` (el ``UPDATE`` de la revisión completa).
Ninguna otra operación toma una fila de la muestra después de la cadena de la planta: no hay ciclo
con las del lazo (que toman antes el hallazgo) ni con los compromisos (solo 2 y 3).
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
from vigia_contracts.models.enumerations import AcceptanceStatus

from vigia_platform.accreditation.detail_codes import AccreditationDetailCode
from vigia_platform.identity.authz.authorize import Authorizer, Resource, ResourceNotFound
from vigia_platform.identity.authz.context import with_unit
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.writer import (
    EscritorExpediente,
    LedgerDatabase,
    LedgerRejection,
    LedgerRejectionCode,
    Receipt,
    RecordScope,
)
from vigia_platform.ledger.free_text import FreeTextField, FreeTextRejected, apply_base_policy
from vigia_platform.loop.detail_codes import LoopDetailCode, U04DetailCode
from vigia_platform.loop.domain.enums import ClassificationOutcome, SampleAuditOutcome
from vigia_platform.loop.projection.store import LoopProjectionStore
from vigia_platform.loop.record_types import Classification
from vigia_platform.shared.api.errors import ApiErrorCode, ExternalDependencyDown
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

__all__ = [
    "CLASSIFICATION_FIELD_NAMES",
    "COMMENT_MAX_CHARS",
    "SAMPLE_AUDIT_RESULT",
    "SAMPLE_ITEM_RESOURCE_KIND",
    "SampleAuditRejected",
    "SampleAuditRequestInvalid",
    "SampleAuditResult",
    "SampleAuditService",
    "SampleAuditUnavailable",
    "modification_attempt",
]

SAMPLE_AUDIT_RESULT: Final = "sample_audit_result"
COMMENT_MAX_CHARS: Final = 1000
"""Comentario de la auditoría (DE §3.3: 0 a 1000)."""
SAMPLE_ITEM_RESOURCE_KIND: Final = "audit_sample_item"
"""``kind`` del recurso en la auditoría de la denegación."""

_COMMENT_FIELD: Final = FreeTextField(SAMPLE_AUDIT_RESULT, "/comment", 1, COMMENT_MAX_CHARS)

CLASSIFICATION_FIELD_NAMES: Final[frozenset[str]] = frozenset(
    {
        # Los campos del registro ``classification`` (DE §5.2) salvo ``outcome``, que en esta
        # operación es el resultado de la auditoría (``sample_audit_outcome``).
        *(name for name in Classification.model_fields if name != "outcome"),
        # Los de la clasificación tal como los muestra ``GET …/sample`` (SUP-U05-25).
        "classification_outcome",
        "classified_by_role",
        "classification_record_id",
        # Verbos de una modificación o una reversión.
        "reclassify",
        "reclassification",
        "revert",
        "reverse",
        "rollback",
        "override",
        "new_outcome",
        "corrected_outcome",
        "lifecycle_state",
    }
)
"""Nombres que, en el cuerpo, son un intento de alterar o revertir la clasificación auditada."""

_NOT_ALNUM: Final = re.compile(r"[^0-9a-z]")


def _folded(name: str) -> str:
    """El nombre sin mayúsculas, acentos, guiones, subrayados ni espacios (``reasonText`` y
    ``reason-text`` son ``reason_text``)."""
    decomposed = unicodedata.normalize("NFKD", name.casefold())
    return _NOT_ALNUM.sub("", "".join(c for c in decomposed if not unicodedata.combining(c)))


_FOLDED_CLASSIFICATION_FIELDS: Final = frozenset(
    _folded(name) for name in CLASSIFICATION_FIELD_NAMES
)
_CLASSIFICATION_OUTCOMES: Final = frozenset(
    _folded(outcome.value) for outcome in ClassificationOutcome
)


def modification_attempt(outcome: object, extra_fields: Mapping[str, object]) -> bool:
    """¿El cuerpo intenta alterar o revertir la clasificación (BR-LAZ-107)?

    Sí si nombra un campo de la clasificación o si el resultado pedido es una salida de
    clasificación en vez de un ``sample_audit_outcome``."""
    if isinstance(outcome, str) and _folded(outcome) in _CLASSIFICATION_OUTCOMES:
        return True
    return any(
        isinstance(name, str) and _folded(name) in _FOLDED_CLASSIFICATION_FIELDS
        for name in extra_fields
    )


_ITEM: Final = text(
    "SELECT ai.sample_item_id, ai.organization_id, ai.plant_id, ai.zone_id, ai.sample_id,"
    " ai.classification_id, s.package_id, p.state AS package_state,"
    " p.audit_sample_id AS package_sample_id"
    " FROM accreditation.audit_sample_item AS ai"
    " JOIN accreditation.audit_sample AS s"
    " ON s.organization_id = ai.organization_id AND s.sample_id = ai.sample_id"
    " JOIN accreditation.monthly_package AS p"
    " ON p.organization_id = s.organization_id AND p.package_id = s.package_id"
    " WHERE ai.organization_id = :organization_id AND ai.sample_item_id = :sample_item_id"
)
_LOCK_ITEM: Final = text(
    "SELECT outcome FROM accreditation.audit_sample_item"
    " WHERE organization_id = :organization_id AND sample_item_id = :sample_item_id"
    " FOR UPDATE"
)
_CLASSIFICATION_RECORD: Final = text(
    "SELECT record_id FROM loop.classification"
    " WHERE organization_id = :organization_id AND classification_id = :classification_id"
)
_CLOSE_ITEM: Final = text(
    "UPDATE accreditation.audit_sample_item SET outcome = :outcome, comment = :comment,"
    " reviewed_by = CAST(:reviewed_by AS jsonb), reviewed_at = :reviewed_at,"
    " record_id = :record_id"
    " WHERE organization_id = :organization_id AND sample_item_id = :sample_item_id"
    " AND outcome IS NULL"
    " RETURNING sample_item_id"
)
_COMPLETE_SAMPLE: Final = text(
    "UPDATE accreditation.audit_sample SET review_completed_at = :completed_at"
    " WHERE organization_id = :organization_id AND sample_id = :sample_id"
    " AND review_completed_at IS NULL"
    " AND NOT EXISTS (SELECT 1 FROM accreditation.audit_sample_item AS ai"
    " WHERE ai.organization_id = :organization_id AND ai.sample_id = :sample_id"
    " AND ai.outcome IS NULL)"
    " RETURNING review_completed_at"
)
"""La revisión completa: después del registro (la cadena de la planta ordena a los concurrentes)."""

_VALIDATOR_CODES: Final[Mapping[str, LoopDetailCode]] = {
    LoopDetailCode.BLOCKED_VOCABULARY_TERM.value: LoopDetailCode.BLOCKED_VOCABULARY_TERM,
    LoopDetailCode.PERSON_ATTRIBUTION_DETECTED.value: LoopDetailCode.PERSON_ATTRIBUTION_DETECTED,
}


class SampleAuditRejected(Exception):
    """Un rechazo de negocio, con su ``detail_code`` (acreditación o validadores del lazo)."""

    def __init__(self, detail_code: U04DetailCode) -> None:
        super().__init__(detail_code.value)
        self.detail_code = detail_code


class SampleAuditRequestInvalid(Exception):
    """El cuerpo no tiene la forma del esquema: ``invalid_request`` sin ``detail_code``."""

    api_code: Final = ApiErrorCode.INVALID_REQUEST


class SampleAuditUnavailable(ExternalDependencyDown):
    """Transitorio: la cadena de la planta no se pudo tomar a tiempo."""


@dataclass(frozen=True, slots=True)
class SampleAuditResult:
    """El recibo del resultado (SUP-U05-26)."""

    sample_item_id: uuid.UUID
    sample_id: uuid.UUID
    package_id: uuid.UUID
    classification_id: uuid.UUID
    outcome: SampleAuditOutcome
    comment: str | None
    record_id: uuid.UUID
    reviewed_at: datetime
    review_completed: bool
    """Con este resultado ya no queda ningún elemento de la muestra sin revisar."""


@dataclass(frozen=True, slots=True)
class _Item:
    sample_item_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    sample_id: uuid.UUID
    classification_id: uuid.UUID
    package_id: uuid.UUID
    reviewable: bool
    """El paquete está sellado y esta muestra es la suya."""


def _uuid(value: object) -> uuid.UUID:
    return uuid.UUID(str(value))


def _item(row: Any) -> _Item:
    data = row._mapping
    sample_id = _uuid(data["sample_id"])
    package_sample = data["package_sample_id"]
    return _Item(
        sample_item_id=_uuid(data["sample_item_id"]),
        organization_id=_uuid(data["organization_id"]),
        plant_id=_uuid(data["plant_id"]),
        zone_id=_uuid(data["zone_id"]),
        sample_id=sample_id,
        classification_id=_uuid(data["classification_id"]),
        package_id=_uuid(data["package_id"]),
        reviewable=(
            data["package_state"] == "sealed"
            and package_sample is not None
            and _uuid(package_sample) == sample_id
        ),
    )


def _outcome(value: object) -> SampleAuditOutcome:
    if not isinstance(value, str):
        raise SampleAuditRequestInvalid("el resultado es una cadena de la lista cerrada")
    try:
        return SampleAuditOutcome(value)
    except ValueError:
        raise SampleAuditRequestInvalid("resultado fuera de sample_audit_outcome") from None


def _comment(value: object) -> str | None:
    """El comentario en NFC de hasta 1000 caracteres; ausente o en blanco, ``None``."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise SampleAuditRequestInvalid("el comentario es una cadena")
    try:
        return apply_base_policy(value, _COMMENT_FIELD)
    except FreeTextRejected:
        raise SampleAuditRequestInvalid("comentario rechazado por la política base") from None


def _written(result: Receipt | LedgerRejection) -> Receipt:
    """Un rechazo o un duplicado revierten la transacción (nada queda escrito)."""
    if isinstance(result, LedgerRejection):
        if result.code is LedgerRejectionCode.FREE_TEXT_REJECTED:
            code = _VALIDATOR_CODES.get(result.detail_code or "")
            if code is not None:
                raise SampleAuditRejected(code)
            raise SampleAuditRequestInvalid("texto libre rechazado por el expediente")
        if result.code is LedgerRejectionCode.CHAIN_LOCKED_TIMEOUT:
            raise SampleAuditUnavailable("ledger_chain")
        raise RuntimeError(f"sample_audit_result rechazado: {result.code.value}")
    if result.status is not AcceptanceStatus.ACCEPTED:
        raise RuntimeError("sample_audit_result de un elemento ya revisado")
    return result


@repository
class SampleAuditService:
    """``accreditation.monthly``: el resultado de un elemento de la muestra auditada."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        writer: EscritorExpediente,
        authorizer: Authorizer,
        clock: Clock,
        store: LoopProjectionStore | None = None,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._database = database
        self._writer = writer
        self._authorizer = authorizer
        self._clock = clock
        self._store = store if store is not None else LoopProjectionStore()
        self._random_bytes = random_bytes

    def __repr__(self) -> str:
        return "SampleAuditService()"

    async def record_result(
        self,
        context: ScopeContext,
        sample_item_id: uuid.UUID,
        *,
        outcome: object,
        comment: object = None,
        extra_fields: Mapping[str, object] | None = None,
    ) -> SampleAuditResult:
        """Registra el resultado del elemento ``sample_item_id`` (una sola vez)."""
        authorized, item = await self._authorized(context, sample_item_id)
        extras = extra_fields or {}
        if modification_attempt(outcome, extras):
            raise SampleAuditRejected(AccreditationDetailCode.SAMPLE_CANNOT_MODIFY_CLASSIFICATION)
        if extras:
            raise SampleAuditRequestInvalid("campo desconocido en el cuerpo")
        chosen = _outcome(outcome)
        note = _comment(comment)
        if not item.reviewable:
            raise SampleAuditRejected(AccreditationDetailCode.MONTHLY_PACKAGE_NOT_AVAILABLE)
        writer_context = with_unit(authorized, ActorUnit.U04)
        async with self._database.transaction(writer_context) as transaction:
            locked = (
                await transaction.execute(
                    _LOCK_ITEM,
                    {
                        "organization_id": item.organization_id,
                        "sample_item_id": item.sample_item_id,
                    },
                )
            ).first()
            if locked is None:  # pragma: no cover - la fila ⛓ no se borra
                raise ResourceNotFound()
            if locked.outcome is not None:
                raise SampleAuditRejected(AccreditationDetailCode.SAMPLE_ALREADY_REVIEWED)
            await self._checked(transaction)
            return await self._write(transaction, writer_context, item, chosen, note)

    async def _checked(self, transaction: Transaction) -> None:
        """Con la fila del elemento bloqueada y sin resultado, antes de escribir (gancho de las
        pruebas de concurrencia, como ``ClassificationService._checked``)."""

    # --- pasos -----------------------------------------------------------------------------------

    async def _authorized(
        self, context: ScopeContext, sample_item_id: uuid.UUID
    ) -> tuple[ScopeContext, _Item]:
        """Paso 1: el elemento de la organización y ``findings.classify`` sobre su zona."""
        if not isinstance(context, ScopeContext) or type(sample_item_id) is not uuid.UUID:
            raise ResourceNotFound()
        rows = await self._database.read(
            context,
            _ITEM,
            {"organization_id": context.organization_id, "sample_item_id": sample_item_id},
        )
        if not rows:
            raise ResourceNotFound()
        item = _item(rows[0])
        resource = Resource(
            item.organization_id,
            SAMPLE_ITEM_RESOURCE_KIND,
            item.sample_item_id,
            plant_id=item.plant_id,
            zone_id=item.zone_id,
        )
        authorized = await self._authorizer.authorize(
            context, PermissionKey.FINDINGS_CLASSIFY, resource
        )
        if authorized.concession_id is not None:  # pragma: no cover - la matriz ya lo deniega
            await self._authorizer.deny(authorized, PermissionKey.FINDINGS_CLASSIFY, resource)
        return authorized, item

    async def _write(
        self,
        transaction: Transaction,
        context: ScopeContext,
        item: _Item,
        outcome: SampleAuditOutcome,
        comment: str | None,
    ) -> SampleAuditResult:
        """Paso 5: el registro, su proyección, el cierre de la fila y la revisión completa."""
        classification = (
            await transaction.execute(
                _CLASSIFICATION_RECORD,
                {
                    "organization_id": item.organization_id,
                    "classification_id": item.classification_id,
                },
            )
        ).first()
        if classification is None:  # pragma: no cover - la muestra solo toma clasificaciones
            raise RuntimeError("el elemento de la muestra no tiene su clasificación")
        actor = context.actor
        if actor.role_in_use is None:  # pragma: no cover - authorize fija el rol en uso
            raise ResourceNotFound()
        reviewer = {
            "user_id": str(actor.id),
            "display_name": actor.display_name_snapshot,
            "role_in_use": actor.role_in_use.value,
        }
        at = to_millisecond(self._clock.now())
        record_id = uuid7(self._clock, self._random_bytes)
        content: dict[str, Any] = {
            "sample_item_id": str(item.sample_item_id),
            "sample_id": str(item.sample_id),
            "package_id": str(item.package_id),
            "classification_id": str(item.classification_id),
            "classification_record_id": str(_uuid(classification.record_id)),
            "outcome": outcome.value,
            "reviewed_by": reviewer,
            "reviewed_at": format_timestamp(at),
        }
        if comment is not None:
            content["comment"] = comment
        result, _step = await self._store.write_and_apply(
            transaction,
            self._writer,
            context,
            SAMPLE_AUDIT_RESULT,
            content,
            scope=RecordScope(plant_id=item.plant_id, zone_id=item.zone_id),
            occurred_at=at,
            record_id=record_id,
        )
        receipt = _written(result)
        parameters = {
            "organization_id": item.organization_id,
            "sample_item_id": item.sample_item_id,
        }
        closed = (
            await transaction.execute(
                _CLOSE_ITEM,
                {
                    **parameters,
                    "outcome": outcome.value,
                    "comment": comment,
                    "reviewed_by": json.dumps(reviewer, ensure_ascii=False),
                    "reviewed_at": at,
                    "record_id": receipt.record_id,
                },
            )
        ).first()
        if closed is None:  # pragma: no cover - la fila está bloqueada y sin resultado
            raise RuntimeError("el elemento de la muestra ya tenía resultado")
        completed = (
            await transaction.execute(
                _COMPLETE_SAMPLE,
                {
                    "organization_id": item.organization_id,
                    "sample_id": item.sample_id,
                    "completed_at": at,
                },
            )
        ).first()
        return SampleAuditResult(
            sample_item_id=item.sample_item_id,
            sample_id=item.sample_id,
            package_id=item.package_id,
            classification_id=item.classification_id,
            outcome=outcome,
            comment=comment,
            record_id=receipt.record_id,
            reviewed_at=at,
            review_completed=completed is not None,
        )
