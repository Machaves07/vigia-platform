"""Publicación de la versión objetivo de la flota (TASK-226; LC-GOB-17; BR-GOB-101 y 102; BL §2.7).

``POST /fleet/target-versions`` (``fleet.manage`` sobre la planta) con ``{plant_id, node_ids[] |
group, target_version, maintenance_window {from, to}}``:

1. la planta tiene que ser de la organización del contexto y estar en su alcance con
   ``fleet.manage``; si no, ``ResourceNotFound`` (BR-NUC-09);
2. en **una** transacción (BL §2.7, PAT-GOB-RES-02):

   a. los nodos: con ``node_ids``, todos tienen que ser de esa planta (``ResourceNotFound`` si
      alguno no lo es: de otra planta, de otra organización o inexistente, NFR-GOB-30); con
      ``group = plant``, los nodos no revocados ni dados de baja de la planta (decisión del
      redactor, Notes de TASK-226), y la publicación guarda esa lista resuelta en ``node_ids``;
   b. el candado de la fila de inventario de cada nodo, por ``node_id``;
   c. el instante de publicar (``Clock``, ya con los candados) y la ventana de compatibilidad del
      contrato en ese instante (BR-GOB-101, ``within_contract_window``): fuera,
      ``version_outside_contract_window`` y no queda nada escrito;
   d. ``TargetVersionPublication`` (⛓), ``NodeInventory.target_version`` de cada nodo alcanzado
      (la de su publicación más reciente, la misma que lee el latido),
      el registro ``node_target_version_published`` (``source_key = publication_id``) con un
      evento ``target_version_published`` ``{node_id, target_version}`` por nodo, y la entrada de
      auditoría ``target_version_published`` (la exige una escritura bajo concesión, BR-NUC-38).

``maintenance_window`` es **informativa** (D-5): está en la publicación y en el registro, nunca en
los eventos ni en la respuesta del latido, que lee solo ``target_version`` (TASK-223). La
actualización la aplica el nodo a mano; la plataforma nunca empuja nada (BR-CTR-55).

**Orden de los candados** (el de toda operación de U-03 que toca el inventario de un nodo: latido,
publicación y resultado de actualización): (1) las filas de ``fleet.node_inventory`` de los nodos,
por ``node_id``; (2) la exclusión de la cadena de la planta (el ``INSERT`` del registro); (3) la
cadena de auditoría. El resultado de actualización toma antes su propia fila de
``fleet.update_result`` (clave única), que nadie más bloquea.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from vigia_platform.fleet.adapters.postgres.fleet_version_store import PostgresFleetVersionStore
from vigia_platform.fleet.adapters.postgres.inventory_queries import PostgresInventoryQueries
from vigia_platform.fleet.application.common import FleetRejected, FleetWriteFailed
from vigia_platform.fleet.detail_codes import FleetDetailCode
from vigia_platform.fleet.domain.fleet_versions import (
    MAX_TARGET_NODES,
    PUBLISHED_RECORD_TYPE,
    TARGET_PUBLISHED_EVENT,
    MaintenanceWindow,
    NodeGroup,
    TargetVersionInvalid,
    TargetVersionPublication,
    VersionWindowPolicy,
    is_release_version,
    within_contract_window,
)
from vigia_platform.identity.authz.authorize import Authorizer, Resource, ResourceNotFound
from vigia_platform.identity.authz.context import with_unit
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import (
    AuditOperation,
    AuditWriter,
    ResourceRef,
)
from vigia_platform.ledger.application.writer import (
    EscritorExpediente,
    LedgerDatabase,
    LedgerRejection,
    RecordScope,
)
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.outbox.publish import NewEvent
from vigia_platform.shared.signing.keys import to_millisecond

__all__ = ["TargetVersionService"]


class TargetVersionService:
    """``POST /fleet/target-versions``: publica la versión objetivo de nodos de una planta."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        writer: EscritorExpediente,
        authorizer: Authorizer,
        audit: AuditWriter,
        clock: Clock,
        policy: VersionWindowPolicy,
    ) -> None:
        self._database = database
        self._writer = writer
        self._authorizer = authorizer
        self._audit = audit
        self._clock = clock
        self._policy = policy
        self._store = PostgresFleetVersionStore(database)
        self._queries = PostgresInventoryQueries(database)

    def __repr__(self) -> str:
        return "TargetVersionService()"

    async def _plant(self, context: ScopeContext, plant_id: uuid.UUID) -> ScopeContext:
        """El contexto autorizado con ``fleet.manage`` sobre la planta; inexistente o ajena,
        igual (``ResourceNotFound``)."""
        if not isinstance(context, ScopeContext) or type(plant_id) is not uuid.UUID:
            raise ResourceNotFound()
        if not await self._queries.plant_exists(context, plant_id):
            raise ResourceNotFound()
        return await self._authorizer.authorize(
            context, PermissionKey.FLEET_MANAGE, Resource.plant(context.organization_id, plant_id)
        )

    async def publish(
        self,
        context: ScopeContext,
        plant_id: uuid.UUID,
        *,
        node_ids: Sequence[uuid.UUID] | None,
        group: NodeGroup | None,
        target_version: str,
        window_from: datetime,
        window_to: datetime,
    ) -> TargetVersionPublication:
        """Publica ``target_version`` para los nodos y devuelve la publicación escrita.

        Raises:
            ResourceNotFound: la planta o algún nodo no es de la organización, de la planta o
                del alcance.
            TargetVersionInvalid: ``node_ids`` y ``group`` a la vez o ninguno, nodos repetidos,
                ninguno o más de 100, versión que no es ``ReleaseVersion`` o ventana vacía.
            FleetRejected: ``fleet_version_outside_contract_window`` (BR-GOB-101).
        """
        authorized = await self._plant(context, plant_id)
        if (node_ids is None) == (group is None):
            raise TargetVersionInvalid("se publica por node_ids o por group, nunca los dos")
        if node_ids is not None:
            requested = tuple(node_ids)
            if not 1 <= len(requested) <= MAX_TARGET_NODES:
                raise TargetVersionInvalid("la publicación alcanza de 1 a 100 nodos")
            if len(set(requested)) != len(requested):
                raise TargetVersionInvalid("la publicación no repite nodos")
        if not is_release_version(target_version):
            raise TargetVersionInvalid("la versión objetivo no es MAJOR.MINOR.PATCH en minúsculas")
        window = MaintenanceWindow.of(window_from, window_to)
        writer = with_unit(authorized, ActorUnit.U03)
        async with self._database.transaction(writer) as transaction:
            if node_ids is not None:
                found = await self._store.plant_nodes(transaction, plant_id, requested)
                if found != frozenset(requested):
                    raise ResourceNotFound()
                nodes = requested
            else:
                nodes = await self._store.group_nodes(transaction, plant_id, MAX_TARGET_NODES + 1)
                if not 1 <= len(nodes) <= MAX_TARGET_NODES:
                    raise TargetVersionInvalid("el grupo de la planta tiene de 1 a 100 nodos")
            await self._store.lock_inventory(transaction, nodes)
            now = self._clock.now()
            if not within_contract_window(target_version, self._policy, now):
                raise FleetRejected(FleetDetailCode.VERSION_OUTSIDE_CONTRACT_WINDOW)
            publication = TargetVersionPublication(
                publication_id=uuid7(self._clock),
                plant_id=plant_id,
                target_version=target_version,
                node_ids=nodes,
                window=window,
                published_by=uuid.UUID(str(authorized.actor.id)),
                published_at=to_millisecond(now),
                ledger_record_id=uuid7(self._clock),
            )
            await self._store.insert_publication(transaction, publication)
            await self._store.project_target(transaction, publication)
            written = await self._writer.write(
                writer,
                PUBLISHED_RECORD_TYPE,
                publication.record_content(),
                scope=RecordScope(plant_id=plant_id),
                events=tuple(
                    NewEvent(event_name=TARGET_PUBLISHED_EVENT, payload=payload)
                    for payload in publication.event_payloads()
                ),
                occurred_at=publication.published_at,
                transaction=transaction,
                record_id=publication.ledger_record_id,
            )
            if isinstance(written, LedgerRejection):
                raise FleetWriteFailed(written)
            await self._audit.append(
                writer,
                AuditOperation.TARGET_VERSION_PUBLISHED,
                plant_id=plant_id,
                resource=ResourceRef("target_version_publication", publication.publication_id),
                result_count=len(nodes),
                transaction=transaction,
            )
        return publication
