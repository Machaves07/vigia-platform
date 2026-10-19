"""Plantillas de las notificaciones (TASK-324; LC-LAZ-30; BR-LAZ-128; PR-LAZ-41; NFR-LAZ-52).

Una plantilla por ``template_key`` del catálogo, en este directorio, versionada con el código y en
español: ``<template_key>.es.j2`` con dos bloques, ``title`` y ``body``. Lo que la plantilla
recibe son **hechos** (``facts_for``): solo números y fechas sacados de la carga del evento, nunca
una cadena de la carga, un nombre, un correo ni un identificador. Por construcción, ningún aviso
lleva imagen, clip, recorte, dato de persona ni enlace (el enlace es ``deep_link_ref``, que exige
sesión y alcance). El texto sale en una sola línea (la política base de U-02 no admite saltos de
línea) y después pasa por los dos validadores de U-04 (``validated``): un término bloqueado en
una plantilla hace fallar el aviso, y ``tests/unit/test_notification_templates.py`` lo detecta
antes.

``templates_version()`` cambia con cualquier plantilla: el cuerpo de un correo caduca a los 90 días
y se recompone de la plantilla versionada y del evento (nota de domain-entities §6.2).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Final

import jinja2

from vigia_platform.ledger.free_text import FreeTextField, FreeTextPolicyRegistry

__all__ = [
    "BODY_FIELD",
    "FACT_NAMES",
    "TEMPLATES",
    "TEMPLATE_SUFFIX",
    "TITLE_FIELD",
    "RenderedNotification",
    "environment",
    "facts_for",
    "render",
    "render_template",
    "template_keys",
    "templates_version",
    "validated",
]

TEMPLATES: Final = Path(__file__).resolve().parent
TEMPLATE_SUFFIX: Final = ".es.j2"
TITLE_FIELD: Final = FreeTextField("notification", "/title_es", 1, 120)
"""``Notification.title_es`` (≤ 120, validado)."""
BODY_FIELD: Final = FreeTextField("notification", "/body_es", 1, 1000)
"""``Notification.body_es`` (≤ 1 000, validado)."""
BLOCKS: Final = ("title", "body")

_COUNTS: Final = (
    "days_overdue",
    "reminders_sent",
    "attachment_count",
    "silent_days",
    "commitment_count",
    "added_count",
    "removed_count",
    "rows_diverged",
)
"""Enteros de la carga que una plantilla puede citar (nunca negativos ni enormes)."""
_DATES: Final = {
    "due_at": "due_date",
    "review_due_at": "review_due_date",
    "expires_at": "expires_date",
}
"""Marcas de la carga que una plantilla cita como fecha UTC ``AAAA-MM-DD``."""
_COUNT_MAX: Final = 1_000_000
_FLAGS: Final = ("revoked", "manager_weekly")
FACT_NAMES: Final = frozenset({*_COUNTS, *_DATES.values(), *_FLAGS})
"""Las únicas variables que una plantilla puede usar."""


@dataclass(frozen=True, slots=True)
class RenderedNotification:
    title_es: str
    body_es: str


def template_keys() -> tuple[str, ...]:
    """Las plantillas del directorio, por su clave."""
    return tuple(
        sorted(path.name[: -len(TEMPLATE_SUFFIX)] for path in TEMPLATES.glob(f"*{TEMPLATE_SUFFIX}"))
    )


@cache
def templates_version() -> str:
    """SHA-256 de todas las plantillas en orden de clave (cambia con cualquiera de ellas)."""
    digest = hashlib.sha256()
    for key in template_keys():
        digest.update(key.encode())
        digest.update((TEMPLATES / f"{key}{TEMPLATE_SUFFIX}").read_bytes())
    return digest.hexdigest()


@cache
def _environment() -> jinja2.Environment:
    return _new_environment(TEMPLATES)


def _new_environment(directory: Path) -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(directory),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _count(value: object) -> int | None:
    if type(value) is not int or not 0 <= value <= _COUNT_MAX:
        return None
    return value


def _date(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(UTC).strftime("%Y-%m-%d")


def facts_for(event_name: str, payload: Mapping[str, object]) -> dict[str, object]:
    """Los hechos que la plantilla puede citar: números y fechas de la carga, y dos marcas.

    Un valor que falta o no tiene la forma esperada queda en ``None`` y la plantilla lo omite;
    ninguna cadena de la carga llega a la plantilla.
    """
    facts: dict[str, object] = {name: _count(payload.get(name)) for name in _COUNTS}
    for source, name in _DATES.items():
        facts[name] = _date(payload.get(source))
    facts[_FLAGS[0]] = event_name == "gate_state_changed" and payload.get("status") == "revoked"
    facts[_FLAGS[1]] = payload.get("escalation_level") == "manager_weekly"
    return facts


def _single_line(text: str) -> str:
    return " ".join(text.split())


def environment(directory: Path = TEMPLATES) -> jinja2.Environment:
    """El entorno de las plantillas de ``directory`` (el de producción es el de ``TEMPLATES``)."""
    if directory == TEMPLATES:
        return _environment()
    return _new_environment(directory)


def render(template_key: str, facts: Mapping[str, object]) -> RenderedNotification:
    """Título y cuerpo de la plantilla, en una línea cada uno; sin validar todavía."""
    return render_template(_environment().get_template(f"{template_key}{TEMPLATE_SUFFIX}"), facts)


def render_template(template: jinja2.Template, facts: Mapping[str, object]) -> RenderedNotification:
    """Título y cuerpo de una plantilla ya cargada, en una línea cada uno."""
    missing = [block for block in BLOCKS if block not in template.blocks]
    if missing:
        raise ValueError(f"la plantilla {template.name} no tiene los bloques {missing}")
    context = template.new_context(dict(facts))
    title, body = (_single_line("".join(template.blocks[block](context))) for block in BLOCKS)
    return RenderedNotification(title_es=title, body_es=body)


def validated(
    policy: FreeTextPolicyRegistry,
    rendered: RenderedNotification,
    organization_id: uuid.UUID,
) -> RenderedNotification:
    """Título y cuerpo pasados por la política base y los dos validadores de U-04 (BR-LAZ-128).

    ``FreeTextRejected`` si alguno no pasa: el aviso no se crea y la entrega reintenta (es un
    defecto de la plantilla, que la prueba de plantillas detecta antes).
    """
    return RenderedNotification(
        title_es=policy.apply(rendered.title_es, TITLE_FIELD, organization_id=organization_id),
        body_es=policy.apply(rendered.body_es, BODY_FIELD, organization_id=organization_id),
    )
