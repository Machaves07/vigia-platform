"""Motor ``notification_engine`` contra PostgreSQL 16 real como ``vigia_app`` (TASK-324; LC-LAZ-31;
BR-LAZ-124, 127 a 130 y 132; PAT-LAZ-RES-04; A-58 y A-69).

Sobre ``NotificationWorld`` (``tests/notification_support.py``): el ``IdentityQueryPort`` real, la
política de texto libre real y los contextos del sistema reales.

- **Destinatarios** (BR-LAZ-124): exactamente los usuarios activos con un rol de la fila sobre un
  alcance que contiene la zona; ni otra zona, ni otra planta, ni otro rol, ni otra organización, ni
  una cuenta desactivada o una asignación retirada. Las personas del evento (responsable,
  solicitante), solo con un rol sobre la zona. ``adoption_low`` sube al gerente sin coordinador.
- **Entrega**: el aviso en la aplicación de inmediato (``in_app`` ``sent``); ``normal`` deja el
  correo ``pending`` para la agrupación sin ``EmailMessage``; ``immediate`` encola un
  ``EmailMessage`` por destinatario; sin ``EmailSenderPort`` o con el circuito abierto, todo queda
  ``degraded_app_only`` y visible en ``GET /notifications`` (``email_status``); nada se pierde.
  ``integrity_compromised`` encola su correo igualmente, para el cliente y para el proveedor
  (BR-LAZ-127).
- **Proveedor**: las alarmas de flota y las alertas de la plataforma solo a la proveedora; la
  concesión concedida a los administradores y gerentes del cliente (BR-LAZ-132).
- **Sin respuesta de la identidad**: la entrega falla, no queda ninguna fila y el reintento entrega.
- **Bajo concesión** (A-69): la consulta acotada y ni una fila en ``user_notification_preference``,
  ``digest_batch`` ni ``email_message``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any, Final

import pytest

from tests.factories import uuid7
from tests.integration.conftest import PostgresEndpoint
from tests.notification_support import (
    Member,
    NotificationWorld,
    Senders,
    StubRecords,
    StubSender,
    notification_world,
)
from vigia_platform.identity.authz.authorize import Authorizer, ResourceNotFound
from vigia_platform.notifications.delivery import (
    CONCESSION_CONTEXT,
    EMAIL_CIRCUIT_OPEN,
    EMAIL_SENDER_ABSENT,
)
from vigia_platform.notifications.reader import NotificationReader
from vigia_platform.shared.context import (
    Actor,
    ActorKind,
    ActorUnit,
    AllowedScope,
    ContextOrigin,
    Role,
    ScopeContext,
    ScopeLevel,
    _seal_scope_context,
)

pytestmark = pytest.mark.integration

C, PM, LM, AD = Role.COORDINATOR_SST, Role.PLANT_MANAGER, Role.LINE_MANAGER, Role.ADMINISTRATOR
ORG, PLANT, ZONE = ScopeLevel.ORGANIZATION, ScopeLevel.PLANT, ScopeLevel.ZONE
A69_TABLES: Final = ("user_notification_preference", "digest_batch", "email_message")


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[NotificationWorld]:
    with notification_world(postgres_endpoint, "notification_engine") as built:
        yield built


def _subject(zone_id: uuid.UUID, **extra: Any) -> dict[str, Any]:
    return {
        "zone_id": str(zone_id),
        "subject_kind": "finding",
        "subject_id": str(uuid7()),
        "record_id": str(uuid7()),
        **extra,
    }


def _recipients(rows: list[dict[str, Any]]) -> set[uuid.UUID]:
    return {row["recipient_user_id"] for row in rows}


class Site:
    """Una organización nueva con dos plantas y tres zonas, y su gente."""

    def __init__(self, world: NotificationWorld) -> None:
        self.organization, self.plant, self.zone = world.new_organization()
        self.other_zone = world.add_zone(self.organization, self.plant)
        self.other_plant, self.far_zone = world.add_plant(self.organization)


# --- destinatarios ---


def test_only_users_whose_scope_contains_the_zone_receive_it(world: NotificationWorld) -> None:
    site = Site(world)
    org = site.organization
    zone_coordinator = world.add_member(org, C, ZONE, site.zone)
    plant_coordinator = world.add_member(org, C, PLANT, site.plant)
    org_manager = world.add_member(org, PM, ORG, org)
    expected = {m.user_id for m in (zone_coordinator, plant_coordinator, org_manager)}
    # Ninguno de estos lo recibe.
    world.add_member(org, C, ZONE, site.other_zone)
    world.add_member(org, C, PLANT, site.other_plant)
    world.add_member(org, PM, ZONE, site.far_zone)
    world.add_member(org, LM, ZONE, site.zone)
    world.add_member(org, C, ZONE, site.zone, status="deactivated")
    world.add_member(org, C, ZONE, site.zone, removed=True)
    other_org, _, _ = world.new_organization()
    world.add_member(other_org, C, ORG, other_org)
    event = world.event(org, "finding_confirmed", _subject(site.zone), plant_id=site.plant)
    engine = world.engine()
    assert world.deliver(engine, event) == len(expected)
    rows = world.notifications(event.event_id)
    assert _recipients(rows) == expected
    assert {row["organization_id"] for row in rows} == {org}
    assert {(row["plant_id"], row["zone_id"]) for row in rows} == {(site.plant, site.zone)}
    row = rows[0]
    assert row["urgency"] == "normal" and row["event_name"] == "finding_confirmed"
    assert row["deep_link_ref"] == {"kind": "finding", "id": event.payload["subject_id"]}
    assert row["title_es"] == "Hallazgo confirmado"
    # Aplicación de inmediato; el correo agrupado queda para TASK-348, sin EmailMessage.
    deliveries = world.deliveries(event.event_id)
    assert {(d["channel"], d["status"]) for d in deliveries} == {
        ("in_app", "sent"),
        ("email", "pending"),
    }
    assert len(deliveries) == 2 * len(expected)
    assert all(d["digest_batch_id"] is None for d in deliveries)
    assert world.emails(event.event_id) == []
    # La reentrega no crea nada (BR-LAZ-129).
    assert world.deliver(engine, event) == 0
    assert world.notifications(event.event_id) == rows


def test_a_plant_event_does_not_reach_a_zone_scoped_user(world: NotificationWorld) -> None:
    site = Site(world)
    org = site.organization
    plant_admin = world.add_member(org, AD, PLANT, site.plant)
    world.add_member(org, AD, ZONE, site.zone)
    world.add_member(org, AD, PLANT, site.other_plant)
    node = str(uuid.uuid4())
    event = world.event(
        org,
        "node_revoked",
        {"node_id": node, "plant_id": str(site.plant), "zone_ids": [str(site.zone)]},
        plant_id=site.plant,
    )
    assert world.deliver(world.engine(), event) == 1
    (row,) = world.notifications(event.event_id)
    assert row["recipient_user_id"] == plant_admin.user_id
    assert (row["plant_id"], row["zone_id"]) == (site.plant, None)
    assert row["deep_link_ref"] == {"kind": "node", "id": node}


def test_the_assignee_receives_it_only_with_a_role_over_the_zone(world: NotificationWorld) -> None:
    site = Site(world)
    org = site.organization
    coordinator = world.add_member(org, C, ZONE, site.zone)
    assignee = world.add_member(org, LM, ZONE, site.zone)
    outsider = world.add_member(org, LM, ZONE, site.other_zone)
    for person, expected in ((assignee, True), (outsider, False)):
        event = world.event(
            org,
            "action_assigned",
            _subject(
                site.zone,
                action_id=str(uuid7()),
                action_type="engineering",
                assignee_user_id=str(person.user_id),
                due_at="2026-11-01T00:00:00.000Z",
            ),
            plant_id=site.plant,
        )
        world.deliver(world.engine(), event)
        wanted = {coordinator.user_id} | ({person.user_id} if expected else set())
        assert _recipients(world.notifications(event.event_id)) == wanted


def test_the_reopening_reaches_the_assignee_of_the_new_action(world: NotificationWorld) -> None:
    site = Site(world)
    org = site.organization
    coordinator = world.add_member(org, C, PLANT, site.plant)
    assignee = world.add_member(org, LM, ZONE, site.zone)
    action_id = world.seed_action(org, site.plant, site.zone, assignee.user_id)
    event = world.event(
        org,
        "finding_reopened",
        _subject(
            site.zone, reopening_id=str(uuid7()), from_state="closed", new_action_id=str(action_id)
        ),
        plant_id=site.plant,
    )
    assert world.deliver(world.engine(), event) == 2
    assert _recipients(world.notifications(event.event_id)) == {
        coordinator.user_id,
        assignee.user_id,
    }


def test_the_export_reaches_its_requester(world: NotificationWorld) -> None:
    site = Site(world)
    org = site.organization
    requester = world.add_member(org, C, ZONE, site.zone)
    world.add_member(org, C, ZONE, site.zone)
    record_id = uuid7()
    records = StubRecords({record_id: {"requested_by": {"user_id": str(requester.user_id)}}})
    export_id = uuid7()
    event = world.event(
        org,
        "export_available",
        {
            "export_id": str(export_id),
            "record_id": str(record_id),
            "zone_ids": [str(site.zone)],
            "package_form": "without_clips",
            "expires_at": "2026-11-08T00:00:00.000Z",
        },
        plant_id=site.plant,
    )
    assert world.deliver(world.engine(records=records), event) == 1
    (row,) = world.notifications(event.event_id)
    assert row["recipient_user_id"] == requester.user_id
    assert row["deep_link_ref"] == {"kind": "export", "id": str(export_id)}
    assert "2026-11-08" in row["body_es"]


def test_adoption_low_falls_back_to_the_plant_manager_and_copies_the_provider(
    world: NotificationWorld,
) -> None:
    site = Site(world)
    org = site.organization
    manager = world.add_member(org, PM, PLANT, site.plant)
    event = world.event(
        org, "adoption_low", {"zone_id": str(site.zone), "silent_days": 14}, plant_id=site.plant
    )
    world.deliver(world.engine(), event)
    rows = world.notifications(event.event_id)
    by_org = {(row["organization_id"], row["recipient_user_id"]) for row in rows}
    assert {
        (org, manager.user_id),
        (world.provider_id, world.seed.operator_id),
        (world.provider_id, world.seed.installer_id),
    } <= by_org
    assert {user for organization, user in by_org if organization == org} == {manager.user_id}
    provider_rows = [row for row in rows if row["organization_id"] == world.provider_id]
    assert all(row["plant_id"] is None and row["zone_id"] is None for row in provider_rows)
    assert all(row["deep_link_ref"]["kind"] == "zone" for row in provider_rows)
    # Con coordinador en la zona, el gerente ya no lo recibe.
    coordinator = world.add_member(org, C, ZONE, site.zone)
    again = world.event(
        org, "adoption_low", {"zone_id": str(site.zone), "silent_days": 15}, plant_id=site.plant
    )
    world.deliver(world.engine(), again)
    client = {
        r["recipient_user_id"]
        for r in world.notifications(again.event_id)
        if r["organization_id"] == org
    }
    assert client == {coordinator.user_id}


@pytest.mark.parametrize(
    "event_name", ["fleet_alarm_raised", "security_alert", "loop_projection_divergence"]
)
def test_provider_alerts_never_reach_the_client(world: NotificationWorld, event_name: str) -> None:
    site = Site(world)
    org = site.organization
    for role in (AD, C, PM):
        world.add_member(org, role, ORG, org)
    payloads = {
        "fleet_alarm_raised": {
            "alarm_id": str(uuid7()),
            "alarm_kind": "node_mute",
            "zone_id": str(site.zone),
            "since": "2026-10-01T00:00:00.000Z",
        },
        "security_alert": {"alert_kind": "csrf_rejected", "occurred_at": "2026-10-01T00:00:00Z"},
        "loop_projection_divergence": {
            "rows_checked": 10,
            "rows_diverged": 2,
            "full_scan": False,
            "divergence_count": 2,
        },
    }
    event = world.event(org, event_name, payloads[event_name], plant_id=site.plant)
    world.deliver(world.engine(), event)
    rows = world.notifications(event.event_id)
    assert rows and {row["organization_id"] for row in rows} == {world.provider_id}
    assert world.seed.operator_id in _recipients(rows)
    assert all(row["urgency"] == "immediate" for row in rows)


def test_a_granted_concession_notifies_the_client_administrators_and_managers(
    world: NotificationWorld,
) -> None:
    site = Site(world)
    org = site.organization
    administrator = world.add_member(org, AD, ORG, org)
    manager = world.add_member(org, PM, PLANT, site.plant)
    world.add_member(org, PM, PLANT, site.other_plant)
    world.add_member(org, C, PLANT, site.plant)
    event = world.event(
        org,
        "concession_granted",
        {
            "concession_id": str(uuid.uuid4()),
            "provider_organization_id": str(world.provider_id),
            "scope_level": "plant",
            "scope_id": str(site.plant),
            "granted_by": str(world.seed.installer_id),
            "expires_at": "2026-10-08T00:00:00.000Z",
        },
    )
    world.deliver(world.engine(), event)
    rows = world.notifications(event.event_id)
    assert _recipients(rows) == {administrator.user_id, manager.user_id}
    assert {row["organization_id"] for row in rows} == {org}
    assert {row["deep_link_ref"]["kind"] for row in rows} == {"organization"}
    assert len(world.emails(event.event_id)) == 2


# --- correo y degradación ---


def _overdue(world: NotificationWorld, site: Site) -> Any:
    return world.event(
        site.organization,
        "review_overdue",
        {
            "zone_id": str(site.zone),
            "detection_id": str(uuid7()),
            "review_due_at": "2026-10-01T00:00:00.000Z",
            "reminders_sent": 2,
        },
        plant_id=site.plant,
    )


def test_an_immediate_event_queues_one_email_per_recipient(world: NotificationWorld) -> None:
    site = Site(world)
    members = [world.add_member(site.organization, C, ZONE, site.zone) for _ in range(2)]
    event = _overdue(world, site)
    sender = StubSender("closed")
    engine = world.engine(senders=Senders(sender))
    assert world.deliver(engine, event) == 2
    emails = world.emails(event.event_id)
    assert {email["to_user_id"] for email in emails} == {m.user_id for m in members}
    assert {(e["kind"], e["status"]) for e in emails} == {("notification", "queued")}
    notifications = {r["notification_id"]: r for r in world.notifications(event.event_id)}
    for email in emails:
        (related,) = email["related_ids"]
        assert notifications[related]["recipient_user_id"] == email["to_user_id"]
        assert email["subject_es"] == notifications[related]["title_es"]
    assert {(d["channel"], d["status"]) for d in world.deliveries(event.event_id)} == {
        ("in_app", "sent"),
        ("email", "pending"),
    }
    assert sender.checks == 1  # el circuito se mira una vez por evento
    assert world.deliver(engine, event) == 0
    assert len(world.emails(event.event_id)) == 2


@pytest.mark.parametrize(
    ("senders", "failure_code"),
    [
        (Senders(None), EMAIL_SENDER_ABSENT),
        (Senders(StubSender("open")), EMAIL_CIRCUIT_OPEN),
    ],
    ids=["sin_puerto", "circuito_abierto"],
)
def test_without_email_everything_stays_in_the_app_and_is_declared(
    world: NotificationWorld, senders: Senders, failure_code: str
) -> None:
    site = Site(world)
    member = world.add_member(site.organization, C, ZONE, site.zone)
    event = _overdue(world, site)
    assert world.deliver(world.engine(senders=senders), event) == 1
    assert world.emails(event.event_id) == []
    email = [d for d in world.deliveries(event.event_id) if d["channel"] == "email"]
    assert [(d["status"], d["failure_code"]) for d in email] == [
        ("degraded_app_only", failure_code)
    ]
    # Visible en la lista propia del usuario, con la degradación declarada (BR-LAZ-130).
    page = world.run(_reader(world).list_own(_session(world, member)))
    assert [(item.event_name, item.email_status) for item in page.items] == [
        ("review_overdue", "degraded_app_only")
    ]


def test_half_open_still_queues_the_email(world: NotificationWorld) -> None:
    site = Site(world)
    world.add_member(site.organization, C, ZONE, site.zone)
    event = _overdue(world, site)
    world.deliver(world.engine(senders=Senders(StubSender("half_open"))), event)
    assert len(world.emails(event.event_id)) == 1


@pytest.mark.parametrize(
    "senders", [Senders(None), Senders(StubSender("open"))], ids=["sin_puerto", "abierto"]
)
def test_integrity_compromised_always_queues_its_email_for_both(
    world: NotificationWorld, senders: Senders
) -> None:
    site = Site(world)
    org = site.organization
    administrator = world.add_member(org, AD, ORG, org)
    world.add_member(org, C, ORG, org)
    event = world.event(
        org,
        "integrity_compromised",
        {
            "chain_kind": "ledger",
            "first_failed_sequence": 7,
            "verification_mode": "full",
            "detected_at": "2026-10-01T00:00:00.000Z",
        },
    )
    world.deliver(world.engine(senders=senders), event)
    rows = world.notifications(event.event_id)
    client = {r["recipient_user_id"] for r in rows if r["organization_id"] == org}
    provider = {r["recipient_user_id"] for r in rows if r["organization_id"] == world.provider_id}
    assert client == {administrator.user_id}
    assert world.seed.operator_id in provider
    emails = world.emails(event.event_id)
    assert {email["to_user_id"] for email in emails} == client | provider
    assert all(d["status"] != "degraded_app_only" for d in world.deliveries(event.event_id))


# --- sin respuesta de la identidad ---


class _Unanswered:
    """``IdentityQueryPort`` que no responde (la consulta de destinatarios falla)."""

    def __init__(self) -> None:
        self.calls = 0

    async def users_by_role_and_scope(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        raise ConnectionError("identidad sin respuesta")

    async def recipients_under_concession(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        raise ConnectionError("identidad sin respuesta")


@pytest.mark.parametrize("event_name", ["review_overdue", "integrity_compromised"])
def test_without_identity_nothing_is_delivered_and_the_event_retries(
    world: NotificationWorld, event_name: str
) -> None:
    site = Site(world)
    member = world.add_member(
        site.organization, AD if "integrity" in event_name else C, ORG, site.organization
    )
    event = (
        _overdue(world, site)
        if event_name == "review_overdue"
        else world.event(
            site.organization,
            event_name,
            {
                "chain_kind": "ledger",
                "first_failed_sequence": 1,
                "verification_mode": "full",
                "detected_at": "2026-10-01T00:00:00.000Z",
            },
        )
    )
    unanswered = _Unanswered()
    with pytest.raises(ConnectionError):
        world.deliver(world.engine(directory=unanswered), event)
    assert unanswered.calls >= 1
    assert world.notifications(event.event_id) == []
    assert world.deliveries(event.event_id) == []
    assert world.emails(event.event_id) == []
    # El reintento, con la identidad de vuelta, entrega.
    assert world.deliver(world.engine(), event) >= 1
    assert member.user_id in _recipients(world.notifications(event.event_id))


# --- bajo concesión (A-58, A-69) ---


class _Spy:
    """El ``IdentityQueryPort`` real, contando qué consulta se usó."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.calls: list[str] = []

    async def users_by_role_and_scope(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append("users_by_role_and_scope")
        return await self.inner.users_by_role_and_scope(*args, **kwargs)

    async def recipients_under_concession(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append("recipients_under_concession")
        return await self.inner.recipients_under_concession(*args, **kwargs)


def test_under_a_concession_the_bounded_query_is_used_and_no_org_only_row_appears(
    world: NotificationWorld,
) -> None:
    site = Site(world)
    org = site.organization
    zone_coordinator = world.add_member(org, C, ZONE, site.zone)
    org_manager = world.add_member(org, PM, ORG, org)
    world.add_member(org, C, ZONE, site.other_zone)
    concession = world.plant_concession(org, site.plant)
    before = {table: world.count(table, org) for table in A69_TABLES}
    spy = _Spy(world.directory)
    engine = world.engine(directory=spy)
    normal = world.event(org, "finding_confirmed", _subject(site.zone), plant_id=site.plant)
    assert world.deliver(engine, normal, context=concession) == 2
    assert _recipients(world.notifications(normal.event_id)) == {
        zone_coordinator.user_id,
        org_manager.user_id,
    }
    immediate = _overdue(world, site)
    assert world.deliver(engine, immediate, context=concession) == 1
    assert set(spy.calls) == {"recipients_under_concession"}
    email = [d for d in world.deliveries(immediate.event_id) if d["channel"] == "email"]
    assert [(d["status"], d["failure_code"]) for d in email] == [
        ("degraded_app_only", CONCESSION_CONTEXT)
    ]
    assert {table: world.count(table, org) for table in A69_TABLES} == before


def test_under_a_concession_another_plant_is_not_found(world: NotificationWorld) -> None:
    site = Site(world)
    org = site.organization
    world.add_member(org, C, ORG, org)
    concession = world.plant_concession(org, site.plant)
    event = world.event(
        org, "finding_confirmed", _subject(site.far_zone), plant_id=site.other_plant
    )
    with pytest.raises(ResourceNotFound):
        world.deliver(world.engine(), event, context=concession)
    assert world.notifications(event.event_id) == []


def test_the_bounded_query_returns_only_user_and_role(world: NotificationWorld) -> None:
    site = Site(world)
    org = site.organization
    member = world.add_member(org, C, PLANT, site.plant)
    world.add_member(org, C, PLANT, site.other_plant)
    concession = world.plant_concession(org, site.plant)
    found = world.run(world.directory.recipients_under_concession(concession, [C], ZONE, site.zone))
    assert [(r.user_id, r.role) for r in found] == [(member.user_id, C)]
    # La RLS de A-46 no se amplió: la consulta abierta, con el contexto de la concesión, no ve a
    # nadie (``identity.user_account`` oculta); solo la acotada responde, y solo usuario y rol.
    assert (
        world.run(world.directory.users_by_role_and_scope(concession, [C], ZONE, site.zone)) == ()
    )
    with pytest.raises(ResourceNotFound):
        world.run(world.directory.recipients_under_concession(concession, [C], ORG, org))


# --- lista propia (SUP-U05-33, forma mínima) ---


class _DenialAudit:
    """``AuthorizationAudit`` que guarda las denegaciones."""

    def __init__(self) -> None:
        self.denied: list[tuple[uuid.UUID, str]] = []

    async def authorization_denied(self, context: Any, key: Any, resource: Any) -> None:
        self.denied.append((context.organization_id, str(key)))


def _reader(world: NotificationWorld, audit: _DenialAudit | None = None) -> NotificationReader:
    return NotificationReader(
        database=world.database,
        authorizer=Authorizer(
            audit=audit or _DenialAudit(), provider_organization_id=world.provider_id
        ),
        provider_organization_id=world.provider_id,
    )


def test_under_a_concession_the_list_is_denied_and_audited(world: NotificationWorld) -> None:
    site = Site(world)
    concession = world.plant_concession(site.organization, site.plant)
    audit = _DenialAudit()
    with pytest.raises(ResourceNotFound):
        world.run(_reader(world, audit).list_own(concession))
    assert audit.denied == [(site.organization, "notifications.read")]


def _session(world: NotificationWorld, member: Member) -> ScopeContext:
    return _seal_scope_context(
        organization_id=member.organization_id,
        actor=Actor(
            kind=ActorKind.USER,
            id=member.user_id,
            display_name_snapshot="Persona sintética",
            unit=ActorUnit.U02,
            role_in_use=member.role,
        ),
        origin=ContextOrigin.SESSION,
        allowed_scopes=[AllowedScope(member.scope_level, member.scope_id, member.role)],
        correlation_id=uuid7(),
        session_id_hash=uuid.uuid4().hex * 2,
    )


def test_the_list_shows_only_the_actors_own_notifications_by_key(world: NotificationWorld) -> None:
    site = Site(world)
    org = site.organization
    mine = world.add_member(org, C, PLANT, site.plant)
    colleague = world.add_member(org, C, PLANT, site.plant)
    engine = world.engine()
    events = []
    for _ in range(5):
        world.clock.advance(1)
        event = world.event(org, "finding_confirmed", _subject(site.zone), plant_id=site.plant)
        world.deliver(engine, event)
        events.append(event)
    reader = _reader(world)
    seen: list[uuid.UUID] = []
    after = None
    while True:
        page = world.run(reader.list_own(_session(world, mine), after=after, limit=2))
        seen += [item.notification_id for item in page.items]
        after = page.next_cursor
        if after is None:
            break
    rows = world.fetch(
        "SELECT notification_id, recipient_user_id, created_at FROM notifications.notification"
        " WHERE organization_id = $1 ORDER BY created_at DESC, notification_id DESC",
        org,
    )
    assert seen == [r["notification_id"] for r in rows if r["recipient_user_id"] == mine.user_id]
    assert len(seen) == 5 and len(set(seen)) == 5
    theirs = world.run(reader.list_own(_session(world, colleague)))
    assert not set(seen) & {item.notification_id for item in theirs.items}
    assert len(theirs.items) == 5
