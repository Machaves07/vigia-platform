"""SUP-U05-02: el detalle degrada declarando y falla cerrado sin compuerta (TASK-342;
PAT-LAZ-RES-04, PAT-LAZ-SEG-09; BR-LAZ-12 y 152; NFR-LAZ-29).

Sobre PostgreSQL 16 real como ``vigia_app`` y la aplicación real (``detail_stack``), con un doble
por puerto (``DetailPorts``: cada operación de U-03 y ``estado_en`` caída a voluntad) y el almacén
de evidencias caído (``DetailEvidenceStore.down``):

- **Compuerta sin respuesta**: 503 ``temporarily_unavailable`` con ``loop_gate_state_unavailable``,
  ``Retry-After`` y un cuerpo que es **exactamente** el error (ningún campo del hallazgo, igualdad
  exacta); no se lee el expediente ni se pregunta a ningún otro puerto.
- **Compuerta no aprobada en el instante del hecho**: el hallazgo no existe (``not_found``, igual
  que un identificador inexistente).
- **Texto del estándar no disponible** (puerto caído o versión sin declarar): 200 con la cita
  ``(standard_id, version)`` y la sección degradada ``standard_text_unavailable``.
- **Almacén caído**: 200 con la referencia de cada clip (los metadatos del expediente) sin URL y
  el aviso ``evidence_url_unavailable``.
- **Un clip ilegible** (su objeto no está) de los dos de la cámara (``start`` y ``end``): solo
  ese queda sin URL; el otro la tiene y el aviso se declara (BR-LAZ-08).
- **Nodos o cobertura sin respuesta**: ``node_state_unavailable`` y ``coverage_unavailable``; la
  zona nunca se presenta como observada.
- Con **todos** los puertos opcionales caídos a la vez el hallazgo **sigue** saliendo: en ningún
  caso se omite.

Solo datos generados.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any, Final

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import IngestedSubject
from tests.loop_detail_support import DetailStack, Who, detail_stack
from vigia_platform.shared.api.errors import DEFAULT_RETRY_AFTER_SECONDS
from vigia_platform.shared.api.labels import PlatformLabels

pytestmark = pytest.mark.integration

_OPTIONAL: Final = ("standard_version", "nodes_by_zone", "estado_en")


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[DetailStack]:
    with detail_stack(postgres_endpoint, "loop_detail_degradation") as built:
        built.declare_standard(built.world.a)
        yield built


@pytest.fixture(autouse=True)
def _healthy(stack: DetailStack) -> Iterator[None]:
    stack.ports.reset()
    stack.evidence_store.down = False
    stack.evidence_store.unreadable.clear()
    yield
    stack.ports.reset()
    stack.evidence_store.down = False
    stack.evidence_store.unreadable.clear()


@pytest.fixture(scope="module")
def coordinator(stack: DetailStack) -> Who:
    return stack.member(stack.world.a)


@pytest.fixture(scope="module")
def finding(stack: DetailStack) -> IngestedSubject:
    return stack.received()


def _detail(stack: DetailStack, who: Who, finding: IngestedSubject) -> dict[str, Any]:
    response = stack.detail(who, stack.finding_state_id(finding))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    assert body["finding_state_id"] == str(stack.finding_state_id(finding))
    return body


def _codes(body: dict[str, Any]) -> list[str]:
    return [notice["code"] for notice in body["notices"]]


def _reads(stack: DetailStack, finding: IngestedSubject) -> list[dict[str, Any]]:
    return [
        row
        for row in stack.audit_reads(finding.organization)
        if row["resource_id"] == finding.record_id
    ]


def test_without_a_readable_gate_the_finding_is_not_shown_503_with_retry_after(
    stack: DetailStack, coordinator: Who, finding: IngestedSubject
) -> None:
    stack.ports.down.add("state_at")
    reads = len(_reads(stack, finding))
    response = stack.detail(coordinator, stack.finding_state_id(finding))
    assert response.status_code == 503
    body = response.json()
    assert body == {
        "code": "temporarily_unavailable",
        "detail_code": "loop_gate_state_unavailable",
        "message_es": PlatformLabels.load().label("api_error_code", "temporarily_unavailable"),
        "correlation_id": body["correlation_id"],
        "retry_after_seconds": DEFAULT_RETRY_AFTER_SECONDS,
    }
    uuid.UUID(body["correlation_id"])
    assert response.headers["Retry-After"] == str(DEFAULT_RETRY_AFTER_SECONDS)
    assert response.headers["Cache-Control"] == "no-store"
    # Fallo cerrado antes de leer nada: ni el expediente ni otro puerto.
    assert dict(stack.ports.calls) == {"state_at": 1}
    assert len(_reads(stack, finding)) == reads


@pytest.mark.parametrize("status", [None, "revoked", "pending"])
def test_a_gate_not_approved_at_the_instant_is_like_a_finding_that_does_not_exist(
    stack: DetailStack, coordinator: Who, finding: IngestedSubject, status: str | None
) -> None:
    stack.ports.gate_status = status
    response = stack.detail(coordinator, stack.finding_state_id(finding))
    missing = stack.detail(coordinator, uuid.uuid4())
    assert response.status_code == missing.status_code == 404
    strip = {"correlation_id"}
    assert {k: v for k, v in response.json().items() if k not in strip} == {
        k: v for k, v in missing.json().items() if k not in strip
    }


def test_without_the_standard_text_the_citation_and_the_degraded_section(
    stack: DetailStack, coordinator: Who, finding: IngestedSubject
) -> None:
    stack.ports.down.add("standard_version")
    body = _detail(stack, coordinator, finding)
    site = finding.organization.site
    assert body["standard"] == {
        "standard_id": str(site.standard_id),
        "version": 1,
        "title_es": None,
        "declared_text": None,
        "declared_text_sha256": None,
        "effective_from": None,
        "superseded_since": None,
        "superseded_in_catalog_version": None,
    }
    assert _codes(body) == ["standard_text_unavailable"]


def test_a_cited_version_the_catalog_does_not_declare_also_degrades(
    stack: DetailStack, coordinator: Who
) -> None:
    other = stack.world.b
    finding = stack.received(other)
    who = stack.member(other)
    body = _detail(stack, who, finding)  # B no tiene la tabla de estándares sembrada
    assert body["standard"]["standard_id"] == str(other.site.standard_id)
    assert body["standard"]["version"] == 1
    assert body["standard"]["declared_text"] is None
    assert _codes(body) == ["standard_text_unavailable"]


def test_with_the_store_down_the_clip_reference_and_the_notice(
    stack: DetailStack, coordinator: Who, finding: IngestedSubject
) -> None:
    stack.evidence_store.down = True
    body = _detail(stack, coordinator, finding)
    (record,) = body["records"]
    evidences = stack.world.stack.evidence(finding.record_id)
    assert evidences
    assert sorted(clip["clip_id"] for clip in record["clips"]) == sorted(
        str(e["clip_id"]) for e in evidences
    )
    assert all(clip["read_url"] is None for clip in record["clips"])
    assert [clip["clip_id"] for clip in record["clips"]] == [
        clip["clip_id"] for camera in finding.document["cameras"] for clip in camera["clips"]
    ]
    assert _codes(body) == ["evidence_url_unavailable"]


def test_with_the_store_up_each_clip_has_its_read_url(
    stack: DetailStack, coordinator: Who, finding: IngestedSubject
) -> None:
    body = _detail(stack, coordinator, finding)
    (record,) = body["records"]
    assert record["clips"] and all(clip["read_url"] for clip in record["clips"])
    assert "evidence_url_unavailable" not in _codes(body)


@pytest.mark.parametrize("broken", [0, 1])
def test_one_unreadable_clip_does_not_take_the_read_url_of_the_others(
    stack: DetailStack, coordinator: Who, broken: int
) -> None:
    finding = stack.received_with_two_clips()
    clips = finding.document["cameras"][0]["clips"]
    stack.evidence_store.unreadable.add(clips[broken]["storage_key"])
    stack.evidence_store.presigned.clear()
    body = _detail(stack, coordinator, finding)
    (record,) = body["records"]
    assert [clip["clip_id"] for clip in record["clips"]] == [clip["clip_id"] for clip in clips]
    for index, clip in enumerate(record["clips"]):
        assert (clip["read_url"] is None) is (index == broken), (index, clip)
    # BR-LAZ-08: una URL por cada clip legible; el ilegible no corta los demás.
    assert sorted(stack.evidence_store.presigned) == sorted(
        clip["storage_key"] for index, clip in enumerate(clips) if index != broken
    )
    assert _codes(body) == ["evidence_url_unavailable"]


def test_without_nodes_or_coverage_the_sections_are_declared_never_observed(
    stack: DetailStack, coordinator: Who, finding: IngestedSubject
) -> None:
    stack.ports.down.update({"nodes_by_zone", "estado_en"})
    body = _detail(stack, coordinator, finding)
    assert body["nodes"] is None
    assert body["coverage_at_event"] is None
    assert _codes(body) == ["coverage_unavailable", "node_state_unavailable"]


def test_with_every_optional_port_and_the_store_down_the_finding_is_still_shown(
    stack: DetailStack, coordinator: Who, finding: IngestedSubject
) -> None:
    stack.ports.down.update(_OPTIONAL)
    stack.evidence_store.down = True
    body = _detail(stack, coordinator, finding)
    assert body["gate_at_event"]["status"] == "approved"
    assert body["records"][0]["record_id"] == str(finding.record_id)
    assert _codes(body) == [
        "standard_text_unavailable",
        "coverage_unavailable",
        "node_state_unavailable",
        "evidence_url_unavailable",
    ]
    # Cada puerto se preguntó una vez, también los que no respondieron.
    assert dict(stack.ports.calls) == {
        "state_at": 1,
        "standard_version": 1,
        "nodes_by_zone": 1,
        "estado_en": 1,
    }
