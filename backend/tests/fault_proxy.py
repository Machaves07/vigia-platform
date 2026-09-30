"""Intermediario TCP con fallos conmutables para las pruebas de salud (TASK-133; NFR-NUC-13).

Se interpone entre la plataforma y una dependencia real (PostgreSQL o LocalStack) y, en caliente:

- ``FORWARD``: reenvía en ambos sentidos;
- ``REFUSE``: corta las conexiones abiertas y cierra enseguida las nuevas (servicio caído);
- ``FREEZE``: acepta y mantiene las conexiones pero no reenvía nada, tampoco en las ya abiertas
  (servicio congelado, como un contenedor en pausa: la petición queda sin respuesta).

Solo hilos y sockets locales; no toca los contenedores, que comparten otras pruebas.
"""

from __future__ import annotations

import contextlib
import enum
import select
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field

__all__ = ["FaultProxy", "ProxyMode", "fault_proxy"]

_CHUNK = 65_536
_POLL_SECONDS = 0.05


class ProxyMode(enum.Enum):
    FORWARD = "forward"
    REFUSE = "refuse"
    FREEZE = "freeze"


@dataclass
class FaultProxy:
    target: tuple[str, int]
    listener: socket.socket
    mode: ProxyMode = ProxyMode.FORWARD
    _stop: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _open: set[socket.socket] = field(default_factory=set)
    _threads: list[threading.Thread] = field(default_factory=list)

    @property
    def port(self) -> int:
        return int(self.listener.getsockname()[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def set_mode(self, mode: ProxyMode) -> None:
        with self._lock:
            self.mode = mode
            if mode is ProxyMode.REFUSE:
                for sock in list(self._open):
                    _close(sock)
                self._open.clear()

    def _track(self, *sockets: socket.socket) -> None:
        with self._lock:
            self._open.update(sockets)

    def _accept_loop(self) -> None:
        self.listener.settimeout(_POLL_SECONDS)
        while not self._stop.is_set():
            try:
                client, _ = self.listener.accept()
            except (TimeoutError, OSError):
                continue
            if self.mode is ProxyMode.REFUSE:
                _close(client)
                continue
            thread = threading.Thread(target=self._serve, args=(client,), daemon=True)
            self._threads.append(thread)
            thread.start()

    def _serve(self, client: socket.socket) -> None:
        self._track(client)
        upstream: socket.socket | None = None
        try:
            while upstream is None and not self._stop.is_set():
                if self.mode is ProxyMode.REFUSE:
                    return
                if self.mode is ProxyMode.FORWARD:
                    upstream = socket.create_connection(self.target, timeout=5)
                    self._track(upstream)
                else:
                    self._stop.wait(_POLL_SECONDS)
            if upstream is None:
                return
            pair = {client: upstream, upstream: client}
            while not self._stop.is_set():
                readable, _, _ = select.select(list(pair), [], [], _POLL_SECONDS)
                if self.mode is ProxyMode.REFUSE:
                    return
                if self.mode is ProxyMode.FREEZE:
                    # Nada sale ni entra: la dependencia no responde.
                    self._stop.wait(_POLL_SECONDS)
                    continue
                for sock in readable:
                    data = sock.recv(_CHUNK)
                    if not data:
                        return
                    pair[sock].sendall(data)
        except OSError:
            return
        finally:
            _close(client)
            if upstream is not None:
                _close(upstream)

    def close(self) -> None:
        self._stop.set()
        _close(self.listener)
        self.set_mode(ProxyMode.REFUSE)
        for thread in self._threads:
            thread.join(timeout=2)


def _close(sock: socket.socket) -> None:
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):
        sock.close()


@contextlib.contextmanager
def fault_proxy(host: str, port: int) -> Iterator[FaultProxy]:
    """Intermediario en un puerto local libre hacia ``host:port``; se cierra al salir."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(64)
    proxy = FaultProxy((host, port), listener)
    thread = threading.Thread(target=proxy._accept_loop, daemon=True)
    thread.start()
    try:
        yield proxy
    finally:
        proxy.close()
        thread.join(timeout=2)
