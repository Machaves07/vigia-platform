"""``vigia-api`` real del arnés de resiliencia (LC-NUC-34; NFR-NUC-06; FS-NUC-05 a).

``python -m tests.resilience.api_process`` sirve con uvicorn la aplicación de ``create_app``: la
cadena fija de middleware, las rutas de ``platform_units()`` y el arranque supervisado
(PAT-NUC-RES-02), con la composición de ``identity`` de ``tests/examples/test_auth_routes.py``
sobre PostgreSQL de verdad como ``vigia_app`` con los ajustes de producción de ``vigia-api``:
sesiones, retardo de fallos, aviso de tratamiento y contextos en la base, nada en memoria salvo
lo que NFR-NUC-06 permite (claves y cubos del límite de tasa). Dobles deterministas solo donde el
módulo real tiene sus propias pruebas: el hash de contraseñas (``fake$``, sin Argon2id de 64 MB)
y el segundo factor.

Firma y KMS, según ``VIGIA_TEST_SIGNING``:

- ``stub`` (por defecto): los dobles de ``tests.worker_support``, que pasan sus comprobaciones.
- ``localstack``: ``SigningService`` con el material en el Secrets Manager de LocalStack y
  ``KmsAdapter``. El proceso da de alta sus claves por ``VIGIA_TEST_BOOTSTRAP_SECRETS_URL``
  (LocalStack directo) y luego arranca la aplicación contra ``VIGIA_TEST_SECRETS_URL`` y
  ``VIGIA_TEST_KMS_URL``, que el escenario puede bloquear (FS-NUC-05 a): si no hay gestor ni
  KMS, el proceso nunca queda listo y termina con ``STARTUP_FAILURE_EXIT_CODE``.

Variables: ``VIGIA_TEST_DATABASE_URL``, ``VIGIA_TEST_PROVIDER_ORGANIZATION``, ``VIGIA_TEST_PORT``,
``VIGIA_TEST_STARTUP_DEADLINE`` y las de firma. Solo datos generados.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from collections.abc import Mapping
from typing import Any, Final

import uvicorn

from tests.authz_support import SYSTEM_ACTOR_ID
from tests.examples.test_auth_routes import ORIGIN, STATIC
from tests.hierarchy_support import FakeActivationPasswords, FakeActivationSecondFactor, NoStorage
from tests.integration.conftest import LOCALSTACK_ACCESS_KEY_ID, LOCALSTACK_SECRET_ACCESS_KEY
from tests.resilience.processes import EffectHandler, resilience_catalog
from tests.session_support import ORIGIN_KEY, FakePasswords, FakeSecondFactor
from tests.signing_support import (
    BOOTSTRAP_ORDER,
    PROVIDER_ORGANIZATION_ID,
    InMemoryKeyStore,
    RecordingEvents,
    provider_context,
)
from tests.worker_support import ProbeTask, StubKms, StubSigning, StubStorage, synchronize
from vigia_platform.identity.adapters.authz_store import (
    LedgerProviderQueryLedger,
    PostgresAuthorizationAudit,
    PostgresContextStore,
)
from vigia_platform.identity.adapters.http import IdentityHttp
from vigia_platform.identity.adapters.session_store import PostgresSessionStore
from vigia_platform.identity.application.common import IdentityDependencies
from vigia_platform.identity.application.invitations import InvitationService
from vigia_platform.identity.application.me import MeService
from vigia_platform.identity.application.password_change import PasswordChangeService
from vigia_platform.identity.application.privacy_notice import PrivacyNoticeService
from vigia_platform.identity.auth.login import LoginService
from vigia_platform.identity.auth.sessions import SessionService
from vigia_platform.identity.authz.authorize import Authorizer
from vigia_platform.identity.authz.context import ScopeContexts
from vigia_platform.ledger.application.audit_writer import AuditWriter
from vigia_platform.ledger.application.writer import EscritorExpediente
from vigia_platform.ledger.evidence import EvidenceVerifier
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.ledger.record_types.u02 import U02_RECORD_TYPES
from vigia_platform.ledger.registry import RecordTypeRegistry
from vigia_platform.shared.api.app import AppConfig, AppRuntime, create_app
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.clock import SimulatedClock, SystemClock
from vigia_platform.shared.db import Database, DatabaseSettings, ProcessKind, SslMode
from vigia_platform.shared.observability.logging import configure_logging
from vigia_platform.shared.outbox.publish import Outbox
from vigia_platform.shared.secrets import (
    AwsCredentials,
    AwsSettings,
    KmsAdapter,
    SecretsManagerAdapter,
)
from vigia_platform.shared.signing import SigningService

SENTINEL: Final = "health/ready-sentinel"
SIGNING_ENVIRONMENT_VARIABLE: Final = "VIGIA_TEST_SIGNING_ENVIRONMENT"
_CLOCK = SystemClock()


def _aws(url: str) -> AwsSettings:
    return AwsSettings(
        region="us-east-1",
        endpoint_url=url,
        credentials=AwsCredentials(LOCALSTACK_ACCESS_KEY_ID, LOCALSTACK_SECRET_ACCESS_KEY),
    )


async def _signing(environ: Mapping[str, str]) -> tuple[Any, Any]:
    """Firma y KMS del proceso: dobles, o los de verdad sobre LocalStack (FS-NUC-05 a).

    Las claves son de la organización proveedora de ``tests.signing_support`` (la firma no lee la
    base: su almacén de claves está en memoria, NFR-NUC-06).
    """
    if environ.get("VIGIA_TEST_SIGNING", "stub") != "localstack":
        return StubSigning(), StubKms()
    environment = environ[SIGNING_ENVIRONMENT_VARIABLE]
    store = InMemoryKeyStore()
    bootstrap_clock = SimulatedClock(_CLOCK.now())
    bootstrap = SigningService(
        provider_organization_id=PROVIDER_ORGANIZATION_ID,
        store=store,
        secrets=SecretsManagerAdapter(
            _aws(environ["VIGIA_TEST_BOOTSTRAP_SECRETS_URL"]), bootstrap_clock
        ),
        events=RecordingEvents(),
        clock=bootstrap_clock,
        environment=environment,
    )
    await bootstrap.start(required=())
    for purpose in BOOTSTRAP_ORDER:
        await bootstrap.rotate(purpose, context=provider_context())
    signing = SigningService(
        provider_organization_id=PROVIDER_ORGANIZATION_ID,
        store=store,
        secrets=SecretsManagerAdapter(_aws(environ["VIGIA_TEST_SECRETS_URL"]), _CLOCK),
        events=RecordingEvents(),
        clock=_CLOCK,
        environment=environment,
    )
    return signing, KmsAdapter(_aws(environ["VIGIA_TEST_KMS_URL"]))


def build(environ: Mapping[str, str]) -> Any:
    provider = uuid.UUID(environ["VIGIA_TEST_PROVIDER_ORGANIZATION"])
    database = Database.create(
        DatabaseSettings(
            url=environ["VIGIA_TEST_DATABASE_URL"],
            process=ProcessKind.API,
            sslmode=SslMode.DISABLE,  # el contenedor local no tiene TLS
        )
    )
    catalog = resilience_catalog(ProbeTask(), EffectHandler("api", {}))
    outbox = Outbox(catalog, _CLOCK)
    audit = AuditWriter(database=database, clock=_CLOCK, provider_organization_id=provider)
    contexts = ScopeContexts(
        store=PostgresContextStore(database),
        clock=_CLOCK,
        provider_organization_id=provider,
        system_actor_id=SYSTEM_ACTOR_ID,
    )
    authorization_audit = PostgresAuthorizationAudit(
        database=database, audit=audit, outbox=outbox, clock=_CLOCK
    )
    authorizer = Authorizer(audit=authorization_audit, provider_organization_id=provider)
    registry = RecordTypeRegistry()
    for definition in U02_RECORD_TYPES:
        registry.register(definition)
    registry.seal()  # los tipos de U-02 ya están en ``ledger.record_type`` (los sincronizó la prueba)
    free_text = FreeTextPolicyRegistry()
    writer = EscritorExpediente(
        database=database,
        registry=registry,
        free_text=free_text,
        evidence=EvidenceVerifier(NoStorage(), _CLOCK),
        outbox=outbox,
        clock=_CLOCK,
    )
    deps = IdentityDependencies(
        database=database,
        writer=writer,
        audit=audit,
        outbox=outbox,
        authorizer=authorizer,
        free_text=free_text,
        clock=_CLOCK,
        provider_organization_id=provider,
    )
    store = PostgresSessionStore(database, audit, outbox)
    passwords = FakePasswords()
    identity = IdentityHttp(
        login=LoginService(
            store=store,
            sessions=store,
            passwords=passwords,
            second_factor=FakeSecondFactor(),
            contexts=contexts,
            clock=_CLOCK,
            provider_organization_id=provider,
            origin_key=ORIGIN_KEY,
        ),
        sessions=SessionService(store, contexts, _CLOCK),
        invitations=InvitationService(
            deps,
            contexts=contexts,
            passwords=FakeActivationPasswords(),
            second_factor=FakeActivationSecondFactor(),
        ),
        privacy_notice=PrivacyNoticeService(deps),
        passwords=PasswordChangeService(
            database=database,
            audit=audit,
            passwords=passwords,
            throttle=store,
            contexts=contexts,
            clock=_CLOCK,
        ),
        me=MeService(database, contexts=contexts, provider_organization_id=provider),
        provider_organization_id=provider,
    )
    signing, kms = asyncio.run(_signing(environ))

    async def synchronize_catalog() -> None:
        await synchronize(database, catalog, _CLOCK)

    config = AppConfig(
        environment="test",
        data_key_id=environ.get("VIGIA_SECRETS_KEY_ARN", "alias/vigia-secrets"),
        health_sentinel_key=SENTINEL,
        static_dir=STATIC,
        public_origin=ORIGIN,
        startup_deadline_seconds=float(environ.get("VIGIA_TEST_STARTUP_DEADLINE", "60")),
        startup_retry_seconds=0.5,
    )
    runtime = AppRuntime(
        clock=_CLOCK,
        database=database,
        storage=StubStorage(),
        signing=signing,
        kms=kms,
        registries=(synchronize_catalog,),
        authorizer=ContextAuthorizer(
            audit=authorization_audit,
            provider_organization_id=provider,
            provider_queries=LedgerProviderQueryLedger(writer),
            clock=_CLOCK,
        ),
        sessions=contexts,
        origin_secret=ORIGIN_KEY,
        identity=identity,
    )
    return create_app(config, runtime=runtime)


def run(environ: Mapping[str, str]) -> None:
    configure_logging()
    app = build(environ)
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(environ["VIGIA_TEST_PORT"]),
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    run(os.environ)
    sys.exit(0)
