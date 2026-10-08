"""FS-GOB-06 · Nodo mudo durante la ventana de oclusión (NFR-GOB-44; PR-GOB-09, 24; BR-GOB-41, 42;
PAT-GOB-RES-03, LC-GOB-07).

Sobre la aplicación completa (``gob_platform``: todas las unidades, PostgreSQL 16 como
``vigia_app`` y LocalStack), por las rutas reales del instalador y del nodo.

**Inyección**: una zona en comisionamiento con su nodo dado de alta; el walk-test abierto, la
matriz completa y el clip de verificación; en cada cámara, una prueba de oclusión cuya ventana
termina ahora y un **nodo que no emite ningún evento de observabilidad** desde ``started_at``. El
**reloj simulado** (el de la aplicación, alineado con la hora de la base, que pone
``received_at``) avanza más allá de la fecha límite; los instantes de las lecturas salen de la
semilla dentro de cada tramo.

**Resultado esperado**:

- antes de los 5 min (tolerancia de 30 s) la prueba sigue ``pending`` y **el acta no se cierra**
  (``catalog_redundancy_not_verified``): nunca se presenta como ``verified`` ni como fallo antes
  de tiempo;
- a los 5 min (±30 s) pasa a ``failed`` con ``no_observability_events_in_window`` y el acta sigue
  sin cerrarse;
- se admite la **declaración manual con motivo** (otra prueba de la cámara, declarada); declarar
  la prueba ya fallida es ``conflict`` y no escribe nada;
- el acta se cierra y su resumen de oclusión dice ``declared`` para cada cámara; en el expediente,
  ningún ``occlusion_test_result`` es ``verified``.

Solo datos generados.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any, Final

import pytest

from tests.gob_platform_support import (
    GobPlatform,
    GobZone,
    Onboarding,
    close_body,
    detail_of,
    gob_platform,
    ok,
    require_close_route,
    stamp,
)
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint
from tests.resilience.harness import scenario

pytestmark = [pytest.mark.integration, pytest.mark.nightly]

PASSES_PER_CELL: Final = 25
"""Con las cuatro posturas del estándar, 100 pases: las repeticiones de la latencia (BR-GOB-48)."""
WAIT: Final = dt.timedelta(minutes=5)
TOLERANCE: Final = dt.timedelta(seconds=30)
REASON: Final = "La cámara quedó tapada por la grúa durante el turno; se declara con motivo."
REFUSED: Final = (409, "conflict", "catalog_redundancy_not_verified")


@pytest.fixture(scope="module")
def gob(
    postgres_endpoint: PostgresEndpoint, localstack_endpoint: LocalStackEndpoint
) -> Iterator[GobPlatform]:
    with gob_platform(postgres_endpoint, localstack_endpoint, "fs_gob_06") as platform:
        yield platform


def _occlusion(
    flow: Onboarding,
    zone: GobZone,
    session_id: str,
    camera: int,
    ended_at: dt.datetime,
    reason: str | None = None,
) -> Any:
    body: dict[str, Any] = {
        "camera_id": str(zone.cameras[camera]),
        "started_at": stamp(ended_at - dt.timedelta(seconds=20)),
        "ended_at": stamp(ended_at),
    }
    if reason is not None:
        body["declared_reason_es"] = reason
    return flow.as_installer(zone, "POST", f"/walk-tests/{session_id}/occlusion-tests", body)


def _tests(flow: Onboarding, zone: GobZone) -> list[dict[str, Any]]:
    current = ok(flow.as_installer(zone, "GET", f"/zones/{zone.zone_id}/walk-tests/current"))
    tests: list[dict[str, Any]] = current["session"]["occlusion_tests"]
    return tests


def _close(flow: Onboarding, zone: GobZone, session_id: str) -> Any:
    return flow.as_installer(
        zone, "POST", f"/walk-tests/{session_id}/close", close_body(flow.gob, zone)
    )


def _state(flow: Onboarding, zone: GobZone) -> list[tuple[str, str | None]]:
    return [(test["verification"], test.get("failure_reason")) for test in _tests(flow, zone)]


def test_fs_gob_06_silent_node_during_the_occlusion_window(gob: GobPlatform) -> None:
    require_close_route(gob)
    with scenario(
        "FS-GOB-06",
        title="Nodo mudo durante la ventana de oclusión",
        injection=(
            "nodo que no emite eventos de observabilidad desde started_at; reloj simulado"
            " avanzado más allá de la fecha límite"
        ),
        expected=(
            "pending y acta sin cerrar antes de 5 min (±30 s); failed con"
            " no_observability_events_in_window después; declaración con motivo admitida; el acta"
            " dice declared y nunca verified"
        ),
    ) as run:
        flow = Onboarding(gob)
        zone = flow.zone()
        flow.mount(zone)
        # Un solo reloj (retro 14): la ventana se compara con ``received_at``, que pone la base.
        gob.resync()
        gob.advance(20)
        session = ok(
            flow.as_installer(
                zone,
                "POST",
                f"/zones/{zone.zone_id}/walk-tests",
                {"passes_per_cell": PASSES_PER_CELL},
            ),
            201,
        )
        session_id = session["session_id"]
        current = ok(flow.as_installer(zone, "GET", f"/zones/{zone.zone_id}/walk-tests/current"))
        for row in current["session"]["rows"]:
            for _ in range(PASSES_PER_CELL):
                ok(
                    flow.as_installer(
                        zone,
                        "POST",
                        f"/walk-tests/{session_id}/passes",
                        {"row_id": row["row_id"], "result": "detected"},
                    ),
                    201,
                )
        flow.verification_clip(zone)

        # La ventana de cada cámara termina ahora; el nodo calla desde ``started_at``.
        ended = gob.now() - dt.timedelta(seconds=2)
        cameras = range(len(zone.cameras))
        started = [ok(_occlusion(flow, zone, session_id, camera, ended), 201) for camera in cameras]
        deadline = stamp(ended + WAIT)
        assert {test["deadline"] for test in started} == {deadline}
        assert {test["verification"] for test in started} == {"pending"}

        # Antes de la fecha límite menos la tolerancia: «verificando», y el acta no se cierra.
        early = run.random.uniform(10, (WAIT - TOLERANCE).total_seconds() - 1)
        gob.advance(early - (gob.now() - ended).total_seconds())
        before = _state(flow, zone)
        refused_before = detail_of(_close(flow, zone, session_id))

        # Pasada la fecha límite más la tolerancia: ``failed`` sin eventos en la ventana.
        late = run.random.uniform((WAIT + TOLERANCE).total_seconds() + 1, 7 * 60)
        gob.advance(late - (gob.now() - ended).total_seconds())
        after = _state(flow, zone)
        refused_after = detail_of(_close(flow, zone, session_id))
        records_before = len(gob.contents(zone.organization_id, "occlusion_test_result"))
        # La prueba fallida ya no se puede declarar (otra ventana, la misma cámara, sin motivo
        # nuevo no cambia nada): una declaración solo cabe en una prueba vigente.
        stale = detail_of(_occlusion(flow, zone, session_id, 0, ended, reason=REASON))
        unchanged = len(gob.contents(zone.organization_id, "occlusion_test_result"))

        # Declaración manual con motivo: otra prueba de cada cámara, declarada.
        retry_end = gob.now() - dt.timedelta(seconds=2)
        declared = []
        for camera in cameras:
            ok(_occlusion(flow, zone, session_id, camera, retry_end), 201)
            declared.append(
                ok(_occlusion(flow, zone, session_id, camera, retry_end, reason=REASON), 201)
            )
        record = ok(_close(flow, zone, session_id))
        results = gob.contents(zone.organization_id, "occlusion_test_result")
        summary = record["occlusion_summary"]
        run.observe(
            cameras=len(zone.cameras),
            deadline=deadline,
            read_before_seconds=round(early, 1),
            state_before=before,
            close_before=list(refused_before),
            read_after_seconds=round(late, 1),
            state_after=after,
            close_after=list(refused_after),
            declare_failed_test=list(stale),
            declared=[test["verification"] for test in declared],
            occlusion_summary=summary,
            ledger_results=sorted(result["verification"] for result in results),
        )

        assert before == [("pending", None)] * len(zone.cameras)
        assert refused_before == REFUSED, "el acta no cierra antes de la fecha límite"
        assert after == [("failed", "no_observability_events_in_window")] * len(zone.cameras)
        assert refused_after == REFUSED, "una prueba fallida tampoco deja cerrar el acta"
        assert stale[0] == 409 and unchanged == records_before
        assert [test["verification"] for test in declared] == ["declared"] * len(zone.cameras)
        assert all(test["declared_reason_es"] == REASON for test in declared)
        assert sorted(item["verification"] for item in summary) == ["declared"] * len(zone.cameras)
        assert "verified" not in {result["verification"] for result in results}
        assert sorted(result["verification"] for result in results) == sorted(
            ["failed"] * len(zone.cameras) + ["declared"] * len(zone.cameras)
        )
