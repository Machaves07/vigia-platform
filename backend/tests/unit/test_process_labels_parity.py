"""Paridad de las etiquetas de los valores de proceso con el contrato v1.6.0 (TASK-593; A-129).

La consola sigue en ``@vigia/contracts`` v1.1.0: las listas cerradas nuevas de v1.6.0 que muestra
(``process_variable_kind``, ``process_unit`` y ``process_state_pair``, con el lado ``.asserted`` y
``.deasserted`` de cada par) llegan por ``backend/resources/labels.platform.es.json``. Esta prueba
falla si falta, sobra o difiere una sola etiqueta frente a ``labels.es.json`` del paquete fijado, y
comprueba que el cargador de la plataforma las admite (fallo cerrado en lo demás).
"""

from __future__ import annotations

import json
from typing import Final

import pytest
from vigia_contracts.labels import labels_es
from vigia_contracts.models.enumerations import ProcessStatePair, ProcessUnit, ProcessVariableKind

from vigia_platform.shared.api.labels import DEFAULT_LABELS_PATH, LabelsInvalid, PlatformLabels

PROCESS_LISTS: Final = ("process_variable_kind", "process_unit", "process_state_pair")


def _platform_file() -> dict[str, dict[str, str]]:
    document: dict[str, dict[str, str]] = json.loads(
        DEFAULT_LABELS_PATH.read_text(encoding="utf-8")
    )
    return document


@pytest.mark.parametrize("name", PROCESS_LISTS)
def test_each_process_list_is_exactly_the_one_of_the_pinned_contract(name: str) -> None:
    assert _platform_file()[name] == dict(labels_es()[name])


def test_every_member_of_the_contract_enumerations_has_its_labels() -> None:
    labels = PlatformLabels.load()
    assert labels.missing("process_variable_kind", ProcessVariableKind) == []
    assert labels.missing("process_unit", ProcessUnit) == []
    assert labels.missing("process_state_pair", ProcessStatePair) == []
    for pair in ProcessStatePair:
        assert labels.has("process_state_pair", f"{pair.value}.asserted")
        assert labels.has("process_state_pair", f"{pair.value}.deasserted")
    assert labels.label("process_state_pair", "on_off.asserted") == "encendido"


@pytest.mark.parametrize(
    "value",
    ["on_off.other", "on_off.asserted.asserted", "On_off.asserted", ".asserted", "on_off."],
)
def test_only_the_side_of_a_pair_may_follow_the_value(value: str) -> None:
    with pytest.raises(LabelsInvalid):
        PlatformLabels({"process_state_pair": {value: "texto"}})


def test_the_side_of_a_pair_is_accepted_by_the_loader() -> None:
    labels = PlatformLabels({"process_state_pair": {"yes_no.deasserted": "no"}})
    assert labels.label("process_state_pair", "yes_no.deasserted") == "no"
