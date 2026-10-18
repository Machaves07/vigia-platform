"""Registro de U-04 en la raíz de composición (TASK-302; LC-LAZ-37; NFR-LAZ-14; A-52).

- **Tres unidades** en ``REGISTERED_UNITS`` (``loop``, ``accreditation`` y ``notifications``),
  después de las de U-02 y U-03, que delegan en el ``registration.py`` de su paquete.
- **Manifiestos a mano**: ``U04_RECORD_TYPES`` (17) y ``U04_EVENT_TYPES`` (25) coinciden con lo
  que registra el código; ``U04_TASKS`` y ``U04_CONSUMERS``, hoy vacíos, y los ``ROUTERS``:
  solo las dos rutas de la configuración del lazo (VIG-205).
- **La raíz real** compone los 17 tipos y los 25 eventos de U-04 además de los de U-02 y U-03, y
  ``missing_u04_registrations`` no encuentra nada que falte.
- **Fallo cerrado**: sin un tipo, un evento, una tarea o un consumidor del manifiesto (o con la
  tarea en otra cadencia, otra unidad u otra iteración, o el tipo escrito por otra unidad),
  ``missing_u04_registrations`` nombra exactamente lo que falta y ``verify_required`` lanza
  ``RegistrationIncomplete``.

Sin base ni red: los consumidores y las tareas se componen con dependencias inertes.
"""

from __future__ import annotations

import uuid
from typing import Any, Final
from unittest.mock import Mock

import pytest

from tests.properties.laz.strategies.u04_records import u04_event_registry, u04_registry
from vigia_platform.accreditation import registration as accreditation_registration
from vigia_platform.accreditation.events import ACCREDITATION_EVENT_TYPES
from vigia_platform.accreditation.record_types import ACCREDITATION_RECORD_TYPES
from vigia_platform.ledger.registry import ChainLevel, ContentModel, RecordType, RecordTypeRegistry
from vigia_platform.loop import registration
from vigia_platform.loop.adapters.http.services import LOOP_STATE_KEY, LoopHttp
from vigia_platform.loop.events import LOOP_EVENT_TYPES
from vigia_platform.loop.record_types import LOOP_RECORD_TYPES
from vigia_platform.notifications import registration as notifications_registration
from vigia_platform.notifications.events import NOTIFICATIONS_EVENT_TYPES
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.observability.metrics import get_metrics
from vigia_platform.shared.outbox.registries import (
    Consumer,
    ConsumerRegistry,
    EventType,
    OutboxCatalog,
    PayloadModel,
    PeriodicTaskRegistry,
    Schedule,
    TaskIteration,
)
from vigia_platform.shared.runtime.config import RegistrationIncomplete
from vigia_platform.shared.runtime.units import (
    UnitServices,
    outbox_catalog,
    record_type_registry,
    registered_units,
    verify_required,
)

U04_UNITS: Final = ("loop", "accreditation", "notifications")


def _services() -> UnitServices:
    """Servicios inertes (como ``composed_tasks`` de ``test_task_isolation``): las tareas y los
    consumidores se construyen, pero ninguno se ejecuta."""
    inert: Any = Mock()
    return UnitServices(
        clock=inert,
        metrics=get_metrics(),
        provider_organization_id=uuid.uuid4(),
        database=inert,
        contexts=inert,
        authorizer=inert,
        audit=inert,
        outbox=inert,
        writer=inert,
        free_text=inert,
        signing=inert,
        checkpoints=inert,
        kms=inert,
    )


def _root() -> tuple[RecordTypeRegistry, OutboxCatalog]:
    units = registered_units()
    return record_type_registry(units), outbox_catalog(units, _services())


# --- las tres unidades ----------------------------------------------------------------------


def test_the_three_u04_units_are_registered_after_u02_and_u03() -> None:
    names = [unit.name for unit in registered_units()]
    assert names[-3:] == list(U04_UNITS)
    assert names[:-3] == ["shared", "identity", "ledger", "catalog", "fleet", "node_api"]
    units = {unit.name: unit for unit in registered_units()}
    assert units["loop"].required is registration.missing_u04_registrations
    assert units["loop"].free_text is registration.loop_free_text
    accreditation, notifications = units["accreditation"], units["notifications"]
    assert accreditation.record_types is accreditation_registration.accreditation_record_types
    assert notifications.event_types is notifications_registration.notifications_event_types


def test_the_u04_units_register_only_what_their_tasks_added() -> None:
    units = {unit.name: unit for unit in registered_units()}
    tasks = PeriodicTaskRegistry()
    consumers = ConsumerRegistry()
    for name in U04_UNITS:
        unit = units[name]
        unit.periodic_tasks(tasks, _services())
        unit.consumers(consumers, _services())
    assert tasks.tasks() == () and consumers.consumers() == ()
    # VIG-205 (TASK-304): las dos rutas de la configuración y LoopHttp en app.state.
    routes = {
        (method, route.path)
        for router in units["loop"].routers()
        for route in router.routes
        for method in getattr(route, "methods", ())
    }
    assert routes == {
        ("GET", "/loop/plants/{plant_id}/configuration"),
        ("PUT", "/loop/plants/{plant_id}/configuration"),
    }
    state = units["loop"].api_state(_services())
    assert set(state) == {LOOP_STATE_KEY} and isinstance(state[LOOP_STATE_KEY], LoopHttp)
    for name in ("accreditation", "notifications"):
        assert units[name].routers() == ()
        assert units[name].api_state(_services()) == {}
    for module in (accreditation_registration, notifications_registration):
        assert module.ROUTERS == ()
    for module in (registration, accreditation_registration, notifications_registration):
        assert module.TASK_REGISTRARS == () and module.CONSUMER_REGISTRARS == ()
    assert registration.U04_TASKS == {} and registration.U04_CONSUMERS == {}
    assert registration.FREE_TEXT_REGISTRARS == ()


# --- manifiestos ----------------------------------------------------------------------------


def test_the_manifests_match_the_types_and_events_of_the_code() -> None:
    assert len(registration.U04_RECORD_TYPES) == 17 and len(registration.U04_EVENT_TYPES) == 25
    assert len(LOOP_RECORD_TYPES) == 13 and len(ACCREDITATION_RECORD_TYPES) == 4
    assert len(LOOP_EVENT_TYPES) == 21 and len(ACCREDITATION_EVENT_TYPES) == 4
    assert NOTIFICATIONS_EVENT_TYPES == ()
    assert {
        definition.record_type for definition in (*LOOP_RECORD_TYPES, *ACCREDITATION_RECORD_TYPES)
    } == registration.U04_RECORD_TYPES
    assert {
        event.event_name for event in (*LOOP_EVENT_TYPES, *ACCREDITATION_EVENT_TYPES)
    } == registration.U04_EVENT_TYPES
    assert set(u04_registry().record_types()) == registration.U04_RECORD_TYPES
    assert set(u04_event_registry().event_names()) == registration.U04_EVENT_TYPES


def test_the_real_root_lists_u04_besides_u02_and_u03() -> None:
    record_types, catalog = _root()
    by_unit: dict[ActorUnit, set[str]] = {}
    for compiled in record_types.latest():
        by_unit.setdefault(compiled.writer_unit, set()).add(compiled.record_type)
    assert by_unit[ActorUnit.U04] == registration.U04_RECORD_TYPES
    assert by_unit[ActorUnit.U02] and by_unit[ActorUnit.U03]
    events = {
        compiled.event_name
        for compiled in catalog.event_types.compiled_types()
        if compiled.definition.publisher_unit is ActorUnit.U04
    }
    assert events == registration.U04_EVENT_TYPES
    assert registration.missing_u04_registrations(record_types, catalog) == ()
    verify_required(registered_units(), record_types, catalog)


# --- fallo cerrado --------------------------------------------------------------------------


def _types_without(name: str) -> RecordTypeRegistry:
    full = record_type_registry(registered_units())
    reduced = RecordTypeRegistry()
    for compiled in full.all_versions():
        if compiled.record_type != name:
            reduced.register(compiled.definition)
    return reduced


def _catalog_without_event(name: str) -> OutboxCatalog:
    reduced = OutboxCatalog()
    for unit in registered_units():
        full = OutboxCatalog()
        unit.event_types(full.event_types)
        for compiled in full.event_types.compiled_types():
            if compiled.event_name != name:
                reduced.event_types.register(compiled.definition)
    return reduced


@pytest.mark.parametrize("name", sorted(registration.U04_RECORD_TYPES))
def test_a_missing_type_is_named(name: str) -> None:
    _, catalog = _root()
    types = _types_without(name)
    assert registration.missing_u04_registrations(types, catalog) == (("record_type", name),)
    with pytest.raises(RegistrationIncomplete) as raised:
        verify_required(registered_units(), types, catalog)
    assert raised.value.missing == (("record_type", name),)


@pytest.mark.parametrize("name", sorted(registration.U04_EVENT_TYPES))
def test_a_missing_event_is_named(name: str) -> None:
    types, _ = _root()
    catalog = _catalog_without_event(name)
    assert registration.missing_u04_registrations(types, catalog) == (("event_type", name),)


class _Probe(ContentModel):
    probe: bool


class _ProbePayload(PayloadModel):
    probe: bool = True


def test_a_u04_type_registered_by_another_unit_counts_as_missing() -> None:
    _, catalog = _root()
    types = _types_without("closure")
    types.register(
        RecordType(
            record_type="closure",
            writer_unit=ActorUnit.U03,
            chain_level=ChainLevel.PLANT,
            schema_version=1,
            content_model=_Probe,
        )
    )
    assert registration.missing_u04_registrations(types, catalog) == (("record_type", "closure"),)


def test_a_u04_event_published_by_another_unit_counts_as_missing() -> None:
    types, _ = _root()
    catalog = _catalog_without_event("adoption_low")
    catalog.event_types.register(
        EventType(
            event_name="adoption_low",
            publisher_unit=ActorUnit.U02,
            payload_model=_ProbePayload,
            description_es="Sonda",
        )
    )
    missing = registration.missing_u04_registrations(types, catalog)
    assert missing == (("event_type", "adoption_low"),)


async def _never(*_: object) -> None:
    raise AssertionError("el manejador no se ejecuta")


_PROBE_TASK: Final = "u04_probe_task"
_PROBE_SCHEDULE: Final = Schedule.every(3600)


def _with_task(
    schedule: Schedule = _PROBE_SCHEDULE,
    unit: ActorUnit = ActorUnit.U04,
    iteration: TaskIteration = TaskIteration.PER_ORGANIZATION,
) -> OutboxCatalog:
    _, catalog = _root()
    catalog.periodic_tasks.register(_PROBE_TASK, schedule, _never, unit=unit, iteration=iteration)
    return catalog


@pytest.fixture
def probe_manifests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un manifiesto con una tarea y un consumidor, como los que añadirán las tareas de U-04."""
    monkeypatch.setattr(
        registration,
        "U04_TASKS",
        {_PROBE_TASK: (_PROBE_SCHEDULE, TaskIteration.PER_ORGANIZATION)},
    )
    monkeypatch.setattr(registration, "U04_CONSUMERS", {"u04_probe_consumer": 2})


@pytest.mark.usefixtures("probe_manifests")
def test_a_declared_task_and_consumer_pass_when_registered_as_declared() -> None:
    types, _ = _root()
    catalog = _with_task()
    catalog.consumers.register(
        Consumer(
            consumer_name="u04_probe_consumer",
            unit=ActorUnit.U04,
            subscribed_events=("finding_closed",),
            handler=_never,
        )
    )
    assert registration.missing_u04_registrations(types, catalog) == ()


@pytest.mark.usefixtures("probe_manifests")
@pytest.mark.parametrize(
    "catalog_of",
    [
        lambda: _root()[1],
        lambda: _with_task(schedule=Schedule.every(60)),
        lambda: _with_task(unit=ActorUnit.U03),
        lambda: _with_task(iteration=TaskIteration.GLOBAL),
    ],
    ids=["ausente", "otra-cadencia", "otra-unidad", "otra-iteracion"],
)
def test_a_task_missing_or_not_as_declared_is_named(catalog_of: Any) -> None:
    types, _ = _root()
    catalog = catalog_of()
    missing = registration.missing_u04_registrations(types, catalog)
    assert ("periodic_task", _PROBE_TASK) in missing
    assert ("consumer", "u04_probe_consumer") in missing
    assert len(missing) == 2


@pytest.mark.usefixtures("probe_manifests")
def test_a_consumer_of_another_unit_is_named() -> None:
    types, _ = _root()
    catalog = _with_task()
    catalog.consumers.register(
        Consumer(
            consumer_name="u04_probe_consumer",
            unit=ActorUnit.U02,
            subscribed_events=("finding_closed",),
            handler=_never,
        )
    )
    assert registration.missing_u04_registrations(types, catalog) == (
        ("consumer", "u04_probe_consumer"),
    )


def test_the_unit_registrars_are_called_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lo que una tarea añada a ``TASK_REGISTRARS`` se registra sin tocar ``units.py``."""
    calls: list[str] = []

    def register(registry: PeriodicTaskRegistry, services: UnitServices) -> None:
        calls.append("task")
        registry.register(_PROBE_TASK, _PROBE_SCHEDULE, _never, unit=ActorUnit.U04)

    monkeypatch.setattr(registration, "TASK_REGISTRARS", (register,))
    unit = next(unit for unit in registered_units() if unit.name == "loop")
    registry = PeriodicTaskRegistry()
    unit.periodic_tasks(registry, _services())
    assert calls == ["task"] and [task.task_name for task in registry.tasks()] == [_PROBE_TASK]
