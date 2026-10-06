"""H-48: actualizar la flota sin apagarla (TASK-226; LC-GOB-17; BR-GOB-102 a 104; nota T-05).

**Sin propiedades nuevas** (BL §6, C-PLA-16): el componente no transforma datos ni tiene máquina de
estados propia, y la decisión de ventana ya la cubre PR-GOB-07 por oráculo; se verifica con estos
ejemplos de H-48, contra PostgreSQL 16 real como ``vigia_app`` (``tests/fleet_ingest_support.py``
más ``POST update-results``, ``tests/fleet_versions_support.py``):

- **aplicada**: la plataforma publica la versión objetivo, el nodo la aplica y reporta ``applied``:
  ``last_update_result = applied``, con su registro y su evento;
- **revertida conservando la cola**: el nodo tenía hallazgos, una detección y un evento de
  observabilidad en cola (presentaciones del contrato construidas **antes** de actualizar, todavía
  sin enviar); la actualización revierte, reporta ``reverted`` y, después, la cola entra **entera**
  y sin pérdida (BR-GOB-103, 104: la ingesta acepta la ventana de compatibilidad durante la
  convivencia);
- **fallida**: ``failed`` se escribe, se publica y se proyecta igual (nota T-05).

El «nodo simulado de U-01» es aquí el nodo sembrado con su certificado y sus presentaciones del
contrato, leídas con el lector estricto de U-01 (``api.parse_*``): el ``SimulatedNode`` del kit
necesita un conjunto sellado y ``GET conformance-profile`` (fuera de v1, A-51). Solo datos generados
(NFR-CTR-43). Reloj simulado desde la hora de la base.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from tests.fleet_ingest_support import IngestSite, IngestStack, ingest_stack
from tests.fleet_versions_support import (
    NodeApp,
    installer_context,
    inventory_row,
    node_app,
    publisher,
    send_update,
    update_result,
)
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.fleet.domain.fleet_versions import TargetVersionPublication
from vigia_platform.fleet.domain.ingest_order import IngestKind
from vigia_platform.node_api.routes.detection_reviews import detection_review_operation
from vigia_platform.node_api.routes.findings import finding_operation
from vigia_platform.node_api.routes.observability_events import observability_event_operation
from vigia_platform.shared.api.declarations import NodeRoute

pytestmark = pytest.mark.integration

PREVIOUS = "1.0.2"
TARGET = "1.0.3"


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[IngestStack]:
    with ingest_stack(postgres_endpoint, "h48_update_results") as built:
        yield built


@pytest.fixture(scope="module")
def app(stack: IngestStack) -> Iterator[NodeApp]:
    service = stack.primary.service
    built = node_app(
        stack.authz,
        stack.primary.database,
        stack.primary.writer,
        {
            NodeRoute.FINDING: finding_operation(service),
            NodeRoute.DETECTION_REVIEW: detection_review_operation(service),
            NodeRoute.OBSERVABILITY_EVENT: observability_event_operation(service),
        },
    )
    yield built
    stack.run(built.client.aclose())


def _node(stack: IngestStack) -> IngestSite:
    """Un nodo en operación (con su fila de inventario) con la versión objetivo publicada."""
    site = stack.site()
    inventory_row(
        stack.authz,
        organization_id=site.organization_id,
        plant_id=site.plant_id,
        node_id=site.node_id,
        at=stack.now(),
    )
    return site


def _publish(stack: IngestStack, site: IngestSite) -> TargetVersionPublication:
    service = publisher(stack.authz, stack.primary.database, stack.primary.writer)
    start = stack.now() + dt.timedelta(hours=1)
    publication: TargetVersionPublication = stack.run(
        service.publish(
            installer_context(stack.authz, site.organization_id),
            site.plant_id,
            node_ids=[site.node_id],
            group=None,
            target_version=TARGET,
            window_from=start,
            window_to=start + dt.timedelta(hours=2),
        )
    )
    return publication


def _report(
    stack: IngestStack, app: NodeApp, site: IngestSite, outcome: str
) -> tuple[httpx.Response, dict[str, Any]]:
    document = update_result(
        organization_id=site.organization_id,
        plant_id=site.plant_id,
        node_id=site.node_id,
        update_result_id=stack.uuid7(),
        verified_at=stack.now(),
        target_version=TARGET,
        previous_version=PREVIOUS,
        outcome=outcome,
    )
    response: httpx.Response = stack.run(send_update(app.client, site.certificate, document))
    return response, document


def _inventory(stack: IngestStack, site: IngestSite) -> tuple[str | None, str | None]:
    (row,) = stack.fetch(
        "SELECT target_version, last_update_result FROM fleet.node_inventory WHERE node_id = $1",
        site.node_id,
    )
    return row["target_version"], row["last_update_result"]


@pytest.mark.parametrize("outcome", ["applied", "reverted", "failed"])
def test_h48_each_result_is_written_published_and_projected(
    stack: IngestStack, app: NodeApp, outcome: str
) -> None:
    site = _node(stack)
    _publish(stack, site)
    assert _inventory(stack, site) == (TARGET, None)
    stack.tick()
    response, document = _report(stack, app, site, outcome)
    # reverted y failed no son errores de la plataforma: son datos del inventario.
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["status"] == "accepted"
    assert receipt["received_at"].endswith("Z")
    assert _inventory(stack, site) == (TARGET, outcome)
    (record,) = stack.records("update_result_received", site.organization_id)
    assert str(record["record_id"]) == receipt["platform_record_id"]
    assert record["scope_node_id"] == site.node_id and record["plant_id"] == site.plant_id
    assert record["content"] == {
        "update_result_id": document["update_result_id"],
        "node_id": str(site.node_id),
        "target_version": TARGET,
        "result": outcome,
        "reported_at": receipt["received_at"],
    }
    (event,) = stack.events("update_result_received", site.organization_id)
    assert event["payload"] == {
        "node_id": str(site.node_id),
        "target_version": TARGET,
        "result": outcome,
    }


def test_h48_a_reverted_update_keeps_the_queue_and_it_enters_afterwards_without_loss(
    stack: IngestStack, app: NodeApp
) -> None:
    site = _node(stack)
    # La cola local del nodo antes de actualizar: presentaciones del contrato aún sin enviar.
    started = stack.now() - dt.timedelta(minutes=20)
    queued: list[tuple[IngestKind, dict[str, Any]]] = [
        (IngestKind.FINDING, stack.finding(site, started)),
        (IngestKind.FINDING, stack.finding(site, started + dt.timedelta(minutes=2))),
        (IngestKind.DETECTION_FOR_REVIEW, stack.detection(site, started)),
        (IngestKind.OBSERVABILITY_EVENT, stack.event(site, started)),
    ]
    _publish(stack, site)
    stack.tick()
    response, _ = _report(stack, app, site, "reverted")
    assert response.status_code == 200, response.text
    assert _inventory(stack, site) == (TARGET, "reverted")
    # Tras revertir, la cola entra entera: nada se descartó por la actualización.
    for kind, document in queued:
        stack.tick()
        accepted = stack.post(site, kind, document, stack.primary)
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["status"] == "accepted"
    findings = stack.records("finding_received", site.organization_id)
    assert sorted(r["content"]["finding_id"] for r in findings) == sorted(
        str(document["finding_id"]) for kind, document in queued if kind is IngestKind.FINDING
    )
    assert len(stack.records("detection_for_review_received", site.organization_id)) == 1
    assert len(stack.records("observability_event_received", site.organization_id)) == 1
    assert len(stack.events("finding_received", site.organization_id)) == 2
    # El resultado de la actualización sigue en el inventario después de la cola.
    assert _inventory(stack, site) == (TARGET, "reverted")


def test_h48_the_queue_also_enters_through_the_same_application_as_the_result(
    stack: IngestStack, app: NodeApp
) -> None:
    site = _node(stack)
    finding = stack.finding(site)
    _publish(stack, site)
    response, _ = _report(stack, app, site, "failed")
    assert response.status_code == 200, response.text
    sent = stack.run(
        app.client.post(
            NodeRoute.FINDING.path,
            content=httpx.Request("POST", "/", json=finding).content,
            headers=site.headers(finding, IngestKind.FINDING),
        )
    )
    assert sent.status_code == 200, sent.text
    assert uuid.UUID(sent.json()["platform_record_id"]).version == 7
    assert _inventory(stack, site) == (TARGET, "failed")
