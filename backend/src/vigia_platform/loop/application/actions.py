"""``loop.lifecycle`` (1 de 2): la acción correctiva (LC-LAZ-09; S-PLA-12; SUP-U05-07; TASK-311).

``assign(context, finding_state_id, ...)`` es ``asignar_accion`` (BLM §3 y §11), en el orden de
sus pasos:

1. **Alcance**: la fila de la proyección del hallazgo de la organización y ``actions.manage``
   sobre **su zona** (BR-NUC-13). Inexistente, de otra organización, fuera de alcance o bajo
   concesión de proveedor: ``ResourceNotFound`` (``not_found``), sin escribir nada.
2. **En una sola transacción** (BR-LAZ-146), tras los candados de ``loop.application.locks`` (el
   hallazgo y después sus acciones): el estado vigente y la transición.

   - Sin ``supersedes_action_id``: T07 (``open → action_assigned``) de ``lifecycle.transition``;
     desde cualquier otro estado, ``transition_not_allowed`` (BR-LAZ-36) sin efecto ni registro.
   - Con ``supersedes_action_id`` (**reasignación**, BR-LAZ-49 y 154): solo sobre la acción
     **vigente** del hallazgo en ``action_assigned`` u ``overdue``, viva (``assigned`` u
     ``overdue``). Escribe un ``action_assigned`` nuevo que la sustituye; la anterior queda
     ``superseded`` (nunca se borra ni se reescribe su registro) y el estado del hallazgo **no
     cambia**: un vencimiento ya declarado sigue declarado (``overdue_since`` se conserva, P5).
     Cualquier otra cosa, ``transition_not_allowed``.
3. **Tipo** de la lista cerrada ``action_type`` (``engineering`` o ``process``); fuera de ella,
   ``action_type_not_allowed``. Es **fijo** (BR-LAZ-51): una reasignación conserva el de la
   acción que sustituye y otro tipo es ``action_type_not_allowed`` (para cambiarlo se cierra o se
   reabre).
4. **Textos** (BR-LAZ-37): ``action_description`` (20 a 2000) y ``expected_outcome`` (opcional,
   hasta 1000) en NFC por la política base; una acción **sobre una persona** (formarla, entrenarla,
   amonestarla, sancionarla, evaluarla…: ``person_directed``) es ``action_on_person_forbidden``
   (F16 de §4.2). Los dos validadores de U-04 los aplica el escritor antes del hash
   (``blocked_vocabulary_term``, ``person_attribution_detected``).
5. **Responsable** (BR-LAZ-38): un usuario **activo** de la organización con alguna asignación de
   rol vigente que cubra la zona (la organización, la planta de la zona o la zona); si no,
   ``not_found`` (BR-NUC-09, nunca ``forbidden``). ``assignee_display_name`` es la instantánea.
6. **Plazo** (BR-LAZ-39): ``deadline_days`` (por defecto ``action_deadline_default_days``) dentro
   de ``[action_deadline_min_days, action_deadline_max_days]`` de la configuración **vigente** de
   la planta (``settings_in``, TASK-304); fuera, ``deadline_out_of_range``. ``due_at`` es el fin
   del día hábil ``deadline_days`` posterior a la fecha local de ``assigned_at``
   (``business_deadline``, con el calendario declarado y la zona de la planta). Los dos quedan
   **congelados** en el registro y en ``loop.corrective_action``: un cambio posterior de la
   configuración no los mueve (PR-LAZ-15). Una reasignación tiene su propio plazo, validado igual.
7. ``LoopProjectionStore.write_and_apply`` (registro ``action_assigned``, su evento y la
   proyección en la misma transacción) y la fila de ``loop.corrective_action``.

``assign_in_transaction`` es el mismo núcleo para quien ya tiene la transacción y los candados
(TASK-312 y TASK-343): la segunda mitad de T12, T13 y T14, cuando la revisión o la reapertura ya
movió el hallazgo a ``action_assigned`` con ``current_action_id`` y sin ``due_at``
(``pending_action_id``).

Toda marca devuelta de una zona unipersonal se agrega a su ventana (BR-LAZ-06); el registro guarda
la exacta. ``due_at`` es un plazo del calendario (fin de un día local), no un instante observado,
y se devuelve tal cual. La hora sale siempre del ``Clock`` inyectado.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
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
    RecordScope,
)
from vigia_platform.ledger.free_text import (
    FreeTextField,
    FreeTextRejected,
    apply_base_policy,
    canonical_form,
)
from vigia_platform.loop.application.locks import lock_actions, lock_finding
from vigia_platform.loop.calendar import business_deadline
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.domain.enums import ActionType, FindingLifecycleState
from vigia_platform.loop.domain.lifecycle import (
    AssignAction,
    LifecycleSnapshot,
    Rejected,
    transition,
)
from vigia_platform.loop.domain.time_aggregation import presented
from vigia_platform.loop.projection.store import LoopProjectionStore
from vigia_platform.loop.validators.folding import fold_text
from vigia_platform.shared.api.errors import ApiErrorCode, ExternalDependencyDown
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.outbox.publish import NewEvent
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

__all__ = [
    "ACTION_ASSIGNED",
    "DESCRIPTION_FIELD",
    "DESCRIPTION_MAX_CHARS",
    "DESCRIPTION_MIN_CHARS",
    "EXPECTED_OUTCOME_FIELD",
    "EXPECTED_OUTCOME_MAX_CHARS",
    "FINDING_RESOURCE_KIND",
    "LIVE_ACTION_STATES",
    "ActionRejected",
    "ActionRequestInvalid",
    "ActionRow",
    "ActionService",
    "ActionUnavailable",
    "AssignmentRequest",
    "AssignmentResult",
    "LoopFinding",
    "action_rows",
    "action_text",
    "actor_snapshot",
    "finding_from",
    "parse_action_type",
    "person_directed",
    "read_finding",
    "screen_action_text",
    "written",
]

ACTION_ASSIGNED: Final = "action_assigned"
DESCRIPTION_MIN_CHARS: Final = 20
DESCRIPTION_MAX_CHARS: Final = 2000
EXPECTED_OUTCOME_MAX_CHARS: Final = 1000
"""Límites de ``LongText`` y ``NoteText`` (``loop.record_types``; DE §2.5)."""
FINDING_RESOURCE_KIND: Final = "finding"
"""``kind`` del recurso en la auditoría de la denegación: la fila de la bandeja."""
LIVE_ACTION_STATES: Final = frozenset({"assigned", "overdue"})
"""Estados de ``corrective_action`` de una acción viva (DE §2.5)."""

S = FindingLifecycleState

DESCRIPTION_FIELD: Final = FreeTextField(
    ACTION_ASSIGNED, "/action_description", DESCRIPTION_MIN_CHARS, DESCRIPTION_MAX_CHARS
)
EXPECTED_OUTCOME_FIELD: Final = FreeTextField(
    ACTION_ASSIGNED, "/expected_outcome", 1, EXPECTED_OUTCOME_MAX_CHARS
)
"""Los dos textos libres de ``action_assigned`` (§5.1), con los límites de su esquema."""
_REASSIGNABLE: Final = frozenset({S.ACTION_ASSIGNED, S.OVERDUE})

_FINDING_BY_ID: Final = text(
    "SELECT finding_state_id, organization_id, plant_id, zone_id, subject_kind, subject_id,"
    " anchor_record_id, lifecycle_state, previous_lifecycle_state, due_at, current_action_id,"
    " single_occupancy_zone, aggregation_window_minutes FROM loop.finding_state"
    " WHERE organization_id = :organization_id AND finding_state_id = :finding_state_id"
)
_ASSIGNEE: Final = text(
    # Un usuario activo de la organización con un rol vigente sobre la organización, la planta de
    # la zona o la propia zona (el patrón de A-58): nunca un listado ni otros campos.
    "SELECT u.user_id, u.display_name FROM identity.user_account AS u"
    " WHERE u.organization_id = :organization_id AND u.user_id = :user_id"
    " AND u.status = 'active' AND EXISTS (SELECT 1 FROM identity.role_assignment AS r"
    " WHERE r.organization_id = :organization_id AND r.user_id = u.user_id"
    " AND r.removed_at IS NULL AND (r.scope_level = 'organization'"
    " OR (r.scope_level = 'plant' AND r.scope_id = :plant_id)"
    " OR (r.scope_level = 'zone' AND r.scope_id = :zone_id)))"
)
_PLANT_TIMEZONE: Final = text(
    "SELECT timezone FROM identity.plant"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
)
_INSERT_ACTION: Final = text(
    "INSERT INTO loop.corrective_action (action_id, organization_id, plant_id, zone_id,"
    " subject_kind, subject_id, record_id, action_type, action_description, expected_outcome,"
    " assignee_user_id, assigned_by, assigned_at, due_at, state, counts_as_overdue)"
    " VALUES (:action_id, :organization_id, :plant_id, :zone_id, :subject_kind, :subject_id,"
    " :record_id, :action_type, :action_description, :expected_outcome, :assignee_user_id,"
    " CAST(:assigned_by AS jsonb), :assigned_at, :due_at, 'assigned', true)"
)
_SUPERSEDE: Final = text(
    "UPDATE loop.corrective_action SET state = 'superseded'"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " AND action_id = :action_id AND state <> 'superseded'"
)

_VALIDATOR_CODES: Final[Mapping[str, LoopDetailCode]] = {
    LoopDetailCode.BLOCKED_VOCABULARY_TERM.value: LoopDetailCode.BLOCKED_VOCABULARY_TERM,
    LoopDetailCode.PERSON_ATTRIBUTION_DETECTED.value: LoopDetailCode.PERSON_ATTRIBUTION_DETECTED,
}
"""Los ``detail_code`` de los dos validadores de U-04 que un rechazo del escritor conserva."""


class ActionRejected(Exception):
    """Un rechazo de negocio del lazo, con su ``detail_code`` (LoopDetailCode)."""

    def __init__(self, detail_code: LoopDetailCode) -> None:
        super().__init__(detail_code.value)
        self.detail_code = detail_code


class ActionRequestInvalid(Exception):
    """El cuerpo no tiene la forma del esquema: ``invalid_request`` sin ``detail_code``."""

    api_code: Final = ApiErrorCode.INVALID_REQUEST


class ActionUnavailable(ExternalDependencyDown):
    """Transitorio: la cadena de la planta no se pudo tomar a tiempo."""


# --- acción sobre una persona (F16 de §4.2, BR-LAZ-37) ------------------------------------------

_PERSONS: Final = (
    r"(?:operari[oa]s?|operador(?:a|es|as)?|trabajador(?:a|es|as)?|emplead[oa]s?"
    r"|colaborador(?:a|es|as)?|supervisor(?:a|es|as)?|tecnic[oa]s?|auxiliar(?:es)?"
    r"|contratistas?|jef[ea]s?|encargad[oa]s?|mecanic[oa]s?|electricistas?"
    r"|soldador(?:a|es|as)?|montacarguistas?|conductor(?:a|es|as)?|aprendiz|aprendices"
    r"|practicantes?|obrer[oa]s?|personas?|personal|gente|cuadrillas?|brigadistas?"
    r"|ayudantes?|vigilantes?|peon|peones|responsables?|quien|quienes|usuari[oa]s?"
    r"|peaton|peatones|transeuntes?|visitantes?|infractor(?:a|es|as)?|culpables?"
    r"|causantes?|involucrad[oa]s?|implicad[oa]s?|individu[oa]s?|sujetos?|companer[oa]s?)"
)
"""Personas o grupos de personas: los cargos del validador de atribución, sus colectivos, quien
cruza o usa la zona y las formas de señalar a alguien («el responsable», «a quien cruzó»)."""

_TOWARD_PERSON: Final = (
    r"(?:re)?(?:entren\w*|capacit\w*|form(?:ar|e|en|acion(?:es)?)|instru(?:ir|ya|yan)"
    r"|amonest\w*|sancion\w*|evalu\w*|reprend\w*|reprimend\w*|desped\w*|despid\w*"
    r"|suspend\w*|disciplin\w*|mult(?:a|as|ar|e|en)|sensibiliz\w*|concien(?:ci|ti)z\w*"
    r"|induc(?:ir|cion(?:es)?)|charlas?|retroaliment\w*|llamad[oa]s? de atencion"
    r"|llamar la atencion|retir\w*)"
)
"""Intervenciones sobre una persona: formarla, entrenarla, amonestarla, sancionarla, evaluarla,
retirarla de la zona…"""

_ONLY_ON_PERSONS: Final = (
    r"(?:re)?(?:amonest|sancion|reprend|reprimend|desped|despid|mult|capacit|entren|instru"
    r"|sensibiliz|concien(?:ci|ti)z|disciplin|retroaliment)"
)
"""Las raíces de los verbos que solo se aplican a personas: con un pronombre pegado («sancionarlo»,
«capacitarlos») la acción recae sobre alguien. Fuera, a propósito, los que también se dicen de una
cosa («evaluarla», «suspenderla», «formarla», «retirarlo»)."""

_INFRASTRUCTURE: Final = (
    r"(?:paso|pasos|cruce|cruces|senda|sendas|ruta|rutas|acceso|accesos|zona|zonas|area|areas"
    r"|senalizacion|senal|senales|transito|circulacion|via|vias|pasillo|pasillos|corredor"
    r"|corredores|franja|franjas|demarcacion|puerta|puertas|entrada|entradas|salida|salidas"
    r"|refugio|refugios|baranda|barandas)"
)
"""Elementos de la zona que se nombran por quien los usa («paso de peatones», «senda para
visitantes»): no son una persona, y se apartan antes de buscar."""

_DETERMINERS: Final = (
    r"(?: (?:todo|toda|todos|todas|cada))?"
    r"(?: (?:el|la|los|las|un|una|unos|unas|este|esta|estos|estas|ese|esa|esos|esas|su|sus"
    r"|nuestro|nuestra|nuestros|nuestras|dicho|dicha|aquel|aquella|aquellos|aquellas))?"
    r"(?: (?:nuevo|nueva|nuevos|nuevas))?"
)
_OBJECT: Final = rf"(?:a|al|de|del|para|sobre|contra|hacia|ante|en contra del?){_DETERMINERS}"
"""Lo que hace de la persona el **objeto** de la intervención: «al personal», «de los operarios»,
«contra el operario». Sin él, «evaluar con el técnico el resguardo» no es una acción sobre el
técnico."""

_PARTICIPLES: Final = (
    r"(?:re)?(?:entrenad[oa]s?|capacitad[oa]s?|formad[oa]s?|instruid[oa]s?|amonestad[oa]s?"
    r"|sancionad[oa]s?|evaluad[oa]s?|reprendid[oa]s?|despedid[oa]s?|suspendid[oa]s?"
    r"|multad[oa]s?|sensibilizad[oa]s?|concien(?:ci|ti)zad[oa]s?)"
)
_B: Final = r"(?<![^\W_])"
_E: Final = r"(?![^\W_])"
_WORDS: Final = r"(?: [^\W\d_]+){0,4}?"
"""Hasta cuatro palabras entre la intervención y su objeto («en el procedimiento»)."""
_PERSON_DIRECTED: Final = (
    re.compile(rf"{_B}{_TOWARD_PERSON}{_WORDS} {_OBJECT} {_PERSONS}{_E}"),
    re.compile(rf"{_B}{_TOWARD_PERSON} {_PERSONS}{_E}"),
    re.compile(
        rf"{_B}{_PERSONS}{_WORDS} (?:sea|sean|ser|sera|seran|es|son|fue|fueron|quede|queden"
        rf"|este|esten) {_PARTICIPLES}{_E}"
    ),
    # «medidas disciplinarias contra el operario», «proceso en contra del conductor».
    re.compile(rf"{_B}(?:contra|en contra del?){_DETERMINERS} {_PERSONS}{_E}"),
    # El pronombre pegado: «sancionarlo», «capacitarlos», «amonestandole».
    re.compile(rf"{_B}{_ONLY_ON_PERSONS}(?:ar|er|ir|ando|iendo|yendo)(?:se)?(?:lo|la|le)s?{_E}"),
    # El relativo que señala a alguien: «a los que cruzaron», «al que», «contra quienes».
    re.compile(
        rf"{_B}{_TOWARD_PERSON}{_WORDS} (?:al|a (?:el|la|los|las)|para (?:el|la|los|las)"
        rf"|contra (?:el|la|los|las)|hacia (?:el|la|los|las)) que{_E}"
    ),
    # P7: la productividad, el desempeño o la falta de una persona.
    re.compile(
        rf"{_B}(?:productividad|desempeno|rendimiento|faltas?(?: (?:grave|graves|leve|leves))?)"
        rf" (?:de|del|de la|de los|de las){_DETERMINERS} {_PERSONS}{_E}"
    ),
)
"""Los patrones de una acción sobre una persona, sobre las formas plegadas (``_folded_forms``)."""
_INFRASTRUCTURE_PHRASE: Final = re.compile(
    rf"{_B}{_INFRASTRUCTURE}(?: (?:peatonal|peatonales))? (?:de|del|para){_DETERMINERS}"
    r" (?:peaton|peatones|visitantes?|usuari[oa]s?|transeuntes?|conductor(?:a|es|as)?)"
    rf"{_E}"
)
"""Solo quienes **usan** la zona: «la zona de los operarios» no se aparta."""
_SPELLED: Final = re.compile(r"(?<![^\W\d_])(?:[^\W\d_] ){2,}[^\W\d_](?![^\W\d_])")
"""Una palabra deletreada con espacios («o p e r a r i o s»)."""
_NON_WORD: Final = re.compile(r"[\W_]+")
_WORD_BREAK: Final = re.compile(r"(?<=[^\W\d_])[-\u2010-\u2015.\u00b7_]+\s*(?=[^\W\d_])")
"""Un corte dentro de una palabra («opera-rios», «ope.ra.rio»): guiones, puntos o subrayados entre
dos letras, con espacios después (el corte de línea)."""


_PERSON_WORD: Final = re.compile(_PERSONS)


def _rejoined(form: str) -> str:
    """Une dos piezas seguidas que juntas son una persona y por separado no («ope rarios»)."""
    words = form.split(" ")
    out: list[str] = []
    index = 0
    while index < len(words):
        if index + 1 < len(words):
            pair = words[index] + words[index + 1]
            if _PERSON_WORD.fullmatch(pair) and not (
                _PERSON_WORD.fullmatch(words[index]) or _PERSON_WORD.fullmatch(words[index + 1])
            ):
                out.append(pair)
                index += 2
                continue
        out.append(words[index])
        index += 1
    return " ".join(out)


def _folded_forms(value: str) -> tuple[str, ...]:
    """Las formas en que se busca, sobre la forma de validación de U-04 (canónica de U-02,
    ``casefold`` y confusables de UTS #39: sin tildes, homoglifos ni anchos raros): con cada signo
    como un espacio y, además, con las palabras cortadas unidas. Se buscan **las dos**: unir los
    cortes no puede tapar «capacitar-al-operario». Solo para buscar."""
    folded = fold_text(canonical_form(unicodedata.normalize("NFC", value)))
    spaced = " ".join(_NON_WORD.split(folded)).strip()
    joined = " ".join(_NON_WORD.split(_WORD_BREAK.sub("", folded))).strip()
    joined = _rejoined(_SPELLED.sub(lambda found: found.group().replace(" ", ""), joined))
    forms = (spaced,) if joined == spaced else (spaced, joined)
    # «paso de peatones» es un elemento de la zona, no una persona.
    return tuple(_INFRASTRUCTURE_PHRASE.sub("infraestructura", form) for form in forms)


def person_directed(value: str) -> bool:
    """¿La acción recae sobre una persona (formarla, entrenarla, amonestarla…) y no sobre la zona?

    Sin red, sin base y sin reloj; nunca devuelve ni repite el texto (P3)."""
    return any(
        pattern.search(form) is not None
        for form in _folded_forms(value)
        for pattern in _PERSON_DIRECTED
    )


def screen_action_text(value: str) -> None:
    """``action_on_person_forbidden`` si el texto describe una acción sobre una persona."""
    if person_directed(value):
        raise ActionRejected(LoopDetailCode.ACTION_ON_PERSON_FORBIDDEN)


# --- piezas puras ---------------------------------------------------------------------------------


def parse_action_type(value: object) -> ActionType:
    """El tipo de la lista cerrada; cualquier otro valor es ``action_type_not_allowed``."""
    if type(value) is not str:
        raise ActionRejected(LoopDetailCode.ACTION_TYPE_NOT_ALLOWED)
    try:
        return ActionType(value)
    except ValueError:
        raise ActionRejected(LoopDetailCode.ACTION_TYPE_NOT_ALLOWED) from None


def action_text(value: object, field: FreeTextField, *, required: bool) -> str | None:
    """Paso 4: el texto en NFC por la política base, sin acción sobre una persona."""
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ActionRequestInvalid("el texto de la acción es una cadena")
    try:
        normalized = apply_base_policy(value, field)
    except FreeTextRejected:
        raise ActionRequestInvalid("texto rechazado por la política base") from None
    if len(normalized.strip()) < field.min_length:
        raise ActionRequestInvalid("texto más corto que su mínimo")
    screen_action_text(normalized)
    return normalized


def _uuid(value: object) -> uuid.UUID:
    return uuid.UUID(str(value))


def _optional_uuid(value: object) -> uuid.UUID | None:
    return None if value is None else _uuid(value)


@dataclass(frozen=True, slots=True)
class LoopFinding:
    """Lo que las operaciones de acción y de evidencia necesitan de la fila de la proyección."""

    finding_state_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    subject_kind: str
    subject_id: uuid.UUID
    anchor_record_id: uuid.UUID
    state: FindingLifecycleState
    previous_state: FindingLifecycleState | None
    due_at: datetime | None
    current_action_id: uuid.UUID | None
    single_occupancy_zone: bool
    aggregation_window_minutes: int

    @property
    def snapshot(self) -> LifecycleSnapshot:
        return LifecycleSnapshot(self.state, self.previous_state, self.due_at)

    @property
    def scope(self) -> RecordScope:
        return RecordScope(plant_id=self.plant_id, zone_id=self.zone_id)

    def resource(self) -> Resource:
        return Resource(
            self.organization_id,
            FINDING_RESOURCE_KIND,
            self.finding_state_id,
            plant_id=self.plant_id,
            zone_id=self.zone_id,
        )

    def presented(self, moment: datetime) -> datetime:
        """La marca que se devuelve a una persona (BR-LAZ-06)."""
        return presented(
            moment,
            single_occupancy=self.single_occupancy_zone,
            window_minutes=self.aggregation_window_minutes,
        )


def finding_from(row: Any) -> LoopFinding:
    data = row._mapping
    previous = data["previous_lifecycle_state"]
    return LoopFinding(
        finding_state_id=_uuid(data["finding_state_id"]),
        organization_id=_uuid(data["organization_id"]),
        plant_id=_uuid(data["plant_id"]),
        zone_id=_uuid(data["zone_id"]),
        subject_kind=str(data["subject_kind"]),
        subject_id=_uuid(data["subject_id"]),
        anchor_record_id=_uuid(data["anchor_record_id"]),
        state=S(data["lifecycle_state"]),
        previous_state=None if previous is None else S(previous),
        due_at=data["due_at"],
        current_action_id=_optional_uuid(data["current_action_id"]),
        single_occupancy_zone=bool(data["single_occupancy_zone"]),
        aggregation_window_minutes=int(data["aggregation_window_minutes"]),
    )


async def read_finding(transaction: Transaction, finding_state_id: uuid.UUID) -> LoopFinding | None:
    """La fila del hallazgo dentro de la transacción (tras su exclusión)."""
    result = await transaction.execute(
        _FINDING_BY_ID,
        {
            "organization_id": transaction.context.organization_id,
            "finding_state_id": finding_state_id,
        },
    )
    row = result.first()
    return None if row is None else finding_from(row)


@dataclass(frozen=True, slots=True)
class ActionRow:
    """Una fila de ``loop.corrective_action`` leída bajo su candado (``lock_actions``)."""

    action_id: uuid.UUID
    record_id: uuid.UUID
    action_type: ActionType
    assignee_user_id: uuid.UUID
    assigned_at: datetime
    due_at: datetime
    state: str


def action_rows(rows: Sequence[Any]) -> dict[uuid.UUID, ActionRow]:
    found: dict[uuid.UUID, ActionRow] = {}
    for row in rows:
        data = row._mapping
        action_id = _uuid(data["action_id"])
        found[action_id] = ActionRow(
            action_id=action_id,
            record_id=_uuid(data["record_id"]),
            action_type=ActionType(data["action_type"]),
            assignee_user_id=_uuid(data["assignee_user_id"]),
            assigned_at=data["assigned_at"],
            due_at=data["due_at"],
            state=str(data["state"]),
        )
    return found


def written(
    result: Receipt | LedgerRejection,
    unavailable: Callable[[str], Exception] = ActionUnavailable,
) -> Receipt:
    """Un rechazo o un duplicado revierten la transacción (nada queda escrito)."""
    if isinstance(result, LedgerRejection):
        if result.code is LedgerRejectionCode.FREE_TEXT_REJECTED:
            code = _VALIDATOR_CODES.get(result.detail_code or "")
            if code is not None:
                raise ActionRejected(code)
            raise ActionRequestInvalid("texto libre rechazado por el expediente")
        if result.code is LedgerRejectionCode.CHAIN_LOCKED_TIMEOUT:
            raise unavailable("ledger_chain")
        raise RuntimeError(f"registro del lazo rechazado: {result.code.value}")
    if result.status is not AcceptanceStatus.ACCEPTED:
        raise RuntimeError("registro del lazo con un identificador ya usado")
    return result


def actor_snapshot(context: ScopeContext) -> dict[str, Any]:
    """``UserSnapshot`` del actor: usuario, nombre en el instante y rol con el que actúa."""
    actor = context.actor
    if actor.role_in_use is None:  # pragma: no cover - authorize fija el rol en uso
        raise ResourceNotFound()
    return {
        "user_id": str(actor.id),
        "display_name": actor.display_name_snapshot,
        "role_in_use": actor.role_in_use.value,
    }


# --- la asignación --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AssignmentResult:
    """El recibo de la asignación y el estado del hallazgo (SUP-U05-07)."""

    finding_state_id: uuid.UUID
    action_id: uuid.UUID
    record_id: uuid.UUID
    action_type: ActionType
    assignee_user_id: uuid.UUID
    assignee_display_name: str
    deadline_days: int
    due_at: datetime
    supersedes_action_id: uuid.UUID | None
    transition_id: str | None
    """``T07``; ``None`` en una reasignación o en la segunda mitad de T12, T13 o T14."""
    lifecycle_state: FindingLifecycleState
    assigned_at: datetime
    """La marca **exacta** del registro; a una persona se le devuelve ``presented_at``."""
    presented_at: datetime
    time_aggregated: bool
    configuration_version: int | None
    """La versión de la configuración de la planta con la que se fijó el plazo (``None``: los
    valores por defecto, sin fila todavía)."""


@dataclass(frozen=True, slots=True)
class AssignmentRequest:
    """Lo que pide quien asigna, tal cual llega (se valida en ``assign_in_transaction``)."""

    action_type: object
    action_description: object
    expected_outcome: object
    assignee_user_id: object
    deadline_days: object
    supersedes_action_id: object


@repository
class ActionService:
    """``loop.lifecycle``: asignar (y reasignar) la acción correctiva de un hallazgo."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        writer: EscritorExpediente,
        authorizer: Authorizer,
        clock: Clock,
        store: LoopProjectionStore | None = None,
        configuration: PostgresLoopConfigurationRepository | None = None,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._database = database
        self._writer = writer
        self._authorizer = authorizer
        self._clock = clock
        self._store = store if store is not None else LoopProjectionStore()
        self._configuration = (
            configuration
            if configuration is not None
            else PostgresLoopConfigurationRepository(database)
        )
        self._random_bytes = random_bytes

    def __repr__(self) -> str:
        return "ActionService()"

    async def assign(
        self,
        context: ScopeContext,
        finding_state_id: uuid.UUID,
        *,
        action_type: object,
        action_description: object,
        expected_outcome: object = None,
        assignee_user_id: object,
        deadline_days: object = None,
        supersedes_action_id: object = None,
    ) -> AssignmentResult:
        """Asigna (o reasigna) la acción del hallazgo ``finding_state_id``."""
        authorized, place = await self._authorized(context, finding_state_id)
        request = AssignmentRequest(
            action_type,
            action_description,
            expected_outcome,
            assignee_user_id,
            deadline_days,
            supersedes_action_id,
        )
        writer_context = with_unit(authorized, ActorUnit.U04)
        async with self._database.transaction(writer_context) as transaction:
            await lock_finding(transaction, place.organization_id, place.subject_id)
            actions = action_rows(
                await lock_actions(
                    transaction, place.organization_id, place.plant_id, place.subject_id
                )
            )
            current = await read_finding(transaction, finding_state_id)
            if current is None or current.subject_id != place.subject_id:
                raise ResourceNotFound()  # pragma: no cover - la proyección no borra filas
            return await self.assign_in_transaction(
                transaction, writer_context, current, actions, request
            )

    async def _checked(self, transaction: Transaction) -> None:
        """Con los candados tomados y la transición, el responsable y el plazo ya comprobados,
        antes de escribir (gancho de las pruebas de concurrencia)."""

    async def assign_in_transaction(
        self,
        transaction: Transaction,
        context: ScopeContext,
        current: LoopFinding,
        actions: Mapping[uuid.UUID, ActionRow],
        request: AssignmentRequest,
        *,
        pending_action_id: uuid.UUID | None = None,
    ) -> AssignmentResult:
        """Pasos 2 a 7 con los candados ya tomados (``locks``) y ``current`` leído tras ellos.

        ``pending_action_id`` es la acción que la revisión de un incerrable o la reapertura ya
        dejó vigente (T12, T13, T14): el registro la completa sin otra transición."""
        superseded = self._superseded(current, actions, request, pending_action_id)
        chosen = parse_action_type(request.action_type)
        if superseded is not None and chosen is not superseded.action_type:
            raise ActionRejected(LoopDetailCode.ACTION_TYPE_NOT_ALLOWED)
        description = action_text(request.action_description, DESCRIPTION_FIELD, required=True)
        expected = action_text(request.expected_outcome, EXPECTED_OUTCOME_FIELD, required=False)
        if description is None:  # pragma: no cover - required
            raise ActionRequestInvalid("sin descripción")
        assignee_id, assignee_name = await self._assignee(
            transaction, current, request.assignee_user_id
        )
        at = to_millisecond(self._clock.now())
        days, due_at, version = await self._deadline(
            transaction, current, request.deadline_days, at
        )
        await self._checked(transaction)
        action_id = (
            pending_action_id
            if pending_action_id is not None
            else uuid7(self._clock, self._random_bytes)
        )
        record_id = uuid7(self._clock, self._random_bytes)
        signer = actor_snapshot(context)
        content: dict[str, Any] = {
            "action_id": str(action_id),
            "subject_kind": current.subject_kind,
            "subject_id": str(current.subject_id),
            "anchor_record_id": str(current.anchor_record_id),
            "action_type": chosen.value,
            "action_description": description,
            "assignee_user_id": str(assignee_id),
            "assignee_display_name": assignee_name,
            "due_at": format_timestamp(due_at),
            "deadline_days": days,
            "assigned_by": signer,
            "assigned_at": format_timestamp(at),
        }
        if expected is not None:
            content["expected_outcome"] = expected
        if superseded is not None:
            content["supersedes_action_id"] = str(superseded.action_id)
        event = NewEvent(
            event_name=ACTION_ASSIGNED,
            payload={
                "zone_id": str(current.zone_id),
                "subject_kind": current.subject_kind,
                "subject_id": str(current.subject_id),
                "action_id": str(action_id),
                "record_id": str(record_id),
                "action_type": chosen.value,
                "assignee_user_id": str(assignee_id),
                "due_at": format_timestamp(due_at),
            },
        )
        result, step = await self._store.write_and_apply(
            transaction,
            self._writer,
            context,
            ACTION_ASSIGNED,
            content,
            scope=current.scope,
            events=(event,),
            occurred_at=at,
            record_id=record_id,
        )
        receipt = written(result)
        expected_transition = "T07" if superseded is None and pending_action_id is None else None
        finding = step.finding
        if not step.applied or finding is None or step.transition_id != expected_transition:
            raise RuntimeError("la proyección no aplicó la acción recién escrita")
        if superseded is not None:
            await transaction.execute(
                _SUPERSEDE,
                {
                    "organization_id": current.organization_id,
                    "plant_id": current.plant_id,
                    "action_id": superseded.action_id,
                },
            )
        await transaction.execute(
            _INSERT_ACTION,
            {
                "action_id": action_id,
                "organization_id": current.organization_id,
                "plant_id": current.plant_id,
                "zone_id": current.zone_id,
                "subject_kind": current.subject_kind,
                "subject_id": current.subject_id,
                "record_id": receipt.record_id,
                "action_type": chosen.value,
                "action_description": description,
                "expected_outcome": expected,
                "assignee_user_id": assignee_id,
                "assigned_by": json.dumps(signer),
                "assigned_at": at,
                "due_at": due_at,
            },
        )
        return AssignmentResult(
            finding_state_id=current.finding_state_id,
            action_id=action_id,
            record_id=receipt.record_id,
            action_type=chosen,
            assignee_user_id=assignee_id,
            assignee_display_name=assignee_name,
            deadline_days=days,
            due_at=due_at,
            supersedes_action_id=None if superseded is None else superseded.action_id,
            transition_id=expected_transition,
            lifecycle_state=finding.state,
            assigned_at=at,
            presented_at=current.presented(at),
            time_aggregated=current.single_occupancy_zone,
            configuration_version=version,
        )

    # --- pasos -----------------------------------------------------------------------------------

    async def _authorized(
        self, context: ScopeContext, finding_state_id: uuid.UUID
    ) -> tuple[ScopeContext, LoopFinding]:
        """Paso 1: la fila de la organización y ``actions.manage`` sobre su zona."""
        if not isinstance(context, ScopeContext) or type(finding_state_id) is not uuid.UUID:
            raise ResourceNotFound()
        rows = await self._database.read(
            context,
            _FINDING_BY_ID,
            {"organization_id": context.organization_id, "finding_state_id": finding_state_id},
        )
        if not rows:
            raise ResourceNotFound()
        place = finding_from(rows[0])
        resource = place.resource()
        authorized = await self._authorizer.authorize(
            context, PermissionKey.ACTIONS_MANAGE, resource
        )
        if authorized.concession_id is not None:  # pragma: no cover - la matriz ya lo deniega
            await self._authorizer.deny(authorized, PermissionKey.ACTIONS_MANAGE, resource)
        return authorized, place

    @staticmethod
    def _superseded(
        current: LoopFinding,
        actions: Mapping[uuid.UUID, ActionRow],
        request: AssignmentRequest,
        pending_action_id: uuid.UUID | None,
    ) -> ActionRow | None:
        """Paso 2: la transición (T07), la reasignación o la segunda mitad de T12 a T14."""
        supersedes = request.supersedes_action_id
        if supersedes is not None and type(supersedes) is not uuid.UUID:
            raise ActionRequestInvalid("supersedes_action_id es un UUID")
        if pending_action_id is not None:
            pending = (
                current.state is S.ACTION_ASSIGNED
                and current.current_action_id == pending_action_id
                and current.due_at is None
                and pending_action_id not in actions
            )
            if not pending:
                raise ActionRejected(LoopDetailCode.TRANSITION_NOT_ALLOWED)
            previous = actions.get(supersedes) if isinstance(supersedes, uuid.UUID) else None
            if supersedes is not None and (previous is None or previous.state == "superseded"):
                raise ActionRejected(LoopDetailCode.TRANSITION_NOT_ALLOWED)
            return previous
        if supersedes is None:
            if isinstance(transition(current.snapshot, AssignAction()), Rejected):
                raise ActionRejected(LoopDetailCode.TRANSITION_NOT_ALLOWED)
            return None
        previous = actions.get(supersedes)
        if (
            current.state not in _REASSIGNABLE
            or current.current_action_id != supersedes
            or previous is None
            or previous.state not in LIVE_ACTION_STATES
        ):
            raise ActionRejected(LoopDetailCode.TRANSITION_NOT_ALLOWED)
        return previous

    @staticmethod
    async def _assignee(
        transaction: Transaction, current: LoopFinding, value: object
    ) -> tuple[uuid.UUID, str]:
        """Paso 5: usuario activo de la organización con alcance sobre la zona; si no, 404."""
        if type(value) is not uuid.UUID:
            raise ActionRequestInvalid("assignee_user_id es un UUID")
        row = (
            await transaction.execute(
                _ASSIGNEE,
                {
                    "organization_id": current.organization_id,
                    "user_id": value,
                    "plant_id": current.plant_id,
                    "zone_id": current.zone_id,
                },
            )
        ).first()
        if row is None:
            raise ResourceNotFound()
        return _uuid(row.user_id), str(row.display_name)

    async def _deadline(
        self, transaction: Transaction, current: LoopFinding, value: object, at: datetime
    ) -> tuple[int, datetime, int | None]:
        """Paso 6: el plazo en el rango de la configuración vigente, en días hábiles."""
        if value is not None and type(value) is not int:
            raise ActionRequestInvalid("deadline_days es un entero")
        settings, version = await self._configuration.settings_in(transaction, current.plant_id)
        days = settings["action_deadline_default_days"] if value is None else value
        if not (
            settings["action_deadline_min_days"] <= days <= settings["action_deadline_max_days"]
        ):
            raise ActionRejected(LoopDetailCode.DEADLINE_OUT_OF_RANGE)
        row = (
            await transaction.execute(
                _PLANT_TIMEZONE,
                {"organization_id": current.organization_id, "plant_id": current.plant_id},
            )
        ).one()
        due_at = business_deadline(at, days, settings.business_calendar, str(row.timezone))
        return days, due_at, version
