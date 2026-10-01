"""PR-NUC-15, PR-NUC-16 (escritura) y el orden fijo de verificación del escritor (TASK-113).

Contra PostgreSQL 16 real, como ``vigia_app``, con el escritor completo:

- **PR-NUC-15**: para contenido generado con clave (``order_probe`` y ``Finding`` del kit de U-01),
  ``write(x); write(x)`` devuelve el mismo recibo con ``accepted_duplicate`` y no crea nada;
  ``write(x')`` con la misma clave y otro contenido responde ``idempotency_conflict`` y no crea
  nada. Lo mismo con las dos escrituras **a la vez**: una gana y la otra se resuelve como
  duplicado o conflicto tras chocar con la unicidad de la clave.
- **PR-NUC-16 (escritura)**: todo contenido válido generado se acepta y el registro guarda la
  instantánea del actor (``display_name_snapshot``, ``role_in_use``, ``concession_id``, unidad) y
  el ``correlation_id`` del contexto; toda mutación (campo que falta, propiedad de más, tipo
  cambiado, texto sobre el límite, campo prohibido de identidad) se rechaza con
  ``content_invalid`` y el puntero del campo; un tipo no registrado, con ``record_type_unknown``.
- **Orden fijo**: un registro que falla varias verificaciones a la vez devuelve el código de la
  primera en el orden contexto → tipo y unidad → esquema → texto libre → idempotencia →
  evidencias, y no deja nada escrito.

Semillas y ejemplos del perfil activo (``tests/conftest.py``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import unicodedata
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from vigia_contracts.conformance.generators import finding, zone_catalog
from vigia_contracts.models.enumerations import AcceptanceStatus, RejectionCode

from tests.identity_db import migrated_database
from tests.integration.conftest import PostgresEndpoint
from tests.writer_support import (
    FINDING_TYPE,
    ORDER_TYPE,
    ZONE_TYPE,
    Place,
    WriterEnvironment,
    clips_of,
    fetch_record,
    localize_finding,
    order_document,
    organization_counts,
    real_concession,
    unit_context,
    writer_environment,
    zone_document,
)
from vigia_platform.ledger.application.writer import (
    LedgerRejection,
    LedgerRejectionCode,
    Receipt,
    to_contract_rejection,
)
from vigia_platform.shared.context import ActorKind, ActorUnit, Role

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def environment(postgres_endpoint: PostgresEndpoint) -> Iterator[WriterEnvironment]:
    with (
        migrated_database(postgres_endpoint, "writer_idempotency") as migrated,
        writer_environment(migrated) as environment,
    ):
        yield environment


def _write(
    environment: WriterEnvironment, context: Any, record_type: str, document: Any, **kwargs: Any
) -> Receipt | LedgerRejection:
    return environment.loop.run(environment.writer.write(context, record_type, document, **kwargs))


def _stock(environment: WriterEnvironment, document: dict[str, Any]) -> None:
    for clip in clips_of(document):
        environment.storage.put(clip)


def _accepted(result: Receipt | LedgerRejection) -> Receipt:
    assert isinstance(result, Receipt), result
    return result


def _rejected(result: Receipt | LedgerRejection) -> LedgerRejection:
    assert isinstance(result, LedgerRejection), result
    return result


# --- PR-NUC-15 ------------------------------------------------------------------------------


@st.composite
def keyed_documents(draw: st.DrawFn, place: Place) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """``(tipo, x, x')``: dos contenidos válidos con la misma clave y distinto contenido."""
    if draw(st.booleans()):
        level = draw(st.integers(0, 999))
        document = order_document(place, level=level, clips=draw(st.integers(0, 2)))
        document["note"] = draw(st.sampled_from(["Guarda abierta", "Revisión", "Á é í ó ú ñ"]))
        other = json.loads(json.dumps(document))
        other["level"] = level + 1
        return ORDER_TYPE, document, other
    catalog = draw(zone_catalog())
    document = localize_finding(draw(finding(catalog)), place)
    other = json.loads(json.dumps(document))
    other["family"] = next(
        f
        for f in ("dwell", "coexistence", "startup_transition", "guard_bypass")
        if f != document["family"]
    )
    other.pop("secondary_families", None)
    return FINDING_TYPE, document, other


@given(data=st.data(), concurrent=st.booleans())
def test_same_key_is_a_duplicate_or_a_conflict_and_never_a_new_record(
    environment: WriterEnvironment, data: st.DataObject, concurrent: bool
) -> None:
    place = Place.new()
    record_type, x, x_prime = data.draw(keyed_documents(place))
    _stock(environment, x)
    _stock(environment, x_prime)
    context = unit_context(place.organization_id, ActorUnit.U03, kind=ActorKind.NODE)

    if concurrent:
        # Las dos pasan el paso 5 a la vez; la perdedora choca con la clave en el disparador.
        async def both() -> list[Receipt | LedgerRejection]:
            return list(
                await asyncio.gather(
                    environment.writer.write(context, record_type, x),
                    environment.writer.write(context, record_type, x),
                )
            )

        results = environment.loop.run(both())
        receipts = [_accepted(r) for r in results]
        assert sorted(r.status for r in receipts) == [
            AcceptanceStatus.ACCEPTED,
            AcceptanceStatus.ACCEPTED_DUPLICATE,
        ]
        first = next(r for r in receipts if r.status is AcceptanceStatus.ACCEPTED)
        assert {(r.record_id, r.received_at) for r in receipts} == {
            (first.record_id, first.received_at)
        }
    else:
        first = _accepted(_write(environment, context, record_type, x))
        assert first.status is AcceptanceStatus.ACCEPTED
    before = environment.loop.run(organization_counts(environment.migrated, place.organization_id))

    again = _accepted(_write(environment, context, record_type, x))
    assert again.status is AcceptanceStatus.ACCEPTED_DUPLICATE
    assert (again.record_id, again.received_at) == (first.record_id, first.received_at)

    conflict = _rejected(_write(environment, context, record_type, x_prime))
    assert conflict.code is LedgerRejectionCode.IDEMPOTENCY_CONFLICT
    assert to_contract_rejection(conflict).code is RejectionCode.IDEMPOTENCY_CONFLICT

    after = environment.loop.run(organization_counts(environment.migrated, place.organization_id))
    assert after == before
    assert after["ledger.ledger_record"] == 1


_LETTERS = st.characters(min_codepoint=0x41, max_codepoint=0x24F, categories=["L"])
_ACCENTED = st.sampled_from("áéíóúÁÉÍÓÚñÑüÜçÇàèâêôãõ")


@given(
    parts=st.tuples(
        st.text(_LETTERS, max_size=30), _ACCENTED, st.text(_LETTERS | st.just(" "), max_size=30)
    )
)
def test_free_text_in_nfd_is_hashed_and_stored_in_nfc(
    environment: WriterEnvironment, parts: tuple[str, str, str]
) -> None:
    """VIG-129 (mutación M6): el texto libre se normaliza **antes** de canonicalizar y hashear.

    Un texto en NFD se guarda en NFC, el ``content_hash`` es el de lo guardado y la misma
    escritura en NFC con la misma clave es un duplicado del mismo registro, no un conflicto.
    """
    composed = unicodedata.normalize("NFC", "".join(parts).strip() or parts[1])
    decomposed = unicodedata.normalize("NFD", composed)
    assert decomposed != composed
    place = Place.new()
    x = order_document(place, clips=0)
    x["note"] = decomposed
    x_nfc = dict(x, note=composed)
    context = unit_context(place.organization_id, ActorUnit.U03, kind=ActorKind.NODE)

    first = _accepted(_write(environment, context, ORDER_TYPE, x))
    assert first.status is AcceptanceStatus.ACCEPTED
    row = environment.loop.run(fetch_record(environment.migrated, first.record_id))
    stored = json.loads(bytes(row["content"]))
    assert stored["note"] == composed
    assert hashlib.sha256(bytes(row["content"])).hexdigest() == row["content_hash"]

    again = _accepted(_write(environment, context, ORDER_TYPE, x_nfc))
    assert again.status is AcceptanceStatus.ACCEPTED_DUPLICATE
    assert again.record_id == first.record_id


def test_concurrent_writes_of_different_content_with_one_key_leave_one_record(
    environment: WriterEnvironment,
) -> None:
    place = Place.new()
    x = order_document(place, clips=0)
    x_prime = dict(x, level=x["level"] + 1)
    context = unit_context(place.organization_id, ActorUnit.U03, kind=ActorKind.NODE)

    async def both() -> list[Receipt | LedgerRejection]:
        return list(
            await asyncio.gather(
                environment.writer.write(context, ORDER_TYPE, x),
                environment.writer.write(context, ORDER_TYPE, x_prime),
            )
        )

    results = environment.loop.run(both())
    assert sum(isinstance(r, Receipt) for r in results) == 1
    loser = next(r for r in results if isinstance(r, LedgerRejection))
    assert loser.code is LedgerRejectionCode.IDEMPOTENCY_CONFLICT
    counts = environment.loop.run(organization_counts(environment.migrated, place.organization_id))
    assert counts["ledger.ledger_record"] == 1
    assert counts["ledger.evidence"] == 0


# --- PR-NUC-16 (escritura) -------------------------------------------------------------------


_ACTORS = st.sampled_from(
    [
        (ActorKind.USER, Role.COORDINATOR_SST),
        (ActorKind.USER, Role.PLANT_MANAGER),
        (ActorKind.PROVIDER_USER, Role.PROVIDER_INSTALLER),
        (ActorKind.SYSTEM, None),
        (ActorKind.OPERATOR, None),
    ]
)


@given(
    actor=_ACTORS,
    name=st.text(
        st.characters(min_codepoint=0x20, max_codepoint=0x24F, categories=["L", "N", "Zs", "M"]),
        min_size=1,
        max_size=120,
    ),
    display=st.sampled_from(["Coordinación SST", "Gerencia de planta Ñandú", "Operación 😀"]),
)
def test_valid_content_is_accepted_with_the_actor_snapshot(
    environment: WriterEnvironment, actor: tuple[ActorKind, Role | None], name: str, display: str
) -> None:
    kind, role = actor
    place = Place.new()
    # El actor del proveedor escribe bajo una concesión real: sin ella no ve la zona (nuc_0009).
    concession = real_concession(place) if kind is ActorKind.PROVIDER_USER else None
    context = unit_context(
        place.organization_id,
        ActorUnit.U02,
        kind=kind,
        role=role,
        display_name=display,
        concession_id=concession,
    )
    receipt = _accepted(_write(environment, context, ZONE_TYPE, zone_document(place, name)))
    row = environment.loop.run(fetch_record(environment.migrated, receipt.record_id))
    assert row["received_at"] == receipt.received_at
    assert row["actor_kind"] == kind.value
    assert row["actor_id"] == context.actor.id
    assert row["actor_display_name_snapshot"] == display
    assert row["actor_role_in_use"] == (None if context.actor.role_in_use is None else role)
    assert row["actor_concession_id"] == context.concession_id
    assert row["actor_unit"] == "U-02"
    assert row["correlation_id"] == context.correlation_id
    assert (row["scope_plant_id"], row["plant_id"]) == (place.plant_id, place.plant_id)
    assert row["schema_version"] == 1
    stored = json.loads(bytes(row["content"]))
    # Lo que se guarda es la forma NFC del texto libre (política base, BR-NUC-44).
    assert stored["name"] == unicodedata.normalize("NFC", name)
    assert hashlib.sha256(bytes(row["content"])).hexdigest() == row["content_hash"]


_MUTATIONS = (
    ("missing_field", "/code"),
    ("extra_property", "/unexpected"),
    ("type_changed", "/code"),
    ("text_over_limit", "/name"),
    ("identity_field", "/worker_name"),
    ("empty_text", "/name"),
    ("uuid_invalid", "/zone_id"),
)


@given(mutation=st.sampled_from(_MUTATIONS), value=st.integers(-(2**70), 2**70))
def test_mutated_content_is_rejected_with_the_first_failing_field(
    environment: WriterEnvironment, mutation: tuple[str, str], value: int
) -> None:
    kind, pointer = mutation
    place = Place.new()
    document = zone_document(place)
    if kind == "missing_field":
        del document["code"]
    elif kind == "extra_property":
        document["unexpected"] = value
    elif kind == "type_changed":
        document["code"] = value
    elif kind == "text_over_limit":
        document["name"] = "n" * 121
    elif kind == "identity_field":
        document["worker_name"] = "Persona sintética"
    elif kind == "empty_text":
        document["name"] = ""
    else:
        document["zone_id"] = "no-es-un-uuid"
    context = unit_context(place.organization_id, ActorUnit.U02)
    rejection = _rejected(_write(environment, context, ZONE_TYPE, document))
    assert rejection.code is LedgerRejectionCode.CONTENT_INVALID
    assert rejection.field == pointer
    assert "Persona" not in rejection.message_es
    counts = environment.loop.run(organization_counts(environment.migrated, place.organization_id))
    assert counts["ledger.ledger_record"] == 0


@pytest.mark.parametrize(
    "document",
    [
        {"n": float("nan")},
        {"n": 2**80},
        {"deep": json.loads("[" * 900 + "]" * 900)},
        {"big": "x" * (300 * 1024)},
        ["no es un objeto"],
        {"key": object()},
    ],
    ids=["nan", "huge_int", "deep", "over_256kb", "not_object", "not_json"],
)
def test_hostile_content_ends_in_content_invalid(
    environment: WriterEnvironment, document: Any
) -> None:
    place = Place.new()
    context = unit_context(place.organization_id, ActorUnit.U02)
    rejection = _rejected(_write(environment, context, ZONE_TYPE, document))
    assert rejection.code is LedgerRejectionCode.CONTENT_INVALID
    if isinstance(document, dict) and "big" in document:
        # El tope de 256 KB se aplica antes de validar el esquema: sin puntero de campo.
        assert rejection.field is None


@pytest.mark.parametrize(
    ("record_type", "unit"),
    [("no_such_type", ActorUnit.U02), (ORDER_TYPE, ActorUnit.U02), (ZONE_TYPE, ActorUnit.U04)],
    ids=["unregistered", "u03_type_written_by_u02", "u02_type_written_by_u04"],
)
def test_unknown_type_or_foreign_unit_is_record_type_unknown(
    environment: WriterEnvironment, record_type: str, unit: ActorUnit
) -> None:
    place = Place.new()
    context = unit_context(place.organization_id, unit)
    rejection = _rejected(_write(environment, context, record_type, zone_document(place)))
    assert rejection.code is LedgerRejectionCode.RECORD_TYPE_UNKNOWN


def test_write_without_context_is_context_absent(environment: WriterEnvironment) -> None:
    before = environment.database.probe.opened
    rejection = _rejected(_write(environment, None, ZONE_TYPE, zone_document(Place.new())))
    assert rejection.code is LedgerRejectionCode.CONTEXT_ABSENT
    assert environment.database.probe.opened == before


def test_scope_argument_and_content_must_agree(environment: WriterEnvironment) -> None:
    from vigia_platform.ledger.application.writer import RecordScope

    place = Place.new()
    context = unit_context(place.organization_id, ActorUnit.U02)
    rejection = _rejected(
        _write(
            environment,
            context,
            ZONE_TYPE,
            zone_document(place),
            scope=RecordScope(plant_id=uuid.uuid4()),
        )
    )
    assert (rejection.code, rejection.field) == (LedgerRejectionCode.CONTENT_INVALID, "/plant_id")
    accepted = _accepted(
        _write(
            environment,
            context,
            ZONE_TYPE,
            zone_document(place),
            scope=RecordScope(plant_id=place.plant_id, node_id=place.node_id),
        )
    )
    row = environment.loop.run(fetch_record(environment.migrated, accepted.record_id))
    # Lo que el contenido no lleva (el nodo) sale del argumento; lo demás, del contenido.
    assert (row["scope_plant_id"], row["scope_node_id"]) == (place.plant_id, place.node_id)


# --- Orden fijo -------------------------------------------------------------------------------

_ORDER = (
    LedgerRejectionCode.CONTEXT_ABSENT,  # (1) organización del contenido
    LedgerRejectionCode.RECORD_TYPE_UNKNOWN,  # (2) unidad no autorizada
    LedgerRejectionCode.CONTENT_INVALID,  # (3) esquema
    LedgerRejectionCode.FREE_TEXT_REJECTED,  # (4) texto libre
    LedgerRejectionCode.IDEMPOTENCY_CONFLICT,  # (5) misma clave, otro contenido
    LedgerRejectionCode.EVIDENCE_MISSING,  # (6) evidencias
)
_EVIDENCE_FAULTS = {
    "missing": (LedgerRejectionCode.EVIDENCE_MISSING, "/clips/0"),
    "hash": (LedgerRejectionCode.EVIDENCE_HASH_MISMATCH, "/clips/0"),
    "marker": (LedgerRejectionCode.EVIDENCE_NOT_ANONYMIZED, "/clips/0"),
}


@given(
    failures=st.sets(st.sampled_from(_ORDER), min_size=1),
    evidence_fault=st.sampled_from(sorted(_EVIDENCE_FAULTS)),
)
def test_several_failures_return_the_first_in_the_fixed_order(
    environment: WriterEnvironment,
    failures: set[LedgerRejectionCode],
    evidence_fault: str,
) -> None:
    place = Place.new()
    writer_unit = ActorUnit.U03
    document = order_document(place, clips=1)
    clip = document["clips"][0]
    if LedgerRejectionCode.IDEMPOTENCY_CONFLICT in failures:
        # Un registro previo, válido, con la misma clave y otro contenido.
        previous = json.loads(json.dumps(document))
        previous["level"] = document["level"] + 1
        previous["clips"] = []
        base = unit_context(place.organization_id, writer_unit, kind=ActorKind.NODE)
        _accepted(_write(environment, base, ORDER_TYPE, previous))
    if LedgerRejectionCode.EVIDENCE_MISSING in failures:
        if evidence_fault == "hash":
            environment.storage.put(clip, sha256="ab" * 32)
        elif evidence_fault == "marker":
            environment.storage.put(clip, marker=None)
    else:
        environment.storage.put(clip)
    if LedgerRejectionCode.CONTEXT_ABSENT in failures:
        document["organization_id"] = str(uuid.uuid4())
    unit = ActorUnit.U02 if LedgerRejectionCode.RECORD_TYPE_UNKNOWN in failures else writer_unit
    if LedgerRejectionCode.CONTENT_INVALID in failures:
        document["level"] = "alto"
    if LedgerRejectionCode.FREE_TEXT_REJECTED in failures:
        document["note"] = "guarda <b>norte</b>"
    context = unit_context(place.organization_id, unit, kind=ActorKind.NODE)
    before = environment.loop.run(organization_counts(environment.migrated, place.organization_id))
    calls = environment.storage.calls

    rejection = _rejected(_write(environment, context, ORDER_TYPE, document))

    first = next(code for code in _ORDER if code in failures)
    if first is LedgerRejectionCode.EVIDENCE_MISSING:
        assert (rejection.code, rejection.field) == _EVIDENCE_FAULTS[evidence_fault]
    else:
        assert rejection.code is first
    expected_field = {
        LedgerRejectionCode.CONTEXT_ABSENT: "/organization_id",
        LedgerRejectionCode.CONTENT_INVALID: "/level",
        LedgerRejectionCode.FREE_TEXT_REJECTED: "/note",
    }.get(first)
    if expected_field is not None:
        assert rejection.field == expected_field
    # Ninguna verificación posterior a la que falló llegó al almacén.
    if first is not LedgerRejectionCode.EVIDENCE_MISSING:
        assert environment.storage.calls == calls
    after = environment.loop.run(organization_counts(environment.migrated, place.organization_id))
    assert after == before
