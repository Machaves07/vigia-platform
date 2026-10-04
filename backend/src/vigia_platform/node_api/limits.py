"""Límites de tasa de las rutas del contrato (NFR-GOB-33 y su nota por ruta; BR-GOB-62; R10).

Sobre el cubo de fichas heredado de U-02 (``shared.ratelimit``: por proceso, aproximado por
instancia, riesgo R10 aceptado) y antes de la verificación previa (como la plataforma simulada de
U-01: la ficha se consume al llegar la petición, también si después se rechaza):

1. **Freno global de emergencia** (``EmergencyBrake``), configurable **sin despliegue**: lo fija
   ``vigia-admin set-node-rate-brake --per-minute N|--off`` escribiendo la entrada de auditoría
   ``node_rate_brake_set`` en la cadena de la proveedora (la auditoría **es** el ajuste, con actor,
   fecha y valor); cada proceso lee la última con una caché de ``BRAKE_CACHE_SECONDS`` y, si hay
   límite, un cubo ``brake:node`` común a todas las rutas del contrato del proceso. Si la lectura
   falla se conserva el último valor leído.
2. **Por nodo** (sujeto del certificado; clave ``node:<node_id>:<operación>``): hallazgos,
   detecciones y eventos comparten 240 por minuto con ráfaga 60; concesiones 480 con ráfaga 120;
   latidos 4 por minuto; catálogo 30; confirmación 30; rotación 2 por hora; resultado de
   actualización 10 por hora.
3. **Alta**: por origen (HMAC de la dirección, nunca en claro; claves
   ``enrollment:<hmac>:quarter`` y ``:day``) 5 cada 15 minutos y 20 al día, y por ``node_id``
   5 cada 15 minutos (``admit_enrollment_node``, que llama la ruta del alta tras leer la CSR).

Todo ``rate_limited`` lleva ``retry_after_seconds`` entre 1 y 60 (el cubo admite hasta 3 600: se
acota para el contrato). Ningún presupuesto baja del mínimo de NFR-CTR-02 (60 registros y 240
concesiones por minuto por nodo; ``MINIMUM_PER_MINUTE``): ``check_minimum`` lo comprueba al
importar. La métrica ``rate_limited_total`` lleva la causa (``node``, ``origin`` o ``brake``).
"""

from __future__ import annotations

import asyncio
import enum
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Protocol

from sqlalchemy import text
from vigia_contracts.models.enumerations import RejectionCode

from vigia_platform.ledger.application.audit_writer import AuditOperation
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.node_api.rejections import NodeRejection
from vigia_platform.shared.api.declarations import NodeRoute
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics
from vigia_platform.shared.ratelimit import (
    BRAKE_KEY,
    Budget,
    EnrollmentWindow,
    Limited,
    RateLimiter,
    contract_retry_after,
    enrollment_origin_key,
    node_key,
)

__all__ = [
    "BRAKE_CACHE_SECONDS",
    "BRAKE_OFF",
    "BRAKE_STATEMENT",
    "ENROLLMENT_NODE_BUDGET",
    "ENROLLMENT_ORIGIN_BUDGETS",
    "MAX_BRAKE_PER_MINUTE",
    "MINIMUM_PER_MINUTE",
    "NODE_BUDGETS",
    "AuditBrakeSource",
    "BrakeSource",
    "EmergencyBrake",
    "NodeRateLimits",
    "RateCause",
    "RouteBudget",
    "brake_filters",
    "check_minimum",
    "parse_brake",
]

BRAKE_CACHE_SECONDS: Final = 10.0
"""Caché corta del freno ``[objetivo propio]``: lo que tarda un cambio en llegar a cada proceso."""
BRAKE_OFF: Final = "off"
MAX_BRAKE_PER_MINUTE: Final = 1_000_000

_log = get_logger("node_api.limits")


class RateCause(enum.StrEnum):
    """Causa de un ``rate_limited`` (atributo ``rate_limit`` de ``rate_limited_total``)."""

    NODE = "node"
    ORIGIN = "origin"
    BRAKE = "brake"


@dataclass(frozen=True, slots=True)
class RouteBudget:
    """El cubo de una ruta: el nombre de su operación en la clave y su presupuesto."""

    operation: str
    budget: Budget

    @property
    def sustained_per_minute(self) -> float:
        """Reposición sostenida en peticiones por minuto."""
        return self.budget.limit * 60 / self.budget.window_seconds


_INGEST: Final = RouteBudget("ingest", Budget(240, 60, burst=60))
NODE_BUDGETS: Final[Mapping[NodeRoute, RouteBudget]] = MappingProxyType(
    {
        NodeRoute.FINDING: _INGEST,
        NodeRoute.DETECTION_REVIEW: _INGEST,
        NodeRoute.OBSERVABILITY_EVENT: _INGEST,
        NodeRoute.CLIP_UPLOAD: RouteBudget("clip_upload", Budget(480, 60, burst=120)),
        NodeRoute.HEARTBEAT: RouteBudget("heartbeat", Budget(4, 60)),
        NodeRoute.ZONE_CATALOG: RouteBudget("zone_catalog", Budget(30, 60)),
        NodeRoute.CLIP_CONFIRMATION: RouteBudget("clip_confirmation", Budget(30, 60)),
        NodeRoute.CREDENTIAL_ROTATION: RouteBudget("credential_rotation", Budget(2, 3_600)),
        NodeRoute.UPDATE_RESULT: RouteBudget("update_result", Budget(10, 3_600)),
    }
)
"""Por nodo (nota de NFR-GOB-33); los tres registros comparten un cubo (``ingest``)."""

ENROLLMENT_NODE_BUDGET: Final = RouteBudget("enrollment", Budget(5, 900))
"""El alta por ``node_id``: 5 cada 15 minutos."""
ENROLLMENT_ORIGIN_BUDGETS: Final[Mapping[EnrollmentWindow, Budget]] = MappingProxyType(
    {EnrollmentWindow.QUARTER: Budget(5, 900), EnrollmentWindow.DAY: Budget(20, 86_400)}
)
"""El alta por origen: 5 cada 15 minutos y 20 al día."""

MINIMUM_PER_MINUTE: Final[Mapping[str, int]] = MappingProxyType({"ingest": 60, "clip_upload": 240})
"""NFR-CTR-02: la plataforma nunca limita por debajo de 60 registros y 240 concesiones por
minuto por nodo."""


def check_minimum() -> list[str]:
    """Presupuestos por debajo del mínimo de NFR-CTR-02 (vacío si ninguno)."""
    problems: list[str] = []
    for route_budget in NODE_BUDGETS.values():
        minimum = MINIMUM_PER_MINUTE.get(route_budget.operation)
        if minimum is not None and route_budget.sustained_per_minute < minimum:
            problems.append(f"{route_budget.operation} por debajo de {minimum} por minuto")
    return problems


if check_minimum():  # pragma: no cover - un cambio de presupuesto lo detiene al importar
    raise RuntimeError("un límite de las rutas del contrato baja del mínimo de NFR-CTR-02")


# --- Freno global --------------------------------------------------------------------------------


def brake_filters(per_minute: int | None) -> dict[str, str]:
    """Los ``filters`` de ``node_rate_brake_set``: ``{"per_minute": "<n>"|"off"}``."""
    if per_minute is None:
        return {"per_minute": BRAKE_OFF}
    if type(per_minute) is not int or not 1 <= per_minute <= MAX_BRAKE_PER_MINUTE:
        raise ValueError(f"el freno debe ser de 1 a {MAX_BRAKE_PER_MINUTE} por minuto")
    return {"per_minute": str(per_minute)}


def parse_brake(filters: object) -> int | None:
    """El límite por minuto de una entrada ``node_rate_brake_set`` (``None``: sin freno)."""
    value = filters.get("per_minute") if isinstance(filters, Mapping) else None
    if value == BRAKE_OFF or not isinstance(value, str) or not value.isascii():
        return None
    if not value.isdigit() or not 1 <= int(value) <= MAX_BRAKE_PER_MINUTE:
        return None
    return int(value)


class BrakeSource(Protocol):
    """El ajuste vigente del freno global (``None``: sin freno)."""

    async def current(self) -> int | None: ...


BRAKE_STATEMENT: Final = text(
    "SELECT filters_json FROM shared.audit_entry"
    " WHERE organization_id = :organization_id AND operation = :operation"
    " ORDER BY occurred_at DESC, chain_sequence DESC LIMIT 1"
)
"""La última ``node_rate_brake_set`` de la proveedora (``audit_entry_operation_occurred``)."""


@repository
class AuditBrakeSource:
    """``BrakeSource`` sobre la auditoría de la proveedora (la escribe ``vigia-admin``)."""

    def __init__(
        self, *, database: LedgerDatabase, provider_context: Callable[[], ScopeContext]
    ) -> None:
        self._database = database
        self._provider_context = provider_context

    async def current(self) -> int | None:
        return await self.read(self._provider_context())

    async def read(self, context: ScopeContext) -> int | None:
        rows = await self._database.read(
            context,
            BRAKE_STATEMENT,
            {
                "organization_id": context.organization_id,
                "operation": AuditOperation.NODE_RATE_BRAKE_SET.value,
            },
        )
        if not rows:
            return None
        filters: Any = rows[0].filters_json
        if isinstance(filters, str):
            try:
                filters = json.loads(filters)
            except ValueError:
                return None
        return parse_brake(filters)


class EmergencyBrake:
    """El freno global con caché corta; un fallo de lectura conserva el último valor."""

    def __init__(
        self, source: BrakeSource, clock: Clock, *, cache_seconds: float = BRAKE_CACHE_SECONDS
    ) -> None:
        self._source = source
        self._clock = clock
        self._cache_seconds = cache_seconds
        self._value: int | None = None
        self._read_at: float | None = None
        self._lock = asyncio.Lock()

    async def budget(self) -> Budget | None:
        """El presupuesto del freno, o ``None`` si no hay freno."""
        if self._stale():
            async with self._lock:
                if self._stale():
                    await self._refresh()
        return None if self._value is None else Budget(self._value, 60)

    def _stale(self) -> bool:
        read_at = self._read_at
        return read_at is None or self._clock.monotonic() - read_at >= self._cache_seconds

    async def _refresh(self) -> None:
        try:
            self._value = await self._source.current()
        except Exception:
            _log.exception("no se pudo leer el freno de las rutas del contrato")
        self._read_at = self._clock.monotonic()


# --- Admisión ------------------------------------------------------------------------------------


class NodeRateLimits:
    """Freno, límite por nodo y límite del alta por origen y por ``node_id``."""

    def __init__(
        self,
        limiter: RateLimiter,
        *,
        brake: EmergencyBrake | None = None,
        origin_secret: bytes | None = None,
        metrics: PlatformMetrics | None = None,
    ) -> None:
        self._limiter = limiter
        self._brake = brake
        self._origin_secret = origin_secret
        self._metrics = metrics

    @property
    def _instruments(self) -> PlatformMetrics:
        return self._metrics if self._metrics is not None else get_metrics()

    def _check(self, route: NodeRoute, key: str, budget: Budget, cause: RateCause) -> None:
        verdict = self._limiter.check(key, budget)
        if isinstance(verdict, Limited):
            self._instruments.rate_limited_total.add(
                1, {"route": route.path, "rate_limit": cause.value}
            )
            raise NodeRejection(
                RejectionCode.RATE_LIMITED,
                retry_after_seconds=contract_retry_after(verdict.retry_after_seconds),
            )

    async def admit(self, route: NodeRoute, *, node_id: uuid.UUID | None, address: str) -> None:
        """Freno y límite de la ruta; ``NodeRejection(rate_limited)`` si no entra."""
        if self._brake is not None:
            budget = await self._brake.budget()
            if budget is not None:
                self._check(route, BRAKE_KEY, budget, RateCause.BRAKE)
        if route is NodeRoute.ENROLLMENT:
            for window, budget in ENROLLMENT_ORIGIN_BUDGETS.items():
                key = enrollment_origin_key(address, window, secret=self._origin_secret)
                self._check(route, key, budget, RateCause.ORIGIN)
            return
        route_budget = NODE_BUDGETS[route]
        if node_id is not None:
            key = node_key(node_id, route_budget.operation)
            self._check(route, key, route_budget.budget, RateCause.NODE)

    def admit_enrollment_node(self, node_id: uuid.UUID) -> None:
        """El alta por ``node_id`` (5 cada 15 minutos): la llama la ruta del alta (TASK-219)."""
        key = node_key(node_id, ENROLLMENT_NODE_BUDGET.operation)
        self._check(NodeRoute.ENROLLMENT, key, ENROLLMENT_NODE_BUDGET.budget, RateCause.NODE)
