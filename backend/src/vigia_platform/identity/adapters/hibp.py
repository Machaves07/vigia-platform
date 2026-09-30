"""Contraseñas filtradas por rango de anonimato k con respaldo local (LC-NUC-01; PAT-NUC-RES-03).

Implementa el puerto ``BreachChecker`` de ``identity.auth.passwords`` (NFR-NUC-26, riesgo R8):

1. Se calcula el SHA-1 de la contraseña (UTF-8). Si está en el **respaldo local** (las 100 000
   más filtradas, ``resources/pwned-top100k.txt``, generado en la construcción de la imagen por
   ``tools/build_pwned_top100k.py``), se responde ``breached`` sin salir a la red.
2. Si no, se consulta ``GET /range/<5 primeros caracteres del SHA-1>`` al servicio de Have I Been
   Pwned. **Solo salen esos 5 caracteres hexadecimales** (anonimato k): ni la contraseña, ni el
   resto del hash, ni el correo, ni ningún dato de la persona. Se pide relleno
   (``Add-Padding: true``) para que el tamaño de la respuesta no delate el prefijo; las entradas
   de relleno (cuenta 0) se descartan.
3. Tiempo de espera **total** de 3 s (NFR-NUC-36): conexión, envío y lectura juntos, no por
   tramo; una respuesta que gotea no lo alarga.
4. Si el servicio no responde a tiempo, rechaza la conexión, responde algo que no es un 200 bien
   formado o su circuito está abierto, decide el respaldo local, que ya dijo "no está": la
   contraseña se acepta con ``BreachSource.LOCAL_FALLBACK``, se suma 1 a ``hibp_fallback_used`` y
   se registra un aviso. Nunca se rechaza ni se acepta una contraseña **por** la caída: decide la
   misma lista local que decide cuando el servicio responde (PAT-NUC-RES-03, FS-NUC-06).

Cortacircuito ``[objetivo propio]`` (el diseño lo nombra sin umbrales): se abre tras 3 fallos
seguidos y durante 60 s responde el respaldo sin esperar los 3 s; pasado ese tiempo deja pasar
**una** consulta de prueba (semiabierto): si responde, se cierra; si falla, vuelve a abrirse
otros 60 s. El tiempo se mide con el ``Clock`` inyectado (``monotonic``).

La respuesta se lee como flujo con un tope de 512 KiB (una respuesta real con relleno ronda los
40 KiB); cada línea debe ser ``<35 hex>:<cuenta>``: una respuesta que no encaja se trata como
fallo del servicio, nunca como "no filtrada".
"""

from __future__ import annotations

import asyncio
import enum
import hashlib
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Final

import httpx

from vigia_platform.identity.auth.passwords import (
    BreachCheck,
    BreachSource,
    password_bytes,
)
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.observability import redaction
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics

__all__ = [
    "BREAKER_FAILURE_THRESHOLD",
    "BREAKER_OPEN_SECONDS",
    "DEFAULT_LOCAL_LIST_PATH",
    "HIBP_RANGE_URL",
    "MAX_RESPONSE_BYTES",
    "PREFIX_LENGTH",
    "TIMEOUT_SECONDS",
    "BreakerState",
    "CircuitBreaker",
    "FallbackReason",
    "HibpBreachChecker",
    "LocalBreachList",
    "sha1_hex",
]

HIBP_RANGE_URL: Final = "https://api.pwnedpasswords.com/range/"
PREFIX_LENGTH: Final = 5
"""Caracteres del SHA-1 que salen hacia el servicio (anonimato k)."""
TIMEOUT_SECONDS: Final = 3.0
"""Tope total de la consulta (NFR-NUC-26, NFR-NUC-36)."""
MAX_RESPONSE_BYTES: Final = 512 * 1024
BREAKER_FAILURE_THRESHOLD: Final = 3
"""Fallos seguidos que abren el circuito ``[objetivo propio]``."""
BREAKER_OPEN_SECONDS: Final = 60.0
"""Tiempo con el circuito abierto antes de la consulta de prueba ``[objetivo propio]``."""
USER_AGENT: Final = "vigia-platform"
"""``User-Agent`` fijo: la consulta por rango no lo exige, pero la documentación del servicio pide
identificar al cliente. No lleva versión ni nada del despliegue."""

DEFAULT_LOCAL_LIST_PATH: Final = Path(__file__).resolve().parents[4] / "resources/pwned-top100k.txt"
"""``backend/resources/pwned-top100k.txt`` (copiado a la imagen con ``backend/``)."""

_SHA1_HEX: Final = re.compile(r"[0-9A-F]{40}")
_RANGE_LINE: Final = re.compile(r"([0-9A-F]{35}):([0-9]{1,12})")

_LOG_FALLBACK: Final = "consulta de filtradas resuelta con el respaldo local"
_LOG_BREAKER_OPEN: Final = "circuito del servicio de filtradas abierto"
_log = get_logger("identity.adapters.hibp")


class FallbackReason(enum.StrEnum):
    """Por qué respondió el respaldo local (atributo ``reason`` del registro)."""

    TIMEOUT = "hibp_timeout"
    UNAVAILABLE = "hibp_unavailable"
    BAD_RESPONSE = "hibp_bad_response"
    CIRCUIT_OPEN = "hibp_circuit_open"


redaction.DEFAULT_POLICY.register("reason", [reason.value for reason in FallbackReason])


def sha1_hex(password: str) -> str:
    """SHA-1 en hexadecimal en mayúsculas del UTF-8 de ``password`` (formato del servicio)."""
    # SHA-1 lo impone el protocolo del servicio de rango; no protege nada por sí mismo.
    return hashlib.sha1(password_bytes(password), usedforsecurity=False).hexdigest().upper()


class LocalBreachList:
    """Respaldo local: SHA-1 de las contraseñas más filtradas, en memoria."""

    __slots__ = ("_digests",)

    def __init__(self, sha1_hex_digests: Iterable[str]) -> None:
        digests: set[bytes] = set()
        for number, line in enumerate(sha1_hex_digests, start=1):
            if not isinstance(line, str) or not _SHA1_HEX.fullmatch(line):
                raise ValueError(
                    f"respaldo local de filtradas: la línea {number} no es un SHA-1 en "
                    "hexadecimal en mayúsculas"
                )
            digests.add(bytes.fromhex(line))
        if not digests:
            # Un respaldo vacío aceptaría toda contraseña durante una caída del servicio.
            raise ValueError("respaldo local de filtradas vacío")
        self._digests = frozenset(digests)

    @classmethod
    def from_file(cls, path: Path = DEFAULT_LOCAL_LIST_PATH) -> LocalBreachList:
        """Carga el archivo (una línea por hash, LF). Un archivo ausente o inválido lanza al
        arrancar: el servicio no arranca sin su respaldo.

        Raises:
            OSError: el archivo no existe o no se puede leer.
            ValueError: el archivo está vacío o tiene una línea que no es un SHA-1.
        """
        text = path.read_text(encoding="ascii")
        return cls(line for line in text.split("\n") if line)

    def __len__(self) -> int:
        return len(self._digests)

    def __contains__(self, sha1_hex_digest: object) -> bool:
        if not isinstance(sha1_hex_digest, str) or not _SHA1_HEX.fullmatch(sha1_hex_digest):
            return False
        return bytes.fromhex(sha1_hex_digest) in self._digests


class BreakerState(enum.StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Cortacircuito del servicio de filtradas: cerrado, abierto y semiabierto con una prueba."""

    def __init__(
        self,
        clock: Clock,
        *,
        failure_threshold: int = BREAKER_FAILURE_THRESHOLD,
        open_seconds: float = BREAKER_OPEN_SECONDS,
    ) -> None:
        if failure_threshold < 1 or open_seconds <= 0:
            raise ValueError("umbral de fallos ≥ 1 y tiempo abierto > 0")
        self._clock = clock
        self._failure_threshold = failure_threshold
        self._open_seconds = open_seconds
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probe_in_flight = False

    @property
    def state(self) -> BreakerState:
        return self._state

    def allow(self) -> bool:
        """``True`` si la consulta puede salir; en semiabierto, solo una a la vez."""
        if self._state is BreakerState.OPEN:
            if self._clock.monotonic() - self._opened_at < self._open_seconds:
                return False
            self._state = BreakerState.HALF_OPEN
        if self._state is BreakerState.HALF_OPEN:
            if self._probe_in_flight:
                return False
            self._probe_in_flight = True
        return True

    def release_probe(self) -> None:
        """La consulta se abandonó sin resultado: otra puede probar."""
        self._probe_in_flight = False

    def record_success(self) -> None:
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._probe_in_flight = False

    def record_failure(self) -> None:
        self._probe_in_flight = False
        self._failures += 1
        if self._state is BreakerState.HALF_OPEN or self._failures >= self._failure_threshold:
            if self._state is not BreakerState.OPEN:
                _log.warning(_LOG_BREAKER_OPEN)
            self._state = BreakerState.OPEN
            self._opened_at = self._clock.monotonic()


class _BadRangeResponse(Exception):
    """El servicio respondió algo que no es un rango bien formado."""


class HibpBreachChecker:
    """``BreachChecker`` con el servicio por rango de anonimato k y el respaldo local."""

    def __init__(
        self,
        local_list: LocalBreachList,
        clock: Clock,
        *,
        client: httpx.AsyncClient | None = None,
        range_url: str = HIBP_RANGE_URL,
        timeout_seconds: float = TIMEOUT_SECONDS,
        breaker: CircuitBreaker | None = None,
        metrics: PlatformMetrics | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("el tiempo de espera debe ser positivo")
        self._local = local_list
        self._range_url = range_url
        self._timeout = timeout_seconds
        self._breaker = breaker if breaker is not None else CircuitBreaker(clock)
        self._metrics = metrics
        self._owns_client = client is None
        self._client = (
            client
            if client is not None
            else httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds), follow_redirects=False)
        )

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def check(self, password: str) -> BreachCheck:
        """Filtrada o no; nunca lanza por el servicio (decide el respaldo local)."""
        digest = sha1_hex(password)
        if digest in self._local:
            return BreachCheck(breached=True, source=BreachSource.LOCAL_LIST)
        if not self._breaker.allow():
            return self._fallback(FallbackReason.CIRCUIT_OPEN)
        prefix, suffix = digest[:PREFIX_LENGTH], digest[PREFIX_LENGTH:]
        try:
            async with asyncio.timeout(self._timeout):
                suffixes = await self._fetch_range(prefix)
        except TimeoutError:
            self._breaker.record_failure()
            return self._fallback(FallbackReason.TIMEOUT)
        except _BadRangeResponse:
            self._breaker.record_failure()
            return self._fallback(FallbackReason.BAD_RESPONSE)
        except (httpx.HTTPError, httpx.StreamError, httpx.InvalidURL, OSError):
            self._breaker.record_failure()
            return self._fallback(FallbackReason.UNAVAILABLE)
        except BaseException:
            # Cancelación de quien llama: no es un fallo del servicio, pero la consulta de
            # prueba del semiabierto no puede quedar retenida.
            self._breaker.release_probe()
            raise
        self._breaker.record_success()
        return BreachCheck(breached=suffix in suffixes, source=BreachSource.REMOTE)

    async def _fetch_range(self, prefix: str) -> frozenset[str]:
        async with self._client.stream(
            "GET",
            self._range_url + prefix,
            headers={
                "Add-Padding": "true",
                "Accept-Encoding": "identity",
                "User-Agent": USER_AGENT,
            },
            timeout=httpx.Timeout(self._timeout),
        ) as response:
            # Sin compresión: el tope cuenta los bytes tal como llegan (ninguna bomba de
            # compresión se expande en memoria antes de medirla).
            if response.status_code != httpx.codes.OK or response.headers.get(
                "content-encoding", "identity"
            ).lower() not in ("", "identity"):
                raise _BadRangeResponse
            body = bytearray()
            async for chunk in response.aiter_raw():
                body += chunk
                if len(body) > MAX_RESPONSE_BYTES:
                    raise _BadRangeResponse
        return _parse_range(bytes(body))

    def _fallback(self, reason: FallbackReason) -> BreachCheck:
        metrics = self._metrics if self._metrics is not None else get_metrics()
        metrics.hibp_fallback_used.add(1)
        _log.warning(_LOG_FALLBACK, reason=reason)
        # Ya se comprobó que no está en el respaldo local: decide la lista, no la caída.
        return BreachCheck(breached=False, source=BreachSource.LOCAL_FALLBACK)


def _parse_range(body: bytes) -> frozenset[str]:
    """Sufijos con cuenta > 0; cualquier línea que no encaje invalida la respuesta."""
    try:
        text = body.decode("ascii")
    except UnicodeDecodeError as error:
        raise _BadRangeResponse from error
    suffixes: set[str] = set()
    lines = [line for line in text.replace("\r\n", "\n").split("\n") if line]
    if not lines:
        raise _BadRangeResponse
    for line in lines:
        match = _RANGE_LINE.fullmatch(line)
        if match is None:
            raise _BadRangeResponse
        if int(match[2]) > 0:
            suffixes.add(match[1])
    return frozenset(suffixes)
