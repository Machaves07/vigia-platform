"""``detail_code`` del lazo y el **mapa único** de estados de U-04 (domain-entities §1.2; A-68).

Los 44 códigos de negocio de U-04 (``loop_error_code``: los 43 de la tabla de §1.2 más
``attachment_metadata_not_allowed`` de la nota de NFR Requirements, NFR-LAZ-19) viajan como
``ApiError.detail_code`` con el prefijo del módulo que los produce (pendiente nº 33, LC-LAZ §6.1):
33 con ``loop_`` (aquí), 10 con ``accreditation_`` (``accreditation.detail_codes``) y 1 con
``notifications_`` (``notifications.detail_codes``). ``code`` sigue siendo la lista cerrada de
U-02 (``ApiErrorCode``) y nunca se sustituye; un recurso de otra organización responde
``not_found``, nunca uno de estos códigos (BR-NUC-09).

``HTTP_STATUS_BY_DETAIL_CODE`` es la **única** tabla de U-04 que dice bajo qué ``code`` y con qué
estado viaja cada uno (A-68, que manda sobre LC-LAZ §6.2):

- **400 ``invalid_request``**: los rechazos de validación (donde LC-LAZ §6.2 dice 422);
- **409 ``conflict``**: los seis de estado incompatible de LC-LAZ §6.2;
- **503 ``temporarily_unavailable``**: los cinco de dependencia caída con fallo cerrado
  declarado (nota de la sesión B en LC-LAZ §6.2: la API sigue viva, solo esa sección degrada);
- **ningún 422**: ``ApiErrorCode`` no lo tiene y no se amplía.

``metric_not_computable`` **no está en el mapa**: no es un error, viaja dentro de una respuesta 200
por métrica (LC-LAZ §6.2). El mensaje genérico en español de cada código es su etiqueta en
``labels.loop.es.json`` (``loop_detail_code``, ``accreditation_detail_code`` y
``notifications_detail_code``, DE §11.2). Una tarea que necesite otro código lo **añade** aquí y
en su lista, con su estado; ninguno se retira.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from vigia_platform.accreditation.detail_codes import AccreditationDetailCode
from vigia_platform.notifications.detail_codes import NotificationsDetailCode
from vigia_platform.shared.api.errors import HTTP_STATUS, ApiErrorCode

__all__ = [
    "HTTP_STATUS_BY_DETAIL_CODE",
    "LOOP_DETAIL_CODE_LABEL_BINDINGS",
    "NOT_AN_ERROR",
    "U04_DETAIL_CODES",
    "LoopDetailCode",
    "U04DetailCode",
    "design_name",
]


class LoopDetailCode(enum.StrEnum):
    """Lista cerrada de ``detail_code`` del módulo ``loop``."""

    # motivos y validadores de texto
    REASON_REQUIRED = "loop_reason_required"
    REASON_TOO_SHORT = "loop_reason_too_short"
    BLOCKED_VOCABULARY_TERM = "loop_blocked_vocabulary_term"
    PERSON_ATTRIBUTION_DETECTED = "loop_person_attribution_detected"
    # acciones y cierre
    ACTION_ON_PERSON_FORBIDDEN = "loop_action_on_person_forbidden"
    ACTION_TYPE_NOT_ALLOWED = "loop_action_type_not_allowed"
    DEADLINE_OUT_OF_RANGE = "loop_deadline_out_of_range"
    CLOSURE_EVIDENCE_MISSING = "loop_closure_evidence_missing"
    SIGNER_LICENSE_MISSING = "loop_signer_license_missing"
    CLOSURE_REQUIRES_ASSIGNED_ACTION = "loop_closure_requires_assigned_action"
    # incerrable y reapertura
    UNCLOSABLE_FIELDS_INCOMPLETE = "loop_unclosable_fields_incomplete"
    UNCLOSABLE_POLICY_NOT_SIGNED = "loop_unclosable_policy_not_signed"
    UNCLOSABLE_POLICY_UNAVAILABLE = "loop_unclosable_policy_unavailable"
    UNCLOSABLE_POLICY_REVOKED = "loop_unclosable_policy_revoked"
    UNCLOSABLE_REVIEW_DATE_OUT_OF_RANGE = "loop_unclosable_review_date_out_of_range"
    TRANSITION_NOT_ALLOWED = "loop_transition_not_allowed"
    REOPENING_REASON_REQUIRED = "loop_reopening_reason_required"
    # causa raíz y asistente
    CAUSE_CATEGORY_NOT_ALLOWED = "loop_cause_category_not_allowed"
    ANALYSIS_INFORMATION_INSUFFICIENT = "loop_analysis_information_insufficient"
    ANALYSIS_STEP_OUT_OF_ORDER = "loop_analysis_step_out_of_order"
    ASSISTANT_UNAVAILABLE = "loop_assistant_unavailable"
    ASSISTANT_OUTPUT_REJECTED = "loop_assistant_output_rejected"
    # puertos de U-03
    GATE_STATE_UNAVAILABLE = "loop_gate_state_unavailable"
    ZONE_GATE_NOT_APPROVED = "loop_zone_gate_not_approved"
    STANDARD_TEXT_UNAVAILABLE = "loop_standard_text_unavailable"
    # vocabulario y configuración
    VOCABULARY_CHANGE_REASON_REQUIRED = "loop_vocabulary_change_reason_required"
    VOCABULARY_TERM_DUPLICATE = "loop_vocabulary_term_duplicate"
    CONFIGURATION_CHANGE_REASON_REQUIRED = "loop_configuration_change_reason_required"
    # adjuntos de evidencia de cierre
    ATTACHMENT_TYPE_NOT_ALLOWED = "loop_attachment_type_not_allowed"
    ATTACHMENT_TOO_LARGE = "loop_attachment_too_large"
    ATTACHMENT_HASH_MISMATCH = "loop_attachment_hash_mismatch"
    ATTACHMENT_DECLARATION_MISSING = "loop_attachment_declaration_missing"
    ATTACHMENT_METADATA_NOT_ALLOWED = "loop_attachment_metadata_not_allowed"


type U04DetailCode = LoopDetailCode | AccreditationDetailCode | NotificationsDetailCode

U04_DETAIL_CODES: Final[tuple[U04DetailCode, ...]] = (
    *LoopDetailCode,
    *AccreditationDetailCode,
    *NotificationsDetailCode,
)
"""Los 44 ``detail_code`` de U-04, en el orden de sus tres listas."""

_PREFIXES: Final = ("loop_", "accreditation_", "notifications_")


def design_name(code: U04DetailCode) -> str:
    """El nombre de ``loop_error_code`` de domain-entities §1.2, sin el prefijo del módulo."""
    for prefix in _PREFIXES:
        if code.value.startswith(prefix):
            return code.value.removeprefix(prefix)
    raise ValueError(f"«{code.value}» no lleva prefijo de U-04")  # pragma: no cover


NOT_AN_ERROR: Final[frozenset[U04DetailCode]] = frozenset(
    {AccreditationDetailCode.METRIC_NOT_COMPUTABLE}
)
"""Códigos que viajan dentro de una respuesta 200 y nunca como error (LC-LAZ §6.2)."""

_INVALID: Final = (ApiErrorCode.INVALID_REQUEST, HTTP_STATUS[ApiErrorCode.INVALID_REQUEST])
_CONFLICT: Final = (ApiErrorCode.CONFLICT, HTTP_STATUS[ApiErrorCode.CONFLICT])
_UNAVAILABLE: Final = (
    ApiErrorCode.TEMPORARILY_UNAVAILABLE,
    HTTP_STATUS[ApiErrorCode.TEMPORARILY_UNAVAILABLE],
)

_CONFLICTS: Final[frozenset[U04DetailCode]] = frozenset(
    {
        LoopDetailCode.TRANSITION_NOT_ALLOWED,
        LoopDetailCode.ANALYSIS_STEP_OUT_OF_ORDER,
        LoopDetailCode.CLOSURE_REQUIRES_ASSIGNED_ACTION,
        AccreditationDetailCode.SAMPLE_ALREADY_REVIEWED,
        LoopDetailCode.VOCABULARY_TERM_DUPLICATE,
        AccreditationDetailCode.MONTHLY_PACKAGE_NOT_AVAILABLE,
    }
)
"""Los seis de estado incompatible (LC-LAZ §6.2, fila 409)."""

_DEPENDENCIES_DOWN: Final[frozenset[U04DetailCode]] = frozenset(
    {
        LoopDetailCode.GATE_STATE_UNAVAILABLE,
        LoopDetailCode.UNCLOSABLE_POLICY_UNAVAILABLE,
        AccreditationDetailCode.COVERAGE_UNAVAILABLE,
        LoopDetailCode.STANDARD_TEXT_UNAVAILABLE,
        LoopDetailCode.ASSISTANT_UNAVAILABLE,
    }
)
"""Los cinco de dependencia caída con fallo cerrado declarado (LC-LAZ §6.2, fila 503; el sexto
de esa fila, ``temporarily_unavailable``, es ``code`` de U-02 y no lleva ``detail_code``)."""


def _status(code: U04DetailCode) -> tuple[ApiErrorCode, int]:
    if code in _CONFLICTS:
        return _CONFLICT
    if code in _DEPENDENCIES_DOWN:
        return _UNAVAILABLE
    return _INVALID


HTTP_STATUS_BY_DETAIL_CODE: Final[Mapping[U04DetailCode, tuple[ApiErrorCode, int]]] = (
    MappingProxyType({code: _status(code) for code in U04_DETAIL_CODES if code not in NOT_AN_ERROR})
)
"""``detail_code`` → (``code`` de U-02, estado HTTP): la tabla única de A-68 (43 entradas)."""

LOOP_DETAIL_CODE_LABEL_BINDINGS: Final[Mapping[str, type[enum.Enum]]] = {
    "loop_detail_code": LoopDetailCode,
}
"""Mensaje en español de cada código: el arranque falla si falta uno (NFR-LAZ-51)."""
