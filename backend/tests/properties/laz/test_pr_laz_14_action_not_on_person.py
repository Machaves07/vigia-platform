"""PR-LAZ-14: una acción correctiva nunca recae sobre una persona y su tipo es de la lista cerrada.

TASK-311 (BR-LAZ-37, 38 y 51; F16 de domain-entities §4.2; LC-LAZ-09). Sin base: el camino de
los textos de ``asignar_accion`` es ``actions.action_text`` (política base de U-02 y
``person_directed``) seguido de la política de texto libre del escritor con los **dos
validadores reales** de U-04 sobre la ruta ``/action_description`` de ``action_assigned``
(``both_policy``, la de ``loop.registration``). La ruta y la escritura las prueba
``tests/integration/test_loop_actions_routes.py``.

- **todo texto de acción generado** a partir del vocabulario de atribución (``action_texts``: una
  intervención sobre una persona, o una acción que arrastra una atribución de TASK-340) se rechaza
  con ``action_on_person_forbidden`` o ``person_attribution_detected``;
- **todo texto sin atribución** (``neutral_action_texts``: acciones de ingeniería o de proceso
  sobre la zona) se acepta, también en ``expected_outcome``;
- **metapropiedad**: ningún método del componente admite un tipo de acción fuera de la lista
  cerrada. Cada esquema de U-04 que lleva ``action_type`` (registros y eventos) lo declara con
  exactamente ``engineering`` y ``process``; el único parámetro de tipo de los métodos públicos
  de ``ActionService`` y ``EvidenceService`` es el de ``assign`` (la evidencia no puede cambiarlo,
  BR-LAZ-51); y ``parse_action_type``, por donde pasa, rechaza toda otra cadena con
  ``action_type_not_allowed``.

Se comparan códigos, nunca subcadenas del texto. Solo datos generados.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.attribution_support import both_policy
from tests.properties.laz.strategies.lifecycle import (
    ACTION_TYPES,
    ActionText,
    NeutralAction,
    action_texts,
    neutral_action_texts,
    not_an_action_type,
)
from vigia_platform.accreditation.events import ACCREDITATION_EVENT_TYPES
from vigia_platform.accreditation.record_types import ACCREDITATION_RECORD_TYPES
from vigia_platform.ledger.free_text import FreeTextRejected
from vigia_platform.loop.adapters.http.actions_routes import CorrectiveActionBody
from vigia_platform.loop.application.actions import (
    DESCRIPTION_FIELD,
    EXPECTED_OUTCOME_FIELD,
    ActionRejected,
    ActionService,
    AssignmentRequest,
    action_text,
    parse_action_type,
    person_directed,
)
from vigia_platform.loop.application.evidence import EvidenceService
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.domain.enums import ActionType
from vigia_platform.loop.events import LOOP_EVENT_TYPES
from vigia_platform.loop.record_types import LOOP_RECORD_TYPES

ON_PERSON = LoopDetailCode.ACTION_ON_PERSON_FORBIDDEN.value
ATTRIBUTION = LoopDetailCode.PERSON_ATTRIBUTION_DETECTED.value
REJECTIONS = {ON_PERSON, ATTRIBUTION}


def _verdict(text: str, declared: tuple[str, ...] = (), *, expected: bool = False) -> str | None:
    """``None`` si el texto se acepta; si no, el ``detail_code`` con que lo rechaza el camino de
    ``asignar_accion`` (el servicio y, después, el escritor)."""
    field = EXPECTED_OUTCOME_FIELD if expected else DESCRIPTION_FIELD
    try:
        normalized = action_text(text, field, required=True)
    except ActionRejected as rejected:
        return rejected.detail_code.value
    assert normalized is not None
    try:
        both_policy(declared).apply(normalized, field, organization_id=uuid.uuid4())
    except FreeTextRejected as rejected:
        return rejected.detail_code or rejected.reason
    return None


# --- textos ---------------------------------------------------------------------------------------


@given(action=action_texts())
def test_every_action_on_a_person_or_with_attribution_is_rejected(action: ActionText) -> None:
    verdict = _verdict(action.text)
    assert verdict in REJECTIONS, (action.family, verdict)
    if action.family == "on_person":
        # La intervención sobre una persona la nombra el servicio, antes que ningún validador.
        assert verdict == ON_PERSON
        assert person_directed(action.text)


@given(action=action_texts())
def test_the_same_holds_for_the_expected_outcome(action: ActionText) -> None:
    assert _verdict(action.text, expected=True) in REJECTIONS


@given(action=neutral_action_texts())
def test_every_action_on_the_zone_is_accepted(action: NeutralAction) -> None:
    assert not person_directed(action.text)
    assert _verdict(action.text, action.declared) is None
    assert _verdict(action.text, action.declared, expected=True) is None


@pytest.mark.parametrize(
    "text",
    [
        "Capacitar al operario en el uso del resguardo",
        "Reentrenar a todo el personal de la línea de empaque",
        "Que el personal sea capacitado en el nuevo procedimiento",
        "Evaluación del desempeño de los operarios del turno",
        "Llamado de atención al conductor del montacargas",
        "Sancionar operarios reincidentes en la zona de carga",
        f"{chr(0x0421)}apacitar al {chr(0x043E)}perario de la prensa",  # homoglifos cirílicos
        "CAPACITACIÓN DE LOS MONTACARGUISTAS DE LA ZONA",
        # Revisión automática de VIG-223, ronda 1: formas corrientes que pasaban.
        "Amonestar al responsable del cruce en el pasillo",
        "Sancionar a quien cruzó la zona de carga sin permiso",
        "Evaluar al responsable de la zona por el incidente",
        "Tomar medidas disciplinarias contra el operario del turno",
        "Reentrenar a los peatones que cruzan el pasillo norte",
        "Capacitar a los usuarios del montacargas de la zona",
        "Capacitar a los opera-rios en el uso del resguardo",
        # Unir los cortes no tapa la forma separada.
        "Capacitar-al-operario en el uso del resguardo",
        "Abrir un proceso en contra del conductor del montacargas",
        "Retroalimentar hacia los colaboradores del área de carga",
    ],
)
def test_borders_of_an_action_on_a_person(text: str) -> None:
    assert _verdict(text) == ON_PERSON


@pytest.mark.parametrize(
    "text",
    [
        # Una persona nombrada, pero no como objeto de la intervención.
        "Evaluar con el técnico de mantenimiento la instalación del resguardo",
        "Actualizar las instrucciones de trabajo de la prensa del sector norte",
        "Suspender la operación del montacargas durante la noche en la zona",
        "Formar una barrera física con vallas en el pasillo de carga",
        "Señalizar el pasillo para peatones y conductores de la planta",
        "Evaluar el riesgo de la operación de la zona norte con el comité",
        "Instalar barandas contra caídas en la plataforma de carga",
        "Evaluar las guardas de la zona con el responsable de mantenimiento",
        "Reemplazar el sensor según el procedimiento PRO-001 de la línea",
    ],
)
def test_a_person_mentioned_without_being_the_target_is_accepted(text: str) -> None:
    assert not person_directed(text)
    assert _verdict(text) is None


# --- metapropiedad del tipo ----------------------------------------------------------------------


def _action_type_schemas(schema: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    """Cada subesquema de una propiedad ``action_type``, siguiendo ``$defs`` y ``$ref``."""
    definitions = schema.get("$defs", {})

    def walk(node: Any) -> Iterator[Mapping[str, Any]]:
        if isinstance(node, Mapping):
            reference = node.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/$defs/"):
                yield from walk(definitions[reference.removeprefix("#/$defs/")])
            for name, value in node.get("properties", {}).items():
                if name == "action_type":
                    yield value
            for key, value in node.items():
                if key not in ("$defs", "properties"):
                    yield from walk(value)
            for value in node.get("properties", {}).values():
                yield from walk(value)
        elif isinstance(node, list):
            for item in node:
                yield from walk(item)

    yield from walk(schema)


def _enum(schema: Mapping[str, Any], definitions: Mapping[str, Any]) -> frozenset[str]:
    reference = schema.get("$ref")
    if isinstance(reference, str):
        return _enum(definitions[reference.removeprefix("#/$defs/")], definitions)
    return frozenset(schema["enum"])


def test_every_u04_schema_with_an_action_type_declares_exactly_the_closed_list() -> None:
    assert frozenset(ActionType) == frozenset(ACTION_TYPES)
    models: list[type[Any]] = [
        *(record.content_model for record in (*LOOP_RECORD_TYPES, *ACCREDITATION_RECORD_TYPES)),
        *(event.payload_model for event in (*LOOP_EVENT_TYPES, *ACCREDITATION_EVENT_TYPES)),
    ]
    found = 0
    for model in models:
        schema = model.model_json_schema()
        for property_schema in _action_type_schemas(schema):
            assert _enum(property_schema, schema.get("$defs", {})) == frozenset(ACTION_TYPES), (
                model.__name__
            )
            found += 1
    # action_assigned, closure y los eventos action_assigned y finding_closed como mínimo.
    assert found >= 4


def _public_methods(cls: type[Any]) -> dict[str, inspect.Signature]:
    return {
        name: inspect.signature(member)
        for name, member in inspect.getmembers(cls, inspect.isfunction)
        if not name.startswith("_")
    }


def test_only_assign_takes_an_action_type_and_evidence_cannot_change_it() -> None:
    carriers = {
        (cls.__name__, name)
        for cls in (ActionService, EvidenceService)
        for name, signature in _public_methods(cls).items()
        if "action_type" in signature.parameters
        or any(
            parameter.annotation in ("AssignmentRequest", AssignmentRequest)
            for parameter in signature.parameters.values()
        )
    }
    assert carriers == {("ActionService", "assign"), ("ActionService", "assign_in_transaction")}
    assert set(_public_methods(EvidenceService)) == {"add"}
    assert "action_type" not in inspect.signature(EvidenceService.add).parameters
    assert "action_type" in AssignmentRequest.__dataclass_fields__


@given(value=not_an_action_type())
def test_any_other_string_is_action_type_not_allowed(value: str) -> None:
    with pytest.raises(ActionRejected) as raised:
        parse_action_type(value)
    assert raised.value.detail_code is LoopDetailCode.ACTION_TYPE_NOT_ALLOWED
    # El cuerpo HTTP no lo descarta antes: el servicio da el código de negocio, no un 400 mudo.
    body = CorrectiveActionBody.model_validate(
        {
            "action_type": value[:64],
            "action_description": "Instalar un resguardo fijo en el acceso a la zona",
            "assignee_user_id": str(uuid.uuid4()),
        }
    )
    with pytest.raises(ActionRejected):
        parse_action_type(body.action_type)


@given(value=st.one_of(st.integers(), st.none(), st.booleans(), st.lists(st.text(max_size=3))))
def test_non_strings_are_action_type_not_allowed(value: object) -> None:
    with pytest.raises(ActionRejected) as raised:
        parse_action_type(value)
    assert raised.value.detail_code is LoopDetailCode.ACTION_TYPE_NOT_ALLOWED


@pytest.mark.parametrize("value", ACTION_TYPES)
def test_the_two_members_are_accepted(value: str) -> None:
    assert parse_action_type(value) is ActionType(value)
