"""PR-LAZ-60: cada URL de lectura se audita como concesión, no descarga."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

from tests.factories import make_context
from vigia_platform.loop.application.attachments import ClosureAttachmentService
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.storage import PresignedRequest


class Repository:
    async def action_scope(self, context: object, action_id: uuid.UUID) -> None:
        return None

    async def attachment_key(self, context: object, attachment_id: uuid.UUID) -> str:
        return "org/a/plant/b/zone/c/closure/d/e.png"


class Reader:
    async def read_url(
        self, key: str, now: dt.datetime, expires_at: dt.datetime
    ) -> PresignedRequest:
        return PresignedRequest("GET", "https://example.invalid", {}, expires_at)


class Audit:
    def __init__(self) -> None:
        self.operations: list[object] = []

    async def append(self, context: object, operation: object, **fields: object) -> None:
        self.operations.append(operation)


def test_each_read_url_emission_has_one_audit_entry() -> None:
    now = dt.datetime(2026, 10, 18, tzinfo=dt.UTC)
    audit = Audit()
    service = ClosureAttachmentService(
        repository=Repository(),  # type: ignore[arg-type]
        grants=None,  # type: ignore[arg-type]
        reader=Reader(),  # type: ignore[arg-type]
        clock=SimulatedClock(now),
        audit=audit,  # type: ignore[arg-type]
        limiter=None,  # type: ignore[arg-type]
    )
    context, attachment = make_context(), uuid.uuid4()
    asyncio.run(service.read_url(context, attachment))
    asyncio.run(service.read_url(context, attachment))
    assert len(audit.operations) == 2
