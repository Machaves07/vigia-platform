"""PR-LAZ-32: el mando de línea no cambia ningún hallazgo; solo compromisos, y de su alcance
(TASK-347; BR-LAZ-109; H12, H-31, H-54).

business-logic-model.md de U-04 §11, ``line_manager_commands``. Dos partes:

1. **Metapropiedad sobre la matriz** (sin base): ninguna clave de las rutas de ``/loop/*``
   (``loop_routers()``, lo que la raíz monta) está en la columna ``line_manager`` de
   ``identity.authz.matrix``. La única excepción, declarada y exacta, es la **lectura** de la
   configuración del lazo de una planta (``GET /loop/plants/{plant_id}/configuration``, SUP-U05-38),
   que usa ``hierarchy.read``, la clave de lectura universal (la tienen los siete roles, nota del
   2026-09-21 de BLM §10): no lee ni toca ningún hallazgo. Además, el mando de línea no tiene
   ninguna clave que actúe sobre un hallazgo. Una ruta sonda de ``/loop/*`` con una clave de su
   columna hace fallar la comprobación.
2. **Propiedad** (PostgreSQL 16 real, aplicación real): para cualquier secuencia generada del
   mando de línea de **una** zona (altas de compromisos en su zona, en otra zona, en otra
   organización o sobre un paquete inexistente; actualizaciones de compromisos propios y ajenos;
   intentos sobre cada escritura del lazo y sobre el resultado de la muestra; lecturas):

   - tras **cada** operación, ningún hallazgo de ninguna de las dos organizaciones cambia de
     estado, clasificación ni visibilidad (registros ``classification`` hash a hash,
     ``loop.finding_state``, ``loop.classification``, ``ledger.label`` y acciones idénticos);
   - solo cambian compromisos, y solo los del paquete de su zona: las filas de
     ``accreditation.commitment`` de cualquier otro paquete (de su organización o de otra) son
     idénticas, el expediente de la otra organización no recibe nada y el de la suya solo
     ``commitments_recorded``;
   - fuera de su alcance, ``not_found`` (o ``commitment_out_of_scope`` para un responsable sin
     alcance); cada respuesta es la del modelo, en el orden del servicio.

Solo datos generados.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Iterator, Mapping
from typing import Any, Final

import pytest
from fastapi import APIRouter, FastAPI
from hypothesis import HealthCheck, event, given, settings

from tests.integration.conftest import PostgresEndpoint
from tests.loop_classification_support import BLOCKED_REASON
from tests.monthly_review_support import COMMITMENT_TEXT, MonthlyReviewStack, monthly_review_stack
from tests.properties.laz.strategies.monthly_review import (
    LineManagerCommand,
    line_manager_commands,
)
from vigia_platform.identity.authz.matrix import MATRIX, PermissionKey
from vigia_platform.loop.registration import loop_routers
from vigia_platform.shared.api.declarations import iter_declared_routes, requires
from vigia_platform.shared.context import Role, ScopeLevel

EXAMPLES: Final = 20
"""Cada ejemplo siembra un paquete, registra un compromiso y hace hasta ocho peticiones reales."""
NOTE: Final = "Señalización renovada y verificada en la ronda semanal"
SHORT_TEXT: Final = "Texto muy corto"

READ_ONLY_LOOP_ROUTES: Final[frozenset[tuple[str, str]]] = frozenset(
    {("GET", "/loop/plants/{plant_id}/configuration")}
)
"""Las rutas de ``/loop/*`` cuya clave está en la columna del mando de línea, por diseño: la
lectura de la configuración de la planta con ``hierarchy.read`` (la tienen todos los roles)."""

FINDING_KEYS: Final = frozenset(
    {
        PermissionKey.FINDINGS_READ,
        PermissionKey.FINDINGS_CLASSIFY,
        PermissionKey.REVIEW_QUEUE_RESOLVE,
        PermissionKey.ACTIONS_MANAGE,
        PermissionKey.FINDINGS_CLOSE_AND_SIGN,
        PermissionKey.ROOT_CAUSE_ANALYZE,
        PermissionKey.CLOSURE_ATTACHMENTS_READ,
    }
)
"""Las claves que leen, clasifican, reclasifican, cierran, editan u ocultan un hallazgo."""


# --- 1. metapropiedad sobre la matriz ----------------------------------------------------------


def _loop_route_keys(routers: Iterable[APIRouter]) -> dict[tuple[str, str], str]:
    app = FastAPI()
    for router in routers:
        app.include_router(router)
    keys: dict[tuple[str, str], str] = {}
    for route in iter_declared_routes(app.routes):
        if not route.path.startswith("/loop/"):
            continue
        for declaration in route.declarations:
            if declaration.permission is not None:
                for method in route.methods:
                    keys[(method, route.path)] = declaration.permission
    return keys


def _line_manager_routes(keys: Mapping[tuple[str, str], str]) -> dict[tuple[str, str], str]:
    column = {key.value for key in MATRIX[Role.LINE_MANAGER]}
    return {
        route: key
        for route, key in keys.items()
        if key in column and route not in READ_ONLY_LOOP_ROUTES
    }


def test_pr_laz_32_the_line_manager_has_no_key_of_any_loop_route() -> None:
    keys = _loop_route_keys(loop_routers())
    assert len(keys) >= 15  # el catálogo del lazo está montado de verdad
    assert _line_manager_routes(keys) == {}
    # La excepción es exacta: una lectura, con la clave universal de jerarquía.
    for route in READ_ONLY_LOOP_ROUTES:
        assert route[0] == "GET"
        assert keys[route] == PermissionKey.HIERARCHY_READ.value
        assert set(Role) <= {
            role for role, column in MATRIX.items() if PermissionKey.HIERARCHY_READ in column
        }
    # Y ninguna clave que actúe sobre un hallazgo está en su columna (BR-LAZ-109).
    assert not FINDING_KEYS & MATRIX[Role.LINE_MANAGER]
    assert PermissionKey.COMMITMENTS_WRITE in MATRIX[Role.LINE_MANAGER]


@pytest.mark.parametrize(
    ("method", "key"),
    [
        ("POST", PermissionKey.COMMITMENTS_WRITE.value),
        ("GET", PermissionKey.REVIEW_PACKAGE_READ.value),
        ("GET", PermissionKey.HIERARCHY_READ.value),
    ],
)
def test_a_loop_route_with_a_key_of_the_line_manager_is_caught(method: str, key: str) -> None:
    probe = APIRouter()

    @probe.api_route("/loop/probe", methods=[method], dependencies=[requires(key)])
    async def handler() -> None:  # pragma: no cover - nunca se llama
        return None

    keys = _loop_route_keys((*loop_routers(), probe))
    assert _line_manager_routes(keys) == {(method, "/loop/probe"): key}


# --- 2. propiedad sobre la base ----------------------------------------------------------------


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[MonthlyReviewStack]:
    with monthly_review_stack(postgres_endpoint, "pr_laz_32") as built:
        yield built


def _expected_record(command: LineManagerCommand) -> tuple[int, str | None]:
    """El modelo del alta, en el orden del servicio: alcance, forma, responsable, validadores."""
    if command.target != "own":
        return 404, None
    if command.text == "short":
        return 400, None
    if command.responsible == "out_of_scope":
        return 400, "accreditation_commitment_out_of_scope"
    if command.text == "blocked":
        return 400, "loop_blocked_vocabulary_term"
    return 201, None


def _expected_update(command: LineManagerCommand) -> tuple[int, str | None]:
    if command.target != "own":
        return 404, None
    if command.note and command.state == "open":
        return 400, None
    return 200, None


@pytest.mark.integration
def test_pr_laz_32_no_finding_changes_and_only_own_commitments_do(
    stack: MonthlyReviewStack,
) -> None:
    world = stack.actions.world
    a, b = world.a, world.b
    zone = a.zone_id
    other_zone = stack.zone_in(a)

    # Hallazgos reales en las dos organizaciones; en A, uno con una acción vigente.
    coordinator = stack.member(a)
    assignee = stack.user(a)
    finding, action_id = stack.actions.assigned(coordinator, assignee, a)
    finding_state_id = stack.actions.finding_state_id(finding)
    stack.classified(b)
    sample_seed = stack.seed_sample(a, [stack.classified(a)])

    # Compromisos ajenos: otra zona de A y la zona de B.
    in_scope = stack.user(a, Role.LINE_MANAGER, ScopeLevel.ZONE, zone)
    out_of_scope = stack.user(a, Role.LINE_MANAGER, ScopeLevel.ZONE, other_zone)
    other_package = stack.seed_package(a, zone_id=other_zone)
    b_package = stack.seed_package(b)
    foreign: dict[str, uuid.UUID] = {}
    for name, package_id, who, responsible in (
        ("other_zone", other_package, coordinator, out_of_scope),
        ("other_organization", b_package, stack.member(b), stack.user(b)),
    ):
        response = stack.record_commitments(who, package_id, [stack.commitment(responsible)])
        assert response.status_code == 201, response.text
        foreign[name] = uuid.UUID(response.json()["commitments"][0]["commitment_id"])
    line_manager = stack.line_manager(a, zone)

    def loop_call(target: str, own_item: uuid.UUID) -> tuple[str, str, Any]:
        return {
            "classify": (
                "POST",
                f"/loop/findings/{finding_state_id}/classifications",
                {
                    "outcome": "false_positive",
                    "reason_category": "no_person_present",
                    "reason_text": "Revisión posterior: no había ninguna persona en el cruce",
                    "reinforced_reason": True,
                },
            ),
            "resolve_review": (
                "POST",
                f"/loop/review-queue/{uuid.uuid4()}/resolution",
                {
                    "outcome": "review_discarded",
                    "reason_category": "no_person_present",
                    "reason_text": "Revisión posterior: no había ninguna persona en el cruce",
                },
            ),
            "assign_action": (
                "POST",
                f"/loop/findings/{finding_state_id}/actions",
                stack.actions.action_body(assignee),
            ),
            "add_evidence": (
                "POST",
                f"/loop/actions/{action_id}/evidence",
                stack.actions.evidence_body(),
            ),
            "close": (
                "POST",
                f"/loop/findings/{finding_state_id}/closure",
                stack.actions.closure_body(),
            ),
            "reopen": (
                "POST",
                f"/loop/findings/{finding_state_id}/reopening",
                stack.actions.reopening_body(assignee),
            ),
            "start_root_cause": (
                "POST",
                f"/loop/findings/{finding_state_id}/root-cause-analyses",
                None,
            ),
            "sample_result": (
                "POST",
                f"/accreditation/sample-items/{own_item}/result",
                {"outcome": "criterion_error"},
            ),
        }[target]

    def frozen() -> tuple[Any, ...]:
        """Lo que el mando de línea nunca puede cambiar, en las dos organizaciones."""
        return tuple(
            (
                stack.classification_records(org.organization_id),
                stack.loop_rows(org.organization_id),
            )
            for org in (a, b)
        )

    @settings(
        max_examples=EXAMPLES,
        deadline=None,
        database=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(line_manager_commands())
    def run(commands: list[LineManagerCommand]) -> None:
        own_package = stack.seed_package(a, zone_id=zone)
        first = stack.record_commitments(line_manager, own_package, [stack.commitment(in_scope)])
        assert first.status_code == 201, first.text
        own_commitment = uuid.UUID(first.json()["commitments"][0]["commitment_id"])
        own_period = first.json()["commitments"][0]["period"]
        packages = {
            "own": own_package,
            "other_zone": other_package,
            "other_organization": b_package,
            "missing": uuid.uuid4(),
        }
        commitments = {"own": own_commitment, **foreign, "missing": uuid.uuid4()}

        def others() -> tuple[Any, ...]:
            return (
                [
                    row
                    for row in stack.commitment_rows(a.organization_id)
                    if row["package_id"] != own_package
                ],
                stack.commitment_rows(b.organization_id),
            )

        before, foreign_rows = frozen(), others()
        a_counts = stack.record_type_counts(a.organization_id)
        b_counts = stack.record_type_counts(b.organization_id)
        own_count = 1
        for command in commands:
            event(f"comando: {command.kind}/{command.target}")
            if command.kind == "record":
                text = {"valid": COMMITMENT_TEXT, "short": SHORT_TEXT, "blocked": BLOCKED_REASON}[
                    command.text
                ]
                responsible = in_scope if command.responsible == "in_scope" else out_of_scope
                entries = [stack.commitment(responsible, text=text) for _ in range(command.count)]
                response = stack.record_commitments(line_manager, packages[command.target], entries)
                status, code = _expected_record(command)
                if status == 201:
                    own_count += command.count
            elif command.kind == "update":
                body: dict[str, Any] = {"state": command.state}
                if command.note:
                    body["note"] = NOTE
                response = stack.update_commitment(line_manager, commitments[command.target], body)
                status, code = _expected_update(command)
            elif command.kind == "loop":
                method, path, body = loop_call(command.target, sample_seed.item_ids[0])
                response = stack.send(line_manager, method, path, json_body=body)
                status, code = 404, None  # el mando de línea no tiene la clave de la ruta
            else:
                if command.target == "commitments":
                    response = stack.send(
                        line_manager,
                        "GET",
                        "/accreditation/commitments",
                        params={"zone_id": str(zone), "period": own_period},
                    )
                else:
                    response = stack.send(
                        line_manager, "GET", f"/accreditation/monthly-packages/{own_package}/sample"
                    )
                status, code = 200, None
            assert response.status_code == status, (command, response.text)
            if status >= 400:
                assert response.json().get("detail_code") == code, (command, response.text)

            # Tras **cada** operación: ningún hallazgo cambia; nada ajeno cambia.
            assert frozen() == before, command
            assert others() == foreign_rows, command
            assert stack.record_type_counts(b.organization_id) == b_counts, command
            a_now = stack.record_type_counts(a.organization_id)
            changed = {
                name
                for name in a_now.keys() | a_counts.keys()
                if a_now.get(name, 0) != a_counts.get(name, 0)
            }
            assert changed <= {"commitments_recorded"}, (command, changed)

        own_rows = [
            row
            for row in stack.commitment_rows(a.organization_id)
            if row["package_id"] == own_package
        ]
        assert len(own_rows) == own_count

    run()
