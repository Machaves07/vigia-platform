"""El almacén de adjuntos de cierre visto por U-04 y su cortacircuito (PAT-LAZ-RES-04).

``ClosureObjectStorage`` es lo único que ``loop.attachments`` usa de ``S3Storage`` sobre
``vigia-evidence``: metadatos, dos lecturas por rango acotadas y dos firmas. **No hay listado
de objetos** (BR-LAZ-45, PR-LAZ-60) ni escritura: la plataforma solo firma; los bytes los sube
el navegador.

``StoreCircuit`` reutiliza la máquina de estados pura ``ConsumerBreaker`` de U-02 tal cual
(PAT-NUC-RES-04), con su estado **en el proceso** (el circuito protege a esta instancia de un
almacén que no responde; no hay consumidor que pausar):

- cerrado: la llamada pasa; un ``StorageUnavailable`` abre el circuito;
- abierto: toda llamada responde ``StorageUnavailable`` **de inmediato y sin tocar el almacén**,
  hasta cumplirse el intervalo de sonda (60 s, del ``Clock`` inyectado);
- sonda: **una sola** llamada pasa (``half_open``); las demás siguen sin tocar el almacén. Si
  responde, cierra; si vuelve a fallar, reabre.

Con el circuito abierto la operación lo **declara** (``storage_unavailable``) y el cierre sigue
siendo posible con la descripción estructurada: verificar cero adjuntos no llama al almacén
(FS-LAZ-03). El estado del circuito no entra en ``/health/ready``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from typing import Protocol

from vigia_platform.shared.clock import Clock
from vigia_platform.shared.outbox.breaker import (
    BreakerAction,
    BreakerState,
    CircuitState,
    ConsumerBreaker,
)
from vigia_platform.shared.storage import ObjectHead, PresignedRequest, StorageUnavailable

__all__ = ["CIRCUIT_OPERATION", "ClosureObjectStorage", "StoreCircuit"]

CIRCUIT_OPERATION = "closure_attachment_circuit_open"
"""``StorageUnavailable.operation`` cuando responde el circuito, no el almacén."""


class ClosureObjectStorage(Protocol):
    """Las operaciones de ``S3Storage`` que usa ``loop.attachments``; ninguna lista ni escribe."""

    async def head_object(self, key: str) -> ObjectHead | None: ...

    async def get_object_prefix(
        self, key: str, length: int, *, version_id: str | None = None
    ) -> bytes: ...

    async def get_object_suffix(
        self, key: str, length: int, *, version_id: str | None = None
    ) -> bytes: ...

    async def presign_put(
        self,
        key: str,
        content_type: str,
        checksum_sha256: str,
        required_headers: Mapping[str, str],
        ttl: timedelta = ...,
    ) -> PresignedRequest: ...

    async def presign_attachment_get(
        self, key: str, ttl: timedelta = ..., *, version_id: str | None = None
    ) -> PresignedRequest: ...


class StoreCircuit:
    """Cortacircuito en proceso sobre el almacén de adjuntos (``ConsumerBreaker`` de U-02)."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._state = BreakerState.closed()

    @property
    def state(self) -> CircuitState:
        return self._state.state

    async def call[T](self, work: Callable[[], Awaitable[T]]) -> T:
        """``work()`` si el circuito lo deja; si no, ``StorageUnavailable`` sin llamar a nada."""
        now = self._clock.now()
        action = ConsumerBreaker.tick(self._state, now)
        if action is BreakerAction.PAUSE:
            raise StorageUnavailable(CIRCUIT_OPERATION)
        probe = action is BreakerAction.PROBE
        if probe:
            self._state = ConsumerBreaker.on_probe(self._state, now)
        try:
            result = await work()
        except StorageUnavailable:
            self._state = ConsumerBreaker.on_dependency_failure(
                self._state, self._clock.now(), probe=probe
            )
            raise
        except Exception:
            # El almacén respondió y el fallo es de la petición: cuenta como respuesta.
            self._state = ConsumerBreaker.on_handler_defect(self._state, probe=probe)
            raise
        self._state = ConsumerBreaker.on_success(self._state, probe=probe)
        return result
