"""Intervalos semiabiertos en UTC: la regla de la historia de compuertas (tech-stack §2.6).

``[start, end)`` con ``end = None`` como no acotado (el intervalo vigente). La regla vive aquí, en
dominio puro y verificable con Hypothesis; ``tstzrange`` y el índice GiST de ``gob_0017``
(``gate_state_no_overlap``) son apoyo y red de seguridad, nunca la sede de la regla
(PAT-GOB-REN-01):

- **contención** (``contains``): ``start <= t < end`` (la de ``state_at``, ``effective @> t``);
- **solapamiento** (``overlaps``): ``[a, b)`` y ``[c, d)`` comparten un instante si ``a < d`` y
  ``c < b`` (la de ``gate_history``, ``effective && [from, to)``); dos intervalos contiguos
  (``b = c``) no se solapan;
- **instante de relevo** (``handover_instant``): el cierre del intervalo abierto y la apertura del
  siguiente usan el mismo instante, que nunca es anterior al inicio del abierto ni a la última
  emisión; si el reloj inyectado no avanzó (o retrocedió), avanza un milisegundo, para que el
  intervalo que se cierra nunca quede vacío (``effective_until > effective_from``).

Todos los instantes llevan zona horaria y se normalizan a UTC con precisión de milisegundo (la del
``Timestamp`` del contrato). Nada lee la hora del sistema: el «ahora» llega del ``Clock``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

__all__ = [
    "MILLISECOND",
    "HalfOpenInterval",
    "containing",
    "handover_instant",
    "overlapping",
    "utc_instant",
]

MILLISECOND: Final = timedelta(milliseconds=1)


def utc_instant(moment: datetime) -> datetime:
    """``moment`` en UTC truncado al milisegundo; un instante sin zona horaria es un error."""
    if not isinstance(moment, datetime) or moment.utcoffset() is None:
        raise ValueError("el instante debe llevar zona horaria")
    moment = moment.astimezone(UTC)
    return moment.replace(microsecond=moment.microsecond // 1000 * 1000)


@dataclass(frozen=True, slots=True)
class HalfOpenInterval:
    """``[start, end)`` en UTC; ``end = None`` es no acotado. Nunca vacío ni invertido."""

    start: datetime
    end: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("start", "end"):
            value = getattr(self, name)
            if value is None and name == "end":
                continue
            if not isinstance(value, datetime) or value.utcoffset() is None:
                raise ValueError(f"{name} debe ser un instante con zona horaria")
            object.__setattr__(self, name, value.astimezone(UTC))
        if self.end is not None and not self.end > self.start:
            raise ValueError("un intervalo [start, end) exige end > start")

    @property
    def bounded(self) -> bool:
        return self.end is not None

    def contains(self, moment: datetime) -> bool:
        """``start <= moment < end``."""
        if not isinstance(moment, datetime) or moment.utcoffset() is None:
            raise ValueError("el instante debe llevar zona horaria")
        return self.start <= moment and (self.end is None or moment < self.end)

    def overlaps(self, other: HalfOpenInterval) -> bool:
        """¿Comparten algún instante? Los contiguos (``end == other.start``) no."""
        if not isinstance(other, HalfOpenInterval):
            raise TypeError("other debe ser HalfOpenInterval")
        before_other_ends = other.end is None or self.start < other.end
        other_before_self_ends = self.end is None or other.start < self.end
        return before_other_ends and other_before_self_ends

    def closed_at(self, end: datetime) -> HalfOpenInterval:
        """El mismo intervalo con su cota superior en ``end`` (el cierre al relevarse)."""
        if self.end is not None:
            raise ValueError("solo se cierra un intervalo abierto, una vez")
        return HalfOpenInterval(self.start, end)


def containing[T](
    items: Iterable[T], moment: datetime, window: Callable[[T], HalfOpenInterval]
) -> T | None:
    """El único elemento cuyo intervalo contiene ``moment``, o ``None``.

    Dos a la vez es una historia rota (solapada): ``ValueError`` en lugar de elegir uno.
    """
    found = [item for item in items if window(item).contains(moment)]
    if len(found) > 1:
        raise ValueError("historia con intervalos solapados")
    return found[0] if found else None


def overlapping[T](
    items: Iterable[T], query: HalfOpenInterval, window: Callable[[T], HalfOpenInterval]
) -> tuple[T, ...]:
    """Los elementos cuyo intervalo se solapa con ``query``, en el orden recibido."""
    return tuple(item for item in items if window(item).overlaps(query))


def handover_instant(
    now: datetime, *, open_start: datetime | None = None, last_issued: datetime | None = None
) -> datetime:
    """Instante del relevo: ``now`` al milisegundo, salvo que el reloj no haya avanzado.

    Nunca antes de ``last_issued`` (la última emisión del sobre, que puede coincidir) y siempre
    **estrictamente** después de ``open_start`` (el inicio del intervalo que se cierra): si no,
    un milisegundo más, para que el intervalo cerrado no quede vacío.
    """
    instant = utc_instant(now)
    if last_issued is not None:
        instant = max(instant, utc_instant(last_issued))
    if open_start is not None:
        instant = max(instant, utc_instant(open_start) + MILLISECOND)
    return instant
