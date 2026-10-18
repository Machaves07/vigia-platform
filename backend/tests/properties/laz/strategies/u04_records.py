"""Generadores de contenidos y cargas válidos de U-04 (``record_by_type``, ``event_payload``).

Sobre el kit que ya usan U-02 y U-03 (``tests.properties.gob.u03_records``): ``schema_strategy``
genera desde el JSON Schema que el registro persiste y aquí se aplican las reglas entre campos
que los modelos de U-04 comprueban y el esquema no expresa (motivo de la lista de su salida,
reclasificación con secuencia mayor que 1, porqués numerados en orden, revisión de incerrable
con su campo según la salida, periodo ordenado, métrica no calculable sin valor…). Las tareas de
dominio de U-04 los reutilizan para sus propios registros.

Solo datos generados.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final

from hypothesis import assume
from hypothesis import strategies as st

from tests.properties.gob.u03_records import JsonObject, schema_strategy
from tests.properties.gob.u03_records import event_payload as event_payload
from vigia_platform.accreditation.events import register_accreditation_event_types
from vigia_platform.accreditation.record_types import register_accreditation_record_types
from vigia_platform.ledger.registry import CompiledType, RecordTypeRegistry
from vigia_platform.loop.domain.enums import (
    ClassificationOutcome,
    MetricStatus,
    ReasonCategoryAuthorized,
    ReasonCategoryConfirmed,
    ReasonCategoryFalsePositive,
)
from vigia_platform.loop.events import register_loop_event_types
from vigia_platform.loop.record_types import register_loop_record_types
from vigia_platform.notifications.events import register_notifications_event_types
from vigia_platform.shared.outbox.registries import EventTypeRegistry

__all__ = [
    "event_payload",
    "record_by_type",
    "u04_event_registry",
    "u04_registry",
]

_SAFE_TEXT: Final = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZáéíóúñü "
_UUID7: Final = st.uuids(version=4).map(lambda value: str(value)[:14] + "7" + str(value)[15:])

_REASONS: Final[Mapping[str, tuple[str, ...]]] = {
    ClassificationOutcome.CONFIRMED: tuple(ReasonCategoryConfirmed),
    ClassificationOutcome.AUTHORIZED_OPERATION: tuple(ReasonCategoryAuthorized),
    ClassificationOutcome.FALSE_POSITIVE: tuple(ReasonCategoryFalsePositive),
}


def u04_registry() -> RecordTypeRegistry:
    """Registro con los 17 tipos de U-04."""
    registry = RecordTypeRegistry()
    register_loop_record_types(registry)
    register_accreditation_record_types(registry)
    return registry


def u04_event_registry() -> EventTypeRegistry:
    """Registro con los 25 eventos de U-04 y el trabajo interno ``export_requested``."""
    registry = EventTypeRegistry()
    register_loop_event_types(registry)
    register_accreditation_event_types(registry)
    register_notifications_event_types(registry)
    return registry


def _fix_classification(document: JsonObject, draw: st.DrawFn) -> None:
    document["reason_category"] = draw(st.sampled_from(_REASONS[document["outcome"]]))
    document["is_reclassification"] = document["sequence_in_subject"] > 1


def _fix_unclosable_review(document: JsonObject, draw: st.DrawFn) -> None:
    document.pop("new_review_due_at", None)
    document.pop("new_action_id", None)
    if document["outcome"] == "renewed":
        document["new_review_due_at"] = document["reviewed_at"]
    else:
        document["new_action_id"] = draw(_UUID7)


def _fix_root_cause(document: JsonObject, draw: st.DrawFn) -> None:
    for index, step in enumerate(document["why_steps"], start=1):
        step["step_index"] = index
    pending = document["state"] == "pending_information"
    if pending and document.get("missing_information") is None:
        note = st.text(alphabet=_SAFE_TEXT, min_size=1, max_size=40)
        document["missing_information"] = draw(note)


def _fix_configuration(document: JsonObject, draw: st.DrawFn) -> None:
    for key in ("previous_values", "new_values"):
        for setting in document[key]:
            if setting.get("integer_value") is None and setting.get("business_calendar") is None:
                setting["integer_value"] = draw(st.integers(0, 100_000))
            elif setting.get("integer_value") is not None:
                setting.pop("business_calendar", None)


def _fix_period(document: JsonObject, draw: st.DrawFn) -> None:
    period = document["period"]
    first, second = sorted((period["starts_at"], period["ends_at"]))
    assume(first != second)
    period["starts_at"], period["ends_at"] = first, second


def _fix_monthly(document: JsonObject, draw: st.DrawFn) -> None:
    for metric in document["metrics"]:
        if metric["status"] == MetricStatus.NOT_COMPUTABLE:
            metric.pop("value", None)


_FIXES: Final[Mapping[str, Callable[[JsonObject, st.DrawFn], None]]] = {
    "classification": _fix_classification,
    "unclosable_review": _fix_unclosable_review,
    "root_cause_analysis": _fix_root_cause,
    "loop_configuration_changed": _fix_configuration,
    "export_produced": _fix_period,
    "monthly_package_produced": _fix_monthly,
}
"""Reglas entre campos que los modelos comprueban y el esquema no expresa."""

_OVERRIDES: Final[Mapping[str, Mapping[str, st.SearchStrategy[Any]]]] = {
    # El patrón «sin ruta» admite casi todo: se genera un nombre que también pasa la política base.
    "action_evidence_added": {
        "/attachments[*]/filename": st.text(alphabet="abcdefxyz_-.", min_size=1, max_size=40)
    },
}


@st.composite
def record_by_type(draw: st.DrawFn, compiled: CompiledType) -> JsonObject:
    """Un contenido válido del tipo ``compiled`` de U-04."""
    name = compiled.record_type
    document: JsonObject = draw(
        schema_strategy(compiled.content_schema, overrides=_OVERRIDES.get(name, {}))
    )
    fix = _FIXES.get(name)
    if fix is not None:
        fix(document, draw)
    return document
