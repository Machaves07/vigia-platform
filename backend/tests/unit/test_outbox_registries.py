"""Registros de la bandeja de salida (TASK-111, LC-NUC-23 parte 1; BR-NUC-75, domain-entities §4.3).

- ``EventTypeRegistry``: nombre, unidad, descripción y modelo de carga estricto; la carga solo
  admite identificadores, enumeraciones y marcas: texto libre, números con decimales, mapas,
  campos que identifican a una persona y JSON Schema personalizado se rechazan al registrar.
- ``ConsumerRegistry`` y ``OutboxCatalog.check``: un consumidor suscrito a un evento no
  registrado impide arrancar.
- ``PeriodicTaskRegistry`` y ``Schedule``: horarios en UTC, bordes y ``next_after``.
- ``OutboxCatalog.synchronize``: contraste con lo persistido (retirar o estrechar impide
  arrancar), guardado idempotente y sellado.
- Los catorce eventos de U-02 se registran (los trece iniciales y
  ``evidence_marker_verification_failed``, TASK-121).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from typing import Annotated, Any, Literal, NamedTuple

import pytest
from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    WithJsonSchema,
    create_model,
)
from pydantic.alias_generators import to_camel
from pydantic.dataclasses import dataclass as pydantic_dataclass
from typing_extensions import TypedDict
from vigia_contracts.models.common import UUID, Timestamp

from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.outbox.registries import (
    Consumer,
    EventType,
    InMemoryOutboxCatalogStore,
    OutboxCatalog,
    OutboxRegistrationRejected,
    OutboxStartupError,
    PayloadModel,
    PersistedEventType,
    Schedule,
    ScheduleKind,
)
from vigia_platform.shared.outbox.u02_events import U02_EVENT_TYPES, register_u02_event_types

U02_EVENTS = (
    "user_invited",
    "user_activated",
    "user_deactivated",
    "role_assignment_changed",
    "concession_granted",
    "concession_revoked",
    "concession_expired",
    "security_alert",
    "integrity_compromised",
    "checkpoint_written",
    "key_set_published",
    "key_rotation_due",
    "dead_letter_created",
    "evidence_marker_verification_failed",
)

Closed = Annotated[StrictStr, Field(min_length=1, max_length=64, pattern=r"^[a-z_]{1,64}$")]


class Payload(PayloadModel):
    zone_id: UUID
    state: Literal["observable", "degraded"]
    observed_at: Timestamp
    count: Annotated[StrictInt, Field(ge=0, le=100)]
    code: Closed


async def _noop(*_: Any) -> None:
    return None


def _event(name: str = "zone_probe", model: type[BaseModel] = Payload, **changes: Any) -> EventType:
    fields: dict[str, Any] = {
        "event_name": name,
        "publisher_unit": ActorUnit.U02,
        "payload_model": model,
        "description_es": "Evento de prueba",
    }
    fields.update(changes)
    return EventType(**fields)


def _consumer(name: str = "probe_consumer", events: tuple[str, ...] = ("zone_probe",)) -> Consumer:
    return Consumer(consumer_name=name, unit=ActorUnit.U02, subscribed_events=events, handler=_noop)


def _rejected(catalog: OutboxCatalog, event: EventType) -> str:
    with pytest.raises(OutboxRegistrationRejected) as caught:
        catalog.event_types.register(event)
    return str(caught.value)


# --- EventType ------------------------------------------------------------------------------


def test_u02_registers_its_fourteen_events() -> None:
    catalog = OutboxCatalog()
    register_u02_event_types(catalog.event_types)
    assert set(catalog.event_types.event_names()) == set(U02_EVENTS)
    assert len(U02_EVENT_TYPES) == 14
    assert all(t.publisher_unit is ActorUnit.U02 for t in U02_EVENT_TYPES)


@pytest.mark.parametrize(
    "resource_kind",
    [
        "juan_perez",  # la sonda del revisor de VIG-47: un snake_case cualquiera
        "maria",
        "users",
        "User",
        "user ",
        "usеr",  # noqa: RUF001 - homoglifo a propósito (e cirílica U+0435)
        "user​",
        "",
        "persona_observada",
    ],
)
def test_security_alert_resource_kind_is_a_closed_list(resource_kind: str) -> None:
    """P3 (seguimiento de VIG-47): el recurso de ``security_alert`` no admite códigos libres."""
    catalog = OutboxCatalog()
    register_u02_event_types(catalog.event_types)
    compiled = catalog.event_types.get("security_alert")
    assert compiled is not None
    payload = {
        "alert_kind": "login_failures_account",
        "resource_kind": "user",
        "resource_id": "0192f0c4-3b8a-7c3e-9d2b-5f6a7b8c9d0e",
        "occurred_at": "2026-09-30T10:00:00.000Z",
    }
    compiled.payload_model.model_validate(payload)
    with pytest.raises(ValueError):
        compiled.payload_model.model_validate({**payload, "resource_kind": resource_kind})
    schema = compiled.payload_model.model_json_schema()["properties"]["resource_kind"]
    enums = [option.get("enum") for option in schema["anyOf"] if option.get("type") != "null"]
    assert enums == [
        [
            "user",
            "organization",
            "plant",
            "zone",
            "node",
            "evidence",
            "ledger_record",
            "concession",
        ]
    ]


def test_a_valid_payload_model_registers_with_its_schema() -> None:
    compiled = OutboxCatalog().event_types.register(_event())
    assert compiled.payload_schema["additionalProperties"] is False
    assert set(compiled.payload_schema["required"]) == {
        "zone_id",
        "state",
        "observed_at",
        "count",
        "code",
    }


class FreeText(PayloadModel):
    zone_id: UUID
    comment: Annotated[StrictStr, Field(max_length=200)]


class OpenPattern(PayloadModel):
    label: Annotated[StrictStr, Field(max_length=64, pattern=r"^[A-Za-z ]{1,64}$")]


class UnanchoredPattern(PayloadModel):
    label: Annotated[StrictStr, Field(max_length=64, pattern=r"[a-z]+")]


class FormatOnly(PayloadModel):
    contact: Annotated[StrictStr, Field(max_length=64, json_schema_extra={"format": "uuid"})]


class NestedFreeText(PayloadModel):
    class Inner(PayloadModel):
        note: Annotated[StrictStr, Field(max_length=10)]

    items: Annotated[list[Inner], Field(max_length=3)]


class OptionalFreeText(PayloadModel):
    detail: Annotated[StrictStr, Field(max_length=10)] | None = None


@pytest.mark.parametrize(
    "model",
    [FreeText, OpenPattern, UnanchoredPattern, NestedFreeText, OptionalFreeText],
    ids=["plain", "mixed-case-with-space", "unanchored", "nested-list", "optional"],
)
def test_free_text_in_the_payload_is_rejected_at_registration(model: type[BaseModel]) -> None:
    message = _rejected(OutboxCatalog(), _event(model=model))
    assert "texto libre en la carga" in message


def test_format_does_not_close_a_string() -> None:
    message = _rejected(OutboxCatalog(), _event(model=FormatOnly))
    assert "texto libre en la carga" in message or "JSON Schema personalizado" in message


class PersonField(PayloadModel):
    worker_id: UUID


class CamelPerson(PayloadModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, alias_generator=None)
    personId: UUID


class Decimal(PayloadModel):
    ratio: Annotated[StrictFloat, Field(ge=0, le=1)]


class OpenMap(PayloadModel):
    model_config = ConfigDict(extra="allow", strict=True, frozen=True)
    zone_id: UUID


class Mapping_(PayloadModel):
    values: Annotated[dict[str, int], Field(max_length=3)]


class Unbounded(PayloadModel):
    count: StrictInt


class LaxModel(BaseModel):
    zone_id: UUID


class CustomSchema(PayloadModel):
    code: Annotated[StrictStr, WithJsonSchema({"type": "string", "enum": ["a"]})]


class Binary(PayloadModel):
    blob: Annotated[bytes, Field(max_length=10)]


@pytest.mark.parametrize(
    ("model", "reason"),
    [
        (PersonField, "BR-NUC-51"),
        (CamelPerson, "BR-NUC-51"),
        (Decimal, "decimales"),
        (OpenMap, "estricto"),
        (Mapping_, "propiedades adicionales"),
        (Unbounded, "número sin"),
        (LaxModel, "estricto"),
        (CustomSchema, "JSON Schema personalizado"),
        (Binary, "binario"),
    ],
    ids=lambda value: value if isinstance(value, str) else value.__name__,
)
def test_payload_models_outside_the_rules_are_rejected(model: type[BaseModel], reason: str) -> None:
    assert reason in _rejected(OutboxCatalog(), _event(model=model))


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"event_name": "ZoneProbe"}, "snake_case"),
        ({"event_name": ""}, "snake_case"),
        ({"event_name": "a" * 65}, "snake_case"),
        ({"event_name": "zone_probe\n"}, "snake_case"),
        ({"publisher_unit": "U-02"}, "unidad"),
        ({"description_es": ""}, "description_es"),
        ({"description_es": "   "}, "description_es"),
        ({"description_es": "x" * 501}, "description_es"),
        ({"payload_model": dict}, "payload_model"),
    ],
)
def test_malformed_event_declarations_are_rejected(changes: dict[str, Any], reason: str) -> None:
    assert reason in _rejected(OutboxCatalog(), _event(**changes))


def test_name_limits_are_inclusive() -> None:
    catalog = OutboxCatalog()
    catalog.event_types.register(_event("a" * 64, description_es="x" * 500))
    catalog.event_types.register(_event("b", description_es="x"))
    assert catalog.event_types.event_names() == ("a" * 64, "b")


def test_duplicate_event_and_registration_after_sealing_are_rejected() -> None:
    catalog = OutboxCatalog()
    catalog.event_types.register(_event())
    assert "ya está registrado" in _rejected(catalog, _event())
    catalog.seal()
    assert "sellado" in _rejected(catalog, _event("other_probe"))


def test_consumers_and_periodic_tasks_cannot_register_after_sealing() -> None:
    """Seguimiento 2 de VIG-47: sin la guarda del sello, esta prueba falla."""
    catalog = OutboxCatalog()
    catalog.event_types.register(_event())
    catalog.consumers.register(_consumer())
    catalog.periodic_tasks.register("probe_task", Schedule.every(60), _noop, unit=ActorUnit.U02)
    catalog.seal()
    assert catalog.consumers.sealed and catalog.periodic_tasks.sealed
    with pytest.raises(OutboxRegistrationRejected, match="sellado"):
        catalog.consumers.register(_consumer("late_consumer"))
    with pytest.raises(OutboxRegistrationRejected, match="sellado"):
        catalog.periodic_tasks.register("late_task", Schedule.every(60), _noop, unit=ActorUnit.U02)
    assert [c.consumer_name for c in catalog.consumers.consumers()] == ["probe_consumer"]
    assert [t.task_name for t in catalog.periodic_tasks.tasks()] == ["probe_task"]


# --- Alias en la carga (seguimiento de VIG-129, P3) -----------------------------------------

Zone = Annotated[UUID, Field(alias="zona")]


class _AliasedTyped(TypedDict):
    zone_id: Zone


class _AliasedTuple(NamedTuple):
    zone_id: Zone


@pydantic_dataclass(config=ConfigDict(extra="forbid", strict=True))
class _AliasedPart:
    zone_id: Zone


class _Inner(PayloadModel):
    zone_id: Zone


class _Generated(PayloadModel):
    model_config = PayloadModel.model_config | {"alias_generator": to_camel}
    zone_id: UUID


def _payload_with(annotation: Any) -> type[BaseModel]:
    return create_model("AliasProbe", __base__=PayloadModel, part=(annotation, ...))


_ALIASED_PAYLOADS: dict[str, tuple[type[BaseModel], str]] = {
    "alias": (_payload_with(Zone), "/part"),
    "validation_alias": (
        _payload_with(Annotated[UUID, Field(validation_alias="zona")]),
        "/part",
    ),
    "alias_choices": (
        _payload_with(Annotated[UUID, Field(validation_alias=AliasChoices("part", "zona"))]),
        "/part",
    ),
    "alias_path": (
        _payload_with(Annotated[UUID, Field(validation_alias=AliasPath("zona", 0))]),
        "/part",
    ),
    "serialization_alias": (
        _payload_with(Annotated[UUID, Field(serialization_alias="zona")]),
        "/part",
    ),
    "alias_generator": (_Generated, "/zone_id"),
    "nested_model": (_payload_with(_Inner), "/part/zone_id"),
    "typed_dict": (_payload_with(_AliasedTyped), "/part/zone_id"),
    "named_tuple": (_payload_with(_AliasedTuple), "/part/zone_id"),
    "dataclass": (_payload_with(_AliasedPart), "/part/zone_id"),
}


@pytest.mark.parametrize("how", sorted(_ALIASED_PAYLOADS))
def test_payload_fields_with_aliases_are_rejected_at_registration(how: str) -> None:
    """Con alias, la carga se validaría con claves que el esquema persistido no declara."""
    model, path = _ALIASED_PAYLOADS[how]
    with pytest.raises(OutboxRegistrationRejected) as caught:
        OutboxCatalog().event_types.register(_event(model=model))
    assert any(
        problem.startswith(f"{path}: el campo declara un alias")
        for problem in caught.value.problems
    ), caught.value.problems


# --- Consumer y arranque ------------------------------------------------------------------


def test_consumer_subscribed_to_an_unregistered_event_blocks_startup() -> None:
    catalog = OutboxCatalog()
    catalog.event_types.register(_event())
    catalog.consumers.register(_consumer(events=("zone_probe", "zone_unknown")))
    with pytest.raises(OutboxStartupError, match="zone_unknown"):
        catalog.check()
    with pytest.raises(OutboxStartupError):
        asyncio.run(catalog.synchronize(InMemoryOutboxCatalogStore(), _clock()))
    assert not catalog.sealed


def test_subscribers_are_resolved_per_event_in_order() -> None:
    catalog = OutboxCatalog()
    catalog.event_types.register(_event())
    catalog.event_types.register(_event("other_probe"))
    catalog.consumers.register(_consumer("b_consumer", ("zone_probe",)))
    catalog.consumers.register(_consumer("a_consumer", ("zone_probe", "other_probe")))
    catalog.check()
    assert catalog.consumers.subscribers("zone_probe") == ("a_consumer", "b_consumer")
    assert catalog.consumers.subscribers("other_probe") == ("a_consumer",)
    assert catalog.consumers.subscribers("nobody") == ()


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"consumer_name": "Bad-Name"}, "snake_case"),
        ({"subscribed_events": ()}, "al menos un evento"),
        ({"subscribed_events": ["zone_probe"]}, "tupla"),
        ({"subscribed_events": ("zone_probe", "zone_probe")}, "repetidos"),
        ({"subscribed_events": ("Zone",)}, "mal formado"),
        ({"subscribed_events": tuple(f"e{i}" for i in range(65))}, "como mucho"),
        ({"handler": "not callable"}, "invocable"),
        ({"has_external_dependency": 1}, "bool"),
        ({"unit": "U-09"}, "unidad"),
    ],
)
def test_malformed_consumers_are_rejected(changes: dict[str, Any], reason: str) -> None:
    fields: dict[str, Any] = {
        "consumer_name": "probe_consumer",
        "unit": ActorUnit.U02,
        "subscribed_events": ("zone_probe",),
        "handler": _noop,
    }
    fields.update(changes)
    with pytest.raises(OutboxRegistrationRejected, match=reason):
        OutboxCatalog().consumers.register(Consumer(**fields))


def test_consumer_with_external_dependency_is_declared() -> None:
    consumer = Consumer(
        consumer_name="mailer",
        unit=ActorUnit.U04,
        subscribed_events=("zone_probe",),
        handler=_noop,
        has_external_dependency=True,
    )
    assert OutboxCatalog().consumers.register(consumer).to_persisted().has_external_dependency


# --- PeriodicTask y Schedule --------------------------------------------------------------


def _clock(at: datetime = datetime(2026, 9, 29, 10, 30, tzinfo=UTC)) -> SimulatedClock:
    return SimulatedClock(at)


def test_periodic_task_registration() -> None:
    catalog = OutboxCatalog()
    task = catalog.periodic_tasks.register(
        "expire_sessions", Schedule.every(300), _noop, unit=ActorUnit.U02
    )
    assert task.to_persisted().schedule == "every:300s"
    with pytest.raises(OutboxRegistrationRejected, match="ya está registrada"):
        catalog.periodic_tasks.register(
            "expire_sessions", Schedule.every(300), _noop, unit=ActorUnit.U02
        )
    for name, schedule, handler, reason in (
        ("Bad", Schedule.every(1), _noop, "snake_case"),
        ("ok_task", "every 5 min", _noop, "Schedule"),
        ("ok_task", Schedule.every(1), None, "invocable"),
    ):
        with pytest.raises(OutboxRegistrationRejected, match=reason):
            catalog.periodic_tasks.register(name, schedule, handler, unit=ActorUnit.U02)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("schedule", "now", "expected"),
    [
        (Schedule.every(300), "2026-09-29T10:30:00", "2026-09-29T10:35:00"),
        (Schedule.every(300), "2026-09-29T10:34:59.999999", "2026-09-29T10:35:00"),
        (Schedule.every(1), "2026-09-29T10:30:00.5", "2026-09-29T10:30:01"),
        (Schedule.daily(), "2026-09-29T00:00:00", "2026-09-30T00:00:00"),
        (Schedule.daily(), "2026-09-28T23:59:59", "2026-09-29T00:00:00"),
        (Schedule.daily(hour=23, minute=59), "2026-09-29T10:00:00", "2026-09-29T23:59:00"),
        (Schedule.weekly(), "2026-09-29T10:00:00", "2026-10-05T00:00:00"),
        (Schedule.weekly(weekday=6, hour=1), "2026-10-04T00:59:00", "2026-10-04T01:00:00"),
        (Schedule.monthly(), "2026-09-29T10:00:00", "2026-10-01T00:00:00"),
        (Schedule.monthly(), "2026-12-15T00:00:00", "2027-01-01T00:00:00"),
        (Schedule.monthly(day=28, hour=23), "2027-02-28T22:00:00", "2027-02-28T23:00:00"),
        (Schedule.monthly(day=28, hour=23), "2027-02-28T23:00:00", "2027-03-28T23:00:00"),
    ],
)
def test_next_after_is_strictly_later_and_on_the_schedule(
    schedule: Schedule, now: str, expected: str
) -> None:
    instant = datetime.fromisoformat(now).replace(tzinfo=UTC)
    assert schedule.next_after(instant) == datetime.fromisoformat(expected).replace(tzinfo=UTC)


def test_next_after_accepts_other_offsets_and_rejects_naive() -> None:
    bogota = timezone(timedelta(hours=-5))
    # 18:00 en Bogotá son las 23:00 UTC; 19:00 son ya las 00:00 UTC del día siguiente.
    assert Schedule.daily().next_after(datetime(2026, 9, 29, 18, 0, tzinfo=bogota)) == datetime(
        2026, 9, 30, tzinfo=UTC
    )
    assert Schedule.daily().next_after(datetime(2026, 9, 29, 19, 0, tzinfo=bogota)) == datetime(
        2026, 10, 1, tzinfo=UTC
    )
    with pytest.raises(ValueError, match="zona"):
        Schedule.daily().next_after(datetime(2026, 9, 29))  # noqa: DTZ001


@pytest.mark.parametrize(
    "build",
    [
        lambda: Schedule.every(0),
        lambda: Schedule.every(31 * 24 * 3600 + 1),
        lambda: Schedule.every(True),
        lambda: Schedule.daily(hour=24),
        lambda: Schedule.daily(minute=60),
        lambda: Schedule.daily(hour=-1),
        lambda: Schedule.weekly(weekday=7),
        lambda: Schedule.monthly(day=29),
        lambda: Schedule.monthly(day=0),
        lambda: Schedule(ScheduleKind.DAILY, 24 * 3600),
        lambda: Schedule("daily", 0),  # type: ignore[arg-type]
    ],
)
def test_schedule_bounds(build: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        build()


def test_schedule_limits_are_inclusive() -> None:
    assert Schedule.every(1).offset_seconds == 1
    assert Schedule.every(31 * 24 * 3600).text == "every:2678400s"
    assert Schedule.daily(hour=23, minute=59).text == "daily:86340s"
    assert Schedule.weekly(weekday=6, hour=23, minute=59).offset_seconds == 7 * 86400 - 60
    assert Schedule.monthly(day=28, hour=23, minute=59).offset_seconds == 28 * 86400 - 60


# --- synchronize --------------------------------------------------------------------------


def _catalog(model: type[BaseModel] = Payload) -> OutboxCatalog:
    catalog = OutboxCatalog()
    catalog.event_types.register(_event(model=model))
    catalog.consumers.register(_consumer())
    catalog.periodic_tasks.register("probe_task", Schedule.daily(), _noop, unit=ActorUnit.U02)
    return catalog


def test_synchronize_saves_and_seals_and_is_idempotent() -> None:
    store = InMemoryOutboxCatalogStore()
    catalog = _catalog()
    asyncio.run(catalog.synchronize(store, _clock()))
    assert catalog.sealed
    assert set(store.event_types) == {"zone_probe"}
    assert store.consumers["probe_consumer"].subscribed_events == ("zone_probe",)
    row, next_run_at = store.periodic_tasks["probe_task"]
    assert (row.schedule, next_run_at) == ("daily:0s", datetime(2026, 9, 30, tzinfo=UTC))

    saved: list[str] = []
    original = store.save_event_type

    async def spy(row: PersistedEventType) -> None:
        saved.append(row.event_name)
        await original(row)

    store.save_event_type = spy  # type: ignore[method-assign]
    asyncio.run(_catalog().synchronize(store, _clock(datetime(2026, 10, 1, tzinfo=UTC))))
    assert saved == []
    assert store.periodic_tasks["probe_task"][1] == datetime(2026, 9, 30, tzinfo=UTC)


class Widened(PayloadModel):
    zone_id: UUID
    state: Literal["observable", "degraded", "unobservable"]
    observed_at: Timestamp
    count: Annotated[StrictInt, Field(ge=0, le=200)]
    code: Closed
    extra_id: UUID | None = None


class Narrowed(PayloadModel):
    zone_id: UUID
    state: Literal["observable"]
    observed_at: Timestamp
    count: Annotated[StrictInt, Field(ge=0, le=100)]
    code: Closed


class NewRequired(Payload):
    extra_id: UUID


def test_payload_can_only_widen_between_deployments() -> None:
    store = InMemoryOutboxCatalogStore()
    asyncio.run(_catalog().synchronize(store, _clock()))
    asyncio.run(_catalog(Widened).synchronize(store, _clock()))
    assert "unobservable" in str(store.event_types["zone_probe"].payload_schema)
    for model in (Narrowed, NewRequired, Payload):
        catalog = _catalog(model)
        with pytest.raises(OutboxStartupError, match="la carga solo puede ampliarse"):
            asyncio.run(catalog.synchronize(store, _clock()))
        assert not catalog.sealed


def test_retiring_or_moving_what_is_persisted_blocks_startup() -> None:
    store = InMemoryOutboxCatalogStore()
    asyncio.run(_catalog().synchronize(store, _clock()))

    empty = OutboxCatalog()
    with pytest.raises(OutboxStartupError) as caught:
        asyncio.run(empty.synchronize(store, _clock()))
    text = str(caught.value)
    assert "zone_probe" in text and "probe_consumer" in text and "probe_task" in text

    moved = OutboxCatalog()
    moved.event_types.register(_event(publisher_unit=ActorUnit.U03))
    moved.consumers.register(
        Consumer(
            consumer_name="probe_consumer",
            unit=ActorUnit.U04,
            subscribed_events=("zone_probe",),
            handler=_noop,
        )
    )
    moved.periodic_tasks.register("probe_task", Schedule.daily(), _noop, unit=ActorUnit.U03)
    with pytest.raises(OutboxStartupError) as caught:
        asyncio.run(moved.synchronize(store, _clock()))
    assert str(caught.value).count("cambió") == 3


def test_schedule_change_updates_next_run() -> None:
    store = InMemoryOutboxCatalogStore()
    asyncio.run(_catalog().synchronize(store, _clock()))
    changed = OutboxCatalog()
    changed.event_types.register(_event())
    changed.consumers.register(_consumer())
    changed.periodic_tasks.register("probe_task", Schedule.every(60), _noop, unit=ActorUnit.U02)
    asyncio.run(changed.synchronize(store, _clock()))
    row, next_run_at = store.periodic_tasks["probe_task"]
    assert row.schedule == "every:60s"
    assert next_run_at == datetime(2026, 9, 29, 10, 30, tzinfo=UTC) + timedelta(seconds=60)
