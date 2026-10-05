"""``GET zones/{zone_id}/catalog``: el catálogo firmado de una zona del nodo (LC-GOB-14, BR-GOB-82).

La verificación previa de ``node_api`` ya comprobó versión, certificado y que la zona de la ruta
está entre las asignadas al nodo **en el instante de la petición** (``node_zone_mismatch``,
BR-GOB-88). El servicio lo vuelve a exigir con el alcance que recibe (defensa en profundidad: un
manejador que se llamara sin la verificación tampoco serviría una zona ajena) y responde el
``ZoneCatalogVersion.envelope`` vigente **byte a byte** tal como se guardó: sin canonicalizar, sin
serializar de nuevo y sin firmar (PAT-GOB-REN-02). Con el puerto de firma caído sigue sirviendo.

Una zona asignada sin catálogo publicado todavía responde ``temporarily_unavailable``: el nodo
reintenta y sigue con su catálogo cacheado (decisión del redactor, la misma que TASK-219 toma en el
alta; declarada en el PR). No existe ruta de estado de compuertas (nota T-04).
"""

from __future__ import annotations

import uuid

from vigia_platform.fleet.adapters.postgres.node_catalog_store import PostgresNodeCatalogStore
from vigia_platform.fleet.application.heartbeat import NodeScopeMismatch
from vigia_platform.identity.authz.context import NodeScope
from vigia_platform.ledger.application.writer import LedgerDatabase

__all__ = ["CatalogNotPublished", "ZoneCatalogForNode"]


class CatalogNotPublished(Exception):
    """La zona asignada aún no tiene catálogo publicado: ``temporarily_unavailable``."""


class ZoneCatalogForNode:
    """El sobre del catálogo vigente de una zona asignada al nodo, tal como se guardó."""

    def __init__(
        self, *, database: LedgerDatabase, store: PostgresNodeCatalogStore | None = None
    ) -> None:
        self._database = database
        self._store = store if store is not None else PostgresNodeCatalogStore()

    def __repr__(self) -> str:
        return "ZoneCatalogForNode()"

    async def envelope(self, node: NodeScope, zone_id: uuid.UUID) -> bytes:
        """Los bytes del sobre vigente de ``zone_id``; ``NodeScopeMismatch`` si no es del nodo."""
        if not isinstance(node, NodeScope):
            raise TypeError("node debe ser NodeScope")
        if type(zone_id) is not uuid.UUID or not node.covers_zone(zone_id):
            raise NodeScopeMismatch("zone_id")
        async with self._database.transaction(node.context) as transaction:
            stored = await self._store.current_catalog_envelope(transaction, zone_id)
        if stored is None:
            raise CatalogNotPublished("la zona no tiene catálogo publicado")
        return stored
