"""Catálogo fijo U-05 contra el validador real de U-04, sin duplicar lemas ni patrones."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vigia_platform.ledger.free_text import FreeTextCandidate, canonical_form
from vigia_platform.loop.validators.vocabulary import (
    BlockedVocabularyValidator,
    initial_vocabulary,
)

TEXTS = Path(__file__).resolve().parents[3] / "frontend" / "src" / "texts"
VALIDATOR = BlockedVocabularyValidator(lambda _organization: initial_vocabulary())


def violations(directory: Path) -> list[str]:
    """Descubre todos los dominios; un rechazo nombra archivo, clave y término plegado."""
    files = sorted(directory.glob("*.json"))
    assert files, f"{directory}: catálogo sin archivos JSON"
    errors = []
    for path in files:
        content = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(content, dict), f"{path.name}: se esperaba un objeto plano"
        for key, text in content.items():
            assert isinstance(text, str) and text.strip(), f"{path.name}: {key} no es texto"
            verdict = VALIDATOR.verdict(FreeTextCandidate(text, canonical_form(text)))
            if verdict is not None:
                errors.append(f"{path.name}: {key}: término bloqueado «{verdict.matched_value}»")
    return errors


def test_all_u05_catalog_texts_pass_real_blocked_vocabulary() -> None:
    assert violations(TEXTS) == []


@pytest.mark.parametrize(
    "text",
    [
        "Observable no significa zona despejada",
        "Observable no equivale a zona despejada",
        "Observable no significa zona segura",
        "Observable no equivale a zona segura",
        "OBSERVABLE NO SIGNIFICA ZONA DESPEJADA",
    ],
)
def test_d9_negated_legend_passes(text: str) -> None:
    assert VALIDATOR.verdict(FreeTextCandidate(text, canonical_form(text))) is None


@pytest.mark.parametrize(
    ("text", "term"),
    [
        ("zona despejada", "despejada"),
        ("la zona está despejada", "despejada"),
        ("área despejada", "despejada"),
        ("la zona está segura", "zona esta segura"),
        ("Observable no significa zona despejada; la zona está despejada", "despejada"),
        ("la zona está d\u0435spejada", "despejada"),  # e cirílica: plegado UTS #39 real
        ("\uff19\uff15\u00a0% de detección", "95 % de deteccion"),
    ],
)
def test_new_domain_fails_with_key_and_term(tmp_path: Path, text: str, term: str) -> None:
    path = tmp_path / "scr99.json"
    path.write_text(json.dumps({"scr99.probe": text}), encoding="utf-8")
    assert violations(tmp_path) == [f"scr99.json: scr99.probe: término bloqueado «{term}»"]
