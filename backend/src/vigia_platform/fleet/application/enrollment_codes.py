"""Códigos de alta e intentos de alta (TASK-218; BR-GOB-58 a 61; BLM §3.4; PAT-GOB-SEG-02, SEG-08).

**Emisión** (``issue``; ``POST /nodes/{node_id}/enrollment-codes``, ``commissioning.run`` sobre la
planta del nodo), en una transacción:

- elegibilidad (``code_eligibility``): reemisión con el nodo ``declared`` o
  ``re_enrollment_pending``; **re-alta** del mismo ``node_id`` desde ``revoked`` deliberado o con
  la credencial vencida, revocada o ``superseded``: el nodo pasa a ``re_enrollment_pending`` por
  ``IdentityCommandPort.update_node`` y la credencial que siga ``active``/``overlapping`` se revoca
  con la marca de la lista; cualquier otro caso (incluida la baja) → ``fleet_node_not_declared``;
- el ``active`` anterior pasa a ``superseded`` y se anexa el nuevo con su sal y su hash; registro
  ``enrollment_code_issued`` (``source_key = code_id``, **nunca** el código ni su hash) y
  auditoría. **A lo sumo un ``active`` por nodo** lo garantiza el índice único parcial: dos
  emisiones simultáneas chocan en él y la perdedora se revierte entera (``ConcurrentIssue``,
  ``conflict``). No se reintenta dentro: un reintento con la ficha bloqueada se cruzaría con la
  exclusión de la cadena del expediente que tiene la otra (bloqueo mutuo);
- la respuesta lleva el código en claro (**la única vez**: ``disclosed_at`` es ese instante), su
  vencimiento (24 h) y las huellas SHA-256 de las raíces publicadas en ``ca/root.pem``
  (``node_ca_root_sha256``: una, o dos durante una sustitución de raíz; D-6).

**Verificación para el alta** (la usa la ruta del contrato de VIG-151, con el ``EnrollmentScope``
que resuelve ``node_api`` por el ``node_id`` del nombre común de la CSR, A-51): ``verify`` compara
el presentado en tiempo constante con los códigos de **ese** nodo (``check_presented``);
``consume(transaction, code_id, now)`` es el ``UPDATE … WHERE status = 'active' AND expires_at >
now`` cuyo éxito es una fila, para componerlo con la emisión del certificado en **una**
transacción.

**Intentos** (``register_attempt``): todo intento presentado deja ``EnrollmentAttempt`` con el
``sha256`` del código presentado, la huella, las versiones, el ``correlation_id`` de la plataforma
y el origen como HMAC con la clave estable (``SourceIpHasher``); el rechazo escribe además
``enrollment_attempt_rejected`` en la cadena de la planta. Un ``node_id`` que no es de ningún nodo
declarado no tiene organización: no deja fila ni registro de cliente, solo la métrica
``enrollment_attempts_total`` (``reason = node_unknown``) y un registro estructurado con el
``source_ip_tag`` (los 16 primeros hexadecimales de ``source_ip_hash``: un valor de 64 no sale
nunca en un registro, PR-GOB-31; lectura del redactor de TASK-218). ``attempts`` lista los
intentos de un nodo (``fleet.read``), el más reciente primero.

El código en claro solo existe en la memoria de la emisión y en la respuesta 201: nunca en la
base, el expediente, la auditoría, los eventos, los registros estructurados ni las métricas
(NFR-GOB-31, BR-GOB-59).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Protocol

from sqlalchemy import exc as sa_exc

from vigia_platform.fleet.adapters.postgres.enrollment_store import (
    ONE_ACTIVE_PER_NODE,
    AttemptCursor,
)
from vigia_platform.fleet.application.common import (
    FleetDependencies,
    FleetRejected,
    authorized_node,
    write,
)
from vigia_platform.fleet.application.node_revocation import revoke_credentials_in
from vigia_platform.fleet.detail_codes import FleetDetailCode
from vigia_platform.fleet.domain.enrollment_attempt import EnrollmentAttempt, SourceIpHasher
from vigia_platform.fleet.domain.enrollment_code import (
    VALIDITY,
    CodeCheck,
    EnrollmentCode,
    check_presented,
    generate_code,
    new_salt,
    presented_code_hash,
    salted_hash,
)
from vigia_platform.fleet.domain.enums import EnrollmentAttemptResult, EnrollmentCodeStatus
from vigia_platform.fleet.domain.node_fleet_record import CodeEligibility, code_eligibility
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.identity.authz.context import EnrollmentScope, with_unit
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import AuditOperation, ResourceRef
from vigia_platform.ledger.application.writer import violated_constraint
from vigia_platform.shared.context import ActorUnit, ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.node_ca import (
    ROOT_CERTIFICATE_KEY,
    NodeCaError,
    fingerprint,
    read_bundle,
)
from vigia_platform.shared.observability import redaction
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "ATTEMPT_RECORD_TYPE",
    "ISSUED_RECORD_TYPE",
    "MAX_ATTEMPTS_PAGE",
    "AttemptPage",
    "AttemptRequest",
    "BundleRoots",
    "ConcurrentIssue",
    "EnrollmentCodeService",
    "IssuedCode",
    "NodeCaRoots",
    "RootsUnavailable",
]

ISSUED_RECORD_TYPE: Final = "enrollment_code_issued"
ATTEMPT_RECORD_TYPE: Final = "enrollment_attempt_rejected"
MAX_ATTEMPTS_PAGE: Final = 200
"""Página máxima de ``GET /nodes/{node_id}/enrollment-attempts``."""
_UNIQUE_VIOLATION: Final = "23505"

_DECLARED: Final = "node_declared"
_UNKNOWN: Final = "node_unknown"
"""``reason`` de ``enrollment_attempts_total``: si el ``node_id`` era de un nodo declarado."""
# ``result`` viaja como miembro de ``EnrollmentAttemptResult`` (la política acepta su valor).
redaction.DEFAULT_POLICY.register("reason", (_DECLARED, _UNKNOWN))

_log = get_logger("fleet.enrollment")


class ConcurrentIssue(Exception):
    """Dos emisiones simultáneas para el mismo nodo: el índice de un ``active`` por nodo dejó
    pasar la otra y esta se revirtió entera (``conflict``; repetirla emite otro código)."""


class RootsUnavailable(Exception):
    """``ca/root.pem`` no se pudo leer o no es un paquete válido: no se emite el código."""


class NodeCaRoots(Protocol):
    """Las huellas de las raíces de ``vigia-node-ca`` publicadas en ``ca/root.pem``."""

    async def fingerprints(self) -> tuple[str, ...]: ...


class _RootObjects(Protocol):
    async def get_object(self, key: str, *, version_id: str | None = None) -> bytes: ...


@dataclass(frozen=True, slots=True)
class BundleRoots:
    """``NodeCaRoots`` sobre el depósito ``vigia-edge`` (``shared.node_ca``: ``read_bundle``)."""

    storage: _RootObjects
    object_key: str = ROOT_CERTIFICATE_KEY

    async def fingerprints(self) -> tuple[str, ...]:
        try:
            bundle = read_bundle(await self.storage.get_object(self.object_key))
        except NodeCaError:
            raise RootsUnavailable("ca/root.pem no es un paquete de raíces válido") from None
        except Exception:
            raise RootsUnavailable("ca/root.pem no se pudo leer") from None
        return tuple(fingerprint(root) for root in bundle)


@dataclass(frozen=True, slots=True)
class IssuedCode:
    """La respuesta 201 de la emisión: la **única** vez que existe el código en claro."""

    code: str = field(repr=False)
    code_id: uuid.UUID
    node_id: uuid.UUID
    issued_at: datetime
    expires_at: datetime
    disclosed_at: datetime
    node_ca_root_sha256: tuple[str, ...]
    re_enrollment: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class AttemptRequest:
    """Lo que el alta presenta (``NodeEnrollmentRequest`` ya validado) y su origen de red."""

    presented_code: str = field(repr=False)
    hardware_fingerprint: str
    software_version: str
    contract_version: str
    source_address: str | None = field(default=None, repr=False)
    correlation_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class AttemptPage:
    items: tuple[EnrollmentAttempt, ...]
    next_cursor: AttemptCursor | None


@repository
class EnrollmentCodeService:
    """Emitir, verificar y consumir códigos de alta; registrar y listar los intentos."""

    def __init__(
        self,
        deps: FleetDependencies,
        *,
        roots: NodeCaRoots,
        source_hasher: SourceIpHasher | None = None,
    ) -> None:
        self._deps = deps
        self._roots = roots
        self._source_hasher = source_hasher

    def __repr__(self) -> str:
        return "EnrollmentCodeService()"

    # --- Emisión ---------------------------------------------------------------------------------

    async def issue(self, context: ScopeContext, node_id: uuid.UUID) -> IssuedCode:
        """Emite un código nuevo para el nodo (reemisión o re-alta); el anterior queda
        ``superseded``. ``fleet_node_not_declared`` si el nodo no admite código."""
        deps = self._deps
        authorized, _ = await authorized_node(
            deps, context, node_id, PermissionKey.COMMISSIONING_RUN
        )
        roots = await self._roots.fingerprints()
        writer = with_unit(authorized, ActorUnit.U03)
        try:
            return await self._issue(authorized, writer, node_id, roots)
        except sa_exc.IntegrityError as error:
            if violated_constraint(error, _UNIQUE_VIOLATION) == ONE_ACTIVE_PER_NODE:
                # Otra emisión simultánea para el mismo nodo ganó en el índice: esta no escribió
                # nada (ni código ni registro) y se puede repetir.
                raise ConcurrentIssue() from None
            raise

    async def _issue(
        self,
        authorized: ScopeContext,
        writer: ScopeContext,
        node_id: uuid.UUID,
        roots: tuple[str, ...],
    ) -> IssuedCode:
        deps = self._deps
        now = deps.clock.now()
        async with deps.database.transaction(writer) as transaction:
            node = await deps.nodes.read(transaction, node_id)
            if node is None:
                raise ResourceNotFound()
            credentials = await deps.nodes.credentials(transaction, node_id)
            eligibility = code_eligibility(node.status, node.record, credentials, now)
            if eligibility is CodeEligibility.RE_ENROLLMENT:
                # La re-alta cambia el nodo: se decide otra vez con la ficha bloqueada.
                node = await deps.nodes.lock(transaction, node_id)
                if node is None:
                    raise ResourceNotFound()
                credentials = await deps.nodes.credentials(transaction, node_id)
                eligibility = code_eligibility(node.status, node.record, credentials, now)
            if eligibility is CodeEligibility.REJECTED:
                raise FleetRejected(FleetDetailCode.NODE_NOT_DECLARED)
            if eligibility is CodeEligibility.RE_ENROLLMENT:
                await deps.identity.update_node(
                    authorized,
                    node_id,
                    "re_enrollment_pending",
                    node.live_view_local_url,
                    transaction=transaction,
                )
                await revoke_credentials_in(deps, transaction, node_id, now)
                if node.record.revoked:
                    await deps.nodes.clear_revocation(transaction, node_id)
            await deps.enrollment.supersede_active(transaction, node_id)
            code = generate_code()
            salt = new_salt()
            code_id = uuid7(deps.clock, deps.random_bytes)
            expires_at = now + VALIDITY
            receipt = await write(
                deps,
                writer,
                transaction,
                ISSUED_RECORD_TYPE,
                {
                    "code_id": str(code_id),
                    "node_id": str(node_id),
                    "issued_at": format_timestamp(now),
                    "expires_at": format_timestamp(expires_at),
                    "disclosed_at": format_timestamp(now),
                    "issued_by": str(authorized.actor.id),
                },
                plant_id=node.plant_id,
                occurred_at=now,
            )
            await deps.enrollment.insert_code(
                transaction,
                EnrollmentCode(
                    code_id=code_id,
                    organization_id=authorized.organization_id,
                    plant_id=node.plant_id,
                    node_id=node_id,
                    code_hash=salted_hash(salt, code),
                    code_salt=salt,
                    issued_at=now,
                    issued_by=authorized.actor.id,
                    expires_at=expires_at,
                    disclosed_at=now,
                    status=EnrollmentCodeStatus.ACTIVE,
                    ledger_record_id=receipt.record_id,
                ),
            )
            await deps.audit.append(
                writer,
                AuditOperation.ENROLLMENT_CODE_ISSUED,
                plant_id=node.plant_id,
                resource=ResourceRef("node", node_id),
                transaction=transaction,
            )
        return IssuedCode(
            code=code,
            code_id=code_id,
            node_id=node_id,
            issued_at=now,
            expires_at=expires_at,
            disclosed_at=now,
            node_ca_root_sha256=roots,
            re_enrollment=eligibility is CodeEligibility.RE_ENROLLMENT,
        )

    # --- Verificación y consumo (alta, VIG-151) --------------------------------------------------

    async def verify(self, enrollment: EnrollmentScope, presented: object) -> CodeCheck:
        """El código presentado frente a los del nodo del alta, en tiempo constante."""
        if not isinstance(enrollment, EnrollmentScope):
            raise TypeError("enrollment debe ser el EnrollmentScope del alta")
        deps = self._deps
        async with deps.database.transaction(enrollment.context) as transaction:
            codes = await deps.enrollment.codes(transaction, enrollment.node_id)
        return check_presented(codes, presented, deps.clock.now())

    async def consume(self, transaction: Transaction, code_id: uuid.UUID, now: datetime) -> bool:
        """``active → used`` si sigue ``active`` y vigente; ``True`` solo con una fila afectada."""
        return await self._deps.enrollment.consume(transaction, code_id, now)

    # --- Intentos --------------------------------------------------------------------------------

    def _hasher(self) -> SourceIpHasher:
        if self._source_hasher is None:
            raise RuntimeError("falta la clave estable del hash de origen (SourceIpHasher)")
        return self._source_hasher

    async def register_attempt(
        self,
        enrollment: EnrollmentScope | None,
        request: AttemptRequest,
        result: EnrollmentAttemptResult,
        *,
        transaction: Transaction | None = None,
    ) -> EnrollmentAttempt | None:
        """Deja el intento (y, si es un rechazo, ``enrollment_attempt_rejected``).

        Sin ``enrollment`` (``node_id`` de ningún nodo declarado) no hay organización: solo
        métrica y registro estructurado, y devuelve ``None``. Con ``transaction`` (la del alta
        aceptada, VIG-151) va en ella; si no, en una propia con el contexto del alta.
        """
        result = EnrollmentAttemptResult(result)
        deps = self._deps
        source_ip_hash = self._hasher().hash(request.source_address)
        now = deps.clock.now()
        metrics = deps.platform_metrics()
        if enrollment is None:
            if result is EnrollmentAttemptResult.ACCEPTED:
                raise ValueError("un intento aceptado es de un nodo declarado")
            metrics.enrollment_attempts_total.add(1, {"result": result, "reason": _UNKNOWN})
            _log.warning(
                "intento de alta de un nodo no declarado",
                result=result.value,
                source_ip_tag=source_ip_hash[:16],
                correlation_id=str(request.correlation_id),
            )
            return None
        if not isinstance(enrollment, EnrollmentScope):
            raise TypeError("enrollment debe ser el EnrollmentScope del alta")
        draft = EnrollmentAttempt(
            attempt_id=uuid7(deps.clock, deps.random_bytes),
            organization_id=enrollment.context.organization_id,
            plant_id=enrollment.plant_id,
            node_id=enrollment.node_id,
            presented_code_hash=presented_code_hash(request.presented_code),
            hardware_fingerprint=request.hardware_fingerprint,
            software_version=request.software_version,
            contract_version=request.contract_version,
            result=result,
            attempted_at=now,
            source_ip_hash=source_ip_hash,
            correlation_id=request.correlation_id,
        )
        if transaction is None:
            async with deps.database.transaction(enrollment.context) as own:
                attempt = await self._insert_attempt(enrollment, own, draft)
        else:
            attempt = await self._insert_attempt(enrollment, transaction, draft)
        metrics.enrollment_attempts_total.add(1, {"result": result, "reason": _DECLARED})
        return attempt

    async def _insert_attempt(
        self, enrollment: EnrollmentScope, transaction: Transaction, draft: EnrollmentAttempt
    ) -> EnrollmentAttempt:
        deps = self._deps
        attempt = draft
        if draft.rejected:
            receipt = await write(
                deps,
                enrollment.context,
                transaction,
                ATTEMPT_RECORD_TYPE,
                {
                    "attempt_id": str(draft.attempt_id),
                    "node_id": str(draft.node_id),
                    "result": draft.result.value,
                    "hardware_fingerprint": draft.hardware_fingerprint,
                    "attempted_at": format_timestamp(draft.attempted_at),
                    "source_ip_hash": draft.source_ip_hash,
                },
                plant_id=enrollment.plant_id,
                occurred_at=draft.attempted_at,
            )
            attempt = EnrollmentAttempt(
                attempt_id=draft.attempt_id,
                organization_id=draft.organization_id,
                plant_id=draft.plant_id,
                node_id=draft.node_id,
                presented_code_hash=draft.presented_code_hash,
                hardware_fingerprint=draft.hardware_fingerprint,
                software_version=draft.software_version,
                contract_version=draft.contract_version,
                result=draft.result,
                attempted_at=draft.attempted_at,
                source_ip_hash=draft.source_ip_hash,
                correlation_id=draft.correlation_id,
                ledger_record_id=receipt.record_id,
            )
        await deps.enrollment.insert_attempt(transaction, attempt)
        return attempt

    async def attempts(
        self,
        context: ScopeContext,
        node_id: uuid.UUID,
        *,
        after: AttemptCursor | None = None,
        limit: int = MAX_ATTEMPTS_PAGE,
    ) -> AttemptPage:
        """Los intentos del nodo (``fleet.read`` sobre su planta), el más reciente primero."""
        if type(limit) is not int or not 1 <= limit <= MAX_ATTEMPTS_PAGE:
            raise ValueError(f"limit debe estar entre 1 y {MAX_ATTEMPTS_PAGE}")
        deps = self._deps
        authorized, node = await authorized_node(deps, context, node_id, PermissionKey.FLEET_READ)
        async with deps.database.transaction(authorized) as transaction:
            rows: Sequence[EnrollmentAttempt] = await deps.enrollment.attempts(
                transaction, plant_id=node.plant_id, node_id=node_id, after=after, limit=limit + 1
            )
            items = tuple(rows[:limit])
            if authorized.concession_id is not None:
                # BR-NUC-38: la lectura del proveedor, auditada en la misma transacción.
                await deps.audit.append(
                    authorized,
                    AuditOperation.FLEET_READ,
                    plant_id=node.plant_id,
                    resource=ResourceRef("node", node_id),
                    result_count=len(items),
                    transaction=transaction,
                )
        last = items[-1] if len(rows) > limit and items else None
        cursor = None if last is None else AttemptCursor(last.attempted_at, last.attempt_id)
        return AttemptPage(items, cursor)
