"""L-7: MAN-05, la hora exacta no se presenta en una zona unipersonal.

Mutación: devolver ``moment`` sin agregar en ``time_aggregation.presented``.
Detalle, bandeja, historial, puerto caído y exportación tienen además PR-LAZ-02 y H-37.
"""

from datetime import UTC, datetime

import pytest

from vigia_platform.loop.domain.time_aggregation import presented


@pytest.mark.parametrize("minute", [1, 17, 59])
def test_laz_l07_exact_minutes_cannot_identify_an_occupant(minute: int) -> None:
    exact = datetime(2026, 10, 19, 10, minute, 23, tzinfo=UTC)
    assert presented(exact, single_occupancy=True, window_minutes=60) == datetime(
        2026, 10, 19, 10, tzinfo=UTC
    )
    assert presented(exact, single_occupancy=False, window_minutes=60) == exact
