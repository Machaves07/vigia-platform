"""Matriz de permisos, ``authorize`` y constructores de contexto (TASK-125, LC-NUC-04).

Propiedades de ``business-logic-model.md`` §11 (C-PLA-04):

- **PR-NUC-03**: los permisos son la unión de las asignaciones; añadir una nunca quita un permiso
  y retirarla devuelve exactamente el conjunto anterior (sobre claves **y** recursos).
- **PR-NUC-05**: una asignación de planta concede sobre toda zona de esa planta (también las
  creadas después) y sobre ninguna de otra planta; una de organización, sobre todo.
- **PR-NUC-10**: con cualquier concesión y cualquier instante, ``context_from_session`` bajo esa
  concesión falla si ``t >= expires_at`` o ``t >= revoked_at`` y, si no, el alcance del contexto
  es exactamente el de la concesión (contra PostgreSQL).
- **PR-NUC-16** (``role_in_use``): la regla determinista (zona > planta > organización; a igual
  alcance, el orden de ``role``) no depende del orden de las asignaciones, y el registro que se
  escribe con el contexto autorizado guarda ese rol (contra PostgreSQL).

Criterios de aceptación: denegación → ``authorization_denied`` auditado y ``not_found`` (nunca
``forbidden``); la matriz es la de §2.6 más A-13 y no tiene clave para ninguna prohibición por
diseño; ``context_from_session`` es **una** sentencia (contando las que llegan al servidor).
Seguimiento de VIG-53 (BR-NUC-45): el escritor rechaza una planta o zona fuera del alcance de la
sesión.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import itertools
import re
import uuid
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from tests.authz_support import (
    CLIENT_ROLES,
    AuthzEnvironment,
    authz_environment,
    sealed_context,
)
from tests.identity_db import migrated_database
from tests.integration.conftest import PostgresEndpoint
from tests.writer_support import (
    ZONE_TYPE,
    Place,
    WriterEnvironment,
    fetch_record,
    writer_environment,
    zone_document,
)
from vigia_platform.identity.adapters.authz_store import SESSION_CONTEXT_STATEMENT
from vigia_platform.identity.auth.sessions import SessionCookie
from vigia_platform.identity.authz.authorize import (
    Authorizer,
    Resource,
    ResourceNotFound,
    decide,
    role_in_use,
)
from vigia_platform.identity.authz.context import (
    ContextUnavailable,
    ContextUnavailableReason,
    ScopeContexts,
    record_provider_query,
    with_role_in_use,
)
from vigia_platform.identity.authz.matrix import (
    MATRIX,
    PermissionKey,
    effective_permissions,
    is_platform_key,
    permission_key,
    roles_with,
)
from vigia_platform.ledger.application.writer import (
    LedgerRejection,
    LedgerRejectionCode,
    Receipt,
)
from vigia_platform.shared.api.errors import ApiErrorCode, translate
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import (
    ActorKind,
    ActorUnit,
    AllowedScope,
    ContextOrigin,
    Role,
    ScopeContext,
    ScopeLevel,
)

# --- Oráculo: la tabla de §2.6 con las notas de U-03 y U-04 (adenda A-13) ---------------------

ROLE_COLUMNS = (
    Role.COORDINATOR_SST,
    Role.LINE_MANAGER,
    Role.PLANT_MANAGER,
    Role.ADMINISTRATOR,
    Role.PROVIDER_INSTALLER,
    Role.COPASST,
    Role.PLATFORM_OPERATOR,
)
"""Orden de las columnas de la tabla: coord., mando, gerente, admin., instalador, COPASST, oper."""

DESIGN_TABLE: dict[str, str] = {
    "findings.read": "x.x....",
    "findings.classify": "x......",
    "review_queue.resolve": "x......",
    "actions.manage": "x......",
    "findings.close_and_sign": "x......",
    "root_cause.analyze": "x......",
    "review_package.read": "xxx....",
    "executive_summary.read": "x.x....",
    "export.create": "x.x....",
    "coverage.read": "xxxxxx.",
    "evidence.read": "x.x....",
    "labels.read": "x.x....",
    "integrity.verify": "x.xx..x",
    "catalog.manage": "...x...",
    "catalog.read": "xxxxxx.",
    "commissioning.run": "....x..",
    "fleet.read": "...xx.x",
    "fleet.manage": "....x..",
    "health.read": "...x..x",
    "live_view.open": "x..xxx.",
    "transparency.read": "xxxxxx.",
    "users.manage": "...x..x",
    "roles.manage": "...x..x",
    "hierarchy.manage": "...x...",
    "hierarchy.read": "xxxxxxx",
    "organization.settings": "..xx...",
    "concessions.read": "..xx..x",
    "concessions.revoke": "..xx..x",
    "concessions.grant": "....x.x",
    "audit.read": "..xx..x",
    "notifications.read": "xxxxxxx",
    "platform.organizations.create": "......x",
    "platform.keys.rotate": "......x",
    "platform.dead_letter.replay": "......x",
    # Nota fechada de U-03 y A-13.
    "agreements.sign": "x.xxx..",
    # Notas fechadas de U-04 y A-13.
    "metrics.read": "x.x....",
    "exposure.read": "x.xx...",
    "commitments.write": "xx.....",
    "vocabulary.manage": "...x...",
    "closure_attachments.read": "x.x....",
}


def test_matrix_is_the_design_table() -> None:
    assert len(DESIGN_TABLE) == 40 == len(PermissionKey)
    assert {key.value for key in PermissionKey} == set(DESIGN_TABLE)
    for key, marks in DESIGN_TABLE.items():
        expected = {role for role, mark in zip(ROLE_COLUMNS, marks, strict=True) if mark == "x"}
        assert roles_with(PermissionKey(key)) == expected, key
    assert set(MATRIX) == set(Role)


# --- Prohibiciones por diseño (§2.6, "lo que no está en la matriz") ---------------------------

RECORD_OPERATIONS = {
    "findings.read",
    "findings.classify",
    "review_queue.resolve",
    "actions.manage",
    "findings.close_and_sign",
    "root_cause.analyze",
    "executive_summary.read",
    "export.create",
    "evidence.read",
    "labels.read",
    "integrity.verify",
    "closure_attachments.read",
    "metrics.read",
    "exposure.read",
}
"""Operaciones sobre el registro (hallazgos, evidencias, etiquetas, exportación)."""

FORBIDDEN_WORDS = re.compile(
    r"unblur|raw|video|original|delete|erase|purge|edit|update|modify|rewrite|"
    r"retrain|training|dataset|approve|approval|consent_by|productivity|performance|"
    r"cycle|count|person|identity|face|track"
)
"""Ninguna clave nombra una capacidad prohibida: video sin difuminar (P3), editar o borrar el
expediente (P4), reentrenar con etiquetas (RF-PLA-14), aprobar la transparencia del COPASST
(H-55), productividad (P7) ni identificar personas (P3)."""


def test_matrix_has_no_key_for_the_design_prohibitions() -> None:
    names = [key.value for key in PermissionKey]
    assert [name for name in names if FORBIDDEN_WORDS.search(name)] == []
    # H-54: el mando de línea no opera sobre el registro; commitments.write es su única clave
    # de U-04 y no toca hallazgos.
    line = {key.value for key in MATRIX[Role.LINE_MANAGER]}
    assert line & RECORD_OPERATIONS == set()
    assert line == {
        "review_package.read",
        "coverage.read",
        "catalog.read",
        "transparency.read",
        "hierarchy.read",
        "notifications.read",
        "commitments.write",
    }
    # RF-PLA-11: el administrador no lee hallazgos, evidencias ni adjuntos de cierre.
    admin = {key.value for key in MATRIX[Role.ADMINISTRATOR]}
    assert {
        "findings.read",
        "evidence.read",
        "closure_attachments.read",
        "labels.read",
    }.isdisjoint(admin)
    assert not any(name.startswith("findings.") for name in admin)
    # P4 y RF-PLA-14: sobre etiquetas solo existe leer; nada escribe o borra el expediente.
    assert [n for n in names if n.startswith("labels.")] == ["labels.read"]
    assert not any(n.startswith(("ledger.", "records.")) for n in names)
    # BR-NUC-37: el instalador (también bajo concesión) no lee hallazgos, evidencias,
    # exportaciones ni auditoría.
    installer = {key.value for key in MATRIX[Role.PROVIDER_INSTALLER]}
    assert {"findings.read", "evidence.read", "export.create", "audit.read"}.isdisjoint(installer)
    # U-04: sus cinco claves no son del instalador, del COPASST ni del operador.
    u04 = {
        "metrics.read",
        "exposure.read",
        "commitments.write",
        "vocabulary.manage",
        "closure_attachments.read",
    }
    for role in (Role.PROVIDER_INSTALLER, Role.COPASST, Role.PLATFORM_OPERATOR):
        assert u04.isdisjoint({key.value for key in MATRIX[role]}), role
    # H-55 y BR-NUC-17: el COPASST ve transparencia y vista en vivo por su asignación, sin
    # ninguna clave de aprobación que otro rol pueda negarle.
    assert {PermissionKey.TRANSPARENCY_READ, PermissionKey.LIVE_VIEW_OPEN} <= MATRIX[Role.COPASST]
    # platform.* solo del operador.
    for key in PermissionKey:
        if is_platform_key(key):
            assert roles_with(key) == {Role.PLATFORM_OPERATOR}


@pytest.mark.parametrize("value", ["findings.delete", "FINDINGS.READ", "", " findings.read", 3])
def test_unregistered_keys_do_not_exist(value: object) -> None:
    with pytest.raises(ValueError, match="no registrada"):
        permission_key(value)


# --- Generadores -------------------------------------------------------------------------------


@st.composite
def hierarchies(draw: st.DrawFn) -> tuple[uuid.UUID, dict[uuid.UUID, tuple[uuid.UUID, ...]]]:
    """Una organización con 1 a 3 plantas y 0 a 3 zonas por planta."""
    organization = draw(st.uuids(version=4))
    plants = draw(st.lists(st.uuids(version=4), min_size=1, max_size=3, unique=True))
    layout = {
        plant: tuple(draw(st.lists(st.uuids(version=4), max_size=3, unique=True)))
        for plant in plants
    }
    return organization, layout


def _scopes(
    organization: uuid.UUID, layout: Mapping[uuid.UUID, Sequence[uuid.UUID]]
) -> st.SearchStrategy[AllowedScope]:
    targets: list[tuple[ScopeLevel, uuid.UUID]] = [(ScopeLevel.ORGANIZATION, organization)]
    targets += [(ScopeLevel.PLANT, plant) for plant in layout]
    targets += [(ScopeLevel.ZONE, zone) for zones in layout.values() for zone in zones]
    return st.builds(
        lambda target, role: AllowedScope(target[0], target[1], role),
        st.sampled_from(targets),
        st.sampled_from(CLIENT_ROLES),
    )


def _resources(
    organization: uuid.UUID, layout: Mapping[uuid.UUID, Sequence[uuid.UUID]]
) -> st.SearchStrategy[Resource]:
    options: list[Resource] = [Resource.organization(organization)]
    for plant, zones in layout.items():
        options.append(Resource.plant(organization, plant))
        options += [Resource.zone(organization, plant, zone) for zone in zones]
    foreign = st.builds(
        lambda o, p, z: Resource.zone(o, p, z),
        st.uuids(version=4),
        st.sampled_from(list(layout)),
        st.uuids(version=4),
    )
    return st.one_of(st.sampled_from(options), foreign)


PROVIDER = uuid.UUID("00000000-0000-4000-8000-00000000c0de")
"""Organización proveedora de las propiedades puras."""


def _granted(
    organization: uuid.UUID, scopes: Sequence[AllowedScope], key: PermissionKey, resource: Resource
) -> bool:
    context = sealed_context(organization, scopes)
    return decide(context, key, resource, provider_organization_id=PROVIDER).granted


@st.composite
def authorization_cases(
    draw: st.DrawFn,
) -> tuple[uuid.UUID, list[AllowedScope], AllowedScope, PermissionKey, Resource]:
    organization, layout = draw(hierarchies())
    scopes = draw(st.lists(_scopes(organization, layout), max_size=5))
    extra = draw(_scopes(organization, layout))
    key = draw(st.sampled_from(PermissionKey))
    resource = draw(_resources(organization, layout))
    return organization, scopes, extra, key, resource


# --- PR-NUC-03 ---------------------------------------------------------------------------------


@given(case=authorization_cases())
def test_pr_nuc_03_permissions_are_the_union_of_assignments(
    case: tuple[uuid.UUID, list[AllowedScope], AllowedScope, PermissionKey, Resource],
) -> None:
    organization, scopes, extra, key, resource = case
    before = _granted(organization, scopes, key, resource)
    after = _granted(organization, [*scopes, extra], key, resource)
    # Monotonía: añadir una asignación nunca quita un permiso.
    assert after or not before
    # Inversa: retirarla devuelve exactamente lo anterior.
    assert _granted(organization, scopes, key, resource) == before
    # Unión: se concede si y solo si alguna asignación sola lo concede.
    assert after == any(
        _granted(organization, [scope], key, resource) for scope in [*scopes, extra]
    )
    assert effective_permissions([*scopes, extra]) == effective_permissions(
        scopes
    ) | effective_permissions([extra])


# --- PR-NUC-05 ---------------------------------------------------------------------------------


@given(data=st.data(), hierarchy=hierarchies(), key=st.sampled_from(PermissionKey))
def test_pr_nuc_05_plant_and_organization_scopes_cover_present_and_future_zones(
    data: st.DataObject,
    hierarchy: tuple[uuid.UUID, dict[uuid.UUID, tuple[uuid.UUID, ...]]],
    key: PermissionKey,
) -> None:
    organization, layout = hierarchy
    role = data.draw(st.sampled_from([r for r in CLIENT_ROLES if key in MATRIX[r]] or [None]))
    assume(role is not None)
    assert role is not None
    plant = data.draw(st.sampled_from(list(layout)))
    later_zone = data.draw(st.uuids(version=4))  # zona creada después de asignar
    plant_scope = [AllowedScope(ScopeLevel.PLANT, plant, role)]
    organization_scope = [AllowedScope(ScopeLevel.ORGANIZATION, organization, role)]
    for zone in (*layout[plant], later_zone):
        resource = Resource.zone(organization, plant, zone)
        assert _granted(organization, plant_scope, key, resource)
        assert _granted(organization, organization_scope, key, resource)
    for other, zones in layout.items():
        if other == plant:
            continue
        assert not _granted(organization, plant_scope, key, Resource.plant(organization, other))
        for zone in zones:
            assert not _granted(
                organization, plant_scope, key, Resource.zone(organization, other, zone)
            )
            assert _granted(
                organization, organization_scope, key, Resource.zone(organization, other, zone)
            )
    # Un recurso de organización solo lo cubre un alcance de organización.
    assert not _granted(organization, plant_scope, key, Resource.organization(organization))
    assert _granted(organization, organization_scope, key, Resource.organization(organization))
    # Una zona concede sobre ella y sobre nada más.
    zone_scope = [AllowedScope(ScopeLevel.ZONE, later_zone, role)]
    assert _granted(organization, zone_scope, key, Resource.zone(organization, plant, later_zone))
    assert not _granted(organization, zone_scope, key, Resource.plant(organization, plant))
    for zone in layout[plant]:
        assert not _granted(organization, zone_scope, key, Resource.zone(organization, plant, zone))


# --- PR-NUC-16: role_in_use determinista --------------------------------------------------------

_SPECIFICITY = {ScopeLevel.ZONE: 0, ScopeLevel.PLANT: 1, ScopeLevel.ORGANIZATION: 2}
_ORDER = list(Role)


@given(case=authorization_cases(), permutation_seed=st.randoms(use_true_random=False))
def test_pr_nuc_16_role_in_use_is_deterministic(
    case: tuple[uuid.UUID, list[AllowedScope], AllowedScope, PermissionKey, Resource],
    permutation_seed: Any,
) -> None:
    organization, scopes, extra, key, resource = case
    scopes = [*scopes, extra]
    context = sealed_context(organization, scopes)
    decision = decide(context, key, resource, provider_organization_id=PROVIDER)
    # Oráculo independiente: las que conceden, ordenadas por (especificidad, orden del rol).
    granting = [
        s
        for s in scopes
        if key in MATRIX[s.role]
        and resource.organization_id == organization
        and _granted(organization, [s], key, resource)
    ]
    expected = (
        min(granting, key=lambda s: (_SPECIFICITY[s.scope_level], _ORDER.index(s.role))).role
        if granting
        else None
    )
    assert decision.role_in_use == expected
    shuffled = list(scopes)
    permutation_seed.shuffle(shuffled)
    again = decide(
        sealed_context(organization, shuffled), key, resource, provider_organization_id=PROVIDER
    )
    assert again.role_in_use == expected
    assert role_in_use(reversed(granting)) == expected


def test_role_in_use_examples() -> None:
    organization, plant, zone = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    scopes = [
        AllowedScope(ScopeLevel.ORGANIZATION, organization, Role.COORDINATOR_SST),
        AllowedScope(ScopeLevel.PLANT, plant, Role.PLANT_MANAGER),
        AllowedScope(ScopeLevel.ZONE, zone, Role.COPASST),
    ]
    resource = Resource.zone(organization, plant, zone)
    context = sealed_context(organization, scopes)
    # La zona es la más específica aunque el COPASST vaya último en la enumeración.
    assert (
        decide(
            context, PermissionKey.COVERAGE_READ, resource, provider_organization_id=PROVIDER
        ).role_in_use
        is Role.COPASST
    )
    # Sin la clave en la zona, gana la planta.
    assert (
        decide(
            context, PermissionKey.FINDINGS_READ, resource, provider_organization_id=PROVIDER
        ).role_in_use
        is Role.PLANT_MANAGER
    )
    # A igual alcance, el primero de la enumeración.
    tie = [
        AllowedScope(ScopeLevel.PLANT, plant, Role.PLANT_MANAGER),
        AllowedScope(ScopeLevel.PLANT, plant, Role.COORDINATOR_SST),
    ]
    assert role_in_use(tie) is Role.COORDINATOR_SST
    assert role_in_use([]) is None


# --- authorize: authorization_denied y not_found --------------------------------------------------


class RecordingAudit:
    def __init__(self) -> None:
        self.denied: list[tuple[ScopeContext, PermissionKey, Resource]] = []

    async def authorization_denied(
        self, context: ScopeContext, key: PermissionKey, resource: Resource
    ) -> None:
        self.denied.append((context, key, resource))


@given(case=authorization_cases())
def test_authorize_denies_as_not_found_and_audits_or_returns_role_in_use(
    case: tuple[uuid.UUID, list[AllowedScope], AllowedScope, PermissionKey, Resource],
) -> None:
    organization, scopes, extra, key, resource = case
    audit = RecordingAudit()
    authorizer = Authorizer(audit=audit, provider_organization_id=PROVIDER)
    context = sealed_context(organization, [*scopes, extra])
    decision = decide(context, key, resource, provider_organization_id=PROVIDER)
    if decision.granted:
        authorized = asyncio.run(authorizer.authorize(context, key, resource))
        assert authorized.actor.role_in_use is decision.role_in_use
        assert authorized.organization_id == context.organization_id
        assert authorized.allowed_scopes == context.allowed_scopes
        assert authorized.correlation_id == context.correlation_id
        assert authorized.session_id_hash == context.session_id_hash
        assert authorized.actor.id == context.actor.id
        assert audit.denied == []
    else:
        with pytest.raises(ResourceNotFound) as raised:
            asyncio.run(authorizer.authorize(context, key.value, resource))
        assert audit.denied == [(context, key, resource)]
        error = translate(raised.value)
        assert error.code is ApiErrorCode.NOT_FOUND
        assert error.code is not ApiErrorCode.FORBIDDEN


def test_denied_and_absent_resources_answer_exactly_the_same() -> None:
    organization, plant = uuid.uuid4(), uuid.uuid4()
    context = sealed_context(organization, [AllowedScope(ScopeLevel.PLANT, plant, Role.COPASST)])
    authorizer = Authorizer(audit=RecordingAudit(), provider_organization_id=PROVIDER)
    responses = []
    for key, resource in (
        (PermissionKey.FINDINGS_READ, Resource.plant(organization, plant)),  # sin la clave
        (PermissionKey.COVERAGE_READ, Resource.plant(organization, uuid.uuid4())),  # otra planta
        (PermissionKey.COVERAGE_READ, Resource.plant(uuid.uuid4(), plant)),  # otra organización
    ):
        with pytest.raises(ResourceNotFound) as raised:
            asyncio.run(authorizer.authorize(context, key, resource))
        responses.append((str(raised.value), translate(raised.value).code))
    assert len(set(responses)) == 1
    assert responses[0][1] is ApiErrorCode.NOT_FOUND


def test_owner_restricted_resource() -> None:
    organization = uuid.uuid4()
    context = sealed_context(
        organization,
        [AllowedScope(ScopeLevel.ORGANIZATION, organization, Role.LINE_MANAGER)],
    )
    own = Resource(
        organization, "notification_preferences", uuid.uuid4(), owner_id=context.actor.id
    )
    other = Resource(organization, "notification_preferences", uuid.uuid4(), owner_id=uuid.uuid4())
    key = PermissionKey.NOTIFICATIONS_READ
    assert decide(context, key, own, provider_organization_id=PROVIDER).granted
    assert not decide(context, key, other, provider_organization_id=PROVIDER).granted


def test_provider_and_platform_rules() -> None:
    client = uuid.uuid4()
    plant = uuid.uuid4()
    # platform.* solo en la proveedora sin concesión.
    operator = sealed_context(
        PROVIDER, [AllowedScope(ScopeLevel.ORGANIZATION, PROVIDER, Role.PLATFORM_OPERATOR)]
    )
    for key in PermissionKey:
        granted = decide(
            operator, key, Resource.organization(PROVIDER), provider_organization_id=PROVIDER
        ).granted
        assert granted == (Role.PLATFORM_OPERATOR in roles_with(key)), key
    # Un operador o un instalador en una organización cliente no conceden nada.
    for role in (Role.PLATFORM_OPERATOR, Role.PROVIDER_INSTALLER):
        misplaced = sealed_context(client, [AllowedScope(ScopeLevel.ORGANIZATION, client, role)])
        for key in PermissionKey:
            assert not decide(
                misplaced, key, Resource.organization(client), provider_organization_id=PROVIDER
            ).granted
    # Un rol de cliente en la proveedora tampoco.
    stray = sealed_context(
        PROVIDER, [AllowedScope(ScopeLevel.ORGANIZATION, PROVIDER, Role.ADMINISTRATOR)]
    )
    assert not decide(
        stray,
        PermissionKey.USERS_MANAGE,
        Resource.organization(PROVIDER),
        provider_organization_id=PROVIDER,
    ).granted
    # Bajo concesión: exactamente la columna del instalador, sobre el alcance concedido, y
    # ninguna clave platform.*.
    concession = sealed_context(
        client,
        [AllowedScope(ScopeLevel.PLANT, plant, Role.PROVIDER_INSTALLER)],
        kind=ActorKind.PROVIDER_USER,
        concession_id=uuid.uuid4(),
    )
    zone = Resource.zone(client, plant, uuid.uuid4())
    for key in PermissionKey:
        granted = decide(concession, key, zone, provider_organization_id=PROVIDER).granted
        assert granted == (key in MATRIX[Role.PROVIDER_INSTALLER]), key
        assert not decide(
            concession, key, Resource.plant(client, uuid.uuid4()), provider_organization_id=PROVIDER
        ).granted
    # Contextos del sistema (evento, iteración): sin asignaciones, nunca se concede.
    system = sealed_context(client, [], kind=ActorKind.SYSTEM, origin=ContextOrigin.OUTBOX_EVENT)
    assert not any(
        decide(
            system, key, Resource.organization(client), provider_organization_id=PROVIDER
        ).granted
        for key in PermissionKey
    )


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        ({"kind": "Zona"}, ValueError),
        ({"kind": ""}, ValueError),
        ({"kind": "a" * 65}, ValueError),
        ({"kind": "zone", "zone_id": uuid.uuid4()}, ValueError),  # zona sin planta
        ({"kind": "zone", "id": str(uuid.uuid4())}, TypeError),
        ({"kind": "zone", "plant_id": "p"}, TypeError),
    ],
)
def test_resource_is_validated(arguments: dict[str, Any], error: type[Exception]) -> None:
    fields: dict[str, Any] = {"organization_id": uuid.uuid4(), "kind": "zone", "id": uuid.uuid4()}
    fields.update(arguments)
    with pytest.raises(error):
        Resource(**fields)


def test_with_role_in_use_only_takes_a_role_of_the_context() -> None:
    organization = uuid.uuid4()
    context = sealed_context(
        organization, [AllowedScope(ScopeLevel.ORGANIZATION, organization, Role.COPASST)]
    )
    assert with_role_in_use(context, Role.COPASST).actor.role_in_use is Role.COPASST
    with pytest.raises(ValueError, match="role_in_use"):
        with_role_in_use(context, Role.ADMINISTRATOR)


# --- Constructores sin base: evento, iteración, inicio de sesión, provider_query ----------------


class _NoStore:
    async def session_row(self, *args: Any) -> Any:
        raise AssertionError("no debe consultar")

    async def operator_row(self, *args: Any) -> Any:
        raise AssertionError("no debe consultar")


def _contexts() -> ScopeContexts:
    from tests.session_support import START

    return ScopeContexts(
        store=_NoStore(),
        clock=SimulatedClock(START),
        provider_organization_id=PROVIDER,
        system_actor_id=uuid.uuid4(),
    )


class _Event:
    def __init__(self, organization_id: uuid.UUID, correlation_id: uuid.UUID) -> None:
        self.organization_id = organization_id
        self.correlation_id = correlation_id


class _Task:
    task_name = "expire_concessions"
    unit = ActorUnit.U03


def test_system_constructors() -> None:
    from tests.factories import uuid7

    contexts = _contexts()
    organization, correlation = uuid.uuid4(), uuid7()
    event = contexts.context_from_event(_Event(organization, correlation), unit=ActorUnit.U04)
    assert (event.organization_id, event.correlation_id) == (organization, correlation)
    assert event.origin is ContextOrigin.OUTBOX_EVENT and event.allowed_scopes == ()
    assert event.actor.kind is ActorKind.SYSTEM and event.actor.unit is ActorUnit.U04
    periodic = contexts.context_for_organization(_Task(), organization)
    assert periodic.origin is ContextOrigin.PERIODIC_ITERATION
    assert periodic.actor.unit is ActorUnit.U03 and periodic.correlation_id.version == 7
    # Un evento con correlación que no es v7 no construye contexto.
    with pytest.raises(ValueError, match="v7"):
        contexts.context_from_event(_Event(organization, uuid.uuid4()))
    anonymous = contexts.anonymous(organization)
    assert anonymous.actor.kind is ActorKind.SYSTEM and anonymous.allowed_scopes == ()
    user = contexts.for_user(organization, uuid.uuid4(), "Persona sintética", "a" * 64)
    assert user.origin is ContextOrigin.SESSION and user.allowed_scopes == ()
    assert contexts.provider_audit_context().organization_id == PROVIDER


class _RowStore:
    """Almacén que devuelve una fila fija: prueba la capa de Python sin la de SQL."""

    def __init__(self, row: Any) -> None:
        self.row = row

    async def session_row(self, *args: Any) -> Any:
        return self.row

    async def operator_row(self, *args: Any) -> Any:
        return None


def test_concession_conditions_are_checked_in_python_too() -> None:
    """Defensa en profundidad: aunque la sentencia devolviera la concesión, el constructor la
    rechaza si ya venció, es de zona, es de la proveedora, no es la pedida o el usuario no puede
    concederse concesiones (PR-NUC-10, BR-NUC-04, 37, 40)."""
    from tests.session_support import START
    from vigia_platform.identity.auth.sessions import new_session_cookie
    from vigia_platform.identity.authz.context import ConcessionRow, SessionRow

    client, concession_id, user = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    installer = AllowedScope(ScopeLevel.ORGANIZATION, PROVIDER, Role.PROVIDER_INSTALLER)

    def row(**changes: Any) -> SessionRow:
        concession = ConcessionRow(
            concession_id=changes.pop("concession_id", concession_id),
            organization_id=changes.pop("client", client),
            scope_level=changes.pop("level", ScopeLevel.ORGANIZATION),
            scope_id=client,
            expires_at=changes.pop("expires_at", START + timedelta(hours=1)),
        )
        fields: dict[str, Any] = {
            "user_id": user,
            "organization_id": PROVIDER,
            "organization_kind": "provider",
            "display_name": "Instalador sintético",
            "privacy_notice_version_accepted": None,
            "assignments": (installer,),
            "concession": concession,
        }
        fields.update(changes)
        return SessionRow(**fields)

    def build(session_row: SessionRow) -> Any:
        contexts = ScopeContexts(
            store=_RowStore(session_row),
            clock=SimulatedClock(START),
            provider_organization_id=PROVIDER,
            system_actor_id=uuid.uuid4(),
        )
        cookie = new_session_cookie(PROVIDER)
        return asyncio.run(contexts.context_from_session(cookie, concession_id=concession_id))

    assert build(row()).context.organization_id == client
    for bad in (
        {"expires_at": START},  # en el instante exacto ya venció
        {"expires_at": START - timedelta(milliseconds=1)},
        {"level": ScopeLevel.ZONE},
        {"client": PROVIDER},
        {"concession_id": uuid.uuid4()},
        {"assignments": (AllowedScope(ScopeLevel.ORGANIZATION, PROVIDER, Role.COPASST),)},
        {"assignments": ()},
        {"organization_kind": "client"},
        {"concession": None},
    ):
        with pytest.raises(ContextUnavailable) as raised:
            build(row(**bad))
        assert raised.value.reason is ContextUnavailableReason.CONCESSION_INVALID, bad


def test_session_constructor_without_cookie_is_unavailable() -> None:
    contexts = _contexts()
    for cookie in (None, "cookie", object()):
        with pytest.raises(ContextUnavailable) as raised:
            asyncio.run(contexts.context_from_session(cookie))  # type: ignore[arg-type]
        assert raised.value.reason is ContextUnavailableReason.SESSION_INVALID
        assert translate(raised.value).code is ApiErrorCode.UNAUTHENTICATED
    concession_error = ContextUnavailable(ContextUnavailableReason.CONCESSION_INVALID)
    assert translate(concession_error).code is ApiErrorCode.NOT_FOUND


class _Ledger:
    def __init__(self) -> None:
        self.writes: list[tuple[ScopeContext, dict[str, str], uuid.UUID | None]] = []

    async def write_provider_query(
        self, context: ScopeContext, content: Mapping[str, str], plant_id: uuid.UUID | None
    ) -> None:
        self.writes.append((context, dict(content), plant_id))


def test_provider_query_only_under_concession_and_in_the_concession_chain() -> None:
    from tests.session_support import START

    client, plant, concession_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ledger = _Ledger()
    arguments: dict[str, Any] = {
        "operation": "read",
        "method": "GET",
        "route_template": "/api/v1/zones/{zone_id}/coverage",
        "occurred_at": START,
    }
    plain = sealed_context(client, [AllowedScope(ScopeLevel.ORGANIZATION, client, Role.COPASST)])
    assert not asyncio.run(record_provider_query(plain, ledger, **arguments))
    for level, scope_id, expected_plant in (
        (ScopeLevel.PLANT, plant, plant),
        (ScopeLevel.ORGANIZATION, client, None),
    ):
        context = sealed_context(
            client,
            [AllowedScope(level, scope_id, Role.PROVIDER_INSTALLER)],
            kind=ActorKind.PROVIDER_USER,
            concession_id=concession_id,
        )
        assert asyncio.run(record_provider_query(context, ledger, **arguments))
        _, content, chain_plant = ledger.writes[-1]
        assert chain_plant == expected_plant
        assert content == {
            "concession_id": str(concession_id),
            "operation": "read",
            "method": "GET",
            "resource": "/api/v1/zones/{zone_id}/coverage",
            "occurred_at": "2026-09-30T09:00:00.000Z",
        }
    assert len(ledger.writes) == 2
    for bad in (
        {"method": "TRACE"},
        {"operation": "delete"},
        {"route_template": "https://x/y"},
        {"route_template": "/zones/123?a=b"},
    ):
        with pytest.raises(ValueError):
            asyncio.run(record_provider_query(context, ledger, **{**arguments, **bad}))


# --- Contra PostgreSQL: contexto desde la sesión ------------------------------------------------


@pytest.fixture(scope="module")
def environment(postgres_endpoint: PostgresEndpoint) -> Iterator[AuthzEnvironment]:
    with authz_environment(postgres_endpoint, "authz") as env:
        yield env


def _session_scope(
    env: AuthzEnvironment, cookie: SessionCookie, concession_id: uuid.UUID | None = None
) -> Any:
    return env.run(env.contexts.context_from_session(cookie, concession_id=concession_id))


def _assert_single_context_statement(env: AuthzEnvironment) -> None:
    """Exactamente una sentencia de datos, la de ``SESSION_CONTEXT_STATEMENT`` (con ``$n``)."""
    statements = env.log.data_statements()
    assert len(statements) == 1, statements
    expected = re.sub(r":\w+", "", str(SESSION_CONTEXT_STATEMENT))
    assert re.sub(r"\$\d+", "", statements[0]) == expected


def _unavailable(
    env: AuthzEnvironment, cookie: SessionCookie, concession_id: uuid.UUID | None = None
) -> ContextUnavailableReason:
    with pytest.raises(ContextUnavailable) as raised:
        _session_scope(env, cookie, concession_id)
    return raised.value.reason


@pytest.mark.integration
def test_session_context_is_one_statement_with_assignments(environment: AuthzEnvironment) -> None:
    env = environment
    site = env.add_site()
    user = env.add_user(site.organization_id)
    plant, zones = next(iter(site.plants.items()))
    env.assign(site.organization_id, user, Role.COORDINATOR_SST, ScopeLevel.PLANT, plant)
    env.assign(site.organization_id, user, Role.COPASST, ScopeLevel.ZONE, zones[0])
    removed = env.assign(site.organization_id, user, Role.PLANT_MANAGER)
    env.remove_assignment(removed)
    env.execute(
        "UPDATE identity.user_account SET privacy_notice_version_accepted = '2026-09'"
        " WHERE user_id = $1",
        user,
    )
    cookie = env.open_session(site.organization_id, user, at=env.now() - timedelta(minutes=10))
    env.log.statements.clear()
    scope = _session_scope(env, cookie)
    _assert_single_context_statement(env)
    context = scope.context
    assert context.organization_id == site.organization_id
    assert context.origin is ContextOrigin.SESSION
    assert context.session_id_hash == cookie.session_id_hash
    assert context.actor.kind is ActorKind.USER and context.actor.id == user
    assert context.actor.display_name_snapshot == "Persona sintética"
    assert set(context.allowed_scopes) == {
        AllowedScope(ScopeLevel.PLANT, plant, Role.COORDINATOR_SST),
        AllowedScope(ScopeLevel.ZONE, zones[0], Role.COPASST),
    }
    assert scope.privacy_notice_version_accepted == "2026-09"
    # La misma sentencia prolongó la sesión.
    (row,) = env.fetch(
        "SELECT last_seen_at, idle_expires_at FROM identity.session WHERE session_id_hash = $1",
        cookie.session_id_hash,
    )
    assert row["last_seen_at"] == env.now()
    assert row["idle_expires_at"] == env.now() + timedelta(minutes=30)


@pytest.mark.integration
def test_session_context_refuses_what_validation_refuses(environment: AuthzEnvironment) -> None:
    env = environment
    site = env.add_site(plants=1, zones_per_plant=0)
    other = env.add_site(plants=1, zones_per_plant=0)
    user = env.add_user(site.organization_id)
    env.assign(site.organization_id, user, Role.ADMINISTRATOR)
    cookie = env.open_session(site.organization_id, user)
    # La cookie con otra organización no encuentra la fila (seguridad a nivel de fila).
    forged = SessionCookie(other.organization_id, cookie.token)
    assert _unavailable(env, forged) is ContextUnavailableReason.SESSION_INVALID
    # Segundo factor sin verificar.
    pending = env.open_session(site.organization_id, user, verified=False)
    assert _unavailable(env, pending) is ContextUnavailableReason.SESSION_INVALID
    # Vencida por inactividad: en el instante exacto ya no sirve.
    idle = env.open_session(site.organization_id, user, at=env.now() - timedelta(minutes=30))
    assert _unavailable(env, idle) is ContextUnavailableReason.SESSION_INVALID
    # Vencida por el tope absoluto (12 h) con la inactividad aún vigente: en el instante exacto
    # ya no sirve, y un milisegundo antes sí.
    for offset, usable in ((timedelta(0), False), (timedelta(milliseconds=1), True)):
        absolute = env.open_session(
            site.organization_id, user, at=env.now() - timedelta(hours=12) + offset
        )
        # Visto hace un minuto: la inactividad vence dentro de 29 minutos.
        env.execute(
            "UPDATE identity.session SET last_seen_at = $2::timestamptz,"
            " idle_expires_at = $2::timestamptz + interval '30 minutes'"
            " WHERE session_id_hash = $1",
            absolute.session_id_hash,
            env.now() - timedelta(minutes=1),
        )
        if usable:
            assert _session_scope(env, absolute).context.organization_id == site.organization_id
        else:
            assert _unavailable(env, absolute) is ContextUnavailableReason.SESSION_INVALID
    # Una sesión válida sin asignaciones da un contexto que no concede nada.
    bare = env.add_user(site.organization_id)
    context = _session_scope(env, env.open_session(site.organization_id, bare)).context
    assert context.allowed_scopes == ()
    # Un usuario del proveedor sin concesión: su propia organización y sus asignaciones.
    installer = _session_scope(
        env, env.open_session(env.provider_organization_id, env.installer_id)
    )
    assert installer.context.organization_id == env.provider_organization_id
    assert installer.context.concession_id is None


@pytest.mark.integration
def test_authorization_denied_is_audited_in_the_database(environment: AuthzEnvironment) -> None:
    env = environment
    site = env.add_site(plants=1, zones_per_plant=1)
    plant, zones = next(iter(site.plants.items()))
    user = env.add_user(site.organization_id)
    env.assign(site.organization_id, user, Role.LINE_MANAGER, ScopeLevel.ZONE, zones[0])
    context = _session_scope(env, env.open_session(site.organization_id, user)).context
    resource = Resource.zone(site.organization_id, plant, zones[0])
    with pytest.raises(ResourceNotFound):
        env.run(env.authorizer.authorize(context, PermissionKey.FINDINGS_READ, resource))
    rows = env.fetch(
        "SELECT operation, outcome, actor_id, scope_plant_id, scope_zone_id, resource_kind,"
        " resource_id, convert_from(filters, 'UTF8') AS filters, correlation_id"
        " FROM shared.audit_entry"
        " WHERE organization_id = $1 AND operation = 'authorization_denied'",
        site.organization_id,
    )
    assert len(rows) == 1
    (row,) = rows
    assert (row["outcome"], row["actor_id"]) == ("denied", user)
    assert (row["scope_plant_id"], row["scope_zone_id"]) == (plant, zones[0])
    assert (row["resource_kind"], row["resource_id"]) == ("zone", zones[0])
    assert row["filters"] == '{"permission_key":"findings.read"}'
    assert row["correlation_id"] == context.correlation_id
    # La misma persona sí tiene su clave y no deja rastro de denegación.
    allowed = env.run(env.authorizer.authorize(context, PermissionKey.COMMITMENTS_WRITE, resource))
    assert allowed.actor.role_in_use is Role.LINE_MANAGER
    assert (
        len(
            env.fetch(
                "SELECT 1 FROM shared.audit_entry WHERE organization_id = $1"
                " AND operation = 'authorization_denied'",
                site.organization_id,
            )
        )
        == 1
    )


# --- PR-NUC-10 y reglas de la concesión ----------------------------------------------------------


@st.composite
def concession_timelines(draw: st.DrawFn) -> dict[str, Any]:
    """Concesión con duración en [1 h, 90 d], revocación opcional y un instante t alrededor."""
    duration = timedelta(seconds=draw(st.sampled_from([3600, 3601, 86400, 7 * 86400, 90 * 86400])))
    revoked_after = draw(st.one_of(st.none(), st.integers(0, int(duration.total_seconds()) + 60)))
    edges = [0, 1, int(duration.total_seconds()) - 1, int(duration.total_seconds())]
    if revoked_after is not None:
        edges += [revoked_after - 1, revoked_after, revoked_after + 1]
    offset = draw(
        st.one_of(
            st.sampled_from([e for e in edges if e >= 0]),
            st.integers(0, int(duration.total_seconds()) + 3600),
        )
    )
    level = draw(st.sampled_from([ScopeLevel.ORGANIZATION, ScopeLevel.PLANT]))
    expired_marked = draw(st.booleans())
    return {
        "duration": duration,
        "revoked_after": revoked_after,
        "offset": timedelta(seconds=offset),
        "level": level,
        "expired_marked": expired_marked,
    }


@pytest.fixture(scope="module")
def concession_site(environment: AuthzEnvironment) -> Any:
    site = environment.add_site(plants=2, zones_per_plant=1)
    # nuc_0009 (TASK-127) impone el tope del cliente: las líneas de tiempo llegan a 90 días.
    environment.execute(
        "UPDATE identity.organization SET concession_max_days = 90 WHERE organization_id = $1",
        site.organization_id,
    )
    return site


@pytest.mark.integration
@given(timeline=concession_timelines())
def test_pr_nuc_10_concession_context_follows_the_timeline(
    environment: AuthzEnvironment, concession_site: Any, timeline: dict[str, Any]
) -> None:
    env = environment
    site = concession_site
    plant = next(iter(site.plants))
    granted = env.now() - timedelta(days=100)
    t = granted + timeline["offset"]
    revoked_at = (
        None
        if timeline["revoked_after"] is None
        else granted + timedelta(seconds=timeline["revoked_after"])
    )
    expires = granted + timeline["duration"]
    # En t la revocación solo existe si ya ocurrió (antes, la fila aún estaba activa).
    revoked_by_t = revoked_at is not None and revoked_at <= t and revoked_at <= expires
    status = "revoked" if revoked_by_t else "active"
    if status == "active" and timeline["expired_marked"] and t >= expires:
        status = "expired"  # la tarea expire_concessions ya corrió
    scope_id = site.organization_id if timeline["level"] is ScopeLevel.ORGANIZATION else plant
    concession_id = env.add_concession(
        site.organization_id,
        env.installer_id,
        level=timeline["level"],
        scope_id=scope_id,
        granted_at=granted,
        duration=timeline["duration"],
        status=status,
        revoked_at=revoked_at if revoked_by_t else None,
    )
    cookie = env.open_session(env.provider_organization_id, env.installer_id, at=t)
    clock = env.sessions.clock
    saved = clock.now()
    clock.set(t)
    try:
        must_fail = t >= expires or (revoked_at is not None and t >= revoked_at)
        if must_fail:
            assert _unavailable(env, cookie, concession_id) is (
                ContextUnavailableReason.CONCESSION_INVALID
            )
            return
        context = _session_scope(env, cookie, concession_id).context
        assert context.organization_id == site.organization_id
        assert context.concession_id == concession_id
        assert context.actor.kind is ActorKind.PROVIDER_USER
        assert context.allowed_scopes == (
            AllowedScope(timeline["level"], scope_id, Role.PROVIDER_INSTALLER),
        )
    finally:
        clock.set(saved)


@pytest.mark.integration
def test_concession_context_rules(environment: AuthzEnvironment) -> None:
    env = environment
    site = env.add_site(plants=1, zones_per_plant=1)
    other_client = env.add_site(plants=1, zones_per_plant=0)
    installer = env.add_provider_user()
    concession = env.add_concession(site.organization_id, installer)
    cookie = env.open_session(env.provider_organization_id, installer)
    env.log.statements.clear()
    context = _session_scope(env, cookie, concession).context
    # Bajo concesión también es una sola sentencia.
    _assert_single_context_statement(env)
    assert context.organization_id == site.organization_id
    # La concesión de otro usuario del proveedor no sirve.
    colleague = env.add_provider_user()
    colleague_cookie = env.open_session(env.provider_organization_id, colleague)
    assert _unavailable(env, colleague_cookie, concession) is (
        ContextUnavailableReason.CONCESSION_INVALID
    )
    # Una concesión inexistente tampoco.
    assert _unavailable(env, cookie, uuid.uuid4()) is ContextUnavailableReason.CONCESSION_INVALID
    # Un cliente no puede seleccionar concesiones: la sentencia no ve la de la proveedora.
    client_user = env.add_user(other_client.organization_id)
    env.assign(other_client.organization_id, client_user, Role.ADMINISTRATOR)
    client_cookie = env.open_session(other_client.organization_id, client_user)
    assert _unavailable(env, client_cookie, concession) is (
        ContextUnavailableReason.CONCESSION_INVALID
    )
    # El cliente suspendido corta el acceso aunque la concesión siga activa.
    env.set_organization_status(site.organization_id, "suspended")
    try:
        assert _unavailable(env, cookie, concession) is ContextUnavailableReason.CONCESSION_INVALID
    finally:
        env.set_organization_status(site.organization_id, "active")
    assert _session_scope(env, cookie, concession).context.concession_id == concession
    # Sin su asignación de instalador ya no puede actuar bajo concesión.
    (assignment,) = [
        row["assignment_id"]
        for row in env.fetch(
            "SELECT assignment_id FROM identity.role_assignment WHERE user_id = $1", installer
        )
    ]
    env.remove_assignment(assignment)
    assert _unavailable(env, cookie, concession) is ContextUnavailableReason.CONCESSION_INVALID


@pytest.mark.integration
def test_session_concession_function_is_narrow(environment: AuthzEnvironment) -> None:
    """La función SECURITY DEFINER no devuelve nada fuera del contexto de la proveedora."""
    env = environment
    site = env.add_site(plants=1, zones_per_plant=0)
    concession = env.add_concession(site.organization_id, env.installer_id)

    async def call(organization: uuid.UUID | None, user: uuid.UUID) -> list[Any]:
        connection = await env.sessions.migrated.connect("vigia_app")
        try:
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('vigia.organization_id', $1, true)",
                    "" if organization is None else str(organization),
                )
                rows = await connection.fetch(
                    "SELECT * FROM identity.session_concession($1, $2, now())", concession, user
                )
                # Desde otra organización que el cliente, vigia_app no ve la fila aunque fije la
                # variable de la función: la política concession_lookup es solo del dueño.
                await connection.execute("SELECT set_config('vigia.concession_lookup', 'on', true)")
                visible = await connection.fetch(
                    "SELECT reason FROM identity.provider_concession WHERE concession_id = $1",
                    concession,
                )
                assert visible == [] or organization == site.organization_id
                return list(rows)
        finally:
            await connection.close()

    assert len(env.run(call(env.provider_organization_id, env.installer_id))) == 1
    # La función misma comprueba la vigencia (sin depender de la capa de Python).
    expired = env.add_concession(
        site.organization_id,
        env.installer_id,
        granted_at=env.now() - timedelta(days=3),
        duration=timedelta(days=1),
    )

    async def expired_rows() -> list[Any]:
        connection = await env.sessions.migrated.connect("vigia_app")
        try:
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('vigia.organization_id', $1, true)",
                    str(env.provider_organization_id),
                )
                return list(
                    await connection.fetch(
                        "SELECT * FROM identity.session_concession($1, $2, $3)",
                        expired,
                        env.installer_id,
                        env.now(),
                    )
                )
        finally:
            await connection.close()

    assert env.run(expired_rows()) == []
    assert env.run(call(site.organization_id, env.installer_id)) == []
    assert env.run(call(None, env.installer_id)) == []
    assert env.run(call(env.provider_organization_id, env.operator_id)) == []
    (row,) = env.run(call(env.provider_organization_id, env.installer_id))
    assert "reason" not in dict(row)


@pytest.mark.integration
def test_operator_context(environment: AuthzEnvironment) -> None:
    env = environment
    context = env.run(env.contexts.context_from_operator(env.operator_id))
    assert context.organization_id == env.provider_organization_id
    assert context.origin is ContextOrigin.ADMIN_COMMAND
    assert context.actor.kind is ActorKind.OPERATOR
    assert context.allowed_scopes == (
        AllowedScope(ScopeLevel.ORGANIZATION, env.provider_organization_id, Role.PLATFORM_OPERATOR),
    )
    authorized = env.run(
        env.authorizer.authorize(
            context,
            PermissionKey.PLATFORM_KEYS_ROTATE,
            Resource.organization(env.provider_organization_id),
        )
    )
    assert authorized.actor.role_in_use is Role.PLATFORM_OPERATOR
    for candidate in (env.installer_id, uuid.uuid4()):
        with pytest.raises(ContextUnavailable) as raised:
            env.run(env.contexts.context_from_operator(candidate))
        assert raised.value.reason is ContextUnavailableReason.OPERATOR_INVALID


# --- PR-NUC-16 en el expediente y seguimiento de VIG-53 (BR-NUC-45) ------------------------------


@pytest.fixture(scope="module")
def writer(postgres_endpoint: PostgresEndpoint) -> Iterator[WriterEnvironment]:
    with (
        migrated_database(postgres_endpoint, "authz_writer") as migrated,
        writer_environment(migrated) as environment,
    ):
        yield environment


def _write(env: WriterEnvironment, context: ScopeContext, document: Any) -> Any:
    return env.loop.run(env.writer.write(context, ZONE_TYPE, document))


@st.composite
def plant_assignment_sets(draw: st.DrawFn, place: Place) -> list[AllowedScope]:
    other_plant = uuid.uuid4()
    targets = [
        (ScopeLevel.ORGANIZATION, place.organization_id),
        (ScopeLevel.PLANT, place.plant_id),
        (ScopeLevel.PLANT, other_plant),
        (ScopeLevel.ZONE, place.zone_id),
    ]
    return draw(
        st.lists(
            st.builds(
                lambda target, role: AllowedScope(target[0], target[1], role),
                st.sampled_from(targets),
                st.sampled_from(CLIENT_ROLES),
            ),
            min_size=1,
            max_size=4,
        )
    )


@pytest.mark.integration
@given(data=st.data(), key=st.sampled_from(PermissionKey))
def test_pr_nuc_16_the_record_keeps_the_role_in_use(
    writer: WriterEnvironment, data: st.DataObject, key: PermissionKey
) -> None:
    place = Place.new()
    scopes = data.draw(plant_assignment_sets(place))
    context = sealed_context(place.organization_id, scopes)
    resource = Resource.plant(place.organization_id, place.plant_id)
    decision = decide(context, key, resource, provider_organization_id=PROVIDER)
    assume(decision.granted)
    authorizer = Authorizer(audit=RecordingAudit(), provider_organization_id=PROVIDER)
    authorized = writer.loop.run(authorizer.authorize(context, key, resource))
    receipt = _write(writer, authorized, zone_document(place))
    assert isinstance(receipt, Receipt), receipt
    record = writer.loop.run(fetch_record(writer.migrated, receipt.record_id))
    assert record["actor_role_in_use"] == decision.role_in_use
    assert record["actor_id"] == context.actor.id


@pytest.mark.integration
def test_writer_rejects_a_plant_or_zone_outside_the_session_scope(
    writer: WriterEnvironment,
) -> None:
    place = Place.new()
    organization = place.organization_id
    before = writer.database.probe.opened
    for scopes, pointer in (
        ([AllowedScope(ScopeLevel.PLANT, uuid.uuid4(), Role.ADMINISTRATOR)], "/plant_id"),
        ([AllowedScope(ScopeLevel.ZONE, place.zone_id, Role.ADMINISTRATOR)], "/zone_id"),
        ([], "/plant_id"),
    ):
        rejection = _write(writer, sealed_context(organization, scopes), zone_document(place))
        assert isinstance(rejection, LedgerRejection), rejection
        assert (rejection.code, rejection.field) == (LedgerRejectionCode.CONTEXT_ABSENT, pointer)
    assert writer.database.probe.opened == before  # rechazado antes de tocar la base
    # Dentro del alcance (planta u organización) se escribe.
    for scope in (
        AllowedScope(ScopeLevel.PLANT, place.plant_id, Role.ADMINISTRATOR),
        AllowedScope(ScopeLevel.ORGANIZATION, organization, Role.ADMINISTRATOR),
    ):
        receipt = _write(writer, sealed_context(organization, [scope]), zone_document(place))
        assert isinstance(receipt, Receipt), receipt
    # Los contextos del sistema actúan sobre su organización entera.
    system = sealed_context(
        organization, [], kind=ActorKind.SYSTEM, origin=ContextOrigin.OUTBOX_EVENT
    )
    assert isinstance(_write(writer, system, zone_document(place)), Receipt)


def _insert_hierarchy(
    env: WriterEnvironment, organization_id: uuid.UUID, layout: Mapping[uuid.UUID, uuid.UUID]
) -> None:
    """Organización, una persona y, por cada planta, su zona (``layout``: planta → zona)."""

    async def insert() -> None:
        user_id = uuid.uuid4()
        created = datetime(2026, 1, 1, tzinfo=UTC)
        connection = await env.migrated.connect()
        try:
            # La organización y su persona van juntas: ``created_by`` es diferible.
            transaction = connection.transaction()
            await transaction.start()
            await connection.execute(
                "INSERT INTO identity.organization"
                " (organization_id, code, name, kind, created_at, created_by)"
                " VALUES ($1, $2, 'Organización sintética', 'client', $3, $4)",
                organization_id,
                f"ORG-{organization_id.hex[:8].upper()}",
                created,
                user_id,
            )
            await connection.execute(
                "INSERT INTO identity.user_account (user_id, organization_id, email,"
                " display_name, status, second_factor_required, created_at)"
                " VALUES ($1, $2, $3, 'Persona sintética', 'active', false, $4)",
                user_id,
                organization_id,
                f"persona-{user_id.hex[:12]}@example.test",
                created,
            )
            for plant_id, zone_id in layout.items():
                await connection.execute(
                    "INSERT INTO identity.plant (plant_id, organization_id, code, name, country,"
                    " data_region, timezone, created_at, created_by) VALUES ($1, $2, $3,"
                    " 'Planta sintética', 'CO', 'us-east-1', 'America/Bogota', $4, $5)",
                    plant_id,
                    organization_id,
                    f"PL-{plant_id.hex[:6].upper()}",
                    created,
                    user_id,
                )
                await connection.execute(
                    "INSERT INTO identity.zone (zone_id, organization_id, plant_id, code, name,"
                    " created_at, created_by) VALUES ($1, $2, $3, $4, 'Zona sintética', $5, $6)",
                    zone_id,
                    organization_id,
                    plant_id,
                    f"ZN-{zone_id.hex[:6].upper()}",
                    created,
                    user_id,
                )
            await transaction.commit()
        finally:
            await connection.close()

    env.loop.run(insert())


@pytest.mark.integration
def test_writer_rejects_an_own_zone_under_a_foreign_plant(writer: WriterEnvironment) -> None:
    """BR-NUC-45 (S7 de la revisión): con solo un alcance de zona, la zona es de la planta.

    Quien tiene la zona Z de la planta P no escribe un registro con ``zone_id = Z`` y la planta
    P' (el registro entraría en la cadena de P'); con su planta real, sí.
    """
    place = Place.new()
    organization = place.organization_id
    foreign_plant, foreign_zone = uuid.uuid4(), uuid.uuid4()
    _insert_hierarchy(
        writer, organization, {place.plant_id: place.zone_id, foreign_plant: foreign_zone}
    )
    context = sealed_context(
        organization, [AllowedScope(ScopeLevel.ZONE, place.zone_id, Role.ADMINISTRATOR)]
    )

    def document(plant_id: uuid.UUID, zone_id: uuid.UUID) -> dict[str, Any]:
        return {**zone_document(place), "plant_id": str(plant_id), "zone_id": str(zone_id)}

    rejection = _write(writer, context, document(foreign_plant, place.zone_id))
    assert isinstance(rejection, LedgerRejection), rejection
    assert (rejection.code, rejection.field) == (LedgerRejectionCode.CONTEXT_ABSENT, "/plant_id")
    # Una zona que no existe en la organización tampoco vale con la planta propia.
    unknown = sealed_context(
        organization, [AllowedScope(ScopeLevel.ZONE, uuid.uuid4(), Role.ADMINISTRATOR)]
    )
    (unknown_zone,) = (scope.scope_id for scope in unknown.allowed_scopes)
    rejection = _write(writer, unknown, document(place.plant_id, unknown_zone))
    assert isinstance(rejection, LedgerRejection), rejection
    assert (rejection.code, rejection.field) == (LedgerRejectionCode.CONTEXT_ABSENT, "/plant_id")
    # Con su planta real se escribe.
    receipt = _write(writer, context, document(place.plant_id, place.zone_id))
    assert isinstance(receipt, Receipt), receipt
    # Con la planta cubierta por sí misma (alcance de planta) no se consulta la zona.
    plant_context = sealed_context(
        organization, [AllowedScope(ScopeLevel.PLANT, place.plant_id, Role.ADMINISTRATOR)]
    )
    receipt = _write(writer, plant_context, document(place.plant_id, place.zone_id))
    assert isinstance(receipt, Receipt), receipt


def test_scope_containment_edges() -> None:
    organization, plant, zone = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for level, scope_id, cases in (
        (
            ScopeLevel.ORGANIZATION,
            organization,
            {(None, None): True, (plant, None): True, (plant, zone): True},
        ),
        (
            ScopeLevel.PLANT,
            plant,
            {(None, None): False, (plant, None): True, (plant, zone): True, (zone, None): False},
        ),
        (
            ScopeLevel.ZONE,
            zone,
            {(None, None): False, (plant, None): False, (plant, zone): True, (None, zone): True},
        ),
    ):
        scope = AllowedScope(level, scope_id, Role.COPASST)
        for (p, z), expected in cases.items():
            assert scope.covers(organization, p, z) is expected, (level, p, z)
    # Un alcance de organización con otro identificador no cubre nada.
    assert not AllowedScope(ScopeLevel.ORGANIZATION, uuid.uuid4(), Role.COPASST).covers(
        organization, plant, zone
    )


def test_every_key_is_granted_to_someone_and_every_role_has_keys() -> None:
    assert all(roles_with(key) for key in PermissionKey)
    assert all(MATRIX[role] for role in Role)
    assert list(itertools.chain.from_iterable(MATRIX.values()))
