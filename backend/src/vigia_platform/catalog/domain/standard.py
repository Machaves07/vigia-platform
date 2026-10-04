"""``DeclaredStandardVersion``: el estándar tal como la planta lo declaró (DE §2.2 ⛓🔒).

Una versión emitida **nunca** se edita: un cambio de texto, de predicado o de parámetros crea la
versión siguiente (``version`` + 1) en una versión nueva del catálogo, y la anterior sigue
consultable con ``retired_in_catalog_version`` (BR-GOB-04, 05; H-42). Retirar un estándar es lo
mismo sin sucesora (BR-GOB-09).

**Vigencia** (PR-GOB-06, parte de catálogo): una versión rige en el intervalo semiabierto
``[effective_from, effective_until)``, en UTC. ``effective_from`` es el ``issued_at`` de la versión
del catálogo donde nace; ``effective_until`` es el ``issued_at`` de la versión del catálogo que la
retira o la sustituye (``None`` mientras sigue vigente). ``standard_valid_at`` resuelve la versión
vigente de un estándar en un instante: la que usan la ingesta (BR-GOB-08) y ``CatalogQueryPort``.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from vigia_contracts.models.enumerations import PredicateFamily

__all__ = [
    "MAX_DECLARED_TEXT_CHARS",
    "MAX_STANDARDS",
    "MAX_TITLE_CHARS",
    "TIER_POLICY",
    "DeclaredBy",
    "DeclaredStandardVersion",
    "contract_timestamp",
    "standard_valid_at",
]

MAX_STANDARDS: Final = 32
"""Estándares vigentes por catálogo (``ZoneCatalog.standards``: 1 a 32; BR-GOB-04)."""
MAX_TITLE_CHARS: Final = 120
MAX_DECLARED_TEXT_CHARS: Final = 4000
"""``title_es`` ≤ 120 y ``declared_text`` ≤ 4 000 (el único texto libre del contrato)."""
TIER_POLICY: Final = "tier_1_when_signal_valid"
"""Constante del contrato: el tier no es configurable."""
_TECHNICAL_ID: Final = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


def contract_timestamp(moment: datetime) -> str:
    """``Timestamp`` del contrato: ISO 8601 UTC con milisegundos y ``Z``."""
    if not isinstance(moment, datetime) or moment.utcoffset() is None:
        raise ValueError("el instante debe llevar zona horaria")
    moment = moment.astimezone(UTC)
    moment = moment.replace(microsecond=moment.microsecond // 1000 * 1000)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class DeclaredBy:
    """Firmante responsable del estándar: usuario de la plataforma, nunca persona observada."""

    user_id: uuid.UUID
    display_name: str
    role: str

    def __post_init__(self) -> None:
        if type(self.user_id) is not uuid.UUID:
            raise TypeError("user_id debe ser uuid.UUID")
        if not isinstance(self.display_name, str) or not 1 <= len(self.display_name) <= 120:
            raise ValueError("display_name debe tener de 1 a 120 caracteres")
        if not isinstance(self.role, str) or not _TECHNICAL_ID.fullmatch(self.role):
            raise ValueError("role debe ser un identificador técnico")

    def as_json(self) -> dict[str, str]:
        return {"user_id": str(self.user_id), "display_name": self.display_name, "role": self.role}


@dataclass(frozen=True, slots=True, kw_only=True)
class DeclaredStandardVersion:
    """Una versión de un estándar declarado, con su intervalo de vigencia."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    standard_id: uuid.UUID
    version: int
    family: PredicateFamily
    title_es: str
    declared_text: str
    declared_by: DeclaredBy
    effective_from: datetime
    predicate: Mapping[str, Any]
    catalog_version: int
    reason_es: str
    retired_in_catalog_version: int | None = None
    effective_until: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("organization_id", "plant_id", "zone_id", "standard_id"):
            if type(getattr(self, name)) is not uuid.UUID:
                raise TypeError(f"{name} debe ser uuid.UUID")
        for name in ("version", "catalog_version"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} debe ser un entero ≥ 1")
        object.__setattr__(self, "family", PredicateFamily(self.family))
        if not isinstance(self.declared_by, DeclaredBy):
            raise TypeError("declared_by debe ser DeclaredBy")
        if self.effective_from.utcoffset() is None:
            raise ValueError("effective_from debe llevar zona horaria")
        retired = self.retired_in_catalog_version
        if retired is not None and (type(retired) is not int or retired <= self.catalog_version):
            raise ValueError("retired_in_catalog_version es posterior a la versión donde nace")
        until = self.effective_until
        if (retired is None) != (until is None):
            raise ValueError("effective_until existe si y solo si la versión está retirada")
        if until is not None and (until.utcoffset() is None or until < self.effective_from):
            raise ValueError("effective_until no precede a effective_from")

    @property
    def current(self) -> bool:
        """¿Sigue vigente (ninguna versión del catálogo la retiró)?"""
        return self.retired_in_catalog_version is None

    def valid_at(self, moment: datetime) -> bool:
        """¿Rige en ``moment``? Intervalo semiabierto ``[effective_from, effective_until)``."""
        if not isinstance(moment, datetime) or moment.utcoffset() is None:
            raise ValueError("el instante debe llevar zona horaria")
        if moment < self.effective_from:
            return False
        return self.effective_until is None or moment < self.effective_until

    def contract(self) -> dict[str, Any]:
        """La versión como ``DeclaredStandard`` del contrato (forma JSON)."""
        return {
            "standard_id": str(self.standard_id),
            "version": self.version,
            "family": self.family.value,
            "title_es": self.title_es,
            "declared_text": self.declared_text,
            "declared_by": self.declared_by.as_json(),
            "effective_from": contract_timestamp(self.effective_from),
            "tier_policy": TIER_POLICY,
            "predicate": dict(self.predicate),
        }


def standard_valid_at(
    history: Iterable[DeclaredStandardVersion], standard_id: uuid.UUID, moment: datetime
) -> DeclaredStandardVersion | None:
    """La versión de ``standard_id`` que rige en ``moment``, o ``None`` si ninguna.

    Las vigencias de un estándar no se solapan por construcción (cada versión cierra la anterior
    en el mismo instante); una historia con dos vigentes a la vez es un estado roto:
    ``ValueError`` en lugar de elegir una.
    """
    found = [v for v in history if v.standard_id == standard_id and v.valid_at(moment)]
    if len(found) > 1:
        raise ValueError("historia del estándar con vigencias solapadas")
    return found[0] if found else None
