"""Métricas de la plataforma frente al informe del kit (NFR-GOB-54, NFR-CTR-42; TASK-230).

La suite del kit de U-01 corre contra ``vigia-api`` en el proceso de la prueba (raíz de
producción, ``MeterProvider`` con exportador en memoria) detrás de su balanceador local, con
``run_checks`` y un ``PlatformUrl`` cuyo transporte (``transport_factory``, el gancho del kit)
anota **lo que el kit recibió** de cada petición a ``/api/nodes``: la operación del esqueleto y
el resultado leído con el ``Reply`` del kit (``status`` del ``Receipt``, ``rejection_code`` o
``not_found``). Esos conteos son las métricas del ``SuiteReport`` (``metrics``, como las de la
plataforma simulada) y se imprimen en su informe; ``ConformanceReport`` (el JSON del contrato) no
lleva conteos para un objetivo por URL.

Se comparan con ``node_requests_total`` de la plataforma (incremento durante la ejecución) por
operación y resultado: aceptados por tipo de registro, ``accepted_duplicate`` y rechazados **por
``rejection_code``**. Cualquier diferencia falla y la nombra. La comparación y el informe quedan en
el directorio de informes (artefacto ``informe-conformidad`` de ``nightly.yml``).

Las pruebas de ``compare`` (sin contenedores) alteran a propósito un contador y comprueban que la
comparación falla nombrándolo.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import httpx
import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint
from vigia_contracts.conformance.checks import PlatformUrl, run_checks
from vigia_contracts.conformance.checks._platform import Reply
from vigia_contracts.conformance.checks._target import tls_context
from vigia_contracts.conformance.cli import load_provision
from vigia_contracts.conformance.stub_platform.metrics import LatencySummary, MetricsSnapshot
from vigia_contracts.models.receipt import Receipt
from vigia_contracts.server_skeleton import OPERATIONS

from tests.conformance.conftest import (
    PlatformTarget,
    conformance_profile,
    conformance_seed,
)
from tests.conformance.platform_target import ENROLLMENT_PATH, INGEST_PATH
from vigia_platform.shared.api.declarations import NODE_PREFIX, NodeRoute

METRIC: Final = "node_requests_total"
UNMATCHED: Final = "unmatched"
"""Ruta de ``node_requests_total`` sin operación (``node_api.observability``)."""

type Outcomes = Mapping[tuple[str, str], int]
"""``(operation_id, resultado)`` → peticiones; resultado: ``accepted``,
``accepted_duplicate``, ``not_found`` o el ``rejection_code``."""


def _template(path: str) -> re.Pattern[str]:
    return re.compile("^" + re.sub(r"\\\{[a-z_]+\\\}", "[^/]+", re.escape(path)) + "$")


_MOUNTED: Final = {route.operation_id: OPERATIONS[route.operation_id] for route in NodeRoute}
"""Las operaciones que la plataforma monta: ``conformance-profile`` no (A-51), y su petición
sale en la métrica como ruta ``unmatched``."""
_TEMPLATES: Final = tuple(
    (operation.method, _template(NODE_PREFIX + operation.path), operation_id)
    for operation_id, operation in _MOUNTED.items()
)
_BY_PATH: Final = {NODE_PREFIX + op.path: op_id for op_id, op in _MOUNTED.items()}
RECEIPT_OPERATIONS: Final = frozenset(
    operation_id
    for operation_id, operation in OPERATIONS.items()
    if any(
        response.status_code == 200 and response.model is Receipt
        for response in operation.responses
    )
)
"""Las operaciones cuya respuesta correcta es un ``Receipt``: solo en ellas ve el kit si una
aceptación es ``accepted_duplicate`` (en el latido, la concesión o el catálogo, un duplicado
recibe la misma respuesta que el original). En las demás se comparan las aceptaciones juntas."""
NO_RESPONSE: Final = "sin_respuesta"
KIT_TIMEOUT_SECONDS: Final = 30.0
"""El tope de ``checks._target._UrlEndpoint.client`` del kit (``timeout=30.0``)."""


def comparable(outcomes: Outcomes) -> Counter[tuple[str, str]]:
    """``outcomes`` con ``accepted_duplicate`` como ``accepted`` fuera de ``RECEIPT_OPERATIONS``."""
    folded: Counter[tuple[str, str]] = Counter()
    for (operation_id, result), count in outcomes.items():
        if result == "accepted_duplicate" and operation_id not in RECEIPT_OPERATIONS:
            result = "accepted"
        folded[(operation_id, result)] += count
    return folded


def operation_of(method: str, path: str) -> str:
    """La operación montada de ``method path`` (``unmatched`` si ninguna)."""
    for expected, pattern, operation_id in _TEMPLATES:
        if method == expected and pattern.match(path):
            return operation_id
    return UNMATCHED


def kit_outcome(operation_id: str, status: int, content: bytes) -> str:
    """El resultado de una respuesta como lo lee el kit (``Reply`` de ``checks._platform``)."""
    reply = Reply(operation_id, status, content, httpx.Headers())
    if reply.receipt is not None:
        return str(reply.receipt["status"])
    if status == 200:
        return "accepted"
    if reply.code is not None:
        return reply.code
    if status == 404:
        return "not_found"
    return f"status_{status}"


# --- Lado del kit -------------------------------------------------------------------------------


class CountingTransport(httpx.AsyncBaseTransport):
    """Transporte real (TLS con el certificado del nodo) que anota cada respuesta de la ingesta."""

    def __init__(self, inner: httpx.AsyncBaseTransport, outcomes: Counter[tuple[str, str]]) -> None:
        self._inner = inner
        self._outcomes = outcomes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if not (path.startswith(NODE_PREFIX + "/") or path == NODE_PREFIX):
            return await self._inner.handle_async_request(request)
        operation_id = operation_of(request.method, path)
        # El cliente que el kit crea con ``transport_factory`` no fija tope (el de httpx, 5 s); el
        # de su objetivo por URL, sí: 30 s. Se usa el mismo que la orden ``vigia-conformance``.
        request.extensions["timeout"] = httpx.Timeout(KIT_TIMEOUT_SECONDS).as_dict()
        try:
            response = await self._inner.handle_async_request(request)
            content = await response.aread()
        except httpx.HTTPError as error:
            # El kit no recibió respuesta: queda nombrado en la comparación, nunca se pierde.
            self._outcomes[(operation_id, f"{NO_RESPONSE}:{type(error).__name__}")] += 1
            raise
        self._outcomes[
            (operation_id, kit_outcome(operation_id, response.status_code, content))
        ] += 1
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


class CountingPlatformUrl(PlatformUrl):
    """``PlatformUrl`` cuyo informe lleva lo que el kit recibió (``SuiteReport.metrics``)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.outcomes: Counter[tuple[str, str]] = Counter()

    def metrics(self) -> MetricsSnapshot:
        return MetricsSnapshot(
            outcomes=MappingProxyType(dict(self.outcomes)),
            objects_stored=0,
            validation=LatencySummary(0, 0.0, 0.0),
        )


def counting_target(target: PlatformTarget) -> CountingPlatformUrl:
    provision = load_provision(target.provisioned.provision_file)
    outcomes: Counter[tuple[str, str]] = Counter()

    def transport(node_id: str | None) -> httpx.AsyncBaseTransport:
        certificate = provision.certificates.get(node_id.lower()) if node_id else None
        inner = httpx.AsyncHTTPTransport(verify=tls_context(provision.verify, certificate))
        return CountingTransport(inner, outcomes)

    platform = CountingPlatformUrl(
        target.nodes.url + INGEST_PATH,
        target.app.url + ENROLLMENT_PATH,
        provision.primary,
        second=provision.second,
        certificates=provision.certificates,
        verify=provision.verify,
        flood=provision.flood,
        transport_factory=transport,
    )
    platform.outcomes = outcomes
    return platform


# --- Lado de la plataforma ----------------------------------------------------------------------


def platform_outcomes(reader: InMemoryMetricReader) -> Counter[tuple[str, str]]:
    """``node_requests_total`` acumulado por (operación, resultado o ``rejection_code``)."""
    found: Counter[tuple[str, str]] = Counter()
    data = reader.get_metrics_data()
    if data is None:
        return found
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name != METRIC:
                    continue
                for point in metric.data.data_points:
                    if not isinstance(point, NumberDataPoint):
                        continue
                    attributes = dict(point.attributes or {})
                    operation_id = _BY_PATH.get(str(attributes.get("route")), UNMATCHED)
                    result = str(attributes.get("result"))
                    if result == "rejected":
                        result = str(attributes.get("rejection_code"))
                    found[(operation_id, result)] += int(point.value)
    return found


# --- Comparación ----------------------------------------------------------------------------------


def compare(platform: Outcomes, kit: Outcomes) -> list[str]:
    """Cada (operación, resultado) en que difieren los conteos, nombrado; vacío si coinciden."""
    return [
        f"{operation} {result}: plataforma {platform.get((operation, result), 0)},"
        f" informe del kit {kit.get((operation, result), 0)}"
        for operation, result in sorted(set(platform) | set(kit))
        if platform.get((operation, result), 0) != kit.get((operation, result), 0)
    ]


def _document(outcomes: Outcomes) -> dict[str, dict[str, int]]:
    grouped: dict[str, dict[str, int]] = {}
    for (operation, result), count in sorted(outcomes.items()):
        grouped.setdefault(operation, {})[result] = count
    return grouped


def test_compare_names_every_altered_counter() -> None:
    kit = {("post_finding", "accepted"): 3, ("post_finding", "schema_invalid"): 2}
    assert compare(dict(kit), kit) == []
    altered = {**kit, ("post_finding", "schema_invalid"): 1}
    assert compare(altered, kit) == ["post_finding schema_invalid: plataforma 1, informe del kit 2"]
    missing = {("post_finding", "accepted"): 3}
    assert compare(missing, kit) == ["post_finding schema_invalid: plataforma 0, informe del kit 2"]
    extra = {**kit, ("post_heartbeat", "rate_limited"): 1}
    assert compare(extra, kit) == ["post_heartbeat rate_limited: plataforma 1, informe del kit 0"]


def test_duplicates_are_only_told_apart_where_the_kit_sees_a_receipt() -> None:
    assert {
        "post_finding",
        "post_detection_review",
        "post_observability_event",
        "post_update_result",
    } == RECEIPT_OPERATIONS
    outcomes = {
        ("post_heartbeat", "accepted"): 2,
        ("post_heartbeat", "accepted_duplicate"): 1,
        ("post_finding", "accepted_duplicate"): 1,
    }
    assert comparable(outcomes) == {
        ("post_heartbeat", "accepted"): 3,
        ("post_finding", "accepted_duplicate"): 1,
    }


def test_operations_and_outcomes_are_read_like_the_kit() -> None:
    assert operation_of("POST", "/api/nodes/findings") == "post_finding"
    assert operation_of("GET", "/api/nodes/zones/0192f0c4-0000-7000-8000-000000000001/catalog") == (
        "get_zone_catalog"
    )
    assert operation_of("POST", "/api/nodes/clip-uploads/x/confirmation") == (
        "post_clip_upload_confirmation"
    )
    assert operation_of("GET", "/api/nodes/findings") == UNMATCHED
    assert operation_of("GET", "/api/nodes/conformance-profile") == UNMATCHED  # A-51
    assert kit_outcome("post_heartbeat", 404, b"") == "not_found"
    rejection = json.dumps(
        {
            "code": "node_zone_mismatch",
            "retryable": False,
            "message_es": "La zona no es del nodo.",
            "contract_version": "1.0.0",
        }
    ).encode()
    assert kit_outcome("post_finding", 403, rejection) == "node_zone_mismatch"


@pytest.mark.integration
def test_platform_metrics_match_the_kit_report(
    platform_target: PlatformTarget, report_directory: Path
) -> None:
    profile, seed = conformance_profile(), conformance_seed()
    reader = platform_target.api.reader
    before = platform_outcomes(reader)
    target = counting_target(platform_target)
    report = run_checks(target, profile, seed)
    after = platform_outcomes(reader)
    platform = comparable(after - before)
    kit = comparable(target.outcomes)
    differences = compare(platform, kit)
    comparison = {
        "profile": profile,
        "seed": seed,
        "passed": report.passed,
        "platform": _document(platform),
        "kit_report": _document(kit),
        "differences": differences,
    }
    name = f"metricas-{profile}-{seed}"
    (report_directory / f"{name}.json").write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (report_directory / f"{name}-informe.txt").write_text(report.render_es(), encoding="utf-8")
    (report_directory / f"{name}-informe.json").write_bytes(report.to_json_bytes())
    print(report.render_es())

    assert sum(kit.values()) > 0
    assert report.metrics is not None and dict(report.metrics.outcomes) == dict(target.outcomes)
    assert {result for _, result in kit} >= {"accepted", "accepted_duplicate"}
    assert differences == [], "\n".join(differences)
    assert report.passed, report.render_es()
