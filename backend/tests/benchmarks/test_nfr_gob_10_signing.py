"""NFR-GOB-10: presupuesto de la firma (TASK-233; PAT-GOB-REN-08; NFR-GOB-64).

Sobre la aplicación completa de U-03 (``GobPlatform``: PostgreSQL 16, LocalStack y el doble
``MemoryKms`` de ``vigia-node-ca``):

- **Cero llamadas al puerto de firma en 1 000 latidos** (aserción, no tiempo; perfil ``ci``): el
  sobre del conjunto de claves que viaja en cada respuesta del latido sale de la caché en memoria
  de ``SigningService`` (NFR-NUC-36). Se cuentan **todas** las firmas Ed25519 de la plataforma
  (``SigningService._signer``, por donde pasan ``sign`` y ``sign_detached``), las lecturas del
  gestor de secretos y del almacén de claves que recargarían el material, y las firmas del doble
  de KMS; las 1 000 respuestas llevan el mismo sobre del conjunto de claves.
- **Bancos** (perfil ``nightly``, factor de regresión 1,2):
  - ``gob_catalog_publication``: una versión nueva del catálogo de la zona de la matriz máxima
    (32 estándares, 8 cámaras) por ``PUT /zones/{zone_id}/thresholds``, con canonicalización y
    sobre firmado por ``SigningPort`` en la transacción; objetivo p95 ≤ 500 ms;
  - ``gob_node_ca_kms_sign``: ``kms:Sign`` del doble en memoria, **solo informativo** (la cifra
    real, p95 ≤ 100 ms, es de AWS y se mide en el ``soak``); sin objetivo en el informe.

Solo datos generados.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

import httpx
import pytest

from tests.benchmarks.conftest import GOB_REGRESSION_FACTOR, Measure
from tests.benchmarks.gob_support import (
    HEARTBEAT_SPACING_SECONDS,
    MAX_CAMERAS,
    MAX_STANDARDS,
    catalog_zone,
)
from tests.gob_platform_support import REASON, GobPlatform, ok

pytestmark = pytest.mark.integration

HEARTBEATS: Final = 1_000
PUBLICATION_MS: Final = 500.0
PERSON_SPACING_SECONDS: Final = 0.2
"""600 peticiones por minuto y sesión (U-02): una ficha cada 0,1 s."""


@dataclass
class SigningCalls:
    """Llamadas que costarían una firma o una recarga del material de claves."""

    signatures: int = 0
    secret_reads: int = 0
    key_store_loads: int = 0
    kms_signs: int = 0


def count_signing(gob: GobPlatform, monkeypatch: pytest.MonkeyPatch) -> SigningCalls:
    """Envuelve, solo en esta prueba, cada camino hacia el material de firma de la plataforma."""
    calls = SigningCalls()
    signing: Any = gob.services.signing
    signer: Callable[..., Any] = signing._signer
    secrets: Any = signing._secrets
    store: Any = signing._store
    read_secret = secrets.get
    load_keys = store.load
    kms_sign = gob.kms.sign

    def counted_signer(purpose: Any) -> Any:
        calls.signatures += 1
        return signer(purpose)

    async def counted_get(*args: Any, **kwargs: Any) -> Any:
        calls.secret_reads += 1
        return await read_secret(*args, **kwargs)

    async def counted_load(*args: Any, **kwargs: Any) -> Any:
        calls.key_store_loads += 1
        return await load_keys(*args, **kwargs)

    async def counted_kms(*args: Any, **kwargs: Any) -> Any:
        calls.kms_signs += 1
        return await kms_sign(*args, **kwargs)

    monkeypatch.setattr(signing, "_signer", counted_signer)
    monkeypatch.setattr(secrets, "get", counted_get)
    monkeypatch.setattr(store, "load", counted_load)
    monkeypatch.setattr(gob.kms, "sign", counted_kms)
    return calls


def test_nfr_gob_10_key_set_from_cache_zero_signing_calls_in_1000_heartbeats(
    gob: GobPlatform, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow, zone = catalog_zone(gob, productive=True)
    gob.advance(HEARTBEAT_SPACING_SECONDS)
    first = ok(flow.post_heartbeat(zone))
    calls = count_signing(gob, monkeypatch)

    envelopes: set[str] = set()
    for _ in range(HEARTBEATS):
        gob.advance(HEARTBEAT_SPACING_SECONDS)
        body = ok(flow.post_heartbeat(zone))
        envelopes.add(json.dumps(body["platform_public_keys"], sort_keys=True))

    assert calls == SigningCalls(), f"llamadas al puerto de firma en {HEARTBEATS} latidos: {calls}"
    assert envelopes == {json.dumps(first["platform_public_keys"], sort_keys=True)}


@pytest.mark.nightly
def test_nfr_gob_10_catalog_publication(gob: GobPlatform, measure: Measure) -> None:
    _, zone = catalog_zone(gob, standards=MAX_STANDARDS, cameras=MAX_CAMERAS)
    (row,) = gob.fetch(
        "SELECT octet_length(envelope::text) AS size, catalog_version"
        " FROM catalog.zone_catalog_version WHERE zone_id = $1 AND superseded_at IS NULL",
        zone.zone_id,
    )
    responses: list[httpx.Response] = []
    toggle = [0]

    def setup() -> None:
        gob.advance(PERSON_SPACING_SECONDS)
        toggle[0] ^= 1

    def target() -> None:
        body = {"review": 0.4 + toggle[0] / 100, "publication": 0.8, "reason_es": REASON}
        responses.append(
            gob.run(
                gob.send("PUT", f"/zones/{zone.zone_id}/thresholds", cookie=zone.admin,
                         json_body=body)
            )
        )  # fmt: skip

    result = measure(
        "gob_catalog_publication",
        "Publicación de una versión del catálogo (32 estándares, 8 cámaras), con sobre firmado"
        " (NFR-GOB-10)",
        target,
        setup=setup,
        objective_ms=PUBLICATION_MS,
        regression_factor=GOB_REGRESSION_FACTOR,
        details={
            "standards": MAX_STANDARDS,
            "cameras": MAX_CAMERAS,
            "envelope_bytes": row["size"],
            "route": "PUT /zones/{zone_id}/thresholds",
        },
    )
    assert [response.status_code for response in responses] == [200] * len(responses)
    assert result.samples > 0


@pytest.mark.nightly
def test_nfr_gob_10_node_ca_kms_sign_informative(gob: GobPlatform, measure: Measure) -> None:
    message = hashlib.sha256(b"tbs sintetico del certificado del nodo").digest() * 8

    def target() -> None:
        gob.run(gob.kms.sign(gob.kms.key_id, message))

    measure(
        "gob_node_ca_kms_sign",
        "kms:Sign de vigia-node-ca con el doble en memoria, informativo (NFR-GOB-10)",
        target,
        objective_ms=None,
        regression_factor=GOB_REGRESSION_FACTOR,
        informative=True,
        details={"informative": True, "double": "MemoryKms (P-256)", "aws_objective_ms": 100},
    )
