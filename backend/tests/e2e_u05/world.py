"""Plataforma real local para Playwright, reutilizando el arnés de U-04.

El manifiesto público no contiene credenciales. El directorio privado y sus archivos se
eliminan también cuando falla el arranque o Playwright cierra su canal de control.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import secrets
import tempfile
import uuid
import zipfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from tests.conformance.mtls_proxy import server_tls
from tests.conformance.platform_target import (
    BACKEND,
    WALL,
    PlatformStack,
    Provisioner,
    platform_stack,
)
from tests.e2e_u05.commissioning import commissioning_clips
from tests.e2e_u05.journeys import prepare_journeys
from tests.integration.conftest import localstack_endpoint_session, postgres_endpoint_session
from tests.integration.test_u04_end_to_end import (
    _expected_subjects,
    _projection,
    in_process_worker,
)
from tests.load.balancer import balancer_process
from tests.load.conftest import LoadTarget
from tests.load.profiles import PROFILES, write_sealed_dataset
from tests.load.provision import Fleet, load_api
from tests.load.test_load_ci import assert_functional, ledger_check, run_load
from tests.resilience.harness import free_port, wait_until
from vigia_platform.identity.application.hierarchy import ZoneSpec
from vigia_platform.identity.application.invitations import InvitationOutcome
from vigia_platform.identity.application.roles import AssignmentRequest
from vigia_platform.identity.application.users import InviteRequest
from vigia_platform.identity.auth.passwords import hash_password
from vigia_platform.identity.auth.second_factor import credential_aad
from vigia_platform.loop import registration
from vigia_platform.loop.adapters.assistant.fake import FakeAssistantProvider, Script
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.crypto import EnvelopeCipher
from vigia_platform.shared.secrets import AwsSettings, KmsAdapter

ROOT = BACKEND.parent
MANIFEST = ROOT / "frontend/tests/e2e/.world.json"
ROLES = tuple(role for role in Role if role is not Role.PLATFORM_OPERATOR)


def private_json(path: Path, document: Mapping[str, Any]) -> None:
    """Creación exclusiva y privada desde el primer byte, no chmod posterior."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(document, stream)


@contextlib.contextmanager
def published_manifest(path: Path, document: Mapping[str, Any]) -> Iterator[None]:
    """Solo elimina el manifiesto que creó, también ante un fallo al escribirlo."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream)
        yield
    finally:
        path.unlink(missing_ok=True)


def assert_no_leaks(
    roots: tuple[Path, ...], forbidden: tuple[str, ...], *, allowed: tuple[Path, ...] = ()
) -> None:
    """Busca valores exactos generados; un fallo nunca imprime el valor encontrado.

    Excluye dependencias y cachés, pero incluye dist, artefactos Playwright y registros.
    """
    excluded = {"node_modules", ".venv", "__pycache__", ".git", ".mypy_cache"}
    needles = tuple(value.encode() for value in forbidden)

    def check(content: bytes) -> None:
        if any(needle in content for needle in needles):
            raise AssertionError("Se detectó una credencial sintética fuera del archivo privado")

    for root in roots:
        for directory, folders, files in os.walk(root):
            folders[:] = [name for name in folders if name not in excluded]
            for name in files:
                path = Path(directory) / name
                if path.is_symlink() or path in allowed:
                    continue
                check(path.read_bytes())
                if path.suffix == ".zip":
                    with zipfile.ZipFile(path) as archive:
                        for entry in archive.infolist():
                            check(archive.read(entry))


def users(target: LoadTarget, fleet: Fleet) -> tuple[dict[str, Any], dict[str, Any]]:
    """Un usuario por rol, con Argon2id y TOTP cifrado por KMS real de LocalStack.

    Administrador e instalador son los que creó FleetProvisioner; no se duplican.
    Los demás se siembran como en U-04. Ninguna sesión sembrada se entrega al navegador.
    """
    provisioner = Provisioner(
        target.stack, target.api, target.app.url, target.tls.ca_file, target.directory
    )
    environ = target.stack.environ
    cipher = EnvelopeCipher(
        KmsAdapter(
            AwsSettings(
                region=environ["AWS_REGION"], endpoint_url=environ["VIGIA_AWS_ENDPOINT_URL"]
            )
        ),
        environ["VIGIA_SECRETS_KEY_ARN"],
        WALL,
    )
    public: dict[str, Any] = {}
    private: dict[str, Any] = {}
    for role in ROLES:
        organization = (
            target.stack.provider_organization_id
            if role is Role.PROVIDER_INSTALLER
            else fleet.organization_id
        )
        rows = target.stack.fetch(
            "SELECT u.user_id FROM identity.user_account u JOIN identity.role_assignment r"
            " ON r.user_id = u.user_id WHERE r.organization_id = $1 AND r.role = $2",
            organization,
            role.value,
        )
        if rows:
            user = uuid.UUID(str(rows[0]["user_id"]))
        else:
            user, statements = provisioner._user(organization, role)
            if role is Role.LINE_MANAGER:
                # La asignación nace acotada; identity no permite reescribir su alcance.
                assignment, parameters = statements[-1]
                assert "'organization', $2" in assignment
                statements[-1] = (
                    assignment.replace("'organization', $2", "'zone', $7"),
                    (*parameters, fleet.zone_ids[0]),
                )
            target.stack.admin(statements)
        password = secrets.token_urlsafe(32)
        secret = secrets.token_bytes(20)
        hashed = hash_password(password)
        sealed = target.api.call(cipher.encrypt(secret, credential_aad(organization, user)))
        email = f"{role.value}@e2e.example.test"
        now = WALL.now()
        target.stack.admin(
            [
                (
                    "UPDATE identity.user_account SET email = $2, second_factor_required = true,"
                    " second_factor_enrolled_at = $3 WHERE user_id = $1",
                    (user, email, now),
                ),
                (
                    "INSERT INTO identity.password_credential (user_id, organization_id,"
                    " password_hash,"
                    " algorithm_version, updated_at) VALUES ($1, $2, $3, $4, $5)",
                    (
                        user,
                        organization,
                        hashed.encoded,
                        f"argon2id-v{hashed.algorithm_version}",
                        now,
                    ),
                ),
                (
                    "INSERT INTO identity.totp_credential (user_id, organization_id,"
                    " secret_encrypted,"
                    " data_key_wrapped, enrolled_at) VALUES ($1, $2, $3, $4, $5)",
                    (user, organization, sealed.ciphertext, sealed.wrapped_key, now),
                ),
            ]
        )
        public[role.value] = {"email": email, "user_id": str(user)}
        private[role.value] = {"password": password, "totp_hex": secret.hex()}
    return public, private


def non_productive_zone(target: LoadTarget, fleet: Fleet, administrator: str) -> uuid.UUID:
    """Zona fuera de producción: sin nodo ni compuerta de uso aprobada."""
    provisioner = Provisioner(
        target.stack, target.api, target.app.url, target.tls.ca_file, target.directory
    )
    cookie, statement = provisioner._session(fleet.organization_id, uuid.UUID(administrator))
    target.stack.admin([statement])

    async def create() -> uuid.UUID:
        assert target.api.runtime is not None and target.api.runtime.identity is not None
        hierarchy = target.api.runtime.identity.hierarchy
        assert hierarchy is not None
        context = await provisioner._context(cookie, None)
        zone = await hierarchy.create_zone(
            context, fleet.plant_ids[0], ZoneSpec(code="ZN-NON-PROD", name="Zona no productiva")
        )
        return zone.zone_id

    return target.api.call(create())


def deploy_check_invitation(target: LoadTarget, fleet: Fleet, administrator: str) -> dict[str, str]:
    """Invitación de un solo uso de coordinación SST en la organización sintética (VIG-352).

    Es el material ``frontend`` del secreto ``vigia/<entorno>/deploy-checks`` de staging, para
    las comprobaciones de despliegue con ``--target``: la emite la persona administradora por el
    servicio real, con el enlace divulgado una vez en lugar de enviarlo por correo.

    El recorrido inscribe y usa el segundo factor de prueba, que BR-NUC-21 solo exige a
    administración y operación (sin panel de inicio ni bandeja). Como las seis cuentas del
    mundo, la cuenta se marca por SQL con ``second_factor_required`` antes de activarse.
    """
    provisioner = Provisioner(
        target.stack, target.api, target.app.url, target.tls.ca_file, target.directory
    )
    cookie, statement = provisioner._session(fleet.organization_id, uuid.UUID(administrator))
    target.stack.admin([statement])
    email = "deploy-check@e2e.example.test"

    async def invite() -> InvitationOutcome:
        assert target.api.runtime is not None and target.api.runtime.identity is not None
        users = target.api.runtime.identity.users
        assert users is not None
        context = await provisioner._context(cookie, None)
        return await users.invite_user(
            context,
            InviteRequest(
                email=email,
                display_name="Comprobación de despliegue",
                assignments=(
                    AssignmentRequest(
                        Role.COORDINATOR_SST, ScopeLevel.ORGANIZATION, fleet.organization_id
                    ),
                ),
                disclose_link=True,
            ),
        )

    outcome = target.api.call(invite())
    if not outcome.link:
        raise RuntimeError("La invitación de la comprobación de despliegue no divulgó su enlace")
    target.stack.admin(
        [
            (
                "UPDATE identity.user_account SET second_factor_required = true WHERE user_id = $1",
                (outcome.user_id,),
            )
        ]
    )
    return {"invitation_link": outcome.link, "email": email}


def configure_store_origin(platform: PlatformStack, origin: str) -> None:
    """CORS de vigia-evidence como en infra/stacks/data.py, con el origen TLS efímero.

    El arnés de carga solo usa clientes de servidor y crea depósitos sin CORS. El
    navegador necesita la misma regla de producción, nunca un origen comodín.
    """
    with contextlib.closing(platform.localstack.aws_client("s3")) as store:
        store.put_bucket_cors(
            Bucket=platform.environ["VIGIA_EVIDENCE_BUCKET"],
            CORSConfiguration={
                "CORSRules": [
                    {
                        "AllowedOrigins": [origin],
                        "AllowedMethods": ["PUT", "GET", "HEAD"],
                        "AllowedHeaders": ["Content-Type", "x-amz-checksum-sha256", "x-amz-meta-*"],
                        "ExposeHeaders": ["ETag"],
                        "MaxAgeSeconds": 3600,
                    }
                ]
            },
        )


@contextlib.contextmanager
def world(manifest: Path = MANIFEST) -> Iterator[dict[str, Any]]:
    """Arranque y liberación simétricos, incluidos los fallos antes de publicar el manifiesto."""
    if manifest.exists():
        raise RuntimeError("Ya existe un mundo; no se sobrescribe una ejecución concurrente")
    static = ROOT / "frontend/dist"
    if not (static / "version.json").is_file():
        raise RuntimeError("Primero construye frontend con npm run build")
    with (
        tempfile.TemporaryDirectory(prefix="vigia-e2e-") as temporary,
        contextlib.ExitStack() as stack,
    ):
        directory = Path(temporary)
        if directory.resolve().is_relative_to(ROOT):
            raise RuntimeError("El directorio temporal del mundo debe quedar fuera del árbol")
        os.chmod(directory, 0o700)
        postgres = stack.enter_context(contextlib.contextmanager(postgres_endpoint_session)())
        localstack = stack.enter_context(contextlib.contextmanager(localstack_endpoint_session)())
        tls = server_tls(directory, WALL.now())
        aws = stack.enter_context(
            balancer_process("aws", [localstack.url], tls, directory, preserve_host=True)
        )
        platform = stack.enter_context(
            platform_stack(postgres, localstack, directory, aws_url=aws.url, ca_bundle=tls.ca_file)
        )
        port = free_port()
        app = stack.enter_context(
            balancer_process("app", [f"http://127.0.0.1:{port}"], tls, directory)
        )
        platform.environ.update(
            VIGIA_STATIC_DIR=str(static),
            VIGIA_PUBLIC_ORIGIN=app.url,
            VIGIA_CSP_STORE_ORIGINS=aws.url,
        )
        configure_store_origin(platform, app.url)
        api = stack.enter_context(load_api(platform.environ, port, log_file=directory / "api.log"))
        node_ca = directory / "node-ca.crt"
        node_ca.write_bytes(platform.node_ca_root())
        nodes = stack.enter_context(
            balancer_process("nodes", [api.url], tls, directory, client_ca=node_ca)
        )
        target = LoadTarget(platform, api, app, nodes, tls, directory)
        patch = stack.enter_context(pytest.MonkeyPatch.context())
        patch.setattr(
            registration,
            "assistant_provider",
            lambda *_, **__: FakeAssistantProvider(Script.VALID_WITH_CITATIONS),
        )
        stack.enter_context(in_process_worker(platform.environ))
        work = directory / "load"
        work.mkdir()
        cache = directory / "clips"
        cache.mkdir()
        dataset = directory / "dataset"
        dataset.mkdir()
        profile = PROFILES["u04-e2e"]
        seed = 449
        fleet, result, analysis, _ = run_load(
            target,
            profile,
            dataset=write_sealed_dataset(dataset, seed, WALL.now()),
            cache=cache,
            work=work,
        )
        assert_functional(analysis, ledger_check(target, fleet, set(result["journal"]["emitted"])))
        commissioning_clips(target, fleet)
        expected = _expected_subjects(target, fleet.organization_id)
        assert expected["finding"] and expected["detection"], "La carga no llenó bandeja y cola"
        wait_until(
            lambda: (
                len(_projection(target, fleet.organization_id)) == sum(map(len, expected.values()))
            ),
            timeout=180,
            message="La bandeja no alcanzó al expediente",
        )
        public, credentials = users(target, fleet)
        journeys = prepare_journeys(target, fleet, public, profile=profile.name, seed=seed)
        non_productive = non_productive_zone(target, fleet, public["administrator"]["user_id"])
        invitation = deploy_check_invitation(target, fleet, public["administrator"]["user_id"])
        credentials_file = directory / "credentials.json"
        private_json(credentials_file, credentials)
        private_json(Path(str(credentials_file) + ".steps"), {})
        # Misma forma que el secreto vigia/<entorno>/deploy-checks (tools/deploy_checks.py).
        deploy_checks_file = directory / "deploy-checks.json"
        private_json(deploy_checks_file, {"frontend": invitation})
        document = {
            "base_url": app.url,
            "store_origin": aws.url,
            "users": public,
            "credentials_file": str(credentials_file),
            "deploy_checks_file": str(deploy_checks_file),
            "ca_file": str(tls.ca_file),
            "organization_id": str(fleet.organization_id),
            "plant_ids": [str(value) for value in fleet.plant_ids],
            "zone_ids": [str(value) for value in fleet.zone_ids],
            "non_productive_zone_id": str(non_productive),
            "concession_id": str(fleet.console.concession_id),
            "journeys": journeys,
        }
        try:
            with published_manifest(manifest, document):
                yield document
        finally:
            # Cerrar también productores de registros antes del barrido; ExitStack es idempotente.
            stack.close()
            forbidden = tuple(
                value for account in credentials.values() for value in account.values()
            )
            # También las formas base32/base64 del secreto, no solo la codificación que usa TS.
            forbidden += tuple(
                form
                for account in credentials.values()
                for form in (
                    base64.b32encode(bytes.fromhex(account["totp_hex"])).decode().rstrip("="),
                    base64.b64encode(bytes.fromhex(account["totp_hex"])).decode(),
                )
            )
            # El token de la invitación de despliegue, también tras activarse.
            forbidden += (invitation["invitation_link"].rsplit("#", 1)[-1],)
            assert_no_leaks((ROOT / "frontend", BACKEND), forbidden)
            assert_no_leaks((directory,), forbidden, allowed=(credentials_file, deploy_checks_file))
