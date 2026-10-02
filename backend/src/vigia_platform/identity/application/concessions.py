"""``identity.concessions``: el acceso temporal del proveedor (LC-NUC-06; S-PLA-02; BR-NUC-35 a 42).

La máquina de estados de ``business-logic-model.md`` §3: una concesión nace ``active``, pasa a
``revoked`` si el cliente o el proveedor la revocan, o a ``expired`` cuando llega su
``expires_at``; no vuelve a ``active`` (se concede otra). La tabla
``identity.provider_concession`` es la proyección de los registros del expediente del cliente:
cada transición escribe la fila **y** su registro en la misma transacción (``projection`` de
``EscritorExpediente.write``), con su evento en la bandeja.

- ``grant(context, …)``: un usuario de la organización proveedora con ``concessions.grant`` se
  concede acceso sobre una organización **cliente** activa, con alcance de organización o de
  planta, motivo de 10 a 500 caracteres y duración en ``[1 h, concession_max_days]`` del cliente
  (sin duración, ``concession_default_days``). Entra en vigor de inmediato: fila,
  ``provider_concession_granted`` en la cadena del cliente (planta u organización, BR-NUC-45) y
  ``concession_granted`` (BR-NUC-35, 36). El concesionario es siempre el actor de la sesión: no
  existe conceder a nombre de otro, ni sobre la proveedora (BR-NUC-42).
- ``revoke(context, concession_id)``: el cliente (``concessions.revoke`` sobre el alcance de la
  concesión) o el proveedor (el concesionario o un operador con ``concessions.revoke`` en la
  proveedora). ``provider_concession_revoked`` y ``concession_revoked``; la petición siguiente del
  proveedor ya no obtiene contexto, porque cada contexto se construye consultando el estado
  (BR-NUC-39).
- ``expire_due(transaction)``: la tarea ``expire_concessions`` (cada 5 minutos, una organización
  por iteración) deja constancia con ``provider_concession_expired`` y ``concession_expired`` de
  cada concesión activa cuyo ``expires_at`` pasó. Ninguna petición depende de ella: la vigencia
  se comprueba al construir cada contexto (BR-NUC-40).
- ``list_concessions`` y ``list_provider_queries``: el panel del cliente con ``concessions.read``,
  historia completa (vigentes, vencidas, revocadas y cada ``provider_query`` con momento,
  operación y el motivo de la concesión), cada lectura auditada como ``ledger_read`` en su misma
  transacción (BR-NUC-41).
- ``list_own_concessions``: el lado proveedor (``GET /provider/concessions``, TASK-136), las
  concesiones que el actor se concedió, sin el motivo (``identity.provider_concessions_of`` de
  ``nuc_0012``).

Lo que no hace: las rutas (TASK-136, ``identity.adapters.http.concessions``) y la notificación al
cliente (consumidor de U-04 de
``concession_granted``). ``provider_query`` lo escribe ``identity.authz.context`` al terminar cada
petición bajo concesión (BR-NUC-38).

Errores: un recurso inexistente o fuera de alcance es ``ResourceNotFound`` (``not_found``, nunca
``forbidden``); una entrada fuera de las reglas, ``ConcessionRejected`` con un código de lista
cerrada. El estado que se muestra es honesto (P5): una concesión ``active`` con ``expires_at`` ya
pasado se presenta ``expired`` aunque la tarea aún no haya corrido.
"""

from __future__ import annotations

import enum
import os
import unicodedata
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

from vigia_platform.identity.authz.authorize import Authorizer, Resource, ResourceNotFound
from vigia_platform.identity.authz.context import ScopeContexts
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.writer import (
    EscritorExpediente,
    LedgerRejection,
    LedgerRejectionCode,
    RecordScope,
)
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import (
    ActorUnit,
    ContextOrigin,
    ScopeContext,
    ScopeLevel,
    repository,
)
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.outbox.publish import NewEvent
from vigia_platform.shared.outbox.registries import (
    PeriodicTask,
    PeriodicTaskRegistry,
    Schedule,
)
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "EXPIRE_CONCESSIONS",
    "EXPIRE_CONCESSIONS_SCHEDULE",
    "MAX_PAGE_SIZE",
    "MAX_REASON_CHARS",
    "MIN_DURATION",
    "MIN_REASON_CHARS",
    "ClientTerms",
    "Concession",
    "ConcessionRejected",
    "ConcessionRejectionCode",
    "ConcessionService",
    "ConcessionStatus",
    "ConcessionStore",
    "ProviderQueryCursor",
    "ProviderQueryPage",
    "ProviderQueryView",
    "RevokedBySide",
    "concession_duration",
    "register_expire_concessions",
]

_log = get_logger("identity.concessions")

MIN_DURATION: Final = timedelta(hours=1)
"""Duración mínima de una concesión (BR-NUC-35)."""
MAX_CONCESSION_DAYS: Final = 90
"""Tope de ``Organization.concession_max_days`` (domain-entities §2.1)."""
MIN_REASON_CHARS: Final = 10
MAX_REASON_CHARS: Final = 500
MAX_PAGE_SIZE: Final = 200
"""Página de ``list_provider_queries`` (PAT-NUC-ESC-07)."""

EXPIRE_CONCESSIONS: Final = "expire_concessions"
EXPIRE_CONCESSIONS_SCHEDULE: Final = Schedule.every(5 * 60)
"""Cada 5 minutos (domain-entities §4.3)."""


class ConcessionStatus(enum.StrEnum):
    """``concession_status`` (domain-entities §1)."""

    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


class RevokedBySide(enum.StrEnum):
    CLIENT = "client"
    PROVIDER = "provider"


class ConcessionRejectionCode(enum.StrEnum):
    """Por qué no se concede o no se revoca (lista cerrada; nunca repite la entrada)."""

    REASON_INVALID = "reason_invalid"
    """Motivo fuera de 10 a 500 caracteres o rechazado por la política de texto libre."""
    DURATION_OUT_OF_RANGE = "duration_out_of_range"
    """Duración fuera de ``[1 h, concession_max_days]`` del cliente."""
    SCOPE_INVALID = "scope_invalid"
    """Alcance que no es la organización cliente ni una planta."""
    CONCESSION_CLOSED = "concession_closed"
    """La concesión ya está revocada o vencida: no se revoca otra vez."""
    LEDGER_REJECTED = "ledger_rejected"
    """El expediente rechazó el registro (``rejection`` lleva su código)."""


class ConcessionRejected(ValueError):
    """La operación no se hizo: ``code`` es de ``ConcessionRejectionCode``."""

    def __init__(
        self, code: ConcessionRejectionCode, rejection: LedgerRejection | None = None
    ) -> None:
        super().__init__(f"concesión rechazada: {code.value}")
        self.code = code
        self.rejection = rejection


def _floor_ms(moment: datetime) -> datetime:
    """La marca a milisegundos, como la escriben los registros (``format_timestamp``)."""
    return moment.replace(microsecond=moment.microsecond // 1000 * 1000)


def concession_duration(
    requested: timedelta | None, *, max_days: int, default_days: int
) -> timedelta:
    """La duración de una concesión nueva (BR-NUC-35; PR-NUC-12).

    Sin ``requested``, ``default_days``. Con ella, tiene que estar en ``[1 h, max_days]``; si no,
    ``ConcessionRejected(duration_out_of_range)``. ``max_days`` y ``default_days`` son los del
    cliente (``1 <= default_days <= max_days <= 90``); otros valores son un error del llamador.
    """
    for name, value in (("max_days", max_days), ("default_days", default_days)):
        if type(value) is not int:
            raise TypeError(f"{name} debe ser int")
    if not 1 <= default_days <= max_days <= MAX_CONCESSION_DAYS:
        raise ValueError("se exige 1 <= default_days <= max_days <= 90")
    if requested is None:
        return timedelta(days=default_days)
    if type(requested) is not timedelta:
        raise ConcessionRejected(ConcessionRejectionCode.DURATION_OUT_OF_RANGE)
    if not MIN_DURATION <= requested <= timedelta(days=max_days):
        raise ConcessionRejected(ConcessionRejectionCode.DURATION_OUT_OF_RANGE)
    return requested


def _reason(value: object) -> str:
    """El motivo en NFC (la forma que guarda el expediente) con 10 a 500 caracteres."""
    if type(value) is not str:
        raise ConcessionRejected(ConcessionRejectionCode.REASON_INVALID)
    normalized = unicodedata.normalize("NFC", value)
    if not MIN_REASON_CHARS <= len(normalized) <= MAX_REASON_CHARS:
        raise ConcessionRejected(ConcessionRejectionCode.REASON_INVALID)
    return normalized


# --- Valores y puerto del almacén ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClientTerms:
    """``concession_max_days`` y ``concession_default_days`` de un cliente activo."""

    max_days: int
    default_days: int


@dataclass(frozen=True, slots=True)
class Concession:
    """Una fila de ``identity.provider_concession`` (domain-entities §2.11).

    ``reason`` es ``None`` cuando se lee desde la proveedora (``provider_concession_of`` no lo
    devuelve). ``status`` es el persistido; ``effective_status(now)`` el que se muestra.
    """

    concession_id: uuid.UUID
    organization_id: uuid.UUID
    provider_user_id: uuid.UUID
    provider_organization_id: uuid.UUID
    scope_level: ScopeLevel
    scope_id: uuid.UUID
    reason: str | None
    granted_at: datetime
    expires_at: datetime
    status: ConcessionStatus
    revoked_at: datetime | None = None
    revoked_by: uuid.UUID | None = None
    revoked_by_side: RevokedBySide | None = None

    @property
    def plant_id(self) -> uuid.UUID | None:
        """La planta del alcance, o ``None`` con alcance de organización."""
        return self.scope_id if self.scope_level is ScopeLevel.PLANT else None

    def in_force(self, now: datetime) -> bool:
        """Activa, sin revocar y con ``granted_at <= now < expires_at`` (BR-NUC-40)."""
        return (
            self.status is ConcessionStatus.ACTIVE
            and self.revoked_at is None
            and self.granted_at <= now < self.expires_at
        )

    def effective_status(self, now: datetime) -> ConcessionStatus:
        """``expired`` si ya venció aunque la tarea no haya dejado constancia (P5)."""
        if self.status is ConcessionStatus.ACTIVE and now >= self.expires_at:
            return ConcessionStatus.EXPIRED
        return self.status

    def resource(self) -> Resource:
        """El recurso que se autoriza: la organización o la planta del alcance."""
        return Resource(
            self.organization_id, "provider_concession", self.concession_id, plant_id=self.plant_id
        )


@dataclass(frozen=True, slots=True)
class ProviderQueryCursor:
    """Clave de la última consulta de una página (``received_at, record_id``)."""

    received_at: datetime
    record_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class ProviderQueryView:
    """Un ``provider_query`` del expediente del cliente, con el motivo de su concesión (H-56)."""

    record_id: uuid.UUID
    received_at: datetime
    plant_id: uuid.UUID | None
    concession_id: uuid.UUID
    provider_user_id: uuid.UUID
    operation: str
    method: str
    resource: str
    occurred_at: str
    reason: str | None

    @property
    def cursor(self) -> ProviderQueryCursor:
        return ProviderQueryCursor(self.received_at, self.record_id)


@dataclass(frozen=True, slots=True)
class ProviderQueryPage:
    items: tuple[ProviderQueryView, ...]
    next_cursor: ProviderQueryCursor | None


class ConcessionStore(Protocol):
    """Puerto del almacén (adaptador en ``identity.adapters.concession_store``)."""

    async def client_terms(
        self, context: ScopeContext, client_organization_id: uuid.UUID
    ) -> ClientTerms | None:
        """Términos de un cliente activo, vistos desde la proveedora; ``None`` si no hay."""
        ...

    async def concession(
        self, context: ScopeContext, concession_id: uuid.UUID
    ) -> Concession | None:
        """La concesión visible en el contexto (el del cliente), o ``None``."""
        ...

    async def provider_concession(
        self, context: ScopeContext, concession_id: uuid.UUID
    ) -> Concession | None:
        """Una concesión de la proveedora vista desde ella (sin el motivo), o ``None``."""
        ...

    async def insert(self, transaction: Transaction, concession: Concession) -> None:
        """La fila nueva; ``ResourceNotFound`` o ``ConcessionRejected`` si la base la rechaza."""
        ...

    async def revoke(
        self,
        transaction: Transaction,
        concession: Concession,
        *,
        revoked_at: datetime,
        revoked_by: uuid.UUID,
        side: RevokedBySide,
    ) -> None:
        """Cierra la fila como ``revoked``; ``ConcessionRejected(closed)`` si ya no está activa."""
        ...

    async def expire(self, transaction: Transaction, concession: Concession, now: datetime) -> None:
        """Cierra la fila como ``expired``; ``ConcessionRejected(closed)`` si no procede."""
        ...

    async def due_for_expiry(
        self, transaction: Transaction, now: datetime
    ) -> tuple[Concession, ...]:
        """Las concesiones activas de la organización con ``expires_at <= now``."""
        ...

    async def list_concessions(
        self, context: ScopeContext, plant_id: uuid.UUID | None
    ) -> tuple[Concession, ...]:
        """Todas (cualquier estado), de la organización o de una planta; audita la lectura."""
        ...

    async def provider_concessions(
        self, context: ScopeContext, grantee: uuid.UUID
    ) -> tuple[Concession, ...]:
        """Las concesiones de la proveedora concedidas a ``grantee``, sin el motivo."""
        ...

    async def provider_queries(
        self,
        context: ScopeContext,
        concession: Concession,
        after: ProviderQueryCursor | None,
        limit: int,
    ) -> ProviderQueryPage:
        """Los ``provider_query`` de la concesión en orden de llegada; audita la lectura."""
        ...


# --- Servicio ------------------------------------------------------------------------------------


@repository
class ConcessionService:
    """Servicios de las rutas de concesiones (``business-logic-model.md`` §10.2) y la tarea."""

    def __init__(
        self,
        *,
        store: ConcessionStore,
        writer: EscritorExpediente,
        authorizer: Authorizer,
        contexts: ScopeContexts,
        clock: Clock,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._store = store
        self._writer = writer
        self._authorizer = authorizer
        self._contexts = contexts
        self._clock = clock
        self._random_bytes = random_bytes
        self._provider_organization_id = contexts.provider_organization_id

    def __repr__(self) -> str:
        return "ConcessionService()"

    def now(self) -> datetime:
        """El instante del reloj inyectado, para mostrar ``effective_status`` (P5)."""
        return self._clock.now()

    # --- grant ---------------------------------------------------------------------------------

    async def grant(
        self,
        context: ScopeContext,
        *,
        client_organization_id: uuid.UUID,
        scope_level: ScopeLevel | str,
        scope_id: uuid.UUID,
        reason: str,
        duration: timedelta | None = None,
    ) -> Concession:
        """Concede acceso al actor de ``context`` sobre el cliente (BR-NUC-35, 36, 42)."""
        context = self._provider_session(context)
        authorized = await self._authorizer.authorize(
            context,
            PermissionKey.CONCESSIONS_GRANT,
            Resource.organization(self._provider_organization_id),
        )
        text = _reason(reason)
        level = self._scope(client_organization_id, scope_level, scope_id)
        if client_organization_id == self._provider_organization_id:
            raise ResourceNotFound()  # BR-NUC-42: nunca sobre la proveedora
        terms = await self._store.client_terms(authorized, client_organization_id)
        if terms is None:
            raise ResourceNotFound()
        length = concession_duration(
            duration, max_days=terms.max_days, default_days=terms.default_days
        )
        granted_at = _floor_ms(self._clock.now())
        concession = Concession(
            concession_id=self._uuid7(),
            organization_id=client_organization_id,
            provider_user_id=authorized.actor.id,
            provider_organization_id=self._provider_organization_id,
            scope_level=level,
            scope_id=scope_id,
            reason=text,
            granted_at=granted_at,
            expires_at=_floor_ms(granted_at + length),
            status=ConcessionStatus.ACTIVE,
        )
        grant_context = self._contexts.provider_concession_context(
            authorized,
            concession_id=concession.concession_id,
            client_organization_id=client_organization_id,
            scope_level=level,
            scope_id=scope_id,
        )
        content = {
            "concession_id": str(concession.concession_id),
            "provider_organization_id": str(concession.provider_organization_id),
            "provider_user_id": str(concession.provider_user_id),
            "scope_level": level.value,
            "scope_id": str(scope_id),
            "reason": text,
            "granted_at": format_timestamp(concession.granted_at),
            "expires_at": format_timestamp(concession.expires_at),
        }
        event = NewEvent(
            event_name="concession_granted",
            payload={
                "concession_id": str(concession.concession_id),
                "provider_organization_id": str(concession.provider_organization_id),
                "scope_level": level.value,
                "scope_id": str(scope_id),
                "granted_by": str(concession.provider_user_id),
                "expires_at": format_timestamp(concession.expires_at),
            },
        )

        async def insert(transaction: Transaction) -> None:
            await self._store.insert(transaction, concession)

        await self._write(
            grant_context, "provider_concession_granted", content, concession, event, insert
        )
        return concession

    def _provider_session(self, context: ScopeContext) -> ScopeContext:
        """El contexto de sesión de un usuario de la proveedora, sin concesión."""
        if (
            context.origin is not ContextOrigin.SESSION
            or context.organization_id != self._provider_organization_id
            or context.concession_id is not None
        ):
            raise ResourceNotFound()
        return context

    def _scope(
        self, client_organization_id: object, scope_level: object, scope_id: object
    ) -> ScopeLevel:
        if (
            type(client_organization_id) is not uuid.UUID
            or type(scope_id) is not uuid.UUID
            or not isinstance(scope_level, str)
        ):
            raise ConcessionRejected(ConcessionRejectionCode.SCOPE_INVALID)
        try:
            level = ScopeLevel(scope_level)
        except ValueError:
            raise ConcessionRejected(ConcessionRejectionCode.SCOPE_INVALID) from None
        if level is ScopeLevel.ZONE or (
            level is ScopeLevel.ORGANIZATION and scope_id != client_organization_id
        ):
            raise ConcessionRejected(ConcessionRejectionCode.SCOPE_INVALID)
        return level

    # --- revoke --------------------------------------------------------------------------------

    async def revoke(self, context: ScopeContext, concession_id: uuid.UUID) -> Concession:
        """Revoca la concesión desde el cliente o desde la proveedora (BR-NUC-39)."""
        if type(concession_id) is not uuid.UUID or context.concession_id is not None:
            raise ResourceNotFound()
        if context.organization_id == self._provider_organization_id:
            return await self._revoke_as_provider(context, concession_id)
        concession = await self._store.concession(context, concession_id)
        if concession is None:
            raise ResourceNotFound()
        authorized = await self._authorizer.authorize(
            context, PermissionKey.CONCESSIONS_REVOKE, concession.resource()
        )
        return await self._close(authorized, concession, RevokedBySide.CLIENT)

    async def _revoke_as_provider(
        self, context: ScopeContext, concession_id: uuid.UUID
    ) -> Concession:
        context = self._provider_session(context)
        concession = await self._store.provider_concession(context, concession_id)
        if concession is None:
            raise ResourceNotFound()
        authorized = context
        if concession.provider_user_id != context.actor.id:
            # Otro usuario del proveedor: solo un operador (``concessions.revoke`` en la
            # proveedora); si no, ``authorization_denied`` y ``not_found``.
            authorized = await self._authorizer.authorize(
                context,
                PermissionKey.CONCESSIONS_REVOKE,
                Resource.organization(self._provider_organization_id),
            )
        derived = self._contexts.provider_concession_context(
            authorized,
            concession_id=concession.concession_id,
            client_organization_id=concession.organization_id,
            scope_level=concession.scope_level,
            scope_id=concession.scope_id,
        )
        return await self._close(derived, concession, RevokedBySide.PROVIDER)

    async def _close(
        self, context: ScopeContext, concession: Concession, side: RevokedBySide
    ) -> Concession:
        now = self._clock.now()
        if not concession.in_force(now):
            raise ConcessionRejected(ConcessionRejectionCode.CONCESSION_CLOSED)
        revoked_at = max(_floor_ms(now), concession.granted_at)
        revoked_by = context.actor.id
        content = {
            "concession_id": str(concession.concession_id),
            "revoked_at": format_timestamp(revoked_at),
            "revoked_by": str(revoked_by),
            "revoked_by_side": side.value,
        }
        event = NewEvent(
            event_name="concession_revoked",
            payload={
                "concession_id": str(concession.concession_id),
                "revoked_by": str(revoked_by),
                "revoked_at": format_timestamp(revoked_at),
            },
        )

        async def close(transaction: Transaction) -> None:
            await self._store.revoke(
                transaction, concession, revoked_at=revoked_at, revoked_by=revoked_by, side=side
            )

        await self._write(context, "provider_concession_revoked", content, concession, event, close)
        return Concession(
            concession_id=concession.concession_id,
            organization_id=concession.organization_id,
            provider_user_id=concession.provider_user_id,
            provider_organization_id=concession.provider_organization_id,
            scope_level=concession.scope_level,
            scope_id=concession.scope_id,
            reason=concession.reason,
            granted_at=concession.granted_at,
            expires_at=concession.expires_at,
            status=ConcessionStatus.REVOKED,
            revoked_at=revoked_at,
            revoked_by=revoked_by,
            revoked_by_side=side,
        )

    # --- expire_concessions --------------------------------------------------------------------

    async def expire_due(self, transaction: Transaction) -> int:
        """Una iteración de ``expire_concessions``: constancia de las vencidas (BR-NUC-40).

        Devuelve cuántas cerró. Una que otro proceso ya cerró, o que la base aún no da por
        vencida, se deja para la siguiente pasada; un rechazo del expediente queda en el registro
        de errores y no detiene a las demás.
        """
        context = transaction.context
        if context.organization_id == self._provider_organization_id:
            return 0
        now = self._clock.now()
        closed = 0
        for concession in await self._store.due_for_expiry(transaction, now):
            content = {
                "concession_id": str(concession.concession_id),
                "expires_at": format_timestamp(concession.expires_at),
                "recorded_at": format_timestamp(now),
            }
            event = NewEvent(
                event_name="concession_expired",
                payload={
                    "concession_id": str(concession.concession_id),
                    "expired_at": format_timestamp(concession.expires_at),
                },
            )

            async def expire(inner: Transaction, target: Concession = concession) -> None:
                await self._store.expire(inner, target, now)

            try:
                await self._write(
                    context, "provider_concession_expired", content, concession, event, expire
                )
            except ConcessionRejected as rejected:
                if rejected.code is ConcessionRejectionCode.LEDGER_REJECTED:
                    _log.error(
                        "el expediente rechazó provider_concession_expired",
                        code=None if rejected.rejection is None else rejected.rejection.code.value,
                    )
                continue
            closed += 1
        return closed

    # --- panel del cliente ---------------------------------------------------------------------

    async def list_concessions(
        self, context: ScopeContext, *, plant_id: uuid.UUID | None = None
    ) -> tuple[Concession, ...]:
        """Todas las concesiones de la organización (o las que alcanzan a ``plant_id``)."""
        if plant_id is not None and type(plant_id) is not uuid.UUID:
            raise ResourceNotFound()
        resource = (
            Resource.organization(context.organization_id)
            if plant_id is None
            else Resource.plant(context.organization_id, plant_id)
        )
        authorized = await self._authorizer.authorize(
            context, PermissionKey.CONCESSIONS_READ, resource
        )
        return await self._store.list_concessions(authorized, plant_id)

    async def list_own_concessions(self, context: ScopeContext) -> tuple[Concession, ...]:
        """Las concesiones que el actor de la proveedora se concedió (lado proveedor, §10.2).

        Solo desde la sesión de la proveedora sin concesión y con ``concessions.grant``; nunca
        el motivo (lo guarda el expediente del cliente). La más reciente primero.
        """
        context = self._provider_session(context)
        authorized = await self._authorizer.authorize(
            context,
            PermissionKey.CONCESSIONS_GRANT,
            Resource.organization(self._provider_organization_id),
        )
        return await self._store.provider_concessions(authorized, authorized.actor.id)

    async def list_provider_queries(
        self,
        context: ScopeContext,
        concession_id: uuid.UUID,
        *,
        after: ProviderQueryCursor | None = None,
        limit: int = MAX_PAGE_SIZE,
    ) -> ProviderQueryPage:
        """Una página de los ``provider_query`` de la concesión, en orden de llegada."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValueError(f"limit debe estar entre 1 y {MAX_PAGE_SIZE}")
        if after is not None and not isinstance(after, ProviderQueryCursor):
            raise TypeError("after debe ser ProviderQueryCursor")
        if type(concession_id) is not uuid.UUID or context.concession_id is not None:
            raise ResourceNotFound()
        concession = await self._store.concession(context, concession_id)
        if concession is None:
            raise ResourceNotFound()
        authorized = await self._authorizer.authorize(
            context, PermissionKey.CONCESSIONS_READ, concession.resource()
        )
        return await self._store.provider_queries(authorized, concession, after, limit)

    # --- comunes -------------------------------------------------------------------------------

    def _uuid7(self) -> uuid.UUID:
        return uuid7(self._clock, self._random_bytes)

    async def _write(
        self,
        context: ScopeContext,
        record_type: str,
        content: dict[str, Any],
        concession: Concession,
        event: NewEvent,
        projection: Callable[[Transaction], Awaitable[None]],
    ) -> None:
        result = await self._writer.write(
            context,
            record_type,
            content,
            scope=RecordScope(plant_id=concession.plant_id),
            events=(event,),
            projection=projection,
        )
        if isinstance(result, LedgerRejection):
            if result.code is LedgerRejectionCode.FREE_TEXT_REJECTED:
                raise ConcessionRejected(ConcessionRejectionCode.REASON_INVALID, result)
            raise ConcessionRejected(ConcessionRejectionCode.LEDGER_REJECTED, result)


def register_expire_concessions(
    registry: PeriodicTaskRegistry, service: ConcessionService
) -> PeriodicTask:
    """Registra ``expire_concessions`` de U-02 cada 5 minutos (``domain-entities.md`` §4.3)."""

    async def handler(transaction: Transaction) -> None:
        await service.expire_due(transaction)

    return registry.register(
        EXPIRE_CONCESSIONS, EXPIRE_CONCESSIONS_SCHEDULE, handler, unit=ActorUnit.U02
    )
