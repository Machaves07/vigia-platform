/**
 * Perfiles de fast-check (NFR-APP-43, PAT-APP-MAN-04): `ci` con 200 ejemplos y `nightly` con
 * 2 000, con una semilla por corrida que se imprime y se repite con `FC_SEED`.
 *
 * `vitest.config.ts` resuelve el perfil y la semilla una sola vez por corrida y los pasa a cada
 * archivo de prueba por el entorno; `tests/setup/fastCheck.ts` los aplica con `configureGlobal`.
 */
import { randomInt } from 'node:crypto';

export type FastCheckProfileName = 'ci' | 'nightly';

export interface FastCheckProfile {
  readonly name: FastCheckProfileName;
  readonly numRuns: number;
  readonly seed: number;
}

export const NUM_RUNS: Readonly<Record<FastCheckProfileName, number>> = { ci: 200, nightly: 2000 };

/** Mayor semilla que fast-check acepta sin perder bits (entero de 32 bits con signo). */
const MAX_SEED = 2 ** 31 - 1;

export function parseProfileName(value: string | undefined): FastCheckProfileName {
  if (value === undefined || value === '') return 'ci';
  if (value === 'ci' || value === 'nightly') return value;
  throw new Error(`FC_PROFILE debe ser «ci» o «nightly», no «${value}».`);
}

export function parseSeed(value: string | undefined): number | null {
  if (value === undefined || value === '') return null;
  if (!/^-?\d{1,10}$/.test(value)) throw new Error(`FC_SEED debe ser un entero, no «${value}».`);
  const seed = Number(value);
  if (Math.abs(seed) > MAX_SEED) throw new Error(`FC_SEED fuera de rango: «${value}».`);
  return seed;
}

/** Perfil de la corrida: `profileName` por omisión cuando `FC_PROFILE` no está definido. */
export function resolveProfile(
  env: Readonly<Record<string, string | undefined>>,
  profileName: FastCheckProfileName = 'ci',
): FastCheckProfile {
  const name = env.FC_PROFILE === undefined ? profileName : parseProfileName(env.FC_PROFILE);
  const seed = parseSeed(env.FC_SEED) ?? randomInt(0, MAX_SEED);
  return { name, numRuns: NUM_RUNS[name], seed };
}

export function describeProfile(profile: FastCheckProfile): string {
  return (
    `[fast-check] perfil ${profile.name}: ${String(profile.numRuns)} ejemplos, ` +
    `semilla ${String(profile.seed)} (repítela con FC_SEED=${String(profile.seed)})`
  );
}
