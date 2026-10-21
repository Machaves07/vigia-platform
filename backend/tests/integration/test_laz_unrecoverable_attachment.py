"""DR-LAZ-04 sin versión anterior de punta a punta (VIG-274; runbook 7.3, paso 4).

Sobre PostgreSQL 16 real (``actions_stack``: hallazgo ingresado por el contrato, acción, entrada
de evidencia con dos adjuntos y cierre firmados por las rutas reales) y la orden real
``vigia-admin declare-unrecoverable-attachment`` con la raíz de producción
(``compose_admin_runtime``):

- **La declaración en el hallazgo**: un registro ``closure_attachment_unrecoverable`` en la cadena
  de la planta con el hash y la fecha del adjunto, escrito con el contexto de U-04, y la intención y
  el resultado auditados en la proveedora. El hallazgo sigue ``closed`` y su fila de la proyección
  no cambia (P4, P5).
- **Guardas** (fallo cerrado; nada escrito): ``--dry-run``, otro hash, otra planta, un adjunto que
  no existe, una fecha futura y sin confirmación. Repetir la orden con los mismos datos es un
  duplicado aceptado (un solo registro); con otra fecha, ``already_declared``.
- **La exportación posterior** (``ExportComposer`` sobre el expediente real con
  ``LectorExpediente`` y ``StorageBlobs`` sobre el almacén sin el objeto): antes de declarar,
  falla; después, se compone sin pedir los bytes perdidos, el manifiesto declara el adjunto con
  el **mismo** campo estructurado que el registro del hallazgo, el hallazgo va ``closed`` con su
  cierre y dos composiciones del mismo estado dan el mismo ``archive_sha256``.

Solo datos generados. Reloj simulado desde la hora de la base (retro 14); topes de 60 s.
"""

from __future__ import annotations

import io
import json
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, Final

import pytest

from tests.export_stream_support import MemoryExportStore, RecordingSink, read_volume
from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import InstrumentedPorts, register_u04_and_catalog
from tests.loop_actions_support import ActionsStack, Who, actions_stack
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
from vigia_platform.accreditation.manifest.model import ATTACHMENT_UNRECOVERABLE_ES
from vigia_platform.identity.application import admin_cli
from vigia_platform.ledger.adapters.checkpoint_store import SqlCheckpointStore
from vigia_platform.ledger.application.evidence_read import EvidenceService
from vigia_platform.ledger.application.reader import LectorExpediente
from vigia_platform.ledger.chain.checkpoints import CheckpointChain, CheckpointService
from vigia_platform.loop.application.unrecoverable import declaration_id
from vigia_platform.loop.domain.enums import PackageForm
from vigia_platform.shared.context import ActorKind, ActorUnit, Role, ScopeLevel
from vigia_platform.shared.runtime.admin import compose_admin_runtime
from vigia_platform.shared.runtime.config import RuntimeConfig
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

pytestmark = pytest.mark.integration

KIB: Final = 1024
SETTINGS: Final = ExportStreamSettings(
    part_size=5 * 1024 * KIB, volume_max_bytes=4 * 1024 * KIB, manifest_reserve_bytes=128 * KIB
)
COMMAND: Final = "declare-unrecoverable-attachment"
OPERATION: Final = "closure_attachment_unrecoverable_declared"


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[ActionsStack]:
    with actions_stack(
        postgres_endpoint, "laz_unrecoverable", register=register_u04_and_catalog
    ) as built:
        yield built


def _cli(stack: ActionsStack, *arguments: str, stdin: str = "") -> tuple[int, Any, Any]:
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


def _arguments(
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


def _declarations(stack: ActionsStack) -> list[Any]:
    return stack.fetch(
        "SELECT record_id, content, plant_id, scope_zone_id, actor_unit"
        " FROM ledger.ledger_record WHERE organization_id = $1"
        " AND record_type = 'closure_attachment_unrecoverable' ORDER BY chain_sequence",
        stack.world.a.organization_id,
    )


def _audit(stack: ActionsStack) -> list[Any]:
    return stack.fetch(
        "SELECT outcome FROM shared.audit_entry WHERE organization_id = $1 AND operation = $2",
        stack.world.stack.authz.provider_organization_id,
        OPERATION,
    )


class _Objects:
    """``get_object`` sobre el almacén en memoria del mundo: un objeto que falta, falta."""

    def __init__(self, stack: ActionsStack) -> None:
        self._store = stack.store

    async def get_object(self, key: str, *, version_id: str | None = None) -> bytes:
        return self._store.objects[key][0]


def _ledger_now(stack: ActionsStack) -> Any:
    (row,) = stack.fetch("SELECT clock_timestamp() AS now")
    return to_millisecond(row["now"]) + timedelta(milliseconds=1)


def _compose(stack: ActionsStack, job: ExportJob, checkpoints: CheckpointService) -> Any:
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


def test_the_declaration_is_written_once_and_the_next_export_declares_it(
    stack: ActionsStack,
) -> None:
    world = stack.world
    a = world.a
    # El texto declarado del estándar que cita el hallazgo (BR-LAZ-95).
    world.stack.execute(
        "INSERT INTO catalog.declared_standard_version (organization_id, plant_id, zone_id,"
        " standard_id, version, family, title_es, declared_text, declared_by, effective_from,"
        " predicate, catalog_version, reason_es) VALUES ($1, $2, $3, $4, 1, 'coexistence',"
        " 'Zona de prensa', 'Ninguna persona dentro del perímetro con la máquina energizada.',"
        " $5, $6, '{}', 1, 'Versión sintética del estándar')",
        a.organization_id,
        a.plant_id,
        a.zone_id,
        a.site.standard_id,
        json.dumps(
            {"user_id": str(uuid.uuid4()), "display_name": "Firmante", "role": "administrator"}
        ),
        world.stack.now() - timedelta(days=400),
    )
    start = _ledger_now(stack)
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
    arguments = _arguments(stack, lost, detected_at=detected)

    # Un adjunto de la otra organización, con su planta o con la de la primera: no existe.
    b = world.b
    foreign_coordinator: Who = stack.member(b)
    foreign_assignee = stack.user(b, Role.COORDINATOR_SST, ScopeLevel.ZONE, b.zone_id)
    foreign, foreign_action = stack.assigned(foreign_coordinator, foreign_assignee, b)
    foreign_claim = stack.uploaded(foreign)
    response = stack.add_evidence(
        foreign_coordinator, foreign_action, stack.evidence_body(attachments=[foreign_claim])
    )
    assert response.status_code == 201, response.text

    # Guardas: nada se escribe.
    for plant in (a.plant_id, b.plant_id):
        code, _, error = _cli(
            stack,
            *_arguments(stack, foreign_claim, detected_at=detected, **{"--plant": str(plant)}),
            "--yes",
        )
        assert (code, error["error"]) == (admin_cli.ExitCode.REJECTED, "not_found")
    assert _cli(stack, *arguments, "--dry-run")[0] == admin_cli.ExitCode.OK
    code, _, error = _cli(stack, *arguments)  # sin --yes ni confirmación
    assert (code, error["error"]) == (admin_cli.ExitCode.NOT_CONFIRMED, "not_confirmed")
    for changes, expected in (
        ({"--sha256": kept["sha256"]}, "sha256_mismatch"),
        ({"--plant": str(world.add_plant(a))}, "not_found"),
        ({"--attachment": str(uuid.uuid4())}, "not_found"),
        (
            {"--detected-at": format_timestamp(stack.now() + timedelta(hours=1))},
            "detected_at_invalid",
        ),
    ):
        code, _, error = _cli(
            stack, *_arguments(stack, lost, detected_at=detected, **changes), "--yes"
        )
        assert (code, error["error"]) == (admin_cli.ExitCode.REJECTED, expected), changes
    assert _declarations(stack) == [] and _audit(stack) == []

    # Antes de declarar, la exportación del alcance falla: nunca silencio.
    database = world.stack.primary.database
    sessions = world.stack.authz.sessions
    signing = world.stack.run(bootstrapped_world())
    checkpoints = CheckpointService(
        store=SqlCheckpointStore(
            database=database,
            writer=world.stack.primary.writer,
            audit=sessions.audit,
            outbox=world.stack.outbox,
        ),
        signer=signing.service,
        clock=sessions.clock,
    )

    def job(generated_at: Any) -> ExportJob:
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
            period_to=to_millisecond(generated_at),
            package_form=PackageForm.WITHOUT_CLIPS,
            requested_by={"user_id": str(uuid.uuid4()), "display_name": "P", "role_in_use": None},
            correlation_id=uuid.uuid4(),
            attempt=1,
            generated_at=to_millisecond(generated_at + timedelta(seconds=1)),
            checkpoint_record_ids=ids,
            context=world.event_context(a),
        )

    with pytest.raises(KeyError):
        _compose(stack, job(_ledger_now(stack)), checkpoints)

    # La orden real.
    code, output, error = _cli(stack, *arguments, "--yes")
    assert (code, error) == (admin_cli.ExitCode.OK, None)
    (record,) = _declarations(stack)
    content = json.loads(bytes(record["content"]))  # los bytes canónicos del expediente
    (entry,) = stack.entry_rows(action_id)
    assert content == {
        "declaration_id": str(declaration_id(uuid.UUID(lost["attachment_id"]))),
        "subject_kind": "finding",
        "subject_id": str(finding.subject_id),
        "action_id": str(action_id),
        "evidence_entry_id": str(entry["evidence_entry_id"]),
        "evidence_record_id": str(entry["record_id"]),
        "attachment_id": lost["attachment_id"],
        "sha256": lost["sha256"],
        "size_bytes": lost["size_bytes"],
        "content_type": lost["content_type"],
        "detected_at": format_timestamp(detected),
        "previous_version_available": False,
    }
    assert (record["plant_id"], record["scope_zone_id"]) == (a.plant_id, a.zone_id)
    assert record["actor_unit"] == "U-04"
    assert output["record_id"] == str(record["record_id"])
    assert output["already_declared"] is False
    assert [row["outcome"] for row in _audit(stack)] == ["success", "success"]
    # El hallazgo y su cierre no se alteran.
    after = world.finding_state(finding)
    assert after is not None
    assert {k: v for k, v in after.items() if k != "updated_at"} == {
        k: v for k, v in closed.items() if k != "updated_at"
    }

    # Repetir con los mismos datos: el mismo registro; con otra fecha: ya declarado.
    code, again, _ = _cli(stack, *arguments, "--yes")
    assert (code, again["already_declared"], again["record_id"]) == (
        admin_cli.ExitCode.OK,
        True,
        str(record["record_id"]),
    )
    other_date = _arguments(stack, lost, detected_at=detected - timedelta(hours=1))
    code, _, error = _cli(stack, *other_date, "--yes")
    assert (code, error["error"]) == (admin_cli.ExitCode.REJECTED, "already_declared")
    assert len(_declarations(stack)) == 1

    # La exportación posterior del mismo alcance se compone y lo declara.
    stack.tick(5)
    generated = _ledger_now(stack)
    first = job(generated)
    package, findings, names = _compose(stack, first, checkpoints)
    (exported,) = [f for f in findings if f["subject_id"] == str(finding.subject_id)]
    assert exported["lifecycle_state"] == "closed"
    assert "closure" in {r["record_type"] for r in exported["records"]}
    (declared,) = [
        r for r in exported["records"] if r["record_type"] == "closure_attachment_unrecoverable"
    ]
    assert declared["content"] == content
    attachments = {item["attachment_id"]: item for item in package.manifest["attachments"]}
    assert attachments[lost["attachment_id"]]["available"] is False
    assert attachments[lost["attachment_id"]]["path"] is None
    assert attachments[lost["attachment_id"]]["unrecoverable"] == {
        "record_id": str(record["record_id"]),
        "declaration_id": content["declaration_id"],
        "sha256": content["sha256"],
        "detected_at": content["detected_at"],
        "reason_es": ATTACHMENT_UNRECOVERABLE_ES,
    }
    assert attachments[kept["attachment_id"]]["available"] is True
    assert f"attachments/{lost['attachment_id']}.png" not in names
    # Mismo estado declarado, mismo paquete.
    again_package, _, _ = _compose(stack, first, checkpoints)
    assert again_package.archive_sha256 == package.archive_sha256
