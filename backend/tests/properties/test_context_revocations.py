"""PR-NUC-50: las revocaciones se reflejan en la petición siguiente (TASK-125; PBT-06; BR-NUC-18).

"Para cualquier secuencia generada de revocaciones (retirar rol, revocar concesión, cerrar sesión,
desactivar usuario, suspender organización), la primera ``build_context`` posterior refleja el
cambio: nunca existe un contexto con un permiso o alcance que ya no corresponde"
(``nfr-design-patterns.md``, LC-NUC-04).

Máquina de estados contra PostgreSQL 16 real, como ``vigia_app``, con los constructores reales
(``ScopeContexts`` sobre ``PostgresContextStore``). Cada ejemplo crea una organización cliente
con dos plantas y una zona por planta, dos personas del cliente y un instalador del proveedor.
Los comandos asignan y retiran roles, abren y cierran sesiones, conceden y revocan concesiones,
desactivan y reactivan personas, y suspenden y reactivan la organización (las revocaciones se
escriben directamente en la base, sin pasar por los cierres de sesión que las acompañan en
producción: el contexto no puede depender de ellos).

**Invariante tras cada comando**: para cada sesión abierta (y, la del instalador, con cada
concesión), ``context_from_session`` da exactamente lo que dice el modelo: sin contexto, o con los
``allowed_scopes`` vigentes; cada sesión ya cerrada da ``session_invalid``; y ``authorize``
concede exactamente las claves del modelo sobre cada planta y zona del cliente.
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
    precondition,
    rule,
    run_state_machine_as_test,
)

from tests.authz_support import CLIENT_ROLES, AuthzEnvironment, Site, authz_environment
from tests.conftest import _seeds_for_profile
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.identity.auth.sessions import SessionCookie
from vigia_platform.identity.authz.authorize import Resource, decide
from vigia_platform.identity.authz.context import ContextUnavailable, ContextUnavailableReason
from vigia_platform.identity.authz.matrix import MATRIX, PermissionKey
from vigia_platform.shared.context import AllowedScope, Role, ScopeLevel

pytestmark = pytest.mark.integration

STEPS_PER_EXAMPLE = 12
PEOPLE = ("ana", "beto")
PROBE_KEYS = (
    PermissionKey.FINDINGS_READ,
    PermissionKey.COVERAGE_READ,
    PermissionKey.COMMISSIONING_RUN,
    PermissionKey.USERS_MANAGE,
    PermissionKey.LIVE_VIEW_OPEN,
)
"""Claves con columnas distintas: bastan para ver que el contexto concede lo que debe."""


@pytest.fixture(scope="module")
def environment(postgres_endpoint: PostgresEndpoint) -> Iterator[AuthzEnvironment]:
    with authz_environment(postgres_endpoint, "context_revocations") as env:
        RevocationMachine.environment = env
        yield env


@dataclass
class ModelPerson:
    user_id: uuid.UUID
    active: bool = True
    assignments: dict[uuid.UUID, AllowedScope] = field(default_factory=dict)
    sessions: list[SessionCookie] = field(default_factory=list)
    closed: list[SessionCookie] = field(default_factory=list)


@dataclass
class ModelConcession:
    concession_id: uuid.UUID
    scope: AllowedScope
    active: bool = True


class RevocationMachine(RuleBasedStateMachine):
    environment: AuthzEnvironment

    def __init__(self) -> None:
        super().__init__()
        self.env = self.environment
        self.site: Site
        self.people: dict[str, ModelPerson] = {}
        self.organization_active = True
        self.installer = ModelPerson(uuid.uuid4())
        self.concessions: list[ModelConcession] = []

    @initialize()
    def setup(self) -> None:
        self.site = self.env.add_site(plants=2, zones_per_plant=1)
        for name in PEOPLE:
            self.people[name] = ModelPerson(self.env.add_user(self.site.organization_id))
        self.installer = ModelPerson(self.env.add_provider_user())
        self.installer.sessions.append(
            self.env.open_session(self.env.provider_organization_id, self.installer.user_id)
        )

    # --- Destinos ----------------------------------------------------------------------------

    def targets(self) -> list[tuple[ScopeLevel, uuid.UUID]]:
        result = [(ScopeLevel.ORGANIZATION, self.site.organization_id)]
        for plant, zones in self.site.plants.items():
            result.append((ScopeLevel.PLANT, plant))
            result += [(ScopeLevel.ZONE, zone) for zone in zones]
        return result

    # --- Comandos ------------------------------------------------------------------------------

    @rule(person=st.sampled_from(PEOPLE), role=st.sampled_from(CLIENT_ROLES), data=st.data())
    def assign(self, person: str, role: Role, data: st.DataObject) -> None:
        level, scope_id = data.draw(st.sampled_from(self.targets()))
        model = self.people[person]
        assignment = self.env.assign(
            self.site.organization_id, model.user_id, role, level, scope_id
        )
        model.assignments[assignment] = AllowedScope(level, scope_id, role)

    @precondition(lambda self: any(p.assignments for p in self.people.values()))
    @rule(data=st.data())
    def remove_role(self, data: st.DataObject) -> None:
        owners = [p for p in self.people.values() if p.assignments]
        model = data.draw(st.sampled_from(owners))
        assignment = data.draw(st.sampled_from(sorted(model.assignments)))
        self.env.remove_assignment(assignment)
        del model.assignments[assignment]

    @rule(person=st.sampled_from(PEOPLE))
    def open_session(self, person: str) -> None:
        model = self.people[person]
        model.sessions.append(self.env.open_session(self.site.organization_id, model.user_id))

    @precondition(lambda self: any(p.sessions for p in self.people.values()))
    @rule(data=st.data())
    def close_session(self, data: st.DataObject) -> None:
        owners = [p for p in self.people.values() if p.sessions]
        model = data.draw(st.sampled_from(owners))
        cookie = data.draw(st.sampled_from(model.sessions))
        self.env.close_session(cookie)
        model.sessions.remove(cookie)
        model.closed.append(cookie)

    @rule(person=st.sampled_from(PEOPLE))
    def deactivate(self, person: str) -> None:
        self.env.set_user_status(self.people[person].user_id, "deactivated")
        self.people[person].active = False

    @rule(person=st.sampled_from(PEOPLE))
    def reactivate(self, person: str) -> None:
        self.env.set_user_status(self.people[person].user_id, "active")
        self.people[person].active = True

    @rule()
    def suspend_organization(self) -> None:
        self.env.set_organization_status(self.site.organization_id, "suspended")
        self.organization_active = False

    @rule()
    def reactivate_organization(self) -> None:
        self.env.set_organization_status(self.site.organization_id, "active")
        self.organization_active = True

    # nuc_0009 (TASK-127): sobre un cliente suspendido no se concede (la base lo rechaza).
    @precondition(lambda self: self.organization_active)
    @rule(data=st.data())
    def grant_concession(self, data: st.DataObject) -> None:
        level, scope_id = data.draw(
            st.sampled_from([t for t in self.targets() if t[0] is not ScopeLevel.ZONE])
        )
        concession_id = self.env.add_concession(
            self.site.organization_id, self.installer.user_id, level=level, scope_id=scope_id
        )
        self.concessions.append(
            ModelConcession(concession_id, AllowedScope(level, scope_id, Role.PROVIDER_INSTALLER))
        )

    @precondition(lambda self: any(c.active for c in self.concessions))
    @rule(data=st.data())
    def revoke_concession(self, data: st.DataObject) -> None:
        concession = data.draw(st.sampled_from([c for c in self.concessions if c.active]))
        self.env.revoke_concession(concession.concession_id, self.env.now())
        concession.active = False

    # --- Invariante ----------------------------------------------------------------------------

    def resources(self) -> list[Resource]:
        organization = self.site.organization_id
        result = [Resource.organization(organization)]
        for plant, zones in self.site.plants.items():
            result.append(Resource.plant(organization, plant))
            result += [Resource.zone(organization, plant, zone) for zone in zones]
        return result

    def check_grants(self, context: object, expected: set[AllowedScope]) -> None:
        provider = self.env.provider_organization_id
        for key in PROBE_KEYS:
            for resource in self.resources():
                granted = decide(
                    context,  # type: ignore[arg-type]
                    key,
                    resource,
                    provider_organization_id=provider,
                ).granted
                oracle = any(
                    key in MATRIX[scope.role]
                    and scope.covers(resource.organization_id, resource.plant_id, resource.zone_id)
                    for scope in expected
                )
                assert granted == oracle, (key, resource, expected)

    @invariant()
    def contexts_match_the_model(self) -> None:
        if not self.people:
            return
        for model in self.people.values():
            # Una sesión cerrada no vuelve a dar contexto, aunque la persona y la organización
            # sigan activas y conserven sus asignaciones.
            for cookie in model.closed:
                with pytest.raises(ContextUnavailable) as closed:
                    self.env.run(self.env.contexts.context_from_session(cookie))
                assert closed.value.reason is ContextUnavailableReason.SESSION_INVALID
            for cookie in model.sessions:
                usable = model.active and self.organization_active
                if not usable:
                    with pytest.raises(ContextUnavailable):
                        self.env.run(self.env.contexts.context_from_session(cookie))
                    continue
                context = self.env.run(self.env.contexts.context_from_session(cookie)).context
                expected = set(model.assignments.values())
                assert set(context.allowed_scopes) == expected
                self.check_grants(context, expected)
        (cookie,) = self.installer.sessions
        for concession in self.concessions:
            if not (concession.active and self.organization_active):
                with pytest.raises(ContextUnavailable):
                    self.env.run(
                        self.env.contexts.context_from_session(
                            cookie, concession_id=concession.concession_id
                        )
                    )
                continue
            context = self.env.run(
                self.env.contexts.context_from_session(
                    cookie, concession_id=concession.concession_id
                )
            ).context
            assert context.allowed_scopes == (concession.scope,)
            self.check_grants(context, {concession.scope})


def test_pr_nuc_50_revocations_reach_the_next_context(environment: AuthzEnvironment) -> None:
    """PR-NUC-50 con cada semilla del perfil activo (``tests/conftest.py``)."""
    for value in _seeds_for_profile():
        seeded = hypothesis_seed(value)(RevocationMachine)
        run_state_machine_as_test(seeded, settings=settings(stateful_step_count=STEPS_PER_EXAMPLE))
