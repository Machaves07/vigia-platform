/**
 * Informe de los escenarios FS-APP (LC-APP-26): los diez constan siempre, con `not_run` si no
 * corrieron; uno falla si falla cualquiera de sus casos; una corrida sin escenarios no escribe
 * informe; casos de semillas distintas no se mezclan.
 */
import { describe, expect, it } from 'vitest';

import { buildFsAppReport, type ScenarioCase } from '../../resilience/report';
import { SCENARIO_IDS, type ScenarioId } from '../../resilience/scenarios';

function scenarioCase(
  id: ScenarioId,
  state: ScenarioCase['state'],
  overrides: { readonly runSeed?: number; readonly test?: string } = {},
): ScenarioCase {
  return {
    meta: {
      id,
      title: 'título',
      run_seed: overrides.runSeed ?? 7,
      scenario_seed: 11,
      parameters: { a: 1 },
    },
    test: overrides.test ?? `${id} caso`,
    file: `tests/resilience/${id}.test.tsx`,
    state,
    duration_ms: 5,
  };
}

describe('buildFsAppReport', () => {
  it('sin escenarios no hay informe', () => {
    expect(buildFsAppReport([])).toBeNull();
  });

  it('lista los diez en orden; los que no corrieron, not_run', () => {
    const report = buildFsAppReport([scenarioCase('FS-APP-03', 'passed')]);
    expect(report?.run_seed).toBe(7);
    expect(report?.scenarios.map((item) => item.id)).toEqual(SCENARIO_IDS);
    expect(report?.scenarios.map((item) => item.status)).toEqual(
      SCENARIO_IDS.map((id) => (id === 'FS-APP-03' ? 'passed' : 'not_run')),
    );
    expect(report?.scenarios[2]?.cases).toEqual([
      {
        test: 'FS-APP-03 caso',
        file: 'tests/resilience/FS-APP-03.test.tsx',
        state: 'passed',
        duration_ms: 5,
        scenario_seed: 11,
        parameters: { a: 1 },
      },
    ]);
  });

  it('un caso fallido hace fallar su escenario; uno omitido no cuenta como pasado', () => {
    const report = buildFsAppReport([
      scenarioCase('FS-APP-08', 'passed', { test: 'a' }),
      scenarioCase('FS-APP-08', 'failed', { test: 'b' }),
      scenarioCase('FS-APP-09', 'skipped'),
    ]);
    const status = Object.fromEntries(
      report?.scenarios.map((item) => [item.id, item.status]) ?? [],
    );
    expect(status['FS-APP-08']).toBe('failed');
    expect(status['FS-APP-09']).toBe('not_run');
  });

  it('rechaza mezclar casos de corridas con semillas distintas', () => {
    expect(() =>
      buildFsAppReport([
        scenarioCase('FS-APP-01', 'passed', { runSeed: 1 }),
        scenarioCase('FS-APP-02', 'passed', { runSeed: 2 }),
      ]),
    ).toThrow(/semillas distintas/);
  });
});
