"""FS-GOB-02 · ``SigningPort`` inaccesible al publicar catálogo y al aprobar compuerta (NFR-GOB-42;
BR-GOB-06; PR-GOB-05, 18; PAT-GOB-RES-03, LC-GOB-01, LC-GOB-03).

Sobre la aplicación completa (``gob_platform``) con el ``SigningService`` real envuelto en
``BlockableSigning``: el **punto de KMS de la firma de sobres bloqueado** (la llamada no responde)
durante la transacción de cada operación. Las dos operaciones se piden por sus rutas reales:

- **publicación de catálogo**: ``PUT /zones/{zone_id}/thresholds`` (versión nueva del catálogo de
  una zona con su versión 1 publicada);
- **aprobación de compuerta**: ``POST /use-agreements/{agreement_id}/approval`` (la compuerta de
  uso de una zona en comisionamiento con su acuerdo confirmado por los tres firmantes).

El orden de las dos operaciones sale de la semilla.

**Resultado esperado**:

- las dos **fallan enteras** con ``temporarily_unavailable`` (503, transitorio, con
  ``retry_after_seconds``) dentro del tope de la firma (5 s, NFR-GOB-43) y **revierten**: ninguna
  ``ZoneCatalogVersion`` ni ``GateStateHistory`` nueva o a medias, la proyección de compuertas y
  el acuerdo sin cambiar, ningún registro ni evento (nada sin sobre);
- mientras, el nodo **sigue recibiendo lo ya emitido**: el catálogo firmado de la versión 1;
- al liberar la firma, las mismas peticiones se aceptan y dejan **una** versión y **un**
  intervalo nuevos, cada uno con su sobre firmado.

Solo datos generados.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any, Final

import httpx
import pytest

from tests.gob_platform_support import GobPlatform, GobZone, Onboarding, gob_platform, ok
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint
from tests.resilience.gob_support import BlockableSigning
from tests.resilience.harness import WALL, scenario

pytestmark = [pytest.mark.integration, pytest.mark.nightly]

SIGN_TIMEOUT_SECONDS: Final = 5.0
"""Tope de ``kms:Sign`` (NFR-GOB-43): la operación termina en él."""
MARGIN_SECONDS: Final = 5.0
REASON: Final = "Ajuste sintético de los umbrales de la zona de prueba"


@pytest.fixture(scope="module")
def platform(
    postgres_endpoint: PostgresEndpoint, localstack_endpoint: LocalStackEndpoint
) -> Iterator[tuple[GobPlatform, BlockableSigning]]:
    holder: list[BlockableSigning] = []

    def wrap(target: Any) -> BlockableSigning:
        holder.append(BlockableSigning(target))
        return holder[0]

    with gob_platform(
        postgres_endpoint, localstack_endpoint, "fs_gob_02", wrap_signing=wrap
    ) as gob:
        try:
            yield gob, holder[0]
        finally:
            holder[0].release()


def _snapshot(gob: GobPlatform, zone: GobZone, agreement_id: Any) -> dict[str, Any]:
    """Lo que las dos operaciones escribirían, leído como superusuario."""
    versions = gob.fetch(
        "SELECT catalog_version, superseded_at IS NOT NULL AS superseded,"
        " envelope IS NOT NULL AS signed FROM catalog.zone_catalog_version WHERE zone_id = $1"
        " ORDER BY catalog_version",
        zone.zone_id,
    )
    history = gob.fetch(
        "SELECT gate, status, effective_until IS NOT NULL AS closed"
        " FROM catalog.gate_state_history WHERE zone_id = $1 ORDER BY gate, effective_from",
        zone.zone_id,
    )
    (gates,) = gob.fetch(
        "SELECT resulting_mode, issued_at, envelope::text AS envelope"
        " FROM catalog.zone_gate_state WHERE zone_id = $1",
        zone.zone_id,
    )
    (agreement,) = gob.fetch(
        "SELECT status FROM catalog.use_agreement WHERE agreement_id = $1", agreement_id
    )
    # El ``provider_query`` del instalador se escribe antes de ejecutar la ruta (A-48), también si
    # la operación falla: es el rastro del acceso, no parte de la operación.
    (counts,) = gob.fetch(
        "SELECT (SELECT count(*) FROM ledger.ledger_record WHERE organization_id = $1"
        " AND record_type <> 'provider_query') AS records,"
        " (SELECT count(*) FROM shared.outbox_event WHERE organization_id = $1) AS events",
        zone.organization_id,
    )
    return {
        "versions": [tuple(row) for row in versions],
        "history": [tuple(row) for row in history],
        "gates": (gates["resulting_mode"], str(gates["issued_at"]), gates["envelope"]),
        "agreement": agreement["status"],
        "records": counts["records"],
        "events": counts["events"],
    }


def _timed(call: Callable[[], httpx.Response]) -> tuple[httpx.Response, float]:
    started = WALL.monotonic()
    response = call()
    return response, WALL.monotonic() - started


def test_fs_gob_02_signing_port_down_while_publishing_a_catalog_and_approving_a_gate(
    platform: tuple[GobPlatform, BlockableSigning],
) -> None:
    gob, signing = platform
    with scenario(
        "FS-GOB-02",
        title="SigningPort inaccesible al publicar catálogo y al aprobar compuerta",
        injection="punto de KMS de la firma de sobres bloqueado durante la transacción",
        expected=(
            "las dos operaciones fallan enteras (temporarily_unavailable) y revierten: ninguna"
            " ZoneCatalogVersion ni GateStateHistory a medias; el nodo sigue con lo ya emitido"
        ),
    ) as run:
        flow = Onboarding(gob)
        zone = flow.zone()
        flow.mount(zone)
        # La compuerta de uso, lista para aprobar: acta cerrada, política, firmantes y acuerdo.
        flow.closed_record(zone)
        flow.plant_policy(zone)
        ok(flow.signatory_policy(zone))
        signatories = flow.signatories(zone)
        created = ok(flow.agreement(zone, signatories), 201)
        agreement_id = created["agreement_id"]
        for signatory in signatories:
            assert flow.confirm(agreement_id, signatory).status_code == 201
        catalog_path = f"/api/nodes/zones/{zone.zone_id}/catalog"
        served_before = ok(gob.node_call("GET", catalog_path, certificate=zone.cert))
        before = _snapshot(gob, zone, agreement_id)

        operations: dict[str, Callable[[], httpx.Response]] = {
            "publicación": lambda: flow.gob.call(
                "PUT",
                f"/zones/{zone.zone_id}/thresholds",
                cookie=zone.admin,
                json_body={"review": 0.45, "publication": 0.85, "reason_es": REASON},
            ),
            "aprobación": lambda: flow.approve(zone, agreement_id),
        }
        order = list(operations)
        run.random.shuffle(order)

        signing.block()
        try:
            failed = {name: _timed(operations[name]) for name in order}
            served_blocked = gob.node_call("GET", catalog_path, certificate=zone.cert)
            during = _snapshot(gob, zone, agreement_id)
        finally:
            signing.release()
        blocked_calls = signing.blocked_calls

        retried = {name: operations[name]() for name in order}
        after = _snapshot(gob, zone, agreement_id)
        run.observe(
            order=order,
            failed={
                name: {
                    "status": response.status_code,
                    "code": response.json().get("code"),
                    "retry_after_seconds": response.json().get("retry_after_seconds"),
                    "seconds": round(seconds, 2),
                }
                for name, (response, seconds) in failed.items()
            },
            blocked_sign_calls=blocked_calls,
            node_catalog_while_blocked=served_blocked.status_code,
            changed_while_blocked={
                key: [str(before[key]), str(during[key])]
                for key in before
                if before[key] != during[key]
            },
            retried={name: response.status_code for name, response in retried.items()},
            versions_after=[list(map(str, row)) for row in after["versions"]],
            history_after=[list(map(str, row)) for row in after["history"]],
            mode_after=after["gates"][0],
        )

        for name, (response, seconds) in failed.items():
            body = response.json()
            assert response.status_code == 503, (name, response.text)
            assert body["code"] == "temporarily_unavailable"
            assert 1 <= body["retry_after_seconds"] <= 60
            assert seconds <= SIGN_TIMEOUT_SECONDS + MARGIN_SECONDS, (name, seconds)
        assert blocked_calls == len(operations), "cada operación llegó a firmar dentro de su tx"
        # Fallan enteras y revierten: nada nuevo ni a medias, nada sin sobre.
        assert during == before
        # El nodo sigue recibiendo lo ya emitido (el sobre de la versión 1).
        assert served_blocked.status_code == 200
        assert served_blocked.json() == served_before
        # Al liberar, una versión y un intervalo nuevos, firmados.
        assert {name: response.status_code for name, response in retried.items()} == {
            "publicación": 200,
            "aprobación": 200,
        }, {name: response.text for name, response in retried.items()}
        assert after["versions"] == [(1, True, True), (2, False, True)]
        assert len(after["history"]) == len(before["history"]) + 1
        assert ("usage", "approved", False) in after["history"]
        assert after["gates"][0] == "productive" and after["gates"][2] is not None
        assert after["agreement"] != before["agreement"]
        envelope = json.loads(after["gates"][2])
        assert envelope, "el estado de compuertas nuevo lleva su sobre"
