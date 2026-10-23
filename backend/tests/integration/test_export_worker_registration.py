"""``export_worker`` compuesto por ``accreditation.registration`` con el ``CheckpointService`` de la
raíz (VIG-276, seguimiento de VIG-251; U04-H-08; LC-NUC-14).

Las demás pruebas de la exportación le dan al ``ExportWorker`` un doble del puerto de puntos de
control (``CountingCheckpoints``), así que ninguna ejercía la composición real. Aquí el worker sale
de ``_register_export_worker`` sobre unos ``UnitServices`` cuyo ``checkpoints`` es el
``CheckpointService`` **tal como lo construye** ``shared.runtime.core`` (sin ``writer_context``:
solo admite un contexto de U-02). El primer intento recibe el contexto de U-04 de la entrega; si el
registro le pasara ese servicio compartido, ``write_checkpoints_now`` lanzaría
``CheckpointContextRejected`` y la solicitud no saldría nunca de ``in_progress``.

Lo único que no es de producción son el flujo de bytes y el compositor: en un proceso sin depósito
de evidencias el registro usa ``MissingUploadPort`` y ``MissingPackageComposer``, que esta prueba
sustituye por los dobles sintéticos de ``tests.export_support`` (el compositor real y LocalStack
los cubre ``test_export_contents``). Los puntos de control, el arrendamiento, el cierre y el
expediente son los reales contra PostgreSQL 16.

Solo datos generados. Reloj simulado desde la hora de la base, también el del firmante (retro 14).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from unittest.mock import Mock

import pytest

from tests.export_support import (
    ExportStack,
    RecordingUploads,
    SyntheticComposer,
    export_body,
    export_stack,
)
from tests.integration.conftest import PostgresEndpoint
from tests.signing_support import bootstrapped_world
from vigia_platform.accreditation import registration
from vigia_platform.accreditation.export.worker import EXPORT_WORKER, ExportWorker
from vigia_platform.ledger.adapters.checkpoint_store import SqlCheckpointStore
from vigia_platform.ledger.chain.checkpoints import CheckpointService
from vigia_platform.shared.observability.metrics import get_metrics
from vigia_platform.shared.outbox.registries import ConsumerRegistry
from vigia_platform.shared.runtime.units import UnitServices, free_text_registry, registered_units

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[ExportStack]:
    with export_stack(postgres_endpoint, "export_worker_registration") as built:
        yield built


def _root_services(stack: ExportStack) -> UnitServices:
    """Lo que la raíz entrega a la unidad, con ``checkpoints`` como en ``shared.runtime.core``."""
    assert stack.writer is not None
    authz = stack.authz
    # El firmante nace en la hora de la pila: un solo reloj para la vigencia de sus claves.
    signing = stack.run(bootstrapped_world(start=stack.now())).service
    inert: Any = Mock()
    return UnitServices(
        clock=stack.clock,
        metrics=get_metrics(),
        provider_organization_id=authz.provider_organization_id,
        database=stack.database,
        contexts=authz.contexts,
        authorizer=authz.authorizer,
        audit=authz.sessions.audit,
        outbox=stack.outbox,
        writer=stack.writer,
        free_text=free_text_registry(registered_units()),
        signing=signing,
        checkpoints=CheckpointService(
            store=SqlCheckpointStore(
                database=stack.database,
                writer=stack.writer,
                audit=authz.sessions.audit,
                outbox=stack.outbox,
            ),
            signer=signing,
            clock=stack.clock,
        ),
        kms=inert,
    )


def _registered_worker(stack: ExportStack, monkeypatch: pytest.MonkeyPatch) -> ExportWorker:
    # Sin depósito de evidencias (``evidence=None``), los dos marcadores de la raíz se sustituyen
    # por los dobles sintéticos; el resto del worker es el que compone el registro.
    monkeypatch.setattr(registration, "MissingUploadPort", RecordingUploads)
    monkeypatch.setattr(registration, "MissingPackageComposer", SyntheticComposer)
    registry = ConsumerRegistry()
    registration._register_export_worker(registry, _root_services(stack))
    consumer = registry.get(EXPORT_WORKER)
    assert consumer is not None
    assert isinstance(consumer.handler, ExportWorker)
    return consumer.handler


def test_registered_export_worker_writes_u02_checkpoints_and_ends_ready(
    stack: ExportStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _registered_worker(stack, monkeypatch)
    site = stack.site(plants=1, zones=2)
    plant = next(iter(site.plants))
    response = stack.request_export(
        stack.member(site),
        export_body(plant, list(site.plants[plant]), start=stack.now() - timedelta(days=31)),
    )
    assert response.status_code == 202, response.text
    export_id = uuid.UUID(response.json()["export_id"])

    # El contexto que da el despachador a la entrega: organización del evento, actor de U-04.
    stack.run(worker.run(stack.event_context(site.organization_id), export_id))

    row = stack.export_row(export_id)
    assert (row["state"], row["attempt_count"], row["failure_code"]) == ("ready", 1, None)
    assert stack.package_row(export_id) is not None
    fixed = list(row["checkpoint_record_ids"])
    assert fixed and row["generated_at"] is not None
    # Los puntos de control que fijó el primer intento, escritos por U-02 en la misma organización.
    # El expediente de una organización recién creada no tiene registros (``write_checkpoints_now``
    # salta las cadenas vacías); la auditoría ya tiene al menos ``export_requested``.
    written = stack.fetch(
        "SELECT organization_id, 'audit' AS chain, operation AS kind, actor_unit"
        " FROM shared.audit_entry WHERE entry_id = ANY($1::uuid[])"
        " UNION ALL SELECT organization_id, 'ledger', record_type, actor_unit"
        " FROM ledger.ledger_record WHERE record_id = ANY($1::uuid[])",
        fixed,
    )
    assert len(written) == len(fixed)
    assert "audit" in {r["chain"] for r in written}
    assert {(r["organization_id"], r["kind"], r["actor_unit"]) for r in written} == {
        (site.organization_id, "checkpoint", "U-02")
    }
