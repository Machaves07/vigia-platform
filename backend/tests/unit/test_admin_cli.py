"""``vigia-admin`` (TASK-132; LC-NUC-07, NFR-NUC-54): ayuda, ``--dry-run``, confirmación y salida.

Con dobles (``tests.admin_support.FakeWorld``) que anotan cada escritura: la base, los secretos,
el depósito ``vigia-edge``, las claves, la bandeja y la auditoría. Los ``ScopeContexts``, la
validación de la génesis y la raíz de la autoridad (con un KMS en memoria) son los reales. Lo
mismo contra PostgreSQL y LocalStack: ``tests/integration/test_admin_bootstrap.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes

from tests.admin_support import (
    LINK_BASE,
    OPERATOR_ID,
    PROVIDER_ID,
    START,
    TOKEN,
    FakeWorld,
    output,
)
from tests.factories import make_context
from tests.properties.test_audit_archive import archive_of, fixed_partition
from vigia_platform.identity.application.admin_cli import (
    BOOTSTRAP_KEY_ORDER,
    RUNTIME_VARIABLE,
    AdminConfig,
    AdminError,
    ExitCode,
    _reject_links,
    build_parser,
    resolve_runtime_builder,
)
from vigia_platform.identity.application.common import IdentityRejected, IdentityRejection
from vigia_platform.identity.application.hierarchy import FirstOperator, ProviderGenesisRequest
from vigia_platform.identity.authz.context import ScopeContexts
from vigia_platform.ledger.application.audit_writer import AuditReceipt
from vigia_platform.shared.archive.restore_drill import DrillResult, RestoreDrills
from vigia_platform.shared.context import ActorKind, ContextOrigin, ScopeContext
from vigia_platform.shared.db import TemporarilyUnavailable, Transaction
from vigia_platform.shared.node_ca import ROOT_CERTIFICATE_KEY
from vigia_platform.shared.signing.keys import SigningPurpose

TEST_SECRET = "vigia/test/bootstrap/invitation"  # noqa: S105 - nombre del secreto, no su valor
PILOT_SECRET = "vigia/pilot/bootstrap/invitation"  # noqa: S105 - nombre del secreto
READS_ONLY = frozenset(
    {"operator_row", "check_key", "first_operator", "archive.get_object", "edge.get_object"}
)
"""Lo único que ``--dry-run`` puede llamar: lecturas."""

BOOTSTRAP = (
    "bootstrap",
    "--organization-code",
    "VIGIA-PROV",
    "--organization-name",
    "Proveedor sintético",
    "--operator-email",
    "operadora@example.test",
    "--operator-name",
    "Operadora sintética",
)
CREATE = (
    "create-organization",
    "--operator",
    str(OPERATOR_ID),
    "--code",
    "CLIENTE-1",
    "--name",
    "Cliente sintético",
    "--plant-code",
    "PLANTA-1",
    "--plant-name",
    "Planta sintética",
    "--country",
    "CO",
    "--data-region",
    "us-east-1",
    "--timezone",
    "America/Bogota",
    "--admin-email",
    "admin@example.test",
    "--admin-name",
    "Administradora sintética",
)


def _log_text(caplog: pytest.LogCaptureFixture) -> str:
    """Todo lo que llegó a los registros: mensajes, campos y trazas."""
    parts: list[str] = []
    for record in caplog.records:
        parts.append(record.getMessage())
        parts.append(repr(record.__dict__))
    return "\n".join(parts)


# --- Ayuda y uso --------------------------------------------------------------------------------


def test_help_is_in_spanish(capsys: pytest.CaptureFixture[str]) -> None:
    world = FakeWorld()
    code, _, _ = world.run("--help")
    help_text = capsys.readouterr().out
    assert code == 0
    assert help_text.startswith("uso: vigia-admin")
    assert "muestra esta ayuda" in help_text and "órdenes" in help_text
    for english in ("usage:", "show this help", "positional arguments", "options:"):
        assert english not in help_text


@pytest.mark.parametrize(
    "command",
    [
        "bootstrap",
        "create-organization",
        "rotate-key",
        "rotate-node-ca",
        "replay-dead-letter",
        "create-partitions",
        "restore-audit-partition",
        "record-restore-drill",
    ],
)
def test_every_command_has_spanish_help_and_dry_run(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, _ = FakeWorld().run(command, "--help")
    help_text = capsys.readouterr().out
    assert code == 0
    assert help_text.startswith(f"uso: vigia-admin {command}")
    assert "--dry-run" in help_text and "sin escribir nada" in help_text
    assert "show this help" not in help_text


def test_usage_errors_are_in_spanish(capsys: pytest.CaptureFixture[str]) -> None:
    world = FakeWorld()
    code, _, _ = world.run("create-organization", "--dry-run")
    err = capsys.readouterr().err
    assert code == ExitCode.USAGE
    assert "faltan argumentos obligatorios" in err
    code, _, _ = world.run("rotate-key", "secreto", "--operator", str(OPERATOR_ID))
    assert code == ExitCode.USAGE
    assert "opción no válida" in capsys.readouterr().err
    code, _, _ = world.run("create-partitions", "--until", "2026-13", "--operator", "x")
    assert code == ExitCode.USAGE
    assert world.calls.writes == []


def test_runtime_builder_is_closed_to_the_package() -> None:
    for reference in (None, "os:system", "vigia_platform:main", "vigia_platform.x:Y"):
        with pytest.raises(ValueError, match=RUNTIME_VARIABLE):
            resolve_runtime_builder(reference)
    with pytest.raises(ValueError, match="no nombra una función"):
        resolve_runtime_builder("vigia_platform.identity.application.admin_cli:nothing")


def test_configuration_is_strict() -> None:
    config = AdminConfig.from_environ({"VIGIA_ENVIRONMENT": "pilot"})
    assert config.invitation_secret == PILOT_SECRET
    assert config.provider_organization_id is None
    for environ in (
        {"VIGIA_ENVIRONMENT": "produccion"},
        {"VIGIA_ENVIRONMENT": "pilot", "VIGIA_PUBLIC_ORIGIN": "http://app.example"},
        {"VIGIA_ENVIRONMENT": "pilot", "VIGIA_PROVIDER_ORGANIZATION_ID": "no-uuid"},
        {"VIGIA_ENVIRONMENT": "pilot", "VIGIA_EDGE_BUCKET": "Mayúsculas"},
    ):
        with pytest.raises(ValueError):
            AdminConfig.from_environ(environ)


def test_invalid_configuration_fails_without_building_anything() -> None:
    world = FakeWorld()
    code, out, err = world.run(
        *CREATE, "--dry-run", environ=world.environ | {"VIGIA_ENVIRONMENT": "x"}
    )
    assert code == ExitCode.FAILURE and out == ""
    assert json.loads(err)["error"] == "config_invalid"
    assert world.provider_ids == []


# --- bootstrap ---------------------------------------------------------------------------------


def test_bootstrap_dry_run_writes_nothing() -> None:
    world = FakeWorld()
    code, out, _ = world.run(*BOOTSTRAP, "--dry-run")
    assert code == 0
    document = output(out)
    assert document["dry_run"] is True
    assert document["provider_organization_code"] == "VIGIA-PROV"
    assert world.calls.writes == []
    assert set(world.calls.reads) <= READS_ONLY
    assert world.storage.puts == [] and world.secrets.values == {}


def test_bootstrap_creates_everything_and_never_prints_the_link(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    world = FakeWorld()
    code, out, err = world.run(*BOOTSTRAP, "--yes")
    assert code == 0, err
    document = output(out)
    provider_id = uuid.UUID(document["provider_organization_id"])
    assert world.provider_ids == [provider_id] and provider_id != PROVIDER_ID
    # El enlace solo está en el secreto de un solo uso.
    (stored,) = world.secrets.values[TEST_SECRET]
    secret = json.loads(stored)
    assert secret["link"] == f"{LINK_BASE}/invitacion#{TOKEN}"
    assert secret["user_id"] == document["operator_user_id"]
    for text_ in (out, err, _log_text(caplog)):
        assert TOKEN not in text_ and "invitacion#" not in text_ and "://" not in text_
        assert "contraseña" not in text_.lower() and "password" not in text_.lower()
    assert document["invitation_secret"] == TEST_SECRET
    # Claves: key_set primero; una por propósito, atribuidas al operador.
    assert tuple(world.signing.rotated) == BOOTSTRAP_KEY_ORDER
    assert set(document["signing_keys"]) == {purpose.value for purpose in SigningPurpose}
    assert {key.rotated_by for key in world.signing.keys} == {
        uuid.UUID(document["operator_user_id"])
    }
    # Raíz: solo ca/root.pem.
    assert world.storage.puts == [ROOT_CERTIFICATE_KEY]
    root = x509.load_pem_x509_certificate(world.storage.objects[ROOT_CERTIFICATE_KEY])
    assert document["root_sha256"] == root.fingerprint(hashes.SHA256()).hex()
    assert world.calls.writes.index("synchronize") < world.calls.writes.index(
        "create_provider_organization"
    )


def test_bootstrap_requires_confirmation() -> None:
    world = FakeWorld()
    code, out, err = world.run(*BOOTSTRAP, stdin="OTRO\n")
    assert code == ExitCode.NOT_CONFIRMED and out == ""
    assert json.loads(err.splitlines()[-1].split(": ", 1)[-1])["error"] == "not_confirmed"
    assert world.calls.writes == []
    code, out, _ = world.run(*BOOTSTRAP, stdin="VIGIA-PROV\n")
    assert code == 0
    assert output(out)["dry_run"] is False


def test_bootstrap_validates_before_touching_anything() -> None:
    world = FakeWorld()
    arguments = list(BOOTSTRAP)
    arguments[arguments.index("operadora@example.test")] = "no-es-un-correo"
    code, _, err = world.run(*arguments, "--yes")
    assert code == ExitCode.REJECTED
    assert json.loads(err) | {"mensaje": ""} == {
        "error": "invalid_value",
        "campo": "/email",
        "mensaje": "",
    }
    assert world.calls.writes == []


def test_bootstrap_refuses_a_node_ca_key_that_is_not_p256() -> None:
    from cryptography.hazmat.primitives.asymmetric import ec

    world = FakeWorld()
    world.kms.add("vigia-node-ca", ec.SECP384R1())
    code, _, err = world.run(*BOOTSTRAP, "--yes")
    assert code == ExitCode.REJECTED
    assert json.loads(err)["error"] == "node_ca_invalid"
    assert world.calls.writes == []


def test_bootstrap_without_node_ca_configuration_fails_closed() -> None:
    world = FakeWorld()
    environ = {k: v for k, v in world.environ.items() if k != "VIGIA_NODE_CA_KEY_ARN"}
    code, _, err = world.run(*BOOTSTRAP, "--yes", environ=environ)
    assert code == ExitCode.FAILURE
    assert json.loads(err)["error"] == "node_ca_unavailable"
    assert world.calls.writes == []


def test_bootstrap_resume_completes_only_what_is_missing() -> None:
    world = FakeWorld()
    world.genesis.operator = FirstOperator(OPERATOR_ID, "Operadora", "o@example.test", "invited")
    code, out, _ = world.run("bootstrap", "--resume", "--dry-run")
    assert code == 0 and world.calls.writes == []
    assert output(out)["would_publish_root"] is False
    code, out, err = world.run("bootstrap", "--resume", "--yes")
    assert code == 0, err
    document = output(out)
    assert document["operator_user_id"] == str(OPERATOR_ID)
    assert "reissue_operator_invitation" in world.calls.writes
    assert len(world.secrets.values[TEST_SECRET]) == 1
    assert world.storage.puts == []  # sin --publish-root no toca la raíz
    # Una segunda vuelta no crea claves: ya hay una activa por propósito.
    world.genesis.operator = FirstOperator(OPERATOR_ID, "Operadora", "o@example.test", "active")
    world.calls.writes.clear()
    code, out, _ = world.run("bootstrap", "--resume", "--yes", "--publish-root")
    assert code == 0
    assert not [w for w in world.calls.writes if w.startswith("rotate:")]
    assert "reissue_operator_invitation" not in world.calls.writes
    assert world.storage.puts == [ROOT_CERTIFICATE_KEY]


def test_publish_root_needs_resume() -> None:
    world = FakeWorld()
    code, _, err = world.run(*BOOTSTRAP, "--publish-root", "--yes")
    assert code == ExitCode.REJECTED and json.loads(err)["error"] == "invalid_arguments"
    assert world.calls.writes == []


# --- create-organization --------------------------------------------------------------------------


def test_create_organization_dry_run_writes_nothing() -> None:
    world = FakeWorld()
    code, out, err = world.run(*CREATE, "--dry-run")
    assert code == 0, err
    document = output(out)
    assert document["dry_run"] is True
    assert document["organization_code"] == "CLIENTE-1"
    assert world.calls.writes == [] and world.synchronized == 0
    assert set(world.calls.reads) <= READS_ONLY
    assert world.secrets.values == {}


def test_create_organization_dry_run_still_validates() -> None:
    world = FakeWorld()
    arguments = list(CREATE)
    arguments[arguments.index("CO")] = "colombia"
    code, out, err = world.run(*arguments, "--dry-run")
    assert code == ExitCode.REJECTED and out == ""
    assert json.loads(err)["campo"] == "/country"
    code, _, err = world.run(
        *CREATE, "--dry-run", "--concession-max-days", "5", "--concession-default-days", "6"
    )
    assert code == ExitCode.REJECTED
    assert world.calls.writes == []


def test_create_organization_needs_an_active_operator() -> None:
    world = FakeWorld()
    world.store.active.clear()
    code, _, err = world.run(*CREATE, "--dry-run")
    assert code == ExitCode.REJECTED
    assert json.loads(err)["error"] == "operator_invalid"
    code, _, _ = world.run(*CREATE, "--yes")
    assert code == ExitCode.REJECTED
    assert world.calls.writes == []


def test_create_organization_requires_typing_the_code() -> None:
    world = FakeWorld()
    code, _, _ = world.run(*CREATE)  # sin consola: no hay confirmación
    assert code == ExitCode.NOT_CONFIRMED and world.calls.writes == []
    code, out, err = world.run(*CREATE, stdin="CLIENTE-1\n")
    assert code == 0, err
    document = output(out)
    assert {"organization_id", "plant_id", "administrator_user_id"} <= set(document)
    assert world.calls.writes == ["synchronize", "create_client_organization", "put_one_time"]
    assert TOKEN not in out and TOKEN not in err


# --- Órdenes del operador -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [
        ("rotate-key", "catalog"),
        ("replay-dead-letter", str(uuid.uuid4()), "u02_alerts"),
        ("create-partitions", "--until", "2027-03"),
        ("record-restore-drill", "--result", "ok"),
    ],
)
def test_operator_commands_dry_run_write_nothing(arguments: tuple[str, ...]) -> None:
    world = FakeWorld()
    code, out, err = world.run(*arguments, "--operator", str(OPERATOR_ID), "--dry-run")
    assert code == 0, err
    assert output(out)["dry_run"] is True
    assert world.calls.writes == []
    assert set(world.calls.reads) <= READS_ONLY


@pytest.mark.parametrize(
    ("arguments", "write"),
    [
        (("rotate-key", "gate"), "rotate:gate"),
        (("replay-dead-letter", str(uuid.uuid4()), "u02_alerts"), "replay"),
        (("create-partitions", "--until", "2027-03"), "partitions.create"),
        (("record-restore-drill", "--result", "failed"), "drill:failed"),
    ],
)
def test_operator_commands_call_the_service_as_the_operator(
    arguments: tuple[str, ...], write: str
) -> None:
    world = FakeWorld()
    code, out, err = world.run(*arguments, "--operator", str(OPERATOR_ID))
    assert code == 0, err
    assert write in world.calls.writes
    assert "operator_row" in world.calls.reads
    assert output(out)["dry_run"] is False
    # Un operador que no está activo no ejecuta nada.
    other = FakeWorld()
    code, _, err = other.run(*arguments, "--operator", str(uuid.uuid4()))
    assert code == ExitCode.REJECTED and json.loads(err)["error"] == "operator_invalid"
    assert other.calls.writes == []


def test_rotate_key_is_authorized_with_its_permission() -> None:
    world = FakeWorld()
    code, out, _ = world.run("rotate-key", "checkpoint", "--operator", str(OPERATOR_ID))
    assert code == 0
    assert "authorize:platform.keys.rotate" in world.calls.reads
    assert output(out)["purpose"] == "checkpoint"


@pytest.mark.parametrize(("until", "message"), [("2026-09", "anterior"), ("2036-10", "120 meses")])
def test_create_partitions_bounds(until: str, message: str) -> None:
    world = FakeWorld()  # el reloj está en 2026-10
    code, _, err = world.run("create-partitions", "--until", until, "--operator", str(OPERATOR_ID))
    assert code == ExitCode.REJECTED and message in json.loads(err)["mensaje"]
    assert world.calls.writes == []
    code, _, _ = world.run(
        "create-partitions", "--until", "2036-09", "--operator", str(OPERATOR_ID), "--dry-run"
    )
    assert code == 0


def test_replay_rejects_consumer_names_outside_the_registry_pattern() -> None:
    world = FakeWorld()
    code, _, err = world.run(
        "replay-dead-letter", str(uuid.uuid4()), "U02 Alerts", "--operator", str(OPERATOR_ID)
    )
    assert code == ExitCode.REJECTED and json.loads(err)["error"] == "invalid_value"
    assert world.calls.writes == []


def test_commands_other_than_bootstrap_need_the_provider_id() -> None:
    world = FakeWorld()
    environ = {k: v for k, v in world.environ.items() if k != "VIGIA_PROVIDER_ORGANIZATION_ID"}
    code, _, err = world.run(*CREATE, "--dry-run", environ=environ)
    assert code == ExitCode.FAILURE and json.loads(err)["error"] == "provider_unknown"
    assert world.provider_ids == []


# --- rotate-node-ca -------------------------------------------------------------------------------


def test_rotate_node_ca_dry_run_and_rotation() -> None:
    world = FakeWorld()
    code, _, err = world.run(*BOOTSTRAP, "--yes")
    assert code == 0, err
    world.calls.writes.clear()
    code, out, _ = world.run("rotate-node-ca", "--new-key-id", "vigia-node-ca-2", "--dry-run")
    assert code == 0 and output(out)["dry_run"] is True
    assert world.calls.writes == []
    code, _, err = world.run("rotate-node-ca", "--new-key-id", "vigia-node-ca")
    assert code == ExitCode.REJECTED and json.loads(err)["error"] == "invalid_value"
    code, out, err = world.run("rotate-node-ca", "--new-key-id", "vigia-node-ca-2", "--yes")
    assert code == 0, err
    document = output(out)
    assert len(document["root_bundle_sha256"]) == 2
    assert world.calls.writes == ["rotate_root", "edge.put_object"]
    assert "edge.get_object" in world.calls.reads  # la raíz vigente sale del depósito
    bundle = x509.load_pem_x509_certificates(world.storage.objects[ROOT_CERTIFICATE_KEY])
    fingerprints = [certificate.fingerprint(hashes.SHA256()).hex() for certificate in bundle]
    assert fingerprints == document["root_bundle_sha256"]


# --- restore-audit-partition --------------------------------------------------------------------


def test_restore_audit_partition_verifies_and_extracts(tmp_path: Path) -> None:
    import hashlib

    data = archive_of(fixed_partition())
    digest = hashlib.sha256(data).hexdigest()
    world = FakeWorld(archive_data=data)
    target = tmp_path / "restaurada"
    code, out, _ = world.run(
        "restore-audit-partition", "audit/x.zip", "--sha256", digest, "--output", str(target),
        "--dry-run",
    )  # fmt: skip
    assert code == 0 and not target.exists()
    assert world.calls.reads == []
    code, out, err = world.run(
        "restore-audit-partition", "audit/x.zip", "--sha256", digest, "--output", str(target)
    )
    assert code == 0, err
    document = output(out)
    # fixed_partition: dos cadenas de 3 y 2 entradas; manifiesto, verificador y 2 por cadena.
    assert document["entry_count"] == 5 and document["files"] == 6
    assert len(document["organizations"]) == 2
    assert (target / "vigia_verify.py").is_file()
    assert world.calls.writes == []  # solo lectura
    # El directorio ya existe, el resumen no coincide o el archivo está alterado: no se extrae.
    code, _, err = world.run(
        "restore-audit-partition", "audit/x.zip", "--sha256", digest, "--output", str(target)
    )
    assert code == ExitCode.REJECTED
    other = tmp_path / "otra"
    code, _, err = world.run(
        "restore-audit-partition", "audit/x.zip", "--sha256", "0" * 64, "--output", str(other)
    )
    assert code == ExitCode.REJECTED and json.loads(err)["error"] == "archive_invalid"
    assert not other.exists()
    code, _, err = world.run(
        "restore-audit-partition", "audit/x.zip", "--sha256", "ABC", "--output", str(other)
    )
    assert code == ExitCode.REJECTED and json.loads(err)["error"] == "invalid_value"


# --- Salida y errores ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        {"a": "https://app.vigia.example/invitacion"},
        {"a": ["x", "algo#token"]},
        {"a": "x" * 257},
        {"a": 1.5},
        {"https://x": 1},
    ],
)
def test_output_only_carries_identifiers(value: dict[str, Any]) -> None:
    with pytest.raises(AdminError):
        _reject_links(value)
    _reject_links({"a": [str(uuid.uuid4()), 3, True, None], "b": {"c": "ca/root.pem"}})


@pytest.mark.parametrize(
    ("error", "code", "exit_code"),
    [
        (IdentityRejected(IdentityRejection.CODE_TAKEN, field="/code"), "code_taken", 4),
        (TemporarilyUnavailable(), "temporarily_unavailable", 5),
        (RuntimeError("detalle interno"), "internal_error", 1),
    ],
)
def test_failures_map_to_closed_codes(error: Exception, code: str, exit_code: int) -> None:
    world = FakeWorld()

    async def failing(*args: Any, **kwargs: Any) -> Any:
        raise error

    world.genesis.create_client_organization = failing  # type: ignore[method-assign]
    status, out, err = world.run(*CREATE, "--yes")
    assert status == exit_code and out == ""
    document = json.loads(err)
    assert document["error"] == code
    assert "detalle interno" not in err


def test_parser_lists_every_command() -> None:
    help_text = build_parser().format_help()
    for command in (
        "bootstrap",
        "create-organization",
        "rotate-key",
        "rotate-node-ca",
        "replay-dead-letter",
        "create-partitions",
        "restore-audit-partition",
        "record-restore-drill",
    ):
        assert command in help_text


# --- Guardas de los servicios que llama la orden ----------------------------------------------


def _contexts(world: FakeWorld) -> ScopeContexts:
    return ScopeContexts(
        store=world.store,
        clock=world.clock,
        provider_organization_id=PROVIDER_ID,
        system_actor_id=uuid.uuid4(),
    )


def _provider_request() -> ProviderGenesisRequest:
    return ProviderGenesisRequest("VIGIA-PROV", "Proveedor", "o@example.test", "Operadora")


def test_the_provider_is_only_created_with_the_bootstrap_context() -> None:
    world = FakeWorld()
    contexts = _contexts(world)
    genesis = world.genesis.real  # dependencias Unused: la guarda corre antes de la base
    operator = asyncio.run(contexts.context_from_operator(OPERATOR_ID))
    others = (
        contexts.provider_audit_context(),  # actor del sistema
        operator,  # un operador con asignaciones (no el que nace en la orden)
        make_context(kind=ActorKind.OPERATOR, organization_id=uuid.uuid4()),  # otra organización
    )
    for context in others:
        with pytest.raises(PermissionError):
            asyncio.run(genesis.create_provider_organization(context, _provider_request()))
    bootstrap = contexts.bootstrap_operator_context(OPERATOR_ID, "Operadora")
    assert bootstrap.actor.kind is ActorKind.OPERATOR and bootstrap.allowed_scopes == ()
    assert bootstrap.origin is ContextOrigin.ADMIN_COMMAND
    with pytest.raises(AssertionError, match="dependencia inesperada"):
        # Con el contexto correcto sí llega a la base (aquí, un doble que no se debe tocar).
        asyncio.run(genesis.create_provider_organization(bootstrap, _provider_request()))


class _AuditDouble:
    provider_organization_id = PROVIDER_ID

    def __init__(self) -> None:
        self.appended: list[tuple[str, str, Any]] = []

    async def append(self, context: ScopeContext, operation: Any, **kwargs: Any) -> AuditReceipt:
        self.appended.append((operation.value, kwargs["outcome"].value, kwargs["filters"]))
        return AuditReceipt(uuid.uuid4(), 1, START)


def test_a_restore_drill_is_recorded_only_by_an_operator_command() -> None:
    world = FakeWorld()
    contexts = _contexts(world)
    audit = _AuditDouble()
    drills = RestoreDrills(database=world.database, audit=cast(Any, audit), clock=world.clock)
    for context in (
        contexts.provider_audit_context(),
        contexts.bootstrap_operator_context(OPERATOR_ID, "Operadora"),  # sin platform_operator
        make_context(kind=ActorKind.OPERATOR, organization_id=uuid.uuid4()),
    ):
        with pytest.raises(PermissionError):
            asyncio.run(drills.record(context, DrillResult.OK))
    assert audit.appended == []
    operator = asyncio.run(contexts.context_from_operator(OPERATOR_ID))
    asyncio.run(drills.record(operator, DrillResult.OK))
    asyncio.run(drills.record(operator, DrillResult.FAILED))
    assert audit.appended == [
        ("restore_drill_recorded", "success", {"result": "ok"}),
        ("restore_drill_recorded", "error", {"result": "failed"}),
    ]


class _DrillRows:
    """Transacción que resuelve las dos consultas de ``age_days`` sobre filas en memoria."""

    def __init__(self, context: ScopeContext, rows: list[tuple[str, str, datetime]]) -> None:
        self.context = context
        self.rows = rows
        self.created = START - timedelta(days=400)

    async def execute(self, statement: Any, parameters: dict[str, Any]) -> Any:
        sql = str(statement)
        if "audit_entry" in sql:
            matching = [
                at
                for operation, outcome, at in self.rows
                if operation == parameters["operation"] and outcome == parameters["outcome"]
            ]
            return _Result(SimpleNamespace(last_success=max(matching, default=None)))
        return _Result(self.created)


@dataclass
class _Result:
    value: Any

    def one(self) -> Any:
        return self.value

    def scalar_one(self) -> Any:
        return self.value


def test_restore_drill_age_counts_from_the_last_successful_drill() -> None:
    world = FakeWorld()
    contexts = _contexts(world)
    drills = RestoreDrills(
        database=world.database, audit=cast(Any, _AuditDouble()), clock=world.clock
    )
    context = contexts.provider_audit_context()
    rows = [
        ("restore_drill_recorded", "success", START - timedelta(days=120)),
        ("restore_drill_recorded", "error", START - timedelta(days=3)),
        ("login_succeeded", "success", START - timedelta(days=1)),
    ]
    transaction = cast(Transaction, _DrillRows(context, rows))
    assert asyncio.run(drills.age_days(transaction)) == 120  # el fallido no reinicia
    # Sin ensayos correctos: desde el alta de la proveedora.
    assert asyncio.run(drills.age_days(cast(Transaction, _DrillRows(context, [])))) == 400
    # Solo en la proveedora.
    other = make_context(kind=ActorKind.SYSTEM, organization_id=uuid.uuid4())
    with pytest.raises(PermissionError):
        asyncio.run(drills.age_days(cast(Transaction, _DrillRows(other, rows))))
