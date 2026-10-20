"""Caché por proceso de la lista de vocabulario (PAT-LAZ-REN-02; LC-LAZ-17; PR-LAZ-53).

Cada proceso guarda la **lista compilada** de cada organización con su ``version``:

- **La versión se comprueba al abrir la escritura** (``refresh``, el preparador que el escritor
  corre antes del paso 4), con una lectura de **una sola fila indexada** de
  ``loop.blocked_vocabulary_list`` y **nunca dentro del validador**. Si la versión cambió, o la
  entrada tiene más de ``TTL_SECONDS`` (60 s, ``[objetivo propio]``), se leen los términos de esa
  versión y se compila la lista **fuera del bucle de eventos** (``asyncio.to_thread``).
- ``invalidate`` es el refresco por ``vocabulary_list_changed``: tira la entrada y la siguiente
  escritura la vuelve a leer. La corrección no depende de que el evento llegue: la versión se
  mira en cada escritura y toda entrada caduca a los 60 s.
- ``compiled_for`` es lo que usa el validador: **solo memoria**. Sin entrada (la organización aún
  no escribió en este proceso, o el llamador no dio organización) es la lista inicial, el mínimo
  de BR-LAZ-86; una entrada caducada sigue valiendo hasta la próxima comprobación, porque nunca es
  más laxa que la inicial.
- **Nunca se retrocede de versión**: dos comprobaciones a la vez que leen versiones distintas
  dejan la más nueva, aunque la vieja termine de compilar después (prueba concurrente en
  ``tests/properties/laz/test_pr_laz_53_vocabulary_cache.py``).

``CompiledVocabulary.list_version`` es la versión de la entrada **realmente usada**, la que lleva
el veredicto (domain-entities §2.12, nota de NFR Design). Si la lista de la base no compila (un
patrón inválido), se conserva la anterior y se registra el fallo, sin el patrón.

Sin red y sin base: las lecturas las hace el ``VocabularyStore`` con el lector de filas de la
escritura (``FreeTextPreparation.rows``); el reloj es el del escritor.

``DeclaredNamesCache`` (TASK-340) guarda igual, por organización, el **vocabulario declarado** que
exime al patrón de nombre propio de ``person_attribution`` (los nombres de sus plantas y zonas y
los que la organización declara en su lista, TASK-350: equipos, máquinas, normas y alias):
``refresh`` es el preparador de ese validador, que relee los nombres al abrir una escritura si la
entrada tiene más de ``TTL_SECONDS`` o, con ``versions``, si la versión de la lista cambió;
``names_for`` es lo que usa el validador, solo memoria. Sin entrada se valida sin vocabulario
declarado (``NO_DECLARED_NAMES``), lo más estricto. Un nombre nuevo de la jerarquía tarda como
mucho ``TTL_SECONDS`` en eximir, y uno de la lista, hasta la siguiente escritura: hasta entonces un
texto que lo cite se rechaza (fallo cerrado), nunca al revés.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, Protocol

from vigia_platform.ledger.free_text import FreeTextPreparation, RowReader
from vigia_platform.loop.validators.attribution import (
    NO_DECLARED_NAMES,
    DeclaredNames,
    declared_names,
)
from vigia_platform.loop.validators.initial_list import INITIAL_LIST_VERSION
from vigia_platform.loop.validators.vocabulary import (
    CompiledVocabulary,
    VocabularyCompileError,
    VocabularyList,
    compile_vocabulary,
    initial_vocabulary,
)
from vigia_platform.shared.observability.logging import get_logger

__all__ = [
    "TTL_SECONDS",
    "DeclaredNamesCache",
    "DeclaredNamesStore",
    "ListHead",
    "VocabularyCache",
    "VocabularyStore",
]

TTL_SECONDS: Final = 60.0
"""Caducidad de una entrada aunque nadie la invalide (PAT-LAZ-REN-02, ``[objetivo propio]``)."""

_log = get_logger("loop.validators")

type Compiler = Callable[[VocabularyList], CompiledVocabulary]
type CpuRunner = Callable[[Compiler, VocabularyList], Awaitable[CompiledVocabulary]]


@dataclass(frozen=True)
class ListHead:
    """La fila de ``loop.blocked_vocabulary_list`` de una organización."""

    list_id: uuid.UUID
    version: int
    lemmatizer_version: str | None


class VocabularyStore(Protocol):
    """Lecturas de la lista; las hace el adaptador de PostgreSQL con el lector de la escritura."""

    async def head(self, organization_id: uuid.UUID, rows: RowReader) -> ListHead | None:
        """La fila de la lista (una sola, indexada), o ``None`` si la organización no tiene."""
        ...

    async def load(
        self, organization_id: uuid.UUID, head: ListHead, rows: RowReader
    ) -> VocabularyList:
        """Los términos vigentes en ``head.version``."""
        ...


@dataclass(frozen=True)
class _Entry:
    compiled: CompiledVocabulary
    loaded_at: float
    """``Clock.monotonic()`` del escritor al cargarla."""


async def _in_thread(compiler: Compiler, vocabulary: VocabularyList) -> CompiledVocabulary:
    return await asyncio.to_thread(compiler, vocabulary)


class VocabularyCache:
    """Listas compiladas por organización, en memoria del proceso."""

    def __init__(
        self,
        store: VocabularyStore,
        *,
        ttl_seconds: float = TTL_SECONDS,
        compiler: Compiler = compile_vocabulary,
        run_cpu: CpuRunner = _in_thread,
    ) -> None:
        if not ttl_seconds > 0:
            raise ValueError("ttl_seconds debe ser positivo")
        self._store = store
        self._ttl = ttl_seconds
        self._compiler = compiler
        self._run_cpu = run_cpu
        self._entries: dict[uuid.UUID, _Entry] = {}

    def compiled_for(self, organization_id: uuid.UUID | None) -> CompiledVocabulary:
        """La lista con la que se valida ahora: solo memoria, nunca red ni base."""
        entry = None if organization_id is None else self._entries.get(organization_id)
        return initial_vocabulary() if entry is None else entry.compiled

    def invalidate(self, organization_id: uuid.UUID) -> None:
        """Refresco por ``vocabulary_list_changed``: la próxima escritura relee la lista."""
        self._entries.pop(organization_id, None)

    async def refresh(self, preparation: FreeTextPreparation) -> None:
        """Comprueba la versión al abrir la escritura (el preparador de ``blocked_vocabulary``)."""
        organization = preparation.organization_id
        now = preparation.clock.monotonic()
        head = await self._store.head(organization, preparation.rows)
        if head is None:
            return  # sin fila: la lista inicial, versión 1
        entry = self._entries.get(organization)
        if (
            entry is not None
            and entry.compiled.list_version == head.version
            and now - entry.loaded_at < self._ttl
        ):
            return
        vocabulary = await self._store.load(organization, head, preparation.rows)
        try:
            compiled = await self._run_cpu(self._compiler, vocabulary)
        except VocabularyCompileError:
            _log.error("la lista de vocabulario no compila: se conserva la anterior")
            return
        current = self._entries.get(organization)
        if current is None or compiled.list_version >= current.compiled.list_version:
            self._entries[organization] = _Entry(compiled, now)


class DeclaredNamesStore(Protocol):
    """Lectura del vocabulario declarado; la hace el adaptador de PostgreSQL con el lector de la
    escritura."""

    async def names(self, organization_id: uuid.UUID, rows: RowReader) -> tuple[str, ...]:
        """Los nombres de las plantas y zonas de la organización que ve la escritura."""
        ...


@dataclass(frozen=True)
class _NamesEntry:
    names: DeclaredNames
    loaded_at: float
    list_version: int = INITIAL_LIST_VERSION
    """La versión de la lista de vocabulario con la que se leyeron (TASK-350)."""


class DeclaredNamesCache:
    """Vocabulario declarado por organización, en memoria del proceso (``person_attribution``).

    Con ``versions`` (el ``VocabularyStore`` de la lista, TASK-350), ``refresh`` comprueba además
    la versión de la lista al abrir la escritura, con la misma lectura de una fila que
    ``VocabularyCache``: los nombres que la organización declara son parte de la lista, así que un
    cambio se aplica en la siguiente escritura de cualquier proceso, sin esperar a ``TTL_SECONDS``.
    Como en la lista, una comprobación que leyó una versión más vieja nunca sustituye a una entrada
    más nueva.
    """

    def __init__(
        self,
        store: DeclaredNamesStore,
        *,
        ttl_seconds: float = TTL_SECONDS,
        versions: VocabularyStore | None = None,
    ) -> None:
        if not ttl_seconds > 0:
            raise ValueError("ttl_seconds debe ser positivo")
        self._store = store
        self._ttl = ttl_seconds
        self._versions = versions
        self._entries: dict[uuid.UUID, _NamesEntry] = {}

    def names_for(self, organization_id: uuid.UUID | None) -> DeclaredNames:
        """El vocabulario con el que se valida ahora: solo memoria, nunca red ni base."""
        entry = None if organization_id is None else self._entries.get(organization_id)
        return NO_DECLARED_NAMES if entry is None else entry.names

    def invalidate(self, organization_id: uuid.UUID) -> None:
        """La próxima escritura de la organización relee sus nombres."""
        self._entries.pop(organization_id, None)

    async def refresh(self, preparation: FreeTextPreparation) -> None:
        """Relee los nombres al abrir la escritura si la entrada caducó o la versión de la lista
        cambió (el preparador)."""
        organization = preparation.organization_id
        now = preparation.clock.monotonic()
        version = await self._list_version(preparation)
        entry = self._entries.get(organization)
        if (
            entry is not None
            and entry.list_version == version
            and now - entry.loaded_at < self._ttl
        ):
            return
        names = declared_names(await self._store.names(organization, preparation.rows))
        current = self._entries.get(organization)
        if current is None or (version, now) >= (current.list_version, current.loaded_at):
            self._entries[organization] = _NamesEntry(names, now, version)

    async def _list_version(self, preparation: FreeTextPreparation) -> int:
        if self._versions is None:
            return INITIAL_LIST_VERSION
        head = await self._versions.head(preparation.organization_id, preparation.rows)
        return INITIAL_LIST_VERSION if head is None else head.version
