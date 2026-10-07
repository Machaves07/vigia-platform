"""FS-GOB-01 · Almacén de objetos inaccesible durante una concesión y durante una ingesta con clips
(NFR-GOB-09, 42; PR-GOB-01, 20; BR-GOB-96; PAT-GOB-RES-03, LC-GOB-13, LC-GOB-05).

Sobre la aplicación completa de U-03 en los contenedores **propios** del escenario
(``gob_support.gob_stack``: PostgreSQL 16 y LocalStack del arnés), por las rutas del contrato con
el certificado del nodo (cabeceras mTLS del balanceador).

**Inyección**: el contenedor de **LocalStack** (S3, ``vigia-evidence``) se **detiene**:

- (a) **justo antes de una concesión** de subida (``POST /api/nodes/clip-uploads``);
- (b) **en mitad de la verificación de metadatos** de un hallazgo con su clip subido: la primera
  consulta ``head_object`` del verificador del escritor detiene el contenedor antes de salir.

LocalStack no conserva los objetos al detenerse (S3 de verdad sí): al volver, el escenario
recrea los depósitos y el nodo vuelve a subir el mismo clip con la misma concesión.

**Resultado esperado**: ``storage_unavailable`` **transitorio** (503, con
``retry_after_seconds`` entre 1 y 60) dentro del tope de la dependencia (5 s de la concesión, 5 s
de conexión más 10 s de metadatos en la verificación, NFR-GOB-43); **ninguna concesión emitida**
(ninguna fila de ``fleet.clip_upload_grant`` para ese clip), **ningún clip verificado** (la
concesión del clip del hallazgo sigue ``issued``) y **nada escrito ni encadenado** (ni registro,
ni evento); al restablecerse, el **reintento con la misma** ``source_key`` devuelve ``accepted`` y
uno más, ``accepted_duplicate`` con el mismo recibo (PR-GOB-01).

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, Final

import pytest

from tests.factories import uuid7
from tests.gob_platform_support import (
    CLIP_WINDOW_SECONDS,
    GobZone,
    Onboarding,
    ok,
    stamp,
)
from tests.resilience.gob_support import GobStack, gob_stack
from tests.resilience.harness import WALL, scenario
from vigia_platform.shared.api.declarations import NodeRoute

pytestmark = [pytest.mark.integration, pytest.mark.nightly]

GRANT_BUDGET_SECONDS: Final = 5.0
"""Tope de la concesión (firma de URL y consulta del objeto, ``signing_deadline``)."""
METADATA_BUDGET_SECONDS: Final = 5.0 + 10.0
"""Conexión (5 s) más una consulta de metadatos (10 s), PAT-GOB-RES-03."""
MARGIN_SECONDS: Final = 5.0


@pytest.fixture(scope="module")
def stack() -> Iterator[GobStack]:
    with gob_stack("fs_gob_01") as built:
        yield built


def _clip_request(zone: GobZone, data: bytes) -> dict[str, Any]:
    return {
        "clip_id": str(uuid7()),
        "camera_id": str(zone.cameras[0]),
        "zone_id": str(zone.zone_id),
        "media_kind": "video",
        "content_type": "video/mp4",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "duration_ms": 50_000,
        "purpose": "evidence",
    }


def _reference(request: dict[str, Any], grant: dict[str, Any], started: Any) -> dict[str, Any]:
    window = timedelta(seconds=CLIP_WINDOW_SECONDS)
    return {
        "clip_id": request["clip_id"],
        "camera_id": request["camera_id"],
        "media_kind": "video",
        "content_type": "video/mp4",
        "sha256": request["sha256"],
        "size_bytes": request["size_bytes"],
        "duration_ms": request["duration_ms"],
        "starts_at": stamp(started - window),
        "ends_at": stamp(started + timedelta(seconds=30) + window),
        "segment": "full",
        "anonymized": True,
        "storage_key": grant["storage_key"],
    }


def _transient(response: Any) -> dict[str, Any]:
    body: dict[str, Any] = response.json()
    return {
        "status": response.status_code,
        "code": body.get("code"),
        "retryable": body.get("retryable"),
        "retry_after_seconds": body.get("retry_after_seconds"),
        "retry_after_header": response.headers.get("retry-after"),
    }


def _written(stack: GobStack, zone: GobZone, clip_ids: list[str]) -> dict[str, Any]:
    gob = stack.gob
    (counts,) = gob.fetch(
        "SELECT (SELECT count(*) FROM ledger.ledger_record WHERE organization_id = $1"
        " AND record_type <> 'provider_query') AS records,"
        " (SELECT count(*) FROM shared.outbox_event WHERE organization_id = $1) AS events",
        zone.organization_id,
    )
    grants = gob.fetch(
        "SELECT clip_id::text AS clip_id, status FROM fleet.clip_upload_grant"
        " WHERE clip_id = ANY($1::uuid[]) ORDER BY clip_id",
        clip_ids,
    )
    return {
        "records": counts["records"],
        "events": counts["events"],
        "grants": {row["clip_id"]: row["status"] for row in grants},
    }


def test_fs_gob_01_object_store_down_during_a_grant_and_during_an_ingest(
    stack: GobStack,
) -> None:
    gob, flow = stack.gob, stack.flow
    with scenario(
        "FS-GOB-01",
        title="Almacén de objetos inaccesible durante una concesión y una ingesta con clips",
        injection=(
            "LocalStack (S3) detenido justo antes de la concesión y en mitad de la verificación"
            " de metadatos"
        ),
        expected=(
            "storage_unavailable transitorio; ninguna concesión, ningún clip verificado, nada"
            " escrito; al volver, el reintento con la misma source_key da accepted"
        ),
    ) as run:
        zone = flow.productive_zone()

        # (a) Concesión con el almacén detenido.
        data = secrets.token_bytes(run.random.randint(128, 1024))
        request = _clip_request(zone, data)
        before_grant = _written(stack, zone, [request["clip_id"]])
        stack.localstack.stop()
        started = WALL.monotonic()
        refused_grant = gob.node_call(
            "POST", NodeRoute.CLIP_UPLOAD.path, certificate=zone.cert, body=request
        )
        grant_seconds = WALL.monotonic() - started
        after_grant = _written(stack, zone, [request["clip_id"]])
        stack.localstack.start()
        stack.localstack.wait_ready()
        stack.recreate_buckets()
        grant = ok(
            gob.node_call("POST", NodeRoute.CLIP_UPLOAD.path, certificate=zone.cert, body=request)
        )
        Onboarding.put(grant, data)

        # (b) Ingesta: el almacén se detiene en mitad de la consulta de metadatos.
        started_at = gob.now() - timedelta(minutes=5)
        document = flow.finding(zone, started_at, clip=_reference(request, grant, started_at))
        before_ingest = _written(stack, zone, [request["clip_id"]])
        storage = gob.storage
        original = storage.head_object
        heads: list[str] = []

        async def head_then_stop(key: str) -> Any:
            if not heads:
                await asyncio.to_thread(stack.localstack.stop)
            heads.append(key)
            return await original(key)

        storage.head_object = head_then_stop  # type: ignore[method-assign]
        try:
            started = WALL.monotonic()
            refused_ingest = flow.post_finding(zone, document)
            ingest_seconds = WALL.monotonic() - started
        finally:
            storage.head_object = original  # type: ignore[method-assign]
        after_ingest = _written(stack, zone, [request["clip_id"]])

        # Al volver: el nodo vuelve a subir el clip y reintenta con la misma clave.
        stack.localstack.start()
        stack.localstack.wait_ready()
        stack.recreate_buckets()
        Onboarding.put(grant, data)
        accepted = ok(flow.post_finding(zone, document))
        duplicate = ok(flow.post_finding(zone, document))
        records = [
            row
            for row in gob.records(zone.organization_id, "finding_received")
            if row["source_key"] == document["finding_id"]
        ]
        final = _written(stack, zone, [request["clip_id"]])
        run.observe(
            grant={**_transient(refused_grant), "seconds": round(grant_seconds, 2)},
            grant_rows_before=before_grant["grants"],
            grant_rows_after=after_grant["grants"],
            ingest={**_transient(refused_ingest), "seconds": round(ingest_seconds, 2)},
            head_calls_during_ingest=len(heads),
            written_before_ingest=before_ingest,
            written_after_ingest=after_ingest,
            retry=accepted["status"],
            second_retry=duplicate["status"],
            records_with_source_key=len(records),
            grant_after_recovery=final["grants"],
        )

        for refused in (refused_grant, refused_ingest):
            seen = _transient(refused)
            assert (seen["status"], seen["code"]) == (503, "storage_unavailable"), refused.text
            assert seen["retryable"] is True
            assert 1 <= seen["retry_after_seconds"] <= 60
            assert seen["retry_after_header"] == str(seen["retry_after_seconds"])
        assert grant_seconds <= GRANT_BUDGET_SECONDS + MARGIN_SECONDS
        assert ingest_seconds <= METADATA_BUDGET_SECONDS + MARGIN_SECONDS
        # Ninguna concesión emitida ni nada escrito.
        assert after_grant == before_grant and after_grant["grants"] == {}
        # Ningún clip verificado y nada escrito ni encadenado.
        assert heads, "la ingesta llegó a consultar los metadatos"
        assert after_ingest == before_ingest
        assert after_ingest["grants"] == {request["clip_id"]: "issued"}
        # Al volver, la misma source_key: accepted y después accepted_duplicate, una sola vez.
        assert accepted["status"] == "accepted"
        assert duplicate["status"] == "accepted_duplicate"
        assert {k: v for k, v in duplicate.items() if k != "status"} == {
            k: v for k, v in accepted.items() if k != "status"
        }, "el mismo recibo"
        assert len(records) == 1
