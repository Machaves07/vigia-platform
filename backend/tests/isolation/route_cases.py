"""Tipos del catálogo de casos de aislamiento por ruta (``test_route_isolation.py``, PR-NUC-01).

Viven aquí, y no en la prueba, para que los casos de U-04 (``laz_cases.py``) se escriban sin
importar el módulo de la prueba (TASK-302): ``Kind`` (tipo de aislamiento), ``Ids`` (los
identificadores de una organización), ``Call`` y ``Builder`` (la petición de un caso) y ``Case``.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from tests.factories import uuid7

__all__ = ["Builder", "Call", "Case", "Ids", "Kind"]


class Kind(enum.Enum):
    RESOURCE = "resource"
    OWN = "own"
    SESSION = "session"
    PROVIDER_ONLY = "provider_only"
    PUBLIC = "public"
    NODE = "node"
    """Ruta del contrato (``node_route``, A-51): con el certificado de un nodo de A y un recurso
    de B responde ``node_zone_mismatch`` o ``not_found`` (TASK-206; los casos de cada ruta
    publicada los añade su tarea y TASK-228)."""


@dataclass(frozen=True)
class Ids:
    """Identificadores de una organización: los de B (conocidos) o inexistentes."""

    organization: uuid.UUID
    plant: uuid.UUID
    zone: uuid.UUID
    user: uuid.UUID
    assignment: uuid.UUID
    concession: uuid.UUID
    record: uuid.UUID
    evidence: uuid.UUID
    event: uuid.UUID
    label: uuid.UUID
    node: uuid.UUID
    """Nodo ``declared`` con ficha de flota (rutas de SCR-07, VIG-147)."""
    spare_zone: uuid.UUID
    """Zona de la planta sin nodo: se asigna y se retira en la misma pasada."""
    retired_node: uuid.UUID
    """Nodo revocado con ficha de flota: el de la baja."""
    agreement: uuid.UUID
    """Acuerdo de uso ``pending_signatures`` de la zona (VIG-149)."""
    walk_test: uuid.UUID
    """Sesión de walk-test ``in_progress`` de la zona, con matriz vacía (VIG-150)."""
    step: uuid.UUID
    """Paso abierto de esa sesión (VIG-150)."""
    commissioning_record: uuid.UUID
    """Acta cerrada de otra sesión de la zona (VIG-158)."""
    finding: uuid.UUID = field(default_factory=uuid7)
    detection: uuid.UUID = field(default_factory=uuid7)
    action: uuid.UUID = field(default_factory=uuid7)
    analysis: uuid.UUID = field(default_factory=uuid7)
    assistant_request: uuid.UUID = field(default_factory=uuid7)
    declaration: uuid.UUID = field(default_factory=uuid7)
    attachment: uuid.UUID = field(default_factory=uuid7)
    export: uuid.UUID = field(default_factory=uuid7)
    package: uuid.UUID = field(default_factory=uuid7)
    sample_item: uuid.UUID = field(default_factory=uuid7)
    commitment: uuid.UUID = field(default_factory=uuid7)
    notification: uuid.UUID = field(default_factory=uuid7)
    period: str = "2026-01"

    @classmethod
    def missing(cls) -> Ids:
        return cls(**{name: uuid7() for name in cls.__dataclass_fields__ if name != "period"})

    def values(self) -> tuple[uuid.UUID, ...]:
        return tuple(getattr(self, name) for name in self.__dataclass_fields__ if name != "period")


@dataclass(frozen=True)
class Call:
    method: str
    path: str
    params: dict[str, str] | None = None
    json: Any = None


Builder = Callable[[Ids], Call]


@dataclass(frozen=True)
class Case:
    kind: Kind
    call: Builder | None = None
    why: str = ""
    own_trail: bool = False
    """La respuesta lista la auditoría de la organización del contexto: incluye las entradas de
    las peticiones de la propia persona que nombró ese identificador (su rastro, no datos de B),
    que no cuentan al comparar."""
