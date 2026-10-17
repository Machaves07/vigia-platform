"""La zona del rechazo auditado de la ingesta cuando el esquema rechaza (BR-GOB-96; VIG-257).

``IngestRejected.zone_id`` es «la zona que nombra la presentación, si se pudo leer». Los rechazos
que decide la verificación previa común después de leer el cuerpo (esquema, ``Idempotency-Key``
repetida) no pasan por ``IngestService``: la auditoría tiene que tomar la zona del JSON leído, y
solo si es de la organización del certificado. Con el cuerpo sin leer (tamaño) no hay zona.

Las rutas de la ingesta no llevan ``zone_id`` en el path: la zona va en el cuerpo.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from vigia_contracts.conformance.generators import (
    detection_for_review,
    finding,
    observability_event,
    zone_catalog,
)
from vigia_contracts.models.api import parse_rejection_response

from tests.ingest_support import ROUTE_OF, IngestWorld, ingest_world, place
from vigia_platform.fleet.domain.ingest_order import IngestKind

HOUR = dt.timedelta(hours=1)
YEAR = dt.timedelta(days=365)
SETTINGS = settings(
    max_examples=15, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large]
)


def _strategy(kind: IngestKind, catalog: dict[str, Any], node_id: str) -> st.SearchStrategy[Any]:
    if kind is IngestKind.FINDING:
        return finding(catalog, node_id=node_id)
    if kind is IngestKind.DETECTION_FOR_REVIEW:
        return detection_for_review(catalog, node_id=node_id)
    return observability_event(catalog, node_id=node_id)


def _ready(data: st.DataObject, kind: IngestKind) -> tuple[IngestWorld, dict[str, Any]]:
    world = ingest_world()
    catalog = world.scoped(data.draw(zone_catalog(), label="catalog"))
    world.publish_catalog(catalog, world.now - YEAR)
    world.set_usage(True, world.now - YEAR)
    document = data.draw(_strategy(kind, catalog, str(world.a.node_id)), label="document")
    return world, place(document, world.now - HOUR)


def _rejected(world: IngestWorld, response: Any, code: str) -> dict[str, Any]:
    assert parse_rejection_response(response.content).code.value == code, response.text
    assert world.store.records == {} and world.writer.events == []
    (entry,) = world.audit.entries
    assert entry["filters"]["code"] == code
    return entry


@SETTINGS
@given(data=st.data(), kind=st.sampled_from(tuple(IngestKind)))
def test_a_schema_rejection_audits_the_zone_the_body_names(
    data: st.DataObject, kind: IngestKind
) -> None:
    world, document = _ready(data, kind)
    document["campo_no_declarado"] = 1
    entry = _rejected(world, world.post(kind, document), "schema_invalid")
    assert entry["zone_id"] == world.zone


@SETTINGS
@given(data=st.data(), kind=st.sampled_from(tuple(IngestKind)))
def test_a_repeated_idempotency_key_audits_the_zone_the_body_names(
    data: st.DataObject, kind: IngestKind
) -> None:
    world, document = _ready(data, kind)
    headers = list(world.headers(document, kind).items())
    headers.append(("Idempotency-Key", str(uuid.uuid4())))
    with TestClient(world.app) as client:
        response = client.post(
            ROUTE_OF[kind].path, headers=headers, content=json.dumps(document).encode()
        )
    entry = _rejected(world, response, "schema_invalid")
    assert parse_rejection_response(response.content).field == "Idempotency-Key"
    assert entry["zone_id"] == world.zone


@SETTINGS
@given(
    data=st.data(),
    kind=st.sampled_from(tuple(IngestKind)),
    zone=st.sampled_from(["foreign", "garbage", "number", "absent"]),
)
def test_a_schema_rejection_audits_no_zone_outside_the_organization_or_unreadable(
    data: st.DataObject, kind: IngestKind, zone: str
) -> None:
    world, document = _ready(data, kind)
    # Sin instante legible, ``check_body`` no consulta la asignación y decide el esquema.
    del document["node_time"]
    if zone == "foreign":
        document["zone_id"] = str(world.foreign_zone)
    elif zone == "garbage":
        document["zone_id"] = "zona-1"
    elif zone == "number":
        document["zone_id"] = 7
    else:
        del document["zone_id"]
    entry = _rejected(world, world.post(kind, document), "schema_invalid")
    assert entry["zone_id"] is None


@SETTINGS
@given(data=st.data(), kind=st.sampled_from(tuple(IngestKind)))
def test_an_audit_that_cannot_be_written_still_fails_closed_as_transient(
    data: st.DataObject, kind: IngestKind
) -> None:
    """El patrón de ``on_rejection`` no cambia: sin rastro, transitorio (el nodo reintenta)."""
    world, document = _ready(data, kind)
    document["campo_no_declarado"] = 1

    async def broken(*_: Any, **__: Any) -> None:
        raise ConnectionError("auditoría caída")

    world.audit.append = broken  # type: ignore[method-assign]
    response = world.post(kind, document)
    rejection = parse_rejection_response(response.content)
    assert rejection.code.value == "temporarily_unavailable", response.text
    assert rejection.retryable is True
    assert world.store.records == {} and world.writer.events == []


@SETTINGS
@given(data=st.data(), kind=st.sampled_from(tuple(IngestKind)))
def test_a_body_over_the_limit_is_not_read_and_audits_no_zone(
    data: st.DataObject, kind: IngestKind
) -> None:
    world, document = _ready(data, kind)
    body = json.dumps(document, separators=(",", ":")).encode()
    limit = 262_144 if kind.gated else 65_536
    body += b" " * (limit - len(body) + 1)
    response = world.post(kind, document, body=body)
    entry = _rejected(world, response, "payload_too_large")
    assert entry["zone_id"] is None
