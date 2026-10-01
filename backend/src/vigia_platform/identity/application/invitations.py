"""Invitaciones de un solo uso y activación de la cuenta (LC-NUC-05; BR-NUC-30, 32; NFR-NUC-29).

**Emisión** (``issue_invitation``, dentro de la transacción de quien invita o reactiva): token
aleatorio de 256 bits (≥ 128, BR-NUC-30) en base64url; en la base solo su SHA-256
(``token_hash``); vence a las 72 horas; una invitación nueva cancela la pendiente anterior del
mismo usuario. Audita ``user_invited`` y publica ``user_invited`` con identificadores.

**Entrega del enlace** (``deliver``, después de confirmar; nota fechada de 2026-09-23 de
``business-logic-model.md`` §10.1): el correo lo envía **solo U-02**, en proceso, por
``EmailSenderPort`` (lo aporta U-04 al arrancar en ``EmailSenderRegistry``). El evento
``user_invited`` se publica igual, pero ningún consumidor envía correo con él. Si el puerto no está
registrado, responde ``unavailable`` o falla, o si el administrador pide ver el enlace, el enlace
se devuelve **una sola vez** al administrador que invita y se audita
``invitation_link_disclosed`` (``disclosed_to_inviter_at``). El token en claro no se guarda en
ningún sitio, así que no se puede volver a mostrar: hace falta otra invitación.

**Activación** (``InvitationService``, ruta pública ``POST /invitations/{token}/accept``,
TASK-135): ``begin_activation(token)`` valida el enlace y, si el rol exige segundo factor
(``administrator``, ``platform_operator``), inscribe la credencial y devuelve lo que se muestra
una vez (QR y códigos de recuperación). ``accept_invitation(token, password, notice_version,
second_factor_code)`` aplica la política de contraseña (BR-NUC-20), confirma la inscripción con
un primer código si el rol la exige (BR-NUC-32: hasta entonces la cuenta no puede iniciar sesión),
exige la aceptación de la versión vigente del aviso y, en una transacción, marca la invitación
``accepted``, la cuenta ``active``, guarda el hash de la contraseña, escribe la aceptación del
aviso (``privacy_notice_accepted``), audita ``user_activated`` y publica ``user_activated``. Un
token inexistente, usado, vencido o cancelado, de una cuenta que no está invitada o de una
organización suspendida responde **igual**: ``invitation_invalid``.

Antes de conocer la organización, la única búsqueda es ``identity.invitation_organization``
(``nuc_0010``), que solo devuelve el identificador de la organización del hash.
"""

from __future__ import annotations

import base64
import enum
import hashlib
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Literal, Protocol

from sqlalchemy import text

from vigia_platform.identity.adapters.session_store import password_algorithm_version
from vigia_platform.identity.application.common import (
    IdentityDependencies,
    IdentityRejected,
    IdentityRejection,
    as_uuid,
)
from vigia_platform.identity.application.privacy_notice import (
    PrivacyNotice,
    current_notice,
    record_acceptance,
    require_current_version,
)
from vigia_platform.identity.auth.passwords import PasswordHash, PolicyResult
from vigia_platform.identity.auth.second_factor import (
    EnrollmentChallenge,
    SecondFactorUser,
    TotpCredential,
)
from vigia_platform.ledger.application.audit_writer import AuditOperation, ResourceRef
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.outbox.publish import NewEvent
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "INVITATION_TOKEN_BYTES",
    "INVITATION_VALIDITY",
    "ActivatedAccount",
    "ActivationStart",
    "EmailSenderPort",
    "EmailSenderRegistry",
    "InvitationDelivery",
    "InvitationEmail",
    "InvitationOutcome",
    "InvitationService",
    "IssuedInvitation",
    "checked_link_base",
    "deliver",
    "invitation_link",
    "issue_invitation",
    "new_token",
    "token_hash",
]

_log = get_logger("identity.invitations")

INVITATION_TOKEN_BYTES: Final = 32
"""256 bits de azar por invitación (BR-NUC-30 exige ≥ 128)."""
INVITATION_VALIDITY: Final = timedelta(hours=72)
_TOKEN: Final = re.compile(r"[A-Za-z0-9_-]{43}")
_LINK_BASE: Final = re.compile(r"https://[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?")
_USER: Final = "user"
_INVITATION: Final = "invitation"


def new_token(random_bytes: Callable[[int], bytes]) -> str:
    """Token de invitación: 32 bytes aleatorios en base64url sin relleno (43 caracteres)."""
    raw = random_bytes(INVITATION_TOKEN_BYTES)
    if not isinstance(raw, bytes) or len(raw) != INVITATION_TOKEN_BYTES:
        raise ValueError("el generador no devolvió 32 bytes")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def token_hash(token: str) -> str:
    """SHA-256 en hexadecimal del token: lo único que se guarda."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def invitation_link(base_url: str, token: str) -> str:
    """El enlace de activación: el token va en el fragmento, que el navegador no envía."""
    return f"{base_url.rstrip('/')}/invitacion#{token}"


def _well_formed(token: object) -> bool:
    return isinstance(token, str) and _TOKEN.fullmatch(token) is not None


# --- Puerto del correo (lo aporta U-04) ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InvitationEmail:
    """El correo de invitación (``EmailMessage`` de U-04 con ``kind = platform_invitation``)."""

    organization_id: uuid.UUID
    to_user_id: uuid.UUID
    to_email: str = field(repr=False)
    subject_es: str
    body_es: str = field(repr=False)
    related_ids: tuple[uuid.UUID, ...]
    kind: Literal["platform_invitation"] = "platform_invitation"


class EmailSenderPort(Protocol):
    """``EmailSenderPort`` (business-logic-model §10.1, nota de U-04): lo registra U-04."""

    async def send(
        self, context: ScopeContext, message: InvitationEmail
    ) -> Literal["queued", "unavailable"]: ...

    def circuit_state(self) -> Literal["closed", "open", "half_open"]: ...


class EmailSenderRegistry:
    """La ranura donde U-04 registra su ``EmailSenderPort`` al arrancar (a lo sumo uno).

    Ninguna ruta de U-02 depende de que esté: sin él, el enlace se divulga una vez.
    """

    def __init__(self) -> None:
        self._sender: EmailSenderPort | None = None

    def register(self, sender: EmailSenderPort) -> None:
        if self._sender is not None:
            raise ValueError("ya hay un EmailSenderPort registrado")
        self._sender = sender

    @property
    def sender(self) -> EmailSenderPort | None:
        return self._sender


# --- Emisión y entrega --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IssuedInvitation:
    invitation_id: uuid.UUID
    user_id: uuid.UUID
    expires_at: datetime
    token: str = field(repr=False)


class InvitationDelivery(enum.StrEnum):
    EMAIL_QUEUED = "email_queued"
    """El correo quedó en cola en U-04; el enlace no se muestra."""
    LINK_DISCLOSED = "link_disclosed"
    """Sin correo (o a petición): el enlace va en ``InvitationOutcome.link``, esta única vez."""


@dataclass(frozen=True, slots=True)
class InvitationOutcome:
    """Lo que recibe quien invita o reactiva."""

    user_id: uuid.UUID
    invitation_id: uuid.UUID
    expires_at: datetime
    delivery: InvitationDelivery
    link: str | None = field(default=None, repr=False)


_CANCEL_PENDING: Final = text(
    "UPDATE identity.invitation SET status = 'cancelled'"
    " WHERE user_id = :user_id AND status = 'pending'"
)
_INSERT_INVITATION: Final = text(
    "INSERT INTO identity.invitation (invitation_id, organization_id, user_id, token_hash,"
    " issued_at, expires_at, status, invited_by) VALUES (:invitation_id, :organization_id,"
    " :user_id, :token_hash, :issued_at, :expires_at, 'pending', :invited_by)"
)
_MARK_DISCLOSED: Final = text(
    "UPDATE identity.invitation SET disclosed_to_inviter_at = :now"
    " WHERE invitation_id = :invitation_id AND status = 'pending'"
    " AND disclosed_to_inviter_at IS NULL RETURNING invitation_id"
)


async def issue_invitation(
    deps: IdentityDependencies, transaction: Transaction, user_id: uuid.UUID, now: datetime
) -> IssuedInvitation:
    """Invitación nueva de ``user_id`` (cancela la pendiente), ``user_invited`` y su evento."""
    context = transaction.context
    token = new_token(deps.random_bytes)
    invitation_id = uuid7(deps.clock, deps.random_bytes)
    expires_at = now + INVITATION_VALIDITY
    await transaction.execute(_CANCEL_PENDING, {"user_id": user_id})
    await transaction.execute(
        _INSERT_INVITATION,
        {
            "invitation_id": invitation_id,
            "organization_id": context.organization_id,
            "user_id": user_id,
            "token_hash": token_hash(token),
            "issued_at": now,
            "expires_at": expires_at,
            "invited_by": context.actor.id,
        },
    )
    await deps.audit.append(
        context,
        AuditOperation.USER_INVITED,
        resource=ResourceRef(_USER, user_id),
        filters={"invitation_id": str(invitation_id)},
        transaction=transaction,
    )
    await deps.outbox.publish(
        transaction,
        NewEvent(
            event_name="user_invited",
            payload={
                "user_id": str(user_id),
                "invitation_id": str(invitation_id),
                "invited_by": str(context.actor.id),
                "expires_at": format_timestamp(expires_at),
            },
        ),
    )
    return IssuedInvitation(invitation_id, user_id, expires_at, token)


def _message(
    context: ScopeContext, issued: IssuedInvitation, email: str, link: str
) -> InvitationEmail:
    # El texto definitivo es una plantilla de U-04; este es el mínimo para poder activar.
    body = (
        "Te invitaron a Vigía. Para activar tu cuenta abre este enlace antes de que venza"
        f" ({format_timestamp(issued.expires_at)}):\n\n{link}\n\n"
        "Si no esperabas esta invitación, ignora este correo."
    )
    return InvitationEmail(
        organization_id=context.organization_id,
        to_user_id=issued.user_id,
        to_email=email,
        subject_es="Invitación a Vigía",
        body_es=body,
        related_ids=(issued.invitation_id,),
    )


async def deliver(
    deps: IdentityDependencies,
    senders: EmailSenderRegistry,
    context: ScopeContext,
    issued: IssuedInvitation,
    *,
    email: str,
    link_base: str,
    disclose: bool,
) -> InvitationOutcome:
    """Entrega el enlace por correo o, si no se puede (o se pide), lo divulga una vez."""
    link = invitation_link(link_base, issued.token)
    sender = senders.sender
    if sender is not None and not disclose:
        try:
            result = await sender.send(context, _message(context, issued, email, link))
        except Exception:
            # El fallo del correo no deja al administrador sin enlace: se degrada a mostrarlo.
            _log.warning("el envío del correo de invitación falló; se divulga el enlace")
            result = "unavailable"
        if result == "queued":
            return InvitationOutcome(
                issued.user_id,
                issued.invitation_id,
                issued.expires_at,
                InvitationDelivery.EMAIL_QUEUED,
            )
    now = deps.clock.now()
    async with deps.database.transaction(context) as transaction:
        marked = (
            await transaction.execute(
                _MARK_DISCLOSED, {"invitation_id": issued.invitation_id, "now": now}
            )
        ).first()
        if marked is None:  # pragma: no cover - la invitación es de esta misma operación
            raise IdentityRejected(IdentityRejection.INVITATION_INVALID)
        await deps.audit.append(
            context,
            AuditOperation.INVITATION_LINK_DISCLOSED,
            resource=ResourceRef(_INVITATION, issued.invitation_id),
            filters={"user_id": str(issued.user_id)},
            transaction=transaction,
        )
    return InvitationOutcome(
        issued.user_id,
        issued.invitation_id,
        issued.expires_at,
        InvitationDelivery.LINK_DISCLOSED,
        link,
    )


def checked_link_base(value: object) -> str:
    """La URL base de la aplicación para los enlaces (``https://``, sin consulta)."""
    if not isinstance(value, str) or _LINK_BASE.fullmatch(value) is None:
        raise ValueError("link_base debe ser una URL https sin consulta ni fragmento")
    return value


# --- Activación ---------------------------------------------------------------------------------


class ActivationContexts(Protocol):
    """El constructor de contexto de la organización antes de conocer a la persona."""

    def anonymous(self, organization_id: uuid.UUID) -> ScopeContext: ...


class ActivationPasswords(Protocol):
    """``PasswordService``: política (BR-NUC-20) y hash Argon2id."""

    async def check_policy(self, password: str, email: str) -> PolicyResult: ...

    async def hash(self, password: str) -> PasswordHash: ...


class ActivationSecondFactor(Protocol):
    """``SecondFactorService``: inscripción y confirmación con el primer código."""

    async def credential(
        self, context: ScopeContext, user_id: uuid.UUID
    ) -> TotpCredential | None: ...

    async def enroll(
        self, context: ScopeContext, user: SecondFactorUser
    ) -> EnrollmentChallenge: ...

    async def confirm_enrollment(
        self, context: ScopeContext, credential: TotpCredential, code: str, now: datetime
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class ActivationStart:
    """Lo que la pantalla de activación necesita antes de pedir la contraseña."""

    second_factor_required: bool
    enrollment: EnrollmentChallenge | None
    notice: PrivacyNotice


@dataclass(frozen=True, slots=True)
class ActivatedAccount:
    user_id: uuid.UUID
    organization_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class _Pending:
    context: ScopeContext
    invitation_id: uuid.UUID
    user_id: uuid.UUID
    email: str = field(repr=False)
    second_factor_required: bool


_INVITATION_ORGANIZATION: Final = text(
    "SELECT identity.invitation_organization(:token_hash) AS organization_id"
)
_PENDING_INVITATION: Final = text(
    "SELECT i.invitation_id, i.user_id, u.email, u.second_factor_required"
    " FROM identity.invitation AS i"
    " JOIN identity.user_account AS u ON u.user_id = i.user_id"
    " JOIN identity.organization AS o ON o.organization_id = i.organization_id"
    " WHERE i.token_hash = :token_hash AND i.status = 'pending' AND i.expires_at > :now"
    " AND u.status = 'invited' AND o.status = 'active'"
)
_ACCEPT_INVITATION: Final = text(
    "UPDATE identity.invitation SET status = 'accepted', accepted_at = :now"
    " WHERE invitation_id = :invitation_id AND status = 'pending' AND expires_at > :now"
    " RETURNING invitation_id"
)
_ACTIVATE_USER: Final = text(
    "UPDATE identity.user_account SET status = 'active', password_updated_at = :now"
    " WHERE user_id = :user_id AND status = 'invited' RETURNING user_id"
)
_UPSERT_PASSWORD: Final = text(
    "INSERT INTO identity.password_credential (user_id, organization_id, password_hash,"
    " algorithm_version, updated_at, breach_checked_at) VALUES (:user_id, :organization_id,"
    " :password_hash, :algorithm_version, :now, :breach_checked_at)"
    " ON CONFLICT (user_id) DO UPDATE SET password_hash = EXCLUDED.password_hash,"
    " algorithm_version = EXCLUDED.algorithm_version, updated_at = EXCLUDED.updated_at,"
    " breach_checked_at = EXCLUDED.breach_checked_at"
)


class InvitationService:
    """``accept_invitation`` y su paso previo (``begin_activation``).

    No recibe ``ScopeContext``: la ruta es pública y el contexto de la organización lo construye
    a partir del enlace (``ActivationContexts.anonymous``).
    """

    def __init__(
        self,
        deps: IdentityDependencies,
        *,
        contexts: ActivationContexts,
        passwords: ActivationPasswords,
        second_factor: ActivationSecondFactor,
    ) -> None:
        self._deps = deps
        self._contexts = contexts
        self._passwords = passwords
        self._second_factor = second_factor

    def __repr__(self) -> str:
        return "InvitationService()"

    async def _pending(self, token: object) -> _Pending:
        if not isinstance(token, str) or not _well_formed(token):
            raise IdentityRejected(IdentityRejection.INVITATION_INVALID)
        hashed = token_hash(token)
        deps = self._deps
        lookup = self._contexts.anonymous(deps.provider_organization_id)
        rows = await deps.database.read(lookup, _INVITATION_ORGANIZATION, {"token_hash": hashed})
        if not rows or rows[0].organization_id is None:
            raise IdentityRejected(IdentityRejection.INVITATION_INVALID)
        context = self._contexts.anonymous(as_uuid(rows[0].organization_id))
        found = await deps.database.read(
            context, _PENDING_INVITATION, {"token_hash": hashed, "now": deps.clock.now()}
        )
        if not found:
            raise IdentityRejected(IdentityRejection.INVITATION_INVALID)
        row = found[0]
        return _Pending(
            context=context,
            invitation_id=as_uuid(row.invitation_id),
            user_id=as_uuid(row.user_id),
            email=row.email,
            second_factor_required=bool(row.second_factor_required),
        )

    async def begin_activation(self, token: str) -> ActivationStart:
        """Valida el enlace y, si el rol lo exige, inscribe el segundo factor (QR una vez)."""
        pending = await self._pending(token)
        enrollment: EnrollmentChallenge | None = None
        if pending.second_factor_required:
            enrollment = await self._second_factor.enroll(
                pending.context,
                SecondFactorUser(pending.user_id, pending.context.organization_id, pending.email),
            )
        return ActivationStart(pending.second_factor_required, enrollment, current_notice())

    async def accept_invitation(
        self,
        token: str,
        password: str,
        notice_version: str,
        second_factor_code: str | None = None,
    ) -> ActivatedAccount:
        """Activa la cuenta del enlace (BR-NUC-32); ``IdentityRejected`` con el motivo."""
        pending = await self._pending(token)
        version = require_current_version(notice_version)
        if not isinstance(password, str):
            raise IdentityRejected(IdentityRejection.PASSWORD_REJECTED, field="/password")
        policy = await self._passwords.check_policy(password, pending.email)
        if not policy.ok:
            raise IdentityRejected(
                IdentityRejection.PASSWORD_REJECTED,
                field="/password",
                details=tuple(violation.value for violation in policy.violations),
            )
        deps = self._deps
        context = pending.context
        await self._confirm_second_factor(pending, second_factor_code)
        hashed = await self._passwords.hash(password)
        now = deps.clock.now()
        async with deps.database.transaction(context) as transaction:
            accepted = (
                await transaction.execute(
                    _ACCEPT_INVITATION, {"invitation_id": pending.invitation_id, "now": now}
                )
            ).first()
            activated = (
                await transaction.execute(_ACTIVATE_USER, {"user_id": pending.user_id, "now": now})
            ).first()
            if accepted is None or activated is None:
                raise IdentityRejected(IdentityRejection.INVITATION_INVALID)
            await transaction.execute(
                _UPSERT_PASSWORD,
                {
                    "user_id": pending.user_id,
                    "organization_id": context.organization_id,
                    "password_hash": hashed.encoded,
                    "algorithm_version": password_algorithm_version(hashed.algorithm_version),
                    "now": now,
                    "breach_checked_at": None if policy.breach_source is None else now,
                },
            )
            await record_acceptance(deps, transaction, pending.user_id, version, now)
            await deps.audit.append(
                context,
                AuditOperation.USER_ACTIVATED,
                resource=ResourceRef(_USER, pending.user_id),
                filters={"invitation_id": str(pending.invitation_id)},
                transaction=transaction,
            )
            await deps.outbox.publish(
                transaction,
                NewEvent(
                    event_name="user_activated",
                    payload={
                        "user_id": str(pending.user_id),
                        "activated_at": format_timestamp(now),
                    },
                ),
            )
        return ActivatedAccount(pending.user_id, context.organization_id)

    async def _confirm_second_factor(self, pending: _Pending, code: str | None) -> None:
        """Con rol que lo exige, la inscripción queda confirmada antes de activar (BR-NUC-32)."""
        credential = await self._second_factor.credential(pending.context, pending.user_id)
        if credential is not None and credential.usable:
            return
        pending_credential = credential is not None and credential.active
        if not pending.second_factor_required and (code is None or not pending_credential):
            return
        if credential is None or not pending_credential or not isinstance(code, str):
            raise IdentityRejected(
                IdentityRejection.SECOND_FACTOR_REQUIRED, field="/second_factor_code"
            )
        confirmed = await self._second_factor.confirm_enrollment(
            pending.context, credential, code, self._deps.clock.now()
        )
        if not confirmed:
            raise IdentityRejected(
                IdentityRejection.SECOND_FACTOR_REQUIRED, field="/second_factor_code"
            )
