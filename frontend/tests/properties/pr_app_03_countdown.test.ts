/**
 * PR-APP-03 (BR-APP-11): para todo `retry_after_seconds` de 0 a 900, la acción queda deshabilitada
 * exactamente ese tiempo, ni antes ni después; sin `retry_after_seconds` no se deshabilita nada.
 *
 * El tiempo avanza solo con tics de `FakeClock`; el fin de la cuenta lo programa
 * `scheduleCountdownEnd` en ese mismo reloj, como hará la interfaz.
 */
import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import {
  countdownReducer,
  EVIDENCE_URL_MAX_SECONDS,
  idleCountdown,
  isCountingDown,
  LIVE_VIEW_TOKEN_SECONDS,
  remainingSeconds,
  scheduleCountdownEnd,
  type CountdownState,
} from '@/state/reducers/countdown';

import { FakeClock } from '../support/fakeClock';
import { PROPERTY_TIMEOUT_MS } from '../support/timeouts';

const seconds = fc.integer({ min: 0, max: 900 });
const startMs = fc.integer({ min: 0, max: 4_102_444_800_000 });
const ticks = fc.array(fc.integer({ min: 0, max: 120_000 }), { minLength: 1, maxLength: 40 });

/** Cuenta conectada a un reloj: arranca en `clock.now()` y se cierra con su temporizador. */
function drive(clock: FakeClock, retryAfter: number | null | undefined) {
  let state: CountdownState = countdownReducer(idleCountdown, {
    type: 'start',
    seconds: retryAfter,
    nowMs: clock.now(),
  });
  let endedAt: number | null = null;
  scheduleCountdownEnd(clock, state, (nowMs) => {
    endedAt = nowMs;
    state = countdownReducer(state, { type: 'tick', nowMs });
  });
  return {
    get state() {
      return state;
    },
    get endedAt() {
      return endedAt;
    },
  };
}

describe('PR-APP-03 · cuenta regresiva de retry_after_seconds', () => {
  it(
    'deshabilita exactamente retry_after_seconds y rehabilita sola al vencer',
    () => {
      fc.assert(
        fc.property(seconds, startMs, ticks, (retryAfter, start, steps) => {
          const clock = new FakeClock(start);
          const countdown = drive(clock, retryAfter);
          const deadline = start + retryAfter * 1000;
          // El instante de recepción ya cuenta, salvo con 0 s.
          expect(isCountingDown(countdown.state, clock.now())).toBe(retryAfter > 0);
          for (const step of steps) {
            clock.advance(step);
            const now = clock.now();
            expect(isCountingDown(countdown.state, now)).toBe(now < deadline);
            expect(remainingSeconds(countdown.state, now)).toBe(
              now < deadline ? Math.ceil((deadline - now) / 1000) : 0,
            );
          }
          if (clock.now() >= deadline && retryAfter > 0) {
            expect(countdown.endedAt).toBe(deadline);
            expect(countdown.state).toEqual(idleCountdown);
          }
        }),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );

  it(
    'en el borde: deshabilitada 1 ms antes del vencimiento y habilitada en el vencimiento',
    () => {
      fc.assert(
        fc.property(fc.integer({ min: 1, max: 900 }), startMs, (retryAfter, start) => {
          const clock = new FakeClock(start);
          const countdown = drive(clock, retryAfter);
          clock.advance(retryAfter * 1000 - 1);
          expect(isCountingDown(countdown.state, clock.now())).toBe(true);
          expect(remainingSeconds(countdown.state, clock.now())).toBe(1);
          expect(countdown.endedAt).toBeNull();
          clock.advance(1);
          expect(isCountingDown(countdown.state, clock.now())).toBe(false);
          expect(countdown.state).toEqual(idleCountdown);
          expect(clock.pendingCount).toBe(0);
        }),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );

  it(
    'sin retry_after_seconds válido no deshabilita nada ni programa temporizadores',
    () => {
      const invalid = fc.oneof(
        fc.constant(undefined),
        fc.constant(null),
        fc.integer({ max: -1 }),
        fc.double({ noInteger: true }),
        fc.constantFrom(Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY),
      );
      fc.assert(
        fc.property(invalid, startMs, ticks, (retryAfter, start, steps) => {
          const clock = new FakeClock(start);
          const countdown = drive(clock, retryAfter);
          expect(countdown.state).toEqual(idleCountdown);
          expect(clock.pendingCount).toBe(0);
          for (const step of steps) {
            clock.advance(step);
            expect(isCountingDown(countdown.state, clock.now())).toBe(false);
          }
        }),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );

  it(
    'un aviso nuevo sustituye al anterior: no se acumulan intentos',
    () => {
      fc.assert(
        fc.property(
          seconds,
          seconds,
          startMs,
          fc.integer({ min: 0, max: 900_000 }),
          (a, b, start, gap) => {
            let state = countdownReducer(idleCountdown, {
              type: 'start',
              seconds: a,
              nowMs: start,
            });
            const second = start + gap;
            state = countdownReducer(state, { type: 'tick', nowMs: second });
            state = countdownReducer(state, { type: 'start', seconds: b, nowMs: second });
            expect(state).toEqual(
              b > 0 ? { kind: 'running', untilMs: second + b * 1000 } : idleCountdown,
            );
          },
        ),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );

  it('cancelar la programación deja el reloj sin temporizadores', () => {
    const clock = new FakeClock(1_000);
    const state = countdownReducer(idleCountdown, { type: 'start', seconds: 30, nowMs: 1_000 });
    let ended = false;
    const cancel = scheduleCountdownEnd(clock, state, () => {
      ended = true;
    });
    expect(clock.pendingCount).toBe(1);
    cancel();
    clock.advance(60_000);
    expect(ended).toBe(false);
    expect(countdownReducer(state, { type: 'reset' })).toEqual(idleCountdown);
  });
});

describe('vencimientos reutilizan la cuenta regresiva', () => {
  it('token de vista en vivo: 600 s exactos', () => {
    const clock = new FakeClock(5_000);
    const countdown = drive(clock, LIVE_VIEW_TOKEN_SECONDS);
    clock.advance(599_999);
    expect(isCountingDown(countdown.state, clock.now())).toBe(true);
    clock.advance(1);
    expect(isCountingDown(countdown.state, clock.now())).toBe(false);
  });

  it(
    'URL de evidencia: vence en su expires_at y nunca después de 5 min',
    () => {
      fc.assert(
        fc.property(startMs, fc.integer({ min: -60_000, max: 3_600_000 }), (now, offset) => {
          const state = countdownReducer(idleCountdown, {
            type: 'start_until',
            untilMs: now + offset,
            maxSeconds: EVIDENCE_URL_MAX_SECONDS,
            nowMs: now,
          });
          const expected = Math.min(offset, EVIDENCE_URL_MAX_SECONDS * 1000);
          if (expected <= 0) {
            expect(state).toEqual(idleCountdown);
          } else {
            expect(state).toEqual({ kind: 'running', untilMs: now + expected });
            expect(isCountingDown(state, now + expected - 1)).toBe(true);
            expect(isCountingDown(state, now + expected)).toBe(false);
          }
        }),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );

  it('un vencimiento no finito no abre una cuenta', () => {
    for (const untilMs of [Number.NaN, Number.POSITIVE_INFINITY]) {
      const state = countdownReducer(idleCountdown, {
        type: 'start_until',
        untilMs,
        maxSeconds: EVIDENCE_URL_MAX_SECONDS,
        nowMs: 0,
      });
      expect(state).toEqual(idleCountdown);
    }
  });
});
