"""Identidad del nodo por petición sobre PostgreSQL 16 como ``vigia_app`` (TASK-206; SEG-01).

La aplicación real (``create_app`` con la cadena fija) con la ruta de prueba interna, la
``NodeApiGate`` real y ``PostgresNodeContextStore`` sobre la base migrada hasta ``gob_0019`` y
sembrada con dos clientes (``tests/identity_db.py``). Cada prueba da de alta un nodo nuevo con su
zona y su credencial, y presenta las cabeceras del balanceador de una hoja de ``TestAuthority``.

- **Una sola consulta y sin caché**: cada petición ejecuta exactamente una sentencia de datos
  (``NODE_IDENTITY_STATEMENT``; las tres ``set_config`` de la apertura no cuentan) y la siguiente
  vuelve a consultar.
- **Revocación confirmada → ``node_revoked`` 401 en la siguiente petición** (identidad,
  credencial o baja); una credencial ``overlapping`` autentica 24 h desde su sucesora y después
  ``node_revoked``.
- **Guarda de alcance** (BR-GOB-88): un certificado de la organización A con una zona de B →
  ``node_zone_mismatch``; también una zona de otra planta de A y una zona que el nodo tuvo y ya
  no tiene; un certificado que dice ser de B → ``node_not_enrolled`` (la RLS no deja ver la fila).
- **Alta** (A-51): ``context_from_node_enrollment`` encuentra la organización del nodo declarado
  por ``node_id`` en cualquier organización con la función de ``gob_0019``, en una sentencia y solo
  con actor ``system``; ``vigia_app`` sigue sin ver ``identity.node_identity`` de otra
  organización.
- **Freno global** (NFR-GOB-33): la entrada ``node_rate_brake_set`` de la auditoría de la
  proveedora fija el freno de la instancia.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from sqlalchemy import event
from vigia_contracts.models.api import parse_rejection_response

from tests.api_support import World
from tests.authz_support import SYSTEM_ACTOR_ID
from tests.integration.conftest import PostgresEndpoint
from tests.node_api_db import DbNode, insert_node, insert_zone, issue, reassign, revoke_credential
from tests.node_api_support import (
    DAY,
    VERSION,
    Probe,
    TestAuthority,
    alb_headers,
    node_app,
    node_gate,
)
from tests.session_support import SessionEnvironment, session_environment
from vigia_platform.identity.adapters.authz_store import PostgresContextStore
from vigia_platform.identity.authz.context import OVERLAP, ScopeContexts
from vigia_platform.ledger.application.audit_writer import AuditOperation
from vigia_platform.node_api.identity import NODE_IDENTITY_STATEMENT, PostgresNodeContextStore
from vigia_platform.node_api.limits import AuditBrakeSource, EmergencyBrake, NodeRateLimits
from vigia_platform.shared.db import Database
from vigia_platform.shared.ratelimit import RateLimiter, brake_filters

pytestmark = pytest.mark.integration

CATALOG = "/api/nodes/zones/{zone}/catalog"


@dataclass
class Statements:
    """Sentencias que llegan al servidor por el motor (sin las ``set_config`` de la apertura)."""

    seen: list[str]

    def data(self) -> list[str]:
        return [s for s in self.seen if "set_config('vigia.organization_id'" not in s]


def _log(database: Database) -> Statements:
    log = Statements([])
    for pool in database._pools.values():
        engine = getattr(pool, "engine", None)
        if engine is None:
            continue

        def before(*arguments: Any) -> None:
            log.seen.append(str(arguments[2]))

        event.listen(engine.sync_engine, "before_cursor_execute", before)
    return log


@dataclass
class World2:
    env: SessionEnvironment
    contexts: ScopeContexts
    store: PostgresNodeContextStore
    authority: TestAuthority
    statements: Statements

    @property
    def now(self) -> dt.datetime:
        return self.env.clock.now()

    def run(self, awaitable: Any) -> Any:
        return self.env.run(awaitable)

    def node(self, tenant: str = "a", plant: int = 0) -> DbNode:
        owner = getattr(self.env.seed, tenant)
        return self.run(
            insert_node(
                self.env.admin,
                owner.organization_id,
                owner.plants[plant].plant_id,
                owner.user_id,
                self.now,
            )
        )

    def app(self, probe: Probe | None = None, **gate: Any) -> Any:
        return node_app(
            World(clock=self.env.clock),
            node_gate(
                contexts=self.contexts,
                store=self.store,
                clock=self.env.clock,
                probe=probe or Probe(),
                **gate,
            ),
        )

    def get(self, app: Any, zone: uuid.UUID, headers: dict[str, str]) -> httpx.Response:
        async def call() -> httpx.Response:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://nodes.vigia.test",
                timeout=30.0,
            ) as client:
                return await client.get(CATALOG.format(zone=zone), headers=headers)

        response: httpx.Response = self.run(call())
        return response


def _headers(certificate: Any) -> dict[str, str]:
    return {**alb_headers(certificate), "X-Vigia-Contract-Version": VERSION}


def _code(response: httpx.Response) -> tuple[int, str]:
    return response.status_code, parse_rejection_response(response.content).code.value


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[World2]:
    with session_environment(postgres_endpoint, "node_api_identity") as env:
        contexts = ScopeContexts(
            store=PostgresContextStore(env.database),
            clock=env.clock,
            provider_organization_id=env.seed.provider_organization_id,
            system_actor_id=SYSTEM_ACTOR_ID,
        )
        yield World2(
            env,
            contexts,
            PostgresNodeContextStore(env.database),
            TestAuthority(),
            _log(env.database),
        )


# --- Una consulta, sin caché --------------------------------------------------------------------


def test_identity_is_one_indexed_statement_per_request_and_never_cached(world: World2) -> None:
    node = world.node()
    certificate, _ = world.run(issue(world.env.admin, world.authority, node, world.now))
    app = world.app()
    world.statements.seen.clear()
    first = world.get(app, node.zone_id, _headers(certificate))
    after_first = world.statements.data()
    second = world.get(app, node.zone_id, _headers(certificate))
    assert first.status_code == second.status_code == 200
    assert first.json()["zones"] == [str(node.zone_id)]
    assert len(after_first) == 1, after_first
    # La misma sentencia, con los marcadores del controlador ($1, $2).
    expected = (
        str(NODE_IDENTITY_STATEMENT)
        .replace(":node_id", "$1")
        .replace(":certificate_serial", "$2")
    )
    assert after_first[0] == expected
    assert len(world.statements.data()) == 2


def test_the_identity_statement_uses_the_credential_index(world: World2) -> None:
    node = world.node()
    certificate, _ = world.run(issue(world.env.admin, world.authority, node, world.now))

    async def plan() -> str:
        async with world.env.admin.transaction():
            await world.env.admin.execute("SET LOCAL enable_seqscan = off")
            rows = await world.env.admin.fetch(
                "EXPLAIN "
                + str(NODE_IDENTITY_STATEMENT)
                .replace(":node_id", "$1")
                .replace(":certificate_serial", "$2"),
                node.node_id,
                format(certificate.serial_number, "x"),
            )
        return "\n".join(row[0] for row in rows)

    text = world.run(plan())
    assert "node_credential_identity" in text, text


# --- Revocación, rotación y baja ----------------------------------------------------------------


def test_a_confirmed_revocation_rejects_the_next_request_with_node_revoked(world: World2) -> None:
    node = world.node()
    certificate, credential = world.run(issue(world.env.admin, world.authority, node, world.now))
    app = world.app()
    assert world.get(app, node.zone_id, _headers(certificate)).status_code == 200
    world.run(revoke_credential(world.env.admin, credential, world.now))
    assert _code(world.get(app, node.zone_id, _headers(certificate))) == (401, "node_revoked")


@pytest.mark.parametrize("change", ["node", "decommission"])
def test_a_revoked_or_decommissioned_node_is_node_revoked(world: World2, change: str) -> None:
    node = world.node()
    certificate, _ = world.run(issue(world.env.admin, world.authority, node, world.now))
    app = world.app()
    assert world.get(app, node.zone_id, _headers(certificate)).status_code == 200

    async def revoke() -> None:
        admin = world.env.admin
        async with admin.transaction():
            await admin.execute(
                "UPDATE identity.node_identity SET status = 'revoked' WHERE node_id = $1",
                node.node_id,
            )
            await admin.execute(
                "UPDATE fleet.node_fleet_record SET revoked_at = $2,"
                " revocation_reason_es = 'Revocación sintética de prueba'"
                " , decommissioned_at = CASE WHEN $3::boolean THEN $2::timestamptz END"
                " WHERE node_id = $1",
                node.node_id,
                world.now,
                change == "decommission",
            )

    world.run(revoke())
    assert _code(world.get(app, node.zone_id, _headers(certificate))) == (401, "node_revoked")


def test_an_overlapping_credential_lasts_24_hours_from_its_successor(world: World2) -> None:
    node = world.node()
    old, old_id = world.run(issue(world.env.admin, world.authority, node, world.now - DAY))
    new, _ = world.run(
        issue(world.env.admin, world.authority, node, world.now, rotated_from=old_id)
    )
    world.run(
        world.env.admin.execute(
            "UPDATE fleet.node_credential SET status = 'overlapping' WHERE credential_id = $1",
            old_id,
        )
    )
    app = world.app()
    assert world.get(app, node.zone_id, _headers(old)).status_code == 200
    assert world.get(app, node.zone_id, _headers(new)).status_code == 200
    world.env.clock.advance(OVERLAP.total_seconds())
    assert _code(world.get(app, node.zone_id, _headers(old))) == (401, "node_revoked")
    assert world.get(app, node.zone_id, _headers(new)).status_code == 200


# --- Guarda de alcance --------------------------------------------------------------------------


def test_a_certificate_of_a_with_a_zone_of_b_is_node_zone_mismatch(world: World2) -> None:
    node = world.node("a")
    other = world.node("b")
    certificate, _ = world.run(issue(world.env.admin, world.authority, node, world.now))
    app = world.app()
    response = world.get(app, other.zone_id, _headers(certificate))
    assert _code(response) == (403, "node_zone_mismatch")
    # Ni otra planta de A ni la zona sembrada de esa planta (de otro nodo) entran en su alcance.
    plant_two_zone = world.env.seed.a.plants[1].zone_id
    assert _code(world.get(app, plant_two_zone, _headers(certificate))) == (
        403,
        "node_zone_mismatch",
    )


def test_a_zone_the_node_no_longer_has_is_node_zone_mismatch(world: World2) -> None:
    node = world.node()
    certificate, _ = world.run(issue(world.env.admin, world.authority, node, world.now))
    new_zone = world.run(
        insert_zone(world.env.admin, node.organization_id, node.plant_id, node.user_id)
    )
    app = world.app()
    assert world.get(app, node.zone_id, _headers(certificate)).status_code == 200
    world.run(
        reassign(world.env.admin, node, node.zone_id, new_zone, world.now - dt.timedelta(seconds=1))
    )
    assert _code(world.get(app, node.zone_id, _headers(certificate))) == (403, "node_zone_mismatch")
    assert world.get(app, new_zone, _headers(certificate)).json()["zones"] == [str(new_zone)]


def test_a_certificate_claiming_another_organization_is_not_enrolled(world: World2) -> None:
    node = world.node("a")
    other = world.node("b")
    _, _ = world.run(issue(world.env.admin, world.authority, node, world.now))
    # Una hoja con el node_id de A y la organización y planta de B: la RLS de B no ve el nodo.
    forged = world.authority.leaf(
        type(node.subject)(node.node_id, other.organization_id, other.plant_id),
        not_before=world.now - DAY,
        not_after=world.now + DAY,
    )
    app = world.app()
    assert _code(world.get(app, other.zone_id, _headers(forged))) == (401, "node_not_enrolled")


# --- Alta: búsqueda por node_id (gob_0019) ------------------------------------------------------


def test_the_enrollment_lookup_finds_the_declared_node_in_any_organization(world: World2) -> None:
    node = world.node("b")
    world.statements.seen.clear()
    scope = world.run(world.contexts.context_from_node_enrollment(world.store, node.node_id))
    assert scope is not None
    assert (scope.context.organization_id, scope.plant_id) == (node.organization_id, node.plant_id)
    assert len(world.statements.data()) == 1
    assert world.run(world.contexts.context_from_node_enrollment(world.store, uuid.uuid4())) is None


def test_the_enrollment_function_answers_only_the_system_actor(world: World2) -> None:
    node = world.node("b")

    async def as_app(actor: str) -> list[Any]:
        connection = await world.env.migrated.connect("vigia_app")
        try:
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('vigia.organization_id', $1, true),"
                    " set_config('vigia.actor_kind', $2, true),"
                    " set_config('vigia.concession_id', '', true)",
                    str(world.env.seed.a.organization_id),
                    actor,
                )
                found = await connection.fetch(
                    "SELECT * FROM fleet.vigia_node_enrollment_scope($1)", node.node_id
                )
                direct = await connection.fetch(
                    "SELECT node_id FROM identity.node_identity WHERE node_id = $1", node.node_id
                )
            return [len(found), len(direct)]
        finally:
            await connection.close()

    assert world.run(as_app("system")) == [1, 0]
    for actor in ("user", "node", "operator", "provider_user"):
        assert world.run(as_app(actor)) == [0, 0], actor


# --- Freno global por la auditoría --------------------------------------------------------------


def test_the_brake_is_the_last_audit_entry_of_the_provider(world: World2) -> None:
    provider = world.contexts.provider_audit_context()
    source = AuditBrakeSource(database=world.env.database, provider_context=lambda: provider)
    assert world.run(source.current()) is None

    async def set_brake(value: int | None) -> None:
        await world.env.audit.append(
            provider, AuditOperation.NODE_RATE_BRAKE_SET, filters=brake_filters(value)
        )

    world.run(set_brake(1))
    assert world.run(source.current()) == 1
    node = world.node()
    certificate, _ = world.run(issue(world.env.admin, world.authority, node, world.now))
    limits = NodeRateLimits(
        RateLimiter(world.env.clock), brake=EmergencyBrake(source, world.env.clock)
    )
    app = world.app(limits=limits)
    assert world.get(app, node.zone_id, _headers(certificate)).status_code == 200
    assert _code(world.get(app, node.zone_id, _headers(certificate))) == (429, "rate_limited")
    world.run(set_brake(None))
    assert world.run(source.current()) is None
