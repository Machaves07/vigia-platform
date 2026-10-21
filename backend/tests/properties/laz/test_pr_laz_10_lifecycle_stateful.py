"""PR-LAZ-10: oráculo estatal independiente del ciclo de vida del hallazgo.

El modelo de referencia se escribió desde DE §4.1 y §4.2 y no importa la máquina de producción.
Ambas máquinas recorren los catorce comandos; la de integración compara tras cada paso los
servicios reales y PostgreSQL con el oráculo, usando un único reloj inyectado.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, Final, cast

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import seed as hypothesis_seed
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule, run_state_machine_as_test

from tests.conftest import _seeds_for_profile
from tests.examples.regressions.test_pr_laz_10_lifecycle import REGRESSIONS
from tests.integration.conftest import PostgresEndpoint
from tests.loop_actions_support import actions_stack
from tests.loop_deadlines_support import CONFIGURATION_REASON
from tests.loop_unclosable_support import (
    ALTERNATIVE,
    COMPENSATORY,
    DECISION_DATE,
    LICENSE,
    REVIEW_NOTE,
    Gates,
)
from tests.loop_unclosable_support import (
    REASON as UNCLOSABLE_REASON,
)
from tests.properties.laz.lifecycle_model import LifecycleModel
from tests.properties.laz.strategies.lifecycle import (
    AddEvidence,
    Assign,
    ChangeDeadlineRange,
    ClassifyFinding,
    CloseFinding,
    DeactivateAssignee,
    DeclareUnclosable,
    LateSegment,
    LifecycleCommand,
    Reclassify,
    ReopenFinding,
    ResolveQueue,
    ReviewUnclosable,
    RevokeGate,
    Tick,
    lifecycle_command_sequences,
)
from vigia_platform.catalog.adapters.postgres.query_ports import catalog_query_ports
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.loop.application.deadlines import DeadlineEvaluator
from vigia_platform.loop.application.inbox import InboxService
from vigia_platform.loop.application.review_queue import ReviewQueueService
from vigia_platform.loop.application.unclosable import UnclosableService, add_months
from vigia_platform.shared.clock import SystemClock
from vigia_platform.shared.context import Role
from vigia_platform.shared.signing.keys import format_timestamp

_STEPS: Final = 50
_PLATFORM_STEPS: Final = 12
"""Hasta 12 comandos por secuencia CI (≤ 50); nightly amplía también la longitud a 50.
Las cuatro combinaciones entrada/semilla CI exploran 32 secuencias, no un único ejemplo afortunado.
El tiempo real se publica en la salida del runner, sin confundirlo con el job entero.
"""
SERVICE_EXAMPLES: Final = 8
"""Secuencias reales por semilla, como las demás máquinas estatales con PostgreSQL."""


def _service_examples() -> int:
    """Amplía la exploración nocturna sin alterar el presupuesto de CI."""

    if settings.get_current_profile_name() == "nightly":
        return SERVICE_EXAMPLES * 2
    return SERVICE_EXAMPLES


def _apply(model: LifecycleModel, command: LifecycleCommand) -> str | None:
    """Aplica los valores puros de la estrategia al modelo de la tabla, sin producción."""
    match command:
        case ClassifyFinding(outcome=outcome, reinforced=reinforced):
            return model.classify(outcome, reinforced)
        case Reclassify(outcome=outcome):
            return model.classify(outcome, reinforced=True)
        case ResolveQueue(outcome=outcome):
            return model.resolve_queue(outcome)
        case Assign(deadline_days=days):
            return model.assign_action(model.deadline_days if days is None else days)
        case AddEvidence():
            return model.add_evidence()
        case CloseFinding():
            return model.close(signer_has_license=True)
        case DeclareUnclosable(attempt=attempt):
            return model.declare_unclosable(
                attempt.fields == 5, attempt.policy.kind, attempt.review_months, attempt.license
            )
        case ReviewUnclosable(attempt=attempt):
            return model.review_unclosable(attempt.outcome, attempt.license, attempt.review_months)
        case ReopenFinding(deadline_days=days):
            return model.reopen(reason_present=True, deadline_days=days)
        case Tick(seconds=seconds):
            model.advance_clock(seconds)
        case LateSegment():
            model.late_segment()
        case DeactivateAssignee():
            model.deactivate_assignee()
        case RevokeGate():
            model.revoke_gate()
        case ChangeDeadlineRange(
            min_days=minimum_days,
            default_days=default_days,
            max_days=maximum_days,
            working_weekdays=working_weekdays,
        ):
            return model.change_configuration(
                minimum_days, default_days, maximum_days, working_weekdays
            )
        case _:
            raise AssertionError(f"comando sin modelo: {command!r}")
    return None


class LifecycleReferenceMachine(RuleBasedStateMachine):
    """Recorre la tabla independiente con comandos de longitud variable, incluida la vacía."""

    def __init__(self) -> None:
        super().__init__()
        self.model = LifecycleModel()
        self.commands: list[LifecycleCommand] = []

    @rule(command=lifecycle_command_sequences(max_size=1, min_size=1))
    def apply_one(self, command: list[LifecycleCommand]) -> None:
        if command:
            self.commands.append(command[0])
            _apply(self.model, command[0])

    @invariant()
    def invariantes_del_modelo(self) -> None:
        if self.model.state == "closed":
            assert self.model.action_assigned
            assert self.model.evidence_entries >= 1
        if self.model.state == "unclosable":
            assert self.model.unclosable_review_due_at is not None
        if self.model.state == "overdue":
            assert self.model.action_assigned
        if self.model.action_assigned and self.model.assignee_inactive:
            assert self.model.state in {
                "action_assigned",
                "overdue",
                "suspended_by_reclassification",
                "closed",
                "unclosable",
            }


class ReviewLifecycleReferenceMachine(LifecycleReferenceMachine):
    """La otra entrada de §4.1: una detección en cola permite recorrer T05 y T06."""

    def __init__(self) -> None:
        super().__init__()
        self.model = LifecycleModel(state="in_review")


class LifecyclePlatformMachine(RuleBasedStateMachine):
    """Oráculo independiente frente a los servicios reales y su proyección PostgreSQL.

    El sujeto nace en ``in_review`` mediante el consumidor real de la cola. Así ``resolver_cola``
    puede abrirlo y el resto de los comandos recorre el mismo sujeto; clasificar antes o después
    de resolver también verifica los rechazos de T03--T20. Cada regla aplica primero la tabla
    pequeña y después el servicio de producción, sin avanzar el modelo según la respuesta real.
    """

    stack: Any
    review_queue: Any
    unclosable: Any
    gates: Any
    deadlines: Any
    starts_in_review: bool = True

    def __init__(self) -> None:
        super().__init__()
        self.organization = self.stack.world.organization()
        self.assignee = self.stack.user(self.organization)
        self.model = LifecycleModel(state="in_review" if self.starts_in_review else "received")
        self.action_id: uuid.UUID | None = None
        self.declaration_id: uuid.UUID | None = None
        self.frozen_due_at: dict[uuid.UUID, object] = {}
        self.frozen_review_due_at: object | None = None
        self.frozen_declarations: dict[uuid.UUID, object] = {}
        self.gates.override = None
        if self.starts_in_review:
            self.subject = self.stack.world.ingest_detection(self.organization)
            consumer, _repository = self.stack.world.review_queue_consumer(
                database=self.stack.database
            )
            event = self.stack.world.review_queue_event(self.subject)
            assert self.stack.world.deliver_review_queue(
                consumer, event, database=self.stack.database
            )
        else:
            self.subject = self.stack.received(self.organization)
        self.model.sync_platform_clock(self.stack.now())

    def _context(self) -> Any:
        # El avance del reloj inyectado también vence sesiones por inactividad. Cada comando usa
        # una sesión real recién abierta, como lo haría una persona que vuelve a operar la bandeja.
        return self.stack.context(self.stack.member(self.organization))

    def _state_id(self) -> uuid.UUID:
        return cast(uuid.UUID, self.stack.finding_state_id(self.subject))

    def _code(self, operation: Any) -> tuple[str | None, Any | None]:
        try:
            return None, self.stack.run(operation())
        except Exception as error:
            if isinstance(error, ResourceNotFound):
                return "not_found", None
            detail = getattr(error, "detail_code", None)
            if detail is None:
                if getattr(error, "api_code", None) is not None:
                    return str(error.api_code.value), None
                raise
            return str(detail.value).removeprefix("loop_"), None

    @staticmethod
    def _assert_code(expected: str | None, actual: str | None) -> None:
        assert actual == expected, (expected, actual)

    def _declare_body(self, command: DeclareUnclosable) -> dict[str, object]:
        attempt = command.attempt
        body: dict[str, object] = {
            "evaluated_alternative": ALTERNATIVE,
            "rejection_reason": UNCLOSABLE_REASON,
            "responsible_user_id": self.assignee,
            "decision_date": DECISION_DATE,
            "compensatory_measure": COMPENSATORY,
            "professional_license": LICENSE if attempt.license else None,
        }
        fields = tuple(body)[:5]
        for name in fields[attempt.fields :]:
            body[name] = None
        if attempt.review_months is not None:
            body["review_due_at"] = format_timestamp(
                add_months(self.stack.now(), attempt.review_months)
            )
        return body

    @rule(command=lifecycle_command_sequences(max_size=1, min_size=1))
    def apply_command(self, command: list[LifecycleCommand]) -> None:
        """Ejecuta uno de los catorce comandos contra modelo y plataforma reales."""
        if not command:
            return
        # Las fixtures también avanzan el reloj al ingresar sujetos o eventos. Sincronizar no
        # ejecuta las tareas periódicas ni adopta nunca el estado que devuelve la producción.
        self.model.sync_platform_clock(self.stack.now())
        item = command[0]
        match item:
            case ClassifyFinding(outcome=outcome, reinforced=reinforced):
                expected = _apply(self.model, item)
                category = "real_coexistence" if outcome == "confirmed" else "no_person_present"
                actual, _result = self._code(
                    lambda: self.stack.classification.classify(
                        self._context(),
                        self._state_id(),
                        outcome=outcome,
                        reason_category=category,
                        reason_text="Motivo sintético de clasificación para la prueba estatal",
                        reinforced_reason=reinforced,
                    )
                )
                self._assert_code(expected, actual)
            case Reclassify(outcome=outcome):
                expected = _apply(self.model, item)
                category = "real_coexistence" if outcome == "confirmed" else "no_person_present"
                actual, _result = self._code(
                    lambda: self.stack.classification.classify(
                        self._context(),
                        self._state_id(),
                        outcome=outcome,
                        reason_category=category,
                        reason_text="Motivo sintético de reclasificación para la prueba estatal",
                        reinforced_reason=True,
                    )
                )
                self._assert_code(expected, actual)
            case ResolveQueue(outcome=outcome):
                expected = _apply(self.model, item)
                category = (
                    "real_coexistence" if outcome == "review_confirmed" else "no_person_present"
                )
                actual, _result = self._code(
                    lambda: self.review_queue.resolve(
                        self._context(),
                        self.subject.subject_id,
                        outcome=outcome,
                        reason_category=category,
                        reason_text="Resolución sintética de la cola de revisión estatal",
                    )
                )
                self._assert_code(expected, actual)
            case Assign(action_type=action_type, deadline_days=days):
                expected = _apply(self.model, item)
                actual, result = self._code(
                    lambda: self.stack.actions.assign(
                        self._context(),
                        self._state_id(),
                        action_type=action_type,
                        action_description="Instalar un resguardo fijo en el acceso de la zona",
                        expected_outcome="Eliminar el cruce por el frente del equipo",
                        assignee_user_id=self.assignee,
                        deadline_days=days,
                    )
                )
                self._assert_code(expected, actual)
                if result is not None:
                    self.action_id = result.action_id
                    self.frozen_due_at[result.action_id] = result.due_at
                    self.model.observe_action_deadline(result.due_at)
            case AddEvidence():
                expected = _apply(self.model, item)
                actual, _result = self._code(
                    lambda: self.stack.evidence.add(
                        self._context(),
                        self.action_id or uuid.UUID(int=0),
                        description="Resguardo instalado y anclado en el acceso de la zona",
                        performed_at=format_timestamp(self.stack.now() - timedelta(hours=1)),
                        performed_by_role="Técnico de mantenimiento",
                        verification_note="Se verificó el anclaje durante la ronda sintética",
                    )
                )
                self._assert_code(expected, actual)
            case CloseFinding():
                expected = _apply(self.model, item)
                actual, _result = self._code(
                    lambda: self.stack.closure.close(
                        self._context(),
                        self._state_id(),
                        closure_description=(
                            "Resguardo fijo verificado y anclado en el acceso de la zona"
                        ),
                        verification_note="La verificación de cierre es sintética y trazable",
                        professional_license=LICENSE,
                    )
                )
                self._assert_code(expected, actual)
            case DeclareUnclosable(attempt=attempt):
                expected = _apply(self.model, item)
                self.gates.override = attempt.policy.state()
                actual, result = self._code(
                    lambda: self.unclosable.declare(
                        self._context(), self._state_id(), **self._declare_body(item)
                    )
                )
                self._assert_code(expected, actual)
                if result is not None:
                    self.declaration_id = result.declaration_id
                    self.frozen_review_due_at = result.review_due_at
                    self.frozen_declarations[self.declaration_id] = result.review_due_at
            case ReviewUnclosable(attempt=attempt):
                expected = _apply(self.model, item)
                body: dict[str, object] = {
                    "outcome": attempt.outcome,
                    "review_note": REVIEW_NOTE,
                    "professional_license": LICENSE if attempt.license else None,
                }
                if attempt.review_months is not None:
                    body["new_review_due_at"] = format_timestamp(
                        add_months(self.stack.now(), attempt.review_months)
                    )
                if attempt.outcome == "converted_to_action":
                    body["action"] = {
                        "action_type": "engineering",
                        "action_description": "Instalar un resguardo fijo en el acceso de la zona",
                        "expected_outcome": "Eliminar el cruce por el frente del equipo",
                        "assignee_user_id": self.assignee,
                    }
                actual, result = self._code(
                    lambda: self.unclosable.review(
                        self._context(), self.declaration_id or uuid.UUID(int=0), **body
                    )
                )
                self._assert_code(expected, actual)
                if result is not None and attempt.outcome == "converted_to_action":
                    self.action_id = result.new_action_id
                    assert self.action_id is not None
                    (action,) = [
                        row
                        for row in self.stack.action_rows(self.subject)
                        if row["action_id"] == self.action_id
                    ]
                    self.frozen_due_at[self.action_id] = action["due_at"]
                    self.model.observe_action_deadline(action["due_at"])
                if result is not None and result.new_review_due_at is not None:
                    self.frozen_review_due_at = result.new_review_due_at
                    assert self.declaration_id is not None
                    self.frozen_declarations[self.declaration_id] = result.new_review_due_at
            case ReopenFinding(action_type=action_type, deadline_days=days):
                expected = _apply(self.model, item)
                actual, result = self._code(
                    lambda: self.stack.reopening.reopen(
                        self._context(),
                        self._state_id(),
                        reason_text=(
                            "El resguardo se retiró durante el mantenimiento mayor de la zona"
                        ),
                        action_type=action_type,
                        action_description="Instalar un resguardo fijo en el acceso de la zona",
                        expected_outcome="Eliminar el cruce por el frente del equipo",
                        assignee_user_id=self.assignee,
                        deadline_days=days,
                    )
                )
                self._assert_code(expected, actual)
                if result is not None:
                    self.action_id = result.action.action_id
                    self.frozen_due_at[self.action_id] = result.action.due_at
                    self.model.observe_action_deadline(result.action.due_at)
            case Tick(seconds=seconds):
                _apply(self.model, item)
                self.stack.tick(seconds)
                self.model.sync_platform_clock(self.stack.now())

                async def evaluate() -> Any:
                    context = self.stack.world.event_context(self.organization)
                    async with self.stack.database.transaction(context) as transaction:
                        return await self.deadlines.evaluate(transaction)

                self.stack.run(evaluate())

                async def mark_review_due() -> Any:
                    context = self.stack.world.event_context(self.organization)
                    async with self.stack.database.transaction(context) as transaction:
                        return await self.unclosable.mark_due(transaction)

                self.stack.run(mark_review_due())
            case LateSegment():
                expected = _apply(self.model, item)
                receipt, step = self.stack.world.write(
                    self.organization,
                    "late_segment_attached",
                    {
                        "segment_record_id": str(uuid.uuid4()),
                        "episode_id": str(self.subject.subject_id),
                        "segment_index": 1,
                        "received_at": format_timestamp(self.stack.now()),
                        "episode_state_at_arrival": self.model.state,
                        "reopened_loop": False,
                        "delay_ms": 1000,
                    },
                )
                assert step is not None and step.applied
                assert expected is None and receipt is not None
            case DeactivateAssignee():
                _apply(self.model, item)
                self.stack.deactivate(self.assignee)
                consumer, _ports = self.stack.world.projection_consumer(
                    database=self.stack.database
                )
                event = self.stack.world.synthetic_event(
                    self.organization,
                    "user_deactivated",
                    {
                        "user_id": str(self.assignee),
                        "deactivated_by": str(self.stack.world.stack.authz.operator_id),
                        "deactivated_at": format_timestamp(self.stack.now()),
                    },
                    plant=False,
                )
                self.stack.world.deliver(consumer, event, database=self.stack.database)
            case RevokeGate():
                _apply(self.model, item)
                consumer, _ports = self.stack.world.projection_consumer(
                    database=self.stack.database
                )
                event = self.stack.world.gate_event(self.organization, "revoked")
                assert self.stack.world.deliver(consumer, event, database=self.stack.database) >= 0
            case ChangeDeadlineRange(
                min_days=low,
                default_days=default,
                max_days=high,
                working_weekdays=weekdays,
            ):
                expected = _apply(self.model, item)
                manager = self.stack.member(self.organization, Role.PLANT_MANAGER)
                self.stack.run(
                    self.stack.configuration.change(
                        self.stack.context(manager),
                        self.organization.plant_id,
                        integers={
                            "action_deadline_min_days": low,
                            "action_deadline_default_days": default,
                            "action_deadline_max_days": high,
                        },
                        business_calendar={
                            "working_weekdays": list(weekdays),
                            "non_working_dates": [],
                        },
                        reason=CONFIGURATION_REASON,
                    )
                )
                self._assert_code(expected, None)
            case _:
                raise AssertionError(f"comando sin plataforma: {item!r}")

    @invariant()
    def platform_and_model_keep_the_same_observable_lifecycle(self) -> None:
        row = self.stack.world.finding_state(self.subject)
        assert row is not None
        assert row["lifecycle_state"] == self.model.state, (row, self.model)
        if self.action_id is not None:
            assert row["due_at"] == self.model.observed_due_at
            assert len(self.stack.entry_rows(self.action_id)) == self.model.evidence_entries
            assert row["assignee_inactive"] == self.model.assignee_inactive
        for action in self.stack.action_rows(self.subject):
            expected_due_at = self.frozen_due_at.get(action["action_id"])
            if expected_due_at is not None:
                assert action["due_at"] == expected_due_at
        for declaration in self.stack.fetch(
            "SELECT declaration_id, review_due_at FROM loop.unclosable_declaration"
            " WHERE organization_id = $1 AND subject_id = $2",
            self.organization.organization_id,
            self.subject.subject_id,
        ):
            assert (
                declaration["review_due_at"]
                == self.frozen_declarations[declaration["declaration_id"]]
            )
        if self.model.state == "closed":
            closure = self.stack.records_of(self.subject, "closure")[-1]
            assert closure["content"]["evidence_entry_ids"]
            assert closure["content"]["signer"]["professional_license"]
        if self.model.state == "unclosable":
            declaration = self.stack.records_of(self.subject, "unclosable_state_declared")[-1]
            assert declaration["content"]["policy"]["document_sha256"]
            for field in (
                "evaluated_alternative",
                "rejection_reason",
                "responsible_user_id",
                "decision_date",
                "compensatory_measure",
            ):
                assert declaration["content"][field]
            assert row["unclosable_review_due_at"] is not None
            assert row["unclosable_review_due_at"] == self.frozen_review_due_at
            assert (
                int(row["unclosable_review_due_at"].timestamp())
                == self.model.unclosable_review_due_at
            )
            assert row["unclosable_pending_review"] == self.model.unclosable_pending_review
        if self.model.gate_revoked:
            assert row["zone_agreement_revoked"]
        if self.model.state == "overdue":
            inbox = InboxService(
                database=self.stack.database, authorizer=self.stack.world.stack.authz.authorizer
            )
            page = self.stack.run(inbox.page(self._context()))
            assert self._state_id() in {item.finding_state_id for item in page.items}


class ReceivedLifecyclePlatformMachine(LifecyclePlatformMachine):
    """La segunda entrada de la tabla: ``received`` permite comprobar T03 y T04 reales."""

    starts_in_review = False


def test_pr_laz_10_reference_state_machine() -> None:
    """La semilla fija y la de sesión ejercitan los catorce comandos hasta 50 pasos."""
    for value in _seeds_for_profile():
        for machine in (LifecycleReferenceMachine, ReviewLifecycleReferenceMachine):
            seeded = hypothesis_seed(value)(machine)
            run_state_machine_as_test(
                seeded,
                settings=settings(
                    stateful_step_count=_STEPS,
                    deadline=None,
                    suppress_health_check=[HealthCheck.too_slow],
                ),
            )


@pytest.fixture
def lifecycle_machine_stack(postgres_endpoint: PostgresEndpoint) -> Iterator[Any]:
    """Servicios reales compartidos por exploración y regresiones; cada sujeto tiene su planta."""
    with actions_stack(postgres_endpoint, "pr_laz_10_platform") as stack:
        _consumer, repository = stack.world.review_queue_consumer(database=stack.database)
        gates = Gates(catalog_query_ports(stack.database).gates)
        LifecyclePlatformMachine.stack = stack
        LifecyclePlatformMachine.review_queue = ReviewQueueService(
            database=stack.database,
            writer=stack.writer,
            authorizer=stack.world.stack.authz.authorizer,
            repository=repository,
            clock=stack.world.stack.authz.sessions.clock,
        )
        LifecyclePlatformMachine.gates = gates
        LifecyclePlatformMachine.unclosable = UnclosableService(
            database=stack.database,
            writer=stack.writer,
            authorizer=stack.world.stack.authz.authorizer,
            clock=stack.world.stack.authz.sessions.clock,
            gates=gates,
            actions=stack.actions,
            configuration=stack.configuration.repository,
            outbox=stack.world.stack.outbox,
        )
        LifecyclePlatformMachine.deadlines = DeadlineEvaluator(
            writer=stack.writer,
            clock=stack.world.stack.authz.sessions.clock,
            configuration=stack.configuration.repository,
        )
        yield stack


@pytest.mark.integration
def test_pr_laz_10_platform_state_machine(
    lifecycle_machine_stack: Any, request: pytest.FixtureRequest
) -> None:
    """El recorrido estatal contrasta los catorce comandos con PostgreSQL real."""
    # Solo el presupuesto de pared se mide con el reloj real. Ninguna fecha de negocio sale de él.
    budget_clock = SystemClock()
    started = budget_clock.monotonic()
    for value in _seeds_for_profile():
        for machine in (LifecyclePlatformMachine, ReceivedLifecyclePlatformMachine):
            seeded = hypothesis_seed(value)(machine)
            run_state_machine_as_test(
                seeded,
                settings=settings(
                    max_examples=_service_examples(),
                    stateful_step_count=(
                        _STEPS
                        if settings.get_current_profile_name() == "nightly"
                        else _PLATFORM_STEPS
                    ),
                    deadline=None,
                    database=None,
                    suppress_health_check=[
                        HealthCheck.too_slow,
                        HealthCheck.function_scoped_fixture,
                    ],
                ),
            )
    elapsed = budget_clock.monotonic() - started
    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(f"PR-LAZ-10 PostgreSQL: {elapsed:.2f} s (presupuesto CI: 300 s)")
    if settings.get_current_profile_name() in {"ci", "pr"}:
        assert elapsed < 300, f"PR-LAZ-10 excedió el presupuesto: {elapsed:.2f} s"


@pytest.mark.integration
@pytest.mark.laz_examples
def test_pr_laz_10_permanent_counterexamples(lifecycle_machine_stack: Any) -> None:
    """Las reducciones de la revisión y recorridos profundos usan la misma comparación estatal."""
    for name, in_review, commands, final_state in REGRESSIONS:
        machine = (LifecyclePlatformMachine if in_review else ReceivedLifecyclePlatformMachine)()
        try:
            machine.platform_and_model_keep_the_same_observable_lifecycle()
            for command in commands:
                machine.apply_command([command])
                machine.platform_and_model_keep_the_same_observable_lifecycle()
            assert machine.model.state == final_state
        except Exception as error:
            error.add_note(f"Regresión permanente: {name}")
            raise
        finally:
            machine.teardown()


@pytest.mark.laz_examples
def test_pr_laz_10_rejection_and_frozen_deadline_regressions() -> None:
    """El modelo conserva los códigos de la tabla para contrastarlos con la plataforma."""
    model = LifecycleModel()
    assert model.close(signer_has_license=True) == "closure_requires_assigned_action"
    # DE §4.2: received → acción no existe.
    assert model.assign_action(1) == "transition_not_allowed"

    model.classify("confirmed", reinforced=False)
    assert model.classify("confirmed", reinforced=False) == "transition_not_allowed"
    assert model.assign_action(1) is None
    original_due_at = model.due_at
    assert model.change_configuration(1, 30, 180, (1, 2, 3, 4, 5)) is None
    assert model.due_at == original_due_at
    model.advance_clock(2 * 86_400)
    assert model.state == "overdue"
    assert model.close(signer_has_license=True) == "closure_evidence_missing"
    assert model.state == "overdue"


@pytest.mark.integration
def test_pr_laz_10_production_mutation_regressions(postgres_endpoint: PostgresEndpoint) -> None:
    """Sondas de las cuatro mutaciones de PR-LAZ-10 sobre servicios y PostgreSQL reales.

    No mutan el oráculo: cada aserción observa la respuesta o la proyección que produce el código
    de producción. Así una guarda eliminada en ``ClosureService``, ``DeadlineEvaluator`` o la
    configuración hace fallar esta prueba.
    """
    with actions_stack(postgres_endpoint, "pr_laz_10_mutations") as stack:
        organization = stack.world.organization()
        coordinator = stack.member(organization)
        assignee = stack.user(organization)

        open_finding = stack.opened(organization)
        from_open = stack.close(coordinator, open_finding, stack.closure_body())
        assert from_open.status_code == 409, from_open.text
        assert from_open.json()["detail_code"] == "loop_closure_requires_assigned_action"

        finding = stack.opened(organization)
        assigned = stack.assign(coordinator, finding, stack.action_body(assignee, deadline_days=1))
        assert assigned.status_code == 201, assigned.text
        action_id = assigned.json()["action_id"]
        frozen_due_at = assigned.json()["due_at"]

        without_evidence = stack.close(coordinator, finding, stack.closure_body())
        assert without_evidence.status_code == 400, without_evidence.text
        assert without_evidence.json()["detail_code"] == "loop_closure_evidence_missing"

        manager = stack.member(organization, Role.PLANT_MANAGER)
        for integers, calendar in (
            ({"action_deadline_min_days": 1, "action_deadline_max_days": 180}, None),
            (
                {
                    "action_deadline_min_days": 2,
                    "action_deadline_default_days": 2,
                    "action_deadline_max_days": 3,
                },
                {"working_weekdays": [1, 2, 3], "non_working_dates": []},
            ),
        ):
            stack.run(
                stack.configuration.change(
                    stack.context(manager),
                    organization.plant_id,
                    integers=integers,
                    business_calendar=calendar,
                    reason=CONFIGURATION_REASON,
                )
            )
        matching_actions = [
            row for row in stack.action_rows(finding) if str(row["action_id"]) == action_id
        ]
        (action,) = matching_actions
        assert format_timestamp(action["due_at"]) == frozen_due_at
        projected = stack.world.finding_state(finding)
        assert projected is not None
        assert format_timestamp(projected["due_at"]) == frozen_due_at

        stack.tick((action["due_at"] - stack.now() + timedelta(seconds=1)).total_seconds())
        evaluator = DeadlineEvaluator(
            writer=stack.writer,
            clock=stack.world.stack.authz.sessions.clock,
            configuration=stack.configuration.repository,
        )

        async def evaluate() -> Any:
            context = stack.world.event_context(organization)
            async with stack.database.transaction(context) as transaction:
                return await evaluator.evaluate(transaction)

        declared = stack.run(evaluate())
        assert declared and declared[0].transition_id == "T08"
        row = stack.world.finding_state(finding)
        assert row is not None and row["lifecycle_state"] == "overdue"
        assert stack.records_of(finding, "closure") == []
