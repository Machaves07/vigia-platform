"""``loop.root_cause`` (LC-LAZ-12; S-PLA-13; SUP-U05-16 a 18): el análisis de causa raíz guiado.

La IA conduce el método y el humano decide (P8): el flujo funciona **sin asistente** (TASK-316 lo
añade después) y lo que se firma es exactamente el texto que el humano aceptó. Las reglas puras
están en ``loop.domain.root_cause``; aquí, el alcance, la base y el expediente, en el orden de
business-logic-model.md §4:

1. ``start`` (``iniciar_analisis``): la fila del sujeto en la proyección y ``root_cause.analyze``
   sobre **su zona**; compone el ``ZoneObjectiveContext`` (DE §2.10, un valor que se guarda en
   ``zone_context``, no una entidad) y crea el borrador en ``loop.root_cause_analysis`` (estado
   operativo, DE §9: el borrador no entra al expediente).
2. ``put_step`` (``avanzar_paso``): el paso anterior completo (``analysis_step_out_of_order``),
   el enunciado por la política de texto libre **con los dos validadores de U-04**
   (``blocked_vocabulary_term``, ``person_attribution_detected``) y, si el paso declara categoría,
   una de las tres (``cause_category_not_allowed``).
3. ``sign`` (``firmar``): las categorías y los textos finales validados; si falta algo, el
   análisis queda ``pending_information`` con lo que falta (``missing_information``, que se
   **confirma**) y la respuesta es ``analysis_information_insufficient``; si no, nombre y licencia
   (``signer_license_missing``) y, en una sola transacción, el registro ``root_cause_analysis`` por
   ``EscritorExpediente`` con su evento ``root_cause_analysis_signed`` y su proyección
   (``LoopProjectionStore.write_and_apply``: ``has_signed_root_cause``), y la fila ``signed``.

``in_progress`` (VIG-363) lee, con el alcance de ``start`` y sin escribir, el análisis sin firmar
más reciente del sujeto y la última solicitud al asistente de ese análisis: así el coordinador
retoma lo guardado sin terminar en vez de iniciar otro.

Un análisis ``signed`` no admite pasos ni otra firma: ``transition_not_allowed`` (P4). Un sujeto o
un análisis inexistente, de otra organización, fuera del alcance o pedido bajo concesión de
proveedor responde ``ResourceNotFound`` (``not_found``) sin escribir nada.

**El contexto objetivo** (BR-LAZ-63): solo hechos medidos, ningún texto de un usuario ni un nombre
de persona. Del sujeto, su familia, *tier*, estado de observabilidad y estándar; del registro de
anclaje, las señales por rol (``role``, ``value``, ``valid``); del episodio, sus duraciones; de
``CatalogQueryPort.standard_version``, el texto del estándar con que se evaluó (si no lo da,
``standard_text_unavailable``); de ``CoveragePort``, el resumen de cobertura del periodo; de
``FleetQueryPort.nodes_by_zone``, el estado de comunicación de los nodos; de la proyección, los
hallazgos por familia del periodo, las reincidencias, las acciones previas con su resultado y las
categorías de causa ya firmadas en la zona. El **periodo** son los ``CONTEXT_PERIOD_DAYS`` días
hasta el inicio del análisis ``[objetivo propio]``: ``CoveragePort`` admite 31 como mucho. Son
dos llamadas a U-03 (PAT-LAZ-REN-05: a lo sumo cinco por operación que responde a una persona).

**Orden de candados** (el de ``loop.application.classification``; AGENTS.md):

1. la exclusión del sujeto (``loop_subject|<organización>|<sujeto>``), solo al firmar;
2. la fila del análisis (``SELECT … FOR UPDATE``), al firmar y al escribir un paso;
3. la cadena de la planta (el disparador del expediente al anexar);
4. el control de la proyección de la planta y las filas del sujeto (``LoopProjectionStore``).

``put_step`` toma solo 2; ``start`` e ``in_progress`` ninguno. Así dos firmas del mismo análisis se
ordenan (la segunda lee ``signed`` y recibe ``transition_not_allowed``), un paso nunca se pierde
frente a otro paso, y un paso que llega durante una firma lee ``signed`` y no toca lo firmado.

**Enlace con la acción**: ``linked_analysis`` resuelve el ``root_cause_analysis_record_id``
opcional del registro ``action_assigned`` (DE §5.2) al ``analysis_id`` de un análisis firmado del
mismo sujeto, para ``corrective_action.root_cause_analysis_id`` (§2.5); la ruta de la acción es de
TASK-311.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import text
from vigia_contracts.models.enumerations import AcceptanceStatus

from vigia_platform.catalog.domain.ports import CatalogQueryPort
from vigia_platform.fleet.ports import FleetQueryPort
from vigia_platform.identity.authz.authorize import Authorizer, Resource, ResourceNotFound
from vigia_platform.identity.authz.context import with_unit
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.coverage import CoveragePort, CoverageZoneNotFound
from vigia_platform.ledger.application.writer import (
    EscritorExpediente,
    LedgerDatabase,
    LedgerRejection,
    LedgerRejectionCode,
    Receipt,
    RecordScope,
)
from vigia_platform.ledger.domain.coverage import CoveragePeriod
from vigia_platform.ledger.free_text import (
    FreeTextField,
    FreeTextHint,
    FreeTextPolicyRegistry,
    FreeTextPreparation,
    FreeTextRejected,
    apply_base_policy,
)
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.domain.enums import (
    AnalysisMissingItem,
    CauseCategory,
    EvidenceRefKind,
    RootCauseAnalysisState,
)
from vigia_platform.loop.domain.root_cause import (
    MAX_EVIDENCE_REFS,
    MAX_FACTORS,
    MAX_WHY_STEPS,
    Branch,
    EvidenceReference,
    Factor,
    GuidedAnalysis,
    Pending,
    RootCauseCode,
    RootCauseRejected,
    WhyStep,
    cause_category,
    missing_information_es,
    missing_items,
    put_step,
    record_content,
    sign,
)
from vigia_platform.loop.projection.store import LoopProjectionStore
from vigia_platform.shared.api.errors import ApiErrorCode, ExternalDependencyDown
from vigia_platform.shared.api.labels import PlatformLabels
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.outbox.publish import NewEvent
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

__all__ = [
    "ANALYSIS_RESOURCE_KIND",
    "CONTEXT_PERIOD_DAYS",
    "MAX_PREVIOUS_ACTIONS",
    "ROOT_CAUSE_ANALYSIS",
    "SUBJECT_RESOURCE_KIND",
    "AnalysisRejected",
    "AnalysisRequestInvalid",
    "AnalysisUnavailable",
    "AnalysisView",
    "InProgressView",
    "RootCauseService",
    "checked_branches",
    "checked_category",
    "checked_evidence_refs",
    "checked_text",
    "linked_analysis",
]

ROOT_CAUSE_ANALYSIS: Final = "root_cause_analysis"
SUBJECT_RESOURCE_KIND: Final = "finding"
"""``kind`` del recurso al iniciar: la fila de la bandeja."""
ANALYSIS_RESOURCE_KIND: Final = "root_cause_analysis"
"""``kind`` del recurso en los pasos y la firma: el análisis."""
CONTEXT_PERIOD_DAYS: Final = 30
"""Días del periodo del contexto objetivo `[objetivo propio]` (``CoveragePort``: ≤ 31)."""
MAX_PREVIOUS_ACTIONS: Final = 20
"""Acciones previas de la zona en el contexto, las más recientes `[objetivo propio]`."""

STATEMENT_FIELD: Final = FreeTextField(ROOT_CAUSE_ANALYSIS, "/why_steps[*]/statement", 20, 1000)
FACTOR_FIELD: Final = FreeTextField(
    ROOT_CAUSE_ANALYSIS, "/cause_effect_branches[*]/factors[*]/text", 5, 500
)
ROOT_STATEMENT_FIELD: Final = FreeTextField(ROOT_CAUSE_ANALYSIS, "/root_cause_statement", 20, 2000)
CONCLUSION_FIELD: Final = FreeTextField(ROOT_CAUSE_ANALYSIS, "/conclusion", 20, 2000)
LICENSE_FIELD: Final = FreeTextField(ROOT_CAUSE_ANALYSIS, "/signer/professional_license", 1, 64)
"""Los límites de ``loop.record_types`` (``StepStatement``, ``FactorText``, ``LongText`` y
``ProfessionalLicense``) para las rutas de texto libre del tipo (DE §5.1)."""

_DOMAIN_CODES: Final[Mapping[RootCauseCode, LoopDetailCode]] = {
    RootCauseCode.ANALYSIS_STEP_OUT_OF_ORDER: LoopDetailCode.ANALYSIS_STEP_OUT_OF_ORDER,
    RootCauseCode.CAUSE_CATEGORY_NOT_ALLOWED: LoopDetailCode.CAUSE_CATEGORY_NOT_ALLOWED,
    RootCauseCode.SIGNER_LICENSE_MISSING: LoopDetailCode.SIGNER_LICENSE_MISSING,
    RootCauseCode.TRANSITION_NOT_ALLOWED: LoopDetailCode.TRANSITION_NOT_ALLOWED,
}
_VALIDATOR_CODES: Final[Mapping[str, LoopDetailCode]] = {
    LoopDetailCode.BLOCKED_VOCABULARY_TERM.value: LoopDetailCode.BLOCKED_VOCABULARY_TERM,
    LoopDetailCode.PERSON_ATTRIBUTION_DETECTED.value: LoopDetailCode.PERSON_ATTRIBUTION_DETECTED,
}
"""Los ``detail_code`` de los dos validadores de U-04 que un rechazo de texto conserva."""

_LOCK_SUBJECT: Final = text(
    "SELECT pg_advisory_xact_lock("
    "hashtextextended('loop_subject|' || :organization_id || '|' || :subject_id, 0))"
)
_SUBJECT: Final = text(
    "SELECT finding_state_id, organization_id, plant_id, zone_id, subject_kind, subject_id,"
    " anchor_record_id, family, tier, standard_id, standard_version,"
    " observability_state_at_event FROM loop.finding_state"
    " WHERE organization_id = :organization_id AND finding_state_id = :finding_state_id"
)
_ANCHOR: Final = text(
    "SELECT anchor_record_id FROM loop.finding_state"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id AND zone_id = :zone_id"
    " AND subject_id = :subject_id"
)
_ANALYSIS: Final = text(
    "SELECT analysis_id, organization_id, plant_id, zone_id, subject_kind, subject_id, record_id,"
    " state, zone_context, why_steps, cause_effect_branches, root_cause_category,"
    " root_cause_statement, conclusion, missing_information, signer, signed_at, created_at,"
    " updated_at FROM loop.root_cause_analysis"
    " WHERE organization_id = :organization_id AND analysis_id = :analysis_id"
)
_ANALYSIS_FOR_UPDATE: Final = text(
    "SELECT analysis_id, organization_id, plant_id, zone_id, subject_kind, subject_id, record_id,"
    " state, zone_context, why_steps, cause_effect_branches, root_cause_category,"
    " root_cause_statement, conclusion, missing_information, signer, signed_at, created_at,"
    " updated_at FROM loop.root_cause_analysis"
    " WHERE organization_id = :organization_id AND analysis_id = :analysis_id FOR UPDATE"
)
_INSERT: Final = text(
    "INSERT INTO loop.root_cause_analysis (analysis_id, organization_id, plant_id, zone_id,"
    " subject_kind, subject_id, state, zone_context, why_steps, cause_effect_branches,"
    " assistant_used, created_at, updated_at) VALUES (:analysis_id, :organization_id, :plant_id,"
    " :zone_id, :subject_kind, :subject_id, 'draft', CAST(:zone_context AS jsonb),"
    " CAST('[]' AS jsonb), CAST('[]' AS jsonb), false, :at, :at)"
)
_UPDATE_STEPS: Final = text(
    "UPDATE loop.root_cause_analysis SET why_steps = CAST(:why_steps AS jsonb), state = :state,"
    " missing_information = :missing_information, updated_at = :at"
    " WHERE organization_id = :organization_id AND analysis_id = :analysis_id"
)
_UPDATE_FINAL: Final = text(
    "UPDATE loop.root_cause_analysis SET state = :state,"
    " cause_effect_branches = CAST(:branches AS jsonb), root_cause_category = :category,"
    " root_cause_statement = :statement, conclusion = :conclusion,"
    " missing_information = :missing_information, record_id = :record_id,"
    " signer = CAST(:signer AS jsonb), signed_at = :signed_at, updated_at = :at"
    " WHERE organization_id = :organization_id AND analysis_id = :analysis_id"
)
_IN_PROGRESS: Final = text(
    "SELECT analysis_id, organization_id, plant_id, zone_id, subject_kind, subject_id, record_id,"
    " state, zone_context, why_steps, cause_effect_branches, root_cause_category,"
    " root_cause_statement, conclusion, missing_information, signer, signed_at, created_at,"
    " updated_at FROM loop.root_cause_analysis"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id AND zone_id = :zone_id"
    " AND subject_id = :subject_id AND state <> 'signed'"
    " ORDER BY updated_at DESC, analysis_id DESC LIMIT 1"
)
"""El análisis sin firmar más reciente del sujeto (índice ``root_cause_analysis_scope_subject``)."""
_LATEST_REQUEST: Final = text(
    "SELECT request_id FROM loop.assistant_request"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " AND analysis_id = :analysis_id ORDER BY requested_at DESC, request_id DESC LIMIT 1"
)
"""La última solicitud al asistente del análisis (índice ``assistant_request_scope_analysis``)."""
_LINKED: Final = text(
    "SELECT analysis_id FROM loop.root_cause_analysis"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id AND zone_id = :zone_id"
    " AND subject_id = :subject_id AND record_id = :record_id AND state = 'signed'"
)

# --- el contexto objetivo (lecturas de la proyección y del expediente) -----------------------

_SIGNALS: Final = text(
    "SELECT content FROM ledger.ledger_record"
    " WHERE organization_id = :organization_id AND record_id = :record_id"
)
_EPISODE: Final = text(
    "SELECT total_episode_duration_ms, total_condition_duration_ms FROM loop.episode"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id AND zone_id = :zone_id"
    " AND episode_id = :subject_id"
)
_FAMILY_COUNTS: Final = text(
    "SELECT family, count(*) AS findings,"
    " count(*) FILTER (WHERE subject_id <> :subject_id) AS others FROM loop.finding_state"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id AND zone_id = :zone_id"
    " AND subject_kind = 'finding' AND lifecycle_state <> 'discarded'"
    " AND occurred_at_exact >= :period_start AND occurred_at_exact < :period_end"
    " GROUP BY family ORDER BY family"
)
_PREVIOUS_ACTIONS: Final = text(
    "SELECT f.current_action_type, f.lifecycle_state, f.closed_at,"
    " (SELECT min(n.occurred_at_exact) FROM loop.finding_state AS n"
    "   WHERE n.organization_id = f.organization_id AND n.plant_id = f.plant_id"
    "   AND n.zone_id = f.zone_id AND n.family = f.family AND n.subject_kind = 'finding'"
    "   AND f.closed_at IS NOT NULL AND n.occurred_at_exact > f.closed_at) AS recurred_at"
    " FROM loop.finding_state AS f"
    " WHERE f.organization_id = :organization_id AND f.plant_id = :plant_id"
    " AND f.zone_id = :zone_id AND f.subject_id <> :subject_id"
    " AND f.current_action_type IS NOT NULL"
    " AND f.lifecycle_state IN ('closed', 'overdue', 'unclosable')"
    " ORDER BY f.occurred_at_exact DESC, f.finding_state_id LIMIT :limit"
)
_PREVIOUS_CAUSES: Final = text(
    "SELECT root_cause_category, count(*) AS analyses FROM loop.root_cause_analysis"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id AND zone_id = :zone_id"
    " AND state = 'signed' GROUP BY root_cause_category ORDER BY root_cause_category"
)


class AnalysisRejected(Exception):
    """Un rechazo de negocio del análisis, con su ``detail_code`` (``LoopDetailCode``) y, si lo
    rechazó un validador de texto libre, su pista para quien escribió (``hint``, VIG-344)."""

    def __init__(self, detail_code: LoopDetailCode, hint: FreeTextHint | None = None) -> None:
        super().__init__(detail_code.value)
        self.detail_code = detail_code
        self.hint = hint


class AnalysisRequestInvalid(Exception):
    """El cuerpo no tiene la forma del esquema: ``invalid_request`` sin ``detail_code``."""

    api_code: Final = ApiErrorCode.INVALID_REQUEST


class AnalysisUnavailable(ExternalDependencyDown):
    """Transitorio: la cadena de la planta no se pudo tomar a tiempo."""


@dataclass(frozen=True, slots=True)
class _Subject:
    finding_state_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    subject_kind: str
    subject_id: uuid.UUID
    anchor_record_id: uuid.UUID
    family: str
    tier: str
    standard_id: uuid.UUID
    standard_version: int
    observability_state_at_event: str


@dataclass(frozen=True, slots=True)
class _Row:
    """La fila de ``loop.root_cause_analysis`` y su borrador como lo lee el dominio."""

    analysis_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    subject_kind: str
    subject_id: uuid.UUID
    record_id: uuid.UUID | None
    zone_context: Mapping[str, Any]
    analysis: GuidedAnalysis
    missing_information: str | None
    signer: Mapping[str, Any] | None
    signed_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AnalysisView:
    """El análisis tal como lo ve el coordinador (SUP-U05-16 a 18)."""

    analysis_id: uuid.UUID
    subject_kind: str
    subject_id: uuid.UUID
    zone_id: uuid.UUID
    state: RootCauseAnalysisState
    zone_context: Mapping[str, Any]
    analysis: GuidedAnalysis
    pending_information: tuple[AnalysisMissingItem, ...]
    """Lo que falta para firmar, de la lista cerrada; vacía si nada falta o ya está firmado."""
    missing_information: str | None
    record_id: uuid.UUID | None
    signer: Mapping[str, Any] | None
    signed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @property
    def next_step_index(self) -> int | None:
        """El paso que el flujo pide ahora; ``None`` con los cinco porqués o ya firmado."""
        if self.state is RootCauseAnalysisState.SIGNED:
            return None
        count = len(self.analysis.steps)
        return count + 1 if count < MAX_WHY_STEPS else None


@dataclass(frozen=True, slots=True)
class InProgressView:
    """El análisis en curso del sujeto, si lo hay, y su última solicitud al asistente (VIG-363)."""

    analysis: AnalysisView | None
    assistant_request_id: uuid.UUID | None
    """La solicitud cuyo borrador marcado se vuelve a consultar al retomar; ``None`` sin ninguna."""


def _uuid(value: object) -> uuid.UUID:
    return uuid.UUID(str(value))


def _json(value: object) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _refs(values: Sequence[Mapping[str, Any]]) -> tuple[EvidenceReference, ...]:
    return tuple(EvidenceReference(EvidenceRefKind(v["kind"]), _uuid(v["id"])) for v in values)


def _row(row: Any) -> _Row:
    data = row._mapping
    steps = tuple(
        WhyStep(
            step_index=int(step["step_index"]),
            statement=str(step["statement"]),
            evidence_refs=_refs(step["evidence_refs"]),
            category=None if step.get("category") is None else CauseCategory(step["category"]),
        )
        for step in _json(data["why_steps"])
    )
    branches = tuple(
        Branch(
            category=CauseCategory(branch["branch_category"]),
            factors=tuple(
                Factor(str(factor["text"]), _refs(factor["evidence_refs"]))
                for factor in branch["factors"]
            ),
        )
        for branch in _json(data["cause_effect_branches"])
    )
    category = data["root_cause_category"]
    record_id = data["record_id"]
    signer = data["signer"]
    return _Row(
        analysis_id=_uuid(data["analysis_id"]),
        organization_id=_uuid(data["organization_id"]),
        plant_id=_uuid(data["plant_id"]),
        zone_id=_uuid(data["zone_id"]),
        subject_kind=str(data["subject_kind"]),
        subject_id=_uuid(data["subject_id"]),
        record_id=None if record_id is None else _uuid(record_id),
        zone_context=_json(data["zone_context"]),
        analysis=GuidedAnalysis(
            state=RootCauseAnalysisState(data["state"]),
            steps=steps,
            branches=branches,
            root_cause_category=None if category is None else CauseCategory(category),
            root_cause_statement=data["root_cause_statement"],
            conclusion=data["conclusion"],
        ),
        missing_information=data["missing_information"],
        signer=None if signer is None else _json(signer),
        signed_at=data["signed_at"],
        created_at=data["created_at"],
        updated_at=data["updated_at"],
    )


def _view(row: _Row) -> AnalysisView:
    analysis = row.analysis
    pending = () if analysis.state is RootCauseAnalysisState.SIGNED else missing_items(analysis)
    return AnalysisView(
        analysis_id=row.analysis_id,
        subject_kind=row.subject_kind,
        subject_id=row.subject_id,
        zone_id=row.zone_id,
        state=analysis.state,
        zone_context=row.zone_context,
        analysis=analysis,
        pending_information=pending,
        missing_information=row.missing_information,
        record_id=row.record_id,
        signer=row.signer,
        signed_at=row.signed_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# --- comprobaciones de la entrada (sin base; las usa también el doble del arnés) ----------------


def _rejected(rejected: RootCauseRejected) -> AnalysisRejected:
    return AnalysisRejected(_DOMAIN_CODES[rejected.code])


def checked_category(value: object) -> CauseCategory:
    """Una de las tres categorías; si no, ``cause_category_not_allowed`` (PR-LAZ-16)."""
    try:
        return cause_category(value)
    except RootCauseRejected as rejected:
        raise _rejected(rejected) from None


def checked_text(
    free_text: FreeTextPolicyRegistry,
    value: object,
    field: FreeTextField,
    organization_id: uuid.UUID | None,
) -> str:
    """El texto en NFC si pasa la política base y los validadores; si no, su rechazo."""
    if not isinstance(value, str):
        raise AnalysisRequestInvalid("el texto es una cadena")
    try:
        return free_text.apply(value, field, organization_id=organization_id)
    except FreeTextRejected as rejected:
        code = _VALIDATOR_CODES.get(rejected.detail_code or "")
        if code is not None:
            raise AnalysisRejected(code, rejected.hint) from None
        raise AnalysisRequestInvalid("texto rechazado por la política base") from None


def checked_evidence_refs(values: object) -> tuple[EvidenceReference, ...]:
    """Las referencias ``{kind, id}`` de la lista cerrada ``evidence_ref_kind`` (≤ 32)."""
    if not isinstance(values, Sequence) or isinstance(values, str):
        raise AnalysisRequestInvalid("las referencias de evidencia son una lista")
    if len(values) > MAX_EVIDENCE_REFS:
        raise AnalysisRequestInvalid("demasiadas referencias de evidencia")
    refs: list[EvidenceReference] = []
    for value in values:
        if not isinstance(value, Mapping) or set(value) != {"kind", "id"}:
            raise AnalysisRequestInvalid("una referencia es {kind, id}")
        kind, ref_id = value["kind"], value["id"]
        if not isinstance(kind, str) or kind not in {k.value for k in EvidenceRefKind}:
            raise AnalysisRequestInvalid("kind fuera de la lista cerrada")
        if type(ref_id) is not uuid.UUID:
            raise AnalysisRequestInvalid("id de la referencia es un UUID")
        refs.append(EvidenceReference(EvidenceRefKind(kind), ref_id))
    return tuple(refs)


def checked_branches(
    free_text: FreeTextPolicyRegistry,
    values: Sequence[Mapping[str, Any]],
    organization_id: uuid.UUID | None,
) -> tuple[Branch, ...]:
    """Las ramas del diagrama: categoría de las tres y factores validados (PR-LAZ-16)."""
    branches: list[Branch] = []
    for value in values:
        category = checked_category(value.get("branch_category"))
        factors = value.get("factors")
        if not isinstance(factors, Sequence) or not 1 <= len(factors) <= MAX_FACTORS:
            raise AnalysisRequestInvalid("una rama tiene de 1 a 16 factores")
        branches.append(
            Branch(
                category,
                tuple(
                    Factor(
                        checked_text(free_text, factor.get("text"), FACTOR_FIELD, organization_id),
                        checked_evidence_refs(factor.get("evidence_refs", ())),
                    )
                    for factor in factors
                ),
            )
        )
    return tuple(branches)


def _optional_text(
    free_text: FreeTextPolicyRegistry,
    value: object,
    field: FreeTextField,
    organization_id: uuid.UUID,
) -> str | None:
    """Un texto final: ausente o en blanco es «falta» (``pending_information``)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return checked_text(free_text, value, field, organization_id)


def _license(value: object) -> str | None:
    """La licencia por la política base; ausente o en blanco, ``None`` (falta la licencia)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise AnalysisRequestInvalid("la licencia es una cadena")
    try:
        return apply_base_policy(value, LICENSE_FIELD)
    except FreeTextRejected:
        raise AnalysisRequestInvalid("licencia rechazada por la política base") from None


def _written(written: Receipt | LedgerRejection) -> Receipt:
    """Un rechazo o un duplicado revierten la transacción (nada queda escrito)."""
    if isinstance(written, LedgerRejection):
        if written.code is LedgerRejectionCode.FREE_TEXT_REJECTED:
            code = _VALIDATOR_CODES.get(written.detail_code or "")
            if code is not None:
                raise AnalysisRejected(code, written.hint)
            raise AnalysisRequestInvalid("texto libre rechazado por el expediente")
        if written.code is LedgerRejectionCode.CHAIN_LOCKED_TIMEOUT:
            raise AnalysisUnavailable("ledger_chain")
        raise RuntimeError(f"root_cause_analysis rechazado: {written.code.value}")
    if written.status is not AcceptanceStatus.ACCEPTED:
        raise RuntimeError("root_cause_analysis con un identificador ya usado")
    return written


async def linked_analysis(
    transaction: Transaction,
    *,
    plant_id: uuid.UUID,
    zone_id: uuid.UUID,
    subject_id: uuid.UUID,
    record_id: uuid.UUID,
) -> uuid.UUID:
    """El ``analysis_id`` del análisis **firmado** del sujeto cuyo registro es ``record_id``.

    Es el enlace opcional de la acción (``root_cause_analysis_record_id`` de ``action_assigned``,
    DE §5.2): un registro que no es el de un análisis firmado del mismo sujeto, de la misma zona
    y de la organización de la transacción responde ``ResourceNotFound``."""
    result = await transaction.execute(
        _LINKED,
        {
            "organization_id": transaction.context.organization_id,
            "plant_id": plant_id,
            "zone_id": zone_id,
            "subject_id": subject_id,
            "record_id": record_id,
        },
    )
    row = result.first()
    if row is None:
        raise ResourceNotFound()
    return _uuid(row.analysis_id)


@repository
class RootCauseService:
    """``loop.root_cause``: ``iniciar_analisis``, ``avanzar_paso`` y ``firmar``."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        writer: EscritorExpediente,
        authorizer: Authorizer,
        free_text: FreeTextPolicyRegistry,
        catalog: CatalogQueryPort,
        fleet: FleetQueryPort,
        coverage: CoveragePort,
        clock: Clock,
        labels: PlatformLabels | None = None,
        store: LoopProjectionStore | None = None,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._database = database
        self._writer = writer
        self._authorizer = authorizer
        self._free_text = free_text
        self._catalog = catalog
        self._fleet = fleet
        self._coverage = coverage
        self._clock = clock
        self._labels = labels if labels is not None else PlatformLabels.load()
        self._store = store if store is not None else LoopProjectionStore()
        self._random_bytes = random_bytes

    def __repr__(self) -> str:
        return "RootCauseService()"

    # --- SUP-U05-16 ------------------------------------------------------------------------------

    async def start(self, context: ScopeContext, finding_state_id: uuid.UUID) -> AnalysisView:
        """Crea el borrador del análisis del sujeto con el contexto objetivo de su zona."""
        authorized, subject = await self._authorized_subject(context, finding_state_id)
        at = to_millisecond(self._clock.now())
        zone_context = await self._zone_context(authorized, subject, at)
        analysis_id = uuid7(self._clock, self._random_bytes)
        writer_context = with_unit(authorized, ActorUnit.U04)
        async with self._database.transaction(writer_context) as transaction:
            await transaction.execute(
                _INSERT,
                {
                    "analysis_id": analysis_id,
                    "organization_id": subject.organization_id,
                    "plant_id": subject.plant_id,
                    "zone_id": subject.zone_id,
                    "subject_kind": subject.subject_kind,
                    "subject_id": subject.subject_id,
                    "zone_context": json.dumps(zone_context),
                    "at": at,
                },
            )
            row = await self._locked(transaction, analysis_id)
        return _view(row)

    async def in_progress(
        self, context: ScopeContext, finding_state_id: uuid.UUID
    ) -> InProgressView:
        """El análisis sin firmar más reciente del sujeto y su última solicitud al asistente.

        Lectura para retomar el análisis guardado sin terminar (VIG-363): mismo alcance que
        ``start`` (``root_cause.analyze`` sobre la zona del sujeto; fuera de él, ``not_found``) y
        sin escribir nada. Un análisis firmado ya no está en curso.
        """
        authorized, subject = await self._authorized_subject(context, finding_state_id)
        rows = await self._database.read(
            authorized,
            _IN_PROGRESS,
            {
                "organization_id": subject.organization_id,
                "plant_id": subject.plant_id,
                "zone_id": subject.zone_id,
                "subject_id": subject.subject_id,
            },
        )
        if not rows:
            return InProgressView(analysis=None, assistant_request_id=None)
        row = _row(rows[0])
        requests = await self._database.read(
            authorized,
            _LATEST_REQUEST,
            {
                "organization_id": row.organization_id,
                "plant_id": row.plant_id,
                "analysis_id": row.analysis_id,
            },
        )
        request_id = _uuid(requests[0]._mapping["request_id"]) if requests else None
        return InProgressView(analysis=_view(row), assistant_request_id=request_id)

    # --- SUP-U05-17 ------------------------------------------------------------------------------

    async def put_step(
        self,
        context: ScopeContext,
        analysis_id: uuid.UUID,
        step_index: int,
        *,
        statement: object,
        evidence_refs: object,
        category: object,
    ) -> AnalysisView:
        """Escribe el porqué ``step_index`` (el siguiente o uno ya completo) del borrador."""
        authorized = await self._authorized_analysis(context, analysis_id)
        refs = checked_evidence_refs(evidence_refs)
        writer_context = with_unit(authorized, ActorUnit.U04)
        async with self._database.transaction(writer_context) as transaction:
            row = await self._locked(transaction, analysis_id)
            analysis = row.analysis
            if analysis.state is RootCauseAnalysisState.SIGNED:
                raise AnalysisRejected(LoopDetailCode.TRANSITION_NOT_ALLOWED)
            if type(step_index) is not int or not (
                1 <= step_index <= min(len(analysis.steps) + 1, MAX_WHY_STEPS)
            ):
                raise AnalysisRejected(LoopDetailCode.ANALYSIS_STEP_OUT_OF_ORDER)
            await self._prepare(transaction)
            text_ = checked_text(
                self._free_text, statement, STATEMENT_FIELD, writer_context.organization_id
            )
            chosen = None if category is None else checked_category(category)
            try:
                updated = put_step(analysis, WhyStep(step_index, text_, refs, chosen))
            except RootCauseRejected as rejected:  # pragma: no cover - comprobado arriba
                raise _rejected(rejected) from None
            state, missing_information = self._recomputed(updated)
            at = to_millisecond(self._clock.now())
            await transaction.execute(
                _UPDATE_STEPS,
                {
                    "organization_id": row.organization_id,
                    "analysis_id": row.analysis_id,
                    "why_steps": json.dumps(
                        [
                            {**step.to_json(), "question_es": step.question_es}
                            for step in updated.steps
                        ]
                    ),
                    "state": state.value,
                    "missing_information": missing_information,
                    "at": at,
                },
            )
            row = await self._locked(transaction, analysis_id)
        return _view(row)

    # --- SUP-U05-18 ------------------------------------------------------------------------------

    async def sign(
        self,
        context: ScopeContext,
        analysis_id: uuid.UUID,
        *,
        professional_license: object,
        root_cause_category: object,
        root_cause_statement: object,
        conclusion: object,
        cause_effect_branches: Sequence[Mapping[str, Any]],
    ) -> AnalysisView:
        """Firma el análisis con el texto final aceptado, o lo deja pendiente de información."""
        authorized = await self._authorized_analysis(context, analysis_id)
        writer_context = with_unit(authorized, ActorUnit.U04)
        organization = writer_context.organization_id
        pending = False
        async with self._database.transaction(writer_context) as transaction:
            row = await self._subject_locked(transaction, analysis_id)
            if row.analysis.state is RootCauseAnalysisState.SIGNED:
                raise AnalysisRejected(LoopDetailCode.TRANSITION_NOT_ALLOWED)
            await self._prepare(transaction)
            category = (
                None if root_cause_category is None else checked_category(root_cause_category)
            )
            branches = checked_branches(self._free_text, cause_effect_branches, organization)
            statement = _optional_text(
                self._free_text, root_cause_statement, ROOT_STATEMENT_FIELD, organization
            )
            final = _optional_text(self._free_text, conclusion, CONCLUSION_FIELD, organization)
            license_ = _license(professional_license)
            actor = writer_context.actor
            try:
                outcome = sign(
                    row.analysis,
                    branches=branches,
                    root_cause_category=category,
                    root_cause_statement=statement,
                    conclusion=final,
                    signer_name=actor.display_name_snapshot,
                    professional_license=license_,
                )
            except RootCauseRejected as rejected:
                raise _rejected(rejected) from None
            at = to_millisecond(self._clock.now())
            if isinstance(outcome, Pending):
                await self._save_final(
                    transaction,
                    row,
                    outcome.analysis,
                    missing_information=self._missing_es(outcome.missing),
                    at=at,
                )
                pending = True
            else:
                if license_ is None:  # pragma: no cover - sign ya lo exige
                    raise AnalysisRejected(LoopDetailCode.SIGNER_LICENSE_MISSING)
                signer = {
                    "user_id": str(actor.id),
                    "display_name": actor.display_name_snapshot,
                    "professional_license": license_,
                }
                record_id = await self._write(
                    transaction, writer_context, row, outcome.analysis, signer, at
                )
                await self._save_final(
                    transaction,
                    row,
                    outcome.analysis,
                    record_id=record_id,
                    signer=signer,
                    at=at,
                )
            row = await self._locked(transaction, analysis_id)
        if pending:
            # El estado pendiente y lo que falta quedan confirmados; la firma no procede.
            raise AnalysisRejected(LoopDetailCode.ANALYSIS_INFORMATION_INSUFFICIENT)
        return _view(row)

    # --- alcance ---------------------------------------------------------------------------------

    async def _authorized_subject(
        self, context: ScopeContext, finding_state_id: uuid.UUID
    ) -> tuple[ScopeContext, _Subject]:
        """La fila del sujeto de la organización y ``root_cause.analyze`` sobre su zona."""
        if not isinstance(context, ScopeContext) or type(finding_state_id) is not uuid.UUID:
            raise ResourceNotFound()
        rows = await self._database.read(
            context,
            _SUBJECT,
            {"organization_id": context.organization_id, "finding_state_id": finding_state_id},
        )
        if not rows:
            raise ResourceNotFound()
        data = rows[0]._mapping
        subject = _Subject(
            finding_state_id=_uuid(data["finding_state_id"]),
            organization_id=_uuid(data["organization_id"]),
            plant_id=_uuid(data["plant_id"]),
            zone_id=_uuid(data["zone_id"]),
            subject_kind=str(data["subject_kind"]),
            subject_id=_uuid(data["subject_id"]),
            anchor_record_id=_uuid(data["anchor_record_id"]),
            family=str(data["family"]),
            tier=str(data["tier"]),
            standard_id=_uuid(data["standard_id"]),
            standard_version=int(data["standard_version"]),
            observability_state_at_event=str(data["observability_state_at_event"]),
        )
        authorized = await self._authorize(
            context,
            Resource(
                subject.organization_id,
                SUBJECT_RESOURCE_KIND,
                subject.finding_state_id,
                plant_id=subject.plant_id,
                zone_id=subject.zone_id,
            ),
        )
        return authorized, subject

    async def _authorized_analysis(
        self, context: ScopeContext, analysis_id: uuid.UUID
    ) -> ScopeContext:
        """El análisis de la organización y ``root_cause.analyze`` sobre su zona."""
        if not isinstance(context, ScopeContext) or type(analysis_id) is not uuid.UUID:
            raise ResourceNotFound()
        rows = await self._database.read(
            context,
            _ANALYSIS,
            {"organization_id": context.organization_id, "analysis_id": analysis_id},
        )
        if not rows:
            raise ResourceNotFound()
        row = _row(rows[0])
        return await self._authorize(
            context,
            Resource(
                row.organization_id,
                ANALYSIS_RESOURCE_KIND,
                row.analysis_id,
                plant_id=row.plant_id,
                zone_id=row.zone_id,
            ),
        )

    async def _authorize(self, context: ScopeContext, resource: Resource) -> ScopeContext:
        authorized = await self._authorizer.authorize(
            context, PermissionKey.ROOT_CAUSE_ANALYZE, resource
        )
        if authorized.concession_id is not None:  # pragma: no cover - la matriz ya lo deniega
            await self._authorizer.deny(authorized, PermissionKey.ROOT_CAUSE_ANALYZE, resource)
        return authorized

    # --- lecturas bajo candado -------------------------------------------------------------------

    async def _locked(self, transaction: Transaction, analysis_id: uuid.UUID) -> _Row:
        """La fila del análisis con ``FOR UPDATE`` (candado 2)."""
        result = await transaction.execute(
            _ANALYSIS_FOR_UPDATE,
            {"organization_id": transaction.context.organization_id, "analysis_id": analysis_id},
        )
        row = result.first()
        if row is None:  # pragma: no cover - ninguna fila se borra
            raise ResourceNotFound()
        return _row(row)

    async def _subject_locked(self, transaction: Transaction, analysis_id: uuid.UUID) -> _Row:
        """La exclusión del sujeto (candado 1) y después la fila del análisis (candado 2)."""
        current = await self._locked_free(transaction, analysis_id)
        await transaction.execute(
            _LOCK_SUBJECT,
            {
                "organization_id": str(current.organization_id),
                "subject_id": str(current.subject_id),
            },
        )
        return await self._locked(transaction, analysis_id)

    @staticmethod
    async def _locked_free(transaction: Transaction, analysis_id: uuid.UUID) -> _Row:
        """El sujeto del análisis, sin candado (no cambia nunca: solo localiza la exclusión)."""
        result = await transaction.execute(
            _ANALYSIS,
            {"organization_id": transaction.context.organization_id, "analysis_id": analysis_id},
        )
        row = result.first()
        if row is None:  # pragma: no cover - ninguna fila se borra
            raise ResourceNotFound()
        return _row(row)

    async def _prepare(self, transaction: Transaction) -> None:
        """Los preparadores de los validadores (la versión de la lista de vocabulario) en la
        transacción de la escritura, como hace el escritor (PAT-LAZ-REN-02)."""
        if not self._free_text.has_preparers:
            return

        async def rows(statement: Any, parameters: Mapping[str, Any]) -> Sequence[Any]:
            return (await transaction.execute(statement, parameters)).all()

        await self._free_text.prepare(
            FreeTextPreparation(
                organization_id=transaction.context.organization_id,
                rows=rows,
                clock=self._clock,
            )
        )

    # --- escrituras ------------------------------------------------------------------------------

    def _missing_es(self, missing: Sequence[AnalysisMissingItem]) -> str:
        return missing_information_es(
            missing, lambda item: self._labels.label("analysis_missing_item", item)
        )

    def _recomputed(self, analysis: GuidedAnalysis) -> tuple[RootCauseAnalysisState, str | None]:
        """Un borrador sigue en borrador; uno pendiente vuelve a enumerar lo que le falta (y
        vuelve a borrador cuando ya no le falta nada)."""
        if analysis.state is not RootCauseAnalysisState.PENDING_INFORMATION:
            return analysis.state, None
        missing = missing_items(analysis)
        if not missing:
            return RootCauseAnalysisState.DRAFT, None
        return RootCauseAnalysisState.PENDING_INFORMATION, self._missing_es(missing)

    @staticmethod
    async def _save_final(
        transaction: Transaction,
        row: _Row,
        analysis: GuidedAnalysis,
        *,
        at: datetime,
        missing_information: str | None = None,
        record_id: uuid.UUID | None = None,
        signer: Mapping[str, str] | None = None,
    ) -> None:
        await transaction.execute(
            _UPDATE_FINAL,
            {
                "organization_id": row.organization_id,
                "analysis_id": row.analysis_id,
                "state": analysis.state.value,
                "branches": json.dumps([branch.to_json() for branch in analysis.branches]),
                "category": (
                    None
                    if analysis.root_cause_category is None
                    else analysis.root_cause_category.value
                ),
                "statement": analysis.root_cause_statement,
                "conclusion": analysis.conclusion,
                "missing_information": missing_information,
                "record_id": record_id,
                "signer": None if signer is None else json.dumps(dict(signer)),
                "signed_at": None if record_id is None else at,
                "at": at,
            },
        )

    async def _write(
        self,
        transaction: Transaction,
        context: ScopeContext,
        row: _Row,
        analysis: GuidedAnalysis,
        signer: Mapping[str, str],
        at: datetime,
    ) -> uuid.UUID:
        """El registro ``root_cause_analysis`` con su evento y su proyección (misma ``tx``)."""
        anchors = (
            await transaction.execute(
                _ANCHOR,
                {
                    "organization_id": row.organization_id,
                    "plant_id": row.plant_id,
                    "zone_id": row.zone_id,
                    "subject_id": row.subject_id,
                },
            )
        ).all()
        if len(anchors) != 1:  # pragma: no cover - la proyección tiene una fila por sujeto
            raise RuntimeError("el sujeto del análisis no tiene una fila en la proyección")
        record_id = uuid7(self._clock, self._random_bytes)
        content = record_content(
            analysis,
            analysis_id=row.analysis_id,
            subject_kind=row.subject_kind,
            subject_id=row.subject_id,
            anchor_record_id=_uuid(anchors[0].anchor_record_id),
            signer=signer,
            signed_at=format_timestamp(at),
        )
        if analysis.root_cause_category is None:  # pragma: no cover - record_content lo exige
            raise RuntimeError("sin categoría de la causa raíz")
        event = NewEvent(
            event_name="root_cause_analysis_signed",
            payload={
                "zone_id": str(row.zone_id),
                "subject_kind": row.subject_kind,
                "subject_id": str(row.subject_id),
                "analysis_id": str(row.analysis_id),
                "record_id": str(record_id),
                "root_cause_category": analysis.root_cause_category.value,
            },
        )
        written, step = await self._store.write_and_apply(
            transaction,
            self._writer,
            context,
            ROOT_CAUSE_ANALYSIS,
            content,
            scope=RecordScope(plant_id=row.plant_id, zone_id=row.zone_id),
            events=(event,),
            occurred_at=at,
            record_id=record_id,
        )
        receipt = _written(written)
        if step.finding is None or not step.finding.has_signed_root_cause:
            raise RuntimeError("la proyección no aplicó el análisis recién firmado")
        return receipt.record_id

    # --- el contexto objetivo (BR-LAZ-63) --------------------------------------------------------

    async def _zone_context(
        self, context: ScopeContext, subject: _Subject, at: datetime
    ) -> dict[str, Any]:
        period = CoveragePeriod(at - timedelta(days=CONTEXT_PERIOD_DAYS), at)
        try:
            standard = await self._catalog.standard_version(
                context, subject.zone_id, subject.standard_id, subject.standard_version
            )
        except ResourceNotFound:
            raise AnalysisRejected(LoopDetailCode.STANDARD_TEXT_UNAVAILABLE) from None
        nodes = await self._fleet.nodes_by_zone(context, subject.zone_id)
        try:
            coverage = await self._coverage.linea_de_tiempo(context, subject.zone_id, period)
        except CoverageZoneNotFound:  # pragma: no cover - la zona ya se autorizó
            raise ResourceNotFound() from None
        facts = await self._projection_facts(context, subject, period)
        summary = coverage.summary
        return {
            "zone_id": str(subject.zone_id),
            "family": subject.family,
            "tier": subject.tier,
            "standard_id": str(subject.standard_id),
            "standard_version": subject.standard_version,
            "standard_title_es": standard.title_es,
            "standard_text": standard.declared_text,
            "observability_state_at_event": subject.observability_state_at_event,
            "period": {"from": format_timestamp(period.start), "to": format_timestamp(period.end)},
            "coverage_summary_period": {
                "observable_ms": summary.observable_ms,
                "degraded_ms": summary.degraded_ms,
                "not_observable_ms": summary.not_observable_ms,
                "no_communication_ms": summary.no_communication_ms,
                "never_reported_ms": summary.never_reported_ms,
                "zone_not_active_ms": summary.zone_not_active_ms,
            },
            "node_state": [
                {
                    "communication_state": node.communication_state.value,
                    "last_heartbeat_at": (
                        None
                        if node.last_heartbeat_at is None
                        else format_timestamp(node.last_heartbeat_at)
                    ),
                }
                for node in nodes
            ],
            **facts,
        }

    async def _projection_facts(
        self, context: ScopeContext, subject: _Subject, period: CoveragePeriod
    ) -> dict[str, Any]:
        """Señales, duraciones e historial de la zona, en una transacción de lectura."""
        place = {
            "organization_id": subject.organization_id,
            "plant_id": subject.plant_id,
            "zone_id": subject.zone_id,
            "subject_id": subject.subject_id,
        }
        async with self._database.transaction(context) as transaction:
            anchor = (
                await transaction.execute(
                    _SIGNALS,
                    {
                        "organization_id": subject.organization_id,
                        "record_id": subject.anchor_record_id,
                    },
                )
            ).first()
            episode = (await transaction.execute(_EPISODE, place)).first()
            families = (
                await transaction.execute(
                    _FAMILY_COUNTS,
                    {**place, "period_start": period.start, "period_end": period.end},
                )
            ).all()
            actions = (
                await transaction.execute(
                    _PREVIOUS_ACTIONS, {**place, "limit": MAX_PREVIOUS_ACTIONS}
                )
            ).all()
            causes = (await transaction.execute(_PREVIOUS_CAUSES, place)).all()
        document: Mapping[str, Any] = {} if anchor is None else json.loads(bytes(anchor.content))
        recurrences = sum(int(r.others) for r in families if r.family == subject.family)
        return {
            "signals_by_role": [
                {"role": signal["role"], "value": signal["value"], "valid": signal["valid"]}
                for signal in document.get("signals", ())
            ],
            "episode_durations": {
                "episode_ms": None if episode is None else int(episode.total_episode_duration_ms),
                "condition_ms": (
                    None if episode is None else int(episode.total_condition_duration_ms)
                ),
            },
            "finding_counts_by_family_in_period": [
                {"family": str(r.family), "count": int(r.findings)} for r in families
            ],
            "recurrences_in_period": recurrences,
            "previous_actions": [_previous_action(r) for r in actions],
            "previous_cause_categories": [
                {"category": str(r.root_cause_category), "count": int(r.analyses)} for r in causes
            ],
        }


def _previous_action(row: Any) -> dict[str, Any]:
    closed_at: datetime | None = row.closed_at
    recurred_at: datetime | None = row.recurred_at
    action: dict[str, Any] = {
        "action_type": str(row.current_action_type),
        "outcome": str(row.lifecycle_state),
    }
    if closed_at is not None:
        action["closed_at"] = format_timestamp(closed_at)
        if recurred_at is not None:
            action["recurrence_after_days"] = (recurred_at - closed_at).days
    return action
