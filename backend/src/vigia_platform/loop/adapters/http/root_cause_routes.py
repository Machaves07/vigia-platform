"""Las rutas del análisis de causa raíz guiado (SUP-U05-16 a 18; LC-LAZ §6.6; LC-LAZ-12).

Las tres exigen ``root_cause.analyze`` sobre la zona del sujeto (coordinación SST):

- ``POST /loop/findings/{finding_state_id}/root-cause-analyses``: crea el borrador con el
  contexto objetivo de la zona (``ZoneObjectiveContext``). ``201`` con el análisis.
- ``PUT /loop/root-cause-analyses/{analysis_id}/steps/{step_index}``: el porqué ``step_index``
  (1 a 5) con ``statement`` (20 a 1000 caracteres, validado), ``evidence_refs`` (``{kind, id}``,
  ≤ 32) y ``category`` opcional (``process``, ``engineering`` u ``organization``). ``200`` con el
  análisis.
- ``POST /loop/root-cause-analyses/{analysis_id}/signature``: el texto final que el humano acepta
  (``root_cause_category``, ``root_cause_statement``, ``conclusion`` y ``cause_effect_branches``)
  y ``professional_license``; el nombre es el de la sesión. ``201`` con el análisis firmado y su
  ``record_id``.

Toda respuesta con el análisis lleva ``pending_information``: lo que falta para firmar, de la lista
cerrada ``analysis_missing_item`` (vacía si nada falta o ya está firmado).

Rechazos de negocio, como ``invalid_request`` con su ``detail_code`` (400, A-68):
``loop_cause_category_not_allowed``, ``loop_blocked_vocabulary_term``,
``loop_person_attribution_detected``, ``loop_signer_license_missing`` y
``loop_analysis_information_insufficient`` (la firma no procede: el análisis queda
``pending_information`` con lo que falta). Como ``conflict`` (409):
``loop_analysis_step_out_of_order`` y ``loop_transition_not_allowed`` (análisis ya firmado). Sin
el texto del estándar, ``loop_standard_text_unavailable`` (503). Un cuerpo que no tiene la forma
del esquema (paso fuera de 1 a 5, ``kind`` desconocido, textos fuera de sus límites, campos de
más), ``invalid_request`` sin ``detail_code``. Un sujeto o un análisis inexistente, de otra
organización, fuera del alcance o pedido bajo concesión de proveedor responde ``not_found``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Path, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store
from vigia_platform.loop.adapters.http.services import LoopHttp, installed, loop_http
from vigia_platform.loop.application.root_cause import (
    AnalysisRejected,
    AnalysisRequestInvalid,
    AnalysisView,
)
from vigia_platform.loop.detail_codes import HTTP_STATUS_BY_DETAIL_CODE, LoopDetailCode
from vigia_platform.loop.domain.root_cause import (
    MAX_BRANCHES,
    MAX_EVIDENCE_REFS,
    MAX_FACTORS,
    MAX_WHY_STEPS,
    QUESTIONS_ES,
    EvidenceReference,
)
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "SIGNATURE_PATH",
    "START_PATH",
    "STEP_PATH",
    "AnalysisOut",
    "AnalysisSignatureBody",
    "AnalysisStepBody",
    "root_cause_router",
]

START_PATH: Final = "/loop/findings/{finding_state_id}/root-cause-analyses"
STEP_PATH: Final = "/loop/root-cause-analyses/{analysis_id}/steps/{step_index}"
SIGNATURE_PATH: Final = "/loop/root-cause-analyses/{analysis_id}/signature"
_ANALYZE: Final = PermissionKey.ROOT_CAUSE_ANALYZE.value
_TEXT_CODES: Final = (
    LoopDetailCode.CAUSE_CATEGORY_NOT_ALLOWED.value,
    LoopDetailCode.BLOCKED_VOCABULARY_TERM.value,
    LoopDetailCode.PERSON_ATTRIBUTION_DETECTED.value,
    LoopDetailCode.TRANSITION_NOT_ALLOWED.value,
)
_START_CODES: Final = (LoopDetailCode.STANDARD_TEXT_UNAVAILABLE.value,)
_STEP_CODES: Final = (LoopDetailCode.ANALYSIS_STEP_OUT_OF_ORDER.value, *_TEXT_CODES)
_SIGNATURE_CODES: Final = (
    LoopDetailCode.ANALYSIS_INFORMATION_INSUFFICIENT.value,
    LoopDetailCode.SIGNER_LICENSE_MISSING.value,
    *_TEXT_CODES,
)

Services = Annotated[LoopHttp, Depends(loop_http)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceRefBody(_Body):
    """Una referencia a un hecho de la zona: nunca su contenido (§2.9)."""

    kind: StrictStr
    """``record``, ``clip``, ``metric`` o ``coverage``."""
    id: uuid.UUID


class AnalysisStepBody(_Body):
    """Un porqué: enunciado, referencias de evidencia y, si lo declara, su categoría."""

    statement: StrictStr
    """De 20 a 1000 caracteres; pasa los dos validadores de texto de U-04."""
    evidence_refs: Annotated[
        tuple[EvidenceRefBody, ...], Field(default=(), max_length=MAX_EVIDENCE_REFS)
    ]
    category: StrictStr | None = None
    """``process``, ``engineering`` u ``organization``: ninguna categoría es una persona."""


class FactorBody(_Body):
    text: StrictStr
    """De 5 a 500 caracteres; validado."""
    evidence_refs: Annotated[
        tuple[EvidenceRefBody, ...], Field(default=(), max_length=MAX_EVIDENCE_REFS)
    ]


class BranchBody(_Body):
    """Una rama del diagrama de causa y efecto."""

    branch_category: StrictStr
    """``process``, ``engineering`` u ``organization``."""
    factors: Annotated[tuple[FactorBody, ...], Field(min_length=1, max_length=MAX_FACTORS)]


class AnalysisSignatureBody(_Body):
    """El texto final que el humano acepta y su licencia profesional."""

    professional_license: StrictStr | None = None
    """Hasta 64 caracteres; sin ella no hay firma."""
    root_cause_category: StrictStr | None = None
    root_cause_statement: StrictStr | None = None
    """De 20 a 2000 caracteres; validado."""
    conclusion: StrictStr | None = None
    """De 20 a 2000 caracteres; validado."""
    cause_effect_branches: Annotated[
        tuple[BranchBody, ...], Field(default=(), max_length=MAX_BRANCHES)
    ]


class EvidenceRefOut(BaseModel):
    kind: str
    id: uuid.UUID


class WhyStepOut(BaseModel):
    step_index: int
    question_es: str
    statement: str
    evidence_refs: list[EvidenceRefOut]
    category: str | None


class FactorOut(BaseModel):
    text: str
    evidence_refs: list[EvidenceRefOut]


class BranchOut(BaseModel):
    branch_category: str
    factors: list[FactorOut]


class SignerOut(BaseModel):
    user_id: uuid.UUID
    display_name: str
    professional_license: str


class SignalOut(BaseModel):
    role: str
    value: str
    valid: StrictBool


class EpisodeDurationsOut(BaseModel):
    episode_ms: StrictInt | None
    condition_ms: StrictInt | None


class PeriodOut(BaseModel):
    from_: str = Field(serialization_alias="from")
    to: str


class CoverageSummaryOut(BaseModel):
    observable_ms: int
    degraded_ms: int
    not_observable_ms: int
    no_communication_ms: int
    never_reported_ms: int
    zone_not_active_ms: int


class FamilyCountOut(BaseModel):
    family: str
    count: int


class PreviousActionOut(BaseModel):
    action_type: str
    outcome: str
    """``closed``, ``overdue`` o ``unclosable``."""
    closed_at: str | None = None
    recurrence_after_days: int | None = None


class CauseCountOut(BaseModel):
    category: str
    count: int


class NodeStateOut(BaseModel):
    communication_state: str
    last_heartbeat_at: str | None


class ZoneContextOut(BaseModel):
    """``ZoneObjectiveContext`` (DE §2.10): solo hechos medidos de la zona (BR-LAZ-63)."""

    zone_id: uuid.UUID
    family: str
    tier: str
    standard_id: uuid.UUID
    standard_version: int
    standard_title_es: str
    standard_text: str
    observability_state_at_event: str
    period: PeriodOut
    coverage_summary_period: CoverageSummaryOut
    node_state: list[NodeStateOut]
    signals_by_role: list[SignalOut]
    episode_durations: EpisodeDurationsOut
    finding_counts_by_family_in_period: list[FamilyCountOut]
    recurrences_in_period: int
    previous_actions: list[PreviousActionOut]
    previous_cause_categories: list[CauseCountOut]


class AnalysisOut(BaseModel):
    analysis_id: uuid.UUID
    subject_kind: str
    subject_id: uuid.UUID
    zone_id: uuid.UUID
    state: str
    """``draft``, ``pending_information`` o ``signed``."""
    zone_context: ZoneContextOut
    why_steps: list[WhyStepOut]
    next_step_index: int | None
    """El porqué que el flujo pide ahora (``None`` con cinco porqués o ya firmado)."""
    next_question_es: str | None
    cause_effect_branches: list[BranchOut]
    root_cause_category: str | None
    root_cause_statement: str | None
    conclusion: str | None
    pending_information: list[str]
    """Lo que falta para firmar (``analysis_missing_item``)."""
    missing_information: str | None
    """La enumeración en español de lo que faltaba al intentar firmar."""
    record_id: uuid.UUID | None
    signer: SignerOut | None
    signed_at: str | None
    created_at: str
    updated_at: str


def _refs(refs: tuple[EvidenceReference, ...]) -> list[EvidenceRefOut]:
    return [EvidenceRefOut(kind=ref.kind.value, id=ref.id) for ref in refs]


def view(result: AnalysisView) -> AnalysisOut:
    analysis = result.analysis
    following = result.next_step_index
    signer = result.signer
    return AnalysisOut(
        analysis_id=result.analysis_id,
        subject_kind=result.subject_kind,
        subject_id=result.subject_id,
        zone_id=result.zone_id,
        state=result.state.value,
        zone_context=ZoneContextOut.model_validate(
            {
                **result.zone_context,
                "period": PeriodOut(
                    from_=result.zone_context["period"]["from"],
                    to=result.zone_context["period"]["to"],
                ),
            }
        ),
        why_steps=[
            WhyStepOut(
                step_index=step.step_index,
                question_es=step.question_es,
                statement=step.statement,
                evidence_refs=_refs(step.evidence_refs),
                category=None if step.category is None else step.category.value,
            )
            for step in analysis.steps
        ],
        next_step_index=following,
        next_question_es=None if following is None else QUESTIONS_ES[following - 1],
        cause_effect_branches=[
            BranchOut(
                branch_category=branch.category.value,
                factors=[
                    FactorOut(text=factor.text, evidence_refs=_refs(factor.evidence_refs))
                    for factor in branch.factors
                ],
            )
            for branch in analysis.branches
        ],
        root_cause_category=(
            None if analysis.root_cause_category is None else analysis.root_cause_category.value
        ),
        root_cause_statement=analysis.root_cause_statement,
        conclusion=analysis.conclusion,
        pending_information=[item.value for item in result.pending_information],
        missing_information=result.missing_information,
        record_id=result.record_id,
        signer=None if signer is None else SignerOut.model_validate(signer),
        signed_at=None if result.signed_at is None else format_timestamp(result.signed_at),
        created_at=format_timestamp(result.created_at),
        updated_at=format_timestamp(result.updated_at),
    )


def _refs_in(refs: tuple[EvidenceRefBody, ...]) -> list[dict[str, Any]]:
    return [{"kind": ref.kind, "id": ref.id} for ref in refs]


def _rejected(rejected: AnalysisRejected) -> ApiError:
    code, _status = HTTP_STATUS_BY_DETAIL_CODE[rejected.detail_code]
    return ApiError(code, detail_code=rejected.detail_code.value)


def root_cause_router() -> APIRouter:
    router = APIRouter(tags=["lazo"])

    @router.post(
        START_PATH,
        status_code=201,
        dependencies=[requires(_ANALYZE, detail_codes=_START_CODES), Depends(exact_query())],
        summary="Inicia el análisis de causa raíz guiado con el contexto objetivo de la zona",
    )
    async def start(
        finding_state_id: uuid.UUID, request: Request, response: Response, services: Services
    ) -> AnalysisOut:
        no_store(response)
        try:
            result = await installed(services.root_cause).start(
                request_context(request), finding_state_id
            )
        except AnalysisRejected as rejected:
            raise _rejected(rejected) from None
        return view(result)

    @router.put(
        STEP_PATH,
        dependencies=[requires(_ANALYZE, detail_codes=_STEP_CODES), Depends(exact_query())],
        summary="Escribe un porqué del análisis (el siguiente o uno ya completo)",
    )
    async def put_step(
        analysis_id: uuid.UUID,
        step_index: Annotated[int, Path(ge=1, le=MAX_WHY_STEPS)],
        body: AnalysisStepBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> AnalysisOut:
        no_store(response)
        try:
            result = await installed(services.root_cause).put_step(
                request_context(request),
                analysis_id,
                step_index,
                statement=body.statement,
                evidence_refs=_refs_in(body.evidence_refs),
                category=body.category,
            )
        except AnalysisRejected as rejected:
            raise _rejected(rejected) from None
        except AnalysisRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        return view(result)

    @router.post(
        SIGNATURE_PATH,
        status_code=201,
        dependencies=[requires(_ANALYZE, detail_codes=_SIGNATURE_CODES), Depends(exact_query())],
        summary="Firma el análisis con nombre y licencia, o lo deja pendiente de información",
    )
    async def signature(
        analysis_id: uuid.UUID,
        body: AnalysisSignatureBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> AnalysisOut:
        no_store(response)
        try:
            result = await installed(services.root_cause).sign(
                request_context(request),
                analysis_id,
                professional_license=body.professional_license,
                root_cause_category=body.root_cause_category,
                root_cause_statement=body.root_cause_statement,
                conclusion=body.conclusion,
                cause_effect_branches=[
                    {
                        "branch_category": branch.branch_category,
                        "factors": [
                            {"text": factor.text, "evidence_refs": _refs_in(factor.evidence_refs)}
                            for factor in branch.factors
                        ],
                    }
                    for branch in body.cause_effect_branches
                ],
            )
        except AnalysisRejected as rejected:
            raise _rejected(rejected) from None
        except AnalysisRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        return view(result)

    return router
