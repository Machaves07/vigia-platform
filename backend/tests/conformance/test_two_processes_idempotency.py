"""Latido e ingesta repetidos en dos procesos con la misma clave de idempotencia (NFR-GOB-15).

Dos procesos ``vigia-api`` de verdad (la orden de la imagen, ``tests/resilience/processes.py``)
sobre la misma base, tras el balanceador local con reparto **alterno por petición**: ninguna
ruta del contrato depende de la afinidad de instancia. Con la sesión del kit de U-01
(``PlatformSession``: el mismo cliente, preparación de registros y subida de clips que la suite)
y registros de sus generadores, cada presentación se envía tres veces con la misma
``Idempotency-Key``, así que llega a los dos procesos:

- hallazgo, detección para revisión y evento de observabilidad: la primera ``accepted`` y las
  repetidas ``accepted_duplicate`` con el ``Receipt`` original (mismo ``platform_record_id`` y
  ``received_at``), un solo registro con su ``source_key`` en el expediente;
- latido: ``200`` las tres y una sola fila en ``fleet.heartbeat_history`` (el duplicado no se
  vuelve a aplicar);
- al final, todas las cadenas de la organización íntegras (``verify_ledger_chains``: secuencia
  sin huecos, ``previous_hash``, hash del contenido y del registro, y cabeza).
"""

from __future__ import annotations

import asyncio
from typing import Any, Final

import pytest
from vigia_contracts.conformance import generators as g
from vigia_contracts.conformance.checks import PlatformUrl
from vigia_contracts.conformance.checks._core import derive_seed, draw_seeded
from vigia_contracts.conformance.checks._data import fresh_heartbeat, heartbeats, records
from vigia_contracts.conformance.checks._platform import RECORD_OPERATIONS, PlatformSession
from vigia_contracts.conformance.cli import load_provision

from tests.conformance.conftest import BalancedTarget, PlatformTarget, conformance_seed
from tests.conformance.platform_target import ENROLLMENT_PATH, INGEST_PATH
from tests.writer_support import verify_ledger_chains

pytestmark = pytest.mark.integration

REPEATS: Final = 3
KINDS: Final = ("finding", "detection_for_review", "observability_event")


def _session(target: BalancedTarget, seed: int) -> PlatformSession:
    provision = load_provision(target.provisioned.provision_file)
    platform = PlatformUrl(
        target.nodes.url + INGEST_PATH,
        target.app.url + ENROLLMENT_PATH,
        provision.primary,
        second=provision.second,
        certificates=provision.certificates,
        verify=provision.verify,
    )
    return PlatformSession(platform.open(seed), seed)


def _backends(target: BalancedTarget, start: int, path: str) -> set[int]:
    return {item.backend for item in target.nodes.exchanges[start:] if item.path == path}


@pytest.mark.parametrize("kind", KINDS)
def test_a_repeated_record_is_a_duplicate_with_the_original_receipt_in_both_processes(
    balanced_target: BalancedTarget, platform_target: PlatformTarget, kind: str
) -> None:
    seed = derive_seed(conformance_seed(), "dos-procesos", kind)
    session = _session(balanced_target, seed)
    try:
        record = draw_seeded(records(session, (kind,)), seed)
        ready = session.prepare(record)
        start = len(balanced_target.nodes.exchanges)
        replies = [session.submit(ready) for _ in range(REPEATS)]
    finally:
        session.close()
    path = "/api/nodes" + RECORD_OPERATIONS[kind][1]
    assert _backends(balanced_target, start, path) == {0, 1}
    receipts: list[dict[str, Any]] = []
    for reply in replies:
        assert reply.receipt is not None, reply.outcome_es()
        receipts.append(reply.receipt)
    original, *repeated = receipts
    assert original["status"] == "accepted"
    for receipt in repeated:
        assert receipt == {**original, "status": "accepted_duplicate"}
    source_key = str(ready[g.RECORD_ID_FIELDS[kind]])
    rows = platform_target.stack.fetch(
        "SELECT record_id FROM ledger.ledger_record WHERE source_key = $1", source_key
    )
    assert [str(row["record_id"]) for row in rows] == [original["platform_record_id"]]


def test_a_repeated_heartbeat_is_applied_once_in_both_processes(
    balanced_target: BalancedTarget, platform_target: PlatformTarget
) -> None:
    seed = derive_seed(conformance_seed(), "dos-procesos", "heartbeat")
    session = _session(balanced_target, seed)
    try:
        heartbeat = fresh_heartbeat(session, draw_seeded(heartbeats(session), seed))
        start = len(balanced_target.nodes.exchanges)
        replies = [session.heartbeat(heartbeat) for _ in range(REPEATS)]
    finally:
        session.close()
    assert _backends(balanced_target, start, "/api/nodes/heartbeats") == {0, 1}
    assert [reply.status for reply in replies] == [200] * REPEATS, [
        reply.outcome_es() for reply in replies
    ]
    rows = platform_target.stack.fetch(
        "SELECT heartbeat_id FROM fleet.heartbeat_history WHERE heartbeat_id = $1",
        heartbeat["heartbeat_id"],
    )
    assert len(rows) == 1


def test_the_chains_are_intact_after_the_repetitions(
    balanced_target: BalancedTarget, platform_target: PlatformTarget
) -> None:
    provisioned = platform_target.provisioned
    stack = platform_target.stack
    for node in (provisioned.primary, provisioned.second):
        lengths = asyncio.run(verify_ledger_chains(stack.migrated, node.organization_id))
        assert lengths.get(node.plant_id, 0) > 0
