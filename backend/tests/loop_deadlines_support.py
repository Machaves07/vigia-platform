"""Los plazos de TASK-313 sobre PostgreSQL 16 real como ``vigia_app`` y por HTTP.

``deadlines_stack`` construye, sobre ``actions_stack`` (TASK-311: hallazgos ingresados por la ruta
del contrato de U-03, clasificados y con su acción asignada por los servicios reales), la tarea
real ``evaluate_deadlines`` (``DeadlineEvaluator`` con el escritor de producción y el contexto de
iteración periódica que le da el planificador, ``context_for_organization``), el panel de
vencidos (``OverdueService``) y una aplicación real (``create_app`` con la cadena fija de
middleware y ``ContextAuthorizer``) con ``LoopHttp`` para ``GET /loop/overdue``.

``DeadlinesStack`` da los hallazgos con su acción vencida, el reloj llevado hasta un instante,
la configuración del umbral de la planta, las ejecuciones de la tarea y lo que quedó escrito
(registros ``action_overdue`` y sus eventos).

Solo datos generados. Un solo reloj: las marcas salen del reloj simulado del mundo (retro 15), y
ninguna prueba compara con la hora de la base. Topes de las esperas de la base: 60 s (retro 15).
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import httpx

from tests.api_support import World
from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import IngestedSubject, LoopOrganization
from tests.loop_actions_support import SAME_ORIGIN, ActionsStack, Who, actions_stack
from tests.properties.laz.strategies.lifecycle import (
    AddEvidence,
    Assign,
    ChangeDeadlineRange,
    ChangeHighlightDays,
    CloseFinding,
    DeclareUnclosableFinding,
    EvaluateDeadlines,
    LifecycleCommand,
    Reassign,
    Reclassify,
    ReopenFinding,
    Tick,
)
from vigia_platform.identity.adapters.authz_store import LedgerProviderQueryLedger
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME
from vigia_platform.loop.adapters.http.classification_routes import PATH as CLASSIFICATION_PATH
from vigia_platform.loop.adapters.http.overdue_routes import PATH as OVERDUE_PATH
from vigia_platform.loop.adapters.http.services import LOOP_STATE_KEY, LoopHttp
from vigia_platform.loop.application.deadlines import (
    DeadlineEvaluator,
    OverdueDeclared,
    OverdueService,
    register_evaluate_deadlines,
)
from vigia_platform.loop.application.inbox import InboxService
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.context import Role, ScopeContext
from vigia_platform.shared.outbox.registries import PeriodicTask, PeriodicTaskRegistry

__all__ = ["CONFIGURATION_REASON", "DeadlinesStack", "deadlines_stack"]

CONFIGURATION_REASON: Final = "Ajuste del umbral acordado en el comité de seguridad"
RECLASSIFICATION_REASON: Final = (
    "Revisado el clip completo con mantenimiento: la intervención constaba en el registro"
)


@dataclass
class DeadlinesStack:
    actions: ActionsStack
    evaluator: DeadlineEvaluator
    task: PeriodicTask
    overdue: OverdueService
    inbox: InboxService
    client: httpx.AsyncClient

    # --- utilidades ---------------------------------------------------------------------------

    def run(self, awaitable: Any) -> Any:
        return self.actions.run(awaitable)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.actions.fetch(sql, *args)

    def now(self) -> datetime:
        now: datetime = self.actions.now()
        return now

    def tick(self, seconds: float = 1.0) -> None:
        self.actions.tick(seconds)

    def tick_to(self, moment: datetime) -> None:
        """Lleva el reloj simulado hasta ``moment`` (nunca hacia atrás)."""
        delta = (moment - self.now()).total_seconds()
        if delta > 0:
            self.tick(delta)

    def evaluator_like(self, cls: type[Any] = DeadlineEvaluator, **changes: Any) -> Any:
        """Otra ``DeadlineEvaluator`` (p. ej. una subclase con ganchos) sobre la misma base."""
        arguments: dict[str, Any] = {
            "writer": self.actions.writer,
            "clock": self.actions.world.stack.authz.sessions.clock,
            "configuration": PostgresLoopConfigurationRepository(self.actions.database),
        }
        arguments.update(changes)
        return cls(**arguments)

    # --- la tarea -----------------------------------------------------------------------------

    def periodic_context(self, organization: LoopOrganization) -> ScopeContext:
        """El contexto que el planificador da a la tarea en ``organization`` (BR-NUC-03)."""
        contexts = self.actions.world.stack.authz.contexts
        context: ScopeContext = contexts.context_for_organization(
            self.task, organization.organization_id
        )
        return context

    async def evaluate_async(
        self, organization: LoopOrganization, evaluator: DeadlineEvaluator | None = None
    ) -> tuple[OverdueDeclared, ...]:
        chosen = evaluator if evaluator is not None else self.evaluator
        async with self.actions.database.transaction(
            self.periodic_context(organization)
        ) as transaction:
            return await chosen.evaluate(transaction)

    def evaluate(
        self, organization: LoopOrganization, evaluator: DeadlineEvaluator | None = None
    ) -> tuple[OverdueDeclared, ...]:
        """Una ejecución de ``evaluate_deadlines`` en ``organization``, como la del planificador."""
        declared: tuple[OverdueDeclared, ...] = self.run(
            self.evaluate_async(organization, evaluator)
        )
        return declared

    # --- configuración y personas --------------------------------------------------------------

    def manager(self, organization: LoopOrganization) -> Who:
        return self.actions.member(organization, Role.PLANT_MANAGER)

    def set_highlight_days(self, organization: LoopOrganization, days: int) -> None:
        """``manager_highlight_overdue_days`` de la planta por ``loop.config`` (TASK-304)."""
        self.tick()
        self.run(
            self.actions.configuration.change(
                self.actions.context(self.manager(organization)),
                organization.plant_id,
                integers={"manager_highlight_overdue_days": days},
                business_calendar=None,
                reason=CONFIGURATION_REASON,
            )
        )

    # --- hallazgos ----------------------------------------------------------------------------

    def with_action(
        self,
        organization: LoopOrganization,
        coordinator: Who,
        assignee: uuid.UUID,
        deadline_days: int = 1,
    ) -> tuple[IngestedSubject, uuid.UUID, datetime]:
        """Un hallazgo ``action_assigned`` con su ``action_id`` y su ``due_at`` congelado."""
        finding = self.actions.opened(organization)
        body = self.actions.action_body(assignee, deadline_days=deadline_days)
        response = self.actions.assign(coordinator, finding, body)
        assert response.status_code == 201, response.text
        payload = response.json()
        due_at = datetime.fromisoformat(payload["due_at"].replace("Z", "+00:00"))
        return finding, uuid.UUID(payload["action_id"]), due_at

    def state(self, subject: IngestedSubject) -> str:
        row = self.actions.world.finding_state(subject)
        assert row is not None
        return str(row["lifecycle_state"])

    # --- lo que quedó escrito ------------------------------------------------------------------

    def overdue_records(self, subject: IngestedSubject) -> list[dict[str, Any]]:
        return self.actions.records_of(subject, "action_overdue")

    def organization_overdue(self, organization: LoopOrganization) -> list[dict[str, Any]]:
        """Los ``action_overdue`` de la organización (contenido parseado), en orden de cadena."""
        return [
            json.loads(row["content"])
            for row in self.fetch(
                "SELECT ledger.vigia_bytes_to_jsonb(content)::text AS content"
                " FROM ledger.ledger_record WHERE organization_id = $1"
                " AND record_type = 'action_overdue' ORDER BY plant_id, chain_sequence",
                organization.organization_id,
            )
        ]

    def record_events(
        self, organization: LoopOrganization, record_id: uuid.UUID
    ) -> list[tuple[str, dict[str, Any]]]:
        """``(nombre, carga)`` de los eventos que citan ``record_id``, en orden de nombre."""
        return [
            (str(row["event_name"]), json.loads(row["payload"]))
            for row in self.fetch(
                "SELECT event_name, payload::text AS payload FROM shared.outbox_event"
                " WHERE organization_id = $1 AND payload ->> 'record_id' = $2"
                " ORDER BY event_name",
                organization.organization_id,
                str(record_id),
            )
        ]

    # --- secuencias de comandos (PR-LAZ-13 y 15) ----------------------------------------------

    def change_deadline_range(
        self,
        organization: LoopOrganization,
        low: int,
        default: int,
        high: int,
        weekdays: tuple[int, ...],
    ) -> None:
        """El rango de plazos y el calendario de la planta (``loop.config``), abriendo el rango
        primero para que ``min ≤ default ≤ max`` se cumpla sobre el resultado."""
        configuration = self.actions.configuration
        for integers, calendar in (
            ({"action_deadline_min_days": 1, "action_deadline_max_days": 180}, None),
            (
                {
                    "action_deadline_min_days": low,
                    "action_deadline_default_days": default,
                    "action_deadline_max_days": high,
                },
                {"working_weekdays": list(weekdays), "non_working_dates": []},
            ),
        ):
            self.tick()
            self.run(
                configuration.change(
                    self.actions.context(self.manager(organization)),
                    organization.plant_id,
                    integers=integers,
                    business_calendar=calendar,
                    reason=CONFIGURATION_REASON,
                )
            )

    def apply(
        self, organization: LoopOrganization, finding: IngestedSubject, command: LifecycleCommand
    ) -> list[str]:
        """Aplica ``command`` (de ``deadline_command_sequences``) por el servicio o la ruta real,
        con sesiones nuevas (el reloj salta días). Devuelve los tipos de registro que escribió.

        Un rechazo de negocio es un resultado válido (la secuencia no se predice aquí); una
        respuesta 5xx no lo es. ``EvaluateDeadlines`` comprueba además que el comando del
        sistema solo declara: el estado sigue igual o pasa de ``action_assigned`` a ``overdue``,
        la fila del hallazgo sigue ahí y solo escribe ``action_overdue``."""
        actions = self.actions
        before = len(actions.subject_records(finding))
        coordinator = actions.member(organization)
        row = actions.world.finding_state(finding)
        assert row is not None
        current = row["current_action_id"]
        responses: list[httpx.Response] = []
        match command:
            case Assign(action_type=action_type, deadline_days=days):
                body = actions.action_body(
                    self.assignee(organization), action_type=action_type, deadline_days=days
                )
                responses.append(actions.assign(coordinator, finding, body))
            case Reassign(deadline_days=days):
                supersedes = str(current) if current is not None else str(uuid.uuid4())
                body = actions.action_body(
                    self.assignee(organization),
                    action_type=row["current_action_type"] or "engineering",
                    deadline_days=days,
                    supersedes_action_id=supersedes,
                )
                responses.append(actions.assign(coordinator, finding, body))
            case AddEvidence():
                if current is not None:
                    responses.append(
                        actions.add_evidence(coordinator, current, actions.evidence_body())
                    )
            case ChangeDeadlineRange(
                min_days=low, default_days=default, max_days=high, working_weekdays=weekdays
            ):
                self.change_deadline_range(organization, low, default, high, weekdays)
            case Tick(seconds=seconds):
                self.tick(seconds)
            case CloseFinding():
                responses.append(actions.close(coordinator, finding, actions.closure_body()))
            case ReopenFinding(action_type=action_type, deadline_days=days):
                body = actions.reopening_body(
                    self.assignee(organization), action_type=action_type, deadline_days=days
                )
                responses.append(actions.reopen(coordinator, finding, body))
            case Reclassify(outcome=outcome):
                category = (
                    "no_person_present" if outcome == "false_positive" else "real_coexistence"
                )
                path = CLASSIFICATION_PATH.format(
                    finding_state_id=actions.finding_state_id(finding)
                )
                responses.append(
                    actions.post(
                        coordinator,
                        path,
                        {
                            "outcome": outcome,
                            "reason_category": category,
                            "reason_text": RECLASSIFICATION_REASON,
                            "reinforced_reason": True,
                        },
                    )
                )
            case ChangeHighlightDays(days=days):
                self.set_highlight_days(organization, days)
            case DeclareUnclosableFinding():
                if row["lifecycle_state"] in ("open", "action_assigned", "overdue"):
                    actions.write_unclosable(finding)
            case EvaluateDeadlines():
                self.evaluate(organization)
                after = actions.world.finding_state(finding)
                assert after is not None, "la tarea retiró la fila del hallazgo"
                allowed = {row["lifecycle_state"]}
                if row["lifecycle_state"] == "action_assigned":
                    allowed.add("overdue")
                assert after["lifecycle_state"] in allowed, (row["lifecycle_state"], after)
                written = actions.subject_records(finding)[before:]
                assert {r["record_type"] for r in written} <= {"action_overdue"}, written
            case _:  # pragma: no cover - un comando nuevo sin su rama
                raise AssertionError(f"comando sin aplicar: {command!r}")
        for response in responses:
            assert response.status_code < 500, response.text
        return [r["record_type"] for r in actions.subject_records(finding)[before:]]

    def assignee(self, organization: LoopOrganization) -> uuid.UUID:
        """Un responsable con alcance sobre la organización (uno nuevo cada vez)."""
        return self.actions.user(organization)

    # --- peticiones ---------------------------------------------------------------------------

    def get_overdue(self, who: Who, params: Mapping[str, Any]) -> httpx.Response:
        self.tick()
        cookie, concession = who
        headers = {**SAME_ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}"}
        if concession is not None:
            headers["X-Vigia-Concession"] = str(concession)
        response: httpx.Response = self.run(
            self.client.get(OVERDUE_PATH, params=dict(params), headers=headers)
        )
        return response


@contextlib.contextmanager
def deadlines_stack(endpoint: PostgresEndpoint, prefix: str) -> Iterator[DeadlinesStack]:
    with actions_stack(endpoint, prefix) as actions:
        stack = actions.world.stack
        authz = stack.authz
        clock = authz.sessions.clock
        database = actions.database
        configuration = PostgresLoopConfigurationRepository(database)
        evaluator = DeadlineEvaluator(
            writer=actions.writer, clock=clock, configuration=configuration
        )
        task = register_evaluate_deadlines(PeriodicTaskRegistry(), evaluator)
        overdue = OverdueService(
            database=database,
            authorizer=authz.authorizer,
            configuration=configuration,
            clock=clock,
        )
        inbox = InboxService(database=database, authorizer=authz.authorizer)
        app = World(clock=clock).app(
            runtime={
                "sessions": authz.contexts,
                "authorizer": ContextAuthorizer(
                    audit=authz.audit,
                    provider_organization_id=authz.provider_organization_id,
                    provider_queries=LedgerProviderQueryLedger(actions.writer),
                    clock=clock,
                ),
                "state": {LOOP_STATE_KEY: LoopHttp(overdue=overdue, inbox=inbox)},
            },
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60.0
        )
        try:
            yield DeadlinesStack(actions, evaluator, task, overdue, inbox, client)
        finally:
            stack.run(client.aclose())
