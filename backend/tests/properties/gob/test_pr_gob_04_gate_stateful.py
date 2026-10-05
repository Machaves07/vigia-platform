"""PR-GOB-04 como ``RuleBasedStateMachine``: ninguna aceptación en un intervalo sin uso aprobado.

BR-GOB-92 (G-4, P9; TASK-221). La máquina intercala, sobre la aplicación real y el
``IngestService`` real (dobles de ``tests/ingest_support.py``):

- **cambios de compuerta** de ``gate_sequences`` (TASK-211): aprobar o revocar el montaje o el uso,
  con el reloj avanzando 0 ms, 1 ms, 1 s o 1 h (un cambio en el mismo instante que el anterior se
  corre 1 ms, como el relevo de la plataforma); revocar lo que no está aprobado no cambia nada, y el
  montaje no cuenta para la ingesta;
- **presentaciones** del kit de U-01 (hallazgo, detección para revisión y evento de observabilidad)
  con ``node_time.started_at`` en distintos puntos alrededor de ahora y el reloj declarado de
  ``clock_offsets``.

**Propiedades**: un hallazgo o una detección se acepta **si y solo si** el oráculo independiente
(``UsageHistory``) dice que el uso estuvo aprobado en algún instante de la ventana de la decisión
(``[t - tol, t + tol]``, o el instante de recepción sin reloj sincronizado; la misma lectura que
PR-GOB-17); si no, ``zone_gate_not_approved`` con su ``ingest_rejected``. Los eventos de
observabilidad
se aceptan **en todo modo**. **Invariante**: ningún ``finding_received`` ni
``detection_for_review_received`` del expediente tiene su ventana fuera de un uso aprobado.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from hypothesis import HealthCheck, settings
from hypothesis import seed as hypothesis_seed
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
    run_state_machine_as_test,
)
from vigia_contracts.conformance.generators import (
    detection_for_review,
    finding,
    observability_event,
    zone_catalog,
)
from vigia_contracts.models.api import parse_rejection_response

from tests.conftest import _seeds_for_profile
from tests.ingest_support import IngestWorld, ingest_world, place
from tests.properties.gob.strategies.clock import (
    ClockDeclaration,
    UsageHistory,
    clock_offsets,
    expected_window,
    fact_offsets,
)
from tests.properties.gob.strategies.gates import STEPS, Approve, Revoke
from vigia_platform.catalog.domain.enums import GateKind
from vigia_platform.fleet.domain.ingest_order import IngestKind
from vigia_platform.shared.ids import uuid7

YEAR = dt.timedelta(days=365)
MILLISECOND = dt.timedelta(milliseconds=1)

COMMANDS = st.one_of(
    st.builds(Approve, st.sampled_from(tuple(GateKind))),
    st.builds(Revoke, st.sampled_from(tuple(GateKind))),
)


class IngestGateMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.world: IngestWorld = ingest_world()
        self.history = UsageHistory()
        self.catalog: dict[str, Any] = {}
        self.windows: dict[str, tuple[dt.datetime, dt.datetime, dt.datetime]] = {}
        """Clave aceptada → ventana de la decisión y su instante de recepción."""
        self.gate_rejections = 0
        self.last_instant = self.world.now - YEAR

    @initialize(catalog=zone_catalog())
    def publish(self, catalog: dict[str, Any]) -> None:
        self.catalog = self.world.scoped(catalog)
        self.world.publish_catalog(self.catalog, self.world.now - YEAR)
        self.world.open()

    def teardown(self) -> None:
        self.world.close()

    @rule(advance=st.sampled_from(STEPS), command=COMMANDS)
    def change_gate(self, advance: dt.timedelta, command: Approve | Revoke) -> None:
        self.world.advance(advance)
        if command.gate is not GateKind.USAGE:
            return
        approve = isinstance(command, Approve)
        if not approve and not self.history.approved_now:
            return  # revocar lo que no está aprobado se rechaza y no cambia nada
        at = self.world.now
        if at <= self.last_instant:
            # El relevo en el mismo instante suma 1 ms (como la plataforma), y una transición se
            # decide después de lo que ya se recibió en ese milisegundo.
            self.world.advance(self.last_instant + MILLISECOND - at)
            at = self.world.now
        self.last_instant = at
        self.history.change(at, approve)
        self.world.set_usage(approve, at)

    @rule(
        data=st.data(),
        kind=st.sampled_from(tuple(IngestKind)),
        offset=fact_offsets(),
        clock=clock_offsets(),
    )
    def submit(
        self, data: st.DataObject, kind: IngestKind, offset: dt.timedelta, clock: ClockDeclaration
    ) -> None:
        node_id = str(self.world.a.node_id)
        strategy = {
            IngestKind.FINDING: finding(self.catalog, node_id=node_id),
            IngestKind.DETECTION_FOR_REVIEW: detection_for_review(self.catalog, node_id=node_id),
            IngestKind.OBSERVABILITY_EVENT: observability_event(self.catalog, node_id=node_id),
        }[kind]
        received_at = self.world.now
        started = received_at - offset
        document = place(
            data.draw(strategy, label="document"),
            started,
            synchronized=clock.synchronized,
            offset_ms=clock.offset_ms,
        )
        # Cada presentación es un hecho nuevo (el kit repite identificadores al reducir).
        document[kind.id_field] = str(uuid7(self.world.world.clock))
        response = self.world.post(kind, document)
        self.last_instant = max(self.last_instant, received_at)
        if not kind.gated:
            assert response.status_code == 200, response.text  # en cualquier modo (BR-GOB-92)
            return
        start, end = expected_window(started, clock, received_at)
        if self.history.approved_during(start, end):
            assert response.status_code == 200, response.text
            self.windows[document[kind.id_field]] = (start, end, received_at)
            return
        assert response.status_code == 403, response.text
        assert parse_rejection_response(response.content).code.value == "zone_gate_not_approved"
        self.gate_rejections += 1
        assert len(self.world.writer.rejections) == self.gate_rejections

    @invariant()
    def no_finding_outside_an_approved_use(self) -> None:
        """Todo hallazgo o detección del expediente se aceptó con el uso aprobado en su ventana, y
        una ventana ya pasada al recibirlo sigue aprobada en la historia (nada la reescribe)."""
        for (_, record_type, key), _record in self.world.store.records.items():
            if record_type == IngestKind.OBSERVABILITY_EVENT.record_type:
                continue
            start, end, received_at = self.windows[key]
            if end <= received_at:
                assert self.history.approved_during(start, end), (record_type, key)


STEPS_PER_EXAMPLE = 20


def test_pr_gob_04_no_acceptance_in_an_interval_without_approved_use() -> None:
    """La máquina corre con cada semilla del perfil activo (semilla registrada, ``conftest``)."""
    for value in _seeds_for_profile():
        seeded = hypothesis_seed(value)(IngestGateMachine)
        run_state_machine_as_test(
            seeded,
            settings=settings(
                stateful_step_count=STEPS_PER_EXAMPLE,
                suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
            ),
        )
