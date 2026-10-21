"""DR-LAZ-04 sin versión anterior de punta a punta (VIG-274, A-7x; runbook 7.3, paso 4).

Sobre PostgreSQL 16 real (``actions_stack``: hallazgo ingresado por el contrato, acción, entrada
de evidencia con dos adjuntos y cierre firmados por las rutas reales) y la orden real
``vigia-admin declare-unrecoverable-attachment`` con la raíz de producción
(``compose_admin_runtime``), con ``tests/unrecoverable_support.py``:

- **La declaración en el hallazgo**: un registro ``closure_attachment_unrecoverable`` en la cadena
  de la planta con el hash y la fecha del adjunto, escrito con el contexto de U-04, y la intención y
  el resultado auditados en la proveedora. El hallazgo sigue ``closed`` y su fila de la proyección
  no cambia (P4, P5).
- **Guardas** (fallo cerrado; nada escrito): un adjunto de otra organización (con su planta o con
  la de la primera), ``--dry-run``, sin confirmación, otro hash, otra planta, un adjunto que no
  existe y una fecha futura. Repetir la orden con los mismos datos es un duplicado aceptado (un
  solo registro); con otra fecha, ``already_declared``.
- **La exportación posterior** (``ExportComposer`` sobre el expediente real con
  ``LectorExpediente`` y ``StorageBlobs``): antes de declarar, falla con el error del objeto
  ausente que ve S3 (``NoSuchKey``); después, se compone sin pedir los bytes perdidos, el
  manifiesto declara el adjunto con el **mismo** campo estructurado que el registro del hallazgo,
  el hallazgo va ``closed`` con su cierre y dos composiciones del mismo estado dan el mismo
  ``archive_sha256``.

Solo datos generados. Reloj simulado desde la hora de la base (retro 14); topes de 60 s.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest
from botocore.exceptions import ClientError

from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import register_u04_and_catalog
from tests.loop_actions_support import ActionsStack, Who, actions_stack
from tests.unrecoverable_support import (
    arguments,
    audit,
    checkpoint_service,
    cli,
    compose,
    declarations,
    declare_and_export,
    export_job,
    ledger_now,
    prepare,
)
from vigia_platform.accreditation.manifest.model import ATTACHMENT_UNRECOVERABLE_ES
from vigia_platform.identity.application import admin_cli
from vigia_platform.loop.application.unrecoverable import declaration_id
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[ActionsStack]:
    with actions_stack(
        postgres_endpoint, "laz_unrecoverable", register=register_u04_and_catalog
    ) as built:
        yield built


def test_the_declaration_is_written_once_and_the_next_export_declares_it(
    stack: ActionsStack,
) -> None:
    world = stack.world
    a, b = world.a, world.b
    lost = prepare(stack)
    finding, claim, kept, detected = lost.finding, lost.lost, lost.kept, lost.detected
    own = arguments(stack, claim, detected_at=detected)

    # Un adjunto de la otra organización, con su planta o con la de la primera: no existe.
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
        code, _, error = cli(
            stack,
            *arguments(stack, foreign_claim, detected_at=detected, **{"--plant": str(plant)}),
            "--yes",
        )
        assert (code, error["error"]) == (admin_cli.ExitCode.REJECTED, "not_found")
    assert cli(stack, *own, "--dry-run")[0] == admin_cli.ExitCode.OK
    code, _, error = cli(stack, *own)  # sin --yes ni confirmación
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
        code, _, error = cli(
            stack, *arguments(stack, claim, detected_at=detected, **changes), "--yes"
        )
        assert (code, error["error"]) == (admin_cli.ExitCode.REJECTED, expected), changes
    assert declarations(stack) == [] and audit(stack) == []

    # Antes de declarar, la exportación del alcance falla con el objeto ausente: nunca silencio.
    checkpoints = checkpoint_service(world)
    with pytest.raises(ClientError) as missing:
        compose(stack, export_job(world, checkpoints, lost.start, ledger_now(stack)), checkpoints)
    assert missing.value.response["Error"]["Code"] == "NoSuchKey"

    # La orden real y la exportación posterior del mismo alcance.
    declared = declare_and_export(stack, lost, checkpoints)
    (entry,) = stack.entry_rows(lost.action_id)
    assert declared.content == {
        "declaration_id": str(declaration_id(uuid.UUID(claim["attachment_id"]))),
        "subject_kind": "finding",
        "subject_id": str(finding.subject_id),
        "action_id": str(lost.action_id),
        "evidence_entry_id": str(entry["evidence_entry_id"]),
        "evidence_record_id": str(entry["record_id"]),
        "attachment_id": claim["attachment_id"],
        "sha256": claim["sha256"],
        "size_bytes": claim["size_bytes"],
        "content_type": claim["content_type"],
        "detected_at": format_timestamp(detected),
        "previous_version_available": False,
    }
    (record,) = declarations(stack)
    assert (record["plant_id"], record["scope_zone_id"]) == (a.plant_id, a.zone_id)
    assert record["actor_unit"] == "U-04"
    assert [row["outcome"] for row in audit(stack)] == ["success", "success"]
    # El hallazgo y su cierre no se alteran.
    after = world.finding_state(finding)
    assert after is not None
    assert {k: v for k, v in after.items() if k != "updated_at"} == {
        k: v for k, v in lost.closed.items() if k != "updated_at"
    }

    # El paquete: mismo campo estructurado en el hallazgo y en el manifiesto.
    assert declared.exported["lifecycle_state"] == "closed"
    assert "closure" in {r["record_type"] for r in declared.exported["records"]}
    (in_finding,) = [
        r
        for r in declared.exported["records"]
        if r["record_type"] == "closure_attachment_unrecoverable"
    ]
    assert in_finding["content"] == declared.content
    assert declared.entry["available"] is False and declared.entry["path"] is None
    assert declared.entry["unrecoverable"] == {
        "record_id": declared.record_id,
        "declaration_id": declared.content["declaration_id"],
        "sha256": declared.content["sha256"],
        "detected_at": declared.content["detected_at"],
        "reason_es": ATTACHMENT_UNRECOVERABLE_ES,
    }
    manifest = {item["attachment_id"]: item for item in declared.package.manifest["attachments"]}
    assert manifest[kept["attachment_id"]]["available"] is True
    assert f"attachments/{claim['attachment_id']}.png" not in declared.names

    # Repetir con los mismos datos: el mismo registro; con otra fecha: ya declarado.
    code, again, _ = cli(stack, *own, "--yes")
    assert (code, again["already_declared"], again["record_id"]) == (
        admin_cli.ExitCode.OK,
        True,
        declared.record_id,
    )
    other_date = arguments(stack, claim, detected_at=detected - timedelta(hours=1))
    code, _, error = cli(stack, *other_date, "--yes")
    assert (code, error["error"]) == (admin_cli.ExitCode.REJECTED, "already_declared")
    assert len(declarations(stack)) == 1

    # Mismo estado declarado, mismo paquete.
    stack.tick(5)
    job = export_job(world, checkpoints, lost.start, ledger_now(stack))
    first, _, _ = compose(stack, job, checkpoints)
    second, _, _ = compose(stack, job, checkpoints)
    assert first.archive_sha256 == second.archive_sha256
