"""El motor de verificación contra PostgreSQL 16 real (TASK-118, LC-NUC-13; BR-NUC-56, 58).

Base migrada; cadenas escritas por ``vigia_app`` y encadenadas por el disparador; el motor lee
como ``vigia_app`` en un ``shared.db`` de worker (``verify_support``). Complementa PR-NUC-14 y
PR-NUC-49 (``tests/properties/test_verify_*.py``) con lo que no generan:

- **Criterio 3**: una cadena rota deja una entrada ``integrity_verification`` con resultado
  ``broken`` (el registro roto como ``resource_ref``) y un ``integrity_compromised`` en la bandeja,
  y suma ``integrity_compromised_total``; una íntegra, solo la entrada ``intact``.
- **Paso 2**: la forma canónica de los bytes (``content_not_canonical``) y los puntos de control:
  clave no publicada, firma que no verifica y firma válida recodificada en base64 no canónico
  (seguimiento de VIG-62), en ``full`` y en ``incremental``.
- **Estado incremental**: la incremental parte del último punto íntegro auditado, con la muestra
  del 1 % y al menos 100; una cadena rota sigue rota en la siguiente (BR-NUC-58), también si la
  completa encuentra la rotura por detrás del punto de la incremental, hasta que una completa
  íntegra tras la restauración la cierra.
- **Huecos**: falta la última fila de un lote o de la cadena → ``sequence_gap`` en esa secuencia.
- **Cabeza**: ``ChainHead`` con otro hash o registros por encima de ella → ``head_mismatch``.
- **Oráculo del sobre**: el sobre escrito en la consulta del paso 1 es byte a byte el de
  ``ledger.vigia_canonical_envelope`` y ``shared.vigia_canonical_audit_envelope`` con nombres
  difíciles, marcas límite y nulos (la función recalcula el hash; el paso 1 no debe ver rotura).
- **Fallos a mitad**: una sentencia o el ``COMMIT`` de la auditoría del resultado que fallan no
  dejan entrada ni evento; un paso 1 que falla en el segundo lote tampoco (P5).
- **Worker**: ``statement_timeout`` de 30 s; el adaptador no se construye sobre la API.
- **Tareas**: ``verify_chains_incremental`` y ``verify_chains_full`` registradas con su horario,
  y su manejador verifica todas las cadenas de la organización.

Solo datos generados.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text

from tests.hibp_service import metric_total, metrics_with_reader
from tests.identity_db import MigratedDatabase, migrated_database
from tests.integration.conftest import PostgresEndpoint
from tests.outbox_support import InjectedFault
from tests.properties.envelope_strategies import BOUNDARY_TIMESTAMPS, display_names
from tests.verify_support import (
    FOREIGN_KEY,
    BuiltChain,
    VerifyEnvironment,
    append_entry,
    build_chain,
    checkpoint_content,
    delete_entry,
    mutate,
    mutate_head,
    verify_environment,
)
from tests.writer_support import Fault
from vigia_platform.ledger.adapters.integrity_store import SqlIntegrityStore
from vigia_platform.ledger.application.verify_tasks import (
    VERIFY_CHAINS_FULL,
    VERIFY_CHAINS_INCREMENTAL,
    register_verify_chains,
)
from vigia_platform.ledger.chain.chain_walk import genesis_hash
from vigia_platform.ledger.chain.checkpoints import CheckpointChain
from vigia_platform.ledger.chain.verify import (
    BatchScan,
    IntegrityResult,
    IntegrityService,
    IntegrityStatus,
    VerificationMode,
    VerifiedPoint,
    sql_pass,
)
from vigia_platform.shared.db import ProcessKind, TemporarilyUnavailable
from vigia_platform.shared.observability.metrics import MetricName
from vigia_platform.shared.outbox.registries import PeriodicTaskRegistry, Schedule
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def environment(postgres_endpoint: PostgresEndpoint) -> Iterator[VerifyEnvironment]:
    with (
        migrated_database(postgres_endpoint, "vigia_integrity_verify") as migrated,
        verify_environment(migrated) as environment,
    ):
        yield environment


# --- Lecturas como superusuario ------------------------------------------------------------------


async def _fetch(migrated: MigratedDatabase, query: str, *args: Any) -> list[Any]:
    connection = await migrated.connect()
    try:
        return list(await connection.fetch(query, *args))
    finally:
        await connection.close()


def verifications(environment: VerifyEnvironment, organization_id: uuid.UUID) -> list[Any]:
    """Entradas ``integrity_verification`` de la organización, en orden de la cadena."""
    return environment.run(
        _fetch(
            environment.migrated,
            "SELECT filters_json, resource_kind, resource_id, scope_plant_id, actor_kind"
            " FROM shared.audit_entry WHERE organization_id = $1"
            " AND operation = 'integrity_verification' ORDER BY chain_sequence",
            organization_id,
        )
    )


def compromised(environment: VerifyEnvironment, organization_id: uuid.UUID) -> list[Any]:
    """Eventos ``integrity_compromised`` de la organización en la bandeja."""
    return environment.run(
        _fetch(
            environment.migrated,
            "SELECT plant_id, payload FROM shared.outbox_event WHERE organization_id = $1"
            " AND event_name = 'integrity_compromised' ORDER BY created_at, event_id",
            organization_id,
        )
    )


def verify(
    environment: VerifyEnvironment,
    built: BuiltChain,
    mode: VerificationMode = VerificationMode.FULL,
    service: IntegrityService | None = None,
) -> IntegrityResult:
    service = service or environment.service()
    return environment.run(service.verify(built.context, built.chain, mode))


def _as_json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


# --- Criterio 3: auditoría y evento ---------------------------------------------------------------


@pytest.mark.parametrize("shape", ["plant", "organization", "audit"])
def test_broken_chain_audits_broken_and_publishes_integrity_compromised(
    environment: VerifyEnvironment, shape: Any
) -> None:
    built = environment.run(build_chain(environment.migrated, shape, ["record"] * 5))
    target = built.rows[2]
    hash_column = "filters_hash" if shape == "audit" else "content_hash"
    environment.run(
        mutate(
            environment.migrated,
            built.table,
            built.id_column,
            target[built.id_column],
            {hash_column: "f" * 64},
        )
    )
    metrics, reader = metrics_with_reader()
    service = environment.service(metrics=metrics)

    result = verify(environment, built, service=service)

    assert result.status is IntegrityStatus.BROKEN
    assert result.broken_sequence == 3
    assert result.to_sequence == 2
    assert result.reason == "content_hash_mismatch"
    assert result.broken_entry_id == uuid.UUID(str(target[built.id_column]))
    assert result.verified_at is not None
    (entry,) = verifications(environment, built.organization_id)
    filters = _as_json(entry["filters_json"])
    assert filters["result"] == "broken"
    assert filters["mode"] == "full"
    assert filters["chain_kind"] == built.kind
    assert filters["broken_sequence"] == 3
    assert filters["reason"] == "content_hash_mismatch"
    assert entry["resource_kind"] == ("audit_entry" if shape == "audit" else "ledger_record")
    assert entry["resource_id"] == target[built.id_column]
    assert entry["actor_kind"] == "system"
    (event,) = compromised(environment, built.organization_id)
    assert event["plant_id"] == built.plant_id
    assert _as_json(event["payload"]) == {
        "chain_kind": built.kind,
        "first_failed_sequence": 3,
        "verification_mode": "full",
        "detected_at": format_timestamp(environment.env.clock.now()),
    }
    assert metric_total(reader, MetricName.INTEGRITY_COMPROMISED_TOTAL) == 1


def test_intact_chain_audits_intact_without_event(environment: VerifyEnvironment) -> None:
    built = environment.run(
        build_chain(environment.migrated, "plant", ["record", "keyed", "checkpoint", "record"])
    )
    metrics, reader = metrics_with_reader()
    result = verify(environment, built, service=environment.service(metrics=metrics))
    assert result.status is IntegrityStatus.INTACT
    assert (result.from_sequence, result.to_sequence, result.head_sequence) == (1, 4, 4)
    assert result.verified_hash == built.rows[-1]["record_hash"]
    assert result.checkpoints_checked == 1
    assert result.canonical_checked == 4
    assert result.sample_seed is None
    (entry,) = verifications(environment, built.organization_id)
    assert _as_json(entry["filters_json"])["result"] == "intact"
    assert entry["resource_kind"] is None and entry["resource_id"] is None
    assert entry["scope_plant_id"] == built.plant_id
    assert compromised(environment, built.organization_id) == []
    assert metric_total(reader, MetricName.INTEGRITY_COMPROMISED_TOTAL) == 0


def test_empty_chain_is_intact_at_genesis(environment: VerifyEnvironment) -> None:
    built = BuiltChain("plant", uuid.uuid4(), uuid.uuid4())
    result = verify(environment, built)
    assert result.status is IntegrityStatus.INTACT
    assert (result.to_sequence, result.head_sequence) == (0, 0)
    assert result.verified_hash == genesis_hash(str(built.organization_id), built.plant_text)


# --- Paso 2: forma canónica y puntos de control ---------------------------------------------------


@pytest.mark.parametrize("shape", ["plant", "audit"])
@pytest.mark.parametrize("mode", [VerificationMode.FULL, VerificationMode.INCREMENTAL])
def test_non_canonical_bytes_break_in_the_second_pass(
    environment: VerifyEnvironment, shape: Any, mode: VerificationMode
) -> None:
    """Bytes con el mismo documento en forma no canónica: el disparador calcula su hash (el paso 1
    no ve nada) y el paso 2 los rechaza; la incremental los ve porque el lote tiene < 100."""
    built = environment.run(build_chain(environment.migrated, shape, ["record"] * 3))
    environment.run(
        append_entry(environment.migrated, built, "record", raw_content=b'{"b": 1, "a": [2]}')
    )
    environment.run(append_entry(environment.migrated, built, "record"))
    result = verify(environment, built, mode)
    assert result.status is IntegrityStatus.BROKEN
    assert (result.broken_sequence, result.reason) == (4, "content_not_canonical")
    assert result.broken_entry_id == uuid.UUID(str(built.rows[3][built.id_column]))


def _noncanonical(text_value: str) -> str:
    """El mismo valor en base64 con bits de relleno distintos de cero en el último carácter."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    body = text_value.rstrip("=")
    padding = len(text_value) - len(body)
    assert padding > 0
    last = alphabet.index(body[-1]) | ((1 << (2 * padding)) - 1)
    changed = body[:-1] + alphabet[last] + "=" * padding
    assert changed != text_value
    assert base64.b64decode(changed, validate=True) == base64.b64decode(text_value)
    return changed


def _signature_cases(built: BuiltChain) -> dict[str, dict[str, Any]]:
    last = built.rows[-1]
    valid = checkpoint_content(built, last)
    return {
        "unknown_key": checkpoint_content(built, last, FOREIGN_KEY),
        "bad_signature": {
            **valid,
            "signature": checkpoint_content(built, built.rows[0])["signature"],
        },
        "checkpoint_malformed": {**valid, "signature": _noncanonical(valid["signature"])},
    }


@pytest.mark.parametrize("reason", ["unknown_key", "bad_signature", "checkpoint_malformed"])
@pytest.mark.parametrize("shape", ["organization", "audit"])
@pytest.mark.parametrize("mode", [VerificationMode.FULL, VerificationMode.INCREMENTAL])
def test_checkpoint_signature_failures(
    environment: VerifyEnvironment, reason: str, shape: Any, mode: VerificationMode
) -> None:
    built = environment.run(build_chain(environment.migrated, shape, ["record"] * 3))
    content = _signature_cases(built)[reason]
    environment.run(append_entry(environment.migrated, built, "checkpoint", checkpoint=content))
    environment.run(append_entry(environment.migrated, built, "record"))
    result = verify(environment, built, mode)
    assert result.status is IntegrityStatus.BROKEN, result
    assert (result.broken_sequence, result.reason) == (4, reason)
    assert result.checkpoints_checked == 1


def test_valid_checkpoint_with_its_own_encoding_is_intact(environment: VerifyEnvironment) -> None:
    """Control del caso anterior: la misma firma en su base64 canónico verifica."""
    built = environment.run(build_chain(environment.migrated, "organization", ["record"] * 3))
    environment.run(append_entry(environment.migrated, built, "checkpoint"))
    assert verify(environment, built).status is IntegrityStatus.INTACT


def test_noncanonical_public_key_is_not_used(environment: VerifyEnvironment) -> None:
    """Una clave publicada en base64 no canónico no verifica nada (``unknown_key``)."""
    built = environment.run(build_chain(environment.migrated, "organization", ["record"] * 2))
    environment.run(append_entry(environment.migrated, built, "checkpoint"))

    class _Keys:
        def checkpoint_public_keys(self) -> Sequence[Any]:
            (key,) = environment.keys.checkpoint_public_keys()
            return [SimpleNamespace(key_id=key.key_id, public_key=_noncanonical(key.public_key))]

    service = IntegrityService(store=environment.store, keys=_Keys(), clock=environment.env.clock)
    result = verify(environment, built, service=service)
    assert (result.status, result.reason) == (IntegrityStatus.BROKEN, "unknown_key")


# --- Estado incremental ---------------------------------------------------------------------------


def test_incremental_starts_after_the_last_intact_point(environment: VerifyEnvironment) -> None:
    built = environment.run(build_chain(environment.migrated, "plant", ["record"] * 4))
    full = verify(environment, built, VerificationMode.FULL)
    assert full.to_sequence == 4
    for _ in range(3):
        environment.run(append_entry(environment.migrated, built, "record"))

    seeds = iter([bytes(range(16))])
    service = environment.service(random_bytes=lambda size: next(seeds)[:size])
    result = verify(environment, built, VerificationMode.INCREMENTAL, service)
    assert result.status is IntegrityStatus.INTACT
    assert (result.from_sequence, result.to_sequence) == (5, 7)
    assert result.canonical_checked == 3  # lote de 3 < 100: todo el lote
    assert result.sample_seed == bytes(range(16)).hex()

    again = verify(environment, built, VerificationMode.INCREMENTAL)
    assert again.status is IntegrityStatus.INTACT
    assert (again.from_sequence, again.to_sequence, again.canonical_checked) == (8, 7, 0)

    (latest,) = environment.run(environment.service().last_results(built.context))
    assert latest.chain == built.chain
    assert latest.to_sequence == 7 and latest.mode is VerificationMode.INCREMENTAL
    assert latest.verified_at is not None


def test_incremental_on_the_audit_chain_covers_its_own_results(
    environment: VerifyEnvironment,
) -> None:
    """Cada resultado de la auditoría entra en la propia cadena de auditoría: la incremental
    siguiente lo verifica."""
    built = environment.run(build_chain(environment.migrated, "audit", ["record", "empty"]))
    first = verify(environment, built, VerificationMode.INCREMENTAL)
    assert (first.from_sequence, first.to_sequence) == (1, 2)
    second = verify(environment, built, VerificationMode.INCREMENTAL)
    assert second.status is IntegrityStatus.INTACT
    assert (second.from_sequence, second.to_sequence) == (3, 3)


def test_broken_chain_stays_broken_until_restored(environment: VerifyEnvironment) -> None:
    """BR-NUC-58: la incremental vuelve a partir del último punto íntegro y la encuentra rota."""
    built = environment.run(build_chain(environment.migrated, "plant", ["record"] * 3))
    assert verify(environment, built, VerificationMode.FULL).intact
    for _ in range(2):
        environment.run(append_entry(environment.migrated, built, "record"))
    target = built.rows[3]
    environment.run(
        mutate(
            environment.migrated,
            built.table,
            built.id_column,
            target["record_id"],
            {"actor_display_name_snapshot": "Otra persona"},
        )
    )
    for _ in range(2):
        result = verify(environment, built, VerificationMode.INCREMENTAL)
        assert result.status is IntegrityStatus.BROKEN
        assert (result.from_sequence, result.broken_sequence) == (4, 4)
        assert result.reason == "record_hash_mismatch"
    assert len(compromised(environment, built.organization_id)) == 2
    results = [
        _as_json(e["filters_json"])["result"]
        for e in verifications(environment, built.organization_id)
    ]
    assert results == ["intact", "broken", "broken"]


def test_full_break_before_the_incremental_point_stays_broken(
    environment: VerifyEnvironment,
) -> None:
    """BR-NUC-58: la completa encuentra una rotura por detrás del punto de la incremental; la
    incremental siguiente parte de antes de la rotura y la vuelve a encontrar (no «intact» desde el
    punto posterior), y ``last_results`` la sigue dando rota. Tras restaurar, una completa íntegra
    cierra la rotura y la incremental vuelve a partir de su punto."""
    built = environment.run(build_chain(environment.migrated, "plant", ["record"] * 3))
    assert verify(environment, built, VerificationMode.FULL).intact
    environment.run(append_entry(environment.migrated, built, "record"))
    point = verify(environment, built, VerificationMode.INCREMENTAL)
    assert (point.status, point.to_sequence) == (IntegrityStatus.INTACT, 4)
    target = built.rows[1]
    original = target["schema_version"]
    environment.run(
        mutate(
            environment.migrated,
            built.table,
            built.id_column,
            target["record_id"],
            {"schema_version": original + 5},
        )
    )
    full = verify(environment, built, VerificationMode.FULL)
    assert (full.status, full.broken_sequence) == (IntegrityStatus.BROKEN, 2)
    environment.run(append_entry(environment.migrated, built, "record"))

    for _ in range(2):
        again = verify(environment, built, VerificationMode.INCREMENTAL)
        assert (again.status, again.from_sequence, again.broken_sequence, again.reason) == (
            IntegrityStatus.BROKEN,
            1,
            2,
            "record_hash_mismatch",
        )
        (latest,) = environment.run(environment.service().last_results(built.context))
        assert (latest.status, latest.broken_sequence) == (IntegrityStatus.BROKEN, 2)
    assert len(compromised(environment, built.organization_id)) == 3

    # Restauración: la fila vuelve a su valor. Una incremental íntegra no cierra la rotura (sigue
    # partiendo de antes de ella); una completa íntegra sí.
    environment.run(
        mutate(
            environment.migrated,
            built.table,
            built.id_column,
            target["record_id"],
            {"schema_version": original},
        )
    )
    for _ in range(2):
        incremental = verify(environment, built, VerificationMode.INCREMENTAL)
        assert (incremental.status, incremental.from_sequence) == (IntegrityStatus.INTACT, 1)
    restored = verify(environment, built, VerificationMode.FULL)
    assert (restored.status, restored.to_sequence) == (IntegrityStatus.INTACT, 5)
    environment.run(append_entry(environment.migrated, built, "record"))
    after = verify(environment, built, VerificationMode.INCREMENTAL)
    assert (after.status, after.from_sequence, after.to_sequence) == (IntegrityStatus.INTACT, 6, 6)


@pytest.mark.parametrize("altered", [4, 5])
def test_incremental_broken_before_its_start_goes_back_to_the_earlier_point(
    environment: VerifyEnvironment, altered: int
) -> None:
    """La rotura abierta más baja manda: un punto íntegro anterior a ella sigue valiendo como
    partida (no hace falta volver a la génesis). Con la rotura justo en el punto de la
    incremental (5), ese punto ya no vale: su hash es el de una fila alterada."""
    built = environment.run(build_chain(environment.migrated, "organization", ["record"] * 2))
    assert verify(environment, built, VerificationMode.FULL).to_sequence == 2
    for _ in range(3):
        environment.run(append_entry(environment.migrated, built, "record"))
    assert verify(environment, built, VerificationMode.INCREMENTAL).to_sequence == 5
    environment.run(
        mutate(
            environment.migrated,
            built.table,
            built.id_column,
            built.rows[altered - 1]["record_id"],
            {"actor_display_name_snapshot": "Otra persona"},
        )
    )
    assert verify(environment, built, VerificationMode.ON_DEMAND).broken_sequence == altered
    again = verify(environment, built, VerificationMode.INCREMENTAL)
    assert (again.status, again.from_sequence, again.broken_sequence) == (
        IntegrityStatus.BROKEN,
        3,
        altered,
    )


@pytest.mark.parametrize("shape", ["plant", "audit"])
@pytest.mark.parametrize("deleted", [3, 5])
def test_deleted_last_row_of_a_batch_is_a_sequence_gap(
    environment: VerifyEnvironment, shape: Any, deleted: int
) -> None:
    """Falta la última fila de un lote (3, con lotes de 3) o de la cadena (5) sin tocar la cabeza:
    ``sequence_gap`` en esa secuencia (la ventana del lote no la ve; la cuenta de filas sí)."""
    built = environment.run(build_chain(environment.migrated, shape, ["record"] * 5))
    environment.run(
        delete_entry(
            environment.migrated,
            built.table,
            built.id_column,
            built.rows[deleted - 1][built.id_column],
        )
    )
    for mode in (VerificationMode.FULL, VerificationMode.INCREMENTAL):
        result = verify(environment, built, mode, environment.service(batch_size=3))
        assert (result.status, result.broken_sequence, result.reason) == (
            IntegrityStatus.BROKEN,
            deleted,
            "sequence_gap",
        )
        assert result.to_sequence == deleted - 1
        if mode is VerificationMode.FULL:
            # En la auditoría, el resultado de la completa entra detrás: después ya hay una fila
            # tras el hueco (la ventana lo ve y nombra esa fila).
            assert result.broken_entry_id is None


def test_incremental_does_not_rescan_but_full_does(environment: VerifyEnvironment) -> None:
    """Lo anterior al punto íntegro es de la completa (la mensual), no de la diaria."""
    built = environment.run(build_chain(environment.migrated, "plant", ["record"] * 4))
    assert verify(environment, built, VerificationMode.FULL).intact
    environment.run(
        mutate(
            environment.migrated,
            built.table,
            built.id_column,
            built.rows[1]["record_id"],
            {"schema_version": 7},
        )
    )
    assert verify(environment, built, VerificationMode.INCREMENTAL).intact
    full = verify(environment, built, VerificationMode.FULL)
    assert (full.status, full.broken_sequence) == (IntegrityStatus.BROKEN, 2)
    on_demand = verify(environment, built, VerificationMode.ON_DEMAND)
    assert (on_demand.status, on_demand.broken_sequence) == (IntegrityStatus.BROKEN, 2)


def test_sample_is_one_percent_and_at_least_one_hundred_per_batch(
    environment: VerifyEnvironment,
) -> None:
    built = environment.run(build_chain(environment.migrated, "organization", ["record"] * 250))
    result = verify(
        environment,
        built,
        VerificationMode.INCREMENTAL,
        environment.service(batch_size=120),
    )
    assert result.status is IntegrityStatus.INTACT
    assert result.canonical_checked == 100 + 100 + 10  # lotes de 120, 120 y 10


# --- Cabeza ---------------------------------------------------------------------------------------


def test_head_with_another_hash_is_head_mismatch(environment: VerifyEnvironment) -> None:
    built = environment.run(build_chain(environment.migrated, "organization", ["record"] * 3))
    environment.run(mutate_head(environment.migrated, built, {"last_hash": "e" * 64}))
    result = verify(environment, built)
    assert (result.status, result.broken_sequence, result.reason) == (
        IntegrityStatus.BROKEN,
        3,
        "head_mismatch",
    )


def test_records_beyond_the_head_are_head_mismatch(environment: VerifyEnvironment) -> None:
    built = environment.run(build_chain(environment.migrated, "plant", ["record"] * 4))
    environment.run(
        mutate_head(
            environment.migrated,
            built,
            {"last_sequence": 2, "last_hash": built.rows[1]["record_hash"]},
        )
    )
    result = verify(environment, built)
    assert (result.status, result.broken_sequence, result.reason) == (
        IntegrityStatus.BROKEN,
        3,
        "head_mismatch",
    )


def test_incremental_start_beyond_the_head_is_head_mismatch(
    environment: VerifyEnvironment,
) -> None:
    """La cabeza retrocede por debajo del último punto verificado (restauración parcial)."""
    built = environment.run(build_chain(environment.migrated, "plant", ["record"] * 4))
    assert verify(environment, built).intact
    environment.run(
        mutate_head(
            environment.migrated,
            built,
            {"last_sequence": 2, "last_hash": built.rows[1]["record_hash"]},
        )
    )
    result = verify(environment, built, VerificationMode.INCREMENTAL)
    assert (result.status, result.broken_sequence, result.reason) == (
        IntegrityStatus.BROKEN,
        3,
        "head_mismatch",
    )


# --- Oráculo del sobre ----------------------------------------------------------------------------

_RECOMPUTE_LEDGER = """
UPDATE ledger.ledger_record AS r SET record_hash = encode(public.digest(
    ledger.vigia_canonical_envelope(r) || convert_to(r.previous_hash, 'UTF8'), 'sha256'), 'hex')
WHERE r.record_id = $1 RETURNING r.record_hash
"""
_RECOMPUTE_AUDIT = """
UPDATE shared.audit_entry AS a SET entry_hash = encode(public.digest(
    shared.vigia_canonical_audit_envelope(a) || convert_to(a.previous_hash, 'UTF8'), 'sha256'),
    'hex')
WHERE a.entry_id = $1 RETURNING a.entry_hash
"""


async def _rehash(
    migrated: MigratedDatabase, built: BuiltChain, entry_id: uuid.UUID, *, head: bool = True
) -> None:
    """Recalcula el hash de la fila con la función de la base y, con ``head``, lo lleva a la
    cabeza (la fila es la última)."""
    connection = await migrated.connect()
    try:
        async with connection.transaction():
            table = "shared.audit_entry" if built.shape == "audit" else "ledger.ledger_record"
            triggers = await connection.fetch(
                "SELECT DISTINCT c.oid::regclass::text AS relation FROM pg_trigger AS t"
                " JOIN pg_class AS c ON c.oid = t.tgrelid WHERE NOT t.tgisinternal"
                " AND (c.oid = $1::regclass OR c.oid IN"
                " (SELECT inhrelid FROM pg_inherits WHERE inhparent = $1::regclass))",
                table,
            )
            for row in triggers:
                await connection.execute(f"ALTER TABLE {row['relation']} DISABLE TRIGGER USER")
            query = _RECOMPUTE_AUDIT if built.shape == "audit" else _RECOMPUTE_LEDGER
            new_hash = await connection.fetchval(query, entry_id)
            for row in triggers:
                await connection.execute(f"ALTER TABLE {row['relation']} ENABLE TRIGGER USER")
            if not head:
                return
            await connection.execute(
                "UPDATE ledger.chain_head SET last_hash = $1 WHERE organization_id = $2"
                " AND kind = $3 AND plant_id IS NOT DISTINCT FROM $4",
                new_hash,
                built.organization_id,
                built.kind,
                built.plant_id,
            )
    finally:
        await connection.close()


_OPTIONAL = st.one_of(st.none(), st.uuids())


@settings(
    max_examples=40,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    shape=st.sampled_from(("organization", "audit")),
    name=display_names,
    role=st.one_of(st.none(), st.sampled_from(("coordinator_sst", "platform_operator"))),
    concession=_OPTIONAL,
    zone=_OPTIONAL,
    node=_OPTIONAL,
    stamp=st.sampled_from(BOUNDARY_TIMESTAMPS)
    | st.integers(0, 999).map(
        lambda ms: datetime(2026, 9, 30, 12, tzinfo=UTC) + timedelta(milliseconds=ms)
    ),
    resource=st.booleans(),
    count=st.one_of(st.none(), st.integers(0, 2**31 - 1)),
)
def test_scan_envelope_matches_the_database_function(
    environment: VerifyEnvironment,
    shape: Any,
    name: str,
    role: str | None,
    concession: uuid.UUID | None,
    zone: uuid.UUID | None,
    node: uuid.UUID | None,
    stamp: datetime,
    resource: bool,
    count: int | None,
) -> None:
    built = environment.run(build_chain(environment.migrated, shape, ["record"]))
    row = built.rows[0]
    changes: dict[str, Any] = {
        "actor_display_name_snapshot": name,
        "actor_role_in_use": role,
        "actor_concession_id": concession,
        "scope_zone_id": zone,
    }
    if shape == "audit":
        changes |= {
            "occurred_at": stamp,
            "result_count": count,
            "resource_kind": "evidence" if resource else None,
            "resource_id": uuid.uuid4() if resource else None,
        }
    else:
        changes |= {"received_at": stamp, "scope_node_id": node}
    entry_id = row[built.id_column]
    environment.run(mutate(environment.migrated, built.table, built.id_column, entry_id, changes))
    environment.run(_rehash(environment.migrated, built, row[built.id_column]))
    start = VerifiedPoint(0, genesis_hash(str(built.organization_id), built.plant_text))
    head = environment.run(environment.store.head(built.context, built.chain))
    found = environment.run(
        sql_pass(environment.store, built.context, built.chain, start=start, head=head)
    )
    assert found is None, (changes, found)


@pytest.mark.parametrize("shape", ["plant", "organization", "audit"])
@pytest.mark.parametrize("batch_size", [2, 10])
def test_relinked_row_with_a_consistent_hash_breaks_at_its_link(
    environment: VerifyEnvironment, shape: Any, batch_size: int
) -> None:
    """Fila 3 con otro ``previous_hash`` y su propio hash recalculado con la función de la base:
    solo el enlace con la fila anterior (``LAG``, o el hash del lote anterior con lotes de 2) la
    delata, y en esa secuencia."""
    built = environment.run(build_chain(environment.migrated, shape, ["record"] * 5))
    target = built.rows[2][built.id_column]
    environment.run(
        mutate(
            environment.migrated, built.table, built.id_column, target, {"previous_hash": "c" * 64}
        )
    )
    environment.run(_rehash(environment.migrated, built, target, head=False))
    result = verify(environment, built, service=environment.service(batch_size=batch_size))
    assert (result.status, result.broken_sequence, result.reason) == (
        IntegrityStatus.BROKEN,
        3,
        "previous_hash_mismatch",
    )


@pytest.mark.parametrize("shape", ["plant", "audit"])
@pytest.mark.parametrize("microseconds", [1, 999])
def test_stamp_moved_below_the_millisecond_breaks(
    environment: VerifyEnvironment, shape: Any, microseconds: int
) -> None:
    """El sobre escribe la marca truncada al milisegundo: sin la guarda de milisegundos, mover la
    marca persistida por debajo de él no cambiaría el hash."""
    built = environment.run(build_chain(environment.migrated, shape, ["record"] * 3))
    row = built.rows[1]
    column = "occurred_at" if shape == "audit" else "received_at"
    environment.run(
        mutate(
            environment.migrated,
            built.table,
            built.id_column,
            row[built.id_column],
            {column: row[column] + timedelta(microseconds=microseconds)},
        )
    )
    result = verify(environment, built)
    assert (result.status, result.broken_sequence, result.reason) == (
        IntegrityStatus.BROKEN,
        2,
        "malformed",
    )


# --- Fallos a mitad de operación ------------------------------------------------------------------


RECORD_STATEMENTS = 2


@pytest.mark.parametrize(
    "fault",
    [Fault(commit=True), *(Fault(statement=n) for n in range(1, RECORD_STATEMENTS + 1))],
)
def test_fault_while_recording_leaves_no_entry_and_no_event(
    environment: VerifyEnvironment, fault: Fault
) -> None:
    built = environment.run(build_chain(environment.migrated, "plant", ["record"] * 3))
    environment.run(
        mutate(
            environment.migrated,
            built.table,
            built.id_column,
            built.rows[1]["record_id"],
            {"previous_hash": "a" * 64},
        )
    )
    environment.env.database.next_fault = fault
    with pytest.raises((InjectedFault, TemporarilyUnavailable)):
        verify(environment, built)
    environment.env.database.next_fault = None
    assert verifications(environment, built.organization_id) == []
    assert compromised(environment, built.organization_id) == []

    result = verify(environment, built)
    assert (result.status, result.broken_sequence) == (IntegrityStatus.BROKEN, 2)
    # Las dos sentencias de la transacción (entrada y evento) están cubiertas por los fallos.
    assert environment.env.database.probe.statements[-1] == RECORD_STATEMENTS
    assert len(verifications(environment, built.organization_id)) == 1
    assert len(compromised(environment, built.organization_id)) == 1


class _FailingScan:
    """``IntegrityStore`` cuyo paso 1 falla en el lote ``fail_at`` (base caída a mitad)."""

    def __init__(self, store: SqlIntegrityStore, fail_at: int) -> None:
        self._store = store
        self._fail_at = fail_at
        self._calls = 0

    async def scan(self, *args: Any) -> BatchScan:
        self._calls += 1
        if self._calls == self._fail_at:
            raise TemporarilyUnavailable()
        return await self._store.scan(*args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


@pytest.mark.parametrize("fail_at", [1, 2])
def test_database_down_mid_walk_records_nothing(
    environment: VerifyEnvironment, fail_at: int
) -> None:
    built = environment.run(build_chain(environment.migrated, "plant", ["record"] * 5))
    service = IntegrityService(
        store=_FailingScan(environment.store, fail_at),  # type: ignore[arg-type]
        keys=environment.keys,
        clock=environment.env.clock,
        batch_size=2,
    )
    with pytest.raises(TemporarilyUnavailable):
        verify(environment, built, service=service)
    assert verifications(environment, built.organization_id) == []
    assert verify(environment, built).intact


# --- Worker -------------------------------------------------------------------------------------


def test_worker_statement_timeout_is_30_seconds(environment: VerifyEnvironment) -> None:
    context = BuiltChain("organization", uuid.uuid4(), None).context
    assert environment.env.database.process is ProcessKind.WORKER
    rows = environment.run(environment.env.database.read(context, text("SHOW statement_timeout")))
    assert rows[0][0] == "30s"


def test_store_refuses_the_api_process(environment: VerifyEnvironment) -> None:
    for database in (SimpleNamespace(process=ProcessKind.API), SimpleNamespace()):
        with pytest.raises(ValueError, match="worker"):
            SqlIntegrityStore(
                database=database,  # type: ignore[arg-type]
                audit=environment.env.audit,
                outbox=environment.env.outbox,  # type: ignore[arg-type]
            )


# --- Tareas ---------------------------------------------------------------------------------------


def test_tasks_are_registered_and_verify_every_chain(environment: VerifyEnvironment) -> None:
    registry = PeriodicTaskRegistry()
    incremental, full = register_verify_chains(registry, environment.service())
    assert incremental.task_name == VERIFY_CHAINS_INCREMENTAL
    assert full.task_name == VERIFY_CHAINS_FULL
    assert incremental.schedule == Schedule.daily(hour=1)
    assert full.schedule == Schedule.monthly(day=1, hour=2)
    assert incremental.unit.value == full.unit.value == "U-02"

    organization_id = uuid.uuid4()
    for shape in ("plant", "organization", "audit"):
        steps = ["record"] * 2
        environment.run(
            build_chain(environment.migrated, shape, steps, organization_id=organization_id)
        )
    context = BuiltChain("audit", organization_id, None).context

    async def once(task: Any) -> None:
        async with environment.env.database.transaction(context) as transaction:
            await task.handler(transaction)

    for task, mode in ((full, "full"), (incremental, "incremental")):
        before = len(verifications(environment, organization_id))
        environment.run(once(task))
        entries = verifications(environment, organization_id)[before:]
        assert len(entries) == 3
        assert {_as_json(e["filters_json"])["mode"] for e in entries} == {mode}
        assert {_as_json(e["filters_json"])["result"] for e in entries} == {"intact"}

    results = environment.run(environment.service().last_results(context))
    assert len(results) == 3
    assert [r.chain for r in results] == sorted(
        (r.chain for r in results), key=CheckpointChain.sort_key
    )
    assert {r.chain for r in results} >= {CheckpointChain.audit(), CheckpointChain.organization()}
    assert all(r.mode is VerificationMode.INCREMENTAL and r.intact for r in results)
