"""Las lecturas internas no se convierten en asignaciones de rol ni conceden URL de clips."""

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from tests.factories import make_context, session_hash, uuid7
from vigia_platform.catalog.adapters.postgres.catalog_query import scope_parameters
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.ledger.application.coverage import _visible
from vigia_platform.ledger.application.evidence_read import _scope_parameters
from vigia_platform.ledger.application.reader import _Scopes
from vigia_platform.loop.projection.admin import ProjectionRebuildCommand
from vigia_platform.shared.context import (
    Actor,
    ActorKind,
    ActorUnit,
    ContextOrigin,
    Role,
    _seal_scope_context,
)


@pytest.mark.parametrize(
    "kind", [ActorKind.SYSTEM, ActorKind.USER, ActorKind.NODE, ActorKind.OPERATOR]
)
@pytest.mark.parametrize("unit", list(ActorUnit))
@pytest.mark.parametrize("origin", list(ContextOrigin))
@pytest.mark.parametrize("port", ["catalog", "records", "coverage", "markers", "url"])
def test_only_u04_background_system_reads_are_organization_wide(
    kind: ActorKind,
    unit: ActorUnit,
    origin: ContextOrigin,
    port: str,
) -> None:
    organization = uuid.uuid4()
    context = _seal_scope_context(
        organization_id=organization,
        actor=Actor(kind=kind, unit=unit, id=uuid.uuid4(), display_name_snapshot="Actor sintético"),
        origin=origin,
        allowed_scopes=(),
        correlation_id=uuid7(),
        session_id_hash=session_hash() if origin is ContextOrigin.SESSION else None,
    )
    expected = (
        kind is ActorKind.SYSTEM
        and unit is ActorUnit.U04
        and origin in {ContextOrigin.PERIODIC_ITERATION, ContextOrigin.OUTBOX_EVENT}
    )
    values = {
        "catalog": scope_parameters(context)["whole_organization"],
        "records": _Scopes.of(context).parameters()["whole_organization"],
        "coverage": _visible(context, uuid.uuid4(), uuid.uuid4()),
        "markers": _scope_parameters(context, None)["whole_organization"],
        "url": _scope_parameters(context, frozenset({Role.COORDINATOR_SST}))["whole_organization"],
    }
    assert values[port] is (False if port == "url" else expected)


@pytest.mark.parametrize("invalid", ["actor", "origin", "organization"])
def test_rebuild_command_refuses_non_operator_context_before_any_data_access(invalid: str) -> None:
    provider = uuid.uuid4()
    context = make_context(
        kind=ActorKind.USER if invalid == "actor" else ActorKind.OPERATOR,
        origin=ContextOrigin.OUTBOX_EVENT if invalid == "origin" else ContextOrigin.ADMIN_COMMAND,
        organization_id=uuid.uuid4() if invalid == "organization" else provider,
    )
    # Ningún atributo de datos: si la guarda cede, falla antes de poder consultar nada.
    services: Any = SimpleNamespace(provider_organization_id=provider)
    with pytest.raises(ResourceNotFound):
        asyncio.run(
            ProjectionRebuildCommand(services).run(
                context, uuid.uuid4(), uuid.uuid4(), dry_run=False
            )
        )
