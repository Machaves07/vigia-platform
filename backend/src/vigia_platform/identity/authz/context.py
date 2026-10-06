"""Los cinco constructores de ``ScopeContext`` (LC-NUC-04; BR-NUC-02 a 04, 18, 37, 38, 40; A-51).

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
  ``operator_in_organization(operator_context, organization_id, …)`` es la misma orden actuando
  sobre la organización cliente que el operador da de alta (génesis, TASK-126): mismo actor y
  correlación, sin asignaciones en ese cliente.
- ``context_from_node(store, presented)`` (quinto constructor, A-51; **solo** ``node_api``, que
  lo llama con el certificado que verificó el balanceador): **una** sentencia sin caché
  (``NodeContextStore.node_row``, bajo la organización del certificado) trae la identidad del
  nodo, la credencial del número de serie, la marca de flota y las asignaciones de zona del nodo.
  Exige identidad ``enrolled`` con ``enrolled_at``, sin revocación ni baja; credencial ``active``
  dentro de su vigencia u ``overlapping`` dentro de las 24 h desde el ``issued_at`` de su sucesora
  (``OVERLAP``, NFR-GOB-34); organización y planta del certificado iguales a las de la fila. Si
  no, ``NodeContextRejected`` con ``not_enrolled``, ``revoked`` o ``zone_mismatch``. El contexto
  lleva el actor ``node`` (su ``node_id``), el origen ``node_request`` y la organización del
  certificado; su **alcance** son solo las zonas asignadas al nodo **ahora**
  (``NodeScope.zone_ids``; el filtro por instante vive aquí). ``allowed_scopes`` queda vacío: un
  nodo no tiene rol en la matriz y ``authorize`` nunca le concede una clave.
- ``context_from_node_enrollment(store, node_id)``: el alta, sin certificado. La organización del
  nodo declarado se busca por ``node_id`` (nombre común de la CSR, nota U03-H-13) con la función
  de ``gob_0019``; mismo actor y origen, sin zonas. ``None`` si no hay nodo declarado.

``with_unit(context, unit)`` es el mismo contexto escrito por otra unidad: los puertos de U-02
que U-03 y U-04 llaman en proceso (``IdentityCommandPort``) escriben sus propios tipos de
registro (``node_declared``…) con la unidad U-02, sin cambiar actor, alcance ni correlación.
``with_scopes(context, scopes, role)`` es el mismo contexto **reducido** a las asignaciones que
conceden una clave (``identity.authz.authorize.narrowed``); nunca amplía.

Los contextos de evento e iteración no tienen asignaciones: ``authorize`` nunca les concede una
clave; operan sobre su organización por construcción.

``provider_concession_context(base, …)`` deriva del contexto de sesión de un usuario de la
proveedora el de **una** concesión sobre el cliente, para concederla o revocarla desde la
proveedora (TASK-127, ``business-logic-model.md`` §3); el servicio de concesiones lo pide después
de autorizar.

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
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
    repository,
)
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "OVERLAP",
    "ConcessionRow",
    "ContextAbsentAuditor",
    "ContextStore",
    "ContextUnavailable",
    "ContextUnavailableReason",
    "EnrollmentRow",
    "EnrollmentScope",
    "NodeAssignment",
    "NodeContextReason",
    "NodeContextRejected",
    "NodeContextStore",
    "NodeRow",
    "NodeScope",
    "OperatorRow",
    "OrganizationEvent",
    "OrganizationTask",
    "PresentedNode",
    "ProviderQueryLedger",
    "ScopeContexts",
    "SecurityAudit",
    "SessionRow",
    "SessionScope",
    "operator_in_organization",
    "record_provider_query",
    "with_role_in_use",
    "with_scopes",
    "with_unit",
]

_log = get_logger("identity.authz")

SYSTEM_DISPLAY_NAME: Final = "Sistema Vigía"
"""Instantánea del nombre del actor del sistema (eventos, iteraciones, inicio de sesión)."""
_OPERATION: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,127}")
_UNKNOWN_OPERATION: Final = "unknown"
OVERLAP: Final = timedelta(hours=24)
"""Una credencial ``overlapping`` autentica 24 h desde el ``issued_at`` de su sucesora (BLM §3.5,
NFR-GOB-34, nota de TASK-219): después deja de autenticar aunque su estado no haya cambiado."""
_NODE_DISPLAY_PREFIX: Final = "Nodo "
_SERIAL: Final = re.compile(r"[0-9a-f]{1,64}")


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


# --- Nodo (A-51) ---------------------------------------------------------------------------------


class NodeContextReason(enum.StrEnum):
    """Por qué una petición de nodo no tiene contexto (cada uno es un ``rejection_code``)."""

    NOT_ENROLLED = "node_not_enrolled"
    """Sin identidad ni credencial de ese número de serie en la organización del certificado,
    identidad aún sin alta (``declared``, ``re_enrollment_pending``) o credencial vencida o fuera
    de su vigencia: el nodo necesita un alta (NFR-GOB-34)."""
    REVOKED = "node_revoked"
    """Identidad revocada o dada de baja, o credencial revocada, sustituida o fuera de su
    solapamiento de 24 h (BR-GOB-66, BLM §3.5)."""
    ZONE_MISMATCH = "node_zone_mismatch"
    """Planta del certificado distinta de la del nodo, o zona fuera de las asignadas al nodo
    (BR-GOB-88)."""


class NodeContextRejected(Exception):
    """La petición del nodo no tiene contexto; ``reason`` es su ``rejection_code``."""

    def __init__(self, reason: NodeContextReason) -> None:
        super().__init__(f"sin contexto de nodo: {reason.value}")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class PresentedNode:
    """Lo que el certificado de cliente que verificó el balanceador dice del nodo.

    Lo construye ``node_api.identity`` con el perfil del sujeto de ``certificate_profile``
    (nombre común = ``node_id``, organización y planta) y el número de serie en hexadecimal.
    """

    node_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    certificate_serial: str

    def __post_init__(self) -> None:
        for name in ("node_id", "organization_id", "plant_id"):
            if type(getattr(self, name)) is not uuid.UUID:
                raise TypeError(f"{name} debe ser uuid.UUID")
        if (
            not isinstance(self.certificate_serial, str)
            or _SERIAL.fullmatch(self.certificate_serial) is None
        ):
            raise ValueError("certificate_serial debe ser hexadecimal en minúsculas")


@dataclass(frozen=True, slots=True)
class NodeAssignment:
    """Una asignación (vigente o pasada) de una zona al nodo (``identity.zone_node_assignment``)."""

    zone_id: uuid.UUID
    assigned_at: datetime
    unassigned_at: datetime | None

    def in_force(self, now: datetime) -> bool:
        """¿Está asignada la zona en ``now``? ``[assigned_at, unassigned_at)``."""
        return self.assigned_at <= now and (self.unassigned_at is None or now < self.unassigned_at)


@dataclass(frozen=True, slots=True)
class NodeRow:
    """El resultado de la sentencia única de ``context_from_node`` (una fila o ninguna)."""

    node_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    code: str
    node_status: str
    """``identity.node_identity.status``: ``declared``, ``enrolled``, ``revoked`` o
    ``re_enrollment_pending``."""
    enrolled_at: datetime | None
    revoked_at: datetime | None
    decommissioned_at: datetime | None
    credential_organization_id: uuid.UUID
    credential_plant_id: uuid.UUID
    credential_status: str
    """``active``, ``overlapping``, ``revoked`` o ``superseded``."""
    issued_at: datetime
    expires_at: datetime
    successor_issued_at: datetime | None
    """``issued_at`` de la credencial que rotó desde esta (su sucesora), si existe."""
    assignments: tuple[NodeAssignment, ...]


@dataclass(frozen=True, slots=True)
class EnrollmentRow:
    """El nodo declarado de un ``node_id``, buscado sin conocer su organización (gob_0019)."""

    node_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    code: str
    node_status: str


class NodeContextStore(Protocol):
    """Lecturas de ``context_from_node`` (``node_api.identity``): **una** sentencia cada una."""

    async def node_row(
        self, lookup: ScopeContext, node_id: uuid.UUID, certificate_serial: str
    ) -> NodeRow | None:
        """El nodo y la credencial ``(node_id, certificate_serial)`` vistos desde ``lookup``."""
        ...

    async def enrollment_row(
        self, lookup: ScopeContext, node_id: uuid.UUID
    ) -> EnrollmentRow | None:
        """El nodo declarado ``node_id`` en cualquier organización (solo actor ``system``)."""
        ...


@dataclass(frozen=True, slots=True)
class NodeScope:
    """El contexto de una petición de nodo y su alcance: solo las zonas asignadas ahora."""

    context: ScopeContext
    node_id: uuid.UUID
    plant_id: uuid.UUID
    zone_ids: frozenset[uuid.UUID]
    certificate_serial: str
    credential_status: str

    @property
    def organization_id(self) -> uuid.UUID:
        return self.context.organization_id

    def covers_zone(self, zone_id: uuid.UUID) -> bool:
        """¿Está ``zone_id`` entre las zonas asignadas al nodo en el instante de la petición?"""
        return zone_id in self.zone_ids


@dataclass(frozen=True, slots=True)
class EnrollmentScope:
    """El contexto del alta: la organización del nodo declarado, sin zonas (A-51)."""

    context: ScopeContext
    node_id: uuid.UUID
    plant_id: uuid.UUID
    node_status: str


def _node_display_name(code: str) -> str:
    return (_NODE_DISPLAY_PREFIX + code)[:120] if code else SYSTEM_DISPLAY_NAME


_RETIRED_CREDENTIAL: Final = frozenset({"revoked", "superseded"})
"""Credenciales retiradas: ``node_revoked`` sea cual sea el estado del nodo. ``superseded``
es la materialización del ``overlapping → revoked`` de BLM §3.5 (nota de §4) y va en la lista
de revocación como ``revoked``: con el nodo ``re_enrollment_pending`` o ``declared`` responde
igual que una revocada, no ``node_not_enrolled`` (PR-GOB-28)."""


def _credential_reason(row: NodeRow, now: datetime) -> NodeContextReason | None:
    """Por qué la credencial no autentica en ``now`` (``None``: autentica)."""
    if row.credential_status == "active":
        if row.issued_at <= now < row.expires_at:
            return None
        return NodeContextReason.NOT_ENROLLED
    if row.credential_status == "overlapping":
        successor = row.successor_issued_at
        if (
            successor is not None
            and row.issued_at <= now < row.expires_at
            and now < successor + OVERLAP
        ):
            return None
        if not now < row.expires_at:
            return NodeContextReason.NOT_ENROLLED
        return NodeContextReason.REVOKED
    return NodeContextReason.REVOKED


def _node_reason(
    row: NodeRow | None, presented: PresentedNode, now: datetime
) -> NodeContextReason | None:
    """La regla de ``context_from_node`` sobre la fila (``None``: hay contexto)."""
    if (
        row is None
        or row.node_id != presented.node_id
        or row.organization_id != presented.organization_id
        or row.credential_organization_id != presented.organization_id
    ):
        return NodeContextReason.NOT_ENROLLED
    if (
        row.node_status == "revoked"
        or row.revoked_at is not None
        or row.decommissioned_at is not None
        or row.credential_status in _RETIRED_CREDENTIAL
    ):
        return NodeContextReason.REVOKED
    if row.node_status != "enrolled" or row.enrolled_at is None:
        return NodeContextReason.NOT_ENROLLED
    if row.plant_id != presented.plant_id or row.credential_plant_id != presented.plant_id:
        return NodeContextReason.ZONE_MISMATCH
    return _credential_reason(row, now)


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


def with_unit(context: ScopeContext, unit: ActorUnit) -> ScopeContext:
    """El mismo contexto con ``actor.unit = unit`` (un puerto de U-02 que escribe sus tipos)."""
    if not isinstance(context, ScopeContext):
        raise TypeError("context debe ser ScopeContext")
    actor = context.actor
    return _seal_scope_context(
        organization_id=context.organization_id,
        actor=Actor(
            kind=actor.kind,
            id=actor.id,
            display_name_snapshot=actor.display_name_snapshot,
            unit=ActorUnit(unit),
            role_in_use=actor.role_in_use,
            concession_id=actor.concession_id,
        ),
        origin=context.origin,
        allowed_scopes=context.allowed_scopes,
        correlation_id=context.correlation_id,
        session_id_hash=context.session_id_hash,
    )


def with_scopes(context: ScopeContext, scopes: Iterable[AllowedScope], role: Role) -> ScopeContext:
    """El mismo contexto reducido a ``scopes`` y con ``actor.role_in_use = role`` (TASK-137).

    Lo usan las rutas que leen con un puerto que filtra por ``allowed_scopes`` sin mirar el rol
    (``LectorExpediente``): el contexto reducido solo conserva las asignaciones que conceden la
    clave de la ruta, así que una asignación de otro rol nunca amplía lo que se ve. Solo reduce:
    cada alcance tiene que ser una asignación del contexto, no puede quedar vacío y ``role`` tiene
    que ser el de una de ellas.
    """
    if not isinstance(context, ScopeContext):
        raise TypeError("context debe ser ScopeContext")
    kept = tuple(dict.fromkeys(scopes))
    if not kept:
        raise ValueError("el contexto reducido necesita al menos una asignación")
    if any(scope not in context.allowed_scopes for scope in kept):
        raise ValueError("solo se reduce: cada alcance tiene que ser una asignación del contexto")
    role = Role(role)
    if role not in {scope.role for scope in kept}:
        raise ValueError("role_in_use debe ser el rol de una asignación conservada")
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
        allowed_scopes=kept,
        correlation_id=context.correlation_id,
        session_id_hash=context.session_id_hash,
    )


def operator_in_organization(
    operator_context: ScopeContext,
    organization_id: uuid.UUID,
    *,
    provider_organization_id: uuid.UUID,
) -> ScopeContext:
    """La orden administrativa de ``operator_context`` actuando sobre ``organization_id``.

    Solo para la génesis de una organización cliente (BR-NUC-06): mismo actor ``operator``,
    mismo origen y correlación, **sin asignaciones** en el cliente (``authorize`` no le concede
    nada allí). ``operator_context`` tiene que salir de ``context_from_operator``: orden
    administrativa de un operador en la proveedora.
    """
    if not isinstance(operator_context, ScopeContext):
        raise TypeError("operator_context debe ser ScopeContext")
    for name, value in (
        ("organization_id", organization_id),
        ("provider_organization_id", provider_organization_id),
    ):
        if type(value) is not uuid.UUID:
            raise TypeError(f"{name} debe ser uuid.UUID")
    actor = operator_context.actor
    if (
        operator_context.origin is not ContextOrigin.ADMIN_COMMAND
        or actor.kind is not ActorKind.OPERATOR
        or operator_context.organization_id != provider_organization_id
        or organization_id == provider_organization_id
    ):
        raise ContextUnavailable(ContextUnavailableReason.OPERATOR_INVALID)
    return _seal_scope_context(
        organization_id=organization_id,
        actor=actor,
        origin=ContextOrigin.ADMIN_COMMAND,
        allowed_scopes=(),
        correlation_id=operator_context.correlation_id,
    )


def _display_name(value: str) -> str:
    """La instantánea del nombre, recortada al máximo de ``Actor`` (§3.2)."""
    return value[:120] if value else SYSTEM_DISPLAY_NAME


@repository
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

    # --- Petición de un nodo (quinto constructor, A-51; solo node_api) ------------------------

    async def context_from_node(
        self,
        store: NodeContextStore,
        presented: PresentedNode,
        *,
        correlation_id: uuid.UUID | None = None,
    ) -> NodeScope:
        """El contexto de la petición del nodo ``presented``; ``NodeContextRejected`` si no hay.

        Una sentencia por petición y nada guardado entre peticiones: una revocación, una
        rotación, una baja o una reasignación surten efecto en la siguiente (PR-GOB-28).
        """
        if not isinstance(presented, PresentedNode):
            raise TypeError("presented debe ser PresentedNode")
        correlation = self._correlation(correlation_id)
        now = self._clock.now()
        lookup = self._system(
            presented.organization_id, ContextOrigin.OUTBOX_EVENT, ActorUnit.U03, correlation
        )
        row = await store.node_row(lookup, presented.node_id, presented.certificate_serial)
        reason = _node_reason(row, presented, now)
        if reason is not None or row is None:
            raise NodeContextRejected(reason or NodeContextReason.NOT_ENROLLED)
        zones = frozenset(
            assignment.zone_id for assignment in row.assignments if assignment.in_force(now)
        )
        context = _seal_scope_context(
            organization_id=row.organization_id,
            actor=Actor(
                kind=ActorKind.NODE,
                id=row.node_id,
                display_name_snapshot=_node_display_name(row.code),
                unit=ActorUnit.U03,
            ),
            origin=ContextOrigin.NODE_REQUEST,
            allowed_scopes=(),
            correlation_id=correlation,
        )
        return NodeScope(
            context=context,
            node_id=row.node_id,
            plant_id=row.plant_id,
            zone_ids=zones,
            certificate_serial=presented.certificate_serial,
            credential_status=row.credential_status,
        )

    async def context_from_node_enrollment(
        self,
        store: NodeContextStore,
        node_id: uuid.UUID,
        *,
        correlation_id: uuid.UUID | None = None,
    ) -> EnrollmentScope | None:
        """El contexto del alta del nodo declarado ``node_id`` (nombre común de la CSR).

        La búsqueda cruza organizaciones (el alta no lleva certificado): la hace la función de
        ``gob_0019`` con el actor del sistema en la proveedora. ``None`` si no existe el nodo.
        """
        if type(node_id) is not uuid.UUID:
            raise TypeError("node_id debe ser uuid.UUID")
        correlation = self._correlation(correlation_id)
        lookup = self._system(
            self._provider_organization_id, ContextOrigin.OUTBOX_EVENT, ActorUnit.U03, correlation
        )
        row = await store.enrollment_row(lookup, node_id)
        if row is None or row.node_id != node_id:
            return None
        context = _seal_scope_context(
            organization_id=row.organization_id,
            actor=Actor(
                kind=ActorKind.NODE,
                id=row.node_id,
                display_name_snapshot=_node_display_name(row.code),
                unit=ActorUnit.U03,
            ),
            origin=ContextOrigin.NODE_REQUEST,
            allowed_scopes=(),
            correlation_id=correlation,
        )
        return EnrollmentScope(
            context=context, node_id=row.node_id, plant_id=row.plant_id, node_status=row.node_status
        )

    def bootstrap_operator_context(self, operator_id: uuid.UUID, display_name: str) -> ScopeContext:
        """``vigia-admin bootstrap``: el primer operador, que nace en esta orden (BR-NUC-06).

        Actor ``operator`` en la organización proveedora con origen de orden administrativa,
        **sin asignaciones**: ``authorize`` no le concede nada. Solo sirve para lo que la orden
        escribe directamente (la proveedora, el operador invitado y sus claves de firma), que la
        base exige atribuir a una cuenta (``created_by``, ``invited_by``, ``rotated_by``). Las
        órdenes siguientes usan ``context_from_operator``, que exige el operador ya activo.
        """
        if type(operator_id) is not uuid.UUID:
            raise ContextUnavailable(ContextUnavailableReason.OPERATOR_INVALID)
        if not isinstance(display_name, str):
            raise TypeError("display_name debe ser str")
        return _seal_scope_context(
            organization_id=self._provider_organization_id,
            actor=Actor(
                kind=ActorKind.OPERATOR,
                id=operator_id,
                display_name_snapshot=_display_name(display_name),
                unit=ActorUnit.U02,
            ),
            origin=ContextOrigin.ADMIN_COMMAND,
            allowed_scopes=(),
            correlation_id=self._correlation(None),
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

    # --- Contexto derivado de la concesión (TASK-127) ------------------------------------------

    def provider_concession_context(
        self,
        base: ScopeContext,
        *,
        concession_id: uuid.UUID,
        client_organization_id: uuid.UUID,
        scope_level: ScopeLevel,
        scope_id: uuid.UUID,
    ) -> ScopeContext:
        """El contexto del cliente con el que el proveedor concede o revoca **esa** concesión.

        ``business-logic-model.md`` §3: conceder es la única escritura de un contexto del proveedor
        en un cliente sin concesión previa, y solo de ese tipo. ``base`` es el contexto de la sesión
        del usuario en la proveedora (sin concesión) ya autorizado por el servicio de concesiones;
        el resultado es el de la concesión ``concession_id`` sobre el cliente, como el que
        construye ``context_from_session`` (mismo actor, sesión y correlación). En la base, con él
        solo se puede insertar o revocar la fila de esa concesión (``nuc_0009``). Sin ``base``,
        ``ContextAbsent`` (la guarda de ``@repository``).
        """
        for name, value in (
            ("concession_id", concession_id),
            ("client_organization_id", client_organization_id),
            ("scope_id", scope_id),
        ):
            if type(value) is not uuid.UUID:
                raise TypeError(f"{name} debe ser uuid.UUID")
        level = ScopeLevel(scope_level)
        if (
            base.origin is not ContextOrigin.SESSION
            or base.organization_id != self._provider_organization_id
            or base.concession_id is not None
            or base.actor.kind is not ActorKind.USER
            or base.session_id_hash is None
            or client_organization_id == self._provider_organization_id
            or level is ScopeLevel.ZONE
            or (level is ScopeLevel.ORGANIZATION and scope_id != client_organization_id)
        ):
            raise ContextUnavailable(ContextUnavailableReason.CONCESSION_INVALID)
        actor = base.actor
        return _seal_scope_context(
            organization_id=client_organization_id,
            actor=Actor(
                kind=ActorKind.PROVIDER_USER,
                id=actor.id,
                display_name_snapshot=actor.display_name_snapshot,
                unit=actor.unit,
                role_in_use=actor.role_in_use,
                concession_id=concession_id,
            ),
            origin=ContextOrigin.SESSION,
            allowed_scopes=(AllowedScope(level, scope_id, Role.PROVIDER_INSTALLER),),
            correlation_id=base.correlation_id,
            session_id_hash=base.session_id_hash,
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
