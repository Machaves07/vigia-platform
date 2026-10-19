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

Solo datos generados: los cargos, equipos y zonas son sintéticos y de uso corriente.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from hypothesis import strategies as st

from tests.properties.laz.strategies.texts import (
    DECLARED_BRANDS,
    EQUIPMENT,
    QUALIFIERS,
    person_attribution_texts,
)

__all__ = [
    "ACTION_TYPES",
    "ActionText",
    "AddEvidence",
    "Assign",
    "ChangeDeadlineRange",
    "CloseFinding",
    "LifecycleCommand",
    "NeutralAction",
    "Reassign",
    "ReopenFinding",
    "Tick",
    "action_texts",
    "deadline_ranges",
    "lifecycle_command_sequences",
    "neutral_action_texts",
    "not_an_action_type",
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
"""Mínimo de ``action_description`` (DE §2.5)."""
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


# --- secuencias de comandos (base de PR-LAZ-10) --------------------------------------------------


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


type LifecycleCommand = (
    Assign | Reassign | AddEvidence | ChangeDeadlineRange | Tick | CloseFinding | ReopenFinding
)
"""Los comandos de la base; las tareas siguientes añaden los suyos a esta unión."""


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
