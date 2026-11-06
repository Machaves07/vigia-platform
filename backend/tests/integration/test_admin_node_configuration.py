"""``vigia-admin node-configuration`` contra PostgreSQL real, como ``vigia_app`` (TASK-544; Q7).

La orden corre entera (``admin_cli.run``) con los servicios reales de la base, y la aplicación real
de las rutas del contrato responde en proceso:

- **Alta** (BR-BOR-61, D-11): tras ``set``, el alta siguiente del nodo recibe ``time_sources``,
  ``heartbeat``, retención y token.
- **Latido** (A-04): la respuesta trae el intervalo de la fila y cinco veces ese intervalo.
- **Auditoría**: exactamente una ``node_configuration_changed`` por ``set``, con actor y nodo, en la
  cadena de la proveedora y sin valores libres.
- **Concurrencia**: cambios a la vez sobre un nodo sin fila no chocan ni se pisan.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import io
import json
import uuid
from collections.abc import Iterator, Sequence
from typing import Any, Final

import pytest

from tests.fleet_enrollment_support import EnrollmentWorld, enrollment_world
from tests.heartbeat_support import HeartbeatStack, heartbeat_stack
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.fleet.application.node_configuration_write import (
    NodeConfigurationChanges,
    NodeConfigurationWriter,
)
from vigia_platform.identity.application.admin_cli import (
    AdminConfig,
    ExitCode,
    Streams,
    build_parser,
    execute,
)
from vigia_platform.ledger.application.audit_writer import AuditWriter

pytestmark = pytest.mark.integration

LAN_SOURCE: Final = "192.168.20.69"
ROUNDS: Final = 3


class Admin:
    """``vigia-admin`` con la base y los contextos de una pila de prueba."""

    def __init__(self, authz: Any, database: Any) -> None:
        self.authz = authz
        self.database = database
        self.audit = AuditWriter(
            database=database,
            clock=authz.sessions.clock,
            provider_organization_id=authz.provider_organization_id,
        )

    def _runtime(self) -> Any:
        return _Runtime(self.database, self.authz.contexts, self.audit, self.authz.sessions.clock)

    def writer(self) -> NodeConfigurationWriter:
        return NodeConfigurationWriter(
            database=self.database,
            contexts=self.authz.contexts,
            audit=self.audit,
            clock=self.authz.sessions.clock,
            provider_organization_id=self.authz.provider_organization_id,
        )

    def operator(self) -> Any:
        return self.authz.run(self.authz.contexts.context_from_operator(self.authz.operator_id))

    def order(self, node: uuid.UUID, action: str, *argv: str) -> tuple[int, dict[str, Any]]:
        async def builder(_config: Any, _provider: uuid.UUID) -> Any:
            return self._runtime()

        stdout, stderr = io.StringIO(), io.StringIO()
        arguments = build_parser().parse_args(
            [
                "node-configuration",
                action,
                "--node",
                str(node),
                "--operator",
                str(self.authz.operator_id),
                *argv,
            ]
        )
        config = AdminConfig.from_environ(
            {
                "VIGIA_ENVIRONMENT": "test",
                "VIGIA_PROVIDER_ORGANIZATION_ID": str(self.authz.provider_organization_id),
            }
        )
        # ``execute`` y no ``run``: la base de la pila vive en el bucle de la pila.
        code: int = self.authz.run(
            execute(arguments, config, builder, Streams(io.StringIO(), stdout, stderr))
        )
        text = stdout.getvalue().strip() or stderr.getvalue().strip().splitlines()[-1]
        return code, json.loads(text)

    def audited(self, node: uuid.UUID) -> list[Any]:
        rows: list[Any] = self.authz.fetch(
            "SELECT organization_id, actor_id, resource_kind, resource_id, filters, outcome"
            " FROM shared.audit_entry WHERE operation = 'node_configuration_changed'"
            " AND resource_id = $1 ORDER BY chain_sequence",
            node,
        )
        return rows

    def row(self, node: uuid.UUID) -> Any:
        rows = self.authz.fetch("SELECT * FROM fleet.node_configuration WHERE node_id = $1", node)
        return rows[0] if rows else None


class _Runtime:
    """Lo que usa la orden de ``AdminRuntime``: el resto de sus campos no se toca."""

    def __init__(self, database: Any, contexts: Any, audit: Any, clock: Any) -> None:
        self.database = _KeepOpen(database)
        self.contexts = contexts
        self.audit = audit
        self.clock = clock


class _KeepOpen:
    """La base es de la pila: ``vigia-admin`` la cerraría al terminar."""

    def __init__(self, database: Any) -> None:
        self._database = database

    def transaction(self, context: Any) -> Any:
        return self._database.transaction(context)

    async def dispose(self) -> None:
        return None


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[EnrollmentWorld]:
    with enrollment_world(postgres_endpoint, "node_configuration") as built:
        yield built


@pytest.fixture(scope="module")
def admin(world: EnrollmentWorld) -> Admin:
    return Admin(world.fleet.authz, world.fleet.database)


# --- Alta ----------------------------------------------------------------------------------------


def test_the_next_enrollment_receives_the_configuration(
    world: EnrollmentWorld, admin: Admin
) -> None:
    setup = world.declared()
    code, out = admin.order(
        setup.node_id,
        "set",
        "--time-source",
        LAN_SOURCE,
        "--heartbeat-interval",
        "60",
        "--retention-days",
        "45",
        "--token-max-age",
        "900",
    )
    assert code == ExitCode.OK, out
    assert out["mute_after_seconds"] == 300
    enrolled = world.enroll(setup)
    initial = enrolled.body["initial_configuration"]
    assert initial["time_sources"] == [LAN_SOURCE]
    assert initial["heartbeat"] == {"interval_seconds": 60, "mute_after_seconds": 300}
    assert initial["sent_records_retention_days"] == 45
    assert initial["live_view"] == {"token_max_age_seconds": 900}


def test_without_a_row_the_enrollment_keeps_the_default(
    world: EnrollmentWorld, admin: Admin
) -> None:
    """Sonda: lo de arriba no es el valor por defecto."""
    setup = world.declared()
    code, out = admin.order(setup.node_id, "show")
    assert code == ExitCode.OK, out
    assert out["stored"] is False
    enrolled = world.enroll(setup)
    assert enrolled.body["initial_configuration"]["time_sources"] == out["time_sources"]
    assert out["time_sources"] != [LAN_SOURCE]


# --- Auditoría -----------------------------------------------------------------------------------


def test_each_set_leaves_exactly_one_audit_entry_without_free_values(
    world: EnrollmentWorld, admin: Admin
) -> None:
    setup = world.declared()
    for interval in ("60", "120"):
        code, out = admin.order(
            setup.node_id, "set", "--time-source", LAN_SOURCE, "--heartbeat-interval", interval
        )
        assert code == ExitCode.OK, out
    rows = admin.audited(setup.node_id)
    assert len(rows) == 2
    last = rows[-1]
    authz = world.fleet.authz
    assert last["organization_id"] == authz.provider_organization_id
    assert last["actor_id"] == authz.operator_id
    assert last["resource_kind"] == "node"
    assert last["outcome"] == "success"
    assert json.loads(last["filters"]) == {
        "organization_id": str(setup.organization_id),
        "plant_id": str(setup.plant_id),
        "time_sources_count": 1,
        "sent_records_retention_days": 30,
        "token_max_age_seconds": 600,
        "heartbeat_interval_seconds": 120,
        "mute_after_seconds": 600,
    }


@pytest.mark.parametrize(
    ("arguments", "field"),
    [
        (
            tuple(item for i in range(9) for item in ("--time-source", f"ntp{i}")),
            "time_sources",
        ),
        (("--heartbeat-interval", "10"), "heartbeat_interval_seconds"),
        (("--token-max-age", "500"), "token_max_age_seconds"),
    ],
)
def test_a_rejected_value_names_the_field_and_leaves_no_trace(
    world: EnrollmentWorld, admin: Admin, arguments: Sequence[str], field: str
) -> None:
    setup = world.declared()
    code, out = admin.order(setup.node_id, "set", *arguments)
    assert code == ExitCode.REJECTED
    assert out["error"] == "invalid_value"
    assert out["campo"] == field
    assert admin.row(setup.node_id) is None
    assert admin.audited(setup.node_id) == []


def test_a_node_that_does_not_exist_is_not_found(admin: Admin) -> None:
    code, out = admin.order(uuid.uuid4(), "set", "--heartbeat-interval", "60")
    assert code == ExitCode.REJECTED
    assert out["error"] == "not_found"


def test_a_dry_run_writes_nothing(world: EnrollmentWorld, admin: Admin) -> None:
    setup = world.declared()
    code, out = admin.order(setup.node_id, "set", "--heartbeat-interval", "120", "--dry-run")
    assert code == ExitCode.OK, out
    assert out["mute_after_seconds"] == 600
    assert admin.row(setup.node_id) is None
    assert admin.audited(setup.node_id) == []


# --- Concurrencia --------------------------------------------------------------------------------


def test_concurrent_changes_on_a_node_without_a_row_neither_collide_nor_overwrite(
    world: EnrollmentWorld, admin: Admin
) -> None:
    """Candado: dos operadores nombran campos distintos a la vez y los dos cambios quedan."""
    writer = admin.writer()
    operator = admin.operator()

    async def race(node: uuid.UUID) -> list[Any]:
        return await asyncio.gather(
            writer.set_node_configuration(
                node, NodeConfigurationChanges(heartbeat_interval_seconds=120), operator
            ),
            writer.set_node_configuration(
                node, NodeConfigurationChanges(sent_records_retention_days=45), operator
            ),
            writer.set_node_configuration(
                node, NodeConfigurationChanges(token_max_age_seconds=900), operator
            ),
            writer.set_node_configuration(
                node, NodeConfigurationChanges(time_sources=(LAN_SOURCE,)), operator
            ),
        )

    for _ in range(ROUNDS):
        setup = world.declared()
        world.fleet.run(race(setup.node_id))
        row = admin.row(setup.node_id)
        assert row["heartbeat_interval_seconds"] == 120
        assert row["mute_after_seconds"] == 600
        assert row["sent_records_retention_days"] == 45
        assert row["token_max_age_seconds"] == 900
        assert json.loads(row["time_sources"]) == [LAN_SOURCE]
        assert len(admin.audited(setup.node_id)) == 4


# --- Latido --------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[HeartbeatStack]:
    with heartbeat_stack(postgres_endpoint, "node_configuration_latido") as built:
        yield built


@pytest.mark.parametrize(("interval", "mute"), [(60, 300), (120, 600)])
def test_the_heartbeat_answers_with_the_row(
    stack: HeartbeatStack, interval: int, mute: int
) -> None:
    """Con 120 s no puede ser el valor por defecto; con 60 s es el criterio de aceptación."""
    site = stack.site()
    admin = Admin(stack.authz, stack.primary.database)
    code, out = admin.order(
        site.node_id, "set", "--time-source", LAN_SOURCE, "--heartbeat-interval", str(interval)
    )
    assert code == ExitCode.OK, out
    response = stack.post(site, stack.body(site))
    assert response.status_code == 200, response.text
    document = response.json()
    assert document["heartbeat_interval_seconds"] == interval
    assert document["mute_after_seconds"] == mute
