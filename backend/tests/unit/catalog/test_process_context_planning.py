"""Planificación pura de las variables de proceso de la zona (TASK-594; A-129, BR-GOB-52).

``SetProcessContext`` compone la versión siguiente con ``process_context`` (o sin él, con la
lista vacía) y ``changed_fields = (process_context,)``; las reglas son las del modelo del contrato
(regla 7 del catálogo), así que se rechazan antes de escribir nada con ``request_invalid``.
Cualquier otro cambio conserva el bloque. ``publication_rows`` no marca la regresión por un cambio
de variables (Q7) y sí por uno de señales. Solo datos generados, con las escalas de Pope.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

import pytest

from tests.unit.catalog.test_catalog_planning import _first, _plan, _state, _violation
from vigia_platform.catalog.domain.catalog_version import (
    ALL_CHANGED_FIELDS,
    CatalogViolation,
    SetProcessContext,
    SetSignals,
    SetThresholds,
)
from vigia_platform.catalog.domain.enums import CatalogChangedField
from vigia_platform.catalog.domain.regression import ZONE_WIDE_FIELDS, publication_rows

GUARD: Final = {
    "signal_id": str(uuid.UUID(int=700, version=4)),
    "code": "SG-JUMBO",
    "role": "guard",
    "asserted_level": "high",
    "source": {"reader": "waveshare", "channel": 5},
    "description_es": "Jumbo en posición",
}
SPEED: Final = {
    "variable_id": str(uuid.UUID(int=800, version=4)),
    "code": "VEL-MAQ",
    "kind": "analog",
    "description_es": "Velocidad de la máquina",
    "source": {"reader": "waveshare", "channel": 0},
    "unit": "m_per_min",
    "scale": {"at_4_ma": 0.0, "at_20_ma": 2000.0},
    "decimals": 0,
}
METERS: Final = {
    **SPEED,
    "variable_id": str(uuid.UUID(int=801, version=4)),
    "code": "MET-JUMBO",
    "description_es": "Metros en bobina",
    "source": {"reader": "waveshare", "channel": 1},
    "unit": "m",
    "scale": {"at_4_ma": 0.0, "at_20_ma": 50_000.0},
}
JUMBO: Final = {
    "variable_id": str(uuid.UUID(int=802, version=4)),
    "code": "JUMBO-POS",
    "kind": "binary",
    "description_es": "Jumbo en posición",
    "signal_id": GUARD["signal_id"],
    "states": "in_position_out",
}


def _numbered(n: int) -> dict[str, Any]:
    """Variables analógicas distintas: solo las separa su número."""
    return {
        **SPEED,
        "variable_id": str(uuid.UUID(int=900 + n, version=4)),
        "code": f"VAR-{n}",
        "source": {"reader": "waveshare", "channel": n},
    }


def test_twelve_distinct_variables_are_the_maximum() -> None:
    plan = _plan(_declared(), _set(*(_numbered(n) for n in range(12))))
    assert len(plan.catalog["process_context"]["variables"]) == 12


def _declared() -> Any:
    return _state(_first(signals=(GUARD,)))


def _set(*variables: dict[str, Any], before_ms: int = 30_000) -> SetProcessContext:
    return SetProcessContext(before_ms=before_ms, variables=variables)


def test_the_block_enters_version_n_plus_1_and_only_process_context_changed() -> None:
    previous = _declared()

    plan = _plan(previous, _set(SPEED, METERS, JUMBO))

    assert plan.catalog_version == 2 == plan.catalog["version"]
    assert plan.changed_fields == (CatalogChangedField.PROCESS_CONTEXT,)
    assert plan.catalog["process_context"] == {
        "before_ms": 30_000,
        "variables": [SPEED, METERS, JUMBO],
    }
    assert plan.born == () and plan.cameras is None and not plan.single_occupancy_declared


def test_an_empty_list_withdraws_the_block_and_other_changes_keep_it() -> None:
    with_block = _state(_plan(_declared(), _set(SPEED, JUMBO)))

    kept = _plan(with_block, SetThresholds(thresholds={"review": 0.3, "publication": 0.9}))
    assert kept.catalog["process_context"] == with_block.catalog["process_context"]

    withdrawn = _plan(with_block, _set())
    assert "process_context" not in withdrawn.catalog
    assert withdrawn.changed_fields == (CatalogChangedField.PROCESS_CONTEXT,)


@pytest.mark.parametrize(
    "variables",
    [
        pytest.param(({**JUMBO, "signal_id": str(uuid.uuid4())},), id="senal-no-declarada"),
        pytest.param((SPEED, {**METERS, "code": SPEED["code"]}), id="codigo-repetido"),
        pytest.param(
            (SPEED, {**METERS, "variable_id": SPEED["variable_id"]}), id="identificador-repetido"
        ),
        pytest.param(({**SPEED, "scale": {"at_4_ma": 4.0, "at_20_ma": 4.0}},), id="escala-plana"),
        pytest.param(({**SPEED, "signal_id": GUARD["signal_id"]},), id="analogica-con-senal"),
        pytest.param(({k: v for k, v in JUMBO.items() if k != "states"},), id="estado-sin-par"),
        pytest.param(tuple(_numbered(n) for n in range(13)), id="trece"),
    ],
)
def test_a_contract_rule_violation_is_request_invalid(variables: tuple[Any, ...]) -> None:
    assert _violation(_declared(), _set(*variables)) is CatalogViolation.REQUEST_INVALID


@pytest.mark.parametrize("before_ms", [-1, 120_001])
def test_the_window_before_is_0_to_120_000_ms(before_ms: int) -> None:
    assert _violation(_declared(), _set(SPEED, before_ms=before_ms)) is (
        CatalogViolation.REQUEST_INVALID
    )
    assert _plan(_declared(), _set(SPEED, before_ms=120_000)).catalog_version == 2


def test_a_signal_cited_by_a_variable_cannot_be_withdrawn() -> None:
    with_block = _state(_plan(_declared(), _set(JUMBO)))

    assert _violation(with_block, SetSignals(signals=())) is CatalogViolation.REQUEST_INVALID


def test_version_1_never_declares_process_context() -> None:
    assert CatalogChangedField.PROCESS_CONTEXT not in ALL_CHANGED_FIELDS
    assert len(ALL_CHANGED_FIELDS) == 8
    assert "process_context" not in _first().catalog


def test_q7_only_the_variables_never_mark_the_regression_and_signals_do() -> None:
    """La mutación con nombre ``m_regresion_completa`` (``process_context`` en la zona)."""
    previous = _declared()
    process = _plan(previous, _set(SPEED, JUMBO))
    assert CatalogChangedField.PROCESS_CONTEXT not in ZONE_WIDE_FIELDS
    assert publication_rows(previous.catalog, process.catalog, process.changed_fields) is None

    signals = _plan(_state(process), SetSignals(signals=(GUARD,)))
    assert publication_rows(process.catalog, signals.catalog, signals.changed_fields) == "all"
