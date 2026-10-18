"""SUP-U05-38 y 39: las rutas de la configuración del lazo (TASK-304; LC-LAZ-19; BR-LAZ-144, 145).

Sobre PostgreSQL 16 real como ``vigia_app`` y la aplicación real (``loop_config_stack``):

- **Valores por defecto**: la primera lectura crea la fila (versión 1, los valores de la tabla de
  DE §2.13, ``timezone`` de ``Plant.timezone``) y **un** registro de origen con motivo
  ``initial_defaults``; las lecturas siguientes no escriben. Varias primeras lecturas a la vez
  crean una sola fila y un solo registro.
- **Cambio**: versión siguiente, exactamente un ``loop_configuration_changed`` con los valores
  anterior y nuevo de lo que cambia, la fila igual a la respuesta; sin cambio, nada escrito.
- **Bordes por parámetro** (tabla de DE §2.13, ``PARAMETERS``): mínimo y máximo aceptados; mínimo
  - 1 y máximo + 1, ``loop_deadline_out_of_range``; un valor que no es un entero,
  ``invalid_request`` sin ``detail_code``. Reglas entre parámetros, el calendario y el motivo
  (sin motivo, ``loop_configuration_change_reason_required``).
- **Concurrencia**: dos ``PUT`` simultáneos sobre la misma planta dan dos versiones consecutivas y
  dos registros, nunca dos con la misma ``version``.
- **Alcance**: otra organización, planta inexistente, otra planta, alcance de zona, un rol sin
  ``organization.settings`` y la concesión de proveedor responden ``not_found`` sin escribir.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any, Final

import pytest

from tests.authz_support import Site
from tests.integration.conftest import PostgresEndpoint
from tests.loop_config_support import REASON, LoopConfigStack, Who, loop_config_stack
from vigia_platform.loop.config.parameters import PARAMETERS, IntegerParameter, LoopSettings
from vigia_platform.shared.context import Role, ScopeLevel

pytestmark = pytest.mark.integration

DEFAULTS: Final = LoopSettings.defaults()
_ORDERED: Final = (
    ("action_deadline_min_days", "action_deadline_default_days"),
    ("action_deadline_default_days", "action_deadline_max_days"),
    ("unclosable_review_default_months", "unclosable_review_max_months"),
    ("sample_min", "sample_max"),
)


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[LoopConfigStack]:
    with loop_config_stack(postgres_endpoint, "loop_configuration_routes") as built:
        yield built


@pytest.fixture
def site(stack: LoopConfigStack) -> Site:
    return stack.site()


@pytest.fixture
def admin(stack: LoopConfigStack, site: Site) -> Who:
    return stack.member(site)


def _plant(site: Site, index: int = 0) -> uuid.UUID:
    return list(site.plants)[index]


def _error(response: Any) -> tuple[int, str | None, str | None]:
    body = response.json()
    return response.status_code, body.get("code"), body.get("detail_code")


def _comparable(response: Any) -> tuple[int, Any]:
    body = response.json()
    return response.status_code, {k: v for k, v in body.items() if k != "correlation_id"}


def _consistent(name: str, value: int) -> dict[str, int]:
    """Los 19 enteros con ``name = value`` y los demás ajustados a las reglas entre parámetros."""
    values = dict(DEFAULTS.integers)
    values[name] = value
    for _ in range(2):
        for low, high in _ORDERED:
            if values[low] > values[high]:
                if name == high:
                    values[low] = values[high]
                else:
                    values[high] = values[low]
    return values


# --- valores por defecto ------------------------------------------------------------------------


def test_the_first_read_creates_the_defaults_with_their_origin_record(
    stack: LoopConfigStack, site: Site, admin: Who
) -> None:
    plant = _plant(site)
    first = stack.get(admin, plant)
    assert first.status_code == 200, first.text
    body = first.json()
    assert {name: body[name] for name in DEFAULTS.integers} == dict(DEFAULTS.integers)
    assert body["business_calendar"] == {
        "working_weekdays": [1, 2, 3, 4, 5],
        "non_working_dates": [],
    }
    assert body["timezone"] == "America/Bogota"
    assert (body["version"], body["change_reason"]) == (1, "initial_defaults")
    assert body["changed_by"]["role_in_use"] == "administrator"
    (record,) = stack.records(plant)
    content = record["content"]
    assert (content["version"], content["reason"], content["previous_values"]) == (
        1,
        "initial_defaults",
        [],
    )
    assert len(content["new_values"]) == len(PARAMETERS) + 1
    assert content["applies_to_new_items_only"] is True
    assert record["actor_unit"] == "U-04"
    # La segunda lectura no escribe nada y devuelve lo mismo.
    second = stack.get(admin, plant)
    assert second.json() == body
    assert len(stack.records(plant)) == 1
    # Otra planta de la misma organización tiene su propia configuración.
    assert stack.get(admin, _plant(site, 1)).json()["plant_id"] == str(_plant(site, 1))


def test_simultaneous_first_reads_create_one_row_and_one_record(
    stack: LoopConfigStack, site: Site, admin: Who
) -> None:
    plant = _plant(site)
    context = stack.context(admin)

    async def all_at_once() -> list[Any]:
        return await asyncio.gather(
            *(stack.service.configuration(context, plant) for _ in range(4)),
            return_exceptions=True,
        )

    results = stack.run(all_at_once())
    assert [type(r).__name__ for r in results] == ["PlantLoopConfiguration"] * 4, results
    assert {r.version for r in results} == {1}
    assert len(stack.records(plant)) == 1


# --- cambio --------------------------------------------------------------------------------------


def test_a_change_writes_one_record_with_previous_and_new_values(
    stack: LoopConfigStack, site: Site, admin: Who
) -> None:
    plant = _plant(site)
    changed = stack.put(
        admin, plant, {"review_deadline_business_days": 7, "sample_percent": 15, "reason": REASON}
    )
    assert changed.status_code == 200, changed.text
    body = changed.json()
    assert (body["version"], body["review_deadline_business_days"], body["sample_percent"]) == (
        2,
        7,
        15,
    )
    assert body["change_reason"] == REASON
    origin, change = stack.records(plant)
    assert origin["content"]["version"] == 1
    assert change["content"]["version"] == 2
    assert change["content"]["previous_values"] == [
        {"parameter": "review_deadline_business_days", "integer_value": 5},
        {"parameter": "sample_percent", "integer_value": 10},
    ]
    assert change["content"]["new_values"] == [
        {"parameter": "review_deadline_business_days", "integer_value": 7},
        {"parameter": "sample_percent", "integer_value": 15},
    ]
    assert change["content"]["reason"] == REASON
    assert change["actor_role_in_use"] == "administrator"
    row = stack.row(plant)
    assert (row["version"], row["review_deadline_business_days"], row["sample_percent"]) == (
        2,
        7,
        15,
    )
    assert stack.get(admin, plant).json() == body
    # La otra planta no cambió (el filtro por planta del repositorio).
    other = stack.get(admin, _plant(site, 1)).json()
    assert (other["version"], other["review_deadline_business_days"]) == (1, 5)
    # Lo mismo otra vez no es un cambio: ni versión ni registro.
    same = stack.put(admin, plant, {"review_deadline_business_days": 7, "reason": REASON})
    assert same.json()["version"] == 2
    assert len(stack.records(plant)) == 2


def test_the_calendar_is_declared_and_recorded(
    stack: LoopConfigStack, site: Site, admin: Who
) -> None:
    plant = _plant(site)
    calendar = {"working_weekdays": [1, 2, 3, 4, 5, 6], "non_working_dates": ["2026-12-25"]}
    changed = stack.put(admin, plant, {"business_calendar": calendar, "reason": REASON})
    assert changed.status_code == 200, changed.text
    assert changed.json()["business_calendar"] == calendar
    change = stack.records(plant)[-1]["content"]
    assert change["new_values"] == [
        {"parameter": "business_calendar", "business_calendar": calendar}
    ]
    assert change["previous_values"] == [
        {
            "parameter": "business_calendar",
            "business_calendar": {"working_weekdays": [1, 2, 3, 4, 5], "non_working_dates": []},
        }
    ]
    forty = [f"2026-01-{day:02d}" for day in range(1, 32)] + [
        f"2026-02-{day:02d}" for day in range(1, 10)
    ]
    assert (
        stack.put(
            admin,
            plant,
            {
                "business_calendar": {"working_weekdays": [1], "non_working_dates": forty},
                "reason": REASON,
            },
        ).status_code
        == 200
    )
    for bad in (
        {"working_weekdays": [1], "non_working_dates": [*forty, "2026-02-10"]},
        {"working_weekdays": [8], "non_working_dates": []},
        {"working_weekdays": [], "non_working_dates": []},
        {"working_weekdays": [1, 1], "non_working_dates": []},
        {"working_weekdays": [1], "non_working_dates": ["2026-02-30"]},
        {"working_weekdays": [1], "non_working_dates": ["2026-01-01", "2026-01-01"]},
        {"working_weekdays": [1.0], "non_working_dates": []},
        {"working_weekdays": [1], "non_working_dates": [], "extra": 1},
        None,
    ):
        response = stack.put(admin, plant, {"business_calendar": bad, "reason": REASON})
        assert _error(response) == (400, "invalid_request", None), (bad, response.text)


# --- bordes de cada parámetro --------------------------------------------------------------------


@pytest.mark.parametrize("parameter", PARAMETERS, ids=lambda p: p.name)
def test_each_parameter_accepts_its_borders_and_rejects_beyond(
    stack: LoopConfigStack, site: Site, admin: Who, parameter: IntegerParameter
) -> None:
    plant = _plant(site)
    for value in (parameter.minimum, parameter.maximum):
        body = {**_consistent(parameter.name, value), "reason": REASON}
        response = stack.put(admin, plant, body)
        assert response.status_code == 200, (value, response.text)
        assert response.json()[parameter.name] == value
    written = len(stack.records(plant))
    for value in (parameter.minimum - 1, parameter.maximum + 1):
        response = stack.put(admin, plant, {parameter.name: value, "reason": REASON})
        assert _error(response) == (400, "invalid_request", "loop_deadline_out_of_range"), value
    for value in (float(parameter.default), str(parameter.default), True, None, [1], 2**63):
        response = stack.put(admin, plant, {parameter.name: value, "reason": REASON})
        assert _error(response)[:2] == (400, "invalid_request"), (value, response.text)
        if value != 2**63:
            assert _error(response)[2] is None, value
    assert len(stack.records(plant)) == written


def test_rules_between_parameters(stack: LoopConfigStack, site: Site, admin: Who) -> None:
    plant = _plant(site)
    for body in (
        {"action_deadline_min_days": 31},  # min > default (30)
        {"action_deadline_max_days": 29},  # max < default
        {"action_deadline_default_days": 1, "action_deadline_min_days": 2},
        {"unclosable_review_default_months": 12, "unclosable_review_max_months": 11},
        {"sample_min": 31},  # min > max (30)
        {"sample_max": 4},
    ):
        response = stack.put(admin, plant, {**body, "reason": REASON})
        assert _error(response) == (400, "invalid_request", "loop_deadline_out_of_range"), body
    # Al borde de cada regla: iguales se aceptan.
    response = stack.put(admin, plant, {"sample_min": 30, "sample_max": 30, "reason": REASON})
    assert response.status_code == 200, response.text
    assert len(stack.records(plant)) == 2


def test_the_reason_is_mandatory(stack: LoopConfigStack, site: Site, admin: Who) -> None:
    plant = _plant(site)
    change = {"review_deadline_business_days": 9}
    for reason in (None, "", "   ", "\t\n", "123456789", " 12345678 "):
        body = {**change} if reason is None else {**change, "reason": reason}
        response = stack.put(admin, plant, body)
        assert _error(response) == (
            400,
            "invalid_request",
            "loop_configuration_change_reason_required",
        ), repr(reason)
    explicit_null = stack.put(admin, plant, {**change, "reason": None})
    assert _error(explicit_null)[2] == "loop_configuration_change_reason_required"
    for reason in (
        "x" * 501,
        12345678901,
        "Motivo con \u200b invisible largo",
        "<b>negrita</b> sí",
    ):
        response = stack.put(admin, plant, {**change, "reason": reason})
        assert _error(response) == (400, "invalid_request", None), repr(reason)
    # Sin motivo, el rango no se mira: el motivo va primero.
    response = stack.put(admin, plant, {"review_deadline_business_days": 0})
    assert _error(response)[2] == "loop_configuration_change_reason_required"
    assert stack.records(plant) == []
    for reason in ("1234567890", "y" * 500):
        response = stack.put(admin, plant, {**change, "reason": reason})
        assert response.status_code == 200, response.text
        change = {"review_deadline_business_days": 10}
    assert [r["content"]["version"] for r in stack.records(plant)] == [1, 2, 3]


def test_unknown_fields_and_empty_changes_are_invalid(
    stack: LoopConfigStack, site: Site, admin: Who
) -> None:
    plant = _plant(site)
    for body in (
        {"reason": REASON},
        {"timezone": "UTC", "reason": REASON},
        {"version": 3, "reason": REASON},
        {"review_deadline": 3, "reason": REASON},
        [],
    ):
        assert _error(stack.put(admin, plant, body)) == (400, "invalid_request", None), body
    query = stack.send(
        admin,
        "PUT",
        f"/loop/plants/{plant}/configuration?x=1",
        {"sample_max": 31, "reason": REASON},
    )
    assert _error(query)[:2] == (400, "invalid_request")
    assert stack.records(plant) == []


# --- concurrencia --------------------------------------------------------------------------------


@pytest.mark.parametrize("round_", range(3))
def test_two_simultaneous_puts_give_two_consecutive_versions(
    stack: LoopConfigStack, site: Site, admin: Who, round_: int
) -> None:
    plant = _plant(site)
    context = stack.context(admin)
    service = stack.service
    assert stack.get(admin, plant).json()["version"] == 1

    async def both() -> list[Any]:
        return await asyncio.gather(
            service.change(
                context, plant, integers={"sample_max": 40}, business_calendar=None, reason=REASON
            ),
            service.change(
                context, plant, integers={"sample_min": 6}, business_calendar=None, reason=REASON
            ),
            return_exceptions=True,
        )

    results = stack.run(both())
    assert [type(r).__name__ for r in results] == ["PlantLoopConfiguration"] * 2, results
    assert sorted(r.version for r in results) == [2, 3]
    versions = [record["content"]["version"] for record in stack.records(plant)]
    assert versions == [1, 2, 3]
    row = stack.row(plant)
    assert (row["version"], row["sample_max"], row["sample_min"]) == (3, 40, 6)


# --- alcance -------------------------------------------------------------------------------------


def test_out_of_scope_answers_like_missing_and_writes_nothing(
    stack: LoopConfigStack, site: Site, admin: Who
) -> None:
    plant, other_plant = _plant(site), _plant(site, 1)
    foreign_site = stack.site()
    foreign = _plant(foreign_site)
    body = {"sample_max": 31, "reason": REASON}
    for method, payload in (("GET", None), ("PUT", body)):
        known = stack.send(admin, method, f"/loop/plants/{foreign}/configuration", payload)
        missing = stack.send(admin, method, f"/loop/plants/{uuid.uuid4()}/configuration", payload)
        assert _comparable(known) == _comparable(missing)
        assert _error(known)[:2] == (404, "not_found")
    plant_admin = stack.member(site, Role.ADMINISTRATOR, ScopeLevel.PLANT, plant)
    assert stack.get(plant_admin, plant).status_code == 200
    assert stack.put(plant_admin, plant, body).status_code == 200
    for method, payload in (("GET", None), ("PUT", body)):
        outside = stack.send(
            plant_admin, method, f"/loop/plants/{other_plant}/configuration", payload
        )
        assert _error(outside)[:2] == (404, "not_found")
    zone = site.plants[plant][0]
    zone_admin = stack.member(site, Role.ADMINISTRATOR, ScopeLevel.ZONE, zone)
    assert _error(stack.get(zone_admin, plant))[:2] == (404, "not_found")
    assert _error(stack.put(zone_admin, plant, body))[:2] == (404, "not_found")
    # hierarchy.read lo tienen todos los roles; organization.settings, gerente y administrador.
    coordinator = stack.member(site, Role.COORDINATOR_SST)
    assert stack.get(coordinator, plant).status_code == 200
    assert _error(stack.put(coordinator, plant, body))[:2] == (404, "not_found")
    manager = stack.member(site, Role.PLANT_MANAGER, ScopeLevel.PLANT, plant)
    assert stack.put(manager, plant, {"sample_max": 32, "reason": REASON}).status_code == 200
    assert stack.row(foreign) is None and stack.records(foreign) == []
    assert stack.row(other_plant) is None


@pytest.mark.parametrize("level", [ScopeLevel.ORGANIZATION, ScopeLevel.PLANT])
def test_under_a_provider_concession_both_routes_answer_not_found(
    stack: LoopConfigStack, site: Site, level: ScopeLevel
) -> None:
    plant = _plant(site)
    installer = stack.installer(site, level, plant if level is ScopeLevel.PLANT else None)
    body = {"sample_max": 31, "reason": REASON}
    for method, payload in (("GET", None), ("PUT", body)):
        own = stack.send(installer, method, f"/loop/plants/{plant}/configuration", payload)
        missing = stack.send(
            installer, method, f"/loop/plants/{uuid.uuid4()}/configuration", payload
        )
        assert _error(own)[:2] == (404, "not_found"), own.text
        assert _comparable(own) == _comparable(missing)
    denied = stack.fetch(
        "SELECT operation FROM shared.audit_entry WHERE organization_id = $1"
        " AND operation = 'authorization_denied' AND actor_concession_id = $2",
        site.organization_id,
        installer[1],
    )
    assert len(denied) >= 2
    assert stack.row(plant) is None and stack.records(plant) == []
