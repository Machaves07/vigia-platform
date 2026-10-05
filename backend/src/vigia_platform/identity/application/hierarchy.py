"""Jerarquía: organizaciones, plantas, zonas, nodos y asignaciones nodo-zona (LC-NUC-05).

``business-logic-model.md`` §2 y §10.1; BR-NUC-05 a 08; domain-entities §2.1 a §2.3.

**Servicios de la interfaz** (rutas de jerarquía de TASK-136):

- ``create_plant``: ``hierarchy.manage`` sobre la organización; planta con ``country`` (ISO 3166-1
  alfa-2), ``data_region`` (de la lista ``DataRegion``, inmutable, BR-NUC-07) y ``timezone``
  (IANA). En la misma transacción escribe ``plant_created``, la génesis de la cadena de la planta
  (secuencia 1), y audita ``plant_created``.
- ``create_zone``: ``hierarchy.manage`` sobre la planta; ``zone_created`` en la cadena de la planta
  y auditoría ``zone_created``.

**``IdentityQueryPort``** (U-03, U-04): ``hierarchy``, ``users_by_role_and_scope`` (destinatarios
de notificaciones), ``node_identity`` y ``assigned_node``. Con un contexto de sesión devuelven
solo lo que cubren sus asignaciones (una zona se ve con el nombre de su planta); con un contexto
de evento, de iteración o de orden administrativa, toda la organización.

**``IdentityCommandPort``** (U-03): ``declare_node`` (``node_declared`` en la cadena de la planta),
``update_node`` (``status``, también ``re_enrollment_pending``, y ``live_view_local_url``, que puede
volver a nulo), ``assign_node_to_zone`` (misma planta y a lo sumo un nodo vigente por zona,
BR-NUC-08; ``node_zone_assigned``) y ``unassign_node`` (la actualización de cierre de TASK-107:
fija ``unassigned_at``, nunca borra; ``node_zone_unassigned``). Escriben los registros de U-02 con
la unidad U-02 (``with_unit``) aunque el contexto venga de U-03. La autorización de la operación
(``commissioning.run``…) la hace la ruta de U-03; aquí, con un contexto de sesión, la planta tiene
que estar en su alcance.

Las cuatro admiten ``transaction`` (TASK-218, extensión aditiva como
``EscritorExpediente.write(..., transaction=...)``): con ella, la operación va en la transacción
del llamador, de la organización del contexto, y solo existe si él confirma (la declaración con
sus zonas, el reemplazo y la revocación de U-03 son una sola transacción); las lecturas previas
van también en ella. Sin ella abren la suya, como siempre. ``unassign_node`` admite además el
``node_id`` que debe tener la zona (si es otro, ``zone_without_node``) y ``reason_es``, que queda
en ``node_zone_unassigned`` (versión 2).

**Génesis** (``OrganizationGenesis.create_client_organization``, la llama la orden administrativa
de TASK-132): con un contexto de orden administrativa con ``platform.organizations.create``, crea
la organización cliente y escribe ``organization_created`` (secuencia 1 de su cadena), su primera
planta con ``plant_created`` y su primer administrador ``invited`` con la asignación
``administrator`` sobre la organización y su invitación, todo en una transacción; después entrega
el enlace. La organización proveedora solo nace en el arranque de la plataforma (BR-NUC-06,
TASK-132): ``create_provider_organization`` con el contexto de ``bootstrap``, cuyo actor es el
primer ``platform_operator``, que nace invitado en la misma transacción. ``check`` y
``check_provider`` validan sin tocar la base (``vigia-admin … --dry-run``).

Todos los nombres pasan la política de texto libre (``FreeTextPolicyRegistry``).
"""

from __future__ import annotations

import contextlib
import re
import uuid
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal, Protocol

from sqlalchemy import exc as sa_exc
from sqlalchemy import text

from vigia_platform.identity.application.common import (
    IdentityDependencies,
    IdentityRejected,
    IdentityRejection,
    ScopeRef,
    as_uuid,
    checked_free_text,
    new_uuid4,
    resolve_scope,
    unique_violation,
    write_record,
)
from vigia_platform.identity.application.invitations import (
    EmailSenderRegistry,
    InvitationOutcome,
    checked_link_base,
    deliver,
    issue_invitation,
)
from vigia_platform.identity.application.users import (
    PlannedAssignment,
    checked_display_name,
    checked_email,
    checked_license,
    create_invited_user,
)
from vigia_platform.identity.authz.authorize import Resource, ResourceNotFound
from vigia_platform.identity.authz.context import operator_in_organization, with_unit
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import AuditOperation, ResourceRef
from vigia_platform.ledger.application.writer import RecordScope
from vigia_platform.ledger.free_text import FreeTextField, FreeTextRejected
from vigia_platform.shared.context import (
    ActorKind,
    ActorUnit,
    ContextOrigin,
    Role,
    ScopeContext,
    ScopeLevel,
    repository,
)
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "PROVIDER_CONCESSION_DEFAULT_DAYS",
    "PROVIDER_CONCESSION_MAX_DAYS",
    "FirstOperator",
    "GenesisRequest",
    "GenesisResult",
    "HierarchyService",
    "HierarchyView",
    "IdentityCommandPort",
    "IdentityQueryPort",
    "NodeStatus",
    "NodeView",
    "OrganizationGenesis",
    "OrganizationView",
    "PlantSpec",
    "PlantView",
    "ProviderAlreadyExists",
    "ProviderGenesisRequest",
    "ProviderGenesisResult",
    "Recipient",
    "ZoneSpec",
    "ZoneView",
]

_CODE: Final = re.compile(r"[A-Z0-9-]{2,32}")
_COUNTRY: Final = re.compile(r"[A-Z]{2}")
_TIMEZONE: Final = re.compile(r"(UTC|[A-Z][A-Za-z_]+(/[A-Za-z0-9_+-]+){1,2})")
"""La forma de ``PlantCreated.timezone`` (``ledger.record_types.u02``), ≤ 64 caracteres."""
_LIVE_VIEW_URL: Final = re.compile(
    r"https://([A-Za-z0-9]([A-Za-z0-9-]{0,62})(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}))*"
    r"|\[[0-9A-Fa-f:.]{2,45}\]):8443/"
)
"""Forma canónica de ``Heartbeat.live_view_local_url`` (adenda A-35; la de ``nuc_0004``)."""
NAME_MAX: Final = 120
NodeStatus = Literal["declared", "enrolled", "revoked", "re_enrollment_pending"]
_NODE_STATUSES: Final = frozenset({"declared", "enrolled", "revoked", "re_enrollment_pending"})
_PERSON_ACTORS: Final = frozenset({ActorKind.USER, ActorKind.PROVIDER_USER, ActorKind.OPERATOR})
"""Quien asigna un nodo a una zona queda en ``assigned_by`` (una cuenta, nunca el sistema)."""
UNASSIGNMENT_REASON: Final = FreeTextField("node_zone_unassigned", "/reason_es", 10, 500)
"""``reason_es`` de ``node_zone_unassigned`` v2: los límites de su esquema."""


# --- Valores ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlantSpec:
    code: str
    name: str
    country: str
    data_region: str
    timezone: str


@dataclass(frozen=True, slots=True)
class ZoneSpec:
    code: str
    name: str


@dataclass(frozen=True, slots=True)
class OrganizationView:
    organization_id: uuid.UUID
    code: str
    name: str
    kind: str
    status: str


@dataclass(frozen=True, slots=True)
class ZoneView:
    zone_id: uuid.UUID
    plant_id: uuid.UUID
    code: str
    name: str
    node_id: uuid.UUID | None
    """El nodo vigente de la zona, si tiene."""


@dataclass(frozen=True, slots=True)
class PlantView:
    plant_id: uuid.UUID
    code: str
    name: str
    country: str
    data_region: str
    timezone: str
    status: str
    zones: tuple[ZoneView, ...] = ()


@dataclass(frozen=True, slots=True)
class NodeView:
    node_id: uuid.UUID
    plant_id: uuid.UUID
    code: str
    status: str
    live_view_local_url: str | None


@dataclass(frozen=True, slots=True)
class HierarchyView:
    organization: OrganizationView
    plants: tuple[PlantView, ...]
    nodes: tuple[NodeView, ...]


@dataclass(frozen=True, slots=True)
class Recipient:
    """Un destinatario activo con un rol vigente sobre un alcance que contiene al pedido."""

    user_id: uuid.UUID
    role: Role
    display_name: str = field(repr=False)
    email: str = field(repr=False)


class IdentityQueryPort(Protocol):
    """``IdentityQueryPort`` (business-logic-model §10.1)."""

    async def hierarchy(self, context: ScopeContext) -> HierarchyView: ...

    async def users_by_role_and_scope(
        self,
        context: ScopeContext,
        roles: Iterable[Role],
        scope_level: ScopeLevel,
        scope_id: uuid.UUID,
    ) -> tuple[Recipient, ...]: ...

    async def node_identity(self, context: ScopeContext, node_id: uuid.UUID) -> NodeView | None: ...

    async def assigned_node(self, context: ScopeContext, zone_id: uuid.UUID) -> NodeView | None: ...


class IdentityCommandPort(Protocol):
    """``IdentityCommandPort`` (business-logic-model §10.1); ``transaction`` es de TASK-218."""

    async def declare_node(
        self,
        context: ScopeContext,
        plant_id: uuid.UUID,
        code: str,
        *,
        transaction: Transaction | None = None,
    ) -> NodeView: ...

    async def update_node(
        self,
        context: ScopeContext,
        node_id: uuid.UUID,
        status: NodeStatus,
        live_view_local_url: str | None,
        *,
        transaction: Transaction | None = None,
    ) -> NodeView: ...

    async def assign_node_to_zone(
        self,
        context: ScopeContext,
        node_id: uuid.UUID,
        zone_id: uuid.UUID,
        *,
        transaction: Transaction | None = None,
    ) -> uuid.UUID: ...

    async def unassign_node(
        self,
        context: ScopeContext,
        zone_id: uuid.UUID,
        *,
        node_id: uuid.UUID | None = None,
        reason_es: str | None = None,
        transaction: Transaction | None = None,
    ) -> uuid.UUID: ...


# --- Validación ---------------------------------------------------------------------------------


def checked_code(value: object, path: str = "/code") -> str:
    if not isinstance(value, str) or _CODE.fullmatch(value) is None:
        raise IdentityRejected(IdentityRejection.INVALID_VALUE, field=path)
    return value


def _checked_plant(deps: IdentityDependencies, spec: PlantSpec) -> PlantSpec:
    if not isinstance(spec, PlantSpec):
        raise TypeError("spec debe ser PlantSpec")
    if not isinstance(spec.country, str) or _COUNTRY.fullmatch(spec.country) is None:
        raise IdentityRejected(IdentityRejection.INVALID_VALUE, field="/country")
    if (
        not isinstance(spec.timezone, str)
        or not 3 <= len(spec.timezone) <= 64
        or _TIMEZONE.fullmatch(spec.timezone) is None
    ):
        raise IdentityRejected(IdentityRejection.INVALID_VALUE, field="/timezone")
    if not isinstance(spec.data_region, str) or not 1 <= len(spec.data_region) <= 32:
        raise IdentityRejected(IdentityRejection.INVALID_VALUE, field="/data_region")
    return PlantSpec(
        code=checked_code(spec.code),
        name=checked_free_text(deps, spec.name, entity="plant", path="/name", max_length=NAME_MAX),
        country=spec.country,
        data_region=spec.data_region,
        timezone=spec.timezone,
    )


def _checked_zone(deps: IdentityDependencies, spec: ZoneSpec) -> ZoneSpec:
    if not isinstance(spec, ZoneSpec):
        raise TypeError("spec debe ser ZoneSpec")
    return ZoneSpec(
        code=checked_code(spec.code),
        name=checked_free_text(deps, spec.name, entity="zone", path="/name", max_length=NAME_MAX),
    )


def _whole_organization(context: ScopeContext) -> bool:
    """Los contextos de evento, iteración y orden administrativa operan sobre su organización."""
    return context.origin is not ContextOrigin.SESSION


def _require_plant_in_scope(context: ScopeContext, plant_id: uuid.UUID) -> None:
    if not _whole_organization(context) and not context.covers(plant_id):
        raise ResourceNotFound()


# --- Sentencias ---------------------------------------------------------------------------------

_DATA_REGION: Final = text("SELECT 1 FROM identity.data_region WHERE region_code = :region")
_PLANT_CODE_TAKEN: Final = text(
    "SELECT 1 FROM identity.plant WHERE organization_id = :organization_id AND code = :code"
)
_ZONE_CODE_TAKEN: Final = text(
    "SELECT 1 FROM identity.zone WHERE plant_id = :plant_id AND code = :code"
)
_NODE_CODE_TAKEN: Final = text(
    "SELECT 1 FROM identity.node_identity WHERE organization_id = :organization_id AND code = :code"
)
_INSERT_ORGANIZATION: Final = text(
    "INSERT INTO identity.organization (organization_id, code, name, kind, status,"
    " concession_max_days, concession_default_days, created_at, created_by)"
    " VALUES (:organization_id, :code, :name, 'client', 'active', :concession_max_days,"
    " :concession_default_days, :created_at, :created_by)"
)
_INSERT_PROVIDER: Final = text(
    "INSERT INTO identity.organization (organization_id, code, name, kind, status,"
    " concession_max_days, concession_default_days, created_at, created_by)"
    " VALUES (:organization_id, :code, :name, 'provider', 'active', :concession_max_days,"
    " :concession_default_days, :created_at, :created_by)"
)
_FIRST_OPERATOR: Final = text(
    "SELECT u.user_id, u.display_name, u.email, u.status FROM identity.user_account AS u"
    " WHERE EXISTS (SELECT 1 FROM identity.role_assignment AS r WHERE r.user_id = u.user_id"
    " AND r.role = 'platform_operator' AND r.removed_at IS NULL)"
    " ORDER BY u.created_at, u.user_id LIMIT 1"
)
_INSERT_PLANT: Final = text(
    "INSERT INTO identity.plant (plant_id, organization_id, code, name, country, data_region,"
    " timezone, status, created_at, created_by) VALUES (:plant_id, :organization_id, :code,"
    " :name, :country, :data_region, :timezone, 'active', :created_at, :created_by)"
)
_INSERT_ZONE: Final = text(
    "INSERT INTO identity.zone (zone_id, organization_id, plant_id, code, name, created_at,"
    " created_by) VALUES (:zone_id, :organization_id, :plant_id, :code, :name, :created_at,"
    " :created_by)"
)
_INSERT_NODE: Final = text(
    "INSERT INTO identity.node_identity (node_id, organization_id, plant_id, code, status,"
    " created_at) VALUES (:node_id, :organization_id, :plant_id, :code, 'declared', :created_at)"
)
_UPDATE_NODE: Final = text(
    "UPDATE identity.node_identity SET status = :status,"
    " live_view_local_url = :live_view_local_url WHERE node_id = :node_id"
    " RETURNING node_id, plant_id, code, status, live_view_local_url"
)
_NODE: Final = text(
    "SELECT node_id, plant_id, code, status, live_view_local_url FROM identity.node_identity"
    " WHERE node_id = :node_id"
)
_LOCK_ZONE: Final = text(
    "SELECT zone_id, plant_id FROM identity.zone WHERE zone_id = :zone_id FOR UPDATE"
)
_CURRENT_ASSIGNMENT: Final = text(
    "SELECT assignment_id, node_id, plant_id FROM identity.zone_node_assignment"
    " WHERE zone_id = :zone_id AND unassigned_at IS NULL"
)
_INSERT_NODE_ASSIGNMENT: Final = text(
    "INSERT INTO identity.zone_node_assignment (assignment_id, organization_id, plant_id,"
    " zone_id, node_id, assigned_at, assigned_by) VALUES (:assignment_id, :organization_id,"
    " :plant_id, :zone_id, :node_id, :assigned_at, :assigned_by)"
)
_CLOSE_NODE_ASSIGNMENT: Final = text(
    "UPDATE identity.zone_node_assignment SET unassigned_at = :now"
    " WHERE assignment_id = :assignment_id AND unassigned_at IS NULL RETURNING assignment_id"
)
_ORGANIZATION: Final = text(
    "SELECT organization_id, code, name, kind, status FROM identity.organization"
)
_PLANTS: Final = text(
    "SELECT plant_id, code, name, country, data_region, timezone, status FROM identity.plant"
    " ORDER BY code, plant_id"
)
_ZONES: Final = text(
    "SELECT z.zone_id, z.plant_id, z.code, z.name, a.node_id FROM identity.zone AS z"
    " LEFT JOIN identity.zone_node_assignment AS a"
    " ON a.zone_id = z.zone_id AND a.unassigned_at IS NULL ORDER BY z.code, z.zone_id"
)
_NODES: Final = text(
    "SELECT node_id, plant_id, code, status, live_view_local_url FROM identity.node_identity"
    " ORDER BY code, node_id"
)
_ASSIGNED_NODE: Final = text(
    "SELECT n.node_id, n.plant_id, n.code, n.status, n.live_view_local_url,"
    " z.plant_id AS zone_plant FROM identity.zone AS z"
    " LEFT JOIN identity.zone_node_assignment AS a"
    " ON a.zone_id = z.zone_id AND a.unassigned_at IS NULL"
    " LEFT JOIN identity.node_identity AS n ON n.node_id = a.node_id"
    " WHERE z.zone_id = :zone_id"
)
_RECIPIENTS: Final = text(
    "SELECT u.user_id, u.display_name, u.email, r.role, r.scope_level, r.scope_id,"
    " COALESCE(p.plant_id, z.plant_id) AS plant_id"
    " FROM identity.role_assignment AS r"
    " JOIN identity.user_account AS u ON u.user_id = r.user_id"
    " LEFT JOIN identity.plant AS p ON r.scope_level = 'plant' AND p.plant_id = r.scope_id"
    " LEFT JOIN identity.zone AS z ON r.scope_level = 'zone' AND z.zone_id = r.scope_id"
    " WHERE r.removed_at IS NULL AND u.status = 'active' AND r.role = ANY(:roles)"
    " ORDER BY u.user_id, r.assignment_id"
)


def _node_view(row: Any) -> NodeView:
    return NodeView(
        node_id=as_uuid(row.node_id),
        plant_id=as_uuid(row.plant_id),
        code=row.code,
        status=row.status,
        live_view_local_url=row.live_view_local_url,
    )


def _hierarchy_view(
    context: ScopeContext,
    organization: Any,
    plants: Iterable[Any],
    zones: Iterable[Any],
    nodes: Iterable[Any],
) -> HierarchyView:
    """Las filas leídas, recortadas al alcance del contexto."""
    whole = _whole_organization(context)
    zone_views = [
        ZoneView(
            as_uuid(row.zone_id),
            as_uuid(row.plant_id),
            row.code,
            row.name,
            None if row.node_id is None else as_uuid(row.node_id),
        )
        for row in zones
    ]
    visible_zones = [
        zone for zone in zone_views if whole or context.covers(zone.plant_id, zone.zone_id)
    ]
    plant_views: list[PlantView] = []
    for row in plants:
        plant_id = as_uuid(row.plant_id)
        own = tuple(zone for zone in visible_zones if zone.plant_id == plant_id)
        if not (whole or context.covers(plant_id) or own):
            continue
        plant_views.append(
            PlantView(
                plant_id,
                row.code,
                row.name,
                row.country,
                row.data_region,
                row.timezone,
                row.status,
                own,
            )
        )
    node_views = tuple(
        view
        for view in (_node_view(row) for row in nodes)
        if whole or context.covers(view.plant_id)
    )
    return HierarchyView(
        OrganizationView(
            as_uuid(organization.organization_id),
            organization.code,
            organization.name,
            organization.kind,
            organization.status,
        ),
        tuple(plant_views),
        node_views,
    )


async def _require_absent(
    transaction: Transaction, statement: object, **parameters: object
) -> None:
    if (await transaction.execute(statement, parameters)).first() is not None:  # type: ignore[arg-type]
        raise IdentityRejected(IdentityRejection.CODE_TAKEN, field="/code")


async def insert_plant(
    deps: IdentityDependencies, transaction: Transaction, spec: PlantSpec, now: datetime
) -> PlantView:
    """La planta, su génesis ``plant_created`` y su auditoría, en ``transaction``."""
    context = transaction.context
    if (await transaction.execute(_DATA_REGION, {"region": spec.data_region})).first() is None:
        raise IdentityRejected(IdentityRejection.INVALID_VALUE, field="/data_region")
    await _require_absent(
        transaction, _PLANT_CODE_TAKEN, organization_id=context.organization_id, code=spec.code
    )
    plant_id = new_uuid4(deps.random_bytes)
    await transaction.execute(
        _INSERT_PLANT,
        {
            "plant_id": plant_id,
            "organization_id": context.organization_id,
            "code": spec.code,
            "name": spec.name,
            "country": spec.country,
            "data_region": spec.data_region,
            "timezone": spec.timezone,
            "created_at": now,
            "created_by": context.actor.id,
        },
    )
    await write_record(
        deps,
        context,
        transaction,
        "plant_created",
        {
            "plant_id": str(plant_id),
            "code": spec.code,
            "name": spec.name,
            "country": spec.country,
            "data_region": spec.data_region,
            "timezone": spec.timezone,
            "created_by": str(context.actor.id),
        },
    )
    await deps.audit.append(
        context,
        AuditOperation.PLANT_CREATED,
        plant_id=plant_id,
        resource=ResourceRef("plant", plant_id),
        transaction=transaction,
    )
    return PlantView(
        plant_id, spec.code, spec.name, spec.country, spec.data_region, spec.timezone, "active"
    )


def _code_taken(error: sa_exc.IntegrityError, *constraints: str) -> bool:
    return unique_violation(error) in constraints


@contextlib.asynccontextmanager
async def _within(
    deps: IdentityDependencies, context: ScopeContext, transaction: Transaction | None
) -> AsyncIterator[Transaction]:
    """La transacción del llamador (de la organización de ``context``) o una propia."""
    if transaction is None:
        async with deps.database.transaction(context) as opened:
            yield opened
        return
    if (
        not isinstance(transaction, Transaction)
        or transaction.context.organization_id != context.organization_id
    ):
        raise TypeError("transaction debe ser una Transaction de la organización del contexto")
    yield transaction


def _checked_reason(deps: IdentityDependencies, value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise IdentityRejected(IdentityRejection.INVALID_VALUE, field="/reason_es")
    try:
        return deps.free_text.apply(value, UNASSIGNMENT_REASON)
    except FreeTextRejected:
        raise IdentityRejected(IdentityRejection.FREE_TEXT_REJECTED, field="/reason_es") from None


# --- Servicio de jerarquía ----------------------------------------------------------------------


@repository
class HierarchyService:
    """Plantas y zonas (interfaz), ``IdentityQueryPort`` e ``IdentityCommandPort``."""

    def __init__(self, deps: IdentityDependencies) -> None:
        self._deps = deps

    def __repr__(self) -> str:
        return "HierarchyService()"

    # --- Plantas y zonas ------------------------------------------------------------------------

    async def create_plant(self, context: ScopeContext, spec: PlantSpec) -> PlantView:
        """``POST /plants``: planta nueva con su génesis (``hierarchy.manage``)."""
        deps = self._deps
        checked = _checked_plant(deps, spec)
        authorized = await deps.authorizer.authorize(
            context, PermissionKey.HIERARCHY_MANAGE, Resource.organization(context.organization_id)
        )
        try:
            async with deps.database.transaction(authorized) as transaction:
                return await insert_plant(deps, transaction, checked, deps.clock.now())
        except sa_exc.IntegrityError as error:
            if _code_taken(error, "plant_code_unique"):
                raise IdentityRejected(IdentityRejection.CODE_TAKEN, field="/code") from None
            raise

    async def create_zone(
        self, context: ScopeContext, plant_id: uuid.UUID, spec: ZoneSpec
    ) -> ZoneView:
        """``POST /plants/{id}/zones``: zona nueva de ``plant_id`` (``hierarchy.manage``)."""
        deps = self._deps
        checked = _checked_zone(deps, spec)
        plant = await resolve_scope(deps, context, ScopeLevel.PLANT, plant_id)
        authorized = await deps.authorizer.authorize(
            context,
            PermissionKey.HIERARCHY_MANAGE,
            Resource.plant(context.organization_id, plant.scope_id),
        )
        now = deps.clock.now()
        zone_id = new_uuid4(deps.random_bytes)
        try:
            async with deps.database.transaction(authorized) as transaction:
                await _require_absent(
                    transaction, _ZONE_CODE_TAKEN, plant_id=plant.scope_id, code=checked.code
                )
                await transaction.execute(
                    _INSERT_ZONE,
                    {
                        "zone_id": zone_id,
                        "organization_id": authorized.organization_id,
                        "plant_id": plant.scope_id,
                        "code": checked.code,
                        "name": checked.name,
                        "created_at": now,
                        "created_by": authorized.actor.id,
                    },
                )
                await write_record(
                    deps,
                    authorized,
                    transaction,
                    "zone_created",
                    {
                        "zone_id": str(zone_id),
                        "plant_id": str(plant.scope_id),
                        "code": checked.code,
                        "name": checked.name,
                        "created_by": str(authorized.actor.id),
                    },
                )
                await deps.audit.append(
                    authorized,
                    AuditOperation.ZONE_CREATED,
                    plant_id=plant.scope_id,
                    zone_id=zone_id,
                    resource=ResourceRef("zone", zone_id),
                    transaction=transaction,
                )
        except sa_exc.IntegrityError as error:
            if _code_taken(error, "zone_code_unique"):
                raise IdentityRejected(IdentityRejection.CODE_TAKEN, field="/code") from None
            raise
        return ZoneView(zone_id, plant.scope_id, checked.code, checked.name, None)

    # --- IdentityQueryPort ---------------------------------------------------------------------

    async def hierarchy(self, context: ScopeContext) -> HierarchyView:
        """La organización con las plantas, zonas y nodos al alcance del contexto.

        Bajo concesión, la lectura deja además su entrada ``hierarchy_read`` en la auditoría del
        cliente, en la misma transacción (BR-NUC-38), con el número de plantas devueltas.
        """
        async with self._deps.database.transaction(context) as transaction:
            organization = (await transaction.execute(_ORGANIZATION)).one()
            plants = (await transaction.execute(_PLANTS)).all()
            zones = (await transaction.execute(_ZONES)).all()
            nodes = (await transaction.execute(_NODES)).all()
            view = _hierarchy_view(context, organization, plants, zones, nodes)
            if context.concession_id is not None:
                await self._deps.audit.append(
                    context,
                    AuditOperation.HIERARCHY_READ,
                    result_count=len(view.plants),
                    transaction=transaction,
                )
        return view

    async def users_by_role_and_scope(
        self,
        context: ScopeContext,
        roles: Iterable[Role],
        scope_level: ScopeLevel,
        scope_id: uuid.UUID,
    ) -> tuple[Recipient, ...]:
        """Usuarios activos con alguno de ``roles`` vigente sobre un alcance que contiene al
        pedido (destinatarios de una notificación de U-04)."""
        deps = self._deps
        wanted = sorted({Role(role).value for role in roles})
        target = await resolve_scope(deps, context, scope_level, scope_id)
        # Con una sesión, el alcance pedido tiene que estar cubierto por sus asignaciones
        # (BR-NUC-09, 12): si no, como inexistente. Un alcance de organización solo lo cubre una
        # asignación de organización; el contexto del aviso pendiente, sin asignaciones, no cubre
        # nada.
        zone_id = target.scope_id if target.level is ScopeLevel.ZONE else None
        if not _whole_organization(context) and not context.covers(target.plant_id, zone_id):
            raise ResourceNotFound()
        if not wanted:
            return ()
        rows = await deps.database.read(context, _RECIPIENTS, {"roles": wanted})
        seen: set[tuple[uuid.UUID, Role]] = set()
        result: list[Recipient] = []
        for row in rows:
            level = ScopeLevel(row.scope_level)
            held = ScopeRef(
                level,
                as_uuid(row.scope_id),
                None if level is ScopeLevel.ORGANIZATION else as_uuid(row.plant_id),
            )
            key = (as_uuid(row.user_id), Role(row.role))
            if key in seen or not held.contains(target):
                continue
            seen.add(key)
            result.append(Recipient(key[0], key[1], row.display_name, row.email))
        return tuple(result)

    async def node_identity(self, context: ScopeContext, node_id: uuid.UUID) -> NodeView | None:
        """La identidad del nodo, o ``None`` si no existe o está fuera de alcance."""
        if type(node_id) is not uuid.UUID:
            return None
        rows = await self._deps.database.read(context, _NODE, {"node_id": node_id})
        if not rows:
            return None
        view = _node_view(rows[0])
        if not _whole_organization(context) and not context.covers(view.plant_id):
            return None
        return view

    async def _node_in(
        self, context: ScopeContext, node_id: uuid.UUID, transaction: Transaction | None
    ) -> NodeView | None:
        """``node_identity``; con ``transaction``, leída dentro de ella (ve lo no confirmado)."""
        if transaction is None:
            return await self.node_identity(context, node_id)
        if type(node_id) is not uuid.UUID:
            return None
        row = (await transaction.execute(_NODE, {"node_id": node_id})).first()
        if row is None:
            return None
        view = _node_view(row)
        if not _whole_organization(context) and not context.covers(view.plant_id):
            return None
        return view

    async def assigned_node(self, context: ScopeContext, zone_id: uuid.UUID) -> NodeView | None:
        """El nodo vigente de la zona, o ``None`` (sin nodo, inexistente o fuera de alcance)."""
        if type(zone_id) is not uuid.UUID:
            return None
        rows = await self._deps.database.read(context, _ASSIGNED_NODE, {"zone_id": zone_id})
        if not rows:
            return None
        row = rows[0]
        if not _whole_organization(context) and not context.covers(
            as_uuid(row.zone_plant), zone_id
        ):
            return None
        return None if row.node_id is None else _node_view(row)

    # --- IdentityCommandPort -------------------------------------------------------------------

    async def declare_node(
        self,
        context: ScopeContext,
        plant_id: uuid.UUID,
        code: str,
        *,
        transaction: Transaction | None = None,
    ) -> NodeView:
        """Declara la identidad de un nodo de ``plant_id`` (``node_declared``)."""
        deps = self._deps
        node_code = checked_code(code)
        plant = await resolve_scope(deps, context, ScopeLevel.PLANT, plant_id)
        _require_plant_in_scope(context, plant.scope_id)
        writer_context = with_unit(context, ActorUnit.U02)
        node_id = new_uuid4(deps.random_bytes)
        now = deps.clock.now()
        try:
            async with _within(deps, writer_context, transaction) as current:
                await _require_absent(
                    current,
                    _NODE_CODE_TAKEN,
                    organization_id=context.organization_id,
                    code=node_code,
                )
                await current.execute(
                    _INSERT_NODE,
                    {
                        "node_id": node_id,
                        "organization_id": context.organization_id,
                        "plant_id": plant.scope_id,
                        "code": node_code,
                        "created_at": now,
                    },
                )
                await write_record(
                    deps,
                    writer_context,
                    current,
                    "node_declared",
                    {
                        "node_id": str(node_id),
                        "plant_id": str(plant.scope_id),
                        "code": node_code,
                        "declared_by": str(context.actor.id),
                    },
                )
        except sa_exc.IntegrityError as error:
            if _code_taken(error, "node_identity_code_unique"):
                raise IdentityRejected(IdentityRejection.CODE_TAKEN, field="/code") from None
            raise
        return NodeView(node_id, plant.scope_id, node_code, "declared", None)

    async def update_node(
        self,
        context: ScopeContext,
        node_id: uuid.UUID,
        status: NodeStatus,
        live_view_local_url: str | None,
        *,
        transaction: Transaction | None = None,
    ) -> NodeView:
        """Fija ``status`` y ``live_view_local_url`` (nula si el nodo no la anunció)."""
        if status not in _NODE_STATUSES:
            raise IdentityRejected(IdentityRejection.INVALID_VALUE, field="/status")
        if live_view_local_url is not None and (
            not isinstance(live_view_local_url, str)
            or len(live_view_local_url) > 256
            or _LIVE_VIEW_URL.fullmatch(live_view_local_url) is None
        ):
            raise IdentityRejected(IdentityRejection.INVALID_VALUE, field="/live_view_local_url")
        if transaction is None and await self.node_identity(context, node_id) is None:
            raise ResourceNotFound()
        async with _within(self._deps, context, transaction) as current:
            if transaction is not None and await self._node_in(context, node_id, current) is None:
                raise ResourceNotFound()
            row = (
                await current.execute(
                    _UPDATE_NODE,
                    {
                        "node_id": node_id,
                        "status": status,
                        "live_view_local_url": live_view_local_url,
                    },
                )
            ).one_or_none()
        if row is None:
            raise ResourceNotFound()
        return _node_view(row)

    async def assign_node_to_zone(
        self,
        context: ScopeContext,
        node_id: uuid.UUID,
        zone_id: uuid.UUID,
        *,
        transaction: Transaction | None = None,
    ) -> uuid.UUID:
        """Asigna ``node_id`` a ``zone_id`` de la misma planta; la zona no puede tener otro nodo
        vigente (BR-NUC-08). Devuelve el identificador de la asignación."""
        deps = self._deps
        if context.actor.kind not in _PERSON_ACTORS:
            raise PermissionError("la asignación de un nodo la hace una persona identificada")
        found: NodeView | None = None
        if transaction is None:
            found = await self.node_identity(context, node_id)
            if found is None or type(zone_id) is not uuid.UUID:
                raise ResourceNotFound()
        writer_context = with_unit(context, ActorUnit.U02)
        assignment_id = uuid7(deps.clock, deps.random_bytes)
        now = deps.clock.now()
        async with _within(deps, writer_context, transaction) as current:
            if transaction is not None:
                found = await self._node_in(context, node_id, current)
            if found is None or type(zone_id) is not uuid.UUID:
                raise ResourceNotFound()
            node = found
            zone = (await current.execute(_LOCK_ZONE, {"zone_id": zone_id})).first()
            if zone is None:
                raise ResourceNotFound()
            plant_id = as_uuid(zone.plant_id)
            _require_plant_in_scope(context, plant_id)
            if plant_id != node.plant_id:
                raise IdentityRejected(IdentityRejection.NODE_PLANT_MISMATCH, field="/zone_id")
            if (await current.execute(_CURRENT_ASSIGNMENT, {"zone_id": zone_id})).first():
                raise IdentityRejected(IdentityRejection.ZONE_HAS_NODE, field="/zone_id")
            await current.execute(
                _INSERT_NODE_ASSIGNMENT,
                {
                    "assignment_id": assignment_id,
                    "organization_id": context.organization_id,
                    "plant_id": plant_id,
                    "zone_id": zone_id,
                    "node_id": node.node_id,
                    "assigned_at": now,
                    "assigned_by": context.actor.id,
                },
            )
            await write_record(
                deps,
                writer_context,
                current,
                "node_zone_assigned",
                {
                    "assignment_id": str(assignment_id),
                    "zone_id": str(zone_id),
                    "node_id": str(node.node_id),
                    "assigned_at": format_timestamp(now),
                    "assigned_by": str(context.actor.id),
                },
                scope=RecordScope(plant_id=plant_id),
            )
            await deps.audit.append(
                writer_context,
                AuditOperation.NODE_ZONE_ASSIGNED,
                plant_id=plant_id,
                zone_id=zone_id,
                resource=ResourceRef("node", node.node_id),
                transaction=current,
            )
        return assignment_id

    async def unassign_node(
        self,
        context: ScopeContext,
        zone_id: uuid.UUID,
        *,
        node_id: uuid.UUID | None = None,
        reason_es: str | None = None,
        transaction: Transaction | None = None,
    ) -> uuid.UUID:
        """Retira el nodo vigente de ``zone_id`` (actualización de cierre; nunca borra).

        Con ``node_id``, la zona tiene que tenerlo a él (si no, ``zone_without_node``);
        ``reason_es`` pasa la política de texto libre y queda en el registro.
        """
        deps = self._deps
        if type(zone_id) is not uuid.UUID:
            raise ResourceNotFound()
        reason = _checked_reason(deps, reason_es)
        writer_context = with_unit(context, ActorUnit.U02)
        now = deps.clock.now()
        async with _within(deps, writer_context, transaction) as tx:
            zone = (await tx.execute(_LOCK_ZONE, {"zone_id": zone_id})).first()
            if zone is None:
                raise ResourceNotFound()
            plant_id = as_uuid(zone.plant_id)
            _require_plant_in_scope(context, plant_id)
            current = (await tx.execute(_CURRENT_ASSIGNMENT, {"zone_id": zone_id})).first()
            if current is None or (node_id is not None and as_uuid(current.node_id) != node_id):
                raise IdentityRejected(IdentityRejection.ZONE_WITHOUT_NODE, field="/zone_id")
            assignment_id = as_uuid(current.assignment_id)
            await tx.execute(_CLOSE_NODE_ASSIGNMENT, {"assignment_id": assignment_id, "now": now})
            content: dict[str, Any] = {
                "assignment_id": str(assignment_id),
                "zone_id": str(zone_id),
                "node_id": str(as_uuid(current.node_id)),
                "unassigned_at": format_timestamp(now),
                "unassigned_by": str(context.actor.id),
            }
            if reason is not None:
                content["reason_es"] = reason
            await write_record(
                deps,
                writer_context,
                tx,
                "node_zone_unassigned",
                content,
                scope=RecordScope(plant_id=plant_id),
            )
            await deps.audit.append(
                writer_context,
                AuditOperation.NODE_ZONE_UNASSIGNED,
                plant_id=plant_id,
                zone_id=zone_id,
                resource=ResourceRef("node", as_uuid(current.node_id)),
                transaction=tx,
            )
        return assignment_id


# --- Génesis ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GenesisRequest:
    """``vigia-admin create-organization`` (TASK-132): organización, primera planta y primer
    administrador."""

    code: str
    name: str
    plant: PlantSpec
    administrator_email: str
    administrator_display_name: str
    administrator_professional_license: str | None = None
    concession_max_days: int = 30
    concession_default_days: int = 7
    disclose_link: bool = False


@dataclass(frozen=True, slots=True)
class GenesisResult:
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    administrator_user_id: uuid.UUID
    invitation: InvitationOutcome


PROVIDER_CONCESSION_MAX_DAYS: Final = 30
PROVIDER_CONCESSION_DEFAULT_DAYS: Final = 7
"""Los topes por omisión de ``identity.organization``: en la proveedora no se usan (las
concesiones son siempre sobre clientes), pero la génesis los registra como todas."""


@dataclass(frozen=True, slots=True)
class ProviderGenesisRequest:
    """``vigia-admin bootstrap``: la organización proveedora y su primer operador."""

    code: str
    name: str
    operator_email: str = field(repr=False)
    operator_display_name: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ProviderGenesisResult:
    organization_id: uuid.UUID
    operator_user_id: uuid.UUID
    invitation: InvitationOutcome


@dataclass(frozen=True, slots=True)
class FirstOperator:
    """El primer ``platform_operator`` (``bootstrap --resume``)."""

    user_id: uuid.UUID
    display_name: str = field(repr=False)
    email: str = field(repr=False)
    status: str


class ProviderAlreadyExists(Exception):
    """Ya hay una organización proveedora: ``bootstrap`` no crea otra (BR-NUC-06)."""

    def __init__(self) -> None:
        super().__init__("la organización proveedora ya existe")


@repository
class OrganizationGenesis:
    """Alta de una organización cliente (``business-logic-model.md`` §2; BR-NUC-06)."""

    def __init__(
        self,
        deps: IdentityDependencies,
        *,
        senders: EmailSenderRegistry,
        link_base: str,
    ) -> None:
        self._deps = deps
        self._senders = senders
        self._link_base = checked_link_base(link_base)

    def __repr__(self) -> str:
        return "OrganizationGenesis()"

    def check(self, request: GenesisRequest) -> GenesisRequest:
        """La petición validada y normalizada, sin tocar la base ni autorizar (``--dry-run``)."""
        deps = self._deps
        if not isinstance(request, GenesisRequest):
            raise TypeError("request debe ser GenesisRequest")
        max_days, default_days = request.concession_max_days, request.concession_default_days
        checked = GenesisRequest(
            code=checked_code(request.code),
            name=checked_free_text(
                deps, request.name, entity="organization", path="/name", max_length=NAME_MAX
            ),
            plant=_checked_plant(deps, request.plant),
            administrator_email=checked_email(request.administrator_email),
            administrator_display_name=checked_display_name(
                deps, request.administrator_display_name
            ),
            administrator_professional_license=checked_license(
                deps, request.administrator_professional_license
            ),
            concession_max_days=max_days,
            concession_default_days=default_days,
            disclose_link=request.disclose_link is True,
        )
        if (
            type(max_days) is not int
            or type(default_days) is not int
            or not 1 <= max_days <= 90
            or not 1 <= default_days <= max_days
        ):
            raise IdentityRejected(IdentityRejection.INVALID_VALUE, field="/concession_max_days")
        return checked

    async def create_client_organization(
        self, operator_context: ScopeContext, request: GenesisRequest
    ) -> GenesisResult:
        """Organización cliente, su génesis, su primera planta y su primer administrador."""
        deps = self._deps
        checked = self.check(request)
        code, name, plant = checked.code, checked.name, checked.plant
        email = checked.administrator_email
        display_name = checked.administrator_display_name
        license_ = checked.administrator_professional_license
        max_days, default_days = checked.concession_max_days, checked.concession_default_days
        authorized = await deps.authorizer.authorize(
            operator_context,
            PermissionKey.PLATFORM_ORGANIZATIONS_CREATE,
            Resource.organization(deps.provider_organization_id),
        )
        organization_id = new_uuid4(deps.random_bytes)
        client = operator_in_organization(
            authorized, organization_id, provider_organization_id=deps.provider_organization_id
        )
        now = deps.clock.now()
        try:
            async with deps.database.transaction(client) as transaction:
                await transaction.execute(
                    _INSERT_ORGANIZATION,
                    {
                        "organization_id": organization_id,
                        "code": code,
                        "name": name,
                        "concession_max_days": max_days,
                        "concession_default_days": default_days,
                        "created_at": now,
                        "created_by": client.actor.id,
                    },
                )
                await write_record(
                    deps,
                    client,
                    transaction,
                    "organization_created",
                    {
                        "organization_id": str(organization_id),
                        "code": code,
                        "name": name,
                        "kind": "client",
                        "concession_max_days": max_days,
                        "concession_default_days": default_days,
                        "created_by": str(client.actor.id),
                    },
                )
                first_plant = await insert_plant(deps, transaction, plant, now)
                issued = await create_invited_user(
                    deps,
                    transaction,
                    email=email,
                    display_name=display_name,
                    professional_license=license_,
                    assignments=(
                        PlannedAssignment(
                            Role.ADMINISTRATOR,
                            ScopeRef(ScopeLevel.ORGANIZATION, organization_id),
                        ),
                    ),
                    now=now,
                )
        except sa_exc.IntegrityError as error:
            if _code_taken(error, "organization_code_unique"):
                raise IdentityRejected(IdentityRejection.CODE_TAKEN, field="/code") from None
            if _code_taken(error, "user_account_email_unique"):
                raise IdentityRejected(
                    IdentityRejection.EMAIL_UNAVAILABLE, field="/email"
                ) from None
            raise
        invitation = await deliver(
            deps,
            self._senders,
            client,
            issued,
            email=email,
            link_base=self._link_base,
            disclose=checked.disclose_link,
        )
        return GenesisResult(organization_id, first_plant.plant_id, issued.user_id, invitation)

    # --- Organización proveedora (arranque de la plataforma) -----------------------------------

    def check_provider(self, request: ProviderGenesisRequest) -> ProviderGenesisRequest:
        """La petición de ``bootstrap`` validada, sin tocar la base (``--dry-run``)."""
        deps = self._deps
        if not isinstance(request, ProviderGenesisRequest):
            raise TypeError("request debe ser ProviderGenesisRequest")
        return ProviderGenesisRequest(
            code=checked_code(request.code),
            name=checked_free_text(
                deps, request.name, entity="organization", path="/name", max_length=NAME_MAX
            ),
            operator_email=checked_email(request.operator_email),
            operator_display_name=checked_display_name(deps, request.operator_display_name),
        )

    async def create_provider_organization(
        self, bootstrap_context: ScopeContext, request: ProviderGenesisRequest
    ) -> ProviderGenesisResult:
        """La organización proveedora, su génesis y su primer ``platform_operator`` invitado.

        ``bootstrap_context`` sale de ``ScopeContexts.bootstrap_operator_context``: su actor es el
        operador que nace aquí (``created_by``, ``invited_by``). Todo en una transacción; después
        el enlace se divulga **una vez** al llamador (``invitation.link``), que lo deja en el
        secreto de un solo uso y nunca en la salida. Si ya existe una proveedora (índice
        ``organization_single_provider``), ``ProviderAlreadyExists`` y no queda nada.
        """
        deps = self._deps
        checked = self.check_provider(request)
        _require_bootstrap_context(deps, bootstrap_context)
        organization_id = bootstrap_context.organization_id
        operator_id = bootstrap_context.actor.id
        now = deps.clock.now()
        try:
            async with deps.database.transaction(bootstrap_context) as transaction:
                await transaction.execute(
                    _INSERT_PROVIDER,
                    {
                        "organization_id": organization_id,
                        "code": checked.code,
                        "name": checked.name,
                        "concession_max_days": PROVIDER_CONCESSION_MAX_DAYS,
                        "concession_default_days": PROVIDER_CONCESSION_DEFAULT_DAYS,
                        "created_at": now,
                        "created_by": operator_id,
                    },
                )
                await write_record(
                    deps,
                    bootstrap_context,
                    transaction,
                    "organization_created",
                    {
                        "organization_id": str(organization_id),
                        "code": checked.code,
                        "name": checked.name,
                        "kind": "provider",
                        "concession_max_days": PROVIDER_CONCESSION_MAX_DAYS,
                        "concession_default_days": PROVIDER_CONCESSION_DEFAULT_DAYS,
                        "created_by": str(operator_id),
                    },
                )
                issued = await create_invited_user(
                    deps,
                    transaction,
                    email=checked.operator_email,
                    display_name=checked.operator_display_name,
                    professional_license=None,
                    assignments=(
                        PlannedAssignment(
                            Role.PLATFORM_OPERATOR,
                            ScopeRef(ScopeLevel.ORGANIZATION, organization_id),
                        ),
                    ),
                    now=now,
                    user_id=operator_id,
                )
        except sa_exc.IntegrityError as error:
            if _code_taken(error, "organization_single_provider"):
                raise ProviderAlreadyExists from None
            if _code_taken(error, "organization_code_unique"):
                raise IdentityRejected(IdentityRejection.CODE_TAKEN, field="/code") from None
            if _code_taken(error, "user_account_email_unique"):
                raise IdentityRejected(
                    IdentityRejection.EMAIL_UNAVAILABLE, field="/email"
                ) from None
            raise
        invitation = await deliver(
            deps,
            self._senders,
            bootstrap_context,
            issued,
            email=checked.operator_email,
            link_base=self._link_base,
            disclose=True,
        )
        return ProviderGenesisResult(organization_id, operator_id, invitation)

    async def first_operator(self, provider_context: ScopeContext) -> FirstOperator | None:
        """El primer ``platform_operator`` de la proveedora (``bootstrap --resume``)."""
        if provider_context.organization_id != self._deps.provider_organization_id:
            raise PermissionError("el primer operador se busca en la organización proveedora")
        rows = await self._deps.database.read(provider_context, _FIRST_OPERATOR)
        if not rows:
            return None
        row = rows[0]
        return FirstOperator(as_uuid(row.user_id), row.display_name, row.email, row.status)

    async def reissue_operator_invitation(
        self, bootstrap_context: ScopeContext, operator: FirstOperator
    ) -> InvitationOutcome | None:
        """``bootstrap --resume``: invitación nueva del primer operador si sigue ``invited``.

        La anterior queda cancelada (``issue_invitation``); si ya activó su cuenta, ``None``.
        """
        deps = self._deps
        _require_bootstrap_context(deps, bootstrap_context)
        if operator.user_id != bootstrap_context.actor.id:
            raise PermissionError("la invitación es del operador del contexto de arranque")
        if operator.status != "invited":
            return None
        async with deps.database.transaction(bootstrap_context) as transaction:
            issued = await issue_invitation(deps, transaction, operator.user_id, deps.clock.now())
        return await deliver(
            deps,
            self._senders,
            bootstrap_context,
            issued,
            email=operator.email,
            link_base=self._link_base,
            disclose=True,
        )


def _require_bootstrap_context(deps: IdentityDependencies, context: ScopeContext) -> None:
    if (
        not isinstance(context, ScopeContext)
        or context.origin is not ContextOrigin.ADMIN_COMMAND
        or context.actor.kind is not ActorKind.OPERATOR
        or context.organization_id != deps.provider_organization_id
        or context.allowed_scopes
    ):
        raise PermissionError("la proveedora solo nace con el contexto de bootstrap")
