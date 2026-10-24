/**
 * PR-APP-16 (BR-APP-54 y 55): para todo grafo pantalla → recurso y toda secuencia de escrituras,
 * quedan invalidadas las entradas de la pantalla que escribe y de las pantallas que muestran un
 * recurso escrito (más `GET /me` si la escritura afecta al propio usuario) **y ninguna otra**; y
 * ninguna lectura `on_demand` se ejecuta por una escritura.
 */
import { QueryObserver } from '@tanstack/react-query';
import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import {
  FRESHNESS,
  RESOURCES,
  SCREENS,
  screenKey,
  screenQueryOptions,
  type Invalidation,
  type QueryPolicy,
  type ReadOperation,
  type Resource,
  type Screen,
  type ScreenGraph,
} from '@/state/query';

import { queryFixture, settle } from '../support/queryCache';
import { PROPERTY_TIMEOUT_MS } from '../support/timeouts';

const screen = fc.constantFrom(...SCREENS);
const resource = fc.constantFrom(...RESOURCES);
const graph: fc.Arbitrary<ScreenGraph> = fc.record(
  Object.fromEntries(
    SCREENS.map((name) => [name, fc.uniqueArray(resource, { maxLength: 4 })]),
  ) as Record<Screen, fc.Arbitrary<Resource[]>>,
);
const OPERATIONS: Readonly<Record<QueryPolicy['kind'], ReadOperation>> = {
  identity: 'GET /me',
  live: 'GET /loop/findings',
  reference: 'GET /zones/{zone_id}/catalog',
  audited: 'GET /loop/findings/{finding_state_id}',
};
const entry = fc.record({
  screen,
  kind: fc.constantFrom(...(Object.keys(OPERATIONS) as QueryPolicy['kind'][])),
  id: fc.integer({ min: 0, max: 3 }),
  mounted: fc.boolean(),
  /** Una auditada montada y ya pedida por la persona (acción explícita previa). */
  requested: fc.boolean(),
});
const write: fc.Arbitrary<Invalidation> = fc.record({
  screen,
  resources: fc.uniqueArray(resource, { maxLength: 3 }),
  affectsSelf: fc.boolean(),
});

/** Modelo independiente de BR-APP-55. */
function expectedHit(
  model: ScreenGraph,
  writes: readonly Invalidation[],
  target: { screen: Screen; operation: ReadOperation },
): boolean {
  return writes.some(
    (w) =>
      w.screen === target.screen ||
      model[target.screen].some((shown) => w.resources.includes(shown)) ||
      (w.affectsSelf === true && target.operation === 'GET /me'),
  );
}

describe('PR-APP-16', () => {
  it(
    'invalida su pantalla y las que muestran el recurso, ninguna otra, sin pedir auditadas',
    async () => {
      await fc.assert(
        fc.asyncProperty(
          graph,
          fc.uniqueArray(entry, {
            maxLength: 24,
            selector: (e) => `${e.screen} ${e.kind} ${String(e.id)}`,
          }),
          fc.array(write, { minLength: 1, maxLength: 6 }),
          async (model, entries, writes) => {
            const { cache } = queryFixture({ graph: model });
            const reads = new Map<string, number>();
            const stops: (() => void)[] = [];
            try {
              const targets = entries.map((e) => {
                const operation = OPERATIONS[e.kind];
                const key = screenKey({
                  screen: e.screen,
                  operation,
                  scope: 'all',
                  concession: null,
                  params: { id: e.id },
                });
                const hash = JSON.stringify(key);
                const options = screenQueryOptions({
                  key,
                  policy: FRESHNESS[e.kind],
                  read: () => {
                    reads.set(hash, (reads.get(hash) ?? 0) + 1);
                    return Promise.resolve(hash);
                  },
                  ...(e.kind === 'audited' ? { enabled: e.requested } : {}),
                });
                if (e.mounted) {
                  const observer = new QueryObserver(cache.client, options);
                  stops.push(observer.subscribe(() => undefined));
                } else {
                  cache.client.setQueryData(key, hash);
                }
                return { ...e, operation, key, hash };
              });
              await settle();
              const before = new Map(reads);
              for (const w of writes) await cache.invalidate(w);
              await settle();

              for (const target of targets) {
                const query = cache.client
                  .getQueryCache()
                  .find({ queryKey: target.key, exact: true });
                expect(query, target.hash).toBeDefined();
                // Una entrada montada que no es auditada se vuelve a pedir y deja de estar
                // invalidada: cuenta como tocada si se marcó o se volvió a pedir.
                const refetched = (reads.get(target.hash) ?? 0) > (before.get(target.hash) ?? 0);
                expect(query?.state.isInvalidated === true || refetched, target.hash).toBe(
                  expectedHit(model, writes, target),
                );
                if (target.kind === 'audited') {
                  expect(reads.get(target.hash) ?? 0, target.hash).toBe(
                    before.get(target.hash) ?? 0,
                  );
                }
              }
            } finally {
              for (const stop of stops) stop();
              cache.dispose();
            }
          },
        ),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );
});
