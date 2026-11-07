"""Siembra del entorno de integración, dentro de la imagen de ``vigia-platform`` (TASK-545).

Se ejecuta con ``docker compose -f compose.integration.yml run --rm vigia-admin python
/integration/seed.py <orden>`` (lo hace ``bootstrap.sh``). Usa los **mismos servicios de
aplicación** que ``vigia-api``, como el aprovisionamiento de ``tests/conformance`` (no importa
nada de ``tests/``, que no viaja en la imagen), y solo datos sintéticos.

Órdenes (cada una imprime una línea JSON y nada que sea un secreto, salvo ``issue-code`` y
``session``, que existen para entregar un secreto de un solo uso a quien las lanza):

- ``provider-id``: escribe en ``/shared`` el identificador de la organización proveedora que creó
  ``vigia-admin bootstrap`` (lo leen ``vigia-api`` y ``vigia-worker``).
- ``site``: idempotente. La organización cliente ``INT-CLI`` con su planta y dos zonas, los
  umbrales de flota, y **un usuario por rol** con contraseña y segundo factor (TOTP) de prueba.
  Las credenciales de los usuarios que crea esta ejecución quedan en ``/state/credentials.json``
  (``0600``): ``bootstrap.sh`` las copia fuera del árbol del repositorio. Una segunda ejecución no
  cambia nada ni genera credenciales nuevas.
- ``issue-code``: declara un nodo nuevo en la primera zona y emite su código de alta (un solo uso).
  Es lo que haría la persona instaladora; la concesión de la proveedora se renueva si hace falta.
- ``session <rol>``: abre una sesión de la persona de ese rol (segundo factor ya verificado) y
  escribe el valor de la cookie ``__Host-vigia_session``. Solo para pruebas y diagnóstico.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import os
import secrets
import sys
import uuid
from pathlib import Path
from typing import Any, Final

import asyncpg  # type: ignore[import-untyped]

from vigia_platform.catalog.adapters.http import CATALOG_STATE_KEY, CatalogHttp
from vigia_platform.catalog.application.admission import AdmissionRequest
from vigia_platform.catalog.domain.admission import AdmissionAnswers
from vigia_platform.catalog.domain.catalog_version import (
    InitialZoneParameters,
    NewStandard,
    StandardDraft,
)
from vigia_platform.catalog.domain.enums import GateKind
from vigia_platform.catalog.domain.zone_camera import ZoneCamera
from vigia_platform.fleet.adapters.http import FLEET_STATE_KEY, FleetHttp
from vigia_platform.identity.application.hierarchy import PlantSpec, ZoneSpec
from vigia_platform.identity.auth.passwords import hash_password
from vigia_platform.identity.auth.second_factor import credential_aad
from vigia_platform.identity.auth.sessions import (
    ABSOLUTE_TIMEOUT,
    IDLE_TIMEOUT,
    SessionCookie,
    new_session_cookie,
)
from vigia_platform.identity.authz.context import with_unit
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.identity.domain.privacy_notice import CURRENT_PRIVACY_NOTICE_VERSION
from vigia_platform.shared.api.app import AppConfig
from vigia_platform.shared.clock import SystemClock
from vigia_platform.shared.context import ActorUnit, Role, ScopeContext
from vigia_platform.shared.crypto import EnvelopeCipher
from vigia_platform.shared.runtime.api import compose_api_runtime
from vigia_platform.shared.runtime.config import RuntimeConfig
from vigia_platform.shared.secrets import AwsSettings, KmsAdapter

from vigia_contracts.models.enumerations import (  # isort: skip
    CameraRoleInZone,
    GateStatus,
    PredicateFamily,
)

NAMESPACE: Final = uuid.UUID("5d0b7a2e-9f6c-4d51-8a3e-2f7c1b6e4d90")
CLIENT_CODE: Final = "INT-CLI"
PLANT_CODE: Final = "INT-PLANTA"
ZONE_CODES: Final = ("INT-ZONA-1", "INT-ZONA-2")
ROLES: Final = tuple(role for role in Role if role is not Role.PLATFORM_OPERATOR)
THRESHOLDS: Final = {
    "queue_pending_threshold": 100,
    "queue_age_threshold_minutes": 30,
    "clock_drift_threshold_ms": 5_000,
}
"""Los valores de diseño de ``fleet.domain.fleet_thresholds``, escritos de forma explícita."""
CONCESSION_DAYS: Final = 7
PROVIDER_FILE: Final = Path("/shared/provider_organization_id")
BREACH_KIND_FILE: Final = Path("/shared/breach_kind")
"""``sintetica`` o ``real``: lo que dejó ``init_state.py`` (``bootstrap.sh`` lo muestra)."""
CREDENTIALS_FILE: Final = Path("/state/credentials.json")
CLOCK: Final = SystemClock()
MAX_SEGMENT_MS: Final = 60_000
REASON: Final = "Publicación del catálogo de la zona de integración"


def user_id(role: Role) -> uuid.UUID:
    """Identificador estable de la persona sintética de ``role`` (la siembra es idempotente)."""
    return uuid.uuid5(NAMESPACE, f"usuario/{role.value}")


def uuid7() -> uuid.UUID:
    value = int.from_bytes(os.urandom(16), "big")
    value &= ~(0xF << 76)
    value |= 0x7 << 76
    value &= ~(0x3 << 62)
    value |= 0x2 << 62
    return uuid.UUID(int=value)


async def connect() -> Any:
    """Superusuario ``vigia`` (contraseña por ``PGPASSFILE``): solo las tareas puntuales."""
    return await asyncpg.connect(
        host=os.environ["PGHOST"],
        port=int(os.environ["PGPORT"]),
        user=os.environ["PGUSER"],
        database=os.environ["PGDATABASE"],
    )


async def provider_organization(connection: Any) -> uuid.UUID:
    row = await connection.fetchrow(
        "SELECT organization_id FROM identity.organization WHERE kind = 'provider'"
        " ORDER BY created_at LIMIT 1"
    )
    if row is None:
        raise SystemExit("no hay organización proveedora: corre antes vigia-admin bootstrap")
    return uuid.UUID(str(row["organization_id"]))


async def provider_id() -> dict[str, Any]:
    connection = await connect()
    try:
        provider = await provider_organization(connection)
    finally:
        await connection.close()
    PROVIDER_FILE.write_text(f"{provider}\n", encoding="ascii")
    kind = "desconocida"
    if BREACH_KIND_FILE.exists():
        kind = BREACH_KIND_FILE.read_text(encoding="ascii").strip()
    return {
        "command": "provider-id",
        "provider_organization_id": str(provider),
        "breach_list": kind,
    }


# --- Personas -----------------------------------------------------------------------------------


async def operator_of(connection: Any, provider: uuid.UUID) -> uuid.UUID:
    row = await connection.fetchrow(
        "SELECT user_id FROM identity.user_account WHERE organization_id = $1"
        " ORDER BY created_at LIMIT 1",
        provider,
    )
    return uuid.UUID(str(row["user_id"]))


async def ensure_organization(
    connection: Any, provider: uuid.UUID, operator: uuid.UUID
) -> uuid.UUID:
    row = await connection.fetchrow(
        "SELECT organization_id FROM identity.organization WHERE code = $1", CLIENT_CODE
    )
    if row is not None:
        return uuid.UUID(str(row["organization_id"]))
    organization = uuid.uuid5(NAMESPACE, f"organizacion/{CLIENT_CODE}")
    await connection.execute(
        "INSERT INTO identity.organization (organization_id, code, name, kind, created_at,"
        " created_by) VALUES ($1, $2, 'Cliente de integración', 'client', $3, $4)",
        organization,
        CLIENT_CODE,
        CLOCK.now(),
        operator,
    )
    return organization


async def ensure_user(
    connection: Any,
    cipher: EnvelopeCipher,
    *,
    role: Role,
    organization: uuid.UUID,
    operator: uuid.UUID,
    zone: uuid.UUID | None,
) -> dict[str, str] | None:
    """La persona del rol, con contraseña y TOTP. Las credenciales solo si las crea esta vez."""
    user = user_id(role)
    if await connection.fetchrow("SELECT 1 FROM identity.user_account WHERE user_id = $1", user):
        return None
    password = secrets.token_urlsafe(24)
    secret = secrets.token_bytes(20)
    hashed = hash_password(password)
    sealed = await cipher.encrypt(secret, credential_aad(organization, user))
    now = CLOCK.now()
    scope_level, scope_id = (
        ("zone", zone) if role is Role.LINE_MANAGER else ("organization", organization)
    )
    if scope_id is None:
        raise SystemExit("la persona responsable de línea necesita una zona")
    email = f"{role.value}@integracion.example.test"
    async with connection.transaction():
        await connection.execute(
            "INSERT INTO identity.user_account (user_id, organization_id, email, display_name,"
            " status, second_factor_required, second_factor_enrolled_at, created_at,"
            " privacy_notice_version_accepted)"
            " VALUES ($1, $2, $3, $4, 'active', true, $5, $5, $6)",
            user,
            organization,
            email,
            f"Persona de prueba ({role.value})",
            now,
            CURRENT_PRIVACY_NOTICE_VERSION,
        )
        await connection.execute(
            "INSERT INTO identity.role_assignment (assignment_id, organization_id, user_id, role,"
            " scope_level, scope_id, assigned_at, assigned_by)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            uuid7(),
            organization,
            user,
            role.value,
            scope_level,
            scope_id,
            now,
            operator,
        )
        await connection.execute(
            "INSERT INTO identity.password_credential (user_id, organization_id, password_hash,"
            " algorithm_version, updated_at) VALUES ($1, $2, $3, $4, $5)",
            user,
            organization,
            hashed.encoded,
            f"argon2id-v{hashed.algorithm_version}",
            now,
        )
        await connection.execute(
            "INSERT INTO identity.totp_credential (user_id, organization_id, secret_encrypted,"
            " data_key_wrapped, enrolled_at) VALUES ($1, $2, $3, $4, $5)",
            user,
            organization,
            sealed.ciphertext,
            sealed.wrapped_key,
            now,
        )
    return {
        "email": email,
        "password": password,
        "totp_base32": base64.b32encode(secret).decode("ascii").rstrip("="),
    }


async def mint_session(connection: Any, organization: uuid.UUID, user: uuid.UUID) -> SessionCookie:
    cookie = new_session_cookie(organization)
    now = CLOCK.now()
    await connection.execute(
        "INSERT INTO identity.session (session_id_hash, user_id, organization_id, created_at,"
        " last_seen_at, idle_expires_at, absolute_expires_at, second_factor_verified,"
        " origin_hash) VALUES ($1, $2, $3, $4, $4, $5, $6, true, $7)",
        cookie.session_id_hash,
        user,
        organization,
        now,
        now + IDLE_TIMEOUT,
        now + ABSOLUTE_TIMEOUT,
        secrets.token_hex(32),
    )
    return cookie


async def renew_concession(connection: Any, *, client: uuid.UUID, provider: uuid.UUID) -> uuid.UUID:
    """Una concesión vigente de la persona instaladora sobre la organización cliente."""
    installer = user_id(Role.PROVIDER_INSTALLER)
    now = CLOCK.now()
    row = await connection.fetchrow(
        "SELECT concession_id FROM identity.provider_concession WHERE organization_id = $1"
        " AND provider_user_id = $2 AND status = 'active' AND expires_at > $3"
        " ORDER BY expires_at DESC LIMIT 1",
        client,
        installer,
        now + dt.timedelta(days=1),
    )
    if row is not None:
        return uuid.UUID(str(row["concession_id"]))
    concession = uuid7()
    await connection.execute(
        "INSERT INTO identity.provider_concession (concession_id, organization_id,"
        " provider_user_id, provider_organization_id, scope_level, scope_id, reason, granted_at,"
        " expires_at, status) VALUES ($1, $2, $3, $4, 'organization', $2,"
        " 'Puesta en marcha del entorno de integración', $5, $6, 'active')",
        concession,
        client,
        installer,
        provider,
        now,
        now + dt.timedelta(days=CONCESSION_DAYS),
    )
    return concession


# --- Servicios ----------------------------------------------------------------------------------


class Runtime:
    """El ``AppRuntime`` de ``vigia-api`` en este proceso (la misma raíz de composición)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    @classmethod
    async def open(cls) -> Runtime:
        environ = os.environ
        app = await compose_api_runtime(
            AppConfig.from_environ(environ), RuntimeConfig.from_environ(environ)
        )
        # Lo que hace el arranque de `vigia-api` antes de atender: sellar los registros (sin ello,
        # publicar un catálogo falla con `outbox_not_ready`) y cargar las claves de firma.
        for synchronize in app.registries:
            await synchronize()
        await app.signing.start()
        return cls(app)

    async def close(self) -> None:
        await self.app.database.dispose()

    async def context(
        self, cookie: SessionCookie, concession: uuid.UUID | None = None
    ) -> ScopeContext:
        scope = await self.app.sessions.context_from_session(cookie, concession_id=concession)
        context: ScopeContext = scope.context
        return context

    def cipher(self) -> EnvelopeCipher:
        environ = os.environ
        kms = KmsAdapter(
            AwsSettings(
                region=environ["AWS_REGION"], endpoint_url=environ.get("VIGIA_AWS_ENDPOINT_URL")
            )
        )
        return EnvelopeCipher(kms, environ["VIGIA_SECRETS_KEY_ARN"], CLOCK)


async def ensure_plant_and_zones(
    runtime: Runtime, connection: Any, client: uuid.UUID, administrator: SessionCookie
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    row = await connection.fetchrow(
        "SELECT plant_id FROM identity.plant WHERE organization_id = $1 AND code = $2",
        client,
        PLANT_CODE,
    )
    hierarchy = runtime.app.identity.hierarchy
    catalog: CatalogHttp = runtime.app.state[CATALOG_STATE_KEY]
    if row is None:
        plant = await hierarchy.create_plant(
            await runtime.context(administrator),
            PlantSpec(
                code=PLANT_CODE,
                name="Planta de integración",
                country="CO",
                data_region="us-east-1",
                timezone="America/Bogota",
            ),
        )
        plant_id = plant.plant_id
        await catalog.admissions.evaluate(
            await runtime.context(administrator),
            plant_id,
            AdmissionRequest(
                family=PredicateFamily.COEXISTENCE,
                answers=AdmissionAnswers(standard=True, remedy=True, subject=True),
            ),
        )
    else:
        plant_id = uuid.UUID(str(row["plant_id"]))
    zones: list[uuid.UUID] = []
    for code in ZONE_CODES:
        found = await connection.fetchrow(
            "SELECT zone_id FROM identity.zone WHERE plant_id = $1 AND code = $2", plant_id, code
        )
        if found is None:
            zone = await hierarchy.create_zone(
                await runtime.context(administrator),
                plant_id,
                ZoneSpec(code=code, name=f"Zona de integración {code[-1]}"),
            )
            zones.append(zone.zone_id)
        else:
            zones.append(uuid.UUID(str(found["zone_id"])))
    return plant_id, zones


# --- Catálogo y compuertas de cada zona (lo que exige la alta del nodo) -------------------------


def _camera(zone: uuid.UUID, index: int) -> ZoneCamera:
    return ZoneCamera(
        camera_id=uuid.uuid5(zone, f"camara-{index}"),
        code=f"CM-{index}",
        role_in_zone=CameraRoleInZone.PRIMARY if index == 0 else CameraRoleInZone.REDUNDANT,
        declared_min_fps=5.0,
        stream_reference=f"cam-{index}",
    )


def _new_standard(zone: uuid.UUID) -> NewStandard:
    """La misma norma sintética de coexistencia que el arnés de conformidad."""
    cameras = (_camera(zone, 0), _camera(zone, 1))
    return NewStandard(
        draft=StandardDraft(
            family=PredicateFamily.COEXISTENCE,
            title_es="Coexistencia en la celda",
            declared_text="Nadie permanece en la celda mientras la máquina está energizada.",
            predicate={
                "all_of": [{"presence": True}, {"signal_role": "energy", "value": "asserted"}],
                "min_duration_ms": 0,
            },
        ),
        initial=InitialZoneParameters(
            cameras=cameras,
            required_count=1,
            required_camera_ids=(cameras[0].camera_id,),
            signals=(
                {
                    "signal_id": str(uuid.uuid5(zone, "senal-energia")),
                    "code": "SG-1",
                    "role": "energy",
                    "asserted_level": "high",
                    "source": {"reader": "plc-1", "channel": 1},
                    "description_es": "Energía de la prensa",
                },
            ),
            thresholds={"review": 0.4, "publication": 0.8},
            clip_window={"pre_seconds": 10, "post_seconds": 10},
            episode={"grouping_window_ms": 3000, "max_segment_ms": MAX_SEGMENT_MS},
        ),
    )


async def ensure_catalog_and_gates(
    runtime: Runtime,
    connection: Any,
    zones: list[uuid.UUID],
    *,
    administrator: SessionCookie,
    installer: SessionCookie,
    concession: uuid.UUID,
    approve_usage: bool = True,
) -> None:
    """Catálogo v1 firmado de cada zona; montaje aprobado en todas y uso en la primera.

    Sin catálogo ni sobre de compuerta en una zona, la alta del nodo responde 503
    (``fleet.credentials``). La administradora publica el catálogo; la persona instaladora, con su
    concesión, aprueba las compuertas (lo que harán el acta de alcance y el acuerdo de uso: aquí
    el respaldo es un identificador sintético).
    """
    catalog: CatalogHttp = runtime.app.state[CATALOG_STATE_KEY]
    gates = catalog.gates
    for index, zone in enumerate(zones):
        if (
            await connection.fetchrow(
                "SELECT 1 FROM catalog.zone_catalog_version WHERE zone_id = $1", zone
            )
            is None
        ):
            await catalog.catalog.publish_catalog_version(
                await runtime.context(administrator), zone, _new_standard(zone), REASON
            )
        state = await connection.fetchrow(
            "SELECT mounting->>'status' AS mounting, usage->>'status' AS usage"
            " FROM catalog.zone_gate_state WHERE zone_id = $1",
            zone,
        )
        wanted = [(GateKind.MOUNTING, "mounting")] + (
            [(GateKind.USAGE, "usage")] if index == 0 and approve_usage else []
        )
        for kind, column in wanted:
            if state is not None and state[column] == "approved":
                continue
            _, authorized = await gates.zone(
                await runtime.context(installer, concession), zone, PermissionKey.COMMISSIONING_RUN
            )
            writer = with_unit(authorized, ActorUnit.U03)

            async def transition(
                transaction: Any,
                kind: GateKind = kind,
                zone: uuid.UUID = zone,
                writer: Any = writer,
            ) -> Any:
                return await gates.transition_gate(
                    transaction, writer, zone, kind, GateStatus.APPROVED, uuid.uuid4()
                )

            await gates.run(writer, transition)


async def free_zone(
    runtime: Runtime,
    connection: Any,
    plant: uuid.UUID,
    *,
    administrator: SessionCookie,
    installer: SessionCookie,
    concession: uuid.UUID,
) -> uuid.UUID:
    """Una zona de la planta sin nodo asignado; si no queda ninguna, crea la siguiente.

    Una zona la sirve un solo nodo (``fleet_zone_already_served``): cada alta nueva necesita la
    suya, con su catálogo y su compuerta de montaje como las dos primeras.
    """
    rows = await connection.fetch(
        "SELECT z.zone_id FROM identity.zone z WHERE z.plant_id = $1 AND NOT EXISTS ("
        " SELECT 1 FROM identity.zone_node_assignment a"
        " WHERE a.zone_id = z.zone_id AND a.unassigned_at IS NULL) ORDER BY z.code",
        plant,
    )
    if rows:
        return uuid.UUID(str(rows[0]["zone_id"]))
    total = await connection.fetchval(
        "SELECT count(*) FROM identity.zone WHERE plant_id = $1", plant
    )
    code = f"INT-ZONA-{total + 1}"
    zone = await runtime.app.identity.hierarchy.create_zone(
        await runtime.context(administrator),
        plant,
        ZoneSpec(code=code, name=f"Zona de integración {total + 1}"),
    )
    await ensure_catalog_and_gates(
        runtime,
        connection,
        [zone.zone_id],
        administrator=administrator,
        installer=installer,
        concession=concession,
        approve_usage=False,
    )
    return zone.zone_id


def merge_credentials(new: dict[str, dict[str, str]]) -> None:
    if not new:
        return
    current: dict[str, Any] = {}
    if CREDENTIALS_FILE.exists():
        current = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
    current.update(new)
    temporary = CREDENTIALS_FILE.with_name(".credentials.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(current, output, indent=2, sort_keys=True)
    os.replace(temporary, CREDENTIALS_FILE)


async def site() -> dict[str, Any]:
    runtime = await Runtime.open()
    connection = await connect()
    try:
        provider = await provider_organization(connection)
        operator = await operator_of(connection, provider)
        client = await ensure_organization(connection, provider, operator)
        # Primero las personas de la organización cliente (la administradora crea la planta).
        created: dict[str, dict[str, str]] = {}
        cipher = runtime.cipher()
        zone_for_line_manager: uuid.UUID | None = None
        for role in ROLES:
            organization = provider if role is Role.PROVIDER_INSTALLER else client
            if role is Role.LINE_MANAGER:
                continue  # necesita una zona: va después
            credentials = await ensure_user(
                connection,
                cipher,
                role=role,
                organization=organization,
                operator=operator,
                zone=None,
            )
            if credentials is not None:
                created[role.value] = credentials
        administrator = await mint_session(connection, client, user_id(Role.ADMINISTRATOR))
        plant, zones = await ensure_plant_and_zones(runtime, connection, client, administrator)
        zone_for_line_manager = zones[0]
        credentials = await ensure_user(
            connection,
            cipher,
            role=Role.LINE_MANAGER,
            organization=client,
            operator=operator,
            zone=zone_for_line_manager,
        )
        if credentials is not None:
            created[Role.LINE_MANAGER.value] = credentials
        concession = await renew_concession(connection, client=client, provider=provider)
        installer = await mint_session(connection, provider, user_id(Role.PROVIDER_INSTALLER))
        await ensure_catalog_and_gates(
            runtime,
            connection,
            zones,
            administrator=administrator,
            installer=installer,
            concession=concession,
        )
        # Los umbrales de flota los fija la persona instaladora, con su concesión (`fleet.manage`).
        thresholds = runtime.app.state[FLEET_STATE_KEY].thresholds
        await thresholds.put_thresholds(
            await runtime.context(installer, concession), plant, **THRESHOLDS
        )
        merge_credentials(created)
    finally:
        await connection.close()
        await runtime.close()
    return {
        "command": "site",
        "organization_code": CLIENT_CODE,
        "plant_code": PLANT_CODE,
        "zone_codes": list(ZONE_CODES),
        "roles": [role.value for role in ROLES],
    }


async def issue_code() -> dict[str, Any]:
    runtime = await Runtime.open()
    connection = await connect()
    try:
        provider = await provider_organization(connection)
        client = await ensure_organization(
            connection, provider, await operator_of(connection, provider)
        )
        row = await connection.fetchrow(
            "SELECT plant_id FROM identity.plant WHERE organization_id = $1 AND code = $2",
            client,
            PLANT_CODE,
        )
        if row is None:
            raise SystemExit("falta la planta de integración: corre antes seed.py site")
        plant = uuid.UUID(str(row["plant_id"]))
        concession = await renew_concession(connection, client=client, provider=provider)
        cookie = await mint_session(connection, provider, user_id(Role.PROVIDER_INSTALLER))
        administrator = await mint_session(connection, client, user_id(Role.ADMINISTRATOR))
        zone = await free_zone(
            runtime,
            connection,
            plant,
            administrator=administrator,
            installer=cookie,
            concession=concession,
        )
        fleet: FleetHttp = runtime.app.state[FLEET_STATE_KEY]
        declared = await fleet.declarations.declare(
            await runtime.context(cookie, concession),
            plant,
            code=f"ND-INT-{secrets.token_hex(3).upper()}",
            zone_ids=[zone],
        )
        issued = await fleet.enrollment_codes.issue(
            await runtime.context(cookie, concession), declared.node_id
        )
    finally:
        await connection.close()
        await runtime.close()
    return {
        "command": "issue-code",
        "node_id": str(declared.node_id),
        "organization_id": str(client),
        "plant_id": str(plant),
        "zone_id": str(zone),
        "enrollment_code": issued.code,
    }


async def session(role_name: str) -> dict[str, Any]:
    role = Role(role_name)
    if role in {Role.PROVIDER_INSTALLER, Role.PLATFORM_OPERATOR}:
        raise SystemExit("session solo abre sesiones de la organización cliente")
    connection = await connect()
    try:
        provider = await provider_organization(connection)
        client = await ensure_organization(
            connection, provider, await operator_of(connection, provider)
        )
        cookie = await mint_session(connection, client, user_id(role))
    finally:
        await connection.close()
    return {"command": "session", "cookie": cookie.value}


def main(argv: list[str]) -> int:
    commands = {"provider-id", "site", "issue-code", "session"}
    if not argv or argv[0] not in commands:
        print("uso: seed.py provider-id|site|issue-code|session <rol>", file=sys.stderr)
        return 2
    if argv[0] == "provider-id":
        result = asyncio.run(provider_id())
    elif argv[0] == "site":
        result = asyncio.run(site())
    elif argv[0] == "issue-code":
        result = asyncio.run(issue_code())
    else:
        result = asyncio.run(session(argv[1] if len(argv) > 1 else Role.ADMINISTRATOR.value))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
