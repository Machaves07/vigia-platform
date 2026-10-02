"""Arnés de inyección de fallos de la plataforma (LC-NUC-34; PAT-NUC-RES-06; TASK-141).

Lo comparten los escenarios ``FS-NUC-01`` a ``FS-NUC-10`` (``test_fs_nuc_*.py``) y la suite con
dos procesos (``test_two_processes.py``):

- **Contenedores propios** (``dedicated_postgres``, ``dedicated_localstack``): PostgreSQL 16 y
  LocalStack con las mismas imágenes fijadas por digest que ``docker-compose.yml`` y
  ``tests/integration/conftest.py``, publicados en un puerto fijo de ``127.0.0.1`` para que un
  reinicio no cambie la dirección. ``Container`` los **pausa**, **reanuda**, **reinicia**,
  **detiene** y **arranca** con la API de Docker. Nunca se tocan los contenedores de la sesión,
  que comparten otras pruebas; los propios se eliminan al salir (también si la prueba falla).
- **Puntos concretos bloqueados**: ``tests.fault_proxy.FaultProxy`` delante de un servicio (p. ej.
  Secrets Manager y KMS de LocalStack por separado del almacén) rechaza o congela sus conexiones.
- **Ganchos de terminación de procesos**: solo existen en los procesos de prueba
  (``tests/resilience/worker_process.py``) y solo se activan con variables ``VIGIA_TEST_*``; el
  código de la plataforma no tiene ninguno.
- **Semilla y resultado esperado** (``scenario``): cada escenario declara su resultado esperado,
  deriva su ``random.Random`` de la semilla de la sesión (``--hypothesis-seed``, o una aleatoria
  que se imprime) y deja un **informe JSON** con la semilla, la inyección, lo esperado, lo
  observado, la duración y el resultado en ``VIGIA_RESILIENCE_REPORT_DIR`` (por defecto
  ``backend/reports/resilience``). ``nightly.yml`` lo conserva como artefacto 90 días. El resumen de
  pytest imprime una línea por escenario con su semilla (``tests/resilience/conftest.py``).

Solo datos generados (NFR-CTR-43). Docker es obligatorio: sin él la prueba falla, nunca pasa sin
ejecutar nada.
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import re
import socket
import time
import traceback
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any, Final

import pytest

import tests.conftest as root_conftest
from tests.integration.conftest import (
    LOCALSTACK_ENVIRONMENT,
    LOCALSTACK_IMAGE,
    LOCALSTACK_PORT,
    POSTGRES_DATABASE,
    POSTGRES_IMAGE,
    POSTGRES_PASSWORD,
    POSTGRES_PORT,
    POSTGRES_USER,
    LocalStackEndpoint,
    PostgresEndpoint,
    _localstack_services_running,
)
from vigia_platform.shared.clock import SystemClock

__all__ = [
    "BACKEND",
    "REPORT_DIR_VARIABLE",
    "Container",
    "ScenarioRun",
    "dedicated_localstack",
    "dedicated_postgres",
    "finished_scenarios",
    "free_port",
    "scenario",
    "wait_until",
]

BACKEND: Final = Path(__file__).resolve().parents[2]
REPORT_DIR_VARIABLE: Final = "VIGIA_RESILIENCE_REPORT_DIR"
DEFAULT_REPORT_DIR: Final = BACKEND / "reports" / "resilience"
LABEL: Final = "vigia.resilience"
"""Etiqueta de los contenedores del arnés (para encontrarlos si una sesión muere a medias)."""

READY_TIMEOUT_SECONDS: Final = 120.0
WALL: Final = SystemClock()
"""Reloj real: el arnés mide tiempos de verdad a propósito (tiempos de espera, RTO)."""


# --- Esperas ------------------------------------------------------------------------------------


def wait_until[T](
    probe: Callable[[], T], *, timeout: float, message: str, interval: float = 0.2
) -> T:
    """Repite ``probe`` hasta que devuelva algo verdadero; si vence ``timeout``, falla."""
    deadline = WALL.monotonic() + timeout
    while True:
        value = probe()
        if value:
            return value
        if WALL.monotonic() > deadline:
            raise AssertionError(f"{message} (tras {timeout:.0f} s)")
        time.sleep(interval)


def free_port() -> int:
    """Un puerto libre de ``127.0.0.1`` (lo toma enseguida quien lo pide)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def _tcp_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


# --- Contenedores -------------------------------------------------------------------------------


def _docker() -> Any:
    try:
        import docker  # type: ignore[import-untyped]

        client = docker.from_env()
        client.ping()
    except Exception as error:  # docker.errors.DockerException y fallos de conexión
        pytest.fail(
            "El arnés de resiliencia necesita Docker en ejecución. "
            f"Detalle: {type(error).__name__}: {error}",
            pytrace=False,
        )
    return client


@dataclass
class Container:
    """Un contenedor del arnés y las operaciones de inyección sobre él."""

    raw: Any
    host_port: int
    kind: str
    ready: Callable[[Container], bool]
    actions: list[tuple[str, float]] = field(default_factory=list)
    """Cada operación con el instante (monotónico) en que se pidió, para el informe."""

    def _note(self, action: str) -> None:
        self.actions.append((action, WALL.monotonic()))

    def pause(self) -> None:
        """Congela todos sus procesos: las conexiones abiertas quedan sin respuesta."""
        self._note("pause")
        self.raw.pause()

    def unpause(self) -> None:
        self._note("unpause")
        self.raw.unpause()

    def stop(self, timeout: int = 10) -> None:
        """Parada del servicio (``SIGINT`` en PostgreSQL: parada rápida)."""
        self._note("stop")
        self.raw.stop(timeout=timeout)

    def start(self) -> None:
        self._note("start")
        self.raw.start()

    def restart(self, timeout: int = 10) -> None:
        """Parada y arranque: el reinicio de una conmutación (las conexiones se cortan)."""
        self._note("restart")
        self.raw.restart(timeout=timeout)

    def exec(self, command: Sequence[str], *, user: str = "") -> tuple[int, str]:
        result = self.raw.exec_run(list(command), user=user, demux=False)
        output = result.output.decode("utf-8", "replace") if result.output else ""
        return int(result.exit_code), output

    def wait_ready(self, timeout: float = READY_TIMEOUT_SECONDS) -> None:
        wait_until(
            lambda: self.ready(self),
            timeout=timeout,
            message=f"el contenedor {self.kind} no quedó listo",
            interval=0.5,
        )

    def status(self) -> str:
        self.raw.reload()
        return str(self.raw.status)

    def remove(self) -> None:
        with contextlib.suppress(Exception):
            if self.status() == "paused":
                self.raw.unpause()
        with contextlib.suppress(Exception):
            self.raw.remove(force=True, v=True)


def _postgres_ready(container: Container) -> bool:
    if not _tcp_open(container.host_port):
        return False
    with contextlib.suppress(Exception):
        # Por TCP dentro del contenedor: durante la inicialización el servidor temporal solo
        # escucha en el socket local, así que esto no da por listo un servidor a medio iniciar.
        code, _ = container.exec(
            ["pg_isready", "-h", "127.0.0.1", "-U", POSTGRES_USER, "-d", POSTGRES_DATABASE]
        )
        return code == 0
    return False


def _localstack_ready(container: Container) -> bool:
    return _tcp_open(container.host_port) and _localstack_services_running(
        f"http://127.0.0.1:{container.host_port}"
    )


def _run(
    client: Any,
    image: str,
    *,
    kind: str,
    internal_port: int,
    environment: Mapping[str, str],
    command: Sequence[str] | None,
    ready: Callable[[Container], bool],
    volumes: Mapping[str, Mapping[str, str]] | None = None,
    start: bool = True,
) -> Container:
    port = free_port()
    options: dict[str, Any] = {
        "detach": True,
        "environment": dict(environment),
        "ports": {f"{internal_port}/tcp": ("127.0.0.1", port)},
        "labels": {LABEL: kind},
        "name": f"vigia-resilience-{kind}-{uuid.uuid4().hex[:10]}",
    }
    if command is not None:
        options["command"] = list(command)
    if volumes is not None:
        options["volumes"] = {key: dict(value) for key, value in volumes.items()}
    try:
        if start:
            raw = client.containers.run(image, **options)
        else:
            options.pop("detach")
            raw = client.containers.create(image, **options)
    except Exception as error:
        pytest.fail(
            f"No se pudo crear el contenedor {kind} del arnés: {type(error).__name__}: {error}",
            pytrace=False,
        )
    return Container(raw=raw, host_port=port, kind=kind, ready=ready)


POSTGRES_ENVIRONMENT: Final = {
    "POSTGRES_USER": POSTGRES_USER,
    "POSTGRES_PASSWORD": POSTGRES_PASSWORD,
    "POSTGRES_DB": POSTGRES_DATABASE,
    "TZ": "UTC",
    "PGTZ": "UTC",
}


def postgres_endpoint(container: Container) -> PostgresEndpoint:
    return PostgresEndpoint(
        host="127.0.0.1",
        port=container.host_port,
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
        database=POSTGRES_DATABASE,
    )


@contextlib.contextmanager
def dedicated_postgres(
    *, command: Sequence[str] | None = None
) -> Iterator[tuple[PostgresEndpoint, Container]]:
    """PostgreSQL 16 propio del escenario (la imagen de la sesión), con puerto de host fijo."""
    client = _docker()
    container = _run(
        client,
        POSTGRES_IMAGE,
        kind="postgres",
        internal_port=POSTGRES_PORT,
        environment=POSTGRES_ENVIRONMENT,
        command=command,
        ready=_postgres_ready,
    )
    try:
        container.wait_ready()
        yield postgres_endpoint(container), container
    finally:
        container.remove()


@contextlib.contextmanager
def restored_postgres(
    files: Sequence[tuple[str, bytes]],
    *,
    command: Sequence[str],
) -> Iterator[tuple[PostgresEndpoint, Container]]:
    """Un PostgreSQL **nuevo** cuyo directorio de datos se rellena antes de arrancar.

    ``files`` son archivos ``tar`` que se extraen en el contenedor creado y aún sin arrancar
    (``(ruta, tar)``). El punto de entrada de la imagen, como ``root``, devuelve el directorio de
    datos al usuario ``postgres`` y, al encontrar ``PG_VERSION``, no inicializa nada: arranca el
    servidor con ``command`` (p. ej. la recuperación hasta un punto con nombre).
    """
    client = _docker()
    container = _run(
        client,
        POSTGRES_IMAGE,
        kind="postgres-restored",
        internal_port=POSTGRES_PORT,
        environment=POSTGRES_ENVIRONMENT,
        command=command,
        ready=_postgres_ready,
        start=False,
    )
    try:
        for path, archive in files:
            if not container.raw.put_archive(path, archive):
                raise AssertionError(f"no se pudo copiar el respaldo en {path}")
        container.start()
        container.wait_ready()
        yield postgres_endpoint(container), container
    finally:
        container.remove()


@contextlib.contextmanager
def dedicated_localstack() -> Iterator[tuple[LocalStackEndpoint, Container]]:
    """LocalStack propio (S3, KMS y Secrets Manager), con puerto de host fijo."""
    client = _docker()
    container = _run(
        client,
        LOCALSTACK_IMAGE,
        kind="localstack",
        internal_port=LOCALSTACK_PORT,
        environment=LOCALSTACK_ENVIRONMENT,
        command=None,
        ready=_localstack_ready,
    )
    try:
        container.wait_ready()
        yield LocalStackEndpoint(url=f"http://127.0.0.1:{container.host_port}"), container
    finally:
        container.remove()


# --- Escenarios: semilla, resultado esperado e informe ----------------------------------------


@dataclass
class ScenarioRecord:
    """El informe de un escenario (una ejecución): lo que se conserva 90 días."""

    scenario_id: str
    title: str
    injection: str
    expected: str
    seed: str
    profile: str
    started_at: str
    duration_seconds: float = 0.0
    outcome: str = "running"
    observed: dict[str, Any] = field(default_factory=dict)
    failure: str | None = None
    report_path: str | None = None


_FINISHED: list[ScenarioRecord] = []


def finished_scenarios() -> list[ScenarioRecord]:
    """Los escenarios ya terminados en esta sesión (para el resumen de pytest)."""
    return list(_FINISHED)


def session_seed() -> int:
    """La semilla de la sesión: la de ``--hypothesis-seed`` o la aleatoria que eligió la sesión.

    Es la misma que imprime la cabecera de pytest, así que un escenario se repite con
    ``--hypothesis-seed=<semilla>``.
    """
    value = root_conftest._session_seed
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return int.from_bytes(str(value).encode("utf-8")[:8].ljust(8, b"\0"), "big")


@dataclass
class ScenarioRun:
    """Lo que el escenario usa mientras corre: su generador aleatorio y lo observado."""

    record: ScenarioRecord
    random: random.Random

    def child(self) -> random.Random:
        """Un generador propio para una pieza del escenario (un nodo, un escritor), derivado del
        del escenario: la misma semilla da la misma secuencia en cada pieza."""
        # S311: decide inyecciones y carga de prueba, nunca secretos.
        return random.Random(self.random.getrandbits(32))  # noqa: S311

    def observe(self, **values: Any) -> None:
        """Anota lo observado (valores JSON: números, textos, listas y objetos)."""
        self.record.observed.update(values)


def _report_dir() -> Path:
    raw = os.environ.get(REPORT_DIR_VARIABLE, "").strip()
    return Path(raw) if raw else DEFAULT_REPORT_DIR


def _json_default(value: object) -> str:
    return str(value)


def _write_report(record: ScenarioRecord) -> None:
    directory = _report_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = re.sub(r"[^0-9A-Za-z]", "", record.started_at)
    path = directory / f"{record.scenario_id}-{stamp}.json"
    record.report_path = str(path)
    path.write_text(
        json.dumps(asdict(record), ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )


@contextlib.contextmanager
def scenario(
    scenario_id: str, *, title: str, injection: str, expected: str
) -> Iterator[ScenarioRun]:
    """Corre un escenario con su semilla y deja su informe, pase o falle.

    El generador del escenario se deriva de la semilla de la sesión y del identificador, así que
    dos escenarios no comparten secuencia y cada uno se repite con la misma semilla.
    """
    if not re.fullmatch(r"FS-NUC-\d{2}[a-z]?|NFR-NUC-\d{2}", scenario_id):
        raise ValueError(f"identificador de escenario no válido: {scenario_id!r}")
    seed = session_seed()
    record = ScenarioRecord(
        scenario_id=scenario_id,
        title=title,
        injection=injection,
        expected=expected,
        seed=str(seed),
        profile=root_conftest._active_profile(),
        started_at=WALL.now().astimezone(UTC).isoformat(timespec="milliseconds"),
    )
    run = ScenarioRun(record, random.Random(f"{seed}:{scenario_id}"))  # noqa: S311 - ídem
    started = WALL.monotonic()
    # En la salida del escenario (``-s`` o el informe de un fallo) y en el resumen de la sesión.
    print(f"{scenario_id}: semilla {seed} (reproducir: --hypothesis-seed={seed})")
    try:
        yield run
    except BaseException as error:
        record.outcome = "failed"
        record.failure = "".join(traceback.format_exception_only(type(error), error)).strip()
        raise
    else:
        record.outcome = "passed"
    finally:
        record.duration_seconds = round(WALL.monotonic() - started, 3)
        _write_report(record)
        _FINISHED.append(record)
