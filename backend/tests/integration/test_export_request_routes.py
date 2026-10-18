"""Solicitud y estado de una exportación por HTTP contra PostgreSQL 16 real (TASK-317; LC-LAZ-20;
SUP-U05-21 y 22; BR-LAZ-87, 88 y 100; NFR-LAZ-10 y 25; A-68).

Como ``vigia_app``, con la aplicación real y ``ContextAuthorizer`` (``tests/export_support.py``):

- **Admisión**: ``202`` con ``requested``; en **una** transacción, la fila con el
  ``correlation_id`` de la petición, la auditoría ``export_requested`` y el evento
  ``export_requested`` en la partición de la **organización** (``plant_id`` nulo).
- **Bordes de BR-LAZ-87**: 20 zonas y 366 días se admiten; 21 zonas o 367 días,
  ``400 invalid_request`` con ``accreditation_export_scope_too_large`` (A-68); zonas de dos
  plantas, ``accreditation_export_zones_span_plants``; formas imposibles (cero zonas, repetidas,
  periodo vacío o al revés, campo de más), ``invalid_request`` sin ``detail_code``. Ningún rechazo
  deja fila, auditoría ni evento.
- **Estimación previa** (NFR-LAZ-10): con clips, la suma de ``size_bytes`` del periodo justo en
  20 GB se admite y un byte más se rechaza **sin encolar nada y sin tocar el almacén** (un doble
  de ``S3Storage`` que cuenta cero llamadas); los clips fuera del periodo, de otra zona o de otra
  organización no cuentan; sin clips, la misma suma se admite.
- **Alcance** (BR-NUC-09): una zona de otra organización, una inexistente o una fuera del alcance
  del solicitante responden ``404 not_found`` (nunca ``forbidden`` ni
  ``export_zone_out_of_scope``), y lo mismo la exportación de otra organización o de una zona
  fuera de alcance al consultarla.
- **Límites exactos por HTTP**: la sexta solicitud de un usuario en una hora, ``429
  rate_limited`` con ``Retry-After`` hasta que sale la más antigua; a la hora, cabe otra. La
  propiedad concurrente es ``tests/properties/laz/test_pr_laz_52_exact_limits_export.py``.
- **Estado**: ``GET`` devuelve lo de la fila; ``expired`` a los 30 días de ``ready`` con el reloj
  inyectado; nunca ``composing``.

Solo datos generados (NFR-CTR-43). El reloj simulado arranca en la hora de la base.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest

from tests.authz_support import Site
from tests.export_support import ExportStack, Person, SyntheticComposer, export_body, export_stack
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.accreditation.export.state import MAX_ESTIMATED_BYTES
from vigia_platform.loop.domain.enums import ExportState
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.storage import S3Storage

pytestmark = pytest.mark.integration

TOO_LARGE = "accreditation_export_scope_too_large"
SPAN = "accreditation_export_zones_span_plants"


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[ExportStack]:
    with export_stack(postgres_endpoint, "export_routes") as built:
        yield built


def _start(stack: ExportStack, days_ago: int = 60) -> datetime:
    return stack.now() - timedelta(days=days_ago)


def _first(site: Site) -> tuple[uuid.UUID, tuple[uuid.UUID, ...]]:
    plant = next(iter(site.plants))
    return plant, site.plants[plant]


def _assert_nothing_written(stack: ExportStack, organization_id: uuid.UUID) -> None:
    assert stack.requests_of(organization_id) == []
    assert stack.events(organization_id) == []
    assert stack.audit(organization_id) == []


def _detail(response: Any) -> tuple[int, str | None, str | None]:
    body = response.json()
    return response.status_code, body.get("code"), body.get("detail_code")


# --- admisión -----------------------------------------------------------------------------------


def test_an_admitted_request_writes_the_row_the_audit_and_the_job_in_one_go(
    stack: ExportStack,
) -> None:
    site = stack.site()
    plant, zones = _first(site)
    person = stack.member(site)
    start = _start(stack)
    response = stack.request_export(person, export_body(plant, list(zones[:2]), start=start))
    assert response.status_code == 202, response.text
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["export_state"] == "requested" and body["progress_percent"] == 0
    assert body["attempt_count"] == 0 and body["manifest_summary"] is None
    assert body["zone_ids"] == [str(zone) for zone in zones[:2]]
    assert body["package_form"] == "without_clips" and body["failure_code"] is None
    row = stack.export_row(body["export_id"])
    assert row["state"] == "requested" and row["user_id"] == person.user_id
    assert row["organization_id"] == site.organization_id and row["plant_id"] == plant
    assert str(row["correlation_id"]) == body["correlation_id"]
    assert row["expires_at"] - row["requested_at"] == timedelta(days=30)
    assert row["lease_owner"] is None and row["generated_at"] is None
    requested_by = json.loads(row["requested_by"])
    assert requested_by["user_id"] == str(person.user_id)
    assert requested_by["role_in_use"] == "coordinator_sst"
    (event,) = stack.events(site.organization_id)
    assert event["plant_id"] is None
    assert event["partition_key"] == f"{site.organization_id}:organization"
    assert json.loads(event["payload"]) == {
        "export_id": body["export_id"],
        "plant_id": str(plant),
        "zone_ids": [str(zone) for zone in zones[:2]],
        "package_form": "without_clips",
    }
    assert str(event["correlation_id"]) == body["correlation_id"]
    (audit,) = stack.audit(site.organization_id)
    assert audit["outcome"] == "success" and audit["scope_plant_id"] == plant
    assert (audit["resource_kind"], str(audit["resource_id"])) == ("export", body["export_id"])
    assert str(audit["correlation_id"]) == body["correlation_id"]


def test_the_plant_manager_with_plant_scope_can_request(stack: ExportStack) -> None:
    site = stack.site()
    plant, zones = _first(site)
    person = stack.member(site, Role.PLANT_MANAGER, ScopeLevel.PLANT, plant)
    response = stack.request_export(person, export_body(plant, [zones[0]], start=_start(stack)))
    assert response.status_code == 202, response.text


# --- bordes de BR-LAZ-87 ------------------------------------------------------------------------


def test_twenty_zones_and_366_days_are_admitted(stack: ExportStack) -> None:
    site = stack.site(plants=1, zones=21)
    plant, zones = _first(site)
    person = stack.member(site)
    body = export_body(plant, list(zones[:20]), start=_start(stack, 400), days=366)
    response = stack.request_export(person, body)
    assert response.status_code == 202, response.text


@pytest.mark.parametrize(
    ("zones", "days"),
    [(21, 30), (1, 367), (1, 366.001)],
    ids=["21-zonas", "367-dias", "366-dias-y-un-poco"],
)
def test_more_than_twenty_zones_or_366_days_is_too_large(
    stack: ExportStack, zones: int, days: float
) -> None:
    site = stack.site(plants=1, zones=21)
    plant, plant_zones = _first(site)
    person = stack.member(site)
    body = export_body(plant, list(plant_zones[:zones]), start=_start(stack, 400), days=days)
    response = stack.request_export(person, body)
    assert _detail(response) == (400, "invalid_request", TOO_LARGE), response.text
    _assert_nothing_written(stack, site.organization_id)


def test_zones_of_two_plants_are_rejected(stack: ExportStack) -> None:
    site = stack.site(plants=2, zones=1)
    (plant_a, zones_a), (_, zones_b) = site.plants.items()
    person = stack.member(site)
    body = export_body(plant_a, [zones_a[0], zones_b[0]], start=_start(stack))
    assert _detail(stack.request_export(person, body)) == (400, "invalid_request", SPAN)
    # Todas las zonas de otra planta que la pedida, también.
    body = export_body(plant_a, [zones_b[0]], start=_start(stack))
    assert _detail(stack.request_export(person, body)) == (400, "invalid_request", SPAN)
    _assert_nothing_written(stack, site.organization_id)


@pytest.mark.parametrize(
    "change",
    [
        lambda body: body.update(zone_ids=[]),
        lambda body: body.update(zone_ids=body["zone_ids"] * 2),
        lambda body: body["period"].update(to=body["period"]["from"]),
        lambda body: body["period"].update(
            {"from": body["period"]["to"], "to": body["period"]["from"]}
        ),
        lambda body: body["period"].update({"from": "2026-09-01T00:00:00"}),
        lambda body: body.update(package_form="composing"),
        lambda body: body.update(note="Juan Pérez"),
        lambda body: body.update(zone_ids=[str(uuid.uuid4())] * 81),
    ],
    ids=[
        "cero-zonas",
        "zonas-repetidas",
        "periodo-vacio",
        "periodo-al-reves",
        "sin-zona-horaria",
        "forma-desconocida",
        "campo-de-mas",
        "cuerpo-desmesurado",
    ],
)
def test_impossible_shapes_are_invalid_without_detail_code(stack: ExportStack, change: Any) -> None:
    site = stack.site()
    plant, zones = _first(site)
    person = stack.member(site)
    body = export_body(plant, [zones[0]], start=_start(stack))
    change(body)
    response = stack.request_export(person, body)
    assert _detail(response) == (400, "invalid_request", None), response.text
    _assert_nothing_written(stack, site.organization_id)


# --- estimación previa ---------------------------------------------------------------------------


@pytest.fixture
def storage_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Doble del almacén: cuenta cualquier operación de ``S3Storage`` (debe quedar en cero)."""
    calls: list[str] = []

    async def counted(self: Any, operation: str, *args: Any, **kwargs: Any) -> Any:
        calls.append(operation)
        raise AssertionError("la estimación no toca el almacén")

    monkeypatch.setattr(S3Storage, "_call", counted)
    monkeypatch.setattr(S3Storage, "_presign", counted)
    return calls


def _estimate_site(stack: ExportStack, total: int) -> tuple[Site, Person, dict[str, Any]]:
    site = stack.site(plants=1, zones=3)
    plant, zones = _first(site)
    start = _start(stack)
    inside = start + timedelta(days=1)
    # Lo pedido: dos zonas del periodo, en dos clips.
    stack.add_evidence(site.organization_id, plant, zones[0], total // 2, inside)
    stack.add_evidence(site.organization_id, plant, zones[1], total - total // 2, inside)
    # Lo que no cuenta: otra zona, antes y en el borde final del periodo, y otra organización.
    stack.add_evidence(site.organization_id, plant, zones[2], 10**12, inside)
    stack.add_evidence(site.organization_id, plant, zones[0], 10**12, start - timedelta(seconds=1))
    stack.add_evidence(site.organization_id, plant, zones[0], 10**12, start + timedelta(days=30))
    other = stack.site(plants=1, zones=1)
    other_plant, other_zones = _first(other)
    stack.add_evidence(other.organization_id, other_plant, other_zones[0], 10**12, inside)
    body = export_body(plant, list(zones[:2]), start=start, form="with_clips")
    return site, stack.member(site), body


def test_an_estimate_of_exactly_20_gb_is_admitted(
    stack: ExportStack, storage_calls: list[str]
) -> None:
    _, person, body = _estimate_site(stack, MAX_ESTIMATED_BYTES)
    response = stack.request_export(person, body)
    assert response.status_code == 202, response.text
    assert storage_calls == []


def test_an_estimate_over_20_gb_is_rejected_without_queueing_or_touching_storage(
    stack: ExportStack, storage_calls: list[str]
) -> None:
    site, person, body = _estimate_site(stack, MAX_ESTIMATED_BYTES + 1)
    response = stack.request_export(person, body)
    assert _detail(response) == (400, "invalid_request", TOO_LARGE), response.text
    _assert_nothing_written(stack, site.organization_id)
    assert storage_calls == []
    # La forma sin clips no lleva bytes de clips: la misma suma se admite.
    body["package_form"] = "without_clips"
    assert stack.request_export(person, body).status_code == 202
    assert storage_calls == []


# --- alcance ------------------------------------------------------------------------------------


def test_a_zone_out_of_scope_answers_like_a_missing_one(stack: ExportStack) -> None:
    site = stack.site(plants=1, zones=2)
    plant, zones = _first(site)
    # Coordinador con alcance de una sola zona: la otra existe y no es suya.
    person = stack.member(site, Role.COORDINATOR_SST, ScopeLevel.ZONE, zones[0])
    foreign = stack.site(plants=1, zones=1)
    foreign_plant, foreign_zones = _first(foreign)
    start = _start(stack)
    answers = [
        stack.request_export(person, export_body(plant, [zones[0], zones[1]], start=start)),
        stack.request_export(person, export_body(plant, [zones[1]], start=start)),
        stack.request_export(person, export_body(foreign_plant, [foreign_zones[0]], start=start)),
        stack.request_export(person, export_body(plant, [zones[0], foreign_zones[0]], start=start)),
        stack.request_export(person, export_body(plant, [uuid.uuid4()], start=start)),
    ]
    bodies = [(r.status_code, {k: v for k, v in r.json().items() if k != "correlation_id"})
              for r in answers]  # fmt: skip
    assert all(body == bodies[-1] for body in bodies), bodies
    assert bodies[-1] == (404, {"code": "not_found", "message_es": bodies[-1][1]["message_es"]})
    _assert_nothing_written(stack, site.organization_id)
    _assert_nothing_written(stack, foreign.organization_id)
    # Con su zona, sí.
    response = stack.request_export(person, export_body(plant, [zones[0]], start=start))
    assert response.status_code == 202, response.text


def test_roles_without_export_create_get_not_found(stack: ExportStack) -> None:
    site = stack.site()
    plant, zones = _first(site)
    for role in (Role.LINE_MANAGER, Role.ADMINISTRATOR, Role.COPASST):
        person = stack.member(site, role)
        response = stack.request_export(person, export_body(plant, [zones[0]], start=_start(stack)))
        assert _detail(response) == (404, "not_found", None), (role, response.text)
    _assert_nothing_written(stack, site.organization_id)


def test_the_status_of_another_organization_or_zone_answers_like_a_missing_one(
    stack: ExportStack,
) -> None:
    site = stack.site(plants=1, zones=2)
    plant, zones = _first(site)
    owner = stack.member(site)
    response = stack.request_export(owner, export_body(plant, [zones[1]], start=_start(stack)))
    export_id = response.json()["export_id"]
    assert stack.status(owner, export_id).status_code == 200
    narrow = stack.member(site, Role.COORDINATOR_SST, ScopeLevel.ZONE, zones[0])
    foreign = stack.member(stack.site())
    missing = stack.status(owner, uuid.uuid4())
    for person in (narrow, foreign):
        found = stack.status(person, export_id)
        assert found.status_code == 404 and found.json()["code"] == "not_found"
        assert {k: v for k, v in found.json().items() if k != "correlation_id"} == {
            k: v for k, v in missing.json().items() if k != "correlation_id"
        }


# --- límites exactos por HTTP -------------------------------------------------------------------


def test_the_sixth_request_of_a_user_in_an_hour_is_rate_limited(stack: ExportStack) -> None:
    site = stack.site()
    plant, zones = _first(site)
    person = stack.member(site)
    body = export_body(plant, [zones[0]], start=_start(stack))
    first_at = stack.now()
    for _ in range(5):
        assert stack.request_export(person, body).status_code == 202
        stack.tick(60)
    limited = stack.request_export(person, body)
    assert _detail(limited) == (429, "rate_limited", None), limited.text
    expected = int((first_at + timedelta(hours=1) - stack.now()).total_seconds())
    assert limited.json()["retry_after_seconds"] == expected
    assert limited.headers["Retry-After"] == str(expected)
    assert len(stack.requests_of(site.organization_id)) == 5
    # Otro usuario de la organización sí cabe; el primero, cuando sale la más antigua.
    assert stack.request_export(stack.member(site), body).status_code == 202
    stack.clock.set(first_at + timedelta(hours=1) - timedelta(milliseconds=1))
    assert stack.request_export(person, body).status_code == 429
    stack.clock.set(first_at + timedelta(hours=1))
    assert stack.request_export(person, body).status_code == 202


def test_the_twenty_first_request_of_an_organization_in_an_hour_is_rate_limited(
    stack: ExportStack,
) -> None:
    site = stack.site()
    plant, zones = _first(site)
    body = export_body(plant, [zones[0]], start=_start(stack))
    people = [stack.member(site) for _ in range(5)]
    for index in range(20):
        response = stack.request_export(people[index // 4], body)
        assert response.status_code == 202, (index, response.text)
    limited = stack.request_export(people[4], body)
    assert _detail(limited) == (429, "rate_limited", None)
    assert len(stack.requests_of(site.organization_id)) == 20
    # Otra organización no comparte el límite.
    other = stack.site()
    other_plant, other_zones = _first(other)
    response = stack.request_export(
        stack.member(other), export_body(other_plant, [other_zones[0]], start=_start(stack))
    )
    assert response.status_code == 202


# --- estado -------------------------------------------------------------------------------------


def test_the_status_follows_the_row_and_expires_after_30_days_of_ready(stack: ExportStack) -> None:
    site = stack.site()
    plant, zones = _first(site)
    person = stack.member(site)
    response = stack.request_export(person, export_body(plant, [zones[0]], start=_start(stack)))
    export_id = response.json()["export_id"]
    composer = SyntheticComposer()
    stack.worker = stack.export_worker(composer=composer)
    stack.run(stack.worker.run(stack.event_context(site.organization_id), uuid.UUID(export_id)))
    ready = stack.status(person, export_id).json()
    assert ready["export_state"] == "ready" and ready["progress_percent"] == 100
    assert ready["attempt_count"] == 1 and ready["current_artifact"] is None
    package = composer.packages[0]
    assert ready["manifest_summary"] == {
        "document_sha256": package.document_sha256,
        "archive_sha256": package.archive_sha256,
        "size_bytes": package.size_bytes,
        "artifact_count": package.artifact_count,
        "checkpoint_count": 3,
        "verifier_version": package.verifier_version,
    }
    completed = datetime.fromisoformat(ready["completed_at"])
    assert datetime.fromisoformat(ready["expires_at"]) == completed + timedelta(days=30)
    stack.clock.set(completed + timedelta(days=30) - timedelta(milliseconds=1))
    assert stack.status(person, export_id).json()["export_state"] == "ready"
    stack.clock.set(completed + timedelta(days=30))
    expired = stack.status(person, export_id).json()
    assert expired["export_state"] == "expired"
    # Lo que caduca es la descarga: el paquete sigue ahí y la fila no cambia.
    assert expired["manifest_summary"] == ready["manifest_summary"]
    assert stack.export_row(export_id)["state"] == ExportState.READY.value
    assert stack.package_row(export_id) is not None
