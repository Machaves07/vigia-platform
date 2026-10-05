"""PR-GOB-15: el código de alta de un solo uso frente a un modelo (TASK-218; BR-GOB-58, 60; G-2).

``EnrollmentCodeService`` real (emisión, verificación, consumo y registro de intentos) sobre los
almacenes en memoria de ``tests/fleet_support.py``, que imponen lo que la base garantiza con sus
restricciones (un solo ``active`` por nodo, estados solo hacia adelante). Para cualquier secuencia
generada de emisiones, presentaciones (el código vigente, uno anterior, el de otro nodo, uno
inventado) y avances del reloj (también justo hasta el borde de las 24 h):

- **a lo sumo un uso por código**: nunca se consume dos veces;
- **toda reemisión deja la anterior ``superseded``** (si seguía ``active``);
- **nunca se acepta** un código ``used``, ``expired`` (derivado del reloj) o ``superseded``: el
  resultado coincide con el del modelo, que solo acepta el último emitido para ese nodo, sin usar
  y antes de su vencimiento;
- **nunca hay dos ``active`` para el mismo nodo**;
- el código en claro nunca aparece en los registros del expediente, la auditoría ni los intentos.

Generadores: ``enrollment_commands`` (U-03) y ``enrollment_requests`` (lo que presenta el alta;
ver ``strategies/enrollment.py``). Los almacenes en memoria no prueban la concurrencia: eso lo
hace ``tests/integration/test_fleet_enrollment_code_concurrency.py`` contra PostgreSQL 16.

Semilla registrada: con el perfil ``ci`` corre con la semilla fija del proyecto y con la de la
sesión (que se imprime al final de pytest para reproducir).
"""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from hypothesis import seed as hypothesis_seed
from hypothesis import settings
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule, run_state_machine_as_test

from tests.conftest import _seeds_for_profile
from tests.fleet_support import FakeFleet, FakeTransaction
from tests.properties.gob.strategies.enrollment import (
    NODES,
    EnrollmentCommand,
    PresentedRequest,
    enrollment_commands,
    enrollment_requests,
)
from vigia_platform.fleet.application.enrollment_codes import (
    AttemptRequest,
    EnrollmentCodeService,
)
from vigia_platform.fleet.domain.enrollment_attempt import SourceIpHasher
from vigia_platform.fleet.domain.enrollment_code import VALIDITY
from vigia_platform.fleet.domain.enums import EnrollmentAttemptResult, EnrollmentCodeStatus
from vigia_platform.shared.observability.metrics import get_metrics

START = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
ROOTS = ("ab" * 32,)


class _Roots:
    async def fingerprints(self) -> tuple[str, ...]:
        return ROOTS


@dataclass
class Issued:
    code: str
    code_id: uuid.UUID
    issued_at: datetime
    used: bool = False


@dataclass
class Model:
    issued: dict[uuid.UUID, list[Issued]] = field(default_factory=dict)

    def expected(self, node_id: uuid.UUID, issued: Issued | None, now: datetime) -> str:
        if issued is None:
            return EnrollmentAttemptResult.ENROLLMENT_CODE_INVALID.value
        latest = self.issued[node_id][-1]
        if issued.used:
            return EnrollmentAttemptResult.ENROLLMENT_CODE_USED.value
        if issued is not latest:
            # Una reemisión la dejó superseded antes de usarse o vencer: como desconocida.
            return EnrollmentAttemptResult.ENROLLMENT_CODE_INVALID.value
        if now >= issued.issued_at + VALIDITY:
            return EnrollmentAttemptResult.ENROLLMENT_CODE_EXPIRED.value
        return EnrollmentAttemptResult.ACCEPTED.value


class EnrollmentMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.fleet = FakeFleet.build(START, NODES, get_metrics())
        self.service = EnrollmentCodeService(
            self.fleet.deps, roots=_Roots(), source_hasher=SourceIpHasher(secrets.token_bytes(32))
        )
        self.node_ids = list(self.fleet.nodes)
        self.model = Model({node: [] for node in self.node_ids})
        self.plain_codes: set[str] = set()

    def teardown(self) -> None:
        self.loop.close()

    def _run[T](self, coroutine: object) -> T:
        return self.loop.run_until_complete(coroutine)  # type: ignore[arg-type, no-any-return]

    def _issue(self, node_id: uuid.UUID) -> None:
        before = [code.code_id for code in self.fleet.enrollment.active_of(node_id)]
        issued = self._run(self.service.issue(self.fleet.person, node_id))
        assert issued.node_ca_root_sha256 == ROOTS
        assert issued.expires_at == issued.issued_at + VALIDITY == issued.disclosed_at + VALIDITY
        self.model.issued[node_id].append(Issued(issued.code, issued.code_id, issued.issued_at))
        self.plain_codes.add(issued.code)
        # Toda reemisión deja la anterior superseded.
        for code_id in before:
            assert (
                self.fleet.enrollment.codes_by_id[code_id].status is EnrollmentCodeStatus.SUPERSEDED
            )

    def _present(self, command: EnrollmentCommand, request: PresentedRequest) -> None:
        node_id = self.node_ids[command.node]
        history = self.model.issued[node_id]
        target: Issued | None = None
        presented = request.invented_code
        if command.which == "current" and history:
            target = history[-1]
        elif command.which == "previous" and len(history) > command.age:
            target = history[-1 - command.age]
        elif command.which == "other_node":
            other = self.model.issued[self.node_ids[(command.node + 1) % NODES]]
            if other:
                presented = other[-1].code
        if target is not None:
            presented = target.code
        elif any(item.code == presented for item in history):
            target = next(item for item in history if item.code == presented)
        now = self.fleet.clock.now()
        expected = self.model.expected(node_id, target, now)
        scope = self.fleet.enrollment_scope(node_id)
        check = self._run(self.service.verify(scope, presented))
        result = check.result
        if check.valid:
            assert check.code is not None
            consumed = self._run(
                self.service.consume(FakeTransaction(scope.context), check.code.code_id, now)
            )
            assert consumed, "un código válido se consume"
            # Consumir otra vez el mismo código nunca vuelve a funcionar.
            assert not self._run(
                self.service.consume(FakeTransaction(scope.context), check.code.code_id, now)
            )
        assert result.value == expected, (command, result, expected)
        if target is not None and result is EnrollmentAttemptResult.ACCEPTED:
            target.used = True
        self._run(
            self.service.register_attempt(
                scope,
                AttemptRequest(
                    presented_code=presented,
                    hardware_fingerprint=request.hardware_fingerprint,
                    software_version=request.software_version,
                    contract_version=request.contract_version,
                    source_address=request.source_address,
                    correlation_id=uuid.uuid4(),
                ),
                result,
            )
        )

    @rule(command=enrollment_commands(), request=enrollment_requests())
    def step(self, command: EnrollmentCommand, request: PresentedRequest) -> None:
        if command.kind == "issue":
            self._issue(self.node_ids[command.node])
        elif command.kind == "present":
            self._present(command, request)
        else:
            self.fleet.clock.advance(command.seconds)

    @invariant()
    def at_most_one_active_per_node(self) -> None:
        for node_id in self.node_ids:
            assert len(self.fleet.enrollment.active_of(node_id)) <= 1

    @invariant()
    def at_most_one_use_per_code(self) -> None:
        assert all(uses == 1 for uses in self.fleet.enrollment.uses.values())

    @invariant()
    def the_plain_code_is_never_kept(self) -> None:
        kept = json.dumps(
            [
                [record[0], record[1], [repr(event) for event in record[2]]]
                for record in self.fleet.writer.records
            ]
            + [repr(entry) for entry in self.fleet.audit.entries]
            + [repr(attempt) for attempt in self.fleet.enrollment.attempts_list]
            + [repr(code) for code in self.fleet.enrollment.codes_by_id.values()],
            default=str,
        )
        assert not any(code in kept for code in self.plain_codes)

    @invariant()
    def every_rejection_is_in_the_ledger(self) -> None:
        rejected = [a for a in self.fleet.enrollment.attempts_list if a.rejected]
        assert all(attempt.ledger_record_id is not None for attempt in rejected)
        recorded = [r for r in self.fleet.writer.records if r[0] == "enrollment_attempt_rejected"]
        assert len(recorded) == len(rejected)


def test_pr_gob_15_enrollment_code_matches_the_model() -> None:
    for value in _seeds_for_profile():
        seeded = hypothesis_seed(value)(EnrollmentMachine)
        run_state_machine_as_test(seeded, settings=settings(stateful_step_count=50))
