"""Generador de carga del arnés: hallazgos del kit de U-01 escritos por ``EscritorExpediente``.

``kit_findings(seed, place, count)`` saca ``count`` hallazgos de
``vigia_contracts.conformance.generators.finding`` (con un ``zone_catalog`` también del kit) de
forma **determinista por semilla** y los lleva al lugar (``writer_support.localize_finding``).
``SimulatedNode`` es un nodo simulado con su cola local: envía cada registro por el escritor y,
ante un fallo transitorio (``temporarily_unavailable``, ``chain_locked_timeout`` o almacén caído),
lo **reencola** y reintenta con retroceso, como el nodo de U-01 (H-13); un ``accepted_duplicate``
cuenta como entregado (la idempotencia por ``source_key`` absorbe el reintento, BR-NUC-48).

La variante con nodos que envían por HTTP la activa U-03 cuando existan las rutas del contrato.
Solo datos generados.
"""

from __future__ import annotations

import asyncio
import random
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from hypothesis import HealthCheck, Phase, given, settings
from hypothesis import seed as hypothesis_seed
from hypothesis import strategies as st
from vigia_contracts.conformance.generators import finding, zone_catalog
from vigia_contracts.models.enumerations import AcceptanceStatus

from tests.writer_support import FINDING_TYPE, Place, clips_of, localize_finding
from vigia_platform.ledger.application.writer import (
    LedgerRejection,
    LedgerRejectionCode,
    Receipt,
)
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.db import TemporarilyUnavailable
from vigia_platform.shared.storage import StorageUnavailable

__all__ = ["NodeReport", "SimulatedNode", "draw_example", "kit_findings"]

_DRAWS: Final = 2
"""Ejemplos por extracción: el primero de Hypothesis es siempre el mínimo; se usa el último, que
depende de la semilla."""


def draw_example[T](strategy: st.SearchStrategy[T], seed: int) -> T:
    """Un ejemplo de ``strategy`` que solo depende de ``seed`` (sin base de ejemplos)."""
    drawn: list[T] = []

    @settings(
        max_examples=_DRAWS,
        database=None,
        phases=[Phase.generate],
        suppress_health_check=list(HealthCheck),
        deadline=None,
    )
    @given(strategy)
    @hypothesis_seed(seed)
    def collect(value: T) -> None:
        drawn.append(value)

    collect()
    return drawn[-1]


def kit_findings(seed: int, place: Place, count: int) -> list[dict[str, Any]]:
    """``count`` hallazgos del kit, coherentes con un catálogo del kit, llevados a ``place``."""
    strategy = zone_catalog().flatmap(
        lambda catalog: st.lists(finding(catalog), min_size=count, max_size=count)
    )
    return [localize_finding(document, place) for document in draw_example(strategy, seed)]


TRANSIENT_REJECTIONS: Final = frozenset({LedgerRejectionCode.CHAIN_LOCKED_TIMEOUT})


@dataclass
class NodeReport:
    """Lo que vio un nodo simulado: envíos, reintentos y resultado de cada registro."""

    sent: int = 0
    retries: Counter[str] = field(default_factory=Counter)
    accepted: dict[str, Receipt] = field(default_factory=dict)
    """``finding_id`` → recibo final (``accepted`` o ``accepted_duplicate``)."""
    rejected: dict[str, str] = field(default_factory=dict)
    """``finding_id`` → código de un rechazo permanente (no debe haber ninguno)."""


@dataclass
class SimulatedNode:
    """Un nodo con su cola local que envía por ``write`` y reencola lo transitorio."""

    name: str
    context: ScopeContext
    queue: deque[dict[str, Any]]
    write: Callable[[ScopeContext, str, Mapping[str, Any]], Awaitable[Receipt | LedgerRejection]]
    rng: random.Random
    backoff: tuple[float, float] = (0.05, 0.5)
    max_attempts: int = 400
    report: NodeReport = field(default_factory=NodeReport)

    async def drain(self) -> NodeReport:
        """Envía hasta vaciar la cola; un registro que agota los intentos queda sin entregar."""
        attempts: Counter[str] = Counter()
        while self.queue:
            document = self.queue.popleft()
            key = str(document["finding_id"])
            attempts[key] += 1
            self.report.sent += 1
            try:
                outcome = await self.write(self.context, FINDING_TYPE, document)
            except (TemporarilyUnavailable, StorageUnavailable) as error:
                outcome = None
                self.report.retries[type(error).__name__] += 1
            if isinstance(outcome, Receipt):
                self.report.accepted[key] = outcome
                continue
            if isinstance(outcome, LedgerRejection):
                if outcome.code not in TRANSIENT_REJECTIONS:
                    self.report.rejected[key] = outcome.code.value
                    continue
                self.report.retries[outcome.code.value] += 1
            if attempts[key] >= self.max_attempts:
                self.report.rejected[key] = "attempts_exhausted"
                continue
            self.queue.append(document)  # reencolado: vuelve al final de la cola local
            await asyncio.sleep(self.rng.uniform(*self.backoff))
        return self.report


def duplicates(statuses: Sequence[AcceptanceStatus]) -> int:
    return sum(1 for status in statuses if status is AcceptanceStatus.ACCEPTED_DUPLICATE)


def all_clips(documents: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [clip for document in documents for clip in clips_of(document)]
