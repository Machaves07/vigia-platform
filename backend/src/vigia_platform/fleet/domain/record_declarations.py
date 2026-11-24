"""Nombre de cada señal y de cada variable de proceso de un registro, con la versión del catálogo
con la que se aceptó (TASK-595; A-129; O-2 y O-14; BR-LAZ-06).

El registro (hallazgo, detección o foto del episodio) solo lleva identificadores, números y
desplazamientos (BR-CTR-13): el nombre, la unidad, los decimales y el par de estados viven en el
catálogo firmado. Para leerlos se busca **la misma versión que el paso 7 de la ingesta**
(``ingest_order.accepted_version`` y ``episode_photo.accepted_version``): la más nueva vigente en la
ventana del hecho que cita el estándar del registro y concuerda con él. Nunca la versión vigente
hoy: un renombre posterior no cambia lo que se muestra de un registro viejo.

- ``DeclaredCatalogVersion.from_payload``: la vista del paso 7 (``CatalogVersionView``) y, por
  identificador, ``code`` y ``description_es`` de cada señal y ``code``, ``description_es``,
  ``kind``, ``unit``, ``decimals`` y ``states`` de cada variable de proceso.
- ``record_version`` (hallazgo o detección, con la ventana de ``node_time`` como la ingesta) y
  ``photo_version`` (la foto del episodio, que no guarda el reloj del nodo: ventana del tramo con
  la tolerancia máxima y, si nada concuerda, el instante de recepción).
- ``process_context_view``: el bloque **tal como se guardó** (sin recalcular ni redondear) con lo
  resuelto; ``at_event.read_at`` es ``node_time.started_at + offset_ms`` **presentado** por
  ``shown`` (agregado en zona unipersonal, BR-LAZ-06). Las muestras y los cambios solo llevan su
  desplazamiento relativo: ninguna hora absoluta.
- ``process_summary``: el resumen de la ficha de vida (sin la serie).

Si no se encuentra la declaración (ninguna versión concuerda, o la variable no está en la
elegida) los campos resueltos van ``None`` («variable sin declaración»): la lectura nunca falla por
eso. Dominio puro: sin base ni reloj.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Final

from vigia_platform.fleet.domain import episode_photo
from vigia_platform.fleet.domain.clock_tolerance import MAX_CLOCK_TOLERANCE, FactTime, Window
from vigia_platform.fleet.domain.ingest_order import (
    CatalogUnreadable,
    CatalogVersionView,
    IngestKind,
    accepted_version,
)

__all__ = [
    "DeclaredCatalogVersion",
    "ProcessAtEventView",
    "ProcessChangeView",
    "ProcessContextView",
    "ProcessSampleView",
    "ProcessVariableSummary",
    "ProcessVariableView",
    "SignalName",
    "VariableDeclaration",
    "catalog_window",
    "photo_version",
    "process_context_view",
    "process_summary",
    "record_version",
    "started_at_of",
]

_KINDS: Final[Mapping[str, IngestKind]] = {
    kind.record_type: kind for kind in IngestKind if kind.gated
}
"""Los registros del expediente que citan una versión del catálogo (hallazgo y detección)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalName:
    code: str
    description_es: str


@dataclass(frozen=True, slots=True, kw_only=True)
class VariableDeclaration:
    """Lo que el catálogo declara de una variable de proceso (``ProcessVariableDeclaration``)."""

    code: str
    description_es: str
    kind: str
    unit: str | None
    """Analógica: la unidad (``ProcessUnit``); de estado, ``None``."""
    decimals: int | None
    states: str | None
    """De estado: el par de etiquetas (``ProcessStatePair``); la consola muestra
    ``<par>.asserted`` o ``<par>.deasserted`` de ``process_state_pair``."""


@dataclass(frozen=True, slots=True)
class DeclaredCatalogVersion:
    """Una versión guardada del catálogo de la zona con los nombres que la lectura necesita."""

    view: CatalogVersionView
    signals: Mapping[uuid.UUID, SignalName]
    variables: Mapping[uuid.UUID, VariableDeclaration]

    @classmethod
    def from_payload(
        cls,
        version: int,
        effective_from: datetime,
        effective_until: datetime | None,
        payload: Mapping[str, Any],
    ) -> DeclaredCatalogVersion:
        """La versión con sus nombres (``CatalogUnreadable`` si no tiene la forma de
        ``ZoneCatalog``)."""
        view = CatalogVersionView.from_payload(version, effective_from, effective_until, payload)
        try:
            signals = MappingProxyType(
                {
                    uuid.UUID(str(item["signal_id"])): SignalName(
                        code=str(item["code"]), description_es=str(item["description_es"])
                    )
                    for item in payload["signals"]
                }
            )
            declared = (payload.get("process_context") or {}).get("variables", ())
            variables = MappingProxyType(
                {
                    uuid.UUID(str(item["variable_id"])): VariableDeclaration(
                        code=str(item["code"]),
                        description_es=str(item["description_es"]),
                        kind=str(item["kind"]),
                        unit=_optional_text(item.get("unit")),
                        decimals=_optional_integer(item.get("decimals")),
                        states=_optional_text(item.get("states")),
                    )
                    for item in declared
                }
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            raise CatalogUnreadable("versión del catálogo ilegible") from None
        return cls(view, signals, variables)


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_integer(value: Any) -> int | None:
    return None if value is None or isinstance(value, bool) else int(value)


def _instant(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.utcoffset() is not None else None
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.utcoffset() is not None else None


def _fact(document: Mapping[str, Any], received_at: datetime) -> FactTime | None:
    """El ``FactTime`` del registro como en la ingesta, o ``None`` si no se puede leer."""
    node_time = document.get("node_time")
    if not isinstance(node_time, Mapping):
        return None
    clock = node_time.get("clock")
    if not isinstance(clock, Mapping):
        return None
    started, ended = _instant(node_time.get("started_at")), _instant(node_time.get("ended_at"))
    synchronized, offset = clock.get("synchronized"), clock.get("offset_ms")
    if (
        started is None
        or ended is None
        or type(synchronized) is not bool
        or type(offset) is not int
    ):
        return None
    try:
        return FactTime(
            started_at=started,
            ended_at=ended,
            synchronized=synchronized,
            offset_ms=offset,
            received_at=received_at,
        )
    except ValueError:
        return None


def catalog_window(document: Mapping[str, Any], received_at: datetime) -> Window | None:
    """La ventana del paso 7 de un hallazgo o una detección (``FactTime.catalog_window``)."""
    fact = _fact(document, received_at)
    return None if fact is None else fact.catalog_window


def _by_version(
    versions: Sequence[DeclaredCatalogVersion], found: CatalogVersionView | None
) -> DeclaredCatalogVersion | None:
    if found is None:
        return None
    return next((item for item in versions if item.view is found), None)


def record_version(
    versions: Sequence[DeclaredCatalogVersion],
    document: Mapping[str, Any],
    *,
    record_type: str,
    received_at: datetime,
) -> DeclaredCatalogVersion | None:
    """La versión con la que se aceptó un hallazgo o una detección, o ``None``."""
    kind = _KINDS.get(record_type)
    window = catalog_window(document, received_at)
    if kind is None or window is None:
        return None
    try:
        found = accepted_version([item.view for item in versions], document, kind, window)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None  # un documento que no tiene la forma del contrato: sin declaración
    return _by_version(versions, found)


def photo_version(
    versions: Sequence[DeclaredCatalogVersion],
    document: Mapping[str, Any],
    *,
    started_at: datetime,
    ended_at: datetime,
    received_at: datetime,
) -> DeclaredCatalogVersion | None:
    """La versión con la que se aceptó la foto del episodio, o ``None``.

    ``document`` lleva ``standard``, ``camera_id`` y ``process_context`` de la fila. La fila no
    guarda el reloj del nodo: se prueba la ventana del tramo con la tolerancia máxima (la de un
    nodo sincronizado) y, si nada concuerda, el instante de recepción (la de uno que no lo está).
    """
    views = [item.view for item in versions]
    for window in (
        Window(started_at - MAX_CLOCK_TOLERANCE, ended_at + MAX_CLOCK_TOLERANCE),
        Window.at(received_at),
    ):
        try:
            found = episode_photo.accepted_version(views, document, window)
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
        if found is not None:
            return _by_version(versions, found)
    return None


# --- el bloque presentado --------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessAtEventView:
    value: float | str | None
    """Analógica: el número (``None`` si la lectura no fue válida); de estado, el valor lógico."""
    valid: bool
    offset_ms: int
    read_at: datetime | None
    """``node_time.started_at + offset_ms``, presentado (agregado en zona unipersonal)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessSampleView:
    offset_ms: int
    value: float | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessChangeView:
    offset_ms: int
    value: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessVariableView:
    variable_id: uuid.UUID
    kind: str
    """La clase que trae el registro (la ingesta comprobó que es la del catálogo)."""
    code: str | None
    description_es: str | None
    unit: str | None
    decimals: int | None
    states: str | None
    at_event: ProcessAtEventView
    min: float | None
    max: float | None
    last: float | None
    samples: tuple[ProcessSampleView, ...]
    changes: tuple[ProcessChangeView, ...]
    truncated: bool | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessContextView:
    from_offset_ms: int
    to_offset_ms: int
    variables: tuple[ProcessVariableView, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessVariableSummary:
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
    read_at: datetime | None


def _number(value: Any) -> float | None:
    """El número guardado, sin redondear (el JSON canónico escribe ``1397.0`` como ``1397``)."""
    if value is None or isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _reading(value: Any) -> float | str | None:
    if isinstance(value, str):
        return value
    return _number(value)


def _offset(value: Any) -> int:
    if type(value) is not int:
        raise ValueError("desplazamiento no entero")
    return value


def _variable(
    item: Mapping[str, Any],
    declared: DeclaredCatalogVersion | None,
    started_at: datetime | None,
    shown: Callable[[datetime], datetime],
) -> ProcessVariableView:
    variable_id = uuid.UUID(str(item["variable_id"]))
    declaration = None if declared is None else declared.variables.get(variable_id)
    event = item["at_event"]
    offset = _offset(event["offset_ms"])
    return ProcessVariableView(
        variable_id=variable_id,
        kind=str(item["kind"]),
        code=None if declaration is None else declaration.code,
        description_es=None if declaration is None else declaration.description_es,
        unit=None if declaration is None else declaration.unit,
        decimals=None if declaration is None else declaration.decimals,
        states=None if declaration is None else declaration.states,
        at_event=ProcessAtEventView(
            value=_reading(event.get("value")),
            valid=event["valid"] is True,
            offset_ms=offset,
            read_at=(
                None if started_at is None else shown(started_at + timedelta(milliseconds=offset))
            ),
        ),
        min=_number(item.get("min")),
        max=_number(item.get("max")),
        last=_number(item.get("last")),
        samples=tuple(
            ProcessSampleView(offset_ms=_offset(s["offset_ms"]), value=_number(s.get("value")))
            for s in item.get("samples") or ()
        ),
        changes=tuple(
            ProcessChangeView(offset_ms=_offset(c["offset_ms"]), value=str(c["value"]))
            for c in item.get("changes") or ()
        ),
        truncated=item.get("truncated") if type(item.get("truncated")) is bool else None,
    )


def process_context_view(
    block: Any,
    declared: DeclaredCatalogVersion | None,
    *,
    started_at: datetime | None,
    shown: Callable[[datetime], datetime],
) -> ProcessContextView | None:
    """El bloque ``process_context`` guardado con lo resuelto de ``declared``; ``None`` sin bloque
    (o con uno que no tiene la forma del contrato: nunca se inventa)."""
    if not isinstance(block, Mapping):
        return None
    try:
        return ProcessContextView(
            from_offset_ms=_offset(block["from_offset_ms"]),
            to_offset_ms=_offset(block["to_offset_ms"]),
            variables=tuple(
                _variable(item, declared, started_at, shown) for item in block["variables"]
            ),
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def process_summary(view: ProcessContextView | None) -> tuple[ProcessVariableSummary, ...]:
    """El resumen por variable del hito de detección: valor o estado en el instante del hecho y
    su hora presentada; sin mínimo, máximo, serie ni cambios."""
    if view is None:
        return ()
    return tuple(
        ProcessVariableSummary(
            variable_id=item.variable_id,
            kind=item.kind,
            code=item.code,
            description_es=item.description_es,
            unit=item.unit,
            decimals=item.decimals,
            states=item.states,
            value=item.at_event.value,
            valid=item.at_event.valid,
            read_at=item.at_event.read_at,
        )
        for item in view.variables
    )


def started_at_of(document: Mapping[str, Any]) -> datetime | None:
    """``node_time.started_at`` del documento, o ``None``."""
    node_time = document.get("node_time")
    return _instant(node_time.get("started_at")) if isinstance(node_time, Mapping) else None
