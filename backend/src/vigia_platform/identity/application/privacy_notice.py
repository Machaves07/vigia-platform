"""Aviso de tratamiento de datos de los usuarios (LC-NUC-05; NFR-NUC-29; BR-NUC-32).

- ``current_notice()``: la versión vigente (``identity.domain.privacy_notice``) con su texto, que
  vive versionado en ``backend/resources/privacy-notice/<versión>.md``. Hoy es el **texto
  marcador** ``v0-pendiente``, identificado como pendiente del abogado del gate 21 (P4).
- ``record_acceptance``: en la transacción de quien acepta, escribe ``PrivacyNoticeAcceptance``
  (solo anexar), fija ``user_account.privacy_notice_version_accepted`` y audita
  ``privacy_notice_accepted`` con la versión. La usan la activación de la cuenta
  (``identity.application.invitations``) y la aceptación de una versión nueva.
- ``PrivacyNoticeService.accept``: la aceptación de una versión nueva en el siguiente inicio de
  sesión (ruta ``POST /privacy-notice/accept``, TASK-135). La puerta es el paso
  ``PrivacyNoticeStep`` de la cadena de middleware (LC-NUC-20, VIG-78): mientras
  ``SessionScope.privacy_notice_version_accepted`` no sea la vigente, toda ruta con sesión salvo
  la de aceptación responde ``privacy_notice_required``. Este servicio es lo único que cambia esa
  versión. Es un repositorio registrado (``@repository``): sin contexto, ``ContextAbsent`` y
  ``context_absent_attempt`` (PR-NUC-02).

Solo se acepta la versión vigente: aceptar otra es ``privacy_notice_outdated``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Final

from sqlalchemy import text

from vigia_platform.identity.application.common import (
    IdentityDependencies,
    IdentityRejected,
    IdentityRejection,
)
from vigia_platform.identity.domain.privacy_notice import (
    CURRENT_PRIVACY_NOTICE_VERSION,
    PRIVACY_NOTICE_PENDING_LEGAL_TEXT,
    PRIVACY_NOTICE_VERSION_PATTERN,
)
from vigia_platform.ledger.application.audit_writer import AuditOperation, ResourceRef
from vigia_platform.shared.context import ActorKind, ContextOrigin, ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7

__all__ = [
    "NOTICE_DIRECTORY",
    "PrivacyNotice",
    "PrivacyNoticeService",
    "current_notice",
    "notice",
    "record_acceptance",
]

NOTICE_DIRECTORY: Final = Path(__file__).resolve().parents[4] / "resources" / "privacy-notice"
"""``backend/resources/privacy-notice`` (se copia a la imagen con ``backend/``)."""

_USER: Final = "user"


@dataclass(frozen=True, slots=True)
class PrivacyNotice:
    version: str
    text: str = field(repr=False)
    pending_legal_text: bool


@cache
def notice(version: str) -> PrivacyNotice:
    """El texto de ``version``; ``ValueError`` si la versión no tiene la forma o no existe."""
    if not isinstance(version, str) or PRIVACY_NOTICE_VERSION_PATTERN.fullmatch(version) is None:
        raise ValueError("versión del aviso fuera de la forma")
    path = NOTICE_DIRECTORY / f"{version}.md"
    if not path.is_file():
        raise ValueError("versión del aviso sin texto publicado")
    return PrivacyNotice(
        version=version,
        text=path.read_text(encoding="utf-8"),
        pending_legal_text=(
            PRIVACY_NOTICE_PENDING_LEGAL_TEXT and version == CURRENT_PRIVACY_NOTICE_VERSION
        ),
    )


def current_notice() -> PrivacyNotice:
    """La versión vigente con su texto (lo que la aplicación muestra para aceptar)."""
    return notice(CURRENT_PRIVACY_NOTICE_VERSION)


_INSERT_ACCEPTANCE: Final = text(
    "INSERT INTO identity.privacy_notice_acceptance (acceptance_id, organization_id, user_id,"
    " notice_version, accepted_at, correlation_id) VALUES (:acceptance_id, :organization_id,"
    " :user_id, :notice_version, :accepted_at, :correlation_id)"
)
_SET_ACCEPTED_VERSION: Final = text(
    "UPDATE identity.user_account SET privacy_notice_version_accepted = :notice_version"
    " WHERE user_id = :user_id"
)
_USER_FOR_ACCEPTANCE: Final = text(
    "SELECT status, privacy_notice_version_accepted FROM identity.user_account"
    " WHERE user_id = :user_id FOR UPDATE"
)


def require_current_version(version: object) -> str:
    """``version`` si es la vigente; si no, ``privacy_notice_outdated``."""
    if version != CURRENT_PRIVACY_NOTICE_VERSION:
        raise IdentityRejected(IdentityRejection.PRIVACY_NOTICE_OUTDATED, field="/notice_version")
    return CURRENT_PRIVACY_NOTICE_VERSION


async def record_acceptance(
    deps: IdentityDependencies,
    transaction: Transaction,
    user_id: uuid.UUID,
    version: str,
    now: datetime,
) -> uuid.UUID:
    """``PrivacyNoticeAcceptance``, la versión en la cuenta y ``privacy_notice_accepted``."""
    context = transaction.context
    acceptance_id = uuid7(deps.clock, deps.random_bytes)
    await transaction.execute(
        _INSERT_ACCEPTANCE,
        {
            "acceptance_id": acceptance_id,
            "organization_id": context.organization_id,
            "user_id": user_id,
            "notice_version": version,
            "accepted_at": now,
            "correlation_id": context.correlation_id,
        },
    )
    await transaction.execute(
        _SET_ACCEPTED_VERSION, {"user_id": user_id, "notice_version": version}
    )
    await deps.audit.append(
        context,
        AuditOperation.PRIVACY_NOTICE_ACCEPTED,
        resource=ResourceRef(_USER, user_id),
        filters={"notice_version": version},
        transaction=transaction,
    )
    return acceptance_id


@repository
class PrivacyNoticeService:
    """Aceptación de la versión vigente con sesión (``POST /privacy-notice/accept``)."""

    def __init__(self, deps: IdentityDependencies) -> None:
        self._deps = deps

    async def accept(self, context: ScopeContext, version: str) -> bool:
        """Acepta ``version`` (la vigente); ``False`` si ya estaba aceptada (sin escribir nada).

        ``context`` es el de la sesión de la ruta de aceptación (``SessionScope.context``): la
        persona de la sesión acepta por sí misma, nunca bajo concesión ni desde un contexto que
        no sea de sesión.
        """
        if (
            context.origin is not ContextOrigin.SESSION
            or context.actor.kind is not ActorKind.USER
            or context.concession_id is not None
        ):
            raise IdentityRejected(IdentityRejection.USER_STATE)
        user_id = context.actor.id
        accepted = require_current_version(version)
        deps = self._deps
        async with deps.database.transaction(context) as transaction:
            row = (await transaction.execute(_USER_FOR_ACCEPTANCE, {"user_id": user_id})).first()
            if row is None or row.status != "active":
                raise IdentityRejected(IdentityRejection.USER_STATE)
            if row.privacy_notice_version_accepted == accepted:
                return False
            await record_acceptance(deps, transaction, user_id, accepted, deps.clock.now())
        return True
