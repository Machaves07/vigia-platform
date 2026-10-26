"""SUP-U05-36 y 37: las rutas de la lista de vocabulario (TASK-350; LC-LAZ-17; BR-LAZ-82 a 86).

Sobre PostgreSQL 16 real como ``vigia_app``, la aplicación real y la política de texto libre de
U-04 con sus dos validadores (``loop_vocabulary_stack``):

- **Lista inicial**: sin cambios, la versión 1 con los términos de BLM §14 (``derived``) y la tabla
  de flexiones vigente; nada escrito.
- **Cambio**: versión siguiente, exactamente un ``vocabulary_list_changed`` (v2, cadena de la
  organización) y su evento (solo identificadores y recuentos); el lema se guarda plegado; la fila
  registra la tabla de flexiones; ``GET`` devuelve lo mismo.
- **El término no va como texto libre** (BR-LAZ-83): un término que es él mismo bloqueado (un
  valor de la lista inicial como patrón, una flexión de un lema inicial) se registra; el mismo
  término en ``/reason`` o en la alternativa se rechaza con ``loop_blocked_vocabulary_term``.
- **Errores** (mapa de VIG-201): sin motivo, ``loop_vocabulary_change_reason_required`` (400); ya
  vigente, ``loop_vocabulary_term_duplicate`` (409); retirar un ``derived`` o uno que no está,
  ``invalid_request``; nada escrito en ningún rechazo.
- **Retirar** no borra: la fila del término queda con ``removed_in_version`` y su registro de alta
  sigue en el expediente (BR-LAZ-85).
- **Vocabulario declarado**: un nombre declarado deja de caer como nombre propio en la siguiente
  escritura de la organización (y no en otra), y se registra en el mismo cambio.
- **Alcance**: otra organización no ve ni toca la lista; un rol sin ``vocabulary.manage`` y la
  concesión de proveedor responden ``not_found`` sin escribir. El repositorio y el almacén de
  nombres filtran por organización aun sin seguridad de fila (lectura como superusuario).

Solo datos generados.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Final

import asyncpg  # type: ignore[import-untyped]
import pytest
from sqlalchemy.engine import Row
from sqlalchemy.sql import Executable

from tests.authz_support import Site
from tests.integration.conftest import PostgresEndpoint
from tests.loop_vocabulary_support import (
    REASON,
    VocabularyStack,
    Who,
    loop_vocabulary_stack,
    term,
)
from tests.outbox_support import app_database
from tests.writer_support import unit_context
from vigia_platform.ledger.free_text import FreeTextPreparation
from vigia_platform.loop.adapters.postgres.declared_names import PostgresDeclaredNamesStore
from vigia_platform.loop.detail_codes import HTTP_STATUS_BY_DETAIL_CODE, LoopDetailCode
from vigia_platform.loop.validators import lexicon
from vigia_platform.loop.validators.initial_list import INITIAL_TERMS
from vigia_platform.loop.vocabulary.repository import PostgresVocabularyRepository
from vigia_platform.shared.context import ActorUnit, Role, ScopeContext, ScopeLevel

pytestmark = pytest.mark.integration

DERIVED: Final = INITIAL_TERMS[0]
SABOTAJE: Final = next(term for term in INITIAL_TERMS if term.value == "sabotaje")


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[VocabularyStack]:
    with loop_vocabulary_stack(postgres_endpoint, "loop_vocabulary_routes") as built:
        yield built


@pytest.fixture
def site(stack: VocabularyStack) -> Site:
    return stack.site()


@pytest.fixture
def admin(stack: VocabularyStack, site: Site) -> Who:
    return stack.member(site)


def _error(response: Any) -> tuple[int, str | None, str | None]:
    body = response.json()
    return response.status_code, body.get("code"), body.get("detail_code")


def _mapped(code: LoopDetailCode) -> tuple[int, str, str]:
    """El estado y el ``code`` del mapa único de U-04 (A-68, VIG-201) para ``code``."""
    api_code, status = HTTP_STATUS_BY_DETAIL_CODE[code]
    return status, api_code.value, code.value


def _nothing_written(stack: VocabularyStack, organization: uuid.UUID) -> None:
    assert stack.records(organization) == []
    assert stack.row(organization) is None
    assert stack.terms(organization) == []


# --- lista inicial -------------------------------------------------------------------------------


def test_without_changes_the_list_is_the_initial_one_in_version_1(
    stack: VocabularyStack, site: Site, admin: Who
) -> None:
    response = stack.get(admin)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["list_version"], body["record_id"], body["effective_from"]) == (1, None, None)
    assert body["lemmatizer_version"] == lexicon.LEMMATIZER_VERSION
    assert body["declared_names"] == []
    assert [item["term_id"] for item in body["terms"]] == [str(t.term_id) for t in INITIAL_TERMS]
    assert {item["origin"] for item in body["terms"]} == {"derived"}
    assert {item["added_in_version"] for item in body["terms"]} == {1}
    for item, initial in zip(body["terms"], INITIAL_TERMS, strict=True):
        assert (item["form"], item["value"], item["category"]) == (
            initial.form.value,
            initial.value,
            initial.category.value,
        )
        assert item["lemma"] == (initial.value if initial.form.value == "lemma" else None)
    _nothing_written(stack, site.organization_id)


# --- cambio --------------------------------------------------------------------------------------


def test_a_change_writes_one_record_one_event_and_the_row(
    stack: VocabularyStack, site: Site, admin: Who
) -> None:
    organization = site.organization_id
    response = stack.post(
        admin,
        {
            "added": [
                term("Pasíllos"),
                term(r"linea[\W_]*sur", form="pattern", category="reduction_promise"),
            ],
            "declared_names_added": [{"kind": "machine", "value": "Prensa Schuler Kompakt"}],
            "reason": REASON,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["list_version"] == 2
    lemma, pattern = body["added"]
    # El lema se guarda ya plegado, con la tabla de flexiones vigente (no es un plural: es el
    # valor escrito, plegado).
    assert (lemma["value"], lemma["lemma"], lemma["origin"]) == (
        "pasillos",
        "pasillos",
        "customer_added",
    )
    assert (pattern["value"], pattern["lemma"]) == (r"linea[\W_]*sur", None)
    (record,) = stack.records(organization)
    assert record["record_id"] == uuid.UUID(body["record_id"])
    assert (record["schema_version"], record["plant_id"], record["actor_unit"]) == (
        2,
        None,
        "U-04",
    )
    content = record["content"]
    assert (content["list_version"], content["reason"], content["removed"]) == (2, REASON, [])
    assert [item["value"] for item in content["added"]] == ["pasillos", r"linea[\W_]*sur"]
    assert content["declared_names_added"] == [
        {
            "name_id": body["declared_names_added"][0]["name_id"],
            "kind": "machine",
            "value": "Prensa Schuler Kompakt",
        }
    ]
    assert content["changed_by"]["role_in_use"] == "administrator"
    (event,) = stack.events(organization)
    assert event == {
        "change_id": body["change_id"],
        "record_id": body["record_id"],
        "list_version": 2,
        "added_count": 2,
        "removed_count": 0,
    }
    row = stack.row(organization)
    assert (row["version"], row["record_id"], row["lemmatizer_version"]) == (
        2,
        record["record_id"],
        lexicon.LEMMATIZER_VERSION,
    )
    terms = stack.terms(organization)
    assert sorted((t["value"], t["added_in_version"], t["removed_in_version"]) for t in terms) == [
        (r"linea[\W_]*sur", 2, None),
        ("pasillos", 2, None),
    ]
    assert {(t["origin"], t["added_reason"]) for t in terms} == {("customer_added", REASON)}
    listed = stack.get(admin).json()
    assert listed["list_version"] == 2 and listed["record_id"] == body["record_id"]
    customer = [item for item in listed["terms"] if item["origin"] == "customer_added"]
    assert sorted(item["term_id"] for item in customer) == sorted(
        item["term_id"] for item in body["added"]
    )
    assert [name["value"] for name in listed["declared_names"]] == ["Prensa Schuler Kompakt"]


def test_a_blocked_term_is_recorded_but_never_as_free_text(
    stack: VocabularyStack, site: Site, admin: Who
) -> None:
    """BR-LAZ-83 con términos de la lista inicial: ``sabotaje`` (lema inicial) como patrón y
    ``garantizamos`` (flexión de ``garantizar``) como lema son ellos mismos bloqueados."""
    organization = site.organization_id
    added = [term(SABOTAJE.value, form="pattern"), term("garantizamos")]
    response = stack.post(admin, {"added": added, "reason": REASON})
    assert response.status_code == 201, response.text
    (record,) = stack.records(organization)
    assert [item["value"] for item in record["content"]["added"]] == ["sabotaje", "garantizamos"]
    # Las mismas palabras en las rutas que sí son texto libre se rechazan sin escribir nada.
    for body in (
        {"added": [term("bocina")], "reason": "Prohibir la palabra sabotaje en los textos"},
        {"added": [term("bocina", suggestion_es="Garantizamos la detección en la zona")]},
    ):
        body.setdefault("reason", REASON)
        rejected = stack.post(admin, body)
        assert _error(rejected) == _mapped(LoopDetailCode.BLOCKED_VOCABULARY_TERM), rejected.text
    assert len(stack.records(organization)) == 1
    # Y el término nuevo ya bloquea en la siguiente escritura de la organización.
    blocked = stack.post(
        admin, {"added": [term("bocina")], "reason": "Ajuste del comité: garantizamos revisión"}
    )
    assert _error(blocked) == _mapped(LoopDetailCode.BLOCKED_VOCABULARY_TERM)


# --- errores ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [None, "", "   ", "Corto 9 c", "  nueve c  "],
    ids=["ausente", "vacio", "blanco", "nueve", "nueve-con-bordes"],
)
def test_without_a_reason_it_is_vocabulary_change_reason_required(
    stack: VocabularyStack, site: Site, admin: Who, reason: str | None
) -> None:
    body: dict[str, Any] = {"added": [term("pasillo")]}
    if reason is not None:
        body["reason"] = reason
    response = stack.post(admin, body)
    assert _error(response) == _mapped(LoopDetailCode.VOCABULARY_CHANGE_REASON_REQUIRED)
    assert _mapped(LoopDetailCode.VOCABULARY_CHANGE_REASON_REQUIRED)[:2] == (400, "invalid_request")
    _nothing_written(stack, site.organization_id)


def test_a_reason_of_exactly_ten_characters_is_accepted(
    stack: VocabularyStack, site: Site, admin: Who
) -> None:
    response = stack.post(admin, {"added": [term("pasillo")], "reason": "Diez letra"})
    assert response.status_code == 201, response.text


def test_a_term_already_in_force_is_vocabulary_term_duplicate(
    stack: VocabularyStack, site: Site, admin: Who
) -> None:
    organization = site.organization_id
    assert stack.post(admin, {"added": [term("pasillo")], "reason": REASON}).status_code == 201
    stack.post(
        admin,
        {"declared_names_added": [{"kind": "zone", "value": "Línea Krones"}], "reason": REASON},
    )
    before = (len(stack.records(organization)), stack.row(organization)["version"])
    for body in (
        {"added": [term(DERIVED.value, form=DERIVED.form.value)]},  # de la lista inicial
        {"added": [term("despejado")]},  # lema inicial
        {"added": [term("PASILLO")]},  # mismo valor plegado que uno del cliente
        {"added": [term("cinta"), term("Cinta")]},  # repetido dentro del cambio
        {"declared_names_added": [{"kind": "machine", "value": "LINEA KRONES"}]},
        {
            "declared_names_added": [
                {"kind": "machine", "value": "Torno Uno"},
                {"kind": "equipment", "value": "torno uno"},
            ]
        },
    ):
        response = stack.post(admin, {**body, "reason": REASON})
        assert _error(response) == _mapped(LoopDetailCode.VOCABULARY_TERM_DUPLICATE), body
        assert response.status_code == 409
    assert (len(stack.records(organization)), stack.row(organization)["version"]) == before


def test_a_derived_term_or_one_not_in_force_cannot_be_removed(
    stack: VocabularyStack, site: Site, admin: Who
) -> None:
    organization = site.organization_id
    assert stack.post(admin, {"added": [term("pasillo")], "reason": REASON}).status_code == 201
    for removed in (str(DERIVED.term_id), str(SABOTAJE.term_id), str(uuid.uuid4())):
        response = stack.post(admin, {"removed": [removed], "reason": REASON})
        assert _error(response) == (400, "invalid_request", None), response.text
    unknown_name = stack.post(
        admin, {"declared_names_removed": [str(uuid.uuid4())], "reason": REASON}
    )
    assert _error(unknown_name) == (400, "invalid_request", None)
    assert stack.row(organization)["version"] == 2 and len(stack.records(organization)) == 1
    # Las 50 de la lista inicial siguen vigentes.
    listed = stack.get(admin).json()
    assert sum(item["origin"] == "derived" for item in listed["terms"]) == len(INITIAL_TERMS)


@pytest.mark.parametrize(
    "item",
    [
        term("dos palabras"),
        term("x"),
        term("a" * 65),
        term("(sin cerrar", form="pattern"),
        term("(a+)+", form="pattern"),
        term("a*", form="pattern"),
        term("a" * 257, form="pattern"),
        term("pasillo", category="desconocida"),
        term("pasillo", form="otra"),
    ],
    ids=[
        "lema-dos-palabras",
        "lema-una-letra",
        "lema-65",
        "patron-no-compila",
        "patron-anidado",
        "patron-vacio",
        "patron-257",
        "categoria",
        "forma",
    ],
)
def test_a_malformed_term_is_invalid_request(
    stack: VocabularyStack, site: Site, admin: Who, item: dict[str, str]
) -> None:
    response = stack.post(admin, {"added": [item], "reason": REASON})
    assert _error(response) == (400, "invalid_request", None), response.text
    _nothing_written(stack, site.organization_id)


def test_an_empty_change_is_invalid_request(stack: VocabularyStack, site: Site, admin: Who) -> None:
    response = stack.post(admin, {"reason": REASON})
    assert _error(response) == (400, "invalid_request", None)
    _nothing_written(stack, site.organization_id)


# --- retirar ---------------------------------------------------------------------------------


def test_removing_a_customer_term_keeps_its_row_and_its_record(
    stack: VocabularyStack, site: Site, admin: Who
) -> None:
    organization = site.organization_id
    added = stack.post(admin, {"added": [term("pasillo"), term("cinta")], "reason": REASON})
    pasillo, cinta = added.json()["added"]
    removed = stack.post(admin, {"removed": [pasillo["term_id"]], "reason": REASON})
    assert removed.status_code == 201, removed.text
    assert (removed.json()["list_version"], removed.json()["removed"]) == (3, [pasillo["term_id"]])
    first, second = stack.records(organization)
    assert [item["value"] for item in first["content"]["added"]] == ["pasillo", "cinta"]
    assert second["content"]["removed"] == [{"term_id": pasillo["term_id"], "value": "pasillo"}]
    rows = {str(row["term_id"]): row for row in stack.terms(organization)}
    assert (
        rows[pasillo["term_id"]]["added_in_version"],
        rows[pasillo["term_id"]]["removed_in_version"],
    ) == (2, 3)
    assert rows[cinta["term_id"]]["removed_in_version"] is None
    listed = stack.get(admin).json()
    assert listed["list_version"] == 3
    customer = {item["term_id"] for item in listed["terms"] if item["origin"] == "customer_added"}
    assert customer == {cinta["term_id"]}
    assert stack.events(organization)[-1]["removed_count"] == 1
    # Retirado, se puede volver a añadir (otro término, otra versión).
    again = stack.post(admin, {"added": [term("pasillo")], "reason": REASON})
    assert again.status_code == 201 and again.json()["list_version"] == 4


# --- vocabulario declarado -------------------------------------------------------------------


def test_a_declared_name_stops_being_a_proper_name_in_the_next_write(
    stack: VocabularyStack, site: Site, admin: Who
) -> None:
    organization = site.organization_id
    reason = "Revisión de la Prensa Schuler Kompakt acordada en comité"
    # Sin declarar, el nombre de la máquina parece el de una persona: fallo cerrado.
    rejected = stack.post(admin, {"added": [term("pasillo")], "reason": reason})
    assert _error(rejected) == _mapped(LoopDetailCode.PERSON_ATTRIBUTION_DETECTED)
    # VIG-344: la alternativa del patrón, sin el nombre que coincidió (P3).
    assert rejected.json()["rejection"] == {
        "code": "person_attribution_detected",
        "suggestion": (
            "Retire el nombre de la persona; nombre la zona, el equipo o el cargo sin persona."
        ),
    }
    declared = stack.post(
        admin,
        {
            "declared_names_added": [{"kind": "machine", "value": "Prensa Schuler Kompakt"}],
            "reason": REASON,
        },
    )
    assert declared.status_code == 201, declared.text
    # La siguiente escritura de la organización ya lo exime (la versión de la lista cambió: no
    # espera a la caducidad de 60 s de la caché).
    accepted = stack.post(admin, {"added": [term("pasillo")], "reason": reason})
    assert accepted.status_code == 201, accepted.text
    # Otra organización no lo tiene declarado.
    other = stack.site()
    other_admin = stack.member(other)
    still = stack.post(other_admin, {"added": [term("pasillo")], "reason": reason})
    assert _error(still) == _mapped(LoopDetailCode.PERSON_ATTRIBUTION_DETECTED)
    # Retirarlo vuelve a rechazar el nombre en la siguiente escritura.
    name_id = declared.json()["declared_names_added"][0]["name_id"]
    removed = stack.post(admin, {"declared_names_removed": [name_id], "reason": REASON})
    assert removed.status_code == 201, removed.text
    assert stack.records(organization)[-1]["content"]["declared_names_removed"] == [
        {"name_id": name_id, "value": "Prensa Schuler Kompakt"}
    ]
    again = stack.post(admin, {"added": [term("cinta")], "reason": reason})
    assert _error(again) == _mapped(LoopDetailCode.PERSON_ATTRIBUTION_DETECTED)
    assert stack.get(admin).json()["declared_names"] == []


@pytest.mark.parametrize(
    "value",
    ["x", " Prensa", "Prensa ", "12345", "a" * 121, "Juan <b>Pérez</b>"],
    ids=["una-letra", "espacio-inicial", "espacio-final", "sin-letras", "121", "marcado"],
)
def test_a_malformed_declared_name_is_invalid_request(
    stack: VocabularyStack, site: Site, admin: Who, value: str
) -> None:
    response = stack.post(
        admin, {"declared_names_added": [{"kind": "machine", "value": value}], "reason": REASON}
    )
    assert _error(response) == (400, "invalid_request", None), response.text
    _nothing_written(stack, site.organization_id)


def test_a_declared_name_with_a_blocked_term_is_rejected(
    stack: VocabularyStack, site: Site, admin: Who
) -> None:
    response = stack.post(
        admin,
        {"declared_names_added": [{"kind": "zone", "value": "Zona Despejada"}], "reason": REASON},
    )
    assert _error(response) == _mapped(LoopDetailCode.BLOCKED_VOCABULARY_TERM)
    # VIG-344: el término que cayó (plegado) y la alternativa de la lista inicial (§14.1).
    assert response.json()["rejection"] == {
        "code": "blocked_vocabulary_term",
        "term": "despejada",
        "suggestion": (
            "Describe lo que el sistema observó y en qué periodo; no afirmes lo que no se observó."
        ),
    }
    _nothing_written(stack, site.organization_id)


# --- alcance ---------------------------------------------------------------------------------


def test_another_organization_neither_sees_nor_touches_the_list(
    stack: VocabularyStack, site: Site, admin: Who
) -> None:
    stack.post(
        admin,
        {
            "added": [term("pasillo")],
            "declared_names_added": [{"kind": "machine", "value": "Prensa Norte"}],
            "reason": REASON,
        },
    )
    other = stack.site()
    other_admin = stack.member(other)
    listed = stack.get(other_admin).json()
    assert listed["list_version"] == 1 and listed["declared_names"] == []
    assert {item["origin"] for item in listed["terms"]} == {"derived"}
    changed = stack.post(other_admin, {"added": [term("cinta")], "reason": REASON})
    assert changed.json()["list_version"] == 2
    assert stack.row(site.organization_id)["version"] == 2
    assert len(stack.records(site.organization_id)) == 1
    mine = stack.get(admin).json()
    assert {item["value"] for item in mine["terms"] if item["origin"] == "customer_added"} == {
        "pasillo"
    }


@pytest.mark.parametrize("role", [Role.COORDINATOR_SST, Role.PLANT_MANAGER, Role.COPASST])
def test_a_role_without_vocabulary_manage_gets_not_found(
    stack: VocabularyStack, site: Site, role: Role
) -> None:
    who = stack.member(site, role=role, level=_level_for(role), scope=_scope_for(site, role))
    for response in (
        stack.get(who),
        stack.post(who, {"added": [term("pasillo")], "reason": REASON}),
    ):
        assert _error(response) == (404, "not_found", None), response.text
    _nothing_written(stack, site.organization_id)


def _level_for(role: Role) -> ScopeLevel:
    return ScopeLevel.ORGANIZATION if role is Role.COORDINATOR_SST else ScopeLevel.PLANT


def _scope_for(site: Site, role: Role) -> uuid.UUID | None:
    return None if role is Role.COORDINATOR_SST else next(iter(site.plants))


def test_under_a_provider_concession_both_routes_are_not_found(
    stack: VocabularyStack, site: Site, admin: Who
) -> None:
    installer = stack.installer(site)
    for response in (
        stack.get(installer),
        stack.post(installer, {"added": [term("pasillo")], "reason": REASON}),
    ):
        assert _error(response) == (404, "not_found", None), response.text
    _nothing_written(stack, site.organization_id)


@pytest.fixture(scope="module")
def superuser(stack: VocabularyStack) -> Iterator[Any]:
    """``shared.db`` como superusuario: la seguridad de fila no filtra nada."""
    database = app_database(
        stack.authz.sessions.migrated,
        url=stack.authz.sessions.migrated.as_role(None).sqlalchemy_url,
    )
    yield database
    stack.run(database.dispose())


async def _superuser_read(
    superuser: Any, repository: PostgresVocabularyRepository, context: ScopeContext
) -> tuple[Any, Any, tuple[str, ...]]:
    """La fila, los términos y los nombres de ``context`` leídos como superusuario."""
    async with superuser.transaction(context) as transaction:
        head, terms = await repository.current(transaction)

        async def rows(statement: Executable, params: Mapping[str, Any]) -> Sequence[Row[Any]]:
            result = await transaction.execute(statement, params)
            return list(result.all())

        names = await PostgresDeclaredNamesStore().names(context.organization_id, rows)
    return head, terms, names


def test_the_repository_and_the_names_store_filter_by_organization_without_rls(
    stack: VocabularyStack, superuser: Any
) -> None:
    first, second = stack.site(), stack.site()
    for site, value, name in ((first, "pasillo", "Prensa Uno"), (second, "cinta", "Torno Dos")):
        response = stack.post(
            stack.member(site),
            {
                "added": [term(value)],
                "declared_names_added": [{"kind": "machine", "value": name}],
                "reason": REASON,
            },
        )
        assert response.status_code == 201, response.text
    repository = PostgresVocabularyRepository(superuser)
    for site, value, name in ((first, "pasillo", "Prensa Uno"), (second, "cinta", "Torno Dos")):
        context = unit_context(site.organization_id, ActorUnit.U04)
        head, terms, names = stack.run(_superuser_read(superuser, repository, context))
        assert head is not None and head.organization_id == site.organization_id
        assert [item.value for item in head.declared_names] == [name]
        assert [item.value for item in terms] == [value]
        assert name in names
        other_name = "Torno Dos" if name == "Prensa Uno" else "Prensa Uno"
        assert other_name not in names
        read_head, read_terms = stack.run(repository.read(context))
        assert [item.value for item in read_terms] == [value]
        assert read_head is not None and read_head.version == 2


def test_the_cache_preparation_reads_the_declared_names_of_its_writer(
    stack: VocabularyStack, site: Site, admin: Who
) -> None:
    """``PostgresDeclaredNamesStore`` con el lector de la escritura (como ``vigia_app``): los
    nombres de la jerarquía y los declarados de la organización."""
    stack.post(
        admin,
        {
            "declared_names_added": [{"kind": "standard", "value": "Norma Técnica Uno"}],
            "reason": REASON,
        },
    )
    context = unit_context(site.organization_id, ActorUnit.U04)

    async def rows(statement: Executable, params: Mapping[str, Any]) -> Sequence[Row[Any]]:
        return await stack.database.read(context, statement, params)

    preparation = FreeTextPreparation(site.organization_id, rows, stack.authz.sessions.clock)
    names = stack.run(PostgresDeclaredNamesStore().names(preparation.organization_id, rows))
    assert "Norma Técnica Uno" in names
    hierarchy = stack.fetch(
        "SELECT name FROM identity.zone WHERE organization_id = $1", site.organization_id
    )
    assert {row["name"] for row in hierarchy} <= set(names)


def test_the_list_is_read_at_the_version_of_its_head(
    stack: VocabularyStack, site: Site, admin: Who
) -> None:
    """Un ``GET`` que coincide con un cambio a medio confirmar (los términos de la versión 3 ya
    escritos, la cabeza aún en la 2) responde la versión 2 entera, sin mezclar las dos."""
    organization = site.organization_id
    added = stack.post(admin, {"added": [term("pasillo")], "reason": REASON}).json()
    (pasillo,) = added["added"]
    row = stack.row(organization)
    stack.fetch(
        "INSERT INTO loop.blocked_term (term_id, list_id, organization_id, category, form, value,"
        " suggestion_es, origin, added_in_version, added_reason) VALUES ($1, $2, $3,"
        " 'absence_claim', 'lemma', 'bocina', 'Alternativa sintética', 'customer_added', 3,"
        " 'Motivo sintético del término')",
        uuid.uuid4(),
        row["list_id"],
        organization,
    )
    stack.fetch(
        "UPDATE loop.blocked_term SET removed_in_version = 3 WHERE term_id = $1",
        uuid.UUID(pasillo["term_id"]),
    )
    listed = stack.get(admin).json()
    assert listed["list_version"] == 2
    customer = [item["value"] for item in listed["terms"] if item["origin"] == "customer_added"]
    assert customer == ["pasillo"]


def test_declared_names_is_always_a_json_array(
    stack: VocabularyStack, site: Site, admin: Who
) -> None:
    """``laz_0039``: la columna nace con ``[]`` y su restricción rechaza lo que no es una lista."""
    assert stack.post(admin, {"added": [term("pasillo")], "reason": REASON}).status_code == 201
    assert stack.row(site.organization_id)["declared_names"] in ("[]", [])
    with pytest.raises(asyncpg.CheckViolationError):
        stack.fetch(
            "UPDATE loop.blocked_vocabulary_list SET declared_names = '{}'::jsonb"
            " WHERE organization_id = $1",
            site.organization_id,
        )
