/**
 * Prueba de oclusión por cámara (IF-13 paso 5, nota v1.3; TASK-435).
 *
 * - Una prueba `pending` muestra «verificando» y su plazo; al vencer el plazo (reloj simulado) la
 *   pantalla vuelve a consultar **solo** la sesión, una vez, y pinta el resultado final. Ninguna
 *   otra lectura se pide y, resuelta la prueba, no queda nada programado.
 * - El registro envía el cuerpo exacto del contrato; el motivo declarado solo viaja si se escribe.
 * - `no_observability_events_in_window` se explica con su texto; los rechazos del servidor se
 *   anclan al campo.
 */
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';
import { axe } from 'vitest-axe';

import { OcclusionScreen } from '@/pages/scr06/occlusion/OcclusionScreen';
import { scr06CommissioningPort, type OcclusionTest } from '@/ports/scr06Commissioning';
import { label } from '@/texts/labels';
import { text } from '@/texts';

import { apiError, mswPath } from '../../msw/handlers';
import { server } from '../../msw/server';
import {
  CAMERA_A,
  EVENT_ID,
  OCCLUSION_TEST_ID,
  SESSION_ID,
  ZONE_ID,
  captureHandler,
  currentWalkTest,
  currentWalkTestHandler,
  occlusionTest,
  renderScreen,
  resetTransport,
  session,
  type CapturedRequest,
  type TestSession,
} from './support';

const WAIT = { timeout: 10_000 };
/** Tope de cada prueba: por encima de las esperas de `WAIT` (lección de VIG-300). */
const TEST_TIMEOUT = 30_000;
const REQUIRED = `(${text('components_forms.reason.required')})`;
const CAMERA_LABEL = `${text('scr06.occlusion.form.camera')} ${REQUIRED}`;
const START_LABEL = `${text('scr06.occlusion.form.started_at')} ${REQUIRED}`;
const END_LABEL = `${text('scr06.occlusion.form.ended_at')} ${REQUIRED}`;
const REASON_LABEL = text('scr06.occlusion.form.declared_reason');
const SUBMIT_LABEL = text('scr06.occlusion.form.submit');
/** Navegador 4 min antes del plazo de la prueba sintética (10:10:00.000Z). */
const BEFORE_DEADLINE_MS = Date.parse('2026-10-24T10:06:00.000Z');
const TO_DEADLINE_MS = 4 * 60_000;

let mounted: TestSession | null = null;
/** Todas las peticiones que salen hacia la API: método y ruta. */
let requests: string[] = [];
/**
 * Lecturas de la sesión por el puerto: la caché llama a `read` en el mismo instante en que vence
 * el temporizador, así que cuenta también lo que aún no ha salido a la red.
 */
let sessionReads: MockInstance;

beforeEach(() => {
  mounted = null;
  requests = [];
  server.events.on('request:start', ({ request }) => {
    requests.push(`${request.method} ${new URL(request.url).pathname}`);
  });
  sessionReads = vi.spyOn(scr06CommissioningPort.currentWalkTest, 'read');
});

afterEach(() => {
  vi.restoreAllMocks();
  server.events.removeAllListeners();
  mounted?.cache.dispose();
  mounted = null;
  resetTransport();
});

function openScreen(nowMs = BEFORE_DEADLINE_MS): TestSession {
  const rendered = renderScreen((clock) => <OcclusionScreen zoneId={ZONE_ID} clock={clock} />, {
    nowMs,
  });
  mounted = rendered;
  return rendered;
}

const CURRENT = `GET /zones/${ZONE_ID}/walk-tests/current`;

function withTests(tests: readonly OcclusionTest[]) {
  return currentWalkTest(session({ occlusion_tests: tests.map((test) => ({ ...test })) }));
}

function verificationOf(testId: string): string | null {
  return (
    document
      .querySelector(`[data-occlusion-test="${testId}"]`)
      ?.getAttribute('data-verification') ?? null
  );
}

/** Avanza el reloj simulado y deja que React y la caché procesen lo que vence. */
async function advance(clock: TestSession['clock'], ms: number): Promise<void> {
  clock.advance(ms);
  await Promise.resolve();
}

describe('oclusión pending: plazo y nueva consulta de la sesión', { timeout: TEST_TIMEOUT }, () => {
  it('muestra el plazo, vuelve a consultar solo la sesión al vencer y pinta el resultado', async () => {
    let resolved = false;
    server.use(
      http.get(mswPath('/zones/:zone_id/walk-tests/current'), () =>
        HttpResponse.json(
          withTests([
            resolved
              ? occlusionTest({ verification: 'verified', correlated_event_ids: [EVENT_ID] })
              : occlusionTest(),
          ]),
        ),
      ),
    );
    const { clock } = openScreen();
    const pending = await screen.findByText(
      label('occlusion_verification', 'pending'),
      {
        selector: '[data-part="verification"]',
      },
      WAIT,
    );
    expect(pending).toBeTruthy();
    // VIG-366: el instante del servidor, con la zona nombrada (fuera del armazón, UTC).
    expect(
      document
        .querySelector('[data-part="deadline"] [data-part="server_instant"] time')
        ?.getAttribute('dateTime'),
    ).toBe('2026-10-24T10:10:00.000Z');
    expect(requests).toEqual([CURRENT]);
    expect(sessionReads).toHaveBeenCalledTimes(1);

    // Justo antes del plazo (más el margen) no se consulta nada.
    await advance(clock, TO_DEADLINE_MS);
    expect(sessionReads).toHaveBeenCalledTimes(1);

    resolved = true;
    await advance(clock, 1_000);
    expect(sessionReads).toHaveBeenCalledTimes(2);
    await waitFor(() => {
      expect(verificationOf(OCCLUSION_TEST_ID)).toBe('verified');
    }, WAIT);
    expect(requests).toEqual([CURRENT, CURRENT]);
    expect(
      screen.getByText(label('occlusion_verification', 'verified'), {
        selector: '[data-part="verification"]',
      }),
    ).toBeTruthy();
    expect(document.querySelector('[data-part="deadline"]')).toBeNull();

    // Resuelta, no queda nada programado: ni la sesión ni otra lectura se vuelven a pedir.
    await advance(clock, 60 * 60_000);
    expect(sessionReads).toHaveBeenCalledTimes(2);
    expect(requests).toEqual([CURRENT, CURRENT]);
  });

  it('si el servidor sigue pending tras el plazo, vuelve a consultar a los 30 s', async () => {
    let reads = 0;
    server.use(
      http.get(mswPath('/zones/:zone_id/walk-tests/current'), () => {
        reads += 1;
        // La segunda respuesta sigue `pending` (el reloj del servidor va por detrás) y ya cuenta
        // un evento: así la prueba sabe cuándo la pantalla la pintó.
        return HttpResponse.json(
          withTests([
            reads >= 3
              ? occlusionTest({
                  verification: 'failed',
                  failure_reason: 'no_observability_events_in_window',
                })
              : occlusionTest({ correlated_event_ids: reads === 2 ? [EVENT_ID] : [] }),
          ]),
        );
      }),
    );
    const { clock } = openScreen();
    await screen.findByText(
      label('occlusion_verification', 'pending'),
      {
        selector: '[data-part="verification"]',
      },
      WAIT,
    );
    await advance(clock, TO_DEADLINE_MS + 1_000);
    await screen.findByText('1', { selector: '[data-part="correlated_events"]' }, WAIT);
    expect(reads).toBe(2);
    await advance(clock, 29_999);
    expect(sessionReads).toHaveBeenCalledTimes(2);
    await advance(clock, 1);
    expect(sessionReads).toHaveBeenCalledTimes(3);
    await waitFor(() => {
      expect(verificationOf(OCCLUSION_TEST_ID)).toBe('failed');
    }, WAIT);
    expect(reads).toBe(3);
    await advance(clock, 60 * 60_000);
    expect(sessionReads).toHaveBeenCalledTimes(3);
    expect(requests).toEqual([CURRENT, CURRENT, CURRENT]);
  });

  it('una prueba fallida sin eventos en la ventana explica la causa', async () => {
    server.use(
      currentWalkTestHandler(
        withTests([
          occlusionTest({
            verification: 'failed',
            failure_reason: 'no_observability_events_in_window',
          }),
        ]),
      ),
    );
    const { clock } = openScreen();
    const reason = await screen.findByText(
      label('catalog_detail_code', 'catalog_no_observability_events_in_window'),
      {},
      WAIT,
    );
    expect(
      reason.closest('[data-part="failure_reason"]')?.getAttribute('data-failure-reason'),
    ).toBe('no_observability_events_in_window');
    expect(
      document.querySelector(
        '[data-text-key="scr06.occlusion.failure.no_observability_events_in_window"]',
      ),
    ).not.toBeNull();
    // Resuelta: nada vuelve a consultar la sesión.
    await advance(clock, 60 * 60_000);
    expect(sessionReads).toHaveBeenCalledTimes(1);
    expect(requests).toEqual([CURRENT]);
  });
});

describe('regresiones de PR-APP-07', { timeout: TEST_TIMEOUT }, () => {
  it('una causa de fallo con nombre del prototipo («toString») no rompe la lista (FC_SEED=1618033)', async () => {
    server.use(
      currentWalkTestHandler(
        withTests([occlusionTest({ verification: 'failed', failure_reason: 'toString' })]),
      ),
    );
    openScreen();
    await waitFor(() => {
      expect(verificationOf(OCCLUSION_TEST_ID)).toBe('failed');
    }, WAIT);
    const reason = document.querySelector('[data-part="failure_reason"]');
    expect(reason?.getAttribute('data-failure-reason')).toBe('toString');
    expect(reason?.querySelector('[data-text-key]')).toBeNull();
  });
});

describe('registrar la prueba', { timeout: TEST_TIMEOUT }, () => {
  async function fillWindow(): Promise<void> {
    fireEvent.change(await screen.findByRole('textbox', { name: CAMERA_LABEL }, WAIT), {
      target: { value: CAMERA_A },
    });
    fireEvent.change(screen.getByRole('textbox', { name: START_LABEL }), {
      target: { value: '2026-10-24T10:03:00.000Z' },
    });
    fireEvent.change(screen.getByRole('textbox', { name: END_LABEL }), {
      target: { value: '2026-10-24T10:05:00.000Z' },
    });
  }

  it('envía el cuerpo exacto sin motivo y muestra la prueba pending con su plazo', async () => {
    const calls: CapturedRequest[] = [];
    let recorded = false;
    server.use(
      http.get(mswPath('/zones/:zone_id/walk-tests/current'), () =>
        HttpResponse.json(withTests(recorded ? [occlusionTest()] : [])),
      ),
      captureHandler({
        method: 'post',
        path: '/walk-tests/{session_id}/occlusion-tests',
        calls,
        response: () => {
          recorded = true;
          return HttpResponse.json(occlusionTest(), { status: 201 });
        },
      }),
    );
    openScreen();
    await fillWindow();
    fireEvent.click(screen.getByRole('button', { name: SUBMIT_LABEL }));
    await waitFor(() => {
      expect(calls).toHaveLength(1);
    }, WAIT);
    expect(calls[0]?.url).toBe(`/walk-tests/${SESSION_ID}/occlusion-tests`);
    expect(calls[0]?.body).toEqual({
      camera_id: CAMERA_A,
      started_at: '2026-10-24T10:03:00.000Z',
      ended_at: '2026-10-24T10:05:00.000Z',
    });
    await waitFor(() => {
      expect(verificationOf(OCCLUSION_TEST_ID)).toBe('pending');
    }, WAIT);
    expect(document.querySelector('[data-part="deadline"]')).not.toBeNull();
  });

  it('con resultado declarado envía declared_reason_es', async () => {
    const calls: CapturedRequest[] = [];
    server.use(
      currentWalkTestHandler(withTests([])),
      captureHandler({
        method: 'post',
        path: '/walk-tests/{session_id}/occlusion-tests',
        calls,
        response: () =>
          HttpResponse.json(
            occlusionTest({
              verification: 'declared',
              declared_reason_es: 'La cámara de respaldo se revisó en sitio.',
            }),
            { status: 201 },
          ),
      }),
    );
    openScreen();
    await fillWindow();
    fireEvent.change(screen.getByRole('textbox', { name: REASON_LABEL }), {
      target: { value: 'La cámara de respaldo se revisó en sitio.' },
    });
    fireEvent.click(screen.getByRole('button', { name: SUBMIT_LABEL }));
    await waitFor(() => {
      expect(calls).toHaveLength(1);
    }, WAIT);
    expect(calls[0]?.body).toEqual({
      camera_id: CAMERA_A,
      started_at: '2026-10-24T10:03:00.000Z',
      ended_at: '2026-10-24T10:05:00.000Z',
      declared_reason_es: 'La cámara de respaldo se revisó en sitio.',
    });
  });

  it('no envía una ventana con el fin anterior al inicio', async () => {
    const calls: CapturedRequest[] = [];
    server.use(
      currentWalkTestHandler(withTests([])),
      captureHandler({
        method: 'post',
        path: '/walk-tests/{session_id}/occlusion-tests',
        calls,
        response: () => HttpResponse.json(occlusionTest(), { status: 201 }),
      }),
    );
    openScreen();
    await fillWindow();
    fireEvent.change(screen.getByRole('textbox', { name: END_LABEL }), {
      target: { value: '2026-10-24T10:03:00.000Z' },
    });
    fireEvent.click(screen.getByRole('button', { name: SUBMIT_LABEL }));
    await waitFor(() => {
      expect(
        document.querySelector('[data-text-key="scr06.occlusion.form.window_order"]'),
      ).not.toBeNull();
    }, WAIT);
    expect(calls).toHaveLength(0);
  });

  it('el rechazo del motivo por la política de texto libre se ancla al campo', async () => {
    server.use(
      currentWalkTestHandler(withTests([])),
      http.post(mswPath('/walk-tests/:session_id/occlusion-tests'), () =>
        apiError('invalid_request', 422, { detailCode: 'catalog_free_text_rejected' }),
      ),
    );
    openScreen();
    await fillWindow();
    const reason = screen.getByRole('textbox', { name: REASON_LABEL });
    fireEvent.change(reason, { target: { value: 'corto' } });
    fireEvent.click(screen.getByRole('button', { name: SUBMIT_LABEL }));
    await waitFor(() => {
      expect(reason.getAttribute('aria-invalid')).toBe('true');
    }, WAIT);
    const anchored = reason.parentElement?.querySelector('[role="alert"]');
    expect(anchored?.getAttribute('data-code')).toBe('invalid_request');
    // Lo escrito se conserva.
    expect((reason as HTMLTextAreaElement).value).toBe('corto');
  });

  it('«declarar» copia la cámara y la ventana de una prueba pending al formulario', async () => {
    server.use(currentWalkTestHandler(withTests([occlusionTest()])));
    openScreen();
    fireEvent.click(
      await screen.findByRole('button', { name: text('scr06.occlusion.list.declare') }, WAIT),
    );
    expect(screen.getByRole<HTMLInputElement>('textbox', { name: CAMERA_LABEL }).value).toBe(
      CAMERA_A,
    );
    expect(screen.getByRole<HTMLInputElement>('textbox', { name: START_LABEL }).value).toBe(
      '2026-10-24T10:03:00.000Z',
    );
    expect(screen.getByRole<HTMLInputElement>('textbox', { name: END_LABEL }).value).toBe(
      '2026-10-24T10:05:00.000Z',
    );
  });
});

describe('estados comunes', { timeout: TEST_TIMEOUT }, () => {
  it('sin sesión muestra el estado de la consola sin programar nada', async () => {
    server.use(currentWalkTestHandler(currentWalkTest(null)));
    const { clock } = openScreen();
    expect(await screen.findByText(text('scr06.matrix.empty'), {}, WAIT)).toBeTruthy();
    await advance(clock, 60 * 60_000);
    expect(sessionReads).toHaveBeenCalledTimes(1);
    expect(requests).toEqual([CURRENT]);
  });

  it('axe no encuentra violaciones serias en la prueba de oclusión', async () => {
    server.use(
      currentWalkTestHandler(
        withTests([
          occlusionTest(),
          occlusionTest({
            test_id: '0190a1b2-0000-7000-8000-0000000000aa',
            verification: 'failed',
            failure_reason: 'redundancy_not_verified',
          }),
        ]),
      ),
    );
    const { result } = openScreen();
    await screen.findByText(
      label('occlusion_verification', 'pending'),
      {
        selector: '[data-part="verification"]',
      },
      WAIT,
    );
    const results = await axe(result.container);
    const serious = results.violations.filter(
      (violation) => violation.impact === 'serious' || violation.impact === 'critical',
    );
    expect(serious).toEqual([]);
    expect(results).toHaveNoViolations();
  });
});
