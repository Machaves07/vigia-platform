"""L-10: RES-05, dos trabajadores no duplican una detección reentregada.

Mutación: quitar ``ON CONFLICT (detection_id) DO NOTHING`` de ReviewQueueRepository.
La prueba lanza dos transacciones y comprueba tanto el ganador como la fila única.
"""

import asyncio

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.loop_review_queue_support import review_queue_stack

pytestmark = pytest.mark.integration


def test_laz_l10_concurrent_replay_has_one_effect(postgres_endpoint: PostgresEndpoint) -> None:
    with review_queue_stack(postgres_endpoint, "laz_l10_replay") as stack:
        detection = stack.world.ingest_detection(stack.world.a)
        event = stack.world.review_queue_event(detection)
        context = stack.world.event_context(detection.organization)
        database = stack.world.stack.database()

        async def deliver() -> bool:
            async with database.transaction(context) as transaction:
                return await stack.consumer.consume(event, transaction)

        async def race() -> list[bool | BaseException]:
            return list(await asyncio.gather(deliver(), deliver(), return_exceptions=True))

        try:
            results = stack.run(race())
            assert [result for result in results if isinstance(result, BaseException)] == []
            assert sorted(result for result in results if isinstance(result, bool)) == [False, True]
            assert stack.run(deliver()) is False
        finally:
            stack.run(database.dispose())
        rows = stack.fetch("SELECT detection_id FROM loop.review_queue_item")
        assert [row["detection_id"] for row in rows] == [detection.subject_id]
