"""FS-LAZ-03: LocalStack detenido, adjuntos indisponibles y cierre estructurado posible."""

from __future__ import annotations

import uuid

import httpx
import pytest

from tests.attachment_support import png, sha256_hex
from tests.integration.conftest import PostgresEndpoint
from tests.loop_actions_support import actions_stack
from tests.resilience.harness import dedicated_localstack, scenario
from tests.resilience.laz_support import PROFILES, resilience_storage, started_api
from vigia_platform.loop.application.attachments import build_attachment_service

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("nightly", PROFILES)
def test_attachment_outage_does_not_prevent_signed_closure(
    postgres_endpoint: PostgresEndpoint, nightly: bool
) -> None:
    with (
        scenario(
            "FS-LAZ-03",
            title="Almacén de adjuntos caído",
            injection="Detener LocalStack con un adjunto ya verificado",
            expected="503 de concesión/lectura, cierre íntegro y ready sin cambios",
        ) as run,
        dedicated_localstack() as (endpoint, container),
        actions_stack(postgres_endpoint, "fs_laz_03") as stack,
        started_api(stack),
    ):
        # El contenedor es propio y se elimina al salir, también si LocalStack pierde su estado.
        bucket = "fs-laz-03"
        endpoint.aws_client("s3").create_bucket(Bucket=bucket)
        org = stack.world.a
        who = stack.member(org)
        finding, action = stack.with_evidence(who, stack.user(org))
        port = stack.attachments
        real = build_attachment_service(
            repository=port.repository,
            storage=resilience_storage(endpoint, bucket, port.clock),
            authorizer=port.authorizer,
            audit=port.audit,
            clock=port.clock,
        )
        # Se conserva la misma instancia que usan las rutas y EvidenceService.
        port.signer, port.verifier, port.reader = real.signer, real.verifier, real.reader
        payload = png()
        body = {
            "content_type": "image/png",
            "size_bytes": len(payload),
            "sha256": sha256_hex(payload),
        }
        grant_path = f"/loop/actions/{action}/attachments/upload-grant"
        grant = stack.post(who, grant_path, body)
        assert grant.status_code == 201, grant.text
        issued = grant.json()
        uploaded = httpx.put(
            issued["upload"]["url"],
            content=payload,
            headers=issued["upload"]["required_headers"],
            timeout=60,
        )
        assert uploaded.status_code == 200
        claim = {
            "attachment_id": issued["attachment_id"],
            **body,
            "filename": "resguardo.png",
            "no_identifiable_person_declared": True,
        }
        evidence = stack.add_evidence(who, action, stack.evidence_body(attachments=[claim]))
        assert evidence.status_code == 201, evidence.text
        ready = stack.run(stack.client.get("/health/ready")).status_code
        assert ready == 200
        before = stack.untouched(finding)
        container.stop()
        try:
            refused = stack.post(who, grant_path, body)
            read = stack.post(
                who, f"/loop/attachments/{uuid.UUID(issued['attachment_id'])}/read-url", {}
            )
            assert refused.status_code == read.status_code == 503
            assert refused.json()["code"] == read.json()["code"] == "storage_unavailable"
            assert stack.untouched(finding) == before
            closed = stack.close(who, finding, stack.closure_body())
            assert closed.status_code == 201, closed.text
            assert len(stack.records_of(finding, "closure")) == 1
            assert stack.world.finding_state(finding)["lifecycle_state"] == "closed"
            after_ready = stack.run(stack.client.get("/health/ready")).status_code
            assert after_ready == ready
            run.observe(
                grant_status=refused.status_code,
                read_status=read.status_code,
                closure_status=closed.status_code,
                ready_before=ready,
                ready_after=after_ready,
            )
        finally:
            container.start()
            container.wait_ready()
