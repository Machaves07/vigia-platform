"""``vigia-admin`` contra PostgreSQL 16 y LocalStack (TASK-132; NFR-NUC-09, 54; D-6, D-7).

Cada prueba parte de una base **recién migrada, sin sembrar** (como la del paso 6 del primer
despliegue) y de LocalStack con una clave KMS ``ECC_NIST_P256`` de ``SIGN_VERIFY`` (la de
``vigia-node-ca``), una simétrica (``vigia-secrets``), Secrets Manager y dos depósitos con versiones
(``vigia-edge`` y ``vigia-archive``). La orden corre entera (``admin_cli.run``) con el constructor
real de ``tests.admin_support.IntegrationWorld``: todo como ``vigia_app``.

- **Criterio 2**: ni la salida ni los registros de ``bootstrap`` contienen el enlace (ni el token)
  ni una contraseña; el enlace solo está en el secreto de un solo uso y activa la cuenta.
- **Criterio 4**: ``ca/root.pem`` es un certificado autofirmado de 10 años cuya clave pública es
  la de la clave KMS; ``rotate-node-ca`` deja un paquete de dos raíces y un nodo con credencial de
  la raíz anterior sigue validando.
- **Criterio 1**: ``create-organization --dry-run`` no escribe nada: mismas filas en cada tabla de
  ``identity``, ``ledger`` y ``shared`` (particiones incluidas), mismos secretos y objetos.
- **Criterio 3** (NFR-NUC-09): una base iniciada con ``bootstrap`` y **una sola** organización
  cliente arranca (firma con todas las claves requeridas), activa sus cuentas y todas sus cadenas
  verifican ``intact``; la suite completa la corre la canalización sobre el mismo código.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes

from tests.admin_support import (
    LINK_BASE,
    BuiltRuntime,
    IntegrationWorld,
    issue_node_certificate,
    output,
    public_der,
    validates,
)
from tests.hierarchy_support import FakeActivationPasswords, FakeActivationSecondFactor
from tests.identity_db import MigratedDatabase, migrated_database
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint, versioned_bucket
from tests.outbox_support import app_database
from tests.session_support import GOOD_CODE
from tests.verify_support import CHECKPOINT_KEY, StaticKeys
from vigia_platform.identity.application.admin_cli import AdminConfig
from vigia_platform.identity.application.invitations import InvitationService
from vigia_platform.identity.domain.privacy_notice import CURRENT_PRIVACY_NOTICE_VERSION
from vigia_platform.ledger.adapters.integrity_store import SqlIntegrityStore
from vigia_platform.ledger.application.audit_writer import AuditOperation
from vigia_platform.ledger.chain.checkpoints import ChainKind, CheckpointChain
from vigia_platform.ledger.chain.verify import IntegrityService, VerificationMode
from vigia_platform.shared.archive.restore_drill import RestoreDrills
from vigia_platform.shared.node_ca import ROOT_CERTIFICATE_KEY, read_bundle
from vigia_platform.shared.observability.logging import QUIET_LOGGERS, configure_logging
from vigia_platform.shared.signing.keys import KeyStatus, SigningPurpose

pytestmark = pytest.mark.integration

PASSWORD = "una-clave-sintetica-larga"  # noqa: S105 - dato sintético de prueba
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
    "--yes",
)


def _create(operator_id: uuid.UUID, *extra: str) -> tuple[str, ...]:
    return (
        "create-organization",
        "--operator",
        str(operator_id),
        "--code",
        "CLIENTE-PILOTO",
        "--name",
        "Cliente sintético del piloto",
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
        "administradora@example.test",
        "--admin-name",
        "Administradora sintética",
        *extra,
    )


@dataclass
class Env:
    world: IntegrationWorld
    migrated: MigratedDatabase
    localstack: LocalStackEndpoint

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        async def go() -> list[Any]:
            connection = await asyncpg.connect(self.migrated.as_role().dsn)
            try:
                return list(await connection.fetch(sql, *args))
            finally:
                await connection.close()

        return asyncio.run(go())

    def secret(self, name: str) -> dict[str, Any]:
        value = self.localstack.aws_client("secretsmanager").get_secret_value(SecretId=name)
        document: dict[str, Any] = json.loads(value["SecretString"])
        return document

    def object(self, key: str) -> bytes:
        s3 = self.localstack.aws_client("s3")
        body: bytes = s3.get_object(Bucket=self.world.edge_bucket, Key=key)["Body"].read()
        return body

    def with_runtime(self, provider_id: uuid.UUID, action: Any) -> Any:
        """Ejecuta ``action(built)`` con un ``AdminRuntime`` nuevo y sus registros sellados."""

        async def go() -> Any:
            config = AdminConfig.from_environ(self.world.environ(provider_id))
            runtime = await self.world.builder(config, provider_id)
            built = self.world.built[-1]
            try:
                for synchronize in runtime.registries:
                    await synchronize()
                return await action(built)
            finally:
                await runtime.database.dispose()

        return asyncio.run(go())

    def activate(self, provider_id: uuid.UUID, link: str) -> uuid.UUID:
        """Activa la cuenta del enlace (``POST /invitations/{token}/accept``)."""
        assert link.startswith(f"{LINK_BASE}/invitacion#")
        token = link.split("#", 1)[1]

        async def accept(built: BuiltRuntime) -> uuid.UUID:
            service = InvitationService(
                built.deps,
                contexts=built.contexts,
                passwords=FakeActivationPasswords(),
                second_factor=FakeActivationSecondFactor(),
            )
            account = await service.accept_invitation(
                token, PASSWORD, CURRENT_PRIVACY_NOTICE_VERSION, GOOD_CODE
            )
            return account.user_id

        result: uuid.UUID = self.with_runtime(provider_id, accept)
        return result

    def table_counts(self) -> dict[str, int]:
        tables = self.fetch(
            "SELECT n.nspname || '.' || c.relname AS name FROM pg_class AS c"
            " JOIN pg_namespace AS n ON n.oid = c.relnamespace"
            " WHERE c.relkind = 'r' AND n.nspname IN ('identity', 'ledger', 'shared')"
            " ORDER BY 1"
        )
        counts: dict[str, int] = {}
        for row in tables:
            name = row["name"]
            schema, table = name.split(".")
            (count,) = self.fetch(f'SELECT count(*) AS n FROM "{schema}"."{table}"')  # noqa: S608
            counts[name] = int(count["n"])
        return counts

    def secret_versions(self) -> dict[str, int]:
        client = self.localstack.aws_client("secretsmanager")
        prefix = f"vigia/{self.world.environment}/"
        versions: dict[str, int] = {}
        for secret in client.list_secrets(MaxResults=100)["SecretList"]:
            if secret["Name"].startswith(prefix):
                listed = client.list_secret_version_ids(SecretId=secret["ARN"])
                versions[secret["Name"]] = len(listed["Versions"])
        return versions

    def object_versions(self) -> int:
        s3 = self.localstack.aws_client("s3")
        total = 0
        for bucket in (self.world.edge_bucket, self.world.archive_bucket):
            listing = s3.list_object_versions(Bucket=bucket)
            total += len(listing.get("Versions", [])) + len(listing.get("DeleteMarkers", []))
        return total


@pytest.fixture
def env(
    postgres_endpoint: PostgresEndpoint, localstack_endpoint: LocalStackEndpoint
) -> Iterator[Env]:
    kms = localstack_endpoint.aws_client("kms")
    node_ca = kms.create_key(
        Description="vigia-node-ca de prueba", KeyUsage="SIGN_VERIFY", KeySpec="ECC_NIST_P256"
    )["KeyMetadata"]["KeyId"]
    secrets_key = kms.create_key(Description="vigia-secrets de prueba")["KeyMetadata"]["KeyId"]
    s3 = localstack_endpoint.aws_client("s3")
    with (
        migrated_database(postgres_endpoint, "admin") as migrated,
        versioned_bucket(s3, "vigia-edge") as edge,
        versioned_bucket(s3, "vigia-archive") as archive,
    ):
        world = IntegrationWorld(
            database_factory=lambda: app_database(migrated, worker_pool_size=4),
            localstack_url=localstack_endpoint.url,
            region=localstack_endpoint.region,
            node_ca_key_id=node_ca,
            edge_bucket=edge,
            archive_bucket=archive,
            secrets_key_id=secrets_key,
        )
        yield Env(world, migrated, localstack_endpoint)


def _bootstrap(env: Env) -> dict[str, Any]:
    code, out, err = env.world.run(*BOOTSTRAP, environ=env.world.environ())
    assert code == 0, err
    return output(out)


def _secret_name(env: Env) -> str:
    return env.world.environ()["VIGIA_BOOTSTRAP_INVITATION_SECRET"]


# --- Criterios 2 y 4 (bootstrap) ----------------------------------------------------------------


@contextlib.contextmanager
def process_logs(level: str) -> Iterator[io.StringIO]:
    """Los registros del proceso tal como los instala ``admin_cli.main`` (``configure_logging``
    con su lista de registradores silenciados), en ``level``; restaura el estado al salir."""
    root = logging.getLogger()
    handlers, root_level = list(root.handlers), root.level
    quiet = {name: logging.getLogger(name).level for name in QUIET_LOGGERS}
    stream = io.StringIO()
    configure_logging(level, stream=stream)
    try:
        yield stream
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in handlers:
            root.addHandler(handler)
        root.setLevel(root_level)
        for name, previous in quiet.items():
            logging.getLogger(name).setLevel(previous)


def test_bootstrap_creates_the_platform_and_never_shows_the_link(env: Env) -> None:
    # Registros del proceso en su nivel más hablador (VIGIA_LOG_LEVEL=DEBUG).
    with process_logs("DEBUG") as logs:
        code, out, err = env.world.run(*BOOTSTRAP, environ=env.world.environ())
    assert code == 0, err
    document = output(out)
    provider_id = uuid.UUID(document["provider_organization_id"])
    operator_id = uuid.UUID(document["operator_user_id"])
    secret = env.secret(_secret_name(env))
    link = secret["link"]
    token = link.split("#", 1)[1]
    assert len(token) == 43
    assert secret["user_id"] == str(operator_id)

    # Criterio 2: ni la salida ni los registros llevan el enlace, el token ni una contraseña.
    logged = logs.getvalue()
    assert "organización proveedora creada" in logged  # los registros sí se capturaron
    for text_ in (out, err, logged):
        assert link not in text_ and token not in text_ and "://" not in text_
        assert "password" not in text_.lower() and "contraseña" not in text_.lower()
    # Tampoco en la base: solo el SHA-256 del token.
    stored = env.fetch("SELECT token_hash FROM identity.invitation")
    assert [row["token_hash"] for row in stored] != [token]

    # Una sola proveedora, creada por su primer operador, invitado con platform_operator.
    organizations = env.fetch("SELECT organization_id, kind, created_by FROM identity.organization")
    assert [(r["organization_id"], r["kind"], r["created_by"]) for r in organizations] == [
        (provider_id, "provider", operator_id)
    ]
    (operator,) = env.fetch(
        "SELECT u.status, r.role FROM identity.user_account AS u"
        " JOIN identity.role_assignment AS r ON r.user_id = u.user_id"
    )
    assert (operator["status"], operator["role"]) == ("invited", "platform_operator")

    # Una clave Ed25519 activa por propósito, atribuida al operador, y el conjunto publicado.
    keys = env.fetch(
        "SELECT purpose, status, rotated_by, private_key_ref FROM identity.signing_key"
    )
    assert sorted(r["purpose"] for r in keys) == sorted(p.value for p in SigningPurpose)
    assert {(r["status"], r["rotated_by"]) for r in keys} == {("active", operator_id)}
    assert document["signing_keys"].keys() == {p.value for p in SigningPurpose}
    publications = env.fetch(
        "SELECT publication_id, jsonb_array_length(keys) AS n FROM identity.key_set_publication"
        " ORDER BY issued_at DESC"
    )
    assert str(publications[0]["publication_id"]) == document["key_set_publication_id"]
    assert publications[0]["n"] == 4  # catalog, gate, live_view_token y key_set
    records = env.fetch(
        "SELECT record_type, chain_sequence FROM ledger.ledger_record ORDER BY chain_sequence"
    )
    assert records[0]["record_type"] == "organization_created"
    assert [r["record_type"] for r in records].count("key_rotated") == 5

    # Criterio 4 (raíz): autofirmada, 10 años, con la clave pública de la clave KMS.
    (root,) = read_bundle(env.object(ROOT_CERTIFICATE_KEY))
    root.verify_directly_issued_by(root)
    kms_public = env.localstack.aws_client("kms").get_public_key(KeyId=env.world.node_ca_key_id)
    assert public_der(root.public_key()) == kms_public["PublicKey"]
    start, end = root.not_valid_before_utc, root.not_valid_after_utc
    assert end == start.replace(year=start.year + 10)
    assert root.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert document["root_sha256"] == root.fingerprint(hashes.SHA256()).hex()
    s3 = env.localstack.aws_client("s3")
    listed = s3.list_objects_v2(Bucket=env.world.edge_bucket).get("Contents", [])
    assert [item["Key"] for item in listed] == [ROOT_CERTIFICATE_KEY]  # sin ca/crl.pem (D-7)

    # El enlace del secreto activa la cuenta del operador (con segundo factor).
    assert env.activate(provider_id, link) == operator_id
    (status,) = env.fetch("SELECT status FROM identity.user_account")
    assert status["status"] == "active"


def test_a_second_bootstrap_creates_nothing(env: Env) -> None:
    _bootstrap(env)
    before, secrets_before = env.table_counts(), env.secret_versions()
    # Otra proveedora (otro código y otro operador): el índice organization_single_provider.
    other = list(BOOTSTRAP)
    other[other.index("VIGIA-PROV")] = "OTRA-PROV"
    other[other.index("operadora@example.test")] = "otra@example.test"
    code, out, err = env.world.run(*other, environ=env.world.environ())
    assert code == 4 and out == ""
    assert json.loads(err)["error"] == "provider_exists"
    # La misma orden otra vez: su código ya está tomado.
    code, out, err = env.world.run(*BOOTSTRAP, environ=env.world.environ())
    assert code == 4 and out == ""
    assert json.loads(err)["error"] == "code_taken"
    assert env.table_counts() == before
    assert env.secret_versions() == secrets_before


def test_rotate_node_ca_publishes_two_roots_and_old_nodes_keep_validating(env: Env) -> None:
    document = _bootstrap(env)
    provider_id = uuid.UUID(document["provider_organization_id"])
    kms_client = env.localstack.aws_client("kms")
    (old_root,) = read_bundle(env.object(ROOT_CERTIFICATE_KEY))
    now = env.world.clock.now()
    kms = _kms(env)
    old_node = asyncio.run(issue_node_certificate(kms, env.world.node_ca_key_id, old_root, now))
    new_key = kms_client.create_key(KeyUsage="SIGN_VERIFY", KeySpec="ECC_NIST_P256")["KeyMetadata"][
        "KeyId"
    ]
    environ = env.world.environ(provider_id)
    operator_id = env.activate(provider_id, env.secret(_secret_name(env))["link"])
    rotate = ("rotate-node-ca", "--new-key-id", new_key, "--operator", str(operator_id))
    code, out, err = env.world.run(*rotate, "--dry-run", environ=environ)
    assert code == 0, err
    assert len(read_bundle(env.object(ROOT_CERTIFICATE_KEY))) == 1  # --dry-run no publica
    code, out, err = env.world.run(*rotate, "--yes", environ=environ)
    assert code == 0, err
    # Intención y resultado auditados con el operador (bootstrap dejó los dos primeros).
    audited = env.fetch(
        "SELECT operation, actor_id FROM shared.audit_entry"
        " WHERE operation LIKE 'node_ca_root_%' ORDER BY chain_sequence"
    )
    assert [(r["operation"], r["actor_id"]) for r in audited[2:]] == [
        ("node_ca_root_requested", operator_id),
        ("node_ca_root_published", operator_id),
    ]
    bundle = read_bundle(env.object(ROOT_CERTIFICATE_KEY))
    assert len(bundle) == 2 and bundle[0] == old_root
    new_root = bundle[1]
    assert (
        public_der(new_root.public_key()) == kms_client.get_public_key(KeyId=new_key)["PublicKey"]
    )
    assert output(out)["root_bundle_sha256"] == [
        root.fingerprint(hashes.SHA256()).hex() for root in bundle
    ]
    new_node = asyncio.run(issue_node_certificate(kms, new_key, new_root, now))
    at = now + timedelta(minutes=5)
    assert validates(old_node, bundle, at)  # el nodo de la raíz anterior sigue validando
    assert validates(new_node, bundle, at)
    assert not validates(old_node, (new_root,), at)  # sonda: sin la raíz vigente, no valida


def _kms(env: Env) -> Any:
    from vigia_platform.shared.secrets import KmsAdapter

    return KmsAdapter(env.world.aws_settings())


# --- Criterio 1 (create-organization --dry-run) -------------------------------------------------


def test_create_organization_dry_run_writes_nothing(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    document = _bootstrap(env)
    provider_id = uuid.UUID(document["provider_organization_id"])
    operator_id = env.activate(provider_id, env.secret(_secret_name(env))["link"])
    environ = env.world.environ(provider_id)
    tables, secrets_before, objects = (
        env.table_counts(),
        env.secret_versions(),
        env.object_versions(),
    )
    caplog.clear()
    caplog.set_level(logging.INFO)
    code, out, err = env.world.run(*_create(operator_id, "--dry-run"), environ=environ)
    assert code == 0, err
    assert output(out)["dry_run"] is True
    assert env.table_counts() == tables
    assert env.secret_versions() == secrets_before
    assert env.object_versions() == objects
    assert not [r for r in caplog.records if r.levelno >= logging.INFO]
    # El mismo recuento sí ve la orden real: la comprobación no es ciega.
    code, out, err = env.world.run(*_create(operator_id), environ=environ, stdin="CLIENTE-PILOTO\n")
    assert code == 0, err
    after = env.table_counts()
    changed = {name for name in after if after[name] != tables[name]}
    assert {"identity.organization", "identity.plant", "identity.user_account"} <= changed
    assert env.secret_versions()[_secret_name(env)] == secrets_before[_secret_name(env)] + 1


# --- Criterio 3 (instancia con una sola organización cliente) -----------------------------------


def test_a_single_client_deployment_starts_and_its_chains_are_intact(env: Env) -> None:
    document = _bootstrap(env)
    provider_id = uuid.UUID(document["provider_organization_id"])
    operator_id = env.activate(provider_id, env.secret(_secret_name(env))["link"])
    code, out, err = env.world.run(
        *_create(operator_id, "--yes"), environ=env.world.environ(provider_id)
    )
    assert code == 0, err
    created = output(out)
    client_id = uuid.UUID(created["organization_id"])
    plant_id = uuid.UUID(created["plant_id"])
    secret = env.secret(_secret_name(env))
    assert secret["organization_id"] == str(client_id)
    administrator = env.activate(provider_id, secret["link"])
    assert str(administrator) == created["administrator_user_id"]

    kinds = env.fetch("SELECT kind, count(*) AS n FROM identity.organization GROUP BY kind")
    assert {(row["kind"], row["n"]) for row in kinds} == {("provider", 1), ("client", 1)}

    async def start_and_verify(built: BuiltRuntime) -> list[str]:
        # Arranque de vigia-api: las claves de los cinco propósitos, requeridas y cargadas.
        await built.signing.start()
        assert all(built.signing.has_active_key(purpose) for purpose in SigningPurpose)
        envelope = built.signing.current_key_set_envelope()
        assert envelope is not None
        database = built.database
        integrity = IntegrityService(
            store=SqlIntegrityStore(
                database=database, audit=built.deps.audit, outbox=built.deps.outbox
            ),
            keys=StaticKeys(CHECKPOINT_KEY),
            clock=built.runtime.clock,
        )
        statuses: list[str] = []
        for organization_id, chains in (
            (provider_id, (CheckpointChain(ChainKind.LEDGER), CheckpointChain(ChainKind.AUDIT))),
            (
                client_id,
                (
                    CheckpointChain(ChainKind.LEDGER),
                    CheckpointChain(ChainKind.LEDGER, plant_id),
                    CheckpointChain(ChainKind.AUDIT),
                ),
            ),
        ):
            context = built.contexts.anonymous(organization_id)
            for chain in chains:
                result = await integrity.verify(context, chain, VerificationMode.FULL)
                statuses.append(result.status.value)
        return statuses

    assert env.with_runtime(provider_id, start_and_verify) == ["intact"] * 5


# --- Órdenes del operador -----------------------------------------------------------------------


def test_operator_commands_against_the_bootstrapped_database(env: Env) -> None:
    document = _bootstrap(env)
    provider_id = uuid.UUID(document["provider_organization_id"])
    environ = env.world.environ(provider_id)
    invited_operator = uuid.UUID(document["operator_user_id"])
    # Mientras el operador no activa su cuenta, ninguna orden lo acepta.
    code, _, err = env.world.run(
        "rotate-key", "catalog", "--operator", str(invited_operator), environ=environ
    )
    assert code == 4 and json.loads(err)["error"] == "operator_invalid"
    operator_id = env.activate(provider_id, env.secret(_secret_name(env))["link"])

    # rotate-key: la clave anterior pasa a solapamiento y se publica otro conjunto.
    previous = document["signing_keys"]["catalog"]
    code, out, err = env.world.run(
        "rotate-key", "catalog", "--operator", str(operator_id), environ=environ
    )
    assert code == 0, err
    rotated = output(out)
    assert rotated["previous_key_id"] == previous
    statuses = {
        row["key_id"]: row["status"]
        for row in env.fetch("SELECT key_id, status FROM identity.signing_key")
    }
    assert statuses[previous] == KeyStatus.OVERLAPPING.value
    assert statuses[rotated["key_id"]] == KeyStatus.ACTIVE.value

    # replay-dead-letter: sin una entrega en la cola muerta, not_found y nada cambia.
    before = env.table_counts()
    code, _, err = env.world.run(
        "replay-dead-letter", str(uuid.uuid4()), "u02_alerts", "--operator", str(operator_id),
        environ=environ,
    )  # fmt: skip
    assert code == 4 and json.loads(err)["error"] == "not_found"
    assert env.table_counts() == before

    # create-partitions: idempotente.
    month = env.world.clock.now()
    until = f"{month.year + 1}-{month.month:02d}"
    code, out, err = env.world.run(
        "create-partitions", "--until", until, "--operator", str(operator_id), environ=environ
    )
    assert code == 0, err
    first = output(out)
    assert first["last_month"] == until and first["blocked"] == []
    code, out, _ = env.world.run(
        "create-partitions", "--until", until, "--operator", str(operator_id), environ=environ
    )
    assert code == 0 and output(out)["created"] == []

    # record-restore-drill y restore_drill_age_days: cuenta desde el último ensayo ok.
    code, out, err = env.world.run(
        "record-restore-drill", "--result", "ok", "--operator", str(operator_id), environ=environ
    )
    assert code == 0, err
    entry = output(out)["audit_entry_id"]
    (recorded,) = env.fetch(
        "SELECT operation, outcome, actor_id FROM shared.audit_entry WHERE entry_id = $1",
        uuid.UUID(entry),
    )
    assert (recorded["operation"], recorded["outcome"], recorded["actor_id"]) == (
        "restore_drill_recorded",
        "success",
        operator_id,
    )

    async def age(built: BuiltRuntime) -> int:
        drills = RestoreDrills(
            database=built.database, audit=built.deps.audit, clock=built.runtime.clock
        )
        async with built.database.transaction(
            built.contexts.provider_audit_context()
        ) as transaction:
            return await drills.report(transaction)

    assert env.with_runtime(provider_id, age) == 0
    code, _, _ = env.world.run(
        "record-restore-drill", "--result", "failed", "--operator", str(operator_id),
        environ=environ,
    )  # fmt: skip
    assert code == 0
    env.world.clock.offset = timedelta(days=101, minutes=1)
    assert env.with_runtime(provider_id, age) == 101  # el ensayo fallido no la reinicia


# --- Rotación y auditoría (revisión de VIG-93) -------------------------------------------------


def _audited(env: Env, operation: str) -> int:
    (row,) = env.fetch(
        "SELECT count(*) AS n FROM shared.audit_entry WHERE operation = $1", operation
    )
    return int(row["n"])


def _key_rotated_records(env: Env) -> int:
    (row,) = env.fetch(
        "SELECT count(*) AS n FROM ledger.ledger_record WHERE record_type = 'key_rotated'"
    )
    return int(row["n"])


def test_a_key_rotation_never_stays_without_its_audit_entry(env: Env) -> None:
    document = _bootstrap(env)
    provider_id = uuid.UUID(document["provider_organization_id"])
    environ = env.world.environ(provider_id)
    # bootstrap: cinco claves, cada una con su key_rotated en el expediente y en la auditoría.
    assert _audited(env, "key_rotated") == _key_rotated_records(env) == 5
    operator_id = env.activate(provider_id, env.secret(_secret_name(env))["link"])
    keys = env.fetch("SELECT key_id FROM identity.signing_key ORDER BY key_id")

    async def failing_audit(built: BuiltRuntime) -> None:
        audit = built.deps.audit
        original = audit.append

        async def append(context: Any, operation: Any, *args: Any, **kwargs: Any) -> Any:
            if operation == AuditOperation.KEY_ROTATED:
                raise ConnectionError("la base se cae entre la rotación y su auditoría")
            return await original(context, operation, *args, **kwargs)

        audit.append = append  # type: ignore[method-assign]

    env.world.on_build = failing_audit
    rotate = ("rotate-key", "catalog", "--operator", str(operator_id))
    code, out, _ = env.world.run(*rotate, environ=environ)
    env.world.on_build = None
    assert code != 0 and out == ""
    # Fallo inyectado entre la clave y su auditoría: no queda la clave, ni el registro.
    assert env.fetch("SELECT key_id FROM identity.signing_key ORDER BY key_id") == keys
    assert _audited(env, "key_rotated") == _key_rotated_records(env) == 5
    code, out, err = env.world.run(*rotate, environ=environ)
    assert code == 0, err
    rotated = output(out)["key_id"]
    filters = [
        json.loads(bytes(row["filters"]))
        for row in env.fetch(
            "SELECT filters FROM shared.audit_entry WHERE operation = 'key_rotated'"
        )
    ]
    assert [f["key_id"] for f in filters].count(rotated) == 1  # una sola entrada, no dos
    assert _audited(env, "key_rotated") == _key_rotated_records(env) == 6


def test_bootstrap_audits_the_root_before_publishing_it(env: Env) -> None:
    document = _bootstrap(env)
    entries = env.fetch(
        "SELECT operation, outcome, filters FROM shared.audit_entry"
        " WHERE operation LIKE 'node_ca_root_%' ORDER BY chain_sequence"
    )
    assert [(row["operation"], row["outcome"]) for row in entries] == [
        ("node_ca_root_requested", "success"),
        ("node_ca_root_published", "success"),
    ]
    requested = json.loads(bytes(entries[0]["filters"]))
    assert requested["root_sha256"] == document["root_sha256"]
