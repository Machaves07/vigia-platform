"""PR-GOB-28 con las credenciales reales: cada cambio se ve en la primera petición (TASK-219;
BR-GOB-64, 66; NFR-GOB-34; BLM §3.5).

``RuleBasedStateMachine`` sobre PostgreSQL 16 real con **dos instancias** de la aplicación de las
rutas del contrato (``create_app`` con la cadena fija y su ``NodeApiGate``), cada una con su propio
``Database`` (sus conexiones) sobre la misma base, como dos tareas detrás del balanceador. Las altas
y las rotaciones pasan por las rutas reales de TASK-219 (``POST enrollment`` y ``POST
credential-rotations``) en una instancia elegida por la prueba; el resto de la secuencia generada
lo escribe la prueba con las transiciones que hacen las operaciones de TASK-218 y que admite
``gob_0018``: emisión de código (y re-alta: nodo ``re_enrollment_pending`` y credenciales
revocadas), **revocación** del nodo (con sus credenciales), **baja**, **reasignación** de zona y
**avances del reloj** (para cruzar las 24 h del solapamiento). Así el reloj simulado puede adelantar
días sin pasar por la seguridad a nivel de fila de las concesiones, que compara con la hora de la
base.

Tras cada paso, la **primera** petición de cada credencial en **cada** instancia (la ruta de prueba
interna de TASK-206 y ``credential-rotations``) refleja el modelo: nunca se acepta una credencial
revocada, sustituida, vencida o fuera de su solapamiento de 24 h, ni un nodo revocado o dado de
baja, y el alcance es exactamente la zona asignada en ese instante. Con una credencial
``overlapping`` dentro del solapamiento la identidad la acepta, pero la rotación no
(``node_revoked``).

Semillas del perfil (``tests/conftest.py``): la fija y la de la sesión.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, ClassVar, Final

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

from tests.conftest import _seeds_for_profile
from tests.fleet_enrollment_support import (
    ENROLLMENT_PATH,
    ROTATION_PATH,
    EnrollmentWorld,
    NodeSetup,
    enrollment_world,
)
from tests.integration.conftest import PostgresEndpoint
from tests.node_api_db import insert_zone
from tests.node_api_support import VERSION, alb_headers
from tests.outbox_support import app_database
from vigia_platform.fleet.adapters.postgres.node_fleet_store import PostgresNodeFleetStore
from vigia_platform.fleet.domain.enrollment_code import generate_code, new_salt, salted_hash
from vigia_platform.identity.authz.context import OVERLAP
from vigia_platform.shared.db import Database

pytestmark = pytest.mark.integration

STEPS_PER_EXAMPLE: Final = 10
HOUR: Final = dt.timedelta(hours=1)
CHECKED: Final = 3
"""Credenciales del final que se comprueban tras cada paso (las anteriores ya no cambian)."""


@dataclass
class Credential:
    certificate: x509.Certificate
    status: str
    issued_at: dt.datetime
    expires_at: dt.datetime
    successor_issued_at: dt.datetime | None = None


@dataclass
class Instances:
    world: EnrollmentWorld
    second: Database
    clients: tuple[httpx.AsyncClient, httpx.AsyncClient]
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    user_id: uuid.UUID

    def run(self, awaitable: Any) -> Any:
        return self.world.run(awaitable)


@pytest.fixture(scope="module")
def instances(postgres_endpoint: PostgresEndpoint) -> Iterator[Instances]:
    with enrollment_world(postgres_endpoint, "pr_gob_28_credentials") as world:
        fleet = world.fleet
        second = app_database(
            fleet.authz.sessions.migrated,
            worker_pool_size=4,
            lock_timeout_ms=60_000,
            pool_timeout_seconds=60.0,
        )
        # La segunda instancia: su propia base (sus conexiones) para la identidad, el alta y la
        # rotación; el escritor del expediente escribe en la transacción que recibe.
        deps = dataclasses.replace(
            fleet.deps, database=second, nodes=PostgresNodeFleetStore(second)
        )
        enrollment, rotation = world.services(deps=deps)
        _, second_app = world.app(enrollment, rotation, database=second)
        clients = (
            world.client,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=second_app),
                base_url="https://nodes.vigia.test",
                timeout=60,
            ),
        )
        site = fleet.site(plants=1, zones=0)
        plant = next(iter(site.plants))
        user = fleet.authz.add_user(site.organization_id)
        built = Instances(world, second, clients, site.organization_id, plant, user)
        CredentialMachine.instances = built
        try:
            yield built
        finally:
            world.run(clients[1].aclose())
            world.run(second.dispose())


class CredentialMachine(RuleBasedStateMachine):
    instances: ClassVar[Instances]

    def __init__(self) -> None:
        super().__init__()
        bed = self.instances
        self.bed = bed
        self.world = bed.world
        admin = self.world.fleet.authz.sessions.admin
        self.admin = admin
        zones = [
            bed.run(insert_zone(admin, bed.organization_id, bed.plant_id, bed.user_id))
            for _ in range(3)
        ]
        self.node_id = uuid.uuid4()
        self.setup = NodeSetup(
            self.node_id, bed.organization_id, bed.plant_id, (zones[0],), installer=None
        )
        for zone in zones:
            self.world.publish(self.setup, zone)
        self.zone = zones[0]
        self.spare = zones[1:]
        self.previous_zones: list[uuid.UUID] = []
        bed.run(self._declare())
        self.status = "declared"
        self.decommissioned = False
        self.credentials: list[Credential] = []

    @property
    def now(self) -> dt.datetime:
        return self.world.now()

    # --- Escrituras de TASK-218 (las transiciones que hace la operación, en SQL) ---------------

    async def _declare(self) -> None:
        bed, now = self.bed, self.now
        async with self.admin.transaction():
            await self.admin.execute(
                "INSERT INTO identity.node_identity (node_id, organization_id, plant_id, code,"
                " status, created_at) VALUES ($1, $2, $3, $4, 'declared', $5)",
                self.node_id,
                bed.organization_id,
                bed.plant_id,
                f"ND-{secrets.token_hex(8).upper()}",
                now,
            )
            await self.admin.execute(
                "INSERT INTO identity.zone_node_assignment (assignment_id, organization_id,"
                " plant_id, zone_id, node_id, assigned_at, assigned_by)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7)",
                uuid.uuid4(),
                bed.organization_id,
                bed.plant_id,
                self.zone,
                self.node_id,
                now,
                bed.user_id,
            )
            await self.admin.execute(
                "INSERT INTO fleet.node_fleet_record (node_id, organization_id, plant_id,"
                " declared_at, declared_by) VALUES ($1, $2, $3, $4, $5)",
                self.node_id,
                bed.organization_id,
                bed.plant_id,
                now,
                bed.user_id,
            )

    async def _issue_code(self) -> str:
        code, salt, now = generate_code(), new_salt(), self.now
        async with self.admin.transaction():
            await self.admin.execute(
                "UPDATE fleet.enrollment_code SET status = 'superseded'"
                " WHERE node_id = $1 AND status = 'active'",
                self.node_id,
            )
            await self.admin.execute(
                "INSERT INTO fleet.enrollment_code (code_id, organization_id, plant_id, node_id,"
                " code_hash, code_salt, issued_at, issued_by, expires_at, disclosed_at, status,"
                " ledger_record_id) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $7, 'active', $10)",
                uuid.uuid4(),
                self.bed.organization_id,
                self.bed.plant_id,
                self.node_id,
                salted_hash(salt, code),
                salt,
                now,
                self.bed.user_id,
                now + 24 * HOUR,
                uuid.uuid4(),
            )
        return code

    # --- Reglas ---------------------------------------------------------------------------------

    @precondition(
        lambda self: (
            self.status in ("declared", "re_enrollment_pending") and not self.decommissioned
        )
    )
    @rule(instance=st.sampled_from([0, 1]))
    def enroll(self, instance: int) -> None:
        code = self.bed.run(self._issue_code())
        body = self.world.body(self.setup, code)
        response = self.bed.run(
            self.bed.clients[instance].post(
                ENROLLMENT_PATH, json=body, headers={"X-Vigia-Contract-Version": VERSION}
            )
        )
        assert response.status_code == 200, (instance, response.text)
        certificate = x509.load_pem_x509_certificate(response.json()["certificate"].encode())
        self.credentials.append(
            Credential(certificate, "active", self.now, certificate.not_valid_after_utc)
        )
        self.status = "enrolled"

    @precondition(lambda self: self.status == "enrolled" and self._active() is not None)
    @rule(instance=st.sampled_from([0, 1]))
    def rotate(self, instance: int) -> None:
        self.world.advance(1)
        current = self._active()
        assert current is not None
        response = self.bed.run(
            self.bed.clients[instance].post(
                ROTATION_PATH,
                json=self.world.rotation_body(self.node_id),
                headers=self._headers(current.certificate),
            )
        )
        assert response.status_code == 200, (instance, response.text)
        certificate = x509.load_pem_x509_certificate(response.json()["certificate"].encode())
        now = self.now
        for credential in self.credentials:
            if credential.status == "overlapping" and not self._in_window(credential, now):
                credential.status = "superseded"
        current.status = "overlapping"
        current.successor_issued_at = now
        self.credentials.append(
            Credential(certificate, "active", now, certificate.not_valid_after_utc)
        )

    @precondition(lambda self: self.status in ("enrolled", "re_enrollment_pending"))
    @rule()
    def revoke_node(self) -> None:
        async def revoke() -> None:
            async with self.admin.transaction():
                await self.admin.execute(
                    "UPDATE identity.node_identity SET status = 'revoked' WHERE node_id = $1",
                    self.node_id,
                )
                await self.admin.execute(
                    "UPDATE fleet.node_credential SET status = 'revoked', revoked_at = $2"
                    " WHERE node_id = $1 AND status IN ('active', 'overlapping')",
                    self.node_id,
                    self.now,
                )
                await self.admin.execute(
                    "UPDATE fleet.enrollment_code SET status = 'superseded'"
                    " WHERE node_id = $1 AND status = 'active'",
                    self.node_id,
                )
                await self.admin.execute(
                    "UPDATE fleet.node_fleet_record SET revoked_at = $2,"
                    " revocation_reason_es = 'Revocación sintética de la máquina'"
                    " WHERE node_id = $1",
                    self.node_id,
                    self.now,
                )

        self.bed.run(revoke())
        self._revoke_live()
        self.status = "revoked"

    @precondition(lambda self: self.status == "revoked" and not self.decommissioned)
    @rule()
    def issue_re_enrollment_code(self) -> None:
        async def reopen() -> None:
            async with self.admin.transaction():
                await self.admin.execute(
                    "UPDATE identity.node_identity SET status = 're_enrollment_pending'"
                    " WHERE node_id = $1",
                    self.node_id,
                )
                await self.admin.execute(
                    "UPDATE fleet.node_fleet_record SET revoked_at = NULL,"
                    " revocation_reason_es = NULL WHERE node_id = $1",
                    self.node_id,
                )

        self.bed.run(reopen())
        self.status = "re_enrollment_pending"

    @precondition(lambda self: self.status == "revoked" and not self.decommissioned)
    @rule()
    def decommission(self) -> None:
        self.bed.run(
            self.admin.execute(
                "UPDATE fleet.node_fleet_record SET decommissioned_at = $2 WHERE node_id = $1",
                self.node_id,
                self.now,
            )
        )
        self.decommissioned = True

    @precondition(lambda self: self.status != "revoked" and not self.decommissioned)
    @rule()
    def reassign(self) -> None:
        from tests.node_api_db import DbNode, reassign

        self.world.advance(1)  # la retirada exige unassigned_at > assigned_at
        target = self.spare.pop(0)
        node = DbNode(
            self.node_id, self.bed.organization_id, self.bed.plant_id, self.zone, self.bed.user_id
        )
        self.bed.run(reassign(self.admin, node, self.zone, target, self.now))
        self.spare.append(self.zone)
        self.previous_zones.append(self.zone)
        self.zone = target
        self.setup = NodeSetup(
            self.node_id,
            self.bed.organization_id,
            self.bed.plant_id,
            (target,),
            installer=None,
            fingerprint=self.setup.fingerprint,
        )
        self.world.advance(1)

    @rule(hours=st.sampled_from([1, 12, 23, 24, 25, 48]))
    def advance(self, hours: int) -> None:
        self.world.advance((hours * HOUR).total_seconds())

    # --- Modelo ---------------------------------------------------------------------------------

    def _active(self) -> Credential | None:
        active = [c for c in self.credentials if c.status == "active"]
        return active[-1] if active else None

    def _revoke_live(self) -> None:
        for credential in self.credentials:
            if credential.status in ("active", "overlapping"):
                credential.status = "revoked"

    @staticmethod
    def _in_window(credential: Credential, now: dt.datetime) -> bool:
        successor = credential.successor_issued_at
        return successor is not None and now < successor + OVERLAP and now < credential.expires_at

    @staticmethod
    def _headers(certificate: x509.Certificate) -> dict[str, str]:
        return {**alb_headers(certificate), "X-Vigia-Contract-Version": VERSION}

    def expected(self, credential: Credential) -> tuple[int, str | None]:
        now = self.now
        if self.status == "revoked" or self.decommissioned:
            return 401, "node_revoked"
        if credential.status in ("revoked", "superseded"):
            return 401, "node_revoked"
        if self.status != "enrolled":
            return 401, "node_not_enrolled"
        if credential.status == "overlapping":
            if self._in_window(credential, now):
                return 200, None
            return (
                (401, "node_revoked") if now < credential.expires_at else (401, "node_not_enrolled")
            )
        if credential.issued_at <= now < credential.expires_at:
            return 200, None
        return 401, "node_not_enrolled"

    @invariant()
    def the_first_request_on_both_instances_reflects_every_change(self) -> None:
        for credential in self.credentials[-CHECKED:]:
            expected = self.expected(credential)
            headers = self._headers(credential.certificate)
            for instance in (0, 1):
                client = self.bed.clients[instance]
                response = self.bed.run(
                    client.get(f"/api/nodes/zones/{self.zone}/catalog", headers=headers)
                )
                if expected[0] == 200:
                    assert response.status_code == 200, (instance, response.text)
                    assert response.json()["zones"] == [str(self.zone)]
                    for old in self.previous_zones[-1:]:
                        stale = self.bed.run(
                            client.get(f"/api/nodes/zones/{old}/catalog", headers=headers)
                        )
                        assert (stale.status_code, _code(stale)) == (403, "node_zone_mismatch")
                else:
                    assert (response.status_code, _code(response)) == expected, (
                        instance,
                        credential.status,
                    )
                if credential.status == "active" and expected[0] == 200:
                    continue
                # La rotación nunca se acepta con una credencial que no es la active vigente.
                rotation = self.bed.run(
                    client.post(
                        ROTATION_PATH, json=self.world.rotation_body(self.node_id), headers=headers
                    )
                )
                assert rotation.status_code == 401, (instance, rotation.text)
                assert _code(rotation) == (expected[1] or "node_revoked")


def _code(response: httpx.Response) -> str:
    return parse_rejection_response(response.content).code.value


def test_pr_gob_28_every_credential_change_is_seen_by_the_first_request_on_both_instances(
    instances: Instances,
) -> None:
    """PR-GOB-28 con cada semilla del perfil activo (``tests/conftest.py``)."""
    for value in _seeds_for_profile():
        seeded = hypothesis_seed(value)(CredentialMachine)
        run_state_machine_as_test(seeded, settings=settings(stateful_step_count=STEPS_PER_EXAMPLE))
