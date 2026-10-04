"""``detail_code`` del catálogo y las compuertas (pendiente nº 33; interfaces §4, BLM §4.1).

Cada error de negocio con nombre de las rutas de personas del catálogo, las actas, los acuerdos
y el walk-test viaja como ``ApiError.detail_code`` con el prefijo ``catalog_``; ``code`` sigue
siendo ``conflict`` o ``invalid_request`` de U-02 (``CATALOG_API_ERROR_CODES``) y nunca se
sustituye. El mensaje en español es la etiqueta de ``catalog_detail_code`` en
``labels.platform.es.json``; ninguna respuesta al nodo usa estos nombres (solo los
``rejection_code`` del contrato).

No son errores, y no están aquí: ``walk_test_regression_pending`` (estado de la zona que no
bloquea nada, respuesta 13) ni los avisos del inventario. Una tarea posterior que necesite otro
código lo **añade** en su propio PR.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from typing import Final

from vigia_platform.shared.api.errors import ApiErrorCode, DetailCodeRegistry

__all__ = [
    "CATALOG_API_ERROR_CODES",
    "CATALOG_DETAIL_CODE_LABEL_BINDINGS",
    "CatalogDetailCode",
    "register_catalog_detail_codes",
]


class CatalogDetailCode(enum.StrEnum):
    """Lista cerrada de ``detail_code`` del módulo ``catalog``."""

    # admisión y catálogo
    ADMISSION_REJECTED = "catalog_admission_rejected"
    FAMILY_ALREADY_ADMITTED = "catalog_family_already_admitted"
    FAMILY_NOT_ADMITTED = "catalog_family_not_admitted"
    PREDICATE_INVALID = "catalog_predicate_invalid"
    UNSATISFIABLE_COVERAGE = "catalog_unsatisfiable_coverage"
    ZONE_WITHOUT_CAMERAS = "catalog_zone_without_cameras"
    LAST_STANDARD_IN_ZONE = "catalog_last_standard_in_zone"
    FREE_TEXT_REJECTED = "catalog_free_text_rejected"
    # compuertas, actas, acuerdos y política
    PLANT_POLICY_MISSING = "catalog_plant_policy_missing"
    MOUNTING_GATE_PENDING = "catalog_mounting_gate_pending"
    USE_GATE_NOT_LOADED = "catalog_use_gate_not_loaded"
    BLUR_NOT_VERIFIED = "catalog_blur_not_verified"
    FEWER_THAN_THREE = "catalog_fewer_than_three"
    WORKERS_REPRESENTATION_MISSING = "catalog_workers_representation_missing"
    SIGNATORY_ROLE_NOT_IN_POLICY = "catalog_signatory_role_not_in_policy"
    SIGNATORY_USER_ROLE_MISMATCH = "catalog_signatory_user_role_mismatch"
    SIGNATORY_NOT_EXPECTED = "catalog_signatory_not_expected"
    SIGNATURES_INCOMPLETE = "catalog_signatures_incomplete"
    AGREEMENT_REUSED_FROM_OTHER_ZONE = "catalog_agreement_reused_from_other_zone"
    COMMISSIONING_RECORD_MISSING = "catalog_commissioning_record_missing"
    # walk-test y acta
    NODE_NOT_ASSIGNED = "catalog_node_not_assigned"
    WALK_TEST_IN_PROGRESS = "catalog_walk_test_in_progress"
    PASSES_BELOW_MINIMUM = "catalog_passes_below_minimum"
    MATRIX_INCOMPLETE = "catalog_matrix_incomplete"
    PASS_NOT_FOUND = "catalog_pass_not_found"  # noqa: S105 — «pass» es el pase del walk-test.
    FALSE_NEGATIVE_PRESENT = "catalog_false_negative_present"
    FALSE_ALARM_RATE_ABOVE_THRESHOLD = "catalog_false_alarm_rate_above_threshold"
    REDUNDANCY_NOT_VERIFIED = "catalog_redundancy_not_verified"
    NO_OBSERVABILITY_EVENTS_IN_WINDOW = "catalog_no_observability_events_in_window"
    STEPS_STILL_OPEN = "catalog_steps_still_open"
    LATENCY_NOT_MEASURED = "catalog_latency_not_measured"
    WALK_TEST_INCOMPLETE = "catalog_walk_test_incomplete"


_CONFLICT: Final = ApiErrorCode.CONFLICT
_INVALID: Final = ApiErrorCode.INVALID_REQUEST

CATALOG_API_ERROR_CODES: Final[Mapping[CatalogDetailCode, ApiErrorCode]] = {
    CatalogDetailCode.ADMISSION_REJECTED: _INVALID,
    CatalogDetailCode.FAMILY_ALREADY_ADMITTED: _CONFLICT,
    CatalogDetailCode.FAMILY_NOT_ADMITTED: _CONFLICT,
    CatalogDetailCode.PREDICATE_INVALID: _INVALID,
    CatalogDetailCode.UNSATISFIABLE_COVERAGE: _INVALID,
    CatalogDetailCode.ZONE_WITHOUT_CAMERAS: _CONFLICT,
    CatalogDetailCode.LAST_STANDARD_IN_ZONE: _CONFLICT,
    CatalogDetailCode.FREE_TEXT_REJECTED: _INVALID,
    CatalogDetailCode.PLANT_POLICY_MISSING: _CONFLICT,
    CatalogDetailCode.MOUNTING_GATE_PENDING: _CONFLICT,
    CatalogDetailCode.USE_GATE_NOT_LOADED: _CONFLICT,
    CatalogDetailCode.BLUR_NOT_VERIFIED: _CONFLICT,
    CatalogDetailCode.FEWER_THAN_THREE: _INVALID,
    CatalogDetailCode.WORKERS_REPRESENTATION_MISSING: _INVALID,
    CatalogDetailCode.SIGNATORY_ROLE_NOT_IN_POLICY: _INVALID,
    CatalogDetailCode.SIGNATORY_USER_ROLE_MISMATCH: _INVALID,
    CatalogDetailCode.SIGNATORY_NOT_EXPECTED: _CONFLICT,
    CatalogDetailCode.SIGNATURES_INCOMPLETE: _CONFLICT,
    CatalogDetailCode.AGREEMENT_REUSED_FROM_OTHER_ZONE: _CONFLICT,
    CatalogDetailCode.COMMISSIONING_RECORD_MISSING: _CONFLICT,
    CatalogDetailCode.NODE_NOT_ASSIGNED: _CONFLICT,
    CatalogDetailCode.WALK_TEST_IN_PROGRESS: _CONFLICT,
    CatalogDetailCode.PASSES_BELOW_MINIMUM: _INVALID,
    CatalogDetailCode.MATRIX_INCOMPLETE: _CONFLICT,
    CatalogDetailCode.PASS_NOT_FOUND: _INVALID,
    CatalogDetailCode.FALSE_NEGATIVE_PRESENT: _CONFLICT,
    CatalogDetailCode.FALSE_ALARM_RATE_ABOVE_THRESHOLD: _CONFLICT,
    CatalogDetailCode.REDUNDANCY_NOT_VERIFIED: _CONFLICT,
    CatalogDetailCode.NO_OBSERVABILITY_EVENTS_IN_WINDOW: _CONFLICT,
    CatalogDetailCode.STEPS_STILL_OPEN: _CONFLICT,
    CatalogDetailCode.LATENCY_NOT_MEASURED: _CONFLICT,
    CatalogDetailCode.WALK_TEST_INCOMPLETE: _CONFLICT,
}
"""``api_error_code`` de U-02 bajo el que viaja cada ``detail_code`` (BLM §4.1; los que la
tabla no fija: ``invalid_request`` si el cuerpo es incoherente por sí mismo, ``conflict`` si
choca con el estado de la zona)."""

CATALOG_DETAIL_CODE_LABEL_BINDINGS: Final[Mapping[str, type[enum.Enum]]] = {
    "catalog_detail_code": CatalogDetailCode,
}
"""Mensaje en español de cada ``detail_code``: el arranque falla si falta uno (NFR-GOB-67)."""


def register_catalog_detail_codes(registry: DetailCodeRegistry) -> None:
    """Registra los ``detail_code`` del catálogo; ``ApiStartupError`` si alguno no cumple."""
    registry.register(code.value for code in CatalogDetailCode)
