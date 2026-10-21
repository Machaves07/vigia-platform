"""L-2: SEG-05/SEG-09, fallo cerrado de T09 (la ruta real se ejercita en H-25).

Mutación: quitar ``command.evidence_entry_count < 1`` en la guarda de cierre de lifecycle.
"""

import pytest

from vigia_platform.loop.domain.enums import FindingLifecycleState as State
from vigia_platform.loop.domain.lifecycle import (
    Accepted,
    Close,
    LifecycleCode,
    LifecycleSnapshot,
    Rejected,
    transition,
)


@pytest.mark.parametrize("state", [State.ACTION_ASSIGNED, State.OVERDUE])
def test_laz_l02_no_evidence_or_license_means_no_closure(state: State) -> None:
    snapshot = LifecycleSnapshot(state)
    assert transition(snapshot, Close(0, True)) == Rejected(LifecycleCode.CLOSURE_EVIDENCE_MISSING)
    assert transition(snapshot, Close(1, False)) == Rejected(LifecycleCode.SIGNER_LICENSE_MISSING)
    assert isinstance(transition(snapshot, Close(1, True)), Accepted)
