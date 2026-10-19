"""``assistant_worker``: el consumidor del asistente en ``vigia-worker`` (LC-LAZ-13; BR-LAZ-67 a
76; PAT-LAZ-RES-04, RES-05, SEG-08 y ESC-01; NFR-LAZ-31 y 35; PR-LAZ-20 y 21; TASK-316).

Recibe ``assistant_requested`` (solo identificadores) con el contexto de la organización emisora
y, para la solicitud ``pending``, en este orden:

1. **Una sola vez**: ``pg_try_advisory_xact_lock`` de la solicitud en la transacción de la entrega
   (``loop.assistant_request|<solicitud>``). Si otra entrega del mismo evento ya la tiene, esta
   termina sin hacer nada; si la solicitud ya no está ``pending`` (entrega repetida tras una
   caída), tampoco: idempotente por ``event_id`` y por el estado de ``AssistantRequest``. El
   candado vive lo que la entrega: si el proceso muere, la bandeja la vuelve a entregar.
2. **Sin llamada ni espera** (BR-LAZ-74): sin clave del proveedor, ``unavailable`` con
   ``no_api_key``; pasado el **tope duro de 120 s** desde ``requested_at``, ``timeout``; con el
   **tope mensual de fichas** agotado, ``budget_exceeded`` **antes de construir el contexto**
   (PAT-LAZ-ESC-01). El circuito abierto lo atiende el despachador (no entrega) y la API
   (``circuit_open`` inmediato).
3. **Contexto**: el mismo objeto de la frontera que la API resumió al admitir
   (``build_context`` es determinista); si el resumen no coincide con ``context_digest``, no sale
   nada (``provider_error``).
4. **Intentos**: hasta tres llamadas (la primera y **dos propias** con retroceso de 5 s y 20 s, o
   la espera de la cabecera de un 429 si es mayor), cada una con tope de **60 s** o lo que quede
   de los 120 s. El reloj es el inyectado y la espera, el ``sleep`` inyectado: en pruebas, ninguno
   de los dos es de pared. ``attempt_count`` cuenta las llamadas y el uso de cada respuesta
   (fichas, duración, modelo, región, motivo de parada) se suma en la fila, **sin contenido**
   (NFR-LAZ-31).
5. **Motivo de parada antes del contenido** (``completion_failure``): ``refusal`` (200) y
   ``max_tokens`` marcan ``provider_error`` y nunca producen borrador; un 400 y demás
   permanentes, también.
6. **Salida** (``assess_output``, PR-LAZ-20): esquema estricto; citas contra la **lista cerrada
   de alias** (y de su clase); ``citations`` vacía solo con ``insufficient_evidence_declared``;
   y **los dos validadores** de U-04 (``blocked_vocabulary`` y ``person_attribution``) sobre
   ``draft_text`` y sobre cada hecho citado. Cualquier rechazo deja la solicitud
   ``rejected_by_validators`` **sin borrador**: el texto no se guarda ni se muestra.
7. **Borrador** en ``loop.assistant_draft``, fuera del expediente (BR-LAZ-73), con el modelo
   **de la respuesta** y las citas resueltas a sus referencias reales; la solicitud, ``ready``.

**Fallo declarado** (PAT-LAZ-RES-04): agotados los intentos por fallos transitorios o por tiempo,
la solicitud queda ``unavailable`` (``timeout`` o ``provider_error``) en su propia transacción
y la entrega termina con ``AssistantProviderDown`` (``ExternalDependencyDown``): el
``ConsumerBreaker`` de U-02 abre el circuito al primer fallo declarado, sin consumir intentos ni
producir cola muerta, y lo sondea cada 60 s. La sonda vuelve a entregar ese evento; como la
solicitud ya está resuelta, termina sin llamar y el circuito se cierra: la siguiente solicitud
real vuelve a probar al proveedor.

Las escrituras de la solicitud van en transacciones propias (``database.transaction`` con el
contexto de la entrega), no en la de la entrega: así quedan confirmadas aunque la entrega termine
con el fallo declarado. Ninguna toma otro candado que el de la fila que escribe.
"""

from __future__ import annotations

import asyncio
import enum
import json
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import text

from vigia_platform.ledger.free_text import (
    FreeTextField,
    FreeTextPolicyRegistry,
    FreeTextPreparation,
    FreeTextRejected,
)
from vigia_platform.loop.assistant.budget import report_budget, token_budget
from vigia_platform.loop.assistant.context import BuiltContext, build_context
from vigia_platform.loop.assistant.port import (
    AssistantCompletion,
    AssistantFailure,
    AssistantProviderPort,
    FailureKind,
    TokenUsage,
    completion_failure,
)
from vigia_platform.loop.assistant.schema import (
    MAX_DRAFT_CHARS,
    MAX_QUOTED_FACT_CHARS,
    AssistantOutput,
    parse_output,
)
from vigia_platform.loop.assistant.service import ASSISTANT_WORKER, TransactionSource
from vigia_platform.loop.domain.enums import AssistantRequestState, AssistantUnavailableCause
from vigia_platform.loop.events import ASSISTANT_REQUESTED, AssistantRequested
from vigia_platform.loop.observability.metrics import LoopMetricName, LoopMetrics, get_loop_metrics
from vigia_platform.shared.api.errors import ExternalDependencyDown
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7_at
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.outbox.publish import OutboxEvent
from vigia_platform.shared.outbox.registries import Consumer, ConsumerRegistry
from vigia_platform.shared.signing.keys import to_millisecond

__all__ = [
    "ASSISTANT_WORKER",
    "ASSISTANT_WORKER_CONCURRENCY",
    "ATTEMPT_TIMEOUT",
    "HARD_DEADLINE",
    "MAX_CALLS",
    "RETRY_BACKOFFS",
    "Accepted",
    "AssistantProviderDown",
    "AssistantWorker",
    "Rejected",
    "RejectionReason",
    "assess_output",
    "register_assistant_worker",
]

_log = get_logger("loop.assistant")

ASSISTANT_WORKER_CONCURRENCY: Final = 2
"""El mamparo de ``assistant_worker`` (PAT-LAZ-ESC-01): cada hueco puede esperar 60 s."""
ATTEMPT_TIMEOUT: Final = timedelta(seconds=60)
"""Tope de cada llamada al proveedor `[objetivo propio]` (NFR-LAZ-35)."""
HARD_DEADLINE: Final = timedelta(seconds=120)
"""Tope duro desde ``requested_at`` hasta ``unavailable`` con ``timeout`` (NFR-LAZ-35)."""
RETRY_BACKOFFS: Final = (timedelta(seconds=5), timedelta(seconds=20))
"""Los dos intentos propios y su retroceso (tech-stack-decisions §2.4)."""
MAX_CALLS: Final = 1 + len(RETRY_BACKOFFS)
GUARD: Final = timedelta(seconds=5)
"""Holgura sobre el tope del intento por si un adaptador no respetara el suyo."""

DRAFT_FIELD: Final = FreeTextField("assistant_draft", "/draft_text", 1, MAX_DRAFT_CHARS)
QUOTED_FACT_FIELD: Final = FreeTextField(
    "assistant_draft", "/citations[*]/quoted_fact_es", 1, MAX_QUOTED_FACT_CHARS
)
PROVIDER: Final = "anthropic"
"""``AssistantDraft.provider`` (constante del diseño, DE §2.11)."""

_LOCK: Final = text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))")
_REQUEST: Final = text(
    "SELECT r.request_id, r.analysis_id, r.state, r.requested_at, r.context_digest,"
    " a.zone_context, a.plant_id, a.zone_id, a.subject_id"
    " FROM loop.assistant_request AS r JOIN loop.root_cause_analysis AS a"
    " ON a.organization_id = r.organization_id AND a.plant_id = r.plant_id"
    " AND a.analysis_id = r.analysis_id"
    " WHERE r.organization_id = :organization_id AND r.request_id = :request_id"
)
_ANCHOR: Final = text(
    "SELECT anchor_record_id FROM loop.finding_state"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id AND zone_id = :zone_id"
    " AND subject_id = :subject_id"
)
_ATTEMPT: Final = text(
    "UPDATE loop.assistant_request SET attempt_count = attempt_count + 1"
    " WHERE organization_id = :organization_id AND request_id = :request_id"
    " AND state = 'pending'"
)
_USAGE: Final = text(
    "UPDATE loop.assistant_request SET input_tokens = input_tokens + :input_tokens,"
    " output_tokens = output_tokens + :output_tokens,"
    " cache_read_tokens = cache_read_tokens + :cache_read_tokens,"
    " provider_duration_ms = provider_duration_ms + :duration_ms,"
    " response_model = COALESCE(:response_model, response_model),"
    " inference_geo = COALESCE(:inference_geo, inference_geo),"
    " stop_reason = COALESCE(:stop_reason, stop_reason),"
    " provider_request_id = COALESCE(:provider_request_id, provider_request_id)"
    " WHERE organization_id = :organization_id AND request_id = :request_id"
    " AND state = 'pending'"
)
_RESOLVE: Final = text(
    "UPDATE loop.assistant_request SET state = :state, unavailable_cause = :cause,"
    " resolved_at = :at"
    " WHERE organization_id = :organization_id AND request_id = :request_id"
    " AND state = 'pending' RETURNING requested_at"
)
_INSERT_DRAFT: Final = text(
    "INSERT INTO loop.assistant_draft (draft_id, organization_id, request_id, draft_text,"
    " citations, insufficient_evidence_declared, proposed_cause_category, model_name,"
    " model_version, provider, validation_results, accepted_by_human, edited_by_human,"
    " created_at) VALUES (:draft_id, :organization_id, :request_id, :draft_text,"
    " CAST(:citations AS jsonb), :insufficient, :category, :model_name, :model_version,"
    " :provider, CAST(:validation_results AS jsonb), false, false, :at)"
)

type Sleep = Callable[[float], Awaitable[None]]


class AssistantProviderDown(ExternalDependencyDown):
    """Fallo declarado del proveedor: agotados los intentos (abre el circuito)."""


class RejectionReason(enum.StrEnum):
    """Por qué una salida no se muestra (``rejected_by_validators``); nunca lleva el texto."""

    SCHEMA_INVALID = "schema_invalid"
    CITATION_OUTSIDE_LIST = "citation_outside_list"
    NO_EVIDENCE = "no_evidence"
    BLOCKED_VOCABULARY_TERM = "blocked_vocabulary_term"
    PERSON_ATTRIBUTION_DETECTED = "person_attribution_detected"
    FREE_TEXT_POLICY = "free_text_policy"


_VALIDATOR_REASONS: Final[Mapping[str, RejectionReason]] = {
    "loop_blocked_vocabulary_term": RejectionReason.BLOCKED_VOCABULARY_TERM,
    "loop_person_attribution_detected": RejectionReason.PERSON_ATTRIBUTION_DETECTED,
}


@dataclass(frozen=True, slots=True)
class Accepted:
    """Una salida que se puede guardar como borrador: textos en NFC y citas resueltas."""

    output: AssistantOutput
    draft_text: str
    citations: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class Rejected:
    reason: RejectionReason


def assess_output(
    content_json: str | None,
    built: BuiltContext,
    policy: FreeTextPolicyRegistry,
    organization_id: uuid.UUID,
) -> Accepted | Rejected:
    """La salida del proveedor contra el esquema, la lista de alias y los dos validadores.

    Pura (sin red ni base: la caché de los validadores ya está preparada). Una salida que no se
    acepta no deja nada: el llamante no guarda su texto (PR-LAZ-20)."""
    output = parse_output(content_json)
    if output is None:
        return Rejected(RejectionReason.SCHEMA_INVALID)
    if not output.citations and not output.insufficient_evidence_declared:
        return Rejected(RejectionReason.NO_EVIDENCE)
    for citation in output.citations:
        ref = built.aliases.get(citation.id)
        if ref is None or ref.kind is not citation.kind:
            return Rejected(RejectionReason.CITATION_OUTSIDE_LIST)
    try:
        organization = organization_id
        draft_text = policy.apply(output.draft_text, DRAFT_FIELD, organization_id=organization)
        facts = [
            policy.apply(citation.quoted_fact_es, QUOTED_FACT_FIELD, organization_id=organization)
            for citation in output.citations
        ]
    except FreeTextRejected as rejected:
        return Rejected(
            _VALIDATOR_REASONS.get(rejected.detail_code or "", RejectionReason.FREE_TEXT_POLICY)
        )
    citations = tuple(
        {
            "kind": citation.kind.value,
            "id": citation.id,
            "quoted_fact_es": fact,
            "reference_id": (
                None
                if built.aliases[citation.id].reference_id is None
                else str(built.aliases[citation.id].reference_id)
            ),
        }
        for citation, fact in zip(output.citations, facts, strict=True)
    )
    return Accepted(output=output, draft_text=draft_text, citations=citations)


@dataclass(frozen=True, slots=True)
class _Pending:
    request_id: uuid.UUID
    requested_at: datetime
    context_digest: str
    zone_context: Mapping[str, Any]
    anchor_record_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class _Resolution:
    state: AssistantRequestState
    cause: AssistantUnavailableCause | None = None
    accepted: Accepted | None = None
    completion: AssistantCompletion | None = None


class AssistantWorker:
    """El manejador de ``assistant_worker`` (``ConsumerHandler``)."""

    def __init__(
        self,
        *,
        database: TransactionSource,
        provider: AssistantProviderPort,
        free_text: FreeTextPolicyRegistry,
        clock: Clock,
        sleep: Sleep | None = None,
        metrics: LoopMetrics | None = None,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._database = database
        self._provider = provider
        self._free_text = free_text
        self._clock = clock
        self._sleep: Sleep = sleep if sleep is not None else asyncio.sleep
        self._metrics = metrics if metrics is not None else get_loop_metrics()
        self._random_bytes = random_bytes

    def __repr__(self) -> str:
        return "AssistantWorker()"

    async def __call__(self, event: OutboxEvent, transaction: Transaction) -> None:
        payload = AssistantRequested.model_validate_json(
            json.dumps(dict(event.payload)), strict=True
        )
        request_id = uuid.UUID(str(payload.request_id))
        context = transaction.context
        locked = (
            await transaction.execute(_LOCK, {"key": f"loop.assistant_request|{request_id}"})
        ).scalar_one()
        if not locked:
            return  # otra entrega del mismo evento la está atendiendo
        pending = await self._pending(transaction, request_id)
        if pending is None:
            return  # ya resuelta (o inexistente): idempotente
        resolution, declared = await self._attend(transaction, context, pending)
        await self._resolve(context, pending, resolution)
        if declared:
            raise AssistantProviderDown(ASSISTANT_WORKER)

    # --- pasos -----------------------------------------------------------------------------------

    async def _attend(
        self, transaction: Transaction, context: ScopeContext, pending: _Pending
    ) -> tuple[_Resolution, bool]:
        """La resolución de la solicitud y si hubo fallo declarado del proveedor."""
        unavailable = AssistantRequestState.UNAVAILABLE
        if not self._provider.configured:
            return _Resolution(unavailable, AssistantUnavailableCause.NO_API_KEY), False
        deadline = pending.requested_at + HARD_DEADLINE
        if self._clock.now() >= deadline:
            return _Resolution(unavailable, AssistantUnavailableCause.TIMEOUT), False
        budget = await token_budget(transaction, context.organization_id, self._clock.now())
        report_budget(self._metrics, context.organization_id, budget)
        if budget.exhausted:
            return _Resolution(unavailable, AssistantUnavailableCause.BUDGET_EXCEEDED), False
        built = build_context(
            pending.zone_context,
            request_id=pending.request_id,
            anchor_record_id=pending.anchor_record_id,
        )
        if built.digest != pending.context_digest:
            _log.error("el contexto recompuesto no coincide con el admitido: no sale nada")
            return _Resolution(unavailable, AssistantUnavailableCause.PROVIDER_ERROR), False
        return await self._attempts(transaction, context, pending, built, deadline)

    async def _attempts(
        self,
        transaction: Transaction,
        context: ScopeContext,
        pending: _Pending,
        built: BuiltContext,
        deadline: datetime,
    ) -> tuple[_Resolution, bool]:
        unavailable = AssistantRequestState.UNAVAILABLE
        last: FailureKind = FailureKind.TIMEOUT
        for call in range(MAX_CALLS):
            remaining = deadline - self._clock.now()
            if remaining <= timedelta(0):
                last = FailureKind.TIMEOUT
                break
            outcome = await self._call(context, pending, built, min(ATTEMPT_TIMEOUT, remaining))
            if isinstance(outcome, AssistantCompletion):
                failure = completion_failure(outcome)
                if failure is None:
                    return await self._assessed(transaction, context, built, outcome), False
                return _Resolution(unavailable, AssistantUnavailableCause.PROVIDER_ERROR), False
            if outcome.kind not in (FailureKind.TRANSIENT, FailureKind.TIMEOUT):
                return _Resolution(unavailable, AssistantUnavailableCause.PROVIDER_ERROR), False
            last = outcome.kind
            if call == MAX_CALLS - 1:
                break
            wait = RETRY_BACKOFFS[call]
            if outcome.retry_after is not None and outcome.retry_after > wait:
                wait = outcome.retry_after
            if self._clock.now() + wait >= deadline:
                last = FailureKind.TIMEOUT
                break
            await self._sleep(wait.total_seconds())
        cause = (
            AssistantUnavailableCause.TIMEOUT
            if last is FailureKind.TIMEOUT or self._clock.now() >= deadline
            else AssistantUnavailableCause.PROVIDER_ERROR
        )
        return _Resolution(unavailable, cause), True

    async def _call(
        self, context: ScopeContext, pending: _Pending, built: BuiltContext, timeout: timedelta
    ) -> AssistantCompletion | AssistantFailure:
        """Una llamada: cuenta el intento antes de salir y suma el uso de la respuesta."""
        await self._write(context, _ATTEMPT, self._keys(context, pending))
        started = self._clock.now()
        try:
            async with asyncio.timeout((timeout + GUARD).total_seconds()):
                outcome = await self._provider.complete(built.context, timeout=timeout)
        except TimeoutError:
            outcome = AssistantFailure(kind=FailureKind.TIMEOUT)
        elapsed = max(timedelta(0), self._clock.now() - started)
        usage, model, request = _usage_of(outcome)
        stop = outcome.stop_reason if isinstance(outcome, AssistantCompletion) else None
        await self._write(
            context,
            _USAGE,
            {
                **self._keys(context, pending),
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_tokens": usage.cache_read_tokens,
                "duration_ms": int(elapsed / timedelta(milliseconds=1)),
                "response_model": model,
                "inference_geo": usage.inference_geo,
                "stop_reason": stop,
                "provider_request_id": request,
            },
        )
        organization = str(context.organization_id)
        tokens = self._metrics.counter(LoopMetricName.ASSISTANT_TOKENS_TOTAL)
        for direction, amount in (("input", usage.input_tokens), ("output", usage.output_tokens)):
            tokens.add(amount, {"token_direction": direction, "organization_id": organization})
        return outcome

    async def _assessed(
        self,
        transaction: Transaction,
        context: ScopeContext,
        built: BuiltContext,
        completion: AssistantCompletion,
    ) -> _Resolution:
        await self._prepare(transaction)
        verdict = assess_output(
            completion.content_json, built, self._free_text, context.organization_id
        )
        if isinstance(verdict, Rejected):
            _log.warning("salida del asistente rechazada", reason=verdict.reason.value)
            return _Resolution(AssistantRequestState.REJECTED_BY_VALIDATORS)
        return _Resolution(AssistantRequestState.READY, accepted=verdict, completion=completion)

    # --- lecturas y escrituras -------------------------------------------------------------------

    @staticmethod
    async def _pending(transaction: Transaction, request_id: uuid.UUID) -> _Pending | None:
        organization = transaction.context.organization_id
        row = (
            (
                await transaction.execute(
                    _REQUEST, {"organization_id": organization, "request_id": request_id}
                )
            )
            .mappings()
            .first()
        )
        if row is None or row["state"] != AssistantRequestState.PENDING.value:
            return None
        anchor = (
            await transaction.execute(
                _ANCHOR,
                {
                    "organization_id": organization,
                    "plant_id": row["plant_id"],
                    "zone_id": row["zone_id"],
                    "subject_id": row["subject_id"],
                },
            )
        ).first()
        zone_context = row["zone_context"]
        if isinstance(zone_context, str):
            zone_context = json.loads(zone_context)
        return _Pending(
            request_id=request_id,
            requested_at=row["requested_at"],
            context_digest=row["context_digest"],
            zone_context=zone_context,
            anchor_record_id=None if anchor is None else uuid.UUID(str(anchor.anchor_record_id)),
        )

    async def _prepare(self, transaction: Transaction) -> None:
        """Los preparadores de los validadores (la versión de la lista de la organización)."""
        if not self._free_text.has_preparers:
            return

        async def rows(statement: Any, parameters: Mapping[str, Any]) -> Sequence[Any]:
            return (await transaction.execute(statement, parameters)).all()

        await self._free_text.prepare(
            FreeTextPreparation(
                organization_id=transaction.context.organization_id, rows=rows, clock=self._clock
            )
        )

    @staticmethod
    def _keys(context: ScopeContext, pending: _Pending) -> dict[str, Any]:
        return {"organization_id": context.organization_id, "request_id": pending.request_id}

    async def _write(self, context: ScopeContext, statement: Any, parameters: Any) -> None:
        async with self._database.transaction(context) as transaction:
            await transaction.execute(statement, parameters)

    async def _resolve(
        self, context: ScopeContext, pending: _Pending, resolution: _Resolution
    ) -> None:
        """La solicitud sale de ``pending`` (una sola vez) y, si está lista, su borrador."""
        at = to_millisecond(self._clock.now())
        async with self._database.transaction(context) as transaction:
            row = (
                await transaction.execute(
                    _RESOLVE,
                    {
                        **self._keys(context, pending),
                        "state": resolution.state.value,
                        "cause": None if resolution.cause is None else resolution.cause.value,
                        "at": at,
                    },
                )
            ).first()
            if row is None:
                return  # otra entrega la resolvió antes
            accepted, completion = resolution.accepted, resolution.completion
            if accepted is not None and completion is not None:
                await transaction.execute(
                    _INSERT_DRAFT,
                    {
                        "draft_id": uuid7_at(at, self._random_bytes(10)),
                        "organization_id": context.organization_id,
                        "request_id": pending.request_id,
                        "draft_text": accepted.draft_text,
                        "citations": json.dumps(list(accepted.citations), ensure_ascii=False),
                        "insufficient": accepted.output.insufficient_evidence_declared,
                        "category": accepted.output.proposed_cause_category.value,
                        "model_name": completion.model_name,
                        "model_version": completion.model_version,
                        "provider": PROVIDER,
                        "validation_results": json.dumps(
                            [
                                {"validator": name, "verdict": "accepted"}
                                for name in self._free_text.validator_names
                            ]
                        ),
                        "at": at,
                    },
                )
        self._observe(context, pending, resolution, at)

    def _observe(
        self, context: ScopeContext, pending: _Pending, resolution: _Resolution, at: datetime
    ) -> None:
        state = resolution.state.value
        self._metrics.counter(LoopMetricName.ASSISTANT_REQUESTS_TOTAL).add(
            1, {"assistant_state": state, "organization_id": str(context.organization_id)}
        )
        self._metrics.histogram(LoopMetricName.ASSISTANT_REQUEST_DURATION_MS).record(
            max(0, int((at - pending.requested_at) / timedelta(milliseconds=1))),
            {"assistant_state": state},
        )
        self._metrics.gauge(LoopMetricName.ASSISTANT_UNAVAILABLE).set(
            1 if resolution.state is AssistantRequestState.UNAVAILABLE else 0
        )


def _usage_of(
    outcome: AssistantCompletion | AssistantFailure,
) -> tuple[TokenUsage, str | None, str | None]:
    if isinstance(outcome, AssistantCompletion):
        return outcome.usage, outcome.model_name, outcome.provider_request_id
    return outcome.usage or TokenUsage(), outcome.model_name, outcome.provider_request_id


def register_assistant_worker(registry: ConsumerRegistry, worker: AssistantWorker) -> Consumer:
    """Registra ``assistant_worker`` (U-04, con dependencia externa: su cortacircuito)."""
    return registry.register(
        Consumer(
            consumer_name=ASSISTANT_WORKER,
            unit=ActorUnit.U04,
            subscribed_events=(ASSISTANT_REQUESTED,),
            handler=worker,
            has_external_dependency=True,
        )
    )
