"""TASK-583 (O-3; A-126; BR-LAZ-38 y 49; BR-APP-21, 33 y 53): la lista de responsables posibles de
la acción correctiva, ``GET /loop/findings/{finding_state_id}/assignees``.

Sobre PostgreSQL 16 real como ``vigia_app`` y la aplicación real (``actions_stack``):

- **Mismo conjunto** (propiedad): para todo mundo generado de usuarios (organización, estado,
  nombre) y asignaciones de rol (rol, alcance, vigente o retirada), un ``user_id`` está en la
  lista si y solo si ``assign`` lo acepta como responsable (su paso 5, ``ActionService._assignee``,
  en una transacción como la suya), y los dos coinciden con el modelo de A-126 escrito aquí.
- **A-126 de punta a punta**: ``POST .../actions`` y ``POST .../reopening`` aceptan a un jefe de
  línea o a un gerente de planta de la lista y responden ``not_found`` para un administrador, un
  COPASST, un desactivado, uno de otra planta o zona y uno de otra organización.
- **Alcance** (siete roles y tres alcances): solo ``coordinator_sst`` con alcance sobre la zona
  lee la lista; los demás, la concesión del proveedor, el operador de la plataforma, otra
  organización y fuera de alcance responden exactamente igual que un hallazgo inexistente
  (``not_found``).
- **Minimización**: por persona, exactamente ``user_id``, ``display_name`` y ``roles``; ningún
  parámetro de búsqueda (``invalid_request``); orden por ``display_name`` y ``user_id`` y
  paginación por clave de 1 a 200.
- **Inactivos fuera** (BR-LAZ-49): un desactivado o con el rol retirado deja la lista; si se
  envía igual, ``assign`` responde ``not_found``.

No hay garantía atómica nueva: la lista es una sola lectura y ``assign`` vuelve a comprobar al
responsable dentro de su transacción (la prueba de «desactivado entre la lista y la asignación»
lo muestra). Solo datos generados: nombres inventados, ningún dato real.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from tests.integration.conftest import PostgresEndpoint
from tests.loop_actions_support import ActionsStack, Who, actions_stack
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.identity.authz.context import with_unit
from vigia_platform.loop.adapters.http.assignees_routes import (
    decode_assignee_cursor,
    encode_assignee_cursor,
)
from vigia_platform.loop.application.actions import (
    ASSIGNEE_ROLES,
    ActionService,
    authorized_finding,
)
from vigia_platform.shared.context import ActorUnit, Role, ScopeContext, ScopeLevel

pytestmark = pytest.mark.integration

_NOT_FOUND: Final = (404, "not_found")
_INVALID: Final = (400, "invalid_request")
CLIENT_ROLES: Final = (
    Role.COORDINATOR_SST,
    Role.LINE_MANAGER,
    Role.PLANT_MANAGER,
    Role.ADMINISTRATOR,
    Role.COPASST,
)
"""Los cinco roles de una organización cliente (los otros dos solo existen en la proveedora)."""
NAMES: Final = ("Alba", "Bruno", "Celia", "Dario", "Elena", "Fabio")
"""Nombres inventados de una sola palabra (sin espacios: el orden no depende de la colación)."""
ITEM_FIELDS: Final = {"user_id", "display_name", "roles"}
APP_YAML: Final = Path(__file__).resolve().parents[2] / "openapi" / "app.yaml"


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[ActionsStack]:
    with actions_stack(postgres_endpoint, "loop_assignees_routes") as built:
        yield built


@pytest.fixture(scope="module")
def coordinator(stack: ActionsStack) -> Who:
    return stack.member(stack.world.a)


@dataclass(frozen=True)
class Place:
    """El hallazgo abierto de la organización A y los alcances que no cubren su zona."""

    finding: Any
    finding_state_id: uuid.UUID
    other_plant: uuid.UUID
    other_zone: uuid.UUID


@pytest.fixture(scope="module")
def place(stack: ActionsStack) -> Place:
    finding = stack.opened()
    return Place(
        finding,
        stack.finding_state_id(finding),
        stack.world.add_plant(stack.world.a),
        stack.world.a.site.other_zone_id,
    )


def _error(response: Any) -> tuple[int, str | None]:
    return response.status_code, response.json().get("code")


def _comparable(response: Any) -> tuple[int, Any]:
    body = response.json()
    return response.status_code, {k: v for k, v in body.items() if k != "correlation_id"}


def _rename(stack: ActionsStack, user: uuid.UUID, name: str) -> None:
    stack.execute(
        "UPDATE identity.user_account SET display_name = $2 WHERE user_id = $1", user, name
    )


def _listed(
    stack: ActionsStack, who: Who, subject: Any, *, limit: int = 200
) -> list[dict[str, Any]]:
    """Toda la lista, página a página (cada página comprobada)."""
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        response = stack.assignees(who, subject, params)
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {"items", "next_cursor"}
        assert len(body["items"]) <= limit
        items.extend(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return items


def _context(stack: ActionsStack, who: Who) -> ScopeContext:
    return stack.context(who)


def _accepted_by_assign(stack: ActionsStack, who: Who, fsid: uuid.UUID, user: uuid.UUID) -> bool:
    """El paso 5 de ``assign`` (``ActionService._assignee``) en una transacción como la suya: el
    oráculo de «``assign`` lo acepta como responsable»."""
    database = stack.database
    authorizer = stack.world.stack.authz.authorizer

    async def check() -> bool:
        authorized, current = await authorized_finding(
            database, authorizer, _context(stack, who), fsid
        )
        async with database.transaction(with_unit(authorized, ActorUnit.U04)) as transaction:
            try:
                await ActionService._assignee(transaction, current, user)
            except ResourceNotFound:
                return False
        return True

    result: bool = stack.run(check())
    return result


# --- el modelo de A-126 ---------------------------------------------------------------------------

SCOPES: Final = ("organization", "plant", "zone", "other_plant", "other_zone")
COVERING: Final = frozenset({"organization", "plant", "zone"})


@dataclass(frozen=True)
class Grant:
    role: Role
    scope: str
    removed: bool


@dataclass(frozen=True)
class Person:
    same_organization: bool
    status: str
    name: str
    grants: tuple[Grant, ...]

    def expected_roles(self) -> list[str] | None:
        """A-126: activo, de la organización y con un rol de la lista que cubra la zona."""
        if not self.same_organization or self.status != "active":
            return None
        roles = sorted(
            {
                g.role.value
                for g in self.grants
                if not g.removed and g.role in ASSIGNEE_ROLES and g.scope in COVERING
            }
        )
        return roles or None


grants = st.builds(
    Grant,
    role=st.sampled_from(CLIENT_ROLES),
    scope=st.sampled_from(SCOPES),
    removed=st.booleans(),
)
people = st.lists(
    st.builds(
        Person,
        same_organization=st.booleans(),
        status=st.sampled_from(("active", "invited", "deactivated")),
        name=st.sampled_from(NAMES),
        grants=st.lists(grants, max_size=3).map(tuple),
    ),
    min_size=1,
    max_size=5,
)


def _create(stack: ActionsStack, place: Place, person: Person) -> uuid.UUID:
    authz = stack.world.stack.authz
    world = stack.world
    organization = world.a if person.same_organization else world.b
    user = authz.add_user(organization.organization_id, status=person.status)
    _rename(stack, user, person.name)
    for grant in person.grants:
        if person.same_organization:
            level, scope = {
                "organization": (ScopeLevel.ORGANIZATION, None),
                "plant": (ScopeLevel.PLANT, organization.plant_id),
                "zone": (ScopeLevel.ZONE, organization.zone_id),
                "other_plant": (ScopeLevel.PLANT, place.other_plant),
                "other_zone": (ScopeLevel.ZONE, place.other_zone),
            }[grant.scope]
        else:  # en B, el mismo alcance sobre la planta o la zona de B
            level, scope = {
                "organization": (ScopeLevel.ORGANIZATION, None),
                "plant": (ScopeLevel.PLANT, organization.plant_id),
                "zone": (ScopeLevel.ZONE, organization.zone_id),
                "other_plant": (ScopeLevel.PLANT, organization.plant_id),
                "other_zone": (ScopeLevel.ZONE, organization.site.other_zone_id),
            }[grant.scope]
        assignment = authz.assign(organization.organization_id, user, grant.role, level, scope)
        if grant.removed:
            authz.remove_assignment(assignment)
    return user


def _retire(stack: ActionsStack, users: list[uuid.UUID]) -> None:
    """Desactiva a los generados: dejan la lista y el mundo no crece entre ejemplos."""
    for user in users:
        stack.deactivate(user)


@given(world=people)
def test_the_list_is_exactly_what_assign_accepts_for_every_generated_world(
    stack: ActionsStack, coordinator: Who, place: Place, world: list[Person]
) -> None:
    users = [_create(stack, place, person) for person in world]
    try:
        listed = {
            uuid.UUID(item["user_id"]): item for item in _listed(stack, coordinator, place.finding)
        }
        for user, person in zip(users, world, strict=True):
            accepted = _accepted_by_assign(stack, coordinator, place.finding_state_id, user)
            expected = person.expected_roles()
            assert (user in listed) == accepted == (expected is not None), person
            if expected is not None:
                assert listed[user] == {
                    "user_id": str(user),
                    "display_name": person.name,
                    "roles": expected,
                }
    finally:
        _retire(stack, users)


# --- A-126 de punta a punta ----------------------------------------------------------------------


def test_assign_accepts_the_listed_people_and_rejects_the_rest_like_a_missing_one(
    stack: ActionsStack, coordinator: Who, place: Place
) -> None:
    world = stack.world
    line = stack.user(world.a, Role.LINE_MANAGER, ScopeLevel.ZONE, world.a.zone_id)
    plant = stack.user(world.a, Role.PLANT_MANAGER, ScopeLevel.PLANT, world.a.plant_id)
    rejected = {
        "administrator": stack.user(world.a, Role.ADMINISTRATOR),
        "copasst": stack.user(world.a, Role.COPASST),
        "other_plant": stack.user(world.a, Role.PLANT_MANAGER, ScopeLevel.PLANT, place.other_plant),
        "other_zone": stack.user(world.a, Role.LINE_MANAGER, ScopeLevel.ZONE, place.other_zone),
        "other_organization": stack.user(world.b, Role.LINE_MANAGER),
    }
    listed = {item["user_id"] for item in _listed(stack, coordinator, place.finding)}
    assert {str(line), str(plant)} <= listed
    assert not {str(user) for user in rejected.values()} & listed
    finding = stack.opened()
    missing = stack.assign(coordinator, finding, stack.action_body(uuid.uuid4()))
    assert _error(missing) == _NOT_FOUND
    before = stack.untouched(finding)
    for reason, user in rejected.items():
        response = stack.assign(coordinator, finding, stack.action_body(user))
        assert _comparable(response) == _comparable(missing), reason
    assert stack.untouched(finding) == before
    for user in (line, plant):
        response = stack.assign(coordinator, stack.opened(), stack.action_body(user))
        assert response.status_code == 201, response.text
        assert response.json()["assignee_user_id"] == str(user)


def test_the_reopening_uses_the_same_rule(stack: ActionsStack, coordinator: Who) -> None:
    world = stack.world
    responsible = stack.user(world.a, Role.COORDINATOR_SST, ScopeLevel.ZONE, world.a.zone_id)
    finding, _ = stack.closed(coordinator, responsible)
    administrator = stack.user(world.a, Role.ADMINISTRATOR)
    refused = stack.reopen(coordinator, finding, stack.reopening_body(administrator))
    assert _error(refused) == _NOT_FOUND
    line = stack.user(world.a, Role.LINE_MANAGER)
    assert str(line) in {item["user_id"] for item in _listed(stack, coordinator, finding)}
    accepted = stack.reopen(coordinator, finding, stack.reopening_body(line))
    assert accepted.status_code == 201, accepted.text


# --- inactivos fuera (BR-LAZ-49) ---------------------------------------------------------------


def test_a_deactivated_or_unassigned_person_leaves_the_list_and_assign_refuses_them(
    stack: ActionsStack, coordinator: Who, place: Place
) -> None:
    world = stack.world
    authz = world.stack.authz
    deactivated = stack.user(world.a, Role.LINE_MANAGER)
    unassigned = authz.add_user(world.a.organization_id)
    assignment = authz.assign(world.a.organization_id, unassigned, Role.PLANT_MANAGER)
    before = {item["user_id"] for item in _listed(stack, coordinator, place.finding)}
    assert {str(deactivated), str(unassigned)} <= before
    stack.deactivate(deactivated)  # entre la lista y la asignación
    authz.remove_assignment(assignment)
    after = {item["user_id"] for item in _listed(stack, coordinator, place.finding)}
    assert after == before - {str(deactivated), str(unassigned)}
    finding = stack.opened()
    missing = stack.assign(coordinator, finding, stack.action_body(uuid.uuid4()))
    for user in (deactivated, unassigned):
        response = stack.assign(coordinator, finding, stack.action_body(user))
        assert _comparable(response) == _comparable(missing)
        assert _error(response) == _NOT_FOUND


def test_an_invited_person_is_not_listed(
    stack: ActionsStack, coordinator: Who, place: Place
) -> None:
    authz = stack.world.stack.authz
    invited = authz.add_user(stack.world.a.organization_id, status="invited")
    authz.assign(stack.world.a.organization_id, invited, Role.COORDINATOR_SST)
    listed = {item["user_id"] for item in _listed(stack, coordinator, place.finding)}
    assert str(invited) not in listed


# --- alcance: siete roles y tres alcances -------------------------------------------------------


def _scopes(stack: ActionsStack, place: Place) -> dict[str, tuple[ScopeLevel, uuid.UUID | None]]:
    a = stack.world.a
    return {
        "organization": (ScopeLevel.ORGANIZATION, None),
        "plant": (ScopeLevel.PLANT, a.plant_id),
        "zone": (ScopeLevel.ZONE, a.zone_id),
        "other_plant": (ScopeLevel.PLANT, place.other_plant),
        "other_zone": (ScopeLevel.ZONE, place.other_zone),
    }


@pytest.mark.parametrize("role", CLIENT_ROLES, ids=lambda role: role.value)
def test_only_the_coordinator_over_the_zone_reads_the_list(
    stack: ActionsStack, place: Place, role: Role
) -> None:
    for name, (level, scope) in _scopes(stack, place).items():
        who = stack.member(stack.world.a, role, level, scope)
        known = stack.assignees(who, place.finding)
        missing = stack.assignees(who, uuid.uuid4())
        assert _error(missing) == _NOT_FOUND
        if role is Role.COORDINATOR_SST and name in COVERING:
            assert known.status_code == 200, (role, name, known.text)
            assert set(known.json()) == {"items", "next_cursor"}
        else:
            assert _comparable(known) == _comparable(missing), (role, name)


def test_the_provider_and_the_platform_operator_answer_like_a_missing_finding(
    stack: ActionsStack, place: Place
) -> None:
    authz = stack.world.stack.authz
    installer = stack.installer(stack.world.a)
    operator: Who = (authz.open_session(authz.provider_organization_id, authz.operator_id), None)
    for who in (installer, operator):
        known = stack.assignees(who, place.finding)
        missing = stack.assignees(who, uuid.uuid4())
        assert _error(known) == _NOT_FOUND, known.text
        assert _comparable(known) == _comparable(missing)


def test_a_finding_of_another_organization_answers_like_a_missing_one(
    stack: ActionsStack, coordinator: Who
) -> None:
    foreign = stack.opened(stack.world.b)
    known = stack.assignees(coordinator, foreign)
    missing = stack.assignees(coordinator, uuid.uuid4())
    assert _error(known) == _NOT_FOUND
    assert _comparable(known) == _comparable(missing)
    # Y el coordinador de B sí la lee, sin ver a nadie de A.
    b_coordinator = stack.member(stack.world.b)
    ids = {item["user_id"] for item in _listed(stack, b_coordinator, foreign)}
    a_users = {
        str(row["user_id"])
        for row in stack.fetch(
            "SELECT user_id FROM identity.user_account WHERE organization_id = $1",
            stack.world.a.organization_id,
        )
    }
    assert ids and not ids & a_users


# --- minimización, búsqueda y paginación -------------------------------------------------------


def test_each_person_carries_exactly_three_fields_and_the_schema_says_so(
    stack: ActionsStack, coordinator: Who, place: Place
) -> None:
    items = _listed(stack, coordinator, place.finding)
    assert items
    for item in items:
        assert set(item) == ITEM_FIELDS
    document = yaml.safe_load(APP_YAML.read_text(encoding="utf-8"))
    schema = document["components"]["schemas"]["AssigneeOut"]
    assert set(schema["properties"]) == ITEM_FIELDS
    assert set(schema["required"]) == ITEM_FIELDS
    assert schema["additionalProperties"] is False
    page = document["components"]["schemas"]["AssigneePageOut"]
    assert set(page["properties"]) == {"items", "next_cursor"}
    operation = document["paths"]["/loop/findings/{finding_state_id}/assignees"]["get"]
    assert {(p["in"], p["name"]) for p in operation["parameters"]} == {
        ("path", "finding_state_id"),
        ("query", "cursor"),
        ("query", "limit"),
    }


@pytest.mark.parametrize(
    "params",
    [
        {"q": "Alba"},
        {"search": "Alba"},
        {"display_name": "Alba"},
        {"name": "Alba"},
        {"user_id": "0190a1b2-4444-7000-8000-000000000001"},
        {"role": "line_manager"},
        [("limit", "10"), ("limit", "20")],
        {"limit": "0"},
        {"limit": "201"},
        {"cursor": "no-es-un-cursor"},
        {"cursor": "A" * 23},
    ],
    ids=[
        "q",
        "search",
        "display_name",
        "name",
        "user_id",
        "role",
        "limit_repeated",
        "limit_0",
        "limit_201",
        "cursor_malformed",
        "cursor_too_long",
    ],
)
def test_there_is_no_search_and_out_of_range_requests_are_invalid(
    stack: ActionsStack, coordinator: Who, place: Place, params: Any
) -> None:
    response = stack.assignees(coordinator, place.finding, params)
    assert _error(response) == _INVALID, response.text


def test_the_limits_one_and_two_hundred_are_accepted(
    stack: ActionsStack, coordinator: Who, place: Place
) -> None:
    for limit in (1, 200):
        response = stack.assignees(coordinator, place.finding, {"limit": limit})
        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) <= limit
    default = stack.assignees(coordinator, place.finding)
    assert default.status_code == 200
    assert default.headers["cache-control"] == "no-store"


def test_the_list_is_ordered_by_name_and_id_and_paged_by_key(
    stack: ActionsStack, coordinator: Who, place: Place
) -> None:
    world = stack.world
    created = []
    for name in ("Fabio", "Alba", "Celia", "Alba", "Bruno"):
        user = stack.user(world.a, Role.LINE_MANAGER)
        _rename(stack, user, name)
        created.append(user)
    try:
        whole = _listed(stack, coordinator, place.finding)
        keys = [(item["display_name"], uuid.UUID(item["user_id"])) for item in whole]
        assert keys == sorted(keys)
        assert len({item["user_id"] for item in whole}) == len(whole)
        for limit in (1, 2, 3):
            assert _listed(stack, coordinator, place.finding, limit=limit) == whole
    finally:
        _retire(stack, created)


def test_a_cursor_of_someone_outside_the_organization_gives_an_empty_page(
    stack: ActionsStack, coordinator: Who, place: Place
) -> None:
    foreign = stack.user(stack.world.b, Role.LINE_MANAGER)
    for after in (foreign, uuid.uuid4()):
        response = stack.assignees(
            coordinator, place.finding, {"cursor": encode_assignee_cursor(after)}
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"items": [], "next_cursor": None}


def test_the_cursor_round_trips_the_user_id() -> None:
    user = uuid.UUID("0190a1b2-4444-7000-8000-00000000abcd")
    assert decode_assignee_cursor(encode_assignee_cursor(user)) == user
    assert len(encode_assignee_cursor(user)) == 22
