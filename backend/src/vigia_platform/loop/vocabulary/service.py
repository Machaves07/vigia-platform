"""``loop.vocabulary`` (LC-LAZ-17): la lista de vocabulario de la organización (SUP-U05-36 y 37).

``current`` (``consultar_lista_de_vocabulario``, ``GET /loop/vocabulary``, ``vocabulary.manage``
sobre la organización): la lista vigente. Sin fila, la **versión 1**: la lista inicial del
producto (BLM §14, ``origin = derived``), sin vocabulario declarado. Con fila, la inicial más los
términos que añadió el cliente y sus nombres declarados, en la versión de la fila. Cada término
lleva su categoría, su forma (``lemma`` o ``pattern``), su lema guardado (el valor ya plegado; los
patrones no tienen lema), su origen y la versión en que entró; la lista, la tabla de flexiones con
la que se plegaron sus lemas (``lemmatizer_version``, NFR-LAZ-24).

``change`` (``modificar_lista_de_vocabulario``, ``POST /loop/vocabulary/changes``): términos que
se añaden (categoría, forma, valor y alternativa), términos que se retiran (por ``term_id``),
nombres declarados que se añaden (tipo y valor) y que se retiran (por ``name_id``), y el motivo.

1. **Antes de la transacción**: el motivo (sin motivo, en blanco o con menos de 10 caracteres,
   ``vocabulary_change_reason_required``; uno que no pasa la política base de U-02,
   ``invalid_request``); la forma de cada elemento
   (``invalid_request``): un lema es **una palabra** de 2 a 64 caracteres, que se guarda **ya
   plegada** (``lexicon.fold_word``) con la tabla de flexiones vigente; un patrón cumple la
   gramática segura de ``loop.vocabulary.patterns`` (retroceso lineal) y no casa con la cadena
   vacía (un patrón del cliente corre en la ruta
   caliente de toda escritura de su organización); un nombre declarado tiene de 2 a 120
   caracteres y al menos una letra; nada se repite dentro del cambio y el cambio nombra algo.
2. **En una sola transacción**: exclusión de la organización (``loop_vocabulary|<org>``); la
   fila vigente; las reglas contra ella:

   - un término que ya está vigente (la lista inicial o uno del cliente, con la misma forma y el
     mismo valor plegado) o un nombre ya declarado, ``vocabulary_term_duplicate`` (409);
   - retirar un término de la lista inicial (``derived``), un término que no está vigente o un
     nombre que no está declarado, ``invalid_request``: el cliente solo añade, o retira lo que él
     añadió (BR-LAZ-86);

   la versión siguiente (**monótona**: 1 es la inicial, el primer cambio crea la fila en la 2);
   **exactamente un** ``vocabulary_list_changed`` (versión 2 del tipo, cadena de la
   organización) con su evento; y la fila (``UPDATE`` condicional a la versión leída) y los
   términos. Si algo falla, no queda nada.

**El registro no lleva el término como texto libre** (BR-LAZ-83, DE §2.12): ``/added[*]/value`` y
``/removed[*]/value`` son la exclusión declarada (solo esquema y política base); ``/reason`` es la
única ruta narrativa y pasa los dos validadores con la lista **vigente al abrir la escritura** (el
registro se escribe antes de cambiar la fila, así que un fallo nunca deja en la caché de este
proceso una versión que no se confirmó). Las alternativas y los nombres declarados también pasan
los dos validadores (los nombres, como un nombre de zona). Los validadores los aplica **solo el
escritor**, después de sus preparadores: un rechazo suyo vuelve con su ``detail_code``
(``loop_blocked_vocabulary_term`` o ``loop_person_attribution_detected``) y no escribe nada.

**Refresco de la caché** (PAT-LAZ-REN-02): la versión nueva la ve cualquier proceso en su siguiente
escritura, porque ``VocabularyCache`` y ``DeclaredNamesCache`` comprueban la versión de la fila al
abrir cada escritura; el evento ``vocabulary_list_changed`` no hace falta para la corrección (y un
consumidor del outbox, que recibe cada evento una vez, no podría refrescar a todos los procesos).

**Orden de candados**: primero el de la lista de la organización (``loop_vocabulary|<org>``) y
después la cadena de la organización del expediente (la toma el escritor al anexar). Ninguna
operación toma esa cadena antes que este candado.

**Bajo concesión de proveedor** las dos operaciones responden como inexistentes (con su
``authorization_denied``): ``vocabulary.manage`` no está en la columna ``provider_installer``.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final

from vigia_contracts.models.enumerations import AcceptanceStatus

from vigia_platform.identity.authz.authorize import Authorizer, Resource, ResourceNotFound
from vigia_platform.identity.authz.context import with_unit
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.writer import (
    EscritorExpediente,
    LedgerDatabase,
    LedgerRejection,
    LedgerRejectionCode,
    Receipt,
)
from vigia_platform.ledger.free_text import (
    FreeTextField,
    FreeTextHint,
    FreeTextRejected,
    FreeTextRejection,
    apply_base_policy,
)
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.domain.enums import (
    BlockedTermCategory,
    BlockedTermForm,
    BlockedTermOrigin,
    DeclaredNameKind,
)
from vigia_platform.loop.record_types import MAX_TERMS_PER_CHANGE
from vigia_platform.loop.validators import lexicon
from vigia_platform.loop.validators.initial_list import INITIAL_LIST_VERSION, INITIAL_TERMS
from vigia_platform.loop.vocabulary.patterns import UnsafePattern, check_pattern
from vigia_platform.loop.vocabulary.repository import (
    DeclaredName,
    ListRow,
    PostgresVocabularyRepository,
    StoredTerm,
)
from vigia_platform.shared.api.errors import ApiErrorCode, ExternalDependencyDown
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext, repository
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.outbox.publish import NewEvent
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

__all__ = [
    "MAX_CUSTOMER_TERMS",
    "MAX_DECLARED_NAMES",
    "REASON_MAX_CHARS",
    "REASON_MIN_CHARS",
    "VOCABULARY_LIST_CHANGED",
    "ChangeResult",
    "NameRequest",
    "TermRequest",
    "VocabularyRejected",
    "VocabularyRequestInvalid",
    "VocabularyService",
    "VocabularyTermView",
    "VocabularyUnavailable",
    "VocabularyView",
]

VOCABULARY_LIST_CHANGED: Final = "vocabulary_list_changed"
REASON_MIN_CHARS: Final = 10
REASON_MAX_CHARS: Final = 500
"""Límites de ``ChangeReason`` (``loop.record_types``)."""
LEMMA_MIN_CHARS: Final = 2
LEMMA_MAX_CHARS: Final = 64
PATTERN_MAX_CHARS: Final = 256
NAME_MIN_CHARS: Final = 2
NAME_MAX_CHARS: Final = 120
SUGGESTION_MAX_CHARS: Final = 200
MAX_CUSTOMER_TERMS: Final = 1_000
"""Términos vigentes que puede añadir una organización `[estimación propia]`: la lista entera se
compila en memoria de cada proceso y se responde completa (sin paginar, LC-LAZ §6.10)."""
MAX_DECLARED_NAMES: Final = 1_000
"""Nombres declarados vigentes de una organización `[estimación propia]`, por lo mismo."""
_MAX_VERSION: Final = 2**31 - 1

_REASON: Final = FreeTextField(
    VOCABULARY_LIST_CHANGED, "/reason", REASON_MIN_CHARS, REASON_MAX_CHARS
)
_VALUE: Final = FreeTextField(VOCABULARY_LIST_CHANGED, "/added[*]/value", 1, PATTERN_MAX_CHARS)
_SUGGESTION: Final = FreeTextField(
    VOCABULARY_LIST_CHANGED, "/added[*]/suggestion_es", 1, SUGGESTION_MAX_CHARS
)
_NAME: Final = FreeTextField(
    VOCABULARY_LIST_CHANGED, "/declared_names_added[*]/value", NAME_MIN_CHARS, NAME_MAX_CHARS
)

_ONE_WORD: Final = re.compile(r"[^\W_]+")
_LETTER: Final = re.compile(r"[^\W\d_]")
_VALIDATOR_CODES: Final[Mapping[str, LoopDetailCode]] = MappingProxyType(
    {
        LoopDetailCode.BLOCKED_VOCABULARY_TERM.value: LoopDetailCode.BLOCKED_VOCABULARY_TERM,
        LoopDetailCode.PERSON_ATTRIBUTION_DETECTED.value: (
            LoopDetailCode.PERSON_ATTRIBUTION_DETECTED
        ),
    }
)
"""Los ``detail_code`` de los dos validadores de U-04 que un rechazo conserva."""

_INITIAL_IDS: Final = frozenset(term.term_id for term in INITIAL_TERMS)


class VocabularyRejected(Exception):
    """Un rechazo de negocio de la lista, con su ``detail_code`` (LoopDetailCode) y, si lo
    rechazó un validador de texto libre, su pista para quien escribió (``hint``, VIG-344)."""

    def __init__(self, detail_code: LoopDetailCode, hint: FreeTextHint | None = None) -> None:
        super().__init__(detail_code.value)
        self.detail_code = detail_code
        self.hint = hint


class VocabularyRequestInvalid(Exception):
    """El cuerpo no tiene la forma del esquema: ``invalid_request`` sin ``detail_code``."""

    api_code: Final = ApiErrorCode.INVALID_REQUEST

    def __init__(self, reason: str) -> None:
        super().__init__(reason)


class VocabularyUnavailable(ExternalDependencyDown):
    """Transitorio: la fila ya no tenía la versión leída, o la cadena estaba ocupada."""


@dataclass(frozen=True, slots=True)
class TermRequest:
    """Un término que se pide añadir, tal como llega (se valida en ``change``)."""

    category: object
    form: object
    value: object
    suggestion_es: object


@dataclass(frozen=True, slots=True)
class NameRequest:
    """Un nombre declarado que se pide añadir, tal como llega."""

    kind: object
    value: object


@dataclass(frozen=True, slots=True)
class VocabularyTermView:
    """Un término vigente de la lista."""

    term_id: uuid.UUID
    category: BlockedTermCategory
    form: BlockedTermForm
    value: str
    lemma: str | None
    """El lema guardado (el valor ya plegado) de un ``lemma``; ``None`` en un patrón."""
    suggestion_es: str
    origin: BlockedTermOrigin
    added_in_version: int


@dataclass(frozen=True, slots=True)
class VocabularyView:
    """La lista vigente de la organización."""

    list_version: int
    lemmatizer_version: str
    effective_from: datetime | None
    record_id: uuid.UUID | None
    terms: tuple[VocabularyTermView, ...]
    declared_names: tuple[DeclaredName, ...]


@dataclass(frozen=True, slots=True)
class ChangeResult:
    """Lo que produjo un cambio: la versión nueva y su registro."""

    change_id: uuid.UUID
    record_id: uuid.UUID
    list_version: int
    changed_at: datetime
    added: tuple[VocabularyTermView, ...]
    removed_term_ids: tuple[uuid.UUID, ...]
    declared_names_added: tuple[DeclaredName, ...]
    removed_name_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True, slots=True)
class _Change:
    """Un cambio ya validado fuera de la base."""

    added: tuple[tuple[BlockedTermCategory, BlockedTermForm, str, str], ...]
    removed: tuple[uuid.UUID, ...]
    names_added: tuple[tuple[DeclaredNameKind, str], ...]
    names_removed: tuple[uuid.UUID, ...]
    reason: str


def term_key(form: BlockedTermForm, value: str) -> tuple[BlockedTermForm, str]:
    """La clave de igualdad de un término: su forma y su valor plegado (como
    ``compile_vocabulary``); un patrón se compara tal cual."""
    return form, value if form is BlockedTermForm.PATTERN else lexicon.fold_word(value)


def name_key(value: str) -> str:
    """La clave de igualdad de un nombre declarado: plegado como el texto que se valida."""
    return lexicon.fold_word(value)


def _initial_views() -> tuple[VocabularyTermView, ...]:
    return tuple(
        VocabularyTermView(
            term_id=term.term_id,
            category=term.category,
            form=term.form,
            value=term.value,
            lemma=term.value if term.form is BlockedTermForm.LEMMA else None,
            suggestion_es=term.suggestion_es,
            origin=BlockedTermOrigin.DERIVED,
            added_in_version=INITIAL_LIST_VERSION,
        )
        for term in INITIAL_TERMS
    )


def _stored_view(term: StoredTerm) -> VocabularyTermView:
    return VocabularyTermView(
        term_id=term.term_id,
        category=term.category,
        form=term.form,
        value=term.value,
        lemma=term.value if term.form is BlockedTermForm.LEMMA else None,
        suggestion_es=term.suggestion_es,
        origin=term.origin,
        added_in_version=term.added_in_version,
    )


def _view(head: ListRow | None, terms: Sequence[StoredTerm]) -> VocabularyView:
    initial = _initial_views()
    if head is None:
        return VocabularyView(
            list_version=INITIAL_LIST_VERSION,
            lemmatizer_version=lexicon.LEMMATIZER_VERSION,
            effective_from=None,
            record_id=None,
            terms=initial,
            declared_names=(),
        )
    return VocabularyView(
        list_version=head.version,
        lemmatizer_version=head.lemmatizer_version or lexicon.LEMMATIZER_VERSION,
        effective_from=head.effective_from,
        record_id=head.record_id,
        terms=(*initial, *(_stored_view(term) for term in terms)),
        declared_names=head.declared_names,
    )


# --- validación fuera de la base -----------------------------------------------------------------


def _base(text: str, field: FreeTextField) -> str:
    """La política base de U-02 (NFC, longitud, caracteres, marcado). Los dos validadores de U-04
    los aplica el escritor, que antes corre sus preparadores: aquí sus cachés podrían no tener aún
    la versión vigente de la lista (un nombre recién declarado se rechazaría)."""
    try:
        return apply_base_policy(text, field)
    except FreeTextRejected as rejected:
        raise VocabularyRequestInvalid(f"{field.path}: {rejected.reason}") from None


def _checked_reason(reason: object) -> str:
    if reason is None or (isinstance(reason, str) and not reason.strip()):
        raise VocabularyRejected(LoopDetailCode.VOCABULARY_CHANGE_REASON_REQUIRED)
    if not isinstance(reason, str):
        raise VocabularyRequestInvalid("el motivo es una cadena")
    if len(reason.strip()) < REASON_MIN_CHARS:
        raise VocabularyRejected(LoopDetailCode.VOCABULARY_CHANGE_REASON_REQUIRED)
    try:
        return apply_base_policy(reason, _REASON)
    except FreeTextRejected as rejected:
        if rejected.reason == FreeTextRejection.TOO_SHORT:
            raise VocabularyRejected(LoopDetailCode.VOCABULARY_CHANGE_REASON_REQUIRED) from None
        raise VocabularyRequestInvalid("motivo rechazado por la política") from None


def _enum[E: (BlockedTermCategory, BlockedTermForm, DeclaredNameKind)](
    kind: type[E], value: object
) -> E:
    if not isinstance(value, str):
        raise VocabularyRequestInvalid(f"{kind.__name__} es una cadena")
    try:
        return kind(value)
    except ValueError:
        raise VocabularyRequestInvalid(f"{kind.__name__} desconocido") from None


def checked_pattern(value: str) -> str:
    """Un patrón del cliente que se puede compilar y correr en la ruta caliente, o rechazo."""
    if len(value) > PATTERN_MAX_CHARS:
        raise VocabularyRequestInvalid("patrón demasiado largo")
    try:
        check_pattern(value)
    except UnsafePattern as unsafe:
        raise VocabularyRequestInvalid(str(unsafe)) from None
    return value


def checked_lemma(value: str) -> str:
    """El lema guardado: una palabra, plegada con la tabla de flexiones vigente."""
    folded = lexicon.fold_word(value.strip())
    if _ONE_WORD.fullmatch(folded) is None:
        raise VocabularyRequestInvalid("un lema es una sola palabra")
    if not LEMMA_MIN_CHARS <= len(folded) <= LEMMA_MAX_CHARS:
        raise VocabularyRequestInvalid("un lema tiene de 2 a 64 caracteres")
    return folded


def _term(request: object) -> tuple[BlockedTermCategory, BlockedTermForm, str, str]:
    if not isinstance(request, TermRequest):
        raise VocabularyRequestInvalid("término mal formado")
    category = _enum(BlockedTermCategory, request.category)
    form = _enum(BlockedTermForm, request.form)
    if not isinstance(request.value, str) or not isinstance(request.suggestion_es, str):
        raise VocabularyRequestInvalid("el valor y la alternativa son cadenas")
    try:
        # El valor solo pasa la política base (BR-LAZ-83): nunca los validadores de U-04.
        value = apply_base_policy(request.value, _VALUE)
    except FreeTextRejected:
        raise VocabularyRequestInvalid("valor rechazado por la política base") from None
    stored = checked_lemma(value) if form is BlockedTermForm.LEMMA else checked_pattern(value)
    suggestion = _base(request.suggestion_es, _SUGGESTION)
    return category, form, stored, suggestion


def _name(request: object) -> tuple[DeclaredNameKind, str]:
    if not isinstance(request, NameRequest):
        raise VocabularyRequestInvalid("nombre mal formado")
    kind = _enum(DeclaredNameKind, request.kind)
    if not isinstance(request.value, str):
        raise VocabularyRequestInvalid("el nombre es una cadena")
    value = _base(request.value, _NAME)
    if value != value.strip() or _LETTER.search(value) is None:
        raise VocabularyRequestInvalid("un nombre declarado lleva letras, sin espacios al borde")
    return kind, value


def _ids(values: object, what: str) -> tuple[uuid.UUID, ...]:
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        raise VocabularyRequestInvalid(f"{what}: una lista de identificadores")
    ids: list[uuid.UUID] = []
    for value in values:
        if type(value) is not uuid.UUID:
            raise VocabularyRequestInvalid(f"{what}: un identificador no es un UUID")
        ids.append(value)
    if len(set(ids)) != len(ids):
        raise VocabularyRequestInvalid(f"{what}: un identificador repetido")
    return tuple(ids)


def _bounded(values: object, what: str) -> Sequence[object]:
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        raise VocabularyRequestInvalid(f"{what}: una lista")
    if len(values) > MAX_TERMS_PER_CHANGE:
        raise VocabularyRequestInvalid(f"{what}: más de {MAX_TERMS_PER_CHANGE}")
    return values


def validated_change(
    *,
    added: object,
    removed: object,
    declared_names_added: object,
    declared_names_removed: object,
    reason: object,
) -> _Change:
    """Las comprobaciones de ``change`` que no miran la base (paso 1), en su orden: motivo y
    después cada elemento; nada se repite dentro del cambio y el cambio nombra algo."""
    reason_text = _checked_reason(reason)
    terms = tuple(_term(item) for item in _bounded(added, "added"))
    removed_ids = _ids(_bounded(removed, "removed"), "removed")
    names = tuple(_name(item) for item in _bounded(declared_names_added, "declared_names_added"))
    removed_names = _ids(
        _bounded(declared_names_removed, "declared_names_removed"), "declared_names_removed"
    )
    if not (terms or removed_ids or names or removed_names):
        raise VocabularyRequestInvalid("el cambio no nombra ningún término ni nombre")
    keys = [term_key(form, value) for _, form, value, _ in terms]
    if len(set(keys)) != len(keys):
        raise VocabularyRejected(LoopDetailCode.VOCABULARY_TERM_DUPLICATE)
    name_keys = [name_key(value) for _, value in names]
    if len(set(name_keys)) != len(name_keys):
        raise VocabularyRejected(LoopDetailCode.VOCABULARY_TERM_DUPLICATE)
    return _Change(
        added=terms,
        removed=removed_ids,
        names_added=names,
        names_removed=removed_names,
        reason=reason_text,
    )


# --- el servicio ---------------------------------------------------------------------------------


def _written(written: Receipt | LedgerRejection) -> Receipt:
    """Un rechazo o un duplicado revierten la transacción (nada queda escrito)."""
    if isinstance(written, LedgerRejection):
        if written.code is LedgerRejectionCode.FREE_TEXT_REJECTED:
            code = _VALIDATOR_CODES.get(written.detail_code or "")
            if code is not None:
                raise VocabularyRejected(code, written.hint)
            raise VocabularyRequestInvalid("texto libre rechazado por el expediente")
        if written.code is LedgerRejectionCode.CHAIN_LOCKED_TIMEOUT:
            raise VocabularyUnavailable("ledger_chain")
        raise RuntimeError(f"vocabulary_list_changed rechazado: {written.code.value}")
    if written.status is not AcceptanceStatus.ACCEPTED:
        raise RuntimeError("vocabulary_list_changed con un identificador ya usado")
    return written


def _snapshot(context: ScopeContext) -> dict[str, Any]:
    """``UserSnapshot`` del actor: usuario, nombre en el instante y rol con el que actúa."""
    actor = context.actor
    if actor.role_in_use is None:
        raise ResourceNotFound()
    return {
        "user_id": str(actor.id),
        "display_name": actor.display_name_snapshot,
        "role_in_use": actor.role_in_use.value,
    }


@repository
class VocabularyService:
    """``loop.vocabulary``: consultar y modificar la lista de vocabulario de la organización."""

    def __init__(
        self,
        *,
        repository: PostgresVocabularyRepository,
        database: LedgerDatabase,
        writer: EscritorExpediente,
        authorizer: Authorizer,
        clock: Clock,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._repository = repository
        self._database = database
        self._writer = writer
        self._authorizer = authorizer
        self._clock = clock
        self._random_bytes = random_bytes

    def __repr__(self) -> str:
        return "VocabularyService()"

    async def _authorized(self, context: ScopeContext) -> ScopeContext:
        """``vocabulary.manage`` sobre la organización; bajo concesión, denegado."""
        if not isinstance(context, ScopeContext):
            raise ResourceNotFound()
        key = PermissionKey.VOCABULARY_MANAGE
        resource = Resource.organization(context.organization_id)
        authorized = await self._authorizer.authorize(context, key, resource)
        if authorized.concession_id is not None:
            await self._authorizer.deny(authorized, key, resource)
        return authorized

    # --- consultar -------------------------------------------------------------------------------

    async def current(self, context: ScopeContext) -> VocabularyView:
        """La lista vigente: la inicial (versión 1) si la organización aún no la cambió."""
        authorized = await self._authorized(context)
        head, terms = await self._repository.read(authorized)
        return _view(head, terms)

    # --- modificar -------------------------------------------------------------------------------

    async def change(
        self,
        context: ScopeContext,
        *,
        added: object = (),
        removed: object = (),
        declared_names_added: object = (),
        declared_names_removed: object = (),
        reason: object,
    ) -> ChangeResult:
        """Aplica el cambio con su registro; devuelve la versión nueva."""
        authorized = await self._authorized(context)
        request = validated_change(
            added=added,
            removed=removed,
            declared_names_added=declared_names_added,
            declared_names_removed=declared_names_removed,
            reason=reason,
        )
        writer_context = with_unit(authorized, ActorUnit.U04)
        async with self._database.transaction(writer_context) as transaction:
            await self._repository.lock(transaction)
            head, current = await self._repository.current(transaction)
            version = (INITIAL_LIST_VERSION if head is None else head.version) + 1
            if version > _MAX_VERSION:
                raise VocabularyRequestInvalid("la lista llegó a su última versión")
            removed_terms = _removed_terms(request.removed, current)
            names = () if head is None else head.declared_names
            removed_names = _removed_names(request.names_removed, names)
            _no_duplicates(request, current, names)
            now = to_millisecond(self._clock.now())
            new_terms = tuple(
                StoredTerm(
                    term_id=uuid7(self._clock, self._random_bytes),
                    category=category,
                    form=form,
                    value=value,
                    suggestion_es=suggestion,
                    origin=BlockedTermOrigin.CUSTOMER_ADDED,
                    added_in_version=version,
                )
                for category, form, value, suggestion in request.added
            )
            new_names = tuple(
                DeclaredName(
                    name_id=uuid7(self._clock, self._random_bytes),
                    kind=kind,
                    value=value,
                    added_in_version=version,
                )
                for kind, value in request.names_added
            )
            kept_names = tuple(name for name in names if name not in removed_names)
            if len(current) - len(removed_terms) + len(new_terms) > MAX_CUSTOMER_TERMS:
                raise VocabularyRequestInvalid("la lista supera los términos permitidos")
            if len(kept_names) + len(new_names) > MAX_DECLARED_NAMES:
                raise VocabularyRequestInvalid("la lista supera los nombres permitidos")
            change_id = uuid7(self._clock, self._random_bytes)
            record_id = uuid7(self._clock, self._random_bytes)
            snapshot = _snapshot(writer_context)
            content = {
                "change_id": str(change_id),
                "list_version": version,
                "added": [
                    {
                        "term_id": str(term.term_id),
                        "category": term.category.value,
                        "form": term.form.value,
                        "value": term.value,
                        "suggestion_es": term.suggestion_es,
                    }
                    for term in new_terms
                ],
                "removed": [
                    {"term_id": str(term.term_id), "value": term.value} for term in removed_terms
                ],
                "declared_names_added": [
                    {"name_id": str(name.name_id), "kind": name.kind.value, "value": name.value}
                    for name in new_names
                ],
                "declared_names_removed": [
                    {"name_id": str(name.name_id), "value": name.value} for name in removed_names
                ],
                "reason": request.reason,
                "changed_by": snapshot,
                "changed_at": format_timestamp(now),
            }
            event = NewEvent(
                event_name=VOCABULARY_LIST_CHANGED,
                payload={
                    "change_id": str(change_id),
                    "record_id": str(record_id),
                    "list_version": version,
                    "added_count": len(new_terms),
                    "removed_count": len(removed_terms),
                },
            )
            # El registro va antes que la fila: el preparador de los validadores lee la versión
            # vigente (la que se confirma, o nada si esto falla) y nunca una a medio escribir.
            _written(
                await self._writer.write(
                    writer_context,
                    VOCABULARY_LIST_CHANGED,
                    content,
                    events=(event,),
                    occurred_at=now,
                    transaction=transaction,
                    record_id=record_id,
                )
            )
            row = ListRow(
                list_id=uuid7(self._clock, self._random_bytes) if head is None else head.list_id,
                organization_id=writer_context.organization_id,
                version=version,
                effective_from=now,
                created_by=snapshot,
                record_id=record_id,
                lemmatizer_version=lexicon.LEMMATIZER_VERSION,
                declared_names=(*kept_names, *new_names),
            )
            if head is None:
                await self._repository.insert_list(transaction, row)
            elif not await self._repository.update_list(
                transaction, row, expected_version=head.version
            ):
                raise VocabularyUnavailable("vocabulary_list_race")
            await self._repository.insert_terms(
                transaction, row.list_id, new_terms, reason=request.reason
            )
            retired = await self._repository.remove_terms(
                transaction,
                row.list_id,
                [term.term_id for term in removed_terms],
                version=version,
            )
            if retired != len(removed_terms):
                raise VocabularyUnavailable("vocabulary_term_race")
        return ChangeResult(
            change_id=change_id,
            record_id=record_id,
            list_version=version,
            changed_at=now,
            added=tuple(_stored_view(term) for term in new_terms),
            removed_term_ids=tuple(term.term_id for term in removed_terms),
            declared_names_added=new_names,
            removed_name_ids=tuple(name.name_id for name in removed_names),
        )


def _removed_terms(
    term_ids: Sequence[uuid.UUID], current: Sequence[StoredTerm]
) -> tuple[StoredTerm, ...]:
    """Los términos que se retiran: vigentes y del cliente. Uno de la lista inicial (``derived``)
    o que no está vigente en la organización es ``invalid_request`` (BR-LAZ-86)."""
    by_id = {term.term_id: term for term in current}
    removed: list[StoredTerm] = []
    for term_id in term_ids:
        if term_id in _INITIAL_IDS:
            raise VocabularyRequestInvalid("un término de la lista inicial no se retira")
        term = by_id.get(term_id)
        if term is None or term.origin is not BlockedTermOrigin.CUSTOMER_ADDED:
            raise VocabularyRequestInvalid("el término no está vigente en la lista")
        removed.append(term)
    return tuple(removed)


def _removed_names(
    name_ids: Sequence[uuid.UUID], names: Sequence[DeclaredName]
) -> tuple[DeclaredName, ...]:
    by_id = {name.name_id: name for name in names}
    removed: list[DeclaredName] = []
    for name_id in name_ids:
        name = by_id.get(name_id)
        if name is None:
            raise VocabularyRequestInvalid("el nombre no está declarado en la lista")
        removed.append(name)
    return tuple(removed)


def _no_duplicates(
    request: _Change, current: Sequence[StoredTerm], names: Sequence[DeclaredName]
) -> None:
    """Un término ya vigente (la lista inicial o del cliente) o un nombre ya declarado es
    ``vocabulary_term_duplicate``; lo que se retira en el mismo cambio sigue contando."""
    vigentes = {term_key(term.form, term.value) for term in INITIAL_TERMS}
    vigentes |= {term_key(term.form, term.value) for term in current}
    if any(term_key(form, value) in vigentes for _, form, value, _ in request.added):
        raise VocabularyRejected(LoopDetailCode.VOCABULARY_TERM_DUPLICATE)
    declared = {name_key(name.value) for name in names}
    if any(name_key(value) in declared for _, value in request.names_added):
        raise VocabularyRejected(LoopDetailCode.VOCABULARY_TERM_DUPLICATE)
