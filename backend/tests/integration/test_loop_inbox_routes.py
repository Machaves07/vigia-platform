"""SUP-U05-01: la bandeja del lazo (TASK-307; LC-LAZ-05; BR-LAZ-01 a 12 y 149 a 156).

Sobre PostgreSQL 16 real como ``vigia_app`` y la aplicación real (``inbox_stack``):

- **Lo que se declara** (P2): una zona con nodo mudo o sin estado, sin nodo o con nodo que
  comunica sale con su aviso cerrado, su nodo y su ``communication_state``, **también sin
  elementos** (igualdad exacta del cuerpo); una planta en ``rebuilding`` o ``diverged``, con su
  código (BR-LAZ-156).
- **Avisos de cada elemento** desde las columnas enriquecidas: acuerdo revocado con su fecha, acta
  no vigente con su causa, evidencia marcada y tramos faltantes; BR-LAZ-12: sin compuerta de uso
  aprobada en el instante del hecho, el elemento no se muestra.
- **Filtros** (BR-LAZ-02): zona, familia, tier, estado (solo los siete activos) y periodo; un
  parámetro desconocido, repetido o fuera de su lista, ``invalid_request``.
- **Alcance** (BR-LAZ-01): una persona con alcance de zona no ve la zona hermana de su planta;
  una con alcance de planta no ve la otra planta de su organización; una zona fuera del alcance,
  de otra organización o inexistente responde ``not_found``, igual.
- **Agregación** (BR-LAZ-06, PR-LAZ-02): en zona unipersonal toda marca es el inicio de la
  ventana y el ``content_hash`` del registro no cambia; con ``false``, la marca es exacta.
- **Cero llamadas** a los puertos de U-03 y de U-02, **una sola sentencia** y el **índice
  parcial** en el plan (PR-LAZ-57, NFR-LAZ-03, PAT-LAZ-ESC-03); página de 1 a 200.

Solo datos generados.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest
from sqlalchemy import text

from tests.integration.conftest import PostgresEndpoint
from tests.loop_inbox_support import InboxStack, Who, counting_ports, inbox_stack
from vigia_platform.loop.adapters.postgres.inbox_query import inbox_statement
from vigia_platform.loop.domain.time_aggregation import GRID_ORIGIN, bucket_start
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration

_EXPLAIN_NEWEST_SQL: Final = "EXPLAIN (FORMAT JSON) " + inbox_statement(newest_first=True)
_EXPLAIN_OLDEST_SQL: Final = "EXPLAIN (FORMAT JSON) " + inbox_statement(newest_first=False)
EXPLAIN_NEWEST: Final = text(_EXPLAIN_NEWEST_SQL)
EXPLAIN_OLDEST: Final = text(_EXPLAIN_OLDEST_SQL)
"""El plan de la sentencia de la bandeja en los dos sentidos (solo constantes)."""


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[InboxStack]:
    with inbox_stack(postgres_endpoint, "loop_inbox_routes") as built:
        yield built


@pytest.fixture(scope="module")
def coordinator(stack: InboxStack) -> Who:
    return stack.member(stack.world.a)


def _body(response: Any) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _error(response: Any) -> tuple[int, str | None]:
    return response.status_code, response.json().get("code")


def _comparable(response: Any) -> tuple[int, Any]:
    body = response.json()
    return response.status_code, {k: v for k, v in body.items() if k != "correlation_id"}


def _zone(stack: InboxStack, organization: Any, zone_id: uuid.UUID, notice: str) -> dict[str, Any]:
    """La entrada esperada de ``zones`` de una zona del sitio de ``organization``."""
    site = organization.site
    if zone_id != site.zone_id:
        return {
            "plant_id": str(site.plant_id),
            "zone_id": str(zone_id),
            "notice": notice,
            "node_id": None,
            "node_code": None,
            "communication_state": None,
        }
    (row,) = stack.fetch(
        "SELECT n.code, i.communication_state FROM identity.node_identity n"
        " LEFT JOIN fleet.node_inventory i ON i.node_id = n.node_id WHERE n.node_id = $1",
        site.node_id,
    )
    return {
        "plant_id": str(site.plant_id),
        "zone_id": str(zone_id),
        "notice": notice,
        "node_id": str(site.node_id),
        "node_code": row["code"],
        "communication_state": row["communication_state"] or "unknown",
    }


# --- lo que se declara: nunca «sin hallazgos» -----------------------------------------------------


def test_a_zone_without_findings_declares_its_coverage_notice_and_never_an_empty_inbox(
    stack: InboxStack,
) -> None:
    # Una organización nueva: su zona con nodo (sin latido: unknown) y la hermana sin nodo.
    organization = stack.world.organization()
    who = stack.member(organization)
    site = organization.site
    body = _body(stack.get(who, {"zone_id": [str(site.zone_id), str(site.other_zone_id)]}))
    assert body == {
        "items": [],
        "zones": sorted(
            [
                _zone(stack, organization, site.zone_id, "not_observed"),
                _zone(stack, organization, site.other_zone_id, "no_node_assigned"),
            ],
            key=lambda zone: zone["zone_id"],
        ),
        "partitions": [],
        "next_cursor": None,
    }
    # El nodo mudo: no se observó; el nodo que comunica: la cobertura no está en la bandeja.
    for state, notice in (("mute", "not_observed"), ("reachable", "coverage_unavailable")):
        stack.node_state(organization, state)
        body = _body(stack.get(who, {"zone_id": str(site.zone_id)}))
        assert body == {
            "items": [],
            "zones": [_zone(stack, organization, site.zone_id, notice)],
            "partitions": [],
            "next_cursor": None,
        }
        assert body["zones"][0]["communication_state"] == state


def test_without_zone_filter_every_zone_in_scope_is_declared(stack: InboxStack) -> None:
    organization = stack.world.organization()
    who = stack.member(organization)
    site = organization.site
    body = _body(stack.get(who))
    assert {zone["zone_id"]: zone["notice"] for zone in body["zones"]} == {
        str(site.zone_id): "not_observed",
        str(site.other_zone_id): "no_node_assigned",
    }


@pytest.mark.parametrize(
    ("state", "notice"),
    [("rebuilding", "projection_rebuilding"), ("diverged", "projection_diverged")],
)
def test_a_plant_whose_projection_is_rebuilding_or_diverged_is_declared(
    stack: InboxStack, state: str, notice: str
) -> None:
    organization = stack.world.organization()
    who = stack.member(organization)
    finding = stack.finding(organization)
    stack.checkpoint_state(organization, state)
    body = _body(stack.get(who))
    assert body["partitions"] == [{"plant_id": str(organization.plant_id), "notice": notice}]
    # Los elementos siguen visibles: la declaración no inventa ni esconde estado.
    assert [item["finding_state_id"] for item in body["items"]] == [str(finding.record_id)]
    stack.checkpoint_state(organization, "healthy")
    assert _body(stack.get(who))["partitions"] == []


# --- avisos de cada elemento ------------------------------------------------------------------


def test_each_item_carries_the_notices_of_its_enriched_columns(stack: InboxStack) -> None:
    organization = stack.world.organization()
    who = stack.member(organization)
    finding = stack.finding(organization)
    revoked_at = datetime(2026, 9, 3, 10, 7, 11, 250000, tzinfo=UTC)
    plain = stack.row(finding, single_occupancy_zone=False)
    flagged = stack.row(
        finding,
        single_occupancy_zone=False,
        zone_agreement_revoked=True,
        zone_agreement_revoked_at=revoked_at,
        walk_test_regression_pending=True,
        walk_test_regression_cause="model_version_change",
        evidence_flagged=True,
        missing_segments_declared=True,
    )
    hidden = stack.row(finding, zone_gate_status_at_event="pending")
    revoked_gate = stack.row(finding, zone_gate_status_at_event="revoked")
    items = {item["finding_state_id"]: item for item in _body(stack.get(who))["items"]}
    # BR-LAZ-12: sin compuerta de uso aprobada en el instante del hecho, no se muestra.
    assert str(hidden) not in items and str(revoked_gate) not in items
    assert items[str(plain)]["notices"] == [] and items[str(plain)]["evidence_flagged"] is False
    assert items[str(flagged)]["notices"] == [
        {"code": "agreement_revoked", "since": format_timestamp(revoked_at), "cause": None},
        {"code": "walk_test_regression_pending", "since": None, "cause": "model_version_change"},
        {"code": "evidence_flagged", "since": None, "cause": None},
        {"code": "missing_segments_declared", "since": None, "cause": None},
    ]
    assert items[str(flagged)]["evidence_flagged"] is True


def test_an_item_has_only_zone_level_fields(stack: InboxStack) -> None:
    organization = stack.world.organization()
    who = stack.member(organization)
    finding = stack.finding(organization)
    (item,) = _body(stack.get(who))["items"]
    row = stack.world.finding_state(finding)
    assert row is not None
    assert set(item) == {
        "finding_state_id",
        "subject_kind",
        "plant_id",
        "zone_id",
        "family",
        "tier",
        "standard",
        "lifecycle_state",
        "classification_outcome",
        "classification_count",
        "age_days",
        "observability_state_at_event",
        "occurred_at",
        "due_at",
        "overdue_days",
        "time_aggregated",
        "aggregation_window_minutes",
        "evidence_flagged",
        "notices",
    }
    assert (item["family"], item["tier"], item["lifecycle_state"]) == (
        row["family"],
        row["tier"],
        "received",
    )
    assert item["standard"] == {
        "standard_id": str(row["standard_id"]),
        "version": row["standard_version"],
    }
    assert item["observability_state_at_event"] == row["observability_state_at_event"]


# --- filtros ------------------------------------------------------------------------------------


def test_filters_are_zone_family_tier_state_and_period(stack: InboxStack) -> None:
    organization = stack.world.organization()
    who = stack.member(organization)
    finding = stack.finding(organization)
    base = datetime(2026, 10, 1, tzinfo=UTC)
    rows = {
        "early": stack.row(finding, occurred_at_exact=base, occurred_at_bucket=base),
        "late": stack.row(
            finding,
            occurred_at_exact=base + timedelta(days=5),
            occurred_at_bucket=base + timedelta(days=5),
        ),
        "dwell": stack.row(finding, family="dwell"),
        "tier_2": stack.row(finding, tier="tier_2"),
        "open": stack.row(
            finding,
            lifecycle_state="open",
            classification_count=1,
            current_classification_outcome="confirmed",
        ),
        "closed": stack.row(
            finding,
            lifecycle_state="closed",
            classification_count=1,
            current_classification_outcome="confirmed",
        ),
        "discarded": stack.row(
            finding,
            lifecycle_state="discarded",
            classification_count=1,
            current_classification_outcome="false_positive",
        ),
        "other_zone": stack.row(finding, zone_id=organization.site.other_zone_id),
    }

    def ids(params: dict[str, Any]) -> set[str]:
        return set(stack.ids(who, params))

    everything = ids({})
    # Solo los siete estados activos (índice parcial): cerrado y descartado no son de la bandeja.
    assert str(rows["closed"]) not in everything and str(rows["discarded"]) not in everything
    assert {str(rows[name]) for name in ("early", "late", "dwell", "open")} <= everything
    assert ids({"family": "dwell"}) == {str(rows["dwell"])}
    assert ids({"tier": "tier_2"}) == {str(rows["tier_2"])}
    assert ids({"lifecycle_state": "open"}) == {str(rows["open"])}
    assert ids({"lifecycle_state": ["open", "received"]}) == everything
    assert ids({"zone_id": str(organization.site.other_zone_id)}) == {str(rows["other_zone"])}
    assert str(rows["other_zone"]) not in ids({"zone_id": str(organization.site.zone_id)})
    # Periodo [from, to) sobre la marca agregada: el borde inferior entra, el superior no.
    assert ids(
        {"from": format_timestamp(base), "to": format_timestamp(base + timedelta(days=5))}
    ) == {str(rows["early"])}
    assert ids(
        {
            "from": format_timestamp(base + timedelta(milliseconds=1)),
            "to": format_timestamp(base + timedelta(days=5, milliseconds=1)),
        }
    ) == {str(rows["late"])}


@pytest.mark.parametrize(
    "params",
    [
        {"lifecycle_state": "closed"},
        {"lifecycle_state": "discarded"},
        {"lifecycle_state": "nope"},
        {"family": "headcount"},
        {"tier": "tier_3"},
        {"order": "by_person"},
        {"limit": "0"},
        {"limit": "201"},
        {"limit": "-1"},
        {"from": "2026-10-01"},
        {"from": "2026-10-01T00:00:00"},
        {"from": "2026-10-02T00:00:00Z", "to": "2026-10-01T00:00:00Z"},
        {"from": "2026-10-01T00:00:00Z", "to": "2026-10-01T00:00:00Z"},
        {"cursor": "!!"},
        {"cursor": "bm9wZQ"},
        {"cursor": "x" * 129},
        {"zone_id": "not-a-uuid"},
        {"zone_id": [str(uuid.uuid4()) for _ in range(51)]},
        {"family": ["dwell", "coexistence"]},
        {"person_id": str(uuid.uuid4())},
        {"shift": "night"},
        {"crew": "a"},
    ],
)
def test_invalid_or_unknown_parameters_are_invalid_request(
    stack: InboxStack, coordinator: Who, params: dict[str, Any]
) -> None:
    assert _error(stack.get(coordinator, params)) == (400, "invalid_request")


# --- alcance --------------------------------------------------------------------------------------


def test_a_zone_scope_never_shows_the_sibling_zone_of_its_plant(stack: InboxStack) -> None:
    organization = stack.world.organization()
    site = organization.site
    finding = stack.finding(organization)
    sibling = stack.row(finding, zone_id=site.other_zone_id)
    zone_user = stack.member(organization, Role.COORDINATOR_SST, ScopeLevel.ZONE, site.zone_id)
    body = _body(stack.get(zone_user))
    assert [item["finding_state_id"] for item in body["items"]] == [str(finding.record_id)]
    assert [zone["zone_id"] for zone in body["zones"]] == [str(site.zone_id)]
    assert str(sibling) not in stack.ids(zone_user)
    # La zona hermana pedida explícitamente: igual que una inexistente.
    sibling_response = stack.get(zone_user, {"zone_id": str(site.other_zone_id)})
    missing_response = stack.get(zone_user, {"zone_id": str(uuid.uuid4())})
    assert _error(sibling_response) == (404, "not_found")
    assert _comparable(sibling_response) == _comparable(missing_response)


def test_a_plant_scope_never_shows_another_plant_of_its_organization(stack: InboxStack) -> None:
    organization = stack.world.organization()
    finding = stack.finding(organization)
    other_plant = stack.world.add_plant(organization)
    other_zone = stack.zone_in(organization, other_plant)
    elsewhere = stack.row(finding, plant_id=other_plant, zone_id=other_zone)
    plant_user = stack.member(
        organization, Role.PLANT_MANAGER, ScopeLevel.PLANT, organization.plant_id
    )
    body = _body(stack.get(plant_user))
    seen = {item["finding_state_id"] for item in body["items"]}
    assert str(finding.record_id) in seen and str(elsewhere) not in seen
    assert {zone["plant_id"] for zone in body["zones"]} == {str(organization.plant_id)}
    other_response = stack.get(plant_user, {"zone_id": str(other_zone)})
    assert _comparable(other_response) == _comparable(
        stack.get(plant_user, {"zone_id": str(uuid.uuid4())})
    )
    assert _error(other_response) == (404, "not_found")
    # Con alcance de organización, la otra planta sí aparece (la guarda es la del alcance).
    everyone = stack.member(organization)
    assert str(elsewhere) in set(stack.ids(everyone))


def test_another_organization_and_a_role_without_the_key_answer_not_found(
    stack: InboxStack, coordinator: Who
) -> None:
    b = stack.world.b
    stack.finding(b)
    foreign = stack.get(coordinator, {"zone_id": str(b.zone_id)})
    missing = stack.get(coordinator, {"zone_id": str(uuid.uuid4())})
    assert _error(foreign) == (404, "not_found")
    assert _comparable(foreign) == _comparable(missing)
    # Sin filtro, nada de B.
    assert str(b.zone_id) not in stack.get(coordinator).text
    # Un rol sin findings.read: not_found, nunca forbidden.
    copasst = stack.member(stack.world.a, Role.COPASST)
    assert _error(stack.get(copasst)) == (404, "not_found")


# --- agregación temporal (BR-LAZ-06; PR-LAZ-02) --------------------------------------------------


def test_in_a_single_occupancy_zone_every_mark_is_the_start_of_its_window(
    stack: InboxStack,
) -> None:
    organization = stack.world.organization()
    who = stack.member(organization)
    finding = stack.finding(organization)
    exact = datetime(2026, 10, 7, 13, 47, 31, 123000, tzinfo=UTC)
    due = datetime(2026, 10, 30, 9, 13, 2, 7000, tzinfo=UTC)
    revoked = datetime(2026, 10, 9, 22, 59, 59, 999000, tzinfo=UTC)
    columns: dict[str, Any] = {
        "occurred_at_exact": exact,
        "occurred_at_bucket": bucket_start(exact, 60),
        "due_at": due,
        "zone_agreement_revoked": True,
        "zone_agreement_revoked_at": revoked,
    }
    (record_hash,) = stack.fetch(
        "SELECT content_hash, record_hash FROM ledger.ledger_record WHERE record_id = $1",
        finding.record_id,
    )
    for window in (15, 45, 60, 120, 480):
        aggregated = stack.row(
            finding, single_occupancy_zone=True, aggregation_window_minutes=window, **columns
        )
        exact_row = stack.row(
            finding, single_occupancy_zone=False, aggregation_window_minutes=window, **columns
        )
        items = {item["finding_state_id"]: item for item in _body(stack.get(who))["items"]}
        shown = items[str(aggregated)]
        assert (shown["time_aggregated"], shown["aggregation_window_minutes"]) == (True, window)
        marks = [shown["occurred_at"], shown["due_at"], shown["notices"][0]["since"]]
        for mark, real in zip(marks, (exact, due, revoked), strict=True):
            moment = datetime.fromisoformat(mark)
            assert (moment - GRID_ORIGIN) % timedelta(minutes=window) == timedelta(0), mark
            assert moment <= real < moment + timedelta(minutes=window)
        plain = items[str(exact_row)]
        assert (plain["time_aggregated"], plain["aggregation_window_minutes"]) == (False, None)
        assert [plain["occurred_at"], plain["due_at"], plain["notices"][0]["since"]] == [
            format_timestamp(exact),
            format_timestamp(due),
            format_timestamp(revoked),
        ]
    # El expediente conserva la marca exacta y su hash: la agregación es solo de presentación.
    assert stack.fetch(
        "SELECT content_hash, record_hash FROM ledger.ledger_record WHERE record_id = $1",
        finding.record_id,
    ) == [record_hash]
    (projected,) = stack.fetch(
        "SELECT occurred_at_exact FROM loop.finding_state WHERE finding_state_id = $1",
        finding.record_id,
    )
    row = stack.world.finding_state(finding)
    assert row is not None and projected["occurred_at_exact"] == row["occurred_at_exact"]


# --- cero llamadas, una sentencia, índice parcial y página (PR-LAZ-57) --------------------------


def test_serving_the_inbox_makes_zero_port_calls_and_a_single_statement(
    stack: InboxStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    organization = stack.world.organization()
    who = stack.member(organization)
    finding = stack.finding(organization)
    for _ in range(5):
        stack.row(finding)
    stack.checkpoint_state(organization, "rebuilding")
    with counting_ports(monkeypatch, stack.database) as calls:
        # Control positivo: una llamada real a un puerto sí se cuenta.
        coverage = stack.world.real_ports(stack.database).coverage
        stack.run(
            coverage.estado_en(stack.world.context(organization), organization.zone_id, stack.now())
        )
        assert sum(calls.values()) == 1
        calls.clear()
        with stack.counting() as log:
            body = _body(stack.get(who, {"limit": "3"}))
            assert len(body["items"]) == 3 and body["next_cursor"] is not None
            _body(stack.get(who, {"limit": "3", "cursor": body["next_cursor"]}))
    assert calls == Counter()
    # Una sentencia por página (la apertura del contexto de la transacción no cuenta).
    assert len(log.queries) == 2, log.queries
    assert all("FROM loop.finding_state f" in statement for statement in log.queries)
    stack.checkpoint_state(organization, "healthy")


def test_the_statement_uses_the_partial_inbox_index(stack: InboxStack) -> None:
    organization = stack.world.organization()
    finding = stack.finding(organization)
    for _ in range(20):
        stack.row(finding)
    indexes = {
        row["relid"]
        for row in stack.fetch(
            "SELECT relid::regclass::text AS relid"
            " FROM pg_partition_tree('loop.finding_state_inbox')"
        )
    }
    context = stack.world.context(organization)
    site = organization.site

    async def explain(newest_first: bool) -> Any:
        # Con pocas filas el planificador prefiere recorrer la tabla: se le quita esa opción para
        # ver si la sentencia **puede** usar el índice parcial (su predicado está implicado).
        async with stack.database.transaction(context) as transaction:
            await transaction.execute(text("SET LOCAL enable_seqscan = off"))
            result = await transaction.execute(
                EXPLAIN_NEWEST if newest_first else EXPLAIN_OLDEST,
                {
                    "organization_id": organization.organization_id,
                    "organization_wide": False,
                    "scope_plant_ids": [site.plant_id],
                    "scope_zone_ids": [],
                    "zone_ids": [site.zone_id],
                    "lifecycle_states": None,
                    "family": None,
                    "tier": None,
                    "period_from": None,
                    "period_to": None,
                    "after_bucket": None,
                    "after_id": None,
                    "limit": 201,
                },
            )
            return result.scalar_one()

    for newest_first in (True, False):
        plan = stack.run(explain(newest_first))
        document = json.dumps(plan)
        used = {name for name in indexes if f'"{name.split(".")[-1]}"' in document}
        assert used - {"loop.finding_state_inbox"}, document


def test_no_page_exceeds_200_and_the_cursor_continues(stack: InboxStack) -> None:
    organization = stack.world.organization()
    who = stack.member(organization)
    finding = stack.finding(organization)
    base = datetime(2026, 10, 2, tzinfo=UTC)
    for index in range(204):
        moment = base + timedelta(hours=index)
        stack.row(finding, occurred_at_exact=moment, occurred_at_bucket=moment)
    first = _body(stack.get(who))
    assert len(first["items"]) == 200 and first["next_cursor"] is not None
    second = _body(stack.get(who, {"cursor": first["next_cursor"]}))
    assert len(second["items"]) == 5 and second["next_cursor"] is None
    ids = [item["finding_state_id"] for item in first["items"] + second["items"]]
    assert len(ids) == len(set(ids)) == 205
    assert _error(stack.get(who, {"limit": "201"})) == (400, "invalid_request")
