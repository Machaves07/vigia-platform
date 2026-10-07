"""El balanceador local de TASK-230 en un proceso propio (TASK-231).

``tests.conformance.mtls_proxy.MtlsProxy`` termina TLS (y mTLS en ``nodes.``) en un hilo; en los
perfiles de carga corre aparte, como el balanceador real fuera de la tarea de ``vigia-api``: así el
saludo TLS de cien nodos y el reenvío de sus cuerpos no compiten por el GIL con el trabajador de
``vigia-api`` del proceso de la prueba ni con el cliente sintético de consola, que mide de extremo
a extremo.

``python -m tests.load.balancer --name N --tls-dir D --backend URL [--backend URL …]
[--client-ca F] [--preserve-host] --port-file F``: escribe su puerto en ``--port-file`` y atiende
hasta que se cierra su entrada estándar. ``balancer_process`` lo lanza y lo detiene.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from tests.conformance.mtls_proxy import ServerTls, mtls_proxy

__all__ = ["Balancer", "balancer_process", "main"]

START_SECONDS: Final = 60.0
STOP_SECONDS: Final = 30.0


@dataclass(frozen=True)
class Balancer:
    name: str
    port: int

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.port}"


@contextlib.contextmanager
def balancer_process(
    name: str,
    backends: Sequence[str],
    tls: ServerTls,
    directory: Path,
    *,
    client_ca: Path | None = None,
    preserve_host: bool = False,
) -> Iterator[Balancer]:
    """Lanza el balanceador ``name`` y espera su puerto (precondición, nunca decide nada)."""
    from tests.resilience.harness import WALL

    port_file = directory / f"balanceador-{name}.port"
    port_file.unlink(missing_ok=True)
    command = [
        sys.executable,
        "-m",
        "tests.load.balancer",
        "--name",
        name,
        "--ca",
        str(tls.ca_file),
        "--certificate",
        str(tls.certificate_file),
        "--key",
        str(tls.key_file),
        "--port-file",
        str(port_file),
    ]
    for backend in backends:
        command += ["--backend", backend]
    if client_ca is not None:
        command += ["--client-ca", str(client_ca)]
    if preserve_host:
        command.append("--preserve-host")
    backend_dir = Path(__file__).resolve().parents[2]
    environ = {**os.environ, "PYTHONPATH": str(backend_dir)}
    output = (directory / f"balanceador-{name}.out").open("wb")
    process = subprocess.Popen(
        command, cwd=backend_dir, env=environ, stdin=subprocess.PIPE, stdout=output, stderr=output
    )
    try:
        deadline = WALL.monotonic() + START_SECONDS
        while not port_file.is_file() or not port_file.read_text().strip():
            if process.poll() is not None:
                raise AssertionError(f"el balanceador {name} terminó al arrancar")
            if WALL.monotonic() > deadline:
                raise AssertionError(f"el balanceador {name} no arrancó en {START_SECONDS:.0f} s")
            time.sleep(0.2)
        yield Balancer(name, int(port_file.read_text().strip()))
    finally:
        if process.stdin is not None:
            with contextlib.suppress(OSError):
                process.stdin.close()
        try:
            process.wait(timeout=STOP_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=STOP_SECONDS)
        output.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.load.balancer")
    parser.add_argument("--name", required=True)
    parser.add_argument("--ca", type=Path, required=True)
    parser.add_argument("--certificate", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--backend", action="append", required=True)
    parser.add_argument("--client-ca", type=Path)
    parser.add_argument("--preserve-host", action="store_true")
    parser.add_argument("--port-file", type=Path, required=True)
    arguments = parser.parse_args(argv)
    tls = ServerTls(arguments.ca, arguments.certificate, arguments.key)
    with mtls_proxy(
        arguments.name,
        arguments.backend,
        tls,
        client_ca=arguments.client_ca,
        preserve_host=arguments.preserve_host,
    ) as proxy:
        arguments.port_file.write_text(str(proxy.port), encoding="utf-8")
        sys.stdin.read()  # hasta que el proceso de la prueba cierre la entrada
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
