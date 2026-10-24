/**
 * Preparación global de vitest: imprime una vez por corrida el perfil de fast-check y la
 * semilla que `vitest.config.ts` resolvió (NFR-APP-43); `FC_SEED=<semilla>` repite la corrida.
 */
import type { TestProject } from 'vitest/node';

import {
  describeProfile,
  NUM_RUNS,
  parseProfileName,
  parseSeed,
} from '../../config/fastCheckProfile';

export default function setup(project: TestProject): void {
  const env = project.config.env;
  const name = parseProfileName(env.FC_PROFILE);
  const seed = parseSeed(env.FC_SEED);
  if (seed === null) throw new Error('vitest.config.ts no resolvió FC_SEED.');
  process.stdout.write(`${describeProfile({ name, numRuns: NUM_RUNS[name], seed })}\n`);
}
