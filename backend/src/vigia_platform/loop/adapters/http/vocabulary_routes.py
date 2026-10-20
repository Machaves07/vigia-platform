"""Las dos rutas de la lista de vocabulario (SUP-U05-36 y 37; LC-LAZ §6.10; LC-LAZ-17).

- ``GET /loop/vocabulary`` (``vocabulary.manage`` sobre la organización): ``200`` con la lista
  vigente completa (sin paginar): ``list_version``, ``lemmatizer_version``, cada término con su
  categoría, forma, valor, lema guardado, alternativa, origen (``derived`` o ``customer_added``) y
  versión de entrada, y los nombres declarados. Sin cambios, la lista inicial en la versión 1.
- ``POST /loop/vocabulary/changes`` (``vocabulary.manage`` sobre la organización): ``added``
  (``category``, ``form``, ``value``, ``suggestion_es``), ``removed`` (``term_id``),
  ``declared_names_added`` (``kind``, ``value``), ``declared_names_removed`` (``name_id``) y
  ``reason`` (10 a 500 caracteres). ``201`` con la versión nueva y su registro.

Rechazos: sin motivo, ``loop_vocabulary_change_reason_required``; un motivo, una alternativa o un
nombre que no pasan los validadores, ``loop_blocked_vocabulary_term`` o
``loop_person_attribution_detected`` (los tres como ``invalid_request`` con su ``detail_code``,
400, A-68); un término ya vigente o un nombre ya declarado, ``loop_vocabulary_term_duplicate``
como ``conflict`` (409). Un cuerpo que no tiene la forma del esquema (categoría, forma o tipo
desconocidos, un lema de más de una palabra, un patrón que no compila, campos de más) o que retira
un término de la lista inicial o uno que no está vigente, ``invalid_request`` sin ``detail_code``.

Las dos son de la organización del contexto (sin identificador en la ruta); bajo concesión de
proveedor responden ``not_found`` (``vocabulary.manage`` no está en la columna
``provider_installer``).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store
from vigia_platform.loop.adapters.http.services import LoopHttp, installed, loop_http
from vigia_platform.loop.detail_codes import HTTP_STATUS_BY_DETAIL_CODE, LoopDetailCode
from vigia_platform.loop.record_types import MAX_TERMS_PER_CHANGE
from vigia_platform.loop.vocabulary.repository import DeclaredName
from vigia_platform.loop.vocabulary.service import (
    ChangeResult,
    NameRequest,
    TermRequest,
    VocabularyRejected,
    VocabularyRequestInvalid,
    VocabularyTermView,
    VocabularyView,
)
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "CHANGES_PATH",
    "PATH",
    "VocabularyChangeBody",
    "VocabularyChangeOut",
    "VocabularyOut",
    "vocabulary_router",
]

PATH: Final = "/loop/vocabulary"
CHANGES_PATH: Final = "/loop/vocabulary/changes"
_MANAGE: Final = PermissionKey.VOCABULARY_MANAGE.value
_DETAIL_CODES: Final = (
    LoopDetailCode.VOCABULARY_CHANGE_REASON_REQUIRED.value,
    LoopDetailCode.VOCABULARY_TERM_DUPLICATE.value,
    LoopDetailCode.BLOCKED_VOCABULARY_TERM.value,
    LoopDetailCode.PERSON_ATTRIBUTION_DETECTED.value,
)

Services = Annotated[LoopHttp, Depends(loop_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


_Id = Annotated[uuid.UUID, Field(strict=False)]
"""Un identificador: en JSON llega como cadena (el modo estricto solo admitiría ``uuid.UUID``)."""


class TermBody(_Strict):
    """Un término que se añade."""

    category: StrictStr
    """``absence_claim``, ``percentage_guarantee``, ``reduction_promise`` o
    ``intent_or_blame_attribution``."""
    form: StrictStr
    """``lemma`` (una palabra: cae con sus flexiones) o ``pattern`` (expresión sobre la forma de
    validación, sin tildes y en minúsculas)."""
    value: StrictStr
    """El término: hasta 64 caracteres un lema y 256 un patrón."""
    suggestion_es: StrictStr
    """La alternativa que se propone a quien lo escriba (hasta 200 caracteres)."""


class DeclaredNameBody(_Strict):
    """Un nombre que se declara: exime al patrón de nombre propio del validador de atribución."""

    kind: StrictStr
    """``plant``, ``zone``, ``equipment``, ``machine`` o ``standard``."""
    value: StrictStr
    """El nombre, de 2 a 120 caracteres («Prensa Schuler Kompakt»)."""


class VocabularyChangeBody(_Strict):
    """Lo que se añade y se retira, y el motivo (obligatorio)."""

    added: Annotated[list[TermBody], Field(max_length=MAX_TERMS_PER_CHANGE)] = []
    removed: Annotated[list[_Id], Field(max_length=MAX_TERMS_PER_CHANGE)] = []
    """``term_id`` de términos que añadió la organización (nunca de la lista inicial)."""
    declared_names_added: Annotated[
        list[DeclaredNameBody], Field(max_length=MAX_TERMS_PER_CHANGE)
    ] = []
    declared_names_removed: Annotated[list[_Id], Field(max_length=MAX_TERMS_PER_CHANGE)] = []
    """``name_id`` de nombres declarados."""
    reason: StrictStr | None = None
    """Motivo del cambio, de 10 a 500 caracteres."""


class TermOut(BaseModel):
    term_id: uuid.UUID
    category: str
    form: str
    value: str
    lemma: str | None
    """El lema guardado (ya plegado) de un ``lemma``; ``null`` en un patrón."""
    suggestion_es: str
    origin: str
    added_in_version: int


class DeclaredNameOut(BaseModel):
    name_id: uuid.UUID
    kind: str
    value: str
    added_in_version: int


class VocabularyOut(BaseModel):
    list_version: int
    lemmatizer_version: str
    """La tabla de flexiones con la que se plegaron los lemas (NFR-LAZ-24)."""
    effective_from: str | None
    record_id: uuid.UUID | None
    """El ``vocabulary_list_changed`` de la versión vigente; ``null`` en la lista inicial."""
    terms: list[TermOut]
    declared_names: list[DeclaredNameOut]


class VocabularyChangeOut(BaseModel):
    change_id: uuid.UUID
    record_id: uuid.UUID
    list_version: int
    changed_at: str
    added: list[TermOut]
    removed: list[uuid.UUID]
    declared_names_added: list[DeclaredNameOut]
    declared_names_removed: list[uuid.UUID]


def _term(term: VocabularyTermView) -> TermOut:
    return TermOut(
        term_id=term.term_id,
        category=term.category.value,
        form=term.form.value,
        value=term.value,
        lemma=term.lemma,
        suggestion_es=term.suggestion_es,
        origin=term.origin.value,
        added_in_version=term.added_in_version,
    )


def _name(name: DeclaredName) -> DeclaredNameOut:
    return DeclaredNameOut(
        name_id=name.name_id,
        kind=name.kind.value,
        value=name.value,
        added_in_version=name.added_in_version,
    )


def view(vocabulary: VocabularyView) -> VocabularyOut:
    return VocabularyOut(
        list_version=vocabulary.list_version,
        lemmatizer_version=vocabulary.lemmatizer_version,
        effective_from=(
            None
            if vocabulary.effective_from is None
            else format_timestamp(vocabulary.effective_from)
        ),
        record_id=vocabulary.record_id,
        terms=[_term(term) for term in vocabulary.terms],
        declared_names=[_name(name) for name in vocabulary.declared_names],
    )


def change_view(result: ChangeResult) -> VocabularyChangeOut:
    return VocabularyChangeOut(
        change_id=result.change_id,
        record_id=result.record_id,
        list_version=result.list_version,
        changed_at=format_timestamp(result.changed_at),
        added=[_term(term) for term in result.added],
        removed=list(result.removed_term_ids),
        declared_names_added=[_name(name) for name in result.declared_names_added],
        declared_names_removed=list(result.removed_name_ids),
    )


def vocabulary_router() -> APIRouter:
    router = APIRouter(tags=["lazo"])

    @router.get(
        PATH,
        dependencies=[requires(_MANAGE), Depends(exact_query())],
        summary="Lista de vocabulario bloqueado y vocabulario declarado de la organización",
    )
    async def vocabulary(request: Request, response: Response, services: Services) -> VocabularyOut:
        no_store(response)
        found = await installed(services.vocabulary).current(request_context(request))
        return view(found)

    @router.post(
        CHANGES_PATH,
        status_code=201,
        dependencies=[requires(_MANAGE, detail_codes=_DETAIL_CODES), Depends(exact_query())],
        summary="Cambia la lista de vocabulario con motivo (versión nueva, registrada)",
    )
    async def change_vocabulary(
        body: VocabularyChangeBody, request: Request, response: Response, services: Services
    ) -> VocabularyChangeOut:
        no_store(response)
        try:
            result = await installed(services.vocabulary).change(
                request_context(request),
                added=tuple(
                    TermRequest(
                        category=term.category,
                        form=term.form,
                        value=term.value,
                        suggestion_es=term.suggestion_es,
                    )
                    for term in body.added
                ),
                removed=tuple(body.removed),
                declared_names_added=tuple(
                    NameRequest(kind=name.kind, value=name.value)
                    for name in body.declared_names_added
                ),
                declared_names_removed=tuple(body.declared_names_removed),
                reason=body.reason,
            )
        except VocabularyRejected as rejected:
            code, _status = HTTP_STATUS_BY_DETAIL_CODE[rejected.detail_code]
            raise ApiError(code, detail_code=rejected.detail_code.value) from None
        except VocabularyRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        return change_view(result)

    return router
