"""FS-LAZ-09: cola muerta y reproceso con ``replay-dead-letter`` en los seis consumidores.

Para cada consumidor (``laz_consumers.consumer_case``), con el despachador real de U-02:

1. un evento se entrega bien (la referencia);
2. el manejador falla de forma permanente (error no declarado) y el evento agota los ocho intentos
   de ``RetryPolicy`` con su retroceso en el reloj inyectado: va a la cola muerta y la alarma
   salta **a la primera entrada** (``dead_letter_created_total`` de U-02 y
   ``loop_dead_letter_entries_total`` de U-04 = 1 en el exportador en memoria, y un
   ``dead_letter_created`` en la bandeja); entregado + cola muerta = publicado;
3. recuperado el manejador, la partición sigue: un evento posterior se entrega;
4. la orden real ``vigia-admin replay-dead-letter`` devuelve la entrega a ``pending`` con el
   **mismo** ``event_id``, el despachador la entrega y el efecto es el de un evento sin caída; un
   segundo reproceso responde ``not_found`` y nada se duplica (PR-LAZ-40; NFR-LAZ-39).

``assistant_worker`` es la excepción declarada del efecto: tras 26 minutos de reintentos la
solicitud ya pasó su tope duro de 120 s y el reproceso la resuelve ``unavailable`` con
``timeout`` **sin llamar al proveedor** (NFR-LAZ-35), nunca con una segunda llamada.
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.resilience.harness import scenario
from tests.resilience.laz_consumers import CONSUMERS, consumer_case, dispatched
from tests.resilience.laz_support import PROFILES
from vigia_platform.shared.outbox.dispatcher import Outcome
from vigia_platform.shared.outbox.retry import MAX_ATTEMPTS

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("nightly", PROFILES)
@pytest.mark.parametrize("name", CONSUMERS)
def test_dead_letter_alarms_once_and_replay_duplicates_nothing(
    postgres_endpoint: PostgresEndpoint, name: str, nightly: bool
) -> None:
    with (
        scenario(
            f"FS-LAZ-09{'abcdef'[CONSUMERS.index(name)]}",
            title=f"Cola muerta y reproceso en {name}",
            injection="Manejador que falla de forma permanente hasta agotar los 8 intentos",
            expected="Alarma a la primera entrada; replay-dead-letter sin duplicar; "
            "entregado + cola muerta = publicado",
        ) as run,
        consumer_case(postgres_endpoint, name) as case,
    ):
        healthy = case.produce(0)
        case.drain([healthy], settled={"delivered"})
        expected_shape = case.shape(healthy)

        case.outage.down = True
        # El primero llega solo a la cola muerta (en export_worker cada evento es otra partición).
        first = case.produce(1)
        reports = case.drain([first], settled={"dead_letter"})
        assert dispatched(reports, Outcome.DEAD_LETTERED) == 1
        assert dispatched(reports, Outcome.RETRIED) == MAX_ATTEMPTS - 1
        assert case.outage.failures == MAX_ATTEMPTS
        # La alarma salta a la primera entrada: la métrica de U-02, la de U-04 (la que vigila
        # ``loop-dead-letter``) y el evento, ni antes ni después.
        assert case.dead_letter_alarms() == 1
        assert case.loop_dead_letter_alarms() == 1
        assert [row["event_id"] for row in case.dead_letter_events([first])] == [
            str(first.event_id)
        ]
        poisoned = [first, *(case.produce(1) for _ in range(1 if nightly else 0))]
        case.drain(poisoned, settled={"dead_letter"})
        assert len(case.dead_letter_rows(poisoned)) == len(poisoned)
        assert case.dead_letter_alarms() >= len(poisoned)
        assert len(case.dead_letter_events(poisoned)) == len(poisoned)
        (row,) = case.dead_letter_rows([first])
        assert row["attempts"] == MAX_ATTEMPTS
        published = [healthy, *poisoned]
        statuses = case.statuses(published)
        delivered = sum(1 for s in statuses.values() if s == "delivered")
        dead = sum(1 for s in statuses.values() if s == "dead_letter")
        assert delivered + dead == len(published) and dead == len(poisoned)

        # Recuperado el manejador, la partición del evento muerto sigue entregando.
        case.outage.down = False
        later = case.produce(1)
        case.drain([later], settled={"delivered"})

        before_replay = case.effects()
        for event in poisoned:
            receipt = case.replay(event)
            assert receipt["event_id"] == str(event.event_id) and receipt["status"] == "pending"
        assert set(case.statuses(poisoned).values()) == {"pending"}
        case.drain(poisoned, settled={"delivered"})
        if name == "assistant_worker":
            provider = case.extra["provider"]
            calls = provider.call_count
            assert all(case.shape(event) == ("unavailable", "timeout", 0, 0) for event in poisoned)
            assert provider.call_count == calls == 2  # sano y posterior; ninguno del reproceso
        else:
            assert all(case.shape(event) == expected_shape for event in poisoned)
        after_replay = case.effects()

        # Un segundo reproceso no encuentra nada que reprocesar y nada se duplica.
        code, _, error = case.cli("replay-dead-letter", str(first.event_id), name)
        assert code == 4, error
        for event in poisoned:
            case.deliver(event)
        assert case.drain([*published, later], settled={"delivered"}) == []
        assert case.effects() == after_replay
        assert len(case.dead_letter_rows(poisoned)) == len(poisoned)  # la constancia queda
        statuses = case.statuses([*published, later])
        assert set(statuses.values()) == {"delivered"} and len(statuses) == len(published) + 1
        run.observe(
            consumer=name,
            attempts_to_dead_letter=MAX_ATTEMPTS,
            dead_letter_alarm_after_first_entry=1,
            published=len(published),
            delivered_before_replay=delivered,
            dead_letter_before_replay=dead,
            changed_by_replay=before_replay != after_replay,
            second_replay_exit_code=code,
        )
