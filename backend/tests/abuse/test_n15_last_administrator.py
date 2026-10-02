"""N-15 · Bloqueo de la organización (business-rules §14).

**Qué intenta**: dejar a una organización cliente sin ningún administrador activo (desactivándolo
o retirándole la asignación), para que nadie pueda gestionarla; también con dos administradores
que se desactivan el uno al otro **a la vez**.

**Qué lo detiene** (BR-NUC-31): una organización cliente activa conserva siempre al menos un
``administrator`` activo; no se puede desactivar al último ni retirarle la asignación, y la
comprobación resiste dos operaciones simultáneas.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest

from tests.platform_support import Platform, code_of
from vigia_platform.shared.context import Role

pytestmark = pytest.mark.integration


def _active_administrators(platform: Platform, organization_id: uuid.UUID) -> int:
    (row,) = platform.fetch(
        "SELECT count(DISTINCT u.user_id) AS n FROM identity.user_account AS u"
        " JOIN identity.role_assignment AS r ON r.user_id = u.user_id"
        " WHERE u.organization_id = $1 AND u.status = 'active' AND r.role = 'administrator'"
        " AND r.removed_at IS NULL",
        organization_id,
    )
    return int(row["n"])


def _assignment(platform: Platform, user_id: uuid.UUID) -> uuid.UUID:
    (row,) = platform.fetch(
        "SELECT assignment_id FROM identity.role_assignment WHERE user_id = $1"
        " AND role = 'administrator' AND removed_at IS NULL",
        user_id,
    )
    assignment: uuid.UUID = row["assignment_id"]
    return assignment


def test_n15_the_last_administrator_is_neither_deactivated_nor_stripped(
    platform: Platform,
) -> None:
    site = platform.site()
    admin, cookie = platform.person(site.organization_id, Role.ADMINISTRATOR)
    for method, path in (
        ("POST", f"/users/{admin}/deactivate"),
        ("DELETE", f"/users/{admin}/roles/{_assignment(platform, admin)}"),
    ):
        response = platform.call(method, path, cookie=cookie)
        assert response.status_code == 409 and code_of(response) == "conflict", (path, response)
    assert _active_administrators(platform, site.organization_id) == 1
    # Con un segundo administrador, uno de los dos sí puede irse.
    second, _ = platform.person(site.organization_id, Role.ADMINISTRATOR)
    response = platform.call("POST", f"/users/{second}/deactivate", cookie=cookie)
    assert response.status_code == 200, response.text
    assert _active_administrators(platform, site.organization_id) == 1


@pytest.mark.parametrize("rounds", range(3))
def test_n15_two_administrators_removing_each_other_at_once_leave_one(
    platform: Platform, rounds: int
) -> None:
    site = platform.site()
    first, first_cookie = platform.person(site.organization_id, Role.ADMINISTRATOR)
    second, second_cookie = platform.person(site.organization_id, Role.ADMINISTRATOR)

    async def both() -> list[httpx.Response]:
        return list(
            await asyncio.gather(
                platform.send("POST", f"/users/{second}/deactivate", cookie=first_cookie),
                platform.send("POST", f"/users/{first}/deactivate", cookie=second_cookie),
            )
        )

    responses = platform.run(both())
    assert _active_administrators(platform, site.organization_id) == 1
    # Exactamente una desactivación entra; la otra no deja a la organización sin nadie.
    assert sum(r.status_code == 200 for r in responses) == 1, [r.text for r in responses]


@pytest.mark.parametrize("rounds", range(3))
def test_n15_deactivating_and_stripping_at_once_still_leave_one(
    platform: Platform, rounds: int
) -> None:
    site = platform.site()
    first, first_cookie = platform.person(site.organization_id, Role.ADMINISTRATOR)
    second, second_cookie = platform.person(site.organization_id, Role.ADMINISTRATOR)
    assignment = _assignment(platform, second)

    async def both() -> list[Any]:
        return list(
            await asyncio.gather(
                platform.send("DELETE", f"/users/{second}/roles/{assignment}", cookie=first_cookie),
                platform.send("POST", f"/users/{first}/deactivate", cookie=second_cookie),
            )
        )

    responses = platform.run(both())
    assert _active_administrators(platform, site.organization_id) == 1, [r.text for r in responses]
