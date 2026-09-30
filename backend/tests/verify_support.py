"""Entorno, cadenas, alteraciones y referencia en Python del motor de verificación (TASK-118).

Lo comparten ``tests/properties/test_verify_mutations.py`` (PR-NUC-14),
``tests/properties/test_verify_sql_vs_python.py`` (PR-NUC-49),
``tests/integration/test_integrity_verify.py`` y ``tests/benchmarks/test_verify_throughput.py``:

- ``verify_environment``: base migrada, escritor, auditoría y bandeja reales como ``vigia_app``
  (``writer_support``) y el motor sobre ``SqlIntegrityStore`` en un ``shared.db`` de worker; las
  claves ``checkpoint`` son de prueba (``CHECKPOINT_KEY``).
- ``build_chain``: una cadena escrita **como ``vigia_app``** con ``INSERT`` directos (el disparador
  encadena como en producción): registros con y sin ``source_key``, auditoría con y sin filtros, y
  puntos de control firmados que cubren la cabeza.
- ``mutate``: alteración directa de columnas persistidas **como superusuario**, con los
  disparadores de solo anexar desactivados en la tabla y sus particiones durante la transacción y
  restaurados en su modo (``ENABLE ALWAYS``) antes de confirmar: la "mutación con un rol
  privilegiado" de PR-NUC-14.
- ``mutations``: el generador de alteraciones de cada columna persistida del registro (o de la
  entrada de auditoría), siempre con un valor distinto y dentro de las restricciones de la tabla.
- ``reference_walk``: el recorrido de referencia en Python sobre las filas leídas por
  ``vigia_app``: ``chain_walk`` (forma del paquete) más lo que la forma del paquete no ve de los
  bytes persistidos (SHA-256 de los bytes, marca en milisegundos y ``source_key`` del tipo).

Solo datos generados.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from hypothesis import strategies as st
from vigia_contracts.canonical import canonicalize

from tests.identity_db import MigratedDatabase
from tests.ledger_database import (
    audit_values,
    canonical_timestamp,
    insert_audit,
    insert_record,
    record_values,
    set_organization,
)
from tests.properties.envelope_strategies import contents, display_names
from tests.verifier_packages import SigningKey, signed_message
from tests.writer_support import (
    ORDER_TYPE,
    ORGANIZATION_TYPE,
    ZONE_TYPE,
    WriterEnvironment,
    unit_context,
    writer_environment,
)
from vigia_platform.ledger.adapters.integrity_store import SqlIntegrityStore
from vigia_platform.ledger.chain.chain_walk import ChainRef, ChainWalker, genesis_hash
from vigia_platform.ledger.chain.checkpoints import CheckpointChain, CheckpointPublicKey
from vigia_platform.ledger.chain.verify import IntegrityService, audit_entry, ledger_entry
from vigia_platform.ledger.content_paths import locate
from vigia_platform.shared.context import ActorKind, ActorUnit, ScopeContext
from vigia_platform.shared.signing.keys import KeyStatus

CHECKPOINT_KEY = SigningKey.from_seed("checkpoint-verify-1", b"motor de verificacion sintetico")
FOREIGN_KEY = SigningKey.from_seed("checkpoint-ajena", b"clave que no se publica")
KEY_WINDOW = (datetime.fromisoformat("2026-01-01T00:00:00+00:00"),) * 2

Shape = Literal["plant", "organization", "audit"]
Step = Literal["record", "keyed", "empty", "checkpoint"]
"""``record``: registro sin clave (o entrada con filtros); ``keyed``: con ``source_key``;
``empty``: entrada de auditoría sin filtros; ``checkpoint``: punto de control firmado."""


class StaticKeys:
    """``CheckpointKeySource`` con claves fijas de prueba."""

    def __init__(self, *keys: SigningKey) -> None:
        self._keys = keys

    def checkpoint_public_keys(self) -> tuple[CheckpointPublicKey, ...]:
        return tuple(
            CheckpointPublicKey(
                key_id=key.key_id,
                public_key=key.public_b64,
                status=KeyStatus.ACTIVE,
                valid_from=KEY_WINDOW[0],
                valid_until=KEY_WINDOW[1] + timedelta(days=365),
            )
            for key in self._keys
        )


@dataclass
class VerifyEnvironment:
    env: WriterEnvironment
    store: SqlIntegrityStore
    keys: StaticKeys

    @property
    def migrated(self) -> MigratedDatabase:
        return self.env.migrated

    def service(self, *, batch_size: int = 10_000, **extra: Any) -> IntegrityService:
        return IntegrityService(
            store=self.store,
            keys=self.keys,
            clock=self.env.clock,
            batch_size=batch_size,
            **extra,
        )

    def run[T](self, awaitable: Any) -> T:
        result: T = self.env.loop.run(awaitable)
        return result


@contextlib.contextmanager
def verify_environment(migrated: MigratedDatabase) -> Iterator[VerifyEnvironment]:
    with writer_environment(migrated) as env:
        assert env.outbox is not None
        store = SqlIntegrityStore(database=env.database, audit=env.audit, outbox=env.outbox)
        yield VerifyEnvironment(env, store, StaticKeys(CHECKPOINT_KEY))


def system_context(organization_id: uuid.UUID) -> ScopeContext:
    return unit_context(organization_id, ActorUnit.U02, kind=ActorKind.SYSTEM)


# --- Cadenas --------------------------------------------------------------------------------------


@dataclass
class BuiltChain:
    shape: Shape
    organization_id: uuid.UUID
    plant_id: uuid.UUID | None
    rows: list[Any] = field(default_factory=list)

    @property
    def chain(self) -> CheckpointChain:
        if self.shape == "audit":
            return CheckpointChain.audit()
        if self.shape == "organization":
            return CheckpointChain.organization()
        assert self.plant_id is not None
        return CheckpointChain.plant(self.plant_id)

    @property
    def plant_text(self) -> str | None:
        return None if self.plant_id is None else str(self.plant_id)

    @property
    def kind(self) -> str:
        return "audit" if self.shape == "audit" else "ledger"

    @property
    def hash_column(self) -> str:
        return "entry_hash" if self.shape == "audit" else "record_hash"

    @property
    def id_column(self) -> str:
        return "entry_id" if self.shape == "audit" else "record_id"

    @property
    def table(self) -> str:
        return "shared.audit_entry" if self.shape == "audit" else "ledger.ledger_record"

    @property
    def context(self) -> ScopeContext:
        return system_context(self.organization_id)


def checkpoint_content(
    built: BuiltChain,
    last: Mapping[str, Any] | None,
    key: SigningKey = CHECKPOINT_KEY,
) -> dict[str, Any]:
    """Contenido de un punto de control que cubre ``last`` (la cabeza), firmado con ``key``."""
    if last is None:
        covered_sequence = 0
        covered_hash = genesis_hash(str(built.organization_id), built.plant_text)
        taken_at = "2026-09-29T10:30:00.000Z"
    else:
        covered_sequence = int(last["chain_sequence"])
        covered_hash = str(last[built.hash_column])
        stamp = last["occurred_at" if built.shape == "audit" else "received_at"]
        taken_at = canonical_timestamp(stamp)
    message = signed_message(
        built.kind,
        str(built.organization_id),
        None if built.plant_id is None else str(built.plant_id),
        covered_sequence,
        covered_hash,
        taken_at,
    )
    return {
        "covered_sequence": covered_sequence,
        "covered_hash": covered_hash,
        "taken_at": taken_at,
        "key_id": key.key_id,
        "signature": key.sign(message),
    }


async def insert_entry(
    connection: Any,
    built: BuiltChain,
    step: Step,
    document: Mapping[str, Any] | None = None,
    *,
    raw_content: bytes | None = None,
    checkpoint: Mapping[str, Any] | None = None,
) -> Any:
    """Una fila más de la cadena, como ``vigia_app`` (dentro de una transacción con contexto)."""
    last = built.rows[-1] if built.rows else None
    document = dict(document if document is not None else {"n": len(built.rows) + 1})
    if built.shape == "audit":
        values = audit_values(built.organization_id, filters=None if step == "empty" else document)
        if step == "checkpoint":
            values["operation"] = "checkpoint"
            values["filters"] = canonicalize(dict(checkpoint or checkpoint_content(built, last)))
        elif raw_content is not None:
            values["filters"] = raw_content
        row = await insert_audit(connection, values)
    else:
        if step == "checkpoint":
            values = record_values(
                built.organization_id,
                built.plant_id,
                record_type="checkpoint",
                content=checkpoint or checkpoint_content(built, last),
            )
        elif step == "keyed" and built.shape == "plant":
            probe_id = str(uuid.uuid4())
            values = record_values(
                built.organization_id,
                built.plant_id,
                record_type=ORDER_TYPE,
                content={**document, "probe_id": probe_id},
                source_key=probe_id,
            )
        else:
            record_type = ZONE_TYPE if built.shape == "plant" else ORGANIZATION_TYPE
            values = record_values(
                built.organization_id, built.plant_id, record_type=record_type, content=document
            )
        if raw_content is not None:
            values["content"] = raw_content
        row = await insert_record(connection, values)
    built.rows.append(row)
    return row


async def build_chain(
    migrated: MigratedDatabase,
    shape: Shape,
    steps: Sequence[Step],
    documents: Sequence[Mapping[str, Any]] = (),
    *,
    organization_id: uuid.UUID | None = None,
) -> BuiltChain:
    """Cadena nueva de ``shape`` con una fila por paso; los puntos de control cubren la cabeza."""
    organization_id = organization_id or uuid.uuid4()
    plant_id = uuid.uuid4() if shape == "plant" else None
    built = BuiltChain(shape, organization_id, plant_id)
    connection = await migrated.connect("vigia_app")
    try:
        async with connection.transaction():
            await set_organization(connection, organization_id)
            for index, step in enumerate(steps):
                document = documents[index] if index < len(documents) else None
                await insert_entry(connection, built, step, document)
    finally:
        await connection.close()
    return built


async def append_entry(
    migrated: MigratedDatabase, built: BuiltChain, step: Step, **options: Any
) -> Any:
    connection = await migrated.connect("vigia_app")
    try:
        async with connection.transaction():
            await set_organization(connection, built.organization_id)
            return await insert_entry(connection, built, step, **options)
    finally:
        await connection.close()


# --- Alteraciones como superusuario ---------------------------------------------------------------

_TRIGGERS = """
SELECT c.oid::regclass::text AS relation, t.tgname::text AS tgname, t.tgenabled::text AS tgenabled
FROM pg_trigger AS t JOIN pg_class AS c ON c.oid = t.tgrelid
WHERE NOT t.tgisinternal
  AND (c.oid = $1::regclass
       OR c.oid IN (SELECT inhrelid FROM pg_inherits WHERE inhparent = $1::regclass))
"""
_MODES = {"A": "ENABLE ALWAYS TRIGGER", "O": "ENABLE TRIGGER", "R": "ENABLE REPLICA TRIGGER"}


async def mutate(
    migrated: MigratedDatabase,
    table: str,
    id_column: str,
    entry_id: uuid.UUID,
    changes: Mapping[str, Any],
) -> None:
    """``UPDATE`` directo de ``changes`` en la fila ``entry_id`` saltando la guarda de solo anexar.

    Los disparadores de la tabla y de sus particiones se desactivan y se restauran en su modo en
    la misma transacción: nada queda desprotegido al confirmar.
    """
    assignments = ", ".join(f"{column} = ${index}" for index, column in enumerate(changes, start=1))
    await _unguarded(
        migrated,
        table,
        f"UPDATE {table} SET {assignments}"  # noqa: S608 - nombres fijos de la prueba
        f" WHERE {id_column} = ${len(changes) + 1}",
        [*changes.values(), entry_id],
        "UPDATE 1",
    )


async def delete_entry(
    migrated: MigratedDatabase, table: str, id_column: str, entry_id: uuid.UUID
) -> None:
    """``DELETE`` directo de la fila ``entry_id`` (sin tocar la cabeza), como ``mutate``."""
    await _unguarded(
        migrated,
        table,
        f"DELETE FROM {table} WHERE {id_column} = $1",  # noqa: S608 - nombres fijos de la prueba
        [entry_id],
        "DELETE 1",
    )


async def _unguarded(
    migrated: MigratedDatabase,
    table: str,
    statement: str,
    arguments: Sequence[Any],
    expected: str,
) -> None:
    """Ejecuta ``statement`` con los disparadores de ``table`` desactivados en la transacción."""
    connection = await migrated.connect()
    try:
        async with connection.transaction():
            triggers = await connection.fetch(_TRIGGERS, table)
            for relation in sorted({row["relation"] for row in triggers}):
                await connection.execute(f"ALTER TABLE {relation} DISABLE TRIGGER USER")
            status = await connection.execute(statement, *arguments)
            assert status == expected, status
            for row in triggers:
                mode = _MODES[row["tgenabled"]]
                await connection.execute(f'ALTER TABLE {row["relation"]} {mode} "{row["tgname"]}"')
            restored = await connection.fetch(_TRIGGERS, table)
            assert sorted(map(tuple, restored)) == sorted(map(tuple, triggers))
    finally:
        await connection.close()


async def mutate_head(
    migrated: MigratedDatabase, built: BuiltChain, changes: Mapping[str, Any]
) -> None:
    """``UPDATE`` directo de la cabeza de la cadena como superusuario."""
    connection = await migrated.connect()
    try:
        assignments = ", ".join(f"{c} = ${i}" for i, c in enumerate(changes, start=1))
        await connection.execute(
            f"UPDATE ledger.chain_head SET {assignments}"  # noqa: S608 - nombres fijos
            f" WHERE organization_id = ${len(changes) + 1} AND kind = ${len(changes) + 2}"
            f" AND plant_id IS NOT DISTINCT FROM ${len(changes) + 3}",
            *changes.values(),
            built.organization_id,
            built.kind,
            built.plant_id,
        )
    finally:
        await connection.close()


# --- Generadores ----------------------------------------------------------------------------------

LEDGER_COLUMNS = (
    "record_id",
    "organization_id",
    "plant_id",
    "chain_sequence",
    "record_type",
    "schema_version",
    "actor_kind",
    "actor_id",
    "actor_display_name_snapshot",
    "actor_role_in_use",
    "actor_concession_id",
    "actor_unit",
    "scope_plant_id",
    "scope_zone_id",
    "scope_node_id",
    "correlation_id",
    "received_at",
    "source_key",
    "content",
    "content_hash",
    "previous_hash",
    "record_hash",
)
"""Columnas persistidas de ``LedgerRecord`` (domain-entities §3.1). ``content_json`` es una
columna generada de ``content`` (no se escribe: se altera alterando ``content``) y
``occurred_at`` no es un campo de la entidad ni del sobre (marca de la línea de tiempo)."""

AUDIT_COLUMNS = (
    "entry_id",
    "organization_id",
    "chain_sequence",
    "actor_kind",
    "actor_id",
    "actor_display_name_snapshot",
    "actor_role_in_use",
    "actor_concession_id",
    "actor_unit",
    "operation",
    "scope_plant_id",
    "scope_zone_id",
    "resource_kind",
    "resource_id",
    "filters",
    "filters_hash",
    "result_count",
    "outcome",
    "correlation_id",
    "occurred_at",
    "previous_hash",
    "entry_hash",
)
"""Columnas persistidas de ``AuditEntry`` (``filters_json`` es generada de ``filters``)."""

_ACTOR_KINDS = ("user", "provider_user", "node", "system", "operator")
_ROLES = (
    "coordinator_sst",
    "line_manager",
    "plant_manager",
    "administrator",
    "provider_installer",
    "copasst",
    "platform_operator",
)
_UNITS = ("U-02", "U-03", "U-04")
_RECORD_TYPES = (ZONE_TYPE, ORGANIZATION_TYPE, ORDER_TYPE, "checkpoint")
_OPERATIONS = ("ledger_read", "checkpoint", "audit_read", "integrity_verification")
_OUTCOMES = ("success", "denied", "error")

hex64 = st.binary(min_size=32, max_size=32).map(bytes.hex)


def _candidates(strategy: st.SearchStrategy[Any]) -> st.SearchStrategy[tuple[Any, ...]]:
    """Dos o tres valores distintos: al aplicar, el primero que no sea el actual."""
    return st.lists(strategy, min_size=2, max_size=3, unique=True).map(tuple)


_UUIDS = st.uuids()
_OPTIONAL_UUIDS = st.one_of(st.none(), st.uuids())
_SOURCE_KEYS = st.one_of(
    st.none(),
    st.text(st.characters(min_codepoint=33, max_codepoint=126), min_size=1, max_size=64),
)
_DOCUMENTS = contents.map(lambda document: canonicalize(dict(document)))
_STAMP_DELTAS = st.one_of(
    st.integers(-(10**9), 10**9).filter(bool).map(lambda ms: timedelta(milliseconds=ms)),
    st.integers(1, 999).map(lambda us: timedelta(microseconds=us)),
)
"""Otra marca: milisegundos enteros o un cambio por debajo del milisegundo."""

_VALUES: dict[str, st.SearchStrategy[Any]] = {
    "record_id": _UUIDS,
    "entry_id": _UUIDS,
    "organization_id": _UUIDS,
    "actor_id": _UUIDS,
    "correlation_id": _UUIDS,
    "plant_id": _OPTIONAL_UUIDS,
    "actor_concession_id": _OPTIONAL_UUIDS,
    "scope_plant_id": _OPTIONAL_UUIDS,
    "scope_zone_id": _OPTIONAL_UUIDS,
    "scope_node_id": _OPTIONAL_UUIDS,
    "chain_sequence": st.integers(1, 1000),
    "record_type": st.sampled_from(_RECORD_TYPES),
    "schema_version": st.integers(1, 1000),
    "actor_kind": st.sampled_from(_ACTOR_KINDS),
    "actor_display_name_snapshot": display_names,
    "actor_role_in_use": st.one_of(st.none(), st.sampled_from(_ROLES)),
    "actor_unit": st.sampled_from(_UNITS),
    "received_at": _STAMP_DELTAS,
    "occurred_at": _STAMP_DELTAS,
    "source_key": _SOURCE_KEYS,
    "content": _DOCUMENTS,
    "filters": _DOCUMENTS,
    "content_hash": hex64,
    "previous_hash": hex64,
    "record_hash": hex64,
    "entry_hash": hex64,
    "filters_hash": st.one_of(st.none(), hex64),
    "operation": st.sampled_from(_OPERATIONS),
    "resource_kind": st.sampled_from(("ledger_record", "evidence", "zone")),
    "resource_id": _UUIDS,
    "result_count": st.one_of(st.none(), st.integers(0, 2**31 - 1)),
    "outcome": st.sampled_from(_OUTCOMES),
}


@dataclass(frozen=True)
class Mutation:
    """Alteración de una columna, generada **sin mirar la fila** (Hypothesis exige que los datos
    generados no dependan de la base); ``changes`` la concreta sobre la fila."""

    column: str
    candidates: tuple[Any, ...]
    variant: int
    """En ``content`` y ``filters``: 0 otro documento, 1 el mismo con un espacio delante, 2 el
    mismo con sangría, 3 sin documento (solo ``filters``)."""

    def changes(self, row: Mapping[str, Any]) -> dict[str, Any]:
        column, current = self.column, row[self.column]
        if column in ("resource_kind", "resource_id") and row["resource_kind"] is None:
            # ``resource_ref`` es ``{kind, id}``: las dos columnas son nulas a la vez o ninguna.
            return {"resource_kind": "ledger_record", "resource_id": uuid.UUID(int=len(row))}
        if column in ("chain_sequence", "received_at", "occurred_at"):
            # La secuencia, hacia arriba: hacia abajo la fila alterada ocupa un hueco anterior
            # (prueba aparte). La marca, en milisegundos enteros o por debajo del milisegundo.
            return {column: current + self.candidates[0]}
        if column in ("content", "filters"):
            return {column: self._document(None if current is None else bytes(current))}
        return {column: self._other(current)}

    def _other(self, current: object) -> Any:
        return next(value for value in self.candidates if value != current)

    def _document(self, current: bytes | None) -> bytes | None:
        if current is not None and self.variant == 1:
            return b" " + current
        if current is not None and self.variant == 2:
            indented = json.dumps(json.loads(current), indent=1).encode()
            if indented != current:
                return indented
        if current is not None and self.variant == 3 and self.column == "filters":
            return None
        document: bytes = self._other(current)
        return document


@st.composite
def mutations(draw: st.DrawFn, shape: Shape) -> Mutation:
    """Una alteración de una columna persistida de la tabla de ``shape``."""
    column = draw(st.sampled_from(AUDIT_COLUMNS if shape == "audit" else LEDGER_COLUMNS))
    if column in ("chain_sequence", "received_at", "occurred_at"):
        candidates: tuple[Any, ...] = (draw(_VALUES[column]),)
    elif column == "plant_id":
        # Otra planta o ninguna (la fila pasa a otra cadena): en la de organización, otra planta.
        candidates = draw(_candidates(_UUIDS))
        candidates = (*candidates, None) if draw(st.booleans()) else (None, *candidates)
    else:
        candidates = draw(_candidates(_VALUES[column]))
    return Mutation(column, candidates, draw(st.integers(0, 3)))


steps_strategy = st.lists(
    st.sampled_from(("record", "keyed", "empty", "checkpoint")), min_size=1, max_size=9
).map(lambda steps: ["record", *steps[1:]] if steps[0] == "checkpoint" else steps)
"""Pasos de una cadena: el primero nunca es un punto de control (no hay nada que cubrir)."""


def normalize_steps(shape: Shape, steps: Sequence[str]) -> list[Step]:
    """``keyed`` solo en la cadena de planta; ``empty`` solo en la auditoría."""
    result: list[Step] = []
    for step in steps:
        if (step == "keyed" and shape != "plant") or (step == "empty" and shape != "audit"):
            result.append("record")
        else:
            result.append(step)  # type: ignore[arg-type]
    return result


# --- Referencia en Python -------------------------------------------------------------------------


@dataclass(frozen=True)
class Reference:
    status: str
    sequence: int | None
    entry_id: str | None


SOURCE_KEY_PATHS = {ORDER_TYPE: "/probe_id"}
"""``source_key_path`` de los tipos que escribe ``build_chain`` (el resto no tiene clave)."""


def _source_key_ok(row: Mapping[str, Any]) -> bool:
    path = SOURCE_KEY_PATHS.get(row["record_type"])
    if path is None:
        return row["source_key"] is None
    try:
        document = json.loads(bytes(row["content"]))
    except ValueError:
        return False
    found = locate(document, path)
    value = found[0].value if len(found) == 1 else None
    return isinstance(value, str) and value == row["source_key"]


def _raw_ok(built: BuiltChain, row: Mapping[str, Any]) -> bool:
    """Lo que ``chain_walk`` no ve en la forma del paquete: bytes, milisegundos y clave."""
    if built.shape == "audit":
        stamp, data, declared = row["occurred_at"], row["filters"], row["filters_hash"]
        digest = None if data is None else hashlib.sha256(bytes(data)).hexdigest()
        return stamp.microsecond % 1000 == 0 and digest == declared
    stamp, data, declared = row["received_at"], row["content"], row["content_hash"]
    return (
        stamp.microsecond % 1000 == 0
        and hashlib.sha256(bytes(data)).hexdigest() == declared
        and _source_key_ok(row)
    )


async def chain_rows(migrated: MigratedDatabase, built: BuiltChain) -> tuple[list[Any], Any]:
    """Filas visibles para ``vigia_app`` en el orden del motor, y la cabeza."""
    connection = await migrated.connect("vigia_app")
    try:
        async with connection.transaction():
            await set_organization(connection, built.organization_id)
            if built.shape == "audit":
                rows = await connection.fetch(
                    "SELECT * FROM shared.audit_entry ORDER BY chain_sequence, entry_id"
                )
            else:
                rows = await connection.fetch(
                    "SELECT * FROM ledger.ledger_record WHERE plant_id IS NOT DISTINCT FROM $1"
                    " ORDER BY chain_sequence, record_id",
                    built.plant_id,
                )
            head = await connection.fetchrow(
                "SELECT last_sequence, last_hash FROM ledger.chain_head WHERE kind = $1"
                " AND plant_id IS NOT DISTINCT FROM $2",
                built.kind,
                built.plant_id,
            )
    finally:
        await connection.close()
    return list(rows), head


def reference_walk(
    built: BuiltChain,
    rows: Sequence[Mapping[str, Any]],
    head: Mapping[str, Any] | None,
    keys: Mapping[str, bytes],
) -> Reference:
    """El recorrido en Python: ``chain_walk`` y los bytes persistidos, fila a fila, en orden."""
    walker = ChainWalker(
        ChainRef(
            built.kind,
            str(built.organization_id),
            None if built.plant_id is None else str(built.plant_id),
        ),
        keys,
    )
    convert = audit_entry if built.shape == "audit" else ledger_entry
    for position, row in enumerate(rows, start=1):
        values = dict(row)
        if not _raw_ok(built, values):
            return Reference("broken", position, str(values[built.id_column]))
        failure = walker.feed(convert(values))
        if failure is not None:
            return Reference("broken", failure.sequence, failure.entry_id)
    declared = (
        (0, genesis_hash(str(built.organization_id), built.plant_text))
        if head is None
        else (int(head["last_sequence"]), str(head["last_hash"]))
    )
    result = walker.finish(declared)
    if result.broken is not None:
        return Reference("broken", result.broken.sequence, result.broken.entry_id)
    return Reference("intact", None, None)


def public_keys(*keys: SigningKey) -> dict[str, bytes]:
    return {key.key_id: base64.b64decode(key.public_b64) for key in keys}
