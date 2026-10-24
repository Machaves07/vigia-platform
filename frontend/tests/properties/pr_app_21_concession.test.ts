/**
 * PR-APP-21 (BR-APP-67 a 69, LC-APP-08, IF-06): para toda secuencia de selección, cambio,
 * vencimiento y `not_found` de concesiones, intercalada con peticiones, la cabecera
 * `X-Vigia-Concession` está en una petición si y solo si hay una concesión activa y no vencida
 * (y es la suya), y cada final (cambio, vencimiento o `not_found` bajo la concesión vigente)
 * vacía la caché de consultas y descarta el borrador.
 *
 * El transporte es el de `ports.base` con `transportOptions(store)`; el `fetch` inyectado anota
 * las cabeceras de cada petición. El modelo es independiente del almacén: concesión activa y su
 * vencimiento. La pestaña en segundo plano se simula con un reloj cuyos temporizadores no se
 * ejecutan (los navegadores los retrasan): el vencimiento se comprueba igual al pedir.
 */
import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import { CONCESSION_HEADER, createPortConnection, createTransport } from '@/ports/base';
import type { Clock, TimerHandle } from '@/state/clock';
import { createConcessionStore, transportOptions, type ConcessionEnd } from '@/state/concession';
import { createScreenQueryCache, screenKey } from '@/state/query';
import { createFormDraft, formDraftReducer, type FormDraft } from '@/state/reducers/formDraft';

import { FakeClock } from '../support/fakeClock';
import { PROPERTY_TIMEOUT_MS } from '../support/timeouts';

const START = Date.parse('2026-10-24T12:00:00.000Z');
const HOUR = 3_600_000;

type Event =
  | {
      readonly kind: 'activate';
      readonly id: string;
      readonly ttlMs: number;
      readonly status: 'active' | 'expired' | 'revoked';
    }
  | { readonly kind: 'request' }
  | { readonly kind: 'not_found'; readonly stale: boolean }
  | { readonly kind: 'advance'; readonly ms: number }
  | { readonly kind: 'change' }
  | { readonly kind: 'fill' };

const event: fc.Arbitrary<Event> = fc.oneof(
  fc.record({
    kind: fc.constant('activate' as const),
    id: fc.uuid(),
    // Hasta 60 días: por encima del tope de `setTimeout` (24,8 días).
    ttlMs: fc.oneof(fc.integer({ min: -HOUR, max: 2 * HOUR }), fc.constant(60 * 24 * HOUR)),
    status: fc.constantFrom(
      'active' as const,
      'active' as const,
      'expired' as const,
      'revoked' as const,
    ),
  }),
  fc.constant({ kind: 'request' as const }),
  fc.record({ kind: fc.constant('not_found' as const), stale: fc.boolean() }),
  fc.record({
    kind: fc.constant('advance' as const),
    ms: fc.oneof(fc.integer({ min: 0, max: 2 * HOUR }), fc.constant(30 * 24 * HOUR)),
  }),
  fc.constant({ kind: 'change' as const }),
  fc.constant({ kind: 'fill' as const }),
);

/** Reloj de pestaña en segundo plano: el tiempo avanza, los temporizadores no llegan. */
class ThrottledClock implements Clock {
  #now: number;
  constructor(start: number) {
    this.#now = start;
  }
  now(): number {
    return this.#now;
  }
  setTimeout(): TimerHandle {
    return 0 as unknown as TimerHandle;
  }
  clearTimeout(): void {
    return undefined;
  }
  advance(ms: number): void {
    this.#now += ms;
  }
}

const gates = { isVisible: () => true, canReadPlatform: () => true, isActive: () => true };

describe('PR-APP-21: cabecera de concesión si y solo si hay concesión activa', () => {
  it(
    'para toda secuencia, la cabecera sale solo bajo la concesión vigente y cada final vacía caché y borrador',
    async () => {
      await fc.assert(
        fc.asyncProperty(
          fc.constantFrom('provider', 'client'),
          fc.boolean(),
          fc.array(event, { minLength: 1, maxLength: 30 }),
          async (kind, throttled, events) => {
            const clock = throttled ? new ThrottledClock(START) : new FakeClock(START);
            const cache = createScreenQueryCache({ clock, gates });
            const ended: ConcessionEnd[] = [];
            const store = createConcessionStore({
              clock,
              organizationKind: () => kind,
              cache,
              onEnded: (reason) => ended.push(reason),
            });
            let draft: FormDraft = createFormDraft('scr09', 'closure');
            const unsubscribe = store.onDiscard(() => {
              draft = formDraftReducer(draft, { type: 'discard' });
            });
            const sent: (string | null)[] = [];
            let respondNotFound = false;
            const transport = createTransport({
              clock,
              connection: createPortConnection(clock),
              devLog: () => undefined,
              origin: 'http://localhost',
              ...transportOptions(store),
              fetch: (_input, init) => {
                sent.push(new Headers(init?.headers).get(CONCESSION_HEADER));
                return Promise.resolve(
                  respondNotFound
                    ? Response.json({ code: 'not_found', message_es: 'No.' }, { status: 404 })
                    : Response.json({ ok: true }),
                );
              },
            });

            /** Modelo: concesión activa, su vencimiento y la última que estuvo activa. */
            let model: { id: string; expiresAt: number } | null = null;
            let previous: string | null = null;
            let endings = 0;
            /** Un final del modelo cuyo efecto aún no se comprobó. */
            let pending: ConcessionEnd | null = null;
            const modelActive = (): string | null => {
              if (model !== null && clock.now() >= model.expiresAt) {
                model = null;
                pending = 'expired';
              }
              return model?.id ?? null;
            };
            const endModel = (reason: ConcessionEnd) => {
              model = null;
              pending = reason;
            };
            /** Tras cada final: caché vacía, borrador descartado y aviso con su motivo. */
            const checkEnded = () => {
              if (pending === null) return;
              endings += 1;
              expect(ended.at(-1)).toBe(pending);
              expect(cache.client.getQueryCache().getAll()).toHaveLength(0);
              expect(draft.dirty).toBe(false);
              expect(draft.values).toEqual({});
              pending = null;
            };
            const request = async (status: 'ok' | 'not_found') => {
              const expected = modelActive();
              respondNotFound = status === 'not_found';
              await transport
                .request({ operationId: 'hierarchy_hierarchy_get', kind: 'read' })
                .catch(() => undefined);
              respondNotFound = false;
              expect(sent.at(-1)).toBe(expected);
              checkEnded();
              return expected;
            };

            for (const step of events) {
              switch (step.kind) {
                case 'activate': {
                  const was = modelActive();
                  const expiresAt = clock.now() + step.ttlMs;
                  const accepted = store.activate({
                    concession_id: step.id,
                    client_organization_id: 'org-cliente',
                    scope_level: 'plant',
                    scope_id: 'planta-1',
                    expires_at: new Date(expiresAt).toISOString(),
                    status: step.status,
                  });
                  const allowed =
                    kind === 'provider' &&
                    was === null &&
                    step.status === 'active' &&
                    step.ttlMs > 0;
                  expect(accepted).toBe(allowed);
                  if (allowed) {
                    model = { id: step.id, expiresAt };
                    previous = step.id;
                  }
                  break;
                }
                case 'request':
                  await request('ok');
                  break;
                case 'not_found': {
                  if (step.stale && previous !== null && previous !== modelActive()) {
                    // Respuesta tardía de una concesión ya terminada: no afecta a la vigente.
                    const before = store.getSnapshot().active?.concession_id ?? null;
                    store.reportNotFound(previous);
                    expect(store.getSnapshot().active?.concession_id ?? null).toBe(before);
                    break;
                  }
                  const expected = modelActive();
                  if (expected !== null) endModel('not_found');
                  respondNotFound = true;
                  await transport
                    .request({ operationId: 'hierarchy_hierarchy_get', kind: 'read' })
                    .catch(() => undefined);
                  respondNotFound = false;
                  expect(sent.at(-1)).toBe(expected);
                  break;
                }
                case 'advance':
                  clock.advance(step.ms);
                  break;
                case 'change': {
                  const was = modelActive();
                  store.change();
                  if (was !== null) endModel('changed');
                  break;
                }
                case 'fill':
                  // Lo que una pantalla pidió y lo que la persona escribió.
                  cache.client.setQueryData(
                    screenKey({
                      screen: 'scr08',
                      operation: 'GET /loop/findings',
                      scope: 'all',
                      concession: model?.id ?? null,
                      params: { n: sent.length },
                    }),
                    { items: [] },
                  );
                  draft = formDraftReducer(draft, {
                    type: 'set_value',
                    field: 'reason',
                    value: 'Motivo en curso',
                  });
                  break;
              }
              // Con la pestaña en segundo plano, el vencimiento lo descubre la siguiente petición.
              if (throttled) store.header();
              const expected = modelActive();
              checkEnded();
              expect(store.getSnapshot().active?.concession_id ?? null).toBe(expected);
            }
            await request('ok');
            if (kind === 'client') expect(sent.every((value) => value === null)).toBe(true);
            expect(ended).toHaveLength(endings);
            unsubscribe();
            store.dispose();
            cache.dispose();
          },
        ),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );
});
