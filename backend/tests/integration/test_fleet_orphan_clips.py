"""``mark_orphan_clips`` sobre PostgreSQL 16 y LocalStack (TASK-222; LC-GOB-18; BR-GOB-94).

Concesiones reales (``ClipGrantService``) y subidas reales con su URL a un depósito versionado de
LocalStack; el barrido corre como lo corre el planificador: en la transacción de **una**
organización con su contexto de iteración periódica, como ``vigia_app``.

- Un clip ``evidence`` subido sin registro que lo cite pasa a ``orphan`` desde las 24 h y cuenta
  **una** vez en ``clip_grants_orphaned_total`` de su nodo; sin objeto y vencido, ``expired``; un
  clip ``verification``, citado o reciente no cambia; una segunda pasada no cambia ni cuenta nada.
- **Prueba concurrente** (criterio 4, segunda mitad): dos ejecuciones solapadas sobre la misma
  organización (las dos leen los candidatos y consultan el almacén antes de que ninguna escriba)
  marcan cada huérfano y suman su contador una sola vez.
- Almacén caído o que falla en una sola consulta: ningún estado cambia en la organización
  (FS-GOB-01 en comportamiento, NFR-GOB-42).
- ``node_clip_counts``: por nodo, huérfanos y clips ``evidence`` de la ventana (TASK-225).

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator, Sequence
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text

from tests.dispatch_support import metric_points
from tests.fleet_clip_support import (
    LONG_TIMEOUT_SECONDS,
    ClipNode,
    ClipWorld,
    clip_world,
    video,
)
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint
from vigia_platform.fleet.adapters.s3.clip_storage import ClipObjectStore
from vigia_platform.fleet.application.orphan_clips import OrphanClipSweeper, OrphanSweepReport
from vigia_platform.fleet.domain.clip_upload_grant import ORPHAN_AFTER
from vigia_platform.fleet.domain.verification_clip import ObjectFacts
from vigia_platform.shared.context import ContextAbsent
from vigia_platform.shared.observability.metrics import MetricName
from vigia_platform.shared.storage import StorageUnavailable

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def clips(
    postgres_endpoint: PostgresEndpoint, localstack_endpoint: LocalStackEndpoint
) -> Iterator[ClipWorld]:
    with clip_world(postgres_endpoint, localstack_endpoint, "fleet_orphans") as world:
        yield world


def _orphans_counted(clips: ClipWorld, node: ClipNode) -> int:
    return sum(
        int(value)
        for attributes, value in metric_points(clips.reader, MetricName.CLIP_GRANTS_ORPHANED_TOTAL)
        if attributes.get("node_id") == str(node.db.node_id)
    )


def _status(clips: ClipWorld, clip_id: uuid.UUID) -> str:
    return str(clips.grant_row(clip_id)["status"])


def _sweep(
    clips: ClipWorld, organization_id: uuid.UUID, sweeper: OrphanClipSweeper | None = None
) -> OrphanSweepReport:
    worker = sweeper or clips.sweeper

    async def run() -> OrphanSweepReport:
        async with clips.authz.sessions.database.transaction(
            clips.system(organization_id)
        ) as transaction:
            return await worker.sweep(transaction)

    report: OrphanSweepReport = clips.run(run())
    return report


def _cite(clips: ClipWorld, node: ClipNode, clip_id: uuid.UUID) -> None:
    """Lo que hará la ingesta (TASK-221) al aceptar un registro que cita el clip."""
    context = clips.scope(node).context

    async def run() -> None:
        async with clips.authz.sessions.database.transaction(context) as tx:
            await tx.execute(
                text(
                    "UPDATE fleet.clip_upload_grant SET status = 'used', used_at = :now"
                    " WHERE clip_id = :clip_id AND status = 'issued'"
                ),
                {"now": clips.now(), "clip_id": clip_id},
            )

    clips.run(run())


def test_an_uploaded_clip_without_record_becomes_orphan_from_24_hours_and_counts_once(
    clips: ClipWorld,
) -> None:
    node = clips.node()
    organization_id = node.db.organization_id
    orphan = clips.uploaded(node, video("huerfano"), purpose="evidence").grant.clip_id
    never_uploaded = clips.issue(node, video("sin-objeto"), purpose="evidence").grant.clip_id
    verification = clips.uploaded(node, video("verificacion")).grant.clip_id
    cited = clips.uploaded(node, video("citado"), purpose="evidence").grant.clip_id
    _cite(clips, node, cited)
    clips.clock.advance(12 * 3600)
    recent = clips.uploaded(node, video("reciente"), purpose="evidence").grant.clip_id
    counted = _orphans_counted(clips, node)

    # Justo antes de las 24 h, nada.
    clips.clock.advance(12 * 3600 - 1)
    assert _sweep(clips, organization_id) == OrphanSweepReport()
    clips.clock.advance(1)
    report = _sweep(clips, organization_id)
    assert report.orphaned == {orphan: node.db.node_id}
    assert report.expired == {never_uploaded: node.db.node_id}
    assert _status(clips, orphan) == "orphan"
    row = clips.grant_row(orphan)
    assert row["orphaned_at"] == row["used_at"] and row["orphaned_at"] is not None
    assert _status(clips, never_uploaded) == "expired"
    assert _status(clips, verification) == "issued"
    assert _status(clips, cited) == "used"
    assert _status(clips, recent) == "issued"
    assert _orphans_counted(clips, node) == counted + 1

    # Una segunda pasada (y otra días después) no cambia ni cuenta nada; la verificación sigue.
    assert _sweep(clips, organization_id) == OrphanSweepReport()
    clips.clock.advance(3 * 86_400)
    later = _sweep(clips, organization_id)
    assert later.orphaned == {recent: node.db.node_id} and later.expired == {}
    assert _status(clips, verification) == "issued"
    assert _orphans_counted(clips, node) == counted + 2
    # Nada se borra del depósito.
    key = clips.grant_row(orphan)["storage_key"]
    assert clips.s3.head_object(Bucket=clips.bucket, Key=key)["ContentLength"] > 0


def test_a_sweep_only_touches_its_own_organization(clips: ClipWorld) -> None:
    mine = clips.node()
    theirs = clips.node()
    own = clips.uploaded(mine, video("propio"), purpose="evidence").grant.clip_id
    other = clips.uploaded(theirs, video("ajeno"), purpose="evidence").grant.clip_id
    clips.clock.advance(ORPHAN_AFTER.total_seconds())
    report = _sweep(clips, mine.db.organization_id)
    assert set(report.orphaned) == {own}
    assert _status(clips, other) == "issued"


# --- Prueba concurrente ---------------------------------------------------------------------------


class OverlappingStore(ClipObjectStore):
    """Cada ejecución consulta el almacén y espera a la otra antes de escribir."""

    def __init__(self, storage: Any, barrier: asyncio.Barrier) -> None:
        super().__init__(
            storage,
            presign_timeout_seconds=LONG_TIMEOUT_SECONDS,
            head_timeout_seconds=LONG_TIMEOUT_SECONDS,
        )
        self.barrier = barrier

    async def heads(self, keys: Sequence[str]) -> dict[str, ObjectFacts | None]:
        facts = await super().heads(keys)
        async with asyncio.timeout(LONG_TIMEOUT_SECONDS):
            await self.barrier.wait()
        return facts


@pytest.mark.parametrize("run", range(3))
def test_two_overlapping_runs_mark_each_orphan_and_count_it_once(
    clips: ClipWorld, run: int
) -> None:
    node = clips.node()
    organization_id = node.db.organization_id
    orphans = {
        clips.uploaded(node, video(f"solapado-{run}-{i}"), purpose="evidence").grant.clip_id
        for i in range(3)
    }
    expired = clips.issue(node, video(f"solapado-{run}-sin"), purpose="evidence").grant.clip_id
    clips.clock.advance(ORPHAN_AFTER.total_seconds() + 60)
    counted = _orphans_counted(clips, node)
    barrier = asyncio.Barrier(2)

    def sweeper() -> OrphanClipSweeper:
        return OrphanClipSweeper(
            database=clips.authz.sessions.database,
            store=OverlappingStore(clips.storage, barrier),
            clock=clips.clock,
            metrics=clips.metrics,
        )

    first, second = sweeper(), sweeper()

    async def both() -> list[Any]:
        database = clips.authz.sessions.database

        async def one(worker: OrphanClipSweeper) -> OrphanSweepReport:
            async with database.transaction(clips.system(organization_id)) as transaction:
                return await worker.sweep(transaction)

        return list(await asyncio.gather(one(first), one(second), return_exceptions=True))

    results = clips.run(both())
    assert [r for r in results if isinstance(r, BaseException)] == []
    reports: list[OrphanSweepReport] = results
    orphaned = [clip for report in reports for clip in report.orphaned]
    assert sorted(orphaned) == sorted(orphans)  # cada uno una sola vez, entre las dos
    assert [clip for report in reports for clip in report.expired] == [expired]
    assert all(_status(clips, clip) == "orphan" for clip in orphans)
    assert _orphans_counted(clips, node) == counted + len(orphans)


# --- Almacén caído ------------------------------------------------------------------------------


class FailingStorage:
    """Almacén que falla (transitorio) en las claves elegidas, o en todas."""

    def __init__(self, target: Any, failing: set[str] | None = None) -> None:
        self.target = target
        self.failing = failing

    async def head_object(self, key: str) -> Any:
        if self.failing is None or key in self.failing:
            raise StorageUnavailable("head_object")
        return await self.target.head_object(key)

    async def presign_put(self, *arguments: Any) -> Any:  # pragma: no cover - no se usa
        return await self.target.presign_put(*arguments)


@pytest.mark.parametrize("partial", [False, True])
def test_a_store_down_leaves_every_state_of_the_organization_unchanged(
    clips: ClipWorld, partial: bool
) -> None:
    node = clips.node()
    organization_id = node.db.organization_id
    uploaded = clips.uploaded(node, video(f"caido-{partial}"), purpose="evidence").grant
    missing = clips.issue(node, video(f"caido-sin-{partial}"), purpose="evidence").grant
    clips.clock.advance(ORPHAN_AFTER.total_seconds() + 60)
    failing = {missing.storage_key} if partial else None
    down = OrphanClipSweeper(
        database=clips.authz.sessions.database,
        store=ClipObjectStore(FailingStorage(clips.storage, failing)),
        clock=clips.clock,
        metrics=clips.metrics,
    )
    counted = _orphans_counted(clips, node)
    with pytest.raises(StorageUnavailable):
        _sweep(clips, organization_id, down)
    assert _status(clips, uploaded.clip_id) == "issued"
    assert _status(clips, missing.clip_id) == "issued"
    assert _orphans_counted(clips, node) == counted
    # En el ciclo siguiente, con el almacén de vuelta, se marcan.
    report = _sweep(clips, organization_id)
    assert set(report.orphaned) == {uploaded.clip_id}
    assert set(report.expired) == {missing.clip_id}


# --- Consulta por nodo para orphan_clips_growing ------------------------------------------------


def test_node_clip_counts_give_orphans_and_evidence_clips_of_the_window(clips: ClipWorld) -> None:
    organization = clips.organization()
    node = clips.node(organization)
    other = clips.node(organization)
    for index in range(2):
        clips.uploaded(node, video(f"cuenta-{index}"), purpose="evidence")
    clips.issue(node, video("cuenta-sin"), purpose="evidence")
    clips.uploaded(node, video("cuenta-verificacion"))  # no cuenta
    clips.uploaded(other, video("cuenta-otro"), purpose="evidence")
    clips.clock.advance(ORPHAN_AFTER.total_seconds() + 1)
    _sweep(clips, organization[0])
    clips.issue(node, video("cuenta-hoy"), purpose="evidence")

    async def counts() -> Any:
        async with clips.authz.sessions.database.transaction(
            clips.system(organization[0])
        ) as transaction:
            return await clips.sweeper.node_clip_counts(transaction, until=clips.now())

    found = {count.node_id: count for count in clips.run(counts())}
    assert found[node.db.node_id].orphan_clips == 2
    assert found[node.db.node_id].day_clips == 1  # las de ayer quedan fuera de la ventana
    assert found[other.db.node_id].orphan_clips == 1
    assert found[other.db.node_id].day_clips == 0

    async def empty_window() -> Any:
        async with clips.authz.sessions.database.transaction(
            clips.system(organization[0])
        ) as transaction:
            return await clips.sweeper.node_clip_counts(
                transaction, until=clips.now(), window=timedelta(0)
            )

    with pytest.raises(ValueError, match="ventana"):
        clips.run(empty_window())
    # Sin transacción, la guarda del repositorio (``@repository``) corta antes que nada.
    nothing: Any = None
    with pytest.raises(ContextAbsent):
        clips.run(clips.sweeper.node_clip_counts(nothing, until=clips.now()))
