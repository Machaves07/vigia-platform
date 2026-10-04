"""Constructor de producción de ``vigia-admin`` (A-52; LC-NUC-07).

``VIGIA_ADMIN_RUNTIME=vigia_platform.shared.runtime.admin:build_admin_runtime``. Recibe la
``AdminConfig`` y el identificador de la organización proveedora (nuevo en un ``bootstrap``; si
no, ``VIGIA_PROVIDER_ORGANIZATION_ID``), lee ``RuntimeConfig`` una vez del entorno y devuelve el
``AdminRuntime`` con los **mismos servicios de aplicación** que la interfaz.

**Base perezosa.** La conexión a ``vigia_app`` (y la lectura del secreto de
``VIGIA_DB_APP_SECRET``) se hace en la primera operación de datos de la orden (``LazyDatabase``).
Construir el ``AdminRuntime`` no toca la base: ``restore-audit-partition``, que solo lee
``vigia-archive``, funciona sin alcance a las subredes de datos (runbook 6.5; comentario de
VIG-98 del 2026-10-03, punto 2). Un secreto inexistente aparece al usar la base, como
``RuntimeConfigInvalid`` sobre la variable.

**Dependencias condicionales**: ``node_ca`` (``NodeCaPublisher``) solo con
``VIGIA_NODE_CA_KEY_ARN`` y ``VIGIA_EDGE_BUCKET`` (``first_deploy`` o ``ca_rotation``); ``archive``
solo con ``VIGIA_ARCHIVE_BUCKET``. Sin ellas, la orden que las necesita se niega con su mensaje.

El ``SigningService`` lleva ``LedgerRotationRecorder`` (``shared.runtime.core``): ``vigia-admin``
nunca rota una clave sin su auditoría en la misma transacción.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Sequence
from typing import Final

from vigia_platform.identity.application.admin_cli import AdminConfig, AdminRuntime
from vigia_platform.identity.application.common import IdentityDependencies
from vigia_platform.identity.application.hierarchy import OrganizationGenesis
from vigia_platform.identity.application.invitations import EmailSenderRegistry
from vigia_platform.shared.archive.partitions import PartitionMaintenance
from vigia_platform.shared.archive.restore_drill import RestoreDrills
from vigia_platform.shared.clock import Clock, SystemClock
from vigia_platform.shared.db import Database, ProcessKind
from vigia_platform.shared.node_ca import NodeCaPublisher
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics
from vigia_platform.shared.outbox.replay import DeadLetterReplay
from vigia_platform.shared.runtime.config import RuntimeConfig
from vigia_platform.shared.runtime.core import (
    build_core,
    load_credentials,
    open_database,
    s3_storage,
)
from vigia_platform.shared.runtime.db_credentials import LazyDatabase, SecretStringReader
from vigia_platform.shared.runtime.units import PlatformUnit, registered_units

__all__ = ["ADMIN_POOL_SIZE", "build_admin_runtime", "compose_admin_runtime"]

ADMIN_POOL_SIZE: Final = 4
"""Conexiones de una orden administrativa: una tarea puntual que hace pocas a la vez."""
_UNSET_LINK_BASE: Final = "https://vigia.invalid"
"""Base de enlaces cuando falta ``VIGIA_PUBLIC_ORIGIN`` (``.invalid``, RFC 2606: no resuelve).
Toda orden que emite una invitación exige ``VIGIA_PUBLIC_ORIGIN`` antes de llegar aquí."""


async def build_admin_runtime(
    config: AdminConfig, provider_organization_id: uuid.UUID
) -> AdminRuntime:
    """El constructor de ``VIGIA_ADMIN_RUNTIME``: ``RuntimeConfig`` del entorno del proceso."""
    return await compose_admin_runtime(
        config, provider_organization_id, RuntimeConfig.from_environ(os.environ)
    )


async def compose_admin_runtime(
    config: AdminConfig,
    provider_organization_id: uuid.UUID,
    runtime: RuntimeConfig,
    *,
    clock: Clock | None = None,
    metrics: PlatformMetrics | None = None,
    units: Sequence[PlatformUnit] | None = None,
    reader: SecretStringReader | None = None,
) -> AdminRuntime:
    """El ``AdminRuntime`` sin abrir la base (las pruebas inyectan reloj y lector)."""
    clock = clock if clock is not None else SystemClock()
    metrics = metrics if metrics is not None else get_metrics()
    selected = tuple(units) if units is not None else registered_units()
    provider = provider_organization_id

    async def open_lazily() -> Database:
        credentials = await load_credentials(runtime, reader)
        return open_database(
            runtime, ProcessKind.WORKER, credentials, metrics, worker_pool_size=ADMIN_POOL_SIZE
        )

    database = LazyDatabase(open_lazily)
    archive = (
        s3_storage(runtime, config.archive_bucket, clock)
        if config.archive_bucket is not None
        else None
    )
    core = build_core(
        runtime,
        database=database,
        clock=clock,
        metrics=metrics,
        provider_organization_id=provider,
        units=selected,
        archive=archive,
    )
    services = core.services
    deps = IdentityDependencies(
        database=database,
        writer=services.writer,
        audit=services.audit,
        outbox=services.outbox,
        authorizer=services.authorizer,
        free_text=core.free_text,
        clock=clock,
        provider_organization_id=provider,
    )
    node_ca = None
    if config.node_ca_key_id is not None and config.edge_bucket is not None:
        node_ca = NodeCaPublisher(
            storage=s3_storage(runtime, config.edge_bucket, clock),
            kms=core.kms,
            environment=config.environment,
            random_bytes=os.urandom,
            object_key=config.root_certificate_key,
        )
    return AdminRuntime(
        clock=clock,
        database=database,
        contexts=services.contexts,
        authorizer=services.authorizer,
        genesis=OrganizationGenesis(
            deps,
            senders=EmailSenderRegistry(),
            link_base=config.link_base if config.link_base is not None else _UNSET_LINK_BASE,
        ),
        signing=services.signing,
        replay=DeadLetterReplay(
            database=database, authorizer=services.authorizer, audit=services.audit, clock=clock
        ),
        partitions=PartitionMaintenance(clock=clock, metrics=metrics),
        drills=RestoreDrills(database=database, audit=services.audit, clock=clock, metrics=metrics),
        secrets=core.secrets,
        audit=services.audit,
        node_ca=node_ca,
        archive=archive,
        registries=core.synchronizers,
    )
