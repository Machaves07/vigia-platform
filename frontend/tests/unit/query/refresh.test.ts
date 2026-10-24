/**
 * Refresco periódico (`periodic_60s`, BR-APP-54, D-4): con la pestaña oculta, el circuito
 * `platform` abierto o la puerta de actividad cerrada no sale ninguna petición periódica en 5 min
 * simulados; con las tres abiertas sale una por minuto, y al reabrir la puerta se reanuda.
 * MSW cuenta las peticiones que llegan; el tiempo es el de `FakeClock`.
 */
import { QueryObserver } from '@tanstack/react-query';
import { http, HttpResponse } from 'msw';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  createScreenQueryCache,
  policyFor,
  refreshGates,
  screenKey,
  screenQueryOptions,
  type ScreenQueryCache,
} from '@/state/query';

import { fixture } from '../../support/connection';
import { server } from '../../setup/msw';

const FIVE_MINUTES_MS = 300_000;
const WAIT = { timeout: 10_000 };

const cleanup: (() => void)[] = [];
afterEach(() => {
  for (const stop of cleanup.splice(0)) stop();
});

function setup(initial: { visible?: boolean; active?: boolean } = {}) {
  const context = fixture();
  const document: { visibilityState: DocumentVisibilityState } = {
    visibilityState: initial.visible === false ? 'hidden' : 'visible',
  };
  const activity = { open: initial.active ?? true };
  const cache: ScreenQueryCache = createScreenQueryCache({
    clock: context.clock,
    gates: refreshGates({
      document,
      connection: context.connection,
      isActive: () => activity.open,
    }),
  });
  let requests = 0;
  server.use(
    http.get('http://localhost/notifications', () => {
      requests++;
      return HttpResponse.json({ items: [] });
    }),
  );
  const observer = new QueryObserver(
    cache.client,
    screenQueryOptions({
      key: screenKey({
        screen: 'shell',
        operation: 'GET /notifications',
        scope: 'all',
        concession: null,
      }),
      policy: policyFor('GET /notifications'),
      read: ({ signal }) =>
        fetch('http://localhost/notifications', { signal }).then(
          (r) => r.json() as Promise<unknown>,
        ),
    }),
  );
  cleanup.push(
    observer.subscribe(() => undefined),
    () => {
      cache.dispose();
    },
  );
  const idle = () =>
    vi.waitFor(() => {
      expect(observer.getCurrentResult().fetchStatus).toBe('idle');
    }, WAIT);
  /** Avanza minuto a minuto, esperando a que termine cada petición. */
  const advance = async (ms: number) => {
    for (let elapsed = 0; elapsed < ms; elapsed += 60_000) {
      context.clock.advance(Math.min(60_000, ms - elapsed));
      await idle();
    }
  };
  return { ...context, document, activity, advance, idle, requests: () => requests };
}

describe('refresco periódico de 60 s con puertas', () => {
  it('con las tres puertas abiertas pide una vez por minuto', async () => {
    const { advance, idle, requests } = setup();
    await vi.waitFor(() => {
      expect(requests()).toBe(1);
    }, WAIT);
    await idle();
    await advance(FIVE_MINUTES_MS);
    expect(requests()).toBe(6);
  });

  it('con la pestaña oculta no pide nada en 5 min', async () => {
    const { advance, idle, requests } = setup({ visible: false });
    await vi.waitFor(() => {
      expect(requests()).toBe(1);
    }, WAIT);
    await idle();
    await advance(FIVE_MINUTES_MS);
    expect(requests()).toBe(1);
  });

  it('con el circuito platform abierto no pide nada en 5 min', async () => {
    const { advance, clock, connection, fail, idle, open, requests } = setup();
    await vi.waitFor(() => {
      expect(requests()).toBe(1);
    }, WAIT);
    await idle();
    open();
    // La sonda sigue fallando cada 10 s: el circuito nunca llega a cerrarse.
    const probe = () => {
      if (connection.getSnapshot().circuits.platform.state === 'half_open') fail();
      clock.setTimeout(probe, 10_000);
    };
    clock.setTimeout(probe, 5_000);
    await advance(FIVE_MINUTES_MS);
    expect(connection.canRead('platform')).toBe(false);
    expect(requests()).toBe(1);
  });

  it('con la puerta de actividad cerrada no pide nada en 5 min y se reanuda al abrirla', async () => {
    const { activity, advance, idle, requests } = setup({ active: false });
    await vi.waitFor(() => {
      expect(requests()).toBe(1);
    }, WAIT);
    await idle();
    await advance(FIVE_MINUTES_MS);
    expect(requests()).toBe(1);
    activity.open = true;
    await advance(60_000);
    expect(requests()).toBe(2);
  });

  it('una pestaña que vuelve a ser visible no pide hasta el siguiente tic', async () => {
    const { advance, document, idle, requests } = setup({ visible: false });
    await vi.waitFor(() => {
      expect(requests()).toBe(1);
    }, WAIT);
    await idle();
    await advance(90_000);
    document.visibilityState = 'visible';
    globalThis.dispatchEvent(new Event('visibilitychange'));
    globalThis.dispatchEvent(new Event('focus'));
    await idle();
    expect(requests()).toBe(1);
    await advance(30_000);
    expect(requests()).toBe(2);
  });
});
