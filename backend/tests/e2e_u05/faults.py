"""Canal de control del intermediario de fallos del mundo de U-05 (VIG-334, FS-APP-01).

El mundo interpone ``tests.fault_proxy.FaultProxy`` entre el balanceador de la aplicación y la API,
y Playwright cambia su modo en caliente con ``POST <url>/mode/<forward|refuse|freeze>``. El canal
escucha solo en ``127.0.0.1``, no lleva credenciales ni datos y solo acepta esos tres modos.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.fault_proxy import FaultProxy, ProxyMode

__all__ = ["fault_control"]

_PREFIX = "/mode/"


@contextlib.contextmanager
def fault_control(proxy: FaultProxy) -> Iterator[str]:
    """URL local del canal que cambia el modo de ``proxy``; se cierra al salir."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
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
