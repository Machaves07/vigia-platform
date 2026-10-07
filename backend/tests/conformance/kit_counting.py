"""La suite del kit con un transporte que cuenta lo que recibió (NFR-GOB-54; TASK-230).

``run_checks`` de U-01 contra la plataforma por URL con un ``PlatformUrl`` cuyo transporte
(``transport_factory``, el gancho del kit) anota **lo que el kit recibió** de cada petición a
``/api/nodes``: la operación del esqueleto y el resultado leído con el ``Reply`` del kit
(``status`` del ``Receipt``, ``rejection_code`` o ``not_found``). Esos conteos son las métricas
del ``SuiteReport`` (``metrics``, como las de la plataforma simulada) y salen en su informe.

Corre en un **proceso aparte** (``python -m tests.conformance.kit_counting``): la ``vigia-api``
de la prueba de métricas está en el proceso de pytest (para leer su exportador en memoria) y el
kit, en el mismo proceso, competiría con ella por el GIL y duplicaría la duración. El proceso
escribe en ``--out`` el informe (``-informe.json`` y ``-informe.txt``) y ``-kit.json`` con
``passed`` y los conteos de ``SuiteReport.metrics``; ``--group`` (repetible) limita la suite a
esos grupos, como la orden ``vigia-conformance run``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import httpx
from vigia_contracts.conformance.checks import PlatformUrl, run_checks
from vigia_contracts.conformance.checks._platform import Reply
from vigia_contracts.conformance.checks._target import tls_context
from vigia_contracts.conformance.cli import load_provision
from vigia_contracts.conformance.stub_platform.metrics import LatencySummary, MetricsSnapshot
from vigia_contracts.server_skeleton import OPERATIONS

from tests.conformance.platform_target import ENROLLMENT_PATH, INGEST_PATH
from vigia_platform.shared.api.declarations import NODE_PREFIX, NodeRoute

UNMATCHED: Final = "unmatched"
"""Ruta de ``node_requests_total`` sin operación (``node_api.observability``)."""
NO_RESPONSE: Final = "sin_respuesta"
KIT_TIMEOUT_SECONDS: Final = 30.0
"""El tope de ``checks._target._UrlEndpoint.client`` del kit (``timeout=30.0``)."""


def _template(path: str) -> re.Pattern[str]:
    return re.compile("^" + re.sub(r"\\\{[a-z_]+\\\}", "[^/]+", re.escape(path)) + "$")


MOUNTED: Final = {route.operation_id: OPERATIONS[route.operation_id] for route in NodeRoute}
"""Las operaciones que la plataforma monta: ``conformance-profile`` no (A-51), y su petición
sale en la métrica como ruta ``unmatched``."""
_TEMPLATES: Final = tuple(
    (operation.method, _template(NODE_PREFIX + operation.path), operation_id)
    for operation_id, operation in MOUNTED.items()
)


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


def counting_target(nodes_url: str, app_url: str, provision_file: Path) -> CountingPlatformUrl:
    provision = load_provision(provision_file)
    outcomes: Counter[tuple[str, str]] = Counter()

    def transport(node_id: str | None) -> httpx.AsyncBaseTransport:
        certificate = provision.certificates.get(node_id.lower()) if node_id else None
        inner = httpx.AsyncHTTPTransport(verify=tls_context(provision.verify, certificate))
        return CountingTransport(inner, outcomes)

    platform = CountingPlatformUrl(
        nodes_url + INGEST_PATH,
        app_url + ENROLLMENT_PATH,
        provision.primary,
        second=provision.second,
        certificates=provision.certificates,
        verify=provision.verify,
        flood=provision.flood,
        transport_factory=transport,
    )
    platform.outcomes = outcomes
    return platform


def main(argv: Sequence[str] | None = None) -> int:
    """Corre la suite con el perfil y la semilla dados y escribe el informe y sus conteos."""
    parser = argparse.ArgumentParser(prog="kit_counting")
    parser.add_argument("--nodes-url", required=True)
    parser.add_argument("--app-url", required=True)
    parser.add_argument("--provision", type=Path, required=True)
    parser.add_argument("--profile", choices=("ci", "nightly"), required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--group", action="append", default=None, help="limita a ese grupo")
    parser.add_argument("--out", type=Path, required=True, help="prefijo de los archivos")
    arguments = parser.parse_args(argv)
    target = counting_target(arguments.nodes_url, arguments.app_url, arguments.provision)
    report = run_checks(target, arguments.profile, arguments.seed, groups=arguments.group)
    out: Path = arguments.out
    out.with_name(out.name + "-informe.txt").write_text(report.render_es(), encoding="utf-8")
    out.with_name(out.name + "-informe.json").write_bytes(report.to_json_bytes())
    metrics = report.metrics.outcomes if report.metrics is not None else None
    document = {
        "passed": report.passed,
        "metrics": None if metrics is None else [[*key, count] for key, count in metrics.items()],
        "metrics_are_the_transport_counts": metrics is not None
        and dict(metrics) == dict(target.outcomes),
    }
    out.with_name(out.name + "-kit.json").write_text(json.dumps(document) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
