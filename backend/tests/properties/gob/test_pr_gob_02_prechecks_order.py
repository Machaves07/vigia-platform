"""PR-GOB-02, parte común: orden fijo de la verificación previa (BR-GOB-84; BR-CTR-32; TASK-206).

Para cualquier petición de nodo que viola **una o varias** de las cuatro verificaciones comunes,
en el orden de BR-GOB-84 —(1) versión, (2) certificado y alcance, (3) tamaño, (4) esquema—, el
código que responde la aplicación real (cadena fija y ruta de prueba interna) es **siempre el de
menor índice**; sin violaciones, la operación recibe la petición.

- Los documentos válidos salen de los generadores del kit de U-01 (``zone_catalog``, ``finding``,
  ``detection_for_review``, ``observability_event``, ``heartbeat``, ``update_result``) y la
  violación de esquema, de su mutación explícita ``mutate_missing_field`` (o un campo no
  declarado). El kit fijado no trae ``mutate_submission`` ni ``violation_combinations``; aquí
  ``violation_combinations`` es el conjunto no vacío de verificaciones que se rompen.
- Versión: sin cabecera, mal formada, menor más nueva (``rejected_newer``) o mayor ajena.
  Certificado y alcance: sin certificado, nodo revocado, planta distinta de la del certificado o,
  en el catálogo, una zona de otra organización. Tamaño: un cuerpo de más de su límite (con o sin
  ``gzip``). Esquema: un campo obligatorio ausente o uno no declarado.
- Un cuerpo grande con la versión inválida responde la versión: el límite de cuerpo de la cadena
  solo **marca** el exceso (``payload_too_large`` se emite en el paso 3).

Semilla registrada por el perfil (``tests/conftest.py``).
"""

from __future__ import annotations

import gzip
import json
import uuid
from typing import Any

from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from vigia_contracts.conformance.generators import (
    detection_for_review,
    finding,
    heartbeat,
    mutate_missing_field,
    observability_event,
    update_result,
    zone_catalog,
)
from vigia_contracts.models.api import parse_rejection_response

from tests.node_api_support import VERSION, NodeWorld, node_world, zone_of
from vigia_platform.node_api.declarations import spec_of
from vigia_platform.shared.api.declarations import NodeRoute

VERSION_STEP, CERTIFICATE_STEP, SIZE_STEP, SCHEMA_STEP = 1, 2, 3, 4
STEPS = (VERSION_STEP, CERTIFICATE_STEP, SIZE_STEP, SCHEMA_STEP)

BODY_ROUTES = (
    NodeRoute.FINDING,
    NodeRoute.DETECTION_REVIEW,
    NodeRoute.OBSERVABILITY_EVENT,
    NodeRoute.HEARTBEAT,
    NodeRoute.UPDATE_RESULT,
)
ROUTES = (*BODY_ROUTES, NodeRoute.ZONE_CATALOG)

EXPECTED_CODES = {
    VERSION_STEP: {"contract_version_unsupported", "contract_version_retired", "schema_invalid"},
    CERTIFICATE_STEP: {"node_not_enrolled", "node_revoked", "node_zone_mismatch"},
    SIZE_STEP: {"payload_too_large"},
    SCHEMA_STEP: {"schema_invalid"},
}
"""Los códigos que puede dar cada verificación (``schema_invalid`` del paso 1: cabecera mal
formada, con ``field`` = ``X-Vigia-Contract-Version``)."""

_MAJOR, _MINOR = (int(part) for part in VERSION.split("-")[0].split("+")[0].split(".")[:2])


@st.composite
def documents(draw: st.DrawFn, route: NodeRoute) -> dict[str, Any] | None:
    """Un documento válido de la operación, de los generadores del kit (``None`` sin cuerpo)."""
    if route is NodeRoute.ZONE_CATALOG:
        return None
    if route is NodeRoute.UPDATE_RESULT:
        document: dict[str, Any] = draw(update_result())
        return document
    catalog = draw(zone_catalog())
    strategy = {
        NodeRoute.FINDING: finding(catalog),
        NodeRoute.DETECTION_REVIEW: detection_for_review(catalog),
        NodeRoute.OBSERVABILITY_EVENT: observability_event(catalog),
        NodeRoute.HEARTBEAT: heartbeat(catalog),
    }[route]
    result: dict[str, Any] = draw(strategy)
    return result


def violation_combinations(route: NodeRoute) -> st.SearchStrategy[frozenset[int]]:
    """Conjuntos no vacíos de verificaciones violadas (sin esquema donde no hay cuerpo)."""
    steps = STEPS if route in BODY_ROUTES else STEPS[:3]
    return st.sets(st.sampled_from(steps), min_size=1).map(frozenset)


@st.composite
def requests(draw: st.DrawFn) -> tuple[NodeRoute, frozenset[int], dict[str, Any]]:
    route = draw(st.sampled_from(ROUTES))
    violated = draw(st.one_of(st.just(frozenset[int]()), violation_combinations(route)))
    plan: dict[str, Any] = {
        "document": draw(documents(route)),
        "version": draw(
            st.sampled_from(
                [
                    None,
                    "uno.dos",
                    f"{_MAJOR}.{_MINOR + 1}.0",
                    f"{_MAJOR + 1}.0.0",
                    f"{_MAJOR}.{_MINOR}",
                ]
            )
        ),
        "certificate": draw(
            st.sampled_from(
                ["none", "revoked", "other_plant"]
                + (["foreign_zone"] if route is NodeRoute.ZONE_CATALOG else [])
            )
        ),
        "size": draw(st.sampled_from(["identity", "gzip"])),
        "schema": draw(st.sampled_from(["missing", "extra"])),
        "padding": draw(st.integers(1, 2_048)),
    }
    if SCHEMA_STEP in violated and plan["document"] is not None:
        if plan["schema"] == "missing":
            model = spec_of(route).parser(json.dumps(plan["document"]).encode())  # type: ignore[misc]
            mutation = draw(mutate_missing_field(model))
            plan["document"] = mutation.document
        else:
            plan["document"] = {**plan["document"], "campo_no_declarado": 1}
    return route, violated, plan


def _send(
    nodes: NodeWorld, route: NodeRoute, violated: frozenset[int], plan: dict[str, Any]
) -> Any:
    zone = zone_of(nodes.a)
    if CERTIFICATE_STEP in violated:
        if plan["certificate"] == "revoked":
            nodes.a.node_status, nodes.a.revoked_at = "revoked", nodes.now
        elif plan["certificate"] == "other_plant":
            nodes.a.plant_id = uuid.uuid4()
        elif plan["certificate"] == "foreign_zone":
            zone = zone_of(nodes.b)
    node = None if CERTIFICATE_STEP in violated and plan["certificate"] == "none" else nodes.a
    version = plan["version"] if VERSION_STEP in violated else VERSION
    headers = nodes.headers(node, version=version)
    document = plan["document"]
    body = b"" if document is None else json.dumps(document, separators=(",", ":")).encode()
    limit = route.max_body_bytes
    if SIZE_STEP in violated:
        if limit == 0:
            body = b"{}"
        elif plan["size"] == "gzip":
            body = gzip.compress(body + b" " * (limit + plan["padding"]))
            headers["Content-Encoding"] = "gzip"
        else:
            body = body + b" " * (limit - len(body) + plan["padding"])
    path = route.path.replace("{zone_id}", str(zone))
    with TestClient(nodes.app) as client:
        return client.request(route.method, path, headers=headers, content=body)


@settings(suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(request=requests())
def test_pr_gob_02_the_code_is_the_one_of_the_lowest_violated_check(
    request: tuple[NodeRoute, frozenset[int], dict[str, Any]],
) -> None:
    route, violated, plan = request
    nodes = node_world()
    response = _send(nodes, route, violated, plan)
    if not violated:
        assert response.status_code == 200, response.text
        assert len(nodes.probe.seen) == 1
        return
    first = min(violated)
    rejection = parse_rejection_response(response.content)
    assert rejection.code.value in EXPECTED_CODES[first], (route, sorted(violated), response.text)
    if first == VERSION_STEP:
        assert rejection.field == "X-Vigia-Contract-Version"
        if rejection.compatibility_result is not None:
            assert rejection.code.value == "contract_version_unsupported" or (
                rejection.code.value == "contract_version_retired"
            )
    elif first == SCHEMA_STEP:
        assert rejection.field != "X-Vigia-Contract-Version"
    assert nodes.probe.seen == [], "la operación no se ejecuta"


def test_a_large_body_with_an_invalid_version_answers_the_version() -> None:
    nodes = node_world()
    route = NodeRoute.FINDING
    body = b"{" + b" " * (route.max_body_bytes + 10) + b"}"
    with TestClient(nodes.app) as client:
        response = client.post(
            route.path, headers=nodes.headers(nodes.a, version=None), content=body
        )
    assert parse_rejection_response(response.content).code.value == "contract_version_unsupported"
