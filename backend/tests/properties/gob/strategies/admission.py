"""Generador ``admission_answers`` de PR-GOB-11 (BLM §6, C-PLA-08).

Las tres respuestas booleanas de la prueba de admisión. Con ``st.booleans()`` independientes cada
combinación sale con la misma probabilidad (1/8): la única admitida no queda en minoría frente a
los siete rechazos, y Hypothesis reduce un contraejemplo a la combinación mínima.
"""

from __future__ import annotations

from hypothesis import strategies as st

from vigia_platform.catalog.domain.admission import AdmissionAnswers

__all__ = ["admission_answers"]


def admission_answers() -> st.SearchStrategy[AdmissionAnswers]:
    """``AdmissionAnswers`` con cada respuesta ``True`` o ``False``."""
    return st.builds(
        AdmissionAnswers, standard=st.booleans(), remedy=st.booleans(), subject=st.booleans()
    )
