/**
 * PR-APP-18 (BR-APP-61, 72 y 73; PAT-APP-RES-02; NFR-APP-36): para toda secuencia de fallos y
 * aciertos de lectura, como mucho 3 reintentos con 2, 4 y 8 s del `FakeClock`, solo tras un fallo
 * reintentable y con el circuito cerrado, y después `offline`; para toda escritura, 0 reintentos.
 */
import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import { behaviourFor } from '@/ports/errors/map';
import {
  createTransport,
  isPortError,
  READ_RETRY_DELAYS_MS,
  TIMEOUT_MS,
  type RequestKind,
} from '@/ports/base';

import { FakeClock } from '../support/fakeClock';
import {
  drive,
  recordingConnection,
  RETRYABLE,
  scriptedFetch,
  TRANSIENT_RETRY_AFTER,
  type Step,
} from '../support/ports';
import { PROPERTY_TIMEOUT_MS } from '../support/timeouts';

const step = fc.constantFrom<Step>(
  'ok',
  'network',
  'timeout',
  'transient',
  'business',
  'not_found',
  'rate_limited',
);

const READ = 'list_users_users_get';
const WRITE = 'invite_users_post';

function setup(steps: readonly Step[], seededFailures: number) {
  const clock = new FakeClock();
  const { connection, after } = recordingConnection(clock);
  for (let i = 0; i < seededFailures; i += 1) {
    connection.register('platform', { ok: false, error: { kind: 'network' } });
  }
  after.length = 0;
  const scripted = scriptedFetch(clock, steps);
  const transport = createTransport({
    clock,
    connection,
    fetch: scripted.fetch,
    devLog: () => undefined,
  });
  return { clock, connection, after, calls: scripted.calls, transport };
}

/** Lo que el fallo de un intento de `kind` debe dar a la pantalla. */
function expectFailure(error: unknown, last: Step, kind: RequestKind): void {
  expect(isPortError(error)).toBe(true);
  if (!isPortError(error)) return;
  const { failure } = error;
  if (last === 'network' || last === 'timeout') {
    expect(failure).toMatchObject({ kind: last, behaviour: { kind: 'transient' } });
    return;
  }
  expect(failure.kind).toBe('http');
  if (failure.kind !== 'http') return;
  expect(failure.behaviour).toEqual(behaviourFor(failure.error));
  if (last === 'transient' && kind === 'write') {
    expect(failure.behaviour).toMatchObject({
      kind: 'transient',
      retry_after_seconds: TRANSIENT_RETRY_AFTER,
    });
  }
}

describe('PR-APP-18', () => {
  it(
    'lecturas: como mucho 3 reintentos a 2, 4 y 8 s, solo reintentables y con el circuito cerrado; después offline',
    async () => {
      await fc.assert(
        fc.asyncProperty(
          fc.array(step, { minLength: 1, maxLength: 6 }),
          fc.integer({ min: 0, max: 4 }),
          async (steps, seeded) => {
            const { clock, connection, after, calls, transport } = setup(steps, seeded);
            const outcome = await drive(
              clock,
              transport.request({ operationId: READ, kind: 'read' }),
            );

            if (seeded >= 4) {
              // Circuito abierto: falla rápido, sin red.
              expect(calls).toHaveLength(0);
              expect(!outcome.ok && isPortError(outcome.error) && outcome.error.failure).toEqual({
                kind: 'offline',
              });
              return;
            }

            expect(calls.length).toBeGreaterThanOrEqual(1);
            expect(calls.length).toBeLessThanOrEqual(1 + READ_RETRY_DELAYS_MS.length);
            expect(after).toHaveLength(calls.length);

            for (let i = 0; i < calls.length; i += 1) {
              const current = steps[i] ?? 'ok';
              const call = calls[i];
              const circuit = after[i]?.circuits.platform.state;
              if (call === undefined || call.endedAt === null) throw new Error('intento sin fin');
              if (current === 'timeout') expect(call.endedAt - call.at).toBe(TIMEOUT_MS.read);
              else expect(call.endedAt).toBe(call.at);

              const retriedNext = i + 1 < calls.length;
              const mayRetry =
                RETRYABLE.has(current) && i < READ_RETRY_DELAYS_MS.length && circuit === 'closed';
              expect(retriedNext).toBe(mayRetry);
              if (retriedNext) {
                expect((calls[i + 1]?.at ?? NaN) - call.endedAt).toBe(READ_RETRY_DELAYS_MS[i]);
              }
            }

            const last = steps[calls.length - 1] ?? 'ok';
            if (last === 'ok') {
              expect(outcome).toEqual({ ok: true, value: { answer: calls.length } });
            } else if (RETRYABLE.has(last)) {
              // Reintentos agotados o circuito ya no cerrado: offline.
              expect(!outcome.ok && isPortError(outcome.error) && outcome.error.failure).toEqual({
                kind: 'offline',
              });
              // Con 4 fallos dentro de la ventana de 30 s del circuito (U05-H-10), offline.
              const firstFailure = seeded > 0 ? 0 : (calls[0]?.endedAt ?? 0);
              const lastEnd = calls.at(-1)?.endedAt ?? Infinity;
              if (seeded + calls.length >= 4 && lastEnd - firstFailure < 30_000) {
                expect(connection.getSnapshot().connection.state).toBe('offline');
              }
            } else {
              expect(outcome.ok).toBe(false);
              if (!outcome.ok) expectFailure(outcome.error, last, 'read');
            }
          },
        ),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );

  it(
    'escrituras: 0 reintentos; transient con retry_after_seconds para «repetir»',
    async () => {
      await fc.assert(
        fc.asyncProperty(
          fc.array(step, { minLength: 1, maxLength: 6 }),
          fc.integer({ min: 0, max: 4 }),
          async (steps, seeded) => {
            const { clock, calls, transport } = setup(steps, seeded);
            const outcome = await drive(
              clock,
              transport.request({ operationId: WRITE, kind: 'write', body: { any: 1 } }),
            );

            if (seeded >= 4) {
              expect(calls).toHaveLength(0);
              expect(!outcome.ok && isPortError(outcome.error) && outcome.error.failure).toEqual({
                kind: 'offline',
              });
              return;
            }

            expect(calls).toHaveLength(1);
            const first = steps[0] ?? 'ok';
            const call = calls[0];
            if (call === undefined || call.endedAt === null) throw new Error('intento sin fin');
            if (first === 'timeout') expect(call.endedAt - call.at).toBe(TIMEOUT_MS.write);
            if (first === 'ok') expect(outcome).toEqual({ ok: true, value: { answer: 1 } });
            else {
              expect(outcome.ok).toBe(false);
              if (!outcome.ok) expectFailure(outcome.error, first, 'write');
            }
            // Nada queda programado: ninguna escritura se repite más tarde.
            expect(clock.pendingCount).toBe(0);
          },
        ),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );
});
