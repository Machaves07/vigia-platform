"""PR-NUC-35: máquina de rotación frente a un modelo sobre historias de tiempo generadas
(TASK-115; BR-NUC-84 a 86; PBT-06).

Cada paso avanza el reloj (de milisegundos a más de un año), rota un propósito (en este proceso,
en uno nuevo que este ve al refrescar, o en un segundo proceso vivo que no ha refrescado: API y
worker a la vez), corre la tarea ``key_rotation_reminder``, retira las claves vencidas o reinicia
el proceso. Después de cada paso:

- en todo instante hay exactamente una clave ``active`` por propósito y a lo sumo una
  ``overlapping``, y el servicio, la base y el modelo coinciden clave por clave (estado y
  ``valid_until``);
- toda publicación del conjunto está firmada por una clave ``key_set`` que estaba ``active`` u
  ``overlapping`` y **vigente** al emitir, lista exactamente las claves ``active`` y
  ``overlapping`` de los cuatro propósitos del nodo y la firma verifica;
- un nodo simulado (``KeySet`` de U-01), fijado con el conjunto inicial, aplica cada publicación
  en orden; la continuidad solo se rompe cuando la ``key_set`` anterior ya había vencido, y
  entonces el nodo se vuelve a dar de alta (BR-NUC-86);
- ninguna clave ``checkpoint`` deja de estar publicada.

Además, el ejemplo del criterio de aceptación: un nodo con el conjunto inicial fijado acepta el
conjunto publicado tras rotar la propia ``key_set`` (``KeySet.apply_signed_set``).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pytest
from hypothesis import seed as hypothesis_seed
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    rule,
    run_state_machine_as_test,
)
from vigia_contracts.canonical import canonical_sha256, canonicalize
from vigia_contracts.signing import KeySet, KeySetConflictError, SignatureInvalidError

from tests.conftest import _seeds_for_profile
from tests.signing_support import (
    SigningWorld,
    StoredPublication,
    bootstrapped_world,
    provider_context,
)
from vigia_platform.shared.context import ActorKind, ScopeContext
from vigia_platform.shared.key_rotation import key_rotation_reminder_handler
from vigia_platform.shared.outbox.publish import NewEvent
from vigia_platform.shared.outbox.u02_events import KeyRotationDue
from vigia_platform.shared.signing import (
    NODE_PURPOSES,
    KeySetPublicationRecord,
    KeyStatus,
    SigningKeyRecord,
    SigningKeyUnavailable,
    SigningPurpose,
    SigningService,
    verify_detached,
)

STEPS_PER_EXAMPLE = 30
DAY = timedelta(days=1)
MS = timedelta(milliseconds=1)


# --- Modelo -----------------------------------------------------------------------------------


@dataclass
class ModelKey:
    purpose: SigningPurpose
    status: KeyStatus
    valid_from: datetime
    valid_until: datetime

    def valid_at(self, moment: datetime) -> bool:
        return self.valid_from <= moment < self.valid_until


@dataclass
class RotationModel:
    """Especificación de BR-NUC-85 y 86, escrita sin reutilizar ``shared.signing.keys``."""

    keys: dict[str, ModelKey] = field(default_factory=dict)
    last_issued: datetime | None = None
    checkpoint_ids: set[str] = field(default_factory=set)
    rotations: int = 0

    @classmethod
    def from_records(
        cls, records: Mapping[str, SigningKeyRecord], last_issued: datetime | None
    ) -> RotationModel:
        model = cls(last_issued=last_issued, rotations=len(records))
        for key_id, record in records.items():
            model.keys[key_id] = ModelKey(
                record.purpose, record.status, record.valid_from, record.valid_until
            )
            if record.purpose is SigningPurpose.CHECKPOINT:
                model.checkpoint_ids.add(key_id)
        return model

    def with_status(self, purpose: SigningPurpose, status: KeyStatus) -> list[str]:
        return [
            key_id
            for key_id, key in self.keys.items()
            if key.purpose is purpose and key.status is status
        ]

    def active(self, purpose: SigningPurpose) -> tuple[str, ModelKey] | None:
        found = self.with_status(purpose, KeyStatus.ACTIVE)
        return (found[0], self.keys[found[0]]) if found else None

    def issued_at(self, now: datetime) -> datetime:
        if self.last_issued is not None and now <= self.last_issued:
            return self.last_issued + MS
        return now

    def rotation_allowed(self, purpose: SigningPurpose, now: datetime) -> bool:
        """Otro propósito del nodo exige una ``key_set`` activa vigente al emitir."""
        if purpose not in NODE_PURPOSES or purpose is SigningPurpose.KEY_SET:
            return True
        key_set = self.active(SigningPurpose.KEY_SET)
        return key_set is None or key_set[1].valid_at(self.issued_at(now))

    def expected_signer(self, purpose: SigningPurpose, now: datetime, new_id: str) -> str | None:
        if purpose not in NODE_PURPOSES:
            return None
        key_set = self.active(SigningPurpose.KEY_SET)
        if purpose is SigningPurpose.KEY_SET:
            if key_set is not None and key_set[1].valid_at(self.issued_at(now)):
                return key_set[0]
            return new_id
        return None if key_set is None else key_set[0]

    def rotate(self, purpose: SigningPurpose, new_id: str, now: datetime) -> None:
        for key_id in self.with_status(purpose, KeyStatus.OVERLAPPING):
            key = self.keys[key_id]
            key.status = KeyStatus.RETIRED
            key.valid_until = max(min(key.valid_until, now), key.valid_from + MS)
        for key_id in self.with_status(purpose, KeyStatus.ACTIVE):
            key = self.keys[key_id]
            key.status = KeyStatus.OVERLAPPING
            key.valid_until = max(
                min(key.valid_until, now + timedelta(days=30)), key.valid_from + MS
            )
        self.keys[new_id] = ModelKey(purpose, KeyStatus.ACTIVE, now, now + timedelta(days=365))
        if purpose is SigningPurpose.CHECKPOINT:
            self.checkpoint_ids.add(new_id)
        self.rotations += 1

    def retire(self, now: datetime) -> None:
        for key in self.keys.values():
            if key.status is KeyStatus.OVERLAPPING and key.valid_until <= now:
                key.status = KeyStatus.RETIRED

    def reminder(self, now: datetime) -> tuple[set[str], list[SigningPurpose]]:
        notices: set[str] = set()
        due: list[SigningPurpose] = []
        order = [SigningPurpose.KEY_SET] + [
            p for p in SigningPurpose if p is not SigningPurpose.KEY_SET
        ]
        for purpose in order:
            found = self.active(purpose)
            if found is None:
                continue
            remaining = found[1].valid_until - now
            if remaining <= timedelta(days=30):
                due.append(purpose)
            elif timedelta(days=44) < remaining <= timedelta(days=45):
                notices.add(found[0])
        return notices, due


# --- Dobles de la tarea periódica -------------------------------------------------------------


@dataclass
class FakeTransaction:
    context: ScopeContext


@dataclass
class RecordingOutbox:
    events: list[NewEvent] = field(default_factory=list)

    async def publish(self, transaction: Any, event: NewEvent) -> Any:
        assert isinstance(transaction, FakeTransaction)
        KeyRotationDue.model_validate_json(json.dumps(dict(event.payload)))  # type: ignore[arg-type]
        self.events.append(event)
        return None


# --- Nodo simulado -----------------------------------------------------------------------------


def pinned_node(world: SigningWorld, publication: KeySetPublicationRecord) -> KeySet:
    """``KeySet`` de U-01 fijado con ``platform_public_keys`` de la alta."""
    node = KeySet(world.clock)
    node.pin_initial(
        [dict(key) for key in publication.keys],
        issued_at=publication.issued_at,
        key_set_sha256=publication.envelope.payload_canonical_sha256,
    )
    return node


def check_publication(stored: StoredPublication) -> None:
    """Firmante ``key_set`` ``active`` u ``overlapping`` y vigente al emitir; claves exactas."""
    record = stored.record
    signer = stored.keys_at_issue[record.signed_by_key_id]
    assert signer.purpose is SigningPurpose.KEY_SET
    assert signer.status in (KeyStatus.ACTIVE, KeyStatus.OVERLAPPING)
    assert signer.is_valid_at(record.issued_at)
    expected = sorted(
        key_id
        for key_id, key in stored.keys_at_issue.items()
        if key.purpose in NODE_PURPOSES and key.status in (KeyStatus.ACTIVE, KeyStatus.OVERLAPPING)
    )
    assert list(record.key_ids) == expected
    envelope = record.envelope.model_dump(mode="json", exclude_none=True)
    assert envelope["key_id"] == record.signed_by_key_id
    assert envelope["payload_canonical_sha256"] == canonical_sha256(envelope["payload"])
    assert verify_detached(
        signer.public_key, canonicalize(envelope["payload"]), envelope["signature"]
    )


# --- Máquina ----------------------------------------------------------------------------------


class KeyRotationMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.world = self.loop.run_until_complete(bootstrapped_world())
        latest = self.world.store.publications[-1]
        self.model = RotationModel.from_records(self.world.store.keys, latest.record.issued_at)
        self.node = pinned_node(self.world, latest.record)
        self.checked_publications = 0
        self.re_enrollments = 0
        # Segundo proceso vivo (API frente a worker) que nunca refresca por su cuenta: rota desde
        # una memoria que puede haberse quedado atrás (sonda R2 de la revisión del PR #21).
        self.stale = self.world.new_service()
        self.loop.run_until_complete(self.stale.start())
        self.in_sync = True
        """``False`` si el otro proceso rotó y este todavía no releyó la base."""

    def teardown(self) -> None:
        self.loop.close()

    @property
    def service(self) -> SigningService:
        return self.world.service

    def _run(self, coroutine: Any) -> Any:
        return self.loop.run_until_complete(coroutine)

    def _now(self) -> datetime:
        now = self.world.clock.now()
        return now.replace(microsecond=now.microsecond // 1000 * 1000)

    def _node_receives(self, publication: KeySetPublicationRecord, continuity: bool) -> None:
        if continuity:
            self.node.apply_signed_set(publication.envelope)
            assert publication.signed_by_key_id in self.node
        else:
            # La key_set anterior venció: el nodo no puede comprobar el conjunto (BR-NUC-86).
            with pytest.raises(SignatureInvalidError):
                self.node.apply_signed_set(publication.envelope)
            self.node = pinned_node(self.world, publication)
            self.re_enrollments += 1

    def _rotate_with(self, service: SigningService, purpose: SigningPurpose) -> None:
        now = self._now()
        before_keys = dict(self.world.store.keys)
        before_secrets = len(self.world.secrets.values)
        if not self.model.rotation_allowed(purpose, now):
            with pytest.raises(SigningKeyUnavailable):
                self._run(service.rotate(purpose, context=provider_context()))
            assert self.world.store.keys == before_keys
            assert len(self.world.secrets.values) == before_secrets
            return
        issued = self.model.issued_at(now)
        result = self._run(service.rotate(purpose, context=provider_context()))
        new_id = result.new_key.key_id
        signer = self.model.expected_signer(purpose, now, new_id)
        self.model.rotate(purpose, new_id, now)
        if signer is None:
            assert result.publication is None
            return
        publication = result.publication
        assert publication is not None
        assert publication.signed_by_key_id == signer
        assert publication.issued_at == issued
        self.model.last_issued = issued
        self._node_receives(publication, continuity=signer != new_id)

    # --- reglas --------------------------------------------------------------------------------

    @rule(
        seconds=st.one_of(
            st.integers(min_value=0, max_value=3_600),
            st.integers(min_value=1, max_value=400).map(lambda days: days * 86_400),
            st.sampled_from([29, 30, 31, 44, 45, 46, 335, 365, 366]).map(lambda d: d * 86_400),
        )
    )
    def advance(self, seconds: int) -> None:
        self.world.clock.advance(seconds)

    @rule(purpose=st.sampled_from(SigningPurpose))
    def rotate(self, purpose: SigningPurpose) -> None:
        self._rotate_with(self.service, purpose)
        self.in_sync = True  # la rotación relee la base con el candado tomado

    @rule(purpose=st.sampled_from(SigningPurpose))
    def rotate_in_another_process(self, purpose: SigningPurpose) -> None:
        other = self.world.new_service()
        self._run(other.start())
        self._rotate_with(other, purpose)
        assert self._run(self.service.refresh()) is True
        self.in_sync = True

    @rule(purpose=st.sampled_from(SigningPurpose))
    def rotate_in_the_stale_process(self, purpose: SigningPurpose) -> None:
        """Dos procesos vivos: el otro rota sin haber refrescado desde hace tiempo, y este queda
        atrás hasta su próxima lectura de la base."""
        self._rotate_with(self.stale, purpose)
        self.in_sync = False

    @rule()
    def run_reminder(self) -> None:
        now = self._now()
        notices, due = self.model.reminder(now)
        outbox = RecordingOutbox()
        handler = key_rotation_reminder_handler(
            self.service,
            outbox,
            self.world.clock,
            provider_organization_id=provider_context().organization_id,
        )
        rotations_before = len(self.world.events.rotated)
        publications_before = len(self.world.store.publications)
        self._run(handler(FakeTransaction(provider_context(ActorKind.SYSTEM))))  # type: ignore[arg-type]
        assert {e.payload["key_id"] for e in outbox.events} == notices  # type: ignore[index]
        assert all(e.event_name == "key_rotation_due" for e in outbox.events)
        rotated = self.world.events.rotated[rotations_before:]
        assert [SigningPurpose(r["purpose"]) for r in rotated] == due
        new_publications = self.world.store.publications[publications_before:]
        for content in rotated:
            purpose = SigningPurpose(content["purpose"])
            signer = self.model.expected_signer(purpose, now, content["key_id"])
            self.model.rotate(purpose, content["key_id"], now)
            if signer is not None:
                stored = new_publications.pop(0)
                assert stored.record.signed_by_key_id == signer
                self.model.last_issued = stored.record.issued_at
                self._node_receives(stored.record, continuity=signer != content["key_id"])
        assert not new_publications
        self.model.retire(now)
        self.in_sync = True  # la tarea refresca antes de decidir

    @rule()
    def retire_expired(self) -> None:
        self._run(self.service.retire_expired())
        self.model.retire(self._now())
        self.in_sync = True

    @rule()
    def restart(self) -> None:
        fresh = self.world.new_service()
        self._run(fresh.start())
        self.world.service = fresh
        self.in_sync = True

    # --- invariantes ---------------------------------------------------------------------------

    @invariant()
    def one_active_and_at_most_one_overlapping_per_purpose(self) -> None:
        for keys in (tuple(self.world.store.keys.values()), self.service.all_keys()):
            for purpose in SigningPurpose:
                statuses = [k.status for k in keys if k.purpose is purpose]
                assert statuses.count(KeyStatus.ACTIVE) == 1
                assert statuses.count(KeyStatus.OVERLAPPING) <= 1

    @invariant()
    def service_store_and_model_agree(self) -> None:
        store = {k: (v.status, v.valid_until) for k, v in self.world.store.keys.items()}
        model = {k: (v.status, v.valid_until) for k, v in self.model.keys.items()}
        assert store == model
        if self.in_sync:
            service = {k.key_id: (k.status, k.valid_until) for k in self.service.all_keys()}
            assert service == store

    @invariant()
    def every_publication_is_signed_by_a_valid_key_set(self) -> None:
        publications = self.world.store.publications
        for stored in publications[self.checked_publications :]:
            check_publication(stored)
        self.checked_publications = len(publications)
        if publications and self.in_sync:
            assert self.service.current_key_set_envelope() == publications[-1].record.envelope
        if publications:
            issued = [p.record.issued_at for p in publications]
            assert issued == sorted(issued) and len(set(issued)) == len(issued)

    @invariant()
    def checkpoint_keys_never_stop_being_published(self) -> None:
        stored = {
            k for k, v in self.world.store.keys.items() if v.purpose is SigningPurpose.CHECKPOINT
        }
        assert self.model.checkpoint_ids <= stored
        if self.in_sync:
            published = {k.key_id for k in self.service.public_keys(SigningPurpose.CHECKPOINT)}
            assert self.model.checkpoint_ids <= published

    @invariant()
    def every_rotation_and_publication_reaches_the_provider_chain(self) -> None:
        assert len(self.world.events.rotated) == self.model.rotations
        assert len(self.world.events.published) == len(self.world.store.publications)


def test_rotation_machine_matches_the_model_over_generated_time_histories() -> None:
    """PR-NUC-35 (con estado): la máquina corre con cada semilla del perfil activo."""
    for value in _seeds_for_profile():
        seeded = hypothesis_seed(value)(KeyRotationMachine)
        run_state_machine_as_test(seeded, settings=settings(stateful_step_count=STEPS_PER_EXAMPLE))


# --- Criterio de aceptación: continuidad del conjunto para un nodo ya dado de alta -------------


def test_node_with_initial_set_accepts_the_set_published_after_rotating_key_set() -> None:
    async def scenario() -> None:
        world = await bootstrapped_world()
        initial = world.store.publications[-1].record
        node = pinned_node(world, initial)
        first_key_set = initial.signed_by_key_id
        context = provider_context()

        # 1. Rotar la propia key_set: la publica la anterior, que el nodo ya tiene fijada.
        world.clock.advance((100 * DAY).total_seconds())
        rotation = await world.service.rotate(SigningPurpose.KEY_SET, context=context)
        first = rotation.publication
        assert first is not None
        assert first.signed_by_key_id == first_key_set
        assert rotation.new_key.key_id in first.key_ids and first_key_set in first.key_ids
        node.apply_signed_set(first.envelope)
        assert rotation.new_key.key_id in node

        # 2. La siguiente publicación ya la firma la key_set nueva, y el nodo la acepta.
        world.clock.advance((10 * DAY).total_seconds())
        second = (await world.service.rotate(SigningPurpose.CATALOG, context=context)).publication
        assert second is not None and second.signed_by_key_id == rotation.new_key.key_id
        node.apply_signed_set(second.envelope)
        # Un conjunto viejo reenviado, bien firmado, no se aplica.
        with pytest.raises(KeySetConflictError):
            node.apply_signed_set(first.envelope)

        # 3. Pasado el solapamiento, la key_set anterior deja de estar en el conjunto del nodo.
        world.clock.advance((21 * DAY).total_seconds())
        await world.service.retire_expired()
        third = (await world.service.rotate(SigningPurpose.GATE, context=context)).publication
        assert third is not None and first_key_set not in third.key_ids
        node.apply_signed_set(third.envelope)
        assert first_key_set not in node

        # La continuidad exige el orden: un nodo que solo tiene el conjunto inicial no puede
        # comprobar lo que firma la key_set nueva.
        stale = pinned_node(world, initial)
        with pytest.raises(SignatureInvalidError):
            stale.apply_signed_set(third.envelope)

    asyncio.run(scenario())


def test_rotating_key_set_twice_inside_the_overlap_keeps_the_node_in_sync() -> None:
    async def scenario() -> None:
        world = await bootstrapped_world()
        node = pinned_node(world, world.store.publications[-1].record)
        context = provider_context()
        signers = []
        for _ in range(3):
            world.clock.advance((5 * DAY).total_seconds())
            publication = (
                await world.service.rotate(SigningPurpose.KEY_SET, context=context)
            ).publication
            assert publication is not None
            signers.append(publication.signed_by_key_id)
            node.apply_signed_set(publication.envelope)
        keys = world.service.all_keys()
        key_sets = [k for k in keys if k.purpose is SigningPurpose.KEY_SET]
        assert [k.status for k in key_sets].count(KeyStatus.OVERLAPPING) == 1
        assert [k.status for k in key_sets].count(KeyStatus.RETIRED) == 2
        assert len(set(signers)) == 3

    asyncio.run(scenario())


def test_rotations_at_the_same_instant_publish_strictly_increasing_sets() -> None:
    async def scenario() -> None:
        world = await bootstrapped_world()
        node = pinned_node(world, world.store.publications[-1].record)
        context = provider_context()
        issued = []
        for purpose in (SigningPurpose.CATALOG, SigningPurpose.GATE, SigningPurpose.KEY_SET):
            publication = (await world.service.rotate(purpose, context=context)).publication
            assert publication is not None
            issued.append(publication.issued_at)
            node.apply_signed_set(publication.envelope)
        assert issued == sorted(issued) and len(set(issued)) == 3

    asyncio.run(scenario())


def test_with_the_key_set_expired_other_node_purposes_do_not_rotate() -> None:
    async def scenario() -> None:
        world = await bootstrapped_world()
        context = provider_context()
        world.clock.advance((366 * DAY).total_seconds())
        before = dict(world.store.keys)
        secrets_before = len(world.secrets.values)
        for purpose in (
            SigningPurpose.CATALOG,
            SigningPurpose.GATE,
            SigningPurpose.LIVE_VIEW_TOKEN,
        ):
            with pytest.raises(SigningKeyUnavailable):
                await world.service.rotate(purpose, context=context)
        assert world.store.keys == before
        assert len(world.secrets.values) == secrets_before
        # checkpoint no depende de la key_set, y la key_set se rota firmándose a sí misma.
        await world.service.rotate(SigningPurpose.CHECKPOINT, context=context)
        rotation = await world.service.rotate(SigningPurpose.KEY_SET, context=context)
        assert rotation.publication is not None
        assert rotation.publication.signed_by_key_id == rotation.new_key.key_id
        await world.service.rotate(SigningPurpose.CATALOG, context=context)

    asyncio.run(scenario())
