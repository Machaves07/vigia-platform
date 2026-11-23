"""El histórico de lo cerrado: ``GET /loop/closed-findings`` (TASK-586; O-11; BR-LAZ-01, 02, 06,
09 y 12; LC-LAZ-05).

Sobre PostgreSQL 16 real como ``vigia_app`` y la aplicación real (el mundo de U-04 de
``tests/laz_support``: hallazgos ingresados por las rutas del contrato de U-03 y proyectados;
filas de ``finding_state`` copiadas como superusuario con las columnas que cada prueba fija), con
el histórico, la bandeja, el detalle y la ficha de vida montados en la misma aplicación:

- **Solo lo cerrado**: con filas en los nueve estados, la lista da exactamente las ``closed`` y
  ``discarded`` del alcance, en orden ``(occurred_at_bucket, finding_state_id)`` descendente, sin
  repetidos al paginar de 1 en 1 (oráculo: la proyección leída como superusuario).
- **Paginación** (propiedad, como PR-APP-15 y PR-LAZ-57): para todo conjunto generado (estados
  mezclados, cubos repetidos) y todo límite de 1 a 200, recorrer todas las páginas da la misma
  lista que una sola consulta de 200.
- **Alcance**: otra organización, otra planta, la zona hermana y una zona sin compuerta de uso
  aprobada en el instante del hecho (BR-LAZ-12) nunca aparecen; una zona ajena pedida responde
  igual que una inexistente (``not_found``).
- **Sin filtro por persona**: cualquier parámetro fuera de BR-LAZ-02, un estado activo, un orden
  o un límite fuera de 1 a 200, ``invalid_request``.
- **Sin auditoría de consulta** (BR-LAZ-09): listar no escribe ninguna entrada; abrir la ficha de
  vida de una fila escribe exactamente una.
- **Índice**: ``EXPLAIN`` de la sentencia usa ``finding_state_closed`` (``laz_0044``), que está en
  la tabla y en cada partición con el predicado de los dos estados; la migración sube y, como toda
  migración de la plataforma, no baja (NFR-NUC-14).

Solo datos generados. Las marcas salen del reloj simulado, que arranca en la hora de la base
(retro 14); topes de las esperas de la base: 60 s (retro 15).
"""

from __future__ import annotations

import importlib.util
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import text

from tests.api_support import World
from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import IngestedSubject, LoopOrganization, RealPorts, loop_world
from tests.loop_detail_support import DetailEvidenceStore
from tests.loop_inbox_support import SAME_ORIGIN, InboxStack, Who
from vigia_platform.catalog.adapters.postgres.query_ports import catalog_query_ports
from vigia_platform.fleet.application.inventory_read import FleetInventory
from vigia_platform.identity.adapters.authz_store import LedgerProviderQueryLedger
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.ledger.application.coverage import CoverageService
from vigia_platform.ledger.application.evidence_read import EvidenceService
from vigia_platform.ledger.application.reader import LectorExpediente
from vigia_platform.loop.adapters.http.history_routes import CLOSED_FINDINGS_PATH
from vigia_platform.loop.adapters.http.services import LOOP_STATE_KEY, LoopHttp
from vigia_platform.loop.adapters.http.timeline_routes import TIMELINE_PATH
from vigia_platform.loop.adapters.postgres.history_query import (
    CLOSED_STATES,
    closed_findings_statement,
)
from vigia_platform.loop.adapters.postgres.inbox_query import InboxCursor, InboxFilters
from vigia_platform.loop.application import history as history_module
from vigia_platform.loop.application.detail import FindingDetailService
from vigia_platform.loop.application.history import (
    MAX_HISTORY_PAGE,
    ClosedFindingsPage,
    ClosedFindingsService,
    HistoryRequestInvalid,
)
from vigia_platform.loop.application.inbox import InboxService
from vigia_platform.loop.application.timeline import FindingTimelineService
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.domain.enums import FindingLifecycleState
from vigia_platform.loop.domain.time_aggregation import GRID_ORIGIN, bucket_start
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration

BACKEND: Final = Path(__file__).resolve().parents[2]
BASE: Final = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
BUDGET: Final = {"ci": 25, "pr": 25, "nightly": 200}
"""Ejemplos por perfil: cada uno siembra una zona nueva sobre la base real y la recorre."""
EXAMPLES: Final = BUDGET.get(settings.get_current_profile_name(), BUDGET["nightly"])
_EXPLAIN_SQL: Final = "EXPLAIN (FORMAT JSON) " + closed_findings_statement()
_EXPLAIN: Final = text(_EXPLAIN_SQL)
"""El plan de la sentencia del histórico (solo constantes)."""

ACTIVE: Final = (
    "received",
    "in_review",
    "open",
    "action_assigned",
    "overdue",
    "unclosable",
    "suspended_by_reclassification",
)
"""Los siete estados activos, escritos aquí de forma literal (oráculo, no la constante)."""
CLOSED: Final = ("closed", "discarded")
"""Los dos estados del histórico, escritos aquí de forma literal."""


def _columns(state: str) -> dict[str, Any]:
    """Columnas coherentes con las restricciones de ``finding_state`` para ``state``."""
    if state in ("received", "in_review"):
        return {
            "lifecycle_state": state,
            "classification_count": 0,
            "current_classification_outcome": None,
        }
    outcome = "false_positive" if state == "discarded" else "confirmed"
    columns: dict[str, Any] = {
        "lifecycle_state": state,
        "classification_count": 1,
        "current_classification_outcome": outcome,
    }
    if state == "suspended_by_reclassification":
        columns["previous_lifecycle_state"] = "open"
    return columns


def _load_migration() -> Any:
    path = BACKEND / "migrations" / "versions" / "laz_0044_closed_findings_index.py"
    spec = importlib.util.spec_from_file_location(f"{path.stem}_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass
class HistoryStack:
    inbox: InboxStack
    """Siembra, personas y utilidades del mundo (``loop_inbox_support``)."""
    history: ClosedFindingsService
    client: httpx.AsyncClient

    def _get(self, who: Who, path: str, params: Any = None) -> httpx.Response:
        headers = {**SAME_ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={who.value}"}
        response: httpx.Response = self.inbox.run(
            self.client.get(path, params=params, headers=headers)
        )
        return response

    def get(self, who: Who, params: Any = None) -> httpx.Response:
        return self._get(who, CLOSED_FINDINGS_PATH, params)

    def timeline(self, who: Who, finding_state_id: uuid.UUID | str) -> httpx.Response:
        return self._get(who, TIMELINE_PATH.format(finding_state_id=finding_state_id))

    def ids(self, who: Who, params: Any = None) -> list[str]:
        """Los ``finding_state_id`` de todas las páginas, en orden."""
        seen: list[str] = []
        query = dict(params or {})
        while True:
            body = _body(self.get(who, query))
            seen += [item["finding_state_id"] for item in body["items"]]
            if body["next_cursor"] is None:
                return seen
            query["cursor"] = body["next_cursor"]

    def audit_count(self, organization: LoopOrganization) -> int:
        (row,) = self.inbox.fetch(
            "SELECT count(*) AS n FROM shared.audit_entry WHERE organization_id = $1",
            organization.organization_id,
        )
        return int(row["n"])

    def expected(self, zone_ids: list[uuid.UUID]) -> list[str]:
        """El oráculo: las filas cerradas o descartadas con compuerta aprobada de esas zonas, en
        orden de clave descendente (leídas como superusuario)."""
        rows = self.inbox.fetch(
            "SELECT finding_state_id FROM loop.finding_state WHERE zone_id = ANY($1::uuid[])"
            " AND lifecycle_state IN ('closed', 'discarded')"
            " AND zone_gate_status_at_event = 'approved'"
            " ORDER BY occurred_at_bucket DESC, finding_state_id DESC",
            zone_ids,
        )
        return [str(row["finding_state_id"]) for row in rows]


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[HistoryStack]:
    with loop_world(postgres_endpoint, "loop_history_routes") as world:
        base = world.stack
        authz = base.authz
        clock = authz.sessions.clock
        audit = authz.sessions.audit
        database = base.database()
        queries = catalog_query_ports(database)
        ports = RealPorts(
            catalog=queries.catalog,
            gates=queries.gates,
            coverage=CoverageService(database=database, audit=audit, clock=clock),
            fleet=FleetInventory(
                database=database,
                authorizer=authz.authorizer,
                audit=audit,
                clock=clock,
                provider_organization_id=authz.provider_organization_id,
            ),
        )
        detail = FindingDetailService(
            database=database,
            authorizer=authz.authorizer,
            reader=LectorExpediente(database=database, audit=audit, clock=clock),
            evidence=EvidenceService(
                database=database, audit=audit, storage=DetailEvidenceStore(base.storage, clock)
            ),
            catalog=ports.catalog,
            gates=ports.gates,
            fleet=ports.fleet,
            coverage=ports.coverage,
        )
        timeline = FindingTimelineService(
            subjects=detail,
            reader=LectorExpediente(database=database, audit=audit, clock=clock),
            timezones=PostgresLoopConfigurationRepository(database),
        )
        inbox = InboxService(database=database, authorizer=authz.authorizer)
        history = ClosedFindingsService(database=database, authorizer=authz.authorizer)
        app = World(clock=clock).app(
            runtime={
                "sessions": authz.contexts,
                "authorizer": ContextAuthorizer(
                    audit=authz.audit,
                    provider_organization_id=authz.provider_organization_id,
                    provider_queries=LedgerProviderQueryLedger(base.primary.writer),
                    clock=clock,
                ),
                "state": {
                    LOOP_STATE_KEY: LoopHttp(
                        inbox=inbox, detail=detail, timeline=timeline, history=history
                    )
                },
            },
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60.0
        )
        try:
            yield HistoryStack(InboxStack(world, database, inbox, client), history, client)
        finally:
            base.run(client.aclose())
            base.run(database.dispose())


def _body(response: Any) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _error(response: Any) -> tuple[int, str | None]:
    return response.status_code, response.json().get("code")


def _comparable(response: Any) -> tuple[int, Any]:
    body = response.json()
    return response.status_code, {k: v for k, v in body.items() if k != "correlation_id"}


def _nine_states(stack: HistoryStack, finding: IngestedSubject, zone_id: uuid.UUID) -> None:
    """Dos filas por estado en ``zone_id``, con cubos repetidos (empates por identificador)."""
    for index, state in enumerate(ACTIVE + CLOSED):
        for copy in range(2):
            moment = BASE - timedelta(hours=index % 3)
            stack.inbox.row(
                finding,
                zone_id=zone_id,
                occurred_at_exact=moment + timedelta(minutes=copy),
                occurred_at_bucket=moment,
                **_columns(state),
            )


# --- solo lo cerrado ------------------------------------------------------------------------------


def test_only_closed_and_discarded_in_key_order_one_by_one_to_the_end(
    stack: HistoryStack,
) -> None:
    organization = stack.inbox.world.organization()
    who = stack.inbox.member(organization)
    finding = stack.inbox.finding(organization)
    _nine_states(stack, finding, organization.zone_id)
    expected = stack.expected([organization.zone_id])
    assert len(expected) == 4
    seen = stack.ids(who, {"limit": "1"})
    assert seen == expected
    assert len(seen) == len(set(seen))
    states = {
        str(row["finding_state_id"]): row["lifecycle_state"]
        for row in stack.inbox.fetch(
            "SELECT finding_state_id, lifecycle_state FROM loop.finding_state WHERE zone_id = $1",
            organization.zone_id,
        )
    }
    assert sorted(states[item] for item in seen) == ["closed", "closed", "discarded", "discarded"]
    # El estado filtra dentro de los dos: closed solo da los cerrados, discarded los descartados.
    assert {states[i] for i in stack.ids(who, {"lifecycle_state": "closed"})} == {"closed"}
    assert {states[i] for i in stack.ids(who, {"lifecycle_state": "discarded"})} == {"discarded"}
    assert stack.ids(who, {"lifecycle_state": ["closed", "discarded"]}) == expected
    # La bandeja, con las mismas filas, no da ninguna de ellas (cola de trabajo aparte).
    assert not set(seen) & set(stack.inbox.ids(who))


def test_a_row_has_only_zone_level_fields_and_its_values(stack: HistoryStack) -> None:
    organization = stack.inbox.world.organization()
    who = stack.inbox.member(organization)
    finding = stack.inbox.finding(organization)
    exact = datetime(2026, 10, 7, 13, 47, 31, 123000, tzinfo=UTC)
    closed = stack.inbox.row(
        finding,
        occurred_at_exact=exact,
        occurred_at_bucket=bucket_start(exact, 60),
        single_occupancy_zone=False,
        current_reason_category="guard_bypassed",
        current_action_id=uuid.uuid4(),
        has_signed_root_cause=True,
        **_columns("closed"),
    )
    discarded = stack.inbox.row(
        finding,
        occurred_at_exact=exact - timedelta(days=1),
        occurred_at_bucket=bucket_start(exact - timedelta(days=1), 60),
        single_occupancy_zone=False,
        current_reason_category=None,
        current_action_id=None,
        has_signed_root_cause=False,
        **_columns("discarded"),
    )
    body = _body(stack.get(who))
    assert body["partitions"] == [] and body["next_cursor"] is None
    by_id = {item["finding_state_id"]: item for item in body["items"]}
    assert set(by_id) == {str(closed), str(discarded)}
    row = stack.inbox.world.finding_state(finding)
    assert row is not None
    assert by_id[str(closed)] == {
        "finding_state_id": str(closed),
        "plant_id": str(organization.plant_id),
        "zone_id": str(organization.zone_id),
        "family": row["family"],
        "tier": row["tier"],
        "lifecycle_state": "closed",
        "classification_outcome": "confirmed",
        "reason_category": "guard_bypassed",
        "has_action": True,
        "has_signed_root_cause": True,
        "occurred_at": format_timestamp(exact),
        "time_aggregated": False,
        "aggregation_window_minutes": None,
    }
    assert (
        by_id[str(discarded)]["lifecycle_state"],
        by_id[str(discarded)]["reason_category"],
        by_id[str(discarded)]["has_action"],
        by_id[str(discarded)]["has_signed_root_cause"],
    ) == ("discarded", None, False, False)


def test_in_a_single_occupancy_zone_the_mark_is_the_start_of_its_window(
    stack: HistoryStack,
) -> None:
    organization = stack.inbox.world.organization()
    who = stack.inbox.member(organization)
    finding = stack.inbox.finding(organization)
    exact = datetime(2026, 10, 9, 22, 59, 59, 999000, tzinfo=UTC)
    for window in (15, 60, 480):
        single = stack.inbox.row(
            finding,
            occurred_at_exact=exact,
            occurred_at_bucket=bucket_start(exact, window),
            single_occupancy_zone=True,
            aggregation_window_minutes=window,
            **_columns("closed"),
        )
        items = {item["finding_state_id"]: item for item in _body(stack.get(who))["items"]}
        shown = items[str(single)]
        assert (shown["time_aggregated"], shown["aggregation_window_minutes"]) == (True, window)
        moment = datetime.fromisoformat(shown["occurred_at"])
        assert (moment - GRID_ORIGIN) % timedelta(minutes=window) == timedelta(0)
        assert moment <= exact < moment + timedelta(minutes=window)
        assert shown["occurred_at"] != format_timestamp(exact)


def test_filters_are_zone_family_tier_state_and_period(stack: HistoryStack) -> None:
    organization = stack.inbox.world.organization()
    who = stack.inbox.member(organization)
    finding = stack.inbox.finding(organization)
    base = datetime(2026, 10, 1, tzinfo=UTC)
    closed = _columns("closed")
    rows = {
        "early": stack.inbox.row(
            finding, occurred_at_exact=base, occurred_at_bucket=base, **closed
        ),
        "late": stack.inbox.row(
            finding,
            occurred_at_exact=base + timedelta(days=5),
            occurred_at_bucket=base + timedelta(days=5),
            **closed,
        ),
        "dwell": stack.inbox.row(finding, family="dwell", **closed),
        "tier_2": stack.inbox.row(finding, tier="tier_2", **closed),
        "other_zone": stack.inbox.row(finding, zone_id=organization.site.other_zone_id, **closed),
    }

    def ids(params: dict[str, Any]) -> set[str]:
        return set(stack.ids(who, params))

    assert ids({}) == {str(row) for row in rows.values()}
    assert ids({"family": "dwell"}) == {str(rows["dwell"])}
    assert ids({"tier": "tier_2"}) == {str(rows["tier_2"])}
    assert ids({"zone_id": str(organization.site.other_zone_id)}) == {str(rows["other_zone"])}
    assert str(rows["other_zone"]) not in ids({"zone_id": str(organization.site.zone_id)})
    # Periodo [from, to) sobre la marca que se muestra: el borde inferior entra, el superior no.
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
        *({"lifecycle_state": state} for state in ACTIVE),
        {"lifecycle_state": "nope"},
        {"lifecycle_state": ["closed", "open"]},
        {"family": "headcount"},
        {"tier": "tier_3"},
        {"order": "newest_first"},
        {"order": "by_person"},
        {"limit": "0"},
        {"limit": "201"},
        {"limit": "-1"},
        {"from": "2026-10-01"},
        {"from": "2026-10-02T00:00:00Z", "to": "2026-10-01T00:00:00Z"},
        {"from": "2026-10-01T00:00:00Z", "to": "2026-10-01T00:00:00Z"},
        {"cursor": "!!"},
        {"cursor": "bm9wZQ"},
        {"cursor": "x" * 129},
        {"zone_id": "not-a-uuid"},
        {"zone_id": [str(uuid.UUID(int=index + 1)) for index in range(51)]},
        {"family": ["dwell", "coexistence"]},
        {"person_id": str(uuid.UUID(int=7))},
        {"user_id": str(uuid.UUID(int=8))},
        {"assignee_user_id": str(uuid.UUID(int=9))},
        {"shift": "night"},
        {"crew": "a"},
        {"team": "a"},
    ],
)
def test_any_parameter_outside_br_laz_02_is_invalid_request(
    stack: HistoryStack, params: dict[str, Any]
) -> None:
    who = stack.inbox.member(stack.inbox.world.a)
    assert _error(stack.get(who, params)) == (400, "invalid_request")


def test_the_page_is_50_by_default_200_at_most_and_the_cursor_continues(
    stack: HistoryStack,
) -> None:
    organization = stack.inbox.world.organization()
    who = stack.inbox.member(organization)
    finding = stack.inbox.finding(organization)
    for index in range(201):
        moment = BASE - timedelta(hours=index)
        stack.inbox.row(
            finding, occurred_at_exact=moment, occurred_at_bucket=moment, **_columns("closed")
        )
    default = _body(stack.get(who))
    assert len(default["items"]) == 50 and default["next_cursor"] is not None
    first = _body(stack.get(who, {"limit": "200"}))
    assert len(first["items"]) == 200 and first["next_cursor"] is not None
    second = _body(stack.get(who, {"limit": "200", "cursor": first["next_cursor"]}))
    assert len(second["items"]) == 1 and second["next_cursor"] is None
    ids = [item["finding_state_id"] for item in first["items"] + second["items"]]
    assert ids == stack.expected([organization.zone_id])


# --- paginación: propiedad -----------------------------------------------------------------------


@pytest.fixture(scope="module")
def template(stack: HistoryStack) -> IngestedSubject:
    return stack.inbox.finding(stack.inbox.world.a)


@given(
    states=st.lists(st.sampled_from(ACTIVE + CLOSED), min_size=0, max_size=14),
    hours=st.lists(st.integers(0, 3), min_size=14, max_size=14),
    limit=st.one_of(st.integers(1, 15), st.integers(1, MAX_HISTORY_PAGE)),
)
@settings(max_examples=EXAMPLES, deadline=None)
def test_walking_all_pages_with_any_limit_gives_the_single_query(
    stack: HistoryStack,
    template: IngestedSubject,
    states: list[str],
    hours: list[int],
    limit: int,
) -> None:
    organization = stack.inbox.world.a
    zone_id = stack.inbox.zone_in(organization, organization.plant_id)
    for index, state in enumerate(states):
        # Pocas horas distintas: hay cubos repetidos y el empate lo resuelve el identificador.
        moment = BASE - timedelta(hours=hours[index])
        stack.inbox.row(
            template,
            zone_id=zone_id,
            occurred_at_exact=moment,
            occurred_at_bucket=moment,
            **_columns(state),
        )
    context = stack.inbox.world.context(organization)
    filters = InboxFilters(zone_ids=(zone_id,))

    async def walk() -> tuple[list[uuid.UUID], list[int]]:
        seen: list[uuid.UUID] = []
        sizes: list[int] = []
        after: InboxCursor | None = None
        while True:
            page = await stack.history.page(context, filters=filters, after=after, limit=limit)
            assert isinstance(page, ClosedFindingsPage)
            sizes.append(len(page.items))
            seen += [item.finding_state_id for item in page.items]
            if page.next_cursor is None:
                return seen, sizes
            after = page.next_cursor
            assert len(sizes) <= len(states) + 1, "el recorrido no termina"

    single = stack.inbox.run(stack.history.page(context, filters=filters, limit=MAX_HISTORY_PAGE))
    assert single.next_cursor is None
    seen, sizes = stack.inbox.run(walk())
    assert seen == [item.finding_state_id for item in single.items]
    assert [str(i) for i in seen] == stack.expected([zone_id])
    assert all(size <= limit for size in sizes)


# --- alcance (BR-LAZ-01 y 12) --------------------------------------------------------------------


def test_a_zone_without_an_approved_gate_at_the_event_never_appears(stack: HistoryStack) -> None:
    organization = stack.inbox.world.organization()
    who = stack.inbox.member(organization)
    finding = stack.inbox.finding(organization)
    shown = stack.inbox.row(finding, **_columns("closed"))
    pending = stack.inbox.row(finding, zone_gate_status_at_event="pending", **_columns("closed"))
    revoked = stack.inbox.row(finding, zone_gate_status_at_event="revoked", **_columns("discarded"))
    seen = stack.ids(who)
    assert seen == [str(shown)]
    assert str(pending) not in seen and str(revoked) not in seen


def test_a_zone_scope_never_shows_the_sibling_zone_of_its_plant(stack: HistoryStack) -> None:
    organization = stack.inbox.world.organization()
    site = organization.site
    finding = stack.inbox.finding(organization)
    own = stack.inbox.row(finding, **_columns("closed"))
    sibling = stack.inbox.row(finding, zone_id=site.other_zone_id, **_columns("closed"))
    zone_user = stack.inbox.member(
        organization, Role.COORDINATOR_SST, ScopeLevel.ZONE, site.zone_id
    )
    assert stack.ids(zone_user) == [str(own)]
    sibling_response = stack.get(zone_user, {"zone_id": str(site.other_zone_id)})
    missing_response = stack.get(zone_user, {"zone_id": str(uuid.uuid4())})
    assert _error(sibling_response) == (404, "not_found")
    assert _comparable(sibling_response) == _comparable(missing_response)
    # Con alcance de organización la zona hermana sí aparece (la guarda es la del alcance).
    assert str(sibling) in stack.ids(stack.inbox.member(organization))


def test_a_plant_scope_never_shows_another_plant_of_its_organization(stack: HistoryStack) -> None:
    organization = stack.inbox.world.organization()
    finding = stack.inbox.finding(organization)
    own = stack.inbox.row(finding, **_columns("discarded"))
    other_plant = stack.inbox.world.add_plant(organization)
    other_zone = stack.inbox.zone_in(organization, other_plant)
    elsewhere = stack.inbox.row(
        finding, plant_id=other_plant, zone_id=other_zone, **_columns("closed")
    )
    plant_user = stack.inbox.member(
        organization, Role.PLANT_MANAGER, ScopeLevel.PLANT, organization.plant_id
    )
    assert stack.ids(plant_user) == [str(own)]
    other_response = stack.get(plant_user, {"zone_id": str(other_zone)})
    assert _error(other_response) == (404, "not_found")
    assert _comparable(other_response) == _comparable(
        stack.get(plant_user, {"zone_id": str(uuid.uuid4())})
    )
    assert str(elsewhere) in stack.ids(stack.inbox.member(organization))


def test_another_organization_and_a_role_without_the_key_answer_not_found(
    stack: HistoryStack,
) -> None:
    a = stack.inbox.world.organization()
    b = stack.inbox.world.organization()
    who = stack.inbox.member(a)
    foreign = stack.inbox.row(stack.inbox.finding(b), **_columns("closed"))
    own = stack.inbox.row(stack.inbox.finding(a), **_columns("closed"))
    assert stack.ids(who) == [str(own)]
    assert str(foreign) not in stack.ids(who)
    foreign_response = stack.get(who, {"zone_id": str(b.zone_id)})
    missing_response = stack.get(who, {"zone_id": str(uuid.uuid4())})
    assert _error(foreign_response) == (404, "not_found")
    assert _comparable(foreign_response) == _comparable(missing_response)
    copasst = stack.inbox.member(a, Role.COPASST)
    assert _error(stack.get(copasst)) == (404, "not_found")


@pytest.mark.parametrize(
    ("state", "notice"),
    [("rebuilding", "projection_rebuilding"), ("diverged", "projection_diverged")],
)
def test_a_plant_whose_projection_is_not_healthy_is_declared_and_not_served(
    stack: HistoryStack, state: str, notice: str
) -> None:
    organization = stack.inbox.world.organization()
    who = stack.inbox.member(organization)
    finding = stack.inbox.finding(organization)
    closed = stack.inbox.row(finding, **_columns("closed"))
    stack.inbox.checkpoint_state(organization, state)
    body = _body(stack.get(who))
    assert body["partitions"] == [{"plant_id": str(organization.plant_id), "notice": notice}]
    assert body["items"] == []
    stack.inbox.checkpoint_state(organization, "healthy")
    assert stack.ids(who) == [str(closed)]


# --- sin auditoría de consulta (BR-LAZ-09) -------------------------------------------------------


def test_listing_writes_no_audit_entry_and_opening_a_row_writes_exactly_one(
    stack: HistoryStack,
) -> None:
    organization = stack.inbox.world.organization()
    who = stack.inbox.member(organization)
    finding = stack.inbox.finding(organization)
    # El hallazgo ingresado, cerrado en la proyección: su ficha lee su expediente real.
    stack.inbox.execute(
        "UPDATE loop.finding_state SET lifecycle_state = 'closed', classification_count = 1,"
        " current_classification_outcome = 'confirmed' WHERE finding_state_id = $1",
        finding.record_id,
    )
    before = stack.audit_count(organization)
    for params in ({}, {"limit": "1"}, {"lifecycle_state": "closed"}):
        body = _body(stack.get(who, params))
        assert [item["finding_state_id"] for item in body["items"]] == [str(finding.record_id)]
    assert stack.audit_count(organization) == before
    # Abrir la ficha de vida desde la fila: exactamente una entrada (la lectura auditada).
    (row,) = _body(stack.get(who))["items"]
    timeline = _body(stack.timeline(who, row["finding_state_id"]))
    assert timeline["finding_state_id"] == row["finding_state_id"]
    assert timeline["lifecycle_state"] == "closed"
    assert stack.audit_count(organization) == before + 1


# --- índice parcial y migración -------------------------------------------------------------------


def test_the_statement_uses_the_partial_closed_index(stack: HistoryStack) -> None:
    organization = stack.inbox.world.organization()
    finding = stack.inbox.finding(organization)
    for index in range(20):
        stack.inbox.row(finding, **_columns(CLOSED[index % 2]))
    indexes = {
        row["relid"]
        for row in stack.inbox.fetch(
            "SELECT relid::regclass::text AS relid"
            " FROM pg_partition_tree('loop.finding_state_closed')"
        )
    }
    context = stack.inbox.world.context(organization)
    site = organization.site

    async def explain() -> Any:
        # Con pocas filas el planificador prefiere recorrer la tabla: se le quita esa opción para
        # ver si la sentencia **puede** usar el índice parcial (su predicado está implicado).
        async with stack.inbox.database.transaction(context) as transaction:
            await transaction.execute(text("SET LOCAL enable_seqscan = off"))
            result = await transaction.execute(
                _EXPLAIN,
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
                    "limit": 51,
                },
            )
            return result.scalar_one()

    document = json.dumps(stack.inbox.run(explain()))
    used = {name for name in indexes if f'"{name.split(".")[-1]}"' in document}
    assert used - {"loop.finding_state_closed"}, document
    # Y nunca el de la bandeja, que no cubre estos estados.
    assert '"finding_state_inbox"' not in document


def test_the_migration_creates_the_index_on_every_partition_and_only_goes_forward(
    stack: HistoryStack,
) -> None:
    migration = _load_migration()
    assert migration.revision == "laz_0044" and migration.down_revision == "gob_0043"
    assert tuple(state.value for state in CLOSED_STATES) == migration.CLOSED_STATES == CLOSED
    (parent,) = stack.inbox.fetch(
        "SELECT indexdef, obj_description('loop.finding_state_closed'::regclass, 'pg_class')"
        " AS comment FROM pg_indexes WHERE schemaname = 'loop'"
        " AND indexname = 'finding_state_closed'"
    )
    assert parent["indexdef"].endswith(
        "(organization_id, plant_id, zone_id, lifecycle_state, occurred_at_bucket DESC,"
        " finding_state_id) WHERE (lifecycle_state = ANY (ARRAY['closed'::text,"
        " 'discarded'::text]))"
    ), parent["indexdef"]
    assert parent["comment"] == (
        "Histórico de lo cerrado: closed y discarded, en orden de cubo (TASK-586, PAT-LAZ-ESC-03)"
    )
    leaves = stack.inbox.fetch(
        "SELECT relid::regclass::text AS relid FROM pg_partition_tree('loop.finding_state')"
        " WHERE isleaf"
    )
    copies = stack.inbox.fetch(
        "SELECT relid::regclass::text AS relid FROM pg_partition_tree('loop.finding_state_closed')"
        " WHERE isleaf"
    )
    assert leaves and len(copies) == len(leaves)
    # Solo hacia adelante (NFR-NUC-14): la bajada se rechaza, como en toda migración.
    with pytest.raises(NotImplementedError):
        migration.downgrade()


# --- validación del servicio (las guardas que la ruta no deja llegar) --------------------------


@pytest.mark.parametrize(
    ("filters", "limit"),
    [
        (InboxFilters(), 0),
        (InboxFilters(), MAX_HISTORY_PAGE + 1),
        (InboxFilters(lifecycle_states=(FindingLifecycleState.OPEN,)), 1),
        (InboxFilters(lifecycle_states=()), 1),
        (InboxFilters(zone_ids=()), 1),
        (
            InboxFilters(
                period_from=datetime(2026, 10, 2, tzinfo=UTC),
                period_to=datetime(2026, 10, 2, tzinfo=UTC),
            ),
            1,
        ),
    ],
    ids=["limit_0", "limit_201", "active_state", "no_states", "no_zones", "empty_period"],
)
def test_the_service_rejects_impossible_filters(
    stack: HistoryStack, filters: InboxFilters, limit: int
) -> None:
    context = stack.inbox.world.context(stack.inbox.world.a)
    with pytest.raises(HistoryRequestInvalid):
        stack.inbox.run(stack.history.page(context, filters=filters, limit=limit))


def test_the_service_denies_a_context_without_findings_read(
    stack: HistoryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defensa en profundidad bajo la guarda de la ruta: si el contexto no conserva ninguna
    asignación con ``findings.read`` (``narrowed`` da ``None``), el servicio deniega (auditado)
    con ``ResourceNotFound`` sin abrir transacción, nunca con una lista."""
    organization = stack.inbox.world.organization()
    stack.inbox.row(stack.inbox.finding(organization), **_columns("closed"))
    monkeypatch.setattr(history_module, "narrowed", lambda *args, **kwargs: None)
    context = stack.inbox.world.context(organization)
    with pytest.raises(ResourceNotFound):
        stack.inbox.run(stack.history.page(context))
