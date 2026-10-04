"""Las 21 enumeraciones de U-03 y sus etiquetas en español (domain-entities §4; NFR-GOB-67).

Cada valor de las 21 listas cerradas, con los cinco que añadieron las notas fechadas
(``update_result.failed``, ``occlusion_verification.pending``, ``document_kind.blur_check_capture``,
``credential_status.superseded``, ``regression_cause.framing_recaptured``), tiene etiqueta en
``labels.platform.es.json`` y está ligado a la comprobación del arranque (``LABEL_BINDINGS``): si
falta una sola etiqueta, ``vigia-api`` no arranca.
"""

from __future__ import annotations

import enum
import json
from pathlib import Path
from typing import Any

import pytest

from tests.api_support import World
from vigia_platform.catalog.detail_codes import CATALOG_DETAIL_CODE_LABEL_BINDINGS
from vigia_platform.catalog.domain.enums import (
    CATALOG_LABEL_BINDINGS,
    register_catalog_label_bindings,
)
from vigia_platform.fleet.detail_codes import FLEET_DETAIL_CODE_LABEL_BINDINGS
from vigia_platform.fleet.domain.enums import FLEET_LABEL_BINDINGS, register_fleet_label_bindings
from vigia_platform.shared.api.app import LABEL_BINDINGS
from vigia_platform.shared.api.errors import ApiStartupError
from vigia_platform.shared.api.labels import DEFAULT_LABELS_PATH, PlatformLabels

EXPECTED: dict[str, dict[str, str]] = {
    "admission_result": {"admitted": "admitida", "rejected": "rechazada"},
    "admission_criterion": {
        "standard": "estándar escrito",
        "remedy": "remedio de ingeniería o de proceso",
        "subject": "sujeto del dato",
    },
    "gate_kind": {"mounting": "compuerta de montaje", "usage": "compuerta de uso"},
    "agreement_status": {
        "pending_signatures": "pendiente de firmas",
        "approved": "aprobado",
        "superseded": "sustituido",
        "revoked": "revocado",
    },
    "confirmation_origin": {
        "management": "consola de gestión",
        "transparency": "vista de transparencia",
    },
    "walk_test_kind": {"initial": "inicial", "regression_rerun": "reejecución por regresión"},
    "walk_test_status": {
        "in_progress": "en curso",
        "closed": "cerrada",
        "incomplete": "incompleta",
        "reopened": "reabierta",
    },
    "step_kind": {
        "physical_setup": "configuración física",
        "signal_mapping": "mapeo de señales",
        "framing": "encuadre",
        "walk_test_passes": "pases de walk-test",
        "occlusion_test": "prueba de oclusión",
        "latency_measurement": "medición de latencia",
        "review_and_signatures": "revisión y firmas",
        "other": "otro",
    },
    "posture": {
        "standing": "de pie",
        "crouched": "agachado",
        "partially_occluded": "parcialmente ocluido",
        "slow_movement": "desplazamiento lento",
    },
    "pass_result": {
        "detected": "detectado",
        "missed": "falso negativo",
        "false_alarm": "falsa alarma",
    },
    "occlusion_verification": {
        "verified": "verificada",
        "declared": "declarada",
        "failed": "fallida",
        "pending": "verificando",
    },
    "regression_state": {"current": "acta vigente", "pending": "regresión pendiente"},
    "regression_cause": {
        "catalog_change": "cambio de catálogo",
        "model_version_change": "cambio de versión del modelo",
        "framing_recaptured": "línea base recapturada",
    },
    "enrollment_code_status": {
        "active": "activo",
        "used": "usado",
        "expired": "vencido",
        "superseded": "sustituido",
    },
    "enrollment_attempt_result": {
        "accepted": "aceptado",
        "enrollment_code_used": "código ya usado",
        "enrollment_code_expired": "código vencido",
        "enrollment_code_invalid": "código inválido",
        "rate_limited": "límite de tasa",
    },
    "credential_status": {
        "active": "activo",
        "overlapping": "en solapamiento",
        "revoked": "revocado",
        "superseded": "sustituida",
    },
    "fleet_alarm_kind": {
        "node_mute": "nodo mudo",
        "queue_over_threshold": "cola sobre umbral",
        "clock_drift": "desviación de reloj",
        "version_retiring": "versión próxima a retirarse",
        "simulated_adapter_in_productive": "adaptador simulado en modo productivo",
        "certificate_expiring": "certificado por vencer",
        "camera_below_min_fps": "cámara por debajo de su tasa mínima",
        "orphan_clips_growing": "clips huérfanos en aumento",
    },
    "update_result": {"applied": "aplicada", "reverted": "revertida", "failed": "fallida"},
    "upload_grant_status": {
        "issued": "emitida",
        "used": "usada",
        "expired": "vencida",
        "orphan": "huérfana",
    },
    "document_kind": {
        "scope_record": "acta de alcance",
        "use_agreement": "acuerdo de uso",
        "plant_policy": "política de planta",
        "blur_check_capture": "captura del difuminado",
    },
    "catalog_changed_field": {
        "standards": "estándares",
        "cameras": "cámaras",
        "minimum_coverage": "cobertura mínima",
        "signals": "señales",
        "thresholds": "umbrales",
        "clip_window": "ventana de clip",
        "episode": "episodio",
        "single_occupancy": "marca unipersonal",
    },
}
"""Las 21 enumeraciones de §4 con sus notas y la etiqueta propuesta de cada valor."""

U03_BINDINGS: dict[str, type[enum.Enum]] = {**CATALOG_LABEL_BINDINGS, **FLEET_LABEL_BINDINGS}
LABELS = PlatformLabels.load()


def test_exactly_the_21_enumerations_with_every_value_of_the_design() -> None:
    assert len(EXPECTED) == 21
    assert set(U03_BINDINGS) == set(EXPECTED)
    assert len(CATALOG_LABEL_BINDINGS) == 15
    assert len(FLEET_LABEL_BINDINGS) == 6
    for name, kind in U03_BINDINGS.items():
        assert {member.value for member in kind} == set(EXPECTED[name]), name


@pytest.mark.parametrize(
    ("enumeration", "value", "label"),
    [
        ("update_result", "failed", "fallida"),
        ("occlusion_verification", "pending", "verificando"),
        ("document_kind", "blur_check_capture", "captura del difuminado"),
        ("credential_status", "superseded", "sustituida"),
        ("regression_cause", "framing_recaptured", "línea base recapturada"),
    ],
)
def test_the_values_added_by_the_dated_notes_have_their_label(
    enumeration: str, value: str, label: str
) -> None:
    assert value in {member.value for member in U03_BINDINGS[enumeration]}
    assert LABELS.label(enumeration, value) == label


def test_every_value_has_its_spanish_label_in_the_labels_file() -> None:
    assert LABELS.require_complete(U03_BINDINGS) == []
    for name, values in EXPECTED.items():
        for value, label in values.items():
            assert LABELS.label(name, value) == label
        assert LABELS.values(name) == frozenset(values), f"{name}: valores sin enumeración"


def test_every_detail_code_has_its_spanish_message() -> None:
    bindings = {**CATALOG_DETAIL_CODE_LABEL_BINDINGS, **FLEET_DETAIL_CODE_LABEL_BINDINGS}
    assert LABELS.require_complete(bindings) == []
    for name, kind in bindings.items():
        assert LABELS.values(name) == frozenset(member.value for member in kind)


def test_the_startup_label_check_covers_the_u03_enumerations() -> None:
    for name, kind in {
        **U03_BINDINGS,
        **CATALOG_DETAIL_CODE_LABEL_BINDINGS,
        **FLEET_DETAIL_CODE_LABEL_BINDINGS,
    }.items():
        assert LABEL_BINDINGS[name] is kind


def _labels_file(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


_EVERY_VALUE = [
    pytest.param(name, value, id=f"{name}.{value}")
    for name, values in EXPECTED.items()
    for value in values
]


@pytest.mark.parametrize(("enumeration", "value"), _EVERY_VALUE)
def test_the_application_does_not_start_if_one_u03_label_is_missing(
    tmp_path: Path, enumeration: str, value: str
) -> None:
    document: dict[str, Any] = json.loads(DEFAULT_LABELS_PATH.read_text(encoding="utf-8"))
    del document[enumeration][value]
    if not document[enumeration]:
        del document[enumeration]
    with pytest.raises(ApiStartupError, match=f"«{value}» de «{enumeration}»"):
        World().app(labels_path=_labels_file(tmp_path, document))


def test_the_application_does_not_start_if_a_detail_code_message_is_missing(
    tmp_path: Path,
) -> None:
    document: dict[str, Any] = json.loads(DEFAULT_LABELS_PATH.read_text(encoding="utf-8"))
    del document["fleet_detail_code"]["fleet_zone_already_served"]
    with pytest.raises(ApiStartupError, match="fleet_zone_already_served"):
        World().app(labels_path=_labels_file(tmp_path, document))


def test_the_registration_functions_add_the_bindings_and_refuse_a_clash() -> None:
    bindings: dict[str, type[enum.Enum]] = {}
    register_catalog_label_bindings(bindings)
    register_fleet_label_bindings(bindings)
    assert bindings == U03_BINDINGS
    register_catalog_label_bindings(bindings)  # volver a ligar la misma lista no cambia nada
    assert bindings == U03_BINDINGS

    class Other(enum.StrEnum):
        X = "x"

    with pytest.raises(ValueError, match="gate_kind"):
        register_catalog_label_bindings({"gate_kind": Other})
    with pytest.raises(ValueError, match="update_result"):
        register_fleet_label_bindings({"update_result": Other})
