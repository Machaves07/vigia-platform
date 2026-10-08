"""FS-GOB-03 · Base inaccesible durante ingesta, latido y lectura de consola (NFR-GOB-42;
PAT-NUC-RES-01, RES-02; PAT-GOB-RES-03, LC-GOB-12, LC-GOB-14).

Sobre la aplicación completa de U-03 en los contenedores **propios** del escenario
(``gob_support.gob_stack``) con los topes de base de ``vigia-api`` (``statement_timeout`` 10 s,
``lock_timeout`` 2 s, conexión y pool 5 s) y, contra la misma base, un ``vigia-api`` **real** del
arnés (``tests/resilience/api_process.py``, con U-03 cargado) para la salud.

**Inyección**: el contenedor de PostgreSQL se **pausa en mitad de cada operación**
(``PausingDatabase``: dentro de la transacción ya abierta o justo antes de la lectura, nunca
antes de que la petición empiece) en tres peticiones, en el orden que sale de la semilla:

- **ingesta**: ``POST /api/nodes/findings`` del nodo, con su clip ya subido;
- **latido**: ``POST /api/nodes/heartbeats``;
- **lectura de consola**: ``GET /fleet/nodes`` de la administración de la organización.

**Resultado esperado**: cada una responde ``temporarily_unavailable`` (503, transitorio) con
``retry_after_seconds`` entre 1 y 60; **cero aceptaciones y cero registros parciales** (ni
registro, ni evento, ni latido en el historial); con la base pausada, ``/health/ready`` del
proceso falla dentro de su presupuesto y ``/health/live`` responde 200; al reanudarse, la **cola
local del nodo sigue intacta**: el reenvío del mismo hallazgo y del mismo latido se acepta una
sola vez, la consola responde y ``/health/ready`` vuelve a 200.

Solo datos generados.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Final

import httpx
import pytest

from tests.gob_platform_support import GobZone, ok
from tests.resilience.gob_support import PRODUCTION_API_DATABASE, GobStack, gob_stack
from tests.resilience.harness import WALL, free_port, scenario
from tests.resilience.processes import process_environment, process_group, wait_http
from vigia_platform.shared.api.health import READINESS_BUDGET_SECONDS

pytestmark = [pytest.mark.integration, pytest.mark.nightly]

API_MODULE: Final = "tests.resilience.api_process"
OPERATION_BUDGET_SECONDS: Final = 5.0 + 10.0 + 1.0 + 5.0 + 10.0
"""Lo más que espera una operación con la base pausada: conexión (5 s) y comando (10 s más su
holgura) del intento, y otro intento de lectura; el tope de la plataforma, no de la prueba."""
MARGIN_SECONDS: Final = 5.0


@pytest.fixture(scope="module")
def stack() -> Iterator[GobStack]:
    with gob_stack("fs_gob_03", database_changes=PRODUCTION_API_DATABASE) as built:
        yield built


def _written(stack: GobStack, zone: GobZone) -> dict[str, int]:
    (row,) = stack.gob.fetch(
        "SELECT (SELECT count(*) FROM ledger.ledger_record WHERE organization_id = $1) AS records,"
        " (SELECT count(*) FROM shared.outbox_event WHERE organization_id = $1) AS events,"
        " (SELECT count(*) FROM fleet.heartbeat_history WHERE node_id = $2) AS heartbeats",
        zone.organization_id,
        zone.node,
    )
    return dict(row)


def _health(base: str) -> dict[str, Any]:
    started = WALL.monotonic()
    ready = httpx.get(f"{base}/health/ready", timeout=30.0)
    ready_seconds = WALL.monotonic() - started
    live = httpx.get(f"{base}/health/live", timeout=30.0)
    return {
        "ready": ready.status_code,
        "ready_seconds": round(ready_seconds, 3),
        "live": live.status_code,
    }


def test_fs_gob_03_database_paused_during_ingest_heartbeat_and_console_read(
    stack: GobStack, tmp_path: Path
) -> None:
    gob, flow = stack.gob, stack.flow
    with scenario(
        "FS-GOB-03",
        title="Base inaccesible durante ingesta, latido y lectura de consola",
        injection="contenedor de PostgreSQL pausado en mitad de cada operación",
        expected=(
            "temporarily_unavailable con retry_after_seconds; cero aceptaciones y cero registros"
            " parciales; /health/ready falla y /health/live no; la cola del nodo queda intacta"
        ),
    ) as run:
        zone = flow.productive_zone()
        ok(flow.post_heartbeat(zone))
        document = flow.finding(zone)
        heartbeat = flow.heartbeat(zone)

        operations: dict[str, Callable[[], httpx.Response]] = {
            "ingesta": lambda: flow.post_finding(zone, document),
            "latido": lambda: flow.post_heartbeat(zone, heartbeat),
            "consola": lambda: gob.call("GET", "/fleet/nodes", cookie=zone.admin),
        }
        order = list(operations)
        run.random.shuffle(order)

        port = free_port()
        environ = process_environment(
            VIGIA_TEST_DATABASE_URL=gob.env.authz.sessions.migrated.as_role(
                "vigia_app"
            ).sqlalchemy_url,
            VIGIA_TEST_PROVIDER_ORGANIZATION=gob.provider,
            VIGIA_TEST_PORT=port,
        )
        base = f"http://127.0.0.1:{port}"
        results: dict[str, Any] = {}
        with process_group(tmp_path) as group:
            group.start("api", API_MODULE, environ)
            wait_http(f"{base}/health/ready", message="el vigia-api del arnés no quedó listo")
            healthy_before = _health(base)
            for name in order:
                before = _written(stack, zone)
                stack.database.arm(stack.postgres.pause)
                started = WALL.monotonic()
                response = operations[name]()
                seconds = WALL.monotonic() - started
                paused = stack.postgres.status() == "paused"
                health = _health(base)
                stack.postgres.unpause()
                stack.postgres.wait_ready()
                body = response.json()
                results[name] = {
                    "status": response.status_code,
                    "code": body.get("code"),
                    "retry_after_seconds": body.get("retry_after_seconds"),
                    "seconds": round(seconds, 2),
                    "paused_mid_operation": paused,
                    "health_while_paused": health,
                    "written": [before, _written(stack, zone)],
                }
            retried = {name: operations[name]() for name in order}
            wait_http(f"{base}/health/ready", message="/health/ready no volvió a 200")
            healthy_after = _health(base)
        findings = [
            row
            for row in gob.records(zone.organization_id, "finding_received")
            if row["source_key"] == document["finding_id"]
        ]
        run.observe(
            order=order,
            health_before=healthy_before,
            operations=results,
            retried={name: response.status_code for name, response in retried.items()},
            finding_status=retried["ingesta"].json().get("status"),
            findings_with_source_key=len(findings),
            health_after=healthy_after,
        )

        assert healthy_before == {**healthy_before, "ready": 200, "live": 200}
        for name, result in results.items():
            assert result["paused_mid_operation"], name
            assert (result["status"], result["code"]) == (503, "temporarily_unavailable"), (
                name,
                result,
            )
            assert 1 <= result["retry_after_seconds"] <= 60
            assert result["seconds"] <= OPERATION_BUDGET_SECONDS + MARGIN_SECONDS
            # Cero aceptaciones y cero registros parciales.
            assert result["written"][0] == result["written"][1], name
            health = result["health_while_paused"]
            assert health["ready"] == 503 and health["live"] == 200, (name, health)
            assert health["ready_seconds"] < READINESS_BUDGET_SECONDS + MARGIN_SECONDS
        # La cola del nodo intacta: el reenvío entra, una sola vez.
        assert {name: response.status_code for name, response in retried.items()} == dict.fromkeys(
            order, 200
        ), {name: response.text for name, response in retried.items()}
        assert retried["ingesta"].json()["status"] == "accepted"
        assert len(findings) == 1
        assert healthy_after["ready"] == 200
