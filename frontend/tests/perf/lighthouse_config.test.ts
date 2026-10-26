/**
 * NFR-APP-02 y PAT-APP-REN-04: `lighthouserc.json` mide con la limitación de red (10 Mbit/s) y de
 * procesador del diseño, en tres corridas con la mediana, y sus umbrales salen de
 * `perf/baseline.json` con la regla de regresión del 20 % (`perf/calibrate.mjs`): el menor entre
 * el presupuesto y la mediana calibrada más un 20 %. Cambiar la línea base sin los umbrales, o al
 * revés, falla aquí.
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const read = (path: string): unknown => JSON.parse(readFileSync(resolve(root, path), 'utf8'));

interface Assertion {
  readonly maxNumericValue: number;
  readonly aggregationMethod: string;
}
interface Config {
  readonly ci: {
    readonly collect: {
      readonly url: readonly string[];
      readonly numberOfRuns: number;
      readonly startServerCommand: string;
      readonly settings: {
        readonly throttlingMethod: string;
        readonly throttling: Readonly<Record<string, number>>;
      };
    };
    readonly assert: {
      readonly assertions: Readonly<Record<string, readonly [string, Assertion]>>;
    };
  };
}
type Metric = 'first-contentful-paint' | 'interactive';
interface Baseline {
  readonly machine: string;
  readonly load: string;
  readonly budget_ms: Readonly<Record<Metric, number>>;
  readonly regression_tolerance: number;
  readonly runs: readonly Readonly<Record<Metric, number>>[];
  readonly median_ms: Readonly<Record<Metric, number>>;
  readonly threshold_ms: Readonly<Record<Metric, number>>;
}

const config = read('lighthouserc.json') as Config;
const baseline = read('perf/baseline.json') as Baseline;
const METRICS: readonly Metric[] = ['first-contentful-paint', 'interactive'];

describe('Lighthouse CI: limitación, línea base y regresión del 20 %', () => {
  it('mide la construcción servida por el mundo con 10 Mbit/s y CPU limitada, tres corridas', () => {
    const { collect } = config.ci;
    expect(collect.startServerCommand).toBe('node perf/world-server.mjs');
    expect(collect.url).toEqual(['https://127.0.0.1:47330/login']);
    expect(collect.numberOfRuns).toBe(3);
    expect(collect.settings.throttlingMethod).toBe('simulate');
    expect(collect.settings.throttling.throughputKbps).toBe(10 * 1024);
    expect(collect.settings.throttling.cpuSlowdownMultiplier).toBeGreaterThan(1);
  });

  it('la línea base tiene tres corridas, la máquina y la carga anotadas, y su mediana', () => {
    expect(baseline.runs).toHaveLength(3);
    expect(baseline.machine.trim()).not.toBe('');
    expect(baseline.load.trim()).not.toBe('');
    expect(baseline.budget_ms).toEqual({ 'first-contentful-paint': 2000, interactive: 3000 });
    expect(baseline.regression_tolerance).toBe(0.2);
    for (const metric of METRICS) {
      const sorted = baseline.runs.map((run) => run[metric]).sort((a, b) => a - b);
      expect(baseline.median_ms[metric]).toBe(sorted[1]);
    }
  });

  it.each(METRICS)('%s: umbral = mín(presupuesto, mediana × 1,2), sobre la mediana', (metric) => {
    const expected = Math.min(
      baseline.budget_ms[metric],
      Math.floor(baseline.median_ms[metric] * (1 + baseline.regression_tolerance)),
    );
    expect(baseline.threshold_ms[metric]).toBe(expected);
    const assertion = config.ci.assert.assertions[metric];
    expect(assertion?.[0]).toBe('error');
    expect(assertion?.[1]).toEqual({ maxNumericValue: expected, aggregationMethod: 'median-run' });
  });
});
