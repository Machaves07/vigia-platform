"""TASK-326: retirada exhaustiva del catálogo de U-04 en las tres raíces reales.

18 tipos (17 de §5.1 y el de VIG-274), 25 eventos de dominio (+3 internos), 6 consumidores, 10
periódicas (9 del diseño + el sondeo de correo), 2 validadores y el puerto. Reconstruir sigue a
demanda (U04-H-14).
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
from pathlib import Path
from typing import Any

import pytest

from tests.integration.test_laz_bootstrap_closed import _compose_admin, _without
from tests.integration.test_u03_startup_checks import (
    _app_config,
    _compose_api,
    _compose_worker,
    _free_port,
    _registration_line,
    _runtime,
    _worker_config,
)
from tests.runtime_support import PROVIDER_ID, FakeReader
from vigia_platform.identity.application import admin_cli
from vigia_platform.loop.registration import (
    U04_CONSUMERS,
    U04_DOMAIN_EVENT_TYPES,
    U04_EVENT_TYPES,
    U04_FREE_TEXT_VALIDATORS,
    U04_INTERNAL_EVENT_TYPES,
    U04_RECORD_TYPES,
    U04_TASKS,
)
from vigia_platform.loop.worker.bulkheads import ConsumerBulkhead
from vigia_platform.notifications import registration as notifications
from vigia_platform.shared.api import main as api_main
from vigia_platform.shared.api.app import STARTUP_FAILURE_EXIT_CODE
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.outbox.registries import ConsumerRegistry, PeriodicTaskRegistry, Schedule
from vigia_platform.shared.runtime.admin import compose_admin_runtime
from vigia_platform.shared.runtime.api import compose_api_runtime
from vigia_platform.shared.runtime.config import RegistrationIncomplete
from vigia_platform.shared.runtime.units import UnitServices, record_type_registry, registered_units
from vigia_platform.shared.runtime.worker import compose_worker_runtime
from vigia_platform.shared.worker import main as worker_main

pytestmark = pytest.mark.integration

MISSING = (
    *(("record_type", n) for n in sorted(U04_RECORD_TYPES)),
    *(("event_type", n) for n in sorted(U04_DOMAIN_EVENT_TYPES)),
    *(("consumer", n) for n in sorted(U04_CONSUMERS)),
    *(("periodic_task", n) for n in sorted(U04_TASKS)),
    *(("free_text_validator", n) for n in sorted(U04_FREE_TEXT_VALIDATORS)),
    ("port", "email_sender"),
)


def test_exact_counts_and_real_registrations(tmp_path: Path) -> None:
    assert tuple(
        map(
            len,
            (
                U04_RECORD_TYPES,
                U04_DOMAIN_EVENT_TYPES,
                U04_CONSUMERS,
                U04_TASKS,
                U04_FREE_TEXT_VALIDATORS,
            ),
        )
    ) == (18, 25, 6, 10, 2)  # 17 de §5.1 y closure_attachment_unrecoverable (VIG-274)
    assert len(MISSING) == 62
    assert {
        "assistant_requested",
        "export_requested",
        "email_delivery_events_poll",
    } == U04_INTERNAL_EVENT_TYPES
    assert len(U04_EVENT_TYPES) == 28
    assert {
        c.record_type
        for c in record_type_registry(registered_units()).latest()
        if c.writer_unit is ActorUnit.U04
    } == U04_RECORD_TYPES
    runtime = _runtime(tmp_path)
    for compose in (_compose_worker, _compose_api, _compose_admin):
        result = compose(runtime, registered_units())
        assert result is not None
        if compose is _compose_admin:
            assert result.loop_projection is not None
            assert "rebuild-loop-projection" in admin_cli.COMMANDS
        elif compose is _compose_worker:
            catalog = result.catalog
            assert {
                c.event_name
                for c in catalog.event_types.compiled_types()
                if c.definition.publisher_unit is ActorUnit.U04
            } == U04_EVENT_TYPES
            assert {
                c.consumer_name for c in catalog.consumers.consumers() if c.unit is ActorUnit.U04
            } == set(U04_CONSUMERS)
            admissions = []
            for name, size in U04_CONSUMERS.items():
                consumer = catalog.consumers.get(name)
                assert consumer is not None
                assert isinstance(consumer.admission, ConsumerBulkhead)
                assert consumer.admission.size == size
                assert consumer.admission.consumer_name == name
                admissions.append(id(consumer.admission))
            assert len(set(admissions)) == 6


@pytest.mark.parametrize(("kind", "name"), MISSING, ids=[f"{k}:{n}" for k, n in MISSING])
def test_every_missing_registration_refuses_all_three_roots_and_processes(
    kind: str, name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if kind == "port":
        monkeypatch.setattr(notifications, "EMAIL_SENDERS", None)
        units = registered_units()
    else:
        units = _without(kind, name)
    runtime = _runtime(tmp_path)
    for compose in (_compose_worker, _compose_api, _compose_admin):
        with pytest.raises(RegistrationIncomplete) as raised:
            compose(runtime, units)
        assert raised.value.missing == ((kind, name),)
    error = io.StringIO()
    monkeypatch.setattr("sys.stderr", error)

    async def worker(config: Any) -> Any:
        return await compose_worker_runtime(config, runtime, units=units, reader=FakeReader())

    async def api(config: Any) -> Any:
        return await compose_api_runtime(config, runtime, units=units, reader=FakeReader())

    async def admin(config: Any, provider: Any) -> Any:
        return await compose_admin_runtime(
            config, provider, runtime, units=units, reader=FakeReader()
        )

    def check(code: int) -> None:
        assert code == STARTUP_FAILURE_EXIT_CODE
        assert _registration_line(error.getvalue())["missing"] == [{"kind": kind, "name": name}]
        error.seek(0)
        error.truncate()

    check(asyncio.run(worker_main.serve(_worker_config(), worker)))
    check(
        asyncio.run(
            api_main.serve(
                _app_config(), api_main.ApiServerConfig(host="127.0.0.1", port=_free_port()), api
            )
        )
    )
    check(
        admin_cli.run(
            [
                "rebuild-loop-projection",
                "--organization",
                str(PROVIDER_ID),
                "--plant",
                str(PROVIDER_ID),
                "--operator",
                str(PROVIDER_ID),
                "--dry-run",
            ],
            environ={
                "VIGIA_ENVIRONMENT": "test",
                "VIGIA_PROVIDER_ORGANIZATION_ID": str(PROVIDER_ID),
            },
            builder=admin,
            stderr=error,
        )
    )


@pytest.mark.parametrize("name", sorted(U04_CONSUMERS))
@pytest.mark.parametrize("mutation", ["absent", "foreign"])
def test_removing_or_misassigning_any_bulkhead_is_a_startup_failure(
    name: str, mutation: str, tmp_path: Path
) -> None:
    units = []
    for unit in registered_units():
        original = unit.consumers

        def consumers(
            registry: ConsumerRegistry, services: UnitServices, register: Any = original
        ) -> None:
            full = ConsumerRegistry()
            register(full, services)
            for consumer in full.consumers():
                if consumer.consumer_name == name:
                    admission = consumer.admission
                    if mutation == "foreign":
                        assert isinstance(admission, ConsumerBulkhead)
                        admission.consumer_name = "another_consumer"
                    else:
                        consumer = dataclasses.replace(consumer, admission=None)
                registry.register(consumer)

        units.append(dataclasses.replace(unit, consumers=consumers))
    with pytest.raises(RegistrationIncomplete) as raised:
        _compose_worker(_runtime(tmp_path), units)
    assert raised.value.missing == (("consumer", name),)


@pytest.mark.parametrize("name", sorted(U04_TASKS))
@pytest.mark.parametrize("attribute", ["schedule", "unit", "iteration"])
def test_wrong_task_metadata_is_a_startup_failure(
    name: str, attribute: str, tmp_path: Path
) -> None:
    from vigia_platform.shared.outbox.registries import TaskIteration

    units = []
    for unit in registered_units():
        original = unit.periodic_tasks

        def tasks(
            registry: PeriodicTaskRegistry, services: UnitServices, register: Any = original
        ) -> None:
            full = PeriodicTaskRegistry()
            register(full, services)
            for task in full.tasks():
                values: dict[str, Any] = {
                    "schedule": task.schedule,
                    "unit": task.unit,
                    "iteration": task.iteration,
                }
                if task.task_name == name:
                    values[attribute] = {
                        "schedule": Schedule.every(7),
                        "unit": ActorUnit.U03,
                        "iteration": (
                            TaskIteration.GLOBAL
                            if task.iteration is TaskIteration.PER_ORGANIZATION
                            else TaskIteration.PER_ORGANIZATION
                        ),
                    }[attribute]
                registry.register(task.task_name, handler=task.handler, **values)

        units.append(dataclasses.replace(unit, periodic_tasks=tasks))
    with pytest.raises(RegistrationIncomplete) as raised:
        _compose_worker(_runtime(tmp_path), units)
    assert raised.value.missing == (("periodic_task", name),)
