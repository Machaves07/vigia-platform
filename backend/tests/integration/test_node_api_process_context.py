"""Valores de proceso por mTLS contra PostgreSQL 16 real (TASK-593; A-129; BR-GOB-84).

Las rutas reales del nodo (cadena fija, ``NodeApiGate`` con la identidad por certificado contra la
base) y los servicios que compone la raíz, como ``vigia_app``, con el catálogo de la zona en una
versión que declara dos variables de proceso (velocidad de la máquina, analógica, y jumbo en
posición, de estado sobre la señal declarada):

- **Hallazgo y detección**: sin bloque, aceptado; con el bloque, aceptado y el contenido guardado en
  el expediente tiene, sin ``receipt``, el mismo hash canónico (RFC 8785) que la presentación del
  nodo; con una variable no declarada, que falta o de otra clase, ``schema_invalid`` con el campo
  exacto del paso 7 y nada escrito; repetida, el modelo la rechaza antes (``field =
  process_context.variables``).
- **Concurrencia**: dos envíos simultáneos del mismo hallazgo con bloque, por dos instancias,
  dejan un registro y un ``accepted_duplicate`` con el mismo ``Receipt``.
- **Foto del episodio**: con bloque guarda la columna ``process_context`` (``gob_0046``); sin
  bloque, ``NULL``; con un bloque que no concuerda, ``schema_invalid`` y ninguna fila; la purga
  por vencimiento borra la fila entera. La foto ``productive`` (TASK-603) guarda su bloque igual
  y uno que no concuerda la rechaza en el mismo paso 7.
- **Migración** ``gob_0046`` sobre una base en ``gob_0045`` con fotos: cada fila queda con
  ``process_context`` nulo y ningún otro valor cambia; el ``INSERT`` de la imagen anterior (sin la
  columna) sigue funcionando. Solo hacia adelante (NFR-NUC-14, ``MIG001``): sin ``downgrade``.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import hashlib
import json
import secrets
import uuid
from collections.abc import Iterator
from typing import Any, Final

import asyncpg  # type: ignore[import-untyped]
import httpx
import pytest
from vigia_contracts.canonical import canonicalize
from vigia_contracts.models import api

from tests.fleet_alarm_support import system
from tests.fleet_episode_photo_support import PhotoStack
from tests.fleet_ingest_support import IngestSite, ingest_stack
from tests.identity_db import MigratedDatabase
from tests.integration.conftest import PostgresEndpoint
from tests.integration.test_roles import run_alembic
from vigia_platform.fleet.application.episode_photo_purge import EpisodePhotoPurger
from vigia_platform.fleet.domain.ingest_order import IngestKind
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.migration_credentials import (
    APP_PASSWORD_VARIABLE,
    MIGRATE_PASSWORD_VARIABLE,
)

pytestmark = pytest.mark.integration

DAY: Final = dt.timedelta(days=1)
SPEED: Final = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
JUMBO: Final = "2b3c4d5e-6f70-4b8c-9d0e-1f2a3b4c5d6e"
CATALOG_VERSION: Final = 2


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[PhotoStack]:
    with ingest_stack(postgres_endpoint, "process_context") as built:
        photos = PhotoStack(built)
        try:
            yield photos
        finally:
            photos.run(photos.release())


@pytest.fixture(autouse=True)
def _release_instances(stack: PhotoStack) -> Iterator[None]:
    yield
    stack.run(stack.ingest.release())
    stack.run(stack.release())


def _declaration(site: IngestSite) -> dict[str, Any]:
    return {
        "before_ms": 30_000,
        "variables": [
            {
                "variable_id": SPEED,
                "code": "VEL-MAQ",
                "kind": "analog",
                "description_es": "Velocidad de la máquina",
                "source": {"reader": "a", "channel": 1},
                "unit": "m_per_min",
                "scale": {"at_4_ma": 0.0, "at_20_ma": 2000.0},
                "decimals": 0,
            },
            {
                "variable_id": JUMBO,
                "code": "JUMBO-POS",
                "kind": "binary",
                "description_es": "Jumbo en posición",
                "signal_id": str(site.signal_id),
                "states": "in_position_out",
            },
        ],
    }


def _site(stack: PhotoStack) -> IngestSite:
    """Un nodo con su zona, montaje y uso aprobados y la versión 2 del catálogo con variables."""
    site = stack.site()
    ingest = stack.ingest
    issued = stack.now() - 300 * DAY
    payload = ingest.catalog(site, issued, CATALOG_VERSION)
    payload["process_context"] = _declaration(site)
    api.parse_zone_catalog(json.dumps(payload).encode())
    ingest.execute(
        "UPDATE catalog.zone_catalog_version SET superseded_at = $3"
        " WHERE zone_id = $1 AND catalog_version = $2",
        site.zone_id,
        CATALOG_VERSION - 1,
        issued,
    )
    ingest.execute(
        "INSERT INTO catalog.zone_catalog_version (organization_id, plant_id, zone_id,"
        " catalog_version, issued_at, issued_by, role_in_use, reason_es, changed_fields,"
        " payload, envelope, single_occupancy, ledger_record_id)"
        " VALUES ($1, $2, $3, $4, $5, $6, 'administrator', 'Variables de proceso sintéticas',"
        " ARRAY['standards'], $7::jsonb, $8::jsonb, false, $9)",
        site.organization_id,
        site.plant_id,
        site.zone_id,
        CATALOG_VERSION,
        issued,
        ingest.authz.operator_id,
        json.dumps(payload),
        json.dumps({"payload": payload}),
        uuid.uuid4(),
    )
    return site


def _block(to_offset_ms: int) -> dict[str, Any]:
    return {
        "from_offset_ms": -30_000,
        "to_offset_ms": to_offset_ms,
        "variables": [
            {
                "variable_id": JUMBO,
                "kind": "binary",
                "at_event": {"value": "asserted", "valid": True, "offset_ms": 0},
                "changes": [{"offset_ms": -30_000, "value": "asserted"}],
                "truncated": False,
            },
            {
                "variable_id": SPEED,
                "kind": "analog",
                "at_event": {"value": 1397.0, "valid": True, "offset_ms": 0},
                "min": 1390.0,
                "max": 1400.0,
                "last": 1397.0,
                "samples": [
                    {"offset_ms": -10_000, "value": 1390.0},
                    {"offset_ms": 0, "value": 1397.0},
                ],
            },
        ],
    }


def _record(stack: PhotoStack, site: IngestSite, kind: IngestKind) -> dict[str, Any]:
    ingest = stack.ingest
    if kind is IngestKind.FINDING:
        document = ingest.finding(site, standard_version=CATALOG_VERSION)
    else:
        document = ingest.detection(site)
        document["standard"] = {**document["standard"], "version": CATALOG_VERSION}
    return {**document, "process_context": _block(30_000)}


def _written(stack: PhotoStack, site: IngestSite, kind: IngestKind, key: str) -> list[Any]:
    return [
        record
        for record in stack.ingest.records(kind.record_type, site.organization_id)
        if record["content"][kind.id_field] == key
    ]


def _rejection(response: httpx.Response) -> tuple[str | None, str | None]:
    body = response.json()
    return body.get("code"), body.get("field")


KINDS: Final = (IngestKind.FINDING, IngestKind.DETECTION_FOR_REVIEW)


@pytest.mark.parametrize("kind", KINDS, ids=["hallazgo", "deteccion"])
def test_without_the_block_the_record_is_accepted(stack: PhotoStack, kind: IngestKind) -> None:
    site = _site(stack)
    document = _record(stack, site, kind)
    del document["process_context"]
    response = stack.ingest.post(site, kind, document)
    assert response.status_code == 200, response.text
    (record,) = _written(stack, site, kind, document[kind.id_field])
    assert "process_context" not in record["content"]


@pytest.mark.parametrize("kind", KINDS, ids=["hallazgo", "deteccion"])
def test_with_the_block_the_record_is_accepted_and_kept_byte_for_byte(
    stack: PhotoStack, kind: IngestKind
) -> None:
    site = _site(stack)
    document = _record(stack, site, kind)
    response = stack.ingest.post(site, kind, document)
    assert response.status_code == 200, response.text
    assert api.parse_receipt(response.content).status.value == "accepted"
    (record,) = _written(stack, site, kind, document[kind.id_field])
    stored = {key: value for key, value in record["content"].items() if key != "receipt"}
    assert hashlib.sha256(canonicalize(stored)).hexdigest() == (
        hashlib.sha256(canonicalize(document)).hexdigest()
    )
    assert record["content"]["process_context"] == document["process_context"]


def _mismatch(document: dict[str, Any], case: str) -> str:
    variables = document["process_context"]["variables"]
    if case == "no_declarada":
        variables[1] = {**variables[1], "variable_id": str(uuid.uuid4())}
        return "process_context.variables[1]"
    if case == "falta":
        del variables[0]
        return "process_context.variables"
    if case == "otra_clase":
        variables[0] = {
            "variable_id": JUMBO,
            "kind": "analog",
            "at_event": {"valid": False, "offset_ms": 0},
            "samples": [],
        }
        return "process_context.variables[0]"
    variables.append(copy.deepcopy(variables[0]))  # repetida: la rechaza el modelo
    return "process_context.variables"


@pytest.mark.parametrize("kind", KINDS, ids=["hallazgo", "deteccion"])
@pytest.mark.parametrize("case", ["no_declarada", "falta", "otra_clase", "repetida"])
def test_a_block_that_does_not_match_is_schema_invalid_and_nothing_is_written(
    stack: PhotoStack, kind: IngestKind, case: str
) -> None:
    site = _site(stack)
    document = _record(stack, site, kind)
    field = _mismatch(document, case)
    response = stack.ingest.post(site, kind, document)
    assert response.status_code in {400, 422}, response.text
    assert _rejection(response) == ("schema_invalid", field)
    assert _written(stack, site, kind, document[kind.id_field]) == []


def test_two_simultaneous_submissions_leave_one_record_and_one_duplicate(
    stack: PhotoStack,
) -> None:
    site = _site(stack)
    kind = IngestKind.FINDING
    document = _record(stack, site, kind)
    ingest = stack.ingest
    second = ingest.instance()

    async def both() -> list[httpx.Response]:
        return list(
            await asyncio.gather(
                ingest.send(site, kind, document), ingest.send(site, kind, document, second)
            )
        )

    responses = stack.run(both())
    assert [response.status_code for response in responses] == [200, 200]
    receipts = [api.parse_receipt(response.content) for response in responses]
    assert sorted(receipt.status.value for receipt in receipts) == [
        "accepted",
        "accepted_duplicate",
    ]
    assert receipts[0].platform_record_id == receipts[1].platform_record_id
    assert len(_written(stack, site, kind, document[kind.id_field])) == 1


# --- Foto del episodio ---------------------------------------------------------------------------


def _photo(stack: PhotoStack, site: IngestSite, *, block: bool) -> dict[str, Any]:
    document = stack.photo(site, standard_version=CATALOG_VERSION)
    if block:
        document["process_context"] = _block(4_000)
    return document


def _column(stack: PhotoStack, photo_id: str) -> list[Any]:
    return [
        row["process_context"]
        for row in stack.fetch(
            "SELECT process_context::text AS process_context FROM fleet.episode_photo"
            " WHERE photo_id = $1",
            uuid.UUID(photo_id),
        )
    ]


def test_a_photo_with_the_block_stores_the_column_and_without_it_null(stack: PhotoStack) -> None:
    site = _site(stack)
    with_block = _photo(stack, site, block=True)
    response = stack.post(site, with_block)
    assert response.status_code == 200, response.text
    (stored,) = _column(stack, with_block["photo_id"])
    assert json.loads(stored) == with_block["process_context"]
    without = _photo(stack, site, block=False)
    response = stack.post(site, without)
    assert response.status_code == 200, response.text
    assert _column(stack, without["photo_id"]) == [None]


def test_a_photo_whose_block_does_not_match_leaves_no_row(stack: PhotoStack) -> None:
    site = _site(stack)
    document = _photo(stack, site, block=True)
    del document["process_context"]["variables"][0]
    response = stack.post(site, document)
    assert _rejection(response) == ("schema_invalid", "process_context.variables"), response.text
    assert _column(stack, document["photo_id"]) == []


def test_a_productive_photo_stores_its_block_and_a_mismatch_leaves_no_row(
    stack: PhotoStack,
) -> None:
    site = _site(stack)
    document = _photo(stack, site, block=True)
    document["zone_mode"] = "productive"
    document["record_id"] = str(uuid7(stack.ingest.authz.sessions.clock))
    api.parse_episode_photo_submission(json.dumps(document).encode())
    response = stack.post(site, document)
    assert response.status_code == 200, response.text
    (stored,) = _column(stack, document["photo_id"])
    assert json.loads(stored) == document["process_context"]
    mismatch = _photo(stack, site, block=True)
    mismatch["zone_mode"] = "productive"
    mismatch["record_id"] = str(uuid7(stack.ingest.authz.sessions.clock))
    del mismatch["process_context"]["variables"][0]
    response = stack.post(site, mismatch)
    assert _rejection(response) == ("schema_invalid", "process_context.variables"), response.text
    assert _column(stack, mismatch["photo_id"]) == []


def test_the_purge_removes_the_whole_row_with_its_block(stack: PhotoStack) -> None:
    site = _site(stack)
    document = _photo(stack, site, block=True)
    assert stack.post(site, document).status_code == 200
    (row,) = stack.fetch(
        "SELECT expires_at FROM fleet.episode_photo WHERE photo_id = $1",
        uuid.UUID(document["photo_id"]),
    )
    database = stack.ingest.primary.database
    purger = EpisodePhotoPurger(database=database, clock=SimulatedClock(row["expires_at"]))

    async def purge() -> Any:
        async with database.transaction(system(site.organization_id)) as transaction:
            return await purger.purge(transaction)

    report = stack.run(purge())
    # La zona ya está en ``productive`` (uso aprobado): la purga la cuenta como retirada.
    assert uuid.UUID(document["photo_id"]) in (*report.retired, *report.expired)
    assert _column(stack, document["photo_id"]) == []


# --- Migración gob_0046 ---------------------------------------------------------------------------

PREVIOUS_HEAD: Final = "gob_0045"
NOW: Final = dt.datetime(2026, 11, 23, 9, 30, tzinfo=dt.UTC)
_PREVIOUS_INSERT: Final = (
    "INSERT INTO fleet.episode_photo (organization_id, photo_id, plant_id, zone_id, node_id,"
    " episode_id, segment_index, classified_as, family, standard_id, standard_version,"
    " max_confidence, started_at, ended_at, received_at, expires_at, image_absent_reason,"
    " canonical_sha256, platform_record_id, zone_mode, record_id, photo_index, photo_role)"
    " VALUES ($1, $2, $3, $4, $5, $6, 0, 'finding', 'coexistence', $7, 1, 0.9, $8, $8, $8, $9,"
    " 'frame_unavailable', $10, $11, 'commissioning', NULL, 0, 'peak')"
)
"""El ``INSERT`` de la imagen anterior (``gob_0045``): sin ``process_context``."""


def _previous_row() -> list[Any]:
    photo_id = uuid.uuid4()
    return [
        uuid.uuid4(),
        photo_id,
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        NOW,
        NOW + 30 * DAY,
        photo_id.hex * 2,
        uuid.uuid4(),
    ]


def _alembic(migrated: MigratedDatabase, revision: str) -> None:
    result = run_alembic(
        migrated.endpoint,
        migrated.database,
        {
            APP_PASSWORD_VARIABLE: migrated.app_password,
            MIGRATE_PASSWORD_VARIABLE: migrated.migrate_password,
        },
        "upgrade",
        revision,
    )
    assert result.returncode == 0, result.stderr


async def _admin(endpoint: PostgresEndpoint, statement: str) -> None:
    connection = await asyncpg.connect(
        host=endpoint.host,
        port=endpoint.port,
        user=endpoint.user,
        password=endpoint.password,
        database=endpoint.database,
    )
    try:
        await connection.execute(statement)
    finally:
        await connection.close()


async def _rows(migrated: MigratedDatabase) -> dict[uuid.UUID, dict[str, Any]]:
    connection = await migrated.connect()
    try:
        found = await connection.fetch("SELECT * FROM fleet.episode_photo")
    finally:
        await connection.close()
    return {uuid.UUID(str(row["photo_id"])): dict(row) for row in found}


async def _insert(migrated: MigratedDatabase, row: list[Any]) -> None:
    connection = await migrated.connect()
    try:
        await connection.execute(_PREVIOUS_INSERT, *row)
    finally:
        await connection.close()


async def _set_block(migrated: MigratedDatabase, photo_id: uuid.UUID, block: str) -> str:
    """``ok`` o el nombre de la restricción que rechaza guardar ``block`` en la fila."""
    connection = await migrated.connect()
    try:
        await connection.execute(
            "UPDATE fleet.episode_photo SET process_context = CAST($2 AS jsonb)"
            " WHERE photo_id = $1",
            photo_id,
            block,
        )
    except asyncpg.CheckViolationError as error:
        return str(error.constraint_name)
    finally:
        await connection.close()
    return "ok"


def test_migration_gob_0046_adds_a_null_column_and_keeps_the_previous_image_working(
    postgres_endpoint: PostgresEndpoint,
) -> None:
    name = f"vigia_gob_0046_{secrets.token_hex(4)}"
    asyncio.run(_admin(postgres_endpoint, f"CREATE DATABASE {name}"))
    migrated = MigratedDatabase(
        postgres_endpoint, name, secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    )
    try:
        _alembic(migrated, PREVIOUS_HEAD)
        for _ in range(3):
            asyncio.run(_insert(migrated, _previous_row()))
        before = asyncio.run(_rows(migrated))
        _alembic(migrated, "head")
        after = asyncio.run(_rows(migrated))
        assert set(after) == set(before) and len(after) == 3
        for photo_id, old in before.items():
            new = after[photo_id]
            assert new["process_context"] is None
            assert {key: value for key, value in new.items() if key != "process_context"} == old
        later = _previous_row()
        asyncio.run(_insert(migrated, later))
        assert asyncio.run(_rows(migrated))[later[1]]["process_context"] is None
        # Con bloque, siempre un objeto JSON (``episode_photo_process_context_object``).
        constraint = "episode_photo_process_context_object"
        assert asyncio.run(_set_block(migrated, later[1], '{"variables": []}')) == "ok"
        assert asyncio.run(_set_block(migrated, later[1], "[]")) == constraint
        assert asyncio.run(_set_block(migrated, later[1], '"texto"')) == constraint
    finally:
        asyncio.run(_admin(postgres_endpoint, f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
