"""PR-LAZ-38: en una zona unipersonal los cubos de la distribución horaria tienen exactamente el
ancho de la ventana de agregación, y ningún cubo, etiqueta ni campo lleva nombre de turno, equipo
o grupo (TASK-322; BR-LAZ-122; PAT-LAZ-MAN-05; RF-PLA-19 salvaguarda (a)).

Sobre ``zones_with_privacy`` (las de PR-LAZ-02: unipersonal o no, ventanas de 15 a 480 con los
bordes y los divisores de la hora por delante) y episodios confirmados en instantes con cualquier
zona horaria y en plantas de varias zonas horarias (con cambio de hora y medias horas):

- unipersonal: ``ceil(1440 / ventana)`` cubos, **todos** de la ventana, contiguos desde la
  medianoche local; si no: 24 cubos de 60 minutos;
- cada episodio cae en el cubo que contiene la hora local (de pared) de su inicio, y los cubos
  suman lo confirmado;
- un cubo no tiene más campos que su inicio, su ancho y su cifra (ni en el cálculo, ni en la fila
  guardada, ni en la respuesta), y la ruta no acepta ni devuelve nada con nombre de persona,
  turno, equipo o grupo, ni otro parámetro que ``zone_id`` y ``period`` (sin desagregación).
"""

from __future__ import annotations

import dataclasses
import math
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.routing import APIRoute
from hypothesis import given
from hypothesis import strategies as st

from tests.properties.laz.strategies.inbox import PrivacyZone, instants, zones_with_privacy
from tests.properties.laz.test_pr_laz_01_no_person_parameters import PERSON_WORDS, route_names
from vigia_platform.accreditation.adapters.http.exposure_routes import (
    ExposureHourlyBucketOut,
    exposure_router,
)
from vigia_platform.accreditation.exposure.compute import (
    MINUTES_PER_DAY,
    EpisodeInput,
    HourlyBucket,
    Span,
    compute_exposure,
    hourly_distribution,
)

_TIMEZONES = st.sampled_from(
    ("America/Bogota", "Europe/Madrid", "Asia/Kolkata", "America/Santiago", "UTC")
)
GROUP_WORDS = frozenset({"group", "groups", "grupo", "grupos", "label", "labels", "etiqueta"})
"""BR-LAZ-122: ni grupo de trabajo; y ningún cubo lleva etiqueta (solo su inicio y su ancho)."""


def _wall_minute(moment: datetime, timezone: ZoneInfo) -> int:
    local = moment.astimezone(timezone)
    return local.hour * 60 + local.minute


@st.composite
def contributions(draw: st.DrawFn) -> list[tuple[datetime, int]]:
    return draw(st.lists(st.tuples(instants(), st.integers(0, 3_600_000)), max_size=12))


@given(zones_with_privacy(), _TIMEZONES, contributions())
def test_pr_laz_38_buckets_have_exactly_the_window_width_in_single_occupancy_zones(
    zone: PrivacyZone, timezone_name: str, confirmed: list[tuple[datetime, int]]
) -> None:
    timezone = ZoneInfo(timezone_name)
    buckets = hourly_distribution(
        confirmed,
        timezone=timezone,
        single_occupancy=zone.single_occupancy,
        window_minutes=zone.window_minutes,
    )
    width = zone.window_minutes if zone.single_occupancy else 60
    assert len(buckets) == math.ceil(MINUTES_PER_DAY / width)
    assert all(bucket.width_minutes == width for bucket in buckets)
    assert [bucket.start_minute for bucket in buckets] == [i * width for i in range(len(buckets))]
    assert buckets[-1].start_minute < MINUTES_PER_DAY <= buckets[-1].start_minute + width
    assert sum(bucket.confirmed_ms for bucket in buckets) == sum(amount for _, amount in confirmed)
    expected = [0] * len(buckets)
    for started_at, amount in confirmed:
        expected[_wall_minute(started_at, timezone) // width] += amount
    assert [bucket.confirmed_ms for bucket in buckets] == expected


@given(zones_with_privacy(), _TIMEZONES, instants())
def test_pr_laz_38_the_indicator_uses_the_zone_window(
    zone: PrivacyZone, timezone_name: str, started_at: datetime
) -> None:
    episode = EpisodeInput(
        episode_id=uuid.uuid5(uuid.NAMESPACE_URL, "vigia/exposure/38"),
        family="dwell",
        tier="tier_1",
        started_at=started_at,
        ended_at=started_at + timedelta(minutes=5),
        condition_duration_ms=300_000,
        classification_outcome="confirmed",
    )
    result = compute_exposure(
        period=Span(started_at, episode.ended_at),
        episodes=(episode,),
        observable=(Span(started_at, episode.ended_at),),
        revoked=(),
        hard_signal=True,
        threshold_percent=80,
        timezone=ZoneInfo(timezone_name),
        single_occupancy=zone.single_occupancy,
        window_minutes=zone.window_minutes,
    )
    width = zone.window_minutes if zone.single_occupancy else 60
    assert {bucket.width_minutes for bucket in result.hourly} == {width}
    (hit,) = [bucket for bucket in result.hourly if bucket.confirmed_ms]
    minute = _wall_minute(started_at, ZoneInfo(timezone_name))
    assert hit.start_minute <= minute < hit.start_minute + width
    assert hit.confirmed_ms == 300_000


def test_pr_laz_38_a_bucket_has_only_its_start_its_width_and_its_figure() -> None:
    assert [field.name for field in dataclasses.fields(HourlyBucket)] == [
        "start_minute",
        "width_minutes",
        "confirmed_ms",
    ]
    assert set(ExposureHourlyBucketOut.model_json_schema()["properties"]) == {
        "start_minute_of_day",
        "width_minutes",
        "confirmed_minutes",
    }


def test_pr_laz_38_the_route_has_no_shift_team_group_or_person_name_and_no_breakdown() -> None:
    (route,) = [
        route
        for route in exposure_router().routes
        if isinstance(route, APIRoute) and route.path == "/accreditation/exposure"
    ]
    names = list(route_names(route))
    assert {"zone_id", "period", "hourly_distribution", "start_minute_of_day"} <= {
        name for _, name in names
    }
    forbidden = PERSON_WORDS | GROUP_WORDS
    for place, name in names:
        words = set(name.lower().replace("-", "_").split("_"))
        assert not words & forbidden, (place, name)
    parameters = {param.name for param in route.dependant.query_params}
    assert parameters == {"zone_id", "period"}
