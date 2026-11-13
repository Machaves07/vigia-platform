"""Lectura de la foto del episodio de la sombra por la instaladora (TASK-573; A-112; QF10).

Los episodios de la sombra son material de **puesta en marcha**: los ve la instaladora en SCR-06,
con ``commissioning.run`` sobre la zona (en la matriz, solo ``provider_installer``, bajo concesión
vigente, A-69). El coordinador, el gerente y el resto no los ven: lo capturado en comisionamiento
no aparece en ningún panel de gestión (BR-GOB-30, RF-BOR-11, RNF-PRI-04).

**Listado** (``EpisodePhotoReader.page``, ``GET /zones/{zone_id}/episode-photos``):

1. la zona en la organización del contexto (RLS) y ``commissioning.run`` sobre ella; si no,
   ``ResourceNotFound`` (``not_found``, nunca ``forbidden``, BR-NUC-09);
2. con la compuerta de uso de la zona aprobada ahora (zona en ``productive``), ``not_found``;
3. las fotos vivas (sin vencer ni retiradas, ``episode_photo_queries``), la más reciente primero
   por ``(started_at, photo_id)``, con ``from``/``to`` sobre ``started_at``; páginas de 50 por
   defecto y 200 como mucho; ``after`` es el ``photo_id`` del último de la página anterior, y un
   cursor que no es una foto viva de la zona es ``invalid_request``;
4. solo metadatos y ``image_absent_reason``: nunca bytes, suma ni URL. **Sin auditoría propia**
   (identificadores y enumeraciones); bajo concesión, la lectura del proveedor se audita como las
   demás lecturas de la flota (``fleet_read`` con el número de fotos, BR-NUC-38), en la misma
   transacción.

**Imagen** (``EpisodePhotoReader.image``, ``GET /episode-photos/{photo_id}/image``): **una**
entrada ``episode_photo_read`` por petición, en la cadena de la organización del contexto (bajo
concesión, la del cliente, con el instalador y su concesión como actor: el contexto de las demás
operaciones de la instaladora bajo concesión, A-58):

- foto invisible (inexistente, de otra organización o planta, concesión vencida), sin la clave
  sobre su zona, vencida, retirada o sin imagen → ``denied`` y ``not_found``. La denegación no
  depende de la base (como ``authorization_denied``): si su entrada no se escribe, la respuesta
  sigue siendo ``not_found``;
- con los bytes: se recalcula la ``sha256`` y el tamaño **antes** de servir. Si no coinciden con lo
  guardado → ``error`` y nada se sirve (``EpisodePhotoIntegrityFailed``);
- si coinciden → ``success`` (la lectura concedida). La entrada va en la **misma transacción** que
  la lectura de los bytes y se confirma antes de devolverlos: si la auditoría falla, la transacción
  se revierte y no sale ningún byte.

La petición que la cadena deniega antes de la ruta (un rol sin ``commissioning.run``) deja su
``authorization_denied`` y, por la dependencia de la ruta, su ``episode_photo_read`` ``denied``
(``gate_denied``). Ningún paso lee la hora del sistema: ``Clock`` inyectado.
"""

from __future__ import annotations

import enum
import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Protocol

from vigia_platform.fleet.adapters.postgres.episode_photo_queries import (
    EpisodePhotoSummary,
    PhotoLocation,
    PostgresEpisodePhotoQueries,
    StoredImage,
)
from vigia_platform.identity.authz.authorize import Resource, ResourceNotFound
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import (
    AuditOperation,
    AuditOutcome,
    AuditWriter,
    ResourceRef,
)
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.observability.logging import get_logger, log_context

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "RESOURCE_KIND",
    "EpisodePhotoIntegrityFailed",
    "EpisodePhotoPage",
    "EpisodePhotoReader",
    "EpisodePhotoReads",
    "EpisodePhotosRequestInvalid",
    "ServedImage",
    "image_outcome",
]

DEFAULT_PAGE_SIZE: Final = 50
MAX_PAGE_SIZE: Final = 200
"""Fotos por página del listado: 50 por defecto, 200 como mucho (TASK-573)."""
RESOURCE_KIND: Final = "episode_photo"
"""``resource_ref.kind`` de la auditoría y de la autorización."""

_RUN: Final = PermissionKey.COMMISSIONING_RUN

_log = get_logger("fleet.episode_photo_read")


class EpisodePhotosRequestInvalid(ValueError):
    """Cursor, tamaño de página o intervalo fuera de los límites: ``invalid_request``."""


class EpisodePhotoIntegrityFailed(Exception):
    """Los bytes guardados no tienen la suma o el tamaño de la fila: no se sirve nada."""

    def __init__(self) -> None:
        super().__init__("la foto del episodio no coincide con su suma")


class PhotoAuthorizer(Protocol):
    """``AuthorizationPort.authorize`` (``identity.authz.authorize.Authorizer``)."""

    async def authorize(
        self, context: ScopeContext, key: PermissionKey | str, resource: Resource
    ) -> ScopeContext: ...


class EpisodePhotoReads(Protocol):
    """Lo que la lectura usa de ``PostgresEpisodePhotoQueries``."""

    async def zone(self, context: ScopeContext, zone_id: uuid.UUID) -> PhotoLocation | None: ...

    async def location(
        self, context: ScopeContext, photo_id: uuid.UUID
    ) -> PhotoLocation | None: ...

    async def zone_retired(
        self, transaction: Transaction, zone_id: uuid.UUID, now: datetime
    ) -> bool: ...

    async def page(
        self,
        transaction: Transaction,
        zone_id: uuid.UUID,
        *,
        now: datetime,
        since: datetime | None,
        until: datetime | None,
        after: uuid.UUID | None,
        limit: int,
    ) -> tuple[EpisodePhotoSummary, ...] | None: ...

    async def image(
        self, transaction: Transaction, photo_id: uuid.UUID, now: datetime
    ) -> StoredImage | None: ...


@dataclass(frozen=True, slots=True)
class EpisodePhotoPage:
    """Una página del listado; ``next_after`` es el cursor de la siguiente (o ``None``)."""

    photos: tuple[EpisodePhotoSummary, ...]
    next_after: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class ServedImage:
    """Los bytes que se sirven, ya verificados contra la fila."""

    data: bytes = field(repr=False)
    sha256: str


class _Verdict(enum.Enum):
    SERVE = "serve"
    ABSENT = "absent"
    CORRUPT = "corrupt"


def image_outcome(stored: StoredImage | None) -> AuditOutcome:
    """El resultado de la petición de imagen para la foto viva ``stored`` (o ``None``).

    ``denied`` sin foto o sin bytes; ``error`` si los bytes no miden lo declarado o no tienen su
    suma; ``success`` solo si los dos coinciden.
    """
    verdict = _verdict(stored)
    if verdict is _Verdict.ABSENT:
        return AuditOutcome.DENIED
    if verdict is _Verdict.CORRUPT:
        return AuditOutcome.ERROR
    return AuditOutcome.SUCCESS


def _verdict(stored: StoredImage | None) -> _Verdict:
    if stored is None or stored.data is None:
        return _Verdict.ABSENT
    data = stored.data
    if stored.sha256 is None or stored.size_bytes != len(data):
        return _Verdict.CORRUPT
    if hashlib.sha256(data).hexdigest() != stored.sha256:
        return _Verdict.CORRUPT
    return _Verdict.SERVE


@repository
class EpisodePhotoReader:
    """``GET /zones/{zone_id}/episode-photos`` y ``GET /episode-photos/{photo_id}/image``."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        authorizer: PhotoAuthorizer,
        audit: AuditWriter,
        clock: Clock,
        queries: EpisodePhotoReads | None = None,
    ) -> None:
        self._database = database
        self._authorizer = authorizer
        self._audit = audit
        self._clock = clock
        self._queries = queries if queries is not None else PostgresEpisodePhotoQueries(database)

    def __repr__(self) -> str:
        return "EpisodePhotoReader()"

    async def page(
        self,
        context: ScopeContext,
        zone_id: uuid.UUID,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        after: uuid.UUID | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> EpisodePhotoPage:
        """Las fotos vivas de la zona, sin bytes (ver el módulo)."""
        if type(zone_id) is not uuid.UUID:
            raise ResourceNotFound()
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
            raise EpisodePhotosRequestInvalid("limit fuera de 1 a 200")
        if since is not None and until is not None and until <= since:
            raise EpisodePhotosRequestInvalid("to debe ser posterior a from")
        queries = self._queries
        zone = await queries.zone(context, zone_id)
        if zone is None:
            raise ResourceNotFound()
        authorized = await self._authorizer.authorize(
            context, _RUN, Resource.zone(zone.organization_id, zone.plant_id, zone.zone_id)
        )
        now = self._clock.now()
        async with self._database.transaction(authorized) as transaction:
            if await queries.zone_retired(transaction, zone.zone_id, now):
                # BR-GOB-30: con la zona en ``productive`` el material de puesta en marcha deja de
                # existir para la lectura, también antes de que pase la purga.
                raise ResourceNotFound()
            # Una de más: dice si hay página siguiente sin otra consulta.
            found = await queries.page(
                transaction,
                zone.zone_id,
                now=now,
                since=since,
                until=until,
                after=after,
                limit=limit + 1,
            )
            if found is None:
                raise EpisodePhotosRequestInvalid("el cursor no es una foto viva de la zona")
            items = found[:limit]
            if authorized.concession_id is not None:
                # BR-NUC-38: la lectura del proveedor bajo concesión, en la misma transacción.
                await self._audit.append(
                    authorized,
                    AuditOperation.FLEET_READ,
                    plant_id=zone.plant_id,
                    zone_id=zone.zone_id,
                    result_count=len(items),
                    transaction=transaction,
                )
        following = items[-1].photo_id if len(found) > limit else None
        return EpisodePhotoPage(photos=items, next_after=following)

    async def image(self, context: ScopeContext, photo_id: uuid.UUID) -> ServedImage:
        """Los bytes de la foto, con su entrada ``episode_photo_read`` ya confirmada."""
        if type(photo_id) is not uuid.UUID:
            raise ResourceNotFound()
        location = await self._queries.location(context, photo_id)
        if location is None:
            await self._denied(context, photo_id, None)
            raise ResourceNotFound()
        resource = Resource(
            location.organization_id,
            RESOURCE_KIND,
            photo_id,
            plant_id=location.plant_id,
            zone_id=location.zone_id,
        )
        try:
            authorized = await self._authorizer.authorize(context, _RUN, resource)
        except ResourceNotFound:
            await self._denied(context, photo_id, location)
            raise
        now = self._clock.now()
        async with self._database.transaction(authorized) as transaction:
            stored = await self._queries.image(transaction, photo_id, now)
            verdict = _verdict(stored)
            # La entrada se escribe en la transacción de la lectura y se confirma con ella: si
            # falla, la transacción se revierte y la excepción sale antes de servir un byte.
            await self._audit.append(
                authorized,
                AuditOperation.EPISODE_PHOTO_READ,
                outcome=image_outcome(stored),
                plant_id=location.plant_id,
                zone_id=location.zone_id,
                resource=ResourceRef(RESOURCE_KIND, photo_id),
                transaction=transaction,
            )
        if verdict is _Verdict.ABSENT or stored is None or stored.data is None:
            raise ResourceNotFound()
        if verdict is _Verdict.CORRUPT:
            with log_context(zone_id=location.zone_id):
                _log.error("la foto del episodio no coincide con su suma: no se sirve")
            raise EpisodePhotoIntegrityFailed()
        return ServedImage(data=stored.data, sha256=hashlib.sha256(stored.data).hexdigest())

    async def gate_denied(self, context: ScopeContext, photo_id: uuid.UUID) -> None:
        """La entrada ``denied`` de una petición de imagen que la cadena denegó antes de la ruta
        (sin ``commissioning.run``). Sin zona: la petición no llegó a ver la foto."""
        await self._denied(context, photo_id, None)

    async def _denied(
        self, context: ScopeContext, photo_id: uuid.UUID, location: PhotoLocation | None
    ) -> None:
        """``episode_photo_read`` ``denied`` en su propia transacción; sin base, no cambia nada."""
        try:
            await self._audit.append(
                context,
                AuditOperation.EPISODE_PHOTO_READ,
                outcome=AuditOutcome.DENIED,
                plant_id=None if location is None else location.plant_id,
                zone_id=None if location is None else location.zone_id,
                resource=ResourceRef(RESOURCE_KIND, photo_id),
            )
        except Exception:
            # Como ``authorization_denied``: la denegación no depende de la base.
            _log.exception("no se pudo auditar episode_photo_read denegada")
