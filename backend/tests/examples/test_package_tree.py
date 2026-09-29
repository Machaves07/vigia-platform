"""Prueba de ejemplo: el paquete y el árbol de módulos de ``tech-stack-decisions.md`` §7."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import vigia_platform

PACKAGE = Path(vigia_platform.__file__).resolve().parent
MODULES = (
    "identity",
    "identity.domain",
    "identity.application",
    "identity.adapters",
    "identity.auth",
    "identity.authz",
    "ledger",
    "ledger.domain",
    "ledger.application",
    "ledger.adapters",
    "ledger.chain",
    "shared",
    "shared.api",
    "shared.outbox",
    "shared.signing",
    "shared.crypto",
    "shared.observability",
    "shared.clock",
)


def test_contract_and_platform_import_together() -> None:
    contracts = importlib.import_module("vigia_contracts")
    assert contracts.__name__ == "vigia_contracts"
    assert vigia_platform.__doc__ is not None
    assert (PACKAGE / "py.typed").is_file()


@pytest.mark.parametrize("module", MODULES)
def test_module_tree_exists_with_spanish_docstring(module: str) -> None:
    imported = importlib.import_module(f"vigia_platform.{module}")
    assert imported.__doc__, f"vigia_platform.{module} sin docstring"
    assert (PACKAGE / module.replace(".", "/") / "__init__.py").is_file()
