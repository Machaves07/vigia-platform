"""Escritor de ``fleet.node_configuration`` y sus órdenes, sin base (TASK-544; D-11; A-04, A-09).

Un doble de la base reconoce las cuatro sentencias del módulo. Contra PostgreSQL real, el alta, el
latido, la auditoría y la concurrencia: ``tests/integration/test_admin_node_configuration.py``.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import types
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any, Final

import pytest

from vigia_platform.fleet.application.node_configuration_write import (
    NodeConfigurationChanges,
    NodeConfigurationWriter,
    NodeScope,
    audit_filters,
    merged_configuration,
)
from vigia_platform.fleet.domain.node_configuration import (
    MUTE_FACTOR,
    NodeConfiguration,
    NodeConfigurationInvalid,
)
from vigia_platform.identity.application.admin_cli import ExitCode, run
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.ledger.application.audit_writer import AuditOperation, AuditReceipt
from vigia_platform.shared.context import (
    Actor,
    ActorKind,
    ActorUnit,
    ContextOrigin,
    ScopeContext,
    _seal_scope_context,
)

PROVIDER: Final = uuid.UUID("0192f0c4-0000-7000-8000-000000000001")
ORGANIZATION: Final = uuid.UUID("0192f0c4-0000-7000-8000-000000000002")
PLANT: Final = uuid.UUID("0192f0c4-0000-7000-8000-000000000003")
NODE: Final = uuid.UUID("0192f0c4-0000-7000-8000-000000000004")
OPERATOR: Final = uuid.UUID("0192f0c4-0000-7000-8000-000000000005")
NOW: Final = dt.datetime(2026, 11, 6, 9, 30, tzinfo=dt.UTC)
LAN_SOURCE: Final = "192.168.20.69"


class _Clock:
    def now(self) -> dt.datetime:
        return NOW


def _row(**values: Any) -> types.SimpleNamespace:
    return types.SimpleNamespace(**values)


class FakeDatabase:
    """La fila del nodo en memoria; ``statements`` lleva qué sentencia corrió y con qué valores."""

    def __init__(self, *, node_exists: bool = True, row: dict[str, Any] | None = None) -> None:
        self.node_exists = node_exists
        self.row = row
        self.statements: list[tuple[str, dict[str, Any]]] = []
        self.rolled_back = 0

    @asynccontextmanager
    async def transaction(self, _context: Any) -> AsyncIterator[FakeDatabase]:
        before = None if self.row is None else dict(self.row)
        try:
            yield self
        except BaseException:
            self.rolled_back += 1
            self.row = before
            raise

    async def execute(self, statement: Any, values: dict[str, Any]) -> Any:
        sql = str(statement)
        name = sql.split()[0]
        self.statements.append((name, values))
        if "vigia_node_enrollment_scope" in sql:
            return types.SimpleNamespace(
                first=lambda: (
                    _row(node_id=NODE, organization_id=ORGANIZATION, plant_id=PLANT)
                    if self.node_exists
                    else None
                )
            )
        if name == "INSERT":
            if self.row is None:
                self.row = _db_values(values)
            return None
        if name == "UPDATE":
            self.row = _db_values(values)
            return None
        return types.SimpleNamespace(first=lambda: None if self.row is None else _row(**self.row))

    def names(self) -> list[str]:
        return [name for name, _ in self.statements]


def _db_values(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "time_sources": values["time_sources"],
        "sent_records_retention_days": values["sent_records_retention_days"],
        "token_max_age_seconds": values["token_max_age_seconds"],
        "heartbeat_interval_seconds": values["heartbeat_interval_seconds"],
        "mute_after_seconds": values["mute_after_seconds"],
    }


class FakeContexts:
    def __init__(self) -> None:
        self.organizations: list[uuid.UUID] = []

    def context_for_organization(self, _task: Any, organization_id: uuid.UUID) -> Any:
        self.organizations.append(organization_id)
        return _operator_context(
            kind=ActorKind.SYSTEM,
            origin=ContextOrigin.PERIODIC_ITERATION,
            organization_id=organization_id,
        )

    async def context_from_operator(self, _operator: uuid.UUID) -> Any:
        return _operator_context()


class FakeAudit:
    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails
        self.entries: list[dict[str, Any]] = []

    async def append(self, _context: Any, operation: Any, **fields: Any) -> AuditReceipt:
        if self.fails:
            raise RuntimeError("auditoría caída")
        self.entries.append({"operation": operation, **fields})
        return AuditReceipt(uuid.uuid4(), len(self.entries), NOW)


def _operator_context(
    *,
    kind: ActorKind = ActorKind.OPERATOR,
    origin: ContextOrigin = ContextOrigin.ADMIN_COMMAND,
    organization_id: uuid.UUID = PROVIDER,
) -> ScopeContext:
    return _seal_scope_context(
        organization_id=organization_id,
        actor=Actor(kind=kind, id=OPERATOR, display_name_snapshot="Operadora", unit=ActorUnit.U02),
        origin=origin,
        allowed_scopes=(),
        correlation_id=OPERATOR,
    )


def _writer(database: FakeDatabase, audit: FakeAudit | None = None) -> NodeConfigurationWriter:
    return NodeConfigurationWriter(
        database=database,
        contexts=FakeContexts(),
        audit=audit if audit is not None else FakeAudit(),
        clock=_Clock(),
        provider_organization_id=PROVIDER,
    )


def _set(writer: NodeConfigurationWriter, **changes: Any) -> Any:
    dry_run = changes.pop("dry_run", False)
    return asyncio.run(
        writer.set_node_configuration(
            NODE, NodeConfigurationChanges(**changes), _operator_context(), dry_run=dry_run
        )
    )


# --- La validación es la del dominio -------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"time_sources": tuple(f"ntp{i}" for i in range(9))}, "time_sources"),
        ({"time_sources": ()}, "time_sources"),
        ({"time_sources": ("con espacio",)}, "time_sources"),
        ({"time_sources": ("",)}, "time_sources"),
        ({"heartbeat_interval_seconds": 14}, "heartbeat_interval_seconds"),
        ({"heartbeat_interval_seconds": 10}, "heartbeat_interval_seconds"),
        ({"heartbeat_interval_seconds": 601}, "heartbeat_interval_seconds"),
        ({"sent_records_retention_days": 0}, "sent_records_retention_days"),
        ({"sent_records_retention_days": 91}, "sent_records_retention_days"),
        ({"token_max_age_seconds": 599}, "token_max_age_seconds"),
        ({"token_max_age_seconds": 500}, "token_max_age_seconds"),
        ({"token_max_age_seconds": 3_601}, "token_max_age_seconds"),
    ],
)
def test_the_domain_names_the_field_it_rejects(changes: dict[str, Any], field: str) -> None:
    with pytest.raises(NodeConfigurationInvalid) as invalid:
        merged_configuration(_defaults(), NodeConfigurationChanges(**changes))
    assert invalid.value.field == field


@pytest.mark.parametrize(
    "changes",
    [
        {"time_sources": tuple(f"ntp{i}" for i in range(8))},
        {"time_sources": (LAN_SOURCE,)},
        {"heartbeat_interval_seconds": 15},
        {"heartbeat_interval_seconds": 600},
        {"sent_records_retention_days": 1},
        {"sent_records_retention_days": 90},
        {"token_max_age_seconds": 600},
        {"token_max_age_seconds": 3_600},
    ],
)
def test_the_edges_are_accepted(changes: dict[str, Any]) -> None:
    configuration = merged_configuration(_defaults(), NodeConfigurationChanges(**changes))
    assert configuration.mute_after_seconds == (
        MUTE_FACTOR * configuration.heartbeat_interval_seconds
    )


def _defaults() -> dict[str, Any]:
    defaults = NodeConfiguration()
    return {
        "time_sources": defaults.time_sources,
        "sent_records_retention_days": defaults.sent_records_retention_days,
        "token_max_age_seconds": defaults.token_max_age_seconds,
        "heartbeat_interval_seconds": defaults.heartbeat_interval_seconds,
    }


def test_what_the_order_does_not_name_keeps_its_value() -> None:
    current = {**_defaults(), "sent_records_retention_days": 45, "token_max_age_seconds": 900}
    configuration = merged_configuration(
        current, NodeConfigurationChanges(heartbeat_interval_seconds=120)
    )
    assert configuration.sent_records_retention_days == 45
    assert configuration.token_max_age_seconds == 900
    assert configuration.heartbeat_interval_seconds == 120
    assert configuration.mute_after_seconds == 600


def test_the_audit_filters_carry_no_free_value() -> None:
    node = NodeScope(NODE, ORGANIZATION, PLANT)
    filters = audit_filters(
        node, replace(NodeConfiguration(), time_sources=(LAN_SOURCE, "ntp.planta"))
    )
    assert filters == {
        "organization_id": str(ORGANIZATION),
        "plant_id": str(PLANT),
        "time_sources_count": 2,
        "sent_records_retention_days": 30,
        "token_max_age_seconds": 600,
        "heartbeat_interval_seconds": 60,
        "mute_after_seconds": 300,
    }


# --- El caso de uso ------------------------------------------------------------------------------


def test_set_writes_the_row_and_one_audit_entry_in_the_organization_of_the_node() -> None:
    database, audit = FakeDatabase(), FakeAudit()
    contexts = FakeContexts()
    writer = NodeConfigurationWriter(
        database=database,
        contexts=contexts,
        audit=audit,
        clock=_Clock(),
        provider_organization_id=PROVIDER,
    )
    result = asyncio.run(
        writer.set_node_configuration(
            NODE,
            NodeConfigurationChanges(time_sources=(LAN_SOURCE,), heartbeat_interval_seconds=120),
            _operator_context(),
        )
    )
    assert result.configuration.mute_after_seconds == 600
    assert result.receipt is not None and result.stored
    assert contexts.organizations == [PROVIDER, ORGANIZATION]
    assert database.names() == ["SELECT", "INSERT", "SELECT", "UPDATE"]
    assert database.row is not None
    assert json.loads(database.row["time_sources"]) == [LAN_SOURCE]
    assert database.row["mute_after_seconds"] == 600
    (entry,) = audit.entries
    assert entry["operation"] is AuditOperation.NODE_CONFIGURATION_CHANGED
    assert entry["resource"].kind == "node"
    assert entry["resource"].id == NODE


def test_a_rejected_value_writes_nothing_and_audits_nothing() -> None:
    database, audit = FakeDatabase(), FakeAudit()
    with pytest.raises(NodeConfigurationInvalid):
        _set(_writer(database, audit), heartbeat_interval_seconds=10)
    assert database.row is None
    assert audit.entries == []


def test_a_failed_audit_undoes_the_change() -> None:
    database = FakeDatabase()
    with pytest.raises(RuntimeError):
        _set(_writer(database, FakeAudit(fails=True)), heartbeat_interval_seconds=120)
    assert database.rolled_back == 1
    assert database.row is None


def test_a_dry_run_validates_and_writes_nothing() -> None:
    database, audit = FakeDatabase(), FakeAudit()
    result = _set(_writer(database, audit), heartbeat_interval_seconds=120, dry_run=True)
    assert result.configuration.heartbeat_interval_seconds == 120
    assert result.receipt is None and not result.stored
    assert database.names() == ["SELECT", "SELECT"]
    assert audit.entries == []


def test_a_dry_run_still_rejects_with_the_domain() -> None:
    with pytest.raises(NodeConfigurationInvalid):
        _set(_writer(FakeDatabase()), token_max_age_seconds=500, dry_run=True)


def test_a_stored_row_is_the_base_of_the_change_with_either_jsonb_decoding() -> None:
    stored = {
        "sent_records_retention_days": 45,
        "token_max_age_seconds": 900,
        "heartbeat_interval_seconds": 120,
        "mute_after_seconds": 600,
    }
    for sources in (json.dumps([LAN_SOURCE]), [LAN_SOURCE]):
        database = FakeDatabase(row={"time_sources": sources, **stored})
        result = _set(_writer(database), sent_records_retention_days=60)
        assert result.configuration.time_sources == (LAN_SOURCE,)
        assert result.configuration.heartbeat_interval_seconds == 120
        assert result.configuration.sent_records_retention_days == 60


def test_show_without_a_row_is_the_default_of_d11() -> None:
    database = FakeDatabase()
    result = asyncio.run(_writer(database).show(NODE, _operator_context()))
    assert not result.stored
    assert result.configuration == NodeConfiguration()


def test_show_reads_the_row() -> None:
    database = FakeDatabase(
        row={
            "time_sources": [LAN_SOURCE],
            "sent_records_retention_days": 45,
            "token_max_age_seconds": 900,
            "heartbeat_interval_seconds": 120,
            "mute_after_seconds": 600,
        }
    )
    result = asyncio.run(_writer(database).show(NODE, _operator_context()))
    assert result.stored
    assert result.configuration.time_sources == (LAN_SOURCE,)
    assert result.configuration.mute_after_seconds == 600


@pytest.mark.parametrize("action", ["show", "set"])
def test_a_node_that_does_not_exist_is_not_found(action: str) -> None:
    writer = _writer(FakeDatabase(node_exists=False))
    with pytest.raises(ResourceNotFound):
        if action == "show":
            asyncio.run(writer.show(NODE, _operator_context()))
        else:
            _set(writer, heartbeat_interval_seconds=120)


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": ActorKind.USER},
        {"origin": ContextOrigin.PERIODIC_ITERATION},
        {"organization_id": ORGANIZATION},
    ],
)
@pytest.mark.parametrize("action", ["show", "set"])
def test_only_an_operator_of_the_provider_reaches_the_row(
    changes: dict[str, Any], action: str
) -> None:
    database = FakeDatabase()
    writer = _writer(database)
    actor = _operator_context(**changes)
    with pytest.raises(ResourceNotFound):
        if action == "show":
            asyncio.run(writer.show(NODE, actor))
        else:
            asyncio.run(
                writer.set_node_configuration(
                    NODE, NodeConfigurationChanges(heartbeat_interval_seconds=120), actor
                )
            )
    assert database.statements == []


# --- La orden ------------------------------------------------------------------------------------


def _runtime(database: FakeDatabase, audit: FakeAudit) -> Any:
    return types.SimpleNamespace(
        database=_Disposable(database),
        contexts=FakeContexts(),
        audit=audit,
        clock=_Clock(),
    )


class _Disposable:
    def __init__(self, database: FakeDatabase) -> None:
        self._database = database

    def transaction(self, context: Any) -> Any:
        return self._database.transaction(context)

    async def dispose(self) -> None:
        return None


def _vigia_admin(database: FakeDatabase, audit: FakeAudit, *argv: str) -> tuple[int, Any, Any]:
    async def builder(_config: Any, _provider: uuid.UUID) -> Any:
        return _runtime(database, audit)

    stdout, stderr = io.StringIO(), io.StringIO()
    code = run(
        ["node-configuration", *argv],
        environ={"VIGIA_ENVIRONMENT": "test", "VIGIA_PROVIDER_ORGANIZATION_ID": str(PROVIDER)},
        builder=builder,
        stdin=io.StringIO(),
        stdout=stdout,
        stderr=stderr,
    )
    out = stdout.getvalue().strip()
    err = stderr.getvalue().strip()
    return (
        code,
        json.loads(out) if out else None,
        json.loads(err.splitlines()[-1]) if err else None,
    )


ACTOR: Final = ("--node", str(NODE), "--operator", str(OPERATOR))


def test_set_prints_the_resulting_mute_after_seconds() -> None:
    database, audit = FakeDatabase(), FakeAudit()
    code, out, _ = _vigia_admin(
        database,
        audit,
        "set",
        *ACTOR,
        "--time-source",
        LAN_SOURCE,
        "--heartbeat-interval",
        "60",
        "--json",
    )
    assert code == ExitCode.OK
    assert out["time_sources"] == [LAN_SOURCE]
    assert out["heartbeat_interval_seconds"] == 60
    assert out["mute_after_seconds"] == 300
    assert out["dry_run"] is False
    assert "audit_entry_id" in out
    assert len(audit.entries) == 1


def test_every_flag_reaches_its_field() -> None:
    database, audit = FakeDatabase(), FakeAudit()
    code, out, _ = _vigia_admin(
        database,
        audit,
        "set",
        *ACTOR,
        "--time-source",
        "ntp1",
        "--time-source",
        "ntp2",
        "--retention-days",
        "45",
        "--token-max-age",
        "900",
        "--heartbeat-interval",
        "120",
    )
    assert code == ExitCode.OK
    assert out["time_sources"] == ["ntp1", "ntp2"]
    assert out["sent_records_retention_days"] == 45
    assert out["token_max_age_seconds"] == 900
    assert out["mute_after_seconds"] == 600


@pytest.mark.parametrize(
    ("arguments", "field"),
    [
        (
            tuple(item for i in range(9) for item in ("--time-source", f"ntp{i}")),
            "time_sources",
        ),
        (("--heartbeat-interval", "10"), "heartbeat_interval_seconds"),
        (("--token-max-age", "500"), "token_max_age_seconds"),
        (("--retention-days", "91"), "sent_records_retention_days"),
    ],
)
def test_a_rejected_value_names_the_field(arguments: tuple[str, ...], field: str) -> None:
    database, audit = FakeDatabase(), FakeAudit()
    code, out, err = _vigia_admin(database, audit, "set", *ACTOR, *arguments)
    assert code == ExitCode.REJECTED
    assert out is None
    assert err["error"] == "invalid_value"
    assert err["campo"] == field
    assert database.row is None
    assert audit.entries == []


def test_set_without_any_field_is_rejected() -> None:
    database, audit = FakeDatabase(), FakeAudit()
    code, _, err = _vigia_admin(database, audit, "set", *ACTOR)
    assert code == ExitCode.REJECTED
    assert err["error"] == "invalid_arguments"
    assert database.statements == []


def test_set_dry_run_prints_without_writing() -> None:
    database, audit = FakeDatabase(), FakeAudit()
    code, out, _ = _vigia_admin(
        database, audit, "set", *ACTOR, "--heartbeat-interval", "120", "--dry-run"
    )
    assert code == ExitCode.OK
    assert out["dry_run"] is True
    assert "audit_entry_id" not in out
    assert audit.entries == []


def test_show_prints_the_configuration() -> None:
    database, audit = FakeDatabase(), FakeAudit()
    code, out, _ = _vigia_admin(database, audit, "show", *ACTOR, "--json")
    assert code == ExitCode.OK
    assert out["action"] == "show"
    assert out["stored"] is False
    assert out["mute_after_seconds"] == 300
    assert audit.entries == []
