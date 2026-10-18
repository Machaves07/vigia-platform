"""``detail_code`` de la acreditación (domain-entities.md de U-04 §1.2; pendiente nº 33).

Diez de los 44 códigos de negocio de U-04: exportación, cobertura, métricas, exposición,
auditoría por muestra, compromisos y paquete mensual. Viajan como ``ApiError.detail_code`` con el
prefijo ``accreditation_``; ``code`` sigue siendo la lista cerrada de U-02. El estado HTTP de
cada uno está en el mapa único de U-04 (``loop.detail_codes.HTTP_STATUS_BY_DETAIL_CODE``, A-68)
y su mensaje genérico en español en ``labels.loop.es.json`` (``accreditation_detail_code``).
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from typing import Final

__all__ = ["ACCREDITATION_DETAIL_CODE_LABEL_BINDINGS", "AccreditationDetailCode"]


class AccreditationDetailCode(enum.StrEnum):
    """Lista cerrada de ``detail_code`` del módulo ``accreditation``."""

    EXPORT_SCOPE_TOO_LARGE = "accreditation_export_scope_too_large"
    EXPORT_ZONES_SPAN_PLANTS = "accreditation_export_zones_span_plants"
    EXPORT_ZONE_OUT_OF_SCOPE = "accreditation_export_zone_out_of_scope"
    COVERAGE_UNAVAILABLE = "accreditation_coverage_unavailable"
    METRIC_NOT_COMPUTABLE = "accreditation_metric_not_computable"
    EXPOSURE_NOT_COMPUTABLE = "accreditation_exposure_not_computable"
    SAMPLE_ALREADY_REVIEWED = "accreditation_sample_already_reviewed"
    SAMPLE_CANNOT_MODIFY_CLASSIFICATION = "accreditation_sample_cannot_modify_classification"
    COMMITMENT_OUT_OF_SCOPE = "accreditation_commitment_out_of_scope"
    MONTHLY_PACKAGE_NOT_AVAILABLE = "accreditation_monthly_package_not_available"


ACCREDITATION_DETAIL_CODE_LABEL_BINDINGS: Final[Mapping[str, type[enum.Enum]]] = {
    "accreditation_detail_code": AccreditationDetailCode,
}
"""Mensaje en español de cada código: el arranque falla si falta uno (NFR-LAZ-51)."""
