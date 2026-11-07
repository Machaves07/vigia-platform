"""``vigia-mtls-edge`` contra la plataforma real (TASK-542): API en proceso, base y LocalStack.

La autoridad es la de ``bootstrap``; el nodo, uno de los aprovisionados por el alta real con su
certificado emitido por esa autoridad, y la raíz se lee del depósito ``vigia-edge`` por URL, como
en el entorno de integración. El latido es un latido válido del kit de U-01 y la plataforma lo
acepta: la identidad del nodo solo pudo salir de las cinco cabeceras que puso el balanceador.

``python -m vigia_contracts.conformance.simulated_node --target`` solo sabe hablar con el
``StubPlatform`` del kit (su propia documentación: «Otra plataforma se prueba desde Python»): aquí
se usa la sesión del kit que la suite de conformidad usa contra la plataforma.
"""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import Final

import httpx
import pytest
from vigia_contracts.conformance.checks import PlatformUrl
from vigia_contracts.conformance.checks._core import derive_seed, draw_seeded
from vigia_contracts.conformance.checks._data import fresh_heartbeat, heartbeats
from vigia_contracts.conformance.checks._platform import PlatformSession
from vigia_contracts.conformance.cli import load_provision

from tests.conformance.conftest import PlatformTarget, conformance_seed
from tests.conformance.platform_target import ENROLLMENT_PATH, INGEST_PATH
from tests.conformance.test_mtls_proxy import _authority, _leaf
from tests.integration.conftest import LocalStackEndpoint
from tools.mtls_edge import EdgeConfig, MtlsEdge

pytestmark = pytest.mark.integration

FIVE_HEADERS: Final = {
    "x-amzn-mtls-clientcert-leaf",
    "x-amzn-mtls-clientcert-serial-number",
    "x-amzn-mtls-clientcert-subject",
    "x-amzn-mtls-clientcert-issuer",
    "x-amzn-mtls-clientcert-validity",
}
HEARTBEATS_PATH: Final = "/api/nodes/heartbeats"


def _root_url(target: PlatformTarget, localstack: LocalStackEndpoint) -> str:
    return f"{localstack.url}/{target.stack.environ['VIGIA_EDGE_BUCKET']}/ca/root.pem"


def _edge(target: PlatformTarget, localstack: LocalStackEndpoint) -> MtlsEdge:
    return MtlsEdge(
        EdgeConfig(
            bind="127.0.0.1",
            backends=(target.api.url,),
            server_cert=target.tls.certificate_file,
            server_key=target.tls.key_file,
            server_ca=target.tls.ca_file,
            client_ca=_root_url(target, localstack),
            app_port=0,
            nodes_port=0,
        )
    )


def test_a_node_of_the_environment_beats_through_the_edge(
    platform_target: PlatformTarget, localstack_endpoint: LocalStackEndpoint
) -> None:
    edge = _edge(platform_target, localstack_endpoint)
    edge.start()
    try:
        provision = load_provision(platform_target.provisioned.provision_file)
        platform = PlatformUrl(
            edge.nodes.url + INGEST_PATH,
            edge.app.url + ENROLLMENT_PATH,
            provision.primary,
            second=provision.second,
            certificates=provision.certificates,
            verify=provision.verify,
        )
        seed = derive_seed(conformance_seed(), "mtls-edge", "heartbeat")
        session = PlatformSession(platform.open(seed), seed)
        try:
            heartbeat = fresh_heartbeat(session, draw_seeded(heartbeats(session), seed))
            reply = session.heartbeat(heartbeat)
        finally:
            session.close()
        assert reply.status == 200, reply.outcome_es()
        rows = platform_target.stack.fetch(
            "SELECT heartbeat_id FROM fleet.heartbeat_history WHERE heartbeat_id = $1",
            heartbeat["heartbeat_id"],
        )
        assert len(rows) == 1
        (exchange,) = edge.nodes.served(HEARTBEATS_PATH)
        names = [name for name, _ in exchange.forwarded_mtls]
        assert sorted(names) == sorted(FIVE_HEADERS)
    finally:
        edge.close()


def test_a_certificate_of_another_authority_is_refused_by_the_platform_root(
    platform_target: PlatformTarget,
    localstack_endpoint: LocalStackEndpoint,
    tmp_path: Path,
) -> None:
    edge = _edge(platform_target, localstack_endpoint)
    edge.start()
    try:
        _, foreign = _leaf(_authority("Otra autoridad"), tmp_path, "ajeno")
        context = ssl.create_default_context(cafile=str(platform_target.tls.ca_file))
        context.load_cert_chain(*foreign)
        with (
            httpx.Client(verify=context, timeout=60.0) as client,
            pytest.raises(httpx.HTTPError),
        ):
            client.post(edge.nodes.url + HEARTBEATS_PATH)
        assert edge.nodes.served() == []
    finally:
        edge.close()
