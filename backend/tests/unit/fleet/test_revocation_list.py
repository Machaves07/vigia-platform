"""Lista de revocación global de ``vigia-node-ca`` sin base (TASK-220; LC-GOB-11, LC-GOB-18).

- **Contenido** (``revoked_certificates``): revocadas y sustituidas no vencidas; una
  ``overlapping`` fuera de su solapamiento cuenta como sustituida; las vencidas y las ``active``
  nunca; bordes de ``expires_at`` y de las 24 h del solapamiento.
- **Ciclo** (``cycle_reason``): marca, regeneración diaria en el borde de las 24 h, forzada.
- **Firma** (``NodeCaRevocationListSigner``): la lista verifica con la clave pública de la raíz
  publicada, la emite esa raíz (nombre e identificador de autoridad), lleva ``CRLNumber`` y
  ``next_update = last_update + 7 días``; KMS caído, colgado o una raíz ajena terminan en
  ``RevocationListPublishFailed(SIGN)`` en ≤ 5 s.
- **Publicación** (``TrustStorePublisher`` sobre los dobles de ``tests/revocation_list_support``):
  objeto versionado, la versión exacta al almacén, cuenta comprobada, las anteriores retiradas;
  un paso parcial se repite sin efecto doble; **cada** paso que no responde termina en ≤ 5 s.
- **Servicio** (``RevocationListService``): la marca se limpia con la generación leída al empezar;
  el fallo la deja intacta y suma ``revocation_list_publish_failed``; una lista a menos de 24 h de
  vencer suma también; el candado de publicación; el arrendamiento perdido no publica; ningún PEM,
  número de serie ni ARN en los registros.

Solo datos generados. Los topes de 5 s son los de producción (NFR-GOB-43): las pruebas que tratan
del tope miden con márgenes de segundos.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid

import pytest
from cryptography import x509
from cryptography.x509.oid import ExtensionOID
from hypothesis import example, given
from hypothesis import strategies as st

from tests.dispatch_support import metric_points, metrics_with_reader
from tests.fleet_credentials_support import MemoryKms, root_bundle_for
from tests.revocation_list_support import (
    TRUST_STORE_ARN,
    FakeTrustStore,
    MemoryCredential,
    MemoryCredentials,
    MemoryEdge,
    MemoryScope,
    MemoryStates,
    crl_of,
)
from vigia_platform.fleet.adapters.ca.crl_signing import NodeCaRevocationListSigner
from vigia_platform.fleet.adapters.ca.trust_store_publisher import TrustStorePublisher
from vigia_platform.fleet.application.revocation_list_task import (
    SCHEDULE,
    TASK_NAME,
    CycleOutcome,
    RevocationListService,
    register_regenerate_revocation_list,
)
from vigia_platform.fleet.domain.enums import CredentialStatus
from vigia_platform.fleet.domain.node_credential import OVERLAP
from vigia_platform.fleet.domain.revocation_list import (
    CredentialRevocationFacts,
    CycleReason,
    PublishStep,
    RevocationListPublishFailed,
    RevocationListStatus,
    RevocationReason,
    SignedRevocationList,
    alarm_active,
    cycle_reason,
    plan_revocation_list,
    revoked_certificates,
)
from vigia_platform.shared.clock import SimulatedClock, SystemClock
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.observability.metrics import MetricName
from vigia_platform.shared.outbox.registries import (
    OutboxRegistrationRejected,
    PeriodicTaskRegistry,
    Schedule,
    TaskIteration,
)
from vigia_platform.shared.worker.leases import LeaseLost

NOW = dt.datetime(2026, 10, 5, 12, 0, 0, tzinfo=dt.UTC)
DAY = dt.timedelta(days=1)
SECOND = dt.timedelta(seconds=1)
STEP_TIMEOUT = 5.0
WALL = SystemClock()
"""Reloj de pared, solo para medir los topes de producción."""
WALL_MARGIN = 2.0
"""Margen de pared sobre el tope de 5 s: segundos, nunca milisegundos (retro 14)."""


def facts(
    serial: str,
    status: CredentialStatus,
    *,
    issued_at: dt.datetime = NOW - 30 * DAY,
    expires_at: dt.datetime = NOW + 300 * DAY,
    revoked_at: dt.datetime | None = None,
    successor_issued_at: dt.datetime | None = None,
) -> CredentialRevocationFacts:
    return CredentialRevocationFacts(
        certificate_serial=serial,
        status=status,
        issued_at=issued_at,
        expires_at=expires_at,
        revoked_at=revoked_at,
        successor_issued_at=successor_issued_at,
    )


# --- Contenido ------------------------------------------------------------------------------------


def test_revoked_and_superseded_unexpired_credentials_are_listed_in_serial_order() -> None:
    entries = revoked_certificates(
        [
            facts("0b", CredentialStatus.REVOKED, revoked_at=NOW - DAY),
            facts("0a", CredentialStatus.SUPERSEDED, successor_issued_at=NOW - 3 * DAY),
            facts("0c", CredentialStatus.ACTIVE),
        ],
        NOW,
    )
    assert [(e.serial_number, e.revocation_date, e.reason) for e in entries] == [
        (0x0A, NOW - 3 * DAY + OVERLAP, RevocationReason.SUPERSEDED),
        (0x0B, NOW - DAY, RevocationReason.REVOKED),
    ]


def test_expired_credentials_are_retired_at_the_exact_boundary() -> None:
    at_expiry = facts("01", CredentialStatus.REVOKED, revoked_at=NOW - DAY, expires_at=NOW)
    just_before = facts(
        "02", CredentialStatus.REVOKED, revoked_at=NOW - DAY, expires_at=NOW + SECOND
    )
    assert [e.serial_number for e in revoked_certificates([at_expiry, just_before], NOW)] == [2]


def test_an_overlapping_credential_counts_only_once_its_24_hours_are_over() -> None:
    inside = facts("01", CredentialStatus.OVERLAPPING, successor_issued_at=NOW - OVERLAP + SECOND)
    boundary = facts("02", CredentialStatus.OVERLAPPING, successor_issued_at=NOW - OVERLAP)
    without_successor = facts("03", CredentialStatus.OVERLAPPING)
    entries = revoked_certificates([inside, boundary, without_successor], NOW)
    assert [(e.serial_number, e.reason) for e in entries] == [(2, RevocationReason.SUPERSEDED)]


def test_revocation_dates_never_lie_in_the_future_and_are_whole_seconds() -> None:
    late = facts(
        "01",
        CredentialStatus.SUPERSEDED,
        successor_issued_at=NOW,  # su solapamiento aún no termina: se lista con la fecha de ahora
    )
    revoked = facts(
        "02", CredentialStatus.REVOKED, revoked_at=NOW - dt.timedelta(microseconds=1_500)
    )
    first, second = revoked_certificates([late, revoked], NOW)
    assert first.revocation_date == NOW
    assert second.revocation_date == NOW - SECOND  # truncada al segundo, nunca redondeada arriba


def test_a_naive_instant_is_rejected() -> None:
    with pytest.raises(ValueError, match="zona horaria"):
        revoked_certificates([], NOW.replace(tzinfo=None))


_STATUS = st.sampled_from(list(CredentialStatus))


@st.composite
def _facts(draw: st.DrawFn) -> CredentialRevocationFacts:
    status = draw(_STATUS)
    issued = NOW - dt.timedelta(seconds=draw(st.integers(0, 400 * 86_400)))
    expires = issued + dt.timedelta(seconds=draw(st.integers(1, 400 * 86_400)))
    successor = draw(
        st.none() | st.integers(0, 400 * 86_400).map(lambda s: NOW - dt.timedelta(seconds=s))
    )
    return facts(
        draw(st.from_regex(r"[0-9a-f]{1,40}", fullmatch=True)),
        status,
        issued_at=issued,
        expires_at=expires,
        revoked_at=issued if status is CredentialStatus.REVOKED else None,
        successor_issued_at=successor,
    )


@given(st.lists(_facts(), max_size=30))
# Contraejemplo reducido (regresión permanente): «0» y «00» son el mismo número de serie; la
# comparación es por valor, como en la lista, no por el texto.
@example(
    [
        facts(
            "0", CredentialStatus.REVOKED, issued_at=NOW, expires_at=NOW + SECOND, revoked_at=NOW
        ),
        facts("00", CredentialStatus.ACTIVE, issued_at=NOW, expires_at=NOW + SECOND),
    ]
)
def test_the_list_never_has_expired_or_active_credentials_nor_duplicates(
    items: list[CredentialRevocationFacts],
) -> None:
    entries = revoked_certificates(items, NOW)
    serials = [e.serial_number for e in entries]
    assert serials == sorted(set(serials))
    live = {int(f.certificate_serial, 16) for f in items if f.expires_at > NOW}
    active_only = {
        int(f.certificate_serial, 16)
        for f in items
        if f.status is CredentialStatus.ACTIVE
        and not any(
            int(o.certificate_serial, 16) == int(f.certificate_serial, 16)
            and o.status is not CredentialStatus.ACTIVE
            for o in items
        )
    }
    assert set(serials) <= live
    assert not set(serials) & active_only
    assert all(e.revocation_date <= NOW for e in entries)


# --- Ciclo --------------------------------------------------------------------------------------


def status(**changes: object) -> RevocationListStatus:
    values: dict[str, object] = {
        "dirty_generation": 3,
        "published_generation": 3,
        "published_at": NOW - DAY + SECOND,
        "next_update": NOW + 6 * DAY,
        "entries": 1,
        "crl_number": 7,
    }
    values.update(changes)
    return RevocationListStatus(**values)  # type: ignore[arg-type]


def test_cycle_reason_follows_the_mark_the_daily_boundary_and_the_command() -> None:
    assert cycle_reason(status(), NOW) is None
    assert cycle_reason(status(published_at=NOW - DAY), NOW) is CycleReason.DAILY
    assert cycle_reason(status(published_at=None, next_update=None), NOW) is CycleReason.DAILY
    assert cycle_reason(status(dirty_generation=4), NOW) is CycleReason.DIRTY
    assert cycle_reason(status(), NOW, force=True) is CycleReason.FORCED


def test_the_plan_lasts_seven_days_from_the_whole_second() -> None:
    plan = plan_revocation_list([], crl_number=1, now=NOW + dt.timedelta(microseconds=999_999))
    assert plan.last_update == NOW
    assert plan.next_update - plan.last_update == dt.timedelta(days=7)
    for wrong in (0, -1, True):
        with pytest.raises(ValueError):
            plan_revocation_list([], crl_number=wrong, now=NOW)


def test_the_alarm_condition_is_a_failure_or_less_than_24_hours_left() -> None:
    assert alarm_active(failed=True, seconds_left=7 * 86_400)
    assert alarm_active(failed=False, seconds_left=86_399.999)
    assert not alarm_active(failed=False, seconds_left=86_400)
    assert not alarm_active(failed=False, seconds_left=None)


def test_the_task_is_registered_global_every_60_seconds_for_u03() -> None:
    registry = PeriodicTaskRegistry()
    service = _service(Harness())
    task = register_regenerate_revocation_list(registry, service)
    assert (task.task_name, task.schedule, task.unit, task.iteration) == (
        TASK_NAME,
        Schedule.every(60),
        ActorUnit.U03,
        TaskIteration.GLOBAL,
    )
    assert Schedule.every(60) == SCHEDULE


def test_registry_iteration_defaults_to_per_organization_and_rejects_anything_else() -> None:
    registry = PeriodicTaskRegistry()

    async def handler(_: object) -> None:
        return None

    task = registry.register("u02_like", Schedule.every(60), handler, unit=ActorUnit.U02)
    assert task.iteration is TaskIteration.PER_ORGANIZATION
    with pytest.raises(OutboxRegistrationRejected, match="iteration"):
        registry.register(
            "bad_iteration",
            Schedule.every(60),
            handler,
            unit=ActorUnit.U03,
            iteration="global",  # type: ignore[arg-type]
        )


# --- Firma ------------------------------------------------------------------------------------


class Harness:
    """Los dobles de un ciclo: KMS en memoria, ``vigia-edge``, el almacén y la fila global."""

    def __init__(self) -> None:
        self.kms = MemoryKms()
        self.edge = MemoryEdge()
        self.store = FakeTrustStore(self.edge.fetch)
        self.states = MemoryStates()
        self.credentials = MemoryCredentials()
        self.clock = SimulatedClock(NOW)
        self.organizations = [uuid.uuid4(), uuid.uuid4()]
        self.scope = MemoryScope(self.organizations)
        self.metrics, self.reader = metrics_with_reader()
        self.root: x509.Certificate | None = None

    async def publish_root(self) -> x509.Certificate:
        body, root = await root_bundle_for(self.kms, NOW)
        self.edge.put_root(body)
        self.root = root
        return root

    def signer(self) -> NodeCaRevocationListSigner:
        return NodeCaRevocationListSigner(kms=self.kms, key_id=self.kms.key_id, roots=self.edge)

    def publisher(self) -> TrustStorePublisher:
        return TrustStorePublisher(
            storage=self.edge,
            elb=self.store,
            trust_store_arn=TRUST_STORE_ARN,
            bucket=self.edge.bucket,
        )

    def credential(
        self, organization: int, status: CredentialStatus, **changes: object
    ) -> MemoryCredential:
        row = MemoryCredential(
            organization_id=self.organizations[organization],
            serial=uuid.uuid4().hex,
            status=status,
            issued_at=NOW - 10 * DAY,
            expires_at=NOW + 300 * DAY,
            revoked_at=NOW - DAY if status is CredentialStatus.REVOKED else None,
        )
        for name, value in changes.items():
            setattr(row, name, value)
        self.credentials.rows.append(row)
        return row

    def counter(self) -> float:
        return sum(
            v for _, v in metric_points(self.reader, MetricName.REVOCATION_LIST_PUBLISH_FAILED)
        )

    def gauge(self, name: MetricName) -> list[float]:
        return [v for _, v in metric_points(self.reader, name)]


def _service(harness: Harness) -> RevocationListService:
    return RevocationListService(
        states=harness.states,
        credentials=harness.credentials,
        signer=harness.signer(),
        publisher=harness.publisher(),
        clock=harness.clock,
        metrics=harness.metrics,
    )


def _plan_with(serials: list[int]) -> object:
    from vigia_platform.fleet.domain.revocation_list import RevokedCertificate

    return plan_revocation_list(
        [RevokedCertificate(s, NOW - DAY, RevocationReason.REVOKED) for s in serials],
        crl_number=42,
        now=NOW,
    )


@pytest.mark.asyncio
async def test_the_signed_list_verifies_with_the_root_and_carries_number_dates_and_reasons() -> (
    None
):
    harness = Harness()
    root = await harness.publish_root()
    plan = plan_revocation_list(
        revoked_certificates(
            [
                facts("0a", CredentialStatus.REVOKED, revoked_at=NOW - DAY),
                facts("0b", CredentialStatus.SUPERSEDED, successor_issued_at=NOW - 2 * DAY),
            ],
            NOW,
        ),
        crl_number=9,
        now=NOW,
    )
    signed = await harness.signer().sign(plan)
    crl = crl_of(signed.pem)
    assert crl.is_signature_valid(root.public_key())  # type: ignore[arg-type]
    assert crl.issuer == root.subject
    assert crl.next_update_utc - crl.last_update_utc == dt.timedelta(days=7)
    assert crl.last_update_utc == NOW
    assert crl.extensions.get_extension_for_class(x509.CRLNumber).value.crl_number == 9
    authority = crl.extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_KEY_IDENTIFIER).value
    subject_key = root.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
    assert authority.key_identifier == subject_key.digest
    reasons = {
        entry.serial_number: entry.extensions.get_extension_for_class(x509.CRLReason).value.reason
        for entry in crl
    }
    assert reasons == {0x0A: x509.ReasonFlags.unspecified, 0x0B: x509.ReasonFlags.superseded}
    assert harness.kms.signs == 1
    assert (signed.crl_number, signed.entries) == (9, 2)


@pytest.mark.asyncio
async def test_kms_failure_or_a_foreign_root_fail_closed_at_the_sign_step() -> None:
    harness = Harness()
    await harness.publish_root()
    harness.kms.fail = True
    with pytest.raises(RevocationListPublishFailed) as failed:
        await harness.signer().sign(_plan_with([1]))  # type: ignore[arg-type]
    assert failed.value.step is PublishStep.SIGN
    other = Harness()
    await other.publish_root()
    foreign = NodeCaRevocationListSigner(
        kms=harness.kms, key_id=harness.kms.key_id, roots=other.edge
    )
    harness.kms.fail = False
    with pytest.raises(RevocationListPublishFailed):
        await foreign.sign(_plan_with([1]))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_hanging_kms_ends_within_five_seconds() -> None:
    harness = Harness()
    await harness.publish_root()
    harness.kms.hang = True
    started = WALL.monotonic()
    with pytest.raises(RevocationListPublishFailed):
        await harness.signer().sign(_plan_with([1]))  # type: ignore[arg-type]
    assert WALL.monotonic() - started <= STEP_TIMEOUT + WALL_MARGIN


# --- Publicación ------------------------------------------------------------------------------


async def _signed(harness: Harness, serials: list[int], number: int = 1) -> SignedRevocationList:
    from vigia_platform.fleet.domain.revocation_list import RevokedCertificate

    plan = plan_revocation_list(
        [RevokedCertificate(s, NOW - DAY, RevocationReason.REVOKED) for s in serials],
        crl_number=number,
        now=NOW,
    )
    return await harness.signer().sign(plan)


@pytest.mark.asyncio
async def test_publication_versions_the_object_adds_it_checks_the_count_and_retires_the_old() -> (
    None
):
    harness = Harness()
    await harness.publish_root()
    publisher = harness.publisher()
    first = await publisher.publish(await _signed(harness, [1, 2], 1))
    second = await publisher.publish(await _signed(harness, [1, 2, 3], 2))
    versions = [version for version, _ in harness.edge.versions["ca/crl.pem"]]
    assert versions == [first.object_version_id, second.object_version_id]
    (current,) = harness.store.current()
    assert (current.version, current.entries, current.key) == (versions[1], 3, "ca/crl.pem")
    assert harness.store.removed == [first.revocation_id]
    assert (second.removed, second.revocation_id) == (1, current.revocation_id)
    # El almacén nunca se quedó sin lista: se añadió antes de retirar.
    assert harness.store.switch.calls == [
        "describe",
        "add",
        "describe_ids",
        "describe",
        "add",
        "describe_ids",
        "remove",
    ]


@pytest.mark.asyncio
async def test_a_partial_step_is_repeated_without_double_effect() -> None:
    harness = Harness()
    await harness.publish_root()
    publisher = harness.publisher()
    await publisher.publish(await _signed(harness, [1], 1))
    harness.store.switch.fail.add("remove")
    with pytest.raises(RevocationListPublishFailed) as failed:
        await publisher.publish(await _signed(harness, [1, 2], 2))
    assert failed.value.step is PublishStep.REMOVE_REVOCATIONS
    assert len(harness.store.current()) == 2  # la nueva quedó añadida, la vieja sin retirar
    harness.store.switch.fail.clear()
    third = await publisher.publish(await _signed(harness, [1, 2], 3))
    (current,) = harness.store.current()
    assert current.revocation_id == third.revocation_id and third.removed == 2


@pytest.mark.asyncio
async def test_a_count_mismatch_fails_the_verify_step_and_keeps_a_list_in_the_store() -> None:
    harness = Harness()
    await harness.publish_root()
    signed = await _signed(harness, [1, 2])
    lying = SignedRevocationList(
        pem=signed.pem,
        crl_number=signed.crl_number,
        last_update=signed.last_update,
        next_update=signed.next_update,
        entries=3,
    )
    with pytest.raises(RevocationListPublishFailed) as failed:
        await harness.publisher().publish(lying)
    assert failed.value.step is PublishStep.VERIFY_REVOCATIONS
    assert len(harness.store.current()) == 1


@pytest.mark.asyncio
async def test_an_unversioned_bucket_stops_before_the_trust_store() -> None:
    harness = Harness()
    await harness.publish_root()

    class Unversioned(MemoryEdge):
        async def put_object(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            head = await super().put_object(*args, **kwargs)  # type: ignore[arg-type]
            from dataclasses import replace

            return replace(head, version_id=None)

    edge = Unversioned()
    publisher = TrustStorePublisher(
        storage=edge, elb=harness.store, trust_store_arn=TRUST_STORE_ARN, bucket=edge.bucket
    )
    with pytest.raises(RevocationListPublishFailed) as failed:
        await publisher.publish(await _signed(harness, [1]))
    assert failed.value.step is PublishStep.PUT_OBJECT
    assert harness.store.switch.calls == []


@pytest.mark.asyncio
async def test_each_step_that_does_not_answer_ends_within_five_seconds() -> None:
    """Cinco publicadores a la vez, cada uno colgado en un paso distinto, con el tope de 5 s."""
    hanging = {
        PublishStep.PUT_OBJECT: ("edge", "put_object"),
        PublishStep.LIST_REVOCATIONS: ("store", "describe"),
        PublishStep.ADD_REVOCATIONS: ("store", "add"),
        PublishStep.VERIFY_REVOCATIONS: ("store", "describe_ids"),
        PublishStep.REMOVE_REVOCATIONS: ("store", "remove"),
    }
    harnesses: dict[PublishStep, Harness] = {}
    for step, (where, operation) in hanging.items():
        harness = Harness()
        await harness.publish_root()
        if step is PublishStep.REMOVE_REVOCATIONS:
            await harness.publisher().publish(await _signed(harness, [1]))
        (harness.edge if where == "edge" else harness.store).switch.hang.add(operation)
        harnesses[step] = harness

    async def attempt(step: PublishStep, harness: Harness) -> tuple[PublishStep, float]:
        signed = await _signed(harness, [1, 2], 2)
        started = WALL.monotonic()
        try:
            await harness.publisher().publish(signed)
        except RevocationListPublishFailed as failure:
            return failure.step, WALL.monotonic() - started
        raise AssertionError(f"{step} no falló")

    try:
        results = await asyncio.gather(*(attempt(s, h) for s, h in harnesses.items()))
    finally:
        for harness in harnesses.values():
            harness.edge.switch.release()
            harness.store.switch.release()
    assert [step for step, _ in results] == list(hanging)
    assert all(elapsed <= STEP_TIMEOUT + WALL_MARGIN for _, elapsed in results), results


# --- Servicio ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_dirty_cycle_publishes_both_organizations_and_clears_the_mark() -> None:
    harness = Harness()
    await harness.publish_root()
    a = harness.credential(0, CredentialStatus.REVOKED)
    b = harness.credential(1, CredentialStatus.REVOKED)
    harness.credential(1, CredentialStatus.ACTIVE)
    harness.states.published_at = NOW - 60 * SECOND
    harness.states.mark_dirty(NOW)
    cycle = await _service(harness).run_cycle(harness.scope)
    assert (cycle.outcome, cycle.reason, cycle.entries, cycle.mark_cleared) == (
        CycleOutcome.PUBLISHED,
        CycleReason.DIRTY,
        2,
        True,
    )
    assert not harness.states.dirty and harness.states.dirty_since is None
    assert harness.scope.read_contexts == harness.organizations
    assert harness.credentials.reads == harness.organizations
    crl = crl_of(harness.edge.versions["ca/crl.pem"][-1][1])
    assert {entry.serial_number for entry in crl} == {int(a.serial, 16), int(b.serial, 16)}
    assert harness.gauge(MetricName.REVOCATION_LIST_SECONDS_TO_EXPIRY) == [7 * 86_400]
    assert harness.gauge(MetricName.REVOCATION_LIST_ENTRIES) == [2]
    assert harness.counter() == 0


@pytest.mark.asyncio
async def test_a_clean_and_fresh_list_does_nothing_but_report() -> None:
    harness = Harness()
    await harness.publish_root()
    harness.states.published_at = NOW - DAY + SECOND
    harness.states.next_update = NOW + 6 * DAY
    cycle = await _service(harness).run_cycle(harness.scope)
    assert cycle.outcome is CycleOutcome.UP_TO_DATE
    assert harness.edge.versions.get("ca/crl.pem") is None
    assert harness.scope.read_contexts == []
    assert harness.gauge(MetricName.REVOCATION_LIST_SECONDS_TO_EXPIRY) == [6 * 86_400]


@pytest.mark.asyncio
async def test_a_failed_publication_keeps_the_mark_counts_and_fails_the_handler(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = Harness()
    await harness.publish_root()
    harness.credential(0, CredentialStatus.REVOKED)
    harness.states.mark_dirty(NOW)
    harness.store.switch.fail.add("add")
    service = _service(harness)
    with caplog.at_level(logging.ERROR), pytest.raises(RevocationListPublishFailed) as failed:
        await service(harness.scope)
    assert failed.value.step is PublishStep.ADD_REVOCATIONS
    # El paso llega al registro con su código (no como «other»): menos de 20 caracteres.
    fields = [getattr(r, "vigia_fields", {}) for r in caplog.records]
    assert {"task": TASK_NAME, "code": "crl_add_revocation"} in fields
    assert harness.states.dirty and harness.states.published_generation == 0
    assert harness.counter() == 1
    # Al restablecerse, el primer ciclo publica y limpia la marca.
    harness.store.switch.fail.clear()
    await service(harness.scope)
    assert not harness.states.dirty
    assert harness.counter() == 1
    assert harness.states.crl_number == 2  # el número del intento fallido no se reutiliza


@pytest.mark.asyncio
async def test_a_revocation_during_publication_keeps_the_mark_for_the_next_cycle() -> None:
    harness = Harness()
    await harness.publish_root()
    harness.credential(0, CredentialStatus.REVOKED)
    late = harness.credential(1, CredentialStatus.ACTIVE)
    harness.states.mark_dirty(NOW)
    service = _service(harness)
    real_add = harness.store.add_trust_store_revocations

    def revoke_while_adding(**kwargs: object) -> object:
        # La revocación confirma en su propia transacción mientras el ciclo publica.
        harness.credentials.revoke(late.serial, NOW)
        harness.states.mark_dirty(NOW)
        harness.store.add_trust_store_revocations = real_add  # type: ignore[method-assign]
        return real_add(**kwargs)  # type: ignore[arg-type]

    harness.store.add_trust_store_revocations = revoke_while_adding  # type: ignore[method-assign]
    first = await service.run_cycle(harness.scope)
    assert first.outcome is CycleOutcome.PUBLISHED and not first.mark_cleared
    assert harness.states.dirty and harness.states.published_generation == 1
    assert harness.states.dirty_since == NOW
    second = await service.run_cycle(harness.scope)
    assert second.mark_cleared and not harness.states.dirty
    crl = crl_of(harness.edge.versions["ca/crl.pem"][-1][1])
    assert int(late.serial, 16) in {entry.serial_number for entry in crl}


@pytest.mark.asyncio
async def test_less_than_24_hours_to_expiry_raises_the_same_condition() -> None:
    harness = Harness()
    harness.states.published_at = NOW - 3600 * SECOND
    harness.states.next_update = NOW + 86_399 * SECOND
    cycle = await _service(harness).run_cycle(harness.scope)
    assert cycle.outcome is CycleOutcome.UP_TO_DATE
    assert harness.counter() == 1
    assert harness.gauge(MetricName.REVOCATION_LIST_SECONDS_TO_EXPIRY) == [86_399]


@pytest.mark.asyncio
async def test_another_publisher_holding_the_lock_skips_the_cycle() -> None:
    harness = Harness()
    harness.states.locked = True
    harness.states.mark_dirty(NOW)
    cycle = await _service(harness).run_cycle(harness.scope)
    assert cycle.outcome is CycleOutcome.BUSY
    assert harness.states.dirty and harness.edge.versions == {}


@pytest.mark.asyncio
async def test_a_lost_lease_publishes_nothing() -> None:
    harness = Harness()
    await harness.publish_root()
    harness.states.mark_dirty(NOW)
    harness.scope.lease_lost = True
    with pytest.raises(LeaseLost):
        await _service(harness).run_cycle(harness.scope)
    assert "ca/crl.pem" not in harness.edge.versions
    assert harness.states.dirty and not harness.states.locked


@pytest.mark.asyncio
async def test_the_forced_cycle_publishes_without_mark_and_dry_run_writes_nothing() -> None:
    harness = Harness()
    await harness.publish_root()
    harness.credential(0, CredentialStatus.REVOKED)
    harness.states.published_at = NOW - SECOND
    service = _service(harness)
    preview = await service.run_cycle(harness.scope, force=True, dry_run=True)
    assert (preview.outcome, preview.entries, preview.crl_number) == (CycleOutcome.DRY_RUN, 1, 1)
    assert harness.states.crl_number == 0 and "ca/crl.pem" not in harness.edge.versions
    forced = await service.run_cycle(harness.scope, force=True)
    assert (forced.outcome, forced.reason) == (CycleOutcome.PUBLISHED, CycleReason.FORCED)


@pytest.mark.asyncio
async def test_no_pem_serial_or_arn_reaches_the_logs(caplog: pytest.LogCaptureFixture) -> None:
    harness = Harness()
    await harness.publish_root()
    row = harness.credential(0, CredentialStatus.REVOKED)
    harness.states.mark_dirty(NOW)
    service = _service(harness)
    with caplog.at_level(logging.DEBUG):
        harness.store.switch.fail.add("add")
        await service.run_cycle(harness.scope)
        harness.store.switch.fail.clear()
        harness.kms.fail = True
        await service.run_cycle(harness.scope)
        harness.kms.fail = False
        await service.run_cycle(harness.scope)
    text = "\n".join(
        f"{record.getMessage()} {getattr(record, 'vigia_fields', '')} {record.exc_text or ''}"
        for record in caplog.records
    )
    assert caplog.records
    for secret in ("BEGIN", row.serial, str(int(row.serial, 16)), "arn:", harness.edge.bucket):
        assert secret not in text, secret
