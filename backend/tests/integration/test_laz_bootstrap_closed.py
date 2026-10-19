"""Arranque con fallo cerrado de U-04 (TASK-302; NFR-LAZ-14; PAT-LAZ-RES-07; LC-LAZ-37).

Mismo patrón que ``test_u03_startup_checks.py``: ``vigia-api``, ``vigia-worker`` y
``vigia-admin`` construidos con la raíz real (``compose_*_runtime`` con las unidades del
registro), quitando **una sola cosa** cada vez:

- **cada uno de los 17 tipos y de los 25 eventos** de U-04 y el trabajo interno
  ``export_requested`` (y cada tarea de ``U04_TASKS``, hoy vacío, cada consumidor de
  ``U04_CONSUMERS``: ``export_worker`` desde TASK-317 y ``notification_engine`` desde TASK-324,
  y cada validador de texto libre de
  ``U04_FREE_TEXT_VALIDATORS``: ``blocked_vocabulary``, TASK-303, y ``person_attribution``,
  TASK-340): la composición no termina
  (``RegistrationIncomplete`` nombra exactamente lo que falta) y el proceso sale con
  ``STARTUP_FAILURE_EXIT_CODE`` y la línea ``registration_missing`` que lo nombra;
- **cada enumeración con etiqueta** de U-04: sin la etiqueta de uno de sus valores en
  ``labels.loop.es.json``, ``vigia-api`` no se construye (``ApiStartupError`` con el valor) y el
  proceso sale con ``STARTUP_FAILURE_EXIT_CODE``. Las etiquetas solo las muestra ``vigia-api``:
  el worker y ``vigia-admin`` no las cargan.

Con todo registrado, los tres procesos componen y listan los 17 tipos y los 25 eventos de U-04
además de los de U-02 y U-03, y la política de texto libre lista ``blocked_vocabulary`` y
``person_attribution`` junto al validador mínimo de U-03. Reloj simulado; solo datos generados.
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
from collections.abc import Sequence
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any, Final

import pytest

from tests.integration.test_u03_startup_checks import (
    _app_config,
    _compose_api,
    _compose_worker,
    _free_port,
    _registration_line,
    _runtime,
    _worker_config,
)
from tests.integration.test_u03_startup_checks import _strip as _strip_u03
from tests.runtime_support import FakeReader
from tests.unit.test_runtime_units import PROVIDER_ID, _admin_config
from vigia_platform.accreditation.registration import ACCREDITATION_LABELS
from vigia_platform.catalog.application.free_text_validator import U03_FREE_TEXT_VALIDATOR
from vigia_platform.fleet.registration import U03_EVENT_TYPES
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.ledger.registry import ChainLevel
from vigia_platform.loop.registration import (
    LOOP_LABELS,
    U04_CONSUMERS,
    U04_EVENT_TYPES,
    U04_FREE_TEXT_VALIDATORS,
    U04_RECORD_TYPES,
    U04_TASKS,
)
from vigia_platform.notifications.registration import NOTIFICATIONS_LABELS
from vigia_platform.shared.api import main as api_main
from vigia_platform.shared.api.app import STARTUP_FAILURE_EXIT_CODE, AppConfig, create_app
from vigia_platform.shared.api.errors import ApiStartupError
from vigia_platform.shared.api.labels import DEFAULT_LOOP_LABELS_PATH
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.outbox.registries import ConsumerRegistry, OutboxStartupError
from vigia_platform.shared.runtime.admin import compose_admin_runtime
from vigia_platform.shared.runtime.api import compose_api_runtime
from vigia_platform.shared.runtime.config import RegistrationIncomplete, RuntimeConfig
from vigia_platform.shared.runtime.units import (
    PlatformUnit,
    UnitServices,
    free_text_registry,
    record_type_registry,
    registered_units,
)
from vigia_platform.shared.runtime.worker import compose_worker_runtime
from vigia_platform.shared.worker import main as worker_main
from vigia_platform.shared.worker.main import WorkerConfig, WorkerRuntime

pytestmark = pytest.mark.integration

MISSING: Final[tuple[tuple[str, str], ...]] = (
    *(("record_type", name) for name in sorted(U04_RECORD_TYPES)),
    *(("event_type", name) for name in sorted(U04_EVENT_TYPES)),
    *(("periodic_task", name) for name in sorted(U04_TASKS)),
    *(("consumer", name) for name in sorted(U04_CONSUMERS)),
    *(("free_text_validator", name) for name in sorted(U04_FREE_TEXT_VALIDATORS)),
)
"""Cada una de las 17 + 26 + ``len(U04_TASKS)`` + ``len(U04_CONSUMERS)`` +
``len(U04_FREE_TEXT_VALIDATORS)`` cosas que U-04 exige."""

SUBSCRIBED: Final = frozenset({"export_requested"})
"""Evento de U-04 cuya suscripción se conserva al quitarlo (``export_worker``, TASK-317): el
catálogo se niega a componerse con la suscripción huérfana. De los demás eventos de U-04 también
se quita la suscripción de ``notification_engine`` (TASK-324)."""

ONE_OF_EACH: Final = (
    ("record_type", "closure"),
    ("record_type", "vocabulary_list_changed"),
    ("event_type", "finding_closed"),
    ("event_type", "adoption_low"),
    ("free_text_validator", "blocked_vocabulary"),
    ("free_text_validator", "person_attribution"),
)

U04_BINDINGS: Final = {**LOOP_LABELS, **ACCREDITATION_LABELS, **NOTIFICATIONS_LABELS}
LABELLED: Final[tuple[tuple[str, str], ...]] = tuple(
    (name, next(iter(kind)).value) for name, kind in sorted(U04_BINDINGS.items())
)
"""Cada enumeración con etiqueta de U-04 con el primer valor de su lista, para quitarlo."""


def _strip(unit: PlatformUnit, kind: str, name: str) -> PlatformUnit:
    """``unit`` igual, salvo que no registra ``name`` (también consumidores y validadores de texto
    libre, que U-03 no exige)."""
    if kind == "free_text_validator":
        return _strip_free_text(unit, name)
    if kind == "event_type" and name not in SUBSCRIBED:
        # ``notification_engine`` (TASK-324) se suscribe a los eventos de U-04 que notifican: sin
        # el evento se quita también su suscripción, para que el manifiesto nombre lo que falta
        # (la caída por la suscripción huérfana la fija ``export_requested``).
        return _without_subscription(_strip_u03(unit, kind, name), name)
    if kind != "consumer":
        return _strip_u03(unit, kind, name)
    original = unit.consumers

    def consumers(registry: ConsumerRegistry, services: UnitServices) -> None:
        full = ConsumerRegistry()
        original(full, services)
        for consumer in full.consumers():
            if consumer.consumer_name != name:
                registry.register(consumer)

    return dataclasses.replace(unit, consumers=consumers)


def _without_subscription(unit: PlatformUnit, event_name: str) -> PlatformUnit:
    """``unit`` con sus consumidores sin la suscripción a ``event_name``."""
    original = unit.consumers

    def consumers(registry: ConsumerRegistry, services: UnitServices) -> None:
        full = ConsumerRegistry()
        original(full, services)
        for consumer in full.consumers():
            events = tuple(event for event in consumer.subscribed_events if event != event_name)
            registry.register(dataclasses.replace(consumer, subscribed_events=events))

    return dataclasses.replace(unit, consumers=consumers)


def _strip_free_text(unit: PlatformUnit, name: str) -> PlatformUnit:
    """``unit`` sin el validador de texto libre ``name`` (ni su preparador)."""
    original = unit.free_text

    def free_text(registry: FreeTextPolicyRegistry) -> None:
        full = FreeTextPolicyRegistry()
        original(full)
        for validator_name, validator in full._validators.items():
            if validator_name == name:
                continue
            registry.register(validator_name, validator)
            preparer = full._preparers.get(validator_name)
            if preparer is not None:
                registry.register_preparer(validator_name, preparer)

    return dataclasses.replace(unit, free_text=free_text)


def _without(kind: str, name: str) -> tuple[PlatformUnit, ...]:
    return tuple(_strip(unit, kind, name) for unit in registered_units())


def _compose_admin(runtime: RuntimeConfig, units: Sequence[PlatformUnit]) -> Any:
    return asyncio.run(
        compose_admin_runtime(
            _admin_config(), PROVIDER_ID, runtime, units=units, reader=FakeReader()
        )
    )


# --- con todo registrado ------------------------------------------------------------------------


def test_the_three_processes_compose_and_list_u04_besides_u02_and_u03(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    units = registered_units()
    worker = _compose_worker(runtime, units)
    api = _compose_api(runtime, units)
    admin = _compose_admin(runtime, units)
    assert admin is not None
    create_app(_app_config(), runtime=api)  # etiquetas completas: la aplicación se construye
    by_unit: dict[ActorUnit, set[str]] = {}
    for compiled in record_type_registry(units).latest():
        by_unit.setdefault(compiled.writer_unit, set()).add(compiled.record_type)
    assert by_unit[ActorUnit.U04] == U04_RECORD_TYPES
    assert len(by_unit[ActorUnit.U02]) == 14 and len(by_unit[ActorUnit.U03]) == 26
    vocabulary = record_type_registry(units).get("vocabulary_list_changed")
    assert vocabulary.chain_level is ChainLevel.ORGANIZATION
    published = {
        compiled.event_name
        for compiled in worker.catalog.event_types.compiled_types()
        if compiled.definition.publisher_unit is ActorUnit.U04
    }
    assert published == U04_EVENT_TYPES
    assert set(worker.catalog.event_types.event_names()) >= U03_EVENT_TYPES | U04_EVENT_TYPES
    assert free_text_registry(units).validator_names == (
        U03_FREE_TEXT_VALIDATOR,
        *sorted(U04_FREE_TEXT_VALIDATORS),
    )
    assert {"blocked_vocabulary", "person_attribution"} == U04_FREE_TEXT_VALIDATORS


# --- sin una pieza --------------------------------------------------------------------------------


@pytest.mark.parametrize(("kind", "name"), MISSING, ids=[f"{k}:{n}" for k, n in MISSING])
def test_composition_fails_naming_exactly_what_is_missing(
    kind: str, name: str, tmp_path: Path
) -> None:
    units = _without(kind, name)
    runtime = _runtime(tmp_path)
    for compose in (_compose_worker, _compose_api, _compose_admin):
        if kind == "event_type" and name in SUBSCRIBED:
            # Un evento al que se suscribe un consumidor registrado: el catálogo ya se niega a
            # componerse antes del manifiesto (también fallo cerrado), y nombra los dos.
            with pytest.raises(OutboxStartupError) as refused:
                compose(runtime, units)
            assert name in str(refused.value) and "export_worker" in str(refused.value)
            continue
        with pytest.raises(RegistrationIncomplete) as raised:
            compose(runtime, units)
        assert raised.value.missing == ((kind, name),)


@pytest.mark.parametrize(("kind", "name"), ONE_OF_EACH)
def test_vigia_worker_does_not_start_and_names_what_is_missing(
    kind: str, name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path)
    units = _without(kind, name)

    async def builder(config: WorkerConfig) -> WorkerRuntime:
        return await compose_worker_runtime(config, runtime, units=units, reader=FakeReader())

    stderr = io.StringIO()
    monkeypatch.setattr("sys.stderr", stderr)
    with redirect_stderr(stderr):
        code = asyncio.run(worker_main.serve(_worker_config(), builder))
    assert code == STARTUP_FAILURE_EXIT_CODE
    assert _registration_line(stderr.getvalue())["missing"] == [{"kind": kind, "name": name}]


@pytest.mark.parametrize(("kind", "name"), ONE_OF_EACH)
def test_vigia_api_does_not_start_and_names_what_is_missing(
    kind: str, name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path)
    units = _without(kind, name)

    async def builder(config: AppConfig) -> Any:
        return await compose_api_runtime(config, runtime, units=units, reader=FakeReader())

    stderr = io.StringIO()
    monkeypatch.setattr("sys.stderr", stderr)
    server = api_main.ApiServerConfig(host="127.0.0.1", port=_free_port())
    with redirect_stderr(stderr):
        code = asyncio.run(api_main.serve(_app_config(), server, builder))
    assert code == STARTUP_FAILURE_EXIT_CODE
    assert _registration_line(stderr.getvalue())["missing"] == [{"kind": kind, "name": name}]


# --- sin una etiqueta -----------------------------------------------------------------------------


def _labels_without(enumeration: str, value: str, directory: Path) -> Path:
    document = json.loads(DEFAULT_LOOP_LABELS_PATH.read_text(encoding="utf-8"))
    del document[enumeration][value]
    path = directory / "labels.loop.es.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def _expected_problem(enumeration: str, value: str) -> str:
    if len(U04_BINDINGS[enumeration]) == 1:
        return f"la enumeración «{enumeration}» no tiene valores"
    return f"el valor «{value}» de «{enumeration}» no tiene etiqueta en español"


@pytest.mark.parametrize(("enumeration", "value"), LABELLED, ids=[f"{e}:{v}" for e, v in LABELLED])
def test_vigia_api_is_not_built_without_a_u04_label(
    enumeration: str, value: str, tmp_path: Path
) -> None:
    runtime = _compose_api(_runtime(tmp_path), registered_units())
    config = _app_config().model_copy(
        update={"extension_labels_paths": (_labels_without(enumeration, value, tmp_path),)}
    )
    with pytest.raises(ApiStartupError) as raised:
        create_app(config, runtime=runtime)
    assert raised.value.problems == (_expected_problem(enumeration, value),)


def test_vigia_api_does_not_start_without_a_u04_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path)
    labels = _labels_without("finding_lifecycle_state", "unclosable", tmp_path)
    config = _app_config().model_copy(update={"extension_labels_paths": (labels,)})

    async def builder(config: AppConfig) -> Any:
        return await compose_api_runtime(
            config, runtime, units=registered_units(), reader=FakeReader()
        )

    stderr = io.StringIO()
    monkeypatch.setattr("sys.stderr", stderr)
    server = api_main.ApiServerConfig(host="127.0.0.1", port=_free_port())
    with redirect_stderr(stderr):
        code = asyncio.run(api_main.serve(config, server, builder))
    assert code == STARTUP_FAILURE_EXIT_CODE
