"""Procesos de verdad para el arnés: API, worker, balanceador local y su ciclo de vida (LC-NUC-34).

- ``resilience_catalog``: el catálogo de la bandeja que comparten los procesos de API y de trabajo
  del arnés (un consumidor persistido sin registrar impide arrancar): los eventos y el consumidor
  de U-02, la tarea de prueba ``worker_probe`` (``tests.worker_support``) y el consumidor
  ``resilience_effect``, suscrito a ``worker_probe_effect``.
- ``EffectHandler``: el manejador de ``resilience_effect``. Su **efecto** es externo a la base
  (una línea por ``event_id`` en un archivo) e **idempotente por ``event_id``** (BR-NUC-76): si el
  evento ya tiene efecto, no lo repite. Anota cada entrega (con el proceso) en otro archivo.
  **Gancho de terminación**: con ``VIGIA_TEST_KILL_AFTER_EFFECT=1`` el primer proceso que produce
  un efecto se mata con ``SIGKILL`` justo después, antes de que el despachador confirme la
  entrega (FS-NUC-08). Solo existe en este proceso de prueba y solo con esa variable.
- ``Balancer``: balanceador TCP local de turno rotatorio entre los procesos de API, que solo
  enruta a los que responden 200 en ``/health/ready`` (como el balanceador de la nube) y cuenta
  las conexiones que llevó a cada uno (NFR-NUC-06).
- ``ProcessGroup``: lanza ``python -m <módulo>`` con su entorno, guarda su salida y, al salir, los
  para todos (``SIGTERM`` y, si no salen, ``SIGKILL``): nunca quedan procesos vivos.

Solo datos generados.
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import signal
import socket
import subprocess
import sys
import threading
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Final

import httpx

from tests.resilience.harness import BACKEND, free_port, wait_until
from tests.worker_support import EFFECT_EVENT, ProbeTask, worker_catalog
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.outbox.publish import OutboxEvent
from vigia_platform.shared.outbox.registries import Consumer, OutboxCatalog, Schedule

__all__ = [
    "EFFECT_CONSUMER",
    "KILL_AFTER_EFFECT_VARIABLE",
    "Balancer",
    "EffectHandler",
    "ProcessGroup",
    "read_lines",
    "resilience_catalog",
]

EFFECT_CONSUMER: Final = "resilience_effect"
KILL_AFTER_EFFECT_VARIABLE: Final = "VIGIA_TEST_KILL_AFTER_EFFECT"
EFFECT_LOG_VARIABLE: Final = "VIGIA_TEST_EFFECT_LOG"
DELIVERY_LOG_VARIABLE: Final = "VIGIA_TEST_DELIVERY_LOG"
KILL_MARK_VARIABLE: Final = "VIGIA_TEST_KILL_MARK"


def read_lines(path: Path) -> list[dict[str, Any]]:
    """Las líneas JSON de un registro de prueba (vacío si aún no existe)."""
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _append(path: str, entry: Mapping[str, Any]) -> None:
    with open(path, "a", encoding="utf-8") as log:
        log.write(json.dumps(dict(entry)) + "\n")
        log.flush()
        os.fsync(log.fileno())


@dataclass
class EffectHandler:
    """Manejador de ``resilience_effect``: efecto externo idempotente por ``event_id``."""

    owner: str
    environ: Mapping[str, str]

    async def __call__(self, event: OutboxEvent, transaction: Transaction) -> None:
        effect_log = self.environ.get(EFFECT_LOG_VARIABLE)
        delivery_log = self.environ.get(DELIVERY_LOG_VARIABLE)
        if not effect_log or not delivery_log:
            return  # el proceso de API registra el consumidor, pero nunca entrega
        event_id = str(event.event_id)
        _append(delivery_log, {"event_id": event_id, "owner": self.owner})
        done = {entry["event_id"] for entry in read_lines(Path(effect_log))}
        if event_id in done:
            return  # idempotente: la reentrega no repite el efecto (BR-NUC-76)
        _append(effect_log, {"event_id": event_id, "owner": self.owner})
        if self.environ.get(KILL_AFTER_EFFECT_VARIABLE) == "1":
            mark = Path(self.environ[KILL_MARK_VARIABLE])
            with contextlib.suppress(FileExistsError):
                # Solo el primero: el archivo se crea en exclusiva y el que lo crea muere.
                mark.open("x").close()
                os.kill(os.getpid(), signal.SIGKILL)


def resilience_catalog(
    probe: ProbeTask, effect: EffectHandler, schedule: Schedule | None = None
) -> OutboxCatalog:
    catalog = worker_catalog(probe, schedule)
    catalog.consumers.register(
        Consumer(
            consumer_name=EFFECT_CONSUMER,
            unit=ActorUnit.U02,
            subscribed_events=(EFFECT_EVENT,),
            handler=effect,
        )
    )
    return catalog


# --- Procesos ---------------------------------------------------------------------------------


@dataclass
class Spawned:
    name: str
    process: subprocess.Popen[bytes]
    output: IO[bytes]
    output_path: Path

    def tail(self, lines: int = 40) -> str:
        self.output.flush()
        text = self.output_path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])


@dataclass
class ProcessGroup:
    """Procesos de prueba con su salida en ``directory``; ``stop_all`` los para a todos."""

    directory: Path
    spawned: dict[str, Spawned] = field(default_factory=dict)

    def start(self, name: str, module: str, environ: Mapping[str, str]) -> Spawned:
        output_path = self.directory / f"{name}.out"
        output = output_path.open("wb")
        process = subprocess.Popen(
            [sys.executable, "-m", module],
            cwd=BACKEND,
            env={**os.environ, **environ},
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        spawned = Spawned(name, process, output, output_path)
        self.spawned[name] = spawned
        return spawned

    def stop(self, name: str, *, grace: float = 30.0) -> int:
        spawned = self.spawned[name]
        process = spawned.process
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(grace)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(10)
        return int(process.returncode)

    def stop_all(self) -> None:
        for name in list(self.spawned):
            with contextlib.suppress(Exception):
                self.stop(name, grace=15.0)
        for spawned in self.spawned.values():
            with contextlib.suppress(Exception):
                spawned.output.close()


@contextlib.contextmanager
def process_group(directory: Path) -> Iterator[ProcessGroup]:
    group = ProcessGroup(directory)
    try:
        yield group
    finally:
        group.stop_all()


WORKER_STARTED_MARK: Final = "worker en marcha"
"""Lo que registra ``shared.worker.main`` al terminar el arranque supervisado: catálogo
sincronizado y sellado, y bucles de despacho y planificador en marcha."""


def wait_worker_started(group: ProcessGroup, name: str, *, timeout: float = 90.0) -> None:
    """Espera el fin del arranque del worker ``name`` (no basta ``/health/live``).

    ``/health/live`` responde en cuanto arranca el servidor interno, antes de que el arranque
    sincronice el catálogo. Esa sincronización guarda ``next_run_at`` de las tareas nuevas: si la
    prueba fija antes ``shared.periodic_task``, el worker puede pisarlo.
    """
    spawned = group.spawned[name]

    def started() -> bool:
        if spawned.process.poll() is not None:
            raise AssertionError(f"el worker {name} terminó al arrancar: {spawned.tail()}")
        return WORKER_STARTED_MARK in spawned.tail(400)

    wait_until(started, timeout=timeout, message=f"el worker {name} no terminó de arrancar")


def wait_http(url: str, *, status: int = 200, timeout: float = 90.0, message: str = "") -> None:
    def probe() -> bool:
        try:
            return httpx.get(url, timeout=2.0).status_code == status
        except httpx.HTTPError:
            return False

    wait_until(probe, timeout=timeout, message=message or f"{url} no respondió {status}")


# --- Balanceador ------------------------------------------------------------------------------

_CHUNK: Final = 65_536
_POLL: Final = 0.05
HEALTH_INTERVAL_SECONDS: Final = 0.5


@dataclass
class Balancer:
    """Turno rotatorio por conexión entre ``backends`` sanos (``/health/ready`` en 200)."""

    backends: dict[str, int]
    """Nombre → puerto de cada proceso de API."""
    listener: socket.socket = field(init=False)
    served: Counter[str] = field(default_factory=Counter)
    healthy: set[str] = field(default_factory=set)
    _stop: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _turn: int = 0
    _threads: list[threading.Thread] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", free_port()))
        self.listener.listen(128)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.listener.getsockname()[1]}"

    def start(self) -> None:
        for target in (self._health_loop, self._accept_loop):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._threads.append(thread)

    def close(self) -> None:
        self._stop.set()
        with contextlib.suppress(OSError):
            self.listener.close()
        for thread in self._threads:
            thread.join(timeout=5)

    def _health_loop(self) -> None:
        while not self._stop.is_set():
            current = set()
            for name, port in self.backends.items():
                try:
                    response = httpx.get(f"http://127.0.0.1:{port}/health/ready", timeout=2.0)
                except httpx.HTTPError:
                    continue
                if response.status_code == 200:
                    current.add(name)
            with self._lock:
                self.healthy = current
            self._stop.wait(HEALTH_INTERVAL_SECONDS)

    def _choose(self) -> str | None:
        with self._lock:
            names = sorted(self.healthy)
            if not names:
                return None
            name = names[self._turn % len(names)]
            self._turn += 1
            self.served[name] += 1
            return name

    def _accept_loop(self) -> None:
        self.listener.settimeout(_POLL)
        while not self._stop.is_set():
            try:
                client, (client_host, _) = self.listener.accept()
            except (TimeoutError, OSError):
                continue
            name = self._choose()
            if name is None:
                with contextlib.suppress(OSError):
                    client.close()  # ningún proceso listo: el balanceador no enruta
                continue
            thread = threading.Thread(
                target=self._pipe, args=(client, client_host, self.backends[name]), daemon=True
            )
            thread.start()

    def _pipe(self, client: socket.socket, client_host: str, port: int) -> None:
        # Conserva la dirección del cliente (como el balanceador de la nube con la IP de origen):
        # el retardo de fallos por origen (BR-NUC-24) la ve. Toda 127.0.0.0/8 es local.
        upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            upstream.bind((client_host, 0))
            upstream.settimeout(5)
            upstream.connect(("127.0.0.1", port))
            upstream.settimeout(None)
        except OSError:
            for sock in (client, upstream):
                with contextlib.suppress(OSError):
                    sock.close()
            return
        pair = {client: upstream, upstream: client}
        try:
            while not self._stop.is_set():
                readable, _, _ = select.select(list(pair), [], [], _POLL)
                for sock in readable:
                    data = sock.recv(_CHUNK)
                    if not data:
                        return
                    pair[sock].sendall(data)
        except OSError:
            return
        finally:
            for sock in pair:
                with contextlib.suppress(OSError):
                    sock.close()


@contextlib.contextmanager
def balancer(backends: Mapping[str, int]) -> Iterator[Balancer]:
    running = Balancer(dict(backends))
    running.start()
    try:
        yield running
    finally:
        running.close()


def process_environment(**values: object) -> dict[str, str]:
    """Variables ``VIGIA_*`` como texto (las rutas y los identificadores, con ``str``)."""
    return {name: str(value) for name, value in values.items()}
