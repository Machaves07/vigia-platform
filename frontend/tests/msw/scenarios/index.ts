/**
 * Escenarios de MSW (FS-APP y variantes de manejadores) por descubrimiento: cada tarea añade su
 * `tests/msw/scenarios/<escenario>.ts` con un `export default { nombre: escenario }` y no edita
 * este archivo. Los manejadores generados desde la OpenAPI llegan con VIG-286 (TASK-409).
 */
import { aggregateModules } from '../../support/aggregate';

export const scenarios = aggregateModules<unknown>(
  'tests/msw/scenarios',
  import.meta.glob<unknown>(['./*.ts', '!./index.ts'], { eager: true, import: 'default' }),
);
