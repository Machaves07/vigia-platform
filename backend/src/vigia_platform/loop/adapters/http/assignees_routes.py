"""La lista de responsables posibles de la acción correctiva (TASK-583; O-3; A-126; SUP-U05-07).

``GET /loop/findings/{finding_state_id}/assignees`` (``actions.manage`` sobre la zona del
hallazgo, la misma clave que asignar) con ``cursor`` y ``limit`` (1 a 200, 50 por defecto).
Responde ``{items, next_cursor}``; cada elemento lleva **solo** ``user_id``, ``display_name`` y
``roles`` (los de A-126 que cubren la zona). Es el mismo conjunto que ``POST
.../actions`` acepta como ``assignee_user_id`` (``loop.application.assignees``).

No hay parámetro de búsqueda ni de filtro por persona (BR-APP-21): cualquier parámetro que no sea
``cursor`` o ``limit``, o uno repetido, es ``invalid_request``; la consola busca en la lista ya
cargada. Un hallazgo inexistente, de otra organización, fuera de alcance o sin el permiso responde
``not_found`` (nunca ``forbidden``). ``Cache-Control: no-store``. Sin auditoría de consulta.
"""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store
from vigia_platform.loop.adapters.http.services import LoopHttp, installed, loop_http
from vigia_platform.loop.application.assignees import (
    DEFAULT_ASSIGNEES_PAGE,
    MAX_ASSIGNEES_PAGE,
    Assignee,
    AssigneePage,
    AssigneeRequestInvalid,
)
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context

__all__ = [
    "ASSIGNEES_PATH",
    "AssigneeOut",
    "AssigneePageOut",
    "AssigneeRole",
    "assignees_router",
    "decode_assignee_cursor",
    "encode_assignee_cursor",
]

ASSIGNEES_PATH: Final = "/loop/findings/{finding_state_id}/assignees"
CURSOR_CHARS: Final = 22
"""Un ``user_id`` (16 bytes) en base64url sin relleno."""
_CURSOR: Final = re.compile(r"[A-Za-z0-9_-]{22}")
_MANAGE: Final = PermissionKey.ACTIONS_MANAGE.value

Services = Annotated[LoopHttp, Depends(loop_http)]
AssigneeRole = Literal["coordinator_sst", "line_manager", "plant_manager"]
"""Los tres roles de A-126 en el esquema (los de ``ASSIGNEE_ROLES``; lo comprueba la unidad)."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AssigneeOut(_Strict):
    """Una persona que puede ser responsable: un usuario de la plataforma, nunca una persona
    observada (BR-APP-33). Solo lo necesario para elegirla."""

    user_id: uuid.UUID
    display_name: str
    roles: tuple[AssigneeRole, ...]
    """Los roles con los que cubre la zona: ``coordinator_sst``, ``line_manager`` o
    ``plant_manager`` (A-126)."""


class AssigneePageOut(_Strict):
    items: tuple[AssigneeOut, ...]
    """Por ``display_name`` y ``user_id``."""
    next_cursor: str | None
    """Pásalo como ``cursor`` para la página siguiente; ``null`` si no hay más."""


def encode_assignee_cursor(user_id: uuid.UUID) -> str:
    """Cursor opaco: el ``user_id`` del último de la página."""
    return base64.urlsafe_b64encode(user_id.bytes).decode("ascii").rstrip("=")


def decode_assignee_cursor(value: str) -> uuid.UUID:
    """El cursor de ``encode_assignee_cursor``; cualquier otra cosa, ``invalid_request``."""
    if _CURSOR.fullmatch(value) is None:
        raise ApiError(ApiErrorCode.INVALID_REQUEST)
    try:
        return uuid.UUID(bytes=base64.urlsafe_b64decode(value + "=="))
    except (ValueError, binascii.Error):
        raise ApiError(ApiErrorCode.INVALID_REQUEST) from None


def _view(item: Assignee) -> AssigneeOut:
    roles: tuple[AssigneeRole, ...] = tuple(
        role.value  # type: ignore[misc]  # ASSIGNEE_ROLES son exactamente los tres de AssigneeRole
        for role in item.roles
    )
    return AssigneeOut(user_id=item.user_id, display_name=item.display_name, roles=roles)


def page_view(page: AssigneePage) -> AssigneePageOut:
    return AssigneePageOut(
        items=tuple(_view(item) for item in page.items),
        next_cursor=None if page.next_cursor is None else encode_assignee_cursor(page.next_cursor),
    )


def assignees_router() -> APIRouter:
    router = APIRouter(tags=["lazo"])

    @router.get(
        ASSIGNEES_PATH,
        dependencies=[requires(_MANAGE), Depends(exact_query("cursor", "limit"))],
        summary="Personas que pueden ser responsables de la acción del hallazgo",
    )
    async def list_assignees(
        request: Request,
        response: Response,
        services: Services,
        finding_state_id: uuid.UUID,
        cursor: Annotated[str | None, Query(max_length=CURSOR_CHARS)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_ASSIGNEES_PAGE)] = DEFAULT_ASSIGNEES_PAGE,
    ) -> AssigneePageOut:
        no_store(response)
        after = None if cursor is None else decode_assignee_cursor(cursor)
        try:
            page = await installed(services.assignees).page(
                request_context(request), finding_state_id, after=after, limit=limit
            )
        except AssigneeRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        return page_view(page)

    return router
