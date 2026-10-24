/**
 * Reloj inyectable (PAT-APP-RES-06) y perfiles de fast-check (NFR-APP-43, PAT-APP-MAN-04).
 *
 * La propiedad de `FakeClock` corre con el perfil de la corrida: 200 ejemplos en `ci` y 2 000
 * en `nightly`. Al terminar imprime cuántos ejemplos usó y una huella de los valores generados:
 * con el mismo `FC_SEED`, dos corridas imprimen la misma huella.
 */
import { createHash } from 'node:crypto';

import fc from 'fast-check';
import { afterAll, describe, expect, it } from 'vitest';

import { systemClock, type TimerHandle } from '@/state/clock';

import { NUM_RUNS, parseProfileName } from '../../config/fastCheckProfile';
import { FakeClock } from '../support/fakeClock';

const profile = parseProfileName(process.env.FC_PROFILE);

const schedule = fc.array(
  fc.record({
    delayMs: fc.integer({ min: 0, max: 10_000 }),
    cancel: fc.boolean(),
  }),
  { maxLength: 30 },
);
const steps = fc.array(fc.integer({ min: 0, max: 5_000 }), { minLength: 1, maxLength: 10 });

const drawn: string[] = [];

describe('FakeClock', () => {
  it('ejecuta cada temporizador no cancelado una vez, en orden de vencimiento y de alta', () => {
    let runs = 0;
    fc.assert(
      fc.property(fc.integer({ min: 0, max: 2 ** 40 }), schedule, steps, (start, timers, moves) => {
        runs += 1;
        drawn.push(JSON.stringify([start, timers, moves]));
        const clock = new FakeClock(start);
        const fired: { index: number; at: number }[] = [];
        const handles: TimerHandle[] = timers.map(({ delayMs }, index) =>
          clock.setTimeout(() => fired.push({ index, at: clock.now() }), delayMs),
        );
        timers.forEach(({ cancel }, index) => {
          const handle = handles[index];
          if (cancel && handle !== undefined) clock.clearTimeout(handle);
        });
        let elapsed = 0;
        for (const move of moves) {
          clock.advance(move);
          elapsed += move;
          expect(clock.now()).toBe(start + elapsed);
        }
        const expected = timers
          .map(({ delayMs, cancel }, index) => ({ index, at: start + delayMs, cancel }))
          .filter(({ at, cancel }) => !cancel && at <= start + elapsed)
          .sort((a, b) => a.at - b.at || a.index - b.index)
          .map(({ index, at }) => ({ index, at }));
        expect(fired).toEqual(expected);
        expect(clock.pendingCount).toBe(timers.filter((t) => !t.cancel).length - expected.length);
      }),
    );
    expect(runs).toBe(NUM_RUNS[profile]);
    expect(runs).toBe(profile === 'nightly' ? 2000 : 200);
  });

  it('un temporizador vence justo en el borde de la ventana, y no antes', () => {
    const clock = new FakeClock(0);
    const fired: number[] = [];
    clock.setTimeout(() => fired.push(clock.now()), 100);
    clock.advance(99);
    expect(fired).toEqual([]);
    clock.advance(1);
    expect(fired).toEqual([100]);
    clock.setTimeout(() => fired.push(clock.now()), 0);
    clock.advance(0);
    expect(fired).toEqual([100, 100]);
  });

  it('un temporizador programado durante advance vence en la misma llamada', () => {
    const clock = new FakeClock(1_000);
    const seen: number[] = [];
    clock.setTimeout(() => {
      seen.push(clock.now());
      clock.setTimeout(() => seen.push(clock.now()), 50);
    }, 100);
    clock.advance(200);
    expect(seen).toEqual([1_100, 1_150]);
    expect(clock.now()).toBe(1_200);
  });

  it('rechaza avances negativos o no enteros', () => {
    const clock = new FakeClock();
    expect(() => {
      clock.advance(-1);
    }).toThrow(RangeError);
    expect(() => {
      clock.advance(0.5);
    }).toThrow(RangeError);
  });
});

describe('systemClock', () => {
  it('lee la hora del sistema y programa en el temporizador real', async () => {
    const before = systemClock.now();
    expect(Number.isSafeInteger(before)).toBe(true);
    await new Promise<void>((resolve) => {
      systemClock.setTimeout(resolve, 1);
    });
    expect(systemClock.now()).toBeGreaterThanOrEqual(before);
    let cancelled = true;
    const handle = systemClock.setTimeout(() => {
      cancelled = false;
    }, 1);
    systemClock.clearTimeout(handle);
    await new Promise<void>((resolve) => {
      systemClock.setTimeout(resolve, 20);
    });
    expect(cancelled).toBe(true);
  });
});

describe('perfil de fast-check', () => {
  it('usa el número de ejemplos del perfil y la semilla de la corrida', () => {
    const global = fc.readConfigureGlobal();
    expect(global.numRuns).toBe(profile === 'nightly' ? 2000 : 200);
    expect(String(global.seed)).toBe(process.env.FC_SEED);
  });
});

afterAll(() => {
  const digest = createHash('sha256').update(drawn.join('\n')).digest('hex').slice(0, 16);
  process.stdout.write(
    `[fast-check] clock.test: ${String(drawn.length)} ejemplos (perfil ${profile}, ` +
      `semilla ${String(process.env.FC_SEED)}), huella ${digest}\n`,
  );
});
