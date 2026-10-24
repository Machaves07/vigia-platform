/**
 * PR-APP-04 ampliada con D-4 (BR-APP-14, nota de BLM §11): para toda secuencia de eventos
 * (respuesta, tic de reloj, acción de la persona),
 *
 * - el aviso de inactividad se emite exactamente una vez por ventana, a los 28 min de la última
 *   respuesta autenticada, y nunca si hubo una respuesta dentro de la ventana;
 * - tras 25 min sin acción de la persona no sale ninguna petición periódica hasta la siguiente
 *   acción, y con acción reciente el refresco de 60 s sí sale.
 *
 * Todo es real salvo la red: el almacén de `state.session`, la puerta de actividad (escuchando
 * eventos del DOM) y la caché de `state.query` con su refresco periódico. El servidor es un modelo
 * de BR-NUC-25: cada petición aceptada renueva la inactividad 30 min; vencida, responde
 * `unauthenticated`. El tiempo es el de `FakeClock`.
 */
import { QueryObserver } from '@tanstack/react-query';
import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import {
  createScreenQueryCache,
  PERIODIC_INTERVAL_MS,
  policyFor,
  screenKey,
  screenQueryOptions,
} from '@/state/query';
import {
  ACTIVITY_EVENTS,
  ACTIVITY_GATE_MS,
  createActivityGate,
  createSessionStore,
  IDLE_TIMEOUT_MS,
  IDLE_WARNING_BEFORE_MS,
  isAuthenticated,
  needsReauth,
} from '@/state/session';

import { FakeClock } from '../support/fakeClock';
import { meResponse } from '../support/session';
import { PROPERTY_TIMEOUT_MS } from '../support/timeouts';

const MINUTE_MS = 60_000;
const ABSOLUTE_MS = 12 * 60 * MINUTE_MS;
const WARNING_AFTER_MS = IDLE_TIMEOUT_MS - IDLE_WARNING_BEFORE_MS;
/** Menor que el periodo del refresco: como mucho un tic periódico por tramo. */
const CHUNK_MS = 50_000;

type Step =
  | { readonly kind: 'wait'; readonly ms: number }
  | { readonly kind: 'action'; readonly event: (typeof ACTIVITY_EVENTS)[number] }
  /** Una lectura o escritura de la pantalla (no periódica). */
  | { readonly kind: 'request' }
  /** «Seguir trabajando» del aviso: interacción y `GET /me`. */
  | { readonly kind: 'continue' }
  /** La persona vuelve a la pestaña: el almacén reevalúa los vencimientos. */
  | { readonly kind: 'visible' }
  /** Reautenticación en la misma pestaña tras perder la sesión. */
  | { readonly kind: 'reauth' };

const stepArb: fc.Arbitrary<Step> = fc.oneof(
  {
    weight: 4,
    arbitrary: fc
      .oneof(
        fc.integer({ min: 0, max: 3 * MINUTE_MS }),
        // Alrededor de la puerta (25 min) y del aviso (28 min), y más allá del vencimiento.
        fc.integer({ min: 24 * MINUTE_MS, max: 29 * MINUTE_MS }),
        fc.integer({ min: 0, max: 32 * MINUTE_MS }),
      )
      .map((ms) => ({ kind: 'wait' as const, ms })),
  },
  {
    weight: 1,
    arbitrary: fc
      .constantFrom(...ACTIVITY_EVENTS)
      .map((event) => ({ kind: 'action' as const, event })),
  },
  { weight: 1, arbitrary: fc.constant({ kind: 'request' as const }) },
  { weight: 1, arbitrary: fc.constant({ kind: 'continue' as const }) },
  { weight: 3, arbitrary: fc.constant({ kind: 'visible' as const }) },
  { weight: 1, arbitrary: fc.constant({ kind: 'reauth' as const }) },
);

type Entry =
  | { readonly type: 'response' | 'warning' | 'periodic' | 'action'; readonly at: number }
  | { readonly type: 'rejected'; readonly at: number };

async function microtasks(): Promise<void> {
  for (let round = 0; round < 8; round += 1) await Promise.resolve();
}

/** Ejecuta la secuencia y devuelve el registro ordenado de lo ocurrido. */
async function run(start: number, steps: readonly Step[]) {
  const clock = new FakeClock(start);
  const log: Entry[] = [];
  const target = new EventTarget();
  const gate = createActivityGate({ clock, target });
  // Modelo del servidor (BR-NUC-25): la sesión nace con el inicio de sesión en `start`.
  const server = { alive: true, lastSeen: start, absolute: start + ABSOLUTE_MS };

  const store = createSessionStore({
    clock,
    port: {
      me: {
        read: () => {
          if (!serve()) return Promise.reject(new Error('unauthenticated'));
          return Promise.resolve(
            meResponse({
              idleExpiresAt: clock.now() + IDLE_TIMEOUT_MS,
              absoluteExpiresAt: server.absolute,
            }),
          );
        },
      },
      logout: { write: () => Promise.resolve(undefined) },
    },
    cache: { clearAll: () => undefined },
    clearPreferences: () => undefined,
    currentRoute: () => '/inbox',
    navigate: () => undefined,
    storages: [],
    onNotice: (notice) => {
      if (notice === 'idle_warning') log.push({ type: 'warning', at: clock.now() });
    },
  });

  /** Una petición autenticada contra el modelo, avisada como lo haría `ports.base`. */
  function serve(): boolean {
    const now = clock.now();
    const ok = server.alive && now < server.lastSeen + IDLE_TIMEOUT_MS && now < server.absolute;
    if (ok) {
      server.lastSeen = now;
      log.push({ type: 'response', at: now });
      store.transportHooks.onAuthenticatedResponse();
    } else {
      server.alive = false;
      log.push({ type: 'rejected', at: now });
      store.transportHooks.onSessionEvent('unauthenticated');
    }
    return ok;
  }

  const cache = createScreenQueryCache({
    clock,
    gates: { isVisible: () => true, canReadPlatform: () => true, isActive: () => gate.isOpen() },
  });
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
      read: () => {
        log.push({ type: 'periodic', at: clock.now() });
        serve();
        return Promise.resolve({ items: [] });
      },
    }),
  );

  await store.load();
  const unsubscribe = observer.subscribe(() => undefined);
  await microtasks();

  try {
    for (const step of steps) {
      const status = store.getSnapshot().status;
      switch (step.kind) {
        case 'wait':
          for (let left = step.ms; left > 0; left -= CHUNK_MS) {
            clock.advance(Math.min(CHUNK_MS, left));
            await microtasks();
          }
          break;
        case 'action':
          log.push({ type: 'action', at: clock.now() });
          target.dispatchEvent(new Event(step.event));
          break;
        case 'request':
          // Con la sesión perdida la pantalla está bloqueada: no pide nada.
          if (isAuthenticated(status)) serve();
          break;
        case 'continue':
          if (status !== 'expiring_soon') break;
          log.push({ type: 'action', at: clock.now() });
          target.dispatchEvent(new Event('click'));
          await store.continueWorking().catch(() => undefined);
          break;
        case 'visible':
          store.tick();
          break;
        case 'reauth':
          if (!needsReauth(status)) break;
          log.push({ type: 'action', at: clock.now() });
          target.dispatchEvent(new Event('keydown'));
          Object.assign(server, {
            alive: true,
            lastSeen: clock.now(),
            absolute: clock.now() + ABSOLUTE_MS,
          });
          await store.load();
          break;
      }
      await microtasks();
    }
  } finally {
    unsubscribe();
    cache.dispose();
    store.dispose();
    gate.dispose();
  }
  return { log, end: clock.now() };
}

/** Aviso de inactividad: uno por ventana, a los 28 min exactos de la última respuesta. */
function checkWarnings(log: readonly Entry[], end: number): void {
  let lastResponse: number | null = null;
  let warningsInWindow = 0;
  const closeWindow = (until: number, strictlyAfter: boolean) => {
    if (lastResponse === null) return;
    const due = lastResponse + WARNING_AFTER_MS;
    // Una respuesta justo a los 28 min puede llegar antes o después del aviso: ambas valen.
    if (strictlyAfter ? until > due : until >= due) expect(warningsInWindow).toBe(1);
  };
  for (const entry of log) {
    if (entry.type === 'response') {
      closeWindow(entry.at, true);
      lastResponse = entry.at;
      warningsInWindow = 0;
    } else if (entry.type === 'warning') {
      expect(lastResponse).not.toBeNull();
      expect(entry.at).toBe((lastResponse ?? 0) + WARNING_AFTER_MS);
      warningsInWindow += 1;
      expect(warningsInWindow).toBe(1);
    }
  }
  closeWindow(end, false);
}

/** D-4: sin acción en 25 min, ningún sondeo; con acción reciente, uno en cada tic de 60 s. */
function checkPolling(log: readonly Entry[], start: number, end: number): void {
  const actions = log.filter((e) => e.type === 'action').map((e) => e.at);
  const periodic = log.filter((e) => e.type === 'periodic').map((e) => e.at);
  // La puerta nace abierta en `start`. Una acción en el mismo instante que un tic ocurre después
  // de él (el tic corre al avanzar el reloj): solo cuentan las anteriores.
  const lastActionBefore = (at: number) => Math.max(start, ...actions.filter((a) => a < at));
  // El primer «periódico» es la lectura al montar, en `start`.
  for (const at of periodic.slice(1)) {
    expect(at - lastActionBefore(at)).toBeLessThan(ACTIVITY_GATE_MS);
  }
  for (let tick = start + PERIODIC_INTERVAL_MS; tick <= end; tick += PERIODIC_INTERVAL_MS) {
    if (tick - lastActionBefore(tick) < ACTIVITY_GATE_MS) {
      expect(periodic).toContain(tick);
    }
  }
}

describe('PR-APP-04 · aviso de inactividad y suspensión de los sondeos (BR-APP-14, D-4)', () => {
  it(
    'un aviso por ventana a los 28 min de la última respuesta; sin sondeos tras 25 min sin acción',
    async () => {
      await fc.assert(
        fc.asyncProperty(
          fc.integer({ min: 1_700_000_000_000, max: 1_900_000_000_000 }),
          fc.array(stepArb, { minLength: 1, maxLength: 40 }),
          async (start, steps) => {
            const { log, end } = await run(start, steps);
            checkWarnings(log, end);
            checkPolling(log, start, end);
          },
        ),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );
});
