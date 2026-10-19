"""Etiquetas en español de U-04 (TASK-302; NFR-LAZ-52; DE §1 y §11.1).

- ``labels.loop.es.json`` tiene **exactamente** las enumeraciones de U-04 (las de DE §1, más
  ``package_form`` y las listas que cierran los tipos de registro, más los tres ``detail_code`` y
  ``analysis_missing_item`` de TASK-315),
  y cada una **exactamente** los valores de su lista del código: igualdad de conjuntos, ninguna
  etiqueta falta ni sobra.
- ``attachment_content_type``: cada tipo MIME tiene su clave de etiqueta.
- Se carga **junto a** ``labels.platform.es.json`` (``PlatformLabels.load``): una enumeración
  repetida entre los dos archivos o un archivo que falta impiden arrancar (``LabelsInvalid``), y
  la aplicación no arranca sin la etiqueta de un valor de U-04 (``ApiStartupError``).
- ``export_state`` no tiene ``composing`` (U04-H-02) y algunas etiquetas de §11.1, por igualdad.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import pytest

from tests.api_support import World
from vigia_platform.accreditation.registration import ACCREDITATION_LABELS
from vigia_platform.loop.domain.enums import (
    AttachmentContentType,
    AttachmentContentTypeLabel,
    ExportState,
    attachment_label_key,
)
from vigia_platform.loop.registration import LOOP_LABELS
from vigia_platform.notifications.registration import NOTIFICATIONS_LABELS
from vigia_platform.shared.api.errors import ApiStartupError
from vigia_platform.shared.api.labels import (
    DEFAULT_LABELS_PATH,
    DEFAULT_LOOP_LABELS_PATH,
    LabelsInvalid,
    PlatformLabels,
)

U04_BINDINGS: Final = {**LOOP_LABELS, **ACCREDITATION_LABELS, **NOTIFICATIONS_LABELS}
LOOP_FILE: Final = json.loads(DEFAULT_LOOP_LABELS_PATH.read_text(encoding="utf-8"))

DESIGN_ENUMERATIONS: Final = frozenset(
    {
        "finding_lifecycle_state",
        "classification_outcome",
        "review_resolution_outcome",
        "reason_category_confirmed",
        "reason_category_authorized",
        "reason_category_false_positive",
        "action_type",
        "cause_category",
        "root_cause_analysis_state",
        "assistant_request_state",
        "assistant_unavailable_cause",
        "sample_audit_outcome",
        "unclosable_review_outcome",
        "notification_urgency",
        "notification_channel",
        "notification_delivery_status",
        "notification_audience",
        "export_state",
        "monthly_package_state",
        "metric_key",
        "metric_status",
        "exposure_status",
        "attachment_content_type",
        "blocked_term_form",
        "blocked_term_category",
        "text_validation_verdict",
        "loop_activity_kind",
        "commitment_state",
    }
)
"""Las 28 enumeraciones cerradas de DE §1, copiadas a mano del diseño."""

RECORD_ENUMERATIONS: Final = frozenset(
    {
        "package_form",
        "subject_kind",
        "effect_on_loop",
        "escalation_level",
        "evidence_ref_kind",
        "blocked_term_origin",
        "metric_unit",
    }
)
"""``package_form`` (U04-H-03) y las listas que cierran los tipos de registro (§2, §5.2)."""

DETAIL_CODE_ENUMERATIONS: Final = frozenset(
    {"loop_detail_code", "accreditation_detail_code", "notifications_detail_code"}
)
RESPONSE_ENUMERATIONS: Final = frozenset({"analysis_missing_item"})
"""Listas cerradas que una respuesta muestra a una persona: ``pending_information`` del análisis
de causa raíz (TASK-315, BR-LAZ-64)."""


def test_the_loop_file_has_exactly_the_u04_enumerations() -> None:
    assert len(DESIGN_ENUMERATIONS) == 28
    expected = (
        DESIGN_ENUMERATIONS | RECORD_ENUMERATIONS | DETAIL_CODE_ENUMERATIONS | RESPONSE_ENUMERATIONS
    )
    assert set(U04_BINDINGS) == expected
    assert set(LOOP_FILE) == expected


@pytest.mark.parametrize("enumeration", sorted(U04_BINDINGS))
def test_each_enumeration_has_exactly_its_values(enumeration: str) -> None:
    kind = U04_BINDINGS[enumeration]
    assert set(LOOP_FILE[enumeration]) == {member.value for member in kind}
    for label in LOOP_FILE[enumeration].values():
        assert isinstance(label, str) and label.strip() == label and label


def test_every_mime_type_has_its_label_key() -> None:
    keys = {attachment_label_key(content_type) for content_type in AttachmentContentType}
    assert keys == set(AttachmentContentTypeLabel)
    assert set(LOOP_FILE["attachment_content_type"]) == {key.value for key in keys}


def test_export_state_has_no_composing_and_some_labels_of_11_1() -> None:
    assert {state.value for state in ExportState} == {
        "requested",
        "in_progress",
        "ready",
        "failed",
        "expired",
    }
    labels = PlatformLabels.load()
    assert labels.label("export_state", "expired") == "caducada para descarga"
    assert labels.label("finding_lifecycle_state", "unclosable") == (
        "SIN ALTERNATIVA SEGURA DISPONIBLE"
    )
    assert labels.label("classification_outcome", "false_positive") == "falso positivo"
    assert labels.label("notification_urgency", "digest") == "resumen"


def test_the_loop_labels_load_with_the_platform_labels() -> None:
    labels = PlatformLabels.load()
    assert set(LOOP_FILE) <= labels.enumerations()
    assert "role" in labels.enumerations()  # las de U-02 siguen
    assert labels.require_complete(U04_BINDINGS) == []
    platform_only = PlatformLabels.load(DEFAULT_LABELS_PATH, extensions=())
    assert not set(LOOP_FILE) & platform_only.enumerations()


def test_an_enumeration_in_both_files_does_not_load(tmp_path: Path) -> None:
    copy = {**LOOP_FILE, "role": {"copasst": "COPASST"}}
    path = tmp_path / "labels.loop.es.json"
    path.write_text(json.dumps(copy, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(LabelsInvalid, match="role"):
        PlatformLabels.load(DEFAULT_LABELS_PATH, extensions=(path,))


def test_a_missing_loop_file_does_not_load(tmp_path: Path) -> None:
    with pytest.raises(LabelsInvalid):
        PlatformLabels.load(DEFAULT_LABELS_PATH, extensions=(tmp_path / "no-existe.json",))


def _without(enumeration: str, value: str, directory: Path) -> Path:
    document = json.loads(DEFAULT_LOOP_LABELS_PATH.read_text(encoding="utf-8"))
    del document[enumeration][value]
    path = directory / "labels.loop.es.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("enumeration", "value"),
    [
        ("finding_lifecycle_state", "unclosable"),
        ("export_state", "expired"),
        ("loop_detail_code", "loop_transition_not_allowed"),
        ("accreditation_detail_code", "accreditation_monthly_package_not_available"),
        ("notification_urgency", "immediate"),
    ],
)
def test_the_api_does_not_start_without_a_u04_label(
    enumeration: str, value: str, tmp_path: Path
) -> None:
    path = _without(enumeration, value, tmp_path)
    with pytest.raises(ApiStartupError) as raised:
        World().app(extension_labels_paths=(path,))
    assert raised.value.problems == (
        f"el valor «{value}» de «{enumeration}» no tiene etiqueta en español",
    )
