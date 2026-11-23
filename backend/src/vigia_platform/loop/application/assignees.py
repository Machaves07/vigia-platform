"""``loop.assignees`` (TASK-583; O-3 del recorrido SST; A-126; BR-LAZ-38; BR-APP-21, 33 y 53).

``AssigneeService.page(context, finding_state_id, after=..., limit=...)`` es la lista de las
personas que pueden ser **responsables** de la acción correctiva de un hallazgo, para elegirla en
la consola (``GET /loop/findings/{finding_state_id}/assignees``):

1. **Alcance**: la fila del hallazgo de la organización y ``actions.manage`` sobre **su zona**,
   exactamente como ``assign`` (``authorized_finding``). Inexistente, de otra organización, fuera
   de alcance, sin el permiso o bajo concesión de proveedor: ``ResourceNotFound``
   (``not_found``, BR-NUC-09 y BR-LAZ-01), con su ``authorization_denied`` auditado.
2. **El mismo conjunto que acepta ``assign``** (A-126): la sentencia ``ASSIGNEES`` y sus
   parámetros (``assignee_parameters``) de ``loop.application.actions``, los mismos de su paso 5.
   Un usuario está en la lista si y solo si ``assign`` lo acepta como responsable.
3. **Minimización**: por persona, solo ``user_id``, ``display_name`` y los roles de
   ``ASSIGNEE_ROLES`` que cubren la zona. Sin correo ni ningún otro dato. No hay búsqueda ni
   filtro por persona (BR-APP-21): la consola busca en la lista ya cargada.
4. **Paginación por clave** sobre ``(display_name, user_id)`` (BR-APP-53): ``limit`` de 1 a 200
   (50 por defecto); ``next_cursor`` es el ``user_id`` del último de la página. Un cursor que no
   es de un usuario de la organización da una página vacía, igual que el final de la lista.

Sin auditoría de consulta (como la bandeja, BR-LAZ-09): no lee el expediente, solo el directorio
de usuarios de la organización. Una sola lectura ``READ ONLY`` con el contexto ya autorizado.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from vigia_platform.identity.authz.authorize import Authorizer
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.loop.application.actions import (
    ASSIGNEES,
    assignee_parameters,
    authorized_finding,
)
from vigia_platform.shared.context import Role, ScopeContext, repository

__all__ = [
    "DEFAULT_ASSIGNEES_PAGE",
    "MAX_ASSIGNEES_PAGE",
    "Assignee",
    "AssigneePage",
    "AssigneeRequestInvalid",
    "AssigneeService",
]

DEFAULT_ASSIGNEES_PAGE: Final = 50
MAX_ASSIGNEES_PAGE: Final = 200
"""Página por defecto y máxima de la lista (BR-APP-53)."""


class AssigneeRequestInvalid(Exception):
    """Una petición fuera de los límites de la lista (``invalid_request``)."""


@dataclass(frozen=True, slots=True)
class Assignee:
    """Una persona que puede ser responsable: lo mínimo para elegirla."""

    user_id: uuid.UUID
    display_name: str
    roles: tuple[Role, ...]
    """Los roles de ``ASSIGNEE_ROLES`` con los que cubre la zona, sin repetir y ordenados."""


@dataclass(frozen=True, slots=True)
class AssigneePage:
    items: tuple[Assignee, ...]
    next_cursor: uuid.UUID | None
    """El ``user_id`` del último de la página si hay más; ``None`` al final."""


@repository
class AssigneeService:
    """La lista de responsables posibles de la acción de un hallazgo (TASK-583)."""

    def __init__(self, *, database: LedgerDatabase, authorizer: Authorizer) -> None:
        self._database = database
        self._authorizer = authorizer

    def __repr__(self) -> str:
        return "AssigneeService()"

    async def page(
        self,
        context: ScopeContext,
        finding_state_id: uuid.UUID,
        *,
        after: uuid.UUID | None = None,
        limit: int = DEFAULT_ASSIGNEES_PAGE,
    ) -> AssigneePage:
        """La página de responsables posibles desde ``after`` (excluido), por nombre."""
        if type(limit) is not int or not 1 <= limit <= MAX_ASSIGNEES_PAGE:
            raise AssigneeRequestInvalid("limit fuera de 1 a 200")
        if after is not None and type(after) is not uuid.UUID:
            raise AssigneeRequestInvalid("after es un UUID")
        authorized, place = await authorized_finding(
            self._database, self._authorizer, context, finding_state_id
        )
        rows = await self._database.read(
            authorized,
            ASSIGNEES,
            assignee_parameters(place, after=after, limit=limit + 1),
        )
        items = tuple(
            Assignee(
                user_id=uuid.UUID(str(row.user_id)),
                display_name=str(row.display_name),
                roles=tuple(Role(role) for role in row.roles),
            )
            for row in rows[:limit]
        )
        more = len(rows) > limit
        return AssigneePage(items, items[-1].user_id if more else None)
