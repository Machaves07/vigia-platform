"""PR-NUC-36 y el token de vista en vivo de ``shared.tokens`` (TASK-128; BR-NUC-88 y 89).

**PR-NUC-36**: para todo usuario, zona y nodo generados, el token emitido tiene
``exp - iat = 600 s``, ``aud`` igual al nodo vigente de la zona, ``role`` igual a ``role_in_use``,
y lo verifica sin red el verificador del nodo escrito solo con U-01 (``KeySet`` fijado con el
conjunto que publica la plataforma, clave Ed25519 del ``kid`` y modelo estricto
``LiveViewToken``). Todo contra PostgreSQL 16 como ``vigia_app`` con los servicios reales.

Además, uno por criterio de aceptación: la JWS compacta de tres partes con ``alg = EdDSA`` y
``kid`` valida contra ``live_view_token.schema.json`` de U-01; ``live_view_local_url`` nula sin
dirección anunciada; el token completo no está en ninguna tabla ni en el registro; un acceso
local con un ``jti`` emitido para otro nodo produce ``unknown_token_reported`` y
``security_alert``. Y los bordes: zona sin nodo, nodo retirado o revocado, zona fuera de alcance,
roles sin ``live_view.open``, acceso duplicado, acceso con rol fuera de la lista.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from vigia_contracts.models._validators import validate_instance

from tests.factories import uuid7
from tests.identity_db import BASE_TIME
from tests.integration.conftest import PostgresEndpoint
from tests.live_view_support import (
    LIVE_VIEW_URL,
    LiveViewEnvironment,
    jws_parts,
    live_view_environment,
    node_verifies,
)
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.signing.keys import KeyStatus, SigningPurpose
from vigia_platform.shared.signing.service import DetachedSignature, SigningKeyUnavailable
from vigia_platform.shared.tokens import (
    TOKEN_LIFETIME_SECONDS,
    AccessReport,
    IssuedLiveViewToken,
    LiveViewRejection,
    LiveViewTokenRejected,
    LiveViewTokenService,
    compact_jws,
)

pytestmark = pytest.mark.integration

SCHEMA = "https://vigia.local/schemas/live_view_token.schema.json#"
OPENERS = (Role.COORDINATOR_SST, Role.ADMINISTRATOR, Role.COPASST)
"""Roles de una organización cliente con ``live_view.open`` (el proveedor, bajo concesión)."""
NON_OPENERS = (Role.LINE_MANAGER, Role.PLANT_MANAGER)
ROLES_BY_SIDE = {False: OPENERS, True: (Role.PROVIDER_INSTALLER,)}
"""Roles que abren la vista: de la organización cliente o, bajo concesión, del proveedor."""
MAX_OFFSET_SECONDS = 300 * 24 * 3600
"""Dentro de la vigencia de 365 días de la clave ``live_view_token`` dada de alta."""
PROVIDER_OFFSET_SECONDS = 30 * 24 * 3600
"""Bajo concesión, la vigencia tiene que cubrir también la hora real de la base (≤ 90 días)."""


@pytest.fixture(scope="module")
def env(postgres_endpoint: PostgresEndpoint) -> Iterator[LiveViewEnvironment]:
    with live_view_environment(postgres_endpoint, "live_view_token") as environment:
        yield environment


@pytest.fixture(scope="module")
def site(env: LiveViewEnvironment) -> Any:
    return env.add_site(plants=2, zones_per_plant=2)


def _issue(env: LiveViewEnvironment, context: Any, zone_id: uuid.UUID) -> IssuedLiveViewToken:
    issued: IssuedLiveViewToken = env.run(env.service().issue(context, zone_id))
    return issued


def _fresh_zone(env: LiveViewEnvironment, site: Any, *, url: str | None = LIVE_VIEW_URL) -> Any:
    """Una zona nueva de la primera planta con un nodo vigente."""
    plant_id = next(iter(site.plants))
    zone_id = uuid.uuid4()
    env.authz.add_zone(site.organization_id, plant_id, zone_id)
    node_id = env.add_node(site.organization_id, plant_id, url=url)
    assignment = env.assign_node(site.organization_id, plant_id, zone_id, node_id)
    return plant_id, zone_id, node_id, assignment


# --- PR-NUC-36 -------------------------------------------------------------------------------


@st.composite
def issuance_cases(draw: st.DrawFn) -> dict[str, Any]:
    provider = draw(st.booleans())
    return {
        "provider": provider,
        "role": draw(st.sampled_from(ROLES_BY_SIDE[provider])),
        "level": draw(st.sampled_from(list(ScopeLevel))),
        "url": draw(st.sampled_from([None, LIVE_VIEW_URL, "https://[fd00::1]:8443/"])),
        "plant_index": draw(st.integers(0, 1)),
        "offset": draw(st.integers(0, MAX_OFFSET_SECONDS)),
        "verify_after": draw(st.integers(0, TOKEN_LIFETIME_SECONDS - 1)),
    }


@given(case=issuance_cases())
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_pr_nuc_36_token_verified_offline_by_the_u01_verifier(
    env: LiveViewEnvironment, site: Any, case: dict[str, Any]
) -> None:
    clock = env.clock
    start = clock.now()
    try:
        offset = case["offset"] % PROVIDER_OFFSET_SECONDS if case["provider"] else case["offset"]
        clock.advance(offset + 0.123)
        plant_id = list(site.plants)[case["plant_index"]]
        zone_id = uuid.uuid4()
        env.authz.add_zone(site.organization_id, plant_id, zone_id)
        node_id = env.add_node(site.organization_id, plant_id, url=case["url"])
        env.assign_node(site.organization_id, plant_id, zone_id, node_id)
        scope_id = {
            ScopeLevel.ORGANIZATION: site.organization_id,
            ScopeLevel.PLANT: plant_id,
            ScopeLevel.ZONE: zone_id,
        }[case["level"]]
        if case["provider"]:
            plant_level = case["level"] is ScopeLevel.PLANT
            level = ScopeLevel.PLANT if plant_level else ScopeLevel.ORGANIZATION
            installer = env.authz.add_provider_user()
            # La política de la base mira su ``now()`` real y el constructor del contexto, el
            # reloj simulado: la concesión cubre los dos instantes.
            (database_now,) = env.fetch("SELECT now() AS now")
            low = min(clock.now(), database_now["now"]) - timedelta(hours=1)
            high = max(clock.now(), database_now["now"]) + timedelta(hours=1)
            concession = env.authz.add_concession(
                site.organization_id,
                installer,
                level=level,
                scope_id=plant_id if level is ScopeLevel.PLANT else None,
                granted_at=low,
                duration=high - low,
            )
            context = env.concession_context(installer, concession)
            user_id = installer
        else:
            user_id = env.user_with_role(
                site.organization_id, case["role"], case["level"], scope_id
            )
            context = env.session_context(site.organization_id, user_id)
        issued = _issue(env, context, zone_id)

        header, payload, _ = jws_parts(issued.token)
        assert payload["exp"] - payload["iat"] == TOKEN_LIFETIME_SECONDS
        assert payload["aud"] == str(node_id) == str(issued.node_id)
        assert payload["role"] == case["role"].value
        assert payload["sub"] == str(user_id)
        assert payload["zone_id"] == str(zone_id) and payload["plant_id"] == str(plant_id)
        assert payload["organization_id"] == str(site.organization_id)
        assert payload["iat"] == int(clock.now().timestamp())
        assert uuid.UUID(payload["jti"]).version == 7 and uuid.UUID(payload["jti"]) == issued.jti
        assert issued.live_view_local_url == case["url"]
        assert issued.expires_at.timestamp() == payload["exp"]

        key_set = env.node_key_set()
        verify_at = clock.now() + timedelta(seconds=case["verify_after"])
        claims = node_verifies(issued.token, node_id, key_set, verify_at)
        assert claims is not None and claims.jti == payload["jti"]
        # El mismo verificador rechaza el token en otro nodo y ya vencido.
        assert node_verifies(issued.token, uuid.uuid4(), key_set, verify_at) is None
        expired = clock.now() + timedelta(seconds=TOKEN_LIFETIME_SECONDS)
        assert node_verifies(issued.token, node_id, key_set, expired) is None
        assert header["kid"] in {k.key_id for k in key_set.active("live_view_token", verify_at)}

        (row,) = env.fetch(
            "SELECT node_id, user_id, role_in_use, issued_at, expires_at, zone_id, plant_id"
            " FROM identity.live_view_token_issuance WHERE jti = $1",
            issued.jti,
        )
        assert row["node_id"] == node_id and row["user_id"] == user_id
        assert row["role_in_use"] == case["role"].value
        assert row["expires_at"] - row["issued_at"] == timedelta(seconds=TOKEN_LIFETIME_SECONDS)
        assert int(row["issued_at"].timestamp()) == payload["iat"]
    finally:
        clock.set(start)


def test_verifier_rejects_tampered_and_foreign_tokens(env: LiveViewEnvironment, site: Any) -> None:
    """El oráculo no es vacío: una carga cambiada, otra firma o un ``kid`` ajeno no verifican."""
    _, zone_id, node_id, _ = _fresh_zone(env, site)
    user = env.user_with_role(site.organization_id, Role.COORDINATOR_SST)
    issued = _issue(env, env.session_context(site.organization_id, user), zone_id)
    key_set = env.node_key_set()
    now = env.clock.now()
    header, payload, signature = issued.token.split(".")
    assert node_verifies(issued.token, node_id, key_set, now) is not None
    other = _issue(env, env.session_context(site.organization_id, user), zone_id)
    _, other_payload, other_signature = other.token.split(".")
    assert node_verifies(f"{header}.{other_payload}.{signature}", node_id, key_set, now) is None
    assert node_verifies(f"{header}.{payload}.{other_signature}", node_id, key_set, now) is None
    assert node_verifies(f"{header}.{payload}", node_id, key_set, now) is None


class RotatingSigner:
    """Firmante que rota la clave ``rotations`` veces justo entre leer el ``kid`` y firmar."""

    def __init__(self, rotations: int, *, active: bool = True) -> None:
        self.rotations = rotations
        self.active = active
        self.generation = 0
        self.messages: list[bytes] = []

    def public_keys(self, purpose: SigningPurpose) -> tuple[Any, ...]:
        status = KeyStatus.ACTIVE if self.active else KeyStatus.OVERLAPPING
        return (SimpleNamespace(key_id=f"live-{self.generation}", status=status),)

    def sign_detached(self, purpose: SigningPurpose, message: bytes) -> DetachedSignature:
        assert purpose is SigningPurpose.LIVE_VIEW_TOKEN
        self.messages.append(message)
        if self.rotations:
            self.rotations -= 1
            self.generation += 1
        return DetachedSignature(key_id=f"live-{self.generation}", signature=b"\x01" * 64)


@pytest.mark.parametrize("rotations", [0, 1, 2])
def test_kid_always_names_the_key_that_signed(rotations: int) -> None:
    signer = RotatingSigner(rotations)
    token = compact_jws({"jti": "x"}, signer)
    header, _, _ = jws_parts(token)
    assert header == {"alg": "EdDSA", "kid": f"live-{rotations}"}
    assert len(signer.messages) == rotations + 1
    assert signer.messages[-1] == token.rsplit(".", 1)[0].encode("ascii")


def test_without_a_stable_active_key_nothing_is_signed() -> None:
    with pytest.raises(SigningKeyUnavailable):
        compact_jws({"jti": "x"}, RotatingSigner(3))
    signer = RotatingSigner(0, active=False)
    with pytest.raises(SigningKeyUnavailable):
        compact_jws({"jti": "x"}, signer)
    assert not signer.messages


# --- Criterios de aceptación ------------------------------------------------------------------


def test_token_is_a_compact_eddsa_jws_valid_against_the_u01_schema(
    env: LiveViewEnvironment, site: Any
) -> None:
    _, zone_id, _, _ = _fresh_zone(env, site)
    user = env.user_with_role(site.organization_id, Role.COPASST)
    issued = _issue(env, env.session_context(site.organization_id, user), zone_id)
    segments = issued.token.split(".")
    assert len(segments) == 3 and all(segments)
    assert all("=" not in segment for segment in segments)
    header, payload, signature = jws_parts(issued.token)
    keys = env.signing.public_keys(SigningPurpose.LIVE_VIEW_TOKEN)
    active = [key for key in keys if key.status is KeyStatus.ACTIVE]
    assert header == {"alg": "EdDSA", "kid": active[0].key_id}
    assert len(signature) == 64
    validate_instance(SCHEMA, payload, tolerant=False)
    assert payload["iss"] == "vigia-platform" and payload["purpose"] == "live_view"
    response = issued.to_response()
    assert set(response) == {"token", "live_view_local_url", "expires_at"}
    assert response["expires_at"].endswith(".000Z")


def test_node_without_announced_address_gives_null_url(env: LiveViewEnvironment, site: Any) -> None:
    _, zone_id, _, _ = _fresh_zone(env, site, url=None)
    user = env.user_with_role(site.organization_id, Role.ADMINISTRATOR)
    issued = _issue(env, env.session_context(site.organization_id, user), zone_id)
    assert issued.live_view_local_url is None
    assert issued.to_response()["live_view_local_url"] is None


def test_full_token_is_never_persisted_nor_logged(
    env: LiveViewEnvironment, site: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _, zone_id, _, _ = _fresh_zone(env, site)
    user = env.user_with_role(site.organization_id, Role.COORDINATOR_SST)
    issued = _issue(env, env.session_context(site.organization_id, user), zone_id)
    header, payload, signature = issued.token.split(".")
    assert issued.token not in repr(issued)
    dumps = env.fetch(
        "SELECT row_to_json(t)::text AS dump FROM identity.live_view_token_issuance AS t"
        " UNION ALL SELECT row_to_json(a)::text FROM shared.audit_entry AS a"
        " UNION ALL SELECT convert_from(a.filters, 'UTF8') FROM shared.audit_entry AS a"
        " WHERE a.filters IS NOT NULL"
        " UNION ALL SELECT row_to_json(e)::text FROM shared.outbox_event AS e"
        " UNION ALL SELECT row_to_json(r)::text FROM ledger.ledger_record AS r"
    )
    assert dumps
    # Los campos estructurados viajan en atributos del registro, no en el texto formateado.
    logged = caplog.text + "".join(repr(vars(record)) for record in caplog.records)
    for piece in (issued.token, signature, payload, f"{header}.{payload}"):
        assert all(piece not in row["dump"] for row in dumps)
        assert piece not in logged
    (audit,) = env.fetch(
        "SELECT operation, outcome, resource_kind, filters_json FROM shared.audit_entry"
        " WHERE resource_id = $1",
        issued.jti,
    )
    assert audit["operation"] == "live_view_token_issued" and audit["outcome"] == "success"
    assert audit["resource_kind"] == "live_view_token"


# --- Bordes de BR-NUC-88 ---------------------------------------------------------------------


def test_zone_without_current_node_is_rejected(env: LiveViewEnvironment, site: Any) -> None:
    user = env.user_with_role(site.organization_id, Role.COORDINATOR_SST)
    context = env.session_context(site.organization_id, user)
    plant_id, zone_id, _, assignment = _fresh_zone(env, site)
    _issue(env, context, zone_id)  # con nodo vigente sí
    env.unassign(assignment)
    for _ in range(2):
        with pytest.raises(LiveViewTokenRejected) as raised:
            _issue(env, context, zone_id)
        assert raised.value.code is LiveViewRejection.ZONE_WITHOUT_NODE
        assert raised.value.api_code == "zone_without_node"
        assert raised.value.retry_after_seconds is None
    # Un nodo revocado tampoco es vigente.
    revoked = env.add_node(site.organization_id, plant_id, status="revoked")
    env.assign_node(
        site.organization_id, plant_id, zone_id, revoked, at=BASE_TIME + timedelta(hours=2)
    )
    with pytest.raises(LiveViewTokenRejected):
        _issue(env, context, zone_id)
    # Una zona que nunca tuvo nodo.
    bare = uuid.uuid4()
    env.authz.add_zone(site.organization_id, plant_id, bare)
    with pytest.raises(LiveViewTokenRejected):
        _issue(env, context, bare)
    rows = env.fetch(
        "SELECT count(*) AS n FROM identity.live_view_token_issuance WHERE zone_id = ANY($1)",
        [zone_id, bare],
    )
    assert rows[0]["n"] == 1


@pytest.mark.parametrize("role", NON_OPENERS)
def test_roles_without_live_view_open_get_not_found(
    env: LiveViewEnvironment, site: Any, role: Role
) -> None:
    _, zone_id, _, _ = _fresh_zone(env, site)
    user = env.user_with_role(site.organization_id, role)
    with pytest.raises(ResourceNotFound):
        _issue(env, env.session_context(site.organization_id, user), zone_id)
    assert not env.fetch("SELECT 1 FROM identity.live_view_token_issuance WHERE user_id = $1", user)


def test_out_of_scope_zones_are_not_found(env: LiveViewEnvironment, site: Any) -> None:
    """Asignación de otra zona, de otra planta y de otra organización: ``not_found``."""
    (plant_a, zone_a), (_, zone_b), (plant_c, zone_c) = site.zones()[:3]
    for zone in (zone_a, zone_b, zone_c):
        plant = plant_a if zone != zone_c else plant_c
        node = env.add_node(site.organization_id, plant)
        env.assign_node(site.organization_id, plant, zone, node)
    zone_user = env.user_with_role(site.organization_id, Role.COPASST, ScopeLevel.ZONE, zone_a)
    context = env.session_context(site.organization_id, zone_user)
    _issue(env, context, zone_a)
    with pytest.raises(ResourceNotFound):
        _issue(env, context, zone_b)  # misma planta, otra zona
    plant_user = env.user_with_role(
        site.organization_id, Role.COORDINATOR_SST, ScopeLevel.PLANT, plant_a
    )
    with pytest.raises(ResourceNotFound):
        _issue(env, env.session_context(site.organization_id, plant_user), zone_c)
    other = env.add_site(plants=1, zones_per_plant=1)
    other_user = env.user_with_role(other.organization_id, Role.ADMINISTRATOR)
    with pytest.raises(ResourceNotFound):
        _issue(env, env.session_context(other.organization_id, other_user), zone_a)
    with pytest.raises(ResourceNotFound):
        _issue(env, context, uuid.uuid4())
    with pytest.raises(ResourceNotFound):
        _issue(env, context, str(zone_a))  # type: ignore[arg-type]


def test_system_context_cannot_open_the_view(env: LiveViewEnvironment, site: Any) -> None:
    _, zone_id, _, _ = _fresh_zone(env, site)
    with pytest.raises(ResourceNotFound):
        _issue(env, env.node_context(site.organization_id), zone_id)


class _GrantingAuthorizer:
    """Concede todo y devuelve el contexto tal cual: deja sola a la guarda de persona con sesión."""

    async def authorize(self, context: Any, key: Any, resource: Any) -> Any:
        return context


def test_person_guard_holds_even_if_the_authorizer_grants(
    env: LiveViewEnvironment, site: Any
) -> None:
    """Aunque la matriz conceda, un contexto del sistema no obtiene token: ``sub`` es persona."""
    _, zone_id, _, _ = _fresh_zone(env, site)
    sessions = env.authz.sessions
    service = LiveViewTokenService(
        database=sessions.database,
        authorizer=cast(Any, _GrantingAuthorizer()),
        audit=sessions.audit,
        outbox=sessions.outbox,
        signer=env.signing,
        clock=env.clock,
    )
    context = env.node_context(site.organization_id)
    with pytest.raises(ResourceNotFound):
        env.run(service.issue(context, zone_id))
    assert not env.fetch(
        "SELECT 1 FROM identity.live_view_token_issuance WHERE zone_id = $1", zone_id
    )


# --- BR-NUC-89: accesos locales ----------------------------------------------------------------


def _access(issued: IssuedLiveViewToken, **changes: Any) -> dict[str, Any]:
    _, payload, _ = jws_parts(issued.token)
    access: dict[str, Any] = {
        "access_id": str(uuid7()),
        "jti": payload["jti"],
        "sub": payload["sub"],
        "role": payload["role"],
        "zone_id": payload["zone_id"],
        "opened_at": "2026-09-30T09:01:00.000Z",
        "closed_at": "2026-09-30T09:05:00.000Z",
        "outcome": "closed_expired",
    }
    access.update(changes)
    return {key: value for key, value in access.items() if value is not None}


def _json(value: Any) -> dict[str, Any]:
    """asyncpg entrega ``jsonb`` como texto."""
    return json.loads(value) if isinstance(value, str) else dict(value)


def _incorporate(
    env: LiveViewEnvironment, organization_id: uuid.UUID, node_id: uuid.UUID, records: list[Any]
) -> AccessReport:
    report: AccessReport = env.run(
        env.service().incorporate(env.node_context(organization_id), node_id, records)
    )
    return report


def _entries(env: LiveViewEnvironment, jti: str, operation: str) -> list[Any]:
    return env.fetch(
        "SELECT organization_id, outcome, scope_plant_id, scope_zone_id, filters_json"
        " FROM shared.audit_entry WHERE operation = $1 AND resource_id = $2",
        operation,
        uuid.UUID(jti),
    )


def _alerts(env: LiveViewEnvironment, node_id: uuid.UUID) -> list[Any]:
    return env.fetch(
        "SELECT organization_id, payload FROM shared.outbox_event WHERE event_name ="
        " 'security_alert' AND payload ->> 'resource_id' = $1",
        str(node_id),
    )


def test_access_from_the_issued_node_is_incorporated_once(
    env: LiveViewEnvironment, site: Any
) -> None:
    plant_id, zone_id, node_id, _ = _fresh_zone(env, site)
    user = env.user_with_role(site.organization_id, Role.COORDINATOR_SST)
    issued = _issue(env, env.session_context(site.organization_id, user), zone_id)
    record = _access(issued)
    report = _incorporate(env, site.organization_id, node_id, [record])
    assert report == AccessReport(incorporated=1)
    (entry,) = _entries(env, record["jti"], "live_view_access_local")
    assert entry["outcome"] == "success"
    assert entry["scope_plant_id"] == plant_id and entry["scope_zone_id"] == zone_id
    details = _json(entry["filters_json"])
    assert details["access_id"] == record["access_id"]
    assert details["opened_at"] == record["opened_at"]
    assert details["closed_at"] == record["closed_at"]
    assert details["outcome"] == "closed_expired"
    # Por access_id sin duplicar (adenda A-02): el latido lo repite, con otro resultado o dos
    # veces en el mismo lote, y sigue habiendo una sola entrada.
    again = _incorporate(env, site.organization_id, node_id, [record])
    assert again == AccessReport(duplicates=1)
    opened = _access(issued, access_id=record["access_id"], closed_at=None, outcome="opened")
    assert _incorporate(env, site.organization_id, node_id, [opened]) == AccessReport(duplicates=1)
    twice = _access(issued)
    assert _incorporate(env, site.organization_id, node_id, [twice, twice]) == AccessReport(
        incorporated=1, duplicates=1
    )
    assert len(_entries(env, record["jti"], "live_view_access_local")) == 2
    assert not _entries(env, record["jti"], "unknown_token_reported")
    assert not _alerts(env, node_id)
    # El mismo access_id desde otro nodo no se toma por repetido: es otro acceso, y alerta.
    other_node = env.add_node(site.organization_id, plant_id)
    assert _incorporate(env, site.organization_id, other_node, [record]) == AccessReport(unknown=1)


def test_access_with_a_jti_issued_for_another_node_alerts(
    env: LiveViewEnvironment, site: Any
) -> None:
    plant_id, zone_id, _, _ = _fresh_zone(env, site)
    other_node = env.add_node(site.organization_id, plant_id)
    user = env.user_with_role(site.organization_id, Role.COORDINATOR_SST)
    issued = _issue(env, env.session_context(site.organization_id, user), zone_id)
    record = _access(issued)
    report = _incorporate(env, site.organization_id, other_node, [record])
    assert report == AccessReport(unknown=1)
    (entry,) = _entries(env, record["jti"], "unknown_token_reported")
    assert entry["outcome"] == "denied" and entry["scope_plant_id"] == plant_id
    details = _json(entry["filters_json"])
    assert details["reason"] == "other_node" and details["node_id"] == str(other_node)
    (alert,) = _alerts(env, other_node)
    payload = _json(alert["payload"])
    assert payload["alert_kind"] == "unknown_token_reported"
    assert payload["resource_kind"] == "node"
    assert alert["organization_id"] == site.organization_id
    assert not _entries(env, record["jti"], "live_view_access_local")
    # Repetido, no vuelve a alertar.
    assert _incorporate(env, site.organization_id, other_node, [record]).duplicates == 1
    assert len(_alerts(env, other_node)) == 1
    assert len(_entries(env, record["jti"], "unknown_token_reported")) == 1


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"jti": None}, "not_issued"),
        ({"zone_id": None}, "claims_mismatch"),
        ({"sub": None}, "claims_mismatch"),
        ({"role": "copasst"}, "claims_mismatch"),
    ],
)
def test_unknown_or_mismatched_tokens_alert(
    env: LiveViewEnvironment, site: Any, change: dict[str, Any], reason: str
) -> None:
    _, zone_id, node_id, _ = _fresh_zone(env, site)
    user = env.user_with_role(site.organization_id, Role.COORDINATOR_SST)
    issued = _issue(env, env.session_context(site.organization_id, user), zone_id)
    replacement = {
        key: (str(uuid7()) if key == "jti" else str(uuid.uuid4())) if value is None else value
        for key, value in change.items()
    }
    record = _access(issued, **replacement)
    assert _incorporate(env, site.organization_id, node_id, [record]) == AccessReport(unknown=1)
    (entry,) = _entries(env, record["jti"], "unknown_token_reported")
    details = _json(entry["filters_json"])
    assert details["reason"] == reason
    assert "sub" not in details and "zone_id" not in details
    assert len(_alerts(env, node_id)) == 1


def test_a_jti_of_another_organization_is_unknown(env: LiveViewEnvironment, site: Any) -> None:
    _, zone_id, _, _ = _fresh_zone(env, site)
    user = env.user_with_role(site.organization_id, Role.COORDINATOR_SST)
    issued = _issue(env, env.session_context(site.organization_id, user), zone_id)
    other = env.add_site(plants=1, zones_per_plant=1)
    other_plant = next(iter(other.plants))
    foreign_node = env.add_node(other.organization_id, other_plant)
    record = _access(issued)
    assert _incorporate(env, other.organization_id, foreign_node, [record]).unknown == 1
    (entry,) = _entries(env, record["jti"], "unknown_token_reported")
    assert entry["organization_id"] == other.organization_id
    # El nodo de otra organización no existe para el contexto de esta: nada se escribe.
    with pytest.raises(ResourceNotFound):
        _incorporate(env, site.organization_id, foreign_node, [record])


@pytest.mark.parametrize(
    "change",
    [
        {"role": "superuser"},
        {"role": "Coordinator_SST"},
        {"role": "coordinator_sst​"},
        {"role": None},
        {"outcome": "accepted"},
        {"opened_at": "2026-09-30T09:06:00.000Z"},
        {"extra": "texto libre"},
        {"jti": "no-es-un-uuid"},
    ],
)
def test_invalid_access_records_are_not_incorporated(
    env: LiveViewEnvironment, site: Any, change: dict[str, Any]
) -> None:
    """Rol fuera de la lista cerrada, campos de más o de menos, cierre antes de la apertura."""
    _, zone_id, node_id, _ = _fresh_zone(env, site)
    user = env.user_with_role(site.organization_id, Role.COORDINATOR_SST)
    issued = _issue(env, env.session_context(site.organization_id, user), zone_id)
    record = _access(issued, **change)
    good = _access(issued)
    report = _incorporate(env, site.organization_id, node_id, [record, good, 42])
    assert report == AccessReport(incorporated=1, invalid=2)
    assert len(_entries(env, good["jti"], "live_view_access_local")) == 1
    assert not _alerts(env, node_id)
