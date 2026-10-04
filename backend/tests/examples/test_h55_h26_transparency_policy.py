"""Pruebas de ejemplo de H-55 y H-26 (TASK-212; SCR-05 y SCR-16).

Cada prueba recorre el criterio de su historia con un caso concreto, de extremo a extremo, sobre la
aplicación real y PostgreSQL 16 real (``tests/agreements_support.py``): solo peticiones HTTP, como
las hará U-05. Lo previo a la historia (montaje, acta de comisionamiento, firmantes) se siembra con
los servicios reales o por repositorio.

- **H-55** El COPASST ve lo declarado y acordado sobre la zona y confirma su firma desde la vista de
  transparencia con solo ``transparency.read``, sin ningún permiso de gestión: la confirmación queda
  con ``origin = transparency``. La transparencia no depende de ninguna aprobación.
- **H-26** Sin política de hallazgos incerrables de la planta, la aprobación del acuerdo responde
  ``catalog_plant_policy_missing`` y la zona sigue en ``commissioning``; cargada la política, la
  misma aprobación la lleva a ``productive``.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any, Final

import pytest

from tests.agreements_support import SIGNER_ROLES, AgreementsWorld, agreements_world
from tests.gates_support import FRAMING, SCOPE_TEXT
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.identity.authz.matrix import PermissionKey, permissions_of
from vigia_platform.shared.context import Role

pytestmark = pytest.mark.integration

STANDARD_ID: Final = str(uuid.UUID(int=50_000, version=4))
CATALOG: Final = {
    "standards": [
        {
            "standard_id": STANDARD_ID,
            "version": 2,
            "family": "coexistence",
            "title_es": "Coexistencia en la celda",
            "declared_text": "Nadie permanece en la celda mientras la máquina está energizada.",
        }
    ],
}


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[AgreementsWorld]:
    with agreements_world(postgres_endpoint, "agreement_examples") as world:
        yield world


def _ok(response: Any, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    return body


def _error(response: Any, status: int, code: str, detail_code: str | None = None) -> None:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code, body
    assert body.get("detail_code") == detail_code, body


def test_h55_copasst_confirms_from_transparency_with_only_transparency_read(
    world: AgreementsWorld,
) -> None:
    # La columna copasst: transparency.read sí; agreements.sign y commissioning.run, no.
    copasst_keys = permissions_of(Role.COPASST)
    assert PermissionKey.TRANSPARENCY_READ in copasst_keys
    assert PermissionKey.AGREEMENTS_SIGN not in copasst_keys
    assert PermissionKey.COMMISSIONING_RUN not in copasst_keys
    ready = world.ready(
        confirm=tuple(r for r in SIGNER_ROLES if r is not Role.COPASST),
        catalog={
            **CATALOG,
            "minimum_coverage": {"required_count": 1, "required_camera_ids": []},
        },
    )
    assert ready.agreement is not None
    agreement_id = str(ready.agreement.agreement_id)
    copasst = ready.copasst
    path = f"/zones/{ready.zone}/transparency"

    # Lo declarado y acordado, antes de ninguna aprobación (H-55).
    view = _ok(world.request("GET", path, copasst.cookie))
    assert view["declared_scope"]["scope_text_es"] == SCOPE_TEXT
    cameras = view["declared_scope"]["cameras"]
    assert len(cameras) == 2 and {c["framing_description_es"] for c in cameras} == {FRAMING}
    assert view["declared_scope"]["minimum_coverage"] == {
        "required_count": 1,
        "required_camera_ids": [],
    }
    assert view["standards"] == [
        {
            "standard_id": STANDARD_ID,
            "version": 2,
            "title_es": "Coexistencia en la celda",
            "declared_text": "Nadie permanece en la celda mientras la máquina está energizada.",
        }
    ]
    assert view["gates"]["mounting"]["status"] == "approved"
    assert view["gates"]["usage"]["status"] == "pending"
    assert view["gates"]["resulting_mode"] == "commissioning"
    assert view["current_agreement"] is None
    assert view["pending_confirmation_for_me"] == agreement_id

    # La única acción desde la vista: confirmar su propia firma.
    confirmation_path = f"/use-agreements/{agreement_id}/confirmations"
    confirmed = _ok(world.request("POST", confirmation_path, copasst.cookie), 201)
    assert confirmed["user_id"] == str(copasst.user_id)
    assert (confirmed["role_in_use"], confirmed["origin"]) == ("copasst", "transparency")
    (row,) = [
        r
        for r in world.confirmation_rows(ready.agreement.agreement_id)
        if r["origin"] == "transparency"
    ]
    assert (row["user_id"], row["role_in_use"]) == (copasst.user_id, "copasst")
    again = _ok(world.request("POST", confirmation_path, copasst.cookie))  # 200: ya confirmó
    assert again == confirmed
    assert _ok(world.request("GET", path, copasst.cookie))["pending_confirmation_for_me"] is None
    # Sin permisos de gestión: ni aprueba ni crea acuerdos (como inexistente).
    _error(
        world.request("POST", f"/use-agreements/{agreement_id}/approval", copasst.cookie),
        404,
        "not_found",
    )

    # El instalador aprueba; el COPASST ve el acuerdo vigente con todas las firmas.
    _, cookie, concession = world.installer_session(ready.site)
    approval = _ok(
        world.request(
            "POST", f"/use-agreements/{agreement_id}/approval", cookie, concession=concession
        )
    )
    assert approval["gates"]["resulting_mode"] == "productive"
    view = _ok(world.request("GET", path, copasst.cookie))
    current = view["current_agreement"]
    assert current["agreement_id"] == agreement_id and current["status"] == "approved"
    assert {s["user_id"] for s in current["signatories"]} == {str(s.user_id) for s in ready.signers}
    assert all(s["confirmed_at"] is not None for s in current["signatories"])
    assert view["gates"]["usage"] == {
        "status": "approved",
        "decided_at": current["approved_at"],
        "agreement_id": agreement_id,
        "decided_by": current["approved_by"],
    }
    # G-9 por HTTP: bajo concesión la confirmación no existe para el proveedor.
    _error(
        world.request("POST", confirmation_path, cookie, concession=concession), 404, "not_found"
    )


def test_h26_without_plant_policy_the_approval_is_plant_policy_missing(
    world: AgreementsWorld,
) -> None:
    ready = world.ready(plant_policy=False, signatory_policy=False, create=False)
    _, cookie, concession = world.installer_session(ready.site)

    # La política de firmantes por HTTP, con sus guardas (BR-GOB-25).
    policy_path = f"/plants/{ready.plant}/signatory-policy"
    _error(
        world.request(
            "PUT",
            policy_path,
            cookie,
            {"required_roles": ["coordinator_sst", "copasst"], "minimum": 2},
            concession,
        ),
        400,
        "invalid_request",
        "catalog_fewer_than_three",
    )
    _error(
        world.request(
            "PUT",
            policy_path,
            cookie,
            {"required_roles": ["coordinator_sst", "plant_manager"], "minimum": 3},
            concession,
        ),
        400,
        "invalid_request",
        "catalog_workers_representation_missing",
    )
    assert _ok(world.request("GET", policy_path, cookie, concession=concession)) == {
        "plant_id": str(ready.plant),
        "configured": False,
        "required_roles": None,
        "minimum": None,
        "workers_role": "copasst",
        "updated_by": None,
        "updated_at": None,
    }
    policy = _ok(
        world.request(
            "PUT",
            policy_path,
            cookie,
            {"required_roles": ["coordinator_sst", "plant_manager", "copasst"], "minimum": 3},
            concession,
        )
    )
    assert policy["configured"] and policy["minimum"] == 3

    # El acuerdo por HTTP y las firmas en la sesión de cada firmante.
    created = _ok(
        world.request(
            "POST",
            f"/zones/{ready.zone}/use-agreements",
            cookie,
            {
                "signatories": [
                    {"role": s.role.value, "user_id": str(s.user_id)} for s in ready.signers
                ]
            },
            concession,
        ),
        201,
    )
    assert created["status"] == "pending_signatures"
    agreement_id = created["agreement_id"]
    for signer in ready.signers:
        _ok(
            world.request("POST", f"/use-agreements/{agreement_id}/confirmations", signer.cookie),
            201,
        )
    before = world.written(ready.plant, ready.zone)

    approval_path = f"/use-agreements/{agreement_id}/approval"
    _error(
        world.request("POST", approval_path, cookie, concession=concession),
        409,
        "conflict",
        "catalog_plant_policy_missing",
    )

    # La zona sigue en comisionamiento y no se escribió nada.
    gates = _ok(world.request("GET", f"/zones/{ready.zone}/gates", cookie, concession=concession))
    assert (gates["usage"]["status"], gates["resulting_mode"]) == ("pending", "commissioning")
    assert world.written(ready.plant, ready.zone) == before
    # Con la política cargada, la misma aprobación lleva la zona a productiva.
    world.plant_policy(ready.site, ready.plant)
    approved = _ok(world.request("POST", approval_path, cookie, concession=concession))
    assert approved["agreement"]["status"] == "approved"
    assert approved["gates"]["resulting_mode"] == "productive"
