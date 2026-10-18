"""Resumen de control de la proyección del lazo (domain-entities.md de U-04, §2.2, §2.14 y §9).

Módulo **puro** (PAT-LAZ-SEG-09): sin FastAPI, sin SQLAlchemy y sin reloj.

- ``row_digest(fields)``: SHA-256 en hexadecimal de la forma canónica RFC 8785 de los **campos de
  estado** de una fila (``FINDING_STATE_DIGEST_FIELDS`` o ``EPISODE_DIGEST_FIELDS``). Es el
  ``projection_digest`` de ``finding_state`` que ``verify_loop_projection`` (TASK-306) recalcula
  desde el expediente. Quedan fuera los campos de contabilidad de la propia proyección
  (``last_event_id``, ``last_record_id``, ``projection_updated_at`` y el propio resumen), que
  dependen de por dónde llegó el hecho (evento o reconstrucción) y no de qué hecho fue.
- ``partition_digest``: el resumen de una partición ``(organization_id, plant_id)``, que guarda
  ``loop_projection_checkpoint.projection_digest``. Es una **suma de conjunto** (suma módulo
  2²⁵⁶ de un término por fila, AdHash): no depende del orden en que se aplicaron las filas y se
  actualiza en O(1) al cambiar una (``replace_term``), sin releer la partición.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Final

from vigia_contracts.canonical import canonicalize

__all__ = [
    "EMPTY_PARTITION_DIGEST",
    "EPISODE_DIGEST_FIELDS",
    "FINDING_STATE_DIGEST_FIELDS",
    "partition_digest",
    "partition_term",
    "replace_term",
    "row_digest",
]

FINDING_STATE_DIGEST_FIELDS: Final = (
    "finding_state_id",
    "organization_id",
    "plant_id",
    "zone_id",
    "subject_kind",
    "subject_id",
    "anchor_record_id",
    "family",
    "tier",
    "standard_id",
    "standard_version",
    "lifecycle_state",
    "previous_lifecycle_state",
    "current_classification_outcome",
    "current_reason_category",
    "classification_count",
    "first_classified_at",
    "last_classified_at",
    "classified_within_deadline",
    "current_action_id",
    "current_action_type",
    "assignee_user_id",
    "assignee_inactive",
    "due_at",
    "overdue_since",
    "overdue_days",
    "closed_at",
    "closure_record_id",
    "unclosable_since",
    "unclosable_review_due_at",
    "unclosable_pending_review",
    "closed_with_later_different_classification",
    "reopening_count",
    "has_signed_root_cause",
    "observability_state_at_event",
    "zone_gate_status_at_event",
    "zone_agreement_revoked",
    "zone_agreement_revoked_at",
    "single_occupancy_zone",
    "aggregation_window_minutes",
    "walk_test_regression_pending",
    "walk_test_regression_cause",
    "occurred_at_exact",
    "occurred_at_bucket",
    "age_days",
    "evidence_flagged",
    "missing_segments_declared",
)
"""Los campos de estado de ``FindingState`` (§2.2): todos menos la contabilidad de la proyección.
Los de enriquecimiento entran: TASK-341 los rellena y recalcula el resumen (§9)."""

EPISODE_DIGEST_FIELDS: Final = (
    "episode_id",
    "organization_id",
    "plant_id",
    "zone_id",
    "family",
    "tier",
    "standard_id",
    "standard_version",
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
    "observability_state_at_start",
    "continues_open",
    "last_segment_received_at",
)
"""Los campos de estado de ``Episode`` (§2.1)."""

EMPTY_PARTITION_DIGEST: Final = "0" * 64
"""Resumen de una partición sin filas."""

_MODULUS: Final = 2**256


def _json(value: Any) -> Any:
    """La forma JSON de un valor de fila: marcas en UTC con milisegundos y ``Z``."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise ValueError("las marcas de la proyección llevan zona horaria")
        moment = value.astimezone(UTC).replace(tzinfo=None)
        return moment.isoformat(timespec="milliseconds") + "Z"
    if isinstance(value, Mapping):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json(item) for item in value]
    return value


def row_digest(fields: Mapping[str, Any], names: Iterable[str]) -> str:
    """SHA-256 de la forma canónica de los campos ``names`` de ``fields``."""
    document = {name: _json(fields[name]) for name in names}
    return hashlib.sha256(canonicalize(document)).hexdigest()


def partition_term(table: str, key: uuid.UUID, digest: str) -> int:
    """El término de una fila en la suma de su partición."""
    material = f"{table}\x00{key}\x00{digest}".encode()
    return int.from_bytes(hashlib.sha256(material).digest(), "big")


def _hex(value: int) -> str:
    return f"{value % _MODULUS:064x}"


def partition_digest(terms: Iterable[int]) -> str:
    """El resumen de una partición: la suma módulo 2²⁵⁶ de los términos de sus filas."""
    return _hex(sum(terms))


def replace_term(current: str | None, removed: int | None, added: int | None) -> str:
    """El resumen tras sustituir el término ``removed`` por ``added`` (cualquiera puede faltar)."""
    value = int(current or EMPTY_PARTITION_DIGEST, 16)
    if removed is not None:
        value -= removed
    if added is not None:
        value += added
    return _hex(value)
