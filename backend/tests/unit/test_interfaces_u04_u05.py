"""Lo que U-04 y U-05 consumen de U-03, contra ``interfaces-para-u04-u05.md`` (TASK-237; hito «U-03
terminada»). Manda la sección «Versión 1.5» sobre §1 a §4; A-54, A-55 y A-61 de la adenda mandan
sobre ambas.

Cada bloque copia aquí la tabla de la interfaz **tal como está escrita** y la compara con el código:

- **Puertos** (§1): las 19 operaciones de ``CatalogQueryPort``, ``GateQueryPort`` y
  ``FleetQueryPort``, sus parámetros y los de sus implementaciones, y los topes en el borde
  (``single_occupancy_many`` ≤ 50, ``standards_at_many`` ≤ 200, ``gate_history`` y
  ``assignment_history`` ≤ 366 días): en el tope se consulta la base, uno más es un error sin
  consultarla, nunca un resultado truncado.
- **Eventos** (§2 y v1.5): los 15 nombres, el campo de cada carga (obligatorio u opcional, unión de
  §2 y v1.5) y sus listas cerradas; los campos comunes van en el sobre del evento.
- **Rutas** (§3 y v1.5): las de ``backend/openapi/app.yaml`` que sirven los módulos de U-03, con su
  clave de permiso y los ``detail_code`` que §3 nombra; sin la ruta ``diff`` del catálogo (A-54);
  las diez rutas del contrato bajo ``/api/nodes`` y ninguna de compuertas.
- **Errores** (§4 y pendiente nº 33): los ``detail_code`` con prefijo ``catalog_`` y ``fleet_``.

Toda diferencia entre el código y el texto de la interfaz está en un ``*_DIFFERENCES`` con su motivo
y una prueba exige que siga siendo necesaria: si el código se alinea con el texto, la entrada sobra
y la prueba falla hasta que se quita. Las diferencias son intencionadas; el texto de la nota fechada
para el §7 «Historial» lo propone el PR de VIG-173 y lo escribe la sesión de control.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, get_args

import pytest
import yaml

from tests.factories import make_context
from vigia_platform.catalog.adapters.postgres.catalog_query import PostgresCatalogQuery
from vigia_platform.catalog.adapters.postgres.gate_query import PostgresGateQuery
from vigia_platform.catalog.detail_codes import CatalogDetailCode
from vigia_platform.catalog.domain.enums import GateKind, RegressionCause
from vigia_platform.catalog.domain.ports import (
    MAX_GATE_HISTORY_RANGE,
    MAX_SINGLE_OCCUPANCY_ZONES,
    MAX_STANDARD_REFS,
    CatalogQueryPort,
    GateQueryPort,
    PortLimitExceeded,
    StandardRef,
)
from vigia_platform.catalog.events import CATALOG_EVENT_TYPES
from vigia_platform.fleet.application.inventory_read import FleetInventory
from vigia_platform.fleet.detail_codes import FleetDetailCode
from vigia_platform.fleet.domain.enums import FleetAlarmKind, UpdateResult
from vigia_platform.fleet.events import FLEET_EVENT_TYPES, ReviewReason
from vigia_platform.fleet.ports import MAX_ASSIGNMENT_RANGE, FleetQueryPort, FleetRangeTooLong
from vigia_platform.shared.api.app import build_openapi_app
from vigia_platform.shared.api.declarations import (
    NODE_PREFIX,
    DeclaredRoute,
    NodeRoute,
    iter_declared_routes,
)
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.outbox.publish import OutboxEvent

APP_YAML: Final = Path(__file__).resolve().parents[2] / "openapi" / "app.yaml"

# --- §1 Puertos --------------------------------------------------------------------------------

DOCUMENTED_PORTS: Final[Mapping[type, Mapping[str, str]]] = {
    # §1.1, con las operaciones por lote de v1.1; «?» = opcional.
    CatalogQueryPort: {
        "current_catalog": "context, zone_id",
        "catalog_at": "context, zone_id, at",
        "standard_version": "context, zone_id, standard_id, version",
        "standard_at": "context, zone_id, standard_id, at",
        "catalog_history": "context, zone_id, cursor?",
        "single_occupancy": "context, zone_id, at?",
        "regression_state": "context, zone_id",
        "single_occupancy_many": "context, zone_ids, at?",
        "standards_at_many": "context, refs",
    },
    # §1.2
    GateQueryPort: {
        "state": "context, zone_id",
        "state_at": "context, zone_id, gate, at",
        "states_by_plant": "context, plant_id",
        "gate_history": "context, zone_id, from, to",
        "plant_policy": "context, plant_id",
        "current_agreement": "context, zone_id",
    },
    # §1.3
    FleetQueryPort: {
        "nodes_by_zone": "context, zone_id",
        "node": "context, node_id",
        "assignment_at": "context, zone_id, at",
        "assignment_history": "context, zone_id, from, to",
    },
}
"""Las 19 operaciones de §1, con los parámetros tal como los escribe la interfaz."""

PORT_DIFFERENCES: Final[Mapping[str, tuple[str, str]]] = {
    # operación: (firma del código, motivo)
    "gate_history": (
        "context, zone_id, start, end",
        "«from» es palabra reservada de Python: el rango se llama start y end (mismo orden y "
        "sentido; los mensajes de error siguen diciendo from y to)",
    ),
    "assignment_history": (
        "context, zone_id, start, end",
        "«from» es palabra reservada de Python: el rango se llama start y end",
    ),
    "catalog_history": (
        "context, zone_id, cursor?, limit?",
        "página opcional de 1 a 200 (convención de paginación del encabezado de la interfaz); "
        "por omisión, 200",
    ),
}

IMPLEMENTATIONS: Final[Mapping[type, type]] = {
    CatalogQueryPort: PostgresCatalogQuery,
    GateQueryPort: PostgresGateQuery,
    FleetQueryPort: FleetInventory,
}


def _signature(function: Callable[..., Any]) -> str:
    parameters = list(inspect.signature(function).parameters.values())[1:]  # sin self
    return ", ".join(
        p.name + ("?" if p.default is not inspect.Parameter.empty else "") for p in parameters
    )


def _port_operations(port: type) -> dict[str, str]:
    return {
        name: _signature(member)
        for name, member in vars(port).items()
        if not name.startswith("_") and inspect.iscoroutinefunction(member)
    }


def test_the_three_ports_expose_the_19_documented_operations() -> None:
    assert sum(len(operations) for operations in DOCUMENTED_PORTS.values()) == 19
    for port, operations in DOCUMENTED_PORTS.items():
        assert set(_port_operations(port)) == set(operations), port.__name__


@pytest.mark.parametrize(
    ("port", "operation"),
    [(port, name) for port, operations in DOCUMENTED_PORTS.items() for name in operations],
    ids=lambda value: getattr(value, "__name__", value),
)
def test_each_operation_has_the_documented_parameters(port: type, operation: str) -> None:
    expected = DOCUMENTED_PORTS[port][operation]
    if operation in PORT_DIFFERENCES:
        expected = PORT_DIFFERENCES[operation][0]
    assert _port_operations(port)[operation] == expected


@pytest.mark.parametrize("operation", sorted(PORT_DIFFERENCES))
def test_each_listed_port_difference_is_still_a_difference(operation: str) -> None:
    documented = next(ops[operation] for ops in DOCUMENTED_PORTS.values() if operation in ops)
    assert PORT_DIFFERENCES[operation][0] != documented


@pytest.mark.parametrize("port", list(IMPLEMENTATIONS), ids=lambda port: port.__name__)
def test_the_implementation_has_the_signature_of_its_port(port: type) -> None:
    implementation = IMPLEMENTATIONS[port]
    for name, signature in _port_operations(port).items():
        assert _signature(getattr(implementation, name)) == signature, f"{port.__name__}.{name}"


def test_the_caps_are_the_documented_ones() -> None:
    assert MAX_SINGLE_OCCUPANCY_ZONES == 50
    assert MAX_STANDARD_REFS == 200
    assert timedelta(days=366) == MAX_GATE_HISTORY_RANGE
    assert timedelta(days=366) == MAX_ASSIGNMENT_RANGE


class _Reached(Exception):
    """La operación pasó sus comprobaciones y fue a la base."""


class _Database:
    """Base que anota cada acceso y no responde: llegar a ella es haber superado el tope."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Callable[..., Any]:
        async def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            raise _Reached(name)

        return call


_START: Final = datetime(2026, 1, 1, tzinfo=UTC)
_UNUSED: Final[Any] = object()


async def _single_occupancy_many(database: _Database, count: int) -> Any:
    port = PostgresCatalogQuery(database)
    zones = [uuid.uuid4() for _ in range(count)]
    return await port.single_occupancy_many(make_context(), zones)


async def _standards_at_many(database: _Database, count: int) -> Any:
    port = PostgresCatalogQuery(database)
    refs = [StandardRef(uuid.uuid4(), uuid.uuid4(), _START) for _ in range(count)]
    return await port.standards_at_many(make_context(), refs)


async def _gate_history(database: _Database, span: timedelta) -> Any:
    port = PostgresGateQuery(database)
    return await port.gate_history(make_context(), uuid.uuid4(), _START, _START + span)


async def _assignment_history(database: _Database, span: timedelta) -> Any:
    port = FleetInventory(
        database=database,
        authorizer=_UNUSED,
        audit=_UNUSED,
        clock=_UNUSED,
        provider_organization_id=uuid.uuid4(),
    )
    return await port.assignment_history(make_context(), uuid.uuid4(), _START, _START + span)


_DAY: Final = timedelta(days=1)
_TICK: Final = timedelta(microseconds=1)

CAPS: Final = [
    # (operación, llamada, en el tope, uno más, error)
    ("single_occupancy_many", _single_occupancy_many, 50, 51, PortLimitExceeded),
    ("standards_at_many", _standards_at_many, 200, 201, PortLimitExceeded),
    ("gate_history", _gate_history, 366 * _DAY, 366 * _DAY + _TICK, PortLimitExceeded),
    ("assignment_history", _assignment_history, 366 * _DAY, 366 * _DAY + _TICK, FleetRangeTooLong),
]


@pytest.mark.parametrize(
    ("call", "at_cap", "error"),
    [(call, at_cap, error) for _, call, at_cap, _, error in CAPS],
    ids=[name for name, *_ in CAPS],
)
@pytest.mark.asyncio
async def test_at_the_cap_the_operation_reaches_the_database(
    call: Callable[[_Database, Any], Any], at_cap: Any, error: type[Exception]
) -> None:
    database = _Database()
    with pytest.raises(_Reached):
        await call(database, at_cap)
    assert database.calls


@pytest.mark.parametrize(
    ("call", "over_cap", "error"),
    [(call, over, error) for _, call, _, over, error in CAPS],
    ids=[name for name, *_ in CAPS],
)
@pytest.mark.asyncio
async def test_one_over_the_cap_is_an_error_without_querying(
    call: Callable[[_Database, Any], Any], over_cap: Any, error: type[Exception]
) -> None:
    database = _Database()
    with pytest.raises(error):
        await call(database, over_cap)
    assert database.calls == []


# --- §2 Eventos --------------------------------------------------------------------------------

DOCUMENTED_EVENTS: Final[Mapping[str, str]] = {
    # §2 con la unión de v1.5; «?» = opcional.
    "finding_received": (
        "zone_id, node_id, finding_id, platform_record_id, received_at, family, tier, standard"
    ),
    "detection_for_review_received": (
        "zone_id, node_id, detection_id, platform_record_id, received_at, review_reason, "
        "idempotency_key"
    ),
    "observability_event_received": (
        "zone_id, node_id, event_id_node, platform_record_id, subject_kind, state, phase"
    ),
    "gate_state_changed": "zone_id, gate, status, resulting_mode, record_id, reason_es_present",
    "zone_activated": "zone_id, activated_at, agreement_id, commissioning_record_id",
    "catalog_updated": "zone_id, catalog_version, changed_fields",
    "regression_marked": "zone_id, cause, catalog_version?, model_version?, affected_row_ids",
    "regression_cleared": (
        "zone_id, cause, catalog_version?, model_version?, affected_row_ids, cleared_by_session_id"
    ),
    "node_enrolled": "node_id, plant_id, zone_ids, replaces_node_id?",
    "node_revoked": "node_id, plant_id, zone_ids, replaces_node_id?",
    "node_decommissioned": "node_id, plant_id, zone_ids, replaces_node_id?",
    "target_version_published": "node_id, target_version, result?",
    "update_result_received": "node_id, target_version, result?",
    "fleet_alarm_raised": "node_id?, zone_id?, alarm_kind, since, alarm_id",
    "fleet_alarm_cleared": "node_id?, zone_id?, alarm_kind, since, alarm_id, cleared_at",
}
"""Los 15 eventos de §2 con los campos de v1.5 (``event_id_node``, ``cleared_by_session_id``,
``alarm_id``, ``since`` y ``cleared_at``)."""

EVENT_DIFFERENCES: Final[Mapping[str, tuple[str, str]]] = {
    "target_version_published": (
        "node_id, target_version",
        "la fila de §2 es compartida: «result?» es del resultado de la actualización, que aún no "
        "existe al publicar la versión objetivo (fleet.events)",
    ),
    "update_result_received": (
        "node_id, target_version, result",
        "el evento solo se publica cuando el nodo informa un resultado: «result» siempre está "
        "(applied, reverted o failed, v1.5)",
    ),
}

ENVELOPE_FIELDS: Final = {
    # campo común de §2: campo del sobre persistido (``OutboxEvent``)
    "organization_id": "organization_id",
    "plant_id": "plant_id",
    "event_id": "event_id",
    "occurred_at": "created_at",
    "correlation_id": "correlation_id",
}
"""Los campos comunes van en el sobre, no en la carga; ``occurred_at`` es ``created_at``, la marca
de la bandeja de U-02 (domain-entities §4.3 de U-02)."""

EVENT_TYPES: Final = {event.event_name: event for event in CATALOG_EVENT_TYPES + FLEET_EVENT_TYPES}


def _fields(event_name: str) -> set[str]:
    model = EVENT_TYPES[event_name].payload_model
    return {name + ("" if f.is_required() else "?") for name, f in model.model_fields.items()}


def _split(text: str) -> set[str]:
    return {item.strip() for item in text.split(",")}


def test_u03_publishes_exactly_the_15_documented_events() -> None:
    assert len(CATALOG_EVENT_TYPES) + len(FLEET_EVENT_TYPES) == 15
    assert set(EVENT_TYPES) == set(DOCUMENTED_EVENTS)
    assert {event.publisher_unit for event in EVENT_TYPES.values()} == {ActorUnit.U03}


@pytest.mark.parametrize("event_name", sorted(DOCUMENTED_EVENTS))
def test_each_payload_has_the_documented_fields(event_name: str) -> None:
    expected = DOCUMENTED_EVENTS[event_name]
    if event_name in EVENT_DIFFERENCES:
        expected = EVENT_DIFFERENCES[event_name][0]
    assert _fields(event_name) == _split(expected)


@pytest.mark.parametrize("event_name", sorted(EVENT_DIFFERENCES))
def test_each_listed_event_difference_is_still_a_difference(event_name: str) -> None:
    assert _split(EVENT_DIFFERENCES[event_name][0]) != _split(DOCUMENTED_EVENTS[event_name])


def test_the_common_fields_travel_in_the_envelope_not_in_the_payload() -> None:
    envelope = set(OutboxEvent.__dataclass_fields__)
    assert set(ENVELOPE_FIELDS.values()) <= envelope
    for event_name in EVENT_TYPES:
        assert not {f.rstrip("?") for f in _fields(event_name)} & set(ENVELOPE_FIELDS) - {
            "plant_id"  # el ciclo de vida del nodo la repite en la carga (§2)
        }, event_name


def test_the_closed_lists_of_the_payloads_are_the_documented_ones() -> None:
    assert {kind.value for kind in GateKind} == {"mounting", "usage"}
    # §1.1 y respuesta 13, más ``framing_recaptured`` de la precisión (c) de v1.5
    assert {cause.value for cause in RegressionCause} == {
        "catalog_change",
        "model_version_change",
        "framing_recaptured",
    }
    assert set(get_args(ReviewReason)) == {
        "low_confidence",
        "ambiguous",
        "no_bounding_box",
        "other",
    }
    assert {result.value for result in UpdateResult} == {"applied", "reverted", "failed"}
    # §2: las ocho clases (``orphan_clips_growing`` de v1.5)
    assert {kind.value for kind in FleetAlarmKind} == {
        "node_mute",
        "queue_over_threshold",
        "clock_drift",
        "version_retiring",
        "simulated_adapter_in_productive",
        "certificate_expiring",
        "camera_below_min_fps",
        "orphan_clips_growing",
    }


# --- §3 Rutas ----------------------------------------------------------------------------------

DOCUMENTED_ROUTES: Final[Mapping[tuple[str, str], tuple[str, tuple[str, ...]]]] = {
    # (método, ruta de la interfaz): (clave de permiso, errores que §3 nombra para la ruta)
    # §3.1 (sin la ruta diff, A-54)
    ("GET", "/zones/{zone_id}/catalog"): ("catalog.read", ()),
    ("GET", "/zones/{zone_id}/catalog/versions"): ("catalog.read", ()),
    ("GET", "/zones/{zone_id}/catalog/versions/{catalog_version}"): ("catalog.read", ()),
    ("POST", "/zones/{zone_id}/standards"): (
        "catalog.manage",
        (
            "family_not_admitted",
            "predicate_invalid",
            "free_text_rejected",
            "zone_without_cameras",
            "unsatisfiable_coverage",
        ),
    ),
    ("POST", "/zones/{zone_id}/standards/{standard_id}/versions"): ("catalog.manage", ()),
    ("POST", "/zones/{zone_id}/standards/{standard_id}/retirement"): (
        "catalog.manage",
        ("last_standard_in_zone",),
    ),
    ("PUT", "/zones/{zone_id}/catalog/single-occupancy"): ("catalog.manage", ()),
    ("POST", "/plants/{plant_id}/admissions"): (
        "catalog.manage",
        ("admission_rejected", "family_already_admitted"),
    ),
    ("GET", "/plants/{plant_id}/admissions"): ("catalog.read", ()),
    ("GET", "/zones/{zone_id}/regression"): ("catalog.read", ()),
    # §3.2
    ("GET", "/zones/{zone_id}/gates"): ("catalog.read", ()),
    ("POST", "/zones/{zone_id}/gates/mounting/scope-record"): (
        "commissioning.run",
        ("blur_not_verified", "node_not_assigned"),
    ),
    ("PUT", "/plants/{plant_id}/signatory-policy"): (
        "commissioning.run",
        ("fewer_than_three", "workers_representation_missing"),
    ),
    ("GET", "/plants/{plant_id}/signatory-policy"): ("catalog.read", ()),
    ("POST", "/zones/{zone_id}/use-agreements"): (
        "commissioning.run",
        (
            "signatory_role_not_in_policy",
            "workers_representation_missing",
            "signatory_user_role_mismatch",
            "agreement_reused_from_other_zone",
        ),
    ),
    ("POST", "/use-agreements/{agreement_id}/confirmations"): (
        "agreements.sign",
        ("signatory_not_expected",),
    ),
    ("POST", "/use-agreements/{agreement_id}/approval"): (
        "commissioning.run",
        (
            "signatures_incomplete",
            "plant_policy_missing",
            "commissioning_record_missing",
            "mounting_gate_pending",
        ),
    ),
    ("POST", "/zones/{zone_id}/gates/{gate}/revocation"): ("commissioning.run", ()),
    ("POST", "/plants/{plant_id}/policy"): ("commissioning.run", ()),
    ("GET", "/plants/{plant_id}/policy"): ("catalog.read", ()),
    ("POST", "/documents"): ("commissioning.run", ()),
    # §3.3
    ("POST", "/zones/{zone_id}/walk-tests"): (
        "commissioning.run",
        (
            "mounting_gate_pending",
            "walk_test_in_progress",
            "node_not_assigned",
            "passes_below_minimum",
        ),
    ),
    ("GET", "/zones/{zone_id}/walk-tests/current"): ("catalog.read", ()),
    ("POST", "/walk-tests/{id}/steps"): ("commissioning.run", ()),
    ("POST", "/walk-tests/{id}/steps/{step_id}/close"): ("commissioning.run", ()),
    ("POST", "/walk-tests/{id}/passes"): ("commissioning.run", ()),
    ("POST", "/walk-tests/{id}/occlusion-tests"): ("commissioning.run", ()),
    ("POST", "/walk-tests/{id}/close"): (
        "commissioning.run",
        (
            "false_negative_present",
            "false_alarm_rate_above_threshold",
            "redundancy_not_verified",
            "steps_still_open",
            "latency_not_measured",
            "blur_not_verified",  # v1.5: guarda nueva de difuminado
        ),
    ),
    ("POST", "/walk-tests/{id}/reopen"): ("commissioning.run", ()),
    ("GET", "/commissioning-records/{id}"): ("catalog.read", ()),
    ("GET", "/commissioning-records/{id}/document"): ("catalog.read", ()),
    ("POST", "/walk-tests/{id}/exposure-samples"): ("commissioning.run", ("pass_not_found",)),
    ("POST", "/zones/{zone_id}/walk-tests/regression-rerun"): ("commissioning.run", ()),
    # §3.4
    ("GET", "/fleet/nodes"): ("fleet.read", ()),
    ("GET", "/fleet/nodes/{node_id}"): ("fleet.read", ()),
    ("GET", "/plants/{plant_id}/fleet-thresholds"): ("fleet.read", ()),
    ("PUT", "/plants/{plant_id}/fleet-thresholds"): ("fleet.manage", ()),
    ("POST", "/plants/{plant_id}/nodes"): (
        "commissioning.run",
        ("zone_already_served", "zone_in_other_plant", "code_in_use", "replaced_node_not_found"),
    ),
    ("POST", "/nodes/{node_id}/zones"): ("commissioning.run", ()),
    ("POST", "/nodes/{node_id}/zones/{zone_id}/unassignment"): ("commissioning.run", ()),
    ("POST", "/nodes/{node_id}/enrollment-codes"): ("commissioning.run", ("node_not_declared",)),
    ("GET", "/nodes/{node_id}/enrollment-attempts"): ("fleet.read", ()),
    ("POST", "/nodes/{node_id}/revocation"): ("fleet.manage", ()),
    ("POST", "/nodes/{node_id}/decommission"): ("fleet.manage", ()),
    ("POST", "/fleet/target-versions"): ("fleet.manage", ("version_outside_contract_window",)),
    ("GET", "/fleet/alarms"): ("fleet.read", ()),
    # §3.5
    ("GET", "/zones/{zone_id}/transparency"): ("transparency.read", ()),
    # v1.5: parámetros del catálogo y clips de verificación
    ("PUT", "/zones/{zone_id}/cameras"): ("catalog.manage", ()),
    ("PUT", "/zones/{zone_id}/minimum-coverage"): ("catalog.manage", ("unsatisfiable_coverage",)),
    ("PUT", "/zones/{zone_id}/signals"): ("catalog.manage", ()),
    ("PUT", "/zones/{zone_id}/thresholds"): ("catalog.manage", ()),
    ("PUT", "/zones/{zone_id}/windows"): ("catalog.manage", ()),
    ("GET", "/zones/{zone_id}/commissioning-clips"): ("catalog.read", ()),
}
"""Las rutas de U-03 para U-05 de §3 y v1.5, con la clave de su columna «Permiso y alcance»."""

WITHDRAWN_ROUTES: Final = {
    ("GET", "/zones/{zone_id}/catalog/versions/{from}/diff/{to}"): "A-54: fuera de v1",
}

ROUTE_ADDITIONS: Final[Mapping[tuple[str, str], tuple[str, str]]] = {
    ("POST", "/zones/{zone_id}/framing-recaptures"): (
        "commissioning.run",
        "A-55: recaptura del encuadre (precisión (c) de v1.5), marca la regresión con "
        "framing_recaptured sin versión nueva del catálogo; la interfaz no la lista",
    ),
}

PERMISSION_DIFFERENCES: Final[Mapping[tuple[str, str], tuple[str, str]]] = {
    ("POST", "/use-agreements/{agreement_id}/confirmations"): (
        "transparency.read",
        "§3.2 da dos claves: la ruta declara transparency.read (la tienen todos los roles "
        "firmantes, también copasst desde SCR-16) y el servicio exige además agreements.sign a "
        "los firmantes de gestión (TASK-212, BR-GOB-27)",
    ),
}

PARAMETER_RENAMES: Final = {
    "/walk-tests/{id}": "/walk-tests/{session_id}",
    "/commissioning-records/{id}": "/commissioning-records/{record_id}",
}
"""Nombres de parámetro de ruta del código frente a ``{id}`` de §3.3 (misma ruta: la forma de la
URL no cambia, solo el nombre en la OpenAPI de la que U-05 genera su cliente)."""

U03_MODULES: Final = (
    "vigia_platform.catalog.",
    "vigia_platform.fleet.",
    "vigia_platform.node_api.",
)


def _code_path(path: str) -> str:
    for documented, code in PARAMETER_RENAMES.items():
        if path.startswith(documented):
            return code + path[len(documented) :]
    return path


def _app_yaml_operations() -> set[tuple[str, str]]:
    document = yaml.safe_load(APP_YAML.read_text(encoding="utf-8"))
    return {
        (method.upper(), path)
        for path, operations in document["paths"].items()
        for method in operations
        if method in {"get", "post", "put", "patch", "delete"}
    }


def _u03_routes() -> dict[tuple[str, str], DeclaredRoute]:
    """Las rutas de la aplicación cuyo manejador vive en un módulo de U-03, y las del contrato."""
    found: dict[tuple[str, str], DeclaredRoute] = {}
    for route in iter_declared_routes(build_openapi_app().routes):
        endpoint = getattr(route.route, "endpoint", None)
        if not route.is_api_route or endpoint is None:
            continue
        is_node = any(d.node is not None for d in route.declarations)
        if not (is_node or endpoint.__module__.startswith(U03_MODULES)):
            continue
        for method in route.methods - {"HEAD"}:
            found[(method, route.path)] = route
    return found


@pytest.fixture(scope="module")
def u03_routes() -> dict[tuple[str, str], DeclaredRoute]:
    return _u03_routes()


def _expected_person_routes() -> dict[tuple[str, str], str]:
    expected = {
        (method, _code_path(path)): PERMISSION_DIFFERENCES.get((method, path), (permission,))[0]
        for (method, path), (permission, _) in DOCUMENTED_ROUTES.items()
    }
    expected.update({key: permission for key, (permission, _) in ROUTE_ADDITIONS.items()})
    return expected


def test_the_documented_routes_are_in_app_yaml_and_the_withdrawn_one_is_not() -> None:
    operations = _app_yaml_operations()
    missing = sorted(set(_expected_person_routes()) - operations)
    assert missing == []
    assert len(DOCUMENTED_ROUTES) == 53
    for method, path in WITHDRAWN_ROUTES:
        assert (method, path) not in operations
        assert not any("/diff/" in candidate for _, candidate in operations)


def test_u03_serves_exactly_the_documented_person_routes(
    u03_routes: dict[tuple[str, str], DeclaredRoute],
) -> None:
    person = {key for key in u03_routes if not key[1].startswith(NODE_PREFIX)}
    assert sorted(person - set(_expected_person_routes())) == []
    assert sorted(set(_expected_person_routes()) - person) == []
    assert person <= _app_yaml_operations()


def _permission(route: DeclaredRoute) -> str | None:
    (permission,) = {d.permission for d in route.declarations if d.permission is not None}
    return permission


def _detail_codes(route: DeclaredRoute) -> set[str]:
    return {code for d in route.declarations for code in d.detail_codes}


@pytest.mark.parametrize(
    "key", sorted(DOCUMENTED_ROUTES) + sorted(ROUTE_ADDITIONS), ids=lambda key: " ".join(key)
)
def test_each_route_declares_the_documented_permission_and_errors(
    key: tuple[str, str], u03_routes: dict[tuple[str, str], DeclaredRoute]
) -> None:
    method, path = key
    route = u03_routes[(method, _code_path(path))]
    assert _permission(route) == _expected_person_routes()[(method, _code_path(path))]
    documented = DOCUMENTED_ROUTES.get(key, ("", ()))[1]
    module = (
        "fleet" if path.startswith(("/fleet", "/nodes", "/plants/{plant_id}/nodes")) else "catalog"
    )
    assert {f"{module}_{name}" for name in documented} <= _detail_codes(route)


def test_each_listed_route_difference_is_still_a_difference() -> None:
    for key, (permission, _) in PERMISSION_DIFFERENCES.items():
        assert DOCUMENTED_ROUTES[key][0] != permission
    for documented, code in PARAMETER_RENAMES.items():
        assert any(path.startswith(documented) for _, path in DOCUMENTED_ROUTES)
        assert documented != code
    assert not set(ROUTE_ADDITIONS) & set(DOCUMENTED_ROUTES)


def test_the_node_routes_are_the_ten_of_the_contract_and_none_for_gates(
    u03_routes: dict[tuple[str, str], DeclaredRoute],
) -> None:
    node = {key for key in u03_routes if key[1].startswith(NODE_PREFIX)}
    expected = {(route.method, route.path) for route in NodeRoute}
    assert len(NodeRoute) == 10
    assert node == expected
    assert expected <= _app_yaml_operations()
    # v1.5 (§3.6): sin ruta de compuertas; las compuertas viajan en la respuesta al latido
    assert not any("gate" in path for _, path in node)


# --- §4 Errores --------------------------------------------------------------------------------

DOCUMENTED_ERRORS: Final[Mapping[str, str]] = {
    # nombre de §4 (o de §3 cuando §4 no lo lista): módulo que lo produce
    "admission_rejected": "catalog",
    "family_already_admitted": "catalog",
    "family_not_admitted": "catalog",
    "predicate_invalid": "catalog",
    "unsatisfiable_coverage": "catalog",
    "zone_without_cameras": "catalog",
    "last_standard_in_zone": "catalog",
    "plant_policy_missing": "catalog",
    "mounting_gate_pending": "catalog",
    "use_gate_not_loaded": "catalog",
    "blur_not_verified": "catalog",
    "fewer_than_three": "catalog",
    "workers_representation_missing": "catalog",
    "signatory_role_not_in_policy": "catalog",
    "signatory_user_role_mismatch": "catalog",
    "signatory_not_expected": "catalog",
    "signatures_incomplete": "catalog",
    "agreement_reused_from_other_zone": "catalog",
    "commissioning_record_missing": "catalog",
    "walk_test_in_progress": "catalog",
    "passes_below_minimum": "catalog",
    "false_negative_present": "catalog",
    "false_alarm_rate_above_threshold": "catalog",
    "redundancy_not_verified": "catalog",
    "no_observability_events_in_window": "catalog",
    "steps_still_open": "catalog",
    "latency_not_measured": "catalog",
    "walk_test_incomplete": "catalog",
    "node_not_assigned": "catalog",
    "node_not_declared": "fleet",
    "node_unregistered": "fleet",
    "zone_already_served": "fleet",
    "zone_in_other_plant": "fleet",
    "code_in_use": "fleet",
    "replaced_node_not_found": "fleet",
    "version_outside_contract_window": "fleet",
    # §3.1 (alta de estándar) y §3.3 v1.2 (muestras de exposición)
    "free_text_rejected": "catalog",
    "pass_not_found": "catalog",
}
"""Los errores de negocio con nombre de §4 (y los dos que solo nombra §3), con el prefijo que el
pendiente nº 33 les da por módulo."""

NOT_ERRORS: Final = (
    # §4 los lista, pero son estado de la zona o avisos del inventario, no ``detail_code``
    "walk_test_regression_pending",
    "node_mute",
    "version_retiring",
    "queue_over_threshold",
    "clock_drift",
    "certificate_expiring",
    "simulated_adapter_in_productive",
    "fleet_version_pending",
    "update_reverted",
)

DETAIL_CODE_ADDITIONS: Final[Mapping[str, str]] = {
    "catalog_matrix_incomplete": "cierre del acta con celdas de la matriz sin los pases exigidos "
    "(TASK-214)",
    "fleet_node_not_revoked": "la baja exige revoked (§3.4 lo dice en «Salida» sin nombrar el "
    "error)",
    "fleet_free_text_rejected": "motivo con texto libre identificable en las rutas de la flota "
    "(mismo validador que catalog_free_text_rejected)",
    "fleet_threshold_invalid": "umbral del panel fuera de 1 a 2 147 483 647 (BR-GOB-79; TASK-224)",
    "fleet_assignment_range_too_long": "assignment_history con más de 366 días (PAT-GOB-REN-04; "
    "TASK-224), para cuando U-04 lo traslade a una respuesta",
}
"""``detail_code`` del código que la interfaz no nombra (añadidos, ninguno sustituye a otro)."""

ALIASES_NOT_IMPLEMENTED: Final = {
    "usage_gate_pending": "§4 lo da como alias de use_gate_not_loaded; solo existe el nombre "
    "principal (una lista cerrada no admite dos nombres para lo mismo)",
}


def _codes() -> set[str]:
    return {code.value for code in CatalogDetailCode} | {code.value for code in FleetDetailCode}


def test_the_detail_codes_are_the_documented_ones_plus_the_listed_additions() -> None:
    documented = {f"{module}_{name}" for name, module in DOCUMENTED_ERRORS.items()}
    assert sorted(documented - _codes()) == []
    assert sorted(_codes() - documented) == sorted(DETAIL_CODE_ADDITIONS)


def test_every_code_has_the_prefix_of_its_module() -> None:
    assert all(code.value.startswith("catalog_") for code in CatalogDetailCode)
    assert all(code.value.startswith("fleet_") for code in FleetDetailCode)


def test_states_warnings_and_aliases_are_not_detail_codes() -> None:
    names = {code.split("_", 1)[1] for code in _codes()}
    for name in (*NOT_ERRORS, *ALIASES_NOT_IMPLEMENTED):
        assert name not in names, name
