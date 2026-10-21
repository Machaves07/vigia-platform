"""DR-LAZ-03 y DR-LAZ-04: reproceso de la cola muerta acumulada y objeto perdido de ``closure/``.

**DR-LAZ-03** (runbook 7.2, camino A): para **cada** consumidor de U-04
(``laz_consumers.consumer_case``), unos eventos se entregan sin caída (la referencia) y otros se
acumulan en la cola muerta durante una caída del manejador (ocho intentos de U-02 en el reloj
inyectado). Recuperado el manejador, ``vigia-admin replay-dead-letter`` (la orden real) reprocesa
cada uno con su ``event_id``: el efecto de cada evento reprocesado es el de uno sin caída
(notificaciones, elementos de cola, aplicaciones a la proyección, exportaciones y marcas) y nada
se duplica al volver a entregarlos. ``assistant_worker``: tras la caída la solicitud ya pasó su
tope duro de 120 s y el reproceso la resuelve ``unavailable`` con ``timeout`` sin llamar al
proveedor (NFR-LAZ-35): ninguna llamada de más.

**DR-LAZ-04** (runbook 7.3) sobre LocalStack con versionado (el depósito de U-02 §6.2) y el
servicio real de adjuntos de cierre: un adjunto verificado se borra (marca de borrado) o se
sobrescribe; la lectura lo **declara** ilegible (``AttachmentUnreadable`` con auditoría
``ERROR``, nunca otros bytes) y la **versión anterior** restaurada vuelve a servirse con el
``sha256`` declarado. Sin versión anterior, la lectura sigue declarándolo ilegible: nunca silencio.
La declaración estructurada «no recuperable» en el hallazgo y en el manifiesto que pide el paso 4
del runbook **no existe todavía en ``src``**: la registra VIG-274 (seguimiento de VIG-248).

Solo datos generados (bytes sintéticos de ``tests.attachment_support``).
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import httpx
import pytest

from tests.attachment_support import jpeg, tiff, utc
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint, versioned_bucket
from tests.integration.test_loop_attachments_localstack import (
    HTTP_TIMEOUT,
    _flow,
    _subject,
    _upload,
)
from tests.resilience.harness import scenario
from tests.resilience.laz_consumers import CONSUMERS, consumer_case
from tests.resilience.laz_support import PROFILES, resilience_storage
from vigia_platform.ledger.application.audit_writer import AuditOutcome
from vigia_platform.loop.adapters.attachments.read import AttachmentUnreadable
from vigia_platform.loop.adapters.attachments.repository import StoredAttachment
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import AllowedScope, Role, ScopeLevel

pytestmark = pytest.mark.integration


# --- DR-LAZ-03 ------------------------------------------------------------------------------------


def _rehearse(endpoint: PostgresEndpoint, name: str, per_partition: int) -> dict[str, Any]:
    with consumer_case(endpoint, name) as case:
        reference = [case.produce(0) for _ in range(per_partition)]
        case.drain(reference, settled={"delivered"})
        case.outage.down = True
        accumulated = [case.produce(1) for _ in range(per_partition)]
        case.drain(accumulated, settled={"dead_letter"})
        assert len(case.dead_letter_rows(accumulated)) == per_partition
        case.outage.down = False
        for event in accumulated:
            receipt = case.replay(event)
            assert receipt["event_id"] == str(event.event_id)
        case.drain(accumulated, settled={"delivered"})
        after = case.effects()
        # Ningún efecto duplicado: otra entrega de cada evento no cambia nada.
        for event in [*reference, *accumulated]:
            case.deliver(event)
        assert case.effects() == after
        expected = sorted(repr(case.shape(event)) for event in reference)
        replayed = sorted(repr(case.shape(event)) for event in accumulated)
        if name == "assistant_worker":
            assert set(replayed) == {repr(("unavailable", "timeout", 0, 0))}
            assert case.extra["provider"].call_count == per_partition
        else:
            assert replayed == expected
        return {"events": 2 * per_partition, "dead_lettered": per_partition, "same_effect": True}


@pytest.mark.parametrize("nightly", PROFILES)
def test_dr_laz_03_replaying_the_accumulated_dead_letter_duplicates_nothing(
    postgres_endpoint: PostgresEndpoint, nightly: bool
) -> None:
    with scenario(
        "DR-LAZ-03",
        title="Reproceso de la cola muerta acumulada durante la caída",
        injection="Caída del manejador de cada consumidor de U-04 hasta la cola muerta",
        expected="replay-dead-letter sin efectos duplicados; el mismo efecto que sin caída",
    ) as run:
        observed = {
            name: _rehearse(postgres_endpoint, name, 2 if nightly else 1) for name in CONSUMERS
        }
        run.observe(consumers=observed)


# --- DR-LAZ-04 ------------------------------------------------------------------------------------


def _stored(flow: Any, verified: Any) -> None:
    flow.repository.attachments[verified.attachment_id] = StoredAttachment(
        flow.place.organization_id,
        flow.place.plant_id,
        flow.place.zone_id,
        verified.storage_key,
        verified.sha256,
        verified.size_bytes,
    )


def _read(flow: Any, attachment_id: Any) -> bytes | None:
    """Los bytes que sirve la URL de lectura, o ``None`` si la lectura se declara ilegible."""
    from tests.authz_support import sealed_context

    reader = sealed_context(
        flow.place.organization_id,
        [AllowedScope(ScopeLevel.ZONE, flow.place.zone_id, Role.PLANT_MANAGER)],
    )
    try:
        grant = asyncio.run(flow.service.read_url(reader, attachment_id))
    except AttachmentUnreadable:
        return None
    response = httpx.get(grant.url, timeout=HTTP_TIMEOUT)
    assert response.status_code == 200
    return response.content


def _versions(s3: Any, bucket: str, key: str) -> list[dict[str, Any]]:
    listed = s3.list_object_versions(Bucket=bucket, Prefix=key)
    return [v for v in listed.get("Versions", []) if v["Key"] == key]


def _restore_previous(s3: Any, bucket: str, key: str, version_id: str) -> None:
    """Paso 3 del runbook 7.3: la versión anterior vuelve a ser la vigente de la clave.

    Se retiran las versiones y marcas de borrado posteriores a ``version_id``: la anterior queda
    vigente con su propia suma de objeto entero (la que verifica la lectura). LocalStack rechaza
    copiar una versión sobre su misma clave sin cambiar metadatos («copy to itself»); en S3 el
    runbook la copia, con el mismo resultado verificado por ``sha256``."""
    listed = s3.list_object_versions(Bucket=bucket, Prefix=key)
    newer = [
        entry["VersionId"]
        for entry in [*listed.get("Versions", []), *listed.get("DeleteMarkers", [])]
        if entry["Key"] == key and entry["VersionId"] != version_id
    ]
    for newer_id in newer:
        s3.delete_object(Bucket=bucket, Key=key, VersionId=newer_id)


def _errors(flow: Any) -> int:
    return sum(1 for entry in flow.audit.entries if entry.outcome == str(AuditOutcome.ERROR))


@pytest.mark.parametrize("nightly", PROFILES)
def test_dr_laz_04_lost_closure_object_is_restored_or_declared(
    localstack_endpoint: LocalStackEndpoint, nightly: bool
) -> None:
    with (
        scenario(
            "DR-LAZ-04",
            title="Pérdida de un objeto de closure/",
            injection="Borrado y sobrescritura de un adjunto; borrado de su única versión",
            expected="Versión anterior restaurada con su sha256; sin ella, lectura declarada "
            "ilegible (la declaración estructurada es VIG-274)",
        ) as run,
        versioned_bucket(localstack_endpoint.aws_client("s3"), "dr-laz-04") as bucket,
    ):
        s3 = localstack_endpoint.aws_client("s3")
        clock = SimulatedClock(utc())
        observed: dict[str, Any] = {}
        for loss in ("delete_marker", "overwrite", "no_previous_version"):
            flow = _flow(resilience_storage(localstack_endpoint, bucket, clock), clock)
            body = jpeg(exif=tiff(gps_tags=None))
            (verified,) = asyncio.run(
                flow.service.verify(_subject(flow), [_upload(flow, body, "image/jpeg")])
            )
            _stored(flow, verified)
            key, declared = verified.storage_key, hashlib.sha256(body).hexdigest()
            assert "/closure/" in key and verified.sha256 == declared
            assert _read(flow, verified.attachment_id) == body
            errors = _errors(flow)
            if loss == "delete_marker":
                s3.delete_object(Bucket=bucket, Key=key)
            elif loss == "overwrite":
                s3.put_object(Bucket=bucket, Key=key, Body=body + b"alterado")
            else:
                for version in _versions(s3, bucket, key):
                    s3.delete_object(Bucket=bucket, Key=key, VersionId=version["VersionId"])
            # La pérdida se declara en la lectura (409 y auditoría ERROR), nunca otros bytes.
            assert _read(flow, verified.attachment_id) is None
            assert _errors(flow) == errors + 1
            previous = [
                v for v in _versions(s3, bucket, key) if v["VersionId"] == verified.version_id
            ]
            if loss == "no_previous_version":
                assert previous == [] and _versions(s3, bucket, key) == []
                assert (
                    _read(flow, verified.attachment_id) is None
                )  # sigue declarado, nunca silencio
                observed[loss] = {
                    "restored": False,
                    "declared_unreadable": True,
                    "sha256": declared,
                }
                continue
            (found,) = previous
            _restore_previous(s3, bucket, key, found["VersionId"])
            restored = _read(flow, verified.attachment_id)
            assert restored == body and hashlib.sha256(restored).hexdigest() == declared
            observed[loss] = {"restored": True, "sha256_matches": True}
        run.observe(**observed, structured_declaration="VIG-274")
