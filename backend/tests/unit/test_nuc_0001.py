"""``nuc_0001`` envía verificadores SCRAM, nunca la contraseña en claro (TASK-106, NFR-NUC-20).

PostgreSQL cifra igual una contraseña en claro, así que las pruebas de integración no distinguen
qué recibió el servidor (revisión de VIG-31, menor 2). Aquí ``upgrade()`` corre con un ``op`` que
anota cada sentencia y sus parámetros, y se exige que cada contraseña llegue solo como
verificador ``SCRAM-SHA-256$4096:...`` y no aparezca en ninguna sentencia ni parámetro.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.sql.elements import TextClause

from vigia_platform.shared.role_passwords import RolePasswordError

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "nuc_0001_roles_schemas_global_tables.py"
)
APP_VALUE = "app-plain-value-0123456789"
MIGRATE_VALUE = "migrate-plain-value-012345"


class RecordingOp:
    def __init__(self) -> None:
        self.statements: list[tuple[str, dict[str, Any]]] = []

    def execute(self, statement: object) -> None:
        if isinstance(statement, TextClause):
            self.statements.append((statement.text, dict(statement.compile().params)))
        else:
            self.statements.append((str(statement), {}))


def _load(
    monkeypatch: pytest.MonkeyPatch, passwords: dict[str, str]
) -> tuple[ModuleType, RecordingOp]:
    spec = importlib.util.spec_from_file_location("nuc_0001_under_test", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    recorder = RecordingOp()
    monkeypatch.setattr(module, "op", recorder)
    context = SimpleNamespace(config=SimpleNamespace(attributes={"role_passwords": passwords}))
    monkeypatch.setattr(module, "context", context)
    return module, recorder


def test_only_scram_verifiers_reach_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    module, recorder = _load(monkeypatch, {"vigia_app": APP_VALUE, "vigia_migrate": MIGRATE_VALUE})
    module.upgrade()

    staged = [
        (index, params["verifier"])
        for index, (_, params) in enumerate(recorder.statements)
        if "verifier" in params
    ]
    assert len(staged) == 2
    for _, verifier in staged:
        assert verifier.startswith("SCRAM-SHA-256$4096:")
    # Cada verificador se deja en la variable justo antes del bloque de su rol, y se borra después.
    texts = [text for text, _ in recorder.statements]
    assert "ROLE vigia_app" in texts[staged[0][0] + 1]
    assert "ROLE vigia_migrate" in texts[staged[1][0] + 1]
    assert "set_config('vigia.role_verifier', '', true)" in texts[staged[1][0] + 2]
    everything = repr(recorder.statements)
    assert APP_VALUE not in everything
    assert MIGRATE_VALUE not in everything


@pytest.mark.parametrize("missing", ["vigia_app", "vigia_migrate"])
def test_missing_password_fails_before_any_statement(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    passwords = {"vigia_app": APP_VALUE, "vigia_migrate": MIGRATE_VALUE}
    del passwords[missing]
    module, recorder = _load(monkeypatch, passwords)
    with pytest.raises(RolePasswordError, match=f"falta la contraseña de {missing}"):
        module.upgrade()
    assert recorder.statements == []


def test_ownership_change_grants_create_on_public_only_around_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revisión de VIG-31, bloqueante 1: sin SUPERUSER el nuevo dueño necesita CREATE en
    ``public`` para ``ALTER TABLE ... OWNER``; se concede justo antes y se retira justo después."""
    module, recorder = _load(monkeypatch, {"vigia_app": APP_VALUE, "vigia_migrate": MIGRATE_VALUE})
    module.upgrade()
    texts = [text for text, _ in recorder.statements]
    owner = texts.index("ALTER TABLE public.alembic_version OWNER TO vigia_migrate")
    assert texts[owner - 1] == "GRANT CREATE ON SCHEMA public TO vigia_migrate"
    assert texts[owner + 1] == "REVOKE CREATE ON SCHEMA public FROM vigia_migrate"
