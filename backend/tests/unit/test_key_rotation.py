"""Tarea ``key_rotation_reminder`` y escritura de la rotación en el expediente (TASK-115;
BR-NUC-85 y 86).

Bordes de BR-NUC-85: aviso ``key_rotation_due`` cuando faltan 45 días (ventana de una corrida
diaria), rotación automática cuando faltan 30 días o menos, retiro exacto al pasar
``valid_until``. El ``LedgerKeyEventWriter`` escribe con el ``EscritorExpediente`` y convierte
un rechazo en ``KeyEventRejected``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.factories import make_context
from tests.signing_support import bootstrapped_world, provider_context
from vigia_platform.ledger.application.writer import (
    LedgerRejection,
    LedgerRejectionCode,
    Receipt,
)
from vigia_platform.shared.context import ActorKind, ActorUnit, ScopeContext
from vigia_platform.shared.key_rotation import (
    KEY_ROTATION_REMINDER,
    KeyEventRejected,
    LedgerKeyEventWriter,
    key_rotation_reminder_handler,
    register_key_rotation_reminder,
)
from vigia_platform.shared.outbox.publish import NewEvent
from vigia_platform.shared.outbox.registries import (
    OutboxRegistrationRejected,
    PeriodicTaskRegistry,
    ScheduleKind,
)
from vigia_platform.shared.outbox.u02_events import KeyRotationDue
from vigia_platform.shared.signing import KeyStatus, SigningKeyRecord, SigningPurpose
from vigia_platform.shared.signing.keys import (
    days_to_expiry,
    expiry_transitions,
    reminder_decision,
    rotation_transitions,
)

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
MS = timedelta(milliseconds=1)
PUBLIC_KEY = "11qYAYKxCrfVS/7TyWQHOg7hcvPapiMlrwIaaPcHURo="


def _reference(purpose: SigningPurpose, name: str) -> str:
    """Nombre sintético del secreto de una versión de clave (se arma por partes)."""
    return "/".join(("vigia", "pilot", "signing", purpose.value, name))


def _key(
    purpose: SigningPurpose,
    valid_until: datetime,
    status: KeyStatus = KeyStatus.ACTIVE,
    key_id: str | None = None,
) -> SigningKeyRecord:
    return SigningKeyRecord(
        key_id=key_id or f"{purpose.value}-k",
        purpose=purpose,
        public_key=PUBLIC_KEY,
        private_key_ref=_reference(purpose, "k"),
        valid_from=valid_until - timedelta(days=365),
        valid_until=valid_until,
        status=status,
        created_at=valid_until - timedelta(days=365),
        rotated_by=uuid.uuid4(),
    )


# --- Bordes de BR-NUC-85 ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("remaining", "notice", "rotate"),
    [
        (timedelta(days=45) + MS, False, False),
        (timedelta(days=45), True, False),
        (timedelta(days=44) + MS, True, False),
        (timedelta(days=44), False, False),
        (timedelta(days=30) + MS, False, False),
        (timedelta(days=30), False, True),
        (timedelta(0), False, True),
        (-timedelta(days=3), False, True),
    ],
    ids=["45d+1ms", "45d", "44d+1ms", "44d", "30d+1ms", "30d", "vence_ahora", "vencida"],
)
def test_notice_at_45_days_and_rotation_at_30(
    remaining: timedelta, notice: bool, rotate: bool
) -> None:
    key = _key(SigningPurpose.GATE, NOW + remaining)
    decision = reminder_decision([key], NOW)
    assert (decision.notices == (key,)) is notice
    assert (decision.rotations == (SigningPurpose.GATE,)) is rotate


def test_due_rotations_start_with_key_set() -> None:
    keys = [_key(purpose, NOW + timedelta(days=1)) for purpose in SigningPurpose]
    decision = reminder_decision(keys, NOW)
    assert decision.rotations[0] is SigningPurpose.KEY_SET
    assert set(decision.rotations) == set(SigningPurpose)


def test_only_the_active_key_counts_for_the_reminder() -> None:
    overlapping = _key(SigningPurpose.CATALOG, NOW + timedelta(days=45), KeyStatus.OVERLAPPING)
    assert reminder_decision([overlapping], NOW).notices == ()
    assert reminder_decision([], NOW).rotations == ()
    with pytest.raises(ValueError, match="period"):
        reminder_decision([], NOW, period=timedelta(0))


def test_days_to_expiry_rounds_down_and_goes_negative() -> None:
    keys = [
        _key(SigningPurpose.CATALOG, NOW + timedelta(days=10, hours=23)),
        _key(SigningPurpose.GATE, NOW - MS),
        _key(SigningPurpose.KEY_SET, NOW + timedelta(days=1), KeyStatus.OVERLAPPING),
    ]
    assert days_to_expiry(keys, NOW) == {SigningPurpose.CATALOG: 10, SigningPurpose.GATE: -1}


def test_overlapping_retires_exactly_at_valid_until() -> None:
    key = _key(SigningPurpose.GATE, NOW, KeyStatus.OVERLAPPING)
    assert expiry_transitions([key], NOW - MS) == ()
    assert [t.status for t in expiry_transitions([key], NOW)] == [KeyStatus.RETIRED]
    assert expiry_transitions([_key(SigningPurpose.GATE, NOW)], NOW) == ()


def test_rotation_caps_the_overlap_at_30_days_and_never_inverts_validity() -> None:
    active = _key(SigningPurpose.GATE, NOW + timedelta(days=300))
    (transition,) = rotation_transitions([active], SigningPurpose.GATE, NOW)
    assert transition.status is KeyStatus.OVERLAPPING
    assert transition.valid_until == NOW + timedelta(days=30)
    # A menos de 30 días del vencimiento conserva su valid_until.
    late = _key(SigningPurpose.GATE, NOW + timedelta(days=10))
    assert rotation_transitions([late], SigningPurpose.GATE, NOW)[0].valid_until == late.valid_until
    # Una overlapping creada en este mismo instante se retira sin dejar valid_until <= valid_from.
    fresh = SigningKeyRecord(
        key_id="gate-fresh",
        purpose=SigningPurpose.GATE,
        public_key=PUBLIC_KEY,
        private_key_ref=_reference(SigningPurpose.GATE, "fresh"),
        valid_from=NOW,
        valid_until=NOW + timedelta(days=30),
        status=KeyStatus.OVERLAPPING,
        created_at=NOW,
        rotated_by=uuid.uuid4(),
    )
    (retired,) = rotation_transitions([fresh], SigningPurpose.GATE, NOW)
    assert retired.status is KeyStatus.RETIRED and retired.valid_until > fresh.valid_from


@pytest.mark.parametrize(
    "changes",
    [
        {"key_id": "Catalog-1"},
        {"key_id": ""},
        {"key_id": "a" * 65},
        {"key_id": "catalog-1\n"},
        {"key_id": "catalog 1"},
        {"public_key": "A" * 44},
        {"public_key": PUBLIC_KEY + "\n"},
        {"private_key_ref": "vigia/pilot signing"},
        {"private_key_ref": "-----BEGIN PRIVATE KEY-----"},
        {"private_key_ref": "x" * 513},
        {"valid_from": NOW.replace(tzinfo=None)},
        {"valid_until": NOW - timedelta(days=365)},
    ],
)
def test_signing_key_record_rejects_malformed_fields(changes: dict[str, Any]) -> None:
    fields: dict[str, Any] = {
        "key_id": "catalog-1",
        "purpose": SigningPurpose.CATALOG,
        "public_key": PUBLIC_KEY,
        "private_key_ref": "vigia/pilot/signing/catalog/catalog-1",
        "valid_from": NOW - timedelta(days=365),
        "valid_until": NOW,
        "status": KeyStatus.ACTIVE,
        "created_at": NOW,
        "rotated_by": uuid.uuid4(),
    }
    fields.update(changes)
    with pytest.raises((ValueError, TypeError)):
        SigningKeyRecord(**fields)


# --- Tarea periódica --------------------------------------------------------------------------


@dataclass
class _Transaction:
    context: ScopeContext


@dataclass
class _Outbox:
    events: list[NewEvent] = field(default_factory=list)

    async def publish(self, transaction: Any, event: NewEvent) -> Any:
        self.events.append(event)


@pytest.mark.asyncio
async def test_reminder_notifies_rotates_retires_and_reports_only_in_the_provider() -> None:
    world = await bootstrapped_world()
    outbox = _Outbox()
    handler = key_rotation_reminder_handler(
        world.service,
        outbox,
        world.clock,
        provider_organization_id=provider_context().organization_id,
    )
    client = make_context(kind=ActorKind.SYSTEM, organization_id=uuid.uuid4())
    world.clock.advance(timedelta(days=320, hours=1).total_seconds())
    await handler(_Transaction(client))  # type: ignore[arg-type]
    assert outbox.events == [] and len(world.events.rotated) == 5

    # 44 días y 23 horas antes del vencimiento: un aviso por propósito, ninguna rotación.
    await handler(_Transaction(provider_context(ActorKind.SYSTEM)))  # type: ignore[arg-type]
    assert sorted(e.payload["key_id"] for e in outbox.events) == sorted(  # type: ignore[index]
        k.key_id for k in world.service.all_keys()
    )
    for event in outbox.events:
        assert event.event_name == "key_rotation_due"
        KeyRotationDue.model_validate_json(json.dumps(dict(event.payload)))  # type: ignore[arg-type]
    assert len(world.events.rotated) == 5

    # Al día siguiente, ni aviso repetido ni rotación.
    world.clock.advance(timedelta(days=1).total_seconds())
    await handler(_Transaction(provider_context(ActorKind.SYSTEM)))  # type: ignore[arg-type]
    assert len(outbox.events) == 5 and len(world.events.rotated) == 5

    # A 30 días rota los cinco, con la key_set primero.
    world.clock.advance(timedelta(days=14).total_seconds())
    await handler(_Transaction(provider_context(ActorKind.SYSTEM)))  # type: ignore[arg-type]
    assert world.events.rotated[5]["purpose"] == "key_set"
    assert len(world.events.rotated) == 10

    # Pasado el solapamiento, las anteriores quedan retiradas.
    world.clock.advance(timedelta(days=30).total_seconds())
    await handler(_Transaction(provider_context(ActorKind.SYSTEM)))  # type: ignore[arg-type]
    statuses = [k.status for k in world.service.all_keys()]
    assert statuses.count(KeyStatus.ACTIVE) == 5 and statuses.count(KeyStatus.RETIRED) == 5


def test_reminder_is_registered_daily_for_u02() -> None:
    registry = PeriodicTaskRegistry()

    async def handler(transaction: Any) -> None:
        return None

    task = register_key_rotation_reminder(registry, handler)
    assert task.task_name == KEY_ROTATION_REMINDER == "key_rotation_reminder"
    assert task.schedule.kind is ScheduleKind.DAILY and task.unit is ActorUnit.U02
    assert registry.get(KEY_ROTATION_REMINDER) is task
    with pytest.raises(OutboxRegistrationRejected):
        register_key_rotation_reminder(registry, handler)


# --- Expediente -------------------------------------------------------------------------------


@dataclass
class _Writer:
    result: Receipt | LedgerRejection
    calls: list[tuple[ScopeContext, str, dict[str, Any], tuple[NewEvent, ...]]] = field(
        default_factory=list
    )

    async def write(
        self,
        context: ScopeContext,
        record_type: str,
        content: dict[str, Any],
        *,
        events: tuple[NewEvent, ...] = (),
    ) -> Receipt | LedgerRejection:
        self.calls.append((context, record_type, content, events))
        return self.result


@pytest.mark.asyncio
async def test_ledger_writer_writes_both_records_with_the_outbox_event() -> None:
    receipt = Receipt(record_id=uuid.uuid4(), received_at=NOW, status=None)  # type: ignore[arg-type]
    writer = _Writer(receipt)
    events = LedgerKeyEventWriter(writer)  # type: ignore[arg-type]
    context = provider_context()
    await events.key_rotated(context, {"key_id": "k"})
    await events.key_set_published(context, {"publication_id": "p"}, {"signing_key_id": "k"})
    (first, second) = writer.calls
    assert first[1] == "key_rotated" and first[3] == ()
    assert second[1] == "key_set_published"
    assert [(e.event_name, dict(e.payload)) for e in second[3]] == [
        ("key_set_published", {"signing_key_id": "k"})
    ]
    assert first[0] is context and second[0] is context


@pytest.mark.asyncio
async def test_ledger_rejection_is_raised_with_its_code() -> None:
    writer = _Writer(LedgerRejection.of(LedgerRejectionCode.CONTENT_INVALID, "/public_key"))
    events = LedgerKeyEventWriter(writer)  # type: ignore[arg-type]
    with pytest.raises(KeyEventRejected) as raised:
        await events.key_rotated(provider_context(), {"key_id": "k"})
    assert raised.value.rejection.code is LedgerRejectionCode.CONTENT_INVALID
    assert "content_invalid" in str(raised.value)
