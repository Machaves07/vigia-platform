"""PR-GOB-20: concesiones de subida de clip como máquina de estados (TASK-222; BR-GOB-93, 94; G-13).

``domain-entities.md`` §6 (C-PLA-14, «Con estado», generador ``upload_grant_commands``): «Toda
concesión vencida o usada rechaza un segundo ``PUT``; un clip sin hallazgo en 24 h pasa a
``orphan`` y cuenta una vez», y la nota de BR-GOB-94: un clip ``verification`` nunca pasa a
``orphan``.

Corren los **servicios reales** (``ClipGrantService``, ``ClipConfirmationService`` y
``OrphanClipSweeper`` con ``ClipObjectStore``) sobre un repositorio en memoria con la semántica de
``PostgresClipGrants`` (escrituras condicionales) y un **doble del almacén** que hace cumplir lo que
cumple S3 con una URL prefirmada: vencimiento, cabeceras firmadas **exactas**, ``BadDigest`` con
otra suma, escritura condicional (``If-None-Match: *`` si la URL la firmara) y versiones (nada se
sobrescribe). Comandos (``upload_grant_commands``): pedir concesión (nueva, repetida, con otros
parámetros), ``PUT`` (bytes buenos o malos; cabeceras exactas, sin la suma o con una de más), que el
tiempo avance (bordes 15 min y 24 h), que un registro cite el clip (TASK-221), confirmar y barrer.

Propiedades tras cada paso:

- una URL vencida rechaza todo ``PUT``; una concesión **usada** (objeto subido, citado, confirmado
  o huérfano) nunca recibe una URL nueva: después de su vencimiento ningún ``PUT`` llega a su clave;
- cada versión de cada objeto tiene la suma concedida: un segundo ``PUT`` nunca cambia el
  contenido (el rechazo de un segundo ``PUT`` de los **mismos** bytes dentro de la vigencia exige
  ``If-None-Match``, que el contrato fijado no deja enviar: declarado en el PR);
- un clip subido sin registro que lo cite pasa a ``orphan`` en el primer barrido desde las 24 h y
  cuenta **una** vez en ``clip_grants_orphaned_total`` de su nodo (sin etiqueta de zona);
- un clip ``verification`` nunca pasa a ``orphan`` ni a ``expired``.

Perfil ``ci`` con la semilla fija del proyecto y la de la sesión (``tests/conftest.py``).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from hypothesis import seed as hypothesis_seed
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule, run_state_machine_as_test
from vigia_contracts.models import api
from vigia_contracts.models.enumerations import ClipUploadPurpose

from tests.conftest import _seeds_for_profile
from tests.dispatch_support import metric_points, metrics_with_reader
from tests.writer_support import unit_context
from vigia_platform.fleet.adapters.postgres.clip_grant_store import NodeClipCounts
from vigia_platform.fleet.adapters.s3.clip_storage import ClipObjectStore
from vigia_platform.fleet.application.clip_confirmation import (
    ClipCheckFailed,
    ClipConfirmationService,
    ClipNotOfNode,
    EvidenceClipNotConfirmable,
)
from vigia_platform.fleet.application.clip_grants import (
    ClipGrantConflict,
    ClipGrantService,
    IssuedClipGrant,
)
from vigia_platform.fleet.application.orphan_clips import OrphanClipSweeper
from vigia_platform.fleet.domain.clip_upload_grant import (
    CLIP_GRANT_TTL,
    ORPHAN_AFTER,
    ClipUploadGrant,
)
from vigia_platform.fleet.domain.enums import UploadGrantStatus
from vigia_platform.fleet.domain.verification_clip import ClipCheckFailure, VerificationClip
from vigia_platform.identity.authz.context import NodeScope
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import ActorKind, ActorUnit, ScopeContext
from vigia_platform.shared.observability.metrics import MetricName
from vigia_platform.shared.storage import ObjectHead, PresignedRequest

START = datetime(2026, 10, 4, 8, 0, 0, tzinfo=UTC)
ORG = uuid.UUID("0192f0c4-aaaa-7000-8000-000000000001")
PLANT = uuid.UUID("0192f0c4-bbbb-7000-8000-000000000002")
NODES = (
    uuid.UUID("0192f0c4-cccc-7000-8000-000000000003"),
    uuid.UUID("0192f0c4-cccc-7000-8000-000000000004"),
)
ZONES = (
    uuid.UUID("0192f0c4-dddd-7000-8000-000000000005"),
    uuid.UUID("0192f0c4-dddd-7000-8000-000000000006"),
)
CLIPS = tuple(uuid.UUID(f"0192f0c4-eeee-7000-8000-00000000001{index}") for index in range(4))
CONTENTS = (b"clip difuminado A" * 32, b"clip difuminado B" * 48)
WRONG = b"otros bytes que no son los concedidos"
CHECKSUM = "x-amz-checksum-sha256"


def _b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


# --- Doble del almacén ----------------------------------------------------------------------------


@dataclass
class Signed:
    key: str
    headers: dict[str, str]
    expires_at: datetime


@dataclass
class Version:
    data: bytes
    content_type: str
    metadata: dict[str, str]


class ConditionalStore:
    """S3 con URL prefirmadas: vencimiento, cabeceras firmadas exactas, suma, ``If-None-Match`` y
    versiones. ``get_object`` (o cualquier otra operación) hace fallar la prueba."""

    def __init__(self, clock: SimulatedClock) -> None:
        self.clock = clock
        self.urls: dict[str, Signed] = {}
        self.objects: dict[str, list[Version]] = {}
        self.late_puts: list[str] = []
        """URL que aceptaron un ``PUT`` en su vencimiento o después (debe quedar vacía)."""

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        async def forbidden(*_: Any, **__: Any) -> Any:
            raise AssertionError(f"operación prohibida en clips: {name}")

        return forbidden

    async def head_object(self, key: str) -> ObjectHead | None:
        versions = self.objects.get(key)
        if not versions:
            return None
        last = versions[-1]
        return ObjectHead(
            key=key,
            size_bytes=len(last.data),
            checksum_sha256=_b64(last.data),
            checksum_type=None,
            content_type=last.content_type,
            metadata=dict(last.metadata),
            version_id=str(len(versions)),
        )

    async def presign_put(
        self,
        key: str,
        content_type: str,
        checksum_sha256: str,
        required_headers: Mapping[str, str],
        ttl: timedelta = CLIP_GRANT_TTL,
    ) -> PresignedRequest:
        assert timedelta(seconds=1) <= ttl <= CLIP_GRANT_TTL
        headers = {
            "content-type": content_type,
            CHECKSUM: base64.b64encode(bytes.fromhex(checksum_sha256)).decode(),
            **{name.lower(): value for name, value in required_headers.items()},
        }
        expires_at = self.clock.now() + ttl
        url = f"https://store.vigia.test/{key}?X-Amz-Signature={len(self.urls)}"
        self.urls[url] = Signed(key, headers, expires_at)
        return PresignedRequest("PUT", url, dict(headers), expires_at)

    def put(self, url: str, data: bytes, headers: Mapping[str, str]) -> int:
        signed = self.urls[url]
        sent = {name.lower(): value for name, value in headers.items()}
        if self.clock.now() >= signed.expires_at:
            return 403  # Request has expired
        if sent != signed.headers:
            return 403  # SignatureDoesNotMatch: cabeceras firmadas exactas
        if sent.get("if-none-match") == "*" and self.objects.get(signed.key):
            return 412  # PreconditionFailed
        if _b64(data) != sent[CHECKSUM]:
            return 400  # BadDigest
        metadata = {
            name.removeprefix("x-amz-meta-"): value
            for name, value in sent.items()
            if name.startswith("x-amz-meta-")
        }
        self.objects.setdefault(signed.key, []).append(
            Version(data, sent["content-type"], metadata)
        )
        return 200


# --- Repositorio en memoria con la semántica de PostgresClipGrants -------------------------------


class FakeDatabase:
    @contextlib.asynccontextmanager
    async def transaction(self, context: ScopeContext) -> AsyncIterator[Any]:
        yield SimpleNamespace(context=context)

    async def read(self, *_: Any, **__: Any) -> Any:  # pragma: no cover - no se usa
        raise AssertionError("el repositorio en memoria no lee por la base")


class MemoryGrants:
    """Las escrituras condicionales de ``clip_grant_store``, en memoria."""

    def __init__(self) -> None:
        self.grants: dict[uuid.UUID, ClipUploadGrant] = {}
        self.clips: dict[uuid.UUID, VerificationClip] = {}

    async def grant(self, context: ScopeContext, clip_id: uuid.UUID) -> ClipUploadGrant | None:
        found = self.grants.get(clip_id)
        return found if found and found.organization_id == context.organization_id else None

    async def insert(self, transaction: Any, grant: ClipUploadGrant) -> bool:
        if grant.clip_id in self.grants:
            return False
        self.grants[grant.clip_id] = grant
        return True

    async def reissue(
        self, transaction: Any, grant: ClipUploadGrant, previous_issued_at: datetime
    ) -> bool:
        current = self.grants.get(grant.clip_id)
        if (
            current is None
            or current.status is not UploadGrantStatus.ISSUED
            or current.issued_at != previous_issued_at
            or grant.issued_at < current.expires_at  # reissue_guard de gob_0022
        ):
            return False
        self.grants[grant.clip_id] = replace(
            current, issued_at=grant.issued_at, expires_at=grant.expires_at
        )
        return True

    async def read_verification_clip(
        self, context: ScopeContext, clip_id: uuid.UUID
    ) -> VerificationClip | None:
        return self.clips.get(clip_id)

    async def lock_for_confirmation(
        self, transaction: Any, node_id: uuid.UUID, clip_id: uuid.UUID
    ) -> ClipUploadGrant | None:
        found = self.grants.get(clip_id)
        return found if found and found.node_id == node_id else None

    async def verification_clip(
        self, transaction: Any, clip_id: uuid.UUID
    ) -> VerificationClip | None:
        return self.clips.get(clip_id)

    async def insert_verification_clip(self, transaction: Any, clip: VerificationClip) -> None:
        assert clip.clip_id not in self.clips
        self.clips[clip.clip_id] = clip

    async def mark_used(
        self, transaction: Any, node_id: uuid.UUID, clip_id: uuid.UUID, now: datetime
    ) -> bool:
        found = self.grants.get(clip_id)
        if found is None or found.node_id != node_id:
            return False
        if found.status is not UploadGrantStatus.ISSUED:
            return False
        self.grants[clip_id] = replace(found, status=UploadGrantStatus.USED, used_at=now)
        return True

    def cite(self, clip_id: uuid.UUID, now: datetime) -> bool:
        """Lo que hará la ingesta (TASK-221) con un clip citado por un registro aceptado."""
        found = self.grants[clip_id]
        if found.status is not UploadGrantStatus.ISSUED:
            return False
        self.grants[clip_id] = replace(found, status=UploadGrantStatus.USED, used_at=now)
        return True

    async def orphan_candidates(
        self, transaction: Any, *, issued_before: datetime, limit: int
    ) -> tuple[ClipUploadGrant, ...]:
        found = sorted(
            (
                grant
                for grant in self.grants.values()
                if grant.status is UploadGrantStatus.ISSUED
                and grant.purpose is ClipUploadPurpose.EVIDENCE
                and grant.issued_at <= issued_before
            ),
            key=lambda grant: (grant.issued_at, grant.clip_id),
        )
        return tuple(found[:limit])

    async def mark_orphans(
        self, transaction: Any, clip_ids: Sequence[uuid.UUID], now: datetime
    ) -> dict[uuid.UUID, uuid.UUID]:
        changed: dict[uuid.UUID, uuid.UUID] = {}
        for clip_id in clip_ids:
            grant = self.grants[clip_id]
            if grant.status is UploadGrantStatus.ISSUED and (
                grant.purpose is ClipUploadPurpose.EVIDENCE
            ):
                self.grants[clip_id] = replace(
                    grant, status=UploadGrantStatus.ORPHAN, used_at=now, orphaned_at=now
                )
                changed[clip_id] = grant.node_id
        return changed

    async def mark_expired(
        self, transaction: Any, clip_ids: Sequence[uuid.UUID], now: datetime
    ) -> dict[uuid.UUID, uuid.UUID]:
        changed: dict[uuid.UUID, uuid.UUID] = {}
        for clip_id in clip_ids:
            grant = self.grants[clip_id]
            if (
                grant.status is UploadGrantStatus.ISSUED
                and grant.purpose is ClipUploadPurpose.EVIDENCE
                and grant.expires_at <= now
            ):
                self.grants[clip_id] = replace(grant, status=UploadGrantStatus.EXPIRED)
                changed[clip_id] = grant.node_id
        return changed

    async def node_clip_counts(
        self, transaction: Any, *, since: datetime, until: datetime
    ) -> tuple[NodeClipCounts, ...]:  # pragma: no cover - lo prueban las de integración
        raise AssertionError("no se usa en la máquina")


# --- Comandos ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class GrantCommand:
    slot: int
    node: int
    purpose: ClipUploadPurpose
    content: int


upload_grant_commands = st.builds(
    GrantCommand,
    slot=st.integers(0, len(CLIPS) - 1),
    node=st.integers(0, len(NODES) - 1),
    purpose=st.sampled_from(list(ClipUploadPurpose)),
    content=st.integers(0, len(CONTENTS) - 1),
)
"""``upload_grant_commands``: qué clip pide qué nodo, con qué propósito y qué contenido."""

steps = st.one_of(
    st.sampled_from([1, 899, 900, 901, 3600, 86_399, 86_400, 86_401]),
    st.integers(1, 2 * 86_400),
)
"""Avances del reloj: los bordes de los 15 minutos y de las 24 h, y valores al azar."""


@dataclass
class Model:
    """Lo que el modelo espera de cada clip pedido."""

    command: GrantCommand
    issued_at: datetime
    expires_at: datetime
    status: UploadGrantStatus = UploadGrantStatus.ISSUED
    cited: bool = False
    confirmed: bool = False


@dataclass
class Issued:
    clip_id: uuid.UUID
    url: str
    headers: dict[str, str]
    data: bytes


class UploadGrantMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.clock = SimulatedClock(START)
        self.store = ConditionalStore(self.clock)
        self.repository = MemoryGrants()
        self.metrics, self.reader = metrics_with_reader()
        objects = ClipObjectStore(self.store)
        database = FakeDatabase()
        self.grants = ClipGrantService(
            database=database,  # type: ignore[arg-type]
            store=objects,
            clock=self.clock,
            metrics=self.metrics,
            grants=self.repository,
        )
        self.confirmations = ClipConfirmationService(
            database=database,  # type: ignore[arg-type]
            store=objects,
            clock=self.clock,
            metrics=self.metrics,
            grants=self.repository,
        )
        self.sweeper = OrphanClipSweeper(
            database=database,  # type: ignore[arg-type]
            store=objects,
            clock=self.clock,
            metrics=self.metrics,
            grants=self.repository,
        )
        self.context = unit_context(ORG, ActorUnit.U03, kind=ActorKind.NODE, role=None)
        self.model: dict[uuid.UUID, Model] = {}
        self.issued: list[Issued] = []

    def _run(self, awaitable: Any) -> Any:
        return self.loop.run_until_complete(awaitable)

    def _node(self, index: int) -> NodeScope:
        return NodeScope(
            context=self.context,
            node_id=NODES[index],
            plant_id=PLANT,
            zone_ids=frozenset({ZONES[index]}),
            certificate_serial="0abc",
            credential_status="active",
        )

    def _uploaded(self, clip_id: uuid.UUID) -> bool:
        key = self.repository.grants[clip_id].storage_key
        return bool(self.store.objects.get(key))

    # --- Reglas ---------------------------------------------------------------------------------

    @rule(command=upload_grant_commands)
    def request_grant(self, command: GrantCommand) -> None:
        clip_id = CLIPS[command.slot]
        data = CONTENTS[command.content]
        body = {
            "clip_id": str(clip_id),
            "camera_id": f"0192f0c4-ffff-4000-8000-00000000000{command.slot}",
            "zone_id": str(ZONES[command.node]),
            "media_kind": "video",
            "content_type": "video/mp4",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
            "duration_ms": 10_000,
            "purpose": command.purpose.value,
        }
        request = api.parse_clip_upload_request(json.dumps(body).encode())
        now = self.clock.now()
        known = self.model.get(clip_id)
        try:
            issued: IssuedClipGrant | None = self._run(
                self.grants.issue(self._node(command.node), request)
            )
        except ClipGrantConflict:
            issued = None
        if known is None:
            assert issued is not None
            self.model[clip_id] = Model(command, issued.grant.issued_at, issued.grant.expires_at)
        elif (
            known.command != command
            or self._uploaded(clip_id)
            or known.status is not UploadGrantStatus.ISSUED
        ):
            # Usada (subida, citada, confirmada o huérfana), cerrada u otra petición: sin URL.
            assert issued is None, (known, command)
            return
        elif now < known.expires_at:
            assert issued is not None and issued.grant.issued_at == known.issued_at
        else:
            assert issued is not None and issued.grant.issued_at >= known.expires_at
            known.issued_at, known.expires_at = issued.grant.issued_at, issued.grant.expires_at
        assert issued is not None
        assert issued.grant.expires_at - issued.grant.issued_at == CLIP_GRANT_TTL
        assert issued.upload.expires_at <= issued.grant.expires_at
        self.issued.append(Issued(clip_id, issued.upload.url, dict(issued.upload.headers), data))

    @rule(
        pick=st.integers(0, 1_000),
        good_bytes=st.booleans(),
        headers=st.sampled_from(["exact", "no_checksum", "extra"]),
    )
    def put(self, pick: int, good_bytes: bool, headers: str) -> None:
        if not self.issued:
            return
        target = self.issued[pick % len(self.issued)]
        sent = dict(target.headers)
        if headers == "no_checksum":
            del sent[CHECKSUM]
        elif headers == "extra":
            sent["x-amz-meta-otra"] = "1"
        signed = self.store.urls[target.url]
        expired = self.clock.now() >= signed.expires_at
        status = self.store.put(target.url, target.data if good_bytes else WRONG, sent)
        if expired or headers != "exact" or not good_bytes:
            assert status != 200
        else:
            assert status == 200
        if status == 200 and expired:
            self.store.late_puts.append(target.url)

    @rule(seconds=steps)
    def advance(self, seconds: int) -> None:
        self.clock.advance(seconds)

    @rule(slot=st.integers(0, len(CLIPS) - 1))
    def cite(self, slot: int) -> None:
        clip_id = CLIPS[slot]
        known = self.model.get(clip_id)
        if (
            known is None
            or known.command.purpose is not ClipUploadPurpose.EVIDENCE
            or not self._uploaded(clip_id)
        ):
            return
        if self.repository.cite(clip_id, self.clock.now()):
            known.status, known.cited = UploadGrantStatus.USED, True

    @rule(slot=st.integers(0, len(CLIPS) - 1), node=st.integers(0, len(NODES) - 1))
    def confirm(self, slot: int, node: int) -> None:
        clip_id = CLIPS[slot]
        known = self.model.get(clip_id)
        try:
            clip: VerificationClip | None = self._run(
                self.confirmations.confirm(self._node(node), clip_id)
            )
            failure: object = None
        except (ClipNotOfNode, EvidenceClipNotConfirmable, ClipCheckFailed) as error:
            clip, failure = None, error
        if known is None or known.command.node != node:
            assert isinstance(failure, ClipNotOfNode)
            return
        if known.command.purpose is ClipUploadPurpose.EVIDENCE:
            assert isinstance(failure, EvidenceClipNotConfirmable)
            return
        if not self._uploaded(clip_id):
            assert isinstance(failure, ClipCheckFailed)
            assert failure.failure is ClipCheckFailure.CLIP_MISSING
            return
        assert clip is not None
        assert clip.sha256 == hashlib.sha256(CONTENTS[known.command.content]).hexdigest()
        if known.confirmed:
            assert clip == self.repository.clips[clip_id]
        known.confirmed, known.status = True, UploadGrantStatus.USED

    @rule()
    def sweep(self) -> None:
        now = self.clock.now()
        expected_orphans: set[uuid.UUID] = set()
        expected_expired: set[uuid.UUID] = set()
        for clip_id, known in self.model.items():
            if (
                known.command.purpose is not ClipUploadPurpose.EVIDENCE
                or known.status is not UploadGrantStatus.ISSUED
                or now < known.issued_at + ORPHAN_AFTER
            ):
                continue
            if self._uploaded(clip_id):
                expected_orphans.add(clip_id)
            elif now >= known.expires_at:
                expected_expired.add(clip_id)
        report = self._run(self.sweeper.sweep(SimpleNamespace(context=self.context)))
        assert set(report.orphaned) == expected_orphans
        assert set(report.expired) == expected_expired
        for clip_id in expected_orphans:
            self.model[clip_id].status = UploadGrantStatus.ORPHAN
        for clip_id in expected_expired:
            self.model[clip_id].status = UploadGrantStatus.EXPIRED

    # --- Propiedades ---------------------------------------------------------------------------

    @invariant()
    def no_put_lands_after_its_url_expires(self) -> None:
        assert self.store.late_puts == []

    @invariant()
    def every_version_holds_the_granted_bytes(self) -> None:
        for clip_id, grant in self.repository.grants.items():
            granted = hashlib.sha256(CONTENTS[self.model[clip_id].command.content]).hexdigest()
            for version in self.store.objects.get(grant.storage_key, []):
                assert hashlib.sha256(version.data).hexdigest() == granted

    @invariant()
    def a_verification_clip_never_becomes_orphan_or_expired(self) -> None:
        for grant in self.repository.grants.values():
            if grant.purpose is ClipUploadPurpose.VERIFICATION:
                assert grant.status in (UploadGrantStatus.ISSUED, UploadGrantStatus.USED)

    @invariant()
    def each_orphan_counts_once_in_its_node_without_zone(self) -> None:
        expected = Counter(
            str(grant.node_id)
            for grant in self.repository.grants.values()
            if grant.status is UploadGrantStatus.ORPHAN
        )
        counted: Counter[str] = Counter()
        for attributes, value in metric_points(self.reader, MetricName.CLIP_GRANTS_ORPHANED_TOTAL):
            assert set(attributes) == {"node_id"}
            counted[str(attributes["node_id"])] += int(value)
        assert counted == expected

    @invariant()
    def the_model_and_the_repository_agree(self) -> None:
        for clip_id, known in self.model.items():
            grant = self.repository.grants[clip_id]
            assert grant.status is known.status
            assert (grant.issued_at, grant.expires_at) == (known.issued_at, known.expires_at)

    def teardown(self) -> None:
        self.loop.close()


def test_pr_gob_20_upload_grants_match_the_model() -> None:
    for value in _seeds_for_profile():
        seeded = hypothesis_seed(value)(UploadGrantMachine)
        run_state_machine_as_test(seeded, settings=settings(stateful_step_count=60))
