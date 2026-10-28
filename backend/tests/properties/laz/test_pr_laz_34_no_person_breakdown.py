"""PR-LAZ-34: gramática cerrada de entrada y salida, nunca búsqueda de subcadenas aleatorias."""

import inspect

from fastapi.routing import APIRoute
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from tests.isolation.laz_cases import LAZ_CASES, LAZ_OPERATION_CASES
from tests.properties.laz.strategies.program import ProgramHistory, program_histories
from tests.properties.laz.test_pr_laz_01_no_person_parameters import _words, route_names
from tests.properties.laz.test_pr_laz_33_metrics_oracle import calculate
from vigia_platform.accreditation.metrics.model import MetricValue
from vigia_platform.accreditation.metrics.service import ProgramMetricsService
from vigia_platform.loop.domain.enums import MetricKey
from vigia_platform.shared.api.app import build_openapi_app
from vigia_platform.shared.api.declarations import iter_declared_routes

FORBIDDEN = ("person_id", "user_id", "team_id", "shift", "employee", "productivity", "performance")
ROUTE_FORBIDDEN = frozenset({"person", "employee", "team", "shift", "productivity", "performance"})
ROUTE_EXCEPTIONS = frozenset(
    {("POST", "/loop/actions/{action_id}/evidence", "body", "no_identifiable_person_declared")}
)


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


def test_pr_laz_34_no_u04_route_accepts_a_person_or_productivity_breakdown() -> None:
    """Las 41 operaciones no añaden una desagregación prohibida fuera de métricas.

    ``user_id`` no se incluye aquí: identifica a quien decide dentro de la plataforma (P8), no a
    una persona observada. Las rutas de métricas siguen cubiertas por la comprobación estricta de
    arriba, que sí prohíbe ese parámetro.
    """
    routes = [
        route
        for declared in iter_declared_routes(build_openapi_app().routes)
        if declared.path.startswith(("/loop", "/accreditation", "/notifications"))
        and isinstance(route := declared.route, APIRoute)
    ]
    found = [
        f"{method} {route.path}: {place} {name}"
        for route in routes
        for method in route.methods
        for place, name in route_names(route)
        if _words(name) & ROUTE_FORBIDDEN
        and (method, route.path, place, name) not in ROUTE_EXCEPTIONS
    ]
    assert len(routes) == len(dict(LAZ_CASES)) == 43
    assert set(LAZ_OPERATION_CASES) == {f"SUP-U05-{number:02d}" for number in range(1, 42)}
    assert found == []
