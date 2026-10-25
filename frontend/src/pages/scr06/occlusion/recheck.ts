/**
 * Nueva consulta de la sesión mientras una prueba de oclusión está `pending` (IF-13 paso 5, nota
 * v1.3): el servidor evalúa la prueba al consultar la sesión, y hasta `deadline` (fin + 5 min)
 * espera los eventos de observabilidad del nodo. La pantalla no sondea: programa **una** lectura
 * de la sesión al vencer el plazo y, si el servidor aún la da por `pending` (su reloj va por
 * detrás del navegador), otra pasado `RECHECK_RETRY_MS`. Ninguna otra lectura se refresca y, sin
 * pruebas pendientes, no queda nada programado.
 */
import { useEffect, useRef } from 'react';

import type { OcclusionTest } from '@/ports/scr06Commissioning';
import type { Clock } from '@/state/clock';

/** Margen tras el plazo: la lectura incluye lo recibido justo en `deadline`. */
const RECHECK_MARGIN_MS = 1_000;
/** Espera entre consultas si el plazo ya venció en el navegador y el servidor sigue `pending`. */
const RECHECK_RETRY_MS = 30_000;

/**
 * Milisegundos hasta la próxima consulta de la sesión, o `null` si ninguna prueba está `pending`
 * con un plazo legible.
 */
function nextRecheckDelayMs(tests: readonly OcclusionTest[], nowMs: number): number | null {
  let earliest: number | null = null;
  for (const test of tests) {
    if (test.verification !== 'pending') continue;
    const deadline = Date.parse(test.deadline);
    if (Number.isNaN(deadline)) continue;
    if (earliest === null || deadline < earliest) earliest = deadline;
  }
  if (earliest === null) return null;
  return earliest > nowMs ? earliest - nowMs + RECHECK_MARGIN_MS : RECHECK_RETRY_MS;
}

/**
 * Programa con el reloj inyectado la consulta de la sesión que resuelve las pruebas pendientes.
 * Se rearma con cada respuesta (`fetchedAt`) y se cancela al desmontar o al resolverse.
 */
export function usePendingRecheck(options: {
  readonly tests: readonly OcclusionTest[];
  readonly clock: Clock;
  readonly fetchedAt: number | null;
  readonly refresh: () => Promise<void>;
}): void {
  const { clock, fetchedAt } = options;
  // `tests` y `refresh` cambian de identidad en cada render: el efecto los lee de la última
  // versión y se rearma solo cuando cambian las pendientes (`pendingKey`) o llega una respuesta.
  const latest = useRef(options);
  useEffect(() => {
    latest.current = options;
  });
  const pendingKey = options.tests
    .filter((test) => test.verification === 'pending')
    .map((test) => `${test.test_id}@${test.deadline}`)
    .join(',');
  useEffect(() => {
    if (pendingKey === '') return undefined;
    const delay = nextRecheckDelayMs(latest.current.tests, clock.now());
    if (delay === null) return undefined;
    const handle = clock.setTimeout(() => {
      void latest.current.refresh();
    }, delay);
    return () => {
      clock.clearTimeout(handle);
    };
  }, [pendingKey, fetchedAt, clock]);
}
