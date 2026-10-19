"""PR-LAZ-19 · metapropiedad sobre el objeto que cruza la frontera hacia el proveedor (TASK-316;
BR-LAZ-67 a 69; NFR-LAZ-30; PAT-LAZ-SEG-08).

Para cualquier contexto objetivo generado (``assistant_contexts``: identificadores reales al azar
y **textos de usuario colados** en claves de más, en el título del estándar, en señales y en
acciones previas), el objeto serializado que sale (``canonical_json`` de ``build_context``):

- tiene **solo** rutas de clave de la lista cerrada de BR-LAZ-67, escrita aquí a mano (``ALLOWED``,
  independiente de la del código);
- no contiene **ningún identificador** de organización, planta, zona, nodo, estándar, registro de
  anclaje ni de la solicitud (ni con guiones ni en hexadecimal), **ningún texto de usuario** y,
  del estándar, solo su texto declarado (nunca su título);
- cita solo **alias de sesión** (``<clase>-<8 hex>``), únicos, que dependen de la solicitud: dos
  solicitudes sobre el mismo contexto no comparten ninguno, y la misma solicitud da el mismo objeto
  y el mismo ``context_digest`` (lo que la API resume y el consumidor comprueba).

Mutación (Evidence del PR): que el constructor deje pasar un campo más (``zone_id`` o el título del
estándar) o copie una clave desconocida pone esta propiedad en rojo.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator
from typing import Any, Final

from hypothesis import given
from hypothesis import strategies as st

from tests.properties.laz.strategies.assistant import GeneratedContext, assistant_contexts
from vigia_platform.loop.assistant.context import (
    STANDARD_NOTICE_ES,
    build_context,
    canonical_json,
    context_digest,
)

ALLOWED: Final = frozenset(
    {
        "finding",
        "finding.alias",
        "finding.family",
        "finding.tier",
        "finding.observability_state_at_event",
        "finding.signals_by_role",
        "finding.signals_by_role[].role",
        "finding.signals_by_role[].value",
        "finding.signals_by_role[].valid",
        "finding.durations",
        "finding.durations.episode_ms",
        "finding.durations.condition_ms",
        "standard",
        "standard.notice_es",
        "standard.declared_text",
        "coverage",
        "coverage.alias",
        "coverage.period_days",
        "coverage.observable_ms",
        "coverage.degraded_ms",
        "coverage.not_observable_ms",
        "coverage.no_communication_ms",
        "coverage.never_reported_ms",
        "coverage.zone_not_active_ms",
        "nodes",
        "nodes[].alias",
        "nodes[].communication_state",
        "history",
        "history.finding_counts_by_family",
        "history.finding_counts_by_family[].alias",
        "history.finding_counts_by_family[].family",
        "history.finding_counts_by_family[].count",
        "history.recurrences_in_period",
        "history.recurrences_in_period.alias",
        "history.recurrences_in_period.count",
        "history.previous_actions",
        "history.previous_actions[].alias",
        "history.previous_actions[].action_type",
        "history.previous_actions[].outcome",
        "history.previous_actions[].recurrence_after_days",
        "history.previous_cause_categories",
        "history.previous_cause_categories[].category",
        "history.previous_cause_categories[].count",
        "citable",
        "citable[].alias",
        "citable[].kind",
    }
)
"""BR-LAZ-67, a mano: familia y tier, texto del estándar como dato, señales por rol, duraciones,
observabilidad y cobertura, historial estructurado y la lista cerrada de alias."""

ALIAS: Final = re.compile(r"[A-Z]-[0-9a-f]{8}")


def _paths(value: Any, prefix: str = "") -> Iterator[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else key
            yield path
            yield from _paths(child, path)
    elif isinstance(value, list):
        for child in value:
            yield from _paths(child, f"{prefix}[]")


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)
    elif isinstance(value, str):
        yield value


def _build(generated: GeneratedContext, request_id: uuid.UUID | None = None) -> Any:
    return build_context(
        generated.zone_context,
        request_id=request_id or generated.request_id,
        anchor_record_id=generated.anchor_record_id,
    )


@given(generated=assistant_contexts())
def test_pr_laz_19_only_the_closed_list_crosses_the_boundary(generated: GeneratedContext) -> None:
    built = _build(generated)
    serialized = canonical_json(built.context)
    document = json.loads(serialized)
    assert set(_paths(document)) <= ALLOWED, set(_paths(document)) - ALLOWED
    lowered = serialized.lower()
    for identifier in generated.identifiers:
        assert str(identifier) not in lowered
        assert identifier.hex not in lowered
    for text in generated.user_texts:
        assert text not in serialized
        assert text.rsplit(" ", 1)[-1] not in serialized  # ni su marca suelta
    # Del estándar sale solo su texto declarado, delimitado en su campo y declarado como dato.
    assert document["standard"] == {
        "notice_es": STANDARD_NOTICE_ES,
        "declared_text": generated.standard_text,
    }
    assert generated.zone_context["standard_title_es"] not in serialized
    # Lo único de texto que sale son valores de lista cerrada, alias, el aviso y el estándar.
    for value in _strings(document):
        assert (
            value in (STANDARD_NOTICE_ES, generated.standard_text)
            or ALIAS.fullmatch(value)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value)
        ), value


@given(generated=assistant_contexts(), other=st.uuids(version=4))
def test_pr_laz_19_aliases_are_per_session_and_the_object_is_deterministic(
    generated: GeneratedContext, other: uuid.UUID
) -> None:
    built = _build(generated)
    again = _build(generated)
    assert canonical_json(again.context) == canonical_json(built.context)
    assert context_digest(again.context) == built.digest
    assert re.fullmatch(r"[0-9a-f]{64}", built.digest)
    citable = [item.alias for item in built.context.citable]
    assert len(citable) == len(set(citable)) == len(built.aliases)
    assert set(citable) == set(built.aliases)
    assert all(ALIAS.fullmatch(alias) for alias in citable)
    if other != generated.request_id:
        elsewhere = _build(generated, other)
        assert not set(elsewhere.aliases) & set(built.aliases)
    # El alias del hecho observado nombra el registro de anclaje, del lado que no cruza.
    anchor = built.aliases[built.context.finding.alias]
    assert anchor.reference_id == generated.anchor_record_id
