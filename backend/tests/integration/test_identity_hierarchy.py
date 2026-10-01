"""Ejemplos de ``identity.hierarchy`` contra PostgreSQL 16 real (TASK-126, LC-NUC-05).

Los criterios de aceptación y los bordes de cada regla que las máquinas de estado no fijan con un
valor concreto:

- BR-NUC-05: invitar un correo que ya existe en **otra** organización (o en la misma) se rechaza
  con ``email_unavailable`` y un mensaje que no nombra ninguna organización; con mayúsculas o
  espacios es el mismo correo.
- BR-NUC-32 y la nota de §10.1: sin ``EmailSenderPort`` (o con él ``unavailable`` o caído) el
  enlace vuelve **una sola vez** y queda ``invitation_link_disclosed``; con él ``queued`` el
  enlace no vuelve y no hay divulgación; el evento ``user_invited`` se publica igual.
- NFR-NUC-29: un usuario sin la aceptación de la versión vigente no obtiene contexto
  (``privacy_notice_required``) salvo para aceptarla, y ese contexto no tiene asignaciones.
- Génesis: ``organization_created`` es la secuencia 1 de la cadena de la organización y
  ``plant_created`` la de la planta.
- Activación: token usado, vencido, cancelado o inexistente responden igual; el administrador no
  se activa sin confirmar el segundo factor; la aceptación del aviso queda con su versión.
- ``nuc_0009``: una asignación con planta o zona de otra organización se rechaza en la base.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest

from tests.hierarchy_support import (
    GOOD_PASSWORD,
    REJECTED_PASSWORD,
    HierarchyEnvironment,
    RecordingEmailSender,
    hierarchy_environment,
    new_code,
    new_email,
)
from tests.identity_db import BASE_TIME
from tests.integration.conftest import PostgresEndpoint
from tests.session_support import GOOD_CODE
from vigia_platform.identity.application.common import IdentityRejected, IdentityRejection
from vigia_platform.identity.application.hierarchy import GenesisRequest, PlantSpec, ZoneSpec
from vigia_platform.identity.application.invitations import (
    EmailSenderRegistry,
    InvitationDelivery,
    token_hash,
)
from vigia_platform.identity.application.roles import AssignmentRequest
from vigia_platform.identity.application.users import InviteRequest, ProfileChange
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.identity.authz.context import ContextUnavailable, ContextUnavailableReason
from vigia_platform.identity.domain.privacy_notice import CURRENT_PRIVACY_NOTICE_VERSION
from vigia_platform.shared.context import Role, ScopeContext, ScopeLevel

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def env(postgres_endpoint: PostgresEndpoint) -> Iterator[HierarchyEnvironment]:
    with hierarchy_environment(postgres_endpoint, "hierarchy_examples") as environment:
        yield environment


def _plant() -> PlantSpec:
    return PlantSpec(new_code("PL"), "Planta de prensas", "CO", "us-east-1", "America/Bogota")


class Organization:
    """Una organización cliente creada por la génesis, con su administrador ya activo."""

    def __init__(self, env: HierarchyEnvironment) -> None:
        self.env = env
        result = env.run(
            env.genesis().create_client_organization(
                env.operator_context(),
                GenesisRequest(
                    code=new_code("ORG"),
                    name="Organización sintética",
                    plant=_plant(),
                    administrator_email=new_email("admin"),
                    administrator_display_name="Administración sintética",
                ),
            )
        )
        self.organization_id: uuid.UUID = result.organization_id
        self.plant_id: uuid.UUID = result.plant_id
        self.admin_id: uuid.UUID = result.administrator_user_id
        self.genesis = result
        link = result.invitation.link
        assert link is not None
        env.run(
            env.invitations().accept_invitation(
                link.split("#", 1)[1], GOOD_PASSWORD, CURRENT_PRIVACY_NOTICE_VERSION, GOOD_CODE
            )
        )

    def admin(self) -> ScopeContext:
        return self.env.session_context(self.organization_id, self.admin_id)


def _token(link: str | None) -> str:
    assert link is not None
    return link.split("#", 1)[1]


def _audit(env: HierarchyEnvironment, organization_id: uuid.UUID, operation: str) -> list[Any]:
    return env.fetch(
        "SELECT * FROM shared.audit_entry WHERE organization_id = $1 AND operation = $2"
        " ORDER BY chain_sequence",
        organization_id,
        operation,
    )


def _invite(
    env: HierarchyEnvironment,
    org: Organization,
    email: str,
    *,
    role: Role = Role.COORDINATOR_SST,
    senders: EmailSenderRegistry | None = None,
    disclose: bool = False,
) -> Any:
    return env.run(
        env.users(senders).invite_user(
            org.admin(),
            InviteRequest(
                email=email,
                display_name="Persona sintética",
                assignments=(AssignmentRequest(role, ScopeLevel.PLANT, org.plant_id),),
                disclose_link=disclose,
            ),
        )
    )


# --- Génesis ------------------------------------------------------------------------------------


def test_genesis_opens_the_organization_and_plant_chains(env: HierarchyEnvironment) -> None:
    org = Organization(env)
    rows = env.fetch(
        "SELECT record_type, plant_id, chain_sequence FROM ledger.ledger_record"
        " WHERE organization_id = $1 ORDER BY received_at, chain_sequence",
        org.organization_id,
    )
    assert [(r["record_type"], r["plant_id"], r["chain_sequence"]) for r in rows] == [
        ("organization_created", None, 1),
        ("plant_created", org.plant_id, 1),
    ]
    (organization,) = env.fetch(
        "SELECT kind, status, created_by FROM identity.organization WHERE organization_id = $1",
        org.organization_id,
    )
    assert (organization["kind"], organization["status"]) == ("client", "active")
    assert organization["created_by"] == env.authz.operator_id
    (admin,) = env.fetch(
        "SELECT status, second_factor_required, privacy_notice_version_accepted"
        " FROM identity.user_account WHERE user_id = $1",
        org.admin_id,
    )
    assert tuple(admin) == ("active", True, CURRENT_PRIVACY_NOTICE_VERSION)
    assert org.genesis.invitation.delivery is InvitationDelivery.LINK_DISCLOSED
    for operation in ("plant_created", "user_invited", "role_assigned"):
        assert _audit(env, org.organization_id, operation), operation


def test_genesis_requires_platform_organizations_create(env: HierarchyEnvironment) -> None:
    org = Organization(env)
    with pytest.raises(ResourceNotFound):
        env.run(
            env.genesis().create_client_organization(
                org.admin(),
                GenesisRequest(
                    code=new_code("ORG"),
                    name="Intento",
                    plant=_plant(),
                    administrator_email=new_email(),
                    administrator_display_name="Persona sintética",
                ),
            )
        )


# --- BR-NUC-05: correo único sin revelar dónde -------------------------------------------------


def test_email_of_another_organization_is_rejected_without_naming_it(
    env: HierarchyEnvironment,
) -> None:
    first, second = Organization(env), Organization(env)
    email = new_email("compartido")
    _invite(env, first, email)
    for variant in (email, email.upper(), f"  {email}  "):
        with pytest.raises(IdentityRejected) as raised:
            _invite(env, second, variant)
        assert raised.value.code is IdentityRejection.EMAIL_UNAVAILABLE
        assert raised.value.api_code == "conflict"
        message = str(raised.value) + raised.value.message_es + repr(raised.value.details)
        for secret in (str(first.organization_id), str(second.organization_id), "Organización"):
            assert secret not in message
    # El mismo rechazo para un correo de la propia organización: no distingue dónde existe.
    with pytest.raises(IdentityRejected) as same:
        _invite(env, first, email)
    assert same.value.code is IdentityRejection.EMAIL_UNAVAILABLE
    assert str(same.value) == str(raised.value)
    # Nada se escribió en la segunda organización.
    assert not env.fetch(
        "SELECT 1 FROM identity.user_account WHERE organization_id = $1 AND email = $2",
        second.organization_id,
        email,
    )


# --- Entrega del enlace -------------------------------------------------------------------------


def test_without_email_sender_the_link_is_disclosed_once_and_audited(
    env: HierarchyEnvironment,
) -> None:
    org = Organization(env)
    outcome = _invite(env, org, new_email(), senders=EmailSenderRegistry())
    assert outcome.delivery is InvitationDelivery.LINK_DISCLOSED
    token = _token(outcome.link)
    assert outcome.link.startswith("https://app.vigia.example/invitacion#")
    (invitation,) = env.fetch(
        "SELECT token_hash, disclosed_to_inviter_at, status FROM identity.invitation"
        " WHERE invitation_id = $1",
        outcome.invitation_id,
    )
    assert invitation["token_hash"] == token_hash(token)
    assert invitation["disclosed_to_inviter_at"] is not None
    assert invitation["status"] == "pending"
    disclosed = [
        row
        for row in _audit(env, org.organization_id, "invitation_link_disclosed")
        if row["resource_id"] == outcome.invitation_id
    ]
    assert len(disclosed) == 1
    # El token en claro no está en ninguna fila ni en la auditoría.
    assert not env.fetch(
        "SELECT 1 FROM shared.audit_entry WHERE convert_from(filters, 'UTF8') LIKE $1",
        f"%{token}%",
    )
    assert "link" not in repr(outcome) or token not in repr(outcome)
    # El evento se publica igual, sin consumidor de correo que dependa de él.
    events = env.fetch(
        "SELECT payload FROM shared.outbox_event WHERE organization_id = $1"
        " AND event_name = 'user_invited'",
        org.organization_id,
    )
    assert any(str(outcome.invitation_id) in str(event["payload"]) for event in events)


@pytest.mark.parametrize("result", ["unavailable", "raise"])
def test_unavailable_email_sender_degrades_to_one_disclosure(
    env: HierarchyEnvironment, result: Any
) -> None:
    org = Organization(env)
    sender = RecordingEmailSender(result=result)
    senders = EmailSenderRegistry()
    senders.register(sender)
    outcome = _invite(env, org, new_email(), senders=senders)
    assert len(sender.sent) == 1
    assert outcome.delivery is InvitationDelivery.LINK_DISCLOSED
    assert outcome.link is not None
    assert [
        row
        for row in _audit(env, org.organization_id, "invitation_link_disclosed")
        if row["resource_id"] == outcome.invitation_id
    ]


def test_queued_email_sends_the_link_in_process_and_never_discloses_it(
    env: HierarchyEnvironment,
) -> None:
    org = Organization(env)
    sender = RecordingEmailSender(result="queued")
    senders = EmailSenderRegistry()
    senders.register(sender)
    email = new_email()
    outcome = _invite(env, org, email, senders=senders)
    assert outcome.delivery is InvitationDelivery.EMAIL_QUEUED
    assert outcome.link is None
    (message,) = sender.sent
    assert message.to_email == email and message.kind == "platform_invitation"
    assert message.related_ids == (outcome.invitation_id,)
    assert "https://app.vigia.example/invitacion#" in message.body_es
    assert not [
        row
        for row in _audit(env, org.organization_id, "invitation_link_disclosed")
        if row["resource_id"] == outcome.invitation_id
    ]
    # A petición del administrador: se muestra una vez y no se envía correo.
    asked = _invite(env, org, new_email(), senders=senders, disclose=True)
    assert asked.delivery is InvitationDelivery.LINK_DISCLOSED and asked.link is not None
    assert len(sender.sent) == 1


def test_registry_accepts_a_single_sender() -> None:
    senders = EmailSenderRegistry()
    senders.register(RecordingEmailSender())
    with pytest.raises(ValueError, match="ya hay"):
        senders.register(RecordingEmailSender())


# --- Aviso de tratamiento de datos (NFR-NUC-29) ------------------------------------------------


def test_without_current_notice_there_is_no_usable_context_except_to_accept(
    env: HierarchyEnvironment,
) -> None:
    org = Organization(env)
    contexts = env.authz.contexts
    user = env.authz.sessions.add_user(org.organization_id, privacy_notice=None).user_id
    env.authz.assign(org.organization_id, user, Role.COORDINATOR_SST)
    cookie = env.authz.open_session(org.organization_id, user)
    with pytest.raises(ContextUnavailable) as raised:
        env.run(contexts.context_from_session(cookie))
    assert raised.value.reason is ContextUnavailableReason.PRIVACY_NOTICE_REQUIRED
    pending = env.run(contexts.context_from_session(cookie, privacy_notice_acceptance=True))
    assert pending.privacy_notice_pending is True
    assert pending.context.allowed_scopes == ()
    # Ese contexto no autoriza nada: ni leer la jerarquía de su propia organización.
    with pytest.raises(ResourceNotFound):
        env.run(env.users().update_profile(pending.context, user, ProfileChange(display_name="X")))
    # Una versión vieja no vale; la vigente sí, y desde ahí hay contexto con sus asignaciones.
    with pytest.raises(IdentityRejected) as outdated:
        env.run(env.privacy_notice().accept(pending, "v0-anterior"))
    assert outdated.value.code is IdentityRejection.PRIVACY_NOTICE_OUTDATED
    assert env.run(env.privacy_notice().accept(pending, CURRENT_PRIVACY_NOTICE_VERSION)) is True
    assert env.run(env.privacy_notice().accept(pending, CURRENT_PRIVACY_NOTICE_VERSION)) is False
    usable = env.run(contexts.context_from_session(cookie))
    assert usable.context.allowed_scopes
    (acceptance,) = env.fetch(
        "SELECT notice_version FROM identity.privacy_notice_acceptance WHERE user_id = $1", user
    )
    assert acceptance["notice_version"] == CURRENT_PRIVACY_NOTICE_VERSION
    (audit,) = [
        row
        for row in _audit(env, org.organization_id, "privacy_notice_accepted")
        if row["resource_id"] == user
    ]
    assert CURRENT_PRIVACY_NOTICE_VERSION in bytes(audit["filters"]).decode()


def test_an_outdated_acceptance_also_blocks_and_a_concession_cannot_accept(
    env: HierarchyEnvironment,
) -> None:
    org = Organization(env)
    contexts = env.authz.contexts
    user = env.authz.sessions.add_user(org.organization_id, privacy_notice="v0-anterior").user_id
    cookie = env.authz.open_session(org.organization_id, user)
    with pytest.raises(ContextUnavailable):
        env.run(contexts.context_from_session(cookie))
    with pytest.raises(ContextUnavailable) as raised:
        env.run(
            contexts.context_from_session(
                cookie, concession_id=uuid.uuid4(), privacy_notice_acceptance=True
            )
        )
    assert raised.value.reason is ContextUnavailableReason.PRIVACY_NOTICE_REQUIRED


# --- Activación ---------------------------------------------------------------------------------


def test_activation_rejects_reuse_expiry_and_unknown_tokens_alike(
    env: HierarchyEnvironment,
) -> None:
    org = Organization(env)
    invitations = env.invitations()
    used = _invite(env, org, new_email())
    env.run(
        invitations.accept_invitation(
            _token(used.link), GOOD_PASSWORD, CURRENT_PRIVACY_NOTICE_VERSION
        )
    )
    # Desactivar cancela la invitación pendiente: su token deja de valer.
    cancelled = _invite(env, org, new_email())
    env.run(env.users().deactivate_user(org.admin(), cancelled.user_id))
    expired = _invite(env, org, new_email())
    # Justo antes de las 72 horas sigue valiendo (se comprueba sin consumirlo).
    env.advance(timedelta(hours=72).total_seconds() - 1)
    env.run(invitations.begin_activation(_token(expired.link)))
    env.advance(1)
    messages = set()
    for token in (
        _token(used.link),
        _token(cancelled.link),
        _token(expired.link),
        "A" * 43,
        "corto",
        "",
    ):
        with pytest.raises(IdentityRejected) as raised:
            env.run(
                invitations.accept_invitation(token, GOOD_PASSWORD, CURRENT_PRIVACY_NOTICE_VERSION)
            )
        assert raised.value.code is IdentityRejection.INVITATION_INVALID
        messages.add(str(raised.value))
    assert len(messages) == 1


def test_activation_checks_password_notice_and_second_factor(env: HierarchyEnvironment) -> None:
    org = Organization(env)
    invitations = env.invitations()
    outcome = _invite(env, org, new_email(), role=Role.ADMINISTRATOR)
    token = _token(outcome.link)
    start = env.run(invitations.begin_activation(token))
    assert start.second_factor_required and start.enrollment is not None
    assert start.notice.version == CURRENT_PRIVACY_NOTICE_VERSION
    assert start.notice.pending_legal_text and "PENDIENTE DEL ABOGADO" in start.notice.text
    for password, version, code, expected in (
        (REJECTED_PASSWORD, CURRENT_PRIVACY_NOTICE_VERSION, GOOD_CODE, "password_rejected"),
        (GOOD_PASSWORD, "v0-anterior", GOOD_CODE, "privacy_notice_outdated"),
        (GOOD_PASSWORD, CURRENT_PRIVACY_NOTICE_VERSION, None, "second_factor_required"),
        (GOOD_PASSWORD, CURRENT_PRIVACY_NOTICE_VERSION, "000000", "second_factor_required"),
    ):
        with pytest.raises(IdentityRejected) as raised:
            env.run(invitations.accept_invitation(token, password, version, code))
        assert raised.value.code.value == expected
        (user,) = env.fetch(
            "SELECT status FROM identity.user_account WHERE user_id = $1", outcome.user_id
        )
        assert user["status"] == "invited"
    activated = env.run(
        invitations.accept_invitation(
            token, GOOD_PASSWORD, CURRENT_PRIVACY_NOTICE_VERSION, GOOD_CODE
        )
    )
    assert activated.user_id == outcome.user_id
    (credential,) = env.fetch(
        "SELECT password_hash FROM identity.password_credential WHERE user_id = $1",
        outcome.user_id,
    )
    assert credential["password_hash"] == f"fake${GOOD_PASSWORD}"
    assert [
        row
        for row in _audit(env, org.organization_id, "user_activated")
        if row["resource_id"] == outcome.user_id
    ]


# --- Desactivación, reactivación y perfil -------------------------------------------------------


def test_deactivation_closes_sessions_and_reactivation_discards_credentials(
    env: HierarchyEnvironment,
) -> None:
    org = Organization(env)
    outcome = _invite(env, org, new_email())
    env.run(
        env.invitations().accept_invitation(
            _token(outcome.link), GOOD_PASSWORD, CURRENT_PRIVACY_NOTICE_VERSION
        )
    )
    cookie = env.authz.open_session(org.organization_id, outcome.user_id)
    env.run(env.users().deactivate_user(org.admin(), outcome.user_id))
    with pytest.raises(ContextUnavailable):
        env.run(env.authz.contexts.context_from_session(cookie))
    rows = env.fetch(
        "SELECT removed_at FROM identity.role_assignment WHERE user_id = $1", outcome.user_id
    )
    assert rows and all(row["removed_at"] is not None for row in rows)
    with pytest.raises(IdentityRejected) as again:
        env.run(env.users().deactivate_user(org.admin(), outcome.user_id))
    assert again.value.code is IdentityRejection.USER_STATE
    reinvited = env.run(
        env.users().reactivate_user(
            org.admin(),
            outcome.user_id,
            (AssignmentRequest(Role.COPASST, ScopeLevel.PLANT, org.plant_id),),
        )
    )
    (user,) = env.fetch(
        "SELECT u.status, u.privacy_notice_version_accepted, c.password_hash"
        " FROM identity.user_account u JOIN identity.password_credential c USING (user_id)"
        " WHERE user_id = $1",
        outcome.user_id,
    )
    assert tuple(user) == ("invited", None, "!discarded")
    env.run(
        env.invitations().accept_invitation(
            _token(reinvited.link), GOOD_PASSWORD, CURRENT_PRIVACY_NOTICE_VERSION
        )
    )
    assert [r["operation"] for r in _audit(env, org.organization_id, "user_reactivated")]


def test_profile_change_is_audited_without_values(env: HierarchyEnvironment) -> None:
    org = Organization(env)
    users = env.users()
    env.run(
        users.update_profile(
            org.admin(),
            org.admin_id,
            ProfileChange(display_name="Nombre nuevo sintético", professional_license="LIC-123"),
        )
    )
    (row,) = [
        r
        for r in _audit(env, org.organization_id, "user_profile_changed")
        if r["resource_id"] == org.admin_id
    ]
    filters = bytes(row["filters"]).decode()
    assert "display_name" in filters and "professional_license" in filters
    assert "Nombre nuevo" not in filters and "LIC-123" not in filters
    with pytest.raises(IdentityRejected) as raised:
        env.run(
            users.update_profile(org.admin(), org.admin_id, ProfileChange(display_name="<b>x</b>"))
        )
    assert raised.value.code is IdentityRejection.FREE_TEXT_REJECTED


# --- Jerarquía y nodos --------------------------------------------------------------------------


def test_plants_zones_and_nodes_keep_one_node_per_zone(env: HierarchyEnvironment) -> None:
    org = Organization(env)
    hierarchy = env.hierarchy()
    admin = org.admin()
    other = env.run(hierarchy.create_plant(admin, _plant()))
    zone = env.run(hierarchy.create_zone(admin, org.plant_id, ZoneSpec(new_code("ZN"), "Prensas")))
    with pytest.raises(IdentityRejected) as taken:
        env.run(hierarchy.create_zone(admin, org.plant_id, ZoneSpec(zone.code, "Otra")))
    assert taken.value.code is IdentityRejection.CODE_TAKEN
    node = env.run(hierarchy.declare_node(admin, org.plant_id, new_code("ND")))
    foreign = env.run(hierarchy.declare_node(admin, other.plant_id, new_code("ND")))
    with pytest.raises(IdentityRejected) as mismatch:
        env.run(hierarchy.assign_node_to_zone(admin, foreign.node_id, zone.zone_id))
    assert mismatch.value.code is IdentityRejection.NODE_PLANT_MISMATCH
    env.run(hierarchy.assign_node_to_zone(admin, node.node_id, zone.zone_id))
    with pytest.raises(IdentityRejected) as busy:
        env.run(hierarchy.assign_node_to_zone(admin, node.node_id, zone.zone_id))
    assert busy.value.code is IdentityRejection.ZONE_HAS_NODE
    assert env.run(hierarchy.assigned_node(admin, zone.zone_id)).node_id == node.node_id
    env.advance(1)
    env.run(hierarchy.unassign_node(admin, zone.zone_id))
    assert env.run(hierarchy.assigned_node(admin, zone.zone_id)) is None
    with pytest.raises(IdentityRejected) as empty:
        env.run(hierarchy.unassign_node(admin, zone.zone_id))
    assert empty.value.code is IdentityRejection.ZONE_WITHOUT_NODE
    updated = env.run(
        hierarchy.update_node(
            admin, node.node_id, "re_enrollment_pending", "https://nodo-1.local:8443/"
        )
    )
    assert (updated.status, updated.live_view_local_url) == (
        "re_enrollment_pending",
        "https://nodo-1.local:8443/",
    )
    cleared = env.run(hierarchy.update_node(admin, node.node_id, "enrolled", None))
    assert cleared.live_view_local_url is None
    for bad in ("http://nodo.local:8443/", "https://nodo.local/", "https://nodo.local:8443/?x"):
        with pytest.raises(IdentityRejected):
            env.run(hierarchy.update_node(admin, node.node_id, "enrolled", bad))
    records = [
        row["record_type"]
        for row in env.fetch(
            "SELECT record_type FROM ledger.ledger_record WHERE organization_id = $1"
            " AND plant_id = $2 ORDER BY chain_sequence",
            org.organization_id,
            org.plant_id,
        )
    ]
    assert records == [
        "plant_created",
        "zone_created",
        "node_declared",
        "node_zone_assigned",
        "node_zone_unassigned",
    ]


def test_hierarchy_view_follows_the_session_scope(env: HierarchyEnvironment) -> None:
    org = Organization(env)
    hierarchy = env.hierarchy()
    admin = org.admin()
    second = env.run(hierarchy.create_plant(admin, _plant()))
    zone = env.run(hierarchy.create_zone(admin, second.plant_id, ZoneSpec(new_code("ZN"), "Z")))
    user = env.authz.sessions.add_user(org.organization_id).user_id
    env.authz.assign(org.organization_id, user, Role.COPASST, ScopeLevel.ZONE, zone.zone_id)
    view = env.run(hierarchy.hierarchy(env.session_context(org.organization_id, user)))
    assert [p.plant_id for p in view.plants] == [second.plant_id]
    assert [z.zone_id for z in view.plants[0].zones] == [zone.zone_id]
    full = env.run(hierarchy.hierarchy(admin))
    assert {p.plant_id for p in full.plants} == {org.plant_id, second.plant_id}
    recipients = env.run(
        hierarchy.users_by_role_and_scope(
            admin, [Role.COPASST, Role.ADMINISTRATOR], ScopeLevel.ZONE, zone.zone_id
        )
    )
    assert {(r.user_id, r.role) for r in recipients} == {
        (user, Role.COPASST),
        (org.admin_id, Role.ADMINISTRATOR),
    }


def test_plant_validation_borders(env: HierarchyEnvironment) -> None:
    org = Organization(env)
    hierarchy = env.hierarchy()
    admin = org.admin()
    base = _plant()
    for field_name, value in (
        ("country", "co"),
        ("country", "COL"),
        ("timezone", "America/Bogota/Extra/Mas"),
        ("timezone", "../etc"),
        ("data_region", "eu-west-9"),
        ("code", "a"),
        ("code", "X" * 33),
        ("name", ""),
        ("name", "x" * 121),
    ):
        with pytest.raises(IdentityRejected):
            env.run(hierarchy.create_plant(admin, _replace(base, field_name, value)))
    accepted = env.run(hierarchy.create_plant(admin, _replace(base, "name", "x" * 120)))
    assert len(accepted.name) == 120


def _replace(spec: PlantSpec, name: str, value: str) -> PlantSpec:
    values = {
        "code": spec.code,
        "name": spec.name,
        "country": spec.country,
        "data_region": spec.data_region,
        "timezone": spec.timezone,
    }
    values[name] = value
    return PlantSpec(**values)


# --- nuc_0009 -----------------------------------------------------------------------------------


def test_database_rejects_an_assignment_scoped_to_another_organization(
    env: HierarchyEnvironment,
) -> None:
    first, second = Organization(env), Organization(env)

    async def insert(level: str, scope_id: uuid.UUID) -> None:
        connection = await env.authz.sessions.migrated.connect("vigia_app")
        try:
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('vigia.organization_id', $1, true)",
                    str(first.organization_id),
                )
                await connection.execute(
                    "INSERT INTO identity.role_assignment (assignment_id, organization_id,"
                    " user_id, role, scope_level, scope_id, assigned_at, assigned_by)"
                    " VALUES ($1, $2, $3, 'copasst', $4, $5, $6, $3)",
                    uuid.uuid4(),
                    first.organization_id,
                    first.admin_id,
                    level,
                    scope_id,
                    BASE_TIME,
                )
        finally:
            await connection.close()

    zone = env.run(
        env.hierarchy().create_zone(second.admin(), second.plant_id, ZoneSpec(new_code("ZN"), "Z"))
    )
    for level, scope_id in (
        ("plant", second.plant_id),
        ("zone", zone.zone_id),
        ("plant", uuid.uuid4()),
    ):
        with pytest.raises(asyncpg.exceptions.CheckViolationError, match="no es de su"):
            env.run(insert(level, scope_id))
    env.run(insert("plant", first.plant_id))
