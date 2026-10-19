"""``loop.assistant.service`` en ``vigia-api``: SUP-U05-19 y 20 (LC-LAZ-13; BR-LAZ-73 y 74;
PAT-LAZ-RES-04 y ESC-01; TASK-316).

``AssistantService.request(context, analysis_id)`` (``crear_solicitud_al_asistente``), en este
orden; si algo falla no escribe nada ni publica nada:

1. **Alcance**: el análisis de la organización del contexto y ``root_cause.analyze`` sobre su
   zona; si no, ``not_found`` (igual que uno inexistente, BR-NUC-09).
2. **Cortacircuito**: con el circuito de ``assistant_worker`` abierto (o en sonda),
   ``AssistantUnavailable(circuit_open)`` **de inmediato**, sin llamada externa, sin espera y sin
   fila (503 ``loop_assistant_unavailable``). El flujo guiado sigue operando (BR-LAZ-74).
3. **Admisión** (``loop.assistant.budget.admit``, en la transacción de la escritura): el candado de
   la organización, los límites exactos (``AssistantRateLimited``, 429) y el tope mensual de
   fichas (``AssistantUnavailable(budget_exceeded)``, 503) **antes de construir el contexto**.
4. **Contexto**: ``build_context`` sobre el contexto objetivo congelado del análisis; solo se
   guarda su resumen (``context_digest``), nunca el texto.
5. En esa misma transacción: el evento interno ``assistant_requested`` (solo identificadores) y la
   fila ``pending`` con su ``outbox_event_id``. Respuesta 202: el flujo guiado continúa.

``AssistantService.status(context, request_id)`` (``consultar_estado``): la solicitud de la
organización con ``root_cause.analyze`` sobre su zona; ``ready`` con su borrador (marcado como
tal, con sus citas y el modelo **de la respuesta**), ``pending`` o ``unavailable`` con su causa.
Una solicitud ``rejected_by_validators`` responde ``AssistantOutputRejected``
(``loop_assistant_output_rejected``, A-68): el texto rechazado nunca se guardó ni se muestra.

Esta capa **no construye ningún adaptador** del proveedor ni conoce su clave: la clave vive solo
en ``vigia-worker`` (NFR-LAZ-26). Sin clave, el consumidor responde ``no_api_key``.
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol

from sqlalchemy import text

from vigia_platform.identity.authz.authorize import Authorizer, Resource, ResourceNotFound
from vigia_platform.identity.authz.context import with_unit
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.loop.assistant.budget import (
    AssistantBudgetExhausted,
    AssistantRateLimited,
    admit,
    report_budget,
)
from vigia_platform.loop.assistant.context import build_context
from vigia_platform.loop.domain.enums import (
    AssistantRequestState,
    AssistantUnavailableCause,
    CauseCategory,
)
from vigia_platform.loop.events import ASSISTANT_REQUESTED, AssistantRequested
from vigia_platform.loop.observability.metrics import LoopMetricName, LoopMetrics, get_loop_metrics
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7_at
from vigia_platform.shared.outbox.breaker import CircuitState
from vigia_platform.shared.outbox.publish import NewEvent, OutboxPort
from vigia_platform.shared.signing.keys import to_millisecond

__all__ = [
    "ASSISTANT_WORKER",
    "AssistantDraftView",
    "AssistantOutputRejected",
    "AssistantRateLimited",
    "AssistantRequestView",
    "AssistantService",
    "AssistantUnavailable",
    "read_request",
]

ASSISTANT_WORKER: Final = "assistant_worker"
"""El consumidor cuyo cortacircuito decide la respuesta inmediata (``shared.consumer``)."""
ANALYSIS_RESOURCE_KIND: Final = "root_cause_analysis"
REQUEST_RESOURCE_KIND: Final = "assistant_request"

_ANALYSIS: Final = text(
    "SELECT analysis_id, organization_id, plant_id, zone_id, subject_id, zone_context"
    " FROM loop.root_cause_analysis"
    " WHERE organization_id = :organization_id AND analysis_id = :analysis_id"
)
_ANCHOR: Final = text(
    "SELECT anchor_record_id FROM loop.finding_state"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id AND zone_id = :zone_id"
    " AND subject_id = :subject_id"
)
_CIRCUIT: Final = text("SELECT circuit_state FROM shared.consumer WHERE consumer_name = :consumer")
_INSERT: Final = text(
    "INSERT INTO loop.assistant_request (request_id, organization_id, plant_id, zone_id,"
    " analysis_id, user_id, requested_by, requested_at, state, outbox_event_id, attempt_count,"
    " context_digest) VALUES (:request_id, :organization_id, :plant_id, :zone_id, :analysis_id,"
    " :user_id, CAST(:requested_by AS jsonb), :requested_at, 'pending', :outbox_event_id, 0,"
    " :context_digest)"
)
_REQUEST: Final = text(
    "SELECT request_id, organization_id, plant_id, zone_id, analysis_id, state,"
    " unavailable_cause, requested_at, resolved_at, attempt_count"
    " FROM loop.assistant_request"
    " WHERE organization_id = :organization_id AND request_id = :request_id"
)
_DRAFT: Final = text(
    "SELECT draft_id, draft_text, citations, insufficient_evidence_declared,"
    " proposed_cause_category, model_name, model_version, provider, created_at"
    " FROM loop.assistant_draft"
    " WHERE organization_id = :organization_id AND request_id = :request_id"
)


class AssistantUnavailable(Exception):
    """El asistente no está disponible ahora, con su causa (503 ``loop_assistant_unavailable``)."""

    def __init__(self, cause: AssistantUnavailableCause) -> None:
        self.cause = cause
        super().__init__(cause.value)


class AssistantOutputRejected(Exception):
    """La salida no pasó los validadores y no se muestra (``loop_assistant_output_rejected``)."""


@dataclass(frozen=True, slots=True)
class AssistantDraftView:
    """El borrador, marcado como tal: nunca entra al expediente sin firma humana (BR-LAZ-73)."""

    draft_id: uuid.UUID
    draft_text: str
    citations: tuple[Mapping[str, Any], ...]
    insufficient_evidence_declared: bool
    proposed_cause_category: CauseCategory | None
    model_name: str
    model_version: str
    provider: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AssistantRequestView:
    """La solicitud tal como la ve el coordinador (SUP-U05-19 y 20)."""

    request_id: uuid.UUID
    analysis_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    state: AssistantRequestState
    unavailable_cause: AssistantUnavailableCause | None
    requested_at: datetime
    resolved_at: datetime | None
    attempt_count: int
    draft: AssistantDraftView | None = None


class TransactionSource(Protocol):
    """``shared.db.Database``: el único camino a la base."""

    def transaction(
        self, context: ScopeContext
    ) -> contextlib.AbstractAsyncContextManager[Transaction]: ...


def _uuid(value: object) -> uuid.UUID:
    return uuid.UUID(str(value))


def _json(value: object) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def read_request(
    transaction: Transaction, request_id: uuid.UUID
) -> AssistantRequestView | None:
    """La solicitud (y su borrador, si está lista) visible en ``transaction``."""
    keys = {"organization_id": transaction.context.organization_id, "request_id": request_id}
    row = (await transaction.execute(_REQUEST, keys)).mappings().first()
    if row is None:
        return None
    draft: AssistantDraftView | None = None
    state = AssistantRequestState(row["state"])
    if state is AssistantRequestState.READY:
        found = (await transaction.execute(_DRAFT, keys)).mappings().first()
        if found is not None:
            category = found["proposed_cause_category"]
            draft = AssistantDraftView(
                draft_id=_uuid(found["draft_id"]),
                draft_text=found["draft_text"],
                citations=tuple(_json(found["citations"])),
                insufficient_evidence_declared=bool(found["insufficient_evidence_declared"]),
                proposed_cause_category=None if category is None else CauseCategory(category),
                model_name=found["model_name"],
                model_version=found["model_version"],
                provider=found["provider"],
                created_at=found["created_at"],
            )
    cause = row["unavailable_cause"]
    return AssistantRequestView(
        request_id=_uuid(row["request_id"]),
        analysis_id=_uuid(row["analysis_id"]),
        plant_id=_uuid(row["plant_id"]),
        zone_id=_uuid(row["zone_id"]),
        state=state,
        unavailable_cause=None if cause is None else AssistantUnavailableCause(cause),
        requested_at=row["requested_at"],
        resolved_at=row["resolved_at"],
        attempt_count=int(row["attempt_count"]),
        draft=draft,
    )


@repository
class AssistantService:
    """``crear_solicitud_al_asistente`` y ``consultar_estado_solicitud_al_asistente``."""

    def __init__(
        self,
        *,
        database: TransactionSource,
        authorizer: Authorizer,
        outbox: OutboxPort,
        clock: Clock,
        metrics: LoopMetrics | None = None,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._database = database
        self._authorizer = authorizer
        self._outbox = outbox
        self._clock = clock
        self._metrics = metrics if metrics is not None else get_loop_metrics()
        self._random_bytes = random_bytes

    def __repr__(self) -> str:
        return "AssistantService()"

    # --- SUP-U05-19 ------------------------------------------------------------------------------

    async def request(self, context: ScopeContext, analysis_id: uuid.UUID) -> AssistantRequestView:
        """Crea la solicitud ``pending`` y encola el trabajo; el flujo guiado continúa."""
        authorized, analysis = await self._authorized_analysis(context, analysis_id)
        writer = with_unit(authorized, ActorUnit.U04)
        now = to_millisecond(self._clock.now())
        request_id = uuid7_at(now, self._random_bytes(10))
        actor = writer.actor
        async with self._database.transaction(writer) as transaction:
            if await self._circuit_open(transaction):
                raise AssistantUnavailable(AssistantUnavailableCause.CIRCUIT_OPEN)
            try:
                budget = await admit(transaction, writer, now)
            except AssistantBudgetExhausted:
                raise AssistantUnavailable(AssistantUnavailableCause.BUDGET_EXCEEDED) from None
            report_budget(self._metrics, writer.organization_id, budget)
            anchor = (
                await transaction.execute(
                    _ANCHOR,
                    {
                        "organization_id": analysis["organization_id"],
                        "plant_id": analysis["plant_id"],
                        "zone_id": analysis["zone_id"],
                        "subject_id": analysis["subject_id"],
                    },
                )
            ).first()
            built = build_context(
                _json(analysis["zone_context"]),
                request_id=request_id,
                anchor_record_id=None if anchor is None else _uuid(anchor.anchor_record_id),
            )
            publication = await self._outbox.publish(
                transaction,
                NewEvent(
                    event_name=ASSISTANT_REQUESTED,
                    payload=AssistantRequested(
                        request_id=str(request_id),
                        analysis_id=str(analysis["analysis_id"]),
                        zone_id=str(analysis["zone_id"]),
                    ),
                    plant_id=_uuid(analysis["plant_id"]),
                ),
            )
            await transaction.execute(
                _INSERT,
                {
                    "request_id": request_id,
                    "organization_id": writer.organization_id,
                    "plant_id": analysis["plant_id"],
                    "zone_id": analysis["zone_id"],
                    "analysis_id": analysis["analysis_id"],
                    "user_id": actor.id,
                    "requested_by": json.dumps(
                        {
                            "user_id": str(actor.id),
                            "display_name": actor.display_name_snapshot,
                            "role_in_use": (
                                None if actor.role_in_use is None else actor.role_in_use.value
                            ),
                        },
                        ensure_ascii=False,
                    ),
                    "requested_at": now,
                    "outbox_event_id": publication.event.event_id,
                    "context_digest": built.digest,
                },
            )
        self._metrics.counter(LoopMetricName.ASSISTANT_REQUESTS_TOTAL).add(
            1,
            {
                "assistant_state": AssistantRequestState.PENDING.value,
                "organization_id": str(writer.organization_id),
            },
        )
        return AssistantRequestView(
            request_id=request_id,
            analysis_id=_uuid(analysis["analysis_id"]),
            plant_id=_uuid(analysis["plant_id"]),
            zone_id=_uuid(analysis["zone_id"]),
            state=AssistantRequestState.PENDING,
            unavailable_cause=None,
            requested_at=now,
            resolved_at=None,
            attempt_count=0,
        )

    # --- SUP-U05-20 ------------------------------------------------------------------------------

    async def status(self, context: ScopeContext, request_id: uuid.UUID) -> AssistantRequestView:
        """El estado de la solicitud y, si está lista, su borrador con sus citas."""
        if not isinstance(context, ScopeContext) or type(request_id) is not uuid.UUID:
            raise ResourceNotFound()
        async with self._database.transaction(context) as transaction:
            found = await read_request(transaction, request_id)
        if found is None:
            raise ResourceNotFound()
        await self._authorize(
            context,
            Resource(
                context.organization_id,
                REQUEST_RESOURCE_KIND,
                found.request_id,
                plant_id=found.plant_id,
                zone_id=found.zone_id,
            ),
        )
        if found.state is AssistantRequestState.REJECTED_BY_VALIDATORS:
            raise AssistantOutputRejected()
        return found

    # --- alcance ---------------------------------------------------------------------------------

    async def _authorized_analysis(
        self, context: ScopeContext, analysis_id: uuid.UUID
    ) -> tuple[ScopeContext, Mapping[str, Any]]:
        """El análisis de la organización y ``root_cause.analyze`` sobre su zona."""
        if not isinstance(context, ScopeContext) or type(analysis_id) is not uuid.UUID:
            raise ResourceNotFound()
        async with self._database.transaction(context) as transaction:
            row = (
                (
                    await transaction.execute(
                        _ANALYSIS,
                        {"organization_id": context.organization_id, "analysis_id": analysis_id},
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise ResourceNotFound()
        analysis = dict(row)
        authorized = await self._authorize(
            context,
            Resource(
                _uuid(analysis["organization_id"]),
                ANALYSIS_RESOURCE_KIND,
                _uuid(analysis["analysis_id"]),
                plant_id=_uuid(analysis["plant_id"]),
                zone_id=_uuid(analysis["zone_id"]),
            ),
        )
        return authorized, analysis

    async def _authorize(self, context: ScopeContext, resource: Resource) -> ScopeContext:
        authorized = await self._authorizer.authorize(
            context, PermissionKey.ROOT_CAUSE_ANALYZE, resource
        )
        if authorized.concession_id is not None:  # pragma: no cover - la matriz ya lo deniega
            await self._authorizer.deny(authorized, PermissionKey.ROOT_CAUSE_ANALYZE, resource)
        return authorized

    @staticmethod
    async def _circuit_open(transaction: Transaction) -> bool:
        """El circuito de ``assistant_worker`` no está cerrado (abierto o con la sonda en curso).

        Sin la fila (el worker aún no registró su consumidor), cerrado: el consumidor decide."""
        state = (await transaction.execute(_CIRCUIT, {"consumer": ASSISTANT_WORKER})).scalar()
        return state is not None and state != CircuitState.CLOSED.value
