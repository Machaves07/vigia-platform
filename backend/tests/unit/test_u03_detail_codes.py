"""``detail_code`` de U-03 (pendiente nº 33; interfaces §4, BLM §4.1).

Cada error de negocio con nombre de las rutas de personas de U-03 tiene su ``detail_code`` con el
prefijo del módulo que lo produce (``catalog_`` o ``fleet_``), en ``snake_case``, registrado en
``DetailCodeRegistry`` y bajo ``conflict`` o ``invalid_request`` de U-02. Ningún nombre coincide
con un ``rejection_code`` del contrato (las rutas del nodo solo usan esos), y los avisos del
inventario y ``walk_test_regression_pending`` no son errores.
"""

from __future__ import annotations

import re
import uuid

import pytest
from vigia_contracts.models.enumerations import RejectionCode

from vigia_platform.catalog.detail_codes import (
    CATALOG_API_ERROR_CODES,
    CatalogDetailCode,
    register_catalog_detail_codes,
)
from vigia_platform.fleet.detail_codes import (
    FLEET_API_ERROR_CODES,
    FleetDetailCode,
    register_fleet_detail_codes,
)
from vigia_platform.shared.api.errors import (
    DETAIL_CODE_PREFIXES,
    MAX_DETAIL_CODE_CHARS,
    ApiError,
    ApiErrorCode,
    ApiStartupError,
    DetailCodeRegistry,
    ErrorCatalog,
)
from vigia_platform.shared.api.labels import PlatformLabels

CODES: dict[str, ApiErrorCode] = {
    **{code.value: error for code, error in CATALOG_API_ERROR_CODES.items()},
    **{code.value: error for code, error in FLEET_API_ERROR_CODES.items()},
}

INTERFACE_ERRORS: dict[str, str] = {
    "admission_rejected": "catalog",
    "family_already_admitted": "catalog",
    "family_not_admitted": "catalog",
    "predicate_invalid": "catalog",
    "unsatisfiable_coverage": "catalog",
    "zone_without_cameras": "catalog",
    "last_standard_in_zone": "catalog",
    "plant_policy_missing": "catalog",
    "mounting_gate_pending": "catalog",
    "use_gate_not_loaded": "catalog",
    "blur_not_verified": "catalog",
    "fewer_than_three": "catalog",
    "workers_representation_missing": "catalog",
    "signatory_role_not_in_policy": "catalog",
    "signatory_user_role_mismatch": "catalog",
    "signatory_not_expected": "catalog",
    "signatures_incomplete": "catalog",
    "agreement_reused_from_other_zone": "catalog",
    "commissioning_record_missing": "catalog",
    "walk_test_in_progress": "catalog",
    "passes_below_minimum": "catalog",
    "false_negative_present": "catalog",
    "false_alarm_rate_above_threshold": "catalog",
    "redundancy_not_verified": "catalog",
    "no_observability_events_in_window": "catalog",
    "steps_still_open": "catalog",
    "latency_not_measured": "catalog",
    "walk_test_incomplete": "catalog",
    "node_not_assigned": "catalog",
    "pass_not_found": "catalog",
    "node_not_declared": "fleet",
    "node_unregistered": "fleet",
    "zone_already_served": "fleet",
    "zone_in_other_plant": "fleet",
    "code_in_use": "fleet",
    "replaced_node_not_found": "fleet",
    "version_outside_contract_window": "fleet",
}
"""Errores con nombre de interfaces §4 (con ``pass_not_found`` de v1.2) y el módulo que los
produce. Fuera: los avisos del inventario y ``walk_test_regression_pending``."""

TASK_INITIAL_CODES = (
    "catalog_matrix_incomplete",
    "catalog_pass_not_found",
    "catalog_family_already_admitted",
    "fleet_node_not_revoked",
    "catalog_family_not_admitted",
    "catalog_unsatisfiable_coverage",
    "catalog_free_text_rejected",
    "fleet_node_not_declared",
    "fleet_zone_already_served",
    "fleet_version_outside_contract_window",
)
"""Los que nombra la tarea (TASK-204) además de los de interfaces §4."""

NOT_ERRORS = (
    "walk_test_regression_pending",
    "node_mute",
    "version_retiring",
    "queue_over_threshold",
    "clock_drift",
    "certificate_expiring",
    "simulated_adapter_in_productive",
    "orphan_clips_growing",
    "camera_below_min_fps",
    "fleet_version_pending",
    "update_reverted",
)

BLM_API_ERROR_CODES: dict[str, ApiErrorCode] = {
    "catalog_admission_rejected": ApiErrorCode.INVALID_REQUEST,
    "catalog_family_not_admitted": ApiErrorCode.CONFLICT,
    "catalog_predicate_invalid": ApiErrorCode.INVALID_REQUEST,
    "catalog_unsatisfiable_coverage": ApiErrorCode.INVALID_REQUEST,
    "catalog_last_standard_in_zone": ApiErrorCode.CONFLICT,
    "catalog_free_text_rejected": ApiErrorCode.INVALID_REQUEST,
    "catalog_plant_policy_missing": ApiErrorCode.CONFLICT,
    "catalog_blur_not_verified": ApiErrorCode.CONFLICT,
    "catalog_mounting_gate_pending": ApiErrorCode.CONFLICT,
    "catalog_fewer_than_three": ApiErrorCode.INVALID_REQUEST,
    "catalog_workers_representation_missing": ApiErrorCode.INVALID_REQUEST,
    "catalog_signatory_not_expected": ApiErrorCode.CONFLICT,
    "catalog_signatures_incomplete": ApiErrorCode.CONFLICT,
    "catalog_agreement_reused_from_other_zone": ApiErrorCode.CONFLICT,
    "catalog_commissioning_record_missing": ApiErrorCode.CONFLICT,
    "catalog_walk_test_in_progress": ApiErrorCode.CONFLICT,
    "catalog_node_not_assigned": ApiErrorCode.CONFLICT,
    "catalog_steps_still_open": ApiErrorCode.CONFLICT,
    "catalog_false_negative_present": ApiErrorCode.CONFLICT,
    "catalog_false_alarm_rate_above_threshold": ApiErrorCode.CONFLICT,
    "catalog_redundancy_not_verified": ApiErrorCode.CONFLICT,
    "catalog_no_observability_events_in_window": ApiErrorCode.CONFLICT,
    "fleet_node_not_declared": ApiErrorCode.CONFLICT,
    "fleet_zone_already_served": ApiErrorCode.CONFLICT,
    "fleet_zone_in_other_plant": ApiErrorCode.INVALID_REQUEST,
    "fleet_code_in_use": ApiErrorCode.CONFLICT,
    "fleet_version_outside_contract_window": ApiErrorCode.INVALID_REQUEST,
}
"""La tabla de BLM §4.1, fila por fila."""

_SNAKE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")


def _registry() -> DetailCodeRegistry:
    registry = DetailCodeRegistry()
    register_catalog_detail_codes(registry)
    register_fleet_detail_codes(registry)
    registry.seal()
    return registry


def _domain_name(code: str) -> str:
    return code.split("_", 1)[1]


def test_every_code_has_the_prefix_of_its_module_is_snake_case_and_registers() -> None:
    assert {code.value for code in CatalogDetailCode} == set(CATALOG_API_ERROR_CODES)
    assert {code.value for code in FleetDetailCode} == set(FLEET_API_ERROR_CODES)
    assert all(code.value.startswith("catalog_") for code in CatalogDetailCode)
    assert all(code.value.startswith("fleet_") for code in FleetDetailCode)
    for name in CODES:
        assert name.startswith(DETAIL_CODE_PREFIXES)
        assert _SNAKE.fullmatch(name) and name.isascii(), name
        assert len(name) <= MAX_DETAIL_CODE_CHARS, name
    assert _registry().codes() == frozenset(CODES)


def test_every_named_error_of_the_interfaces_has_its_code_in_its_module() -> None:
    for name, module in INTERFACE_ERRORS.items():
        assert f"{module}_{name}" in CODES, name


@pytest.mark.parametrize("code", TASK_INITIAL_CODES)
def test_the_initial_codes_of_the_task_are_registered(code: str) -> None:
    assert code in _registry()


def test_inventory_warnings_and_the_regression_state_are_not_errors() -> None:
    names = {_domain_name(code) for code in CODES}
    for name in NOT_ERRORS:
        assert name not in names, name


def test_no_u03_domain_name_is_a_contract_rejection_code() -> None:
    rejection_codes = {code.value for code in RejectionCode}
    assert {_domain_name(code) for code in CODES}.isdisjoint(rejection_codes)
    assert set(CODES).isdisjoint(rejection_codes)
    assert set(INTERFACE_ERRORS).isdisjoint(rejection_codes)


def test_every_code_travels_as_conflict_or_invalid_request() -> None:
    assert set(CODES.values()) <= {ApiErrorCode.CONFLICT, ApiErrorCode.INVALID_REQUEST}


@pytest.mark.parametrize(("code", "expected"), sorted(BLM_API_ERROR_CODES.items()))
def test_the_api_error_code_is_the_one_of_the_blm_table(code: str, expected: ApiErrorCode) -> None:
    assert CODES[code] is expected


@pytest.mark.parametrize("code", sorted(CODES))
def test_a_registered_code_reaches_the_response_with_its_api_error_code(code: str) -> None:
    catalog = ErrorCatalog(PlatformLabels.load(), _registry())
    body = catalog.body(ApiError(CODES[code], detail_code=code), uuid.uuid4())
    assert body.code is CODES[code]
    assert body.detail_code == code


def test_an_unregistered_or_unprefixed_code_is_refused() -> None:
    catalog = ErrorCatalog(PlatformLabels.load(), _registry())
    body = catalog.body(
        ApiError(ApiErrorCode.CONFLICT, detail_code="catalog_not_a_real_code"), uuid.uuid4()
    )
    assert body.code is ApiErrorCode.INTERNAL_ERROR
    assert body.detail_code is None
    with pytest.raises(ApiStartupError):
        DetailCodeRegistry().register(["family_not_admitted"])
