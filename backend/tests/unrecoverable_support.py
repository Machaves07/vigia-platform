"""DR-LAZ-04 sin versión anterior sobre PostgreSQL real (VIG-274; runbook 7.3, paso 4).

Lo comparten ``tests/integration/test_laz_unrecoverable_attachment.py`` y el ensayo
``tests/resilience/test_dr_laz_03_04.py`` (caso ``no_previous_version``):

- ``prepare(stack)``: sobre ``actions_stack`` (registrado con ``register_u04_and_catalog``), el
  texto del estándar citado, un hallazgo con su acción, una entrada de evidencia con dos adjuntos
  y el cierre, todo por las rutas reales; después el objeto de uno de los adjuntos desaparece del
  almacén, como cuando no hay versión anterior que restaurar.
- ``cli(stack, …)``: la orden real ``vigia-admin`` con la raíz de producción
  (``compose_admin_runtime``).
- ``compose(stack, job, checkpoints)``: el compositor real sobre el expediente
  (``LectorExpediente``) y ``StorageBlobs`` sobre el almacén, cuyo objeto ausente falla como S3
  (``ClientError`` ``NoSuchKey``).
- ``declare_and_export(stack, lost)``: declara el adjunto con la orden y compone la exportación
  posterior del alcance; devuelve el registro del hallazgo y la entrada del manifiesto para
  compararlos por igualdad.

Solo datos generados. Reloj simulado desde la hora de la base (retro 14); topes de 60 s.
"""

from __future__ import annotations

import io
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from botocore.exceptions import ClientError

from tests.export_stream_support import MemoryExportStore, RecordingSink, read_volume
from tests.laz_support import InstrumentedPorts, LoopWorld
from tests.loop_actions_support import ActionsStack, Who
from tests.properties.laz.strategies.export import WorldPorts, job_world
from tests.runtime_support import FakeReader, database_secret, runtime_environ
from tests.signing_support import bootstrapped_world
from tests.writer_support import unit_context
from vigia_platform.accreditation.adapters.postgres.export_reads import (
    SqlCheckpointLookup,
    SqlEpisodeHistory,
    SqlExportPlaces,
    StorageBlobs,
)
from vigia_platform.accreditation.export.composer_port import ExportJob, ExportPackage
from vigia_platform.accreditation.export.contents import ExportComposer, ExportPorts
from vigia_platform.accreditation.export.stream import ExportStream, ExportStreamSettings
from vigia_platform.accreditation.export.volumes import ExportLocation
from vigia_platform.identity.application import admin_cli
from vigia_platform.ledger.adapters.checkpoint_store import SqlCheckpointStore
from vigia_platform.ledger.application.evidence_read import EvidenceService
from vigia_platform.ledger.application.reader import LectorExpediente
from vigia_platform.ledger.chain.checkpoints import CheckpointChain, CheckpointService
from vigia_platform.loop.domain.enums import PackageForm
from vigia_platform.shared.context import ActorKind, ActorUnit, Role, ScopeLevel
from vigia_platform.shared.runtime.admin import compose_admin_runtime
from vigia_platform.shared.runtime.config import RuntimeConfig
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

__all__ = [
    "COMMAND",
    "OPERATION",
    "Declared",
    "Lost",
    "arguments",
    "audit",
    "checkpoint_service",
    "cli",
    "compose",
    "declarations",
    "declare_and_export",
    "export_job",
    "ledger_now",
    "prepare",
]

KIB: Final = 1024
SETTINGS: Final = ExportStreamSettings(
    part_size=5 * 1024 * KIB, volume_max_bytes=4 * 1024 * KIB, manifest_reserve_bytes=128 * KIB
)
COMMAND: Final = "declare-unrecoverable-attachment"
OPERATION: Final = "closure_attachment_unrecoverable_declared"


def cli(stack: ActionsStack, *arguments: str, stdin: str = "") -> tuple[int, Any, Any]:
    """La orden real con la raíz de producción de ``vigia-admin`` (como TASK-326)."""
    authz = stack.world.stack.authz
    endpoint = authz.sessions.migrated.as_role("vigia_app")
    runtime = RuntimeConfig.from_environ(
        runtime_environ(
            VIGIA_PROVIDER_ORGANIZATION_ID=str(authz.provider_organization_id),
            VIGIA_DB_STATEMENT_TIMEOUT_MS="60000",
            VIGIA_DB_POOL_TIMEOUT_SECONDS="60",
        )
    )
    reader = FakeReader(
        database_secret(
            host=endpoint.host,
            port=endpoint.port,
            dbname=endpoint.database,
            password=endpoint.password,
        )
    )

    async def build(config: Any, provider: uuid.UUID) -> Any:
        return await compose_admin_runtime(
            config, provider, runtime, clock=authz.sessions.clock, reader=reader
        )

    out, err = io.StringIO(), io.StringIO()
    code = admin_cli.run(
        [COMMAND, *arguments, "--operator", str(authz.operator_id)],
        environ={
            "VIGIA_ENVIRONMENT": "test",
            "VIGIA_PROVIDER_ORGANIZATION_ID": str(authz.provider_organization_id),
        },
        builder=build,
        stdout=out,
        stderr=err,
        stdin=io.StringIO(stdin),
    )
    output = json.loads(out.getvalue()) if out.getvalue() else None
    # La línea JSON del error va detrás de la pregunta de confirmación, si la hubo.
    text = err.getvalue()
    error = json.loads(text[text.rindex('{"error"') :]) if '{"error"' in text else None
    return code, output, error


def arguments(
    stack: ActionsStack, claim: dict[str, Any], *, detected_at: Any, **changes: str
) -> list[str]:
    a = stack.world.a
    values = {
        "--organization": str(a.organization_id),
        "--plant": str(a.plant_id),
        "--attachment": claim["attachment_id"],
        "--sha256": claim["sha256"],
        "--detected-at": format_timestamp(detected_at),
        **changes,
    }
    return [item for pair in values.items() for item in pair]


def declarations(stack: ActionsStack) -> list[Any]:
    return stack.fetch(
        "SELECT record_id, content, plant_id, scope_zone_id, actor_unit"
        " FROM ledger.ledger_record WHERE organization_id = $1"
        " AND record_type = 'closure_attachment_unrecoverable' ORDER BY chain_sequence",
        stack.world.a.organization_id,
    )


def audit(stack: ActionsStack) -> list[Any]:
    return stack.fetch(
        "SELECT outcome FROM shared.audit_entry WHERE organization_id = $1 AND operation = $2",
        stack.world.stack.authz.provider_organization_id,
        OPERATION,
    )


class _Objects:
    """``get_object`` sobre el almacén en memoria del mundo; un objeto que falta responde como
    S3 (``ClientError`` ``NoSuchKey``, que ``S3Storage`` no convierte en transitorio)."""

    def __init__(self, stack: ActionsStack) -> None:
        self._store = stack.store

    async def get_object(self, key: str, *, version_id: str | None = None) -> bytes:
        found = self._store.objects.get(key)
        if found is None:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "The specified key does not exist."}},
                "GetObject",
            )
        return found[0]


def ledger_now(stack: ActionsStack) -> datetime:
    """El reloj con que la base sella ``received_at``, un milisegundo por delante."""
    (row,) = stack.fetch("SELECT clock_timestamp() AS now")
    moment: datetime = to_millisecond(row["now"]) + timedelta(milliseconds=1)
    return moment


def checkpoint_service(world: LoopWorld) -> CheckpointService:
    database = world.stack.primary.database
    sessions = world.stack.authz.sessions
    signing = world.stack.run(bootstrapped_world())
    return CheckpointService(
        store=SqlCheckpointStore(
            database=database,
            writer=world.stack.primary.writer,
            audit=sessions.audit,
            outbox=world.stack.outbox,
        ),
        signer=signing.service,
        clock=sessions.clock,
    )


def export_job(
    world: LoopWorld, checkpoints: CheckpointService, start: datetime, until: datetime
) -> ExportJob:
    """La exportación de la zona de ``world.a`` en ``[start, until)``, con puntos de control
    tomados ahora (U-02)."""
    a = world.a
    u02 = unit_context(a.organization_id, ActorUnit.U02, kind=ActorKind.SYSTEM)
    results = world.stack.run(
        checkpoints.write_checkpoints_now(
            u02, (CheckpointChain.plant(a.plant_id), CheckpointChain.audit())
        )
    )
    ids = tuple(r.checkpoint.entry_id for r in results if r.checkpoint is not None)
    return ExportJob(
        export_id=uuid.UUID(int=0x274),
        organization_id=a.organization_id,
        plant_id=a.plant_id,
        zone_ids=(a.zone_id,),
        period_from=to_millisecond(start),
        period_to=to_millisecond(until),
        package_form=PackageForm.WITHOUT_CLIPS,
        requested_by={"user_id": str(uuid.uuid4()), "display_name": "P", "role_in_use": None},
        correlation_id=uuid.uuid4(),
        attempt=1,
        generated_at=to_millisecond(until + timedelta(seconds=1)),
        checkpoint_record_ids=ids,
        context=world.event_context(a),
    )


def compose(
    stack: ActionsStack, job: ExportJob, checkpoints: CheckpointService
) -> tuple[ExportPackage, list[dict[str, Any]], set[str]]:
    """El compositor real sobre el expediente: el paquete, los hallazgos de la zona y las
    entradas de ``vol-01``."""
    world = stack.world
    database = world.stack.primary.database
    sessions = world.stack.authz.sessions
    doubles = WorldPorts(job_world(job))
    counted = InstrumentedPorts(
        organization_id=job.organization_id, plant_id=job.plant_id, wrapped=world.real_ports()
    )
    ports = ExportPorts(
        reader=LectorExpediente(database=database, audit=sessions.audit, clock=sessions.clock),
        catalog=counted,
        gates=counted,
        fleet=doubles,
        coverage=counted,
        markers=EvidenceService(
            database=database, audit=sessions.audit, storage=world.stack.storage
        ),
        checkpoints=SqlCheckpointLookup(database, checkpoints),
        places=SqlExportPlaces(database),
        blobs=StorageBlobs(database, _Objects(stack)),
        episodes=SqlEpisodeHistory(database),
        signer=doubles,
    )
    store = MemoryExportStore()
    composer = ExportComposer(stream=ExportStream(store=store, settings=SETTINGS), ports=ports)
    package: ExportPackage = world.stack.run(composer.compose(job, RecordingSink()))
    location = ExportLocation(job.organization_id, job.plant_id, job.export_id)
    archive = read_volume(store.latest(location.volume_key(1)))
    findings = json.loads(archive.read(f"findings/zone-{world.a.zone_id}.json"))["findings"]
    return package, findings, set(archive.namelist())


@dataclass(frozen=True)
class Lost:
    """Un hallazgo cerrado con dos adjuntos, uno perdido del almacén sin versión anterior."""

    finding: Any
    action_id: uuid.UUID
    lost: dict[str, Any]
    kept: dict[str, Any]
    closed: dict[str, Any]
    start: datetime
    detected: datetime


def prepare(stack: ActionsStack) -> Lost:
    world = stack.world
    a = world.a
    # El texto declarado del estándar que cita el hallazgo (BR-LAZ-95).
    world.stack.execute(
        "INSERT INTO catalog.declared_standard_version (organization_id, plant_id, zone_id,"
        " standard_id, version, family, title_es, declared_text, declared_by, effective_from,"
        " predicate, catalog_version, reason_es) VALUES ($1, $2, $3, $4, 1, 'coexistence',"
        " 'Zona de prensa', 'Ninguna persona dentro del perímetro con la máquina energizada.',"
        " $5, $6, '{}', 1, 'Versión sintética del estándar') ON CONFLICT DO NOTHING",
        a.organization_id,
        a.plant_id,
        a.zone_id,
        a.site.standard_id,
        json.dumps(
            {"user_id": str(uuid.uuid4()), "display_name": "Firmante", "role": "administrator"}
        ),
        world.stack.now() - timedelta(days=400),
    )
    start = ledger_now(stack)
    stack.tick(5)
    coordinator: Who = stack.member(a)
    assignee = stack.user(a, Role.COORDINATOR_SST, ScopeLevel.ZONE, a.zone_id)
    finding, action_id = stack.assigned(coordinator, assignee)
    lost, kept = stack.uploaded(finding), stack.uploaded(finding)
    response = stack.add_evidence(
        coordinator, action_id, stack.evidence_body(attachments=[lost, kept])
    )
    assert response.status_code == 201, response.text
    response = stack.close(coordinator, finding, stack.closure_body())
    assert response.status_code == 201, response.text
    closed = world.finding_state(finding)
    assert closed is not None and closed["lifecycle_state"] == "closed"
    # El objeto del adjunto se pierde sin versión anterior (el almacén ya no lo tiene).
    (lost_key,) = [
        row["storage_key"]
        for row in stack.attachment_rows(finding)
        if str(row["attachment_id"]) == lost["attachment_id"]
    ]
    del stack.store.objects[lost_key]
    stack.tick(5)
    detected = to_millisecond(stack.now()) - timedelta(minutes=1)
    return Lost(finding, action_id, lost, kept, dict(closed), start, detected)


@dataclass(frozen=True)
class Declared:
    """Lo que queda en el hallazgo y en el manifiesto tras declarar y volver a exportar."""

    record_id: str
    content: dict[str, Any]
    """El contenido del registro ``closure_attachment_unrecoverable`` en el expediente."""
    exported: dict[str, Any]
    """El hallazgo en ``findings/zone-….json`` del paquete."""
    entry: dict[str, Any]
    """La entrada del adjunto en ``manifest.attachments``."""
    package: ExportPackage
    names: set[str]


def declare_and_export(stack: ActionsStack, lost: Lost, checkpoints: CheckpointService) -> Declared:
    code, output, error = cli(
        stack, *arguments(stack, lost.lost, detected_at=lost.detected), "--yes"
    )
    assert (code, error) == (admin_cli.ExitCode.OK, None), error
    (record,) = declarations(stack)
    assert output["record_id"] == str(record["record_id"])
    content = json.loads(bytes(record["content"]))  # los bytes canónicos del expediente
    stack.tick(5)
    job = export_job(stack.world, checkpoints, lost.start, ledger_now(stack))
    package, findings, names = compose(stack, job, checkpoints)
    (exported,) = [f for f in findings if f["subject_id"] == str(lost.finding.subject_id)]
    entries = {item["attachment_id"]: item for item in package.manifest["attachments"]}
    return Declared(
        str(record["record_id"]),
        content,
        exported,
        entries[lost.lost["attachment_id"]],
        package,
        names,
    )
