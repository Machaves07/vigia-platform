"""Piezas puras de los perfiles de carga, sin contenedores (TASK-231).

El veredicto del cliente sintético nombra la ruta que incumple su p95, responde fuera de 2xx o no
reúne muestras; el informe y la detección de secretos (PR-GOB-31); las fases de los perfiles; el
cálculo del vaciado, de los ``rate_limited`` por debajo del mínimo de NFR-CTR-02 y de la peor
proporción de rechazos permanentes en 15 minutos; la pérdida de respuestas una sola vez por
cuerpo y que la etiqueta de una subida nunca lleva su URL prefirmada.
"""

from __future__ import annotations

import datetime as dt
import json
import socket
from pathlib import Path
from typing import Any

import httpx
import pytest
from vigia_contracts.conformance.simulated_node.dataset import load_sealed_dataset

from tests.load import soak
from tests.load.console_client import CONSOLE_ROUTES, MIN_SAMPLES, Sample, evaluate
from tests.load.driver import LostReplies, route_label
from tests.load.profiles import PROFILES, nightly_profile, write_sealed_dataset
from tests.load.report import analyse, drains_of, percentile, secret_findings

T0 = dt.datetime(2026, 10, 7, 12, 0, tzinfo=dt.UTC)


def _stamp(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _parse_stamp(stamp: str) -> dt.datetime:
    return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _samples(route: str, elapsed: list[float], status: int = 200) -> list[Sample]:
    return [
        Sample(route, T0 + dt.timedelta(seconds=index), value, status)
        for index, value in enumerate(elapsed)
    ]


def _healthy() -> list[Sample]:
    return [
        sample
        for route in CONSOLE_ROUTES
        for sample in _samples(route.name, [route.p95_ms / 2] * MIN_SAMPLES)
    ]


# --- Veredicto del cliente sintético --------------------------------------------------------------


def test_the_verdict_passes_when_every_route_meets_its_p95() -> None:
    verdict = evaluate(_healthy())
    assert verdict.passed, verdict.failures


def test_the_verdict_names_the_route_whose_p95_is_over_its_target() -> None:
    slow = "GET /zones/{zone_id}/gates"
    samples = [sample for sample in _healthy() if sample.route != slow]
    samples += _samples(slow, [150.0] * 18 + [450.0, 460.0])  # el p95 cae en la cola
    verdict = evaluate(samples)
    assert not verdict.passed
    assert len(verdict.failures) == 1
    assert verdict.failures[0].startswith(f"{slow}: p95 ")
    assert slow in verdict.message()


def test_the_p95_at_exactly_the_target_passes() -> None:
    route = CONSOLE_ROUTES[0]
    samples = [s for s in _healthy() if s.route != route.name]
    samples += _samples(route.name, [route.p95_ms] * MIN_SAMPLES)
    assert evaluate(samples).passed


def test_a_route_without_enough_samples_in_the_window_fails_by_name() -> None:
    route = CONSOLE_ROUTES[2]
    samples = [s for s in _healthy() if s.route != route.name]
    samples += _samples(route.name, [10.0] * (MIN_SAMPLES - 1))
    verdict = evaluate(samples)
    assert verdict.failures == (f"{route.name}: {MIN_SAMPLES - 1} muestras (mínimo {MIN_SAMPLES})",)


def test_a_non_2xx_answer_fails_by_name_even_with_a_fast_p95() -> None:
    route = CONSOLE_ROUTES[1]
    samples = [s for s in _healthy() if s.route != route.name]
    samples += _samples(route.name, [10.0] * MIN_SAMPLES, status=200)
    samples += _samples(route.name, [10.0], status=503)
    verdict = evaluate(samples)
    assert verdict.failures == (f"{route.name}: respondió 503",)


def test_only_samples_inside_the_windows_count() -> None:
    route = CONSOLE_ROUTES[0]
    before = T0 - dt.timedelta(hours=1)
    samples = [*_healthy(), *(Sample(route.name, before, 10_000.0, 200) for _ in range(5))]
    assert not evaluate(samples).passed
    window = (T0, T0 + dt.timedelta(minutes=5))
    assert evaluate(samples, windows=[window]).passed


def test_an_unpublished_route_is_reported_and_never_measured() -> None:
    record = "GET /commissioning-records/{record_id}"
    samples = [s for s in _healthy() if s.route != record]
    verdict = evaluate(samples, unpublished=[record])
    assert verdict.passed
    assert verdict.routes[record]["status"] == "unpublished"


def test_percentile_is_nearest_rank() -> None:
    assert percentile([], 95) is None
    assert percentile([5.0], 95) == 5.0
    assert percentile([float(n) for n in range(1, 101)], 95) == 95.0
    assert percentile([float(n) for n in range(1, 21)], 95) == 19.0


# --- Secretos (PR-GOB-31) ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "-----BEGIN CERTIFICATE-----\nMIIB",
        "-----begin private key-----",
        "https://s3/x?X-Amz-Algorithm=AWS4&X-Amz-Credential=a%2Fb&X-Amz-Signature=00",
        "x-amz-signature=deadbeef",
    ],
)
def test_a_pem_or_a_presigned_url_is_found_by_name_and_never_echoed(text: str) -> None:
    found = secret_findings({"informe": f"antes {text} después"})
    assert found and all(item.startswith("informe: ") for item in found)
    assert all(text not in item for item in found)


def test_an_enrollment_code_is_found_without_echoing_it() -> None:
    code = "ABCD-EFGH-IJKL"
    assert secret_findings({"salida": f"x{code}y"}, [code]) == ["salida: código de alta"]
    assert secret_findings({"salida": "limpia"}, [code]) == []


# --- Perfiles ----------------------------------------------------------------------------------


def test_the_ci_profile_is_ten_nodes_at_sixty_for_five_minutes() -> None:
    ci = PROFILES["ci"]
    assert (ci.nodes, ci.speed_factor, ci.wall_seconds) == (10, 60.0, 300.0)
    assert [phase.name for phase in ci.phases()] == ["steady"]
    assert ci.lost_reply_fraction > 0


def test_the_nightly_profile_is_one_hundred_nodes_three_hundred_zones_and_two_outages() -> None:
    nightly = PROFILES["nightly"]
    assert (nightly.nodes, nightly.zones, nightly.speed_factor) == (100, 300, 60.0)
    phases = {phase.name: phase for phase in nightly.phases()}
    assert phases["steady"].end_ms == 30 * 60_000 * 60
    assert phases["reconnection"].end_ms - phases["reconnection"].start_ms == 3_600_000
    assert phases["outage"].end_ms - phases["outage"].start_ms == 4 * 3_600_000
    assert phases["reconnection"].unreachable and phases["outage"].unreachable
    assert not phases["steady"].unreachable
    assert nightly.console


def test_the_smoke_scale_stays_within_twenty_nodes_and_three_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIGIA_LOAD_SCALE", "smoke")
    smoke = nightly_profile()
    assert smoke.nodes <= 20 and smoke.wall_seconds <= 180
    assert [p.name for p in smoke.phases()] == [p.name for p in PROFILES["nightly"].phases()]
    monkeypatch.setenv("VIGIA_LOAD_SCALE", "otra")
    with pytest.raises(ValueError, match="full o smoke"):
        nightly_profile()
    monkeypatch.delenv("VIGIA_LOAD_SCALE")
    assert nightly_profile() is PROFILES["nightly"]


def test_the_synthetic_sealed_dataset_passes_the_kit_verification(tmp_path: Path) -> None:
    root = write_sealed_dataset(tmp_path / "conjunto", 7, T0)
    assert len(load_sealed_dataset(root).names) == 30


# --- Análisis del resultado de los nodos -------------------------------------------------------


def _result(**changes: Any) -> dict[str, Any]:
    end = T0 + dt.timedelta(seconds=60)
    records = {
        f"r{index}": {
            "emitted_at": _stamp(T0 + dt.timedelta(seconds=index)),
            "accepted_at": _stamp(end + dt.timedelta(seconds=index / 10)),
            "node_index": index % 2,
            "plant_id": "p1" if index % 2 else "p2",
        }
        for index in range(50)
    }
    result: dict[str, Any] = {
        "phases": [
            {"name": "reconnection", "unreachable": True, "start": _stamp(T0), "end": _stamp(end)}
        ],
        "records": records,
        "journal": {
            "planned": 50,
            "emitted": list(records),
            "accepted": list(records),
            "rejected": {},
            "emitted_by_kind": {"finding": 50},
        },
        "events": [["submit_record", "success", "", 50]],
        "receipts": {"accepted": 50},
        "lost_replies": 0,
        "rate_limited": [],
        "sends": {},
        "halted": {},
        "latency": {},
    }
    result.update(changes)
    return result


def test_the_drain_rate_is_the_queue_over_the_time_since_the_platform_came_back() -> None:
    (drain,) = drains_of(_result())
    assert drain.queued == 50 and drain.accepted == 50
    assert drain.seconds == pytest.approx(4.9)
    assert drain.writes_per_second == pytest.approx(50 / 4.9)
    # Las 50 aceptaciones caben en una ventana de 10 s: pico de 5 por segundo, 2,5 por planta.
    assert drain.peak_writes_per_second == pytest.approx(5.0)
    assert drain.peak_plant_writes_per_second == {"p1": 2.5, "p2": 2.5}


def test_a_profile_without_outages_has_no_drains() -> None:
    result = _result()
    result["phases"] = [{**result["phases"][0], "name": "steady", "unreachable": False}]
    assert drains_of(result) == []
    assert analyse(result)["drains"] == []


def test_the_peak_of_each_drain_stops_at_the_next_outage() -> None:
    result = _result()
    first = result["phases"][0]
    second_start = _parse_stamp(first["end"]) + dt.timedelta(seconds=2)
    result["phases"].append(
        {
            "name": "outage",
            "unreachable": True,
            "start": _stamp(second_start),
            "end": _stamp(second_start + dt.timedelta(seconds=30)),
        }
    )
    reconnection, outage = drains_of(result)
    # Solo las aceptaciones de los dos primeros segundos tras la vuelta cuentan para la primera.
    assert reconnection.peak_writes_per_second == pytest.approx(2.0)
    assert outage.queued == 0


def test_an_unaccepted_queued_record_leaves_the_drain_without_rate() -> None:
    result = _result()
    result["records"]["r3"]["accepted_at"] = None
    (drain,) = drains_of(result)
    assert drain.accepted == 49 and drain.seconds is None and drain.writes_per_second is None


def test_a_rate_limited_below_the_contract_minimum_is_counted() -> None:
    at = T0 + dt.timedelta(seconds=30)
    sends = {"0": [["submit_record", _stamp(T0 + dt.timedelta(seconds=n))] for n in range(10)]}
    limited = [{"node_index": 0, "operation": "submit_record", "at": _stamp(at)}]
    assert analyse(_result(sends=sends, rate_limited=limited))["rate_limited_below_minimum"] == 1
    many = {"0": [["submit_record", _stamp(at - dt.timedelta(milliseconds=n))] for n in range(61)]}
    assert analyse(_result(sends=many, rate_limited=limited))["rate_limited_below_minimum"] == 0


def test_the_worst_permanent_ratio_is_computed_in_fifteen_minute_windows() -> None:
    result = _result()
    result["journal"]["rejected"] = {"r0": "schema_invalid"}
    assert analyse(result)["worst_permanent_ratio_15min"] == pytest.approx(1.0)
    assert analyse(_result())["worst_permanent_ratio_15min"] == 0.0


# --- soak.py --dry-run ---------------------------------------------------------------------------


def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("soak --dry-run intentó usar la red")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def test_the_soak_dry_run_declares_target_seed_and_absolute_thresholds_without_network(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _no_network(monkeypatch)
    code = soak.main(
        [
            "--dry-run",
            "--target",
            "https://staging-3.example.invalid",
            "--seed",
            "11",
            "--report",
            str(tmp_path),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    document = json.loads(out[: out.rindex("}") + 1])
    assert document["target"] == "https://staging-3.example.invalid"
    assert document["nodes_url"] == "https://staging-3-nodes.example.invalid"
    assert document["seed"] == 11 and "semilla 11" in out
    assert document["speed_factor"] == 1.0 and document["episodes_per_day"] == 77
    assert document["hours"] == 8.0 and document["full_duration"]
    assert document["absolute_thresholds"]["blocking"] is True
    assert set(document["absolute_thresholds"]["targets"]) == {f"NFR-GOB-0{n}" for n in range(1, 6)}
    assert document["provisioning"]["status"] == "pending"
    assert document["plan"]["planned_records"] > 0
    assert json.loads((tmp_path / "soak-dry-run-11.json").read_text()) == document
    assert not secret_findings({"salida": out})


@pytest.mark.parametrize(
    "target",
    [
        "http://staging-0.example.invalid",
        "https://pilot.example.invalid",
        "https://staging-0.example.invalid/ruta",
        "https://staging-0.example.invalid?x=1",
        "https://user:pw@staging-0.example.invalid",
        "https://staging-x.example.invalid",
        "https://staging-0",
    ],
)
def test_the_soak_dry_run_rejects_a_target_that_is_not_a_staging_origin(
    target: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_network(monkeypatch)
    assert soak.main(["--dry-run", "--target", target]) == 2
    assert "error: --target" in capsys.readouterr().err


@pytest.mark.parametrize("hours", ["0", "-1", "8.5"])
def test_the_soak_hours_stay_within_eight(
    hours: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_network(monkeypatch)
    arguments = ["--dry-run", "--target", "https://staging-0.example.invalid", "--hours", hours]
    assert soak.main(arguments) == 2
    assert "--hours" in capsys.readouterr().err


def test_a_soak_run_without_fleet_or_console_session_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_network(monkeypatch)
    assert soak.main(["--target", "https://staging-0.example.invalid"]) == 2
    assert "--fleet y --console-session" in capsys.readouterr().err


def test_the_soak_dry_run_rejects_a_fleet_pointing_elsewhere(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _no_network(monkeypatch)
    fleet = tmp_path / "fleet.json"
    fleet.write_text(json.dumps({"ingest_base_url": "https://otro/api/nodes", "nodes": []}))
    arguments = ["--dry-run", "--target", "https://staging-0.example.invalid"]
    assert soak.main([*arguments, "--fleet", str(fleet)]) == 2
    assert "staging-0-nodes.example.invalid/api/nodes" in capsys.readouterr().err


def test_soak_thresholds_name_each_route_over_its_absolute_target() -> None:
    latency = {
        "POST /api/nodes/findings": {"count": 10, "p95_ms": 520.0, "p99_ms": 900.0},
        "POST /api/nodes/heartbeats": {"count": 10, "p95_ms": 150.0, "p99_ms": 400.0},
    }
    console = {"failures": ["GET /fleet/nodes: p95 600 ms > 500 ms (n=40)"]}
    assert soak.evaluate_thresholds(latency, console) == [
        "NFR-GOB-01 POST /api/nodes/findings: p95_ms 520.0 > 500",
        "NFR-GOB-03 GET /fleet/nodes: p95 600 ms > 500 ms (n=40)",
    ]


# --- Nodo simulado -------------------------------------------------------------------------------


def _record(body: dict[str, Any]) -> httpx.Request:
    return httpx.Request(
        "POST", "https://127.0.0.1/api/nodes/findings", content=json.dumps(body).encode()
    )


def test_a_reply_is_lost_at_most_once_per_body() -> None:
    lost = LostReplies(seed=1, fraction=0.999)
    request = _record({"finding_id": "a"})
    assert lost.drop(request)
    assert not lost.drop(_record({"finding_id": "a"}))
    assert not LostReplies(seed=1, fraction=0.0).drop(request)
    heartbeat = httpx.Request("POST", "https://127.0.0.1/api/nodes/heartbeats", content=b"{}")
    assert not lost.drop(heartbeat)


def test_an_upload_label_never_carries_its_presigned_url() -> None:
    upload = httpx.Request("PUT", "https://127.0.0.1:9/b/k?X-Amz-Signature=00&X-Amz-Credential=1")
    label = route_label(upload, upload=True)
    assert "X-Amz" not in label and "127.0.0.1" not in label
    zone = "0b5d0a0e-5a6b-4f3c-9e0d-2b1a3c4d5e6f"
    catalog = httpx.Request("GET", f"https://127.0.0.1/api/nodes/zones/{zone}/catalog")
    assert route_label(catalog, upload=False) == "GET /api/nodes/zones/{zone_id}/catalog"
