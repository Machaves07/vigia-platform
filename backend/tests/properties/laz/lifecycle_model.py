"""Modelo independiente de la tabla de ciclo de vida de DE §4.1 y §4.2.

No importa código de producción deliberadamente: PR-LAZ-10 compara esta tabla pequeña con
la proyección y los servicios reales.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LifecycleModel:
    """Estado mínimo observable y las guardas de la tabla de transiciones."""

    state: str = "received"
    previous_state: str | None = None
    action_assigned: bool = False
    evidence_entries: int = 0
    due_at: int | None = None
    clock: int = 0
    assignee_inactive: bool = False
    gate_revoked: bool = False
    deadline_days: int = 1
    unclosable_review_due_at: int | None = None
    unclosable_pending_review: bool = False

    def classify(self, outcome: str, reinforced: bool) -> str | None:
        if self.state == "received":
            self.state = "open" if outcome == "confirmed" else "discarded"
            return None
        if self.state == "in_review":
            return "transition_not_allowed"
        if self.state in {"open", "action_assigned", "overdue"}:
            if outcome == "confirmed":
                return "transition_not_allowed"
            if not reinforced:
                return "reason_required"
            self.previous_state, self.state = self.state, "suspended_by_reclassification"
            return None
        if self.state == "suspended_by_reclassification":
            if outcome != "confirmed":
                return "transition_not_allowed"
            if not reinforced:
                return "reason_required"
            previous = self.previous_state
            self.previous_state = None
            self.state = (
                "overdue" if previous == "overdue" or self._is_overdue() else previous or "open"
            )
            return None
        if self.state == "discarded":
            if not reinforced:
                return "reason_required"
            self.state = "open" if outcome == "confirmed" else "discarded"
            return None
        if self.state in {"closed", "unclosable"}:
            if not reinforced:
                return "reason_required"
            return None
        return "transition_not_allowed"

    def resolve_queue(self, outcome: str) -> str | None:
        if self.state != "in_review":
            return "transition_not_allowed"
        self.state = "open" if outcome == "review_confirmed" else "discarded"
        return None

    def assign_action(self, deadline_days: int) -> str | None:
        if self.state != "open":
            return "transition_not_allowed"
        if not 1 <= deadline_days <= 180:
            return "deadline_out_of_range"
        self.action_assigned = True
        self.evidence_entries = 0
        self.due_at = self.clock + deadline_days * 86_400
        self.state = "action_assigned"
        return None

    def add_evidence(self) -> str | None:
        if not self.action_assigned:
            return "transition_not_allowed"
        self.evidence_entries += 1
        return None

    def close(self, signer_has_license: bool) -> str | None:
        if self.state not in {"action_assigned", "overdue"}:
            if self.state in {"received", "in_review", "open", "discarded"}:
                return "closure_requires_assigned_action"
            return "transition_not_allowed"
        if self.evidence_entries == 0:
            return "closure_evidence_missing"
        if not signer_has_license:
            return "signer_license_missing"
        self.state = "closed"
        return None

    def declare_unclosable(
        self, fields_complete: bool, policy: str, review_months: int | None, license_present: bool
    ) -> str | None:
        if policy == "revoked":
            return "unclosable_policy_revoked"
        if policy == "partial":
            return "unclosable_policy_unavailable"
        if policy == "not_signed":
            return "unclosable_policy_not_signed"
        # BLM §8.2 consulta primero la política; una vez superada, la implementación valida los
        # cinco campos antes de comprobar el estado de la tabla. Esta prioridad permite que el
        # oráculo compare también el ``detail_code`` de un rechazo de la plataforma.
        if not fields_complete:
            return "unclosable_fields_incomplete"
        if self.state not in {"open", "action_assigned", "overdue"}:
            return "transition_not_allowed"
        if review_months is not None and not 0 < review_months <= 12:
            return "unclosable_review_date_out_of_range"
        if not license_present:
            return "signer_license_missing"
        self.state = "unclosable"
        self.unclosable_review_due_at = self.clock + (review_months or 6) * 30 * 86_400
        return None

    def review_unclosable(
        self, outcome: str, license_present: bool, review_months: int | None
    ) -> str | None:
        if self.state != "unclosable":
            return "transition_not_allowed"
        if outcome == "converted_to_action" and review_months is not None:
            return "invalid_request"
        if outcome == "renewed" and review_months is not None and not 0 < review_months <= 12:
            return "unclosable_review_date_out_of_range"
        if not license_present:
            return "signer_license_missing"
        if outcome == "converted_to_action":
            self.state, self.action_assigned, self.evidence_entries = "action_assigned", True, 0
            self.due_at = self.clock + self.deadline_days * 86_400
        return None

    def reopen(self, reason_present: bool) -> str | None:
        if self.state not in {"closed", "unclosable"}:
            return "transition_not_allowed"
        if not reason_present:
            return "reopening_reason_required"
        self.state, self.action_assigned, self.evidence_entries = "action_assigned", True, 0
        self.due_at = self.clock + self.deadline_days * 86_400
        return None

    def advance_clock(self, seconds: int) -> None:
        self.clock += seconds
        if self.state == "unclosable" and self.unclosable_review_due_at is not None:
            self.unclosable_pending_review = self.clock >= self.unclosable_review_due_at
        if self.state == "action_assigned" and self._is_overdue():
            self.state = "overdue"

    def late_segment(self) -> None:
        """T21 conserva el estado observable sin reabrir el lazo."""

    def deactivate_assignee(self) -> None:
        self.assignee_inactive = True

    def revoke_gate(self) -> None:
        self.gate_revoked = True

    def change_configuration(self, deadline_days: int) -> str | None:
        if not 1 <= deadline_days <= 180:
            return "deadline_out_of_range"
        self.deadline_days = deadline_days
        return None

    def _is_overdue(self) -> bool:
        return self.due_at is not None and self.clock >= self.due_at
