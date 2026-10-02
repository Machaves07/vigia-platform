"""Operación de la plataforma: reproceso de la cola muerta y rotación de claves (TASK-137).

Claves ``platform.*``: solo ``platform_operator`` con un contexto de la organización
**proveedora** y sin concesión (``identity.authz``); cualquier otro contexto recibe ``not_found``
con ``authorization_denied`` auditado, igual que una ruta inexistente (BR-NUC-09).

La operación es una **orden administrativa** (BR-NUC-03, BR-NUC-82): tras la autorización por
ruta, el manejador construye el contexto del operador con ``context_from_operator`` (el mismo
usuario de la sesión, que tiene que seguir siendo operador activo de la proveedora, y la misma
correlación). Es el contexto con el que la base deja reprocesar la cola muerta
(``vigia.actor_kind = operator``) y con el que se escriben la rotación y su auditoría.

- ``POST /platform/dead-letter/{event_id}/{consumer}/replay`` (``platform.dead_letter.replay``;
  BR-NUC-82): ``DeadLetterReplay`` devuelve la entrega de ``dead_letter`` a ``pending`` con el
  **mismo** ``event_id`` y audita ``dead_letter_replayed`` en una transacción. Sin una entrega en
  cola muerta con ese par: ``not_found``. La cola muerta nunca se borra.
- ``POST /platform/keys/{purpose}/rotate`` (``platform.keys.rotate``; BR-NUC-84 a 86): rota el
  propósito con ``SigningService.rotate`` (clave nueva activa, la anterior ``overlapping``;
  ``key_rotated`` y, si el propósito es del nodo, ``key_set_published`` en la cadena de la
  proveedora) y audita ``key_rotated`` con el propósito y los identificadores de clave
  (BR-NUC-59). Nunca sale material privado: solo identificadores, la clave pública y su vigencia.
  Si la clave ``key_set`` vigente no está disponible para firmar el conjunto: ``conflict``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict

from vigia_platform.identity.authz.authorize import Resource
from vigia_platform.identity.authz.context import ContextUnavailable
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import AuditOperation
from vigia_platform.shared.adapters.http.services import PlatformHttp, platform_http
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.context import ActorKind, ScopeContext
from vigia_platform.shared.signing.keys import SigningPurpose, format_timestamp
from vigia_platform.shared.signing.service import SigningKeyUnavailable, SigningNotReady

__all__ = ["KeyRotatedOut", "ReplayOut", "platform_ops_router"]

Services = Annotated[PlatformHttp, Depends(platform_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReplayOut(_Strict):
    event_id: uuid.UUID
    consumer_name: str
    organization_id: uuid.UUID
    status: Literal["pending"]


class KeyRotatedOut(_Strict):
    purpose: str
    key_id: str
    public_key: str
    valid_from: str
    valid_until: str
    previous_key_id: str | None
    key_set_published: bool


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


async def _operator(request: Request, services: PlatformHttp) -> ScopeContext:
    """La orden administrativa del operador de la sesión (``context_from_operator``).

    Solo un usuario de la proveedora, sin concesión, que siga siendo ``platform_operator`` activo;
    si no, ``not_found`` (como la autorización por ruta, nunca ``forbidden``).
    """
    session = request_context(request)
    if (
        session.organization_id != services.provider_organization_id
        or session.concession_id is not None
        or session.actor.kind is not ActorKind.USER
    ):
        raise ApiError(ApiErrorCode.NOT_FOUND)
    try:
        return await services.operators.context_from_operator(
            session.actor.id, correlation_id=session.correlation_id
        )
    except ContextUnavailable:
        raise ApiError(ApiErrorCode.NOT_FOUND) from None


def platform_ops_router() -> APIRouter:
    router = APIRouter(tags=["operación"])

    @router.post(
        "/platform/dead-letter/{event_id}/{consumer}/replay",
        dependencies=[requires(PermissionKey.PLATFORM_DEAD_LETTER_REPLAY.value)],
        summary="Reentrega desde la cola muerta con el mismo event_id (solo el operador)",
    )
    async def replay(
        event_id: uuid.UUID,
        consumer: str,
        request: Request,
        response: Response,
        services: Services,
    ) -> ReplayOut:
        _no_store(response)
        operator = await _operator(request, services)
        receipt = await services.dead_letter.replay(operator, event_id, consumer)
        return ReplayOut(
            event_id=receipt.event_id,
            consumer_name=receipt.consumer_name,
            organization_id=receipt.organization_id,
            status="pending",
        )

    @router.post(
        "/platform/keys/{purpose}/rotate",
        dependencies=[requires(PermissionKey.PLATFORM_KEYS_ROTATE.value)],
        summary="Rota la clave de firma de un propósito (solo el operador)",
    )
    async def rotate(
        purpose: SigningPurpose, request: Request, response: Response, services: Services
    ) -> KeyRotatedOut:
        _no_store(response)
        operator = await _operator(request, services)
        authorized = await services.authorizer.authorize(
            operator,
            PermissionKey.PLATFORM_KEYS_ROTATE,
            Resource.organization(services.provider_organization_id),
        )
        try:
            result = await services.signing.rotate(purpose, context=authorized)
        except SigningKeyUnavailable:
            raise ApiError(ApiErrorCode.CONFLICT) from None
        except SigningNotReady:
            raise ApiError(ApiErrorCode.TEMPORARILY_UNAVAILABLE) from None
        key = result.new_key
        filters: dict[str, str | None] = {
            "purpose": key.purpose.value,
            "key_id": key.key_id,
            "previous_key_id": result.previous_key_id,
        }
        await services.audit.append(authorized, AuditOperation.KEY_ROTATED, filters=filters)
        return KeyRotatedOut(
            purpose=key.purpose.value,
            key_id=key.key_id,
            public_key=key.public_key,
            valid_from=format_timestamp(key.valid_from),
            valid_until=format_timestamp(key.valid_until),
            previous_key_id=result.previous_key_id,
            key_set_published=result.publication is not None,
        )

    return router
