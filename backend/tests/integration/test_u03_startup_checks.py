"""Comprobaciones de arranque de U-03 (TASK-227; NFR-GOB-12, 20; BR-NUC-52; PAT-GOB-RES-03).

- **Registros completos**: ``vigia-api`` y ``vigia-worker`` construidos con la raíz real
  (``compose_api_runtime`` y ``compose_worker_runtime`` con las unidades del registro, quitando
  una sola cosa) no se componen si falta **cualquiera** de los 26 tipos de registro, de los 15
  eventos o de las 7 tareas de U-03 (``RegistrationIncomplete`` nombra exactamente lo que falta),
  ni si una tarea cambia de cadencia; y el proceso sale con ``STARTUP_FAILURE_EXIT_CODE`` y la
  línea ``registration_missing`` que lo nombra. Con todo registrado, cada evento de U-03 está en
  el ``EventTypeRegistry`` que compone la raíz (nota de la revisión de VIG-148), y los
  manifiestos de ``fleet.registration`` coinciden con los tipos y eventos del código.
- **Salud profunda con vigia-node-ca**: sin la clave (un doble de KMS que la niega, una clave que
  no es ECC P-256 o un KMS que no responde en 5 s) ni ``vigia-api`` ni ``vigia-worker`` quedan
  listos; si KMS cae **después** del arranque, ``/health/ready`` sigue en 200 y el worker sigue en
  marcha (FS-GOB-05). La comprobación que compone la raíz real usa la clave de
  ``VIGIA_NODE_CA_KEY_ARN`` contra KMS de LocalStack: pasa con una ECC P-256 y falla con una RSA o
  con una que no existe.

Reloj simulado en los arranques (el plazo de 60 s avanza con él). Solo datos generados.
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import socket
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any, Final, cast

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from tests.api_support import World
from tests.integration.conftest import (
    LOCALSTACK_ACCESS_KEY_ID,
    LOCALSTACK_SECRET_ACCESS_KEY,
    LocalStackEndpoint,
    PostgresEndpoint,
)
from tests.integration.test_registered_tasks import u02_catalog
from tests.runtime_support import FakeReader, breach_list, runtime_environ
from tests.worker_support import (
    StubKms,
    StubSigning,
    StubStorage,
    WorkerEnvironment,
    synchronize,
    worker_environment,
)
from vigia_platform.catalog.events import CATALOG_EVENT_TYPES
from vigia_platform.catalog.record_types import CATALOG_RECORD_TYPES
from vigia_platform.fleet.events import FLEET_EVENT_TYPES
from vigia_platform.fleet.record_types import FLEET_RECORD_TYPES
from vigia_platform.fleet.registration import U03_EVENT_TYPES, U03_RECORD_TYPES, U03_TASKS
from vigia_platform.ledger.registry import RecordTypeRegistry
from vigia_platform.shared.api import main as api_main
from vigia_platform.shared.api.app import STARTUP_FAILURE_EXIT_CODE, AppConfig, StartupSupervisor
from vigia_platform.shared.node_ca import NodeCaError
from vigia_platform.shared.outbox.dispatcher import Dispatcher
from vigia_platform.shared.outbox.publish import Outbox
from vigia_platform.shared.outbox.registries import (
    EventTypeRegistry,
    OutboxCatalog,
    PeriodicTaskRegistry,
    Schedule,
)
from vigia_platform.shared.runtime.api import compose_api_runtime
from vigia_platform.shared.runtime.config import RegistrationIncomplete, RuntimeConfig
from vigia_platform.shared.runtime.core import NODE_CA_CHECK_TIMEOUT_SECONDS, node_ca_check
from vigia_platform.shared.runtime.units import (
    PlatformUnit,
    UnitServices,
    record_type_registry,
    registered_units,
)
from vigia_platform.shared.runtime.worker import compose_worker_runtime
from vigia_platform.shared.secrets import KmsPort
from vigia_platform.shared.worker import main as worker_main
from vigia_platform.shared.worker.main import WorkerConfig, WorkerProcess, WorkerRuntime

pytestmark = pytest.mark.integration

STATIC: Final = Path(__file__).resolve().parents[1] / "fixtures" / "static"
ORIGIN: Final = "https://app.vigia.test"
NODE_CA_KEY: Final = "alias/vigia-node-ca"

MISSING: Final[tuple[tuple[str, str], ...]] = (
    *(("record_type", name) for name in sorted(U03_RECORD_TYPES)),
    *(("event_type", name) for name in sorted(U03_EVENT_TYPES)),
    *(("periodic_task", name) for name in sorted(U03_TASKS)),
)
"""Cada una de las 26 + 15 + 7 cosas que U-03 exige, para quitarla de una en una."""
ONE_OF_EACH: Final = (
    ("record_type", "walk_test_result"),
    ("event_type", "catalog_updated"),
    ("periodic_task", "expire_walk_test_sessions"),
)


# --- La raíz real sin una pieza -------------------------------------------------------------------


def _strip(unit: PlatformUnit, kind: str, name: str) -> PlatformUnit:
    """``unit`` igual, salvo que no registra ``name`` (de la clase ``kind``)."""
    if kind == "record_type":
        original_types = unit.record_types

        def record_types(registry: RecordTypeRegistry) -> None:
            full = RecordTypeRegistry()
            original_types(full)
            for compiled in full.all_versions():
                if compiled.record_type != name:
                    registry.register(compiled.definition)

        return dataclasses.replace(unit, record_types=record_types)
    if kind == "event_type":
        original_events = unit.event_types

        def event_types(registry: EventTypeRegistry) -> None:
            full = EventTypeRegistry()
            original_events(full)
            for compiled in full.compiled_types():
                if compiled.event_name != name:
                    registry.register(compiled.definition)

        return dataclasses.replace(unit, event_types=event_types)
    original_tasks = unit.periodic_tasks

    def periodic_tasks(registry: PeriodicTaskRegistry, services: UnitServices) -> None:
        full = PeriodicTaskRegistry()
        original_tasks(full, services)
        for task in full.tasks():
            if task.task_name != name:
                registry.register(
                    task.task_name,
                    task.schedule,
                    task.handler,
                    unit=task.unit,
                    iteration=task.iteration,
                )

    return dataclasses.replace(unit, periodic_tasks=periodic_tasks)


def _without(kind: str, name: str) -> tuple[PlatformUnit, ...]:
    return tuple(_strip(unit, kind, name) for unit in registered_units())


def _runtime(tmp_path: Path, **changes: str | None) -> RuntimeConfig:
    return RuntimeConfig.from_environ(
        runtime_environ(VIGIA_BREACH_LIST_PATH=str(breach_list(tmp_path)), **changes)
    )


def _app_config() -> AppConfig:
    return AppConfig(
        environment="test",
        data_key_id="alias/vigia-secrets",
        static_dir=STATIC,
        public_origin=ORIGIN,
    )


def _worker_config() -> WorkerConfig:
    return WorkerConfig(environment="test", data_key_id="alias/vigia-secrets")


def _compose_api(runtime: RuntimeConfig, units: Sequence[PlatformUnit]) -> Any:
    return asyncio.run(
        compose_api_runtime(_app_config(), runtime, units=units, reader=FakeReader())
    )


def _compose_worker(runtime: RuntimeConfig, units: Sequence[PlatformUnit]) -> Any:
    return asyncio.run(
        compose_worker_runtime(_worker_config(), runtime, units=units, reader=FakeReader())
    )


def test_the_manifests_match_the_types_and_events_of_the_code() -> None:
    assert len(U03_RECORD_TYPES) == 26 and len(U03_EVENT_TYPES) == 15 and len(U03_TASKS) == 7
    assert {
        definition.record_type for definition in (*CATALOG_RECORD_TYPES, *FLEET_RECORD_TYPES)
    } == U03_RECORD_TYPES
    assert {
        event.event_name for event in (*CATALOG_EVENT_TYPES, *FLEET_EVENT_TYPES)
    } == U03_EVENT_TYPES


def test_the_real_root_registers_every_u03_type_event_and_task(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    worker = _compose_worker(runtime, registered_units())
    api = _compose_api(runtime, registered_units())
    catalog = worker.catalog
    assert set(catalog.event_types.event_names()) >= U03_EVENT_TYPES
    assert set(U03_TASKS) <= {task.task_name for task in catalog.periodic_tasks.tasks()}
    assert set(record_type_registry(registered_units()).record_types()) >= U03_RECORD_TYPES
    assert api.node_ca is not None and worker.node_ca is not None


@pytest.mark.parametrize(("kind", "name"), MISSING, ids=[f"{k}:{n}" for k, n in MISSING])
def test_composition_fails_naming_exactly_what_is_missing(
    kind: str, name: str, tmp_path: Path
) -> None:
    units = _without(kind, name)
    runtime = _runtime(tmp_path)
    for compose in (_compose_worker, _compose_api):
        with pytest.raises(RegistrationIncomplete) as raised:
            compose(runtime, units)
        assert raised.value.missing == ((kind, name),)
        assert name in str(raised.value)


def test_a_u03_task_with_another_cadence_is_missing(tmp_path: Path) -> None:
    def slower(unit: PlatformUnit) -> PlatformUnit:
        original = unit.periodic_tasks

        def periodic_tasks(registry: PeriodicTaskRegistry, services: UnitServices) -> None:
            full = PeriodicTaskRegistry()
            original(full, services)
            for task in full.tasks():
                schedule = (
                    Schedule.every(120) if task.task_name == "detect_mute_nodes" else task.schedule
                )
                registry.register(
                    task.task_name, schedule, task.handler, unit=task.unit, iteration=task.iteration
                )

        return dataclasses.replace(unit, periodic_tasks=periodic_tasks)

    with pytest.raises(RegistrationIncomplete) as raised:
        _compose_worker(_runtime(tmp_path), tuple(slower(u) for u in registered_units()))
    assert raised.value.missing == (("periodic_task", "detect_mute_nodes"),)


def _registration_line(stderr: str) -> dict[str, Any]:
    lines = [json.loads(line) for line in stderr.splitlines() if line.startswith("{")]
    (line,) = [line for line in lines if line.get("error") == "registration_missing"]
    return line


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


# --- vigia-node-ca --------------------------------------------------------------------------------


def _public_der(key: Any) -> bytes:
    der: bytes = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return der


class NodeCaKms(StubKms):
    """KMS de prueba: la clave de datos pasa siempre; ``get_public_key`` según ``mode``."""

    def __init__(self, mode: str = "p256") -> None:
        self.mode = mode
        self.calls = 0

    async def get_public_key(self, key_id: str) -> bytes:
        self.calls += 1
        if self.mode == "deny":
            raise PermissionError("AccessDeniedException: kms:GetPublicKey")
        if self.mode == "hang":
            await asyncio.sleep(NODE_CA_CHECK_TIMEOUT_SECONDS * 4)
        if self.mode == "p384":
            return _public_der(ec.generate_private_key(ec.SECP384R1()))
        return _public_der(ec.generate_private_key(ec.SECP256R1()))

    async def sign(self, key_id: str, message: bytes) -> bytes:
        raise AssertionError("la comprobación de arranque no firma")


@pytest.mark.parametrize("mode", ["deny", "p384"])
def test_the_node_ca_check_fails_without_an_accessible_p256_key(mode: str) -> None:
    kms = NodeCaKms(mode)
    with pytest.raises((PermissionError, NodeCaError)):
        asyncio.run(node_ca_check(kms, NODE_CA_KEY)())
    assert kms.calls == 1


def test_the_node_ca_check_gives_up_after_five_seconds() -> None:
    """Mide un tope real (5 s, PAT-GOB-RES-03): KMS no responde y la comprobación falla."""
    with pytest.raises(TimeoutError):
        asyncio.run(node_ca_check(NodeCaKms("hang"), NODE_CA_KEY)())


def _supervisor(app: Any) -> StartupSupervisor:
    supervisor = app.state.vigia_readiness
    assert isinstance(supervisor, StartupSupervisor)
    return supervisor


def _until(predicate: Callable[[], bool], what: str) -> None:
    async def wait() -> None:
        async with asyncio.timeout(60):
            while not predicate():
                await asyncio.sleep(0.01)

    try:
        asyncio.run(wait())
    except TimeoutError:
        raise AssertionError(f"no se cumplió: {what}") from None


@pytest.mark.parametrize("mode", ["deny", "p384", "hang"])
def test_vigia_api_never_becomes_ready_without_the_node_ca_key(mode: str) -> None:
    world = World()
    kms = NodeCaKms(mode)
    # Con KMS colgado, cada intento gasta de verdad su tope de 5 s: un plazo de 10 s (dos o tres
    # intentos) basta para ver que nunca queda lista sin esperar los 60 s del diseño.
    app = world.app(
        runtime={"node_ca": node_ca_check(kms, NODE_CA_KEY)}, startup_deadline_seconds=10.0
    )
    with TestClient(app) as client:
        _until(lambda: world.exits != [], "el arranque terminó")
        assert world.exits == [STARTUP_FAILURE_EXIT_CODE]
        assert _supervisor(app).failed and not _supervisor(app).started
        assert client.get("/health/ready").status_code == 503
        assert client.get("/health/live").status_code == 200
    assert kms.calls >= 1


def test_vigia_api_stays_ready_when_kms_falls_after_startup() -> None:
    world = World()
    kms = NodeCaKms("p256")
    app = world.app(runtime={"node_ca": node_ca_check(kms, NODE_CA_KEY)})
    with TestClient(app) as client:
        _until(lambda: _supervisor(app).started, "el arranque terminó")
        assert client.get("/health/ready").status_code == 200
        calls = kms.calls
        kms.mode = "deny"  # vigia-node-ca cae
        world.kms.down = True  # y con ella todo KMS
        for _ in range(3):
            assert client.get("/health/ready").status_code == 200
    assert kms.calls == calls  # /health/ready no consulta KMS
    assert world.exits == []


# --- vigia-worker ---------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def environment(postgres_endpoint: PostgresEndpoint) -> Iterator[WorkerEnvironment]:
    with worker_environment(postgres_endpoint, "u03_startup") as env:
        yield env


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def _worker(environment: WorkerEnvironment, kms: NodeCaKms) -> tuple[WorkerProcess, int]:
    """El worker con el catálogo de la raíz real y la comprobación de ``vigia-node-ca``."""
    catalog: OutboxCatalog = u02_catalog(environment)
    database = environment.new_database()
    clock = environment.clock

    async def synchronize_catalog() -> None:
        await synchronize(database, catalog, clock)

    async def simulated_sleep(seconds: float) -> None:
        wall = clock.now()
        clock.advance(seconds)
        clock.set(wall)  # ninguna tarea vence: solo avanza el plazo de arranque
        await asyncio.sleep(0)

    port = _free_port()
    config = WorkerConfig(
        environment="test",
        data_key_id="alias/vigia-secrets",
        health_port=port,
        startup_deadline_seconds=60.0,
        startup_retry_seconds=5.0,
        scheduler_poll_seconds=3600.0,
        monitor_seconds=3600.0,
    )
    runtime = WorkerRuntime(
        clock=clock,
        database=database,
        storage=StubStorage(),
        signing=StubSigning(),
        kms=cast(KmsPort, kms),
        catalog=catalog,
        dispatcher=Dispatcher(
            database=database,
            catalog=catalog,
            outbox=Outbox(catalog, clock),
            contexts=environment.contexts,
            clock=clock,
        ),
        contexts=environment.contexts,
        registries=(synchronize_catalog,),
        node_ca=node_ca_check(kms, NODE_CA_KEY),
        owner=f"arranque-{uuid.uuid4().hex[:8]}",
        sleep=simulated_sleep,
    )
    return WorkerProcess(config, runtime), port


async def _boot(process: WorkerProcess, stop: asyncio.Event) -> asyncio.Task[int]:
    running = asyncio.create_task(process.run(stop))
    started = asyncio.create_task(process.started.wait())
    async with asyncio.timeout(120):
        await asyncio.wait({running, started}, return_when=asyncio.FIRST_COMPLETED)
    started.cancel()
    return running


@pytest.mark.parametrize("mode", ["deny", "p384"])
def test_vigia_worker_does_not_start_without_the_node_ca_key(
    environment: WorkerEnvironment, mode: str
) -> None:
    process, _ = _worker(environment, NodeCaKms(mode))

    async def scenario() -> int:
        stop = asyncio.Event()
        running = await _boot(process, stop)
        stop.set()  # si llegó a arrancar (el defecto), se para en orden y sale con 0
        async with asyncio.timeout(120):
            return await running

    assert environment.run(scenario()) == STARTUP_FAILURE_EXIT_CODE
    assert not process.started.is_set()


def test_vigia_worker_keeps_running_when_kms_falls_after_startup(
    environment: WorkerEnvironment,
) -> None:
    kms = NodeCaKms("p256")
    process, port = _worker(environment, kms)

    async def scenario() -> tuple[bool, int, int]:
        stop = asyncio.Event()
        running = await _boot(process, stop)
        started = process.started.is_set() and not running.done()
        kms.mode = "deny"
        await asyncio.sleep(0.5)  # el worker sigue con sus bucles; nada vuelve a mirar KMS
        async with httpx.AsyncClient(timeout=30.0) as client:
            live = await client.get(f"http://127.0.0.1:{port}/health/live")
        alive = not running.done()
        stop.set()
        async with asyncio.timeout(120):
            code = await running
        return started and alive, live.status_code, code

    up, live, code = environment.run(scenario())
    assert up and live == 200 and code == 0
    assert kms.calls == 1


# --- La comprobación que compone la raíz real, contra KMS de LocalStack ---------------------------


@pytest.fixture
def aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", LOCALSTACK_ACCESS_KEY_ID)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", LOCALSTACK_SECRET_ACCESS_KEY)


def _root_checks(
    tmp_path: Path, localstack: LocalStackEndpoint, key_id: str
) -> Mapping[str, Callable[[], Any]]:
    runtime = _runtime(
        tmp_path,
        VIGIA_AWS_ENDPOINT_URL=localstack.url,
        AWS_REGION=localstack.region,
        VIGIA_NODE_CA_KEY_ARN=key_id,
    )
    api = _compose_api(runtime, registered_units())
    worker = _compose_worker(runtime, registered_units())
    return {"api": api.node_ca, "worker": worker.node_ca}


@pytest.mark.usefixtures("aws_credentials")
def test_the_root_checks_the_configured_node_ca_key_in_kms(
    tmp_path: Path, localstack_endpoint: LocalStackEndpoint
) -> None:
    kms = localstack_endpoint.aws_client("kms")
    p256 = kms.create_key(KeyUsage="SIGN_VERIFY", KeySpec="ECC_NIST_P256")["KeyMetadata"]["KeyId"]
    rsa = kms.create_key(KeyUsage="SIGN_VERIFY", KeySpec="RSA_2048")["KeyMetadata"]["KeyId"]
    missing = str(uuid.uuid4())

    for check in _root_checks(tmp_path, localstack_endpoint, p256).values():
        asyncio.run(check())
    for key_id in (rsa, missing):
        for process, check in _root_checks(tmp_path, localstack_endpoint, key_id).items():
            with pytest.raises(Exception) as raised:
                asyncio.run(check())
            assert not isinstance(raised.value, AssertionError), process
