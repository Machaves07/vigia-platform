"""N-11 · Registro malformado colado por otra unidad (business-rules §14).

**Qué intenta**: que otra unidad (o un nodo a través de ella) escriba en el expediente un tipo
desconocido, un tipo que no es suyo, o contenido fuera del esquema: campos de más, tipos
cambiados, tamaños enormes, valores que rompen la serialización; o que un tipo nuevo declare un
campo que identifique a una persona.

**Qué lo detiene** (BR-NUC-44, BR-NUC-51):

- BR-NUC-44: el tipo debe estar en el registro cerrado y ser escribible por la unidad que llama;
  el contenido se valida contra el esquema estricto (sin propiedades adicionales, longitudes,
  rangos, ≤ 256 KB) **antes** de calcular el hash; ``record_type_unknown`` o ``content_invalid``
  con la ruta del primer campo que falla, y nada se escribe;
- BR-NUC-51: la metapropiedad recorre los esquemas al registrar: un campo prohibido (nombre,
  documento, ``track_id``, rostro) o un texto libre no declarado impide arrancar; después del
  arranque el registro está sellado.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

import pytest
from pydantic import Field, StrictStr, create_model

from tests.platform_support import T0, Platform
from vigia_platform.ledger.application.writer import LedgerRejection, LedgerRejectionCode
from vigia_platform.ledger.record_types.u02 import U02_RECORD_TYPES
from vigia_platform.ledger.registry import (
    ChainLevel,
    ContentModel,
    RecordType,
    RecordTypeRegistry,
    RecordTypeRejected,
)
from vigia_platform.shared.context import ActorUnit

pytestmark = pytest.mark.integration

FreeText = Annotated[StrictStr, Field(min_length=1, max_length=200)]


def _gate(plant: uuid.UUID, zone: uuid.UUID, /, **changes: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "zone_id": str(zone),
        "plant_id": str(plant),
        "gate": "use",
        "status": "approved",
        "resulting_mode": "productive",
    }
    document.update(changes)
    return document


def _nested(depth: int) -> Any:
    value: Any = "x"
    for _ in range(depth):
        value = {"a": value}
    return value


def test_n11_unknown_or_foreign_types_are_refused_and_nothing_is_written(
    platform: Platform,
) -> None:
    site = platform.site()
    ((plant_id, zone_id),) = site.zones()
    before = len(platform.records(site.organization_id))
    unknown = platform.write(site.organization_id, "finding_deleted", _gate(plant_id, zone_id))
    assert isinstance(unknown, LedgerRejection)
    assert unknown.code is LedgerRejectionCode.RECORD_TYPE_UNKNOWN
    # ``gate_state_changed`` es de U-03: U-04 no lo escribe.
    foreign = platform.write(
        site.organization_id, "gate_state_changed", _gate(plant_id, zone_id), unit=ActorUnit.U04
    )
    assert isinstance(foreign, LedgerRejection)
    assert foreign.code is LedgerRejectionCode.RECORD_TYPE_UNKNOWN
    assert len(platform.records(site.organization_id)) == before


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"approved_by_name": "Persona sintética"}, "/approved_by_name"),
        ({"resulting_mode": "deleted"}, "/resulting_mode"),
        ({"status": 1}, "/status"),
        ({"gate": None}, "/gate"),
        ({"zone_id": "no-es-un-uuid"}, "/zone_id"),
        ({"extra": "x" * (300 * 1024)}, None),
        ({"extra": _nested(5_000)}, None),
        ({"extra": 10**400}, None),
        ({"extra": float("nan")}, None),
    ],
    ids=[
        "campo-de-mas",
        "valor-fuera-de-lista",
        "tipo-cambiado",
        "nulo",
        "uuid-malformado",
        "300-kb",
        "anidamiento",
        "entero-enorme",
        "nan",
    ],
)
def test_n11_content_outside_the_schema_is_content_invalid_before_any_hash(
    platform: Platform, changes: dict[str, Any], field: str | None
) -> None:
    site = platform.site()
    ((plant_id, zone_id),) = site.zones()
    before = len(platform.records(site.organization_id))
    rejection = platform.write(
        site.organization_id, "gate_state_changed", _gate(plant_id, zone_id, **changes), T0
    )
    assert isinstance(rejection, LedgerRejection), rejection
    assert rejection.code is LedgerRejectionCode.CONTENT_INVALID
    if field is not None:
        assert rejection.field == field
    # El rechazo nunca repite el contenido recibido.
    assert "Persona sintética" not in repr(rejection)
    assert len(platform.records(site.organization_id)) == before
    assert not platform.fetch(
        "SELECT 1 FROM ledger.chain_head WHERE organization_id = $1", site.organization_id
    )


@pytest.mark.parametrize(
    "name", ["employee_name", "worker_document", "track_id", "face_embedding", "full_name"]
)
def test_n11_a_type_that_names_a_person_never_registers(name: str) -> None:
    model = create_model(  # type: ignore[call-overload]
        "Probe", __base__=ContentModel, zone_id=(uuid.UUID, ...), **{name: (FreeText, ...)}
    )
    definition = RecordType(
        record_type="observation_with_person",
        writer_unit=ActorUnit.U04,
        chain_level=ChainLevel.PLANT,
        schema_version=1,
        content_model=model,
        free_text_paths=(f"/{name}",),
    )
    with pytest.raises(RecordTypeRejected) as raised:
        RecordTypeRegistry().register(definition)
    assert f"/{name}" in str(raised.value)


def test_n11_undeclared_free_text_and_late_registration_are_refused() -> None:
    model = create_model(
        "Probe", __base__=ContentModel, zone_id=(uuid.UUID, ...), remark=(FreeText, ...)
    )
    undeclared = RecordType(
        record_type="remark_recorded",
        writer_unit=ActorUnit.U04,
        chain_level=ChainLevel.PLANT,
        schema_version=1,
        content_model=model,
    )
    with pytest.raises(RecordTypeRejected):
        RecordTypeRegistry().register(undeclared)
    # Después del arranque el registro está sellado: ni siquiera un tipo correcto entra.
    sealed = RecordTypeRegistry()
    for definition in U02_RECORD_TYPES:
        sealed.register(definition)
    sealed.seal()
    declared = RecordType(
        record_type="remark_recorded",
        writer_unit=ActorUnit.U04,
        chain_level=ChainLevel.PLANT,
        schema_version=1,
        content_model=model,
        free_text_paths=("/remark",),
    )
    with pytest.raises(RecordTypeRejected, match="sellado"):
        sealed.register(declared)
