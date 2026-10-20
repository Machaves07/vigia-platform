"""La auditoría muestral y los compromisos sobre PostgreSQL 16 real (TASK-347; SUP-U05-26 a 29;
BR-LAZ-106, 107 y 109; H-39; L-1).

Sobre ``tests/monthly_review_support.py``: hallazgos confirmados por el ``ClassificationService``
real, un paquete sellado con su muestra sembrado sobre esas clasificaciones, el escritor con los
validadores de producción y la aplicación real con ``LoopHttp`` y ``AccreditationHttp``.

- **Resultado de la muestra** (SUP-U05-26): ``201`` con un registro ``sample_audit_result`` que
  cita la clasificación auditada; los registros ``classification``, la proyección del hallazgo,
  el historial y las etiquetas **no cambian**; un segundo resultado es ``409
  sample_already_reviewed`` sin registro; un cuerpo que intenta alterar o revertir la
  clasificación es ``400 sample_cannot_modify_classification``; el último elemento completa la
  muestra (``review_completed_at``).
- **Compromisos** (SUP-U05-27 a 29): alta, actualización (registro nuevo, versión 2) y lista,
  con ``commitment_out_of_scope`` solo para lo que el actor ve, y ``not_found`` para el resto.
- **Concurrencia**: dos resultados simultáneos del mismo elemento (uno solo gana), los dos
  últimos elementos a la vez (la muestra se completa una vez), dos actualizaciones simultáneas de
  un compromiso (la fila acaba como el último registro) y una auditoría frente a un alta de
  compromisos en la misma planta (sin interbloqueo ni ``TemporarilyUnavailable``).

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any, Final

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.loop_classification_support import BLOCKED_REASON
from tests.monthly_review_support import (
    COMMITMENT_TEXT,
    MonthlyReviewStack,
    SampleSeed,
    monthly_review_stack,
)
from vigia_platform.accreditation.detail_codes import AccreditationDetailCode
from vigia_platform.accreditation.monthly.audit import (
    SampleAuditRejected,
    SampleAuditResult,
    SampleAuditService,
)
from vigia_platform.accreditation.monthly.commitments import (
    CommitmentDraft,
    CommitmentService,
    CommitmentsRecordedResult,
    CommitmentUpdateResult,
)
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.db import Transaction

pytestmark = pytest.mark.integration

LOCK_WAIT_SECONDS: Final = 60.0
"""Tope de las esperas de candado de las pruebas (retro 15: nunca por debajo de 10 s)."""
COMMENT: Final = "El criterio aplicado coincide con el procedimiento de la planta"
NOTE: Final = "Señalización renovada y verificada en la ronda semanal"


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[MonthlyReviewStack]:
    with monthly_review_stack(postgres_endpoint, "monthly_review_routes") as built:
        yield built


@pytest.fixture(autouse=True)
def _release(stack: MonthlyReviewStack) -> Iterator[None]:
    yield
    stack.run(stack.actions.world.stack.release())


def _seed(stack: MonthlyReviewStack, count: int = 1, **changes: Any) -> SampleSeed:
    organization = stack.actions.world.a
    classified = [stack.classified(organization) for _ in range(count)]
    return stack.seed_sample(organization, classified, **changes)


def _coordinator(stack: MonthlyReviewStack, seed: SampleSeed) -> Any:
    return stack.member(seed.organization, Role.COORDINATOR_SST, ScopeLevel.ZONE, seed.zone_id)


def _untouched(stack: MonthlyReviewStack, seed: SampleSeed) -> tuple[Any, ...]:
    organization_id = seed.organization.organization_id
    return stack.classification_records(organization_id), stack.loop_rows(organization_id)


async def _waiting_locks(stack: MonthlyReviewStack) -> int:
    rows = await stack.actions.world.stack.authz.sessions.admin.fetch(
        "SELECT count(*) AS n FROM pg_locks WHERE NOT granted"
        " AND (database IS NULL"
        " OR database = (SELECT oid FROM pg_database WHERE datname = current_database()))"
    )
    return int(rows[0]["n"])


# --- SUP-U05-26: el resultado, que no toca ninguna clasificación -------------------------------


def test_the_coordinator_records_a_result_and_no_classification_changes(
    stack: MonthlyReviewStack,
) -> None:
    seed = _seed(stack, count=2)
    coordinator = _coordinator(stack, seed)
    organization_id = seed.organization.organization_id
    before = _untouched(stack, seed)
    classification_record = {
        record["content"]["classification_id"]: record["record_id"]
        for record in stack.records_of(organization_id, "classification")
    }

    response = stack.sample_result(
        coordinator, seed.item_ids[0], {"outcome": "consistent_criterion", "comment": COMMENT}
    )
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["sample_item_id"] == str(seed.item_ids[0])
    assert body["package_id"] == str(seed.package_id)
    assert body["classification_id"] == str(seed.classification_ids[0])
    assert body["outcome"] == "consistent_criterion"
    assert body["comment"] == COMMENT
    assert body["review_completed"] is False

    (record,) = stack.results_of(seed)
    assert str(record["record_id"]) == body["record_id"]
    content = record["content"]
    assert content["sample_item_id"] == str(seed.item_ids[0])
    assert content["classification_record_id"] == str(
        classification_record[str(seed.classification_ids[0])]
    )
    assert content["reviewed_by"]["role_in_use"] == "coordinator_sst"
    assert stack.events(organization_id, body["record_id"]) == []  # §5.1: ningún evento

    # Lo que la auditoría no puede tocar, idéntico registro a registro y hash a hash (H-39).
    assert _untouched(stack, seed) == before

    item = next(row for row in stack.item_rows(seed.sample_id) if row["outcome"] is not None)
    assert item["sample_item_id"] == seed.item_ids[0]
    assert item["comment"] == COMMENT
    assert str(item["record_id"]) == body["record_id"]
    assert stack.sample_row(seed.sample_id)["review_completed_at"] is None

    listed = stack.send(
        coordinator, "GET", f"/accreditation/monthly-packages/{seed.package_id}/sample"
    )
    assert listed.status_code == 200, listed.text
    reviewed = {item["sample_item_id"]: item for item in listed.json()}
    assert reviewed[str(seed.item_ids[0])]["outcome"] == "consistent_criterion"
    assert reviewed[str(seed.item_ids[0])]["classification_outcome"] == "confirmed"
    assert reviewed[str(seed.item_ids[1])]["outcome"] is None

    # El último elemento completa la muestra.
    last = stack.sample_result(coordinator, seed.item_ids[1], {"outcome": "criterion_error"})
    assert last.status_code == 201, last.text
    assert last.json()["review_completed"] is True
    assert last.json()["comment"] is None
    assert stack.sample_row(seed.sample_id)["review_completed_at"] is not None
    assert _untouched(stack, seed) == before


def test_a_second_result_is_409_and_leaves_no_record(stack: MonthlyReviewStack) -> None:
    seed = _seed(stack)
    coordinator = _coordinator(stack, seed)
    first = stack.sample_result(coordinator, seed.item_ids[0], {"outcome": "doubtful_criterion"})
    assert first.status_code == 201, first.text
    rows = stack.item_rows(seed.sample_id)

    for body in (
        {"outcome": "doubtful_criterion"},  # el mismo resultado
        {"outcome": "consistent_criterion", "comment": COMMENT},  # otro
    ):
        again = stack.sample_result(coordinator, seed.item_ids[0], body)
        assert again.status_code == 409, again.text
        assert again.json()["code"] == "conflict"
        assert again.json()["detail_code"] == "accreditation_sample_already_reviewed"
    assert len(stack.results_of(seed)) == 1
    assert stack.item_rows(seed.sample_id) == rows


@pytest.mark.parametrize(
    "body",
    [
        {"outcome": "false_positive"},
        {"outcome": "confirmed"},
        {"outcome": "Authorized_Operation"},
        {"outcome": "consistent_criterion", "reason_category": "no_person_present"},
        {"outcome": "consistent_criterion", "classification_outcome": "false_positive"},
        {"outcome": "consistent_criterion", "classificationOutcome": "false_positive"},
        {"outcome": "consistent_criterion", "Reason-Text": "Otro motivo para la clasificación"},
        {"outcome": "criterion_error", "revert": True},
        {"outcome": "criterion_error", "reclassify": {"outcome": "false_positive"}},
        {"outcome": "criterion_error", "lifecycle_state": "discarded"},
    ],
    ids=[
        "outcome-false-positive",
        "outcome-confirmed",
        "outcome-authorized-mixed-case",
        "reason-category",
        "classification-outcome",
        "classification-outcome-camel",
        "reason-text-hyphen",
        "revert",
        "reclassify",
        "lifecycle-state",
    ],
)
def test_an_attempt_to_modify_the_classification_is_rejected_and_writes_nothing(
    stack: MonthlyReviewStack, body: dict[str, Any]
) -> None:
    seed = _seed(stack)
    coordinator = _coordinator(stack, seed)
    before = _untouched(stack, seed)
    response = stack.sample_result(coordinator, seed.item_ids[0], body)
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "invalid_request"
    assert response.json()["detail_code"] == "accreditation_sample_cannot_modify_classification"
    assert stack.results_of(seed) == []
    assert all(row["outcome"] is None for row in stack.item_rows(seed.sample_id))
    assert _untouched(stack, seed) == before


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"outcome": None},
        {"outcome": "consistent"},
        {"outcome": "consistent_criterion", "comment": "x" * 1001},
        {"outcome": "consistent_criterion", "comment": "con\u0000nulo"},
        {"outcome": "consistent_criterion", "foo": 1},
    ],
    ids=["empty", "null", "unknown", "comment-1001", "comment-control", "unknown-field"],
)
def test_a_malformed_result_is_invalid_request_without_detail(
    stack: MonthlyReviewStack, body: dict[str, Any]
) -> None:
    seed = _seed(stack)
    coordinator = _coordinator(stack, seed)
    response = stack.sample_result(coordinator, seed.item_ids[0], body)
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "invalid_request"
    assert "detail_code" not in response.json() or response.json()["detail_code"] is None
    assert stack.results_of(seed) == []


def test_a_comment_of_exactly_1000_and_a_blank_comment_are_accepted(
    stack: MonthlyReviewStack,
) -> None:
    seed = _seed(stack, count=2)
    coordinator = _coordinator(stack, seed)
    longest = ("Criterio coherente con el procedimiento. " * 30)[:1000]
    response = stack.sample_result(
        coordinator, seed.item_ids[0], {"outcome": "consistent_criterion", "comment": longest}
    )
    assert response.status_code == 201, response.text
    blank = stack.sample_result(
        coordinator, seed.item_ids[1], {"outcome": "consistent_criterion", "comment": "   "}
    )
    assert blank.status_code == 201, blank.text
    assert blank.json()["comment"] is None
    records = stack.results_of(seed)
    assert "comment" not in records[-1]["content"]


def test_a_blocked_term_in_the_comment_is_rejected_by_the_validator(
    stack: MonthlyReviewStack,
) -> None:
    seed = _seed(stack)
    coordinator = _coordinator(stack, seed)
    response = stack.sample_result(
        coordinator, seed.item_ids[0], {"outcome": "doubtful_criterion", "comment": BLOCKED_REASON}
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail_code"] == "loop_blocked_vocabulary_term"
    assert stack.results_of(seed) == []
    assert all(row["outcome"] is None for row in stack.item_rows(seed.sample_id))


@pytest.mark.parametrize("case", ["generating", "orphan"])
def test_an_item_of_an_unsealed_package_or_an_orphan_sample_is_not_available(
    stack: MonthlyReviewStack, case: str
) -> None:
    seed = _seed(stack, state="generating") if case == "generating" else _seed(stack, attach=False)
    coordinator = _coordinator(stack, seed)
    response = stack.sample_result(coordinator, seed.item_ids[0], {"outcome": "criterion_error"})
    assert response.status_code == 409, response.text
    assert response.json()["detail_code"] == "accreditation_monthly_package_not_available"
    assert stack.results_of(seed) == []


def test_out_of_scope_or_missing_items_answer_not_found(stack: MonthlyReviewStack) -> None:
    seed = _seed(stack)
    organization = seed.organization
    other_zone = stack.zone_in(organization)
    callers = {
        # Sin ``findings.classify`` (la ruta responde not_found antes del servicio).
        "line_manager": stack.line_manager(organization, seed.zone_id),
        "plant_manager": stack.member(
            organization, Role.PLANT_MANAGER, ScopeLevel.PLANT, organization.plant_id
        ),
        # Con la clave, pero sobre otra zona (guarda de zona del servicio).
        "coordinator_other_zone": stack.member(
            organization, Role.COORDINATOR_SST, ScopeLevel.ZONE, other_zone
        ),
        # Otra organización.
        "coordinator_b": stack.member(stack.actions.world.b),
        # Bajo concesión de proveedor (``findings.classify`` no está en la columna del instalador).
        "installer": stack.installer(organization),
    }
    for name, who in callers.items():
        response = stack.sample_result(who, seed.item_ids[0], {"outcome": "criterion_error"})
        assert response.status_code == 404, (name, response.text)
        assert response.json()["code"] == "not_found", name
    coordinator = _coordinator(stack, seed)
    missing = stack.sample_result(coordinator, uuid.uuid4(), {"outcome": "criterion_error"})
    assert missing.status_code == 404, missing.text
    assert stack.results_of(seed) == []
    # Y la coordinación de la zona sí puede (la guarda no está rota).
    ok = stack.sample_result(coordinator, seed.item_ids[0], {"outcome": "criterion_error"})
    assert ok.status_code == 201, ok.text


# --- SUP-U05-26: concurrencia ------------------------------------------------------------------


class _HoldsItem(SampleAuditService):
    """Con la fila del elemento bloqueada y sin resultado, espera a que la otra operación esté
    esperando un candado o haya terminado antes de escribir: fuerza el mismo entrelazado en cada
    corrida, sin depender del planificador (como ``test_loop_closure_vs_reclassification``)."""

    stack: MonthlyReviewStack
    holding: asyncio.Event
    other_done: asyncio.Event

    async def _checked(self, transaction: Transaction) -> None:
        self.holding.set()
        async with asyncio.timeout(LOCK_WAIT_SECONDS):
            while not self.other_done.is_set() and await _waiting_locks(self.stack) == 0:
                await asyncio.sleep(0.05)


def _race(
    stack: MonthlyReviewStack, seed: SampleSeed, first_item: uuid.UUID, second_item: uuid.UUID
) -> list[Any]:
    context = stack.context(_coordinator(stack, seed))
    holding, other_done = asyncio.Event(), asyncio.Event()
    first: Any = stack.service_like(_HoldsItem)
    first.stack, first.holding, first.other_done = stack, holding, other_done
    second = stack.audit

    async def run_first() -> Any:
        return await first.record_result(
            context, first_item, outcome="consistent_criterion", comment=COMMENT
        )

    async def run_second() -> Any:
        await holding.wait()
        try:
            return await second.record_result(context, second_item, outcome="criterion_error")
        finally:
            other_done.set()

    async def race() -> list[Any]:
        async with asyncio.timeout(4 * LOCK_WAIT_SECONDS):
            return list(await asyncio.gather(run_first(), run_second(), return_exceptions=True))

    results: list[Any] = stack.run(race())
    return results


@pytest.mark.parametrize("round_", range(3))
def test_two_simultaneous_results_of_one_item_have_a_single_winner(
    stack: MonthlyReviewStack, round_: int
) -> None:
    seed = _seed(stack)
    item = seed.item_ids[0]
    results = _race(stack, seed, item, item)
    winners = [result for result in results if isinstance(result, SampleAuditResult)]
    losers = [result for result in results if isinstance(result, SampleAuditRejected)]
    assert len(winners) == 1 and len(losers) == 1, results
    assert losers[0].detail_code is AccreditationDetailCode.SAMPLE_ALREADY_REVIEWED
    records = stack.results_of(seed)
    assert [record["content"]["sample_item_id"] for record in records] == [str(item)]
    (row,) = stack.item_rows(seed.sample_id)
    assert row["outcome"] == winners[0].outcome.value
    assert row["record_id"] == winners[0].record_id


@pytest.mark.parametrize("round_", range(3))
def test_the_last_two_items_at_once_complete_the_sample_exactly_once(
    stack: MonthlyReviewStack, round_: int
) -> None:
    seed = _seed(stack, count=2)
    results = _race(stack, seed, seed.item_ids[0], seed.item_ids[1])
    assert all(isinstance(result, SampleAuditResult) for result in results), results
    assert sorted(result.review_completed for result in results) == [False, True]
    assert stack.sample_row(seed.sample_id)["review_completed_at"] is not None


# --- SUP-U05-27: el alta de compromisos --------------------------------------------------------


def test_the_line_manager_records_commitments_and_no_finding_changes(
    stack: MonthlyReviewStack,
) -> None:
    seed = _seed(stack)
    organization = seed.organization
    organization_id = organization.organization_id
    line_manager = stack.line_manager(organization, seed.zone_id)
    responsible = stack.user(organization, Role.LINE_MANAGER, ScopeLevel.ZONE, seed.zone_id)
    before = _untouched(stack, seed)
    response = stack.record_commitments(
        line_manager,
        seed.package_id,
        [
            stack.commitment(responsible),
            stack.commitment(
                responsible,
                text="Programar la charla de cinco minutos del turno",
                due_date="2027-01-31",
            ),
        ],
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["package_id"] == str(seed.package_id)
    assert [item["state"] for item in body["commitments"]] == ["open", "open"]
    assert body["commitments"][0]["text"] == COMMITMENT_TEXT
    assert body["commitments"][1]["due_date"] == "2027-01-31"

    (record,) = stack.batches_of(organization_id, seed.package_id)
    assert record["schema_version"] == 2
    assert str(record["record_id"]) == body["record_id"]
    assert record["content"]["commitment_batch_id"] == body["commitment_batch_id"]
    assert [entry["state"] for entry in record["content"]["commitments"]] == ["open", "open"]
    assert record["content"]["recorded_by"]["role_in_use"] == "line_manager"
    assert stack.events(organization_id, body["record_id"]) == ["commitments_recorded"]
    rows = [
        row
        for row in stack.commitment_rows(organization_id)
        if row["package_id"] == seed.package_id
    ]
    assert {str(row["commitment_id"]) for row in rows} == {
        item["commitment_id"] for item in body["commitments"]
    }
    assert all(
        row["state"] == "open" and str(row["record_id"]) == body["record_id"] for row in rows
    )
    assert _untouched(stack, seed) == before

    listed = stack.send(
        line_manager,
        "GET",
        "/accreditation/commitments",
        params={"zone_id": str(seed.zone_id), "period": body["commitments"][0]["period"]},
    )
    assert listed.status_code == 200, listed.text
    # Un mismo lote comparte ``recorded_at``: la lista los ordena por ``commitment_id``.
    assert [item["commitment_id"] for item in listed.json()] == sorted(
        item["commitment_id"] for item in body["commitments"]
    )


@pytest.mark.parametrize(
    ("entry_changes", "count"),
    [
        ({"text": "Diecinueve letras.."}, 1),  # 19 caracteres
        ({"text": "x" * 1001}, 1),
        ({"text": "   " + "a" * 18 + "   "}, 1),  # 18 de contenido
        ({"due_date": "2026-02-30"}, 1),
        ({"due_date": "2026-2-03"}, 1),
        ({"due_date": "2026-12-01T00:00:00Z"}, 1),
        ({}, 21),
        ({}, 0),
    ],
    ids=[
        "text-19",
        "text-1001",
        "text-blank-padded",
        "date-30-feb",
        "date-short",
        "date-with-time",
        "21-entries",
        "0-entries",
    ],
)
def test_malformed_commitments_are_invalid_request(
    stack: MonthlyReviewStack, entry_changes: dict[str, Any], count: int
) -> None:
    seed = _seed(stack)
    organization = seed.organization
    line_manager = stack.line_manager(organization, seed.zone_id)
    responsible = stack.user(organization, Role.LINE_MANAGER, ScopeLevel.ZONE, seed.zone_id)
    entries = [stack.commitment(responsible, **entry_changes) for _ in range(count)]
    response = stack.record_commitments(line_manager, seed.package_id, entries)
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "invalid_request"
    assert response.json().get("detail_code") is None
    assert [
        row
        for row in stack.commitment_rows(organization.organization_id)
        if row["package_id"] == seed.package_id
    ] == []


def test_the_bounds_of_text_and_count_are_accepted(stack: MonthlyReviewStack) -> None:
    seed = _seed(stack)
    organization = seed.organization
    line_manager = stack.line_manager(organization, seed.zone_id)
    responsible = stack.user(organization, Role.LINE_MANAGER, ScopeLevel.ZONE, seed.zone_id)
    entries = [stack.commitment(responsible, text="Veinte letras exacta") for _ in range(19)]
    entries.append(stack.commitment(responsible, text="y" * 1000))
    response = stack.record_commitments(line_manager, seed.package_id, entries)
    assert response.status_code == 201, response.text
    assert len(response.json()["commitments"]) == 20


def test_a_responsible_without_scope_on_the_zone_is_out_of_scope(
    stack: MonthlyReviewStack,
) -> None:
    seed = _seed(stack)
    organization = seed.organization
    line_manager = stack.line_manager(organization, seed.zone_id)
    other_zone = stack.zone_in(organization)
    inactive = stack.user(organization, Role.LINE_MANAGER, ScopeLevel.ZONE, seed.zone_id)
    stack.actions.deactivate(inactive)
    plant_wide = stack.user(
        organization, Role.PLANT_MANAGER, ScopeLevel.PLANT, organization.plant_id
    )
    for name, responsible in {
        "other_zone": stack.user(organization, Role.LINE_MANAGER, ScopeLevel.ZONE, other_zone),
        "inactive": inactive,
        "missing": uuid.uuid4(),
        "other_organization": stack.user(stack.actions.world.b),
    }.items():
        response = stack.record_commitments(
            line_manager,
            seed.package_id,
            [stack.commitment(plant_wide), stack.commitment(responsible)],
        )
        assert response.status_code == 400, (name, response.text)
        assert response.json()["detail_code"] == "accreditation_commitment_out_of_scope", name
    assert [
        row
        for row in stack.commitment_rows(organization.organization_id)
        if row["package_id"] == seed.package_id
    ] == []
    # Con alcance de planta u organización, sí.
    ok = stack.record_commitments(line_manager, seed.package_id, [stack.commitment(plant_wide)])
    assert ok.status_code == 201, ok.text


def test_a_blocked_term_in_a_commitment_is_rejected_by_the_validator(
    stack: MonthlyReviewStack,
) -> None:
    seed = _seed(stack)
    organization = seed.organization
    line_manager = stack.line_manager(organization, seed.zone_id)
    responsible = stack.user(organization, Role.LINE_MANAGER, ScopeLevel.ZONE, seed.zone_id)
    response = stack.record_commitments(
        line_manager, seed.package_id, [stack.commitment(responsible, text=BLOCKED_REASON)]
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail_code"] == "loop_blocked_vocabulary_term"
    assert stack.records_of(organization.organization_id, "commitments_recorded") == [] or all(
        record["content"]["package_id"] != str(seed.package_id)
        for record in stack.records_of(organization.organization_id, "commitments_recorded")
    )


def test_commitments_on_a_package_out_of_scope(stack: MonthlyReviewStack) -> None:
    seed = _seed(stack)
    organization = seed.organization
    other_zone = stack.zone_in(organization)
    other_package = stack.seed_package(organization, zone_id=other_zone)
    line_manager = stack.line_manager(organization, seed.zone_id)
    responsible = stack.user(organization, Role.LINE_MANAGER, ScopeLevel.ORGANIZATION)
    entry = [stack.commitment(responsible)]

    # Un paquete que el mando de línea no ve (otra zona, otra organización o inexistente).
    b = stack.actions.world.b
    b_package = stack.seed_package(b)
    for package_id in (other_package, b_package, uuid.uuid4()):
        response = stack.record_commitments(line_manager, package_id, entry)
        assert response.status_code == 404, (package_id, response.text)
        assert response.json()["code"] == "not_found"

    # Un paquete que ve (gerente de la planta) pero fuera de la zona en la que registra.
    wider = stack.with_role(
        organization, line_manager, Role.PLANT_MANAGER, ScopeLevel.PLANT, organization.plant_id
    )
    response = stack.record_commitments(wider, other_package, entry)
    assert response.status_code == 400, response.text
    assert response.json()["detail_code"] == "accreditation_commitment_out_of_scope"

    # Sin ``commitments.write`` (gerente de planta solo; instalador bajo concesión): not_found.
    for who in (
        stack.member(organization, Role.PLANT_MANAGER, ScopeLevel.PLANT, organization.plant_id),
        stack.installer(organization),
    ):
        response = stack.record_commitments(who, seed.package_id, entry)
        assert response.status_code == 404, response.text
    assert all(
        row["package_id"] not in (other_package, b_package, seed.package_id)
        for row in stack.commitment_rows(organization.organization_id)
    )

    # La coordinación SST también registra compromisos; un paquete en composición, no.
    coordinator = _coordinator(stack, seed)
    assert stack.record_commitments(coordinator, seed.package_id, entry).status_code == 201
    generating = stack.seed_package(organization, state="generating")
    response = stack.record_commitments(line_manager, generating, entry)
    assert response.status_code == 409, response.text
    assert response.json()["detail_code"] == "accreditation_monthly_package_not_available"


# --- SUP-U05-28: la actualización, un registro nuevo -------------------------------------------


def _one_commitment(stack: MonthlyReviewStack) -> tuple[SampleSeed, Any, dict[str, Any]]:
    seed = _seed(stack)
    organization = seed.organization
    line_manager = stack.line_manager(organization, seed.zone_id)
    responsible = stack.user(organization, Role.LINE_MANAGER, ScopeLevel.ZONE, seed.zone_id)
    response = stack.record_commitments(
        line_manager, seed.package_id, [stack.commitment(responsible)]
    )
    assert response.status_code == 201, response.text
    return seed, line_manager, response.json()


def test_an_update_writes_a_new_record_and_changes_only_the_commitment(
    stack: MonthlyReviewStack,
) -> None:
    seed, line_manager, recorded = _one_commitment(stack)
    organization_id = seed.organization.organization_id
    commitment_id = recorded["commitments"][0]["commitment_id"]
    before = _untouched(stack, seed)

    response = stack.update_commitment(
        line_manager, uuid.UUID(commitment_id), {"state": "done", "note": NOTE}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["commitment"]["state"] == "done"
    assert body["commitment"]["closed_note"] == NOTE
    assert body["commitment"]["closed_at"] is not None
    assert body["commitment"]["record_id"] == recorded["record_id"]  # el del alta, intacto
    assert body["record_id"] != recorded["record_id"]

    records = [
        record
        for record in stack.records_of(organization_id, "commitments_recorded")
        if record["content"]["package_id"] == str(seed.package_id)
    ]
    assert [str(record["record_id"]) for record in records] == [
        recorded["record_id"],
        body["record_id"],
    ]
    (entry,) = records[-1]["content"]["commitments"]
    assert entry == {
        "commitment_id": commitment_id,
        "text": COMMITMENT_TEXT,
        "responsible_user_id": recorded["commitments"][0]["responsible_user_id"],
        "due_date": "2026-12-15",
        "state": "done",
        "closed_note": NOTE,
    }
    assert stack.events(organization_id, body["record_id"]) == []  # sin evento al actualizar
    (row,) = [
        row
        for row in stack.commitment_rows(organization_id)
        if str(row["commitment_id"]) == commitment_id
    ]
    assert (row["state"], row["closed_note"]) == ("done", NOTE)
    assert str(row["record_id"]) == recorded["record_id"]
    assert _untouched(stack, seed) == before

    # Volver a ``open`` limpia la nota y la marca de cierre (el expediente las conserva).
    reopened = stack.update_commitment(line_manager, uuid.UUID(commitment_id), {"state": "open"})
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["commitment"]["closed_at"] is None
    assert reopened.json()["commitment"]["closed_note"] is None
    dropped = stack.update_commitment(line_manager, uuid.UUID(commitment_id), {"state": "dropped"})
    assert dropped.status_code == 200, dropped.text
    assert dropped.json()["commitment"]["closed_note"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"state": "closed"},
        {"state": "open", "note": NOTE},
        {"state": "done", "note": "n" * 501},
        {"state": "done", "closed_at": "2026-12-01T00:00:00.000Z"},
        {"state": "done", "text": "Cambiar el texto del compromiso registrado"},
    ],
    ids=["unknown-state", "note-with-open", "note-501", "closed-at", "text"],
)
def test_a_malformed_update_is_invalid_request(
    stack: MonthlyReviewStack, body: dict[str, Any]
) -> None:
    seed, line_manager, recorded = _one_commitment(stack)
    commitment_id = uuid.UUID(recorded["commitments"][0]["commitment_id"])
    rows = stack.commitment_rows(seed.organization.organization_id)
    response = stack.update_commitment(line_manager, commitment_id, body)
    assert response.status_code == 400, response.text
    assert response.json().get("detail_code") is None
    assert stack.commitment_rows(seed.organization.organization_id) == rows


def test_a_note_of_exactly_500_and_a_blocked_note(stack: MonthlyReviewStack) -> None:
    _seed_unused, line_manager, recorded = _one_commitment(stack)
    commitment_id = uuid.UUID(recorded["commitments"][0]["commitment_id"])
    blocked = stack.update_commitment(
        line_manager, commitment_id, {"state": "done", "note": BLOCKED_REASON}
    )
    assert blocked.status_code == 400, blocked.text
    assert blocked.json()["detail_code"] == "loop_blocked_vocabulary_term"
    longest = stack.update_commitment(
        line_manager, commitment_id, {"state": "done", "note": "n" * 500}
    )
    assert longest.status_code == 200, longest.text


def test_an_update_out_of_scope(stack: MonthlyReviewStack) -> None:
    seed, _line_manager, recorded = _one_commitment(stack)
    organization = seed.organization
    commitment_id = uuid.UUID(recorded["commitments"][0]["commitment_id"])
    other_zone = stack.zone_in(organization)
    other_manager = stack.line_manager(organization, other_zone)
    for who in (
        other_manager,
        stack.member(stack.actions.world.b, Role.LINE_MANAGER),
        stack.installer(organization),
    ):
        response = stack.update_commitment(who, commitment_id, {"state": "done"})
        assert response.status_code == 404, response.text
    wider = stack.with_role(
        organization, other_manager, Role.PLANT_MANAGER, ScopeLevel.PLANT, organization.plant_id
    )
    response = stack.update_commitment(wider, commitment_id, {"state": "done"})
    assert response.status_code == 400, response.text
    assert response.json()["detail_code"] == "accreditation_commitment_out_of_scope"
    (row,) = [
        row
        for row in stack.commitment_rows(organization.organization_id)
        if row["commitment_id"] == commitment_id
    ]
    assert row["state"] == "open"


@pytest.mark.parametrize("round_", range(3))
def test_two_simultaneous_updates_leave_the_row_as_the_last_record(
    stack: MonthlyReviewStack, round_: int
) -> None:
    seed, line_manager, recorded = _one_commitment(stack)
    organization_id = seed.organization.organization_id
    commitment_id = uuid.UUID(recorded["commitments"][0]["commitment_id"])
    context = stack.context(line_manager)

    async def update(state: str) -> Any:
        return await stack.commitments.update(context, commitment_id, state=state)

    async def race() -> list[Any]:
        async with asyncio.timeout(4 * LOCK_WAIT_SECONDS):
            return list(
                await asyncio.gather(update("done"), update("dropped"), return_exceptions=True)
            )

    results = stack.run(race())
    assert all(isinstance(result, CommitmentUpdateResult) for result in results), results
    updates = [
        record
        for record in stack.records_of(organization_id, "commitments_recorded")
        if record["content"]["commitments"][0]["commitment_id"] == str(commitment_id)
    ][1:]
    assert len(updates) == 2
    (row,) = [
        row
        for row in stack.commitment_rows(organization_id)
        if row["commitment_id"] == commitment_id
    ]
    assert row["state"] == updates[-1]["content"]["commitments"][0]["state"]


# --- SUP-U05-29: la lista ----------------------------------------------------------------------


def test_the_list_filters_by_zone_and_period_and_hides_the_rest(stack: MonthlyReviewStack) -> None:
    organization = stack.actions.world.a
    zone_id = stack.zone_in(organization)
    line_manager = stack.line_manager(organization, zone_id)
    responsible = stack.user(organization, Role.LINE_MANAGER, ScopeLevel.ZONE, zone_id)
    first = stack.seed_package(organization, zone_id=zone_id, period="2031-03")
    second = stack.seed_package(organization, zone_id=zone_id, period="2031-04")
    for package_id in (first, second):
        response = stack.record_commitments(
            line_manager, package_id, [stack.commitment(responsible)]
        )
        assert response.status_code == 201, response.text
    listed = stack.send(
        line_manager,
        "GET",
        "/accreditation/commitments",
        params={"zone_id": str(zone_id), "period": "2031-03"},
    )
    assert listed.status_code == 200, listed.text
    assert [item["package_id"] for item in listed.json()] == [str(first)]
    empty = stack.send(
        line_manager,
        "GET",
        "/accreditation/commitments",
        params={"zone_id": str(zone_id), "period": "2031-05"},
    )
    assert empty.status_code == 200 and empty.json() == []
    for zone in (organization.zone_id, stack.actions.world.b.zone_id, uuid.uuid4()):
        hidden = stack.send(
            line_manager,
            "GET",
            "/accreditation/commitments",
            params={"zone_id": str(zone), "period": "2031-03"},
        )
        assert hidden.status_code == 404, (zone, hidden.text)
    bad = stack.send(
        line_manager,
        "GET",
        "/accreditation/commitments",
        params={"zone_id": str(zone_id), "period": "2031-13"},
    )
    assert bad.status_code == 400, bad.text


# --- orden de candados: auditoría frente a compromisos en la misma planta ----------------------


@pytest.mark.parametrize("round_", range(3))
def test_an_audit_and_a_commitment_batch_at_once_never_deadlock(
    stack: MonthlyReviewStack, round_: int
) -> None:
    seed = _seed(stack)
    organization = seed.organization
    coordinator = stack.context(_coordinator(stack, seed))
    line_manager = stack.context(stack.line_manager(organization, seed.zone_id))
    responsible = stack.user(organization, Role.LINE_MANAGER, ScopeLevel.ZONE, seed.zone_id)
    audit: SampleAuditService = stack.audit
    commitments: CommitmentService = stack.commitments

    async def review() -> Any:
        return await audit.record_result(
            coordinator, seed.item_ids[0], outcome="consistent_criterion"
        )

    async def commit() -> Any:
        return await commitments.record(
            line_manager,
            seed.package_id,
            [CommitmentDraft(COMMITMENT_TEXT, str(responsible), "2026-12-15")],
        )

    async def race() -> list[Any]:
        async with asyncio.timeout(4 * LOCK_WAIT_SECONDS):
            return list(await asyncio.gather(review(), commit(), return_exceptions=True))

    reviewed, committed = stack.run(race())
    assert isinstance(reviewed, SampleAuditResult), reviewed
    assert isinstance(committed, CommitmentsRecordedResult), committed
