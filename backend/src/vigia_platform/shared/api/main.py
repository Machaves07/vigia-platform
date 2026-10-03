"""Punto de entrada ``vigia-api`` (LC-NUC-19; PAT-NUC-RES-02; NFR-NUC-23; TASK-143).

El proceso por defecto de la imagen (``backend/Dockerfile``). Sirve con uvicorn la aplicación de
``create_app`` en ``0.0.0.0:VIGIA_API_PORT`` (8000, el puerto de los dos grupos de destino de
``infra/stacks/edge.py``):

1. **Configuración.** ``AppConfig.from_environ`` y ``ApiServerConfig.from_environ``; sin
   argumentos de línea de órdenes.
2. **Composición.** ``create_app`` no construye dependencias: las recibe en ``AppRuntime``. El
   proceso pide el ``AppRuntime`` al constructor que nombra ``VIGIA_API_RUNTIME``
   (``vigia_platform.<módulo>:<función>``, asíncrono, recibe la ``AppConfig``), igual que
   ``vigia-worker`` con ``VIGIA_WORKER_RUNTIME``. Sin él, o si la construcción falla
   (``ApiStartupError``: etiquetas, rutas sin declaración…), el proceso sale con
   ``STARTUP_FAILURE_EXIT_CODE`` sin abrir el puerto.
3. **Arranque supervisado.** ``/health/live`` responde 200 en cuanto el puerto está abierto; las
   comprobaciones de PAT-NUC-RES-02 corren en segundo plano y, si a los 60 s alguna sigue
   fallando, el proceso termina con ``STARTUP_FAILURE_EXIT_CODE`` (``shared.api.app``).
4. **Parada.** ``SIGTERM`` o ``SIGINT``: uvicorn deja de aceptar conexiones, termina las peticiones
   en curso durante ``graceful_shutdown_seconds`` y cierra el ciclo de vida de la aplicación.

**Cabeceras del balanceador** (nota de VIG-93, ``tests/abuse/test_n07_brute_force.py``). La
aplicación nunca lee ``X-Forwarded-For``: el límite por origen usa la dirección de la conexión.
Sin ``VIGIA_FORWARDED_ALLOW_IPS``, uvicorn tampoco: ninguna cabecera cambia esa dirección. Con
ella (redes separadas por espacios: las subredes del balanceador), uvicorn toma la dirección del
cliente de ``X-Forwarded-For`` **solo** en las conexiones que llegan desde esas redes. Una red que
lo admita todo (``*``, ``0.0.0.0/0``, ``::/0``) no se acepta: equivale a creer a cualquiera.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import ipaddress
import os
import re
import signal
import sys
import threading
from collections.abc import Awaitable, Callable, Generator, Mapping, Sequence
from typing import Any, Final

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, field_validator

from vigia_platform.shared.api.app import (
    STARTUP_FAILURE_EXIT_CODE,
    AppConfig,
    AppRuntime,
    create_app,
)
from vigia_platform.shared.observability.logging import configure_logging, get_logger

__all__ = [
    "API_PORT",
    "FORWARDED_VARIABLE",
    "RUNTIME_VARIABLE",
    "ApiServerConfig",
    "build_server",
    "main",
    "resolve_runtime_builder",
    "serve",
]

_log = get_logger("shared.api.main")

API_PORT: Final = 8000
"""Puerto de ``vigia-api`` (``infra/stacks/edge.py``, ``API_PORT``)."""
ALL_INTERFACES: Final = "0.0.0.0"  # noqa: S104 - el contenedor escucha en su propia interfaz
RUNTIME_VARIABLE: Final = "VIGIA_API_RUNTIME"
FORWARDED_VARIABLE: Final = "VIGIA_FORWARDED_ALLOW_IPS"
PORT_VARIABLE: Final = "VIGIA_API_PORT"
MAX_FORWARDED_NETWORKS: Final = 16
GRACEFUL_SHUTDOWN_SECONDS: Final = 25.0
"""Espera de las peticiones en curso al parar: menos que los 30 s de ``stopTimeout`` por defecto
de Fargate ``[objetivo propio]``."""
_SIGNALS: Final = (signal.SIGINT, signal.SIGTERM)
_RUNTIME_REFERENCE: Final = re.compile(r"vigia_platform(?:\.[a-z_][a-z0-9_]*)+:[a-z_][a-z0-9_]*")
"""Solo un constructor del propio paquete: la variable no puede nombrar cualquier función."""

type RuntimeBuilder = Callable[[AppConfig], Awaitable[AppRuntime]]


class ApiServerConfig(BaseModel):
    """Configuración del servidor HTTP de ``vigia-api``: modelo estricto, inmutable."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    host: str = Field(default=ALL_INTERFACES, pattern=r"^(0\.0\.0\.0|127\.0\.0\.1)$")
    """La interfaz del contenedor: el balanceador llega a la tarea por su dirección privada."""
    port: int = Field(default=API_PORT, ge=1, le=65535)
    forwarded_allow_ips: tuple[str, ...] = Field(default=(), max_length=MAX_FORWARDED_NETWORKS)
    """Redes del balanceador cuyas cabeceras ``X-Forwarded-*`` se creen; vacío: ninguna."""
    graceful_shutdown_seconds: float = Field(default=GRACEFUL_SHUTDOWN_SECONDS, gt=0, le=115)

    @field_validator("forwarded_allow_ips")
    @classmethod
    def _networks(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        networks = []
        for value in values:
            if not value.isascii() or value.strip() != value:
                raise ValueError(f"{FORWARDED_VARIABLE}: red no válida")
            try:
                network = ipaddress.ip_network(value, strict=True)
            except ValueError:
                raise ValueError(f"{FORWARDED_VARIABLE}: red no válida") from None
            if network.prefixlen == 0:
                raise ValueError(f"{FORWARDED_VARIABLE}: una red que lo admite todo no vale")
            networks.append(str(network))
        if len(set(networks)) != len(networks):
            raise ValueError(f"{FORWARDED_VARIABLE} tiene redes repetidas")
        return tuple(networks)

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> ApiServerConfig:
        """Lee, si están, ``VIGIA_API_PORT`` y ``VIGIA_FORWARDED_ALLOW_IPS`` (separadas por
        espacios); ``ValueError`` si no son válidas."""
        values: dict[str, Any] = {}
        port = environ.get(PORT_VARIABLE)
        if port is not None:
            if not port.isascii() or not port.isdigit() or len(port) > 5:
                raise ValueError(f"{PORT_VARIABLE} debe ser un número de puerto")
            values["port"] = int(port)
        forwarded = environ.get(FORWARDED_VARIABLE)
        if forwarded is not None:
            values["forwarded_allow_ips"] = tuple(forwarded.split(" ")) if forwarded else ()
        return cls(**values)


def resolve_runtime_builder(reference: str | None) -> RuntimeBuilder:
    """El constructor de ``VIGIA_API_RUNTIME`` (``vigia_platform.<módulo>:<función>``)."""
    if reference is None or not _RUNTIME_REFERENCE.fullmatch(reference):
        raise ValueError(f"{RUNTIME_VARIABLE} debe ser «vigia_platform.<módulo>:<función>»")
    module_name, function_name = reference.split(":")
    builder = getattr(importlib.import_module(module_name), function_name, None)
    if not callable(builder):
        raise ValueError(f"{RUNTIME_VARIABLE} no nombra una función")
    resolved: RuntimeBuilder = builder
    return resolved


class _ApiServer(uvicorn.Server):
    """uvicorn con parada ordenada en ``SIGTERM`` y ``SIGINT`` que termina con 0.

    uvicorn vuelve a lanzar la señal capturada al terminar, y el proceso saldría con 143 (128 +
    ``SIGTERM``) tras una parada limpia. Aquí la señal solo pide la parada: el código de salida es
    el de ``serve``, como en ``vigia-worker``.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        previous = {number: signal.signal(number, self.handle_exit) for number in _SIGNALS}
        try:
            yield
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)


def build_server(app: FastAPI, server: ApiServerConfig) -> uvicorn.Server:
    """El servidor de uvicorn de ``vigia-api``, sin leer el entorno (``FORWARDED_ALLOW_IPS``)."""
    trusted = list(server.forwarded_allow_ips)
    return _ApiServer(
        uvicorn.Config(
            app,
            host=server.host,
            port=server.port,
            lifespan="on",
            proxy_headers=bool(trusted),
            # Con ``proxy_headers`` apagado no se usa; nunca se deja en su valor por defecto,
            # que uvicorn tomaría de la variable ``FORWARDED_ALLOW_IPS`` del proceso.
            forwarded_allow_ips=trusted or ["127.0.0.1/32"],
            server_header=False,
            log_config=None,
            access_log=False,
            timeout_graceful_shutdown=int(server.graceful_shutdown_seconds),
        )
    )


async def serve(
    config: AppConfig,
    server: ApiServerConfig,
    builder: RuntimeBuilder,
    *,
    stop: asyncio.Event | None = None,
) -> int:
    """Compone la aplicación y la sirve hasta la parada; devuelve el código de salida.

    ``stop`` (pruebas) pide la parada como lo haría ``SIGTERM``.
    """
    try:
        runtime = await builder(config)
        app = create_app(config, runtime=runtime)
    except Exception:
        _log.exception("la composición de vigia-api falló: el proceso no arranca")
        return STARTUP_FAILURE_EXIT_CODE
    uvicorn_server = build_server(app, server)

    async def watch() -> None:
        if stop is not None:
            await stop.wait()
            uvicorn_server.should_exit = True

    watcher = asyncio.create_task(watch())
    try:
        await uvicorn_server.serve()
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
    return 0 if uvicorn_server.started else STARTUP_FAILURE_EXIT_CODE


def main(argv: Sequence[str] | None = None) -> int:
    """``vigia-api``: sin argumentos; todo llega por el entorno."""
    configure_logging()
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        _log.critical("vigia-api no admite argumentos: se configura por el entorno")
        return 2
    try:
        config = AppConfig.from_environ(os.environ)
        server = ApiServerConfig.from_environ(os.environ)
        builder = resolve_runtime_builder(os.environ.get(RUNTIME_VARIABLE))
    except Exception:
        _log.exception("configuración de vigia-api no válida: el proceso no arranca")
        return STARTUP_FAILURE_EXIT_CODE
    return asyncio.run(serve(config, server, builder))


if __name__ == "__main__":  # pragma: no cover - punto de entrada
    sys.exit(main())
