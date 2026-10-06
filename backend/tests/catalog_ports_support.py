"""Entorno de las pruebas de ``CatalogQueryPort`` y ``GateQueryPort`` (TASK-213; LC-GOB-23a).

``ports_world`` levanta sobre una base migrada propia (``authz_environment``: ``shared.db`` como
``vigia_app``, con la seguridad a nivel de fila y el contador de sentencias por motor) los dos
puertos de ``catalog_query_ports`` y altas directas como superusuario de lo que leen: versiones del
catálogo encadenadas, versiones de estándares con su retiro, intervalos de compuertas, la
proyección, la regresión, la política de planta y acuerdos aprobados con sus confirmaciones.

``ZoneData`` y ``populate`` dejan una zona con todo lo que leen las quince operaciones;
``operations`` las devuelve por nombre sobre una zona (las de planta, sobre su planta) para las
pruebas de aislamiento, de alcance y de una sentencia por operación.

Solo datos generados (NFR-CTR-43). Las marcas parten de ``BASE_TIME``: ninguna se compara con la
hora de la base.
"""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from tests.authz_support import AuthzEnvironment, Site, authz_environment, sealed_context
from tests.identity_db import BASE_TIME
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.catalog.adapters.postgres.query_ports import (
    CatalogQueryPorts,
    catalog_query_ports,
)
from vigia_platform.catalog.domain.enums import GateKind
from vigia_platform.catalog.domain.ports import StandardRef
from vigia_platform.shared.context import AllowedScope, Role, ScopeContext, ScopeLevel

DAY: Final = timedelta(days=1)
HOUR: Final = timedelta(hours=1)
REASON: Final = "Motivo sintético del cambio"
SHA: Final = "a" * 64

Operation = Callable[[ScopeContext], Awaitable[Any]]

OPERATION_NAMES: Final = (
    "current_catalog",
    "catalog_at",
    "standard_version",
    "standard_at",
    "catalog_history",
    "single_occupancy",
    "single_occupancy_many",
    "standards_at_many",
    "regression_state",
    "state",
    "states_by_plant",
    "state_at",
    "gate_history",
    "plant_policy",
    "current_agreement",
)
"""Las nueve operaciones de ``CatalogQueryPort`` y las seis de ``GateQueryPort``."""


@dataclass(frozen=True)
class ZoneData:
    """Una zona poblada: catálogo 1 en ``T0`` y 2 en ``T0 + 1 día`` (que crea la versión 2 del
    estándar), compuertas aprobadas, regresión, política y acuerdo."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    standard_id: uuid.UUID
    t0: datetime

    @property
    def at(self) -> datetime:
        """Un instante en que rigen el catálogo 2 y las dos compuertas."""
        return self.t0 + DAY + HOUR


@dataclass
class PortsWorld:
    authz: AuthzEnvironment
    ports: CatalogQueryPorts

    # --- Utilidades ----------------------------------------------------------------------------

    def run(self, awaitable: Awaitable[Any]) -> Any:
        return self.authz.run(awaitable)

    def execute(self, sql: str, *args: Any) -> None:
        self.authz.execute(sql, *args)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.authz.fetch(sql, *args)

    @property
    def user_id(self) -> uuid.UUID:
        return self.authz.operator_id

    def site(self, plants: int = 1, zones: int = 1) -> Site:
        return self.authz.add_site(plants=plants, zones_per_plant=zones)

    @staticmethod
    def context(organization_id: uuid.UUID, *scopes: tuple[ScopeLevel, uuid.UUID]) -> ScopeContext:
        """Contexto de sesión con esos alcances; sin ninguno, el de la organización entera."""
        allowed = scopes or ((ScopeLevel.ORGANIZATION, organization_id),)
        return sealed_context(
            organization_id,
            [AllowedScope(level, scope_id, Role.COORDINATOR_SST) for level, scope_id in allowed],
        )

    # --- Altas ---------------------------------------------------------------------------------

    def add_version(
        self,
        organization_id: uuid.UUID,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        catalog_version: int,
        issued_at: datetime,
        *,
        superseded_at: datetime | None = None,
        single_occupancy: bool = False,
        window: int = 60,
        changed_fields: Sequence[str] = ("standards",),
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.execute(
            "INSERT INTO catalog.zone_catalog_version (organization_id, plant_id, zone_id,"
            " catalog_version, issued_at, issued_by, role_in_use, reason_es, changed_fields,"
            " payload, envelope, single_occupancy, aggregation_window_minutes, ledger_record_id,"
            " superseded_at) VALUES ($1, $2, $3, $4, $5, $6, 'administrator', $7, $8, $9,"
            " '{}', $10, $11, $12, $13)",
            organization_id,
            plant_id,
            zone_id,
            catalog_version,
            issued_at,
            self.user_id,
            f"{REASON} {catalog_version}",
            list(changed_fields),
            json.dumps(payload or {"version": catalog_version}),
            single_occupancy,
            window,
            uuid.uuid4(),
            superseded_at,
        )

    def add_standard(
        self,
        organization_id: uuid.UUID,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        standard_id: uuid.UUID,
        version: int,
        catalog_version: int,
        effective_from: datetime,
        *,
        retired_in: int | None = None,
    ) -> None:
        self.execute(
            "INSERT INTO catalog.declared_standard_version (organization_id, plant_id, zone_id,"
            " standard_id, version, family, title_es, declared_text, declared_by, effective_from,"
            " predicate, catalog_version, retired_in_catalog_version, reason_es)"
            " VALUES ($1, $2, $3, $4, $5, 'coexistence', $6, 'Texto declarado sintético', $7,"
            " $8, '{}', $9, $10, $11)",
            organization_id,
            plant_id,
            zone_id,
            standard_id,
            version,
            f"Estándar sintético v{version}",
            json.dumps(
                {"user_id": str(self.user_id), "display_name": "Firmante", "role": "administrator"}
            ),
            effective_from,
            catalog_version,
            retired_in,
            REASON,
        )

    def add_interval(
        self,
        organization_id: uuid.UUID,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        gate: GateKind,
        status: str,
        start: datetime,
        until: datetime | None = None,
    ) -> uuid.UUID | None:
        record_id = None if status == "pending" else uuid.uuid4()
        self.execute(
            "INSERT INTO catalog.gate_state_history (organization_id, plant_id, zone_id, gate,"
            " status, effective_from, effective_until, decided_by, reason_es, ledger_record_id,"
            " record_id) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
            organization_id,
            plant_id,
            zone_id,
            gate.value,
            status,
            start,
            until,
            self.user_id,
            REASON if status == "revoked" else None,
            uuid.uuid4(),
            record_id,
        )
        return record_id

    def set_projection(
        self,
        organization_id: uuid.UUID,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        mounting: dict[str, Any],
        usage: dict[str, Any],
        issued_at: datetime,
        mode: str = "commissioning",
    ) -> None:
        self.execute(
            "INSERT INTO catalog.zone_gate_state (zone_id, organization_id, plant_id, mounting,"
            " usage, resulting_mode, issued_at, envelope, valid_until)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, '{}', $7::timestamptz + interval '7 days')"
            " ON CONFLICT (zone_id) DO UPDATE SET mounting = EXCLUDED.mounting,"
            " usage = EXCLUDED.usage, resulting_mode = EXCLUDED.resulting_mode,"
            " issued_at = EXCLUDED.issued_at, valid_until = EXCLUDED.valid_until",
            zone_id,
            organization_id,
            plant_id,
            json.dumps(mounting),
            json.dumps(usage),
            mode,
            issued_at,
        )

    def set_regression(
        self,
        organization_id: uuid.UUID,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        cause: str,
        marked_at: datetime,
    ) -> None:
        self.execute(
            "INSERT INTO catalog.walk_test_regression (zone_id, organization_id, plant_id, state,"
            " marked_at, cause, affected_row_ids, ledger_record_id)"
            " VALUES ($1, $2, $3, 'pending', $4, $5, '\"all\"', $6)",
            zone_id,
            organization_id,
            plant_id,
            marked_at,
            cause,
            uuid.uuid4(),
        )

    def add_policy(
        self, organization_id: uuid.UUID, plant_id: uuid.UUID, version: int, signed_at: datetime
    ) -> uuid.UUID:
        policy_id = uuid.uuid4()
        self.execute(
            "INSERT INTO catalog.plant_policy (policy_id, organization_id, plant_id, version,"
            " signed_at, signed_by_display_name, legal_opinion_reference, document_ref,"
            " criteria_summary_es, loaded_by, loaded_at, ledger_record_id)"
            " VALUES ($1, $2, $3, $4, $5, 'Firmante sintético', 'REF-SINTETICA', $6,"
            " 'Resumen sintético de criterios', $7, $5, $8)",
            policy_id,
            organization_id,
            plant_id,
            version,
            signed_at,
            json.dumps(_document_ref()),
            self.user_id,
            uuid.uuid4(),
        )
        return policy_id

    def add_agreement(
        self,
        organization_id: uuid.UUID,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        approved_at: datetime,
        *,
        replaces: uuid.UUID | None = None,
        confirmed: int = 3,
    ) -> uuid.UUID:
        """Acuerdo aprobado con tres firmantes, de los que confirman los ``confirmed`` primeros."""
        agreement_id = uuid.uuid4()
        signers = [
            (role, self.authz.add_user(organization_id))
            for role in ("coordinator_sst", "plant_manager", "copasst")
        ]
        self.execute(
            "INSERT INTO catalog.use_agreement (agreement_id, organization_id, plant_id, zone_id,"
            " status, signatories, document_ref, replaces_agreement_id, created_by, created_at,"
            " approved_at, approved_by, ledger_record_id)"
            " VALUES ($1, $2, $3, $4, 'approved', $5, $6, $7, $8, $9, $9, $8, $10)",
            agreement_id,
            organization_id,
            plant_id,
            zone_id,
            json.dumps(
                [
                    {"role": role, "user_id": str(user), "display_name": f"Firmante {role}"}
                    for role, user in signers
                ]
            ),
            json.dumps(_document_ref()),
            replaces,
            self.user_id,
            approved_at,
            uuid.uuid4(),
        )
        for role, user in signers[:confirmed]:
            self.execute(
                "INSERT INTO catalog.agreement_confirmation (agreement_id, user_id,"
                " organization_id, plant_id, role_in_use, confirmed_at, origin)"
                " VALUES ($1, $2, $3, $4, $5, $6, 'management')",
                agreement_id,
                user,
                organization_id,
                plant_id,
                role,
                approved_at - HOUR,
            )
        return agreement_id

    def populate(
        self, organization_id: uuid.UUID, plant_id: uuid.UUID, zone_id: uuid.UUID
    ) -> ZoneData:
        """Todo lo que leen las quince operaciones sobre la zona."""
        t0 = BASE_TIME + timedelta(milliseconds=secrets.randbelow(10**6))
        data = ZoneData(organization_id, plant_id, zone_id, uuid.uuid4(), t0)
        keys = (organization_id, plant_id, zone_id)
        self.add_version(*keys, 1, t0, superseded_at=t0 + DAY)
        self.add_version(*keys, 2, t0 + DAY, single_occupancy=True, window=120)
        self.add_standard(*keys, data.standard_id, 1, 1, t0, retired_in=2)
        self.add_standard(*keys, data.standard_id, 2, 2, t0 + DAY)
        mounting = self.add_interval(*keys, GateKind.MOUNTING, "approved", t0)
        usage = self.add_interval(*keys, GateKind.USAGE, "approved", t0 + DAY)
        self.set_projection(
            *keys,
            {"status": "approved", "decided_at": t0.isoformat(), "record_id": str(mounting),
             "decided_by": str(self.user_id)},
            {"status": "approved", "decided_at": (t0 + DAY).isoformat(),
             "agreement_id": str(usage), "decided_by": str(self.user_id)},
            t0 + DAY,
            mode="productive",
        )  # fmt: skip
        self.set_regression(*keys, "framing_recaptured", t0 + DAY)
        if not self.fetch(
            "SELECT 1 FROM catalog.plant_policy WHERE plant_id = $1", plant_id
        ):  # una política por planta basta
            self.add_policy(organization_id, plant_id, 1, t0)
        self.add_agreement(*keys, t0 + DAY)
        return data


def _document_ref() -> dict[str, Any]:
    return {
        "document_id": str(uuid.uuid4()),
        "storage_key": f"documents/{uuid.uuid4()}",
        "sha256": SHA,
        "content_type": "application/pdf",
        "size_bytes": 1024,
    }


def operations(ports: CatalogQueryPorts, data: ZoneData) -> dict[str, Operation]:
    """Las quince operaciones sobre la zona de ``data`` (las de planta, sobre su planta)."""
    catalog, gates = ports.catalog, ports.gates
    zone, standard, at = data.zone_id, data.standard_id, data.at
    ref = StandardRef(zone, standard, at)
    found: dict[str, Operation] = {
        "current_catalog": lambda c: catalog.current_catalog(c, zone),
        "catalog_at": lambda c: catalog.catalog_at(c, zone, at),
        "standard_version": lambda c: catalog.standard_version(c, zone, standard, 2),
        "standard_at": lambda c: catalog.standard_at(c, zone, standard, at),
        "catalog_history": lambda c: catalog.catalog_history(c, zone),
        "single_occupancy": lambda c: catalog.single_occupancy(c, zone, at),
        "single_occupancy_many": lambda c: catalog.single_occupancy_many(c, [zone, zone], at),
        "standards_at_many": lambda c: catalog.standards_at_many(c, [ref, ref]),
        "regression_state": lambda c: catalog.regression_state(c, zone),
        "state": lambda c: gates.state(c, zone),
        "states_by_plant": lambda c: gates.states_by_plant(c, data.plant_id),
        "state_at": lambda c: gates.state_at(c, zone, GateKind.USAGE, at),
        "gate_history": lambda c: gates.gate_history(c, zone, data.t0, data.t0 + 30 * DAY),
        "plant_policy": lambda c: gates.plant_policy(c, data.plant_id),
        "current_agreement": lambda c: gates.current_agreement(c, zone),
    }
    assert tuple(found) == OPERATION_NAMES
    return found


def missing(data: ZoneData) -> ZoneData:
    """La misma forma con identificadores que no existen en ninguna organización."""
    return ZoneData(data.organization_id, uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), data.t0)


@contextmanager
def ports_world(endpoint: PostgresEndpoint, prefix: str) -> Iterator[PortsWorld]:
    """Los puertos sobre la base de ``authz_environment`` (la que cuenta sentencias)."""
    with authz_environment(endpoint, prefix) as authz:
        yield PortsWorld(authz, catalog_query_ports(authz.sessions.database))
