"""PR-LAZ-44: la reconstrucción produce exactamente el estado incremental (TASK-306; LC-LAZ-03;
PAT-LAZ-RES-03; domain-entities.md de U-04 §9; BR-LAZ-146 a 148 y 156).

Contra PostgreSQL 16 real como ``vigia_app``: un hallazgo recibido por el consumidor real
``loop_projection`` (con sus campos enriquecidos, TASK-341), clasificado, con acción asignada y
cerrado por el escritor real. Con ``ProjectionRebuilder.rebuild_plant`` sobre los mismos puertos:

- la fila de ``loop.episode`` y la de ``loop.finding_state`` que quedan en servicio tras
  reconstruir son **idénticas**, campo a campo, a las que produjo la vía incremental (mismo
  ``projection_digest``);
- mutando un campo cualquiera de la fila en servicio, ``verify_loop_projection`` la detecta
  (``state = diverged``) y dispara ``rebuild_loop_projection``, que la corrige exactamente a su
  valor anterior.

Solo datos generados.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any, Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import LoopWorld, loop_world
from vigia_platform.ledger.application.evidence_read import EvidenceService
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.projection.fold import FindingStateRow
from vigia_platform.loop.projection.rebuild import ProjectionRebuilder
from vigia_platform.loop.projection.verify import ProjectionVerifier
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration

EXAMPLES: Final = 6
"""Cada ejemplo ingresa un hallazgo, lo clasifica, asigna y cierra, y reconstruye la planta."""

_MUTABLE_BOOLEAN_FIELDS: Final = ("evidence_flagged", "assignee_inactive", "single_occupancy_zone")


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[LoopWorld]:
    with loop_world(postgres_endpoint, "pr_laz_44") as built:
        yield built


def _build_rebuilder(world: LoopWorld, ports: Any) -> ProjectionRebuilder:
    database = world.stack.primary.database
    sessions = world.stack.authz.sessions
    return ProjectionRebuilder(
        database=database,
        catalog=ports,
        gates=ports,
        coverage=ports,
        markers=EvidenceService(
            database=database, audit=sessions.audit, storage=world.stack.storage
        ),
        settings=PostgresLoopConfigurationRepository(database),
        clock=sessions.clock,
    )


def _build_verifier(world: LoopWorld, rebuilder: ProjectionRebuilder) -> ProjectionVerifier:
    sessions = world.stack.authz.sessions
    return ProjectionVerifier(
        database=world.stack.primary.database,
        rebuilder=rebuilder,
        outbox=world.stack.outbox,
        clock=sessions.clock,
    )


def _assign_and_close(world: LoopWorld, subject: Any) -> None:
    stack = world.stack
    now = stack.now()
    action_id = str(stack.uuid7())
    stack.tick()
    _receipt, step = world.write(
        subject.organization,
        "action_assigned",
        {
            "action_id": action_id,
            "subject_kind": subject.subject_kind,
            "subject_id": str(subject.subject_id),
            "anchor_record_id": str(subject.record_id),
            "action_type": "engineering",
            "action_description": "Instalar un resguardo fijo en el acceso a la zona de carga",
            "assignee_user_id": str(stack.authz.operator_id),
            "assignee_display_name": "Responsable sintético",
            "due_at": format_timestamp(now + timedelta(days=30)),
            "deadline_days": 30,
            "assigned_by": {
                "user_id": str(stack.authz.operator_id),
                "display_name": "Coordinación SST sintética",
                "role_in_use": "coordinator_sst",
            },
            "assigned_at": format_timestamp(now),
        },
    )
    assert step is not None and step.applied
    stack.tick()
    _receipt, step = world.write(
        subject.organization,
        "closure",
        {
            "closure_id": str(stack.uuid7()),
            "subject_kind": subject.subject_kind,
            "subject_id": str(subject.subject_id),
            "anchor_record_id": str(subject.record_id),
            "action_id": action_id,
            "action_type": "engineering",
            "closure_description": "Resguardo instalado y verificado en el acceso de la zona",
            "evidence_entry_ids": [str(stack.uuid7())],
            "attachment_digests": [],
            "referenced_clip_ids": [],
            "signer": {
                "user_id": str(stack.authz.operator_id),
                "display_name": "Coordinación SST sintética",
                "professional_license": "LIC-SST-0001",
            },
            "signed_at": format_timestamp(stack.now()),
        },
    )
    assert step is not None and step.applied


def _lifecycle(world: LoopWorld) -> tuple[Any, dict[str, Any], dict[str, Any], Any, Any]:
    """Un hallazgo recibido, enriquecido, clasificado, asignado y cerrado."""
    organization = world.organization()
    subject = world.ingest_finding(organization)
    consumer, ports = world.projection_consumer()
    event = world.outbox_event(subject)
    written = world.deliver(consumer, event)
    assert written == 1
    world.stack.tick()
    content = world.classification_content(subject)
    _receipt, step = world.write(organization, "classification", content)
    assert step is not None and step.applied
    _assign_and_close(world, subject)
    finding = world.finding_state(subject)
    episode = world.episode(subject)
    assert finding is not None and episode is not None
    return subject, finding, episode, ports, organization


def _without(row: dict[str, Any], *keys: str) -> dict[str, Any]:
    """La fila sin la contabilidad que digest.py excluye a propósito (``last_event_id``…):
    depende de por dónde llegó el hecho (evento o reconstrucción), no de qué hecho fue."""
    return {key: value for key, value in row.items() if key not in keys}


def test_pr_laz_44_rebuild_matches_incremental_state(world: LoopWorld) -> None:
    @settings(
        max_examples=EXAMPLES,
        deadline=None,
        database=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(st.sampled_from(_MUTABLE_BOOLEAN_FIELDS))
    def run(mutated_field: str) -> None:
        subject, finding, episode, ports, organization = _lifecycle(world)
        context = world.context(organization)
        rebuilder = _build_rebuilder(world, ports)

        world.stack.run(rebuilder.rebuild_plant(context, organization.plant_id))
        rebuilt_finding = world.finding_state(subject)
        rebuilt_episode = world.episode(subject)
        assert rebuilt_finding is not None and rebuilt_episode is not None
        # `projection_digest` ya resume los campos de estado (digest.py): si coincide, el estado
        # coincide. `last_event_id` queda fuera a propósito (evento o reconstrucción, no el hecho).
        assert rebuilt_finding["projection_digest"] == finding["projection_digest"]
        assert _without(rebuilt_finding, "last_event_id") == _without(finding, "last_event_id")
        assert _without(rebuilt_episode, "last_event_id") == _without(episode, "last_event_id")

        # Mutación directa, como un defecto de la caché (no una operación de U-04): un campo
        # booleano se invierte y su `projection_digest` se recalcula sobre el valor ya mutado,
        # igual que haría un defecto de la vía incremental (fila consistente consigo misma, pero
        # divergente del expediente). `mutated_field` sale de `_MUTABLE_BOOLEAN_FIELDS`, una lista
        # cerrada de la prueba, nunca de una petición (noqa: S608, prueba que muta un campo a
        # propósito).
        world.stack.execute(
            f"UPDATE loop.finding_state SET {mutated_field} = NOT {mutated_field}"  # noqa: S608
            " WHERE organization_id = $1 AND finding_state_id = $2",
            organization.organization_id,
            finding["finding_state_id"],
        )
        mutated_row = world.finding_state(subject)
        assert mutated_row is not None and mutated_row[mutated_field] != finding[mutated_field]
        mutated_digest = FindingStateRow(**mutated_row).computed_digest()
        world.stack.execute(
            "UPDATE loop.finding_state SET projection_digest = $1"
            " WHERE organization_id = $2 AND finding_state_id = $3",
            mutated_digest,
            organization.organization_id,
            finding["finding_state_id"],
        )
        mutated = world.finding_state(subject)
        assert mutated is not None and mutated[mutated_field] != finding[mutated_field]

        verifier = _build_verifier(world, _build_rebuilder(world, ports))
        outcome = world.stack.run(verifier.verify_plant(context, organization.plant_id))
        assert outcome.is_diverged
        assert outcome.rebuild_triggered

        corrected = world.finding_state(subject)
        assert corrected is not None
        assert corrected["projection_digest"] == finding["projection_digest"]
        assert _without(corrected, "last_event_id") == _without(finding, "last_event_id")

    run()
