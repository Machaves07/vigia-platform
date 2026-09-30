"""PR-NUC-13 con el escritor: máquina de estados sobre varias cadenas y organizaciones (TASK-113).

Contra PostgreSQL 16 real, como ``vigia_app``, con ``EscritorExpediente`` y ``AuditWriter``
completos (PBT-06). Cada ejemplo arranca con dos organizaciones de dos plantas cada una y aplica
una secuencia generada de comandos:

- escrituras de cadena de planta (``zone_created`` de U-02) y de cadena de organización
  (``organization_created``);
- hallazgos ``finding_received`` de U-03 cuyo contenido sale de los **generadores del kit de U-01**
  (``vigia_contracts.conformance.generators.finding``), con sus clips verificados;
- **escrituras concurrentes entrelazadas**: de dos a seis escrituras a la vez sobre cadenas
  mezcladas (la misma planta, otra planta, otra organización);
- reenvíos de un hallazgo ya aceptado (``accepted_duplicate`` con el recibo original, sin
  registro);
- entradas de auditoría por ``AuditWriter``, también concurrentes, y eventos sin organización a la
  cadena de la organización proveedora.

Al final de cada ejemplo, el oráculo relee cada cadena: secuencias contiguas ``1..n``,
``received_at`` (u ``occurred_at``) no decreciente, ``content_hash`` y ``record_hash`` recalculados
en Python desde las columnas persistidas y el hash anterior, cabeza coincidente, y tantas filas
por cadena como escrituras aceptadas el modelo.

La propiedad corre con cada semilla del perfil activo (``tests/conftest.py``): en ``ci`` la fija y
la aleatoria de la sesión; en ``nightly`` 2 000 ejemplos con la aleatoria.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from collections.abc import Iterator
from typing import Any

import pytest
from hypothesis import seed as hypothesis_seed
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    precondition,
    rule,
    run_state_machine_as_test,
)
from vigia_contracts.conformance.generators import finding, zone_catalog
from vigia_contracts.models.enumerations import AcceptanceStatus

from tests.conftest import _seeds_for_profile
from tests.identity_db import migrated_database
from tests.integration.conftest import PostgresEndpoint
from tests.writer_support import (
    FINDING_TYPE,
    ORGANIZATION_TYPE,
    ZONE_TYPE,
    Place,
    WriterEnvironment,
    clips_of,
    localize_finding,
    organization_document,
    unit_context,
    verify_audit_chain,
    verify_ledger_chains,
    writer_environment,
    zone_document,
)
from vigia_platform.ledger.application.audit_writer import AuditOperation, AuditOutcome
from vigia_platform.ledger.application.writer import LedgerRejection, Receipt
from vigia_platform.shared.context import ActorKind, ActorUnit, ScopeContext

pytestmark = pytest.mark.integration

STEPS_PER_EXAMPLE = 10
ORGANIZATIONS = 2
PLANTS_PER_ORGANIZATION = 2

_NAMES = st.sampled_from(["Zona de prensas", "Almacén Ñ", "Línea 2 — troquel", "Zona 😀 norte"])
_AUDIT_OPERATIONS = st.sampled_from(
    [AuditOperation.LEDGER_READ, AuditOperation.ZONE_CREATED, AuditOperation.AUTHORIZATION_DENIED]
)

Job = tuple[ScopeContext, str, dict[str, Any], Any]
"""(contexto, tipo, contenido, clave de la cadena en el modelo)."""


@pytest.fixture(scope="module")
def environment(postgres_endpoint: PostgresEndpoint) -> Iterator[WriterEnvironment]:
    with (
        migrated_database(postgres_endpoint, "writer_chain_stateful") as migrated,
        writer_environment(migrated, pool_size=8) as environment,
    ):
        yield environment


class LedgerChainMachine(RuleBasedStateMachine):
    """Modelo: cuántas escrituras aceptó cada cadena (organización, planta o ``None``)."""

    environment: WriterEnvironment

    def __init__(self) -> None:
        super().__init__()
        self.places: list[Place] = []
        self.organizations: list[uuid.UUID] = []
        self.ledger: Counter[tuple[uuid.UUID, uuid.UUID | None]] = Counter()
        self.audit: Counter[uuid.UUID] = Counter()
        self.findings: list[tuple[ScopeContext, dict[str, Any], Receipt]] = []
        self.catalog: dict[str, Any] = {}

    # --- preparación ----------------------------------------------------------------------

    @initialize(data=st.data())
    def organizations_and_plants(self, data: st.DataObject) -> None:
        for _ in range(ORGANIZATIONS):
            organization = uuid.uuid4()
            self.organizations.append(organization)
            self.places.extend(Place.new(organization) for _ in range(PLANTS_PER_ORGANIZATION))
        self.catalog = data.draw(zone_catalog())

    def _run(self, awaitable: Any) -> Any:
        return self.environment.loop.run(awaitable)

    # --- trabajos ---------------------------------------------------------------------------

    def _zone_job(self, place: Place, name: str) -> Job:
        context = unit_context(place.organization_id, ActorUnit.U02)
        return (
            context,
            ZONE_TYPE,
            zone_document(place, name),
            (place.organization_id, place.plant_id),
        )

    def _organization_job(self, organization: uuid.UUID) -> Job:
        context = unit_context(organization, ActorUnit.U02, kind=ActorKind.OPERATOR)
        return context, ORGANIZATION_TYPE, organization_document(organization), (organization, None)

    def _finding_job(self, place: Place, data: st.DataObject) -> Job:
        document = localize_finding(data.draw(finding(self.catalog)), place)
        for clip in clips_of(document):
            self.environment.storage.put(clip)
        context = unit_context(place.organization_id, ActorUnit.U03, kind=ActorKind.NODE)
        return context, FINDING_TYPE, document, (place.organization_id, place.plant_id)

    def _accept(self, job: Job, result: Receipt | LedgerRejection) -> None:
        assert isinstance(result, Receipt), result
        assert result.status is AcceptanceStatus.ACCEPTED
        self.ledger[job[3]] += 1
        if job[1] == FINDING_TYPE:
            self.findings.append((job[0], job[2], result))

    def _write(self, job: Job) -> None:
        context, record_type, document, _ = job
        self._accept(job, self._run(self.environment.writer.write(context, record_type, document)))

    # --- reglas ------------------------------------------------------------------------------

    @rule(index=st.integers(0, ORGANIZATIONS * PLANTS_PER_ORGANIZATION - 1), name=_NAMES)
    def write_plant_chain(self, index: int, name: str) -> None:
        self._write(self._zone_job(self.places[index], name))

    @rule(index=st.integers(0, ORGANIZATIONS - 1))
    def write_organization_chain(self, index: int) -> None:
        self._write(self._organization_job(self.organizations[index]))

    @rule(index=st.integers(0, ORGANIZATIONS * PLANTS_PER_ORGANIZATION - 1), data=st.data())
    def write_kit_finding(self, index: int, data: st.DataObject) -> None:
        self._write(self._finding_job(self.places[index], data))

    @rule(
        plan=st.lists(
            st.tuples(
                st.sampled_from(["zone", "organization", "finding"]),
                st.integers(0, ORGANIZATIONS * PLANTS_PER_ORGANIZATION - 1),
            ),
            min_size=2,
            max_size=6,
        ),
        data=st.data(),
    )
    def write_concurrently(self, plan: list[tuple[str, int]], data: st.DataObject) -> None:
        jobs: list[Job] = []
        for kind, index in plan:
            place = self.places[index]
            if kind == "zone":
                jobs.append(self._zone_job(place, "Zona concurrente"))
            elif kind == "organization":
                jobs.append(self._organization_job(place.organization_id))
            else:
                jobs.append(self._finding_job(place, data))

        async def together() -> list[Receipt | LedgerRejection]:
            return list(
                await asyncio.gather(
                    *(self.environment.writer.write(c, t, d) for c, t, d, _ in jobs)
                )
            )

        for job, result in zip(jobs, self._run(together()), strict=True):
            self._accept(job, result)

    @precondition(lambda self: bool(self.findings))
    @rule(data=st.data())
    def resend_accepted_finding(self, data: st.DataObject) -> None:
        context, document, original = data.draw(st.sampled_from(self.findings))
        result = self._run(self.environment.writer.write(context, FINDING_TYPE, document))
        assert isinstance(result, Receipt), result
        assert result.status is AcceptanceStatus.ACCEPTED_DUPLICATE
        assert (result.record_id, result.received_at) == (original.record_id, original.received_at)

    @rule(
        indexes=st.lists(st.integers(0, ORGANIZATIONS - 1), min_size=1, max_size=4),
        operation=_AUDIT_OPERATIONS,
    )
    def append_audit(self, indexes: list[int], operation: AuditOperation) -> None:
        audit = self.environment.audit

        async def together() -> None:
            await asyncio.gather(
                *(
                    audit.append(
                        unit_context(self.organizations[i], ActorUnit.U02),
                        operation,
                        outcome=AuditOutcome.SUCCESS,
                        filters={"zone": "Z-01", "page": 1},
                        result_count=3,
                    )
                    for i in indexes
                )
            )

        self._run(together())
        for i in indexes:
            self.audit[self.organizations[i]] += 1

    @rule()
    def audit_event_without_organization(self) -> None:
        environment = self.environment
        provider = environment.provider_organization_id
        context = unit_context(provider, ActorUnit.U02, kind=ActorKind.SYSTEM)
        receipt = self._run(
            environment.audit.append_without_organization(
                context, AuditOperation.CONTEXT_ABSENT_ATTEMPT, outcome=AuditOutcome.DENIED
            )
        )
        assert receipt.chain_sequence >= 1
        self.audit[provider] += 1

    # --- comprobación ------------------------------------------------------------------------

    def teardown(self) -> None:
        migrated = self.environment.migrated
        for organization in self.organizations:
            lengths = self._run(verify_ledger_chains(migrated, organization))
            expected = {
                plant: count
                for (owner, plant), count in self.ledger.items()
                if owner == organization and count
            }
            assert lengths == expected
            assert self._run(verify_audit_chain(migrated, organization)) == self.audit[organization]
        provider = self.environment.provider_organization_id
        # La cadena de la proveedora es de todo el módulo: se comprueba entera, sin contarla.
        assert self._run(verify_audit_chain(migrated, provider)) >= self.audit[provider]


def test_every_chain_stays_contiguous_and_recomputable(environment: WriterEnvironment) -> None:
    """PR-NUC-13 (con estado): la propiedad corre con cada semilla del perfil activo."""

    def factory() -> LedgerChainMachine:
        machine = LedgerChainMachine()
        return machine

    LedgerChainMachine.environment = environment
    for value in _seeds_for_profile():
        seeded = hypothesis_seed(value)(factory)
        run_state_machine_as_test(seeded, settings=settings(stateful_step_count=STEPS_PER_EXAMPLE))
    # La transacción corta se cumple también bajo concurrencia (PAT-NUC-RES-08).
    assert environment.storage.calls_in_transaction == 0
    assert environment.storage.calls > 0
