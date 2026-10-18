"""Mamparos por consumidor de U-04 dentro de ``vigia-worker`` (LC-LAZ-40; PAT-LAZ-ESC-01).

Que un trabajo largo (una exportación de una hora, una espera de 60 s al proveedor) no desplace a
uno urgente (la notificación de un vencido) en el mismo proceso. Cada consumidor de U-04 tiene un
**semáforo en memoria del proceso** con su concurrencia declarada `[objetivos propios, a ajustar
con la medición de NFR-LAZ-49]`:

======================== ==============
consumidor               concurrencia
======================== ==============
``notification_engine``  4
``loop_projection``      4
``review_queue``         2
``assistant_worker``     2
``export_worker``        1
``email_delivery_events`` 2
======================== ==============

Las cifras van en la configuración (``VIGIA_LOOP_BULKHEADS``, ``RuntimeConfig.loop_bulkheads``);
estas son las del diseño cuando la variable no las cambia.

**Cómo se aplica.** El registro de cada módulo de U-04 (``loop``, ``accreditation`` y
``notifications``) entrega a sus registradores ``bulkheaded_registry(registry, services)`` en vez
del registro del catálogo: cada ``Consumer`` que registran sale con su ``admission``, así que los
consumidores **no conocen el mamparo**. El despachador (``shared.outbox.dispatcher``) pide puesto
**antes** de abrir la transacción y de tomar el bloqueo consultivo de la partición: en su tope,
la ronda termina con ``Outcome.SATURATED``, la partición queda para el siguiente ciclo (o para
otro proceso) y la entrega **no consume intentos**. Un consumidor en su tope no bloquea a los
demás: cada uno tiene su propio semáforo.

El semáforo es un contador del bucle de eventos: ``try_enter`` y ``leave`` no ceden el control,
así que comprobar y ocupar es atómico entre corrutinas del mismo proceso. Métricas por consumidor:
``loop_bulkhead_in_use``, ``loop_bulkhead_size`` y ``loop_bulkhead_saturated_total``.
"""

from __future__ import annotations

import dataclasses
import weakref
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from vigia_platform.loop.observability.metrics import LoopMetrics, get_loop_metrics
from vigia_platform.shared.outbox.registries import (
    Consumer,
    ConsumerRegistry,
    OutboxRegistrationRejected,
)
from vigia_platform.shared.runtime.config import RuntimeConfig, RuntimeConfigInvalid

if TYPE_CHECKING:
    from vigia_platform.shared.runtime.units import UnitServices

__all__ = [
    "BULKHEADS_VARIABLE",
    "DEFAULT_CONCURRENCY",
    "BulkheadedConsumerRegistry",
    "ConsumerBulkhead",
    "ConsumerBulkheads",
    "bulkhead_sizes",
    "bulkheaded_registry",
]

BULKHEADS_VARIABLE: Final = "VIGIA_LOOP_BULKHEADS"

DEFAULT_CONCURRENCY: Final[Mapping[str, int]] = {
    "notification_engine": 4,
    "loop_projection": 4,
    "review_queue": 2,
    "assistant_worker": 2,
    "export_worker": 1,
    "email_delivery_events": 2,
}
"""Concurrencia máxima por proceso de cada consumidor de U-04 `[objetivos propios]`."""


def bulkhead_sizes(config: RuntimeConfig | None) -> dict[str, int]:
    """Las concurrencias del diseño con lo que cambie ``VIGIA_LOOP_BULKHEADS``.

    Raises:
        RuntimeConfigInvalid: la variable nombra un consumidor que no es de U-04.
    """
    sizes = dict(DEFAULT_CONCURRENCY)
    overrides = config.loop_bulkheads if config is not None else None
    for name, size in (overrides or {}).items():
        if name not in DEFAULT_CONCURRENCY:
            raise RuntimeConfigInvalid(BULKHEADS_VARIABLE, "nombra un consumidor sin mamparo")
        sizes[name] = size
    return sizes


class ConsumerBulkhead:
    """El semáforo de un consumidor (``ConsumerAdmission`` del despachador)."""

    def __init__(self, consumer_name: str, size: int, metrics: LoopMetrics) -> None:
        if size < 1:
            raise ValueError("la concurrencia de un mamparo es al menos 1")
        self.consumer_name = consumer_name
        self.size = size
        self._in_use = 0
        self._metrics = metrics
        self._attributes = {"consumer": consumer_name}
        metrics.loop_bulkhead_size.set(size, self._attributes)
        metrics.loop_bulkhead_in_use.set(0, self._attributes)

    @property
    def in_use(self) -> int:
        return self._in_use

    def try_enter(self) -> bool:
        """Ocupa un puesto si queda; si no, cuenta la saturación y devuelve ``False``."""
        if self._in_use >= self.size:
            self._metrics.loop_bulkhead_saturated_total.add(1, self._attributes)
            return False
        self._in_use += 1
        self._metrics.loop_bulkhead_in_use.set(self._in_use, self._attributes)
        return True

    def leave(self) -> None:
        """Libera el puesto de una entrega terminada (con éxito o no)."""
        if self._in_use <= 0:
            raise RuntimeError("leave sin try_enter: el mamparo no tiene puestos ocupados")
        self._in_use -= 1
        self._metrics.loop_bulkhead_in_use.set(self._in_use, self._attributes)


class ConsumerBulkheads:
    """Los mamparos de los consumidores de U-04 de un proceso."""

    def __init__(self, sizes: Mapping[str, int], metrics: LoopMetrics | None = None) -> None:
        chosen = metrics if metrics is not None else get_loop_metrics()
        self._bulkheads = {
            name: ConsumerBulkhead(name, size, chosen) for name, size in sorted(sizes.items())
        }

    @property
    def sizes(self) -> dict[str, int]:
        return {name: bulkhead.size for name, bulkhead in self._bulkheads.items()}

    def get(self, consumer_name: str) -> ConsumerBulkhead | None:
        return self._bulkheads.get(consumer_name)

    def in_use(self, consumer_name: str) -> int:
        return self._bulkheads[consumer_name].in_use

    def wrap(self, consumer: Consumer) -> Consumer:
        """``consumer`` con su mamparo; sin mamparo declarado no se registra (fallo cerrado)."""
        bulkhead = self._bulkheads.get(consumer.consumer_name)
        if bulkhead is None:
            raise OutboxRegistrationRejected(
                "el consumidor",
                consumer.consumer_name,
                ["es de U-04 y no tiene mamparo declarado (LC-LAZ-40)"],
            )
        return dataclasses.replace(consumer, admission=bulkhead)


class BulkheadedConsumerRegistry(ConsumerRegistry):
    """El registro del catálogo visto por los registradores de U-04: ``register`` aplica el
    mamparo y delega; el resto lee del registro real."""

    def __init__(self, inner: ConsumerRegistry, bulkheads: ConsumerBulkheads) -> None:
        super().__init__()
        self._inner = inner
        self.bulkheads = bulkheads

    def register(self, consumer: Consumer) -> Consumer:
        declared: object = consumer
        if not isinstance(declared, Consumer):
            return self._inner.register(consumer)  # el registro real lo rechaza con su motivo
        return self._inner.register(self.bulkheads.wrap(declared))

    def get(self, consumer_name: str) -> Consumer | None:
        return self._inner.get(consumer_name)

    def consumers(self) -> tuple[Consumer, ...]:
        return self._inner.consumers()

    def subscribers(self, event_name: str) -> tuple[str, ...]:
        return self._inner.subscribers(event_name)

    def seal(self) -> None:
        self._inner.seal()

    @property
    def sealed(self) -> bool:
        return self._inner.sealed


_PROCESS_BULKHEADS: Final[weakref.WeakKeyDictionary[ConsumerRegistry, ConsumerBulkheads]] = (
    weakref.WeakKeyDictionary()
)
"""Un juego de mamparos por registro del catálogo (uno por proceso), compartido por los tres
módulos de U-04."""


def bulkheaded_registry(
    registry: ConsumerRegistry, services: UnitServices
) -> BulkheadedConsumerRegistry:
    """El registro que ``loop``, ``accreditation`` y ``notifications`` entregan a sus
    registradores de consumidores."""
    if isinstance(registry, BulkheadedConsumerRegistry):
        return registry
    bulkheads = _PROCESS_BULKHEADS.get(registry)
    if bulkheads is None:
        bulkheads = ConsumerBulkheads(bulkhead_sizes(services.config))
        _PROCESS_BULKHEADS[registry] = bulkheads
    return BulkheadedConsumerRegistry(registry, bulkheads)
