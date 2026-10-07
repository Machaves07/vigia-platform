"""G-7 · Instalador que intenta cerrar un acta con un falso negativo (§12; H-51; RF-PLA-07).

**Qué intenta**: aprobar la zona pese a una detección perdida en la matriz del walk-test:
borrando o editando el pase ``missed``, tapándolo con pases ``detected`` de la misma celda o
cerrando el acta sin la prueba de oclusión de alguna cámara.

**Qué lo detiene**:

- BR-GOB-38: cada pase se registra con su resultado, quién y cuándo, en una tabla de **solo
  anexar**: no hay ruta para editarlo ni borrarlo y el rol de la aplicación no tiene ``UPDATE`` ni
  ``DELETE`` sobre ella; corregir es registrar otro pase, y el ``missed`` sigue contando;
- BR-GOB-39: cero falsos negativos **por zona**: cualquier ``missed`` de la matriz bloquea el
  cierre con ``catalog_false_negative_present``;
- BR-GOB-43: el cierre exige además una prueba de oclusión verificada por **cada** cámara del
  catálogo (``catalog_redundancy_not_verified``).

Por las rutas reales de la aplicación completa (``tests/gob_platform_support.py``).
"""

from __future__ import annotations

from typing import Any

import asyncpg
import pytest

from tests.gob_platform_support import (
    GobPlatform,
    GobZone,
    Onboarding,
    close_body,
    detail_of,
    ok,
    require_close_route,
)

pytestmark = pytest.mark.integration


def _open(flow: Onboarding, zone: GobZone) -> dict[str, Any]:
    opened: dict[str, Any] = ok(
        flow.as_installer(
            zone, "POST", f"/zones/{zone.zone_id}/walk-tests", {"passes_per_cell": 3}
        ),
        201,
    )
    return opened


def _rows(flow: Onboarding, zone: GobZone) -> list[dict[str, Any]]:
    current = ok(flow.as_installer(zone, "GET", f"/zones/{zone.zone_id}/walk-tests/current"))
    rows: list[dict[str, Any]] = current["session"]["rows"]
    return rows


def _pass(flow: Onboarding, zone: GobZone, session_id: str, row_id: str, result: str) -> Any:
    return ok(
        flow.as_installer(
            zone,
            "POST",
            f"/walk-tests/{session_id}/passes",
            {"row_id": row_id, "result": result},
        ),
        201,
    )


def test_g07_a_missed_pass_can_be_neither_edited_nor_deleted_nor_covered(
    gob: GobPlatform,
) -> None:
    flow = Onboarding(gob)
    zone = flow.zone()
    flow.mount(zone)
    session_id = _open(flow, zone)["session_id"]
    row_id = _rows(flow, zone)[0]["row_id"]
    missed = _pass(flow, zone, session_id, row_id, "missed")
    # «Taparlo» con pases ``detected`` de la misma celda no lo borra.
    for _ in range(3):
        _pass(flow, zone, session_id, row_id, "detected")
    counts = {row["row_id"]: row["passes"] for row in _rows(flow, zone)}
    assert counts[row_id] == {"detected": 3, "missed": 1, "false_alarm": 0}

    # No hay ruta para editar ni borrar un pase.
    for method in ("PUT", "PATCH", "DELETE"):
        response = flow.as_installer(
            zone, method, f"/walk-tests/{session_id}/passes/{missed['pass_id']}", {}
        )
        assert response.status_code in (404, 405), (method, response.text)

    # Ni la base deja al rol de la aplicación reescribirlo.
    async def rewrite() -> list[str]:
        connection = await gob.authz.sessions.migrated.connect()
        refused: list[str] = []
        try:
            await connection.execute("SET ROLE vigia_app")
            for statement in (
                "UPDATE catalog.walk_test_pass SET result = 'detected' WHERE pass_id = $1",
                "DELETE FROM catalog.walk_test_pass WHERE pass_id = $1",
            ):
                try:
                    await connection.execute(statement, missed["pass_id"])
                except asyncpg.PostgresError as error:
                    refused.append(type(error).__name__)
        finally:
            await connection.close()
        return refused

    assert gob.run(rewrite()) == ["InsufficientPrivilegeError", "InsufficientPrivilegeError"]
    (row,) = gob.fetch(
        "SELECT result FROM catalog.walk_test_pass WHERE pass_id = $1", missed["pass_id"]
    )
    assert row["result"] == "missed"


def _complete_matrix(
    flow: Onboarding, zone: GobZone, session_id: str, *, missed: bool
) -> list[dict[str, Any]]:
    """Tres pases ``detected`` por celda (``passes_per_cell``) y, con ``missed``, además una
    detección perdida en la primera fila."""
    rows = _rows(flow, zone)
    for row in rows:
        for _ in range(3):
            _pass(flow, zone, session_id, row["row_id"], "detected")
    if missed:
        _pass(flow, zone, session_id, rows[0]["row_id"], "missed")
    return rows


def _close(flow: Onboarding, zone: GobZone, session_id: str, body: dict[str, Any]) -> Any:
    return flow.as_installer(zone, "POST", f"/walk-tests/{session_id}/close", body)


def test_g07_a_false_negative_in_the_matrix_never_closes_the_record(gob: GobPlatform) -> None:
    require_close_route(gob)
    flow = Onboarding(gob)
    zone = flow.zone()
    flow.mount(zone)
    session_id = _open(flow, zone)["session_id"]
    rows = _complete_matrix(flow, zone, session_id, missed=True)
    body = close_body(gob, zone)
    refused = _close(flow, zone, session_id, body)
    assert detail_of(refused) == (409, "conflict", "catalog_false_negative_present"), refused.text
    # Taparlo con más detecciones de la misma celda no cambia nada: cero por zona.
    for _ in range(3):
        _pass(flow, zone, session_id, rows[0]["row_id"], "detected")
    again = _close(flow, zone, session_id, body)
    assert detail_of(again) == detail_of(refused)
    assert gob.records(zone.organization_id, "walk_test_result") == []
    assert flow.mode(zone) == "commissioning"


def test_g07_without_an_occlusion_test_per_camera_the_record_never_closes(
    gob: GobPlatform,
) -> None:
    require_close_route(gob)
    flow = Onboarding(gob)
    zone = flow.zone()
    flow.mount(zone)
    session_id = _open(flow, zone)["session_id"]
    _complete_matrix(flow, zone, session_id, missed=False)
    refused = _close(flow, zone, session_id, close_body(gob, zone))
    assert detail_of(refused) == (409, "conflict", "catalog_redundancy_not_verified"), refused.text
    assert gob.records(zone.organization_id, "walk_test_result") == []
