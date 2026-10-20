"""``ExposureIndicator``, sus textos fijos y ``ExposurePort`` (TASK-322; DE §3.5 con la nota 4 de
NFR Design; BR-LAZ-119 a 123).

``ExposurePort`` es lo que consume el paquete mensual (VIG-239): el mismo cálculo que sirve
``GET /accreditation/exposure``, sin la comprobación de permiso de la persona (la tarea lo invoca
con su propio contexto y la seguridad a nivel de fila la limita igual).

**Textos fijos** (BR-LAZ-123): ``USAGE_NOTICE_ES`` acompaña a toda presentación y declara que la
cifra es un hecho medido y un insumo interno de priorización; nunca una meta de incentivo, una
medida de eficiencia ni una promesa. Los motivos de ``not_computable`` y ``partial`` dicen por qué
no se puede afirmar un número completo; ninguno afirma que no ocurrió nada.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol

from vigia_platform.accreditation.exposure.compute import HourlyBucket
from vigia_platform.loop.domain.enums import ExposureStatus
from vigia_platform.shared.context import ScopeContext

__all__ = [
    "NO_CATALOG_REASON_ES",
    "NO_HARD_SIGNAL_REASON_ES",
    "USAGE_NOTICE_ES",
    "ExposureIndicator",
    "ExposurePort",
    "partial_reason_es",
]

USAGE_NOTICE_ES: Final = (
    "Este indicador es un hecho medido: el tiempo observado en que hubo presencia en la zona con "
    "energía peligrosa a la vez, en hallazgos ya confirmados. Es un insumo interno para priorizar "
    "acciones de ingeniería. No es una meta de incentivo ni de bonificación, no mide eficiencia ni "
    "desempeño y no promete ningún resultado futuro."
)
"""Salvaguardas (c) y (d) de RF-PLA-19: acompaña siempre al indicador."""

NO_HARD_SIGNAL_REASON_ES: Final = (
    "La zona no declara ninguna señal dura en su catálogo: sin el estado de la máquina no se "
    "distingue la presencia de la exposición, así que el indicador no se calcula."
)
NO_CATALOG_REASON_ES: Final = (
    "La zona no tenía un catálogo vigente al final del periodo: no hay señal dura declarada y el "
    "indicador no se calcula."
)


def _percent_es(value: float) -> str:
    return f"{value:.1f}".replace(".", ",")


def partial_reason_es(coverage_percent: float, threshold_percent: int) -> str:
    """Por qué la cifra es parcial: la cobertura observable del periodo no llega al umbral."""
    return (
        f"La cobertura observable del periodo es del {_percent_es(coverage_percent)} %, por debajo "
        f"del umbral de la planta ({threshold_percent} %). La cifra solo cuenta lo que se observó: "
        "el tiempo no observado, también el del periodo que aún no ha transcurrido, no está en "
        "ella."
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ExposureIndicator:
    """El indicador de una zona y un mes (DE §3.5).

    Con ``not_computable`` no hay ninguna cifra (``None``, nunca cero; BR-LAZ-115) ni fila
    guardada (``exposure_id`` nulo). Con ``computed`` o ``partial`` lleva siempre las tres cifras,
    la línea de tiempo de cobertura del mismo periodo (BR-LAZ-121) y la distribución horaria.
    """

    exposure_id: uuid.UUID | None
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    period: str
    """``AAAA-MM`` en la zona horaria de la planta."""
    period_from: datetime
    period_to: datetime
    status: ExposureStatus
    confirmed_minutes: float | None
    pending_classification_minutes: float | None
    observable_coverage_percent: float | None
    hourly_distribution: tuple[HourlyBucket, ...]
    coverage_timeline: Mapping[str, Any] | None
    not_computable_reason_es: str | None
    partial_reason_es: str | None
    usage_notice_es: str
    materialized_at: datetime | None
    """Primer guardado; en un mes ya cerrado, la materialización definitiva (≥ ``period_to``)."""
    recomputed_at: datetime | None
    """Último recálculo a demanda del mes en curso; nulo en la materialización definitiva."""
    inputs_digest: str | None


class ExposurePort(Protocol):
    """El indicador para el paquete mensual (VIG-239) y para la ruta (SUP-U05-31)."""

    async def indicator(
        self, context: ScopeContext, zone_id: uuid.UUID, period: str
    ) -> ExposureIndicator: ...
