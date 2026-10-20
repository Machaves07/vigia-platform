"""La versión de la lista de vocabulario es monótona y el mínimo no se retira (TASK-350; LC-LAZ-17;
BR-LAZ-82, 85 y 86; PAT-LAZ-REN-02).

- **Propiedad** (PostgreSQL 16 como ``vigia_app``, ``VocabularyService`` real): para toda secuencia
  generada de cambios (añadir un término del cliente, retirar uno suyo, intentar retirar uno de la
  lista inicial, añadir uno ya vigente), cada cambio aceptado da **exactamente** la versión
  siguiente y un registro con ella; cada rechazo no cambia nada; un término ``derived`` nunca se
  retira; y al final la lista es la del modelo (las 50 iniciales más las del cliente vigentes).
- **Concurrencia**: varios cambios simultáneos de la misma organización dan versiones distintas y
  consecutivas, un registro por versión y ningún término perdido (el candado de la organización);
  sin el candado, el ``UPDATE`` condicional sigue impidiendo dos versiones iguales.
- **Vocabulario declarado en la caché** (el doble de ``DeclaredNamesCache``): un nombre declarado
  deja de caer como nombre propio en la siguiente escritura de la organización porque la versión
  de la lista cambió, sin esperar a la caducidad de 60 s; retirarlo vuelve a rechazarlo; otra
  organización no lo ve.

Reloj simulado; solo datos generados.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.integration.conftest import PostgresEndpoint
from tests.loop_vocabulary_support import REASON, VocabularyStack, loop_vocabulary_stack
from tests.vocabulary_support import MemoryStore
from vigia_platform.ledger.free_text import (
    FreeTextCandidate,
    FreeTextPreparation,
    RowReader,
    canonical_form,
)
from vigia_platform.loop.validators.attribution import (
    NO_DECLARED_NAMES,
    PersonAttributionValidator,
)
from vigia_platform.loop.validators.cache import TTL_SECONDS, DeclaredNamesCache
from vigia_platform.loop.validators.initial_list import INITIAL_LIST_VERSION, INITIAL_TERMS
from vigia_platform.loop.validators.vocabulary import VocabularyList
from vigia_platform.loop.vocabulary.repository import PostgresVocabularyRepository
from vigia_platform.loop.vocabulary.service import (
    TermRequest,
    VocabularyRejected,
    VocabularyRequestInvalid,
    VocabularyUnavailable,
)
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.db import Transaction

NOW: Final = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
POOL: Final = ("pasillo", "baliza", "bocina", "cinta", "puerta", "umbral", "montacargas", "rampa")
"""Lemas del cliente: ninguno está en la lista inicial."""


def _request(value: str) -> TermRequest:
    return TermRequest("absence_claim", "lemma", value, "Describa lo observado en la zona")


# --- propiedad sobre secuencias de cambios --------------------------------------------------------


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[VocabularyStack]:
    with loop_vocabulary_stack(postgres_endpoint, "laz_vocabulary_versions") as built:
        yield built


OPERATIONS = st.lists(
    st.one_of(
        st.tuples(st.just("add"), st.sampled_from(POOL)),
        st.tuples(st.just("remove_own"), st.integers(0, 7)),
        st.tuples(st.just("remove_derived"), st.integers(0, len(INITIAL_TERMS) - 1)),
        st.tuples(st.just("add_initial"), st.integers(0, len(INITIAL_TERMS) - 1)),
    ),
    min_size=1,
    max_size=6,
)


@dataclass
class _Model:
    version: int = INITIAL_LIST_VERSION
    terms: dict[str, uuid.UUID] = field(default_factory=dict)
    """Valor → ``term_id`` de los términos vigentes del cliente."""


@pytest.mark.integration
@given(operations=OPERATIONS)
def test_versions_are_consecutive_and_derived_terms_are_never_removed(
    stack: VocabularyStack, operations: list[tuple[str, Any]]
) -> None:
    site = stack.site()
    context = stack.context(stack.member(site))
    organization = site.organization_id
    model = _Model()
    for kind, argument in operations:
        stack.tick()
        if kind == "add":
            change: dict[str, Any] = {"added": (_request(argument),)}
            expected_ok = argument not in model.terms
        elif kind == "remove_own":
            owned = sorted(model.terms)
            target = model.terms[owned[argument % len(owned)]] if owned else uuid.uuid4()
            change = {"removed": (target,)}
            expected_ok = bool(owned)
        elif kind == "remove_derived":
            change = {"removed": (INITIAL_TERMS[argument].term_id,)}
            expected_ok = False
        else:
            initial = INITIAL_TERMS[argument]
            change = {
                "added": (
                    TermRequest(
                        initial.category.value,
                        initial.form.value,
                        initial.value,
                        "Describa lo observado en la zona",
                    ),
                )
            }
            expected_ok = False
        try:
            result = stack.run(stack.service.change(context, reason=REASON, **change))
        except (VocabularyRejected, VocabularyRequestInvalid):
            assert not expected_ok, (kind, argument)
            continue
        assert expected_ok, (kind, argument)
        assert result.list_version == model.version + 1
        model.version = result.list_version
        for added in result.added:
            model.terms[added.value] = added.term_id
        for removed in result.removed_term_ids:
            model.terms = {k: v for k, v in model.terms.items() if v != removed}
    records = stack.records(organization)
    assert [record["content"]["list_version"] for record in records] == list(
        range(INITIAL_LIST_VERSION + 1, model.version + 1)
    )
    row = stack.row(organization)
    assert (row is None) == (model.version == INITIAL_LIST_VERSION)
    if row is not None:
        assert row["version"] == model.version
    view = stack.run(stack.service.current(context))
    assert view.list_version == model.version
    derived = [term for term in view.terms if term.origin.value == "derived"]
    assert [term.term_id for term in derived] == [term.term_id for term in INITIAL_TERMS]
    customer = {term.value: term.term_id for term in view.terms if term.origin.value != "derived"}
    assert customer == model.terms
    # Ningún término de la lista inicial está nunca en la base (ni vigente ni retirado).
    initial_ids = {term.term_id for term in INITIAL_TERMS}
    assert not {row["term_id"] for row in stack.terms(organization)} & initial_ids


# --- concurrencia ---------------------------------------------------------------------------------


async def _together(stack: VocabularyStack, service: Any, context: Any, values: list[str]) -> Any:
    return await asyncio.gather(
        *(service.change(context, added=(_request(value),), reason=REASON) for value in values),
        return_exceptions=True,
    )


@pytest.mark.integration
@pytest.mark.parametrize("attempt", range(3))
@pytest.mark.parametrize("with_row", [False, True], ids=["primer-cambio", "con-fila"])
def test_simultaneous_changes_get_distinct_consecutive_versions_and_lose_nothing(
    stack: VocabularyStack, attempt: int, with_row: bool
) -> None:
    site = stack.site()
    context = stack.context(stack.member(site))
    organization = site.organization_id
    start = INITIAL_LIST_VERSION
    if with_row:
        stack.run(stack.service.change(context, added=(_request("rampa"),), reason=REASON))
        start += 1
    values = ["pasillo", "baliza", "bocina", "cinta"]
    results = stack.run(_together(stack, stack.service, context, values))
    assert [type(result).__name__ for result in results] == ["ChangeResult"] * 4, results
    versions = sorted(result.list_version for result in results)
    assert versions == list(range(start + 1, start + 5))
    assert stack.row(organization)["version"] == start + 4
    records = stack.records(organization)
    assert sorted(record["content"]["list_version"] for record in records) == list(
        range(INITIAL_LIST_VERSION + 1, start + 5)
    )
    current = {
        row["value"] for row in stack.terms(organization) if row["removed_in_version"] is None
    }
    assert set(values) <= current


class _NoLock(PostgresVocabularyRepository):
    """El repositorio sin el candado de la organización (la mutación «comprobar y luego
    escribir»): solo queda el respaldo, el ``UPDATE`` condicional a la versión leída."""

    async def lock(self, transaction: Transaction) -> None:
        return None


@pytest.mark.integration
@pytest.mark.parametrize("attempt", range(3))
def test_without_the_lock_the_conditional_update_still_forbids_two_equal_versions(
    stack: VocabularyStack, attempt: int
) -> None:
    site = stack.site()
    context = stack.context(stack.member(site))
    organization = site.organization_id
    stack.run(stack.service.change(context, added=(_request("rampa"),), reason=REASON))
    unlocked = stack.service_like(repository=_NoLock(stack.database))
    values = ["pasillo", "baliza", "bocina", "cinta"]
    results = stack.run(_together(stack, unlocked, context, values))
    accepted = [result for result in results if type(result).__name__ == "ChangeResult"]
    refused = [result for result in results if type(result).__name__ != "ChangeResult"]
    assert all(isinstance(result, VocabularyUnavailable) for result in refused), refused
    versions = [result.list_version for result in accepted]
    assert len(set(versions)) == len(versions) >= 1
    assert stack.row(organization)["version"] == 2 + len(accepted)
    records = stack.records(organization)
    assert len(records) == 1 + len(accepted)
    assert len({record["content"]["list_version"] for record in records}) == len(records)


# --- vocabulario declarado en la caché (doble) ----------------------------------------------------


@dataclass
class _Names:
    """``DeclaredNamesStore`` en memoria que cuenta sus lecturas."""

    by_organization: dict[uuid.UUID, tuple[str, ...]] = field(default_factory=dict)
    reads: int = 0

    async def names(self, organization_id: uuid.UUID, rows: RowReader) -> tuple[str, ...]:
        self.reads += 1
        return self.by_organization.get(organization_id, ())


async def _no_rows(*_: object) -> list[object]:
    raise AssertionError("los dobles no leen filas")


def _candidate(text: str, organization: uuid.UUID) -> FreeTextCandidate:
    return FreeTextCandidate(text, canonical_form(text), organization)


def test_a_declared_name_is_exempt_in_the_next_write_after_a_version_change() -> None:
    organization, other = uuid.uuid4(), uuid.uuid4()
    names = _Names({organization: ("Planta Uno",), other: ()})
    versions = MemoryStore({organization: VocabularyList(version=2, terms=())})
    cache = DeclaredNamesCache(names, versions=versions)
    validator = PersonAttributionValidator(cache.names_for)
    clock = SimulatedClock(NOW)
    start = clock.monotonic()
    text = "Revisión de la Prensa Schuler Kompakt en la zona"

    def write(organization_id: uuid.UUID) -> bool:
        """Una escritura: el preparador y después el validador; ``True`` si acepta."""
        asyncio.run(cache.refresh(FreeTextPreparation(organization_id, _no_rows, clock)))
        return validator.verdict(_candidate(text, organization_id)) is None

    assert cache.names_for(organization) is NO_DECLARED_NAMES
    assert write(organization) is False  # sin declarar: parece el nombre de una persona
    assert names.reads == 1
    # La organización declara «Prensa Schuler Kompakt»: versión 3 de la lista.
    clock.advance(1)  # muy por debajo de los 60 s de caducidad
    names.by_organization[organization] = ("Planta Uno", "Prensa Schuler Kompakt")
    versions.lists[organization] = VocabularyList(version=3, terms=())
    assert write(organization) is True
    assert names.reads == 2
    # Sin cambio de versión ni caducidad, la siguiente escritura no relee.
    clock.advance(1)
    assert write(organization) is True and names.reads == 2
    # Otra organización no lo tiene declarado.
    assert write(other) is False
    # Retirarlo (versión 4) vuelve a rechazarlo en la siguiente escritura.
    clock.advance(1)
    names.by_organization[organization] = ("Planta Uno",)
    versions.lists[organization] = VocabularyList(version=4, terms=())
    assert write(organization) is False
    # Todo pasó dentro de la caducidad: lo que relee es el cambio de versión, no el reloj.
    assert clock.monotonic() - start < TTL_SECONDS


def test_an_older_version_never_replaces_a_newer_declared_names_entry() -> None:
    """Como la lista (PR-LAZ-53): una comprobación que leyó la versión 3 después de que otra
    dejara la 4 no la sustituye."""
    organization = uuid.uuid4()
    names = _Names({organization: ("Prensa Schuler Kompakt",)})
    versions = MemoryStore({organization: VocabularyList(version=4, terms=())})
    cache = DeclaredNamesCache(names, versions=versions)
    clock = SimulatedClock(NOW)
    asyncio.run(cache.refresh(FreeTextPreparation(organization, _no_rows, clock)))
    exempt = cache.names_for(organization)
    versions.lists[organization] = VocabularyList(version=3, terms=())  # lectura atrasada
    names.by_organization[organization] = ()
    asyncio.run(cache.refresh(FreeTextPreparation(organization, _no_rows, clock)))
    assert cache.names_for(organization) is exempt
