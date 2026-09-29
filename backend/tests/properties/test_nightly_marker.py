"""El marcador ``nightly`` se omite en ``ci`` y corre con ``--hypothesis-profile=nightly``."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st


@pytest.mark.nightly
@given(st.binary())
def test_nightly_property_runs_with_two_thousand_examples(data: bytes) -> None:
    assert settings().max_examples == 2000
    assert bytes(bytearray(data)) == data
