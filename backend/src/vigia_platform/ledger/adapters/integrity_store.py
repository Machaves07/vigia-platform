"""``IntegrityStore`` sobre PostgreSQL (LC-NUC-13; PAT-NUC-REN-02): el paso 1 y las lecturas del 2.

Lo que ``ledger.chain.verify`` (módulo aislado, sin SQLAlchemy) necesita de la base:

- ``head``: ``ledger.chain_head`` y la mayor secuencia guardada de la cadena **en la misma
  sentencia** (una sola instantánea): el disparador avanza la cabeza en la transacción que inserta
  el registro, así que un registro por encima de la cabeza en esa instantánea es una incoherencia.
- ``scan`` (**paso 1**): por lote ``[first, last]``, una consulta que recalcula ``content_hash``
  sobre los bytes persistidos (``filters_hash`` en la auditoría, nulo si no hay filtros) y
  ``record_hash`` (``entry_hash``) sobre el sobre canónico y el ``previous_hash`` persistido, como
  el disparador. El sobre es la misma forma fija de ``ledger.vigia_canonical_envelope``
  (``shared.vigia_canonical_audit_envelope``) escrita en la propia consulta: esas funciones llevan
  ``SET search_path`` y PostgreSQL no las inserta en línea, y llamarlas por fila cuesta unos
  210 µs (el lote de 10 000 tardaba 2,1 s: 4 181 registros por segundo en el banco, por debajo de
  NFR-NUC-01). La igualdad byte a byte con la función la prueba
  ``tests/integration/test_integrity_verify.py`` (oráculo con nombres difíciles, marcas límite y
  nulos), además de PR-NUC-14 y PR-NUC-49. Compara ``previous_hash`` con el hash de la fila
  anterior por ``LAG`` sobre ``chain_sequence`` (la primera del lote, con ``start_hash``); exige
  que la fila de la posición ``p`` tenga la secuencia ``first + p - 1``, que la marca
  (``received_at`` u ``occurred_at``) esté en milisegundos, como la fija el disparador (el sobre
  la escribe truncada: sin esto, un cambio por debajo del milisegundo pasaría), y que
  ``source_key`` sea el valor que declara ``ledger.record_type.source_key_path`` en
  ``content_json``. Devuelve solo la primera fila rota (la secuencia esperada, su identificador y
  el motivo) y el hash de la última fila del lote.
- ``checkpoints`` y ``documents`` (**paso 2**): las filas completas de los puntos de control del
  lote y el ``content_json::text`` (``filters_json::text``) de todo el lote o de la muestra.
- ``last_verified`` y ``results``: el estado incremental y los últimos resultados, leídos de las
  entradas ``integrity_verification`` de la auditoría (el resultado anterior de cada cadena).
- ``record``: la entrada ``integrity_verification`` por ``AuditWriter`` y, si la cadena está
  rota, ``integrity_compromised`` por la bandeja, en una sola transacción.

El orden de las filas es ``(chain_sequence, identificador)``: con secuencias repetidas (solo tras
una alteración), el mismo en el paso 1 y en la referencia de PR-NUC-49. La cadena de expediente
es la de ``:plant_id`` (la de organización con ``:plant_id`` nulo).

Solo en el worker (``ProcessKind.WORKER``): allí ``statement_timeout`` es de 30 s; en la API sigue
siendo de 10 s y el adaptador se niega a construirse sobre ella. Todas las consultas van con el
``ScopeContext`` recibido: la seguridad a nivel de fila limita a su organización.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.sql.elements import TextClause

from vigia_platform.ledger.application.audit_writer import (
    AuditOperation,
    AuditWriter,
    ResourceRef,
)
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.ledger.chain.chain_walk import Break, genesis_hash
from vigia_platform.ledger.chain.checkpoints import ChainKind, CheckpointChain
from vigia_platform.ledger.chain.verify import (
    INTEGRITY_COMPROMISED,
    BatchScan,
    CanonicalRow,
    HeadSnapshot,
    IntegrityResult,
    VerifiedPoint,
)
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.db import ProcessKind
from vigia_platform.shared.outbox.publish import NewEvent, OutboxPort

__all__ = ["SqlIntegrityStore"]

# Paso 1. ``IS NOT TRUE``: una comparación con NULL (una columna alterada a nulo) también rompe.
_LEDGER_SCAN: Final = text(
    """
    WITH batch AS (
        SELECT r.chain_sequence, r.record_id AS entry_id, r.previous_hash,
            r.record_hash AS entry_hash,
            r.content_hash = encode(public.digest(r.content, 'sha256'), 'hex') AS content_ok,
            r.record_hash = encode(public.digest(pg_catalog.convert_to(
                '{"actor":{"concession_id":'
                    || coalesce('"' || r.actor_concession_id::text || '"', 'null')
                || ',"display_name_snapshot":'
                    || coalesce(pg_catalog.to_json(r.actor_display_name_snapshot)::text, 'null')
                || ',"id":' || coalesce('"' || r.actor_id::text || '"', 'null')
                || ',"kind":' || coalesce(pg_catalog.to_json(r.actor_kind)::text, 'null')
                || ',"role_in_use":'
                    || coalesce(pg_catalog.to_json(r.actor_role_in_use)::text, 'null')
                || ',"unit":' || coalesce(pg_catalog.to_json(r.actor_unit)::text, 'null')
                || '},"chain_sequence":' || coalesce(r.chain_sequence::text, 'null')
                || ',"content_hash":' || coalesce(pg_catalog.to_json(r.content_hash)::text, 'null')
                || ',"correlation_id":' || coalesce('"' || r.correlation_id::text || '"', 'null')
                || ',"organization_id":'
                    || coalesce('"' || r.organization_id::text || '"', 'null')
                || ',"plant_id":' || coalesce('"' || r.plant_id::text || '"', 'null')
                || ',"received_at":' || coalesce('"' || pg_catalog.to_char(
                    r.received_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') || '"',
                    'null')
                || ',"record_id":' || coalesce('"' || r.record_id::text || '"', 'null')
                || ',"record_type":' || coalesce(pg_catalog.to_json(r.record_type)::text, 'null')
                || ',"schema_version":' || coalesce(r.schema_version::text, 'null')
                || ',"scope":{"node_id":' || coalesce('"' || r.scope_node_id::text || '"', 'null')
                || ',"plant_id":' || coalesce('"' || r.scope_plant_id::text || '"', 'null')
                || ',"zone_id":' || coalesce('"' || r.scope_zone_id::text || '"', 'null')
                || '}}' || r.previous_hash,
                'UTF8'), 'sha256'), 'hex') AS record_ok,
            r.source_key IS NOT DISTINCT FROM
                (r.content_json #>> string_to_array(substr(t.source_key_path, 2), '/'))
                AS source_ok,
            r.received_at = date_trunc('milliseconds', r.received_at) AS stamp_ok,
            row_number() OVER w AS position,
            lag(r.record_hash) OVER w AS lagged
        FROM ledger.ledger_record AS r
        LEFT JOIN ledger.record_type AS t ON t.record_type = r.record_type
        WHERE r.organization_id = :organization_id
          AND (r.plant_id = CAST(:plant_id AS uuid)
               OR (r.plant_id IS NULL AND CAST(:plant_id AS uuid) IS NULL))
          AND r.chain_sequence BETWEEN :first AND :last
        WINDOW w AS (ORDER BY r.chain_sequence, r.record_id)
    ),
    checked AS (
        SELECT entry_id, position,
            CAST(:first AS bigint) + position - 1 AS expected,
            CASE
                WHEN chain_sequence <> CAST(:first AS bigint) + position - 1 THEN 'sequence_gap'
                WHEN stamp_ok IS NOT TRUE THEN 'malformed'
                WHEN previous_hash IS DISTINCT FROM coalesce(lagged, CAST(:start_hash AS text))
                    THEN 'previous_hash_mismatch'
                WHEN content_ok IS NOT TRUE THEN 'content_hash_mismatch'
                WHEN record_ok IS NOT TRUE THEN 'record_hash_mismatch'
                WHEN source_ok IS NOT TRUE THEN 'source_key_mismatch'
            END AS reason
        FROM batch
    )
    SELECT (SELECT count(*) FROM batch) AS rows,
        (SELECT entry_hash FROM batch ORDER BY position DESC LIMIT 1) AS last_hash,
        broken.expected, broken.entry_id, broken.reason
    FROM (SELECT 1) AS one
    LEFT JOIN LATERAL (
        SELECT expected, entry_id, reason FROM checked
        WHERE reason IS NOT NULL ORDER BY position LIMIT 1
    ) AS broken ON true
    """
)

_AUDIT_SCAN: Final = text(
    """
    WITH batch AS (
        SELECT a.chain_sequence, a.entry_id, a.previous_hash, a.entry_hash,
            a.filters_hash IS NOT DISTINCT FROM encode(public.digest(a.filters, 'sha256'), 'hex')
                AS content_ok,
            a.entry_hash = encode(public.digest(pg_catalog.convert_to(
                '{"actor":{"concession_id":'
                    || coalesce('"' || a.actor_concession_id::text || '"', 'null')
                || ',"display_name_snapshot":'
                    || coalesce(pg_catalog.to_json(a.actor_display_name_snapshot)::text, 'null')
                || ',"id":' || coalesce('"' || a.actor_id::text || '"', 'null')
                || ',"kind":' || coalesce(pg_catalog.to_json(a.actor_kind)::text, 'null')
                || ',"role_in_use":'
                    || coalesce(pg_catalog.to_json(a.actor_role_in_use)::text, 'null')
                || ',"unit":' || coalesce(pg_catalog.to_json(a.actor_unit)::text, 'null')
                || '},"chain_sequence":' || coalesce(a.chain_sequence::text, 'null')
                || ',"correlation_id":' || coalesce('"' || a.correlation_id::text || '"', 'null')
                || ',"entry_id":' || coalesce('"' || a.entry_id::text || '"', 'null')
                || ',"filters_hash":' || coalesce(pg_catalog.to_json(a.filters_hash)::text, 'null')
                || ',"occurred_at":' || coalesce('"' || pg_catalog.to_char(
                    a.occurred_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') || '"',
                    'null')
                || ',"operation":' || coalesce(pg_catalog.to_json(a.operation)::text, 'null')
                || ',"organization_id":'
                    || coalesce('"' || a.organization_id::text || '"', 'null')
                || ',"outcome":' || coalesce(pg_catalog.to_json(a.outcome)::text, 'null')
                || ',"resource_ref":' || CASE
                    WHEN a.resource_kind IS NULL AND a.resource_id IS NULL THEN 'null'
                    ELSE '{"id":' || coalesce('"' || a.resource_id::text || '"', 'null')
                        || ',"kind":' || coalesce(pg_catalog.to_json(a.resource_kind)::text, 'null')
                        || '}'
                    END
                || ',"result_count":' || coalesce(a.result_count::text, 'null')
                || ',"scope":{"plant_id":' || coalesce('"' || a.scope_plant_id::text || '"', 'null')
                || ',"zone_id":' || coalesce('"' || a.scope_zone_id::text || '"', 'null')
                || '}}' || a.previous_hash,
                'UTF8'), 'sha256'), 'hex') AS record_ok,
            a.occurred_at = date_trunc('milliseconds', a.occurred_at) AS stamp_ok,
            row_number() OVER w AS position,
            lag(a.entry_hash) OVER w AS lagged
        FROM shared.audit_entry AS a
        WHERE a.organization_id = :organization_id
          AND a.chain_sequence BETWEEN :first AND :last
        WINDOW w AS (ORDER BY a.chain_sequence, a.entry_id)
    ),
    checked AS (
        SELECT entry_id, position,
            CAST(:first AS bigint) + position - 1 AS expected,
            CASE
                WHEN chain_sequence <> CAST(:first AS bigint) + position - 1 THEN 'sequence_gap'
                WHEN stamp_ok IS NOT TRUE THEN 'malformed'
                WHEN previous_hash IS DISTINCT FROM coalesce(lagged, CAST(:start_hash AS text))
                    THEN 'previous_hash_mismatch'
                WHEN content_ok IS NOT TRUE THEN 'content_hash_mismatch'
                WHEN record_ok IS NOT TRUE THEN 'record_hash_mismatch'
            END AS reason
        FROM batch
    )
    SELECT (SELECT count(*) FROM batch) AS rows,
        (SELECT entry_hash FROM batch ORDER BY position DESC LIMIT 1) AS last_hash,
        broken.expected, broken.entry_id, broken.reason
    FROM (SELECT 1) AS one
    LEFT JOIN LATERAL (
        SELECT expected, entry_id, reason FROM checked
        WHERE reason IS NOT NULL ORDER BY position LIMIT 1
    ) AS broken ON true
    """
)

_LEDGER_HEAD: Final = text(
    "SELECT h.last_sequence, h.last_hash,"
    " (SELECT r.chain_sequence FROM ledger.ledger_record AS r"
    "  WHERE r.organization_id = :organization_id"
    "  AND (r.plant_id = CAST(:plant_id AS uuid)"
    "       OR (r.plant_id IS NULL AND CAST(:plant_id AS uuid) IS NULL))"
    "  ORDER BY r.chain_sequence DESC LIMIT 1) AS max_sequence"
    " FROM (SELECT 1) AS one"
    " LEFT JOIN ledger.chain_head AS h ON h.organization_id = :organization_id"
    " AND h.kind = 'ledger' AND h.plant_id IS NOT DISTINCT FROM CAST(:plant_id AS uuid)"
)

_AUDIT_HEAD: Final = text(
    "SELECT h.last_sequence, h.last_hash,"
    " (SELECT a.chain_sequence FROM shared.audit_entry AS a"
    "  WHERE a.organization_id = :organization_id"
    "  ORDER BY a.chain_sequence DESC LIMIT 1) AS max_sequence"
    " FROM (SELECT 1) AS one"
    " LEFT JOIN ledger.chain_head AS h ON h.organization_id = :organization_id"
    " AND h.kind = 'audit' AND h.plant_id IS NULL"
)

_LEDGER_CHECKPOINTS: Final = text(
    "SELECT r.record_id, r.organization_id, r.plant_id, r.chain_sequence, r.record_type,"
    " r.schema_version, r.actor_kind, r.actor_id, r.actor_display_name_snapshot,"
    " r.actor_role_in_use, r.actor_concession_id, r.actor_unit, r.scope_plant_id,"
    " r.scope_zone_id, r.scope_node_id, r.correlation_id, r.received_at, r.content,"
    " r.content_hash, r.previous_hash, r.record_hash"
    " FROM ledger.ledger_record AS r"
    " WHERE r.organization_id = :organization_id"
    " AND (r.plant_id = CAST(:plant_id AS uuid)"
    "      OR (r.plant_id IS NULL AND CAST(:plant_id AS uuid) IS NULL))"
    " AND r.chain_sequence BETWEEN :first AND :last AND r.record_type = 'checkpoint'"
    " ORDER BY r.chain_sequence, r.record_id"
)
_AUDIT_CHECKPOINTS: Final = text(
    "SELECT a.entry_id, a.organization_id, a.chain_sequence, a.actor_kind, a.actor_id,"
    " a.actor_display_name_snapshot, a.actor_role_in_use, a.actor_concession_id, a.actor_unit,"
    " a.operation, a.scope_plant_id, a.scope_zone_id, a.resource_kind, a.resource_id,"
    " a.filters, a.filters_hash, a.result_count, a.outcome, a.correlation_id, a.occurred_at,"
    " a.previous_hash, a.entry_hash"
    " FROM shared.audit_entry AS a"
    " WHERE a.organization_id = :organization_id"
    " AND a.chain_sequence BETWEEN :first AND :last AND a.operation = 'checkpoint'"
    " ORDER BY a.chain_sequence, a.entry_id"
)

# ``:sequences`` nulo: todo el lote (verificación completa); si no, solo la muestra.
_LEDGER_DOCUMENTS: Final = text(
    "SELECT r.chain_sequence, CAST(r.record_id AS text) AS entry_id,"
    " CAST(r.content_json AS text) AS document, r.content_hash"
    " FROM ledger.ledger_record AS r"
    " WHERE r.organization_id = :organization_id"
    " AND (r.plant_id = CAST(:plant_id AS uuid)"
    "      OR (r.plant_id IS NULL AND CAST(:plant_id AS uuid) IS NULL))"
    " AND r.chain_sequence BETWEEN :first AND :last"
    " AND (CAST(:sequences AS bigint[]) IS NULL"
    "      OR r.chain_sequence = ANY(CAST(:sequences AS bigint[])))"
    " ORDER BY r.chain_sequence, r.record_id"
)
_AUDIT_DOCUMENTS: Final = text(
    "SELECT a.chain_sequence, CAST(a.entry_id AS text) AS entry_id,"
    " CAST(a.filters_json AS text) AS document, a.filters_hash AS content_hash"
    " FROM shared.audit_entry AS a"
    " WHERE a.organization_id = :organization_id"
    " AND a.chain_sequence BETWEEN :first AND :last"
    " AND (CAST(:sequences AS bigint[]) IS NULL"
    "      OR a.chain_sequence = ANY(CAST(:sequences AS bigint[])))"
    " ORDER BY a.chain_sequence, a.entry_id"
)

_CHAINS: Final = text(
    "SELECT kind, plant_id FROM ledger.chain_head WHERE organization_id = :organization_id"
    " ORDER BY kind, plant_id NULLS FIRST"
)

_LAST_VERIFIED: Final = text(
    "SELECT filters FROM shared.audit_entry"
    " WHERE organization_id = :organization_id AND operation = 'integrity_verification'"
    " AND filters_json ->> 'chain_kind' = :kind"
    " AND (filters_json ->> 'plant_id') IS NOT DISTINCT FROM CAST(:plant_id AS text)"
    " AND filters_json ->> 'result' = 'intact'"
    " ORDER BY chain_sequence DESC LIMIT 1"
)

_RESULTS: Final = text(
    "SELECT DISTINCT ON (filters_json ->> 'chain_kind', filters_json ->> 'plant_id')"
    " filters, resource_id, occurred_at FROM shared.audit_entry"
    " WHERE organization_id = :organization_id AND operation = 'integrity_verification'"
    " ORDER BY filters_json ->> 'chain_kind', filters_json ->> 'plant_id', chain_sequence DESC"
)


def _uuid(value: object) -> uuid.UUID:
    """El UUID del controlador (``asyncpg`` tiene su propio tipo) como ``uuid.UUID``."""
    return value if type(value) is uuid.UUID else uuid.UUID(str(value))


def _row(row: Row[Any]) -> dict[str, Any]:
    values = dict(row._mapping)
    for name in ("content", "filters"):
        if values.get(name) is not None:
            values[name] = bytes(values[name])
    return values


def _pick(chain: CheckpointChain, ledger: TextClause, audit: TextClause) -> TextClause:
    return audit if chain.kind is ChainKind.AUDIT else ledger


class SqlIntegrityStore:
    """``IntegrityStore`` sobre ``shared.db`` (solo en el worker), ``AuditWriter`` y la bandeja."""

    def __init__(self, *, database: LedgerDatabase, audit: AuditWriter, outbox: OutboxPort) -> None:
        if getattr(database, "process", None) is not ProcessKind.WORKER:
            raise ValueError(
                "la verificación de cadenas corre en el worker (statement_timeout de 30 s)"
            )
        self._database = database
        self._audit = audit
        self._outbox = outbox

    @staticmethod
    def _parameters(context: ScopeContext, chain: CheckpointChain, **extra: Any) -> dict[str, Any]:
        parameters: dict[str, Any] = {"organization_id": context.organization_id, **extra}
        if chain.kind is ChainKind.LEDGER:
            parameters["plant_id"] = chain.plant_id
        return parameters

    async def chains(self, context: ScopeContext) -> Sequence[CheckpointChain]:
        rows = await self._database.read(
            context, _CHAINS, {"organization_id": context.organization_id}
        )
        return tuple(
            CheckpointChain(
                ChainKind(row.kind), None if row.plant_id is None else _uuid(row.plant_id)
            )
            for row in rows
        )

    async def head(self, context: ScopeContext, chain: CheckpointChain) -> HeadSnapshot:
        statement = _pick(chain, _LEDGER_HEAD, _AUDIT_HEAD)
        rows = await self._database.read(context, statement, self._parameters(context, chain))
        row = rows[0]
        maximum = 0 if row.max_sequence is None else int(row.max_sequence)
        if row.last_sequence is None:
            genesis = genesis_hash(
                str(context.organization_id),
                None if chain.plant_id is None else str(chain.plant_id),
            )
            return HeadSnapshot(0, genesis, maximum)
        return HeadSnapshot(int(row.last_sequence), str(row.last_hash), maximum)

    async def scan(
        self, context: ScopeContext, chain: CheckpointChain, first: int, last: int, start_hash: str
    ) -> BatchScan:
        statement = _pick(chain, _LEDGER_SCAN, _AUDIT_SCAN)
        parameters = self._parameters(context, chain, first=first, last=last, start_hash=start_hash)
        row = (await self._database.read(context, statement, parameters))[0]
        if row.reason is not None:
            broken = Break(int(row.expected), str(row.entry_id), str(row.reason))
            return BatchScan(broken, None)
        rows = int(row.rows)
        if rows < last - first + 1:
            # Faltan secuencias al final del lote: la primera que falta.
            return BatchScan(Break(first + rows, None, "sequence_gap"), None)
        return BatchScan(None, str(row.last_hash))

    async def checkpoints(
        self, context: ScopeContext, chain: CheckpointChain, first: int, last: int
    ) -> Sequence[Mapping[str, Any]]:
        statement = _pick(chain, _LEDGER_CHECKPOINTS, _AUDIT_CHECKPOINTS)
        rows = await self._database.read(
            context, statement, self._parameters(context, chain, first=first, last=last)
        )
        return tuple(_row(row) for row in rows)

    async def documents(
        self,
        context: ScopeContext,
        chain: CheckpointChain,
        first: int,
        last: int,
        sequences: Sequence[int] | None,
    ) -> Sequence[CanonicalRow]:
        statement = _pick(chain, _LEDGER_DOCUMENTS, _AUDIT_DOCUMENTS)
        parameters = self._parameters(
            context,
            chain,
            first=first,
            last=last,
            sequences=None if sequences is None else list(sequences),
        )
        rows = await self._database.read(context, statement, parameters)
        return tuple(
            CanonicalRow(
                sequence=int(row.chain_sequence),
                entry_id=str(row.entry_id),
                document=row.document,
                content_hash=row.content_hash,
            )
            for row in rows
        )

    async def last_verified(
        self, context: ScopeContext, chain: CheckpointChain
    ) -> VerifiedPoint | None:
        rows = await self._database.read(
            context,
            _LAST_VERIFIED,
            {
                "organization_id": context.organization_id,
                "kind": chain.kind.value,
                "plant_id": None if chain.plant_id is None else str(chain.plant_id),
            },
        )
        if not rows:
            return None
        return IntegrityResult.from_filters(json.loads(bytes(rows[0].filters))).verified_point

    async def results(self, context: ScopeContext) -> Sequence[IntegrityResult]:
        rows = await self._database.read(
            context, _RESULTS, {"organization_id": context.organization_id}
        )
        return tuple(
            IntegrityResult.from_filters(
                json.loads(bytes(row.filters)),
                broken_entry_id=None if row.resource_id is None else _uuid(row.resource_id),
                verified_at=row.occurred_at,
            )
            for row in rows
        )

    async def record(
        self,
        context: ScopeContext,
        result: IntegrityResult,
        event: Mapping[str, Any] | None,
    ) -> datetime:
        resource = None
        if result.broken_entry_id is not None:
            kind = "audit_entry" if result.chain.kind is ChainKind.AUDIT else "ledger_record"
            resource = ResourceRef(kind, result.broken_entry_id)
        async with self._database.transaction(context) as transaction:
            receipt = await self._audit.append(
                context,
                AuditOperation.INTEGRITY_VERIFICATION,
                plant_id=result.chain.plant_id,
                resource=resource,
                filters=result.to_filters(),
                transaction=transaction,
            )
            if event is not None:
                await self._outbox.publish(
                    transaction,
                    NewEvent(
                        event_name=INTEGRITY_COMPROMISED,
                        payload=event,
                        plant_id=result.chain.plant_id,
                    ),
                )
        return receipt.occurred_at
