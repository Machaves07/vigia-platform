"""Servicio de la prueba de admisión (LC-GOB-02; BR-GOB-13 a 18; BLM §2.1, variante «Admisión»).

- ``evaluate`` (``POST /plants/{plant_id}/admissions``, ``catalog.manage`` sobre la planta): valida
  ``justification_es`` con la política base de U-02 más los validadores registrados (el mínimo de
  U-03, A-45), decide con ``decide_admission`` y, en **una sola transacción**, escribe
  ``standard_admission_test`` (``source_key = admission_id``, cadena de la planta) por
  ``EscritorExpediente`` y anexa la fila de ``catalog.family_admission`` con su
  ``ledger_record_id``. La rechazada se confirma igual (evidencia de diligencia, BR-GOB-15): la
  ruta responde el rechazo **después** de confirmar. Volver a intentarlo crea otra evaluación.
- ``family_already_admitted`` si ya hay una ``admitted`` de (planta, familia): no escribe nada
  (BR-GOB-14). La comprobación previa evita el trabajo; la garantía es el índice único parcial
  ``family_admission_admitted_once``, que resuelve las carreras (su violación revierte también el
  registro del expediente).
- ``list_admissions`` (``GET``, ``catalog.read`` sobre la planta): páginas por clave, la más
  reciente primero, de hasta ``MAX_PAGE_SIZE``.
- ``admission_for``: consulta interna (VIG-145, ``family_not_admitted``). Solo devuelve
  admisiones ``admitted`` de esa planta en la organización del contexto: no se hereda entre
  plantas ni entre organizaciones (BR-GOB-16).

No hay otra forma de admitir: ni clave de permiso, ni parámetro, ni ruta que salte las tres
respuestas (BR-GOB-18, NFR-GOB-41). Un recurso fuera de alcance responde como inexistente
(``ResourceNotFound``, BR-NUC-09). Ningún paso lee la hora del sistema: ``Clock`` inyectado.
"""

from __future__ import annotations

import dataclasses
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import exc as sa_exc
from vigia_contracts.models.enumerations import PredicateFamily

from vigia_platform.catalog.adapters.postgres.admission_repository import (
    FAMILY_ADMITTED_ONCE,
    AdmissionCursor,
    PostgresAdmissionRepository,
)
from vigia_platform.catalog.detail_codes import CATALOG_API_ERROR_CODES, CatalogDetailCode
from vigia_platform.catalog.domain.admission import (
    MAX_JUSTIFICATION_CHARS,
    AdmissionAnswers,
    FamilyAdmission,
    decide_admission,
)
from vigia_platform.identity.authz.authorize import Authorizer, Resource, ResourceNotFound
from vigia_platform.identity.authz.context import with_unit
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import AuditOperation, AuditWriter
from vigia_platform.ledger.application.writer import (
    EscritorExpediente,
    LedgerDatabase,
    LedgerRejection,
    LedgerRejectionCode,
    violated_constraint,
)
from vigia_platform.ledger.free_text import FreeTextField, FreeTextPolicyRegistry, FreeTextRejected
from vigia_platform.shared.api.errors import ApiErrorCode
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, Role, ScopeContext, repository
from vigia_platform.shared.ids import uuid7

__all__ = [
    "ADMISSION_RECORD_TYPE",
    "MAX_PAGE_SIZE",
    "AdmissionPage",
    "AdmissionRequest",
    "AdmissionService",
    "AdmissionWriteFailed",
    "CatalogRejected",
    "record_content",
]

ADMISSION_RECORD_TYPE: Final = "standard_admission_test"
MAX_PAGE_SIZE: Final = 200
"""Página máxima de ``GET /plants/{plant_id}/admissions``."""
_UNIQUE_VIOLATION: Final = "23505"
_JUSTIFICATION: Final = FreeTextField(
    ADMISSION_RECORD_TYPE, "/justification_es", 1, MAX_JUSTIFICATION_CHARS
)
"""El campo declarado de ``standard_admission_test`` (los límites de su esquema)."""


class CatalogRejected(Exception):
    """Rechazo de negocio con su ``detail_code`` de la lista cerrada (``catalog_*``).

    El mensaje es genérico: nunca lleva el valor recibido. ``api_code`` es el de U-02 bajo el que
    viaja (``CATALOG_API_ERROR_CODES``).
    """

    def __init__(self, detail_code: CatalogDetailCode) -> None:
        super().__init__(f"rechazo del catálogo: {detail_code.value}")
        self.detail_code = CatalogDetailCode(detail_code)

    @property
    def api_code(self) -> ApiErrorCode:
        return CATALOG_API_ERROR_CODES[self.detail_code]


class AdmissionWriteFailed(Exception):
    """El expediente rechazó el registro por una causa que no es del cuerpo (p. ej. la cadena
    ocupada): la evaluación se revierte entera."""

    def __init__(self, rejection: LedgerRejection) -> None:
        super().__init__(f"registro rechazado: {rejection.code.value}")
        self.rejection = rejection


@dataclass(frozen=True, slots=True, kw_only=True)
class AdmissionRequest:
    """El cuerpo de ``POST /plants/{plant_id}/admissions``."""

    family: PredicateFamily
    answers: AdmissionAnswers
    justification_es: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "family", PredicateFamily(self.family))
        if not isinstance(self.answers, AdmissionAnswers):
            raise TypeError("answers debe ser AdmissionAnswers")
        if self.justification_es is not None and not isinstance(self.justification_es, str):
            raise TypeError("justification_es debe ser texto")


@dataclass(frozen=True, slots=True)
class AdmissionPage:
    items: tuple[FamilyAdmission, ...]
    next_cursor: AdmissionCursor | None


def record_content(admission: FamilyAdmission) -> dict[str, Any]:
    """Contenido de ``standard_admission_test`` (``StandardAdmissionTest``)."""
    content: dict[str, Any] = {
        "admission_id": str(admission.admission_id),
        "plant_id": str(admission.plant_id),
        "family": admission.family.value,
        "answers": admission.answers.as_dict(),
        "result": admission.result.value,
    }
    if admission.failed_criterion is not None:
        content["failed_criterion"] = admission.failed_criterion.value
    if admission.justification_es is not None:
        content["justification_es"] = admission.justification_es
    return content


@repository
class AdmissionService:
    """``catalog.admission``: evaluar, listar y ``admission_for``."""

    def __init__(
        self,
        *,
        repository: PostgresAdmissionRepository,
        database: LedgerDatabase,
        writer: EscritorExpediente,
        authorizer: Authorizer,
        audit: AuditWriter,
        free_text: FreeTextPolicyRegistry,
        clock: Clock,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._repository = repository
        self._database = database
        self._writer = writer
        self._authorizer = authorizer
        self._audit = audit
        self._free_text = free_text
        self._clock = clock
        self._random_bytes = random_bytes

    def __repr__(self) -> str:
        return "AdmissionService()"

    async def _plant(
        self, context: ScopeContext, plant_id: uuid.UUID, key: PermissionKey
    ) -> ScopeContext:
        """El contexto autorizado sobre la planta; inexistente o fuera de alcance, igual."""
        if type(plant_id) is not uuid.UUID or not await self._repository.plant_exists(
            context, plant_id
        ):
            raise ResourceNotFound()
        return await self._authorizer.authorize(
            context, key, Resource.plant(context.organization_id, plant_id)
        )

    def _justification(self, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return self._free_text.apply(value, _JUSTIFICATION)
        except FreeTextRejected:
            raise CatalogRejected(CatalogDetailCode.FREE_TEXT_REJECTED) from None

    async def evaluate(
        self, context: ScopeContext, plant_id: uuid.UUID, request: AdmissionRequest
    ) -> FamilyAdmission:
        """Evalúa y registra la prueba; devuelve la evaluación confirmada (admitida o no).

        ``CatalogRejected(family_already_admitted)`` sin escribir nada si la familia ya está
        admitida en la planta; ``CatalogRejected(free_text_rejected)`` si la justificación no
        pasa la política.
        """
        if not isinstance(request, AdmissionRequest):
            raise TypeError("request debe ser AdmissionRequest")
        justification = self._justification(request.justification_es)
        authorized = await self._plant(context, plant_id, PermissionKey.CATALOG_MANAGE)
        role = authorized.actor.role_in_use
        if role is None:  # ``authorize`` siempre lo fija; sin él no se registra autor.
            raise ResourceNotFound()
        decision = decide_admission(request.answers)
        writer_context = with_unit(authorized, ActorUnit.U03)
        now = self._clock.now()
        admission_id = uuid7(self._clock, self._random_bytes)
        try:
            async with self._database.transaction(writer_context) as transaction:
                if await self._repository.admitted_in(transaction, plant_id, request.family):
                    raise CatalogRejected(CatalogDetailCode.FAMILY_ALREADY_ADMITTED)
                draft = FamilyAdmission(
                    admission_id=admission_id,
                    organization_id=authorized.organization_id,
                    plant_id=plant_id,
                    family=request.family,
                    answers=request.answers,
                    justification_es=justification,
                    result=decision.result,
                    failed_criterion=decision.failed_criterion,
                    evaluated_by=authorized.actor.id,
                    role_in_use=Role(role),
                    evaluated_at=now,
                    # Provisional: el identificador real lo da el recibo de abajo.
                    ledger_record_id=admission_id,
                )
                written = await self._writer.write(
                    writer_context,
                    ADMISSION_RECORD_TYPE,
                    record_content(draft),
                    occurred_at=now,
                    transaction=transaction,
                )
                if isinstance(written, LedgerRejection):
                    if written.code is LedgerRejectionCode.FREE_TEXT_REJECTED:
                        raise CatalogRejected(CatalogDetailCode.FREE_TEXT_REJECTED)
                    raise AdmissionWriteFailed(written)
                admission = dataclasses.replace(draft, ledger_record_id=written.record_id)
                await self._repository.insert(transaction, admission)
        except sa_exc.IntegrityError as error:
            if violated_constraint(error, _UNIQUE_VIOLATION) == FAMILY_ADMITTED_ONCE:
                raise CatalogRejected(CatalogDetailCode.FAMILY_ALREADY_ADMITTED) from None
            raise
        return admission

    async def list_admissions(
        self,
        context: ScopeContext,
        plant_id: uuid.UUID,
        *,
        after: AdmissionCursor | None = None,
        limit: int = MAX_PAGE_SIZE,
    ) -> AdmissionPage:
        """Las evaluaciones de la planta (``catalog.read``), la más reciente primero."""
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValueError(f"limit debe estar entre 1 y {MAX_PAGE_SIZE}")
        authorized = await self._plant(context, plant_id, PermissionKey.CATALOG_READ)
        async with self._database.transaction(authorized) as transaction:
            rows = await self._repository.page(transaction, plant_id, after=after, limit=limit + 1)
            items = tuple(rows[:limit])
            if authorized.concession_id is not None:
                # BR-NUC-38: la lectura del proveedor, auditada en la misma transacción (fallo
                # cerrado, como ``hierarchy_read`` de A-50).
                await self._audit.append(
                    authorized,
                    AuditOperation.CATALOG_READ,
                    plant_id=plant_id,
                    result_count=len(items),
                    transaction=transaction,
                )
        more = len(rows) > limit
        last = items[-1] if items else None
        cursor = (
            AdmissionCursor(last.evaluated_at, last.admission_id)
            if more and last is not None
            else None
        )
        return AdmissionPage(items, cursor)

    async def admission_for(
        self, context: ScopeContext, plant_id: uuid.UUID, family: PredicateFamily
    ) -> FamilyAdmission | None:
        """La admisión ``admitted`` de (planta, familia) en la organización del contexto.

        ``None`` si no la hay: la de otra planta u otra organización no cuenta (BR-GOB-16).
        """
        if type(plant_id) is not uuid.UUID:
            return None
        return await self._repository.admitted(context, plant_id, PredicateFamily(family))
