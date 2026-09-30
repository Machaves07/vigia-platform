"""Dobles de prueba de ``shared.api`` (TASK-133): dependencias que responden, fallan o se cuelgan.

Cada doble cuenta sus llamadas y cuántas siguen en curso, para comprobar que las sondas de salud
nunca acumulan trabajo contra una dependencia colgada. Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from vigia_contracts.clock import SimulatedClock

from tests.second_factor_support import FakeKms
from tests.signing_support import START
from vigia_platform.shared.api.app import AppConfig, AppRuntime, UnitRegistration, create_app
from vigia_platform.shared.db import DatabaseHealth, TemporarilyUnavailable
from vigia_platform.shared.observability.redaction import AttributePolicy
from vigia_platform.shared.schema_version import MINIMUM_SCHEMA_VERSION
from vigia_platform.shared.signing.keys import SigningPurpose
from vigia_platform.shared.signing.service import SigningStartupError
from vigia_platform.shared.storage import ObjectHead, StorageUnavailable

__all__ = [
    "SENTINEL",
    "FakeDatabase",
    "FakeSigning",
    "FakeStorage",
    "Mode",
    "World",
    "config",
]

SENTINEL = "health/ready-sentinel"
HANG_SECONDS = 30.0
"""Lo que tarda una dependencia «colgada»: mucho más que cualquier tope de la aplicación."""


class Mode(enum.Enum):
    UP = "up"
    DOWN = "down"
    """Falla enseguida (conexión rechazada)."""
    HUNG = "hung"
    """No responde (servicio congelado)."""


@dataclass
class _Tracked:
    mode: Mode = Mode.UP
    calls: int = 0
    in_flight: int = 0

    async def _enter(self, failure: Exception) -> None:
        self.calls += 1
        if self.mode is Mode.DOWN:
            raise failure
        if self.mode is Mode.HUNG:
            self.in_flight += 1
            try:
                await asyncio.sleep(HANG_SECONDS)
            finally:
                self.in_flight -= 1
            raise failure


@dataclass
class FakeDatabase(_Tracked):
    """``Database.health``: filas visibles y versión configurables."""

    visible_organizations: int = 0
    schema_version: int | None = MINIMUM_SCHEMA_VERSION

    async def health(self, *, timeout_seconds: float) -> DatabaseHealth:
        await self._enter(TemporarilyUnavailable())
        return DatabaseHealth(
            visible_organizations=self.visible_organizations, schema_version=self.schema_version
        )


@dataclass
class FakeStorage(_Tracked):
    """``head_object`` del objeto centinela (``present = False``: el objeto no existe)."""

    present: bool = True

    async def head_object(self, key: str) -> ObjectHead | None:
        await self._enter(StorageUnavailable("head_object"))
        if not self.present or key != SENTINEL:
            return None
        return ObjectHead(
            key=key,
            size_bytes=1,
            checksum_sha256=None,
            checksum_type=None,
            content_type="text/plain",
            metadata={},
            version_id=None,
        )


@dataclass
class FakeSigning(_Tracked):
    """``SigningService`` reducido: ``start`` falla si ``mode`` no es ``UP``."""

    ready: bool = False
    missing: set[SigningPurpose] = field(default_factory=set)
    refreshes_started: int = 0

    async def start(self) -> None:
        await self._enter(SigningStartupError(["secret_unavailable"]))
        self.ready = True

    def has_active_key(self, purpose: SigningPurpose) -> bool:
        return self.ready and purpose not in self.missing

    async def run_refresh(self, stop: asyncio.Event) -> None:
        self.refreshes_started += 1
        await stop.wait()


def config(environment: str = "test", **changes: Any) -> AppConfig:
    values: dict[str, Any] = {
        "environment": environment,
        "data_key_id": "alias/vigia-secrets",
        "health_sentinel_key": SENTINEL,
        "startup_deadline_seconds": 60.0,
        "startup_retry_seconds": 5.0,
    }
    values.update(changes)
    return AppConfig(**values)


@dataclass
class World:
    """Una aplicación con dobles; ``sleep`` avanza el reloj simulado sin esperar de verdad."""

    clock: SimulatedClock = field(default_factory=lambda: SimulatedClock(START))
    database: FakeDatabase = field(default_factory=FakeDatabase)
    storage: FakeStorage = field(default_factory=FakeStorage)
    signing: FakeSigning = field(default_factory=FakeSigning)
    kms: FakeKms = field(default_factory=FakeKms)
    exits: list[int] = field(default_factory=list)
    synchronized: list[str] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)
    registry_failures: int = 0

    async def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock.advance(seconds)
        await asyncio.sleep(0)

    async def _synchronize(self) -> None:
        if self.registry_failures:
            self.registry_failures -= 1
            raise RuntimeError("registro incoherente")
        self.synchronized.append("registries")

    def runtime(self, **changes: Any) -> AppRuntime:
        values: dict[str, Any] = {
            "clock": self.clock,
            "database": self.database,
            "storage": self.storage,
            "signing": self.signing,
            "kms": self.kms,
            "registries": (self._synchronize,),
            "sleep": self._sleep,
            "on_startup_failure": self.exits.append,
            # Una política propia: la global no se toca en las pruebas.
            "attribute_policy": AttributePolicy(),
        }
        values.update(changes)
        return AppRuntime(**values)

    def app(
        self,
        environment: str = "test",
        *,
        units: tuple[UnitRegistration, ...] | None = None,
        permissions: frozenset[str] | None = None,
        runtime: Mapping[str, Any] | None = None,
        **config_changes: Any,
    ) -> Any:
        return create_app(
            config(environment, **config_changes),
            runtime=self.runtime(**dict(runtime or {})),
            units=units,
            permissions=permissions,
        )
