"""SUP-U05-03: la ruta de la clasificación (TASK-308; LC-LAZ-07; BR-LAZ-18 a 28).

Sobre PostgreSQL 16 real como ``vigia_app`` y la aplicación real (``classification_stack``):

- **Tres salidas** (BR-LAZ-18, 22): confirmar abre el lazo (T03); operación autorizada y falso
  positivo lo descartan (T04). Cada una escribe un registro, su ``Label`` (DE §5.3), su evento y
  la fila del historial, y la proyección queda en el estado de la transición, todo en la misma
  transacción.
- **Motivo** (BR-LAZ-19): sin categoría o sin texto, ``loop_reason_required``; de 19 caracteres,
  ``loop_reason_too_short``; de 20 y de 1000, aceptado; de 1001, una categoría de otra salida o
  una salida desconocida, ``invalid_request`` sin ``detail_code``. Un texto con vocabulario
  bloqueado o con atribución a persona, el código del validador. **Nada escrito** en ningún
  rechazo: ni registro, ni etiqueta, ni evento, ni historial, ni proyección.
- **Reclasificación** (BR-LAZ-20, 21, 23 a 25): sin motivo reforzado, ``loop_reason_required``;
  T15 suspende el lazo conservando la acción, T16 lo reanuda, T17 y T18 desde descartado, T19 y
  T20 dejan el cierre o la declaración intactos (igualdad de registros) y marcan
  ``closed_with_later_different_classification``; ``transition_not_allowed`` (409) donde §4.1 no
  tiene fila.
- **Concurrencia**: dos clasificaciones simultáneas del mismo hallazgo dan dos registros
  ordenados o un ``transition_not_allowed``; la proyección es siempre la del último registro. Y
  la prueba cruzada del orden de candados frente a la vía de eventos de un tramo tardío.
- **Alcance**: otra organización, inexistente, otra zona, un rol sin ``findings.classify`` y la
  concesión de proveedor responden ``not_found`` sin escribir.
- **Ocupación unipersonal** (BR-LAZ-06): la marca devuelta es el inicio de la ventana; la del
  registro es exacta.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any, Final

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import IngestedSubject
from tests.loop_classification_support import (
    ATTRIBUTION_MARKER,
    BLOCKED_REASON,
    REASON,
    REINFORCED_REASON,
    ClassificationStack,
    Who,
    classification_stack,
)
from vigia_platform.ledger.application.writer import Receipt
from vigia_platform.loop.application.classification import (
    ClassificationRejected,
    ClassificationResult,
    ClassificationService,
)
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.domain.time_aggregation import bucket_start
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.db import Transaction

pytestmark = pytest.mark.integration

LOCK_WAIT_SECONDS: Final = 60.0
"""Tope para ver una transacción esperando un candado (no decide el resultado)."""


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[ClassificationStack]:
    with classification_stack(postgres_endpoint, "loop_classification_routes") as built:
        yield built


@pytest.fixture(autouse=True)
def _release_instances(stack: ClassificationStack) -> Iterator[None]:
    yield
    stack.world.stack.run(stack.world.stack.release())


@pytest.fixture(scope="module")
def coordinator(stack: ClassificationStack) -> Who:
    return stack.member(stack.world.a)


def _error(response: Any) -> tuple[int, str | None, str | None]:
    body = response.json()
    return response.status_code, body.get("code"), body.get("detail_code")


def _comparable(response: Any) -> tuple[int, Any]:
    body = response.json()
    return response.status_code, {k: v for k, v in body.items() if k != "correlation_id"}


# --- tres salidas ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "category", "state", "event", "effect"),
    [
        ("confirmed", "real_coexistence", "open", "finding_confirmed", "loop_opened"),
        ("authorized_operation", "scheduled_maintenance", "discarded", "finding_discarded", "none"),
        ("false_positive", "object_mistaken_for_person", "discarded", "finding_discarded", "none"),
    ],
)
def test_each_outcome_writes_record_label_event_history_and_projection(
    stack: ClassificationStack,
    coordinator: Who,
    outcome: str,
    category: str,
    state: str,
    event: str,
    effect: str,
) -> None:
    finding = stack.received()
    response = stack.classify(coordinator, finding, outcome, category)
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["outcome"], body["reason_category"], body["lifecycle_state"]) == (
        outcome,
        category,
        state,
    )
    assert (body["sequence_in_subject"], body["classification_count"]) == (1, 1)
    assert (body["is_reclassification"], body["reinforced_reason"]) == (False, False)
    assert body["effect_on_loop"] == effect
    (record,) = stack.classifications(finding)
    record_id = uuid.UUID(body["record_id"])
    assert record["record_id"] == record_id
    content = record["content"]
    assert content["reason_text"] == REASON
    assert content["anchor_record_id"] == str(finding.record_id)
    assert content["signer"]["role_in_use"] == "coordinator_sst"
    assert "previous_outcome" not in content
    (label,) = [x for x in stack.labels(finding.organization) if x["source_record_id"] == record_id]
    assert (label["outcome"], label["reason_category"], label["family"]) == (
        outcome,
        category,
        "coexistence",
    )
    assert stack.events(finding.organization, record_id) == [event]
    (history,) = stack.history(finding)
    assert (history["record_id"], history["label_id"]) == (record_id, label["label_id"])
    row = stack.world.finding_state(finding)
    assert row is not None
    assert (row["lifecycle_state"], row["classification_count"], row["last_record_id"]) == (
        state,
        1,
        record_id,
    )


# --- motivo y validadores ----------------------------------------------------------------------


def _untouched(stack: ClassificationStack, subject: IngestedSubject) -> tuple[Any, Any]:
    """Lo que un rechazo no puede cambiar: recuentos de la organización y la fila del sujeto."""
    return stack.counts(subject.organization), stack.world.finding_state(subject)


def _text(length: int) -> str:
    """Un motivo neutro de exactamente ``length`` caracteres, sin espacios en los extremos."""
    base = "Coexistencia observada en el pasillo de carga con la carretilla en marcha "
    text = (base * (length // len(base) + 1))[: length - 1]
    return text + "x"


_CONFIRMED: Final = {"outcome": "confirmed", "reason_category": "real_coexistence"}
_REQUIRED: Final = (400, "invalid_request", "loop_reason_required")
_TOO_SHORT: Final = (400, "invalid_request", "loop_reason_too_short")
_INVALID: Final = (400, "invalid_request", None)


def test_reason_borders_and_shape(stack: ClassificationStack, coordinator: Who) -> None:
    finding = stack.received()
    before = _untouched(stack, finding)
    fsid = stack.finding_state_id(finding)
    rejected: list[tuple[dict[str, Any], tuple[int, str, str | None]]] = [
        ({"outcome": "confirmed", "reason_text": REASON}, _REQUIRED),
        (dict(_CONFIRMED), _REQUIRED),
        ({**_CONFIRMED, "reason_text": None}, _REQUIRED),
        ({**_CONFIRMED, "reason_text": " \t\n "}, _REQUIRED),
        ({"outcome": "confirmed", "reason_category": "", "reason_text": REASON}, _REQUIRED),
        ({**_CONFIRMED, "reason_text": _text(19)}, _TOO_SHORT),
        ({**_CONFIRMED, "reason_text": "     " + _text(15) + "     "}, _TOO_SHORT),
        ({**_CONFIRMED, "reason_text": _text(1001)}, _INVALID),
        ({**_CONFIRMED, "reason_category": "reflection_or_glare", "reason_text": REASON}, _INVALID),
        ({**_CONFIRMED, "reason_category": "unknown_category", "reason_text": REASON}, _INVALID),
        ({**_CONFIRMED, "outcome": "pending", "reason_text": REASON}, _INVALID),
        ({"reason_category": "real_coexistence", "reason_text": REASON}, _INVALID),
        ({**_CONFIRMED, "reason_text": REASON, "reinforced_reason": "true"}, _INVALID),
        ({**_CONFIRMED, "reason_text": REASON, "person": "x"}, _INVALID),
        ({**_CONFIRMED, "reason_text": "Motivo con ​ invisible en la zona de carga"}, _INVALID),
        ({**_CONFIRMED, "reason_text": "<b>Motivo con marcado en la zona de carga</b>"}, _INVALID),
    ]
    for body, expected in rejected:
        response = stack.send(coordinator, fsid, body)
        assert _error(response) == expected, (body, response.text)
    assert _untouched(stack, finding) == before
    # 20 y 1000 caracteres: en el borde se aceptan.
    assert len(_text(20)) == 20 and len(_text(1000)) == 1000
    assert stack.classify(coordinator, finding, reason_text=_text(20)).status_code == 201
    other = stack.received()
    accepted = stack.classify(
        coordinator, other, "false_positive", "no_person_present", _text(1000)
    )
    assert accepted.status_code == 201, accepted.text
    (record,) = stack.classifications(other)
    assert len(record["content"]["reason_text"]) == 1000


@pytest.mark.parametrize(
    ("text", "code"),
    [
        (BLOCKED_REASON, "loop_blocked_vocabulary_term"),
        (
            f"El cruce {ATTRIBUTION_MARKER} del turno de noche en la zona",
            "loop_person_attribution_detected",
        ),
    ],
)
def test_a_rejected_text_writes_nothing_in_a_first_classification_or_a_reclassification(
    stack: ClassificationStack, coordinator: Who, text: str, code: str
) -> None:
    finding = stack.received()
    before = _untouched(stack, finding)
    response = stack.classify(coordinator, finding, reason_text=text)
    assert _error(response) == (400, "invalid_request", code), response.text
    assert _untouched(stack, finding) == before
    assert stack.classify(coordinator, finding).status_code == 201
    before = _untouched(stack, finding)
    response = stack.classify(
        coordinator, finding, "false_positive", "no_person_present", text, reinforced=True
    )
    assert _error(response) == (400, "invalid_request", code), response.text
    assert _untouched(stack, finding) == before


# --- reclasificación ---------------------------------------------------------------------------


def test_a_reclassification_requires_the_reinforced_reason(
    stack: ClassificationStack, coordinator: Who
) -> None:
    finding = stack.received()
    first = stack.classify(coordinator, finding, "false_positive", "reflection_or_glare")
    assert first.status_code == 201
    before = _untouched(stack, finding)
    response = stack.classify(coordinator, finding, "confirmed", "real_coexistence")
    assert _error(response) == _REQUIRED
    # Sin motivo reforzado y con el texto bloqueado: el motivo reforzado va antes que el validador.
    response = stack.classify(coordinator, finding, "confirmed", "real_coexistence", BLOCKED_REASON)
    assert _error(response) == _REQUIRED
    assert _untouched(stack, finding) == before
    # T17: descartado → abierto, con su secuencia, la salida anterior y el registro anterior.
    second = stack.classify(
        coordinator, finding, "confirmed", "guard_bypassed", REINFORCED_REASON, reinforced=True
    )
    assert second.status_code == 201, second.text
    body = second.json()
    assert (body["lifecycle_state"], body["effect_on_loop"]) == ("open", "loop_opened")
    assert (body["sequence_in_subject"], body["is_reclassification"]) == (2, True)
    assert body["reinforced_reason"] is True
    old, new = stack.classifications(finding)
    assert old["record_id"] == uuid.UUID(first.json()["record_id"])
    assert new["content"]["previous_outcome"] == "false_positive"
    assert new["content"]["previous_classification_record_id"] == str(old["record_id"])
    assert new["content"]["reinforced_reason"] is True
    assert stack.events(finding.organization, new["record_id"]) == ["finding_confirmed"]
    assert [row["sequence_in_subject"] for row in stack.history(finding)] == [1, 2]


def test_t18_changes_category_or_outcome_within_discard(
    stack: ClassificationStack, coordinator: Who
) -> None:
    finding = stack.received()
    first = stack.classify(coordinator, finding, "false_positive", "invalid_signal")
    assert first.status_code == 201
    response = stack.classify(
        coordinator,
        finding,
        "authorized_operation",
        "cleaning_or_setup",
        REINFORCED_REASON,
        reinforced=True,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["lifecycle_state"], body["effect_on_loop"]) == ("discarded", "none")
    record = stack.classifications(finding)[-1]
    assert stack.events(finding.organization, record["record_id"]) == ["finding_reclassified"]
    row = stack.world.finding_state(finding)
    assert row is not None
    assert (row["current_classification_outcome"], row["current_reason_category"]) == (
        "authorized_operation",
        "cleaning_or_setup",
    )


def _reclassify(
    stack: ClassificationStack,
    who: Who,
    subject: IngestedSubject,
    outcome: str = "false_positive",
    category: str = "no_person_present",
) -> Any:
    return stack.classify(who, subject, outcome, category, REINFORCED_REASON, reinforced=True)


def test_t15_suspends_keeping_the_action_and_t16_resumes_it(
    stack: ClassificationStack, coordinator: Who
) -> None:
    finding = stack.received()
    assert stack.classify(coordinator, finding).status_code == 201
    stack.assign_action(finding)
    assigned = stack.world.finding_state(finding)
    assert assigned is not None and assigned["lifecycle_state"] == "action_assigned"
    suspended = _reclassify(stack, coordinator, finding)
    assert suspended.status_code == 201, suspended.text
    assert (suspended.json()["lifecycle_state"], suspended.json()["effect_on_loop"]) == (
        "suspended_by_reclassification",
        "loop_suspended",
    )
    row = stack.world.finding_state(finding)
    assert row is not None
    assert row["previous_lifecycle_state"] == "action_assigned"
    for kept in ("current_action_id", "current_action_type", "assignee_user_id", "due_at"):
        assert row[kept] == assigned[kept], kept
    record = stack.classifications(finding)[-1]
    assert stack.events(finding.organization, record["record_id"]) == ["loop_suspended"]
    # Suspendido: otra salida de descarte no es una transición (409).
    before = _untouched(stack, finding)
    again = _reclassify(stack, coordinator, finding, "authorized_operation", "other_authorized")
    assert _error(again) == (409, "conflict", "loop_transition_not_allowed")
    assert _untouched(stack, finding) == before
    resumed = _reclassify(stack, coordinator, finding, "confirmed", "real_coexistence")
    assert resumed.status_code == 201, resumed.text
    assert (resumed.json()["lifecycle_state"], resumed.json()["effect_on_loop"]) == (
        "action_assigned",
        "loop_resumed",
    )
    record = stack.classifications(finding)[-1]
    assert stack.events(finding.organization, record["record_id"]) == ["loop_resumed"]
    final = stack.world.finding_state(finding)
    assert final is not None and final["current_action_id"] == assigned["current_action_id"]
    assert final["classification_count"] == 3


def test_t16_recalculates_the_overdue_with_the_original_deadline(
    stack: ClassificationStack, coordinator: Who
) -> None:
    finding = stack.received()
    assert stack.classify(coordinator, finding).status_code == 201
    stack.assign_action(finding, due_in=timedelta(seconds=30))
    assert _reclassify(stack, coordinator, finding).status_code == 201
    stack.tick(120)  # el plazo original pasa mientras está suspendido
    resumed = _reclassify(stack, coordinator, finding, "confirmed", "real_coexistence")
    assert resumed.status_code == 201
    assert resumed.json()["lifecycle_state"] == "overdue"


def test_transition_not_allowed_writes_nothing(
    stack: ClassificationStack, coordinator: Who
) -> None:
    finding = stack.received()
    assert stack.classify(coordinator, finding).status_code == 201
    detection = stack.in_review()
    for subject, outcome, category in (
        (finding, "confirmed", "real_coexistence"),  # abierto → confirmado no existe
        (detection, "confirmed", "real_coexistence"),  # la cola se resuelve en TASK-309
        (detection, "false_positive", "no_person_present"),
    ):
        before = _untouched(stack, subject)
        response = _reclassify(stack, coordinator, subject, outcome, category)
        assert _error(response) == (409, "conflict", "loop_transition_not_allowed"), subject
        assert _untouched(stack, subject) == before
    # La transición se comprueba antes que el motivo (BLM §1, pasos 2 y 3).
    response = stack.classify(coordinator, finding, reason_text=None)
    assert _error(response)[2] == "loop_transition_not_allowed"


def _kept_records(stack: ClassificationStack, finding: IngestedSubject) -> list[Any]:
    return [
        (r["record_id"], r["chain_sequence"], r["content_hash"], r["record_hash"], r["content"])
        for r in stack.subject_records(finding)
        if r["record_type"] in ("closure", "unclosable_state_declared", "action_assigned")
    ]


@pytest.mark.parametrize("terminal", ["closed", "unclosable"])
def test_t19_and_t20_keep_the_closure_or_declaration_intact_and_mark_it(
    stack: ClassificationStack, coordinator: Who, terminal: str
) -> None:
    finding = stack.received()
    assert stack.classify(coordinator, finding).status_code == 201
    if terminal == "closed":
        stack.close(finding, stack.assign_action(finding))
    else:
        stack.declare_unclosable(finding)
    before_records = stack.subject_records(finding)
    kept = _kept_records(stack, finding)
    assert kept
    before = stack.world.finding_state(finding)
    assert before is not None and before["lifecycle_state"] == terminal
    response = _reclassify(stack, coordinator, finding, "false_positive", "reflection_or_glare")
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["lifecycle_state"], body["effect_on_loop"]) == (terminal, "closed_marked")
    assert body["closed_with_later_different_classification"] is True
    after_records = stack.subject_records(finding)
    # Igualdad de registros: lo anterior es un prefijo idéntico; solo se añade la clasificación.
    assert after_records[: len(before_records)] == before_records
    assert [r["record_type"] for r in after_records[len(before_records) :]] == ["classification"]
    assert _kept_records(stack, finding) == kept
    after = stack.world.finding_state(finding)
    assert after is not None
    for name in ("closed_at", "closure_record_id", "unclosable_since", "unclosable_review_due_at"):
        assert after[name] == before[name], name
    assert after["closed_with_later_different_classification"] is True
    record = after_records[-1]
    assert stack.events(finding.organization, record["record_id"]) == ["finding_reclassified"]
    # Volver a confirmado tampoco toca el cierre y la marca se conserva.
    back = _reclassify(stack, coordinator, finding, "confirmed", "real_coexistence")
    assert back.status_code == 201
    assert back.json()["closed_with_later_different_classification"] is True
    assert back.json()["lifecycle_state"] == terminal
    assert _kept_records(stack, finding) == kept


# --- concurrencia ------------------------------------------------------------------------------


async def _waiting_advisory_locks(stack: ClassificationStack) -> int:
    rows = await stack.world.stack.authz.sessions.admin.fetch(
        "SELECT count(*) AS n FROM pg_locks WHERE NOT granted AND locktype = 'advisory'"
        " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
    )
    return int(rows[0]["n"])


class _Rendezvous(ClassificationService):
    """La primera clasificación espera, tras leer el estado, a que la segunda lo haya leído
    también **o** esté esperando la exclusión del sujeto: sin la exclusión, las dos leen el mismo
    estado de forma determinista; con ella, la segunda espera y la primera sigue."""

    stack: ClassificationStack
    arrivals: int = 0

    async def _current(  # type: ignore[override]
        self, transaction: Transaction, finding_state_id: uuid.UUID
    ) -> Any:
        current = await ClassificationService._current(transaction, finding_state_id)
        self.arrivals += 1
        if self.arrivals == 1:
            async with asyncio.timeout(LOCK_WAIT_SECONDS):
                while self.arrivals < 2 and await _waiting_advisory_locks(self.stack) == 0:
                    await asyncio.sleep(0.05)
        return current


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (("confirmed", "real_coexistence"), ("confirmed", "guard_bypassed")),
        (("confirmed", "real_coexistence"), ("false_positive", "no_person_present")),
        (("false_positive", "invalid_signal"), ("authorized_operation", "cleaning_or_setup")),
    ],
)
@pytest.mark.parametrize("round_", range(2))
def test_two_simultaneous_classifications_leave_a_coherent_history(
    stack: ClassificationStack,
    coordinator: Who,
    first: tuple[str, str],
    second: tuple[str, str],
    round_: int,
) -> None:
    finding = stack.received()
    fsid = stack.finding_state_id(finding)
    context = stack.context(coordinator)
    service = stack.service_like(cls=_Rendezvous)
    assert isinstance(service, _Rendezvous)
    service.stack = stack

    async def both() -> list[Any]:
        calls = [
            service.classify(
                context,
                fsid,
                outcome=outcome,
                reason_category=category,
                reason_text=REINFORCED_REASON,
                reinforced_reason=True,
            )
            for outcome, category in (first, second)
        ]
        return list(await asyncio.gather(*calls, return_exceptions=True))

    results = stack.run(both())
    for result in results:
        assert isinstance(result, ClassificationResult) or (
            isinstance(result, ClassificationRejected)
            and result.detail_code is LoopDetailCode.TRANSITION_NOT_ALLOWED
        ), repr(result)
    succeeded = [r for r in results if isinstance(r, ClassificationResult)]
    records = stack.classifications(finding)
    assert len(records) == len(succeeded) >= 1
    sequences = list(range(1, len(records) + 1))
    assert [r["content"]["sequence_in_subject"] for r in records] == sequences
    assert sorted(r.sequence_in_subject for r in succeeded) == sequences
    row = stack.world.finding_state(finding)
    assert row is not None
    last = max(succeeded, key=lambda r: r.sequence_in_subject)
    assert row["last_record_id"] == records[-1]["record_id"] == last.record_id
    assert (row["lifecycle_state"], row["classification_count"]) == (
        last.lifecycle_state.value,
        len(records),
    )
    assert row["current_classification_outcome"] == records[-1]["content"]["outcome"]
    assert len(stack.history(finding)) == len(records)


CROSS_PAIRS: Final = 6


def test_classification_and_late_segment_writes_share_one_lock_order(
    stack: ClassificationStack, coordinator: Who
) -> None:
    """Prueba cruzada: a la vez, sobre los mismos sujetos, la reclasificación (exclusión del sujeto
    → cadena → control → filas) y la escritura del tramo tardío de la vía de eventos (cadena →
    control → filas). Ninguna acaba en interbloqueo ni en ``TemporarilyUnavailable``."""
    subjects = [stack.received() for _ in range(CROSS_PAIRS)]
    for subject in subjects:
        assert stack.classify(coordinator, subject).status_code == 201
    ids = {subject.subject_id: stack.finding_state_id(subject) for subject in subjects}
    context = stack.context(coordinator)
    world = stack.world
    event_context = world.event_context(world.a)
    late_database = world.stack.database()
    writer = world.stack.primary.writer
    start = asyncio.Event()

    async def reclassify(subject: IngestedSubject) -> Any:
        await start.wait()
        return await stack.service.classify(
            context,
            ids[subject.subject_id],
            outcome="false_positive",
            reason_category="no_person_present",
            reason_text=REINFORCED_REASON,
            reinforced_reason=True,
        )

    async def late(subject: IngestedSubject) -> Any:
        await start.wait()
        async with late_database.transaction(event_context) as transaction:
            receipt, step = await world.store.write_and_apply(
                transaction,
                writer,
                event_context,
                "late_segment_attached",
                stack.late_segment_content(subject, "open"),
                scope=world.scope(world.a),
            )
            assert isinstance(receipt, Receipt), receipt
            return step

    async def race() -> list[Any]:
        tasks = [
            *(asyncio.create_task(reclassify(s)) for s in subjects),
            *(asyncio.create_task(late(s)) for s in subjects),
        ]
        await asyncio.sleep(0)
        start.set()
        return list(await asyncio.gather(*tasks, return_exceptions=True))

    try:
        results = stack.run(race())
    finally:
        stack.run(late_database.dispose())
    failures = [r for r in results if isinstance(r, BaseException)]
    assert failures == []
    for subject in subjects:
        row = world.finding_state(subject)
        assert row is not None
        assert (row["lifecycle_state"], row["classification_count"]) == (
            "suspended_by_reclassification",
            2,
        )


# --- alcance -----------------------------------------------------------------------------------


def test_out_of_scope_answers_like_missing_and_writes_nothing(
    stack: ClassificationStack, coordinator: Who
) -> None:
    world = stack.world
    foreign = stack.received(world.b)
    foreign_before = _untouched(stack, foreign)
    body = {**_CONFIRMED, "reason_text": REASON}
    known = stack.classify(coordinator, foreign)
    missing = stack.send(coordinator, uuid.uuid4(), body)
    assert _error(known)[:2] == (404, "not_found")
    assert _comparable(known) == _comparable(missing)
    assert _untouched(stack, foreign) == foreign_before
    finding = stack.received()
    before = _untouched(stack, finding)
    site = world.a.site
    for who in (
        stack.member(world.a, Role.COORDINATOR_SST, ScopeLevel.ZONE, site.other_zone_id),
        stack.member(world.a, Role.PLANT_MANAGER),
        stack.member(world.a, Role.ADMINISTRATOR),
        stack.member(world.a, Role.LINE_MANAGER),
        stack.installer(world.a),
    ):
        response = stack.classify(who, finding)
        assert _error(response)[:2] == (404, "not_found"), response.text
        assert _comparable(response) == _comparable(stack.send(who, uuid.uuid4(), body))
    assert _untouched(stack, finding) == before
    zone_coordinator = stack.member(world.a, Role.COORDINATOR_SST, ScopeLevel.ZONE, site.zone_id)
    assert stack.classify(zone_coordinator, finding).status_code == 201
    plant_coordinator = stack.member(
        world.a, Role.COORDINATOR_SST, ScopeLevel.PLANT, world.a.plant_id
    )
    second = _reclassify(stack, plant_coordinator, finding)
    assert second.status_code == 201, second.text


# --- ocupación unipersonal ---------------------------------------------------------------------


def _set_occupancy(
    stack: ClassificationStack, subject: IngestedSubject, *, single: bool, window: int
) -> None:
    stack.world.stack.execute(
        "UPDATE loop.finding_state SET single_occupancy_zone = $1, aggregation_window_minutes = $2"
        " WHERE organization_id = $3 AND subject_id = $4",
        single,
        window,
        subject.organization.organization_id,
        subject.subject_id,
    )


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.mark.parametrize(("single", "window"), [(True, 60), (True, 15), (True, 480), (False, 60)])
def test_the_returned_instant_is_aggregated_and_the_record_keeps_it_exact(
    stack: ClassificationStack, coordinator: Who, single: bool, window: int
) -> None:
    finding = stack.received()
    _set_occupancy(stack, finding, single=single, window=window)
    stack.tick(7 * 60 + 13.5)  # lejos del inicio de una ventana de 15 minutos o más
    response = stack.classify(coordinator, finding)
    assert response.status_code == 201, response.text
    body = response.json()
    (record,) = stack.classifications(finding)
    exact = _instant(record["content"]["classified_at"])
    assert body["time_aggregated"] is single
    expected = bucket_start(exact, window) if single else exact
    assert _instant(body["classified_at"]) == expected
    (history,) = stack.history(finding)
    assert history["classified_at"] == exact
