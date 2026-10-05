"""Rotación de la credencial del nodo (TASK-219; BR-GOB-64; NFR-GOB-34; BLM §3.5 y nota de §4).

``POST credential-rotations`` llega con mTLS: ``node_api`` ya resolvió el ``NodeScope`` con la
credencial presentada (``active`` o ``overlapping`` dentro de su solapamiento), validó el cuerpo y
las dos CSR (nombre común = el ``node_id`` del certificado). ``rotate``:

1. exige la credencial presentada **``active``**: con una ``overlapping`` no se rota
   (``RotationRefused`` → ``node_revoked``: esa credencial se está retirando);
2. elige la dirección de la vista en vivo (``csr.announced_host``) y firma cliente y servidor
   nuevos con ``vigia-node-ca`` (``NodeCaIssuer``, plazo; sin respuesta → transitorio, nada
   escrito);
3. en **una** transacción, con la ficha del nodo bloqueada primero (orden de
   ``fleet.application.common``) y la credencial presentada bloqueada después: nodo ``enrolled``
   sin revocación ni baja; la presentada sigue ``active`` (si otra rotación ganó, ya es
   ``overlapping`` y esta responde ``node_revoked``); las ``overlapping`` que ya no autentican
   pasan a ``superseded`` (materialización perezosa); la presentada pasa a ``overlapping`` con un
   ``UPDATE`` condicional de una fila; la nueva nace ``active`` con ``rotated_from``; y el registro
   ``node_credential_rotated`` ``{node_id, credential_id, rotated_from, certificate_serial,
   issued_at, expires_at}`` (``source_key = credential_id``), sin evento.

La anterior sigue autenticando 24 h desde el ``issued_at`` de la nueva: lo decide la consulta de
identidad de cada petición (``context_from_node``), sin columna ni tarea nuevas.
"""

from __future__ import annotations

import enum
from typing import Final

from vigia_platform.fleet.adapters.ca.certificate_profiles import NodeCaIssuer, NodeCaUnavailable
from vigia_platform.fleet.adapters.ca.csr import NodeCsr, announced_host
from vigia_platform.fleet.adapters.postgres.credential_store import PostgresCredentialStore
from vigia_platform.fleet.application.common import FleetDependencies, write
from vigia_platform.fleet.application.enrollment import (
    CA_RETRY_AFTER_SECONDS,
    CredentialUnavailable,
    IssuedCredential,
    PlatformKeys,
    platform_public_keys,
)
from vigia_platform.fleet.domain.enums import CredentialStatus
from vigia_platform.fleet.domain.node_credential import NodeCredential, stale_overlapping
from vigia_platform.fleet.domain.node_subject import NodeSubject, serial_hex
from vigia_platform.identity.authz.context import NodeScope
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "ROTATED_RECORD_TYPE",
    "CredentialRotationService",
    "RotationRefusal",
    "RotationRefused",
]

ROTATED_RECORD_TYPE: Final = "node_credential_rotated"


class RotationRefusal(enum.StrEnum):
    """Por qué no se rota (cada uno es un ``rejection_code`` del contrato)."""

    NODE_REVOKED = "node_revoked"
    """Credencial presentada no ``active`` (``overlapping`` o ya sustituida) o nodo revocado."""
    NODE_NOT_ENROLLED = "node_not_enrolled"
    """El nodo ya no está ``enrolled`` (re-alta pendiente)."""


class RotationRefused(Exception):
    def __init__(self, reason: RotationRefusal) -> None:
        super().__init__(f"rotación rechazada: {reason.value}")
        self.reason = RotationRefusal(reason)


class CredentialRotationService:
    """``POST credential-rotations`` del contrato."""

    def __init__(
        self,
        deps: FleetDependencies,
        *,
        issuer: NodeCaIssuer,
        keys: PlatformKeys,
        credentials: PostgresCredentialStore | None = None,
    ) -> None:
        self._deps = deps
        self._issuer = issuer
        self._keys = keys
        self._credentials = credentials if credentials is not None else PostgresCredentialStore()

    def __repr__(self) -> str:
        return "CredentialRotationService()"

    async def rotate(self, node: NodeScope, client: NodeCsr, server: NodeCsr) -> IssuedCredential:
        """Credencial nueva ``active``; la presentada queda ``overlapping`` (ver el módulo)."""
        if not isinstance(node, NodeScope):
            raise TypeError("node debe ser el NodeScope de la petición")
        if node.credential_status != CredentialStatus.ACTIVE.value:
            raise RotationRefused(RotationRefusal.NODE_REVOKED)
        deps = self._deps
        fleet = await deps.nodes.node(node.context, node.node_id)
        if fleet is None:
            raise RotationRefused(RotationRefusal.NODE_NOT_ENROLLED)
        host = announced_host(server, fleet.live_view_local_url)
        public_keys = platform_public_keys(self._keys)
        now = deps.clock.now()
        subject = NodeSubject(node.node_id, node.organization_id, node.plant_id)
        try:
            issued = await self._issuer.issue(
                subject, client.public_key, server.public_key, host, now=now
            )
        except NodeCaUnavailable:
            raise CredentialUnavailable("node_ca", CA_RETRY_AFTER_SECONDS) from None
        credential = NodeCredential(
            credential_id=uuid7(deps.clock, deps.random_bytes),
            organization_id=node.organization_id,
            plant_id=node.plant_id,
            node_id=node.node_id,
            certificate_serial=serial_hex(issued.client.serial_number),
            issued_at=now,
            expires_at=issued.client.not_valid_after_utc,
        )
        rotated = await self._commit(node, credential)
        deps.platform_metrics().node_credential_rotations_total.add(1)
        return IssuedCredential(
            credential=rotated,
            certificates=issued,
            platform_public_keys=tuple(public_keys),
        )

    async def _commit(self, node: NodeScope, credential: NodeCredential) -> NodeCredential:
        deps = self._deps
        now = credential.issued_at
        async with deps.database.transaction(node.context) as transaction:
            # Primer candado: la ficha del nodo (como la revocación y la re-alta).
            locked = await deps.nodes.lock(transaction, node.node_id)
            if locked is None or locked.record.revoked or locked.record.decommissioned:
                raise RotationRefused(RotationRefusal.NODE_REVOKED)
            if locked.status != "enrolled":
                raise RotationRefused(
                    RotationRefusal.NODE_REVOKED
                    if locked.status == "revoked"
                    else RotationRefusal.NODE_NOT_ENROLLED
                )
            current = await self._credentials.locked(
                transaction, node.node_id, node.certificate_serial
            )
            if current is None or current.status is not CredentialStatus.ACTIVE:
                raise RotationRefused(RotationRefusal.NODE_REVOKED)
            stored = await self._credentials.credentials(transaction, node.node_id)
            for stale in stale_overlapping(stored, now):
                await self._credentials.transition(
                    transaction, stale, CredentialStatus.OVERLAPPING, CredentialStatus.SUPERSEDED
                )
            if not await self._credentials.transition(
                transaction,
                current.credential_id,
                CredentialStatus.ACTIVE,
                CredentialStatus.OVERLAPPING,
            ):
                raise RotationRefused(RotationRefusal.NODE_REVOKED)
            rotated = NodeCredential(
                credential_id=credential.credential_id,
                organization_id=credential.organization_id,
                plant_id=credential.plant_id,
                node_id=credential.node_id,
                certificate_serial=credential.certificate_serial,
                issued_at=credential.issued_at,
                expires_at=credential.expires_at,
                rotated_from=current.credential_id,
            )
            await self._credentials.insert(transaction, rotated)
            await write(
                deps,
                node.context,
                transaction,
                ROTATED_RECORD_TYPE,
                {
                    "node_id": str(node.node_id),
                    "credential_id": str(rotated.credential_id),
                    "rotated_from": str(current.credential_id),
                    "certificate_serial": rotated.certificate_serial,
                    "issued_at": format_timestamp(rotated.issued_at),
                    "expires_at": format_timestamp(rotated.expires_at),
                },
                plant_id=locked.plant_id,
                occurred_at=now,
            )
        return rotated
