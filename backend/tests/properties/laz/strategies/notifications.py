"""``notification_event_sequences`` y el oráculo de destinatarios (TASK-324; BLM de U-04 §12;
PR-LAZ-39, 40 y 41).

- ``hierarchies``: una organización con 1 o 2 plantas de 1 o 2 zonas y de 0 a 10 asignaciones de
  rol de cualquier rol del cliente, en cualquier nivel (organización, planta o zona), activas o no,
  vigentes o retiradas. Es la forma de ``hierarchy_commands`` del kit de U-02 (crear plantas y
  zonas, invitar, activar, asignar y retirar roles, desactivar) reducida a su estado final, que es
  lo único que ve el motor.
- ``notification_event_sequences``: eventos del catálogo sobre esa jerarquía (de zona, de planta,
  de organización, de concesión, con persona y solo del proveedor), con **repeticiones** y, por
  entrega, si la transacción se confirma o se cae antes (``crash``).
- ``expected_recipients``: el **oráculo**, escrito sin el código del motor ni de la identidad: quién
  debe recibir cada evento según la regla de BR-LAZ-124 (el alcance contiene la zona o planta), las
  personas del evento y el relevo de ``adoption_low``.
- ``hostile_payload``: la carga de un evento con texto hostil (nombres, correos, URL, referencias de
  clip) en todo campo de texto, para PR-LAZ-41.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from hypothesis import strategies as st

from vigia_platform.notifications.catalog import CATALOG, PersonRef
from vigia_platform.shared.context import Role, ScopeLevel

__all__ = [
    "Assignment",
    "Delivery",
    "EventDraft",
    "Hierarchy",
    "Place",
    "Placed",
    "expected_recipients",
    "hierarchies",
    "hostile_payload",
    "notification_event_sequences",
    "payload_for",
]

CLIENT_ROLES: Final = (
    Role.COORDINATOR_SST,
    Role.LINE_MANAGER,
    Role.PLANT_MANAGER,
    Role.ADMINISTRATOR,
    Role.COPASST,
)
BIASED_ROLES: Final = (
    Role.COORDINATOR_SST,
    Role.COORDINATOR_SST,
    Role.COORDINATOR_SST,
    Role.PLANT_MANAGER,
    Role.ADMINISTRATOR,
    Role.LINE_MANAGER,
    Role.COPASST,
)
"""Los roles del cliente con más peso para el que más filas reciben (el coordinador SST): así los
ejemplos tienen a menudo un destinatario de otra zona o planta que **no** debe recibir el aviso."""
ZONE_EVENTS: Final = (
    "finding_received",
    "finding_confirmed",
    "finding_closed",
    "finding_unclosable",
    "review_overdue",
    "monthly_package_available",
    "commitments_recorded",
    "adoption_low",
    "late_segment_attached",
    "zone_activated",
    "gate_state_changed",
    "regression_marked",
)
PERSON_EVENTS: Final = ("action_assigned", "finding_overdue")
PLANT_EVENTS: Final = ("node_revoked", "node_enrolled")
ORGANIZATION_EVENTS: Final = ("vocabulary_list_changed", "user_activated", "integrity_compromised")
CONCESSION_EVENTS: Final = ("concession_granted",)
PROVIDER_EVENTS: Final = ("fleet_alarm_raised", "security_alert", "loop_projection_divergence")
EVENT_NAMES: Final = (
    *ZONE_EVENTS,
    *PERSON_EVENTS,
    *PLANT_EVENTS,
    *ORGANIZATION_EVENTS,
    *CONCESSION_EVENTS,
    *PROVIDER_EVENTS,
)
HOSTILE: Final = (
    "Juan Pérez",
    "juan.perez@example.test",
    "https://evil.example/clip.mp4",
    "clip-0001",
    "<img src=x>",
    "CC 1020304050",
)
"""Textos que nunca deben llegar a un aviso (PR-LAZ-41)."""


@dataclass(frozen=True)
class Place:
    """Dónde ocurre un evento: índice de planta y, si lo hay, de zona dentro de ella."""

    plant: int
    zone: int | None


@dataclass(frozen=True)
class Assignment:
    role: Role
    level: ScopeLevel
    place: Place
    """Para ``plant`` la planta; para ``zone`` la zona; ignorado en ``organization``."""
    active: bool = True
    removed: bool = False


@dataclass(frozen=True)
class Hierarchy:
    zones_per_plant: tuple[int, ...]
    assignments: tuple[Assignment, ...]


@dataclass(frozen=True)
class EventDraft:
    event_name: str
    place: Place
    """La zona o planta del evento (se ignora en los de organización)."""
    person: int | None = None
    """Índice de la asignación cuyo usuario es el responsable (``PERSON_EVENTS``)."""
    concession_level: ScopeLevel = ScopeLevel.PLANT
    under_concession: bool = False
    """Entregar con el contexto de una concesión de la planta 0 (solo eventos de la planta 0)."""
    flag: bool = False
    """``revoked`` en ``gate_state_changed``, ``node_mute`` en ``fleet_alarm_raised``."""


@dataclass(frozen=True)
class Delivery:
    """Una entrega del evento ``index``: confirmada o caída antes de confirmar."""

    index: int
    outcome: Literal["commit", "crash"]


@st.composite
def _place(draw: st.DrawFn, zones_per_plant: tuple[int, ...], *, zone: bool = True) -> Place:
    plant = draw(st.integers(0, len(zones_per_plant) - 1))
    if not zone:
        return Place(plant, None)
    return Place(plant, draw(st.integers(0, zones_per_plant[plant] - 1)))


@st.composite
def hierarchies(draw: st.DrawFn) -> Hierarchy:
    zones = tuple(draw(st.lists(st.integers(1, 2), min_size=1, max_size=2)))
    assignments: list[Assignment] = []
    for _ in range(draw(st.integers(0, 10))):
        level = draw(st.sampled_from(ScopeLevel))
        place = draw(_place(zones, zone=level is ScopeLevel.ZONE))
        assignments.append(
            Assignment(
                role=draw(st.sampled_from(BIASED_ROLES)),
                level=level,
                place=place,
                active=draw(st.sampled_from((True, True, True, False))),
                removed=draw(st.sampled_from((False, False, False, True))),
            )
        )
    return Hierarchy(zones, tuple(assignments))


@st.composite
def _event(draw: st.DrawFn, hierarchy: Hierarchy) -> EventDraft:
    name = draw(st.sampled_from(EVENT_NAMES))
    zones = hierarchy.zones_per_plant
    place = draw(_place(zones, zone=name not in PLANT_EVENTS))
    person = None
    if name in PERSON_EVENTS and hierarchy.assignments:
        person = draw(st.integers(0, len(hierarchy.assignments) - 1))
    level = draw(st.sampled_from(ScopeLevel)) if name in CONCESSION_EVENTS else ScopeLevel.PLANT
    concession = (
        place.plant == 0
        and name not in ORGANIZATION_EVENTS + CONCESSION_EVENTS + PROVIDER_EVENTS
        and draw(st.booleans())
    )
    return EventDraft(name, place, person, level, concession, draw(st.booleans()))


@st.composite
def notification_event_sequences(
    draw: st.DrawFn, hierarchy: Hierarchy, *, max_events: int = 5
) -> tuple[tuple[EventDraft, ...], tuple[Delivery, ...]]:
    """Eventos y sus entregas: cada evento se entrega una o más veces (repeticiones), alguna se
    cae antes de confirmar, y al final todo evento tiene al menos una entrega confirmada (la cola
    muerta se reprocesa)."""
    events = tuple(draw(st.lists(_event(hierarchy), min_size=1, max_size=max_events)))
    deliveries: list[Delivery] = []
    for index in range(len(events)):
        attempts = draw(st.lists(st.sampled_from(("commit", "crash")), min_size=0, max_size=2))
        deliveries += [Delivery(index, outcome) for outcome in attempts]
        deliveries.append(Delivery(index, "commit"))
    order = draw(st.permutations(deliveries))
    return events, tuple(order)


# --- ids y cargas ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Placed:
    """La jerarquía sembrada: identificadores reales de plantas, zonas y usuarios."""

    organization_id: uuid.UUID
    plants: tuple[uuid.UUID, ...]
    zones: tuple[tuple[uuid.UUID, ...], ...]
    users: tuple[uuid.UUID, ...]
    """El usuario de cada asignación de ``Hierarchy.assignments``, en el mismo orden."""

    def plant(self, place: Place) -> uuid.UUID:
        return self.plants[place.plant]

    def zone(self, place: Place) -> uuid.UUID | None:
        return None if place.zone is None else self.zones[place.plant][place.zone]


def payload_for(draft: EventDraft, placed: Placed, ids: Mapping[str, str]) -> dict[str, Any]:
    """Una carga mínima del evento en su lugar (lo que el motor lee de ella)."""
    name = draft.event_name
    zone = placed.zone(draft.place)
    plant = placed.plant(draft.place)
    if name in PLANT_EVENTS:
        return {
            "node_id": ids["node"],
            "plant_id": str(plant),
            "zone_ids": [str(z) for z in placed.zones[draft.place.plant]],
        }
    if name in ORGANIZATION_EVENTS:
        return {
            "change_id": ids["record"],
            "user_id": ids["record"],
            "chain_kind": "ledger",
            "first_failed_sequence": 3,
        }
    if name in CONCESSION_EVENTS:
        scope = {
            ScopeLevel.ORGANIZATION: placed.organization_id,
            ScopeLevel.PLANT: plant,
            ScopeLevel.ZONE: zone,
        }[draft.concession_level]
        return {
            "concession_id": ids["record"],
            "scope_level": draft.concession_level.value,
            "scope_id": str(scope),
            "expires_at": "2026-12-01T00:00:00.000Z",
        }
    payload: dict[str, Any] = {
        "zone_id": str(zone),
        "subject_kind": "finding",
        "subject_id": ids["subject"],
        "record_id": ids["record"],
        "days_overdue": 3,
        "silent_days": 14,
    }
    if name == "gate_state_changed":
        payload["status"] = "revoked" if draft.flag else "approved"
    if name == "fleet_alarm_raised":
        payload["alarm_kind"] = "node_mute" if draft.flag else "clock_drift"
    if name in PERSON_EVENTS and draft.person is not None:
        payload["assignee_user_id"] = str(placed.users[draft.person])
    return payload


def event_plant(draft: EventDraft, placed: Placed) -> uuid.UUID | None:
    """``OutboxEvent.plant_id``: la planta de la cadena (ninguna en los de organización)."""
    if draft.event_name in ORGANIZATION_EVENTS + CONCESSION_EVENTS:
        return None
    return placed.plant(draft.place)


# --- oráculo -------------------------------------------------------------------------------------


def _targets(draft: EventDraft) -> tuple[ScopeLevel, Place]:
    """El alcance que el destinatario tiene que contener."""
    name = draft.event_name
    if name in ORGANIZATION_EVENTS:
        return ScopeLevel.ORGANIZATION, draft.place
    if name in CONCESSION_EVENTS:
        if draft.concession_level is ScopeLevel.ZONE:
            return ScopeLevel.ZONE, draft.place
        return draft.concession_level, Place(draft.place.plant, None)
    if name in PLANT_EVENTS:
        return ScopeLevel.PLANT, Place(draft.place.plant, None)
    return ScopeLevel.ZONE, draft.place


def _contains(assignment: Assignment, level: ScopeLevel, place: Place) -> bool:
    """Organización ⊇ planta ⊇ zona (BR-NUC-12), sobre los índices del modelo."""
    if assignment.level is ScopeLevel.ORGANIZATION:
        return True
    if level is ScopeLevel.ORGANIZATION:
        return False
    if assignment.level is ScopeLevel.PLANT:
        return assignment.place.plant == place.plant
    return (
        level is ScopeLevel.ZONE
        and assignment.place.plant == place.plant
        and assignment.place.zone == place.zone
    )


def _holders(
    hierarchy: Hierarchy, roles: Sequence[Role], level: ScopeLevel, place: Place
) -> set[int]:
    """Índices de las asignaciones vigentes, de usuario activo, con ``roles`` que contienen."""
    return {
        index
        for index, assignment in enumerate(hierarchy.assignments)
        if assignment.active
        and not assignment.removed
        and assignment.role in roles
        and _contains(assignment, level, place)
    }


def expected_recipients(hierarchy: Hierarchy, draft: EventDraft) -> set[int]:
    """Las asignaciones (índices) cuyo usuario recibe el evento en la organización cliente.

    Cada asignación es un usuario distinto (``add_member``), así que el índice identifica al
    usuario. Los del proveedor no están en la jerarquía generada: los comprueba la prueba.
    """
    entry = CATALOG[draft.event_name]
    rule = entry.recipients
    if entry.audience.value == "provider":
        return set()
    level, place = _targets(draft)
    users = _holders(hierarchy, rule.client, level, place)
    if not users and rule.client_fallback:
        users = _holders(hierarchy, rule.client_fallback, level, place)
    if (
        PersonRef.ASSIGNEE in rule.persons
        and draft.person is not None
        and draft.person in _holders(hierarchy, CLIENT_ROLES, level, place)
    ):
        users.add(draft.person)
    return users


def hostile_payload(event_name: str, text: str, ids: Mapping[str, str]) -> dict[str, Any]:
    """Una carga del evento con ``text`` en cada campo de texto libre que pudiera tener."""
    return {
        "zone_id": ids["zone"],
        "subject_kind": "finding",
        "subject_id": ids["subject"],
        "record_id": ids["record"],
        "node_id": ids["node"],
        "export_id": ids["record"],
        "package_id": ids["record"],
        "detection_id": ids["subject"],
        "display_name": text,
        "assignee_display_name": text,
        "reason_es": text,
        "reason_text": text,
        "alert_kind": text,
        "alarm_kind": text,
        "family": text,
        "period": text,
        "status": text,
        "escalation_level": text,
        "clip_id": text,
        "evidence_id": text,
        "url": text,
        "days_overdue": 3,
        "due_at": "2026-10-01T00:00:00.000Z",
    }
