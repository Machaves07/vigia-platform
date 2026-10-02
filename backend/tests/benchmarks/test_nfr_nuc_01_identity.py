"""Bancos de identidad de NFR-NUC-01 (TASK-142, VIG-91): sesión, contexto y vista en vivo.

Sobre PostgreSQL 16 en contenedor, como ``vigia_app``:

=========================  ================================================  ==========
banco                      operación                                         objetivo
=========================  ================================================  ==========
``auth_login``             ``LoginService.authenticate`` con Argon2id real   p95 700 ms
``authz_context``          ``ScopeContexts.context_from_session``            sin cifra
``live_view_token``        ``LiveViewTokenService.issue`` (firma Ed25519)    p95 100 ms
=========================  ================================================  ==========

El inicio de sesión verifica la contraseña con los parámetros vigentes (Argon2id v1: 64 MiB,
t = 3, p = 2) en el pool de CPU y crea la sesión; sin segundo factor (el usuario no lo tiene
exigido). El contexto se construye desde una sesión abierta con un rol de planta. La emisión de
token respeta el tope de 30 por usuario cada 10 minutos: la preparación cambia de usuario cada
25 emisiones, fuera de la medida. Solo datos generados. Solo corre con
``--hypothesis-profile=nightly``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from tests.benchmarks.conftest import Measure
from tests.integration.conftest import PostgresEndpoint
from tests.integration.test_login_sessions import NeverBreached
from tests.live_view_support import LiveViewEnvironment, live_view_environment
from tests.session_support import SessionEnvironment, session_environment
from vigia_platform.identity.auth.login import Authenticated
from vigia_platform.identity.auth.passwords import PasswordService, hash_password
from vigia_platform.shared.context import Role, ScopeContext, ScopeLevel
from vigia_platform.shared.cpu_pool import CpuPool

pytestmark = [pytest.mark.nightly, pytest.mark.integration]

PASSWORD = "contraseña sintética del banco 2026"  # noqa: S105 - contraseña sintética
TOKENS_PER_USER = 25
"""Por debajo del tope de 30 emisiones por usuario en 10 minutos."""


@pytest.fixture(scope="module")
def sessions(postgres_endpoint: PostgresEndpoint) -> Iterator[SessionEnvironment]:
    with session_environment(postgres_endpoint, "bench_login") as environment:
        yield environment


@pytest.fixture(scope="module")
def live_view(postgres_endpoint: PostgresEndpoint) -> Iterator[LiveViewEnvironment]:
    with live_view_environment(postgres_endpoint, "bench_live_view") as environment:
        yield environment


def test_login_with_argon2id(sessions: SessionEnvironment, measure: Measure) -> None:
    organization = sessions.add_organization()
    user = sessions.add_user(organization, password_hash=hash_password(PASSWORD).encoded)
    pool = CpuPool(sessions.clock, max_workers=2)
    try:
        login = sessions.login(passwords=PasswordService(NeverBreached(), pool))

        def target() -> None:
            result = sessions.run(login.authenticate(user.email, PASSWORD, "198.51.100.40"))
            assert isinstance(result, Authenticated), result

        measure(
            "auth_login",
            "Inicio de sesión con Argon2id (64 MiB, t = 3, p = 2)",
            target,
            objective_ms=700,
        )
    finally:
        pool.shutdown()


def test_context_from_session(live_view: LiveViewEnvironment, measure: Measure) -> None:
    site = live_view.add_site(plants=1, zones_per_plant=3)
    plant_id = next(iter(site.plants))
    user = live_view.user_with_role(
        site.organization_id, Role.PLANT_MANAGER, ScopeLevel.PLANT, plant_id
    )
    cookie = live_view.authz.open_session(site.organization_id, user)

    def target() -> None:
        scope = live_view.run(live_view.authz.contexts.context_from_session(cookie))
        assert isinstance(scope.context, ScopeContext)

    measure("authz_context", "Contexto de alcance desde la sesión", target)


def test_live_view_token_issuance(live_view: LiveViewEnvironment, measure: Measure) -> None:
    site = live_view.add_site(plants=1, zones_per_plant=1)
    ((plant_id, zone_id),) = site.zones()
    node_id = live_view.add_node(site.organization_id, plant_id)
    live_view.assign_node(site.organization_id, plant_id, zone_id, node_id)
    service = live_view.service()
    contexts: list[Any] = []
    issued = 0

    def setup() -> None:
        nonlocal issued
        if issued % TOKENS_PER_USER == 0:
            user = live_view.user_with_role(site.organization_id, Role.COORDINATOR_SST)
            contexts.append(live_view.session_context(site.organization_id, user))
        issued += 1

    def target() -> None:
        live_view.run(service.issue(contexts[-1], zone_id))

    measure(
        "live_view_token",
        "Emisión de token de vista en vivo (JWS Ed25519)",
        target,
        setup=setup,
        objective_ms=100,
    )
