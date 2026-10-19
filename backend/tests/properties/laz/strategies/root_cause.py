"""Generadores de C-PLA-26 (PR-LAZ-16 a 18; TASK-315): ``cause_categories``,
``analysis_step_commands`` y ``analysis_flows``.

- ``cause_categories``: valores que alguien podría mandar como categoría de causa. Las tres de la
  lista (``process``, ``engineering``, ``organization``), sus variantes de forma (mayúsculas,
  espacios, plural británico, tildes en NFD), valores que nombran a una **persona o una conducta**
  (``person``, ``operario``, ``human_error``…) y cualquier texto. Solo las tres exactas valen.
- ``analysis_step_commands``: una orden del flujo guiado sobre un análisis: escribir el paso *n*
  (de 0 a 7, para incluir los inválidos; avanzar, retroceder y editar salen de aquí) con un
  enunciado de una de cuatro clases (``valid``, ``short``, ``blocked`` o ``attributed``) y, a veces,
  una categoría de ``cause_categories``; o intentar firmar (``SignCommand``).
- ``analysis_flows``: un flujo completo hasta la firma (``k`` porqués y un ``SignCommand``) con
  textos válidos que mezclan NFC, NFD y tildes, para comparar lo firmado con lo aceptado.

``SignCommand`` dice qué trae la firma: cada texto final presente, ausente o en blanco, la
categoría de la causa raíz (de ``cause_categories`` o ninguna), ramas del diagrama y la licencia.
La generación depende solo de lo dibujado (retro 14): ni marcas de tiempo ni estado de la base.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Final, Literal

from hypothesis import strategies as st

from tests.loop_classification_support import ATTRIBUTION_MARKER
from tests.properties.laz.strategies.classification import BLOCKED_TEXT
from vigia_platform.loop.domain.root_cause import CAUSE_CATEGORIES

__all__ = [
    "ATTRIBUTED_TEXT",
    "PERSON_LIKE_CATEGORIES",
    "THREE",
    "VALID_STATEMENTS",
    "AnalysisCommand",
    "AnalysisFlow",
    "FinalPresence",
    "PutStepCommand",
    "SignCommand",
    "StatementKind",
    "analysis_flows",
    "analysis_step_commands",
    "cause_categories",
    "sign_commands",
    "statement_text",
]

type StatementKind = Literal["valid", "short", "blocked", "attributed"]
type FinalPresence = Literal["present", "absent", "blank"]

THREE: Final = tuple(sorted(CAUSE_CATEGORIES))
PERSON_LIKE_CATEGORIES: Final = (
    "person",
    "persona",
    "people",
    "operator",
    "operario",
    "worker",
    "trabajador",
    "human_error",
    "error_humano",
    "behavior",
    "behaviour",
    "conducta",
    "individual_behavior",
    "comportamiento_individual",
    "supervisor",
    "shift",
    "turno",
    "crew",
    "cuadrilla",
    "negligence",
)
"""Categorías que nombran a una persona, un grupo de personas o una conducta (H-28, P7)."""

VALID_STATEMENTS: Final = (
    "La carretilla cruzó el pasillo peatonal con la estación de carga energizada",
    "El pasillo de carga comparte el trazado con el acceso peatonal de la zona",
    "El plano de la zona no separa los flujos de carga y de paso peatonal",
    "La revisión del trazado no incluyó la ampliación de la estación de carga",
    "El procedimiento de cambios de trazado no exige revisar los flujos",
    "Revisión del diseño sin análisis de la interacción entre máquina y acceso",
)
"""Enunciados neutros de 20 a 1000 caracteres que pasan la política y los validadores."""
SHORT_STATEMENT: Final = "Pasillo sin señal."
"""18 caracteres: por debajo del mínimo (20)."""
ATTRIBUTED_TEXT: Final = f"El atasco de la prensa ocurrió porque {ATTRIBUTION_MARKER} sin avisar"
"""El doble de ``person_attribution`` lo rechaza (TASK-340 trae el real)."""


def _variants(category: str) -> st.SearchStrategy[str]:
    return st.sampled_from(
        (
            category.upper(),
            category.title(),
            f" {category}",
            f"{category} ",
            f"{category}s",
            category.replace("ization", "isation"),
            unicodedata.normalize("NFD", category.replace("e", "é")),
        )
    )


def cause_categories() -> st.SearchStrategy[str]:
    """Una categoría candidata: válida, variante de forma, de persona o cualquier texto."""
    return st.one_of(
        st.sampled_from(THREE),
        st.sampled_from(THREE).flatmap(_variants),
        st.sampled_from(PERSON_LIKE_CATEGORIES),
        st.text(max_size=40),
    )


def statement_text(kind: StatementKind, index: int = 0) -> str:
    """El enunciado de cada clase (``valid`` rota entre ``VALID_STATEMENTS``)."""
    if kind == "valid":
        return VALID_STATEMENTS[index % len(VALID_STATEMENTS)]
    if kind == "short":
        return SHORT_STATEMENT
    if kind == "blocked":
        return BLOCKED_TEXT
    return ATTRIBUTED_TEXT


@dataclass(frozen=True)
class PutStepCommand:
    step_index: int
    kind: StatementKind
    text_index: int
    category: str | None

    @property
    def statement(self) -> str:
        return statement_text(self.kind, self.text_index)


@dataclass(frozen=True)
class SignCommand:
    root_cause_category: str | None
    root_cause_statement: FinalPresence
    conclusion: FinalPresence
    branch_categories: tuple[str, ...]
    license: FinalPresence


type AnalysisCommand = PutStepCommand | SignCommand


def _final(draw: st.DrawFn) -> FinalPresence:
    return draw(st.sampled_from(("present", "present", "present", "absent", "blank")))


@st.composite
def put_step_commands(draw: st.DrawFn) -> PutStepCommand:
    return PutStepCommand(
        step_index=draw(st.integers(min_value=0, max_value=7)),
        kind=draw(st.sampled_from(("valid", "valid", "valid", "short", "blocked", "attributed"))),
        text_index=draw(st.integers(min_value=0, max_value=len(VALID_STATEMENTS) - 1)),
        category=draw(st.one_of(st.none(), st.sampled_from(THREE), cause_categories())),
    )


@st.composite
def sign_commands(draw: st.DrawFn) -> SignCommand:
    category = draw(st.one_of(st.sampled_from(THREE), st.none(), cause_categories()))
    return SignCommand(
        root_cause_category=category,
        root_cause_statement=_final(draw),
        conclusion=_final(draw),
        branch_categories=tuple(
            draw(
                st.lists(
                    st.one_of(st.sampled_from(THREE), cause_categories()), min_size=0, max_size=3
                )
            )
        ),
        license=_final(draw),
    )


def analysis_step_commands() -> st.SearchStrategy[AnalysisCommand]:
    """Una orden del flujo guiado: casi siempre un paso; a veces, un intento de firma."""
    return st.one_of(put_step_commands(), put_step_commands(), put_step_commands(), sign_commands())


@dataclass(frozen=True)
class AnalysisFlow:
    """``k`` porqués válidos (en NFC o NFD) y una firma."""

    statements: tuple[str, ...]
    sign: SignCommand
    root_cause_statement: str
    conclusion: str


@st.composite
def analysis_flows(draw: st.DrawFn) -> AnalysisFlow:
    def accepted() -> str:
        text = draw(st.sampled_from(VALID_STATEMENTS))
        return unicodedata.normalize(draw(st.sampled_from(("NFC", "NFD"))), text)

    count = draw(st.integers(min_value=0, max_value=5))
    sign = draw(sign_commands())
    return AnalysisFlow(
        statements=tuple(accepted() for _ in range(count)),
        sign=sign,
        root_cause_statement=accepted(),
        conclusion=accepted(),
    )
