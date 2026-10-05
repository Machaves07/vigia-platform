"""Sesión de walk-test sobre PostgreSQL 16 real (TASK-214, LC-GOB-06; BR-GOB-35 a 38, 44 a 47).

Servicios reales como ``vigia_app`` (``tests/walk_test_support.py``) y la aplicación real para las
respuestas HTTP:

- **Apertura**: matriz derivada del catálogo vigente; guardas en orden (``mounting_gate_pending``,
  ``node_not_assigned``, ``walk_test_in_progress``, ``passes_below_minimum``); una sesión vencida
  deja de bloquear y queda ``incomplete``.
- **Concurrencia**: N aperturas simultáneas dejan una sesión (índice único parcial); N cierres
  simultáneos del mismo paso dejan un ``ended_at`` y un ``commissioning_step``; una apertura y una
  reapertura simultáneas de la misma zona dejan una sola sesión abierta y nunca un transitorio.
  Las tres fuerzan el choque con una barrera que reúne a todas las transacciones en la fase
  decisiva (ningún tope de pared decide el resultado).
- **Pasos**: reloj del servidor; corrección anexa que conserva las marcas; responsables.
- **Pases**: solo anexar; fila ajena a la matriz, ``invalid_request``.
- **Inactividad y reapertura**: a los 7 días la operación responde ``walk_test_incomplete`` y la
  sesión queda ``incomplete``; la reapertura solo sale de ``incomplete``, con motivo en la sesión y
  en la auditoría, y conserva los pases.
- **Alcance**: otra organización o una zona fuera de la concesión, ``not_found``.
- **NFR-GOB-03**: ``GET …/current`` hace el mismo número de consultas con 4 filas que con 32.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any, Final

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.walk_test_support import (
    CORRECTION,
    REASON,
    Mounted,
    WalkTestWorld,
    walk_test_world,
)
from vigia_platform.catalog.adapters.postgres.walk_test_repository import (
    PostgresWalkTestRepository,
)
from vigia_platform.catalog.application.admission import CatalogRejected
from vigia_platform.catalog.application.walk_test import (
    WalkTestConflict,
    WalkTestView,
)
from vigia_platform.catalog.detail_codes import CatalogDetailCode
from vigia_platform.catalog.domain.matrix import derive_matrix
from vigia_platform.catalog.domain.steps import WalkTestStep
from vigia_platform.catalog.domain.walk_test import INACTIVITY_LIMIT, WalkTestSession
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.shared.context import Role, ScopeContext, ScopeLevel
from vigia_platform.shared.db import Database, Transaction
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration

CONCURRENT: Final = 5
"""Transacciones a la vez (el pool de prueba tiene 8 conexiones)."""
BARRIER_SECONDS: Final = 60.0
"""Tope de la barrera (retro 15: nunca decide el resultado, solo evita colgar la corrida)."""


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[WalkTestWorld]:
    with walk_test_world(postgres_endpoint, "walk_test") as world:
        yield world


def _code(error: pytest.ExceptionInfo[CatalogRejected]) -> CatalogDetailCode:
    return error.value.detail_code


def _me(context: ScopeContext) -> uuid.UUID:
    return uuid.UUID(str(context.actor.id))


def _ok(response: Any, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    return body


def _error(response: Any, status: int, code: str, detail_code: str | None = None) -> None:
    assert response.status_code == status, response.text
    body = response.json()
    assert (body["code"], body.get("detail_code")) == (code, detail_code), body


class _Barrier:
    """Reúne ``parties`` corrutinas antes de la fase que se quiere hacer chocar."""

    def __init__(self, parties: int) -> None:
        self._barrier = asyncio.Barrier(parties)

    async def wait(self) -> None:
        async with asyncio.timeout(BARRIER_SECONDS):
            await self._barrier.wait()


class _MeetBeforeInsert(PostgresWalkTestRepository):
    """Todas las aperturas pasan su comprobación previa antes de que ninguna inserte."""

    def __init__(self, barrier: _Barrier) -> None:
        self._meet = barrier

    async def insert_open(self, transaction: Transaction, session: WalkTestSession) -> bool:
        await self._meet.wait()
        return await super().insert_open(transaction, session)


class _MeetBeforeLock(PostgresWalkTestRepository):
    """Todos los cierres llegan a la vez al candado de la sesión."""

    def __init__(self, barrier: _Barrier) -> None:
        self._meet = barrier

    async def lock_session(
        self, transaction: Transaction, session_id: uuid.UUID
    ) -> WalkTestSession | None:
        await self._meet.wait()
        return await super().lock_session(transaction, session_id)


class _MeetBeforeWrite(PostgresWalkTestRepository):
    """La apertura y la reapertura escriben a la vez, tras sus comprobaciones previas."""

    def __init__(self, barrier: _Barrier) -> None:
        self._meet = barrier

    async def insert_open(self, transaction: Transaction, session: WalkTestSession) -> bool:
        await self._meet.wait()
        return await super().insert_open(transaction, session)

    async def reopen(
        self, transaction: Transaction, seen: WalkTestSession, reopened: WalkTestSession
    ) -> None:
        await self._meet.wait()
        await super().reopen(transaction, seen, reopened)


# --- Apertura ------------------------------------------------------------------------------------


def test_open_derives_the_matrix_from_the_current_catalog(world: WalkTestWorld) -> None:
    mounted = world.mounted(count=3)
    body = _ok(
        world.request("POST", f"/zones/{mounted.zone}/walk-tests", mounted, {"passes_per_cell": 4}),
        201,
    )
    expected = derive_matrix(mounted.catalog)
    assert [row["row_id"] for row in body["rows"]] == [str(row.row_id) for row in expected]
    assert len(body["rows"]) == 12
    assert {row["required_passes"] for row in body["rows"]} == {4}
    assert {"presence": True} in body["rows"][0]["predicate_conditions"]
    assert (body["status"], body["kind"], body["catalog_version"]) == ("in_progress", "initial", 1)
    assert body["started_at"] == body["last_activity_at"]
    assert (body["steps"], body["passes"], body["total_duration_ms"]) == ([], [], 0)
    row = world.session_row(uuid.UUID(body["session_id"]))
    (node,) = world.fetch(
        "SELECT node_id FROM identity.zone_node_assignment WHERE zone_id = $1", mounted.zone
    )
    assert (row["status"], row["rows"], row["node_id"]) == ("in_progress", 12, node["node_id"])


def test_open_without_mounting_is_mounting_gate_pending_first(world: WalkTestWorld) -> None:
    mounted = world.mounted(mount=False, node=False)
    # Montaje pendiente, sin nodo y con 2 pases: la primera guarda es la del montaje.
    _error(
        world.request("POST", f"/zones/{mounted.zone}/walk-tests", mounted, {"passes_per_cell": 2}),
        409,
        "conflict",
        "catalog_mounting_gate_pending",
    )
    assert world.open_sessions(mounted.zone) == []


def test_open_without_node_is_node_not_assigned(world: WalkTestWorld) -> None:
    mounted = world.mounted(node=False)
    _error(
        world.request("POST", f"/zones/{mounted.zone}/walk-tests", mounted, {"passes_per_cell": 2}),
        409,
        "conflict",
        "catalog_node_not_assigned",
    )
    assert world.open_sessions(mounted.zone) == []


@pytest.mark.parametrize(("passes", "status"), [(2, 400), (0, 400), (-3, 400)])
def test_fewer_than_three_passes_is_passes_below_minimum(
    world: WalkTestWorld, passes: int, status: int
) -> None:
    mounted = world.mounted()
    _error(
        world.request(
            "POST", f"/zones/{mounted.zone}/walk-tests", mounted, {"passes_per_cell": passes}
        ),
        status,
        "invalid_request",
        "catalog_passes_below_minimum",
    )
    assert world.open_sessions(mounted.zone) == []


@pytest.mark.parametrize("body", [{"passes_per_cell": 1001}, {"passes_per_cell": "3"}, {}])
def test_passes_out_of_shape_is_invalid_request(world: WalkTestWorld, body: Any) -> None:
    mounted = world.mounted()
    _error(
        world.request("POST", f"/zones/{mounted.zone}/walk-tests", mounted, body),
        400,
        "invalid_request",
    )


def test_an_open_session_blocks_a_second_one_before_the_passes_guard(
    world: WalkTestWorld,
) -> None:
    mounted = world.mounted()
    world.open(mounted)
    for passes in (3, 2):
        _error(
            world.request(
                "POST", f"/zones/{mounted.zone}/walk-tests", mounted, {"passes_per_cell": passes}
            ),
            409,
            "conflict",
            "catalog_walk_test_in_progress",
        )
    assert len(world.open_sessions(mounted.zone)) == 1


def test_a_session_inactive_for_seven_days_stops_blocking(world: WalkTestWorld) -> None:
    mounted = world.mounted()
    stale = world.open(mounted)
    world.age(stale.session_id, INACTIVITY_LIMIT)
    fresh = world.open(mounted)
    assert world.session_row(stale.session_id)["status"] == "incomplete"
    assert [r["session_id"] for r in world.open_sessions(mounted.zone)] == [fresh.session_id]


def test_concurrent_opens_leave_a_single_session(world: WalkTestWorld) -> None:
    mounted = world.mounted()
    service = world.build(repository=_MeetBeforeInsert(_Barrier(CONCURRENT)))

    async def race() -> list[Any]:
        return await asyncio.gather(
            *(service.open(mounted.installer, mounted.zone, 3) for _ in range(CONCURRENT)),
            return_exceptions=True,
        )

    world.advance()
    results = world.run(race())
    opened = [r for r in results if isinstance(r, WalkTestSession)]
    rejected = [r for r in results if isinstance(r, CatalogRejected)]
    assert len(opened) == 1, results
    assert len(rejected) == CONCURRENT - 1, results
    assert {r.detail_code for r in rejected} == {CatalogDetailCode.WALK_TEST_IN_PROGRESS}
    assert [r["session_id"] for r in world.open_sessions(mounted.zone)] == [opened[0].session_id]


# --- Pasos ---------------------------------------------------------------------------------------


def test_steps_are_timed_by_the_server_and_corrections_keep_the_marks(
    world: WalkTestWorld,
) -> None:
    mounted = world.mounted()
    session = world.open(mounted)
    me = str(mounted.installer.actor.id)
    opened = _ok(
        world.request(
            "POST",
            f"/walk-tests/{session.session_id}/steps",
            mounted,
            {"step_kind": "physical_setup", "responsible_user_id": me},
        ),
        201,
    )
    assert (opened["ended_at"], opened["correction"], opened["effective_duration_ms"]) == (
        None,
        None,
        None,
    )
    world.advance(600)
    step_id = opened["step_id"]
    # Se olvidó abrirlo: diez minutos antes de la marca del servidor.
    earlier = format_timestamp(datetime.fromisoformat(opened["started_at"]) - timedelta(minutes=10))
    closed = _ok(
        world.request(
            "POST",
            f"/walk-tests/{session.session_id}/steps/{step_id}/close",
            mounted,
            {"correction": {"started_at": earlier, "reason_es": CORRECTION}},
        )
    )
    assert closed["started_at"] == opened["started_at"]  # la marca del servidor sigue visible
    assert closed["ended_at"] is not None
    correction = closed["correction"]
    assert (correction["started_at"], correction["ended_at"]) == (earlier, None)
    assert (correction["reason_es"], correction["corrected_by"]) == (CORRECTION, me)
    assert correction["corrected_at"] == closed["ended_at"]
    (row,) = world.step_rows(uuid.UUID(step_id))
    assert row["started_at"] < row["ended_at"]
    assert json.loads(row["correction"])["started_at"] == earlier
    (record,) = world.step_records(mounted.zone, uuid.UUID(step_id))
    assert record["content"]["started_at"] == opened["started_at"]
    assert record["content"]["ended_at"] == closed["ended_at"]
    assert record["content"]["correction"]["started_at"] == earlier
    assert record["content"]["responsible_user_id"] == me
    # Ya cerrado: el cierre ocurre una sola vez.
    _error(
        world.request(
            "POST", f"/walk-tests/{session.session_id}/steps/{step_id}/close", mounted, {}
        ),
        409,
        "conflict",
    )
    assert len(world.step_records(mounted.zone, uuid.UUID(step_id))) == 1


@pytest.mark.parametrize(
    ("correction", "status", "code", "detail"),
    [
        ({"started_at": "2020-01-01T00:00:00.000Z"}, 400, "invalid_request", None),
        (
            {"started_at": "2020-01-01T00:00:00.000Z", "reason_es": ""},
            400,
            "invalid_request",
            "catalog_free_text_rejected",
        ),
        (
            {"started_at": "2020-01-01T00:00:00.000Z", "reason_es": "corto"},
            400,
            "invalid_request",
            "catalog_free_text_rejected",
        ),
        ({"reason_es": CORRECTION}, 400, "invalid_request", None),
        (
            {
                "started_at": "2020-01-01T00:00:10.000Z",
                "ended_at": "2020-01-01T00:00:00.000Z",
                "reason_es": CORRECTION,
            },
            400,
            "invalid_request",
            None,
        ),
        (
            {"ended_at": "2099-01-01T00:00:00.000Z", "reason_es": CORRECTION},
            400,
            "invalid_request",
            None,
        ),
        (
            {"started_at": "2020-01-01T00:00:00", "reason_es": CORRECTION},
            400,
            "invalid_request",
            None,
        ),
    ],
    ids=[
        "sin-motivo",
        "motivo-vacio",
        "motivo-corto",
        "sin-marcas",
        "duracion-negativa",
        "fin-futuro",
        "sin-zona-horaria",
    ],
)
def test_an_incoherent_correction_writes_nothing(
    world: WalkTestWorld,
    correction: dict[str, Any],
    status: int,
    code: str,
    detail: str | None,
) -> None:
    mounted = world.mounted()
    session = world.open(mounted)
    step = world.start(mounted, session.session_id)
    _error(
        world.request(
            "POST",
            f"/walk-tests/{session.session_id}/steps/{step.step_id}/close",
            mounted,
            {"correction": correction},
        ),
        status,
        code,
        detail,
    )
    (row,) = world.step_rows(step.step_id)
    assert (row["ended_at"], row["correction"]) == (None, None)
    assert world.step_records(mounted.zone, step.step_id) == []


def test_concurrent_closes_leave_one_end_and_one_record(world: WalkTestWorld) -> None:
    mounted = world.mounted()
    session = world.open(mounted)
    step = world.start(mounted, session.session_id)
    service = world.build(repository=_MeetBeforeLock(_Barrier(CONCURRENT)))

    async def race() -> list[Any]:
        return await asyncio.gather(
            *(
                service.close_step(mounted.installer, session.session_id, step.step_id)
                for _ in range(CONCURRENT)
            ),
            return_exceptions=True,
        )

    world.advance()
    results = world.run(race())
    closed = [r for r in results if isinstance(r, WalkTestStep)]
    conflicts = [r for r in results if isinstance(r, WalkTestConflict)]
    assert len(closed) == 1, results
    assert len(conflicts) == CONCURRENT - 1, results
    (row,) = world.step_rows(step.step_id)
    assert row["ended_at"] == closed[0].ended_at
    (record,) = world.step_records(mounted.zone, step.step_id)
    assert record["content"]["ended_at"] == closed[0].record_content()["ended_at"]


def test_the_responsible_is_the_installer_or_a_user_with_scope(world: WalkTestWorld) -> None:
    mounted = world.mounted()
    session = world.open(mounted)
    stranger = world.a.signer(world.a.g.site(), Role.COORDINATOR_SST)  # otra organización
    for responsible in (uuid.uuid4(), stranger.user_id):
        _error(
            world.request(
                "POST",
                f"/walk-tests/{session.session_id}/steps",
                mounted,
                {"step_kind": "framing", "responsible_user_id": str(responsible)},
            ),
            400,
            "invalid_request",
        )
    assert (
        world.fetch(
            "SELECT 1 FROM catalog.walk_test_step WHERE session_id = $1", session.session_id
        )
        == []
    )
    _ok(
        world.request(
            "POST",
            f"/walk-tests/{session.session_id}/steps",
            mounted,
            {"step_kind": "framing", "responsible_user_id": str(mounted.installer.actor.id)},
        ),
        201,
    )


@pytest.mark.parametrize(
    "body",
    [
        {"step_kind": "lunch_break", "responsible_user_id": str(uuid.uuid4())},
        {"step_kind": "framing"},
        {"step_kind": "framing", "responsible_user_id": "no-es-uuid"},
        {"step_kind": "framing", "responsible_user_id": str(uuid.uuid4()), "extra": 1},
    ],
)
def test_a_step_body_out_of_shape_is_invalid_request(world: WalkTestWorld, body: Any) -> None:
    mounted = world.mounted()
    session = world.open(mounted)
    _error(
        world.request("POST", f"/walk-tests/{session.session_id}/steps", mounted, body),
        400,
        "invalid_request",
    )


# --- Pases ---------------------------------------------------------------------------------------


def test_passes_are_append_only_and_need_a_row_of_the_matrix(world: WalkTestWorld) -> None:
    mounted = world.mounted()
    session = world.open(mounted)
    row_id = str(session.matrix_rows[0].row_id)
    clip = str(uuid.uuid4())
    for result, evidence in (("missed", None), ("detected", clip)):
        body: dict[str, Any] = {"row_id": row_id, "result": result}
        if evidence is not None:
            body["evidence_ref"] = evidence
        created = _ok(
            world.request("POST", f"/walk-tests/{session.session_id}/passes", mounted, body), 201
        )
        assert (created["result"], created["evidence_ref"]) == (result, evidence)
    foreign = str(uuid.uuid4())
    _error(
        world.request(
            "POST",
            f"/walk-tests/{session.session_id}/passes",
            mounted,
            {"row_id": foreign, "result": "detected"},
        ),
        400,
        "invalid_request",
    )
    _error(
        world.request(
            "POST",
            f"/walk-tests/{session.session_id}/passes",
            mounted,
            {"row_id": row_id, "result": "maybe"},
        ),
        400,
        "invalid_request",
    )
    rows = world.pass_rows(session.session_id)
    assert [(str(r["row_id"]), r["result"]) for r in rows] == [
        (row_id, "missed"),
        (row_id, "detected"),
    ]
    assert str(rows[1]["evidence_ref"]) == clip
    current = _ok(world.request("GET", f"/zones/{mounted.zone}/walk-tests/current", mounted))
    counts = {r["row_id"]: r["passes"] for r in current["session"]["rows"]}
    assert counts[row_id] == {"detected": 1, "missed": 1, "false_alarm": 0}
    assert all(
        c == {"detected": 0, "missed": 0, "false_alarm": 0}
        for key, c in counts.items()
        if key != row_id
    )


# --- Actividad, inactividad y reapertura ---------------------------------------------------------


def test_every_valid_operation_moves_last_activity(world: WalkTestWorld) -> None:
    mounted = world.mounted()
    session = world.open(mounted)
    seen = [world.session_row(session.session_id)["last_activity_at"]]
    step = world.start(mounted, session.session_id)
    seen.append(world.session_row(session.session_id)["last_activity_at"])
    world.advance()
    world.run(world.service.close_step(mounted.installer, session.session_id, step.step_id))
    seen.append(world.session_row(session.session_id)["last_activity_at"])
    world.advance()
    world.run(
        world.service.record_pass(
            mounted.installer, session.session_id, session.matrix_rows[0].row_id, "detected"
        )
    )
    seen.append(world.session_row(session.session_id)["last_activity_at"])
    assert seen == sorted(seen) and len(set(seen)) == len(seen)


def _operations(
    world: WalkTestWorld, mounted: Mounted, session: WalkTestSession, step: WalkTestStep
) -> list[Callable[[], Any]]:
    service = world.service
    context = mounted.installer
    return [
        lambda: service.start_step(context, session.session_id, "framing", _me(context)),
        lambda: service.close_step(context, session.session_id, step.step_id),
        lambda: service.record_pass(
            context, session.session_id, session.matrix_rows[0].row_id, "detected"
        ),
    ]


def test_after_seven_days_every_operation_is_walk_test_incomplete(world: WalkTestWorld) -> None:
    mounted = world.mounted()
    session = world.open(mounted)
    step = world.start(mounted, session.session_id)
    world.age(session.session_id, INACTIVITY_LIMIT - timedelta(seconds=30))
    # Antes de los 7 días sigue abierta (la actividad la renueva).
    world.run(
        world.service.record_pass(
            mounted.installer, session.session_id, session.matrix_rows[0].row_id, "detected"
        )
    )
    world.age(session.session_id, INACTIVITY_LIMIT)
    for operation in _operations(world, mounted, session, step):
        world.advance()
        with pytest.raises(CatalogRejected) as error:
            world.run(operation())
        assert _code(error) is CatalogDetailCode.WALK_TEST_INCOMPLETE
        assert world.session_row(session.session_id)["status"] == "incomplete"
    assert len(world.pass_rows(session.session_id)) == 1  # nada se borró ni se añadió
    current = _ok(world.request("GET", f"/zones/{mounted.zone}/walk-tests/current", mounted))
    assert current["session"]["status"] == "incomplete"


def test_get_current_reports_the_effective_status_without_writing(world: WalkTestWorld) -> None:
    mounted = world.mounted()
    session = world.open(mounted)
    world.age(session.session_id, INACTIVITY_LIMIT)
    current = _ok(world.request("GET", f"/zones/{mounted.zone}/walk-tests/current", mounted))
    assert current["session"]["status"] == "incomplete"
    assert world.session_row(session.session_id)["status"] == "in_progress"


def test_reopen_only_from_incomplete_with_reason_and_keeps_the_passes(
    world: WalkTestWorld,
) -> None:
    mounted = world.mounted()
    session = world.open(mounted)
    path = f"/walk-tests/{session.session_id}/reopen"
    _error(world.request("POST", path, mounted, {"reason_es": REASON}), 409, "conflict")
    world.run(
        world.service.record_pass(
            mounted.installer, session.session_id, session.matrix_rows[1].row_id, "false_alarm"
        )
    )
    world.age(session.session_id, INACTIVITY_LIMIT)
    _error(world.request("POST", path, mounted, {}), 400, "invalid_request")
    _error(
        world.request("POST", path, mounted, {"reason_es": "   ...   "}),
        400,
        "invalid_request",
        "catalog_free_text_rejected",
    )
    assert world.reopen_trail(session.session_id) == []
    body = _ok(world.request("POST", path, mounted, {"reason_es": REASON}))
    assert (body["status"], body["reopen_reason_es"]) == ("reopened", REASON)
    assert body["reopened_by"] == str(mounted.installer.actor.id)
    assert body["last_activity_at"] == body["reopened_at"]
    assert [p["result"] for p in body["passes"]] == ["false_alarm"]
    row = world.session_row(session.session_id)
    assert (row["status"], row["reopen_reason_es"]) == ("reopened", REASON)
    (trail,) = world.reopen_trail(session.session_id)
    assert json.loads(trail["filters"]) == {"reason_es": REASON}
    assert trail["scope_zone_id"] == mounted.zone
    # Reabierta, opera de nuevo y no se reabre otra vez.
    world.run(
        world.service.record_pass(
            mounted.installer, session.session_id, session.matrix_rows[1].row_id, "detected"
        )
    )
    _error(world.request("POST", path, mounted, {"reason_es": REASON}), 409, "conflict")
    assert len(world.pass_rows(session.session_id)) == 2


def test_reopen_is_walk_test_in_progress_when_the_zone_has_another_open(
    world: WalkTestWorld,
) -> None:
    mounted = world.mounted()
    stale = world.open(mounted)
    world.age(stale.session_id, INACTIVITY_LIMIT)
    world.open(mounted)  # la vencida pasa a incomplete y deja de bloquear
    with pytest.raises(CatalogRejected) as error:
        world.run(world.service.reopen(mounted.installer, stale.session_id, REASON))
    assert _code(error) is CatalogDetailCode.WALK_TEST_IN_PROGRESS
    assert world.session_row(stale.session_id)["status"] == "incomplete"
    assert world.reopen_trail(stale.session_id) == []


def test_concurrent_open_and_reopen_leave_one_open_session(world: WalkTestWorld) -> None:
    """Dos operaciones distintas sobre la misma zona: nunca dos abiertas ni un transitorio."""
    mounted = world.mounted()
    stale = world.open(mounted)
    world.age(stale.session_id, INACTIVITY_LIMIT)
    world.execute(
        "UPDATE catalog.walk_test_session SET status = 'incomplete' WHERE session_id = $1",
        stale.session_id,
    )
    service = world.build(repository=_MeetBeforeWrite(_Barrier(2)))

    async def race() -> list[Any]:
        return await asyncio.gather(
            service.open(mounted.installer, mounted.zone, 3),
            service.reopen(mounted.installer, stale.session_id, REASON),
            return_exceptions=True,
        )

    world.advance()
    results = world.run(race())
    winners = [r for r in results if isinstance(r, WalkTestSession | WalkTestView)]
    losers = [r for r in results if isinstance(r, CatalogRejected)]
    assert len(winners) == 1 and len(losers) == 1, results
    assert losers[0].detail_code is CatalogDetailCode.WALK_TEST_IN_PROGRESS
    assert len(world.open_sessions(mounted.zone)) == 1


def test_a_closed_session_answers_conflict(world: WalkTestWorld) -> None:
    mounted = world.mounted()
    session = world.open(mounted)
    step = world.start(mounted, session.session_id)
    world.execute(
        "UPDATE catalog.walk_test_session SET status = 'closed', closed_at = last_activity_at"
        " WHERE session_id = $1",
        session.session_id,
    )
    for operation in _operations(world, mounted, session, step):
        world.advance()
        with pytest.raises(WalkTestConflict):
            world.run(operation())
    with pytest.raises(WalkTestConflict):
        world.run(world.service.reopen(mounted.installer, session.session_id, REASON))
    current = _ok(world.request("GET", f"/zones/{mounted.zone}/walk-tests/current", mounted))
    assert current["session"] is None


# --- Alcance -------------------------------------------------------------------------------------


def test_another_organization_answers_not_found(world: WalkTestWorld) -> None:
    own = world.mounted()
    other = world.mounted()
    session = world.open(other)
    step = world.start(other, session.session_id)
    row = str(session.matrix_rows[0].row_id)
    calls = [
        ("POST", f"/zones/{other.zone}/walk-tests", {"passes_per_cell": 3}),
        ("GET", f"/zones/{other.zone}/walk-tests/current", None),
        (
            "POST",
            f"/walk-tests/{session.session_id}/steps",
            {"step_kind": "framing", "responsible_user_id": str(own.installer.actor.id)},
        ),
        ("POST", f"/walk-tests/{session.session_id}/steps/{step.step_id}/close", {}),
        ("POST", f"/walk-tests/{session.session_id}/passes", {"row_id": row, "result": "missed"}),
        ("POST", f"/walk-tests/{session.session_id}/reopen", {"reason_es": REASON}),
    ]
    for method, path, body in calls:
        _error(world.request(method, path, own, body), 404, "not_found")
    assert world.pass_rows(session.session_id) == []
    assert world.step_rows(step.step_id)[0]["ended_at"] is None


def test_a_plant_outside_the_concession_answers_not_found(world: WalkTestWorld) -> None:
    """Misma organización, concesión de la planta A: la sesión de la planta B no existe."""
    site = world.a.g.site(plants=2)
    (plant_a, zone_a), (_, zone_b) = site.zones()
    on_b = world.mounted(site=site, zone_index=1)
    session = world.open(on_b)
    step = world.start(on_b, session.session_id)
    on_a = world.mounted(site=site, zone_index=0)
    context, _, _ = world.a.installer_session(site, ScopeLevel.PLANT, plant_a)
    service = world.service
    for operation in (
        lambda: service.open(context, zone_b, 3),
        lambda: service.current(context, zone_b),
        lambda: service.start_step(context, session.session_id, "framing", _me(context)),
        lambda: service.close_step(context, session.session_id, step.step_id),
        lambda: service.record_pass(
            context, session.session_id, session.matrix_rows[0].row_id, "missed"
        ),
        lambda: service.reopen(context, session.session_id, REASON),
    ):
        world.advance()
        with pytest.raises(ResourceNotFound):
            world.run(operation())
    assert world.pass_rows(session.session_id) == []
    assert world.step_rows(step.step_id)[0]["ended_at"] is None
    # Con la concesión de su planta, la misma persona sí opera en A.
    world.advance()
    own: WalkTestSession = world.run(service.open(context, on_a.zone, 3))
    assert own.zone_id == zone_a
    world.advance()
    world.run(service.start_step(context, own.session_id, "framing", _me(context)))


def test_a_zone_scoped_reader_does_not_see_the_other_zone(world: WalkTestWorld) -> None:
    """Misma organización y planta: quien lee solo la zona A no ve la sesión de la zona B (la
    RLS es por organización; el filtro es la autorización sobre la zona pedida)."""
    site = world.a.g.site(zones=2)
    (_, zone_a), (_, zone_b) = site.zones()
    for index in (0, 1):
        world.open(world.mounted(site=site, zone_index=index))
    reader = world.a.g.member(site, Role.LINE_MANAGER, ScopeLevel.ZONE, zone_a)
    world.advance()
    view = world.run(world.service.current(reader, zone_a))
    assert view is not None and view.session.zone_id == zone_a
    world.advance()
    with pytest.raises(ResourceNotFound):
        world.run(world.service.current(reader, zone_b))


def test_a_step_of_another_session_answers_not_found(world: WalkTestWorld) -> None:
    mounted = world.mounted()
    session = world.open(mounted)
    other = world.mounted(site=world.a.g.site())
    other_session = world.open(other)
    other_step = world.start(other, other_session.session_id)
    world.advance()
    with pytest.raises(ResourceNotFound):
        world.run(
            world.service.close_step(mounted.installer, session.session_id, other_step.step_id)
        )


# --- NFR-GOB-03 ----------------------------------------------------------------------------------


def test_get_current_queries_do_not_grow_with_the_matrix(
    world: WalkTestWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    small = world.mounted(count=1)
    world.open(small)
    large = world.mounted(count=8)
    session = world.open(large)
    for index in range(3):
        step = world.start(large, session.session_id)
        world.run(world.service.close_step(large.installer, session.session_id, step.step_id))
        for row in session.matrix_rows[index * 8 : index * 8 + 8]:
            world.run(
                world.service.record_pass(
                    large.installer, session.session_id, row.row_id, "detected"
                )
            )
    statements: list[str] = []
    execute, read = Transaction.execute, Database.read

    async def counted_execute(self: Transaction, statement: Any, *args: Any, **kw: Any) -> Any:
        statements.append(str(statement))
        return await execute(self, statement, *args, **kw)

    async def counted_read(self: Database, context: Any, statement: Any, *args: Any) -> Any:
        statements.append(str(statement))
        return await read(self, context, statement, *args)

    monkeypatch.setattr(Transaction, "execute", counted_execute)
    monkeypatch.setattr(Database, "read", counted_read)

    def count(mounted: Mounted) -> tuple[int, WalkTestView]:
        statements.clear()
        view = world.run(world.service.current(mounted.installer, mounted.zone))
        return len(statements), view

    small_count, small_view = count(small)
    large_count, large_view = count(large)
    assert (len(small_view.session.matrix_rows), len(large_view.session.matrix_rows)) == (4, 32)
    assert (len(large_view.steps), len(large_view.passes)) == (3, 24)
    assert large_count == small_count, (small_count, large_count)
