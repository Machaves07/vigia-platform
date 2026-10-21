"""Nombres de métrica de U-04 frente a NFR-LAZ-41 y NFR-LAZ-42 (TASK-349; LC-LAZ-38).

Las listas esperadas están escritas **a mano** desde el requisito (no se derivan del módulo):

- ``EXPECTED_NFR_LAZ_41``: cada partida de NFR-LAZ-41 con las métricas que la cubren;
- ``EXPECTED_ALARMS``: las ocho alarmas de NFR-LAZ-42 con la métrica que dispara cada una;
- ``EXPECTED_TRANSVERSAL``: las de los mamparos (LC-LAZ-40) y de la fábrica de clientes
  (LC-LAZ-39), fuera de las dos listas;
- ``EXPECTED_EXPORT_FAILED``: el contador de ``export_worker`` que dispara ``export-failed``
  (TASK-332 lo trae al módulo desde TASK-317).

El catálogo es **exactamente** su unión (igualdad de conjuntos): una métrica de más o de menos, o
una alarma sin su métrica, cae en rojo. Además: ninguna métrica de U-04 lleva dimensión de usuario
ni de nodo (A-66) y solo organización y planta como identificadores; ningún nombre choca con los
de U-02 y U-03; cada atributo declarado es una lista cerrada de la política de redacción.

Solo datos generados.
"""

from __future__ import annotations

from typing import Final

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from vigia_platform.loop.observability.metrics import (
    EGRESS_BLOCKED_ALARM,
    EXPORT_FAILED_ALARM,
    LOOP_ALARM_CONDITIONS,
    LOOP_CATALOG,
    NFR_LAZ_41,
    TRANSVERSAL_METRICS,
    LoopCounter,
    LoopGauge,
    LoopHistogram,
    LoopMetrics,
)
from vigia_platform.shared.observability import redaction
from vigia_platform.shared.observability.metrics import CATALOG, METER_NAME, MetricKind

EXPECTED_NFR_LAZ_41: Final[dict[str, set[str]]] = {
    # «pendientes y antigüedad del evento más viejo por consumidor del lazo»
    "consumer_pending_and_oldest_age": {
        "loop_consumer_pending",
        "loop_consumer_oldest_pending_age_seconds",
    },
    # «entradas en cola muerta»
    "dead_letter_entries": {"loop_dead_letter_entries_total"},
    # «duración y resultado de cada una de las diez tareas periódicas»
    "periodic_task_duration_and_result": {"loop_periodic_task_duration_ms"},
    # «solicitudes al asistente por estado (ready, unavailable, rejected_by_validators) y su
    # latencia»
    "assistant_requests_by_state_and_latency": {
        "assistant_requests_total",
        "assistant_request_duration_ms",
    },
    # «fichas de entrada y de salida consumidas por organización y mes»
    "assistant_tokens_by_organization_and_month": {"assistant_tokens_total"},
    # «correos por estado (enviado, rebotado, quejado, agrupado, degraded_app_only)»
    "emails_by_state": {"email_messages_total"},
    # «exportaciones por estado, duración y tamaño del paquete»
    "exports_by_state_duration_and_size": {
        "export_requests_total",
        "export_duration_ms",
        "export_package_size_bytes",
    },
    # «divergencias de proyección»
    "projection_divergences": {"loop_projection_divergences_total"},
    # «vencidos por zona»
    "overdue_by_zone": {"loop_overdue_findings"},
    # «hallazgos sin clasificar por zona y antigüedad»
    "unclassified_by_zone_and_age": {
        "loop_unclassified_findings",
        "loop_unclassified_oldest_age_seconds",
    },
    # «validaciones de texto por veredicto»
    "text_validations_by_verdict": {"loop_text_validations_total"},
}

EXPECTED_ALARMS: Final[dict[str, str]] = {
    "loop_dead_letter": "loop_dead_letter_entries_total",
    "assistant_unavailable": "assistant_unavailable",
    "export_stuck_or_failed": "export_stuck_or_failed",
    "email_bounce_rate": "email_messages_total",
    "loop_projection_divergence": "loop_projection_divergences_total",
    "loop_periodic_task_stale": "loop_periodic_task_last_success_age_seconds",
    "assistant_token_budget": "assistant_token_budget_used_ratio",
    "assistant_rate_limit_headroom": "assistant_rate_limit_headroom_ratio",
}

EXPECTED_EXPORT_FAILED: Final = "export_failed_total"
"""El contador de ``export_worker`` para ``export-failed`` (TASK-317 y TASK-332)."""

EXPECTED_TRANSVERSAL: Final = {
    "loop_bulkhead_in_use",
    "loop_bulkhead_size",
    "loop_bulkhead_saturated_total",
    "loop_egress_blocked_total",
}

PERMITTED_IDENTIFIERS: Final = frozenset({"organization_id", "plant_id"})
FORBIDDEN_LABELS: Final = frozenset(
    {"node_id", "actor_id", "user_id", "zone_id", "correlation_id", "term_id"}
    | (redaction.IDENTIFIER_KEYS - PERMITTED_IDENTIFIERS)
)


def _names() -> set[str]:
    return {spec.name.value for spec in LOOP_CATALOG}


def test_catalog_is_exactly_nfr_laz_41_plus_nfr_laz_42_plus_transversal() -> None:
    from_requirement = set().union(*EXPECTED_NFR_LAZ_41.values())
    from_alarms = set(EXPECTED_ALARMS.values())
    assert _names() == (
        from_requirement | from_alarms | EXPECTED_TRANSVERSAL | {EXPECTED_EXPORT_FAILED}
    )
    assert len(LOOP_CATALOG) == len(_names()), "nombre repetido en el catálogo"


def test_every_nfr_laz_41_item_maps_to_its_metrics() -> None:
    declared = {key: {name.value for name in names} for key, names in NFR_LAZ_41.items()}
    assert declared == EXPECTED_NFR_LAZ_41
    assert len(NFR_LAZ_41) == 11


def test_the_eight_alarms_of_nfr_laz_42_each_with_its_metric() -> None:
    assert {c.key: c.metric.value for c in LOOP_ALARM_CONDITIONS} == EXPECTED_ALARMS
    assert len(LOOP_ALARM_CONDITIONS) == 8
    names = _names()
    for condition in (*LOOP_ALARM_CONDITIONS, EGRESS_BLOCKED_ALARM, EXPORT_FAILED_ALARM):
        assert condition.metric.value in names
        if condition.denominator is not None:
            assert condition.denominator.value in names
        spec = next(spec for spec in LOOP_CATALOG if spec.name is condition.metric)
        for key, values in condition.selector.items():
            assert key in spec.attributes
            assert values <= redaction.DEFAULT_POLICY.values(key)


def test_transversal_metrics_are_the_bulkheads_and_the_factory() -> None:
    assert {name.value for name in TRANSVERSAL_METRICS} == EXPECTED_TRANSVERSAL
    assert EGRESS_BLOCKED_ALARM.metric.value == "loop_egress_blocked_total"
    assert EXPORT_FAILED_ALARM.metric.value == EXPECTED_EXPORT_FAILED


def test_no_user_or_node_dimension_and_only_organization_and_plant_ids() -> None:
    for spec in LOOP_CATALOG:
        assert not spec.attributes & FORBIDDEN_LABELS, (spec.name, spec.attributes)
        identifiers = spec.attributes & redaction.IDENTIFIER_KEYS
        assert identifiers <= PERMITTED_IDENTIFIERS, (spec.name, identifiers)


def test_every_declared_attribute_is_a_closed_list_of_the_policy() -> None:
    policy = redaction.DEFAULT_POLICY
    for spec in LOOP_CATALOG:
        assert spec.attributes <= policy.keys, (spec.name, spec.attributes - policy.keys)
        for key in spec.attributes - PERMITTED_IDENTIFIERS:
            assert key not in redaction.INTEGER_KEYS, (spec.name, key)


def test_no_name_collides_with_u02_or_u03() -> None:
    assert not _names() & {spec.name.value for spec in CATALOG}


def test_loop_metrics_builds_one_instrument_per_spec_of_its_kind() -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    try:
        metrics = LoopMetrics(provider.get_meter(METER_NAME))
        kinds = {
            MetricKind.COUNTER: LoopCounter,
            MetricKind.HISTOGRAM: LoopHistogram,
            MetricKind.GAUGE: LoopGauge,
        }
        for spec in LOOP_CATALOG:
            assert isinstance(metrics.instrument(spec.name), kinds[spec.kind])
    finally:
        provider.shutdown()


def test_term_id_leaves_logs_as_identifier_never_the_term_value() -> None:
    """El filtro de redacción heredado, ampliado: ``term_id`` sale si es un UUID; su valor, o
    cualquier otro texto en su lugar, nunca (NFR-LAZ-17)."""
    policy = redaction.AttributePolicy()
    term_id = "0192a6f0-7b3c-7cde-8f00-0123456789ab"
    assert policy.clean_value("term_id", term_id, redaction.REDACTED) == term_id
    assert policy.clean_value("term_id", "garantizado", redaction.REDACTED) == redaction.REDACTED
    assert policy.clean({"term_value": "garantizado", "matched_value": "cero"}) == {}
