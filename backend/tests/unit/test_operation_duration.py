"""Emisores de las alarmas de latencia de NFR-NUC-38 (VIG-136, seguimiento de VIG-55).

- Cada operación de ``OPERATION_P95_TARGET_MS`` registra ``operation_duration_ms`` con su
  ``operation`` al terminar con resultado, medida con el ``Clock`` inyectado; una excepción no se
  mide. La entrega de la bandeja (``outbox_delivery``) va en
  ``tests/integration/test_outbox_dispatch.py``: se mide tras confirmar en la base.
- ``IntegrityService`` publica ``chain_verification_records_per_second`` de cada verificación
  incremental (alarma ``latency-verify-chains-incremental``), y nunca de la completa.
- ``configure_telemetry`` exporta los histogramas como exponenciales (``awsemf`` solo publica
  esos con ``Values`` y ``Counts``, de los que CloudWatch saca el p95), y la limpieza de
  atributos fusiona dos histogramas exponenciales sin perder cuentas.

Solo datos generados; servicios con sus colaboradores sustituidos donde la prueba no los usa.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    Buckets,
    ExponentialHistogram,
    ExponentialHistogramDataPoint,
    HistogramDataPoint,
    InMemoryMetricReader,
    Metric,
    MetricExporter,
    MetricExportResult,
    MetricsData,
    ResourceMetrics,
    ScopeMetrics,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from vigia_contracts.models.enumerations import AcceptanceStatus

from tests.writer_support import unit_context
from vigia_platform.identity.auth.login import LoginService
from vigia_platform.ledger.application.coverage import CoverageService
from vigia_platform.ledger.application.reader import LectorExpediente, LedgerQueryInvalid
from vigia_platform.ledger.application.writer import (
    EscritorExpediente,
    LedgerRejection,
    LedgerRejectionCode,
    Receipt,
    _Rejected,
)
from vigia_platform.ledger.chain.checkpoints import CheckpointChain
from vigia_platform.ledger.chain.verify import (
    IntegrityResult,
    IntegrityService,
    IntegrityStatus,
    VerificationMode,
)
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.ledger.registry import RecordTypeRegistry
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.observability import redaction
from vigia_platform.shared.observability.metrics import (
    OPERATION_P95_TARGET_MS,
    MetricName,
    Operation,
    PlatformMetrics,
)
from vigia_platform.shared.observability.tracing import (
    HISTOGRAM_MAX_BUCKETS,
    TelemetrySettings,
    configure_telemetry,
    sanitize_metrics,
)
from vigia_platform.shared.tokens import (
    LiveViewRejection,
    LiveViewTokenRejected,
    LiveViewTokenService,
)

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
PLANT = uuid.UUID("5a5b5c5d-6e6f-4a4b-8c8d-9e9f0a0b0c0d")


def _metrics() -> tuple[PlatformMetrics, InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    return PlatformMetrics(provider.get_meter("pruebas")), reader


def _points(reader: InMemoryMetricReader, name: MetricName) -> list[Any]:
    data = reader.get_metrics_data()
    if data is None:  # nada medido todavía
        return []
    return [
        point
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == name.value
        for point in metric.data.data_points
    ]


def _durations(reader: InMemoryMetricReader) -> dict[str, tuple[int, float]]:
    """``operation`` → (cuenta, suma en ms) de ``operation_duration_ms``."""
    durations: dict[str, tuple[int, float]] = {}
    for point in _points(reader, MetricName.OPERATION_DURATION_MS):
        assert isinstance(point, HistogramDataPoint)
        assert set(point.attributes) == {"operation"}
        durations[str(point.attributes["operation"])] = (point.count, point.sum)
    return durations


def _context() -> Any:
    return unit_context(uuid.uuid4(), ActorUnit.U03)


def test_every_operation_has_a_p95_target_and_a_registered_value() -> None:
    assert {operation.value for operation in Operation} == set(OPERATION_P95_TARGET_MS)
    assert redaction.DEFAULT_POLICY.values("operation") == set(OPERATION_P95_TARGET_MS)


def test_a_negative_interval_is_recorded_as_zero() -> None:
    metrics, reader = _metrics()
    metrics.record_operation(Operation.OUTBOX_DELIVERY, -0.5)
    assert _durations(reader) == {"outbox_delivery": (1, 0.0)}


def test_an_unknown_operation_is_refused() -> None:
    metrics, reader = _metrics()
    with pytest.raises(ValueError):
        metrics.record_operation(cast(Operation, "ledger_delete"), 0.1)
    assert _durations(reader) == {}


# --- ledger_write y ledger_write_with_evidence ------------------------------------------------


def _writer(clock: SimulatedClock, metrics: PlatformMetrics, prepared: object) -> Any:
    writer = EscritorExpediente(
        database=cast(Any, None),
        registry=RecordTypeRegistry(),
        free_text=FreeTextPolicyRegistry(),
        evidence=cast(Any, None),
        outbox=cast(Any, None),
        clock=clock,
        metrics=metrics,
    )

    async def prepare(*_: Any) -> object:
        clock.advance(0.03)  # pasos 1 a 6
        if isinstance(prepared, BaseException):
            raise prepared
        return prepared

    async def commit(*_: Any) -> Receipt | LedgerRejection:
        clock.advance(0.09)  # paso 7
        return Receipt(record_id=uuid.uuid4(), received_at=NOW, status=AcceptanceStatus.ACCEPTED)

    writer._prepare = prepare
    writer._commit = commit
    return writer


@pytest.mark.parametrize(
    ("evidences", "operation"),
    [((), "ledger_write"), ((object(), object()), "ledger_write_with_evidence")],
)
def test_a_committed_write_records_its_duration_by_evidence(
    evidences: tuple[object, ...], operation: str
) -> None:
    clock = SimulatedClock(NOW)
    metrics, reader = _metrics()
    writer = _writer(clock, metrics, SimpleNamespace(evidences=evidences))
    result = asyncio.run(writer.write(_context(), "probe", {}))
    assert isinstance(result, Receipt)
    # Desde la llamada: pasos 1 a 6 (30 ms) y paso 7 (90 ms).
    assert _durations(reader) == {operation: (1, pytest.approx(120.0))}


def test_a_write_rejected_before_step_7_or_a_duplicate_is_not_measured() -> None:
    clock = SimulatedClock(NOW)
    metrics, reader = _metrics()
    rejected = _Rejected(LedgerRejection.of(LedgerRejectionCode.CONTENT_INVALID))
    result = asyncio.run(_writer(clock, metrics, rejected).write(_context(), "probe", {}))
    assert isinstance(result, LedgerRejection)
    duplicate = Receipt(
        record_id=uuid.uuid4(), received_at=NOW, status=AcceptanceStatus.ACCEPTED_DUPLICATE
    )
    assert asyncio.run(_writer(clock, metrics, duplicate).write(_context(), "probe", {})) is (
        duplicate
    )
    assert _durations(reader) == {}


# --- ledger_list y ledger_timeline ------------------------------------------------------------


def test_a_list_records_ledger_list_and_a_failed_one_does_not() -> None:
    clock = SimulatedClock(NOW)
    metrics, reader = _metrics()
    reader_port = LectorExpediente(
        database=cast(Any, None), audit=cast(Any, None), clock=clock, metrics=metrics
    )
    page = object()
    failure: list[BaseException] = []

    async def listed(*_: Any) -> object:
        clock.advance(0.25)
        if failure:
            raise failure[0]
        return page

    reader_port._list = listed  # type: ignore[method-assign]
    assert asyncio.run(reader_port.list(_context())) is page
    failure.append(LedgerQueryInvalid("página"))
    with pytest.raises(LedgerQueryInvalid):
        asyncio.run(reader_port.list(_context()))
    assert _durations(reader) == {"ledger_list": (1, pytest.approx(250.0))}


def test_a_timeline_records_ledger_timeline_and_a_failed_one_does_not() -> None:
    clock = SimulatedClock(NOW)
    metrics, reader = _metrics()
    coverage = CoverageService(
        database=cast(Any, None), audit=cast(Any, None), clock=clock, metrics=metrics
    )
    timeline = object()
    failure: list[BaseException] = []

    async def composed(*_: Any) -> object:
        clock.advance(0.8)
        if failure:
            raise failure[0]
        return timeline

    coverage._timeline = composed  # type: ignore[method-assign]
    assert asyncio.run(coverage.linea_de_tiempo(_context(), uuid.uuid4(), cast(Any, None))) is (
        timeline
    )
    failure.append(RuntimeError("base caída"))
    with pytest.raises(RuntimeError):
        asyncio.run(coverage.linea_de_tiempo(_context(), uuid.uuid4(), cast(Any, None)))
    assert _durations(reader) == {"ledger_timeline": (1, pytest.approx(800.0))}


# --- login ------------------------------------------------------------------------------------


def test_every_login_outcome_records_login() -> None:
    clock = SimulatedClock(NOW)
    metrics, reader = _metrics()
    login = LoginService(
        store=cast(Any, None),
        sessions=cast(Any, None),
        passwords=cast(Any, None),
        second_factor=cast(Any, None),
        contexts=cast(Any, None),
        clock=clock,
        provider_organization_id=uuid.uuid4(),
        origin_key=b"k" * 32,
        metrics=metrics,
    )
    outcomes = iter([object(), object()])  # p. ej. aceptado y credenciales inválidas

    async def authenticated(*_: Any) -> object:
        clock.advance(0.4)
        return next(outcomes)

    login._authenticate = authenticated  # type: ignore[method-assign]
    asyncio.run(login.authenticate("persona@example.org", "clave", "192.0.2.1"))
    asyncio.run(login.authenticate("persona@example.org", "otra", "192.0.2.1"))
    assert _durations(reader) == {"login": (2, pytest.approx(800.0))}


# --- live_view_token --------------------------------------------------------------------------


def test_an_issued_token_records_live_view_token_and_a_rejection_does_not() -> None:
    clock = SimulatedClock(NOW)
    metrics, reader = _metrics()
    service = LiveViewTokenService(
        database=cast(Any, None),
        authorizer=cast(Any, None),
        audit=cast(Any, None),
        outbox=cast(Any, None),
        signer=cast(Any, None),
        clock=clock,
        metrics=metrics,
    )
    issued = object()
    failure: list[BaseException] = []

    async def issue(*_: Any) -> object:
        clock.advance(0.06)
        if failure:
            raise failure[0]
        return issued

    service._issue = issue  # type: ignore[method-assign]
    assert asyncio.run(service.issue(_context(), uuid.uuid4())) is issued
    failure.append(LiveViewTokenRejected(LiveViewRejection.ZONE_WITHOUT_NODE))
    with pytest.raises(LiveViewTokenRejected):
        asyncio.run(service.issue(_context(), uuid.uuid4()))
    assert _durations(reader) == {"live_view_token": (1, pytest.approx(60.0))}


# --- chain_verification_records_per_second ----------------------------------------------------


class _Store:
    async def record(self, *_: Any) -> datetime:
        return NOW


def _result(
    chain: CheckpointChain, mode: VerificationMode, from_sequence: int, to_sequence: int
) -> IntegrityResult:
    return IntegrityResult(
        chain=chain,
        mode=mode,
        status=IntegrityStatus.INTACT,
        from_sequence=from_sequence,
        to_sequence=to_sequence,
        verified_hash="a" * 64,
        head_sequence=to_sequence,
        broken_sequence=None,
        broken_entry_id=None,
        reason=None,
        canonical_checked=0,
        checkpoints_checked=0,
        sample_seed="0" * 32 if mode is VerificationMode.INCREMENTAL else None,
    )


def _verify(
    mode: VerificationMode,
    from_sequence: int,
    to_sequence: int,
    chain: CheckpointChain | None = None,
) -> list[Any]:
    clock = SimulatedClock(NOW)
    metrics, reader = _metrics()
    service = IntegrityService(
        store=cast(Any, _Store()), keys=cast(Any, None), clock=clock, metrics=metrics
    )
    verified = chain if chain is not None else CheckpointChain.plant(PLANT)

    async def walk(*_: Any) -> IntegrityResult:
        clock.advance(2.0)
        return _result(verified, mode, from_sequence, to_sequence)

    service._walk = walk  # type: ignore[method-assign]
    context = unit_context(uuid.uuid4(), ActorUnit.U02)
    asyncio.run(service.verify(context, verified, mode))
    return _points(reader, MetricName.CHAIN_VERIFICATION_RECORDS_PER_SECOND)


@pytest.mark.parametrize(
    ("chain", "kind"),
    [
        (CheckpointChain.plant(PLANT), "plant"),
        (CheckpointChain.organization(), "organization"),
        (CheckpointChain.audit(), "audit"),
    ],
)
def test_an_incremental_verification_publishes_its_records_per_second(
    chain: CheckpointChain, kind: str
) -> None:
    # Secuencias 101 a 10 100: 10 000 registros en 2 s. ``chain_kind`` es un valor admitido
    # por la política (``ledger`` saldría como ``other``).
    (point,) = _verify(VerificationMode.INCREMENTAL, 101, 10_100, chain)
    assert point.value == pytest.approx(5_000.0)
    assert dict(point.attributes) == {"chain_kind": kind}


def test_a_single_new_record_is_a_rate() -> None:
    (point,) = _verify(VerificationMode.INCREMENTAL, 10, 10)
    assert point.value == pytest.approx(0.5)


@pytest.mark.parametrize(
    ("mode", "from_sequence", "to_sequence"),
    [
        (VerificationMode.FULL, 1, 10_000),  # la alarma mide la incremental
        (VerificationMode.ON_DEMAND, 1, 10_000),
        (VerificationMode.INCREMENTAL, 11, 10),  # sin registros nuevos
    ],
)
def test_no_rate_without_new_records_or_outside_the_incremental(
    mode: VerificationMode, from_sequence: int, to_sequence: int
) -> None:
    assert _verify(mode, from_sequence, to_sequence) == []


# --- exportación exponencial ------------------------------------------------------------------


class _Capture(MetricExporter):
    def __init__(self) -> None:
        super().__init__()
        self.batches: list[MetricsData] = []

    def export(
        self, metrics_data: MetricsData, timeout_millis: float = 10_000, **kwargs: Any
    ) -> MetricExportResult:
        self.batches.append(metrics_data)
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: Any) -> None:
        return None


@pytest.mark.parametrize(
    ("name", "attributes"),
    [
        (MetricName.OPERATION_DURATION_MS, {"operation": "ledger_list"}),
        (MetricName.HTTP_SERVER_DURATION_MS, {"method": "GET"}),
    ],
)
def test_p95_histograms_are_exported_as_exponential(
    name: MetricName, attributes: dict[str, str]
) -> None:
    capture = _Capture()
    telemetry = configure_telemetry(TelemetrySettings(), metric_exporter=capture)
    try:
        instrument = telemetry.metrics.instrument(name)
        for value in range(1, 101):
            instrument.record(value, attributes)  # type: ignore[union-attr]
        telemetry.meter_provider.force_flush()
    finally:
        telemetry.shutdown()
    (metric,) = [  # el primer lote (el del ``force_flush``; el cierre exporta otro)
        metric
        for resource in capture.batches[0].resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == name.value
    ]
    assert isinstance(metric.data, ExponentialHistogram)
    (point,) = metric.data.data_points
    assert dict(point.attributes) == attributes
    assert (point.count, point.sum, point.min, point.max) == (100, 5050, 1, 100)
    assert 0 < len(point.positive.bucket_counts) <= HISTOGRAM_MAX_BUCKETS


def _exponential(
    scale: int, offset: int, counts: list[int], *, label: str, zero: int = 0
) -> ExponentialHistogramDataPoint:
    return ExponentialHistogramDataPoint(
        attributes={"operation": label},
        start_time_unix_nano=1,
        time_unix_nano=2,
        count=sum(counts) + zero,
        sum=float(sum(counts)),
        scale=scale,
        zero_count=zero,
        positive=Buckets(offset=offset, bucket_counts=counts),
        negative=Buckets(offset=0, bucket_counts=[]),
        flags=0,
        min=1.0,
        max=float(len(counts)),
    )


def _sanitized(*points: ExponentialHistogramDataPoint) -> list[ExponentialHistogramDataPoint]:
    """Los puntos tras limpiar: un ``operation`` no registrado sale como ``other`` y se fusionan."""
    data = MetricsData(
        resource_metrics=[
            ResourceMetrics(
                resource=Resource.create({}),
                scope_metrics=[
                    ScopeMetrics(
                        scope=InstrumentationScope("pruebas"),
                        metrics=[
                            Metric(
                                name="operation_duration_ms",
                                description="",
                                unit="ms",
                                data=ExponentialHistogram(
                                    data_points=list(points),
                                    aggregation_temporality=AggregationTemporality.CUMULATIVE,
                                ),
                            )
                        ],
                        schema_url="",
                    )
                ],
                schema_url="",
            )
        ]
    )
    cleaned = sanitize_metrics(data, redaction.DEFAULT_POLICY)
    return list(cleaned.resource_metrics[0].scope_metrics[0].metrics[0].data.data_points)


def _value_counts(point: ExponentialHistogramDataPoint, scale: int) -> dict[int, int]:
    """Cuentas por índice de cubo reescaladas a ``scale``."""
    shift = point.scale - scale
    counts: dict[int, int] = {}
    for position, count in enumerate(point.positive.bucket_counts):
        if count:
            index = (point.positive.offset + position) >> shift
            counts[index] = counts.get(index, 0) + count
    return counts


def test_two_exponential_series_merged_by_the_cleanup_keep_every_count() -> None:
    first = _exponential(3, 10, [1, 2, 3], label="ledger_delete", zero=1)
    second = _exponential(1, 2, [4, 0, 5], label="ledger_purge", zero=2)
    (merged,) = _sanitized(first, second)
    assert dict(merged.attributes) == {"operation": "other"}
    assert (merged.count, merged.sum, merged.zero_count) == (18, 15.0, 3)
    assert merged.scale == 1
    expected = _value_counts(first, 1)
    for index, count in _value_counts(second, 1).items():
        expected[index] = expected.get(index, 0) + count
    assert _value_counts(merged, 1) == expected


@given(
    scales=st.tuples(st.integers(-2, 8), st.integers(-2, 8)),
    offsets=st.tuples(st.integers(-500, 500), st.integers(-500, 500)),
    counts=st.tuples(
        st.lists(st.integers(0, 50), min_size=1, max_size=HISTOGRAM_MAX_BUCKETS),
        st.lists(st.integers(0, 50), min_size=1, max_size=HISTOGRAM_MAX_BUCKETS),
    ),
)
def test_merging_exponential_series_never_loses_counts_nor_exceeds_the_bucket_cap(
    scales: tuple[int, int], offsets: tuple[int, int], counts: tuple[list[int], list[int]]
) -> None:
    first = _exponential(scales[0], offsets[0], counts[0], label="ledger_delete")
    second = _exponential(scales[1], offsets[1], counts[1], label="ledger_purge")
    (merged,) = _sanitized(first, second)
    assert merged.count == first.count + second.count
    assert sum(merged.positive.bucket_counts) == sum(counts[0]) + sum(counts[1])
    assert len(merged.positive.bucket_counts) <= HISTOGRAM_MAX_BUCKETS
    assert merged.scale <= min(scales)
    # Cada cuenta queda en el cubo que contiene al original a la escala final.
    expected = _value_counts(first, merged.scale)
    for index, count in _value_counts(second, merged.scale).items():
        expected[index] = expected.get(index, 0) + count
    assert _value_counts(merged, merged.scale) == expected
