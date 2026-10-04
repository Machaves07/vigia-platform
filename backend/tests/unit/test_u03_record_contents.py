"""Reglas entre campos de los contenidos de U-03 que el esquema no expresa (ejemplos, bordes).

Cada modelo de contenido comprueba, después de validar el esquema, la coherencia que su entidad
exige: la clave compuesta con sus partes (A-55 para ``node_enrolled``), la admisión si y solo si
las tres respuestas son afirmativas (H-43), el motivo solo al revocar (respuesta 7), la
representación de los trabajadores en el acuerdo (H-50), los totales exactos del acta (H-51,
H-53), la verificación de la oclusión (respuesta 11) y la regresión por causa (respuesta 13).
Un contenido que las incumple no llega al expediente (``content_invalid`` en el escritor).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from tests.properties.gob.u03_records import u03_registry
from vigia_platform.fleet.record_types import enrollment_source_key

REGISTRY = u03_registry()
T0 = "2026-10-03T10:00:00.000Z"
T1 = "2026-10-03T11:00:00.000Z"
REASON = "Motivo declarado de la prueba"


def _id() -> str:
    return str(uuid.uuid4())


def _v7() -> str:
    raw = uuid.uuid4().int
    value = (raw & ~(0xF << 76)) | (0x7 << 76)
    value = (value & ~(0x3 << 62)) | (0x2 << 62)
    return str(uuid.UUID(int=value))


def _valid(record_type: str, document: dict[str, Any]) -> None:
    REGISTRY.get(record_type).validate_json(json.dumps(document))


def _invalid(record_type: str, document: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _valid(record_type, document)


# --- la tabla de §5: clave de idempotencia y eventos de cada tipo -----------------------------

TABLE: dict[str, tuple[str | None, tuple[str, ...]]] = {
    "finding_received": ("/finding_id", ("finding_received",)),
    "detection_for_review_received": ("/detection_id", ("detection_for_review_received",)),
    "observability_event_received": ("/event_id", ("observability_event_received",)),
    "node_communication_state_changed": (None, ()),
    "node_enrolled": ("/source_key", ("node_enrolled",)),
    "node_credential_rotated": ("/credential_id", ()),
    "node_revoked": (None, ("node_revoked",)),
    "update_result_received": ("/update_result_id", ("update_result_received",)),
    "node_target_version_published": ("/publication_id", ("target_version_published",)),
    "catalog_version_published": ("/source_key", ("catalog_updated", "regression_marked")),
    "standard_admission_test": ("/admission_id", ()),
    "gate_state_changed": (None, ("gate_state_changed", "zone_activated")),
    "mounting_gate_record": ("/record_id", ("gate_state_changed",)),
    "use_agreement_signed": ("/agreement_id", ("gate_state_changed", "zone_activated")),
    "commissioning_step": ("/step_id", ()),
    "walk_test_result": ("/commissioning_record_id", ("regression_cleared",)),
    "plant_policy_signed": ("/policy_id", ()),
    "occlusion_test_result": ("/test_id", ()),
    "walk_test_regression_marked": (None, ("regression_marked",)),
    "walk_test_regression_cleared": (None, ("regression_cleared",)),
    "catalog_standard_retired": ("/source_key", ("catalog_updated",)),
    "single_occupancy_declared": ("/source_key", ("catalog_updated",)),
    "node_decommissioned": ("/node_id", ("node_decommissioned",)),
    "enrollment_code_issued": ("/code_id", ()),
    "enrollment_attempt_rejected": ("/attempt_id", ()),
    "ingest_rejected": (None, ()),
}
"""``source_key`` y «Eventos que publica» de ``domain-entities.md`` §5; las claves compuestas
(``zone_id`` + ``catalog_version``, ``standard_id`` + ``version``, ``node_id`` +
``credential_id``) van en el campo ``source_key``, que el modelo comprueba contra sus partes."""


@pytest.mark.parametrize("record_type", sorted(TABLE))
def test_source_key_and_events_are_those_of_the_table(record_type: str) -> None:
    source_key_path, events = TABLE[record_type]
    definition = REGISTRY.get(record_type).definition
    assert definition.source_key_path == source_key_path
    assert definition.outbox_events == events


# --- claves compuestas ------------------------------------------------------------------------


def test_node_enrolled_key_is_node_and_credential_and_a_re_enrollment_does_not_collide() -> None:
    node, first, second = _id(), _v7(), _v7()
    base = {
        "node_id": node,
        "zone_ids": [_id()],
        "hardware_fingerprint": "a" * 64,
        "certificate_serial": "0abc",
        "enrolled_at": T0,
    }
    one = {**base, "credential_id": first, "source_key": enrollment_source_key(node, first)}
    two = {**base, "credential_id": second, "source_key": enrollment_source_key(node, second)}
    _valid("node_enrolled", one)
    _valid("node_enrolled", two)
    assert one["source_key"] != two["source_key"]  # misma node_id, otra clave (U03-H-04)
    assert len(one["source_key"]) == 64  # el tope de source_key del escritor
    _invalid("node_enrolled", {**one, "source_key": enrollment_source_key(node, second)}, "A-55")


@pytest.mark.parametrize(
    ("record_type", "identifier", "version"),
    [
        ("catalog_standard_retired", "standard_id", "version"),
        ("single_occupancy_declared", "zone_id", "catalog_version"),
    ],
)
def test_versioned_keys_must_match_their_parts(
    record_type: str, identifier: str, version: str
) -> None:
    documents = {
        "catalog_standard_retired": {
            "zone_id": _id(),
            "standard_id": _id(),
            "version": 3,
            "retired_in_catalog_version": 9,
            "reason_es": REASON,
        },
        "single_occupancy_declared": {
            "zone_id": _id(),
            "catalog_version": 2147483647,
            "single_occupancy": True,
            "aggregation_window_minutes": 480,
            "reason_es": REASON,
        },
    }
    document = documents[record_type]
    document["source_key"] = f"{document[identifier]}:{document[version]}"
    _valid(record_type, document)
    _invalid(record_type, {**document, "source_key": f"{_id()}:{document[version]}"}, "source_key")
    _invalid(record_type, {**document, "source_key": f"{document[identifier]}:1"}, "source_key")


# --- admisión, compuertas y acuerdo --------------------------------------------------------------


def _admission(standard: bool, remedy: bool, subject: bool, **extra: Any) -> dict[str, Any]:
    return {
        "admission_id": _v7(),
        "plant_id": _id(),
        "family": "dwell",
        "answers": {"standard": standard, "remedy": remedy, "subject": subject},
        **extra,
    }


def test_admission_is_admitted_if_and_only_if_the_three_answers_are_yes() -> None:
    _valid("standard_admission_test", _admission(True, True, True, result="admitted"))
    _valid(
        "standard_admission_test",
        _admission(True, False, True, result="rejected", failed_criterion="remedy"),
    )
    _invalid("standard_admission_test", _admission(True, True, False, result="admitted"), "result")
    _invalid("standard_admission_test", _admission(True, True, True, result="rejected"), "result")
    _invalid(
        "standard_admission_test",
        _admission(True, False, True, result="rejected", failed_criterion="standard"),
        "failed_criterion",
    )
    _invalid(
        "standard_admission_test",
        _admission(True, False, True, result="rejected"),
        "failed_criterion",
    )


def test_a_gate_reason_goes_with_a_revocation_and_only_with_it() -> None:
    base = {"zone_id": _id(), "gate": "usage", "resulting_mode": "commissioning"}
    _valid("gate_state_changed", {**base, "status": "revoked", "reason_es": REASON})
    _valid("gate_state_changed", {**base, "status": "approved", "agreement_id": _id()})
    _invalid("gate_state_changed", {**base, "status": "revoked"}, "reason_es")
    _invalid("gate_state_changed", {**base, "status": "approved", "reason_es": REASON}, "reason_es")
    _invalid("gate_state_changed", {**base, "status": "revoked", "reason_es": "corto"}, "10")


def _agreement(roles: list[str], confirm: int | None = None) -> dict[str, Any]:
    signatories = [{"role": role, "user_id": _id()} for role in roles]
    confirmed = signatories if confirm is None else signatories[:confirm]
    return {
        "agreement_id": _v7(),
        "zone_id": _id(),
        "signatories": signatories,
        "confirmations": [
            {
                "user_id": s["user_id"],
                "role_in_use": s["role"],
                "confirmed_at": T0,
                "origin": "management",
            }
            for s in confirmed
        ],
    }


def test_the_use_agreement_needs_three_signatories_the_copasst_and_every_confirmation() -> None:
    _valid("use_agreement_signed", _agreement(["copasst", "plant_manager", "coordinator_sst"]))
    _invalid(
        "use_agreement_signed",
        _agreement(["plant_manager", "coordinator_sst", "administrator"]),
        "copasst",
    )
    _invalid("use_agreement_signed", _agreement(["copasst", "plant_manager"]), "at least 3")
    _invalid(
        "use_agreement_signed",
        _agreement(["copasst", "plant_manager", "coordinator_sst", "administrator"], confirm=3),
        "confirma",
    )
    document = _agreement(["copasst", "plant_manager", "coordinator_sst"])
    document["replaces_agreement_id"] = document["agreement_id"]
    _invalid("use_agreement_signed", document, "sí mismo")


# --- acta, oclusión y regresión -------------------------------------------------------------


def _tranche(by: str) -> dict[str, Any]:
    return {"median_ms": 120, "p95_ms": 300, "max_ms": 900, "repetitions": 100, "measured_by": by}


def _walk_test(**changes: Any) -> dict[str, Any]:
    camera = _id()
    document = {
        "commissioning_record_id": _v7(),
        "session_id": _v7(),
        "zone_id": _id(),
        "catalog_version": 4,
        "matrix_results": [
            {"row_id": _id(), "detected": 3, "missed": 0, "false_alarms": 1},
            {"row_id": _id(), "detected": 3, "missed": 0, "false_alarms": 0},
        ],
        "false_negatives_total": 0,
        "false_alarm_rate_observed": 0.1,
        "false_alarm_threshold": 0.0,
        "false_alarm_acceptance": {"reason_es": REASON, "accepted_by": _id(), "accepted_at": T1},
        "latency": {
            "node_tranche": {"p95_ms": 250, "repetitions": 100, "measured_by": "installer"},
            "platform_tranche": _tranche("platform"),
            "served_tranche": _tranche("platform"),
            "indicative_sum_p95_ms": 850,
        },
        "cameras_measured": [{"camera_id": camera, "measured_fps": 15.0, "declared_min_fps": 10.0}],
        "occlusion_summary": [{"camera_id": camera, "verification": "verified"}],
        "total_duration_ms": 5_400_000,
        "steps_summary": [
            {"step_kind": "physical_setup", "duration_ms": 3_600_000},
            {"step_kind": "walk_test_passes", "duration_ms": 1_800_000},
        ],
        "installer_measurements": {
            "beacon_latency_ms_p95": 250,
            "baselines": [{"camera_id": camera, "zone_id": _id(), "captured_at": T0}],
        },
        "signatures": [{"user_id": _id(), "role_in_use": "provider_installer", "signed_at": T1}],
        "closed_at": T1,
    }
    document.update(changes)
    return document


def test_the_commissioning_record_keeps_exact_totals_and_four_separate_tranches() -> None:
    _valid("walk_test_result", _walk_test())
    rows = _walk_test()["matrix_results"]
    _invalid("walk_test_result", _walk_test(false_negatives_total=1), "false_negatives_total")
    missed = [{**rows[0], "missed": 1}, rows[1]]
    _valid("walk_test_result", _walk_test(matrix_results=missed, false_negatives_total=1))
    _invalid("walk_test_result", _walk_test(total_duration_ms=5_400_001), "suma exacta")
    repeated = [rows[0], {**rows[1], "row_id": rows[0]["row_id"]}]
    _invalid("walk_test_result", _walk_test(matrix_results=repeated), "fila")
    steps = _walk_test()["steps_summary"]
    _invalid(
        "walk_test_result",
        _walk_test(steps_summary=[steps[0], {**steps[1], "step_kind": "physical_setup"}]),
        "tipo de paso",
    )


def test_the_commissioning_record_has_no_responsible_person_per_step() -> None:
    document = _walk_test()
    document["steps_summary"][0]["responsible_user_id"] = _id()
    _invalid("walk_test_result", document, "Extra inputs are not permitted")


def _occlusion(verification: str, **changes: Any) -> dict[str, Any]:
    return {
        "test_id": _v7(),
        "session_id": _v7(),
        "camera_id": _id(),
        "started_at": T0,
        "ended_at": T1,
        "deadline": "2026-10-03T11:05:00.000Z",
        "verification": verification,
        "correlated_event_ids": [],
        **changes,
    }


def test_occlusion_verification_rules() -> None:
    _valid("occlusion_test_result", _occlusion("pending"))
    _valid("occlusion_test_result", _occlusion("failed"))
    _valid("occlusion_test_result", _occlusion("verified", correlated_event_ids=[_id()]))
    _valid("occlusion_test_result", _occlusion("declared", declared_reason_es=REASON))
    _invalid("occlusion_test_result", _occlusion("verified"), "verified")
    _invalid("occlusion_test_result", _occlusion("declared"), "declared_reason_es")
    _invalid(
        "occlusion_test_result",
        _occlusion("pending", declared_reason_es=REASON),
        "declared_reason_es",
    )


def _regression(cause: str, **changes: Any) -> dict[str, Any]:
    return {"zone_id": _id(), "cause": cause, "marked_at": T0, **changes}


@pytest.mark.parametrize(
    ("document", "valid"),
    [
        (lambda: _regression("catalog_change", catalog_version=5, affected_row_ids=[_id()]), True),
        (lambda: _regression("catalog_change", affected_row_ids="all"), False),
        (
            lambda: _regression(
                "model_version_change", model_version="yolo-v9", affected_row_ids="all"
            ),
            True,
        ),
        (lambda: _regression("model_version_change", affected_row_ids="all"), False),
        (lambda: _regression("framing_recaptured", affected_row_ids="all"), True),
        (lambda: _regression("framing_recaptured", affected_row_ids=[_id()]), False),
        (lambda: _regression("catalog_change", catalog_version=5, affected_row_ids=[]), False),
    ],
)
def test_regression_marks_follow_their_cause(
    document: Callable[[], dict[str, Any]], valid: bool
) -> None:
    built = document()
    if valid:
        _valid("walk_test_regression_marked", built)
    else:
        with pytest.raises(ValidationError):
            _valid("walk_test_regression_marked", built)


def test_repeated_rows_in_a_regression_are_rejected() -> None:
    row = _id()
    _invalid(
        "walk_test_regression_marked",
        _regression("catalog_change", catalog_version=5, affected_row_ids=[row, row]),
        "repetidas",
    )


# --- flota ------------------------------------------------------------------------------------


def test_an_enrollment_attempt_record_is_only_for_failed_attempts_and_never_carries_the_code() -> (
    None
):
    base = {
        "attempt_id": _v7(),
        "result": "enrollment_code_expired",
        "hardware_fingerprint": "b" * 64,
        "attempted_at": T0,
        "source_ip_hash": "c" * 64,
    }
    _valid("enrollment_attempt_rejected", base)
    _invalid("enrollment_attempt_rejected", {**base, "result": "accepted"}, "result")
    _invalid("enrollment_attempt_rejected", {**base, "code": "ABCD2345EFGH"}, "Extra inputs")
    _invalid("enrollment_attempt_rejected", {**base, "hardware_fingerprint": "B" * 64}, "pattern")


def test_ingest_rejected_only_for_its_two_codes_and_never_with_content() -> None:
    base = {
        "node_id": _id(),
        "record_kind": "finding",
        "code": "zone_gate_not_approved",
        "correlation_id": _id(),
        "received_at": T0,
    }
    _valid("ingest_rejected", base)
    _valid("ingest_rejected", {**base, "code": "node_zone_mismatch", "zone_id": _id()})
    _invalid("ingest_rejected", {**base, "code": "schema_invalid"}, "code")
    _invalid("ingest_rejected", {**base, "content": {"finding_id": _id()}}, "Extra inputs")


def test_the_maintenance_window_ends_after_it_starts_and_nodes_do_not_repeat() -> None:
    node = _id()
    base = {
        "publication_id": _v7(),
        "target_version": "1.4.0",
        "node_ids": [node],
        "maintenance_window": {"starts_at": T0, "ends_at": T1},
    }
    _valid("node_target_version_published", base)
    _invalid(
        "node_target_version_published",
        {**base, "maintenance_window": {"starts_at": T1, "ends_at": T1}},
        "ventana",
    )
    _invalid("node_target_version_published", {**base, "node_ids": [node, node]}, "repetidos")
    _invalid("node_target_version_published", {**base, "target_version": "1.4.0-RC1"}, "pattern")
