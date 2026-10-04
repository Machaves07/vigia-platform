"""Solo ``node_api`` llama al quinto constructor (A-51; TASK-206) y su regla, sin base.

**Prueba de importación** (como la de ``_seal_scope_context`` en ``test_scope_context.py``): el
árbol sintáctico de cada módulo de ``src/`` se recorre buscando ``context_from_node`` y
``context_from_node_enrollment`` (nombre, atributo, importación o cadena); solo pueden nombrarlos
su definición (``identity/authz/context.py``) y ``node_api/``. ``test_the_check_names_an_intruder``
demuestra que la comprobación falla con un módulo intruso.

**Regla de ``context_from_node``** (``ScopeContexts`` real con un almacén en memoria): actor
``node``, origen ``node_request``, organización del certificado, sin ``allowed_scopes`` (un nodo
no tiene rol en la matriz) y como alcance solo las zonas asignadas **ahora**; ``node_revoked``,
``node_not_enrolled`` y ``node_zone_mismatch`` según identidad, marca de flota, credencial
(``active`` en vigencia; ``overlapping`` 24 h desde el ``issued_at`` de su sucesora) y planta.
"""

from __future__ import annotations

import ast
import asyncio
import datetime as dt
import uuid
from pathlib import Path

import pytest

from tests.node_api_support import DAY, MemoryNodeStore, NodeFixture, scope_contexts
from vigia_platform.identity.authz.context import (
    OVERLAP,
    NodeAssignment,
    NodeContextReason,
    NodeContextRejected,
    NodeScope,
    PresentedNode,
)
from vigia_platform.identity.authz.matrix import effective_permissions
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import ActorKind, ContextOrigin

BACKEND = Path(__file__).resolve().parents[2]
SRC = BACKEND / "src" / "vigia_platform"
CONSTRUCTORS = frozenset({"context_from_node", "context_from_node_enrollment"})
DEFINITION = SRC / "identity" / "authz" / "context.py"
ALLOWED_PACKAGE = SRC / "node_api"


def _names(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in CONSTRUCTORS:
            found.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in CONSTRUCTORS:
            found.add(node.attr)
        elif isinstance(node, ast.alias) and node.name.split(".")[-1] in CONSTRUCTORS:
            found.add(node.name)
        elif isinstance(node, ast.Constant) and node.value in CONSTRUCTORS:
            found.add(str(node.value))
    return found


def intruders(root: Path) -> list[str]:
    """Módulos de ``root`` que nombran el quinto constructor sin ser su definición ni node_api."""
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path == DEFINITION or ALLOWED_PACKAGE in path.parents:
            continue
        if _names(ast.parse(path.read_text(encoding="utf-8"))):
            found.append(str(path.relative_to(root)))
    return found


def test_only_node_api_calls_the_fifth_constructor() -> None:
    assert intruders(SRC) == []
    # Y node_api sí lo llama (la identidad del nodo de cada petición).
    assert _names(ast.parse((ALLOWED_PACKAGE / "identity.py").read_text(encoding="utf-8")))


def test_the_check_names_an_intruder(tmp_path: Path) -> None:
    (tmp_path / "fleet").mkdir()
    (tmp_path / "fleet" / "atajo.py").write_text(
        "async def f(contexts, store, presented):\n"
        "    return await contexts.context_from_node(store, presented)\n",
        encoding="utf-8",
    )
    (tmp_path / "ledger.py").write_text(
        "from vigia_platform.identity.authz import context\n"
        "builder = getattr(context.ScopeContexts, 'context_from_node_enrollment')\n",
        encoding="utf-8",
    )
    assert intruders(tmp_path) == ["fleet/atajo.py", "ledger.py"]


# --- Regla del constructor ---------------------------------------------------------------------

NOW = dt.datetime(2026, 10, 4, 12, 0, tzinfo=dt.UTC)


def _setup() -> tuple[MemoryNodeStore, NodeFixture, PresentedNode, SimulatedClock]:
    clock = SimulatedClock(NOW)
    node = NodeFixture(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), enrolled_at=NOW - 30 * DAY)
    node.assignments.append(NodeAssignment(uuid.uuid4(), NOW - 30 * DAY, None))
    node.credentials["abc1"] = {
        "status": "active",
        "issued_at": NOW - DAY,
        "expires_at": NOW + 300 * DAY,
    }
    store = MemoryNodeStore(nodes={node.node_id: node})
    presented = PresentedNode(node.node_id, node.organization_id, node.plant_id, "abc1")
    return store, node, presented, clock


def _resolve(store: MemoryNodeStore, presented: PresentedNode, clock: SimulatedClock) -> NodeScope:
    return asyncio.run(scope_contexts(clock).context_from_node(store, presented))


def _reason(store: MemoryNodeStore, presented: PresentedNode, clock: SimulatedClock) -> str:
    with pytest.raises(NodeContextRejected) as raised:
        _resolve(store, presented, clock)
    return raised.value.reason.value


def test_the_node_context_is_the_certificate_organization_with_only_its_zones() -> None:
    store, node, presented, clock = _setup()
    scope = _resolve(store, presented, clock)
    context = scope.context
    assert context.organization_id == node.organization_id
    assert context.actor.kind is ActorKind.NODE and context.actor.id == node.node_id
    assert context.origin is ContextOrigin.NODE_REQUEST
    assert context.allowed_scopes == () and effective_permissions(context.allowed_scopes) == set()
    assert context.concession_id is None and context.session_id_hash is None
    assert scope.zone_ids == frozenset({node.assignments[0].zone_id})
    assert len(store.lookups) == 1


def test_zones_outside_their_assignment_interval_are_out_of_scope() -> None:
    store, node, presented, clock = _setup()
    past, future, closed_now = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    node.assignments += [
        NodeAssignment(past, NOW - 10 * DAY, NOW - DAY),
        NodeAssignment(future, NOW + DAY, None),
        NodeAssignment(closed_now, NOW - DAY, NOW),
    ]
    scope = _resolve(store, presented, clock)
    assert scope.zone_ids == frozenset({node.assignments[0].zone_id})


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"node_status": "revoked"}, NodeContextReason.REVOKED),
        ({"revoked_at": NOW - DAY}, NodeContextReason.REVOKED),
        ({"decommissioned_at": NOW - DAY}, NodeContextReason.REVOKED),
        ({"node_status": "declared"}, NodeContextReason.NOT_ENROLLED),
        ({"node_status": "re_enrollment_pending"}, NodeContextReason.NOT_ENROLLED),
        ({"enrolled_at": None}, NodeContextReason.NOT_ENROLLED),
    ],
)
def test_the_identity_decides_revoked_or_not_enrolled(
    change: dict[str, object], reason: NodeContextReason
) -> None:
    store, node, presented, clock = _setup()
    for name, value in change.items():
        setattr(node, name, value)
    assert _reason(store, presented, clock) == reason.value


@pytest.mark.parametrize(
    ("credential", "reason"),
    [
        ({"status": "revoked"}, NodeContextReason.REVOKED),
        ({"status": "superseded"}, NodeContextReason.REVOKED),
        ({"expires_at": NOW}, NodeContextReason.NOT_ENROLLED),
        ({"issued_at": NOW + DAY}, NodeContextReason.NOT_ENROLLED),
        ({"status": "overlapping"}, NodeContextReason.REVOKED),
        (
            {"status": "overlapping", "successor_issued_at": NOW - OVERLAP},
            NodeContextReason.REVOKED,
        ),
        (
            {"status": "overlapping", "successor_issued_at": NOW - DAY, "expires_at": NOW},
            NodeContextReason.NOT_ENROLLED,
        ),
    ],
)
def test_the_credential_decides(credential: dict[str, object], reason: NodeContextReason) -> None:
    store, node, presented, clock = _setup()
    node.credentials["abc1"].update(credential)
    assert _reason(store, presented, clock) == reason.value


def test_an_overlapping_credential_authenticates_24_hours_from_its_successor() -> None:
    store, node, presented, clock = _setup()
    successor = NOW - OVERLAP + dt.timedelta(seconds=1)
    node.credentials["abc1"].update({"status": "overlapping", "successor_issued_at": successor})
    assert _resolve(store, presented, clock).credential_status == "overlapping"
    clock.advance(1)
    assert _reason(store, presented, clock) == NodeContextReason.REVOKED.value


@pytest.mark.parametrize("what", ["organization", "plant", "node", "serial"])
def test_a_certificate_that_does_not_match_the_row(what: str) -> None:
    store, _, presented, clock = _setup()
    changes = {
        "organization": ("organization_id", uuid.uuid4(), NodeContextReason.NOT_ENROLLED),
        "plant": ("plant_id", uuid.uuid4(), NodeContextReason.ZONE_MISMATCH),
        "node": ("node_id", uuid.uuid4(), NodeContextReason.NOT_ENROLLED),
        "serial": ("certificate_serial", "ff", NodeContextReason.NOT_ENROLLED),
    }
    field, value, reason = changes[what]
    other = PresentedNode(**{**_presented_fields(presented), field: value})
    assert _reason(store, other, clock) == reason.value


def _presented_fields(presented: PresentedNode) -> dict[str, object]:
    return {
        "node_id": presented.node_id,
        "organization_id": presented.organization_id,
        "plant_id": presented.plant_id,
        "certificate_serial": presented.certificate_serial,
    }


def test_the_enrollment_context_is_the_declared_node_organization() -> None:
    store, node, _, clock = _setup()
    node.node_status = "declared"
    scope = asyncio.run(scope_contexts(clock).context_from_node_enrollment(store, node.node_id))
    assert scope is not None
    assert scope.context.organization_id == node.organization_id
    assert scope.context.actor.kind is ActorKind.NODE and scope.node_status == "declared"
    assert scope.context.allowed_scopes == ()
    unknown = asyncio.run(scope_contexts(clock).context_from_node_enrollment(store, uuid.uuid4()))
    assert unknown is None
