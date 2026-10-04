"""``catalog.gates``: transición, revocación, lecturas y sobre ``GateState`` (LC-GOB-03).

**Transición** (``transition_gate``, la reutiliza la aprobación del acuerdo de TASK-212 dentro de
su transacción), en la transacción del llamador, que ya autorizó ``commissioning.run`` sobre la
zona, en dos tiempos para que la firma no retenga la cadena del expediente:

1. ``prepare_transition``: exclusión de la proyección de la zona (``pg_advisory_xact_lock``),
   proyección vigente, plan puro (``plan_transition``: ``revoked`` solo desde ``approved``),
   instante del relevo (``handover_instant``: nunca antes del intervalo abierto ni de la última
   emisión) y ``SigningPort.sign(purpose=gate)`` del ``GateState`` con tope de espera;
2. ``commit_transition``: ``gate_state_changed`` en la cadena de la planta con el evento homónimo
   ``{zone_id, gate, status, resulting_mode, record_id, reason_es_present}`` (nunca el motivo),
   cierre del intervalo abierto y apertura del siguiente (misma transacción) y la proyección con
   su sobre conservado.

**Revocación** (``revoke``, ``POST /zones/{zone_id}/gates/{gate}/revocation``): motivo de 10 a 500
por la política de texto libre; solo una compuerta ``approved`` (si no, ``GateConflict``, que es
``conflict`` sin ``detail_code``); cierra el intervalo aprobado y abre ``revoked``. Revocar el uso
deja además el acuerdo vigente de la zona en ``revoked`` en la misma transacción (TASK-212,
BL §3.2). No toca ningún hallazgo ni registro anterior (BR-GOB-34).

**Candados** (orden único, también para ``catalog.agreements``): primero la exclusión de la
proyección de la zona (``lock_projection``), después la de la cadena de la planta que toma
``EscritorExpediente`` al escribir. La aprobación del acuerdo (``locked_state``) y la revocación
toman la misma exclusión de la zona antes de leer nada.

**Fallo cerrado** (FS-GOB-02, PAT-GOB-RES-03): si la firma no responde (clave no disponible,
servicio sin arrancar o tope agotado) la transacción entera se revierte: sin historia, registro ni
evento, y la persona recibe ``temporarily_unavailable`` (``GateUnavailable``).

**Lecturas**: ``gate_state`` (``catalog.read``; la zona sin transiciones es ``pending`` en las dos),
``state_at`` por contención y ``gate_history`` por solapamiento, **siempre** desde la historia
(BR-GOB-20); ``stored_gate_envelopes`` devuelve los sobres guardados sin firmar (PAT-GOB-REN-02,
NFR-GOB-10). ``renew_gate_envelope`` vuelve a emitir el sobre con el mismo estado y un
``issued_at`` nuevo, sin historia, registro ni evento (A-55: lo invoca el latido de TASK-223 cuando
al sobre le quedan menos de 24 h). Ningún paso lee la hora del sistema: ``Clock`` inyectado.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

from sqlalchemy import exc as sa_exc
from vigia_contracts.models.enumerations import AcceptanceStatus, GateStatus

from vigia_platform.catalog.adapters.postgres.agreement_repository import (
    PostgresAgreementRepository,
)
from vigia_platform.catalog.adapters.postgres.catalog_repository import PostgresCatalogRepository
from vigia_platform.catalog.adapters.postgres.gate_repository import (
    GateWriteConflict,
    PostgresGateRepository,
)
from vigia_platform.catalog.application.admission import CatalogRejected
from vigia_platform.catalog.detail_codes import CatalogDetailCode
from vigia_platform.catalog.domain.catalog_version import ZoneRef
from vigia_platform.catalog.domain.enums import GateKind
from vigia_platform.catalog.domain.gates import (
    MAX_REASON_CHARS,
    MIN_REASON_CHARS,
    GateInterval,
    GateRuleViolated,
    GateViolation,
    ZoneGateState,
    gate_state_payload,
    plan_transition,
)
from vigia_platform.catalog.domain.texts import has_content
from vigia_platform.catalog.domain.time_windows import (
    HalfOpenInterval,
    containing,
    handover_instant,
    utc_instant,
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
    Receipt,
    RecordScope,
)
from vigia_platform.ledger.free_text import FreeTextField, FreeTextPolicyRegistry, FreeTextRejected
from vigia_platform.shared.api.errors import ApiErrorCode, ExternalDependencyDown
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.outbox.publish import NewEvent
from vigia_platform.shared.signing import SigningKeyUnavailable, SigningNotReady, SigningPurpose

__all__ = [
    "GATE_STATE_CHANGED",
    "MAX_HISTORY_RANGE",
    "SIGN_TIMEOUT_SECONDS",
    "GateConflict",
    "GateRequestInvalid",
    "GateService",
    "GateSigner",
    "GateTransition",
    "GateUnavailable",
    "GateWriteFailed",
    "LedgerRaceLost",
    "PreparedTransition",
    "record_id_of",
]

GATE_STATE_CHANGED: Final = "gate_state_changed"
"""Tipo de registro y nombre del evento de cada transición (BR-GOB-20)."""
SIGN_TIMEOUT_SECONDS: Final = 5.0
"""Tope de ``SigningPort.sign`` (PAT-GOB-RES-03: 5 s, ``kms:Sign``)."""
MAX_HISTORY_RANGE: Final = timedelta(days=366)
"""``gate_history``: intervalo de consulta de hasta 366 días (interfaces §1.2, NFR-GOB-04)."""

_REASON: Final = FreeTextField(GATE_STATE_CHANGED, "/reason_es", MIN_REASON_CHARS, MAX_REASON_CHARS)


# --- Puertos y errores -------------------------------------------------------------------------


class GateSigner(Protocol):
    """La parte de ``SigningPort`` (``SigningService``) que usan las compuertas."""

    def sign(self, purpose: SigningPurpose, payload: Any) -> Any: ...


class GateUnavailable(ExternalDependencyDown):
    """Transitorio: la firma no respondió u otra transición de la zona ganó la carrera.

    Hacia la persona es ``temporarily_unavailable``; no queda nada escrito.
    """


class GateConflict(Exception):
    """Revocar una compuerta que no está ``approved``: ``conflict`` sin ``detail_code``."""

    api_code: Final = ApiErrorCode.CONFLICT

    def __init__(self) -> None:
        super().__init__("la compuerta no está aprobada")


class GateRequestInvalid(Exception):
    """La petición incumple un límite (rango de historia, compuerta): ``invalid_request``."""

    api_code: Final = ApiErrorCode.INVALID_REQUEST

    def __init__(self, reason: str = "petición de compuertas fuera de los límites") -> None:
        super().__init__(reason)


class GateWriteFailed(Exception):
    """El expediente rechazó el registro por una causa que no es del cuerpo: se revierte."""

    def __init__(self, rejection: LedgerRejection) -> None:
        super().__init__(f"registro rechazado: {rejection.code.value}")
        self.rejection = rejection


class LedgerRaceLost(Exception):
    """El registro ya existía (duplicado o conflicto de idempotencia): el llamador lo traduce a
    transitorio fuera de la transacción, que así se revierte."""


# --- Resultados ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreparedTransition:
    """Lo que ``prepare_transition`` deja listo (candado tomado y sobre firmado)."""

    zone: ZoneRef
    gate: GateKind
    previous: ZoneGateState
    state: ZoneGateState
    envelope: Mapping[str, Any]
    closes_open_interval: bool
    reason_es: str | None

    @property
    def at(self) -> datetime:
        issued = self.state.issued_at
        assert issued is not None  # noqa: S101 - prepare_transition siempre lo fija
        return issued


@dataclass(frozen=True, slots=True)
class GateTransition:
    """Una transición confirmable: el estado nuevo, su intervalo y su registro."""

    state: ZoneGateState
    interval: GateInterval
    envelope: Mapping[str, Any]
    ledger_record_id: uuid.UUID


# --- Servicio ------------------------------------------------------------------------------


@repository
class GateService:
    """``catalog.gates``: transiciones, revocación, lecturas y sobre conservado."""

    def __init__(
        self,
        *,
        repository: PostgresGateRepository,
        catalog: PostgresCatalogRepository,
        agreements: PostgresAgreementRepository,
        database: LedgerDatabase,
        writer: EscritorExpediente,
        authorizer: Authorizer,
        audit: AuditWriter,
        free_text: FreeTextPolicyRegistry,
        signer: GateSigner,
        clock: Clock,
        sign_timeout_seconds: float = SIGN_TIMEOUT_SECONDS,
    ) -> None:
        if not sign_timeout_seconds > 0:
            raise ValueError("sign_timeout_seconds debe ser positivo")
        self._repository = repository
        self._catalog = catalog
        self._agreements = agreements
        self._database = database
        self._writer = writer
        self._authorizer = authorizer
        self._audit = audit
        self._free_text = free_text
        self._signer = signer
        self._clock = clock
        self._sign_timeout = sign_timeout_seconds

    def __repr__(self) -> str:
        return "GateService()"

    @property
    def clock(self) -> Clock:
        return self._clock

    # --- Alcance ---------------------------------------------------------------------------

    async def zone(
        self, context: ScopeContext, zone_id: uuid.UUID, key: PermissionKey
    ) -> tuple[ZoneRef, ScopeContext]:
        """La zona y el contexto autorizado sobre ella; inexistente o fuera de alcance, igual."""
        if not isinstance(context, ScopeContext) or type(zone_id) is not uuid.UUID:
            raise ResourceNotFound()
        zone = await self._catalog.zone(context, zone_id)
        if zone is None:
            raise ResourceNotFound()
        authorized = await self._authorizer.authorize(
            context, key, Resource.zone(context.organization_id, zone.plant_id, zone.zone_id)
        )
        return zone, authorized

    # --- Transición (en la transacción del llamador) -----------------------------------------

    async def prepare_transition(
        self,
        transaction: Transaction,
        context: ScopeContext,
        zone_id: uuid.UUID,
        gate: GateKind,
        status: GateStatus,
        record_id: uuid.UUID | None,
        reason_es: str | None = None,
    ) -> PreparedTransition:
        """Candado, plan y sobre firmado de la transición; aún no escribe nada.

        ``GateRuleViolated`` si el dominio la rechaza; ``GateUnavailable`` si la firma no
        responde; ``ResourceNotFound`` si la zona no está en la organización de la transacción.
        """
        if transaction.context.organization_id != context.organization_id:
            raise ValueError("la transacción es de otra organización que el contexto")
        if type(zone_id) is not uuid.UUID:
            raise ResourceNotFound()
        gate, status = GateKind(gate), GateStatus(status)
        await self._repository.lock_projection(transaction, zone_id)
        zone = await self._repository.zone(transaction, zone_id)
        if zone is None:
            raise ResourceNotFound()
        current = await self._repository.state(transaction, zone_id) or ZoneGateState.initial(
            zone.organization_id, zone.plant_id, zone.zone_id
        )
        previous = current.decision(gate)
        at = handover_instant(
            self._clock.now(),
            open_start=previous.decided_at,
            last_issued=current.issued_at,
        )
        plan = plan_transition(
            current,
            gate,
            status,
            at=at,
            decided_by=uuid.UUID(str(context.actor.id)),
            record_id=record_id,
            reason_es=reason_es,
        )
        state = dataclasses.replace(plan.state, issued_at=at, envelope=None)
        envelope = await self._sign(gate_state_payload(state, at))
        return PreparedTransition(
            zone=zone,
            gate=gate,
            previous=current,
            state=dataclasses.replace(state, envelope=envelope),
            envelope=envelope,
            closes_open_interval=plan.closes_open_interval,
            reason_es=reason_es if status is GateStatus.REVOKED else None,
        )

    async def commit_transition(
        self, transaction: Transaction, context: ScopeContext, prepared: PreparedTransition
    ) -> GateTransition:
        """``gate_state_changed`` con su evento, relevo de intervalos y proyección con su sobre."""
        if not isinstance(prepared, PreparedTransition):
            raise TypeError("prepared debe salir de prepare_transition")
        zone, gate, state, at = prepared.zone, prepared.gate, prepared.state, prepared.at
        decision = state.decision(gate)
        record_id = decision.record_id
        if record_id is None or decision.decided_by is None:  # el plan siempre los fija
            raise GateRuleViolated(GateViolation.REQUEST_INVALID)
        reason = prepared.reason_es
        if (decision.status is GateStatus.REVOKED) != (reason is not None):
            raise GateRuleViolated(GateViolation.REQUEST_INVALID)
        content: dict[str, Any] = {
            "zone_id": str(zone.zone_id),
            "gate": gate.value,
            "status": decision.status.value,
            "resulting_mode": state.resulting_mode.value,
            ("record_id" if gate is GateKind.MOUNTING else "agreement_id"): str(record_id),
        }
        if reason is not None:
            content["reason_es"] = reason
        written = await self._writer.write(
            context,
            GATE_STATE_CHANGED,
            content,
            scope=RecordScope(plant_id=zone.plant_id, zone_id=zone.zone_id),
            events=(
                NewEvent(
                    event_name=GATE_STATE_CHANGED,
                    payload={
                        "zone_id": str(zone.zone_id),
                        "gate": gate.value,
                        "status": decision.status.value,
                        "resulting_mode": state.resulting_mode.value,
                        "record_id": str(record_id),
                        "reason_es_present": reason is not None,
                    },
                ),
            ),
            occurred_at=at,
            transaction=transaction,
        )
        ledger_record_id = record_id_of(written)
        previous = prepared.previous.decision(gate)
        if prepared.closes_open_interval:
            if previous.decided_at is None:
                raise GateWriteConflict
            await self._repository.close_open_interval(
                transaction, zone.zone_id, gate, previous.decided_at, at
            )
        interval = GateInterval(
            organization_id=zone.organization_id,
            plant_id=zone.plant_id,
            zone_id=zone.zone_id,
            gate=gate,
            status=decision.status,
            effective_from=at,
            effective_until=None,
            decided_by=decision.decided_by,
            reason_es=reason,
            record_id=record_id,
            ledger_record_id=ledger_record_id,
        )
        await self._repository.open_interval(transaction, interval)
        await self._repository.save_state(transaction, state, prepared.envelope)
        return GateTransition(state, interval, prepared.envelope, ledger_record_id)

    async def transition_gate(
        self,
        transaction: Transaction,
        context: ScopeContext,
        zone_id: uuid.UUID,
        gate: GateKind,
        status: GateStatus,
        record_id: uuid.UUID | None,
        reason_es: str | None = None,
    ) -> GateTransition:
        """La transición entera dentro de ``transaction`` (el llamador ya autorizó).

        ``context`` es el del escritor (unidad U-03): su actor es quien decide. Lo que lance
        revierte la transacción del llamador.
        """
        prepared = await self.prepare_transition(
            transaction, context, zone_id, gate, status, record_id, reason_es
        )
        return await self.commit_transition(transaction, context, prepared)

    # --- Revocación --------------------------------------------------------------------------

    async def revoke(
        self, context: ScopeContext, zone_id: uuid.UUID, gate: GateKind, reason_es: str
    ) -> GateTransition:
        """Revoca ``gate`` con motivo (BR-GOB-33); devuelve la transición tras confirmar."""
        reason = self._reason(reason_es)
        zone, authorized = await self.zone(context, zone_id, PermissionKey.COMMISSIONING_RUN)
        if authorized.actor.role_in_use is None:  # ``authorize`` siempre lo fija.
            raise ResourceNotFound()
        writer_context = with_unit(authorized, ActorUnit.U03)

        kind = GateKind(gate)

        async def revoke(transaction: Transaction) -> GateTransition:
            transition = await self.transition_gate(
                transaction,
                writer_context,
                zone.zone_id,
                kind,
                GateStatus.REVOKED,
                None,
                reason,
            )
            if kind is GateKind.USAGE:
                # El acuerdo vigente se revoca con su compuerta (BL §3.2); no caduca nunca solo.
                at = transition.interval.effective_from
                await self._agreements.revoke_current(transaction, zone.zone_id, at)
            return transition

        return await self.run(writer_context, revoke)

    # --- Proyección dentro de una transacción (catalog.agreements) ----------------------------

    async def locked_state(self, transaction: Transaction, zone: ZoneRef) -> ZoneGateState:
        """La exclusión de la proyección de la zona y su estado (``pending`` si nunca cambió).

        La toma la aprobación del acuerdo **antes** de leer el acuerdo y sus guardas: dos
        aprobaciones simultáneas se ordenan y la segunda ve lo que dejó la primera.
        """
        await self._repository.lock_projection(transaction, zone.zone_id)
        return await self.state_in(transaction, zone)

    async def state_in(self, transaction: Transaction, zone: ZoneRef) -> ZoneGateState:
        """El estado de la zona dentro de ``transaction`` (``pending`` si nunca cambió)."""
        state = await self._repository.state(transaction, zone.zone_id)
        return state or ZoneGateState.initial(zone.organization_id, zone.plant_id, zone.zone_id)

    async def run[T](self, context: ScopeContext, body: Callable[[Transaction], Awaitable[T]]) -> T:
        """``body(transaction)`` en una transacción; traduce carreras y reglas a sus errores."""
        try:
            async with self._database.transaction(context) as transaction:
                return await body(transaction)
        except GateRuleViolated as violation:
            if violation.violation is GateViolation.NOT_APPROVED:
                raise GateConflict from None
            raise GateRequestInvalid() from None
        except (LedgerRaceLost, GateWriteConflict):
            raise GateUnavailable("gate_race") from None
        except sa_exc.IntegrityError:
            # Exclusión de intervalos o clave de la historia: otra transición confirmó antes
            # (respaldo del candado). Nada queda escrito.
            raise GateUnavailable("gate_race") from None

    def _reason(self, value: object) -> str:
        if not isinstance(value, str):
            raise GateRequestInvalid("reason_es debe ser texto")
        try:
            reason = self._free_text.apply(value, _REASON)
        except FreeTextRejected:
            raise CatalogRejected(CatalogDetailCode.FREE_TEXT_REJECTED) from None
        if not has_content(reason):  # solo espacios, signos o invisibles: no es un motivo
            raise CatalogRejected(CatalogDetailCode.FREE_TEXT_REJECTED)
        return reason

    # --- Firma -------------------------------------------------------------------------------

    async def _sign(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """El ``SignedEnvelope<GateState>``, o ``GateUnavailable`` (fallo cerrado)."""
        try:
            async with asyncio.timeout(self._sign_timeout):
                signed = await asyncio.to_thread(self._signer.sign, SigningPurpose.GATE, payload)
        except (SigningKeyUnavailable, SigningNotReady, TimeoutError):
            raise GateUnavailable("gate_signing") from None
        envelope: dict[str, Any] = signed.to_json_value()
        return envelope

    async def renew_gate_envelope(
        self, context: ScopeContext, zone_id: uuid.UUID
    ) -> Mapping[str, Any] | None:
        """Vuelve a emitir el sobre con el mismo estado y un ``issued_at`` nuevo (A-55).

        Sin historia, sin ``gate_state_changed`` y sin evento; ``None`` si la zona nunca cambió
        (no hay sobre que renovar). Con la firma caída, ``GateUnavailable`` y el sobre anterior
        intacto. El llamador (el latido, TASK-223) decide cuándo: aquí no hay umbral.
        """
        if not isinstance(context, ScopeContext) or type(zone_id) is not uuid.UUID:
            raise ResourceNotFound()

        async def renew(transaction: Transaction) -> Mapping[str, Any] | None:
            await self._repository.lock_projection(transaction, zone_id)
            current = await self._repository.state(transaction, zone_id)
            if current is None:
                return None
            issued = handover_instant(self._clock.now(), last_issued=current.issued_at)
            state = dataclasses.replace(current, issued_at=issued, envelope=None)
            envelope = await self._sign(gate_state_payload(state, issued))
            await self._repository.save_state(transaction, state, envelope)
            return envelope

        renewed: Mapping[str, Any] | None = await self.run(context, renew)
        return renewed

    # --- Lecturas ----------------------------------------------------------------------------

    async def gate_state(self, context: ScopeContext, zone_id: uuid.UUID) -> ZoneGateState:
        """``GET /zones/{zone_id}/gates`` (``catalog.read``): la proyección, o ``pending`` en las
        dos si la zona nunca cambió. Bajo concesión, auditada en la misma transacción."""
        zone, authorized = await self.zone(context, zone_id, PermissionKey.CATALOG_READ)
        async with self._database.transaction(authorized) as transaction:
            state = await self._repository.state(transaction, zone.zone_id)
            if authorized.concession_id is not None:
                # BR-NUC-38 y A-56: la lectura del proveedor, auditada (fallo cerrado).
                await self._audit.append(
                    authorized,
                    AuditOperation.CATALOG_READ,
                    plant_id=zone.plant_id,
                    zone_id=zone.zone_id,
                    result_count=1,
                    transaction=transaction,
                )
        return state or ZoneGateState.initial(zone.organization_id, zone.plant_id, zone.zone_id)

    async def state_at(
        self, context: ScopeContext, zone_id: uuid.UUID, gate: GateKind, at: datetime
    ) -> GateInterval | None:
        """El intervalo de ``gate`` que contiene ``at``, desde la historia (BR-GOB-20).

        ``None`` es ``pending``: ningún intervalo contiene el instante. Dos a la vez sería una
        historia rota: ``ValueError`` en lugar de elegir uno.
        """
        moment = _instant(at)
        zone, authorized = await self.zone(context, zone_id, PermissionKey.CATALOG_READ)
        found = await self._repository.state_at(authorized, zone.zone_id, GateKind(gate), moment)
        return containing(found, moment, lambda interval: interval.window)

    async def gate_history(
        self, context: ScopeContext, zone_id: uuid.UUID, from_: datetime, to_: datetime
    ) -> tuple[GateInterval, ...]:
        """Los intervalos de las dos compuertas que se solapan con ``[from_, to_)``."""
        start, end = _instant(from_), _instant(to_)
        if not end > start or end - start > MAX_HISTORY_RANGE:
            raise GateRequestInvalid("el rango de la historia va de 1 ms a 366 días")
        query = HalfOpenInterval(start, end)
        zone, authorized = await self.zone(context, zone_id, PermissionKey.CATALOG_READ)
        found = await self._repository.history(authorized, zone.zone_id, start, end)
        return tuple(interval for interval in found if interval.window.overlaps(query))

    async def stored_gate_envelopes(
        self, context: ScopeContext, zone_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, Mapping[str, Any]]:
        """Los sobres ``SignedEnvelope<GateState>`` guardados, **sin firmar** (NFR-GOB-10).

        Puerto interno del latido (TASK-223): las zonas fuera de la organización o del alcance
        del contexto, o sin ninguna transición, no aparecen.
        """
        if not isinstance(context, ScopeContext):
            raise ResourceNotFound()
        zones = [zone_id for zone_id in zone_ids if type(zone_id) is uuid.UUID]
        return await self._repository.envelopes(context, zones)


def _instant(value: object) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise GateRequestInvalid("un instante con zona horaria")
    return utc_instant(value)


def record_id_of(written: Receipt | LedgerRejection) -> uuid.UUID:
    """El identificador del registro escrito; un rechazo o un duplicado revierten."""
    if isinstance(written, LedgerRejection):
        if written.code is LedgerRejectionCode.FREE_TEXT_REJECTED:
            raise CatalogRejected(CatalogDetailCode.FREE_TEXT_REJECTED)
        if written.code is LedgerRejectionCode.IDEMPOTENCY_CONFLICT:
            raise LedgerRaceLost
        raise GateWriteFailed(written)
    if written.status is not AcceptanceStatus.ACCEPTED:
        raise LedgerRaceLost
    return uuid.UUID(str(written.record_id))
