"""N-6 · Escalada por asignación (H-54, H-55; business-rules §14).

**Qué intenta**: un mando de línea obtiene el rol de coordinación SST sobre su propia zona (y con
él, clasificar los hallazgos que lo exponen); un administrador se vuelve coordinador.

**Qué lo detiene** (BR-NUC-14, BR-NUC-19):

- BR-NUC-14: incompatibilidades verificadas al asignar, en ambas direcciones y **de forma
  atómica**: ``line_manager`` sobre Z contra ``coordinator_sst``, ``plant_manager`` o
  ``administrator`` sobre un alcance que contenga a Z; ``administrator`` contra ``coordinator_sst``
  y ``plant_manager`` solapados. El rechazo nombra la asignación en conflicto y se audita como
  ``role_assignment_rejected``;
- BR-NUC-19: solo ``administrator`` (y el operador) asigna; nadie se asigna un rol que no tiene.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest

from tests.platform_support import Platform, code_of
from vigia_platform.shared.context import Role, ScopeLevel

pytestmark = pytest.mark.integration


def _assignment(role: Role, level: ScopeLevel, scope_id: uuid.UUID) -> dict[str, str]:
    return {"role": role.value, "scope_level": level.value, "scope_id": str(scope_id)}


def _active_roles(platform: Platform, user_id: uuid.UUID) -> list[tuple[str, str]]:
    return sorted(
        (row["role"], row["scope_level"])
        for row in platform.fetch(
            "SELECT role, scope_level FROM identity.role_assignment"
            " WHERE user_id = $1 AND removed_at IS NULL",
            user_id,
        )
    )


def _rejections(platform: Platform, organization_id: uuid.UUID, user_id: uuid.UUID) -> list[Any]:
    return [
        json.loads(bytes(row["filters"]))
        for row in platform.audit_entries(organization_id, "role_assignment_rejected")
        if row["resource_id"] == user_id
    ]


def test_n06_a_line_manager_cannot_grant_themself_the_coordinator_role(
    platform: Platform,
) -> None:
    site = platform.site()
    ((plant_id, zone_id),) = site.zones()
    manager, cookie = platform.person(
        site.organization_id, Role.LINE_MANAGER, level=ScopeLevel.ZONE, scope_id=zone_id
    )
    for body in (
        _assignment(Role.COORDINATOR_SST, ScopeLevel.ZONE, zone_id),
        _assignment(Role.COORDINATOR_SST, ScopeLevel.PLANT, plant_id),
    ):
        response = platform.call("POST", f"/users/{manager}/roles", cookie=cookie, json_body=body)
        # Sin ``roles.assign``: como una ruta sobre un recurso inexistente.
        assert response.status_code == 404 and code_of(response) == "not_found", response.text
    assert _active_roles(platform, manager) == [("line_manager", "zone")]


def test_n06_an_administrator_cannot_make_themself_coordinator(platform: Platform) -> None:
    site = platform.site()
    ((plant_id, _),) = site.zones()
    admin, cookie = platform.person(site.organization_id, Role.ADMINISTRATOR)
    response = platform.call(
        "POST",
        f"/users/{admin}/roles",
        cookie=cookie,
        json_body=_assignment(Role.COORDINATOR_SST, ScopeLevel.PLANT, plant_id),
    )
    assert response.status_code == 409 and code_of(response) == "conflict", response.text
    assert _active_roles(platform, admin) == [("administrator", "organization")]
    assert _rejections(platform, site.organization_id, admin)


def test_n06_the_line_manager_of_a_zone_is_not_made_coordinator_of_its_plant(
    platform: Platform,
) -> None:
    site = platform.site()
    ((plant_id, zone_id),) = site.zones()
    _, admin = platform.person(site.organization_id, Role.ADMINISTRATOR)
    manager, _ = platform.person(
        site.organization_id, Role.LINE_MANAGER, level=ScopeLevel.ZONE, scope_id=zone_id
    )
    (zone_assignment,) = platform.fetch(
        "SELECT assignment_id FROM identity.role_assignment WHERE user_id = $1", manager
    )
    for role in (Role.COORDINATOR_SST, Role.PLANT_MANAGER, Role.ADMINISTRATOR):
        response = platform.call(
            "POST",
            f"/users/{manager}/roles",
            cookie=admin,
            json_body=_assignment(role, ScopeLevel.PLANT, plant_id),
        )
        assert response.status_code == 409 and code_of(response) == "conflict", (role, response)
    rejections = _rejections(platform, site.organization_id, manager)
    assert [r["role"] for r in rejections] == ["coordinator_sst", "plant_manager", "administrator"]
    assert {r["conflict_assignment_id"] for r in rejections} == {
        str(zone_assignment["assignment_id"])
    }
    assert _active_roles(platform, manager) == [("line_manager", "zone")]


@pytest.mark.parametrize("rounds", range(3))
def test_n06_two_concurrent_incompatible_assignments_never_both_land(
    platform: Platform, rounds: int
) -> None:
    """La comprobación es atómica: dos administradores a la vez, uno coordinador de la planta y
    otro mando de línea de su zona, para la misma persona; como mucho entra una."""
    site = platform.site()
    ((plant_id, zone_id),) = site.zones()
    _, first = platform.person(site.organization_id, Role.ADMINISTRATOR)
    _, second = platform.person(site.organization_id, Role.ADMINISTRATOR)
    target, _ = platform.person(site.organization_id, Role.COPASST)

    async def both() -> list[Any]:
        return list(
            await asyncio.gather(
                platform.send(
                    "POST",
                    f"/users/{target}/roles",
                    cookie=first,
                    json_body=_assignment(Role.COORDINATOR_SST, ScopeLevel.PLANT, plant_id),
                ),
                platform.send(
                    "POST",
                    f"/users/{target}/roles",
                    cookie=second,
                    json_body=_assignment(Role.LINE_MANAGER, ScopeLevel.ZONE, zone_id),
                ),
            )
        )

    responses = platform.run(both())
    statuses = sorted(response.status_code for response in responses)
    assert statuses == [201, 409], [r.text for r in responses]
    roles = {role for role, _ in _active_roles(platform, target)}
    assert len(roles & {"coordinator_sst", "line_manager"}) == 1
