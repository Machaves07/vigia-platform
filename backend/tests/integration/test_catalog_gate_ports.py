"""``CatalogQueryPort`` y ``GateQueryPort`` en proceso contra PostgreSQL 16 real (TASK-213;
LC-GOB-23a; interfaces §1.1 y §1.2).

- Cada una de las quince operaciones es **una** sentencia contada, de solo lectura;
- lo que leen: catálogo vigente e histórico, versiones de estándar y su vigencia
  (``standard_valid_at``), historia paginada por clave, marca unipersonal, regresión, proyección y
  historia de compuertas, política de planta y acuerdo vigente;
- ``state_at`` sale de la historia y no de la proyección (se altera la proyección);
- con los servicios reales: lo publicado por ``CatalogPublicationService`` es lo que lee el puerto,
  ``regression_state`` refleja ``framing_recaptured`` (``RegressionService``) y
  ``current_agreement`` refleja la sustitución sin hueco (``AgreementService``);
- guarda de alcance: con dos plantas de la misma organización, un contexto de planta o de zona no
  ve la otra; las sentencias, ejecutadas sin RLS, siguen limitadas por su filtro explícito;
- sin contexto, ``ContextAbsent``; entradas inválidas, ``PortQueryInvalid``; topes,
  ``PortLimitExceeded``.

El aislamiento entre organizaciones de cada operación (PR-GOB-12) está en
``tests/properties/gob/test_ports_isolation.py`` y la equivalencia de los lotes (PR-GOB-30) en
``test_ports_batch.py``. Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.dialects.postgresql.asyncpg import dialect as asyncpg_dialect
from vigia_contracts.models.enumerations import GateStatus, ZoneMode

from tests.agreements_support import AgreementsWorld, agreements_world
from tests.catalog_ports_support import (
    DAY,
    HOUR,
    OPERATION_NAMES,
    SHA,
    PortsWorld,
    ZoneData,
    operations,
    ports_world,
)
from tests.catalog_routes_support import CatalogRoutes, catalog_routes_world, first_standard_body
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.catalog.adapters.postgres import catalog_query, gate_query
from vigia_platform.catalog.adapters.postgres.catalog_query import scope_parameters
from vigia_platform.catalog.adapters.postgres.query_ports import catalog_query_ports
from vigia_platform.catalog.domain.enums import (
    AgreementStatus,
    CatalogChangedField,
    GateKind,
    RegressionCause,
    RegressionState,
)
from vigia_platform.catalog.domain.ports import (
    CatalogQueryPort,
    GateQueryPort,
    PortLimitExceeded,
    PortQueryInvalid,
    StandardRef,
)
from vigia_platform.catalog.domain.regression import ALL_ROWS
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.shared.context import ContextAbsent, ScopeLevel
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration

US = timedelta(microseconds=1)


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[PortsWorld]:
    with ports_world(postgres_endpoint, "catalog_gate_ports") as built:
        yield built


def _populated(world: PortsWorld, plants: int = 1, zones: int = 1) -> tuple[Any, list[ZoneData]]:
    site = world.site(plants=plants, zones=zones)
    data = [world.populate(site.organization_id, plant, zone) for plant, zone in site.zones()]
    return site, data


def test_the_ports_satisfy_their_protocols(world: PortsWorld) -> None:
    catalog: CatalogQueryPort = world.ports.catalog
    gates: GateQueryPort = world.ports.gates
    assert catalog is not None and gates is not None
    assert repr(world.ports.catalog) == "PostgresCatalogQuery()"
    assert repr(world.ports.gates) == "PostgresGateQuery()"


def test_each_operation_is_one_read_only_statement(world: PortsWorld) -> None:
    site, (data,) = _populated(world)
    context = world.context(site.organization_id)
    for name, operation in operations(world.ports, data).items():
        world.authz.log.statements.clear()
        world.run(operation(context))
        statements = world.authz.log.data_statements()
        assert len(statements) == 1, (name, statements)
        assert statements[0].lstrip().upper().startswith(("SELECT", "WITH")), name
    assert len(operations(world.ports, data)) == len(OPERATION_NAMES) == 15


def test_the_catalog_operations_read_what_was_written(world: PortsWorld) -> None:
    site, (data,) = _populated(world)
    context = world.context(site.organization_id)
    catalog = world.ports.catalog
    run = world.run
    current = run(catalog.current_catalog(context, data.zone_id))
    assert current.catalog_version == 2 and current.superseded_at is None
    assert current.single_occupancy is True and current.aggregation_window_minutes == 120
    assert current.payload == {"version": 2}
    # catalog_at: semiabierto [issued_at, superseded_at).
    assert run(catalog.catalog_at(context, data.zone_id, data.t0)).catalog_version == 1
    assert run(catalog.catalog_at(context, data.zone_id, data.t0 + DAY - US)).catalog_version == 1
    assert run(catalog.catalog_at(context, data.zone_id, data.t0 + DAY)).catalog_version == 2
    with pytest.raises(ResourceNotFound):  # antes del primer catálogo
        run(catalog.catalog_at(context, data.zone_id, data.t0 - US))
    first = run(catalog.standard_version(context, data.zone_id, data.standard_id, 1))
    assert first.retired_in_catalog_version == 2 and first.effective_until == data.t0 + DAY
    second = run(catalog.standard_version(context, data.zone_id, data.standard_id, 2))
    assert second.effective_until is None and second.catalog_version == 2
    with pytest.raises(ResourceNotFound):
        run(catalog.standard_version(context, data.zone_id, data.standard_id, 3))
    at = catalog.standard_at
    assert run(at(context, data.zone_id, data.standard_id, data.t0 + DAY - US)) == first
    assert run(at(context, data.zone_id, data.standard_id, data.t0 + DAY)) == second
    with pytest.raises(ResourceNotFound):
        run(at(context, data.zone_id, data.standard_id, data.t0 - US))
    with pytest.raises(ResourceNotFound):  # el estándar no es de otra zona de la organización
        other = world.site()
        world.populate(other.organization_id, *other.zones()[0])
        run(at(context, data.zone_id, uuid.uuid4(), data.at))
    marks = run(catalog.single_occupancy(context, data.zone_id))
    assert (marks.single_occupancy, marks.aggregation_window_minutes) == (True, 120)
    assert marks.catalog_version == 2 and marks.issued_at == data.t0 + DAY
    old = run(catalog.single_occupancy(context, data.zone_id, data.t0 + HOUR))
    assert (old.single_occupancy, old.aggregation_window_minutes, old.catalog_version) == (
        False,
        60,
        1,
    )
    regression = run(catalog.regression_state(context, data.zone_id))
    assert regression.state is RegressionState.PENDING
    assert regression.cause is RegressionCause.FRAMING_RECAPTURED
    assert regression.affected_row_ids == ALL_ROWS


def test_a_zone_without_catalog_or_marks(world: PortsWorld) -> None:
    site = world.site()
    ((plant, zone),) = site.zones()
    context = world.context(site.organization_id)
    catalog, gates = world.ports.catalog, world.ports.gates
    run = world.run
    for operation in (
        catalog.current_catalog(context, zone),
        catalog.single_occupancy(context, zone),
        catalog.standard_at(context, zone, uuid.uuid4(), world.authz.now()),
    ):
        with pytest.raises(ResourceNotFound):
            run(operation)
    page = run(catalog.catalog_history(context, zone))
    assert page.items == () and page.next_cursor is None
    regression = run(catalog.regression_state(context, zone))
    assert regression.state is RegressionState.CURRENT and regression.cause is None
    assert (regression.organization_id, regression.plant_id, regression.zone_id) == (
        site.organization_id,
        plant,
        zone,
    )
    state = run(gates.state(context, zone))
    assert state.mounting.status is GateStatus.PENDING and state.usage.status is GateStatus.PENDING
    assert state.resulting_mode is ZoneMode.NO_CAPTURE and state.issued_at is None
    assert run(gates.state_at(context, zone, GateKind.MOUNTING, world.authz.now())) is None
    assert run(gates.gate_history(context, zone, data_start(world), world.authz.now())) == ()
    assert run(gates.plant_policy(context, plant)).loaded is False
    assert run(gates.current_agreement(context, zone)) is None
    assert [s.zone_id for s in run(gates.states_by_plant(context, plant))] == [zone]


def data_start(world: PortsWorld) -> Any:
    return world.authz.now() - 300 * DAY


def test_catalog_history_pages_by_key(world: PortsWorld) -> None:
    site = world.site()
    ((plant, zone),) = site.zones()
    keys = (site.organization_id, plant, zone)
    start = world.authz.now()
    for version in range(1, 6):
        world.add_version(
            *keys,
            version,
            start + version * HOUR,
            superseded_at=None if version == 5 else start + (version + 1) * HOUR,
            changed_fields=("standards", "cameras") if version % 2 else ("thresholds",),
        )
    context = world.context(site.organization_id)
    catalog = world.ports.catalog
    first = world.run(catalog.catalog_history(context, zone, limit=2))
    assert [e.catalog_version for e in first.items] == [5, 4] and first.next_cursor == 4
    assert first.items[0].changed_fields == (
        CatalogChangedField.STANDARDS,
        CatalogChangedField.CAMERAS,
    )
    assert first.items[0].issued_at == start + 5 * HOUR
    assert first.items[0].reason_es.endswith(" 5") and first.items[0].issued_by == world.user_id
    second = world.run(catalog.catalog_history(context, zone, cursor=4, limit=2))
    assert [e.catalog_version for e in second.items] == [3, 2] and second.next_cursor == 2
    last = world.run(catalog.catalog_history(context, zone, cursor=2, limit=2))
    assert [e.catalog_version for e in last.items] == [1] and last.next_cursor is None
    exact = world.run(catalog.catalog_history(context, zone, limit=5))
    assert len(exact.items) == 5 and exact.next_cursor is None
    whole = world.run(catalog.catalog_history(context, zone))
    assert [e.catalog_version for e in whole.items] == [5, 4, 3, 2, 1]


def test_the_gate_operations_read_what_was_written(world: PortsWorld) -> None:
    site = world.site(zones=2)
    (plant, zone), (_, sibling) = site.zones()
    keys = (site.organization_id, plant, zone)
    t0 = world.authz.now() - 100 * DAY
    approved = world.add_interval(*keys, GateKind.MOUNTING, "approved", t0, t0 + 10 * DAY)
    world.add_interval(*keys, GateKind.MOUNTING, "revoked", t0 + 10 * DAY, t0 + 20 * DAY)
    again = world.add_interval(*keys, GateKind.MOUNTING, "approved", t0 + 20 * DAY)
    usage = world.add_interval(*keys, GateKind.USAGE, "approved", t0 + 30 * DAY)
    context = world.context(site.organization_id)
    gates = world.ports.gates
    run = world.run
    found = run(gates.state_at(context, zone, GateKind.MOUNTING, t0 + 10 * DAY - US))
    assert found.status is GateStatus.APPROVED and found.record_id == approved
    assert found.effective_until == t0 + 10 * DAY and found.decided_by == world.user_id
    revoked = run(gates.state_at(context, zone, GateKind.MOUNTING, t0 + 10 * DAY))
    assert revoked.status is GateStatus.REVOKED and revoked.reason_es is not None
    assert run(gates.state_at(context, zone, GateKind.USAGE, t0 + 30 * DAY - US)) is None
    history = run(gates.gate_history(context, zone, t0 + 10 * DAY, t0 + 30 * DAY))
    # [from, to] cerrado: el intervalo que empieza justo en «to» también se solapa.
    assert [(i.gate, i.status, i.effective_from) for i in history] == [
        (GateKind.MOUNTING, GateStatus.REVOKED, t0 + 10 * DAY),
        (GateKind.MOUNTING, GateStatus.APPROVED, t0 + 20 * DAY),
        (GateKind.USAGE, GateStatus.APPROVED, t0 + 30 * DAY),
    ]
    assert history[1].record_id == again and history[2].record_id == usage
    # Un intervalo que termina justo en «from» no se solapa (semiabierto).
    assert run(gates.gate_history(context, zone, t0 + 20 * DAY, t0 + 20 * DAY)) == (history[1],)
    world.set_projection(
        *keys,
        {"status": "approved", "decided_at": (t0 + 20 * DAY).isoformat(),
         "record_id": str(again), "decided_by": str(world.user_id)},
        {"status": "approved", "decided_at": (t0 + 30 * DAY).isoformat(),
         "agreement_id": str(usage), "decided_by": str(world.user_id)},
        t0 + 30 * DAY,
        mode="productive",
    )  # fmt: skip
    state = run(gates.state(context, zone))
    assert state.resulting_mode is ZoneMode.PRODUCTIVE and state.issued_at == t0 + 30 * DAY
    assert state.mounting.record_id == again and state.usage.record_id == usage
    assert state.usage.decided_at == t0 + 30 * DAY and state.envelope is None
    by_plant = run(gates.states_by_plant(context, plant))
    assert {s.zone_id: s.resulting_mode for s in by_plant} == {
        zone: ZoneMode.PRODUCTIVE,
        sibling: ZoneMode.NO_CAPTURE,
    }
    world.add_policy(site.organization_id, plant, 1, t0)
    latest = world.add_policy(site.organization_id, plant, 2, t0 + DAY)
    policy = run(gates.plant_policy(context, plant))
    assert policy.loaded and policy.policy_id == latest and policy.version == 2
    assert policy.document_sha256 == SHA and policy.signed_at == t0 + DAY


def test_state_at_follows_the_history_not_the_projection(world: PortsWorld) -> None:
    site, (data,) = _populated(world)
    keys = (data.organization_id, data.plant_id, data.zone_id)
    context = world.context(site.organization_id)
    gates = world.ports.gates
    before = world.run(gates.state_at(context, data.zone_id, GateKind.USAGE, data.at))
    assert before is not None and before.status is GateStatus.APPROVED
    # La proyección dice otra cosa (revocada y pendiente): state_at sigue la historia.
    world.set_projection(
        *keys,
        {"status": "revoked", "decided_at": data.at.isoformat(), "record_id": str(uuid.uuid4()),
         "decided_by": str(world.user_id)},
        {"status": "pending"},
        data.at,
        mode="no_capture",
    )  # fmt: skip
    assert world.run(gates.state(context, data.zone_id)).usage.status is GateStatus.PENDING
    for gate in GateKind:
        after = world.run(gates.state_at(context, data.zone_id, gate, data.at))
        assert after is not None and after.status is GateStatus.APPROVED, gate
    assert world.run(gates.state_at(context, data.zone_id, GateKind.USAGE, data.at)) == before


def test_the_current_agreement_lists_signatories_and_confirmations(world: PortsWorld) -> None:
    site = world.site()
    ((plant, zone),) = site.zones()
    keys = (site.organization_id, plant, zone)
    at = world.authz.now() - 10 * DAY
    first = world.add_agreement(*keys, at)
    world.execute(
        "UPDATE catalog.use_agreement SET status = 'superseded', superseded_at = $2"
        " WHERE agreement_id = $1",
        first,
        at + DAY,
    )
    second = world.add_agreement(*keys, at + DAY, replaces=first, confirmed=2)
    found = world.run(
        world.ports.gates.current_agreement(world.context(site.organization_id), zone)
    )
    assert found is not None and found.agreement_id == second
    assert found.status is AgreementStatus.APPROVED and found.replaces_agreement_id == first
    assert found.approved_at == at + DAY and found.document_sha256 == SHA
    assert [s.role.value for s in found.signatories] == [
        "coordinator_sst",
        "plant_manager",
        "copasst",
    ]
    assert [s.confirmed_at for s in found.signatories] == [at + DAY - HOUR, at + DAY - HOUR, None]
    assert all(s.display_name and s.display_name.startswith("Firmante") for s in found.signatories)


# --- Alcance -----------------------------------------------------------------------------------


def test_a_plant_or_zone_context_never_sees_the_other_plant_or_zone(world: PortsWorld) -> None:
    site, data = _populated(world, plants=2, zones=2)
    (mine, sibling, other, other_sibling) = data
    plant_context = world.context(site.organization_id, (ScopeLevel.PLANT, mine.plant_id))
    zone_context = world.context(site.organization_id, (ScopeLevel.ZONE, mine.zone_id))
    for context, hidden in ((plant_context, (other, other_sibling)), (zone_context, (sibling,))):
        for target in hidden:
            for name, operation in operations(world.ports, target).items():
                if context is zone_context and name in ("states_by_plant", "plant_policy"):
                    continue  # la planta es la suya: la visibilidad de la planta va abajo
                with pytest.raises(ResourceNotFound):
                    world.run(operation(context))
                    pytest.fail(f"{name} vio un recurso fuera del alcance")
        for operation in operations(world.ports, mine).values():
            world.run(operation(context))
    # Un lote con una sola zona de fuera del alcance: no_found entero, nunca parcial.
    catalog = world.ports.catalog
    with pytest.raises(ResourceNotFound):
        world.run(catalog.single_occupancy_many(plant_context, [mine.zone_id, other.zone_id]))
    with pytest.raises(ResourceNotFound):
        world.run(
            catalog.standards_at_many(
                zone_context,
                [StandardRef(mine.zone_id, mine.standard_id, mine.at),
                 StandardRef(sibling.zone_id, sibling.standard_id, sibling.at)],
            )
        )  # fmt: skip
    # La planta de un contexto de zona es visible, pero solo con su zona.
    gates = world.ports.gates
    states = world.run(gates.states_by_plant(zone_context, mine.plant_id))
    assert [s.zone_id for s in states] == [mine.zone_id]
    assert world.run(gates.plant_policy(zone_context, mine.plant_id)).loaded
    for operation in (
        gates.states_by_plant(zone_context, other.plant_id),
        gates.plant_policy(zone_context, other.plant_id),
    ):
        with pytest.raises(ResourceNotFound):
            world.run(operation)
    plant_states = world.run(gates.states_by_plant(plant_context, mine.plant_id))
    assert {s.zone_id for s in plant_states} == {mine.zone_id, sibling.zone_id}
    # Sin alcance (un contexto de sistema sin asignaciones), nada.
    nothing = world.context(site.organization_id, (ScopeLevel.ZONE, uuid.uuid4()))
    for operation in operations(world.ports, mine).values():
        with pytest.raises(ResourceNotFound):
            world.run(operation(nothing))


def _as_superuser(world: PortsWorld, statement: Any, parameters: dict[str, Any]) -> list[Any]:
    compiled = statement.compile(dialect=asyncpg_dialect())
    order = compiled.positiontup or []
    return world.fetch(str(compiled), *(parameters[name] for name in order))


_ZONE_STATEMENTS = (
    catalog_query._CURRENT,
    catalog_query._AT,
    catalog_query._STANDARD_VERSION,
    catalog_query._STANDARDS_AT,
    catalog_query._SINGLE_OCCUPANCY,
    catalog_query._HISTORY,
    catalog_query._REGRESSION,
    gate_query._STATE,
    gate_query._STATE_AT,
    gate_query._HISTORY,
    gate_query._CURRENT_AGREEMENT,
)
_PLANT_STATEMENTS = (gate_query._STATES_BY_PLANT, gate_query._PLANT_POLICY)


def _visible_rows(rows: list[Any]) -> list[Any]:
    """Las filas con zona o planta visible (los lotes devuelven una por elemento pedido)."""
    visible = []
    for row in rows:
        keys = list(row.keys())
        column = "visible" if "visible" in keys else "zone_id" if "zone_id" in keys else None
        if column is None or row[column] is not None:
            visible.append(row)
    return visible


def test_the_explicit_filters_isolate_even_without_row_level_security(world: PortsWorld) -> None:
    # Defensa en profundidad: las sentencias, como superusuario (sin RLS), siguen limitadas por su
    # filtro explícito de organización, planta y zona.
    site, data = _populated(world, plants=2, zones=2)
    mine, sibling, other, _ = data
    theirs = _populated(world)[1][0]
    organization = world.context(site.organization_id)
    plant_context = world.context(site.organization_id, (ScopeLevel.PLANT, mine.plant_id))
    zone_context = world.context(site.organization_id, (ScopeLevel.ZONE, mine.zone_id))

    def run(statement: Any, context: Any, target: ZoneData) -> list[Any]:
        parameters = {
            **scope_parameters(context),
            "zone_id": target.zone_id,
            "plant_id": target.plant_id,
            "standard_id": target.standard_id,
            "version": 2,
            "at": target.at,
            "gate": GateKind.USAGE.value,
            "start": target.t0,
            "end": target.t0 + 30 * DAY,
            "cursor": None,
            "limit": 10,
            "zone_ids": [target.zone_id],
            "standard_ids": [target.standard_id],
            "ats": [target.at],
        }
        return _visible_rows(_as_superuser(world, statement, parameters))

    for statement in (*_ZONE_STATEMENTS, *_PLANT_STATEMENTS):
        assert run(statement, organization, mine), statement
        assert run(statement, organization, theirs) == [], statement
        assert run(statement, plant_context, other) == [], statement
    for statement in _ZONE_STATEMENTS:
        assert run(statement, zone_context, sibling) == [], statement
        assert run(statement, zone_context, mine), statement


# --- Entradas ----------------------------------------------------------------------------------


def test_without_a_context_nothing_is_read(world: PortsWorld) -> None:
    _, (data,) = _populated(world)
    for name, operation in operations(world.ports, data).items():
        world.authz.log.statements.clear()
        with pytest.raises(ContextAbsent):
            world.run(operation(None))  # type: ignore[arg-type]
        assert world.authz.log.statements == [], name


def test_invalid_inputs_and_the_limits(world: PortsWorld) -> None:
    site, (data,) = _populated(world)
    context = world.context(site.organization_id)
    catalog, gates = world.ports.catalog, world.ports.gates
    naive = data.at.replace(tzinfo=None)
    zone = data.zone_id
    invalid: list[Any] = [
        catalog.current_catalog(context, str(zone)),  # type: ignore[arg-type]
        catalog.catalog_at(context, zone, naive),
        catalog.standard_version(context, zone, data.standard_id, 0),
        catalog.standard_version(context, zone, data.standard_id, True),  # type: ignore[arg-type]
        catalog.standard_version(context, zone, data.standard_id, 2**31),
        catalog.standard_at(context, zone, data.standard_id, naive),
        catalog.catalog_history(context, zone, limit=0),
        catalog.catalog_history(context, zone, limit=201),
        catalog.catalog_history(context, zone, cursor=0),
        catalog.single_occupancy(context, zone, naive),
        catalog.single_occupancy_many(context, {zone}),  # type: ignore[arg-type]
        catalog.single_occupancy_many(context, [str(zone)]),  # type: ignore[list-item]
        catalog.standards_at_many(context, [(zone, data.standard_id, data.at)]),  # type: ignore[list-item]
        catalog.standards_at_many(context, [StandardRef(zone, data.standard_id, naive)]),
        gates.states_by_plant(context, str(data.plant_id)),  # type: ignore[arg-type]
        gates.state_at(context, zone, "usage", data.at),  # type: ignore[arg-type]
        gates.gate_history(context, zone, data.at, data.at - US),
        gates.plant_policy(context, None),  # type: ignore[arg-type]
    ]
    for awaitable in invalid:
        with pytest.raises(PortQueryInvalid):
            world.run(awaitable)
    zones = [zone] * 50
    assert len(world.run(catalog.single_occupancy_many(context, zones))) == 50
    assert world.run(catalog.single_occupancy_many(context, [])) == ()
    ref = StandardRef(zone, data.standard_id, data.at)
    assert len(world.run(catalog.standards_at_many(context, [ref] * 200))) == 200
    assert world.run(catalog.standards_at_many(context, [])) == ()
    assert world.run(gates.gate_history(context, zone, data.t0, data.t0 + timedelta(days=366)))
    world.authz.log.statements.clear()
    for awaitable in (
        catalog.single_occupancy_many(context, [zone] * 51),
        catalog.standards_at_many(context, [ref] * 201),
        gates.gate_history(context, zone, data.t0, data.t0 + timedelta(days=367)),
        gates.gate_history(context, zone, data.t0, data.t0 + timedelta(days=366) + US),
    ):
        with pytest.raises(PortLimitExceeded):
            world.run(awaitable)
    assert world.authz.log.statements == []  # el tope se rechaza antes de consultar


def test_the_constructor_builds_both_ports_on_the_given_database(world: PortsWorld) -> None:
    ports = catalog_query_ports(world.authz.sessions.database)
    assert repr(ports.catalog) == "PostgresCatalogQuery()"
    assert repr(ports.gates) == "PostgresGateQuery()"


# --- Con los servicios reales ------------------------------------------------------------------


@pytest.fixture(scope="module")
def routes(postgres_endpoint: PostgresEndpoint) -> Iterator[CatalogRoutes]:
    with catalog_routes_world(postgres_endpoint, "catalog_gate_ports_routes") as built:
        yield built


def test_the_port_reads_the_published_catalog_and_the_framing_recapture(
    routes: CatalogRoutes,
) -> None:
    site = routes.site()
    ((plant, zone),) = site.zones()
    routes.productive(site, plant, zone)
    admin = routes.member(site)
    created = routes.request("POST", f"/zones/{zone}/standards", admin, first_standard_body())
    assert created.status_code == 201, created.text
    changed = routes.request(
        "PUT",
        f"/zones/{zone}/catalog/single-occupancy",
        admin,
        {"single_occupancy": True, "aggregation_window_minutes": 90,
         "reason_es": "Zona de trabajo unipersonal declarada"},
    )  # fmt: skip
    assert changed.status_code == 200, changed.text
    ports = catalog_query_ports(routes.database)
    context = routes.context(admin)
    catalog = ports.catalog
    current = routes.run(catalog.current_catalog(context, zone))
    published = routes.run(routes.publication.catalog_version(context, zone))
    assert current == published and current.catalog_version == 2
    history = routes.run(routes.publication.standard_history(context, zone))
    (standard,) = history
    found = routes.run(catalog.standard_at(context, zone, standard.standard_id, current.issued_at))
    assert found == standard
    assert (
        routes.run(catalog.standard_version(context, zone, standard.standard_id, standard.version))
        == standard
    )
    marks = routes.run(catalog.single_occupancy(context, zone))
    assert (marks.single_occupancy, marks.aggregation_window_minutes) == (True, 90)
    first = routes.run(catalog.single_occupancy(context, zone, current.issued_at - US))
    assert (first.single_occupancy, first.catalog_version) == (False, 1)
    page = routes.run(catalog.catalog_history(context, zone))
    assert [e.catalog_version for e in page.items] == [2, 1]
    before = routes.run(catalog.regression_state(context, zone))
    assert before == routes.run(routes.regression.regression_state(context, zone))
    assert before.cause is not RegressionCause.FRAMING_RECAPTURED
    # La recaptura del encuadre (ruta y RegressionService reales): framing_recaptured y la matriz
    # entera, sin versión nueva del catálogo.
    installer, concession = routes.installer(site)
    camera = current.payload["cameras"][0]["camera_id"]
    recaptured = routes.request(
        "POST",
        f"/zones/{zone}/framing-recaptures",
        installer,
        {
            "camera_id": camera,
            "captured_at": format_timestamp(routes.authz.now() - timedelta(minutes=3)),
            "reason_es": "Recaptura sintética del encuadre de la celda",
        },
        concession=concession,
    )
    assert recaptured.status_code == 201, recaptured.text
    assert routes.run(catalog.current_catalog(context, zone)) == current
    regression = routes.run(catalog.regression_state(context, zone))
    assert regression.state is RegressionState.PENDING
    assert regression.cause is RegressionCause.FRAMING_RECAPTURED
    assert regression.affected_row_ids == ALL_ROWS
    assert regression == routes.run(routes.regression.regression_state(context, zone))
    assert plant in site.plants


@pytest.fixture(scope="module")
def agreements(postgres_endpoint: PostgresEndpoint) -> Iterator[AgreementsWorld]:
    with agreements_world(postgres_endpoint, "catalog_gate_ports_agreements") as built:
        yield built


def test_current_agreement_reflects_the_replacement_without_a_gap(
    agreements: AgreementsWorld,
) -> None:
    world = agreements
    ready = world.ready()
    assert ready.agreement is not None
    first = world.approve(ready.installer, ready.agreement.agreement_id)
    ports = catalog_query_ports(world.g.database)
    gates = ports.gates
    reader = ready.installer
    current = world.run(gates.current_agreement(reader, ready.zone))
    assert current is not None and current.agreement_id == first.agreement.agreement_id
    assert current.replaces_agreement_id is None
    replacement = world.create(ready, replaces=first.agreement.agreement_id)
    for signer in ready.signers:
        world.confirm(signer.context, replacement.agreement_id)
    second = world.approve(ready.installer, replacement.agreement_id)
    at = second.agreement.approved_at
    assert at is not None
    current = world.run(gates.current_agreement(reader, ready.zone))
    assert current is not None and current.agreement_id == replacement.agreement_id
    assert current.replaces_agreement_id == first.agreement.agreement_id
    assert current.approved_at == at and current.status is AgreementStatus.APPROVED
    confirmations = {row["user_id"]: row["confirmed_at"] for row in
                     world.confirmation_rows(replacement.agreement_id)}  # fmt: skip
    assert {s.user_id: s.confirmed_at for s in current.signatories} == confirmations
    assert {(s.role, s.user_id) for s in current.signatories} == {
        (s.role, s.user_id) for s in ready.signers
    }
    # Sin hueco: la historia de uso es contigua y state_at aprueba a cada lado del relevo.
    history = world.run(gates.gate_history(reader, ready.zone, at - DAY, at + DAY))
    usage = [i for i in history if i.gate is GateKind.USAGE]
    assert [(i.status, i.record_id) for i in usage] == [
        (GateStatus.APPROVED, first.agreement.agreement_id),
        (GateStatus.APPROVED, replacement.agreement_id),
    ]
    assert usage[0].effective_until == usage[1].effective_from == at
    before = world.run(gates.state_at(reader, ready.zone, GateKind.USAGE, at - US))
    after = world.run(gates.state_at(reader, ready.zone, GateKind.USAGE, at))
    assert before == usage[0] and after == usage[1]
    # Y coincide con el servicio de compuertas de la ruta.
    assert after == world.run(world.g.gates.state_at(reader, ready.zone, GateKind.USAGE, at))
    state = world.run(gates.state(reader, ready.zone))
    assert state.resulting_mode is ZoneMode.PRODUCTIVE
    assert state.usage.record_id == replacement.agreement_id
    policy = world.run(gates.plant_policy(reader, ready.plant))
    stored = world.fetch(
        "SELECT policy_id, document_ref FROM catalog.plant_policy WHERE plant_id = $1"
        " ORDER BY version DESC LIMIT 1",
        ready.plant,
    )[0]
    assert policy.loaded and policy.policy_id == stored["policy_id"]
    assert policy.document_sha256 == json.loads(stored["document_ref"]).get("sha256")
