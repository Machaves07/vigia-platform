"""PR-LAZ-54: fábrica única de clientes de salida con lista cerrada (TASK-349; LC-LAZ-39).

- **Metaprueba estática** (``tools/check_outbound_clients.py``): el árbol real de U-04 no
  construye ningún cliente fuera de la fábrica; toda construcción generada (``httpx.AsyncClient``,
  ``httpx.Client``, cliente de SES de boto3 y ``AsyncAnthropic`` con alias, por atributo o con el
  servicio por nombre) metida en un módulo de una copia del árbol cae en rojo con su línea; el
  código inocuo generado (otros servicios, otras clases de ``httpx``, texto que lo menciona) no.
- **Fallo cerrado**: con cada cliente de la fábrica (``httpx``, SES de boto3 y el del proveedor),
  un destino generado fuera de la lista (otro host, un parecido, otro puerto, ``http``, con
  credenciales, el host del otro componente) **no sale**: ``EgressBlocked``, el transporte no se
  llama, ``loop_egress_blocked_total`` suma uno con el componente y el registro crítico nombra
  el componente y la ruta de código (nunca el destino). El destino de la lista sale.
- **Configuración**: la lista se lee de ``RuntimeConfig`` (``VIGIA_ASSISTANT_API_HOST`` y
  ``VIGIA_EMAIL_API_HOST``) y es de **exactamente dos** destinos en ``pilot``; un destino mal
  formado detiene la lectura nombrando la variable y nunca su valor.
- **Separación de transportes** (tech-stack §2.1): el cliente del proveedor nunca recibe
  ``http_client`` ni un objeto de ``httpx``.

Solo datos generados; ninguna petición sale de la máquina (transportes simulados).
"""

from __future__ import annotations

import asyncio
import io
import json
import keyword
import logging
import shutil
from collections.abc import Iterator
from typing import Any, Final

import httpx
import pytest
from botocore.awsrequest import AWSResponse
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint

from tests.runtime_support import runtime_environ
from tools.check_outbound_clients import DEFAULT_SOURCE_ROOT, scan_source, scan_tree, u04_modules
from vigia_platform.loop.http.client_factory import (
    DEFAULT_ASSISTANT_HOST,
    EgressAllowlist,
    EgressBlocked,
    EgressComponent,
    HttpClientFactory,
    default_email_host,
)
from vigia_platform.loop.observability.metrics import LoopMetricName, LoopMetrics
from vigia_platform.shared.observability.logging import JsonFormatter
from vigia_platform.shared.observability.metrics import METER_NAME
from vigia_platform.shared.runtime.config import RuntimeConfig, RuntimeConfigInvalid

CODE_PATH: Final = "vigia_platform.loop.adapters.probe"
TIMEOUT: Final = 30.0
"""Tope de las peticiones simuladas: no decide ningún resultado (retro 15)."""
ASSISTANT: Final = EgressComponent.ASSISTANT_PROVIDER
EMAIL: Final = EgressComponent.EMAIL_SENDER
ALLOWLIST: Final = EgressAllowlist(
    assistant_host=DEFAULT_ASSISTANT_HOST, email_host=default_email_host("us-east-1")
)

# --- Entorno -------------------------------------------------------------------------------------


class Probe:
    """Fábrica con métricas en memoria y el registro crítico capturado en JSON."""

    def __init__(self) -> None:
        self.reader = InMemoryMetricReader()
        self._provider = MeterProvider(metric_readers=[self.reader])
        self.factory = HttpClientFactory(
            ALLOWLIST, metrics=LoopMetrics(self._provider.get_meter(METER_NAME))
        )
        self.stream = io.StringIO()
        self._handler = logging.StreamHandler(self.stream)
        self._handler.setFormatter(JsonFormatter())
        self._logger = logging.getLogger("vigia.loop.http")
        self._logger.addHandler(self._handler)
        self._previous = self._logger.level
        self._logger.setLevel(logging.INFO)

    def blocked(self, component: EgressComponent) -> float:
        data = self.reader.get_metrics_data()
        total = 0.0
        for resource in data.resource_metrics if data is not None else ():
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.name != LoopMetricName.LOOP_EGRESS_BLOCKED_TOTAL.value:
                        continue
                    for point in metric.data.data_points:
                        assert isinstance(point, NumberDataPoint)
                        attributes = dict(point.attributes or {})
                        assert set(attributes) == {"egress_component"}
                        if attributes["egress_component"] == component.value:
                            total += float(point.value)
        return total

    def lines(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.stream.getvalue().splitlines() if line]

    def close(self) -> None:
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._previous)
        self._provider.shutdown()


@pytest.fixture
def probe() -> Iterator[Probe]:
    environment = Probe()
    yield environment
    environment.close()


def _assert_blocked_and_named(
    probe: Probe, component: EgressComponent, before: float, destination: str
) -> None:
    assert probe.blocked(component) == before + 1
    line = probe.lines()[-1]
    assert line["level"] == "CRITICAL"
    assert line["egress_component"] == component.value
    assert line["code_path"] == CODE_PATH
    assert destination not in json.dumps(line, ensure_ascii=False)


# --- Destinos generados --------------------------------------------------------------------------

_LABEL = st.from_regex(r"[a-z][a-z0-9]{0,10}", fullmatch=True)
_TLD = st.from_regex(r"[a-z]{2,6}", fullmatch=True)
_HOSTS = st.builds(
    lambda labels, tld: ".".join([*labels, tld]), st.lists(_LABEL, min_size=1, max_size=3), _TLD
)


def _lookalikes(host: str) -> st.SearchStrategy[str]:
    """URLs que se parecen al destino permitido ``host`` y no lo son."""
    return st.sampled_from(
        [
            f"http://{host}/v1",
            f"https://{host}:8443/v1",
            f"https://{host}.evil.example/v1",
            f"https://evil.example/{host}",
            f"https://user@{host}/v1",
            f"https://{host}@evil.example/v1",
            f"https://x{host}/v1",
            f"https://{host}./v1",
            f"https://{host}:443@evil.example/v1",
            f"https://{host[1:]}/v1",
        ]
    )


def outside_urls(component: EgressComponent) -> st.SearchStrategy[str]:
    allowed = ALLOWLIST.host_for(component)
    other = ALLOWLIST.host_for(EMAIL if component is ASSISTANT else ASSISTANT)
    return st.one_of(
        _HOSTS.filter(lambda host: host != allowed).map(lambda host: f"https://{host}/v1"),
        _lookalikes(allowed),
        st.just(f"https://{other}/v1"),
        st.sampled_from(["https://169.254.169.254/latest", "https://127.0.0.1/", "https://[::1]/"]),
    )


# --- Metaprueba estática -------------------------------------------------------------------------


def test_the_real_u04_tree_builds_no_client_outside_the_factory() -> None:
    assert list(u04_modules(DEFAULT_SOURCE_ROOT)), "el recorrido no encontró módulos de U-04"
    assert scan_tree() == []


# Un alias tiene que ser un identificador válido: sin palabras reservadas (``pass``, ``for``…), que
# rompían el análisis del módulo generado con SyntaxError en vez de probar el escáner (main, 24-10).
_RESERVED_ALIASES: Final = frozenset({"httpx", "boto3", "anthropic", "client"})
_ALIAS = st.from_regex(r"[a-z][a-z]{2,8}", fullmatch=True).filter(
    lambda name: name not in _RESERVED_ALIASES and not keyword.iskeyword(name)
)


@st.composite
def forbidden_construction(draw: st.DrawFn) -> str:
    """Un módulo con **una** construcción de cliente prohibida."""
    alias = draw(_ALIAS)
    service = draw(st.sampled_from(["ses", "sesv2"]))
    return draw(
        st.sampled_from(
            [
                "import httpx\nclient = httpx.AsyncClient()\n",
                f"import httpx as {alias}\nclient = {alias}.AsyncClient(timeout=5)\n",
                f"from httpx import AsyncClient as {alias}\nclient = {alias}()\n",
                f"import httpx\nwith httpx.Client() as {alias}:\n    pass\n",
                f"import boto3\nclient = boto3.client('{service}')\n",
                f"import boto3 as {alias}\nclient = {alias}.client(service_name='{service}')\n",
                f"import boto3\nclient = boto3.session.Session().client('{service}')\n",
                f"{alias} = make()\nclient = {alias}.client('{service}', region_name='x')\n",
                f"import boto3\nclient = boto3.client({alias})\n",
                "from anthropic import AsyncAnthropic\nclient = AsyncAnthropic()\n",
                f"import anthropic as {alias}\nclient = {alias}.AsyncAnthropic(api_key=k)\n",
                f"from vigia_platform.loop import {alias}\nclient = {alias}.AsyncAnthropic()\n",
            ]
        )
    )


BENIGN: Final = st.sampled_from(
    [
        "import httpx\nrequest = httpx.Request('GET', 'https://x')\n",
        "import boto3\nclient = boto3.client('s3')\n",
        "note = 'httpx.AsyncClient()'\n# AsyncAnthropic() en un comentario\n",
        "client = factory.httpx_client(component, CODE_PATH)\n",
        "client = factory.assistant_client(Provider, CODE_PATH, api_key=k)\n",
        "value = self.client\n",
        "def client(service):\n    return service\n",
    ]
)


@settings(max_examples=150)
@given(construction=forbidden_construction(), before=BENIGN, after=BENIGN)
def test_a_generated_construction_is_caught_with_its_line(
    construction: str, before: str, after: str
) -> None:
    source = before + construction + after
    found = scan_source(source, "vigia_platform/loop/adapters/probe.py")
    assert len(found) == 1, (source, found)
    first_line = before.count("\n") + 1
    assert first_line <= found[0].line <= first_line + construction.count("\n")


@given(snippets=st.lists(BENIGN, min_size=1, max_size=5))
def test_benign_code_is_not_reported(snippets: list[str]) -> None:
    assert scan_source("".join(snippets), "vigia_platform/loop/adapters/probe.py") == []


@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(data=st.data(), construction=forbidden_construction())
def test_a_construction_injected_in_a_real_module_fails_the_tree(
    tmp_path_factory: pytest.TempPathFactory, data: st.DataObject, construction: str
) -> None:
    """La mutación: copia del árbol real de U-04 con una construcción metida en un módulo."""
    root = tmp_path_factory.mktemp("src")
    modules = list(u04_modules(DEFAULT_SOURCE_ROOT))
    for package in ("loop", "accreditation", "notifications"):
        shutil.copytree(
            DEFAULT_SOURCE_ROOT / "vigia_platform" / package, root / "vigia_platform" / package
        )
    target = data.draw(st.sampled_from(modules)).relative_to(DEFAULT_SOURCE_ROOT)
    path = root / target
    path.write_text(path.read_text(encoding="utf-8") + "\n" + construction, encoding="utf-8")
    found = scan_tree(root)
    assert [violation.path for violation in found] == [target.as_posix()]


def test_the_factory_module_itself_is_the_only_exception() -> None:
    factory = DEFAULT_SOURCE_ROOT / "vigia_platform" / "loop" / "http" / "client_factory.py"
    assert scan_source(factory.read_text(encoding="utf-8"), "client_factory.py") != []
    assert factory not in set(u04_modules(DEFAULT_SOURCE_ROOT))


# --- Fallo cerrado: httpx ------------------------------------------------------------------------


def _counting_transport(calls: list[httpx.Request]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={})

    return httpx.MockTransport(handle)


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(component=st.sampled_from(EgressComponent), data=st.data())
def test_httpx_client_blocks_any_destination_outside_the_list(
    probe: Probe, component: EgressComponent, data: st.DataObject
) -> None:
    url = data.draw(outside_urls(component))
    calls: list[httpx.Request] = []
    before = probe.blocked(component)

    async def send() -> None:
        async with probe.factory.httpx_client(
            component, CODE_PATH, timeout=TIMEOUT, transport=_counting_transport(calls)
        ) as client:
            await client.get(url)

    try:
        asyncio.run(send())
    except httpx.InvalidURL:
        assume(False)  # httpx no construye la petición: tampoco sale
    except EgressBlocked as error:
        assert error.component is component
        assert error.code_path == CODE_PATH
        assert url not in str(error)
    else:
        raise AssertionError(f"salió hacia un destino fuera de la lista: {url}")
    assert calls == []
    _assert_blocked_and_named(probe, component, before, url)


@pytest.mark.parametrize("component", list(EgressComponent))
def test_httpx_client_reaches_the_allowed_destination(
    probe: Probe, component: EgressComponent
) -> None:
    calls: list[httpx.Request] = []

    async def send() -> int:
        async with probe.factory.httpx_client(
            component, CODE_PATH, timeout=TIMEOUT, transport=_counting_transport(calls)
        ) as client:
            relative = await client.get("/v1/messages")
            absolute = await client.get(f"https://{ALLOWLIST.host_for(component)}:443/v1")
            return relative.status_code + absolute.status_code

    assert asyncio.run(send()) == 400
    assert [request.url.host for request in calls] == [ALLOWLIST.host_for(component)] * 2
    assert probe.blocked(component) == 0


def test_httpx_client_blocks_a_redirect_out_of_the_list(probe: Probe) -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://evil.example/"})

    async def send() -> None:
        async with probe.factory.httpx_client(
            ASSISTANT, CODE_PATH, timeout=TIMEOUT, transport=httpx.MockTransport(handle)
        ) as client:
            response = await client.get("/v1")
            assert response.status_code == 302  # sin seguir la redirección
            assert response.next_request is not None
            await client.send(response.next_request)

    with pytest.raises(EgressBlocked):
        asyncio.run(send())
    assert len(calls) == 1


@pytest.mark.parametrize(
    "option", ["base_url", "event_hooks", "follow_redirects", "proxy", "mounts", "trust_env"]
)
def test_httpx_client_options_owned_by_the_factory_are_refused(option: str) -> None:
    with pytest.raises(ValueError, match=option):
        HttpClientFactory(ALLOWLIST).httpx_client(
            ASSISTANT, CODE_PATH, timeout=TIMEOUT, **{option: None}
        )


@pytest.mark.parametrize(
    "code_path",
    ["", "loop.adapters", "vigia_platform", "vigia_platform.Loop", "vigia_platform.loop.x-y", 7],
)
def test_a_client_without_a_code_path_constant_is_not_built(code_path: Any) -> None:
    with pytest.raises(ValueError):
        HttpClientFactory(ALLOWLIST).httpx_client(ASSISTANT, code_path, timeout=TIMEOUT)


# --- Fallo cerrado: SES de boto3 -----------------------------------------------------------------


def _ses(probe: Probe, sent: list[str], *, redirect_to: str | None) -> Any:
    client = probe.factory.email_client(
        CODE_PATH,
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # noqa: S106 - credencial falsa de prueba
    )
    if redirect_to is not None:

        def redirect(request: Any, **_: Any) -> None:
            request.url = redirect_to

        client.meta.events.register_first("before-send", redirect)

    def answer(request: Any, **_: Any) -> AWSResponse:
        sent.append(str(request.url))
        return AWSResponse(str(request.url), 200, {}, _Raw(b'{"EmailIdentities": []}'))

    client.meta.events.register_last("before-send", answer)
    return client


class _Raw:
    """Cuerpo de respuesta mínimo para ``AWSResponse``."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def stream(self, **_: Any) -> Iterator[bytes]:
        yield self._body

    def read(self, *_: Any, **__: Any) -> bytes:
        return self._body


@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(data=st.data())
def test_ses_client_blocks_any_destination_outside_the_list(
    probe: Probe, data: st.DataObject
) -> None:
    url = data.draw(outside_urls(EMAIL))
    sent: list[str] = []
    before = probe.blocked(EMAIL)
    client = _ses(probe, sent, redirect_to=url)
    with pytest.raises(EgressBlocked):
        client.list_email_identities()
    assert sent == []
    _assert_blocked_and_named(probe, EMAIL, before, url)


def test_ses_client_reaches_the_email_host_only(probe: Probe) -> None:
    sent: list[str] = []
    client = _ses(probe, sent, redirect_to=None)
    assert client.list_email_identities()["EmailIdentities"] == []
    assert [url.split("/")[2] for url in sent] == [ALLOWLIST.email_host]
    assert probe.blocked(EMAIL) == 0
    with pytest.raises(ValueError, match="endpoint_url"):
        probe.factory.email_client(CODE_PATH, region_name="us-east-1", endpoint_url="https://x.io")


# --- Fallo cerrado: cliente del proveedor --------------------------------------------------------


class FakeProvider:
    """Doble del cliente del paquete del proveedor: guarda cómo se construyó."""

    def __init__(self, **options: Any) -> None:
        self.options = options
        self.base_url = httpx.URL(options["base_url"])
        self.calls = 0

    async def create(self) -> str:
        self.calls += 1
        return "ok"


def test_provider_client_is_built_with_its_transport_defaults_and_the_assistant_host(
    probe: Probe,
) -> None:
    guarded = probe.factory.assistant_client(FakeProvider, CODE_PATH, api_key="k", max_retries=2)
    client = guarded._client
    assert client.options == {
        "base_url": f"https://{ALLOWLIST.assistant_host}",
        "api_key": "k",
        "max_retries": 2,
    }
    assert asyncio.run(guarded.call(lambda provider: provider.create())) == "ok"
    assert client.calls == 1


@pytest.mark.parametrize(
    "options",
    [
        {"http_client": None},
        {"base_url": "https://evil.example"},
        {"transport": httpx.AsyncHTTPTransport()},
        {"session": httpx.AsyncClient(timeout=TIMEOUT)},
        {"other": httpx.Client(timeout=TIMEOUT)},
        {"pool": httpx.HTTPTransport()},
    ],
)
def test_provider_client_never_receives_an_httpx_object(options: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        HttpClientFactory(ALLOWLIST).assistant_client(FakeProvider, CODE_PATH, **options)


@settings(max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(data=st.data())
def test_provider_call_is_blocked_when_the_client_points_outside(
    probe: Probe, data: st.DataObject
) -> None:
    url = data.draw(outside_urls(ASSISTANT))
    guarded = probe.factory.assistant_client(FakeProvider, CODE_PATH, api_key="k")
    try:
        guarded._client.base_url = httpx.URL(url)
    except httpx.InvalidURL:
        assume(False)
    before = probe.blocked(ASSISTANT)
    with pytest.raises(EgressBlocked):
        asyncio.run(guarded.call(lambda provider: provider.create()))
    assert guarded._client.calls == 0
    _assert_blocked_and_named(probe, ASSISTANT, before, url)


# --- La lista sale de la configuración -----------------------------------------------------------


def test_pilot_has_exactly_two_destinations_from_the_configuration() -> None:
    config = RuntimeConfig.from_environ(runtime_environ(VIGIA_ENVIRONMENT="pilot"))
    allowlist = EgressAllowlist.from_config(config)
    assert allowlist.hosts == {"api.anthropic.com", f"email.{config.aws_region}.amazonaws.com"}
    assert len(allowlist.hosts) == 2


@given(assistant=_HOSTS, email=_HOSTS)
def test_the_destinations_are_read_from_the_environment(assistant: str, email: str) -> None:
    assume(assistant != email)
    environ = runtime_environ(
        VIGIA_ENVIRONMENT="pilot", VIGIA_ASSISTANT_API_HOST=assistant, VIGIA_EMAIL_API_HOST=email
    )
    allowlist = HttpClientFactory.from_config(RuntimeConfig.from_environ(environ)).allowlist
    assert (allowlist.assistant_host, allowlist.email_host) == (assistant, email)
    assert allowlist.hosts == {assistant, email}


@pytest.mark.parametrize("variable", ["VIGIA_ASSISTANT_API_HOST", "VIGIA_EMAIL_API_HOST"])
@pytest.mark.parametrize(
    "value",
    [
        "https://api.anthropic.com",
        "api.anthropic.com:443",
        "api.anthropic.com/v1",
        "API.anthropic.com",
        "*.anthropic.com",
        "api.anthropic.com.",
        "10.0.0.1",
        "localhost",
        "api..anthropic.com",
        "-api.anthropic.com",
        "api.anthropic.com\n",
        "аpi.anthropic.com",  # noqa: RUF001 - a cirílica
        "api.anthropic.com ",
        "a" * 300 + ".com",
    ],
)
def test_a_malformed_destination_is_named_without_its_value(variable: str, value: str) -> None:
    with pytest.raises(RuntimeConfigInvalid) as raised:
        RuntimeConfig.from_environ(runtime_environ(**{variable: value}))
    assert raised.value.variable == variable
    assert value.strip() not in str(raised.value)


def test_the_two_destinations_cannot_be_the_same() -> None:
    environ = runtime_environ(
        VIGIA_ASSISTANT_API_HOST="api.example.com", VIGIA_EMAIL_API_HOST="api.example.com"
    )
    with pytest.raises(RuntimeConfigInvalid):
        EgressAllowlist.from_config(RuntimeConfig.from_environ(environ))
