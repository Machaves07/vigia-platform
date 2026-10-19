"""Reconstrucción de la proyección del lazo por planta (TASK-306; LC-LAZ-03; PAT-LAZ-RES-03;
domain-entities.md de U-04 §2.14 y §9; BR-LAZ-146 a 148 y 156; DR-LAZ-01 y 02).

``ProjectionRebuilder.rebuild_plant(context, plant_id)`` reconstruye la partición
``(organization_id, plant_id)`` desde el expediente, a demanda (sin cadencia: U04-H-14; la expone
``vigia-admin rebuild-loop-projection`` en VIG-241, fuera de esta tarea). Tres fases, cada una su
propia transacción:

1. **Reclamo** (corta): ``pg_try_advisory_xact_lock(hash("loop_rebuild", organization_id,
   plant_id))`` (PAT-NUC-ESC-05) es el **único** mecanismo de exclusión entre dos
   reconstrucciones de la misma planta; no se consigue si otra ya tiene el candado, y entonces se
   devuelve ``AlreadyRebuilding`` sin tocar nada (PR-LAZ-51). Si se gana, ``state = rebuilding`` se
   confirma aquí: queda visible para cualquier lector **durante toda la reconstrucción**, que es
   como la bandeja la declara (BR-LAZ-156, DR-LAZ-01) sin necesitar el candado todo ese tiempo. Una
   reconstrucción previa que murió a medias (``state`` colgado en ``rebuilding``, sin nadie
   sosteniendo ya el candado) no bloquea la siguiente: el candado se obtiene igual y ``state`` se
   vuelve a confirmar.
2. **Cómputo** (puede ser larga): ``LectorExpediente`` en lotes de 10 000 ``[objetivo propio]``
   por ``chain_sequence``, plegados con ``fold.step`` (puro) y enriquecidos después, por lote de
   hasta 50 zonas (NFR-LAZ-03 y 05), con el mismo procedimiento que un hallazgo nuevo
   (``ProjectionEnrichment.finding_facts``, ``new=True``): el pliegue no depende del reloj, y el
   enriquecimiento consulta los mismos puertos que la vía incremental, así que con el mismo
   estado externo (catálogo, compuertas, cobertura) produce el mismo valor (PR-LAZ-44). Una
   detección (``subject_kind = detection``) nunca se enriquece, igual que la vía incremental.
   El resultado se escribe en la tabla sombra (``*_rebuild_shadow``), sin tocar las tablas en
   servicio: ningún lector ve nada de esto.
3. **Intercambio** (corta): una sola sentencia por tabla, ``INSERT … SELECT … FROM *_shadow WHERE
   plant_id = :plant_id ON CONFLICT (…) DO UPDATE SET <columnas mutables>``, atómica y sin
   ``DELETE`` (P4: ``vigia_app`` nunca borra ``episode`` ni ``finding_state``, nota de ``laz_0029``
   sobre tabla sombra e intercambio). Un lector entre las fases 2 y 3 sigue viendo la fila vieja
   completa; tras el ``COMMIT`` de la fase 3, la nueva completa (PR-LAZ-59: nunca una mezcla).
   Cierra con ``state = healthy``, ``rebuilt_at`` y el ``projection_digest`` recalculado, y vacía
   la sombra de esa planta.

Si la fase 1 o 2 fallan (``TemporarilyUnavailable`` u otro error), ninguna transacción ha tocado
las tablas en servicio: la partición sigue como estaba (a lo sumo, ``rebuilding`` colgado, que una
reconstrucción posterior puede repetir con el mismo candado).
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text

from vigia_platform.catalog.domain.ports import CatalogQueryPort, GateQueryPort
from vigia_platform.ledger.application.coverage import CoveragePort
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.loop.domain.enums import SubjectKind
from vigia_platform.loop.projection.digest import (
    EMPTY_PARTITION_DIGEST,
    partition_digest,
)
from vigia_platform.loop.projection.enrichment import (
    MarkerPort,
    ProjectionEnrichment,
    SettingsSource,
)
from vigia_platform.loop.projection.fold import (
    PROJECTED_RECORD_TYPES,
    EpisodeRow,
    FindingStateRow,
    PlantProjection,
    ProjectionInput,
    episode_term,
    finding_term,
)
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.db import Transaction

__all__ = [
    "BATCH_SIZE",
    "AlreadyRebuilding",
    "ProjectionRebuilder",
    "RebuildResult",
    "enrich_finding",
    "evidence_ids_for",
    "rebuild_lock_key",
]

BATCH_SIZE: Final = 10_000
"""Registros por lote de lectura del expediente ``[objetivo propio]`` (PAT-LAZ-RES-03)."""

_EPISODE_COLUMNS: Final = tuple(field.name for field in dataclasses.fields(EpisodeRow))
_EPISODE_MUTABLE: Final = (
    "tier",
    "segment_record_ids",
    "segment_count",
    "expected_segment_count",
    "missing_segment_indexes",
    "started_at",
    "ended_at",
    "total_episode_duration_ms",
    "total_condition_duration_ms",
    "max_confidence",
    "clip_references",
    "continues_open",
    "last_segment_received_at",
    "last_event_id",
    "projection_updated_at",
)
"""Las columnas de ``loop.episode`` con ``UPDATE`` de ``vigia_app`` (``laz_0029``): las mismas de
``store._EPISODE_MUTABLE``. La organización, la planta, la zona, la familia y el estándar no
cambian nunca, ni para la reconstrucción."""
_FINDING_STATE_COLUMNS: Final = tuple(field.name for field in dataclasses.fields(FindingStateRow))
_FINDING_STATE_FIXED: Final = frozenset(
    {
        "finding_state_id",
        "organization_id",
        "plant_id",
        "zone_id",
        "subject_kind",
        "subject_id",
        "anchor_record_id",
        "family",
        "standard_id",
        "standard_version",
        "occurred_at_exact",
        "occurred_at_bucket",
    }
)
_FINDING_STATE_MUTABLE: Final = tuple(
    name for name in _FINDING_STATE_COLUMNS if name not in _FINDING_STATE_FIXED
)

_LOCK: Final = text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0)) AS acquired")
_ENSURE_CHECKPOINT: Final = text(
    "INSERT INTO loop.loop_projection_checkpoint (organization_id, plant_id)"
    " VALUES (:organization_id, :plant_id) ON CONFLICT (organization_id, plant_id) DO NOTHING"
)
_CLAIM: Final = text(
    "UPDATE loop.loop_projection_checkpoint SET state = 'rebuilding'"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
)
_FINISH: Final = text(
    "UPDATE loop.loop_projection_checkpoint SET state = 'healthy', rebuilt_at = :rebuilt_at,"
    " last_applied_ledger_sequence = :sequence, last_applied_event_id = :event_id,"
    " projection_digest = :projection_digest"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
)
_LEDGER_BATCH: Final = text(
    "SELECT record_id, organization_id, plant_id, chain_sequence, record_type, received_at,"
    " content FROM ledger.ledger_record"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " AND record_type = ANY(CAST(:record_types AS text[]))"
    " AND chain_sequence BETWEEN :first AND :last"
    " ORDER BY chain_sequence"
)
_MAX_CHAIN_SEQUENCE: Final = text(
    "SELECT max(chain_sequence) AS last_sequence FROM ledger.ledger_record"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
)
_EVIDENCE_IDS: Final = text(
    "SELECT evidence_id FROM ledger.evidence"
    " WHERE organization_id = :organization_id AND record_id = ANY(CAST(:record_ids AS uuid[]))"
)
_CLEAR_EPISODE_SHADOW: Final = text(
    "DELETE FROM loop.episode_rebuild_shadow"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
)
_CLEAR_FINDING_STATE_SHADOW: Final = text(
    "DELETE FROM loop.finding_state_rebuild_shadow"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
)
# Las listas de columnas vienen de `dataclasses.fields(...)`, nunca de la petición: no hay dato
# externo en estas plantillas, solo los nombres fijos de las columnas de la fila (NFR-NUC-19).
# Se arman aparte, en una variable, y `text()` solo envuelve el resultado ya hecho (VIG001).
_episode_columns_csv = ", ".join(_EPISODE_COLUMNS)
_episode_placeholders_csv = ", ".join(
    f"CAST(:{name} AS jsonb)" if name == "clip_references" else f":{name}"
    for name in _EPISODE_COLUMNS
)
"""``clip_references`` es ``jsonb``: ``text()`` no conoce el tipo de la columna, así que el
parámetro llega como el JSON ya canónico (``_episode_values``) y se convierte aquí."""
_finding_state_columns_csv = ", ".join(_FINDING_STATE_COLUMNS)
_finding_state_placeholders_csv = ", ".join(f":{name}" for name in _FINDING_STATE_COLUMNS)
_episode_conflict_update = ", ".join(f"{name} = EXCLUDED.{name}" for name in _EPISODE_MUTABLE)
_finding_state_conflict_update = ", ".join(
    f"{name} = EXCLUDED.{name}" for name in _FINDING_STATE_MUTABLE
)

_INSERT_EPISODE_SHADOW_SQL = (
    f"INSERT INTO loop.episode_rebuild_shadow ({_episode_columns_csv})"  # noqa: S608
    f" VALUES ({_episode_placeholders_csv})"
)
_INSERT_EPISODE_SHADOW: Final = text(_INSERT_EPISODE_SHADOW_SQL)
_INSERT_FINDING_STATE_SHADOW_SQL = (
    f"INSERT INTO loop.finding_state_rebuild_shadow ({_finding_state_columns_csv})"  # noqa: S608
    f" VALUES ({_finding_state_placeholders_csv})"
)
_INSERT_FINDING_STATE_SHADOW: Final = text(_INSERT_FINDING_STATE_SHADOW_SQL)
_SWAP_EPISODE_SQL = (
    f"INSERT INTO loop.episode ({_episode_columns_csv})"  # noqa: S608
    f" SELECT {_episode_columns_csv} FROM loop.episode_rebuild_shadow"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    f" ON CONFLICT (episode_id) DO UPDATE SET {_episode_conflict_update}"
)
_SWAP_EPISODE: Final = text(_SWAP_EPISODE_SQL)
_SWAP_FINDING_STATE_SQL = (
    f"INSERT INTO loop.finding_state ({_finding_state_columns_csv})"  # noqa: S608
    f" SELECT {_finding_state_columns_csv} FROM loop.finding_state_rebuild_shadow"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " ON CONFLICT (finding_state_id, occurred_at_bucket) DO UPDATE SET "
    f"{_finding_state_conflict_update}"
)
_SWAP_FINDING_STATE: Final = text(_SWAP_FINDING_STATE_SQL)


def rebuild_lock_key(organization_id: uuid.UUID, plant_id: uuid.UUID) -> str:
    """La clave del candado consultivo de ``(organization_id, plant_id)`` (PAT-NUC-ESC-05)."""
    return f"loop_rebuild|{organization_id}|{plant_id}"


class AlreadyRebuilding(RuntimeError):
    """Otra reconstrucción de la misma planta ya está en curso (PR-LAZ-51): no se hizo nada."""

    def __init__(self, plant_id: uuid.UUID) -> None:
        super().__init__(f"la planta {plant_id} ya se está reconstruyendo")
        self.plant_id = plant_id


@dataclass(frozen=True, slots=True)
class RebuildResult:
    """Lo que hizo una reconstrucción completa de una planta."""

    plant_id: uuid.UUID
    episodes_written: int
    findings_written: int
    last_sequence: int
    projection_digest: str


def _uuid(value: Any) -> uuid.UUID:
    return uuid.UUID(str(value))


def _episode_values(row: EpisodeRow) -> dict[str, Any]:
    values = dataclasses.asdict(row)
    values["segment_record_ids"] = list(row.segment_record_ids)
    values["missing_segment_indexes"] = list(row.missing_segment_indexes)
    values["clip_references"] = json.dumps([dict(clip) for clip in row.clip_references])
    return values


def _finding_values(row: FindingStateRow) -> dict[str, Any]:
    return dataclasses.asdict(row)


async def evidence_ids_for(
    transaction: Transaction, context: ScopeContext, record_ids: Sequence[uuid.UUID]
) -> list[uuid.UUID]:
    """Las evidencias de ``record_ids`` (los tramos de un episodio), para
    ``ProjectionEnrichment.finding_facts``."""
    if not record_ids:
        return []
    rows = await transaction.execute(
        _EVIDENCE_IDS, {"organization_id": context.organization_id, "record_ids": list(record_ids)}
    )
    return [_uuid(row.evidence_id) for row in rows.all()]


async def enrich_finding(
    enrichment: ProjectionEnrichment,
    transaction: Transaction,
    *,
    context: ScopeContext,
    plant_id: uuid.UUID,
    finding: FindingStateRow,
    segment_record_ids: Sequence[uuid.UUID],
    now: datetime,
) -> FindingStateRow:
    """La fila enriquecida como si naciera ahora (§9 con su nota U04-H-05): el pliegue puro no
    enriquece nunca una detección (``subject_kind = detection``), igual que la vía incremental."""
    if finding.subject_kind != SubjectKind.FINDING:
        return finding
    evidence_ids = await evidence_ids_for(transaction, context, segment_record_ids)
    facts = await enrichment.finding_facts(
        transaction,
        plant_id=plant_id,
        zone_id=finding.zone_id,
        occurred_at=finding.occurred_at_exact,
        evidence_ids=evidence_ids,
        new=True,
        now=now,
    )
    enriched = facts.enrich(None, finding)
    return dataclasses.replace(enriched, projection_digest=enriched.computed_digest())


def _projection_input(row: Any) -> ProjectionInput:
    document: dict[str, Any] = json.loads(bytes(row.content))
    return ProjectionInput(
        record_id=_uuid(row.record_id),
        organization_id=_uuid(row.organization_id),
        plant_id=_uuid(row.plant_id),
        chain_sequence=int(row.chain_sequence),
        record_type=str(row.record_type),
        received_at=row.received_at,
        document=document,
    )


@repository
class ProjectionRebuilder:
    """``rebuild_plant(context, plant_id) -> RebuildResult`` (LC-LAZ-03, ``reconstruir``)."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        catalog: CatalogQueryPort,
        gates: GateQueryPort,
        coverage: CoveragePort,
        markers: MarkerPort,
        settings: SettingsSource,
        clock: Clock,
        batch_size: int = BATCH_SIZE,
    ) -> None:
        self._database = database
        self._enrichment = ProjectionEnrichment(
            catalog=catalog, gates=gates, coverage=coverage, markers=markers, settings=settings
        )
        self._clock = clock
        self._batch_size = batch_size

    @property
    def enrichment(self) -> ProjectionEnrichment:
        """El mismo enriquecimiento que usa la reconstrucción, para ``verify_loop_projection``
        (TASK-306): con el mismo estado externo produce el mismo valor (PR-LAZ-44)."""
        return self._enrichment

    async def rebuild_plant(self, context: ScopeContext, plant_id: uuid.UUID) -> RebuildResult:
        """Reconstruye ``(context.organization_id, plant_id)``; ``AlreadyRebuilding`` si otra
        reconstrucción de la misma planta ya está en curso.

        Si la fase 2 falla, la partición queda declarada ``rebuilding`` (DR-LAZ-01): una
        reconstrucción posterior la repite con el mismo candado; no se inventa un estado
        ``healthy`` (P5)."""
        await self._claim(context, plant_id)
        projection = await self._compute(context, plant_id)
        return await self._swap(context, plant_id, projection)

    # --- fase 1: reclamo --------------------------------------------------------------------

    async def _claim(self, context: ScopeContext, plant_id: uuid.UUID) -> None:
        key = rebuild_lock_key(context.organization_id, plant_id)
        partition = {"organization_id": context.organization_id, "plant_id": plant_id}
        async with self._database.transaction(context) as transaction:
            acquired = (await transaction.execute(_LOCK, {"key": key})).one().acquired
            if not acquired:
                raise AlreadyRebuilding(plant_id)
            await transaction.execute(_ENSURE_CHECKPOINT, partition)
            await transaction.execute(_CLAIM, partition)

    # --- fase 2: cómputo ----------------------------------------------------------------------

    async def _compute(self, context: ScopeContext, plant_id: uuid.UUID) -> PlantProjection:
        projection = PlantProjection(context.organization_id, plant_id)
        async with self._database.transaction(context) as transaction:
            await transaction.execute(
                _CLEAR_EPISODE_SHADOW,
                {"organization_id": context.organization_id, "plant_id": plant_id},
            )
            await transaction.execute(
                _CLEAR_FINDING_STATE_SHADOW,
                {"organization_id": context.organization_id, "plant_id": plant_id},
            )
            last = (
                await transaction.execute(
                    _MAX_CHAIN_SEQUENCE,
                    {"organization_id": context.organization_id, "plant_id": plant_id},
                )
            ).one()
            top = last.last_sequence
            if top is not None:
                await self._fold_batches(transaction, context, plant_id, projection, int(top))
            await self._enrich_and_stage(transaction, context, plant_id, projection)
        return projection

    async def _fold_batches(
        self,
        transaction: Transaction,
        context: ScopeContext,
        plant_id: uuid.UUID,
        projection: PlantProjection,
        top: int,
    ) -> None:
        first = 1
        while first <= top:
            last = min(first + self._batch_size - 1, top)
            rows = (
                await transaction.execute(
                    _LEDGER_BATCH,
                    {
                        "organization_id": context.organization_id,
                        "plant_id": plant_id,
                        "record_types": list(PROJECTED_RECORD_TYPES),
                        "first": first,
                        "last": last,
                    },
                )
            ).all()
            for row in rows:
                projection.apply(_projection_input(row))
            first = last + 1

    async def _enrich_and_stage(
        self,
        transaction: Transaction,
        context: ScopeContext,
        plant_id: uuid.UUID,
        projection: PlantProjection,
    ) -> None:
        now = self._clock.now()
        for episode in projection.episodes.values():
            await transaction.execute(_INSERT_EPISODE_SHADOW, _episode_values(episode))
        for finding in projection.findings.values():
            segments = (
                projection.episodes[finding.subject_id].segment_record_ids
                if finding.subject_kind == SubjectKind.FINDING
                else ()
            )
            enriched = await enrich_finding(
                self._enrichment,
                transaction,
                context=context,
                plant_id=plant_id,
                finding=finding,
                segment_record_ids=segments,
                now=now,
            )
            await transaction.execute(_INSERT_FINDING_STATE_SHADOW, _finding_values(enriched))
            projection.findings[finding.subject_id] = enriched

    # --- fase 3: intercambio ------------------------------------------------------------------

    async def _swap(
        self, context: ScopeContext, plant_id: uuid.UUID, projection: PlantProjection
    ) -> RebuildResult:
        partition = {"organization_id": context.organization_id, "plant_id": plant_id}
        digest = _partition_digest(projection)
        async with self._database.transaction(context) as transaction:
            await transaction.execute(_SWAP_EPISODE, partition)
            await transaction.execute(_SWAP_FINDING_STATE, partition)
            await transaction.execute(
                _FINISH,
                {
                    **partition,
                    "rebuilt_at": self._clock.now(),
                    "sequence": projection.last_applied_ledger_sequence,
                    "event_id": projection.last_applied_event_id,
                    "projection_digest": digest,
                },
            )
            await transaction.execute(_CLEAR_EPISODE_SHADOW, partition)
            await transaction.execute(_CLEAR_FINDING_STATE_SHADOW, partition)
        return RebuildResult(
            plant_id=plant_id,
            episodes_written=len(projection.episodes),
            findings_written=len(projection.findings),
            last_sequence=projection.last_applied_ledger_sequence,
            projection_digest=digest,
        )


def _partition_digest(projection: PlantProjection) -> str:
    terms = [episode_term(episode) for episode in projection.episodes.values()]
    terms += [finding_term(finding) for finding in projection.findings.values()]
    return partition_digest(terms) if terms else EMPTY_PARTITION_DIGEST
