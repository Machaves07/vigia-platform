"""Generadores de la bandeja del lazo (TASK-307; PR-LAZ-02 y 57).

- ``zones_with_privacy``: una zona con su marca de ocupación unipersonal y su ventana de
  agregación en todo el rango de U-03 (15 a 480 minutos), con los bordes siempre a mano.
- ``findings_over_time``: filas de ``loop.finding_state`` de esa zona, como las devuelve la
  sentencia de la bandeja, con marcas en cualquier instante (con microsegundos y con zonas horarias
  distintas de UTC), plazo y revocación opcionales y el cubo de la proyección
  (``loop.projection.fold.aggregation_bucket`` con la ventana por defecto, como al nacer la fila).

Solo datos generados; ninguna marca sale de la hora real.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Final

from hypothesis import strategies as st

from vigia_platform.loop.adapters.postgres.inbox_query import INBOX_STATES
from vigia_platform.loop.domain.time_aggregation import MAX_WINDOW_MINUTES, MIN_WINDOW_MINUTES
from vigia_platform.loop.projection.fold import (
    DEFAULT_AGGREGATION_WINDOW_MINUTES,
    aggregation_bucket,
)

__all__ = ["PrivacyZone", "findings_over_time", "instants", "windows", "zones_with_privacy"]

# Hypothesis pide los límites sin zona horaria: la zona la pone ``timezones``.
_EARLIEST: Final = datetime(2020, 1, 1)  # noqa: DTZ001
_LATEST: Final = datetime(2040, 12, 31)  # noqa: DTZ001
_OFFSETS: Final = (
    UTC,
    timezone(timedelta(hours=-5)),
    timezone(timedelta(hours=5, minutes=30)),
    timezone(timedelta(hours=-9, minutes=-30)),
    timezone(timedelta(hours=14)),
)


@dataclass(frozen=True, slots=True)
class PrivacyZone:
    zone_id: uuid.UUID
    single_occupancy: bool
    window_minutes: int


def windows() -> st.SearchStrategy[int]:
    """Ventanas de 15 a 480 minutos, con los bordes y los divisores de la hora por delante."""
    return st.one_of(
        st.sampled_from((MIN_WINDOW_MINUTES, 30, 45, 60, 90, 120, 240, MAX_WINDOW_MINUTES)),
        st.integers(MIN_WINDOW_MINUTES, MAX_WINDOW_MINUTES),
    )


def instants() -> st.SearchStrategy[datetime]:
    """Instantes con zona horaria (no siempre UTC) y microsegundos."""
    return st.datetimes(min_value=_EARLIEST, max_value=_LATEST, timezones=st.sampled_from(_OFFSETS))


def zones_with_privacy() -> st.SearchStrategy[PrivacyZone]:
    return st.builds(PrivacyZone, st.uuids(version=4), st.booleans(), windows())


@st.composite
def _finding_row(draw: st.DrawFn, zone: PrivacyZone) -> dict[str, Any]:
    exact = draw(instants())
    due = draw(st.none() | instants())
    revoked = draw(st.booleans())
    revoked_at = draw(instants()) if revoked else None
    regression = draw(st.booleans())
    exact_utc = exact.astimezone(UTC)
    return {
        "finding_state_id": str(draw(st.uuids())),
        "plant_id": str(draw(st.uuids())),
        "zone_id": str(zone.zone_id),
        "subject_kind": draw(st.sampled_from(("finding", "detection"))),
        "family": draw(
            st.sampled_from(("coexistence", "guard_bypass", "dwell", "startup_transition"))
        ),
        "tier": draw(st.sampled_from(("tier_1", "tier_2"))),
        "standard_id": str(draw(st.uuids())),
        "standard_version": draw(st.integers(1, 10_000)),
        "lifecycle_state": draw(st.sampled_from(INBOX_STATES)).value,
        "current_classification_outcome": draw(
            st.none() | st.sampled_from(("confirmed", "authorized_operation", "false_positive"))
        ),
        "classification_count": draw(st.integers(0, 50)),
        "due_at": None if due is None else due.isoformat(),
        "overdue_days": draw(st.integers(0, 400)),
        "observability_state_at_event": draw(
            st.sampled_from(("observable", "degraded", "not_observable"))
        ),
        "zone_agreement_revoked": revoked,
        "zone_agreement_revoked_at": None if revoked_at is None else revoked_at.isoformat(),
        "single_occupancy_zone": zone.single_occupancy,
        "aggregation_window_minutes": zone.window_minutes,
        "walk_test_regression_pending": regression,
        "walk_test_regression_cause": (
            draw(st.sampled_from(("catalog_change", "model_version_change")))
            if regression
            else None
        ),
        # La sentencia devuelve las marcas como texto ISO de jsonb (a veces con desfase).
        "occurred_at_exact": exact.isoformat(),
        "occurred_at_bucket": aggregation_bucket(
            exact_utc, DEFAULT_AGGREGATION_WINDOW_MINUTES
        ).isoformat(),
        "age_days": draw(st.integers(0, 4000)),
        "evidence_flagged": draw(st.booleans()),
        "missing_segments_declared": draw(st.booleans()),
    }


def findings_over_time(
    zone: PrivacyZone, *, min_size: int = 1, max_size: int = 20
) -> st.SearchStrategy[list[dict[str, Any]]]:
    """Filas de la bandeja de ``zone`` repartidas en el tiempo."""
    return st.lists(_finding_row(zone), min_size=min_size, max_size=max_size)
