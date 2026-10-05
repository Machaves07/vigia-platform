"""Degradación de la ingesta con dobles y fallos reales (FS-GOB-01, 03 y 04 en comportamiento;
TASK-221).

NFR-GOB-42 y 49, BL §5 y BR-GOB-96: un fallo de infraestructura responde **transitorio**, nada se
acepta a medias ni de forma optimista, la ingesta no reintenta por su cuenta y el reintento del nodo
con la misma clave produce ``accepted`` y después ``accepted_duplicate``. Contra PostgreSQL 16 real
como ``vigia_app``:

- **almacén sin respuesta** (``head_object`` cae): ``storage_unavailable`` y nada escrito (ni
  registro, ni evento, ni concesión usada, ni auditoría);
- **base pausada** (un intermediario TCP que congela la conexión, como un contenedor en pausa), al
  empezar y a mitad de la transacción del registro: ``temporarily_unavailable`` y cero aceptaciones;
- **cadena retenida** (otra transacción tiene la cabeza de la cadena de la planta): el escritor
  agota
  su ``lock_timeout`` (``chain_locked_timeout``) y la ruta responde ``temporarily_unavailable`` tras
  **una sola** escritura.

Los topes cortos (2 s) son de las bases de estas pruebas, que tratan del tope (retro 15); la pila
usa 60 s. Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any, Final

import pytest
from vigia_contracts.models import api

from tests.fault_proxy import ProxyMode, fault_proxy
from tests.fleet_ingest_support import IngestInstance, IngestSite, IngestStack, ingest_stack
from tests.integration.conftest import PostgresEndpoint
from tests.writer_support import _superuser
from vigia_platform.fleet.domain.ingest_order import IngestKind

pytestmark = pytest.mark.integration

SHORT_TIMEOUT_MS: Final = 2_000
FINDING: Final = IngestKind.FINDING


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[IngestStack]:
    with ingest_stack(postgres_endpoint, "fleet_ingest_degradation") as built:
        yield built


@pytest.fixture(autouse=True)
def _release_instances(stack: IngestStack) -> Iterator[None]:
    yield
    stack.storage.down = False
    stack.run(stack.release())


def _written(stack: IngestStack, site: IngestSite) -> tuple[int, int, int]:
    return (
        len(stack.records("finding_received", site.organization_id)),
        len(stack.events("finding_received", site.organization_id)),
        len(stack.audit(site.organization_id)),
    )


def _transient(response: Any, code: str) -> None:
    assert response.status_code == 503, response.text
    rejection = api.parse_rejection_response(response.content)
    assert (rejection.code.value, rejection.retryable) == (code, True)
    assert rejection.retry_after_seconds is not None and rejection.retry_after_seconds >= 1


def _retry_is_accepted_then_duplicate(
    stack: IngestStack,
    site: IngestSite,
    document: dict[str, Any],
    instance: IngestInstance | None = None,
) -> None:
    first = stack.post(site, FINDING, document, instance)
    assert first.status_code == 200, first.text
    assert api.parse_receipt(first.content).status.value == "accepted"
    again = stack.post(site, FINDING, document, instance)
    assert api.parse_receipt(again.content).status.value == "accepted_duplicate"
    assert _written(stack, site) == (1, 1, 0)


def test_storage_down_is_storage_unavailable_nothing_written_and_the_retry_is_accepted(
    stack: IngestStack,
) -> None:
    site = stack.site()
    document = stack.finding(site)
    clip = document["cameras"][0]["clips"][0]
    stack.storage.down = True
    _transient(stack.post(site, FINDING, document), "storage_unavailable")
    assert _written(stack, site) == (0, 0, 0)
    assert stack.grant_status(clip)[0] == "issued"
    stack.storage.down = False
    _retry_is_accepted_then_duplicate(stack, site, document)
    assert stack.grant_status(clip)[0] == "used"


def test_a_paused_database_is_temporarily_unavailable_with_zero_acceptances(
    stack: IngestStack, postgres_endpoint: PostgresEndpoint
) -> None:
    site = stack.site()
    migrated = stack.authz.sessions.migrated
    with fault_proxy(postgres_endpoint.host, postgres_endpoint.port) as proxy:
        database = stack.database(
            url=migrated.as_role("vigia_app").sqlalchemy_url.replace(
                f"{postgres_endpoint.host}:{postgres_endpoint.port}", f"127.0.0.1:{proxy.port}"
            ),
            statement_timeout_ms=SHORT_TIMEOUT_MS,
            connect_timeout_seconds=2.0,
            pool_timeout_seconds=2.0,
        )
        paused = stack.instance(database)
        # Pausada desde el principio (antes de la identidad del nodo).
        document = stack.finding(site)
        proxy.set_mode(ProxyMode.FREEZE)
        try:
            response = stack.post(site, FINDING, document, paused)
        finally:
            proxy.set_mode(ProxyMode.FORWARD)
        _transient(response, "temporarily_unavailable")
        assert _written(stack, site) == (0, 0, 0)
        # Pausada a mitad de la transacción del registro: dentro de la proyección, antes del INSERT.
        store: Any = paused.service._deps.store
        original = store.mark_cited

        async def frozen(*args: Any, **kwargs: Any) -> Any:
            proxy.set_mode(ProxyMode.FREEZE)
            return await original(*args, **kwargs)

        store.mark_cited = frozen
        try:
            response = stack.post(site, FINDING, document, paused)
        finally:
            del store.mark_cited
            proxy.set_mode(ProxyMode.FORWARD)
        _transient(response, "temporarily_unavailable")
        assert _written(stack, site) == (0, 0, 0)
        assert stack.grant_status(document["cameras"][0]["clips"][0])[0] == "issued"
        # El reintento del nodo, ya con la base sana, por una instancia con los topes normales.
        _retry_is_accepted_then_duplicate(stack, site, document, stack.primary)
        stack.run(asyncio.sleep(0))


def test_a_retained_chain_is_temporarily_unavailable_after_a_single_write(
    stack: IngestStack,
) -> None:
    site = stack.site()
    # Un primer registro crea la cabeza de la cadena de la planta.
    warm = stack.finding(site)
    assert stack.post(site, FINDING, warm).status_code == 200
    locked = stack.instance(stack.database(lock_timeout_ms=SHORT_TIMEOUT_MS))
    writes = 0
    writer: Any = locked.service._deps.writer
    original = writer.write

    async def counted(*args: Any, **kwargs: Any) -> Any:
        nonlocal writes
        writes += 1
        return await original(*args, **kwargs)

    writer.write = counted
    document = stack.finding(site)

    async def scenario() -> Any:
        connection = await _superuser(stack.authz.sessions.migrated)
        transaction = connection.transaction()
        await transaction.start()
        try:
            await connection.execute(
                "SELECT 1 FROM ledger.chain_head WHERE organization_id = $1 AND plant_id = $2"
                " AND kind = 'ledger' FOR UPDATE",
                site.organization_id,
                site.plant_id,
            )
            return await stack.send(site, FINDING, document, locked)
        finally:
            await transaction.rollback()
            await connection.close()

    response = stack.run(scenario())
    _transient(response, "temporarily_unavailable")
    assert writes == 1, "sin reintento propio"
    assert len(stack.records("finding_received", site.organization_id)) == 1  # solo el primero
    # El reintento del nodo, con la cadena libre, por una instancia con los topes normales.
    first = stack.post(site, FINDING, document, stack.primary)
    assert api.parse_receipt(first.content).status.value == "accepted"
    again = stack.post(site, FINDING, document, stack.primary)
    assert api.parse_receipt(again.content).status.value == "accepted_duplicate"
    assert len(stack.records("finding_received", site.organization_id)) == 2
