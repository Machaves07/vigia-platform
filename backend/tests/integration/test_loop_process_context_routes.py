"""Valores de proceso y nombre de cada señal en el detalle y la ficha de vida (TASK-595; A-129;
BR-LAZ-06 y 09; NFR-LAZ-03).

Sobre PostgreSQL 16 real como ``vigia_app`` y la aplicación real (``detail_stack``): una
organización nueva con la versión 2 del catálogo de su zona, que declara dos variables de proceso
(velocidad de la máquina, analógica, y jumbo en posición, de estado sobre la señal declarada), un
hallazgo ingresado por el nodo con su bloque ``process_context`` y, después, la versión 3 que
renombra la señal:

- **Detalle**: cada variable con su nombre, unidad, decimales y par de estados de la versión del
  registro, y los valores tal como se guardaron; cada señal con su ``code`` y su ``description_es``
  de la versión 2, no de la 3 (la vigente hoy).
- **Ficha de vida**: el hito ``finding_received`` lleva el resumen por variable, sin la serie.
- **Sin lectura ni auditoría nueva**: el detalle deja una ``ledger_detail_read`` por registro y la
  ficha una sola, como antes; las llamadas a U-03 del detalle siguen siendo las tres de siempre.
- **Alcance**: el puerto de versiones del catálogo solo ve la zona pedida de la organización del
  contexto; la de otra organización no devuelve nada (RLS y filtro explícito).
- **Zona unipersonal**: ``at_event.read_at`` (y la hora del resumen) llegan agregadas y la hora
  exacta del instante del hecho no aparece en ninguna respuesta.

Solo datos generados.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import IngestedSubject, LoopOrganization
from tests.loop_detail_support import DetailStack, Who, detail_stack
from vigia_platform.fleet.application.record_declarations import RecordCatalogs
from vigia_platform.fleet.domain.clock_tolerance import Window
from vigia_platform.fleet.domain.ingest_order import IngestKind
from vigia_platform.loop.adapters.http.timeline_routes import TIMELINE_PATH
from vigia_platform.loop.domain.time_aggregation import presented
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration

SPEED: Final = "3c4d5e6f-7081-4c9d-8e0f-2a3b4c5d6e7f"
JUMBO: Final = "4d5e6f70-8192-4dae-9f10-3b4c5d6e7f80"
RENAMED: Final = "Energía de la prensa (renombrada)"
WINDOW_MINUTES: Final = 60


@dataclass(frozen=True)
class Ingested:
    organization: LoopOrganization
    finding: IngestedSubject
    finding_state_id: uuid.UUID
    started: datetime
    who: Who


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[DetailStack]:
    with detail_stack(postgres_endpoint, "loop_process_context") as built:
        yield built


def _publish(stack: DetailStack, organization: LoopOrganization, payload: dict[str, Any]) -> None:
    """La versión ``payload["version"]`` del catálogo, vigente desde su ``issued_at`` (cierra la
    anterior en ese instante)."""
    ingest = stack.world.stack
    site = organization.site
    version = int(payload["version"])
    issued = datetime.fromisoformat(str(payload["issued_at"]))
    ingest.execute(
        "UPDATE catalog.zone_catalog_version SET superseded_at = $3"
        " WHERE zone_id = $1 AND catalog_version = $2",
        site.zone_id,
        version - 1,
        issued,
    )
    ingest.execute(
        "INSERT INTO catalog.zone_catalog_version (organization_id, plant_id, zone_id,"
        " catalog_version, issued_at, issued_by, role_in_use, reason_es, changed_fields,"
        " payload, envelope, single_occupancy, ledger_record_id)"
        " VALUES ($1, $2, $3, $4, $5, $6, 'administrator', 'Variables de proceso sintéticas',"
        " ARRAY['standards'], $7::jsonb, $8::jsonb, false, $9)",
        site.organization_id,
        site.plant_id,
        site.zone_id,
        version,
        issued,
        ingest.authz.operator_id,
        json.dumps(payload),
        json.dumps({"payload": payload}),
        uuid.uuid4(),
    )


def _catalog(
    stack: DetailStack, organization: LoopOrganization, issued: datetime, version: int
) -> dict[str, Any]:
    site = organization.site
    payload = stack.world.stack.catalog(site, issued, version)
    payload["process_context"] = {
        "before_ms": 30_000,
        "variables": [
            {
                "variable_id": SPEED,
                "code": "VEL-MAQ",
                "kind": "analog",
                "description_es": "Velocidad de la máquina",
                "source": {"reader": "a", "channel": 1},
                "unit": "m_per_min",
                "scale": {"at_4_ma": 0.0, "at_20_ma": 2000.0},
                "decimals": 0,
            },
            {
                "variable_id": JUMBO,
                "code": "JUMBO-POS",
                "kind": "binary",
                "description_es": "Jumbo en posición",
                "signal_id": str(site.signal_id),
                "states": "in_position_out",
            },
        ],
    }
    return payload


BLOCK: Final[dict[str, Any]] = {
    "from_offset_ms": -30_000,
    "to_offset_ms": 30_000,
    "variables": [
        {
            "variable_id": SPEED,
            "kind": "analog",
            "at_event": {"value": 1350.75, "valid": True, "offset_ms": 2_500},
            "min": 1340.5,
            "max": 1402.25,
            "last": 1351.0,
            "samples": [
                {"offset_ms": -20_000, "value": 1402.25},
                {"offset_ms": 2_500, "value": 1350.75},
            ],
        },
        {
            "variable_id": JUMBO,
            "kind": "binary",
            "at_event": {"value": "asserted", "valid": True, "offset_ms": 2_500},
            "changes": [
                {"offset_ms": -30_000, "value": "deasserted"},
                {"offset_ms": -5_000, "value": "asserted"},
            ],
            "truncated": False,
        },
    ],
}


@pytest.fixture(scope="module")
def ingested(stack: DetailStack) -> Ingested:
    """Una organización nueva, su catálogo 2 con variables, un hallazgo con su bloque y el
    catálogo 3 (posterior al hallazgo) que renombra la señal."""
    world = stack.world
    ingest = world.stack
    organization = world.organization()
    site = organization.site
    issued = ingest.now() - timedelta(minutes=5)
    _publish(stack, organization, _catalog(stack, organization, issued, 2))
    started = ingest.now() - timedelta(minutes=2)
    document = ingest.finding(site, started, standard_version=2)
    document["process_context"] = BLOCK
    finding = world._ingest(organization, IngestKind.FINDING, document)
    assert world.project(finding).transition_id == "T01"
    stack.tick(5.0)
    later = _catalog(stack, organization, ingest.now(), 3)
    later["signals"][0]["description_es"] = RENAMED
    _publish(stack, organization, later)
    finding_state_id = stack.finding_state_id(finding)
    return Ingested(organization, finding, finding_state_id, started, stack.member(organization))


def _ok(response: Any) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _reads(stack: DetailStack, organization: LoopOrganization) -> list[dict[str, Any]]:
    return stack.audit_reads(organization)


def _timeline(stack: DetailStack, ingested: Ingested) -> dict[str, Any]:
    path = TIMELINE_PATH.format(finding_state_id=ingested.finding_state_id)
    return _ok(stack.get(ingested.who, path))


def test_the_detail_names_each_variable_and_signal_with_the_record_version(
    stack: DetailStack, ingested: Ingested
) -> None:
    stack.occupancy(ingested.finding_state_id, False)
    stack.ports.reset()
    before = len(_reads(stack, ingested.organization))
    body = _ok(stack.detail(ingested.who, ingested.finding_state_id))
    reads = _reads(stack, ingested.organization)[before:]
    # Sin lectura nueva ni auditoría nueva: una entrada por registro leído, como siempre.
    assert [(r["resource_kind"], r["resource_id"]) for r in reads] == [
        ("ledger_record", ingested.finding.record_id)
    ]
    assert dict(stack.ports.calls) == {
        "state_at": 1,
        "standard_version": 1,
        "nodes_by_zone": 1,
        "estado_en": 1,
    }
    (record,) = body["records"]
    ((signal_code, signal_name),) = {(s["code"], s["description_es"]) for s in record["signals"]}
    assert (signal_code, signal_name) == ("SIG-01", "Señal de energía de la prensa")
    assert signal_name != RENAMED  # la versión del registro, no la vigente hoy
    block = record["process_context"]
    assert (block["from_offset_ms"], block["to_offset_ms"]) == (-30_000, 30_000)
    speed, jumbo = block["variables"]
    event_at = format_timestamp(ingested.started + timedelta(milliseconds=2_500))
    assert speed == {
        "variable_id": SPEED,
        "kind": "analog",
        "code": "VEL-MAQ",
        "description_es": "Velocidad de la máquina",
        "unit": "m_per_min",
        "decimals": 0,
        "states": None,
        "at_event": {"value": 1350.75, "valid": True, "offset_ms": 2_500, "read_at": event_at},
        "min": 1340.5,
        "max": 1402.25,
        "last": 1351.0,
        "samples": [
            {"offset_ms": -20_000, "value": 1402.25},
            {"offset_ms": 2_500, "value": 1350.75},
        ],
        "changes": [],
        "truncated": None,
    }
    assert (jumbo["code"], jumbo["description_es"], jumbo["states"]) == (
        "JUMBO-POS",
        "Jumbo en posición",
        "in_position_out",
    )
    assert jumbo["at_event"]["value"] == "asserted"
    assert jumbo["changes"] == BLOCK["variables"][1]["changes"]
    assert jumbo["truncated"] is False


def test_the_timeline_detection_milestone_summarizes_without_the_series(
    stack: DetailStack, ingested: Ingested
) -> None:
    stack.occupancy(ingested.finding_state_id, False)
    before = len(_reads(stack, ingested.organization))
    body = _timeline(stack, ingested)
    reads = _reads(stack, ingested.organization)[before:]
    assert [(r["resource_kind"], r["resource_id"]) for r in reads] == [
        ("finding", ingested.finding_state_id)
    ]
    milestones = {m["kind"]: m for m in body["milestones"]}
    summary = milestones["finding_received"]["process"]
    assert [(item["code"], item["value"], item["valid"]) for item in summary] == [
        ("VEL-MAQ", 1350.75, True),
        ("JUMBO-POS", "asserted", True),
    ]
    assert summary[0]["unit"] == "m_per_min" and summary[1]["states"] == "in_position_out"
    assert all(not {"samples", "changes", "min", "max"} & set(item) for item in summary)
    assert all(m["process"] == [] for m in body["milestones"] if m["kind"] != "finding_received")


def test_in_a_single_occupancy_zone_no_event_time_is_exact(
    stack: DetailStack, ingested: Ingested
) -> None:
    stack.occupancy(ingested.finding_state_id, True, WINDOW_MINUTES)
    try:
        detail = _ok(stack.detail(ingested.who, ingested.finding_state_id))
        timeline = _timeline(stack, ingested)
    finally:
        stack.occupancy(ingested.finding_state_id, False)
    exact = ingested.started + timedelta(milliseconds=2_500)
    aggregated = format_timestamp(
        presented(exact, single_occupancy=True, window_minutes=WINDOW_MINUTES)
    )
    assert detail["time_aggregated"] is True
    (record,) = detail["records"]
    assert [v["at_event"]["read_at"] for v in record["process_context"]["variables"]] == [
        aggregated,
        aggregated,
    ]
    assert [v["at_event"]["offset_ms"] for v in record["process_context"]["variables"]] == [
        2_500,
        2_500,
    ]
    summary = {m["kind"]: m for m in timeline["milestones"]}["finding_received"]["process"]
    assert [item["read_at"] for item in summary] == [aggregated, aggregated]
    for body in (detail, timeline):
        assert format_timestamp(exact) not in _values(body)
        assert format_timestamp(ingested.started) not in _values(body)


def _values(document: Any) -> list[Any]:
    """Todos los valores escalares de un JSON (comparación por igualdad, nunca por subcadena)."""
    if isinstance(document, dict):
        return [v for value in document.values() for v in _values(value)]
    if isinstance(document, list):
        return [v for value in document for v in _values(value)]
    return [document]


def test_the_catalog_port_only_sees_zones_of_the_context_organization(
    stack: DetailStack, ingested: Ingested
) -> None:
    world = stack.world
    catalogs = RecordCatalogs(database=stack.database)
    window = Window(ingested.started - timedelta(days=3650), ingested.started + timedelta(days=1))
    zone = ingested.organization.site.zone_id
    own = stack.run(
        catalogs.versions(world.context(ingested.organization), zone_id=zone, window=window)
    )
    assert [item.view.version for item in own] == [3, 2, 1]
    other = stack.run(catalogs.versions(world.context(world.b), zone_id=zone, window=window))
    assert other == ()
