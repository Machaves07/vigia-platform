"""Dobles de prueba de ``shared.signing`` (TASK-115): gestor de secretos, base y expediente.

Todo en memoria y con datos generados (NFR-CTR-43). ``InMemoryKeyStore`` aplica las mismas
restricciones que ``identity.signing_key`` (una ``active`` y una ``overlapping`` por propósito,
``key_id`` único, ``valid_until > valid_from``) y guarda, con cada publicación, el estado de las
claves en el instante en que se emitió. ``RecordingEvents`` valida cada contenido con el modelo
estricto del tipo registrado (``key_rotated``, ``key_set_published``) y cada carga de evento con
el de la bandeja, como harían ``EscritorExpediente`` y ``OutboxPort``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from vigia_contracts.clock import SimulatedClock

from tests.factories import make_context
from vigia_platform.ledger.record_types.u02 import KeyRotated, KeySetPublished
from vigia_platform.shared.context import ActorKind, ScopeContext
from vigia_platform.shared.outbox.u02_events import KeySetPublished as KeySetPublishedEvent
from vigia_platform.shared.secrets import Dependency, SecretNotFound, SecretsUnavailable
from vigia_platform.shared.signing import (
    KeySetPublicationRecord,
    KeyStateConflict,
    KeyStatus,
    KeyStoreSnapshot,
    KeyTransition,
    RotationCommit,
    SigningKeyRecord,
    SigningPurpose,
    SigningService,
)

START = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
PROVIDER_ORGANIZATION_ID = uuid.UUID("0192f1a2-7c00-7000-8000-00000000c0de")
ENVIRONMENT = "pilot"
BOOTSTRAP_ORDER: tuple[SigningPurpose, ...] = (
    SigningPurpose.KEY_SET,
    SigningPurpose.CATALOG,
    SigningPurpose.GATE,
    SigningPurpose.LIVE_VIEW_TOKEN,
    SigningPurpose.CHECKPOINT,
)
"""Alta inicial: la ``key_set`` primero, para que las demás rotaciones ya publiquen."""


class InMemorySecrets:
    """``SecretsPort`` en memoria; ``down`` simula el gestor inaccesible."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.down = False
        self.gets = 0

    def __repr__(self) -> str:
        return f"InMemorySecrets(secrets={len(self.values)})"

    async def get(self, arn: str) -> bytes:
        self.gets += 1
        if self.down:
            raise SecretsUnavailable(Dependency.SECRETS_MANAGER, "get_secret")
        try:
            return self.values[arn]
        except KeyError:
            raise SecretNotFound from None

    async def create(self, name: str, value: bytes) -> str:
        if self.down:
            raise SecretsUnavailable(Dependency.SECRETS_MANAGER, "create_secret")
        arn = f"arn:aws:secretsmanager:us-east-1:000000000000:secret:{name}"
        if arn in self.values:
            raise ValueError("el secreto ya existe")
        self.values[arn] = value
        return arn


@dataclass(frozen=True)
class StoredPublication:
    """Una publicación y el estado de las claves justo después de confirmarla."""

    record: KeySetPublicationRecord
    keys_at_issue: Mapping[str, SigningKeyRecord]


class InMemoryKeyStore:
    """``SigningKeyStore`` con las restricciones de ``identity.signing_key``."""

    def __init__(self) -> None:
        self.keys: dict[str, SigningKeyRecord] = {}
        self.publications: list[StoredPublication] = []
        self.down = False
        self.fail_next_commit = False

    async def load(self) -> KeyStoreSnapshot:
        if self.down:
            raise ConnectionError("base de datos inaccesible")
        latest = self.publications[-1].record if self.publications else None
        return KeyStoreSnapshot(keys=tuple(self.keys.values()), publication=latest)

    async def commit_rotation(self, commit: RotationCommit) -> None:
        self._check_available()
        if commit.publication is not None:
            # Como ``SqlSigningKeyStore``: solo se publica sobre la última publicación leída.
            latest = self.publications[-1].record.publication_id if self.publications else None
            if latest != commit.expected_publication_id:
                raise KeyStateConflict
        updated = self._apply(commit.transitions)
        if commit.new_key.key_id in updated:
            raise ValueError("key_id duplicado")
        updated[commit.new_key.key_id] = commit.new_key
        _check_constraints(updated.values())
        self.keys = updated
        if commit.publication is not None:
            self.publications.append(StoredPublication(commit.publication, dict(updated)))

    async def commit_transitions(self, transitions: Sequence[KeyTransition]) -> None:
        self._check_available()
        updated = self._apply(transitions)
        _check_constraints(updated.values())
        self.keys = updated

    def _check_available(self) -> None:
        if self.down:
            raise ConnectionError("base de datos inaccesible")
        if self.fail_next_commit:
            self.fail_next_commit = False
            raise ConnectionError("la transacción se cortó")

    def _apply(self, transitions: Sequence[KeyTransition]) -> dict[str, SigningKeyRecord]:
        updated = dict(self.keys)
        for transition in transitions:
            # ``UPDATE … WHERE key_id = :id AND status = :expected`` que afecta una sola fila.
            current = updated.get(transition.key_id)
            if current is None or current.status is not transition.expected_status:
                raise KeyStateConflict
            updated[transition.key_id] = replace(
                updated[transition.key_id],
                status=transition.status,
                valid_until=transition.valid_until,
            )
        return updated


class GatedKeyStore(InMemoryKeyStore):
    """Base que, con ``gate`` puesto, toma la foto al leer y la devuelve cuando se abre la puerta:
    reproduce un refresco cuya lectura llega tarde, después de una rotación (sonda R1)."""

    def __init__(self) -> None:
        super().__init__()
        self.gate: asyncio.Event | None = None
        self.reading = asyncio.Event()

    async def load(self) -> KeyStoreSnapshot:
        snapshot = await super().load()
        gate = self.gate
        if gate is not None:
            self.gate = None
            self.reading.set()
            await gate.wait()
        return snapshot


def _check_constraints(keys: Any) -> None:
    listed = list(keys)
    for purpose in SigningPurpose:
        for status in (KeyStatus.ACTIVE, KeyStatus.OVERLAPPING):
            count = sum(1 for k in listed if k.purpose is purpose and k.status is status)
            if count > 1:
                raise ValueError(f"viola la unicidad de {status.value} para {purpose.value}")
    for key in listed:
        if not key.valid_until > key.valid_from:
            raise ValueError("viola signing_key_validity")


@dataclass
class RecordingEvents:
    """``KeyEventWriter`` que valida y guarda lo escrito en la cadena del proveedor."""

    rotated: list[dict[str, Any]] = field(default_factory=list)
    published: list[tuple[dict[str, Any], dict[str, Any]]] = field(default_factory=list)
    contexts: list[ScopeContext] = field(default_factory=list)
    down: bool = False

    async def key_rotated(self, context: ScopeContext, content: Mapping[str, Any]) -> None:
        if self.down:
            raise ConnectionError("expediente inaccesible")
        KeyRotated.model_validate_json(json.dumps(dict(content)))
        self.contexts.append(context)
        self.rotated.append(dict(content))

    async def key_set_published(
        self, context: ScopeContext, content: Mapping[str, Any], event: Mapping[str, Any]
    ) -> None:
        if self.down:
            raise ConnectionError("expediente inaccesible")
        KeySetPublished.model_validate_json(json.dumps(dict(content)))
        KeySetPublishedEvent.model_validate_json(json.dumps(dict(event)))
        self.contexts.append(context)
        self.published.append((dict(content), dict(event)))


def provider_context(kind: ActorKind = ActorKind.OPERATOR) -> ScopeContext:
    """Contexto de la organización proveedora (orden administrativa o iteración periódica)."""
    return make_context(kind=kind, organization_id=PROVIDER_ORGANIZATION_ID)


@dataclass
class SigningWorld:
    """Un servicio de firma con sus dobles y un reloj simulado."""

    clock: SimulatedClock
    secrets: InMemorySecrets
    store: InMemoryKeyStore
    events: RecordingEvents
    service: SigningService

    def new_service(self, random_bytes: Callable[[int], bytes] | None = None) -> SigningService:
        """Otro proceso sobre el mismo gestor, la misma base y el mismo expediente."""
        return build_service(self.clock, self.secrets, self.store, self.events, random_bytes)


def build_service(
    clock: SimulatedClock,
    secrets: InMemorySecrets,
    store: InMemoryKeyStore,
    events: RecordingEvents,
    random_bytes: Callable[[int], bytes] | None = None,
    metrics: Any = None,
) -> SigningService:
    extra: dict[str, Any] = {} if random_bytes is None else {"random_bytes": random_bytes}
    return SigningService(
        provider_organization_id=PROVIDER_ORGANIZATION_ID,
        store=store,
        secrets=secrets,
        events=events,
        clock=clock,
        environment=ENVIRONMENT,
        metrics=metrics,
        **extra,
    )


async def bootstrapped_world(
    start: datetime = START, metrics: Any = None, store: InMemoryKeyStore | None = None
) -> SigningWorld:
    """Alta inicial de las cinco claves y un proceso arrancado con todas requeridas."""
    clock = SimulatedClock(start)
    secrets, events = InMemorySecrets(), RecordingEvents()
    store = InMemoryKeyStore() if store is None else store
    bootstrap = build_service(clock, secrets, store, events)
    await bootstrap.start(required=())
    context = provider_context()
    for purpose in BOOTSTRAP_ORDER:
        await bootstrap.rotate(purpose, context=context)
    service = build_service(clock, secrets, store, events, metrics=metrics)
    await service.start()
    return SigningWorld(clock, secrets, store, events, service)
