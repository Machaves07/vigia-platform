/**
 * Muestra de exposición del tramo 3a (BR-APP-84; interfaces de U-03 v1.2).
 *
 * - `fetched_at` se lee al recibir la respuesta del pase y `displayed_at` en el fotograma
 *   siguiente, **ambos del mismo reloj inyectado** (que aquí va 3 h por delante del servidor):
 *   `displayed_at ≥ fetched_at` siempre.
 * - Se envía una muestra por pase, en segundo plano: si el `POST` falla no hay segundo intento y el
 *   pase queda registrado; el tramo se marca como no medido.
 * - En el navegador el fotograma siguiente es `requestAnimationFrame`.
 */
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { MatrixScreen } from '@/pages/scr06/matrix/MatrixScreen';
import { createExposureSampler, nextAnimationFrame } from '@/pages/scr06/matrix/exposure';
import { text } from '@/texts';
import { label } from '@/texts/labels';

import { server } from '../../msw/server';
import { FakeClock } from '../../support/fakeClock';
import {
  BROWSER_NOW_MS,
  CLIP_A,
  PASS_ID,
  SESSION_ID,
  ZONE_ID,
  captureHandler,
  clipsHandler,
  clipsPage,
  clip,
  currentWalkTest,
  currentWalkTestHandler,
  frameScheduler,
  pass,
  resetTransport,
  renderScreen,
  row,
  session,
  type CapturedRequest,
  type TestSession,
} from './support';

const SELECT_ROW = text('scr06.matrix.select');
const REGISTER = text('scr06.matrix.form.submit');

/** Sesión con la matriz y, tras registrar el pase, con el pase ya en la lista. */
function sessionWith(passes: ReturnType<typeof pass>[] = []) {
  return session({
    rows: [row()],
    passes,
    passes_per_cell: 3,
  });
}

let mounted: TestSession | null = null;

beforeEach(() => {
  mounted = null;
});

afterEach(() => {
  mounted?.cache.dispose();
  mounted = null;
  resetTransport();
});

interface ExposureFixture {
  readonly sampleCalls: CapturedRequest[];
  readonly passCalls: CapturedRequest[];
  readonly scheduler: ReturnType<typeof frameScheduler>;
  readonly mounted: TestSession;
}

/** Monta la matriz con el programador de fotogramas bajo control de la prueba. */
async function openScreen(
  options: { readonly exposureFails?: boolean; readonly refetchAdvanceMs?: number } = {},
): Promise<ExposureFixture> {
  const sampleCalls: CapturedRequest[] = [];
  const passCalls: CapturedRequest[] = [];
  const scheduler = frameScheduler();
  const clock = new FakeClock(BROWSER_NOW_MS);
  let recorded = false;
  server.use(
    // La lectura devuelve la sesión con el pase registrado después de escribirlo.
    currentWalkTestHandler(currentWalkTest(sessionWith())),
    clipsHandler(clipsPage([clip()])),
    captureHandler({
      method: 'post',
      path: '/walk-tests/{session_id}/passes',
      calls: passCalls,
      response: () => {
        recorded = true;
        return HttpResponse.json(pass(), { status: 201 });
      },
    }),
    captureHandler({
      method: 'post',
      path: '/walk-tests/{session_id}/exposure-samples',
      calls: sampleCalls,
      response: () =>
        options.exposureFails === true
          ? new HttpResponse(null, { status: 503 })
          : HttpResponse.json(
              {
                sample_id: '0190a1b2-0000-7000-8000-0000000000ee',
                session_id: SESSION_ID,
                pass_id: PASS_ID,
                fetched_at: '2026-10-24T13:00:00.000Z',
                displayed_at: '2026-10-24T13:00:00.000Z',
                recorded_at: '2026-10-24T13:00:01.000Z',
              },
              { status: 201 },
            ),
    }),
  );
  // La lectura posterior a la escritura incluye el pase (invalida y vuelve a pedir la sesión).
  server.use(
    captureHandler({
      method: 'get',
      path: '/zones/{zone_id}/walk-tests/current',
      calls: [],
      response: () => {
        // La relectura que dispara la invalidación puede tardar: con `refetchAdvanceMs` el reloj
        // avanza durante ella, así que el tramo 3a no debe incluir esa espera (BR-APP-84).
        if (recorded && options.refetchAdvanceMs !== undefined) {
          clock.advance(options.refetchAdvanceMs);
        }
        return HttpResponse.json(currentWalkTest(sessionWith(recorded ? [pass()] : [])));
      },
    }),
  );
  const rendered = renderScreen(
    (renderClock) => (
      <MatrixScreen zoneId={ZONE_ID} clock={renderClock} nextFrame={scheduler.nextFrame} />
    ),
    { clock },
  );
  mounted = rendered;
  await screen.findByRole('button', { name: `${SELECT_ROW}: ${label('posture', 'standing')} 1` });
  return { sampleCalls, passCalls, scheduler, mounted: rendered };
}

function registerPass(): void {
  fireEvent.click(
    screen.getByRole('button', { name: `${SELECT_ROW}: ${label('posture', 'standing')} 1` }),
  );
  fireEvent.change(screen.getByRole('combobox', { name: text('scr06.matrix.form.evidence') }), {
    target: { value: CLIP_A },
  });
  fireEvent.click(screen.getByRole('button', { name: REGISTER }));
}

describe('muestra de exposición (BR-APP-84)', () => {
  it('una muestra por pase, con displayed_at ≥ fetched_at del mismo reloj', async () => {
    const { sampleCalls, scheduler, mounted: mountedTest } = await openScreen();
    registerPass();
    await waitFor(() => {
      expect(scheduler.pendingCount()).toBe(1);
    });
    // La muestra se programa (con el `fetched_at` ya leído): aún no se ha enviado nada.
    expect(sampleCalls).toHaveLength(0);

    mountedTest.clock.advance(2_000);
    scheduler.run();
    await waitFor(() => {
      expect(sampleCalls).toHaveLength(1);
    });
    expect(sampleCalls[0]?.url).toBe(`/walk-tests/${SESSION_ID}/exposure-samples`);
    expect(sampleCalls[0]?.body).toEqual({
      pass_id: PASS_ID,
      fetched_at: new Date(BROWSER_NOW_MS).toISOString(),
      displayed_at: new Date(BROWSER_NOW_MS + 2_000).toISOString(),
    });
    const sample = sampleCalls[0]?.body as { fetched_at: string; displayed_at: string };
    expect(Date.parse(sample.displayed_at)).toBeGreaterThanOrEqual(Date.parse(sample.fetched_at));
    // Una sola muestra para el pase, aunque el fotograma se ejecute otra vez.
    scheduler.run();
    expect(sampleCalls).toHaveLength(1);
  });

  it('fetched_at es el instante de la respuesta del pase aunque la relectura tarde', async () => {
    const {
      sampleCalls,
      scheduler,
      mounted: mountedTest,
    } = await openScreen({
      refetchAdvanceMs: 7_000,
    });
    registerPass();
    await waitFor(() => {
      expect(scheduler.pendingCount()).toBe(1);
    });
    // La relectura que sigue al pase (invalidación) ya avanzó el reloj 7 s; el tramo 3a empieza en
    // la respuesta del pase, no en esa relectura (BR-APP-84).
    expect(mountedTest.clock.now()).toBe(BROWSER_NOW_MS + 7_000);
    scheduler.run();
    await waitFor(() => {
      expect(sampleCalls).toHaveLength(1);
    });
    expect(sampleCalls[0]?.body).toEqual({
      pass_id: PASS_ID,
      fetched_at: new Date(BROWSER_NOW_MS).toISOString(),
      displayed_at: new Date(BROWSER_NOW_MS + 7_000).toISOString(),
    });
  });

  it('si la muestra falla no hay segundo intento y el pase queda registrado', async () => {
    const { sampleCalls, passCalls, scheduler } = await openScreen({ exposureFails: true });
    registerPass();
    await waitFor(() => {
      expect(passCalls).toHaveLength(1);
    });
    await waitFor(() => {
      expect(scheduler.pendingCount()).toBe(1);
    });
    scheduler.run();
    const failure = await screen.findByText(text('scr06.matrix.exposure.failed'));
    expect(failure.getAttribute('data-outcome')).toBe('failed');
    expect(sampleCalls).toHaveLength(1);
    // El pase sigue registrado y la matriz no se bloqueó: su resultado está en la lista.
    const entry = await screen.findByText(label('pass_result', 'detected'), {
      selector: '[data-part="pass_result"]',
    });
    expect(entry.closest('[data-part="pass"]')?.getAttribute('data-pass-id')).toBe(PASS_ID);
    // Ningún reintento: ni avanzando el reloj ni volviendo a ejecutar el fotograma.
    scheduler.run();
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
    expect(sampleCalls).toHaveLength(1);
  });

  it('el fotograma por omisión del navegador es requestAnimationFrame', async () => {
    expect(typeof globalThis.requestAnimationFrame).toBe('function');
    const spy = vi.spyOn(globalThis, 'requestAnimationFrame');
    server.use(
      currentWalkTestHandler(currentWalkTest(sessionWith())),
      clipsHandler(clipsPage([clip()])),
      captureHandler({
        method: 'post',
        path: '/walk-tests/{session_id}/passes',
        calls: [],
        response: () => HttpResponse.json(pass(), { status: 201 }),
      }),
    );
    const rendered = renderScreen((clock) => <MatrixScreen zoneId={ZONE_ID} clock={clock} />);
    mounted = rendered;
    await screen.findByRole('button', { name: `${SELECT_ROW}: ${label('posture', 'standing')} 1` });
    registerPass();
    await waitFor(() => {
      expect(spy).toHaveBeenCalled();
    });
  });
});

describe('createExposureSampler', () => {
  it('no puede producir displayed_at por debajo de fetched_at aunque el reloj retroceda', () => {
    const clock = new FakeClock(BROWSER_NOW_MS);
    const sent: unknown[] = [];
    let calls = 0;
    const sampler = createExposureSampler({
      // Un reloj que retrocede un segundo en cada lectura: la segunda (`displayed_at`) queda por
      // debajo de la primera (`fetched_at`) si el muestreador no las ordena.
      clock: {
        now: () => BROWSER_NOW_MS - calls++ * 1_000,
        setTimeout: (callback, delayMs) => clock.setTimeout(callback, delayMs),
        clearTimeout: (handle) => {
          clock.clearTimeout(handle);
        },
      },
      nextFrame: (callback) => {
        callback();
      },
      send: (sample) => {
        sent.push(sample);
        return Promise.resolve(null);
      },
    });
    sampler.sample(PASS_ID, clock.now());
    const sample = sent[0] as { fetched_at: string; displayed_at: string };
    expect(Date.parse(sample.displayed_at)).toBeGreaterThanOrEqual(Date.parse(sample.fetched_at));
    expect(sample.displayed_at).toBe(sample.fetched_at);
  });

  it('un envío que falla no se repite ni lanza', async () => {
    const clock = new FakeClock(BROWSER_NOW_MS);
    let calls = 0;
    const sampler = createExposureSampler({
      clock,
      nextFrame: (callback) => {
        callback();
      },
      send: () => {
        calls += 1;
        return Promise.reject(new Error('503'));
      },
    });
    sampler.sample(PASS_ID, clock.now());
    sampler.sample('0190a1b2-0000-7000-8000-0000000000dd', clock.now());
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
    expect(calls).toBe(2);
    expect(sampler.samples).toHaveLength(2);
  });

  it('sin requestAnimationFrame usa una macrotarea', async () => {
    const original = globalThis.requestAnimationFrame;
    // @ts-expect-error -- se simula un navegador sin la API para probar el respaldo.
    delete globalThis.requestAnimationFrame;
    const ran: string[] = [];
    nextAnimationFrame(() => {
      ran.push('frame');
    });
    expect(ran).toEqual([]);
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
    expect(ran).toEqual(['frame']);
    globalThis.requestAnimationFrame = original;
  });
});
