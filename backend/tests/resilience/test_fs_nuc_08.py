"""FS-NUC-08 · Worker muerto entre el efecto y la confirmación (PR-NUC-30, PR-NUC-42;
PAT-NUC-RES-05; BR-NUC-76).

Dos ``vigia-worker`` **reales** (``tests/resilience/worker_process.py``: ``shared.worker.main.serve``
con bucles de despacho, planificador con arrendamiento y PostgreSQL como ``vigia_app``).

**Inyección**:

- (a) **entrega**: el gancho de prueba ``VIGIA_TEST_KILL_AFTER_EFFECT`` hace que el primer proceso
  que produce el efecto externo del consumidor ``resilience_effect`` se mate con ``SIGKILL``
  (señal no capturable) **justo después del efecto**, antes de que el despachador confirme;
- (b) **tarea periódica**: el proceso que tiene el arrendamiento de ``worker_probe`` se termina con
  ``SIGKILL`` **en mitad de la tarea** (después de terminar alguna organización, con otra en curso;
  el número de organizaciones sale de la semilla).

**Resultado esperado**: (a) el evento se **reentrega con el mismo ``event_id``** al otro proceso y
el **efecto ocurre una sola vez** (el manejador es idempotente por ``event_id``); todas las
entregas terminan ``delivered``; (b) la tarea es **retomada por el otro worker al vencer el
arrendamiento** (nunca antes) y **termina sin duplicar**: cada organización con su efecto una sola
vez.

Solo datos generados.
"""

from __future__ import annotations

import signal
import uuid
from collections import Counter
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from tests.factories import make_context
from tests.integration.conftest import PostgresEndpoint
from tests.resilience.harness import WALL, free_port, scenario, wait_until
from tests.resilience.processes import (
    EFFECT_CONSUMER,
    EffectHandler,
    ProcessGroup,
    process_environment,
    process_group,
    read_lines,
    resilience_catalog,
    wait_http,
)
from tests.worker_support import (
    EFFECT_EVENT,
    PROBE_TASK,
    ProbeTask,
    WorkerEnvironment,
    synchronize,
    worker_environment,
)
from vigia_platform.shared.context import ActorKind
from vigia_platform.shared.outbox.publish import NewEvent, Outbox

pytestmark = [pytest.mark.integration, pytest.mark.nightly]

LEASE_SECONDS: Final = 3.0
RENEW_SECONDS: Final = 1.0
ORG_SECONDS: Final = 0.4
WORKER_MODULE: Final = "tests.resilience.worker_process"


@pytest.fixture
def env(postgres_endpoint: PostgresEndpoint) -> Iterator[WorkerEnvironment]:
    with worker_environment(postgres_endpoint, "fs_nuc_08") as environment:
        yield environment


def _start_workers(
    env: WorkerEnvironment, group: ProcessGroup, directory: Path, **extra: object
) -> dict[str, int]:
    ports = {name: free_port() for name in ("a", "b")}
    base = process_environment(
        VIGIA_TEST_DATABASE_URL=env.migrated.as_role("vigia_app").sqlalchemy_url,
        VIGIA_TEST_PROVIDER_ORGANIZATION=env.provider_organization_id,
        VIGIA_TEST_WORKER_LOG=directory / "worker.jsonl",
        VIGIA_TEST_WORKER_ORG_SECONDS=ORG_SECONDS,
        VIGIA_TEST_LEASE_SECONDS=LEASE_SECONDS,
        VIGIA_TEST_RENEW_SECONDS=RENEW_SECONDS,
        VIGIA_TEST_EFFECT_LOG=directory / "effects.jsonl",
        VIGIA_TEST_DELIVERY_LOG=directory / "deliveries.jsonl",
        VIGIA_TEST_KILL_MARK=directory / "killed.mark",
        **extra,
    )
    for name, port in ports.items():
        group.start(name, WORKER_MODULE, {**base, "VIGIA_WORKER_HEALTH_PORT": str(port)})
    for name, port in ports.items():
        wait_http(
            f"http://127.0.0.1:{port}/health/live",
            message=f"el worker {name} no atendió /health/live",
        )
    return ports


async def _publish(env: WorkerEnvironment, organization_id: uuid.UUID, count: int) -> list[str]:
    """``count`` eventos ``worker_probe_effect`` de la organización, como los publica la tarea."""
    catalog = resilience_catalog(ProbeTask(), EffectHandler("prueba", {}))
    await synchronize(env.database, catalog, env.clock)
    outbox = Outbox(catalog, env.clock)
    context = make_context(kind=ActorKind.SYSTEM, organization_id=organization_id)
    published = []
    async with env.database.transaction(context) as transaction:
        for _ in range(count):
            publication = await outbox.publish(
                transaction,
                NewEvent(
                    event_name=EFFECT_EVENT,
                    payload={"organization_id": str(organization_id), "task": PROBE_TASK},
                ),
            )
            published.append(str(publication.event.event_id))
    return published


async def _effect_deliveries(env: WorkerEnvironment) -> list[Any]:
    return await env.fetch(
        "SELECT d.event_id, d.status, d.attempts FROM shared.outbox_delivery d"
        " WHERE d.consumer_name = $1",
        EFFECT_CONSUMER,
    )


def test_fs_nuc_08a_worker_killed_between_the_effect_and_the_confirmation(
    env: WorkerEnvironment, tmp_path: Path
) -> None:
    with scenario(
        "FS-NUC-08a",
        title="Worker muerto entre el efecto y la confirmación (entrega)",
        injection=(
            "proceso de trabajo terminado con SIGKILL justo después del efecto del manejador"
        ),
        expected="el evento se reentrega con el mismo event_id y el efecto ocurre una sola vez",
    ) as run:
        organization = env.run(env.add_organizations(1))[0]
        with process_group(tmp_path) as group:
            _start_workers(env, group, tmp_path, VIGIA_TEST_KILL_AFTER_EFFECT="1")
            published = env.run(_publish(env, organization, run.random.randint(2, 5)))

            def done() -> bool:
                rows = env.run(_effect_deliveries(env))
                mine = [r for r in rows if str(r["event_id"]) in set(published)]
                return len(mine) == len(published) and all(
                    r["status"] == "delivered" for r in mine
                )

            wait_until(done, timeout=90, message="las entregas no terminaron")
            codes = {name: spawned.process.poll() for name, spawned in group.spawned.items()}
        effects = read_lines(tmp_path / "effects.jsonl")
        deliveries = read_lines(tmp_path / "deliveries.jsonl")
        killed_mark = (tmp_path / "killed.mark").exists()
        per_event = Counter(entry["event_id"] for entry in deliveries)
        redelivered = [event for event, count in per_event.items() if count > 1]
        owners = {
            event: sorted({e["owner"] for e in deliveries if e["event_id"] == event})
            for event in redelivered
        }
        run.observe(
            events=len(published),
            exit_codes=codes,
            effects=Counter(entry["event_id"] for entry in effects).most_common(),
            redelivered=owners,
        )
        assert killed_mark, "el gancho mató a un proceso"
        assert sorted(code for code in codes.values() if code is not None) == [-signal.SIGKILL]
        assert sorted(entry["event_id"] for entry in effects) == sorted(published)
        assert len(redelivered) == 1, "solo el evento en curso al morir se reentrega"
        (event_owners,) = owners.values()
        assert len(event_owners) == 2, "lo reentrega el otro proceso, con el mismo event_id"


def test_fs_nuc_08b_worker_killed_during_a_periodic_task(
    env: WorkerEnvironment, tmp_path: Path
) -> None:
    with scenario(
        "FS-NUC-08b",
        title="Worker muerto durante una tarea periódica",
        injection="proceso de trabajo terminado con SIGKILL durante una tarea periódica",
        expected=(
            "la tarea periódica es retomada por otro worker al vencer el arrendamiento y termina"
            " sin duplicar"
        ),
    ) as run:
        env.run(env.suspend_all_organizations())
        organizations = env.run(env.add_organizations(run.random.randint(4, 7)))
        finished_before = run.random.randint(1, 2)
        log = tmp_path / "worker.jsonl"
        with process_group(tmp_path) as group:
            _start_workers(env, group, tmp_path)
            pids = {f"proceso-{s.process.pid}": name for name, s in group.spawned.items()}
            env.run(env.put_task(PROBE_TASK, WALL.now() - timedelta(seconds=1)))

            def holder() -> str | None:
                entries = read_lines(log)
                ends = [e for e in entries if e["event"] == "end"]
                starts = [e for e in entries if e["event"] == "start"]
                if len(ends) >= finished_before and len(starts) > len(ends):
                    return str(starts[-1]["owner"])
                return None

            owner = wait_until(holder, timeout=60, message="ningún worker tomó la tarea")
            killed = pids[owner]
            group.spawned[killed].process.send_signal(signal.SIGKILL)
            group.spawned[killed].process.wait(10)
            killed_at = WALL.now()

            def finished() -> bool:
                row = env.run(env.task_row(PROBE_TASK))
                return bool(row.last_outcome == "succeeded" and row.lease_owner is None)

            wait_until(finished, timeout=60, message="el otro worker no terminó la tarea")
        entries = read_lines(log)
        effects = [org for org in env.run(env.effects()) if org in set(organizations)]
        survivor = [e for e in entries if e["owner"] != owner]
        resumed_after = min(
            (_at(e) - killed_at).total_seconds() for e in survivor
        )
        run.observe(
            organizations=len(organizations),
            killed_after_finished=finished_before,
            resumed_after_seconds=round(resumed_after, 2),
            lease_seconds=LEASE_SECONDS,
            effects=len(effects),
        )
        assert sorted(effects) == sorted(organizations), "cada organización, una sola vez"
        assert len(effects) == len(organizations)
        # Lo retoma al vencer el arrendamiento (renovado cada 1 s, vigente 3 s), nunca antes.
        assert resumed_after >= LEASE_SECONDS - RENEW_SECONDS - 0.5
        before = [e for e in entries if _at(e) <= killed_at]
        assert {e["owner"] for e in before} == {owner}, "mientras vivían, solo trabajó uno"


def _at(entry: dict[str, Any]) -> datetime:
    """El instante de una línea del registro de ``tests/worker_process.py`` (ISO con zona)."""
    return datetime.fromisoformat(entry["at"])