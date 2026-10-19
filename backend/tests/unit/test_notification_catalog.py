"""El catálogo de eventos de las notificaciones contra la tabla del diseño (TASK-324; LC-LAZ-30;
business-logic-model.md de U-04 §7.2 con sus notas U04-H-06 y U04-H-13; domain-entities §6.1).

``EXPECTED`` es la tabla de §7.2 **copiada a mano**, fila a fila, con sus notas ya aplicadas
(D-10: las alarmas de flota solo al proveedor; ``user_invited`` fuera); no se deriva del código.
La igualdad es exacta: un evento de más o de menos, una urgencia, un canal, un rol o una audiencia
distinta hacen fallar la prueba. Además: ninguna fila ``immediate`` es silenciable (BR-LAZ-126), las
urgencias ``[hipótesis]`` son exactamente las de los eventos de U-03 (U04-H-12), solo
``integrity_compromised`` salta el cortacircuito (BR-LAZ-127), cada evento suscrito está
registrado en la bandeja con la unidad que dice la fila y cada fila tiene su plantilla.
"""

from __future__ import annotations

from typing import Final

import pytest

from vigia_platform.accreditation.events import register_accreditation_event_types
from vigia_platform.catalog.events import register_catalog_event_types
from vigia_platform.fleet.domain.enums import FleetAlarmKind
from vigia_platform.fleet.events import register_fleet_event_types
from vigia_platform.identity.application.roles import CLIENT_ROLES as IDENTITY_CLIENT_ROLES
from vigia_platform.identity.application.roles import PROVIDER_ROLES
from vigia_platform.loop.domain.enums import NotificationAudience, NotificationUrgency
from vigia_platform.loop.events import register_loop_event_types
from vigia_platform.notifications.catalog import (
    CATALOG,
    CLIENT_ROLES,
    PROVIDER_STAFF,
    PersonRef,
    catalog_entry,
    subscribed_events,
)
from vigia_platform.notifications.templates import template_keys
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.outbox.registries import MAX_SUBSCRIPTIONS, EventTypeRegistry
from vigia_platform.shared.outbox.u02_events import register_u02_event_types

# Columnas: (unidad, urgencia, correo, roles del cliente, personas, roles del proveedor,
# audiencia). «correo» es «aplicación, correo» o «aplicación, correo agrupado».
C, PM, LM, AD = "coordinator_sst", "plant_manager", "line_manager", "administrator"
OP = "platform_operator"
PROVIDER: Final = frozenset({OP, "provider_installer"})
"""«Proveedor» a secas: los dos roles de la proveedora (el ``administrator`` no es asignable en
ella, así que «administrador de la proveedora» es ``platform_operator``)."""
ASSIGNEE, REQUESTER = "assignee", "requester"
N, IM, D = "normal", "immediate", "digest"

Row = tuple[str, str, bool, frozenset[str], frozenset[str], frozenset[str], str]


def _row(
    unit: str,
    urgency: str,
    email: bool,
    client: tuple[str, ...] = (),
    persons: tuple[str, ...] = (),
    provider: frozenset[str] | tuple[str, ...] = (),
    audience: str = "client",
) -> Row:
    return (
        unit,
        urgency,
        email,
        frozenset(client),
        frozenset(persons),
        frozenset(provider),
        audience,
    )


EXPECTED: Final[dict[str, Row]] = {
    "finding_received": _row("U-03", N, True, (C,)),
    "detection_for_review_received": _row("U-03", N, True, (C,)),
    "finding_confirmed": _row("U-04", N, True, (C, PM)),
    "finding_discarded": _row("U-04", N, False, (C,)),
    "finding_reclassified": _row("U-04", N, False, (C,)),
    "loop_suspended": _row("U-04", N, False, (C,)),
    "loop_resumed": _row("U-04", N, False, (C,)),
    "review_confirmed": _row("U-04", N, False, (C,)),
    "review_discarded": _row("U-04", N, False, (C,)),
    "review_overdue": _row("U-04", IM, True, (C,)),
    "action_assigned": _row("U-04", N, True, (C,), (ASSIGNEE,)),
    "action_evidence_added": _row("U-04", N, False, (C,)),
    "finding_closed": _row("U-04", N, True, (C, PM)),
    "finding_unclosable": _row("U-04", IM, True, (C, PM)),
    "unclosable_review_due": _row("U-04", IM, True, (C, PM)),
    "unclosable_reviewed": _row("U-04", N, False, (C, PM)),
    "finding_reopened": _row("U-04", N, True, (C,), (ASSIGNEE,)),
    "finding_overdue": _row("U-04", IM, True, (C,), (ASSIGNEE,)),
    "finding_escalated": _row("U-04", IM, True, (C,)),
    "root_cause_analysis_signed": _row("U-04", N, False, (C,)),
    "export_available": _row("U-04", N, True, (), (REQUESTER,)),
    "monthly_package_available": _row("U-04", N, True, (LM, C, PM)),
    "commitments_recorded": _row("U-04", N, False, (C, PM)),
    "adoption_low": _row("U-04", IM, True, (C,), (), PROVIDER, "both"),
    "vocabulary_list_changed": _row("U-04", N, False, (AD,)),
    "late_segment_attached": _row("U-04", N, False, (C,)),
    "loop_projection_divergence": _row("U-04", IM, True, (), (), (OP,), "provider"),
    "zone_activated": _row("U-03", N, True, (C, AD)),
    "catalog_updated": _row("U-03", N, False, (C, AD)),
    "gate_state_changed": _row("U-03", N, True, (C, PM, AD)),
    "regression_marked": _row("U-03", N, True, (C, AD)),
    "regression_cleared": _row("U-03", N, False, (C, AD)),
    "node_enrolled": _row("U-03", N, False, (AD,)),
    "node_revoked": _row("U-03", N, False, (AD,)),
    "node_decommissioned": _row("U-03", N, False, (AD,)),
    "target_version_published": _row("U-03", D, False, (AD,)),
    "update_result_received": _row("U-03", D, False, (AD,)),
    # D-10 (U04-H-06): ya no al administrador del cliente; audiencia provider.
    "fleet_alarm_raised": _row("U-03", N, True, (), (), PROVIDER, "provider"),
    "fleet_alarm_cleared": _row("U-03", N, False, (), (), PROVIDER, "provider"),
    "user_activated": _row("U-02", N, False, (AD,)),
    "user_deactivated": _row("U-02", N, False, (AD,)),
    "role_assignment_changed": _row("U-02", N, False, (AD,)),
    "concession_granted": _row("U-02", IM, True, (AD, PM)),
    "concession_revoked": _row("U-02", IM, True, (AD, PM)),
    "concession_expired": _row("U-02", IM, True, (AD, PM)),
    "security_alert": _row("U-02", IM, True, (), (), (OP,), "provider"),
    "dead_letter_created": _row("U-02", IM, True, (), (), (OP,), "provider"),
    "key_rotation_due": _row("U-02", IM, True, (), (), (OP,), "provider"),
    "integrity_compromised": _row("U-02", IM, True, (AD,), (), PROVIDER, "both"),
    "checkpoint_written": _row("U-02", D, False, (), (), (OP,), "provider"),
    "key_set_published": _row("U-02", D, False, (), (), (OP,), "provider"),
}
"""``business-logic-model.md`` de U-04 §7.2, con sus dos notas fechadas del 2026-09-23."""

HYPOTHESIS: Final = frozenset(
    {
        "zone_activated",
        "catalog_updated",
        "gate_state_changed",
        "regression_marked",
        "regression_cleared",
        "node_enrolled",
        "node_revoked",
        "node_decommissioned",
        "target_version_published",
        "update_result_received",
        "fleet_alarm_raised",
        "fleet_alarm_cleared",
    }
)
"""Las urgencias marcadas ``[hipótesis]`` (eventos de U-03, U04-H-12)."""


def _actual(event_name: str) -> Row:
    entry = CATALOG[event_name]
    rule = entry.recipients
    return (
        entry.source_unit.value,
        entry.urgency.value,
        entry.emails,
        frozenset(role.value for role in rule.client),
        frozenset(person.value for person in rule.persons),
        frozenset(role.value for role in rule.provider),
        entry.audience.value,
    )


def test_the_catalog_has_exactly_the_events_of_the_table() -> None:
    assert set(CATALOG) == set(EXPECTED)
    assert len(CATALOG) == 51
    assert subscribed_events() == tuple(CATALOG)
    assert len(subscribed_events()) <= MAX_SUBSCRIPTIONS


@pytest.mark.parametrize("event_name", sorted(EXPECTED))
def test_each_row_matches_the_table(event_name: str) -> None:
    assert _actual(event_name) == EXPECTED[event_name]


def test_user_invited_is_not_in_the_catalog() -> None:
    # U04-H-13: la invitación la envía U-02 por EmailSenderPort; el invitado no recibe dos correos.
    assert catalog_entry("user_invited") is None
    assert "user_invited" not in subscribed_events()


@pytest.mark.parametrize("event_name", ["fleet_alarm_raised", "fleet_alarm_cleared"])
def test_fleet_alarms_go_only_to_the_provider(event_name: str) -> None:
    entry = CATALOG[event_name]
    assert entry.audience is NotificationAudience.PROVIDER
    assert entry.recipients.client == () and entry.recipients.persons == ()


def test_the_conditional_urgencies_follow_the_table() -> None:
    gate = CATALOG["gate_state_changed"]
    assert gate.urgency_for({"status": "revoked"}) is NotificationUrgency.IMMEDIATE
    for status in ("approved", "pending", "suspended", ""):
        assert gate.urgency_for({"status": status}) is NotificationUrgency.NORMAL
    alarm = CATALOG["fleet_alarm_raised"]
    immediate = {"node_mute", "simulated_adapter_in_productive"}
    for kind in FleetAlarmKind:
        expected = NotificationUrgency.IMMEDIATE if kind.value in immediate else N
        assert alarm.urgency_for({"alarm_kind": kind.value}) == expected, kind
    assert alarm.urgency_for({"alarm_kind": "node_mute_"}) is NotificationUrgency.NORMAL
    assert {name for name, entry in CATALOG.items() if entry.immediate_when is not None} == {
        "gate_state_changed",
        "fleet_alarm_raised",
    }


def test_the_hypothesis_urgencies_are_the_u03_ones() -> None:
    assert {name for name, entry in CATALOG.items() if entry.hypothesis} == HYPOTHESIS
    assert all(CATALOG[name].source_unit is ActorUnit.U03 for name in HYPOTHESIS)


def test_immediate_is_never_silenceable() -> None:
    for entry in CATALOG.values():
        immediate = entry.urgency is NotificationUrgency.IMMEDIATE
        assert entry.silenceable({}) is not immediate, entry.event_name
    assert CATALOG["gate_state_changed"].silenceable({"status": "revoked"}) is False
    assert CATALOG["gate_state_changed"].silenceable({"status": "approved"}) is True


def test_only_integrity_compromised_bypasses_the_email_circuit() -> None:
    assert {name for name, entry in CATALOG.items() if entry.bypasses_email_circuit} == {
        "integrity_compromised"
    }


def test_the_audience_matches_the_recipient_rule() -> None:
    for name, entry in CATALOG.items():
        rule = entry.recipients
        client = bool(rule.client or rule.persons)
        provider = bool(rule.provider)
        expected = {
            (True, False): NotificationAudience.CLIENT,
            (False, True): NotificationAudience.PROVIDER,
            (True, True): NotificationAudience.BOTH,
        }[(client, provider)]
        assert entry.audience is expected, name
    # BR-LAZ-137: sin coordinador activo, el gerente de planta; siempre con copia al proveedor.
    adoption = CATALOG["adoption_low"].recipients
    assert [role.value for role in adoption.client_fallback] == [PM]
    assert all(not CATALOG[n].recipients.client_fallback for n in CATALOG if n != "adoption_low")
    assert {person.value for person in PersonRef} == {ASSIGNEE, REQUESTER}


def test_every_subscribed_event_is_registered_by_the_unit_of_its_row() -> None:
    registry = EventTypeRegistry()
    for register in (
        register_u02_event_types,
        register_catalog_event_types,
        register_fleet_event_types,
        register_loop_event_types,
        register_accreditation_event_types,
    ):
        register(registry)
    for name, entry in CATALOG.items():
        compiled = registry.get(name)
        assert compiled is not None, name
        assert compiled.definition.publisher_unit is entry.source_unit, name


def test_the_roles_belong_to_the_side_they_are_asked_on() -> None:
    # Los del cliente, de la organización del evento; los del proveedor, de la proveedora.
    assert set(CLIENT_ROLES) == set(IDENTITY_CLIENT_ROLES)
    assert set(PROVIDER_STAFF) == set(PROVIDER_ROLES)
    for name, entry in CATALOG.items():
        assert set(entry.recipients.client) <= set(IDENTITY_CLIENT_ROLES), name
        assert set(entry.recipients.client_fallback) <= set(IDENTITY_CLIENT_ROLES), name
        assert set(entry.recipients.provider) <= set(PROVIDER_ROLES), name


def test_every_row_has_its_template_and_every_template_a_row() -> None:
    assert {entry.template_key for entry in CATALOG.values()} == set(template_keys())
