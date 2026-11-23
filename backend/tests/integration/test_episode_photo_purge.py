"""``purge_episode_photos`` contra PostgreSQL 16 (TASK-573; A-112 punto 4; QF5 ampliada).

Como ``vigia_app``, la tarea en la transacción de la organización con el contexto de la iteración
periódica de U-03 y **su propio reloj simulado** (el instante de la ejecución), sobre fotos
sintéticas sembradas en ``fleet.episode_photo`` (``episode_photo_read_support``):

- **Vencimiento** (criterio 6): a 1 ms de ``expires_at`` la foto sigue; en ``expires_at`` se borra;
  repetir la purga no cambia nada y la métrica cuenta exactamente las borradas, por causa. Los
  vencimientos van 10 días por delante de la hora real: con el reloj del sistema no se borraría
  nada (``m_reloj_del_sistema``).
- **Zona en productive** (criterio 3): al aprobarse la compuerta de uso, la purga siguiente borra
  **todas** las fotos de la zona (y solo las de esa zona), también las que aún no vencían; una
  foto recibida tras una revocación sobrevive.
- **Por lotes**: con lotes de 2, una pasada borra todo; con un lote por pasada, lo que queda va en
  la siguiente.
- **Organización** (PR-GOB-12): solo borra fotos de la organización de su contexto, y su sentencia
  filtra por organización también sin RLS (como superusuario).
- **Concurrencia** (una prueba por garantía atómica):
  - dos purgas solapadas de la misma organización: cada foto se borra y se cuenta una sola vez (la
    segunda espera el candado de las filas y ya no las encuentra);
  - purgar mientras se lee: la lectura con la purga sin confirmar, y la purga confirmada con la
    lectura a medio camino, devuelven los bytes completos; después, ``not_found``. Y una tanda de
    lecturas lanzadas a la vez que la purga: cada una son los bytes completos o ``not_found``,
    nunca una respuesta parcial.
- **Lo que queda en disco** (documentado en el PR): tras la purga, la fila no se ve; los bytes de
  la tupla muerta siguen en el montón o en la TOAST hasta que ``VACUUM`` la poda (PostgreSQL no
  tiene ``secure_delete``). ``gob_0043`` deja la limpieza automática de la tabla y de su TOAST a 50
  tuplas muertas.

Topes: la espera de un candado se observa en ``pg_stat_activity`` (30 s); la base, 60 s (retro
15). Ninguna prueba decide por tiempo de pared. Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Iterator
from typing import Any, Final

import httpx
import pytest
from sqlalchemy.dialects.postgresql.asyncpg import dialect as asyncpg_dialect

from tests.authz_support import Site
from tests.dispatch_support import metric_points, metrics_with_reader
from tests.episode_photo_read_support import Stored, get, image_audit, stored, usage
from tests.fleet_alarm_support import blocked_or_done, system
from tests.fleet_http_support import CONCESSION_HEADER, SAME_ORIGIN, FleetStack, fleet_stack
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.fleet.adapters.http import FLEET_STATE_KEY
from vigia_platform.fleet.adapters.postgres import episode_photo_queries
from vigia_platform.fleet.adapters.postgres.episode_photo_queries import (
    PostgresEpisodePhotoQueries,
    PurgedPhoto,
    StoredImage,
)
from vigia_platform.fleet.adapters.postgres.episode_photo_store import PostgresEpisodePhotoStore
from vigia_platform.fleet.application.episode_photo_purge import (
    EpisodePhotoPurger,
    PurgeReport,
)
from vigia_platform.fleet.application.episode_photo_read import EpisodePhotoReader
from vigia_platform.fleet.domain.episode_photo import (
    EpisodePhotoRow,
    PhotoRole,
    ZoneMode,
    expires_at,
    finding_expires_at,
)
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME, SessionCookie
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.observability.metrics import MetricName, PlatformMetrics

pytestmark = pytest.mark.integration

MS: Final = dt.timedelta(milliseconds=1)
DAY: Final = dt.timedelta(days=1)
AHEAD: Final = 10 * DAY
"""Los vencimientos de estas pruebas van por delante de la hora real (m_reloj_del_sistema)."""
READERS: Final = 12
"""Lecturas lanzadas a la vez que la purga en la prueba de la tanda."""
Who = tuple[SessionCookie, uuid.UUID | None]


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[FleetStack]:
    with fleet_stack(postgres_endpoint, "episode_photo_purge", pool=12) as built:
        yield built


def _zone(site: Site, plant_index: int = 0, zone_index: int = 0) -> tuple[uuid.UUID, uuid.UUID]:
    plant = list(site.plants)[plant_index]
    return plant, site.plants[plant][zone_index]


def _photo(stack: FleetStack, site: Site, *, zone_index: int = 0, **options: Any) -> Stored:
    plant, zone = _zone(site, zone_index=zone_index)
    return stored(stack, site.organization_id, plant, zone, **options)


def _purger(
    stack: FleetStack, at: dt.datetime, metrics: PlatformMetrics | None = None, **options: Any
) -> EpisodePhotoPurger:
    return EpisodePhotoPurger(
        database=stack.database, clock=SimulatedClock(at), metrics=metrics, **options
    )


async def _run(stack: FleetStack, organization: uuid.UUID, purger: EpisodePhotoPurger) -> Any:
    async with stack.database.transaction(system(organization)) as transaction:
        return await purger.purge(transaction)


def _purge(
    stack: FleetStack,
    organization: uuid.UUID,
    at: dt.datetime,
    metrics: PlatformMetrics | None = None,
    **options: Any,
) -> PurgeReport:
    report: PurgeReport = stack.run(
        _run(stack, organization, _purger(stack, at, metrics, **options))
    )
    return report


def _alive(stack: FleetStack, *photos: Stored) -> set[uuid.UUID]:
    rows = stack.fetch(
        "SELECT photo_id FROM fleet.episode_photo WHERE photo_id = ANY($1::uuid[])",
        [photo.photo_id for photo in photos],
    )
    return {uuid.UUID(str(row["photo_id"])) for row in rows}


def _purged(reader: Any) -> dict[str, float]:
    return {
        str(attributes["cause"]): value
        for attributes, value in metric_points(reader, MetricName.EPISODE_PHOTOS_PURGED_TOTAL)
    }


# --- Vencimiento --------------------------------------------------------------------------------


def test_one_millisecond_before_expires_at_it_stays_and_at_expires_at_it_goes_once(
    stack: FleetStack,
) -> None:
    site = stack.site()
    received = stack.authz.now()
    photo = _photo(stack, site, received_at=received, expires_at=received + AHEAD)
    keeper = _photo(stack, site, received_at=received, expires_at=received + AHEAD + DAY)
    metrics, reader = metrics_with_reader()
    early = _purge(stack, site.organization_id, photo.expires_at - MS, metrics)
    assert early == PurgeReport()
    assert _alive(stack, photo, keeper) == {photo.photo_id, keeper.photo_id}
    due = _purge(stack, site.organization_id, photo.expires_at, metrics)
    assert due == PurgeReport(expired=(photo.photo_id,))
    assert _alive(stack, photo, keeper) == {keeper.photo_id}
    # Repetida, no cambia nada ni vuelve a contar.
    assert _purge(stack, site.organization_id, photo.expires_at, metrics) == PurgeReport()
    assert _alive(stack, photo, keeper) == {keeper.photo_id}
    assert _purged(reader) == {"expired": 1.0}


# --- Zona en productive -------------------------------------------------------------------------


def test_after_the_usage_gate_is_approved_the_next_purge_removes_the_whole_zone(
    stack: FleetStack,
) -> None:
    site = stack.site(plants=1, zones=2)
    plant, zone = _zone(site)
    now = stack.authz.now()
    retired = [_photo(stack, site) for _ in range(3)]
    neighbour = _photo(stack, site, zone_index=1)
    metrics, reader = metrics_with_reader()
    assert _purge(stack, site.organization_id, now, metrics) == PurgeReport()
    stack.tick(1)
    approved_at = stack.authz.now()
    usage(stack, site.organization_id, plant, zone, approved=True, at=approved_at)
    # Antes del instante de la aprobación, nada; desde él, todas las de la zona (sin vencer).
    assert _purge(stack, site.organization_id, approved_at - MS, metrics) == PurgeReport()
    report = _purge(stack, site.organization_id, approved_at, metrics)
    assert sorted(report.retired) == sorted(photo.photo_id for photo in retired)
    assert report.expired == ()
    assert _alive(stack, *retired, neighbour) == {neighbour.photo_id}
    assert _purge(stack, site.organization_id, approved_at, metrics) == PurgeReport()
    assert _purged(reader) == {"productive": 3.0}
    # Revocada la compuerta, una foto recibida después es de la nueva puesta en marcha.
    stack.tick(1)
    usage(stack, site.organization_id, plant, zone, approved=False, at=stack.authz.now())
    stack.tick(1)
    later = _photo(stack, site)
    assert _purge(stack, site.organization_id, stack.authz.now(), metrics) == PurgeReport()
    assert _alive(stack, later) == {later.photo_id}


def test_a_usage_gate_revoked_before_the_purge_still_purges_the_older_photos(
    stack: FleetStack,
) -> None:
    """La otra mitad del desvío 4 (sonda S8 de la revisión de Fable, ronda 1): la zona pasó a
    ``productive`` y se revocó **antes** de que pasara la purga. Las fotos anteriores a la
    aprobación siguen retiradas para la lectura y la purga las borra como ``productive``; la foto
    recibida tras la revocación y las de la zona vecina se quedan (r17: comparar
    ``effective_until`` con ``now`` en vez de con ``received_at`` las dejaría guardadas)."""
    site = stack.site(plants=1, zones=2)
    plant, zone = _zone(site)
    old = [_photo(stack, site) for _ in range(2)]
    neighbour = _photo(stack, site, zone_index=1)
    stack.tick(1)
    usage(stack, site.organization_id, plant, zone, approved=True, at=stack.authz.now())
    stack.tick(60)
    usage(stack, site.organization_id, plant, zone, approved=False, at=stack.authz.now())
    stack.tick(60)
    later = _photo(stack, site)
    installer = stack.installer(site)
    seen = [get(stack, installer, photo.image_path).status_code for photo in (*old, later)]
    assert seen == [404, 404, 200]
    metrics, reader = metrics_with_reader()
    report = _purge(stack, site.organization_id, stack.authz.now(), metrics)
    assert sorted(report.retired) == sorted(photo.photo_id for photo in old)
    assert report.expired == ()
    assert _alive(stack, *old, later, neighbour) == {later.photo_id, neighbour.photo_id}
    assert _purged(reader) == {"productive": 2.0}
    # Repetida, nada.
    assert _purge(stack, site.organization_id, stack.authz.now(), metrics) == PurgeReport()


def test_a_photo_both_expired_and_retired_counts_once_as_productive(stack: FleetStack) -> None:
    site = stack.site()
    plant, zone = _zone(site)
    received = stack.authz.now()
    photo = _photo(stack, site, received_at=received, expires_at=received + AHEAD)
    usage(stack, site.organization_id, plant, zone, approved=True, at=received + DAY)
    metrics, reader = metrics_with_reader()
    report = _purge(stack, site.organization_id, photo.expires_at, metrics)
    assert report == PurgeReport(retired=(photo.photo_id,))
    assert _purged(reader) == {"productive": 1.0}


# --- Lotes y organización -----------------------------------------------------------------------


def test_batches_delete_everything_in_one_run_and_the_cap_leaves_the_rest_for_the_next(
    stack: FleetStack,
) -> None:
    site = stack.site()
    received = stack.authz.now()
    expires = received + AHEAD
    photos = [_photo(stack, site, received_at=received, expires_at=expires) for _ in range(5)]
    first = _purge(stack, site.organization_id, expires, batch_size=2, max_batches=1)
    assert len(first.expired) == 2
    assert len(_alive(stack, *photos)) == 3
    rest = _purge(stack, site.organization_id, expires, batch_size=2)
    assert len(rest.expired) == 3
    assert set(first.expired) | set(rest.expired) == {photo.photo_id for photo in photos}
    assert _alive(stack, *photos) == set()


def test_the_purge_only_deletes_photos_of_its_organization(stack: FleetStack) -> None:
    own, other = stack.site(), stack.site()
    received = stack.authz.now()
    expires = received + AHEAD
    mine = _photo(stack, own, received_at=received, expires_at=expires)
    foreign = _photo(stack, other, received_at=received, expires_at=expires)
    assert _purge(stack, own.organization_id, expires) == PurgeReport(expired=(mine.photo_id,))
    assert _alive(stack, mine, foreign) == {foreign.photo_id}


@pytest.mark.parametrize("statement", ["_PURGE_EXPIRED", "_PURGE_RETIRED"])
def test_the_purge_statements_filter_by_organization_also_without_rls(
    stack: FleetStack, statement: str
) -> None:
    """Como superusuario (sin RLS), solo el filtro explícito separa las organizaciones."""
    own, other = stack.site(), stack.site()
    received = stack.authz.now()
    expires = received + AHEAD
    photos = {}
    for site in (own, other):
        plant, zone = _zone(site)
        photos[site.organization_id] = _photo(stack, site, received_at=received, expires_at=expires)
        usage(stack, site.organization_id, plant, zone, approved=True, at=received)
    compiled = getattr(episode_photo_queries, statement).compile(dialect=asyncpg_dialect())
    parameters = {"organization_id": own.organization_id, "now": expires, "limit": 100}
    rows = stack.fetch(str(compiled), *(parameters[name] for name in compiled.positiontup))
    assert {uuid.UUID(str(row["photo_id"])) for row in rows} == {
        photos[own.organization_id].photo_id
    }
    assert _alive(stack, *photos.values()) == {photos[other.organization_id].photo_id}


# --- Concurrencia -------------------------------------------------------------------------------


class HeldQueries(PostgresEpisodePhotoQueries):
    """Las sentencias reales, con una pausa tras el borrado de vencidas o tras leer la imagen."""

    def __init__(self, database: Any) -> None:
        super().__init__(database)
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def purge_expired(
        self, transaction: Transaction, now: dt.datetime, *, limit: int
    ) -> tuple[PurgedPhoto, ...]:
        deleted = await super().purge_expired(transaction, now, limit=limit)
        self.reached.set()
        await asyncio.wait_for(self.release.wait(), timeout=60)
        return deleted

    async def image(
        self, transaction: Transaction, photo_id: uuid.UUID, now: dt.datetime
    ) -> StoredImage | None:
        found = await super().image(transaction, photo_id, now)
        self.reached.set()
        await asyncio.wait_for(self.release.wait(), timeout=60)
        return found


def test_two_overlapping_purges_delete_and_count_each_photo_once(stack: FleetStack) -> None:
    site = stack.site()
    received = stack.authz.now()
    expires = received + AHEAD
    photos = [_photo(stack, site, received_at=received, expires_at=expires) for _ in range(4)]
    metrics, reader = metrics_with_reader()
    held = HeldQueries(stack.database)
    first = EpisodePhotoPurger(
        database=stack.database, clock=SimulatedClock(expires), metrics=metrics, queries=held
    )
    second = _purger(stack, expires, metrics)

    async def scenario() -> tuple[PurgeReport, PurgeReport]:
        one = asyncio.create_task(_run(stack, site.organization_id, first))
        await asyncio.wait_for(held.reached.wait(), timeout=60)
        two = asyncio.create_task(_run(stack, site.organization_id, second))
        # La segunda espera el candado de las filas que la primera borró sin confirmar.
        await blocked_or_done(stack.authz.sessions.admin, two)
        held.release.set()
        return await asyncio.wait_for(one, timeout=60), await asyncio.wait_for(two, timeout=60)

    a, b = stack.run(scenario())
    assert sorted(a.expired) == sorted(photo.photo_id for photo in photos)
    assert b == PurgeReport()
    assert _alive(stack, *photos) == set()
    assert _purged(reader) == {"expired": 4.0}


def _get(stack: FleetStack, who: Who, path: str) -> Any:
    cookie, concession = who
    headers = {**SAME_ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}"}
    if concession is not None:
        headers[CONCESSION_HEADER] = str(concession)
    return stack.client.get(path, headers=headers)


def _complete_or_missing(response: httpx.Response, photo: Stored) -> str:
    if response.status_code == 200:
        assert response.content == photo.data, "respuesta parcial o distinta"
        return "complete"
    assert (response.status_code, response.json()["code"]) == (404, "not_found"), response.text
    return "missing"


def test_reading_while_the_purge_is_uncommitted_gives_the_whole_image(stack: FleetStack) -> None:
    site = stack.site()
    received = stack.authz.now()
    expires = received + AHEAD
    photo = _photo(stack, site, received_at=received, expires_at=expires - 2 * DAY)
    installer = stack.installer(site)
    held = HeldQueries(stack.database)
    purger = EpisodePhotoPurger(
        database=stack.database, clock=SimulatedClock(expires), queries=held
    )

    async def scenario() -> tuple[httpx.Response, PurgeReport, httpx.Response]:
        purge = asyncio.create_task(_run(stack, site.organization_id, purger))
        await asyncio.wait_for(held.reached.wait(), timeout=60)
        # El DELETE está hecho y sin confirmar: la lectura no espera y ve la fila entera.
        during = await asyncio.wait_for(_get(stack, installer, photo.image_path), timeout=60)
        held.release.set()
        report = await asyncio.wait_for(purge, timeout=60)
        after = await asyncio.wait_for(_get(stack, installer, photo.image_path), timeout=60)
        return during, report, after

    during, report, after = stack.run(scenario())
    assert _complete_or_missing(during, photo) == "complete"
    assert report == PurgeReport(expired=(photo.photo_id,))
    assert _complete_or_missing(after, photo) == "missing"
    assert [e["outcome"] for e in image_audit(stack, photo.photo_id)] == ["success", "denied"]


def test_a_purge_committed_in_the_middle_of_a_read_leaves_the_read_whole(
    stack: FleetStack,
) -> None:
    site = stack.site()
    received = stack.authz.now()
    expires = received + AHEAD
    photo = _photo(stack, site, received_at=received, expires_at=expires - 2 * DAY)
    installer = stack.installer(site)
    services = getattr(stack.app.state, FLEET_STATE_KEY)
    held = HeldQueries(stack.database)
    paused = EpisodePhotoReader(
        database=stack.database,
        authorizer=stack.authz.authorizer,
        audit=stack.authz.sessions.audit,
        clock=stack.authz.sessions.clock,
        queries=held,
    )

    async def scenario() -> tuple[httpx.Response, PurgeReport]:
        read = asyncio.create_task(_get(stack, installer, photo.image_path))
        await asyncio.wait_for(held.reached.wait(), timeout=60)
        # La lectura tiene los bytes en su transacción; la purga confirma mientras tanto.
        report = await asyncio.wait_for(
            _run(stack, site.organization_id, _purger(stack, expires)), timeout=60
        )
        held.release.set()
        return await asyncio.wait_for(read, timeout=60), report

    setattr(stack.app.state, FLEET_STATE_KEY, _with_reader(services, paused))
    try:
        response, report = stack.run(scenario())
    finally:
        setattr(stack.app.state, FLEET_STATE_KEY, services)
    assert report == PurgeReport(expired=(photo.photo_id,))
    assert _complete_or_missing(response, photo) == "complete"
    assert _complete_or_missing(stack.run(_get(stack, installer, photo.image_path)), photo) == (
        "missing"
    )


def _with_reader(services: Any, reader: EpisodePhotoReader) -> Any:
    import dataclasses

    return dataclasses.replace(services, episode_photos=reader)


def test_reads_launched_together_with_the_purge_are_whole_or_not_found(
    stack: FleetStack,
) -> None:
    site = stack.site()
    received = stack.authz.now()
    expires = received + AHEAD
    photo = _photo(stack, site, received_at=received, expires_at=expires - 2 * DAY, padding=30_000)
    installer = stack.installer(site)

    async def scenario() -> tuple[list[httpx.Response], PurgeReport]:
        reads = [_get(stack, installer, photo.image_path) for _ in range(READERS)]
        purge = _run(stack, site.organization_id, _purger(stack, expires))
        *responses, report = await asyncio.wait_for(asyncio.gather(*reads, purge), timeout=120)
        return list(responses), report

    responses, report = stack.run(scenario())
    assert report == PurgeReport(expired=(photo.photo_id,))
    outcomes = [_complete_or_missing(response, photo) for response in responses]
    audit = [e["outcome"] for e in image_audit(stack, photo.photo_id)]
    # Una entrada por lectura, y success exactamente para las que recibieron los bytes.
    assert sorted(audit) == sorted(
        "success" if outcome == "complete" else "denied" for outcome in outcomes
    )


# --- Lo que queda en disco ----------------------------------------------------------------------


def test_after_the_purge_the_row_is_gone_and_vacuum_removes_its_tuples(
    stack: FleetStack,
) -> None:
    """Documenta lo que hace PostgreSQL: el ``DELETE`` deja tuplas muertas (con sus bytes) que
    ``VACUUM`` poda; ``gob_0043`` deja la limpieza automática a 50 tuplas muertas."""
    site = stack.site()
    received = stack.authz.now()
    expires = received + AHEAD
    photo = _photo(stack, site, received_at=received, expires_at=expires, padding=30_000)
    assert _purge(stack, site.organization_id, expires) == PurgeReport(expired=(photo.photo_id,))
    assert _alive(stack, photo) == set()
    (options,) = stack.fetch(
        "SELECT c.reloptions AS heap, t.reloptions AS toast FROM pg_class AS c"
        " JOIN pg_namespace AS n ON n.oid = c.relnamespace"
        " LEFT JOIN pg_class AS t ON t.oid = c.reltoastrelid"
        " WHERE n.nspname = 'fleet' AND c.relname = 'episode_photo'"
    )
    expected = {"autovacuum_vacuum_scale_factor=0", "autovacuum_vacuum_threshold=50"}
    assert set(options["heap"]) == expected
    assert set(options["toast"]) == expected


def _raw_hits(stack: FleetStack, needle: bytes) -> dict[str, int]:
    """Veces que ``needle`` aparece en las páginas crudas de la tabla y de su TOAST."""
    stack.execute("CREATE EXTENSION IF NOT EXISTS pageinspect")
    (row,) = stack.fetch(
        "SELECT t.oid::regclass::text AS toast FROM pg_class AS c JOIN pg_class AS t"
        " ON t.oid = c.reltoastrelid WHERE c.oid = 'fleet.episode_photo'::regclass"
    )
    hits: dict[str, int] = {}
    for label, relation in (("heap", "fleet.episode_photo"), ("toast", str(row["toast"]))):
        (size,) = stack.fetch("SELECT pg_relation_size($1::regclass) / 8192 AS pages", relation)
        pages = [
            bytes(found["page"])
            for number in range(int(size["pages"]))
            for found in stack.fetch(
                "SELECT get_raw_page($1::text, $2::bigint) AS page", relation, number
            )
        ]
        hits[label] = sum(page.count(needle) for page in pages)
    return hits


@pytest.mark.parametrize("padding", [0, 30_000], ids=["en-linea", "toast"])
def test_what_postgresql_keeps_of_a_purged_image_on_disk(stack: FleetStack, padding: int) -> None:
    """Documenta (criterio de A-112 «una imagen rechazada nunca toca la base») lo que queda de
    una imagen purgada, con fotos vivas detrás para que ``VACUUM`` no trunque la relación:

    - tras el ``DELETE``, los bytes siguen en la tupla muerta (en línea o en la TOAST);
    - una imagen en línea desaparece con ``VACUUM`` (la poda compacta la página);
    - una imagen en la TOAST (lo normal: 50 a 150 KB) deja sus trozos en páginas libres hasta que
      otra foto reutiliza el espacio o ``VACUUM FULL`` reescribe la tabla.
    """
    site = stack.site()
    received = stack.authz.now()
    expires = received + AHEAD
    # Sin huecos de pruebas anteriores: la purgada y las vivas se escriben en ese orden al final.
    stack.execute("VACUUM (FULL) fleet.episode_photo")
    purged = _photo(stack, site, received_at=received, expires_at=expires, padding=padding)
    alive = [
        _photo(stack, site, received_at=received, expires_at=expires + DAY, padding=padding)
        for _ in range(2)
    ]
    needle = (purged.data or b"")[-40:-2]
    where = "toast" if padding else "heap"
    assert _raw_hits(stack, needle)[where] == 1
    assert _purge(stack, site.organization_id, expires) == PurgeReport(expired=(purged.photo_id,))
    assert _raw_hits(stack, needle)[where] == 1
    stack.execute("VACUUM fleet.episode_photo")
    assert _raw_hits(stack, needle) == {"heap": 0, "toast": 1 if padding else 0}
    stack.execute("VACUUM (FULL) fleet.episode_photo")
    assert _raw_hits(stack, needle) == {"heap": 0, "toast": 0}
    assert _alive(stack, *alive) == {photo.photo_id for photo in alive}


# --- Fotos del hallazgo (TASK-588, A-128) ---------------------------------------------------------


def _purged_by_mode(reader: Any) -> dict[tuple[str, str], float]:
    return {
        (str(attributes["cause"]), str(attributes["zone_mode"])): value
        for attributes, value in metric_points(reader, MetricName.EPISODE_PHOTOS_PURGED_TOTAL)
    }


def test_a_finding_photo_survives_the_usage_gate_approved_or_reapproved_after_it_arrived(
    stack: FleetStack,
) -> None:
    """La compuerta de uso se aprueba (y se vuelve a aprobar) después de recibir las dos fotos: la
    de puesta en marcha se purga como ``productive``; la del hallazgo, nunca
    (``m_purga_productiva``)."""
    site = stack.site(plants=1, zones=1)
    plant, zone = _zone(site)
    received = stack.authz.now()
    expires = received + AHEAD
    finding = _photo(stack, site, received_at=received, expires_at=expires, zone_mode="productive")
    undated = _photo(stack, site, received_at=received, zone_mode="productive", no_expiry=True)
    shadow = _photo(stack, site, received_at=received, expires_at=expires)
    metrics, reader = metrics_with_reader()
    approved_at = received + DAY
    usage(stack, site.organization_id, plant, zone, approved=True, at=approved_at)
    report = _purge(stack, site.organization_id, approved_at, metrics)
    assert report == PurgeReport(retired=(shadow.photo_id,))
    assert _alive(stack, finding, undated, shadow) == {finding.photo_id, undated.photo_id}
    # Revocada y vuelta a aprobar: la del hallazgo sigue.
    usage(stack, site.organization_id, plant, zone, approved=False, at=approved_at + DAY)
    usage(stack, site.organization_id, plant, zone, approved=True, at=approved_at + 2 * DAY)
    assert _purge(stack, site.organization_id, approved_at + 2 * DAY, metrics) == PurgeReport()
    assert _alive(stack, finding, undated) == {finding.photo_id, undated.photo_id}
    assert _purged_by_mode(reader) == {("productive", "commissioning"): 1.0}


def test_with_zero_days_a_finding_photo_never_expires_ten_years_later(stack: FleetStack) -> None:
    """``VIGIA_FINDING_PHOTO_RETENTION_DAYS = 0``: ``expires_at`` nulo; el reloj inyectado 10 años
    después no la borra (``m_vence_sin_plazo``)."""
    site = stack.site()
    received = stack.authz.now()
    assert finding_expires_at(received, 0) is None
    finding = _photo(stack, site, received_at=received, zone_mode="productive", no_expiry=True)
    later = received + 3653 * DAY
    assert _purge(stack, site.organization_id, later) == PurgeReport()
    assert _alive(stack, finding) == {finding.photo_id}


def test_with_n_days_a_finding_photo_expires_exactly_at_n_days(stack: FleetStack) -> None:
    site = stack.site()
    received = stack.authz.now()
    due = finding_expires_at(received, 365)
    assert due == received + 365 * DAY
    finding = _photo(stack, site, received_at=received, expires_at=due, zone_mode="productive")
    metrics, reader = metrics_with_reader()
    assert _purge(stack, site.organization_id, due - MS, metrics) == PurgeReport()
    assert _alive(stack, finding) == {finding.photo_id}
    report = _purge(stack, site.organization_id, due, metrics)
    assert report == PurgeReport(expired=(finding.photo_id,))
    assert _alive(stack, finding) == set()
    assert _purged_by_mode(reader) == {("expired", "productive"): 1.0}


RACING: Final = 4
"""Fotos de cada modo que entran a la vez que la purga."""


def _race_row(site: Site, mode: ZoneMode, received: dt.datetime) -> EpisodePhotoRow:
    """Una fila sin imagen como la que la ingesta escribiría (TASK-603 usará ``claim`` así)."""
    plant, zone = _zone(site)
    photo_id = uuid.uuid4()
    productive = mode is ZoneMode.PRODUCTIVE
    return EpisodePhotoRow(
        organization_id=site.organization_id,
        photo_id=photo_id,
        plant_id=plant,
        zone_id=zone,
        node_id=uuid.uuid4(),
        episode_id=uuid.uuid4(),
        segment_index=0,
        classified_as="finding",
        family="coexistence",
        standard_id=uuid.uuid4(),
        standard_version=1,
        max_confidence=0.9,
        started_at=received - dt.timedelta(seconds=30),
        ended_at=received - dt.timedelta(seconds=26),
        received_at=received,
        expires_at=None if productive else expires_at(received, 30),
        camera_id=None,
        frame_at=None,
        width=None,
        height=None,
        size_bytes=None,
        sha256=None,
        blur_params_version=None,
        image=None,
        image_absent_reason="frame_unavailable",
        canonical_sha256=photo_id.hex * 2,
        platform_record_id=uuid.uuid4(),
        zone_mode=mode,
        record_id=uuid.uuid4() if productive else None,
        photo_index=0,
        photo_role=PhotoRole.PEAK,
    )


def test_a_purge_racing_inserts_never_deletes_a_finding_photo(stack: FleetStack) -> None:
    """Una purga que corre mientras entran fotos ``productive`` y ``commissioning`` de la misma zona
    (por ``PostgresEpisodePhotoStore.claim``, cada una en su transacción) no borra ninguna del
    hallazgo y deja cada una de puesta en marcha borrada o intacta."""
    site = stack.site(plants=1, zones=1)
    plant, zone = _zone(site)
    received = stack.authz.now()
    before = [
        _photo(stack, site, received_at=received, zone_mode=mode, no_expiry=mode == "productive")
        for mode in ("productive", "commissioning", "productive", "commissioning")
    ]
    usage(stack, site.organization_id, plant, zone, approved=True, at=received + MS)
    rows = [_race_row(site, mode, received) for mode in ZoneMode for _ in range(RACING)]
    store = PostgresEpisodePhotoStore(stack.database)

    async def insert(row: EpisodePhotoRow) -> bool:
        async with stack.database.transaction(system(site.organization_id)) as transaction:
            return await store.claim(transaction, row)

    async def scenario() -> tuple[PurgeReport, list[bool]]:
        purge = _run(stack, site.organization_id, _purger(stack, received + DAY))
        report, *inserted = await asyncio.wait_for(
            asyncio.gather(purge, *(insert(row) for row in rows)), timeout=60
        )
        return report, list(inserted)

    report, inserted = stack.run(scenario())
    assert inserted == [True] * len(rows)
    productive = {p.photo_id for p in before if p.zone_mode == "productive"} | {
        row.photo_id for row in rows if row.zone_mode is ZoneMode.PRODUCTIVE
    }
    commissioning = {p.photo_id for p in before if p.zone_mode == "commissioning"} | {
        row.photo_id for row in rows if row.zone_mode is ZoneMode.COMMISSIONING
    }
    assert set(report.retired) <= commissioning
    assert report.expired == ()
    remaining = {
        uuid.UUID(str(found["photo_id"])): found
        for found in stack.fetch(
            "SELECT photo_id, zone_mode, record_id::text AS record_id, expires_at,"
            " canonical_sha256"
            " FROM fleet.episode_photo WHERE zone_id = $1",
            zone,
        )
    }
    assert productive <= set(remaining)
    # Las de antes de la aprobación ya estaban: la purga las retira todas.
    assert {p.photo_id for p in before if p.zone_mode == "commissioning"} <= set(report.retired)
    for row in rows:
        if row.photo_id in remaining:
            found = remaining[row.photo_id]
            assert (
                found["zone_mode"],
                found["record_id"],
                found["expires_at"],
                found["canonical_sha256"],
            ) == (
                row.zone_mode.value,
                None if row.record_id is None else str(row.record_id),
                row.expires_at,
                row.canonical_sha256,
            )
        else:
            assert row.photo_id in report.retired
    assert set(remaining) | set(report.retired) == productive | commissioning
