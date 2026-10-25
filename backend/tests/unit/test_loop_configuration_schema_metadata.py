"""SUP-U05-39: límites generables de una sola fuente sin cambiar los rechazos de A-68."""

from __future__ import annotations

import pytest

from vigia_platform.loop.adapters.http.configuration_routes import ConfigurationChangeBody
from vigia_platform.loop.config.parameters import PARAMETERS, IntegerParameter
from vigia_platform.loop.config.service import REASON_MAX_CHARS, REASON_MIN_CHARS


def test_schema_publishes_domain_limits_without_copying_them() -> None:
    properties = ConfigurationChangeBody.model_json_schema()["properties"]
    for parameter in PARAMETERS:
        branch = next(
            item for item in properties[parameter.name]["anyOf"] if item.get("type") == "integer"
        )
        assert (branch["minimum"], branch["maximum"]) == (parameter.minimum, parameter.maximum)
    reason = next(item for item in properties["reason"]["anyOf"] if item.get("type") == "string")
    assert (reason["minLength"], reason["maxLength"]) == (REASON_MIN_CHARS, REASON_MAX_CHARS)


@pytest.mark.parametrize("parameter", PARAMETERS, ids=lambda parameter: parameter.name)
def test_schema_annotations_keep_out_of_range_values_for_domain_rejection(
    parameter: IntegerParameter,
) -> None:
    # Los 400/detail_code siguen saliendo del servicio, no de un error Pydantic sin detalle.
    for value in (parameter.minimum - 1, parameter.maximum + 1):
        body = ConfigurationChangeBody.model_validate({parameter.name: value, "reason": "x"})
        assert getattr(body, parameter.name) == value
        assert body.reason == "x"


def test_schema_annotations_do_not_turn_missing_reason_into_http_validation() -> None:
    assert ConfigurationChangeBody.model_validate({}).reason is None
