"""L-1: SEG-09/SEG-03 e historial monótono; no se suaviza el expediente sin motivo.

Mutación: quitar ``if not command.reinforced_reason`` de ``_reclassify`` en lifecycle.
La muestra mensual y los límites exactos siguen en sus suites originales.
"""

import pytest

from tests.loop_classification_support import REINFORCED_REASON, ClassificationStack

pytestmark = pytest.mark.integration


def test_laz_l01_each_reclassification_requires_a_reason_and_preserves_history(
    classification: ClassificationStack,
) -> None:
    stack = classification
    who = stack.member(stack.world.a)
    findings = [stack.received() for _ in range(3)]
    for finding in findings:
        assert stack.classify(who, finding).status_code == 201
    for finding in findings:
        before = stack.classifications(finding)
        response = stack.classify(who, finding, "false_positive", "no_person_present")
        assert (response.status_code, response.json().get("detail_code")) == (
            400,
            "loop_reason_required",
        )
        assert stack.classifications(finding) == before
        response = stack.classify(
            who,
            finding,
            "false_positive",
            "no_person_present",
            REINFORCED_REASON,
            reinforced=True,
        )
        assert response.status_code == 201, response.text
        after = stack.classifications(finding)
        assert after[:-1] == before
        assert len(after) == 2
        assert after[-1]["content"]["previous_classification_record_id"] == str(
            before[0]["record_id"]
        )
