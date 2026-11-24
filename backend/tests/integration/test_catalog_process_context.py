"""``PUT /zones/{zone_id}/process-context`` contra PostgreSQL 16 real (TASK-594; A-129).

La aplicación real (cadena fija de middleware, ``ContextAuthorizer``) con
``CatalogPublicationService`` y el ``RegressionService`` real como marcador, sobre la base migrada
como ``vigia_app`` (``tests.catalog_routes_support``):

- **Permiso y motivo**: sin ``catalog.manage`` se deniega igual que una zona inexistente
  (``not_found``; BR-NUC-09: nunca ``forbidden`` por la clave); sin motivo o con menos de 10
  caracteres, ``400``; con los dos, ``200`` y la versión ``n + 1`` firmada con
  ``process_context``. ``variables: []`` retira el bloque; cualquier otro cambio lo conserva.
- **Reglas** (regla 7 del catálogo): una variable de estado sobre una señal no declarada, un
  ``code`` repetido o una escala plana responden ``400`` y no escriben nada; tampoco se puede
  retirar una señal que una variable cita.
- **Concurrencia**: dos ``PUT`` simultáneos dejan dos versiones consecutivas.
- **Regresión (Q7)**: cambiar solo las variables no deja la regresión pendiente ni la marca; cambiar
  las señales sí, como siempre.
- **Rastro**: ``catalog_version_published`` con el usuario, el rol, el motivo y la hora, y el evento
  ``catalog_updated`` con ``changed_fields = [process_context]``.

Datos sintéticos con las escalas de Pope (A-129): velocidad 4 mA = 0 y 20 mA = 2000 m/min en el
canal 0, metros en bobina 4 mA = 0 y 20 mA = 50 000 m en el canal 1 y «jumbo en posición» sobre
una señal declarada.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from typing import Any, Final

import httpx
import pytest

from tests.authz_support import Site
from tests.catalog_routes_support import (
    REASON,
    CatalogRoutes,
    catalog_routes_world,
)
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.shared.context import Role, ScopeLevel

pytestmark = pytest.mark.integration

GUARD_SIGNAL: Final = str(uuid.UUID(int=40_001, version=4))
"""``SG-2`` de ``first_standard_body`` (rol ``guard``): la señal de «jumbo en posición»."""
SPEED: Final = {
    "variable_id": str(uuid.UUID(int=50_000, version=4)),
    "code": "VEL-MAQ",
    "kind": "analog",
    "description_es": "Velocidad de la máquina",
    "source": {"reader": "waveshare", "channel": 0},
    "unit": "m_per_min",
    "scale": {"at_4_ma": 0.0, "at_20_ma": 2000.0},
    "decimals": 0,
}
METERS: Final = {
    "variable_id": str(uuid.UUID(int=50_001, version=4)),
    "code": "MET-JUMBO",
    "kind": "analog",
    "description_es": "Metros en bobina",
    "source": {"reader": "waveshare", "channel": 1},
    "unit": "m",
    "scale": {"at_4_ma": 0.0, "at_20_ma": 50_000.0},
    "decimals": 0,
}
JUMBO: Final = {
    "variable_id": str(uuid.UUID(int=50_002, version=4)),
    "code": "JUMBO-POS",
    "kind": "binary",
    "description_es": "Jumbo en posición",
    "signal_id": GUARD_SIGNAL,
    "states": "in_position_out",
}
POPE: Final = {"before_ms": 30_000, "variables": [SPEED, METERS, JUMBO]}


@pytest.fixture(scope="module")
def routes(postgres_endpoint: PostgresEndpoint) -> Iterator[CatalogRoutes]:
    with catalog_routes_world(postgres_endpoint, "catalog_process_context") as world:
        yield world


def _code(response: httpx.Response) -> tuple[int, str | None, str | None]:
    body = response.json()
    return response.status_code, body.get("code"), body.get("detail_code")


def _configured(routes: CatalogRoutes) -> tuple[Site, uuid.UUID, Any]:
    """Una zona productiva con su versión 1 publicada por la ruta."""
    site = routes.site()
    ((plant, zone),) = site.zones()
    routes.productive(site, plant, zone)
    admin = routes.member(site)
    routes.configure(admin, zone)
    return site, zone, admin


def _put(routes: CatalogRoutes, zone: uuid.UUID, cookie: Any, body: Any) -> httpx.Response:
    return routes.request("PUT", f"/zones/{zone}/process-context", cookie, body)


def _regression_state(routes: CatalogRoutes, cookie: Any, zone: uuid.UUID) -> str:
    response = routes.request("GET", f"/zones/{zone}/regression", cookie)
    assert response.status_code == 200, response.text
    state: str = response.json()["state"]
    return state


# --- Permiso, motivo y versión nueva -------------------------------------------------------------


def test_the_administrator_publishes_version_n_plus_1_with_the_process_context(
    routes: CatalogRoutes,
) -> None:
    _, zone, admin = _configured(routes)

    response = _put(routes, zone, admin, {**POPE, "reason_es": REASON})

    assert response.status_code == 200, response.text
    view = response.json()
    assert (view["catalog_version"], view["changed_fields"]) == (2, ["process_context"])
    assert view["catalog"]["process_context"] == POPE
    assert view["catalog"]["version"] == 2
    rows = routes.versions(zone)
    assert [r["catalog_version"] for r in rows] == [1, 2]
    envelope = json.loads(rows[1]["envelope"])
    assert envelope["payload"] == view["catalog"]
    assert envelope["key_id"] and envelope["signature"]
    assert rows[0]["superseded_at"] == rows[1]["issued_at"]


@pytest.mark.parametrize(
    ("role", "level"),
    [
        (Role.COORDINATOR_SST, ScopeLevel.ORGANIZATION),
        (Role.PLANT_MANAGER, ScopeLevel.ORGANIZATION),
    ],
)
def test_without_catalog_manage_it_is_denied_and_writes_nothing(
    routes: CatalogRoutes, role: Role, level: ScopeLevel
) -> None:
    site, zone, _ = _configured(routes)
    before = routes.written(zone)
    reader = routes.member(site, role, level)

    denied = _put(routes, zone, reader, {**POPE, "reason_es": REASON})
    unknown = _put(routes, uuid.uuid4(), reader, {**POPE, "reason_es": REASON})

    # Sin la clave, igual que inexistente: ``not_found``, nunca ``forbidden`` (BR-NUC-09).
    assert _code(denied) == (404, "not_found", None)
    assert _code(unknown) == _code(denied)
    assert routes.written(zone) == before


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (None, (400, "invalid_request", None)),
        ("corto", (400, "invalid_request", "catalog_free_text_rejected")),
        ("123456789", (400, "invalid_request", "catalog_free_text_rejected")),
    ],
)
def test_without_a_reason_of_10_characters_nothing_is_written(
    routes: CatalogRoutes, reason: str | None, expected: tuple[int, str, str | None]
) -> None:
    _, zone, admin = _configured(routes)
    before = routes.written(zone)
    body: dict[str, Any] = dict(POPE)
    if reason is not None:
        body["reason_es"] = reason

    assert _code(_put(routes, zone, admin, body)) == expected
    assert routes.written(zone) == before


def test_another_organization_or_an_unknown_zone_answer_the_same(routes: CatalogRoutes) -> None:
    _, zone, admin = _configured(routes)
    other = routes.site()
    ((_, foreign),) = other.zones()
    before = routes.written(foreign)

    unknown = _put(routes, uuid.uuid4(), admin, {**POPE, "reason_es": REASON})
    outside = _put(routes, foreign, admin, {**POPE, "reason_es": REASON})

    assert _code(unknown) == (404, "not_found", None)
    assert _code(outside) == _code(unknown)
    assert routes.written(foreign) == before
    assert len(routes.versions(zone)) == 1


def test_a_zone_without_catalog_is_named(routes: CatalogRoutes) -> None:
    site = routes.site()
    ((_, zone),) = site.zones()
    admin = routes.member(site)

    response = _put(routes, zone, admin, {**POPE, "reason_es": REASON})

    assert _code(response) == (400, "invalid_request", "catalog_zone_without_cameras")
    assert routes.versions(zone) == []


# --- Reglas del contrato (regla 7) ---------------------------------------------------------------


def _flat(variable: dict[str, Any]) -> dict[str, Any]:
    return {**variable, "scale": {"at_4_ma": 5.0, "at_20_ma": 5.0}}


RULE_VIOLATIONS: Final = [
    pytest.param([SPEED, {**JUMBO, "signal_id": str(uuid.uuid4())}], id="senal-no-declarada"),
    pytest.param([SPEED, {**METERS, "code": SPEED["code"]}], id="codigo-repetido"),
    pytest.param(
        [SPEED, {**METERS, "variable_id": SPEED["variable_id"]}], id="identificador-repetido"
    ),
    pytest.param([_flat(SPEED)], id="escala-plana"),
    pytest.param([{**SPEED, "signal_id": GUARD_SIGNAL}], id="analogica-con-senal"),
    pytest.param([{k: v for k, v in JUMBO.items() if k != "states"}], id="estado-sin-par"),
    pytest.param([{k: v for k, v in SPEED.items() if k != "scale"}], id="analogica-sin-escala"),
    pytest.param([{**SPEED, "decimals": 4}], id="decimales"),
    pytest.param(
        [
            {**SPEED, "variable_id": str(uuid.UUID(int=60_000 + n, version=4)), "code": f"V-{n}"}
            for n in range(13)
        ],
        id="trece-variables",
    ),
]


@pytest.mark.parametrize("variables", RULE_VIOLATIONS)
def test_a_rule_violation_is_invalid_and_writes_nothing(
    routes: CatalogRoutes, variables: list[dict[str, Any]]
) -> None:
    _, zone, admin = _configured(routes)
    before = routes.written(zone)

    response = _put(
        routes, zone, admin, {"before_ms": 30_000, "variables": variables, "reason_es": REASON}
    )

    assert _code(response) == (400, "invalid_request", None)
    assert routes.written(zone) == before


@pytest.mark.parametrize("before_ms", [-1, 120_001, 1.5])
def test_the_window_before_is_0_to_120_seconds(routes: CatalogRoutes, before_ms: Any) -> None:
    _, zone, admin = _configured(routes)
    before = routes.written(zone)
    body = {**POPE, "before_ms": before_ms, "reason_es": REASON}

    assert _code(_put(routes, zone, admin, body)) == (400, "invalid_request", None)
    assert routes.written(zone) == before
    edge = _put(routes, zone, admin, {**POPE, "before_ms": 120_000, "reason_es": REASON})
    assert edge.status_code == 200, edge.text


def test_a_description_that_fails_the_free_text_policy_is_named(routes: CatalogRoutes) -> None:
    _, zone, admin = _configured(routes)
    before = routes.written(zone)
    marked = {**SPEED, "description_es": "<b>Velocidad</b>"}

    response = _put(
        routes, zone, admin, {"before_ms": 0, "variables": [marked], "reason_es": REASON}
    )

    assert _code(response) == (400, "invalid_request", "catalog_free_text_rejected")
    assert routes.written(zone) == before


def test_an_empty_list_withdraws_the_block_and_other_changes_keep_it(
    routes: CatalogRoutes,
) -> None:
    _, zone, admin = _configured(routes)
    assert _put(routes, zone, admin, {**POPE, "reason_es": REASON}).status_code == 200

    thresholds = routes.request(
        "PUT",
        f"/zones/{zone}/thresholds",
        admin,
        {"review": 0.35, "publication": 0.85, "reason_es": REASON},
    )
    assert thresholds.status_code == 200, thresholds.text
    assert thresholds.json()["catalog"]["process_context"] == POPE

    # La señal que cita «jumbo en posición» no se puede retirar mientras la variable la cite.
    before = routes.written(zone)
    orphan = routes.request(
        "PUT", f"/zones/{zone}/signals", admin, {"signals": [], "reason_es": REASON}
    )
    assert _code(orphan) == (400, "invalid_request", None)
    assert routes.written(zone) == before

    withdrawn = _put(routes, zone, admin, {"before_ms": 0, "variables": [], "reason_es": REASON})
    assert withdrawn.status_code == 200, withdrawn.text
    view = withdrawn.json()
    assert (view["catalog_version"], view["changed_fields"]) == (4, ["process_context"])
    assert "process_context" not in view["catalog"]


# --- Concurrencia --------------------------------------------------------------------------------


def test_two_simultaneous_puts_leave_two_consecutive_versions(routes: CatalogRoutes) -> None:
    _, zone, admin = _configured(routes)
    routes.tick()
    headers = routes.headers(admin)
    path = f"/zones/{zone}/process-context"
    bodies = [
        {"before_ms": 30_000, "variables": [SPEED], "reason_es": REASON},
        {"before_ms": 10_000, "variables": [METERS], "reason_es": REASON},
    ]

    async def race() -> list[httpx.Response]:
        return list(
            await asyncio.gather(
                *(routes.client.put(path, json=body, headers=headers) for body in bodies)
            )
        )

    responses = routes.run(race())

    assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
    assert sorted(r.json()["catalog_version"] for r in responses) == [2, 3]
    rows = routes.versions(zone)
    assert [r["catalog_version"] for r in rows] == [1, 2, 3]
    assert rows[1]["superseded_at"] == rows[2]["issued_at"]
    published = routes.records(zone, "catalog_version_published")
    sources = [r["content"]["source_key"] for r in published]
    assert sources == [f"{zone}:1", f"{zone}:2", f"{zone}:3"]


# --- Regresión (Q7) y rastro ---------------------------------------------------------------------


def test_only_the_variables_never_leave_the_regression_pending_and_signals_do(
    routes: CatalogRoutes,
) -> None:
    _, zone, admin = _configured(routes)
    assert _regression_state(routes, admin, zone) == "current"

    assert _put(routes, zone, admin, {**POPE, "reason_es": REASON}).status_code == 200

    assert _regression_state(routes, admin, zone) == "current"
    row = routes.regression_row(zone)
    assert row is None or row["state"] == "current"
    assert routes.records(zone, "walk_test_regression_marked") == []
    assert routes.events(zone, "regression_marked") == []

    first = routes.request("GET", f"/zones/{zone}/catalog", admin).json()
    signals = routes.request(
        "PUT",
        f"/zones/{zone}/signals",
        admin,
        {"signals": first["catalog"]["signals"], "reason_es": REASON},
    )
    assert signals.status_code == 200, signals.text
    assert _regression_state(routes, admin, zone) == "pending"
    (mark,) = [r["content"] for r in routes.records(zone, "walk_test_regression_marked")]
    assert (mark["catalog_version"], mark["affected_row_ids"]) == (3, "all")

    # Con la regresión pendiente, cambiar las variables no la toca ni deja otra marca.
    again = _put(routes, zone, admin, {"before_ms": 0, "variables": [SPEED], "reason_es": REASON})
    assert again.status_code == 200, again.text
    pending = routes.request("GET", f"/zones/{zone}/regression", admin).json()
    assert (pending["state"], pending["catalog_version"]) == ("pending", 3)
    assert len(routes.records(zone, "walk_test_regression_marked")) == 1


def test_the_new_version_is_in_the_chain_with_the_user_the_reason_and_the_time(
    routes: CatalogRoutes,
) -> None:
    site, zone, _ = _configured(routes)
    user_id, admin = routes.person(site)

    response = _put(routes, zone, admin, {**POPE, "reason_es": REASON})

    assert response.status_code == 200, response.text
    view = response.json()
    assert (view["issued_by"], view["role_in_use"]) == (str(user_id), "administrator")
    (record,) = routes.records(zone, "catalog_version_published")[1:]
    assert record["actor_id"] == user_id
    assert record["actor_role_in_use"] == "administrator"
    content = record["content"]
    assert content["source_key"] == f"{zone}:2"
    assert content["changed_fields"] == ["process_context"]
    assert content["reason_es"] == REASON
    assert content["envelope"]["payload"]["process_context"] == POPE
    (row,) = routes.versions(zone)[1:]
    assert (row["issued_by"], row["reason_es"]) == (user_id, REASON)
    assert row["issued_at"] == record["occurred_at"]
    assert view["issued_at"].endswith("Z")
    updated = [json.loads(e["payload"]) for e in routes.events(zone, "catalog_updated")]
    assert updated[-1] == {
        "zone_id": str(zone),
        "catalog_version": 2,
        "changed_fields": ["process_context"],
    }
