"""Entorno de prueba de ``identity.hierarchy`` (TASK-126) contra PostgreSQL 16 real.

``hierarchy_environment(endpoint, prefix)``: el entorno de autorización de TASK-125 (base migrada y
sembrada, ``shared.db`` como ``vigia_app``, constructores de contexto y ``Authorizer`` reales) con
el ``EscritorExpediente`` real (tipos de U-02 sincronizados en ``ledger.record_type``), la
auditoría y la bandeja, y los servicios de ``identity.application`` construidos sobre ellos.

Dobles deterministas, solo donde el módulo real tiene sus propias propiedades:

- ``ActivationPasswords``: la política acepta todo salvo ``REJECTED_PASSWORD`` (PR-NUC-08 prueba
  la real) y el hash es ``fake$`` más la contraseña (sin Argon2id de 64 MB por paso).
- ``ActivationSecondFactor``: sin KMS; la credencial pendiente se confirma solo con
  ``GOOD_CODE`` (PR-NUC-09 prueba el TOTP real).
- ``RecordingEmailSender``: el ``EmailSenderPort`` de U-04, que guarda lo enviado y responde lo que
  se le pida (``queued``, ``unavailable`` o una excepción).

Solo datos generados (NFR-CTR-43): correos ``@example.test`` y nombres sintéticos.
"""

from __future__ import annotations

import contextlib
import json
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Literal

from sqlalchemy import text

from tests.authz_support import AuthzEnvironment, authz_environment
from tests.factories import make_context
from tests.integration.conftest import PostgresEndpoint
from tests.session_support import GOOD_CODE
from vigia_platform.identity.application.common import IdentityDependencies
from vigia_platform.identity.application.hierarchy import HierarchyService, OrganizationGenesis
from vigia_platform.identity.application.invitations import (
    EmailSenderRegistry,
    InvitationEmail,
    InvitationService,
)
from vigia_platform.identity.application.privacy_notice import PrivacyNoticeService
from vigia_platform.identity.application.roles import RoleService
from vigia_platform.identity.application.users import UserService
from vigia_platform.identity.auth.passwords import (
    BreachSource,
    PasswordHash,
    PolicyResult,
    PolicyViolation,
)
from vigia_platform.identity.auth.second_factor import (
    EnrollmentChallenge,
    SecondFactorUser,
    TotpCredential,
)
from vigia_platform.ledger.application.writer import EscritorExpediente
from vigia_platform.ledger.evidence import EvidenceVerifier
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.ledger.record_types.u02 import U02_RECORD_TYPES
from vigia_platform.ledger.registry import RecordTypeRegistry
from vigia_platform.shared.context import ActorKind, ScopeContext
from vigia_platform.shared.storage import ObjectHead

LINK_BASE: Final = "https://app.vigia.example"
REJECTED_PASSWORD: Final = "clave-filtrada-1234"  # noqa: S105 - dato sintético de prueba
GOOD_PASSWORD: Final = "una-clave-sintetica-larga"  # noqa: S105 - dato sintético de prueba

_SAVE_RECORD_TYPE: Final = text(
    "INSERT INTO ledger.record_type (record_type, writer_unit, chain_level, schema_version,"
    " content_schema, source_key_path, free_text_paths, evidence_paths, label_rule,"
    " outbox_events, chain_follows_scope) VALUES (:record_type, :writer_unit, :chain_level,"
    " :schema_version, CAST(:content_schema AS jsonb), :source_key_path,"
    " CAST(:free_text_paths AS text[]), CAST(:evidence_paths AS text[]),"
    " CAST(:label_rule AS jsonb), CAST(:outbox_events AS text[]), :chain_follows_scope)"
    " ON CONFLICT DO NOTHING"
)


class NoStorage:
    """Los tipos de U-02 no tienen evidencias: el escritor nunca consulta el almacén."""

    async def head_object(self, key: str) -> ObjectHead | None:
        raise AssertionError("los registros de identidad no llevan evidencias")


class FakeActivationPasswords:
    """``ActivationPasswords`` determinista."""

    async def check_policy(self, password: str, email: str) -> PolicyResult:
        if password == REJECTED_PASSWORD:
            return PolicyResult((PolicyViolation.BREACHED,), BreachSource.LOCAL_LIST)
        return PolicyResult((), BreachSource.REMOTE)

    async def hash(self, password: str) -> PasswordHash:
        return PasswordHash(encoded=f"fake${password}", algorithm_version=1)


@dataclass
class FakeActivationSecondFactor:
    """``ActivationSecondFactor`` sin KMS: cada usuario tiene una credencial pendiente."""

    confirmed: set[uuid.UUID] = field(default_factory=set)
    enrolled: list[uuid.UUID] = field(default_factory=list)

    async def credential(self, context: ScopeContext, user_id: uuid.UUID) -> TotpCredential:
        return TotpCredential(
            user_id=user_id,
            organization_id=context.organization_id,
            secret_encrypted=b"x",
            data_key_wrapped=b"y",
            enrolled_at=datetime(2026, 9, 30, tzinfo=UTC),
            confirmed=user_id in self.confirmed,
        )

    async def enroll(self, context: ScopeContext, user: SecondFactorUser) -> EnrollmentChallenge:
        self.enrolled.append(user.user_id)
        credential = await self.credential(context, user.user_id)
        return EnrollmentChallenge(credential, "otpauth://sintetico", "<svg/>", ("A",) * 10)

    async def confirm_enrollment(
        self, context: ScopeContext, credential: TotpCredential, code: str, now: datetime
    ) -> bool:
        if code != GOOD_CODE:
            return False
        self.confirmed.add(credential.user_id)
        return True


@dataclass
class RecordingEmailSender:
    """``EmailSenderPort`` de prueba: guarda cada mensaje y responde ``result``."""

    result: Literal["queued", "unavailable", "raise"] = "queued"
    sent: list[InvitationEmail] = field(default_factory=list)

    async def send(
        self, context: ScopeContext, message: InvitationEmail
    ) -> Literal["queued", "unavailable"]:
        self.sent.append(message)
        if self.result == "raise":
            raise ConnectionError("proveedor de correo caído (sintético)")
        return self.result

    def circuit_state(self) -> Literal["closed", "open", "half_open"]:
        return "closed" if self.result == "queued" else "open"


@dataclass
class HierarchyEnvironment:
    authz: AuthzEnvironment
    deps: IdentityDependencies
    writer: EscritorExpediente
    senders: EmailSenderRegistry
    second_factor: FakeActivationSecondFactor

    def run(self, awaitable: Any) -> Any:
        return self.authz.run(awaitable)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.authz.fetch(sql, *args)

    def advance(self, seconds: float) -> None:
        self.authz.sessions.clock.advance(seconds)

    # --- Servicios ----------------------------------------------------------------------------

    def hierarchy(self) -> HierarchyService:
        return HierarchyService(self.deps)

    def roles(self) -> RoleService:
        return RoleService(self.deps)

    def users(self, senders: EmailSenderRegistry | None = None) -> UserService:
        return UserService(self.deps, senders=senders or self.senders, link_base=LINK_BASE)

    def genesis(self, senders: EmailSenderRegistry | None = None) -> OrganizationGenesis:
        return OrganizationGenesis(
            self.deps,
            senders=senders or self.senders,
            link_base=LINK_BASE,
        )

    def invitations(self) -> InvitationService:
        return InvitationService(
            self.deps,
            contexts=self.authz.contexts,
            passwords=FakeActivationPasswords(),
            second_factor=self.second_factor,
        )

    def privacy_notice(self) -> PrivacyNoticeService:
        return PrivacyNoticeService(self.deps)

    # --- Contextos ----------------------------------------------------------------------------

    def operator_context(self) -> ScopeContext:
        context: ScopeContext = self.run(
            self.authz.contexts.context_from_operator(self.authz.operator_id)
        )
        return context

    def session_context(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> ScopeContext:
        """El contexto real de una sesión nueva de ``user_id`` (constructor de TASK-125)."""
        cookie = self.authz.open_session(organization_id, user_id)
        scope = self.run(self.authz.contexts.context_from_session(cookie))
        context: ScopeContext = scope.context
        return context


def new_email(label: str = "persona") -> str:
    return f"{label}-{secrets.token_hex(6)}@example.test"


def new_code(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(4).upper()}"


@contextlib.contextmanager
def hierarchy_environment(
    endpoint: PostgresEndpoint, prefix: str
) -> Iterator[HierarchyEnvironment]:
    """``authz_environment`` con el escritor real y los servicios de jerarquía."""
    with authz_environment(endpoint, prefix) as authz:
        sessions = authz.sessions
        registry = RecordTypeRegistry()
        for definition in U02_RECORD_TYPES:
            registry.register(definition)

        async def synchronize() -> None:
            system = make_context(kind=ActorKind.SYSTEM)
            async with sessions.database.transaction(system) as transaction:
                for compiled in registry.latest():
                    row = compiled.to_persisted()
                    await transaction.execute(
                        _SAVE_RECORD_TYPE,
                        {
                            "record_type": row.record_type,
                            "writer_unit": row.writer_unit,
                            "chain_level": row.chain_level,
                            "schema_version": row.schema_version,
                            "content_schema": json.dumps(row.content_schema),
                            "source_key_path": row.source_key_path,
                            "free_text_paths": list(row.free_text_paths),
                            "evidence_paths": list(row.evidence_paths),
                            "label_rule": None
                            if row.label_rule is None
                            else json.dumps(row.label_rule),
                            "outbox_events": list(row.outbox_events),
                            "chain_follows_scope": row.chain_follows_scope,
                        },
                    )
            registry.seal()

        sessions.run(synchronize())
        free_text = FreeTextPolicyRegistry()
        writer = EscritorExpediente(
            database=sessions.database,
            registry=registry,
            free_text=free_text,
            evidence=EvidenceVerifier(NoStorage(), sessions.clock),
            outbox=sessions.outbox,
            clock=sessions.clock,
        )
        deps = IdentityDependencies(
            database=sessions.database,
            writer=writer,
            audit=sessions.audit,
            outbox=sessions.outbox,
            authorizer=authz.authorizer,
            free_text=free_text,
            clock=sessions.clock,
            provider_organization_id=authz.provider_organization_id,
        )
        yield HierarchyEnvironment(
            authz, deps, writer, EmailSenderRegistry(), FakeActivationSecondFactor()
        )
