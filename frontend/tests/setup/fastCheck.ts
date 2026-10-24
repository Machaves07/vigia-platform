/**
 * Aplica el perfil de fast-check de la corrida (NFR-APP-43, PAT-APP-MAN-04).
 *
 * `vitest.config.ts` resuelve `FC_PROFILE` y `FC_SEED` una vez y los deja en el entorno de cada
 * archivo: todos los archivos de una corrida comparten la misma semilla, que la salida imprime
 * al empezar. Los contraejemplos reducidos se guardan en `tests/regressions/` (PBT-10).
 */
import fc from 'fast-check';

import { NUM_RUNS, parseProfileName, parseSeed } from '../../config/fastCheckProfile';

const profile = parseProfileName(process.env.FC_PROFILE);
const seed = parseSeed(process.env.FC_SEED);

if (seed === null) {
  throw new Error('Falta FC_SEED: vitest.config.ts debe resolver la semilla de la corrida.');
}

fc.configureGlobal({ numRuns: NUM_RUNS[profile], seed });
