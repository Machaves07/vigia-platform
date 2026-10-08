"""FS-GOB-05 · KMS de la autoridad ``vigia-node-ca`` inaccesible (NFR-GOB-20, 34, 42;
PAT-GOB-RES-03, LC-GOB-11).

Sobre la aplicación completa (``gob_platform``) con la **autoridad de nodos efímera** del
escenario (clave P-256 en ``MemoryKms``, raíz en ``ca/root.pem`` del ``vigia-edge`` de LocalStack)
y el tope de producción de ``vigia-node-ca`` (``NODE_CA_DEADLINE_SECONDS``), métricas en memoria y
``/health/ready`` sobre la base de verdad (arranque supervisado en el bucle de la pila).

**Inyección**: tras una línea base de 2 minutos, el punto de KMS de la clave de la autoridad
**bloqueado 5 minutos** (``kms:Sign`` no responde: ``MemoryKms.hang``). Durante el bloqueo, a la
vez:

- **alta** (``POST /api/nodes/enrollment``) de nodos declarados con su código y **rotación**
  (``POST /api/nodes/credential-rotations``) de nodos dados de alta, repartidas en la ventana
  (los instantes salen de la semilla, dentro de los límites de tasa de cada ruta);
- **tráfico normal** de tres nodos productivos, con su latencia medida: concesión de subida, subida
  del clip y hallazgo una vez por segundo y nodo, y un latido cada 16 s.

**Resultado esperado**:

- el alta y la rotación responden **transitorio** (``temporarily_unavailable``, 503,
  ``retry_after_seconds`` entre 1 y 60) dentro del tope de la autoridad, sin escribir nada (el
  código de alta sigue activo; la credencial sigue ``active``; ningún ``node_enrolled`` ni
  ``node_credential_rotated``);
- **ingesta, latido y concesiones no se ven afectados**: todas aceptadas, y su p95 con la
  autoridad bloqueada no se degrada frente a la línea base medida en la misma ejecución (2 veces
  más 50 ms como mucho); el p95 frente al objetivo absoluto de NFR-GOB-01
  (``tests/load/profiles.ABSOLUTE_TARGETS``) queda en el informe como tendencia, como en los
  perfiles de carga (infrastructure-design §9.2);
- **alarma**: cada firma vencida cuenta en ``node_ca_sign_duration_ms{result=timeout}``, cada 503
  de la autoridad en ``node_requests_total`` y el p95 del alta supera el doble de su objetivo de
  2 s: la condición de la alarma ``latency-node-enrollment`` (infrastructure-design §8.1). La
  tasa de 5xx sobre todas las peticiones depende del volumen y queda solo en el informe;
- la instancia sigue ``ready`` durante todo el bloqueo (la autoridad solo se comprueba al
  arrancar, NFR-GOB-20);
- al volver la autoridad, el alta pendiente se completa con el mismo código.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from collections import defaultdict
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, Final

import httpx
import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from tests.dispatch_support import metric_points, metrics_with_reader
from tests.factories import uuid7
from tests.fleet_credentials_support import csr_pem, local_ip
from tests.gob_platform_support import GobPlatform, GobZone, Onboarding, gob_platform, stamp
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint
from tests.load.profiles import ABSOLUTE_TARGETS
from tests.resilience.gob_support import metric_sum, p95, productive_zones
from tests.resilience.harness import WALL, scenario
from vigia_platform.fleet.adapters.ca.certificate_profiles import NODE_CA_DEADLINE_SECONDS
from vigia_platform.shared.api.declarations import NodeRoute
from vigia_platform.shared.observability.metrics import MetricName

pytestmark = [pytest.mark.integration, pytest.mark.nightly]

BLOCK_SECONDS: Final = 300.0
"""El bloqueo de la autoridad: 5 minutos reales (PAT-GOB-RES-06)."""
BASELINE_SECONDS: Final = 120.0
"""La línea base, antes del bloqueo: el mismo tráfico con la autoridad respondiendo."""
DEGRADATION_FACTOR: Final = 2.0
DEGRADATION_SLACK_MS: Final = 50.0
"""«No se ven afectados»: el p95 con la autoridad bloqueada no pasa de 2 veces el de la línea
base más 50 ms (holgura del PC o del runner compartidos). Los objetivos absolutos de NFR-GOB-01
son tendencia fuera del ``soak`` (infrastructure-design §9.2) y quedan en el informe."""
ROUND_SECONDS: Final = 16.0
"""Un latido de cada nodo cada 16 s, dentro de los 4 por minuto del nodo."""
CYCLE_SECONDS: Final = 1.0
"""Concesión, subida y hallazgo de cada nodo cada segundo: 60 por minuto, dentro de su límite."""
PRODUCTIVE: Final = 3
DECLARED: Final = 5
"""Nodos declarados sin alta: el alta admite 5 intentos cada 15 minutos por nodo."""
ENROLL_EVERY: Final = (15.0, 25.0)
ROTATE_EVERY: Final = (60.0, 80.0)
"""Entre rotaciones (de nodos distintos: la ruta admite 2 por hora y nodo)."""
MARGIN_SECONDS: Final = 5.0
TARGETS: Final = ABSOLUTE_TARGETS["NFR-GOB-01"]
ENROLLMENT_ALARM_MS: Final = 2 * TARGETS["POST /api/nodes/enrollment"]["p95_ms"]
"""``latency-node-enrollment``: p95 del alta por encima del doble de su objetivo de 2 s."""
ROUTES: Final = {
    "concesión": "POST /api/nodes/clip-uploads",
    "hallazgo": "POST /api/nodes/findings",
    "latido": "POST /api/nodes/heartbeats",
}


@pytest.fixture(scope="module")
def platform(
    postgres_endpoint: PostgresEndpoint, localstack_endpoint: LocalStackEndpoint
) -> Iterator[tuple[GobPlatform, InMemoryMetricReader]]:
    metrics, reader = metrics_with_reader()
    with gob_platform(
        postgres_endpoint,
        localstack_endpoint,
        "fs_gob_05",
        node_ca_deadline_seconds=NODE_CA_DEADLINE_SECONDS,
        metrics=metrics,
        health=True,
    ) as gob:
        yield gob, reader


def _rotation_body(gob: GobPlatform, zone: GobZone) -> dict[str, Any]:
    name = str(zone.node)
    return {
        "node_id": name,
        "certificate_signing_request": csr_pem(name),
        "server_certificate_signing_request": csr_pem(name, names=[local_ip()]),
        "requested_at": stamp(gob.now()),
    }


def _clip_request(zone: GobZone, data: bytes) -> dict[str, Any]:
    return {
        "clip_id": str(uuid7()),
        "camera_id": str(zone.cameras[0]),
        "zone_id": str(zone.zone_id),
        "media_kind": "video",
        "content_type": "video/mp4",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "duration_ms": 50_000,
        "purpose": "evidence",
    }


def test_fs_gob_05_node_ca_kms_blocked_five_minutes(
    platform: tuple[GobPlatform, InMemoryMetricReader],
) -> None:
    gob, reader = platform
    with scenario(
        "FS-GOB-05",
        title="KMS de la autoridad vigia-node-ca inaccesible",
        injection=(
            f"punto de KMS de la clave de la autoridad bloqueado {BLOCK_SECONDS / 60:.0f} min"
        ),
        expected=(
            "alta y rotación transitorias; ingesta, latido y concesiones sin degradarse (p95"
            " frente a la línea base); alarma emitida; la instancia sigue ready"
        ),
    ) as run:
        flow = Onboarding(gob)
        zones = productive_zones(flow, PRODUCTIVE)
        declared = [flow.zone(enrolled=False, within=zones[0]) for _ in range(DECLARED)]
        codes = {zone.node: flow.code(zone) for zone in declared}
        rng = run.child()

        failures: list[dict[str, Any]] = []
        # Por fase: ``base`` (la autoridad responde) y ``bloqueo`` (la autoridad no responde).
        latencies: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
        traffic: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
        ready: list[int] = []
        phase = ["base"]

        async def timed(what: str, request: Any) -> httpx.Response:
            started = WALL.monotonic()
            response: httpx.Response = await request
            latencies[phase[0]][what].append((WALL.monotonic() - started) * 1000)
            traffic[phase[0]][what].append(response.status_code)
            return response

        async def authority(deadline: float) -> None:
            """Altas y rotaciones repartidas en la ventana, una a una."""
            next_rotation = WALL.monotonic() + rng.uniform(*ROTATE_EVERY) / 2
            # Cada nodo productivo rota una sola vez: la ruta admite pocas rotaciones por hora.
            rotations = list(zones)
            turn = 0
            while WALL.monotonic() < deadline - NODE_CA_DEADLINE_SECONDS - 1:
                if rotations and WALL.monotonic() >= next_rotation:
                    zone = rotations.pop(0)
                    kind, request = (
                        "rotación",
                        gob.node_send(
                            "POST",
                            NodeRoute.CREDENTIAL_ROTATION.path,
                            certificate=zone.cert,
                            body=_rotation_body(gob, zone),
                        ),
                    )
                    next_rotation += rng.uniform(*ROTATE_EVERY)
                else:
                    zone = declared[turn % len(declared)]
                    kind, request = (
                        "alta",
                        gob.node_send(
                            "POST",
                            NodeRoute.ENROLLMENT.path,
                            certificate=None,
                            body=flow.enrollment_body(zone, codes[zone.node]),
                        ),
                    )
                turn += 1
                started = WALL.monotonic()
                response = await request
                body = response.json()
                failures.append(
                    {
                        "kind": kind,
                        "status": response.status_code,
                        "code": body.get("code"),
                        "retry_after_seconds": body.get("retry_after_seconds"),
                        "seconds": round(WALL.monotonic() - started, 2),
                    }
                )
                await asyncio.sleep(rng.uniform(*ENROLL_EVERY))

        async def normal(deadline: float) -> None:
            """Concesión, subida y hallazgo de cada nodo productivo una vez por segundo y un latido
            cada ``ROUND_SECONDS`` (dentro de los límites del nodo): la API casi siempre tiene una
            petición en curso, también mientras el alta espera a la autoridad."""
            last = WALL.monotonic()
            next_heartbeat = next_ready = last
            while WALL.monotonic() < deadline:
                round_started = WALL.monotonic()
                gob.advance(round_started - last)  # el reloj de la aplicación sigue al real
                last = round_started
                beat = round_started >= next_heartbeat
                if beat:
                    next_heartbeat = round_started + ROUND_SECONDS
                for zone in zones:
                    data = secrets.token_bytes(256)
                    request = _clip_request(zone, data)
                    grant = await timed(
                        "concesión",
                        gob.node_send(
                            "POST",
                            NodeRoute.CLIP_UPLOAD.path,
                            certificate=zone.cert,
                            body=request,
                        ),
                    )
                    if grant.status_code != 200:
                        continue
                    await asyncio.to_thread(Onboarding.put, grant.json(), data)
                    started = gob.now() - timedelta(minutes=2)
                    reference = {
                        key: request[key]
                        for key in ("clip_id", "camera_id", "content_type", "sha256")
                    } | {
                        "media_kind": "video",
                        "size_bytes": len(data),
                        "duration_ms": request["duration_ms"],
                        "starts_at": stamp(started - timedelta(seconds=10)),
                        "ends_at": stamp(started + timedelta(seconds=40)),
                        "segment": "full",
                        "anonymized": True,
                        "storage_key": grant.json()["storage_key"],
                    }
                    document = flow.finding(zone, started, clip=reference)
                    await timed(
                        "hallazgo", flow.submit(zone, NodeRoute.FINDING, document, "finding_id")
                    )
                    if beat:
                        await timed(
                            "latido",
                            gob.node_send(
                                "POST",
                                NodeRoute.HEARTBEAT.path,
                                certificate=zone.cert,
                                body=flow.heartbeat(zone),
                            ),
                        )
                if phase[0] == "bloqueo" and round_started >= next_ready:
                    next_ready = round_started + ROUND_SECONDS
                    async with gob.client() as client:
                        ready.append((await client.get("/health/ready")).status_code)
                await asyncio.sleep(max(0.0, CYCLE_SECONDS - (WALL.monotonic() - round_started)))

        async def drive() -> None:
            # Línea base: el mismo tráfico con la autoridad respondiendo, en esta misma ejecución.
            await normal(WALL.monotonic() + BASELINE_SECONDS)
            phase[0] = "bloqueo"
            gob.kms.hang = True
            try:
                deadline = WALL.monotonic() + BLOCK_SECONDS
                await asyncio.gather(authority(deadline), normal(deadline))
            finally:
                gob.kms.hang = False

        with gob.lifespan():
            gob.run(drive())
            written = {
                "rotated": len(gob.records(zones[0].organization_id, "node_credential_rotated")),
                "credentials": [
                    row["status"]
                    for row in gob.fetch(
                        "SELECT status FROM fleet.node_credential WHERE node_id = ANY($1::uuid[])",
                        [zone.node for zone in zones],
                    )
                ],
                "declared_with_credential": len(
                    gob.fetch(
                        "SELECT 1 FROM fleet.node_credential WHERE node_id = ANY($1::uuid[])",
                        [zone.node for zone in declared],
                    )
                ),
            }
            recovered = flow.enroll(declared[0], codes[declared[0].node])

            async def ready_now() -> int:
                async with gob.client() as client:
                    return (await client.get("/health/ready")).status_code

            ready_after = gob.run(ready_now())

        timeouts = metric_sum(reader, MetricName.NODE_CA_SIGN_DURATION_MS, result="timeout")
        node_results = _node_results(reader)
        server_errors = sum(
            count
            for (_, result), count in node_results.items()
            if result == "temporarily_unavailable"
        )
        node_total = sum(node_results.values())
        enrollment_p95_ms = round(
            p95([item["seconds"] * 1000 for item in failures if item["kind"] == "alta"]), 1
        )
        p95s = {
            name: {what: round(p95(values), 1) for what, values in by_route.items()}
            for name, by_route in latencies.items()
        }
        allowed = {
            what: round(DEGRADATION_FACTOR * p95s["base"][what] + DEGRADATION_SLACK_MS, 1)
            for what in ROUTES
        }
        run.observe(
            authority_attempts=failures,
            traffic={
                name: {what: dict(_count(codes_)) for what, codes_ in by_route.items()}
                for name, by_route in traffic.items()
            },
            samples={
                name: {what: len(values) for what, values in by_route.items()}
                for name, by_route in latencies.items()
            },
            p95_ms=p95s,
            p95_allowed_while_blocked_ms=allowed,
            targets_ms_trend={what: TARGETS[route]["p95_ms"] for what, route in ROUTES.items()},
            within_absolute_target_while_blocked={
                what: p95s["bloqueo"][what] <= TARGETS[route]["p95_ms"]
                for what, route in ROUTES.items()
            },
            ready_during_block=dict(_count(ready)),
            ready_after=ready_after,
            node_ca_sign_timeouts=timeouts,
            node_requests=[[list(key), value] for key, value in sorted(node_results.items())],
            server_error_rate=round(server_errors / node_total, 4) if node_total else None,
            enrollment_p95_ms=enrollment_p95_ms,
            enrollment_alarm_ms=ENROLLMENT_ALARM_MS,
            written_during_block=written,
            enrolled_after_recovery=recovered is not None,
        )

        kinds = {item["kind"] for item in failures}
        assert kinds == {"alta", "rotación"}, kinds
        for item in failures:
            assert (item["status"], item["code"]) == (503, "temporarily_unavailable"), item
            assert 1 <= item["retry_after_seconds"] <= 60
            assert item["seconds"] <= NODE_CA_DEADLINE_SECONDS + MARGIN_SECONDS, item
        # Nada escrito por el alta ni por la rotación.
        assert written["rotated"] == 0
        assert written["declared_with_credential"] == 0
        assert set(written["credentials"]) == {"active"}
        # Ingesta, latido y concesiones: aceptadas y sin verse afectadas por el bloqueo.
        for name, by_route in traffic.items():
            for what, statuses in by_route.items():
                assert set(statuses) == {200}, (name, what, _count(statuses))
        for what in ROUTES:
            assert len(latencies["base"][what]) >= 15 and len(latencies["bloqueo"][what]) >= 40
            assert p95s["bloqueo"][what] <= allowed[what], (what, p95s, allowed)
        # Alarma: cada firma vencida cuenta (cada alta y cada rotación firman dos certificados,
        # cliente y servidor, y los dos vencen), cada 503 de la autoridad cuenta en
        # node_requests_total y el p95 del alta supera el doble de su objetivo: la condición de
        # latency-node-enrollment (infrastructure-design §8.1).
        assert timeouts >= len(failures)
        assert server_errors == len(failures)
        assert enrollment_p95_ms > ENROLLMENT_ALARM_MS, enrollment_p95_ms
        # La instancia sigue lista durante todo el bloqueo y después.
        assert ready and set(ready) == {200}
        assert ready_after == 200


def _count(values: list[int]) -> dict[int, int]:
    counted: dict[int, int] = defaultdict(int)
    for value in values:
        counted[value] += 1
    return counted


def _node_results(reader: InMemoryMetricReader) -> dict[tuple[str, str], int]:
    """``(ruta, resultado o rejection_code)`` → peticiones, de ``node_requests_total``."""
    results: dict[tuple[str, str], int] = defaultdict(int)
    for attributes, value in metric_points(reader, MetricName.NODE_REQUESTS_TOTAL):
        outcome = attributes.get("rejection_code") or attributes.get("result")
        results[(str(attributes.get("route")), str(outcome))] += int(value)
    return results
