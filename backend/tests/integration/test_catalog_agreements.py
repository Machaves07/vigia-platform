"""Acuerdo de uso, firmantes y transparencia contra PostgreSQL 16 real (TASK-212, LC-GOB-04).

Servicios reales de ``catalog.agreements`` y ``catalog.gates`` sobre la base migrada como
``vigia_app`` (``agreements_support``):

- **Política de firmantes** (BR-GOB-25): fijar, releer, rastro ``signatory_policy_changed`` y sus
  guardas sin escribir nada.
- **Alta** (BR-GOB-26, 28, 31): cada guarda en su orden, G-8 y el documento opcional.
- **Confirmación** (BR-GOB-26, 27): origen y rol en uso, G-14, G-9 (``authorization_denied``), una
  sola fila por firmante, también con N confirmaciones simultáneas (falla sin la clave).
- **Aprobación** (BR-GOB-29, 30, 32): las 15 combinaciones de guardas que faltan sin escribir nada;
  una transacción con ``use_agreement_signed``, ``gate_state_changed``, sus eventos y la compuerta
  con ``effective_from`` igual a ``approved_at``; repetida sin efectos; sustitución sin ningún
  instante sin uso aprobado (PR-GOB-03); N aprobaciones simultáneas con un solo efecto (falla sin
  el candado) y una aprobación cruzada con la revocación sin interbloqueo.
- **Revocación**: el acuerdo vigente pasa a ``revoked`` y lo escrito permanece.
- **Guardas de alcance** (NFR-GOB-30): otra organización, otra planta, zona fuera del alcance o
  inexistente → ``ResourceNotFound``; las sentencias filtran la zona.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, Final

import pytest
from vigia_contracts.models.enumerations import GateStatus, ZoneMode

from tests.agreements_support import (
    POLICY_ROLES,
    SIGNER_ROLES,
    AgreementsWorld,
    Ready,
    agreements_world,
)
from tests.gates_support import POLL_SECONDS, REASON, WAIT_SECONDS
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.catalog.adapters.postgres.agreement_repository import (
    PostgresAgreementRepository,
)
from vigia_platform.catalog.application.admission import CatalogRejected
from vigia_platform.catalog.application.agreements import (
    AgreementApproval,
    AgreementConflict,
    AgreementRequestInvalid,
    ConfirmationResult,
)
from vigia_platform.catalog.application.signatory_policy import SignatoryPolicyInvalid
from vigia_platform.catalog.detail_codes import CatalogDetailCode
from vigia_platform.catalog.domain.agreements import AgreementConfirmation
from vigia_platform.catalog.domain.documents import DocumentRequestInvalid
from vigia_platform.catalog.domain.enums import (
    AgreementStatus,
    ConfirmationOrigin,
    DocumentKind,
    GateKind,
)
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.shared.context import Role, ScopeContext, ScopeLevel
from vigia_platform.shared.db import Transaction

pytestmark = pytest.mark.integration

MICRO: Final = timedelta(microseconds=1)


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[AgreementsWorld]:
    with agreements_world(postgres_endpoint, "catalog_agreements") as world:
        yield world


@pytest.fixture
def faults(world: AgreementsWorld) -> Iterator[AgreementsWorld]:
    """Para las pruebas que retienen la firma: la deja sana al terminar."""
    try:
        yield world
    finally:
        gate = world.g.signer.gate
        if gate is not None:
            gate.set()
        world.g.signer.gate = None


def _code(error: pytest.ExceptionInfo[CatalogRejected]) -> CatalogDetailCode:
    return error.value.detail_code


def _approved(world: AgreementsWorld, ready: Ready) -> AgreementApproval:
    assert ready.agreement is not None
    return world.approve(ready.installer, ready.agreement.agreement_id)


# --- Política de firmantes (BR-GOB-25) ------------------------------------------------------------


def test_the_signatory_policy_is_set_reread_updated_and_leaves_its_trail(
    world: AgreementsWorld,
) -> None:
    site = world.g.site()
    ((plant, _),) = site.zones()
    installer = world.g.installer(site)
    assert world.run(world.signatory_policies.policy(installer, plant)) is None

    first = world.run(world.signatory_policies.put_policy(installer, plant, list(POLICY_ROLES), 3))
    second = world.run(
        world.signatory_policies.put_policy(installer, plant, [Role.COPASST, Role.ADMINISTRATOR], 4)
    )

    read = world.run(world.signatory_policies.policy(installer, plant))
    assert read is not None and read.workers_role is Role.COPASST
    assert (read.required_roles, read.minimum) == ((Role.COPASST, Role.ADMINISTRATOR), 4)
    assert read.updated_by == installer.actor.id
    assert first.minimum == 3 and second.minimum == 4
    (row,) = world.fetch(
        "SELECT required_roles, minimum, workers_role FROM catalog.plant_signatory_policy"
        " WHERE plant_id = $1",
        plant,
    )
    assert (row["required_roles"], row["minimum"], row["workers_role"]) == (
        ["copasst", "administrator"],
        4,
        "copasst",
    )
    trail = world.fetch(
        "SELECT operation, scope_plant_id, convert_from(filters, 'UTF8') AS filters"
        " FROM shared.audit_entry"
        " WHERE organization_id = $1 AND actor_concession_id = $2 ORDER BY chain_sequence",
        site.organization_id,
        installer.concession_id,
    )
    operations = [r["operation"] for r in trail]
    assert operations.count("signatory_policy_changed") == 2
    assert operations.count("catalog_read") == 2  # las dos lecturas bajo concesión (A-56)
    changed = [r for r in trail if r["operation"] == "signatory_policy_changed"]
    assert all(r["scope_plant_id"] == plant for r in changed)
    assert json.loads(changed[-1]["filters"]) == {
        "minimum": 4,
        "required_roles": ["copasst", "administrator"],
    }


@pytest.mark.parametrize(
    ("roles", "minimum", "expected"),
    [
        ([Role.COPASST, Role.COORDINATOR_SST], 2, CatalogDetailCode.FEWER_THAN_THREE),
        (
            [Role.COORDINATOR_SST, Role.PLANT_MANAGER],
            3,
            CatalogDetailCode.WORKERS_REPRESENTATION_MISSING,
        ),
        ([], 3, CatalogDetailCode.WORKERS_REPRESENTATION_MISSING),
        ([Role.COORDINATOR_SST], 2, CatalogDetailCode.FEWER_THAN_THREE),
    ],
)
def test_signatory_policy_guards_write_nothing(
    world: AgreementsWorld, roles: list[Role], minimum: int, expected: CatalogDetailCode
) -> None:
    site = world.g.site()
    ((plant, _),) = site.zones()
    installer = world.g.installer(site)
    with pytest.raises(CatalogRejected) as error:
        world.run(world.signatory_policies.put_policy(installer, plant, roles, minimum))
    assert _code(error) is expected
    assert (
        world.fetch("SELECT 1 FROM catalog.plant_signatory_policy WHERE plant_id = $1", plant) == []
    )
    assert (
        world.fetch(
            "SELECT 1 FROM shared.audit_entry WHERE organization_id = $1"
            " AND operation = 'signatory_policy_changed'",
            site.organization_id,
        )
        == []
    )


@pytest.mark.parametrize(
    ("roles", "minimum"),
    [([Role.COPASST, Role.COPASST], 3), ([Role.COPASST], 33), ([Role.COPASST], True)],
)
def test_an_incoherent_signatory_policy_is_invalid(
    world: AgreementsWorld, roles: list[Role], minimum: int
) -> None:
    site = world.g.site()
    ((plant, _),) = site.zones()
    with pytest.raises(SignatoryPolicyInvalid):
        world.run(
            world.signatory_policies.put_policy(world.g.installer(site), plant, roles, minimum)
        )


def test_only_commissioning_run_sets_the_policy_and_a_reader_reads_it(
    world: AgreementsWorld,
) -> None:
    site = world.g.site()
    ((plant, _),) = site.zones()
    admin = world.g.member(site, Role.ADMINISTRATOR)
    with pytest.raises(ResourceNotFound):  # commissioning.run es solo del instalador
        world.run(world.signatory_policies.put_policy(admin, plant, list(POLICY_ROLES), 3))
    world.signatory_policy(site, plant)
    read = world.run(world.signatory_policies.policy(admin, plant))
    assert read is not None and read.minimum == 3


# --- Alta del acuerdo -----------------------------------------------------------------------------


def test_an_agreement_is_born_pending_with_its_expected_signers(world: AgreementsWorld) -> None:
    ready = world.ready(confirm=())
    agreement = ready.agreement
    assert agreement is not None
    assert agreement.status is AgreementStatus.PENDING_SIGNATURES
    assert [(s.role, s.user_id) for s in agreement.signatories] == [
        (s.role, s.user_id) for s in ready.signers
    ]
    assert all(s.display_name for s in agreement.signatories)  # el nombre, de identidad
    row = world.agreement_row(agreement.agreement_id)
    assert row["status"] == "pending_signatures" and row["approved_at"] is None
    # Nacer no escribe en el expediente: use_agreement_signed es de la aprobación.
    assert world.records(ready.zone, "use_agreement_signed") == []


@pytest.mark.parametrize(
    "case",
    [
        "no_policy",
        "role_not_in_policy",
        "user_without_role",
        "role_on_other_zone",
        "foreign_user",
        "no_copasst",
        "below_minimum",
    ],
)
def test_each_creation_guard_writes_nothing(world: AgreementsWorld, case: str) -> None:
    ready = world.ready(create=False, signatory_policy=case != "no_policy")
    site, plant, zone = ready.site, ready.plant, ready.zone
    signatories = [(s.role, s.user_id) for s in ready.signers]
    expected = CatalogDetailCode.SIGNATORY_USER_ROLE_MISMATCH
    if case == "no_policy":
        expected = CatalogDetailCode.SIGNATORY_ROLE_NOT_IN_POLICY
    elif case == "role_not_in_policy":
        line = world.signer(site, Role.LINE_MANAGER)
        signatories.append((Role.LINE_MANAGER, line.user_id))
        expected = CatalogDetailCode.SIGNATORY_ROLE_NOT_IN_POLICY
    elif case == "user_without_role":
        # Usuario de la organización con otro rol: declara uno que no tiene.
        other = world.signer(site, Role.LINE_MANAGER)
        signatories[0] = (Role.COORDINATOR_SST, other.user_id)
    elif case == "role_on_other_zone":
        # Tiene el rol, pero sobre otra zona de la planta (guarda de alcance de la zona).
        other_zone = uuid.uuid4()
        world.g.authz.add_zone(site.organization_id, plant, other_zone)
        other = world.signer(site, Role.COORDINATOR_SST, ScopeLevel.ZONE, other_zone)
        signatories[0] = (Role.COORDINATOR_SST, other.user_id)
    elif case == "foreign_user":
        foreign = world.signer(world.g.site(), Role.COORDINATOR_SST)
        signatories[0] = (Role.COORDINATOR_SST, foreign.user_id)
    elif case == "no_copasst":
        manager = world.signer(site, Role.PLANT_MANAGER)
        signatories = [s for s in signatories if s[0] is not Role.COPASST]
        signatories.append((Role.PLANT_MANAGER, manager.user_id))
        expected = CatalogDetailCode.WORKERS_REPRESENTATION_MISSING
    elif case == "below_minimum":
        world.signatory_policy(site, plant, minimum=4)
        expected = CatalogDetailCode.FEWER_THAN_THREE
    before = world.written(plant, zone)
    with pytest.raises(CatalogRejected) as error:
        world.create(ready, signatories=signatories)
    assert _code(error) is expected
    assert world.written(plant, zone) == before


def test_g8_an_agreement_of_another_zone_cannot_be_reused(world: AgreementsWorld) -> None:
    site = world.g.site(plants=1, zones=2)
    first = world.ready(site=site)
    approved = _approved(world, first)
    # La segunda zona de la misma planta, lista para su propio acuerdo.
    (_, (plant, zone_b)) = site.zones()
    world.mount(site, plant, zone_b, first.installer)
    other = Ready(site, plant, zone_b, first.installer, first.signers)
    before = world.written(plant, zone_b)

    with pytest.raises(CatalogRejected) as error:
        world.create(other, replaces=approved.agreement.agreement_id)

    assert _code(error) is CatalogDetailCode.AGREEMENT_REUSED_FROM_OTHER_ZONE
    assert world.written(plant, zone_b) == before
    # La zona B sí admite su propio acuerdo (sin citar el de A).
    assert world.create(other).zone_id == zone_b


def test_the_cited_agreement_must_be_the_one_in_force(world: AgreementsWorld) -> None:
    ready = world.ready()
    approved = _approved(world, ready)
    with pytest.raises(AgreementConflict):  # hay uno vigente y no se cita
        world.create(ready)
    with pytest.raises(AgreementRequestInvalid):  # el citado no existe
        world.create(ready, replaces=uuid.uuid4())
    replacement = world.create(ready, replaces=approved.agreement.agreement_id)
    for signer in ready.signers:
        world.confirm(signer.context, replacement.agreement_id)
    world.approve(ready.installer, replacement.agreement_id)
    with pytest.raises(AgreementConflict):  # el sustituido ya no es el vigente
        world.create(ready, replaces=approved.agreement.agreement_id)


def test_the_optional_document_is_verified_and_used(world: AgreementsWorld) -> None:
    ready = world.ready(create=False)
    wrong = world.g.document(ready.installer, ready.plant, DocumentKind.SCOPE_RECORD)
    before = world.written(ready.plant, ready.zone)
    with pytest.raises(DocumentRequestInvalid):
        world.create(ready, document_ref=wrong)
    assert world.written(ready.plant, ready.zone) == before
    assert world.g.grant_status(wrong["document_id"]) == "issued"
    document = world.g.document(ready.installer, ready.plant, DocumentKind.USE_AGREEMENT)

    agreement = world.create(ready, document_ref=document)

    assert agreement.document_ref is not None
    assert agreement.document_ref.sha256 == document["sha256"]
    assert world.g.grant_status(document["document_id"]) == "used"


# --- Confirmación (BR-GOB-26, 27) -----------------------------------------------------------------


def test_each_signer_confirms_in_their_session_with_their_origin(world: AgreementsWorld) -> None:
    ready = world.ready(confirm=())
    assert ready.agreement is not None
    for signer in ready.signers:
        result = world.confirm(signer.context, ready.agreement.agreement_id)
        assert result.created
        assert result.confirmation.role_in_use is signer.role
    rows = world.confirmation_rows(ready.agreement.agreement_id)
    by_user = {r["user_id"]: (r["role_in_use"], r["origin"]) for r in rows}
    assert by_user == {
        s.user_id: (
            s.role.value,
            "transparency" if s.role is Role.COPASST else "management",
        )
        for s in ready.signers
    }


def test_g14_a_user_who_is_not_an_expected_signer_is_rejected(world: AgreementsWorld) -> None:
    ready = world.ready(confirm=())
    assert ready.agreement is not None
    outsiders = [
        world.signer(ready.site, Role.COORDINATOR_SST),  # gestión, con agreements.sign
        world.signer(ready.site, Role.COPASST),  # otro copasst de la organización
    ]
    for outsider in outsiders:
        with pytest.raises(CatalogRejected) as error:
            world.confirm(outsider.context, ready.agreement.agreement_id)
        assert _code(error) is CatalogDetailCode.SIGNATORY_NOT_EXPECTED
    assert world.confirmation_rows(ready.agreement.agreement_id) == []


def test_a_signer_who_lost_the_expected_role_on_the_zone_is_not_expected(
    world: AgreementsWorld,
) -> None:
    ready = world.ready(confirm=())
    assert ready.agreement is not None
    signer = ready.signers[0]
    for row in world.fetch(
        "SELECT assignment_id FROM identity.role_assignment WHERE user_id = $1", signer.user_id
    ):
        world.g.authz.remove_assignment(row["assignment_id"])
    # Ahora solo es mando de línea: tiene transparency.read, pero no el rol esperado.
    world.g.authz.assign(ready.site.organization_id, signer.user_id, Role.LINE_MANAGER)
    cookie = world.g.authz.open_session(ready.site.organization_id, signer.user_id)
    context = world.run(world.g.authz.contexts.context_from_session(cookie)).context
    with pytest.raises(CatalogRejected) as error:
        world.confirm(context, ready.agreement.agreement_id)
    assert _code(error) is CatalogDetailCode.SIGNATORY_NOT_EXPECTED
    assert world.confirmation_rows(ready.agreement.agreement_id) == []


def test_g9_a_provider_under_concession_never_confirms(world: AgreementsWorld) -> None:
    ready = world.ready(confirm=())
    assert ready.agreement is not None
    installer = ready.installer
    assert installer.concession_id is not None
    denials = _denials(world, ready, installer)

    with pytest.raises(ResourceNotFound):
        world.confirm(installer, ready.agreement.agreement_id)

    assert world.confirmation_rows(ready.agreement.agreement_id) == []
    after = _denials(world, ready, installer)
    assert after == [*denials, ("authorization_denied", "agreements.sign")]


def _denials(world: AgreementsWorld, ready: Ready, context: ScopeContext) -> list[tuple[str, str]]:
    return [
        (r["operation"], r["key"])
        for r in world.fetch(
            "SELECT operation, convert_from(filters, 'UTF8')::jsonb ->> 'permission_key' AS key"
            " FROM shared.audit_entry"
            " WHERE organization_id = $1 AND actor_concession_id = $2"
            " AND operation = 'authorization_denied' ORDER BY chain_sequence",
            ready.site.organization_id,
            context.concession_id,
        )
    ]


def test_a_repeated_confirmation_returns_the_first_and_writes_nothing(
    world: AgreementsWorld,
) -> None:
    ready = world.ready(confirm=())
    assert ready.agreement is not None
    signer = ready.copasst
    first = world.confirm(signer.context, ready.agreement.agreement_id)
    again = world.confirm(signer.context, ready.agreement.agreement_id)
    assert first.created and not again.created
    assert again.confirmation == first.confirmation
    assert len(world.confirmation_rows(ready.agreement.agreement_id)) == 1


class BarrierRepository(PostgresAgreementRepository):
    """Retiene cada ``confirm`` hasta que llegan las N: todas han leído «sin confirmar»."""

    def __init__(self, parties: int) -> None:
        self.barrier = asyncio.Barrier(parties)

    async def confirm(self, transaction: Transaction, confirmation: AgreementConfirmation) -> bool:
        await self.barrier.wait()
        return await super().confirm(transaction, confirmation)


@pytest.mark.parametrize("attempt", [1, 2, 3])
def test_n_simultaneous_confirmations_of_one_user_leave_one_row(
    world: AgreementsWorld, attempt: int
) -> None:
    # Las N pasan la lectura previa sin ver ninguna confirmación y llegan al INSERT a la vez: la
    # clave (agreement_id, user_id) deja una sola fila. Sin la clave, N filas: la prueba falla.
    n = 4
    ready = world.ready(confirm=())
    assert ready.agreement is not None
    service = world.build_agreements(repository=BarrierRepository(n))
    signer = ready.copasst
    agreement_id = ready.agreement.agreement_id

    async def race() -> list[Any]:
        async with asyncio.timeout(WAIT_SECONDS):
            return list(
                await asyncio.gather(
                    *(service.confirm(signer.context, agreement_id) for _ in range(n)),
                    return_exceptions=True,
                )
            )

    results = world.run(race())

    assert all(isinstance(r, ConfirmationResult) for r in results), results
    assert sum(r.created for r in results) == 1
    assert len({r.confirmation.confirmed_at for r in results}) == 1
    rows = world.confirmation_rows(agreement_id)
    assert len(rows) == 1 and rows[0]["origin"] == ConfirmationOrigin.TRANSPARENCY.value


# --- Aprobación (BR-GOB-29, 30) -------------------------------------------------------------------


def test_the_approval_opens_usage_in_one_transaction(world: AgreementsWorld) -> None:
    ready = world.ready()
    assert ready.agreement is not None
    agreement_id = ready.agreement.agreement_id

    approval = _approved(world, ready)

    assert approval.transition is not None
    assert approval.state.resulting_mode is ZoneMode.PRODUCTIVE
    row = world.agreement_row(agreement_id)
    assert row["status"] == "approved"
    assert row["approved_by"] == ready.installer.actor.id
    (usage,) = world.usage_history(ready.zone)
    # BR-GOB-30: la compuerta de uso rige desde el instante de la aprobación.
    assert usage["effective_from"] == row["approved_at"] == approval.agreement.approved_at
    assert (usage["status"], usage["record_id"]) == ("approved", agreement_id)
    (signed,) = world.records(ready.zone, "use_agreement_signed")
    assert signed["source_key"] == str(agreement_id)
    assert signed["record_id"] == row["ledger_record_id"]
    content = signed["content"]
    assert content["agreement_id"] == str(agreement_id)
    assert {c["user_id"] for c in content["confirmations"]} == {
        str(s.user_id) for s in ready.signers
    }
    assert "display_name" not in str(content)
    changed = world.records(ready.zone, "gate_state_changed")[-1]["content"]
    assert changed == {
        "zone_id": str(ready.zone),
        "gate": "usage",
        "status": "approved",
        "resulting_mode": "productive",
        "agreement_id": str(agreement_id),
    }
    events = world.events(ready.plant, ready.zone)
    assert sorted(e["event_name"] for e in events[-2:]) == ["gate_state_changed", "zone_activated"]
    (activated,) = world.events(ready.plant, ready.zone, "zone_activated")
    assert activated["payload"] == {
        "zone_id": str(ready.zone),
        "activated_at": activated["payload"]["activated_at"],
        "agreement_id": str(agreement_id),
        "commissioning_record_id": str(ready.commissioning_record_id),
    }
    projection = world.g.projection(ready.zone)
    assert projection is not None and projection["resulting_mode"] == "productive"
    assert projection["usage"]["agreement_id"] == str(agreement_id)


_GUARDS: Final = ("mounted", "record", "signatures", "plant_policy")
_EXPECTED: Final = (
    CatalogDetailCode.MOUNTING_GATE_PENDING,
    CatalogDetailCode.COMMISSIONING_RECORD_MISSING,
    CatalogDetailCode.SIGNATURES_INCOMPLETE,
    CatalogDetailCode.PLANT_POLICY_MISSING,
)
_COMBINATIONS: Final = [frozenset(i for i in range(4) if mask >> i & 1) for mask in range(1, 16)]


@pytest.mark.parametrize(
    "missing", _COMBINATIONS, ids=lambda m: "+".join(_GUARDS[i] for i in sorted(m))
)
def test_each_combination_of_missing_guards_answers_the_first_and_writes_nothing(
    world: AgreementsWorld, missing: frozenset[int]
) -> None:
    ready = world.ready(
        mounted=0 not in missing,
        record=1 not in missing,
        confirm=SIGNER_ROLES[:2] if 2 in missing else SIGNER_ROLES,
        plant_policy=3 not in missing,
    )
    assert ready.agreement is not None
    before = world.written(ready.plant, ready.zone)
    calls = world.g.signer.calls

    with pytest.raises(CatalogRejected) as error:
        world.approve(ready.installer, ready.agreement.agreement_id)

    assert _code(error) is _EXPECTED[min(missing)]
    assert world.written(ready.plant, ready.zone) == before
    assert world.g.signer.calls == calls  # ni siquiera firma el sobre


def test_approving_again_returns_the_state_without_effects(world: AgreementsWorld) -> None:
    ready = world.ready()
    first = _approved(world, ready)
    before = world.written(ready.plant, ready.zone)
    again = _approved(world, ready)
    assert again.transition is None
    assert again.agreement == first.agreement
    assert again.state.usage == first.state.usage
    assert world.written(ready.plant, ready.zone) == before


def test_pr_gob_03_a_replacement_never_leaves_an_instant_without_approved_usage(
    world: AgreementsWorld,
) -> None:
    ready = world.ready()
    first = _approved(world, ready)
    replacement = world.create(ready, replaces=first.agreement.agreement_id)
    for signer in ready.signers:
        world.confirm(signer.context, replacement.agreement_id)

    second = world.approve(ready.installer, replacement.agreement_id)

    at = second.agreement.approved_at
    assert at is not None
    old = world.agreement_row(first.agreement.agreement_id)
    assert old["status"] == "superseded" and old["superseded_at"] == at
    history = world.usage_history(ready.zone)
    assert [(r["status"], r["record_id"]) for r in history] == [
        ("approved", first.agreement.agreement_id),
        ("approved", replacement.agreement_id),
    ]
    # Sobre la historia: contiguos, sin hueco ni solape, y approved en cada instante del cambio.
    assert history[0]["effective_until"] == history[1]["effective_from"] == at
    assert history[1]["effective_until"] is None
    for instant in (history[0]["effective_from"], at - MICRO, at, at + MICRO):
        found = world.run(
            world.g.gates.state_at(ready.installer, ready.zone, GateKind.USAGE, instant)
        )
        assert found is not None and found.status is GateStatus.APPROVED, instant
    assert (
        world.run(
            world.g.gates.state_at(ready.installer, ready.zone, GateKind.USAGE, at - MICRO)
        ).record_id
        == first.agreement.agreement_id
    )
    # La sustitución no vuelve a activar la zona.
    assert len(world.events(ready.plant, ready.zone, "zone_activated")) == 1
    assert second.state.resulting_mode is ZoneMode.PRODUCTIVE


@pytest.mark.parametrize("final", ["superseded", "revoked"])
def test_a_superseded_or_revoked_agreement_is_a_conflict(
    world: AgreementsWorld, final: str
) -> None:
    ready = world.ready()
    first = _approved(world, ready)
    if final == "superseded":
        replacement = world.create(ready, replaces=first.agreement.agreement_id)
        for signer in ready.signers:
            world.confirm(signer.context, replacement.agreement_id)
        world.approve(ready.installer, replacement.agreement_id)
    else:
        world.g.revoke(ready.installer, ready.zone, GateKind.USAGE)
    before = world.written(ready.plant, ready.zone)
    with pytest.raises(AgreementConflict):
        world.approve(ready.installer, first.agreement.agreement_id)
    assert world.written(ready.plant, ready.zone) == before


# --- Revocación -----------------------------------------------------------------------------------


def test_revoking_usage_revokes_the_agreement_and_keeps_what_was_written(
    world: AgreementsWorld,
) -> None:
    ready = world.ready()
    first = _approved(world, ready)
    records_before = world.g.records_of(ready.zone)

    revoked = world.g.revoke(ready.installer, ready.zone, GateKind.USAGE, REASON)

    row = world.agreement_row(first.agreement.agreement_id)
    assert row["status"] == "revoked"
    assert row["revoked_at"] == revoked.interval.effective_from
    assert row["approved_at"] == first.agreement.approved_at  # el cierre anterior permanece
    assert revoked.state.resulting_mode is ZoneMode.COMMISSIONING
    after = world.g.records_of(ready.zone)
    assert after[: len(records_before)] == records_before  # lo escrito antes permanece
    assert [r["record_type"] for r in after[len(records_before) :]] == ["gate_state_changed"]
    # El acuerdo no se reutiliza: uno nuevo (sin citar el revocado) vuelve a activar la zona.
    with pytest.raises(AgreementConflict):
        world.create(ready, replaces=first.agreement.agreement_id)
    renewed = world.create(ready)
    for signer in ready.signers:
        world.confirm(signer.context, renewed.agreement_id)
    again = world.approve(ready.installer, renewed.agreement_id)
    assert again.state.resulting_mode is ZoneMode.PRODUCTIVE
    assert len(world.events(ready.plant, ready.zone, "zone_activated")) == 2


# --- Concurrencia ---------------------------------------------------------------------------------


async def _advisory_waiters(admin: Any) -> int:
    value: int = await admin.fetchval(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
    )
    return value


def _race(world: AgreementsWorld, calls: list[Any], waiters: int) -> list[Any]:
    """Lanza ``calls`` a la vez con la firma retenida; la suelta cuando ``waiters`` esperan la
    exclusión de la zona (todas han llegado hasta el candado)."""
    gate = threading.Event()
    world.g.signer.gate = gate
    admin = world.g.authz.sessions.admin

    async def race() -> list[Any]:
        tasks = [asyncio.create_task(call()) for call in calls]
        async with asyncio.timeout(WAIT_SECONDS):
            while await _advisory_waiters(admin) < waiters:
                await asyncio.sleep(POLL_SECONDS)
        gate.set()
        return list(await asyncio.gather(*tasks, return_exceptions=True))

    try:
        results: list[Any] = world.run(race())
    finally:
        world.g.signer.gate = None
    return results


@pytest.mark.parametrize("attempt", [1, 2, 3])
def test_n_simultaneous_approvals_have_a_single_effect(
    faults: AgreementsWorld, attempt: int
) -> None:
    # Con el candado de la zona, la primera aprueba y las demás esperan, ven «approved» y
    # devuelven el estado sin efectos. Sin él, leen «pending» antes de esperar y terminan en
    # carrera (no aprobación): la prueba falla.
    world = faults
    n = 3
    ready = world.ready()
    assert ready.agreement is not None
    agreement_id = ready.agreement.agreement_id
    world.g.advance()

    results = _race(
        world,
        [lambda: world.agreements.approve(ready.installer, agreement_id) for _ in range(n)],
        waiters=n - 1,
    )

    assert all(isinstance(r, AgreementApproval) for r in results), results
    assert sum(r.transition is not None for r in results) == 1
    assert len(world.records(ready.zone, "use_agreement_signed")) == 1
    assert len(world.events(ready.plant, ready.zone, "zone_activated")) == 1
    assert len(world.usage_history(ready.zone)) == 1


@pytest.mark.parametrize("attempt", [1, 2, 3])
def test_a_replacement_and_a_usage_revocation_at_once_never_deadlock(
    faults: AgreementsWorld, attempt: int
) -> None:
    # Prueba cruzada (orden de candados): aprobar el sustituto y revocar el uso comparten la
    # exclusión de la zona y la cadena de la planta, en ese orden en las dos. Se ordenan: o la
    # revocación gana (el sustituto ya no cita al vigente: conflict) o la aprobación gana (y la
    # revocación revoca al sustituto). Nunca un transitorio ni un interbloqueo.
    world = faults
    ready = world.ready()
    first = _approved(world, ready)
    replacement = world.create(ready, replaces=first.agreement.agreement_id)
    for signer in ready.signers:
        world.confirm(signer.context, replacement.agreement_id)
    world.g.advance()

    results = _race(
        world,
        [
            lambda: world.agreements.approve(ready.installer, replacement.agreement_id),
            lambda: world.g.gates.revoke(ready.installer, ready.zone, GateKind.USAGE, REASON),
        ],
        waiters=1,
    )

    approval, revocation = results
    assert not isinstance(revocation, BaseException), revocation
    rows = {
        a: world.agreement_row(a)["status"]
        for a in (first.agreement.agreement_id, replacement.agreement_id)
    }
    if isinstance(approval, AgreementApproval):
        assert rows == {
            first.agreement.agreement_id: "superseded",
            replacement.agreement_id: "revoked",
        }
    else:
        assert isinstance(approval, AgreementConflict), approval
        assert rows == {
            first.agreement.agreement_id: "revoked",
            replacement.agreement_id: "pending_signatures",
        }
    assert world.g.projection(ready.zone)["usage"]["status"] == "revoked"  # type: ignore[index]


# --- Guardas de alcance (NFR-GOB-30) --------------------------------------------------------------


def test_out_of_scope_confirmations_approval_and_transparency_answer_not_found(
    world: AgreementsWorld,
) -> None:
    site = world.g.site(plants=2, zones=1)
    ready = world.ready(site=site, confirm=())
    assert ready.agreement is not None
    agreement_id = ready.agreement.agreement_id
    (_, (plant_b, zone_b)) = site.zones()
    plant_installer = world.g.installer(site, ScopeLevel.PLANT, plant_b)
    zone_copasst = world.signer(site, Role.COPASST, ScopeLevel.ZONE, zone_b)
    zone_coordinator = world.signer(site, Role.COORDINATOR_SST, ScopeLevel.ZONE, zone_b)
    foreign = world.ready(confirm=())
    before = world.written(ready.plant, ready.zone)

    attempts: list[tuple[ScopeContext, uuid.UUID]] = [
        (foreign.copasst.context, agreement_id),  # otra organización (transparencia)
        (foreign.signers[0].context, agreement_id),  # otra organización (gestión)
        (zone_copasst.context, agreement_id),  # misma organización, otra zona
        (zone_coordinator.context, agreement_id),
        (ready.copasst.context, uuid.uuid4()),  # inexistente
    ]
    for context, agreement in attempts:
        with pytest.raises(ResourceNotFound):
            world.confirm(context, agreement)
    for context in (foreign.installer, plant_installer):  # otra organización, otra planta
        with pytest.raises(ResourceNotFound):
            world.approve(context, agreement_id)
    with pytest.raises(ResourceNotFound):
        world.approve(ready.installer, uuid.uuid4())
    for context, zone in (
        (foreign.copasst.context, ready.zone),
        (zone_copasst.context, ready.zone),
        (plant_installer, ready.zone),
        (ready.copasst.context, uuid.uuid4()),
    ):
        with pytest.raises(ResourceNotFound):
            world.run(world.transparency.view(context, zone))
    assert world.written(ready.plant, ready.zone) == before
    # Dentro del alcance sí: la transparencia de la otra zona y la confirmación propia.
    assert world.run(world.transparency.view(zone_copasst.context, zone_b)).zone_id == zone_b
    assert world.confirm(ready.copasst.context, agreement_id).created


def test_the_statements_filter_the_zone(world: AgreementsWorld) -> None:
    # Dos zonas de la misma planta: B tiene acuerdo vigente y otro pendiente. Sin el filtro de
    # zona, la transparencia de A mostraría los de B.
    site = world.g.site(plants=1, zones=2)
    ready_b = world.ready(site=site)
    approved = _approved(world, ready_b)
    pending = world.create(ready_b, replaces=approved.agreement.agreement_id)
    (_, (plant, zone_a)) = site.zones()  # ``ready`` usa la primera zona: A es la otra
    assert zone_a != ready_b.zone
    world.g.equip(site, plant, zone_a)
    copasst = ready_b.copasst.context

    view_a = world.run(world.transparency.view(copasst, zone_a))
    view_b = world.run(world.transparency.view(copasst, ready_b.zone))

    assert view_a.current_agreement is None and view_a.pending_confirmation_for_me is None
    assert view_b.current_agreement is not None
    assert view_b.current_agreement.agreement_id == approved.agreement.agreement_id
    assert view_b.pending_confirmation_for_me == pending.agreement_id
