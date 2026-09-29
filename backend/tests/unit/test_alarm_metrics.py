"""Exactamente una métrica por condición de alarma de NFR-NUC-38 (TASK-104, criterio 3).

La tabla ``NFR_NUC_38`` copia, condición por condición, el texto del requisito:

    ``aidlc-docs/construction/plataforma-nucleo/nfr-requirements/nfr-requirements.md``,
    fila NFR-NUC-38 «Alarmas del núcleo (RNF-RES-09)»: «`integrity_compromised` (máxima
    severidad, inmediata); `dead_letter_created`; `security_alert`; tasa de
    `chain_locked_timeout` > 1 % de escrituras en 5 min; conexiones del pool > 80 %; tasa de
    errores del servidor > 1 % en 5 min; p95 de cualquier operación de NFR-NUC-01 por encima del
    doble del objetivo durante 15 min; tarea de puntos de control o de verificación sin
    ejecutar en 26 h; muestra de evidencias sin ejecutar en 26 h; rotación de clave vencida; y
    las de infraestructura (operación en una sola zona, retraso de replicación, fallo de copia,
    cuotas > 80 %) en Infrastructure Design».

Las diez primeras son de la aplicación y cada una debe tener su métrica en
``metrics.ALARM_CONDITIONS``. Las cuatro de infraestructura salen de los servicios de AWS
(balanceador, RDS, copias, Service Quotas; ``infrastructure-design.md`` §9.4) y no tienen
métrica de aplicación.

La lista de la tarea mezcla métricas de NFR-NUC-38 con otras de NFR-NUC-01, PAT-NUC-ESC-01,
PAT-NUC-RES-03 y PAT-NUC-REN-05, y no nombra las de la tasa de errores del servidor ni la del
p95. Manda la tabla: ``NOT_NFR_NUC_38`` recoge las de la tarea que no alimentan una condición de
NFR-NUC-38, y todas siguen en el catálogo con nombre fijo.
"""

from __future__ import annotations

from collections import Counter

import pytest
from opentelemetry.metrics import NoOpMeter

from vigia_platform.shared.observability import metrics as metrics_module
from vigia_platform.shared.observability import redaction
from vigia_platform.shared.observability.metrics import (
    ALARM_CONDITIONS,
    CATALOG,
    OPERATION_P95_TARGET_MS,
    MetricKind,
    MetricName,
    PlatformMetrics,
)

NFR_NUC_38: tuple[tuple[str, str, str], ...] = (
    ("integrity_compromised", "`integrity_compromised` (máxima severidad, inmediata)", "app"),
    ("dead_letter_created", "`dead_letter_created`", "app"),
    ("security_alert", "`security_alert`", "app"),
    ("chain_locked_timeout_rate", "tasa de `chain_locked_timeout` > 1 % de escrituras", "app"),
    ("db_pool_saturation", "conexiones del pool > 80 %", "app"),
    ("server_error_rate", "tasa de errores del servidor > 1 % en 5 min", "app"),
    (
        "operation_latency_p95",
        "p95 de cualquier operación de NFR-NUC-01 por encima del doble del objetivo",
        "app",
    ),
    (
        "checkpoint_or_verification_task_stale",
        "tarea de puntos de control o de verificación sin ejecutar en 26 h",
        "app",
    ),
    ("evidence_sample_stale", "muestra de evidencias sin ejecutar en 26 h", "app"),
    ("signing_key_rotation_overdue", "rotación de clave vencida", "app"),
    ("single_zone_operation", "operación en una sola zona", "infrastructure"),
    ("replication_lag", "retraso de replicación", "infrastructure"),
    ("backup_failure", "fallo de copia", "infrastructure"),
    ("quota_usage", "cuotas > 80 %", "infrastructure"),
)

TASK_LIST = (
    "integrity_compromised_total",
    "dead_letter_created_total",
    "security_alert_total",
    "chain_locked_timeout_total",
    "ledger_writes_total",
    "db_pool_in_use",
    "outbox_oldest_pending_age_seconds",
    "periodic_task_last_success_age_seconds",
    "signing_key_days_to_expiry",
    "default_partition_rows",
    "otel_dropped_total",
    "hibp_fallback_used",
    "cpu_pool_wait_ms",
)
"""Nombres que TASK-104 enumera en su alcance."""

NOT_NFR_NUC_38 = {
    "ledger_writes_total": "denominador de chain_locked_timeout_rate",
    "outbox_oldest_pending_age_seconds": "alarma outbox-oldest-age (NFR-NUC-01) y escalado",
    "default_partition_rows": "alarma default-partition-rows (PAT-NUC-ESC-01)",
    "otel_dropped_total": "alarma otel-dropped (PAT-NUC-RES-03)",
    "hibp_fallback_used": "PAT-NUC-RES-03, FS-NUC-06",
    "cpu_pool_wait_ms": "PAT-NUC-REN-05",
}


def test_every_application_condition_has_exactly_one_metric() -> None:
    application = [key for key, _, origin in NFR_NUC_38 if origin == "app"]
    module = [condition.key for condition in ALARM_CONDITIONS]
    assert module == application
    assert all(count == 1 for count in Counter(module).values())


def test_infrastructure_conditions_have_no_application_metric() -> None:
    infrastructure = {key for key, _, origin in NFR_NUC_38 if origin == "infrastructure"}
    assert infrastructure.isdisjoint(condition.key for condition in ALARM_CONDITIONS)


def test_each_condition_watches_a_distinct_series_of_a_published_metric() -> None:
    published = {spec.name: spec for spec in CATALOG}
    watched: set[tuple[MetricName, str, frozenset[str]]] = set()
    for condition in ALARM_CONDITIONS:
        spec = published[condition.metric]
        assert set(condition.selector) <= spec.attributes, condition.key
        if condition.denominator is not None:
            assert condition.denominator in published, condition.key
        series: set[tuple[MetricName, str, frozenset[str]]]
        if not condition.selector:
            series = {(condition.metric, "", frozenset())}
        else:
            series = {(condition.metric, key, values) for key, values in condition.selector.items()}
        assert watched.isdisjoint(series), condition.key
        watched |= series
    tasks = [
        condition.selector["task"]
        for condition in ALARM_CONDITIONS
        if condition.metric is MetricName.PERIODIC_TASK_LAST_SUCCESS_AGE_SECONDS
    ]
    assert len(tasks) == 2 and tasks[0].isdisjoint(tasks[1])


def test_selector_values_belong_to_their_closed_lists() -> None:
    for condition in ALARM_CONDITIONS:
        for key, values in condition.selector.items():
            assert values <= redaction.DEFAULT_POLICY.values(key), condition.key


def test_task_list_is_published_and_the_table_decides_what_is_an_alarm() -> None:
    published = {spec.name.value for spec in CATALOG}
    assert set(TASK_LIST) <= published
    condition_metrics = {condition.metric.value for condition in ALARM_CONDITIONS}
    denominators = {c.denominator.value for c in ALARM_CONDITIONS if c.denominator is not None}
    assert {name for name in TASK_LIST if name not in condition_metrics} == set(NOT_NFR_NUC_38)
    assert "ledger_writes_total" in denominators
    assert (condition_metrics | denominators) - set(TASK_LIST) == {
        "http_server_errors_total",
        "http_server_requests_total",
        "operation_duration_ms",
        "db_pool_size",
    }


def test_latency_operations_have_their_nfr_nuc_01_target() -> None:
    assert set(OPERATION_P95_TARGET_MS) == redaction.DEFAULT_POLICY.values("operation")


def test_catalog_names_are_unique_and_each_has_its_instrument() -> None:
    names = [spec.name for spec in CATALOG]
    assert len(names) == len(set(names)) == len(MetricName)
    instruments = PlatformMetrics(NoOpMeter("pruebas"))
    for spec in CATALOG:
        instrument = instruments.instrument(spec.name)
        assert instrument.spec is spec
        expected = {
            MetricKind.COUNTER: metrics_module.Counter,
            MetricKind.HISTOGRAM: metrics_module.Histogram,
            MetricKind.GAUGE: metrics_module.Gauge,
        }[spec.kind]
        assert isinstance(instrument, expected)


@pytest.mark.parametrize("spec", CATALOG, ids=[spec.name.value for spec in CATALOG])
def test_metric_attributes_are_identifiers_or_enumerations(spec: metrics_module.MetricSpec) -> None:
    allowed = redaction.DEFAULT_POLICY.keys
    assert spec.attributes <= allowed
    assert "correlation_id" not in spec.attributes  # Cardinalidad por petición: solo en trazas.
