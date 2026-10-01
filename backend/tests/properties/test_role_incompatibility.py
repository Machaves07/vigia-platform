"""PR-NUC-04: la verificación de incompatibilidad es simétrica (TASK-126; BR-NUC-11, 14, 19).

"``asignar(a)`` y luego ``asignar(b)`` se rechaza si y solo si ``asignar(b)`` y luego ``asignar(a)``
se rechaza" (pares de asignaciones). Tres capas:

1. **Pura**: sobre una jerarquía generada (organización, plantas, zonas), para cualquier par de
   asignaciones la decisión de ``find_conflict`` es la misma en los dos órdenes, y coincide con
   un **oráculo** escrito aparte desde el texto de BR-NUC-14: (1) ``line_manager`` sobre la zona
   Z frente a ``coordinator_sst``, ``plant_manager`` o ``administrator`` sobre un alcance que
   contiene a Z; (2) ``administrator`` frente a ``coordinator_sst`` o ``plant_manager`` sobre
   alcances que se solapan; más la repetición exacta y el ``line_manager`` de planta u
   organización, que la implementación trata con la misma relación de solapamiento.
2. **Con la base** (``RoleService`` real, PostgreSQL 16 como ``vigia_app``): para pares generados,
   asignar en un orden y en el otro a dos usuarios nuevos da el mismo resultado; el rechazo nombra
   la asignación en conflicto, deja ``role_assignment_rejected`` en la auditoría y no escribe
   nada.
3. **Atomicidad**: dos asignaciones incompatibles concurrentes al mismo usuario (en los dos
   órdenes de llegada) dejan exactamente una.

Más los bordes de BR-NUC-11 (rol de la proveedora en un cliente) y BR-NUC-19 (asignarse un rol que
no se tiene, o ampliar el alcance del que se tiene).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.hierarchy_support import (
    GOOD_PASSWORD,
    HierarchyEnvironment,
    hierarchy_environment,
    new_code,
    new_email,
)
from tests.integration.conftest import PostgresEndpoint
from tests.session_support import GOOD_CODE
from vigia_platform.identity.application.common import (
    IdentityRejected,
    IdentityRejection,
    ScopeRef,
)
from vigia_platform.identity.application.hierarchy import GenesisRequest, PlantSpec, ZoneSpec
from vigia_platform.identity.application.roles import (
    CLIENT_ROLES,
    INCOMPATIBLE_ROLES,
    Assignment,
    AssignmentRequest,
    find_conflict,
    role_assignable,
    self_assignment_allowed,
)
from vigia_platform.identity.domain.privacy_notice import CURRENT_PRIVACY_NOTICE_VERSION
from vigia_platform.shared.context import Role, ScopeContext, ScopeLevel

ORGANIZATION = uuid.UUID("00000000-0000-4000-8000-0000000000a0")
PLANTS = (uuid.UUID(int=0xB1), uuid.UUID(int=0xB2))
ZONES = {
    uuid.UUID(int=0xC11): PLANTS[0],
    uuid.UUID(int=0xC12): PLANTS[0],
    uuid.UUID(int=0xC21): PLANTS[1],
}

_INCOMPATIBLE_WITH_LINE = frozenset({Role.COORDINATOR_SST, Role.PLANT_MANAGER, Role.ADMINISTRATOR})
_INCOMPATIBLE_WITH_ADMIN = frozenset({Role.COORDINATOR_SST, Role.PLANT_MANAGER})


def _all_scopes() -> list[ScopeRef]:
    return [
        ScopeRef(ScopeLevel.ORGANIZATION, ORGANIZATION),
        *(ScopeRef(ScopeLevel.PLANT, plant, plant) for plant in PLANTS),
        *(ScopeRef(ScopeLevel.ZONE, zone, plant) for zone, plant in ZONES.items()),
    ]


scopes = st.sampled_from(_all_scopes())
client_roles = st.sampled_from(sorted(CLIENT_ROLES))
pairs = st.tuples(client_roles, scopes, client_roles, scopes)


# --- Oráculo independiente (texto de BR-NUC-14) -------------------------------------------------


def _zones_under(scope: ScopeRef) -> set[uuid.UUID]:
    """Las zonas que el alcance contiene, contadas desde la tabla fija de la prueba."""
    if scope.level is ScopeLevel.ORGANIZATION:
        return set(ZONES)
    if scope.level is ScopeLevel.PLANT:
        return {zone for zone, plant in ZONES.items() if plant == scope.scope_id}
    return {scope.scope_id}


def _covers(outer: ScopeRef, inner: ScopeRef) -> bool:
    if outer.level is ScopeLevel.ORGANIZATION:
        return True
    if inner.level is ScopeLevel.ORGANIZATION:
        return False
    if outer.level is ScopeLevel.PLANT:
        return inner.plant_id == outer.scope_id
    return inner.level is ScopeLevel.ZONE and inner.scope_id == outer.scope_id


def oracle_rejects(first: tuple[Role, ScopeRef], second: tuple[Role, ScopeRef]) -> bool:
    (role_a, scope_a), (role_b, scope_b) = first, second
    if role_a is role_b and scope_a == scope_b:
        return True
    for (line, line_scope), (other, other_scope) in (
        ((role_a, scope_a), (role_b, scope_b)),
        ((role_b, scope_b), (role_a, scope_a)),
    ):
        if line is Role.LINE_MANAGER and other in _INCOMPATIBLE_WITH_LINE:
            if line_scope.level is ScopeLevel.ZONE:
                # Regla (1) literal: cualquier alcance que contenga a la zona.
                return _covers(other_scope, line_scope)
            # Mando de línea de planta u organización: alcances que se solapan.
            return _covers(other_scope, line_scope) or _covers(line_scope, other_scope)
        if line is Role.ADMINISTRATOR and other in _INCOMPATIBLE_WITH_ADMIN:
            # Regla (2): alcances que se solapan (comparten alguna zona o uno es la organización).
            return (
                _covers(other_scope, line_scope)
                or _covers(line_scope, other_scope)
                or bool(_zones_under(other_scope) & _zones_under(line_scope))
            )
    return False


def _rejects(first: tuple[Role, ScopeRef], second: tuple[Role, ScopeRef]) -> bool:
    existing = [Assignment(uuid.UUID(int=1), first[0], first[1])]
    return find_conflict(existing, second[0], second[1]) is not None


@given(pairs)
def test_incompatibility_is_symmetric_and_matches_the_oracle(
    pair: tuple[Role, ScopeRef, Role, ScopeRef],
) -> None:
    role_a, scope_a, role_b, scope_b = pair
    a, b = (role_a, scope_a), (role_b, scope_b)
    assert _rejects(a, b) == _rejects(b, a)
    assert _rejects(a, b) == oracle_rejects(a, b)


@given(st.lists(st.tuples(client_roles, scopes), min_size=1, max_size=6), client_roles, scopes)
def test_conflict_named_is_a_current_incompatible_assignment(
    held: list[tuple[Role, ScopeRef]], role: Role, scope: ScopeRef
) -> None:
    existing = [
        Assignment(uuid.UUID(int=index + 1), held_role, held_scope)
        for index, (held_role, held_scope) in enumerate(held)
    ]
    conflict = find_conflict(existing, role, scope)
    expected = [a for a in existing if oracle_rejects((a.role, a.scope), (role, scope))]
    if not expected:
        assert conflict is None
    else:
        # El conflicto nombrado es el primero por identificador (determinista).
        assert conflict is not None and conflict.assignment == expected[0]
        assert conflict.duplicate == (
            conflict.assignment.role is role and conflict.assignment.scope == scope
        )


def test_br_nuc_14_examples() -> None:
    zone, plant = next(iter(ZONES.items()))
    other_plant = PLANTS[1]
    z = ScopeRef(ScopeLevel.ZONE, zone, plant)
    p = ScopeRef(ScopeLevel.PLANT, plant, plant)
    q = ScopeRef(ScopeLevel.PLANT, other_plant, other_plant)
    o = ScopeRef(ScopeLevel.ORGANIZATION, ORGANIZATION)
    line = (Role.LINE_MANAGER, z)
    for blocked in (Role.COORDINATOR_SST, Role.PLANT_MANAGER, Role.ADMINISTRATOR):
        for container in (z, p, o):
            assert _rejects(line, (blocked, container)), (blocked, container)
        assert not _rejects(line, (blocked, q)), blocked
    assert not _rejects(line, (Role.COPASST, z))
    assert not _rejects(
        line, (Role.LINE_MANAGER, ScopeRef(ScopeLevel.ZONE, uuid.UUID(int=0xC12), plant))
    )
    assert _rejects((Role.ADMINISTRATOR, o), (Role.PLANT_MANAGER, q))
    assert _rejects((Role.ADMINISTRATOR, p), (Role.COORDINATOR_SST, z))
    assert not _rejects((Role.ADMINISTRATOR, p), (Role.COORDINATOR_SST, q))
    assert not _rejects((Role.COORDINATOR_SST, o), (Role.PLANT_MANAGER, o))
    assert _rejects((Role.COPASST, z), (Role.COPASST, z))  # repetición exacta
    assert len(INCOMPATIBLE_ROLES) == 5


def test_br_nuc_11_and_19_borders() -> None:
    for role in Role:
        assert role_assignable(role, "client") == (role in CLIENT_ROLES)
        assert role_assignable(role, "provider") == (role not in CLIENT_ROLES)
    zone, plant = next(iter(ZONES.items()))
    z = ScopeRef(ScopeLevel.ZONE, zone, plant)
    p = ScopeRef(ScopeLevel.PLANT, plant, plant)
    o = ScopeRef(ScopeLevel.ORGANIZATION, ORGANIZATION)
    held = [Assignment(uuid.UUID(int=1), Role.COPASST, p)]
    assert self_assignment_allowed(held, Role.COPASST, z)  # dentro de lo que tiene
    assert self_assignment_allowed(held, Role.COPASST, p)
    assert not self_assignment_allowed(held, Role.COPASST, o)  # ampliar el alcance
    assert not self_assignment_allowed(held, Role.COORDINATOR_SST, z)  # otro rol
    assert not self_assignment_allowed([], Role.COPASST, z)


# --- Con la base ----------------------------------------------------------------------------------


@dataclass
class Site:
    organization_id: uuid.UUID
    admin_id: uuid.UUID
    plants: tuple[uuid.UUID, uuid.UUID]
    zones: dict[uuid.UUID, uuid.UUID]
    """zona → planta"""


@pytest.fixture(scope="module")
def environment(postgres_endpoint: PostgresEndpoint) -> Iterator[tuple[HierarchyEnvironment, Site]]:
    with hierarchy_environment(postgres_endpoint, "role_incompatibility") as env:
        result = env.run(
            env.genesis().create_client_organization(
                env.operator_context(),
                GenesisRequest(
                    code=new_code("ORG"),
                    name="Organización sintética",
                    plant=PlantSpec(new_code("PL"), "Planta A", "CO", "us-east-1", "UTC"),
                    administrator_email=new_email("admin"),
                    administrator_display_name="Administración sintética",
                ),
            )
        )
        link = result.invitation.link
        assert link is not None
        env.run(
            env.invitations().accept_invitation(
                link.split("#", 1)[1], GOOD_PASSWORD, CURRENT_PRIVACY_NOTICE_VERSION, GOOD_CODE
            )
        )
        admin = env.session_context(result.organization_id, result.administrator_user_id)
        hierarchy = env.hierarchy()
        second = env.run(
            hierarchy.create_plant(
                admin, PlantSpec(new_code("PL"), "Planta B", "CO", "us-east-1", "UTC")
            )
        )
        zones: dict[uuid.UUID, uuid.UUID] = {}
        for plant_id in (result.plant_id, result.plant_id, second.plant_id):
            zone = env.run(hierarchy.create_zone(admin, plant_id, ZoneSpec(new_code("ZN"), "Z")))
            zones[zone.zone_id] = plant_id
        yield (
            env,
            Site(
                result.organization_id,
                result.administrator_user_id,
                (result.plant_id, second.plant_id),
                zones,
            ),
        )


def _site_scopes(site: Site) -> list[ScopeRef]:
    return [
        ScopeRef(ScopeLevel.ORGANIZATION, site.organization_id),
        *(ScopeRef(ScopeLevel.PLANT, plant, plant) for plant in site.plants),
        *(ScopeRef(ScopeLevel.ZONE, zone, plant) for zone, plant in site.zones.items()),
    ]


def _request(role: Role, scope: ScopeRef) -> AssignmentRequest:
    return AssignmentRequest(role, scope.level, scope.scope_id)


def _new_user(env: HierarchyEnvironment, site: Site) -> uuid.UUID:
    return env.authz.sessions.add_user(site.organization_id).user_id


def _attempt(
    env: HierarchyEnvironment, admin: ScopeContext, user: uuid.UUID, role: Role, scope: ScopeRef
) -> IdentityRejected | uuid.UUID:
    try:
        view = env.run(env.roles().assign_role(admin, user, _request(role, scope)))
    except IdentityRejected as rejected:
        return rejected
    return view.assignment_id


@pytest.mark.integration
def test_assignment_order_does_not_change_the_decision_in_the_database(
    environment: tuple[HierarchyEnvironment, Site],
) -> None:
    env, site = environment
    site_scopes = _site_scopes(site)

    @given(client_roles, st.sampled_from(site_scopes), client_roles, st.sampled_from(site_scopes))
    def check(role_a: Role, scope_a: ScopeRef, role_b: Role, scope_b: ScopeRef) -> None:
        admin = env.session_context(site.organization_id, site.admin_id)
        outcomes = []
        for first, second in (
            ((role_a, scope_a), (role_b, scope_b)),
            ((role_b, scope_b), (role_a, scope_a)),
        ):
            user = _new_user(env, site)
            placed = _attempt(env, admin, user, *first)
            assert isinstance(placed, uuid.UUID)
            outcome = _attempt(env, admin, user, *second)
            if isinstance(outcome, IdentityRejected):
                assert outcome.code is IdentityRejection.ROLE_INCOMPATIBLE
                assert outcome.conflict_assignment_id == placed
                rows = env.fetch(
                    "SELECT role FROM identity.role_assignment WHERE user_id = $1"
                    " AND removed_at IS NULL",
                    user,
                )
                assert [row["role"] for row in rows] == [first[0].value]
            outcomes.append(isinstance(outcome, IdentityRejected))
        assert outcomes[0] == outcomes[1]

    check()


@pytest.mark.integration
def test_rejection_is_audited_with_the_conflicting_assignment(
    environment: tuple[HierarchyEnvironment, Site],
) -> None:
    env, site = environment
    admin = env.session_context(site.organization_id, site.admin_id)
    zone, plant = next(iter(site.zones.items()))
    user = _new_user(env, site)
    placed = _attempt(env, admin, user, Role.LINE_MANAGER, ScopeRef(ScopeLevel.ZONE, zone, plant))
    rejected = _attempt(
        env, admin, user, Role.COORDINATOR_SST, ScopeRef(ScopeLevel.PLANT, plant, plant)
    )
    assert isinstance(rejected, IdentityRejected)
    rows = env.fetch(
        "SELECT outcome, convert_from(filters, 'UTF8') AS filters, actor_role_in_use"
        " FROM shared.audit_entry WHERE organization_id = $1"
        " AND operation = 'role_assignment_rejected' AND resource_id = $2",
        site.organization_id,
        user,
    )
    assert len(rows) == 1
    assert rows[0]["outcome"] == "denied"
    assert str(placed) in rows[0]["filters"] and "role_incompatible" in rows[0]["filters"]
    assert rows[0]["actor_role_in_use"] == "administrator"


@pytest.mark.integration
def test_provider_roles_and_self_escalation_are_rejected(
    environment: tuple[HierarchyEnvironment, Site],
) -> None:
    env, site = environment
    admin = env.session_context(site.organization_id, site.admin_id)
    org_scope = ScopeRef(ScopeLevel.ORGANIZATION, site.organization_id)
    user = _new_user(env, site)
    for role in (Role.PROVIDER_INSTALLER, Role.PLATFORM_OPERATOR):
        outcome = _attempt(env, admin, user, role, org_scope)
        assert isinstance(outcome, IdentityRejected)
        assert outcome.code is IdentityRejection.ROLE_NOT_ASSIGNABLE
    # El administrador no se asigna a sí mismo un rol que no tiene (COPASST de toda la org.).
    outcome = _attempt(env, admin, site.admin_id, Role.COPASST, org_scope)
    assert isinstance(outcome, IdentityRejected)
    assert outcome.code is IdentityRejection.SELF_ASSIGNMENT
    # Sin roles.manage no hay asignación posible: como un recurso inexistente.
    from vigia_platform.identity.authz.authorize import ResourceNotFound

    plain = _new_user(env, site)
    env.authz.assign(site.organization_id, plain, Role.COPASST)
    with pytest.raises(ResourceNotFound):
        env.run(
            env.roles().assign_role(
                env.session_context(site.organization_id, plain),
                plain,
                _request(Role.COORDINATOR_SST, org_scope),
            )
        )


@pytest.mark.integration
@pytest.mark.parametrize("first_line", [True, False])
def test_concurrent_incompatible_assignments_leave_exactly_one(
    environment: tuple[HierarchyEnvironment, Site], first_line: bool
) -> None:
    env, site = environment
    zone, plant = next(iter(site.zones.items()))
    user = _new_user(env, site)
    line = _request(Role.LINE_MANAGER, ScopeRef(ScopeLevel.ZONE, zone, plant))
    coordinator = _request(Role.COORDINATOR_SST, ScopeRef(ScopeLevel.PLANT, plant, plant))
    requests = (line, coordinator) if first_line else (coordinator, line)
    contexts = [env.session_context(site.organization_id, site.admin_id) for _ in requests]

    async def both() -> list[object]:
        roles = env.roles()
        return await asyncio.gather(
            *(roles.assign_role(c, user, r) for c, r in zip(contexts, requests, strict=True)),
            return_exceptions=True,
        )

    results = env.run(both())
    rejected = [r for r in results if isinstance(r, IdentityRejected)]
    assert len(rejected) == 1 and rejected[0].code is IdentityRejection.ROLE_INCOMPATIBLE
    rows = env.fetch(
        "SELECT role FROM identity.role_assignment WHERE user_id = $1 AND removed_at IS NULL", user
    )
    assert len(rows) == 1
