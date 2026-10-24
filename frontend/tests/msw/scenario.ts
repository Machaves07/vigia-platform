/**
 * Escenarios con nombre (LC-APP-25): cada tarea registra los suyos en
 * `tests/msw/scenarios/<escenario>.ts` como `export default { nombre: [variant(...), ...] }` y la
 * prueba los aplica con `server.use(...scenario('nombre'))`. Separado de `handlers.ts` para que un
 * escenario pueda importar `variant` y `apiError` sin un ciclo de módulos.
 */
import type { HttpHandler } from 'msw';

import { scenarios } from './scenarios';

/** Las variantes del escenario `name`; un nombre desconocido falla citando los registrados. */
export function scenario(
  name: string,
  registry: Readonly<Record<string, unknown>> = scenarios,
): readonly HttpHandler[] {
  if (!Object.hasOwn(registry, name)) {
    const known = Object.keys(registry).sort();
    throw new Error(
      `Escenario de MSW desconocido: «${name}». Registrados: ${known.length === 0 ? 'ninguno' : known.join(', ')}.`,
    );
  }
  const handlers = registry[name];
  if (!Array.isArray(handlers) || handlers.length === 0) {
    throw new Error(`El escenario «${name}» debe ser una lista no vacía de manejadores de MSW.`);
  }
  return handlers as HttpHandler[];
}
