"""Apoyo de las pruebas de la raíz de composición de producción (VIG-137, A-52).

- ``PROBE_UNIT``: una unidad de prueba con una ruta, un tipo de registro, un evento, un
  consumidor, una tarea periódica, una enumeración con etiqueta y un servicio en ``app.state``.
  Se añade al registro por unidad con ``with_probe_unit`` (``monkeypatch`` de
  ``REGISTERED_UNITS``), sin tocar ningún constructor.
- ``FakeReader``: el ``SecretStringReader`` de la raíz con un secreto fijo, contando lecturas.
- ``runtime_environ``: las variables mínimas de ``RuntimeConfig`` para componer sin red.
- ``probe_labels``: ``labels.platform.es.json`` con la etiqueta de la enumeración de prueba.

Solo datos generados.
"""

from __future__ import annotations

import enum
import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi import APIRouter
from vigia_contracts.models.common import UUID

from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.registry import ChainLevel, ContentModel, RecordType, RecordTypeRegistry
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.labels import DEFAULT_LABELS_PATH
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.outbox.registries import (
    Consumer,
    ConsumerRegistry,
    EventType,
    EventTypeRegistry,
    PayloadModel,
    PeriodicTaskRegistry,
    Schedule,
)
from vigia_platform.shared.runtime import units
from vigia_platform.shared.runtime.units import PlatformUnit, UnitServices

__all__ = [
    "PROBE_CONSUMER",
    "PROBE_ENUMERATION",
    "PROBE_EVENT",
    "PROBE_PATH",
    "PROBE_RECORD_TYPE",
    "PROBE_STATE_KEY",
    "PROBE_TASK",
    "PROBE_UNIT",
    "PROVIDER_ID",
    "FakeReader",
    "ProbeState",
    "breach_list",
    "database_secret",
    "probe_labels",
    "runtime_environ",
    "with_probe_unit",
]

PROVIDER_ID: Final = uuid.UUID("0192f2b0-0000-4000-8000-0000000000ab")
PROBE_RECORD_TYPE: Final = "runtime_unit_probe"
PROBE_EVENT: Final = "runtime_unit_probed"
PROBE_CONSUMER: Final = "runtime_unit_listener"
PROBE_TASK: Final = "runtime_unit_sweep"
PROBE_PATH: Final = "/runtime-unit-probe"
PROBE_STATE_KEY: Final = "runtime_unit_probe"
PROBE_ENUMERATION: Final = "runtime_unit_state"
SECRET_PASSWORD: Final = "clave-sintetica-NO-DEBE-SALIR-7Qz"  # noqa: S105 - dato sintético


class ProbeState(enum.StrEnum):
    READY = "ready"


class ProbeContent(ContentModel):
    probe_id: UUID


class ProbePayload(PayloadModel):
    probe_id: UUID


@dataclass(frozen=True)
class ProbeService:
    """Lo que la unidad de prueba deja en ``app.state``: sabe con qué proveedora se construyó."""

    provider_organization_id: uuid.UUID


async def _consume(*_: Any) -> None:
    return None


async def _sweep(transaction: Transaction) -> None:
    raise AssertionError("la tarea de la unidad de prueba no debía ejecutarse")


def _routers() -> tuple[APIRouter, ...]:
    router = APIRouter()

    async def probe() -> dict[str, str]:
        return {"status": ProbeState.READY.value}

    router.add_api_route(
        PROBE_PATH,
        probe,
        methods=["GET"],
        dependencies=[requires(PermissionKey.FINDINGS_READ.value)],
    )
    return (router,)


def _record_types(registry: RecordTypeRegistry) -> None:
    registry.register(
        RecordType(
            record_type=PROBE_RECORD_TYPE,
            writer_unit=ActorUnit.U03,
            chain_level=ChainLevel.ORGANIZATION,
            schema_version=1,
            content_model=ProbeContent,
            source_key_path="/probe_id",
        )
    )


def _event_types(registry: EventTypeRegistry) -> None:
    registry.register(
        EventType(
            event_name=PROBE_EVENT,
            publisher_unit=ActorUnit.U03,
            payload_model=ProbePayload,
            description_es="Evento sintético de la unidad de prueba",
        )
    )


def _consumers(registry: ConsumerRegistry, services: UnitServices) -> None:
    registry.register(
        Consumer(
            consumer_name=PROBE_CONSUMER,
            unit=ActorUnit.U03,
            subscribed_events=(PROBE_EVENT,),
            handler=_consume,
        )
    )


def _tasks(registry: PeriodicTaskRegistry, services: UnitServices) -> None:
    registry.register(PROBE_TASK, Schedule.daily(hour=3), _sweep, unit=ActorUnit.U03)


def _state(services: UnitServices) -> Mapping[str, object]:
    return {PROBE_STATE_KEY: ProbeService(services.provider_organization_id)}


PROBE_UNIT: Final = PlatformUnit(
    name="runtime_probe",
    routers=_routers,
    labels={PROBE_ENUMERATION: ProbeState},
    record_types=_record_types,
    event_types=_event_types,
    consumers=_consumers,
    periodic_tasks=_tasks,
    api_state=_state,
)


def with_probe_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Añade ``PROBE_UNIT`` al registro por unidad: ningún constructor se edita."""
    monkeypatch.setattr(units, "REGISTERED_UNITS", (*units.REGISTERED_UNITS, PROBE_UNIT))


def probe_labels(directory: Path) -> Path:
    """Copia del archivo de etiquetas con la enumeración de la unidad de prueba."""
    document = json.loads(DEFAULT_LABELS_PATH.read_text(encoding="utf-8"))
    document[PROBE_ENUMERATION] = {ProbeState.READY.value: "Lista"}
    path = directory / "labels.platform.es.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def breach_list(directory: Path) -> Path:
    """Respaldo local sintético de contraseñas filtradas (SHA-1 en mayúsculas)."""
    path = directory / "pwned-sintetico.txt"
    digests = sorted(
        hashlib.sha1(word.encode(), usedforsecurity=False).hexdigest().upper()
        for word in ("password", "123456", "qwerty")
    )
    path.write_text("\n".join(digests) + "\n", encoding="ascii")
    return path


def database_secret(
    *,
    host: str = "db.vigia.invalid",
    port: int = 5432,
    dbname: str = "vigia",
    password: str = SECRET_PASSWORD,
    **changes: Any,
) -> str:
    document: dict[str, Any] = {
        "engine": "postgres",
        "host": host,
        "port": port,
        "dbname": dbname,
        "username": "vigia_app",
        "password": password,
    }
    document.update(changes)
    return json.dumps({k: v for k, v in document.items() if v is not None})


@dataclass
class FakeReader:
    """``SecretStringReader`` con un valor fijo (o un error), contando lecturas."""

    value: str = ""
    error: Exception | None = None
    reads: int = 0

    async def read(self, secret_id: str) -> str:
        self.reads += 1
        if self.error is not None:
            raise self.error
        return self.value or database_secret()


def runtime_environ(**changes: str | None) -> dict[str, str]:
    """Variables de ``RuntimeConfig`` para componer sin red (sin ``VIGIA_AWS_ENDPOINT_URL``)."""
    environ: dict[str, str | None] = {
        "VIGIA_ENVIRONMENT": "test",
        "AWS_REGION": "us-east-1",
        "VIGIA_DB_APP_SECRET": "vigia/test/db/app",
        "VIGIA_SIGNING_SECRET_PREFIX": "vigia/test/signing/",
        "VIGIA_SECRETS_KEY_ARN": "alias/vigia-secrets",
        "VIGIA_PROVIDER_ORGANIZATION_ID": str(PROVIDER_ID),
        "VIGIA_EVIDENCE_BUCKET": "vigia-evidence-test",
        "VIGIA_ARCHIVE_BUCKET": "vigia-archive-test",
        "VIGIA_NODE_CA_KEY_ARN": "alias/vigia-node-ca",
        "PGSSLMODE": "disable",
    }
    environ.update(changes)
    return {key: value for key, value in environ.items() if value is not None}
