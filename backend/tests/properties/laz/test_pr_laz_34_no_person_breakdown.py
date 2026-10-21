"""PR-LAZ-34: gramática cerrada de entrada y salida, nunca búsqueda de subcadenas aleatorias."""

import inspect

from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from tests.properties.laz.strategies.program import ProgramHistory, program_histories
from tests.properties.laz.test_pr_laz_33_metrics_oracle import calculate
from vigia_platform.accreditation.metrics.model import MetricValue
from vigia_platform.accreditation.metrics.service import ProgramMetricsService
from vigia_platform.loop.domain.enums import MetricKey

FORBIDDEN = ("person_id", "user_id", "team_id", "shift", "employee", "productivity", "performance")


@given(program_histories(), st.sampled_from(FORBIDDEN))
def test_pr_laz_34_no_person_breakdown(history: ProgramHistory, field: str) -> None:
    assert set(MetricKey) == {v.key for v in calculate(history)}
    assert not set(FORBIDDEN) & set(inspect.signature(ProgramMetricsService.read).parameters)
    assert not set(FORBIDDEN) & set(inspect.signature(ProgramMetricsService.snapshot).parameters)
    for metric in calculate(history):
        raw = metric.model_dump()
        assert not set(FORBIDDEN) & raw.keys()
        try:
            MetricValue.model_validate({**raw, field: "dato prohibido"})
        except ValidationError:
            pass
        else:
            raise AssertionError("el esquema de métricas aceptó una dimensión prohibida")
