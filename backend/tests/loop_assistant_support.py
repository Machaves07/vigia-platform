"""El asistente de causa raíz de TASK-316 sobre PostgreSQL 16 real como ``vigia_app`` y por HTTP.

``assistant_stack`` construye, sobre ``root_cause_stack`` (el flujo guiado real de TASK-315 con su
aplicación), el catálogo de la bandeja con ``assistant_requested`` y el consumidor
``assistant_worker`` registrado y sincronizado (su fila de ``shared.consumer`` es la del
cortacircuito), ``AssistantService`` en ``LoopHttp`` y lo necesario para atender las solicitudes:

- ``AdvancingSleep``: el ``sleep`` del consumidor y del doble, que **avanza el reloj simulado** en
  vez de esperar: los 60 s, los 120 s y los retrocesos de 5 s y 20 s son del reloj inyectado,
  nunca de pared (retro 14 y 15);
- ``use(provider)``: el ``AssistantWorker`` real con el doble del proveedor y la política con los
  **dos validadores reales** de U-04 (``both_policy``);
- ``deliver``: la entrega del evento al manejador con el contexto que daría el despachador (la
  organización del evento, U-04), en una transacción propia; ``dispatch``: una ronda del
  ``Dispatcher`` real de U-02 (cortacircuito incluido).

Solo datos generados. El reloj simulado arranca en la hora de la base y ninguna marca se compara
con la hora de la base. Topes de las esperas de la base: 60 s (retro 15).
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

import httpx
from sqlalchemy import text

from tests.attribution_support import both_policy
from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import LoopOrganization, register_u04
from tests.loop_classification_support import Who
from tests.loop_root_cause_support import RootCauseStack, root_cause_stack
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.ledger.registry import RecordTypeRegistry
from vigia_platform.loop.adapters.assistant.fake import FakeAssistantProvider, Script
from vigia_platform.loop.assistant.consumer import AssistantWorker, register_assistant_worker
from vigia_platform.loop.assistant.port import AssistantProviderPort
from vigia_platform.loop.assistant.service import ASSISTANT_WORKER, AssistantService
from vigia_platform.loop.registration import loop_free_text
from vigia_platform.shared.context import ActorUnit, Role
from vigia_platform.shared.db import Database, Transaction
from vigia_platform.shared.outbox.dispatcher import Dispatcher, DispatchReport
from vigia_platform.shared.outbox.publish import OutboxEvent
from vigia_platform.shared.outbox.registries import OutboxCatalog

__all__ = [
    "REQUEST",
    "STATUS",
    "AdvancingSleep",
    "AssistantStack",
    "assistant_stack",
]

REQUEST: Final = "/loop/root-cause-analyses/{analysis_id}/assistant-requests"
STATUS: Final = "/loop/assistant-requests/{request_id}"


@dataclass
class AdvancingSleep:
    """``sleep`` inyectado: avanza el reloj simulado lo pedido y lo anota."""

    clock: Any
    waits: list[float] = field(default_factory=list)

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.clock.advance(seconds)


@dataclass(frozen=True)
class _EventRef:
    organization_id: uuid.UUID
    correlation_id: uuid.UUID


@dataclass
class AssistantStack:
    base: RootCauseStack
    catalog: OutboxCatalog
    sleep: AdvancingSleep
    worker: AssistantWorker | None = None

    # --- utilidades ---------------------------------------------------------------------------

    @property
    def clock(self) -> Any:
        return self.base.world.stack.authz.sessions.clock

    @property
    def service(self) -> AssistantService:
        assert self.base.assistant is not None
        return self.base.assistant

    def run(self, awaitable: Any) -> Any:
        return self.base.run(awaitable)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.base.fetch(sql, *args)

    def execute(self, sql: str, *args: Any) -> None:
        self.base.world.stack.execute(sql, *args)

    def now(self) -> datetime:
        moment: datetime = self.clock.now()
        return moment

    def database(self) -> Database:
        """Otro pool, como el de otro proceso (topes de 60 s)."""
        database: Database = self.base.world.stack.database(worker_pool_size=4)
        return database

    # --- personas y análisis ------------------------------------------------------------------

    def organization(self, which: str = "a") -> LoopOrganization:
        return self.base.world.a if which == "a" else self.base.world.b

    def coordinator(self, which: str = "a") -> Who:
        """Una persona nueva con ``coordinator_sst`` en la organización ``which``."""
        return self.base.member(self.organization(which), Role.COORDINATOR_SST)

    def analysis(self, who: Who, which: str = "a") -> str:
        """Un análisis nuevo (sobre un hallazgo recién ingresado en ``which``): su id."""
        subject = self.base.received(self.organization(which))
        return str(self.base.started(who, subject)["analysis_id"])

    def post(self, who: Who, analysis_id: str | uuid.UUID) -> httpx.Response:
        return self.base.request(who, "POST", REQUEST.format(analysis_id=analysis_id))

    def get(self, who: Who, request_id: str | uuid.UUID) -> httpx.Response:
        return self.base.request(who, "GET", STATUS.format(request_id=request_id))

    def requested(self, who: Who, analysis_id: str | None = None, which: str = "a") -> str:
        """Una solicitud admitida (202): su ``request_id``."""
        response = self.post(who, analysis_id or self.analysis(who, which))
        assert response.status_code == 202, response.text
        return str(response.json()["request_id"])

    # --- consumidor ---------------------------------------------------------------------------

    def provider(
        self, scripts: Sequence[Script] | Script = Script.VALID_WITH_CITATIONS, **changes: Any
    ) -> FakeAssistantProvider:
        return FakeAssistantProvider(scripts, clock=self.clock, sleep=self.sleep, **changes)

    def use(
        self,
        provider: AssistantProviderPort,
        *,
        free_text: FreeTextPolicyRegistry | None = None,
        database: Database | None = None,
        cls: type[AssistantWorker] = AssistantWorker,
    ) -> AssistantWorker:
        """El ``AssistantWorker`` real con ``provider`` y los dos validadores reales de U-04."""
        worker = cls(
            database=database or self.base.database,
            provider=provider,
            free_text=free_text or both_policy(),
            clock=self.clock,
            sleep=self.sleep,
        )
        self.worker = worker
        return worker

    @staticmethod
    def production_policy() -> FreeTextPolicyRegistry:
        """La política que registra U-04 al arrancar: los dos validadores con sus cachés y sus
        **preparadores** sobre PostgreSQL (``loop_free_text``)."""
        registry = FreeTextPolicyRegistry()
        loop_free_text(registry)
        registry.seal()
        return registry

    def event_of(self, request_id: str | uuid.UUID) -> OutboxEvent:
        (row,) = self.fetch(
            "SELECT event_id, organization_id, plant_id, event_name, partition_key,"
            " ledger_sequence, payload::text AS payload, correlation_id, created_at"
            " FROM shared.outbox_event WHERE event_name = 'assistant_requested'"
            " AND payload ->> 'request_id' = $1",
            str(request_id),
        )
        return OutboxEvent(
            event_id=uuid.UUID(str(row["event_id"])),
            organization_id=uuid.UUID(str(row["organization_id"])),
            plant_id=uuid.UUID(str(row["plant_id"])),
            event_name=row["event_name"],
            partition_key=row["partition_key"],
            ledger_sequence=row["ledger_sequence"],
            payload=json.loads(row["payload"]),
            correlation_id=uuid.UUID(str(row["correlation_id"])),
            created_at=row["created_at"],
        )

    async def deliver_now(
        self,
        event: OutboxEvent,
        *,
        worker: AssistantWorker | None = None,
        database: Database | None = None,
        idle_limit_ms: int | None = None,
    ) -> None:
        """Una entrega de ``event`` (``event_of``), como la haría el despachador.

        ``idle_limit_ms`` fija en la transacción de la entrega el tope de inactividad de la base
        antes de llamar al manejador (el ``idle_in_transaction_session_timeout`` de RDS)."""
        handler = worker or self.worker
        assert handler is not None
        context = self.base.world.stack.authz.contexts.context_from_event(
            _EventRef(event.organization_id, event.correlation_id), unit=ActorUnit.U04
        )
        async with (database or self.base.database).transaction(context) as transaction:
            if idle_limit_ms is not None:
                await transaction.execute(
                    text("SELECT set_config('idle_in_transaction_session_timeout', :ms, true)"),
                    {"ms": str(idle_limit_ms)},
                )
            await handler(event, transaction)

    def deliver(self, request_id: str | uuid.UUID, **changes: Any) -> None:
        self.run(self.deliver_now(self.event_of(request_id), **changes))

    def dispatcher(self) -> Dispatcher:
        return Dispatcher(
            database=self.base.database,
            catalog=self.catalog,
            outbox=self.base.world.stack.outbox,
            contexts=self.base.world.stack.authz.contexts,
            clock=self.clock,
            jitter=lambda: 0.5,
        )

    def dispatch(self) -> DispatchReport:
        report: DispatchReport = self.run(self.dispatcher().dispatch_once(ASSISTANT_WORKER))
        return report

    def settle_deliveries(self) -> None:
        """Da por entregado lo pendiente de ``assistant_worker`` y cierra su circuito: cada prueba
        empieza sin entregas de otra (la partición de la planta es la misma)."""
        self.execute(
            "UPDATE shared.outbox_delivery SET status = 'delivered', delivered_at = now()"
            " WHERE consumer_name = $1 AND status <> 'delivered'",
            ASSISTANT_WORKER,
        )
        self.set_circuit("closed")

    def set_circuit(self, state: str, opened_at: datetime | None = None) -> None:
        self.execute(
            "UPDATE shared.consumer SET circuit_state = $2, circuit_opened_at = $3"
            " WHERE consumer_name = $1",
            ASSISTANT_WORKER,
            state,
            opened_at,
        )

    def circuit(self) -> tuple[str, datetime | None]:
        (row,) = self.fetch(
            "SELECT circuit_state, circuit_opened_at FROM shared.consumer WHERE consumer_name = $1",
            ASSISTANT_WORKER,
        )
        return str(row["circuit_state"]), row["circuit_opened_at"]

    def delivery(self, request_id: str | uuid.UUID) -> dict[str, Any]:
        event = self.event_of(request_id)
        (row,) = self.fetch(
            "SELECT status, attempts FROM shared.outbox_delivery"
            " WHERE event_id = $1 AND consumer_name = $2",
            event.event_id,
            ASSISTANT_WORKER,
        )
        return dict(row)

    # --- lo que quedó escrito -----------------------------------------------------------------

    def request_row(self, request_id: str | uuid.UUID) -> dict[str, Any]:
        (row,) = self.fetch(
            "SELECT * FROM loop.assistant_request WHERE request_id = $1",
            uuid.UUID(str(request_id)),
        )
        return dict(row)

    def drafts(self, request_id: str | uuid.UUID) -> list[dict[str, Any]]:
        rows = self.fetch(
            "SELECT * FROM loop.assistant_draft WHERE request_id = $1",
            uuid.UUID(str(request_id)),
        )
        found = [dict(row) for row in rows]
        for draft in found:
            if isinstance(draft["citations"], str):
                draft["citations"] = json.loads(draft["citations"])
        return found

    def anchor_of(self, subject_id: uuid.UUID) -> uuid.UUID:
        """El registro de anclaje del sujeto en la proyección (lo que nombra el alias ``F-…``)."""
        (row,) = self.fetch(
            "SELECT anchor_record_id FROM loop.finding_state WHERE subject_id = $1", subject_id
        )
        return uuid.UUID(str(row["anchor_record_id"]))

    def requests_of(self, organization_id: uuid.UUID) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.fetch(
                "SELECT * FROM loop.assistant_request WHERE organization_id = $1"
                " ORDER BY requested_at, request_id",
                organization_id,
            )
        ]

    def add_tokens(
        self, organization_id: uuid.UUID, *, input_tokens: int, output_tokens: int
    ) -> None:
        """Consumo previo del mes: fichas en la solicitud más reciente de la organización."""
        self.execute(
            "UPDATE loop.assistant_request SET input_tokens = input_tokens + $2,"
            " output_tokens = output_tokens + $3 WHERE request_id = ("
            " SELECT request_id FROM loop.assistant_request WHERE organization_id = $1"
            " ORDER BY requested_at DESC LIMIT 1)",
            organization_id,
            input_tokens,
            output_tokens,
        )


async def _delegate(stack_ref: list[AssistantStack], event: OutboxEvent, tx: Transaction) -> None:
    worker = stack_ref[0].worker
    if worker is None:
        raise RuntimeError("la prueba no fijó el worker")
    await worker(event, tx)


@contextlib.contextmanager
def assistant_stack(endpoint: PostgresEndpoint, prefix: str) -> Iterator[AssistantStack]:
    stack_ref: list[AssistantStack] = []
    catalogs: list[OutboxCatalog] = []

    async def handler(event: OutboxEvent, tx: Transaction) -> None:
        await _delegate(stack_ref, event, tx)

    def register(registry: RecordTypeRegistry, catalog: OutboxCatalog) -> None:
        register_u04(registry, catalog)
        register_assistant_worker(catalog.consumers, handler)  # type: ignore[arg-type]
        catalogs.append(catalog)

    with root_cause_stack(endpoint, prefix, register=register, assistant=True) as base:
        stack = AssistantStack(
            base, catalogs[0], AdvancingSleep(base.world.stack.authz.sessions.clock)
        )
        stack_ref.append(stack)
        yield stack
