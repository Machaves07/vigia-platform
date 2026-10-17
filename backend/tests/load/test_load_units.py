"""Piezas puras de los perfiles de carga, sin contenedores (TASK-231).

El veredicto del cliente sintético nombra la ruta que incumple su p95, responde fuera de 2xx o no
reúne muestras (y las que no se midieron en toda la ejecución, ``unmeasured``); los umbrales del
``soak`` (NFR-GOB-01 por ruta, NFR-GOB-03 por la consola y NFR-GOB-02 por los transitorios por
saturación, A-65); el informe y la detección de secretos (PR-GOB-31); las fases de los perfiles;
el cálculo del vaciado, de los ``rate_limited`` por debajo del mínimo de NFR-CTR-02 y de la peor
proporción de rechazos permanentes en 15 minutos; la pérdida de respuestas una sola vez por
cuerpo y que la etiqueta de una subida nunca lleva su URL prefirmada.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import socket
import subprocess
import sys
import uuid
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final, cast

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from vigia_contracts.client import ClientEvent
from vigia_contracts.client.core import RetryPolicy, Wait
from vigia_contracts.conformance.simulated_node.dataset import load_sealed_dataset
from vigia_contracts.credentials import (
    FileCredentialStore,
    IssuedCredential,
    NodeIdentity,
    NodeKey,
    StoredCredential,
    client_certificate_signing_request,
)
from vigia_contracts.outbox import OutboxState, RecordKind

from tests.load import soak
from tests.load.console_client import (
    CONSOLE_ROUTES,
    MIN_SAMPLES,
    SATURATION_STATUS,
    Sample,
    evaluate,
)
from tests.load.driver import (
    FLUSH_EXTENSION_SECONDS,
    HEARTBEAT_FLOOR_SECONDS,
    HEARTBEAT_SLACK_SECONDS,
    LoadTransport,
    LostReplies,
    NodeRun,
    Reachability,
    Sink,
    Stats,
    _flush_deadline,
    _timeline,
    route_label,
    run_node,
)
from tests.load.profiles import ABSOLUTE_TARGETS, PROFILES, nightly_profile, write_sealed_dataset
from tests.load.report import (
    analyse,
    drains_of,
    percentile,
    rate_limited_trend,
    saturation_transients,
    secret_findings,
)
from tests.load.test_load_ci import assert_functional
from tests.load.test_load_nightly import trend
from tests.virtual_time import run_virtual
from vigia_platform.node_api.limits import NODE_BUDGETS
from vigia_platform.shared.api.declarations import NodeRoute
from vigia_platform.shared.ratelimit import Allowed, RateLimiter, node_key

T0 = dt.datetime(2026, 10, 7, 12, 0, tzinfo=dt.UTC)
SUBPROCESS_SECONDS: Final = 120.0
"""Tope del proceso ``soak.py`` lanzado aparte (nunca decide nada: solo evita colgarse)."""


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


def test_a_503_is_a_failure_of_the_p95_verdict_but_not_a_console_error() -> None:
    # A-65: el 503 es saturación (tendencia en el banco de nightly); cualquier otro fuera de 2xx
    # es un error de la ruta y sigue bloqueando.
    saturated, broken = CONSOLE_ROUTES[0], CONSOLE_ROUTES[1]
    samples = [s for s in _healthy() if s.route not in (saturated.name, broken.name)]
    samples += _samples(saturated.name, [10.0] * MIN_SAMPLES)
    samples += _samples(saturated.name, [10.0], status=SATURATION_STATUS)
    samples += _samples(broken.name, [10.0] * MIN_SAMPLES)
    samples += _samples(broken.name, [10.0], status=503)
    samples += _samples(broken.name, [10.0], status=500)
    verdict = evaluate(samples)
    assert verdict.failures == (
        f"{saturated.name}: respondió 503",
        f"{broken.name}: respondió 500, 503",
    )
    assert verdict.errors == (f"{broken.name}: respondió 500",)
    assert verdict.to_json()["errors"] == [f"{broken.name}: respondió 500"]
    assert broken.name in verdict.errors_message()


def test_a_balancer_502_names_its_transport_error_and_its_instant() -> None:
    # VIG-196: el informe dice qué error de transporte tuvo el balanceador y cuándo, para situarlo
    # frente a las ventanas de caída y de reconexión.
    route = CONSOLE_ROUTES[1]
    samples = [s for s in _healthy() if s.route != route.name]
    samples += _samples(route.name, [10.0] * MIN_SAMPLES)
    failed = Sample(route.name, T0, 12.5, 502, "RemoteProtocolError")
    verdict = evaluate([*samples, failed])
    assert verdict.errors == (f"{route.name}: respondió 502 (balanceador: RemoteProtocolError)",)
    assert verdict.routes[route.name]["non_2xx_samples"] == [
        {
            "at": T0.isoformat(),
            "status": 502,
            "elapsed_ms": 12.5,
            "balancer_error": "RemoteProtocolError",
        }
    ]
    json.dumps(verdict.to_json())  # cabe en el informe


def test_a_slow_or_saturated_console_has_no_errors() -> None:
    route = CONSOLE_ROUTES[0]
    samples = [s for s in _healthy() if s.route != route.name]
    samples += _samples(route.name, [route.p95_ms * 4] * MIN_SAMPLES)
    samples += _samples(route.name, [10.0], status=SATURATION_STATUS)
    verdict = evaluate(samples)
    assert not verdict.passed
    assert verdict.errors == ()
    assert verdict.unmeasured == ()


def test_a_route_without_enough_samples_in_the_whole_run_is_unmeasured() -> None:
    # A-65: el p95 es tendencia, pero una ruta publicada que no reúne MIN_SAMPLES en toda la
    # ejecución no se midió: bloquea por `unmeasured`, no por el p95.
    route = CONSOLE_ROUTES[0]
    samples = [s for s in _healthy() if s.route != route.name]
    samples += _samples(route.name, [10.0] * (MIN_SAMPLES - 1))
    verdict = evaluate(samples)
    assert verdict.unmeasured == (route.name,)
    assert not verdict.errors
    assert verdict.to_json()["unmeasured"] == [route.name]


def _functional(**changes: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    analysis: dict[str, Any] = {
        "emitted": 10,
        "accepted": 10,
        "lost": 0,
        "dead_letter": {},
        "halted": {},
        "rate_limited_below_minimum": 0,
        "rate_limited_by_operation": {},
        "rejection_codes": {},
        "events": {"submit_record:success": 10},
        "drains": [],
        "node_latency": {},
        **changes,
    }
    ledger = {
        "missing": 0,
        "unexpected": 0,
        "records": 10,
        "duplicated_source_keys": 0,
        "chains_verified": 1,
    }
    return analysis, ledger


def test_saturation_transients_are_the_temporarily_unavailable_codes_and_the_503_events() -> None:
    analysis, _ = _functional(
        rejection_codes={
            "heartbeat:temporarily_unavailable": 3,
            "submit_record:idempotency_conflict": 1,
        },
        events={"submit_record:success": 10, "request_grant:unavailable": 2},
    )
    assert saturation_transients(analysis) == {
        "heartbeat:temporarily_unavailable": 3,
        "request_grant:unavailable": 2,
    }


def test_saturation_blocks_in_ci_and_is_trend_in_the_nightly_bench() -> None:
    analysis, ledger = _functional(
        rejection_codes={"heartbeat:temporarily_unavailable": 1},
        events={"submit_record:success": 10, "heartbeat:unavailable": 1},
    )
    with pytest.raises(AssertionError):
        assert_functional(analysis, ledger)
    assert_functional(analysis, ledger, saturation_blocks=False)


@pytest.mark.parametrize(
    "changes",
    [
        {"accepted": 9, "lost": 1},
        # `lost` por separado: con `accepted == emitted` la única aserción que cae es `lost == 0`.
        {"lost": 1},
        {"dead_letter": {"schema_invalid": 1}},
        # A-67: por debajo del mínimo de NFR-CTR-02 y en el latido, un `429` bloquea siempre.
        {"rate_limited_below_minimum": 1, "rate_limited_by_operation": {"request_grant": 1}},
        {"rate_limited_below_minimum": 1, "rate_limited_by_operation": {"submit_record": 1}},
        {"rate_limited_by_operation": {"heartbeat": 1}},
        {"rate_limited_by_operation": {"request_grant": 1, "heartbeat": 1}},
    ],
)
def test_the_functional_checks_block_even_when_saturation_is_trend(
    changes: dict[str, Any],
) -> None:
    analysis, ledger = _functional(**changes)
    with pytest.raises(AssertionError):
        assert_functional(analysis, ledger, saturation_blocks=False)


@pytest.mark.parametrize(
    "limited",
    [{"request_grant": 1}, {"submit_record": 2}, {"request_grant": 1, "submit_record": 1}],
)
def test_a_429_above_the_minimum_is_trend_in_nightly_and_blocks_in_ci(
    limited: dict[str, int],
) -> None:
    # A-67: con `rate_limited_below_minimum == 0`, el `429` de concesiones o registros quedó por
    # encima del mínimo garantizado: en `nightly` va al informe como tendencia; en `ci` falla.
    analysis, ledger = _functional(rate_limited_by_operation=limited)
    assert_functional(analysis, ledger, saturation_blocks=False)
    assert rate_limited_trend(analysis) == limited
    assert trend(analysis)["rate_limited_above_minimum"] == limited
    with pytest.raises(AssertionError):
        assert_functional(analysis, ledger)


def test_the_rate_limited_trend_never_includes_the_heartbeat() -> None:
    analysis, _ = _functional(rate_limited_by_operation={"heartbeat": 1, "request_grant": 3})
    assert rate_limited_trend(analysis) == {"request_grant": 3}
    clean, _ = _functional()
    assert rate_limited_trend(clean) == {}
    assert trend(clean)["rate_limited_above_minimum"] == {}


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


SOAK_TARGET: Final = "https://staging-0.example.invalid"
SYNTHETIC_PEM_BODY: Final = "MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgU0lOVEVUSUNBU0lOVEVU"
SYNTHETIC_PEM: Final = (
    f"-----BEGIN PRIVATE KEY-----\n{SYNTHETIC_PEM_BODY}\n{SYNTHETIC_PEM_BODY[::-1]}\n"
    "-----END PRIVATE KEY-----\n"
)
"""Un PEM sintético mal formado (no es una clave): ningún trozo puede llegar a la salida."""


def _pem_fragments(text: str) -> list[str]:
    """Qué trozos del PEM sintético (cabecera, pie o 8 caracteres seguidos del cuerpo) o qué
    ``input_value`` aparecen en ``text``."""
    pieces = ["-----BEGIN", "-----END", "PRIVATE KEY", "BEGIN P", "KEY-----", "input_value"]
    bodies = (SYNTHETIC_PEM_BODY, SYNTHETIC_PEM_BODY[::-1])
    pieces += [body[start : start + 8] for body in bodies for start in range(len(body) - 7)]
    return [piece for piece in pieces if piece in text]


def _synthetic_certificate(identity: NodeIdentity, key: NodeKey, expires: dt.datetime) -> str:
    """Un certificado de ``key`` firmado por una autoridad sintética de un solo uso."""
    authority = ec.generate_private_key(ec.SECP256R1())
    request = x509.load_pem_x509_csr(client_certificate_signing_request(identity, key).encode())
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Autoridad sintética")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(request.subject)
        .issuer_name(issuer)
        .public_key(request.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(expires - dt.timedelta(days=30))
        .not_valid_after(expires)
        .sign(authority, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


@dataclass(frozen=True)
class SyntheticNode:
    node_id: str
    plant_id: str
    zone_id: str
    credential: Path
    configuration: Path


def _synthetic_node(root: Path, plant_id: str, *, credential: str = "valid") -> SyntheticNode:
    """Un nodo de la flota con su credencial: ``valid`` (generada, vigente un año), ``pem``
    (archivo con el formato del almacén y un PEM sintético mal formado) o ``absent``."""
    node_id, organization_id = str(uuid.uuid4()), str(uuid.uuid4())
    directory = root / node_id
    directory.mkdir(mode=0o700, parents=True)
    path = directory / "credential.json"
    if credential == "valid":
        identity = NodeIdentity(node_id, organization_id, plant_id)
        client, server = NodeKey.generate(), NodeKey.generate()
        expires = soak.WALL.now() + dt.timedelta(days=365)
        issued = IssuedCredential(
            _synthetic_certificate(identity, client, expires),
            _synthetic_certificate(identity, server, expires),
            "",
            expires,
            client,
            server,
        )
        FileCredentialStore(path).save(StoredCredential(identity, issued, ()))
    elif credential == "pem":
        document = {
            "format": "vigia-node-credential/1",
            "node_id": node_id,
            "organization_id": organization_id,
            "plant_id": plant_id,
            "current": {
                "certificate": SYNTHETIC_PEM,
                "server_certificate_pem": SYNTHETIC_PEM,
                "ca_chain": SYNTHETIC_PEM,
                "expires_at": "2027-10-17T00:00:00.000Z",
                "client_key": SYNTHETIC_PEM,
                "server_key": SYNTHETIC_PEM,
            },
            "previous": None,
            "rotated_at": None,
            "platform_public_keys": [],
            "key_set_issued_at": None,
            "key_set_sha256": None,
        }
        path.write_text(json.dumps(document), encoding="utf-8")
        path.chmod(0o600)
    configuration = directory / "initial-configuration.json"
    configuration.write_text(json.dumps({"zones": SYNTHETIC_PEM}), encoding="utf-8")
    return SyntheticNode(node_id, plant_id, str(uuid.uuid4()), path, configuration)


def _synthetic_fleet(
    root: Path, nodes: list[SyntheticNode], records: list[dict[str, str]] | None
) -> Path:
    verify = root / "raiz.pem"
    verify.write_text("raíz sintética del entorno\n", encoding="utf-8")
    document: dict[str, Any] = {
        "ingest_base_url": "https://staging-0-nodes.example.invalid/api/nodes",
        "verify": str(verify),
        "nodes": [
            {
                "index": index,
                "node_id": node.node_id,
                "plant_id": node.plant_id,
                "credential": str(node.credential),
                "configuration": str(node.configuration),
                "catalogs": [{"zone_id": node.zone_id}],
            }
            for index, node in enumerate(nodes)
        ],
    }
    if records is not None:
        document["commissioning_records"] = records
    fleet = root / "fleet.json"
    fleet.write_text(json.dumps(document), encoding="utf-8")
    return fleet


def _accept_any_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """La configuración inicial real va firmada por la plataforma: estas pruebas de la flota la
    dan por buena (su validación tiene la suya, abajo)."""
    monkeypatch.setattr(
        soak, "NodeInitialConfiguration", SimpleNamespace(model_validate_json=lambda _: None)
    )


@pytest.mark.parametrize("credential", ["pem", "valid"])
def test_a_malformed_credential_or_configuration_exits_two_without_any_piece_of_its_value(
    credential: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    # `pem`: la credencial tiene el formato del almacén y un PEM mal formado (el error de
    # `cryptography` lo encadena); `valid`: la credencial carga y la configuración inicial lleva el
    # PEM donde va una lista (el error de Pydantic lo cita en `input_value`).
    _no_network(monkeypatch)
    plant = str(uuid.uuid4())
    node = _synthetic_node(tmp_path / "nodos", plant, credential=credential)
    record = {"plant_id": plant, "record_id": str(uuid.uuid4())}
    fleet = _synthetic_fleet(tmp_path, [node], [record])
    code = soak.main(["--dry-run", "--target", SOAK_TARGET, "--seed", "5", "--fleet", str(fleet)])
    captured = capsys.readouterr()
    assert code == soak.EXIT_USAGE == 2, captured.err
    what = "la credencial" if credential == "pem" else "la configuración inicial"
    assert captured.err.startswith(f"error: nodes[0]: {what} no se pudo leer"), captured.err
    assert "Traceback" not in captured.err
    assert _pem_fragments(captured.err + captured.out) == []
    assert not secret_findings({"stderr": captured.err})


@pytest.mark.parametrize("credential", ["pem", "valid"])
def test_a_malformed_credential_from_a_subprocess_exits_two_without_a_traceback(
    credential: str, tmp_path: Path
) -> None:
    # Como lo lanza `release.yml`: `python tests/load/soak.py`, con su propio stderr (antes, el
    # traceback de Pydantic citaba el PEM en `input_value`).
    plant = str(uuid.uuid4())
    node = _synthetic_node(tmp_path / "nodos", plant, credential=credential)
    fleet = _synthetic_fleet(
        tmp_path, [node], [{"plant_id": plant, "record_id": str(uuid.uuid4())}]
    )
    backend = Path(__file__).resolve().parents[2]
    finished = subprocess.run(
        [
            sys.executable,
            str(backend / "tests" / "load" / "soak.py"),
            "--dry-run",
            "--target",
            SOAK_TARGET,
            "--seed",
            "5",
            "--fleet",
            str(fleet),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=backend,
        timeout=SUBPROCESS_SECONDS,
    )
    assert finished.returncode == 2, finished.stderr
    assert "Traceback" not in finished.stderr
    assert _pem_fragments(finished.stderr + finished.stdout) == []


def test_a_fleet_whose_entries_are_not_what_the_soak_reads_exits_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _no_network(monkeypatch)
    _accept_any_configuration(monkeypatch)
    plant = str(uuid.uuid4())
    node = _synthetic_node(tmp_path / "nodos", plant)
    fleet = _synthetic_fleet(tmp_path, [node], [{"plant_id": plant, "record_id": "x"}])
    document = json.loads(fleet.read_text())
    variants: list[tuple[Any, str]] = [
        ([], "--fleet no es un objeto"),
        ({**document, "nodes": [SYNTHETIC_PEM]}, "nodes[0] no es un objeto"),
        ({**document, "nodes": [{**document["nodes"][0], "node_id": 7}]}, "node_id no es un UUID"),
        ({**document, "nodes": [{**document["nodes"][0], "credential": None}]}, "la credencial"),
        ({**document, "nodes": [{**document["nodes"][0], "catalogs": [{}]}]}, "zone_id no es"),
        (document, "commissioning_records[0].record_id no es un UUID"),
    ]
    for variant, expected in variants:
        fleet.write_text(json.dumps(variant))
        code = soak.main(["--dry-run", "--target", SOAK_TARGET, "--fleet", str(fleet)])
        err = capsys.readouterr().err
        assert code == 2 and expected in err, (expected, err)
        assert _pem_fragments(err) == []


@pytest.mark.parametrize("case", ["absent", "empty", "one_plant_only", "foreign_plant"])
def test_the_soak_refuses_a_fleet_without_a_commissioning_record_per_plant(
    case: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _no_network(monkeypatch)
    _accept_any_configuration(monkeypatch)
    plants = [str(uuid.uuid4()), str(uuid.uuid4())]
    nodes = [_synthetic_node(tmp_path / "nodos", plant) for plant in plants]
    every = [{"plant_id": plant, "record_id": str(uuid.uuid4())} for plant in plants]
    foreign = {"plant_id": str(uuid.uuid4()), "record_id": str(uuid.uuid4())}
    records, expected = {
        "absent": (None, "la flota no tiene actas de comisionamiento"),
        "empty": ([], "la flota no tiene actas de comisionamiento"),
        "one_plant_only": (every[:1], "1 planta(s) de la flota sin acta de comisionamiento"),
        "foreign_plant": ([*every, foreign], "el acta es de una planta que no está en la flota"),
    }[case]
    fleet = _synthetic_fleet(tmp_path, nodes, records)
    code = soak.main(["--dry-run", "--target", SOAK_TARGET, "--fleet", str(fleet)])
    assert code == 2
    assert expected in capsys.readouterr().err


def test_the_soak_dry_run_validates_the_fleet_its_records_and_the_console_session(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _no_network(monkeypatch)
    _accept_any_configuration(monkeypatch)
    plants = [str(uuid.uuid4()), str(uuid.uuid4())]
    nodes = [_synthetic_node(tmp_path / "nodos", plant) for plant in [*plants, plants[0]]]
    records = [{"plant_id": plant, "record_id": str(uuid.uuid4())} for plant in plants]
    fleet = _synthetic_fleet(tmp_path, nodes, records)
    session = tmp_path / "sesion.json"
    cookie = "cookie-sintetica-" + uuid.uuid4().hex
    session.write_text(json.dumps({"cookie": cookie, "concession_id": str(uuid.uuid4())}))
    arguments = ["--dry-run", "--target", SOAK_TARGET, "--seed", "5", "--hours", "0.5"]
    code = soak.main([*arguments, "--fleet", str(fleet), "--console-session", str(session)])
    out = capsys.readouterr().out
    assert code == 0, out
    document = json.loads(out[: out.rindex("}") + 1])
    assert document["provisioning"]["status"] == "valid"
    assert document["provisioning"]["nodes"] == 3 and document["provisioning"]["plants"] == 2
    assert document["provisioning"]["commissioning_records"] == 2
    assert document["console_session"] == "valid"
    required = document["absolute_thresholds"]["required_node_routes"]
    assert soak.OBSERVABILITY_ROUTE not in required  # media hora: no llega a la hora 1
    assert cookie not in out


@pytest.mark.parametrize(
    "content",
    [
        "{",
        "[]",
        json.dumps({"concession_id": "0190f5f2-0000-7000-8000-000000000001"}),
        json.dumps({"cookie": "galleta-sintetica-7f3a"}),
        json.dumps({"cookie": "galleta-sintetica-7f3a", "concession_id": "no-es-un-uuid"}),
    ],
    ids=["ilegible", "lista", "sin_cookie", "sin_concesion", "concesion_invalida"],
)
def test_a_malformed_console_session_exits_two_without_its_cookie(
    content: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _no_network(monkeypatch)
    session = tmp_path / "sesion.json"
    session.write_text(content)
    code = soak.main(["--dry-run", "--target", SOAK_TARGET, "--console-session", str(session)])
    err = capsys.readouterr().err
    assert code == 2 and err.startswith("error: --console-session"), err
    assert "galleta-sintetica-7f3a" not in err


@pytest.mark.parametrize("seed", ["once", "-1", str(2**53)])
def test_a_malformed_seed_variable_exits_two(
    seed: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_network(monkeypatch)
    monkeypatch.setenv(soak.SEED_VARIABLE, seed)
    assert soak.main(["--dry-run", "--target", SOAK_TARGET]) == 2
    assert soak.SEED_VARIABLE in capsys.readouterr().err


class _ConsoleStarted(Exception):
    """Corta la ejecución real del soak en cuanto arranca la consola (sin red ni nodos)."""


def test_the_soak_run_gives_the_console_the_commissioning_records_of_the_fleet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # VIG-259: `_run` construía `ConsoleTargets` sin `record_ids` y la consola leía un acta
    # inventada (404 en staging).
    from tests.load import console_client

    _no_network(monkeypatch)
    _accept_any_configuration(monkeypatch)
    plants = [str(uuid.uuid4()), str(uuid.uuid4())]
    nodes = [_synthetic_node(tmp_path / "nodos", plant) for plant in plants]
    records = [{"plant_id": plant, "record_id": str(uuid.uuid4())} for plant in plants]
    fleet = _synthetic_fleet(tmp_path, nodes, records)
    session = tmp_path / "sesion.json"
    session.write_text(json.dumps({"cookie": "c", "concession_id": str(uuid.uuid4())}))
    seen: list[Any] = []

    class Recorder:
        def __init__(self, *arguments: Any, **_: Any) -> None:
            seen.append(arguments)

        def start(self) -> None:
            raise _ConsoleStarted

    monkeypatch.setattr(console_client, "ConsoleClient", Recorder)
    arguments = ["--target", SOAK_TARGET, "--seed", "5", "--hours", "0.5"]
    with pytest.raises(_ConsoleStarted):
        soak.main([*arguments, "--fleet", str(fleet), "--console-session", str(session)])
    ((_, _, console_session, targets, seed),) = seen
    assert targets.record_ids == tuple(uuid.UUID(record["record_id"]) for record in records)
    assert set(targets.plant_ids) == {uuid.UUID(plant) for plant in plants}
    assert targets.node_ids == tuple(uuid.UUID(node.node_id) for node in nodes)
    assert targets.zone_ids == tuple(uuid.UUID(node.zone_id) for node in nodes)
    assert console_session.cookie_value == "c" and seed == 5


def test_the_console_client_refuses_to_measure_the_record_route_without_records(
    tmp_path: Path,
) -> None:
    from tests.load.console_client import ConsoleClient, ConsoleTargets
    from tests.load.provision import ConsoleSession

    one = (uuid.uuid4(),)
    session = ConsoleSession("c", uuid.uuid4())
    with pytest.raises(ValueError, match="record_ids"):
        ConsoleClient("https://app.invalid", tmp_path, session, ConsoleTargets(one, one, one), 1)
    measured = ConsoleClient(
        "https://app.invalid", tmp_path, session, ConsoleTargets(one, one, one, one), 1
    )
    assert "GET /commissioning-records/{record_id}" not in measured.unpublished
    without_record = [
        r for r in CONSOLE_ROUTES if r.name != "GET /commissioning-records/{record_id}"
    ]
    ConsoleClient(
        "https://app.invalid", tmp_path, session, ConsoleTargets(one, one, one), 1, without_record
    )


def test_soak_thresholds_name_each_route_over_its_absolute_target() -> None:
    analysis = {
        "node_latency": {
            "POST /api/nodes/findings": {"count": 10, "p95_ms": 520.0, "p99_ms": 900.0},
            "POST /api/nodes/heartbeats": {"count": 10, "p95_ms": 150.0, "p99_ms": 400.0},
        },
        "rejection_codes": {},
        "events": {},
    }
    console = {"failures": ["GET /fleet/nodes: p95 600 ms > 500 ms (n=40)"]}
    assert soak.evaluate_thresholds(analysis, console, required=()) == [
        "NFR-GOB-01 POST /api/nodes/findings: p95_ms 520.0 > 500",
        "NFR-GOB-03 GET /fleet/nodes: p95 600 ms > 500 ms (n=40)",
    ]


def _every_node_route_within_target() -> dict[str, dict[str, Any]]:
    return {
        route: {"count": 10, "p95_ms": limits["p95_ms"], "p99_ms": limits.get("p99_ms")}
        for route, limits in ABSOLUTE_TARGETS["NFR-GOB-01"].items()
    }


def test_soak_thresholds_fail_a_required_node_route_without_samples() -> None:
    required = soak.required_node_routes(PROFILES["soak"], 11)
    latency = _every_node_route_within_target()
    clean = {"node_latency": latency, "rejection_codes": {}, "events": {}}
    assert soak.evaluate_thresholds(clean, {"failures": []}, required=required) == []
    for route in required:
        for empty in (None, {"count": 0, "p95_ms": None, "p99_ms": None}):
            measured = {name: value for name, value in latency.items() if name != route}
            if empty is not None:
                measured[route] = empty
            analysis = {**clean, "node_latency": measured}
            failures = soak.evaluate_thresholds(analysis, {"failures": []}, required=required)
            assert failures == [f"NFR-GOB-01 {route}: sin muestras"]


def test_soak_thresholds_do_not_require_the_routes_the_soak_does_not_exercise() -> None:
    required = soak.required_node_routes(PROFILES["soak"], 11)
    assert set(required) == set(ABSOLUTE_TARGETS["NFR-GOB-01"]) - set(soak.NOT_MEASURED_NODE_ROUTES)
    assert set(soak.NOT_MEASURED_NODE_ROUTES) == {
        "POST /api/nodes/enrollment",
        "GET /api/nodes/zones/{zone_id}/catalog",
    }
    latency = {
        route: value
        for route, value in _every_node_route_within_target().items()
        if route not in soak.NOT_MEASURED_NODE_ROUTES
    }
    analysis = {"node_latency": latency, "rejection_codes": {}, "events": {}}
    assert soak.evaluate_thresholds(analysis, {"failures": []}, required=required) == []
    # Si aun así se midieron, sus objetivos siguen valiendo.
    slow = {**latency, "GET /api/nodes/zones/{zone_id}/catalog": {"count": 3, "p95_ms": 101.0}}
    assert soak.evaluate_thresholds(
        {**analysis, "node_latency": slow}, {"failures": []}, required=required
    ) == ["NFR-GOB-01 GET /api/nodes/zones/{zone_id}/catalog: p95_ms 101.0 > 100"]


@pytest.mark.parametrize(
    ("hours", "observes"), [(8.0, True), (1.01, True), (1.0, False), (0.5, False)]
)
def test_the_observability_route_is_required_once_the_run_reaches_the_first_degradation(
    hours: float, observes: bool
) -> None:
    # La primera degradación programada es a la hora 1 (`simulation_profile`): una ejecución que
    # no llega no emite eventos de observabilidad, y exigirlos la haría fallar siempre.
    profile = replace(PROFILES["soak"], steady_minutes=hours * 60)
    required = soak.required_node_routes(profile, 11)
    assert (soak.OBSERVABILITY_ROUTE in required) is observes
    assert "POST /api/nodes/findings" in required and "POST /api/nodes/heartbeats" in required


def test_soak_thresholds_block_on_a_single_saturation_transient() -> None:
    # A-65: NFR-GOB-02 bloquea en el soak sobre staging; un solo `temporarily_unavailable` (o un
    # 503 visto como `unavailable`) es un incumplimiento, aunque todo lo demás esté en objetivo.
    analysis = {
        "node_latency": {},
        "rejection_codes": {"heartbeat:temporarily_unavailable": 1},
        "events": {},
    }
    assert soak.evaluate_thresholds(analysis, {"failures": []}, required=()) == [
        "NFR-GOB-02 transitorios por saturación: {'heartbeat:temporarily_unavailable': 1}"
    ]
    clean = {"node_latency": {}, "rejection_codes": {}, "events": {"submit_record:success": 10}}
    assert soak.evaluate_thresholds(clean, {"failures": []}, required=()) == []


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


# --- Latidos frente al cubo de 4 por minuto (VIG-190) ---------------------------------------------


class _LoopClock:
    """``SystemClock`` sobre el reloj del bucle (virtual con ``run_virtual``)."""

    def now(self) -> dt.datetime:
        return T0 + dt.timedelta(seconds=asyncio.get_running_loop().time())

    def monotonic(self) -> float:
        return asyncio.get_running_loop().time()


class _Outbox:
    def records(self, state: object) -> list[Any]:
        return []


class _Client:
    """El cliente del nodo reducido a lo que usa ``run_node``: cada latido pasa por el
    ``LoadTransport`` real. ``busy`` es un vaciado: a los ``busy[0]`` s el primer ``run_once``
    tarda ``busy[1]`` s y, si ``planner_beat_after`` no es ``None``, el planificador del cliente
    late por su cuenta a mitad (como ``SendPlanner`` cuando pasa su intervalo sin latido)."""

    halted_reason = None

    def __init__(
        self,
        transport: LoadTransport,
        *,
        busy: tuple[float, float] | None,
        planner_beat_after: float | None = None,
    ) -> None:
        self.transport = transport
        self.busy = busy
        self.planner_beat_after = planner_beat_after

    async def heartbeat_now(self) -> None:
        request = httpx.Request("POST", "https://127.0.0.1/api/nodes/heartbeats", content=b"{}")
        await self.transport.handle_async_request(request)

    async def run_once(self) -> None:
        if self.busy is None or asyncio.get_running_loop().time() < self.busy[0]:
            return
        _, seconds = self.busy
        self.busy = None
        if self.planner_beat_after is None:
            await asyncio.sleep(seconds)
            return
        await asyncio.sleep(self.planner_beat_after)
        await self.heartbeat_now()
        await asyncio.sleep(seconds - self.planner_beat_after)


def _heartbeats(
    *, busy: tuple[float, float] | None, planner_beat_after: float | None = None
) -> tuple[Counter[int], Stats]:
    """``run_node`` con solo latidos (cada 15 s durante 5 min) contra el cubo real del latido de
    la plataforma (``NODE_BUDGETS``, 4 por minuto y nodo); devuelve los estados y ``Stats``."""
    statuses: Counter[int] = Counter()
    budget = NODE_BUDGETS[NodeRoute.HEARTBEAT]
    key = node_key(uuid.UUID(int=1), budget.operation)

    async def scenario() -> Stats:
        clock = _LoopClock()
        limiter = RateLimiter(cast(Any, clock))

        def platform(request: httpx.Request) -> httpx.Response:
            status = 200 if isinstance(limiter.check(key, budget.budget), Allowed) else 429
            statuses[status] += 1
            return httpx.Response(status, stream=httpx.ByteStream(b"{}"))

        stats = Stats()
        transport = LoadTransport(
            httpx.MockTransport(platform), Reachability(cast(Any, clock)), stats
        )
        client = _Client(transport, busy=busy, planner_beat_after=planner_beat_after)
        node = SimpleNamespace(client=client, outbox=_Outbox(), clip_records={}, index=0)
        run = NodeRun(
            cast(Any, node), "planta", _timeline([], 300_000, 15_000), cast(Any, None), transport
        )
        await run_node(
            run,
            {},
            cast(Any, None),
            stats,
            cast(Any, clock),
            start=clock.now(),
            speed_factor=1.0,
            heartbeat_seconds=HEARTBEAT_FLOOR_SECONDS,
            flush_seconds=0.0,
        )
        return stats

    return statuses, run_virtual(scenario(), cap_seconds=3_600)


def test_overdue_heartbeats_after_a_long_drain_never_burst_into_rate_limited() -> None:
    # Run 38348603446: tras la reconexión masiva un nodo pasa más de un minuto vaciando su cola,
    # su planificador late a mitad y, al volver, los latidos vencidos del calendario salían en
    # ráfaga contra el cubo de 4 por minuto (``{'heartbeat': 1}``). Ahora se omiten.
    statuses, stats = _heartbeats(busy=(60.0, 80.0), planner_beat_after=30.0)
    assert statuses[429] == 0, statuses
    assert stats.heartbeats_skipped > 0
    assert statuses[200] >= 16  # sin hueco de más de un intervalo fuera del vaciado


def test_the_heartbeat_keeps_its_fifteen_second_rhythm_when_the_node_is_barely_late() -> None:
    # Un latido medio segundo tarde (dentro de ``HEARTBEAT_SLACK_SECONDS``) no hace omitir el
    # siguiente: los 20 del calendario (0, 15, …, 285 s) salen y la plataforma los acepta.
    assert HEARTBEAT_SLACK_SECONDS < HEARTBEAT_FLOOR_SECONDS
    statuses, stats = _heartbeats(busy=(60.0, 15.5))  # el de 75 s sale a 75,5 s
    assert stats.heartbeats_skipped == 0
    assert statuses == Counter({200: 20})


# --- Vaciado final frente al retroceso del cliente (VIG-192) --------------------------------------


@dataclass
class _Item:
    """Lo que ``run_node`` y ``_reconcile`` leen de un registro de la bandeja."""

    record_id: str
    next_attempt_at: dt.datetime | None
    attempts: int
    kind: RecordKind = RecordKind.DETECTION_FOR_REVIEW
    state: OutboxState = OutboxState.READY
    clips: tuple[Any, ...] = ()
    receipt: Any = None
    dead_letter_code: Any = None


@dataclass
class _ItemOutbox:
    items: list[_Item]

    def records(self, state: OutboxState) -> list[_Item]:
        return [item for item in self.items if item.state is state]


class _Journal:
    def __init__(self) -> None:
        self.responses: list[tuple[str, str]] = []

    def response(self, record: str, result: str, code: str | None = None) -> None:
        self.responses.append((record, result))

    def dead_letter(self, record: str, code: str) -> None:
        raise AssertionError("sin cola muerta en este escenario")


class _Ceiling:
    """El sorteo del retroceso siempre en lo alto de su techo."""

    def random(self) -> float:
        return 0.999


class _RetryingClient(_Client):
    """El cliente reducido a un registro ``ready`` que se presenta cuando vence su reintento: la
    plataforma responde ``503`` hasta ``saturated_until`` (s del bucle) y lo acepta después; cada
    ``503`` programa el siguiente con el ``RetryPolicy`` real del cliente (tope de 300 s, sin
    decrecer mientras siga fallando), como ``SendPlanner``."""

    def __init__(
        self, transport: LoadTransport, outbox: _ItemOutbox, sink: Sink, saturated_until: float
    ) -> None:
        super().__init__(transport, busy=None)
        self.outbox = outbox
        self.sink = sink
        self.saturated_until = saturated_until
        self.previous_ms = 0

    async def run_once(self) -> Wait | None:  # type: ignore[override]
        clock = _LoopClock()
        now = clock.now()
        item = next((i for i in self.outbox.items if i.state is OutboxState.READY), None)
        if item is None:
            return None
        if item.next_attempt_at is not None and item.next_attempt_at > now:
            return Wait(item.next_attempt_at)
        if clock.monotonic() < self.saturated_until:
            item.attempts += 1
            wait = RetryPolicy().next_wait_ms(
                item.attempts, _Ceiling(), previous_ms=self.previous_ms
            )
            self.previous_ms = wait
            item.next_attempt_at = now + dt.timedelta(milliseconds=wait)
            self.sink(ClientEvent(now, "submit_record", item.record_id, "unavailable"))
            return Wait(item.next_attempt_at)
        item.state = OutboxState.RETAINED
        self.sink(ClientEvent(now, "submit_record", item.record_id, "success"))
        return None


RECORD_ID = "0199d5a0-0000-7000-8000-000000000192"


def _flush(*, first_retry: float, saturated_until: float) -> tuple[Stats, _Item, _Journal]:
    """``run_node`` sin calendario (el vaciado empieza a los 0 s, con ``flush_seconds`` de 600 s)
    y un registro que arrastra 8 intentos de las dos caídas: su próximo reintento es a los
    ``first_retry`` s y la plataforma sigue saturada por el vaciado de la flota hasta
    ``saturated_until`` s."""

    async def scenario() -> tuple[Stats, _Item, _Journal]:
        clock = _LoopClock()
        stats = Stats()
        journal = _Journal()
        item = _Item(RECORD_ID, clock.now() + dt.timedelta(seconds=first_retry), attempts=8)
        stats.emitted_at[RECORD_ID] = (_stamp(clock.now()), 0)
        outbox = _ItemOutbox([item])
        transport = LoadTransport(
            httpx.MockTransport(lambda _: httpx.Response(200, stream=httpx.ByteStream(b"{}"))),
            Reachability(cast(Any, clock)),
            stats,
        )
        sink = Sink(cast(Any, journal), stats, 0)
        client = _RetryingClient(transport, outbox, sink, saturated_until)
        node = SimpleNamespace(client=client, outbox=outbox, clip_records={}, index=0)
        sink.node = cast(Any, node)
        run = NodeRun(cast(Any, node), "planta", [], cast(Any, None), transport)
        await run_node(
            run,
            {},
            cast(Any, journal),
            stats,
            cast(Any, clock),
            start=clock.now(),
            speed_factor=1.0,
            heartbeat_seconds=HEARTBEAT_FLOOR_SECONDS,
            flush_seconds=600.0,
        )
        return stats, item, journal

    return run_virtual(scenario(), cap_seconds=7_200)


def test_a_retry_scheduled_past_the_flush_deadline_is_still_sent() -> None:
    # Run 38371594923: el vaciado del nodo agotó sus 600 s (2759,0 s = 2159,0 + 600,0) con una
    # detección aún en la bandeja; el vaciado de la flota tras la caída de 4 h duró 486 s, y un
    # 503 a esa altura deja al registro (8 intentos acumulados) esperando ~300 s más. El nodo
    # real la habría enviado en su reintento; el arnés la contaba como perdida.
    stats, item, journal = _flush(first_retry=320.0, saturated_until=480.0)
    assert item.state is OutboxState.RETAINED, stats.unsent
    assert stats.unsent == []
    assert (RECORD_ID, "accepted") in journal.responses
    assert RECORD_ID in stats.accepted_at and stats.history == {}
    assert 600.0 < stats.flush_seconds[0] < 600.0 + FLUSH_EXTENSION_SECONDS
    assert stats.flush_extended[0] > 0


def test_a_record_still_refused_at_the_extension_limit_is_reported_unsent_without_its_id() -> None:
    # Si la plataforma nunca deja de responder 503, el vaciado no cuelga: corta en
    # ``flush_seconds + FLUSH_EXTENSION_SECONDS`` y lo que queda es pérdida, con su recorrido.
    stats, item, journal = _flush(first_retry=320.0, saturated_until=float("inf"))
    assert item.state is OutboxState.READY
    assert stats.flush_seconds[0] == pytest.approx(600.0 + FLUSH_EXTENSION_SECONDS)
    assert (RECORD_ID, "unsent") in journal.responses
    [entry] = stats.unsent
    assert entry["kind"] == "detection_for_review" and entry["state"] == "ready"
    assert entry["attempts"] > 8 and entry["emitted_s"] == 0.0
    assert entry["next_attempt_s"] > 600.0 + FLUSH_EXTENSION_SECONDS
    assert {event[2] for event in entry["history"]} == {"unavailable"}
    assert RECORD_ID not in json.dumps(stats.unsent)
    assert not secret_findings({"unsent": json.dumps(stats.unsent)})


def test_the_flush_deadline_follows_the_latest_scheduled_retry_up_to_its_limit() -> None:
    deadline, limit, grace = T0, T0 + dt.timedelta(seconds=600), dt.timedelta(seconds=30)

    def at(seconds: float | None) -> Any:
        moment = None if seconds is None else T0 + dt.timedelta(seconds=seconds)
        return _Item(RECORD_ID, moment, attempts=1)

    assert _flush_deadline([], deadline, limit, grace) == deadline
    assert _flush_deadline([at(None)], deadline, limit, grace) == deadline
    assert _flush_deadline([at(-30)], deadline, limit, grace) == deadline  # reintento + gracia
    later = _flush_deadline([at(-40), at(100)], deadline, limit, grace)
    assert later == T0 + dt.timedelta(seconds=130)
    assert _flush_deadline([at(570)], deadline, limit, grace) == limit
    assert _flush_deadline([at(900)], deadline, limit, grace) == limit
