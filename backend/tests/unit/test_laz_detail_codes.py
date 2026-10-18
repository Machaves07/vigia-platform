"""Los 44 ``detail_code`` de U-04 y su mapa único de estados (TASK-302; DE §1.2; nº 33; A-68).

- **La lista**, por igualdad con DE §1.2 (sus 43 filas) más ``attachment_metadata_not_allowed``
  de la nota de NFR Requirements (NFR-LAZ-19): 44, sin prefijo en el diseño.
- **La forma**: ``snake_case`` ASCII, hasta 64 caracteres, con el prefijo de su módulo (``loop_``,
  ``accreditation_`` o ``notifications_``); ``DetailCodeRegistry`` de U-02 los admite y las tres
  unidades de U-04 los declaran en ``detail_codes``.
- **El mensaje**: cada uno tiene su ``message_es`` (etiqueta de ``labels.loop.es.json``).
- **El mapa** ``HTTP_STATUS_BY_DETAIL_CODE`` (A-68): todos menos ``metric_not_computable`` (que
  viaja en un 200), con un ``ApiErrorCode`` de la lista cerrada de U-02 y su estado de
  ``HTTP_STATUS``; 409 exactamente para los seis de estado incompatible, 503 exactamente para los
  cinco de dependencia caída, 400 ``invalid_request`` para el resto y **ningún 422**.
"""

from __future__ import annotations

import re
from typing import Final

import pytest

from vigia_platform.accreditation.detail_codes import AccreditationDetailCode
from vigia_platform.loop.detail_codes import (
    HTTP_STATUS_BY_DETAIL_CODE,
    NOT_AN_ERROR,
    U04_DETAIL_CODES,
    LoopDetailCode,
    U04DetailCode,
    design_name,
)
from vigia_platform.notifications.detail_codes import NotificationsDetailCode
from vigia_platform.shared.api.errors import (
    HTTP_STATUS,
    MAX_DETAIL_CODE_CHARS,
    ApiErrorCode,
    DetailCodeRegistry,
)
from vigia_platform.shared.api.labels import PlatformLabels
from vigia_platform.shared.runtime.units import registered_units

DESIGN_CODES: Final = frozenset(
    {
        # DE §1.2, tabla (43)
        "reason_required",
        "reason_too_short",
        "blocked_vocabulary_term",
        "person_attribution_detected",
        "action_on_person_forbidden",
        "action_type_not_allowed",
        "deadline_out_of_range",
        "closure_evidence_missing",
        "signer_license_missing",
        "closure_requires_assigned_action",
        "unclosable_fields_incomplete",
        "unclosable_policy_not_signed",
        "unclosable_policy_unavailable",
        "unclosable_policy_revoked",
        "unclosable_review_date_out_of_range",
        "transition_not_allowed",
        "reopening_reason_required",
        "cause_category_not_allowed",
        "analysis_information_insufficient",
        "analysis_step_out_of_order",
        "assistant_unavailable",
        "assistant_output_rejected",
        "gate_state_unavailable",
        "zone_gate_not_approved",
        "standard_text_unavailable",
        "export_scope_too_large",
        "export_zones_span_plants",
        "export_zone_out_of_scope",
        "coverage_unavailable",
        "metric_not_computable",
        "exposure_not_computable",
        "sample_already_reviewed",
        "sample_cannot_modify_classification",
        "commitment_out_of_scope",
        "monthly_package_not_available",
        "vocabulary_change_reason_required",
        "vocabulary_term_duplicate",
        "attachment_type_not_allowed",
        "attachment_too_large",
        "attachment_hash_mismatch",
        "attachment_declaration_missing",
        "configuration_change_reason_required",
        "notification_preference_not_owned",
        # nota fechada de NFR Requirements (NFR-LAZ-19)
        "attachment_metadata_not_allowed",
    }
)
"""``loop_error_code``: copiado a mano de DE §1.2 y de su segunda nota fechada."""

CONFLICTS: Final = frozenset(
    {
        "transition_not_allowed",
        "analysis_step_out_of_order",
        "closure_requires_assigned_action",
        "sample_already_reviewed",
        "vocabulary_term_duplicate",
        "monthly_package_not_available",
    }
)
"""LC-LAZ §6.2, fila 409."""

DEPENDENCIES_DOWN: Final = frozenset(
    {
        "gate_state_unavailable",
        "unclosable_policy_unavailable",
        "coverage_unavailable",
        "standard_text_unavailable",
        "assistant_unavailable",
    }
)
"""LC-LAZ §6.2, fila 503 (sin ``temporarily_unavailable``, que es ``code`` de U-02)."""

PREFIXES: Final[dict[type[U04DetailCode], str]] = {
    LoopDetailCode: "loop_",
    AccreditationDetailCode: "accreditation_",
    NotificationsDetailCode: "notifications_",
}

_SNAKE: Final = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")
_LABEL_ENUMERATION: Final[dict[type[U04DetailCode], str]] = {
    LoopDetailCode: "loop_detail_code",
    AccreditationDetailCode: "accreditation_detail_code",
    NotificationsDetailCode: "notifications_detail_code",
}


def test_the_44_codes_are_exactly_those_of_the_design() -> None:
    assert len(DESIGN_CODES) == 44
    assert len(U04_DETAIL_CODES) == 44
    assert {design_name(code) for code in U04_DETAIL_CODES} == DESIGN_CODES
    assert len({code.value for code in U04_DETAIL_CODES}) == 44


@pytest.mark.parametrize("code", U04_DETAIL_CODES, ids=lambda code: code.value)
def test_each_code_has_its_module_prefix_and_snake_case(code: U04DetailCode) -> None:
    prefix = PREFIXES[type(code)]
    assert code.value.startswith(prefix)
    assert code.value == prefix + design_name(code)
    assert code.value.isascii() and _SNAKE.fullmatch(code.value)
    assert len(code.value) <= MAX_DETAIL_CODE_CHARS


def test_the_module_split_is_33_10_1() -> None:
    assert (len(LoopDetailCode), len(AccreditationDetailCode), len(NotificationsDetailCode)) == (
        33,
        10,
        1,
    )


def test_the_three_units_declare_their_codes_and_u02_accepts_them() -> None:
    units = {unit.name: unit for unit in registered_units()}
    declared = {
        "loop": set(units["loop"].detail_codes),
        "accreditation": set(units["accreditation"].detail_codes),
        "notifications": set(units["notifications"].detail_codes),
    }
    assert declared == {
        "loop": {code.value for code in LoopDetailCode},
        "accreditation": {code.value for code in AccreditationDetailCode},
        "notifications": {code.value for code in NotificationsDetailCode},
    }
    registry = DetailCodeRegistry()
    for unit in registered_units():
        registry.register(unit.detail_codes)
    registry.seal()


@pytest.mark.parametrize("code", U04_DETAIL_CODES, ids=lambda code: code.value)
def test_each_code_has_its_message_es(code: U04DetailCode) -> None:
    message = PlatformLabels.load().label(_LABEL_ENUMERATION[type(code)], code)
    assert message.strip() == message and len(message) >= 10


def test_the_status_map_covers_every_code_but_metric_not_computable() -> None:
    assert {design_name(code) for code in NOT_AN_ERROR} == {"metric_not_computable"}
    assert set(HTTP_STATUS_BY_DETAIL_CODE) == set(U04_DETAIL_CODES) - NOT_AN_ERROR
    assert len(HTTP_STATUS_BY_DETAIL_CODE) == 43


def test_the_status_map_applies_a_68_exactly() -> None:
    by_status: dict[int, set[str]] = {}
    for code, (api_code, status) in HTTP_STATUS_BY_DETAIL_CODE.items():
        assert api_code in set(ApiErrorCode)
        assert HTTP_STATUS[api_code] == status
        assert status != 422
        by_status.setdefault(status, set()).add(design_name(code))
    assert by_status[409] == CONFLICTS
    assert by_status[503] == DEPENDENCIES_DOWN
    assert by_status[400] == DESIGN_CODES - CONFLICTS - DEPENDENCIES_DOWN - {
        "metric_not_computable"
    }
    assert set(by_status) == {400, 409, 503}
    codes = {api_code for api_code, _ in HTTP_STATUS_BY_DETAIL_CODE.values()}
    assert codes == {
        ApiErrorCode.INVALID_REQUEST,
        ApiErrorCode.CONFLICT,
        ApiErrorCode.TEMPORARILY_UNAVAILABLE,
    }


def test_no_api_error_code_answers_422() -> None:
    assert 422 not in set(HTTP_STATUS.values())
