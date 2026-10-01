"""Registros de la bandeja de salida: ``EventType``, ``Consumer`` y ``PeriodicTask`` (LC-NUC-23).

Cada unidad (U-02, U-03, U-04) declara al arrancar sus tipos de evento, sus consumidores y sus
tareas periódicas (business-logic-model §8, domain-entities §4.3). Aquí se decide qué puede
publicarse y a quién se entrega:

- ``EventTypeRegistry.register(EventType)``: el nombre ``snake_case`` ≤ 64, la unidad emisora, la
  descripción en español y el **modelo de carga**, un modelo Pydantic 2 estricto (``PayloadModel``:
  ``extra="forbid"``, ``strict=True``) cuyo JSON Schema es el ``payload_schema`` que se persiste.
  La carga solo lleva identificadores, enumeraciones y marcas (BR-NUC-75): se exigen las reglas de
  estructura del expediente (``ledger.schema_rules``), la metapropiedad de privacidad **sin
  ninguna ruta de texto libre** (una cadena sin lista cerrada ni patrón cerrado se rechaza),
  ningún número con decimales y ningún campo con alias (``ledger.registry.alias_problems``). Un
  tipo que no cumple lanza ``OutboxRegistrationRejected``.
- ``ConsumerRegistry.register(Consumer)``: nombre, unidad, eventos suscritos, manejador y
  ``has_external_dependency`` (el que declara cortacircuito, BR-NUC-79).
- ``PeriodicTaskRegistry.register(task_name, schedule, handler, unit=...)``: el horario es un
  ``Schedule`` (cada N segundos, diario, semanal o mensual, en UTC); ejecutarla es de TASK-130.

``OutboxCatalog`` agrupa los tres. ``check()`` contrasta las suscripciones: un consumidor suscrito
a un evento no registrado impide arrancar (``OutboxStartupError``). ``synchronize(store, clock)``
lo contrasta además con las tablas globales ``shared.event_type``, ``shared.consumer`` y
``shared.periodic_task`` (retirar lo ya persistido está prohibido, P4; una carga solo puede
ampliarse, como un tipo de registro), guarda lo nuevo y **sella** el catálogo. Sin sellar no se
publica; después de sellar no se registra nada más.
"""

from __future__ import annotations

import enum
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Protocol

from pydantic import BaseModel, ConfigDict

from vigia_platform.ledger.registry import alias_problems, custom_json_schema_problems
from vigia_platform.ledger.schema_rules import (
    SchemaProblem,
    compatibility_problems,
    field_nodes,
    normalized_schema,
    privacy_problems,
    structure_problems,
)
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit

if TYPE_CHECKING:
    from vigia_platform.shared.db import Transaction
    from vigia_platform.shared.outbox.publish import OutboxEvent

__all__ = [
    "CompiledEventType",
    "Consumer",
    "ConsumerHandler",
    "ConsumerRegistry",
    "EventType",
    "EventTypeRegistry",
    "InMemoryOutboxCatalogStore",
    "OutboxCatalog",
    "OutboxCatalogStore",
    "OutboxRegistrationRejected",
    "OutboxStartupError",
    "PayloadModel",
    "PeriodicHandler",
    "PeriodicTask",
    "PeriodicTaskRegistry",
    "PersistedConsumer",
    "PersistedEventType",
    "PersistedPeriodicTask",
    "Schedule",
    "ScheduleKind",
]

REGISTRY_NAME: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
"""Nombre de evento, consumidor y tarea: ``snake_case``, hasta 64 caracteres (tablas globales).
Siempre con ``fullmatch``: con ``^…$`` y ``match``, ``$`` admitiría un salto de línea final."""

DESCRIPTION_MAX_CHARS: Final = 500
"""Longitud máxima de ``description_es`` (``event_type_description_length``)."""

MAX_SUBSCRIPTIONS: Final = 64
"""Tope de eventos suscritos por consumidor."""

SCHEDULE_TEXT_MAX_CHARS: Final = 64
"""Longitud máxima del horario persistido (``periodic_task_schedule_length``)."""

MIN_INTERVAL_SECONDS: Final = 1
MAX_INTERVAL_SECONDS: Final = 31 * 24 * 3600


type ConsumerHandler = Callable[[OutboxEvent, Transaction], Awaitable[None]]
"""Manejador de un consumidor: recibe el evento y la transacción abierta con el contexto de la
organización del evento (BR-NUC-80); el despachador (TASK-129) lo invoca y marca la entrega."""

type PeriodicHandler = Callable[[Transaction], Awaitable[None]]
"""Manejador de una tarea periódica: una invocación por organización, cada una en su transacción
con su contexto (BR-NUC-81); el planificador es de TASK-130."""


class PayloadModel(BaseModel):
    """Base de los modelos de carga: estricto, inmutable, sin extras ni ``NaN``."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


class OutboxRegistrationRejected(Exception):
    """Una declaración que no puede registrarse; nombra el elemento y cada motivo."""

    def __init__(self, kind: str, name: object, problems: list[SchemaProblem | str]) -> None:
        self.kind = kind
        self.name = name if isinstance(name, str) else repr(name)
        self.problems = tuple(str(problem) for problem in problems)
        detail = "\n".join(f"  - {problem}" for problem in self.problems)
        super().__init__(
            f"No se puede registrar {kind} «{self.name}»; la plataforma no arranca:\n{detail}"
        )


class OutboxStartupError(Exception):
    """Lo registrado es incoherente, o incompatible con lo persistido: no se arranca."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = tuple(problems)
        detail = "\n".join(f"  - {problem}" for problem in self.problems)
        super().__init__(
            f"El catálogo de la bandeja de salida no es válido; la plataforma no arranca:\n{detail}"
        )


def _name_problems(label: str, name: object) -> list[SchemaProblem | str]:
    if not isinstance(name, str) or not REGISTRY_NAME.fullmatch(name):
        return [f"{label} debe ser snake_case de 1 a 64 caracteres"]
    return []


def _unit_problems(unit: object) -> list[SchemaProblem | str]:
    return [] if isinstance(unit, ActorUnit) else ["la unidad debe ser U-02, U-03 o U-04"]


def _sealed_problem(kind: str) -> list[SchemaProblem | str]:
    return [f"el registro de {kind} está sellado: se registra al arrancar"]


# --- EventType ------------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class EventType:
    """Declaración de un tipo de evento (domain-entities §4.3)."""

    event_name: str
    publisher_unit: ActorUnit
    payload_model: type[BaseModel]
    description_es: str


@dataclass(frozen=True)
class CompiledEventType:
    """Un tipo de evento registrado con su esquema de carga derivado."""

    definition: EventType
    payload_schema: Mapping[str, Any]

    @property
    def event_name(self) -> str:
        return self.definition.event_name

    @property
    def payload_model(self) -> type[BaseModel]:
        return self.definition.payload_model

    def to_persisted(self) -> PersistedEventType:
        return PersistedEventType(
            event_name=self.definition.event_name,
            publisher_unit=self.definition.publisher_unit.value,
            payload_schema=self.payload_schema,
            description_es=self.definition.description_es,
        )


@dataclass(frozen=True, kw_only=True)
class PersistedEventType:
    """Una fila de ``shared.event_type``."""

    event_name: str
    publisher_unit: str
    payload_schema: Mapping[str, Any]
    description_es: str


def _json_copy(document: Mapping[str, Any]) -> dict[str, Any]:
    copied: dict[str, Any] = json.loads(json.dumps(document, allow_nan=False))
    return copied


def _number_problems(schema: Mapping[str, Any]) -> list[SchemaProblem]:
    """La carga no lleva números con decimales: ni identificador, ni enumeración, ni marca."""
    nodes, _ = field_nodes(schema)
    return [
        SchemaProblem(node.path, "número con decimales: la carga solo admite enteros acotados")
        for node in nodes
        if node.schema.get("type") == "number"
    ]


def _event_type_problems(definition: EventType) -> list[SchemaProblem | str]:
    problems = _name_problems("el nombre del evento", definition.event_name)
    problems += _unit_problems(definition.publisher_unit)
    description: object = definition.description_es
    if (
        not isinstance(description, str)
        or not 1 <= len(description) <= DESCRIPTION_MAX_CHARS
        or not description.strip()
    ):
        problems.append(f"description_es debe tener de 1 a {DESCRIPTION_MAX_CHARS} caracteres")
    model: object = definition.payload_model
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        problems.append("payload_model debe ser un modelo Pydantic")
        return problems
    config = model.model_config
    if config.get("extra") != "forbid" or config.get("strict") is not True:
        problems.append(
            "payload_model debe ser estricto: extra='forbid' y strict=True (PAT-NUC-SEG-07)"
        )
    problems.extend(custom_json_schema_problems(model))
    # Con alias la carga se validaría con claves que el esquema persistido no declara: un
    # «nota» de texto libre entraría por una clave que la metapropiedad no ve (P3).
    problems.extend(alias_problems(model))
    try:
        schema = _payload_schema(model)
    except Exception as error:  # un tipo sin JSON Schema, o con valores no representables
        problems.append(f"payload_model sin JSON Schema válido: {type(error).__name__}")
        return problems
    problems.extend(structure_problems(schema))
    # Sin rutas de texto libre: toda cadena de la carga es cerrada (BR-NUC-75, P3).
    problems.extend(
        SchemaProblem(problem.path, _FREE_TEXT_IN_PAYLOAD)
        if problem.message.startswith("texto libre")
        else problem
        for problem in privacy_problems(schema, ())
    )
    problems.extend(_number_problems(schema))
    return problems


_FREE_TEXT_IN_PAYLOAD: Final = (
    "texto libre en la carga: solo se admiten identificadores, enumeraciones y marcas (BR-NUC-75)"
)


def _payload_schema(model: type[BaseModel]) -> dict[str, Any]:
    return _json_copy(model.model_json_schema(mode="validation"))


class EventTypeRegistry:
    """El registro cerrado de tipos de evento de un proceso."""

    def __init__(self) -> None:
        self._types: dict[str, CompiledEventType] = {}
        self._sealed = False

    def register(self, definition: EventType) -> CompiledEventType:
        """Compila y registra un tipo; ``OutboxRegistrationRejected`` si no cumple."""
        name: object = getattr(definition, "event_name", "?")
        if self._sealed:
            raise OutboxRegistrationRejected("el tipo de evento", name, _sealed_problem("eventos"))
        if not isinstance(definition, EventType):
            raise OutboxRegistrationRejected(
                "el tipo de evento", name, ["la declaración debe ser un EventType"]
            )
        problems = _event_type_problems(definition)
        if definition.event_name in self._types:
            problems.append("el evento ya está registrado")
        if problems:
            raise OutboxRegistrationRejected("el tipo de evento", name, problems)
        compiled = CompiledEventType(definition, _payload_schema(definition.payload_model))
        self._types[definition.event_name] = compiled
        return compiled

    def get(self, event_name: str) -> CompiledEventType | None:
        return self._types.get(event_name)

    def event_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._types))

    def compiled_types(self) -> tuple[CompiledEventType, ...]:
        return tuple(self._types[name] for name in self.event_names())

    def seal(self) -> None:
        self._sealed = True

    @property
    def sealed(self) -> bool:
        return self._sealed


# --- Consumer -------------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class Consumer:
    """Declaración de un consumidor (domain-entities §4.3); el circuito lo lleva la tabla."""

    consumer_name: str
    unit: ActorUnit
    subscribed_events: tuple[str, ...]
    handler: ConsumerHandler
    has_external_dependency: bool = False

    def to_persisted(self) -> PersistedConsumer:
        return PersistedConsumer(
            consumer_name=self.consumer_name,
            unit=self.unit.value,
            subscribed_events=tuple(sorted(self.subscribed_events)),
            has_external_dependency=self.has_external_dependency,
        )


@dataclass(frozen=True, kw_only=True)
class PersistedConsumer:
    """Las columnas declaradas de una fila de ``shared.consumer`` (sin el estado del circuito)."""

    consumer_name: str
    unit: str
    subscribed_events: tuple[str, ...]
    has_external_dependency: bool


def _consumer_problems(consumer: Consumer) -> list[SchemaProblem | str]:
    problems = _name_problems("el nombre del consumidor", consumer.consumer_name)
    problems += _unit_problems(consumer.unit)
    events: object = consumer.subscribed_events
    if not isinstance(events, tuple) or not events:
        problems.append("subscribed_events debe ser una tupla con al menos un evento")
    else:
        if len(events) > MAX_SUBSCRIPTIONS:
            problems.append(f"subscribed_events admite como mucho {MAX_SUBSCRIPTIONS} eventos")
        if len(set(events)) != len(events):
            problems.append("subscribed_events tiene eventos repetidos")
        for event in events:
            if not isinstance(event, str) or not REGISTRY_NAME.fullmatch(event):
                problems.append(f"nombre de evento suscrito mal formado: {event!r}")
    # Las unidades declaran en Python sin validación de tipos: se comprueba en ejecución.
    handler: object = consumer.handler
    external: object = consumer.has_external_dependency
    if not callable(handler):
        problems.append("handler debe ser invocable")
    if not isinstance(external, bool):
        problems.append("has_external_dependency debe ser bool")
    return problems


class ConsumerRegistry:
    """Los consumidores de un proceso y, por evento, a quién se entrega."""

    def __init__(self) -> None:
        self._consumers: dict[str, Consumer] = {}
        self._sealed = False

    def register(self, consumer: Consumer) -> Consumer:
        name: object = getattr(consumer, "consumer_name", "?")
        if self._sealed:
            raise OutboxRegistrationRejected("el consumidor", name, _sealed_problem("consumidores"))
        if not isinstance(consumer, Consumer):
            raise OutboxRegistrationRejected(
                "el consumidor", name, ["la declaración debe ser un Consumer"]
            )
        problems = _consumer_problems(consumer)
        if consumer.consumer_name in self._consumers:
            problems.append("el consumidor ya está registrado")
        if problems:
            raise OutboxRegistrationRejected("el consumidor", name, problems)
        self._consumers[consumer.consumer_name] = consumer
        return consumer

    def get(self, consumer_name: str) -> Consumer | None:
        return self._consumers.get(consumer_name)

    def consumers(self) -> tuple[Consumer, ...]:
        return tuple(self._consumers[name] for name in sorted(self._consumers))

    def subscribers(self, event_name: str) -> tuple[str, ...]:
        """Nombres de los consumidores suscritos a ``event_name``, en orden."""
        return tuple(
            name
            for name in sorted(self._consumers)
            if event_name in self._consumers[name].subscribed_events
        )

    def seal(self) -> None:
        self._sealed = True

    @property
    def sealed(self) -> bool:
        return self._sealed


# --- PeriodicTask ---------------------------------------------------------------------------


class ScheduleKind(enum.StrEnum):
    EVERY = "every"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


_DAY: Final = 24 * 3600
_WEEK: Final = 7 * _DAY
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_MONDAY_EPOCH: Final = datetime(1970, 1, 5, tzinfo=UTC)
"""El primer lunes de la época: origen de los horarios semanales."""


@dataclass(frozen=True)
class Schedule:
    """Horario de una tarea periódica, en UTC.

    ``offset_seconds`` es el intervalo (``every``), o el desfase desde el inicio del día, de la
    semana (lunes 00:00) o del mes (día 1, 00:00). Los mensuales van del día 1 al 28, para que
    todo mes lo tenga. Se construye con ``every``, ``daily``, ``weekly`` o ``monthly``.
    """

    kind: ScheduleKind
    offset_seconds: int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ScheduleKind):
            raise TypeError("kind debe ser ScheduleKind")
        value: object = self.offset_seconds
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("offset_seconds debe ser int")
        limits = {
            ScheduleKind.EVERY: (MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS),
            ScheduleKind.DAILY: (0, _DAY - 1),
            ScheduleKind.WEEKLY: (0, _WEEK - 1),
            ScheduleKind.MONTHLY: (0, 28 * _DAY - 1),
        }[self.kind]
        if not limits[0] <= value <= limits[1]:
            raise ValueError(f"offset_seconds fuera de [{limits[0]}, {limits[1]}]")

    @classmethod
    def every(cls, seconds: int) -> Schedule:
        return cls(ScheduleKind.EVERY, seconds)

    @classmethod
    def daily(cls, *, hour: int = 0, minute: int = 0) -> Schedule:
        return cls(ScheduleKind.DAILY, _clock_offset(hour, minute))

    @classmethod
    def weekly(cls, *, weekday: int = 0, hour: int = 0, minute: int = 0) -> Schedule:
        """``weekday``: 0 es el lunes, como ``datetime.weekday()``."""
        if isinstance(weekday, bool) or not isinstance(weekday, int) or not 0 <= weekday <= 6:
            raise ValueError("weekday debe estar entre 0 (lunes) y 6 (domingo)")
        return cls(ScheduleKind.WEEKLY, weekday * _DAY + _clock_offset(hour, minute))

    @classmethod
    def monthly(cls, *, day: int = 1, hour: int = 0, minute: int = 0) -> Schedule:
        if isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 28:
            raise ValueError("day debe estar entre 1 y 28")
        return cls(ScheduleKind.MONTHLY, (day - 1) * _DAY + _clock_offset(hour, minute))

    @property
    def text(self) -> str:
        """Forma persistida en ``shared.periodic_task.schedule`` (≤ 64 caracteres)."""
        return f"{self.kind.value}:{self.offset_seconds}s"

    def next_after(self, instant: datetime) -> datetime:
        """El primer instante del horario estrictamente posterior a ``instant``."""
        if instant.tzinfo is None:
            raise ValueError("instant debe llevar zona horaria")
        instant = instant.astimezone(UTC)
        if self.kind is ScheduleKind.EVERY:
            return _next_on_grid(instant, _EPOCH, self.offset_seconds, 0)
        if self.kind is ScheduleKind.DAILY:
            return _next_on_grid(instant, _EPOCH, _DAY, self.offset_seconds)
        if self.kind is ScheduleKind.WEEKLY:
            return _next_on_grid(instant, _MONDAY_EPOCH, _WEEK, self.offset_seconds)
        month_start = instant.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        candidate = month_start + timedelta(seconds=self.offset_seconds)
        if candidate > instant:
            return candidate
        following = (month_start + timedelta(days=32)).replace(day=1)
        return following + timedelta(seconds=self.offset_seconds)


def _clock_offset(hour: int, minute: int) -> int:
    for value, top, label in ((hour, 23, "hour"), (minute, 59, "minute")):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= top:
            raise ValueError(f"{label} debe estar entre 0 y {top}")
    return hour * 3600 + minute * 60


def _next_on_grid(instant: datetime, origin: datetime, period: int, offset: int) -> datetime:
    """El siguiente ``origin + offset + k·period`` estrictamente posterior a ``instant``."""
    base = origin + timedelta(seconds=offset)
    elapsed = (instant - base) // timedelta(microseconds=1)
    step = period * 1_000_000
    return base + timedelta(microseconds=(elapsed // step + 1) * step)


@dataclass(frozen=True, kw_only=True)
class PeriodicTask:
    """Declaración de una tarea periódica; siempre itera las organizaciones (§4.3)."""

    task_name: str
    unit: ActorUnit
    schedule: Schedule
    handler: PeriodicHandler

    def to_persisted(self) -> PersistedPeriodicTask:
        return PersistedPeriodicTask(
            task_name=self.task_name, unit=self.unit.value, schedule=self.schedule.text
        )


@dataclass(frozen=True, kw_only=True)
class PersistedPeriodicTask:
    """Las columnas declaradas de ``shared.periodic_task`` (sin arrendamiento ni avance)."""

    task_name: str
    unit: str
    schedule: str


class PeriodicTaskRegistry:
    """Las tareas periódicas de un proceso."""

    def __init__(self) -> None:
        self._tasks: dict[str, PeriodicTask] = {}
        self._sealed = False

    def register(
        self,
        task_name: str,
        schedule: Schedule,
        handler: PeriodicHandler,
        *,
        unit: ActorUnit,
    ) -> PeriodicTask:
        if self._sealed:
            raise OutboxRegistrationRejected("la tarea", task_name, _sealed_problem("tareas"))
        problems = _name_problems("el nombre de la tarea", task_name)
        problems += _unit_problems(unit)
        declared_schedule: object = schedule
        declared_handler: object = handler
        if not isinstance(declared_schedule, Schedule):
            problems.append("schedule debe ser un Schedule")
        if not callable(declared_handler):
            problems.append("handler debe ser invocable")
        if isinstance(task_name, str) and task_name in self._tasks:
            problems.append("la tarea ya está registrada")
        if problems:
            raise OutboxRegistrationRejected("la tarea", task_name, problems)
        task = PeriodicTask(task_name=task_name, unit=unit, schedule=schedule, handler=handler)
        self._tasks[task_name] = task
        return task

    def get(self, task_name: str) -> PeriodicTask | None:
        return self._tasks.get(task_name)

    def tasks(self) -> tuple[PeriodicTask, ...]:
        return tuple(self._tasks[name] for name in sorted(self._tasks))

    def seal(self) -> None:
        self._sealed = True

    @property
    def sealed(self) -> bool:
        return self._sealed


# --- Persistencia y arranque ----------------------------------------------------------------


class OutboxCatalogStore(Protocol):
    """Puerto de las tablas globales ``shared.event_type``, ``consumer`` y ``periodic_task``."""

    async def load_event_types(self) -> Mapping[str, PersistedEventType]: ...

    async def load_consumers(self) -> Mapping[str, PersistedConsumer]: ...

    async def load_periodic_tasks(self) -> Mapping[str, PersistedPeriodicTask]: ...

    async def save_event_type(self, row: PersistedEventType) -> None:
        """Inserta la fila o sustituye su esquema y su descripción."""
        ...

    async def save_consumer(self, row: PersistedConsumer) -> None:
        """Inserta la fila o sustituye sus columnas declaradas; nunca toca el circuito."""
        ...

    async def save_periodic_task(self, row: PersistedPeriodicTask, next_run_at: datetime) -> None:
        """Inserta la fila o sustituye su horario y su próxima ejecución; nunca el arrendamiento."""
        ...


class InMemoryOutboxCatalogStore:
    """Adaptador en memoria del puerto (pruebas y arranque sin base)."""

    def __init__(self) -> None:
        self.event_types: dict[str, PersistedEventType] = {}
        self.consumers: dict[str, PersistedConsumer] = {}
        self.periodic_tasks: dict[str, tuple[PersistedPeriodicTask, datetime]] = {}

    async def load_event_types(self) -> Mapping[str, PersistedEventType]:
        return dict(self.event_types)

    async def load_consumers(self) -> Mapping[str, PersistedConsumer]:
        return dict(self.consumers)

    async def load_periodic_tasks(self) -> Mapping[str, PersistedPeriodicTask]:
        return {name: row for name, (row, _) in self.periodic_tasks.items()}

    async def save_event_type(self, row: PersistedEventType) -> None:
        self.event_types[row.event_name] = row

    async def save_consumer(self, row: PersistedConsumer) -> None:
        self.consumers[row.consumer_name] = row

    async def save_periodic_task(self, row: PersistedPeriodicTask, next_run_at: datetime) -> None:
        self.periodic_tasks[row.task_name] = (row, next_run_at)


class OutboxCatalog:
    """Los tres registros de un proceso; ``synchronize`` los contrasta, guarda y sella."""

    def __init__(self) -> None:
        self.event_types = EventTypeRegistry()
        self.consumers = ConsumerRegistry()
        self.periodic_tasks = PeriodicTaskRegistry()

    @property
    def sealed(self) -> bool:
        return self.event_types.sealed and self.consumers.sealed and self.periodic_tasks.sealed

    def seal(self) -> None:
        self.event_types.seal()
        self.consumers.seal()
        self.periodic_tasks.seal()

    def check(self) -> None:
        """Un consumidor suscrito a un evento no registrado impide arrancar (§8)."""
        known = set(self.event_types.event_names())
        problems = [
            f"el consumidor «{consumer.consumer_name}» está suscrito a «{event}», que ningún "
            "EventType registra"
            for consumer in self.consumers.consumers()
            for event in consumer.subscribed_events
            if event not in known
        ]
        if problems:
            raise OutboxStartupError(problems)

    async def synchronize(self, store: OutboxCatalogStore, clock: Clock) -> None:
        """Contrasta con las tablas globales, guarda lo nuevo o ampliado y sella el catálogo."""
        self.check()
        events = await store.load_event_types()
        consumers = await store.load_consumers()
        tasks = await store.load_periodic_tasks()
        problems = [
            *self._event_type_problems(events),
            *self._consumer_problems(consumers),
            *self._task_problems(tasks),
        ]
        if problems:
            raise OutboxStartupError(problems)
        for compiled in self.event_types.compiled_types():
            row = compiled.to_persisted()
            if events.get(compiled.event_name) != row:
                await store.save_event_type(row)
        for consumer in self.consumers.consumers():
            consumer_row = consumer.to_persisted()
            if consumers.get(consumer.consumer_name) != consumer_row:
                await store.save_consumer(consumer_row)
        now = clock.now()
        for task in self.periodic_tasks.tasks():
            task_row = task.to_persisted()
            if tasks.get(task.task_name) != task_row:
                await store.save_periodic_task(task_row, task.schedule.next_after(now))
        self.seal()

    def _event_type_problems(self, persisted: Mapping[str, PersistedEventType]) -> list[str]:
        problems: list[str] = []
        for name in sorted(persisted):
            row = persisted[name]
            compiled = self.event_types.get(name)
            if compiled is None:
                problems.append(
                    f"evento «{name}»: está en la base y ninguna unidad lo registra; retirar un "
                    "evento está prohibido (los eventos publicados deben poder leerse)"
                )
                continue
            if row.publisher_unit != compiled.definition.publisher_unit.value:
                problems.append(
                    f"evento «{name}»: la unidad emisora cambió de {row.publisher_unit} a "
                    f"{compiled.definition.publisher_unit.value}"
                )
            if normalized_schema(row.payload_schema) != normalized_schema(compiled.payload_schema):
                problems.extend(
                    f"evento «{name}»: {problem} (la carga solo puede ampliarse)"
                    for problem in compatibility_problems(
                        row.payload_schema, compiled.payload_schema
                    )
                )
        return problems

    def _consumer_problems(self, persisted: Mapping[str, PersistedConsumer]) -> list[str]:
        problems: list[str] = []
        for name in sorted(persisted):
            consumer = self.consumers.get(name)
            if consumer is None:
                problems.append(
                    f"consumidor «{name}»: está en la base y ninguna unidad lo registra; sus "
                    "entregas pendientes quedarían sin despachar"
                )
            elif persisted[name].unit != consumer.unit.value:
                problems.append(
                    f"consumidor «{name}»: la unidad cambió de {persisted[name].unit} a "
                    f"{consumer.unit.value}"
                )
        return problems

    def _task_problems(self, persisted: Mapping[str, PersistedPeriodicTask]) -> list[str]:
        problems: list[str] = []
        for name in sorted(persisted):
            task = self.periodic_tasks.get(name)
            if task is None:
                problems.append(f"tarea «{name}»: está en la base y ninguna unidad la registra")
            elif persisted[name].unit != task.unit.value:
                problems.append(
                    f"tarea «{name}»: la unidad cambió de {persisted[name].unit} a "
                    f"{task.unit.value}"
                )
        return problems
