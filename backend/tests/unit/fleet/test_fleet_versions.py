"""Versiones de flota (TASK-226; LC-GOB-17, C-PLA-16; BR-GOB-101 a 104; DE §3.11 y §3.12).

**Sin propiedades nuevas** (BL §6, C-PLA-16; LC-GOB-17 «Verificación»): el componente no transforma
datos ni mantiene una máquina de estados propia. Publica una versión objetivo ya validada contra la
ventana de compatibilidad del contrato, cuya decisión es ``is_compatible`` de U-01 y ya la cubre
PR-GOB-07 por oráculo, y refleja en el inventario el resultado que el nodo reporta. Su criticidad es
**Baja** y en el piloto la actualización es manual por equipo (D-5). Se verifica con **ejemplos**:
los de H-48 (aplicada, revertida conservando la cola y fallida) en
``tests/examples/test_h48_update_results.py``, y aquí los bordes de cada regla de dominio:

- ventana del contrato en el instante de publicar (BR-GOB-101): el borde inferior de la ventana de
  menores, la menor siguiente, otra mayor y una menor en aviso de retiro antes y después de su
  fecha, comparados con la decisión de U-01 (``is_accepted(is_compatible(...))``);
- ventana de mantenimiento (D-5): ``to > from`` al milisegundo; está en el registro y nunca en el
  evento;
- versión de la plataforma (``ReleaseVersion``), nodos de una publicación (1 a 100, sin repetir) y
  resultado (``applied | reverted | failed``, nota T-05);
- el cuerpo de la ruta y la parte del alcance que depende del cuerpo (``node_zone_mismatch``).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from vigia_contracts.models.enumerations import RejectionCode, UpdateOutcome
from vigia_contracts.versioning import Version, is_accepted, is_compatible

from tests.factories import uuid7
from vigia_platform.fleet.adapters.http.target_versions import TargetVersionBody
from vigia_platform.fleet.application.ingest import IngestRejected
from vigia_platform.fleet.application.update_results import UpdateResultService
from vigia_platform.fleet.domain.enums import UpdateResult
from vigia_platform.fleet.domain.fleet_versions import (
    MAX_TARGET_NODES,
    MaintenanceWindow,
    TargetVersionInvalid,
    TargetVersionPublication,
    UpdateReport,
    is_release_version,
    outcome_of,
    within_contract_window,
)
from vigia_platform.fleet.events import TargetVersionPublished
from vigia_platform.fleet.events import UpdateResultReceived as UpdateResultEvent
from vigia_platform.fleet.record_types import NodeTargetVersionPublished
from vigia_platform.fleet.record_types import UpdateResultReceived as UpdateResultRecord
from vigia_platform.node_api.versioning import RetiringMinor, VersionPolicy
from vigia_platform.shared.signing.keys import format_timestamp

NOW = datetime(2026, 10, 5, 12, 0, 0, 123000, tzinfo=UTC)


def _oracle(version: str, policy: VersionPolicy, today: datetime) -> bool:
    return is_accepted(
        is_compatible(
            Version.parse(version),
            policy.current,
            today=today,
            window=policy.window,
            retiring=policy.retiring,
            latest_minors=policy.latest_minors,
            major_published_at=policy.major_published_at,
            coexistence_days=policy.coexistence_days,
        )
    )


# --- BR-GOB-101: ventana del contrato en el instante de publicar --------------------------------

CURRENT = VersionPolicy(current=Version.parse("1.3.0"))


@pytest.mark.parametrize(
    ("version", "inside"),
    [
        ("1.3.0", True),  # la vigente
        ("1.3.9", True),  # el parche no interviene
        ("1.2.4-rc.1", True),  # la preliberación tampoco
        ("1.1.0", True),  # borde inferior: vigente - ventana (2)
        ("1.0.9", False),  # una menor por debajo del borde
        ("1.4.0", False),  # más nueva que la plataforma (rejected_newer)
        ("2.0.0", False),  # otra mayor
        ("0.9.0", False),  # mayor anterior sin convivencia
    ],
)
def test_br_gob_101_the_contract_window_is_the_decision_of_u01(version: str, inside: bool) -> None:
    assert within_contract_window(version, CURRENT, NOW) is inside
    assert _oracle(version, CURRENT, NOW) is inside


def test_br_gob_101_a_retiring_minor_publishes_until_its_date_and_not_from_it() -> None:
    retires_at = NOW + timedelta(days=3)
    policy = VersionPolicy(
        current=Version.parse("1.3.0"),
        retiring=(RetiringMinor("1.1.0", format_timestamp(retires_at)),),
    )
    before = retires_at - timedelta(milliseconds=1)
    assert within_contract_window("1.1.5", policy, before) is True
    assert within_contract_window("1.1.5", policy, retires_at) is False
    assert _oracle("1.1.5", policy, before) and not _oracle("1.1.5", policy, retires_at)


def test_br_gob_101_the_default_platform_policy_accepts_its_minor_and_the_previous_one() -> None:
    # La plataforma implementa la 1.1.0 del contrato (A-63): con la ventana de dos menores, las
    # series 1.1 y 1.0 están en ventana; una menor más nueva, otra mayor o la 0.x, no.
    policy = VersionPolicy()
    assert str(policy.current) == "1.1.0"
    assert within_contract_window("1.1.0", policy, NOW)
    assert within_contract_window("1.1.12", policy, NOW)
    assert within_contract_window("1.0.0", policy, NOW)
    assert within_contract_window("1.0.12", policy, NOW)
    assert not within_contract_window("1.2.0", policy, NOW)
    assert not within_contract_window("2.0.0", policy, NOW)
    assert not within_contract_window("0.9.0", policy, NOW)


@pytest.mark.parametrize("version", ["", "1.0", "v1.0.0", "1.0.0.0", "01.0.0", "1.0.0-"])
def test_br_gob_101_a_version_the_policy_cannot_read_is_outside(version: str) -> None:
    assert within_contract_window(version, VersionPolicy(), NOW) is False


# --- ReleaseVersion ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("0.0.0", True),
        ("1.0.0-rc.1", True),
        ("1.0.0+build.7", True),
        ("999999999.0.0", True),  # nueve cifras: el máximo
        ("1000000000.0.0", False),  # diez cifras
        ("1.0.0-RC1", False),  # mayúsculas: texto libre para el registro
        ("1.0.0-" + "a" * 31, True),  # preliberación de 31: el máximo
        ("1.0.0-" + "a" * 32, False),
        ("999.999.999-rc." + "a" * 49, False),  # SemVer de 64 del contrato, fuera de la plataforma
        ("1.0", False),
        (" 1.0.0", False),
        ("1.0.0\n", False),
        (100, False),
        (None, False),
    ],
)
def test_release_version_bounds(value: object, valid: bool) -> None:
    assert is_release_version(value) is valid


# --- D-5: ventana de mantenimiento ---------------------------------------------------------------


def test_d5_the_maintenance_window_needs_to_after_from_at_millisecond_precision() -> None:
    start = NOW
    assert MaintenanceWindow.of(start, start + timedelta(milliseconds=1)).ends_at > start
    with pytest.raises(TargetVersionInvalid):
        MaintenanceWindow.of(start, start)
    with pytest.raises(TargetVersionInvalid):
        MaintenanceWindow.of(start, start - timedelta(milliseconds=1))
    # Dentro del mismo milisegundo, tras truncar, la ventana es vacía.
    with pytest.raises(TargetVersionInvalid):
        MaintenanceWindow.of(start, start + timedelta(microseconds=500))
    with pytest.raises(TargetVersionInvalid):
        MaintenanceWindow.of(start.replace(tzinfo=None), start + timedelta(hours=1))


def _publication(nodes: int, **changes: object) -> TargetVersionPublication:
    fields: dict[str, object] = {
        "publication_id": uuid7(),
        "plant_id": uuid.uuid4(),
        "target_version": "1.0.3",
        "node_ids": tuple(uuid.uuid4() for _ in range(nodes)),
        "window": MaintenanceWindow.of(NOW, NOW + timedelta(hours=2)),
        "published_by": uuid.uuid4(),
        "published_at": NOW,
        "ledger_record_id": uuid7(),
    }
    fields.update(changes)
    return TargetVersionPublication(**fields)  # type: ignore[arg-type]


def test_d5_the_window_is_in_the_record_and_never_in_the_events() -> None:
    publication = _publication(3)
    content = publication.record_content()
    assert content["maintenance_window"] == {
        "starts_at": "2026-10-05T12:00:00.123Z",
        "ends_at": "2026-10-05T14:00:00.123Z",
    }
    NodeTargetVersionPublished.model_validate_json(json.dumps(content))
    payloads = publication.event_payloads()
    assert [payload["node_id"] for payload in payloads] == [
        str(node) for node in publication.node_ids
    ]
    for payload in payloads:
        assert set(payload) == {"node_id", "target_version"}
        TargetVersionPublished.model_validate_json(json.dumps(payload))
    assert "maintenance" not in json.dumps(payloads)


@pytest.mark.parametrize(("nodes", "valid"), [(0, False), (1, True), (100, True), (101, False)])
def test_a_publication_reaches_one_to_a_hundred_nodes(nodes: int, valid: bool) -> None:
    assert MAX_TARGET_NODES == 100
    if valid:
        assert len(_publication(nodes).node_ids) == nodes
    else:
        with pytest.raises(TargetVersionInvalid):
            _publication(nodes)


def test_a_publication_never_repeats_a_node_nor_carries_a_mixed_case_version() -> None:
    node = uuid.uuid4()
    with pytest.raises(TargetVersionInvalid):
        _publication(0, node_ids=(node, node))
    with pytest.raises(TargetVersionInvalid):
        _publication(1, target_version="1.0.0-RC1")


# --- Nota T-05: resultado ------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", list(UpdateOutcome))
def test_t05_every_update_outcome_of_the_contract_has_its_projection(
    outcome: UpdateOutcome,
) -> None:
    assert outcome_of(outcome) is UpdateResult(outcome.value)
    assert {result.value for result in UpdateResult} == {"applied", "reverted", "failed"}


def test_t05_an_unknown_outcome_is_not_projected() -> None:
    with pytest.raises(ValueError):
        outcome_of("rolled_back")


def _report(result: UpdateResult = UpdateResult.APPLIED, **changes: object) -> UpdateReport:
    fields: dict[str, object] = {
        "update_result_id": uuid7(),
        "node_id": uuid.uuid4(),
        "target_version": "1.0.3",
        "result": result,
        "reported_at": NOW,
    }
    fields.update(changes)
    return UpdateReport(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize("result", list(UpdateResult))
def test_the_record_and_the_event_of_each_result_are_valid(result: UpdateResult) -> None:
    report = _report(result)
    content = report.record_content()
    assert content["reported_at"] == "2026-10-05T12:00:00.123Z"
    UpdateResultRecord.model_validate_json(json.dumps(content))
    payload = report.event_payload()
    assert payload == {
        "node_id": str(report.node_id),
        "target_version": "1.0.3",
        "result": result.value,
    }
    UpdateResultEvent.model_validate_json(json.dumps(payload))


def test_the_same_report_ignores_the_reception_mark_and_nothing_else() -> None:
    report = _report()
    assert report.same_as(replace(report, reported_at=NOW + timedelta(hours=1)))
    assert not report.same_as(replace(report, result=UpdateResult.FAILED))
    assert not report.same_as(replace(report, target_version="1.0.4"))
    assert not report.same_as(replace(report, node_id=uuid.uuid4()))
    assert not report.same_as(replace(report, update_result_id=uuid7()))


# --- Cuerpo de la ruta y alcance del cuerpo ------------------------------------------------------

_BODY: dict[str, object] = {
    "plant_id": str(uuid.uuid4()),
    "node_ids": [str(uuid.uuid4())],
    "target_version": "1.0.0",
    "maintenance_window": {"from": "2026-10-10T02:00:00.000Z", "to": "2026-10-10T04:00:00.000Z"},
}


def test_the_route_body_reads_from_and_to_and_forbids_anything_else() -> None:
    body = TargetVersionBody.model_validate_json(json.dumps(_BODY))
    assert body.maintenance_window.from_ < body.maintenance_window.to
    assert body.group is None
    with pytest.raises(ValidationError):
        TargetVersionBody.model_validate_json(json.dumps({**_BODY, "zone_id": str(uuid.uuid4())}))
    with pytest.raises(ValidationError):
        TargetVersionBody.model_validate_json(
            json.dumps({**_BODY, "maintenance_window": {"from_": "x", "to": "y"}})
        )
    with pytest.raises(ValidationError):
        TargetVersionBody.model_validate_json(json.dumps({**_BODY, "group": "organization"}))


@pytest.mark.parametrize(("count", "valid"), [(0, False), (1, True), (100, True), (101, False)])
def test_the_route_body_takes_one_to_a_hundred_node_ids(count: int, valid: bool) -> None:
    document = {**_BODY, "node_ids": [str(uuid.uuid4()) for _ in range(count)]}
    if valid:
        assert len(TargetVersionBody.model_validate_json(json.dumps(document)).node_ids or ()) == (
            count
        )
    else:
        with pytest.raises(ValidationError):
            TargetVersionBody.model_validate_json(json.dumps(document))


class _Node:
    def __init__(self) -> None:
        self.organization_id = uuid.uuid4()
        self.plant_id = uuid.uuid4()
        self.node_id = uuid.uuid4()


@pytest.mark.parametrize("name", ["organization_id", "plant_id", "node_id"])
def test_the_body_scope_of_another_certificate_is_node_zone_mismatch(name: str) -> None:
    node = _Node()
    own = {
        "organization_id": str(node.organization_id),
        "plant_id": str(node.plant_id),
        "node_id": str(node.node_id),
    }
    UpdateResultService.check_body_scope(node, own)  # type: ignore[arg-type]
    with pytest.raises(IngestRejected) as rejected:
        UpdateResultService.check_body_scope(node, {**own, name: str(uuid.uuid4())})  # type: ignore[arg-type]
    assert rejected.value.code is RejectionCode.NODE_ZONE_MISMATCH
    assert rejected.value.field == name
    # Lo que no se puede leer lo decide el esquema (paso 4), no el alcance.
    UpdateResultService.check_body_scope(node, {**own, name: "no-es-un-uuid"})  # type: ignore[arg-type]
    UpdateResultService.check_body_scope(node, {**own, name: 7})  # type: ignore[arg-type]
    UpdateResultService.check_body_scope(node, [own])  # type: ignore[arg-type]
    UpdateResultService.check_body_scope(None, {**own, name: str(uuid.uuid4())})
