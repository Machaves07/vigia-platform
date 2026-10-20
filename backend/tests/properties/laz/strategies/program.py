"""Historias sintéticas del programa con hechos independientes para el oráculo."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from hypothesis import strategies as st

from vigia_platform.loop.projection.fold import ProjectionInput

START = datetime(2026, 3, 1, tzinfo=UTC)
END = datetime(2026, 4, 1, tzinfo=UTC)
PREVIOUS = datetime(2026, 2, 1, tzinfo=UTC)
ORG, PLANT, ZONE = (uuid.UUID(int=n) for n in (1, 2, 3))


@dataclass(frozen=True)
class Subject:
    subject_id: uuid.UUID
    family: str
    received: datetime
    first_classified: datetime | None
    state: str
    due: datetime
    closed: datetime | None
    engineering: bool
    kind: str = "finding"
    closure_eligible: bool = True


@dataclass(frozen=True)
class ProgramHistory:
    records: tuple[ProjectionInput, ...]
    subjects: tuple[Subject, ...]
    approved: bool
    adoption: int
    exposure: float


def stamp(at: datetime) -> str:
    return at.isoformat()


@st.composite
def program_histories(draw: st.DrawFn) -> ProgramHistory:
    events: list[tuple[str, dict[str, Any], datetime]] = []
    subjects: list[Subject] = []
    for index in range(draw(st.integers(0, 12))):
        subject_id = uuid.UUID(int=100 + index)
        action_id = uuid.UUID(int=200 + index)
        entry_id = uuid.UUID(int=300 + index)
        received = START + timedelta(days=draw(st.integers(-24, 12)), hours=8)
        first = received + timedelta(days=draw(st.integers(0, 9)))
        assigned = first + timedelta(minutes=1)
        due = assigned + timedelta(days=2)
        at = assigned + timedelta(days=4)
        state = draw(st.sampled_from(("received", "open", "closed", "overdue", "unclosable")))
        family = draw(st.sampled_from(("coexistence", "dwell")))
        engineering = draw(st.booleans())
        kind = draw(st.sampled_from(("finding", "detection")))
        subject = {"subject_id": str(subject_id), "subject_kind": kind}
        events.append(
            (
                "finding_received",
                {
                    "plant_id": str(PLANT),
                    "zone_id": str(ZONE),
                    "family": family,
                    "tier": "tier_1",
                    "standard": {"standard_id": str(uuid.UUID(int=7)), "version": 1},
                    "node_time": {
                        "started_at": stamp(received),
                        "ended_at": stamp(received + timedelta(seconds=1)),
                    },
                    "observability_state": "observable",
                    "max_confidence": 0.9,
                    "episode_duration_ms": 1000,
                    "condition_duration_ms": 1000,
                    "cameras": [],
                    "episode": {
                        "episode_id": str(subject_id),
                        "segment_index": 0,
                        "continues": False,
                    },
                },
                received,
            )
        )
        if kind == "detection":
            _, observed, observed_at = events[-1]
            observed.pop("episode")
            observed.pop("tier")
            observed["detection_id"] = str(subject_id)
            events[-1] = ("detection_for_review_received", observed, observed_at)
        if state != "received":
            events.append(
                (
                    "classification" if kind == "finding" else "review_resolution",
                    {
                        **subject,
                        "outcome": "confirmed" if kind == "finding" else "review_confirmed",
                        "detection_id": str(subject_id),
                        "reason_category": "real_coexistence",
                        "reinforced_reason": False,
                        "classified_at": stamp(first),
                    },
                    first,
                )
            )
        if state not in ("received", "open"):
            events.append(
                (
                    "action_assigned",
                    {
                        **subject,
                        "action_id": str(action_id),
                        "action_type": "engineering" if engineering else "process",
                        "assignee_user_id": str(uuid.UUID(int=9)),
                        "due_at": stamp(due),
                        "assigned_at": stamp(assigned),
                    },
                    assigned,
                )
            )
        if state in ("overdue", "unclosable"):
            events.append(
                (
                    "action_overdue",
                    {
                        **subject,
                        "action_id": str(action_id),
                        "days_overdue": 2,
                        "declared_at": stamp(at),
                        "escalation_level": "coordinator",
                    },
                    at,
                )
            )
        if state == "unclosable":
            at += timedelta(minutes=1)
            events.append(
                (
                    "unclosable_state_declared",
                    {
                        **subject,
                        "declared_at": stamp(at),
                        "review_due_at": stamp(at + timedelta(days=90)),
                        "signer": {"professional_license": "LIC-SINTETICA"},
                    },
                    at,
                )
            )
        closed = None
        closure_eligible = True
        if state == "closed":
            closed = at
            closure_case = draw(st.sampled_from(("valid", "superseded", "unknown_evidence")))
            closure_eligible = closure_case == "valid"
            if closure_case == "superseded":
                events.append(
                    (
                        "action_assigned",
                        {
                            **subject,
                            "action_id": str(uuid.UUID(int=10000 + index)),
                            "supersedes_action_id": str(action_id),
                            "action_type": "engineering" if engineering else "process",
                            "assignee_user_id": str(uuid.UUID(int=9)),
                            "due_at": stamp(due),
                            "assigned_at": stamp(at - timedelta(seconds=1)),
                        },
                        at - timedelta(seconds=1),
                    )
                )
            events.append(
                (
                    "action_evidence_added",
                    {
                        "subject_id": str(subject_id),
                        "action_id": str(action_id),
                        "evidence_entry_id": str(entry_id),
                        "added_at": stamp(at),
                    },
                    at,
                )
            )
            if closure_case == "unknown_evidence":
                # La lista del cierre no está vacía, pero la entrada que cita no
                # existe en el expediente. El cálculo no debe contar ese cierre.
                events.pop()
            events.append(
                (
                    "closure",
                    {
                        **subject,
                        "action_id": str(action_id),
                        "action_type": "engineering" if engineering else "process",
                        "evidence_entry_ids": [str(entry_id)],
                        "signed_at": stamp(at),
                        "signer": {"professional_license": "LIC-SINTETICA"},
                    },
                    at,
                )
            )
        subjects.append(
            Subject(
                subject_id,
                family,
                received,
                None if state == "received" else first,
                "in_review" if kind == "detection" and state == "received" else state,
                due,
                closed,
                engineering,
                kind,
                closure_eligible,
            )
        )
    records = tuple(
        ProjectionInput(
            record_id=uuid.UUID(int=1000 + i),
            organization_id=ORG,
            plant_id=PLANT,
            chain_sequence=i + 1,
            record_type=kind,
            received_at=at,
            document=doc,
        )
        for i, (kind, doc, at) in enumerate(sorted(events, key=lambda e: e[2]))
    )
    return ProgramHistory(
        records,
        tuple(subjects),
        draw(st.booleans()),
        draw(st.integers(0, 1000)),
        float(draw(st.integers(0, 600))),
    )
