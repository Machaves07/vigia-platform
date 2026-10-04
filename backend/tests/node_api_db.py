"""Filas sintéticas de identidad de nodo para las pruebas de ``node_api`` con PostgreSQL (TASK-206).

Sobre la base sembrada por ``tests/identity_db.py`` (dos clientes con dos plantas cada uno),
``insert_node`` da de alta un nodo **nuevo** con una zona propia asignada, su marca de flota
(``fleet.node_fleet_record`` con ``enrolled_at``) y ``insert_credential`` le emite una credencial
(``fleet.node_credential``) con el número de serie de una hoja de ``TestAuthority``. Todo con la
conexión de superusuario (siembra), nunca con ``vigia_app``; las marcas salen del reloj simulado
de la prueba. Las operaciones de ``revoke_*``, ``rotate`` y ``reassign`` son las que harán
TASK-218 y TASK-219, escritas aquí con las transiciones que admite ``gob_0018``.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
import uuid
from dataclasses import dataclass
from typing import Any

from cryptography import x509

from tests.identity_db import BASE_TIME
from tests.node_api_support import DAY, TestAuthority
from vigia_platform.node_api.certificate_profile import NodeSubject, serial_hex

__all__ = ["DbNode", "insert_credential", "insert_node", "issue", "reassign", "revoke_credential"]


@dataclass(frozen=True)
class DbNode:
    node_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    user_id: uuid.UUID

    @property
    def subject(self) -> NodeSubject:
        return NodeSubject(self.node_id, self.organization_id, self.plant_id)


async def insert_zone(
    admin: Any, organization_id: uuid.UUID, plant_id: uuid.UUID, user_id: uuid.UUID
) -> uuid.UUID:
    zone_id = uuid.uuid4()
    await admin.execute(
        "INSERT INTO identity.zone (zone_id, organization_id, plant_id, code, name, created_at,"
        " created_by) VALUES ($1, $2, $3, $4, 'Zona sintética de nodo', $5, $6)",
        zone_id,
        organization_id,
        plant_id,
        f"ZN-{secrets.token_hex(8).upper()}",
        BASE_TIME,
        user_id,
    )
    return zone_id


async def insert_node(
    admin: Any,
    organization_id: uuid.UUID,
    plant_id: uuid.UUID,
    user_id: uuid.UUID,
    now: dt.datetime,
) -> DbNode:
    """Nodo dado de alta hace 30 días con una zona propia asignada desde entonces."""
    node_id = uuid.uuid4()
    zone_id = await insert_zone(admin, organization_id, plant_id, user_id)
    async with admin.transaction():
        await admin.execute(
            "INSERT INTO identity.node_identity (node_id, organization_id, plant_id, code, status,"
            " created_at) VALUES ($1, $2, $3, $4, 'enrolled', $5)",
            node_id,
            organization_id,
            plant_id,
            f"ND-{secrets.token_hex(8).upper()}",
            BASE_TIME,
        )
        await admin.execute(
            "INSERT INTO identity.zone_node_assignment (assignment_id, organization_id, plant_id,"
            " zone_id, node_id, assigned_at, assigned_by) VALUES ($1, $2, $3, $4, $5, $6, $7)",
            uuid.uuid4(),
            organization_id,
            plant_id,
            zone_id,
            node_id,
            now - 30 * DAY,
            user_id,
        )
        await admin.execute(
            "INSERT INTO fleet.node_fleet_record (node_id, organization_id, plant_id, declared_at,"
            " declared_by, enrolled_at, hardware_fingerprint) VALUES ($1, $2, $3, $4, $5, $6, $7)",
            node_id,
            organization_id,
            plant_id,
            now - 31 * DAY,
            user_id,
            now - 30 * DAY,
            secrets.token_hex(32),
        )
    return DbNode(node_id, organization_id, plant_id, zone_id, user_id)


async def insert_credential(
    admin: Any,
    node: DbNode,
    certificate: x509.Certificate,
    *,
    issued_at: dt.datetime,
    expires_at: dt.datetime,
    rotated_from: uuid.UUID | None = None,
) -> uuid.UUID:
    """La fila ``active`` de ``certificate`` (número de serie del perfil de ``node_api``)."""
    credential_id = uuid.uuid4()
    await admin.execute(
        "INSERT INTO fleet.node_credential (credential_id, organization_id, plant_id, node_id,"
        " certificate_serial, subject, issued_at, expires_at, status, rotated_from)"
        " VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, 'active', $9)",
        credential_id,
        node.organization_id,
        node.plant_id,
        node.node_id,
        serial_hex(certificate.serial_number),
        json.dumps(
            {
                "node_id": str(node.node_id),
                "organization_id": str(node.organization_id),
                "plant_id": str(node.plant_id),
            }
        ),
        issued_at,
        expires_at,
        rotated_from,
    )
    return credential_id


async def issue(
    admin: Any, authority: TestAuthority, node: DbNode, now: dt.datetime, **changes: Any
) -> tuple[x509.Certificate, uuid.UUID]:
    """Hoja nueva de ``node`` (365 días) y su credencial ``active`` desde ``now``."""
    certificate = authority.leaf(node.subject, not_before=now, not_after=now + 365 * DAY)
    credential_id = await insert_credential(
        admin,
        node,
        certificate,
        issued_at=changes.pop("issued_at", now),
        expires_at=changes.pop("expires_at", now + 365 * DAY),
        **changes,
    )
    return certificate, credential_id


async def revoke_credential(admin: Any, credential_id: uuid.UUID, now: dt.datetime) -> None:
    await admin.execute(
        "UPDATE fleet.node_credential SET status = 'revoked', revoked_at = $2"
        " WHERE credential_id = $1",
        credential_id,
        now,
    )


async def reassign(
    admin: Any, node: DbNode, old_zone: uuid.UUID, new_zone: uuid.UUID, now: dt.datetime
) -> None:
    """Retira ``old_zone`` del nodo y le asigna ``new_zone`` en el mismo instante."""
    async with admin.transaction():
        await admin.execute(
            "UPDATE identity.zone_node_assignment SET unassigned_at = $3"
            " WHERE node_id = $1 AND zone_id = $2 AND unassigned_at IS NULL",
            node.node_id,
            old_zone,
            now,
        )
        await admin.execute(
            "INSERT INTO identity.zone_node_assignment (assignment_id, organization_id, plant_id,"
            " zone_id, node_id, assigned_at, assigned_by) VALUES ($1, $2, $3, $4, $5, $6, $7)",
            uuid.uuid4(),
            node.organization_id,
            node.plant_id,
            new_zone,
            node.node_id,
            now,
            node.user_id,
        )
