/**
 * Consumo de la API en reposo (NFR-APP-05) y varias pestañas (NFR-APP-06) sobre el armazón real
 * (`App`, `createAppServices`, transporte, caché y puertas de refresco reales) con las pantallas
 * reales y MSW con datos sintéticos válidos:
 *
 * - con la bandeja o la cola, las notificaciones (campana de la cabecera) y el panel de salud
 *   abiertos, ninguna ventana de un minuto pasa de 30 peticiones durante 5 minutos simulados, y el
 *   total no pasa de 150; se exige además que el refresco periódico ocurra (la prueba no puede
 *   pasar porque la pantalla dejó de pedir) y que ninguna respuesta falle (el circuito no se abre);
 * - con la pestaña oculta, cero peticiones en 5 minutos; al volver a verse, el refresco sigue;
 * - teclear en los filtros o en la página no pide nada; solo «Aplicar» pide;
 * - dos pestañas (dos `createAppServices`) no comparten la caché en memoria: la segunda pide sus
 *   datos y nada de la primera queda en un almacenamiento compartido.
 *
 * El minuto se simula con el `FakeClock` de los servicios: el único refresco periódico es el tic
 * de 60 s de `state.query` (`src/state/query/client.ts`).
 */
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { SCREENS } from '@/app/screens';
import type { QueueItem } from '@/ports/scr10ReviewQueue';
import { text } from '@/texts';

import { server } from '../msw/server';
import { node } from '../components/scr07/support';
import { finding, gate, page, plant, zone, PLANT } from '../components/scr08/support';
import { notification } from '../components/scr15/support';
import { mountShell, ORIGIN, serveSession, shellMe, type ShellFixture } from '../support/appShell';
import { settle } from '../support/queryCache';

const WAIT = { timeout: 10_000 };
const MINUTE_MS = 60_000;
const MINUTES = 5;
/** NFR-APP-05 `[objetivo propio]`: 5 % del límite de 600 por minuto de NFR-NUC-25. */
const PER_MINUTE_BUDGET = 30;
const PERMISSIONS = [
  'findings.read',
  'coverage.read',
  'evidence.read',
  'labels.read',
  'notifications.read',
  'fleet.read',
];
const UNREAD_PAGE = 200;
/** Cursores opacos sintéticos: la página `i + 1` de no leídas se pide con `CURSORS[i]`. */
const CURSORS = ['cGFnZS0x', 'cGFnZS0y', 'cGFnZS0z', 'cGFnZS00', 'cGFnZS01', 'cGFnZS02'];

interface Request {
  readonly at: number;
  readonly method: string;
  readonly path: string;
}

const requests: Request[] = [];
const failures: string[] = [];
let clockNow: () => number = () => 0;

beforeEach(() => {
  requests.length = 0;
  failures.length = 0;
  server.events.on('request:start', ({ request }) => {
    const url = new URL(request.url);
    requests.push({ at: clockNow(), method: request.method, path: url.pathname });
  });
  server.events.on('response:mocked', ({ request, response }) => {
    if (!response.ok) failures.push(`${request.method} ${new URL(request.url).pathname}`);
  });
});

afterEach(() => {
  server.events.removeAllListeners();
  setVisibility('visible');
});

let visibility: DocumentVisibilityState = 'visible';
Object.defineProperty(document, 'visibilityState', {
  configurable: true,
  get: () => visibility,
});
function setVisibility(state: DocumentVisibilityState): void {
  visibility = state;
  document.dispatchEvent(new Event('visibilitychange'));
}

function queueItem(index: number): QueueItem {
  return {
    detection_id: `0190a1b2-4444-7000-8000-${String(index).padStart(12, '0')}`,
    plant_id: PLANT,
    zone_id: zone().zone_id,
    family: 'coexistence',
    standard_id: '0190a1b2-3333-7000-8000-000000000001',
    standard_version: 1,
    max_confidence: 0.65,
    review_threshold: 0.5,
    publication_threshold: 0.8,
    review_reason: 'low_confidence',
    observability_state_at_event: 'observable',
    occurred_at: '2026-01-01T11:00:00.000Z',
    received_at: '2026-01-01T11:00:00.000Z',
    review_due_at: '2026-01-02T11:00:00.000Z',
    reminders_sent: 0,
  };
}

/**
 * Respuestas sintéticas válidas de toda lectura de las pantallas medidas. `unreadPages` es el
 * número de páginas de 200 no leídas que la campana recorre en cada sondeo (VIG-318).
 */
function serveApi(options: { readonly unreadPages?: number } = {}): void {
  const unreadPages = options.unreadPages ?? 1;
  serveSession(shellMe(PERMISSIONS));
  const findings = Array.from({ length: 50 }, (_, index) => finding(index + 1));
  server.use(
    http.get(`${ORIGIN}/hierarchy`, () =>
      HttpResponse.json({
        organization: shellMe(PERMISSIONS).organization,
        plants: [plant()],
        nodes: [],
      }),
    ),
    http.get(`${ORIGIN}/zones/:zone_id/gates`, ({ params }) =>
      HttpResponse.json(gate(String(params.zone_id))),
    ),
    http.get(`${ORIGIN}/zones/:zone_id/regression`, ({ params }) =>
      HttpResponse.json({
        zone_id: params.zone_id,
        state: 'current',
        marked_at: null,
        cause: null,
        catalog_version: null,
        model_version: null,
        affected_row_ids: null,
      }),
    ),
    // El inicio del coordinador pide la cobertura al abrir, una vez por apertura (VIG-365).
    http.get(`${ORIGIN}/zones/:zone_id/coverage/at`, ({ params }) =>
      HttpResponse.json({
        zone_id: params.zone_id,
        instant: '2026-01-01T12:00:00.000Z',
        state: 'observable',
        causes: [],
        layer: 'platform',
        clock_basis: 'platform',
        source_record_ids: [],
      }),
    ),
    http.get(`${ORIGIN}/loop/findings`, () => HttpResponse.json(page(findings))),
    http.get(`${ORIGIN}/loop/review-queue`, () =>
      HttpResponse.json({
        items: Array.from({ length: 50 }, (_, index) => queueItem(index + 1)),
        next_cursor: null,
      }),
    ),
    http.get(`${ORIGIN}/loop/overdue`, () =>
      HttpResponse.json({
        plant_id: PLANT,
        highlight_threshold_days: 3,
        items: [],
        zones: [],
        partition_notice: null,
        next_cursor: null,
      }),
    ),
    http.get(`${ORIGIN}/fleet/nodes`, () =>
      HttpResponse.json({
        items: Array.from({ length: 50 }, (_, index) => node(index + 1)),
        next_cursor: null,
      }),
    ),
    http.get(`${ORIGIN}/notifications`, ({ request }) => {
      const after = new URL(request.url).searchParams.get('after');
      const index = after === null ? 0 : CURSORS.indexOf(after) + 1;
      return HttpResponse.json({
        items: Array.from({ length: unreadPages === 1 ? 3 : UNREAD_PAGE }, (_, item) =>
          notification(
            `0190a1b2-5555-7000-8000-${String(index * UNREAD_PAGE + item + 1).padStart(12, '0')}`,
          ),
        ),
        next_after: index + 1 < unreadPages ? (CURSORS[index] ?? null) : null,
      });
    }),
  );
}

/**
 * Monta la aplicación en `path` sin `StrictMode`: el doble montaje de desarrollo duplica y aborta
 * peticiones que la construcción de producción no hace. Espera a que la pantalla pida `read`.
 */
async function mount(path: string, read: string): Promise<ShellFixture> {
  const before = live(read);
  const fixture = mountShell(path, SCREENS, { strict: false });
  clockNow = () => fixture.clock.now();
  await waitFor(() => {
    expect(live(read)).toBeGreaterThan(before);
  }, WAIT);
  await quiet(fixture);
  return fixture;
}

/** Deja que terminen las peticiones en vuelo y sus renders (sin avanzar el reloj). */
async function quiet(fixture: ShellFixture): Promise<void> {
  await waitFor(() => {
    expect(fixture.services.cache.client.isFetching()).toBe(0);
  }, WAIT);
  await act(async () => {
    await settle(5);
  });
}

/** Avanza un minuto simulado y devuelve las peticiones que salieron en él. */
async function minute(fixture: ShellFixture): Promise<Request[]> {
  const before = requests.length;
  await act(async () => {
    fixture.clock.advance(MINUTE_MS);
    await settle(5);
  });
  await quiet(fixture);
  return requests.slice(before);
}

/**
 * Peticiones por ventana de un minuto en [0, 5 min): la primera es la carga inicial (t = 0) y cada
 * una de las siguientes contiene el tic de su inicio (60, 120, 180 y 240 s).
 */
async function fiveMinutes(fixture: ShellFixture): Promise<number[]> {
  const windows = [requests.length];
  for (let index = 1; index < MINUTES; index++) windows.push((await minute(fixture)).length);
  return windows;
}

function live(path: string): number {
  return requests.filter((request) => request.path === path).length;
}

describe('NFR-APP-05: consumo de la API en reposo', { timeout: 120_000 }, () => {
  it.each([
    // Inicio del coordinador: bandeja y cola en el panel, campana y semáforo de observabilidad.
    { path: '/home', screenRead: '/loop/findings', unreadPages: 1 },
    { path: '/findings', screenRead: '/loop/findings', unreadPages: 1 },
    { path: '/review-queue', screenRead: '/loop/review-queue', unreadPages: 1 },
    // Panel de salud de la flota (SCR-07).
    { path: '/fleet', screenRead: '/fleet/nodes', unreadPages: 1 },
    // Peor caso de la campana: 1 000 no leídas, cinco páginas por sondeo (VIG-318).
    { path: '/home', screenRead: '/loop/findings', unreadPages: 5 },
  ])(
    '$path con $unreadPages página(s) de no leídas: ≤ 30 por minuto en 5 minutos',
    async ({ path, screenRead, unreadPages }) => {
      serveApi({ unreadPages });
      const fixture = await mount(path, screenRead);
      const initialScreenReads = live(screenRead);
      const initialBell = live('/notifications');
      expect(initialScreenReads).toBeGreaterThan(0);
      expect(initialBell).toBe(unreadPages);

      const windows = await fiveMinutes(fixture);
      process.stdout.write(
        `[presupuesto-api] ${path} (${String(unreadPages)} pág. no leídas): ${windows.join(', ')} por minuto; total ${String(requests.length)}\n`,
      );
      for (const count of windows) expect(count).toBeLessThanOrEqual(PER_MINUTE_BUDGET);
      expect(requests.length).toBeLessThanOrEqual(PER_MINUTE_BUDGET * MINUTES);
      // El refresco periódico ocurrió en cada minuto: la cifra no sale de una pantalla parada.
      expect(live(screenRead)).toBe(initialScreenReads * MINUTES);
      expect(live('/notifications')).toBe(unreadPages * MINUTES);
      expect(failures).toEqual([]);
      expect(fixture.services.connection.canRead('platform')).toBe(true);
    },
  );

  it('con la pestaña oculta no sale ninguna petición en 5 minutos; al volver se reanuda', async () => {
    serveApi();
    const fixture = await mount('/home', '/loop/findings');
    const loaded = requests.length;
    expect(loaded).toBeGreaterThan(0);

    setVisibility('hidden');
    for (let index = 0; index < MINUTES; index++) expect(await minute(fixture)).toEqual([]);
    expect(requests.length).toBe(loaded);

    setVisibility('visible');
    const resumed = await minute(fixture);
    expect(resumed.map((request) => request.path)).toContain('/notifications');
    expect(resumed.map((request) => request.path)).toContain('/loop/findings');
    expect(failures).toEqual([]);
  });

  it('teclear en los filtros o en la página no pide nada; solo «Aplicar» pide', async () => {
    serveApi();
    const user = userEvent.setup();
    await mount('/findings', '/loop/findings');
    const loaded = requests.length;
    const form = screen.getByRole('form', { name: text('scr08.inbox.filters') });

    for (const select of within(form).getAllByRole('combobox')) {
      select.focus();
      await user.keyboard('{ArrowDown}{ArrowDown}{ArrowUp}');
    }
    const from = form.querySelector<HTMLInputElement>('input[name="from"]');
    if (from === null) throw new Error('Falta el campo «desde» de la bandeja.');
    await user.type(from, '2026-01-01');
    await user.keyboard('abcdefghij{Tab}{Tab}{Escape}');
    fireEvent.keyDown(document.body, { key: 'r' });
    await act(async () => {
      await settle(5);
    });
    expect(requests.slice(loaded)).toEqual([]);

    // Control: un filtro distinto aplicado con el botón sí pide, una sola vez.
    const family = form.querySelector<HTMLSelectElement>('select[name="family"]');
    const option = family?.options[1];
    if (family === null || option === undefined) throw new Error('Falta el filtro de familia.');
    await user.selectOptions(family, option);
    await act(async () => {
      await settle(5);
    });
    expect(requests.slice(loaded)).toEqual([]);
    await user.click(within(form).getByRole('button', { name: text('scr08.inbox.apply') }));
    await waitFor(() => {
      expect(requests.slice(loaded).map((request) => request.path)).toEqual(['/loop/findings']);
    }, WAIT);
  });
});

describe('NFR-APP-06: dos pestañas no comparten caché en memoria', { timeout: 120_000 }, () => {
  it('la segunda pestaña pide sus propios datos y nada queda en almacenamiento compartido', async () => {
    serveApi();
    const first = await mount('/findings', '/loop/findings');
    expect(live('/loop/findings')).toBe(1);
    const firstEntries = first.services.cache.client.getQueryCache().getAll();
    expect(firstEntries.some((entry) => entry.queryKey[1] === 'GET /loop/findings')).toBe(true);

    const second = await mount('/findings', '/loop/findings');
    expect(second.services.cache.client).not.toBe(first.services.cache.client);
    // La segunda pestaña no encontró la bandeja de la primera: la pidió ella.
    expect(live('/loop/findings')).toBe(2);
    const secondEntries = second.services.cache.client.getQueryCache().getAll();
    for (const entry of secondEntries) expect(firstEntries).not.toContain(entry);

    // Ningún dato de la API queda donde otra pestaña podría leerlo.
    const shared = Object.keys(localStorage)
      .map((key) => `${key}=${localStorage.getItem(key) ?? ''}`)
      .join('\n');
    expect(shared).not.toContain(finding(1).finding_state_id);
    expect(shared).not.toContain(notification().title_es);
  });
});
