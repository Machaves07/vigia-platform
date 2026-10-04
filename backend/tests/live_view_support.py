"""Entorno de prueba de ``shared.tokens`` (TASK-128) contra PostgreSQL 16 real.

``live_view_environment(endpoint, prefix)``: el entorno de autorización de TASK-125 (base migrada
y sembrada, ``shared.db`` como ``vigia_app``, constructores de contexto y ``Authorizer`` reales),
la auditoría y la bandeja reales, y un ``SigningService`` real con las cinco claves dadas de alta
(gestor de secretos y almacén de claves en memoria, como ``tests/signing_support``) con el mismo
reloj simulado.

- ``process()``: un "proceso" simulado más: su propio motor (``shared.db`` con su pool), su
  escritor de auditoría y su servicio de firma arrancado sobre el mismo almacén de claves; todos
  comparten la base, el reloj y el gestor (PR-NUC-45).
- ``node_verifies``: el verificador sin red del nodo, escrito solo con piezas de U-01: el
  ``KeySet`` fijado con el conjunto publicado por la plataforma, la clave Ed25519 del ``kid`` y el
  modelo estricto ``LiveViewToken``. No usa nada de ``vigia_platform``.
- Altas directas como superusuario (datos generados, NFR-CTR-43): zonas, nodos con o sin
  ``live_view_local_url``, asignaciones nodo-zona y usuarios con un rol sobre un alcance.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import ValidationError
from vigia_contracts.clock import SimulatedClock
from vigia_contracts.models.live_view_token import LiveViewToken
from vigia_contracts.signing import KeySet

from tests.authz_support import AuthzEnvironment, Site, authz_environment
from tests.factories import uuid7
from tests.identity_db import BASE_TIME
from tests.integration.conftest import PostgresEndpoint
from tests.outbox_support import app_database
from tests.signing_support import (
    BOOTSTRAP_ORDER,
    InMemoryKeyStore,
    InMemorySecrets,
    RecordingEvents,
    build_service,
    provider_context,
)
from vigia_platform.ledger.application.audit_writer import AuditWriter
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import Role, ScopeContext, ScopeLevel
from vigia_platform.shared.db import Database
from vigia_platform.shared.signing.service import SigningService
from vigia_platform.shared.tokens import LiveViewTokenService

LIVE_VIEW_URL: Final = "https://nodo-sintetico.local:8443/"


class SkewedClock:
    """El reloj compartido con un desfase fijo: el de un proceso con la hora desajustada."""

    def __init__(self, base: SimulatedClock, skew: timedelta) -> None:
        self._base = base
        self._skew = skew

    def now(self) -> datetime:
        return self._base.now() + self._skew

    def monotonic(self) -> float:
        return self._base.monotonic()

    def sleep(self, seconds: float) -> None:  # pragma: no cover - el servicio no duerme
        self._base.sleep(seconds)


@dataclass
class SimulatedProcess:
    database: Database
    signing: SigningService
    service: LiveViewTokenService


@dataclass
class LiveViewEnvironment:
    authz: AuthzEnvironment
    signing: SigningService
    secrets: InMemorySecrets
    store: InMemoryKeyStore
    events: RecordingEvents
    processes: list[SimulatedProcess] = field(default_factory=list)

    @property
    def clock(self) -> SimulatedClock:
        return self.authz.sessions.clock

    def run(self, awaitable: Any) -> Any:
        return self.authz.run(awaitable)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.authz.fetch(sql, *args)

    @contextlib.contextmanager
    def on_database_time(self) -> Iterator[datetime]:
        """El reloj simulado toma el ``now()`` de la base mientras dura el bloque, y vuelve después.

        La RLS de las concesiones compara su vigencia con el ``now()`` de la base (``nuc_0004``,
        ``nuc_0015``) y el constructor del contexto, con el reloj: con un solo reloj, la concesión,
        la sesión y los registros salen de la misma hora y la prueba no caduca cuando la fecha
        real pasa la simulada (VIG-135). Las claves de firma tienen que ser vigentes a esa hora:
        úsalo en un entorno creado con ``at_database_time``.
        """
        start = self.clock.now()
        (row,) = self.fetch("SELECT now() AS now")
        self.clock.set(row["now"])
        try:
            yield row["now"]
        finally:
            self.clock.set(start)

    def execute(self, sql: str, *args: Any) -> None:
        self.authz.execute(sql, *args)

    # --- Servicios ----------------------------------------------------------------------------

    def service(self, clock: Clock | None = None) -> LiveViewTokenService:
        """El servicio sobre el motor principal (un proceso)."""
        sessions = self.authz.sessions
        return LiveViewTokenService(
            database=sessions.database,
            authorizer=self.authz.authorizer,
            audit=sessions.audit,
            outbox=sessions.outbox,
            signer=self.signing,
            clock=clock or self.clock,
        )

    def process(self, clock: Clock | None = None) -> SimulatedProcess:
        """Otro proceso: motor, auditoría y servicio de firma propios sobre la misma base."""
        sessions = self.authz.sessions
        database = app_database(sessions.migrated, worker_pool_size=2)
        own_clock = clock or self.clock
        signing = build_service(self.clock, self.secrets, self.store, self.events)
        self.run(signing.start())
        audit = AuditWriter(
            database=database,
            clock=own_clock,
            provider_organization_id=sessions.seed.provider_organization_id,
        )
        service = LiveViewTokenService(
            database=database,
            authorizer=self.authz.authorizer,
            audit=audit,
            outbox=sessions.outbox,
            signer=signing,
            clock=own_clock,
        )
        process = SimulatedProcess(database, signing, service)
        self.processes.append(process)
        return process

    def node_key_set(self) -> KeySet:
        """El ``KeySet`` de un nodo: fijado con el conjunto publicado y aplicado firmado."""
        publication = self.signing.current_publication()
        assert publication is not None
        key_set = KeySet(self.clock)
        key_set.pin_initial([dict(key) for key in publication.keys])
        envelope = self.signing.current_key_set_envelope()
        assert envelope is not None
        key_set.apply_signed_set(envelope)
        return key_set

    # --- Altas --------------------------------------------------------------------------------

    def add_site(self, plants: int = 1, zones_per_plant: int = 2) -> Site:
        return self.authz.add_site(plants=plants, zones_per_plant=zones_per_plant)

    def add_node(
        self,
        organization_id: uuid.UUID,
        plant_id: uuid.UUID,
        *,
        url: str | None = None,
        status: str = "enrolled",
    ) -> uuid.UUID:
        node_id = uuid.uuid4()
        self.execute(
            "INSERT INTO identity.node_identity (node_id, organization_id, plant_id, code, status,"
            " live_view_local_url, created_at) VALUES ($1, $2, $3, $4, $5, $6, $7)",
            node_id,
            organization_id,
            plant_id,
            f"ND-{secrets.token_hex(8).upper()}",
            status,
            url,
            BASE_TIME,
        )
        return node_id

    def assign_node(
        self,
        organization_id: uuid.UUID,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        node_id: uuid.UUID,
        at: datetime = BASE_TIME,
    ) -> uuid.UUID:
        assignment_id = uuid7()
        self.execute(
            "INSERT INTO identity.zone_node_assignment (assignment_id, organization_id, plant_id,"
            " zone_id, node_id, assigned_at, assigned_by) VALUES ($1, $2, $3, $4, $5, $6, $7)",
            assignment_id,
            organization_id,
            plant_id,
            zone_id,
            node_id,
            at,
            self.authz.operator_id,
        )
        return assignment_id

    def unassign(self, assignment_id: uuid.UUID) -> None:
        self.execute(
            "UPDATE identity.zone_node_assignment SET unassigned_at = $2 WHERE assignment_id = $1",
            assignment_id,
            BASE_TIME + timedelta(hours=1),
        )

    def user_with_role(
        self,
        organization_id: uuid.UUID,
        role: Role,
        level: ScopeLevel = ScopeLevel.ORGANIZATION,
        scope_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        user_id = self.authz.add_user(organization_id)
        self.authz.assign(organization_id, user_id, role, level, scope_id)
        return user_id

    def session_context(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> ScopeContext:
        cookie = self.authz.open_session(organization_id, user_id)
        scope = self.run(self.authz.contexts.context_from_session(cookie))
        context: ScopeContext = scope.context
        return context

    def concession_context(self, provider_user_id: uuid.UUID, concession_id: uuid.UUID) -> Any:
        cookie = self.authz.open_session(self.authz.provider_organization_id, provider_user_id)
        scope = self.run(
            self.authz.contexts.context_from_session(cookie, concession_id=concession_id)
        )
        return scope.context

    def node_context(self, organization_id: uuid.UUID) -> ScopeContext:
        """El contexto con que U-03 procesa el latido de un nodo de la organización."""
        return self.authz.contexts.anonymous(organization_id)


def _b64decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def jws_parts(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    """Cabecera y carga decodificadas y la firma de una JWS compacta (sin verificar)."""
    header, payload, signature = token.split(".")
    return json.loads(_b64decode(header)), json.loads(_b64decode(payload)), _b64decode(signature)


def node_verifies(
    token: str, node_id: uuid.UUID, key_set: KeySet, now: datetime, max_age: int = 600
) -> LiveViewToken | None:
    """El verificador sin red del nodo (U-01 §3.6, escenario A-11): los reclamos o ``None``.

    JWS compacta de tres partes, ``alg = EdDSA``, ``kid`` de una clave ``live_view_token``
    fijada y vigente, firma Ed25519 sobre ``cabecera.carga``, reclamos válidos en modo estricto,
    ``aud`` = este nodo, ``iat <= ahora < exp`` y ``exp - iat <= max_age``.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header = json.loads(_b64decode(parts[0]))
        if not isinstance(header, dict) or header.get("alg") != "EdDSA":
            return None
        key_id = header.get("kid")
        active = {key.key_id: key for key in key_set.active("live_view_token", now)}
        if not isinstance(key_id, str) or key_id not in active:
            return None
        public = Ed25519PublicKey.from_public_bytes(base64.b64decode(active[key_id].public_key))
        public.verify(_b64decode(parts[2]), f"{parts[0]}.{parts[1]}".encode("ascii"))
        claims = LiveViewToken.model_validate_json(_b64decode(parts[1]))
    except (ValueError, binascii.Error, InvalidSignature, ValidationError, UnicodeError):
        return None
    instant = int(now.timestamp())
    if (
        claims.aud != str(node_id)
        or not claims.iat <= instant < claims.exp
        or claims.exp - claims.iat > max_age
    ):
        return None
    return claims


@contextlib.contextmanager
def live_view_environment(
    endpoint: PostgresEndpoint, prefix: str, *, at_database_time: bool = False
) -> Iterator[LiveViewEnvironment]:
    """``authz_environment`` con un servicio de firma real y las cinco claves dadas de alta.

    Con ``at_database_time`` el reloj simulado arranca en el ``now()`` de la base **antes** del
    alta de las claves (VIG-135): las pruebas que comparan con la hora de la base (la RLS de las
    concesiones) firman con claves vigentes sea cual sea la fecha real. Sin él, las claves nacen en
    la hora simulada fija de ``session_support`` y viven ``KEY_LIFETIME``.
    """
    with authz_environment(endpoint, prefix) as authz:
        clock = authz.sessions.clock
        if at_database_time:
            (row,) = authz.fetch("SELECT now() AS now")
            clock.set(row["now"])
        secrets_port, store, events = InMemorySecrets(), InMemoryKeyStore(), RecordingEvents()
        bootstrap = build_service(clock, secrets_port, store, events)
        authz.run(bootstrap.start(required=()))
        for purpose in BOOTSTRAP_ORDER:
            authz.run(bootstrap.rotate(purpose, context=provider_context()))
        signing = build_service(clock, secrets_port, store, events)
        authz.run(signing.start())
        env = LiveViewEnvironment(authz, signing, secrets_port, store, events)
        try:
            yield env
        finally:
            for process in env.processes:
                authz.run(process.database.dispose())
