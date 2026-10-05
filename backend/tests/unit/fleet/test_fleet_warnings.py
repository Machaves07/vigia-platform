"""Evaluador de referencia de los avisos del inventario (TASK-224; las ocho clases de
``fleet_alarm_kind``; BR-GOB-49, 65, 74, 76, 78, 79, 80 y 94) y umbrales por planta (DE §3.10).

Cada clase en su borde: justo en el umbral no avisa y un paso más allá sí; un nodo revocado o dado
de baja nunca muestra avisos; el rótulo del panel es «Sin latido desde», nunca «sin eventos».
"""

from __future__ import annotations

import ast
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import vigia_platform
from vigia_platform.fleet.domain.enums import FleetAlarmKind
from vigia_platform.fleet.domain.fleet_thresholds import (
    DEFAULT_CLOCK_DRIFT_THRESHOLD_MS,
    DEFAULT_QUEUE_AGE_THRESHOLD_MINUTES,
    DEFAULT_QUEUE_PENDING_THRESHOLD,
    MAX_THRESHOLD,
    FleetThresholds,
    ThresholdInvalid,
    check_threshold,
)
from vigia_platform.fleet.domain.fleet_warnings import (
    CameraReading,
    HeartbeatNotice,
    WarningInputs,
    evaluate,
    heartbeat_notice,
    orphan_clips_growing,
)
from vigia_platform.shared.api.labels import DEFAULT_LABELS_PATH, PlatformLabels
from vigia_platform.shared.observability.metrics import CATALOG, MetricKind

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
US = timedelta(microseconds=1)
PLANT = uuid.UUID("00000000-0000-4000-8000-000000000001")
THRESHOLDS = FleetThresholds(plant_id=PLANT)
QUIET = WarningInputs(
    status="enrolled",
    last_heartbeat_at=NOW - timedelta(seconds=10),
    pending=0,
    offset_ms=0,
    adapter="modbus_rtu",
    cameras=(CameraReading(25.0, 10.0),),
)
K = FleetAlarmKind


def _warnings(**changes: object) -> tuple[FleetAlarmKind, ...]:
    return evaluate(replace(QUIET, **changes), THRESHOLDS, NOW)  # type: ignore[arg-type]


def test_a_quiet_node_has_no_warnings() -> None:
    assert _warnings() == ()


@pytest.mark.parametrize(("interval", "seconds"), [(60, 300), (15, 75), (600, 3000)])
def test_node_mute_is_strictly_more_than_five_effective_intervals(
    interval: int, seconds: int
) -> None:
    at_border = NOW - timedelta(seconds=seconds)
    assert _warnings(last_heartbeat_at=at_border, heartbeat_interval_seconds=interval) == ()
    assert _warnings(last_heartbeat_at=at_border - US, heartbeat_interval_seconds=interval) == (
        K.NODE_MUTE,
    )


def test_a_node_that_never_sent_a_heartbeat_is_not_mute() -> None:
    # BR-GOB-74: sin latido el estado es unknown; mute empieza en last_heartbeat_at.
    assert _warnings(last_heartbeat_at=None) == ()


def test_queue_over_threshold_by_pending_or_by_age() -> None:
    assert _warnings(pending=DEFAULT_QUEUE_PENDING_THRESHOLD) == ()
    assert _warnings(pending=DEFAULT_QUEUE_PENDING_THRESHOLD + 1) == (K.QUEUE_OVER_THRESHOLD,)
    border = NOW - timedelta(minutes=DEFAULT_QUEUE_AGE_THRESHOLD_MINUTES)
    assert _warnings(oldest_pending_at=border) == ()
    assert _warnings(oldest_pending_at=border - US) == (K.QUEUE_OVER_THRESHOLD,)
    # Las dos causas a la vez: una sola clase.
    both = _warnings(pending=10_000, oldest_pending_at=border - timedelta(hours=1))
    assert both == (K.QUEUE_OVER_THRESHOLD,)


@pytest.mark.parametrize("sign", [1, -1])
def test_clock_drift_uses_the_absolute_offset(sign: int) -> None:
    assert _warnings(offset_ms=sign * DEFAULT_CLOCK_DRIFT_THRESHOLD_MS) == ()
    assert _warnings(offset_ms=sign * (DEFAULT_CLOCK_DRIFT_THRESHOLD_MS + 1)) == (K.CLOCK_DRIFT,)


def test_version_retiring_is_the_date_the_contract_gives() -> None:
    assert _warnings(retires_at="2027-01-01T00:00:00.000Z") == (K.VERSION_RETIRING,)
    assert _warnings(retires_at=None) == ()


@pytest.mark.parametrize("adapter", ["simulated", "file"])
def test_simulated_adapter_only_with_a_productive_zone(adapter: str) -> None:
    assert _warnings(adapter=adapter, productive_zone=True) == (K.SIMULATED_ADAPTER_IN_PRODUCTIVE,)
    assert _warnings(adapter=adapter, productive_zone=False) == ()
    assert _warnings(adapter="modbus_rtu", productive_zone=True) == ()


def test_certificate_expiring_within_fifteen_days_or_already_expired() -> None:
    border = NOW + timedelta(days=15)
    assert _warnings(certificate_expires_at=border) == (K.CERTIFICATE_EXPIRING,)
    assert _warnings(certificate_expires_at=border + US) == ()
    assert _warnings(certificate_expires_at=NOW - timedelta(days=1)) == (K.CERTIFICATE_EXPIRING,)
    assert _warnings(certificate_expires_at=None) == ()


def test_camera_below_min_fps_is_strictly_below() -> None:
    assert _warnings(cameras=(CameraReading(10.0, 10.0),)) == ()
    assert _warnings(cameras=(CameraReading(25.0, 10.0), CameraReading(9.99, 10.0))) == (
        K.CAMERA_BELOW_MIN_FPS,
    )
    assert _warnings(cameras=()) == ()


def test_orphan_clips_growing_more_than_fifty_or_more_than_five_percent() -> None:
    assert not orphan_clips_growing(50, 10_000)
    assert orphan_clips_growing(51, 10_000)
    assert not orphan_clips_growing(5, 100)
    assert orphan_clips_growing(6, 100)
    assert not orphan_clips_growing(0, 0)
    # Sin clips del día, un solo huérfano ya supera el 5 % (regla literal, sin división por cero).
    assert orphan_clips_growing(1, 0)
    assert _warnings(orphan_clips=51, day_clips=10_000) == (K.ORPHAN_CLIPS_GROWING,)


def test_a_revoked_or_decommissioned_node_shows_no_warnings() -> None:
    everything = {
        "last_heartbeat_at": NOW - timedelta(days=3),
        "pending": 10_000,
        "offset_ms": 10**9,
        "retires_at": "2027-01-01T00:00:00.000Z",
        "adapter": "simulated",
        "productive_zone": True,
        "certificate_expires_at": NOW,
        "cameras": (CameraReading(0.0, 10.0),),
        "orphan_clips": 1_000,
    }
    assert _warnings(**everything) == tuple(FleetAlarmKind)
    assert _warnings(status="revoked", **everything) == ()
    assert _warnings(decommissioned_at=NOW - timedelta(hours=1), **everything) == ()
    # La re-alta no retira al nodo: sigue mostrando sus avisos.
    assert _warnings(status="re_enrollment_pending", **everything) == tuple(FleetAlarmKind)


def test_the_thresholds_of_the_plant_decide() -> None:
    strict = FleetThresholds(
        plant_id=PLANT,
        queue_pending_threshold=1,
        queue_age_threshold_minutes=1,
        clock_drift_threshold_ms=1,
        updated_by=PLANT,
        updated_at=NOW,
    )
    inputs = replace(QUIET, pending=2, offset_ms=-2)
    assert evaluate(inputs, THRESHOLDS, NOW) == ()
    assert evaluate(inputs, strict, NOW) == (K.QUEUE_OVER_THRESHOLD, K.CLOCK_DRIFT)


def test_default_thresholds_and_their_bounds() -> None:
    assert (
        THRESHOLDS.queue_pending_threshold,
        THRESHOLDS.queue_age_threshold_minutes,
        THRESHOLDS.clock_drift_threshold_ms,
    ) == (100, 30, 5_000)
    assert THRESHOLDS.is_default
    for valid in (1, MAX_THRESHOLD):
        assert check_threshold(valid) == valid
    for invalid in (0, -1, MAX_THRESHOLD + 1, True, 1.0, "1", None):
        with pytest.raises(ThresholdInvalid):
            check_threshold(invalid)
    with pytest.raises(ThresholdInvalid):
        FleetThresholds(plant_id=PLANT, queue_age_threshold_minutes=0)
    with pytest.raises(ValueError, match="juntos"):
        FleetThresholds(plant_id=PLANT, updated_by=PLANT)


INVENTORY_MODULES = (
    "fleet/domain/fleet_warnings.py",
    "fleet/domain/fleet_thresholds.py",
    "fleet/application/inventory_read.py",
    "fleet/application/fleet_thresholds.py",
    "fleet/adapters/postgres/inventory_queries.py",
    "fleet/adapters/postgres/assignment_queries.py",
    "fleet/adapters/http/inventory.py",
    "fleet/adapters/http/fleet_thresholds.py",
    "fleet/ports.py",
)


def test_nfr_gob_13_no_metric_with_zone_label_nor_node_histogram() -> None:
    # Metapropiedad sobre el catálogo único de métricas: ninguna lleva etiqueta de zona y ningún
    # histograma lleva etiqueta de nodo. El inventario no añade métricas (no importa el módulo).
    for spec in CATALOG:
        assert "zone_id" not in spec.attributes, spec.name
        if spec.kind is MetricKind.HISTOGRAM:
            assert "node_id" not in spec.attributes, spec.name
    root = Path(vigia_platform.__file__).parent
    for module in INVENTORY_MODULES:
        tree = ast.parse((root / module).read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert not any("observability.metrics" in name for name in imported), module


def test_heartbeat_notice_says_no_heartbeat_since_never_no_events() -> None:
    assert heartbeat_notice(None, "unknown", ()) is HeartbeatNotice.NO_HEARTBEAT_RECEIVED
    last = NOW - timedelta(minutes=10)
    assert heartbeat_notice(last, "mute", ()) is HeartbeatNotice.NO_HEARTBEAT_SINCE
    assert heartbeat_notice(last, "reachable", (K.NODE_MUTE,)) is HeartbeatNotice.NO_HEARTBEAT_SINCE
    assert heartbeat_notice(last, "reachable", ()) is None
    labels = PlatformLabels.load()
    assert labels.label("heartbeat_notice", HeartbeatNotice.NO_HEARTBEAT_SINCE) == (
        "Sin latido desde"
    )
    # BR-GOB-75: ninguna etiqueta de la plataforma dice «sin eventos».
    document = json.loads(DEFAULT_LABELS_PATH.read_text(encoding="utf-8"))
    texts = [text.lower() for values in document.values() for text in values.values()]
    assert texts and not any("sin eventos" in text for text in texts)
