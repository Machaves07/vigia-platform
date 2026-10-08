"""``catalog.agreements``: acuerdo de uso, confirmación de cada firmante y aprobación (LC-GOB-04).

**Alta** (``create``, ``POST /zones/{zone_id}/use-agreements``, ``commissioning.run`` sobre la
zona): antes de escribir nada y en este orden, la zona dentro del alcance (si no,
``ResourceNotFound``), los firmantes contra la política de la planta y
``IdentityQueryPort.signatory_candidates`` (``check_signatories``: rol en la política, usuario con
ese rol sobre la zona, ``copasst`` y ``minimum``; la consulta acotada de A-58 los resuelve también
bajo concesión de planta, y el nombre se guarda recortado a 120, DE §2.8),
``replaces_agreement_id`` (de otra zona, ``agreement_reused_from_other_zone``; de la zona pero no
el vigente, o ninguno citado habiendo uno vigente, ``AgreementConflict``) y
``verify_document_refs`` del documento opcional (``kind = use_agreement``, BR-GOB-28; la clave se
exige antes, porque la verificación no autoriza). Nace en ``pending_signatures``.

**Confirmación** (``confirm``, ``POST /use-agreements/{agreement_id}/confirmations``, sin cuerpo):
la ruta se declara con ``transparency.read``, que tienen todos los roles firmantes; el servicio
autoriza además la clave del origen. El usuario de la sesión confirma por sí mismo (BR-GOB-26):

1. acuerdo visible y ``transparency.read`` sobre su zona (si no, ``ResourceNotFound``);
2. bajo concesión de proveedor, ``authorization_denied`` y ``ResourceNotFound`` aunque la columna
   ``provider_installer`` tenga ``agreements.sign`` (BR-GOB-27, BR-NUC-37, G-9);
3. el usuario y el rol con el que se le espera deben estar entre los firmantes, con ese rol vigente
   sobre la zona (si no, ``signatory_not_expected``, G-14);
4. ``copasst`` confirma con ``transparency.read`` y origen ``transparency`` (H-55); el resto, con
   ``agreements.sign`` y origen ``management``.

Una confirmación por ``(agreement_id, user_id)``: la repetida devuelve la que ya existe y no
escribe nada; sobre un acuerdo que ya no está pendiente, ``AgreementConflict``.

**Aprobación** (``approve``, ``POST /use-agreements/{agreement_id}/approval``, ``commissioning.run``
sobre la zona), en una transacción y **bajo la exclusión de la proyección de la zona** (la misma de
``catalog.gates``; es el único candado que toma, antes de leer nada):

1. un acuerdo ya ``approved`` devuelve el estado actual sin ningún efecto; ``superseded`` o
   ``revoked``, ``AgreementConflict``;
2. las cuatro guardas de BR-GOB-29 en orden (``first_missing``): montaje ``approved``, acta de
   comisionamiento cerrada de la zona, confirmaciones completas y política de planta vigente; la
   primera que falta es el error y nada queda escrito;
3. el acuerdo citado en ``replaces_agreement_id`` tiene que seguir siendo el vigente (si no,
   ``AgreementConflict``);
4. ``prepare_transition`` (sobre ``GateState`` firmado), ``use_agreement_signed`` (``source_key =
   agreement_id``) con ``zone_activated`` solo si la zona pasa a ``productive``, el acuerdo
   ``approved`` en el instante de la transición (``effective_from``, BR-GOB-30), el anterior
   ``superseded`` en ese mismo instante y ``commit_transition`` (``gate_state_changed`` con su
   evento y el relevo contiguo de intervalos: ningún instante sin uso aprobado, BR-GOB-32).

Ningún paso lee la hora del sistema: ``Clock`` inyectado.
"""

from __future__ import annotations

import dataclasses
import os
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from vigia_contracts.models.enumerations import GateStatus, ZoneMode

from vigia_platform.catalog.adapters.postgres.agreement_repository import (
    AgreementWriteConflict,
    PostgresAgreementRepository,
)
from vigia_platform.catalog.adapters.postgres.plant_policy_repository import (
    PostgresPlantPolicyRepository,
)
from vigia_platform.catalog.application.admission import CatalogRejected
from vigia_platform.catalog.application.documents import DocumentService, VerifiedDocuments
from vigia_platform.catalog.application.gates import GateService, GateTransition, record_id_of
from vigia_platform.catalog.application.signatory_policy import rejected
from vigia_platform.catalog.detail_codes import CatalogDetailCode
from vigia_platform.catalog.domain.agreements import (
    MAX_SIGNATORIES,
    AgreementConfirmation,
    AgreementRuleViolated,
    AgreementViolation,
    ApprovalFacts,
    Signatory,
    UseAgreement,
    check_signatories,
    confirmation_origin,
    first_missing,
    signatures_complete,
    use_agreement_signed_content,
)
from vigia_platform.catalog.domain.catalog_version import ZoneRef
from vigia_platform.catalog.domain.documents import DocumentRef
from vigia_platform.catalog.domain.enums import (
    AgreementStatus,
    ConfirmationOrigin,
    DocumentKind,
    GateKind,
)
from vigia_platform.catalog.domain.gates import ZoneGateState
from vigia_platform.catalog.domain.time_windows import utc_instant
from vigia_platform.identity.application.hierarchy import SignatoryCandidate
from vigia_platform.identity.authz.authorize import Authorizer, Resource, ResourceNotFound
from vigia_platform.identity.authz.context import with_scopes, with_unit
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.writer import EscritorExpediente, LedgerDatabase, RecordScope
from vigia_platform.shared.api.errors import ApiErrorCode, ExternalDependencyDown
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, Role, ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.outbox.publish import NewEvent
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "USE_AGREEMENT_SIGNED",
    "ZONE_ACTIVATED",
    "AgreementApproval",
    "AgreementConflict",
    "AgreementRequest",
    "AgreementRequestInvalid",
    "AgreementService",
    "AgreementUnavailable",
    "ConfirmationResult",
    "SignatoryLookup",
]

USE_AGREEMENT_SIGNED: Final = "use_agreement_signed"
ZONE_ACTIVATED: Final = "zone_activated"


class SignatoryLookup(Protocol):
    """``IdentityQueryPort.signatory_candidates`` de U-02 (LC-NUC-05, A-58)."""

    async def signatory_candidates(
        self,
        context: ScopeContext,
        zone_id: uuid.UUID,
        roles: Iterable[Role],
        user_ids: Iterable[uuid.UUID],
    ) -> tuple[SignatoryCandidate, ...]: ...


class AgreementConflict(Exception):
    """El acuerdo no está en el estado que la operación exige: ``conflict`` sin ``detail_code``."""

    api_code: Final = ApiErrorCode.CONFLICT

    def __init__(self) -> None:
        super().__init__("el acuerdo no está en el estado que la operación exige")


class AgreementRequestInvalid(Exception):
    """El cuerpo es incoherente por sí mismo (firmante repetido, acuerdo citado inexistente)."""

    api_code: Final = ApiErrorCode.INVALID_REQUEST

    def __init__(self, reason: str = "acuerdo de uso fuera de los límites") -> None:
        super().__init__(reason)


class AgreementUnavailable(ExternalDependencyDown):
    """Transitorio: otra operación del acuerdo confirmó antes (respaldo del candado)."""


@dataclass(frozen=True, slots=True)
class AgreementRequest:
    """``{signatories[{role, user_id}], document_ref?, replaces_agreement_id?}``."""

    signatories: Sequence[tuple[object, object]]
    document_ref: Mapping[str, Any] | None = None
    replaces_agreement_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class ConfirmationResult:
    """La confirmación del firmante y si la escribió esta petición."""

    confirmation: AgreementConfirmation
    created: bool


@dataclass(frozen=True, slots=True)
class AgreementApproval:
    """El acuerdo aprobado, el estado de las compuertas y la transición (``None`` si ya estaba
    aprobado: la aprobación repetida no tiene efectos)."""

    agreement: UseAgreement
    confirmations: tuple[AgreementConfirmation, ...]
    state: ZoneGateState
    transition: GateTransition | None


def _violation(error: AgreementRuleViolated) -> CatalogRejected:
    return rejected(error)


@repository
class AgreementService:
    """``catalog.agreements``: alta, confirmación y aprobación del acuerdo de uso."""

    def __init__(
        self,
        *,
        repository: PostgresAgreementRepository,
        gates: GateService,
        policies: PostgresPlantPolicyRepository,
        documents: DocumentService,
        identity: SignatoryLookup,
        database: LedgerDatabase,
        writer: EscritorExpediente,
        authorizer: Authorizer,
        clock: Clock,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._repository = repository
        self._gates = gates
        self._policies = policies
        self._documents = documents
        self._identity = identity
        self._database = database
        self._writer = writer
        self._authorizer = authorizer
        self._clock = clock
        self._random_bytes = random_bytes

    def __repr__(self) -> str:
        return "AgreementService()"

    async def _visible(self, context: ScopeContext, agreement_id: uuid.UUID) -> UseAgreement:
        """El acuerdo en la organización del contexto; inexistente o invisible, igual."""
        if not isinstance(context, ScopeContext) or type(agreement_id) is not uuid.UUID:
            raise ResourceNotFound()
        async with self._database.transaction(context) as transaction:
            agreement = await self._repository.agreement(transaction, agreement_id)
        if agreement is None:
            raise ResourceNotFound()
        return agreement

    # --- Alta ------------------------------------------------------------------------------------

    async def create(
        self, context: ScopeContext, zone_id: uuid.UUID, request: AgreementRequest
    ) -> UseAgreement:
        """Registra el acuerdo en ``pending_signatures``; lo devuelve tras confirmar.

        ``ResourceNotFound``, ``CatalogRejected``, ``AgreementRequestInvalid``,
        ``AgreementConflict``, ``DocumentRequestInvalid`` o ``StorageUnavailable``; en todos,
        nada queda escrito.
        """
        if not isinstance(request, AgreementRequest):
            raise TypeError("request debe ser AgreementRequest")
        zone, authorized = await self._gates.zone(context, zone_id, PermissionKey.COMMISSIONING_RUN)
        signatories = _signatories(request.signatories)
        async with self._database.transaction(authorized) as transaction:
            policy = await self._repository.policy(transaction, zone.plant_id)
            current = await self._repository.current(transaction, zone.zone_id)
            replaced = (
                None
                if request.replaces_agreement_id is None
                else await self._repository.agreement(transaction, request.replaces_agreement_id)
            )
        names: dict[tuple[uuid.UUID, Role], str] = {}
        if policy is not None and len(signatories) <= MAX_SIGNATORIES:
            # Solo los usuarios pedidos (A-58); por encima del tope, check_signatories lo rechaza.
            for holder in await self._identity.signatory_candidates(
                authorized,
                zone.zone_id,
                {signatory.role for signatory in signatories},
                {signatory.user_id for signatory in signatories},
            ):
                names[(holder.user_id, Role(holder.role))] = holder.display_name
        try:
            checked = check_signatories(signatories, policy, names.keys())
        except AgreementRuleViolated as violation:
            raise _violation(violation) from None
        except ValueError:
            raise AgreementRequestInvalid() from None
        if request.replaces_agreement_id is not None:
            if replaced is None:
                raise AgreementRequestInvalid("el acuerdo citado no existe")
            if replaced.zone_id != zone.zone_id:
                raise CatalogRejected(CatalogDetailCode.AGREEMENT_REUSED_FROM_OTHER_ZONE)
        if (None if current is None else current.agreement_id) != request.replaces_agreement_id:
            # Uno nuevo sustituye siempre al vigente de la zona, y solo a él (BR-GOB-32).
            raise AgreementConflict
        document = None if request.document_ref is None else DocumentRef.parse(request.document_ref)
        verified: VerifiedDocuments | None = None
        if document is not None:
            verified = await self._documents.verify_document_refs(
                authorized, [document], {DocumentKind.USE_AGREEMENT}, zone.plant_id
            )
        agreement = UseAgreement(
            agreement_id=uuid7(self._clock, self._random_bytes),
            organization_id=zone.organization_id,
            plant_id=zone.plant_id,
            zone_id=zone.zone_id,
            status=AgreementStatus.PENDING_SIGNATURES,
            signatories=tuple(
                dataclasses.replace(s, display_name=names.get((s.user_id, s.role))) for s in checked
            ),
            document_ref=document,
            replaces_agreement_id=request.replaces_agreement_id,
            created_by=uuid.UUID(str(authorized.actor.id)),
            created_at=utc_instant(self._clock.now()),
        )
        writer_context = with_unit(authorized, ActorUnit.U03)
        async with self._database.transaction(writer_context) as transaction:
            await self._repository.insert(transaction, agreement)
            if verified is not None:
                await self._documents.mark_used(transaction, verified)
        return agreement

    # --- Confirmación ----------------------------------------------------------------------------

    async def confirm(self, context: ScopeContext, agreement_id: uuid.UUID) -> ConfirmationResult:
        """Confirma la firma del usuario de la sesión, en su propia sesión (BR-GOB-26, 27)."""
        agreement = await self._visible(context, agreement_id)
        resource = Resource.zone(agreement.organization_id, agreement.plant_id, agreement.zone_id)
        # El alcance de la ruta: todos los roles firmantes tienen transparency.read.
        await self._authorizer.authorize(context, PermissionKey.TRANSPARENCY_READ, resource)
        if context.concession_id is not None:
            # BR-GOB-27 y G-9: la concesión nunca da agreements.sign, aunque esté en la columna.
            await self._authorizer.deny(context, PermissionKey.AGREEMENTS_SIGN, resource)
        user_id = uuid.UUID(str(context.actor.id))
        expected = agreement.expected(user_id)
        if expected is None:
            raise CatalogRejected(CatalogDetailCode.SIGNATORY_NOT_EXPECTED)
        held = [
            scope
            for scope in context.allowed_scopes
            if scope.role is expected.role
            and scope.covers(agreement.organization_id, agreement.plant_id, agreement.zone_id)
        ]
        if not held:  # el rol con el que se le espera ya no es suyo sobre la zona
            raise CatalogRejected(CatalogDetailCode.SIGNATORY_NOT_EXPECTED)
        origin = confirmation_origin(expected.role)
        key = (
            PermissionKey.TRANSPARENCY_READ
            if origin is ConfirmationOrigin.TRANSPARENCY
            else PermissionKey.AGREEMENTS_SIGN
        )
        signer = await self._authorizer.authorize(
            with_scopes(context, held, expected.role), key, resource
        )
        async with self._database.transaction(signer) as transaction:
            existing = await self._confirmation_of(transaction, agreement_id, user_id)
            if existing is not None:
                return ConfirmationResult(existing, created=False)
            current = await self._repository.agreement(transaction, agreement_id)
            if current is None:
                raise ResourceNotFound()
            if current.status is not AgreementStatus.PENDING_SIGNATURES:
                raise AgreementConflict
            confirmation = AgreementConfirmation(
                agreement_id=agreement_id,
                user_id=user_id,
                organization_id=agreement.organization_id,
                plant_id=agreement.plant_id,
                role_in_use=expected.role,
                confirmed_at=utc_instant(self._clock.now()),
                origin=origin,
            )
            if await self._repository.confirm(transaction, confirmation):
                return ConfirmationResult(confirmation, created=True)
            # Otra petición del mismo firmante confirmó antes: la clave deja una sola fila.
            existing = await self._confirmation_of(transaction, agreement_id, user_id)
        if existing is None:
            raise AgreementUnavailable("agreement_confirmation_race")
        return ConfirmationResult(existing, created=False)

    async def _confirmation_of(
        self, transaction: Transaction, agreement_id: uuid.UUID, user_id: uuid.UUID
    ) -> AgreementConfirmation | None:
        for confirmation in await self._repository.confirmations(transaction, agreement_id):
            if confirmation.user_id == user_id:
                return confirmation
        return None

    # --- Aprobación ------------------------------------------------------------------------------

    async def approve(self, context: ScopeContext, agreement_id: uuid.UUID) -> AgreementApproval:
        """Aprueba el acuerdo y abre la compuerta de uso; repetida, devuelve el estado actual.

        ``ResourceNotFound``, ``CatalogRejected`` (las cuatro guardas de BR-GOB-29),
        ``AgreementConflict``, ``GateUnavailable`` o ``AgreementUnavailable``; en todos, nada
        queda escrito.
        """
        agreement = await self._visible(context, agreement_id)
        zone, authorized = await self._gates.zone(
            context, agreement.zone_id, PermissionKey.COMMISSIONING_RUN
        )
        writer_context = with_unit(authorized, ActorUnit.U03)
        approved_by = uuid.UUID(str(authorized.actor.id))

        async def approve(transaction: Transaction) -> AgreementApproval:
            state = await self._gates.locked_state(transaction, zone)
            current = await self._repository.agreement(transaction, agreement_id)
            if current is None or current.zone_id != zone.zone_id:
                raise ResourceNotFound()
            confirmations = await self._repository.confirmations(transaction, agreement_id)
            if current.status is AgreementStatus.APPROVED:
                return AgreementApproval(current, confirmations, state, None)
            if current.status is not AgreementStatus.PENDING_SIGNATURES:
                raise AgreementConflict
            record_id = await self._repository.closed_commissioning_record(
                transaction, zone.zone_id
            )
            missing = first_missing(
                ApprovalFacts(
                    mounting_approved=state.mounting.status is GateStatus.APPROVED,
                    commissioning_record_closed=record_id is not None,
                    signatures_complete=signatures_complete(current.signatories, confirmations),
                    plant_policy_loaded=await self._policies.loaded(transaction, zone.plant_id),
                )
            )
            if missing is not None or record_id is None:
                raise _violation(
                    AgreementRuleViolated(
                        missing or AgreementViolation.COMMISSIONING_RECORD_MISSING
                    )
                )
            in_force = await self._repository.current(transaction, zone.zone_id)
            in_force_id = None if in_force is None else in_force.agreement_id
            if in_force_id != current.replaces_agreement_id:
                raise AgreementConflict
            return await self._approve(
                transaction, writer_context, zone, current, confirmations, record_id, approved_by
            )

        try:
            approval: AgreementApproval = await self._gates.run(writer_context, approve)
        except AgreementWriteConflict:
            raise AgreementUnavailable("agreement_race") from None
        return approval

    async def _approve(
        self,
        transaction: Transaction,
        writer_context: ScopeContext,
        zone: ZoneRef,
        agreement: UseAgreement,
        confirmations: tuple[AgreementConfirmation, ...],
        commissioning_record_id: uuid.UUID,
        approved_by: uuid.UUID,
    ) -> AgreementApproval:
        prepared = await self._gates.prepare_transition(
            transaction,
            writer_context,
            zone.zone_id,
            GateKind.USAGE,
            GateStatus.APPROVED,
            agreement.agreement_id,
        )
        at = prepared.at
        activates = (
            prepared.previous.resulting_mode is not ZoneMode.PRODUCTIVE
            and prepared.state.resulting_mode is ZoneMode.PRODUCTIVE
        )
        events = (
            (
                NewEvent(
                    event_name=ZONE_ACTIVATED,
                    payload={
                        "zone_id": str(zone.zone_id),
                        "activated_at": format_timestamp(at),
                        "agreement_id": str(agreement.agreement_id),
                        "commissioning_record_id": str(commissioning_record_id),
                    },
                ),
            )
            if activates
            else ()
        )
        written = await self._writer.write(
            writer_context,
            USE_AGREEMENT_SIGNED,
            use_agreement_signed_content(agreement, confirmations),
            scope=RecordScope(plant_id=zone.plant_id, zone_id=zone.zone_id),
            events=events,
            occurred_at=at,
            transaction=transaction,
        )
        ledger_record_id = record_id_of(written)
        await self._repository.approve(
            transaction,
            agreement,
            approved_at=at,
            approved_by=approved_by,
            ledger_record_id=ledger_record_id,
        )
        if agreement.replaces_agreement_id is not None:
            # Mismo instante que el relevo de intervalos: ninguno sin uso aprobado (BR-GOB-32).
            await self._repository.supersede(
                transaction, zone.zone_id, agreement.replaces_agreement_id, at
            )
        transition = await self._gates.commit_transition(transaction, writer_context, prepared)
        approved = dataclasses.replace(
            agreement,
            status=AgreementStatus.APPROVED,
            approved_at=at,
            approved_by=approved_by,
            ledger_record_id=ledger_record_id,
        )
        return AgreementApproval(approved, confirmations, transition.state, transition)


def _signatories(values: object) -> tuple[Signatory, ...]:
    """Los firmantes del cuerpo; una forma incoherente es ``AgreementRequestInvalid``."""
    if not isinstance(values, list | tuple):
        raise AgreementRequestInvalid()
    signatories: list[Signatory] = []
    for value in values:
        if not isinstance(value, tuple) or len(value) != 2:
            raise AgreementRequestInvalid()
        role, user_id = value
        if type(user_id) is not uuid.UUID:
            raise AgreementRequestInvalid()
        try:
            signatories.append(Signatory(Role(str(role)), user_id))
        except ValueError:
            raise AgreementRequestInvalid() from None
    return tuple(signatories)
