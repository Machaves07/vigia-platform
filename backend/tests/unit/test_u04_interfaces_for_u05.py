"""Lo que U-05 consumirá de U-04, contra ``logical-components.md`` §6 de U-04 (TASK-335; hito «U-04
terminada»). Las notas fechadas de §6.1, §6.2 y §6.7 mandan sobre el texto de arriba; A-68 de la
adenda manda sobre §6.2.

Cada bloque copia aquí la tabla del diseño **tal como está escrita** y la compara con el código:

- **Rutas** (§6.3 a §6.10 y las dos notas de §6.7): las 41 operaciones ``SUP-U05-01`` a ``41`` en
  ``backend/openapi/app.yaml`` y servidas por los módulos ``loop``, ``accreditation`` y
  ``notifications``, con su clave de permiso, que es de la matriz de roles; ninguna ruta de U-04
  es pública (§6.1 y §6.11).
- **Alcance** (columna «Clave de permiso»): el alcance no se declara en la ruta, así que aquí se
  comprueba solo lo que se ve sin base. Con los parámetros de ruta y de consulta de ``app.yaml`` y
  el tipo de aislamiento de cada ruta en ``tests/isolation/laz_cases.py`` (``Kind.OWN``: no nombra
  recurso; ``Kind.RESOURCE``: nombra uno): «organización» no recibe planta ni zona y es ``OWN``;
  «propias» no recibe planta, zona ni usuario (el dueño es el actor de la sesión); «planta» recibe
  ``plant_id`` obligatorio; «zona o planta» admite los dos filtros; «zona» nunca se pide solo por
  planta (salvo ``SUP-U05-41``, cuya nota sirve una entrada por zona del alcance). Que el alcance
  de planta o de zona de la sesión no alcance el resto de la organización lo prueban, contra la
  base, ``test_pr_gob_12_a_plant_or_zone_scope_never_reaches_the_rest_of_its_organization`` y
  ``test_laz_own_routes_do_not_return_resources_outside_plant_or_zone_scope`` de
  ``tests/isolation/test_route_isolation.py`` (PR-GOB-12 y PR-LAZ-62).
- **Códigos** (domain-entities §1.2 y su nota de NFR-LAZ-19): los 44 ``detail_code``, cada uno con
  el prefijo del módulo que lo produce (pendiente nº 33, nota de §6.1). Sin diferencias.
- **Estados** (§6.2 y A-68): el mapa único ``HTTP_STATUS_BY_DETAIL_CODE`` (400 ``invalid_request``,
  los seis 409 y los cinco 503 de dependencia; ningún 422; ``metric_not_computable`` no es un
  error), el estado de éxito de cada ruta en ``app.yaml`` y, por ruta, los estados que implican
  sus ``detail_code`` declarados frente a la columna «Estados» leída con A-68 (422 → 400). Los
  404 (``not_found`` de U-02) y 429 (límite de tasa) no salen de un ``detail_code`` y no se
  comprueban aquí.

Toda diferencia entre el código y el diseño está en un ``*_DIFFERENCES`` (o en
``UNDOCUMENTED_STATUSES`` y ``STATUSES_WITHOUT_DETAIL_CODE``) con el texto del diseño, lo que hace
el código, el motivo y la **nota propuesta** para la sección de conciliación de U-05
(``plataforma-aplicacion/functional-design/frontend-components.md`` §6), y una prueba exige que
siga siendo una diferencia: si el código se alinea con el diseño, la entrada sobra y la prueba
falla hasta que se quita. Las notas las propone el PR de VIG-251 y las escribe la sesión de
control.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, NamedTuple

import pytest
import yaml

from tests.isolation.laz_cases import LAZ_CASES
from tests.isolation.route_cases import Kind
from vigia_platform.accreditation.detail_codes import AccreditationDetailCode
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.loop.detail_codes import (
    HTTP_STATUS_BY_DETAIL_CODE,
    NOT_AN_ERROR,
    U04_DETAIL_CODES,
    LoopDetailCode,
    design_name,
)
from vigia_platform.notifications.detail_codes import NotificationsDetailCode
from vigia_platform.shared.api.app import build_openapi_app
from vigia_platform.shared.api.declarations import (
    DeclaredRoute,
    UnauthenticatedRoute,
    iter_declared_routes,
)
from vigia_platform.shared.api.errors import HTTP_STATUS, ApiErrorCode

APP_YAML: Final = Path(__file__).resolve().parents[2] / "openapi" / "app.yaml"

U04_MODULES: Final = (
    "vigia_platform.loop.",
    "vigia_platform.accreditation.",
    "vigia_platform.notifications.",
)
U04_PREFIXES: Final = ("/loop", "/accreditation", "/notifications")


@dataclass(frozen=True)
class Difference:
    """Una diferencia entre el diseño y el código, con la nota propuesta para U-05."""

    design: str
    """Lo que dice el diseño, tal cual."""
    code: str
    """Lo que hace el código."""
    reason: str
    """Por qué el código es así (tarea o decisión que lo fija)."""
    note: str
    """Nota propuesta para ``frontend-components.md`` §6 (una frase)."""


# --- §6.3 a §6.10 Rutas -----------------------------------------------------------------------


class Operation(NamedTuple):
    """Una fila de las tablas de §6 (o de una nota fechada de §6.7)."""

    method: str
    path: str
    states: str
    """Columna «Estados» tal cual; vacía si la nota no la da."""
    permission: str
    scope: str


DOCUMENTED_OPERATIONS: Final[Mapping[str, Operation]] = {
    # §6.3 S-PLA-10 · Clasificación
    "SUP-U05-01": Operation("GET", "/loop/findings", "200", "findings.read", "zona"),
    "SUP-U05-02": Operation(
        "GET", "/loop/findings/{finding_state_id}", "200, 404, 503", "findings.read", "zona"
    ),
    "SUP-U05-03": Operation(
        "POST",
        "/loop/findings/{finding_state_id}/classifications",
        "201, 404, 409, 422",
        "findings.classify",
        "zona",
    ),
    "SUP-U05-04": Operation(
        "GET",
        "/loop/findings/{finding_state_id}/classifications",
        "200, 404",
        "findings.read",
        "zona",
    ),
    # §6.4 S-PLA-11 · Revisión
    "SUP-U05-05": Operation("GET", "/loop/review-queue", "200", "review_queue.resolve", "zona"),
    "SUP-U05-06": Operation(
        "POST",
        "/loop/review-queue/{detection_id}/resolution",
        "201, 404, 409, 422",
        "review_queue.resolve",
        "zona",
    ),
    # §6.5 S-PLA-12 · Cierre
    "SUP-U05-07": Operation(
        "POST",
        "/loop/findings/{finding_state_id}/actions",
        "201, 404, 422",
        "actions.manage",
        "zona",
    ),
    "SUP-U05-08": Operation(
        "POST", "/loop/actions/{action_id}/evidence", "201, 404, 422", "actions.manage", "zona"
    ),
    "SUP-U05-09": Operation(
        "POST",
        "/loop/actions/{action_id}/attachments/upload-grant",
        "201, 404, 422, 503",
        "actions.manage",
        "zona",
    ),
    "SUP-U05-10": Operation(
        "POST",
        "/loop/attachments/{attachment_id}/read-url",
        "200, 404, 503",
        "closure_attachments.read",
        "zona",
    ),
    "SUP-U05-11": Operation(
        "POST",
        "/loop/findings/{finding_state_id}/closure",
        "201, 404, 409, 422",
        "findings.close_and_sign",
        "zona",
    ),
    "SUP-U05-12": Operation(
        "POST",
        "/loop/findings/{finding_state_id}/unclosable",
        "201, 404, 422, 503",
        "findings.close_and_sign",
        "zona",
    ),
    "SUP-U05-13": Operation(
        "POST",
        "/loop/unclosable/{declaration_id}/reviews",
        "201, 404, 422",
        "findings.close_and_sign",
        "zona",
    ),
    "SUP-U05-14": Operation(
        "POST",
        "/loop/findings/{finding_state_id}/reopening",
        "201, 404, 409, 422",
        "actions.manage",
        "zona",
    ),
    "SUP-U05-15": Operation("GET", "/loop/overdue", "200", "executive_summary.read", "planta"),
    # §6.6 S-PLA-13 · Causa raíz y asistente
    "SUP-U05-16": Operation(
        "POST",
        "/loop/findings/{finding_state_id}/root-cause-analyses",
        "201, 404",
        "root_cause.analyze",
        "zona",
    ),
    "SUP-U05-17": Operation(
        "PUT",
        "/loop/root-cause-analyses/{analysis_id}/steps/{step_index}",
        "200, 404, 409, 422",
        "root_cause.analyze",
        "zona",
    ),
    "SUP-U05-18": Operation(
        "POST",
        "/loop/root-cause-analyses/{analysis_id}/signature",
        "201, 404, 422",
        "root_cause.analyze",
        "zona",
    ),
    "SUP-U05-19": Operation(
        "POST",
        "/loop/root-cause-analyses/{analysis_id}/assistant-requests",
        "202, 404, 429, 503",
        "root_cause.analyze",
        "zona",
    ),
    "SUP-U05-20": Operation(
        "GET",
        "/loop/assistant-requests/{request_id}",
        "200, 404, 422",
        "root_cause.analyze",
        "zona",
    ),
    # §6.7 S-PLA-14 · Exportación («zona (todas las pedidas)» en la 21)
    "SUP-U05-21": Operation(
        "POST", "/accreditation/exports", "202, 422, 429", "export.create", "zona"
    ),
    "SUP-U05-22": Operation(
        "GET", "/accreditation/exports/{export_id}", "200, 404", "export.create", "zona"
    ),
    "SUP-U05-23": Operation(
        "POST",
        "/accreditation/exports/{export_id}/download-url",
        "200, 404, 409, 503",
        "export.create",
        "zona",
    ),
    # §6.8 S-PLA-15 · Revisión mensual, métricas y exposición
    "SUP-U05-24": Operation(
        "GET", "/accreditation/monthly-packages", "200, 404, 409", "review_package.read", "zona"
    ),
    "SUP-U05-25": Operation(
        "GET",
        "/accreditation/monthly-packages/{package_id}/sample",
        "200, 404",
        "review_package.read",
        "zona",
    ),
    "SUP-U05-26": Operation(
        "POST",
        "/accreditation/sample-items/{sample_item_id}/result",
        "201, 404, 409, 422",
        "findings.classify",
        "zona",
    ),
    "SUP-U05-27": Operation(
        "POST",
        "/accreditation/monthly-packages/{package_id}/commitments",
        "201, 404, 422",
        "commitments.write",
        "zona",
    ),
    "SUP-U05-28": Operation(
        "PATCH",
        "/accreditation/commitments/{commitment_id}",
        "200, 404, 422",
        "commitments.write",
        "zona",
    ),
    "SUP-U05-29": Operation(
        "GET", "/accreditation/commitments", "200", "review_package.read", "zona"
    ),
    "SUP-U05-30": Operation(
        "GET", "/accreditation/metrics", "200", "metrics.read", "zona o planta"
    ),
    "SUP-U05-31": Operation("GET", "/accreditation/exposure", "200, 422", "exposure.read", "zona"),
    "SUP-U05-32": Operation(
        "GET", "/accreditation/adoption", "200", "metrics.read", "zona o planta"
    ),
    # §6.9 S-PLA-16 · Notificaciones y preferencias
    "SUP-U05-33": Operation("GET", "/notifications", "200", "notifications.read", "propias"),
    "SUP-U05-34": Operation(
        "POST",
        "/notifications/{notification_id}/read",
        "204, 404",
        "notifications.read",
        "propias",
    ),
    "SUP-U05-35": Operation(
        "PUT", "/notifications/preferences", "200, 422", "notifications.read", "propias"
    ),
    # §6.10 Administración del vocabulario y de la configuración
    "SUP-U05-36": Operation("GET", "/loop/vocabulary", "200", "vocabulary.manage", "organización"),
    "SUP-U05-37": Operation(
        "POST", "/loop/vocabulary/changes", "201, 409, 422", "vocabulary.manage", "organización"
    ),
    "SUP-U05-38": Operation(
        "GET", "/loop/plants/{plant_id}/configuration", "200, 404", "hierarchy.read", "planta"
    ),
    "SUP-U05-39": Operation(
        "PUT",
        "/loop/plants/{plant_id}/configuration",
        "200, 404, 422",
        "organization.settings",
        "planta",
    ),
    # Notas fechadas de §6.7 (2026-09-23, T-08, U04-H-03). La 40 no da estados; la 41 solo nombra
    # el 409 de ``monthly_package_not_available`` (paquete sin sellar).
    "SUP-U05-40": Operation(
        "GET", "/accreditation/exports/{export_id}/download", "", "export.create", "zona"
    ),
    "SUP-U05-41": Operation(
        "GET",
        "/accreditation/monthly-packages/{plant_id}/{period}/download",
        "409",
        "review_package.read",
        "zona",
    ),
}
"""Las 41 operaciones de §6 (39 de las tablas y las dos de las notas de §6.7), tal cual."""

PERMISSION_DIFFERENCES: Final[Mapping[str, Difference]] = {
    "SUP-U05-05": Difference(
        design="review_queue.resolve, zona",
        code="findings.read, zona",
        reason=(
            "TASK-309 lee la cola con la clave de la bandeja («igual que la bandeja», "
            "review_routes): la lista la ven el coordinador y el gerente de planta, y resolver "
            "(SUP-U05-06) sigue exigiendo review_queue.resolve, que solo tiene el coordinador"
        ),
        note=(
            "**Cola de revisión** (SCR-10): `SUP-U05-05` (`GET /loop/review-queue`) se autoriza "
            "con `findings.read`, zona, y no con `review_queue.resolve`; la pantalla muestra la "
            "cola a quien lee la bandeja y solo ofrece «Resolver» (`SUP-U05-06`) a quien tiene "
            "`review_queue.resolve`."
        ),
    ),
}

ROUTE_ADDITIONS: Final[Mapping[tuple[str, str], tuple[str, Difference]]] = {
    # (método, ruta): (operación cuya clave y alcance comparte, diferencia)
    ("GET", "/notifications/preferences"): (
        "SUP-U05-35",
        Difference(
            design="SUP-U05-35 solo con PUT /notifications/preferences",
            code="GET y PUT /notifications/preferences, misma clave y alcance",
            reason=(
                "TASK-348: la pantalla necesita leer las preferencias vigentes del propio actor "
                "para rellenar el formulario; PR-LAZ-62 la cuenta como segundo verbo de "
                "SUP-U05-35 (41 operaciones, 42 rutas)"
            ),
            note=(
                "**Preferencias** (SCR-15): además de `SUP-U05-35` (`PUT`), U-04 sirve "
                "`GET /notifications/preferences` (`notifications.read`, propias), que devuelve "
                "`email_enabled_normal`, `email_enabled_digest`, `weekly_digest_weekday` y las "
                "dos marcas de supresión para cargar el formulario."
            ),
        ),
    ),
}

SCOPES: Final = frozenset({"zona", "planta", "organización", "propias", "zona o planta"})

SCOPE_PARAMETERS: Final = frozenset({"plant_id", "zone_id", "user_id"})


def _app_yaml() -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(APP_YAML.read_text(encoding="utf-8"))
    return document


@pytest.fixture(scope="module")
def app_yaml() -> dict[str, Any]:
    return _app_yaml()


def _app_yaml_operations(document: Mapping[str, Any]) -> set[tuple[str, str]]:
    return {
        (method.upper(), path)
        for path, operations in document["paths"].items()
        for method in operations
        if method in {"get", "post", "put", "patch", "delete"}
    }


def _operation(document: Mapping[str, Any], method: str, path: str) -> Mapping[str, Any]:
    operation: Mapping[str, Any] = document["paths"][path][method.lower()]
    return operation


def _parameters(document: Mapping[str, Any], method: str, path: str) -> dict[str, bool]:
    """Parámetros de ruta y de consulta de la operación: nombre → obligatorio."""
    common = document["paths"][path].get("parameters", [])
    own = _operation(document, method, path).get("parameters", [])
    return {
        parameter["name"]: bool(parameter.get("required", False))
        for parameter in [*common, *own]
        if parameter.get("in") in {"path", "query"}
    }


def _u04_routes() -> dict[tuple[str, str], DeclaredRoute]:
    """Las rutas de la aplicación cuyo manejador vive en un módulo de U-04."""
    found: dict[tuple[str, str], DeclaredRoute] = {}
    for route in iter_declared_routes(build_openapi_app().routes):
        endpoint = getattr(route.route, "endpoint", None)
        if not route.is_api_route or endpoint is None:
            continue
        if not endpoint.__module__.startswith(U04_MODULES):
            continue
        for method in route.methods - {"HEAD"}:
            found[(method, route.path)] = route
    return found


@pytest.fixture(scope="module")
def u04_routes() -> dict[tuple[str, str], DeclaredRoute]:
    return _u04_routes()


def _key(operation_id: str) -> tuple[str, str]:
    operation = DOCUMENTED_OPERATIONS[operation_id]
    return (operation.method, operation.path)


def _expected_routes() -> set[tuple[str, str]]:
    return {_key(operation_id) for operation_id in DOCUMENTED_OPERATIONS} | set(ROUTE_ADDITIONS)


def _permission(route: DeclaredRoute) -> str | None:
    (permission,) = {d.permission for d in route.declarations}
    return permission


def test_the_41_documented_operations_are_in_app_yaml(app_yaml: dict[str, Any]) -> None:
    assert len(DOCUMENTED_OPERATIONS) == 41
    assert set(DOCUMENTED_OPERATIONS) == {f"SUP-U05-{n:02d}" for n in range(1, 42)}
    assert len({_key(operation_id) for operation_id in DOCUMENTED_OPERATIONS}) == 41
    u04_in_yaml = {key for key in _app_yaml_operations(app_yaml) if key[1].startswith(U04_PREFIXES)}
    assert sorted(_expected_routes() - u04_in_yaml) == []
    assert sorted(u04_in_yaml - _expected_routes()) == []


def test_u04_serves_exactly_the_documented_routes_and_the_listed_additions(
    u04_routes: dict[tuple[str, str], DeclaredRoute], app_yaml: dict[str, Any]
) -> None:
    assert sorted(set(u04_routes) - _expected_routes()) == []
    assert sorted(_expected_routes() - set(u04_routes)) == []
    assert all(path.startswith(U04_PREFIXES) for _, path in u04_routes)
    assert set(u04_routes) <= _app_yaml_operations(app_yaml)


@pytest.mark.parametrize("operation_id", sorted(DOCUMENTED_OPERATIONS))
def test_each_route_declares_the_documented_permission(
    operation_id: str, u04_routes: dict[tuple[str, str], DeclaredRoute]
) -> None:
    documented = DOCUMENTED_OPERATIONS[operation_id].permission
    expected = documented
    if operation_id in PERMISSION_DIFFERENCES:
        expected = PERMISSION_DIFFERENCES[operation_id].code.split(", ")[0]
    assert _permission(u04_routes[_key(operation_id)]) == expected


@pytest.mark.parametrize("key", sorted(ROUTE_ADDITIONS), ids=" ".join)
def test_each_addition_shares_the_permission_of_its_operation(
    key: tuple[str, str], u04_routes: dict[tuple[str, str], DeclaredRoute]
) -> None:
    operation_id, _ = ROUTE_ADDITIONS[key]
    assert _permission(u04_routes[key]) == _permission(u04_routes[_key(operation_id)])


@pytest.mark.parametrize("operation_id", sorted(PERMISSION_DIFFERENCES))
def test_each_listed_permission_difference_is_still_a_difference(operation_id: str) -> None:
    difference = PERMISSION_DIFFERENCES[operation_id]
    documented = DOCUMENTED_OPERATIONS[operation_id]
    assert difference.design == f"{documented.permission}, {documented.scope}"
    assert difference.code.split(", ")[0] != documented.permission


def test_each_listed_route_addition_is_still_an_addition() -> None:
    documented = {_key(operation_id) for operation_id in DOCUMENTED_OPERATIONS}
    assert not set(ROUTE_ADDITIONS) & documented
    for operation_id, _ in ROUTE_ADDITIONS.values():
        assert operation_id in DOCUMENTED_OPERATIONS


def test_no_u04_route_is_public_and_every_key_is_in_the_role_matrix(
    u04_routes: dict[tuple[str, str], DeclaredRoute],
) -> None:
    """§6.1 y §6.11: todas con sesión y clave de la matriz; ninguna en la lista pública cerrada."""
    keys = {permission.value for permission in PermissionKey}
    assert {operation.permission for operation in DOCUMENTED_OPERATIONS.values()} <= keys
    for key, route in u04_routes.items():
        assert route.declarations, key
        for declaration in route.declarations:
            assert declaration.permission in keys, key
            assert declaration.unauthenticated is None, key
            assert declaration.session is None, key
            assert declaration.node is None, key
    assert not any(public.path.startswith(U04_PREFIXES) for public in UnauthenticatedRoute)


# --- Alcance (columna «Clave de permiso») ----------------------------------------------------

_KINDS: Final[Mapping[tuple[str, str], Kind]] = {key: case.kind for key, case in LAZ_CASES}


@pytest.mark.parametrize("operation_id", sorted(DOCUMENTED_OPERATIONS))
def test_each_route_names_its_target_as_its_documented_scope(
    operation_id: str, app_yaml: dict[str, Any]
) -> None:
    operation = DOCUMENTED_OPERATIONS[operation_id]
    assert operation.scope in SCOPES
    parameters = _parameters(app_yaml, operation.method, operation.path)
    kind = _KINDS[_key(operation_id)]
    names_a_resource = "{" in operation.path
    if operation.scope == "organización":
        assert not SCOPE_PARAMETERS & set(parameters)
        assert kind is Kind.OWN
    elif operation.scope == "propias":
        assert not SCOPE_PARAMETERS & set(parameters)
        assert kind is (Kind.RESOURCE if names_a_resource else Kind.OWN)
    elif operation.scope == "planta":
        assert parameters.get("plant_id") is True
        assert kind is Kind.RESOURCE
    elif operation.scope == "zona o planta":
        assert parameters.get("plant_id") is False
        assert parameters.get("zone_id") is False
        assert kind is Kind.RESOURCE
    elif operation_id == "SUP-U05-41":
        # La nota de §6.7: «una entrada por cada zona de la planta, dentro del alcance».
        assert parameters == {"plant_id": True, "period": True}
        assert kind is Kind.RESOURCE
    else:
        assert operation.scope == "zona"
        assert "plant_id" not in parameters
        # Nombra el recurso (identificador, zona o cuerpo) o es la lista sin filtros que el
        # alcance de la sesión recorta.
        assert kind is Kind.RESOURCE or (
            kind is Kind.OWN
            and not parameters.keys()
            - {
                "cursor",
                "limit",
            }
        )


def test_the_scope_of_each_addition_is_the_one_of_its_operation(
    app_yaml: dict[str, Any],
) -> None:
    for (method, path), (operation_id, _) in ROUTE_ADDITIONS.items():
        assert DOCUMENTED_OPERATIONS[operation_id].scope == "propias"
        assert not SCOPE_PARAMETERS & set(_parameters(app_yaml, method, path))
        assert _KINDS[(method, path)] is Kind.OWN


# --- domain-entities §1.2 Códigos ------------------------------------------------------------

DOCUMENTED_DETAIL_CODES: Final[Mapping[str, str]] = {
    # nombre de §1.2, en su orden: módulo que lo produce (prefijo de la nota de §6.1)
    "reason_required": "loop",
    "reason_too_short": "loop",
    "blocked_vocabulary_term": "loop",
    "person_attribution_detected": "loop",
    "action_on_person_forbidden": "loop",
    "action_type_not_allowed": "loop",
    "deadline_out_of_range": "loop",
    "closure_evidence_missing": "loop",
    "signer_license_missing": "loop",
    "closure_requires_assigned_action": "loop",
    "unclosable_fields_incomplete": "loop",
    "unclosable_policy_not_signed": "loop",
    "unclosable_policy_unavailable": "loop",
    "unclosable_policy_revoked": "loop",
    "unclosable_review_date_out_of_range": "loop",
    "transition_not_allowed": "loop",
    "reopening_reason_required": "loop",
    "cause_category_not_allowed": "loop",
    "analysis_information_insufficient": "loop",
    "analysis_step_out_of_order": "loop",
    "assistant_unavailable": "loop",
    "assistant_output_rejected": "loop",
    "gate_state_unavailable": "loop",
    "zone_gate_not_approved": "loop",
    "standard_text_unavailable": "loop",
    "export_scope_too_large": "accreditation",
    "export_zones_span_plants": "accreditation",
    "export_zone_out_of_scope": "accreditation",
    "coverage_unavailable": "accreditation",
    "metric_not_computable": "accreditation",
    "exposure_not_computable": "accreditation",
    "sample_already_reviewed": "accreditation",
    "sample_cannot_modify_classification": "accreditation",
    "commitment_out_of_scope": "accreditation",
    "monthly_package_not_available": "accreditation",
    "vocabulary_change_reason_required": "loop",
    "vocabulary_term_duplicate": "loop",
    "attachment_type_not_allowed": "loop",
    "attachment_too_large": "loop",
    "attachment_hash_mismatch": "loop",
    "attachment_declaration_missing": "loop",
    "configuration_change_reason_required": "loop",
    "notification_preference_not_owned": "notifications",
    # nota fechada de §1.2 (2026-09-20, NFR-LAZ-19)
    "attachment_metadata_not_allowed": "loop",
}
"""Los 44 de ``loop_error_code``: los 43 de la tabla de §1.2 más el de la nota de NFR-LAZ-19."""

PREFIX_BY_ENUM: Final[Mapping[type[enum.StrEnum], str]] = {
    LoopDetailCode: "loop_",
    AccreditationDetailCode: "accreditation_",
    NotificationsDetailCode: "notifications_",
}


def test_the_detail_codes_are_exactly_the_44_documented_with_their_prefix() -> None:
    assert len(DOCUMENTED_DETAIL_CODES) == 44
    documented = {f"{module}_{name}" for name, module in DOCUMENTED_DETAIL_CODES.items()}
    codes = {code.value for code in U04_DETAIL_CODES}
    assert len(U04_DETAIL_CODES) == 44
    assert sorted(documented - codes) == []
    assert sorted(codes - documented) == []
    assert {design_name(code) for code in U04_DETAIL_CODES} == set(DOCUMENTED_DETAIL_CODES)


def test_every_code_has_the_prefix_of_its_module() -> None:
    for enum_type, prefix in PREFIX_BY_ENUM.items():
        for code in enum_type:
            assert code.value.startswith(prefix), code
    assert sum(len(enum_type) for enum_type in PREFIX_BY_ENUM) == 44


# --- §6.2 y A-68 Estados ---------------------------------------------------------------------

DOCUMENTED_CONFLICTS: Final = (
    # §6.2, fila 409, tal cual
    "transition_not_allowed",
    "analysis_step_out_of_order",
    "closure_requires_assigned_action",
    "sample_already_reviewed",
    "vocabulary_term_duplicate",
    "monthly_package_not_available",
)

DOCUMENTED_UNAVAILABLE: Final = (
    # §6.2, fila 503, tal cual; la nota de la sesión B separa ``temporarily_unavailable`` (la
    # plataforma no puede atender, sin detalle de negocio) de los cinco de dependencia caída
    "gate_state_unavailable",
    "unclosable_policy_unavailable",
    "coverage_unavailable",
    "standard_text_unavailable",
    "assistant_unavailable",
    "temporarily_unavailable",
)

NOT_AN_ERROR_DOCUMENTED: Final = "metric_not_computable"
"""§6.2: viaja dentro de una respuesta 200, por métrica."""

STATUS_MAP_DIFFERENCES: Final[Mapping[int, Difference]] = {
    422: Difference(
        design=(
            "422 «Regla de negocio incumplida»: el resto de los 44 códigos; 400 solo para el "
            "esquema estricto, sin código de negocio"
        ),
        code="400 invalid_request con su detail_code; ningún 422",
        reason=(
            "A-68 (dueño, 2026-10-14): ApiErrorCode de U-02 no tiene 422 y no se amplía; igual "
            "que U-02 y U-03 (A-56). TASK-302 lo fija en HTTP_STATUS_BY_DETAIL_CODE"
        ),
        note=(
            "**Estados HTTP de U-04** (LC-APP-15): donde `logical-components.md` §6 dice 422, "
            "U-04 responde 400 `invalid_request` con su `detail_code` (A-68); 404, 409 y 503 "
            "siguen como en §6.2."
        ),
    ),
}


def _by_design_name() -> dict[str, tuple[ApiErrorCode, int]]:
    return {design_name(code): status for code, status in HTTP_STATUS_BY_DETAIL_CODE.items()}


def test_metric_not_computable_is_not_an_error() -> None:
    assert {design_name(code) for code in NOT_AN_ERROR} == {NOT_AN_ERROR_DOCUMENTED}
    assert NOT_AN_ERROR_DOCUMENTED not in _by_design_name()
    assert set(HTTP_STATUS_BY_DETAIL_CODE) == set(U04_DETAIL_CODES) - NOT_AN_ERROR
    assert len(HTTP_STATUS_BY_DETAIL_CODE) == 43


def test_the_409_are_exactly_the_six_of_the_design() -> None:
    conflicts = {name for name, (_, status) in _by_design_name().items() if status == 409}
    assert conflicts == set(DOCUMENTED_CONFLICTS)
    for name in DOCUMENTED_CONFLICTS:
        assert _by_design_name()[name] == (ApiErrorCode.CONFLICT, 409)


def test_the_503_are_the_five_dependencies_and_temporarily_unavailable_is_a_u02_code() -> None:
    unavailable = {name for name, (_, status) in _by_design_name().items() if status == 503}
    dependencies = set(DOCUMENTED_UNAVAILABLE) - {"temporarily_unavailable"}
    assert unavailable == dependencies
    for name in dependencies:
        assert _by_design_name()[name] == (ApiErrorCode.TEMPORARILY_UNAVAILABLE, 503)
    assert "temporarily_unavailable" not in set(DOCUMENTED_DETAIL_CODES)
    assert HTTP_STATUS[ApiErrorCode.TEMPORARILY_UNAVAILABLE] == 503


def test_every_other_code_is_400_invalid_request_and_nothing_is_422() -> None:
    """A-68: el resto (la fila 422 de §6.2) viaja como 400 ``invalid_request``."""
    rest = set(_by_design_name()) - set(DOCUMENTED_CONFLICTS) - set(DOCUMENTED_UNAVAILABLE)
    assert len(rest) == 44 - 1 - 6 - 5
    for name in rest:
        assert _by_design_name()[name] == (ApiErrorCode.INVALID_REQUEST, 400), name
    assert {status for _, status in HTTP_STATUS_BY_DETAIL_CODE.values()} == {400, 409, 503}
    assert 422 not in HTTP_STATUS.values()


@pytest.mark.parametrize("status", sorted(STATUS_MAP_DIFFERENCES))
def test_each_listed_status_map_difference_is_still_a_difference(status: int) -> None:
    assert status not in {code for _, code in HTTP_STATUS_BY_DETAIL_CODE.values()}
    assert status not in HTTP_STATUS.values()


# --- Estados por ruta ------------------------------------------------------------------------

UNDOCUMENTED_STATUSES: Final[Mapping[str, tuple[frozenset[str], Difference]]] = {
    # operación: (``detail_code`` declarados cuyo estado no está en su columna, diferencia)
    "SUP-U05-07": (
        frozenset({"loop_transition_not_allowed"}),
        Difference(
            design="201, 404, 422",
            code="también 409 conflict con loop_transition_not_allowed",
            reason=(
                "asignar una acción es una transición de la máquina de estados del hallazgo "
                "(TASK-305 y 311); pedirla en un estado que no la admite es la fila 409 de §6.2"
            ),
            note=(
                "`SUP-U05-07` también responde 409 `conflict` con `loop_transition_not_allowed` "
                "cuando el estado del hallazgo no admite asignar la acción."
            ),
        ),
    ),
    "SUP-U05-08": (
        frozenset({"loop_transition_not_allowed"}),
        Difference(
            design="201, 404, 422",
            code="también 409 conflict con loop_transition_not_allowed",
            reason=(
                "añadir evidencia exige la acción en un estado que la admite (TASK-311); si no, "
                "es la transición no permitida de la fila 409 de §6.2"
            ),
            note=(
                "`SUP-U05-08` también responde 409 `conflict` con `loop_transition_not_allowed` "
                "cuando la acción ya no admite evidencia."
            ),
        ),
    ),
    "SUP-U05-12": (
        frozenset({"loop_transition_not_allowed"}),
        Difference(
            design="201, 404, 422, 503",
            code="también 409 conflict con loop_transition_not_allowed",
            reason=(
                "declarar incerrable es una transición de la máquina de estados (TASK-313); "
                "desde un estado que no la admite es la fila 409 de §6.2"
            ),
            note=(
                "`SUP-U05-12` también responde 409 `conflict` con `loop_transition_not_allowed` "
                "cuando el hallazgo no puede declararse incerrable en su estado."
            ),
        ),
    ),
    "SUP-U05-13": (
        frozenset({"loop_transition_not_allowed"}),
        Difference(
            design="201, 404, 422",
            code="también 409 conflict con loop_transition_not_allowed",
            reason=(
                "revisar un incerrable mueve el hallazgo (renovar o reabrir, TASK-313); sobre "
                "una declaración que ya no lo admite es la fila 409 de §6.2"
            ),
            note=(
                "`SUP-U05-13` también responde 409 `conflict` con `loop_transition_not_allowed` "
                "cuando la declaración ya no admite revisión."
            ),
        ),
    ),
    "SUP-U05-16": (
        frozenset({"loop_standard_text_unavailable"}),
        Difference(
            design="201, 404",
            code="también 503 temporarily_unavailable con loop_standard_text_unavailable",
            reason=(
                "iniciar el análisis cita el texto del estándar del catálogo de U-03; sin "
                "respuesta del puerto, fallo cerrado de la fila 503 de §6.2 (root_cause_routes)"
            ),
            note=(
                "`SUP-U05-16` también responde 503 con `loop_standard_text_unavailable` "
                "(dependencia caída: degrada la sección, sin abrir el cortacircuito de interfaz)."
            ),
        ),
    ),
    "SUP-U05-18": (
        frozenset({"loop_transition_not_allowed"}),
        Difference(
            design="201, 404, 422",
            code="también 409 conflict con loop_transition_not_allowed",
            reason=(
                "firmar un análisis ya firmado es la transición no permitida (root_cause_routes)"
            ),
            note=(
                "`SUP-U05-18` también responde 409 `conflict` con `loop_transition_not_allowed` "
                "cuando el análisis ya está firmado."
            ),
        ),
    ),
    "SUP-U05-27": (
        frozenset({"accreditation_monthly_package_not_available"}),
        Difference(
            design="201, 404, 422",
            code="también 409 conflict con accreditation_monthly_package_not_available",
            reason=(
                "solo se registran compromisos sobre un paquete sellado (monthly_routes); el "
                "paquete sin sellar es la fila 409 de §6.2"
            ),
            note=(
                "`SUP-U05-27` también responde 409 `conflict` con "
                "`accreditation_monthly_package_not_available` si el paquete aún no está sellado."
            ),
        ),
    ),
    "SUP-U05-31": (
        frozenset({"accreditation_coverage_unavailable", "loop_gate_state_unavailable"}),
        Difference(
            design="200, 422",
            code=(
                "también 503 temporarily_unavailable con accreditation_coverage_unavailable o "
                "loop_gate_state_unavailable"
            ),
            reason=(
                "el indicador necesita la cobertura de U-02 y el estado de compuertas de U-03; "
                "sin ellas, fallo cerrado de la fila 503 de §6.2 (exposure_routes)"
            ),
            note=(
                "`SUP-U05-31` también responde 503 con `accreditation_coverage_unavailable` o "
                "`loop_gate_state_unavailable` (dependencia caída: degrada la sección)."
            ),
        ),
    ),
}

STATUSES_WITHOUT_DETAIL_CODE: Final[Mapping[str, tuple[frozenset[int], Difference]]] = {
    # operación: (estados de su columna, leídos con A-68, que ningún ``detail_code`` declarado
    # produce, diferencia): los produce un ``code`` de U-02 sin ``detail_code``
    "SUP-U05-09": (
        frozenset({503}),
        Difference(
            design="503",
            code="503 storage_unavailable de U-02, sin detail_code",
            reason=(
                "la concesión de subida firma contra el almacén; caído, storage_unavailable "
                "(attachment_routes), no un código de U-04"
            ),
            note=(
                "El 503 de `SUP-U05-09`, `10` y `23` es `storage_unavailable` de U-02, sin "
                "`detail_code`: degrada la sección de adjuntos o de descarga y no abre el "
                "cortacircuito de interfaz, que solo abre `temporarily_unavailable`."
            ),
        ),
    ),
    "SUP-U05-10": (
        frozenset({503}),
        Difference(
            design="503",
            code="503 storage_unavailable de U-02, sin detail_code",
            reason="la URL de lectura se firma contra el almacén (attachment_routes)",
            note="Misma nota que `SUP-U05-09`.",
        ),
    ),
    "SUP-U05-23": (
        frozenset({409, 503}),
        Difference(
            design="409, 503",
            code=(
                "409 conflict sin detail_code, con export_state en el cuerpo; 503 "
                "storage_unavailable de U-02"
            ),
            reason=(
                "solo se descarga una exportación ready; si no, 409 con el estado que se muestra "
                "y sin inventar un detail_code (export_routes, TASK-317 y 345); el almacén caído "
                "es storage_unavailable"
            ),
            note=(
                "El 409 de `SUP-U05-23` y `SUP-U05-40` es `conflict` sin `detail_code` y trae "
                "`export_state` en el cuerpo (`expired` si la descarga venció)."
            ),
        ),
    ),
    "SUP-U05-35": (
        frozenset({400}),
        Difference(
            design="422 (400 con A-68)",
            code="400 invalid_request solo por el esquema, sin detail_code",
            reason=(
                "la ruta no lleva usuario: siempre actúa sobre el actor de la sesión, así que "
                "notifications_notification_preference_not_owned no puede salir por HTTP "
                "(TASK-348; la guarda ensure_owned queda en el servicio)"
            ),
            note=(
                "`SUP-U05-35` nunca devuelve `notifications_notification_preference_not_owned`: "
                "la ruta no lleva usuario y su único 400 es el de esquema."
            ),
        ),
    ),
}

_DETAIL_ERROR_STATUSES: Final = frozenset({400, 409, 503})


def _documented_states(operation_id: str) -> set[int]:
    """La columna «Estados» leída con A-68: 422 es 400 ``invalid_request``."""
    text = DOCUMENTED_OPERATIONS[operation_id].states
    states = {int(state) for state in text.split(", ")} if text else set()
    return {400 if state == 422 else state for state in states}


def _status_of(detail_code: str) -> int:
    (status,) = {
        status for code, (_, status) in HTTP_STATUS_BY_DETAIL_CODE.items() if code == detail_code
    }
    return status


def _declared_errors(route: DeclaredRoute) -> set[str]:
    """Los ``detail_code`` declarados que viajan como error (sin ``metric_not_computable``)."""
    declared = {code for d in route.declarations for code in d.detail_codes}
    return declared - {code.value for code in NOT_AN_ERROR}


def test_every_declared_detail_code_is_a_u04_code(
    u04_routes: dict[tuple[str, str], DeclaredRoute],
) -> None:
    codes = {code.value for code in U04_DETAIL_CODES}
    for key, route in u04_routes.items():
        declared = {code for d in route.declarations for code in d.detail_codes}
        assert sorted(declared - codes) == [], key


@pytest.mark.parametrize("operation_id", sorted(DOCUMENTED_OPERATIONS))
def test_each_route_declares_only_codes_whose_status_is_documented(
    operation_id: str, u04_routes: dict[tuple[str, str], DeclaredRoute]
) -> None:
    declared = _declared_errors(u04_routes[_key(operation_id)])
    documented = _documented_states(operation_id)
    undocumented = {code for code in declared if _status_of(code) not in documented}
    expected = UNDOCUMENTED_STATUSES.get(operation_id, (frozenset(), None))[0]
    assert undocumented == expected


@pytest.mark.parametrize("operation_id", sorted(DOCUMENTED_OPERATIONS))
def test_each_documented_error_status_comes_from_a_declared_code_or_is_listed(
    operation_id: str, u04_routes: dict[tuple[str, str], DeclaredRoute]
) -> None:
    declared = _declared_errors(u04_routes[_key(operation_id)])
    produced = {_status_of(code) for code in declared}
    missing = (_documented_states(operation_id) & _DETAIL_ERROR_STATUSES) - produced
    assert missing == set(STATUSES_WITHOUT_DETAIL_CODE.get(operation_id, (frozenset(), None))[0])


def _success_states(operation_id: str) -> set[int]:
    return {state for state in _documented_states(operation_id) if 200 <= state < 300}


@pytest.mark.parametrize(
    # Las notas de §6.7 no dan el estado de éxito de ``SUP-U05-40`` ni de ``41``.
    "operation_id",
    sorted(operation_id for operation_id in DOCUMENTED_OPERATIONS if _success_states(operation_id)),
)
def test_each_route_answers_the_documented_success_status(
    operation_id: str, app_yaml: dict[str, Any]
) -> None:
    success = _success_states(operation_id)
    assert len(success) == 1
    operation = DOCUMENTED_OPERATIONS[operation_id]
    responses = _operation(app_yaml, operation.method, operation.path)["responses"]
    assert {int(code) for code in responses if code.startswith("2")} == success


@pytest.mark.parametrize("operation_id", sorted(UNDOCUMENTED_STATUSES))
def test_each_listed_undocumented_status_is_still_undocumented(
    operation_id: str, u04_routes: dict[tuple[str, str], DeclaredRoute]
) -> None:
    codes, difference = UNDOCUMENTED_STATUSES[operation_id]
    assert codes
    assert codes <= _declared_errors(u04_routes[_key(operation_id)])
    assert not {_status_of(code) for code in codes} & _documented_states(operation_id)
    assert difference.design == DOCUMENTED_OPERATIONS[operation_id].states


@pytest.mark.parametrize("operation_id", sorted(STATUSES_WITHOUT_DETAIL_CODE))
def test_each_listed_status_without_detail_code_is_still_without_one(
    operation_id: str,
    u04_routes: dict[tuple[str, str], DeclaredRoute],
    app_yaml: dict[str, Any],
) -> None:
    statuses, _ = STATUSES_WITHOUT_DETAIL_CODE[operation_id]
    assert statuses
    assert statuses <= _documented_states(operation_id)
    declared = _declared_errors(u04_routes[_key(operation_id)])
    assert not statuses & {_status_of(code) for code in declared}
    if 409 in statuses:
        # El 409 sin ``detail_code`` sí está en la OpenAPI de la que U-05 genera su cliente.
        operation = DOCUMENTED_OPERATIONS[operation_id]
        assert "409" in _operation(app_yaml, operation.method, operation.path)["responses"]


def test_every_difference_has_its_reason_and_proposed_note() -> None:
    differences: list[Difference] = [
        *PERMISSION_DIFFERENCES.values(),
        *(difference for _, difference in ROUTE_ADDITIONS.values()),
        *STATUS_MAP_DIFFERENCES.values(),
        *(difference for _, difference in UNDOCUMENTED_STATUSES.values()),
        *(difference for _, difference in STATUSES_WITHOUT_DETAIL_CODE.values()),
    ]
    for difference in differences:
        assert difference.design and difference.code, difference
        assert difference.reason and difference.note, difference
