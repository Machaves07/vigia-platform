"""Límites de tasa generales y aproximados: cubo de fichas por proceso (LC-NUC-22; PAT-NUC-ESC-03).

Tercer mecanismo de PAT-NUC-ESC-03 (los dos exactos, el retardo del inicio de sesión y los
tokens de vista en vivo, viven en PostgreSQL): un cubo de fichas **por proceso**, con el
presupuesto completo y sin coordinación entre procesos. Es aproximado a propósito: con N procesos
detrás del balanceador, un cliente puede obtener hasta N veces el presupuesto (NFR-NUC-25 lo
documenta así). La interfaz ``RateLimiter.check(key, budget) -> Allowed | Limited`` es fija: el día
que la medición lo justifique se sustituye por un contador compartido sin tocar las rutas.

**Presupuestos** (NFR-NUC-25, ``[objetivos propios]``): 600 peticiones por minuto por sesión
(``SESSION_BUDGET``), 1 200 por origen en rutas autenticadas (``AUTHENTICATED_ORIGIN_BUDGET``) y
60 por origen en rutas públicas (``PUBLIC_ORIGIN_BUDGET``). La ráfaga de cada uno es igual a su
límite: en cualquier ventana de un minuto un proceso acepta al menos el límite (si se le pide) y
nunca más del límite más la ráfaga (PR-NUC-44).

**Claves** (lista cerrada, ``KEY_PATTERN``): ``session:<sha256>``, ``origin:<hmac>``,
``public:<hmac>`` y, para U-03 (pendiente nº 37), ``node:<node_id>:<operación>``; además (TASK-206)
``enrollment:<hmac>:<ventana>`` (el alta por origen, con una clave por ventana: ``quarter`` de
15 minutos y ``day``) y ``brake:node`` (el freno global de emergencia de las rutas del contrato).
El origen de red nunca se guarda en claro: ``origin_key`` lo reduce a un HMAC-SHA256 con una clave
aleatoria del proceso, así que ni un volcado de memoria contiene direcciones.

**Aritmética entera**: el crédito de un cubo se cuenta en «fichas x nanosegundos de ventana»;
cada nanosegundo suma ``limit`` unidades y una petición cuesta ``window_ns``. Así no hay errores
de coma flotante que dejen pasar una petición de más o de menos.

**Memoria acotada**: como mucho ``max_keys`` cubos. Cada ``prune_every`` comprobaciones (y siempre
que se llega al tope) se podan los cubos que ya se han rellenado del todo, que equivalen a uno
nuevo; si aun así se supera el tope, se descarta el menos usado recientemente (el cliente
recupera un cubo lleno: el límite sigue siendo aproximado, nunca más estricto de lo debido).

``retry_after_seconds`` de una respuesta ``Limited`` es siempre un entero de 1 a 3 600: lo que
falta para la siguiente ficha, redondeado hacia arriba. Hacia un nodo se acota a 1..60
(``contract_retry_after``, NFR-GOB-33).
"""

from __future__ import annotations

import enum
import hashlib
import hmac
import os
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Final

from vigia_platform.shared.clock import Clock

__all__ = [
    "AUTHENTICATED_ORIGIN_BUDGET",
    "BRAKE_KEY",
    "CONTRACT_MAX_RETRY_AFTER_SECONDS",
    "DEFAULT_MAX_KEYS",
    "KEY_PATTERN",
    "MAX_RETRY_AFTER_SECONDS",
    "PUBLIC_ORIGIN_BUDGET",
    "SESSION_BUDGET",
    "Allowed",
    "Budget",
    "EnrollmentWindow",
    "Limited",
    "RateLimiter",
    "contract_retry_after",
    "enrollment_origin_key",
    "node_key",
    "origin_key",
    "public_key",
    "session_key",
]

_NS_PER_SECOND: Final = 1_000_000_000
MAX_RETRY_AFTER_SECONDS: Final = 3_600
CONTRACT_MAX_RETRY_AFTER_SECONDS: Final = 60
"""Tope de ``retry_after_seconds`` en un ``rate_limited`` del contrato (NFR-GOB-33)."""
BRAKE_KEY: Final = "brake:node"
"""Cubo del freno global de emergencia de las rutas del contrato (TASK-206)."""
DEFAULT_MAX_KEYS: Final = 100_000
"""Tope de cubos por proceso ``[objetivo propio]``: unos 20 MB en el peor caso."""
DEFAULT_PRUNE_EVERY: Final = 1_024

KEY_PATTERN: Final = re.compile(
    r"(?:session|origin|public):[0-9a-f]{64}"
    r"|node:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:[a-z][a-z0-9_]{0,63}"
    r"|enrollment:[0-9a-f]{64}:(?:quarter|day)"
    r"|brake:node"
)
"""Forma cerrada de una clave del limitador; ninguna lleva una dirección ni un secreto en claro."""

_SESSION_ID_HASH: Final = re.compile(r"[0-9a-f]{64}")
_OPERATION: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
_ORIGIN_MAX_CHARS: Final = 256


@dataclass(frozen=True, slots=True)
class Budget:
    """``limit`` peticiones por ``window_seconds`` con una ráfaga de ``burst`` (por defecto, el
    propio límite)."""

    limit: int
    window_seconds: int = 60
    burst: int | None = None

    def __post_init__(self) -> None:
        for name in ("limit", "window_seconds"):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 1_000_000:
                raise ValueError(f"{name} debe ser un entero de 1 a 1 000 000")
        if self.burst is not None and (
            type(self.burst) is not int or not 1 <= self.burst <= 1_000_000
        ):
            raise ValueError("burst debe ser un entero de 1 a 1 000 000")

    @property
    def capacity(self) -> int:
        """Fichas que caben en el cubo (la ráfaga)."""
        return self.limit if self.burst is None else self.burst

    @property
    def window_ns(self) -> int:
        return self.window_seconds * _NS_PER_SECOND


SESSION_BUDGET: Final = Budget(600)
"""600 peticiones por minuto por sesión (NFR-NUC-25)."""
AUTHENTICATED_ORIGIN_BUDGET: Final = Budget(1_200)
"""1 200 peticiones por minuto por origen en rutas autenticadas (NFR-NUC-25)."""
PUBLIC_ORIGIN_BUDGET: Final = Budget(60)
"""60 peticiones por minuto por origen en rutas públicas (NFR-NUC-25)."""


@dataclass(frozen=True, slots=True)
class Allowed:
    """La petición entra; consume una ficha."""


@dataclass(frozen=True, slots=True)
class Limited:
    """La petición no entra: ``rate_limited`` con ``retry_after_seconds`` (1 a 3 600)."""

    retry_after_seconds: int


_ALLOWED: Final = Allowed()


class _Bucket:
    __slots__ = ("budget", "credit", "updated_ns")

    def __init__(self, budget: Budget, now_ns: int) -> None:
        self.budget = budget
        self.credit = budget.capacity * budget.window_ns
        self.updated_ns = now_ns

    def refill(self, now_ns: int) -> None:
        budget = self.budget
        elapsed = now_ns - self.updated_ns
        if elapsed > 0:
            self.credit = min(
                self.credit + elapsed * budget.limit, budget.capacity * budget.window_ns
            )
            self.updated_ns = now_ns

    def full(self, now_ns: int) -> bool:
        self.refill(now_ns)
        return self.credit >= self.budget.capacity * self.budget.window_ns


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


class RateLimiter:
    """Cubos de fichas en memoria del proceso (no comparte estado con otros procesos)."""

    def __init__(
        self,
        clock: Clock,
        *,
        max_keys: int = DEFAULT_MAX_KEYS,
        prune_every: int = DEFAULT_PRUNE_EVERY,
    ) -> None:
        if type(max_keys) is not int or max_keys < 1:
            raise ValueError("max_keys debe ser un entero positivo")
        if type(prune_every) is not int or prune_every < 1:
            raise ValueError("prune_every debe ser un entero positivo")
        self._clock = clock
        self._max_keys = max_keys
        self._prune_every = prune_every
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._checks = 0
        self._last_ns = 0

    def __len__(self) -> int:
        return len(self._buckets)

    def _now_ns(self) -> int:
        # El reloj es monótono; aun así, nunca se retrocede (un cubo nunca pierde crédito).
        now = int(self._clock.monotonic() * _NS_PER_SECOND)
        if now < self._last_ns:
            return self._last_ns
        self._last_ns = now
        return now

    def check(self, key: str, budget: Budget) -> Allowed | Limited:
        """Consume una ficha del cubo ``key`` si la hay; si no, ``Limited``.

        Raises:
            ValueError: ``key`` no tiene la forma cerrada de ``KEY_PATTERN``.
        """
        if not isinstance(key, str) or KEY_PATTERN.fullmatch(key) is None:
            raise ValueError("clave del limitador fuera de la lista cerrada")
        if not isinstance(budget, Budget):
            raise TypeError("budget debe ser Budget")
        now_ns = self._now_ns()
        self._checks += 1
        if self._checks % self._prune_every == 0:
            self.prune()
        bucket = self._buckets.get(key)
        if bucket is None or bucket.budget != budget:
            # Un presupuesto distinto para la misma clave empieza con su cubo lleno.
            bucket = _Bucket(budget, now_ns)
            self._buckets[key] = bucket
            if len(self._buckets) > self._max_keys:
                self._evict(keep=key)
        else:
            self._buckets.move_to_end(key)
            bucket.refill(now_ns)
        cost = budget.window_ns
        if bucket.credit >= cost:
            bucket.credit -= cost
            return _ALLOWED
        missing_ns = _ceil_div(cost - bucket.credit, budget.limit)
        seconds = _ceil_div(missing_ns, _NS_PER_SECOND)
        return Limited(min(max(seconds, 1), MAX_RETRY_AFTER_SECONDS))

    def prune(self, *, keep: str | None = None) -> int:
        """Descarta los cubos llenos (equivalen a uno nuevo) salvo ``keep``; devuelve cuántos."""
        now_ns = self._now_ns()
        idle = [key for key, bucket in self._buckets.items() if key != keep and bucket.full(now_ns)]
        for key in idle:
            del self._buckets[key]
        return len(idle)

    def _evict(self, *, keep: str) -> None:
        # El cubo recién creado (``keep``) es el más reciente: nunca se descarta a sí mismo.
        self.prune(keep=keep)
        while len(self._buckets) > self._max_keys:
            self._buckets.popitem(last=False)


# --- Claves -------------------------------------------------------------------------------------


def session_key(session_id_hash: str) -> str:
    """Clave por sesión: el SHA-256 de la sesión (nunca el identificador en claro)."""
    if not isinstance(session_id_hash, str) or _SESSION_ID_HASH.fullmatch(session_id_hash) is None:
        raise ValueError("session_id_hash debe ser un SHA-256 en hexadecimal")
    return f"session:{session_id_hash}"


def _origin_digest(address: object, secret: bytes) -> str:
    text = address.strip().lower() if isinstance(address, str) else ""
    if not text or len(text) > _ORIGIN_MAX_CHARS or not text.isascii():
        text = "unknown"
    return hmac.new(secret, text.encode("ascii"), hashlib.sha256).hexdigest()


class _ProcessSecret:
    value: bytes = os.urandom(32)


def origin_key(address: object, *, secret: bytes | None = None) -> str:
    """Clave por origen de red en rutas autenticadas (HMAC con la clave del proceso)."""
    return f"origin:{_origin_digest(address, secret or _ProcessSecret.value)}"


def public_key(address: object, *, secret: bytes | None = None) -> str:
    """Clave por origen de red en rutas públicas (cubo distinto del autenticado)."""
    return f"public:{_origin_digest(address, secret or _ProcessSecret.value)}"


class EnrollmentWindow(enum.StrEnum):
    """Las dos ventanas del límite del alta por origen (NFR-GOB-33): 15 minutos y un día."""

    QUARTER = "quarter"
    DAY = "day"


def enrollment_origin_key(
    address: object, window: EnrollmentWindow, *, secret: bytes | None = None
) -> str:
    """Clave del alta por origen de red en ``window``: HMAC de la dirección, nunca en claro."""
    window = EnrollmentWindow(window)
    return f"enrollment:{_origin_digest(address, secret or _ProcessSecret.value)}:{window.value}"


def contract_retry_after(seconds: int) -> int:
    """``retry_after_seconds`` de un ``rate_limited`` del contrato: el del cubo acotado a 1..60."""
    return min(max(int(seconds), 1), CONTRACT_MAX_RETRY_AFTER_SECONDS)


def node_key(node_id: object, operation: str) -> str:
    """Clave ``node:<node_id>:<operación>`` que registra U-03 para sus rutas (pendiente nº 37)."""
    text = str(node_id) if node_id is not None else ""
    key = f"node:{text}:{operation}"
    if (
        not isinstance(operation, str)
        or _OPERATION.fullmatch(operation) is None
        or KEY_PATTERN.fullmatch(key) is None
    ):
        raise ValueError("node_id debe ser un UUID y la operación snake_case")
    return key
