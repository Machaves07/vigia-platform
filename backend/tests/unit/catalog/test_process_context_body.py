"""Cuerpo de ``PUT /zones/{zone_id}/process-context`` como lo valida FastAPI (TASK-594).

FastAPI valida el cuerpo ya decodificado (modo Python). Los modelos del contrato son estrictos y,
en ese modo, no admiten un enumerado ni un UUID escritos como texto: el cuerpo pasa cada variable
por el lector del contrato en modo JSON. Sin eso, toda petición válida respondía ``400`` (lo vio
el CI de la ronda 2). Lo que no cumple el contrato sigue siendo un error de validación. Solo datos
sintéticos, con las escalas de Pope.
"""

from __future__ import annotations

import json
from typing import Any, Final

import pytest
from pydantic import ValidationError

from vigia_platform.catalog.adapters.http.process_context import ProcessContextBody

SPEED: Final = {
    "variable_id": "0190a1b2-5555-7000-8000-000000000101",
    "code": "VEL-MAQ",
    "kind": "analog",
    "description_es": "Velocidad de la máquina",
    "source": {"reader": "waveshare", "channel": 0},
    "unit": "m_per_min",
    "scale": {"at_4_ma": 0, "at_20_ma": 2000},
    "decimals": 0,
}
JUMBO: Final = {
    "variable_id": "0190a1b2-5555-7000-8000-000000000102",
    "code": "JUMBO-POS",
    "kind": "binary",
    "description_es": "Jumbo en posición",
    "signal_id": "0190a1b2-4444-7000-8000-000000000102",
    "states": "in_position_out",
}


def _decoded(**changes: Any) -> dict[str, Any]:
    """El cuerpo tal como lo entrega FastAPI: JSON ya decodificado a ``dict``."""
    body = {"before_ms": 30_000, "variables": [SPEED, JUMBO], "reason_es": "Motivo sintético"}
    decoded: dict[str, Any] = json.loads(json.dumps({**body, **changes}))
    return decoded


def test_a_valid_decoded_body_passes_in_python_mode() -> None:
    body = ProcessContextBody.model_validate(_decoded())
    assert [variable.to_json_value() for variable in body.variables] == [
        {**SPEED, "scale": {"at_4_ma": 0.0, "at_20_ma": 2000.0}},
        JUMBO,
    ]
    assert ProcessContextBody.model_validate(_decoded(variables=[])).variables == []


@pytest.mark.parametrize(
    "variable",
    [
        pytest.param({**SPEED, "kind": "digital"}, id="clase-desconocida"),
        pytest.param({**SPEED, "unit": "furlong"}, id="unidad-desconocida"),
        pytest.param({**SPEED, "extra": True}, id="campo-de-mas"),
        pytest.param({**SPEED, "variable_id": "no-es-uuid"}, id="uuid"),
        pytest.param({**SPEED, "decimals": 4}, id="decimales"),
        pytest.param("VEL-MAQ", id="no-objeto"),
    ],
)
def test_what_the_contract_rejects_is_still_a_validation_error(variable: Any) -> None:
    with pytest.raises(ValidationError):
        ProcessContextBody.model_validate(_decoded(variables=[variable]))
