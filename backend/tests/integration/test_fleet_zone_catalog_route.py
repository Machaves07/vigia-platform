"""``GET zones/{zone_id}/catalog`` contra PostgreSQL 16 real como ``vigia_app`` (TASK-223).

- Sirve el ``ZoneCatalogVersion.envelope`` **vigente** byte a byte, tal como lo devuelve la base
  (``envelope::text``), sin canonicalizar ni firmar; una versión nueva se sirve en la siguiente
  petición.
- Guarda de alcance (NFR-GOB-30): una zona no asignada al nodo, de otra organización o inexistente
  responde ``node_zone_mismatch``; una asignación retirada deja de servirse en la petición
  siguiente. Las dos capas (la verificación previa de ``node_api`` y la del servicio) tienen su
  prueba: la del servicio se llama sin la verificación previa.
- Zona asignada sin catálogo publicado: ``temporarily_unavailable`` (el nodo reintenta).
- Con el puerto de firma caído sigue respondiendo y nunca llama a ``sign`` (FS-GOB-03).

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from tests.heartbeat_support import HeartbeatStack, heartbeat_stack
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.fleet.application.heartbeat import NodeScopeMismatch

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[HeartbeatStack]:
    with heartbeat_stack(postgres_endpoint, "fleet_zone_catalog") as built:
        yield built


def test_the_current_envelope_is_served_byte_for_byte(stack: HeartbeatStack) -> None:
    site = stack.site(zones=2)
    for zone in site.zones:
        response = stack.get_catalog(site, zone)
        assert response.status_code == 200, response.text
        assert response.content == stack.catalog_text(zone)
        assert response.headers["content-type"].startswith("application/json")
        assert response.headers["cache-control"] == "no-store"
    zone = site.zones[0]
    stack.tick()
    stack.publish_catalog(site.organization_id, site.plant_id, zone, site.cameras[zone], version=2)
    newer = stack.get_catalog(site, zone)
    assert newer.status_code == 200
    assert newer.content == stack.catalog_text(zone)
    assert newer.json()["payload"]["catalog_version"] == 2


def test_a_zone_outside_the_node_is_node_zone_mismatch(stack: HeartbeatStack) -> None:
    site, neighbour, foreign = stack.site(), stack.site(), stack.site()
    unassigned = stack.authz.add_site(plants=1, zones_per_plant=1)
    # Misma planta que el nodo, sin asignar: la que solo protege la comprobación de asignación.
    same_plant = uuid.uuid4()
    stack.authz.add_zone(site.organization_id, site.plant_id, same_plant)
    stack.publish_catalog(site.organization_id, site.plant_id, same_plant, (uuid.uuid4(),))
    ((_, zones),) = unassigned.plants.items()
    for zone in (same_plant, neighbour.zones[0], foreign.zones[0], zones[0], uuid.uuid4()):
        response = stack.get_catalog(site, zone)
        assert response.status_code == 403, (zone, response.text)
        assert response.json()["code"] == "node_zone_mismatch"
        assert stack.catalog_text(neighbour.zones[0]) not in response.content
    malformed = stack.get_catalog(site, "no-es-un-uuid")
    assert malformed.json()["code"] == "node_zone_mismatch"


def test_the_service_itself_refuses_a_zone_outside_the_node(stack: HeartbeatStack) -> None:
    # Sin la verificación previa: la segunda capa también responde node_zone_mismatch.
    site, other = stack.site(), stack.site()
    node = stack.node_scope(site)
    stack.authz.add_zone(site.organization_id, site.plant_id, same := uuid.uuid4())
    stack.publish_catalog(site.organization_id, site.plant_id, same, (uuid.uuid4(),))
    for zone in (same, other.zones[0]):
        with pytest.raises(NodeScopeMismatch):
            stack.run(stack.primary.catalogs.envelope(node, zone))
    assert stack.run(stack.primary.catalogs.envelope(node, site.zones[0])) == stack.catalog_text(
        site.zones[0]
    )


def test_a_retired_assignment_stops_serving_at_the_next_request(stack: HeartbeatStack) -> None:
    site = stack.site(zones=2)
    kept, retired = site.zones
    assert stack.get_catalog(site, retired).status_code == 200
    stack.tick()
    stack.execute(
        "UPDATE identity.zone_node_assignment SET unassigned_at = $3"
        " WHERE node_id = $1 AND zone_id = $2 AND unassigned_at IS NULL",
        site.node_id,
        retired,
        stack.now(),
    )
    stack.tick()
    assert stack.get_catalog(site, retired).json()["code"] == "node_zone_mismatch"
    assert stack.get_catalog(site, kept).status_code == 200


def test_an_assigned_zone_without_catalog_is_temporarily_unavailable(
    stack: HeartbeatStack,
) -> None:
    site = stack.site(catalogs=False)
    response = stack.get_catalog(site, site.zones[0])
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["code"] == "temporarily_unavailable" and body["retryable"] is True


def test_with_signing_down_the_catalog_is_still_served_without_signing(
    stack: HeartbeatStack,
) -> None:
    site = stack.site()
    before = stack.signer.calls
    stack.signer.down = True
    try:
        response = stack.get_catalog(site, site.zones[0])
    finally:
        stack.signer.down = False
    assert response.status_code == 200
    assert response.content == stack.catalog_text(site.zones[0])
    assert stack.signer.calls == before
