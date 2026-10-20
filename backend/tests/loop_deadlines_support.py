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
from vigia_platform.identity.adapters.authz_store import LedgerProviderQueryLedger
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME
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
