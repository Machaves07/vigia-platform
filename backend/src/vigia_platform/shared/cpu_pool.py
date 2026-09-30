"""Pool de hilos acotado para el trabajo de CPU, fuera del bucle de eventos (PAT-NUC-REN-05).

La canonicalización de documentos grandes (LC-NUC-09), Argon2id, el SHA-256 de cuerpos grandes,
la verificación Ed25519 en lote y el descifrado de sobre no corren en el bucle de eventos: se
envían a **un** pool de 4 hilos por proceso ``[objetivo propio]``, compartido por la API y el
worker. El tope evita que una ráfaga de trabajo de CPU acapare el intérprete; el precio es la
cola, y por eso cada tarea registra cuánto esperó antes de empezar en ``cpu_pool_wait_ms``
(histograma de ``shared.observability.metrics``), que avisa de la saturación antes de que se
note en las rutas.

- ``CpuPool.run(function, *args, **kwargs)`` es el equivalente de ``asyncio.to_thread`` sobre el
  pool acotado: copia las variables de contexto (trazas, correlación) al hilo y devuelve el
  resultado o relanza la excepción de ``function``. Cancelar la espera no detiene la función ya
  iniciada (como ``asyncio.to_thread``). La cancelación llega al pool en una vuelta posterior del
  bucle de eventos: si para entonces la tarea sigue en cola, no se ejecuta; si un hilo quedó libre
  y la tomó antes, se ejecuta hasta el final y su resultado se descarta.
- ``get_cpu_pool()`` devuelve el pool compartido del proceso, con ``SystemClock`` y las métricas
  globales; las pruebas construyen el suyo con ``SimulatedClock`` o sus propias métricas.

La espera se mide con el ``Clock`` inyectado (``monotonic``), nunca con la hora del sistema
(PAT-NUC-RES-07).
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Final

from vigia_platform.shared.clock import Clock, SystemClock
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics

__all__ = ["CPU_POOL_MAX_WORKERS", "CPU_POOL_THREAD_PREFIX", "CpuPool", "get_cpu_pool"]

CPU_POOL_MAX_WORKERS: Final = 4
"""Hilos del pool de CPU por proceso (PAT-NUC-REN-05) ``[objetivo propio]``."""

CPU_POOL_THREAD_PREFIX: Final = "vigia-cpu"
"""Prefijo del nombre de los hilos del pool (visible en volcados de hilos y trazas)."""


class CpuPool:
    """Pool de hilos acotado con la espera en cola medida en ``cpu_pool_wait_ms``."""

    def __init__(
        self,
        clock: Clock,
        *,
        max_workers: int = CPU_POOL_MAX_WORKERS,
        metrics: PlatformMetrics | None = None,
    ) -> None:
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
            raise ValueError("el pool de CPU necesita al menos un hilo")
        self._clock = clock
        self._metrics = metrics
        self._max_workers = max_workers
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix=CPU_POOL_THREAD_PREFIX
        )

    @property
    def max_workers(self) -> int:
        return self._max_workers

    async def run[**P, T](
        self, function: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs
    ) -> T:
        """Ejecuta ``function(*args, **kwargs)`` en el pool y espera su resultado.

        Cancelar la espera no detiene ``function`` si ya empezó. Una tarea todavía en cola no se
        ejecuta si la cancelación llega al pool antes de que un hilo la tome; esa cancelación se
        propaga una vuelta del bucle después de ``Task.cancel()``, y en ese intervalo un hilo
        recién liberado puede tomarla y ejecutarla (mismo comportamiento que ``run_in_executor``).
        """
        loop = asyncio.get_running_loop()
        context = contextvars.copy_context()
        call = functools.partial(function, *args, **kwargs)
        queued_at = self._clock.monotonic()

        def task() -> T:
            self._record_wait(queued_at)
            return context.run(call)

        return await loop.run_in_executor(self._executor, task)

    def _record_wait(self, queued_at: float) -> None:
        waited_ms = max(0.0, (self._clock.monotonic() - queued_at) * 1000.0)
        metrics = self._metrics if self._metrics is not None else get_metrics()
        metrics.cpu_pool_wait_ms.record(waited_ms)

    def shutdown(self, *, wait: bool = True) -> None:
        """Cierra el pool: las tareas en cola que no empezaron se cancelan."""
        self._executor.shutdown(wait=wait, cancel_futures=True)


@functools.cache
def get_cpu_pool() -> CpuPool:
    """Pool de CPU compartido del proceso (4 hilos, ``SystemClock``, métricas globales)."""
    return CpuPool(SystemClock())
