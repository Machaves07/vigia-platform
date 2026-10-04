"""Política de hallazgos incerrables de la planta contra PostgreSQL 16 real (TASK-211, DE §2.10).

``PlantPolicyService`` real sobre la base migrada como ``vigia_app`` (``gates_support``):

- **Carga** (BR-GOB-21): ``plant_policy_signed`` (``source_key = policy_id``) y su fila en una
  transacción, con el documento firmado verificado (``kind = plant_policy``) y pasado a ``used``.
- **Versión monótona por planta**: la fija el servidor como la anterior más uno; un ``version``
  desfasado es ``PolicyVersionConflict`` (``conflict``) sin escribir nada; una versión nueva no
  borra la anterior y la vigente es la última.
- **Lectura**: ``loaded = false`` en una planta sin política; bajo concesión, auditada.
- **Concurrencia**: dos cargas simultáneas con la misma versión dejan una sola; la otra recibe
  ``conflict``. Las dos se retienen justo después de leer la última versión: con el candado de
  la planta la segunda nunca llega a leer antes de que la primera confirme; sin él, las dos leen
  «sin política», la restricción única rechaza una como carrera (transitorio) y la prueba falla.
- **Guardas**: documento de otro tipo, sin subir, ya usado o de otra planta; textos fuera de la
  política o sin contenido; planta de otra organización, fuera de la concesión de planta,
  inexistente o leída por quien no tiene la planta en su alcance → ``ResourceNotFound``.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import pytest

from tests.gates_support import POLL_SECONDS, WAIT_SECONDS, GatesWorld, gates_world
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.catalog.adapters.postgres.plant_policy_repository import (
    PostgresPlantPolicyRepository,
)
from vigia_platform.catalog.application.admission import CatalogRejected
from vigia_platform.catalog.application.plant_policy import (
    PolicyRequestInvalid,
    PolicyUnavailable,
    PolicyVersionConflict,
)
from vigia_platform.catalog.detail_codes import CatalogDetailCode
from vigia_platform.catalog.domain.documents import DocumentRequestInvalid
from vigia_platform.catalog.domain.enums import DocumentKind
from vigia_platform.catalog.domain.plant_policy import PlantPolicy, PlantPolicyRequest
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.shared.context import Role, ScopeContext, ScopeLevel
from vigia_platform.shared.db import Transaction

pytestmark = pytest.mark.integration

SIGNER = "Gerencia General de la planta"
LEGAL = "Concepto jurídico CJ-2026-014"
CRITERIA = "Un hallazgo es incerrable cuando el remedio exige una obra mayor aprobada."


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[GatesWorld]:
    with gates_world(postgres_endpoint, "catalog_plant_policy") as world:
        yield world


class Plant:
    def __init__(self, world: GatesWorld) -> None:
        self.site = world.site()
        ((self.plant, self.zone),) = self.site.zones()
        self.installer = world.installer(self.site)


def _request(world: GatesWorld, p: Plant, version: int = 1, **changes: Any) -> PlantPolicyRequest:
    fields: dict[str, Any] = {
        "version": version,
        "signed_at": world.authz.now() - timedelta(days=2),
        "signed_by_display_name": SIGNER,
        "legal_opinion_reference": LEGAL,
        "criteria_summary_es": CRITERIA,
        "document_ref": world.document(p.installer, p.plant, DocumentKind.PLANT_POLICY),
    }
    fields.update(changes)
    return PlantPolicyRequest(**fields)


def _sign(world: GatesWorld, context: ScopeContext, plant: uuid.UUID, request: Any) -> PlantPolicy:
    world.advance()
    policy: PlantPolicy = world.run(world.policies.sign_policy(context, plant, request))
    return policy


def _rows(world: GatesWorld, plant: uuid.UUID) -> list[Any]:
    return world.fetch(
        "SELECT policy_id, version, signed_at, signed_by_display_name, legal_opinion_reference,"
        " criteria_summary_es, document_ref::text AS document_ref, loaded_by, ledger_record_id"
        " FROM catalog.plant_policy WHERE plant_id = $1 ORDER BY version",
        plant,
    )


def _records(world: GatesWorld, plant: uuid.UUID) -> list[Any]:
    return world.fetch(
        "SELECT record_id, source_key, actor_concession_id,"
        " ledger.vigia_bytes_to_jsonb(content) AS content FROM ledger.ledger_record"
        " WHERE scope_plant_id = $1 AND record_type = 'plant_policy_signed'"
        " ORDER BY chain_sequence",
        plant,
    )


def _written(world: GatesWorld, plant: uuid.UUID) -> tuple[int, int]:
    return len(_rows(world, plant)), len(_records(world, plant))


# --- Carga y versión -----------------------------------------------------------------------------
def test_the_first_policy_is_version_1_with_its_record_and_its_document_used(
    world: GatesWorld,
) -> None:
    p = Plant(world)
    request = _request(world, p)

    policy = _sign(world, p.installer, p.plant, request)

    assert policy.version == 1
    (row,) = _rows(world, p.plant)
    (record,) = _records(world, p.plant)
    assert row["policy_id"] == policy.policy_id and row["ledger_record_id"] == record["record_id"]
    assert row["loaded_by"] == p.installer.actor.id
    assert json.loads(row["document_ref"]) == request.document_ref
    assert record["source_key"] == str(policy.policy_id)
    assert record["actor_concession_id"] == p.installer.concession_id
    content = json.loads(record["content"])
    assert content["version"] == 1 and content["plant_id"] == str(p.plant)
    assert content["signed_by_display_name"] == SIGNER
    assert content["document_ref"] == request.document_ref
    assert world.grant_status(request.document_ref["document_id"]) == "used"
    # GET: la vigente, con el hash del documento.
    reader = world.member(p.site)
    loaded = world.run(world.policies.policy(reader, p.plant))
    assert loaded == replace(policy, ledger_record_id=record["record_id"])
    assert loaded.document_ref.sha256 == request.document_ref["sha256"]


def test_versions_grow_by_one_and_a_new_one_never_erases_the_previous(world: GatesWorld) -> None:
    p = Plant(world)
    first = _sign(world, p.installer, p.plant, _request(world, p, 1))
    second = _sign(world, p.installer, p.plant, _request(world, p, 2, criteria_summary_es="Otro"))

    assert [r["version"] for r in _rows(world, p.plant)] == [1, 2]
    assert [r["policy_id"] for r in _rows(world, p.plant)] == [first.policy_id, second.policy_id]
    current = world.run(world.policies.policy(world.member(p.site), p.plant))
    assert current is not None and current.version == 2
    assert current.criteria_summary_es == "Otro"


@pytest.mark.parametrize(("loaded", "version"), [(0, 2), (0, 7), (1, 1), (1, 3), (2, 1)], ids=str)
def test_a_stale_or_skipped_version_is_a_conflict_without_writing(
    world: GatesWorld, loaded: int, version: int
) -> None:
    p = Plant(world)
    for n in range(1, loaded + 1):
        _sign(world, p.installer, p.plant, _request(world, p, n))
    before = _written(world, p.plant)
    request = _request(world, p, version)

    with pytest.raises(PolicyVersionConflict):
        _sign(world, p.installer, p.plant, request)

    assert _written(world, p.plant) == before
    assert world.grant_status(request.document_ref["document_id"]) == "issued"


@pytest.mark.parametrize("version", [0, -1, 2**31, True, 1.0, "1"], ids=repr)
def test_a_version_outside_its_type_is_invalid(world: GatesWorld, version: Any) -> None:
    p = Plant(world)
    with pytest.raises(PolicyRequestInvalid):
        _sign(world, p.installer, p.plant, _request(world, p, version=version))
    assert _written(world, p.plant) == (0, 0)


def test_signed_at_without_time_zone_is_invalid(world: GatesWorld) -> None:
    p = Plant(world)
    naive = datetime.fromisoformat("2026-09-30T10:00:00")
    with pytest.raises(PolicyRequestInvalid):
        _sign(world, p.installer, p.plant, _request(world, p, signed_at=naive))
    assert _written(world, p.plant) == (0, 0)


def test_a_plant_without_policy_reads_loaded_false(world: GatesWorld) -> None:
    p = Plant(world)
    assert world.run(world.policies.policy(world.member(p.site), p.plant)) is None
    assert world.run(world.policies.policy(p.installer, p.plant)) is None
    # Bajo concesión, la lectura queda auditada en la cadena del cliente (A-56).
    audited = world.fetch(
        "SELECT scope_plant_id, result_count FROM shared.audit_entry WHERE organization_id = $1"
        " AND actor_concession_id = $2 AND operation = 'catalog_read'",
        p.site.organization_id,
        p.installer.concession_id,
    )
    assert [(r["scope_plant_id"], r["result_count"]) for r in audited] == [(p.plant, 1)]


# --- Guardas del documento y de los textos -------------------------------------------------------
@pytest.mark.parametrize("case", ["otro-tipo", "sin-subir", "ya-usado", "otra-planta", "forma"])
def test_a_document_that_does_not_match_its_grant_is_invalid(world: GatesWorld, case: str) -> None:
    p = Plant(world)
    if case == "otro-tipo":
        ref: Any = world.document(p.installer, p.plant, DocumentKind.SCOPE_RECORD)
    elif case == "sin-subir":
        ref = world.document(p.installer, p.plant, DocumentKind.PLANT_POLICY, uploaded=False)
    elif case == "ya-usado":
        first = _request(world, p)
        _sign(world, p.installer, p.plant, first)
        ref = first.document_ref
    elif case == "otra-planta":
        other = Plant(world)
        ref = world.document(other.installer, other.plant, DocumentKind.PLANT_POLICY)
    else:
        ref = {"document_id": str(uuid.uuid4())}
    before = _written(world, p.plant)
    version = before[0] + 1

    with pytest.raises(DocumentRequestInvalid):
        _sign(world, p.installer, p.plant, _request(world, p, version, document_ref=ref))

    assert _written(world, p.plant) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"signed_by_display_name": "n" * 121},
        {"legal_opinion_reference": "r" * 121},
        {"criteria_summary_es": "c" * 2001},
        {"criteria_summary_es": "Resumen <b>con</b> marcado"},
        {"criteria_summary_es": " . - _ , ; : "},
        {"signed_by_display_name": ""},
    ],
    ids=["firmante-121", "referencia-121", "resumen-2001", "marcado", "sin-contenido", "vacío"],
)
def test_texts_outside_the_policy_are_free_text_rejected(
    world: GatesWorld, changes: dict[str, Any]
) -> None:
    p = Plant(world)
    with pytest.raises(CatalogRejected) as raised:
        _sign(world, p.installer, p.plant, _request(world, p, **changes))
    assert raised.value.detail_code is CatalogDetailCode.FREE_TEXT_REJECTED
    assert _written(world, p.plant) == (0, 0)


def test_texts_at_their_edges_are_accepted(world: GatesWorld) -> None:
    p = Plant(world)
    policy = _sign(
        world,
        p.installer,
        p.plant,
        _request(
            world,
            p,
            signed_by_display_name="n" * 120,
            legal_opinion_reference="r" * 120,
            criteria_summary_es="c" * 2000,
        ),
    )
    assert (len(policy.signed_by_display_name), len(policy.criteria_summary_es)) == (120, 2000)


# --- Concurrencia --------------------------------------------------------------------------------
class GatedRepository(PostgresPlantPolicyRepository):
    """Retiene cada carga justo después de leer la última versión, hasta que la prueba suelta."""

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.readers = 0
        self.gate = asyncio.Event()

    async def latest(self, transaction: Transaction, plant_id: uuid.UUID) -> PlantPolicy | None:
        found = await super().latest(transaction, plant_id)
        self.readers += 1
        await self.gate.wait()
        return found


async def _advisory_waiters(admin: Any) -> int:
    value: int = await admin.fetchval(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
    )
    return value


@pytest.mark.parametrize("attempt", [1, 2, 3])
def test_two_simultaneous_loads_of_the_same_version_leave_one(
    world: GatesWorld, attempt: int
) -> None:
    p = Plant(world)
    repository = GatedRepository(world.database)
    service = world.build_policies(repository=repository)
    requests = [_request(world, p, 1), _request(world, p, 1)]
    admin = world.authz.sessions.admin
    world.advance()

    async def race() -> list[Any]:
        tasks = [
            asyncio.create_task(service.sign_policy(p.installer, p.plant, request))
            for request in requests
        ]
        # Con el candado, la segunda espera en pg_locks mientras la primera está retenida; sin
        # él, las dos leen «sin política» y quedan retenidas a la vez.
        async with asyncio.timeout(WAIT_SECONDS):
            while not (repository.readers >= 2 or await _advisory_waiters(admin) >= 1):
                await asyncio.sleep(POLL_SECONDS)
        repository.gate.set()
        return list(await asyncio.gather(*tasks, return_exceptions=True))

    results = world.run(race())

    loaded = [r for r in results if isinstance(r, PlantPolicy)]
    conflicts = [r for r in results if isinstance(r, PolicyVersionConflict)]
    assert (len(loaded), len(conflicts)) == (1, 1), results
    assert not any(isinstance(r, PolicyUnavailable) for r in results)
    assert [r["version"] for r in _rows(world, p.plant)] == [1]
    assert len(_records(world, p.plant)) == 1


# --- Guardas de alcance --------------------------------------------------------------------------
def test_another_organization_another_plant_or_out_of_scope_answer_not_found(
    world: GatesWorld,
) -> None:
    site = world.site(plants=2, zones=1)
    (plant_a, zone_a), (plant_b, _) = site.zones()
    organization = world.installer(site)
    plant_installer = world.installer(site, ScopeLevel.PLANT, plant_a)
    foreign = world.installer(world.site())
    zone_reader = world.member(site, Role.COORDINATOR_SST, ScopeLevel.ZONE, zone_a)
    plant_b_reader = world.member(site, Role.COORDINATOR_SST, ScopeLevel.PLANT, plant_b)
    p_a = Plant.__new__(Plant)
    p_a.site, p_a.plant, p_a.installer = site, plant_a, organization
    request_b = _request(world, replace_plant(p_a, plant_b))

    for context, plant in (
        (foreign, plant_a),  # otra organización
        (plant_installer, plant_b),  # otra planta, fuera de la concesión de planta
        (organization, uuid.uuid4()),  # inexistente
    ):
        with pytest.raises(ResourceNotFound):
            _sign(world, context, plant, request_b)
        with pytest.raises(ResourceNotFound):
            world.run(world.policies.policy(context, plant))
    for reader in (zone_reader, plant_b_reader):  # la planta A no está en su alcance
        with pytest.raises(ResourceNotFound):
            world.run(world.policies.policy(reader, plant_a))
    with pytest.raises(ResourceNotFound):  # leer no da permiso de cargar
        _sign(world, world.member(site, Role.ADMINISTRATOR), plant_a, _request(world, p_a))
    assert _written(world, plant_a) == (0, 0) and _written(world, plant_b) == (0, 0)
    assert world.grant_status(request_b.document_ref["document_id"]) == "issued"
    # El instalador de la planta A sí carga en su planta.
    assert _sign(world, plant_installer, plant_a, _request(world, p_a)).version == 1


def replace_plant(p: Plant, plant: uuid.UUID) -> Plant:
    other = Plant.__new__(Plant)
    other.site, other.plant, other.installer = p.site, plant, p.installer
    return other
