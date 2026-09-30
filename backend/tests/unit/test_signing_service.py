"""``SigningService``: arranque cerrado, refresco degradado y rotación sin efectos a medias
(TASK-115; PAT-NUC-SEG-04, PAT-NUC-RES-02 y 03, FS-NUC-05).

Dobles en memoria de ``tests.signing_support``; la versión con Secrets Manager y KMS reales
(LocalStack) está en ``tests/integration/test_secrets_localstack.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint
from vigia_contracts.clock import SimulatedClock

from tests.factories import make_context
from tests.hibp_service import metric_total, metrics_with_reader
from tests.signing_support import (
    PROVIDER_ORGANIZATION_ID,
    START,
    GatedKeyStore,
    InMemoryKeyStore,
    InMemorySecrets,
    RecordingEvents,
    SigningWorld,
    bootstrapped_world,
    build_service,
    provider_context,
)
from vigia_platform.shared.context import ActorKind
from vigia_platform.shared.observability.metrics import MetricName
from vigia_platform.shared.secrets import SecretsUnavailable
from vigia_platform.shared.signing import (
    KeyStateConflict,
    KeyStatus,
    KeyTransition,
    RotationCommit,
    SigningKeyUnavailable,
    SigningNotReady,
    SigningPurpose,
    SigningService,
    SigningStartupError,
)
from vigia_platform.shared.signing.keys import active_key, format_timestamp

pytestmark = pytest.mark.asyncio


def _active_id(service: SigningService, purpose: SigningPurpose) -> str:
    key = active_key(service.all_keys(), purpose)
    assert key is not None
    return key.key_id


def _gauge_points(reader: InMemoryMetricReader, name: MetricName) -> dict[str, float]:
    points: dict[str, float] = {}
    data = reader.get_metrics_data()
    assert data is not None
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == name.value:
                    for point in metric.data.data_points:
                        assert isinstance(point, NumberDataPoint)
                        points[str(point.attributes["purpose"])] = float(point.value)
    return points


# --- Arranque cerrado -------------------------------------------------------------------------


async def test_start_with_secrets_manager_unreachable_never_becomes_ready() -> None:
    world = await bootstrapped_world()
    world.secrets.down = True
    fresh = world.new_service()
    with pytest.raises(SigningStartupError) as raised:
        await fresh.start()
    assert set(raised.value.causes) == {"secret_unavailable"}
    assert len(raised.value.causes) == 5
    assert fresh.ready is False
    with pytest.raises(SigningNotReady):
        fresh.sign(SigningPurpose.CHECKPOINT, {"a": 1})


async def test_start_with_the_database_unreachable_never_becomes_ready() -> None:
    world = await bootstrapped_world()
    world.store.down = True
    fresh = world.new_service()
    with pytest.raises(SigningStartupError) as raised:
        await fresh.start()
    assert raised.value.causes == ("store_unavailable",)
    assert fresh.ready is False


async def test_start_without_a_required_active_key_fails_closed() -> None:
    clock = SimulatedClock(START)
    service = build_service(clock, InMemorySecrets(), InMemoryKeyStore(), RecordingEvents())
    with pytest.raises(SigningStartupError) as raised:
        await service.start()
    assert raised.value.causes == ("missing_active_key",) * 5
    assert service.ready is False
    # Solo el alta inicial arranca sin claves.
    await service.start(required=())
    assert service.ready is True


async def test_start_with_a_deleted_secret_fails_closed() -> None:
    world = await bootstrapped_world()
    reference = world.store.keys[_active_id(world.service, SigningPurpose.GATE)].private_key_ref
    del world.secrets.values[reference]
    fresh = world.new_service()
    with pytest.raises(SigningStartupError) as raised:
        await fresh.start()
    assert raised.value.causes == ("secret_not_found",)


@pytest.mark.parametrize(
    "tamper",
    [
        lambda raw: bytes([raw[0] ^ 1]) + raw[1:],
        lambda raw: raw[:31],
        lambda raw: raw + b"\x00",
        lambda raw: b"",
    ],
    ids=["otra_clave", "31_bytes", "33_bytes", "vacio"],
)
async def test_start_with_material_that_does_not_match_the_public_key_fails_closed(
    tamper: Any,
) -> None:
    world = await bootstrapped_world()
    reference = world.store.keys[_active_id(world.service, SigningPurpose.CATALOG)].private_key_ref
    world.secrets.values[reference] = tamper(world.secrets.values[reference])
    fresh = world.new_service()
    with pytest.raises(SigningStartupError) as raised:
        await fresh.start()
    assert raised.value.causes == ("key_mismatch",)
    assert "arn:" not in str(raised.value)


async def test_start_rejects_a_store_with_two_active_keys_for_a_purpose() -> None:
    world = await bootstrapped_world()
    gate = world.store.keys[_active_id(world.service, SigningPurpose.GATE)]

    overlapping_as_active = replace(gate, key_id="gate-copia-activa")
    world.store.keys[overlapping_as_active.key_id] = overlapping_as_active
    world.secrets.values[overlapping_as_active.private_key_ref] = world.secrets.values[
        gate.private_key_ref
    ]
    with pytest.raises(SigningStartupError) as raised:
        await world.new_service().start()
    assert "invalid_key_state" in raised.value.causes


# --- Refresco en operación --------------------------------------------------------------------


async def test_refresh_without_secrets_keeps_signing_with_memory_and_counts_the_failure() -> None:
    metrics, reader = metrics_with_reader()
    world = await bootstrapped_world(metrics=metrics)
    old_catalog = _active_id(world.service, SigningPurpose.CATALOG)
    other = world.new_service()
    await other.start()
    await other.rotate(SigningPurpose.CATALOG, context=provider_context())

    world.secrets.down = True
    assert await world.service.refresh() is False
    assert metric_total(reader, MetricName.SECRETS_REFRESH_FAILED) == 1
    # Sigue firmando con lo que tiene en memoria.
    envelope = world.service.sign(SigningPurpose.GATE, _gate_payload())
    assert envelope.key_id == _active_id(world.service, SigningPurpose.GATE)
    assert _active_id(world.service, SigningPurpose.CATALOG) == old_catalog

    world.secrets.down = False
    assert await world.service.refresh() is True
    assert _active_id(world.service, SigningPurpose.CATALOG) != old_catalog
    assert metric_total(reader, MetricName.SECRETS_REFRESH_FAILED) == 1


async def test_refresh_with_the_database_down_keeps_the_current_state() -> None:
    world = await bootstrapped_world()
    before = world.service.all_keys()
    world.store.down = True
    assert await world.service.refresh() is False
    assert world.service.all_keys() == before


async def test_refresh_drops_material_of_keys_that_are_no_longer_active() -> None:
    world = await bootstrapped_world()
    first = _active_id(world.service, SigningPurpose.CHECKPOINT)
    await world.service.rotate(SigningPurpose.CHECKPOINT, context=provider_context())
    assert await world.service.refresh() is True
    assert first not in world.service._private
    assert len(world.service._private) == 5


async def test_run_refresh_stops_when_asked() -> None:
    world = await bootstrapped_world()
    stop = asyncio.Event()
    task = asyncio.create_task(world.service.run_refresh(stop, interval_seconds=0.01))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=1)


# --- Rotación sin efectos a medias ------------------------------------------------------------


async def test_rotation_without_secrets_manager_changes_nothing() -> None:
    world = await bootstrapped_world()
    keys, publications = dict(world.store.keys), list(world.store.publications)
    events = len(world.events.rotated)
    world.secrets.down = True
    with pytest.raises(SecretsUnavailable):
        await world.service.rotate(SigningPurpose.CATALOG, context=provider_context())
    assert world.store.keys == keys and world.store.publications == publications
    assert len(world.events.rotated) == events
    assert {k.key_id: k for k in world.service.all_keys()} == keys


async def test_a_failed_commit_leaves_memory_as_it_was() -> None:
    world = await bootstrapped_world()
    before = world.service.all_keys()
    envelope = world.service.current_key_set_envelope()
    world.store.fail_next_commit = True
    with pytest.raises(ConnectionError):
        await world.service.rotate(SigningPurpose.KEY_SET, context=provider_context())
    assert world.service.all_keys() == before
    assert world.service.current_key_set_envelope() == envelope
    assert len(world.events.rotated) == 5
    # El secreto huérfano no se usa: el siguiente intento crea otro y todo cuadra.
    await world.service.rotate(SigningPurpose.KEY_SET, context=provider_context())
    assert {k.key_id: k for k in world.service.all_keys()} == world.store.keys


async def test_a_ledger_failure_after_the_commit_surfaces_and_memory_matches_the_store() -> None:
    """El expediente abre su propia transacción: si falla después de confirmar las claves, la
    rotación sube el error (la ruta responde error y se ve), y memoria y base siguen iguales."""
    world = await bootstrapped_world()
    world.events.down = True
    with pytest.raises(ConnectionError):
        await world.service.rotate(SigningPurpose.GATE, context=provider_context())
    assert {k.key_id: k for k in world.service.all_keys()} == world.store.keys
    assert len(world.store.keys) == 6
    world.events.down = False
    envelope = world.service.sign(SigningPurpose.GATE, _gate_payload())
    assert envelope.key_id == _active_id(world.service, SigningPurpose.GATE)


async def test_rotation_is_written_only_in_the_provider_chain() -> None:
    world = await bootstrapped_world()
    with pytest.raises(PermissionError):
        await world.service.rotate(
            SigningPurpose.CATALOG, context=make_context(organization_id=uuid.uuid4())
        )
    context = provider_context(ActorKind.SYSTEM)
    result = await world.service.rotate(SigningPurpose.CATALOG, context=context)
    assert world.events.contexts[-1] is context
    assert result.new_key.rotated_by == context.actor.id
    assert world.events.rotated[-1]["previous_key_id"] == result.previous_key_id


async def test_rotation_before_start_is_refused() -> None:
    world = await bootstrapped_world()
    with pytest.raises(SigningNotReady):
        await world.new_service().rotate(SigningPurpose.GATE, context=provider_context())


async def test_first_rotation_of_a_purpose_has_no_previous_key() -> None:
    clock = SimulatedClock(START)
    events = RecordingEvents()
    service = build_service(clock, InMemorySecrets(), InMemoryKeyStore(), events)
    await service.start(required=())
    result = await service.rotate(SigningPurpose.CATALOG, context=provider_context())
    # Sin key_set todavía no se publica nada; la primera publicación sale con la key_set.
    assert result.previous_key_id is None and result.publication is None
    assert "previous_key_id" not in events.rotated[-1]
    key_set = await service.rotate(SigningPurpose.KEY_SET, context=provider_context())
    assert key_set.publication is not None
    assert key_set.publication.signed_by_key_id == key_set.new_key.key_id
    assert set(key_set.publication.key_ids) == {
        result.new_key.key_id,
        key_set.new_key.key_id,
    }


# --- Métrica y validación ---------------------------------------------------------------------


async def test_days_to_expiry_is_published_per_purpose() -> None:
    metrics, reader = metrics_with_reader()
    world = await bootstrapped_world(metrics=metrics)
    world.clock.advance(timedelta(days=100, hours=12).total_seconds())
    values = world.service.report_days_to_expiry(world.clock.now())
    assert values == {purpose: 264 for purpose in SigningPurpose}
    assert _gauge_points(reader, MetricName.SIGNING_KEY_DAYS_TO_EXPIRY) == {
        purpose.value: 264.0 for purpose in SigningPurpose
    }
    world.clock.advance(timedelta(days=265).total_seconds())
    assert set(world.service.report_days_to_expiry(world.clock.now()).values()) == {-1}


@pytest.mark.parametrize(
    "environment",
    ["", "Pilot", "pilot prod", "pilot/../x", "pilot\n", "pílot", "\uff50ilot", "-pilot"],
)
async def test_environment_name_is_closed(environment: str) -> None:
    clock = SimulatedClock(START)
    with pytest.raises(ValueError, match="environment"):
        SigningService(
            provider_organization_id=PROVIDER_ORGANIZATION_ID,
            store=InMemoryKeyStore(),
            secrets=InMemorySecrets(),
            events=RecordingEvents(),
            clock=clock,
            environment=environment,
        )


async def test_expired_overlapping_keys_retire_exactly_at_valid_until() -> None:
    world: SigningWorld = await bootstrapped_world()
    first = _active_id(world.service, SigningPurpose.LIVE_VIEW_TOKEN)
    await world.service.rotate(SigningPurpose.LIVE_VIEW_TOKEN, context=provider_context())
    until = world.store.keys[first].valid_until
    world.clock.set(until - timedelta(milliseconds=1))
    assert await world.service.retire_expired() == ()
    world.clock.set(until)
    retired = await world.service.retire_expired()
    assert [t.key_id for t in retired] == [first]
    assert world.store.keys[first].status is KeyStatus.RETIRED


async def test_signing_needs_the_active_key_to_be_valid_now() -> None:
    world = await bootstrapped_world()
    world.clock.advance(timedelta(days=365).total_seconds())
    with pytest.raises(SigningKeyUnavailable):
        world.service.sign(SigningPurpose.GATE, _gate_payload())


def _gate_payload() -> dict[str, Any]:
    return {
        "zone_id": str(uuid.uuid4()),
        "organization_id": str(uuid.uuid4()),
        "plant_id": str(uuid.uuid4()),
        "mounting_gate": {"status": "pending"},
        "usage_gate": {"status": "pending"},
        "resulting_mode": "no_capture",
        "issued_at": "2026-09-30T12:00:00.000Z",
        "valid_until": "2026-10-07T12:00:00.000Z",
    }


# --- Concurrencia entre procesos y con el refresco (revisión del PR #21, ronda 1) -------------


def _store_active(world: SigningWorld, purpose: SigningPurpose) -> str:
    key = active_key(world.store.keys.values(), purpose)
    assert key is not None
    return key.key_id


async def test_r1_a_refresh_that_read_before_a_rotation_does_not_reinstall_the_old_state() -> None:
    """Regresión R1: el refresco tomó la foto de la base antes de que este proceso rotara; al
    terminar no reinstala el estado anterior ni se vuelve a firmar con la clave superada."""
    store = GatedKeyStore()
    world = await bootstrapped_world(store=store)
    gate = asyncio.Event()
    store.gate = gate
    refreshing = asyncio.create_task(world.service.refresh())
    await store.reading.wait()  # la foto anterior a la rotación ya está tomada
    rotating = asyncio.create_task(
        world.service.rotate(SigningPurpose.LIVE_VIEW_TOKEN, context=provider_context())
    )
    for _ in range(5):
        await asyncio.sleep(0)
    gate.set()
    await asyncio.wait_for(asyncio.gather(refreshing, rotating), timeout=5)
    active = _store_active(world, SigningPurpose.LIVE_VIEW_TOKEN)
    assert world.service.sign_detached(SigningPurpose.LIVE_VIEW_TOKEN, b"x").key_id == active
    assert world.service.current_publication() == world.store.publications[-1].record
    assert {k.key_id: k for k in world.service.all_keys()} == world.store.keys


async def test_r2_a_stale_process_rotates_from_the_confirmed_state() -> None:
    """Regresión R2: B rota catalog; A, sin refrescar, rota gate. La publicación de A lleva la
    catalog nueva y la anterior en solapamiento con el valid_until acotado de la base."""
    world = await bootstrapped_world()
    process_b = world.new_service()
    await process_b.start()
    await process_b.rotate(SigningPurpose.CATALOG, context=provider_context())
    world.clock.advance(60)
    result = await world.service.rotate(SigningPurpose.GATE, context=provider_context())
    publication = result.publication
    assert publication is not None
    published = {key["key_id"]: key for key in publication.keys}
    expected = sorted(
        key_id
        for key_id, key in world.store.keys.items()
        if key.purpose is not SigningPurpose.CHECKPOINT
        and key.status in (KeyStatus.ACTIVE, KeyStatus.OVERLAPPING)
    )
    assert sorted(published) == expected
    assert _store_active(world, SigningPurpose.CATALOG) in published
    for key_id, key in published.items():
        assert key["valid_until"] == format_timestamp(world.store.keys[key_id].valid_until)
    assert {k.key_id: k for k in world.service.all_keys()} == world.store.keys


async def test_a_commit_computed_from_a_superseded_state_is_rejected() -> None:
    world = await bootstrapped_world()
    gate = world.store.keys[_store_active(world, SigningPurpose.GATE)]
    stale = KeyTransition(
        key_id=gate.key_id,
        status=KeyStatus.RETIRED,
        valid_until=gate.valid_until,
        expected_status=KeyStatus.OVERLAPPING,  # ya no lo está: sigue activa
    )
    before = dict(world.store.keys)
    with pytest.raises(KeyStateConflict):
        await world.store.commit_transitions([stale])
    with pytest.raises(KeyStateConflict):
        await world.store.commit_rotation(
            RotationCommit(new_key=gate, transitions=(stale,), publication=None)
        )
    assert world.store.keys == before


async def test_retire_expired_reads_the_confirmed_state() -> None:
    world = await bootstrapped_world()
    process_b = world.new_service()
    await process_b.start()
    first = await process_b.rotate(SigningPurpose.GATE, context=provider_context())
    world.clock.advance(timedelta(days=31).total_seconds())
    # A no vio la rotación de B y aun así retira la gate anterior, que ya venció.
    retired = await world.service.retire_expired()
    assert [t.key_id for t in retired] == [first.previous_key_id]
    assert {k.key_id: k for k in world.service.all_keys()} == world.store.keys


async def test_key_set_is_not_signed_through_the_port() -> None:
    world = await bootstrapped_world()
    with pytest.raises(ValueError, match="key_set"):
        world.service.sign(SigningPurpose.KEY_SET, {"keys": [], "issued_at": "x"})


async def test_refresh_counts_a_secrets_outage_even_without_new_keys() -> None:
    metrics, reader = metrics_with_reader()
    world = await bootstrapped_world(metrics=metrics)
    world.secrets.down = True
    assert await world.service.refresh() is True  # nada nuevo: sigue con lo de memoria
    assert metric_total(reader, MetricName.SECRETS_REFRESH_FAILED) == 1
    world.service.sign(SigningPurpose.CHECKPOINT, {"n": 1})
