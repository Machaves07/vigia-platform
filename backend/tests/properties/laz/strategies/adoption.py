"""Generadores de la adopción por zona (PR-LAZ-46; BR-LAZ-134 a 136; TASK-323).

``loop_activity_sequences`` da una **secuencia de actividad del lazo** de unas pocas zonas de una
planta a lo largo de unos días: para cada zona, el día de su activación (o ninguno: una zona que
nunca está activa) y las escrituras de cada día, cada una con su tipo de registro (de actividad o
no: ``NOT_LOOP_ACTIVITY``) y la persona que la hizo. La persona **solo viaja en el generador**: el
gancho del contador no tiene ningún parámetro de usuario, y la propiedad comprueba que ningún
identificador generado aparece en lo guardado.

Los días son índices desde el primero de la secuencia (0, 1, …); la tarea corre cada mañana, antes
de la actividad del día, y la activación ocurre a mediodía. Solo datos generados.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from hypothesis import strategies as st

from vigia_platform.accreditation.adoption.counter import (
    LOOP_ACTIVITY_BY_RECORD_TYPE,
    NOT_LOOP_ACTIVITY,
)

__all__ = [
    "ActivitySequence",
    "LoopWrite",
    "ZoneActivity",
    "loop_activity_sequences",
]

_RECORD_TYPES: tuple[str, ...] = (*sorted(LOOP_ACTIVITY_BY_RECORD_TYPE), *sorted(NOT_LOOP_ACTIVITY))


@dataclass(frozen=True)
class LoopWrite:
    """Una escritura del lazo en un día de la secuencia."""

    day: int
    record_type: str
    actor_user_id: uuid.UUID
    """Quién la hizo: el contador **no** lo recibe (H-33)."""


@dataclass(frozen=True)
class ZoneActivity:
    activated_on: int | None
    """Día de la activación (a mediodía), o ``None``: la zona nunca está activa."""
    writes: tuple[LoopWrite, ...]


@dataclass(frozen=True)
class ActivitySequence:
    threshold: int
    """``adoption_silence_days`` de la planta."""
    days: int
    """Días que dura la secuencia (la tarea corre cada mañana de ``0`` a ``days - 1``)."""
    zones: tuple[ZoneActivity, ...]

    def activity_days(self, zone: ZoneActivity) -> frozenset[int]:
        """Días con alguna escritura que es actividad del lazo."""
        return frozenset(
            write.day for write in zone.writes if write.record_type in LOOP_ACTIVITY_BY_RECORD_TYPE
        )


@st.composite
def _zone(draw: st.DrawFn, days: int, actors: tuple[uuid.UUID, ...]) -> ZoneActivity:
    activated_on = draw(st.none() | st.integers(0, max(0, days - 2)))
    # Días sin escrituras de actividad dominan: así las rachas de silencio llegan al umbral.
    writes = draw(
        st.lists(
            st.builds(
                LoopWrite,
                day=st.integers(0, days - 1),
                record_type=st.sampled_from(_RECORD_TYPES),
                actor_user_id=st.sampled_from(actors),
            ),
            max_size=max(2, days // 2),
        )
    )
    return ZoneActivity(activated_on, tuple(sorted(writes, key=lambda write: write.day)))


@st.composite
def _sequences(
    draw: st.DrawFn, max_zones: int, max_threshold: int, max_days: int
) -> ActivitySequence:
    threshold = draw(st.integers(1, max_threshold))
    days = draw(st.integers(threshold + 2, max(threshold + 2, max_days)))
    actors = tuple(draw(st.lists(st.uuids(version=4), min_size=1, max_size=3, unique=True)))
    zones = tuple(draw(st.lists(_zone(days, actors), min_size=1, max_size=max_zones)))
    return ActivitySequence(threshold=threshold, days=days, zones=zones)


def loop_activity_sequences(
    *, max_zones: int = 3, max_threshold: int = 4, max_days: int = 14
) -> st.SearchStrategy[ActivitySequence]:
    """Secuencias de actividad del lazo de hasta ``max_zones`` zonas y ``max_days`` días, con el
    umbral entre 1 y ``max_threshold`` (``loop_activity_sequences`` de BLM §11)."""
    return _sequences(max_zones, max_threshold, max_days)
