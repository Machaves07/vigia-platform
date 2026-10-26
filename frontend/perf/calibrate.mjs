#!/usr/bin/env node
/**
 * Calibración de la línea base de Lighthouse (R-APP-3, PAT-APP-REN-04): lee las corridas que dejó
 * `lhci autorun` en `.lighthouseci/lhr-*.json`, imprime la cifra de cada una y la mediana, y con
 * `--write` reescribe `perf/baseline.json` y los umbrales de `lighthouserc.json`.
 *
 * Regla de regresión: el umbral de cada métrica es el menor entre el presupuesto de NFR-APP-02 y
 * la mediana de la línea base más un 20 %; una corrida cuya mediana lo supere falla `lhci assert`.
 * Sin etiquetas de `vigia-platform` (A-47), se compara con `perf/baseline.json`, no con «la última
 * etiqueta». `tests/perf/lighthouse_config.test.ts` comprueba que los dos archivos concuerdan.
 *
 *   node perf/calibrate.mjs [--write --machine "<equipo>" --load "<carga>"]
 */
import { readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';

const root = fileURLToPath(new URL('..', import.meta.url));
const METRICS = ['first-contentful-paint', 'interactive'];
const BUDGET = { 'first-contentful-paint': 2000, interactive: 3000 };
const TOLERANCE = 0.2;

const { values } = parseArgs({
  options: {
    write: { type: 'boolean', default: false },
    machine: { type: 'string' },
    load: { type: 'string' },
  },
});

const directory = `${root}.lighthouseci`;
const runs = readdirSync(directory)
  .filter((name) => /^lhr-\d+\.json$/.test(name))
  .sort()
  .map((name) => {
    const lhr = JSON.parse(readFileSync(`${directory}/${name}`, 'utf8'));
    // Una corrida que mide «navegador no compatible» (React no carga ni pide `GET /me`) no vale.
    const paths = lhr.audits['network-requests'].details.items.map(
      (item) => new URL(item.url).pathname,
    );
    const lcp = JSON.stringify(lhr.audits['largest-contentful-paint-element'].details ?? {});
    if (
      lhr.runtimeError !== undefined ||
      !paths.includes('/me') ||
      lcp.includes('vigia-unsupported')
    ) {
      process.stderr.write(
        `${name}: la corrida no arrancó la aplicación (¿navegador no compatible?).\n`,
      );
      process.exit(2);
    }
    return {
      fetched_at: lhr.fetchTime,
      ...Object.fromEntries(
        METRICS.map((metric) => [metric, Math.round(lhr.audits[metric].numericValue)]),
      ),
    };
  });
if (runs.length !== 3) {
  process.stderr.write(`Se esperaban 3 corridas en .lighthouseci/ y hay ${runs.length}.\n`);
  process.exit(2);
}

const median = (list) => [...list].sort((a, b) => a - b)[Math.floor(list.length / 2)];
const medians = Object.fromEntries(
  METRICS.map((metric) => [metric, median(runs.map((run) => run[metric]))]),
);
const thresholds = Object.fromEntries(
  METRICS.map((metric) => [
    metric,
    Math.min(BUDGET[metric], Math.floor(medians[metric] * (1 + TOLERANCE))),
  ]),
);
for (const [index, run] of runs.entries()) {
  process.stdout.write(
    `corrida ${index + 1}: ${METRICS.map((m) => `${m} ${run[m]} ms`).join(', ')}\n`,
  );
}
process.stdout.write(
  `mediana: ${METRICS.map((m) => `${m} ${medians[m]} ms (umbral ${thresholds[m]} ms)`).join(', ')}\n`,
);

if (values.write) {
  if (values.machine === undefined || values.load === undefined) {
    process.stderr.write('--write exige --machine y --load (R-APP-3: máquina y carga anotadas).\n');
    process.exit(2);
  }
  const baseline = {
    $comment:
      'Línea base de Lighthouse (NFR-APP-02, R-APP-3). La genera perf/calibrate.mjs con tres corridas; no se edita a mano.',
    calibrated_at: runs.at(-1).fetched_at,
    machine: values.machine,
    load: values.load,
    url: '/login',
    budget_ms: BUDGET,
    regression_tolerance: TOLERANCE,
    runs,
    median_ms: medians,
    threshold_ms: thresholds,
  };
  writeFileSync(`${root}perf/baseline.json`, `${JSON.stringify(baseline, null, 2)}\n`);
  const configPath = `${root}lighthouserc.json`;
  const config = JSON.parse(readFileSync(configPath, 'utf8'));
  for (const metric of METRICS) {
    config.ci.assert.assertions[metric][1].maxNumericValue = thresholds[metric];
  }
  writeFileSync(configPath, `${JSON.stringify(config, null, 2)}\n`);
}
