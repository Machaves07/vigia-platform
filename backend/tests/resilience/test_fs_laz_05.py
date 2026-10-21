"""FS-LAZ-05: SIGKILL al 40/80 %, arrendamiento de 60 s y retoma desde cero.

Los procesos y los paquetes contienen únicamente datos sintéticos.
"""

from __future__ import annotations

import json
import signal
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

from tests.export_support import export_body, export_stack
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint, versioned_bucket
from tests.resilience.harness import scenario
from tests.resilience.laz_support import PROFILES
from tests.resilience.processes import process_group, read_lines
from vigia_platform.accreditation.export.volumes import ExportLocation

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("nightly", PROFILES)
@pytest.mark.parametrize("percent", [40, 80])
def test_killed_export_is_restarted_without_orphans(
    postgres_endpoint: PostgresEndpoint,
    localstack_endpoint: LocalStackEndpoint,
    tmp_path: Path,
    percent: int,
    nightly: bool,
) -> None:
    with (
        scenario(
            f"FS-LAZ-05{'a' if percent == 40 else 'b'}",
            title="Worker muerto exportando",
            injection=f"SIGKILL tras publicar {percent} %",
            expected="Lease 60 s, retoma desde cero, hash idéntico y cero huérfanos",
        ) as run,
        export_stack(postgres_endpoint, "fs_laz_05") as stack,
        versioned_bucket(localstack_endpoint.aws_client("s3"), "fs-laz-05") as bucket,
        process_group(tmp_path) as group,
    ):
        site = stack.site(plants=1, zones=1)
        plant = next(iter(site.plants))
        response = stack.request_export(
            stack.member(site),
            export_body(
                plant,
                list(site.plants[plant]),
                start=stack.now() - timedelta(days=32),
                days=31,
            ),
        )
        assert response.status_code == 202, response.text
        export_id = uuid.UUID(response.json()["export_id"])
        result = tmp_path / "export.json"
        progress = tmp_path / "progress.jsonl"
        started = stack.now()
        spec = {
            "case": "export",
            "database_url": stack.authz.sessions.migrated.as_role("vigia_app").sqlalchemy_url,
            "s3_url": localstack_endpoint.url,
            "bucket": bucket,
            "now": started.isoformat(),
            "organization_id": str(site.organization_id),
            "export_id": str(export_id),
            "kill_percent": percent,
            "result": str(result),
            "progress_log": str(progress),
            "artifact_bytes": (16 if nightly else 6) * 1024 * 1024,
        }
        dying = group.start_laz_case("dying", spec)
        assert dying.process.wait(120) == -signal.SIGKILL, dying.tail()
        row = stack.export_row(export_id)
        assert row["attempt_count"] == 1 and row["progress_percent"] == percent
        assert row["lease_until"] == started + timedelta(seconds=60)
        killed_upload = row["multipart_upload_id"]
        assert stack.package_row(export_id) is None
        s3 = localstack_endpoint.aws_client("s3")
        location = ExportLocation(site.organization_id, plant, export_id)
        pending = s3.list_multipart_uploads(Bucket=bucket, Prefix=location.prefix)["Uploads"]
        assert row["multipart_upload_id"] in {p["UploadId"] for p in pending}
        volume = next(p for p in pending if p["UploadId"] == killed_upload)
        parts = s3.list_parts(Bucket=bucket, Key=volume["Key"], UploadId=killed_upload)["Parts"]
        assert parts, "La muerte debe dejar partes reales, no solo una subida vacía"
        assert not s3.list_objects_v2(Bucket=bucket, Prefix=location.prefix).get("Contents")
        spec["kill_percent"] = 0
        for seconds in (59.999, 60):
            spec["now"] = (started + timedelta(seconds=seconds)).isoformat()
            busy = group.start_laz_case(f"busy-{seconds}", spec)
            assert busy.process.wait(120) == 0, busy.tail()
            assert json.loads(result.read_text()) == {"busy": True}
            assert stack.package_row(export_id) is None
        offset = len(read_lines(progress))
        spec["now"] = (started + timedelta(seconds=60, milliseconds=1)).isoformat()
        retaker = group.start_laz_case("retaker", spec)
        assert retaker.process.wait(120) == 0, retaker.tail(100)
        observed = json.loads(result.read_text())
        run.observe(
            **observed,
            killed_at_percent=percent,
            lease_seconds=60,
            parts_left_by_killed_process=len(parts),
        )
        assert observed["attempt"] == 2
        assert observed["archive_sha256"] == observed["reference_sha256"]
        row = stack.export_row(export_id)
        assert row["state"] == "ready" and row["attempt_count"] == 2
        assert row["multipart_upload_id"] is None
        assert stack.package_row(export_id)["archive_sha256"] == observed["archive_sha256"]
        assert (
            s3.list_multipart_uploads(Bucket=bucket, Prefix=location.prefix).get("Uploads", [])
            == []
        )
        run.observe(pending_uploads_after_recovery=0)
        events = read_lines(progress)[offset:]
        assert events[0] == {"event": "abort", "upload_id": killed_upload}
        assert next(e["artifact"] for e in events if e["event"] == "started") == "document"
        assert [e["percent"] for e in events if e["event"] == "progress"] == [20, 40, 60, 80, 100]
        retry = group.start_laz_case("redelivery", spec)
        assert retry.process.wait(120) == 0, retry.tail()
        assert json.loads(result.read_text()) == {"busy": False, "already_ready": True}
        assert stack.export_row(export_id)["attempt_count"] == 2
        assert stack.package_row(export_id)["archive_sha256"] == observed["archive_sha256"]
