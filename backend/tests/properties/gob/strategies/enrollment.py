"""Generadores del código de alta (PR-GOB-15; TASK-218).

- ``enrollment_commands``: órdenes de U-03 sobre los códigos de un conjunto pequeño de nodos:
  emitir (reemisión), presentar el código vigente, uno anterior, el de otro nodo o uno inventado,
  y avanzar el reloj (también justo hasta el borde de las 24 h).
- ``enrollment_requests``: lo que presenta el alta (``NodeEnrollmentRequest`` del contrato sin la
  solicitud de firma, que no participa en el código): huella de hardware, versiones y origen.
  El kit de U-01 no exporta un generador de solicitudes de alta (su comprobación de conformidad
  arma una a mano, ``conformance.checks.enrollment.enrollment_request``); este se construye con
  los mismos campos y formas del esquema ``node_enrollment``.

Solo datos generados: los códigos inventados salen del alfabeto en la ejecución, nunca del árbol.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from hypothesis import strategies as st

from vigia_platform.fleet.domain.enrollment_code import ALPHABET, CODE_LENGTH

__all__ = ["EnrollmentCommand", "PresentedRequest", "enrollment_commands", "enrollment_requests"]

NODES = 2
"""Nodos del escenario: con dos, el código de un nodo presentado al otro aparece pronto."""

Which = Literal["current", "previous", "other_node", "invented"]


@dataclass(frozen=True)
class EnrollmentCommand:
    kind: Literal["issue", "present", "advance"]
    node: int = 0
    which: Which = "current"
    age: int = 1
    """Para ``previous``: cuántas emisiones atrás."""
    seconds: float = 0.0
    """Para ``advance``."""


@dataclass(frozen=True)
class PresentedRequest:
    hardware_fingerprint: str
    software_version: str
    contract_version: str
    source_address: str | None
    invented_code: str


_HOUR = 3600.0
_ADVANCES = st.one_of(
    st.sampled_from([1.0, _HOUR, 12 * _HOUR, 24 * _HOUR - 0.001, 24 * _HOUR, 25 * _HOUR]),
    st.floats(0, 30 * _HOUR, allow_nan=False, allow_infinity=False),
)


def enrollment_commands() -> st.SearchStrategy[EnrollmentCommand]:
    node = st.integers(0, NODES - 1)
    return st.one_of(
        st.builds(EnrollmentCommand, kind=st.just("issue"), node=node),
        st.builds(
            EnrollmentCommand,
            kind=st.just("present"),
            node=node,
            which=st.sampled_from(["current", "previous", "other_node", "invented"]),
            age=st.integers(1, 4),
        ),
        st.builds(EnrollmentCommand, kind=st.just("advance"), seconds=_ADVANCES),
    )


_SEMVER = st.builds(
    lambda major, minor, patch: f"{major}.{minor}.{patch}",
    st.integers(0, 20),
    st.integers(0, 50),
    st.integers(0, 99),
)


def _invented(number: int) -> str:
    symbols = []
    for _ in range(CODE_LENGTH):
        number, index = divmod(number, len(ALPHABET))
        symbols.append(ALPHABET[index])
    return "".join(symbols)


def enrollment_requests() -> st.SearchStrategy[PresentedRequest]:
    # Enteros en vez de texto: el ejemplo mínimo es pequeño (``large_base_example``).
    return st.builds(
        PresentedRequest,
        hardware_fingerprint=st.integers(0, 2**256 - 1).map(lambda n: f"{n:064x}"),
        software_version=_SEMVER,
        contract_version=st.sampled_from(["1.0.0", "1.1.0"]),
        source_address=st.one_of(
            st.none(),
            st.ip_addresses().map(str),
            st.sampled_from(["", "unknown", "x" * 70]),
        ),
        invented_code=st.integers(0, len(ALPHABET) ** CODE_LENGTH - 1).map(_invented),
    )
