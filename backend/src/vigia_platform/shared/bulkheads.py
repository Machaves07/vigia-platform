"""Mamparos por clase de ruta dentro de ``vigia-api`` (LC-GOB-20; PAT-GOB-RES-01; NFR-GOB-19).

Ninguna ráfaga de nodos puede agotar la capacidad de las rutas de personas. Cada trabajador de
uvicorn tiene **un semáforo por clase de ruta** (``asyncio.Semaphore``): por defecto 50 peticiones
en curso, **35** ``node`` y **15** ``person`` (``VIGIA_BULKHEAD_NODE`` y ``VIGIA_BULKHEAD_PERSON``,
§6 de Infrastructure Design de U-03). El límite es por proceso, no global: es la fracción
declarada en PAT-GOB-ESC-05 y el riesgo R10.

- ``BulkheadSettings``: modelo estricto de los dos tamaños; rechaza una reserva de personas
  inferior al 30 % del total (``PERSON_RESERVE_PERCENT``) con un error que nombra
  ``VIGIA_BULKHEAD_PERSON``.
- Clase ``node`` sin puesto libre: rechazo **inmediato** ``BulkheadSaturated`` (hacia la interfaz,
  ``temporarily_unavailable`` con ``retry_after_seconds``), nunca ``rate_limited``: ese código
  queda para el cubo de fichas, para que su métrica siga midiendo abuso y no saturación
  (``tech-stack-decisions.md`` §2.4, nota del 2026-09-20). El nodo encola y reintenta.
- Clase ``person`` sin puesto libre: **espera hasta 2 s** (``PERSON_WAIT_SECONDS``) con el
  temporizador inyectado y, si no obtiene puesto, ``BulkheadSaturated``: una persona no reencola
  sola.
- El puesto se libera siempre al salir de ``Bulkheads.slot``: respuesta, excepción, cancelación o
  desconexión del cliente.

Métricas (atributo ``pool_class``, nunca un nodo): ``bulkhead_in_use`` y ``bulkhead_size``
(medidores que vigila la alarma ``bulkhead-person-saturated``), ``bulkhead_wait_ms`` (espera hasta
obtener puesto o ser rechazada) y ``bulkhead_rejected_total``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vigia_platform.shared.clock import Clock
from vigia_platform.shared.db import RouteClass
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics

__all__ = [
    "MAX_BULKHEAD_SIZE",
    "PERSON_RESERVE_PERCENT",
    "PERSON_VARIABLE",
    "PERSON_WAIT_SECONDS",
    "RETRY_AFTER_SECONDS",
    "BulkheadSaturated",
    "BulkheadSettings",
    "Bulkheads",
    "reserve_problem",
]

PERSON_RESERVE_PERCENT: Final = 30
"""Reserva mínima de puestos para personas, en % del total (NFR-GOB-19)."""
PERSON_WAIT_SECONDS: Final = 2.0
"""Espera máxima de una petición de persona sin puesto libre (PAT-GOB-RES-01)."""
RETRY_AFTER_SECONDS: Final = 5
"""``retry_after_seconds`` del rechazo del mamparo ``[objetivo propio]``.

Entero de 1 a 60. Es el transitorio por defecto de la plataforma (``DEFAULT_RETRY_AFTER_SECONDS``):
la saturación de un semáforo dura lo que tardan en terminar las peticiones en curso (cientos de
milisegundos por los p95 de NFR-GOB-03), pero 1 s haría que 100 nodos que vacían su cola volvieran
a llenar el semáforo en la misma ráfaga; 5 s la reparte sin retrasar la cola de forma apreciable.
"""
MAX_BULKHEAD_SIZE: Final = 1_000
"""Tope de cada semáforo (el mismo que admite ``RuntimeConfig``)."""
NODE_VARIABLE: Final = "VIGIA_BULKHEAD_NODE"
PERSON_VARIABLE: Final = "VIGIA_BULKHEAD_PERSON"

type Sleep = Callable[[float], Awaitable[None]]


def reserve_problem(node: int, person: int) -> str | None:
    """Por qué ``node`` y ``person`` no dejan a las personas su reserva, o ``None``.

    Aritmética entera: ``person / (node + person) >= 30 %`` sin redondeos.
    """
    if person * 100 < PERSON_RESERVE_PERCENT * (node + person):
        return (
            f"{PERSON_VARIABLE} deja a las personas menos del {PERSON_RESERVE_PERCENT} % de los "
            f"puestos del mamparo ({NODE_VARIABLE} + {PERSON_VARIABLE})"
        )
    return None


class BulkheadSettings(BaseModel):
    """Tamaños de los dos semáforos de un trabajador (modelo estricto e inmutable)."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    node: int = Field(default=35, ge=1, le=MAX_BULKHEAD_SIZE)
    person: int = Field(default=15, ge=1, le=MAX_BULKHEAD_SIZE)

    @model_validator(mode="after")
    def _person_reserve(self) -> Self:
        problem = reserve_problem(self.node, self.person)
        if problem is not None:
            raise ValueError(problem)
        return self

    @property
    def total(self) -> int:
        """Peticiones en curso por trabajador (50 con los valores del diseño)."""
        return self.node + self.person

    def size(self, route_class: RouteClass) -> int:
        return self.node if route_class is RouteClass.NODE else self.person


class BulkheadSaturated(Exception):
    """El semáforo de ``route_class`` no tiene puesto: transitorio.

    Hacia la interfaz es ``temporarily_unavailable`` con ``retry_after_seconds`` y la cabecera
    ``Retry-After`` (``shared.api.errors.translate``); el formato del contrato para la clase
    ``node`` lo instala ``node_api``.
    """

    def __init__(self, route_class: RouteClass, *, retry_after_seconds: int) -> None:
        super().__init__(f"mamparo saturado: {route_class.value}")
        self.route_class = route_class
        self.retry_after_seconds = retry_after_seconds


class Bulkheads:
    """Los dos semáforos de un trabajador de uvicorn, con sus métricas.

    ``sleep`` es el temporizador de la espera de las personas (``asyncio.sleep`` en producción;
    las pruebas inyectan uno que avanza con un ``SimulatedClock``); ``clock`` mide esa espera.
    """

    def __init__(
        self,
        settings: BulkheadSettings | None = None,
        *,
        clock: Clock,
        metrics: PlatformMetrics | None = None,
        sleep: Sleep = asyncio.sleep,
        retry_after_seconds: int = RETRY_AFTER_SECONDS,
        person_wait_seconds: float = PERSON_WAIT_SECONDS,
    ) -> None:
        if type(retry_after_seconds) is not int or not 1 <= retry_after_seconds <= 60:
            raise ValueError("retry_after_seconds del mamparo debe ser un entero de 1 a 60")
        self._settings = settings if settings is not None else BulkheadSettings()
        self._clock = clock
        self._metrics = metrics if metrics is not None else get_metrics()
        self._sleep = sleep
        self._retry_after = retry_after_seconds
        self._person_wait = person_wait_seconds
        self._semaphores = {
            route_class: asyncio.Semaphore(self._settings.size(route_class))
            for route_class in RouteClass
        }
        self._in_use = dict.fromkeys(RouteClass, 0)
        for route_class in RouteClass:
            attributes = self._attributes(route_class)
            self._metrics.bulkhead_size.set(self._settings.size(route_class), attributes)
            self._metrics.bulkhead_in_use.set(0, attributes)

    @property
    def settings(self) -> BulkheadSettings:
        return self._settings

    @property
    def retry_after_seconds(self) -> int:
        return self._retry_after

    def in_use(self, route_class: RouteClass) -> int:
        """Puestos de ``route_class`` ocupados ahora mismo en este trabajador."""
        return self._in_use[route_class]

    @contextlib.asynccontextmanager
    async def slot(self, route_class: RouteClass) -> AsyncIterator[None]:
        """Ocupa un puesto de ``route_class`` mientras dura el bloque.

        ``BulkheadSaturated`` si no lo obtiene (al instante los nodos; tras ``2 s`` las personas).
        """
        if not isinstance(route_class, RouteClass):
            raise TypeError("route_class debe ser RouteClass")
        await self._acquire(route_class)
        try:
            yield
        finally:
            self._release(route_class)

    # --- Interno ---------------------------------------------------------------------------------

    @staticmethod
    def _attributes(route_class: RouteClass) -> Mapping[str, str]:
        return {"pool_class": route_class.value}

    async def _acquire(self, route_class: RouteClass) -> None:
        semaphore = self._semaphores[route_class]
        started = self._clock.monotonic()
        acquired = False
        if not semaphore.locked():
            # Sin suspensión: con puesto libre, ``acquire`` toma el puesto en el acto.
            acquired = await semaphore.acquire()
        elif route_class is RouteClass.PERSON:
            acquired = await self._wait(semaphore)
        waited_ms = max(0.0, (self._clock.monotonic() - started) * 1000)
        attributes = self._attributes(route_class)
        self._metrics.bulkhead_wait_ms.record(waited_ms, attributes)
        if not acquired:
            self._metrics.bulkhead_rejected_total.add(1, attributes)
            raise BulkheadSaturated(route_class, retry_after_seconds=self._retry_after)
        self._in_use[route_class] += 1
        self._metrics.bulkhead_in_use.set(self._in_use[route_class], attributes)

    async def _wait(self, semaphore: asyncio.Semaphore) -> bool:
        """Espera un puesto hasta ``person_wait_seconds`` del temporizador inyectado.

        ``True`` si lo obtuvo. Si la petición se cancela mientras espera, no se queda con ninguno.
        """
        acquire = asyncio.ensure_future(semaphore.acquire())
        timer = asyncio.ensure_future(self._sleep(self._person_wait))
        try:
            await asyncio.wait((acquire, timer), return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            # Cancelada mientras esperaba: ni espera ni puesto.
            timer.cancel()
            await _abandon(semaphore, acquire)
            raise
        timer.cancel()
        if not acquire.done():
            await _abandon(semaphore, acquire)
            return False
        return not acquire.cancelled() and acquire.exception() is None

    def _release(self, route_class: RouteClass) -> None:
        self._in_use[route_class] -= 1
        self._semaphores[route_class].release()
        self._metrics.bulkhead_in_use.set(self._in_use[route_class], self._attributes(route_class))


async def _abandon(semaphore: asyncio.Semaphore, acquire: asyncio.Future[Any]) -> None:
    """Retira la espera ``acquire``; si aun así obtuvo el puesto, lo devuelve.

    ``Semaphore.acquire`` ya deshace su cuenta si lo despertaron y se cancela después.
    """
    if not acquire.done():
        acquire.cancel()
        await asyncio.wait((acquire,))
    if not acquire.cancelled() and acquire.exception() is None:
        semaphore.release()
