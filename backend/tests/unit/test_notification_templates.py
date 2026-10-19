"""Las plantillas de las notificaciones (TASK-324; LC-LAZ-30; BR-LAZ-128; PR-LAZ-41; NFR-LAZ-52).

Cada plantilla de ``notifications/templates`` se compone con **todas** las combinaciones de sus
hechos (cada número presente o ausente, cada marca verdadera o falsa) y cada título y cada cuerpo:

- pasan la política base de U-02 y los **dos validadores reales** de U-04 (``blocked_vocabulary``
  con la lista inicial y ``person_attribution``): **un término bloqueado en una plantilla hace
  fallar esta prueba** (``test_a_blocked_term_in_a_template_is_caught`` lo demuestra con una copia
  de una plantilla a la que se le añade uno);
- caben en 120 y 1 000 caracteres, en una sola línea y no vacíos;
- no contienen enlace, dirección, imagen ni referencia a clip, recorte o fotograma (por palabras
  de su forma canónica, no por subcadena, VIG-194);
- solo usan las variables de ``FACT_NAMES`` (números, fechas y dos marcas): ninguna cadena de la
  carga llega a una plantilla.
"""

from __future__ import annotations

import itertools
import re
import shutil
import uuid
from pathlib import Path
from typing import Final

import jinja2
import pytest
from jinja2 import meta

from tests.attribution_support import both_policy
from vigia_platform.ledger.free_text import FreeTextRejected, canonical_form
from vigia_platform.notifications.templates import (
    BODY_FIELD,
    FACT_NAMES,
    TEMPLATE_SUFFIX,
    TEMPLATES,
    TITLE_FIELD,
    RenderedNotification,
    environment,
    render,
    render_template,
    template_keys,
    templates_version,
    validated,
)

ORGANIZATION: Final = uuid.UUID("00000000-0000-4000-8000-000000000324")
FORBIDDEN_WORDS: Final = frozenset(
    {
        "clip",
        "clips",
        "video",
        "imagen",
        "imagenes",
        "foto",
        "fotos",
        "fotografia",
        "recorte",
        "recortes",
        "fotograma",
        "fotogramas",
        "rostro",
        "cara",
        "http",
        "https",
        "www",
        "img",
        "src",
        "href",
    }
)
"""Palabras que un aviso no puede llevar (BR-LAZ-128): imagen, clip, enlace o persona."""
_WORDS: Final = re.compile(r"[a-z0-9]+")
SAMPLE_COUNT: Final = 7
SAMPLE_DATE: Final = "2026-10-01"


def _variables(source: str) -> set[str]:
    return set(meta.find_undeclared_variables(environment().parse(source)))


def _source(key: str, directory: Path = TEMPLATES) -> str:
    return (directory / f"{key}{TEMPLATE_SUFFIX}").read_text(encoding="utf-8")


def _fact_combinations(variables: set[str]) -> list[dict[str, object]]:
    """Cada variable usada presente o ausente (las marcas, verdaderas o falsas)."""
    names = sorted(variables)
    combinations: list[dict[str, object]] = []
    for present in itertools.product((True, False), repeat=len(names)):
        facts: dict[str, object] = dict.fromkeys(FACT_NAMES)
        for name, on in zip(names, present, strict=True):
            if name in {"revoked", "manager_weekly"}:
                facts[name] = on
            elif name.endswith("_date"):
                facts[name] = SAMPLE_DATE if on else None
            else:
                facts[name] = SAMPLE_COUNT if on else None
        combinations.append(facts)
    return combinations


def _problems(rendered: RenderedNotification) -> list[str]:
    problems: list[str] = []
    policy = both_policy()
    for text, field in ((rendered.title_es, TITLE_FIELD), (rendered.body_es, BODY_FIELD)):
        if not text or text != " ".join(text.split()):
            problems.append(f"{field.path}: vacío o con saltos y espacios de más")
        if len(text) > field.max_length:
            problems.append(f"{field.path}: {len(text)} > {field.max_length}")
        words = set(_WORDS.findall(canonical_form(text)))
        if words & FORBIDDEN_WORDS:
            problems.append(f"{field.path}: {sorted(words & FORBIDDEN_WORDS)}")
    try:
        validated(policy, rendered, ORGANIZATION)
    except FreeTextRejected as error:
        problems.append(f"validador: {error.reason} ({error.detail_code})")
    return problems


def _template_problems(key: str, env: jinja2.Environment) -> list[str]:
    source = _source(key, Path(str(env.loader.searchpath[0])))  # type: ignore[union-attr]
    unknown = _variables(source) - FACT_NAMES
    if unknown:
        return [f"{key}: variables fuera de FACT_NAMES {sorted(unknown)}"]
    template = env.get_template(f"{key}{TEMPLATE_SUFFIX}")
    return [
        f"{key}: {problem}"
        for facts in _fact_combinations(_variables(source))
        for problem in _problems(render_template(template, facts))
    ]


@pytest.mark.parametrize("key", template_keys())
def test_every_template_renders_valid_text_with_every_combination_of_facts(key: str) -> None:
    assert _template_problems(key, environment()) == []


def test_there_are_templates_and_the_version_covers_them() -> None:
    keys = template_keys()
    assert len(keys) == 44
    assert re.fullmatch(r"[0-9a-f]{64}", templates_version())


def test_render_produces_one_line_with_the_facts() -> None:
    rendered = render(
        "finding_overdue",
        {**dict.fromkeys(FACT_NAMES), "due_date": SAMPLE_DATE, "days_overdue": 3},
    )
    assert rendered.title_es == "Acción correctiva vencida"
    assert f"del {SAMPLE_DATE} (UTC) hace 3 días." in rendered.body_es
    assert "\n" not in rendered.body_es


def test_a_template_without_its_blocks_is_rejected(tmp_path: Path) -> None:
    (tmp_path / f"broken{TEMPLATE_SUFFIX}").write_text("{% block title %}Aviso{% endblock %}")
    template = environment(tmp_path).get_template(f"broken{TEMPLATE_SUFFIX}")
    with pytest.raises(ValueError, match="bloques"):
        render_template(template, dict.fromkeys(FACT_NAMES))


@pytest.mark.parametrize(
    ("addition", "expected"),
    [
        (" La zona quedó despejada.", "validador: blocked_vocabulary_term"),
        (" Garantizamos la detección.", "validador: blocked_vocabulary_term"),
        (" Vea el clip del episodio.", "['clip']"),
        (" Abra https://vigia.example/x.", "['https']"),
    ],
)
def test_a_blocked_term_in_a_template_is_caught(
    tmp_path: Path, addition: str, expected: str
) -> None:
    """La comprobación de la prueba principal detecta una plantilla con un término prohibido."""
    for key in template_keys():
        shutil.copy(TEMPLATES / f"{key}{TEMPLATE_SUFFIX}", tmp_path)
    target = tmp_path / f"finding_closed{TEMPLATE_SUFFIX}"
    source = target.read_text(encoding="utf-8")
    assert " Abra Vigía" in source
    target.write_text(source.replace(" Abra Vigía", f"{addition} Abra Vigía"), encoding="utf-8")
    env = environment(tmp_path)
    problems = _template_problems("finding_closed", env)
    assert problems and all(expected in problem for problem in problems), problems
    assert _template_problems("finding_received", env) == []


def test_a_template_cannot_read_a_payload_string(tmp_path: Path) -> None:
    """Una plantilla que intentara citar una cadena de la carga (un nombre) no pasa."""
    (tmp_path / f"leaky{TEMPLATE_SUFFIX}").write_text(
        "{% block title %}Aviso{% endblock %}{% block body %}De {{ display_name }}.{% endblock %}"
    )
    assert _template_problems("leaky", environment(tmp_path)) == [
        "leaky: variables fuera de FACT_NAMES ['display_name']"
    ]
