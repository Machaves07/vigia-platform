"""``EvidencePort``: la lectura de un clip y el resultado de su marca (LC-NUC-15 parte 2).

Operaciones del puerto (business-logic-model §10.1; BR-NUC-66; pendiente nº 21, adenda A-14):

- ``url_lectura(context, evidence_id)``: la única forma de leer un clip (BR-NUC-66). Devuelve un
  ``EvidenceReadGrant`` con una URL prefirmada de **solo lectura** vigente a lo sumo 5 minutos,
  y cada concesión deja una entrada de auditoría ``evidence_read_granted``. No existe listado de
  objetos ni URL permanente.
- ``resultados_marca(context, evidence_ids)``: el resultado de la verificación diferida de la
  marca (``pending``, ``intact`` o ``broken``) de muchas evidencias en **una sola consulta**, para
  la bandeja y la exportación de U-04 (marca ``evidence_flagged``, BR-LAZ-150).

**Permiso ``evidence.read`` sobre la zona.** La matriz (``domain-entities.md`` §2.6) lo da solo a
``coordinator_sst`` y ``plant_manager``: la zona de la evidencia tiene que estar cubierta por una
asignación de ``allowed_scopes`` con uno de esos roles (la organización, su planta o la zona).
El administrador, el mando de línea, el instalador del proveedor y el COPASST no leen clips
(RF-PLA-11). Una evidencia que no existe, es de otra organización o está fuera de ese alcance da
``EvidenceNotFound`` (``not_found``, nunca ``forbidden``); el intento queda auditado como
``evidence_read_granted`` con resultado ``denied`` y ``result_count`` 0. La ruta
(``POST /evidence/{id}/read-url``, TASK-137) aplica además ``authorize``.

**La versión verificada.** La evidencia no guarda el ``version_id`` del objeto, así que antes de
firmar se consulta el objeto (``HEAD``) y la URL se fija a la versión vigente **solo si** su suma
SHA-256 de objeto entero y su tamaño son los verificados al registrar (``Evidence.sha256``,
``size_bytes``). Si el objeto falta, sus bytes no son los verificados o el almacén no devuelve
``version_id`` (la URL no quedaría fijada a una versión) no se concede nada
(``EvidenceUnreadable``, auditado con resultado ``error``): la URL nunca sirve otros bytes que
los del expediente. El ``HEAD`` va fuera de toda transacción (PAT-NUC-RES-08).

``resultados_marca`` filtra por ``allowed_scopes`` como ``LectorExpediente`` (sin mirar el rol:
la bandeja y la exportación ya exigen su propio permiso) y omite lo que no existe o está fuera
de alcance. No escribe auditoría: solo devuelve identificador, enumeración y marca, nunca
contenido ni URL, y lo invocan lecturas de U-04 que ya auditan la suya.

Una entrada inválida lanza ``EvidenceQueryInvalid`` antes de tocar la base; sin contexto,
``ContextAbsent``.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

from sqlalchemy import text
from sqlalchemy.engine import Row

from vigia_platform.ledger.application.audit_writer import (
    AuditOperation,
    AuditOutcome,
    AuditWriter,
    ResourceRef,
)
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.context import ContextAbsent, Role, ScopeContext, ScopeLevel, repository
from vigia_platform.shared.storage import (
    PRESIGN_GET_MAX_TTL,
    ObjectHead,
    PresignedRequest,
)

__all__ = [
    "EVIDENCE_READ_ROLES",
    "EVIDENCE_RESOURCE_KIND",
    "MAX_MARKER_BATCH",
    "READ_URL_TTL",
    "EvidenceNotFound",
    "EvidencePort",
    "EvidenceQueryInvalid",
    "EvidenceReadGrant",
    "EvidenceReadStorage",
    "EvidenceService",
    "EvidenceUnreadable",
    "MarkerResult",
    "MarkerVerification",
]

READ_URL_TTL: Final = PRESIGN_GET_MAX_TTL
"""Vigencia de la URL de lectura: 5 minutos, el máximo de BR-NUC-66."""

EVIDENCE_READ_ROLES: Final[frozenset[Role]] = frozenset({Role.COORDINATOR_SST, Role.PLANT_MANAGER})
"""Roles con ``evidence.read`` en la matriz (§2.6). La matriz en código llega con TASK-125."""

EVIDENCE_RESOURCE_KIND: Final = "evidence"
"""``resource_ref.kind`` de las entradas de auditoría de una evidencia."""

MAX_MARKER_BATCH: Final = 1000
"""Evidencias como mucho por llamada a ``resultados_marca`` ``[objetivo propio]``."""


class EvidenceQueryInvalid(ValueError):
    """Identificador o lote inválidos: no se consulta ni se audita nada."""

    code: Final = "query_invalid"

    def __init__(self, detail: str) -> None:
        super().__init__(f"consulta de evidencias inválida: {detail}")


class EvidenceNotFound(LookupError):
    """La evidencia no existe o está fuera del alcance de lectura (``not_found``)."""

    code: Final = "not_found"

    def __init__(self) -> None:
        super().__init__("evidencia no encontrada")


class EvidenceUnreadable(Exception):
    """El objeto falta o sus bytes no son los verificados: no se concede la lectura."""

    code: Final = "evidence_unreadable"

    def __init__(self) -> None:
        super().__init__("el objeto de la evidencia no coincide con lo verificado")


class MarkerResult(enum.StrEnum):
    """``marker_verification_result`` (pendiente nº 21 ampliado)."""

    PENDING = "pending"
    INTACT = "intact"
    BROKEN = "broken"


@dataclass(frozen=True, slots=True)
class EvidenceReadGrant:
    """``EvidenceReadGrant`` (§3.5): no se persiste. La URL es un secreto de corta vida."""

    evidence_id: uuid.UUID
    url: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class MarkerVerification:
    """El resultado de la marca de una evidencia; las marcas son ``None`` mientras ``pending``."""

    result: MarkerResult
    marker_verified_at: datetime | None
    container_marker_sampled_at: datetime | None


class EvidenceReadStorage(Protocol):
    """Lo que ``url_lectura`` usa de ``StoragePort``: ``head_object`` y ``presign_get``."""

    async def head_object(self, key: str) -> ObjectHead | None: ...

    async def presign_get(
        self, key: str, ttl: timedelta = ..., *, version_id: str | None = None
    ) -> PresignedRequest: ...


class EvidencePort(Protocol):
    """Puerto ``EvidencePort`` (business-logic-model §10.1), parte de lectura."""

    async def url_lectura(
        self, context: ScopeContext, evidence_id: uuid.UUID
    ) -> EvidenceReadGrant: ...

    async def resultados_marca(
        self, context: ScopeContext, evidence_ids: Iterable[uuid.UUID]
    ) -> Mapping[uuid.UUID, MarkerVerification]: ...


# --- Alcance ------------------------------------------------------------------------------------


def _scope_parameters(context: ScopeContext, roles: frozenset[Role] | None) -> dict[str, Any]:
    """``allowed_scopes`` como parámetros; con ``roles``, solo las asignaciones de esos roles."""
    scopes = [s for s in context.allowed_scopes if roles is None or s.role in roles]
    whole = any(
        s.scope_level is ScopeLevel.ORGANIZATION and s.scope_id == context.organization_id
        for s in scopes
    )
    return {
        "whole_organization": whole,
        "scope_plants": list(
            dict.fromkeys(s.scope_id for s in scopes if s.scope_level is ScopeLevel.PLANT)
        ),
        "scope_zones": list(
            dict.fromkeys(s.scope_id for s in scopes if s.scope_level is ScopeLevel.ZONE)
        ),
    }


_EVIDENCE_TO_READ: Final = text(
    "SELECT e.evidence_id, e.plant_id, e.zone_id, e.storage_key, e.sha256, e.size_bytes"
    " FROM ledger.evidence AS e"
    " WHERE e.organization_id = :organization_id AND e.evidence_id = :evidence_id"
    " AND (CAST(:whole_organization AS boolean)"
    " OR e.plant_id = ANY(CAST(:scope_plants AS uuid[]))"
    " OR e.zone_id = ANY(CAST(:scope_zones AS uuid[])))"
)

_MARKER_RESULTS: Final = text(
    "SELECT e.evidence_id, e.marker_verification_result, e.marker_verified_at,"
    " e.container_marker_sampled_at"
    " FROM ledger.evidence AS e"
    " WHERE e.organization_id = :organization_id"
    " AND e.evidence_id = ANY(CAST(:evidence_ids AS uuid[]))"
    " AND (CAST(:whole_organization AS boolean)"
    " OR e.plant_id = ANY(CAST(:scope_plants AS uuid[]))"
    " OR e.zone_id = ANY(CAST(:scope_zones AS uuid[])))"
)


def _require_context(context: object) -> ScopeContext:
    if not isinstance(context, ScopeContext):
        raise ContextAbsent()
    return context


def _evidence_id(value: object) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise EvidenceQueryInvalid("evidence_id debe ser uuid.UUID")
    return uuid.UUID(int=value.int)


def _plain(value: uuid.UUID) -> uuid.UUID:
    """``uuid.UUID`` exacto (asyncpg devuelve una subclase propia; la auditoría exige el tipo)."""
    return uuid.UUID(int=value.int)


def _verified_bytes(row: Row[Any], head: ObjectHead | None) -> bool:
    return (
        head is not None
        and head.size_bytes == int(row.size_bytes)
        and head.full_object_sha256_hex == row.sha256
    )


# --- El puerto --------------------------------------------------------------------------------


@repository
class EvidenceService:
    """``EvidencePort`` sobre PostgreSQL y el depósito de evidencias."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        audit: AuditWriter,
        storage: EvidenceReadStorage,
    ) -> None:
        self._database = database
        self._audit = audit
        self._storage = storage

    async def url_lectura(self, context: ScopeContext, evidence_id: uuid.UUID) -> EvidenceReadGrant:
        """URL de solo lectura ≤ 5 min de la versión verificada; auditada (BR-NUC-66)."""
        context = _require_context(context)
        requested = _evidence_id(evidence_id)
        resource = ResourceRef(EVIDENCE_RESOURCE_KIND, requested)
        rows = await self._database.read(
            context,
            _EVIDENCE_TO_READ,
            {
                **_scope_parameters(context, EVIDENCE_READ_ROLES),
                "organization_id": context.organization_id,
                "evidence_id": requested,
            },
        )
        if not rows:
            await self._audit.append(
                context,
                AuditOperation.EVIDENCE_READ_GRANTED,
                outcome=AuditOutcome.DENIED,
                resource=resource,
                result_count=0,
            )
            raise EvidenceNotFound()
        row = rows[0]
        plant_id, zone_id = _plain(row.plant_id), _plain(row.zone_id)
        head = await self._storage.head_object(row.storage_key)
        # Sin ``version_id`` la URL no quedaría fijada a la versión verificada y serviría
        # cualquier versión posterior del objeto: se falla cerrado (seguimiento de VIG-65).
        if head is None or head.version_id is None or not _verified_bytes(row, head):
            await self._audit.append(
                context,
                AuditOperation.EVIDENCE_READ_GRANTED,
                outcome=AuditOutcome.ERROR,
                plant_id=plant_id,
                zone_id=zone_id,
                resource=resource,
                result_count=0,
            )
            raise EvidenceUnreadable()
        presigned = await self._storage.presign_get(
            row.storage_key, READ_URL_TTL, version_id=head.version_id
        )
        # Si la entrada no se escribe, la URL no sale de aquí.
        await self._audit.append(
            context,
            AuditOperation.EVIDENCE_READ_GRANTED,
            plant_id=plant_id,
            zone_id=zone_id,
            resource=resource,
            result_count=1,
        )
        return EvidenceReadGrant(requested, presigned.url, presigned.expires_at)

    async def resultados_marca(
        self, context: ScopeContext, evidence_ids: Iterable[uuid.UUID]
    ) -> Mapping[uuid.UUID, MarkerVerification]:
        """Resultado de la marca por evidencia, en una sola consulta; omite lo que no se ve."""
        context = _require_context(context)
        given: object = evidence_ids
        if isinstance(given, str | bytes) or not isinstance(given, Iterable):
            raise EvidenceQueryInvalid("evidence_ids debe ser una colección de uuid.UUID")
        requested: dict[uuid.UUID, None] = {}
        for value in given:
            requested[_evidence_id(value)] = None
            if len(requested) > MAX_MARKER_BATCH:
                raise EvidenceQueryInvalid(
                    f"resultados_marca admite como mucho {MAX_MARKER_BATCH} evidencias"
                )
        if not requested:
            return {}
        rows = await self._database.read(
            context,
            _MARKER_RESULTS,
            {
                **_scope_parameters(context, None),
                "organization_id": context.organization_id,
                "evidence_ids": list(requested),
            },
        )
        return {
            _plain(row.evidence_id): MarkerVerification(
                result=MarkerResult(row.marker_verification_result),
                marker_verified_at=row.marker_verified_at,
                container_marker_sampled_at=row.container_marker_sampled_at,
            )
            for row in rows
        }
