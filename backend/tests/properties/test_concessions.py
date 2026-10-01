"""Concesiones del proveedor (TASK-127, LC-NUC-06; S-PLA-02; BR-NUC-35 a 42).

Propiedades de ``business-logic-model.md`` §11 (C-PLA-05 y C-PLA-04):

- **PR-NUC-12**: ``grant`` con duración fuera de ``[1 h, concession_max_days]`` se rechaza y
  dentro se acepta, para cualquier ``concession_max_days`` en ``[1, 90]``; sin duración,
  ``expires_at - granted_at = concession_default_days`` (función pura y contra PostgreSQL, con la
  base imponiendo el tope del cliente).
- **PR-NUC-11**: bajo cualquier concesión, toda secuencia generada de peticiones que leen o
  escriben datos del cliente produce exactamente un ``provider_query`` por petición en la cadena
  que corresponde (planta u organización) y ninguna petición de salud lo produce; el panel del
  cliente las devuelve todas, en orden, página a página.
- **PR-NUC-10** (junto con TASK-125): para una concesión concedida y quizá revocada por el
  servicio, ``context_from_session`` en ``t`` falla si ``t >= expires_at`` o ``t >= revoked_at``
  y, si no, su alcance es exactamente el concedido.

Criterios de aceptación: no existe concesión sobre la proveedora ni sin ``provider_user_id``
(``test_no_concession_over_the_provider_nor_without_provider_user``); tras revocar, la siguiente
petición del proveedor sobre ese cliente responde ``not_found``
(``test_after_client_revocation_the_next_provider_request_is_not_found`` y la del proveedor).
Además: la fila, el registro y el evento van juntos; la tarea ``expire_concessions`` y su
registro; el panel con historia completa y auditado; el contexto derivado de la concesión.

Solo datos generados.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.authz_support import SYSTEM_ACTOR_ID, Site, sealed_context
from tests.concession_support import REASON, ConcessionEnvironment, concession_environment
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.identity.application.concessions import (
    EXPIRE_CONCESSIONS,
    MIN_DURATION,
    Concession,
    ConcessionRejected,
    ConcessionRejectionCode,
    ConcessionStatus,
    ProviderQueryCursor,
    RevokedBySide,
    concession_duration,
    register_expire_concessions,
)
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.identity.authz.context import (
    ContextUnavailable,
    ContextUnavailableReason,
    ScopeContexts,
    record_provider_query,
)
from vigia_platform.shared.api.errors import ApiErrorCode, translate
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import (
    ActorKind,
    ActorUnit,
    AllowedScope,
    ContextAbsent,
    ContextOrigin,
    Role,
    ScopeContext,
    ScopeLevel,
)
from vigia_platform.shared.outbox.registries import PeriodicTaskRegistry

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
MICRO = timedelta(microseconds=1)


# --- PR-NUC-12: la duración, como función pura --------------------------------------------------


@st.composite
def duration_cases(draw: st.DrawFn) -> tuple[int, int, timedelta | None]:
    """``concession_max_days`` en [1, 90], su ``default_days`` y una duración junto a los bordes."""
    max_days = draw(st.integers(1, 90))
    default_days = draw(st.integers(1, max_days))
    top = timedelta(days=max_days)
    requested = draw(
        st.one_of(
            st.none(),
            st.sampled_from(
                [
                    timedelta(0),
                    MIN_DURATION - MICRO,
                    MIN_DURATION,
                    MIN_DURATION + MICRO,
                    top - MICRO,
                    top,
                    top + MICRO,
                    -HOUR,
                    timedelta(days=91),
                ]
            ),
            st.timedeltas(min_value=-DAY, max_value=timedelta(days=95)),
        )
    )
    return max_days, default_days, requested


@given(case=duration_cases())
def test_pr_nuc_12_duration_is_accepted_exactly_inside_the_client_bounds(
    case: tuple[int, int, timedelta | None],
) -> None:
    max_days, default_days, requested = case
    if requested is None:
        assert concession_duration(None, max_days=max_days, default_days=default_days) == (
            timedelta(days=default_days)
        )
        return
    inside = MIN_DURATION <= requested <= timedelta(days=max_days)
    if inside:
        assert (
            concession_duration(requested, max_days=max_days, default_days=default_days)
            == requested
        )
    else:
        with pytest.raises(ConcessionRejected) as raised:
            concession_duration(requested, max_days=max_days, default_days=default_days)
        assert raised.value.code is ConcessionRejectionCode.DURATION_OUT_OF_RANGE


@pytest.mark.parametrize(
    ("requested", "max_days", "default_days", "error"),
    [
        (3600, 7, 7, ConcessionRejected),  # segundos sueltos, no una duración
        (True, 7, 7, ConcessionRejected),
        (HOUR, 0, 1, ValueError),  # el cliente fija 1 a 90
        (HOUR, 91, 7, ValueError),
        (HOUR, 7, 8, ValueError),  # default <= max
        (HOUR, 7, 0, ValueError),
        (HOUR, 7.0, 7, TypeError),
        (HOUR, True, 1, TypeError),
    ],
)
def test_duration_inputs_out_of_the_rules(
    requested: Any, max_days: Any, default_days: Any, error: type[Exception]
) -> None:
    with pytest.raises(error):
        concession_duration(requested, max_days=max_days, default_days=default_days)


# --- Contexto derivado de la concesión (sin base) ------------------------------------------------

PROVIDER = uuid.UUID("00000000-0000-4000-8000-0000000000aa")
CLIENT = uuid.UUID("00000000-0000-4000-8000-0000000000bb")
PROBE_START = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)


class _NoStore:
    async def session_row(self, *_: Any) -> None:
        raise AssertionError("sin base")

    async def operator_row(self, *_: Any) -> None:
        raise AssertionError("sin base")


def _contexts() -> ScopeContexts:
    return ScopeContexts(
        store=_NoStore(),
        clock=SimulatedClock(PROBE_START),
        provider_organization_id=PROVIDER,
        system_actor_id=SYSTEM_ACTOR_ID,
    )


def _provider_base(**changes: Any) -> ScopeContext:
    scopes = (AllowedScope(ScopeLevel.ORGANIZATION, PROVIDER, Role.PROVIDER_INSTALLER),)
    arguments: dict[str, Any] = {"organization_id": PROVIDER, "scopes": scopes}
    arguments.update(changes)
    return sealed_context(arguments.pop("organization_id"), arguments.pop("scopes"), **arguments)


def test_provider_concession_context_is_the_client_context_of_that_concession() -> None:
    contexts = _contexts()
    base = _provider_base()
    concession_id, plant = uuid.uuid4(), uuid.uuid4()
    derived = contexts.provider_concession_context(
        base,
        concession_id=concession_id,
        client_organization_id=CLIENT,
        scope_level=ScopeLevel.PLANT,
        scope_id=plant,
    )
    assert derived.organization_id == CLIENT
    assert derived.concession_id == concession_id == derived.actor.concession_id
    assert derived.actor.kind is ActorKind.PROVIDER_USER
    assert derived.actor.id == base.actor.id
    assert derived.origin is ContextOrigin.SESSION
    assert derived.session_id_hash == base.session_id_hash
    assert derived.correlation_id == base.correlation_id
    assert derived.allowed_scopes == (
        AllowedScope(ScopeLevel.PLANT, plant, Role.PROVIDER_INSTALLER),
    )


@pytest.mark.parametrize(
    ("base_changes", "client", "level", "scope"),
    [
        ({"organization_id": CLIENT}, CLIENT, ScopeLevel.ORGANIZATION, CLIENT),  # no es proveedora
        ({"origin": ContextOrigin.OUTBOX_EVENT}, CLIENT, ScopeLevel.ORGANIZATION, CLIENT),
        (
            {"kind": ActorKind.PROVIDER_USER, "concession_id": uuid.uuid4()},
            CLIENT,
            ScopeLevel.ORGANIZATION,
            CLIENT,
        ),  # desde otra concesión no se concede ni se amplía
        ({"kind": ActorKind.OPERATOR}, CLIENT, ScopeLevel.ORGANIZATION, CLIENT),
        ({}, PROVIDER, ScopeLevel.ORGANIZATION, PROVIDER),  # nunca sobre la proveedora
        ({}, CLIENT, ScopeLevel.ZONE, uuid.uuid4()),
        ({}, CLIENT, ScopeLevel.ORGANIZATION, uuid.uuid4()),  # alcance de otra organización
    ],
)
def test_provider_concession_context_rules(
    base_changes: dict[str, Any], client: uuid.UUID, level: ScopeLevel, scope: uuid.UUID
) -> None:
    contexts = _contexts()
    with pytest.raises(ContextUnavailable) as raised:
        contexts.provider_concession_context(
            _provider_base(**base_changes),
            concession_id=uuid.uuid4(),
            client_organization_id=client,
            scope_level=level,
            scope_id=scope,
        )
    assert raised.value.reason is ContextUnavailableReason.CONCESSION_INVALID


def test_provider_concession_context_rejects_wrong_types() -> None:
    contexts = _contexts()
    with pytest.raises(ContextAbsent):
        contexts.provider_concession_context(
            "contexto",  # type: ignore[arg-type]
            concession_id=uuid.uuid4(),
            client_organization_id=CLIENT,
            scope_level=ScopeLevel.ORGANIZATION,
            scope_id=CLIENT,
        )
    with pytest.raises(TypeError):
        contexts.provider_concession_context(
            _provider_base(),
            concession_id=str(uuid.uuid4()),  # type: ignore[arg-type]
            client_organization_id=CLIENT,
            scope_level=ScopeLevel.ORGANIZATION,
            scope_id=CLIENT,
        )


def test_expire_concessions_is_registered_every_five_minutes() -> None:
    registry = PeriodicTaskRegistry()
    task = register_expire_concessions(registry, object())  # type: ignore[arg-type]
    assert task.task_name == EXPIRE_CONCESSIONS == "expire_concessions"
    assert task.unit is ActorUnit.U02
    assert task.to_persisted().schedule == "every:300s"
    assert registry.get(EXPIRE_CONCESSIONS) is task


def test_effective_status_never_shows_a_past_concession_as_active() -> None:
    granted = PROBE_START
    concession = Concession(
        concession_id=uuid.uuid4(),
        organization_id=CLIENT,
        provider_user_id=uuid.uuid4(),
        provider_organization_id=PROVIDER,
        scope_level=ScopeLevel.ORGANIZATION,
        scope_id=CLIENT,
        reason=REASON,
        granted_at=granted,
        expires_at=granted + HOUR,
        status=ConcessionStatus.ACTIVE,
    )
    assert concession.effective_status(granted + HOUR - MICRO) is ConcessionStatus.ACTIVE
    assert concession.in_force(granted) and not concession.in_force(granted - MICRO)
    assert concession.effective_status(granted + HOUR) is ConcessionStatus.EXPIRED
    assert not concession.in_force(granted + HOUR)


# --- Entorno contra PostgreSQL -------------------------------------------------------------------


@pytest.fixture(scope="module")
def env(postgres_endpoint: PostgresEndpoint) -> Iterator[ConcessionEnvironment]:
    with concession_environment(postgres_endpoint, "concessions") as environment:
        yield environment


@pytest.fixture(scope="module")
def site(env: ConcessionEnvironment) -> Site:
    return env.authz.add_site(plants=2, zones_per_plant=1)


@pytest.fixture(scope="module")
def installer(env: ConcessionEnvironment) -> uuid.UUID:
    return env.provider_user()


def _grant(
    env: ConcessionEnvironment,
    user_id: uuid.UUID,
    site: Site,
    *,
    level: ScopeLevel = ScopeLevel.ORGANIZATION,
    plant: uuid.UUID | None = None,
    duration: timedelta | None = None,
    reason: str = REASON,
) -> Concession:
    context = env.provider_context(user_id)
    scope_id = site.organization_id if level is ScopeLevel.ORGANIZATION else plant
    concession: Concession = env.run(
        env.service.grant(
            context,
            client_organization_id=site.organization_id,
            scope_level=level,
            scope_id=scope_id,  # type: ignore[arg-type]
            reason=reason,
            duration=duration,
        )
    )
    return concession


def _records(
    env: ConcessionEnvironment, record_type: str, concession_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Los registros de la concesión, con el contenido ya interpretado."""
    rows = env.fetch(
        "SELECT organization_id, plant_id, actor_kind, actor_id, actor_concession_id,"
        " actor_role_in_use, ledger.vigia_bytes_to_jsonb(content) AS content"
        " FROM ledger.ledger_record WHERE record_type = $1"
        " AND ledger.vigia_bytes_to_jsonb(content) ->> 'concession_id' = $2"
        " ORDER BY received_at, record_id",
        record_type,
        str(concession_id),
    )
    return [{**dict(row), "content": _payload(row["content"])} for row in rows]


def _events(env: ConcessionEnvironment, event_name: str, concession_id: uuid.UUID) -> list[Any]:
    return env.fetch(
        "SELECT organization_id, plant_id, payload FROM shared.outbox_event"
        " WHERE event_name = $1 AND payload ->> 'concession_id' = $2",
        event_name,
        str(concession_id),
    )


def _row(env: ConcessionEnvironment, concession_id: uuid.UUID) -> Any:
    rows = env.fetch(
        "SELECT * FROM identity.provider_concession WHERE concession_id = $1", concession_id
    )
    return rows[0] if rows else None


def _payload(value: Any) -> dict[str, Any]:
    return json.loads(value) if isinstance(value, str) else dict(value)


def _concession_unavailable(
    env: ConcessionEnvironment, user_id: uuid.UUID, concession_id: uuid.UUID
) -> ContextUnavailable:
    with pytest.raises(ContextUnavailable) as raised:
        env.session_context(env.provider_organization_id, user_id, concession_id)
    return raised.value


# --- grant ---------------------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("level", [ScopeLevel.ORGANIZATION, ScopeLevel.PLANT])
def test_grant_writes_row_record_and_event_and_is_in_force_at_once(
    env: ConcessionEnvironment, site: Site, installer: uuid.UUID, level: ScopeLevel
) -> None:
    plant = next(iter(site.plants))
    concession = _grant(env, installer, site, level=level, plant=plant)
    expected_plant = plant if level is ScopeLevel.PLANT else None
    row = _row(env, concession.concession_id)
    assert row["status"] == "active" and row["provider_user_id"] == installer
    assert row["provider_organization_id"] == env.provider_organization_id
    assert row["reason"] == REASON
    assert row["expires_at"] - row["granted_at"] == timedelta(days=7)  # concession_default_days
    (record,) = _records(env, "provider_concession_granted", concession.concession_id)
    # BR-NUC-45: la cadena de la planta si el alcance es de planta (seguimiento de VIG-53).
    assert record["organization_id"] == site.organization_id
    assert record["plant_id"] == expected_plant
    assert record["actor_kind"] == "provider_user" and record["actor_id"] == installer
    assert record["actor_concession_id"] == concession.concession_id
    assert record["content"]["reason"] == REASON
    assert record["content"]["scope_level"] == level.value
    (event,) = _events(env, "concession_granted", concession.concession_id)
    assert event["organization_id"] == site.organization_id
    assert event["plant_id"] == expected_plant
    assert _payload(event["payload"])["granted_by"] == str(installer)
    # Entra en vigor de inmediato (respuesta 6): la siguiente petición ya tiene contexto.
    context = env.session_context(env.provider_organization_id, installer, concession.concession_id)
    assert context.organization_id == site.organization_id
    assert context.allowed_scopes == (
        AllowedScope(level, concession.scope_id, Role.PROVIDER_INSTALLER),
    )


@pytest.mark.integration
def test_operator_grants_only_for_itself(env: ConcessionEnvironment, site: Site) -> None:
    operator = env.authz.operator_id
    concession = _grant(env, operator, site, duration=2 * HOUR)
    assert concession.provider_user_id == operator
    row = _row(env, concession.concession_id)
    assert row["provider_user_id"] == operator
    (record,) = _records(env, "provider_concession_granted", concession.concession_id)
    assert record["actor_role_in_use"] == "platform_operator"


@pytest.mark.integration
def test_no_concession_over_the_provider_nor_without_provider_user(
    env: ConcessionEnvironment, site: Site, installer: uuid.UUID
) -> None:
    """Criterio 2 (BR-NUC-42): ni sobre la proveedora ni sin un usuario del proveedor."""
    provider = env.provider_organization_id
    before = env.fetch("SELECT count(*) AS n FROM identity.provider_concession")[0]["n"]
    context = env.provider_context(installer)
    # El servicio: sobre la proveedora es not_found, como una organización inexistente.
    for client in (provider, uuid.uuid4()):
        with pytest.raises(ResourceNotFound):
            env.run(
                env.service.grant(
                    context,
                    client_organization_id=client,
                    scope_level=ScopeLevel.ORGANIZATION,
                    scope_id=client,
                    reason=REASON,
                )
            )
    # La base: sobre la proveedora, sin provider_user_id o con una «proveedora» que es cliente.
    client_user = env.client_user(site)
    insert = (
        "INSERT INTO identity.provider_concession (concession_id, organization_id,"
        " provider_user_id, provider_organization_id, scope_level, scope_id, reason,"
        " granted_at, expires_at) VALUES ($1, $2, $3, $4, 'organization', $2, $5,"
        " now(), now() + interval '1 day')"
    )
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        env.execute(insert, uuid.uuid4(), provider, installer, provider, REASON)
    with pytest.raises(asyncpg.exceptions.NotNullViolationError):
        env.execute(insert, uuid.uuid4(), site.organization_id, None, provider, REASON)
    other = env.authz.add_site(plants=1, zones_per_plant=1)
    with pytest.raises(asyncpg.exceptions.CheckViolationError) as raised:
        env.execute(
            insert, uuid.uuid4(), other.organization_id, client_user, site.organization_id, REASON
        )
    assert raised.value.constraint_name == "provider_concession_provider_kind"
    after = env.fetch("SELECT count(*) AS n FROM identity.provider_concession")[0]["n"]
    assert after == before


@pytest.mark.integration
def test_only_a_provider_session_with_the_key_grants(
    env: ConcessionEnvironment, site: Site, installer: uuid.UUID
) -> None:
    arguments: dict[str, Any] = {
        "client_organization_id": site.organization_id,
        "scope_level": ScopeLevel.ORGANIZATION,
        "scope_id": site.organization_id,
        "reason": REASON,
    }
    administrator = env.client_user(site)
    under_concession = env.session_context(
        env.provider_organization_id, installer, _grant(env, installer, site).concession_id
    )
    no_role = env.authz.add_user(env.provider_organization_id)
    for context in (
        env.session_context(site.organization_id, administrator),  # un cliente no se concede
        under_concession,  # desde una concesión no se concede otra
        env.provider_context(no_role),  # sin concessions.grant
    ):
        with pytest.raises(ResourceNotFound):
            env.run(env.service.grant(context, **arguments))


@pytest.mark.integration
@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"reason": "x" * 9}, ConcessionRejectionCode.REASON_INVALID),
        ({"reason": "x" * 501}, ConcessionRejectionCode.REASON_INVALID),
        ({"reason": "<b>Revisión del nodo</b>"}, ConcessionRejectionCode.REASON_INVALID),
        ({"reason": 1234567890}, ConcessionRejectionCode.REASON_INVALID),
        ({"duration": MIN_DURATION - MICRO}, ConcessionRejectionCode.DURATION_OUT_OF_RANGE),
        ({"duration": timedelta(days=30) + MICRO}, ConcessionRejectionCode.DURATION_OUT_OF_RANGE),
        ({"scope_level": ScopeLevel.ZONE}, ConcessionRejectionCode.SCOPE_INVALID),
        ({"scope_level": "planta"}, ConcessionRejectionCode.SCOPE_INVALID),
        ({"scope_id": uuid.uuid4()}, ConcessionRejectionCode.SCOPE_INVALID),
    ],
)
def test_grant_rejections_leave_nothing(
    env: ConcessionEnvironment,
    site: Site,
    installer: uuid.UUID,
    changes: dict[str, Any],
    code: ConcessionRejectionCode,
) -> None:
    arguments: dict[str, Any] = {
        "client_organization_id": site.organization_id,
        "scope_level": ScopeLevel.ORGANIZATION,
        "scope_id": site.organization_id,
        "reason": REASON,
    }
    arguments.update(changes)
    before = env.fetch("SELECT count(*) AS n FROM identity.provider_concession")[0]["n"]
    with pytest.raises(ConcessionRejected) as raised:
        env.run(env.service.grant(env.provider_context(installer), **arguments))
    assert raised.value.code is code
    after = env.fetch("SELECT count(*) AS n FROM identity.provider_concession")[0]["n"]
    assert after == before


@pytest.mark.integration
def test_reason_bounds_are_inclusive(
    env: ConcessionEnvironment, site: Site, installer: uuid.UUID
) -> None:
    for reason in ("Revisión 1", "R" * 500):
        concession = _grant(env, installer, site, reason=reason)
        assert _row(env, concession.concession_id)["reason"] == reason


@pytest.mark.integration
def test_grant_on_a_missing_target_is_not_found_and_leaves_nothing(
    env: ConcessionEnvironment, installer: uuid.UUID
) -> None:
    target = env.authz.add_site(plants=1, zones_per_plant=1)
    other = env.authz.add_site(plants=1, zones_per_plant=1)
    foreign_plant = next(iter(other.plants))
    context = env.provider_context(installer)
    before = env.fetch("SELECT count(*) AS n FROM identity.provider_concession")[0]["n"]
    records = env.fetch("SELECT count(*) AS n FROM ledger.ledger_record")[0]["n"]
    # La planta de otra organización: la base la rechaza dentro de la transacción del registro.
    with pytest.raises(ResourceNotFound):
        env.run(
            env.service.grant(
                context,
                client_organization_id=target.organization_id,
                scope_level=ScopeLevel.PLANT,
                scope_id=foreign_plant,
                reason=REASON,
            )
        )
    # Un cliente suspendido no tiene términos.
    env.authz.set_organization_status(target.organization_id, "suspended")
    try:
        with pytest.raises(ResourceNotFound):
            env.run(
                env.service.grant(
                    context,
                    client_organization_id=target.organization_id,
                    scope_level=ScopeLevel.ORGANIZATION,
                    scope_id=target.organization_id,
                    reason=REASON,
                )
            )
    finally:
        env.authz.set_organization_status(target.organization_id, "active")
    assert env.fetch("SELECT count(*) AS n FROM identity.provider_concession")[0]["n"] == before
    assert env.fetch("SELECT count(*) AS n FROM ledger.ledger_record")[0]["n"] == records


@st.composite
def database_duration_cases(draw: st.DrawFn) -> tuple[int, int, timedelta | None]:
    max_days = draw(st.sampled_from([1, 2, 7, 30, 90]))
    default_days = draw(st.integers(1, max_days))
    top = timedelta(days=max_days)
    requested = draw(
        st.sampled_from(
            [None, MIN_DURATION - MICRO, MIN_DURATION, top - HOUR, top, top + MICRO, top + DAY]
        )
    )
    return max_days, default_days, requested


@pytest.fixture(scope="module")
def limits_site(env: ConcessionEnvironment) -> Site:
    return env.authz.add_site(plants=1, zones_per_plant=1)


@pytest.mark.integration
@given(case=database_duration_cases())
def test_pr_nuc_12_grant_in_the_database(
    env: ConcessionEnvironment,
    limits_site: Site,
    installer: uuid.UUID,
    case: tuple[int, int, timedelta | None],
) -> None:
    max_days, default_days, requested = case
    env.set_limits(limits_site.organization_id, max_days, default_days)
    inside = requested is None or MIN_DURATION <= requested <= timedelta(days=max_days)
    if not inside:
        with pytest.raises(ConcessionRejected) as raised:
            _grant(env, installer, limits_site, duration=requested)
        assert raised.value.code is ConcessionRejectionCode.DURATION_OUT_OF_RANGE
        return
    concession = _grant(env, installer, limits_site, duration=requested)
    row = _row(env, concession.concession_id)
    expected = timedelta(days=default_days) if requested is None else requested
    # Las marcas van a milisegundos, como en el registro: a lo sumo 1 ms menos que lo pedido.
    assert (
        timedelta(0)
        <= expected - (row["expires_at"] - row["granted_at"])
        < timedelta(milliseconds=1)
    )
    assert MIN_DURATION <= row["expires_at"] - row["granted_at"] <= timedelta(days=max_days)


@pytest.mark.integration
def test_the_database_enforces_the_client_maximum(
    env: ConcessionEnvironment, installer: uuid.UUID
) -> None:
    """Restricción entre filas (revisión de VIG-38): el tope es el del cliente, no 90 días."""
    target = env.authz.add_site(plants=1, zones_per_plant=1)
    env.set_limits(target.organization_id, 3, 1)
    insert = (
        "INSERT INTO identity.provider_concession (concession_id, organization_id,"
        " provider_user_id, provider_organization_id, scope_level, scope_id, reason,"
        " granted_at, expires_at) VALUES ($1, $2, $3, $4, 'organization', $2, $5,"
        " now(), now() + $6::interval)"
    )
    arguments = (target.organization_id, installer, env.provider_organization_id, REASON)
    env.execute(insert, uuid.uuid4(), *arguments, timedelta(days=3))
    with pytest.raises(asyncpg.exceptions.CheckViolationError) as raised:
        env.execute(insert, uuid.uuid4(), *arguments, timedelta(days=3, microseconds=1))
    assert raised.value.constraint_name == "provider_concession_max_days"


# --- revoke --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_after_client_revocation_the_next_provider_request_is_not_found(
    env: ConcessionEnvironment, site: Site, installer: uuid.UUID
) -> None:
    """Criterio 3 (BR-NUC-39): revoca el cliente; la siguiente petición es ``not_found``."""
    concession = _grant(env, installer, site)
    env.session_context(env.provider_organization_id, installer, concession.concession_id)
    administrator = env.client_user(site)
    client = env.session_context(site.organization_id, administrator)
    revoked: Concession = env.run(env.service.revoke(client, concession.concession_id))
    assert revoked.status is ConcessionStatus.REVOKED
    assert revoked.revoked_by_side is RevokedBySide.CLIENT
    unavailable = _concession_unavailable(env, installer, concession.concession_id)
    assert unavailable.reason is ContextUnavailableReason.CONCESSION_INVALID
    assert translate(unavailable).code is ApiErrorCode.NOT_FOUND
    # Las sesiones del proveedor siguen vivas para su propia organización.
    assert env.provider_context(installer).organization_id == env.provider_organization_id
    row = _row(env, concession.concession_id)
    assert (row["status"], row["revoked_by"], row["revoked_by_side"]) == (
        "revoked",
        administrator,
        "client",
    )
    (record,) = _records(env, "provider_concession_revoked", concession.concession_id)
    assert record["content"]["revoked_by_side"] == "client"
    assert record["actor_id"] == administrator and record["actor_concession_id"] is None
    assert len(_events(env, "concession_revoked", concession.concession_id)) == 1
    # Una concesión cerrada no se revoca otra vez.
    with pytest.raises(ConcessionRejected) as raised:
        env.run(env.service.revoke(client, concession.concession_id))
    assert raised.value.code is ConcessionRejectionCode.CONCESSION_CLOSED


@pytest.mark.integration
@pytest.mark.parametrize("who", ["grantee", "operator"])
def test_after_provider_revocation_the_next_provider_request_is_not_found(
    env: ConcessionEnvironment, site: Site, who: str
) -> None:
    grantee = env.provider_user()
    concession = _grant(env, grantee, site, level=ScopeLevel.PLANT, plant=next(iter(site.plants)))
    actor = grantee if who == "grantee" else env.authz.operator_id
    revoked: Concession = env.run(
        env.service.revoke(env.provider_context(actor), concession.concession_id)
    )
    assert revoked.revoked_by_side is RevokedBySide.PROVIDER and revoked.revoked_by == actor
    unavailable = _concession_unavailable(env, grantee, concession.concession_id)
    assert translate(unavailable).code is ApiErrorCode.NOT_FOUND
    row = _row(env, concession.concession_id)
    assert (row["status"], row["revoked_by"], row["revoked_by_side"]) == (
        "revoked",
        actor,
        "provider",
    )
    (record,) = _records(env, "provider_concession_revoked", concession.concession_id)
    assert record["plant_id"] == concession.scope_id  # cadena de la planta (BR-NUC-45)
    assert record["actor_kind"] == "provider_user"
    assert record["actor_concession_id"] == concession.concession_id


@pytest.mark.integration
def test_who_may_not_revoke_gets_not_found(env: ConcessionEnvironment, site: Site) -> None:
    grantee = env.provider_user()
    plants = list(site.plants)
    concession = _grant(env, grantee, site, level=ScopeLevel.PLANT, plant=plants[0])
    other_installer = env.provider_user()
    other_site = env.authz.add_site(plants=1, zones_per_plant=1)
    contexts = [
        env.provider_context(other_installer),  # otro instalador: ni concesionario ni operador
        env.session_context(  # gerente de otra planta del mismo cliente
            site.organization_id,
            env.client_user(site, Role.PLANT_MANAGER, ScopeLevel.PLANT, plants[1]),
        ),
        env.session_context(  # coordinador SST: sin concessions.revoke
            site.organization_id, env.client_user(site, Role.COORDINATOR_SST)
        ),
        env.session_context(other_site.organization_id, env.client_user(other_site)),
        env.session_context(  # el propio concesionario bajo la concesión
            env.provider_organization_id, grantee, concession.concession_id
        ),
    ]
    for context in contexts:
        with pytest.raises(ResourceNotFound):
            env.run(env.service.revoke(context, concession.concession_id))
    assert _row(env, concession.concession_id)["status"] == "active"
    # El gerente de la planta concedida sí puede.
    manager = env.client_user(site, Role.PLANT_MANAGER, ScopeLevel.PLANT, plants[0])
    env.run(
        env.service.revoke(
            env.session_context(site.organization_id, manager), concession.concession_id
        )
    )
    assert _row(env, concession.concession_id)["status"] == "revoked"


@pytest.mark.integration
def test_an_expired_concession_is_not_revoked(
    env: ConcessionEnvironment, site: Site, installer: uuid.UUID
) -> None:
    concession = _grant(env, installer, site, duration=HOUR)
    administrator = env.session_context(site.organization_id, env.client_user(site))
    clock = env.authz.sessions.clock
    saved = clock.now()
    clock.set(concession.expires_at)
    try:
        with pytest.raises(ConcessionRejected) as raised:
            env.run(env.service.revoke(administrator, concession.concession_id))
        assert raised.value.code is ConcessionRejectionCode.CONCESSION_CLOSED
    finally:
        clock.set(saved)
    assert _row(env, concession.concession_id)["status"] == "active"
    assert not _records(env, "provider_concession_revoked", concession.concession_id)


# --- PR-NUC-10 con el servicio -------------------------------------------------------------------


@st.composite
def service_timelines(draw: st.DrawFn) -> dict[str, Any]:
    duration = draw(st.sampled_from([HOUR, HOUR + timedelta(seconds=1), DAY, 7 * DAY]))
    seconds = int(duration.total_seconds())
    revoke_after = draw(st.one_of(st.none(), st.integers(0, seconds - 1)))
    edges = [0, 1, seconds - 1, seconds, seconds + 1]
    if revoke_after is not None:
        edges += [revoke_after, revoke_after + 1]
    offset = draw(st.one_of(st.sampled_from(edges), st.integers(0, seconds + 3600)))
    level = draw(st.sampled_from([ScopeLevel.ORGANIZATION, ScopeLevel.PLANT]))
    return {
        "duration": duration,
        "revoke_after": revoke_after,
        "offset": offset,
        "level": level,
    }


@pytest.mark.integration
@given(timeline=service_timelines())
def test_pr_nuc_10_context_follows_grant_and_revocation(
    env: ConcessionEnvironment, site: Site, installer: uuid.UUID, timeline: dict[str, Any]
) -> None:
    clock = env.authz.sessions.clock
    start = clock.now()
    plant = next(iter(site.plants))
    concession = _grant(
        env, installer, site, level=timeline["level"], plant=plant, duration=timeline["duration"]
    )
    administrator = env.client_user(site)
    t = concession.granted_at + timedelta(seconds=timeline["offset"])
    # En orden cronológico: la revocación (si la hay) y la petición en t; a igual instante, la
    # revocación va primero (t >= revoked_at ya no tiene contexto).
    steps: list[tuple[timedelta, str]] = [(timedelta(seconds=timeline["offset"]), "request")]
    if timeline["revoke_after"] is not None:
        steps.append((timedelta(seconds=timeline["revoke_after"]), "revoke"))
    steps.sort(key=lambda step: (step[0], step[1] != "revoke"))
    revoked_at = None
    try:
        for offset, step in steps:
            clock.set(concession.granted_at + offset)
            if step == "revoke":
                context = env.session_context(site.organization_id, administrator)
                revoked: Concession = env.run(env.service.revoke(context, concession.concession_id))
                revoked_at = revoked.revoked_at
                continue
            must_fail = t >= concession.expires_at or (revoked_at is not None and t >= revoked_at)
            if must_fail:
                unavailable = _concession_unavailable(env, installer, concession.concession_id)
                assert unavailable.reason is ContextUnavailableReason.CONCESSION_INVALID
                continue
            context = env.session_context(
                env.provider_organization_id, installer, concession.concession_id
            )
            assert context.organization_id == site.organization_id
            assert context.allowed_scopes == (
                AllowedScope(timeline["level"], concession.scope_id, Role.PROVIDER_INSTALLER),
            )
        if revoked_at is not None:
            # Después de revocar, ningún instante posterior vuelve a tener contexto.
            clock.set(max(t, revoked_at))
            unavailable = _concession_unavailable(env, installer, concession.concession_id)
            assert unavailable.reason is ContextUnavailableReason.CONCESSION_INVALID
    finally:
        clock.set(start)


# --- PR-NUC-11 -----------------------------------------------------------------------------------

REQUESTS = st.lists(
    st.tuples(
        st.sampled_from(["read", "write", "health"]),
        st.sampled_from(["GET", "POST", "PUT", "PATCH", "DELETE"]),
        st.sampled_from(["/api/v1/zones/{zone_id}", "/api/v1/fleet", "/api/v1/catalog"]),
    ),
    max_size=8,
)


@pytest.mark.integration
@given(
    requests=REQUESTS,
    level=st.sampled_from([ScopeLevel.ORGANIZATION, ScopeLevel.PLANT]),
    page=st.integers(1, 3),
)
def test_pr_nuc_11_one_provider_query_per_data_request_in_its_chain(
    env: ConcessionEnvironment,
    site: Site,
    installer: uuid.UUID,
    requests: list[tuple[str, str, str]],
    level: ScopeLevel,
    page: int,
) -> None:
    plant = next(iter(site.plants))
    concession = _grant(env, installer, site, level=level, plant=plant)
    context = env.session_context(env.provider_organization_id, installer, concession.concession_id)
    expected: list[tuple[str, str, str]] = []
    for operation, method, route in requests:
        if operation == "health":
            continue  # la cadena de middleware no la cuenta (BR-NUC-38): nunca llega aquí
        assert env.run(
            record_provider_query(
                context,
                env.provider_queries,
                operation=operation,  # type: ignore[arg-type]
                method=method,
                route_template=route,
                occurred_at=env.now(),
            )
        )
        expected.append((operation, method, route))
    rows = env.fetch(
        "SELECT plant_id, ledger.vigia_bytes_to_jsonb(content) AS content"
        " FROM ledger.ledger_record WHERE record_type = 'provider_query'"
        " AND actor_concession_id = $1 ORDER BY received_at, record_id",
        concession.concession_id,
    )
    assert len(rows) == len(expected)
    chain_plant = plant if level is ScopeLevel.PLANT else None
    assert all(row["plant_id"] == chain_plant for row in rows)
    # El panel del cliente las devuelve todas, en orden, sin repetir ni omitir, página a página.
    administrator = env.session_context(site.organization_id, env.client_user(site))
    seen: list[tuple[str, str, str]] = []
    cursor: ProviderQueryCursor | None = None
    while True:
        result = env.run(
            env.service.list_provider_queries(
                administrator, concession.concession_id, after=cursor, limit=page
            )
        )
        assert len(result.items) <= page
        seen += [(item.operation, item.method, item.resource) for item in result.items]
        assert all(item.reason == REASON for item in result.items)
        cursor = result.next_cursor
        if cursor is None:
            break
    assert seen == expected


# --- expire_concessions --------------------------------------------------------------------------


@pytest.mark.integration
def test_expire_concessions_leaves_a_record_once_and_never_early(
    env: ConcessionEnvironment, installer: uuid.UUID
) -> None:
    target = env.authz.add_site(plants=1, zones_per_plant=1)
    plant = next(iter(target.plants))
    now = env.now()
    due = env.authz.add_concession(
        target.organization_id,
        installer,
        level=ScopeLevel.PLANT,
        scope_id=plant,
        granted_at=now - 2 * DAY,
        duration=DAY,
    )
    revoked = env.authz.add_concession(
        target.organization_id,
        installer,
        granted_at=now - 2 * DAY,
        duration=DAY,
        status="revoked",
        revoked_at=now - 2 * DAY + HOUR,
    )
    in_force = _grant(env, installer, target, duration=HOUR)
    assert env.expire_due(target.organization_id) == 1
    assert _row(env, due)["status"] == "expired"
    assert _row(env, revoked)["status"] == "revoked"
    assert _row(env, in_force.concession_id)["status"] == "active"
    (record,) = _records(env, "provider_concession_expired", due)
    assert record["plant_id"] == plant and record["actor_kind"] == "system"
    assert len(_events(env, "concession_expired", due)) == 1
    # Una segunda pasada no escribe nada; en la proveedora no hace nada.
    assert env.expire_due(target.organization_id) == 0
    assert env.expire_due(env.provider_organization_id) == 0
    assert len(_records(env, "provider_concession_expired", due)) == 1
    # Aunque el reloj de la aplicación se adelante, la base no da por vencida la vigente.
    clock = env.authz.sessions.clock
    saved = clock.now()
    clock.set(in_force.expires_at + DAY)
    try:
        assert env.expire_due(target.organization_id) == 0
    finally:
        clock.set(saved)
    assert _row(env, in_force.concession_id)["status"] == "active"
    assert not _records(env, "provider_concession_expired", in_force.concession_id)


# --- Panel del cliente ---------------------------------------------------------------------------


@pytest.mark.integration
def test_client_panel_shows_full_history_and_is_audited(
    env: ConcessionEnvironment, installer: uuid.UUID
) -> None:
    target = env.authz.add_site(plants=2, zones_per_plant=1)
    plants = list(target.plants)
    now = env.now()
    expired = env.authz.add_concession(
        target.organization_id, installer, granted_at=now - 3 * DAY, duration=DAY
    )
    on_plant = _grant(env, installer, target, level=ScopeLevel.PLANT, plant=plants[0])
    on_other_plant = _grant(env, installer, target, level=ScopeLevel.PLANT, plant=plants[1])
    revoked = _grant(env, installer, target)
    administrator = env.session_context(target.organization_id, env.client_user(target))
    env.run(env.service.revoke(administrator, revoked.concession_id))

    listed = env.run(env.service.list_concessions(administrator))
    statuses = {item.concession_id: item.effective_status(env.now()) for item in listed}
    assert statuses == {
        expired: ConcessionStatus.EXPIRED,  # aún sin la tarea: se muestra vencida (P5)
        on_plant.concession_id: ConcessionStatus.ACTIVE,
        on_other_plant.concession_id: ConcessionStatus.ACTIVE,
        revoked.concession_id: ConcessionStatus.REVOKED,
    }
    granted = {on_plant.concession_id, on_other_plant.concession_id, revoked.concession_id}
    assert all(item.reason == REASON for item in listed if item.concession_id in granted)
    (entry,) = env.fetch(
        "SELECT result_count, filters_json, actor_id FROM shared.audit_entry"
        " WHERE organization_id = $1 AND operation = 'ledger_read'"
        " AND filters_json ->> 'view' = 'provider_concessions'",
        target.organization_id,
    )
    assert entry["result_count"] == 4
    # El gerente de una planta ve las de su planta y las de toda la organización.
    manager = env.session_context(
        target.organization_id,
        env.client_user(target, Role.PLANT_MANAGER, ScopeLevel.PLANT, plants[0]),
    )
    by_plant = env.run(env.service.list_concessions(manager, plant_id=plants[0]))
    assert {item.concession_id for item in by_plant} == {
        expired,
        on_plant.concession_id,
        revoked.concession_id,
    }
    with pytest.raises(ResourceNotFound):
        env.run(env.service.list_concessions(manager))
    with pytest.raises(ResourceNotFound):
        env.run(env.service.list_concessions(manager, plant_id=plants[1]))
    # Ni el proveedor bajo concesión ni el instalador en su organización leen el panel.
    under = env.session_context(env.provider_organization_id, installer, on_plant.concession_id)
    for context in (under, env.provider_context(installer)):
        with pytest.raises(ResourceNotFound):
            env.run(env.service.list_concessions(context))
        with pytest.raises(ResourceNotFound):
            env.run(env.service.list_provider_queries(context, on_plant.concession_id))


@pytest.mark.integration
def test_provider_queries_are_audited_and_scoped(
    env: ConcessionEnvironment, installer: uuid.UUID
) -> None:
    target = env.authz.add_site(plants=2, zones_per_plant=1)
    plants = list(target.plants)
    concession = _grant(env, installer, target, level=ScopeLevel.PLANT, plant=plants[0])
    context = env.session_context(env.provider_organization_id, installer, concession.concession_id)
    env.run(
        record_provider_query(
            context,
            env.provider_queries,
            operation="read",
            method="GET",
            route_template="/api/v1/fleet",
            occurred_at=env.now(),
        )
    )
    manager = env.session_context(
        target.organization_id,
        env.client_user(target, Role.PLANT_MANAGER, ScopeLevel.PLANT, plants[0]),
    )
    page = env.run(env.service.list_provider_queries(manager, concession.concession_id))
    (item,) = page.items
    assert (item.operation, item.method, item.resource) == ("read", "GET", "/api/v1/fleet")
    assert item.provider_user_id == installer and item.plant_id == plants[0]
    (entry,) = env.fetch(
        "SELECT result_count, resource_kind, resource_id FROM shared.audit_entry"
        " WHERE organization_id = $1 AND operation = 'ledger_read'"
        " AND filters_json ->> 'view' = 'provider_queries'",
        target.organization_id,
    )
    assert (entry["result_count"], entry["resource_kind"], entry["resource_id"]) == (
        1,
        "provider_concession",
        concession.concession_id,
    )
    other = env.session_context(
        target.organization_id,
        env.client_user(target, Role.PLANT_MANAGER, ScopeLevel.PLANT, plants[1]),
    )
    for wrong in (
        other,
        env.session_context(target.organization_id, env.authz.add_user(target.organization_id)),
    ):
        with pytest.raises(ResourceNotFound):
            env.run(env.service.list_provider_queries(wrong, concession.concession_id))
    with pytest.raises(ResourceNotFound):
        env.run(env.service.list_provider_queries(manager, uuid.uuid4()))
    for limit in (0, 201, True):
        with pytest.raises(ValueError):
            env.run(
                env.service.list_provider_queries(manager, concession.concession_id, limit=limit)
            )
