"""Alta, mTLS, latidos, evento y clip reales; fallos aislados del humo (VIG-408)."""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from vigia_contracts.client import SyncClient

from tests.conformance import platform_target
from tests.conformance.mtls_proxy import ServerTls, mtls_proxy, server_tls
from tests.conformance.platform_target import WALL, PlatformStack, in_process_api, platform_stack
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint
from tests.integration.test_compose_integration import Deployment, app_url, tls_context
from tests.integration.test_compose_integration import deployment as deployment
from tests.integration.test_compose_integration import installed as installed
from tests.integration.test_compose_integration import resolved_names as resolved_names
from tests.integration.test_seed_pope import PopePlatform, SeedSite
from tests.integration.test_seed_pope import site as site  # Fixture HTTP de VIG-394.
from tests.resilience.harness import free_port
from tools import node_enrollment_smoke as smoke
from tools import seed_pope
from vigia_platform.catalog.adapters.http import CATALOG_STATE_KEY, CatalogHttp
from vigia_platform.shared.api.app import AppConfig, AppRuntime
from vigia_platform.shared.db import DatabaseSettings
from vigia_platform.shared.runtime import core
from vigia_platform.shared.runtime.api import compose_api_runtime
from vigia_platform.shared.runtime.config import RuntimeConfig

pytestmark = pytest.mark.integration


@dataclass
class SmokePlatform(PopePlatform):
    app_url: str


@pytest.fixture(scope="module")
def infrastructure(
    postgres_endpoint: PostgresEndpoint,
    localstack_endpoint: LocalStackEndpoint,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[PlatformStack, Path, ServerTls]]:
    directory = tmp_path_factory.mktemp("smoke-platform")
    tls = server_tls(directory, WALL.now())

    def settings(**values: Any) -> DatabaseSettings:
        return replace(
            DatabaseSettings(**values),
            connect_timeout_seconds=60,
            lock_timeout_ms=60_000,
            statement_timeout_ms=60_000,
            pool_timeout_seconds=60,
        )

    async def compose(config: AppConfig, runtime: RuntimeConfig, **values: Any) -> AppRuntime:
        built = await compose_api_runtime(config, runtime, clock=WALL, **values)
        catalog = built.state[CATALOG_STATE_KEY]
        assert isinstance(catalog, CatalogHttp)
        assert catalog.catalog is not None and catalog.gates is not None
        catalog.catalog._sign_timeout = 120
        catalog.gates._sign_timeout = 120
        return built

    with (
        pytest.MonkeyPatch.context() as patch,
        mtls_proxy("aws-smoke", [localstack_endpoint.url], tls, preserve_host=True) as aws,
        platform_stack(
            postgres_endpoint,
            localstack_endpoint,
            directory,
            aws_url=aws.url,
            ca_bundle=tls.ca_file,
        ) as stack,
    ):
        patch.setattr(core, "DatabaseSettings", settings)
        patch.setattr(platform_target, "compose_api_runtime", compose)
        yield stack, directory, tls


@pytest.fixture
def platform(infrastructure: tuple[PlatformStack, Path, ServerTls]) -> Iterator[PopePlatform]:
    # Solo se renueva la API y su limitador por caso: la base y AWS se comparten.
    # Así cada prueba tiene sus cuotas reales sin alterar ni vaciar el limitador.
    stack, directory, tls = infrastructure
    node_ca = directory / "node-ca.pem"
    node_ca.write_bytes(stack.node_ca_root())
    port = free_port()
    backend = f"http://127.0.0.1:{port}"
    with mtls_proxy("nodes-smoke", [backend], tls, client_ca=node_ca) as nodes:
        stack.environ["VIGIA_NODES_BASE_URL"] = nodes.url + "/api/nodes"
        with (
            in_process_api(stack.environ, port, log_file=directory / "api.log") as api,
            mtls_proxy("app-smoke", [api.url], tls) as app,
        ):
            yield SmokePlatform(stack, api, directory, tls.ca_file, app.url)


def issued_args(site: SeedSite, capsys: pytest.CaptureFixture[str]) -> tuple[list[str], str, str]:
    assert seed_pope.main(site.cli()) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    issued, seeded = events
    code = issued["code"]
    assert isinstance(site.platform, SmokePlatform)
    args = [
        "--base-url", site.platform.app_url,
        "--code", code,
        "--node-id", seeded["node_id"],
        "--organization-id", str(site.site.organization_id),
        "--plant-id", site.plant,
        "--ca-file", str(site.platform.ca),
    ]  # fmt: skip
    return args, code, seeded["node_id"]


def output(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    captured = capsys.readouterr()
    assert captured.err == ""
    value: dict[str, Any] = json.loads(captured.out)
    return value


def test_real_enrollment_heartbeats_event_clip_and_used_code(
    site: SeedSite,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    args, code, node = issued_args(site, capsys)
    caplog.set_level(logging.DEBUG)
    exit_code = smoke.main(args, clock=WALL)
    assert exit_code == 0
    captured = capsys.readouterr()
    forbidden = [code, "PRIVATE KEY", "X-Amz-Signature", "X-Amz-Credential", "Bearer "]
    if any(needle in captured.out + captured.err + caplog.text for needle in forbidden):
        pytest.fail("El humo filtró material sensible en su salida o registros", pytrace=False)
    report = json.loads(captured.out)
    expected = {
        "enrollment": "accepted",
        "heartbeats_accepted": 3,
        "observability_event": "accepted",
        "verification_clip": "confirmed",
        "live_view": "not_requested",
        "code": "ok",
    }
    if set(report) != set(expected):
        pytest.fail("El informe contiene campos no permitidos", pytrace=False)
    assert report == expected
    inventory = site.installer.get(f"/fleet/nodes/{node}").json()["node"]
    assert inventory["communication_state"] == "reachable"
    clips = site.installer.get(f"/zones/{report_zone(site)}/commissioning-clips").json()
    assert len(clips["clips"]) == 1
    # En enrolled la API enmascara cualquier código como inválido (verify, BR-GOB-58).
    # Abrir una re-alta autorizada permite observar el rechazo específico del código usado.
    revoked = site.installer.post(
        f"/nodes/{node}/revocation", json={"reason_es": "Re-alta sintética de verificación"}
    )
    assert revoked.status_code == 200
    reopened = site.installer.post(f"/nodes/{node}/enrollment-codes")
    assert reopened.status_code == 201
    exit_code = smoke.main(args, clock=WALL)
    assert exit_code != 0
    reused = capsys.readouterr()
    assert json.loads(reused.out)["code"] == "enrollment_code_used"
    text = captured.out + captured.err + reused.out + reused.err + caplog.text
    # Fallar sin que la introspección de pytest revele el valor que se está buscando.
    if any(needle in text for needle in forbidden):
        pytest.fail("El humo filtró material sensible en su salida o registros", pytrace=False)


def report_zone(site: SeedSite) -> str:
    return site.zones()["POPE"]


def test_rejected_heartbeat_is_not_success(
    site: SeedSite, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    args, _, _ = issued_args(site, capsys)
    # Se rechaza solo el latido; alta, firma, certificados y catálogo siguen siendo reales.
    real = SyncClient._send

    def reject(self: Any, call: Any) -> Any:
        if call.url.endswith("/heartbeats"):
            from vigia_contracts.client._engine import Reply

            return Reply(503, b"{}", {})
        return real(self, call)

    monkeypatch.setattr(SyncClient, "_send", reject)
    exit_code = smoke.main(args, clock=WALL)
    assert exit_code == 1
    report = output(capsys)
    assert report["code"] == "heartbeat_rejected"
    assert report["heartbeats_accepted"] == 0
    assert report["verification_clip"] == "not_run"


@pytest.mark.parametrize("failure", ("grant_503", "confirmation", "receipt"))
def test_clip_failures_are_bounded_and_never_confirmed(
    failure: str,
    site: SeedSite,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args, _, _ = issued_args(site, capsys)
    real = httpx.Client.send
    grants, confirms = [], []

    def send(client: httpx.Client, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        path = request.url.path
        if path.endswith("/clip-uploads"):
            grants.append(path)
            # Tope por número de intercambios, nunca por velocidad del runner.
            if len(grants) > 1:
                raise RuntimeError("El humo reintentó el 503 de clips")
            if failure == "grant_503":
                return httpx.Response(
                    503,
                    request=request,
                    json={
                        "code": "temporarily_unavailable",
                        "message_es": "Almacén sintético no publicado",
                        "retryable": True,
                    },
                )
        if path.endswith("/confirmation"):
            confirms.append(path)
            if failure == "confirmation":
                return httpx.Response(503, request=request, json={})
        return real(client, request, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", send)
    if failure == "receipt":
        upload = SyncClient.upload_verification_clip

        def wrong_receipt(self: Any, *values: Any) -> Any:
            # Aísla la comprobación del humo incluso si un futuro cliente omite su guarda.
            return upload(self, *values).model_copy(update={"sha256": "0" * 64})

        monkeypatch.setattr(SyncClient, "upload_verification_clip", wrong_receipt)
    exit_code = smoke.main(args, clock=WALL)
    assert exit_code == (3 if failure == "grant_503" else 1)
    report = output(capsys)
    assert report["verification_clip"] == "not_run"
    assert report["heartbeats_accepted"] == 3
    assert len(grants) == 1
    assert len(confirms) == (0 if failure == "grant_503" else 1)
    assert (
        report["code"]
        == {
            "grant_503": "clip_storage_unpublished",
            "confirmation": "verification_clip_failed",
            "receipt": "verification_clip_unconfirmed",
        }[failure]
    )


def test_transport_failure_and_parser_do_not_echo_secrets(
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "codigo-" + "no-persistido"

    def fail(*args: Any, **kwargs: Any) -> None:
        raise httpx.TransportError(secret)

    monkeypatch.setattr(smoke, "exercise", fail)
    args = [
        "--base-url",
        "https://api.example.test",
        "--code",
        secret,
        "--node-id",
        "n",
        "--organization-id",
        "o",
        "--plant-id",
        "p",
    ]
    assert smoke.main(args) == 1
    captured = capsys.readouterr()
    if secret in captured.out + captured.err + caplog.text:
        pytest.fail("Se filtró el texto sensible de una excepción", pytrace=False)
    assert json.loads(captured.out)["code"] == "smoke_failed"
    with pytest.raises(SystemExit) as error:
        smoke.main([*args, "--unknown", secret])
    assert error.value.code == 2
    bad = capsys.readouterr()
    assert bad.err == "Argumentos inválidos; consulte --help\n"
    if secret in captured.out + captured.err + bad.out + bad.err + caplog.text:
        pytest.fail("Se filtró un secreto por una excepción o argumento", pytrace=False)


def test_rejected_event_is_not_success(
    site: SeedSite, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    args, _, _ = issued_args(site, capsys)
    real = SyncClient._send

    def reject(self: Any, call: Any) -> Any:
        if call.url.endswith("/observability-events"):
            from vigia_contracts.client._engine import Reply

            return Reply(503, b"{}", {})
        return real(self, call)

    monkeypatch.setattr(SyncClient, "_send", reject)
    exit_code = smoke.main(args, clock=WALL)
    assert exit_code == 1
    assert output(capsys)["code"] == "observability_event_rejected"


@pytest.mark.parametrize(
    "origin",
    (
        "http://api.example.test",
        "https://api.example.test/api/nodes",
        "https://api.example.test?secret=no",
        "https://api.example.test#secret",
        "https://{}@api.example.test".format("user:synthetic"),
    ),
    ids=("http", "path", "query", "fragment", "userinfo"),
)
def test_invalid_origin_never_sends_the_code(
    origin: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("No se debe abrir la red con un origen inválido")

    monkeypatch.setattr(httpx, "Client", forbidden)
    identifier = str(uuid.uuid4())
    args = [
        "--base-url",
        origin,
        "--code",
        "A" * 12,
        "--node-id",
        identifier,
        "--organization-id",
        identifier,
        "--plant-id",
        identifier,
    ]
    exit_code = smoke.main(args)
    assert exit_code == 1
    assert output(capsys)["code"] == "invalid_base_url"


def test_untrusted_platform_certificate_does_not_consume_code(
    site: SeedSite,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    args, _, _ = issued_args(site, capsys)
    alien = server_tls(tmp_path, WALL.now())
    index = args.index("--ca-file") + 1
    trusted = args[index]
    args[index] = str(alien.ca_file)
    exit_code = smoke.main(args, clock=WALL)
    assert exit_code == 1
    report = output(capsys)
    assert report["code"] == "smoke_failed"
    assert report["enrollment"] == "not_run"
    args[index] = trusted
    exit_code = smoke.main(args, clock=WALL)
    assert exit_code == 0
    assert output(capsys)["enrollment"] == "accepted"


@pytest.mark.parametrize("status", (200, 503))
def test_live_view_get_matches_announced_url_without_credentials(
    status: int,
    site: SeedSite,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args, _, _ = issued_args(site, capsys)
    url = "https://192.168.1.10:8443/"
    real = httpx.Client.send
    gets = []
    announced = []

    def send(client: httpx.Client, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        if request.url.path.endswith("/heartbeats"):
            announced.append(json.loads(request.content)["live_view_local_url"])
        if str(request.url) == url:
            gets.append(request)
            return httpx.Response(status, request=request)
        return real(client, request, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", send)
    exit_code = smoke.main([*args, "--live-view-url", url], clock=WALL)
    assert exit_code == (0 if status == 200 else 1)
    report = output(capsys)
    assert report["code"] == ("ok" if status == 200 else "live_view_unavailable")
    assert announced == [url] * 3
    assert len(gets) == 1 and gets[0].method == "GET"
    assert "Authorization" not in gets[0].headers and "Cookie" not in gets[0].headers


@pytest.mark.skipif(
    os.environ.get("VIGIA_408_COMPOSE") != "1",
    reason="Ensayo optativo de la composición completa: VIGIA_408_COMPOSE=1 e imagen de VIG-393",
)
def test_compose_runbook_with_simulated_node(
    deployment: Deployment,
    installed: tuple[str, str],
    resolved_names: None,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> None:
    """Recorre operaciones de las pantallas por HTTP; nunca lee credenciales persistidas.

    El fixture de VIG-393 usa un proyecto, puertos y subred exclusivos y siempre baja
    contenedores y volúmenes en finally. No acredita interfaz visual ni hardware.
    """
    assert installed[0] == installed[1]
    assert deployment.edge_ca is not None
    # Solo generar sesiones sintéticas nuevas, sin abrir credentials.json ni archivos .env.
    program = """
import asyncio, json, sys, uuid
sys.path.insert(0, '/integration')
from seed import (connect, provider_organization, operator_of, user_id, mint_session,
                  renew_concession, Role, CLIENT_CODE, PLANT_CODE)
async def go():
    c = await connect()
    try:
        provider = await provider_organization(c)
        operator = await operator_of(c, provider)
        # Solo este laboratorio: el bootstrap deja pendiente la invitación del operador.
        # No se leen sus credenciales; se prepara una identidad administrativa sintética activa.
        await c.execute(
            "UPDATE identity.user_account SET status='active' WHERE user_id=$1", operator)
        client = await c.fetchval(
            'SELECT organization_id FROM identity.organization WHERE code=$1', CLIENT_CODE)
        client = uuid.UUID(str(client))
        plant = await c.fetchval(
            'SELECT plant_id FROM identity.plant WHERE organization_id=$1 AND code=$2',
            client, PLANT_CODE)
        concession = await renew_concession(c, client=client, provider=provider)
        installer = await mint_session(c, provider, user_id(Role.PROVIDER_INSTALLER))
        admin = await mint_session(c, client, user_id(Role.ADMINISTRATOR))
        return dict(organization=str(client), plant=str(plant), concession=str(concession),
                    operator=str(operator),
                    admin=admin.value, installer=installer.value)
    finally:
        await c.close()
print(json.dumps(asyncio.run(go())))
"""
    done = deployment.compose("run", "--rm", "-T", "vigia-admin", "python", "-c", program)
    context = json.loads(done.stdout.strip().splitlines()[-1])
    monkeypatch.setenv(seed_pope.ADMIN_SESSION, context["admin"])
    monkeypatch.setenv(seed_pope.INSTALLER_SESSION, context["installer"])
    seed_args = [
        "--base-url",
        app_url(deployment),
        "--plant",
        context["plant"],
        "--concession",
        context["concession"],
        "--ca-bundle",
        str(deployment.edge_ca),
    ]
    exchanges = []
    real_send = httpx.Client.send

    def traced_send(client: httpx.Client, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        try:
            response = real_send(client, request, **kwargs)
        except httpx.HTTPError as error:
            exchanges.append(
                (request.method, request.url.host, request.url.path, type(error).__name__)
            )
            raise
        exchanges.append(
            (request.method, request.url.host, request.url.path, str(response.status_code))
        )
        return response

    with monkeypatch.context() as patch:
        patch.setattr(httpx.Client, "send", traced_send)
        exit_code = seed_pope.main(seed_args)
    record_property("seed_pope_exit", exit_code)
    record_property("seed_pope_last_exchange", repr(exchanges[-1]))
    seed_output = capsys.readouterr()
    if exit_code != 0:
        # El documento de montaje también necesita el almacén no publicado. No se
        # finge que Pope quedó preparada: se continúa SOLO con un diagnóstico de
        # transporte sobre la segunda zona sintética que ya prepara VIG-393.
        if not (
            exchanges[-1][0] == "PUT"
            and exchanges[-1][1].endswith("localstack")
            and exchanges[-1][3] == "ConnectError"
        ):
            pytest.fail("Semilla bloqueada: " + repr(exchanges[-2:]), pytrace=False)
        assert seed_output.out == ""
        deployment.seed("issue-code")  # La primera zona de VIG-393 es productive.
        diagnostic = deployment.seed("issue-code")  # La segunda es commissioning.
        issued = {"code": diagnostic["enrollment_code"]}
        seeded = {"node_id": diagnostic["node_id"], "zone_ids": [diagnostic["zone_id"]]}
    else:
        issued, seeded = [json.loads(line) for line in seed_output.out.splitlines()]
    node = seeded["node_id"]
    for action, extra in (("show", ()), ("set", ("--time-source", "ntp.local")), ("show", ())):
        changed = deployment.compose(
            "run",
            "--rm",
            "-T",
            "vigia-admin",
            "vigia-admin",
            "node-configuration",
            action,
            "--node",
            node,
            "--operator",
            context["operator"],
            *extra,
        )
        document = json.loads(changed.stdout.strip().splitlines()[-1])
        if action == "set":
            assert document["time_sources"] == ["ntp.local"]
    args = [
        "--base-url",
        app_url(deployment),
        "--code",
        issued["code"],
        "--node-id",
        node,
        "--organization-id",
        context["organization"],
        "--plant-id",
        context["plant"],
        "--ca-file",
        str(deployment.edge_ca),
    ]
    exit_code = smoke.main(args, clock=WALL)
    assert exit_code == 3
    report = output(capsys)
    assert report["code"] == "clip_storage_unpublished"
    record_property("smoke_report", json.dumps(report, ensure_ascii=False, sort_keys=True))
    assert report["enrollment"] == "accepted" and report["heartbeats_accepted"] == 3
    assert report["observability_event"] == "accepted"
    with httpx.Client(
        base_url=app_url(deployment),
        verify=tls_context(deployment),
        timeout=60,
        headers={
            "Cookie": "__Host-vigia_session=" + context["installer"],
            "X-Vigia-Concession": context["concession"],
            "Sec-Fetch-Site": "same-origin",
        },
    ) as installer:
        inventory = installer.get(f"/fleet/nodes/{node}")
        assert inventory.status_code == 200
        assert inventory.json()["node"]["communication_state"] == "reachable"
        for zone in seeded["zone_ids"]:
            catalog = installer.get(f"/zones/{zone}/catalog")
            assert catalog.status_code == 200
            gates = installer.get(f"/zones/{zone}/gates").json()
            assert gates["resulting_mode"] == "commissioning"
            clips = installer.get(f"/zones/{zone}/commissioning-clips").json()
            assert clips["clips"] == []
        revoked = installer.post(
            f"/nodes/{node}/revocation", json={"reason_es": "Ensayo de retiro"}
        )
        assert revoked.status_code == 200
        reopened = installer.post(f"/nodes/{node}/enrollment-codes")
        assert reopened.status_code == 201
        exit_code = smoke.main(args, clock=WALL)
        assert exit_code == 1
        assert output(capsys)["code"] == "enrollment_code_used"
        args[args.index("--code") + 1] = reopened.json()["code"]
        exit_code = smoke.main(args, clock=WALL)
        assert exit_code == 3
        assert output(capsys)["enrollment"] == "accepted"
        assert (
            installer.post(
                f"/nodes/{node}/revocation", json={"reason_es": "Fin del ensayo"}
            ).status_code
            == 200
        )
        retired = installer.post(
            f"/nodes/{node}/decommission", json={"reason_es": "Fin del ensayo"}
        )
        assert retired.status_code == 200
        record_property("retirement_http_status", retired.status_code)
