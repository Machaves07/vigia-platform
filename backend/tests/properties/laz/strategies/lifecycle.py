"""Generadores del ciclo de vida del hallazgo (TASK-311; PR-LAZ-10, 14 y 15).

- ``action_texts``: textos de **acción** que no pueden aceptarse (PR-LAZ-14), de dos familias:

  - ``on_person``: una intervención **sobre una persona** (formarla, entrenarla, amonestarla,
    sancionarla, evaluarla, darle una charla o un llamado de atención…) con la persona como
    objeto, en sus formas activa, nominal y pasiva, con tildes, mayúsculas o confusables al azar;
  - ``attribution``: una acción sobre la zona que **arrastra** un texto del vocabulario de
    atribución (``person_attribution_texts`` de TASK-340: un cargo con verbo causal, un cargo como
    causa, error humano, documento, nombre propio, correo, teléfono o placa).

  Devuelve el texto y su familia; el rechazo esperado es ``action_on_person_forbidden`` o
  ``person_attribution_detected``.
- ``neutral_action_texts``: acciones de ingeniería o de proceso **sobre la zona** (instalar,
  reubicar, señalizar, actualizar el procedimiento…) sobre equipos y zonas con mayúsculas, que se
  aceptan; los equipos con marca citan su nombre declarado (``NeutralAction.declared``).
- ``lifecycle_command_sequences``: la **base** de las secuencias de comandos del ciclo de vida
  (asignar, reasignar, añadir evidencia, cambiar la configuración del lazo de la planta y avanzar
  el reloj). TASK-343, 312 y 313 la amplían con cierre, reapertura, incerrable y plazos para la
  prueba con estado de TASK-314; los comandos son valores puros, sin base ni reloj.
- ``deadline_command_sequences`` (TASK-313): la base con la tarea ``evaluate_deadlines``, saltos
  de reloj de días, reclasificaciones, el umbral del gerente y la declaración de incerrable.
- ``plant_policies`` y ``unclosable_*`` (TASK-312; PR-LAZ-12): la política de planta tal como la
  puede devolver ``GateQueryPort.plant_policy`` (cargada y completa, sin firmar, revocada o
  cargada **a medias**, que el servicio resuelve cerrada) y lo que lleva una declaración de
  incerrable (cuántos de los cinco campos van, la política, la fecha de revisión en meses y si la
  firma lleva licencia) y una revisión (su salida, la fecha nueva y la licencia). Son valores
  puros: el ``PlantPolicyState`` de cada uno lo construye ``PlantPolicy.state``.
  ``DeclareUnclosable`` y ``ReviewUnclosable`` llevan un intento **cualquiera** (válido o no) para
  la prueba con estado de TASK-314; no entran en ``_commands()``, cuyos consumidores
  (``loop_deadlines_support``) rechazan un comando sin rama. ``DeclareUnclosableFinding`` es la
  declaración siempre válida de los plazos (TASK-313).

Solo datos generados: los cargos, equipos y zonas son sintéticos y de uso corriente.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal

from hypothesis import strategies as st

from tests.properties.laz.strategies.texts import (
    DECLARED_BRANDS,
    EQUIPMENT,
    QUALIFIERS,
    person_attribution_texts,
)
from vigia_platform.catalog.domain.ports import PlantPolicyState

__all__ = [
    "ACTION_TYPES",
    "ActionText",
    "AddEvidence",
    "Assign",
    "ChangeDeadlineRange",
    "ChangeHighlightDays",
    "ClassifyFinding",
    "CloseFinding",
    "DeactivateAssignee",
    "DeclarationAttempt",
    "DeclareUnclosable",
    "DeclareUnclosableFinding",
    "EvaluateDeadlines",
    "LateSegment",
    "LifecycleCommand",
    "NeutralAction",
    "PlantPolicy",
    "PlantPolicyKind",
    "Reassign",
    "Reclassify",
    "ReopenFinding",
    "ResolveQueue",
    "ReviewAttempt",
    "ReviewUnclosable",
    "RevokeGate",
    "RevokedPolicyState",
    "Tick",
    "action_texts",
    "deadline_command_sequences",
    "deadline_ranges",
    "declaration_attempts",
    "lifecycle_command_sequences",
    "neutral_action_texts",
    "not_an_action_type",
    "plant_policies",
    "review_attempts",
    "stateful_lifecycle_command_sequences",
    "unclosable_texts",
]

ACTION_TYPES: Final = ("engineering", "process")
"""La lista cerrada ``action_type`` (DE §1), escrita a mano para la metapropiedad."""

ActionFamily = Literal["on_person", "attribution"]

_PERSONS: Final = (
    "operario", "operaria", "operarios", "trabajador", "trabajadores", "empleado", "empleados",
    "personal", "auxiliar", "auxiliares", "técnico", "técnicos", "supervisor", "supervisora",
    "montacarguista", "montacarguistas", "conductor", "conductores", "contratista", "aprendiz",
    "colaboradores", "cuadrilla", "persona", "personas", "responsable", "responsables",
    "usuarios", "usuarias", "peatones", "peatón", "visitantes", "infractor", "causante",
    "compañero", "compañera", "compañeros",
)  # fmt: skip
_ENCLITIC: Final = (
    "Sancionarlo", "Sancionarlos", "Amonestarla", "Amonestarle", "Capacitarlos", "Reentrenarlas",
    "Despedirlo", "Multarlo", "Sensibilizarlos", "Reprenderlo", "Instruirlos",
)  # fmt: skip
"""Verbos que solo se aplican a personas con el pronombre pegado (revisión, ronda 2)."""
_SANCTIONS: Final = (
    "Tomar medidas disciplinarias contra", "Abrir un proceso disciplinario contra",
    "Iniciar acciones en contra de", "Aplicar una sanción contra",
)  # fmt: skip
"""Formas nominales con la persona tras «contra» (revisión de VIG-223, ronda 1)."""
_DETERMINERS: Final = ("", "el ", "la ", "los ", "las ", "todo el ", "cada ", "los nuevos ")
_VERBS: Final = (
    "Capacitar", "Recapacitar", "Entrenar", "Reentrenar", "Formar", "Instruir", "Amonestar",
    "Sancionar", "Evaluar", "Reprender", "Despedir", "Suspender", "Multar", "Sensibilizar",
    "Concientizar", "Disciplinar", "Retroalimentar",
)  # fmt: skip
_NOUNS: Final = (
    "Capacitación", "Entrenamiento", "Formación", "Amonestación", "Sanción", "Evaluación",
    "Charla de seguridad", "Reinducción", "Inducción", "Llamado de atención", "Retroalimentación",
)  # fmt: skip
_PASSIVE: Final = (
    "capacitado", "capacitados", "entrenado", "entrenada", "formados", "amonestado",
    "sancionados", "evaluado", "reentrenados", "sensibilizados",
)  # fmt: skip
_COMPLEMENTS: Final = (
    "", " en el uso del resguardo", " sobre el procedimiento de bloqueo",
    " en la zona de carga", " por el cruce en el pasillo", " antes del siguiente turno",
)  # fmt: skip
_MIN_CHARS: Final = 20
"""Mínimo de ``action_description`` (DE §2.5) y de los textos libres de §2.7."""
_LONG_TEXT_MAX: Final = 2000
"""Máximo de ``LongText`` (DE §2.7)."""
_UTC: Final = UTC
_SIGNED_FROM: Final = datetime(2026, 1, 1, tzinfo=UTC)
_SIGNED_TO: Final = datetime(2026, 12, 31, tzinfo=UTC)
_REVIEW_MONTHS: Final = (-3, -1, 0, 1, 3, 6, 11, 12, 13, 18)
"""Meses de la fecha de revisión: en rango son ``(0, unclosable_review_max_months]`` (1 a 12)."""
_CONFUSABLES: Final = {
    "a": chr(0x0430),
    "o": chr(0x043E),
    "e": chr(0x0435),
    "c": chr(0x0441),
    "p": chr(0x0440),
}
"""Letras latinas y su homoglifo cirílico (UTS #39)."""

_ZONE_VERBS: Final = (
    "Instalar un resguardo fijo en", "Reubicar la estación de carga junto a",
    "Señalizar y demarcar el pasillo peatonal de", "Reemplazar el sensor de presencia de",
    "Actualizar el procedimiento de bloqueo y etiquetado de", "Ajustar la velocidad máxima de",
    "Instalar una barrera física entre el pasillo y", "Rediseñar el acceso a",
    "Programar el mantenimiento preventivo de", "Reforzar el anclaje de",
)  # fmt: skip
_ZONE_TAILS: Final = (
    "", " antes del próximo mes", " según el procedimiento PRO-001",
    " conforme a NTC 2050 e ISO 45001:2018", " con la guarda verificada el 2026-10-18",
    " en la Zona Norte de la Planta Principal", " con un costo de 1.500.000 pesos",
)  # fmt: skip


_NEAR_TYPES: Final = (
    "Engineering", "PROCESS", " engineering", "process ", "training", "sanction", "warning",
    "formacion", "capacitación", "amonestación", "", "engineering,process", "engineerin",
)  # fmt: skip
"""Casi tipos: mayúsculas, espacios, cortes y lo que el tipo **nunca** es (formación, sanción)."""


@dataclass(frozen=True)
class ActionText:
    text: str
    family: ActionFamily


@dataclass(frozen=True)
class NeutralAction:
    text: str
    declared: tuple[str, ...]
    """Los nombres de equipo declarados que el texto cita (vocabulario declarado)."""


def _cut(draw: st.DrawFn, text: str) -> str:
    """Una palabra larga cortada con guion, punto o guion y espacio («opera-rios»)."""
    words = text.split(" ")
    long = [index for index, word in enumerate(words) if len(word) >= 6 and word.isalpha()]
    if not long:
        return text
    index = draw(st.sampled_from(long))
    word = words[index]
    at = draw(st.integers(2, len(word) - 2))
    words[index] = word[:at] + draw(st.sampled_from(("-", ".", "- "))) + word[at:]
    return " ".join(words)


def _disguised(draw: st.DrawFn, text: str) -> str:
    match draw(st.sampled_from(("plain", "upper", "confusable", "no_accents", "cut"))):
        case "cut":
            return _cut(draw, text)
        case "upper":
            return text.upper()
        case "confusable":
            return "".join(
                _CONFUSABLES.get(char, char) if draw(st.booleans()) else char for char in text
            )
        case "no_accents":
            return text.translate(str.maketrans("áéíóúÁÉÍÓÚ", "aeiouAEIOU"))
    return text


@st.composite
def _on_person(draw: st.DrawFn) -> str:
    person = draw(st.sampled_from(_PERSONS))
    determiner = draw(st.sampled_from(_DETERMINERS))
    complement = draw(st.sampled_from(_COMPLEMENTS))
    kinds = ("verb", "verb_bare", "noun", "passive", "against", "whoever", "enclitic", "those")
    match draw(st.sampled_from(kinds)):
        case "enclitic":
            text = f"{draw(st.sampled_from(_ENCLITIC))} por el cruce en la zona de carga"
        case "those":
            verb = draw(st.sampled_from(_VERBS))
            those = draw(st.sampled_from(("al que", "a los que", "a las que", "a la que")))
            text = f"{verb} {those} cruzaron la zona de carga"
        case "against":
            text = f"{draw(st.sampled_from(_SANCTIONS))} {determiner or 'el '}{person}{complement}"
        case "whoever":
            verb = draw(st.sampled_from(_VERBS))
            text = f"{verb} a {draw(st.sampled_from(('quien', 'quienes')))} cruzó la zona"
        case "verb":
            verb = draw(st.sampled_from(_VERBS))
            preposition = "al " if determiner == "el " else "a "
            target = person if preposition == "al " else f"{determiner}{person}"
            text = f"{verb} {preposition}{target}{complement}"
        case "verb_bare":
            text = f"{draw(st.sampled_from(_VERBS))} {person}{complement}"
        case "noun":
            noun = draw(st.sampled_from(_NOUNS))
            preposition = draw(st.sampled_from(("al ", "a los ", "del ", "de los ", "para el ")))
            text = f"{noun} {preposition}{person}{complement}"
        case _:
            text = (
                f"Que {determiner or 'el '}{person} sea {draw(st.sampled_from(_PASSIVE))}"
                f"{complement}"
            )
    if len(text) < _MIN_CHARS:
        # La descripción tiene un mínimo de 20 caracteres: debajo, el rechazo sería del esquema.
        text += " de la zona de carga norte"
    return _disguised(draw, text)


@st.composite
def action_texts(draw: st.DrawFn) -> ActionText:
    """Una acción que recae sobre una persona o que arrastra una atribución (PR-LAZ-14)."""
    family: ActionFamily = draw(st.sampled_from(("on_person", "attribution")))
    if family == "on_person":
        return ActionText(draw(_on_person()), family)
    head = draw(st.sampled_from(_ZONE_VERBS))
    attribution = draw(person_attribution_texts())
    return ActionText(f"{head} la zona porque {attribution.text}", family)


@st.composite
def neutral_action_texts(draw: st.DrawFn) -> NeutralAction:
    """Una acción de ingeniería o de proceso sobre la zona o el equipo, que debe aceptarse."""
    equipment = draw(st.sampled_from(EQUIPMENT))
    declared: tuple[str, ...] = ()
    if draw(st.booleans()):
        name = f"{equipment} {draw(st.sampled_from(DECLARED_BRANDS))}"
        declared = (name,)
    else:
        name = f"{equipment} {draw(st.sampled_from(QUALIFIERS))}"
    if draw(st.booleans()):
        name = name.upper()
    head = draw(st.sampled_from(_ZONE_VERBS))
    tail = draw(st.sampled_from(_ZONE_TAILS))
    return NeutralAction(f"{head} la {name}{tail}", declared)


@st.composite
def not_an_action_type(draw: st.DrawFn) -> str:
    """Cualquier cadena fuera de la lista cerrada (también casi iguales: mayúsculas, espacios)."""
    value = draw(st.one_of(st.sampled_from(_NEAR_TYPES), st.text(max_size=40)))
    return value if value not in ACTION_TYPES else value + "_x"


# --- la política de planta y el incerrable (TASK-312; PR-LAZ-12) ---------------------------------


PlantPolicyKind = Literal["loaded", "not_signed", "revoked", "partial"]
"""Las cuatro formas con las que el puerto puede contestar: cargada y completa (``loaded``), sin
política firmada (``not_signed``), revocada (``revoked``) y cargada **a medias** (``partial``:
``loaded = true`` pero sin alguno de los cuatro datos que el registro copia, que el servicio
resuelve **cerrada** como si el puerto no hubiera contestado)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RevokedPolicyState(PlantPolicyState):
    """Una política revocada: la forma de §1.2 de las interfaces de U-03 más la marca de
    revocación, que el puerto de hoy no expone y que ``loop.application.unclosable.policy_status``
    honra si aparece (BR-LAZ-56: no se declaran nuevos incerrables)."""

    revoked: bool = True


@dataclass(frozen=True, slots=True)
class PlantPolicy:
    """Lo que ``GateQueryPort.plant_policy`` puede devolver (interfaces §1.2) y su estado."""

    kind: PlantPolicyKind
    policy_id: uuid.UUID
    version: int
    signed_at: datetime
    document_sha256: str

    def state(self) -> PlantPolicyState:
        """El valor del puerto: ``loaded = false`` sin los demás campos (§1.2)."""
        if self.kind == "not_signed":
            return PlantPolicyState(loaded=False)
        if self.kind == "revoked":
            return RevokedPolicyState(
                loaded=True,
                policy_id=self.policy_id,
                version=self.version,
                signed_at=self.signed_at,
                document_sha256=self.document_sha256,
            )
        missing = {
            "partial": ("document_sha256",),
        }.get(self.kind, ())
        return PlantPolicyState(
            loaded=True,
            policy_id=None if "policy_id" in missing else self.policy_id,
            version=None if "version" in missing else self.version,
            signed_at=None if "signed_at" in missing else self.signed_at,
            document_sha256=None if "document_sha256" in missing else self.document_sha256,
        )


@st.composite
def plant_policies(draw: st.DrawFn) -> PlantPolicy:
    """La política de la planta en cualquiera de sus cuatro formas (BR-LAZ-53 a 56)."""
    kind: PlantPolicyKind = draw(st.sampled_from(("loaded", "not_signed", "revoked", "partial")))
    return PlantPolicy(
        kind=kind,
        policy_id=draw(st.uuids(version=4)),
        version=draw(st.integers(1, 2**30)),
        signed_at=draw(
            st.datetimes(timezones=st.just(_UTC), min_value=_SIGNED_FROM, max_value=_SIGNED_TO)
        ),
        document_sha256=draw(st.from_regex(r"[0-9a-f]{64}", fullmatch=True)),
    )


@st.composite
def unclosable_texts(draw: st.DrawFn) -> str:
    """Un texto libre de §2.7 (20 a 2000 caracteres) sobre la zona, del vocabulario permitido."""
    text = draw(neutral_action_texts()).text
    while len(text) < _MIN_CHARS:
        text = f"{text} de la zona de carga"
    return text[:_LONG_TEXT_MAX]


@st.composite
def declaration_attempts(draw: st.DrawFn) -> DeclarationAttempt:
    """Una declaración de incerrable: los cinco campos, la política, la fecha y la firma."""
    return DeclarationAttempt(
        fields=draw(st.integers(0, 5)),
        policy=draw(plant_policies()),
        review_months=draw(st.one_of(st.none(), st.sampled_from(_REVIEW_MONTHS))),
        license=draw(st.booleans()),
    )


@st.composite
def review_attempts(draw: st.DrawFn) -> ReviewAttempt:
    """Una revisión de incerrable: la salida, la fecha nueva y la firma."""
    return ReviewAttempt(
        outcome=draw(st.sampled_from(("renewed", "converted_to_action"))),
        review_months=draw(st.one_of(st.none(), st.sampled_from(_REVIEW_MONTHS))),
        license=draw(st.booleans()),
    )


# --- secuencias de comandos (base de PR-LAZ-10) --------------------------------------------------


@dataclass(frozen=True)
class DeclarationAttempt:
    """Lo que lleva una declaración de incerrable, sin base ni reloj.

    ``fields`` es cuántos de los cinco campos van (5: los cinco); ``policy``, lo que devuelve el
    puerto; ``review_months``, los meses de la fecha de revisión respecto al instante de la
    declaración (``None``: la fecha por defecto de la planta); ``license``, si la firma lleva
    licencia profesional."""

    fields: int
    policy: PlantPolicy
    review_months: int | None
    license: bool


@dataclass(frozen=True)
class ReviewAttempt:
    """Lo que lleva una revisión de incerrable: ``renewed`` o ``converted_to_action``."""

    outcome: str
    review_months: int | None
    license: bool


@dataclass(frozen=True)
class DeclareUnclosable:
    """Declarar SIN ALTERNATIVA SEGURA DISPONIBLE (T10) con los cinco campos y la política."""

    attempt: DeclarationAttempt


@dataclass(frozen=True)
class ReviewUnclosable:
    """Revisar una declaración vigente: renovarla (T11) o convertirla en acción (T12)."""

    attempt: ReviewAttempt


@dataclass(frozen=True)
class Assign:
    """Asignar la acción (T07) con ``deadline_days`` (``None``: el de la planta)."""

    action_type: str
    deadline_days: int | None


@dataclass(frozen=True)
class Reassign:
    """Reasignar la acción vigente (``supersedes_action_id``) con otro plazo."""

    deadline_days: int | None


@dataclass(frozen=True)
class AddEvidence:
    """Añadir una entrada de evidencia sin adjuntos a la acción vigente."""


@dataclass(frozen=True)
class ChangeDeadlineRange:
    """Cambiar el rango de plazos y el calendario de la planta (``loop.config``, TASK-304)."""

    min_days: int
    default_days: int
    max_days: int
    working_weekdays: tuple[int, ...]


@dataclass(frozen=True)
class Tick:
    """Avanzar el reloj simulado."""

    seconds: int


@dataclass(frozen=True)
class CloseFinding:
    """``cerrar`` (T09, VIG-233): cerrar con la descripción y la licencia de la prueba."""


@dataclass(frozen=True)
class ReopenFinding:
    """``reabrir`` (T13 o T14, VIG-233): reabrir con una acción correctiva nueva."""

    action_type: str
    deadline_days: int | None


@dataclass(frozen=True)
class EvaluateDeadlines:
    """Una ejecución de la tarea ``evaluate_deadlines`` (TASK-313): un comando del sistema."""


@dataclass(frozen=True)
class Reclassify:
    """Reclasificar con motivo reforzado (TASK-313): ``false_positive`` suspende el lazo (T15) y
    ``confirmed`` lo reanuda (T16)."""

    outcome: Literal["false_positive", "confirmed"]


@dataclass(frozen=True)
class ChangeHighlightDays:
    """Cambiar ``manager_highlight_overdue_days`` de la planta (``loop.config``, TASK-313)."""

    days: int


@dataclass(frozen=True)
class DeclareUnclosableFinding:
    """Declarar el hallazgo incerrable (T10) con sus cinco campos y la política firmada."""


@dataclass(frozen=True)
class ClassifyFinding:
    """Clasificar o reclasificar un hallazgo con salida y motivo reforzado."""

    outcome: Literal["confirmed", "false_positive"]
    reinforced: bool


@dataclass(frozen=True)
class ResolveQueue:
    """Resolver una detección de cola; se conserva para ejercitar T05/T06 y sus rechazos."""

    outcome: Literal["review_confirmed", "review_discarded"]


@dataclass(frozen=True)
class LateSegment:
    """Adjuntar un tramo tardío (T21), que nunca reabre el lazo."""


@dataclass(frozen=True)
class DeactivateAssignee:
    """Desactivar al responsable sin cambiar el estado de su acción."""


@dataclass(frozen=True)
class RevokeGate:
    """Revocar la compuerta de uso: conserva los hechos ya escritos."""


type LifecycleCommand = (
    Assign
    | Reassign
    | AddEvidence
    | ChangeDeadlineRange
    | Tick
    | CloseFinding
    | ReopenFinding
    | EvaluateDeadlines
    | Reclassify
    | ChangeHighlightDays
    | DeclareUnclosableFinding
    | DeclareUnclosable
    | ReviewUnclosable
    | ClassifyFinding
    | ResolveQueue
    | LateSegment
    | DeactivateAssignee
    | RevokeGate
)
"""Los comandos de la base, los de los plazos (TASK-313) y los del incerrable (TASK-312); las
tareas siguientes añaden los suyos a esta unión."""


@st.composite
def deadline_ranges(draw: st.DrawFn) -> ChangeDeadlineRange:
    """Un rango de plazos válido (``1 ≤ min ≤ default ≤ max ≤ 180``) y un calendario."""
    low, middle, high = sorted(draw(st.lists(st.integers(1, 180), min_size=3, max_size=3)))
    weekdays = draw(st.sets(st.integers(1, 7), min_size=1, max_size=7))
    return ChangeDeadlineRange(low, middle, high, tuple(sorted(weekdays)))


def _deadline() -> st.SearchStrategy[int | None]:
    # Bordes del rango por defecto (1 a 180) y fuera de él, más el valor de la planta (None).
    return st.one_of(st.none(), st.integers(0, 181), st.sampled_from((1, 30, 180)))


def _commands() -> st.SearchStrategy[LifecycleCommand]:
    return st.one_of(
        st.builds(Assign, st.sampled_from(ACTION_TYPES), _deadline()),
        st.builds(Reassign, _deadline()),
        st.just(AddEvidence()),
        deadline_ranges(),
        st.builds(Tick, st.integers(1, 3 * 24 * 3600)),
        st.just(CloseFinding()),
        st.builds(ReopenFinding, st.sampled_from(ACTION_TYPES), _deadline()),
    )


def lifecycle_command_sequences(
    max_size: int = 8,
) -> st.SearchStrategy[list[LifecycleCommand]]:
    """Una secuencia de comandos sobre un hallazgo abierto (incluida la vacía)."""
    return st.lists(_commands(), max_size=max_size)


def stateful_lifecycle_command_sequences(
    max_size: int = 50,
    *,
    min_size: int = 0,
) -> st.SearchStrategy[list[LifecycleCommand]]:
    """Los catorce comandos de PR-LAZ-10, incluida la secuencia vacía.

    La estrategia histórica se conserva para las propiedades de tareas anteriores que usan una
    aplicación parcial; esta es la variante completa que consume la máquina estatal.
    """
    return st.lists(
        st.one_of(
            st.builds(
                ClassifyFinding,
                st.sampled_from(("confirmed", "false_positive")),
                st.booleans(),
            ),
            st.builds(ResolveQueue, st.sampled_from(("review_confirmed", "review_discarded"))),
            st.builds(Assign, st.sampled_from(ACTION_TYPES), _deadline()),
            st.just(AddEvidence()),
            st.just(CloseFinding()),
            st.builds(DeclareUnclosable, declaration_attempts()),
            st.builds(ReviewUnclosable, review_attempts()),
            st.builds(ReopenFinding, st.sampled_from(ACTION_TYPES), _deadline()),
            st.builds(Tick, st.integers(1, 3 * 24 * 3600)),
            st.just(LateSegment()),
            st.just(DeactivateAssignee()),
            st.just(RevokeGate()),
            deadline_ranges(),
        ),
        min_size=min_size,
        max_size=max_size,
    )


def _deadline_commands() -> st.SearchStrategy[LifecycleCommand]:
    return st.one_of(
        _commands(),
        # Saltos de reloj con huecos de días: el plazo vence y el umbral del gerente se cumple.
        st.builds(Tick, st.integers(3600, 6 * 24 * 3600)),
        st.just(EvaluateDeadlines()),
        st.just(EvaluateDeadlines()),
        st.builds(Reclassify, st.sampled_from(("false_positive", "confirmed"))),
        st.builds(ChangeHighlightDays, st.integers(1, 5)),
        st.just(DeclareUnclosableFinding()),
    )


def deadline_command_sequences(
    max_size: int = 10,
) -> st.SearchStrategy[list[LifecycleCommand]]:
    """La base ampliada con los plazos (TASK-313): la tarea ``evaluate_deadlines`` intercalada con
    saltos de reloj de días, reclasificaciones que suspenden y reanudan, cambios del umbral del
    gerente y la declaración de incerrable (PR-LAZ-13 y 15; la usa también TASK-314)."""
    return st.lists(_deadline_commands(), max_size=max_size)
