"""Paso 7 de la ingesta con los valores de proceso (TASK-593; A-129; BR-GOB-84; BR-CTR-04).

Sin base de datos:

- **Dominio** (``ingest_order``): ``CatalogVersionView`` lee ``process_context.variables``
  (``variable_id`` → ``kind``) y las variables de estado que citan una señal ``no_data``;
  ``process_context_violation`` da el ``field`` exacto de la plataforma simulada del contrato
  (``process_context_mismatch``): sin bloque, nada; una variable no declarada, repetida o de otra
  clase, ``process_context.variables[i]``; una que falta, ``process_context.variables``; una de
  estado sobre una señal ``no_data`` con lectura válida o con un cambio distinto de ``unknown``,
  ``…at_event.valid`` o ``…changes[j].value``. El orden dentro del paso 7: señales, valores de
  proceso y umbrales. La foto del episodio aplica la misma regla tras la cámara.
- **Recorrido** (``IngestWorld``: la cadena fija, la verificación previa con los modelos del
  contrato v1.6.0 y el ``IngestService`` real, con dobles de sus puertos): un hallazgo y una
  detección del kit con el bloque se aceptan y el contenido guardado, sin ``receipt``, tiene el
  mismo hash canónico (RFC 8785) que la presentación; cada desajuste es ``schema_invalid`` con su
  ``field`` y no deja nada escrito. Una variable repetida la rechaza ya el **modelo** (paso 4),
  con ``field = process_context.variables``.

Mutaciones con nombre que detectan estas pruebas: ``m_sin_paso7_proceso`` (el paso 7 no mira el
bloque), ``m_acepta_repetida`` (la regla no mira las repetidas) y ``m_ignora_no_data`` (una
variable de estado sobre una señal ``no_data`` puede ir válida). Solo datos generados.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import uuid
from typing import Any, Final

import pytest
from vigia_contracts.canonical import canonicalize
from vigia_contracts.conformance.checks._core import draw_seeded
from vigia_contracts.conformance.generators import (
    detection_for_review,
    finding,
    with_process_context,
    zone_catalog,
)
from vigia_contracts.models.api import parse_rejection_response

from tests.ingest_support import IngestWorld, ingest_world, place
from vigia_platform.fleet.domain.clock_tolerance import Window
from vigia_platform.fleet.domain.episode_photo import catalog_violation as photo_violation
from vigia_platform.fleet.domain.episode_photo import photo_row
from vigia_platform.fleet.domain.ingest_order import (
    CatalogVersionView,
    IngestKind,
    catalog_violation,
    process_context_violation,
)
from vigia_platform.node_api.rejections import NodeRejection
from vigia_platform.node_api.versioning import VersionPolicy, check_version

T0: Final = dt.datetime(2026, 11, 1, 8, 0, tzinfo=dt.UTC)
WINDOW: Final = Window(T0 + dt.timedelta(days=1), T0 + dt.timedelta(days=1, seconds=30))
HOUR: Final = dt.timedelta(hours=1)
YEAR: Final = dt.timedelta(days=365)

STANDARD: Final = "6c7d8e9f-0a1b-4c2d-8e3f-4a5b6c7d8e9f"
CAMERA: Final = "9e8d7c6b-5a49-4382-a716-05f4e3d2c1b0"
ENERGY: Final = "4b5c6d7e-8f90-4a1b-9c2d-3e4f5a6b7c8e"
JUMBO_SIGNAL: Final = "8a9b0c1d-2e3f-4a5b-8c6d-7e8f9a0b1c2d"
SPEED: Final = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
JUMBO: Final = "2b3c4d5e-6f70-4b8c-9d0e-1f2a3b4c5d6e"
UNDECLARED: Final = "3c4d5e6f-7081-4c9d-8e1f-2a3b4c5d6e7f"


# --- Dominio -------------------------------------------------------------------------------------


def _catalog(*, jumbo_no_data: bool = False, process: bool = True) -> dict[str, Any]:
    """Lo que el paso 7 lee de un ``ZoneCatalog``: una analógica (velocidad) y una de estado
    (jumbo en posición) sobre la señal de guarda."""
    jumbo: dict[str, Any] = {"signal_id": JUMBO_SIGNAL, "role": "guard"}
    if jumbo_no_data:
        jumbo["no_data"] = True
    payload: dict[str, Any] = {
        "standards": [{"standard_id": STANDARD, "version": 1}],
        "signals": [{"signal_id": ENERGY, "role": "energy"}, jumbo],
        "thresholds": {"review": 0.4, "publication": 0.7},
        "cameras": [{"camera_id": CAMERA}],
    }
    if process:
        payload["process_context"] = {
            "before_ms": 30_000,
            "variables": [
                {"variable_id": SPEED, "kind": "analog"},
                {"variable_id": JUMBO, "kind": "binary", "signal_id": JUMBO_SIGNAL},
            ],
        }
    return payload


def _view(payload: dict[str, Any], version: int = 1) -> CatalogVersionView:
    return CatalogVersionView.from_payload(version, T0, None, payload)


def _analog(variable_id: str = SPEED) -> dict[str, Any]:
    return {
        "variable_id": variable_id,
        "kind": "analog",
        "at_event": {"value": 1397.0, "valid": True, "offset_ms": 0},
        "min": 1390.0,
        "max": 1400.0,
        "last": 1397.0,
        "samples": [{"offset_ms": -10_000, "value": 1390.0}, {"offset_ms": 0, "value": 1397.0}],
    }


def _binary(variable_id: str = JUMBO, *, valid: bool = True) -> dict[str, Any]:
    value = "asserted" if valid else "unknown"
    return {
        "variable_id": variable_id,
        "kind": "binary",
        "at_event": {"value": value, "valid": valid, "offset_ms": 0},
        "changes": [{"offset_ms": -30_000, "value": value}],
        "truncated": False,
    }


def _document(
    variables: list[dict[str, Any]] | None, *, jumbo_valid: bool = True, confidence: float = 0.9
) -> dict[str, Any]:
    """Lo que el paso 7 lee de un hallazgo; ``variables=None``, sin bloque."""
    document: dict[str, Any] = {
        "standard": {"standard_id": STANDARD, "version": 1},
        "signals": [
            {"signal_id": ENERGY, "role": "energy", "valid": True},
            {"signal_id": JUMBO_SIGNAL, "role": "guard", "valid": jumbo_valid},
        ],
        "automatic_classification": {"trigger": "publication_threshold"},
        "max_confidence": confidence,
    }
    if variables is not None:
        document["process_context"] = {
            "from_offset_ms": -30_000,
            "to_offset_ms": 30_000,
            "variables": variables,
        }
    return document


def test_the_view_reads_the_declared_variables_and_the_binary_ones_without_data() -> None:
    view = _view(_catalog(jumbo_no_data=True))
    assert dict(view.process_variables) == {
        uuid.UUID(SPEED): "analog",
        uuid.UUID(JUMBO): "binary",
    }
    assert view.process_no_data == frozenset({uuid.UUID(JUMBO)})
    assert _view(_catalog()).process_no_data == frozenset()
    bare = _view(_catalog(process=False))
    assert dict(bare.process_variables) == {} and bare.process_no_data == frozenset()


def test_a_declared_variable_without_kind_makes_the_catalog_unreadable() -> None:
    from vigia_platform.fleet.domain.ingest_order import CatalogUnreadable

    payload = _catalog()
    del payload["process_context"]["variables"][0]["kind"]
    with pytest.raises(CatalogUnreadable):
        _view(payload)


def test_without_the_block_there_is_nothing_to_check_even_if_the_catalog_declares_it() -> None:
    assert process_context_violation(_view(_catalog()), _document(None)) is None
    assert (
        catalog_violation([_view(_catalog())], _document(None), IngestKind.FINDING, WINDOW) is None
    )


def test_every_declared_variable_once_and_in_any_order_is_accepted() -> None:
    view = _view(_catalog())
    assert process_context_violation(view, _document([_binary(), _analog()])) is None
    assert process_context_violation(view, _document([_analog(), _binary()])) is None
    assert catalog_violation(
        [view], _document([_analog(), _binary()]), IngestKind.FINDING, WINDOW
    ) is (None)


@pytest.mark.parametrize(
    ("variables", "field"),
    [
        ([_analog(), _binary(), _analog(UNDECLARED)], "process_context.variables[2]"),
        ([_analog(UNDECLARED), _binary()], "process_context.variables[0]"),
        ([_analog(), _binary(), _analog()], "process_context.variables[2]"),
        ([_analog()], "process_context.variables"),
        ([], "process_context.variables"),
        ([_analog(), _analog(JUMBO)], "process_context.variables[1]"),
        ([_binary(SPEED), _binary()], "process_context.variables[0]"),
    ],
    ids=[
        "no_declarada",
        "no_declarada_primera",
        "repetida",
        "falta",
        "vacio",
        "otra_clase",
        "otra_clase_estado",
    ],
)
def test_each_mismatch_names_its_exact_field(variables: list[dict[str, Any]], field: str) -> None:
    """``m_sin_paso7_proceso`` (todas) y ``m_acepta_repetida`` («repetida»)."""
    view = _view(_catalog())
    assert process_context_violation(view, _document(variables)) == field
    assert catalog_violation([view], _document(variables), IngestKind.FINDING, WINDOW) == field
    assert (
        catalog_violation([view], _document(variables), IngestKind.DETECTION_FOR_REVIEW, WINDOW)
        == field
    )


def test_a_block_against_a_catalog_without_variables() -> None:
    """Sin variables declaradas, un bloque vacío concuerda; uno con variables, no."""
    view = _view(_catalog(process=False))
    assert process_context_violation(view, _document([])) is None
    assert process_context_violation(view, _document([_analog()])) == (
        "process_context.variables[0]"
    )


def test_a_binary_variable_on_a_no_data_signal_is_never_valid() -> None:
    """``m_ignora_no_data``: la lectura válida y el cambio con valor dan su campo exacto."""
    view = _view(_catalog(jumbo_no_data=True))
    dead = _binary(valid=False)
    assert process_context_violation(view, _document([_analog(), dead], jumbo_valid=False)) is None
    assert (
        process_context_violation(view, _document([_analog(), _binary()], jumbo_valid=False))
        == "process_context.variables[1].at_event.valid"
    )
    changed = copy.deepcopy(dead)
    changed["changes"] = [
        {"offset_ms": -30_000, "value": "unknown"},
        {"offset_ms": -1_000, "value": "deasserted"},
    ]
    assert (
        process_context_violation(view, _document([changed, _analog()], jumbo_valid=False))
        == "process_context.variables[0].changes[1].value"
    )
    # Sin ``no_data`` la misma lectura válida concuerda.
    assert process_context_violation(_view(_catalog()), _document([_analog(), _binary()])) is None


def test_inside_step_7_signals_go_first_then_process_values_then_thresholds() -> None:
    view = _view(_catalog(jumbo_no_data=True))
    both = _document([_analog(UNDECLARED), _binary()], jumbo_valid=True, confidence=0.1)
    assert catalog_violation([view], both, IngestKind.FINDING, WINDOW) == "signals[1].valid"
    process_and_threshold = _document(
        [_analog(UNDECLARED), _binary(valid=False)], jumbo_valid=False
    )
    process_and_threshold["max_confidence"] = 0.1
    assert (
        catalog_violation([view], process_and_threshold, IngestKind.FINDING, WINDOW)
        == "process_context.variables[0]"
    )


def test_any_version_in_force_that_matches_accepts_and_otherwise_the_newest_decides() -> None:
    old = _view(_catalog(process=False), version=1)
    new = _view(_catalog(), version=2)
    document = _document([_analog(), _binary()])
    assert catalog_violation([old, new], document, IngestKind.FINDING, WINDOW) is None
    assert catalog_violation([old, new], _document([]), IngestKind.FINDING, WINDOW) is None
    wrong = _document([_analog(UNDECLARED)])
    assert (
        catalog_violation([old, new], wrong, IngestKind.FINDING, WINDOW)
        == "process_context.variables[0]"
    )
    missing = _document([_analog()])
    assert (
        catalog_violation([old, new], missing, IngestKind.FINDING, WINDOW)
        == "process_context.variables"
    )


def _photo_document(variables: list[dict[str, Any]] | None) -> dict[str, Any]:
    document: dict[str, Any] = {
        "standard": {"standard_id": STANDARD, "version": 1},
        "camera_id": CAMERA,
    }
    if variables is not None:
        document["process_context"] = {
            "from_offset_ms": -30_000,
            "to_offset_ms": 4_000,
            "variables": variables,
        }
    return document


def test_the_episode_photo_checks_its_process_values_after_the_camera() -> None:
    view = _view(_catalog())
    assert photo_violation([view], _photo_document(None), WINDOW) is None
    assert photo_violation([view], _photo_document([_binary(), _analog()]), WINDOW) is None
    assert (
        photo_violation([view], _photo_document([_analog(), _analog()]), WINDOW)
        == "process_context.variables[1]"
    )
    assert photo_violation([view], _photo_document([_analog()]), WINDOW) == (
        "process_context.variables"
    )
    foreign_camera = _photo_document([_analog(UNDECLARED)]) | {"camera_id": str(uuid.uuid4())}
    assert photo_violation([view], foreign_camera, WINDOW) == "camera_id"


def test_the_photo_row_keeps_the_block_as_canonical_json_or_null() -> None:
    from tests.episode_photo_support import submission

    started = T0 + dt.timedelta(days=1)
    base = submission(
        organization_id=uuid.uuid4(),
        plant_id=uuid.uuid4(),
        zone_id=uuid.uuid4(),
        node_id=uuid.uuid4(),
        standard_id=uuid.UUID(STANDARD),
        camera_id=uuid.UUID(CAMERA),
        started=started,
        absent_reason="frame_unavailable",
    )
    options: dict[str, Any] = {
        "data": None,
        "received_at": started + HOUR,
        "retention_days": 30,
        "canonical_sha256": "0" * 64,
        "platform_record_id": uuid.uuid4(),
    }
    assert photo_row(base, **options).process_context is None
    block = _photo_document([_binary(), _analog()])["process_context"]
    row = photo_row(base | {"process_context": block}, **options)
    assert row.process_context == canonicalize(block).decode("utf-8")
    assert json.loads(row.process_context) == block


# --- Recorrido con el contrato v1.6.0 -------------------------------------------------------------

SEEDS: Final = (20261123, 20261124, 20261125)


def _ready(seed: int, kind: IngestKind) -> tuple[IngestWorld, dict[str, Any]]:
    """Un catálogo del kit con dos o más variables de proceso y un registro con su bloque."""
    world = ingest_world()
    base = draw_seeded(zone_catalog(), seed)
    catalog = world.scoped(draw_seeded(with_process_context(base, minimum=2), seed))
    world.publish_catalog(catalog, world.now - YEAR)
    world.set_usage(True, world.now - YEAR)
    node = str(world.a.node_id)
    strategy = (
        finding(catalog, node_id=node, process_context=True)
        if kind is IngestKind.FINDING
        else detection_for_review(catalog, node_id=node, process_context=True)
    )
    document = place(draw_seeded(strategy, seed), world.now - HOUR)
    assert len(document["process_context"]["variables"]) >= 2
    return world, document


def _rejected_with(world: IngestWorld, response: Any, field: str) -> None:
    rejection = parse_rejection_response(response.content)
    assert (rejection.code.value, rejection.field) == ("schema_invalid", field), response.text
    assert world.store.records == {}
    assert world.writer.events == []


KINDS: Final = (IngestKind.FINDING, IngestKind.DETECTION_FOR_REVIEW)


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("kind", KINDS, ids=["hallazgo", "deteccion"])
def test_a_record_with_the_block_is_accepted_and_stored_byte_for_byte(
    kind: IngestKind, seed: int
) -> None:
    world, document = _ready(seed, kind)
    response = world.post(kind, document)
    assert response.status_code == 200, response.text
    (record,) = world.accepted(kind)
    stored = {key: value for key, value in record.content.items() if key != "receipt"}
    assert canonicalize(stored) == canonicalize(document)
    assert canonicalize(record.content["process_context"]) == canonicalize(
        document["process_context"]
    )


@pytest.mark.parametrize("kind", KINDS, ids=["hallazgo", "deteccion"])
def test_a_record_without_the_block_is_still_accepted(kind: IngestKind) -> None:
    world, document = _ready(SEEDS[0], kind)
    del document["process_context"]
    response = world.post(kind, document)
    assert response.status_code == 200, response.text
    (record,) = world.accepted(kind)
    assert "process_context" not in record.content


def _opposite(reading: dict[str, Any], low: int) -> dict[str, Any]:
    """La misma variable con la otra clase y una lectura mínima válida para el modelo."""
    if reading["kind"] == "analog":
        return {
            "variable_id": reading["variable_id"],
            "kind": "binary",
            "at_event": {"value": "unknown", "valid": False, "offset_ms": low},
            "changes": [],
            "truncated": False,
        }
    return {
        "variable_id": reading["variable_id"],
        "kind": "analog",
        "at_event": {"valid": False, "offset_ms": low},
        "samples": [],
    }


@pytest.mark.parametrize("kind", KINDS, ids=["hallazgo", "deteccion"])
@pytest.mark.parametrize("case", ["no_declarada", "falta", "otra_clase"])
def test_each_mismatch_is_schema_invalid_with_its_field_and_nothing_written(
    kind: IngestKind, case: str
) -> None:
    """``m_sin_paso7_proceso``: sin el paso, las tres presentaciones se aceptarían."""
    world, document = _ready(SEEDS[1], kind)
    block = document["process_context"]
    variables = block["variables"]
    if case == "no_declarada":
        variables[0] = {**variables[0], "variable_id": str(uuid.uuid4())}
        field = "process_context.variables[0]"
    elif case == "falta":
        variables.pop()
        field = "process_context.variables"
    else:
        variables[1] = _opposite(variables[1], block["from_offset_ms"])
        field = "process_context.variables[1]"
    _rejected_with(world, world.post(kind, document), field)


@pytest.mark.parametrize("kind", KINDS, ids=["hallazgo", "deteccion"])
def test_a_repeated_variable_is_rejected_by_the_model_before_step_7(kind: IngestKind) -> None:
    """Con v1.6.0 la repetida la rechaza el modelo (paso 4), con el campo de la lista."""
    world, document = _ready(SEEDS[2], kind)
    variables = document["process_context"]["variables"]
    variables.append(copy.deepcopy(variables[0]))
    _rejected_with(world, world.post(kind, document), "process_context.variables")


# --- Versión (BR-CTR-19) -------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["1.4.0", "1.4.9", "1.5.0", "1.5.2", "1.6.0", "1.6.7"])
def test_nodes_of_1_4_to_1_6_are_accepted(version: str) -> None:
    assert check_version([version], VersionPolicy(), T0).value.startswith("accepted")


def test_a_1_7_node_is_rejected_newer() -> None:
    with pytest.raises(NodeRejection) as rejected:
        check_version(["1.7.0"], VersionPolicy(), T0)
    assert rejected.value.code.value == "contract_version_unsupported"
    assert rejected.value.compatibility_result is not None
    assert rejected.value.compatibility_result.value == "rejected_newer"
