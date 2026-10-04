"""PR-GOB-28: la identidad del nodo refleja cada cambio en la primera petición (BR-GOB-66).

``RuleBasedStateMachine`` sobre PostgreSQL 16 real con **dos instancias** de la aplicación
(``create_app`` con la cadena fija, la ruta de prueba interna y su ``NodeApiGate``), cada una con
su propio ``Database`` (sus conexiones) sobre la misma base, como dos tareas detrás del
balanceador. Cada ejemplo da de alta un nodo nuevo con su zona, su credencial y dos zonas de
repuesto, y aplica una secuencia generada de:

- **rotaciones** (credencial nueva ``active`` con ``rotated_from``; la anterior pasa a
  ``overlapping``), **revocaciones** de credencial, **revocación** y **baja** del nodo,
  **reasignaciones** de zona y **avances del reloj** (para cruzar las 24 h del solapamiento).

Tras cada paso, la **primera** petición de cada credencial en **cada** instancia refleja el modelo:
nunca se acepta una credencial revocada, vencida o fuera de su solapamiento, ni un nodo revocado o
dado de baja, y el alcance es exactamente la zona asignada en ese instante (la zona retirada
responde ``node_zone_mismatch``). Nada se guarda entre peticiones: no hay caché que invalidar.

Semillas del perfil (``tests/conftest.py``): la fija y la de la sesión.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, ClassVar

import httpx
import pytest
from cryptography import x509
from hypothesis import seed as hypothesis_seed
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)
from vigia_contracts.models.api import parse_rejection_response

from tests.api_support import World
from tests.authz_support import SYSTEM_ACTOR_ID
from tests.conftest import _seeds_for_profile
from tests.integration.conftest import PostgresEndpoint
from tests.node_api_db import DbNode, insert_node, insert_zone, issue, reassign, revoke_credential
from tests.node_api_support import VERSION, Probe, TestAuthority, alb_headers, node_app, node_gate
from tests.outbox_support import app_database
from tests.session_support import SessionEnvironment, session_environment
from vigia_platform.identity.adapters.authz_store import PostgresContextStore
from vigia_platform.identity.authz.context import OVERLAP, ScopeContexts
from vigia_platform.node_api.identity import PostgresNodeContextStore
from vigia_platform.shared.db import Database

pytestmark = pytest.mark.integration

STEPS_PER_EXAMPLE = 10
"""Cada paso son escrituras reales más dos peticiones por credencial y por instancia."""
CATALOG = "/api/nodes/zones/{zone}/catalog"
HOUR = dt.timedelta(hours=1)


@dataclass
class Credential:
    certificate: x509.Certificate
    credential_id: uuid.UUID
    status: str
    issued_at: dt.datetime
    expires_at: dt.datetime
    successor_issued_at: dt.datetime | None = None


@dataclass
class Instances:
    env: SessionEnvironment
    databases: tuple[Database, Database]
    apps: tuple[Any, Any]
    authority: TestAuthority = field(default_factory=TestAuthority)

    def run(self, awaitable: Any) -> Any:
        return self.env.run(awaitable)

    def get(self, instance: int, zone: uuid.UUID, certificate: x509.Certificate) -> httpx.Response:
        headers = {**alb_headers(certificate), "X-Vigia-Contract-Version": VERSION}

        async def call() -> httpx.Response:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.apps[instance]),
                base_url="https://nodes.vigia.test",
                timeout=30.0,
            ) as client:
                return await client.get(CATALOG.format(zone=zone), headers=headers)

        response: httpx.Response = self.run(call())
        return response


def _instance(env: SessionEnvironment, database: Database) -> Any:
    contexts = ScopeContexts(
        store=PostgresContextStore(database),
        clock=env.clock,
        provider_organization_id=env.seed.provider_organization_id,
        system_actor_id=SYSTEM_ACTOR_ID,
    )
    gate = node_gate(
        contexts=contexts,
        store=PostgresNodeContextStore(database),
        clock=env.clock,
        probe=Probe(),
    )
    return node_app(World(clock=env.clock), gate)


@pytest.fixture(scope="module")
def instances(postgres_endpoint: PostgresEndpoint) -> Iterator[Instances]:
    with session_environment(postgres_endpoint, "pr_gob_28") as env:
        second = app_database(env.migrated, worker_pool_size=2)
        try:
            built = Instances(
                env,
                (env.database, second),
                (_instance(env, env.database), _instance(env, second)),
            )
            NodeIdentityMachine.instances = built
            yield built
        finally:
            env.run(second.dispose())


class NodeIdentityMachine(RuleBasedStateMachine):
    instances: ClassVar[Instances]

    def __init__(self) -> None:
        super().__init__()
        world = self.instances
        env = world.env
        self.world = world
        self.admin = env.admin
        tenant = env.seed.a
        self.node: DbNode = world.run(
            insert_node(
                self.admin,
                tenant.organization_id,
                tenant.plants[0].plant_id,
                tenant.user_id,
                self.now,
            )
        )
        self.spare = [
            world.run(
                insert_zone(
                    self.admin, self.node.organization_id, self.node.plant_id, self.node.user_id
                )
            )
            for _ in range(2)
        ]
        self.zone = self.node.zone_id
        self.previous_zones: list[uuid.UUID] = []
        self.revoked = False
        self.decommissioned = False
        certificate, credential_id = world.run(
            issue(self.admin, world.authority, self.node, self.now)
        )
        self.credentials = [
            Credential(
                certificate,
                credential_id,
                "active",
                self.now,
                self.now + 365 * dt.timedelta(days=1),
            )
        ]

    @property
    def now(self) -> dt.datetime:
        return self.instances.env.clock.now()

    # --- Reglas ---------------------------------------------------------------------------------

    @precondition(lambda self: not self.revoked and len(self.credentials) < 6)
    @rule()
    def rotate(self) -> None:
        active = [c for c in self.credentials if c.status == "active"]
        if not active:
            return
        previous = active[-1]
        certificate, credential_id = self.world.run(
            issue(
                self.admin,
                self.world.authority,
                self.node,
                self.now,
                rotated_from=previous.credential_id,
            )
        )
        self.world.run(
            self.admin.execute(
                "UPDATE fleet.node_credential SET status = 'overlapping' WHERE credential_id = $1",
                previous.credential_id,
            )
        )
        previous.status = "overlapping"
        previous.successor_issued_at = self.now
        self.credentials.append(
            Credential(
                certificate,
                credential_id,
                "active",
                self.now,
                self.now + 365 * dt.timedelta(days=1),
            )
        )

    @rule(index=st.integers(0, 5))
    def revoke_credential(self, index: int) -> None:
        live = [c for c in self.credentials if c.status in ("active", "overlapping")]
        if not live:
            return
        credential = live[index % len(live)]
        self.world.run(revoke_credential(self.admin, credential.credential_id, self.now))
        credential.status = "revoked"

    @precondition(lambda self: not self.revoked)
    @rule()
    def revoke_node(self) -> None:
        async def revoke() -> None:
            async with self.admin.transaction():
                await self.admin.execute(
                    "UPDATE identity.node_identity SET status = 'revoked' WHERE node_id = $1",
                    self.node.node_id,
                )
                await self.admin.execute(
                    "UPDATE fleet.node_fleet_record SET revoked_at = $2,"
                    " revocation_reason_es = 'Revocación sintética de la máquina'"
                    " WHERE node_id = $1",
                    self.node.node_id,
                    self.now,
                )

        self.world.run(revoke())
        self.revoked = True

    @precondition(lambda self: self.revoked and not self.decommissioned)
    @rule()
    def decommission(self) -> None:
        self.world.run(
            self.admin.execute(
                "UPDATE fleet.node_fleet_record SET decommissioned_at = $2 WHERE node_id = $1",
                self.node.node_id,
                self.now,
            )
        )
        self.decommissioned = True

    @precondition(lambda self: not self.revoked)
    @rule()
    def reassign(self) -> None:
        target = self.spare.pop(0)
        self.world.run(reassign(self.admin, self.node, self.zone, target, self.now))
        self.spare.append(self.zone)
        self.previous_zones.append(self.zone)
        self.zone = target
        # Una zona retirada vuelve a estar libre después del instante de la retirada.
        self.instances.env.clock.advance(1)

    @rule(hours=st.sampled_from([1, 12, 23, 24, 25, 48]))
    def advance(self, hours: int) -> None:
        self.instances.env.clock.advance((hours * HOUR).total_seconds())

    # --- Modelo ---------------------------------------------------------------------------------

    def expected(self, credential: Credential) -> tuple[int, str | None]:
        now = self.now
        if self.revoked or self.decommissioned:
            return 401, "node_revoked"
        if credential.status == "revoked":
            return 401, "node_revoked"
        if credential.status == "overlapping":
            successor = credential.successor_issued_at
            if successor is not None and now < successor + OVERLAP and now < credential.expires_at:
                return 200, None
            return (
                (401, "node_revoked") if now < credential.expires_at else (401, "node_not_enrolled")
            )
        if credential.issued_at <= now < credential.expires_at:
            return 200, None
        return 401, "node_not_enrolled"

    @invariant()
    def every_credential_on_both_instances_reflects_the_last_change(self) -> None:
        for credential in self.credentials[-4:]:
            expected = self.expected(credential)
            for instance in (0, 1):
                response = self.world.get(instance, self.zone, credential.certificate)
                if expected[0] == 200:
                    assert response.status_code == 200, (instance, response.text)
                    assert response.json()["zones"] == [str(self.zone)]
                    for old in self.previous_zones[-1:]:
                        stale = self.world.get(instance, old, credential.certificate)
                        assert stale.status_code == 403, (instance, stale.text)
                        assert parse_rejection_response(stale.content).code.value == (
                            "node_zone_mismatch"
                        )
                else:
                    code = parse_rejection_response(response.content).code.value
                    assert (response.status_code, code) == expected, (instance, credential.status)


def test_pr_gob_28_every_change_is_seen_by_the_first_request_on_both_instances(
    instances: Instances,
) -> None:
    """PR-GOB-28 con cada semilla del perfil activo (``tests/conftest.py``)."""
    for value in _seeds_for_profile():
        seeded = hypothesis_seed(value)(NodeIdentityMachine)
        run_state_machine_as_test(seeded, settings=settings(stateful_step_count=STEPS_PER_EXAMPLE))
