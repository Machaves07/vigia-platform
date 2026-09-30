"""``SigningService``: el único servicio de firma de la plataforma (LC-NUC-25; BR-NUC-84 a 87).

Implementa ``SigningPort`` (``business-logic-model.md`` §10.1) y la rotación de §9:

- ``sign(purpose, payload)`` firma con la clave ``active`` del propósito, con ``canonical`` y
  ``signing`` de U-01: ``catalog`` y ``gate`` devuelven el ``SignedEnvelope`` del contrato
  (``sign`` de U-01, que valida la carga con el lector estricto); ``checkpoint`` un
  ``PlatformSignedEnvelope`` con la misma forma. ``live_view_token`` viaja en una JWS compacta
  (U-01 §3.6), así que se firma con ``sign_detached`` sobre la entrada de firma de la JWS.
  ``key_set`` no se firma por el puerto: solo la rotación publica conjuntos.
- ``public_keys(purpose)``: activas y en solapamiento (``checkpoint``: todas, también retiradas).
- ``current_key_set_envelope()``: la última ``KeySetPublication``.
- ``rotate(purpose, context=...)``: clave nueva en el gestor de secretos, anterior
  ``overlapping``, ``key_rotated`` en la cadena de la organización proveedora y, para los cuatro
  propósitos del nodo, ``KeySetPublication`` firmada por la ``key_set`` que el nodo ya tiene
  fijada, más ``key_set_published`` (con su evento de la bandeja). La rotación y el retiro
  releen la base con el candado tomado: nunca calculan desde una memoria que otro proceso
  (API o worker) dejó atrás, y el almacén rechaza con ``KeyStateConflict`` una confirmación
  calculada desde un estado ya superado.
- ``retire_expired()``, ``reminder_decision(now)`` y ``report_days_to_expiry(now)``: lo que usa la
  tarea ``key_rotation_reminder`` (``shared.key_rotation``).

**Material privado** (NFR-NUC-27, PAT-NUC-SEG-04): el par Ed25519 se genera en el proceso, sus
32 bytes van **solo** al gestor de secretos (un secreto por propósito y versión,
``vigia/<entorno>/signing/<purpose>/<key_id>``) y a la memoria. La base guarda la referencia y la
clave pública (``SigningKeyStore``); ningún ``repr``, registro, evento, registro del expediente ni
respuesta lleva material privado. En memoria solo se conservan las claves ``active``.

**Arranque cerrado** (PAT-NUC-RES-02, FS-NUC-05): ``start()`` carga las claves y el material de
cada ``active``; si falta una clave requerida, el material no se puede leer (gestor de secretos
inaccesible, secreto inexistente) o no corresponde a su clave pública, lanza
``SigningStartupError`` y el servicio no queda listo (``ready`` falso). **En operación**,
``refresh()`` (cada 5 minutos, ``run_refresh``) relee claves y secretos; si falla, se sigue
firmando con lo que hay en memoria y se suma 1 a ``secrets_refresh_failed``. La rotación sin
gestor de secretos responde ``SecretsUnavailable`` (``temporarily_unavailable``) y no cambia nada.

Módulo crítico aislado (NFR-NUC-25): no importa FastAPI ni SQLAlchemy; la base y el expediente
llegan por los puertos ``SigningKeyStore`` y ``KeyEventWriter``. No lee la hora del sistema.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from vigia_contracts.canonical import canonical_sha256
from vigia_contracts.models.gate_state import SignedEnvelope as SignedGateState
from vigia_contracts.models.public_key_set import SignedEnvelope as SignedPublicKeySet
from vigia_contracts.models.zone_catalog import SignedEnvelope as SignedZoneCatalog
from vigia_contracts.signing import public_key_base64, sign_bytes, signing_key_from_bytes
from vigia_contracts.signing import sign as contract_sign

from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics
from vigia_platform.shared.secrets import (
    Dependency,
    SecretNotFound,
    SecretsPort,
    SecretsUnavailable,
)
from vigia_platform.shared.signing.keys import (
    KEY_LIFETIME,
    KeySetPublicationRecord,
    KeySetSignerUnavailable,
    KeyStatus,
    KeyTransition,
    PlatformSignedEnvelope,
    ReminderDecision,
    SigningKeyRecord,
    SigningPurpose,
    active_key,
    apply_transitions,
    check_invariants,
    days_to_expiry,
    expiry_transitions,
    format_timestamp,
    node_key_set,
    publication_plan,
    published_keys,
    reminder_decision,
    rotation_transitions,
    to_millisecond,
)

__all__ = [
    "REFRESH_INTERVAL_SECONDS",
    "DetachedSignature",
    "KeyEventWriter",
    "KeyStoreSnapshot",
    "RotationCommit",
    "RotationResult",
    "SigningKeyStore",
    "SigningKeyUnavailable",
    "SigningNotReady",
    "SigningService",
    "SigningStartupError",
    "SigningStateError",
    "secret_name",
]

REFRESH_INTERVAL_SECONDS: Final = 300.0
"""Refresco de claves y secretos cada 5 minutos (LC-NUC-25)."""
_PRIVATE_KEY_BYTES: Final = 32
_MILLISECOND: Final = timedelta(milliseconds=1)
_ENVIRONMENT: Final = re.compile(r"[a-z][a-z0-9-]{0,31}")
"""Entorno del nombre del secreto (``pilot``, ``staging-<n>``): ASCII, sin rutas ni espacios."""

_log = get_logger("shared.signing")

type ContractEnvelope = SignedZoneCatalog | SignedGateState | SignedPublicKeySet


# --- Errores -----------------------------------------------------------------------------------


class SigningStartupError(Exception):
    """El servicio no pudo cargar sus claves al arrancar: el proceso no queda ``ready``.

    ``causes`` son códigos cerrados (``store_unavailable``, ``missing_active_key``,
    ``secret_unavailable``, ``secret_not_found``, ``key_mismatch``); nunca un ARN ni material.
    """

    def __init__(self, causes: Iterable[str]) -> None:
        self.causes = tuple(causes)
        super().__init__(
            "el servicio de firma no pudo cargar sus claves: " + ", ".join(self.causes)
        )


class SigningStateError(Exception):
    """El estado confirmado de las claves no permite rotar (claves incoherentes o material que
    falta o no corresponde); ``causes`` como en ``SigningStartupError``."""

    def __init__(self, causes: Iterable[str]) -> None:
        self.causes = tuple(causes)
        super().__init__("estado de claves de firma no utilizable: " + ", ".join(self.causes))


class SigningNotReady(Exception):
    """Se pidió una firma o una rotación antes de un ``start()`` correcto."""

    def __init__(self) -> None:
        super().__init__("el servicio de firma no está listo")


class SigningKeyUnavailable(Exception):
    """No hay clave ``active`` vigente con material en memoria para el propósito: no se firma."""

    def __init__(self, purpose: SigningPurpose) -> None:
        super().__init__(f"sin clave de firma vigente para {purpose.value}")
        self.purpose = purpose


# --- Puertos -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class KeyStoreSnapshot:
    """Estado persistido: las claves de la organización proveedora y la última publicación."""

    keys: tuple[SigningKeyRecord, ...]
    publication: KeySetPublicationRecord | None


@dataclass(frozen=True, slots=True, kw_only=True)
class RotationCommit:
    """Lo que una rotación persiste en **una** transacción (``identity.signing_key`` y
    ``identity.key_set_publication``)."""

    new_key: SigningKeyRecord
    transitions: tuple[KeyTransition, ...]
    publication: KeySetPublicationRecord | None


class SigningKeyStore(Protocol):
    """Persistencia de ``SigningKey`` y ``KeySetPublication`` de la organización proveedora.

    El adaptador garantiza la unicidad de la base (una ``active`` y una ``overlapping`` por
    propósito, ``key_id`` único) y que ``commit_rotation`` no deja nada a medias. Cada
    ``KeyTransition`` se aplica solo si la clave sigue en ``expected_status`` (``UPDATE … WHERE
    key_id = :id AND status = :expected`` que afecte exactamente una fila); si no, revierte todo
    y lanza ``KeyStateConflict``: dos procesos nunca confirman desde el mismo estado superado.
    """

    async def load(self) -> KeyStoreSnapshot: ...

    async def commit_rotation(self, commit: RotationCommit) -> None: ...

    async def commit_transitions(self, transitions: Sequence[KeyTransition]) -> None: ...


class KeyEventWriter(Protocol):
    """Escritura de ``key_rotated`` y ``key_set_published`` en la cadena de la organización
    proveedora (``EscritorExpediente``); ``shared.key_rotation`` tiene el adaptador."""

    async def key_rotated(self, context: ScopeContext, content: Mapping[str, Any]) -> None: ...

    async def key_set_published(
        self, context: ScopeContext, content: Mapping[str, Any], event: Mapping[str, Any]
    ) -> None: ...


# --- Resultados --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DetachedSignature:
    """Firma Ed25519 de unos bytes (la entrada de una JWS) y la clave que la produjo."""

    key_id: str
    signature: bytes


@dataclass(frozen=True, slots=True)
class RotationResult:
    new_key: SigningKeyRecord
    previous_key_id: str | None
    publication: KeySetPublicationRecord | None


def secret_name(environment: str, purpose: SigningPurpose, key_id: str) -> str:
    """Nombre del secreto de una versión de clave (``infrastructure-design.md`` §7.2)."""
    return f"vigia/{environment}/signing/{purpose.value}/{key_id}"


# --- Servicio ----------------------------------------------------------------------------------


@repository
class SigningService:
    """``SigningPort`` con un par Ed25519 activo por propósito, cargado en memoria."""

    def __init__(
        self,
        *,
        provider_organization_id: uuid.UUID,
        store: SigningKeyStore,
        secrets: SecretsPort,
        events: KeyEventWriter,
        clock: Clock,
        environment: str,
        metrics: PlatformMetrics | None = None,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        if not isinstance(provider_organization_id, uuid.UUID):
            raise TypeError("provider_organization_id debe ser un UUID")
        if not isinstance(environment, str) or _ENVIRONMENT.fullmatch(environment) is None:
            raise ValueError("environment debe ser un nombre en minúsculas (pilot, staging-1…)")
        self._organization_id = provider_organization_id
        self._store = store
        self._secrets = secrets
        self._events = events
        self._clock = clock
        self._environment = environment
        self._metrics = metrics
        self._random_bytes = random_bytes
        self._keys: dict[str, SigningKeyRecord] = {}
        self._private: dict[str, Ed25519PrivateKey] = {}
        self._publication: KeySetPublicationRecord | None = None
        self._ready = False
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"SigningService(ready={self._ready}, keys={sorted(self._keys)!r})"

    # --- arranque y refresco -----------------------------------------------------------------

    @property
    def ready(self) -> bool:
        return self._ready

    async def start(self, *, required: Iterable[SigningPurpose] = tuple(SigningPurpose)) -> None:
        """Carga claves y material; fallo cerrado. ``required=()`` solo para el alta inicial."""
        wanted = tuple(required)
        try:
            snapshot = await self._store.load()
        except Exception as error:
            _log.error("arranque sin acceso a las claves de firma")
            raise SigningStartupError(["store_unavailable"]) from error
        try:
            check_invariants(snapshot.keys)
        except ValueError:
            _log.error("arranque con claves de firma incoherentes: el proceso no queda listo")
            raise SigningStartupError(["invalid_key_state"]) from None
        causes: list[str] = [
            "missing_active_key" for purpose in wanted if active_key(snapshot.keys, purpose) is None
        ]
        private, failures = await self._load_private(snapshot.keys, {})
        causes += failures
        if causes:
            _log.error("arranque del servicio de firma fallido: el proceso no queda listo")
            raise SigningStartupError(causes)
        self._install(snapshot, private)
        self._ready = True

    async def refresh(self) -> bool:
        """Relee claves y material; si algo falla, conserva lo que hay en memoria.

        Devuelve ``True`` si el estado se renovó. Un fallo del gestor de secretos suma 1 a
        ``secrets_refresh_failed``.
        """
        if not self._ready:
            raise SigningNotReady
        # Todo bajo el candado: un refresco que leyó la base antes de una rotación de este mismo
        # proceso no puede reinstalar después el estado anterior.
        async with self._lock:
            try:
                snapshot = await self._store.load()
                check_invariants(snapshot.keys)
            except Exception:
                _log.warning("refresco de claves de firma fallido; se usan las de memoria")
                return False
            private, failures = await self._load_private(snapshot.keys, self._private)
            if failures:
                self._refresh_failed()
                return False
            self._install(snapshot, private)
            await self._probe_secrets(snapshot.keys)
        return True

    async def _probe_secrets(self, keys: Iterable[SigningKeyRecord]) -> None:
        """Relee del gestor el material de las activas que ya está en memoria (FS-NUC-05 b).

        No cambia nada: con el gestor caído se sigue firmando con lo de memoria, pero la caída se
        cuenta en ``secrets_refresh_failed`` aunque el proceso no rote. El adaptador sirve su
        caché y cuenta él mismo cuando responde con el último valor.
        """
        refs = [k.private_key_ref for k in keys if k.status is KeyStatus.ACTIVE]
        results = await asyncio.gather(
            *(self._secrets.get(ref) for ref in refs), return_exceptions=True
        )
        failed = [r for r in results if isinstance(r, BaseException)]
        for result in failed:
            if not isinstance(result, SecretsUnavailable | SecretNotFound | ValueError):
                raise result
        if failed:
            self._refresh_failed()

    async def _reload_locked(self) -> None:
        """Relee claves, publicación y material de la base con el candado ya tomado.

        Rotar o retirar parte siempre del estado confirmado, nunca de la memoria del proceso, que
        puede llevar hasta 5 minutos de retraso si otro proceso (API o worker) rotó. Una base
        caída sube tal cual; el gestor caído, como ``SecretsUnavailable``.
        """
        snapshot = await self._store.load()
        try:
            check_invariants(snapshot.keys)
        except ValueError:
            raise SigningStateError(["invalid_key_state"]) from None
        private, failures = await self._load_private(snapshot.keys, self._private)
        if "secret_unavailable" in failures:
            raise SecretsUnavailable(Dependency.SECRETS_MANAGER, "load_signing_keys")
        if failures:
            raise SigningStateError(failures)
        self._install(snapshot, private)

    async def run_refresh(
        self, stop: asyncio.Event, *, interval_seconds: float = REFRESH_INTERVAL_SECONDS
    ) -> None:
        """Bucle de refresco cada ``interval_seconds`` hasta que ``stop`` se active."""
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), interval_seconds)
            if not stop.is_set():
                await self.refresh()

    async def _load_private(
        self,
        keys: Iterable[SigningKeyRecord],
        known: Mapping[str, Ed25519PrivateKey],
    ) -> tuple[dict[str, Ed25519PrivateKey], list[str]]:
        """Material de cada clave ``active``: de ``known`` si ya está, si no del gestor.

        Las lecturas al gestor van en paralelo: con el gestor sin responder, el arranque falla en
        un solo tope de llamada (5 s), no en uno por clave.
        """
        loaded: dict[str, Ed25519PrivateKey] = {}
        failures: list[str] = []
        active = [k for k in keys if k.status is KeyStatus.ACTIVE]
        pending = [k for k in active if k.key_id not in known]
        loaded.update((k.key_id, known[k.key_id]) for k in active if k.key_id in known)
        results = await asyncio.gather(
            *(self._secrets.get(key.private_key_ref) for key in pending), return_exceptions=True
        )
        for key, raw in zip(pending, results, strict=True):
            if isinstance(raw, SecretsUnavailable):
                failures.append("secret_unavailable")
                continue
            if isinstance(raw, SecretNotFound | ValueError):
                failures.append("secret_not_found")
                continue
            if isinstance(raw, BaseException):
                raise raw
            private = _private_key(raw)
            if private is None or public_key_base64(private) != key.public_key:
                failures.append("key_mismatch")
                continue
            loaded[key.key_id] = private
        return loaded, failures

    def _install(
        self, snapshot: KeyStoreSnapshot, private: Mapping[str, Ed25519PrivateKey]
    ) -> None:
        self._keys = {key.key_id: key for key in snapshot.keys}
        self._private = dict(private)
        self._publication = snapshot.publication

    def _refresh_failed(self) -> None:
        metrics = self._metrics if self._metrics is not None else get_metrics()
        metrics.secrets_refresh_failed.add(1, {"dependency": Dependency.SECRETS_MANAGER.value})
        _log.warning("material de claves no disponible; se firma con el de memoria")

    # --- SigningPort ---------------------------------------------------------------------------

    def sign(
        self, purpose: SigningPurpose, payload: Any
    ) -> ContractEnvelope | PlatformSignedEnvelope:
        """Sobre firmado de ``payload`` con la clave ``active`` vigente de ``purpose``."""
        if purpose is SigningPurpose.LIVE_VIEW_TOKEN:
            raise ValueError("live_view_token se firma como JWS compacta: usa sign_detached")
        if purpose is SigningPurpose.KEY_SET:
            # Un conjunto de claves solo sale de rotate(), con su key_set_published (BR-NUC-86):
            # nadie firma por el puerto un PublicKeySet que los nodos aceptarían.
            raise ValueError("key_set solo lo firma la rotación: usa current_key_set_envelope")
        key_id, private = self._signer(purpose)
        if purpose is SigningPurpose.CHECKPOINT:
            return PlatformSignedEnvelope(
                payload=payload,
                payload_canonical_sha256=canonical_sha256(payload),
                signature=sign_bytes(private, payload),
                key_id=key_id,
                signed_at=format_timestamp(self._clock.now()),
            )
        return contract_sign(payload, private, key_id, purpose.value, self._clock)

    def sign_detached(self, purpose: SigningPurpose, message: bytes) -> DetachedSignature:
        """Firma Ed25519 de ``message`` tal cual: la entrada de firma de una JWS (``EdDSA``).

        Solo ``live_view_token``: el resto de propósitos firma la forma canónica de su carga.
        """
        if purpose is not SigningPurpose.LIVE_VIEW_TOKEN:
            raise ValueError("solo live_view_token firma bytes sin forma canónica")
        if not isinstance(message, bytes) or not message:
            raise ValueError("el mensaje debe ser bytes no vacíos")
        key_id, private = self._signer(purpose)
        return DetachedSignature(key_id=key_id, signature=private.sign(message))

    def public_keys(self, purpose: SigningPurpose) -> tuple[SigningKeyRecord, ...]:
        """Claves publicadas de ``purpose``: activas y en solapamiento; ``checkpoint``, todas."""
        self._require_ready()
        return published_keys(self._keys.values(), SigningPurpose(purpose))

    def current_key_set_envelope(self) -> SignedPublicKeySet | None:
        """``SignedEnvelope<PublicKeySet>`` de la última publicación, o ``None`` sin ninguna."""
        self._require_ready()
        return None if self._publication is None else self._publication.envelope

    def current_publication(self) -> KeySetPublicationRecord | None:
        self._require_ready()
        return self._publication

    def _signer(self, purpose: SigningPurpose) -> tuple[str, Ed25519PrivateKey]:
        self._require_ready()
        purpose = SigningPurpose(purpose)
        key = active_key(self._keys.values(), purpose)
        now = self._clock.now()
        private = None if key is None else self._private.get(key.key_id)
        if key is None or private is None or not key.is_valid_at(now):
            raise SigningKeyUnavailable(purpose)
        return key.key_id, private

    def _require_ready(self) -> None:
        if not self._ready:
            raise SigningNotReady

    def has_active_key(self, purpose: SigningPurpose) -> bool:
        """Hay clave ``active`` vigente con su material en memoria (``/health/ready``, NFR-NUC-13).

        No llama al gestor de secretos: con el gestor caído en operación se sigue firmando con lo
        que hay en memoria (FS-NUC-05 b), así que la salud tampoco depende de él.
        """
        try:
            self._signer(purpose)
        except (SigningNotReady, SigningKeyUnavailable):
            return False
        return True

    # --- rotación ------------------------------------------------------------------------------

    async def rotate(self, purpose: SigningPurpose, *, context: ScopeContext) -> RotationResult:
        """Rota ``purpose`` (BR-NUC-85 y 86). ``context``: de la organización proveedora."""
        self._require_ready()
        purpose = SigningPurpose(purpose)
        if (
            not isinstance(context, ScopeContext)
            or context.organization_id != self._organization_id
        ):
            raise PermissionError(
                "la rotación se escribe en la cadena de la organización proveedora"
            )
        async with self._lock:
            await self._reload_locked()
            result = await self._rotate(purpose, context)
        await self._write_events(context, result)
        return result

    async def _rotate(self, purpose: SigningPurpose, context: ScopeContext) -> RotationResult:
        now = to_millisecond(self._clock.now())
        previous = active_key(self._keys.values(), purpose)
        # ``issued_at`` estrictamente creciente: el nodo rechaza un conjunto con el mismo
        # ``issued_at`` y otro contenido (``KeySet.apply_signed_set``).
        issued_at = now
        if self._publication is not None and issued_at <= self._publication.issued_at:
            issued_at = self._publication.issued_at + _MILLISECOND
        # Antes de crear nada: sin una key_set vigente no se rota otro propósito del nodo.
        try:
            plan = publication_plan(self._keys.values(), purpose, issued_at)
        except KeySetSignerUnavailable:
            _log.warning("rotación rechazada: la clave key_set activa no está vigente")
            raise SigningKeyUnavailable(SigningPurpose.KEY_SET) from None
        key_id = self._new_key_id(purpose, now)
        raw = self._random_bytes(_PRIVATE_KEY_BYTES)
        private = _private_key(raw)
        if private is None:
            raise ValueError("el generador no dio 32 bytes para la clave")
        # El material va al gestor antes que a la base: sin secreto no hay clave (arranque).
        reference = await self._secrets.create(secret_name(self._environment, purpose, key_id), raw)
        new_key = SigningKeyRecord(
            key_id=key_id,
            purpose=purpose,
            public_key=public_key_base64(private),
            private_key_ref=reference,
            valid_from=now,
            valid_until=now + KEY_LIFETIME,
            status=KeyStatus.ACTIVE,
            created_at=now,
            rotated_by=context.actor.id,
        )
        transitions = rotation_transitions(self._keys.values(), purpose, now)
        keys_after = apply_transitions(self._keys, transitions, new_key)
        private_after = {
            k: v for k, v in self._private.items() if keys_after[k].status is KeyStatus.ACTIVE
        }
        private_after[key_id] = private
        previous_id = None if previous is None else previous.key_id
        publication = None
        if plan is not None:
            signer_id = key_id if plan.signer_key_id is None else plan.signer_key_id
            signer = private if plan.signer_key_id is None else self._private.get(signer_id)
            if signer is None:
                raise SigningKeyUnavailable(SigningPurpose.KEY_SET)
            publication = self._publication_record(
                keys_after.values(), signer_id, signer, issued_at
            )
        await self._store.commit_rotation(
            RotationCommit(new_key=new_key, transitions=transitions, publication=publication)
        )
        self._keys = keys_after
        self._private = private_after
        if publication is not None:
            self._publication = publication
        _log.info("clave de firma rotada")
        return RotationResult(new_key=new_key, previous_key_id=previous_id, publication=publication)

    def _new_key_id(self, purpose: SigningPurpose, now: datetime) -> str:
        suffix = self._random_bytes(4).hex()
        key_id = f"{purpose.value}-{now:%Y%m%d%H%M%S}-{suffix}"
        if key_id in self._keys:
            raise ValueError("colisión de key_id: un identificador nunca se reutiliza")
        return key_id

    def _publication_record(
        self,
        keys: Iterable[SigningKeyRecord],
        signer_id: str,
        signer: Ed25519PrivateKey,
        issued_at: datetime,
    ) -> KeySetPublicationRecord:
        contract_keys = tuple(key.to_contract() for key in node_key_set(keys))
        payload = {
            "keys": [dict(key) for key in contract_keys],
            "issued_at": format_timestamp(issued_at),
        }
        # ``signed_at`` = ``issued_at``: el instante en que se comprobó la vigencia del firmante.
        envelope = contract_sign(
            payload, signer, signer_id, SigningPurpose.KEY_SET.value, _FixedClock(issued_at)
        )
        if not isinstance(envelope, SignedPublicKeySet):  # pragma: no cover - lo fija U-01
            raise TypeError("el sobre del conjunto no es SignedEnvelope<PublicKeySet>")
        return KeySetPublicationRecord(
            publication_id=uuid7(self._clock, self._random_bytes),
            issued_at=issued_at,
            keys=contract_keys,
            signed_by_key_id=signer_id,
            envelope=envelope,
        )

    async def _write_events(self, context: ScopeContext, result: RotationResult) -> None:
        key = result.new_key
        rotated: dict[str, Any] = {
            "key_id": key.key_id,
            "purpose": key.purpose.value,
            "public_key": key.public_key,
            "valid_from": format_timestamp(key.valid_from),
            "rotated_by": str(key.rotated_by),
        }
        if result.previous_key_id is not None:
            rotated["previous_key_id"] = result.previous_key_id
        await self._events.key_rotated(context, rotated)
        publication = result.publication
        if publication is None:
            return
        issued_at = format_timestamp(publication.issued_at)
        await self._events.key_set_published(
            context,
            {
                "publication_id": str(publication.publication_id),
                "issued_at": issued_at,
                "signed_by_key_id": publication.signed_by_key_id,
                "key_ids": list(publication.key_ids),
            },
            {
                "publication_id": str(publication.publication_id),
                "signing_key_id": publication.signed_by_key_id,
                "published_at": issued_at,
            },
        )

    async def retire_expired(self) -> tuple[KeyTransition, ...]:
        """``overlapping`` → ``retired`` para las claves vencidas; persiste y devuelve el cambio."""
        self._require_ready()
        async with self._lock:
            await self._reload_locked()
            transitions = expiry_transitions(self._keys.values(), self._clock.now())
            if transitions:
                await self._store.commit_transitions(transitions)
                self._keys = apply_transitions(self._keys, transitions)
        return transitions

    # --- tarea key_rotation_reminder -----------------------------------------------------------

    def reminder_decision(self, now: datetime, *, period: timedelta) -> ReminderDecision:
        self._require_ready()
        return reminder_decision(self._keys.values(), now, period=period)

    def report_days_to_expiry(self, now: datetime) -> dict[SigningPurpose, int]:
        """Publica ``signing_key_days_to_expiry`` por propósito y devuelve los valores."""
        self._require_ready()
        values = days_to_expiry(self._keys.values(), now)
        metrics = self._metrics if self._metrics is not None else get_metrics()
        for purpose, days in values.items():
            metrics.signing_key_days_to_expiry.set(days, {"purpose": purpose.value})
        return values

    def all_keys(self) -> tuple[SigningKeyRecord, ...]:
        """Todas las claves conocidas (parte pública), ordenadas por ``key_id``."""
        self._require_ready()
        return tuple(self._keys[key_id] for key_id in sorted(self._keys))


class _FixedClock:
    """Reloj detenido en un instante: el ``signed_at`` de un sobre que se firma en ``moment``."""

    def __init__(self, moment: datetime) -> None:
        self._moment = moment

    def now(self) -> datetime:
        return self._moment

    def monotonic(self) -> float:  # pragma: no cover - U-01 solo usa now() al firmar
        raise NotImplementedError

    def sleep(self, seconds: float) -> None:  # pragma: no cover - U-01 solo usa now() al firmar
        raise NotImplementedError


def _private_key(raw: object) -> Ed25519PrivateKey | None:
    if not isinstance(raw, bytes) or len(raw) != _PRIVATE_KEY_BYTES:
        return None
    return signing_key_from_bytes(raw)
