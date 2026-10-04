"""Listas cerradas del catálogo y las compuertas (domain-entities.md de U-03, §4 y sus notas).

Quince de las veintiuna enumeraciones que U-03 aporta a ``labels.platform.es.json``; las seis de
la flota están en ``fleet.domain.enums``. Dentro de una versión mayor solo se **añaden**
valores: nunca se renombran ni se retiran (regla de evolución de U-01). Cada valor tiene su
etiqueta en español y el arranque falla si falta una (``CATALOG_LABEL_BINDINGS``, NFR-GOB-67).

Las heredadas (``gate_status``, ``zone_mode``, ``predicate_family``, ``signal_role``,
``observability_state`` de U-01; ``communication_state`` y ``role`` de U-02) no se redefinen.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping, MutableMapping
from typing import Final

__all__ = [
    "CATALOG_LABEL_BINDINGS",
    "AdmissionCriterion",
    "AdmissionResult",
    "AgreementStatus",
    "CatalogChangedField",
    "ConfirmationOrigin",
    "DocumentKind",
    "GateKind",
    "OcclusionVerification",
    "PassResult",
    "Posture",
    "RegressionCause",
    "RegressionState",
    "StepKind",
    "WalkTestKind",
    "WalkTestStatus",
    "register_catalog_label_bindings",
]


class AdmissionResult(enum.StrEnum):
    """Resultado de la prueba de admisión de tres preguntas (H-43)."""

    ADMITTED = "admitted"
    REJECTED = "rejected"


class AdmissionCriterion(enum.StrEnum):
    """Criterio de la prueba de admisión que no se cumplió."""

    STANDARD = "standard"
    REMEDY = "remedy"
    SUBJECT = "subject"


class GateKind(enum.StrEnum):
    """Las dos compuertas de una zona."""

    MOUNTING = "mounting"
    USAGE = "usage"


class AgreementStatus(enum.StrEnum):
    """Ciclo del acuerdo de uso: nunca vuelve atrás."""

    PENDING_SIGNATURES = "pending_signatures"
    APPROVED = "approved"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"


class ConfirmationOrigin(enum.StrEnum):
    """Pantalla desde la que un firmante confirma su firma (respuesta 5)."""

    MANAGEMENT = "management"
    TRANSPARENCY = "transparency"


class WalkTestKind(enum.StrEnum):
    INITIAL = "initial"
    REGRESSION_RERUN = "regression_rerun"


class WalkTestStatus(enum.StrEnum):
    IN_PROGRESS = "in_progress"
    CLOSED = "closed"
    INCOMPLETE = "incomplete"
    REOPENED = "reopened"


class StepKind(enum.StrEnum):
    """Paso cronometrado del comisionamiento (ocho valores)."""

    PHYSICAL_SETUP = "physical_setup"
    SIGNAL_MAPPING = "signal_mapping"
    FRAMING = "framing"
    WALK_TEST_PASSES = "walk_test_passes"
    OCCLUSION_TEST = "occlusion_test"
    LATENCY_MEASUREMENT = "latency_measurement"
    REVIEW_AND_SIGNATURES = "review_and_signatures"
    OTHER = "other"


class Posture(enum.StrEnum):
    """Postura de una fila de la matriz del walk-test (respuesta 9)."""

    STANDING = "standing"
    CROUCHED = "crouched"
    PARTIALLY_OCCLUDED = "partially_occluded"
    SLOW_MOVEMENT = "slow_movement"


class PassResult(enum.StrEnum):
    DETECTED = "detected"
    MISSED = "missed"
    FALSE_ALARM = "false_alarm"


class OcclusionVerification(enum.StrEnum):
    """Verificación de la prueba de oclusión; ``pending`` por la nota de §2.14."""

    VERIFIED = "verified"
    DECLARED = "declared"
    FAILED = "failed"
    PENDING = "pending"


class RegressionState(enum.StrEnum):
    CURRENT = "current"
    PENDING = "pending"


class RegressionCause(enum.StrEnum):
    """Causa de la regresión del walk-test; ``framing_recaptured`` por la nota de §2.16."""

    CATALOG_CHANGE = "catalog_change"
    MODEL_VERSION_CHANGE = "model_version_change"
    FRAMING_RECAPTURED = "framing_recaptured"


class DocumentKind(enum.StrEnum):
    """Documento firmado de planta; ``blur_check_capture`` por la nota de §3.14."""

    SCOPE_RECORD = "scope_record"
    USE_AGREEMENT = "use_agreement"
    PLANT_POLICY = "plant_policy"
    BLUR_CHECK_CAPTURE = "blur_check_capture"


class CatalogChangedField(enum.StrEnum):
    """Qué cambió de una versión del catálogo a la siguiente."""

    STANDARDS = "standards"
    CAMERAS = "cameras"
    MINIMUM_COVERAGE = "minimum_coverage"
    SIGNALS = "signals"
    THRESHOLDS = "thresholds"
    CLIP_WINDOW = "clip_window"
    EPISODE = "episode"
    SINGLE_OCCUPANCY = "single_occupancy"


CATALOG_LABEL_BINDINGS: Final[Mapping[str, type[enum.Enum]]] = {
    "admission_result": AdmissionResult,
    "admission_criterion": AdmissionCriterion,
    "gate_kind": GateKind,
    "agreement_status": AgreementStatus,
    "confirmation_origin": ConfirmationOrigin,
    "walk_test_kind": WalkTestKind,
    "walk_test_status": WalkTestStatus,
    "step_kind": StepKind,
    "posture": Posture,
    "pass_result": PassResult,
    "occlusion_verification": OcclusionVerification,
    "regression_state": RegressionState,
    "regression_cause": RegressionCause,
    "document_kind": DocumentKind,
    "catalog_changed_field": CatalogChangedField,
}
"""Enumeración de ``labels.platform.es.json`` → lista cerrada del código (NFR-GOB-67)."""


def register_catalog_label_bindings(bindings: MutableMapping[str, type[enum.Enum]]) -> None:
    """Añade las enumeraciones del catálogo a la comprobación de etiquetas del arranque.

    Una enumeración que ya estuviera ligada a otra lista es un error de programación: dos
    unidades no pueden llamar igual a dos listas distintas.
    """
    clashes = sorted(
        name
        for name, kind in CATALOG_LABEL_BINDINGS.items()
        if bindings.get(name, kind) is not kind
    )
    if clashes:
        raise ValueError(f"enumeraciones ya ligadas a otra lista: {', '.join(clashes)}")
    bindings.update(CATALOG_LABEL_BINDINGS)
