"""Contraejemplos reducidos de PR-LAZ-10 (revisión de b5c9a03, semilla 1111).

Los dos casos tras resolver cola detectaron VIG-268: se ejecutan sin xfail contra los servicios
corregidos. Los demás protegen el oráculo y el arnés, no codifican errores 500 como válidos.
"""

from datetime import UTC, datetime
from uuid import UUID

from tests.properties.laz.strategies.lifecycle import (
    AddEvidence,
    Assign,
    ChangeDeadlineRange,
    ClassifyFinding,
    CloseFinding,
    DeactivateAssignee,
    DeclarationAttempt,
    DeclareUnclosable,
    LateSegment,
    LifecycleCommand,
    PlantPolicy,
    Reclassify,
    ReopenFinding,
    ResolveQueue,
    ReviewAttempt,
    ReviewUnclosable,
    RevokeGate,
    Tick,
)

POLICY = PlantPolicy(
    "loaded",
    UUID("00000000-0000-4000-8000-000000000001"),
    1,
    datetime(2026, 1, 1, tzinfo=UTC),
    "a" * 64,
)
DECLARE = DeclareUnclosable(DeclarationAttempt(5, POLICY, 1, True))
CONFIRM = ResolveQueue("review_confirmed")
ASSIGN = Assign("engineering", 1)
CALENDAR = ChangeDeadlineRange(50, 60, 70, (1, 2, 3, 4, 5))

# Nombre estable, entrada de cola, comandos, estado final esperado. No se deriva la generación de
# la base ni de marcas de tiempo, y se comprueba la invariante incluso en el recorrido vacío.
REGRESSIONS: tuple[tuple[str, bool, tuple[LifecycleCommand, ...], str], ...] = (
    ("empty", True, (), "in_review"),
    ("configuration_range", True, (CONFIRM, CALENDAR, Assign("engineering", 10)), "open"),
    (
        "default_deadline",
        True,
        (CONFIRM, Assign("engineering", None), Tick(3 * 86400)),
        "action_assigned",
    ),
    ("business_day", True, (CONFIRM, ASSIGN, Tick(86401)), "action_assigned"),
    ("inactive_assignee", True, (CONFIRM, DeactivateAssignee(), ASSIGN), "open"),
    (
        "vig_268_discarded",
        True,
        (ResolveQueue("review_discarded"), Reclassify("false_positive")),
        "discarded",
    ),
    (
        "vig_268_confirmed",
        True,
        (CONFIRM, Reclassify("false_positive")),
        "suspended_by_reclassification",
    ),
    ("expired_session", False, (Tick(3 * 86400), ClassifyFinding("confirmed", False)), "open"),
    (
        "evidence_after_close",
        True,
        (CONFIRM, ASSIGN, AddEvidence(), CloseFinding(), AddEvidence()),
        "closed",
    ),
    (
        "reopening_cycle",
        True,
        (
            CONFIRM,
            ASSIGN,
            AddEvidence(),
            CloseFinding(),
            ReopenFinding("process", 1),
            AddEvidence(),
            CloseFinding(),
        ),
        "closed",
    ),
    (
        "reopening_deadline_range",
        True,
        (CONFIRM, ASSIGN, AddEvidence(), CloseFinding(), CALENDAR, ReopenFinding("process", 1)),
        "closed",
    ),
    (
        "suspended_deadline",
        True,
        (CONFIRM, ASSIGN, Reclassify("false_positive"), Tick(6 * 86400), Reclassify("confirmed")),
        "overdue",
    ),
    (
        "unclosable_renewal_conversion",
        True,
        (
            CONFIRM,
            DECLARE,
            ReviewUnclosable(ReviewAttempt("renewed", 1, True)),
            ChangeDeadlineRange(1, 2, 3, (1, 2, 3)),
            Tick(32 * 86400),
            ReviewUnclosable(ReviewAttempt("converted_to_action", None, True)),
            AddEvidence(),
            CloseFinding(),
        ),
        "closed",
    ),
    (
        "overdue_stays_visible",
        True,
        (CONFIRM, ASSIGN, DeactivateAssignee(), Tick(6 * 86400), LateSegment(), RevokeGate()),
        "overdue",
    ),
)
