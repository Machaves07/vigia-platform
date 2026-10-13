"""Los registros capturados por pytest no sobreviven a su ejemplo de Hypothesis (VIG-190).

Con el perfil ``nightly`` (2 000 ejemplos), una propiedad que registraba una excepción por ejemplo
retenía en los manejadores de captura de pytest todas las trazas, con sus marcos, hasta agotar la
memoria del runner. ``tests/conftest.py`` vacía esos registros al terminar cada ejemplo.
"""

from __future__ import annotations

import logging

from _pytest.logging import LogCaptureHandler
from hypothesis import given
from hypothesis import strategies as st

_log = logging.getLogger("tests.hypothesis_captured_logs")


def _held() -> int:
    return sum(
        len(handler.records)
        for handler in logging.getLogger().handlers
        if isinstance(handler, LogCaptureHandler)
    )


@given(value=st.integers())
def test_each_example_starts_without_the_records_of_the_previous_one(value: int) -> None:
    assert _held() == 0
    try:
        raise RuntimeError(value)
    except RuntimeError:
        _log.exception("fallo del ejemplo")
    assert _held() > 0
