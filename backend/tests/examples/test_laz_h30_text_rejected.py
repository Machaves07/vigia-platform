"""H-30 · el texto que no pasa (TASK-340; BR-LAZ-80 y 84; escenario L-5).

Historia: quien escribe el motivo de un registro atribuye la causa a una persona («El operario
omitió el bloqueo…»). Con la política que U-04 registra al arrancar (``loop_free_text``: los dos
validadores con sus cachés y preparadores sobre PostgreSQL) y el ``EscritorExpediente`` real como
``vigia_app``:

- la escritura responde ``free_text_rejected`` en la ruta del texto con
  ``detail_code = loop_person_attribution_detected`` (hacia la API, ``invalid_request`` con ese
  ``detail_code``), y **no deja ninguna fila**: ni registro, ni evento, ni auditoría, ni cabeza de
  cadena (fallo cerrado: un texto marcado no se guarda);
- el mismo registro con el texto reescrito sin persona se escribe;
- un equipo con marca cuyo nombre la organización declaró (el de su zona en ``identity.zone``) se
  acepta: el preparador lee los nombres con el lector de la escritura, bajo su seguridad a nivel
  de fila; sin esa declaración el mismo texto cae como nombre propio.

TASK-328 reúne esta historia con las demás de U-04. Solo datos generados.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.attribution_support import loop_writer
from tests.identity_db import migrated_database
from tests.integration.conftest import PostgresEndpoint
from tests.writer_support import (
    ORDER_TYPE,
    Place,
    WriterEnvironment,
    order_document,
    organization_counts,
    unit_context,
    writer_environment,
)
from vigia_platform.ledger.application.writer import LedgerRejection, LedgerRejectionCode, Receipt
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.shared.api.errors import ApiErrorCode, from_ledger_rejection
from vigia_platform.shared.context import ActorUnit

pytestmark = [pytest.mark.integration, pytest.mark.laz_examples]

ATTRIBUTION = "El operario omitió el bloqueo de energía antes de intervenir la prensa."
REWRITTEN = "El bloqueo de energía no estaba exigido antes de intervenir la prensa."
BRANDED = "Revisión de la guarda de la Prensa Schuler Kompakt durante el turno."
REJECTED = LedgerRejection.of(
    LedgerRejectionCode.FREE_TEXT_REJECTED,
    "/note",
    detail_code=LoopDetailCode.PERSON_ATTRIBUTION_DETECTED.value,
)


@pytest.fixture(scope="module")
def environment(postgres_endpoint: PostgresEndpoint) -> Iterator[WriterEnvironment]:
    with (
        migrated_database(postgres_endpoint, "laz_h30") as migrated,
        writer_environment(migrated, lock_timeout_ms=60_000) as environment,
    ):
        yield environment


def _write(environment: WriterEnvironment, place: Place, note: str) -> Receipt | LedgerRejection:
    document = order_document(place, clips=0)
    document["note"] = note
    context = unit_context(place.organization_id, ActorUnit.U03)
    return environment.loop.run(loop_writer(environment).write(context, ORDER_TYPE, document))


def test_h30_the_text_that_does_not_pass_leaves_no_row(environment: WriterEnvironment) -> None:
    place = Place.new()
    counts = organization_counts(environment.migrated, place.organization_id)
    before = environment.loop.run(counts)
    result = _write(environment, place, ATTRIBUTION)
    assert result == REJECTED
    error = from_ledger_rejection(result)
    assert (error.code, error.detail_code) == (
        ApiErrorCode.INVALID_REQUEST,
        LoopDetailCode.PERSON_ATTRIBUTION_DETECTED.value,
    )
    after = environment.loop.run(organization_counts(environment.migrated, place.organization_id))
    assert after == before
    assert after["ledger.ledger_record"] == 0

    assert isinstance(_write(environment, place, REWRITTEN), Receipt)
    written = environment.loop.run(organization_counts(environment.migrated, place.organization_id))
    assert written["ledger.ledger_record"] == 1


async def _rename_zone(environment: WriterEnvironment, place: Place, name: str) -> None:
    connection = await environment.migrated.connect()
    try:
        await connection.execute(
            "UPDATE identity.zone SET name = $1 WHERE zone_id = $2", name, place.zone_id
        )
    finally:
        await connection.close()


def test_h30_a_declared_equipment_name_passes_and_an_undeclared_one_does_not(
    environment: WriterEnvironment,
) -> None:
    undeclared = Place.new()
    assert _write(environment, undeclared, BRANDED) == REJECTED
    declared = Place.new()
    environment.loop.run(_rename_zone(environment, declared, "Prensa Schuler Kompakt"))
    assert isinstance(_write(environment, declared, BRANDED), Receipt)
