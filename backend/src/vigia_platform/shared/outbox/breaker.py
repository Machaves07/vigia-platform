"""Cortacircuito por consumidor (LC-NUC-23; BR-NUC-79; PAT-NUC-RES-04).

``ConsumerBreaker`` es una máquina de estados **pura**: recibe el estado y la hora y devuelve el
estado siguiente o la acción del despachador. El estado vive en la tabla global
``shared.consumer`` (``circuit_state``, ``circuit_opened_at``, ``probe_interval_seconds``) para
que todos los procesos de trabajo lo vean; el despachador lo escribe con una actualización
condicionada al estado que leyó, así que de dos procesos que compiten solo uno gana.

- ``closed``: se entrega (``DELIVER``).
- ``open``: ninguna entrega de ese consumidor en ninguna partición (``PAUSE``), sin consumir
  intentos. Cumplido el intervalo de sonda (60 s, BR-NUC-79) desde ``changed_at``, la acción es
  ``PROBE``: el proceso que gana la reclamación pasa a ``half_open`` y hace **una única**
  entrega, la cabeza vencida más antigua.
- ``half_open``: la sonda está en curso; el resto pausa. Si el proceso de la sonda muere, al
  cumplirse otro intervalo desde la reclamación se puede reclamar otra.

Transiciones por el resultado de una entrega:

- ``ExternalDependencyDown`` (el manejador declara caída su dependencia): con el circuito
  cerrado, o en la sonda, abre con ``changed_at = ahora``. Una entrega que ya estaba en curso
  cuando otro abrió no aplaza la sonda.
- éxito de la sonda: cierra y todo se reanuda.
- cualquier otra excepción en la sonda: la dependencia respondió y el fallo es del manejador (un
  intento, BR-NUC-78), así que también cierra.

Solo los consumidores con ``has_external_dependency`` abren el circuito: en los demás,
``ExternalDependencyDown`` cuenta como un intento fallido más (un consumidor sin dependencia
externa declarada no puede pausarse a sí mismo).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from vigia_platform.shared.api.errors import ExternalDependencyDown

__all__ = [
    "PROBE_INTERVAL",
    "BreakerAction",
    "BreakerState",
    "CircuitState",
    "ConsumerBreaker",
    "ExternalDependencyDown",
]

PROBE_INTERVAL: Final = timedelta(seconds=60)
"""Intervalo de sonda de BR-NUC-79 (``probe_interval_seconds`` por defecto en la tabla)."""


class CircuitState(enum.StrEnum):
    """``shared.consumer.circuit_state``."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class BreakerAction(enum.StrEnum):
    """Qué hace el despachador con el consumidor en este instante."""

    DELIVER = "deliver"
    PROBE = "probe"
    PAUSE = "pause"


@dataclass(frozen=True, slots=True)
class BreakerState:
    """Una fila de ``shared.consumer`` vista por el cortacircuito.

    ``changed_at`` es ``circuit_opened_at``: cuándo se abrió o cuándo se reclamó la sonda en
    curso; nulo con el circuito cerrado.
    """

    state: CircuitState
    changed_at: datetime | None = None
    probe_interval: timedelta = PROBE_INTERVAL

    @classmethod
    def closed(cls, probe_interval: timedelta = PROBE_INTERVAL) -> BreakerState:
        return cls(CircuitState.CLOSED, None, probe_interval)


class ConsumerBreaker:
    """Las transiciones de PAT-NUC-RES-04, sin efectos."""

    __slots__ = ()

    @staticmethod
    def tick(current: BreakerState, now: datetime) -> BreakerAction:
        """``DELIVER`` cerrado; ``PROBE`` si toca sonda; ``PAUSE`` en el resto."""
        if current.state is CircuitState.CLOSED:
            return BreakerAction.DELIVER
        if current.changed_at is None or now >= current.changed_at + current.probe_interval:
            return BreakerAction.PROBE
        return BreakerAction.PAUSE

    @staticmethod
    def on_probe(current: BreakerState, now: datetime) -> BreakerState:
        """La sonda reclamada: ``half_open`` desde ``now``."""
        return BreakerState(CircuitState.HALF_OPEN, now, current.probe_interval)

    @staticmethod
    def on_dependency_failure(current: BreakerState, now: datetime, *, probe: bool) -> BreakerState:
        """``ExternalDependencyDown``: abre (o reabre tras la sonda) con ``changed_at = now``."""
        if probe or current.state is CircuitState.CLOSED:
            return BreakerState(CircuitState.OPEN, now, current.probe_interval)
        return current

    @staticmethod
    def on_success(current: BreakerState, *, probe: bool) -> BreakerState:
        """El éxito de la sonda cierra; fuera de la sonda no cambia nada."""
        if probe:
            return BreakerState.closed(current.probe_interval)
        return current

    @staticmethod
    def on_handler_defect(current: BreakerState, *, probe: bool) -> BreakerState:
        """Otro fallo en la sonda: la dependencia respondió, cierra; fuera de ella, nada."""
        if probe:
            return BreakerState.closed(current.probe_interval)
        return current
