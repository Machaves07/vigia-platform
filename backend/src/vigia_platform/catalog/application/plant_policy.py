"""Política de hallazgos incerrables de la planta (LC-GOB-03; DE §2.10; BR-GOB-21, 22).

``sign_policy`` (``POST /plants/{plant_id}/policy``, ``commissioning.run`` sobre la planta):

1. **Antes de la transacción**: planta de la organización y dentro del alcance (si no,
   ``ResourceNotFound``); la clave se exige **antes** de verificar el documento, que no autoriza;
   textos por la política de texto libre (``free_text_rejected``); ``verify_document_refs`` del
   ``document_ref`` obligatorio, de ``kind = plant_policy``.
2. **En una sola transacción**: exclusión de la planta (``pg_advisory_xact_lock``); la versión
   que toca es la última más uno: un ``version`` distinto en el cuerpo es ``PolicyVersionConflict``
   (``conflict``) y no escribe nada; ``plant_policy_signed`` (``source_key = policy_id``), la fila
   nueva (la anterior permanece) y el documento a ``used``.

Dos cargas simultáneas de la misma planta se ordenan por el candado: la segunda lee la versión que
dejó la primera y, con el mismo número en el cuerpo, recibe ``conflict``. La restricción única
``(plant_id, version)`` es el respaldo (transitorio, nunca dos con el mismo número).

``policy`` (``GET /plants/{plant_id}/policy``, ``catalog.read``): la última versión cargada o
``None`` (``loaded = false``). Bajo concesión, auditada en la misma transacción (A-56).
"""

from __future__ import annotations

import dataclasses
import os
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Final

from sqlalchemy import exc as sa_exc

from vigia_platform.catalog.adapters.postgres.plant_policy_repository import (
    PLANT_POLICY_VERSION_UNIQUE,
    PostgresPlantPolicyRepository,
)
from vigia_platform.catalog.application.admission import CatalogRejected
from vigia_platform.catalog.application.documents import DocumentService
from vigia_platform.catalog.application.gates import LedgerRaceLost, record_id_of
from vigia_platform.catalog.detail_codes import CatalogDetailCode
from vigia_platform.catalog.domain.documents import DocumentRef
from vigia_platform.catalog.domain.enums import DocumentKind
from vigia_platform.catalog.domain.plant_policy import (
    MAX_CRITERIA_SUMMARY_CHARS,
    MAX_DISPLAY_NAME_CHARS,
    MAX_LEGAL_REFERENCE_CHARS,
    MAX_POLICY_VERSION,
    PlantPolicy,
    PlantPolicyRequest,
    next_version,
)
from vigia_platform.catalog.domain.texts import has_content
from vigia_platform.catalog.domain.time_windows import utc_instant
from vigia_platform.identity.authz.authorize import Authorizer, Resource, ResourceNotFound
from vigia_platform.identity.authz.context import with_unit
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import AuditOperation, AuditWriter
from vigia_platform.ledger.application.writer import (
    EscritorExpediente,
    LedgerDatabase,
    RecordScope,
    violated_constraint,
)
from vigia_platform.ledger.free_text import FreeTextField, FreeTextPolicyRegistry, FreeTextRejected
from vigia_platform.shared.api.errors import ApiErrorCode, ExternalDependencyDown
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext, repository
from vigia_platform.shared.ids import uuid7

__all__ = [
    "PLANT_POLICY_SIGNED",
    "PlantPolicyService",
    "PolicyRequestInvalid",
    "PolicyUnavailable",
    "PolicyVersionConflict",
]

PLANT_POLICY_SIGNED: Final = "plant_policy_signed"
_UNIQUE_VIOLATION: Final = "23505"

_DISPLAY_NAME: Final = FreeTextField(
    PLANT_POLICY_SIGNED, "/signed_by_display_name", 1, MAX_DISPLAY_NAME_CHARS
)
_LEGAL_REFERENCE: Final = FreeTextField(
    PLANT_POLICY_SIGNED, "/legal_opinion_reference", 1, MAX_LEGAL_REFERENCE_CHARS
)
_CRITERIA: Final = FreeTextField(
    PLANT_POLICY_SIGNED, "/criteria_summary_es", 1, MAX_CRITERIA_SUMMARY_CHARS
)


class PolicyVersionConflict(Exception):
    """El ``version`` del cuerpo no es el que toca: ``conflict`` sin ``detail_code``."""

    api_code: Final = ApiErrorCode.CONFLICT

    def __init__(self) -> None:
        super().__init__("la versión de la política no es la siguiente de la planta")


class PolicyRequestInvalid(Exception):
    """El cuerpo incumple un límite (versión, fecha): ``invalid_request``."""

    api_code: Final = ApiErrorCode.INVALID_REQUEST

    def __init__(self, reason: str) -> None:
        super().__init__(reason)


class PolicyUnavailable(ExternalDependencyDown):
    """Transitorio: otra carga confirmó el mismo número (respaldo del candado)."""


@repository
class PlantPolicyService:
    """``catalog.gates`` (política de planta): cargar una versión y leer la vigente."""

    def __init__(
        self,
        *,
        repository: PostgresPlantPolicyRepository,
        documents: DocumentService,
        database: LedgerDatabase,
        writer: EscritorExpediente,
        authorizer: Authorizer,
        audit: AuditWriter,
        free_text: FreeTextPolicyRegistry,
        clock: Clock,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._repository = repository
        self._documents = documents
        self._database = database
        self._writer = writer
        self._authorizer = authorizer
        self._audit = audit
        self._free_text = free_text
        self._clock = clock
        self._random_bytes = random_bytes

    def __repr__(self) -> str:
        return "PlantPolicyService()"

    async def _plant(
        self, context: ScopeContext, plant_id: uuid.UUID, key: PermissionKey
    ) -> ScopeContext:
        """El contexto autorizado sobre la planta; inexistente o fuera de alcance, igual."""
        if not isinstance(context, ScopeContext) or type(plant_id) is not uuid.UUID:
            raise ResourceNotFound()
        if not await self._repository.plant_exists(context, plant_id):
            raise ResourceNotFound()
        return await self._authorizer.authorize(
            context, key, Resource.plant(context.organization_id, plant_id)
        )

    def _text(self, value: object, field: FreeTextField) -> str:
        if not isinstance(value, str):
            raise PolicyRequestInvalid("texto esperado")
        try:
            text = self._free_text.apply(value, field)
        except FreeTextRejected:
            raise CatalogRejected(CatalogDetailCode.FREE_TEXT_REJECTED) from None
        if not has_content(text):
            raise CatalogRejected(CatalogDetailCode.FREE_TEXT_REJECTED)
        return text

    async def sign_policy(
        self, context: ScopeContext, plant_id: uuid.UUID, request: PlantPolicyRequest
    ) -> PlantPolicy:
        """Carga la versión siguiente de la política; la devuelve tras confirmar.

        ``ResourceNotFound``, ``PolicyRequestInvalid``, ``CatalogRejected(free_text_rejected)``,
        ``DocumentRequestInvalid``, ``StorageUnavailable``, ``PolicyVersionConflict`` o
        ``PolicyUnavailable``; en todos, nada queda escrito.
        """
        if not isinstance(request, PlantPolicyRequest):
            raise TypeError("request debe ser PlantPolicyRequest")
        authorized = await self._plant(context, plant_id, PermissionKey.COMMISSIONING_RUN)
        version = request.version
        if type(version) is not int or not 1 <= version <= MAX_POLICY_VERSION:
            raise PolicyRequestInvalid("version es un entero de 1 a 2^31 - 1")
        if not isinstance(request.signed_at, datetime) or request.signed_at.utcoffset() is None:
            raise PolicyRequestInvalid("signed_at es un instante con zona horaria")
        signed_at = utc_instant(request.signed_at)
        display_name = self._text(request.signed_by_display_name, _DISPLAY_NAME)
        legal_reference = self._text(request.legal_opinion_reference, _LEGAL_REFERENCE)
        criteria = self._text(request.criteria_summary_es, _CRITERIA)
        document = DocumentRef.parse(request.document_ref)
        verified = await self._documents.verify_document_refs(
            authorized, [document], {DocumentKind.PLANT_POLICY}, plant_id
        )
        writer_context = with_unit(authorized, ActorUnit.U03)
        policy_id = uuid7(self._clock, self._random_bytes)
        loaded_by = uuid.UUID(str(authorized.actor.id))
        try:
            async with self._database.transaction(writer_context) as transaction:
                await self._repository.lock_plant(transaction, plant_id)
                latest = await self._repository.latest(transaction, plant_id)
                if version != next_version(None if latest is None else latest.version):
                    raise PolicyVersionConflict
                now = utc_instant(self._clock.now())
                draft = PlantPolicy(
                    policy_id=policy_id,
                    organization_id=authorized.organization_id,
                    plant_id=plant_id,
                    version=version,
                    signed_at=signed_at,
                    signed_by_display_name=display_name,
                    legal_opinion_reference=legal_reference,
                    criteria_summary_es=criteria,
                    document_ref=document,
                    loaded_by=loaded_by,
                    loaded_at=now,
                    # Provisional: el identificador real lo da el recibo de abajo.
                    ledger_record_id=policy_id,
                )
                written = await self._writer.write(
                    writer_context,
                    PLANT_POLICY_SIGNED,
                    draft.record_content(),
                    scope=RecordScope(plant_id=plant_id),
                    occurred_at=now,
                    transaction=transaction,
                )
                policy = dataclasses.replace(draft, ledger_record_id=record_id_of(written))
                await self._repository.insert(transaction, policy)
                await self._documents.mark_used(transaction, verified)
        except LedgerRaceLost:
            raise PolicyUnavailable("plant_policy_race") from None
        except sa_exc.IntegrityError as error:
            if violated_constraint(error, _UNIQUE_VIOLATION) == PLANT_POLICY_VERSION_UNIQUE:
                raise PolicyUnavailable("plant_policy_race") from None
            raise
        return policy

    async def policy(self, context: ScopeContext, plant_id: uuid.UUID) -> PlantPolicy | None:
        """La versión vigente (la última cargada) o ``None``; ``catalog.read`` sobre la planta."""
        authorized = await self._plant(context, plant_id, PermissionKey.CATALOG_READ)
        async with self._database.transaction(authorized) as transaction:
            latest = await self._repository.latest(transaction, plant_id)
            if authorized.concession_id is not None:
                # BR-NUC-38 y A-56: la lectura del proveedor, auditada (fallo cerrado).
                await self._audit.append(
                    authorized,
                    AuditOperation.CATALOG_READ,
                    plant_id=plant_id,
                    result_count=1,
                    transaction=transaction,
                )
        return latest
