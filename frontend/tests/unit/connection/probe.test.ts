import { afterEach, describe, expect, it, vi } from 'vitest';

import { startConnectionProbe } from '@/state/connection';

import { fixture } from '../../support/connection';

async function settle() {
  // Vacía la cadena de microtareas de fetch sin usar el reloj de pared.
  for (let i = 0; i < 8; i++) await Promise.resolve();
}

function setup() {
  const context = fixture();
  const requests: {
    signal: AbortSignal;
    resolve: (response: Response) => void;
    reject: (error: Error) => void;
  }[] = [];
  const fetch = vi.fn<typeof globalThis.fetch>(
    (_url, init) =>
      new Promise((resolve, reject) => {
        const signal = init?.signal;
        if (!signal) throw new Error('La sonda requiere AbortSignal');
        requests.push({ signal, resolve, reject });
      }),
  );
  const stop = startConnectionProbe({
    ...context,
    document,
    fetch,
    origin: (dep) => `https://${dep}.example.test`,
  });
  return { ...context, requests, fetch, stop };
}

const cleanup: (() => void)[] = [];
afterEach(() => {
  for (const stop of cleanup.splice(0)) stop();
});

function visible(state: DocumentVisibilityState) {
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue(state);
  document.dispatchEvent(new Event('visibilitychange'));
}

describe('sonda de conexión', () => {
  it('no programa ni llama fetch con todos los circuitos cerrados', async () => {
    const { clock, fetch, stop } = setup();
    cleanup.push(stop);
    clock.advance(120_000);
    await settle();
    expect(fetch).not.toHaveBeenCalled();
    expect(clock.pendingCount).toBe(0);
  });

  it('espera 30 s, consulta GET /health/live y una respuesta correcta cierra', async () => {
    const { clock, connection, open, fetch, requests, stop } = setup();
    cleanup.push(stop);
    open();
    clock.advance(29_999);
    await settle();
    expect(fetch).not.toHaveBeenCalled();
    clock.advance(1);
    await settle();
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledWith(new URL('https://platform.example.test/health/live'), {
      method: 'GET',
      signal: requests[0]?.signal,
      cache: 'no-store',
      credentials: 'omit',
      redirect: 'error',
    });
    requests[0]?.resolve(new Response(null, { status: 200 }));
    await settle();
    expect(connection.getSnapshot().connection.state).toBe('online');
    expect(connection.getSnapshot().connection.last_success_at).toBe('1970-01-01T00:00:30.000Z');
    expect(clock.pendingCount).toBe(0);
    clock.advance(90_000);
    await settle();
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('no sondea oculta y reanuda al hacerse visible', async () => {
    visible('hidden');
    const { clock, open, fetch, stop } = setup();
    cleanup.push(stop);
    open();
    clock.advance(90_000);
    await settle();
    expect(fetch).not.toHaveBeenCalled();
    expect(clock.pendingCount).toBe(0);
    visible('visible');
    await settle();
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('aborta al ocultarse y descarta la respuesta tardía de la sonda cancelada', async () => {
    const { clock, connection, open, fetch, requests, stop } = setup();
    cleanup.push(stop);
    open();
    clock.advance(30_000);
    await settle();
    visible('hidden');
    expect(requests[0]?.signal.aborted).toBe(true);
    expect(clock.pendingCount).toBe(0);
    visible('visible');
    await settle();
    expect(fetch).toHaveBeenCalledTimes(2);
    requests[0]?.resolve(new Response(null, { status: 200 }));
    await settle();
    expect(connection.getSnapshot().circuits.platform.state).toBe('half_open');
    requests[1]?.resolve(new Response(null, { status: 200 }));
    await settle();
    expect(connection.getSnapshot().connection.state).toBe('online');
  });

  it('corta exactamente a los 10 s aunque fetch ignore abort; reabre y reintenta', async () => {
    const { clock, connection, open, fetch, requests, stop } = setup();
    cleanup.push(stop);
    open();
    clock.advance(30_000);
    await settle();
    clock.advance(9_999);
    expect(requests[0]?.signal.aborted).toBe(false);
    clock.advance(1);
    expect(requests[0]?.signal.aborted).toBe(true);
    expect(connection.getSnapshot().circuits.platform.state).toBe('open');
    requests[0]?.resolve(new Response(null, { status: 200 }));
    await settle();
    expect(connection.canWrite()).toBe(false);
    clock.advance(29_999);
    await settle();
    expect(fetch).toHaveBeenCalledTimes(1);
    clock.advance(1);
    await settle();
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it.each(['http', 'network', 'throw'] as const)(
    'no cierra ante fallo %s; vuelve a sondear a los 30 s',
    async (failure) => {
      const { clock, connection, open, fetch, requests, stop } = setup();
      cleanup.push(stop);
      if (failure === 'throw')
        fetch.mockImplementationOnce(() => {
          throw new Error('red sintética');
        });
      open();
      clock.advance(30_000);
      await settle();
      if (failure === 'http') requests[0]?.resolve(new Response(null, { status: 503 }));
      if (failure === 'network') requests[0]?.reject(new TypeError('red sintética'));
      await settle();
      expect(connection.canWrite()).toBe(false);
      clock.advance(30_000);
      await settle();
      expect(fetch).toHaveBeenCalledTimes(2);
    },
  );

  it('no duplica sondas en vuelo ante notificaciones simultáneas; aísla la recuperación', async () => {
    const { clock, connection, open, fetch, requests, stop } = setup();
    cleanup.push(stop);
    open();
    open('evidence_store');
    clock.advance(30_000);
    await settle();
    await Promise.all(
      Array.from({ length: 10 }, async () => {
        connection.tick();
        connection.register('document_store', { ok: true });
        await Promise.resolve();
      }),
    );
    expect(fetch).toHaveBeenCalledTimes(2);
    requests[1]?.resolve(new Response(null, { status: 200 }));
    await settle();
    expect(connection.getSnapshot().circuits.evidence_store.state).toBe('closed');
    expect(connection.getSnapshot().circuits.platform.state).toBe('half_open');
  });

  it('stop es idempotente: cancela timers, fetch y suscripciones sin recuperación tardía', async () => {
    const { clock, connection, open, fetch, requests, stop } = setup();
    cleanup.push(stop);
    open();
    clock.advance(30_000);
    await settle();
    stop();
    stop();
    expect(clock.pendingCount).toBe(0);
    expect(requests[0]?.signal.aborted).toBe(true);
    requests[0]?.resolve(new Response(null, { status: 200 }));
    await settle();
    expect(connection.getSnapshot().circuits.platform.state).toBe('half_open');
    visible('hidden');
    visible('visible');
    connection.register('document_store', { ok: true });
    clock.advance(120_000);
    await settle();
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('descarta una sonda anterior si otro puerto reabre durante half_open', async () => {
    const { clock, connection, open, fail, requests, stop } = setup();
    cleanup.push(stop);
    open();
    clock.advance(30_000);
    await settle();
    fail();
    expect(requests[0]?.signal.aborted).toBe(true);
    requests[0]?.resolve(new Response(null, { status: 200 }));
    await settle();
    expect(connection.canWrite()).toBe(false);
    expect(connection.getSnapshot().circuits.platform.retry_at).toBe(60_000);
  });

  it('cancela una sonda pendiente si otra lectura ya recuperó la dependencia', async () => {
    const { clock, connection, open, requests, stop } = setup();
    cleanup.push(stop);
    open();
    clock.advance(30_000);
    await settle();
    connection.register('platform', { ok: true });
    expect(requests[0]?.signal.aborted).toBe(true);
    requests[0]?.reject(new TypeError('respuesta tardía'));
    await settle();
    expect(connection.getSnapshot().connection.state).toBe('online');
    expect(clock.pendingCount).toBe(0);
  });
});
