/**
 * Lectura de la sesión vigente de la zona (`GET /zones/{zone_id}/walk-tests/current`, RV-09) que
 * comparten la prueba de oclusión y el cierre del acta: la misma clave de caché que la consola y
 * la matriz, así que una escritura de cualquier grupo la invalida para todos.
 */
import { scr06CommissioningPort, type CurrentWalkTest } from '@/ports/scr06Commissioning';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';

const CURRENT_WALK_TEST = 'GET /zones/{zone_id}/walk-tests/current';

export function useCurrentWalkTest(zoneId: string, scope: string, concession: string | null) {
  return useScreenQuery<CurrentWalkTest>({
    key: screenKey({
      screen: 'scr06',
      operation: CURRENT_WALK_TEST,
      scope,
      concession,
      params: { zone_id: zoneId },
    }),
    read: ({ signal }) =>
      scr06CommissioningPort.currentWalkTest.read({ path: { zone_id: zoneId } }, signal),
    policy: policyFor(CURRENT_WALK_TEST),
  });
}
