"""La salida estricta del asistente (tech-stack-decisions §2.3; BR-LAZ-70 y 71; TASK-316).

``AssistantOutput`` es el esquema que el proveedor debe cumplir (salida estructurada con
``additionalProperties: false``): ``draft_text``, ``citations`` de ``{kind, id,
quoted_fact_es}``, ``insufficient_evidence_declared`` y ``proposed_cause_category`` (una de las
tres categorías, ninguna es una persona). ``output_json_schema()`` es el esquema que el adaptador
real entrega al proveedor (TASK-344); ``parse_output`` valida el texto JSON de la respuesta **sin
coerción** y devuelve ``None`` ante cualquier forma inválida: campos de más o de menos, tipos
distintos, ``NaN``, anidamiento profundo, cadenas gigantes. Nunca lanza.

Las citas de U-04 **no son** la función de citas del proveedor (incompatible con las salidas
estructuradas, §2.3): son campos de este esquema, y su ``id`` es un **alias de sesión** de la
lista cerrada del contexto (``loop.assistant.context``), que el consumidor comprueba después.
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, ValidationError

from vigia_platform.loop.domain.enums import CauseCategory, EvidenceRefKind

__all__ = [
    "ALIAS_PATTERN",
    "MAX_CITATIONS",
    "MAX_DRAFT_CHARS",
    "MAX_OUTPUT_BYTES",
    "MAX_QUOTED_FACT_CHARS",
    "AssistantCitation",
    "AssistantOutput",
    "output_json_schema",
    "parse_output",
]

MAX_DRAFT_CHARS: Final = 8_000
"""``draft_text`` ≤ 8 000 caracteres (DE §2.11): lo limita el esquema, no ``max_tokens``."""
MAX_CITATIONS: Final = 100
MAX_QUOTED_FACT_CHARS: Final = 500
MAX_OUTPUT_BYTES: Final = 256 * 1024
"""Tope del texto JSON de la respuesta antes de analizarlo `[objetivo propio]`: el borrador y
cien citas caben con holgura; lo que pase de aquí no se analiza."""
ALIAS_PATTERN: Final = r"^[A-Z]-[0-9a-f]{8}$"
"""Forma de un alias de sesión (``loop.assistant.context``): letra de la clase y 8 hexadecimales."""


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class AssistantCitation(_Strict):
    """Una cita: el alias de un hecho de la zona y el hecho citado, en español."""

    kind: EvidenceRefKind
    id: Annotated[StrictStr, Field(pattern=ALIAS_PATTERN)]
    quoted_fact_es: Annotated[StrictStr, Field(min_length=1, max_length=MAX_QUOTED_FACT_CHARS)]


class AssistantOutput(_Strict):
    """La respuesta del proveedor, tal como el esquema estricto la admite."""

    draft_text: Annotated[StrictStr, Field(min_length=1, max_length=MAX_DRAFT_CHARS)]
    citations: Annotated[tuple[AssistantCitation, ...], Field(max_length=MAX_CITATIONS)]
    insufficient_evidence_declared: StrictBool
    proposed_cause_category: CauseCategory


def output_json_schema() -> dict[str, Any]:
    """El esquema JSON de ``AssistantOutput`` (``additionalProperties: false`` en cada objeto)."""
    return AssistantOutput.model_json_schema()


def parse_output(content: object) -> AssistantOutput | None:
    """``AssistantOutput`` si ``content`` (texto JSON) cumple el esquema; si no, ``None``."""
    if not isinstance(content, str) or len(content) > MAX_OUTPUT_BYTES:
        return None
    try:
        if len(content.encode("utf-8")) > MAX_OUTPUT_BYTES:
            return None
        return AssistantOutput.model_validate_json(content, strict=True)
    except (ValidationError, ValueError, RecursionError):
        return None
