"""Adopción de ``vigia-contracts`` v1.4.0: catálogos v1.2.0 y nodos v1.1.0 (TASK-571; A-77, A-112).

La plataforma pasa de la etiqueta ``v1.1.0`` a la ``v1.4.0``, solo por adición (BR-CTR-23). Aquí se
prueba contra la composición real en proceso (PostgreSQL y LocalStack con testcontainers, la API
detrás de los proxies mTLS de ``app.`` y ``nodes.``), con la planta Pope sembrada por
``seed_pope`` y el nodo simulado del kit (``NodeCredentials`` y ``SyncClient`` del contrato
instalado: alta real, latidos reales y ``GET zones/{zone_id}/catalog`` real):

- un catálogo con la gramática v1.2.0 (A-77), una condición ``guard_conditions`` sobre la señal
  ``guard`` del jumbo y una señal ``no_data``, se publica por la consola y lo descarga y verifica
  el nodo v1.4.0, sin ningún sobre rechazado;
- un ``guard_conditions`` que cita una señal que no es ``guard`` (o que no está en ``signals``)
  no se publica: ``invalid_request`` y ninguna versión nueva. El motivo es la regla (6) de
  ``ZoneCatalog``: el lector estricto del contrato rechaza el mismo documento con
  ``field = standards[i].predicate.guard_conditions[0].signal_id`` (la consola no devuelve
  ``field``: su ``ApiError`` es cerrado);
- un nodo que declara la 1.1.0 (``X-Vigia-Contract-Version``, alta y latido) sigue dándose de alta,
  latiendo y descargando el catálogo, que le llega con la forma de v1.1.0: la ventana de la
  plataforma conserva las menores que ya aceptaba (BR-CTR-19 y 21).

Solo datos sintéticos (NFR-CTR-43); el código de alta no se imprime.
"""

from __future__ import annotations

import copy
import json
import ssl
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
from vigia_contracts.client import ClientConfig, SyncClient
from vigia_contracts.credentials import (
    FileCredentialStore,
    HttpCredentialTransport,
    NodeCredentials,
    NodeIdentity,
)
from vigia_contracts.models import api
from vigia_contracts.models.api import ContractValidationError
from vigia_contracts.models.enumerations import CompatibilityResult
from vigia_contracts.outbox import Outbox
from vigia_contracts.transport import Client as TransportClient
from vigia_contracts.versioning import CONTRACT_VERSION, CONTRACT_VERSION_HEADER, VersionNegotiator

from tests.conformance.platform_target import WALL
from tests.integration.test_node_enrollment_smoke import SmokePlatform
from tests.integration.test_node_enrollment_smoke import infrastructure as infrastructure
from tests.integration.test_node_enrollment_smoke import platform as platform
from tests.integration.test_seed_pope import SeedSite
from tests.integration.test_seed_pope import site as site  # Fixture HTTP de VIG-394.
from tools import node_enrollment_smoke as smoke
from tools import seed_pope

pytestmark = pytest.mark.integration

TIMEOUT: Final = 60.0
NODE_V1_1: Final = "1.1.0"
JUMBO: Final = "JUMBO-IN-POSITION"
"""La señal ``guard`` del catálogo Pope (DI5)."""
DOOR: Final = "P1-CLOSED"
"""Puerta P1, hoy sin dato: la señal que se declara ``no_data``."""
ENERGY: Final = "MACHINE-RUNNING"
PIT: Final = "Uso indebido en el foso con el jumbo en posición"
REASON: Final = seed_pope.REASON


@dataclass
class SimulatedNode:
    """El nodo simulado del kit, dado de alta en la planta Pope sembrada."""

    client: SyncClient
    node_id: str
    version: str
    sent: list[tuple[str, str | None, str | None]] = field(default_factory=list)
    """``(url, versión pedida, versión respondida)``: la cabecera ``X-Vigia-Contract-Version``
    de cada petición del cliente a ``nodes.`` y de su respuesta."""

    def beat(self) -> list[tuple[str, str | None, str]]:
        """Un latido fuera de turno y las descargas de catálogo que provoca:
        ``(operación, zona, resultado)`` de cada evento nuevo."""
        before = len(self.client.events)
        step = self.client.heartbeat_now()
        assert step is not None and self.client.halted_reason is None
        events = [(e.operation, e.subject, e.result) for e in self.client.events[before:]]
        assert [e[2] for e in events if e[0] == "heartbeat"] == ["success"], events
        return events

    def catalog(self, zone_id: str) -> dict[str, Any]:
        cached = self.client.state.catalogs[zone_id]
        value: dict[str, Any] = cached.to_json_value()
        return value


@contextmanager
def simulated_node(
    site: SeedSite,
    capsys: pytest.CaptureFixture[str],
    directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: str,
) -> Iterator[SimulatedNode]:
    """Siembra Pope y da de alta un nodo que declara ``version`` en el alta y en cada petición."""
    assert seed_pope.main(site.cli()) == 0
    issued, seeded = (json.loads(line) for line in capsys.readouterr().out.splitlines())
    assert isinstance(site.platform, SmokePlatform)
    identity = NodeIdentity(seeded["node_id"], str(site.site.organization_id), site.plant)
    credentials = NodeCredentials(
        identity,
        FileCredentialStore(directory / "credentials.json"),
        WALL,
        live_view_host="192.168.1.10",
        software_version="1.0.0",
        contract_version=version,
    )
    tls = ssl.create_default_context(cafile=str(site.platform.ca))
    base = site.platform.app_url + "/api/nodes"
    with httpx.Client(base_url=base, verify=tls, timeout=TIMEOUT, follow_redirects=False) as http:
        transport = TransportClient(base, base, timeout=httpx.Timeout(TIMEOUT))
        transport.set_enrollment_httpx_client(http)
        enrolled = credentials.enroll(
            issued["code"],
            "1" * 64,
            HttpCredentialTransport(transport, contract_version=version),
        )
    configuration = enrolled.initial_configuration
    config = ClientConfig.from_initial_configuration(
        configuration,
        cafile=str(site.platform.ca),
        connect_timeout_seconds=TIMEOUT,
        read_timeout_seconds=TIMEOUT,
        upload_timeout_seconds=TIMEOUT,
    )
    report = smoke.report_for(configuration, None)
    node: SimulatedNode | None = None
    real = SyncClient._send

    def spy(client: SyncClient, call: Any) -> Any:
        # Solo observa: la petición y su respuesta son las reales.
        assert node is not None
        reply = real(client, call)
        headers = getattr(reply, "headers", {})  # un fallo de red o de tiempo no trae cabeceras
        answered = {k.lower(): v for k, v in headers.items()}
        node.sent.append(
            (
                call.url,
                call.headers.get(CONTRACT_VERSION_HEADER),
                answered.get(CONTRACT_VERSION_HEADER.lower()),
            )
        )
        return reply

    monkeypatch.setattr(SyncClient, "_send", spy)
    with (
        Outbox(directory / "outbox.sqlite", WALL, lock_timeout=TIMEOUT) as outbox,
        SyncClient(
            config,
            credentials,
            outbox,
            WALL,
            status=lambda: report,
            negotiator=VersionNegotiator(WALL, version=version),
        ) as client,
    ):
        client.seed(configuration)
        node = SimulatedNode(client, seeded["node_id"], version)
        yield node


def _current(site: SeedSite, zone: str) -> dict[str, Any]:
    response = site.administrator.get(f"/zones/{zone}/catalog")
    assert response.status_code == 200, response.text
    catalog: dict[str, Any] = response.json()["catalog"]
    return catalog


def _signal(catalog: dict[str, Any], code: str) -> dict[str, Any]:
    signal: dict[str, Any] = next(s for s in catalog["signals"] if s["code"] == code)
    return signal


def _standard(catalog: dict[str, Any], title: str) -> tuple[int, dict[str, Any]]:
    index, standard = next(
        (i, s) for i, s in enumerate(catalog["standards"]) if s["title_es"] == title
    )
    return index, standard


def _with_guard(predicate: dict[str, Any], signal_id: str) -> dict[str, Any]:
    return {**predicate, "guard_conditions": [{"signal_id": signal_id, "value": "asserted"}]}


def _publish_v1_2_form(site: SeedSite, zone: str) -> int:
    """Señal P1 ``no_data`` y la condición del jumbo en el estándar del foso: forma v1.2.0."""
    catalog = _current(site, zone)
    signals = [
        {**signal, "no_data": True} if signal["code"] == DOOR else signal
        for signal in catalog["signals"]
    ]
    response = site.administrator.put(
        f"/zones/{zone}/signals", json={"signals": signals, "reason_es": REASON}
    )
    assert response.status_code == 200, response.text
    _, standard = _standard(catalog, PIT)
    predicate = _with_guard(standard["predicate"], _signal(catalog, JUMBO)["signal_id"])
    response = site.administrator.post(
        f"/zones/{zone}/standards/{standard['standard_id']}/versions",
        json={"changes": {"predicate": predicate}, "reason_es": REASON},
    )
    assert response.status_code == 201, response.text
    version: int = _current(site, zone)["version"]
    assert version == catalog["version"] + 2
    return version


# --- Criterio 2: gramática v1.2.0 hacia el nodo v1.4.0 ------------------------------------------


def test_a_v1_2_catalog_with_guard_conditions_and_no_data_reaches_the_v1_4_node(
    site: SeedSite,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # TASK-572: el kit instalado es v1.5.0; el nodo sigue declarando la 1.4.0 (en ventana) y la
    # plataforma responde con la suya.
    with simulated_node(site, capsys, tmp_path, monkeypatch, version="1.4.0") as node:
        zone = site.zones()["POPE"]
        node.beat()
        before = node.catalog(zone)["version"]
        version = _publish_v1_2_form(site, zone)
        events = node.beat()
        assert ("fetch_catalog", zone, "success") in events, events
        assert not [e for e in events if e[0] == "fetch_catalog" and e[2] != "success"], events
        served = node.catalog(zone)
    assert served["version"] == version > before
    published = _current(site, zone)
    assert served == published
    # Los campos nuevos llegan tal cual al nodo: nada los quita al publicar ni al servir.
    assert _signal(served, DOOR)["no_data"] is True
    assert all("no_data" not in s for s in served["signals"] if s["code"] != DOOR)
    _, pit = _standard(served, PIT)
    jumbo = _signal(served, JUMBO)
    assert jumbo["role"] == "guard"
    assert pit["predicate"]["guard_conditions"] == [
        {"signal_id": jumbo["signal_id"], "value": "asserted"}
    ]
    # El lector estricto de v1.4.0 acepta el documento servido (sin schema_invalid).
    assert api.parse_zone_catalog(json.dumps(served).encode()).to_json_value() == served
    assert {(asked, answered) for _, asked, answered in node.sent} == {
        ("1.4.0", str(CONTRACT_VERSION))
    }


@pytest.mark.parametrize("cited", ["energy", "unknown"])
def test_guard_conditions_citing_a_non_guard_signal_is_refused_by_rule_6(
    site: SeedSite,
    capsys: pytest.CaptureFixture[str],
    cited: str,
) -> None:
    assert seed_pope.main(site.cli()) == 0
    capsys.readouterr()  # El código de alta no se usa ni se muestra.
    zone = site.zones()["POPE"]
    catalog = _current(site, zone)
    signal_id = _signal(catalog, ENERGY)["signal_id"] if cited == "energy" else str(uuid.uuid4())
    index, standard = _standard(catalog, PIT)
    predicate = _with_guard(standard["predicate"], signal_id)
    response = site.administrator.post(
        f"/zones/{zone}/standards/{standard['standard_id']}/versions",
        json={"changes": {"predicate": predicate}, "reason_es": REASON},
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "invalid_request"
    assert _current(site, zone) == catalog  # ninguna versión nueva
    # El motivo es la regla (6): el mismo documento, con el lector estricto del contrato.
    document = copy.deepcopy(catalog)
    document["standards"][index]["predicate"] = predicate
    with pytest.raises(ContractValidationError) as rejected:
        api.parse_zone_catalog(json.dumps(document).encode())
    assert rejected.value.code == "schema_invalid"
    assert rejected.value.field == f"standards[{index}].predicate.guard_conditions[0].signal_id"
    # Con la señal guard, el mismo documento es válido: lo que falla es la cita, no la forma.
    document["standards"][index]["predicate"] = _with_guard(
        standard["predicate"], _signal(catalog, JUMBO)["signal_id"]
    )
    api.parse_zone_catalog(json.dumps(document).encode())


# --- Criterio 3: compatibilidad hacia atrás con un nodo v1.1.0 (BR-CTR-23) -----------------------


def test_a_v1_1_0_node_still_enrolls_beats_and_fetches_the_catalog(
    site: SeedSite,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with simulated_node(site, capsys, tmp_path, monkeypatch, version=NODE_V1_1) as node:
        zone = site.zones()["POPE"]
        node.beat()
        before = node.catalog(zone)
        _, standard = _standard(before, PIT)
        response = site.administrator.post(
            f"/zones/{zone}/standards/{standard['standard_id']}/versions",
            json={
                "changes": {"title_es": PIT + " (revisado)"},
                "reason_es": REASON,
            },
        )
        assert response.status_code == 201, response.text
        events = node.beat()
        assert ("fetch_catalog", zone, "success") in events, events
        served = node.catalog(zone)
        state = node.client.state
    assert served["version"] == before["version"] + 1
    assert served == _current(site, zone)
    # Aceptada, sin aviso de retiro (BR-CTR-21): no ``accepted_with_notice`` ni ``retires_at``.
    notice = state.contract_notice
    assert notice is not None
    assert (notice.result, notice.retires_at) == (CompatibilityResult.ACCEPTED, None)
    # El nodo declaró la 1.1.0 en todas sus peticiones, y la plataforma respondió con su versión.
    assert node.sent
    assert {(asked, answered) for _, asked, answered in node.sent} == {
        (NODE_V1_1, str(CONTRACT_VERSION))
    }
    # El catálogo Pope sigue en la forma de v1.1.0: un lector v1.1.0 no ve campos nuevos.
    text = json.dumps(served)
    assert "guard_conditions" not in text and "no_data" not in text
    inventory = site.installer.get(f"/fleet/nodes/{node.node_id}").json()["node"]
    assert inventory["communication_state"] == "reachable"
