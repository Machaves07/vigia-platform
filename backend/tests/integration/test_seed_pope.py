"""Semilla HTTP Pope contra la composición real y testcontainers (TASK-547).

Solo organización, personas, concesión y sesiones se preparan con el arnés común.
Planta y toda la semilla pasan por HTTP; no se sustituyen los servicios de catálogo o flota.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import logging
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx
import pytest
import rfc8785
from vigia_contracts.signing import KeySet, verify

from tests.conformance import platform_target
from tests.conformance.mtls_proxy import mtls_proxy, server_tls
from tests.conformance.platform_target import (
    WALL,
    InProcessApi,
    PlatformStack,
    Provisioner,
    _Site,
    _stamp,
    in_process_api,
    platform_stack,
)
from tests.fleet_credentials_support import csr_pem, local_ip
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint
from tests.resilience.harness import free_port
from tools import seed_pope
from vigia_platform.catalog.adapters.http import CATALOG_STATE_KEY, CatalogHttp
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME
from vigia_platform.shared.api.app import AppConfig, AppRuntime
from vigia_platform.shared.db import DatabaseSettings
from vigia_platform.shared.runtime import core
from vigia_platform.shared.runtime.api import compose_api_runtime
from vigia_platform.shared.runtime.config import RuntimeConfig

pytestmark = pytest.mark.integration

CATALOGS = seed_pope.CATALOG_DIRECTORY
HASHES = {
    "zone_catalog.json": "05f15ddfebd858b5c9275fa66b1c925a055a0195cf89703024788bd9cf5fdb3e",
    "zone_catalog.pope_foso.json": (
        "845f78b702b0caf11a6a0fa09bb6d98df576071798e350fcce5b79015fd4baa2"
    ),
}


@dataclass
class PopePlatform:
    stack: PlatformStack
    api: InProcessApi
    directory: Path
    ca: Path

    def site(self) -> _Site:
        return Provisioner(self.stack, self.api, self.api.url, self.ca, self.directory)._site(())


@pytest.fixture(scope="module")
def platform(
    postgres_endpoint: PostgresEndpoint,
    localstack_endpoint: LocalStackEndpoint,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[PopePlatform]:
    directory = tmp_path_factory.mktemp("pope-platform")
    tls = server_tls(directory, WALL.now())

    def database_settings(**values: Any) -> DatabaseSettings:
        return replace(
            DatabaseSettings(**values),
            connect_timeout_seconds=60.0,
            lock_timeout_ms=60_000,
            statement_timeout_ms=60_000,
            pool_timeout_seconds=60.0,
        )

    async def compose(config: AppConfig, runtime: RuntimeConfig, **values: Any) -> AppRuntime:
        built = await compose_api_runtime(config, runtime, clock=WALL, **values)
        catalog = built.state[CATALOG_STATE_KEY]
        assert isinstance(catalog, CatalogHttp)
        assert catalog.catalog is not None and catalog.gates is not None
        catalog.catalog._sign_timeout = 120.0
        catalog.gates._sign_timeout = 120.0
        return built

    with (
        pytest.MonkeyPatch.context() as patch,
        mtls_proxy("aws-pope", [localstack_endpoint.url], tls, preserve_host=True) as aws,
        platform_stack(
            postgres_endpoint,
            localstack_endpoint,
            directory,
            aws_url=aws.url,
            ca_bundle=tls.ca_file,
        ) as stack,
    ):
        # Son los servicios reales: solo se amplían topes que esta tarea no está midiendo.
        patch.setattr(core, "DatabaseSettings", database_settings)
        patch.setattr(platform_target, "compose_api_runtime", compose)
        with in_process_api(stack.environ, free_port(), log_file=directory / "api.log") as api:
            yield PopePlatform(stack, api, directory, tls.ca_file)


@dataclass
class SeedSite:
    platform: PopePlatform
    site: _Site
    plant: str
    administrator: httpx.Client
    installer: httpx.Client

    def cli(self, catalog: Path = CATALOGS / "zone_catalog.json") -> list[str]:
        return [
            "--base-url",
            self.platform.api.url,
            "--plant",
            self.plant,
            "--concession",
            str(self.site.concession_id),
            "--ca-bundle",
            str(self.platform.ca),
            "--catalog",
            str(catalog),
        ]

    def zones(self) -> dict[str, str]:
        response = self.administrator.get("/hierarchy")
        assert response.status_code == 200
        plant = next(p for p in response.json()["plants"] if p["plant_id"] == self.plant)
        return {zone["code"]: zone["zone_id"] for zone in plant["zones"]}

    def counts(self) -> dict[str, int]:
        rows = self.platform.stack.fetch(
            "SELECT 'zones' AS kind, count(*) AS n FROM identity.zone WHERE plant_id = $1"
            " UNION ALL SELECT 'cameras', count(*) FROM catalog.zone_camera WHERE plant_id = $1"
            " UNION ALL SELECT 'standards', count(*) FROM catalog.declared_standard_version"
            " WHERE plant_id = $1"
            " UNION ALL SELECT 'versions', count(*) FROM catalog.zone_catalog_version"
            " WHERE plant_id = $1"
            " UNION ALL SELECT 'nodes', count(*) FROM identity.node_identity WHERE plant_id = $1"
            " UNION ALL SELECT 'assignments', count(*) FROM identity.zone_node_assignment"
            " WHERE plant_id = $1"
            " UNION ALL SELECT 'codes', count(*) FROM fleet.enrollment_code WHERE plant_id = $1"
            " UNION ALL SELECT 'admissions', count(*) FROM catalog.family_admission"
            " WHERE plant_id = $1"
            " UNION ALL SELECT 'mounting', count(*) FROM catalog.mounting_gate_record"
            " WHERE plant_id = $1"
            " UNION ALL SELECT 'documents', count(*) FROM catalog.document_upload_grant"
            " WHERE plant_id = $1"
            " UNION ALL SELECT 'records', count(*) FROM ledger.ledger_record WHERE plant_id = $1",
            uuid.UUID(self.plant),
        )
        return {row["kind"]: row["n"] for row in rows}


@pytest.fixture
def site(platform: PopePlatform, monkeypatch: pytest.MonkeyPatch) -> Iterator[SeedSite]:
    setup = platform.site()
    monkeypatch.setenv(seed_pope.ADMIN_SESSION, setup.administrator.value)
    monkeypatch.setenv(seed_pope.INSTALLER_SESSION, setup.installer.value)
    with (
        httpx.Client(
            base_url=platform.api.url,
            headers={
                "Cookie": f"{SESSION_COOKIE_NAME}={setup.administrator.value}",
                "Sec-Fetch-Site": "same-origin",
            },
            timeout=60.0,
        ) as administrator,
        httpx.Client(
            base_url=platform.api.url,
            headers={
                "Cookie": f"{SESSION_COOKIE_NAME}={setup.installer.value}",
                "Sec-Fetch-Site": "same-origin",
                "X-Vigia-Concession": str(setup.concession_id),
            },
            timeout=60.0,
        ) as installer,
    ):
        response = administrator.post(
            "/plants",
            json={
                "code": "POPE-TEST",
                "name": "Planta sintética Pope",
                "country": "CO",
                "data_region": "us-east-1",
                "timezone": "America/Bogota",
            },
        )
        assert response.status_code == 201, response.text
        yield SeedSite(platform, setup, response.json()["plant_id"], administrator, installer)


def _events(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    captured = capsys.readouterr()
    assert captured.err == ""
    return [json.loads(line) for line in captured.out.splitlines()]


def _expected(source: dict[str, Any]) -> bytes:
    """Oráculo independiente: lista cerrada de campos asignados, no reutiliza el comparador."""
    source = copy.deepcopy(source)
    for field in ("zone_id", "plant_id", "organization_id", "version", "issued_at"):
        del source[field]
    for standard in source["standards"]:
        for field in ("standard_id", "version", "declared_by", "effective_from"):
            del standard[field]
    return rfc8785.dumps(source)


def test_catalogs_and_simulated_enrollment_match_the_source(
    site: SeedSite,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    caplog.set_level(logging.INFO)
    assert seed_pope.main(site.cli()) == 0
    events = _events(capsys)
    assert [event["event"] for event in events] == ["enrollment_code", "seeded"]
    issued, seeded = events
    node = seeded["node_id"]
    sources = seed_pope.load_catalogs(CATALOGS / "zone_catalog.json")
    zones = site.zones()
    assert set(zones) == {"POPE", "POPE-FOSO"}
    envelopes = {}
    for source in sources:
        zone = zones[source["zone_code"]]
        current = site.administrator.get(f"/zones/{zone}/catalog")
        assert current.status_code == 200, current.text
        catalog = current.json()["catalog"]
        detail = site.administrator.get(f"/zones/{zone}/catalog/versions/{catalog['version']}")
        assert detail.status_code == 200
        envelope = detail.json()["envelope"]
        assert envelope["payload"] == catalog
        assert _expected(envelope["payload"]) == _expected(source)
        assert all("stream_reference" not in camera for camera in envelope["payload"]["cameras"])
        envelopes[zone] = envelope
        gates = site.installer.get(f"/zones/{zone}/gates").json()
        assert (gates["mounting"]["status"], gates["usage"]["status"], gates["resulting_mode"]) == (
            "approved",
            "pending",
            "commissioning",
        )
    inventory = site.installer.get(f"/fleet/nodes/{node}").json()["node"]
    assert inventory["status"] == "declared"
    assert set(inventory["zones"]) == set(zones.values())
    counts = site.counts()
    assert {
        key: counts[key]
        for key in (
            "zones",
            "cameras",
            "standards",
            "nodes",
            "codes",
            "admissions",
            "mounting",
            "documents",
        )
    } == {
        "zones": 2,
        "cameras": 4,
        "standards": 6,
        "nodes": 1,
        "codes": 1,
        "admissions": 4,
        "mounting": 2,
        "documents": 2,
    }

    # El alta real acepta el código y transporta los sobres y stream_reference por separado.
    response = httpx.post(
        site.platform.api.url + "/api/nodes/enrollment",
        json={
            "enrollment_code": issued["code"],
            "key_algorithm": "ecdsa_p256",
            "certificate_signing_request": csr_pem(node),
            "server_certificate_signing_request": csr_pem(node, names=[local_ip()]),
            "software_version": "1.0.0",
            "contract_version": "1.1.0",
            "hardware_fingerprint": "1" * 64,
            "requested_at": _stamp(WALL.now()),
        },
        headers={"X-Vigia-Contract-Version": "1.1.0"},
        timeout=60.0,
    )
    assert response.status_code == 200, response.text
    configuration = response.json()["initial_configuration"]
    keyset = KeySet(WALL)
    keyset.pin_initial(response.json()["platform_public_keys"])
    for envelope in configuration["zones"]:
        assert verify(envelope, keyset, "catalog", WALL) == envelope["payload"]
    for envelope in configuration["gate_states"]:
        assert verify(envelope, keyset, "gate", WALL) == envelope["payload"]
    assert {item["payload"]["zone_id"]: item for item in configuration["zones"]} == envelopes
    assert {item["payload"]["resulting_mode"] for item in configuration["gate_states"]} == {
        "commissioning"
    }
    assert {camera["code"]: camera["stream_reference"] for camera in configuration["cameras"]} == {
        "CAM100": "cam100",
        "CAM101": "cam101",
        "CAM102": "cam102",
    }
    assert len({camera["camera_id"] for camera in configuration["cameras"]}) == 3

    # No imprimir valores secretos al fallar esta búsqueda, ni siquiera con introspección pytest.
    needle = issued["code"].encode()
    for root in (tmp_path, site.platform.directory):
        for path in root.rglob("*"):
            if path.is_file() and needle in path.read_bytes():
                pytest.fail("Código de alta escrito en un archivo generado", pytrace=False)
    if needle in caplog.text.encode():
        pytest.fail("Código de alta escrito en los registros", pytrace=False)


def test_second_execution_creates_nothing_and_does_not_disclose_a_code(
    site: SeedSite,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert seed_pope.main(site.cli()) == 0
    _events(capsys)
    before = site.counts()
    assert seed_pope.main(site.cli()) == 0
    events = _events(capsys)
    assert site.counts() == before
    assert len(events) == 1
    assert events[0]["event"] == "unchanged"
    assert events[0]["message_es"] == "sin cambios"
    assert events[0]["writes"] == 0


def test_unsatisfiable_coverage_stops_before_the_next_zone_and_node(
    site: SeedSite,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    source = json.loads((CATALOGS / "zone_catalog.json").read_bytes())
    source["minimum_coverage"] = {
        "required_count": 1,
        "required_camera_ids": [c["camera_id"] for c in source["cameras"][:2]],
    }
    catalog = tmp_path / "zone_catalog.json"
    catalog.write_text(json.dumps(source), encoding="utf-8")
    (tmp_path / "zone_catalog.pope_foso.json").write_bytes(
        (CATALOGS / "zone_catalog.pope_foso.json").read_bytes()
    )
    exit_code = seed_pope.main(site.cli(catalog))
    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert set(site.zones()) == {"POPE"}
    assert captured.err == "unsatisfiable_coverage: la plataforma rechazó la cobertura\n"
    counts = site.counts()
    assert all(
        counts[key] == 0
        for key in ("cameras", "standards", "versions", "nodes", "codes", "mounting", "documents")
    )
    # Es el rechazo real HTTP A-03, no una comprobación anticipada del script.
    zone = site.zones()["POPE"]
    body = {
        key: source["standards"][0][key]
        for key in ("family", "title_es", "declared_text", "predicate")
    }
    body["reason_es"] = seed_pope.REASON
    body["zone_parameters"] = {key: source[key] for key in seed_pope.PARAMETERS}
    body["zone_parameters"]["cameras"] = [
        {**c, "stream_reference": c["code"].lower()} for c in source["cameras"]
    ]
    rejected = site.administrator.post(f"/zones/{zone}/standards", json=body)
    assert rejected.status_code == 400
    assert rejected.json()["detail_code"] == "catalog_unsatisfiable_coverage"


def test_dry_run_has_no_network_or_writes(
    site: SeedSite,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    before = site.counts()
    with monkeypatch.context() as patch:
        patch.chdir(tmp_path)

        def forbidden(*args: Any, **kwargs: Any) -> Any:
            pytest.fail("El plan no debe abrir clientes HTTP")

        patch.setattr(httpx, "Client", forbidden)
        assert seed_pope.main(["--dry-run", "--catalog", str(CATALOGS / "zone_catalog.json")]) == 0
    (plan,) = _events(capsys)
    assert plan["event"] == "plan"
    assert plan["zones"] == [
        {"code": "POPE", "cameras": ["CAM100", "CAM101", "CAM102"], "standards": 5},
        {"code": "POPE-FOSO", "cameras": ["CAM102"], "standards": 1},
    ]
    assert list(tmp_path.iterdir()) == []
    assert site.counts() == before


def test_comparison_is_rfc8785_not_serialization_and_preserves_content() -> None:
    source = json.loads((CATALOGS / "zone_catalog.json").read_bytes())
    reordered = dict(reversed(list(copy.deepcopy(source).items())))
    reordered["cameras"][0]["declared_min_fps"] = 8
    assert seed_pope.comparable_catalog(reordered) == seed_pope.comparable_catalog(source)
    assert seed_pope.comparable_catalog(reordered) == _expected(source)
    changed = copy.deepcopy(source)
    changed["signals"][0]["signal_id"] = str(uuid.uuid4())
    assert seed_pope.comparable_catalog(changed) != seed_pope.comparable_catalog(source)
    changed = copy.deepcopy(source)
    changed["cameras"][0]["stream_reference"] = "cam100"
    assert seed_pope.comparable_catalog(changed) != seed_pope.comparable_catalog(source)


@pytest.mark.parametrize("name", tuple(HASHES))
def test_copied_catalog_has_the_recorded_hash(name: str) -> None:
    assert hashlib.sha256((CATALOGS / name).read_bytes()).hexdigest() == HASHES[name]


@pytest.mark.parametrize("name", tuple(HASHES))
def test_catalog_hash_matches_vigia_nodo_when_available(name: str) -> None:
    original = os.environ.get("VIGIA_NODO_SRC")
    if original is None:
        pytest.skip("VIGIA_NODO_SRC no está disponible")
    source = Path(original) / "tests/fixtures/pope" / name
    assert hashlib.sha256(source.read_bytes()).hexdigest() == HASHES[name]
    assert (CATALOGS / name).read_bytes() == source.read_bytes()


def test_unknown_plant_does_not_write(site: SeedSite, capsys: pytest.CaptureFixture[str]) -> None:
    before = site.counts()
    arguments = site.cli()
    arguments[arguments.index("--plant") + 1] = str(uuid.uuid4())
    assert seed_pope.main(arguments) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "La planta no está al alcance de la sesión administradora\n"
    assert site.counts() == before


def test_same_node_code_in_another_plant_is_not_reused(
    site: SeedSite,
    capsys: pytest.CaptureFixture[str],
) -> None:
    other = site.administrator.post(
        "/plants",
        json={
            "code": "OTHER",
            "name": "Otra planta sintética",
            "country": "CO",
            "data_region": "us-east-1",
            "timezone": "America/Bogota",
        },
    )
    assert other.status_code == 201
    foreign = site.installer.post(
        f"/plants/{other.json()['plant_id']}/nodes",
        json={"code": seed_pope.NODE_CODE, "zone_ids": []},
    )
    assert foreign.status_code == 201
    assert seed_pope.main(site.cli()) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "La plataforma rechazó una operación HTTP (409)\n"
    assert site.counts()["nodes"] == 0
    unchanged = site.installer.get(f"/fleet/nodes/{foreign.json()['node_id']}").json()["node"]
    assert unchanged["zones"] == [] and unchanged["status"] == "declared"


@pytest.mark.parametrize(
    "parameter", ("cameras", "minimum_coverage", "signals", "thresholds", "windows")
)
def test_parameter_changes_use_the_normal_put_routes(parameter: str) -> None:
    source = json.loads((CATALOGS / "zone_catalog.json").read_bytes())
    current = copy.deepcopy(source)
    if parameter == "cameras":
        current["cameras"][0]["declared_min_fps"] = 7
    elif parameter == "minimum_coverage":
        current["minimum_coverage"]["required_count"] = 3
    elif parameter == "signals":
        current["signals"] = []
    elif parameter == "thresholds":
        current["thresholds"]["review"] = 0.3
    else:
        current["episode"]["grouping_window_ms"] = 5000
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"catalog": current if request.method == "GET" else source})

    with httpx.Client(
        transport=httpx.MockTransport(respond), base_url="https://api.example.test", timeout=60.0
    ) as client:
        seed = seed_pope.PopeSeeder(client, client, client, io.StringIO())
        seed.publish("zone", source)
    assert [request.method for request in seen] == ["GET", "PUT"]
    assert seen[-1].url.path == "/zones/zone/" + parameter.replace("_", "-")


@pytest.mark.parametrize("change", ("standard", "unknown_field"))
def test_existing_content_is_checked_without_silently_overwriting(change: str) -> None:
    source = json.loads((CATALOGS / "zone_catalog.json").read_bytes())
    current = copy.deepcopy(source)
    if change == "standard":
        current["standards"][0]["declared_text"] = "Un estándar sintético diferente"
    else:
        current["unexpected"] = True
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(200, json={"catalog": current})

    with (
        httpx.Client(
            transport=httpx.MockTransport(respond),
            base_url="https://api.example.test",
            timeout=60.0,
        ) as client,
        pytest.raises(seed_pope.SeedError) as error,
    ):
        seed_pope.PopeSeeder(client, client, client, io.StringIO()).publish("zone", source)
    assert str(error.value) == (
        "Un estándar existente difiere del origen; no se sobrescribe"
        if change == "standard"
        else "El catálogo publicado no coincide canónicamente con el origen"
    )
    assert seen == ["GET"]


@pytest.mark.parametrize(("mounting", "usage"), (("revoked", "pending"), ("pending", "approved")))
def test_decided_gates_require_human_review(mounting: str, usage: str) -> None:
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(
            200, json={"mounting": {"status": mounting}, "usage": {"status": usage}}
        )

    with (
        httpx.Client(
            transport=httpx.MockTransport(respond),
            base_url="https://api.example.test",
            timeout=60.0,
        ) as client,
        pytest.raises(seed_pope.SeedError),
    ):
        seed_pope.PopeSeeder(client, client, client, io.StringIO()).mounting(
            "zone", "plant", {"cameras": []}
        )
    assert seen == ["GET"]


def test_failed_upload_does_not_file_the_scope_record() -> None:
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/gates"):
            return httpx.Response(
                200, json={"mounting": {"status": "pending"}, "usage": {"status": "pending"}}
            )
        if request.url.path == "/documents":
            return httpx.Response(
                201,
                json={
                    "upload": {
                        "url": "https://storage.example.test/capture",
                        "required_headers": {},
                    },
                    "document_ref": {},
                },
            )
        return httpx.Response(503)

    with (
        httpx.Client(
            transport=httpx.MockTransport(respond),
            base_url="https://api.example.test",
            timeout=60.0,
        ) as client,
        pytest.raises(seed_pope.SeedError),
    ):
        seed_pope.PopeSeeder(client, client, client, io.StringIO()).mounting(
            "zone", "plant", {"cameras": []}
        )
    assert seen == ["/zones/zone/gates", "/documents", "/capture"]


def test_admission_pagination_does_not_treat_rejections_as_admitted() -> None:
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={})
        if "after" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "admissions": [{"family": "coexistence", "result": "rejected"}],
                    "next_after": "next",
                },
            )
        return httpx.Response(
            200,
            json={"admissions": [{"family": "dwell", "result": "admitted"}], "next_after": None},
        )

    with httpx.Client(
        transport=httpx.MockTransport(respond), base_url="https://api.example.test", timeout=60.0
    ) as client:
        seed_pope.PopeSeeder(client, client, client, io.StringIO()).admissions("plant")
    assert seen[1].url.params["after"] == "next"
    assert {
        json.loads(request.content)["family"] for request in seen if request.method == "POST"
    } == {"coexistence", "guard_bypass", "startup_transition"}


@pytest.mark.parametrize("missing", ("arguments", "admin", "installer"))
def test_cli_requires_arguments_and_both_sessions(
    missing: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(seed_pope.ADMIN_SESSION, "sesion-sintetica")
    monkeypatch.setenv(seed_pope.INSTALLER_SESSION, "sesion-sintetica")
    if missing != "arguments":
        monkeypatch.delenv(
            seed_pope.ADMIN_SESSION if missing == "admin" else seed_pope.INSTALLER_SESSION
        )
    args = (
        []
        if missing == "arguments"
        else [
            "--base-url",
            "https://api.example.test",
            "--plant",
            "plant",
            "--concession",
            "concession",
        ]
    )
    assert seed_pope.main(args) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "Se requieren --base-url, --plant y --concession\n"
        if missing == "arguments"
        else (
            f"Se requieren las sesiones verificadas {seed_pope.ADMIN_SESSION}"
            f" e {seed_pope.INSTALLER_SESSION}\n"
        )
    )


def test_cli_does_not_echo_transport_errors_or_response_bodies(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "codigo-" + "sintetico-no-persistido"

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise httpx.TransportError(secret)

    monkeypatch.setattr(seed_pope, "load_catalogs", fail)
    assert seed_pope.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "No se pudo completar la semilla; revise los insumos y la conexión\n"
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(403, json={"secret": secret})),
            base_url="https://api.example.test",
            timeout=60.0,
        ) as client,
        pytest.raises(seed_pope.SeedError) as error,
    ):
        seed_pope.PopeSeeder(client, client, client, io.StringIO()).required(
            client, "POST", "/path"
        )
    assert str(error.value) == "La plataforma rechazó una operación HTTP (403)"


def test_wrong_catalog_pair_is_refused_before_network(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name in seed_pope.CATALOG_NAMES:
        document = json.loads((CATALOGS / name).read_bytes())
        document["zone_code"] = "OTHER"
        (tmp_path / name).write_text(json.dumps(document), encoding="utf-8")
    assert seed_pope.main(["--dry-run", "--catalog", str(tmp_path / "zone_catalog.json")]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Los catálogos deben corresponder a POPE y POPE-FOSO\n"


def test_required_response_cannot_be_null() -> None:
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"null")),
            base_url="https://api.example.test",
            timeout=60.0,
        ) as client,
        pytest.raises(seed_pope.SeedError),
    ):
        seed_pope.PopeSeeder(client, client, client, io.StringIO()).required(client, "GET", "/path")


def test_existing_node_with_different_zones_is_not_modified(
    site: SeedSite,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert seed_pope.main(site.cli()) == 0
    seeded = _events(capsys)[-1]
    node, zone = seeded["node_id"], seeded["zone_ids"][0]
    unassigned = site.installer.post(
        f"/nodes/{node}/zones/{zone}/unassignment", json={"reason_es": seed_pope.REASON}
    )
    assert unassigned.status_code == 200
    before = site.counts()
    assert seed_pope.main(site.cli()) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "El nodo existente no tiene exactamente las zonas Pope\n"
    assert site.counts() == before


def test_upload_does_not_forward_console_sessions() -> None:
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/gates"):
            return httpx.Response(
                200, json={"mounting": {"status": "pending"}, "usage": {"status": "pending"}}
            )
        if request.url.path == "/documents":
            return httpx.Response(
                201,
                json={
                    "upload": {
                        "url": "https://storage.example.test/capture",
                        "required_headers": {"Content-Type": "image/png"},
                    },
                    "document_ref": {},
                },
            )
        return httpx.Response(200, json={})

    with (
        httpx.Client(
            transport=httpx.MockTransport(respond),
            base_url="https://api.example.test",
            headers={
                "Cookie": "sesion-" + "sintetica",
                "X-Vigia-Concession": "concesion-sintetica",
            },
            timeout=60.0,
        ) as console,
        httpx.Client(transport=httpx.MockTransport(respond), timeout=60.0) as uploads,
    ):
        seed_pope.PopeSeeder(console, console, uploads, io.StringIO()).mounting(
            "zone", "plant", {"cameras": []}
        )
    upload = next(request for request in seen if request.url.host == "storage.example.test")
    assert "Cookie" not in upload.headers and "X-Vigia-Concession" not in upload.headers
    assert upload.headers["Content-Type"] == "image/png"
    grant = json.loads(
        next(request for request in seen if request.url.path == "/documents").content
    )
    assert hashlib.sha256(upload.content).hexdigest() == grant["sha256"]
    assert len(upload.content) == grant["size_bytes"]
