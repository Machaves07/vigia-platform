"""``ScopeContext`` inmutable y sin constructor público; ``shared.clock`` reexporta el de U-01.

BR-NUC-03 (solo cuatro constructores), ``domain-entities.md`` §3.2 y §4.1, LC-NUC-32.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
import vigia_contracts.clock as contract_clock

from tests.factories import make_context, session_hash, uuid7
from vigia_platform.shared import clock
from vigia_platform.shared.context import (
    DISPLAY_NAME_MAX_CHARS,
    Actor,
    ActorKind,
    ActorUnit,
    AllowedScope,
    ContextAbsent,
    ContextOrigin,
    Role,
    ScopeContext,
    ScopeLevel,
    _seal_scope_context,
)

BACKEND = Path(__file__).resolve().parents[2]
SRC = BACKEND / "src" / "vigia_platform"
PRIVATE_BUILDER = "_seal_scope_context"
ALLOWED_BUILDER_MODULES = {
    SRC / "shared" / "context.py",
    SRC / "identity" / "authz" / "context.py",
}
"""Módulos de ``src/`` que pueden nombrar el constructor privado: su definición y los cuatro
constructores de TASK-125 (BR-NUC-03)."""


def _actor(kind: ActorKind = ActorKind.USER, **changes: Any) -> Actor:
    fields: dict[str, Any] = {
        "kind": kind,
        "id": uuid.uuid4(),
        "display_name_snapshot": "Actor sintético",
        "unit": ActorUnit.U02,
        "concession_id": uuid.uuid4() if kind is ActorKind.PROVIDER_USER else None,
    }
    fields.update(changes)
    return Actor(**fields)


def _seal(**changes: Any) -> ScopeContext:
    fields: dict[str, Any] = {
        "organization_id": uuid.uuid4(),
        "actor": _actor(),
        "origin": ContextOrigin.SESSION,
        "allowed_scopes": [],
        "correlation_id": uuid7(),
        "session_id_hash": session_hash(),
    }
    fields.update(changes)
    return _seal_scope_context(**fields)


def test_scope_context_has_no_public_constructor() -> None:
    context = make_context()
    fields = {f.name: getattr(context, f.name) for f in dataclasses.fields(ScopeContext)}
    fields.pop("_seal")
    with pytest.raises(TypeError, match="constructor público"):
        ScopeContext(**fields)
    with pytest.raises(TypeError, match="constructor público"):
        ScopeContext(**fields, _seal=object())


def test_replace_cannot_forge_a_context_from_an_existing_one() -> None:
    context = make_context()
    with pytest.raises(TypeError, match="constructor público"):
        dataclasses.replace(context, organization_id=uuid.uuid4())


def test_scope_context_is_immutable() -> None:
    context = make_context()
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.organization_id = uuid.uuid4()  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.actor.kind = ActorKind.SYSTEM  # type: ignore[misc]
    assert type(context.allowed_scopes) is tuple


def test_allowed_scopes_list_is_frozen_into_a_tuple() -> None:
    scopes = [AllowedScope(ScopeLevel.PLANT, uuid.uuid4(), Role.LINE_MANAGER)]
    context = _seal(allowed_scopes=scopes)
    scopes.append(AllowedScope(ScopeLevel.ZONE, uuid.uuid4(), Role.COPASST))
    assert len(context.allowed_scopes) == 1


@pytest.mark.parametrize("kind", list(ActorKind))
def test_valid_context_for_every_actor_kind(kind: ActorKind) -> None:
    context = make_context(kind=kind)
    assert context.actor.kind is kind
    assert (context.concession_id is not None) == (kind is ActorKind.PROVIDER_USER)
    assert context.concession_id == context.actor.concession_id


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"organization_id": "00000000-0000-0000-0000-000000000000"}, TypeError),
        ({"organization_id": None}, TypeError),
        ({"correlation_id": uuid.uuid4()}, ValueError),  # v4, no v7
        ({"correlation_id": str(uuid7())}, TypeError),
        ({"origin": "session"}, TypeError),
        ({"actor": {"kind": "user"}}, TypeError),
        ({"allowed_scopes": ["organization"]}, TypeError),
        ({"session_id_hash": None}, ValueError),  # sesión sin hash
        ({"session_id_hash": session_hash().upper()}, ValueError),
        ({"session_id_hash": session_hash()[:-1]}, ValueError),
        ({"session_id_hash": session_hash() + "0"}, ValueError),
        ({"session_id_hash": session_hash()[:-1] + "\n"}, ValueError),
        ({"origin": ContextOrigin.OUTBOX_EVENT}, ValueError),  # hash sin sesión
    ],
)
def test_invalid_context_is_rejected(changes: dict[str, Any], error: type[Exception]) -> None:
    with pytest.raises(error):
        _seal(**changes)


def test_concession_only_under_session() -> None:
    provider = _actor(ActorKind.PROVIDER_USER)
    assert _seal(actor=provider).concession_id == provider.concession_id
    with pytest.raises(ValueError, match="origin = session"):
        _seal(actor=provider, origin=ContextOrigin.PERIODIC_ITERATION, session_id_hash=None)


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"kind": ActorKind.PROVIDER_USER, "concession_id": None}, ValueError),
        ({"kind": ActorKind.USER, "concession_id": uuid.uuid4()}, ValueError),
        ({"kind": "user"}, TypeError),
        ({"id": str(uuid.uuid4())}, TypeError),
        ({"display_name_snapshot": ""}, ValueError),
        ({"display_name_snapshot": "x" * (DISPLAY_NAME_MAX_CHARS + 1)}, ValueError),
        ({"display_name_snapshot": None}, TypeError),
        ({"unit": "U-02"}, TypeError),
        ({"role_in_use": "copasst"}, TypeError),
    ],
)
def test_invalid_actor_is_rejected(changes: dict[str, Any], error: type[Exception]) -> None:
    kind = changes.pop("kind", ActorKind.USER)
    with pytest.raises(error):
        _actor(kind, **changes)


def test_display_name_limits() -> None:
    assert len(_actor(display_name_snapshot="x" * DISPLAY_NAME_MAX_CHARS).display_name_snapshot)
    assert _actor(display_name_snapshot="x").display_name_snapshot == "x"


def test_allowed_scope_is_closed() -> None:
    with pytest.raises(TypeError):
        AllowedScope("plant", uuid.uuid4(), Role.COPASST)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        AllowedScope(ScopeLevel.PLANT, uuid.uuid4(), "copasst")  # type: ignore[arg-type]


def test_context_absent_code() -> None:
    assert ContextAbsent().code == "context_absent"


def test_only_allowed_modules_name_the_private_builder() -> None:
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path in ALLOWED_BUILDER_MODULES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Name | ast.Attribute):
                names.append(node.id if isinstance(node, ast.Name) else node.attr)
            elif isinstance(node, ast.alias):
                names.append(node.name)
            if PRIVATE_BUILDER in names or "_SEAL" in names:
                offenders.append(f"{path.relative_to(BACKEND)}:{getattr(node, 'lineno', 0)}")
    assert offenders == []


def test_context_module_imports_without_fastapi_or_sqlalchemy() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(BACKEND / "tools" / "check_isolated_imports.py"),
            "--modules",
            "vigia_platform.shared.context",
            "vigia_platform.shared.clock",
        ],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout)["ok"] is True


def test_clock_reexports_the_contract_clock() -> None:
    assert clock.Clock is contract_clock.Clock
    assert clock.SystemClock is contract_clock.SystemClock
    assert clock.SimulatedClock is contract_clock.SimulatedClock
    assert set(clock.__all__) == {"Clock", "SystemClock", "SimulatedClock"}
