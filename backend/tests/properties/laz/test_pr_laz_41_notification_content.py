"""PR-LAZ-41: ninguna notificación generada contiene imagen, referencia a clip, dato de persona ni
enlace público; su título y su cuerpo pasan los dos validadores (TASK-324; BR-LAZ-128; P3).

Para **cada** evento del catálogo y cargas generadas en las que **todo** campo de texto lleva texto
hostil (nombres de persona, correos, URL, referencias de clip, marcado, documentos de identidad o
texto Unicode cualquiera, ``HOSTILE`` y ``st.text``):

- el aviso compuesto (``render`` sobre ``facts_for``) es **idéntico** al de la misma carga sin ese
  texto: ninguna cadena de la carga llega al título ni al cuerpo;
- título y cuerpo pasan la política base y los dos validadores reales de U-04 (``both_policy``),
  caben en 120 y 1 000 caracteres y no llevan ``://``, ``@``, ni palabras de imagen, clip, recorte
  o enlace (por palabras de la forma canónica, VIG-194);
- ``deep_link_ref`` es estructurado: uno de los siete tipos y un UUID, nunca una URL ni el texto
  hostil.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Final

from hypothesis import given
from hypothesis import strategies as st

from tests.attribution_support import both_policy
from tests.factories import uuid7
from tests.properties.laz.strategies.notifications import HOSTILE, hostile_payload
from tests.unit.test_notification_templates import FORBIDDEN_WORDS
from vigia_platform.ledger.free_text import canonical_form
from vigia_platform.notifications.catalog import CATALOG
from vigia_platform.notifications.engine import deep_link
from vigia_platform.notifications.templates import (
    BODY_FIELD,
    TITLE_FIELD,
    facts_for,
    render,
    validated,
)
from vigia_platform.shared.outbox.publish import OutboxEvent, partition_key

LINK_KINDS: Final = frozenset(
    {"finding", "queue_item", "export", "package", "zone", "node", "organization"}
)
_WORDS: Final = re.compile(r"[a-z0-9]+")
_POLICY: Final = both_policy()
ORGANIZATION: Final = uuid.UUID("00000000-0000-4000-8000-000000000041")

hostile_texts = st.sampled_from(HOSTILE) | st.text(min_size=1, max_size=200)


def _event(event_name: str, payload: dict[str, object]) -> OutboxEvent:
    return OutboxEvent(
        event_id=uuid7(),
        organization_id=ORGANIZATION,
        plant_id=None,
        event_name=event_name,
        partition_key=partition_key(ORGANIZATION, None),
        ledger_sequence=None,
        payload=payload,
        correlation_id=uuid7(),
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
    )


@given(
    event_name=st.sampled_from(sorted(CATALOG)),
    text=hostile_texts,
    ids=st.fixed_dictionaries(
        {name: st.uuids().map(str) for name in ("zone", "subject", "record", "node")}
    ),
)
def test_pr_laz_41_no_payload_text_reaches_the_notification(
    event_name: str, text: str, ids: dict[str, str]
) -> None:
    entry = CATALOG[event_name]
    hostile = hostile_payload(event_name, text, ids)
    clean = {key: value for key, value in hostile.items() if value != text}
    rendered = render(entry.template_key, facts_for(event_name, hostile))
    assert rendered == render(entry.template_key, facts_for(event_name, clean))
    checked = validated(_POLICY, rendered, ORGANIZATION)
    assert checked == rendered  # ya en NFC, sin nada que el validador rechace
    for value, field in ((rendered.title_es, TITLE_FIELD), (rendered.body_es, BODY_FIELD)):
        assert 1 <= len(value) <= field.max_length
        assert "://" not in value and "@" not in value
        assert not set(_WORDS.findall(canonical_form(value))) & FORBIDDEN_WORDS
        if text in HOSTILE:
            assert text not in value
    link = deep_link(_event(event_name, hostile))
    assert link.kind in LINK_KINDS
    assert str(uuid.UUID(link.as_json()["id"])) == link.as_json()["id"]
    assert set(link.as_json()) == {"kind", "id"}
