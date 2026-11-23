"""TASK-583: los límites propios de ``AssigneeService.page`` y el cursor de la ruta, sin base.

La ruta ya valida ``limit`` (1 a 200) y el cursor; el servicio repite los límites para quien lo
llame directamente: fuera de ellos, ``AssigneeRequestInvalid`` **antes** de tocar la base ni la
autorización (los dobles fallan si se tocan). ``ASSIGNEE_ROLES`` son exactamente los tres de A-126.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Final, get_args

import pytest

from tests.factories import make_context
from vigia_platform.loop.adapters.http.assignees_routes import (
    AssigneeRole,
    decode_assignee_cursor,
    encode_assignee_cursor,
)
from vigia_platform.loop.application.actions import ASSIGNEE_ROLES
from vigia_platform.loop.application.assignees import (
    DEFAULT_ASSIGNEES_PAGE,
    MAX_ASSIGNEES_PAGE,
    AssigneeRequestInvalid,
    AssigneeService,
)
from vigia_platform.shared.api.errors import ApiError
from vigia_platform.shared.context import Role

FINDING: Final = uuid.UUID("0190a1b2-2222-7000-8000-000000000009")


class _Poison:
    """Cualquier uso es un fallo: el límite se comprueba antes de la base y de la autorización."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"se tocó {name}")


def _service() -> AssigneeService:
    poison: Any = _Poison()
    return AssigneeService(database=poison, authorizer=poison)


def test_the_roles_of_a_126_are_exactly_three() -> None:
    assert ASSIGNEE_ROLES == (Role.COORDINATOR_SST, Role.LINE_MANAGER, Role.PLANT_MANAGER)


def test_the_schema_admits_exactly_the_three_roles_of_a_126() -> None:
    assert get_args(AssigneeRole) == tuple(role.value for role in ASSIGNEE_ROLES)


def test_the_page_limits_are_those_of_br_app_53() -> None:
    assert (DEFAULT_ASSIGNEES_PAGE, MAX_ASSIGNEES_PAGE) == (50, 200)


@pytest.mark.parametrize("limit", [0, -1, 201, True, 2.0, "50"])
def test_a_limit_out_of_range_is_invalid_before_touching_anything(limit: Any) -> None:
    with pytest.raises(AssigneeRequestInvalid):
        asyncio.run(_service().page(make_context(), FINDING, limit=limit))


@pytest.mark.parametrize("after", ["0190a1b2-4444-7000-8000-000000000001", 7, b"x" * 16])
def test_an_after_that_is_not_a_uuid_is_invalid_before_touching_anything(after: Any) -> None:
    with pytest.raises(AssigneeRequestInvalid):
        asyncio.run(_service().page(make_context(), FINDING, after=after))


@pytest.mark.parametrize("limit", [1, 200])
def test_the_borders_reach_the_authorization(limit: int) -> None:
    """1 y 200 pasan el límite: lo siguiente es la base (el doble falla al tocarla)."""
    with pytest.raises(AssertionError, match="se tocó read"):
        asyncio.run(_service().page(make_context(), FINDING, limit=limit))


def test_the_cursor_round_trips_and_rejects_what_is_not_its_alphabet() -> None:
    user = uuid.UUID("0190a1b2-4444-7000-8000-00000000abcd")
    cursor = encode_assignee_cursor(user)
    assert len(cursor) == 22
    assert decode_assignee_cursor(cursor) == user
    # «+» y «/» son de base64 estándar: el decodificador los aceptaría; el patrón los rechaza.
    for value in ("A" * 20 + "+A", "A" * 20 + "/A", "A" * 21, "A" * 23, "", "Ñ" * 22):
        with pytest.raises(ApiError):
            decode_assignee_cursor(value)
