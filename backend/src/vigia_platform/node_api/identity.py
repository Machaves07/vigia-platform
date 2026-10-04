"""Paso (2) de la verificación previa: identidad del nodo por petición, sin caché (PAT-GOB-SEG-01).

**Lector del certificado** (``ClientCertificateReader``, un puerto: el modo ``passthrough`` de la
contingencia R2, con la aplicación terminando TLS, sería otra implementación; TASK-238). El de
producción, ``AlbCertificateHeaders``, lee lo que el balanceador de nodos ya verificó contra el
almacén ``vigia-node-trust`` (``infrastructure-design.md`` §5.2 de U-03):

- ``X-Amzn-Mtls-Clientcert-Leaf``: la hoja en PEM codificado en URL; se analiza con
  ``cryptography.x509`` y su sujeto tiene que tener el perfil de ``certificate_profile``;
- ``-Serial-Number`` (hexadecimal), ``-Subject`` e ``-Issuer`` (RFC 2253) y ``-Validity``
  (``NotBefore=…;NotAfter=…``): cada una **exactamente una vez** y coherente con la hoja (el
  mismo número, los mismos pares atributo-valor y la misma vigencia al segundo);
- la hoja vigente en el instante de la petición (``Clock``).

Cualquier falta, repetición o incoherencia es ``node_not_enrolled`` (401): no hay una identidad de
nodo que verificar. El balanceador de personas quita las cabeceras ``X-Amzn-Mtls-*`` entrantes
(U-02 §4.2) y el alta, la única ruta sin certificado, no las lee.

**Resolución** (``NodeIdentity.resolve``): ``ScopeContexts.context_from_node`` con
``PostgresNodeContextStore``, **una sola consulta indexada** por ``(node_id, certificate_serial)``
(índice único ``node_credential_identity`` de ``gob_0018`` e ``zone_node_assignment_node``) bajo
la organización del certificado: identidad, credencial, sucesora, marca de flota y asignaciones de
zona en la misma sentencia, sin nada guardado entre peticiones (PR-GOB-28). Tramo
``node_api.identity`` por verificación (NFR-GOB-57).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol
from urllib.parse import unquote

from cryptography import x509
from opentelemetry import trace as otel_trace
from sqlalchemy import text
from starlette.datastructures import Headers
from vigia_contracts.models.enumerations import RejectionCode

from vigia_platform.identity.authz.context import (
    EnrollmentRow,
    EnrollmentScope,
    NodeAssignment,
    NodeContextStore,
    NodeRow,
    NodeScope,
    PresentedNode,
)
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.node_api.certificate_profile import (
    CertificateProfileError,
    read_subject,
    serial_hex,
)
from vigia_platform.node_api.rejections import NodeRejection
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.observability.tracing import TRACER_NAME, span_name

__all__ = [
    "ENROLLMENT_SCOPE_STATEMENT",
    "LEAF_HEADER",
    "MAX_LEAF_CHARS",
    "NODE_IDENTITY_STATEMENT",
    "AlbCertificateHeaders",
    "ClientCertificateReader",
    "NodeContextBuilder",
    "NodeIdentity",
    "PostgresNodeContextStore",
    "dn_pairs",
]

LEAF_HEADER: Final = "x-amzn-mtls-clientcert-leaf"
SERIAL_HEADER: Final = "x-amzn-mtls-clientcert-serial-number"
SUBJECT_HEADER: Final = "x-amzn-mtls-clientcert-subject"
ISSUER_HEADER: Final = "x-amzn-mtls-clientcert-issuer"
VALIDITY_HEADER: Final = "x-amzn-mtls-clientcert-validity"
MAX_LEAF_CHARS: Final = 16_384
"""Tope de la hoja codificada ``[objetivo propio]``: un certificado P-256 ocupa menos de 2 KB."""
_MAX_HEADER_CHARS: Final = 1_024
_SERIAL_TEXT: Final = re.compile(r"[0-9A-Fa-f]{1,64}")
_INSTANT: Final = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
_VALIDITY: Final = re.compile(
    rf"NotBefore=(?P<before>{_INSTANT});\s*NotAfter=(?P<after>{_INSTANT})"
)
_DN_TYPE: Final = re.compile(r"[A-Za-z][A-Za-z0-9-]*|[0-9]+(?:\.[0-9]+)+")

IDENTITY_SPAN: Final = span_name("node_api.identity")

NODE_IDENTITY_STATEMENT: Final = text(
    "SELECT n.node_id, n.organization_id, n.plant_id, n.code, n.status AS node_status,"
    " f.enrolled_at, f.revoked_at, f.decommissioned_at,"
    " c.organization_id AS credential_organization_id, c.plant_id AS credential_plant_id,"
    " c.status AS credential_status, c.issued_at, c.expires_at,"
    " (SELECT min(s.issued_at) FROM fleet.node_credential AS s"
    " WHERE s.node_id = c.node_id AND s.rotated_from = c.credential_id) AS successor_issued_at,"
    " ARRAY(SELECT a.zone_id FROM identity.zone_node_assignment AS a"
    " WHERE a.node_id = n.node_id ORDER BY a.assignment_id) AS zone_ids,"
    " ARRAY(SELECT a.assigned_at FROM identity.zone_node_assignment AS a"
    " WHERE a.node_id = n.node_id ORDER BY a.assignment_id) AS assigned_ats,"
    " ARRAY(SELECT a.unassigned_at FROM identity.zone_node_assignment AS a"
    " WHERE a.node_id = n.node_id ORDER BY a.assignment_id) AS unassigned_ats"
    " FROM fleet.node_credential AS c"
    " JOIN identity.node_identity AS n ON n.node_id = c.node_id"
    " LEFT JOIN fleet.node_fleet_record AS f ON f.node_id = c.node_id"
    " WHERE c.node_id = :node_id AND c.certificate_serial = :certificate_serial"
)
"""La sentencia única de ``context_from_node``: una fila o ninguna, sin caché."""

ENROLLMENT_SCOPE_STATEMENT: Final = text(
    "SELECT node_id, organization_id, plant_id, code, status AS node_status"
    " FROM fleet.vigia_node_enrollment_scope(CAST(:node_id AS uuid))"
)
"""El nodo declarado de un ``node_id`` en cualquier organización (``gob_0019``)."""


def _uuid(value: object) -> uuid.UUID:
    """asyncpg devuelve su propio tipo de UUID; el contexto exige ``uuid.UUID``."""
    return uuid.UUID(str(value))


@repository
class PostgresNodeContextStore:
    """``NodeContextStore`` sobre ``shared.db``: una sentencia de lectura por llamada."""

    def __init__(self, database: LedgerDatabase) -> None:
        self._database = database

    async def node_row(
        self, lookup: ScopeContext, node_id: uuid.UUID, certificate_serial: str
    ) -> NodeRow | None:
        rows = await self._database.read(
            lookup,
            NODE_IDENTITY_STATEMENT,
            {"node_id": node_id, "certificate_serial": certificate_serial},
        )
        if not rows:
            return None
        (row,) = rows
        zones: Sequence[Any] = row.zone_ids or ()
        assigned: Sequence[datetime] = row.assigned_ats or ()
        unassigned: Sequence[datetime | None] = row.unassigned_ats or ()
        return NodeRow(
            node_id=_uuid(row.node_id),
            organization_id=_uuid(row.organization_id),
            plant_id=_uuid(row.plant_id),
            code=str(row.code),
            node_status=str(row.node_status),
            enrolled_at=row.enrolled_at,
            revoked_at=row.revoked_at,
            decommissioned_at=row.decommissioned_at,
            credential_organization_id=_uuid(row.credential_organization_id),
            credential_plant_id=_uuid(row.credential_plant_id),
            credential_status=str(row.credential_status),
            issued_at=row.issued_at,
            expires_at=row.expires_at,
            successor_issued_at=row.successor_issued_at,
            assignments=tuple(
                NodeAssignment(_uuid(zone), start, end)
                for zone, start, end in zip(zones, assigned, unassigned, strict=True)
            ),
        )

    async def enrollment_row(
        self, lookup: ScopeContext, node_id: uuid.UUID
    ) -> EnrollmentRow | None:
        rows = await self._database.read(
            lookup, ENROLLMENT_SCOPE_STATEMENT, {"node_id": str(node_id)}
        )
        if not rows:
            return None
        (row,) = rows
        return EnrollmentRow(
            node_id=_uuid(row.node_id),
            organization_id=_uuid(row.organization_id),
            plant_id=_uuid(row.plant_id),
            code=str(row.code),
            node_status=str(row.node_status),
        )


# --- Lector del certificado ---------------------------------------------------------------------


class ClientCertificateReader(Protocol):
    """El certificado de cliente que se verificó antes de llegar a la aplicación."""

    def read(self, headers: Headers, now: datetime) -> PresentedNode:
        """El nodo que presenta; ``NodeRejection(node_not_enrolled)`` si no hay uno coherente."""
        ...


def _not_enrolled() -> NodeRejection:
    return NodeRejection(RejectionCode.NODE_NOT_ENROLLED)


def _single(headers: Headers, name: str, limit: int = _MAX_HEADER_CHARS) -> str:
    values = headers.getlist(name)
    if len(values) != 1 or not 0 < len(values[0]) <= limit:
        raise _not_enrolled()
    return values[0]


def dn_pairs(text: str) -> list[tuple[str, str]] | None:
    """Los pares ``(TIPO, valor)`` de un nombre RFC 2253/4514 sin escapes; ``None`` si no se lee.

    El perfil de los certificados de Vigía no produce valores con comas, comillas, ``+`` ni
    escapes; un nombre que los lleve no es de un nodo y se rechaza (fallo cerrado).
    """
    if not text or any(mark in text for mark in ("\\", '"', "+", "#", "<", ">", ";")):
        return None
    pairs: list[tuple[str, str]] = []
    for part in text.split(","):
        kind, separator, value = part.strip().partition("=")
        if not separator or _DN_TYPE.fullmatch(kind) is None or not value or value != value.strip():
            return None
        pairs.append((kind.upper(), value))
    return pairs


def _name_pairs(name: x509.Name) -> list[tuple[str, str]]:
    return [(attribute.rfc4514_attribute_name.upper(), str(attribute.value)) for attribute in name]


def _instant(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _same_second(first: datetime, second: datetime) -> bool:
    return first.replace(microsecond=0) == second.replace(microsecond=0)


@dataclass(frozen=True, slots=True)
class AlbCertificateHeaders:
    """``ClientCertificateReader`` sobre las cabeceras del balanceador de nodos (§5.2)."""

    def read(self, headers: Headers, now: datetime) -> PresentedNode:
        leaf_text = unquote(_single(headers, LEAF_HEADER, MAX_LEAF_CHARS))
        try:
            leaf = x509.load_pem_x509_certificate(leaf_text.encode("ascii"))
            subject = read_subject(leaf.subject)
            serial = serial_hex(leaf.serial_number)
        except (ValueError, UnicodeError, CertificateProfileError):
            raise _not_enrolled() from None
        serial_header = _single(headers, SERIAL_HEADER).strip()
        if _SERIAL_TEXT.fullmatch(serial_header) is None or int(serial_header, 16) != int(
            serial, 16
        ):
            raise _not_enrolled()
        for header, name in ((SUBJECT_HEADER, leaf.subject), (ISSUER_HEADER, leaf.issuer)):
            pairs = dn_pairs(_single(headers, header))
            if pairs is None or sorted(pairs) != sorted(_name_pairs(name)):
                raise _not_enrolled()
        validity = _VALIDITY.fullmatch(_single(headers, VALIDITY_HEADER).strip())
        not_before, not_after = leaf.not_valid_before_utc, leaf.not_valid_after_utc
        if (
            validity is None
            or not _same_second(_instant(validity["before"]), not_before)
            or not _same_second(_instant(validity["after"]), not_after)
            or not not_before <= now <= not_after
        ):
            raise _not_enrolled()
        return PresentedNode(
            node_id=subject.node_id,
            organization_id=subject.organization_id,
            plant_id=subject.plant_id,
            certificate_serial=serial,
        )


# --- Resolución --------------------------------------------------------------------------------


class NodeContextBuilder(Protocol):
    """``ScopeContexts`` (``identity.authz.context``): el quinto constructor (A-51)."""

    async def context_from_node(
        self,
        store: NodeContextStore,
        presented: PresentedNode,
        *,
        correlation_id: uuid.UUID | None = None,
    ) -> NodeScope: ...

    async def context_from_node_enrollment(
        self,
        store: NodeContextStore,
        node_id: uuid.UUID,
        *,
        correlation_id: uuid.UUID | None = None,
    ) -> EnrollmentScope | None: ...


class NodeIdentity:
    """Identidad del nodo de cada petición: certificado → una consulta → ``NodeScope``."""

    def __init__(
        self,
        *,
        contexts: NodeContextBuilder,
        store: NodeContextStore,
        reader: ClientCertificateReader | None = None,
        tracer: otel_trace.Tracer | None = None,
    ) -> None:
        self._contexts = contexts
        self._store = store
        self._reader = reader if reader is not None else AlbCertificateHeaders()
        self._tracer = tracer if tracer is not None else otel_trace.get_tracer(TRACER_NAME)

    def presented(self, headers: Headers, now: datetime) -> PresentedNode:
        """El nodo del certificado, sin tocar la base (sirve también a la clave del límite)."""
        return self._reader.read(headers, now)

    async def resolve(self, presented: PresentedNode, correlation_id: uuid.UUID) -> NodeScope:
        """``context_from_node`` de ``presented`` (``NodeContextRejected`` si no hay contexto)."""
        with self._tracer.start_as_current_span(
            IDENTITY_SPAN,
            attributes={
                "correlation_id": str(correlation_id),
                "organization_id": str(presented.organization_id),
                "plant_id": str(presented.plant_id),
                "node_id": str(presented.node_id),
            },
        ):
            return await self._contexts.context_from_node(
                self._store, presented, correlation_id=correlation_id
            )

    async def enrollment(
        self, node_id: uuid.UUID, correlation_id: uuid.UUID
    ) -> EnrollmentScope | None:
        """El contexto del alta del nodo declarado ``node_id`` (lo usa la ruta de TASK-219)."""
        with self._tracer.start_as_current_span(
            IDENTITY_SPAN,
            attributes={"correlation_id": str(correlation_id), "node_id": str(node_id)},
        ):
            return await self._contexts.context_from_node_enrollment(
                self._store, node_id, correlation_id=correlation_id
            )
