"""PR-NUC-39 y PR-NUC-40: máquina de estados de la jerarquía frente a un modelo (TASK-126; PBT-06).

``hierarchy_commands``: crear planta, zona y nodo, asignar y retirar un nodo de una zona, invitar,
activar, asignar y retirar un rol, desactivar y reactivar, con el ``HierarchyService``, el
``RoleService``, el ``UserService`` y el ``InvitationService`` reales contra PostgreSQL 16 como
``vigia_app``, actuando cada vez con la sesión real de un administrador activo elegido por el
generador (a veces sobre sí mismo). Solo la contraseña y el código del segundo factor son dobles
deterministas (sus módulos tienen sus propiedades, PR-NUC-08 y 09).

El **modelo** es independiente del código: plantas, zonas, nodos y su asignación vigente; cuentas
con correo, estado y asignaciones; las incompatibilidades del oráculo de BR-NUC-14 de
``test_role_incompatibility``; la regla de asignarse a sí mismo (BR-NUC-19) y la del último
administrador (BR-NUC-31). Cada comando se compara con el modelo: mismo éxito o el mismo código de
rechazo. Así "el comando que violaría la regla es el único rechazado" (PR-NUC-40) se comprueba
en cada paso: ningún comando que el modelo acepta se rechaza, y viceversa.

**Invariantes tras cada comando** (PR-NUC-39), leídos como superusuario sobre toda la base: toda
zona pertenece a una planta de su organización; todo nodo, a una planta de su organización; toda
asignación de rol, a un alcance de la organización del usuario; ninguna zona tiene dos nodos
vigentes; ningún correo se repite en la plataforma. Y el estado de la organización de la máquina
coincide con el modelo (cuentas, asignaciones vigentes y nodo de cada zona). **PR-NUC-40**: la
organización cliente activa conserva al menos un ``administrator`` activo de nivel organización.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest
from hypothesis import seed as hypothesis_seed
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
    run_state_machine_as_test,
)

from tests.conftest import _seeds_for_profile
from tests.hierarchy_support import (
    GOOD_PASSWORD,
    HierarchyEnvironment,
    hierarchy_environment,
    new_code,
    new_email,
)
from tests.integration.conftest import PostgresEndpoint
from tests.properties.test_role_incompatibility import oracle_rejects
from tests.session_support import GOOD_CODE
from vigia_platform.identity.application.common import IdentityRejected, ScopeRef
from vigia_platform.identity.application.hierarchy import GenesisRequest, PlantSpec, ZoneSpec
from vigia_platform.identity.application.roles import CLIENT_ROLES, AssignmentRequest
from vigia_platform.identity.application.users import InviteRequest
from vigia_platform.identity.domain.privacy_notice import CURRENT_PRIVACY_NOTICE_VERSION
from vigia_platform.shared.context import Role, ScopeContext, ScopeLevel

pytestmark = pytest.mark.integration

STEPS_PER_EXAMPLE = 12
"""Cada paso son varias transacciones reales más la relectura de toda la organización."""
OK = "ok"


@dataclass
class ModelUser:
    email: str
    status: str
    assignments: dict[uuid.UUID, tuple[Role, ScopeRef]] = field(default_factory=dict)
    token: str | None = None


@dataclass(frozen=True)
class Foreign:
    """Correos que ya existen en otra organización (BR-NUC-05)."""

    emails: tuple[str, ...]


@pytest.fixture(scope="module")
def environment(postgres_endpoint: PostgresEndpoint) -> Iterator[HierarchyEnvironment]:
    with hierarchy_environment(postgres_endpoint, "hierarchy_stateful") as env:
        other = env.run(
            env.genesis().create_client_organization(
                env.operator_context(),
                GenesisRequest(
                    code=new_code("ORG"),
                    name="Otra organización",
                    plant=PlantSpec(new_code("PL"), "Planta", "CO", "us-east-1", "UTC"),
                    administrator_email=new_email("ajena"),
                    administrator_display_name="Administración ajena",
                ),
            )
        )
        (row,) = env.fetch(
            "SELECT email FROM identity.user_account WHERE user_id = $1",
            other.administrator_user_id,
        )
        HierarchyMachine.environment = env
        HierarchyMachine.foreign = Foreign((row["email"],))
        yield env


def _token(link: str | None) -> str:
    assert link is not None
    return link.split("#", 1)[1]


def _id(value: object) -> uuid.UUID:
    """asyncpg devuelve su propio tipo de UUID; los servicios exigen ``uuid.UUID``."""
    return uuid.UUID(str(value))


def _model_contains(outer: ScopeRef, inner: ScopeRef) -> bool:
    """Organización ⊇ planta ⊇ zona, escrito aparte de ``ScopeRef.contains``."""
    if outer.level is ScopeLevel.ORGANIZATION:
        return True
    if outer.level is ScopeLevel.PLANT:
        return inner.level is not ScopeLevel.ORGANIZATION and inner.plant_id == outer.scope_id
    return inner == outer


class HierarchyMachine(RuleBasedStateMachine):
    environment: HierarchyEnvironment
    foreign: Foreign

    def __init__(self) -> None:
        super().__init__()
        self.env = self.environment
        self.organization_id = uuid.uuid4()
        self.zone_plant: dict[uuid.UUID, uuid.UUID] = {}
        self.plants: list[uuid.UUID] = []
        self.node_plant: dict[uuid.UUID, uuid.UUID] = {}
        self.zone_node: dict[uuid.UUID, uuid.UUID | None] = {}
        self.users: dict[uuid.UUID, ModelUser] = {}

    # --- Ayudas -------------------------------------------------------------------------------

    def tick(self) -> None:
        self.env.advance(1)

    def run(self, awaitable: object) -> tuple[str, object]:
        """Ejecuta un comando: ``("ok", resultado)`` o ``(código de rechazo, None)``."""
        try:
            return OK, self.env.run(awaitable)
        except IdentityRejected as rejected:
            return rejected.code.value, None

    def org_scope(self) -> ScopeRef:
        return ScopeRef(ScopeLevel.ORGANIZATION, self.organization_id)

    def scopes(self) -> list[ScopeRef]:
        return [
            self.org_scope(),
            *(ScopeRef(ScopeLevel.PLANT, plant, plant) for plant in self.plants),
            *(ScopeRef(ScopeLevel.ZONE, zone, plant) for zone, plant in self.zone_plant.items()),
        ]

    def active_admins(self) -> set[uuid.UUID]:
        return {
            user_id
            for user_id, user in self.users.items()
            if user.status == "active"
            and (Role.ADMINISTRATOR, self.org_scope()) in user.assignments.values()
        }

    def actor(self, data: st.DataObject) -> tuple[uuid.UUID, ScopeContext]:
        # Orden de alta, no de UUID: el mismo índice elige lo mismo al reproducir un ejemplo.
        admins = self.active_admins()
        candidates = [user_id for user_id in self.users if user_id in admins]
        actor_id = data.draw(st.sampled_from(candidates), label="actor")
        return actor_id, self.env.session_context(self.organization_id, actor_id)

    def expected_assignment(
        self, actor_id: uuid.UUID, user_id: uuid.UUID, role: Role, scope: ScopeRef
    ) -> str:
        user = self.users[user_id]
        if user.status == "deactivated":
            return "user_state"
        if role not in CLIENT_ROLES:
            return "role_not_assignable"
        if actor_id == user_id and not any(
            held is role and _model_contains(held_scope, scope)
            for held, held_scope in user.assignments.values()
        ):
            return "self_assignment"
        if any(oracle_rejects(held, (role, scope)) for held in user.assignments.values()):
            return "role_incompatible"
        return OK

    def removes_last_admin(self, user_id: uuid.UUID, still_admin: bool) -> bool:
        admins = self.active_admins()
        return user_id in admins and not still_admin and not (admins - {user_id})

    # --- Inicio: génesis con su primer administrador ya activo ------------------------------

    @initialize()
    def genesis(self) -> None:
        env = self.env
        email = new_email("admin")
        result = env.run(
            env.genesis().create_client_organization(
                env.operator_context(),
                GenesisRequest(
                    code=new_code("ORG"),
                    name="Organización de la máquina",
                    plant=PlantSpec(new_code("PL"), "Planta inicial", "CO", "us-east-1", "UTC"),
                    administrator_email=email,
                    administrator_display_name="Administración sintética",
                ),
            )
        )
        self.organization_id = result.organization_id
        self.plants.append(result.plant_id)
        (assignment,) = env.fetch(
            "SELECT assignment_id FROM identity.role_assignment WHERE user_id = $1",
            result.administrator_user_id,
        )
        self.users[result.administrator_user_id] = ModelUser(
            email,
            "invited",
            {_id(assignment["assignment_id"]): (Role.ADMINISTRATOR, self.org_scope())},
            _token(result.invitation.link),
        )
        self.tick()
        env.run(
            env.invitations().accept_invitation(
                _token(result.invitation.link),
                GOOD_PASSWORD,
                CURRENT_PRIVACY_NOTICE_VERSION,
                GOOD_CODE,
            )
        )
        self.users[result.administrator_user_id].status = "active"
        self.users[result.administrator_user_id].token = None

    # --- Jerarquía ------------------------------------------------------------------------------

    @rule(data=st.data())
    def create_plant(self, data: st.DataObject) -> None:
        self.tick()
        _, actor = self.actor(data)
        outcome, plant = self.run(
            self.env.hierarchy().create_plant(
                actor, PlantSpec(new_code("PL"), "Planta", "CO", "us-east-1", "America/Bogota")
            )
        )
        assert outcome == OK
        self.plants.append(plant.plant_id)  # type: ignore[attr-defined]

    @rule(data=st.data())
    def create_zone(self, data: st.DataObject) -> None:
        self.tick()
        _, actor = self.actor(data)
        plant = data.draw(st.sampled_from(self.plants), label="plant")
        outcome, zone = self.run(
            self.env.hierarchy().create_zone(actor, plant, ZoneSpec(new_code("ZN"), "Zona"))
        )
        assert outcome == OK
        self.zone_plant[zone.zone_id] = plant  # type: ignore[attr-defined]
        self.zone_node[zone.zone_id] = None  # type: ignore[attr-defined]

    @rule(data=st.data())
    def declare_node(self, data: st.DataObject) -> None:
        self.tick()
        _, actor = self.actor(data)
        plant = data.draw(st.sampled_from(self.plants), label="plant")
        outcome, node = self.run(self.env.hierarchy().declare_node(actor, plant, new_code("ND")))
        assert outcome == OK
        self.node_plant[node.node_id] = plant  # type: ignore[attr-defined]

    @rule(data=st.data())
    def assign_node(self, data: st.DataObject) -> None:
        if not self.node_plant or not self.zone_plant:
            return
        self.tick()
        _, actor = self.actor(data)
        node = data.draw(st.sampled_from(list(self.node_plant)), label="node")
        zone = data.draw(st.sampled_from(list(self.zone_plant)), label="zone")
        if self.zone_plant[zone] != self.node_plant[node]:
            expected = "node_plant_mismatch"
        elif self.zone_node[zone] is not None:
            expected = "zone_has_node"
        else:
            expected = OK
        outcome, _ = self.run(self.env.hierarchy().assign_node_to_zone(actor, node, zone))
        assert outcome == expected
        if outcome == OK:
            self.zone_node[zone] = node

    @rule(data=st.data())
    def unassign_node(self, data: st.DataObject) -> None:
        if not self.zone_plant:
            return
        self.tick()
        _, actor = self.actor(data)
        zone = data.draw(st.sampled_from(list(self.zone_plant)), label="zone")
        expected = "zone_without_node" if self.zone_node[zone] is None else OK
        outcome, _ = self.run(self.env.hierarchy().unassign_node(actor, zone))
        assert outcome == expected
        self.zone_node[zone] = None

    # --- Cuentas --------------------------------------------------------------------------------

    @rule(data=st.data())
    def invite(self, data: st.DataObject) -> None:
        self.tick()
        _, actor = self.actor(data)
        known = list(
            dict.fromkeys([*(user.email for user in self.users.values()), *self.foreign.emails])
        )
        email = data.draw(
            st.one_of(
                st.builds(new_email),
                st.sampled_from(known),
                st.sampled_from(known).map(str.upper),
            ),
            label="email",
        )
        role = data.draw(st.sampled_from(sorted(CLIENT_ROLES)), label="role")
        scope = data.draw(st.sampled_from(self.scopes()), label="scope")
        expected = "email_unavailable" if email.strip().lower() in known else OK
        outcome, result = self.run(
            self.env.users().invite_user(
                actor,
                InviteRequest(
                    email=email,
                    display_name="Persona sintética",
                    assignments=(AssignmentRequest(role, scope.level, scope.scope_id),),
                ),
            )
        )
        assert outcome == expected
        if outcome == OK:
            (assignment,) = self.env.fetch(
                "SELECT assignment_id FROM identity.role_assignment WHERE user_id = $1",
                result.user_id,  # type: ignore[attr-defined]
            )
            self.users[result.user_id] = ModelUser(  # type: ignore[attr-defined]
                email.strip().lower(),
                "invited",
                {_id(assignment["assignment_id"]): (role, scope)},
                _token(result.link),  # type: ignore[attr-defined]
            )

    @rule(data=st.data())
    def activate(self, data: st.DataObject) -> None:
        pending = [user_id for user_id, user in self.users.items() if user.token is not None]
        if not pending:
            return
        self.tick()
        user_id = data.draw(st.sampled_from(pending), label="user")
        user = self.users[user_id]
        expected = OK if user.status == "invited" else "invitation_invalid"
        assert user.token is not None
        outcome, _ = self.run(
            self.env.invitations().accept_invitation(
                user.token, GOOD_PASSWORD, CURRENT_PRIVACY_NOTICE_VERSION, GOOD_CODE
            )
        )
        assert outcome == expected
        user.token = None
        if outcome == OK:
            user.status = "active"

    @rule(data=st.data())
    def assign_role(self, data: st.DataObject) -> None:
        self.tick()
        actor_id, actor = self.actor(data)
        user_id = data.draw(st.sampled_from(list(self.users)), label="user")
        role = data.draw(st.sampled_from(sorted(Role)), label="role")
        scope = data.draw(st.sampled_from(self.scopes()), label="scope")
        expected = self.expected_assignment(actor_id, user_id, role, scope)
        outcome, view = self.run(
            self.env.roles().assign_role(
                actor, user_id, AssignmentRequest(role, scope.level, scope.scope_id)
            )
        )
        assert outcome == expected, (role, scope.level)
        if outcome == OK:
            self.users[user_id].assignments[view.assignment_id] = (role, scope)  # type: ignore[attr-defined]

    @rule(data=st.data())
    def remove_role(self, data: st.DataObject) -> None:
        holders = [user_id for user_id, user in self.users.items() if user.assignments]
        if not holders:
            return
        self.tick()
        _, actor = self.actor(data)
        user_id = data.draw(st.sampled_from(holders), label="user")
        user = self.users[user_id]
        assignment_id = data.draw(st.sampled_from(list(user.assignments)), label="assignment")
        role, scope = user.assignments[assignment_id]
        still_admin = any(
            other == (Role.ADMINISTRATOR, self.org_scope())
            for key, other in user.assignments.items()
            if key != assignment_id
        )
        is_org_admin = (role, scope) == (Role.ADMINISTRATOR, self.org_scope())
        expected = (
            "last_administrator"
            if is_org_admin and self.removes_last_admin(user_id, still_admin)
            else OK
        )
        outcome, _ = self.run(self.env.roles().remove_role(actor, user_id, assignment_id))
        assert outcome == expected
        if outcome == OK:
            del user.assignments[assignment_id]

    @rule(data=st.data())
    def deactivate(self, data: st.DataObject) -> None:
        self.tick()
        _, actor = self.actor(data)
        user_id = data.draw(st.sampled_from(list(self.users)), label="user")
        user = self.users[user_id]
        if user.status == "deactivated":
            expected = "user_state"
        elif self.removes_last_admin(user_id, still_admin=False):
            expected = "last_administrator"
        else:
            expected = OK
        outcome, _ = self.run(self.env.users().deactivate_user(actor, user_id))
        assert outcome == expected
        if outcome == OK:
            user.status = "deactivated"
            # Su invitación pendiente queda cancelada: el token se conserva para probar que ya
            # no vale (``activate`` espera ``invitation_invalid`` para una cuenta desactivada).
            user.assignments.clear()

    @rule(data=st.data())
    def reactivate(self, data: st.DataObject) -> None:
        candidates = [u for u, user in self.users.items() if user.status == "deactivated"]
        if not candidates:
            return
        self.tick()
        _, actor = self.actor(data)
        user_id = data.draw(st.sampled_from(candidates), label="user")
        role = data.draw(st.sampled_from(sorted(CLIENT_ROLES)), label="role")
        scope = data.draw(st.sampled_from(self.scopes()), label="scope")
        outcome, result = self.run(
            self.env.users().reactivate_user(
                actor, user_id, (AssignmentRequest(role, scope.level, scope.scope_id),)
            )
        )
        assert outcome == OK
        rows = self.env.fetch(
            "SELECT assignment_id FROM identity.role_assignment WHERE user_id = $1"
            " AND removed_at IS NULL",
            user_id,
        )
        user = self.users[user_id]
        user.status = "invited"
        user.assignments = {_id(row["assignment_id"]): (role, scope) for row in rows}
        user.token = _token(result.link)  # type: ignore[attr-defined]

    # --- Invariantes ------------------------------------------------------------------------

    @invariant()
    def pr_nuc_39_whole_database(self) -> None:
        env = self.env
        checks = {
            "zona fuera de su planta u organización": (
                "SELECT count(*) FROM identity.zone z LEFT JOIN identity.plant p"
                " ON p.plant_id = z.plant_id AND p.organization_id = z.organization_id"
                " WHERE p.plant_id IS NULL"
            ),
            "nodo fuera de su planta u organización": (
                "SELECT count(*) FROM identity.node_identity n LEFT JOIN identity.plant p"
                " ON p.plant_id = n.plant_id AND p.organization_id = n.organization_id"
                " WHERE p.plant_id IS NULL"
            ),
            "asignación fuera de la organización del usuario": (
                "SELECT count(*) FROM identity.role_assignment r"
                " JOIN identity.user_account u ON u.user_id = r.user_id"
                " LEFT JOIN identity.plant p ON r.scope_level = 'plant'"
                " AND p.plant_id = r.scope_id AND p.organization_id = u.organization_id"
                " LEFT JOIN identity.zone z ON r.scope_level = 'zone'"
                " AND z.zone_id = r.scope_id AND z.organization_id = u.organization_id"
                " WHERE r.organization_id <> u.organization_id"
                " OR (r.scope_level = 'organization' AND r.scope_id <> u.organization_id)"
                " OR (r.scope_level = 'plant' AND p.plant_id IS NULL)"
                " OR (r.scope_level = 'zone' AND z.zone_id IS NULL)"
            ),
            "zona con dos nodos vigentes": (
                "SELECT count(*) FROM (SELECT zone_id FROM identity.zone_node_assignment"
                " WHERE unassigned_at IS NULL GROUP BY zone_id HAVING count(*) > 1) AS twice"
            ),
            "correo repetido": (
                "SELECT count(*) - count(DISTINCT lower(email)) FROM identity.user_account"
            ),
        }
        for label, sql in checks.items():
            assert env.fetch(sql)[0][0] == 0, label

    @invariant()
    def database_matches_the_model(self) -> None:
        env = self.env
        accounts = {
            row["user_id"]: row["status"]
            for row in env.fetch(
                "SELECT user_id, status FROM identity.user_account WHERE organization_id = $1",
                self.organization_id,
            )
        }
        assert accounts == {user_id: user.status for user_id, user in self.users.items()}
        current = {
            row["assignment_id"]
            for row in env.fetch(
                "SELECT assignment_id FROM identity.role_assignment WHERE organization_id = $1"
                " AND removed_at IS NULL",
                self.organization_id,
            )
        }
        assert current == {key for user in self.users.values() for key in user.assignments}
        nodes = {
            row["zone_id"]: row["node_id"]
            for row in env.fetch(
                "SELECT zone_id, node_id FROM identity.zone_node_assignment"
                " WHERE organization_id = $1 AND unassigned_at IS NULL",
                self.organization_id,
            )
        }
        assert nodes == {zone: node for zone, node in self.zone_node.items() if node is not None}

    @invariant()
    def pr_nuc_40_an_active_administrator_remains(self) -> None:
        assert self.active_admins()
        (count,) = self.env.fetch(
            "SELECT count(DISTINCT r.user_id) FROM identity.role_assignment r"
            " JOIN identity.user_account u ON u.user_id = r.user_id"
            " JOIN identity.organization o ON o.organization_id = r.organization_id"
            " WHERE r.organization_id = $1 AND r.role = 'administrator'"
            " AND r.scope_level = 'organization' AND r.removed_at IS NULL"
            " AND u.status = 'active' AND o.status = 'active' AND o.kind = 'client'",
            self.organization_id,
        )
        assert count[0] == len(self.active_admins()) >= 1


def test_hierarchy_machine_matches_the_model(environment: HierarchyEnvironment) -> None:
    """PR-NUC-39 y PR-NUC-40 con cada semilla del perfil activo (``tests/conftest.py``)."""
    for value in _seeds_for_profile():
        seeded = hypothesis_seed(value)(HierarchyMachine)
        run_state_machine_as_test(seeded, settings=settings(stateful_step_count=STEPS_PER_EXAMPLE))
