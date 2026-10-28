"""VIG-363: ``GET /loop/findings/{finding_state_id}/root-cause-analyses/in-progress``.

El análisis de causa raíz guardado sin terminar se retoma en vez de iniciar otro (hallazgo 21 de
VIG-338). Sobre PostgreSQL 16 real como ``vigia_app`` y la aplicación real (``assistant_stack``,
que monta ``root_cause_stack`` con ``AssistantService``):

- **Lectura**: sin análisis, ``analysis`` y ``assistant_request_id`` nulos; con uno, el cuerpo
  del análisis es **igual** al que devolvió su último paso; de varios sin firmar, el que se
  modificó por última vez; uno ``pending_information`` sigue en curso; uno firmado, no.
- **Asistente**: ``assistant_request_id`` es la última solicitud **de ese análisis**, no la de otro
  análisis del mismo sujeto ni la de otro sujeto.
- **Sujeto**: el análisis de otro hallazgo de la misma zona no aparece.
- **Alcance** (como SUP-U05-16): otra organización, inexistente, otra zona, un rol sin
  ``root_cause.analyze`` y la concesión de proveedor responden lo mismo que un hallazgo
  inexistente (``not_found``) y no escriben nada; la coordinación de la zona y de la planta leen.
- **Sin escritura**: leer no crea análisis, registros ni eventos.

Solo datos generados. Las comparaciones son de códigos, identificadores o igualdad exacta de
cuerpos (VIG-194): ninguna por subcadena.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any, Final

import httpx
import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import IngestedSubject
from tests.loop_assistant_support import AssistantStack, assistant_stack
from tests.loop_classification_support import Who
from tests.loop_root_cause_support import STATEMENTS, RootCauseStack, complete_signature
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.shared.context import Role, ScopeLevel

pytestmark = pytest.mark.integration

IN_PROGRESS: Final = "/loop/findings/{finding_state_id}/root-cause-analyses/in-progress"


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[AssistantStack]:
    with assistant_stack(postgres_endpoint, "loop_root_cause_in_progress") as built:
        yield built


@pytest.fixture
def base(stack: AssistantStack) -> Iterator[RootCauseStack]:
    yield stack.base
    stack.base.world.stack.run(stack.base.world.stack.release())


@pytest.fixture(scope="module")
def coordinator(stack: AssistantStack) -> Who:
    return stack.base.member(stack.base.world.a, Role.COORDINATOR_SST)


def _read(base: RootCauseStack, who: Who, subject: IngestedSubject | uuid.UUID) -> httpx.Response:
    finding = subject if isinstance(subject, uuid.UUID) else base.finding_state_id(subject)
    return base.request(who, "GET", IN_PROGRESS.format(finding_state_id=finding))


def _body(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body: dict[str, Any] = response.json()
    return body


def _comparable(response: httpx.Response) -> tuple[int, dict[str, Any]]:
    body = dict(response.json())
    body.pop("correlation_id", None)
    return response.status_code, body


def _step(base: RootCauseStack, who: Who, analysis_id: str, index: int) -> dict[str, Any]:
    response = base.step(who, analysis_id, index, {"statement": STATEMENTS[index - 1]})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _writes(base: RootCauseStack) -> tuple[Any, ...]:
    world = base.world
    return (base.analysis_counts(world.a), base.analysis_counts(world.b))


# --- lectura -----------------------------------------------------------------------------------


def test_without_an_analysis_there_is_nothing_in_progress(
    base: RootCauseStack, coordinator: Who
) -> None:
    subject = base.received()
    writes = _writes(base)
    assert _body(_read(base, coordinator, subject)) == {
        "analysis": None,
        "assistant_request_id": None,
    }
    assert _writes(base) == writes


def test_the_saved_analysis_is_returned_exactly_as_its_last_step(
    base: RootCauseStack, coordinator: Who
) -> None:
    subject = base.received()
    analysis_id = str(base.started(coordinator, subject)["analysis_id"])
    _step(base, coordinator, analysis_id, 1)
    last = _step(base, coordinator, analysis_id, 2)
    writes = _writes(base)
    body = _body(_read(base, coordinator, subject))
    assert body == {"analysis": last, "assistant_request_id": None}
    assert body["analysis"]["state"] == "draft"
    assert [step["statement"] for step in body["analysis"]["why_steps"]] == list(STATEMENTS[:2])
    assert body["analysis"]["next_step_index"] == 3
    assert _writes(base) == writes


def test_of_several_unsigned_analyses_the_last_modified_one_is_in_progress(
    base: RootCauseStack, coordinator: Who
) -> None:
    subject = base.received()
    older = str(base.started(coordinator, subject)["analysis_id"])
    newer = str(base.started(coordinator, subject)["analysis_id"])
    assert _body(_read(base, coordinator, subject))["analysis"]["analysis_id"] == newer
    # Un paso en el más antiguo lo vuelve el último modificado: es el que se retoma.
    _step(base, coordinator, older, 1)
    assert _body(_read(base, coordinator, subject))["analysis"]["analysis_id"] == older


def test_a_pending_analysis_is_in_progress_and_a_signed_one_is_not(
    base: RootCauseStack, coordinator: Who
) -> None:
    subject = base.received()
    analysis_id = base.with_steps(coordinator, 1, subject)
    pending = base.signature(coordinator, analysis_id, complete_signature(conclusion=None))
    assert pending.status_code == 400, pending.text
    body = _body(_read(base, coordinator, subject))
    assert body["analysis"]["analysis_id"] == analysis_id
    assert body["analysis"]["state"] == "pending_information"
    assert body["analysis"]["pending_information"] == ["conclusion"]
    signed = base.signature(coordinator, analysis_id, complete_signature())
    assert signed.status_code == 201, signed.text
    assert _body(_read(base, coordinator, subject)) == {
        "analysis": None,
        "assistant_request_id": None,
    }


def test_another_subject_of_the_same_zone_is_not_in_progress(
    base: RootCauseStack, coordinator: Who
) -> None:
    subject, other = base.received(), base.received()
    base.with_steps(coordinator, 1, other)
    assert _body(_read(base, coordinator, subject))["analysis"] is None
    own = base.with_steps(coordinator, 1, subject)
    assert _body(_read(base, coordinator, subject))["analysis"]["analysis_id"] == own


# --- asistente ---------------------------------------------------------------------------------


def test_the_last_assistant_request_of_that_analysis_is_returned(
    stack: AssistantStack, base: RootCauseStack, coordinator: Who
) -> None:
    subject = base.received()
    analysis_id = str(base.started(coordinator, subject)["analysis_id"])
    first = stack.requested(coordinator, analysis_id)
    stack.base.tick()
    second = stack.requested(coordinator, analysis_id)
    assert first != second
    body = _body(_read(base, coordinator, subject))
    assert body["analysis"]["analysis_id"] == analysis_id
    assert body["assistant_request_id"] == second
    # La solicitud de otro análisis (otro sujeto) no se confunde con la de este.
    other = base.received()
    stack.requested(coordinator, str(base.started(coordinator, other)["analysis_id"]))
    assert _body(_read(base, coordinator, subject))["assistant_request_id"] == second
    # Un análisis nuevo del mismo sujeto, sin solicitudes, es el que está en curso.
    fresh = str(base.started(coordinator, subject)["analysis_id"])
    body = _body(_read(base, coordinator, subject))
    assert (body["analysis"]["analysis_id"], body["assistant_request_id"]) == (fresh, None)


# --- alcance -----------------------------------------------------------------------------------


def test_out_of_scope_answers_like_missing_and_writes_nothing(
    base: RootCauseStack, coordinator: Who
) -> None:
    world = base.world
    site = world.a.site
    foreign_coordinator = base.member(world.b, Role.COORDINATOR_SST)
    foreign_subject = base.received(world.b)
    base.with_steps(foreign_coordinator, 1, foreign_subject)
    subject = base.received()
    base.with_steps(coordinator, 1, subject)
    writes = _writes(base)
    missing = _comparable(_read(base, coordinator, uuid.uuid4()))
    assert missing[0] == 404
    assert missing[1]["code"] == "not_found"
    outsiders = (
        base.member(world.a, Role.COORDINATOR_SST, ScopeLevel.ZONE, site.other_zone_id),
        base.member(world.a, Role.PLANT_MANAGER),
        base.member(world.a, Role.ADMINISTRATOR),
        base.member(world.a, Role.LINE_MANAGER),
        base.member(world.a, Role.COPASST),
        base.installer(world.a),
    )
    cases = [(coordinator, foreign_subject)] + [(who, subject) for who in outsiders]
    for who, finding in cases:
        assert _comparable(_read(base, who, finding)) == missing
    assert _writes(base) == writes
    zone_coordinator = base.member(world.a, Role.COORDINATOR_SST, ScopeLevel.ZONE, site.zone_id)
    plant_coordinator = base.member(
        world.a, Role.COORDINATOR_SST, ScopeLevel.PLANT, world.a.plant_id
    )
    for who in (zone_coordinator, plant_coordinator):
        assert _body(_read(base, who, subject))["analysis"] is not None


def test_the_service_itself_requires_root_cause_analyze_on_the_zone(
    base: RootCauseStack, coordinator: Who
) -> None:
    """Sin pasar por la ruta (su ``requires`` ya deniega sin la clave): el servicio autoriza
    ``root_cause.analyze`` sobre la zona del sujeto, y si no, ``not_found``."""
    world = base.world
    subject = base.received()
    base.with_steps(coordinator, 1, subject)
    finding = base.finding_state_id(subject)
    for who in (
        base.member(world.a, Role.PLANT_MANAGER),
        base.member(world.a, Role.LINE_MANAGER),
        base.member(world.a, Role.COORDINATOR_SST, ScopeLevel.ZONE, world.a.site.other_zone_id),
        base.member(world.b, Role.COORDINATOR_SST),
    ):
        with pytest.raises(ResourceNotFound):
            base.run(base.root_cause.in_progress(base.context(who), finding))
