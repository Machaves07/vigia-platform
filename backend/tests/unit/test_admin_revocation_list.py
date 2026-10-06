"""``vigia-admin regenerate-revocation-list`` y ``revocation-list publish`` (TASK-220, NFR-GOB-21).

Con los dobles de ``tests/admin_support.FakeWorld`` y un regenerador anotado:

- ayuda en español con ``--dry-run``; ``--dry-run`` no publica ni audita;
- publica forzado (sin marca), deja ``revocation_list_regenerated`` con el contexto del operador
  y sale con 0; si no publica (fallo o candado de otro publicador), audita con ``error`` y sale
  con 5;
- sin la clave, ``vigia-edge`` y el almacén configurados, sale con 1 sin tocar nada;
- un ``--operator`` que no es un ``platform_operator`` activo no ejecuta nada;
- la salida y la auditoría llevan identificadores, nunca el PEM ni números de serie.

Solo datos generados.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from tests.admin_support import OPERATOR_ID, FakeWorld, output
from vigia_platform.fleet.application.revocation_list_task import (
    CycleOutcome,
    RevocationListCycle,
)
from vigia_platform.fleet.domain.revocation_list import PublishStep
from vigia_platform.identity.application.admin_cli import AdminConfig, AdminRuntime

NEXT_UPDATE = dt.datetime(2026, 10, 12, 8, 0, tzinfo=dt.UTC)


@dataclass
class RecordingRegenerator:
    result: RevocationListCycle
    calls: list[bool] = field(default_factory=list)

    async def regenerate(self, *, dry_run: bool) -> RevocationListCycle:
        self.calls.append(dry_run)
        if dry_run:
            return dataclasses.replace(self.result, outcome=CycleOutcome.DRY_RUN)
        return self.result


PUBLISHED = RevocationListCycle(
    outcome=CycleOutcome.PUBLISHED,
    crl_number=12,
    entries=3,
    object_version_id="3sL4kqtJlcpXroDTDmJ+rmSpXd3dIbrHY",
    next_update=NEXT_UPDATE,
    mark_cleared=True,
)


class RevocationWorld(FakeWorld):
    def __init__(self, result: RevocationListCycle | None = PUBLISHED) -> None:
        super().__init__()
        self.regenerator = None if result is None else RecordingRegenerator(result)

    async def builder(self, config: AdminConfig, provider_id: uuid.UUID) -> AdminRuntime:
        runtime = await super().builder(config, provider_id)
        return dataclasses.replace(runtime, revocation_list=self.regenerator)


COMMANDS = (("regenerate-revocation-list",), ("revocation-list", "publish"))


@pytest.mark.parametrize("command", COMMANDS)
def test_help_is_in_spanish_with_dry_run(
    command: tuple[str, ...], capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, _ = RevocationWorld().run(*command, "--help")
    help_text = capsys.readouterr().out
    assert code == 0
    assert help_text.startswith(f"uso: vigia-admin {' '.join(command)}")
    assert "--dry-run" in help_text and "sin escribir nada" in help_text


@pytest.mark.parametrize("command", COMMANDS)
def test_dry_run_neither_publishes_nor_audits(command: tuple[str, ...]) -> None:
    world = RevocationWorld()
    code, out, err = world.run(*command, "--operator", str(OPERATOR_ID), "--dry-run")
    assert code == 0, err
    document = output(out)
    assert (document["dry_run"], document["outcome"]) == (True, "dry_run")
    assert world.regenerator is not None and world.regenerator.calls == [True]
    assert world.audit.entries == []


@pytest.mark.parametrize("command", COMMANDS)
def test_it_publishes_without_mark_and_audits_as_the_operator(command: tuple[str, ...]) -> None:
    world = RevocationWorld()
    code, out, err = world.run(*command, "--operator", str(OPERATOR_ID))
    assert code == 0, err
    document = output(out)
    assert document == {
        "command": " ".join(command),
        "dry_run": False,
        "outcome": "published",
        "crl_number": 12,
        "entries": 3,
        "object_version_id": PUBLISHED.object_version_id,
        "next_update": "2026-10-12T08:00:00.000Z",
        "audit_entry_id": document["audit_entry_id"],
    }
    assert world.regenerator is not None and world.regenerator.calls == [False]
    assert world.audit.entries == [
        (
            "revocation_list_regenerated",
            "success",
            {
                "outcome": "published",
                "crl_number": 12,
                "entries": 3,
                "object_version_id": PUBLISHED.object_version_id,
            },
        )
    ]
    assert "operator_row" in world.calls.reads


@pytest.mark.parametrize(
    "result",
    [
        dataclasses.replace(
            PUBLISHED,
            outcome=CycleOutcome.FAILED,
            object_version_id=None,
            failed_step=PublishStep.ADD_REVOCATIONS,
        ),
        RevocationListCycle(outcome=CycleOutcome.BUSY),
    ],
)
def test_not_publishing_audits_the_error_and_exits_with_5(result: RevocationListCycle) -> None:
    world = RevocationWorld(result)
    code, out, err = world.run("regenerate-revocation-list", "--operator", str(OPERATOR_ID))
    assert code == 5
    assert output(out)["outcome"] == result.outcome.value
    assert "revocation_list_not_published" in err
    ((operation, outcome, filters),) = world.audit.entries
    assert (operation, outcome) == ("revocation_list_regenerated", "error")
    if result.failed_step is not None:
        assert filters["failed_step"] == "crl_add_revocation"


def test_without_its_configuration_it_exits_with_1_and_touches_nothing() -> None:
    world = RevocationWorld(None)
    code, _, err = world.run("regenerate-revocation-list", "--operator", str(OPERATOR_ID))
    assert code == 1
    assert "revocation_list_unavailable" in err
    assert world.audit.entries == [] and world.calls.writes == []


def test_an_inactive_operator_runs_nothing() -> None:
    world = RevocationWorld()
    code, _, err = world.run("regenerate-revocation-list", "--operator", str(uuid.uuid4()))
    assert code == 4 and "operator_invalid" in err
    assert world.regenerator is not None and world.regenerator.calls == []
    assert world.audit.entries == []


def test_the_output_carries_no_pem_nor_serials() -> None:
    world = RevocationWorld()
    _, out, _ = world.run("regenerate-revocation-list", "--operator", str(OPERATOR_ID))
    text: Any = out + repr(world.audit.entries)
    assert "BEGIN" not in text and "serial" not in text
