"""Salud superficial y profunda (BR-NUC-97; NFR-NUC-13; PAT-NUC-RES-02; LC-NUC-19, 31).

- ``GET /health/live`` (pública): el proceso atiende. No toca ninguna dependencia, así que
  responde 200 aunque la base, el almacén o el gestor de secretos estén caídos, y también
  durante el arranque (incluido el plazo previo a terminar por un arranque fallido).
- ``GET /health/ready`` (interna; el balanceador solo enruta a instancias ``ready``): 200 solo si
  el arranque terminó y las cuatro comprobaciones pasan **en menos de 2 s**:

  1. **base**: una lectura con ``SET LOCAL`` a una organización inexistente que debe ver cero
     filas (la seguridad a nivel de fila está en vigor), ``Database.health``;
  2. **esquema**: la versión aplicada no es anterior a ``MINIMUM_SCHEMA_VERSION`` (la misma
     ida y vuelta que la base);
  3. **almacén**: los metadatos del objeto centinela (``head_object``) existen;
  4. **claves**: hay clave ``active`` vigente con material en memoria para cada propósito
     (``SigningService.has_active_key``); no llama al gestor de secretos.

  Las comprobaciones corren a la vez, cada una con su tope dentro del presupuesto. Una que no
  responde a tiempo cuenta como fallida; la respuesta nunca espera más del presupuesto. Si falla,
  503 con un ``ApiError`` genérico (``temporarily_unavailable``): qué comprobación falló va al
  registro, nunca a la respuesta.
"""

from __future__ import annotations

import asyncio
import enum
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Final, Literal, Protocol

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from vigia_platform.shared.api.declarations import UnauthenticatedRoute, unauthenticated
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.db import DatabaseHealth
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.schema_version import MINIMUM_SCHEMA_VERSION
from vigia_platform.shared.signing.keys import SigningPurpose
from vigia_platform.shared.storage import ObjectHead

__all__ = [
    "CHECK_TIMEOUT_SECONDS",
    "READINESS_BUDGET_SECONDS",
    "DatabaseHealthPort",
    "ReadinessCheck",
    "ReadinessProbe",
    "ReadinessReport",
    "SentinelPort",
    "SigningKeysPort",
    "health_router",
]

READINESS_BUDGET_SECONDS: Final = 2.0
"""Tope de ``/health/ready`` (NFR-NUC-13)."""
CHECK_TIMEOUT_SECONDS: Final = 1.5
"""Tope de cada comprobación: deja margen dentro del presupuesto para responder."""

_log = get_logger("shared.api.health")


class ReadinessCheck(enum.StrEnum):
    DATABASE = "database"
    SCHEMA_VERSION = "schema_version"
    STORAGE = "storage"
    SIGNING_KEYS = "signing_keys"
    STARTUP = "startup"


class DatabaseHealthPort(Protocol):
    async def health(self, *, timeout_seconds: float) -> DatabaseHealth: ...


class SentinelPort(Protocol):
    """La parte de ``StoragePort`` que usa la salud."""

    async def head_object(self, key: str) -> ObjectHead | None: ...


class SigningKeysPort(Protocol):
    def has_active_key(self, purpose: SigningPurpose) -> bool: ...


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """Resultado de cada comprobación; ``ready`` si todas pasaron."""

    checks: Mapping[ReadinessCheck, bool]

    @property
    def ready(self) -> bool:
        return all(self.checks.values())

    @property
    def failed(self) -> tuple[ReadinessCheck, ...]:
        return tuple(check for check, passed in self.checks.items() if not passed)


class ReadinessProbe:
    """Las comprobaciones de ``/health/ready`` sobre los puertos de la aplicación."""

    def __init__(
        self,
        *,
        database: DatabaseHealthPort,
        storage: SentinelPort,
        sentinel_key: str,
        signing: SigningKeysPort,
        minimum_schema_version: int = MINIMUM_SCHEMA_VERSION,
        check_timeout_seconds: float = CHECK_TIMEOUT_SECONDS,
    ) -> None:
        if not 0 < check_timeout_seconds < READINESS_BUDGET_SECONDS:
            raise ValueError("el tope de cada comprobación debe caber en el presupuesto de 2 s")
        self._database = database
        self._storage = storage
        self._sentinel_key = sentinel_key
        self._signing = signing
        self._minimum = minimum_schema_version
        self._timeout = check_timeout_seconds
        self._inflight: dict[ReadinessCheck, asyncio.Future[object]] = {}

    async def _bounded(
        self, check: ReadinessCheck, work: Callable[[], Awaitable[object]]
    ) -> object | None:
        """Resultado de ``work`` o ``None`` si falla o no responde a tiempo.

        A lo sumo una comprobación en curso por dependencia: si la anterior sigue colgada (una
        dependencia que no responde), la sonda nueva espera a esa en vez de abrir otra, así que
        las sondas del balanceador nunca acumulan hilos ni conexiones.
        """
        pending: asyncio.Future[object] | None = self._inflight.get(check)
        if pending is None or pending.done():
            pending = asyncio.ensure_future(work())
            pending.add_done_callback(_consume)
            self._inflight[check] = pending
        try:
            async with asyncio.timeout(self._timeout):
                return await asyncio.shield(pending)
        except Exception:  # toda causa cuenta como comprobación fallida
            return None

    async def _database_checks(self) -> dict[ReadinessCheck, bool]:
        async def probe() -> object:
            return await self._database.health(timeout_seconds=self._timeout)

        health = await self._bounded(ReadinessCheck.DATABASE, probe)
        if not isinstance(health, DatabaseHealth):
            return {ReadinessCheck.DATABASE: False, ReadinessCheck.SCHEMA_VERSION: False}
        version = health.schema_version
        return {
            # Filas visibles con una organización inexistente: la seguridad a nivel de fila no
            # está en vigor (p. ej. un rol con BYPASSRLS). No se enruta tráfico.
            ReadinessCheck.DATABASE: health.visible_organizations == 0,
            ReadinessCheck.SCHEMA_VERSION: version is not None and version >= self._minimum,
        }

    async def _storage_check(self) -> dict[ReadinessCheck, bool]:
        async def probe() -> object:
            return await self._storage.head_object(self._sentinel_key)

        head = await self._bounded(ReadinessCheck.STORAGE, probe)
        return {ReadinessCheck.STORAGE: isinstance(head, ObjectHead)}

    def _signing_check(self) -> dict[ReadinessCheck, bool]:
        try:
            passed = all(self._signing.has_active_key(purpose) for purpose in SigningPurpose)
        except Exception:  # fallo cerrado
            passed = False
        return {ReadinessCheck.SIGNING_KEYS: passed}

    async def check(self) -> ReadinessReport:
        database, storage = await asyncio.gather(self._database_checks(), self._storage_check())
        return ReadinessReport({**database, **storage, **self._signing_check()})


def _consume(future: asyncio.Future[object]) -> None:
    """Recoge el resultado de una comprobación abandonada: no deja avisos sin leer."""
    if not future.cancelled():
        future.exception()


class ReadinessSource(Protocol):
    """Lo que ``/health/ready`` consulta: si el arranque terminó y la sonda."""

    @property
    def started(self) -> bool: ...

    @property
    def probe(self) -> ReadinessProbe | None: ...


READINESS_STATE_KEY: Final = "vigia_readiness"


class LiveStatus(BaseModel):
    """Respuesta de ``/health/live``."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    status: Literal["live"]


_LIVE: Final = {"status": "live"}
_READY: Final = {"status": "ready"}
_NO_STORE: Final = {"Cache-Control": "no-store"}


def health_router() -> APIRouter:
    """Las dos rutas de salud, declaradas en la lista cerrada (BR-NUC-91, BR-NUC-97)."""
    router = APIRouter(tags=["salud"])

    @router.get(
        UnauthenticatedRoute.HEALTH_LIVE.path,
        dependencies=[unauthenticated(UnauthenticatedRoute.HEALTH_LIVE)],
        response_model=LiveStatus,
        summary="Salud superficial: el proceso atiende",
    )
    async def live() -> JSONResponse:
        return JSONResponse(_LIVE, headers=_NO_STORE)

    @router.get(
        UnauthenticatedRoute.HEALTH_READY.path,
        dependencies=[unauthenticated(UnauthenticatedRoute.HEALTH_READY)],
        include_in_schema=False,
    )
    async def ready(request: Request) -> JSONResponse:
        source: ReadinessSource | None = getattr(request.app.state, READINESS_STATE_KEY, None)
        probe = None if source is None else source.probe
        if source is None or not source.started or probe is None:
            _log.warning("salud profunda: el arranque no ha terminado")
            raise ApiError(ApiErrorCode.TEMPORARILY_UNAVAILABLE)
        try:
            async with asyncio.timeout(READINESS_BUDGET_SECONDS):
                report = await probe.check()
        except TimeoutError:
            report = ReadinessReport({ReadinessCheck.STARTUP: False})
        if not report.ready:
            for check in report.failed:
                _log.warning("salud profunda fallida", reason=check.value)
            raise ApiError(ApiErrorCode.TEMPORARILY_UNAVAILABLE)
        return JSONResponse(_READY, headers=_NO_STORE)

    return router
