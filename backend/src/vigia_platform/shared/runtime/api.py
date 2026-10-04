"""Constructor de producción de ``vigia-api`` (A-52; LC-NUC-19; PAT-NUC-RES-02).

``VIGIA_API_RUNTIME=vigia_platform.shared.runtime.api:build_api_runtime``. Lee ``RuntimeConfig``
una vez del entorno y devuelve el ``AppRuntime`` que ``create_app`` necesita, sin comprobar nada
contra la red salvo la credencial de la base: las comprobaciones de arranque (base, claves,
clave de datos, registros y centinela) las hace el ``StartupSupervisor`` en segundo plano.

Construye:

- ``Database`` de ``vigia_app`` con los pools ``node`` y ``person`` (``VIGIA_DB_POOL_NODE`` y
  ``VIGIA_DB_POOL_PERSON``, 10 y 5, sin desbordamiento, espera de 5 s) y la credencial rotable de
  ``VIGIA_DB_APP_SECRET`` (runbook 6.6);
- el ``StoragePort`` de S3 sobre ``VIGIA_EVIDENCE_BUCKET`` (también el centinela de salud), KMS,
  ``SigningService`` con ``LedgerRotationRecorder`` y la infraestructura común
  (``shared.runtime.core``);
- ``CpuPool`` de ``VIGIA_THREADPOOL_SIZE`` (Argon2id, TOTP, firmas), ``RateLimiter``,
  los mamparos por clase de ruta de ``VIGIA_BULKHEAD_NODE`` y ``VIGIA_BULKHEAD_PERSON``
  (``Bulkheads``, LC-GOB-20), ``ScopeContexts`` y el autorizador por ruta
  (``ContextAuthorizer``);
- los servicios de las rutas: ``IdentityHttp``, ``LedgerHttp``, ``PlatformHttp`` y los que cada
  unidad deja en ``app.state`` (``units.api_state``);
- los sincronizadores de tipos de registro y del catálogo de la bandeja (``registries``).

La clave del HMAC del origen de red (límite de inicios de sesión por origen y ``csrf_rejected``)
es aleatoria por proceso, como la del limitador de peticiones: ningún volcado contiene
direcciones en claro.

Falla al construir (y ``vigia-api`` sale con ``STARTUP_FAILURE_EXIT_CODE`` sin abrir el puerto)
si falta una variable obligatoria, si el secreto de la base no existe o no tiene la forma de RDS,
si falta ``VIGIA_PUBLIC_ORIGIN`` o si no se puede leer el respaldo local de contraseñas
filtradas (``VIGIA_BREACH_LIST_PATH``). Ningún mensaje lleva un valor de secreto.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from vigia_platform.identity.adapters.authz_store import LedgerProviderQueryLedger
from vigia_platform.identity.adapters.concession_store import PostgresConcessionStore
from vigia_platform.identity.adapters.hibp import (
    DEFAULT_LOCAL_LIST_PATH,
    HibpBreachChecker,
    LocalBreachList,
)
from vigia_platform.identity.adapters.http import IdentityHttp
from vigia_platform.identity.adapters.second_factor_store import PostgresSecondFactorStore
from vigia_platform.identity.adapters.session_store import PostgresSessionStore
from vigia_platform.identity.application.common import IdentityDependencies
from vigia_platform.identity.application.concessions import ConcessionService
from vigia_platform.identity.application.hierarchy import HierarchyService
from vigia_platform.identity.application.invitations import (
    EmailSenderRegistry,
    InvitationService,
)
from vigia_platform.identity.application.me import MeService
from vigia_platform.identity.application.organization import OrganizationSettingsService
from vigia_platform.identity.application.password_change import PasswordChangeService
from vigia_platform.identity.application.privacy_notice import PrivacyNoticeService
from vigia_platform.identity.application.roles import RoleService
from vigia_platform.identity.application.users import SecondFactorResetService, UserService
from vigia_platform.identity.auth.login import LoginService
from vigia_platform.identity.auth.passwords import PasswordService
from vigia_platform.identity.auth.second_factor import SecondFactorService
from vigia_platform.identity.auth.sessions import SessionService
from vigia_platform.ledger.adapters.http import LedgerHttp
from vigia_platform.ledger.adapters.integrity_store import SqlIntegrityResults
from vigia_platform.ledger.application.coverage import CoverageService
from vigia_platform.ledger.application.evidence_read import EvidenceService
from vigia_platform.ledger.application.integrity_requests import IntegrityRequests
from vigia_platform.ledger.application.labels import LabelService
from vigia_platform.ledger.application.reader import LectorExpediente
from vigia_platform.shared.adapters.http import PlatformHttp
from vigia_platform.shared.api.app import AppConfig, AppRuntime
from vigia_platform.shared.api.middleware import AuditCsrfRejections, ContextAuthorizer
from vigia_platform.shared.bulkheads import Bulkheads
from vigia_platform.shared.clock import Clock, SystemClock
from vigia_platform.shared.cpu_pool import CpuPool
from vigia_platform.shared.crypto import EnvelopeCipher
from vigia_platform.shared.db import ProcessKind
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics
from vigia_platform.shared.outbox.replay import DeadLetterReplay
from vigia_platform.shared.ratelimit import RateLimiter
from vigia_platform.shared.runtime.config import RuntimeConfig, RuntimeConfigInvalid
from vigia_platform.shared.runtime.core import (
    build_core,
    load_credentials,
    open_database,
    s3_storage,
)
from vigia_platform.shared.runtime.db_credentials import SecretStringReader
from vigia_platform.shared.runtime.units import PlatformUnit, api_state, registered_units
from vigia_platform.shared.tokens import LiveViewTokenService

__all__ = ["ORIGIN_KEY_BYTES", "build_api_runtime", "compose_api_runtime"]

ORIGIN_KEY_BYTES = 32
"""Longitud de la clave del HMAC del origen de red (``ORIGIN_KEY_MIN_BYTES`` de ``login``)."""


async def build_api_runtime(config: AppConfig) -> AppRuntime:
    """El constructor de ``VIGIA_API_RUNTIME``: ``RuntimeConfig`` del entorno del proceso."""
    return await compose_api_runtime(config, RuntimeConfig.from_environ(os.environ))


def _breach_list(runtime: RuntimeConfig) -> LocalBreachList:
    path = Path(runtime.breach_list_path) if runtime.breach_list_path else DEFAULT_LOCAL_LIST_PATH
    try:
        return LocalBreachList.from_file(path)
    except (OSError, ValueError):
        raise RuntimeConfigInvalid(
            "VIGIA_BREACH_LIST_PATH",
            "no permite leer el respaldo local de contraseñas filtradas",
        ) from None


async def compose_api_runtime(
    config: AppConfig,
    runtime: RuntimeConfig,
    *,
    clock: Clock | None = None,
    metrics: PlatformMetrics | None = None,
    units: Sequence[PlatformUnit] | None = None,
    reader: SecretStringReader | None = None,
) -> AppRuntime:
    """El ``AppRuntime`` de ``config`` y ``runtime`` (las pruebas inyectan reloj y lector)."""
    clock = clock if clock is not None else SystemClock()
    metrics = metrics if metrics is not None else get_metrics()
    selected = tuple(units) if units is not None else registered_units()
    provider = runtime.require("provider_organization_id")
    evidence_bucket: str = runtime.require("evidence_bucket")
    link_base = config.public_origin
    if link_base is None:
        raise RuntimeConfigInvalid("VIGIA_PUBLIC_ORIGIN", "ausente: este proceso la exige")
    breach_list = _breach_list(runtime)
    credentials = await load_credentials(runtime, reader)
    database = open_database(runtime, ProcessKind.API, credentials, metrics)
    cpu_pool = CpuPool(clock, max_workers=runtime.threadpool_size, metrics=metrics)
    evidence = s3_storage(runtime, evidence_bucket, clock)
    core = build_core(
        runtime,
        database=database,
        clock=clock,
        metrics=metrics,
        provider_organization_id=provider,
        units=selected,
        evidence=evidence,
        cpu_pool=cpu_pool,
    )
    services = core.services
    audit, outbox, contexts = services.audit, services.outbox, services.contexts
    authorizer, writer, signing = services.authorizer, services.writer, services.signing
    deps = IdentityDependencies(
        database=database,
        writer=writer,
        audit=audit,
        outbox=outbox,
        authorizer=authorizer,
        free_text=core.free_text,
        clock=clock,
        provider_organization_id=provider,
    )
    passwords = PasswordService(HibpBreachChecker(breach_list, clock, metrics=metrics), cpu_pool)
    second_factor = SecondFactorService(
        PostgresSecondFactorStore(database, audit),
        EnvelopeCipher(core.kms, runtime.secrets_key_arn, clock, metrics=metrics),
        cpu_pool,
        clock,
    )
    session_store = PostgresSessionStore(database, audit, outbox, metrics=metrics)
    origin_key = os.urandom(ORIGIN_KEY_BYTES)
    identity = IdentityHttp(
        login=LoginService(
            store=session_store,
            sessions=session_store,
            passwords=passwords,
            second_factor=second_factor,
            contexts=contexts,
            clock=clock,
            provider_organization_id=provider,
            origin_key=origin_key,
            metrics=metrics,
        ),
        sessions=SessionService(session_store, contexts, clock),
        invitations=InvitationService(
            deps, contexts=contexts, passwords=passwords, second_factor=second_factor
        ),
        privacy_notice=PrivacyNoticeService(deps),
        passwords=PasswordChangeService(
            database=database,
            audit=audit,
            passwords=passwords,
            throttle=session_store,
            contexts=contexts,
            clock=clock,
        ),
        me=MeService(database, contexts=contexts, provider_organization_id=provider),
        provider_organization_id=provider,
        users=UserService(deps, senders=EmailSenderRegistry(), link_base=link_base),
        roles=RoleService(deps),
        second_factor_reset=SecondFactorResetService(deps, second_factor),
        hierarchy=HierarchyService(deps),
        organization=OrganizationSettingsService(deps),
        concessions=ConcessionService(
            store=PostgresConcessionStore(database=database, audit=audit),
            writer=writer,
            authorizer=authorizer,
            contexts=contexts,
            clock=clock,
        ),
    )
    ledger = LedgerHttp(
        reader=LectorExpediente(database=database, audit=audit, clock=clock, metrics=metrics),
        evidence=EvidenceService(database=database, audit=audit, storage=evidence),
        labels=LabelService(database=database, audit=audit),
        coverage=CoverageService(database=database, audit=audit, clock=clock, metrics=metrics),
        integrity_results=SqlIntegrityResults(database=database),
        integrity_requests=IntegrityRequests(database=database, outbox=outbox, clock=clock),
        checkpoints=services.checkpoints,
        live_view=LiveViewTokenService(
            database=database,
            authorizer=authorizer,
            audit=audit,
            outbox=outbox,
            signer=signing,
            clock=clock,
            metrics=metrics,
        ),
        authorizer=authorizer,
        provider_organization_id=provider,
    )
    platform = PlatformHttp(
        signing=signing,
        dead_letter=DeadLetterReplay(
            database=database, authorizer=authorizer, audit=audit, clock=clock
        ),
        operators=contexts,
        authorizer=authorizer,
        audit=audit,
        provider_organization_id=provider,
    )
    return AppRuntime(
        clock=clock,
        database=database,
        storage=evidence,
        signing=signing,
        kms=core.kms,
        registries=core.synchronizers,
        authorizer=ContextAuthorizer(
            audit=core.authorization_audit,
            provider_organization_id=provider,
            provider_queries=LedgerProviderQueryLedger(writer),
            clock=clock,
        ),
        metrics=metrics,
        sessions=contexts,
        csrf_audit=AuditCsrfRejections(
            audit=audit, provider_context=contexts.provider_audit_context
        ),
        origin_secret=origin_key,
        rate_limiter=RateLimiter(clock),
        # Los semáforos por clase de ruta con los tamaños de VIGIA_BULKHEAD_NODE y
        # VIGIA_BULKHEAD_PERSON (LC-GOB-20; revisión de VIG-140).
        bulkheads=Bulkheads(settings=runtime.bulkheads, clock=clock, metrics=metrics),
        identity=identity,
        ledger=ledger,
        platform=platform,
        state=api_state(selected, services),
    )
