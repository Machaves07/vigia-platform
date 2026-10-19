"""PR-LAZ-20 · toda salida del asistente pasa por el esquema, la lista de alias y los dos
validadores **antes** de mostrarse (TASK-316; BR-LAZ-69 a 72; PAT-LAZ-SEG-08).

``assess_output`` (lo que el consumidor aplica a cada respuesta ``end_turn``) con la política de
texto libre **real** de U-04 (``blocked_vocabulary`` y ``person_attribution``, ``both_policy``):

- **Propiedad**: para cualquier contexto y cualquier salida generada (``assistant_outputs``:
  forma rota o válida, citas dentro o fuera de la lista de alias o con otra clase, con o sin
  declaración de evidencia insuficiente, y textos permitidos, bloqueados, de atribución, del
  corpus R5 o de inyección), la salida se acepta **si y solo si** valida contra el esquema
  estricto, cita o declara evidencia insuficiente, cada cita es un alias de la lista con su clase,
  y la política acepta el borrador **y** cada hecho citado. Lo aceptado es exactamente el texto
  que la política devuelve (NFC); lo rechazado no deja nada.
- **Inyección por el texto del estándar** (BR-LAZ-69): con una instrucción incrustada en el
  estándar (``injection_payloads``), un proveedor que la obedeciera (copiando el estándar en el
  borrador o en una cita) nunca produce un borrador aceptado.
- Un borrador sin citas ni declaración, o con una cita fuera de la lista, se descarta aunque su
  texto sea inocuo (BR-LAZ-71; tech-stack §2.3).

La prueba con la base (el borrador rechazado no deja fila y la solicitud queda
``rejected_by_validators``) está en ``tests/integration/test_loop_assistant_flow.py``.
"""

from __future__ import annotations

import json
import uuid
from typing import Final

from hypothesis import given
from hypothesis import strategies as st

from tests.attribution_support import both_policy
from tests.properties.laz.strategies.assistant import (
    GeneratedContext,
    assistant_contexts,
    assistant_outputs,
    injection_payloads,
)
from vigia_platform.ledger.free_text import FreeTextRejected
from vigia_platform.loop.assistant.consumer import (
    DRAFT_FIELD,
    QUOTED_FACT_FIELD,
    Accepted,
    Rejected,
    RejectionReason,
    assess_output,
)
from vigia_platform.loop.assistant.context import BuiltContext, build_context
from vigia_platform.loop.assistant.schema import parse_output

POLICY: Final = both_policy()
ORGANIZATION: Final = uuid.UUID("00000000-0000-4000-8000-000000000316")
INNOCENT: Final = "Revisión del resguardo de la prensa durante el turno de mantenimiento."


def _built(generated: GeneratedContext) -> BuiltContext:
    return build_context(
        generated.zone_context,
        request_id=generated.request_id,
        anchor_record_id=generated.anchor_record_id,
    )


def _policy_accepts(text: str, field: object) -> bool:
    try:
        POLICY.apply(text, field, organization_id=ORGANIZATION)  # type: ignore[arg-type]
    except FreeTextRejected:
        return False
    return True


@given(data=st.data(), generated=assistant_contexts())
def test_pr_laz_20_an_output_is_shown_only_if_it_passes_every_check(
    data: st.DataObject, generated: GeneratedContext
) -> None:
    built = _built(generated)
    output = data.draw(assistant_outputs(built))
    verdict = assess_output(output.content, built, POLICY, ORGANIZATION)
    parsed = parse_output(output.content)
    if parsed is None:
        assert verdict == Rejected(RejectionReason.SCHEMA_INVALID)
        return
    expected = (
        (bool(parsed.citations) or parsed.insufficient_evidence_declared)
        and all(
            citation.id in built.aliases and built.aliases[citation.id].kind is citation.kind
            for citation in parsed.citations
        )
        and _policy_accepts(parsed.draft_text, DRAFT_FIELD)
        and all(_policy_accepts(c.quoted_fact_es, QUOTED_FACT_FIELD) for c in parsed.citations)
    )
    assert isinstance(verdict, Accepted) is expected, (verdict, output.content)
    if isinstance(verdict, Accepted):
        assert verdict.draft_text == POLICY.apply(
            parsed.draft_text, DRAFT_FIELD, organization_id=ORGANIZATION
        )
        assert [c["id"] for c in verdict.citations] == [c.id for c in parsed.citations]
        assert all(c["id"] in built.aliases for c in verdict.citations)


@given(data=st.data(), payload=injection_payloads())
def test_pr_laz_20_an_instruction_inside_the_standard_never_reaches_a_draft(
    data: st.DataObject, payload: str
) -> None:
    generated = data.draw(assistant_contexts(standard_text=st.just(payload)))
    built = _built(generated)
    assert built.context.standard.declared_text == payload  # sale como dato, en su campo
    first = built.context.citable[0]
    obeying = [
        {"draft_text": payload, "citations": [], "insufficient_evidence_declared": True},
        {
            "draft_text": INNOCENT,
            "citations": [{"kind": first.kind.value, "id": first.alias, "quoted_fact_es": payload}],
            "insufficient_evidence_declared": False,
        },
    ]
    for document in obeying:
        content = json.dumps({**document, "proposed_cause_category": "process"})
        verdict = assess_output(content, built, POLICY, ORGANIZATION)
        assert isinstance(verdict, Rejected), (payload, verdict)


@given(generated=assistant_contexts(), alias_hex=st.binary(min_size=4, max_size=4))
def test_a_draft_without_evidence_or_with_an_invented_alias_is_discarded(
    generated: GeneratedContext, alias_hex: bytes
) -> None:
    built = _built(generated)
    bare = json.dumps(
        {
            "draft_text": INNOCENT,
            "citations": [],
            "insufficient_evidence_declared": False,
            "proposed_cause_category": "engineering",
        }
    )
    assert assess_output(bare, built, POLICY, ORGANIZATION) == Rejected(RejectionReason.NO_EVIDENCE)
    invented = f"F-{alias_hex.hex()}"
    if invented in built.aliases:
        return
    cited = json.dumps(
        {
            "draft_text": INNOCENT,
            "citations": [{"kind": "record", "id": invented, "quoted_fact_es": "Hecho"}],
            "insufficient_evidence_declared": True,
            "proposed_cause_category": "engineering",
        }
    )
    assert assess_output(cited, built, POLICY, ORGANIZATION) == Rejected(
        RejectionReason.CITATION_OUTSIDE_LIST
    )
    first = built.context.citable[0]
    honest = json.dumps(
        {
            "draft_text": INNOCENT,
            "citations": [{"kind": first.kind.value, "id": first.alias, "quoted_fact_es": "Hecho"}],
            "insufficient_evidence_declared": False,
            "proposed_cause_category": "engineering",
        }
    )
    assert isinstance(assess_output(honest, built, POLICY, ORGANIZATION), Accepted)
