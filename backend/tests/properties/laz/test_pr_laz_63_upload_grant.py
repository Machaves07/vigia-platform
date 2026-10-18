"""PR-LAZ-63: forma y vigencia de concesiones de adjuntos."""

from __future__ import annotations

import datetime as dt
import uuid

from vigia_platform.loop.adapters.attachments.grants import AttachmentUploadGrant
from vigia_platform.shared.clock import SimulatedClock


def test_closure_key_checksum_and_ttl() -> None:
    now = dt.datetime(2026, 10, 18, tzinfo=dt.UTC)
    clock = SimulatedClock(now)
    organization, plant, zone, action = (uuid.uuid4() for _ in range(4))
    grant = AttachmentUploadGrant.issue(
        organization_id=organization,
        plant_id=plant,
        zone_id=zone,
        subject_id=action,
        content_type="image/png",
        size_bytes=1,
        sha256="a" * 64,
        issued_at=now,
        clock=clock,
        random_bytes=lambda _: b"\0" * 16,
    )
    prefix = f"org/{organization}/plant/{plant}/zone/{zone}/closure/{action}/"
    assert grant.storage_key.startswith(prefix)
    assert "/node/" not in grant.storage_key
    assert "/documents/" not in grant.storage_key
    assert grant.expires_at - now == dt.timedelta(minutes=15)
