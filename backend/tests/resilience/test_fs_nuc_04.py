"""FS-NUC-04 · Almacén inaccesible en una escritura con evidencias (PR-NUC-22, PR-NUC-32;
NFR-NUC-37; PAT-NUC-RES-03).

**Inyección**: el contenedor de **LocalStack** del escenario (el almacén ``S3Storage`` de verdad,
con depósito versionado, clips MP4 sintéticos subidos como el nodo: suma SHA-256 y marca de
anonimización) se **detiene** justo antes de una escritura con evidencias.

**Resultado esperado**:

- la escritura falla con ``StorageUnavailable``, **transitorio** (en la interfaz,
  ``storage_unavailable`` con ``Retry-After``), dentro del tope de una consulta de metadatos;
- **nada persistido**: ni registro, ni evidencia, ni evento de esa escritura;
- las **lecturas sin URL siguen**: ``LectorExpediente.get`` devuelve un registro anterior con sus
  evidencias (metadatos guardados, sin URL firmada) mientras el almacén está detenido;
- ``/health/ready`` falla en menos de 2 s y ``/health/live`` responde 200;
- al volver el almacén, el nodo reenvía y la escritura se acepta.

Solo datos generados: clips de ``synthetic_clip`` del kit, nunca clips reales.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient
from vigia_contracts.conformance.stub_platform.objects import synthetic_clip

from tests.api_support import SENTINEL, World
from tests.factories import uuid7
from tests.integration.conftest import LocalStackEndpoint
from tests.outbox_support import PROBE_EVENT, probe_payload
from tests.resilience.harness import WALL, Container, dedicated_localstack, scenario, wait_until
from tests.resilience.stack import LedgerStack, ledger_stack
from tests.writer_support import (
    NOW,
    ORDER_TYPE,
    Place,
    order_document,
    organization_counts,
    unit_context,
)
from vigia_platform.ledger.application.reader import LectorExpediente
from vigia_platform.ledger.application.writer import EscritorExpediente, Receipt
from vigia_platform.shared.api.app import StartupSupervisor
from vigia_platform.shared.api.errors import TRANSIENT_CODES, ApiErrorCode, translate
from vigia_platform.shared.api.health import READINESS_BUDGET_SECONDS
from vigia_platform.shared.context import ActorKind, ActorUnit, ScopeContext
from vigia_platform.shared.outbox.publish import NewEvent
from vigia_platform.shared.storage import ANONYMIZED_METADATA_KEY, S3Storage, StorageUnavailable

pytestmark = [pytest.mark.integration, pytest.mark.nightly]

METADATA_BUDGET_SECONDS: Final = 5.0 + 10.0
"""Conexión (5 s) más una consulta de metadatos (10 s), PAT-NUC-RES-03."""
MARGIN_SECONDS: Final = 3.0


@pytest.fixture(scope="module")
def backends() -> Iterator[tuple[LedgerStack, LocalStackEndpoint, Container]]:
    with ledger_stack("fs_nuc_04") as stack, dedicated_localstack() as (localstack, container):
        yield stack, localstack, container


def _bucket(localstack: LocalStackEndpoint) -> str:
    s3 = localstack.aws_client("s3")
    name = f"fs-nuc-04-{uuid.uuid4().hex[:12]}"
    s3.create_bucket(Bucket=name)
    s3.put_bucket_versioning(Bucket=name, VersioningConfiguration={"Status": "Enabled"})
    s3.put_object(Bucket=name, Key=SENTINEL, Body=b"centinela sintetico")
    return name


def _clips(localstack: LocalStackEndpoint, bucket: str, place: Place, count: int) -> list[Any]:
    """Sube ``count`` clips sintéticos como el nodo y devuelve sus ``ClipReference``."""
    s3 = localstack.aws_client("s3")
    references = []
    for index in range(count):
        content = synthetic_clip(f"fs-nuc-04 {uuid.uuid4().hex}")
        clip_id = str(uuid7())
        key = place.storage_key(clip_id)
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=content,
            ContentType="video/mp4",
            ChecksumSHA256=base64.b64encode(hashlib.sha256(content).digest()).decode(),
            Metadata={ANONYMIZED_METADATA_KEY: "1"},
        )
        starts = NOW + timedelta(seconds=index * 10)
        references.append(
            {
                "clip_id": clip_id,
                "camera_id": str(uuid.uuid4()),
                "media_kind": "video",
                "content_type": "video/mp4",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
                "duration_ms": 10_000,
                "starts_at": _stamp(starts),
                "ends_at": _stamp(starts + timedelta(seconds=10)),
                "segment": "full",
                "anonymized": True,
                "storage_key": key,
            }
        )
    return references


def _stamp(moment: Any) -> str:
    return str(moment.replace(tzinfo=None).isoformat(timespec="milliseconds")) + "Z"


def _context(place: Place) -> ScopeContext:
    return unit_context(place.organization_id, ActorUnit.U03, kind=ActorKind.SYSTEM)


async def _write(writer: EscritorExpediente, place: Place, document: dict[str, Any]) -> Any:
    event = NewEvent(event_name=PROBE_EVENT, payload=probe_payload(zone_id=str(place.zone_id)))
    return await writer.write(_context(place), ORDER_TYPE, document, events=(event,))


def test_fs_nuc_04_storage_down_during_a_write_with_evidences(
    backends: tuple[LedgerStack, LocalStackEndpoint, Container],
) -> None:
    stack, localstack, container = backends
    with scenario(
        "FS-NUC-04",
        title="Almacén inaccesible en una escritura con evidencias",
        injection="LocalStack detenido",
        expected=(
            "storage_unavailable transitorio; nada persistido; lecturas sin URL siguen; ready falla"
        ),
    ) as run:
        bucket = _bucket(localstack)
        storage = S3Storage(localstack.storage_settings(bucket), WALL)
        database = stack.database()
        writer = stack.writer(database, storage=storage)
        place = Place.new()

        # Antes: un registro con evidencias verificadas contra el almacén de verdad.
        first = order_document(place, clips=0)
        first["clips"] = _clips(localstack, bucket, place, 2)
        receipt = stack.run(_write(writer, place, first))
        assert isinstance(receipt, Receipt), receipt
        before = stack.run(organization_counts(stack.migrated, place.organization_id))

        # La aplicación lista con el almacén de verdad detrás de /health/ready.
        world = World()
        app = world.app(runtime={"storage": storage, "clock": WALL, "sleep": asyncio.sleep})
        second = order_document(place, clips=0)
        second["clips"] = _clips(localstack, bucket, place, run.random.randint(1, 3))
        with TestClient(app) as client:
            supervisor = app.state.vigia_readiness
            assert isinstance(supervisor, StartupSupervisor)
            wait_until(lambda: supervisor.started, timeout=30, message="la API no arrancó")
            assert client.get("/health/ready").status_code == 200

            container.stop()
            started = WALL.monotonic()
            with pytest.raises(StorageUnavailable) as raised:
                stack.run(_write(writer, place, second))
            write_seconds = WALL.monotonic() - started
            api = translate(raised.value)
            after = stack.run(organization_counts(stack.migrated, place.organization_id))

            # Lectura sin URL con el almacén detenido: metadatos guardados, sin firmar nada.
            reader = LectorExpediente(database=database, audit=stack.env.audit)
            reading = unit_context(place.organization_id, ActorUnit.U02)
            view = stack.run(reader.get(reading, receipt.record_id))
            assert view is not None

            probes = []
            for _ in range(3):
                ready_started = WALL.monotonic()
                ready = client.get("/health/ready")
                probes.append(
                    {
                        "ready_status": ready.status_code,
                        "ready_seconds": round(WALL.monotonic() - ready_started, 3),
                        "live_status": client.get("/health/live").status_code,
                    }
                )

        # El almacén vuelve (LocalStack sin persistencia: el nodo vuelve a subir y reenvía).
        container.start()
        container.wait_ready()
        bucket_again = _bucket(localstack)
        storage_again = S3Storage(localstack.storage_settings(bucket_again), WALL)
        resent = dict(second)
        resent["clips"] = _clips(localstack, bucket_again, place, len(second["clips"]))
        retried = stack.run(_write(stack.writer(database, storage=storage_again), place, resent))

        run.observe(
            write={
                "error": type(raised.value).__name__,
                "seconds": round(write_seconds, 2),
                "api_code": api.code.value,
                "api_retry_after_seconds": api.retry_after_seconds,
            },
            rows_before=dict(before),
            rows_after=dict(after),
            read_without_url={
                "record_id": str(view.record_id),
                "evidences": len(getattr(view, "evidences", ()) or ()),
            },
            health=probes,
            retried_after_recovery=type(retried).__name__,
        )
        assert api.code is ApiErrorCode.STORAGE_UNAVAILABLE
        assert api.code in TRANSIENT_CODES and api.retry_after_seconds is not None
        assert write_seconds <= METADATA_BUDGET_SECONDS + MARGIN_SECONDS
        assert after == before, "nada persistido: ni registro, ni evidencia, ni evento"
        assert view.record_id == receipt.record_id
        for probe in probes:
            assert probe["ready_status"] == 503
            assert probe["ready_seconds"] < READINESS_BUDGET_SECONDS
            assert probe["live_status"] == 200
        assert isinstance(retried, Receipt), retried
