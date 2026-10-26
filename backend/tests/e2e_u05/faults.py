"""Canal de control del intermediario de fallos del mundo de U-05 (VIG-334, FS-APP-01).

El mundo interpone ``tests.fault_proxy.FaultProxy`` entre el balanceador de la aplicación y la API,
y Playwright cambia su modo en caliente con ``POST <url>/mode/<forward|refuse|freeze>``. El canal
escucha solo en ``127.0.0.1``, no lleva credenciales ni datos y solo acepta esos tres modos.

Con ``rewind_exports``, ``POST <url>/exports/rewind`` saca de la ventana del límite de exportación
(PAT-LAZ-ESC-02: 5 por usuario y 20 por organización en una hora) las solicitudes ya hechas, como si
hubiera pasado la hora (VIG-356). El límite de producción no cambia: los seis proyectos de
Playwright comparten el mundo y la persona coordinadora, y cada escenario FS-APP empieza con el cupo
de una hora nueva.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.fault_proxy import FaultProxy, ProxyMode

__all__ = ["fault_control"]

_PREFIX = "/mode/"
_REWIND_EXPORTS = "/exports/rewind"


@contextlib.contextmanager
def fault_control(
    proxy: FaultProxy, *, rewind_exports: Callable[[], None] | None = None
) -> Iterator[str]:
    """URL local del canal que cambia el modo de ``proxy``; se cierra al salir."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path == _REWIND_EXPORTS and rewind_exports is not None:
                try:
                    rewind_exports()
                except Exception:
                    # Sin el mensaje: podría llevar datos de la base del mundo.
                    self.send_response(500)
                else:
                    self.send_response(204)
            else:
                mode = self.path.removeprefix(_PREFIX) if self.path.startswith(_PREFIX) else ""
                try:
                    proxy.set_mode(ProxyMode(mode))
                except ValueError:
                    self.send_response(400)
                else:
                    self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
