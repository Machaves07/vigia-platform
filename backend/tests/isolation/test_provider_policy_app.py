"""PR-NUC-52 a nivel de aplicación: la política de proveedor sin el filtro de ``allowed_scopes``.

TASK-139 (PAT-NUC-SEG-01 capa 2, LC-NUC-12; BR-NUC-01 nota del 2026-09-20, BR-NUC-04). Para
cualquier concesión generada y **toda** tabla con planta o zona que ``vigia_app`` puede leer
(``identity``, ``ledger`` y ``shared``), una consulta sin filtro de aplicación bajo el contexto del
proveedor devuelve exactamente las filas del alcance concedido mientras la concesión está vigente,
y cero filas tras revocarla o vencer.

A diferencia de ``tests/properties/test_provider_policy.py`` (la base, con ``SET LOCAL`` a mano),
aquí el contexto lo construye la aplicación: ``ScopeContexts.context_from_session`` con la cookie
de una sesión real del instalador y ``X-Vigia-Concession`` (la misma llamada que hace la cadena de
middleware), y el puerto de prueba (``UnfilteredPort``) lee por ``shared.db.Database`` como
``vigia_app`` con ese contexto, **sin ningún filtro**: si un puerto de U-03 o U-04 olvidara
acotar por ``allowed_scopes``, esto es lo que vería. La verdad se calcula como superusuario.

El contexto se construye con la hora de la aplicación (reloj simulado) dentro de la vigencia; la
base decide con su ``now()``: una concesión que venció **después** de construir el contexto, o que
el cliente revocó, ya no deja ver nada en la transacción siguiente.

La metapropiedad sobre tablas: ``test_the_port_covers_every_table_with_plant_or_zone`` falla, con
el nombre de la tabla, si una migración añade una tabla con planta o zona sin su caso aquí.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Final

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import TextClause, text

from tests.authz_support import AuthzEnvironment, Site, authz_environment
from tests.factories import uuid7
from tests.identity_db import BASE_TIME
from tests.integration.conftest import PostgresEndpoint
from tests.ledger_database import (
    audit_values,
    evidence_values,
    insert_audit,
    insert_evidence,
    insert_record,
    record_values,
    register_record_types,
    set_organization,
)
from vigia_platform.shared.context import Role, ScopeContext, ScopeLevel

pytestmark = pytest.mark.integration

SCHEMAS: Final = ("identity", "ledger", "shared")
PLANT_OR_ZONE: Final = ("plant_id", "zone_id", "scope_plant_id", "scope_zone_id")


@dataclass(frozen=True)
class Table:
    """Cómo lee el puerto la tabla y cuál es la planta de cada fila (la verdad del superusuario).

    ``row`` identifica la fila física (``tableoid`` y ``ctid``: también en las particionadas); la
    verdad devuelve además la organización, la planta y, si la tabla la tiene, la concesión del
    actor.
    """

    port: TextClause
    truth: str


def _table(name: str, plant: str, *, own_concession: bool = False) -> Table:
    concession = "actor_concession_id" if own_concession else "NULL::uuid"
    return Table(
        port=_PORT_QUERIES[name],
        truth=(
            "SELECT tableoid::regclass::text || ctid::text AS row, organization_id,"  # noqa: S608
            f" {plant} AS plant, {concession} AS concession FROM {name}"
        ),
    )


_PORT_QUERIES: Final[dict[str, TextClause]] = {
    "identity.plant": text("SELECT tableoid::regclass::text || ctid::text FROM identity.plant"),
    "identity.zone": text("SELECT tableoid::regclass::text || ctid::text FROM identity.zone"),
    "identity.node_identity": text(
        "SELECT tableoid::regclass::text || ctid::text FROM identity.node_identity"
    ),
    "identity.zone_node_assignment": text(
        "SELECT tableoid::regclass::text || ctid::text FROM identity.zone_node_assignment"
    ),
    "identity.live_view_token_issuance": text(
        "SELECT tableoid::regclass::text || ctid::text FROM identity.live_view_token_issuance"
    ),
    "ledger.chain_head": text(
        "SELECT tableoid::regclass::text || ctid::text FROM ledger.chain_head"
    ),
    "ledger.ledger_record": text(
        "SELECT tableoid::regclass::text || ctid::text FROM ledger.ledger_record"
    ),
    "ledger.evidence": text("SELECT tableoid::regclass::text || ctid::text FROM ledger.evidence"),
    "ledger.label": text("SELECT tableoid::regclass::text || ctid::text FROM ledger.label"),
    "ledger.communication_state": text(
        "SELECT tableoid::regclass::text || ctid::text FROM ledger.communication_state"
    ),
    "shared.audit_entry": text(
        "SELECT tableoid::regclass::text || ctid::text FROM shared.audit_entry"
    ),
    "shared.outbox_event": text(
        "SELECT tableoid::regclass::text || ctid::text FROM shared.outbox_event"
    ),
}
"""``UnfilteredPort``: cada tabla entera, sin ``WHERE`` (ni organización, ni planta, ni zona)."""

TABLES: Final[dict[str, Table]] = {
    "identity.plant": _table("identity.plant", "plant_id"),
    "identity.zone": _table("identity.zone", "plant_id"),
    "identity.node_identity": _table("identity.node_identity", "plant_id"),
    "identity.zone_node_assignment": _table("identity.zone_node_assignment", "plant_id"),
    "identity.live_view_token_issuance": _table("identity.live_view_token_issuance", "plant_id"),
    "ledger.chain_head": _table("ledger.chain_head", "plant_id"),
    # Cadena de planta, o el alcance del registro en una cadena de organización (nuc_0015).
    "ledger.ledger_record": _table("ledger.ledger_record", "COALESCE(plant_id, scope_plant_id)"),
    "ledger.evidence": _table("ledger.evidence", "plant_id"),
    "ledger.label": _table("ledger.label", "plant_id"),
    "ledger.communication_state": _table("ledger.communication_state", "plant_id"),
    # Más las entradas que escribió la propia concesión vigente (nuc_0015).
    "shared.audit_entry": _table("shared.audit_entry", "scope_plant_id", own_concession=True),
    "shared.outbox_event": _table("shared.outbox_event", "plant_id"),
}
"""Toda tabla con planta o zona que ``vigia_app`` puede leer, con la planta de cada fila."""


class UnfilteredPort:
    """Puerto de prueba: lee la tabla entera por ``shared.db`` con el contexto dado (PR-NUC-52).

    No filtra por organización, planta, zona ni ``allowed_scopes``: solo la base decide.
    """

    def __init__(self, env: AuthzEnvironment) -> None:
        self._database = env.sessions.database
        self._env = env

    def rows(self, context: ScopeContext, table: str) -> frozenset[str]:
        found = self._env.run(self._database.read(context, TABLES[table].port, {}))
        return frozenset(str(row[0]) for row in found)


# --- Entorno ------------------------------------------------------------------------------------


@dataclass
class World:
    env: AuthzEnvironment
    client: Site
    other: Site
    port: UnfilteredPort
    installer: uuid.UUID
    truth: dict[str, list[tuple[str, uuid.UUID, uuid.UUID | None, uuid.UUID | None]]] = field(
        default_factory=dict
    )

    @property
    def plants(self) -> list[uuid.UUID]:
        return list(self.client.plants)

    def database_now(self) -> dt.datetime:
        (row,) = self.env.fetch("SELECT now() AS now")
        moment: dt.datetime = row["now"]
        return moment

    def refresh_truth(self) -> None:
        for name, table in TABLES.items():
            self.truth[name] = [
                (str(r["row"]), r["organization_id"], r["plant"], r["concession"])
                for r in self.env.fetch(table.truth)
            ]

    def expected(
        self, table: str, level: ScopeLevel, plant: uuid.UUID | None, concession: uuid.UUID
    ) -> frozenset[str]:
        """Filas del cliente que alcanza la concesión vigente (alcance o autoría propia)."""
        return frozenset(
            row
            for row, organization_id, row_plant, actor_concession in self.truth[table]
            if organization_id == self.client.organization_id
            and (
                level is ScopeLevel.ORGANIZATION
                or (row_plant is not None and row_plant == plant)
                or actor_concession == concession
            )
        )

    def context(self, concession: uuid.UUID, at: dt.datetime) -> ScopeContext:
        """El contexto que construye la cadena para la petición del instalador en ``at``."""
        clock = self.env.sessions.clock
        clock.set(at)
        cookie = self.env.open_session(self.env.provider_organization_id, self.installer, at=at)
        scope = self.env.run(
            self.env.contexts.context_from_session(cookie, concession_id=concession)
        )
        context: ScopeContext = scope.context
        assert context.organization_id == self.client.organization_id
        assert context.concession_id == concession
        return context


def _seed_site(env: AuthzEnvironment, site: Site) -> None:
    """Una fila (o más) por tabla y planta, y filas de organización donde la tabla las admite."""
    organization_id = site.organization_id
    user_id = env.add_user(organization_id)
    for plant_id, zones in site.plants.items():
        zone_id = zones[0]
        node_id = uuid.uuid4()
        env.execute(
            "INSERT INTO identity.node_identity (node_id, organization_id, plant_id, code, status,"
            " created_at) VALUES ($1, $2, $3, $4, 'enrolled', $5)",
            node_id,
            organization_id,
            plant_id,
            f"ND-{secrets.token_hex(8).upper()}",
            BASE_TIME,
        )
        env.execute(
            "INSERT INTO identity.zone_node_assignment (assignment_id, organization_id, plant_id,"
            " zone_id, node_id, assigned_at, assigned_by) VALUES ($1, $2, $3, $4, $5, $6, $7)",
            uuid7(),
            organization_id,
            plant_id,
            zone_id,
            node_id,
            BASE_TIME,
            env.operator_id,
        )
        env.execute(
            "INSERT INTO identity.live_view_token_issuance (jti, organization_id, plant_id,"
            " zone_id, node_id, user_id, role_in_use, issued_at, expires_at, correlation_id)"
            " VALUES ($1, $2, $3, $4, $5, $6, 'copasst', $7::timestamptz,"
            " $7::timestamptz + interval '600 seconds', $8)",
            uuid.uuid4(),
            organization_id,
            plant_id,
            zone_id,
            node_id,
            user_id,
            BASE_TIME,
            uuid.uuid4(),
        )
        env.execute(
            "INSERT INTO ledger.label (label_id, organization_id, plant_id, zone_id,"
            " source_record_id, subject_record_id, family, outcome, reason_category, labeled_at,"
            " labeled_by) VALUES ($1, $2, $3, $4, $5, $6, 'ppe_helmet', 'confirmed',"
            " 'observed', $7, $8)",
            uuid.uuid4(),
            organization_id,
            plant_id,
            zone_id,
            uuid.uuid4(),
            uuid.uuid4(),
            BASE_TIME,
            json.dumps({"kind": "user", "role": "coordinator_sst"}),
        )
        env.execute(
            "INSERT INTO ledger.communication_state (node_id, organization_id, plant_id, state,"
            " since, source_record_id) VALUES ($1, $2, $3, 'reachable', $4, $5)",
            node_id,
            organization_id,
            plant_id,
            BASE_TIME,
            uuid.uuid4(),
        )
    for event_plant in (*site.plants, None):
        env.execute(
            "INSERT INTO shared.outbox_event (event_id, organization_id, plant_id, event_name,"
            " payload, correlation_id, created_at) VALUES ($1, $2, $3, 'security_alert', $4, $5,"
            " $6)",
            uuid7(),
            organization_id,
            event_plant,
            json.dumps({"alert_kind": "context_absent_attempt"}),
            uuid7(),
            BASE_TIME,
        )
    admin = env.sessions.admin

    async def chained() -> None:
        async with admin.transaction():
            await set_organization(admin, organization_id)
            await register_record_types(admin)
            for chain_plant in (*site.plants, None):
                record = await insert_record(admin, record_values(organization_id, chain_plant))
                if chain_plant is not None:
                    await insert_evidence(
                        admin,
                        evidence_values(
                            organization_id, chain_plant, record["record_id"], record["received_at"]
                        ),
                    )
                audit = audit_values(organization_id)
                audit["scope_plant_id"] = chain_plant
                await insert_audit(admin, audit)

    env.run(chained())


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[World]:
    with authz_environment(postgres_endpoint, "provider_policy_app") as env:
        client = env.add_site(plants=2, zones_per_plant=1)
        other = env.add_site(plants=2, zones_per_plant=1)
        for site in (client, other):
            _seed_site(env, site)
        world = World(env, client, other, UnfilteredPort(env), env.add_provider_user())
        world.refresh_truth()
        yield world


# --- Metapropiedad sobre tablas -----------------------------------------------------------------


def test_the_port_covers_every_table_with_plant_or_zone(world: World) -> None:
    rows = world.env.fetch(
        "SELECT DISTINCT n.nspname || '.' || c.relname AS name"
        " FROM pg_catalog.pg_attribute AS a"
        " JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid"
        " JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace"
        " WHERE n.nspname = ANY($1::text[]) AND a.attname = ANY($2::text[])"
        " AND NOT a.attisdropped AND c.relkind IN ('r', 'p') AND NOT c.relispartition"
        " AND has_table_privilege('vigia_app', c.oid, 'SELECT')",
        list(SCHEMAS),
        list(PLANT_OR_ZONE),
    )
    found = {row["name"] for row in rows}
    missing = sorted(found - set(TABLES))
    assert not missing, (
        f"tablas con planta o zona sin caso en la prueba de política de proveedor: {missing}"
    )
    assert set(TABLES) <= found, sorted(set(TABLES) - found)


def test_every_table_has_rows_in_both_plants_of_the_client(world: World) -> None:
    # Sin filas en las dos plantas, «exactamente el alcance» no distinguiría nada.
    for name in TABLES:
        plants = {
            plant
            for _, organization_id, plant, _ in world.truth[name]
            if organization_id == world.client.organization_id
        }
        assert set(world.plants) <= plants, name


def test_a_client_user_context_still_sees_its_whole_organization(world: World) -> None:
    # La política de proveedor no recorta a nadie más (la RLS de organización sigue igual).
    user_id = world.env.add_user(world.client.organization_id)
    world.env.assign(world.client.organization_id, user_id, Role.ADMINISTRATOR)
    cookie = world.env.open_session(world.client.organization_id, user_id)
    context = world.env.run(world.env.contexts.context_from_session(cookie)).context
    for name in TABLES:
        assert world.port.rows(context, name) == world.expected(
            name, ScopeLevel.ORGANIZATION, None, uuid.uuid4()
        ), name


# --- PR-NUC-52 ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    level: ScopeLevel
    plant_index: int
    expires_in_minutes: int
    """Vencimiento frente al ``now()`` de la base; negativo: venció tras construir el contexto."""
    revoked: bool


@st.composite
def scenarios(draw: st.DrawFn) -> Scenario:
    return Scenario(
        level=draw(st.sampled_from([ScopeLevel.ORGANIZATION, ScopeLevel.PLANT])),
        plant_index=draw(st.integers(0, 1)),
        expires_in_minutes=draw(
            # Concedida 2 h antes del now() de la base: la duración mínima es 1 h (nuc_0004).
            st.one_of(st.integers(-59, -1), st.integers(1, 60 * 24 * 3))
        ),
        revoked=draw(st.booleans()),
    )


@given(scenario=scenarios())
def test_provider_sees_exactly_the_granted_scope_while_in_force(
    world: World, scenario: Scenario
) -> None:
    env = world.env
    now = world.database_now()
    plant = world.plants[scenario.plant_index]
    granted = now - dt.timedelta(hours=2)
    expires = now + dt.timedelta(minutes=scenario.expires_in_minutes)
    concession = env.add_concession(
        world.client.organization_id,
        world.installer,
        level=scenario.level,
        scope_id=plant if scenario.level is ScopeLevel.PLANT else None,
        granted_at=granted,
        duration=expires - granted,
    )
    # La cadena construye el contexto un minuto después de conceder: siempre vigente entonces.
    context = world.context(concession, granted + dt.timedelta(minutes=1))
    if scenario.revoked and scenario.expires_in_minutes > 0:
        # Lo ya vencido no se revoca (nuc_0009): vence.
        env.revoke_concession(concession, now)
    in_force = scenario.expires_in_minutes > 0 and not scenario.revoked
    for name in TABLES:
        seen = world.port.rows(context, name)
        if in_force:
            expected = world.expected(
                name,
                scenario.level,
                plant if scenario.level is ScopeLevel.PLANT else None,
                concession,
            )
        else:
            expected = frozenset()
        assert seen == expected, (name, scenario)


def test_a_plant_concession_never_sees_the_other_plant_nor_the_organization_rows(
    world: World,
) -> None:
    # El ejemplo concreto de la propiedad: planta 0, nada de la planta 1 ni de nivel organización.
    env = world.env
    now = world.database_now()
    plant0, plant1 = world.plants
    concession = env.add_concession(
        world.client.organization_id,
        world.installer,
        level=ScopeLevel.PLANT,
        scope_id=plant0,
        granted_at=now - dt.timedelta(hours=1),
        duration=dt.timedelta(days=1),
    )
    context = world.context(concession, now - dt.timedelta(minutes=30))
    plants_seen: dict[str, set[uuid.UUID | None]] = {}
    for name in TABLES:
        rows = world.port.rows(context, name)
        plants_seen[name] = {plant for row, _, plant, _ in world.truth[name] if row in rows}
    assert all(plants == {plant0} for plants in plants_seen.values()), plants_seen
    assert all(plant1 not in plants for plants in plants_seen.values())


# --- Seguimientos de VIG-76 ---------------------------------------------------------------------


def test_the_lookup_variable_set_by_the_application_does_not_widen_anything(world: World) -> None:
    # vigia.concession_lookup solo cuenta dentro de las funciones de vigia_migrate (nuc_0009):
    # fijarla desde vigia_app no amplía lo que ve un contexto de proveedor.
    env = world.env
    now = world.database_now()
    plant0 = world.plants[0]
    concession = env.add_concession(
        world.client.organization_id,
        world.installer,
        level=ScopeLevel.PLANT,
        scope_id=plant0,
        granted_at=now - dt.timedelta(hours=1),
    )
    context = world.context(concession, now - dt.timedelta(minutes=30))
    database = world.env.sessions.database

    async def with_lookup(scope: ScopeContext, table: str) -> frozenset[str]:
        async with database.transaction(scope) as transaction:
            await transaction.execute(
                text("SELECT set_config('vigia.concession_lookup', 'on', true)"), {}
            )
            rows = (await transaction.execute(TABLES[table].port, {})).all()
        return frozenset(str(row[0]) for row in rows)

    for name in TABLES:
        assert env.run(with_lookup(context, name)) == world.port.rows(context, name), name
    revoked = env.add_concession(
        world.client.organization_id, world.installer, granted_at=now - dt.timedelta(hours=1)
    )
    stale = world.context(revoked, now - dt.timedelta(minutes=30))
    env.revoke_concession(revoked, now)
    for name in TABLES:
        assert env.run(with_lookup(stale, name)) == frozenset(), name


def test_a_concession_dated_in_the_future_sees_nothing_until_then(world: World) -> None:
    # granted_at lo fija la hora de la aplicación; la base no da por vigente lo que aún no empezó.
    env = world.env
    now = world.database_now()
    concession = env.add_concession(
        world.client.organization_id,
        world.installer,
        granted_at=now + dt.timedelta(hours=1),
    )
    context = world.context(concession, now + dt.timedelta(hours=1, minutes=1))
    for name in TABLES:
        assert world.port.rows(context, name) == frozenset(), name


def _close(world: World, context: ScopeContext, concession: uuid.UUID, side: str) -> None:
    database = world.env.sessions.database

    async def close() -> None:
        async with database.transaction(context) as transaction:
            await transaction.execute(
                text(
                    "UPDATE identity.provider_concession SET status = 'revoked',"
                    " revoked_at = now(), revoked_by = :actor, revoked_by_side = :side"
                    " WHERE concession_id = :concession"
                ),
                {"actor": context.actor.id, "side": side, "concession": concession},
            )

    world.env.run(close())


def _status(world: World, concession: uuid.UUID) -> tuple[str, str | None]:
    (row,) = world.env.fetch(
        "SELECT status, revoked_by_side FROM identity.provider_concession WHERE concession_id = $1",
        concession,
    )
    return row["status"], row["revoked_by_side"]


def test_the_revoking_side_cannot_be_forged(world: World) -> None:
    env = world.env
    now = world.database_now()
    client_admin = env.add_user(world.client.organization_id)
    env.assign(world.client.organization_id, client_admin, Role.ADMINISTRATOR)
    cookie = env.open_session(world.client.organization_id, client_admin)
    client = env.run(env.contexts.context_from_session(cookie)).context

    def fresh() -> uuid.UUID:
        return env.add_concession(
            world.client.organization_id, world.installer, granted_at=now - dt.timedelta(hours=1)
        )

    # El cliente no escribe «la revocó el proveedor»...
    forged = fresh()
    with pytest.raises(Exception, match="row-level security"):
        _close(world, client, forged, "provider")
    assert _status(world, forged) == ("active", None)
    # ...ni el proveedor «la revocó el cliente».
    provider_forged = fresh()
    provider = world.context(provider_forged, now - dt.timedelta(minutes=30))
    with pytest.raises(Exception, match="row-level security"):
        _close(world, provider, provider_forged, "client")
    assert _status(world, provider_forged) == ("active", None)
    # Cada lado sí escribe el suyo (sin esto, lo anterior pasaría con cualquier UPDATE prohibido).
    honest = fresh()
    _close(world, client, honest, "client")
    assert _status(world, honest) == ("revoked", "client")
    own = fresh()
    _close(world, world.context(own, now - dt.timedelta(minutes=30)), own, "provider")
    assert _status(world, own) == ("revoked", "provider")
