"""L-8: SEG-03, RLS y not_found uniforme aunque se conozca el identificador.

Mutación: desactivar RLS de ``loop.finding_state``; la consulta sin filtro de aplicación
debe ver solo su organización. La alteración de prueba se restaura antes de publicar.
"""

import uuid

import pytest
from sqlalchemy import text

from tests.loop_classification_support import ClassificationStack

pytestmark = pytest.mark.integration


def test_laz_l08_rls_hides_foreign_rows_and_the_route_matches_missing(
    classification: ClassificationStack,
) -> None:
    stack = classification
    own, foreign = stack.received(stack.world.a), stack.received(stack.world.b)
    who = stack.member(stack.world.a)
    context = stack.context(who)

    async def visible() -> set[uuid.UUID]:
        async with stack.database.transaction(context) as transaction:
            rows = await transaction.execute(text("SELECT organization_id FROM loop.finding_state"))
            return {row.organization_id for row in rows}

    assert stack.run(visible()) == {own.organization.organization_id}
    responses = [
        stack.send(
            who,
            identifier,
            {
                "outcome": "confirmed",
                "reason_category": "real_coexistence",
                "reason_text": "Revisión del resguardo de la prensa",
            },
        )
        for identifier in (stack.finding_state_id(foreign), uuid.UUID(int=808))
    ]
    assert [response.status_code for response in responses] == [404, 404]
    bodies = [
        {key: value for key, value in response.json().items() if key != "correlation_id"}
        for response in responses
    ]
    assert bodies[0] == bodies[1]
    assert bodies[0]["code"] == "not_found"
    assert stack.classifications(foreign) == []
