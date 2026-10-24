/**
 * PR-APP-27 (BR-APP-37, PAT-APP-REN-03): para toda lista de intervalos de cobertura (de 1 ms a
 * 31 días) y toda anchura de línea, la agrupación visual de UI-11 conserva, capa por capa, el
 * número de intervalos y la suma de sus duraciones; ningún intervalo se omite ni se fusiona; toda
 * barra mide 1 px o más y es un solo intervalo; y toda marca agrupa intervalos de menos de un
 * píxel de su misma columna y los expone uno a uno.
 */
import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import { layoutLane, type TimelineItem } from '@/components/CoverageTimeline/layout';
import {
  LAYERS,
  type CoverageIntervalOut,
  type CoverageLayer,
  type TimedInterval,
} from '@/state/coverage/compose';

import { coverageIntervals, MAX_INTERVAL_MS } from '../generators/domain';
import { PROPERTY_TIMEOUT_MS } from '../support/timeouts';

/** Intervalos del generador de dominio, contiguos, con una capa por intervalo. */
const fromDomain = coverageIntervals({ minLength: 1, maxLength: 40 }).chain((raw) =>
  fc
    .array(fc.constantFrom(...LAYERS), { minLength: raw.length, maxLength: raw.length })
    .map((layers) =>
      raw.map((interval, index): TimedInterval => {
        const layer = layers[index] ?? 'node_report';
        return {
          startMs: Date.parse(String(interval.starts_at)),
          endMs: Date.parse(String(interval.ends_at)),
          layer,
          interval: { ...(interval as unknown as CoverageIntervalOut), layer },
        };
      }),
    ),
);

/** Muchos intervalos breves entre otros largos: las marcas aparecen en casi todos los casos. */
const skewed = fc
  .tuple(
    fc.integer({ min: Date.UTC(2026, 0, 1), max: Date.UTC(2027, 11, 31) }),
    fc.array(
      fc.record({
        durationMs: fc.oneof(
          { weight: 3, arbitrary: fc.integer({ min: 1, max: 60_000 }) },
          { weight: 1, arbitrary: fc.integer({ min: 1, max: MAX_INTERVAL_MS }) },
        ),
        layer: fc.constantFrom(...LAYERS),
      }),
      { minLength: 1, maxLength: 120 },
    ),
  )
  .map(([start, parts]) => {
    let cursor = start;
    return parts.map(({ durationMs, layer }): TimedInterval => {
      const startMs = cursor;
      cursor += durationMs;
      return {
        startMs,
        endMs: cursor,
        layer,
        interval: {
          starts_at: new Date(startMs).toISOString(),
          ends_at: new Date(cursor).toISOString(),
          duration_ms: durationMs,
          layer,
          state: 'not_observable',
          causes: [],
          clock_basis: layer === 'node_report' ? 'node' : 'platform',
        },
      };
    });
  });

const scenario = fc.record({
  intervals: fc.oneof(fromDomain, skewed),
  widthPx: fc.integer({ min: 1, max: 2000 }),
  /** Margen del periodo alrededor de los intervalos, como un tramo que no está entero. */
  marginMs: fc.nat({ max: 10 * MAX_INTERVAL_MS }),
});

function durations(intervals: readonly TimedInterval[]): number {
  return intervals.reduce((sum, interval) => sum + (interval.endMs - interval.startMs), 0);
}

function grouped(items: readonly TimelineItem[]): TimedInterval[] {
  return items.flatMap((item) => [...item.intervals]);
}

describe('PR-APP-27 agrupación visual de la línea de tiempo', () => {
  it(
    'conserva número de intervalos y suma de duraciones, y toda marca expone sus intervalos',
    () => {
      fc.assert(
        fc.property(scenario, ({ intervals, widthPx, marginMs }) => {
          const first = intervals[0];
          const last = intervals.at(-1);
          if (first === undefined || last === undefined) return;
          const period = { fromMs: first.startMs - marginMs, toMs: last.endMs + marginMs };
          const scale = widthPx / (period.toMs - period.fromMs);
          for (const layer of LAYERS as readonly CoverageLayer[]) {
            const lane = intervals.filter((interval) => interval.layer === layer);
            const items = layoutLane(lane, period, widthPx);
            // Ni omitido ni fusionado: los mismos intervalos, en el mismo orden.
            expect(grouped(items)).toEqual(lane);
            expect(items.reduce((sum, item) => sum + item.intervals.length, 0)).toBe(lane.length);
            expect(durations(grouped(items))).toBe(durations(lane));
            for (const item of items) {
              expect(item.layer).toBe(layer);
              if (item.kind === 'bar') {
                expect(item.intervals).toHaveLength(1);
                expect(item.width).toBeGreaterThanOrEqual(1);
                continue;
              }
              expect(item.intervals.length).toBeGreaterThan(0);
              for (const member of item.intervals) {
                expect((member.endMs - member.startMs) * scale).toBeLessThan(1);
                expect(Math.floor((member.startMs - period.fromMs) * scale)).toBe(item.x);
              }
            }
          }
        }),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );
});
