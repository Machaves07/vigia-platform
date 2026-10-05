"""PR-GOB-23: la lista de revocación global va al día salvo mientras su publicación falla.

``RuleBasedStateMachine`` (perfil ``ci``, semillas registradas por ``tests/conftest.py``) con reloj
simulado, la firma real (``NodeCaRevocationListSigner`` sobre una clave de prueba) y el publicador
real (``TrustStorePublisher``) sobre los dobles de ``tests/revocation_list_support``, que fallan a
voluntad en cualquier paso. Dos organizaciones; la fila global y las credenciales en memoria con la
semántica de sus adaptadores de PostgreSQL. La primera capa es la aplicación real de ``node_api``
con su ruta de prueba interna (``tests/node_api_support.node_world``, VIG-144).

Reglas: alta de nodos en cualquiera de las dos organizaciones, **revocación** (primera capa y marca
única en la misma operación, como TASK-218), el publicador que **empieza a fallar** en un paso o se
**restablece**, y el paso del tiempo en minutos con un ciclo de ``regenerate_revocation_list`` cada
60 s (``Schedule.every(60)``).

Invariantes (NFR-GOB-48, FS-GOB-09 en comportamiento):

- siempre hay una lista publicada en el almacén y en ``vigia-edge``;
- si ningún ciclo falló desde la última revocación y ya pasó un ciclo, la lista publicada contiene
  **todas** las revocaciones y su ``last_update`` está a 5 min como mucho de la última;
- mientras falla: la marca persiste mientras haya una revocación sin publicar y
  ``revocation_list_publish_failed`` cuenta cada ciclo fallido;
- siempre: la ruta de prueba rechaza a **todo** nodo revocado con ``node_revoked`` (401) y acepta a
  los demás;
- al restablecerse, el primer ciclo publica y limpia la marca.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from typing import Any

from fastapi.testclient import TestClient
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
from tests.dispatch_support import metric_points, metrics_with_reader
from tests.fleet_credentials_support import MemoryKms, root_bundle_for
from tests.node_api_support import DAY, NodeFixture, node_world, zone_of
from tests.revocation_list_support import (
    TRUST_STORE_ARN,
    FakeTrustStore,
    MemoryCredential,
    MemoryCredentials,
    MemoryEdge,
    MemoryScope,
    MemoryStates,
    crl_of,
)
from vigia_platform.fleet.adapters.ca.crl_signing import NodeCaRevocationListSigner
from vigia_platform.fleet.adapters.ca.trust_store_publisher import TrustStorePublisher
from vigia_platform.fleet.application.revocation_list_task import (
    CycleOutcome,
    RevocationListService,
)
from vigia_platform.fleet.domain.enums import CredentialStatus
from vigia_platform.identity.authz.context import NodeAssignment
from vigia_platform.shared.observability.metrics import MetricName

CATALOG = "/api/nodes/zones/{zone}/catalog"
CYCLE = dt.timedelta(seconds=60)
FRESHNESS = dt.timedelta(minutes=5)
MAX_NODES = 8
FAILING_STEPS = {
    "put_object": "edge",
    "describe": "store",
    "add": "store",
    "describe_ids": "store",
    "remove": "store",
}


class RevocationListMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.world = node_world()
        self.clock = self.world.world.clock
        self.kms = MemoryKms()
        self.edge = MemoryEdge()
        self.store = FakeTrustStore(self.edge.fetch)
        self.states = MemoryStates()
        self.credentials = MemoryCredentials()
        self.organizations = [self.world.a.organization_id, self.world.b.organization_id]
        self.scope = MemoryScope(list(self.organizations))
        self.metrics, self.reader = metrics_with_reader()
        body, _ = asyncio.run(root_bundle_for(self.kms, self.clock.now()))
        self.edge.put_root(body)
        self.service = RevocationListService(
            states=self.states,
            credentials=self.credentials,
            signer=NodeCaRevocationListSigner(
                kms=self.kms, key_id=self.kms.key_id, roots=self.edge
            ),
            publisher=TrustStorePublisher(
                storage=self.edge,
                elb=self.store,
                trust_store_arn=TRUST_STORE_ARN,
                bucket=self.edge.bucket,
            ),
            clock=self.clock,
            metrics=self.metrics,
        )
        self.nodes: list[NodeFixture] = []
        for node in (self.world.a, self.world.b):
            self._track(node)
        self.revoked: dict[int, dt.datetime] = {}
        self.last_revocation: dt.datetime | None = None
        self.last_failure: dt.datetime | None = None
        self.last_cycle_failed = False
        self.failed_cycles = 0
        self.cycles_since_revocation = 0
        self.failing: str | None = None
        self.client = TestClient(self.world.app).__enter__()
        self._cycle()  # la primera publicación (regeneración diaria: nunca publicada)

    # --- ayudas ---------------------------------------------------------------------------------

    def _track(self, node: NodeFixture) -> None:
        certificate = self.world.certificates[node.node_id]
        now = self.clock.now()
        self.credentials.rows.append(
            MemoryCredential(
                organization_id=node.organization_id,
                serial=format(certificate.serial_number, "x"),
                status=CredentialStatus.ACTIVE,
                issued_at=now - DAY,
                expires_at=now + 364 * DAY,
            )
        )
        self.nodes.append(node)

    def _cycle(self) -> None:
        result = asyncio.run(self.service.run_cycle(self.scope))
        assert result.outcome is not CycleOutcome.BUSY
        self.last_cycle_failed = result.outcome is CycleOutcome.FAILED
        if self.last_cycle_failed:
            self.failed_cycles += 1
            self.last_failure = self.clock.now()
        self.cycles_since_revocation += 1

    def _published(self) -> tuple[set[int], dt.datetime]:
        (current,) = self.store.current()[-1:]
        crl = crl_of(self.edge.fetch(current.bucket, current.key, current.version))
        return {entry.serial_number for entry in crl}, crl.last_update_utc

    # --- reglas ---------------------------------------------------------------------------------

    @precondition(lambda self: len(self.nodes) < MAX_NODES)
    @rule(organization=st.sampled_from([0, 1]))
    def add_node(self, organization: int) -> None:
        template = (self.world.a, self.world.b)[organization]
        node = NodeFixture(
            uuid.uuid4(),
            template.organization_id,
            template.plant_id,
            enrolled_at=self.clock.now() - DAY,
        )
        node.assignments.append(NodeAssignment(zone_of(template), self.clock.now() - DAY, None))
        self.world.store.nodes[node.node_id] = node
        self.world.credential(node)
        self._track(node)

    @rule(data=st.data())
    def revoke(self, data: st.DataObject) -> None:
        live = [node for node in self.nodes if node.revoked_at is None]
        if not live:
            return
        node = data.draw(st.sampled_from(live))
        now = self.clock.now()
        serial = self.world.certificates[node.node_id].serial_number
        # Primera capa y marca única en la misma operación (revoke_in de TASK-218).
        node.node_status = "revoked"
        node.revoked_at = now
        node.credentials[format(serial, "x")]["status"] = "revoked"
        self.credentials.revoke(format(serial, "x"), now)
        self.states.mark_dirty(now)
        self.revoked[serial] = now
        self.last_revocation = now
        self.cycles_since_revocation = 0

    @rule(step=st.sampled_from(sorted(FAILING_STEPS)))
    def publisher_starts_failing(self, step: str) -> None:
        self._restore()
        target = self.edge if FAILING_STEPS[step] == "edge" else self.store
        target.switch.fail.add(step)
        self.failing = step

    @rule()
    def publisher_recovers(self) -> None:
        self._restore()

    def _restore(self) -> None:
        self.edge.switch.fail.clear()
        self.store.switch.fail.clear()
        self.failing = None

    @rule(minutes=st.integers(1, 6))
    def time_passes(self, minutes: int) -> None:
        was_failing = self.last_cycle_failed
        for _ in range(minutes):
            self.clock.advance(CYCLE.total_seconds())
            self._cycle()
            if was_failing and not self.last_cycle_failed:
                # Al restablecerse, el primer ciclo publica y limpia la marca.
                assert not self.states.dirty
                assert set(self.revoked) <= self._published()[0]
            was_failing = self.last_cycle_failed

    # --- invariantes ----------------------------------------------------------------------------

    @invariant()
    def a_published_list_always_exists(self) -> None:
        assert self.store.current()
        assert self.edge.versions.get("ca/crl.pem")

    @invariant()
    def fresh_unless_publication_failed(self) -> None:
        if self.last_revocation is None or self.cycles_since_revocation == 0:
            return
        if self.last_failure is not None and self.last_failure >= self.last_revocation:
            return
        serials, last_update = self._published()
        assert set(self.revoked) <= serials
        assert dt.timedelta(0) <= last_update - self.last_revocation.replace(microsecond=0)
        assert last_update - self.last_revocation <= FRESHNESS

    @invariant()
    def while_failing_the_mark_persists_and_the_metric_counts(self) -> None:
        unpublished = set(self.revoked) - self._published()[0]
        if unpublished:
            assert self.states.dirty
        counted = sum(
            value
            for _, value in metric_points(self.reader, MetricName.REVOCATION_LIST_PUBLISH_FAILED)
        )
        assert counted == self.failed_cycles

    @invariant()
    def the_first_layer_rejects_every_revoked_node(self) -> None:
        for node in self.nodes:
            response = self.client.get(
                CATALOG.format(zone=zone_of(node)), headers=self.world.headers(node)
            )
            if node.revoked_at is None:
                assert response.status_code == 200, response.text
            else:
                rejection = parse_rejection_response(response.content)
                assert (response.status_code, rejection.code.value) == (401, "node_revoked")

    def teardown(self) -> None:
        self.client.__exit__(None, None, None)


def test_pr_gob_23_revocation_list_stays_fresh_unless_publication_fails() -> None:
    for value in _seeds_for_profile():
        seeded: Any = hypothesis_seed(value)(RevocationListMachine)
        run_state_machine_as_test(
            seeded, settings=settings(max_examples=25, stateful_step_count=20, deadline=None)
        )
