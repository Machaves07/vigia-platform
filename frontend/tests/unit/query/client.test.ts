/**
 * `QueryClient` de la aplicación (LC-APP-11, PAT-APP-ESC-02): sin reintentos en lecturas ni en
 * escrituras, `gcTime` de 5 min, sin refresco por foco ni por reconexión, límite de 20 entradas
 * por pantalla con expulsión de la más antigua sin observar, `clearAll()` y claves canónicas.
 */
import { QueryObserver } from '@tanstack/react-query';
import { afterEach, describe, expect, it } from 'vitest';

import {
  FRESHNESS,
  MAX_ENTRIES_PER_SCREEN,
  screenKey,
  screenQueryOptions,
  type ScreenQueryCache,
} from '@/state/query';

import { queryFixture, settle } from '../../support/queryCache';

const caches: ScreenQueryCache[] = [];
afterEach(() => {
  for (const cache of caches.splice(0)) cache.dispose();
});

function fixture() {
  const context = queryFixture();
  caches.push(context.cache);
  return context;
}

const key = (id: number, screen: 'scr08' | 'scr14' = 'scr08') =>
  screenKey({
    screen,
    operation: 'GET /loop/findings',
    scope: 'all',
    concession: null,
    params: { id },
  });

describe('QueryClient', () => {
  it('lecturas sin reintentos, gcTime 5 min, sin refresco por foco, reconexión ni intervalo', () => {
    const { cache } = fixture();
    const { queries, mutations } = cache.client.getDefaultOptions();
    expect(queries).toMatchObject({
      retry: false,
      gcTime: 300_000,
      refetchOnWindowFocus: false,
      refetchOnReconnect: false,
      refetchInterval: false,
      refetchIntervalInBackground: false,
    });
    expect(mutations).toMatchObject({ retry: 0, gcTime: 300_000 });
  });

  it('una lectura que falla se pide una sola vez', async () => {
    const { cache } = fixture();
    let calls = 0;
    const observer = new QueryObserver(
      cache.client,
      screenQueryOptions({
        key: key(1),
        policy: FRESHNESS.live,
        read: () => {
          calls++;
          return Promise.reject(new Error('timeout'));
        },
      }),
    );
    const stop = observer.subscribe(() => undefined);
    await settle();
    stop();
    expect(observer.getCurrentResult().status).toBe('error');
    expect(calls).toBe(1);
  });

  it(`nunca guarda más de ${String(MAX_ENTRIES_PER_SCREEN)} entradas por pantalla; expulsa la más antigua`, () => {
    const { cache } = fixture();
    for (let id = 0; id < 25; id++) cache.client.setQueryData(key(id), id);
    cache.client.setQueryData(key(0, 'scr14'), 0);
    const kept = cache.client
      .getQueryCache()
      .findAll({ queryKey: ['scr08'] })
      .map((query) => (query.queryKey[4] as { id: number }).id)
      .sort((a, b) => a - b);
    expect(kept).toEqual(Array.from({ length: 20 }, (_, i) => i + 5));
    // El límite es por pantalla: la otra conserva su entrada.
    expect(cache.client.getQueryCache().findAll({ queryKey: ['scr14'] })).toHaveLength(1);
  });

  it('expulsa antes una entrada sin observar que una montada', async () => {
    const { cache } = fixture();
    const observer = new QueryObserver(
      cache.client,
      screenQueryOptions({ key: key(0), policy: FRESHNESS.live, read: () => Promise.resolve(0) }),
    );
    const stop = observer.subscribe(() => undefined);
    await settle();
    for (let id = 1; id <= 20; id++) cache.client.setQueryData(key(id), id);
    const ids = cache.client
      .getQueryCache()
      .findAll({ queryKey: ['scr08'] })
      .map((query) => (query.queryKey[4] as { id: number }).id);
    stop();
    expect(ids).toHaveLength(20);
    expect(ids).toContain(0);
    expect(ids).not.toContain(1);
  });

  it('clearAll() vacía todas las pantallas', () => {
    const { cache } = fixture();
    cache.client.setQueryData(key(1), 1);
    cache.client.setQueryData(key(1, 'scr14'), 1);
    cache.clearAll();
    expect(cache.client.getQueryCache().getAll()).toEqual([]);
  });

  it('dispose() detiene el refresco periódico', () => {
    const { cache, clock } = fixture();
    expect(clock.pendingCount).toBe(1);
    cache.dispose();
    expect(clock.pendingCount).toBe(0);
  });
});

describe('screenKey', () => {
  it('[pantalla, operación, alcance, concesión, parámetros canónicos]', () => {
    expect(
      screenKey({
        screen: 'scr08',
        operation: 'GET /loop/findings',
        scope: 'plant-1',
        concession: 'c-1',
        params: { zone: 'z', after: undefined, filter: { b: 1, a: [2, 1] } },
      }),
    ).toEqual([
      'scr08',
      'GET /loop/findings',
      'plant-1',
      'c-1',
      { filter: { a: [2, 1], b: 1 }, zone: 'z' },
    ]);
  });

  it('el orden de los parámetros no cambia la entrada; el alcance y la concesión sí', () => {
    const { cache } = fixture();
    const base = { screen: 'scr08', operation: 'GET /loop/findings' } as const;
    cache.client.setQueryData(
      screenKey({ ...base, scope: 'all', concession: null, params: { a: 1, b: 2 } }),
      'x',
    );
    expect(
      cache.client.getQueryData(
        screenKey({ ...base, scope: 'all', concession: null, params: { b: 2, a: 1 } }),
      ),
    ).toBe('x');
    expect(
      cache.client.getQueryData(
        screenKey({ ...base, scope: 'plant-1', concession: null, params: { a: 1, b: 2 } }),
      ),
    ).toBeUndefined();
    expect(
      cache.client.getQueryData(
        screenKey({ ...base, scope: 'all', concession: 'c-1', params: { a: 1, b: 2 } }),
      ),
    ).toBeUndefined();
  });

  it('rechaza parámetros no finitos', () => {
    expect(() =>
      screenKey({
        screen: 'scr08',
        operation: 'GET /loop/findings',
        scope: 'all',
        concession: null,
        params: { n: Number.NaN },
      }),
    ).toThrow(RangeError);
  });
});
