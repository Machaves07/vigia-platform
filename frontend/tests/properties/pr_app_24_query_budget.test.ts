/**
 * PR-APP-24 (LC-APP-11, PAT-APP-ESC-01 y 02): para toda secuencia de navegación (montar y
 * desmontar lecturas), acciones explícitas, escrituras, paso del tiempo y cambios de las puertas
 * de refresco:
 *
 * - ninguna lectura con `staleTime = 0` (auditada) se ejecuta sin una acción explícita, y nunca
 *   más de una por acción;
 * - ninguna lectura de datos se precarga: solo se pide con una lectura montada;
 * - el refresco periódico no pide nada con una puerta cerrada;
 * - nunca hay más de 20 entradas por pantalla.
 */
import { QueryObserver } from '@tanstack/react-query';
import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import {
  FRESHNESS,
  MAX_ENTRIES_PER_SCREEN,
  RESOURCES,
  SCREENS,
  screenKey,
  screenQueryOptions,
  type QueryPolicy,
  type ReadOperation,
  type ScreenQueryKey,
} from '@/state/query';

import { queryFixture, settle } from '../support/queryCache';
import { PROPERTY_TIMEOUT_MS } from '../support/timeouts';

type Kind = QueryPolicy['kind'];
const KINDS: readonly Kind[] = ['identity', 'live', 'reference', 'audited'];
const OPERATIONS: Readonly<Record<Kind, ReadOperation>> = {
  identity: 'GET /me',
  live: 'GET /notifications',
  reference: 'GET /zones/{zone_id}/catalog',
  audited: 'GET /audit/entries',
};
const SCREENS_UNDER_TEST = ['scr03', 'scr15'] as const;

const action = fc.oneof(
  {
    weight: 6,
    arbitrary: fc.record({
      type: fc.constant('mount' as const),
      screen: fc.constantFrom(...SCREENS_UNDER_TEST),
      kind: fc.constantFrom(...KINDS),
      id: fc.integer({ min: 0, max: 29 }),
      /** En una auditada, montarla activada es la acción explícita de abrirla. */
      requested: fc.boolean(),
    }),
  },
  {
    weight: 1,
    /** Recorrer elementos: montar y desmontar `count` lecturas seguidas de una pantalla. */
    arbitrary: fc.record({
      type: fc.constant('browse' as const),
      screen: fc.constantFrom(...SCREENS_UNDER_TEST),
      kind: fc.constantFrom(...KINDS),
      from: fc.integer({ min: 0, max: 29 }),
      count: fc.integer({ min: 1, max: 25 }),
    }),
  },
  { weight: 2, arbitrary: fc.record({ type: fc.constant('unmount' as const), at: fc.nat() }) },
  { weight: 2, arbitrary: fc.record({ type: fc.constant('refresh' as const), at: fc.nat() }) },
  {
    weight: 2,
    arbitrary: fc.record({
      type: fc.constant('write' as const),
      screen: fc.oneof(fc.constantFrom(...SCREENS_UNDER_TEST), fc.constantFrom(...SCREENS)),
      resources: fc.uniqueArray(fc.constantFrom(...RESOURCES), { maxLength: 3 }),
      affectsSelf: fc.boolean(),
    }),
  },
  {
    weight: 2,
    arbitrary: fc.record({
      type: fc.constant('advance' as const),
      ms: fc.integer({ min: 0, max: 130_000 }),
    }),
  },
  {
    weight: 1,
    arbitrary: fc.record({
      type: fc.constant('gate' as const),
      gate: fc.constantFrom('visible' as const, 'platform' as const, 'active' as const),
      open: fc.boolean(),
    }),
  },
);

interface Mounted {
  readonly hash: string;
  readonly kind: Kind;
  readonly observer: QueryObserver<string, Error, string, string, ScreenQueryKey>;
  readonly stop: () => void;
}

describe('PR-APP-24', () => {
  it(
    'auditadas solo por acción explícita, sin precarga, sin refresco con puertas cerradas y ≤ 20 por pantalla',
    async () => {
      await fc.assert(
        fc.asyncProperty(fc.array(action, { maxLength: 100 }), async (actions) => {
          const { cache, clock, gates } = queryFixture();
          const queries = cache.client.getQueryCache();
          const reads = new Map<string, number>();
          const kinds = new Map<string, Kind>();
          const prefetched: string[] = [];
          const mounted: Mounted[] = [];
          const total = (kind: Kind) =>
            [...reads].reduce((sum, [hash, n]) => sum + (kinds.get(hash) === kind ? n : 0), 0);
          const mount = (
            screen: (typeof SCREENS_UNDER_TEST)[number],
            kind: Kind,
            id: number,
            requested: boolean,
          ): Mounted => {
            const key = screenKey({
              screen,
              operation: OPERATIONS[kind],
              scope: 'all',
              concession: null,
              params: { id },
            });
            const hash = JSON.stringify(key);
            kinds.set(hash, kind);
            const observer: Mounted['observer'] = new QueryObserver(
              cache.client,
              screenQueryOptions({
                key,
                policy: FRESHNESS[kind],
                read: () => {
                  // Precarga: una petición de una entrada que ninguna lectura montada observa.
                  if (observer.getCurrentQuery().getObserversCount() === 0) prefetched.push(hash);
                  reads.set(hash, (reads.get(hash) ?? 0) + 1);
                  return Promise.resolve(hash);
                },
                ...(kind === 'audited' ? { enabled: requested } : {}),
              }),
            );
            return { hash, kind, observer, stop: observer.subscribe(() => undefined) };
          };
          try {
            for (const step of actions) {
              const before = new Map(reads);
              const live = total('live');
              let explicit: string | null = null;

              if (step.type === 'mount') {
                const entry = mount(step.screen, step.kind, step.id, step.requested);
                if (step.kind === 'audited' && step.requested) explicit = entry.hash;
                mounted.push(entry);
              } else if (step.type === 'browse') {
                for (let id = step.from; id < step.from + step.count; id++) {
                  mount(step.screen, step.kind, id, false).stop();
                }
              } else if (step.type === 'unmount' && mounted.length > 0) {
                mounted.splice(step.at % mounted.length, 1)[0]?.stop();
              } else if (step.type === 'refresh' && mounted.length > 0) {
                const target = mounted[step.at % mounted.length];
                if (target !== undefined) {
                  explicit = target.hash;
                  void target.observer.refetch({ cancelRefetch: false });
                }
              } else if (step.type === 'write') {
                await cache.invalidate(step);
              } else if (step.type === 'advance') {
                clock.advance(step.ms);
              } else if (step.type === 'gate') {
                gates[step.gate] = step.open;
              }
              await settle(2);

              for (const [hash, n] of reads) {
                if (kinds.get(hash) !== 'audited') continue;
                const delta = n - (before.get(hash) ?? 0);
                expect(delta, `${step.type} ${hash}`).toBeLessThanOrEqual(
                  hash === explicit ? 1 : 0,
                );
              }
              if (step.type === 'advance' && !(gates.visible && gates.platform && gates.active)) {
                expect(total('live'), 'refresco periódico con una puerta cerrada').toBe(live);
              }
              expect(prefetched, 'lectura sin pantalla montada').toEqual([]);
              for (const screen of SCREENS_UNDER_TEST) {
                expect(queries.findAll({ queryKey: [screen] }).length).toBeLessThanOrEqual(
                  MAX_ENTRIES_PER_SCREEN,
                );
              }
            }
          } finally {
            for (const entry of mounted) entry.stop();
            cache.dispose();
          }
        }),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );
});
