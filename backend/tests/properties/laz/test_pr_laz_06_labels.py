"""PR-LAZ-06: exactamente una ``Label`` por clasificación, con los campos de la regla.

business-logic-model.md de U-04 §11, C-PLA-23 (TASK-308; BR-LAZ-26; DE §5.3; apoyada en
PR-NUC-24). Con ``classification_sequences`` (solo pasos válidos) sobre uno a tres hallazgos por
ejemplo, contra PostgreSQL 16 real como ``vigia_app`` y el ``ClassificationService`` real:

- **exactamente una**: por cada registro ``classification`` escrito hay una y solo una ``Label``
  (``source_record_id``), y ninguna etiqueta sin su registro fuente;
- **los campos de la regla** (DE §5.3): ``subject_record_id`` = ``/anchor_record_id``,
  ``family``, ``outcome`` y ``reason_category`` del registro, ``labeled_by`` = ``/signer``,
  ``evidence_ids`` = las evidencias del registro sujeto (por referencia), la planta y la zona del
  registro y ``labeled_at`` = su ``received_at``; ninguna lleva texto libre;
- **la consulta es el filtro**: ``LabelService.consultar`` por periodo, zona, familia y categoría
  (combinadas con Y, paginada por clave con un tamaño dibujado) devuelve exactamente el conjunto
  que un filtro por fuerza bruta sobre todas las etiquetas de la organización.

Las etiquetas de la organización se acumulan entre ejemplos: el oráculo y la consulta miran
siempre todas. Solo datos generados.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any, Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.integration.conftest import PostgresEndpoint
from tests.loop_classification_support import ClassificationStack, classification_stack
from tests.properties.laz.strategies.classification import (
    ClassifyStep,
    Step,
    classification_sequences,
    reason_text,
)
from vigia_platform.ledger.application.labels import (
    LabelPageRequest,
    LabelPeriod,
    LabelService,
)
from vigia_platform.loop.application.classification import (
    REASONS_BY_OUTCOME,
    ClassificationRejected,
)
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.shared.context import Role

pytestmark = pytest.mark.integration

EXAMPLES: Final = 15
"""Cada ejemplo ingresa hasta tres hallazgos y escribe varias transacciones reales."""
_CATEGORIES: Final = sorted(set().union(*REASONS_BY_OUTCOME.values()))
_PICKS: Final = 10_000
"""Índices dibujados que se reducen sobre las marcas reales de las etiquetas."""


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[ClassificationStack]:
    with classification_stack(postgres_endpoint, "pr_laz_06") as built:
        yield built


def _records(stack: ClassificationStack) -> list[dict[str, Any]]:
    organization = stack.world.a
    return [
        dict(row)
        for row in stack.fetch(
            "SELECT record_id, plant_id, scope_zone_id, received_at,"
            " ledger.vigia_bytes_to_jsonb(content) AS content FROM ledger.ledger_record"
            " WHERE organization_id = $1 AND record_type = 'classification'",
            organization.organization_id,
        )
    ]


def _evidence(stack: ClassificationStack, record_id: uuid.UUID) -> set[uuid.UUID]:
    return {
        uuid.UUID(str(row["evidence_id"]))
        for row in stack.fetch(
            "SELECT evidence_id FROM ledger.evidence WHERE organization_id = $1 AND record_id = $2",
            stack.world.a.organization_id,
            record_id,
        )
    }


def _content(record: dict[str, Any]) -> dict[str, Any]:
    content = record["content"]
    if isinstance(content, str):  # la conexión de superusuario no decodifica ``jsonb``
        content = json.loads(content)
    assert isinstance(content, dict)
    return content


def _check_one_label_per_record(stack: ClassificationStack) -> list[dict[str, Any]]:
    records = {r["record_id"]: r for r in _records(stack)}
    labels = stack.labels(stack.world.a)
    by_source: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for label in labels:
        by_source.setdefault(label["source_record_id"], []).append(label)
    assert set(by_source) == set(records), "etiqueta sin registro o registro sin etiqueta"
    for record_id, record in records.items():
        (label,) = by_source[record_id]
        content = _content(record)
        assert label["subject_record_id"] == uuid.UUID(content["anchor_record_id"])
        assert (label["family"], label["outcome"], label["reason_category"]) == (
            content["family"],
            content["outcome"],
            content["reason_category"],
        )
        assert label["labeled_by"] == content["signer"]
        assert (label["plant_id"], label["zone_id"]) == (
            record["plant_id"],
            record["scope_zone_id"],
        )
        assert str(label["zone_id"]) == content["zone_id"]
        assert set(label["evidence_ids"]) == _evidence(stack, label["subject_record_id"])
        assert label["evidence_ids"], "la etiqueta referencia los clips del episodio"
        assert label["labeled_at"] == record["received_at"]
    return labels


def _classify_all(stack: ClassificationStack, context: Any, sequences: list[list[Step]]) -> None:
    for steps in sequences:
        subject = stack.received()
        fsid = stack.finding_state_id(subject)
        for step in steps:
            assert isinstance(step, ClassifyStep)
            try:
                stack.run(
                    stack.service.classify(
                        context,
                        fsid,
                        outcome=step.outcome.value,
                        reason_category=step.reason_category,
                        reason_text=reason_text(step),
                        reinforced_reason=step.reinforced,
                    )
                )
            except ClassificationRejected as rejected:
                assert rejected.detail_code is LoopDetailCode.TRANSITION_NOT_ALLOWED


def _query_all(
    stack: ClassificationStack,
    service: LabelService,
    context: Any,
    period: LabelPeriod,
    filters: dict[str, Any],
    size: int,
) -> list[uuid.UUID]:
    found: list[uuid.UUID] = []
    page = LabelPageRequest(size=size)
    while True:
        result = stack.run(service.consultar(context, period, page=page, **filters))
        found.extend(item.label_id for item in result.items)
        if result.next_cursor is None:
            return found
        page = LabelPageRequest(size=size, after=result.next_cursor)


def _matches(label: dict[str, Any], period: LabelPeriod, filters: dict[str, Any]) -> bool:
    if not period.start <= label["labeled_at"] < period.end:
        return False
    if filters.get("zone_id") is not None and label["zone_id"] != filters["zone_id"]:
        return False
    if filters.get("family") is not None and label["family"] != filters["family"]:
        return False
    category = filters.get("reason_category")
    return category is None or label["reason_category"] == category


def test_pr_laz_06_exactly_one_label_per_classification_and_query_is_the_filter(
    stack: ClassificationStack,
) -> None:
    world = stack.world
    coordinator = stack.member(world.a, Role.COORDINATOR_SST)
    context = stack.context(coordinator)
    labels_service = LabelService(database=stack.database, audit=world.stack.authz.sessions.audit)
    zones = (world.a.zone_id, world.a.site.other_zone_id, uuid.uuid4())

    @settings(
        max_examples=EXAMPLES,
        deadline=None,
        database=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(
        sequences=st.lists(
            classification_sequences(min_size=1, max_size=4, valid_only=True),
            min_size=1,
            max_size=3,
        ),
        start_pick=st.integers(0, _PICKS),
        end_pick=st.none() | st.integers(0, _PICKS),
        end_on_a_label=st.booleans(),
        zone=st.sampled_from([None, *zones]),
        family=st.sampled_from([None, "coexistence", "dwell"]),
        category=st.sampled_from([None, *_CATEGORIES]),
        size=st.integers(1, 200),
    )
    def run(
        sequences: list[list[Step]],
        start_pick: int,
        end_pick: int | None,
        end_on_a_label: bool,
        zone: uuid.UUID | None,
        family: str | None,
        category: str | None,
        size: int,
    ) -> None:
        _classify_all(stack, context, sequences)
        labels = _check_one_label_per_record(stack)
        # El periodo se elige sobre las marcas reales con índices ya dibujados: la generación no
        # depende del estado de la base (retro 14).
        stamps = sorted(label["labeled_at"] for label in labels)
        start = stamps[start_pick % len(stamps)]
        last = stamps[-1] if end_pick is None else stamps[end_pick % len(stamps)]
        # Con ``end_on_a_label`` el extremo final cae justo en una etiqueta, que queda fuera.
        end: datetime = last if end_on_a_label else last + timedelta(milliseconds=1)
        if end <= start:
            end = start + timedelta(milliseconds=1)
        period = LabelPeriod(start, end)
        filters = {"zone_id": zone, "family": family, "reason_category": category}
        found = _query_all(stack, labels_service, context, period, filters, size)
        assert len(found) == len(set(found)), "una etiqueta repetida entre páginas"
        expected = {label["label_id"] for label in labels if _matches(label, period, filters)}
        assert set(found) == expected

    run()
