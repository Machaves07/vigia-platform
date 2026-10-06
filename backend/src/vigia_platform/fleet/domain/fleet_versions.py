"""Versiones de flota (C-PLA-16, LC-GOB-17): la versión objetivo y el resultado que reporta el nodo.

``domain-entities.md`` de U-03 §3.11 (nota D-5) y §3.12 (nota T-05), BR-GOB-101 a 104 con su nota
del 2026-09-23 y BL §2.7:

- **Ventana del contrato** (BR-GOB-101): la versión objetivo se publica solo si está dentro de la
  ventana de compatibilidad del contrato **en el instante de publicar**. La decisión es la de
  ``is_compatible`` del paquete de U-01 con la política de la plataforma (la misma que aplica la
  verificación previa de ``node_api`` y que PR-GOB-07 cubre por oráculo), nunca una
  reimplementación: ``accepted`` o ``accepted_with_notice`` publican; cualquier rechazo es
  ``version_outside_contract_window``. Una versión que después sale de la ventana no se retira
  sola: el aviso al nodo lo da ``contract_notice`` del latido.
- **Ventana de mantenimiento** (D-5): ``{from, to}`` con ``to > from``; es **informativa** en el
  piloto: se guarda en la publicación y en el registro, y **no** viaja en el latido ni en los
  eventos.
- **Versión** que elige la plataforma: ``ReleaseVersion`` (``SemVer`` en minúsculas,
  ``record_types``), la única que admiten los eventos (sin texto libre). La que reporta el nodo
  en ``update-results`` es ``SemVer`` del contrato; si no cabe en ``ReleaseVersion`` el evento
  ``update_result_received`` no la podría llevar, así que se rechaza como ``schema_invalid`` en
  ``target_version`` (decisión declarada en TASK-226).
- **Resultado** (nota T-05): ``applied``, ``reverted`` o ``failed``, los tres valores de
  ``update_outcome``. ``reverted`` y ``failed`` no son errores de la plataforma: son datos del
  inventario.

Dominio puro: sin base y sin hora del sistema (el instante llega del ``Clock`` del servicio).
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol

from pydantic import TypeAdapter, ValidationError
from vigia_contracts.models.api import ContractValidationError
from vigia_contracts.versioning import Version, is_accepted, is_compatible

from vigia_platform.fleet.domain.enums import UpdateResult
from vigia_platform.fleet.record_types import MAX_TARGET_NODES, ReleaseVersion
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

__all__ = [
    "MAX_TARGET_NODES",
    "PUBLISHED_RECORD_TYPE",
    "RESULT_RECORD_TYPE",
    "TARGET_PUBLISHED_EVENT",
    "UPDATE_RESULT_EVENT",
    "MaintenanceWindow",
    "NodeGroup",
    "TargetVersionInvalid",
    "TargetVersionPublication",
    "UpdateReport",
    "VersionWindowPolicy",
    "is_release_version",
    "outcome_of",
    "within_contract_window",
]

PUBLISHED_RECORD_TYPE: Final = "node_target_version_published"
RESULT_RECORD_TYPE: Final = "update_result_received"
TARGET_PUBLISHED_EVENT: Final = "target_version_published"
UPDATE_RESULT_EVENT: Final = "update_result_received"

_RELEASE: Final[TypeAdapter[str]] = TypeAdapter(ReleaseVersion)


class TargetVersionInvalid(Exception):
    """El cuerpo de la publicación no cumple una regla del dominio (``invalid_request``)."""


class NodeGroup(enum.StrEnum):
    """``group`` de ``POST /fleet/target-versions`` (decisión del redactor, Notes de TASK-226)."""

    PLANT = "plant"
    """Todos los nodos no revocados ni dados de baja de la planta del alcance."""


class VersionWindowPolicy(Protocol):
    """La política de versiones de la plataforma (``node_api.versioning.VersionPolicy``)."""

    @property
    def current(self) -> Version: ...
    @property
    def window(self) -> int: ...
    @property
    def retiring(self) -> Iterable[Any]: ...
    @property
    def latest_minors(self) -> Mapping[int, int]: ...
    @property
    def major_published_at(self) -> Mapping[int, str]: ...
    @property
    def coexistence_days(self) -> int: ...


def is_release_version(value: object) -> bool:
    """¿Es ``value`` una ``ReleaseVersion`` (``SemVer`` en minúsculas que admiten los eventos)?"""
    try:
        _RELEASE.validate_python(value, strict=True)
    except ValidationError:
        return False
    return True


def within_contract_window(version: str, policy: VersionWindowPolicy, today: datetime) -> bool:
    """BR-GOB-101: ¿acepta la política del contrato ``version`` en el instante ``today``?

    ``is_compatible`` de U-01 con la política de la plataforma: ``accepted`` o
    ``accepted_with_notice``. Una versión que la política no sabe leer queda fuera de la ventana.
    """
    try:
        result = is_compatible(
            Version.parse(version, field="target_version"),
            policy.current,
            today=today,
            window=policy.window,
            retiring=policy.retiring,
            latest_minors=policy.latest_minors,
            major_published_at=policy.major_published_at,
            coexistence_days=policy.coexistence_days,
        )
    except ContractValidationError:
        return False
    return is_accepted(result)


@dataclass(frozen=True, slots=True)
class MaintenanceWindow:
    """``{from, to}`` informativa en el piloto (D-5): ``to > from``, al milisegundo."""

    starts_at: datetime
    ends_at: datetime

    @classmethod
    def of(cls, starts_at: datetime, ends_at: datetime) -> MaintenanceWindow:
        """La ventana truncada al milisegundo del contrato; ``TargetVersionInvalid`` si vacía."""
        try:
            start, end = to_millisecond(starts_at), to_millisecond(ends_at)
        except ValueError:
            raise TargetVersionInvalid("la ventana de mantenimiento lleva zona horaria") from None
        if end <= start:
            raise TargetVersionInvalid("la ventana de mantenimiento termina después de empezar")
        return cls(start, end)

    def content(self) -> dict[str, str]:
        """``MaintenanceWindow`` del registro (``starts_at``/``ends_at`` = ``from``/``to``)."""
        return {
            "starts_at": format_timestamp(self.starts_at),
            "ends_at": format_timestamp(self.ends_at),
        }


@dataclass(frozen=True, slots=True)
class TargetVersionPublication:
    """``TargetVersionPublication`` (DE §3.11) lista para escribirse en una transacción."""

    publication_id: uuid.UUID
    plant_id: uuid.UUID
    target_version: str
    node_ids: tuple[uuid.UUID, ...]
    window: MaintenanceWindow
    published_by: uuid.UUID
    published_at: datetime
    ledger_record_id: uuid.UUID

    def __post_init__(self) -> None:
        if not is_release_version(self.target_version):
            raise TargetVersionInvalid("la versión objetivo no es MAJOR.MINOR.PATCH en minúsculas")
        if not 1 <= len(self.node_ids) <= MAX_TARGET_NODES:
            raise TargetVersionInvalid("la publicación alcanza de 1 a 100 nodos")
        if len(set(self.node_ids)) != len(self.node_ids):
            raise TargetVersionInvalid("la publicación no repite nodos")

    def record_content(self) -> dict[str, Any]:
        """``node_target_version_published`` ``{publication_id, target_version, node_ids[],
        maintenance_window}``."""
        return {
            "publication_id": str(self.publication_id),
            "target_version": self.target_version,
            "node_ids": [str(node) for node in self.node_ids],
            "maintenance_window": self.window.content(),
        }

    def event_payloads(self) -> list[dict[str, str]]:
        """Un ``target_version_published`` ``{node_id, target_version}`` por nodo (interfaces
        §2): sin la ventana de mantenimiento (D-5)."""
        return [
            {"node_id": str(node), "target_version": self.target_version} for node in self.node_ids
        ]


def outcome_of(value: object) -> UpdateResult:
    """``update_outcome`` del contrato → ``update_result`` de la plataforma (nota T-05)."""
    return UpdateResult(str(getattr(value, "value", value)))


@dataclass(frozen=True, slots=True)
class UpdateReport:
    """``UpdateResult`` (DE §3.12): lo que se escribe del mensaje del nodo.

    ``reported_at`` es la marca de **recepción** de la plataforma (nota de §3.12), no la
    ``verified_at`` del nodo.
    """

    update_result_id: uuid.UUID
    node_id: uuid.UUID
    target_version: str
    result: UpdateResult
    reported_at: datetime

    def record_content(self) -> dict[str, str]:
        """``update_result_received`` ``{update_result_id, node_id, target_version, result,
        reported_at}``."""
        return {
            "update_result_id": str(self.update_result_id),
            "node_id": str(self.node_id),
            "target_version": self.target_version,
            "result": self.result.value,
            "reported_at": format_timestamp(self.reported_at),
        }

    def event_payload(self) -> dict[str, str]:
        """``update_result_received`` ``{node_id, target_version, result}`` (interfaces §2)."""
        return {
            "node_id": str(self.node_id),
            "target_version": self.target_version,
            "result": self.result.value,
        }

    def same_as(self, other: UpdateReport) -> bool:
        """¿Mismo contenido (nodo, versión y resultado) que ``other``, con el mismo
        identificador? La marca de recepción es de cada intento y no cuenta."""
        return (
            self.update_result_id == other.update_result_id
            and self.node_id == other.node_id
            and self.target_version == other.target_version
            and self.result is other.result
        )
