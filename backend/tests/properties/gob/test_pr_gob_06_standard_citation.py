"""PR-GOB-06 (parte de la ingesta): todo hallazgo aceptado cita un estándar vigente en su intervalo.

BR-CTR-08, BR-GOB-90 y BL §2.4 (TASK-221). La historia del catálogo de la zona sale de
``catalog_versions`` (TASK-208): una primera publicación y sus cambios, compuestos por el dominio
real (``plan_publication``) y publicados en instantes que avanzan 0 ms, 1 ms, 1 s o 1 h; cada
versión
rige en ``[publicada, siguiente)``. Un hallazgo o una detección del kit de U-01 se genera a partir
de
**una** de esas versiones y se coloca alrededor de su intervalo, con el reloj de ``clock_offsets``.

Propiedades, por la aplicación real y el ``IngestService`` real (dobles de ``ingest_support``):

- **invariante**: si se acepta, alguna versión vigente en algún instante de la ventana del hecho
  (``[started_at - tol, ended_at + tol]``, o el instante de recepción sin reloj sincronizado) cita
  su
  ``{standard_id, version}`` (oráculo escrito aquí, sobre la historia publicada);
- **no vacía**: si la versión de la que sale el registro es vigente en esa ventana, se acepta (el
  kit
  lo genera coherente con ella); si ninguna versión vigente en la ventana cita el estándar, es
  ``schema_invalid`` con ``field = standard`` y nada se escribe.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import uuid
from typing import Any

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from vigia_contracts.conformance.generators import detection_for_review, finding
from vigia_contracts.models.api import (
    ContractValidationError,
    parse_finding_submission,
    parse_rejection_response,
)
from vigia_contracts.models.api import (
    parse_detection_for_review_submission as parse_detection_submission,
)

from tests.ingest_support import ingest_world, place
from tests.properties.gob.strategies.catalog import CatalogScenario, catalog_versions, resolve
from tests.properties.gob.strategies.clock import (
    LIMIT_MS,
    ClockDeclaration,
    clock_offsets,
    fact_offsets,
)
from vigia_platform.catalog.domain.catalog_version import (
    CatalogRuleViolated,
    CatalogState,
    PublicationPlan,
    plan_publication,
)
from vigia_platform.catalog.domain.standard import DeclaredBy
from vigia_platform.fleet.domain.ingest_order import AssignmentSpan, IngestKind

YEAR = dt.timedelta(days=365)
DURATION = dt.timedelta(seconds=30)
STEPS = (
    dt.timedelta(0),
    dt.timedelta(milliseconds=1),
    dt.timedelta(seconds=1),
    dt.timedelta(hours=1),
)
DECLARED_BY = DeclaredBy(uuid.UUID(int=7, version=4), "Coordinación SST sintética", "administrator")
REASON = "Cambio sintético del catálogo de la zona"


def _publish(
    scenario: CatalogScenario, start: dt.datetime, steps: list[dt.timedelta]
) -> list[tuple[dt.datetime, PublicationPlan]]:
    """Las versiones emitidas por la secuencia, cada una con su instante (las rechazadas no)."""
    published: list[tuple[dt.datetime, PublicationPlan]] = []
    state: CatalogState | None = None
    moment = start
    for index, intent in enumerate([None, *scenario.intents]):
        change = scenario.first if intent is None else resolve(intent, state)  # type: ignore[arg-type]
        try:
            plan = plan_publication(
                state,
                change,
                zone=scenario.zone,
                issued_at=moment,
                declared_by=DECLARED_BY,
                reason_es=REASON,
                new_standard_id=uuid.UUID(int=index + 1, version=4),
            )
        except CatalogRuleViolated:
            continue
        published.append((moment, plan))
        state = CatalogState(
            catalog=plan.catalog,
            single_occupancy=plan.single_occupancy,
            aggregation_window_minutes=plan.aggregation_window_minutes,
        )
        moment += steps[index % len(steps)]
    return published


def _window(
    started: dt.datetime, clock: ClockDeclaration, received_at: dt.datetime
) -> tuple[dt.datetime, dt.datetime]:
    if not clock.synchronized:
        return received_at, received_at
    tolerance = dt.timedelta(milliseconds=min(abs(clock.offset_ms), LIMIT_MS))
    return started - tolerance, started + DURATION + tolerance


def _in_force(
    start: dt.datetime,
    end: dt.datetime,
    version_from: dt.datetime,
    version_until: dt.datetime | None,
) -> bool:
    """¿Hay un instante ``x`` con ``start ≤ x ≤ end`` y ``version_from ≤ x < version_until``?"""
    earliest = max(start, version_from)
    return earliest <= end and (version_until is None or earliest < version_until)


@settings(
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
        HealthCheck.filter_too_much,
    ]
)
@given(
    data=st.data(),
    scenario=catalog_versions(max_changes=4),
    steps=st.lists(st.sampled_from(STEPS), min_size=1, max_size=4),
    kind=st.sampled_from([IngestKind.FINDING, IngestKind.DETECTION_FOR_REVIEW]),
    clock=clock_offsets(),
    offset=fact_offsets(),
)
def test_pr_gob_06_every_accepted_record_cites_a_standard_in_force_in_its_interval(
    data: st.DataObject,
    scenario: CatalogScenario,
    steps: list[dt.timedelta],
    kind: IngestKind,
    clock: ClockDeclaration,
    offset: dt.timedelta,
) -> None:
    world = ingest_world()
    world.store.assignment_rows = [
        (world.a.node_id, AssignmentSpan(world.zone, world.now - 2 * YEAR, None))
    ]
    world.set_usage(True, world.now - 2 * YEAR)
    zone = dataclasses.replace(
        scenario.zone,
        organization_id=world.a.organization_id,
        plant_id=world.a.plant_id,
        zone_id=world.zone,
    )
    published = _publish(
        dataclasses.replace(scenario, zone=zone), world.now - 7 * dt.timedelta(days=1), steps
    )
    assume(published)
    history: list[tuple[dt.datetime, dt.datetime | None, dict[str, Any]]] = []
    for index, (moment, plan) in enumerate(published):
        until = published[index + 1][0] if index + 1 < len(published) else None
        world.publish_catalog(plan.catalog, moment, until, version=plan.catalog_version)
        history.append((moment, until, plan.catalog))
    source = data.draw(st.integers(0, len(history) - 1), label="source")
    version_from, version_until, catalog = history[source]
    node_id = str(world.a.node_id)
    strategy = (
        finding(catalog, node_id=node_id)
        if kind is IngestKind.FINDING
        else detection_for_review(catalog, node_id=node_id)
    )
    anchor = data.draw(st.sampled_from([version_from, version_until or world.now]), label="anchor")
    started = anchor - offset
    document = place(
        data.draw(strategy, label="document"),
        started,
        duration=DURATION,
        synchronized=clock.synchronized,
        offset_ms=clock.offset_ms,
    )
    # El kit genera siempre ``tier_1``: con un catálogo sin señales (los cambios de
    # ``catalog_versions`` lo permiten) la presentación no es válida para el contrato (paso 4).
    parser = parse_finding_submission if kind is IngestKind.FINDING else parse_detection_submission
    try:
        parser(json.dumps(document).encode())
    except ContractValidationError:
        assume(False)
    received_at = world.now
    response = world.post(kind, document)

    start, end = _window(started, clock, received_at)
    standard = document["standard"]
    cited = (uuid.UUID(standard["standard_id"]), standard["version"])
    citing_in_force = [
        number
        for number, (version_start, version_end, payload) in enumerate(history)
        if _in_force(start, end, version_start, version_end)
        and cited
        in {(uuid.UUID(item["standard_id"]), item["version"]) for item in payload["standards"]}
    ]
    if response.status_code == 200:
        assert citing_in_force, (start, end, history)
        return
    rejection = parse_rejection_response(response.content)
    assert world.accepted(kind) == []
    if not citing_in_force:
        assert (rejection.code.value, rejection.field) == ("schema_invalid", "standard")
    # La versión de la que sale el registro, vigente en la ventana: se acepta.
    assert not _in_force(start, end, version_from, version_until), response.text
