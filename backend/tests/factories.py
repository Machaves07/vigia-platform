"""Fábricas de ``ScopeContext`` para las pruebas (datos generados, NFR-CTR-43).

Las pruebas crean contextos con el constructor privado ``_seal_scope_context``: en ``src/`` solo
lo usan los cuatro constructores de ``identity.authz.context`` (TASK-125).
"""

from __future__ import annotations

import hashlib
import os
import uuid

from hypothesis import strategies as st

from vigia_platform.shared.context import (
    Actor,
    ActorKind,
    ActorUnit,
    AllowedScope,
    ContextOrigin,
    Role,
    ScopeContext,
    ScopeLevel,
    _seal_scope_context,
)


def uuid7() -> uuid.UUID:
    """UUID v7 sintético (los bits de tiempo no importan en las pruebas)."""
    value = int.from_bytes(os.urandom(16), "big")
    value &= ~(0xF << 76)
    value |= 0x7 << 76
    value &= ~(0x3 << 62)
    value |= 0x2 << 62
    return uuid.UUID(int=value)


def session_hash() -> str:
    return hashlib.sha256(os.urandom(16)).hexdigest()


def make_context(
    *,
    kind: ActorKind = ActorKind.USER,
    organization_id: uuid.UUID | None = None,
    origin: ContextOrigin | None = None,
    concession_id: uuid.UUID | None = None,
) -> ScopeContext:
    """Contexto válido para ``kind``; bajo concesión, con ``origin = session``.

    ``concession_id`` fija la concesión del actor del proveedor (por defecto, una al azar).
    """
    if kind is ActorKind.PROVIDER_USER:
        concession_id = concession_id or uuid.uuid4()
    elif concession_id is not None:
        raise ValueError("concession_id solo va con el actor del proveedor")
    if origin is None:
        origin = {
            ActorKind.USER: ContextOrigin.SESSION,
            ActorKind.PROVIDER_USER: ContextOrigin.SESSION,
            ActorKind.NODE: ContextOrigin.SESSION,
            ActorKind.SYSTEM: ContextOrigin.OUTBOX_EVENT,
            ActorKind.OPERATOR: ContextOrigin.ADMIN_COMMAND,
        }[kind]
    actor = Actor(
        kind=kind,
        id=uuid.uuid4(),
        display_name_snapshot="Actor sintético",
        unit=ActorUnit.U02,
        role_in_use=Role.COORDINATOR_SST if kind is ActorKind.USER else None,
        concession_id=concession_id,
    )
    return _seal_scope_context(
        organization_id=organization_id or uuid.uuid4(),
        actor=actor,
        origin=origin,
        allowed_scopes=[AllowedScope(ScopeLevel.ORGANIZATION, uuid.uuid4(), Role.COORDINATOR_SST)],
        correlation_id=uuid7(),
        session_id_hash=session_hash() if origin is ContextOrigin.SESSION else None,
    )


@st.composite
def scope_contexts(draw: st.DrawFn) -> ScopeContext:
    """Contextos válidos de cualquier tipo de actor."""
    kind = draw(st.sampled_from(ActorKind))
    return make_context(kind=kind, organization_id=draw(st.uuids(version=4)))
