"""``CatalogQueryPort`` y ``GateQueryPort``: el catálogo y las compuertas para U-04 (LC-GOB-23a;
interfaces §1.1, §1.2 y «Versión 1.5»; PAT-GOB-REN-04).

En proceso, **solo lectura**, siempre con ``ScopeContext`` (sin él, ``ContextAbsent``), **una
sentencia** por operación, sin caché y sin paginar salvo ``catalog_history`` (U-04 no puede
paginar ni cachear: son el presupuesto duro de su bandeja y de su exportación, NFR-GOB-04).

- ``CatalogQueryPort``, nueve operaciones (errata de LC-GOB-01: nueve, no ocho):
  ``current_catalog``, ``catalog_at``, ``standard_version``, ``standard_at``,
  ``catalog_history`` (la única paginada: por clave, hasta 200), ``single_occupancy``,
  ``single_occupancy_many`` (hasta 50 zonas), ``standards_at_many`` (hasta 200 referencias) y
  ``regression_state`` (``cause`` admite ``framing_recaptured``).
- ``GateQueryPort``, seis operaciones: ``state``, ``states_by_plant``, ``state_at`` (siempre desde
  la historia, BR-GOB-20), ``gate_history`` (intervalos que se solapan con ``[from, to]``, hasta
  366 días), ``plant_policy`` y ``current_agreement``.

**Alcance** (como ``CoveragePort`` de U-02 y ``FleetQueryPort``): la RLS limita a la organización
del contexto y la zona tiene que estar en ``allowed_scopes`` (la organización entera, su planta o
ella misma); una planta, por la organización, por ella misma o por una zona suya. Inexistente, de
otra organización o fuera del alcance responden igual: ``ResourceNotFound`` (``not_found``, nunca
``forbidden``). También lo que no existe en la zona visible: una zona sin catálogo vigente en el
instante, un estándar que no es de la zona o sin versión vigente en el instante.

**Formas por lote y por rango** (decisión del redactor de TASK-213, por la regla «nunca trunca» de
PAT-GOB-REN-04): un solo elemento inexistente o fuera de alcance hace que **toda** la llamada
responda ``ResourceNotFound``, nunca un resultado parcial; superar un tope lanza
``PortLimitExceeded`` antes de consultar, también sin resultado parcial. La salida sigue el orden
declarado de la entrada (PR-GOB-30) y repite los elementos repetidos.

``single_occupancy`` y ``aggregation_window_minutes`` solo salen por ``single_occupancy`` y
``single_occupancy_many`` (y dentro de la versión del catálogo, que nunca viaja al nodo con ellos;
NFR-GOB-38). Ninguna operación agrega horas por responsable (BR-GOB-46).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Protocol

from vigia_platform.catalog.domain.catalog_version import ZoneCatalogVersion
from vigia_platform.catalog.domain.enums import AgreementStatus, CatalogChangedField, GateKind
from vigia_platform.catalog.domain.gates import GateInterval, ZoneGateState
from vigia_platform.catalog.domain.regression import WalkTestRegression
from vigia_platform.catalog.domain.standard import DeclaredStandardVersion
from vigia_platform.shared.context import Role, ScopeContext

__all__ = [
    "MAX_CATALOG_HISTORY_PAGE",
    "MAX_GATE_HISTORY_RANGE",
    "MAX_SINGLE_OCCUPANCY_ZONES",
    "MAX_STANDARD_REFS",
    "AgreementSignatory",
    "CatalogHistoryEntry",
    "CatalogHistoryPage",
    "CatalogQueryPort",
    "CurrentAgreement",
    "GateQueryPort",
    "PlantPolicyState",
    "PortLimitExceeded",
    "PortQueryInvalid",
    "SingleOccupancy",
    "StandardRef",
]

MAX_SINGLE_OCCUPANCY_ZONES: Final = 50
"""Tope de ``single_occupancy_many`` (interfaces v1.1 §1.1)."""
MAX_STANDARD_REFS: Final = 200
"""Tope de ``standards_at_many`` (interfaces v1.1 §1.1)."""
MAX_GATE_HISTORY_RANGE: Final = timedelta(days=366)
"""Tope de ``gate_history`` (interfaces v1.1 §1.2)."""
MAX_CATALOG_HISTORY_PAGE: Final = 200
"""Página máxima de ``catalog_history`` (convención de las interfaces)."""


class PortQueryInvalid(ValueError):
    """Entrada mal formada: tipo, instante sin zona horaria, ``from`` posterior a ``to``, página
    fuera de 1 a 200 o lote que no es una lista."""


class PortLimitExceeded(ValueError):
    """Una forma por lote o por rango supera su tope: error, **nunca** un resultado truncado.

    El mensaje es genérico: nombra la operación y el tope, nunca lo recibido.
    """

    def __init__(self, operation: str, limit: str) -> None:
        super().__init__(f"{operation} admite {limit} como mucho")
        self.operation = operation
        self.limit = limit


# --- Salidas -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogHistoryEntry:
    """Una versión en ``catalog_history``: ``{catalog_version, issued_at, issued_by, reason_es,
    changed_fields[]}``."""

    catalog_version: int
    issued_at: datetime
    issued_by: uuid.UUID
    reason_es: str
    changed_fields: tuple[CatalogChangedField, ...]


@dataclass(frozen=True, slots=True)
class CatalogHistoryPage:
    """Versiones de la más reciente a la más antigua y el cursor de la siguiente página."""

    items: tuple[CatalogHistoryEntry, ...]
    next_cursor: int | None
    """``catalog_version`` exclusivo de la página siguiente; ``None`` si no hay más."""


@dataclass(frozen=True, slots=True, kw_only=True)
class SingleOccupancy:
    """La marca unipersonal de la zona en la versión del catálogo vigente (o en ``at``)."""

    zone_id: uuid.UUID
    single_occupancy: bool
    aggregation_window_minutes: int
    catalog_version: int
    issued_at: datetime


@dataclass(frozen=True, slots=True)
class StandardRef:
    """Una referencia de ``standards_at_many``: ``{zone_id, standard_id, at}``."""

    zone_id: uuid.UUID
    standard_id: uuid.UUID
    at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class PlantPolicyState:
    """``PlantPolicy`` de interfaces §1.2: con ``loaded = False`` los demás campos son ``None``
    (ninguna zona de la planta pasa la compuerta de uso, H-26)."""

    loaded: bool
    policy_id: uuid.UUID | None = None
    version: int | None = None
    signed_at: datetime | None = None
    signed_by_display_name: str | None = None
    legal_opinion_reference: str | None = None
    document_sha256: str | None = None
    criteria_summary_es: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AgreementSignatory:
    """Un firmante del acuerdo vigente: ``{role, user_id, display_name, confirmed_at}``."""

    role: Role
    user_id: uuid.UUID
    display_name: str | None
    confirmed_at: datetime | None


@dataclass(frozen=True, slots=True, kw_only=True)
class CurrentAgreement:
    """``UseAgreement`` vigente de la zona (``status = approved``, interfaces §1.2)."""

    agreement_id: uuid.UUID
    zone_id: uuid.UUID
    status: AgreementStatus
    approved_at: datetime
    approved_by: uuid.UUID
    signatories: tuple[AgreementSignatory, ...]
    document_sha256: str | None
    replaces_agreement_id: uuid.UUID | None


# --- Puertos -----------------------------------------------------------------------------------


class CatalogQueryPort(Protocol):
    """Puerto de lectura del catálogo para U-04 (interfaces §1.1)."""

    async def current_catalog(
        self, context: ScopeContext, zone_id: uuid.UUID
    ) -> ZoneCatalogVersion: ...

    async def catalog_at(
        self, context: ScopeContext, zone_id: uuid.UUID, at: datetime
    ) -> ZoneCatalogVersion: ...

    async def standard_version(
        self, context: ScopeContext, zone_id: uuid.UUID, standard_id: uuid.UUID, version: int
    ) -> DeclaredStandardVersion: ...

    async def standard_at(
        self, context: ScopeContext, zone_id: uuid.UUID, standard_id: uuid.UUID, at: datetime
    ) -> DeclaredStandardVersion: ...

    async def catalog_history(
        self,
        context: ScopeContext,
        zone_id: uuid.UUID,
        cursor: int | None = None,
        limit: int = MAX_CATALOG_HISTORY_PAGE,
    ) -> CatalogHistoryPage: ...

    async def single_occupancy(
        self, context: ScopeContext, zone_id: uuid.UUID, at: datetime | None = None
    ) -> SingleOccupancy: ...

    async def single_occupancy_many(
        self, context: ScopeContext, zone_ids: Sequence[uuid.UUID], at: datetime | None = None
    ) -> tuple[SingleOccupancy, ...]: ...

    async def standards_at_many(
        self, context: ScopeContext, refs: Sequence[StandardRef]
    ) -> tuple[DeclaredStandardVersion, ...]: ...

    async def regression_state(
        self, context: ScopeContext, zone_id: uuid.UUID
    ) -> WalkTestRegression: ...


class GateQueryPort(Protocol):
    """Puerto de lectura de las compuertas, la política y el acuerdo para U-04 (§1.2)."""

    async def state(self, context: ScopeContext, zone_id: uuid.UUID) -> ZoneGateState: ...

    async def states_by_plant(
        self, context: ScopeContext, plant_id: uuid.UUID
    ) -> tuple[ZoneGateState, ...]: ...

    async def state_at(
        self, context: ScopeContext, zone_id: uuid.UUID, gate: GateKind, at: datetime
    ) -> GateInterval | None: ...

    async def gate_history(
        self, context: ScopeContext, zone_id: uuid.UUID, start: datetime, end: datetime
    ) -> tuple[GateInterval, ...]: ...

    async def plant_policy(
        self, context: ScopeContext, plant_id: uuid.UUID
    ) -> PlantPolicyState: ...

    async def current_agreement(
        self, context: ScopeContext, zone_id: uuid.UUID
    ) -> CurrentAgreement | None: ...
