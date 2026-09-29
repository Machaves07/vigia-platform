"""Propiedad trivial: comprueba que Hypothesis corre con el perfil y las semillas del conftest."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st


@given(st.integers())
def test_integer_survives_text_round_trip(value: int) -> None:
    assert int(str(value)) == value


def test_active_profile_is_ci_or_nightly() -> None:
    profile = settings.get_current_profile_name()
    assert profile in {"ci", "nightly"}
    assert settings().max_examples == {"ci": 200, "nightly": 2000}[profile]
