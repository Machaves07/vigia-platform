"""La forma HTTP de los valores de proceso de un registro (TASK-595; A-129).

``ProcessContextOut`` sale igual en el detalle del hallazgo (``RecordOut.process_context``), en la
foto del episodio de SCR-06 (``EpisodePhotoOut.process_context``) y, resumido sin la serie
(``ProcessVariableSummaryOut``), en el hito de detección de la ficha de vida. Los números y los
desplazamientos van tal como los guardó la ingesta; los campos resueltos del catálogo (``code``,
``description_es``, ``unit``, ``decimals`` y ``states``) son ``null`` si la variable no tiene
declaración en la versión del registro («variable sin declaración»). ``unit`` y ``states`` son
códigos cerrados del contrato: la consola los muestra con ``process_unit`` y
``process_state_pair`` (``<par>.asserted`` o ``<par>.deasserted``) de ``labels.platform.es.json``.
La única hora absoluta es ``at_event.read_at``, presentada como las demás marcas de la lectura
(agregada en zona unipersonal, BR-LAZ-06); muestras y cambios solo llevan su desplazamiento.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from vigia_platform.fleet.domain.record_declarations import (
    ProcessContextView,
    ProcessVariableSummary,
)
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "ProcessAtEventOut",
    "ProcessChangeOut",
    "ProcessContextOut",
    "ProcessSampleOut",
    "ProcessVariableOut",
    "ProcessVariableSummaryOut",
    "process_context_out",
    "process_summary_out",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _stamp(moment: datetime | None) -> str | None:
    return None if moment is None else format_timestamp(moment.astimezone(UTC))


class ProcessAtEventOut(_Strict):
    value: float | str | None
    """Analógica: el número en unidades de ingeniería (``null`` si la lectura no fue válida); de
    estado: ``asserted``, ``deasserted`` o ``unknown``."""
    valid: bool
    offset_ms: int
    """Respecto del inicio del tramo (``node_time.started_at``)."""
    read_at: str | None
    """El instante del hecho: exacto, o el inicio de la ventana de agregación si la zona es
    unipersonal."""


class ProcessSampleOut(_Strict):
    offset_ms: int
    value: float | None


class ProcessChangeOut(_Strict):
    offset_ms: int
    value: str


class ProcessVariableOut(_Strict):
    variable_id: uuid.UUID
    kind: str
    """``analog`` o ``binary``."""
    code: str | None
    description_es: str | None
    """El nombre para quien clasifica; ``null`` si la variable no tiene declaración."""
    unit: str | None
    decimals: int | None
    states: str | None
    at_event: ProcessAtEventOut
    min: float | None
    max: float | None
    last: float | None
    samples: tuple[ProcessSampleOut, ...]
    changes: tuple[ProcessChangeOut, ...]
    truncated: bool | None


class ProcessContextOut(_Strict):
    """Los valores de proceso del tramo (A-129), de ``from_offset_ms`` a ``to_offset_ms``."""

    from_offset_ms: int
    to_offset_ms: int
    variables: tuple[ProcessVariableOut, ...]


class ProcessVariableSummaryOut(_Strict):
    """Una variable en el hito de detección de la ficha de vida: sin la serie."""

    variable_id: uuid.UUID
    kind: str
    code: str | None
    description_es: str | None
    unit: str | None
    decimals: int | None
    states: str | None
    value: float | str | None
    valid: bool
    read_at: str | None


def process_context_out(view: ProcessContextView | None) -> ProcessContextOut | None:
    if view is None:
        return None
    return ProcessContextOut(
        from_offset_ms=view.from_offset_ms,
        to_offset_ms=view.to_offset_ms,
        variables=tuple(
            ProcessVariableOut(
                variable_id=item.variable_id,
                kind=item.kind,
                code=item.code,
                description_es=item.description_es,
                unit=item.unit,
                decimals=item.decimals,
                states=item.states,
                at_event=ProcessAtEventOut(
                    value=item.at_event.value,
                    valid=item.at_event.valid,
                    offset_ms=item.at_event.offset_ms,
                    read_at=_stamp(item.at_event.read_at),
                ),
                min=item.min,
                max=item.max,
                last=item.last,
                samples=tuple(
                    ProcessSampleOut(offset_ms=s.offset_ms, value=s.value) for s in item.samples
                ),
                changes=tuple(
                    ProcessChangeOut(offset_ms=c.offset_ms, value=c.value) for c in item.changes
                ),
                truncated=item.truncated,
            )
            for item in view.variables
        ),
    )


def process_summary_out(
    summary: tuple[ProcessVariableSummary, ...],
) -> tuple[ProcessVariableSummaryOut, ...]:
    return tuple(
        ProcessVariableSummaryOut(
            variable_id=item.variable_id,
            kind=item.kind,
            code=item.code,
            description_es=item.description_es,
            unit=item.unit,
            decimals=item.decimals,
            states=item.states,
            value=item.value,
            valid=item.valid,
            read_at=_stamp(item.read_at),
        )
        for item in summary
    )
