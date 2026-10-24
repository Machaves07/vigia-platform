/**
 * Caché de consultas de la aplicación (LC-APP-11, PAT-APP-ESC-01 y 02): un `QueryClient` de
 * TanStack Query con los valores del diseño, el límite de 20 entradas por pantalla, la
 * invalidación por recurso (BR-APP-55) y el refresco periódico con puertas (BR-APP-54, D-4).
 *
 * - `retry: false` en lecturas (los reintentos los hace `ports.base`: nunca doble reintento) y
 *   `retry: 0` en mutaciones; `gcTime` de 5 min.
 * - TanStack Query no refresca nada por su cuenta: sin `refetchOnWindowFocus`, sin
 *   `refetchOnReconnect` y sin `refetchInterval` (`refetchIntervalInBackground = false`). El único
 *   refresco periódico es el de este módulo: cada 60 s con el `Clock` inyectado, solo sobre las
 *   lecturas `periodic_60s` montadas y solo si la pestaña está visible, el circuito `platform`
 *   está cerrado y la puerta de actividad está abierta. Las tres se leen en cada tic.
 * - Ninguna función de precarga: solo pide datos una lectura montada (PR-APP-24).
 *
 * `QueryCacheEntry` del diseño es la propia entrada de TanStack Query: no hay almacén propio.
 */
import { QueryClient, type Query } from '@tanstack/react-query';

import type { Clock, TimerHandle } from '../clock';
import type { Dependency } from '../connection';

import {
  invalidationTargets,
  SCREEN_RESOURCES,
  type Invalidation,
  type ScreenGraph,
} from './invalidation';
import { SCREENS, type Screen } from './keys';
import { PERIODIC_INTERVAL_MS, type QueryPolicy } from './policies';

export const MAX_ENTRIES_PER_SCREEN = 20;
const GC_TIME_MS = 300_000;

/** Condiciones del refresco periódico; se consultan en cada tic. */
export interface RefreshGates {
  /** Pestaña visible. */
  isVisible(): boolean;
  /** Circuito `platform` cerrado (`connection.canRead('platform')`, VIG-282). */
  canReadPlatform(): boolean;
  /** Puerta de actividad (D-4): VIG-300 la cierra tras 25 min sin interacción. */
  isActive(): boolean;
}

/** Las puertas del navegador: visibilidad del documento, circuito y actividad inyectada. */
export function refreshGates(options: {
  readonly document: Pick<Document, 'visibilityState'>;
  readonly connection: { canRead(dep: Dependency): boolean };
  readonly isActive: () => boolean;
}): RefreshGates {
  return {
    isVisible: () => options.document.visibilityState === 'visible',
    canReadPlatform: () => options.connection.canRead('platform'),
    isActive: options.isActive,
  };
}

export interface ScreenQueryCache {
  readonly client: QueryClient;
  /** Invalida las entradas que afecta una escritura (BR-APP-55). */
  invalidate(invalidation: Invalidation): Promise<void>;
  /** Vacía toda la caché: cambio de concesión o de organización, cierre de sesión. */
  clearAll(): void;
  /** Detiene el refresco periódico y vacía la caché. */
  dispose(): void;
}

function screenOf(query: Query): Screen | null {
  const screen = query.queryKey[0];
  return (SCREENS as readonly unknown[]).includes(screen) ? (screen as Screen) : null;
}

function policyOf(query: Query): QueryPolicy | null {
  const policy = query.meta?.policy;
  return typeof policy === 'object' && policy !== null ? (policy as QueryPolicy) : null;
}

export function createScreenQueryCache(options: {
  readonly clock: Clock;
  readonly gates: RefreshGates;
  /** Grafo pantalla → recurso; por omisión el de la aplicación. */
  readonly graph?: ScreenGraph;
}): ScreenQueryCache {
  const { clock, gates } = options;
  const graph = options.graph ?? SCREEN_RESOURCES;
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        gcTime: GC_TIME_MS,
        networkMode: 'always',
        refetchOnWindowFocus: false,
        refetchOnReconnect: false,
        refetchInterval: false,
        refetchIntervalInBackground: false,
      },
      mutations: { retry: 0, gcTime: GC_TIME_MS, networkMode: 'always' },
    },
  });
  const cache = client.getQueryCache();

  // Orden de llegada de cada entrada, para expulsar las más antiguas (PAT-APP-ESC-02).
  const arrival = new WeakMap<Query, number>();
  let arrivals = 0;
  const enforceLimit = (screen: Screen) => {
    const entries = cache.findAll({ queryKey: [screen] });
    if (entries.length <= MAX_ENTRIES_PER_SCREEN) return;
    // Primero las que nadie observa; entre iguales, la más antigua.
    const victims = [...entries].sort(
      (a, b) =>
        Number(a.isActive()) - Number(b.isActive()) ||
        (arrival.get(a) ?? 0) - (arrival.get(b) ?? 0),
    );
    for (const victim of victims.slice(0, entries.length - MAX_ENTRIES_PER_SCREEN)) {
      cache.remove(victim);
    }
  };
  const unsubscribe = cache.subscribe((event) => {
    if (event.type !== 'added') return;
    const query = event.query as Query;
    arrival.set(query, arrivals++);
    const screen = screenOf(query);
    if (screen !== null) enforceLimit(screen);
  });

  let timer: TimerHandle | null = null;
  const tick = () => {
    timer = clock.setTimeout(tick, PERIODIC_INTERVAL_MS);
    if (!gates.isVisible() || !gates.canReadPlatform() || !gates.isActive()) return;
    void client.refetchQueries(
      { type: 'active', predicate: (query) => policyOf(query)?.periodic === true },
      { cancelRefetch: false, throwOnError: false },
    );
  };
  timer = clock.setTimeout(tick, PERIODIC_INTERVAL_MS);

  const clearAll = () => {
    void client.cancelQueries();
    client.clear();
  };

  return {
    client,
    async invalidate(invalidation) {
      const targets = invalidationTargets(graph, invalidation);
      const hit = (query: Query) => {
        const screen = screenOf(query);
        return (
          (screen !== null && targets.screens.has(screen)) ||
          (targets.principal && query.queryKey[1] === 'GET /me')
        );
      };
      const audited = (query: Query) => policyOf(query)?.explicitOnly !== false;
      await Promise.all([
        // Una lectura auditada queda desactualizada, nunca se vuelve a pedir sola (A-6).
        client.invalidateQueries(
          { predicate: (query) => hit(query) && audited(query), refetchType: 'none' },
          { throwOnError: false },
        ),
        client.invalidateQueries(
          { predicate: (query) => hit(query) && !audited(query), refetchType: 'active' },
          { throwOnError: false },
        ),
      ]).then(() => undefined);
    },
    clearAll,
    dispose() {
      if (timer !== null) clock.clearTimeout(timer);
      timer = null;
      unsubscribe();
      clearAll();
    },
  };
}
