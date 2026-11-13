"""Guardas puras de la lectura y la purga de la foto del episodio (TASK-573; A-112), sin base.

- ``image_outcome``: ``denied`` sin foto o sin bytes; ``error`` sin suma, con otro tamaño o con
  otra suma; ``success`` solo con las dos iguales (``m_sin_reverificar``).
- ``EpisodePhotoReader.page`` rechaza ``limit`` fuera de 1 a 200 y un intervalo vacío **antes** de
  tocar nada (la ruta ya los acota; esta es la segunda barrera del servicio).
- ``EpisodePhotoPurger`` no se construye con lotes de menos de una fila.
- Las dos rutas declaran **exactamente** ``commissioning.run`` (``m_coordinador_lee`` en la
  declaración: con otra clave de la matriz, el coordinador pasaría la cadena).
- El listado nunca **selecciona** ``image`` ni ``sha256`` (r22: las pruebas de integración miden
  la respuesta, que el modelo cerrado ya filtra; esta mide la sentencia).
"""

from __future__ import annotations

import dataclasses
import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.factories import make_context
from vigia_platform.fleet.adapters.postgres import episode_photo_queries
from vigia_platform.fleet.adapters.postgres.episode_photo_queries import (
    EpisodePhotoSummary,
    StoredImage,
)
from vigia_platform.fleet.application.episode_photo_purge import EpisodePhotoPurger
from vigia_platform.fleet.application.episode_photo_read import (
    EpisodePhotoReader,
    EpisodePhotosRequestInvalid,
    image_outcome,
)
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.ledger.application.audit_writer import AuditOutcome
from vigia_platform.shared.api.app import build_openapi_app
from vigia_platform.shared.api.declarations import iter_declared_routes
from vigia_platform.shared.clock import SimulatedClock

DATA = b"\xff\xd8jpeg sintetico\xff\xd9"
SUM = hashlib.sha256(DATA).hexdigest()
ZONE = uuid.UUID(int=1)
PLANT = uuid.UUID(int=2)


def _stored(**changes: Any) -> StoredImage:
    values: dict[str, Any] = {
        "plant_id": PLANT,
        "zone_id": ZONE,
        "size_bytes": len(DATA),
        "sha256": SUM,
        "data": DATA,
    }
    values.update(changes)
    return StoredImage(**values)


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (None, AuditOutcome.DENIED),
        (_stored(data=None, size_bytes=None, sha256=None), AuditOutcome.DENIED),
        (_stored(sha256=None), AuditOutcome.ERROR),
        (_stored(size_bytes=len(DATA) + 1), AuditOutcome.ERROR),
        (_stored(sha256=hashlib.sha256(DATA + b"x").hexdigest()), AuditOutcome.ERROR),
        (_stored(data=DATA[:-1] + b"\x00"), AuditOutcome.ERROR),
        (_stored(), AuditOutcome.SUCCESS),
    ],
    ids=["sin-foto", "sin-imagen", "sin-suma", "otro-tamano", "otra-suma", "byte-cambiado", "ok"],
)
def test_the_image_outcome_reverifies_size_and_sum(
    stored: StoredImage | None, expected: AuditOutcome
) -> None:
    assert image_outcome(stored) is expected


def test_the_stored_image_never_shows_its_bytes() -> None:
    assert repr(DATA) not in repr(_stored())
    assert "data=" not in repr(_stored())


class _Untouchable:
    """Cualquier uso falla: la guarda tiene que correr antes."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"se tocó {name} antes de validar")


def _reader() -> EpisodePhotoReader:
    untouchable: Any = _Untouchable()
    return EpisodePhotoReader(
        database=untouchable,
        authorizer=untouchable,
        audit=untouchable,
        clock=SimulatedClock(datetime(2026, 11, 13, tzinfo=UTC)),
        queries=untouchable,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 201, -1])
async def test_the_service_rejects_a_limit_outside_1_to_200_before_reading(limit: int) -> None:
    with pytest.raises(EpisodePhotosRequestInvalid, match="limit"):
        await _reader().page(make_context(), ZONE, limit=limit)


@pytest.mark.asyncio
async def test_an_identifier_that_is_not_a_uuid_is_not_found_before_reading() -> None:
    reader = _reader()
    with pytest.raises(ResourceNotFound):
        await reader.page(make_context(), "zona")  # type: ignore[arg-type]
    with pytest.raises(ResourceNotFound):
        await reader.image(make_context(), "foto")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_service_rejects_an_empty_interval_before_reading() -> None:
    moment = datetime(2026, 11, 13, 10, tzinfo=UTC)
    with pytest.raises(EpisodePhotosRequestInvalid, match="posterior"):
        await _reader().page(make_context(), ZONE, since=moment, until=moment)


@pytest.mark.parametrize("field", ["batch_size", "max_batches"])
@pytest.mark.parametrize("value", [0, -3])
def test_the_purger_needs_batches_of_at_least_one(field: str, value: int) -> None:
    untouchable: Any = _Untouchable()
    with pytest.raises(ValueError, match=field):
        EpisodePhotoPurger(
            database=untouchable,
            clock=SimulatedClock(datetime(2026, 11, 13, tzinfo=UTC)),
            queries=untouchable,
            **{field: value},
        )


def _selected(statement: Any) -> list[str]:
    """Las columnas del primer ``SELECT … FROM`` de una sentencia ``text()``, sin alias."""
    sql = str(statement.text)
    columns = sql[sql.index("SELECT ") + len("SELECT ") : sql.index(" FROM ")]
    return [column.strip().removeprefix("p.") for column in columns.split(",")]


def test_the_listing_statements_never_select_the_image_nor_its_sum() -> None:
    """r22 (revisión de Fable): las columnas de ``_PAGE`` son exactamente los campos de
    ``EpisodePhotoSummary``, en su orden; ni ``image`` ni ``sha256`` salen de la base en ninguna
    página, y el cursor solo lee su clave."""
    summary = [item.name for item in dataclasses.fields(EpisodePhotoSummary)]
    assert _selected(episode_photo_queries._PAGE) == summary
    assert not {"image", "sha256"} & set(_selected(episode_photo_queries._PAGE))
    assert _selected(episode_photo_queries._CURSOR) == ["started_at", "photo_id"]


def test_both_routes_declare_exactly_commissioning_run() -> None:
    declared = {
        (method, route.path): [d.permission for d in route.declarations]
        for route in iter_declared_routes(build_openapi_app().routes)
        for method in route.methods
        if route.path in ("/zones/{zone_id}/episode-photos", "/episode-photos/{photo_id}/image")
    }
    assert declared == {
        ("GET", "/zones/{zone_id}/episode-photos"): ["commissioning.run"],
        ("GET", "/episode-photos/{photo_id}/image"): ["commissioning.run"],
    }
