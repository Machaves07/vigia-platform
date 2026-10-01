"""Los cuatro constructores de ``ScopeContext`` (LC-NUC-04; BR-NUC-02 a 04, 18, 37, 38, 40).

Solo este módulo sella contextos (``_seal_scope_context``; BR-NUC-03). Los constructores:

- ``context_from_session(cookie)``: **una** sentencia sin caché (PAT-NUC-REN-03, adaptador
  ``identity.adapters.authz_store``) valida la sesión (estado, vencimientos, segundo factor,
  usuario y organización activos), prolonga ``idle_expires_at`` y trae las asignaciones vigentes
  del usuario, la versión del aviso aceptada y, si la petición selecciona una concesión
  (``X-Vigia-Concession``), esa concesión. El contexto lleva la organización del usuario, las
  asignaciones como ``allowed_scopes`` y el actor con la instantánea de su nombre.
  **Bajo concesión** el contexto es el del **cliente** (BR-NUC-04): ``organization_id`` del cliente,
  ``allowed_scopes = [(nivel, id, provider_installer)]`` con el alcance concedido, actor
  ``provider_user`` con ``concession_id``. La concesión tiene que ser del mismo usuario, estar
  ``active``, sin revocar y con ``now < expires_at`` (BR-NUC-40: no depende de la tarea
  ``expire_concessions``), y el usuario tiene que conservar en la proveedora una asignación con
  ``concessions.grant``. Nada se guarda entre peticiones: una revocación, un cierre, una
  desactivación o una suspensión surten efecto en la siguiente (BR-NUC-18, PR-NUC-50).
- ``context_from_event(event)``: la organización emisora del evento y su ``correlation_id``, con
  el actor del sistema del proceso de trabajo.
- ``context_for_organization(task, organization_id)``: una organización por iteración de una
  tarea periódica, con el actor del sistema.
- ``context_from_operator(operator_id)``: orden administrativa; el operador tiene que ser un
  usuario activo de la organización proveedora con ``platform_operator`` vigente (una sentencia).

Los contextos de evento e iteración no tienen asignaciones: ``authorize`` nunca les concede una
clave; operan sobre su organización por construcción.

``ScopeContexts`` también implementa ``LoginContexts`` y ``SessionContexts`` del inicio de sesión
(TASK-124): ``anonymous`` (la organización antes de conocer a la persona) y ``for_user`` (la
persona recién acreditada, aún sin asignaciones).

**Intentos sin contexto** (BR-NUC-02): ``ContextAbsentAuditor`` se instala como receptor de
``shared.context.report_context_absent``; por cada intento escribe ``context_absent_attempt``
(``outcome = denied``) en la cadena de auditoría de la organización **proveedora** y publica
``security_alert`` (``alert_kind = context_absent_attempt``). El aviso llega de código síncrono
que acaba de lanzar ``ContextAbsent``, así que la escritura va en una tarea del bucle; ``drain``
la espera (pruebas y cierre ordenado). Un fallo al registrar se deja en el registro de errores.

**``provider_query``** (BR-NUC-38): ``record_provider_query`` escribe, al terminar una petición
bajo concesión que tocó datos del cliente, un registro en la cadena del alcance concedido (planta
u organización) con operación, método, plantilla de ruta y momento. Fuera de concesión no hace
nada. La cadena de middleware (TASK-134) decide cuándo una petición toca datos del cliente.

Módulo crítico aislado (NFR-NUC-25): no importa FastAPI ni SQLAlchemy; no lee la hora del sistema.
"""

from __future__ import annotations

import asyncio
import enum
import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal, Protocol

from vigia_platform.identity.auth.sessions import SessionCookie
from vigia_platform.identity.authz.matrix import MATRIX, PermissionKey
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import (
    Actor,
    ActorKind,
    ActorUnit,
    AllowedScope,
    ContextAbsentReporter,
    ContextOrigin,
    Role,
    ScopeContext,
    ScopeLevel,
    _seal_scope_context,
    install_context_absent_reporter,
)
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "ConcessionRow",
    "ContextAbsentAuditor",
    "ContextStore",
    "ContextUnavailable",
    "ContextUnavailableReason",
    "OperatorRow",
    "OrganizationEvent",
    "OrganizationTask",
    "ProviderQueryLedger",
    "ScopeContexts",
    "SecurityAudit",
    "SessionRow",
    "SessionScope",
    "record_provider_query",
    "with_role_in_use",
]

_log = get_logger("identity.authz")

SYSTEM_DISPLAY_NAME: Final = "Sistema Vigía"
"""Instantánea del nombre del actor del sistema (eventos, iteraciones, inicio de sesión)."""
_OPERATION: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,127}")
_UNKNOWN_OPERATION: Final = "unknown"


class ContextUnavailableReason(enum.StrEnum):
    """Por qué no hay contexto (lista cerrada; nunca dice qué parte falló de la sesión)."""

    SESSION_INVALID = "session_invalid"
    """Sin sesión utilizable: inexistente, de otra organización, vencida, cerrada, sin segundo
    factor, o usuario u organización inactivos. Hacia el cliente, ``unauthenticated``."""
    CONCESSION_INVALID = "concession_invalid"
    """La concesión seleccionada no existe, no es del usuario, venció, fue revocada, su cliente
    está suspendido o el usuario ya no puede concederse concesiones. Hacia el cliente,
    ``not_found``: igual que una organización inexistente."""
    OPERATOR_INVALID = "operator_invalid"
    """El operador no es un usuario activo de la proveedora con ``platform_operator`` vigente."""


class ContextUnavailable(Exception):
    """No se construye ningún contexto; ``reason`` es de una lista cerrada."""

    def __init__(self, reason: ContextUnavailableReason) -> None:
        super().__init__(f"sin contexto: {reason.value}")
        self.reason = reason


# --- Puerto del almacén ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConcessionRow:
    """La concesión seleccionada tal como la devolvió la sentencia (ya comprobada vigente)."""

    concession_id: uuid.UUID
    organization_id: uuid.UUID
    """La organización **cliente**."""
    scope_level: ScopeLevel
    scope_id: uuid.UUID
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class SessionRow:
    """El resultado de la sentencia única de ``context_from_session``."""

    user_id: uuid.UUID
    organization_id: uuid.UUID
    organization_kind: Literal["client", "provider"]
    display_name: str = field(repr=False)
    privacy_notice_version_accepted: str | None
    assignments: tuple[AllowedScope, ...]
    concession: ConcessionRow | None


@dataclass(frozen=True, slots=True)
class OperatorRow:
    """El operador de una orden administrativa y sus asignaciones vigentes en la proveedora."""

    user_id: uuid.UUID
    display_name: str = field(repr=False)
    assignments: tuple[AllowedScope, ...]


class ContextStore(Protocol):
    """Lecturas de los constructores; cada método es **una** sentencia en una transacción."""

    async def session_row(
        self,
        lookup: ScopeContext,
        session_id_hash: str,
        now: datetime,
        concession_id: uuid.UUID | None,
    ) -> SessionRow | None:
        """La sesión utilizable en ``now`` (ya prolongada) con asignaciones y concesión."""
        ...

    async def operator_row(self, lookup: ScopeContext, user_id: uuid.UUID) -> OperatorRow | None:
        """El usuario activo de la proveedora con sus asignaciones vigentes, o ``None``."""
        ...


class OrganizationEvent(Protocol):
    """Lo que un constructor necesita de un evento de la bandeja (``OutboxEvent``)."""

    @property
    def organization_id(self) -> uuid.UUID: ...

    @property
    def correlation_id(self) -> uuid.UUID: ...


class OrganizationTask(Protocol):
    """Lo que un constructor necesita de una tarea periódica (``PeriodicTask``)."""

    @property
    def task_name(self) -> str: ...

    @property
    def unit(self) -> ActorUnit: ...


@dataclass(frozen=True, slots=True)
class SessionScope:
    """El contexto de una petición con sesión y lo que la cadena de middleware necesita además."""

    context: ScopeContext
    user_id: uuid.UUID
    """La persona de la sesión (bajo concesión, el usuario del proveedor)."""
    session_organization_id: uuid.UUID
    """La organización de la sesión (bajo concesión, la proveedora)."""
    privacy_notice_version_accepted: str | None


def with_role_in_use(context: ScopeContext, role: Role) -> ScopeContext:
    """El mismo contexto con ``actor.role_in_use = role`` (lo usa ``authorize``, BR-NUC-16).

    ``role`` tiene que ser el de alguna asignación del contexto.
    """
    if not isinstance(context, ScopeContext):
        raise TypeError("context debe ser ScopeContext")
    role = Role(role)
    if role not in {scope.role for scope in context.allowed_scopes}:
        raise ValueError("role_in_use debe ser el rol de una asignación del contexto")
    actor = context.actor
    return _seal_scope_context(
        organization_id=context.organization_id,
        actor=Actor(
            kind=actor.kind,
            id=actor.id,
            display_name_snapshot=actor.display_name_snapshot,
            unit=actor.unit,
            role_in_use=role,
            concession_id=actor.concession_id,
        ),
        origin=context.origin,
        allowed_scopes=context.allowed_scopes,
        correlation_id=context.correlation_id,
        session_id_hash=context.session_id_hash,
    )


def _display_name(value: str) -> str:
    """La instantánea del nombre, recortada al máximo de ``Actor`` (§3.2)."""
    return value[:120] if value else SYSTEM_DISPLAY_NAME


class ScopeContexts:
    """``AuthorizationPort.build_context`` (sesión, evento, organización, operador)."""

    def __init__(
        self,
        *,
        store: ContextStore,
        clock: Clock,
        provider_organization_id: uuid.UUID,
        system_actor_id: uuid.UUID,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        for name, value in (
            ("provider_organization_id", provider_organization_id),
            ("system_actor_id", system_actor_id),
        ):
            if type(value) is not uuid.UUID:
                raise TypeError(f"{name} debe ser uuid.UUID")
        self._store = store
        self._clock = clock
        self._provider_organization_id = provider_organization_id
        self._system_actor_id = system_actor_id
        self._random_bytes = random_bytes

    def __repr__(self) -> str:
        return "ScopeContexts()"

    @property
    def provider_organization_id(self) -> uuid.UUID:
        return self._provider_organization_id

    def _correlation(self, correlation_id: uuid.UUID | None) -> uuid.UUID:
        if correlation_id is None:
            return uuid7(self._clock, self._random_bytes)
        if type(correlation_id) is not uuid.UUID or correlation_id.version != 7:
            raise ValueError("correlation_id debe ser un UUID v7")
        return correlation_id

    def _system(
        self,
        organization_id: uuid.UUID,
        origin: ContextOrigin,
        unit: ActorUnit,
        correlation_id: uuid.UUID | None = None,
    ) -> ScopeContext:
        if type(organization_id) is not uuid.UUID:
            raise TypeError("organization_id debe ser uuid.UUID")
        return _seal_scope_context(
            organization_id=organization_id,
            actor=Actor(
                kind=ActorKind.SYSTEM,
                id=self._system_actor_id,
                display_name_snapshot=SYSTEM_DISPLAY_NAME,
                unit=ActorUnit(unit),
            ),
            origin=origin,
            allowed_scopes=(),
            correlation_id=self._correlation(correlation_id),
        )

    # --- Sesión --------------------------------------------------------------------------------

    async def context_from_session(
        self,
        cookie: SessionCookie | None,
        *,
        concession_id: uuid.UUID | None = None,
        correlation_id: uuid.UUID | None = None,
        unit: ActorUnit = ActorUnit.U02,
    ) -> SessionScope:
        """El contexto de la petición con ``cookie``; ``ContextUnavailable`` si no hay."""
        if not isinstance(cookie, SessionCookie):
            raise ContextUnavailable(ContextUnavailableReason.SESSION_INVALID)
        if concession_id is not None and type(concession_id) is not uuid.UUID:
            raise ContextUnavailable(ContextUnavailableReason.CONCESSION_INVALID)
        correlation = self._correlation(correlation_id)
        now = self._clock.now()
        lookup = self._system(
            cookie.organization_id, ContextOrigin.OUTBOX_EVENT, ActorUnit.U02, correlation
        )
        session_id_hash = cookie.session_id_hash
        row = await self._store.session_row(lookup, session_id_hash, now, concession_id)
        if row is None or row.organization_id != cookie.organization_id:
            raise ContextUnavailable(ContextUnavailableReason.SESSION_INVALID)
        if concession_id is None:
            context = _seal_scope_context(
                organization_id=row.organization_id,
                actor=Actor(
                    kind=ActorKind.USER,
                    id=row.user_id,
                    display_name_snapshot=_display_name(row.display_name),
                    unit=unit,
                ),
                origin=ContextOrigin.SESSION,
                allowed_scopes=row.assignments,
                correlation_id=correlation,
                session_id_hash=session_id_hash,
            )
        else:
            context = self._concession_context(
                row, concession_id, now, correlation, session_id_hash, unit
            )
        return SessionScope(
            context=context,
            user_id=row.user_id,
            session_organization_id=row.organization_id,
            privacy_notice_version_accepted=row.privacy_notice_version_accepted,
        )

    def _concession_context(
        self,
        row: SessionRow,
        concession_id: uuid.UUID,
        now: datetime,
        correlation: uuid.UUID,
        session_id_hash: str,
        unit: ActorUnit,
    ) -> ScopeContext:
        concession = row.concession
        can_grant = any(
            PermissionKey.CONCESSIONS_GRANT in MATRIX[scope.role]
            and scope.covers(self._provider_organization_id)
            for scope in row.assignments
        )
        if (
            row.organization_kind != "provider"
            or row.organization_id != self._provider_organization_id
            or concession is None
            or concession.concession_id != concession_id
            or concession.organization_id == self._provider_organization_id
            or concession.scope_level is ScopeLevel.ZONE
            or not now < concession.expires_at
            or not can_grant
        ):
            raise ContextUnavailable(ContextUnavailableReason.CONCESSION_INVALID)
        return _seal_scope_context(
            organization_id=concession.organization_id,
            actor=Actor(
                kind=ActorKind.PROVIDER_USER,
                id=row.user_id,
                display_name_snapshot=_display_name(row.display_name),
                unit=unit,
                concession_id=concession.concession_id,
            ),
            origin=ContextOrigin.SESSION,
            allowed_scopes=(
                AllowedScope(concession.scope_level, concession.scope_id, Role.PROVIDER_INSTALLER),
            ),
            correlation_id=correlation,
            session_id_hash=session_id_hash,
        )

    # --- Evento, iteración periódica y orden administrativa ------------------------------------

    def context_from_event(
        self, event: OrganizationEvent, *, unit: ActorUnit = ActorUnit.U02
    ) -> ScopeContext:
        """La organización emisora del evento, con su ``correlation_id`` y el actor del sistema."""
        return self._system(
            event.organization_id, ContextOrigin.OUTBOX_EVENT, unit, event.correlation_id
        )

    def context_for_organization(
        self, task: OrganizationTask, organization_id: uuid.UUID
    ) -> ScopeContext:
        """Una organización por iteración de ``task`` (BR-NUC-03), con el actor del sistema."""
        if not isinstance(task.task_name, str) or not task.task_name:
            raise ValueError("la tarea periódica necesita nombre")
        return self._system(organization_id, ContextOrigin.PERIODIC_ITERATION, task.unit)

    async def context_from_operator(
        self, operator_id: uuid.UUID, *, correlation_id: uuid.UUID | None = None
    ) -> ScopeContext:
        """Orden administrativa del operador ``operator_id`` (``platform_operator`` vigente)."""
        if type(operator_id) is not uuid.UUID:
            raise ContextUnavailable(ContextUnavailableReason.OPERATOR_INVALID)
        correlation = self._correlation(correlation_id)
        lookup = self._system(
            self._provider_organization_id, ContextOrigin.ADMIN_COMMAND, ActorUnit.U02, correlation
        )
        row = await self._store.operator_row(lookup, operator_id)
        scopes = (
            ()
            if row is None
            else tuple(
                scope
                for scope in row.assignments
                if scope.role is Role.PLATFORM_OPERATOR
                and scope.covers(self._provider_organization_id)
            )
        )
        if row is None or row.user_id != operator_id or not scopes:
            raise ContextUnavailable(ContextUnavailableReason.OPERATOR_INVALID)
        return _seal_scope_context(
            organization_id=self._provider_organization_id,
            actor=Actor(
                kind=ActorKind.OPERATOR,
                id=row.user_id,
                display_name_snapshot=_display_name(row.display_name),
                unit=ActorUnit.U02,
            ),
            origin=ContextOrigin.ADMIN_COMMAND,
            allowed_scopes=scopes,
            correlation_id=correlation,
        )

    # --- Inicio de sesión (LoginContexts, SessionContexts) -------------------------------------

    def anonymous(self, organization_id: uuid.UUID) -> ScopeContext:
        """La organización antes de conocer a la persona: actor del sistema, sin asignaciones."""
        return self._system(organization_id, ContextOrigin.OUTBOX_EVENT, ActorUnit.U02)

    def for_user(
        self,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        display_name: str,
        session_id_hash: str,
    ) -> ScopeContext:
        """La persona recién acreditada con la sesión ``session_id_hash`` (sin asignaciones)."""
        return _seal_scope_context(
            organization_id=organization_id,
            actor=Actor(
                kind=ActorKind.USER,
                id=user_id,
                display_name_snapshot=_display_name(display_name),
                unit=ActorUnit.U02,
            ),
            origin=ContextOrigin.SESSION,
            allowed_scopes=(),
            correlation_id=self._correlation(None),
            session_id_hash=session_id_hash,
        )

    def provider_audit_context(self) -> ScopeContext:
        """Contexto del sistema en la proveedora, para lo que no tiene organización (BR-NUC-61)."""
        return self._system(
            self._provider_organization_id, ContextOrigin.OUTBOX_EVENT, ActorUnit.U02
        )


# --- Intentos sin contexto -----------------------------------------------------------------------


class SecurityAudit(Protocol):
    """Puerto de ``ContextAbsentAuditor`` (adaptador en ``identity.adapters.authz_store``)."""

    async def context_absent_attempt(self, provider_context: ScopeContext, operation: str) -> None:
        """``context_absent_attempt`` en la cadena de la proveedora y ``security_alert``."""
        ...


class ContextAbsentAuditor:
    """Receptor de ``report_context_absent``: audita cada intento sin contexto (BR-NUC-02)."""

    def __init__(self, *, contexts: ScopeContexts, audit: SecurityAudit) -> None:
        self._contexts = contexts
        self._audit = audit
        self._pending: set[asyncio.Task[None]] = set()
        self._previous: ContextAbsentReporter | None = None
        self._installed = False

    def install(self) -> None:
        """Se instala como receptor del proceso (una vez, al arrancar)."""
        if not self._installed:
            self._previous = install_context_absent_reporter(self.report)
            self._installed = True

    def uninstall(self) -> None:
        if self._installed:
            install_context_absent_reporter(self._previous)
            self._installed = False

    def report(self, operation: str) -> None:
        """Programa la auditoría del intento; sin bucle en marcha, queda en el registro."""
        name = (
            operation
            if isinstance(operation, str) and _OPERATION.fullmatch(operation)
            else _UNKNOWN_OPERATION
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _log.error("intento sin contexto fuera de un bucle: no se pudo auditar")
            return
        task = loop.create_task(self._record(name))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _record(self, operation: str) -> None:
        try:
            await self._audit.context_absent_attempt(
                self._contexts.provider_audit_context(), operation
            )
        except Exception:
            _log.exception("no se pudo auditar el intento sin contexto")

    async def drain(self) -> None:
        """Espera a que terminen las auditorías programadas."""
        while self._pending:
            await asyncio.gather(*tuple(self._pending), return_exceptions=True)


# --- provider_query (BR-NUC-38) ------------------------------------------------------------------


class ProviderQueryLedger(Protocol):
    """Escritura del registro ``provider_query`` (adaptador sobre ``EscritorExpediente``)."""

    async def write_provider_query(
        self, context: ScopeContext, content: Mapping[str, str], plant_id: uuid.UUID | None
    ) -> None:
        """Escribe el registro en la cadena de ``plant_id`` o, sin planta, en la organización."""
        ...


_METHODS: Final = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_ROUTE: Final = re.compile(r"/[a-z0-9_{}/.-]{0,255}")


async def record_provider_query(
    context: ScopeContext,
    ledger: ProviderQueryLedger,
    *,
    operation: Literal["read", "write"],
    method: str,
    route_template: str,
    occurred_at: datetime,
) -> bool:
    """Un ``provider_query`` por petición bajo concesión; ``False`` (nada) fuera de concesión."""
    if not isinstance(context, ScopeContext):
        raise TypeError("context debe ser ScopeContext")
    if context.concession_id is None:
        return False
    if operation not in ("read", "write") or method not in _METHODS:
        raise ValueError("operación o método fuera de la lista")
    if not isinstance(route_template, str) or _ROUTE.fullmatch(route_template) is None:
        raise ValueError("route_template debe ser la plantilla de la ruta")
    if not isinstance(occurred_at, datetime) or occurred_at.utcoffset() is None:
        raise ValueError("occurred_at debe llevar zona horaria")
    (scope,) = context.allowed_scopes
    plant_id = scope.scope_id if scope.scope_level is ScopeLevel.PLANT else None
    content = {
        "concession_id": str(context.concession_id),
        "operation": operation,
        "method": method,
        "resource": route_template,
        "occurred_at": format_timestamp(occurred_at),
    }
    await ledger.write_provider_query(context, content, plant_id)
    return True
