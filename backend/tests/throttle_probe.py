"""Sonda de otro proceso: lee el retardo de un sujeto de ``AuthThrottle`` (TASK-124, criterio 2).

``python -m tests.throttle_probe <url> <organization_id> <subject_kind> <subject_key> <now>``
abre su propio ``shared.db`` como ``vigia_app``, lee la fila con ``PostgresSessionStore`` y
escribe en la salida estándar ``consecutive_failures``, ``next_allowed_at`` y
``retry_after_seconds`` en ``now``, en JSON. Lo lanza ``tests/properties/test_throttle.py``.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import datetime
from typing import Any, cast

from tests.session_support import TestContexts
from vigia_platform.identity.adapters.session_store import PostgresSessionStore
from vigia_platform.identity.auth.sessions import (
    ThrottleSubject,
    ThrottleSubjectKind,
    retry_after_seconds,
)
from vigia_platform.shared.db import Database, DatabaseSettings, ProcessKind, SslMode


async def _probe(url: str, organization: str, kind: str, key: str, now: datetime) -> dict[str, Any]:
    database = Database.create(
        DatabaseSettings(
            url=url, process=ProcessKind.WORKER, sslmode=SslMode.DISABLE, worker_pool_size=1
        )
    )
    try:
        # La lectura del retardo no audita ni publica: no necesita esos puertos.
        store = PostgresSessionStore(database, cast(Any, None), cast(Any, None))
        organization_id = uuid.UUID(organization)
        state = await store.throttle_state(
            TestContexts().anonymous(organization_id),
            ThrottleSubject(organization_id, ThrottleSubjectKind(kind), key),
        )
    finally:
        await database.dispose()
    if state is None:
        return {"consecutive_failures": 0, "next_allowed_at": None, "retry_after_seconds": 0}
    return {
        "consecutive_failures": state.consecutive_failures,
        "next_allowed_at": state.next_allowed_at.isoformat(),
        "retry_after_seconds": retry_after_seconds(state, now),
    }


def main(argv: list[str]) -> int:
    url, organization, kind, key, now = argv
    print(
        json.dumps(asyncio.run(_probe(url, organization, kind, key, datetime.fromisoformat(now))))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
