"""Política de reintento de la bandeja de salida (LC-NUC-23; BR-NUC-78; PR-NUC-31).

Pura: no lee el reloj ni el azar; el despachador le pasa el número de intentos fallidos y una
fracción aleatoria en ``[0, 1)``.

- El retraso tras el intento fallido ``k`` es ``min(600, 4^(k-1))`` segundos, es decir
  ``[1, 4, 16, 64, 256, 600, 600, 600][k - 1]``, con **variación acotada** de ±10 %
  ``[objetivo propio]``: así los reintentos de muchas entregas que fallaron a la vez no vuelven
  todos en el mismo instante. Se redondea a milisegundos (``next_attempt_at`` los guarda).
- Ocho intentos en total: tras el octavo fallo no hay reintento sino ``DeadLetter`` (cola muerta
  con ``dead_letter_created`` y alarma; la partición continúa). Entre el primero y el octavo se
  esperan siete retrasos, unos 26 minutos.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

__all__ = [
    "BACKOFF_SECONDS",
    "JITTER_RATIO",
    "MAX_ATTEMPTS",
    "DeadLetter",
    "Retry",
    "RetryPolicy",
]

BACKOFF_SECONDS: Final = (1, 4, 16, 64, 256, 600, 600, 600)
"""Retraso base tras el intento fallido ``k`` (``BACKOFF_SECONDS[k - 1]``), BR-NUC-78."""

MAX_ATTEMPTS: Final = len(BACKOFF_SECONDS)
"""El octavo fallo envía la entrega a la cola muerta."""

JITTER_RATIO: Final = 0.1
"""Variación máxima sobre el retraso base, en fracción (±10 %) ``[objetivo propio]``."""


@dataclass(frozen=True, slots=True)
class Retry:
    """Reintentar pasado ``delay``; ``attempts`` son los intentos fallidos hasta ahora."""

    attempts: int
    delay: timedelta


@dataclass(frozen=True, slots=True)
class DeadLetter:
    """Sin más intentos: a la cola muerta."""

    attempts: int


class RetryPolicy:
    """``after_failure(attempts, jitter)``: qué hacer con una entrega tras su fallo número k."""

    __slots__ = ()

    @staticmethod
    def base_delay_seconds(attempt: int) -> int:
        """``min(600, 4^(k-1))`` para ``k`` de 1 a 8 (PR-NUC-31)."""
        _check_attempts(attempt)
        return BACKOFF_SECONDS[attempt - 1]

    @classmethod
    def delay(cls, attempt: int, jitter: float) -> timedelta:
        """Retraso tras el intento fallido ``attempt``: base ± ``JITTER_RATIO``, en milisegundos."""
        if type(jitter) is not float or not math.isfinite(jitter) or not 0.0 <= jitter < 1.0:
            raise ValueError("jitter debe ser un float en [0, 1)")
        factor = 1.0 - JITTER_RATIO + 2.0 * JITTER_RATIO * jitter
        milliseconds = round(cls.base_delay_seconds(attempt) * 1000 * factor)
        return timedelta(milliseconds=milliseconds)

    @classmethod
    def after_failure(cls, attempts: int, jitter: float) -> Retry | DeadLetter:
        """``attempts`` es el número de intentos fallidos contando el que acaba de fallar."""
        _check_attempts(attempts)
        if attempts >= MAX_ATTEMPTS:
            return DeadLetter(attempts)
        return Retry(attempts, cls.delay(attempts, jitter))


def _check_attempts(attempts: int) -> None:
    if type(attempts) is not int or not 1 <= attempts <= MAX_ATTEMPTS:
        raise ValueError(f"attempts debe ser un entero de 1 a {MAX_ATTEMPTS}")
