"""Cierre del acta y reejecución sobre PostgreSQL 16 real (TASK-216, LC-GOB-08 y LC-GOB-09).

Servicios reales como ``vigia_app`` (``tests/close_record_support.py``) y la aplicación real para
las respuestas HTTP; el depósito es ``ClipStore``, que cuenta las llamadas a ``get_object`` (la
fixture ``nothing_is_downloaded`` exige cero en cada prueba).

- **El acta**: el cierre completo escribe ``CommissioningRecord``, ``walk_test_result`` y la sesión
  ``closed``; los cuatro tramos por separado con la suma orientativa; tasas por cámara del último
  latido (sin latido, nulo); ``GET /commissioning-records/{id}`` devuelve lo mismo, sin
  responsable.
- **Las siete guardas** en su orden: con la guarda ``k`` y todas las siguientes fallando a la vez
  (y con ``k`` sola), el error es el de ``k`` y no se escribe nada.
- **Latencia**: 99 pases más clips, ``latency_not_measured``; 100, cierra. Los clips de otra zona
  o de antes de la sesión no cuentan.
- **Difuminado**: sin clip, con el metadato distinto de ``1``, sin metadato, con otra suma o sin
  objeto, ``blur_not_verified``; el almacén caído, ``storage_unavailable`` y nada escrito.
- **Falsas alarmas**: por encima del umbral, solo con aceptación, que queda con autor y fecha.
- **Concurrencia** (barreras; ningún tope de pared decide): N cierres de la misma reejecución dejan
  un acta, un ``walk_test_result`` y un ``regression_cleared``; el cierre y una marca de regresión
  a la vez nunca se interbloquean (orden sesión → regresión → cadena).
- **Reejecución**: exige la regresión ``pending``; abre solo las filas afectadas; su cierre la
  devuelve a ``current`` salvo que otra marca llegue durante la reejecución.
- **Muestras de exposición**: una por pase, la repetida no cambia nada, pase ajeno
  ``catalog_pass_not_found``; alimentan el tramo 3a.
- **Alcance**: la sesión, el acta y la zona de otra organización o de otra planta fuera de la
  concesión responden ``not_found``.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, Final

import httpx
import pytest

from tests.close_record_support import ACCEPTANCE, CloseWorld, Zone, close_world
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.catalog.adapters.postgres.catalog_repository import PostgresCatalogRepository
from vigia_platform.catalog.adapters.postgres.commissioning_record_repository import (
    PostgresCommissioningRecordRepository,
)
from vigia_platform.catalog.adapters.postgres.occlusion_repository import (
    PostgresOcclusionRepository,
)
from vigia_platform.catalog.adapters.postgres.regression_repository import (
    PostgresRegressionRepository,
)
from vigia_platform.catalog.adapters.postgres.walk_test_repository import (
    PostgresWalkTestRepository,
)
from vigia_platform.catalog.application.exposure import ExposureRecorded
from vigia_platform.catalog.application.regression import RegressionService
from vigia_platform.catalog.application.walk_test import WalkTestConflict
from vigia_platform.catalog.domain.commissioning_record import CommissioningRecord
from vigia_platform.catalog.domain.latency import ExposureSample
from vigia_platform.catalog.domain.walk_test import WalkTestSession
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.db import Transaction

pytestmark = pytest.mark.integration

CONCURRENT: Final = 5
"""Transacciones a la vez (el pool de prueba tiene 8 conexiones)."""
BARRIER_SECONDS: Final = 60.0
"""Tope de la barrera (retro 15: nunca decide el resultado, solo evita colgar la corrida)."""
GUARDS: Final = (
    "steps_still_open",
    "matrix_incomplete",
    "false_negative_present",
    "redundancy_not_verified",
    "false_alarm_rate_above_threshold",
    "latency_not_measured",
    "blur_not_verified",
)
FRAMING: Final = "Se recapturó la línea base tras mover la cámara"


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[CloseWorld]:
    with close_world(postgres_endpoint, "close_record") as world:
        yield world


@pytest.fixture(autouse=True)
def nothing_is_downloaded(world: CloseWorld) -> Iterator[None]:
    yield
    # PAT-GOB-REN-05: el cierre verifica por metadatos; nunca descarga el clip.
    assert world.store.gets == 0


def _ok(response: httpx.Response, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    return body


def _error(response: httpx.Response, status: int, code: str, detail: str | None = None) -> None:
    assert response.status_code == status, response.text
    body = response.json()
    assert (body["code"], body.get("detail_code")) == (code, detail), body


class _Barrier:
    """Reúne ``parties`` corrutinas antes de la fase que se quiere hacer chocar."""

    def __init__(self, parties: int) -> None:
        self._barrier = asyncio.Barrier(parties)

    async def wait(self) -> None:
        async with asyncio.timeout(BARRIER_SECONDS):
            await self._barrier.wait()


def _rerun(world: CloseWorld, rows: Any = "all") -> Zone:
    """Una zona con su acta inicial cerrada, una regresión ``pending`` y la reejecución abierta,
    lista para cerrarse."""
    zone = world.zone()
    world.ready(zone)
    world.close(zone)
    world.regression(zone, rows, "catalog_change" if rows != "all" else "framing_recaptured")
    world.reopen_rerun(zone)
    world.ready(zone)
    return zone


# --- El acta -------------------------------------------------------------------------------------


def test_a_complete_close_writes_the_structured_record_and_closes_the_session(
    world: CloseWorld,
) -> None:
    zone = world.zone()
    world.ready(zone)
    measured, unmeasured = zone.cameras
    world.inventory(zone, measured, 12.5, 10.0)

    response = world.post_close(zone)

    body = _ok(response)
    assert "responsible" not in response.text  # H-53: el acta nunca nombra a un responsable
    session = zone.session
    assert (body["session_id"], body["kind"], body["passes_per_cell"]) == (
        str(session.session_id),
        "initial",
        3,
    )
    assert [r["row_id"] for r in body["matrix_results"]] == [
        str(row.row_id) for row in session.matrix_rows
    ]
    assert all(
        (r["detected"], r["missed"], r["false_alarms"]) == (3, 0, 0) for r in body["matrix_results"]
    )
    assert body["false_negatives_total"] == 0
    assert (body["false_alarm_rate_observed"], body["false_alarm_threshold"]) == (0.0, 0.0)
    assert body["false_alarm_acceptance"] is None
    latency = body["latency"]
    assert latency["node_tranche"] == {
        "median_ms": None,
        "p95_ms": 180,
        "max_ms": None,
        "repetitions": None,
        "measured_by": "installer",
    }
    assert latency["platform_tranche"] == {
        "median_ms": 400,
        "p95_ms": 400,
        "max_ms": 400,
        "repetitions": 80,
        "measured_by": "platform",
    }
    assert latency["exposure_tranche"] is None and latency["served_tranche"] is None
    assert latency["not_measured"] == ["exposure_tranche", "served_tranche"]
    assert latency["indicative"] is True
    assert (latency["indicative_sum_median_ms"], latency["indicative_sum_p95_ms"]) == (400, 580)
    assert latency["repetitions_counted"] == len(session.matrix_rows) * 3 + 80
    assert {
        c["camera_id"]: (c["measured_fps"], c["declared_min_fps"]) for c in body["cameras_measured"]
    } == {str(measured): (12.5, 10.0), str(unmeasured): (None, None)}
    assert {(o["camera_id"], o["verification"]) for o in body["occlusion_summary"]} == {
        (str(camera), "declared") for camera in zone.cameras
    }
    assert body["signatures"] == [
        {
            "user_id": str(zone.signer),
            "role_in_use": "coordinator_sst",
            "signed_at": body["closed_at"],
        }
    ]
    assert body["installer_measurements"]["measured_by"] == "installer"
    assert {b["camera_id"] for b in body["installer_measurements"]["baselines"]} == {
        str(camera) for camera in zone.cameras
    }
    # Lo escrito: acta, registro, sesión cerrada y el difuminado del clip que lo verificó.
    record_id = body["commissioning_record_id"]
    records, ledger, events, (status, _, session_record), regression, blurred = world.written(zone)
    assert (records, ledger, status, str(session_record)) == (
        1,
        ["walk_test_result"],
        "closed",
        record_id,
    )
    assert "regression_cleared" not in events and regression == [] and blurred == 1
    (result,) = world.results(zone)
    assert result["source_key"] == record_id
    assert result["content"]["latency"] == latency
    row = world.record_row(zone)
    assert str(row["ledger_record_id"]) == str(result["record_id"])
    # La lectura devuelve la misma acta.
    read = world.request("GET", f"/commissioning-records/{record_id}", zone.mounted)
    assert _ok(read) == body


def test_an_evidence_ref_without_a_zone_clip_is_listed_without_blocking(world: CloseWorld) -> None:
    zone = world.zone()
    world.occlusions_ok(zone)
    (clip,) = world.clips(zone, 1)
    world.clips(zone, 79)
    first, second, *rest = [row.row_id for row in zone.session.matrix_rows]
    stray = uuid.uuid4()
    world.passes(zone, rows=[first], evidence=clip)
    world.passes(zone, rows=[second], evidence=stray)
    world.passes(zone, rows=rest)
    world.advance()

    body = _ok(world.post_close(zone))

    refs = {r["row_id"]: r["unverifiable_evidence_refs"] for r in body["matrix_results"]}
    assert refs[str(first)] == [] and refs[str(second)] == [str(stray)]
    assert all(refs[str(row)] == [] for row in rest)


# --- Las siete guardas ---------------------------------------------------------------------------


def _with_failures(world: CloseWorld, zone: Zone, failing: set[int]) -> None:
    """La sesión con las guardas de ``failing`` (1 a 7) fallando y las demás pasando."""
    rows = [row.row_id for row in zone.session.matrix_rows]
    if 2 in failing:
        world.passes(zone, rows=rows[:1], per_row=2)
        world.passes(zone, rows=rows[1:])
    else:
        world.passes(zone)
    if 3 in failing:
        world.passes(zone, rows=rows[1:2], per_row=1, result="missed")
    if 5 in failing:
        world.passes(zone, rows=rows[2:3], per_row=1, result="false_alarm")
    for camera in zone.cameras[:1] if 4 in failing else zone.cameras:
        world.occlusion(zone, camera)
    world.clips(zone, 10 if 6 in failing else 80, anonymized="0" if 7 in failing else "1")
    if 1 in failing:
        world.walk.start(zone.mounted, zone.session.session_id)
    world.advance()


@pytest.mark.parametrize("alone", [False, True], ids=["with_the_later_ones", "alone"])
@pytest.mark.parametrize("first", range(1, 8), ids=GUARDS)
def test_the_seven_guards_fail_in_order_and_write_nothing(
    world: CloseWorld, first: int, alone: bool
) -> None:
    zone = world.zone()
    _with_failures(world, zone, {first} if alone else set(range(first, 8)))
    before, heads = world.written(zone), world.store.heads

    _error(world.post_close(zone), 409, "conflict", f"catalog_{GUARDS[first - 1]}")

    assert world.written(zone) == before
    # Las seis primeras deciden sin preguntar al almacén.
    assert (world.store.heads > heads) == (first == 7)


def test_with_no_guard_failing_the_same_session_closes(world: CloseWorld) -> None:
    zone = world.zone()
    _with_failures(world, zone, set())
    _ok(world.post_close(zone))


# --- Latencia ------------------------------------------------------------------------------------


@pytest.mark.parametrize(("clips", "closes"), [(75, False), (76, True)], ids=["99", "100"])
def test_latency_needs_one_hundred_passes_and_zone_clips_in_the_window(
    world: CloseWorld, clips: int, closes: bool
) -> None:
    site = world.walk.a.g.site(zones=2)
    zone = world.zone(site=site)
    (_, other_zone) = site.zones()[1]
    world.passes(zone)  # 8 filas por 3 pases
    world.occlusions_ok(zone)
    world.clips(zone, clips)
    # Ninguno de estos cuenta: otra zona de la planta y antes de abrir la sesión.
    world.clips(zone, 5, zone_id=other_zone, node=zone.node)
    world.clips(zone, 5, received_at=zone.session.started_at - timedelta(minutes=1))
    world.advance()

    response = world.post_close(zone)

    if closes:
        assert _ok(response)["latency"]["repetitions_counted"] == 24 + clips == 100
    else:
        _error(response, 409, "conflict", "catalog_latency_not_measured")


# --- Difuminado ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case", ["no_clip", "metadata_0", "no_metadata", "another_sha256", "no_object"]
)
def test_blur_needs_a_zone_clip_whose_head_says_anonymized(world: CloseWorld, case: str) -> None:
    zone = world.zone(passes_per_cell=13)  # 104 pases: la latencia no depende de los clips
    world.passes(zone)
    world.occlusions_ok(zone)
    if case == "metadata_0":
        world.clips(zone, 3, anonymized="0")
    elif case == "no_metadata":
        world.clips(zone, 3, anonymized=None)
    elif case == "no_object":
        world.clips(zone, 3, stored=False)
    elif case == "another_sha256":
        world.clips(zone, 3, stored=False)
        for row in world.fetch(
            "SELECT g.storage_key FROM fleet.clip_upload_grant AS g WHERE g.zone_id = $1",
            zone.zone_id,
        ):
            world.store.put(row["storage_key"], b"otros bytes", "1")
    world.advance()
    before = world.written(zone)

    _error(world.post_close(zone), 409, "conflict", "catalog_blur_not_verified")

    assert world.written(zone) == before


def test_with_the_store_down_the_close_is_transient_and_writes_nothing(world: CloseWorld) -> None:
    zone = world.zone()
    world.ready(zone)
    before = world.written(zone)
    world.store.down = True
    try:
        response = world.post_close(zone)
    finally:
        world.store.down = False

    _error(response, 503, "storage_unavailable")
    assert world.written(zone) == before
    _ok(world.post_close(zone))  # vuelto el almacén, el mismo cierre pasa


# --- Falsas alarmas ------------------------------------------------------------------------------


def test_false_alarms_above_the_threshold_close_only_with_an_acceptance(
    world: CloseWorld,
) -> None:
    zone = world.zone()
    world.ready(zone)
    world.passes(zone, rows=[zone.session.matrix_rows[0].row_id], per_row=1, result="false_alarm")
    world.advance()
    _error(world.post_close(zone), 409, "conflict", "catalog_false_alarm_rate_above_threshold")

    body = _ok(world.post_close(zone, acceptance=ACCEPTANCE))

    assert body["false_alarm_rate_observed"] == pytest.approx(1 / 25)
    assert body["false_alarm_threshold"] == 0.0
    assert body["false_alarm_acceptance"] == {
        "reason_es": ACCEPTANCE,
        "accepted_by": str(zone.installer.actor.id),
        "accepted_at": body["closed_at"],
    }
    assert json.loads(world.record_row(zone)["acceptance"])["reason_es"] == ACCEPTANCE
    (result,) = world.results(zone)
    assert result["content"]["false_alarm_acceptance"] == body["false_alarm_acceptance"]


@pytest.mark.parametrize("reason", ["corto", "   ...   ", "​" * 12])
def test_an_acceptance_that_is_no_reason_is_rejected(world: CloseWorld, reason: str) -> None:
    zone = world.zone()
    world.ready(zone)
    world.passes(zone, rows=[zone.session.matrix_rows[0].row_id], per_row=1, result="false_alarm")
    before = world.written(zone)
    _error(
        world.post_close(zone, acceptance=reason),
        400,
        "invalid_request",
        "catalog_free_text_rejected",
    )
    assert world.written(zone) == before


def test_a_signer_without_scope_on_the_zone_is_invalid_request(world: CloseWorld) -> None:
    site = world.walk.a.g.site(plants=2)
    zone = world.zone(site=site)
    world.ready(zone)
    before = world.written(zone)
    other_plant = list(site.plants)[1]
    signers = world.walk.a
    elsewhere = signers.signer(site, Role.PLANT_MANAGER, ScopeLevel.PLANT, other_plant).user_id
    stranger = signers.signer(world.walk.a.g.site(), Role.COORDINATOR_SST).user_id
    for chosen in ([elsewhere], [stranger], [zone.signer, zone.signer]):
        _error(world.post_close(zone, signers=chosen), 400, "invalid_request")
    assert world.written(zone) == before


# --- Concurrencia y orden de candados ------------------------------------------------------------


class _MeetBeforeSessionLock(PostgresWalkTestRepository):
    """Todos los cierres pasaron sus comprobaciones previas antes de tomar el candado."""

    def __init__(self, barrier: _Barrier) -> None:
        self._meet = barrier

    async def lock_session(
        self, transaction: Transaction, session_id: uuid.UUID
    ) -> WalkTestSession | None:
        await self._meet.wait()
        return await super().lock_session(transaction, session_id)


def test_concurrent_closes_leave_one_record_one_result_and_one_clearance(
    world: CloseWorld,
) -> None:
    zone = _rerun(world)
    racing = world.build(sessions=_MeetBeforeSessionLock(_Barrier(CONCURRENT)))
    request = world.request_of(zone)
    world.advance()

    async def race() -> list[Any]:
        return await asyncio.gather(
            *(
                racing.close(zone.installer, zone.session.session_id, request)
                for _ in range(CONCURRENT)
            ),
            return_exceptions=True,
        )

    outcomes = world.run(race())

    closed = [o for o in outcomes if isinstance(o, CommissioningRecord)]
    others = [o for o in outcomes if not isinstance(o, CommissioningRecord)]
    assert len(closed) == 1, outcomes
    assert all(isinstance(o, WalkTestConflict) for o in others), others
    rerun = str(zone.session.session_id)
    assert [r for r in world.results(zone) if r["content"]["session_id"] == rerun] != []
    assert len([r for r in world.results(zone) if r["content"]["session_id"] == rerun]) == 1
    assert len(world.results(zone, "walk_test_regression_cleared")) == 1
    assert len(world.events(zone, "regression_cleared")) == 1
    (row,) = world.fetch(
        "SELECT count(*) AS n FROM catalog.commissioning_record WHERE session_id = $1",
        zone.session.session_id,
    )
    assert row["n"] == 1


class _HoldFirstLock:
    """Cada operación, al obtener su **primer** candado, espera a que la otra tenga el suyo: así
    el cierre (sesión → regresión → cadena) y la marca (regresión → cadena) se cruzan de verdad."""

    def __init__(self) -> None:
        self.barrier = _Barrier(2)
        self.holding: set[asyncio.Task[Any]] = set()

    async def first(self) -> None:
        task = asyncio.current_task()
        assert task is not None
        if task in self.holding:
            return
        self.holding.add(task)
        await self.barrier.wait()


class _HoldingSessions(PostgresWalkTestRepository):
    def __init__(self, hold: _HoldFirstLock) -> None:
        self._hold = hold

    async def lock_session(
        self, transaction: Transaction, session_id: uuid.UUID
    ) -> WalkTestSession | None:
        session = await super().lock_session(transaction, session_id)
        await self._hold.first()
        return session


class _HoldingRegressions(PostgresRegressionRepository):
    def __init__(self, database: Any, hold: _HoldFirstLock) -> None:
        super().__init__(database)
        self._hold = hold

    async def lock(self, transaction: Transaction, zone_id: uuid.UUID) -> None:
        await super().lock(transaction, zone_id)
        await self._hold.first()


def test_a_close_and_a_regression_mark_at_once_never_deadlock(world: CloseWorld) -> None:
    zone = _rerun(world)
    hold = _HoldFirstLock()
    g = world.walk.a.g
    closing = world.build(sessions=_HoldingSessions(hold))
    marking = RegressionService(
        repository=_HoldingRegressions(g.database, hold),
        catalog=PostgresCatalogRepository(g.database),
        database=g.database,
        writer=g.writer,
        authorizer=g.authz.authorizer,
        audit=g.authz.sessions.audit,
        free_text=g.free_text,
        clock=world.clock,
    )
    request = world.request_of(zone)
    world.advance()

    async def race() -> list[Any]:
        return await asyncio.gather(
            closing.close(zone.installer, zone.session.session_id, request),
            marking.mark_framing_recaptured(zone.installer, zone.zone_id, zone.cameras[0], FRAMING),
            return_exceptions=True,
        )

    record, mark = world.run(race())

    # Ningún interbloqueo ni transitorio: los dos confirman.
    assert isinstance(record, CommissioningRecord), record
    assert not isinstance(mark, BaseException), mark
    # La marca llegó durante la reejecución: la regresión sigue pending.
    (regression,) = world.fetch(
        "SELECT state FROM catalog.walk_test_regression WHERE zone_id = $1", zone.zone_id
    )
    assert regression["state"] == "pending"
    assert world.results(zone, "walk_test_regression_cleared") == []


# --- Reejecución ---------------------------------------------------------------------------------


def test_a_rerun_needs_a_pending_regression_and_the_opening_guards(world: CloseWorld) -> None:
    zone = world.zone()
    path = f"/zones/{zone.zone_id}/walk-tests/regression-rerun"
    world.regression(zone, "all")
    # Con la sesión inicial abierta: la guarda de apertura de VIG-150.
    _error(
        world.request("POST", path, zone.mounted, {"passes_per_cell": 3}),
        409,
        "conflict",
        "catalog_walk_test_in_progress",
    )
    world.ready(zone)
    world.close(zone)
    world.execute(
        "UPDATE catalog.walk_test_regression SET state = 'current', marked_at = NULL,"
        " cause = NULL, affected_row_ids = NULL WHERE zone_id = $1",
        zone.zone_id,
    )
    # Sin regresión pending: conflict sin detail_code, antes que cualquier otra guarda.
    _error(world.request("POST", path, zone.mounted, {"passes_per_cell": 2}), 409, "conflict")


def test_a_rerun_opens_only_the_affected_rows_and_the_zone_keeps_operating(
    world: CloseWorld,
) -> None:
    zone = world.zone()
    world.ready(zone)
    world.close(zone)
    affected = [row.row_id for row in zone.session.matrix_rows[:4]]
    world.regression(zone, affected, "catalog_change")
    (gates,) = world.fetch(
        "SELECT resulting_mode, mounting::text AS mounting FROM catalog.zone_gate_state"
        " WHERE zone_id = $1",
        zone.zone_id,
    )

    body = _ok(
        world.request(
            "POST",
            f"/zones/{zone.zone_id}/walk-tests/regression-rerun",
            zone.mounted,
            {"passes_per_cell": 3},
        ),
        201,
    )

    assert body["kind"] == "regression_rerun" and body["status"] == "in_progress"
    assert [row["row_id"] for row in body["rows"]] == [str(row) for row in affected]
    (after,) = world.fetch(
        "SELECT resulting_mode, mounting::text AS mounting FROM catalog.zone_gate_state"
        " WHERE zone_id = $1",
        zone.zone_id,
    )
    assert tuple(after) == tuple(gates)  # BR-GOB-53: la regresión no toca las compuertas
    (row,) = world.fetch(
        "SELECT state FROM catalog.walk_test_regression WHERE zone_id = $1", zone.zone_id
    )
    assert row["state"] == "pending"


def test_closing_a_rerun_that_covers_the_rows_returns_the_zone_to_current(
    world: CloseWorld,
) -> None:
    zone = world.zone()
    world.ready(zone)
    world.close(zone)
    affected = [row.row_id for row in zone.session.matrix_rows[:4]]
    marked = world.regression(zone, affected, "catalog_change")
    world.reopen_rerun(zone)
    world.ready(zone, clips=90)  # 4 filas por 3 pases: los clips completan las 100

    record = world.close(zone)

    (row,) = world.fetch(
        "SELECT state, cleared_at, cleared_by_session_id, ledger_record_id"
        " FROM catalog.walk_test_regression WHERE zone_id = $1",
        zone.zone_id,
    )
    assert row["state"] == "current" and row["cleared_at"] == record.closed_at
    assert row["cleared_by_session_id"] == zone.session.session_id
    (cleared,) = world.results(zone, "walk_test_regression_cleared")
    assert row["ledger_record_id"] == cleared["record_id"] != marked
    assert cleared["content"] == {
        "zone_id": str(zone.zone_id),
        "cleared_by_session_id": str(zone.session.session_id),
        "cleared_at": record.record_content()["closed_at"],
    }
    (event,) = world.events(zone, "regression_cleared")
    assert event == {
        "zone_id": str(zone.zone_id),
        "cleared_by_session_id": str(zone.session.session_id),
        "cause": "catalog_change",
        "catalog_version": 1,
        "model_version": None,
        "affected_row_ids": [str(row) for row in sorted(affected)],
    }
    assert record.regression_cleared


def test_a_mark_during_the_rerun_leaves_the_regression_pending(world: CloseWorld) -> None:
    zone = _rerun(world)
    world.regression(zone, "all")  # una marca nueva después de abrir la reejecución

    record = world.close(zone)

    assert not record.regression_cleared
    (row,) = world.fetch(
        "SELECT state FROM catalog.walk_test_regression WHERE zone_id = $1", zone.zone_id
    )
    assert row["state"] == "pending"
    assert world.results(zone, "walk_test_regression_cleared") == []


# --- Oclusión: la última prueba no depende del orden de los UUID v7 ------------------------------


def test_the_last_occlusion_test_does_not_depend_on_the_uuid_order(world: CloseWorld) -> None:
    zone = world.zone()
    world.passes(zone)
    world.clips(zone, 80)
    blocked, other = zone.cameras
    world.occlusion(zone, other)
    # Una prueba declared nueva con un test_id menor que el de la failed anterior (UUID v7 de
    # otra instancia con el reloj atrasado).
    world.occlusion(
        zone, blocked, "failed", test_id=uuid.UUID("ffffffff-ffff-7fff-bfff-ffffffffffff")
    )
    newest = world.occlusion(
        zone, blocked, "declared", test_id=uuid.UUID("00000000-0000-7000-8000-000000000001")
    )
    world.advance()

    body = _ok(world.post_close(zone))

    summary = {o["camera_id"]: (o["verification"], o["test_id"]) for o in body["occlusion_summary"]}
    assert summary[str(blocked)] == ("declared", str(newest))

    async def latest() -> Any:
        async with world.walk.a.g.database.transaction(zone.installer) as transaction:
            return await PostgresOcclusionRepository().latest(
                transaction, zone.session.session_id, blocked
            )

    assert world.run(latest()).test_id == newest


# --- Muestras de exposición ----------------------------------------------------------------------


def _sample(
    world: CloseWorld, zone: Zone, pass_id: uuid.UUID, fetched_ms: int = 0, shown_ms: int = 120
) -> httpx.Response:
    base = zone.session.started_at + timedelta(days=3)  # el reloj del navegador va por libre
    return world.request(
        "POST",
        f"/walk-tests/{zone.session.session_id}/exposure-samples",
        zone.mounted,
        {
            "pass_id": str(pass_id),
            "fetched_at": (base + timedelta(milliseconds=fetched_ms)).isoformat(),
            "displayed_at": (base + timedelta(milliseconds=shown_ms)).isoformat(),
        },
    )


def test_one_sample_per_pass_and_a_repeated_one_changes_nothing(world: CloseWorld) -> None:
    zone = world.zone()
    (pass_id,) = world.passes(zone, rows=[zone.session.matrix_rows[0].row_id], per_row=1)

    first = _ok(_sample(world, zone, pass_id), 201)
    again = _ok(_sample(world, zone, pass_id, 5, 900), 200)

    assert again == first
    rows = world.fetch("SELECT sample_id FROM catalog.exposure_sample WHERE pass_id = $1", pass_id)
    assert [str(r["sample_id"]) for r in rows] == [first["sample_id"]]


def test_a_pass_of_another_session_is_pass_not_found(world: CloseWorld) -> None:
    zone, other = world.zone(), world.zone()
    (foreign,) = world.passes(other, rows=[other.session.matrix_rows[0].row_id], per_row=1)

    _error(_sample(world, zone, foreign), 400, "invalid_request", "catalog_pass_not_found")
    _error(_sample(world, zone, uuid.uuid4()), 400, "invalid_request", "catalog_pass_not_found")
    assert world.fetch("SELECT 1 FROM catalog.exposure_sample WHERE pass_id = $1", foreign) == []


def test_a_sample_displayed_before_it_was_fetched_is_invalid(world: CloseWorld) -> None:
    zone = world.zone()
    (pass_id,) = world.passes(zone, rows=[zone.session.matrix_rows[0].row_id], per_row=1)
    _error(_sample(world, zone, pass_id, 100, 99), 400, "invalid_request")
    _ok(_sample(world, zone, pass_id, 100, 100), 201)  # el borde: igual vale


class _MeetBeforeInsert(PostgresCommissioningRecordRepository):
    """Todas las muestras vieron el pase sin muestra antes de insertar la suya."""

    def __init__(self, barrier: _Barrier) -> None:
        self._meet = barrier

    async def insert_sample(self, transaction: Transaction, sample: ExposureSample) -> bool:
        await self._meet.wait()
        return await super().insert_sample(transaction, sample)


def test_concurrent_samples_of_one_pass_leave_one_row(world: CloseWorld) -> None:
    zone = world.zone()
    (pass_id,) = world.passes(zone, rows=[zone.session.matrix_rows[0].row_id], per_row=1)
    racing = world.build_exposures(repository=_MeetBeforeInsert(_Barrier(CONCURRENT)))
    base = zone.session.started_at

    async def race() -> list[Any]:
        return await asyncio.gather(
            *(
                racing.record(
                    zone.installer,
                    zone.session.session_id,
                    pass_id,
                    base,
                    base + timedelta(milliseconds=10 * (index + 1)),
                )
                for index in range(CONCURRENT)
            ),
            return_exceptions=True,
        )

    outcomes = world.run(race())

    assert all(isinstance(o, ExposureRecorded) for o in outcomes), outcomes
    assert sum(o.created for o in outcomes) == 1
    assert len({o.sample.sample_id for o in outcomes}) == 1
    rows = world.fetch("SELECT 1 FROM catalog.exposure_sample WHERE pass_id = $1", pass_id)
    assert len(rows) == 1


def test_samples_feed_the_exposure_tranche_with_the_browser_clock(world: CloseWorld) -> None:
    zone = world.zone()
    world.ready(zone)
    passes = world.fetch(
        "SELECT pass_id FROM catalog.walk_test_pass WHERE session_id = $1 ORDER BY recorded_at",
        zone.session.session_id,
    )
    for index, row in enumerate(passes[:20]):
        _ok(_sample(world, zone, row["pass_id"], 0, 100 + index), 201)

    latency = _ok(world.post_close(zone))["latency"]

    assert latency["exposure_tranche"] == {
        "median_ms": 109,
        "p95_ms": 118,
        "max_ms": 119,
        "repetitions": 20,
        "measured_by": "browser",
    }
    assert latency["not_measured"] == ["served_tranche"]
    # Cerrada la sesión, ya no admite muestras nuevas.
    _error(_sample(world, zone, passes[20]["pass_id"]), 409, "conflict")


def test_served_clips_feed_the_served_tranche(world: CloseWorld) -> None:
    zone = world.zone()
    world.passes(zone)
    world.occlusions_ok(zone)
    world.clips(zone, 80, served=True)
    world.advance()

    latency = _ok(world.post_close(zone))["latency"]

    assert latency["served_tranche"] == {
        "median_ms": 150,
        "p95_ms": 150,
        "max_ms": 150,
        "repetitions": 80,
        "measured_by": "platform",
    }


# --- Alcance -------------------------------------------------------------------------------------


def test_another_organizations_session_record_and_zone_are_not_found(world: CloseWorld) -> None:
    mine, theirs = world.zone(), world.zone()
    world.ready(theirs)
    record = world.close(theirs)
    (pass_id,) = world.passes(theirs, rows=[theirs.session.matrix_rows[0].row_id], per_row=1)
    before = world.written(theirs)

    for path, body in (
        (f"/walk-tests/{theirs.session.session_id}/close", world.body(theirs)),
        (
            f"/walk-tests/{theirs.session.session_id}/exposure-samples",
            {
                "pass_id": str(pass_id),
                "fetched_at": "2026-10-07T10:00:00.000Z",
                "displayed_at": "2026-10-07T10:00:00.100Z",
            },
        ),
        (f"/zones/{theirs.zone_id}/walk-tests/regression-rerun", {"passes_per_cell": 3}),
    ):
        _error(world.request("POST", path, mine.mounted, body), 404, "not_found")
    _error(
        world.request(
            "GET", f"/commissioning-records/{record.commissioning_record_id}", mine.mounted
        ),
        404,
        "not_found",
    )
    assert world.written(theirs) == before


def test_a_plant_concession_does_not_reach_a_zone_of_the_other_plant(world: CloseWorld) -> None:
    site = world.walk.a.g.site(plants=2)
    (plant_a, _), (_, zone_b) = site.zones()
    other = world.zone(site=site, index=1)
    world.ready(other)
    record = world.close(other)
    _, cookie, concession = world.walk.a.installer_session(site, ScopeLevel.PLANT, plant_a)
    outsider = type(other.mounted)(
        site, plant_a, other.zone_id, other.installer, cookie, concession, {}
    )

    _error(
        world.request("GET", f"/commissioning-records/{record.commissioning_record_id}", outsider),
        404,
        "not_found",
    )
    _error(
        world.request(
            "POST",
            f"/zones/{zone_b}/walk-tests/regression-rerun",
            outsider,
            {"passes_per_cell": 3},
        ),
        404,
        "not_found",
    )


def test_reading_the_record_under_concession_is_audited(world: CloseWorld) -> None:
    zone = world.zone()
    world.ready(zone)
    record = world.close(zone)
    count = len(
        world.fetch(
            "SELECT 1 FROM shared.audit_entry WHERE operation = 'catalog_read'"
            " AND actor_concession_id = $1",
            zone.mounted.concession,
        )
    )

    _ok(
        world.request(
            "GET", f"/commissioning-records/{record.commissioning_record_id}", zone.mounted
        )
    )

    rows = world.fetch(
        "SELECT scope_zone_id FROM shared.audit_entry WHERE operation = 'catalog_read'"
        " AND actor_concession_id = $1",
        zone.mounted.concession,
    )
    assert len(rows) == count + 1 and rows[-1]["scope_zone_id"] == zone.zone_id
