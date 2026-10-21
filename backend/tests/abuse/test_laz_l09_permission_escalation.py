"""L-9: SEG-06, la matriz única no concede clasificar al mando de línea.

Mutación: añadir FINDINGS_CLASSIFY a la columna LINE_MANAGER de identity.authz.matrix.
La ruta debe denegar antes de escribir, no confiar en claves definidas por U-04.
"""

import pytest

from tests.loop_classification_support import ClassificationStack
from vigia_platform.shared.context import Role

pytestmark = pytest.mark.integration


def test_laz_l09_line_manager_cannot_claim_the_classification_key(
    classification: ClassificationStack,
) -> None:
    stack = classification
    finding = stack.received()
    who = stack.member(stack.world.a, Role.LINE_MANAGER)
    before = stack.world.finding_state(finding)
    response = stack.classify(who, finding)
    assert response.status_code == 404, response.json()
    assert response.json()["code"] == "not_found"
    assert stack.classifications(finding) == []
    assert stack.world.finding_state(finding) == before
