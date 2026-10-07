"""FS-GOB-08 · Tarea periódica terminada a mitad de barrido (NFR-GOB-08, 12, 47; PR-GOB-16, 32;
PAT-NUC-RES-05; PAT-GOB-RES-04, LC-GOB-16, LC-GOB-18).

Dos ``vigia-worker`` **reales** (``tests/resilience/worker_process.py``: ``serve`` con señales,
arranque supervisado, planificador con arrendamiento y PostgreSQL como ``vigia_app``) **con U-03
cargado**: las siete tareas de U-03 con sus manejadores reales. Contra la base de la aplicación
completa (``gob_platform``), donde cada organización cliente tiene una zona productiva cuyo nodo
late con el adaptador de señales ``simulated``: la primera evaluación de ``evaluate_fleet_alarms``
levanta en cada una ``simulated_adapter_in_productive`` (con su evento y su alerta de seguridad).
El número de organizaciones sale de la semilla.

**Inyección**: el gancho de tarea lenta del proceso de prueba (``VIGIA_TEST_SLOW_TASK``) deja cada
organización abierta un momento **después** de las escrituras del manejador real, sin confirmar;
el proceso que tiene el arrendamiento de ``evaluate_fleet_alarms`` se termina con ``SIGKILL``
(señal no capturable) **mientras recorre la tercera organización**.

**Resultado esperado**: el otro worker **retoma la tarea al vencer el arrendamiento** (nunca
antes; mientras vivían, solo trabajó uno), continúa desde la organización que no se confirmó y
termina el barrido **sin duplicar**: una alarma, un ``fleet_alarm_raised`` y una alerta de
seguridad por nodo; ninguna transición repetida; la tarea queda ``succeeded`` y el ciclo completo
termina **dentro de su cadencia** (60 s).

Solo datos generados.
"""

from __future__ import annotations

import signal
from collections import Counter
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from tests.gob_platform_support import GobPlatform, Onboarding, gob_platform, ok
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint
from tests.resilience.harness import WALL, free_port, scenario, wait_until
from tests.resilience.processes import (
    process_environment,
    process_group,
    read_lines,
    wait_http,
    wait_worker_started,
)

pytestmark = [pytest.mark.integration, pytest.mark.nightly]

TASK: Final = "evaluate_fleet_alarms"
KIND: Final = "simulated_adapter_in_productive"
CADENCE_SECONDS: Final = 60.0
LEASE_SECONDS: Final = 3.0
RENEW_SECONDS: Final = 1.0
ORG_SECONDS: Final = 1.0
KILL_AT: Final = 3
"""La organización en curso al matar al proceso (la tercera del barrido)."""
WORKER_MODULE: Final = "tests.resilience.worker_process"


@pytest.fixture(scope="module")
def gob(
    postgres_endpoint: PostgresEndpoint, localstack_endpoint: LocalStackEndpoint
) -> Iterator[GobPlatform]:
    with gob_platform(postgres_endpoint, localstack_endpoint, "fs_gob_08") as platform:
        yield platform


def _at(entry: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(entry["at"])


def test_fs_gob_08_worker_killed_while_evaluating_the_third_organization(
    gob: GobPlatform, tmp_path: Path
) -> None:
    with scenario(
        "FS-GOB-08",
        title="Tarea periódica terminada a mitad de barrido",
        injection=(
            "proceso de trabajo terminado con SIGKILL mientras evaluate_fleet_alarms recorre la"
            " tercera organización"
        ),
        expected=(
            "otro worker la retoma al vencer el arrendamiento sin duplicar alarmas, transiciones"
            " ni marcas; el ciclo se completa dentro de su cadencia"
        ),
    ) as run:
        flow = Onboarding(gob)
        zones = [flow.productive_zone() for _ in range(run.random.randint(KILL_AT + 1, 6))]
        for zone in zones:
            heartbeat = flow.heartbeat(
                zone, signal_reader={"available": True, "adapter": "simulated"}
            )
            ok(flow.post_heartbeat(zone, heartbeat))
        nodes = {zone.node for zone in zones}
        log = tmp_path / "tarea.jsonl"
        environ = process_environment(
            VIGIA_TEST_DATABASE_URL=gob.env.authz.sessions.migrated.as_role(
                "vigia_app"
            ).sqlalchemy_url,
            VIGIA_TEST_PROVIDER_ORGANIZATION=gob.provider,
            VIGIA_TEST_WORKER_LOG=tmp_path / "worker.jsonl",
            VIGIA_TEST_WORKER_ORG_SECONDS=0.1,
            VIGIA_TEST_LEASE_SECONDS=LEASE_SECONDS,
            VIGIA_TEST_RENEW_SECONDS=RENEW_SECONDS,
            VIGIA_TEST_EFFECT_LOG=tmp_path / "effects.jsonl",
            VIGIA_TEST_DELIVERY_LOG=tmp_path / "deliveries.jsonl",
            VIGIA_TEST_KILL_MARK=tmp_path / "killed.mark",
            VIGIA_TEST_SLOW_TASK=TASK,
            VIGIA_TEST_TASK_LOG=log,
            VIGIA_TEST_TASK_ORG_SECONDS=ORG_SECONDS,
        )
        with process_group(tmp_path) as group:
            ports = {name: free_port() for name in ("a", "b")}
            for name, port in ports.items():
                group.start(name, WORKER_MODULE, {**environ, "VIGIA_WORKER_HEALTH_PORT": str(port)})
            for name, port in ports.items():
                wait_http(f"http://127.0.0.1:{port}/health/live")
                wait_worker_started(group, name)
            pids = {f"proceso-{s.process.pid}": name for name, s in group.spawned.items()}
            due = WALL.now() - timedelta(seconds=1)
            gob.execute(
                "UPDATE shared.periodic_task SET next_run_at = $2, lease_owner = NULL,"
                " lease_until = NULL WHERE task_name = $1",
                TASK,
                due,
            )

            def holder() -> str | None:
                entries = read_lines(log)
                ends = [e for e in entries if e["event"] == "end"]
                starts = [e for e in entries if e["event"] == "start"]
                if len(ends) >= KILL_AT - 1 and len(starts) == KILL_AT:
                    return str(starts[-1]["owner"])
                return None

            owner = str(wait_until(holder, timeout=60, message="ningún worker llegó a la tercera"))
            killed = pids[owner]
            group.spawned[killed].process.send_signal(signal.SIGKILL)
            group.spawned[killed].process.wait(10)
            killed_at = WALL.now()

            def finished() -> bool:
                (row,) = gob.fetch(
                    "SELECT last_outcome, lease_owner, last_run_at FROM shared.periodic_task"
                    " WHERE task_name = $1",
                    TASK,
                )
                return bool(row["last_outcome"] == "succeeded" and row["lease_owner"] is None)

            wait_until(finished, timeout=90, message="el otro worker no terminó el barrido")
            codes = {name: spawned.process.poll() for name, spawned in group.spawned.items()}

        entries = read_lines(log)
        before = [e for e in entries if _at(e) <= killed_at]
        survivor = [e for e in entries if e["owner"] != owner]
        resumed_after = min((_at(e) - killed_at).total_seconds() for e in survivor)
        cycle_seconds = (
            max(_at(e) for e in entries if e["event"] == "end")
            - min(_at(e) for e in entries if e["event"] == "start")
        ).total_seconds()
        alarms = gob.fetch(
            "SELECT node_id, alarm_kind, cleared_at FROM fleet.fleet_alarm"
            " WHERE node_id = ANY($1::uuid[])",
            list(nodes),
        )
        raised = [
            event
            for zone in zones
            for event in gob.events(zone.organization_id, "fleet_alarm_raised")
        ]
        alerts = [
            entry
            for zone in zones
            for entry in gob.audit_entries(zone.organization_id, "fleet_security_alert")
        ]
        ends_by_org = Counter(e["organization_id"] for e in entries if e["event"] == "end")
        starts_by_org = Counter(e["organization_id"] for e in entries if e["event"] == "start")
        run.observe(
            organizations=len(zones),
            killed_owner=owner,
            exit_codes=codes,
            resumed_after_seconds=round(resumed_after, 2),
            lease_seconds=LEASE_SECONDS,
            cycle_seconds=round(cycle_seconds, 2),
            started_twice=[org for org, count in starts_by_org.items() if count > 1],
            alarms=Counter((str(row["node_id"]), row["alarm_kind"]) for row in alarms).most_common(
                3
            ),
            fleet_alarm_raised=len(raised),
            fleet_security_alert=len(alerts),
        )

        assert codes[killed] == -signal.SIGKILL
        assert {e["owner"] for e in before} == {owner}, "mientras vivían, solo trabajó uno"
        # Lo retoma al vencer el arrendamiento (renovado cada 1 s, vigente 3 s), nunca antes.
        assert resumed_after >= LEASE_SECONDS - RENEW_SECONDS - 0.5
        # Cada organización confirmada una sola vez; la tercera, retomada por el superviviente.
        assert max(ends_by_org.values()) == 1
        assert [org for org, count in starts_by_org.items() if count > 1] == [
            before[-1]["organization_id"]
        ]
        # Sin duplicar alarmas, eventos ni alertas: una de cada por nodo.
        assert sorted((str(r["node_id"]), r["alarm_kind"]) for r in alarms) == sorted(
            (str(node), KIND) for node in nodes
        )
        assert all(row["cleared_at"] is None for row in alarms)
        assert sorted(event["node_id"] for event in raised) == sorted(str(n) for n in nodes)
        assert len(alerts) == len(nodes)
        assert cycle_seconds <= CADENCE_SECONDS
