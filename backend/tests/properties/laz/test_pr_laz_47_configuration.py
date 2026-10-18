"""PR-LAZ-47: un cambio de configuración exige motivo, deja un registro y no mueve lo ya fijado.

TASK-304 (BR-LAZ-144, 145; LC-LAZ-19). Con ``configuration_changes`` sobre una planta nueva por
ejemplo, contra PostgreSQL 16 real como ``vigia_app`` y el ``LoopConfigurationService`` real:

- **Motivo**: un cambio sin motivo (ausente, vacío o de menos de 10 caracteres) se rechaza con
  ``configuration_change_reason_required`` y no escribe nada, aunque sus valores sean válidos.
- **Exactamente un registro**: un cambio aceptado que mueve algo escribe exactamente un
  ``loop_configuration_changed`` con la versión siguiente y, por cada parámetro que cambia, su
  valor anterior y el nuevo; uno que no mueve nada, ninguno; uno rechazado (motivo, rango o regla
  entre parámetros, según un oráculo propio sobre la tabla de DE §2.13), ninguno.
- **Lo ya fijado no cambia**: un **doble de otro módulo** fija plazos de revisión intercalados con
  los cambios, leyendo ``loop.config`` por su interfaz pública (``repository.settings_in``) en su
  propia transacción, y guarda el plazo y la versión con que lo calculó. Al final, cada plazo
  guardado sigue igual y es el que da la configuración **de su versión**, reconstruida plegando los
  registros del expediente; la fila vigente es el pliegue de todos (la fila es la proyección).

Solo datos generados.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

import pytest
from hypothesis import HealthCheck, given, settings

from tests.integration.conftest import PostgresEndpoint
from tests.loop_config_support import LoopConfigStack, loop_config_stack
from tests.properties.laz.strategies.calendar import (
    AssignDeadline,
    ChangeConfiguration,
    configuration_changes,
)
from vigia_platform.loop.calendar import BusinessCalendar, add_business_days
from vigia_platform.loop.config.parameters import (
    BUSINESS_CALENDAR,
    PARAMETERS,
    ConfigurationRejected,
    LoopSettings,
)
from vigia_platform.loop.config.service import ConfigurationRequestInvalid
from vigia_platform.loop.detail_codes import LoopDetailCode

pytestmark = pytest.mark.integration

EXAMPLES: Final = 25
"""Cada ejemplo son varias transacciones reales; el perfil ``nightly`` no sube este número."""
_RANGES: Final = {p.name: (p.minimum, p.maximum) for p in PARAMETERS}
_ORDERED: Final = (
    ("action_deadline_min_days", "action_deadline_default_days"),
    ("action_deadline_default_days", "action_deadline_max_days"),
    ("unclosable_review_default_months", "unclosable_review_max_months"),
    ("sample_min", "sample_max"),
)
"""Las reglas entre parámetros de DE §2.13 (y de las restricciones de la tabla), reescritas."""
REVIEW: Final = "review_deadline_business_days"


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[LoopConfigStack]:
    with loop_config_stack(postgres_endpoint, "pr_laz_47") as built:
        yield built


@dataclass(frozen=True)
class Assigned:
    """Un plazo fijado por el doble: lo que guardó y con qué versión lo calculó."""

    start: date
    version: int | None
    deadline: date


def _expected(
    current: dict[str, Any], change: ChangeConfiguration
) -> tuple[str, dict[str, Any] | None]:
    """Oráculo: ``rejected:<código>``, ``unchanged`` o ``changed`` con lo que queda."""
    reason = change.reason
    if reason is None or len(reason.strip()) < 10:
        return "rejected:" + LoopDetailCode.CONFIGURATION_CHANGE_REASON_REQUIRED.value, None
    for name, value in change.integers.items():
        low, high = _RANGES[name]
        if not low <= value <= high:
            return "rejected:" + LoopDetailCode.DEADLINE_OUT_OF_RANGE.value, None
    following = {**current, **change.integers}
    if change.calendar is not None:
        following[BUSINESS_CALENDAR] = change.calendar
    if any(following[low] > following[high] for low, high in _ORDERED):
        return "rejected:" + LoopDetailCode.DEADLINE_OUT_OF_RANGE.value, None
    return ("unchanged" if following == current else "changed"), following


def _fold(records: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """La configuración de cada versión, plegando los registros del expediente en orden."""
    versions: dict[int, dict[str, Any]] = {}
    state: dict[str, Any] = {}
    for record in records:
        content = record["content"]
        previous = {item["parameter"]: _value(item) for item in content["previous_values"]}
        assert all(state.get(name) == value for name, value in previous.items()), content
        for item in content["new_values"]:
            state[item["parameter"]] = _value(item)
        assert content["version"] == len(versions) + 1
        versions[content["version"]] = dict(state)
    return versions


def _value(item: Mapping[str, Any]) -> Any:
    return (
        item["business_calendar"]
        if item["parameter"] == BUSINESS_CALENDAR
        else item["integer_value"]
    )


def _review_deadline(state: Mapping[str, Any], start: date) -> date:
    calendar = BusinessCalendar.from_document(state[BUSINESS_CALENDAR])
    return add_business_days(start, int(state[REVIEW]), calendar)


def _defaults() -> dict[str, Any]:
    settings_ = LoopSettings.defaults()
    return {**settings_.integers, BUSINESS_CALENDAR: settings_.business_calendar.document()}


def test_pr_laz_47_reason_one_record_and_nothing_already_fixed_moves(
    stack: LoopConfigStack,
) -> None:
    @settings(
        max_examples=EXAMPLES,
        deadline=None,
        database=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(configuration_changes())
    def run(operations: list[Any]) -> None:
        site = stack.site(plants=1)
        (plant,) = site.plants
        context = stack.context(stack.member(site))
        service = stack.service
        current: dict[str, Any] | None = None
        version = 0
        assigned: list[Assigned] = []
        for operation in operations:
            if isinstance(operation, AssignDeadline):
                assigned.append(_assign(stack, context, plant, operation.start))
                continue
            before = len(stack.records(plant))
            base = _defaults() if current is None else current
            outcome, following = _expected(base, operation)
            try:
                result = stack.run(
                    service.change(
                        context,
                        plant,
                        integers=operation.integers,
                        business_calendar=operation.calendar,
                        reason=operation.reason,
                    )
                )
            except (ConfigurationRejected, ConfigurationRequestInvalid) as error:
                code = getattr(error, "detail_code", None)
                assert outcome == f"rejected:{code.value if code else None}", (operation, error)
                assert len(stack.records(plant)) == before
                continue
            created = current is None
            if created:
                version = 1  # el cambio creó antes los valores por defecto con su registro
            assert following is not None, (operation, outcome)
            if outcome == "changed":
                version += 1
            assert result.version == version
            assert len(stack.records(plant)) == before + int(created) + int(outcome == "changed")
            if outcome == "changed":
                last = stack.records(plant)[-1]["content"]
                names = [item["parameter"] for item in last["new_values"]]
                assert names and len(names) == len(set(names))
                for item, old in zip(last["new_values"], last["previous_values"], strict=True):
                    name = item["parameter"]
                    assert old["parameter"] == name
                    assert _value(item) == following[name] != base[name] == _value(old)
                assert {n for n in following if following[n] != base[n]} == set(names)
                assert last["reason"] == operation.reason
            current = following
        records = stack.records(plant)
        folded = _fold(records)
        if folded:
            row = stack.row(plant)
            final = folded[max(folded)]
            assert row["version"] == max(folded)
            assert {name: row[name] for name in _RANGES} == {n: final[n] for n in _RANGES}
        for fixed in assigned:
            state = _defaults() if fixed.version is None else folded[fixed.version]
            assert _review_deadline(state, fixed.start) == fixed.deadline, fixed

    run()


def _assign(stack: LoopConfigStack, context: Any, plant: uuid.UUID, start: date) -> Assigned:
    """El doble de otro módulo: lee ``loop.config`` en su transacción y fija un plazo."""

    async def fix() -> Assigned:
        async with stack.database.transaction(context) as transaction:
            settings_, version = await stack.service.repository.settings_in(transaction, plant)
            deadline = add_business_days(start, settings_[REVIEW], settings_.business_calendar)
        return Assigned(start=start, version=version, deadline=deadline)

    fixed: Assigned = stack.run(fix())
    return fixed
