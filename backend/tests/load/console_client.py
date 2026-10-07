"""Cliente sintético de consola (NFR-GOB-19, 03 y 50; LC-GOB-22; TASK-231).

Un usuario de consola con **sesión real** (cookie de sesión y concesión del instalador del
proveedor, por el balanceador ``app.``) que, en un hilo y en paralelo a la carga de los nodos,
recorre una y otra vez las rutas de NFR-GOB-03 y mide cada petición de extremo a extremo:

=========================================== ============
Ruta                                         p95 máximo
=========================================== ============
``GET /fleet/nodes`` (página de 100 nodos)  500 ms
``GET /fleet/nodes/{node_id}``              300 ms
``GET /zones/{zone_id}/walk-tests/current`` 200 ms
catálogo vigente, historial, compuertas y   200 ms
transparencia de la zona
acta estructurada                           300 ms
``POST /documents``                         300 ms
=========================================== ============

``evaluate`` calcula el p95 por ruta en las ventanas pedidas (las reconexiones) y el veredicto
**nombra cada ruta** que incumple su p95, que respondió con un estado fuera de 2xx o que no
reunió ``MIN_SAMPLES`` muestras. La ruta del acta estructurada (``GET /commissioning-records/
{record_id}``, interfaces §3.3) aún no está en ``openapi/app.yaml``: el cliente la declara, la
informa como ``unpublished`` y la medirá en cuanto exista (``published_routes``).

Las respuestas no se guardan: la de ``POST /documents`` lleva una URL prefirmada (PR-GOB-31).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import random
import re
import ssl
import threading
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import httpx
import yaml

from tests.conformance.platform_target import BACKEND, WALL
from tests.load.provision import ConsoleSession
from tests.load.report import percentile
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME

__all__ = [
    "CONSOLE_ROUTES",
    "MIN_SAMPLES",
    "ConsoleClient",
    "ConsoleRoute",
    "ConsoleTargets",
    "ConsoleVerdict",
    "Sample",
    "evaluate",
    "published_routes",
]

OPENAPI: Final = BACKEND / "openapi" / "app.yaml"
MIN_SAMPLES: Final = 20
"""Muestras mínimas por ruta en la ventana evaluada: con menos, el p95 no dice nada."""
HTTP_SECONDS: Final = 60.0
"""Tope de una petición (nunca decide el veredicto: lo decide el p95)."""
CONCESSION_HEADER: Final = "X-Vigia-Concession"
DOCUMENT_BYTES: Final = 4_096


@dataclass(frozen=True)
class ConsoleRoute:
    method: str
    template: str
    p95_ms: float
    requirement: str = "NFR-GOB-03"

    @property
    def name(self) -> str:
        return f"{self.method} {self.template}"


CONSOLE_ROUTES: Final[tuple[ConsoleRoute, ...]] = (
    ConsoleRoute("GET", "/fleet/nodes", 500),
    ConsoleRoute("GET", "/fleet/nodes/{node_id}", 300),
    ConsoleRoute("GET", "/zones/{zone_id}/walk-tests/current", 200),
    ConsoleRoute("GET", "/zones/{zone_id}/catalog", 200),
    ConsoleRoute("GET", "/zones/{zone_id}/catalog/versions", 200),
    ConsoleRoute("GET", "/zones/{zone_id}/gates", 200),
    ConsoleRoute("GET", "/zones/{zone_id}/transparency", 200),
    ConsoleRoute("GET", "/commissioning-records/{record_id}", 300),
    ConsoleRoute("POST", "/documents", 300),
)


def published_routes(openapi: Path = OPENAPI) -> frozenset[str]:
    """``MÉTODO /ruta`` de cada operación de ``openapi/app.yaml``."""
    document: Any = yaml.safe_load(openapi.read_text(encoding="utf-8"))
    return frozenset(
        f"{method.upper()} {path}"
        for path, operations in document["paths"].items()
        for method in operations
        if method in {"get", "post", "put", "patch", "delete"}
    )


@dataclass(frozen=True)
class Sample:
    route: str
    at: dt.datetime
    elapsed_ms: float
    status: int


@dataclass(frozen=True)
class ConsoleTargets:
    """Sobre qué recursos pregunta la consola: nodos, zonas y plantas de la flota."""

    node_ids: tuple[uuid.UUID, ...]
    zone_ids: tuple[uuid.UUID, ...]
    plant_ids: tuple[uuid.UUID, ...]


@dataclass
class ConsoleClient:
    """El usuario de consola (ver el módulo). ``transport`` sustituye la red en las pruebas del
    propio cliente; ``seed`` fija el orden de los recursos consultados."""

    base_url: str
    verify: Path
    session: ConsoleSession
    targets: ConsoleTargets
    seed: int
    routes: Sequence[ConsoleRoute] = CONSOLE_ROUTES
    transport: httpx.BaseTransport | None = None
    samples: list[Sample] = field(default_factory=list)
    unpublished: tuple[str, ...] = ()
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None
    _error: BaseException | None = None

    def __post_init__(self) -> None:
        published = published_routes()
        self.unpublished = tuple(r.name for r in self.routes if r.name not in published)
        self._rng = random.Random(self.seed)  # noqa: S311 - orden de consulta, no un secreto

    def _client(self) -> httpx.Client:
        headers = {
            "Cookie": f"{SESSION_COOKIE_NAME}={self.session.cookie_value}",
            CONCESSION_HEADER: str(self.session.concession_id),
            "Sec-Fetch-Site": "same-origin",
        }
        if self.transport is not None:
            return httpx.Client(
                base_url=self.base_url,
                headers=headers,
                transport=self.transport,
                timeout=HTTP_SECONDS,
            )
        return httpx.Client(
            base_url=self.base_url,
            headers=headers,
            verify=ssl.create_default_context(cafile=str(self.verify)),
            timeout=HTTP_SECONDS,
        )

    def _request(self, route: ConsoleRoute) -> tuple[str, str, dict[str, Any] | None]:
        targets, rng = self.targets, self._rng
        values = {
            "node_id": str(rng.choice(targets.node_ids)),
            "zone_id": str(rng.choice(targets.zone_ids)),
            "record_id": str(uuid.uuid4()),
        }
        path = route.template.format(**values)
        if route.name == "GET /fleet/nodes":
            path += "?limit=100"
        body = None
        if route.name == "POST /documents":
            content = rng.randbytes(DOCUMENT_BYTES)
            body = {
                "plant_id": str(rng.choice(targets.plant_ids)),
                "kind": "scope_record",
                "content_type": "application/pdf",
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        return route.method, path, body

    def round(self, client: httpx.Client) -> None:
        """Una pasada por cada ruta publicada, midiendo cada petición."""
        for route in self.routes:
            if route.name in self.unpublished:
                continue
            method, path, body = self._request(route)
            started = WALL.monotonic()
            response = client.request(method, path, json=body)
            response.read()
            elapsed_ms = (WALL.monotonic() - started) * 1000
            self.samples.append(Sample(route.name, WALL.now(), elapsed_ms, response.status_code))

    def run(self, rounds: int | None = None) -> None:
        """``rounds`` pasadas, o hasta ``stop`` si es ``None``."""
        with self._client() as client:
            done = 0
            while not self._stop.is_set() and (rounds is None or done < rounds):
                self.round(client)
                done += 1

    def start(self) -> None:
        def target() -> None:
            try:
                self.run()
            except BaseException as error:  # el hilo informa su fallo al terminar
                self._error = error

        self._thread = threading.Thread(target=target, name="consola-sintetica", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=HTTP_SECONDS * 2)
        if self._error is not None:
            raise AssertionError(f"el cliente sintético de consola falló: {self._error!r}")


@dataclass(frozen=True)
class ConsoleVerdict:
    routes: Mapping[str, Mapping[str, Any]]
    failures: tuple[str, ...]
    unpublished: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures

    def to_json(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failures": list(self.failures),
            "unpublished": list(self.unpublished),
            "routes": dict(self.routes),
        }

    def message(self) -> str:
        return "rutas de consola fuera de su objetivo (NFR-GOB-19): " + "; ".join(self.failures)


def _inside(moment: dt.datetime, windows: Iterable[tuple[dt.datetime, dt.datetime]]) -> bool:
    return any(start <= moment < end for start, end in windows)


def evaluate(
    samples: Sequence[Sample],
    *,
    windows: Sequence[tuple[dt.datetime, dt.datetime]] | None = None,
    routes: Sequence[ConsoleRoute] = CONSOLE_ROUTES,
    unpublished: Sequence[str] = (),
    min_samples: int = MIN_SAMPLES,
) -> ConsoleVerdict:
    """p95 por ruta de las muestras en ``windows`` (todas si es ``None``) y el veredicto: cada
    ruta publicada que incumple su p95, responde fuera de 2xx o no reúne ``min_samples``."""
    selected: dict[str, list[Sample]] = defaultdict(list)
    for sample in samples:
        if windows is None or _inside(sample.at, windows):
            selected[sample.route].append(sample)
    summary: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    for route in routes:
        if route.name in unpublished:
            summary[route.name] = {"p95_target_ms": route.p95_ms, "status": "unpublished"}
            continue
        measured = selected.get(route.name, [])
        values = [sample.elapsed_ms for sample in measured]
        p95 = percentile(values, 95)
        errors = sorted({sample.status for sample in measured if not 200 <= sample.status < 300})
        summary[route.name] = {
            "p95_target_ms": route.p95_ms,
            "count": len(values),
            "p50_ms": None if not values else round(percentile(values, 50) or 0.0, 1),
            "p95_ms": None if p95 is None else round(p95, 1),
            "max_ms": None if not values else round(max(values), 1),
            "non_2xx": errors,
        }
        if len(values) < min_samples:
            failures.append(f"{route.name}: {len(values)} muestras (mínimo {min_samples})")
        elif p95 is not None and p95 > route.p95_ms:
            failures.append(
                f"{route.name}: p95 {p95:.0f} ms > {route.p95_ms:.0f} ms (n={len(values)})"
            )
        if errors:
            failures.append(f"{route.name}: respondió {', '.join(map(str, errors))}")
    return ConsoleVerdict(summary, tuple(failures), tuple(unpublished))


def delayed(
    inner: httpx.BaseTransport, route: str, delay_ms: float, sleep: Callable[[float], None]
) -> httpx.BaseTransport:
    """Transporte que añade ``delay_ms`` a cada petición de ``route`` (``MÉTODO /plantilla``):
    la latencia inyectada con que se prueba que el veredicto nombra la ruta."""
    method, template = route.split(" ", 1)
    pattern = re.compile(
        "^"
        + "/".join(
            "[^/]+" if part.startswith("{") else re.escape(part) for part in template.split("/")
        )
        + "$"
    )

    class Delayed(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            if request.method == method and pattern.match(request.url.path):
                sleep(delay_ms / 1000)
            return inner.handle_request(request)

        def close(self) -> None:
            inner.close()

    return Delayed()
