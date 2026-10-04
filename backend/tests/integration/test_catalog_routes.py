"""Rutas del catálogo de SCR-04 y marca de regresión contra PostgreSQL 16 real (TASK-209).

La aplicación real (cadena fija de middleware, ``ContextAuthorizer``) con
``CatalogPublicationService`` y el ``RegressionService`` real como marcador, sobre la base migrada
como ``vigia_app`` (``tests.catalog_routes_support``):

- **Rutas** de lectura (``catalog.read``), de estándares y parámetros (``catalog.manage``) y de
  recaptura del encuadre (``commissioning.run``): estado, cuerpo, ``changed_fields``, registro y
  evento de cada una; la primera versión de la zona y sus errores con ``detail_code``.
- **Marca calculada por el sistema** (BR-GOB-51, 52, 56): la tabla de filas afectadas de TASK-209
  con la matriz derivada del catálogo; unión, ``all`` que absorbe, ``marked_at`` del primer
  instante y arrastre de las filas a la versión nueva del estándar. La zona sigue
  ``productive`` (BR-GOB-53).
- **G-10 / BR-GOB-56**: sin ``reason_es`` o con un campo de más, ``invalid_request`` y nada
  escrito.
- **Concurrencia**: una publicación y un cambio de ``model_version`` simultáneos, y dos
  publicaciones, dejan versiones consecutivas, la unión de filas y un registro por marca. La
  primera marca se retiene tras leer la fila; la segunda espera el candado de la regresión (se
  comprueba en ``pg_locks``, sin topes de pared) o llega también a leerla (sin el candado: la
  prueba falla).
- **Guardas de alcance**: otra organización, otra planta o una zona fuera de la sesión responden
  igual que una zona inexistente.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, Final

import httpx
import pytest
from vigia_contracts.models.enumerations import PredicateFamily

from tests.authz_support import Site
from tests.catalog_routes_support import (
    ENERGY_ON,
    GUARD_ON,
    PRESENCE,
    REASON,
    CatalogRoutes,
    camera,
    catalog_routes_world,
    first_standard_body,
    standard_body,
)
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.catalog.adapters.postgres.regression_repository import (
    PostgresRegressionRepository,
)
from vigia_platform.catalog.application.regression import (
    RegressionRequestInvalid,
    requires_model_regression,
)
from vigia_platform.catalog.domain.catalog_version import (
    NewStandard,
    SetThresholds,
    StandardDraft,
    ZoneCatalogVersion,
)
from vigia_platform.catalog.domain.matrix import POSTURES, derive_matrix
from vigia_platform.catalog.domain.regression import WalkTestRegression
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.db import Database, Transaction
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration

GUARD_BYPASS: Final = {"all_of": [PRESENCE, GUARD_ON], "min_duration_ms": 0}
WAIT_SECONDS: Final = 30.0
"""Tope generoso de las esperas por evento (la decisión la da el estado, no el tope)."""
POLL_SECONDS: Final = 0.05
HOLD_SECONDS: Final = 60.0


@pytest.fixture(scope="module")
def routes(postgres_endpoint: PostgresEndpoint) -> Iterator[CatalogRoutes]:
    with catalog_routes_world(postgres_endpoint, "catalog_routes") as world:
        yield world


def _code(response: httpx.Response) -> tuple[int, str | None, str | None]:
    body = response.json()
    return response.status_code, body.get("code"), body.get("detail_code")


def _comparable(response: httpx.Response) -> tuple[int, Any]:
    body = response.json()
    return response.status_code, {k: v for k, v in body.items() if k != "correlation_id"}


def _regression(routes: CatalogRoutes, cookie: Any, zone: uuid.UUID) -> dict[str, Any]:
    response = routes.request("GET", f"/zones/{zone}/regression", cookie)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _standard_rows(catalog: dict[str, Any], standard_id: str) -> list[str]:
    return sorted(
        str(row.row_id) for row in derive_matrix(catalog) if str(row.standard_id) == standard_id
    )


def _new_standard_id(before: dict[str, Any], after: dict[str, Any]) -> str:
    (standard_id,) = {s["standard_id"] for s in after["standards"]} - {
        s["standard_id"] for s in before["standards"]
    }
    return str(standard_id)


def _configured(routes: CatalogRoutes) -> tuple[Site, uuid.UUID, uuid.UUID, Any, dict[str, Any]]:
    """Una zona productiva con su versión 1 publicada por la ruta."""
    site = routes.site()
    ((plant, zone),) = site.zones()
    routes.productive(site, plant, zone)
    admin = routes.member(site)
    first = routes.configure(admin, zone)
    return site, plant, zone, admin, first


def _dwell(min_duration_ms: int) -> dict[str, Any]:
    """Predicado de la plantilla ``dwell``: presencia con energía, sostenida."""
    return {"all_of": [PRESENCE, ENERGY_ON], "min_duration_ms": min_duration_ms}


def _marks(routes: CatalogRoutes, zone: uuid.UUID) -> list[dict[str, Any]]:
    return [r["content"] for r in routes.records(zone, "walk_test_regression_marked")]


# --- Primera versión y estado de una zona nueva --------------------------------------------------


def test_a_new_zone_is_current_and_its_first_version_needs_its_parameters(
    routes: CatalogRoutes,
) -> None:
    site = routes.site()
    ((_, zone),) = site.zones()
    admin = routes.member(site)

    fresh = _regression(routes, admin, zone)
    assert fresh == {
        "zone_id": str(zone),
        "state": "current",
        "marked_at": None,
        "cause": None,
        "catalog_version": None,
        "model_version": None,
        "affected_row_ids": None,
    }
    missing = routes.request("GET", f"/zones/{zone}/catalog", admin)
    assert _code(missing) == (404, "not_found", None)
    history = routes.request("GET", f"/zones/{zone}/catalog/versions", admin)
    assert history.status_code == 200 and history.json() == {"versions": [], "next_after": None}
    # Sin catálogo y sin parámetros: la zona no tiene cámaras declaradas.
    bare = routes.request("POST", f"/zones/{zone}/standards", admin, standard_body())
    assert _code(bare) == (409, "conflict", "catalog_zone_without_cameras")
    assert routes.written(zone) == (0, 0, 0, 0)
    # Los parámetros de la zona sin catálogo tampoco se cambian por su ruta.
    early = routes.request(
        "PUT",
        f"/zones/{zone}/thresholds",
        admin,
        {"review": 0.3, "publication": 0.9, "reason_es": REASON},
    )
    assert _code(early) == (409, "conflict", "catalog_zone_without_cameras")

    created = routes.request("POST", f"/zones/{zone}/standards", admin, first_standard_body())

    assert created.status_code == 201, created.text
    view = created.json()
    assert view["catalog_version"] == 1 and view["reason_es"] == REASON
    assert set(view["changed_fields"]) == {
        "standards",
        "cameras",
        "minimum_coverage",
        "signals",
        "thresholds",
        "clip_window",
        "episode",
        "single_occupancy",
    }
    assert view["catalog"]["episode"]["grouping_window_ms"] == 3000
    # ``stream_reference`` nunca entra en el catálogo firmado (U03-H-03).
    assert "stream_reference" not in json.dumps(view["catalog"])
    # La versión 1 no marca: aún no hay acta que deje de regir.
    assert _regression(routes, admin, zone)["state"] == "current"
    assert _marks(routes, zone) == []
    current = routes.request("GET", f"/zones/{zone}/catalog", admin).json()
    assert current == view
    # Con catálogo, los parámetros solo cambian por sus rutas.
    again = routes.request("POST", f"/zones/{zone}/standards", admin, first_standard_body())
    assert _code(again) == (400, "invalid_request", None)
    assert len(routes.versions(zone)) == 1


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"family": "not_a_family"}, (400, "invalid_request", None)),
        ({"predicate": {"all_of": [PRESENCE], "min_duration_ms": 0}}, None),
        ({"title_es": "Sabotaje en la celda"}, None),
    ],
)
def test_first_standard_errors_carry_their_detail_code_and_write_nothing(
    routes: CatalogRoutes, changes: dict[str, Any], expected: tuple[int, str, str | None] | None
) -> None:
    site = routes.site()
    ((_, zone),) = site.zones()
    admin = routes.member(site)
    body = {**first_standard_body(), **changes}

    response = routes.request("POST", f"/zones/{zone}/standards", admin, body)

    if expected is None:
        detail = (
            "catalog_predicate_invalid" if "predicate" in changes else "catalog_free_text_rejected"
        )
        expected = (400, "invalid_request", detail)
    assert _code(response) == expected
    assert routes.written(zone) == (0, 0, 0, 0)


def test_unsatisfiable_coverage_and_family_not_admitted_are_named(
    routes: CatalogRoutes,
) -> None:
    site = routes.authz.add_site(plants=1, zones_per_plant=1)  # sin familias admitidas
    ((_, zone),) = site.zones()
    admin = routes.member(site)
    not_admitted = routes.request("POST", f"/zones/{zone}/standards", admin, first_standard_body())
    assert _code(not_admitted) == (409, "conflict", "catalog_family_not_admitted")

    site = routes.site()
    ((_, zone),) = site.zones()
    admin = routes.member(site)
    unsatisfiable = first_standard_body(
        minimum_coverage={"required_count": 3, "required_camera_ids": []}
    )
    response = routes.request("POST", f"/zones/{zone}/standards", admin, unsatisfiable)
    assert _code(response) == (400, "invalid_request", "catalog_unsatisfiable_coverage")
    assert routes.written(zone) == (0, 0, 0, 0)


# --- Las cinco rutas de parámetros ---------------------------------------------------------------


PARAMETER_CHANGES: Final[list[tuple[str, dict[str, Any], list[str]]]] = [
    ("cameras", {"cameras": [camera(0), camera(1), camera(2)]}, ["cameras"]),
    (
        "minimum-coverage",
        {"required_count": 2, "required_camera_ids": [camera(0)["camera_id"]]},
        ["minimum_coverage"],
    ),
    ("signals", {"signals": []}, ["signals"]),
    ("thresholds", {"review": 0.35, "publication": 0.85}, ["thresholds"]),
    ("windows", {"clip_window": {"pre_seconds": 15, "post_seconds": 20}}, ["clip_window"]),
    ("windows", {"episode": {}}, ["episode"]),
    (
        "windows",
        {"clip_window": {"pre_seconds": 5}, "episode": {"grouping_window_ms": 5000}},
        ["clip_window", "episode"],
    ),
]


@pytest.mark.parametrize(("route", "body", "fields"), PARAMETER_CHANGES)
def test_each_parameter_route_publishes_a_signed_version_and_marks_the_full_matrix(
    routes: CatalogRoutes, route: str, body: dict[str, Any], fields: list[str]
) -> None:
    _, _, zone, admin, first = _configured(routes)

    response = routes.request("PUT", f"/zones/{zone}/{route}", admin, {**body, "reason_es": REASON})

    assert response.status_code == 200, response.text
    view = response.json()
    assert view["catalog_version"] == 2 and view["changed_fields"] == fields
    assert view["catalog"]["version"] == 2
    if route == "windows" and "episode" in body and body["episode"] == {}:
        # D-11: 3 000 ms por defecto.
        assert view["catalog"]["episode"]["grouping_window_ms"] == 3000
    rows = routes.versions(zone)
    assert [r["catalog_version"] for r in rows] == [1, 2]
    assert json.loads(rows[1]["envelope"])["payload"] == view["catalog"]
    regression = _regression(routes, admin, zone)
    assert regression["state"] == "pending" and regression["cause"] == "catalog_change"
    assert regression["catalog_version"] == 2 and regression["affected_row_ids"] == "all"
    assert regression["marked_at"] == view["issued_at"]
    (mark,) = _marks(routes, zone)
    assert mark == {
        "zone_id": str(zone),
        "cause": "catalog_change",
        "catalog_version": 2,
        "affected_row_ids": "all",
        "marked_at": view["issued_at"],
    }
    (event,) = routes.events(zone, "regression_marked")
    assert json.loads(event["payload"]) == {
        "zone_id": str(zone),
        "cause": "catalog_change",
        "catalog_version": 2,
        "model_version": None,
        "affected_row_ids": "all",
    }
    # BR-GOB-53: la marca no toca las compuertas.
    assert routes.resulting_mode(zone) == "productive"
    assert first["catalog_version"] == 1


def test_a_camera_may_be_declared_in_two_zones_and_its_limits_are_checked(
    routes: CatalogRoutes,
) -> None:
    site = routes.site(zones=2)
    (_, zone_a), (_, zone_b) = site.zones()
    admin = routes.member(site)
    routes.configure(admin, zone_a)
    routes.configure(admin, zone_b)
    shared = camera(7, "X")

    for zone in (zone_a, zone_b):
        response = routes.request(
            "PUT",
            f"/zones/{zone}/cameras",
            admin,
            {"cameras": [camera(0), shared], "reason_es": REASON},
        )
        assert response.status_code == 200, response.text
    assert (
        routes.fetch(
            "SELECT count(*) AS n FROM catalog.zone_camera WHERE camera_id = $1",
            uuid.UUID(shared["camera_id"]),
        )[0]["n"]
        == 2
    )

    for bad in (
        {**camera(3), "declared_min_fps": 0.5},  # BR-GOB-12: de 1 a 60
        {**camera(3), "code": "minúsculas"},
        {**camera(3), "stream_reference": "Con Espacios"},
    ):
        response = routes.request(
            "PUT",
            f"/zones/{zone_a}/cameras",
            admin,
            {"cameras": [camera(0), bad], "reason_es": REASON},
        )
        assert _code(response) == (400, "invalid_request", None)
    duplicated = routes.request(
        "PUT",
        f"/zones/{zone_a}/cameras",
        admin,
        {"cameras": [camera(0), camera(0)], "reason_es": REASON},
    )
    assert _code(duplicated) == (400, "invalid_request", None)
    # Quitar una cámara requerida deja la cobertura insatisfacible (BR-GOB-11).
    without_required = routes.request(
        "PUT", f"/zones/{zone_a}/cameras", admin, {"cameras": [camera(1)], "reason_es": REASON}
    )
    assert _code(without_required) == (400, "invalid_request", "catalog_unsatisfiable_coverage")
    assert [r["catalog_version"] for r in routes.versions(zone_a)] == [1, 2]


@pytest.mark.parametrize(
    "body",
    [
        {"review": 0.8, "publication": 0.8},
        {"review": 0.9, "publication": 0.5},
        {"review": 0.0, "publication": 0.5},
        {"review": 0.5, "publication": 1.5},
        {"review": "0.4", "publication": 0.8},
    ],
)
def test_incoherent_thresholds_are_invalid_and_write_nothing(
    routes: CatalogRoutes, body: dict[str, Any]
) -> None:
    _, _, zone, admin, _ = _configured(routes)
    before = routes.written(zone)

    response = routes.request(
        "PUT", f"/zones/{zone}/thresholds", admin, {**body, "reason_es": REASON}
    )

    assert _code(response) == (400, "invalid_request", None)
    assert routes.written(zone) == before


def test_threshold_edges_publish_and_windows_need_one_part(routes: CatalogRoutes) -> None:
    _, _, zone, admin, _ = _configured(routes)
    edge = routes.request(
        "PUT",
        f"/zones/{zone}/thresholds",
        admin,
        {"review": 0.999, "publication": 1.0, "reason_es": REASON},
    )
    assert edge.status_code == 200, edge.text
    assert edge.json()["catalog"]["thresholds"] == {"review": 0.999, "publication": 1.0}
    empty = routes.request("PUT", f"/zones/{zone}/windows", admin, {"reason_es": REASON})
    assert _code(empty) == (400, "invalid_request", None)
    too_short = routes.request(
        "PUT",
        f"/zones/{zone}/windows",
        admin,
        {"clip_window": {"pre_seconds": 4}, "reason_es": REASON},
    )
    assert _code(too_short) == (400, "invalid_request", None)
    signals = routes.request(
        "PUT",
        f"/zones/{zone}/signals",
        admin,
        {"signals": [{"signal_id": str(uuid.uuid4()), "code": "SG-9"}], "reason_es": REASON},
    )
    assert _code(signals) == (400, "invalid_request", None)
    assert [r["catalog_version"] for r in routes.versions(zone)] == [1, 2]


# --- G-10 y BR-GOB-56: sin motivo, sin campos que fijen la marca ---------------------------------


WRITE_BODIES: Final[list[tuple[str, str, dict[str, Any]]]] = [
    ("PUT", "thresholds", {"review": 0.3, "publication": 0.9}),
    ("PUT", "cameras", {"cameras": [camera(0), camera(1)]}),
    ("PUT", "minimum-coverage", {"required_count": 1, "required_camera_ids": []}),
    ("PUT", "signals", {"signals": []}),
    ("PUT", "windows", {"episode": {}}),
    ("PUT", "catalog/single-occupancy", {"single_occupancy": True}),
    ("POST", "standards", standard_body(family="guard_bypass", predicate=GUARD_BYPASS)),
]


@pytest.mark.parametrize(("method", "route", "body"), WRITE_BODIES)
@pytest.mark.parametrize(
    "tamper",
    [
        "without_reason",
        "regression_marked",
        "skip_regression",
        "affected_row_ids",
        "changed_fields",
    ],
)
def test_g10_no_body_writes_without_reason_or_with_a_field_that_fixes_the_mark(
    routes: CatalogRoutes, method: str, route: str, body: dict[str, Any], tamper: str
) -> None:
    _, _, zone, admin, _ = _configured(routes)
    before = routes.written(zone)
    payload = {**body, "reason_es": REASON}
    if tamper == "without_reason":
        payload.pop("reason_es")
    elif tamper == "skip_regression":
        payload["skip_regression"] = True
    elif tamper == "regression_marked":
        payload["regression_marked"] = False
    elif tamper == "affected_row_ids":
        payload["affected_row_ids"] = []
    else:
        payload["changed_fields"] = ["single_occupancy"]

    response = routes.request(method, f"/zones/{zone}/{route}", admin, payload)

    assert _code(response) == (400, "invalid_request", None)
    assert routes.written(zone) == before
    assert _regression(routes, admin, zone)["state"] == "current"


def test_a_reason_that_fails_the_policy_is_named_and_writes_nothing(
    routes: CatalogRoutes,
) -> None:
    _, _, zone, admin, _ = _configured(routes)
    before = routes.written(zone)
    for reason in ("corto", "x" * 501, "Manipulación deliberada del resguardo"):
        response = routes.request(
            "PUT",
            f"/zones/{zone}/thresholds",
            admin,
            {"review": 0.3, "publication": 0.9, "reason_es": reason},
        )
        assert _code(response) == (400, "invalid_request", "catalog_free_text_rejected")
    assert routes.written(zone) == before


# --- Estándares: filas afectadas, unión y arrastre -----------------------------------------------


def test_a_new_standard_marks_exactly_its_rows_and_marks_union_with_all_absorbing(
    routes: CatalogRoutes,
) -> None:
    _, _, zone, admin, first = _configured(routes)

    second = routes.request(
        "POST",
        f"/zones/{zone}/standards",
        admin,
        standard_body("guard_bypass", GUARD_BYPASS, "Resguardo cerrado con persona dentro"),
    )
    assert second.status_code == 201, second.text
    catalog_2 = second.json()["catalog"]
    new_id = _new_standard_id(first["catalog"], catalog_2)
    rows_2 = _standard_rows(catalog_2, new_id)
    assert len(rows_2) == len(POSTURES)
    regression = _regression(routes, admin, zone)
    assert regression["state"] == "pending" and regression["affected_row_ids"] == rows_2
    marked_at = regression["marked_at"]

    third = routes.request(
        "POST",
        f"/zones/{zone}/standards",
        admin,
        standard_body("dwell", _dwell(5000), "Permanencia"),
    )
    assert third.status_code == 201, third.text
    catalog_3 = third.json()["catalog"]
    third_id = _new_standard_id(catalog_2, catalog_3)
    union = sorted(rows_2 + _standard_rows(catalog_3, third_id))
    regression = _regression(routes, admin, zone)
    assert regression["affected_row_ids"] == union and len(union) == 8
    assert regression["marked_at"] == marked_at  # el primer instante del periodo
    assert regression["catalog_version"] == 3

    routes.request(
        "PUT",
        f"/zones/{zone}/thresholds",
        admin,
        {"review": 0.2, "publication": 0.7, "reason_es": REASON},
    )
    regression = _regression(routes, admin, zone)
    assert regression["affected_row_ids"] == "all" and regression["catalog_version"] == 4
    # Una fila de la matriz no la quita ninguna lista posterior: all absorbe.
    routes.request(
        "POST",
        f"/zones/{zone}/standards",
        admin,
        standard_body("startup_transition", _startup(), "Arranque desde reposo"),
    )
    regression = _regression(routes, admin, zone)
    assert regression["affected_row_ids"] == "all" and regression["marked_at"] == marked_at
    marks = _marks(routes, zone)
    assert [m["catalog_version"] for m in marks] == [2, 3, 4, 5]
    assert marks[0]["affected_row_ids"] == rows_2 and marks[2]["affected_row_ids"] == "all"
    assert len(routes.events(zone, "regression_marked")) == 4
    assert routes.resulting_mode(zone) == "productive"


def _startup() -> dict[str, Any]:
    return {
        "all_of": [PRESENCE, {"signal_role": "start_command", "value": "asserted"}],
        "min_duration_ms": 0,
    }


def test_a_text_only_version_does_not_mark_and_a_predicate_change_marks_its_rows(
    routes: CatalogRoutes,
) -> None:
    _, _, zone, admin, first = _configured(routes)
    (standard,) = first["catalog"]["standards"]
    path = f"/zones/{zone}/standards/{standard['standard_id']}/versions"

    for changes in (
        {"title_es": "Coexistencia revisada"},
        {"declared_text": "Nadie entra en la celda con la máquina energizada."},
        {
            "predicate": {
                "all_of": list(reversed(standard["predicate"]["all_of"])),
                "min_duration_ms": 0,
            }
        },
        {"title_es": standard["title_es"]},  # sin cambio efectivo: publica y no marca
    ):
        response = routes.request("POST", path, admin, {"changes": changes, "reason_es": REASON})
        assert response.status_code == 201, response.text
        assert response.json()["changed_fields"] == ["standards"]
    assert _regression(routes, admin, zone)["state"] == "current"
    assert _marks(routes, zone) == []
    assert [r["catalog_version"] for r in routes.versions(zone)] == [1, 2, 3, 4, 5]
    before = routes.written(zone)
    for changes in ({}, {"standard_id": str(uuid.uuid4())}, {"family": "dwell"}):
        refused = routes.request("POST", path, admin, {"changes": changes, "reason_es": REASON})
        assert _code(refused) == (400, "invalid_request", None)
    outside = routes.request(
        "POST",
        path,
        admin,
        {
            "changes": {"predicate": {"all_of": [GUARD_ON], "min_duration_ms": 0}},
            "reason_es": REASON,
        },
    )
    assert _code(outside) == (400, "invalid_request", "catalog_predicate_invalid")
    assert routes.written(zone) == before


def test_a_predicate_change_marks_the_rows_of_the_new_version_of_that_standard(
    routes: CatalogRoutes,
) -> None:
    site, _, zone, admin, first = _configured(routes)
    dwell = routes.request(
        "POST",
        f"/zones/{zone}/standards",
        admin,
        standard_body("dwell", _dwell(5000), "Permanencia"),
    ).json()
    dwell_id = _new_standard_id(first["catalog"], dwell["catalog"])
    # Se cierra la marca del alta para ver solo la del cambio (la limpieza real es de TASK-216).
    routes.authz.execute(
        "UPDATE catalog.walk_test_regression SET state = 'current' WHERE zone_id = $1", zone
    )
    coexistence = next(s for s in dwell["catalog"]["standards"] if s["standard_id"] != dwell_id)

    changed = routes.request(
        "POST",
        f"/zones/{zone}/standards/{dwell_id}/versions",
        admin,
        {
            "changes": {"predicate": _dwell(9000)},
            "reason_es": REASON,
        },
    )

    assert changed.status_code == 201, changed.text
    catalog = changed.json()["catalog"]
    regression = _regression(routes, admin, zone)
    rows = _standard_rows(catalog, dwell_id)
    assert regression["affected_row_ids"] == rows
    # Las filas del otro estándar no están: la marca es el subconjunto que cambió (BR-GOB-52).
    assert not set(rows) & set(_standard_rows(catalog, coexistence["standard_id"]))
    assert [s["version"] for s in catalog["standards"] if s["standard_id"] == dwell_id] == [2]
    assert site is not None


def test_pending_rows_follow_the_new_version_of_their_standard(routes: CatalogRoutes) -> None:
    _, _, zone, admin, first = _configured(routes)
    added = routes.request(
        "POST",
        f"/zones/{zone}/standards",
        admin,
        standard_body("guard_bypass", GUARD_BYPASS, "Resguardo cerrado con persona dentro"),
    ).json()
    new_id = _new_standard_id(first["catalog"], added["catalog"])
    record = _regression(routes, admin, zone)
    assert record["affected_row_ids"] == _standard_rows(added["catalog"], new_id)

    renamed = routes.request(
        "POST",
        f"/zones/{zone}/standards/{new_id}/versions",
        admin,
        {"changes": {"title_es": "Resguardo con persona"}, "reason_es": REASON},
    )

    assert renamed.status_code == 201, renamed.text
    regression = _regression(routes, admin, zone)
    # Mismas posturas del mismo estándar, ahora con los row_id de su versión 2.
    assert regression["affected_row_ids"] == _standard_rows(renamed.json()["catalog"], new_id)
    assert regression["affected_row_ids"] != record["affected_row_ids"]
    assert (regression["marked_at"], regression["cause"]) == (record["marked_at"], record["cause"])
    assert len(_marks(routes, zone)) == 1  # el arrastre no es una marca


# --- Retiro y marca unipersonal ------------------------------------------------------------------


def test_retirement_marks_all_and_the_last_standard_stays(routes: CatalogRoutes) -> None:
    _, _, zone, admin, first = _configured(routes)
    added = routes.request(
        "POST",
        f"/zones/{zone}/standards",
        admin,
        standard_body("guard_bypass", GUARD_BYPASS, "Resguardo"),
    ).json()
    new_id = _new_standard_id(first["catalog"], added["catalog"])
    now = routes.authz.now()
    path = f"/zones/{zone}/standards/{new_id}/retirement"

    for moment in (now - timedelta(hours=1), now + timedelta(hours=1)):
        late = routes.request(
            "POST", path, admin, {"reason_es": REASON, "effective_from": format_timestamp(moment)}
        )
        assert _code(late) == (400, "invalid_request", None)
    naive = routes.request(
        "POST", path, admin, {"reason_es": REASON, "effective_from": "2026-10-04T10:00:00"}
    )
    assert _code(naive) == (400, "invalid_request", None)
    assert len(routes.versions(zone)) == 2

    retired = routes.request(
        "POST",
        path,
        admin,
        {"reason_es": REASON, "effective_from": format_timestamp(routes.authz.now())},
    )

    assert retired.status_code == 201, retired.text
    view = retired.json()
    assert [s["standard_id"] for s in view["catalog"]["standards"]] == [
        first["catalog"]["standards"][0]["standard_id"]
    ]
    regression = _regression(routes, admin, zone)
    assert regression["affected_row_ids"] == "all" and regression["catalog_version"] == 3
    assert len(routes.records(zone, "catalog_standard_retired")) == 1
    last = first["catalog"]["standards"][0]["standard_id"]
    refused = routes.request(
        "POST",
        f"/zones/{zone}/standards/{last}/retirement",
        admin,
        {"reason_es": REASON, "effective_from": format_timestamp(routes.authz.now())},
    )
    assert _code(refused) == (409, "conflict", "catalog_last_standard_in_zone")
    unknown = routes.request(
        "POST",
        f"/zones/{zone}/standards/{uuid.uuid4()}/retirement",
        admin,
        {"reason_es": REASON, "effective_from": format_timestamp(routes.authz.now())},
    )
    assert _code(unknown) == (404, "not_found", None)
    assert len(routes.versions(zone)) == 3


def test_single_occupancy_publishes_without_marking(routes: CatalogRoutes) -> None:
    _, _, zone, admin, _ = _configured(routes)

    response = routes.request(
        "PUT",
        f"/zones/{zone}/catalog/single-occupancy",
        admin,
        {"single_occupancy": True, "aggregation_window_minutes": 480, "reason_es": REASON},
    )

    assert response.status_code == 200, response.text
    view = response.json()
    assert view["changed_fields"] == ["single_occupancy"]
    assert (view["single_occupancy"], view["aggregation_window_minutes"]) == (True, 480)
    assert "single_occupancy" not in json.dumps(view["catalog"])
    assert _regression(routes, admin, zone)["state"] == "current"
    for minutes in (14, 481):
        bad = routes.request(
            "PUT",
            f"/zones/{zone}/catalog/single-occupancy",
            admin,
            {"single_occupancy": True, "aggregation_window_minutes": minutes, "reason_es": REASON},
        )
        assert _code(bad) == (400, "invalid_request", None)
    defaulted = routes.request(
        "PUT",
        f"/zones/{zone}/catalog/single-occupancy",
        admin,
        {"single_occupancy": False, "reason_es": REASON},
    )
    assert defaulted.json()["aggregation_window_minutes"] == 60


# --- Historial y versiones -----------------------------------------------------------------------


def test_history_pages_from_the_newest_and_versions_keep_their_envelope(
    routes: CatalogRoutes,
) -> None:
    _, _, zone, admin, first = _configured(routes)
    for review in (0.1, 0.2, 0.3, 0.4):
        routes.request(
            "PUT",
            f"/zones/{zone}/thresholds",
            admin,
            {"review": review, "publication": 0.9, "reason_es": REASON},
        )

    page = routes.request(
        "GET", f"/zones/{zone}/catalog/versions", admin, params={"limit": "2"}
    ).json()
    assert [v["catalog_version"] for v in page["versions"]] == [5, 4]
    assert page["next_after"] == "4"
    following = routes.request(
        "GET",
        f"/zones/{zone}/catalog/versions",
        admin,
        params={"limit": "2", "after": page["next_after"]},
    ).json()
    assert [v["catalog_version"] for v in following["versions"]] == [3, 2]
    last = routes.request(
        "GET", f"/zones/{zone}/catalog/versions", admin, params={"after": "2"}
    ).json()
    assert [v["catalog_version"] for v in last["versions"]] == [1]
    assert last["next_after"] is None
    assert last["versions"][0]["superseded_at"] is not None
    for bad in ({"after": "0"}, {"after": "abc"}, {"limit": "201"}, {"limit": "0"}, {"x": "1"}):
        response = routes.request("GET", f"/zones/{zone}/catalog/versions", admin, params=bad)
        assert _code(response) == (400, "invalid_request", None)

    detail = routes.request("GET", f"/zones/{zone}/catalog/versions/1", admin).json()
    assert detail["envelope"]["payload"] == first["catalog"] == detail["catalog"]
    assert _code(routes.request("GET", f"/zones/{zone}/catalog/versions/6", admin)) == (
        404,
        "not_found",
        None,
    )


# --- model_version y encuadre --------------------------------------------------------------------


def test_model_version_change_marks_all_in_each_zone_and_software_never_does(
    routes: CatalogRoutes,
) -> None:
    site = routes.site(zones=2)
    (plant, zone_a), (_, zone_b) = site.zones()
    admin = routes.member(site)
    routes.productive(site, plant, zone_a)
    routes.configure(admin, zone_a)  # zone_b sin catálogo: también se marca

    assert not requires_model_regression("detector-v1", "detector-v1")  # solo software
    assert not requires_model_regression(None, "detector-v1")  # primer latido
    assert requires_model_regression("detector-v1", "detector-v2")
    marked = routes.run(
        routes.regression.mark_model_version_change(
            routes.system_context(site), (zone_b, zone_a, zone_a), "detector-v2"
        )
    )

    assert [r.zone_id for r in marked] == sorted((zone_a, zone_b))
    for zone in (zone_a, zone_b):
        regression = _regression(routes, admin, zone)
        assert regression["state"] == "pending" and regression["affected_row_ids"] == "all"
        assert regression["cause"] == "model_version_change"
        assert regression["model_version"] == "detector-v2"
        (mark,) = _marks(routes, zone)
        assert mark["cause"] == "model_version_change" and "catalog_version" not in mark
    assert routes.resulting_mode(zone_a) == "productive"
    # Una marca de catálogo posterior actualiza la causa y conserva el modelo y el periodo.
    first_mark = _regression(routes, admin, zone_a)["marked_at"]
    routes.request("PUT", f"/zones/{zone_a}/signals", admin, {"signals": [], "reason_es": REASON})
    regression = _regression(routes, admin, zone_a)
    assert (regression["cause"], regression["catalog_version"]) == ("catalog_change", 2)
    assert regression["model_version"] == "detector-v2" and regression["marked_at"] == first_mark

    for bad in ("Detector V2", "", "9model", "x" * 65):
        with pytest.raises(RegressionRequestInvalid):
            routes.run(
                routes.regression.mark_model_version_change(
                    routes.system_context(site), (zone_a,), bad
                )
            )
    with pytest.raises(RegressionRequestInvalid):
        routes.run(
            routes.regression.mark_model_version_change(routes.system_context(site), (), "m-1")
        )


def test_model_version_change_outside_the_scope_writes_nothing(routes: CatalogRoutes) -> None:
    site = routes.site(zones=2)
    (_, zone_a), (_, zone_b) = site.zones()
    other = routes.site()
    ((_, foreign),) = other.zones()
    reader = routes.context(routes.member(site, Role.COORDINATOR_SST, ScopeLevel.ZONE, zone_a))

    for context, zones in (
        (reader, (zone_a, zone_b)),  # zona fuera del alcance del contexto
        (routes.system_context(site), (zone_a, foreign)),  # zona de otra organización
        (routes.system_context(site), (zone_a, uuid.uuid4())),  # inexistente
    ):
        with pytest.raises(ResourceNotFound):
            routes.run(routes.regression.mark_model_version_change(context, zones, "detector-v3"))
    for zone in (zone_a, zone_b, foreign):
        assert routes.regression_row(zone) is None and _marks(routes, zone) == []
    # Con su propia zona sí marca: el rechazo de arriba es el filtro, no la sesión.
    routes.run(routes.regression.mark_model_version_change(reader, (zone_a,), "detector-v3"))
    assert routes.regression_row(zone_a) is not None


def test_framing_recapture_marks_all_with_the_installer_reason(routes: CatalogRoutes) -> None:
    site, plant, zone, admin, first = _configured(routes)
    installer, concession = routes.installer(site)
    camera_id = first["catalog"]["cameras"][1]["camera_id"]
    captured = format_timestamp(routes.authz.now() - timedelta(minutes=3))
    path = f"/zones/{zone}/framing-recaptures"
    body = {"camera_id": camera_id, "captured_at": captured, "reason_es": REASON}

    response = routes.request("POST", path, installer, body, concession=concession)

    assert response.status_code == 201, response.text
    view = response.json()
    assert (view["state"], view["cause"], view["affected_row_ids"]) == (
        "pending",
        "framing_recaptured",
        "all",
    )
    assert view["catalog_version"] is None
    assert len(routes.versions(zone)) == 1  # no crea versión del catálogo
    ((record),) = routes.records(zone, "walk_test_regression_marked")
    assert record["content"] == {
        "zone_id": str(zone),
        "cause": "framing_recaptured",
        "affected_row_ids": "all",
        "marked_at": view["marked_at"],
        "camera_id": camera_id,
        "captured_at": captured,
        "reason_es": REASON,
    }
    assert record["actor_role_in_use"] == "provider_installer"
    (event,) = routes.events(zone, "regression_marked")
    assert json.loads(event["payload"]) == {
        "zone_id": str(zone),
        "cause": "framing_recaptured",
        "catalog_version": None,
        "model_version": None,
        "affected_row_ids": "all",
    }
    assert routes.resulting_mode(zone) == "productive"
    assert _regression(routes, admin, zone)["cause"] == "framing_recaptured"

    before = routes.written(zone)
    for bad in (
        {**body, "camera_id": str(uuid.uuid4())},  # no es cámara de la zona
        {**body, "captured_at": format_timestamp(routes.authz.now() + timedelta(hours=1))},
        {**body, "captured_at": "2026-10-04T10:00:00"},
        {**body, "affected_row_ids": []},
        {k: v for k, v in body.items() if k != "reason_es"},
    ):
        refused = routes.request("POST", path, installer, bad, concession=concession)
        assert _code(refused) == (400, "invalid_request", None)
    named = routes.request(
        "POST", path, installer, {**body, "reason_es": "corto"}, concession=concession
    )
    assert _code(named) == (400, "invalid_request", "catalog_free_text_rejected")
    assert routes.written(zone) == before
    # El administrador del cliente no tiene commissioning.run: ni lo intenta el negocio.
    denied = routes.request("POST", path, admin, body)
    assert denied.status_code in (403, 404)
    assert routes.written(zone) == before
    assert plant is not None


def test_framing_recapture_on_a_zone_without_catalog_is_named(routes: CatalogRoutes) -> None:
    site = routes.site()
    ((_, zone),) = site.zones()
    installer, concession = routes.installer(site)

    response = routes.request(
        "POST",
        f"/zones/{zone}/framing-recaptures",
        installer,
        {
            "camera_id": str(uuid.uuid4()),
            "captured_at": format_timestamp(routes.authz.now()),
            "reason_es": REASON,
        },
        concession=concession,
    )

    assert _code(response) == (409, "conflict", "catalog_zone_without_cameras")
    assert routes.written(zone) == (0, 0, 0, 0)


# --- Lecturas bajo concesión (A-56) --------------------------------------------------------------


def test_provider_reads_are_audited_and_the_manage_routes_stay_closed(
    routes: CatalogRoutes,
) -> None:
    site, _, zone, _, _ = _configured(routes)
    installer, concession = routes.installer(site)
    before = routes.audits(site.organization_id, concession)

    for path in (
        f"/zones/{zone}/catalog",
        f"/zones/{zone}/catalog/versions",
        f"/zones/{zone}/catalog/versions/1",
        f"/zones/{zone}/regression",
    ):
        response = routes.request("GET", path, installer, concession=concession)
        assert response.status_code == 200, response.text
    audits = routes.audits(site.organization_id, concession)[len(before) :]
    assert audits.count("catalog_read") == 4
    written = routes.written(zone)
    closed = routes.request(
        "PUT",
        f"/zones/{zone}/thresholds",
        installer,
        {"review": 0.3, "publication": 0.9, "reason_es": REASON},
        concession=concession,
    )
    assert _code(closed) == (404, "not_found", None)
    assert routes.written(zone) == written


# --- Guardas de alcance --------------------------------------------------------------------------


ROUTE_CALLS: Final[list[tuple[str, str, dict[str, Any] | None]]] = [
    ("GET", "catalog", None),
    ("GET", "catalog/versions", None),
    ("GET", "catalog/versions/1", None),
    ("GET", "regression", None),
    ("PUT", "thresholds", {"review": 0.3, "publication": 0.9, "reason_es": REASON}),
    ("PUT", "cameras", {"cameras": [camera(0), camera(1)], "reason_es": REASON}),
    ("PUT", "catalog/single-occupancy", {"single_occupancy": True, "reason_es": REASON}),
    ("POST", "standards", standard_body("guard_bypass", GUARD_BYPASS)),
]


def test_out_of_scope_zones_answer_exactly_like_missing_ones(routes: CatalogRoutes) -> None:
    site = routes.site(plants=2, zones=2)
    (plant_a, zone_a1), (_, zone_a2), (_, zone_b1), _ = site.zones()
    admin = routes.member(site)
    for zone in (zone_a1, zone_a2, zone_b1):
        routes.configure(admin, zone)
    other = routes.site()
    ((_, foreign),) = other.zones()
    routes.configure(routes.member(other), foreign)
    plant_admin = routes.member(site, Role.ADMINISTRATOR, ScopeLevel.PLANT, plant_a)
    zone_admin = routes.member(site, Role.ADMINISTRATOR, ScopeLevel.ZONE, zone_a2)
    zone_reader = routes.member(site, Role.COORDINATOR_SST, ScopeLevel.ZONE, zone_a2)
    before = {z: routes.written(z) for z in (zone_a1, zone_a2, zone_b1, foreign)}

    for method, route, body in ROUTE_CALLS:
        cookies = (
            [(admin, foreign), (plant_admin, zone_b1), (zone_admin, zone_a1)]
            if method != "GET"
            else [(admin, foreign), (plant_admin, zone_b1), (zone_reader, zone_a1)]
        )
        for cookie, zone in cookies:
            seen = routes.request(method, f"/zones/{zone}/{route}", cookie, body)
            missing = routes.request(method, f"/zones/{uuid.uuid4()}/{route}", cookie, body)
            assert _comparable(seen) == _comparable(missing), (method, route, seen.text)
            assert _code(seen) == (404, "not_found", None), (method, route)
    assert {z: routes.written(z) for z in before} == before
    # Lo propio sí responde: la denegación de arriba es el filtro de zona, no la persona.
    own = routes.request("PUT", f"/zones/{zone_a2}/thresholds", zone_admin, ROUTE_CALLS[4][2])
    assert own.status_code == 200, own.text
    assert routes.request("GET", f"/zones/{zone_a2}/catalog", zone_reader).status_code == 200
    assert routes.request("GET", f"/zones/{zone_a2}/regression", zone_reader).status_code == 200


def test_each_zone_reads_only_its_own_versions_and_regression(routes: CatalogRoutes) -> None:
    # Misma organización y una persona con alcance de organización: la RLS no separa las zonas;
    # solo el filtro de zona de cada sentencia lo hace.
    site = routes.site(zones=2)
    (_, zone_a), (_, zone_b) = site.zones()
    admin = routes.member(site)
    routes.configure(admin, zone_a)
    routes.configure(admin, zone_b)
    for review in (0.1, 0.2):
        routes.request(
            "PUT",
            f"/zones/{zone_a}/thresholds",
            admin,
            {"review": review, "publication": 0.9, "reason_es": REASON},
        )

    history = routes.request("GET", f"/zones/{zone_b}/catalog/versions", admin).json()
    assert [(v["zone_id"], v["catalog_version"]) for v in history["versions"]] == [(str(zone_b), 1)]
    assert _code(routes.request("GET", f"/zones/{zone_b}/catalog/versions/3", admin))[0] == 404
    current = routes.request("GET", f"/zones/{zone_b}/catalog", admin).json()
    assert (current["zone_id"], current["catalog_version"]) == (str(zone_b), 1)
    assert _regression(routes, admin, zone_b)["state"] == "current"
    assert _regression(routes, admin, zone_a)["state"] == "pending"
    # Una marca de modelo en B no toca la fila de A.
    routes.run(
        routes.regression.mark_model_version_change(
            routes.system_context(site), (zone_b,), "detector-v4"
        )
    )
    assert _regression(routes, admin, zone_a)["cause"] == "catalog_change"


# --- Concurrencia --------------------------------------------------------------------------------


class HeldRegressionRepository(PostgresRegressionRepository):
    """Retiene la **primera** marca justo después de leer la fila de regresión.

    Con el candado de la fila, la segunda marca espera en ``pg_advisory_xact_lock``; sin él, llega
    también a leer la fila (la misma, sin la primera marca) y una de las dos uniones se pierde.
    """

    def __init__(self, database: Database) -> None:
        super().__init__(database)
        self.reads = 0
        self.first_read: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    def arm(self) -> None:
        self.reads = 0
        self.first_read = asyncio.Event()
        self.release = asyncio.Event()

    async def get(self, transaction: Transaction, zone_id: uuid.UUID) -> WalkTestRegression | None:
        found = await super().get(transaction, zone_id)
        self.reads += 1
        if self.reads == 1 and self.first_read is not None and self.release is not None:
            self.first_read.set()
            async with asyncio.timeout(HOLD_SECONDS):
                await self.release.wait()
        return found


async def _advisory_waiters(admin: Any) -> int:
    value: int = await admin.fetchval(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
    )
    return value


def _race(
    routes: CatalogRoutes, repository: HeldRegressionRepository, first: Any, second: Any
) -> list[Any]:
    admin = routes.authz.sessions.admin

    async def race() -> list[Any]:
        repository.arm()
        assert repository.first_read is not None and repository.release is not None
        one = asyncio.create_task(first())
        async with asyncio.timeout(WAIT_SECONDS):
            await repository.first_read.wait()
        two = asyncio.create_task(second())
        # Se suelta la primera al ver a la segunda esperando un candado o leyendo la fila.
        async with asyncio.timeout(WAIT_SECONDS):
            while not (await _advisory_waiters(admin) >= 1 or repository.reads >= 2):
                await asyncio.sleep(POLL_SECONDS)
        repository.release.set()
        return list(await asyncio.gather(one, two, return_exceptions=True))

    results: list[Any] = routes.run(race())
    return results


@pytest.mark.parametrize("attempt", [1, 2, 3])
def test_a_parameter_change_and_a_model_change_at_once_keep_both_marks(
    routes: CatalogRoutes, attempt: int
) -> None:
    site, _, zone, admin_cookie, first = _configured(routes)
    added = routes.request(
        "POST",
        f"/zones/{zone}/standards",
        admin_cookie,
        standard_body("guard_bypass", GUARD_BYPASS, "Resguardo"),
    ).json()
    # El periodo empieza con las filas del estándar nuevo; luego llegan las dos marcas a la vez.
    rows = _standard_rows(added["catalog"], _new_standard_id(first["catalog"], added["catalog"]))
    assert _regression(routes, admin_cookie, zone)["affected_row_ids"] == rows
    repository = HeldRegressionRepository(routes.database)
    publication, regression = routes.services(repository)
    admin = routes.context(admin_cookie)
    routes.tick()

    results = _race(
        routes,
        repository,
        lambda: publication.publish_catalog_version(
            admin,
            zone,
            SetThresholds(thresholds={"review": 0.25, "publication": 0.75}),
            REASON,
        ),
        lambda: regression.mark_model_version_change(
            routes.system_context(site), (zone,), "detector-v9"
        ),
    )

    assert not [r for r in results if isinstance(r, BaseException)], results
    assert isinstance(results[0], ZoneCatalogVersion) and results[0].catalog_version == 3
    assert [r["catalog_version"] for r in routes.versions(zone)] == [1, 2, 3]
    marks = _marks(routes, zone)
    assert sorted(m["cause"] for m in marks) == [
        "catalog_change",
        "catalog_change",
        "model_version_change",
    ]
    row = routes.regression_row(zone)
    assert row is not None and row["state"] == "pending"
    # Nunca se pierde una marca: la del modelo es la matriz completa y absorbe a la otra.
    assert json.loads(row["affected_row_ids"]) == "all"
    assert row["model_version"] == "detector-v9" and row["catalog_version"] == 3
    assert len(routes.events(zone, "regression_marked")) == 3
    assert attempt in (1, 2, 3)


def _dwell_change() -> NewStandard:
    return NewStandard(
        draft=StandardDraft(
            family=PredicateFamily.DWELL,
            title_es="Permanencia junto a la prensa",
            declared_text="La permanencia junto a la prensa no supera el tiempo declarado.",
            predicate=_dwell(5000),
        )
    )


@pytest.mark.parametrize("attempt", [1, 2, 3])
def test_two_simultaneous_standard_additions_leave_consecutive_versions_and_the_union(
    routes: CatalogRoutes, attempt: int
) -> None:
    _, _, zone, admin_cookie, _ = _configured(routes)
    repository = HeldRegressionRepository(routes.database)
    publication, _ = routes.services(repository)
    admin = routes.context(admin_cookie)
    routes.tick()
    guard = NewStandard(
        draft=StandardDraft(
            family=PredicateFamily.GUARD_BYPASS,
            title_es="Resguardo cerrado con persona dentro",
            declared_text="El resguardo no se cierra con una persona dentro de la zona.",
            predicate=GUARD_BYPASS,
        )
    )

    results = _race(
        routes,
        repository,
        lambda: publication.publish_catalog_version(admin, zone, guard, REASON),
        lambda: publication.publish_catalog_version(admin, zone, _dwell_change(), REASON),
    )

    assert all(isinstance(r, ZoneCatalogVersion) for r in results), results
    assert sorted(r.catalog_version for r in results) == [2, 3]
    assert [r["catalog_version"] for r in routes.versions(zone)] == [1, 2, 3]
    final = max(results, key=lambda r: r.catalog_version).payload
    new_ids = [s["standard_id"] for s in final["standards"]][1:]
    expected = sorted(
        r for standard_id in new_ids for r in _standard_rows(dict(final), standard_id)
    )
    row = routes.regression_row(zone)
    assert row is not None and row["state"] == "pending"
    assert sorted(json.loads(row["affected_row_ids"])) == expected and len(expected) == 8
    assert len(_marks(routes, zone)) == 2
    assert attempt in (1, 2, 3)
