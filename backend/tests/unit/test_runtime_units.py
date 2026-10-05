"""Registro por unidad y constructores de la raíz de composición, sin red (VIG-137, A-52).

- ``platform_units()`` y ``LABEL_BINDINGS`` salen del registro por unidad: las tres entradas de
  U-02 (``shared``, ``identity``, ``ledger``) con las mismas trece enumeraciones con etiqueta.
- **Una unidad de prueba añadida al registro** (``tests.runtime_support.PROBE_UNIT``: una ruta, un
  tipo de registro, un evento, un consumidor, una tarea y un valor de enumeración con etiqueta)
  aparece en la aplicación de ``create_app``, en el catálogo y en los registros que componen los
  constructores, sin editarlos. Su enumeración se exige en el archivo de etiquetas (fallo
  cerrado). El arranque contra la base lo prueba ``tests/integration/test_production_runtime_boot``.
- El ``SigningService`` de los tres constructores lleva ``LedgerRotationRecorder``
  (``records_rotation``).
- ``vigia-admin`` se compone sin leer el secreto ni abrir la base (``LazyDatabase``); ``node_ca`` y
  ``archive`` solo con sus variables.
- Errores de composición: cada uno nombra su variable y nunca el valor del secreto.

Los constructores se llaman con un lector de secretos falso (``FakeReader``): crear los motores de
SQLAlchemy y los clientes de boto3 no abre conexiones. Solo datos generados.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any, cast

import pytest

from tests.runtime_support import (
    PROBE_CONSUMER,
    PROBE_ENUMERATION,
    PROBE_EVENT,
    PROBE_PATH,
    PROBE_RECORD_TYPE,
    PROBE_STATE_KEY,
    PROBE_TASK,
    PROBE_UNIT,
    PROVIDER_ID,
    SECRET_PASSWORD,
    FakeReader,
    breach_list,
    database_secret,
    probe_labels,
    runtime_environ,
    with_probe_unit,
)
from tests.worker_support import StubKms, StubSigning, StubStorage
from vigia_platform.identity.application.admin_cli import AdminConfig
from vigia_platform.shared.api.app import (
    LABEL_BINDINGS,
    AppConfig,
    AppRuntime,
    create_app,
    platform_units,
)
from vigia_platform.shared.api.declarations import iter_declared_routes
from vigia_platform.shared.api.errors import ApiStartupError
from vigia_platform.shared.context import ActorUnit, ContextAbsent
from vigia_platform.shared.db import DatabaseHealth, ProcessKind
from vigia_platform.shared.runtime import units
from vigia_platform.shared.runtime.admin import compose_admin_runtime
from vigia_platform.shared.runtime.api import compose_api_runtime
from vigia_platform.shared.runtime.config import RuntimeConfig, RuntimeConfigInvalid
from vigia_platform.shared.runtime.db_credentials import LazyDatabase
from vigia_platform.shared.runtime.units import (
    PlatformUnit,
    label_bindings,
    record_type_registry,
    registered_units,
)
from vigia_platform.shared.runtime.worker import compose_worker_runtime
from vigia_platform.shared.schema_version import MINIMUM_SCHEMA_VERSION
from vigia_platform.shared.secrets import SecretNotFound
from vigia_platform.shared.worker.main import WorkerConfig

STATIC = Path(__file__).resolve().parents[1] / "fixtures" / "static"
ORIGIN = "https://app.vigia.test"

U02_LABELS = {
    "api_error_code",
    "role",
    "scope_level",
    "actor_kind",
    "context_origin",
    "signing_purpose",
    "key_status",
    "chain_level",
    "audit_outcome",
    "coverage_state",
    "coverage_layer",
    "platform_cause",
    "communication_state",
}


def _runtime(**changes: str | None) -> RuntimeConfig:
    return RuntimeConfig.from_environ(runtime_environ(**changes))


def _app_config(labels: Path | None = None) -> AppConfig:
    values: dict[str, Any] = {
        "environment": "test",
        "data_key_id": "alias/vigia-secrets",
        "static_dir": STATIC,
        "public_origin": ORIGIN,
    }
    if labels is not None:
        values["labels_path"] = labels
    return AppConfig(**values)


def _worker_config() -> WorkerConfig:
    return WorkerConfig(environment="test", data_key_id="alias/vigia-secrets")


def _admin_config(**environ: str) -> AdminConfig:
    return AdminConfig.from_environ({"VIGIA_ENVIRONMENT": "test", **environ})


def _api(tmp_path: Path, runtime: RuntimeConfig | None = None, **kwargs: Any) -> AppRuntime:
    runtime = runtime or _runtime(VIGIA_BREACH_LIST_PATH=str(breach_list(tmp_path)))
    reader = kwargs.pop("reader", FakeReader())
    return asyncio.run(
        compose_api_runtime(kwargs.pop("config", _app_config()), runtime, reader=reader, **kwargs)
    )


# --- El registro --------------------------------------------------------------------------------


def test_platform_units_and_label_bindings_come_from_the_registry() -> None:
    names = [unit.name for unit in registered_units()]
    assert names[:3] == ["shared", "identity", "ledger"]
    assert [unit.name for unit in platform_units()] == names
    assert set(LABEL_BINDINGS) == set(label_bindings(registered_units()))
    assert set(LABEL_BINDINGS) >= U02_LABELS
    for registration in platform_units():
        if registration.name in {"shared", "identity", "ledger"}:
            assert registration.routers  # cada unidad de U-02 publica rutas


def test_u02_record_types_come_from_the_registry() -> None:
    registry = record_type_registry(registered_units())
    latest = registry.latest()
    names = {compiled.record_type for compiled in latest}
    assert {"key_rotated", "key_set_published"} <= names
    u02 = {c.record_type for c in latest if c.definition.writer_unit is ActorUnit.U02}
    assert len(u02) == 14
    # U-03: solo los tipos que ya escribe una ruta registrada (VIG-142 la admisión; VIG-146 el
    # acta de alcance, la transición de compuerta y la política de planta; VIG-148 el catálogo,
    # el retiro, la marca unipersonal y la marca de regresión; VIG-149 el acuerdo de uso;
    # VIG-147 la identidad del nodo; VIG-150 el cierre de cada paso del walk-test; VIG-151 el
    # alta y la rotación de la credencial).
    assert names - u02 == {
        "node_enrolled",
        "node_credential_rotated",
        "standard_admission_test",
        "mounting_gate_record",
        "gate_state_changed",
        "plant_policy_signed",
        "node_communication_state_changed",
        "node_revoked",
        "node_decommissioned",
        "enrollment_code_issued",
        "enrollment_attempt_rejected",
        "catalog_version_published",
        "catalog_standard_retired",
        "single_occupancy_declared",
        "walk_test_regression_marked",
        "use_agreement_signed",
        "commissioning_step",
    }


def test_the_registry_rejects_duplicates_and_bad_names(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="nombre"):
        PlatformUnit(name="Con Espacios")
    monkeypatch.setattr(
        units, "REGISTERED_UNITS", (*units.REGISTERED_UNITS, PlatformUnit(name="shared"))
    )
    with pytest.raises(ValueError, match="mismo nombre"):
        registered_units()
    clash = PlatformUnit(name="clash", labels={"role": PROBE_UNIT.labels[PROBE_ENUMERATION]})
    with pytest.raises(ValueError, match="role"):
        label_bindings((*units.REGISTERED_UNITS[:1], clash))


# --- Una unidad nueva, sin tocar los constructores ------------------------------------------------


def test_a_test_unit_appears_in_the_app_catalog_and_registries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with_probe_unit(monkeypatch)

    worker = asyncio.run(compose_worker_runtime(_worker_config(), _runtime(), reader=FakeReader()))
    assert PROBE_EVENT in worker.catalog.event_types.event_names()
    assert PROBE_CONSUMER in {c.consumer_name for c in worker.catalog.consumers.consumers()}
    assert PROBE_TASK in {t.task_name for t in worker.catalog.periodic_tasks.tasks()}
    assert PROBE_RECORD_TYPE in {
        c.record_type for c in record_type_registry(registered_units()).latest()
    }

    runtime = _api(tmp_path)
    service = runtime.state[PROBE_STATE_KEY]
    assert getattr(service, "provider_organization_id", None) == PROVIDER_ID
    app = create_app(_app_config(probe_labels(tmp_path)), runtime=runtime)
    assert PROBE_PATH in {route.path for route in iter_declared_routes(app.routes)}
    assert getattr(app.state, PROBE_STATE_KEY) is service
    assert PROBE_ENUMERATION in {name for unit in platform_units() for name in unit.labels}


def test_a_test_unit_enumeration_without_label_does_not_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with_probe_unit(monkeypatch)
    runtime = _api(tmp_path)
    with pytest.raises(ApiStartupError, match=PROBE_ENUMERATION):
        create_app(_app_config(), runtime=runtime)  # el archivo de etiquetas no la tiene


def test_unit_state_cannot_overwrite_a_factory_key(tmp_path: Path) -> None:
    class _Database:
        async def health(self, *, timeout_seconds: float) -> DatabaseHealth:
            return DatabaseHealth(visible_organizations=0, schema_version=MINIMUM_SCHEMA_VERSION)

    runtime = AppRuntime(
        clock=_api(tmp_path).clock,
        database=_Database(),
        storage=StubStorage(),
        signing=StubSigning(),
        kms=cast(Any, StubKms()),
        state={"vigia_readiness": object()},
    )
    with pytest.raises(ApiStartupError, match="vigia_readiness"):
        create_app(_app_config(), runtime=runtime)


# --- Firma con su auditoría (nota de VIG-88) ------------------------------------------------------


def test_every_root_signing_service_records_its_rotations(tmp_path: Path) -> None:
    api = _api(tmp_path)
    worker = asyncio.run(compose_worker_runtime(_worker_config(), _runtime(), reader=FakeReader()))
    admin = asyncio.run(
        compose_admin_runtime(_admin_config(), PROVIDER_ID, _runtime(), reader=FakeReader())
    )
    for signing in (api.signing, worker.signing, admin.signing):
        assert getattr(signing, "records_rotation", False) is True


# --- vigia-admin perezosa ------------------------------------------------------------------------


def test_admin_runtime_does_not_read_the_secret_nor_open_the_database() -> None:
    reader = FakeReader(error=SecretNotFound())
    admin = asyncio.run(
        compose_admin_runtime(_admin_config(), PROVIDER_ID, _runtime(), reader=reader)
    )
    database = admin.database
    assert isinstance(database, LazyDatabase)
    assert not database.opened
    assert reader.reads == 0
    assert admin.node_ca is None and admin.archive is None
    with pytest.raises(ContextAbsent):
        database.transaction(cast(Any, None))
    assert not database.opened and reader.reads == 0


def test_admin_database_opens_on_first_use_and_names_a_missing_secret() -> None:
    reader = FakeReader(error=SecretNotFound())
    admin = asyncio.run(
        compose_admin_runtime(_admin_config(), PROVIDER_ID, _runtime(), reader=reader)
    )

    async def use() -> None:
        async with admin.database.transaction(admin.contexts.provider_audit_context()):
            raise AssertionError("no debía abrir la transacción")

    with pytest.raises(RuntimeConfigInvalid) as raised:
        asyncio.run(use())
    assert raised.value.variable == "VIGIA_DB_APP_SECRET"
    assert reader.reads == 1


def test_admin_node_ca_and_archive_only_with_their_variables() -> None:
    config = _admin_config(
        VIGIA_NODE_CA_KEY_ARN="alias/vigia-node-ca",
        VIGIA_EDGE_BUCKET="vigia-edge-test",
        VIGIA_ARCHIVE_BUCKET="vigia-archive-test",
    )
    admin = asyncio.run(compose_admin_runtime(config, PROVIDER_ID, _runtime(), reader=FakeReader()))
    assert admin.node_ca is not None and admin.archive is not None
    only_key = _admin_config(VIGIA_NODE_CA_KEY_ARN="alias/vigia-node-ca")
    admin = asyncio.run(
        compose_admin_runtime(only_key, PROVIDER_ID, _runtime(), reader=FakeReader())
    )
    assert admin.node_ca is None


# --- Errores de composición ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("variable", "process"),
    [
        ("VIGIA_PROVIDER_ORGANIZATION_ID", "api"),
        ("VIGIA_EVIDENCE_BUCKET", "api"),
        ("VIGIA_PROVIDER_ORGANIZATION_ID", "worker"),
        ("VIGIA_EVIDENCE_BUCKET", "worker"),
        ("VIGIA_ARCHIVE_BUCKET", "worker"),
    ],
)
def test_a_process_requires_its_variables(variable: str, process: str, tmp_path: Path) -> None:
    runtime = _runtime(**{variable: None, "VIGIA_BREACH_LIST_PATH": str(breach_list(tmp_path))})
    with pytest.raises(RuntimeConfigInvalid) as raised:
        if process == "api":
            _api(tmp_path, runtime)
        else:
            asyncio.run(compose_worker_runtime(_worker_config(), runtime, reader=FakeReader()))
    assert raised.value.variable == variable


def test_api_requires_the_public_origin_and_a_readable_breach_list(tmp_path: Path) -> None:
    config = AppConfig(environment="test", data_key_id="alias/vigia-secrets", static_dir=STATIC)
    with pytest.raises(RuntimeConfigInvalid) as raised:
        _api(tmp_path, config=config)
    assert raised.value.variable == "VIGIA_PUBLIC_ORIGIN"
    missing = _runtime(VIGIA_BREACH_LIST_PATH=str(tmp_path / "no-existe.txt"))
    with pytest.raises(RuntimeConfigInvalid) as raised:
        _api(tmp_path, missing)
    assert raised.value.variable == "VIGIA_BREACH_LIST_PATH"


@pytest.mark.parametrize(
    "reader",
    [
        FakeReader(error=SecretNotFound()),
        FakeReader(value=database_secret(host=None)),  # sin host: no es el formato de RDS
        FakeReader(value="no es JSON " + SECRET_PASSWORD),
        FakeReader(value=database_secret(port="cinco")),
    ],
    ids=["inexistente", "sin-host", "no-json", "puerto"],
)
def test_a_bad_database_secret_names_the_variable_without_its_value(
    reader: FakeReader, tmp_path: Path
) -> None:
    with pytest.raises(RuntimeConfigInvalid) as raised:
        asyncio.run(compose_worker_runtime(_worker_config(), _runtime(), reader=reader))
    assert raised.value.variable == "VIGIA_DB_APP_SECRET"
    assert "VIGIA_DB_APP_SECRET" in str(raised.value)
    assert SECRET_PASSWORD not in str(raised.value)
    assert SECRET_PASSWORD not in repr(raised.value)


def test_root_databases_have_the_pools_of_each_process(tmp_path: Path) -> None:
    api = _api(tmp_path)
    worker = asyncio.run(compose_worker_runtime(_worker_config(), _runtime(), reader=FakeReader()))
    assert getattr(api.database, "process", None) is ProcessKind.API
    assert getattr(worker.database, "process", None) is ProcessKind.WORKER
    assert worker.catalog is not None and not worker.catalog.sealed  # lo sella el arranque
    assert uuid.UUID(str(PROVIDER_ID)) == PROVIDER_ID
