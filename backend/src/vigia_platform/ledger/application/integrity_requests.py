"""Verificación de cadenas a demanda: la API la pide y el worker la ejecuta (LC-NUC-13; TASK-137).

``POST /integrity/verify`` (``integrity.verify``) no verifica en la API: el motor de
``ledger.chain.verify`` corre solo en el worker, con ``statement_timeout`` de 30 s
(PAT-NUC-REN-02; ``SqlIntegrityStore`` se niega a construirse sobre la API). La ruta pide la
verificación por la bandeja de salida y ``GET /integrity/results`` la consulta después:

- ``IntegrityRequests.request(context, chain)``: comprueba que la cadena existe en la organización
  del contexto (``ledger.chain_head``; si no, ``ChainNotFound``, que la ruta responde
  ``not_found``) y publica, en una transacción con ese contexto, el evento
  ``integrity_verification_requested`` (cadena, quién y cuándo; solo identificadores y marcas,
  BR-NUC-75). El evento lleva el ``correlation_id`` de la petición.
- ``IntegrityOnDemandConsumer`` (consumidor ``integrity_on_demand`` de U-02, sin dependencia
  externa): el despachador lo invoca en el worker con el contexto de la organización del evento
  (BR-NUC-80), y verifica la cadena con ``IntegrityService.verify(…, on_demand)``, que audita el
  resultado como ``integrity_verification`` con el ``correlation_id`` del evento y, ante
  ``broken``, publica ``integrity_compromised``.

**Idempotente por evento** (BR-NUC-76): antes de verificar busca en la auditoría un resultado
``on_demand`` de esa cadena con el ``correlation_id`` del evento; si ya está (una reentrega tras
una caída entre el efecto y la marca de entrega), no verifica otra vez.

Como las tareas ``verify_chains_*``, el manejador recibe la transacción del despachador y el motor
abre las suyas: la de la entrega queda abierta mientras dura la verificación (la misma decisión que
las tareas periódicas; anotada en el PR de TASK-137).
"""

from __future__ import annotations

import uuid
from typing import Any, Final, Protocol

from sqlalchemy import text

from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.ledger.chain.checkpoints import ChainKind, CheckpointChain
from vigia_platform.ledger.chain.verify import IntegrityResult, VerificationMode
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.outbox.publish import NewEvent, OutboxEvent, OutboxPort
from vigia_platform.shared.outbox.registries import Consumer, ConsumerRegistry
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "INTEGRITY_ON_DEMAND_CONSUMER",
    "INTEGRITY_VERIFICATION_REQUESTED",
    "ChainNotFound",
    "IntegrityOnDemandConsumer",
    "IntegrityRequests",
    "IntegrityVerifier",
    "VerificationRequest",
    "chain_of",
    "register_integrity_on_demand",
]

INTEGRITY_VERIFICATION_REQUESTED: Final = "integrity_verification_requested"
INTEGRITY_ON_DEMAND_CONSUMER: Final = "integrity_on_demand"

_CHAIN_EXISTS: Final = text(
    "SELECT 1 FROM ledger.chain_head WHERE organization_id = :organization_id"
    " AND kind = :kind AND plant_id IS NOT DISTINCT FROM CAST(:plant_id AS uuid)"
)

_ALREADY_VERIFIED: Final = text(
    "SELECT 1 FROM shared.audit_entry WHERE organization_id = :organization_id"
    " AND operation = 'integrity_verification' AND correlation_id = :correlation_id"
    " AND filters_json ->> 'mode' = 'on_demand' AND filters_json ->> 'chain_kind' = :kind"
    " AND (filters_json ->> 'plant_id') IS NOT DISTINCT FROM CAST(:plant_id AS text)"
    " LIMIT 1"
)


class ChainNotFound(LookupError):
    """La cadena no existe en la organización del contexto (``not_found``)."""

    code: Final = "not_found"

    def __init__(self) -> None:
        super().__init__("cadena no encontrada")


class VerificationRequest:
    """Lo que devuelve ``request``: el evento publicado y la cadena."""

    __slots__ = ("chain", "request_id", "requested_at")

    def __init__(self, request_id: uuid.UUID, chain: CheckpointChain, requested_at: str) -> None:
        self.request_id = request_id
        self.chain = chain
        self.requested_at = requested_at


@repository
class IntegrityRequests:
    """Publica ``integrity_verification_requested`` (la API; nunca verifica aquí)."""

    def __init__(self, *, database: LedgerDatabase, outbox: OutboxPort, clock: Clock) -> None:
        self._database = database
        self._outbox = outbox
        self._clock = clock

    async def request(self, context: ScopeContext, chain: CheckpointChain) -> VerificationRequest:
        if not isinstance(chain, CheckpointChain):
            raise TypeError("chain debe ser CheckpointChain")
        requested_at = format_timestamp(self._clock.now())
        payload: dict[str, Any] = {
            "chain_kind": chain.kind.value,
            "requested_by": str(context.actor.id),
            "requested_at": requested_at,
        }
        if chain.plant_id is not None:
            payload["plant_id"] = str(chain.plant_id)
        async with self._database.transaction(context) as transaction:
            exists = (
                await transaction.execute(_CHAIN_EXISTS, _chain_parameters(context, chain))
            ).first()
            if exists is None:
                raise ChainNotFound()
            publication = await self._outbox.publish(
                transaction,
                NewEvent(
                    event_name=INTEGRITY_VERIFICATION_REQUESTED,
                    payload=payload,
                    plant_id=chain.plant_id,
                ),
            )
        return VerificationRequest(publication.event.event_id, chain, requested_at)


class IntegrityVerifier(Protocol):
    """``IntegrityService.verify`` (el motor del worker)."""

    async def verify(
        self, context: ScopeContext, chain: CheckpointChain, mode: VerificationMode
    ) -> IntegrityResult: ...


def _chain_parameters(context: ScopeContext, chain: CheckpointChain) -> dict[str, Any]:
    return {
        "organization_id": context.organization_id,
        "kind": chain.kind.value,
        "plant_id": chain.plant_id,
    }


def chain_of(payload: Any) -> CheckpointChain:
    """La cadena de la carga de ``integrity_verification_requested``; ``ValueError`` si no vale."""
    if not isinstance(payload, dict):
        raise ValueError("carga sin forma de objeto")
    kind = ChainKind(str(payload.get("chain_kind")))
    plant = payload.get("plant_id")
    plant_id = None if plant is None else uuid.UUID(str(plant))
    if kind is ChainKind.AUDIT and plant_id is not None:
        raise ValueError("la cadena de auditoría no tiene planta")
    return CheckpointChain(kind, plant_id)


class IntegrityOnDemandConsumer:
    """El manejador de ``integrity_on_demand``: verifica la cadena pedida, una vez por evento."""

    def __init__(self, verifier: IntegrityVerifier) -> None:
        self._verifier = verifier

    async def __call__(self, event: OutboxEvent, transaction: Transaction) -> None:
        context = transaction.context
        chain = chain_of(dict(event.payload))
        parameters = {
            **_chain_parameters(context, chain),
            "plant_id": None if chain.plant_id is None else str(chain.plant_id),
            "correlation_id": context.correlation_id,
        }
        done = (await transaction.execute(_ALREADY_VERIFIED, parameters)).first()
        if done is not None:
            return
        await self._verifier.verify(context, chain, VerificationMode.ON_DEMAND)


def register_integrity_on_demand(
    registry: ConsumerRegistry, verifier: IntegrityVerifier
) -> Consumer:
    """Registra ``integrity_on_demand`` al arrancar el worker (U-02, sin dependencia externa)."""
    return registry.register(
        Consumer(
            consumer_name=INTEGRITY_ON_DEMAND_CONSUMER,
            unit=ActorUnit.U02,
            subscribed_events=(INTEGRITY_VERIFICATION_REQUESTED,),
            handler=IntegrityOnDemandConsumer(verifier),
        )
    )
