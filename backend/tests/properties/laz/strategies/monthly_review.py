"""Generadores de la revisión mensual (TASK-347; PR-LAZ-31 y 32; BLM §12).

- ``audit_commands``: secuencias de operaciones de **auditoría** sobre los elementos de una
  muestra: resultados válidos (con o sin comentario, también con un término bloqueado), resultados
  repetidos sobre el mismo elemento e **intentos de modificar** la clasificación auditada (una
  salida de clasificación como resultado, o un campo de la clasificación en el cuerpo, con
  variantes de mayúsculas, guiones y subrayados).
- ``line_manager_commands``: secuencias de operaciones del **mando de línea**: alta de
  compromisos en su zona, en otra zona, en otra organización o sobre un paquete inexistente (con
  textos válidos, cortos o bloqueados, y responsables con o sin alcance), actualizaciones de
  compromisos propios y ajenos, intentos sobre cada ruta del lazo y lecturas.

Solo datos generados; la generación no depende de la base ni del reloj (retro 14).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from hypothesis import strategies as st

from vigia_platform.accreditation.monthly.audit import CLASSIFICATION_FIELD_NAMES
from vigia_platform.loop.domain.enums import (
    ClassificationOutcome,
    CommitmentState,
    SampleAuditOutcome,
)

__all__ = [
    "AUDIT_ITEMS",
    "LOOP_ATTEMPTS",
    "AuditCommand",
    "LineManagerCommand",
    "audit_commands",
    "line_manager_commands",
]

AUDIT_ITEMS: Final = 3
"""Elementos de la muestra de cada ejemplo."""

LOOP_ATTEMPTS: Final = (
    "classify",
    "resolve_review",
    "assign_action",
    "add_evidence",
    "close",
    "reopen",
    "start_root_cause",
    "sample_result",
)
"""Las escrituras del lazo (y la de la muestra) que el mando de línea intenta sin su clave."""


@dataclass(frozen=True)
class AuditCommand:
    kind: str
    """``result`` (un resultado de ``sample_audit_outcome``), ``modify_outcome`` (una salida de
    clasificación como resultado) o ``modify_field`` (un campo de la clasificación de más)."""
    item: int
    outcome: str
    comment: str
    """``none``, ``valid``, ``blank`` o ``blocked``."""
    field: str | None = None


def _variant(name: str, style: int) -> str:
    """Mayúsculas, ``camelCase`` o guiones: el servicio los reconoce igual."""
    if style == 1:
        return name.upper()
    if style == 2:
        head, *rest = name.split("_")
        return head + "".join(part.capitalize() for part in rest)
    if style == 3:
        return name.replace("_", "-")
    return name


@st.composite
def _audit_command(draw: st.DrawFn) -> AuditCommand:
    kind = draw(st.sampled_from(("result", "result", "modify_outcome", "modify_field")))
    item = draw(st.integers(0, AUDIT_ITEMS - 1))
    style = draw(st.integers(0, 3))
    if kind == "modify_outcome":
        outcome = _variant(draw(st.sampled_from([o.value for o in ClassificationOutcome])), style)
        return AuditCommand(kind, item, outcome, "none")
    outcome = draw(st.sampled_from([o.value for o in SampleAuditOutcome]))
    comment = draw(st.sampled_from(("none", "valid", "blank", "blocked")))
    if kind == "modify_field":
        name = draw(st.sampled_from(sorted(CLASSIFICATION_FIELD_NAMES)))
        return AuditCommand(kind, item, outcome, comment, _variant(name, style))
    return AuditCommand(kind, item, outcome, comment)


def audit_commands(max_size: int = 8) -> st.SearchStrategy[list[AuditCommand]]:
    """Una secuencia de operaciones de auditoría sobre los ``AUDIT_ITEMS`` de una muestra."""
    return st.lists(_audit_command(), min_size=1, max_size=max_size)


@dataclass(frozen=True)
class LineManagerCommand:
    kind: str
    """``record``, ``update``, ``loop`` o ``read``."""
    target: str = "own"
    """``record``: ``own``, ``other_zone``, ``other_organization`` o ``missing``; ``update``: lo
    mismo sobre un compromiso; ``loop``: una de ``LOOP_ATTEMPTS``; ``read``: ``commitments`` o
    ``sample``."""
    count: int = 1
    text: str = "valid"
    """``valid``, ``short`` o ``blocked``."""
    responsible: str = "in_scope"
    """``in_scope`` o ``out_of_scope``."""
    state: str = "done"
    note: bool = False


_TARGETS: Final = ("own", "own", "other_zone", "other_organization", "missing")


@st.composite
def _line_manager_command(draw: st.DrawFn) -> LineManagerCommand:
    kind = draw(st.sampled_from(("record", "record", "update", "update", "loop", "read")))
    if kind == "record":
        return LineManagerCommand(
            kind,
            target=draw(st.sampled_from(_TARGETS)),
            count=draw(st.integers(1, 3)),
            text=draw(st.sampled_from(("valid", "valid", "short", "blocked"))),
            responsible=draw(st.sampled_from(("in_scope", "in_scope", "out_of_scope"))),
        )
    if kind == "update":
        return LineManagerCommand(
            kind,
            target=draw(st.sampled_from(_TARGETS)),
            state=draw(st.sampled_from([state.value for state in CommitmentState])),
            note=draw(st.booleans()),
        )
    if kind == "loop":
        return LineManagerCommand(kind, target=draw(st.sampled_from(LOOP_ATTEMPTS)))
    return LineManagerCommand(kind, target=draw(st.sampled_from(("commitments", "sample"))))


def line_manager_commands(max_size: int = 8) -> st.SearchStrategy[list[LineManagerCommand]]:
    """Una secuencia de operaciones del mando de línea (BR-LAZ-109)."""
    return st.lists(_line_manager_command(), min_size=1, max_size=max_size)
