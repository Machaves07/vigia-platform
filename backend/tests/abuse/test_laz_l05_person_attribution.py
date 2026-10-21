"""L-5: REN-02/SEG-08, segundo validador sobre borrador y hecho citado.

Mutación: omitir la llamada al validador ``person_attribution`` de ``both_policy``
(implementación de ``PersonAttributionValidator.__call__``), no el doble del proveedor.
"""

import pytest

from tests.abuse.test_laz_r5_corpus_assistant import ORGANIZATION, _answer, _built, _documents
from tests.attribution_support import both_policy
from vigia_platform.loop.assistant.consumer import Rejected, RejectionReason, assess_output


@pytest.mark.parametrize("where", ["draft", "cited_fact"])
def test_laz_l05_no_person_blame_reaches_the_assistant_draft(where: str) -> None:
    built = _built()
    document = _documents(built, "El operario omitió el bloqueo de energía.")[where]
    answer = _answer(built, document)
    verdict = assess_output(answer.content_json, built, both_policy(()), ORGANIZATION)
    assert isinstance(verdict, Rejected)
    assert verdict.reason is RejectionReason.PERSON_ATTRIBUTION_DETECTED
