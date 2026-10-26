/**
 * Informe de los escenarios FS-APP (LC-APP-26, PAT-APP-RES-08): un `Reporter` de vitest que, al
 * terminar una corrida en la que se ejecutó al menos un escenario, escribe
 * `test-results/fs-app-<semilla>.json` con el resultado de cada uno de los diez. Los escenarios
 * que no corrieron constan como `not_run`, nunca se omiten. La canalización `nightly` lo sube con
 * 90 días de retención (VIG-337).
 *
 * Cada prueba se marca con `scenarioCase` (`harness.tsx`): el informe toma de esa marca el
 * escenario, la semilla de la corrida, la del escenario y los parámetros dibujados, y de vitest el
 * estado y la duración. Sin hora del sistema: la fecha la pone el artefacto de la canalización.
 */
import { mkdirSync, writeFileSync } from 'node:fs';
import { join, relative } from 'node:path';

import type { Reporter, TestModule } from 'vitest/node';

import { SCENARIO_IDS, SCENARIOS, type ScenarioId, type ScenarioMeta } from './scenarios';

/** Un caso de prueba marcado como escenario, tal como lo entrega vitest. */
export interface ScenarioCase {
  readonly meta: ScenarioMeta;
  readonly test: string;
  readonly file: string;
  readonly state: 'passed' | 'failed' | 'skipped' | 'pending';
  readonly duration_ms: number | null;
}

/** Un caso en el informe: el de vitest con la semilla y los parámetros de su escenario. */
type ReportCase = Omit<ScenarioCase, 'meta'> & Pick<ScenarioMeta, 'scenario_seed' | 'parameters'>;

export interface FsAppReport {
  readonly report: 'fs-app';
  readonly run_seed: number;
  readonly scenarios: readonly {
    readonly id: ScenarioId;
    readonly title: string;
    readonly status: 'passed' | 'failed' | 'not_run';
    readonly cases: readonly ReportCase[];
  }[];
}

/** El informe de una corrida; `null` si no corrió ningún escenario. */
export function buildFsAppReport(cases: readonly ScenarioCase[]): FsAppReport | null {
  if (cases.length === 0) return null;
  const seeds = new Set(cases.map((item) => item.meta.run_seed));
  if (seeds.size !== 1)
    throw new Error('Informe FS-APP: casos de corridas con semillas distintas.');
  const [runSeed] = seeds;
  return {
    report: 'fs-app',
    run_seed: runSeed ?? 0,
    scenarios: SCENARIO_IDS.map((id) => {
      const own = cases.filter((item) => item.meta.id === id);
      const ran = own.filter((item) => item.state === 'passed' || item.state === 'failed');
      return {
        id,
        title: SCENARIOS[id],
        status:
          ran.length === 0
            ? 'not_run'
            : ran.every((item) => item.state === 'passed')
              ? 'passed'
              : 'failed',
        cases: own.map(({ meta, ...rest }) => ({
          ...rest,
          scenario_seed: meta.scenario_seed,
          parameters: meta.parameters,
        })),
      };
    }),
  };
}

/** Casos marcados de los módulos de una corrida. */
function scenarioCases(modules: readonly TestModule[], root: string): ScenarioCase[] {
  const cases: ScenarioCase[] = [];
  for (const module of modules) {
    for (const test of module.children.allTests()) {
      const meta = test.meta().fsApp;
      if (meta === undefined) continue;
      cases.push({
        meta,
        test: test.fullName,
        file: relative(root, module.moduleId),
        state: test.result().state,
        duration_ms: test.diagnostic()?.duration ?? null,
      });
    }
  }
  return cases;
}

export default class FsAppReporter implements Reporter {
  #root = process.cwd();

  onInit(vitest: { readonly config: { readonly root: string } }): void {
    this.#root = vitest.config.root;
  }

  onTestRunEnd(modules: readonly TestModule[]): void {
    const report = buildFsAppReport(scenarioCases(modules, this.#root));
    if (report === null) return;
    const directory = join(this.#root, 'test-results');
    mkdirSync(directory, { recursive: true });
    const path = join(directory, `fs-app-${String(report.run_seed)}.json`);
    writeFileSync(path, `${JSON.stringify(report, null, 2)}\n`, 'utf8');
    process.stdout.write(`[fs-app] informe de escenarios: ${relative(this.#root, path)}\n`);
  }
}
