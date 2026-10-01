"""Fábrica de la aplicación FastAPI y comprobaciones de arranque (LC-NUC-19; PAT-NUC-RES-02).

``create_app(config, runtime=…)`` construye ``vigia-api``. Nada arranca si algo está mal:

**Al construir** (síncrono; ``ApiStartupError`` con los problemas en español):

- ``labels.platform.es.json`` válido y con etiqueta para cada miembro de las enumeraciones del
  código (``LABEL_BINDINGS``) y un mensaje para cada ``api_error_code``;
- los ``detail_code`` de cada unidad, con prefijo registrado (pendiente nº 33);
- toda ruta declara su clave de permiso, que existe en la matriz, o está en la lista pública
  cerrada (BR-NUC-91, PR-NUC-37), y sus ``detail_code`` están registrados;
- ``/docs`` y ``/openapi.json`` solo existen en ``local`` y ``test``; en cualquier otro entorno
  (``pilot``, ``staging-<n>``) responden ``not_found`` (NFR-NUC-23);
- la aplicación de página única de ``static_dir`` es servible (``shared.api.static``): su ruta
  de pantalla va delante de las de la API (solo navegaciones) y sus archivos detrás.

**Al arrancar** (en segundo plano, ``StartupSupervisor``), la lista fija de PAT-NUC-RES-02, en
este orden: base con la seguridad a nivel de fila en vigor y versión mínima del esquema
(NFR-NUC-14); clave activa cargada para cada propósito de firma (``SigningService.start``); clave
de datos del cifrado de sobre generable y descifrable (KMS, con su contexto de propósito);
registros de tipos, eventos, consumidores y tareas coherentes (los sincronizadores que recibe);
objeto centinela del almacén. Mientras tanto ``/health/live`` responde 200 y ``/health/ready``
503. Lo que falla se reintenta cada ``startup_retry_seconds``; si a los
``startup_deadline_seconds`` (60 s ``[objetivo propio]``) algo sigue fallando, el proceso termina
con código ``STARTUP_FAILURE_EXIT_CODE`` sin haber quedado ``ready``. Tras arrancar, corre el
refresco de claves cada 5 minutos (``SigningService.run_refresh``).

``config`` es un modelo estricto que se lee una vez (``AppConfig.from_environ``). Las
dependencias llegan construidas en ``AppRuntime`` (base, almacén, firma, KMS, sincronizadores y
autorizador): la fábrica no abre conexiones al construir, así que ``build_openapi_app`` genera la
especificación sin red (NFR-NUC-52).

Las unidades registran sus enrutadores y sus ``detail_code`` en ``platform_units()``; la matriz
de permisos llega en ``permissions`` (``identity.authz``, TASK-125). La fábrica instala la cadena
fija de middleware (``shared.api.middleware``, TASK-134) y no arranca si su orden no es el de
PAT-NUC-SEG-06. La sesión, la auditoría de ``csrf_rejected``, la versión vigente del aviso de
tratamiento y el autorizador por ruta llegan en ``AppRuntime``; el origen de la aplicación
(``VIGIA_PUBLIC_ORIGIN``) y los del almacén para la política de contenido
(``VIGIA_CSP_STORE_ORIGINS``) en ``AppConfig``.

La versión vigente del aviso es, por defecto, la del código (``identity.domain.privacy_notice``):
la misma que acepta ``POST /privacy-notice/accept``. Solo ``None`` explícito la deja sin fijar, y
entonces ninguna sesión sirve (fallo cerrado). Los servicios de las rutas de ``identity``
(TASK-135) llegan en ``AppRuntime.identity``; la fábrica deja en ``app.state`` esos servicios, la
versión del aviso y la de la release servida (``api_version``: la ``app_version`` de
``/version.json``, pendiente nº 7).
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import hmac
import logging
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

from fastapi import APIRouter, FastAPI
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel, ConfigDict, Field, model_validator

from vigia_platform.identity.adapters.http import IDENTITY_STATE_KEY, IdentityHttp, identity_routers
from vigia_platform.identity.domain.privacy_notice import CURRENT_PRIVACY_NOTICE_VERSION
from vigia_platform.ledger.application.audit_writer import AuditOutcome
from vigia_platform.ledger.domain.coverage import (
    CommunicationState,
    CoverageLayer,
    CoverageState,
    PlatformCause,
)
from vigia_platform.ledger.registry import ChainLevel
from vigia_platform.shared.api.app_state import API_VERSION_STATE_KEY, PRIVACY_NOTICE_STATE_KEY
from vigia_platform.shared.api.declarations import (
    AUTHORIZER_STATE_KEY,
    Authorizer,
    DenyAll,
    check_routes,
    iter_declared_routes,
)
from vigia_platform.shared.api.errors import (
    ApiErrorBody,
    ApiErrorCode,
    ApiStartupError,
    DetailCodeRegistry,
    ErrorCatalog,
    install_error_handlers,
)
from vigia_platform.shared.api.health import (
    HEALTH_RECORDER_STATE_KEY,
    READINESS_STATE_KEY,
    DatabaseHealthPort,
    ReadinessCheck,
    ReadinessProbe,
    SentinelPort,
    health_router,
)
from vigia_platform.shared.api.labels import DEFAULT_LABELS_PATH, LabelsInvalid, PlatformLabels
from vigia_platform.shared.api.middleware import (
    ChainSettings,
    CsrfAuditPort,
    RouteTable,
    SecurityHeaders,
    SessionContextPort,
    install_chain,
    verify_chain,
)
from vigia_platform.shared.api.middleware.headers import MAX_STORE_ORIGINS, store_origin
from vigia_platform.shared.api.middleware.steps import UNMATCHED_ROUTE
from vigia_platform.shared.api.static import (
    DEFAULT_STATIC_DIR,
    StaticSite,
    StaticSiteInvalid,
    static_routers,
)
from vigia_platform.shared.clock import Clock, SystemClock
from vigia_platform.shared.context import ActorKind, ContextOrigin, Role, ScopeLevel
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics
from vigia_platform.shared.observability.redaction import (
    DEFAULT_POLICY,
    AttributePolicy,
    redact_text,
)
from vigia_platform.shared.ratelimit import RateLimiter
from vigia_platform.shared.schema_version import MINIMUM_SCHEMA_VERSION
from vigia_platform.shared.secrets import KmsPort
from vigia_platform.shared.signing.keys import KeyStatus, SigningPurpose

__all__ = [
    "API_TITLE",
    "API_VERSION",
    "DATA_KEY_CHECK_PURPOSE",
    "LABEL_BINDINGS",
    "STARTUP_FAILURE_EXIT_CODE",
    "AppConfig",
    "AppRuntime",
    "SigningRuntime",
    "StartupCheck",
    "StartupSupervisor",
    "UnitRegistration",
    "build_openapi_app",
    "create_app",
    "platform_permissions",
    "platform_units",
]

API_TITLE: Final = "Vigía — interfaz de la plataforma"
API_VERSION: Final = "0.1.0"
"""Versión de la especificación ``backend/openapi/app.yaml`` (la del paquete)."""
STARTUP_FAILURE_EXIT_CODE: Final = 3
"""Código de salida de un arranque que no llegó a ``ready`` (PAT-NUC-RES-02)."""
DATA_KEY_CHECK_PURPOSE: Final = "startup_check"
"""Propósito del contexto de cifrado de la clave de datos de prueba del arranque: una clave de
datos del arranque nunca descifra un secreto de otro propósito."""
STARTUP_CHECK_TIMEOUT_SECONDS: Final = 10.0
"""Tope de cada comprobación de arranque; no supera el tope del intento de la base (16 s)."""

_ENVIRONMENT: Final = r"^(local|test|pilot|staging-[1-9][0-9]{0,2})$"
_DOCS_ENVIRONMENTS: Final = frozenset({"local", "test"})
_SENTINEL_KEY: Final = r"^[A-Za-z0-9][A-Za-z0-9!_.*'()/=-]{0,511}$"
_KMS_KEY_ID: Final = r"^[A-Za-z0-9][A-Za-z0-9:/_-]{0,2047}$"
_ROUTE_TEMPLATE: Final = re.compile(r"^[A-Za-z0-9_./{}:-]{1,128}$")

_log = get_logger("shared.api.app")

LABEL_BINDINGS: Final[Mapping[str, type[enum.Enum]]] = {
    "api_error_code": ApiErrorCode,
    "role": Role,
    "scope_level": ScopeLevel,
    "actor_kind": ActorKind,
    "context_origin": ContextOrigin,
    "signing_purpose": SigningPurpose,
    "key_status": KeyStatus,
    "chain_level": ChainLevel,
    "audit_outcome": AuditOutcome,
    "coverage_state": CoverageState,
    "coverage_layer": CoverageLayer,
    "platform_cause": PlatformCause,
    "communication_state": CommunicationState,
}
"""Enumeraciones del código cuyos miembros deben tener etiqueta (fallo cerrado, NFR-NUC-51)."""


# --- Configuración -----------------------------------------------------------------------------


class AppConfig(BaseModel):
    """Configuración de ``vigia-api``: modelo estricto, inmutable, leído una vez."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    environment: str = Field(pattern=_ENVIRONMENT)
    """``local``, ``test``, ``staging-<n>`` o ``pilot`` (producción del piloto)."""
    data_key_id: str = Field(pattern=_KMS_KEY_ID)
    """Clave KMS ``vigia-secrets`` del cifrado de sobre (``VIGIA_SECRETS_KEY_ARN``)."""
    health_sentinel_key: str = Field(default="health/ready-sentinel", pattern=_SENTINEL_KEY)
    """Objeto centinela del depósito de evidencias que consulta ``/health/ready``."""
    labels_path: Path = DEFAULT_LABELS_PATH
    static_dir: Path = DEFAULT_STATIC_DIR
    """Construcción de la aplicación de página única (``shared.api.static``)."""
    startup_deadline_seconds: float = Field(default=60.0, gt=0, le=600)
    startup_retry_seconds: float = Field(default=5.0, gt=0, le=60)
    public_origin: str | None = None
    """Origen de la aplicación (``https://app.<dominio>``): la barrera anti-falsificación exige
    que un ``Origin`` presente sea este. Sin él, toda petición que cambia estado con ``Origin``
    se rechaza (fallo cerrado)."""
    csp_store_origins: tuple[str, ...] = ()
    """Orígenes del almacén (``VIGIA_CSP_STORE_ORIGINS``) para ``media-src`` y ``connect-src``."""

    @property
    def docs_enabled(self) -> bool:
        """``/docs`` y ``/openapi.json`` solo fuera de producción y de ``staging`` (NFR-NUC-23)."""
        return self.environment in _DOCS_ENVIRONMENTS

    @property
    def allows_local_origins(self) -> bool:
        """``http://localhost`` y ``http://127.0.0.1`` solo en ``local`` y ``test``."""
        return self.environment in _DOCS_ENVIRONMENTS

    @model_validator(mode="after")
    def _origins(self) -> AppConfig:
        local = self.allows_local_origins
        if self.public_origin is not None:
            store_origin(self.public_origin, allow_local=local)
        for origin in self.csp_store_origins:
            store_origin(origin, allow_local=local)
        if len(set(self.csp_store_origins)) != len(self.csp_store_origins):
            raise ValueError("VIGIA_CSP_STORE_ORIGINS tiene orígenes repetidos")
        if len(self.csp_store_origins) > MAX_STORE_ORIGINS:
            raise ValueError(f"VIGIA_CSP_STORE_ORIGINS admite como mucho {MAX_STORE_ORIGINS}")
        return self

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> AppConfig:
        """Lee ``VIGIA_ENVIRONMENT``, ``VIGIA_SECRETS_KEY_ARN`` y, si están,
        ``VIGIA_HEALTH_SENTINEL_KEY``, ``VIGIA_STATIC_DIR``, ``VIGIA_PUBLIC_ORIGIN`` y
        ``VIGIA_CSP_STORE_ORIGINS`` (separados por espacios); ``ValueError`` (de Pydantic) si
        falta o no es válida."""
        values: dict[str, Any] = {
            "environment": environ.get("VIGIA_ENVIRONMENT", ""),
            "data_key_id": environ.get("VIGIA_SECRETS_KEY_ARN", ""),
        }
        sentinel = environ.get("VIGIA_HEALTH_SENTINEL_KEY")
        if sentinel is not None:
            values["health_sentinel_key"] = sentinel
        static_dir = environ.get("VIGIA_STATIC_DIR")
        if static_dir is not None:
            values["static_dir"] = Path(static_dir)
        public_origin = environ.get("VIGIA_PUBLIC_ORIGIN")
        if public_origin is not None:
            values["public_origin"] = public_origin
        store_origins = environ.get("VIGIA_CSP_STORE_ORIGINS")
        if store_origins is not None:
            values["csp_store_origins"] = tuple(store_origins.split())
        return cls(**values)


# --- Dependencias en tiempo de ejecución --------------------------------------------------------


class SigningRuntime(Protocol):
    """La parte de ``SigningService`` que usa la aplicación."""

    @property
    def ready(self) -> bool: ...

    async def start(self) -> None: ...

    def has_active_key(self, purpose: SigningPurpose) -> bool: ...

    async def run_refresh(self, stop: asyncio.Event) -> None: ...


def _terminate(code: int) -> None:  # pragma: no cover - termina el proceso de verdad
    """Termina el proceso con ``code`` tras vaciar los registros: uvicorn no tiene otra vía para
    salir con un código distinto de cero desde una tarea en segundo plano."""
    for handler in logging.getLogger().handlers:  # noqa: TID251 - solo se vacían
        with contextlib.suppress(Exception):
            handler.flush()
    os._exit(code)


@dataclass(frozen=True, slots=True, kw_only=True)
class AppRuntime:
    """Dependencias ya construidas de ``vigia-api`` (la raíz de composición las crea)."""

    clock: Clock
    database: DatabaseHealthPort
    storage: SentinelPort
    signing: SigningRuntime
    kms: KmsPort
    registries: tuple[Callable[[], Awaitable[None]], ...] = ()
    """Sincronizadores de los registros (``RecordTypeRegistry.synchronize``,
    ``OutboxCatalog.synchronize``…) ya ligados a su almacén; cada uno sella su registro."""
    authorizer: Authorizer = field(default_factory=DenyAll)
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    on_startup_failure: Callable[[int], None] = _terminate
    attribute_policy: AttributePolicy = DEFAULT_POLICY
    """Política de redacción que amplía la fábrica con las rutas y los motivos de salud."""
    metrics: PlatformMetrics | None = None
    """Métricas de la salud (``health_ready``) y de la cadena; por defecto, las del proveedor
    global."""
    sessions: SessionContextPort | None = None
    """``ScopeContexts`` (``context_from_session``) del paso 6; sin él no hay contexto y la
    autorización por ruta responde ``unauthenticated``."""
    csrf_audit: CsrfAuditPort | None = None
    """Auditoría de ``csrf_rejected`` (``AuditCsrfRejections``)."""
    origin_secret: bytes | None = None
    """Clave del HMAC del origen de red en ``csrf_rejected`` (la del retardo de fallos)."""
    privacy_notice_version: str | None = CURRENT_PRIVACY_NOTICE_VERSION
    """Versión vigente del aviso de tratamiento (TASK-126): por defecto la del código; ``None``
    explícito, fallo cerrado (ninguna sesión la tiene aceptada)."""
    rate_limiter: RateLimiter | None = None
    """Cubos de fichas del proceso; por defecto, uno nuevo con ``clock``."""
    identity: IdentityHttp | None = None
    """Servicios de las rutas de sesión, invitación y ``GET /me``; sin ellos, ``internal_error``."""


# --- Arranque ----------------------------------------------------------------------------------


class StartupCheck(enum.StrEnum):
    """Lista fija de comprobaciones de arranque, en su orden (PAT-NUC-RES-02)."""

    DATABASE = "database"
    SIGNING_KEYS = "signing_keys"
    DATA_KEY = "data_key"
    REGISTRIES = "registries"
    STORAGE = "storage"


class StartupSupervisor:
    """Ejecuta el arranque en segundo plano y publica si terminó (``started``)."""

    def __init__(self, config: AppConfig, runtime: AppRuntime) -> None:
        self._config = config
        self._runtime = runtime
        self._probe = ReadinessProbe(
            database=runtime.database,
            storage=runtime.storage,
            sentinel_key=config.health_sentinel_key,
            signing=runtime.signing,
        )
        self._started = False
        self._failed = False
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._registries_done = False

    @property
    def started(self) -> bool:
        return self._started

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def probe(self) -> ReadinessProbe | None:
        return self._probe if self._started else None

    async def _database(self) -> bool:
        health = await self._runtime.database.health(timeout_seconds=STARTUP_CHECK_TIMEOUT_SECONDS)
        if health.schema_version is None or health.schema_version < MINIMUM_SCHEMA_VERSION:
            _log.error("el esquema de la base es anterior al mínimo que exige esta imagen")
            return False
        if health.visible_organizations != 0:
            _log.error("la seguridad a nivel de fila no está en vigor para el rol de la aplicación")
            return False
        return True

    async def _signing_keys(self) -> bool:
        signing = self._runtime.signing
        if not signing.ready:
            await signing.start()
        return all(signing.has_active_key(purpose) for purpose in SigningPurpose)

    async def _data_key(self) -> bool:
        kms = self._runtime.kms
        context = {"vigia_purpose": DATA_KEY_CHECK_PURPOSE}
        data_key = await kms.generate_data_key(self._config.data_key_id, context=context)
        plaintext = await kms.decrypt(
            data_key.wrapped, key_id=self._config.data_key_id, context=context
        )
        return hmac.compare_digest(plaintext, data_key.plaintext)

    async def _registries(self) -> bool:
        if not self._registries_done:
            for synchronize in self._runtime.registries:
                await synchronize()
            self._registries_done = True
        return True

    async def _storage(self) -> bool:
        return await self._runtime.storage.head_object(self._config.health_sentinel_key) is not None

    async def _attempt(self, check: StartupCheck) -> bool:
        action = {
            StartupCheck.DATABASE: self._database,
            StartupCheck.SIGNING_KEYS: self._signing_keys,
            StartupCheck.DATA_KEY: self._data_key,
            StartupCheck.REGISTRIES: self._registries,
            StartupCheck.STORAGE: self._storage,
        }[check]
        try:
            async with asyncio.timeout(STARTUP_CHECK_TIMEOUT_SECONDS):
                passed = await action()
        except Exception:  # toda causa impide arrancar
            _log.exception("comprobación de arranque fallida", reason=check.value)
            return False
        if not passed:
            _log.error("comprobación de arranque fallida", reason=check.value)
        return passed

    async def run(self) -> None:
        """Comprueba en orden; reintenta lo fallido hasta el plazo; si no, termina el proceso."""
        clock = self._runtime.clock
        deadline = clock.monotonic() + self._config.startup_deadline_seconds
        pending = list(StartupCheck)
        while True:
            still: list[StartupCheck] = []
            for check in pending:
                if not await self._attempt(check):
                    still.append(check)
            pending = still
            if not pending:
                self._started = True
                _log.info("arranque completo: el proceso queda listo")
                self._tasks.append(
                    asyncio.create_task(self._runtime.signing.run_refresh(self._stop))
                )
                return
            remaining = deadline - clock.monotonic()
            if remaining <= 0 or self._stop.is_set():
                break
            await self._runtime.sleep(min(self._config.startup_retry_seconds, remaining))
        if self._stop.is_set():
            return
        self._failed = True
        for check in pending:
            _log.critical("el proceso no arranca: comprobación sin superar", reason=check.value)
        self._runtime.on_startup_failure(STARTUP_FAILURE_EXIT_CODE)

    def start(self) -> None:
        self._tasks.append(asyncio.create_task(self.run()))

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()


# --- Unidades y permisos -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnitRegistration:
    """Lo que una unidad aporta a la aplicación: enrutadores y ``detail_code``."""

    name: str
    routers: tuple[APIRouter, ...] = ()
    detail_codes: tuple[str, ...] = ()


def platform_units() -> tuple[UnitRegistration, ...]:
    """Unidades registradas en ``vigia-api``.

    ``identity`` y ``ledger`` añaden aquí sus enrutadores (TASK-135 a 137) y U-03, U-04 y U-05
    los suyos con sus ``detail_code``.
    """
    return (
        UnitRegistration("shared", routers=(health_router(),)),
        UnitRegistration("identity", routers=identity_routers()),
        UnitRegistration("ledger"),
    )


def platform_permissions() -> frozenset[str]:
    """Claves de la matriz de permisos; TASK-125 las aporta desde ``identity.authz.matrix``.

    Mientras no existe la matriz, ninguna clave existe: una ruta que exija una no arranca.
    """
    return frozenset()


# --- Fábrica -----------------------------------------------------------------------------------


def _labels(config: AppConfig) -> PlatformLabels:
    try:
        labels = PlatformLabels.load(config.labels_path)
    except LabelsInvalid as error:
        raise ApiStartupError([str(error)]) from None
    problems = labels.require_complete(LABEL_BINDINGS)
    if problems:
        raise ApiStartupError(problems)
    return labels


def _detail_codes(units: Iterable[UnitRegistration]) -> DetailCodeRegistry:
    registry = DetailCodeRegistry()
    for unit in units:
        registry.register(unit.detail_codes)
    registry.seal()
    return registry


def _openapi(app: FastAPI) -> dict[str, Any]:
    """Especificación con ``ApiErrorBody`` como respuesta de error de toda operación.

    FastAPI documenta por defecto un 422 con su propio modelo; la plataforma responde siempre un
    ``ApiError``, así que ese modelo se sustituye por ``default``.
    """
    if app.openapi_schema is not None:
        return app.openapi_schema
    spec = get_openapi(title=app.title, version=app.version, routes=app.routes)
    error_schema = ApiErrorBody.model_json_schema(ref_template="#/components/schemas/{model}")
    definitions = error_schema.pop("$defs", {})
    components = spec.setdefault("components", {}).setdefault("schemas", {})
    components.pop("HTTPValidationError", None)
    components.pop("ValidationError", None)
    components.update(definitions)
    components["ApiErrorBody"] = error_schema
    default = {
        "description": "Error genérico (ApiError)",
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ApiErrorBody"}}},
    }
    for operations in spec.get("paths", {}).values():
        for operation in operations.values():
            responses = operation.setdefault("responses", {})
            responses.pop("422", None)
            responses["default"] = default
    spec["components"]["schemas"] = dict(sorted(components.items()))
    app.openapi_schema = spec
    return spec


def _assemble(
    config: AppConfig,
    *,
    clock: Clock,
    units: tuple[UnitRegistration, ...],
    permissions: Collection[str],
    lifespan: Callable[[FastAPI], contextlib.AbstractAsyncContextManager[None]] | None,
    site: StaticSite,
    runtime: AppRuntime | None = None,
) -> FastAPI:
    labels = _labels(config)
    detail_codes = _detail_codes(units)
    catalog = ErrorCatalog(labels, detail_codes)
    docs = config.docs_enabled
    app = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
        swagger_ui_oauth2_redirect_url="/docs/oauth2-redirect" if docs else None,
        lifespan=lifespan,
    )
    # Pantallas delante (solo navegaciones, D-3), API en medio, archivos estáticos detrás.
    screens, files = static_routers(site)
    app.include_router(screens)
    for unit in units:
        for router in unit.routers:
            app.include_router(router)
    app.include_router(files)
    problems = check_routes(app.routes, permissions, detail_codes, docs_enabled=docs)
    if problems:
        raise ApiStartupError(problems)
    install_error_handlers(app, catalog, clock)
    install_chain(app, _chain_settings(config, clock, catalog, app, runtime))
    problems = verify_chain(app)
    if problems:
        raise ApiStartupError(problems)
    app.openapi = lambda: _openapi(app)  # type: ignore[method-assign]
    return app


def _chain_settings(
    config: AppConfig,
    clock: Clock,
    catalog: ErrorCatalog,
    app: FastAPI,
    runtime: AppRuntime | None,
) -> ChainSettings:
    """Los ajustes de la cadena de middleware para ``app`` (PAT-NUC-SEG-06)."""
    if config.public_origin is None and not config.allows_local_origins:
        _log.error(
            "sin VIGIA_PUBLIC_ORIGIN: toda petición que cambia estado con Origin se rechazará"
        )
    limiter = runtime.rate_limiter if runtime is not None else None
    return ChainSettings(
        clock=clock,
        catalog=catalog,
        headers=SecurityHeaders(config.csp_store_origins, allow_local=config.allows_local_origins),
        routes=RouteTable(app.routes),
        limiter=limiter if limiter is not None else RateLimiter(clock),
        public_origin=config.public_origin,
        sessions=runtime.sessions if runtime is not None else None,
        csrf_audit=runtime.csrf_audit if runtime is not None else None,
        origin_secret=runtime.origin_secret if runtime is not None else None,
        privacy_notice_version=runtime.privacy_notice_version if runtime is not None else None,
        metrics=runtime.metrics if runtime is not None else None,
    )


def _registrable(values: Iterable[str]) -> list[str]:
    """Los valores que la redacción admite en una lista cerrada (sin forma de token)."""
    return [v for v in values if _ROUTE_TEMPLATE.fullmatch(v) and redact_text(v) == v]


def _register_observability(app: FastAPI, policy: AttributePolicy) -> None:
    """Amplía las listas cerradas de la redacción con las rutas y los motivos de esta app.

    Una plantilla con forma de token (p. ej. 20 o más caracteres seguidos sin punto) sale en
    los registros como ``[redactado]``: la política no la admite y aquí no se registra.
    """
    templates = [route.path for route in iter_declared_routes(app.routes) if route.is_api_route]
    templates.append(UNMATCHED_ROUTE)
    policy.register("route", _registrable(templates))
    policy.register(
        "reason", _registrable([c.value for c in ReadinessCheck] + [c.value for c in StartupCheck])
    )


def _static_site(config: AppConfig) -> StaticSite:
    try:
        return StaticSite.load(config.static_dir)
    except StaticSiteInvalid as error:
        raise ApiStartupError(error.problems) from None


def _health_recorder(
    site: StaticSite, policy: AttributePolicy, metrics: PlatformMetrics | None
) -> Callable[[bool], None]:
    """Publica ``health_ready`` con la dimensión ``app_version`` (LC-NUC-31, pendiente nº 12)."""
    version = site.metric_version
    policy.register("app_version", [version])

    def record(ready: bool) -> None:
        instruments = metrics if metrics is not None else get_metrics()
        instruments.health_ready.set(1 if ready else 0, {"app_version": version})

    return record


def create_app(
    config: AppConfig,
    *,
    runtime: AppRuntime,
    units: tuple[UnitRegistration, ...] | None = None,
    permissions: Collection[str] | None = None,
) -> FastAPI:
    """``vigia-api``: rutas comprobadas al construir y arranque supervisado en segundo plano.

    Raises:
        ApiStartupError: etiquetas incompletas, ``detail_code`` sin prefijo registrado o una
            ruta sin declaración válida. El mensaje, en español, nombra cada problema.
    """
    if not isinstance(config, AppConfig):
        raise TypeError("config debe ser AppConfig")
    supervisor = StartupSupervisor(config, runtime)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        supervisor.start()
        try:
            yield
        finally:
            await supervisor.stop()

    site = _static_site(config)
    app = _assemble(
        config,
        clock=runtime.clock,
        units=platform_units() if units is None else units,
        permissions=platform_permissions() if permissions is None else permissions,
        lifespan=lifespan,
        site=site,
        runtime=runtime,
    )
    _register_observability(app, runtime.attribute_policy)
    recorder = _health_recorder(site, runtime.attribute_policy, runtime.metrics)
    setattr(app.state, AUTHORIZER_STATE_KEY, runtime.authorizer)
    setattr(app.state, IDENTITY_STATE_KEY, runtime.identity)
    setattr(app.state, PRIVACY_NOTICE_STATE_KEY, runtime.privacy_notice_version)
    setattr(app.state, API_VERSION_STATE_KEY, site.app_version)
    setattr(app.state, READINESS_STATE_KEY, supervisor)
    setattr(app.state, HEALTH_RECORDER_STATE_KEY, recorder)
    return app


OPENAPI_CONFIG: Final = AppConfig(environment="local", data_key_id="alias/vigia-secrets")
"""Configuración de la especificación: la de ``local`` (con ``/openapi.json``) sin red."""


def build_openapi_app(
    units: tuple[UnitRegistration, ...] | None = None,
    permissions: Collection[str] | None = None,
) -> FastAPI:
    """La misma aplicación que ``create_app``, sin arranque ni dependencias (para exportar)."""
    return _assemble(
        OPENAPI_CONFIG,
        clock=SystemClock(),
        units=platform_units() if units is None else units,
        permissions=platform_permissions() if permissions is None else permissions,
        lifespan=None,
        site=StaticSite(),
    )
