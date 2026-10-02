"""Pruebas de ejemplo de las historias críticas del núcleo (TASK-140; NFR-NUC-48, RNF-MAN-10).

Cada prueba recorre el criterio de aceptación de su historia (``inception/user-stories``) con un
caso concreto, de extremo a extremo, sobre la aplicación completa y PostgreSQL 16 real
(``tests/platform_support.py``). Complementan las propiedades de ``tests/properties/``: aquí está
el ejemplo que una persona puede leer y repetir.

- **H-57** Los datos de una empresa no se mezclan con los de otra (BR-NUC-01, 02, 09).
- **H-35** El expediente no se puede alterar (BR-NUC-43, 46, 47).
- **H-36** Cualquiera puede verificar la integridad, también con el paquete anterior (BR-NUC-53 a
  57).
- **H-54** Cada quien ve lo suyo, y el mando no controla lo que lo expone (BR-NUC-12 a 15).
- **H-58** La cuenta que puede configurar el sistema exige segundo factor (BR-NUC-20 a 27).
- **H-56** El cliente ve cuándo entra el proveedor y la revocación es inmediata (BR-NUC-35 a 41).
- **H-38** Los huecos de cobertura se muestran, no se esconden (BR-NUC-69 a 74).

Los contraejemplos reducidos que encuentre una propiedad entran como regresión permanente en
``tests/examples/regressions/``.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pyotp
import pytest

from tests.factories import uuid7
from tests.hierarchy_support import REJECTED_PASSWORD
from tests.identity_db import set_scope
from tests.integration.conftest import PostgresEndpoint
from tests.ledger_database import RESTRICT_VIOLATION, chain_hash, genesis_hash, record_envelope
from tests.platform_support import HOUR, T0, Platform, code_of, comparable, platform_world, stamp
from tests.verifier_packages import PackageChain, row_to_entry, write_package
from tests.writer_support import unit_context
from vigia_platform.ledger.chain.package_verifier import verify_package
from vigia_platform.shared.context import ActorKind, ActorUnit, Role, ScopeLevel

pytestmark = pytest.mark.integration

PACKAGE_KEYS = (
    "record_id", "organization_id", "plant_id", "chain_sequence", "record_type",
    "schema_version", "actor", "scope", "correlation_id", "received_at", "content",
    "content_hash", "previous_hash", "record_hash",
)  # fmt: skip


@pytest.fixture(scope="module")
def _world(postgres_endpoint: PostgresEndpoint) -> Iterator[Platform]:
    with platform_world(postgres_endpoint, "stories") as world:
        yield world


@pytest.fixture
def platform(_world: Platform) -> Platform:
    _world.resync()
    return _world


def _rows(platform: Platform, organization_id: uuid.UUID, plant_id: uuid.UUID) -> list[Any]:
    return platform.fetch(
        "SELECT * FROM ledger.ledger_record WHERE organization_id = $1 AND plant_id = $2"
        " ORDER BY chain_sequence",
        organization_id,
        plant_id,
    )


# --- H-57 --------------------------------------------------------------------------------------


def test_h57_no_table_and_no_route_mixes_two_organizations(platform: Platform) -> None:
    a, b = platform.site(), platform.site()
    for site in (a, b):
        platform.gate(site, *site.zones()[0], T0)
    b_user, _ = platform.person(b.organization_id, Role.COPASST)
    _, a_admin = platform.person(a.organization_id, Role.ADMINISTRATOR, Role.COORDINATOR_SST)
    # Por la interfaz: ni leer ni escribir lo de B, aunque se conozca el identificador.
    (b_record,) = [r["record_id"] for r in platform.records(b.organization_id)]
    for method, path, body in (
        ("GET", f"/ledger/records/{b_record}", None),
        ("PATCH", f"/users/{b_user}", {"display_name": "Otro nombre"}),
    ):
        known = platform.call(method, path, cookie=a_admin, json_body=body)
        missing = platform.call(
            method, path.rsplit("/", 1)[0] + f"/{uuid7()}", cookie=a_admin, json_body=body
        )
        assert code_of(known) == "not_found" and comparable(known) == comparable(missing)
    # En la base: toda tabla con ``organization_id``, consultada como la aplicación sin filtro,
    # no devuelve nada sin contexto y solo lo de A con el de A.
    tables = [
        row["name"]
        for row in platform.fetch(
            "SELECT DISTINCT n.nspname || '.' || c.relname AS name"
            " FROM pg_catalog.pg_attribute AS a"
            " JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid"
            " JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace"
            " WHERE n.nspname IN ('identity', 'ledger', 'shared')"
            " AND a.attname = 'organization_id' AND NOT a.attisdropped"
            " AND c.relkind IN ('r', 'p') AND NOT c.relispartition"
        )
    ]
    assert len(tables) >= 10

    async def leaked() -> dict[str, Any]:
        connection = await platform.connect_as("vigia_app")
        found: dict[str, Any] = {}
        try:
            for table in tables:
                async with connection.transaction():
                    try:
                        bare = await connection.fetchval(
                            f"SELECT count(*) FROM {table}"  # noqa: S608 - nombre del catálogo
                        )
                    except Exception as error:
                        bare = getattr(error, "sqlstate", "error")
                if bare not in (0, "42501"):
                    found[f"{table} sin contexto"] = bare
                async with connection.transaction():
                    await set_scope(connection, a.organization_id)
                    try:
                        others = await connection.fetchval(
                            f"SELECT count(*) FROM {table}"  # noqa: S608 - nombre del catálogo
                            " WHERE organization_id <> $1",
                            a.organization_id,
                        )
                    except Exception as error:
                        others = getattr(error, "sqlstate", "error")
                if others not in (0, "42501"):
                    found[f"{table} con el contexto de A"] = others
        finally:
            await connection.close()
        return found

    assert platform.run(leaked()) == {}


# --- H-35 --------------------------------------------------------------------------------------


def test_h35_parallel_writers_keep_one_unbroken_chain_that_nobody_can_rewrite(
    platform: Platform,
) -> None:
    site = platform.site(plants=1, zones_per_plant=2)
    (plant_id, first), (_, second) = site.zones()

    async def nodes() -> None:
        # Dos «nodos» enviando a la vez (y uno con su cola tras un corte), en otro orden de
        # sus propias marcas de tiempo.
        async def send(zone_id: uuid.UUID, hours: list[int]) -> None:
            for hour in hours:
                document = {
                    "zone_id": str(zone_id),
                    "plant_id": str(plant_id),
                    "gate": "use",
                    "status": "approved",
                    "resulting_mode": "productive",
                }
                context = unit_context(site.organization_id, ActorUnit.U03, kind=ActorKind.SYSTEM)
                await platform.writer.write(
                    context, "gate_state_changed", document, occurred_at=T0 + hour * HOUR
                )

        await asyncio.gather(send(first, [5, 1, 3, 0]), send(second, [4, 2, 6, 7]))

    platform.run(nodes())
    rows = _rows(platform, site.organization_id, plant_id)
    # Una cadena por organización y planta, ordenada por la recepción, sin huecos ni repetidos.
    assert [row["chain_sequence"] for row in rows] == list(range(1, 9))
    received = [row["received_at"] for row in rows]
    assert received == sorted(received)
    previous = genesis_hash(site.organization_id, plant_id)
    for row in rows:
        assert row["previous_hash"] == previous
        assert row["record_hash"] == chain_hash(record_envelope(row), previous)
        previous = row["record_hash"]
    # Ni el dueño de las tablas puede actualizar ni borrar (el disparador).

    async def attempt(statement: str) -> str | None:
        connection = await platform.connect_as("vigia_migrate")
        try:
            async with connection.transaction():
                await set_scope(connection, site.organization_id)
                await connection.execute(statement, rows[0]["record_id"])
        except Exception as error:
            return str(getattr(error, "sqlstate", type(error).__name__))
        finally:
            await connection.close()
        return None

    for statement in (
        "UPDATE ledger.ledger_record SET correlation_id = record_id WHERE record_id = $1",
        "DELETE FROM ledger.ledger_record WHERE record_id = $1",
    ):
        assert platform.run(attempt(statement)) == RESTRICT_VIOLATION


# --- H-36 --------------------------------------------------------------------------------------


def _package(platform: Platform, site: Any, plant_id: uuid.UUID, directory: Path) -> Path:
    rows = _rows(platform, site.organization_id, plant_id)
    (head,) = platform.fetch(
        "SELECT * FROM ledger.chain_head WHERE organization_id = $1 AND plant_id = $2",
        site.organization_id,
        plant_id,
    )
    keys = platform.call("GET", "/.well-known/vigia-checkpoint-keys").json()["keys"]
    chain = PackageChain(
        "ledger",
        str(plant_id),
        "chains/ledger-plant.jsonl",
        [row_to_entry("ledger", row) for row in rows],
        1,
        head["last_sequence"],
        head["last_hash"],
    )
    manifest = {
        "format": "vigia-package",
        "format_version": 1,
        "organization_id": str(site.organization_id),
        "chains": [chain.manifest()],
        "checkpoint_keys": [{"key_id": k["key_id"], "public_key": k["public_key"]} for k in keys],
    }
    directory.mkdir()
    write_package(directory, site.organization_id, [chain], [], manifest=manifest)
    return directory


def test_h36_the_package_verifies_offline_with_the_previous_one_and_names_the_altered_record(
    platform: Platform, tmp_path: Path
) -> None:
    site = platform.site()
    ((plant_id, zone_id),) = site.zones()
    for i in range(3):
        platform.gate(site, plant_id, zone_id, T0 + i * HOUR)
    anchor = platform.checkpoint(site.organization_id, plant_id)
    _, coordinator = platform.person(site.organization_id, Role.COORDINATOR_SST)
    record = platform.call("GET", f"/ledger/records/{anchor.entry_id}", cookie=coordinator).json()
    previous = tmp_path / "paquete-anterior.json"
    previous.write_text(json.dumps({k: record[k] for k in PACKAGE_KEYS}), encoding="utf-8")
    for i in range(3, 5):
        platform.gate(site, plant_id, zone_id, T0 + i * HOUR)
    platform.checkpoint(site.organization_id, plant_id)
    package = _package(platform, site, plant_id, tmp_path / "paquete")
    # Sin red, sin acceso al sistema, sin secretos del proveedor: íntegro y con el ancla.
    report = verify_package(package, [previous])
    assert report.intact, report
    assert [p.status for p in report.previous] == ["matched"]
    # Un registro alterado: falla y señala el registro exacto.
    target = package / "chains" / "ledger-plant.jsonl"
    lines = target.read_text(encoding="utf-8").splitlines()
    altered = json.loads(lines[1])
    altered["content"]["resulting_mode"] = "commissioning"
    lines[1] = json.dumps(altered, ensure_ascii=False)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    broken = verify_package(package, [previous])
    assert not broken.intact
    (chain,) = broken.chains
    assert chain.result.broken is not None
    assert (chain.result.broken.sequence, chain.result.broken.entry_id) == (
        2,
        altered["record_id"],
    )


# --- H-54 --------------------------------------------------------------------------------------


def test_h54_each_role_sees_its_scope_and_the_line_manager_cannot_touch_the_record(
    platform: Platform,
) -> None:
    site = platform.site(plants=2, zones_per_plant=1)
    (plant_a, zone_a), (plant_b, zone_b) = site.zones()
    record_a = platform.gate(site, plant_a, zone_a, T0)
    record_b = platform.gate(site, plant_b, zone_b, T0)
    _, admin = platform.person(site.organization_id, Role.ADMINISTRATOR)
    coordinator = platform.authz.add_user(site.organization_id)
    # El administrador asigna el rol declarando su alcance: coordinación SST de la planta A.
    assigned = platform.call(
        "POST",
        f"/users/{coordinator}/roles",
        cookie=admin,
        json_body={"role": "coordinator_sst", "scope_level": "plant", "scope_id": str(plant_a)},
    )
    assert assigned.status_code == 201, assigned.text
    assert (assigned.json()["scope_level"], assigned.json()["scope_id"]) == ("plant", str(plant_a))
    coordinator_cookie = platform.authz.open_session(site.organization_id, coordinator)
    listed = platform.call("GET", "/ledger/records", cookie=coordinator_cookie)
    assert listed.status_code == 200, listed.text
    seen = {item["record_id"] for item in listed.json()["items"]}
    assert str(record_a) in seen and str(record_b) not in seen
    # El mando de línea de la zona A: ninguna operación sobre el registro.
    _, line = platform.person(
        site.organization_id, Role.LINE_MANAGER, level=ScopeLevel.ZONE, scope_id=zone_a
    )
    for method, path in (
        ("GET", f"/ledger/records/{record_a}"),
        ("GET", "/ledger/records"),
        ("GET", "/audit/entries"),
        ("POST", f"/users/{coordinator}/deactivate"),
    ):
        response = platform.call(method, path, cookie=line)
        assert code_of(response) == "not_found", (path, response.text)
    # Y el administrador: configuración sí, hallazgos no.
    assert platform.call("GET", "/hierarchy", cookie=admin).status_code == 200
    assert code_of(platform.call("GET", f"/ledger/records/{record_a}", cookie=admin)) == (
        "not_found"
    )


# --- H-58 --------------------------------------------------------------------------------------


def _totp(provisioning_uri: str) -> pyotp.TOTP:
    return pyotp.TOTP(parse_qs(urlparse(provisioning_uri).query)["secret"][0])


def test_h58_the_administrator_needs_the_second_factor_and_the_session_ends_on_the_server(
    platform: Platform,
) -> None:
    genesis = platform.genesis()
    # Primer inicio de sesión: solo la inscripción.
    response, pending = platform.login(genesis.admin_email, genesis.password)
    assert response.json() == {"status": "second_factor_enrollment_required"}
    assert pending is not None
    assert platform.call("GET", "/me", cookie=pending).status_code == 401
    started = platform.call("POST", "/auth/second-factor/enroll", cookie=pending, json_body={})
    enrollment = started.json()["enrollment"]
    assert len(enrollment["recovery_codes"]) == 10
    totp = _totp(enrollment["provisioning_uri"])
    confirmed = platform.call(
        "POST",
        "/auth/second-factor/enroll",
        cookie=pending,
        json_body={"code": totp.at(platform.clock.now())},
    )
    assert confirmed.json() == {"status": "authenticated"}
    # Siguiente inicio: contraseña y código, y nada útil hasta el código.
    platform.advance(31)
    response, second = platform.login(genesis.admin_email, genesis.password)
    assert response.json() == {"status": "second_factor_required"} and second is not None
    assert platform.call("GET", "/me", cookie=second).status_code == 401
    wrong = platform.call("POST", "/auth/second-factor", cookie=second, json_body={"code": "0"})
    assert wrong.status_code == 401 and code_of(wrong) == "unauthenticated", wrong.text
    good = platform.call(
        "POST",
        "/auth/second-factor",
        cookie=second,
        json_body={"code": totp.at(platform.clock.now())},
    )
    assert good.json() == {"status": "authenticated"}
    assert platform.call("GET", "/me", cookie=second).status_code == 200
    # Contraseña nueva contra la lista de filtradas; cierre de sesión en el servidor.
    weak = platform.call(
        "POST",
        "/auth/password",
        cookie=second,
        json_body={"current_password": genesis.password, "new_password": REJECTED_PASSWORD},
    )
    assert code_of(weak) == "invalid_request"
    assert platform.call("POST", "/auth/logout", cookie=second).status_code == 204
    assert platform.call("GET", "/me", cookie=second).status_code == 401
    # Lo que se guarda es la salida del servicio de hash (aquí su doble ``fake$``), nunca la
    # contraseña tal cual.
    (credential,) = platform.fetch(
        "SELECT password_hash FROM identity.password_credential WHERE user_id = $1",
        genesis.admin_id,
    )
    assert credential["password_hash"] == f"fake${genesis.password}"


# --- H-56 --------------------------------------------------------------------------------------


def test_h56_the_client_sees_when_why_and_until_when_and_revocation_is_immediate(
    platform: Platform,
) -> None:
    site = platform.site()
    ((_, zone_id),) = site.zones()
    _, manager = platform.person(site.organization_id, Role.PLANT_MANAGER)
    _, installer = platform.installer()
    reason = "Ajuste sintético de la cámara de la zona de prensas"
    granted = platform.call(
        "POST",
        "/provider/concessions",
        cookie=installer,
        json_body={
            "client_organization_id": str(site.organization_id),
            "scope_level": "organization",
            "scope_id": str(site.organization_id),
            "reason": reason,
            "duration_hours": 4,
        },
    )
    assert granted.status_code == 201, granted.text
    concession = uuid.UUID(granted.json()["concession_id"])
    coverage = platform.call(
        "GET",
        f"/zones/{zone_id}/coverage",
        cookie=installer,
        concession=concession,
        params={"from": stamp(T0), "to": stamp(T0 + HOUR)},
    )
    assert coverage.status_code == 200, coverage.text
    # El panel del cliente: la concesión con motivo y vencimiento, y la consulta con su momento.
    panel = platform.call("GET", "/concessions", cookie=manager).json()["concessions"]
    (shown,) = [c for c in panel if c["concession_id"] == str(concession)]
    assert shown["reason"] == reason and shown["status"] == "active"
    assert shown["expires_at"] == granted.json()["expires_at"]
    queries = platform.call("GET", f"/concessions/{concession}/queries", cookie=manager).json()
    (query,) = queries["queries"]
    assert query["resource"] == "/zones/{zone_id}/coverage" and query["occurred_at"]
    # El gerente de planta revoca; la petición siguiente del proveedor ya no entra.
    revoked = platform.call("POST", f"/concessions/{concession}/revoke", cookie=manager)
    assert revoked.status_code == 200, revoked.text
    again = platform.call(
        "GET",
        f"/zones/{zone_id}/coverage",
        cookie=installer,
        concession=concession,
        params={"from": stamp(T0), "to": stamp(T0 + HOUR)},
    )
    assert again.status_code == 404 and code_of(again) == "not_found", again.text


# --- H-38 --------------------------------------------------------------------------------------


def test_h38_the_gap_is_its_own_interval_and_each_instant_has_its_state(
    platform: Platform,
) -> None:
    site = platform.site()
    ((plant_id, zone_id),) = site.zones()
    node_id = platform.node(site, plant_id, zone_id)
    platform.gate(site, plant_id, zone_id, T0 - HOUR)
    platform.communication(site, plant_id, node_id, "reachable", T0 - HOUR)
    platform.observability(site, plant_id, zone_id, node_id, T0 - HOUR)
    platform.observability(
        site, plant_id, zone_id, node_id, T0 + HOUR, state="degraded", causes=("backlight",)
    )
    platform.communication(site, plant_id, node_id, "mute", T0 + 3 * HOUR, T0 + 2 * HOUR)
    _, inspector = platform.person(site.organization_id, Role.COORDINATOR_SST)
    response = platform.call(
        "GET",
        f"/zones/{zone_id}/coverage",
        cookie=inspector,
        params={"from": stamp(T0), "to": stamp(T0 + 4 * HOUR)},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [(i["starts_at"], i["ends_at"], i["state"]) for i in body["intervals"]] == [
        (stamp(T0), stamp(T0 + HOUR), "observable"),
        (stamp(T0 + HOUR), stamp(T0 + 2 * HOUR), "degraded"),
        (stamp(T0 + 2 * HOUR), stamp(T0 + 4 * HOUR), "not_observable"),
    ]
    assert sum(body["summary"].values()) == 4 * 3_600_000
    # El estado de cada instante (lo que acompaña a un hallazgo de ese momento).
    for instant, state in (
        (T0 + timedelta(minutes=30), "observable"),
        (T0 + HOUR + timedelta(minutes=30), "degraded"),
        (T0 + 3 * HOUR, "not_observable"),
    ):
        at = platform.call(
            "GET",
            f"/zones/{zone_id}/coverage/at",
            cookie=inspector,
            params={"instant": stamp(instant)},
        )
        assert at.status_code == 200 and at.json()["state"] == state, (instant, at.text)
    assert "no ocurrió" not in response.text and "clear" not in response.text
