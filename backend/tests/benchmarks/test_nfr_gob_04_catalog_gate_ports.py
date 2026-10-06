"""Banco de NFR-GOB-04: los puertos de lectura del catálogo y las compuertas (TASK-213; LC-GOB-23a).

Sobre PostgreSQL 16 en contenedor, como ``vigia_app`` (con la seguridad a nivel de fila), con **un
año** de datos generados de la organización mayor de NFR-GOB-11: 20 plantas, 60 zonas y
32 estándares por zona. Por zona: 52 versiones del catálogo (una por semana, con un
``ZoneCatalog`` de 32 estándares y 4 cámaras y su sobre), cada versión nueva del catálogo
reversiona un estándar (83 versiones de estándar por zona), 12 intervalos por compuerta
(aprobación y revocación mensuales), 12 acuerdos de uso con tres confirmaciones (uno vigente) y su
proyección y su regresión; por planta, tres versiones de la política.

**10 000 llamadas consecutivas por operación** (NFR-GOB-64), recorriendo las zonas y los instantes
del año, con el marco de ``tests/benchmarks/conftest.py`` (mediana y p95 de cada operación en el
informe, frente a ``baseline.json``). Objetivos `[objetivo propio]` de NFR-GOB-04: p95 ≤ 5 ms en
las operaciones puntuales y ≤ 50 ms en las de lote y rango (``single_occupancy_many`` con 50 zonas,
``standards_at_many`` con 200 referencias, ``gate_history`` de 366 días) y en las que devuelven una
lista (``catalog_history``, ``states_by_plant``). El umbral (regresión frente a la base) se aplica
en ``nightly``: solo corre con ``--hypothesis-profile=nightly``. Solo datos generados.
"""

from __future__ import annotations

import json
import random
import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import pytest

from tests.benchmarks.conftest import Measure
from tests.catalog_ports_support import PortsWorld, ports_world
from tests.identity_db import BASE_TIME
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.catalog.domain.enums import GateKind
from vigia_platform.catalog.domain.ports import StandardRef
from vigia_platform.shared.context import ScopeContext

pytestmark = [pytest.mark.nightly, pytest.mark.integration]

SEED: Final = 20261005
PLANTS: Final = 20
ZONES_PER_PLANT: Final = 3
STANDARDS: Final = 32
VERSIONS: Final = 52
GATE_INTERVALS: Final = 12
AGREEMENTS: Final = 12
POLICIES: Final = 3
CALLS: Final = 10_000
WEEK: Final = timedelta(days=7)
MONTH: Final = timedelta(days=30)
YEAR: Final = timedelta(days=365)
POINT_MS: Final = 5.0
BATCH_MS: Final = 50.0
REASON: Final = "Motivo sintético del cambio del catálogo"


@dataclass(frozen=True)
class SeededZone:
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    standards: tuple[uuid.UUID, ...]


@dataclass
class Bench:
    world: PortsWorld
    context: ScopeContext
    plants: list[uuid.UUID]
    zones: list[SeededZone]
    origin: datetime
    rng: random.Random

    def instant(self) -> datetime:
        """Un instante del año sembrado (después del primer catálogo de toda zona)."""
        return self.origin + timedelta(seconds=self.rng.randrange(60, int(YEAR.total_seconds())))


def _catalog_payload(zone: uuid.UUID, version: int, standards: tuple[uuid.UUID, ...]) -> str:
    """Un ``ZoneCatalog`` del tamaño real: 32 estándares, 4 cámaras, señales y parámetros."""
    payload = {
        "version": version,
        "zone_id": str(zone),
        "cameras": [{"camera_id": str(uuid.UUID(int=i + 1)), "code": f"CAM-{i}"} for i in range(4)],
        "minimum_coverage": {"required_count": 1, "required_camera_ids": []},
        "signals": [{"signal_id": f"s{i}", "role": "energy"} for i in range(4)],
        "thresholds": {"review": 0.5, "publication": 0.8},
        "clip_window": {"pre_ms": 5000, "post_ms": 5000},
        "episode": {"grouping_window_ms": 3000},
        "standards": [
            {
                "standard_id": str(standard),
                "version": 1 + version // STANDARDS,
                "family": "coexistence",
                "title_es": f"Estándar sintético {index}",
                "declared_text": "Texto declarado sintético del estándar de la zona. " * 4,
                "predicate": {"all_of": [{"signal": "presence"}, {"signal": "energy"}]},
            }
            for index, standard in enumerate(standards)
        ],
    }
    return json.dumps(payload)


def _seed(
    world: PortsWorld, rng: random.Random
) -> tuple[uuid.UUID, list[uuid.UUID], list[SeededZone]]:
    site = world.site(plants=PLANTS, zones=ZONES_PER_PLANT)
    organization = site.organization_id
    user = world.user_id
    signers = [(role, world.authz.add_user(organization))
               for role in ("coordinator_sst", "plant_manager", "copasst")]  # fmt: skip
    signatories = json.dumps(
        [{"role": r, "user_id": str(u), "display_name": f"Firmante {r}"} for r, u in signers]
    )
    document = json.dumps({"document_id": str(uuid.uuid4()), "storage_key": "documents/x",
                           "sha256": "a" * 64, "content_type": "application/pdf",
                           "size_bytes": 1024})  # fmt: skip
    origin = BASE_TIME - YEAR
    versions: list[tuple[Any, ...]] = []
    standards: list[tuple[Any, ...]] = []
    intervals: list[tuple[Any, ...]] = []
    projections: list[tuple[Any, ...]] = []
    regressions: list[tuple[Any, ...]] = []
    agreements: list[tuple[Any, ...]] = []
    confirmations: list[tuple[Any, ...]] = []
    policies: list[tuple[Any, ...]] = []
    seeded: list[SeededZone] = []
    for plant, zone in site.zones():
        ids = tuple(uuid.uuid4() for _ in range(STANDARDS))
        seeded.append(SeededZone(plant, zone, ids))
        current = dict.fromkeys(ids, 1)
        spans: dict[tuple[uuid.UUID, int], list[Any]] = {}
        for number in range(1, VERSIONS + 1):
            issued = origin + (number - 1) * WEEK
            until = None if number == VERSIONS else issued + WEEK
            envelope = json.dumps({"payload": json.loads(_catalog_payload(zone, number, ids)),
                                   "signature": "s" * 88, "key_id": "k1"})  # fmt: skip
            versions.append((organization, plant, zone, number, issued, user,
                             f"{REASON} {number}", ["standards"],
                             _catalog_payload(zone, number, ids), envelope,
                             number % 5 == 0, 15 + number % 400, uuid.uuid4(), until))  # fmt: skip
            if number == 1:
                for s in ids:
                    spans[s, 1] = [organization, plant, zone, s, 1, issued, 1, None]
                continue
            # Cada versión nueva del catálogo reversiona un estándar y retira la anterior.
            standard = ids[(number - 2) % STANDARDS]
            previous = current[standard]
            spans[standard, previous][7] = number
            spans[standard, previous + 1] = [
                organization, plant, zone, standard, previous + 1, issued, number, None
            ]  # fmt: skip
            current[standard] = previous + 1
        standards.extend(tuple(row) for row in spans.values())
        for gate in GateKind:
            start = origin + timedelta(hours=rng.randrange(1, 48))
            for index in range(GATE_INTERVALS):
                status = "approved" if index % 2 == 0 else "revoked"
                end = None if index == GATE_INTERVALS - 1 else start + MONTH
                intervals.append((organization, plant, zone, gate.value, status, start, end, user,
                                  REASON if status == "revoked" else None, uuid.uuid4(),
                                  uuid.uuid4()))  # fmt: skip
                if end is not None:
                    start = end
        decided = json.dumps({"status": "revoked", "decided_at": BASE_TIME.isoformat(),
                              "record_id": str(uuid.uuid4()), "decided_by": str(user)})  # fmt: skip
        projections.append((zone, organization, plant, decided, decided, "no_capture", BASE_TIME))
        regressions.append((zone, organization, plant, BASE_TIME - MONTH, "catalog_change",
                            uuid.uuid4()))  # fmt: skip
        previous_agreement: uuid.UUID | None = None
        for index in range(AGREEMENTS):
            agreement = uuid.uuid4()
            approved = origin + index * MONTH + timedelta(days=1)
            last = index == AGREEMENTS - 1
            superseded = None if last else approved + MONTH
            agreements.append((agreement, organization, plant, zone,
                               "approved" if last else "superseded", signatories, document,
                               previous_agreement, user, approved, approved, user, uuid.uuid4(),
                               superseded))  # fmt: skip
            confirmed = approved - timedelta(hours=1)
            confirmations.extend(
                (agreement, u, organization, plant, r, confirmed) for r, u in signers
            )
            previous_agreement = agreement
    for plant in site.plants:
        for version in range(1, POLICIES + 1):
            policies.append((uuid.uuid4(), organization, plant, version, origin + version * MONTH,
                             document, user, uuid.uuid4()))  # fmt: skip

    async def load() -> None:
        admin = world.authz.sessions.admin
        await admin.executemany(
            "INSERT INTO catalog.zone_catalog_version (organization_id, plant_id, zone_id,"
            " catalog_version, issued_at, issued_by, role_in_use, reason_es, changed_fields,"
            " payload, envelope, single_occupancy, aggregation_window_minutes, ledger_record_id,"
            " superseded_at) VALUES ($1, $2, $3, $4, $5, $6, 'administrator', $7, $8, $9, $10,"
            " $11, $12, $13, $14)",
            versions,
        )
        await admin.executemany(
            "INSERT INTO catalog.declared_standard_version (organization_id, plant_id, zone_id,"
            " standard_id, version, family, title_es, declared_text, declared_by, effective_from,"
            " predicate, catalog_version, retired_in_catalog_version, reason_es)"
            " VALUES ($1, $2, $3, $4, $5, 'coexistence', 'Estándar sintético',"
            ' \'Texto declarado sintético\', \'{"user_id": "00000000-0000-4000-8000-000000000001",'
            ' "display_name": "Firmante", "role": "administrator"}\', $6, \'{}\', $7, $8,'
            " 'Motivo sintético del estándar')",
            standards,
        )
        await admin.executemany(
            "INSERT INTO catalog.gate_state_history (organization_id, plant_id, zone_id, gate,"
            " status, effective_from, effective_until, decided_by, reason_es, ledger_record_id,"
            " record_id) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
            intervals,
        )
        await admin.executemany(
            "INSERT INTO catalog.zone_gate_state (zone_id, organization_id, plant_id, mounting,"
            " usage, resulting_mode, issued_at, envelope, valid_until)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, '{}', $7::timestamptz + interval '7 days')",
            projections,
        )
        await admin.executemany(
            "INSERT INTO catalog.walk_test_regression (zone_id, organization_id, plant_id, state,"
            " marked_at, cause, catalog_version, affected_row_ids, ledger_record_id)"
            " VALUES ($1, $2, $3, 'pending', $4, $5, 52, '\"all\"', $6)",
            regressions,
        )
        await admin.executemany(
            "INSERT INTO catalog.use_agreement (agreement_id, organization_id, plant_id, zone_id,"
            " status, signatories, document_ref, replaces_agreement_id, created_by, created_at,"
            " approved_at, approved_by, ledger_record_id, superseded_at)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)",
            agreements,
        )
        await admin.executemany(
            "INSERT INTO catalog.agreement_confirmation (agreement_id, user_id, organization_id,"
            " plant_id, role_in_use, confirmed_at, origin)"
            " VALUES ($1, $2, $3, $4, $5, $6, 'management')",
            confirmations,
        )
        await admin.executemany(
            "INSERT INTO catalog.plant_policy (policy_id, organization_id, plant_id, version,"
            " signed_at, signed_by_display_name, legal_opinion_reference, document_ref,"
            " criteria_summary_es, loaded_by, loaded_at, ledger_record_id)"
            " VALUES ($1, $2, $3, $4, $5, 'Firmante sintético', 'REF-SINTETICA', $6,"
            " 'Resumen sintético de criterios', $7, $5, $8)",
            policies,
        )
        await admin.execute("ANALYZE")

    world.run(load())
    return organization, list(site.plants), seeded


@pytest.fixture(scope="module")
def bench(postgres_endpoint: PostgresEndpoint) -> Iterator[Bench]:
    with ports_world(postgres_endpoint, "nfr_gob_04_ports") as world:
        rng = random.Random(SEED)  # noqa: S311 - datos sintéticos del banco, no criptografía
        organization, plants, zones = _seed(world, rng)
        assert len(plants) == PLANTS and len(zones) == PLANTS * ZONES_PER_PLANT
        yield Bench(world, world.context(organization), plants, zones,
                    BASE_TIME - YEAR, rng)  # fmt: skip


Call = Callable[[Bench], Awaitable[Any]]


def _zone(bench: Bench) -> SeededZone:
    return bench.rng.choice(bench.zones)


def _ref(bench: Bench) -> StandardRef:
    zone = _zone(bench)
    return StandardRef(zone.zone_id, bench.rng.choice(zone.standards), bench.instant())


def _current_catalog(b: Bench) -> Awaitable[Any]:
    return b.world.ports.catalog.current_catalog(b.context, _zone(b).zone_id)


def _catalog_at(b: Bench) -> Awaitable[Any]:
    return b.world.ports.catalog.catalog_at(b.context, _zone(b).zone_id, b.instant())


def _standard_version(b: Bench) -> Awaitable[Any]:
    zone = _zone(b)
    return b.world.ports.catalog.standard_version(
        b.context, zone.zone_id, b.rng.choice(zone.standards), 1
    )


def _standard_at(b: Bench) -> Awaitable[Any]:
    ref = _ref(b)
    return b.world.ports.catalog.standard_at(b.context, ref.zone_id, ref.standard_id, ref.at)


def _catalog_history(b: Bench) -> Awaitable[Any]:
    return b.world.ports.catalog.catalog_history(b.context, _zone(b).zone_id)


def _single_occupancy(b: Bench) -> Awaitable[Any]:
    return b.world.ports.catalog.single_occupancy(b.context, _zone(b).zone_id, b.instant())


def _single_occupancy_many(b: Bench) -> Awaitable[Any]:
    zones = [zone.zone_id for zone in b.rng.sample(b.zones, 50)]
    return b.world.ports.catalog.single_occupancy_many(b.context, zones, b.instant())


def _standards_at_many(b: Bench) -> Awaitable[Any]:
    return b.world.ports.catalog.standards_at_many(b.context, [_ref(b) for _ in range(200)])


def _regression_state(b: Bench) -> Awaitable[Any]:
    return b.world.ports.catalog.regression_state(b.context, _zone(b).zone_id)


def _state(b: Bench) -> Awaitable[Any]:
    return b.world.ports.gates.state(b.context, _zone(b).zone_id)


def _states_by_plant(b: Bench) -> Awaitable[Any]:
    return b.world.ports.gates.states_by_plant(b.context, b.rng.choice(b.plants))


def _state_at(b: Bench) -> Awaitable[Any]:
    return b.world.ports.gates.state_at(b.context, _zone(b).zone_id, GateKind.USAGE, b.instant())


def _gate_history(b: Bench) -> Awaitable[Any]:
    start = b.instant() - YEAR / 2
    return b.world.ports.gates.gate_history(
        b.context, _zone(b).zone_id, start, start + timedelta(days=366)
    )


def _plant_policy(b: Bench) -> Awaitable[Any]:
    return b.world.ports.gates.plant_policy(b.context, b.rng.choice(b.plants))


def _current_agreement(b: Bench) -> Awaitable[Any]:
    return b.world.ports.gates.current_agreement(b.context, _zone(b).zone_id)


OPERATIONS: Final[dict[str, tuple[str, float, Call]]] = {
    "current_catalog": ("Catálogo vigente de una zona (32 estándares)", POINT_MS, _current_catalog),
    "catalog_at": ("Catálogo vigente en un instante del año", POINT_MS, _catalog_at),
    "standard_version": ("Versión 1 de un estándar de la zona", POINT_MS, _standard_version),
    "standard_at": ("Versión de un estándar vigente en un instante", POINT_MS, _standard_at),
    "catalog_history": ("Historia del catálogo (52 versiones, una página)", BATCH_MS,
                        _catalog_history),
    "single_occupancy": ("Marca unipersonal de una zona en un instante", POINT_MS,
                         _single_occupancy),
    "single_occupancy_many": ("Marca unipersonal de 50 zonas en un instante", BATCH_MS,
                              _single_occupancy_many),
    "standards_at_many": ("200 referencias de estándar en instantes del año", BATCH_MS,
                          _standards_at_many),
    "regression_state": ("Estado de regresión de una zona", POINT_MS, _regression_state),
    "state": ("Estado de compuertas de una zona", POINT_MS, _state),
    "states_by_plant": ("Compuertas de las zonas de una planta", BATCH_MS, _states_by_plant),
    "state_at": ("Compuerta de uso en un instante, desde la historia", POINT_MS, _state_at),
    "gate_history": ("Historia de las dos compuertas en 366 días", BATCH_MS, _gate_history),
    "plant_policy": ("Política vigente de una planta", POINT_MS, _plant_policy),
    "current_agreement": ("Acuerdo vigente de una zona con sus firmantes", POINT_MS,
                          _current_agreement),
}  # fmt: skip
"""Nombre → (etiqueta, objetivo p95 en ms, llamada con argumentos sorteados)."""


@pytest.mark.parametrize("name", list(OPERATIONS))
def test_nfr_gob_04_port_operation(bench: Bench, measure: Measure, name: str) -> None:
    label, objective, call = OPERATIONS[name]
    assert bench.world.run(call(bench)) is not None or name == "state_at"

    def target() -> object:
        # Sortear los argumentos (sin E/S) cuesta microsegundos frente a la lectura.
        return bench.world.run(call(bench))

    result = measure(
        f"gob_ports_{name}",
        f"{label} (NFR-GOB-04)",
        target,
        objective_ms=objective,
        rounds=CALLS,
        details={
            "calls": CALLS,
            "plants": PLANTS,
            "zones": PLANTS * ZONES_PER_PLANT,
            "standards_per_zone": STANDARDS,
            "catalog_versions_per_zone": VERSIONS,
        },
    )
    assert result.samples == CALLS
