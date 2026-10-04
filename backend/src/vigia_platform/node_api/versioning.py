"""Paso (1) de la verificación previa: la versión del contrato de la petición (BR-GOB-84, 87).

La decisión es la de ``is_compatible`` del paquete de U-01 (BR-CTR-18 a 22; PR-GOB-07), nunca una
reimplementación, con la versión del contrato que implementa la plataforma
(``vigia_contracts.__version__`` del paquete fijado: la plataforma se actualiza antes que los
nodos):

- sin ``X-Vigia-Contract-Version``: ``contract_version_unsupported`` (BLM §4.2);
- cabecera repetida o que no es ``MAJOR.MINOR.PATCH``: ``schema_invalid`` 400 (A-37, cabecera
  inválida) con ``field`` = el nombre de la cabecera;
- rechazo de la política: el ``RejectionResponse`` de ``version_rejection`` (``code``
  ``contract_version_unsupported`` o ``contract_version_retired`` con ``compatibility_result``;
  ``rejected_newer`` **nunca** es un ``code``) y su mensaje, que nombra la versión presentada y
  las aceptadas (BR-CTR-22).

El instante de la decisión sale del ``Clock`` de la verificación, nunca de la hora del sistema.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from vigia_contracts.models.api import ContractValidationError
from vigia_contracts.models.enumerations import (
    CompatibilityResult,
    RejectionCode,
    RejectionCompatibilityResult,
)
from vigia_contracts.versioning import (
    CONTRACT_VERSION,
    CONTRACT_VERSION_HEADER,
    DEFAULT_COEXISTENCE_DAYS,
    DEFAULT_WINDOW,
    Version,
    is_accepted,
    is_compatible,
    version_rejection,
)

from vigia_platform.node_api.rejections import NodeRejection

__all__ = [
    "CONTRACT_VERSION_HEADER",
    "PLATFORM_CONTRACT_VERSION",
    "RetiringMinor",
    "VersionPolicy",
    "check_version",
]

PLATFORM_CONTRACT_VERSION: Final = str(CONTRACT_VERSION)
"""La versión del contrato que implementa la plataforma: va en ``X-Vigia-Contract-Version`` de
toda respuesta a un nodo (BR-CTR-18)."""


@dataclass(frozen=True, slots=True)
class RetiringMinor:
    """Una menor en aviso de retiro (``ConformanceProfile.retiring``)."""

    version: str
    retires_at: str
    """Marca ``Timestamp`` del contrato (``2026-12-01T00:00:00.000Z``)."""


@dataclass(frozen=True, slots=True)
class VersionPolicy:
    """La política de versiones de la plataforma (BR-CTR-19 y 20)."""

    current: Version = CONTRACT_VERSION
    window: int = DEFAULT_WINDOW
    retiring: tuple[RetiringMinor, ...] = ()
    latest_minors: Mapping[int, int] = field(default_factory=dict)
    major_published_at: Mapping[int, str] = field(default_factory=dict)
    coexistence_days: int = DEFAULT_COEXISTENCE_DAYS

    def retires_at(self, version: Version) -> str | None:
        """La fecha de retiro más temprana anunciada para la menor de ``version``."""
        dates = [
            item.retires_at
            for item in self.retiring
            if Version.parse(item.version, field="retiring").series == version.series
        ]
        return min(dates) if dates else None


def check_version(values: list[str], policy: VersionPolicy, today: datetime) -> CompatibilityResult:
    """El resultado aceptado de la política para la cabecera ``values`` (todas sus apariciones).

    Raises:
        NodeRejection: la petición no pasa el paso (1).
    """
    if not values:
        raise NodeRejection(
            RejectionCode.CONTRACT_VERSION_UNSUPPORTED,
            field=CONTRACT_VERSION_HEADER,
            body_level=True,
        )
    if len(values) != 1:
        raise NodeRejection(
            RejectionCode.SCHEMA_INVALID, field=CONTRACT_VERSION_HEADER, body_level=True
        )
    try:
        declared = Version.parse(values[0])
    except ContractValidationError:
        raise NodeRejection(
            RejectionCode.SCHEMA_INVALID, field=CONTRACT_VERSION_HEADER, body_level=True
        ) from None
    result = is_compatible(
        declared,
        policy.current,
        today=today,
        window=policy.window,
        retiring=policy.retiring,
        latest_minors=policy.latest_minors,
        major_published_at=policy.major_published_at,
        coexistence_days=policy.coexistence_days,
    )
    if is_accepted(result):
        return result
    rejection = version_rejection(
        result,
        declared,
        policy.current,
        window=policy.window,
        retires_at=policy.retires_at(declared),
    )
    raise NodeRejection(
        RejectionCode(rejection.code),
        field=CONTRACT_VERSION_HEADER,
        body_level=True,
        message_es=rejection.message_es,
        compatibility_result=RejectionCompatibilityResult(result.value),
    )
