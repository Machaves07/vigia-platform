"""Entorno de prueba de sesiones e inicio de sesión (TASK-124) contra PostgreSQL 16 real.

- ``session_environment(endpoint, prefix)``: base migrada con la proveedora sembrada, el
  catálogo de la bandeja sincronizado (``security_alert`` publicable), ``shared.db`` como
  ``vigia_app`` (nunca superusuario) y un reloj simulado.
- ``TestContexts``: los contextos del inicio de sesión que en producción construye
  ``identity.authz.context`` (TASK-125); aquí con el constructor privado, como ``tests/factories``.
- ``FakePasswords`` y ``FakeSecondFactor``: dobles deterministas (sin Argon2id de 64 MB ni KMS)
  para las propiedades con estado; los módulos reales tienen sus propias pruebas (PR-NUC-08, 09).
- ``SessionEnvironment.add_organization`` y ``add_user``: organizaciones y usuarios nuevos,
  escritos como superusuario (datos generados, NFR-CTR-43).
"""

from __future__ import annotations

import contextlib
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import asyncpg  # type: ignore[import-untyped]

from tests.factories import make_context, uuid7
from tests.identity_db import BASE_TIME, IdentitySeed, MigratedDatabase, seeded_identity
from tests.integration.conftest import PostgresEndpoint
from tests.ledger_database import DatabaseLoop
from tests.outbox_support import app_database
from vigia_platform.identity.adapters.second_factor_store import PostgresSecondFactorStore
from vigia_platform.identity.adapters.session_store import PostgresSessionStore
from vigia_platform.identity.auth.login import LoginService
from vigia_platform.identity.auth.passwords import PasswordHash, VerifyResult
from vigia_platform.identity.auth.second_factor import (
    EnrollmentChallenge,
    SecondFactorUser,
    TotpCredential,
)
from vigia_platform.identity.auth.sessions import SessionService
from vigia_platform.identity.domain.privacy_notice import CURRENT_PRIVACY_NOTICE_VERSION
from vigia_platform.ledger.application.audit_writer import AuditWriter
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import (
    Actor,
    ActorKind,
    ActorUnit,
    ContextOrigin,
    ScopeContext,
    _seal_scope_context,
)
from vigia_platform.shared.db import Database
from vigia_platform.shared.outbox.publish import Outbox
from vigia_platform.shared.outbox.registries import OutboxCatalog
from vigia_platform.shared.outbox.store import SqlOutboxCatalogStore
from vigia_platform.shared.outbox.u02_events import register_u02_event_types

START: Final = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)
ORIGIN_KEY: Final = b"k" * 32
"""Clave sintética del hash de origen."""
SYSTEM_ACTOR_ID: Final = uuid.UUID("00000000-0000-4000-8000-000000000124")
GOOD_CODE: Final = "246810"
"""El único código que acepta ``FakeSecondFactor``."""


class TestContexts:
    """``LoginContexts`` y ``SessionContexts`` de prueba (TASK-125 aporta los reales)."""

    __test__ = False

    def anonymous(self, organization_id: uuid.UUID) -> ScopeContext:
        actor = Actor(
            kind=ActorKind.SYSTEM,
            id=SYSTEM_ACTOR_ID,
            display_name_snapshot="Inicio de sesión",
            unit=ActorUnit.U02,
        )
        return _seal_scope_context(
            organization_id=organization_id,
            actor=actor,
            origin=ContextOrigin.OUTBOX_EVENT,
            allowed_scopes=(),
            correlation_id=uuid7(),
        )

    def for_user(
        self,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        display_name: str,
        session_id_hash: str,
    ) -> ScopeContext:
        actor = Actor(
            kind=ActorKind.USER,
            id=user_id,
            display_name_snapshot=display_name,
            unit=ActorUnit.U02,
        )
        return _seal_scope_context(
            organization_id=organization_id,
            actor=actor,
            origin=ContextOrigin.SESSION,
            allowed_scopes=(),
            correlation_id=uuid7(),
            session_id_hash=session_id_hash,
        )


class FakePasswords:
    """``PasswordPort`` determinista: el hash es ``fake$`` más la contraseña."""

    def __init__(self) -> None:
        self.verify_calls = 0
        self.hash_calls = 0

    async def verify(self, password: str, encoded: str) -> VerifyResult:
        self.verify_calls += 1
        return VerifyResult(ok=encoded == f"fake${password}", needs_rehash=False)

    async def hash(self, password: str) -> PasswordHash:
        self.hash_calls += 1
        return PasswordHash(encoded=f"fake${password}", algorithm_version=1)


class FakeSecondFactor:
    """``SecondFactorPort`` determinista: acepta solo ``GOOD_CODE``; nunca toca la base."""

    async def credential(self, context: ScopeContext, user_id: uuid.UUID) -> TotpCredential:
        return TotpCredential(
            user_id=user_id,
            organization_id=context.organization_id,
            secret_encrypted=b"x",
            data_key_wrapped=b"y",
            enrolled_at=START,
            confirmed=True,
        )

    async def verify_totp(
        self, context: ScopeContext, credential: TotpCredential, code: str, now: datetime
    ) -> bool:
        return code == GOOD_CODE

    async def consume_recovery_code(
        self, context: ScopeContext, credential: TotpCredential, code: str
    ) -> bool:
        return False

    async def enroll(self, context: ScopeContext, user: SecondFactorUser) -> EnrollmentChallenge:
        raise NotImplementedError

    async def confirm_enrollment(
        self, context: ScopeContext, credential: TotpCredential, code: str, now: datetime
    ) -> bool:
        return code == GOOD_CODE


@dataclass(frozen=True)
class User:
    user_id: uuid.UUID
    organization_id: uuid.UUID
    email: str
    password: str = field(repr=False)


@dataclass
class SessionEnvironment:
    loop: DatabaseLoop
    migrated: MigratedDatabase
    seed: IdentitySeed
    database: Database
    audit: AuditWriter
    outbox: Outbox
    clock: SimulatedClock
    admin: Any
    """Conexión de superusuario (solo para sembrar y para leer lo que la RLS oculta)."""
    contexts: TestContexts = field(default_factory=TestContexts)

    def run(self, awaitable: Any) -> Any:
        return self.loop.run(awaitable)

    def store(self, database: Database | None = None) -> PostgresSessionStore:
        return PostgresSessionStore(database or self.database, self.audit, self.outbox)

    def sessions(self, database: Database | None = None) -> SessionService:
        return SessionService(self.store(database), self.contexts, self.clock)

    def login(
        self,
        *,
        passwords: Any | None = None,
        second_factor: Any | None = None,
        database: Database | None = None,
    ) -> LoginService:
        store = self.store(database)
        return LoginService(
            store=store,
            sessions=store,
            passwords=passwords if passwords is not None else FakePasswords(),
            second_factor=second_factor if second_factor is not None else FakeSecondFactor(),
            contexts=self.contexts,
            clock=self.clock,
            provider_organization_id=self.seed.provider_organization_id,
            origin_key=ORIGIN_KEY,
        )

    def second_factor_store(self) -> PostgresSecondFactorStore:
        return PostgresSecondFactorStore(self.database, self.audit)

    def add_organization(self) -> uuid.UUID:
        organization_id = uuid.uuid4()

        async def insert() -> None:
            await self.admin.execute(
                "INSERT INTO identity.organization"
                " (organization_id, code, name, kind, created_at, created_by)"
                " VALUES ($1, $2, 'Organización sintética', 'client', $3, $4)",
                organization_id,
                f"ORG-{secrets.token_hex(4).upper()}",
                BASE_TIME,
                self.seed.operator_id,
            )

        self.run(insert())
        return organization_id

    def add_user(
        self,
        organization_id: uuid.UUID,
        *,
        required: bool = False,
        enrolled: bool = False,
        status: str = "active",
        password_hash: str | None = None,
        with_password: bool = True,
        privacy_notice: str | None = CURRENT_PRIVACY_NOTICE_VERSION,
    ) -> User:
        """Usuario nuevo; su hash es el de ``FakePasswords`` salvo que se dé ``password_hash``.

        Por defecto ya aceptó la versión vigente del aviso (sin ella no hay contexto de sesión,
        NFR-NUC-29); ``privacy_notice=None`` lo deja sin aceptar.
        """
        user_id = uuid.uuid4()
        email = f"persona-{secrets.token_hex(6)}@example.test"
        password = f"clave-{secrets.token_hex(6)}"

        async def insert() -> None:
            async with self.admin.transaction():
                await self.admin.execute(
                    "INSERT INTO identity.user_account (user_id, organization_id, email,"
                    " display_name, status, second_factor_required, second_factor_enrolled_at,"
                    " created_at, deactivated_at, privacy_notice_version_accepted)"
                    " VALUES ($1, $2, $3, 'Persona sintética', $4, $5, $6, $7,"
                    " CASE WHEN $4 = 'deactivated' THEN $7::timestamptz END, $8)",
                    user_id,
                    organization_id,
                    email,
                    status,
                    required,
                    BASE_TIME if enrolled else None,
                    BASE_TIME,
                    privacy_notice,
                )
                if with_password:
                    await self.admin.execute(
                        "INSERT INTO identity.password_credential"
                        " (user_id, organization_id, password_hash, algorithm_version, updated_at)"
                        " VALUES ($1, $2, $3, 'argon2id-v1', $4)",
                        user_id,
                        organization_id,
                        password_hash if password_hash is not None else f"fake${password}",
                        BASE_TIME,
                    )

        self.run(insert())
        return User(user_id, organization_id, email, password)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return list(self.run(self.admin.fetch(sql, *args)))


def synchronized_outbox(loop: DatabaseLoop, database: Database, clock: SimulatedClock) -> Outbox:
    catalog = OutboxCatalog()
    register_u02_event_types(catalog.event_types)

    async def synchronize() -> None:
        async with database.transaction(make_context(kind=ActorKind.SYSTEM)) as transaction:
            await catalog.synchronize(SqlOutboxCatalogStore(transaction), clock)

    loop.run(synchronize())
    return Outbox(catalog, clock)


@contextlib.contextmanager
def session_environment(endpoint: PostgresEndpoint, prefix: str) -> Iterator[SessionEnvironment]:
    """Base migrada y sembrada, bandeja sincronizada y ``shared.db`` como ``vigia_app``."""
    with seeded_identity(endpoint, prefix) as (migrated, seed):
        loop = DatabaseLoop()
        database = app_database(migrated, worker_pool_size=4)
        clock = SimulatedClock(START)
        admin = loop.run(asyncpg.connect(migrated.as_role().dsn))
        try:
            audit = AuditWriter(
                database=database,
                clock=clock,
                provider_organization_id=seed.provider_organization_id,
            )
            outbox = synchronized_outbox(loop, database, clock)
            yield SessionEnvironment(loop, migrated, seed, database, audit, outbox, clock, admin)
        finally:
            loop.run(admin.close())
            loop.run(database.dispose())
            loop.close()
