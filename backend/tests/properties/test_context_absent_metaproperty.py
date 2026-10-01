"""PR-NUC-02: toda operación de repositorio sin contexto lanza ``ContextAbsent`` sin consultar.

``business-logic-model.md`` §11 (C-PLA-04) y BR-NUC-02: "Toda operación de todo repositorio
registrado, invocada sin contexto, lanza ``ContextAbsent`` sin ejecutar consulta alguna"
(recorrido del registro de repositorios); el intento escribe ``context_absent_attempt`` en la
cadena de la organización proveedora y emite ``security_alert``.

1. **El registro está completo**: se importan todos los módulos de ``vigia_platform`` y toda clase
   pública (no ``Protocol``) con un método público que recibe un ``ScopeContext`` (o una
   ``Transaction`` obligatoria) está registrada con ``@repository``.
2. **Metapropiedad**: para cada operación registrada y cualquier valor que no es un contexto
   (``None``, números, textos, diccionarios con la forma de un contexto, objetos), la operación
   invocada sobre una instancia **vacía** (``object.__new__``: sin base, sin pool, sin nada) lanza
   ``ContextAbsent`` con su nombre, y el receptor de avisos recibe exactamente ese intento. Los
   demás argumentos son un veneno que falla si se toca. Como la instancia no tiene ningún
   atributo, llegar a cualquier consulta sería un ``AttributeError``: la guarda corre antes que
   nada. El escritor del expediente (``@handles_absent_context``) devuelve en su lugar el rechazo
   ``context_absent`` y avisa igual.
3. **Contra PostgreSQL**: con ``ContextAbsentAuditor`` instalado, una lectura de ``shared.db`` sin
   contexto no pide ninguna conexión al pool y deja una entrada ``context_absent_attempt``
   (``outcome = denied``, con la operación) en la cadena de auditoría de la proveedora y un
   ``security_alert`` (``alert_kind = context_absent_attempt``) en su bandeja.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import pkgutil
import re
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import event, text

import vigia_platform
from tests.authz_support import AuthzEnvironment, authz_environment
from tests.factories import make_context
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.identity.adapters.authz_store import PostgresAuthorizationAudit
from vigia_platform.identity.authz.context import ContextAbsentAuditor
from vigia_platform.shared.context import (
    ContextAbsent,
    GuardedOperation,
    install_context_absent_reporter,
    registered_repositories,
    repository,
)


def _import_everything() -> None:
    for module in pkgutil.walk_packages(vigia_platform.__path__, "vigia_platform."):
        importlib.import_module(module.name)


_import_everything()

_TRANSACTION = re.compile(r"(\w+\.)?Transaction")


def _is_data_operation(function: Any) -> bool:
    for parameter in inspect.signature(function).parameters.values():
        annotation = str(parameter.annotation)
        if "ScopeContext" in annotation:
            return True
        if _TRANSACTION.fullmatch(annotation.strip("'\"")):
            return True
    return False


def unregistered_data_classes(modules: list[Any]) -> list[str]:
    """Clases públicas con operaciones de datos que no están en el registro."""
    registered = registered_repositories()
    missing: list[str] = []
    for module in modules:
        for name, cls in vars(module).items():
            if (
                not inspect.isclass(cls)
                or cls.__module__ != module.__name__
                or name.startswith("_")
                or getattr(cls, "_is_protocol", False)
                or cls in registered
            ):
                continue
            for method_name, attribute in vars(cls).items():
                function = getattr(attribute, "__func__", attribute)
                if method_name.startswith("_") or not inspect.isfunction(function):
                    continue
                if _is_data_operation(function):
                    missing.append(f"{module.__name__}.{cls.__qualname__}.{method_name}")
    return missing


DELEGATING_FUNCTIONS: frozenset[str] = frozenset(
    {
        # Reciben una ``Transaction`` ya abierta (solo la da ``Database.transaction``, guardada)
        # y consultan a través de ella o de repositorios registrados.
        "vigia_platform.identity.adapters.session_store.end_user_sessions",
        "vigia_platform.identity.adapters.session_store.expire_sessions",
        "vigia_platform.identity.adapters.session_store.cleanup_throttle_windows",
        "vigia_platform.ledger.labels_projection.insert_label",
        # Solo llaman a un puerto registrado (``ProviderQueryLedger``, ``IntegrityStore``), que
        # pone la guarda en cada consulta.
        "vigia_platform.identity.authz.context.record_provider_query",
        "vigia_platform.ledger.chain.verify.sql_pass",
        # identity.application (TASK-126): piezas de las operaciones de jerarquía, cuentas y
        # roles. Las que reciben una ``Transaction`` consultan solo a través de ella; las que
        # reciben un ``ScopeContext`` abren su transacción o leen con ``shared.db`` (guardado) o
        # escriben con ``AuditWriter`` y ``EscritorExpediente`` (registrados).
        "vigia_platform.identity.application.common.lock_organization",
        "vigia_platform.identity.application.common.resolve_scope",
        "vigia_platform.identity.application.common.write_record",
        "vigia_platform.identity.application.hierarchy.insert_plant",
        "vigia_platform.identity.application.invitations.deliver",
        "vigia_platform.identity.application.invitations.issue_invitation",
        "vigia_platform.identity.application.privacy_notice.record_acceptance",
        "vigia_platform.identity.application.roles.active_org_administrators",
        "vigia_platform.identity.application.roles.audit_rejection",
        "vigia_platform.identity.application.roles.insert_assignment",
        "vigia_platform.identity.application.roles.load_assignments",
        "vigia_platform.identity.application.roles.refresh_second_factor_required",
        "vigia_platform.identity.application.roles.remove_assignment",
        "vigia_platform.identity.application.users.create_invited_user",
    }
)
"""Funciones de módulo con operación de datos que delegan en una operación guardada.

Una función de módulo no puede registrarse con ``@repository``: si recibe un ``ScopeContext`` o
una ``Transaction`` y es asíncrona (puede consultar), o está aquí tras comprobar que solo delega
en operaciones guardadas, o la prueba falla. Las síncronas son puras (no hay E/S síncrona a la
base en la plataforma).
"""


def unlisted_data_functions(modules: list[Any]) -> list[str]:
    """Funciones públicas asíncronas de módulo con operación de datos fuera de la lista."""
    missing: list[str] = []
    for module in modules:
        for name, function in vars(module).items():
            if (
                not inspect.isfunction(function)
                or function.__module__ != module.__name__
                or name.startswith("_")
                or not inspect.iscoroutinefunction(function)
                or not _is_data_operation(function)
            ):
                continue
            qualified = f"{module.__name__}.{name}"
            if qualified not in DELEGATING_FUNCTIONS:
                missing.append(qualified)
    return missing


def _platform_modules() -> list[Any]:
    return [
        importlib.import_module(module.name)
        for module in pkgutil.walk_packages(vigia_platform.__path__, "vigia_platform.")
    ]


def test_every_class_with_data_operations_is_registered() -> None:
    assert unregistered_data_classes(_platform_modules()) == []


def test_every_module_function_with_data_operations_is_reviewed() -> None:
    """Las funciones de módulo también: ninguna consulta queda fuera de la guarda sin revisar."""
    assert unlisted_data_functions(_platform_modules()) == []
    # La lista no guarda nombres que ya no existen (se revisaría una función que no está).
    present = {
        f"{module.__name__}.{name}"
        for module in _platform_modules()
        for name, function in vars(module).items()
        if inspect.isfunction(function) and function.__module__ == module.__name__
    }
    assert present >= DELEGATING_FUNCTIONS


def test_the_scan_detects_an_unreviewed_module_function() -> None:
    """Sonda negativa: una función de módulo asíncrona con contexto, fuera de la lista."""
    import types

    module = types.ModuleType("vigia_platform_probe")

    async def stray_query(context: Any) -> None: ...

    stray_query.__module__ = module.__name__
    stray_query.__annotations__["context"] = "ScopeContext"
    module.stray_query = stray_query  # type: ignore[attr-defined]
    assert unlisted_data_functions([module]) == ["vigia_platform_probe.stray_query"]


def test_the_scan_detects_an_unregistered_repository() -> None:
    """Sonda negativa: una clase con operación de datos fuera del registro se detecta."""
    import types

    from vigia_platform.shared.context import ScopeContext

    module = types.ModuleType("vigia_platform_probe")

    class Stray:
        async def load(self, context: ScopeContext) -> None: ...

    Stray.__module__ = module.__name__
    Stray.load.__annotations__["context"] = "ScopeContext"
    module.Stray = Stray  # type: ignore[attr-defined]
    (found,) = unregistered_data_classes([module])
    assert found.startswith("vigia_platform_probe.") and found.endswith("Stray.load")


OPERATIONS: list[GuardedOperation] = [
    operation for operations in registered_repositories().values() for operation in operations
]


def test_the_registry_covers_the_known_repositories() -> None:
    names = {operation.operation for operation in OPERATIONS}
    for expected in (
        "Database.transaction",
        "Database.read",
        "AuditWriter.append",
        "EscritorExpediente.write",
        "LectorExpediente.list",
        "PostgresSessionStore.validate",
        "PostgresContextStore.session_row",
        "Authorizer.authorize",
        "Outbox.publish",
    ):
        assert expected in names
    assert len(OPERATIONS) >= 60


class Poison:
    """Argumento que falla si la operación lo toca antes de comprobar el contexto."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"la operación tocó un argumento ({name}) antes del contexto")

    def __iter__(self) -> Iterator[Any]:
        raise AssertionError("la operación recorrió un argumento antes del contexto")

    def __bool__(self) -> bool:
        raise AssertionError("la operación evaluó un argumento antes del contexto")


class Impostor:
    """Tiene los atributos de un contexto sin serlo."""

    def __init__(self) -> None:
        real = make_context()
        for name in ("organization_id", "actor", "origin", "allowed_scopes", "correlation_id"):
            setattr(self, name, getattr(real, name))
        self.context = self  # tampoco pasa por una Transaction


not_contexts = st.one_of(
    st.none(),
    st.integers(),
    st.text(max_size=8),
    st.uuids(),
    st.dictionaries(st.sampled_from(["organization_id", "actor"]), st.uuids(), max_size=2),
    st.builds(object),
    st.builds(Impostor),
)


def _call(operation: GuardedOperation, bad: Any) -> Any:
    cls = operation.repository
    instance = object.__new__(cls)
    raw = inspect.getattr_static(cls, operation.name)
    function = getattr(raw, "__func__", raw)
    signature = inspect.signature(function)
    arguments: list[Any] = []
    keywords: dict[str, Any] = {}
    parameters = list(signature.parameters.values())
    if not isinstance(raw, staticmethod):
        parameters = parameters[1:]
    for parameter in parameters:
        value = bad if parameter.name in operation.parameters else Poison()
        if parameter.kind is parameter.VAR_POSITIONAL or parameter.kind is parameter.VAR_KEYWORD:
            continue
        if parameter.default is not parameter.empty and parameter.name not in operation.parameters:
            continue
        if parameter.kind is parameter.KEYWORD_ONLY:
            keywords[parameter.name] = value
        else:
            arguments.append(value)
    result = getattr(instance, operation.name)(*arguments, **keywords)
    if inspect.isawaitable(result):
        return asyncio.run(_await(result))
    return result


async def _await(awaitable: Any) -> Any:
    return await awaitable


@pytest.mark.parametrize("operation", OPERATIONS, ids=[o.operation for o in OPERATIONS])
@given(bad=not_contexts)
def test_pr_nuc_02_every_operation_without_context_raises_and_reports(
    operation: GuardedOperation, bad: Any
) -> None:
    reported: list[str] = []
    previous = install_context_absent_reporter(reported.append)
    try:
        if operation.handles_absence:
            result = _call(operation, bad)
            assert getattr(result, "code", None) == "context_absent"
        else:
            with pytest.raises(ContextAbsent) as raised:
                _call(operation, bad)
            assert raised.value.operation == operation.operation
    finally:
        install_context_absent_reporter(previous)
    assert reported == [operation.operation]


def test_repository_without_operations_is_refused() -> None:
    class Empty:
        def helper(self) -> None: ...

    with pytest.raises(TypeError, match="operaciones"):
        repository(Empty)


def test_a_failing_reporter_does_not_hide_context_absent() -> None:
    def broken(operation: str) -> None:
        raise RuntimeError("receptor roto")

    from vigia_platform.ledger.application.audit_writer import AuditWriter

    previous = install_context_absent_reporter(broken)
    try:
        writer = object.__new__(AuditWriter)
        with pytest.raises(ContextAbsent):
            asyncio.run(writer.append(None, "ledger_read"))  # type: ignore[arg-type]
    finally:
        install_context_absent_reporter(previous)


# --- Contra PostgreSQL: el intento queda auditado en la proveedora ------------------------------


@pytest.fixture(scope="module")
def environment(postgres_endpoint: PostgresEndpoint) -> Iterator[AuthzEnvironment]:
    with authz_environment(postgres_endpoint, "context_absent") as env:
        yield env


@pytest.mark.integration
def test_context_absent_attempt_is_audited_with_a_security_alert(
    environment: AuthzEnvironment,
) -> None:
    env = environment
    database = env.sessions.database
    audit = PostgresAuthorizationAudit(
        database=database,
        audit=env.sessions.audit,
        outbox=env.sessions.outbox,
        clock=env.sessions.clock,
    )
    auditor = ContextAbsentAuditor(contexts=env.contexts, audit=audit)
    checkouts: list[int] = []

    def count(*_: object) -> None:
        checkouts.append(1)

    engines = [pool.engine for pool in database._pools.values()]  # type: ignore[attr-defined]
    provider = env.provider_organization_id

    def entries() -> list[Any]:
        return env.fetch(
            "SELECT outcome, actor_kind, convert_from(filters, 'UTF8') AS filters"
            " FROM shared.audit_entry WHERE organization_id = $1"
            " AND operation = 'context_absent_attempt' ORDER BY chain_sequence",
            provider,
        )

    def alerts() -> list[Any]:
        return env.fetch(
            "SELECT payload->>'alert_kind' AS kind FROM shared.outbox_event"
            " WHERE organization_id = $1 AND event_name = 'security_alert'"
            " AND payload->>'alert_kind' = 'context_absent_attempt'",
            provider,
        )

    before_entries, before_alerts = len(entries()), len(alerts())

    async def attempt() -> None:
        for engine in engines:
            event.listen(engine.sync_engine.pool, "checkout", count)
        try:
            with pytest.raises(ContextAbsent):
                await database.read(None, text("SELECT 1"))  # type: ignore[arg-type]
            assert checkouts == []  # ninguna conexión pedida para el intento
        finally:
            for engine in engines:
                event.remove(engine.sync_engine.pool, "checkout", count)
        await auditor.drain()

    auditor.install()
    try:
        env.run(attempt())
    finally:
        auditor.uninstall()
    after = entries()
    assert len(after) == before_entries + 1
    assert (after[-1]["outcome"], after[-1]["actor_kind"]) == ("denied", "system")
    assert after[-1]["filters"] == '{"operation":"Database.read"}'
    assert len(alerts()) == before_alerts + 1
    # Sin receptor instalado ya no se audita (el auditor se desinstaló).
    with pytest.raises(ContextAbsent):
        env.run(database.read(None, text("SELECT 1")))  # type: ignore[arg-type]
    assert len(entries()) == before_entries + 1


def test_auditor_outside_a_loop_only_logs() -> None:
    class Unused:
        async def context_absent_attempt(self, *args: Any) -> None:
            raise AssertionError("no hay bucle")

    from tests.authz_support import SYSTEM_ACTOR_ID
    from tests.session_support import START
    from vigia_platform.identity.authz.context import ScopeContexts
    from vigia_platform.shared.clock import SimulatedClock

    contexts = ScopeContexts(
        store=None,  # type: ignore[arg-type]
        clock=SimulatedClock(START),
        provider_organization_id=uuid.uuid4(),
        system_actor_id=SYSTEM_ACTOR_ID,
    )
    auditor = ContextAbsentAuditor(contexts=contexts, audit=Unused())
    auditor.report("Database.read")  # sin bucle: no lanza, deja el error en el registro
