"""PR-GOB-01: idempotencia de la ingesta (BR-GOB-89; BR-CTR-26 a 29; TASK-221).

Para toda presentación ``x`` de los generadores del kit de U-01 (``finding``,
``detection_for_review`` y ``observability_event``: los ``finding_submissions`` y
``detection_submissions`` del diseño), por la aplicación real y el ``IngestService`` real sobre los
dobles de ``tests/ingest_support.py``:

- ``submit(x); submit(x)`` deja **el mismo estado** que ``submit(x)`` (registros, eventos,
  concesiones, cierres huérfanos y auditoría) y la segunda respuesta es el ``Receipt`` **original**
  (mismo ``platform_record_id`` y ``received_at``) con ``status = accepted_duplicate``, aunque el
  reloj avance entre las dos;
- la misma clave con **cualquier** contenido distinto es siempre ``idempotency_conflict`` (409,
  ``field`` = el identificador) y no cambia nada salvo su entrada de auditoría.

El recibo va **dentro** del registro (``receipt``): la propiedad comprueba que el paso 5 compara el
registro sin recibo, no el hash del escritor. Semilla registrada por el perfil (``conftest``).
"""

from __future__ import annotations

import copy
import datetime as dt
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from vigia_contracts.conformance.generators import (
    detection_for_review,
    finding,
    observability_event,
    zone_catalog,
)
from vigia_contracts.models.api import parse_receipt, parse_rejection_response

from tests.ingest_support import IngestWorld, ingest_world, place
from vigia_platform.fleet.domain.ingest_order import IngestKind

HOUR = dt.timedelta(hours=1)
YEAR = dt.timedelta(days=365)

KINDS = tuple(IngestKind)


def _strategy(kind: IngestKind, catalog: dict[str, Any], node_id: str) -> st.SearchStrategy[Any]:
    if kind is IngestKind.FINDING:
        return finding(catalog, node_id=node_id)
    if kind is IngestKind.DETECTION_FOR_REVIEW:
        return detection_for_review(catalog, node_id=node_id)
    return observability_event(catalog, node_id=node_id)


def _ready(data: st.DataObject, kind: IngestKind) -> tuple[IngestWorld, dict[str, Any]]:
    """Un mundo con catálogo vigente y uso aprobado desde hace un año, y una presentación válida."""
    world = ingest_world()
    catalog = world.scoped(data.draw(zone_catalog(), label="catalog"))
    world.publish_catalog(catalog, world.now - YEAR)
    world.set_usage(True, world.now - YEAR)
    document = data.draw(_strategy(kind, catalog, str(world.a.node_id)), label="document")
    return world, place(document, world.now - HOUR)


def _state(world: IngestWorld) -> dict[str, Any]:
    return {
        "records": {key: (r.record_id, r.content) for key, r in world.store.records.items()},
        "events": [(event.event_name, dict(event.payload)) for event in world.writer.events],
        "grants": dict(world.store.grants),
        "orphans": list(world.store.orphan_closes),
        "rejections": list(world.writer.rejections),
    }


@settings(suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(data=st.data(), kind=st.sampled_from(KINDS))
def test_pr_gob_01_submitting_twice_leaves_the_state_of_once_with_the_original_receipt(
    data: st.DataObject, kind: IngestKind
) -> None:
    world, document = _ready(data, kind)
    first = world.post(kind, document)
    assert first.status_code == 200, first.text
    receipt = parse_receipt(first.content)
    assert receipt.status.value == "accepted"
    once = _state(world)
    assert len(once["records"]) == 1 and len(once["events"]) == 1
    (record,) = world.accepted(kind)
    assert str(record.record_id) == receipt.platform_record_id
    assert record.content["receipt"]["platform_record_id"] == receipt.platform_record_id

    world.advance(dt.timedelta(seconds=data.draw(st.integers(0, 3600), label="later")))
    second = world.post(kind, copy.deepcopy(document))
    assert second.status_code == 200, second.text
    again = parse_receipt(second.content)
    assert again.status.value == "accepted_duplicate"
    assert (again.platform_record_id, again.received_at) == (
        receipt.platform_record_id,
        receipt.received_at,
    )
    assert _state(world) == once
    assert world.audit.entries == []


@st.composite
def _changes(draw: st.DrawFn, document: dict[str, Any]) -> dict[str, Any]:
    """La misma presentación (misma clave) con otro contenido válido."""
    changed = copy.deepcopy(document)
    choice = draw(st.sampled_from(["software_version", "node_time", "clock_source"]))
    if choice == "software_version":
        current = changed["software_version"]
        changed["software_version"] = "9.8.7" if current != "9.8.7" else "9.8.6"
    elif choice == "node_time":
        node_time = changed["node_time"]
        shift = draw(st.sampled_from([1, 1000, 60_000]))
        changed = place(
            changed,
            dt.datetime.fromisoformat(node_time["started_at"]) - dt.timedelta(milliseconds=shift),
            duration=dt.datetime.fromisoformat(node_time["ended_at"])
            - dt.datetime.fromisoformat(node_time["started_at"]),
            offset_ms=node_time["clock"]["offset_ms"],
        )
    else:
        source = changed["node_time"]["clock"]["source"]
        changed["node_time"]["clock"]["source"] = "ntp-otro" if source != "ntp-otro" else "ntp-b"
    return changed


@settings(suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(data=st.data(), kind=st.sampled_from(KINDS))
def test_pr_gob_01_same_key_with_other_content_is_always_idempotency_conflict(
    data: st.DataObject, kind: IngestKind
) -> None:
    world, document = _ready(data, kind)
    assert world.post(kind, document).status_code == 200
    once = _state(world)
    changed = data.draw(_changes(document), label="changed")
    response = world.post(kind, changed)
    assert response.status_code == 409, response.text
    rejection = parse_rejection_response(response.content)
    assert rejection.code.value == "idempotency_conflict"
    assert rejection.field == kind.id_field
    assert _state(world) == once
    # Rechazo permanente: su auditoría, sin ``ingest_rejected`` (BR-GOB-96).
    (entry,) = world.audit.entries
    assert entry["filters"] == {
        "record_kind": kind.record_kind.value,
        "code": "idempotency_conflict",
    }
    assert world.writer.rejections == []
